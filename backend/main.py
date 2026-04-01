"""
Serveur FastAPI — point d'entrée du backend viz_agent_v1.
Lancer avec : uvicorn main:app --reload --port 8000

Endpoints principaux :
  POST /generate-viz   — question → VizIntentResponse + session_id
  POST /refine-viz     — feedback → VizIntentResponse (session préservée)
  POST /setup          — fields   → formules Tableau CASE à créer
  GET  /health         — vérification clé API + connectivité OpenRouter
  GET  /monitoring     — dashboard de monitoring (Airflow-style)
  GET  /api/runs       — liste des runs récents (JSON)
  GET  /api/runs/{id}  — détail d'un run avec IO par étape
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

load_dotenv()

from schema import (
    VizIntentRequest, VizRefinementRequest, VizIntentResponse,
    ErrorResponse, ExecutionTrace, StepTrace, make_default_trace,
)
from openrouter_service import (
    generate_viz_with_reasoning,
    refine_viz_with_reasoning,
    OpenRouterResult,
    OPENROUTER_MODEL,
    OPENROUTER_URL,
)
from viz_mapper import to_tableau_mark_type
from setup_helper import build_setup_guide
from run_store import (
    RunRecord, StepRecord,
    record_run, get_all_runs, get_run, make_run_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_MONITORING_HTML = Path(__file__).parent / "monitoring.html"

app = FastAPI(
    title="Viz Agent — Text-to-Viz Backend",
    version="2.0.0",
    description="Text-to-Viz avec raisonnement OpenRouter (Qwen) + monitoring dashboard.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Helpers trace ──────────────────────────────────────────────────────────

def _step(trace: ExecutionTrace, step_id: str) -> StepTrace:
    return next(s for s in trace.steps if s.id == step_id)


def _fail_trace(trace: ExecutionTrace, step_id: str, error: str) -> ExecutionTrace:
    s = _step(trace, step_id)
    s.status = "error"
    s.error  = error[:500]
    return trace


def _build_response(
    result: OpenRouterResult,
    trace: ExecutionTrace,
    request_fields: list[dict],
) -> VizIntentResponse:
    """Assemble la réponse finale et peuple les champs IO de chaque step."""
    intent = result.intent

    # ── Step 1 : Metadata Extraction (IO inféré des champs reçus) ─────────
    meta = _step(trace, "metadata_extraction")
    dims = [f["name"] for f in request_fields if f.get("role") == "dimension"]
    meas = [f["name"] for f in request_fields if f.get("role") == "measure"]
    meta.input  = "Tableau worksheet — Zone IA"
    meta.output = (
        f"{len(request_fields)} fields\n"
        f"Dimensions : {', '.join(dims) or '—'}\n"
        f"Measures   : {', '.join(meas) or '—'}"
    )

    # ── Step 2 : Request Dispatch ──────────────────────────────────────────
    disp = _step(trace, "request_dispatch")
    disp.input  = f"{len(request_fields)} fields dispatched to FastAPI"
    disp.output = "Request received — forwarding to OpenRouter"

    # ── Step 3 : LLM Processing ────────────────────────────────────────────
    llm             = _step(trace, "llm_processing")
    llm.status      = "success"
    llm.duration_ms = result.llm_duration_ms
    llm.input       = result.prompt_preview
    thinking        = result.reasoning.thinking_text if result.reasoning else None
    thinking_preview = ""
    if thinking:
        thinking_preview = thinking[:400].replace("\n", " ")
        if len(thinking) > 400:
            thinking_preview += " …"
    llm.output = (
        f"viz_type → {intent.viz_type}"
        + (f"\nReasoning : {thinking_preview}" if thinking_preview else "")
    )

    # ── Step 4 : Command Mapping ───────────────────────────────────────────
    cmd             = _step(trace, "command_mapping")
    cmd.status      = "success"
    cmd.duration_ms = result.map_duration_ms
    raw_preview     = (result.llm_raw or "")[:400]
    if len(result.llm_raw or "") > 400:
        raw_preview += " …"
    cmd.input  = raw_preview
    parts = [f"viz_type = {intent.viz_type}"]
    if intent.columns: parts.append(f"columns  = {intent.columns}")
    if intent.rows:    parts.append(f"rows     = {intent.rows}")
    if intent.color:   parts.append(f"color    = {intent.color}")
    if intent.size:    parts.append(f"size     = {intent.size}")
    if intent.filter:  parts.append(f"filter   = {intent.filter.field} ∈ {intent.filter.values}")
    if intent.title:   parts.append(f"title    = {intent.title}")
    cmd.output = "\n".join(parts)

    # ── Step 5 : Viz Execution (JS-side, inferred) ─────────────────────────
    viz           = _step(trace, "viz_execution")
    viz.input     = (
        f"viz_type = {intent.viz_type}\n"
        f"columns  = {intent.columns}\n"
        f"rows     = {intent.rows}"
    )
    viz.output    = "Exécution côté extension Tableau (JS)"

    # ── Méta ───────────────────────────────────────────────────────────────
    trace.reasoning        = result.reasoning
    trace.llm_raw_response = result.llm_raw[:800] if result.llm_raw else None
    trace.attempts         = result.attempts

    intent.tableau_mark_type = to_tableau_mark_type(intent.viz_type)
    intent.session_id        = result.session_id
    intent.execution_trace   = trace
    return intent


# ── Run recording ──────────────────────────────────────────────────────────

def _record_success(
    result: OpenRouterResult,
    intent: VizIntentResponse,
    run_type: str,
    question: str,
    started_at: datetime,
) -> None:
    """Enregistre un run réussi dans le monitoring store."""
    trace = intent.execution_trace
    if not trace:
        return
    duration = (datetime.now(timezone.utc) - started_at).total_seconds() * 1000

    steps = [
        StepRecord(
            id          = s.id,
            label       = s.label,
            status      = s.status,
            duration_ms = s.duration_ms,
            error       = s.error,
            input       = s.input,
            output      = s.output,
        )
        for s in trace.steps
    ]
    run = RunRecord(
        run_id         = make_run_id(),
        run_type       = run_type,
        question       = question,
        session_id     = intent.session_id,
        started_at     = started_at,
        duration_ms    = round(duration, 1),
        status         = "success",
        steps          = steps,
        final_viz_type = intent.viz_type,
        reasoning_text = trace.reasoning.thinking_text if trace.reasoning else None,
        attempts       = trace.attempts,
    )
    record_run(run)


def _record_error(
    run_type: str,
    question: str,
    started_at: datetime,
    error_msg: str,
    trace: ExecutionTrace | None,
) -> None:
    """Enregistre un run en erreur dans le monitoring store."""
    duration = (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
    steps = []
    if trace:
        steps = [
            StepRecord(
                id=s.id, label=s.label, status=s.status,
                duration_ms=s.duration_ms, error=s.error,
                input=s.input, output=s.output,
            )
            for s in trace.steps
        ]
    run = RunRecord(
        run_id        = make_run_id(),
        run_type      = run_type,
        question      = question,
        session_id    = None,
        started_at    = started_at,
        duration_ms   = round(duration, 1),
        status        = "error",
        steps         = steps,
        error_message = error_msg[:500],
    )
    record_run(run)


# ── Gestion centralisée des erreurs OpenRouter ─────────────────────────────

def _handle_openrouter_error(e: Exception, trace: ExecutionTrace) -> HTTPException:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code if e.response else 503
        _fail_trace(trace, "llm_processing", str(e))
        return HTTPException(status_code=status, detail={
            "error": f"OpenRouter HTTP {status}", "detail": str(e), "trace": trace.model_dump(),
        })
    if isinstance(e, httpx.ConnectError):
        _fail_trace(trace, "llm_processing", "Connexion OpenRouter impossible")
        return HTTPException(status_code=503, detail={
            "error": "OpenRouter inaccessible", "detail": str(e), "trace": trace.model_dump(),
        })
    if isinstance(e, httpx.ReadTimeout):
        _fail_trace(trace, "llm_processing", "Timeout OpenRouter")
        return HTTPException(status_code=504, detail={
            "error": "Timeout OpenRouter", "detail": str(e), "trace": trace.model_dump(),
        })
    if isinstance(e, EnvironmentError):
        _fail_trace(trace, "llm_processing", str(e))
        return HTTPException(status_code=500, detail={
            "error": "Clé API manquante", "detail": str(e), "trace": trace.model_dump(),
        })
    if isinstance(e, ValidationError):
        _fail_trace(trace, "llm_processing", f"JSON LLM invalide : {e}")
        return HTTPException(status_code=422, detail={
            "error": "Réponse LLM invalide", "detail": str(e), "trace": trace.model_dump(),
        })
    logger.error("Erreur inattendue : %s", e, exc_info=True)
    _fail_trace(trace, "llm_processing", str(e))
    return HTTPException(status_code=500, detail={
        "error": "Erreur interne", "detail": str(e), "trace": trace.model_dump(),
    })


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/setup", summary="Génère les formules Tableau à créer dans le classeur")
async def setup(body: dict):
    fields = body.get("fields", [])
    if not fields:
        raise HTTPException(status_code=422, detail={"error": "fields requis et non vide."})
    return build_setup_guide(fields)


@app.get("/health")
async def health():
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail={
            "error": "OPENROUTER_API_KEY non définie",
            "detail": "Créer backend/.env avec OPENROUTER_API_KEY=sk-or-...",
        })
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://openrouter.ai/api/v1/models")
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail={
            "error": "OpenRouter inaccessible", "detail": str(e),
        })
    return {"status": "ok", "provider": "openrouter", "model": OPENROUTER_MODEL, "api_key_set": True}


@app.post(
    "/generate-viz",
    response_model=VizIntentResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse},
               503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
async def generate_viz(request: VizIntentRequest):
    logger.info("Question : %r (%d champs)", request.question, len(request.fields))
    started_at = datetime.now(timezone.utc)

    trace = make_default_trace()
    _step(trace, "metadata_extraction").status = "success"
    _step(trace, "request_dispatch").status    = "success"
    _step(trace, "llm_processing").status      = "in_progress"

    try:
        result = await generate_viz_with_reasoning(
            question=request.question,
            fields=request.fields,
        )
    except Exception as e:
        _record_error("generate", request.question, started_at, str(e), trace)
        raise _handle_openrouter_error(e, trace)

    intent = _build_response(result, trace, request.fields)
    _record_success(result, intent, "generate", request.question, started_at)
    logger.info("Intent OK — type=%s llm=%.0fms map=%.0fms", intent.viz_type,
                result.llm_duration_ms, result.map_duration_ms)
    return intent


@app.post(
    "/refine-viz",
    response_model=VizIntentResponse,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
               500: {"model": ErrorResponse}},
    summary="Raffiner le résultat avec un feedback utilisateur",
)
async def refine_viz(request: VizRefinementRequest):
    logger.info("Raffinement session=%s feedback=%r", request.session_id, request.feedback)
    started_at = datetime.now(timezone.utc)

    trace = make_default_trace()
    _step(trace, "metadata_extraction").status = "success"
    _step(trace, "request_dispatch").status    = "success"
    _step(trace, "llm_processing").status      = "in_progress"

    try:
        result = await refine_viz_with_reasoning(
            session_id=request.session_id,
            feedback=request.feedback,
            fields=request.fields,
        )
    except ValueError as e:
        _record_error("refine", request.feedback, started_at, str(e), trace)
        raise HTTPException(status_code=400, detail={"error": "Session invalide", "detail": str(e)})
    except Exception as e:
        _record_error("refine", request.feedback, started_at, str(e), trace)
        raise _handle_openrouter_error(e, trace)

    intent = _build_response(result, trace, request.fields)
    _record_success(result, intent, "refine", request.feedback, started_at)
    logger.info("Raffinement OK — type=%s session=%s", intent.viz_type, intent.session_id)
    return intent


# ── Monitoring ─────────────────────────────────────────────────────────────

@app.get("/monitoring", response_class=HTMLResponse, include_in_schema=False)
async def monitoring():
    """Dashboard de monitoring Airflow-style — http://localhost:8000/monitoring"""
    try:
        html = _MONITORING_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<h1>monitoring.html introuvable dans backend/</h1>"
    return HTMLResponse(content=html)


@app.get("/api/runs", include_in_schema=False)
async def api_runs():
    """Liste des runs récents (summary)."""
    runs = get_all_runs()
    return [
        {
            "run_id":         r.run_id,
            "run_type":       r.run_type,
            "question":       r.question,
            "session_id":     r.session_id,
            "started_at":     r.started_at.isoformat() + "Z",
            "duration_ms":    r.duration_ms,
            "status":         r.status,
            "final_viz_type": r.final_viz_type,
            "attempts":       r.attempts,
            "error_message":  r.error_message,
        }
        for r in runs
    ]


@app.get("/api/runs/{run_id}", include_in_schema=False)
async def api_run_detail(run_id: str):
    """Détail complet d'un run avec IO par étape."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' introuvable.")
    return {
        "run_id":         run.run_id,
        "run_type":       run.run_type,
        "question":       run.question,
        "session_id":     run.session_id,
        "started_at":     run.started_at.isoformat() + "Z",
        "duration_ms":    run.duration_ms,
        "status":         run.status,
        "final_viz_type": run.final_viz_type,
        "reasoning_text": run.reasoning_text,
        "error_message":  run.error_message,
        "attempts":       run.attempts,
        "model":          OPENROUTER_MODEL,
        "steps": [
            {
                "id":          s.id,
                "label":       s.label,
                "status":      s.status,
                "duration_ms": s.duration_ms,
                "error":       s.error,
                "input":       s.input,
                "output":      s.output,
            }
            for s in run.steps
        ],
    }

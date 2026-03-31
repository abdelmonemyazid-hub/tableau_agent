"""
Serveur FastAPI — point d'entrée du backend viz_agent_v1.
Lancer avec : uvicorn main:app --reload --port 8000

Chargement de la clé API :
  Créer un fichier .env à la racine du dossier backend :
    OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxx
  Ou exporter la variable avant de lancer uvicorn :
    set OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxx  (Windows)
"""

import logging
import os
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

# Charger .env si présent (doit être avant les imports qui lisent os.environ)
load_dotenv()

from schema import (
    VizIntentRequest, VizRefinementRequest, VizIntentResponse,
    ErrorResponse, ExecutionTrace, StepTrace, make_default_trace,
)
from openrouter_service import (
    generate_viz_with_reasoning,
    refine_viz_with_reasoning,
    OPENROUTER_MODEL,
    OPENROUTER_URL,
)
from viz_mapper import to_tableau_mark_type

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Viz Agent — Text-to-Viz Backend",
    version="2.0.0",
    description="Text-to-Viz avec raisonnement OpenRouter (Qwen).",
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


def _build_response(result, trace: ExecutionTrace) -> VizIntentResponse:
    """Assemble la réponse finale à partir d'un OpenRouterResult."""
    # Step 3 terminé
    llm_step             = _step(trace, "llm_processing")
    llm_step.status      = "success"
    llm_step.duration_ms = result.llm_duration_ms

    # Step 4 terminé
    map_step             = _step(trace, "command_mapping")
    map_step.status      = "success"
    map_step.duration_ms = result.map_duration_ms

    # Reasoning dans la trace
    trace.reasoning        = result.reasoning
    trace.llm_raw_response = result.llm_raw[:800] if result.llm_raw else None
    trace.attempts         = result.attempts

    intent = result.intent
    intent.tableau_mark_type = to_tableau_mark_type(intent.viz_type)
    intent.session_id        = result.session_id
    intent.execution_trace   = trace
    return intent


# ── Gestion centralisée des erreurs OpenRouter ─────────────────────────────

def _handle_openrouter_error(e: Exception, trace: ExecutionTrace) -> HTTPException:
    """Transforme une exception en HTTPException avec trace partielle."""
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

@app.get("/health")
async def health():
    """Vérifie la configuration de la clé API OpenRouter."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail={
            "error": "OPENROUTER_API_KEY non définie",
            "detail": "Créer un fichier .env avec OPENROUTER_API_KEY=sk-or-...",
        })
    # Ping léger : liste des modèles OpenRouter (sans authentification complète)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://openrouter.ai/api/v1/models")
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail={
            "error": "OpenRouter inaccessible", "detail": str(e),
        })
    return {
        "status": "ok",
        "provider": "openrouter",
        "model": OPENROUTER_MODEL,
        "api_key_set": True,
    }


@app.post(
    "/generate-viz",
    response_model=VizIntentResponse,
    responses={
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def generate_viz(request: VizIntentRequest):
    """
    Génère une intention de visualisation depuis une question en langage naturel.
    Retourne le JSON d'intention + session_id pour le raffinement + execution_trace avec reasoning.
    """
    logger.info("Question : %r (%d champs)", request.question, len(request.fields))

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
        raise _handle_openrouter_error(e, trace)

    intent = _build_response(result, trace)
    logger.info(
        "Intent OK — type=%s llm=%.0fms map=%.0fms session=%s",
        intent.viz_type, result.llm_duration_ms, result.map_duration_ms, result.session_id,
    )
    return intent


@app.post(
    "/refine-viz",
    response_model=VizIntentResponse,
    responses={
        400: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Raffiner le résultat précédent avec un feedback utilisateur",
    description=(
        "Envoie un feedback (ex: 'Are you sure?', 'Use a line chart') au LLM. "
        "Le contexte de raisonnement est préservé via session_id. "
        "Retourne un nouveau VizIntentResponse avec le même session_id."
    ),
)
async def refine_viz(request: VizRefinementRequest):
    """
    Raffinement du graphique via dialogue.
    Le LLM continue son raisonnement depuis le point où il s'était arrêté.
    """
    logger.info("Raffinement session=%s feedback=%r", request.session_id, request.feedback)

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
        # Session introuvable ou expirée
        raise HTTPException(status_code=400, detail={
            "error": "Session invalide", "detail": str(e),
        })
    except Exception as e:
        raise _handle_openrouter_error(e, trace)

    intent = _build_response(result, trace)
    logger.info("Raffinement OK — type=%s session=%s", intent.viz_type, intent.session_id)
    return intent

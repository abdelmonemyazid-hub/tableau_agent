"""
Serveur FastAPI — point d'entrée du backend viz_agent_v1.
Lancer avec : uvicorn main:app --reload --port 8000
"""

import logging
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from schema import (
    VizIntentRequest, VizIntentResponse, ErrorResponse,
    ExecutionTrace, StepTrace, make_default_trace,
)
from llm_service import generate_viz_intent, OLLAMA_BASE_URL, OLLAMA_MODEL
from viz_mapper import to_tableau_mark_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Viz Agent — Text-to-Viz Backend",
    version="1.0.0",
    description="Transforme une question langage naturel en JSON d'intention Tableau.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Helpers trace ──────────────────────────────────────────────────────────

def _step(trace: ExecutionTrace, step_id: str) -> StepTrace:
    """Accès rapide à une étape par son id."""
    return next(s for s in trace.steps if s.id == step_id)


def _fail_trace(trace: ExecutionTrace, step_id: str, error: str) -> ExecutionTrace:
    """Marque une étape en erreur, les suivantes restent pending."""
    s = _step(trace, step_id)
    s.status = "error"
    s.error  = error[:500]   # tronquer pour ne pas exposer de stack trace complète
    return trace


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            res.raise_for_status()
    except Exception as e:
        logger.warning("Ollama inaccessible : %s", e)
        raise HTTPException(
            status_code=503,
            detail={"error": "Ollama inaccessible",
                    "detail": f"Vérifier que `ollama serve` tourne sur {OLLAMA_BASE_URL}"},
        )
    return {"status": "ok", "ollama": True, "model": OLLAMA_MODEL}


@app.get("/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            res.raise_for_status()
        models = [m["name"] for m in res.json().get("models", [])]
        return {"models": models, "current": OLLAMA_MODEL}
    except Exception as e:
        raise HTTPException(status_code=503, detail={"error": str(e)})


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
    Reçoit la question + les métadonnées.
    Retourne un VizIntentResponse avec execution_trace complète.
    En cas d'erreur, retourne la trace partielle dans ErrorResponse.trace.
    """
    logger.info("Question reçue : %r (%d champs)", request.question, len(request.fields))

    trace = make_default_trace()

    # Steps 1 & 2 sont mesurés côté JS — on les marque success ici
    # (le backend reçoit la requête = étapes 1+2 réussies côté client)
    _step(trace, "metadata_extraction").status = "success"
    _step(trace, "request_dispatch").status    = "success"

    # ── Step 3 : LLM Processing ────────────────────────────────────────────
    llm_step = _step(trace, "llm_processing")
    llm_step.status = "in_progress"

    try:
        result = await generate_viz_intent(
            question=request.question,
            fields=request.fields,
        )
    except ValidationError as e:
        _fail_trace(trace, "llm_processing", f"Réponse LLM invalide : {e}")
        raise HTTPException(status_code=422, detail={
            "error": "Réponse LLM invalide", "detail": str(e), "trace": trace.model_dump(),
        })
    except httpx.HTTPStatusError as e:
        _fail_trace(trace, "llm_processing", str(e))
        raise HTTPException(status_code=503, detail={
            "error": "Ollama a retourné une erreur", "detail": str(e), "trace": trace.model_dump(),
        })
    except httpx.ConnectError as e:
        _fail_trace(trace, "llm_processing", "Ollama inaccessible")
        raise HTTPException(status_code=503, detail={
            "error": "Ollama inaccessible",
            "detail": f"Vérifier que `ollama serve` tourne sur {OLLAMA_BASE_URL}",
            "trace": trace.model_dump(),
        })
    except httpx.ReadTimeout:
        _fail_trace(trace, "llm_processing", "Timeout — modèle en cours de chargement en RAM")
        raise HTTPException(status_code=504, detail={
            "error": "Timeout Ollama",
            "detail": "Le modèle dépasse le délai. Réessayer dans quelques secondes.",
            "trace": trace.model_dump(),
        })
    except Exception as e:
        logger.error("Erreur inattendue : %s", e, exc_info=True)
        _fail_trace(trace, "llm_processing", str(e))
        raise HTTPException(status_code=500, detail={
            "error": "Erreur interne", "detail": str(e), "trace": trace.model_dump(),
        })

    # Step 3 terminé
    llm_step.status      = "success"
    llm_step.duration_ms = result.llm_duration_ms
    trace.llm_raw_response = result.llm_raw[:800] if result.llm_raw else None
    trace.attempts         = result.attempts

    # ── Step 4 : Command Mapping ───────────────────────────────────────────
    map_step = _step(trace, "command_mapping")
    map_step.status = "in_progress"

    intent = result.intent
    intent.tableau_mark_type = to_tableau_mark_type(intent.viz_type)

    map_step.status      = "success"
    map_step.duration_ms = result.map_duration_ms

    # ── Step 5 : Viz Execution (sera mis à jour côté JS) ───────────────────
    # Le backend ne peut pas le mesurer — le JS mettra à jour son statut
    _step(trace, "viz_execution").status = "pending"

    # Attacher la trace à la réponse
    intent.execution_trace = trace

    logger.info(
        "Intent généré — type=%s llm=%.0fms map=%.0fms attempts=%d",
        intent.viz_type, result.llm_duration_ms, result.map_duration_ms, result.attempts,
    )
    return intent

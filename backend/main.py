"""
Serveur FastAPI — point d'entrée du backend viz_agent_v1.
Lancer avec : uvicorn main:app --reload --port 8000
"""

import logging
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from schema import VizIntentRequest, VizIntentResponse, ErrorResponse
from llm_service import generate_viz_intent, OLLAMA_BASE_URL, OLLAMA_MODEL
from viz_mapper import to_tableau_mark_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Viz Agent — Text-to-Viz Backend",
    version="1.0.0",
    description="Transforme une question langage naturel en JSON d'intention Tableau.",
)

# CORS : autoriser l'extension Tableau (chargée depuis localhost ou file://)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restreindre si déployé sur un serveur partagé
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Vérifie que le backend ET Ollama sont opérationnels.
    Retourne 503 si Ollama est inaccessible.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            res.raise_for_status()
        ollama_ok = True
    except Exception as e:
        logger.warning("Ollama inaccessible : %s", e)
        ollama_ok = False

    if not ollama_ok:
        raise HTTPException(
            status_code=503,
            detail={"error": "Ollama inaccessible", "detail": f"Vérifier que `ollama serve` tourne sur {OLLAMA_BASE_URL}"},
        )

    return {"status": "ok", "ollama": True, "model": OLLAMA_MODEL}


@app.get("/models")
async def list_models():
    """Liste les modèles disponibles dans Ollama (utile pour le debug)."""
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
    },
)
async def generate_viz(request: VizIntentRequest):
    """
    Reçoit la question + les métadonnées de la source de données.
    Retourne un VizIntentResponse prêt à être consommé par l'extension Tableau.
    """
    logger.info("Question reçue : %r (%d champs)", request.question, len(request.fields))

    try:
        intent = await generate_viz_intent(
            question=request.question,
            fields=request.fields,
        )
    except ValidationError as e:
        logger.error("Réponse LLM invalide : %s", e)
        raise HTTPException(
            status_code=422,
            detail={"error": "Réponse LLM invalide", "detail": str(e)},
        )
    except httpx.HTTPStatusError as e:
        logger.error("Erreur HTTP Ollama : %s", e)
        raise HTTPException(
            status_code=503,
            detail={"error": "Ollama a retourné une erreur", "detail": str(e)},
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail={"error": "Ollama inaccessible", "detail": f"Vérifier que `ollama serve` tourne sur {OLLAMA_BASE_URL}"},
        )
    except httpx.ReadTimeout:
        raise HTTPException(
            status_code=504,
            detail={"error": "Timeout Ollama", "detail": "Le modèle a dépassé le délai. Le premier appel charge le modèle en RAM (~60s). Réessayer dans quelques secondes."},
        )
    except Exception as e:
        logger.error("Erreur inattendue : %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Erreur interne", "detail": str(e)},
        )

    # Enrichissement : ajoute le mark_type Tableau dans le champ prévu par le schéma
    intent.tableau_mark_type = to_tableau_mark_type(intent.viz_type)

    logger.info(
        "Intent généré — type=%s columns=%s rows=%s",
        intent.viz_type, intent.columns, intent.rows,
    )
    return intent

"""
Couche d'interaction avec Ollama.
Responsabilités :
  1. Construire le prompt final (system + user)
  2. Appeler l'API Ollama /api/chat
  3. Extraire et parser le JSON de la réponse (robuste aux imbrications)
  4. Valider le JSON contre VizIntentResponse
  5. Retry automatique (max 2 tentatives) si le JSON est malformé
"""

import json
import re
import logging
import httpx
from functools import lru_cache
from pathlib import Path

from schema import VizIntentResponse

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL    = "llama3.2:latest"  # llama3 nécessite 4.6 GiB — llama3.2 tourne en 2 GiB
OLLAMA_TIMEOUT  = 180.0            # secondes — 3 min (chargement modèle au 1er appel)
MAX_RETRIES     = 2                # tentatives max en cas de JSON malformé

# options : paramètres acceptés dans le champ "options" de l'API Ollama
# stop doit être à la RACINE du payload, pas dans options (Ollama 0.18+)
OLLAMA_OPTIONS = {
    "temperature": 0,
    "num_predict": 350,
}

# stop tokens : niveau racine du payload (hors "options")
OLLAMA_STOP_TOKENS = ["\n\n", "```", "Note:", "Explanation:"]

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system_prompt.txt"


@lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    """Chargé une seule fois au démarrage du serveur, mis en cache."""
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(question: str, fields: list[dict]) -> str:
    fields_str = json.dumps(fields, ensure_ascii=False, indent=2)
    return (
        f"Champs disponibles dans la source de données :\n{fields_str}\n\n"
        f"Question utilisateur : {question}"
    )


def _extract_json(text: str) -> dict:
    """
    Extrait le premier objet JSON valide de la réponse du LLM.

    Stratégies (dans l'ordre) :
      1. Bloc ```json ... ``` — le LLM est bien formaté
      2. Recherche du premier '{' puis comptage de profondeur pour trouver
         le '}' fermant correspondant — robuste aux JSON imbriqués
         (évite le bug de la regex non-greedy qui s'arrête au premier '}')
    """
    # Stratégie 1 : bloc de code markdown
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass  # continuer avec la stratégie 2

    # Stratégie 2 : comptage de profondeur des accolades
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    return json.loads(candidate)  # lève JSONDecodeError si invalide

    raise ValueError(f"Aucun JSON trouvé dans la réponse LLM : {text[:300]!r}")


def _validate_field_names(intent: VizIntentResponse, available_fields: list[dict]) -> None:
    """
    Vérifie que les champs retournés par le LLM existent dans la source de données.
    Lève ValueError avec un message explicite si un champ est inconnu.
    """
    known = {f["name"] for f in available_fields}
    candidates = [
        *intent.columns,
        *intent.rows,
        *([] if intent.color is None else [intent.color]),
        *([] if intent.size  is None else [intent.size]),
        *([] if intent.filter is None else [intent.filter.field]),
    ]
    unknown = [c for c in candidates if c not in known]
    if unknown:
        raise ValueError(
            f"Champ(s) inconnu(s) retourné(s) par le LLM : {unknown}. "
            f"Champs disponibles : {sorted(known)}"
        )


async def generate_viz_intent(question: str, fields: list[dict]) -> VizIntentResponse:
    """
    Appel principal : retourne un VizIntentResponse validé.
    Effectue jusqu'à MAX_RETRIES tentatives si le JSON est malformé.
    """
    system_prompt = _load_system_prompt()
    user_message  = _build_user_message(question, fields)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    last_error: Exception = RuntimeError("Aucune tentative effectuée.")

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("Ollama — tentative %d/%d", attempt, MAX_RETRIES)
        try:
            llm_text = await _call_ollama(messages)
            raw_dict = _extract_json(llm_text)
            intent   = VizIntentResponse(**raw_dict)
            _validate_field_names(intent, fields)
            return intent

        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Tentative %d échouée (JSON invalide) : %s", attempt, e)
            # On ajoute un message de correction pour guider le LLM au retry
            if attempt < MAX_RETRIES:
                messages.append({"role": "assistant", "content": llm_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Ta réponse précédente était invalide : {e}. "
                        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour."
                    ),
                })

    raise ValueError(f"Le LLM n'a pas retourné un JSON valide après {MAX_RETRIES} tentatives. "
                     f"Dernière erreur : {last_error}")


async def _call_ollama(messages: list[dict]) -> str:
    """Appel HTTP brut à Ollama. Retourne le texte brut de la réponse."""
    payload = {
        "model":    OLLAMA_MODEL,
        "stream":   False,
        "options":  OLLAMA_OPTIONS,
        "stop":     OLLAMA_STOP_TOKENS,
        "messages": messages,
    }
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        response = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        response.raise_for_status()
    return response.json()["message"]["content"]

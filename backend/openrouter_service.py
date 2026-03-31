"""
Couche d'interaction avec OpenRouter (remplace llm_service.py / Ollama).

Différences clés vs Ollama :
  - API compatible OpenAI : POST /api/v1/chat/completions
  - Champ "reasoning": {"enabled": True} → retourne reasoning_details
  - reasoning_details doit être CONSERVÉ dans l'historique des messages pour
    que le modèle continue son raisonnement lors d'un appel de raffinement
  - Gestion de sessions en mémoire pour le dialogue multi-tours
"""

import json
import os
import re
import time
import uuid
import logging
import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from schema import VizIntentResponse, ReasoningTrace, ReasoningStep

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "qwen/qwen3.6-plus-preview:free"
OPENROUTER_TIMEOUT = 120.0   # OpenRouter peut être lent sur les modèles gratuits

# Clé API lue depuis la variable d'environnement (jamais en dur dans le code)
def _get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "OPENROUTER_API_KEY n'est pas définie. "
            "Ajouter OPENROUTER_API_KEY=sk-or-... dans le fichier .env"
        )
    return key

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system_prompt.txt"

# ── Gestion des sessions ───────────────────────────────────────────────────
# Chaque session conserve l'historique complet des messages (avec reasoning_details)
# pour permettre au modèle de continuer son raisonnement lors du raffinement.
SESSION_TTL = timedelta(hours=1)

_sessions: dict[str, dict] = {}
# Format : { session_id: { "messages": [...], "created_at": datetime, "last_used": datetime } }


def _new_session(messages: list[dict]) -> str:
    """Crée une nouvelle session et retourne son ID."""
    session_id = str(uuid.uuid4())
    now = datetime.utcnow()
    _sessions[session_id] = {"messages": messages, "created_at": now, "last_used": now}
    _cleanup_sessions()
    return session_id


def _get_session(session_id: str) -> list[dict]:
    """Retourne les messages d'une session existante. Lève KeyError si expirée/inexistante."""
    if session_id not in _sessions:
        raise KeyError(f"Session '{session_id}' introuvable ou expirée.")
    session = _sessions[session_id]
    if datetime.utcnow() - session["last_used"] > SESSION_TTL:
        del _sessions[session_id]
        raise KeyError(f"Session '{session_id}' expirée (TTL: {SESSION_TTL}).")
    session["last_used"] = datetime.utcnow()
    return session["messages"]


def _update_session(session_id: str, messages: list[dict]) -> None:
    if session_id in _sessions:
        _sessions[session_id]["messages"]  = messages
        _sessions[session_id]["last_used"] = datetime.utcnow()


def _cleanup_sessions() -> None:
    """Supprime les sessions expirées (appelé à chaque nouvelle session)."""
    cutoff = datetime.utcnow() - SESSION_TTL
    expired = [sid for sid, s in _sessions.items() if s["last_used"] < cutoff]
    for sid in expired:
        del _sessions[sid]
    if expired:
        logger.info("Sessions expirées supprimées : %d", len(expired))


# ── Données de retour enrichi ─────────────────────────────────────────────

@dataclass
class OpenRouterResult:
    """Résultat complet d'un appel OpenRouter avec reasoning."""
    intent:          VizIntentResponse
    llm_duration_ms: float
    map_duration_ms: float
    llm_raw:         str
    attempts:        int
    reasoning:       ReasoningTrace
    session_id:      str


# ── Helpers ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(question: str, fields: list[dict]) -> str:
    fields_str = json.dumps(fields, ensure_ascii=False, indent=2)
    return (
        f"Champs disponibles dans la source de données :\n{fields_str}\n\n"
        f"Question utilisateur : {question}"
    )


def _extract_json(text: str) -> dict:
    """Extrait le premier objet JSON valide (robuste aux JSON imbriqués)."""
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])

    raise ValueError(f"Aucun JSON trouvé dans la réponse LLM : {text[:300]!r}")


def _validate_field_names(intent: VizIntentResponse, available_fields: list[dict]) -> None:
    known = {f["name"] for f in available_fields}
    candidates = [
        *intent.columns,
        *intent.rows,
        *([] if intent.color  is None else [intent.color]),
        *([] if intent.size   is None else [intent.size]),
        *([] if intent.filter is None else [intent.filter.field]),
    ]
    unknown = [c for c in candidates if c not in known]
    if unknown:
        raise ValueError(
            f"Champ(s) inconnu(s) : {unknown}. Disponibles : {sorted(known)}"
        )


def _parse_reasoning(raw_details: list | None) -> ReasoningTrace:
    """
    Convertit reasoning_details (liste brute OpenRouter) en ReasoningTrace Pydantic.
    Format attendu : [{"type": "thinking", "thinking": "..."}, ...]
    """
    if not raw_details:
        return ReasoningTrace()

    steps = []
    thinking_parts = []

    for item in raw_details:
        step = ReasoningStep(
            type=item.get("type", "text"),
            thinking=item.get("thinking"),
            text=item.get("text"),
        )
        steps.append(step)
        if step.type == "thinking" and step.thinking:
            thinking_parts.append(step.thinking)

    return ReasoningTrace(
        steps=steps,
        thinking_text="\n\n".join(thinking_parts) if thinking_parts else None,
    )


# ── Appel HTTP OpenRouter ──────────────────────────────────────────────────

async def _call_openrouter(messages: list[dict]) -> tuple[str, list, int | None]:
    """
    Appel HTTP vers OpenRouter.
    Retourne (content, reasoning_details, total_tokens).
    """
    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "http://localhost:8000",   # requis par OpenRouter
        "X-Title":       "Viz Agent PFE",
    }
    payload = {
        "model":    OPENROUTER_MODEL,
        "messages": messages,
        "reasoning": {"enabled": True},
    }

    async with httpx.AsyncClient(timeout=OPENROUTER_TIMEOUT) as client:
        response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        response.raise_for_status()

    data    = response.json()
    message = data["choices"][0]["message"]
    content          = message.get("content") or ""
    reasoning_details = message.get("reasoning_details") or []
    total_tokens      = data.get("usage", {}).get("total_tokens")

    return content, reasoning_details, total_tokens


# ── Fonctions publiques ────────────────────────────────────────────────────

async def generate_viz_with_reasoning(
    question: str,
    fields:   list[dict],
) -> OpenRouterResult:
    """
    Premier appel : génère une intention de visualisation avec raisonnement.
    Crée une session pour permettre le raffinement ultérieur.
    """
    system_prompt = _load_system_prompt()
    user_content  = _build_user_message(question, fields)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]

    return await _execute_with_retry(messages, fields, is_new_session=True)


async def refine_viz_with_reasoning(
    session_id: str,
    feedback:   str,
    fields:     list[dict],
) -> OpenRouterResult:
    """
    Appel de raffinement : l'utilisateur corrige ou questionne le résultat précédent.
    Le contexte (avec reasoning_details) est conservé depuis la session.

    Exemple : feedback = "Are you sure? Use a line chart instead."
    """
    try:
        messages = _get_session(session_id)
    except KeyError as e:
        raise ValueError(str(e))

    # Ajouter le feedback utilisateur à l'historique
    messages = list(messages)  # copie défensive
    messages.append({"role": "user", "content": feedback})

    result = await _execute_with_retry(messages, fields, is_new_session=False)

    # Mettre à jour la session avec le nouvel historique
    _update_session(session_id, result._raw_messages)  # type: ignore[attr-defined]
    result.session_id = session_id  # conserver le même session_id

    return result


# ── Logique interne d'exécution avec retry ─────────────────────────────────

async def _execute_with_retry(
    messages:       list[dict],
    fields:         list[dict],
    is_new_session: bool,
    max_retries:    int = 2,
) -> OpenRouterResult:
    """
    Exécute l'appel OpenRouter avec retry sur JSON malformé.
    Préserve reasoning_details dans l'historique entre les tentatives.
    """
    last_error:   Exception   = RuntimeError("Aucune tentative.")
    llm_raw       = ""
    total_llm_ms  = 0.0
    attempts      = 0
    last_reasoning: list      = []

    working_messages = list(messages)

    for attempt in range(1, max_retries + 1):
        attempts = attempt
        logger.info("OpenRouter — tentative %d/%d", attempt, max_retries)

        t_llm = time.perf_counter()
        content, reasoning_details, _ = await _call_openrouter(working_messages)
        total_llm_ms  = (time.perf_counter() - t_llm) * 1000
        llm_raw        = content
        last_reasoning = reasoning_details

        t_map = time.perf_counter()
        try:
            raw_dict = _extract_json(content)
            intent   = VizIntentResponse(**raw_dict)
            _validate_field_names(intent, fields)
            map_ms   = (time.perf_counter() - t_map) * 1000

            # Construire l'historique final en préservant reasoning_details
            # (CRITIQUE pour que le modèle continue son raisonnement au prochain appel)
            final_messages = list(working_messages) + [{
                "role":             "assistant",
                "content":          content,
                "reasoning_details": reasoning_details,  # préservé tel quel
            }]

            session_id = _new_session(final_messages) if is_new_session else "__pending__"

            result = OpenRouterResult(
                intent=intent,
                llm_duration_ms=round(total_llm_ms, 1),
                map_duration_ms=round(map_ms, 1),
                llm_raw=content,
                attempts=attempts,
                reasoning=_parse_reasoning(reasoning_details),
                session_id=session_id,
            )
            # Attacher les messages pour mise à jour de session dans refine
            result._raw_messages = final_messages  # type: ignore[attr-defined]
            return result

        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Tentative %d — JSON invalide : %s", attempt, e)

            if attempt < max_retries:
                # Préserver reasoning_details dans le message d'assistant
                # pour que le modèle continue son raisonnement au retry
                working_messages.append({
                    "role":             "assistant",
                    "content":          content,
                    "reasoning_details": reasoning_details,
                })
                working_messages.append({
                    "role":    "user",
                    "content": (
                        f"Ta réponse précédente était invalide : {e}. "
                        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour."
                    ),
                })

    raise ValueError(
        f"Le LLM n'a pas retourné un JSON valide après {max_retries} tentatives. "
        f"Dernière erreur : {last_error}"
    )

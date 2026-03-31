"""
Couche d'interaction avec Ollama.
Responsabilités :
  1. Construire le prompt final (system + user)
  2. Appeler l'API Ollama /api/chat
  3. Extraire et parser le JSON de la réponse (robuste aux imbrications)
  4. Valider le JSON contre VizIntentResponse
  5. Retry automatique (max 2 tentatives) si le JSON est malformé
  6. Retourner un LLMResult avec timing et trace pour le monitoring
"""

import json
import re
import time
import logging
import httpx
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from schema import VizIntentResponse

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL    = "http://localhost:11434"
OLLAMA_MODEL       = "llama3.2:latest"
OLLAMA_TIMEOUT     = 180.0
MAX_RETRIES        = 2

OLLAMA_OPTIONS = {
    "temperature": 0,
    "num_predict": 350,
}
OLLAMA_STOP_TOKENS = ["\n\n", "```", "Note:", "Explanation:"]

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "system_prompt.txt"


@dataclass
class LLMResult:
    """Résultat enrichi de generate_viz_intent — inclut timing et debug."""
    intent:          VizIntentResponse
    llm_duration_ms: float              # temps de réponse Ollama (ms)
    map_duration_ms: float              # temps de mapping/validation (ms)
    llm_raw:         str                # réponse brute du LLM
    attempts:        int                # nombre de tentatives


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
    """
    Extrait le premier objet JSON valide de la réponse du LLM.
    Stratégie 1 : bloc ```json...```
    Stratégie 2 : comptage de profondeur des accolades (robuste aux JSON imbriqués)
    """
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
            f"Champ(s) inconnu(s) retourné(s) par le LLM : {unknown}. "
            f"Champs disponibles : {sorted(known)}"
        )


async def generate_viz_intent(question: str, fields: list[dict]) -> LLMResult:
    """
    Appel principal.
    Retourne un LLMResult avec l'intent validé + métriques de timing.
    Lève une exception en cas d'échec après MAX_RETRIES tentatives.
    """
    system_prompt = _load_system_prompt()
    user_message  = _build_user_message(question, fields)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_message},
    ]

    last_error: Exception = RuntimeError("Aucune tentative effectuée.")
    llm_raw    = ""
    total_llm_ms = 0.0
    attempts   = 0

    for attempt in range(1, MAX_RETRIES + 1):
        attempts = attempt
        logger.info("Ollama — tentative %d/%d", attempt, MAX_RETRIES)

        # ── Mesure du temps de réponse Ollama ──────────────────────────────
        t_llm_start = time.perf_counter()
        try:
            llm_raw = await _call_ollama(messages)
        except Exception as e:
            # Propager les erreurs réseau directement (timeout, connexion)
            raise
        t_llm_end    = time.perf_counter()
        total_llm_ms = (t_llm_end - t_llm_start) * 1000

        # ── Mesure du temps de mapping/validation ──────────────────────────
        t_map_start = time.perf_counter()
        try:
            raw_dict = _extract_json(llm_raw)
            intent   = VizIntentResponse(**raw_dict)
            _validate_field_names(intent, fields)
            t_map_end = time.perf_counter()
            map_ms    = (t_map_end - t_map_start) * 1000

            return LLMResult(
                intent=intent,
                llm_duration_ms=round(total_llm_ms, 1),
                map_duration_ms=round(map_ms, 1),
                llm_raw=llm_raw,
                attempts=attempts,
            )

        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            logger.warning("Tentative %d — JSON invalide : %s", attempt, e)
            if attempt < MAX_RETRIES:
                messages.append({"role": "assistant", "content": llm_raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Ta réponse précédente était invalide : {e}. "
                        "Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour."
                    ),
                })

    raise ValueError(
        f"Le LLM n'a pas retourné un JSON valide après {MAX_RETRIES} tentatives. "
        f"Dernière erreur : {last_error}"
    )


async def _call_ollama(messages: list[dict]) -> str:
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

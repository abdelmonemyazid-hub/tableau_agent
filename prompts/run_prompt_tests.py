"""
Script de test des prompts contre Ollama.
Exécuter directement depuis le dossier prompts/ :
  python run_prompt_tests.py

Nécessite : pip install httpx
Ollama doit tourner : ollama serve
"""

import json
import re
import sys
import httpx
from pathlib import Path

OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "llama3"
OLLAMA_OPTIONS = {"temperature": 0, "num_predict": 350}

PROMPT_FILE     = Path(__file__).parent / "system_prompt.txt"
TEST_CASES_FILE = Path(__file__).parent / "test_cases.json"

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
WARN = "\033[93m WARN\033[0m"


def load_system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def load_test_cases() -> list[dict]:
    return json.loads(TEST_CASES_FILE.read_text(encoding="utf-8"))


def extract_json(text: str) -> dict | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def call_ollama(system_prompt: str, fields: list[dict], question: str) -> str:
    fields_str = json.dumps(fields, ensure_ascii=False, indent=2)
    user_msg = (
        f"Champs disponibles dans la source de données :\n{fields_str}\n\n"
        f"Question utilisateur : {question}"
    )
    payload = {
        "model":   OLLAMA_MODEL,
        "stream":  False,
        "options": OLLAMA_OPTIONS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
    }
    response = httpx.post(OLLAMA_URL, json=payload, timeout=90.0)
    response.raise_for_status()
    return response.json()["message"]["content"]


def check_result(actual: dict | None, expected: dict, tc_id: str) -> tuple[bool, list[str]]:
    """Compare les champs attendus (subset check) et retourne (ok, erreurs)."""
    if actual is None:
        return False, ["Aucun JSON extrait de la réponse"]
    errors = []
    for key, val in expected.items():
        if key not in actual:
            errors.append(f"  Clé manquante : '{key}'")
        elif key == "filter":
            if actual[key].get("field") != val["field"]:
                errors.append(f"  filter.field: attendu={val['field']} obtenu={actual[key].get('field')}")
        elif actual[key] != val:
            errors.append(f"  '{key}': attendu={val!r} obtenu={actual[key]!r}")
    return len(errors) == 0, errors


def run():
    print(f"\nViz Agent — Prompt Test Runner")
    print(f"Modèle : {OLLAMA_MODEL} | Fichier : {PROMPT_FILE.name}\n")
    print("─" * 60)

    system_prompt = load_system_prompt()
    test_cases    = load_test_cases()

    passed = 0
    failed = 0

    for tc in test_cases:
        tc_id = tc["id"]
        desc  = tc["description"]
        print(f"\n[{tc_id}] {desc}")
        print(f"  Q: {tc['question']!r}")

        try:
            raw = call_ollama(system_prompt, tc["fields"], tc["question"])
            actual = extract_json(raw)
            ok, errors = check_result(actual, tc["expected"], tc_id)

            if ok:
                print(f"  →{PASS}  viz_type={actual.get('viz_type')} | rule: {tc['rule_triggered']}")
                passed += 1
            else:
                print(f"  →{FAIL}")
                for e in errors:
                    print(e)
                print(f"  Raw LLM output: {raw[:200]!r}")
                failed += 1

        except httpx.ConnectError:
            print(f"  →{WARN}  Ollama inaccessible — skipping")
        except Exception as e:
            print(f"  →{FAIL}  Exception: {e}")
            failed += 1

    print("\n" + "─" * 60)
    total = passed + failed
    print(f"Résultats : {passed}/{total} tests passés\n")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run()

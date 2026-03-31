"""
Tests du backend viz_agent_v1.
Aucun appel réel à Ollama — httpx est mocké avec pytest-mock.

Lancer : pytest tests/ -v
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import Response, Request

# ── Fixtures communes ──────────────────────────────────────────────────────

SAMPLE_FIELDS = [
    {"name": "Region",     "type": "string", "role": "dimension"},
    {"name": "Sales",      "type": "float",  "role": "measure"},
    {"name": "Order Date", "type": "date",   "role": "dimension"},
    {"name": "Profit",     "type": "float",  "role": "measure"},
    {"name": "Category",   "type": "string", "role": "dimension"},
]

VALID_INTENT_JSON = {
    "viz_type": "bar",
    "columns":  ["Region"],
    "rows":     ["Sales"],
    "title":    "Ventes par région",
}

VALID_INTENT_WITH_FILTER = {
    "viz_type": "bar",
    "columns":  ["Region"],
    "rows":     ["Sales"],
    "filter":   {"field": "Category", "values": ["Technology"]},
    "title":    "Ventes par région — Technologie",
}


# ── Tests : schema.py ──────────────────────────────────────────────────────

class TestVizIntentResponse:
    def test_valid_intent(self):
        from schema import VizIntentResponse
        intent = VizIntentResponse(**VALID_INTENT_JSON)
        assert intent.viz_type == "bar"
        assert intent.columns == ["Region"]
        assert intent.rows    == ["Sales"]

    def test_invalid_viz_type_raises(self):
        from pydantic import ValidationError
        from schema import VizIntentResponse
        with pytest.raises(ValidationError):
            VizIntentResponse(viz_type="donut", columns=["Region"], rows=["Sales"])

    def test_empty_columns_and_rows_raises(self):
        from pydantic import ValidationError
        from schema import VizIntentResponse
        with pytest.raises(ValidationError, match="au moins un champ"):
            VizIntentResponse(viz_type="bar", columns=[], rows=[])

    def test_strips_whitespace_in_fields(self):
        from schema import VizIntentResponse
        intent = VizIntentResponse(viz_type="bar", columns=["  Region  "], rows=["  Sales  "])
        assert intent.columns == ["Region"]
        assert intent.rows    == ["Sales"]

    def test_filter_model_strict(self):
        from schema import VizIntentResponse
        intent = VizIntentResponse(**VALID_INTENT_WITH_FILTER)
        assert intent.filter.field  == "Category"
        assert intent.filter.values == ["Technology"]

    def test_tableau_mark_type_field_exists(self):
        from schema import VizIntentResponse
        intent = VizIntentResponse(**VALID_INTENT_JSON)
        intent.tableau_mark_type = "Bar"
        dumped = intent.model_dump()
        assert dumped["tableau_mark_type"] == "Bar"


# ── Tests : viz_mapper.py ──────────────────────────────────────────────────

class TestVizMapper:
    def test_known_types(self):
        from viz_mapper import to_tableau_mark_type
        assert to_tableau_mark_type("bar")     == "Bar"
        assert to_tableau_mark_type("line")    == "Line"
        assert to_tableau_mark_type("scatter") == "Circle"
        assert to_tableau_mark_type("pie")     == "Pie"

    def test_unknown_type_defaults_to_bar(self):
        from viz_mapper import to_tableau_mark_type
        assert to_tableau_mark_type("unknown_type") == "Bar"

    def test_case_insensitive(self):
        from viz_mapper import to_tableau_mark_type
        assert to_tableau_mark_type("BAR") == "Bar"
        assert to_tableau_mark_type("Line") == "Line"


# ── Tests : llm_service._extract_json ─────────────────────────────────────

class TestExtractJson:
    def setup_method(self):
        from llm_service import _extract_json
        self.extract = _extract_json

    def test_plain_json(self):
        text = '{"viz_type": "bar", "columns": ["Region"]}'
        assert self.extract(text)["viz_type"] == "bar"

    def test_nested_json(self):
        # Bug original : regex non-greedy s'arrêtait au premier '}'
        text = '{"viz_type": "bar", "filter": {"field": "Category", "values": ["Tech"]}}'
        result = self.extract(text)
        assert result["filter"]["field"] == "Category"

    def test_json_in_markdown_block(self):
        text = 'Voici la réponse :\n```json\n{"viz_type": "line"}\n```'
        assert self.extract(text)["viz_type"] == "line"

    def test_json_with_surrounding_text(self):
        text = "Bien sûr ! Voici le JSON : {\"viz_type\": \"pie\", \"columns\": [\"Category\"]} Bonne visualisation !"
        assert self.extract(text)["viz_type"] == "pie"

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="Aucun JSON"):
            self.extract("Il n'y a pas de JSON ici.")


# ── Tests : _validate_field_names ─────────────────────────────────────────

class TestValidateFieldNames:
    def setup_method(self):
        from llm_service import _validate_field_names
        from schema import VizIntentResponse
        self.validate = _validate_field_names
        self.Response = VizIntentResponse

    def test_valid_fields_pass(self):
        intent = self.Response(**VALID_INTENT_JSON)
        self.validate(intent, SAMPLE_FIELDS)  # ne doit pas lever d'exception

    def test_unknown_column_raises(self):
        intent = self.Response(viz_type="bar", columns=["ChampInexistant"], rows=["Sales"])
        with pytest.raises(ValueError, match="ChampInexistant"):
            self.validate(intent, SAMPLE_FIELDS)


# ── Tests : endpoint /generate-viz (mock httpx) ────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


def _make_ollama_response(intent_dict: dict) -> MagicMock:
    """Crée un faux objet Response Ollama."""
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "message": {"content": json.dumps(intent_dict)}
    }
    return mock


@patch("llm_service.httpx.AsyncClient")
def test_generate_viz_success(mock_client_cls, client):
    mock_http = AsyncMock()
    mock_http.post.return_value = _make_ollama_response(VALID_INTENT_JSON)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_http

    # Mock /health Ollama check
    with patch("main.httpx.AsyncClient") as mock_health_cls:
        mock_health = AsyncMock()
        mock_health.get.return_value = MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={"models": []}))
        mock_health.__aenter__ = AsyncMock(return_value=mock_health)
        mock_health.__aexit__  = AsyncMock(return_value=False)
        mock_health_cls.return_value = mock_health

        res = client.post("/generate-viz", json={
            "question": "Montre les ventes par région",
            "fields":   SAMPLE_FIELDS,
        })

    assert res.status_code == 200
    data = res.json()
    assert data["viz_type"] == "bar"
    assert data["tableau_mark_type"] == "Bar"
    assert "Region" in data["columns"]


@patch("llm_service.httpx.AsyncClient")
def test_generate_viz_llm_returns_bad_json(mock_client_cls, client):
    mock_http = AsyncMock()
    bad_response = MagicMock()
    bad_response.raise_for_status = MagicMock()
    bad_response.json.return_value = {"message": {"content": "Désolé, je ne peux pas répondre."}}
    mock_http.post.return_value = bad_response
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_http

    res = client.post("/generate-viz", json={
        "question": "question test",
        "fields":   SAMPLE_FIELDS,
    })
    assert res.status_code == 500


def test_generate_viz_question_too_short(client):
    res = client.post("/generate-viz", json={
        "question": "ok",  # < 3 chars
        "fields":   SAMPLE_FIELDS,
    })
    assert res.status_code == 422

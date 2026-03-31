"""
Pydantic models — contrat strict entre le LLM et l'extension Tableau.
Toute réponse LLM est validée ici avant d'être renvoyée au frontend.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional

# Types de graphiques supportés par l'API Tableau Extensions v1
SUPPORTED_VIZ_TYPES = Literal[
    "bar", "line", "area", "scatter", "pie",
    "map", "text", "gantt", "histogram", "treemap", "bubble",
]

STEP_STATUS = Literal["pending", "in_progress", "success", "error"]


# ── Monitoring : trace d'exécution ─────────────────────────────────────────

class StepTrace(BaseModel):
    """Trace d'une étape du pipeline agentique."""
    id:          str
    label:       str
    status:      STEP_STATUS        = "pending"
    duration_ms: Optional[float]    = None   # None si pas encore terminé
    error:       Optional[str]      = None   # message d'erreur brut si status="error"


class ExecutionTrace(BaseModel):
    """
    Trace complète du pipeline — incluse dans chaque VizIntentResponse.
    Steps mesurés côté backend : llm_processing, command_mapping.
    Steps mesurés côté frontend JS : metadata_extraction, request_dispatch, viz_execution.
    """
    steps:            list[StepTrace]
    total_duration_ms: Optional[float] = None
    llm_raw_response: Optional[str]    = None   # réponse brute du LLM (debug)
    attempts:         int              = 1       # nombre de tentatives Ollama


def make_default_trace() -> ExecutionTrace:
    """Trace initiale avec les 5 étapes en 'pending' (remplie progressivement)."""
    return ExecutionTrace(steps=[
        StepTrace(id="metadata_extraction", label="Metadata Extraction"),
        StepTrace(id="request_dispatch",    label="Request Dispatch"),
        StepTrace(id="llm_processing",      label="LLM Processing (Ollama)"),
        StepTrace(id="command_mapping",     label="Command Mapping"),
        StepTrace(id="viz_execution",       label="Viz Execution"),
    ])


# ── Modèles principaux ─────────────────────────────────────────────────────

class FilterIntent(BaseModel):
    """Sous-modèle strict pour le filtre — évite les dicts libres non validés."""
    field:  str
    values: list[str] = Field(..., min_length=1)


class VizIntentRequest(BaseModel):
    """Payload envoyé par l'extension au backend."""
    question:   str = Field(..., min_length=3, max_length=500)
    fields:     list[dict]
    sheet_name: str = Field(default="Zone IA")


class VizIntentResponse(BaseModel):
    """
    JSON d'intention retourné par le LLM, validé et enrichi par le backend.
    Consommé par l'extension JS pour piloter Tableau.
    """
    viz_type:          SUPPORTED_VIZ_TYPES
    columns:           list[str]              = Field(default_factory=list)
    rows:              list[str]              = Field(default_factory=list)
    color:             Optional[str]          = None
    size:              Optional[str]          = None
    filter:            Optional[FilterIntent] = None
    title:             Optional[str]          = None
    tableau_mark_type: Optional[str]          = None   # enrichi par main.py
    execution_trace:   Optional[ExecutionTrace] = None  # monitoring pipeline

    @field_validator("columns", "rows")
    @classmethod
    def strip_empty(cls, v):
        return [f.strip() for f in v if f.strip()]

    @model_validator(mode="after")
    def at_least_one_axis(self) -> "VizIntentResponse":
        if not self.columns and not self.rows:
            raise ValueError("Le JSON d'intention doit contenir au moins un champ dans columns ou rows.")
        return self


class ErrorResponse(BaseModel):
    error:  str
    detail: Optional[str]          = None
    trace:  Optional[ExecutionTrace] = None   # trace partielle en cas d'erreur

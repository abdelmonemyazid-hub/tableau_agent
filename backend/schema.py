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


# ── Reasoning (OpenRouter) ─────────────────────────────────────────────────

class ReasoningStep(BaseModel):
    """Un pas individuel du raisonnement retourné par le modèle."""
    type:     str                  # "thinking" | "text"
    thinking: Optional[str] = None # contenu du raisonnement interne
    text:     Optional[str] = None # contenu textuel (si type="text")


class ReasoningTrace(BaseModel):
    """
    Trace de raisonnement complète retournée par OpenRouter (reasoning_details).
    Affichée dans le Flow Monitor pour expliquer les décisions du LLM.
    """
    steps:        list[ReasoningStep] = Field(default_factory=list)
    thinking_text: Optional[str]      = None  # concaténation de tous les steps "thinking"
    tokens_used:  Optional[int]       = None  # tokens de raisonnement utilisés


# ── Monitoring : trace d'exécution ─────────────────────────────────────────

class StepTrace(BaseModel):
    id:          str
    label:       str
    status:      STEP_STATUS     = "pending"
    duration_ms: Optional[float] = None
    error:       Optional[str]   = None


class ExecutionTrace(BaseModel):
    steps:             list[StepTrace]
    total_duration_ms: Optional[float]   = None
    llm_raw_response:  Optional[str]     = None
    attempts:          int               = 1
    reasoning:         Optional[ReasoningTrace] = None  # raisonnement LLM (OpenRouter)


def make_default_trace() -> ExecutionTrace:
    return ExecutionTrace(steps=[
        StepTrace(id="metadata_extraction", label="Metadata Extraction"),
        StepTrace(id="request_dispatch",    label="Request Dispatch"),
        StepTrace(id="llm_processing",      label="LLM Processing"),
        StepTrace(id="command_mapping",     label="Command Mapping"),
        StepTrace(id="viz_execution",       label="Viz Execution"),
    ])


# ── Modèles principaux ─────────────────────────────────────────────────────

class FilterIntent(BaseModel):
    field:  str
    values: list[str] = Field(..., min_length=1)


class VizIntentRequest(BaseModel):
    """Payload de la première question (génération initiale)."""
    question:   str = Field(..., min_length=3, max_length=500)
    fields:     list[dict]
    sheet_name: str = Field(default="Zone IA")


class VizRefinementRequest(BaseModel):
    """
    Payload pour raffiner le résultat précédent ("Are you sure?", correction).
    Le session_id est retourné par /generate-viz et permet de conserver
    le contexte de raisonnement du LLM pour un suivi cohérent.
    """
    session_id: str  = Field(..., min_length=1)
    feedback:   str  = Field(..., min_length=3, max_length=500,
                             description="Ex: 'Use a line chart instead' ou 'Are you sure?'")
    fields:     list[dict]  # re-fourni pour re-valider les champs après correction


class VizIntentResponse(BaseModel):
    """
    JSON d'intention retourné par le LLM, validé et enrichi.
    Inclut session_id pour les appels de raffinement ultérieurs.
    """
    viz_type:          SUPPORTED_VIZ_TYPES
    columns:           list[str]               = Field(default_factory=list)
    rows:              list[str]               = Field(default_factory=list)
    color:             Optional[str]           = None
    size:              Optional[str]           = None
    filter:            Optional[FilterIntent]  = None
    title:             Optional[str]           = None
    tableau_mark_type: Optional[str]           = None
    session_id:        Optional[str]           = None  # pour /refine-viz
    execution_trace:   Optional[ExecutionTrace] = None

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
    detail: Optional[str]            = None
    trace:  Optional[ExecutionTrace] = None

"""
Pydantic models — contrat strict entre le LLM et l'extension Tableau.
Toute réponse LLM est validée ici avant d'être renvoyée au frontend.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional

# Types de graphiques supportés par l'API Tableau Extensions v1
SUPPORTED_VIZ_TYPES = Literal[
    "bar",
    "line",
    "area",
    "scatter",
    "pie",
    "map",
    "text",       # tableau de texte / crosstab
    "gantt",
    "histogram",
    "treemap",
    "bubble",
]


class FilterIntent(BaseModel):
    """Sous-modèle strict pour le filtre — évite les dicts libres non validés."""
    field: str
    values: list[str] = Field(..., min_length=1)


class VizIntentRequest(BaseModel):
    """Payload envoyé par l'extension au backend."""
    question: str = Field(..., min_length=3, max_length=500)
    fields: list[dict]  # [{name: str, type: str, role: str}, ...]
    sheet_name: str = Field(default="Zone IA")


class VizIntentResponse(BaseModel):
    """
    JSON d'intention retourné par le LLM et validé par le backend.
    C'est ce que l'extension JS consomme pour piloter Tableau.
    """
    viz_type: SUPPORTED_VIZ_TYPES
    columns: list[str] = Field(default_factory=list)   # axe X / dimensions
    rows: list[str] = Field(default_factory=list)      # axe Y / mesures
    color: Optional[str] = Field(default=None)         # encodage couleur
    size: Optional[str] = Field(default=None)          # encodage taille
    filter: Optional[FilterIntent] = Field(default=None)
    title: Optional[str] = Field(default=None)

    # Enrichi par main.py APRÈS validation — doit être dans le modèle
    # pour ne pas être supprimé par Pydantic lors de la sérialisation
    tableau_mark_type: Optional[str] = Field(default=None)

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
    error: str
    detail: Optional[str] = None

"""
Table de correspondance entre les viz_type du JSON d'intention
et les constantes réelles de l'API Tableau Extensions.

Référence : https://tableau.github.io/extensions-api/docs/enumerations/tableau_marktype.html
"""

# Tableau Extensions API : tableau.MarkType (pour changeVizTypeAsync)
TABLEAU_MARK_TYPE_MAP: dict[str, str] = {
    "bar":       "Bar",
    "line":      "Line",
    "area":      "Area",
    "scatter":   "Circle",
    "pie":       "Pie",
    "map":       "Map",
    "text":      "Square",     # Crosstab / texte = carré dans Tableau
    "gantt":     "Gantt",
    "histogram": "Bar",        # Histogram = Bar avec agrégation binée
    "treemap":   "Square",
    "bubble":    "Circle",
}

# Agrégations par défaut selon le rôle du champ
DEFAULT_AGGREGATION_MAP: dict[str, str] = {
    "measure": "SUM",
    "dimension": "NONE",
    "date": "YEAR",
}


def to_tableau_mark_type(viz_type: str) -> str:
    """Retourne la constante Tableau correspondant au type de viz."""
    return TABLEAU_MARK_TYPE_MAP.get(viz_type.lower(), "Bar")


def requires_bin(viz_type: str) -> bool:
    """Indique si le type nécessite une discrétisation en intervalles."""
    return viz_type.lower() == "histogram"

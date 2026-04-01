"""
Générateur de formules Tableau pour la configuration de "Zone IA".

Principe :
  L'extension ne peut pas modifier directement les étagères Tableau (Rows/Columns).
  Solution : des Champs Calculés Dynamiques pilotés par des Paramètres Tableau.

  Quand l'extension change [p_VizAgent_Column] → "Region",
  le Champ Calculé [VizAgent Dimension] retourne [Region] → Tableau re-rend
  le graphique natif sur "Zone IA" automatiquement.

Paramètres à créer (une fois dans le classeur Tableau) :
  - p_VizAgent_Column  (String)
  - p_VizAgent_Row     (String)
  - p_VizAgent_Color   (String, optionnel)

Champs Calculés à placer sur les étagères de "Zone IA" :
  - VizAgent Dimension → Columns
  - VizAgent Measure   → Rows
  - VizAgent Color     → Color (optionnel)
"""


def generate_dimension_formula(fields: list[dict]) -> str:
    dims = [f for f in fields if f.get("role") == "dimension"]
    if not dims:
        return '""  // Aucun champ dimension trouvé'
    lines = ["CASE [p_VizAgent_Column]"]
    for f in dims:
        lines.append(f'  WHEN "{f["name"]}" THEN STR([{f["name"]}])')
    lines += ['  ELSE ""', "END"]
    return "\n".join(lines)


def generate_measure_formula(fields: list[dict]) -> str:
    meas = [f for f in fields if f.get("role") == "measure"]
    if not meas:
        return "0  // Aucun champ mesure trouvé"
    lines = ["CASE [p_VizAgent_Row]"]
    for f in meas:
        lines.append(f'  WHEN "{f["name"]}" THEN [{f["name"]}]')
    lines += ["  ELSE 0", "END"]
    return "\n".join(lines)


def generate_color_formula(fields: list[dict]) -> str:
    dims = [f for f in fields if f.get("role") == "dimension"]
    if not dims:
        return '""'
    lines = ["CASE [p_VizAgent_Color]"]
    for f in dims:
        lines.append(f'  WHEN "{f["name"]}" THEN STR([{f["name"]}])')
    lines += ['  ELSE ""', "END"]
    return "\n".join(lines)


def build_setup_guide(fields: list[dict]) -> dict:
    dims      = [f for f in fields if f.get("role") == "dimension"]
    meas      = [f for f in fields if f.get("role") == "measure"]
    dim_names = [f["name"] for f in dims]
    mea_names = [f["name"] for f in meas]

    return {
        "parameters": [
            {
                "name":             "p_VizAgent_Column",
                "type":             "String",
                "default_value":    dim_names[0] if dim_names else "",
                "allowable_values": dim_names,
                "description":      "Dimension affichée sur l'axe X de Zone IA",
            },
            {
                "name":             "p_VizAgent_Row",
                "type":             "String",
                "default_value":    mea_names[0] if mea_names else "",
                "allowable_values": mea_names,
                "description":      "Mesure affichée sur l'axe Y de Zone IA",
            },
            {
                "name":             "p_VizAgent_Color",
                "type":             "String",
                "default_value":    "",
                "allowable_values": [""] + dim_names,
                "description":      "Champ d'encodage couleur (optionnel)",
            },
        ],
        "calculated_fields": [
            {
                "name":    "VizAgent Dimension",
                "shelf":   "Columns de Zone IA",
                "formula": generate_dimension_formula(fields),
            },
            {
                "name":        "VizAgent Measure",
                "shelf":       "Rows de Zone IA",
                "formula":     generate_measure_formula(fields),
                "aggregation": "SUM(VizAgent Measure)",
            },
            {
                "name":    "VizAgent Color",
                "shelf":   "Color de Zone IA (optionnel)",
                "formula": generate_color_formula(fields),
            },
        ],
        "steps": [
            "1. Ouvrir le classeur Tableau Desktop avec la feuille 'Zone IA'",
            "2. Créer les 3 Paramètres (Analyse → Créer un paramètre)",
            "3. Créer les 3 Champs Calculés (clic droit dans le panneau Données)",
            "4. Glisser [VizAgent Dimension] sur Colonnes de Zone IA",
            "5. Glisser SUM([VizAgent Measure]) sur Lignes de Zone IA",
            "6. Optionnel : glisser [VizAgent Color] sur Couleur",
            "7. Sauvegarder le classeur",
            "✓ L'agent peut maintenant piloter les graphiques sur Zone IA",
        ],
        "note": (
            "Limitation V1 : le type de graphique ne peut pas être changé "
            "automatiquement via l'Extensions API. Configurer Zone IA en Barres pour commencer."
        ),
    }

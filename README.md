# Viz Agent V1 — Text-to-Viz pour Tableau Desktop

Agent local qui transforme une question en langage naturel en visualisation Tableau.

## Architecture

```
viz_agent_v1/
├── backend/
│   ├── main.py           # Serveur FastAPI (point d'entrée)
│   ├── llm_service.py    # Interaction avec Ollama
│   ├── schema.py         # Modèles Pydantic (validation JSON)
│   ├── viz_mapper.py     # Mapping viz_type → Tableau MarkType
│   └── requirements.txt
├── extension/
│   ├── viz_agent.trex    # Manifest Tableau Extension
│   ├── index.html        # Interface utilisateur
│   ├── viz_agent.js      # Logique JS (API Tableau Extensions)
│   └── style.css
└── prompts/
    └── system_prompt.txt # Prompt système pour Ollama
```

## Prérequis

- Python 3.11+
- [Ollama](https://ollama.com) installé et en cours d'exécution
- Tableau Desktop 2021.4+ (support Extensions API 1.7)

## Démarrage rapide

### 1. Modèle Ollama
```bash
ollama pull llama3
```

### 2. Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### 3. Serveur de l'extension (HTTP simple)
```bash
cd extension
python -m http.server 8765
```

### 4. Tableau Desktop
1. Ouvrir un dashboard contenant une feuille nommée **"Zone IA"**
2. Ajouter une Extension → choisir le fichier `extension/viz_agent.trex`
3. Poser une question dans l'interface

## Flux de données

```
[Utilisateur] → question
      ↓
[Extension JS] → extraction des métadonnées (getDataSourcesAsync)
      ↓
[Backend FastAPI /generate-viz]
      ↓
[Ollama LLM] → JSON d'intention validé
      ↓
[Extension JS] → addColumnsAsync / addRowsAsync / changeVizTypeAsync
      ↓
[Tableau Desktop] → visualisation mise à jour
```

## Configuration

| Paramètre | Fichier | Valeur par défaut |
|---|---|---|
| Modèle LLM | `backend/llm_service.py` | `llama3` |
| Port backend | `backend/main.py` | `8000` |
| Port extension | commande `http.server` | `8765` |
| Feuille cible | `extension/viz_agent.js` | `Zone IA` |

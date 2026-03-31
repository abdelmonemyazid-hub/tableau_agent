/**
 * viz_agent.js — Extension Tableau Text-to-Viz
 *
 * Contrainte Extensions API v1 (dashboard extension) :
 *   ✓ Lecture des champs       : worksheet.getDataSourcesAsync()
 *   ✓ Filtres                  : worksheet.applyFilterAsync() / clearFilterAsync()
 *   ✓ Paramètres               : parameter.changeValueAsync()
 *   ✗ Modification des étagères: addColumnsAsync / clearRowsAsync → N'EXISTE PAS
 *   ✗ Changement de type de viz: changeVizTypeAsync           → N'EXISTE PAS
 *
 * Stratégie applyIntent :
 *   1. Applique le filtre si présent (API disponible)
 *   2. Tente de passer les champs via des paramètres Tableau pré-configurés
 *      (p_VizAgent_Column, p_VizAgent_Row, p_VizAgent_Color)
 *   3. Affiche une carte récapitulatif pour permettre à l'utilisateur
 *      d'appliquer manuellement ce que l'API ne peut pas faire
 */

"use strict";

// ── Configuration ──────────────────────────────────────────────────────────
const CONFIG = {
  BACKEND_URL:  "http://localhost:8000",
  TARGET_SHEET: "Zone IA",
  // Noms des paramètres Tableau à créer dans le classeur pour le contrôle automatique
  // Si absents, l'extension affiche les suggestions sans les appliquer
  PARAM_COLUMN: "p_VizAgent_Column",
  PARAM_ROW:    "p_VizAgent_Row",
  PARAM_COLOR:  "p_VizAgent_Color",
  MAX_HISTORY:  10,
};

// ── État de l'extension ────────────────────────────────────────────────────
const state = {
  initialized: false,
  lastFields:  [],          // cache des champs extraits
  history:     loadHistory(),
};

// ── Références DOM ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const questionInput  = $("question-input");
const submitBtn      = $("submit-btn");
const clearBtn       = $("clear-btn");
const statusBar      = $("status-bar");
const resultCard     = $("result-card");
const historyList    = $("history-list");
const debugContent   = $("debug-content");
const debugToggle    = $("debug-toggle");
const fieldsBadges   = $("fields-badges");

// ── Utilitaires UI ─────────────────────────────────────────────────────────
function setStatus(message, type = "idle") {
  statusBar.textContent = message;
  statusBar.className   = type;
}

function setLoading(isLoading) {
  submitBtn.disabled     = isLoading;
  questionInput.disabled = isLoading;
  clearBtn.disabled      = isLoading;
}

function showDebug(data) {
  debugContent.textContent = JSON.stringify(data, null, 2);
}

function showResultCard(intent) {
  const applied   = [];
  const manual    = [];

  if (intent._filtersApplied?.length) {
    applied.push(`Filtre : <strong>${intent._filtersApplied.join(", ")}</strong>`);
  }
  if (intent._paramsSet?.length) {
    applied.push(`Paramètres : <strong>${intent._paramsSet.join(", ")}</strong>`);
  }
  if (intent.columns?.length) {
    manual.push(`Colonnes → <code>${intent.columns.join(", ")}</code>`);
  }
  if (intent.rows?.length) {
    manual.push(`Lignes → <code>${intent.rows.join(", ")}</code>`);
  }
  if (intent.viz_type) {
    manual.push(`Type : <code>${intent.viz_type}</code> (mark: <code>${intent.tableau_mark_type}</code>)`);
  }
  if (intent.color) {
    manual.push(`Couleur → <code>${intent.color}</code>`);
  }

  let html = "";
  if (applied.length) {
    html += `<div class="rc-section rc-applied">
      <span class="rc-label">Appliqué automatiquement</span>
      <ul>${applied.map((a) => `<li>${a}</li>`).join("")}</ul>
    </div>`;
  }
  if (manual.length) {
    html += `<div class="rc-section rc-manual">
      <span class="rc-label">À appliquer manuellement sur "Zone IA"</span>
      <ul>${manual.map((m) => `<li>${m}</li>`).join("")}</ul>
    </div>`;
  }

  resultCard.innerHTML  = html;
  resultCard.classList.remove("hidden");
}

function renderFieldBadges(fields) {
  const dims = fields.filter((f) => f.role === "dimension");
  const meas = fields.filter((f) => f.role === "measure");
  const badge = (f, cls) =>
    `<span class="badge badge-${cls}" title="${f.type}">${f.name}</span>`;

  fieldsBadges.innerHTML =
    dims.map((f) => badge(f, "dim")).join("") +
    meas.map((f) => badge(f, "mea")).join("");
}

function renderHistory() {
  if (!state.history.length) {
    historyList.innerHTML = '<li class="history-empty">Aucune question posée</li>';
    return;
  }
  historyList.innerHTML = state.history
    .map(
      (q, i) =>
        `<li class="history-item" data-index="${i}" title="${q}">${q}</li>`
    )
    .join("");

  historyList.querySelectorAll(".history-item").forEach((li) => {
    li.addEventListener("click", () => {
      questionInput.value = li.title;
      questionInput.focus();
    });
  });
}

debugToggle.addEventListener("click", () => {
  const visible = debugContent.classList.toggle("visible");
  debugToggle.textContent = (visible ? "▾" : "▸") + " JSON d'intention (debug)";
});

// ── Historique (localStorage) ──────────────────────────────────────────────
function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem("vizagent_history") ?? "[]");
  } catch {
    return [];
  }
}

function saveHistory(question) {
  state.history = [question, ...state.history.filter((q) => q !== question)].slice(
    0,
    CONFIG.MAX_HISTORY
  );
  try {
    localStorage.setItem("vizagent_history", JSON.stringify(state.history));
  } catch {
    /* localStorage indisponible dans certains contextes Tableau — silencieux */
  }
  renderHistory();
}

// ── 1. Initialisation Tableau Extensions API ───────────────────────────────
tableau.extensions.initializeAsync().then(async () => {
  setStatus("Connexion au backend...", "loading");

  const backendOk = await checkBackend();
  if (!backendOk) return;

  state.initialized = true;
  setStatus("Prêt.", "idle");
  renderHistory();

  // Pré-charger les champs de "Zone IA" au démarrage
  try {
    state.lastFields = await extractFields();
    renderFieldBadges(state.lastFields);
  } catch {
    /* Non bloquant — les champs seront extraits à la soumission */
  }

  submitBtn.addEventListener("click", handleSubmit);
  clearBtn.addEventListener("click", handleClear);

  questionInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  });
}).catch((err) => {
  setStatus(`Erreur d'initialisation Tableau : ${err.message}`, "error");
});

// ── Vérification santé du backend ──────────────────────────────────────────
async function checkBackend() {
  try {
    const res = await fetch(`${CONFIG.BACKEND_URL}/health`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setStatus(`Backend KO : ${body.detail?.error ?? `HTTP ${res.status}`}`, "error");
      return false;
    }
    return true;
  } catch {
    setStatus(
      `Backend inaccessible. Lancer : uvicorn main:app --port 8000`,
      "error"
    );
    return false;
  }
}

// ── 2. Extraction des métadonnées ──────────────────────────────────────────
// CORRECTION BUG #1 : getDataSourcesAsync() s'appelle sur un Worksheet,
// pas sur le Dashboard.
async function extractFields() {
  const sheet = getTargetSheet();
  const dataSources = await sheet.getDataSourcesAsync();

  if (!dataSources.length) throw new Error("Aucune source de données sur la feuille « Zone IA ».");

  const ds = dataSources[0];

  // CORRECTION BUG #4 : f.role est un enum FieldRoleType, pas une string.
  // On normalise en minuscules pour correspondre au schéma backend.
  return ds.fields
    .filter((f) => !f.isHidden)
    .map((f) => ({
      name: f.name,
      type: f.dataType?.toLowerCase() ?? "string",   // enum → string
      role: f.role?.toLowerCase() ?? "dimension",    // enum → "dimension"|"measure"
    }));
}

// ── Utilitaire : trouver la feuille cible ──────────────────────────────────
function getTargetSheet() {
  const sheet = tableau.extensions.dashboardContent.dashboard.worksheets.find(
    (ws) => ws.name === CONFIG.TARGET_SHEET
  );
  if (!sheet) {
    throw new Error(
      `Feuille "${CONFIG.TARGET_SHEET}" introuvable. ` +
      `Vérifier qu'une feuille nommée exactement "${CONFIG.TARGET_SHEET}" est présente dans ce dashboard.`
    );
  }
  return sheet;
}

// ── 3. Appel backend FastAPI ───────────────────────────────────────────────
async function callBackend(question, fields) {
  const response = await fetch(`${CONFIG.BACKEND_URL}/generate-viz`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({
      question,
      fields,
      sheet_name: CONFIG.TARGET_SHEET,
    }),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail?.error ?? `Erreur backend HTTP ${response.status}`);
  }

  return response.json();
}

// ── 4. Application du JSON d'intention sur Tableau ─────────────────────────
//
// Stratégie en 3 niveaux :
//   Niveau 1 — Filtre    : applyFilterAsync()        → toujours disponible
//   Niveau 2 — Paramètres: parameter.changeValueAsync() → si paramètres configurés
//   Niveau 3 — Manuel    : affichage récapitulatif    → fallback universel
//
// CORRECTION BUGS #2 & #3 : clearColumnsAsync, addColumnsAsync,
// clearRowsAsync, addRowsAsync, changeVizTypeAsync N'EXISTENT PAS
// dans l'Extensions API v1. Remplacés par l'approche ci-dessus.
async function applyIntent(intent) {
  const sheet = getTargetSheet();
  intent._filtersApplied = [];
  intent._paramsSet      = [];

  // ── Niveau 1 : Filtres ─────────────────────────────────────────────────
  // D'abord on retire les anciens filtres de l'agent pour repartir proprement
  await clearAgentFilters(sheet);

  if (intent.filter?.field && intent.filter?.values?.length) {
    await sheet.applyFilterAsync(
      intent.filter.field,
      intent.filter.values,
      tableau.FilterUpdateType.Replace
    );
    intent._filtersApplied.push(
      `${intent.filter.field} = ${intent.filter.values.join(", ")}`
    );
  }

  // ── Niveau 2 : Paramètres ──────────────────────────────────────────────
  // Tente de renseigner les paramètres pré-configurés dans le classeur Tableau.
  // Si le paramètre n'existe pas, on passe silencieusement au niveau 3.
  const paramMap = {
    [CONFIG.PARAM_COLUMN]: intent.columns?.[0] ?? null,
    [CONFIG.PARAM_ROW]:    intent.rows?.[0]    ?? null,
    [CONFIG.PARAM_COLOR]:  intent.color        ?? null,
  };

  try {
    const parameters = await tableau.extensions.dashboardContent.dashboard.getParametersAsync();
    const paramIndex  = Object.fromEntries(parameters.map((p) => [p.name, p]));

    for (const [paramName, value] of Object.entries(paramMap)) {
      if (value && paramIndex[paramName]) {
        await paramIndex[paramName].changeValueAsync(value);
        intent._paramsSet.push(`${paramName} = "${value}"`);
      }
    }
  } catch {
    /* Les paramètres sont optionnels — pas bloquant */
  }

  return intent;
}

// ── Nettoyage des filtres précédents de l'agent ────────────────────────────
// On mémorise les champs filtrés lors de la dernière exécution pour pouvoir
// les retirer proprement à la prochaine.
const _lastFilteredFields = new Set();

async function clearAgentFilters(sheet) {
  for (const field of _lastFilteredFields) {
    try {
      await sheet.clearFilterAsync(field);
    } catch {
      /* Le champ n'était peut-être plus filtré */
    }
  }
  _lastFilteredFields.clear();
}

// ── Handler : Générer ──────────────────────────────────────────────────────
async function handleSubmit() {
  const question = questionInput.value.trim();
  if (!question) {
    setStatus("Saisissez une question.", "error");
    return;
  }

  setLoading(true);
  resultCard.classList.add("hidden");

  try {
    // Étape 1 — Extraction des champs
    setStatus("Extraction des métadonnées...", "loading");
    state.lastFields = await extractFields();
    renderFieldBadges(state.lastFields);

    // Étape 2 — Appel LLM
    setStatus("Consultation du modèle IA (peut prendre ~5s)...", "loading");
    const intent = await callBackend(question, state.lastFields);
    showDebug(intent);

    // Étape 3 — Application
    setStatus("Application de l'intention...", "loading");
    const enrichedIntent = await applyIntent(intent);

    // Mémoriser les champs filtrés pour le prochain nettoyage
    if (intent.filter?.field) _lastFilteredFields.add(intent.filter.field);

    showResultCard(enrichedIntent);
    saveHistory(question);
    setStatus(`Intention "${intent.viz_type}" traitée.`, "success");

  } catch (err) {
    setStatus(`Erreur : ${err.message}`, "error");
    console.error("[VizAgent]", err);
  } finally {
    setLoading(false);
  }
}

// ── Handler : Réinitialiser ────────────────────────────────────────────────
async function handleClear() {
  questionInput.value = "";
  resultCard.classList.add("hidden");
  debugContent.textContent = "";
  fieldsBadges.innerHTML   = "";
  setStatus("Réinitialisé.", "idle");

  // Retirer les filtres de l'agent
  try {
    const sheet = getTargetSheet();
    await clearAgentFilters(sheet);
  } catch {
    /* Non bloquant */
  }
}

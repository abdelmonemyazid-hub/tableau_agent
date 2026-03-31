/**
 * viz_agent.js — Extension Tableau Text-to-Viz avec Pipeline Monitor
 *
 * Pipeline agentique surveillé (5 étapes) :
 *   Step 1 — Metadata Extraction  : JS  → worksheet.getDataSourcesAsync()
 *   Step 2 — Request Dispatch     : JS  → fetch() vers FastAPI
 *   Step 3 — LLM Processing       : PY  → Ollama /api/chat (timing retourné par backend)
 *   Step 4 — Command Mapping      : PY  → validation JSON + to_tableau_mark_type()
 *   Step 5 — Viz Execution        : JS  → applyFilterAsync() + changeValueAsync()
 */

"use strict";

// ── Configuration ──────────────────────────────────────────────────────────
const CONFIG = {
  BACKEND_URL:  "http://localhost:8000",
  TARGET_SHEET: "Zone IA",
  PARAM_COLUMN: "p_VizAgent_Column",
  PARAM_ROW:    "p_VizAgent_Row",
  PARAM_COLOR:  "p_VizAgent_Color",
  MAX_HISTORY:  10,
};

// ── État ───────────────────────────────────────────────────────────────────
const state = {
  initialized: false,
  lastFields:  [],
  history:     loadHistory(),
};

// ── Références DOM ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const questionInput = $("question-input");
const submitBtn     = $("submit-btn");
const clearBtn      = $("clear-btn");
const statusBar     = $("status-bar");
const resultCard    = $("result-card");
const historyList   = $("history-list");
const debugContent  = $("debug-content");
const debugToggle   = $("debug-toggle");
const fieldsBadges  = $("fields-badges");
const pipelineEl    = $("pipeline");

// ── Pipeline Monitor ───────────────────────────────────────────────────────
const PIPELINE_STEPS = [
  { id: "metadata_extraction", label: "Metadata Extraction",  icon: "⬡" },
  { id: "request_dispatch",    label: "Request Dispatch",     icon: "⬡" },
  { id: "llm_processing",      label: "LLM Processing",       icon: "⬡" },
  { id: "command_mapping",     label: "Command Mapping",      icon: "⬡" },
  { id: "viz_execution",       label: "Viz Execution",        icon: "⬡" },
];

// Initialise les 5 steps en "pending"
function initPipeline() {
  pipelineEl.innerHTML = PIPELINE_STEPS.map((s) => `
    <div class="pipeline-step pending" id="step-${s.id}">
      <div class="step-dot"></div>
      <div class="step-body">
        <span class="step-label">${s.label}</span>
        <span class="step-badge" id="badge-${s.id}"></span>
      </div>
    </div>
    <div class="step-connector" id="conn-${s.id}"></div>
  `).join("");
}

// Met à jour le statut visuel d'un step
// status : "pending" | "in_progress" | "success" | "error"
// duration_ms : optionnel
// error : message d'erreur optionnel
function updateStep(stepId, status, { duration_ms = null, error = null } = {}) {
  const el    = $(`step-${stepId}`);
  const badge = $(`badge-${stepId}`);
  const conn  = $(`conn-${stepId}`);
  if (!el) return;

  el.className = `pipeline-step ${status}`;

  if (duration_ms !== null) {
    badge.textContent = duration_ms < 1000
      ? `${Math.round(duration_ms)}ms`
      : `${(duration_ms / 1000).toFixed(1)}s`;
    badge.className = "step-badge";
  }

  if (error) {
    const errEl = el.querySelector(".step-error") ?? document.createElement("div");
    errEl.className   = "step-error";
    errEl.textContent = error;
    el.querySelector(".step-body").appendChild(errEl);
  }

  // Colorier le connecteur selon le statut de l'étape précédente
  if (conn) {
    conn.className = `step-connector ${status === "success" ? "done" : ""}`;
  }
}

// Applique une trace complète reçue du backend (steps 3 & 4)
function applyBackendTrace(trace) {
  if (!trace?.steps) return;
  for (const step of trace.steps) {
    // Ne pas écraser les steps JS (1, 2, 5) — déjà mis à jour localement
    if (["metadata_extraction", "request_dispatch", "viz_execution"].includes(step.id)) continue;
    updateStep(step.id, step.status, {
      duration_ms: step.duration_ms,
      error:       step.error,
    });
  }
}

// Applique une trace d'erreur partielle (quand le backend retourne 4xx/5xx)
function applyErrorTrace(traceData) {
  if (!traceData?.steps) return;
  for (const step of traceData.steps) {
    if (step.status === "error" || step.status === "success") {
      updateStep(step.id, step.status, {
        duration_ms: step.duration_ms,
        error:       step.error,
      });
    }
  }
}

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
  const applied = [];
  const manual  = [];

  if (intent._filtersApplied?.length)
    applied.push(`Filtre : <strong>${intent._filtersApplied.join(", ")}</strong>`);
  if (intent._paramsSet?.length)
    applied.push(`Paramètres : <strong>${intent._paramsSet.join(", ")}</strong>`);
  if (intent.columns?.length)
    manual.push(`Colonnes → <code>${intent.columns.join(", ")}</code>`);
  if (intent.rows?.length)
    manual.push(`Lignes → <code>${intent.rows.join(", ")}</code>`);
  if (intent.viz_type)
    manual.push(`Type : <code>${intent.viz_type}</code>`);
  if (intent.color)
    manual.push(`Couleur → <code>${intent.color}</code>`);

  let html = "";
  if (applied.length)
    html += `<div class="rc-section rc-applied">
      <span class="rc-label">Appliqué automatiquement</span>
      <ul>${applied.map((a) => `<li>${a}</li>`).join("")}</ul>
    </div>`;
  if (manual.length)
    html += `<div class="rc-section rc-manual">
      <span class="rc-label">À appliquer sur "Zone IA"</span>
      <ul>${manual.map((m) => `<li>${m}</li>`).join("")}</ul>
    </div>`;

  resultCard.innerHTML = html;
  resultCard.classList.remove("hidden");
}

function renderFieldBadges(fields) {
  const badge = (f, cls) =>
    `<span class="badge badge-${cls}" title="${f.type}">${f.name}</span>`;
  fieldsBadges.innerHTML =
    fields.filter((f) => f.role === "dimension").map((f) => badge(f, "dim")).join("") +
    fields.filter((f) => f.role === "measure").map((f) => badge(f, "mea")).join("");
}

function renderHistory() {
  if (!state.history.length) {
    historyList.innerHTML = '<li class="history-empty">Aucune question posée</li>';
    return;
  }
  historyList.innerHTML = state.history
    .map((q, i) => `<li class="history-item" data-index="${i}" title="${q}">${q}</li>`)
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

// ── Historique ─────────────────────────────────────────────────────────────
function loadHistory() {
  try { return JSON.parse(localStorage.getItem("vizagent_history") ?? "[]"); }
  catch { return []; }
}

function saveHistory(question) {
  state.history = [question, ...state.history.filter((q) => q !== question)].slice(0, CONFIG.MAX_HISTORY);
  try { localStorage.setItem("vizagent_history", JSON.stringify(state.history)); }
  catch { /* silencieux */ }
  renderHistory();
}

// ── Initialisation Tableau Extensions API ──────────────────────────────────
tableau.extensions.initializeAsync().then(async () => {
  initPipeline();
  setStatus("Connexion au backend...", "loading");

  if (!await checkBackend()) return;

  state.initialized = true;
  setStatus("Prêt.", "idle");
  renderHistory();

  try {
    state.lastFields = await extractFields();
    renderFieldBadges(state.lastFields);
  } catch { /* non bloquant */ }

  submitBtn.addEventListener("click", handleSubmit);
  clearBtn.addEventListener("click", handleClear);
  questionInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
  });
}).catch((err) => {
  setStatus(`Erreur d'initialisation Tableau : ${err.message}`, "error");
});

// ── Santé backend ──────────────────────────────────────────────────────────
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
    setStatus("Backend inaccessible. Lancer : uvicorn main:app --port 8000", "error");
    return false;
  }
}

// ── Step 1 : Extraction des métadonnées ────────────────────────────────────
async function extractFields() {
  const sheet = getTargetSheet();
  const dataSources = await sheet.getDataSourcesAsync();
  if (!dataSources.length) throw new Error("Aucune source de données sur la feuille « Zone IA ».");
  const ds = dataSources[0];
  return ds.fields
    .filter((f) => !f.isHidden)
    .map((f) => ({
      name: f.name,
      type: f.dataType?.toLowerCase() ?? "string",
      role: f.role?.toLowerCase() ?? "dimension",
    }));
}

function getTargetSheet() {
  const sheet = tableau.extensions.dashboardContent.dashboard.worksheets
    .find((ws) => ws.name === CONFIG.TARGET_SHEET);
  if (!sheet) throw new Error(
    `Feuille "${CONFIG.TARGET_SHEET}" introuvable dans ce dashboard.`
  );
  return sheet;
}

// ── Step 2 : Appel backend FastAPI ─────────────────────────────────────────
async function callBackend(question, fields) {
  const response = await fetch(`${CONFIG.BACKEND_URL}/generate-viz`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ question, fields, sheet_name: CONFIG.TARGET_SHEET }),
  });

  const body = await response.json().catch(() => ({}));

  if (!response.ok) {
    // Récupérer la trace partielle si disponible pour mise à jour du pipeline
    if (body.detail?.trace) applyErrorTrace(body.detail.trace);
    throw new Error(body.detail?.error ?? `Erreur backend HTTP ${response.status}`);
  }

  return body;
}

// ── Step 5 : Application de l'intention sur Tableau ────────────────────────
async function applyIntent(intent) {
  const sheet = getTargetSheet();
  intent._filtersApplied = [];
  intent._paramsSet      = [];

  await clearAgentFilters(sheet);

  if (intent.filter?.field && intent.filter?.values?.length) {
    await sheet.applyFilterAsync(
      intent.filter.field,
      intent.filter.values,
      tableau.FilterUpdateType.Replace
    );
    intent._filtersApplied.push(`${intent.filter.field} = ${intent.filter.values.join(", ")}`);
  }

  try {
    const parameters = await tableau.extensions.dashboardContent.dashboard.getParametersAsync();
    const paramIndex = Object.fromEntries(parameters.map((p) => [p.name, p]));
    const paramMap   = {
      [CONFIG.PARAM_COLUMN]: intent.columns?.[0] ?? null,
      [CONFIG.PARAM_ROW]:    intent.rows?.[0]    ?? null,
      [CONFIG.PARAM_COLOR]:  intent.color        ?? null,
    };
    for (const [name, value] of Object.entries(paramMap)) {
      if (value && paramIndex[name]) {
        await paramIndex[name].changeValueAsync(value);
        intent._paramsSet.push(`${name} = "${value}"`);
      }
    }
  } catch { /* paramètres optionnels */ }

  return intent;
}

const _lastFilteredFields = new Set();
async function clearAgentFilters(sheet) {
  for (const field of _lastFilteredFields) {
    try { await sheet.clearFilterAsync(field); } catch { /* silencieux */ }
  }
  _lastFilteredFields.clear();
}

// ── Handler principal ──────────────────────────────────────────────────────
async function handleSubmit() {
  const question = questionInput.value.trim();
  if (!question) { setStatus("Saisissez une question.", "error"); return; }

  setLoading(true);
  resultCard.classList.add("hidden");
  initPipeline();   // remet tous les steps à "pending"

  let t_start, t_end;

  try {
    // ── Step 1 : Metadata Extraction ──────────────────────────────────────
    updateStep("metadata_extraction", "in_progress");
    setStatus("Extraction des métadonnées...", "loading");
    t_start = performance.now();
    state.lastFields = await extractFields();
    t_end = performance.now();
    renderFieldBadges(state.lastFields);
    updateStep("metadata_extraction", "success", { duration_ms: t_end - t_start });

    // ── Step 2 : Request Dispatch ─────────────────────────────────────────
    updateStep("request_dispatch", "in_progress");
    setStatus("Envoi au backend...", "loading");
    t_start = performance.now();

    // ── Steps 3 & 4 : LLM + Mapping (backend) ────────────────────────────
    // On marque step 3 "in_progress" dès l'envoi — le backend mettra à jour via la trace
    updateStep("llm_processing", "in_progress");
    setStatus("Consultation du modèle IA...", "loading");

    const intent = await callBackend(question, state.lastFields);
    t_end = performance.now();

    updateStep("request_dispatch", "success", { duration_ms: t_end - t_start });
    showDebug(intent);

    // Appliquer la trace backend (steps 3 & 4 avec vrais timings)
    applyBackendTrace(intent.execution_trace);

    // ── Step 5 : Viz Execution ────────────────────────────────────────────
    updateStep("viz_execution", "in_progress");
    setStatus("Application sur Tableau...", "loading");
    t_start = performance.now();
    const enrichedIntent = await applyIntent(intent);
    t_end = performance.now();
    updateStep("viz_execution", "success", { duration_ms: t_end - t_start });

    if (intent.filter?.field) _lastFilteredFields.add(intent.filter.field);

    showResultCard(enrichedIntent);
    saveHistory(question);
    setStatus(`"${intent.viz_type}" — pipeline complété.`, "success");

  } catch (err) {
    // L'étape en cours passe en error — les suivantes restent pending
    const activeStep = pipelineEl.querySelector(".pipeline-step.in_progress");
    if (activeStep) {
      const stepId = activeStep.id.replace("step-", "");
      updateStep(stepId, "error", { error: err.message });
    }
    setStatus(`Erreur : ${err.message}`, "error");
    console.error("[VizAgent]", err);
  } finally {
    setLoading(false);
  }
}

// ── Handler : Reset ────────────────────────────────────────────────────────
async function handleClear() {
  questionInput.value      = "";
  resultCard.classList.add("hidden");
  debugContent.textContent = "";
  fieldsBadges.innerHTML   = "";
  initPipeline();
  setStatus("Réinitialisé.", "idle");
  try {
    const sheet = getTargetSheet();
    await clearAgentFilters(sheet);
  } catch { /* non bloquant */ }
}

/**
 * vega_mapper.js — Convertit un VizIntentResponse en spec Vega-Lite v5
 *
 * Mapping viz_type → mark Vega-Lite :
 *   bar       → "bar"
 *   line      → "line"
 *   area      → "area"
 *   scatter   → "point"
 *   pie       → "arc"  (theta + color encoding)
 *   text      → "text" (tableau croisé)
 *   histogram → "bar"  (bin sur l'axe X)
 *   treemap   → "rect" (approximé)
 *   bubble    → "circle" (size encoding)
 */

"use strict";

// ── Convertit une DataTable Tableau en tableau d'objets JS ─────────────────
// DataTable.columns = [{ fieldName, dataType }, ...]
// DataTable.data    = [[DataValue, ...], ...]
// DataValue.value   = valeur native (string | number | Date | null)
function tableauDataToObjects(dataTable) {
  const colNames = dataTable.columns.map((c) => c.fieldName);
  return dataTable.data.map((row) =>
    Object.fromEntries(row.map((cell, i) => [colNames[i], cell.value]))
  );
}

// ── Deviner le type Vega-Lite d'un champ à partir des données ─────────────
function guessVegaType(fieldName, sample) {
  if (!sample || sample.length === 0) return "nominal";
  const val = sample[fieldName];
  if (val instanceof Date)             return "temporal";
  if (typeof val === "number")         return "quantitative";
  // Chaînes ressemblant à des dates
  if (typeof val === "string" && /^\d{4}-\d{2}/.test(val)) return "temporal";
  return "nominal";
}

// ── Mapping principal : intent → spec Vega-Lite ────────────────────────────
function intentToVegaSpec(intent, rawData, containerWidth) {
  const { viz_type, columns, rows, color, size, filter, title } = intent;
  const sample = rawData[0] ?? {};
  const w = containerWidth ?? 400;
  const h = Math.round(w * 0.55);

  // ── Base spec ─────────────────────────────────────────────────────────────
  const spec = {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    title:   title ?? undefined,
    width:   "container",
    height:  h,
    data:    { values: rawData },
    config: {
      font:   "Segoe UI, sans-serif",
      axis:   { labelFontSize: 11, titleFontSize: 12 },
      legend: { labelFontSize: 11 },
      title:  { fontSize: 13, fontWeight: "bold" },
      view:   { stroke: null },
    },
    encoding: {},
  };

  const colField  = columns?.[0] ?? null;
  const rowField  = rows?.[0]    ?? null;
  const colType   = colField ? guessVegaType(colField, sample) : "nominal";
  const rowType   = rowField ? guessVegaType(rowField, sample) : "quantitative";

  // ── Encodages selon le type de viz ────────────────────────────────────────
  switch (viz_type) {

    case "pie": {
      spec.mark = { type: "arc", innerRadius: 0 };
      spec.encoding = {
        theta: { field: rowField,  type: "quantitative", aggregate: "sum" },
        color: { field: colField,  type: "nominal" },
      };
      spec.width  = Math.min(w, 300);
      spec.height = Math.min(h, 300);
      break;
    }

    case "scatter": {
      spec.mark = { type: "point", opacity: 0.7, filled: true };
      spec.encoding = {
        x:     { field: colField,  type: "quantitative" },
        y:     { field: rowField,  type: "quantitative" },
      };
      if (color) spec.encoding.color = { field: color, type: guessVegaType(color, sample) };
      if (size)  spec.encoding.size  = { field: size,  type: "quantitative" };
      break;
    }

    case "bubble": {
      spec.mark = { type: "circle", opacity: 0.7 };
      spec.encoding = {
        x:    { field: colField,  type: colType },
        y:    { field: rowField,  type: "quantitative", aggregate: "sum" },
        size: { field: size ?? rowField, type: "quantitative", aggregate: "sum" },
      };
      if (color) spec.encoding.color = { field: color, type: "nominal" };
      break;
    }

    case "histogram": {
      spec.mark = "bar";
      spec.encoding = {
        x: { field: colField ?? rowField, type: "quantitative", bin: true },
        y: { aggregate: "count", type: "quantitative" },
      };
      break;
    }

    case "text": {
      // Tableau croisé simplifié
      spec.mark = { type: "text", fontSize: 11 };
      spec.encoding = {
        row:  { field: colField, type: colType },
        text: { field: rowField, type: "quantitative", aggregate: "sum", format: ",.0f" },
      };
      spec.height = "container";
      break;
    }

    case "line": {
      spec.mark = { type: "line", point: true };
      spec.encoding = {
        x: { field: colField, type: colType, sort: null },
        y: { field: rowField, type: "quantitative", aggregate: "sum" },
      };
      if (color) spec.encoding.color = { field: color, type: "nominal" };
      break;
    }

    case "area": {
      spec.mark = { type: "area", opacity: 0.7 };
      spec.encoding = {
        x: { field: colField, type: colType, sort: null },
        y: { field: rowField, type: "quantitative", aggregate: "sum" },
      };
      if (color) {
        spec.encoding.color = { field: color, type: "nominal" };
        spec.mark = { type: "area", opacity: 0.6 };
      }
      break;
    }

    case "treemap": {
      // Vega-Lite ne supporte pas nativement le treemap — rendu comme bar horizontal trié
      spec.mark = "bar";
      spec.encoding = {
        x: { field: rowField, type: "quantitative", aggregate: "sum" },
        y: { field: colField, type: "nominal",      sort: "-x" },
      };
      if (color) spec.encoding.color = { field: color, type: "nominal" };
      break;
    }

    // bar (default) + gantt
    default: {
      const isHorizontal = colType === "quantitative";
      spec.mark = { type: "bar", cornerRadiusTopLeft: 3, cornerRadiusTopRight: 3 };
      if (isHorizontal) {
        spec.encoding = {
          y: { field: colField, type: colType,        sort: "-x" },
          x: { field: rowField, type: "quantitative", aggregate: "sum" },
        };
      } else {
        spec.encoding = {
          x: { field: colField, type: colType },
          y: { field: rowField, type: "quantitative", aggregate: "sum" },
        };
      }
      if (color) spec.encoding.color = { field: color, type: "nominal" };
      break;
    }
  }

  // ── Tri pour les bar charts ordinaux ─────────────────────────────────────
  if (["bar", "treemap"].includes(viz_type) && spec.encoding.x?.type === "quantitative") {
    // déjà trié via sort: "-x"
  } else if (viz_type === "bar" && spec.encoding.y?.type === "quantitative") {
    spec.encoding.x.sort = "-y";
  }

  // ── Tooltip automatique sur tous les champs encodés ───────────────────────
  spec.encoding.tooltip = Object.entries(spec.encoding)
    .filter(([k]) => !["tooltip", "text"].includes(k))
    .map(([, enc]) => enc.field
      ? { field: enc.field, type: enc.type, aggregate: enc.aggregate }
      : null
    )
    .filter(Boolean);

  return spec;
}

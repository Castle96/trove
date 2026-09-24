/**
 * models.js — Models tab (LLM nodes: Ollama / llama.cpp fleet).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { state, $, esc, fmtBytes, api, registerTabHandler } from "./core.js";

export async function loadModels() {
  const cards = $("#models-cards");
  const table = $("#models-table");
  table.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const fleet = await api("/api/models/fleet");
    state.models = fleet;
    cards.innerHTML = `
      <div class="card"><div class="num">${fleet.total_nodes}</div><div class="label">Model nodes</div></div>
      <div class="card"><div class="num">${fleet.reachable}<span class="muted small">/${fleet.total_nodes}</span></div><div class="label">Reachable</div></div>
      <div class="card"><div class="num">${fleet.total_models}</div><div class="label">Models loaded</div></div>`;
    const nodes = fleet.nodes || [];
    if (!nodes.length) {
      table.innerHTML = `<div class="muted">No model nodes configured.</div>`;
      return;
    }
    table.innerHTML = nodes
      .map((n) => {
        const engine = n.engine === "ollama" ? "Ollama" : "llama.cpp";
        const status = n.available
          ? `<span class="badge badge-active">ok</span>`
          : `<span class="badge badge-failed" title="${esc(n.reason || "")}">down</span>`;
        const version = n.version ? `<span class="mono small">v${esc(n.version)}</span>` : "";
        const rows = (n.models || []).length
          ? n.models
              .map(
                (m) => `<tr>
                  <td class="mono">${esc(m.name)}</td>
                  <td>${fmtBytes(m.size)}</td>
                  <td class="muted">${esc(m.family || "—")}</td>
                </tr>`
              )
              .join("")
          : `<tr><td colspan="3" class="muted">No models loaded</td></tr>`;
        return `
          <div class="stack-card">
            <h3>${esc(n.name)} ${status}</h3>
            <div class="stack-sub">${engine} ${version} &middot; <span class="mono small">${esc(n.url || "")}</span></div>
            <table>
              <thead><tr><th>Model</th><th>Size</th><th>Family</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>`;
      })
      .join("");
  } catch (err) {
    table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

registerTabHandler("models", loadModels);

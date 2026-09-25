/**
 * pipeline.js — Fleet tab dev-pipeline panels. One card per registered code
 * agent (ray / fleet / jarvis): repo/commit/branch header, a voice-style
 * stage-flow (checkout→deps→format→lint→build→test→report) with per-stage
 * status + latency, and run history.
 */
import { $, esc, api, registerTabHandler } from "./core.js";

const STAGES = ["checkout", "deps", "format", "lint", "build", "test", "report"];
const LANG_BADGE = {
  rust: { label: "Rust", cls: "badge-planned", icon: "fa-brands fa-rust" },
  go: { label: "Go", cls: "badge-active", icon: "fa-brands fa-golang" },
  python: { label: "Python", cls: "badge-created", icon: "fa-brands fa-python" },
};

function langBadge(language) {
  const b = LANG_BADGE[language] || { label: language || "code", cls: "badge", icon: "fa-solid fa-microchip" };
  return `<span class="badge ${b.cls}"><i class="${b.icon} mr-1"></i>${esc(b.label)}</span>`;
}

function shortCommit(commit) {
  return commit ? `<span class="mono small" title="${esc(commit)}">@${esc(commit.slice(0, 10))}</span>` : "";
}

function stageFlow(stages) {
  const map = Object.fromEntries((stages || []).map((s) => [s.stage, s]));
  return STAGES.map((stage, i) => {
    const s = map[stage];
    const status = s ? s.status : "idle";
    const cls = status === "error" ? "vf-box error" : status === "ok" ? "vf-box ok" : status === "skipped" ? "vf-box idle" : "vf-box idle";
    const lat = s && s.latency_ms != null ? `${Math.round(s.latency_ms)}ms` : "—";
    const title = s && s.detail ? `title="${esc(s.detail.slice(0, 300))}"` : "";
    return `
      <div class="vf-node">
        ${i > 0 ? `<div class="vf-arrow">→</div>` : ""}
        <div class="${cls}" ${title}><div class="vf-name">${esc(stage)}</div><div class="vf-lat muted">${lat}</div></div>
      </div>`;
  }).join("");
}

export async function loadPipelinesTab() {
  const el = $("#pipeline-panels");
  if (el) el.innerHTML = `<div class="muted">Loading…</div>`;
  await renderPipelineCards();
}

registerTabHandler("pipelines", loadPipelinesTab);

export async function renderPipelineCards() {
  const el = $("#pipeline-panels");
  if (!el) return;
  let entries;
  try {
    entries = await api("/api/pipeline/live");
  } catch (err) {
    el.innerHTML = `<div class="muted">Dev pipelines unavailable: ${esc(err.message)}</div>`;
    return;
  }
  if (!entries.length) {
    el.innerHTML = `<div class="muted">No code agents registered yet. Configure DOCKWATCH_CODE_AGENTS (ray/fleet/jarvis) — projects enrolled in the Projects tab then run their pipeline here.</div>`;
    return;
  }
  el.innerHTML = entries.map((a) => {
    const runBadge =
      a.last_run_status === "error"
        ? `<span class="badge badge-failed">error</span>`
        : a.last_run_status === "ok"
          ? `<span class="badge badge-active">ok</span>`
          : a.last_run_status === "running"
            ? `<span class="badge badge-running">running</span>`
            : `<span class="badge badge-created">no runs yet</span>`;
    const repo = a.repo_url
      ? `<a href="${esc(a.repo_url)}" target="_blank" rel="noopener" class="mono small muted">${esc(a.repo_url)}</a>`
      : `<span class="small muted">no repo enrolled — assign a project on the Projects tab</span>`;
    return `<div class="panel">
      <div class="panel-head">
        <h2><i class="fa-solid fa-robot text-indigo-400 mr-2"></i>${esc(a.agent_name)}</h2>
        <div class="flex items-center gap-2">
          ${langBadge(a.language)}
          ${a.model ? `<span class="mono small muted">${esc(a.model)}</span>` : ""}
          ${a.endpoint_name ? `<span class="badge" title="host endpoint">${esc(a.endpoint_name)}</span>` : ""}
          ${a.status === "working" ? `<span class="badge badge-running">working</span>` : ""}
          ${runBadge}
        </div>
      </div>
      <div class="small muted mb-2">${repo} ${shortCommit(a.last_commit)} ${a.branch ? `<span class="mono small">(${esc(a.branch)})</span>` : ""}</div>
      <div id="pipeline-flow-${esc(a.agent_id)}" class="flex">${stageFlow(a.stages)}</div>
      ${a.total_latency_ms ? `<div class="small muted mt-2">last run ${Math.round(a.total_latency_ms)}ms</div>` : ""}
    </div>`;
  }).join("");
}

/**
 * voice.js — Voice (Jarvis) tab: pipeline flow, COS/latency chart, recent turns.
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { state, $, esc, fmtDate, api, registerTabHandler } from "./core.js";
import { LineChart } from "./charts.js";
import { monitorLegend } from "./monitor.js";

const VOICE_STAGES = ["vad", "stt", "nlu", "agent", "skills", "tts"];
const VOICE_COLORS = {
  vad: "#8b5cf6",
  stt: "#0ea5e9",
  nlu: "#10b981",
  agent: "#f59e0b",
  skills: "#ef4444",
  tts: "#ec4899",
};
let voiceChart = null;

function voiceStatusBadge(status) {
  if (status === "error") return `<span class="badge badge-failed">error</span>`;
  if (status === "ok") return `<span class="badge badge-active">ok</span>`;
  if (status === "active") return `<span class="badge badge-running">active</span>`;
  return `<span class="badge badge-created">idle</span>`;
}

function buildVoiceSeries() {
  // One p50 line + one p95 area per stage (12 series total).
  const series = [];
  for (const st of VOICE_STAGES) {
    series.push({ color: VOICE_COLORS[st], fixedMax: 5000, label: `${st} p50` });
    series.push({ color: VOICE_COLORS[st], fixedMax: 5000, dash: true, alpha: 0.35, area: true, label: `${st} p95` });
  }
  return series;
}

export async function loadVoiceTab() {
  try {
    await Promise.all([
      loadVoiceOverview(),
      loadVoiceLive(),
      loadVoiceTurns(),
      loadVoiceHistory(),
    ]);
  } catch (err) {
    const flow = $("#voice-flow");
    if (flow) flow.innerHTML = `<div class="muted">Voice unavailable: ${esc(err.message)}</div>`;
  }
}

async function loadVoiceOverview() {
  const cards = $("#voice-cards");
  if (!cards) return;
  cards.innerHTML = `<div class="muted">Loading…</div>`;
  const data = await api("/api/voice/overview");
  state.voice = data;
  if (!data.agent) {
    cards.innerHTML = `<div class="muted">Voice is disabled — set DOCKWATCH_ENABLE_VOICE=true and restart.</div>`;
    return;
  }
  cards.innerHTML = `
    <div class="card"><div class="num">${esc(data.agent.status)}</div><div class="label">Jarvis</div></div>
    <div class="card"><div class="num">${data.turns_24h}</div><div class="label">Turns (24h)</div></div>
    <div class="card"><div class="num">${Math.round(data.avg_latency_ms)}<span class="muted small"> ms</span></div><div class="label">Avg turn</div></div>
    <div class="card"><div class="num">${data.errors_24h}</div><div class="label">Errors (24h)</div></div>`;
}

async function loadVoiceLive() {
  const flow = $("#voice-flow");
  const meta = $("#voice-meta");
  if (!flow) return;
  const data = await api("/api/voice/live");
  const stageMap = Object.fromEntries((data.stages || []).map((s) => [s.stage, s]));
  flow.innerHTML = VOICE_STAGES.map((stage, i) => {
    const s = stageMap[stage] || { status: "idle", latency_ms: null };
    const cls = s.status === "error" ? "vf-box error" : s.status === "ok" ? "vf-box ok" : "vf-box idle";
    const lat = s.latency_ms != null ? `${Math.round(s.latency_ms)}ms` : "—";
    return `
      <div class="vf-node">
        ${i > 0 ? `<div class="vf-arrow">→</div>` : ""}
        <div class="${cls}"><div class="vf-name">${esc(stage)}</div><div class="vf-lat muted">${lat}</div></div>
      </div>`;
  }).join("");
  const lt = data.last_turn;
  meta.innerHTML = lt
    ? `${esc(data.agent_status)} · last turn ${fmtDate(new Date(lt.ts).toISOString())} ${voiceStatusBadge(lt.status)}`
    : data.demo_enabled
      ? "demo loop enabled · waiting for first turn"
      : "idle · no turns yet";
}

async function loadVoiceTurns() {
  const el = $("#voice-turns");
  if (!el) return;
  const data = await api("/api/voice/turns?limit=20");
  if (!data.length) {
    el.innerHTML = `<div class="muted">No turns yet — feed the pipeline via POST /api/voice/ingest.</div>`;
    return;
  }
  el.innerHTML = `<table>
    <thead><tr><th>Time</th><th>Transcript</th><th>Response</th><th>Status</th><th>Latency</th></tr></thead>
    <tbody>${data
      .map(
        (t) => `<tr>
          <td class="mono muted">${fmtDate(new Date(t.ts).toISOString())}</td>
          <td>${esc(t.transcript || "—")}</td>
          <td class="muted">${esc(t.response_text || "—")}</td>
          <td>${voiceStatusBadge(t.status)}</td>
          <td class="mono">${Math.round(t.total_latency_ms)}ms</td>
        </tr>`
      )
      .join("")}</tbody></table>`;
}

async function loadVoiceHistory() {
  const canvas = $("#chart-voice");
  if (!canvas) return;
  const data = await api("/api/voice/history?range=3600&bucket=60");
  if (!voiceChart) {
    voiceChart = new LineChart(canvas, {
      series: buildVoiceSeries(),
      maxPoints: 60,
    });
  }
  const buckets = data.stages[VOICE_STAGES[0]] || [];
  const times = buckets.map((b) => b.ts * 1000);
  const arrays = [];
  const markers = [];
  for (const st of VOICE_STAGES) {
    const b = data.stages[st] || [];
    arrays.push(b.map((v) => v.p50));
    markers.push(b.map((v) => v.errors > 0));
    arrays.push(b.map((v) => v.p95));
    markers.push(b.map(() => false));
  }
  voiceChart.setData(arrays, markers, times);
  monitorLegend(
    "legend-voice",
    VOICE_STAGES.flatMap((st) => [
      { color: VOICE_COLORS[st], label: `${st} p50`, value: "" },
      { color: VOICE_COLORS[st], label: `${st} p95`, value: "" },
    ])
  );
}

registerTabHandler("voice", loadVoiceTab);

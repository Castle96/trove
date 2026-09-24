/**
 * monitor.js — Host monitoring (Glances-style): gauges, canvas charts, top
 * processes, filesystems, anomaly detection.
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { $, esc, fmtBytes, api } from "./core.js";
import { LineChart } from "./charts.js";

const MONITOR_COLORS = {
  cpu: "#3B82F6",
  mem: "#22C55E",
  swap: "#F59E0B",
  rx: "#3B82F6",
  tx: "#22C55E",
  read: "#3B82F6",
  write: "#F59E0B",
};

/** Range options for the monitoring panel. Live uses the 2s rolling series;
 *  history modes poll the downsampled /api/monitor/history endpoint. */
const HISTORY_RANGES = {
  live: { range: 0, bucket: 0, label: "Live 3m" },
  "1h": { range: 3600, bucket: 15, label: "1h" },
  "6h": { range: 21600, bucket: 120, label: "6h" },
  "24h": { range: 86400, bucket: 300, label: "24h" },
  "7d": { range: 604800, bucket: 1800, label: "7d" },
};

let monitorCharts = null;
let monitorAvailable = false;
let monitorSeeded = false;
let monitorRangeKey = "live";
let monitorHistoryTimer = null;

function fmtRate(bps) {
  return `${fmtBytes(bps)}/s`;
}

function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function monitorLegend(elId, entries) {
  const el = $(`#${elId}`);
  if (!el) return;
  el.innerHTML = entries
    .map((e) => `<span style="color:${e.color}">&#9679; ${e.label} <strong>${e.value}</strong></span>`)
    .join(" &nbsp; ");
}

export async function initMonitor() {
  const disabled = $("#monitor-disabled");
  try {
    const st = await api("/api/monitor/status");
    monitorAvailable = Boolean(st.available);
  } catch {
    monitorAvailable = false;
  }

  if (!monitorAvailable) {
    if (disabled) {
      disabled.textContent = "Host metrics unavailable on this system.";
      disabled.classList.remove("hidden");
    }
    const gauges = $("#monitor-gauges");
    if (gauges) gauges.innerHTML = "";
    const grid = $(".charts-grid");
    if (grid) grid.style.display = "none";
    const cores = $("#core-bars");
    if (cores) cores.innerHTML = "";
    const procs = $("#top-processes");
    if (procs) procs.innerHTML = "";
    return;
  }

  monitorCharts = {
    cpu: new LineChart($("#chart-cpu"), {
      series: [{ color: MONITOR_COLORS.cpu, label: "CPU", fixedMax: 100 }],
      maxPoints: 90,
    }),
    mem: new LineChart($("#chart-mem"), {
      series: [
        { color: MONITOR_COLORS.mem, label: "Mem", fixedMax: 100 },
        { color: MONITOR_COLORS.swap, label: "Swap", fixedMax: 100 },
      ],
      maxPoints: 90,
    }),
    net: new LineChart($("#chart-net"), {
      series: [
        { color: MONITOR_COLORS.rx, label: "RX" },
        { color: MONITOR_COLORS.tx, label: "TX" },
      ],
      maxPoints: 90,
    }),
    disk: new LineChart($("#chart-disk"), {
      series: [
        { color: MONITOR_COLORS.read, label: "Read" },
        { color: MONITOR_COLORS.write, label: "Write" },
      ],
      maxPoints: 90,
    }),
  };

  await pollMonitorSeries(); // seed from stored history, then let the interval take over
  setInterval(pollMonitorSeries, 2000);
  await pollMonitorSnapshot();
  setInterval(pollMonitorSnapshot, 6000);

  const rangeSel = $("#monitor-range");
  if (rangeSel) {
    rangeSel.value = monitorRangeKey;
    rangeSel.addEventListener("change", () => setMonitorRange(rangeSel.value));
  }
}

/** Switch the monitoring panel between live rolling data and history ranges. */
function setMonitorRange(key) {
  if (!(key in HISTORY_RANGES)) return;
  monitorRangeKey = key;
  const sel = $("#monitor-range");
  if (sel) sel.value = key;
  if (monitorHistoryTimer) {
    clearInterval(monitorHistoryTimer);
    monitorHistoryTimer = null;
  }
  const meta = $("#monitor-meta");
  if (key === "live") {
    if (meta) meta.textContent = "sampling every 2s";
    const density = $("#monitor-density");
    if (density) {
      density.classList.add("hidden");
      density.innerHTML = "";
    }
    if (monitorCharts) {
      Object.values(monitorCharts).forEach((c) => c.setMaxPoints(90));
      monitorSeeded = false; // re-seed the rolling buffers from series
    }
    pollMonitorSeries();
  } else {
    if (meta) meta.textContent = "refreshing every 15s";
    pollMonitorHistory();
    pollMonitorDensity();
    monitorHistoryTimer = setInterval(() => {
      pollMonitorHistory();
      pollMonitorDensity();
    }, 15000);
  }
}

/** Downsampled history view: avg values per bucket with a time axis. */
async function pollMonitorHistory() {
  if (!monitorAvailable || !monitorCharts) return;
  const def = HISTORY_RANGES[monitorRangeKey];
  if (!def) return;
  try {
    const data = await api(`/api/monitor/history?range=${def.range}&bucket=${def.bucket}`);
    const series = data.series || [];
    if (!series.length) return;
    const times = series.map((b) => b.ts * 1000);
    const flags = series.map(() => false);
    monitorCharts.cpu.setMaxPoints(series.length);
    monitorCharts.cpu.setData([series.map((b) => b.cpu.avg)], [flags], times);
    monitorCharts.mem.setMaxPoints(series.length);
    monitorCharts.mem.setData(
      [series.map((b) => b.mem.avg), series.map((b) => b.swap.avg)],
      [[], []],
      times
    );
    monitorCharts.net.setMaxPoints(series.length);
    monitorCharts.net.setData(
      [series.map((b) => b.net.rx_avg), series.map((b) => b.net.tx_avg)],
      [[], []],
      times
    );
    monitorCharts.disk.setMaxPoints(series.length);
    monitorCharts.disk.setData(
      [series.map((b) => b.disk.read_avg), series.map((b) => b.disk.write_avg)],
      [[], []],
      times
    );
  } catch {
    /* transient outage — keep old data on screen */
  }
}

/** Anomaly density strip (history modes only): flagged samples per bucket. */
async function pollMonitorDensity() {
  const def = HISTORY_RANGES[monitorRangeKey];
  if (!def) return;
  const el = $("#monitor-density");
  if (!el) return;
  try {
    const data = await api(`/api/monitor/anomaly-density?range=${def.range}&bucket=${def.bucket}`);
    renderDensity(el, data.density || []);
  } catch {
    /* transient */
  }
}

function renderDensity(el, items) {
  if (!items || !items.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const max = Math.max(...items.map((b) => b.total), 1);
  const flagged = items.reduce((a, b) => a + b.total, 0);
  const bucketsFlagged = items.filter((b) => b.total > 0).length;
  const bars = items
    .map((b) => {
      const h = Math.max(2, Math.round((b.total / max) * 34));
      const bits = [];
      if (b.cpu) bits.push(`CPU ${b.cpu}`);
      if (b.mem) bits.push(`Mem ${b.mem}`);
      const title = `${new Date(b.ts * 1000).toLocaleString()} — ${b.total} anomalous sample(s)${bits.length ? ` (${bits.join(", ")})` : ""}`;
      return `<div class="density-tick" style="height:${h}px" title="${esc(title)}"></div>`;
    })
    .join("");
  el.innerHTML = `
    <div class="density-head">
      <span class="chart-title">Anomaly density</span>
      <span class="small muted">${bucketsFlagged} of ${items.length} buckets flagged · ${flagged} flagged samples</span>
    </div>
    <div class="density-bars">${bars}</div>`;
  el.classList.remove("hidden");
}

async function pollMonitorSeries() {
  if (!monitorAvailable || !monitorCharts) return;
  if (monitorRangeKey !== "live") return; // history mode drives the charts
  try {
    const data = await api("/api/monitor/series?limit=90");
    const samples = data.samples || [];
    if (!samples.length) return;
    const last = samples[samples.length - 1];

    if (!monitorSeeded) {
      const times = samples.map((s) => s.t);
      const cpuFlags = samples.map((s) => Boolean(s.cpu_anomaly));
      monitorCharts.cpu.setData([samples.map((s) => s.cpu)], [cpuFlags], times);
      monitorCharts.mem.setData(
        [samples.map((s) => s.memory.percent), samples.map((s) => s.swap.percent)],
        [samples.map((s) => Boolean(s.mem_anomaly)), []],
        times
      );
      monitorCharts.net.setData([samples.map((s) => s.net.rx), samples.map((s) => s.net.tx)], [[], []], times);
      monitorCharts.disk.setData([samples.map((s) => s.disk.read), samples.map((s) => s.disk.write)], [[], []], times);
      monitorSeeded = true;
    } else {
      monitorCharts.cpu.push([last.cpu], [Boolean(last.cpu_anomaly)], [last.t]);
      monitorCharts.mem.push([last.memory.percent, last.swap.percent], [Boolean(last.mem_anomaly), false], [last.t]);
      monitorCharts.net.push([last.net.rx, last.net.tx], [false, false], [last.t]);
      monitorCharts.disk.push([last.disk.read, last.disk.write], [false, false], [last.t]);
    }

    updateGauges(last);
    updateCoreBars(last);
    monitorLegend("legend-cpu", [{ color: MONITOR_COLORS.cpu, label: "CPU", value: `${last.cpu}%` }]);
    monitorLegend("legend-mem", [
      { color: MONITOR_COLORS.mem, label: "Mem", value: `${last.memory.percent}%` },
      { color: MONITOR_COLORS.swap, label: "Swap", value: `${last.swap.percent}%` },
    ]);
    monitorLegend("legend-net", [
      { color: MONITOR_COLORS.rx, label: "RX", value: fmtRate(last.net.rx) },
      { color: MONITOR_COLORS.tx, label: "TX", value: fmtRate(last.net.tx) },
    ]);
    monitorLegend("legend-disk", [
      { color: MONITOR_COLORS.read, label: "Read", value: fmtRate(last.disk.read) },
      { color: MONITOR_COLORS.write, label: "Write", value: fmtRate(last.disk.write) },
    ]);
  } catch {
    /* transient outage — keep old data on screen */
  }
}

function updateGauges(last) {
  const cores = (last.cores || []).length || "?";
  const cpuWarn = last.cpu_anomaly ? " anomaly" : "";
  const memWarn = last.mem_anomaly ? " anomaly" : "";
  $("#monitor-gauges").innerHTML = `
    <div class="gauge${cpuWarn}"><div class="g-label">CPU${last.cpu_anomaly ? ' <span class="anomaly-chip">&#9888; spike</span>' : ""}</div><div class="g-value">${last.cpu}%</div><div class="g-sub">${cores} cores</div></div>
    <div class="gauge${memWarn}"><div class="g-label">Memory${last.mem_anomaly ? ' <span class="anomaly-chip">&#9888; spike</span>' : ""}</div><div class="g-value">${last.memory.percent}%</div><div class="g-sub">${fmtBytes(last.memory.used)} / ${fmtBytes(last.memory.total)}</div></div>
    <div class="gauge"><div class="g-label">Swap</div><div class="g-value">${last.swap.percent}%</div><div class="g-sub">${fmtBytes(last.swap.used)} used</div></div>
    <div class="gauge"><div class="g-label">Load (1/5/15)</div><div class="g-value">${last.load1}</div><div class="g-sub">${last.load5} / ${last.load15}</div></div>`;
}

function updateCoreBars(last) {
  const cores = last.cores || [];
  $("#core-bars").innerHTML =
    cores
      .map(
        (v, i) => `
      <div class="core-bar">
        <div class="core-label">core ${i} <span class="muted">${v.toFixed(0)}%</span></div>
        <div class="bar cpu"><div style="width:${Math.min(v, 100)}%"></div></div>
      </div>`
      )
      .join("") || `<div class="muted small">no core data</div>`;
}

async function pollMonitorSnapshot() {
  if (!monitorAvailable) return;
  try {
    const snap = await api("/api/monitor/snapshot");
    const sys = snap.system;
    if (sys) {
      const meta = $("#monitor-meta");
      if (meta) meta.textContent = `${sys.hostname} · ${sys.platform} · ${sys.cores} cores · up ${fmtUptime(sys.uptime_seconds)}`;
    }
    if (snap.processes) renderTopProcesses(snap.processes);
    if (snap.fs_usage) renderFsUsage(snap.fs_usage);
  } catch {
    /* transient */
  }
  await pollMonitorAnomalies();
}

async function pollMonitorAnomalies() {
  if (!monitorAvailable) return;
  const bar = $("#monitor-anomalies");
  if (!bar) return;
  try {
    const data = await api("/api/monitor/anomalies?limit=5");
    const items = data.anomalies || [];
    if (!items.length) {
      bar.classList.add("hidden");
      bar.innerHTML = "";
      return;
    }
    bar.innerHTML =
      '<span class="anomalies-title">&#9888; Recent anomalies</span>' +
      items
        .map((a) => {
          const bits = [];
          if (a.cpu_anomaly) bits.push(`CPU ${a.cpu}% (z=${a.z_cpu.toFixed(1)})`);
          if (a.mem_anomaly) bits.push(`Mem ${a.mem_percent}% (z=${a.z_mem.toFixed(1)})`);
          return `<span class="anomaly-item" title="${esc(bits.join(" · "))}">${new Date(a.t).toLocaleTimeString()} — ${esc(bits.join(", "))}</span>`;
        })
        .join("");
    bar.classList.remove("hidden");
  } catch {
    /* transient */
  }
}

function renderTopProcesses(procs) {
  if (!procs.length) {
    $("#top-processes").innerHTML = `<div class="muted small">no process data</div>`;
    return;
  }
  $("#top-processes").innerHTML = `
    <table>
      <thead><tr><th>PID</th><th>Name</th><th>CPU%</th><th>Mem%</th></tr></thead>
      <tbody>
        ${procs
          .map(
            (p) => `<tr>
              <td class="mono muted">${p.pid}</td>
              <td title="${esc(p.cmdline)}"><strong>${esc(p.name)}</strong></td>
              <td>${p.cpu.toFixed(1)}</td>
              <td>${p.mem.toFixed(1)}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderFsUsage(mounts) {
  if (!mounts.length) return;
  $("#fs-usage").innerHTML = `
    <h3 class="monitor-sub">Filesystems</h3>
    ${mounts
      .map(
        (m) => `
      <div class="fs-row">
        <span class="mono small muted" title="${esc(m.device)}">${esc(m.mountpoint)}</span>
        <div class="bar mem" style="flex:1"><div style="width:${Math.min(m.percent, 100)}%"></div></div>
        <span class="small muted">${m.percent}%</span>
      </div>`
      )
      .join("")}`;
}

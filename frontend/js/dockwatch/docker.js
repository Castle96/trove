/**
 * docker.js — Docker engine status, containers, stacks, images, deploy, and the
 * Overview tab (docker-centric dashboard).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 * NOTE: unlike the Dockwatch split (which lost this declaration), the
 * images/vulnerability exposure panel element is bound here at module scope so
 * refreshImages() can toggle it.
 */
import {
  state,
  $,
  esc,
  fmtBytes,
  fmtDate,
  stateBadge,
  selectedEndpointId,
  api,
  toast,
  sevBadge,
  openForm,
  registerTabHandler,
} from "./core.js";
import { refreshActivity } from "./activity.js";

const exposure = $("#images-exposure");

// ------------------------------------------------------------------ docker status
export async function loadDockerStatus() {
  const pill = $("#docker-pill");
  const banner = $("#docker-banner");
  const id = selectedEndpointId();
  try {
    if (id !== null) {
      const status = await api(`/api/docker/status?endpoint_id=${id}`);
      state.dockerStatus = status;
      if (status.available) {
        pill.textContent = `${status.engine === "podman" ? "Podman" : "Docker"} ${status.version || ""}`;
        pill.className = "pill pill-ok";
        banner.classList.add("hidden");
      } else {
        pill.textContent = "Endpoint unavailable";
        pill.className = "pill pill-bad";
        banner.textContent = `Endpoint unreachable: ${status.reason || "unknown reason"}.`;
        banner.classList.remove("hidden");
      }
      return;
    }
    const fleet = await api("/api/endpoints/fleet/overview").catch(() => null);
    state.fleet = fleet;
    if (!fleet || !fleet.total_endpoints) {
      return loadDockerStatusLocal(pill, banner);
    }
    if (fleet.reachable === fleet.total_endpoints) {
      pill.textContent = `${fleet.reachable}/${fleet.total_endpoints} engines · ${fleet.total_running}/${fleet.total_containers} running`;
      pill.className = "pill pill-ok";
      banner.classList.add("hidden");
    } else if (fleet.reachable > 0) {
      pill.textContent = `${fleet.reachable}/${fleet.total_endpoints} engines reachable`;
      pill.className = "pill pill-warn";
      banner.classList.add("hidden");
    } else {
      pill.textContent = "No engines reachable";
      pill.className = "pill pill-bad";
      banner.textContent = `No container engine is reachable. Monitoring features are disabled — inventory still works.`;
      banner.classList.remove("hidden");
    }
    state.dockerStatus = fleet.endpoints.find((e) => e.endpoint_id === null) || null;
  } catch {
    pill.textContent = "Docker: error";
    pill.className = "pill pill-bad";
  }
}

async function loadDockerStatusLocal(pill, banner) {
  try {
    const status = await api("/api/docker/status");
    state.dockerStatus = status;
    if (status.available) {
      pill.textContent = `${status.engine === "podman" ? "Podman" : "Docker"} ${status.version || ""}`;
      pill.className = "pill pill-ok";
      banner.classList.add("hidden");
    } else {
      pill.textContent = "Docker unavailable";
      pill.className = "pill pill-bad";
      banner.textContent = `No container engine is reachable (Docker/Podman): ${status.reason || "unknown reason"}. Monitoring features are disabled — inventory still works.`;
      banner.classList.remove("hidden");
    }
  } catch {
    pill.textContent = "Docker: error";
    pill.className = "pill pill-bad";
  }
}

// ------------------------------------------------------------------ overview
export async function loadOverview() {
  const cards = $("#overview-cards");
  const engine = $("#engine-details");
  const bars = $("#docker-resource-bars");

  try {
    const overview = await api("/api/inventory/overview");
    state.inventoryOverview = overview;
  } catch (err) {
    console.error("inventory overview failed", err);
  }

  let containersTotal = 0;
  let containersRunning = 0;
  let images = 0;
  let networks = 0;
  let volumes = 0;
  let dockerCards = "";
  if (state.dockerStatus && state.dockerStatus.available) {
    containersTotal = state.dockerStatus.containers_total;
    containersRunning = state.dockerStatus.containers_running;
    images = state.dockerStatus.images;
    networks = state.dockerStatus.networks;
    volumes = state.dockerStatus.volumes;
    dockerCards = `
      <div class="card"><div class="num">${containersRunning}<span class="muted small">/${containersTotal}</span></div><div class="label">Containers running</div></div>
      <div class="card"><div class="num">${images}</div><div class="label">Images</div></div>
      <div class="card"><div class="num">${networks}</div><div class="label">Networks</div></div>
      <div class="card"><div class="num">${volumes}</div><div class="label">Volumes</div></div>`;
  } else {
    dockerCards = `<div class="card"><div class="num muted">—</div><div class="label">Docker unavailable</div></div>`;
  }

  const inv = state.inventoryOverview || {};
  cards.innerHTML = `
    ${dockerCards}
    <div class="card"><div class="num">${inv.sites ?? 0}</div><div class="label">Sites</div></div>
    <div class="card"><div class="num">${inv.racks ?? 0}</div><div class="label">Racks</div></div>
    <div class="card"><div class="num">${inv.devices ?? 0}</div><div class="label">Devices</div></div>
    <div class="card"><div class="num">${inv.ip_addresses ?? 0}</div><div class="label">IP addresses</div></div>`;

  if (state.dockerStatus && state.dockerStatus.available) {
    const s = state.dockerStatus;
    engine.innerHTML = `
      <div>Host: <strong>${esc(s.hostname || "?")}</strong> &middot; ${esc(s.os || "")} ${esc(s.arch || "")}</div>
      <div class="small">Engine: ${s.engine === "podman" ? "Podman" : "Docker"} &middot; API ${s.api_version || "?"} &middot; Version ${s.version || "?"}</div>`;
    if (containersTotal > 0) {
      const containers = await api("/api/docker/containers?include_stats=true").catch(() => []);
      let memUse = 0, memLimit = 0, cpuSum = 0, cpuCount = 0;
      for (const c of containers) {
        if (c.stats) {
          memUse += c.stats.memory_usage;
          memLimit += c.stats.memory_limit;
          cpuSum += c.stats.cpu_percent;
          cpuCount += 1;
        }
      }
      const memPct = memLimit > 0 ? Math.min((memUse / memLimit) * 100, 100) : 0;
      const cpuAvg = cpuCount > 0 ? cpuSum / cpuCount : 0;
      bars.innerHTML = `
        <div class="bar-row"><div class="bar-label"><span>CPU average (${cpuCount} containers)</span><span>${cpuAvg.toFixed(1)}%</span></div>
          <div class="bar cpu"><div style="width:${Math.min(cpuAvg, 100)}%"></div></div></div>
        <div class="bar-row"><div class="bar-label"><span>Memory</span><span>${fmtBytes(memUse)} / ${fmtBytes(memLimit)}</span></div>
          <div class="bar mem"><div style="width:${memPct}%"></div></div></div>`;
    } else {
      bars.innerHTML = `<div class="muted">No containers on this host yet.</div>`;
    }
  } else {
    engine.innerHTML = `<div class="muted">Docker socket not reachable — showing inventory counts only.</div>`;
    bars.innerHTML = "";
  }

  await refreshActivity($("#overview-activity"), 6);
}

// ------------------------------------------------------------------ containers
export async function fetchContainers() {
  if (state.loading) return state.containers;
  state.loading = true;
  try {
    if (state.selectedEndpoint === "all") {
      state.containers = await api("/api/endpoints/fleet/containers?include_stats=true");
    } else {
      const id = selectedEndpointId();
      const qs = id === null ? "?include_stats=true" : `?include_stats=true&endpoint_id=${id}`;
      state.containers = await api(`/api/docker/containers${qs}`);
    }
  } catch (err) {
    if (state.tab === "containers") {
      $("#containers-table").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
    }
  } finally {
    state.loading = false;
  }
  return state.containers;
}

export async function refreshContainers() {
  const table = $("#containers-table");
  const rank = $("#containers-rank");
  table.innerHTML = `<div class="muted">Loading…</div>`;
  const containers = await fetchContainers();
  if (!containers.length) {
    table.innerHTML = `<div class="muted">No containers.</div>`;
    if (rank) rank.innerHTML = ``;
    return;
  }
  const rows = containers
    .map((c) => {
      const cpu = c.stats ? `${c.stats.cpu_percent.toFixed(1)}%` : "—";
      const mem = c.stats ? `${c.stats.memory_percent.toFixed(1)}%` : "—";
      const net = c.stats
        ? `${fmtBytes(c.stats.network_rx)} / ${fmtBytes(c.stats.network_tx)}`
        : "—";
      const stack = c.stack ? `<span class="badge badge-planned">${esc(c.stack)}</span>` : "";
      const ep = c.endpoint_id == null ? "" : ` data-endpoint="${c.endpoint_id}"`;
      const endpoint = state.selectedEndpoint === "all" ? `<td class="muted">${esc(c.endpoint_name || "local")}</td>` : "";
      const hotlinks = (c.links || []).map((l) =>
        `<a class="chip" href="${esc(l.url)}" target="_blank" rel="noopener" title="${esc(l.container_port || "")} · ${esc(l.scheme)}">${esc(l.host_port)}<span class="chip-scheme">${esc(l.scheme)}</span></a>`
      ).join("");
      return `<tr>
        <td class="mono">${esc(c.short_id)}</td>
        <td><strong>${esc(c.name)}</strong><div class="small muted">${stack}</div></td>
        <td class="mono">${esc(c.image)}</td>
        ${endpoint}
        <td>${stateBadge(c.state)}</td>
        <td class="muted">${esc(c.status)}</td>
        <td>${hotlinks || `<span class="muted">—</span>`}</td>
        <td>${cpu}</td><td>${mem}</td><td>${net}</td>
        <td>
          <button class="link" data-action="logs" data-id="${esc(c.id)}"${ep}>logs</button>
          ${c.state === "running" ? `<button class="link" data-action="stop" data-id="${esc(c.id)}"${ep}>stop</button>` : `<button class="link" data-action="start" data-id="${esc(c.id)}"${ep}>start</button>`}
          <button class="link" data-action="restart" data-id="${esc(c.id)}"${ep}>restart</button>
          <button class="link danger" data-action="remove" data-id="${esc(c.id)}"${ep}>remove</button>
        </td>
      </tr>`;
    })
    .join("");
  const endpointHead = state.selectedEndpoint === "all" ? "<th>Endpoint</th>" : "";
  table.innerHTML = `<table>
    <thead><tr><th>ID</th><th>Name</th><th>Image</th>${endpointHead}<th>State</th><th>Status</th><th>Ports</th><th>CPU</th><th>Mem</th><th>Net RX/TX</th><th>Actions</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  if (rank) {
    const sorted = containers
      .filter((c) => c.stats)
      .sort((a, b) => (b.stats?.cpu_percent || 0) - (a.stats?.cpu_percent || 0))
      .slice(0, 5);
    if (sorted.length) {
      const maxCpu = Math.max(...sorted.map((c) => c.stats.cpu_percent), 1);
      rank.innerHTML = `
        <div class="rank-head">
          <span class="chart-title">Top CPU consumers (live)</span>
          <span class="small muted">${sorted.length} of ${containers.filter(c => c.stats).length} containers with stats</span>
        </div>
        <div class="rank-bars">
          ${sorted.map((c) => {
            const cpuPct = Math.min((c.stats.cpu_percent / maxCpu) * 100, 100);
            const memPct = Math.min((c.stats.memory_percent / 100) * 100, 100);
            return `<div class="rank-row">
              <span class="mono small">${esc(c.short_id)}</span>
              <span class="rank-name">${esc(c.name)}</span>
              <div class="rank-bar-wrap">
                <div class="rank-bar cpu" style="width:${cpuPct}%"></div>
                <div class="rank-bar mem" style="width:${memPct}%; margin-top:2px"></div>
              </div>
              <span class="mono small">${c.stats.cpu_percent.toFixed(1)}% / ${c.stats.memory_percent.toFixed(1)}%</span>
            </div>`;
          }).join("")}
        </div>`;
      rank.classList.remove("hidden");
    } else {
      rank.classList.add("hidden");
    }
  };
}

export function endpointParam(el) {
  const v = el?.dataset?.endpoint;
  return v || "";
}

export async function containerAction(id, action, endpointId = "") {
  try {
    const qs = endpointId ? `?endpoint_id=${encodeURIComponent(endpointId)}` : "";
    await api(`/api/docker/containers/${id}/action/${action}${qs}`, { method: "POST" });
    toast(`${action} requested for ${id.slice(0, 12)}`);
    refreshContainers();
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function removeContainer(id, endpointId = "") {
  if (!confirm(`Remove container ${id.slice(0, 12)}? (force-stops if running)`)) return;
  try {
    const qs = endpointId ? `&endpoint_id=${encodeURIComponent(endpointId)}` : "";
    await api(`/api/docker/containers/${id}?force=true${qs}`, { method: "DELETE" });
    toast("Container removed");
    refreshContainers();
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function showContainerLogs(id, name, endpointId = "") {
  const modal = $("#logs-modal");
  $("#logs-title").textContent = `Logs — ${name || id}`;
  $("#logs-content").textContent = "Loading…";
  modal.showModal();
  try {
    const qs = endpointId ? `&endpoint_id=${encodeURIComponent(endpointId)}` : "";
    const data = await api(`/api/docker/containers/${id}/logs?tail=200${qs}`);
    $("#logs-content").textContent = data.logs || "(no log output)";
  } catch (err) {
    $("#logs-content").textContent = err.message;
  }
}

// ------------------------------------------------------------------ stacks
export async function refreshStacks() {
  const list = $("#stacks-list");
  list.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const id = selectedEndpointId();
    const qs = id === null ? "" : `?endpoint_id=${id}`;
    const stacks = await api(`/api/docker/stacks${qs}`);
    if (!stacks.length) {
      list.innerHTML = `<div class="muted">No compose stacks found. Stacks are grouped by the <span class="mono">com.docker.compose.project</span> label.</div>`;
      return;
    }
    list.innerHTML = stacks
      .map(
        (s) => `
        <div class="stack-card">
          <h3>${esc(s.project)}</h3>
          <div class="stack-sub">${esc(s.working_dir || "")} &middot; ${s.service_count} service(s) &middot; ${s.containers.length} container(s)</div>
          <div class="stack-members">
            ${s.containers
              .map(
                (c) => `
              <div class="panel" style="margin:0">
                <div><strong>${esc(c.service || c.name)}</strong> ${stateBadge(c.state)}</div>
                <div class="small muted mono">${esc(c.image)}</div>
                ${c.stats ? `<div class="small">CPU ${c.stats.cpu_percent.toFixed(1)}% &middot; Mem ${c.stats.memory_percent.toFixed(1)}%</div>` : ""}
                <button class="link" data-action="logs" data-id="${esc(c.id)}">logs</button>
              </div>`
              )
              .join("")}
          </div>
        </div>`
      )
      .join("");
  } catch (err) {
    list.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

// ------------------------------------------------------------------ images
export async function refreshImages() {
  const table = $("#images-table");
  table.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const id = selectedEndpointId();
    const qs = id === null ? "" : `?endpoint_id=${id}`;
    const [images, scans] = await Promise.all([
      api(`/api/docker/images${qs}`),
      api("/api/security/scans").catch(() => []),
    ]);
    state.scans = scans;
    const byRef = {};
    for (const s of scans) byRef[s.image_ref] = s;
    const scanFor = (img) => {
      const tags = img.tags || [];
      for (const t of tags) {
        if (byRef[t]) return byRef[t];
        const norm = t.startsWith("docker.io/") ? t : `docker.io/library/${t}`;
        if (byRef[norm]) return byRef[norm];
      }
      return null;
    };
    if (!images.length) {
      table.innerHTML = `<div class="muted">No images.</div>`;
      if (exposure) exposure.classList.add("hidden");
      return;
    }
    table.innerHTML = `<table>
      <thead><tr><th>Repository tag</th><th>ID</th><th>Size</th><th>Created</th><th>Vulns</th><th></th></tr></thead>
      <tbody>
        ${images
          .map((img) => {
            const tag = (img.tags && img.tags[0]) || img.id;
            const scan = scanFor(img);
            return `<tr>
              <td class="mono">${esc((img.tags && img.tags.join(", ")) || "<none>")}</td>
              <td class="mono">${esc(img.short_id)}</td>
              <td>${fmtBytes(img.size)}</td>
              <td class="muted">${fmtDate(img.created)}</td>
              <td>${scan ? `<button class="link" data-action="show-scan" data-id="${esc(encodeURIComponent(scan.image_ref))}">${sevBadge(scan)}</button>` : `<span class="muted">—</span>`}</td>
              <td><button class="link" data-action="scan-image" data-id="${esc(encodeURIComponent(tag))}">scan</button></td>
            </tr>`;
          })
          .join("")}
      </tbody></table>`;

    // Fleet vulnerability exposure summary
    if (exposure) {
      const scanned = scans.filter((s) => s.total > 0);
      const totalImg = images.length;
      const critical = scans.reduce((a, s) => a + s.critical, 0);
      const high = scans.reduce((a, s) => a + s.high, 0);
      const medium = scans.reduce((a, s) => a + s.medium, 0);
      const low = scans.reduce((a, s) => a + s.low, 0);
      const totalVulns = critical + high + medium + low;
      const maxStack = Math.max(critical, high, medium, low, 1);
      const bars = [
        { label: "Critical", count: critical, color: "#E63946", pct: (critical / maxStack) * 100 },
        { label: "High", count: high, color: "#F59E0B", pct: (high / maxStack) * 100 },
        { label: "Medium", count: medium, color: "#F97316", pct: (medium / maxStack) * 100 },
        { label: "Low", count: low, color: "#94A3B8", pct: (low / maxStack) * 100 },
      ].filter((b) => b.count > 0);
      const scannedPct = totalImg > 0 ? Math.round((scanned.length / totalImg) * 100) : 0;
      exposure.innerHTML = `
        <div class="exposure-head">
          <span class="chart-title">Vulnerability exposure</span>
          <span class="small muted">${scanned.length}/${totalImg} images scanned · ${scannedPct}% coverage</span>
        </div>
        <div class="exposure-bars">
          ${bars.map((b) => `
            <div class="exposure-row">
              <span class="small" style="color:${b.color}">${b.label}</span>
              <span class="small muted">${b.count}</span>
              <div class="exposure-bar-bg"><div class="exposure-bar" style="width:${b.pct}%; background:${b.color}"></div></div>
            </div>`).join("")}
        </div>
        ${totalVulns > 0 ? `<div class="exposure-total">${totalVulns} total vulnerabilities across ${scanned.length} scanned image(s)</div>` : `<div class="exposure-clean">All scanned images are clean</div>`}
      `;
      exposure.classList.remove("hidden");
    };
  } catch (err) {
    table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

export async function scanImageNow(tag) {
  toast(`Scanning ${tag}…`);
  try {
    const res = await api("/api/security/scans", { method: "POST", body: JSON.stringify({ image: tag }) });
    toast(res.total === 0 ? `${tag} is clean` : `${tag}: ${res.critical}C ${res.high}H ${res.total} total`, res.critical > 0 ? "error" : "ok");
  } catch (err) {
    toast(err.message, "error");
  }
  refreshImages();
}

export async function showScanDetail(imageRef) {
  const modal = $("#scan-modal");
  $("#scan-title").textContent = `Vulnerabilities — ${imageRef}`;
  $("#scan-content").innerHTML = `<div class="muted">Loading…</div>`;
  modal.showModal();
  try {
    const data = await api(`/api/security/scans/detail?image=${encodeURIComponent(imageRef)}`);
    const vulns = data.vulnerabilities || [];
    $("#scan-content").innerHTML = vulns.length
      ? `<table><thead><tr><th>Severity</th><th>CVE</th><th>Package</th><th>Fixed</th></tr></thead><tbody>` +
        vulns.slice(0, 100).map((v) => `<tr><td>${stateBadge(v.severity)}</td>
          <td class="mono">${v.primary_url ? `<a href="${esc(v.primary_url)}" target="_blank" rel="noopener">${esc(v.vulnerability_id)}</a>` : esc(v.vulnerability_id)}</td>
          <td class="mono">${esc(v.package || "")} ${esc(v.installed_version || "")}</td>
          <td class="mono">${esc(v.fixed_version || "—")}</td></tr>`).join("") +
        `</tbody></table>` + (vulns.length > 100 ? `<div class="muted small">Showing 100 of ${vulns.length}.</div>` : "")
      : `<div class="muted">No vulnerabilities found.</div>`;
  } catch (err) {
    $("#scan-content").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

// ------------------------------------------------------------------ deploy
export function openDeploy() {
  const id = selectedEndpointId();
  const target = id === null ? "" : state.endpoints.find((e) => e.id === id)?.name || id;
  openForm(`Deploy container${target ? ` on ${target}` : ""}`, [
    { name: "image", label: "Image (repo:tag)", required: true, full: true },
    { name: "name", label: "Name (optional)" },
    { name: "command", label: "Command (optional)" },
    { name: "ports", label: "Ports (HOST:CONTAINER, comma-separated)" },
    { name: "environment", label: "Env (KEY=value per line)", type: "textarea", full: true },
    { name: "volumes", label: "Volumes (host:container[:ro] per line)", type: "textarea", full: true },
    { name: "restart_policy", label: "Restart", type: "select", value: "unless-stopped", options: ["no", "always", "unless-stopped", "on-failure", "on-failure:3"] },
    { name: "pull_if_missing", label: "Pull image if missing", type: "select", value: "true", options: [{ value: "true", label: "yes" }, { value: "false", label: "no" }] },
  ], async (data) => {
    const ports = {};
    for (const part of String(data.ports || "").split(",")) {
      const [host, container] = part.split(":").map((s) => (s || "").trim());
      if (host && container) ports[`${container}/tcp`] = Number(host);
    }
    const environment = {};
    for (const line of String(data.environment || "").split("\n")) {
      const idx = line.indexOf("=");
      if (idx > 0) environment[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    }
    const volumes = String(data.volumes || "").split("\n").map((s) => s.trim()).filter(Boolean);
    const qs = id === null ? "" : `?endpoint_id=${id}`;
    await api(`/api/docker/containers${qs}`, {
      method: "POST",
      body: JSON.stringify({
        image: data.image.trim(),
        name: data.name.trim() || undefined,
        command: data.command.trim() || undefined,
        environment,
        ports,
        volumes,
        restart_policy: data.restart_policy,
        pull_if_missing: data.pull_if_missing === "true",
      }),
    });
    refreshContainers();
  });
}

// ------------------------------------------------------------------ tab registrations
registerTabHandler("overview", loadOverview);
registerTabHandler("containers", refreshContainers);
registerTabHandler("stacks", refreshStacks);
registerTabHandler("images", refreshImages);

/**
 * main.js — Boot / wiring module. Imports every feature module (evaluated LAST
 * so tab handler registration order is deterministic) and binds the global
 * event handlers + pollers.
 *
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/), with
 * Trove-shell adaptations:
 *   - default tab is "certificates" (PKI is the primary surface)
 *   - digital clock render (#topbar-clock) — the old inline script is gone
 *   - close buttons on the static <dialog> elements are wired here (no inline
 *     onclick under the CSP)
 */
import {
  state,
  $,
  $$,
  activateTab,
  api,
  toast,
  esc,
  openForm,
  loadEndpoints,
  STATUS_OPTIONS,
  IP_STATUS_OPTIONS,
  SITE_STATUS_OPTIONS,
  siteSelect,
  siteFields,
} from "./core.js";
import {
  loadDockerStatus,
  loadOverview,
  refreshContainers,
  refreshStacks,
  refreshImages,
  endpointParam,
  containerAction,
  removeContainer,
  showContainerLogs,
  openDeploy,
  scanImageNow,
  showScanDetail,
} from "./docker.js";
import { loadFleet } from "./fleet.js";
import {
  loadProjectsTab,
  loadSwarmOverview,
  loadProjects,
  loadAgents,
  loadNotifications,
  approveProject,
  requestProjectApproval,
  viewProject,
  markNotificationRead,
} from "./swarm.js";
import { loadModels } from "./models.js";
import { loadVoiceTab } from "./voice.js";
import { loadInventory, loadIps } from "./inventory.js";
import { initMonitor } from "./monitor.js";
import { refreshActivity } from "./activity.js";
import { bindSearch } from "./search.js";
import "./gateways.js"; // side-effect: registers the "gateways" tab handler + wires its buttons

// ------------------------------------------------------------------ digital clock (header)
function updateClock() {
  const el = $("#topbar-clock");
  if (!el) return;
  const now = new Date();
  const p = (x) => String(x).padStart(2, "0");
  const time = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  const date = now.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  el.innerHTML = `<span class="clock-time">${time}</span><span class="clock-date">${date}</span>`;
}

// ------------------------------------------------------------------ dialog close wiring (CSP-safe: no inline onclick)
function bindDialogCloses() {
  $$("dialog .btn-close").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest("dialog").close());
  });
  $$("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (ev) => {
      if (ev.target === dialog) dialog.close();
    });
  });
}

// ------------------------------------------------------------------ global actions (delegated click handler)
function bindGlobalActions() {
  document.addEventListener("click", async (ev) => {
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    const { action, id } = el.dataset;

    switch (action) {
      // docker
      case "logs":
        showContainerLogs(id, el.closest("tr")?.querySelector("strong")?.textContent, endpointParam(el));
        break;
      case "start":
      case "stop":
      case "restart":
        containerAction(id, action, endpointParam(el));
        break;
      case "remove":
        removeContainer(id, endpointParam(el));
        break;
      case "open-deploy":
        openDeploy();
        break;

      // fleet
      case "add-endpoint":
        openForm("Add endpoint", [
          { name: "name", label: "Name", required: true },
          { name: "url", label: "URL (unix:// or tcp://host:2375)", required: true },
          { name: "kind", label: "Kind", type: "select", value: "docker", options: ["docker", "host", "combo"] },
          { name: "description", label: "Description", type: "textarea" },
          { name: "credentials", label: "Credentials (JSON, optional)", type: "textarea", full: true },
          { name: "sort_order", label: "Sort order (0 = default position)", type: "number" },
        ], async (data) => {
          const payload = {};
          for (const k of ["name", "url", "kind", "description", "credentials"]) {
            if (data[k] !== undefined && data[k] !== "") payload[k] = data[k];
          }
          await api("/api/endpoints", { method: "POST", body: JSON.stringify(payload) });
          await loadEndpoints();
          loadFleet();
        });
        break;
      case "test-endpoint": {
        toast("Probing endpoint…");
        try {
          const res = await api(`/api/endpoints/${id}/test`, { method: "POST" });
          toast(res.available ? `${res.endpoint_name} reachable` : `${res.endpoint_name}: ${res.reason || "unreachable"}`, res.available ? "ok" : "error");
        } catch (err) {
          toast(err.message, "error");
        }
        loadFleet();
        break;
      }
      case "toggle-endpoint": {
        const ep = state.endpoints.find((e) => e.id === Number(id));
        if (!ep) break;
        await api(`/api/endpoints/${id}`, { method: "PUT", body: JSON.stringify({ enabled: !ep.enabled }) });
        await loadEndpoints();
        loadFleet();
        break;
      }
      case "edit-endpoint": {
        const ep = state.endpoints.find((e) => e.id === Number(id));
        if (!ep) break;
        openForm(`Edit endpoint — ${esc(ep.name)}`, [
          { name: "name", label: "Name", required: true, value: ep.name },
          { name: "url", label: "URL (unix:// or tcp://host:2375)", required: true, value: ep.url },
          { name: "kind", label: "Kind", type: "select", value: ep.kind, options: ["docker", "host", "combo"] },
          { name: "enabled", label: "Enabled", type: "select", value: ep.enabled ? "true" : "false", options: [{ value: "true", label: "yes" }, { value: "false", label: "no" }] },
          { name: "description", label: "Description", type: "textarea", value: ep.description || "" },
          { name: "credentials", label: "Credentials (JSON, optional)", type: "textarea", value: ep.credentials || "", full: true },
          { name: "sort_order", label: "Sort order (0 = default position)", type: "number", value: ep.sort_order ?? 0 },
        ], async (data) => {
          const payload = {};
          for (const k of ["name", "url", "kind", "description", "credentials"]) {
            if (data[k] !== undefined && data[k] !== "") payload[k] = data[k];
          }
          if (data.enabled !== undefined) payload.enabled = data.enabled === "true";
          if (data.sort_order !== undefined && data.sort_order !== "") payload.sort_order = Number(data.sort_order);
          await api(`/api/endpoints/${id}`, { method: "PUT", body: JSON.stringify(payload) });
          await loadEndpoints();
          loadFleet();
        });
        break;
      }
      case "view-endpoint": {
        const ep = state.endpoints.find((e) => e.id === Number(id));
        if (!ep) break;
        const live = (state.fleet && state.fleet.endpoints || []).find((f) => f.endpoint_id === ep.id);
        const content = document.getElementById("endpoint-detail-content");
        content.innerHTML = `
          <table class="form-grid" style="grid-template-columns: 1fr 1fr; gap:0">
            <tbody>
              <tr><td><strong>Name</strong></td><td class="mono">${esc(ep.name)}</td></tr>
              <tr><td><strong>URL</strong></td><td class="mono small">${esc(ep.url)}</td></tr>
              <tr><td><strong>Kind</strong></td><td><span class="badge badge-planned">${esc(ep.kind || "docker")}</span></td></tr>
              <tr><td><strong>Enabled</strong></td><td>${ep.enabled ? "yes" : "no"}</td></tr>
              <tr><td><strong>Sort order</strong></td><td>${ep.sort_order ?? 0}</td></tr>
              <tr><td><strong>Description</strong></td><td class="small">${ep.description ? esc(ep.description) : "<span class='muted'>—</span>"}</td></tr>
              ${ep.credentials ? `<tr><td><strong>Credentials</strong></td><td class="mono small" style="white-space:pre-wrap">${esc(ep.credentials)}</td></tr>` : ""}
              ${live ? `
                <tr><td><strong>Status</strong></td><td>${live.available ? `<span class="badge badge-active">ok</span>` : `<span class="badge badge-failed">down</span>`}</td></tr>
                <tr><td><strong>Engine</strong></td><td class="mono">${esc(live.engine || "—")}</td></tr>
                <tr><td><strong>Version</strong></td><td class="mono small">${esc(live.version || "—")}</td></tr>
                <tr><td><strong>Containers</strong></td><td>${live.containers_running}/${live.containers_total} running/total</td></tr>
                <tr><td><strong>Last seen</strong></td><td class="mono small">${live.last_seen ? new Date(live.last_seen).toLocaleString() : "—"}</td></tr>
                ${live.last_error ? `<tr><td><strong>Last error</strong></td><td class="small">${esc(live.last_error)}</td></tr>` : ""}
              ` : `<tr><td><strong>Status</strong></td><td class="muted">never probed</td></tr>`}
            </tbody>
          </table>`;
        document.getElementById("endpoint-detail-title").textContent = `Endpoint — ${esc(ep.name)}`;
        document.getElementById("endpoint-detail-modal").showModal();
        break;
      }
      case "delete-endpoint":
        if (confirm("Delete this endpoint? Cached scans stay.")) {
          await api(`/api/endpoints/${id}`, { method: "DELETE" });
          if (String(state.selectedEndpoint) === String(id)) {
            state.selectedEndpoint = "all";
            try { localStorage.setItem("dockwatch-endpoint", "all"); } catch { /* ignore */ }
          }
          await loadEndpoints();
          loadFleet();
        }
        break;

      // security
      case "scan-image":
        await scanImageNow(decodeURIComponent(id));
        break;
      case "show-scan":
        showScanDetail(decodeURIComponent(id));
        break;

      // sites
      case "add-site":
        openForm("Add site", siteFields({}, true), async (data) => {
          await api("/api/inventory/sites", { method: "POST", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      case "edit-site": {
        const site = state.sites.find((s) => s.id === Number(id));
        openForm("Edit site", siteFields(site, false), async (data) => {
          await api(`/api/inventory/sites/${id}`, { method: "PUT", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      }
      case "delete-site":
        if (confirm("Delete this site and everything under it?")) {
          await api(`/api/inventory/sites/${id}`, { method: "DELETE" });
          loadInventory();
        }
        break;

      // racks
      case "add-rack": {
        if (!state.sites.length) {
          toast("Create a site before adding racks", "error");
          break;
        }
        openForm("Add rack", [...siteSelect({}),
          { name: "name", label: "Name", required: true },
          { name: "status", label: "Status", type: "select", value: "active", options: SITE_STATUS_OPTIONS },
          { name: "u_height", label: "U height", type: "number", value: 42 },
          { name: "position", label: "Rack position", type: "number" },
          { name: "role", label: "Role" },
          { name: "description", label: "Description", type: "textarea" },
        ], async (data) => {
          await api("/api/inventory/racks", { method: "POST", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      }
      case "edit-rack": {
        const rack = state.racks.find((r) => r.id === Number(id));
        if (!rack) break;
        openForm("Edit rack", [...siteSelect(rack),
          { name: "name", label: "Name", required: true, value: rack.name },
          { name: "status", label: "Status", type: "select", value: rack.status, options: SITE_STATUS_OPTIONS },
          { name: "u_height", label: "U height", type: "number", value: rack.u_height },
          { name: "position", label: "Rack position", type: "number", value: rack.position },
          { name: "role", label: "Role", value: rack.role },
          { name: "description", label: "Description", type: "textarea", value: rack.description },
        ], async (data) => {
          await api(`/api/inventory/racks/${id}`, { method: "PUT", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      }
      case "delete-rack":
        if (confirm("Delete this rack?")) {
          await api(`/api/inventory/racks/${id}`, { method: "DELETE" });
          loadInventory();
        }
        break;

      // devices
      case "add-device": {
        if (!state.sites.length) {
          toast("Create a site before adding devices", "error");
          break;
        }
        openForm("Add device", [
          { name: "name", label: "Name", required: true },
          { name: "device_type", label: "Device type", required: true },
          { name: "status", label: "Status", type: "select", value: "active", options: STATUS_OPTIONS },
          ...siteSelect({}),
          { name: "rack_id", label: "Rack", type: "select", value: "", options: [{ value: "", label: "— none —" }, ...state.racks.map((r) => ({ value: r.id, label: r.name }))] },
          { name: "rack_position", label: "Rack position (U)", type: "number" },
          { name: "vendor", label: "Vendor" },
          { name: "model", label: "Model" },
          { name: "serial", label: "Serial" },
          { name: "asset_tag", label: "Asset tag" },
          { name: "role", label: "Role" },
          { name: "description", label: "Description", type: "textarea" },
        ], async (data) => {
          if (!data.rack_id) delete data.rack_id;
          if (!data.rack_position) delete data.rack_position;
          await api("/api/inventory/devices", { method: "POST", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      }
      case "edit-device": {
        const device = state.devices.find((d) => d.id === Number(id));
        if (!device) break;
        openForm("Edit device", [
          { name: "name", label: "Name", required: true, value: device.name },
          { name: "device_type", label: "Device type", required: true, value: device.device_type },
          { name: "status", label: "Status", type: "select", value: device.status, options: STATUS_OPTIONS },
          ...siteSelect(device),
          { name: "rack_id", label: "Rack", type: "select", value: device.rack_id ?? "", options: [{ value: "", label: "— none —" }, ...state.racks.map((r) => ({ value: r.id, label: r.name }))] },
          { name: "rack_position", label: "Rack position (U)", type: "number", value: device.rack_position },
          { name: "vendor", label: "Vendor", value: device.vendor },
          { name: "model", label: "Model", value: device.model },
          { name: "serial", label: "Serial", value: device.serial },
          { name: "asset_tag", label: "Asset tag", value: device.asset_tag },
          { name: "role", label: "Role", value: device.role },
          { name: "description", label: "Description", type: "textarea", value: device.description },
        ], async (data) => {
          if (!data.rack_id) { data.rack_id = null; }
          if (!data.rack_position) { data.rack_position = null; }
          await api(`/api/inventory/devices/${id}`, { method: "PUT", body: JSON.stringify(data) });
          loadInventory();
        });
        break;
      }
      case "delete-device":
        if (confirm("Delete this device?")) {
          await api(`/api/inventory/devices/${id}`, { method: "DELETE" });
          loadInventory();
        }
        break;

      // ips
      case "add-ip":
        openForm("Add IP address", [
          { name: "address", label: "Address (CIDR, e.g. 10.0.0.5/24)", required: true },
          { name: "status", label: "Status", type: "select", value: "active", options: IP_STATUS_OPTIONS },
          { name: "dns_name", label: "DNS name" },
          { name: "device_id", label: "Device", type: "select", value: "", options: [{ value: "", label: "— none —" }, ...state.devices.map((d) => ({ value: d.id, label: d.name }))] },
          { name: "interface", label: "Interface" },
          { name: "role", label: "Role" },
          { name: "description", label: "Description", type: "textarea" },
        ], async (data) => {
          if (!data.device_id) delete data.device_id;
          await api("/api/inventory/ip-addresses", { method: "POST", body: JSON.stringify(data) });
          loadIps();
        });
        break;
      case "edit-ip": {
        const ip = state.ips.find((x) => x.id === Number(id));
        if (!ip) break;
        openForm("Edit IP address", [
          { name: "address", label: "Address (CIDR)", required: true, value: ip.address },
          { name: "status", label: "Status", type: "select", value: ip.status, options: IP_STATUS_OPTIONS },
          { name: "dns_name", label: "DNS name", value: ip.dns_name },
          { name: "device_id", label: "Device", type: "select", value: ip.device_id ?? "", options: [{ value: "", label: "— none —" }, ...state.devices.map((d) => ({ value: d.id, label: d.name }))] },
          { name: "interface", label: "Interface", value: ip.interface },
          { name: "role", label: "Role", value: ip.role },
          { name: "description", label: "Description", type: "textarea", value: ip.description },
        ], async (data) => {
          if (!data.device_id) data.device_id = null;
          await api(`/api/inventory/ip-addresses/${id}`, { method: "PUT", body: JSON.stringify(data) });
          loadIps();
        });
        break;
      }
      case "delete-ip":
        if (confirm(`Delete IP ${state.ips.find((x) => x.id === Number(id))?.address}?`)) {
          await api(`/api/inventory/ip-addresses/${id}`, { method: "DELETE" });
          loadIps();
        }
        break;

      // swarm projects
      case "add-project":
        openForm("New project", [
          { name: "name", label: "Name", required: true, full: true },
          { name: "description", label: "Description", type: "textarea", full: true },
          { name: "repo_url", label: "Repo URL", full: true },
          { name: "branch", label: "Branch" },
          { name: "agent_id", label: "Agent", type: "select", value: "", options: [{ value: "", label: "— none —" }, ...state.agents.map((a) => ({ value: a.id, label: a.name }))] },
        ], async (data) => {
          if (!data.agent_id) delete data.agent_id;
          else data.agent_id = Number(data.agent_id);
          await api("/api/swarm/projects", { method: "POST", body: JSON.stringify(data) });
          loadProjectsTab();
        });
        break;
      case "add-agent":
        openForm("New agent", [
          { name: "name", label: "Name", required: true },
          { name: "kind", label: "Kind", type: "select", value: "agent", options: ["agent", "claude", "orca", "codex"] },
          { name: "endpoint_id", label: "Endpoint", type: "select", value: "", options: [{ value: "", label: "— none —" }, ...state.endpoints.map((e) => ({ value: e.id, label: e.name }))] },
        ], async (data) => {
          if (!data.endpoint_id) delete data.endpoint_id;
          else data.endpoint_id = Number(data.endpoint_id);
          await api("/api/swarm", { method: "POST", body: JSON.stringify(data) });
          loadProjectsTab();
        });
        break;
      case "approve-project":
        approveProject(id);
        break;
      case "request-approval":
        openForm("Request approval", [
          { name: "note", label: "Note", type: "textarea", full: true },
        ], async (data) => {
          await requestProjectApproval(id, (data.note || "").trim() || null);
        });
        break;
      case "view-project":
        viewProject(id);
        break;
      case "mark-notification-read":
        markNotificationRead(id);
        break;
    }
  });
}

// ------------------------------------------------------------------ static buttons + endpoint picker
function bindStaticButtons() {
  $$("#tabs .tab").forEach((btn) => btn.addEventListener("click", () => activateTab(btn.dataset.tab)));
  const refreshBtn = $("#refresh-btn");
  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    activateTab(state.tab);
    loadDockerStatus();
    toast("Refreshed");
  });
  const refreshContainersBtn = $("#refresh-containers");
  if (refreshContainersBtn) refreshContainersBtn.addEventListener("click", refreshContainers);
  const refreshStacksBtn = $("#refresh-stacks");
  if (refreshStacksBtn) refreshStacksBtn.addEventListener("click", refreshStacks);
  const refreshImagesBtn = $("#refresh-images");
  if (refreshImagesBtn) refreshImagesBtn.addEventListener("click", refreshImages);
  const refreshActivityBtn = $("#refresh-activity");
  if (refreshActivityBtn) refreshActivityBtn.addEventListener("click", refreshActivity);
  const fleetBtn = $("#refresh-fleet");
  if (fleetBtn) fleetBtn.addEventListener("click", loadFleet);
  const projectsBtn = $("#refresh-projects");
  if (projectsBtn) projectsBtn.addEventListener("click", loadProjectsTab);
  const modelsBtn = $("#refresh-models");
  if (modelsBtn) modelsBtn.addEventListener("click", loadModels);
  const voiceBtn = $("#refresh-voice");
  if (voiceBtn) voiceBtn.addEventListener("click", loadVoiceTab);
  const picker = $("#endpoint-picker");
  if (picker) picker.addEventListener("change", () => {
    state.selectedEndpoint = picker.value;
    try {
      localStorage.setItem("dockwatch-endpoint", picker.value);
    } catch { /* storage unavailable */ }
    loadDockerStatus();
    activateTab(state.tab);
  });
}

// ------------------------------------------------------------------ init
async function init() {
  bindGlobalActions();
  bindSearch();
  bindStaticButtons();
  bindDialogCloses();
  updateClock();
  setInterval(updateClock, 1000);

  await loadEndpoints();
  await loadDockerStatus();
  await loadOverview();
  activateTab("overview"); // Overview is the default landing view
  initMonitor();
  loadSwarmOverview(true);

  // Poll swarm overview every 30s for the approval badge.
  setInterval(async () => {
    const before = state._lastApprovalCount || 0;
    await loadSwarmOverview(true);
    const count = state._lastApprovalCount || 0;
    if (count > 0 && count > before) {
      toast(`${count} project${count === 1 ? "" : "s"} need approval`, "error");
    }
    if (state.tab === "projects") {
      loadProjects();
      loadAgents();
      loadNotifications();
    }
  }, 30000);

  // Voice pipeline is a live view — refresh it while visible.
  setInterval(async () => {
    if (state.tab === "voice") loadVoiceTab();
  }, 4000);

  // Auto-refresh Docker-heavy views every 5s.
  setInterval(async () => {
    if (state.tab === "models") loadModels();
    if (["overview", "containers", "stacks", "images", "fleet"].includes(state.tab)) {
      await loadDockerStatus();
      if (state.dockerStatus && state.dockerStatus.available) {
        if (state.tab === "containers") refreshContainers();
        else if (state.tab === "stacks") refreshStacks();
        else if (state.tab === "images") refreshImages();
        else if (state.tab === "fleet") loadFleet();
        else loadOverview();
      } else if (state.tab === "fleet" || (state.fleet && state.fleet.reachable > 0)) {
        if (state.tab === "fleet") loadFleet();
        else if (state.tab === "containers") refreshContainers();
        else loadOverview();
      }
    }
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
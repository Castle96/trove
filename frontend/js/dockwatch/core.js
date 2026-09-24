/**
 * core.js — shared state, API client, DOM helpers, form builder, tab routing.
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/). All calls now
 * carry the same Bearer token as the Trove API client, and the 401 prompt
 * is shared with js/api.js via window.__troveAuthPrompted.
 */

// ------------------------------------------------------------------ theme (fixed dark — Trove is dark-mode only)
(function () {
  const KEY = "dockwatch-theme";
  try { localStorage.setItem(KEY, "dark"); } catch (e) {}

  function apply() {
    document.documentElement.dataset.theme = "dark";
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = "\u263E";
      btn.title = "Dark mode (Trove)";
      btn.disabled = true;
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply);
  else apply();
})();

// ------------------------------------------------------------------ state
export const state = {
  tab: "overview",
  dockerStatus: null,
  containers: [],
  loading: false,
  sites: [],
  racks: [],
  devices: [],
  ips: [],
  inventoryOverview: null,
  selectedEndpoint: "all",
  endpoints: [],
  fleet: null,
  models: null,
  scans: [],
  projects: [],
  agents: [],
  notifications: [],
  swarmOverview: null,
  _lastApprovalCount: 0,
};

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ------------------------------------------------------------------ helpers
export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function stateBadge(state) {
  const lower = String(state || "unknown").toLowerCase();
  return `<span class="badge badge-${lower}">${esc(state)}</span>`;
}

// ------------------------------------------------------------------ endpoints
export function selectedEndpointId() {
  const v = state.selectedEndpoint;
  if (v === "all" || v === "local") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function endpointSuffix(extra = "") {
  const id = selectedEndpointId();
  if (id === null) return extra;
  return extra ? `${extra}&endpoint_id=${id}` : `?endpoint_id=${id}`;
}

export async function loadEndpoints() {
  try {
    state.endpoints = await api("/api/endpoints");
  } catch {
    state.endpoints = [];
  }
  const picker = $("#endpoint-picker");
  if (!picker) return;
  const saved = (() => {
    try {
      return localStorage.getItem("dockwatch-endpoint") || "all";
    } catch {
      return "all";
    }
  })();
  const valid = new Set(["all", "local", ...state.endpoints.map((e) => String(e.id))]);
  state.selectedEndpoint = valid.has(saved) ? saved : "all";
  picker.innerHTML =
    `<option value="all">All endpoints</option><option value="local">Local</option>` +
    state.endpoints.map((e) => `<option value="${e.id}">${esc(e.name)}${e.enabled ? "" : " (disabled)"}</option>`).join("");
  picker.value = state.selectedEndpoint;
}

export function sevBadge(scan) {
  if (!scan || scan.total === 0) return `<span class="badge badge-active">clean</span>`;
  if (scan.critical > 0) return `<span class="badge badge-failed">${scan.critical}C / ${scan.total}</span>`;
  if (scan.high > 0) return `<span class="badge badge-staged">${scan.high}H / ${scan.total}</span>`;
  return `<span class="badge badge-planned">${scan.total}</span>`;
}

// ------------------------------------------------------------------ api client
function readToken() {
  try {
    // Prefer the Trove key; fall back to the legacy certvault_token so existing
    // sessions keep working after the rebrand (migrated on next set).
    return localStorage.getItem("trove_token") || localStorage.getItem("certvault_token") || "";
  } catch {
    return "";
  }
}

export async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  const token = readToken();
  if (token) opts.headers["Authorization"] = "Bearer " + token;

  const res = await fetch(path, opts);

  // Shared 401 handling: one prompt across both API clients, then retry once.
  if (res.status === 401 && !window.__troveAuthPrompted) {
    window.__troveAuthPrompted = true;
    try {
      const entered = window.prompt(
        "Trove requires an API token. Enter it now (or press Cancel to abort):"
      );
      if (entered) {
        localStorage.setItem("trove_token", entered);
        window.dispatchEvent(new CustomEvent("trove:token", { detail: entered }));
        opts.headers["Authorization"] = "Bearer " + entered;
        const retry = await fetch(path, opts);
        if (retry.status === 204) return null;
        const rdata = await retry.json().catch(() => null);
        if (!retry.ok) {
          const detail = rdata && rdata.detail ? rdata.detail : `HTTP ${retry.status}`;
          throw new Error(detail);
        }
        return rdata;
      }
    } finally {
      window.__troveAuthPrompted = false;
    }
  }

  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

export function toast(message, type = "ok") {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast ${type}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 3200);
}

// ------------------------------------------------------------------ tab routing
const _tabHandlers = {};

export function registerTabHandler(name, fn) {
  _tabHandlers[name] = fn;
}

export function activateTab(tab) {
  state.tab = tab;
  $$("#tabs .tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tab));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${tab}`));
  const handler = _tabHandlers[tab];
  if (handler) handler();
}

// ------------------------------------------------------------------ modal forms
export function openForm(title, fields, onSubmit) {
  const dialog = document.createElement("dialog");
  dialog.className = "modal";
  dialog.innerHTML = `
    <div class="modal-head">
      <h3>${esc(title)}</h3>
      <button class="btn-close">&times;</button>
    </div>
    <form>
      <div class="form-grid">
        ${fields
          .map((f) => {
            const type = f.type === "textarea" ? "textarea" : `input type="${f.type || "text"}"`;
            const attrs = [
              `name="${esc(f.name)}"`,
              f.required ? "required" : "",
              f.pattern ? `pattern="${esc(f.pattern)}"` : "",
            ].join(" ");
            let control;
            if (f.type === "select") {
              control = `<select name="${esc(f.name)}">${(f.options || [])
                .map((o) => {
                  const val = o.value ?? o;
                  const label = o.label ?? o;
                  const sel = f.value !== undefined && String(f.value) === String(val) ? "selected" : "";
                  return `<option value="${esc(val)}" ${sel}>${esc(label)}</option>`;
                })
                .join("")}</select>`;
            } else if (f.type === "textarea") {
              control = `<textarea name="${esc(f.name)}" rows="2">${esc(f.value ?? "")}</textarea>`;
            } else {
              control = `<${type} value="${esc(f.value ?? "")}" ${attrs}>`;
            }
            return `<div class="field ${f.full ? "full" : ""}">
              <label>${esc(f.label)}</label>${control}
            </div>`;
          })
          .join("")}
      </div>
      <div class="form-actions">
        <button type="button" class="btn btn-cancel">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </form>`;

  dialog.querySelector(".btn-close").onclick = () => dialog.close();
  dialog.querySelector(".btn-cancel").onclick = () => dialog.close();

  const form = dialog.querySelector("form");
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const data = {};
    for (const el of form.elements) {
      if (el.name && !el.disabled) data[el.name] = el.type === "number" ? Number(el.value) : el.value;
      if (el.type === "number" && el.value === "") delete data[el.name];
    }
    try {
      await onSubmit(data);
      dialog.close();
      toast("Saved");
    } catch (err) {
      toast(err.message, "error");
    }
  };

  document.body.appendChild(dialog);
  dialog.showModal();
  dialog.addEventListener("close", () => dialog.remove());
}

export const STATUS_OPTIONS = ["active", "planned", "staged", "offline", "failed", "inventory", "retired"];
export const IP_STATUS_OPTIONS = ["active", "reserved", "dhcp", "deprecated"];
export const SITE_STATUS_OPTIONS = ["active", "planned", "decommissioning", "retired"];

export function siteSelect(existing) {
  return [
    { name: "site_id", label: "Site", type: "select", required: true, value: existing?.site_id, options: state.sites.map((s) => ({ value: s.id, label: s.name })) },
  ];
}

export function siteFields(existing, creating) {
  return [
    { name: "name", label: "Name", required: true, value: existing?.name },
    { name: "slug", label: "Slug", required: true, pattern: "[a-z0-9][a-z0-9\\-_]*", value: existing?.slug },
    { name: "status", label: "Status", type: "select", value: existing?.status || "active", options: SITE_STATUS_OPTIONS },
    { name: "facility", label: "Facility", value: existing?.facility },
    { name: "address", label: "Address", value: existing?.address },
    { name: "description", label: "Description", type: "textarea", value: existing?.description },
  ].filter((f) => f.name !== "site_id");
}

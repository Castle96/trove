/**
 * gateways.js — API Gateway management + Request Console.
 *
 * Registers the "gateways" tab. Reads/writes the /api/gateway/* endpoints via
 * the shared Dockwatch api() helper (Bearer token + one shared 401 prompt).
 *
 * Console keys are kept in memory only for the current page session: the
 * plaintext returned at creation/rotation is stashed in a module-local map so
 * the console can exercise the real enforcement path (proxy + auth + limits).
 */
import { api, esc, openForm, toast, registerTabHandler } from "./core.js";

const $ = (id) => document.getElementById(id);

let gw = []; // gateways
let routes = [];
let consumers = [];
let keys = {}; // consumerId -> [key row]
let sessionKeys = {}; // keyId -> plaintext (memory only)
let selected = null; // gateway id

const TLS_PILL = {
  none: { cls: "badge badge-planned", label: "no TLS" },
  issuing: { cls: "badge badge-warning", label: "issuing…" },
  valid: { cls: "badge badge-active", label: "TLS valid" },
  expiring: { cls: "badge badge-warning", label: "TLS expiring" },
  expired: { cls: "badge badge-failed", label: "TLS expired" },
  failed: { cls: "badge badge-failed", label: "TLS failed" },
};

function tlsPill(status) {
  const p = TLS_PILL[status] || TLS_PILL.none;
  return `<span class="${p.cls}">${p.label}</span>`;
}

function methodChips(methods) {
  return (methods || [])
    .map((m) => `<span class="badge badge-planned mono" style="margin-right:3px">${esc(m)}</span>`)
    .join("");
}

function fmtDate(v) {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

// ------------------------------------------------------------------ load + render

async function loadGateways() {
  try {
    gw = (await api("/api/gateway")) || [];
  } catch (err) {
    toast(err.message, "error");
    gw = [];
  }
  renderGatewayList();
  if (selected) {
    const still = gw.find((g) => g.id === selected);
    if (still) await loadDetail(selected);
    else selected = null;
  }
  if (!selected) {
    $("gateway-detail-empty").classList.remove("hidden");
    $("gateway-detail").classList.add("hidden");
  }
}

function renderGatewayList() {
  const list = $("gateway-list");
  if (!gw.length) {
    list.innerHTML = `<div class="px-4 py-8 text-center text-xs text-gray-500">No gateways yet.<br>Create one to expose your first API.</div>`;
    return;
  }
  list.innerHTML = gw
    .map(
      (g) => `
    <div class="px-4 py-3 cursor-pointer hover:bg-slate-900/60 transition ${g.id === selected ? "bg-slate-900/80" : ""}" data-gw-select="${g.id}">
      <div class="flex items-center justify-between gap-2">
        <div class="min-w-0">
          <div class="text-sm font-semibold text-gray-200 flex items-center gap-2">
            ${g.enabled ? `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>` : `<span class="w-1.5 h-1.5 rounded-full bg-rose-500"></span>`}
            <span class="truncate">${esc(g.name)}</span>
          </div>
          <div class="text-[11px] font-mono text-indigo-300/80 mt-0.5">/gw/${esc(g.slug)}</div>
        </div>
        <div class="text-right shrink-0">
          ${tlsPill(g.tls_status)}
          <div class="text-[10px] text-gray-500 mt-1">${g.route_count} routes · ${g.consumer_count} consumers</div>
        </div>
      </div>
      ${g.tls_error ? `<div class="text-[10px] text-rose-400 mt-1 truncate" title="${esc(g.tls_error)}">${esc(g.tls_error)}</div>` : ""}
    </div>`
    )
    .join("");
}

async function loadDetail(id) {
  selected = id;
  renderGatewayList();
  $("gateway-detail-empty").classList.add("hidden");
  $("gateway-detail").classList.remove("hidden");
  const [g, rs, cs] = await Promise.all([
    api(`/api/gateway/${id}`),
    api(`/api/gateway/${id}/routes`),
    api(`/api/gateway/${id}/consumers`),
  ]);
  const idx = gw.findIndex((x) => x.id === id);
  if (idx >= 0) gw[idx] = g;
  routes = rs || [];
  consumers = cs || [];
  keys = {};
  for (const c of consumers) {
    keys[c.id] = (await api(`/api/gateway/consumers/${c.id}/keys`)) || [];
  }
  renderMeta(g);
  renderRoutes();
  renderConsumers();
  renderConsoleAuth();
  await loadLogs(id);
}

function renderMeta(g) {
  $("gw-f-name").value = g.name || "";
  $("gw-f-slug").value = g.slug || "";
  $("gw-f-base-url").value = g.base_url || "";
  $("gw-f-tls-domain").value = g.tls_domain || "";
  $("gw-f-issuer").value = g.issuer || "";
  $("gw-f-enabled").checked = !!g.enabled;
  $("gw-f-tls-enabled").checked = !!g.tls_enabled;
  const pill = $("gw-tls-pill");
  pill.className = TLS_PILL[g.tls_status]?.cls || "badge badge-planned";
  pill.textContent = TLS_PILL[g.tls_status]?.label || "no TLS";
  pill.classList.remove("hidden");
}

function renderRoutes() {
  const box = $("gw-routes");
  if (!routes.length) {
    box.innerHTML = `<div class="text-xs text-gray-500 py-2">No routes. Add one to start proxying.</div>`;
    return;
  }
  box.innerHTML = routes
    .map(
      (r) => `
    <div class="flex items-center justify-between gap-3 rounded-lg border border-gray-800/70 bg-slate-900/40 px-3 py-2">
      <div class="min-w-0">
        <div class="text-xs font-mono text-gray-200 truncate">${esc(r.path)} <span class="text-gray-600">→</span> <span class="text-emerald-300/90">${esc(r.upstream_url)}</span></div>
        <div class="text-[10px] text-gray-500 mt-0.5 flex items-center gap-2 flex-wrap">${methodChips(r.methods)}
          <span class="badge ${r.auth_mode === "api_key" ? "badge-active" : "badge-planned"}">${r.auth_mode}</span>
          ${r.rate_limit_rpm ? `<span class="badge badge-planned">${r.rate_limit_rpm}/min</span>` : ""}
          ${r.strip_prefix ? `<span class="badge badge-planned">strip</span>` : ""}
          ${!r.enabled ? `<span class="badge badge-failed">disabled</span>` : ""}
        </div>
      </div>
      <div class="flex items-center gap-1.5 shrink-0">
        <button type="button" class="px-2 py-1 text-[11px] bg-slate-800 hover:bg-slate-700 text-gray-300 border border-gray-700/70 rounded transition" data-gw-test-route="${r.id}" title="Load into console">Test</button>
        <button type="button" class="px-2 py-1 text-[11px] bg-slate-800 hover:bg-slate-700 text-gray-300 border border-gray-700/70 rounded transition" data-gw-edit-route="${r.id}" title="Edit route">Edit</button>
        <button type="button" class="px-2 py-1 text-[11px] text-rose-300 hover:bg-rose-950/50 border border-rose-900/50 rounded transition" data-gw-del-route="${r.id}" title="Delete route"><i class="fa-solid fa-trash-can"></i></button>
      </div>
    </div>`
    )
    .join("");
}

function renderConsumers() {
  const box = $("gw-consumers");
  if (!consumers.length) {
    box.innerHTML = `<div class="text-xs text-gray-500 py-2">No consumers yet. Add one to mint API keys.</div>`;
    return;
  }
  box.innerHTML = consumers
    .map((c) => {
      const keyRows = (keys[c.id] || [])
        .map(
          (k) => `
        <div class="flex items-center justify-between gap-2 py-1 border-b border-gray-800/40 last:border-0">
          <div class="min-w-0">
            <span class="font-mono text-[11px] text-gray-300">${esc(k.key_prefix)}…</span>
            <span class="text-[10px] text-gray-500 ml-1.5">${esc(k.label || "")}</span>
            ${k.enabled ? '<span class="badge badge-active">active</span>' : '<span class="badge badge-failed">disabled</span>'}
            ${k.expires_at ? `<span class="text-[10px] text-amber-400/80">exp ${fmtDate(k.expires_at)}</span>` : ""}
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <button type="button" class="px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-gray-200 transition" data-gw-rotate-key="${k.id}">Rotate</button>
            <button type="button" class="px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-gray-200 transition" data-gw-toggle-key="${k.id}">${k.enabled ? "Disable" : "Enable"}</button>
            <button type="button" class="px-1.5 py-0.5 text-[10px] text-rose-300 hover:text-rose-200 transition" data-gw-del-key="${k.id}">Delete</button>
          </div>
        </div>`
        )
        .join("");
      return `
      <div class="rounded-lg border border-gray-800/70 bg-slate-900/40 px-3 py-2">
        <div class="flex items-center justify-between">
          <div>
            <span class="text-xs font-semibold text-gray-200">${esc(c.name)}</span>
            <span class="text-[10px] text-gray-500 ml-1.5">${(keys[c.id] || []).length} key(s)</span>
          </div>
          <div class="flex items-center gap-1.5">
            <button type="button" class="px-2 py-1 text-[11px] bg-indigo-600/80 hover:bg-indigo-600 text-white rounded transition" data-gw-add-key="${c.id}"><i class="fa-solid fa-key text-xs mr-1"></i>Add key</button>
            <button type="button" class="px-1.5 py-1 text-[10px] text-rose-300 hover:text-rose-200 transition" data-gw-del-consumer="${c.id}" title="Delete consumer">Delete</button>
          </div>
        </div>
        ${c.description ? `<div class="text-[10px] text-gray-500 mt-0.5">${esc(c.description)}</div>` : ""}
        <div class="mt-1.5">${keyRows || `<div class="text-[10px] text-gray-600 py-1">No keys</div>`}</div>
      </div>`;
    })
    .join("");
}

function renderConsoleAuth() {
  const sel = $("gw-c-auth");
  let opts = `<option value="">No API key</option>`;
  for (const c of consumers) {
    const group = (keys[c.id] || []).filter((k) => k.enabled);
    if (!group.length) continue;
    for (const k of group) {
      const label = sessionKeys[k.id]
        ? `${c.name} · ${k.key_prefix}…`
        : `${c.name} · ${k.key_prefix}… (re-enter key)`;
      opts += `<option value="${k.id}">${esc(label)}</option>`;
    }
  }
  sel.innerHTML = opts;
}

async function loadLogs(gatewayId) {
  try {
    const logs = (await api(`/api/gateway/logs?gateway_id=${gatewayId}&limit=25`)) || [];
    const box = $("gw-logs");
    if (!logs.length) {
      box.innerHTML = `<div class="text-xs text-gray-500 py-1">No proxied requests yet.</div>`;
      return;
    }
    box.innerHTML = `<table class="w-full text-left text-[11px] font-mono text-gray-300">
      <thead class="text-[10px] uppercase text-gray-500 border-b border-gray-800"><tr>
        <th class="py-1 pr-2">Time</th><th class="py-1 pr-2">Method</th><th class="py-1 pr-2">Status</th><th class="py-1 pr-2">Path</th><th class="py-1 pr-2 text-right">ms</th>
      </tr></thead><tbody>
      ${logs
        .map(
          (l) => `<tr class="border-b border-gray-800/40">
          <td class="py-1 pr-2 text-gray-500">${esc(fmtDate(l.created_at))}</td>
          <td class="py-1 pr-2">${esc(l.method)}</td>
          <td class="py-1 pr-2"><span class="${l.status >= 500 ? "text-rose-400" : l.status >= 400 ? "text-amber-400" : "text-emerald-400"}">${l.status}</span></td>
          <td class="py-1 pr-2 text-gray-400">${esc(l.path)}</td>
          <td class="py-1 pr-2 text-right text-gray-500">${l.latency_ms}</td>
        </tr>`
        )
        .join("")}
      </tbody></table>`;
  } catch (err) {
    toast(err.message, "error");
  }
}

// ------------------------------------------------------------------ actions

async function createGateway() {
  openForm(
    "New gateway",
    [
      { name: "name", label: "Name", required: true },
      { name: "slug", label: "Slug (base path  /gw/<slug>)", required: true, pattern: "[a-z0-9][a-z0-9-]*" },
      { name: "base_url", label: "Base URL (optional)" },
      { name: "tls_domain", label: "TLS domain (optional)" },
      { name: "issuer", label: "Issuer", value: "Let's Encrypt" },
    ],
    async (data) => {
      try {
        const created = await api("/api/gateway", { method: "POST", body: JSON.stringify(data) });
        toast(`Gateway /gw/${created.slug} created`);
        await loadGateways();
        await loadDetail(created.id);
      } catch (err) {
        toast(err.message, "error");
      }
    }
  );
}

async function saveGateway() {
  if (!selected) return;
  const body = {
    name: $("gw-f-name").value.trim(),
    slug: $("gw-f-slug").value.trim(),
    base_url: $("gw-f-base-url").value.trim(),
    tls_domain: $("gw-f-tls-domain").value.trim(),
    issuer: $("gw-f-issuer").value.trim(),
    enabled: $("gw-f-enabled").checked,
    tls_enabled: $("gw-f-tls-enabled").checked,
  };
  try {
    await api(`/api/gateway/${selected}`, { method: "PATCH", body: JSON.stringify(body) });
    toast("Gateway saved");
    await loadGateways();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function deleteGateway() {
  if (!selected) return;
  const g = gw.find((x) => x.id === selected);
  if (!confirm(`Delete gateway "${g.name}" and all its routes, consumers and keys?`)) return;
  try {
    await api(`/api/gateway/${selected}`, { method: "DELETE" });
    toast("Gateway deleted");
    selected = null;
    await loadGateways();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function gwTls(action) {
  if (!selected) return;
  try {
    const updated = await api(`/api/gateway/${selected}/${action}`, { method: "POST" });
    toast(`TLS ${action === "issue-tls" ? "issued" : "renewed"} (${updated.tls_status})`);
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
    await loadDetail(selected);
  }
}

async function newRoute() {
  if (!selected) return;
  openForm(
    "New route",
    [
      { name: "name", label: "Name", required: true },
      { name: "methods", label: "Methods (comma separated)", value: "GET,POST", required: true },
      { name: "path", label: "Path after /gw/<slug> (use * for prefix, e.g. /v1/*)", value: "/*", required: true },
      { name: "upstream_url", label: "Upstream URL (http://host:port)", required: true },
      { name: "auth_mode", label: "Auth", type: "select", value: "open", options: ["open", "api_key"] },
      { name: "rate_limit_rpm", label: "Rate limit (req/min, 0 = unlimited)", type: "number", value: 0 },
      { name: "timeout_ms", label: "Timeout (ms)", type: "number", value: 30000 },
      { name: "strip_prefix", label: "Strip matched prefix", type: "select", value: "false", options: [{ value: "false", label: "no" }, { value: "true", label: "yes" }] },
    ],
    async (data) => {
      const payload = {
        name: data.name,
        methods: data.methods.split(",").map((m) => m.trim().toUpperCase()).filter(Boolean),
        path: data.path.startsWith("/") ? data.path : `/${data.path}`,
        upstream_url: data.upstream_url,
        auth_mode: data.auth_mode,
        rate_limit_rpm: Number(data.rate_limit_rpm || 0),
        timeout_ms: Number(data.timeout_ms || 30000),
        strip_prefix: data.strip_prefix === "true",
      };
      try {
        await api(`/api/gateway/${selected}/routes`, { method: "POST", body: JSON.stringify(payload) });
        toast("Route added");
        await loadDetail(selected);
      } catch (err) {
        toast(err.message, "error");
      }
    }
  );
}

async function editRoute(routeId) {
  const r = routes.find((x) => x.id === Number(routeId));
  if (!r) return;
  openForm(
    `Edit route — ${esc(r.name)}`,
    [
      { name: "name", label: "Name", required: true, value: r.name },
      { name: "methods", label: "Methods (comma separated)", value: (r.methods || []).join(","), required: true },
      { name: "path", label: "Path", value: r.path, required: true },
      { name: "upstream_url", label: "Upstream URL", value: r.upstream_url, required: true },
      { name: "auth_mode", label: "Auth", type: "select", value: r.auth_mode, options: ["open", "api_key"] },
      { name: "rate_limit_rpm", label: "Rate limit (req/min)", type: "number", value: r.rate_limit_rpm },
      { name: "timeout_ms", label: "Timeout (ms)", type: "number", value: r.timeout_ms },
      { name: "strip_prefix", label: "Strip matched prefix", type: "select", value: String(r.strip_prefix), options: [{ value: "false", label: "no" }, { value: "true", label: "yes" }] },
      { name: "enabled", label: "Enabled", type: "select", value: String(r.enabled), options: [{ value: "true", label: "yes" }, { value: "false", label: "no" }] },
    ],
    async (data) => {
      const payload = {
        name: data.name,
        methods: data.methods.split(",").map((m) => m.trim().toUpperCase()).filter(Boolean),
        path: data.path,
        upstream_url: data.upstream_url,
        auth_mode: data.auth_mode,
        rate_limit_rpm: Number(data.rate_limit_rpm || 0),
        timeout_ms: Number(data.timeout_ms || 30000),
        strip_prefix: data.strip_prefix === "true",
        enabled: data.enabled === "true",
      };
      try {
        await api(`/api/gateway/routes/${routeId}`, { method: "PATCH", body: JSON.stringify(payload) });
        toast("Route updated");
        await loadDetail(selected);
      } catch (err) {
        toast(err.message, "error");
      }
    }
  );
}

async function deleteRoute(routeId) {
  if (!confirm("Delete this route?")) return;
  try {
    await api(`/api/gateway/routes/${routeId}`, { method: "DELETE" });
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function newConsumer() {
  if (!selected) return;
  openForm(
    "New consumer",
    [
      { name: "name", label: "Name", required: true },
      { name: "description", label: "Description", type: "textarea" },
    ],
    async (data) => {
      try {
        await api(`/api/gateway/${selected}/consumers`, { method: "POST", body: JSON.stringify(data) });
        toast("Consumer added");
        await loadDetail(selected);
      } catch (err) {
        toast(err.message, "error");
      }
    }
  );
}

async function deleteConsumer(consumerId) {
  if (!confirm("Delete this consumer and all its API keys?")) return;
  try {
    await api(`/api/gateway/consumers/${consumerId}`, { method: "DELETE" });
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function addKey(consumerId) {
  openForm(
    "Add API key",
    [
      { name: "label", label: "Label (e.g. prod, staging)" },
      { name: "key", label: "Custom key (8+ chars, optional)" },
      { name: "expires_at", label: "Expires (YYYY-MM-DD, optional)" },
    ],
    async (data) => {
      const payload = {};
      if (data.label) payload.label = data.label;
      if (data.key && data.key.trim()) payload.key = data.key.trim();
      if (data.expires_at) {
        const iso = new Date(`${data.expires_at}T00:00:00`);
        if (!isNaN(iso.getTime())) payload.expires_at = iso.toISOString();
      }
      try {
        const created = await api(`/api/gateway/consumers/${consumerId}/keys`, { method: "POST", body: JSON.stringify(payload) });
        sessionKeys[created.id] = created.key; // memory only
        showSecret(created.key, created.label);
        await loadDetail(selected);
      } catch (err) {
        toast(err.message, "error");
      }
    }
  );
}

async function rotateKey(keyId) {
  if (!confirm("Rotate this API key? The old key stops working immediately; the new one is shown once.")) return;
  try {
    const created = await api(`/api/gateway/keys/${keyId}/rotate`, { method: "POST" });
    sessionKeys[created.id] = created.key;
    showSecret(created.key, created.label);
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function toggleKey(keyId) {
  const k = Object.values(keys).flat().find((x) => x.id === Number(keyId));
  if (!k) return;
  try {
    await api(`/api/gateway/keys/${keyId}`, { method: "PATCH", body: JSON.stringify({ enabled: !k.enabled }) });
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function deleteKey(keyId) {
  if (!confirm("Delete this API key? It stops working immediately.")) return;
  try {
    await api(`/api/gateway/keys/${keyId}`, { method: "DELETE" });
    delete sessionKeys[keyId];
    await loadDetail(selected);
  } catch (err) {
    toast(err.message, "error");
  }
}

function showSecret(key, label) {
  const dlg = document.createElement("dialog");
  dlg.className = "modal";
  dlg.innerHTML = `
    <div class="modal-head">
      <h3>API key created <span class="text-gray-500 font-normal text-xs">${esc(label || "")}</span></h3>
      <button type="button" class="btn-close">&times;</button>
    </div>
    <p class="modal-sub">Copy it now — it is shown only once. The gateway stores only its hash. It is also kept in this page's memory for the Request Console.</p>
    <div class="form-grid"><div class="field full">
      <label>Full key</label>
      <textarea id="secret-copy-area" class="font-mono" rows="2" readonly>${esc(key)}</textarea>
    </div></div>
    <div class="form-actions">
      <button type="button" class="btn btn-cancel">Close</button>
      <button type="button" class="btn btn-primary" id="secret-copy-btn"><i class="fa-solid fa-copy mr-1"></i>Copy</button>
    </div>`;
  dlg.querySelector(".btn-close").onclick = () => dlg.close();
  dlg.querySelector(".btn-cancel").onclick = () => dlg.close();
  dlg.querySelector("#secret-copy-btn").onclick = async () => {
    const text = dlg.querySelector("#secret-copy-area").value;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      dlg.querySelector("#secret-copy-area").select();
      document.execCommand("copy");
    }
    dlg.close();
  };
  document.body.appendChild(dlg);
  dlg.showModal();
}

// ------------------------------------------------------------------ console

function consoleKeyFor(keyId) {
  if (sessionKeys[keyId]) return sessionKeys[keyId];
  const all = Object.values(keys).flat();
  const k = all.find((x) => x.id === Number(keyId));
  const entered = window.prompt(
    `Enter the full API key for ${k ? k.key_prefix : keyId}… (or Cancel to send without a key)`
  );
  if (entered && entered.trim()) {
    sessionKeys[keyId] = entered.trim();
    return entered.trim();
  }
  return "";
}

async function consoleSend() {
  const g = gw.find((x) => x.id === selected);
  if (!g) return;
  const result = $("gw-console-result");
  result.classList.remove("hidden");
  $("gw-console-status-line").textContent = "Sending…";
  $("gw-console-body").textContent = "";
  $("gw-console-status").textContent = "";

  const method = $("gw-c-method").value;
  let path = $("gw-c-path").value.trim();
  if (!path) path = "/";
  if (!path.startsWith("/")) path = `/${path}`;

  const headers = { Accept: "application/json" };
  const raw = $("gw-c-headers").value.trim();
  if (raw) {
    try {
      Object.assign(headers, JSON.parse(raw));
    } catch {
      toast("Headers must be valid JSON", "error");
      return;
    }
  }
  const keyId = $("gw-c-auth").value;
  if (keyId) {
    const key = consoleKeyFor(keyId);
    if (key) headers["X-API-Key"] = key;
  }

  const useBody = !["GET", "HEAD"].includes(method);
  const body = useBody && $("gw-c-body").value.trim() ? $("gw-c-body").value : undefined;

  const start = performance.now();
  try {
    const res = await fetch(`/gw/${g.slug}${path}`, { method, headers, body });
    const text = await res.text();
    const ms = Math.round(performance.now() - start);

    $("gw-console-status-line").innerHTML = `<span class="${res.ok ? "text-emerald-400" : res.status === 429 ? "text-amber-400" : "text-rose-400"} font-bold">${res.status} ${esc(res.statusText)}</span> <span class="text-gray-500">· ${ms}ms</span> <span class="text-gray-600">· /gw/${esc(g.slug)}${esc(path)}</span>`;
    $("gw-console-status").textContent = `${res.status} · ${ms}ms`;
    $("gw-console-latency").textContent = "";
    let pretty = text;
    try {
      pretty = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      /* leave as text */
    }
    $("gw-console-body").textContent = pretty;
    await loadLogs(selected);
  } catch (err) {
    $("gw-console-status-line").innerHTML = `<span class="text-rose-400 font-bold">Request failed</span> <span class="text-gray-500">· ${esc(err.message)}</span>`;
    $("gw-console-body").textContent = err.message;
  }
}

// ------------------------------------------------------------------ events

function wire() {
  $("btn-new-gateway").addEventListener("click", createGateway);
  $("gw-btn-save").addEventListener("click", saveGateway);
  $("gw-btn-delete").addEventListener("click", deleteGateway);
  $("gw-btn-issue-tls").addEventListener("click", () => gwTls("issue-tls"));
  $("gw-btn-renew-tls").addEventListener("click", () => gwTls("renew-tls"));
  $("gw-btn-new-route").addEventListener("click", newRoute);
  $("gw-btn-new-consumer").addEventListener("click", newConsumer);
  $("gw-c-send").addEventListener("click", consoleSend);
  $("gw-btn-clear-logs").addEventListener("click", async () => {
    if (!confirm("Clear the request log for this gateway?")) return;
    try {
      await api(`/api/gateway/logs?gateway_id=${selected}`, { method: "DELETE" });
      await loadLogs(selected);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  document.addEventListener("click", async (ev) => {
    const sel = ev.target.closest("[data-gw-select]");
    if (sel) {
      await loadDetail(Number(sel.dataset.gwSelect));
      return;
    }
    const test = ev.target.closest("[data-gw-test-route]");
    if (test) {
      const r = routes.find((x) => x.id === Number(test.dataset.gwTestRoute));
      if (r) $("gw-c-path").value = r.path.replace(/\*$/, "") + "v1/test";
      return;
    }
    const er = ev.target.closest("[data-gw-edit-route]");
    if (er) return editRoute(er.dataset.gwEditRoute);
    const dr = ev.target.closest("[data-gw-del-route]");
    if (dr) return deleteRoute(dr.dataset.gwDelRoute);
    const dc = ev.target.closest("[data-gw-del-consumer]");
    if (dc) return deleteConsumer(dc.dataset.gwDelConsumer);
    const ak = ev.target.closest("[data-gw-add-key]");
    if (ak) return addKey(ak.dataset.gwAddKey);
    const rk = ev.target.closest("[data-gw-rotate-key]");
    if (rk) return rotateKey(rk.dataset.gwRotateKey);
    const tk = ev.target.closest("[data-gw-toggle-key]");
    if (tk) return toggleKey(tk.dataset.gwToggleKey);
    const dk = ev.target.closest("[data-gw-del-key]");
    if (dk) return deleteKey(dk.dataset.gwDelKey);
  });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
else wire();
registerTabHandler("gateways", loadGateways);

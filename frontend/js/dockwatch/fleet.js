/**
 * fleet.js — Fleet endpoints tab (endpoint management + fleet overview cards +
 * discovered port hotlinks that can be promoted into gateway routes).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { state, $, esc, api, toast, openForm, registerTabHandler } from "./core.js";

export async function loadFleet() {
  const cards = $("#fleet-cards");
  const table = $("#endpoints-table");
  table.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const [fleet, endpoints] = await Promise.all([
      api("/api/endpoints/fleet/overview"),
      api("/api/endpoints"),
    ]);
    state.fleet = fleet;
    state.endpoints = endpoints;
    cards.innerHTML = `
      <div class="card"><div class="num">${fleet.total_endpoints}</div><div class="label">Endpoints</div></div>
      <div class="card"><div class="num">${fleet.reachable}</div><div class="label">Reachable</div></div>
      <div class="card"><div class="num">${fleet.total_running}<span class="muted small">/${fleet.total_containers}</span></div><div class="label">Containers running</div></div>`;
    table.innerHTML = endpoints.length ? `<table>
      <thead><tr><th>Name</th><th>URL</th><th>Sort</th><th>Enabled</th><th>Status</th><th>Containers</th><th></th></tr></thead>
      <tbody>
        ${endpoints.map((e) => {
          const live = (fleet.endpoints || []).find((f) => f.endpoint_id === e.id);
          return `<tr>
           <td><strong>${esc(e.name)}</strong><div class="small muted">${esc(e.kind || "docker")}</div></td>
           <td class="mono small muted">${esc(e.url)}</td>
           <td class="mono small muted">${e.sort_order ?? 0}</td>
           <td>${e.enabled ? "yes" : "no"}</td>
            <td>${live ? (live.available ? `<span class="badge badge-active">ok</span>` : `<span class="badge badge-failed" title="${esc(live.reason || "")}">down</span>`) : `<span class="muted">—</span>`}</td>
            <td>${live ? `${live.containers_running}/${live.containers_total}` : "—"}</td>
            <td>
              <button class="link" data-action="edit-endpoint" data-id="${e.id}">edit</button>
              <button class="link" data-action="view-endpoint" data-id="${e.id}">view</button>
              <button class="link" data-action="test-endpoint" data-id="${e.id}">test</button>
              <button class="link" data-action="discover-endpoint" data-id="${e.id}">discover</button>
              <button class="link" data-action="toggle-endpoint" data-id="${e.id}">${e.enabled ? "disable" : "enable"}</button>
              <button class="link danger" data-action="delete-endpoint" data-id="${e.id}">delete</button>
            </td>
          </tr>`;
        }).join("")}
      </tbody></table>` : `<div class="muted">No remote endpoints yet. Click "Add endpoint". The local engine always shows in the picker.</div>`;
  } catch (err) {
    table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
  await refreshLinks();
}

// ------------------------------------------------------------------ discovered hotlinks
export async function refreshLinks() {
  const table = $("#links-table");
  if (!table) return;
  let links;
  try {
    links = await api("/api/links");
  } catch (err) {
    table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
    return;
  }
  state.links = links;
  if (!links.length) {
    table.innerHTML = `<div class="muted">No published ports discovered yet. Run "discover" on an endpoint (or "test" it) — hotlinks land here and can be promoted into gateway routes.</div>`;
    return;
  }
  const active = links.filter((l) => !l.stale);
  const stale = links.length - active.length;
  table.innerHTML = `<table>
    <thead><tr><th>Link</th><th>Endpoint</th><th>Container</th><th>Port</th><th>Label</th><th>State</th><th></th></tr></thead>
    <tbody>
      ${links.map((l) => {
        const mapped = l.gateway_route_id != null
          ? `<span class="badge badge-active" title="gateway route #${l.gateway_route_id}">mapped</span>`
          : `<button class="link" data-action="promote-link" data-id="${l.id}">map to gateway</button>`;
        return `<tr>
          <td><a href="${esc(l.url)}" target="_blank" rel="noopener" class="mono">${esc(l.url)}</a></td>
          <td class="muted">${esc(l.endpoint_name || l.endpoint_id)}</td>
          <td class="mono small">${esc(l.container_name || l.short_id || l.container_id)}</td>
          <td class="mono small muted">${esc(l.container_port || "")}</td>
          <td>${esc(l.label || "—")}</td>
          <td>${l.stale ? `<span class="badge badge-failed">stale</span>` : l.enabled ? `<span class="badge badge-active">live</span>` : `<span class="badge">disabled</span>`}${l.manual ? ` <span class="badge badge-planned" title="hand-tuned, safe from resyncs">manual</span>` : ""}</td>
          <td>
            ${mapped}
            <button class="link" data-action="edit-link" data-id="${l.id}">edit</button>
            <button class="link danger" data-action="delete-link" data-id="${l.id}">delete</button>
          </td>
        </tr>`;
      }).join("")}
    </tbody></table>
    ${stale ? `<div class="small muted">${stale} stale link(s) — their containers were not seen on the last scan. Stale links are kept until you delete them.</div>` : ""}`;
}

export async function discoverEndpoint(id) {
  toast("Scanning containers…");
  try {
    const res = await api(`/api/endpoints/${id}/discover`, { method: "POST" });
    toast(`${res.endpoint_name}: ${res.discovered} new link(s), ${res.total} total, ${res.stale} stale`);
  } catch (err) {
    toast(err.message, "error");
  }
  loadFleet();
}

export async function editLink(id) {
  const links = state.links || [];
  const link = links.find((l) => l.id === Number(id)) || await api(`/api/links/${id}`);
  openForm(`Edit link — ${esc(link.url)}`, [
    { name: "label", label: "Alias", value: link.label || "" },
    { name: "scheme", label: "Scheme", type: "select", value: link.scheme, options: [{ value: "http", label: "http" }, { value: "https", label: "https" }] },
    { name: "host", label: "Host (overrides discovered host)", value: link.host || "" },
    { name: "sort_order", label: "Sort order", type: "number", value: link.sort_order ?? 0 },
    { name: "enabled", label: "Enabled", type: "select", value: link.enabled ? "true" : "false", options: [{ value: "true", label: "yes" }, { value: "false", label: "no" }] },
  ], async (data) => {
    const payload = {
      label: data.label,
      scheme: data.scheme,
      host: data.host,
      sort_order: Number(data.sort_order),
      enabled: data.enabled === "true",
    };
    await api(`/api/links/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    refreshLinks();
  });
}

export async function deleteLink(id) {
  if (!confirm("Delete this discovered link?")) return;
  try {
    await api(`/api/links/${id}`, { method: "DELETE" });
    toast("Link deleted");
    refreshLinks();
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function promoteLink(id) {
  const links = state.links || [];
  const link = links.find((l) => l.id === Number(id)) || await api(`/api/links/${id}`);
  let gateways = [];
  try {
    gateways = await api("/api/gateway");
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  if (!gateways.length) {
    toast("Create a gateway first (Gateways tab)", "error");
    return;
  }
  openForm(`Map ${esc(link.url)} to gateway`, [
    { name: "gateway_id", label: "Gateway", type: "select", options: gateways.map((g) => ({ value: g.id, label: g.name })) },
    { name: "path", label: "Path prefix", value: "/*" },
    { name: "strip_prefix", label: "Strip prefix", type: "select", value: "false", options: [{ value: "true", label: "yes" }, { value: "false", label: "no" }] },
    { name: "auth_mode", label: "Auth mode", type: "select", value: "open", options: ["open", "api_key"] },
    { name: "rate_limit_rpm", label: "Rate limit (req/min, 0 = none)", type: "number", value: 0 },
  ], async (data) => {
    const res = await api(`/api/links/${id}/map-to-gateway`, {
      method: "POST",
      body: JSON.stringify({
        gateway_id: Number(data.gateway_id),
        path: data.path || "/*",
        strip_prefix: data.strip_prefix === "true",
        auth_mode: data.auth_mode,
        rate_limit_rpm: Number(data.rate_limit_rpm || 0),
      }),
    });
    toast(`Mapped → /gw/${res.gateway_slug} (route #${res.route_id})`);
    refreshLinks();
  });
}

registerTabHandler("fleet", loadFleet);

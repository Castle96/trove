/**
 * fleet.js — Fleet endpoints tab (endpoint management + fleet overview cards).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { state, $, esc, api, registerTabHandler } from "./core.js";

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
              <button class="link" data-action="toggle-endpoint" data-id="${e.id}">${e.enabled ? "disable" : "enable"}</button>
              <button class="link danger" data-action="delete-endpoint" data-id="${e.id}">delete</button>
            </td>
          </tr>`;
        }).join("")}
      </tbody></table>` : `<div class="muted">No remote endpoints yet. Click "Add endpoint". The local engine always shows in the picker.</div>`;
  } catch (err) {
    table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

registerTabHandler("fleet", loadFleet);

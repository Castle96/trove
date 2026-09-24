/**
 * swarm.js — Projects / Agents / Notifications (agent swarm tab).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import {
  state,
  $,
  esc,
  fmtDate,
  stateBadge,
  api,
  toast,
  openForm,
  registerTabHandler,
} from "./core.js";

export async function loadProjectsTab() {
  await Promise.all([loadSwarmOverview(true), loadProjects(), loadAgents(), loadNotifications()]);
}

export async function loadSwarmOverview(quiet = false) {
  try {
    const overview = await api("/api/swarm/overview");
    state.swarmOverview = overview;
    const count = Number(overview.projects_needing_approval || 0);
    const badge = $("#approval-badge");
    if (badge) {
      if (count > 0) {
        badge.textContent = `${count} approval${count === 1 ? "" : "s"}`;
        badge.classList.remove("hidden");
      } else {
        badge.textContent = "";
        badge.classList.add("hidden");
      }
    }
    const cards = $("#swarm-cards");
    if (cards) {
      cards.innerHTML = `
        <div class="card"><div class="num">${overview.agents_total ?? 0}</div><div class="label">Agents</div></div>
        <div class="card"><div class="num">${overview.agents_working ?? 0}</div><div class="label">Working</div></div>
        <div class="card"><div class="num">${overview.projects_active ?? 0}</div><div class="label">Active projects</div></div>
        <div class="card"><div class="num">${count}</div><div class="label">Needs approval</div></div>
        <div class="card"><div class="num">${overview.notifications_unread ?? 0}</div><div class="label">Unread</div></div>`;
    }
    if (count > 0 && !quiet && count > state._lastApprovalCount) {
      toast(`${count} project${count === 1 ? "" : "s"} need approval`, "error");
    }
    state._lastApprovalCount = count;
  } catch (err) {
    if (!quiet) toast(err.message, "error");
  }
}

export async function loadProjects() {
  const table = $("#projects-table");
  if (table) table.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    state.projects = await api("/api/swarm/projects?limit=100");
    renderProjects();
  } catch (err) {
    if (table) table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

export async function loadAgents() {
  const table = $("#agents-table");
  if (table) table.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    state.agents = await api("/api/swarm");
    renderAgents();
  } catch (err) {
    if (table) table.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

export async function loadNotifications() {
  const list = $("#notifications-list");
  if (list) list.innerHTML = `<div class="muted">Loading…</div>`;
  try {
    state.notifications = await api("/api/swarm/notifications?unread_only=false&limit=50");
    renderNotifications();
  } catch (err) {
    if (list) list.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

function renderProjects() {
  const table = $("#projects-table");
  if (!table) return;
  const projects = state.projects || [];
  if (!projects.length) {
    table.innerHTML = `<div class="muted">No projects yet. Click "New project".</div>`;
    return;
  }
  table.innerHTML = `<table>
    <thead><tr><th>Project</th><th>Agent</th><th>Endpoint</th><th>Status</th><th>Needs approval</th><th>Updated</th><th></th></tr></thead>
    <tbody>
      ${projects
        .map(
          (p) => `<tr>
            <td><strong>${esc(p.name)}</strong>${p.approval_note ? `<div class="small muted">${esc(p.approval_note)}</div>` : ""}</td>
            <td class="muted">${esc(p.agent_name || "—")}</td>
            <td class="muted">${esc(p.endpoint_name || "—")}</td>
            <td>${stateBadge(p.status)}</td>
            <td>${p.needs_approval ? `<span class="badge badge-failed">yes</span>` : `<span class="muted">no</span>`}</td>
            <td class="muted small">${esc(fmtDate(p.updated_at))}</td>
            <td>
              <button class="link" data-action="approve-project" data-id="${p.id}">Approve</button>
              <button class="link" data-action="request-approval" data-id="${p.id}">Request approval</button>
              <button class="link" data-action="view-project" data-id="${p.id}">View</button>
            </td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

function renderAgents() {
  const table = $("#agents-table");
  if (!table) return;
  const agents = state.agents || [];
  if (!agents.length) {
    table.innerHTML = `<div class="muted">No agents yet. Click "New agent".</div>`;
    return;
  }
  const endpointName = (id) => {
    if (id === null || id === undefined) return "—";
    return state.endpoints.find((e) => e.id === id)?.name || `#${id}`;
  };
  const projectName = (id) => {
    if (id === null || id === undefined) return "—";
    return state.projects.find((p) => p.id === id)?.name || `#${id}`;
  };
  table.innerHTML = `<table>
    <thead><tr><th>Agent</th><th>Kind</th><th>Status</th><th>Current project</th><th>Last heartbeat</th><th>Endpoint</th></tr></thead>
    <tbody>
      ${agents
        .map(
          (a) => `<tr>
            <td><strong>${esc(a.name)}</strong></td>
            <td class="muted">${esc(a.kind || "—")}</td>
            <td>${stateBadge(a.status)}</td>
            <td class="muted">${esc(projectName(a.current_project_id))}</td>
            <td class="muted small">${esc(fmtDate(a.last_heartbeat))}</td>
            <td class="muted">${esc(endpointName(a.endpoint_id))}</td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

function renderNotifications() {
  const list = $("#notifications-list");
  if (!list) return;
  const notes = state.notifications || [];
  if (!notes.length) {
    list.innerHTML = `<div class="muted">No notifications.</div>`;
    return;
  }
  list.innerHTML = notes
    .map(
      (n) => `<div class="activity-item">
        <span class="activity-time">${esc(fmtDate(n.created_at))}</span>
        <span class="activity-actor">${esc(n.level)}</span>
        <span class="activity-action"><strong>${esc(n.title)}</strong>${n.body ? ` — ${esc(n.body)}` : ""}</span>
        ${n.read ? `<span class="muted small">read</span>` : `<button class="link" data-action="mark-notification-read" data-id="${n.id}">Mark read</button>`}
      </div>`
    )
    .join("");
}

export async function approveProject(id, done = false) {
  try {
    await api(`/api/swarm/projects/${id}/approve${done ? "?done=true" : ""}`, { method: "POST" });
    toast("Project approved");
    await Promise.all([loadSwarmOverview(true), loadProjects(), loadNotifications()]);
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function requestProjectApproval(id, note) {
  try {
    await api(`/api/swarm/projects/${id}/request-approval`, {
      method: "POST",
      body: JSON.stringify({ note: note || null }),
    });
    toast("Approval requested");
    await Promise.all([loadSwarmOverview(true), loadProjects(), loadNotifications()]);
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function viewProject(id) {
  try {
    const data = await api(`/api/swarm/projects/${id}/activity`);
    const p = data.project || {};
    const notes = data.notifications || [];
    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    dialog.innerHTML = `
      <div class="modal-head">
        <h3>${esc(p.name || `Project ${id}`)}</h3>
        <button class="btn-close">&times;</button>
      </div>
      <div>
        <div class="small muted">Status ${esc(p.status || "—")} &middot; Agent ${esc(p.agent_name || "—")} &middot; Endpoint ${esc(p.endpoint_name || "—")}</div>
        ${p.description ? `<p>${esc(p.description)}</p>` : ""}
        ${p.approval_note ? `<p class="small">Approval note: ${esc(p.approval_note)}</p>` : ""}
        <h3 class="monitor-sub">Notifications (${notes.length})</h3>
        ${notes.length ? notes.map((n) => `<div class="activity-item"><span class="activity-actor">${esc(n.level)}</span><span class="activity-target"><strong>${esc(n.title)}</strong>${n.body ? ` — ${esc(n.body)}` : ""}</span></div>`).join("") : `<div class="muted">No notifications.</div>`}
      </div>`;
    dialog.querySelector(".btn-close").onclick = () => dialog.close();
    document.body.appendChild(dialog);
    dialog.showModal();
    dialog.addEventListener("close", () => dialog.remove());
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function markNotificationRead(id) {
  try {
    await api(`/api/swarm/notifications/${id}/read`, { method: "POST" });
    toast("Marked read");
    await Promise.all([loadSwarmOverview(true), loadNotifications()]);
  } catch (err) {
    toast(err.message, "error");
  }
}

registerTabHandler("projects", loadProjectsTab);
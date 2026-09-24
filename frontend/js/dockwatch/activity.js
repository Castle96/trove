/**
 * activity.js — the Activity log tab renderer.
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { $, api, esc, registerTabHandler } from "./core.js";

export async function refreshActivity(target, limit = 50) {
  try {
    const events = await api(`/api/activity?limit=${limit}`);
    const dest = target || $("#activity-list");
    if (!events.length) {
      dest.innerHTML = `<div class="muted">No activity yet — actions performed in this dashboard will show up here.</div>`;
      return;
    }
    dest.innerHTML = events
      .map(
        (e) => `<div class="activity-item">
          <span class="activity-time">${new Date(e.timestamp).toLocaleString()}</span>
          <span class="activity-actor">${esc(e.actor_type)}</span>
          <span class="activity-action">${esc(e.action)}</span>
          <span class="activity-target">${esc(e.target)}${e.detail ? ` — ${esc(e.detail)}` : ""}</span>
        </div>`
      )
      .join("");
  } catch (err) {
    const dest = target || $("#activity-list");
    dest.innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

registerTabHandler("activity", refreshActivity);
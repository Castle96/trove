/**
 * search.js — Global search (devices / IPs / sites / racks / containers).
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { $, esc, api, state, activateTab } from "./core.js";

export function bindSearch() {
  const input = $("#global-search");
  const results = $("#search-results");
  let timer;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      results.classList.add("hidden");
      return;
    }
    timer = setTimeout(async () => {
      const parts = [];
      try {
        const data = await api(`/api/inventory/search?q=${encodeURIComponent(q)}`);
        const rows = [
          ...data.devices.map((d) => ({ group: "Devices", label: d.name, sub: d.device_type, tab: "inventory", id: d.id })),
          ...data.ip_addresses.map((ip) => ({ group: "IPs", label: ip.address, sub: ip.device_name || ip.status, tab: "ips" })),
          ...data.sites.map((s) => ({ group: "Sites", label: s.name, sub: s.slug, tab: "inventory" })),
          ...data.racks.map((r) => ({ group: "Racks", label: r.name, sub: r.site_name, tab: "inventory" })),
        ];
        parts.push(...rows);
      } catch { /* ignore */ }

      try {
        if (state.dockerStatus && state.dockerStatus.available) {
          const containers = await api("/api/docker/containers?include_stats=false").catch(() => []);
          parts.push(
            ...containers
              .filter((c) => c.name.toLowerCase().includes(q.toLowerCase()))
              .map((c) => ({ group: "Containers", label: c.name, sub: c.image, tab: "containers" }))
          );
        }
      } catch { /* ignore */ }

      if (!parts.length) {
        results.innerHTML = `<div class="empty">No matches for “${esc(q)}”.</div>`;
      } else {
        const grouped = {};
        for (const p of parts) (grouped[p.group] ||= []).push(p);
        results.innerHTML = Object.entries(grouped)
          .map(
            ([group, items]) =>
              `<div class="sr-group">${group}</div>` +
              items
                .map(
                  (i) => `<div class="sr-item" data-tab="${i.tab}" data-id="${i.id || ""}">
                    <span><strong>${esc(i.label)}</strong> <span class="muted">${esc(i.sub || "")}</span></span>
                  </div>`
                )
                .join("")
          )
          .join("");
      }
      results.classList.remove("hidden");
    }, 280);
  });

  results.addEventListener("click", (ev) => {
    const item = ev.target.closest(".sr-item");
    if (!item) return;
    results.classList.add("hidden");
    input.value = "";
    activateTab(item.dataset.tab);
    if (item.dataset.id) {
      setTimeout(() => {
        const row = $(`#device-row-${item.dataset.id}`);
        if (row) {
          row.scrollIntoView({ behavior: "smooth", block: "center" });
          row.style.outline = "2px solid var(--accent)";
          setTimeout(() => (row.style.outline = ""), 1800);
        }
      }, 200);
    }
  });

  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".search-wrap")) results.classList.add("hidden");
  });
}
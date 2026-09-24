/**
 * inventory.js — Inventory tab (sites / racks / devices) and IP addresses tab.
 * Ported from Dockwatch into Trove (frontend/js/dockwatch/).
 */
import { state, $, esc, stateBadge, api, registerTabHandler } from "./core.js";

export async function loadInventory() {
  await Promise.all([loadSites(), loadRacks(), loadDevices()]);
  const inv = state.inventoryOverview || { sites: 0, racks: 0, devices: 0, ip_addresses: 0 };
  $("#inventory-cards").innerHTML = `
    <div class="card"><div class="num">${inv.sites}</div><div class="label">Sites</div></div>
    <div class="card"><div class="num">${inv.racks}</div><div class="label">Racks</div></div>
    <div class="card"><div class="num">${inv.devices}</div><div class="label">Devices</div></div>
    <div class="card"><div class="num">${inv.ip_addresses}</div><div class="label">IP addresses</div></div>`;
}

async function loadSites() {
  try {
    state.sites = await api("/api/inventory/sites");
    $("#sites-table").innerHTML = renderSites(state.sites);
  } catch (err) {
    $("#sites-table").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

async function loadRacks() {
  try {
    state.racks = await api("/api/inventory/racks");
    $("#racks-table").innerHTML = renderRacks(state.racks);
  } catch (err) {
    $("#racks-table").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

async function loadDevices() {
  try {
    state.devices = await api("/api/inventory/devices");
    $("#devices-table").innerHTML = renderDevices(state.devices);
  } catch (err) {
    $("#devices-table").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

export async function loadIps() {
  try {
    state.ips = await api("/api/inventory/ip-addresses");
    $("#ips-table").innerHTML = renderIps(state.ips);
    const inv = await api("/api/inventory/overview");
    state.inventoryOverview = { ...(state.inventoryOverview || {}), ...inv };
  } catch (err) {
    $("#ips-table").innerHTML = `<div class="muted">${esc(err.message)}</div>`;
  }
}

function renderSites(sites) {
  if (!sites.length) return `<div class="muted">No sites yet. Click "Add site".</div>`;
  return `<table>
    <thead><tr><th>Name</th><th>Slug</th><th>Status</th><th>Facility</th><th>Racks</th><th>Devices</th><th></th></tr></thead>
    <tbody>
      ${sites
        .map(
          (s) => `<tr>
            <td><strong>${esc(s.name)}</strong></td>
            <td class="mono muted">${esc(s.slug)}</td>
            <td>${stateBadge(s.status)}</td>
            <td class="muted">${esc(s.facility || "")}</td>
            <td>${s.rack_count}</td><td>${s.device_count}</td>
            <td><button class="link" data-action="edit-site" data-id="${s.id}">edit</button>
            <button class="link danger" data-action="delete-site" data-id="${s.id}">delete</button></td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

function renderRacks(racks) {
  if (!racks.length) return `<div class="muted">No racks yet. Click "Add rack".</div>`;
  const siteName = (id) => state.sites.find((s) => s.id === id)?.name || "?";
  return `<table>
    <thead><tr><th>Name</th><th>Site</th><th>Status</th><th>U height</th><th>Devices</th><th></th></tr></thead>
    <tbody>
      ${racks
        .map(
          (r) => `<tr>
            <td><strong>${esc(r.name)}</strong> ${r.position ? `<span class="muted small">pos ${r.position}</span>` : ""}</td>
            <td>${esc(siteName(r.site_id))}</td>
            <td>${stateBadge(r.status)}</td>
            <td>${r.u_height}U</td><td>${r.device_count}</td>
            <td><button class="link" data-action="edit-rack" data-id="${r.id}">edit</button>
            <button class="link danger" data-action="delete-rack" data-id="${r.id}">delete</button></td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

function renderDevices(devices) {
  if (!devices.length) return `<div class="muted">No devices yet. Click "Add device".</div>`;
  return `<table>
    <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Site</th><th>Rack</th><th>Serial</th><th>IPs</th><th></th></tr></thead>
    <tbody>
      ${devices
        .map(
          (d) => `<tr id="device-row-${d.id}">
            <td><strong>${esc(d.name)}</strong></td>
            <td class="muted">${esc(d.device_type)}</td>
            <td>${stateBadge(d.status)}</td>
            <td>${esc(d.site_name)}</td>
            <td class="muted">${esc(d.rack_name || "")}</td>
            <td class="mono muted">${esc(d.serial || "")}</td>
            <td>${d.ip_count}</td>
            <td><button class="link" data-action="edit-device" data-id="${d.id}">edit</button>
            <button class="link danger" data-action="delete-device" data-id="${d.id}">delete</button></td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

function renderIps(ips) {
  if (!ips.length) return `<div class="muted">No IP addresses yet. Click "Add IP".</div>`;
  return `<table>
    <thead><tr><th>Address</th><th>Status</th><th>DNS name</th><th>Device</th><th>Interface</th><th></th></tr></thead>
    <tbody>
      ${ips
        .map(
          (ip) => `<tr>
            <td class="mono"><strong>${esc(ip.address)}</strong></td>
            <td>${stateBadge(ip.status)}</td>
            <td class="muted">${esc(ip.dns_name || "")}</td>
            <td>${esc(ip.device_name || "")}</td>
            <td class="muted">${esc(ip.interface || "")}</td>
            <td><button class="link" data-action="edit-ip" data-id="${ip.id}">edit</button>
            <button class="link danger" data-action="delete-ip" data-id="${ip.id}">delete</button></td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

registerTabHandler("inventory", loadInventory);
registerTabHandler("ips", loadIps);
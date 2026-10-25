// VanRakshak live map: current NASA FIRMS fires, user-marked incidents, per-incident report / factory / air quality.
import { api, esc, html, raw, toast, fmt, download, state } from './core.js';
import { config } from './map.js';
import * as FIRE from './fire.js';
import * as FOREST from './forest.js';
import * as INFRA from './infra.js';
import * as CASES from './cases.js';
import { pop } from './progress.js';
import { followCall } from './escalate.js';
import * as FACTORY from './factory.js';
import * as SEARCH from './search.js';

const INDIA = [[6.5, 68.1], [37.1, 97.5]];
const KIND = { cutting: ['Illegal cutting', '🪓', '#a8431f'], fire: ['Forest fire', '🔥', '#d9382a'], encroachment: ['Encroachment', '⛺', '#7a5a1f'], pollution: ['Industrial pollution', '🏭', '#5b4b8a'], other: ['Other', '📍', '#37474f'] };
const STATUS = { DETECTED: ['Newly reported', 'amber'], NEEDS_REVIEW: ['Under review', 'amber'], APPROVED_FOR_INSPECTION: ['Approved for inspection', 'green'], ASSIGNED: ['Assigned', 'green'], DISPATCHED: ['Responder notified', 'green'], ACKNOWLEDGED: ['Acknowledged', 'green'], FIELD_VERIFICATION_PENDING: ['Field check under way', 'green'], CONFIRMED: ['Confirmed', 'red'], UNCONFIRMED: ['Not confirmed', ''], DISMISSED: ['Dismissed', ''], CLOSED: ['Closed', ''] };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const ago = (t) => { const m = Math.max(0, Math.round((Date.now() - t) / 60000)); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
const kindOf = (i) => KIND[i.evidence?.kind] || (i.change_id ? ['Satellite change', '🛰', '#bf731f'] : i.wildfire_id ? KIND.fire : KIND.other);
const km = (a, b) => { const p = Math.PI / 180, h = Math.sin(((b[0] - a[0]) * p) / 2) ** 2 + Math.cos(a[0] * p) * Math.cos(b[0] * p) * Math.sin(((b[1] - a[1]) * p) / 2) ** 2; return 12742 * Math.asin(Math.sqrt(h)); };

// ------------------------------------------------------------------ map
const map = L.map('map', { zoomControl: false, preferCanvas: true, minZoom: 3, worldCopyJump: true });
L.control.zoom({ position: 'bottomright' }).addTo(map);
const wide = innerWidth > 900;
map.fitBounds(INDIA, { paddingTopLeft: [wide ? 360 : 10, 70], paddingBottomRight: [wide ? 380 : 10, 20] });
const bases = {
  map: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }),
  terrain: L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', { maxZoom: 17, subdomains: 'abc', attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Style &copy; OpenTopoMap (CC-BY-SA)' }),
};
let currentBase = bases.map.addTo(map);
config().then((cfg) => {
  if (cfg.mapbox_public_token) {  // cleaner, English-labelled, forest-friendly base map
    const outdoors = L.tileLayer(`https://api.mapbox.com/styles/v1/mapbox/outdoors-v12/tiles/256/{z}/{x}/{y}@2x?access_token=${cfg.mapbox_public_token}`, { maxZoom: 19, attribution: '&copy; Mapbox &copy; OpenStreetMap contributors' });
    let bad = 0;   // if Mapbox refuses the tiles, fall back to the free OpenStreetMap layer instead of leaving a blank map
    outdoors.on('tileerror', () => { if (++bad === 4 && currentBase === outdoors) { map.removeLayer(outdoors); bases.map = osmBase; currentBase = osmBase.addTo(map); currentBase.bringToBack(); } });
    const osmBase = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' });
    const wasMap = currentBase === bases.map; bases.map = outdoors;
    if (wasMap) { map.removeLayer(currentBase); currentBase = outdoors.addTo(map); currentBase.bringToBack(); }
  }
  bases.sat = cfg.mapbox_public_token
    ? L.tileLayer(`https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=${cfg.mapbox_public_token}`, { maxZoom: 19, attribution: '&copy; Mapbox &copy; OpenStreetMap contributors &copy; Maxar' })
    : L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics' });
});
$('#views').onclick = (e) => {
  const v = e.target.dataset.v; if (!v || !bases[v]) return;
  map.removeLayer(currentBase); currentBase = bases[v].addTo(map); currentBase.bringToBack();
  $$('#views button').forEach((b) => b.classList.toggle('on', b === e.target));
  document.body.classList.toggle('dark-map', v === 'sat');
};

// ------------------------------------------------------------------ India outline
api('/live/boundary').then((f) => L.geoJSON(f, { style: { color: '#14573c', weight: 1.6, fill: false, opacity: 0.8 }, interactive: false }).addTo(map)).catch(() => {});

// ------------------------------------------------------------------ fires
let fires = [], fireDays = 1, fireMeta = null;
const fireRenderer = L.canvas({ padding: 0.4 });
const fireLayer = L.layerGroup().addTo(map), heatLayer = L.layerGroup().addTo(map);
const colorFor = (t) => { const h = (Date.now() / 1000 - t) / 3600; return h < 6 ? '#e5252a' : h < 24 ? '#f08a1f' : '#f4cf3d'; };
const placeCache = {};
async function placeName(lat, lon) {
  const k = `${lat.toFixed(2)},${lon.toFixed(2)}`;
  if (placeCache[k] !== undefined) return placeCache[k];
  try {
    const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&lat=${lat}&lon=${lon}`, { headers: { Accept: 'application/json' } });
    const a = (await r.json()).address || {};
    placeCache[k] = [a.city || a.town || a.village || a.county || a.state_district, a.state].filter(Boolean).join(', ') || null;
  } catch { placeCache[k] = null; }
  return placeCache[k];
}
function firePopup(f, place) {
  const [lat, lon, t, conf, frp, sat, dn] = f;
  const h = (Date.now() / 1000 - t) / 3600, cur = h <= 3 ? ['CURRENT', '#14573c'] : h <= 24 ? ['DELAYED', '#bf731f'] : ['HISTORICAL', '#6b7d73'];
  const fetched = fireMeta ? new Date(fireMeta.fetched_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '?';
  return html`<b>Satellite thermal hotspot</b> <span style="color:${cur[1]};font-weight:800;font-size:11px">${cur[0]}</span><br>${place ? raw(`Near ${esc(place)}<br>`) : ''}${lat.toFixed(4)}, ${lon.toFixed(4)}<br>Detected ${new Date(t * 1000).toLocaleString()} (${ago(t * 1000)})<br>${fireMeta ? esc(fireMeta.source_name) : ''}${sat ? ' · satellite ' + esc(sat) : ''}${dn ? ' · ' + (dn === 'D' ? 'day' : 'night') : ''}<br>${frp != null ? `Fire power ${frp} MW` : 'Fire power not given'}${conf != null ? ` · source confidence ${Math.round(conf * 100)}%` : ''}<br>Feed updated ${fetched}<br><span style="color:#6b7d73">Not a confirmed fire: could also be crop burning, a kiln or industrial heat.</span><br><button class="btn primary sm" data-inv style="margin-top:8px;width:100%">Investigate as fire incident</button>`;
}
function bindPop(pop, f) { const b = pop.getElement()?.querySelector('[data-inv]'); if (b) b.onclick = () => { pop.close(); FIRE.investigate(f); }; }
function openFire(f, zoom = 11) {
  map.setView([f[0], f[1]], Math.max(map.getZoom(), zoom));
  const pop = L.popup({ offset: [0, -4], minWidth: 250 }).setLatLng([f[0], f[1]]).setContent(String(firePopup(f, null))).openOn(map);
  bindPop(pop, f);
  placeName(f[0], f[1]).then((p) => { if (p && pop.isOpen()) { pop.setContent(String(firePopup(f, p))); bindPop(pop, f); } });
}
function drawFires() {
  fireLayer.clearLayers(); heatLayer.clearLayers();
  if ($('#showHeat').checked && L.heatLayer && fires.length) {
    const mx = Math.max(...fires.map((f) => f[4] ?? 5), 1);
    L.heatLayer(fires.map((f) => [f[0], f[1], Math.min(1, 0.25 + (f[4] ?? 5) / mx)]), { radius: 26, blur: 22, maxZoom: 9, minOpacity: 0.35, gradient: { 0.2: '#ffd54a', 0.5: '#f08a1f', 0.8: '#e5252a', 1: '#8b0000' } }).addTo(heatLayer);
  }
  if (!$('#showFires').checked) return;
  fires.forEach((f) => {
    const frp = f[4] ?? 5;
    L.circleMarker([f[0], f[1]], { renderer: fireRenderer, radius: Math.min(9, 3 + Math.sqrt(frp) * 0.55), color: '#fff', weight: 0.6, fillColor: colorFor(f[2]), fillOpacity: 0.85 }).on('click', () => openFire(f, map.getZoom())).addTo(fireLayer);
  });
}
function paintFirePanel() {
  $('#fireCount').textContent = fires.length.toLocaleString();
  $('#fireLabel').textContent = `hotspots, last ${fireDays * 24} h`;
  const when = fireMeta ? new Date(fireMeta.fetched_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  $('#fireSub').textContent = fireMeta ? `${fireMeta.source_name} · updated ${when}${fireMeta.stale ? ' (old copy)' : ''}` : '';
  const top = [...fires].sort((a, b) => (b[4] ?? -1) - (a[4] ?? -1)).slice(0, 15);
  $('#fireList').innerHTML = top.length ? html`${top.map((f, i) => raw(`<button class="row-item" data-i="${i}"><span class="dot" style="background:${colorFor(f[2])}"></span><span class="grow"><b>${f[4] != null ? esc(f[4]) + ' MW' : 'Hotspot'}</b><small>${f[0].toFixed(2)}, ${f[1].toFixed(2)} · ${esc(ago(f[2] * 1000))}</small></span></button>`))}` : html`<div class="empty"><b>No hotspots</b>None detected in this window.</div>`;
  $$('#fireList .row-item').forEach((b) => (b.onclick = () => openFire(top[+b.dataset.i])));
  paintIncidentsIfListing();
}
async function loadFires(force = false) {
  $('#fireSub').innerHTML = '<span class="spin"></span>Loading NASA data…';
  try {
    const d = await api(`/live/fires?days=${fireDays}&source=${$('#src').value}${force ? '&force=true' : ''}`);
    fires = d.fires; fireMeta = d; drawFires(); paintFirePanel();
    $('#fireNote').textContent = '';
    if (d.warning) toast(d.warning, 'error');
  } catch (e) {
    fires = []; fireMeta = null; drawFires(); $('#fireCount').textContent = '–'; $('#fireList').innerHTML = html`<div class="empty"><b>Live fire data is unavailable</b>${e.message}</div>`; $('#fireSub').textContent = 'Not connected to NASA FIRMS';
  }
}
$('#range').onclick = (e) => { const d = +e.target.dataset.d; if (!d) return; fireDays = d; $$('#range button').forEach((b) => b.classList.toggle('on', b === e.target)); loadFires(); };
const anyFireLayer = () => $('#showFires').checked || $('#showHeat').checked;
async function syncFireLayers() {
  $('#fireInfo').hidden = !anyFireLayer();
  if (anyFireLayer() && !fireMeta) await loadFires(); else drawFires();
}
$('#showFires').onchange = syncFireLayers; $('#showHeat').onchange = syncFireLayers;
$('#showInfra').onchange = (e) => window.dispatchEvent(new CustomEvent('rk:infra-layer', { detail: { on: e.target.checked } }));
$('#showCover').onchange = (e) => window.dispatchEvent(new CustomEvent('rk:layers', { detail: { cover: e.target.checked } }));
$('#refresh').onclick = () => loadFires(true); $('#src').onchange = () => loadFires();
setInterval(() => { if (anyFireLayer()) loadFires(); }, 10 * 60 * 1000);

// ------------------------------------------------------------------ incidents
let incidents = [], selected = null, marking = false, draftMarker = null, draftLatLng = null;
const pinLayer = L.layerGroup().addTo(map), shapeLayer = L.layerGroup().addTo(map), pins = {};
const center = (g) => (g.type === 'Point' ? [g.coordinates[1], g.coordinates[0]] : (() => { const c = L.geoJSON(g).getBounds().getCenter(); return [c.lat, c.lng]; })());
const pinIcon = (i, sel) => { const [, emoji, col] = kindOf(i); return L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: `<div class="pin ${sel ? 'sel' : ''}" style="background:${col}"><span>${emoji}</span></div>` }); };

function drawPins() {
  pinLayer.clearLayers(); shapeLayer.clearLayers();
  incidents.filter((i) => i.id === selected).forEach((i) => {
    const m = L.marker(center(i.geometry), { icon: pinIcon(i, i.id === selected), title: i.title, keyboard: true }).on('click', () => showDetail(i.id)).addTo(pinLayer);
    pins[i.id] = m;
    if (i.geometry.type !== 'Point') L.geoJSON(i.geometry, { style: { color: '#bf731f', weight: 2, fillOpacity: 0.12 }, interactive: false }).addTo(shapeLayer);
  });
}
async function loadIncidents() { incidents = await api('/incidents').catch(() => incidents); drawPins(); }
const paintIncidentsIfListing = () => { if (!selected && !marking && $('#rightBody .list')) paintList(); };

function paintList() {
  CASES.clear(); FACTORY.clear();
  const open = incidents.filter((i) => !['CLOSED', 'DISMISSED'].includes(i.status));
  $('#rightBody').innerHTML = html`<div class="side-head"><div><h2>Incidents</h2><div class="sub">${open.length} report${open.length === 1 ? '' : 's'} · pick one to see it on the map</div></div></div>
    <button class="btn primary block" id="mark" style="margin:14px 0 10px">＋ Mark an incident</button>
    <div class="list">${open.length ? open.map((i) => { const [kn, em] = kindOf(i), [st, c] = STATUS[i.status] || [i.status, '']; return raw(`<button class="row-item" data-id="${esc(i.id)}"><span style="font-size:20px">${em}</span><span class="grow"><b>${esc(i.title)}</b><small>${esc(kn)} · ${esc(ago(new Date(i.created_at).getTime()))}</small></span><span class="chip ${c}">${esc(st)}</span></button>`); }) : raw('<div class="empty"><b>No incidents yet</b>Press “Mark an incident”, then click the place on the map.</div>')}</div>
    <h3 style="margin-top:16px">Factory checks</h3>
    <div id="facEntry"></div><div id="facList" class="list"></div>
    <div id="caseWrap" hidden><h3 style="margin-top:16px">Case studies</h3><div class="sub">What happens when a factory is built beside, or by clearing, a forest.</div>
    <div id="caseList" class="cases"></div></div>
    `;
  CASES.mountList($('#caseList'), (id) => CASES.open(id, paintList)).then((n) => { if (n) $('#caseWrap').hidden = false; });
  FACTORY.mountEntry($('#facEntry'), () => FACTORY.start(paintList));
  FACTORY.mountList($('#facList'), paintList);
  $('#mark').onclick = startMarking;
  $$('#rightBody .row-item').forEach((b) => (b.onclick = () => showDetail(b.dataset.id)));
}

function setHint(text) { const h = $('#hint'); h.hidden = !text; h.innerHTML = text ? `${esc(text)} <button class="btn" id="cancelMark">Cancel</button>` : ''; $('#cancelMark')?.addEventListener('click', stopMarking); }
function startMarking() {
  selected = null; marking = true; document.body.classList.add('mark-mode'); setHint('Click the map to place the incident'); drawPins();
  map.once('click', onPlace);
}
function stopMarking() { marking = false; document.body.classList.remove('mark-mode'); setHint(''); map.off('click', onPlace); if (draftMarker) { map.removeLayer(draftMarker); draftMarker = null; } paintList(); }
function onPlace(e) {
  if (!marking) return; document.body.classList.remove('mark-mode'); setHint(''); draftLatLng = e.latlng;
  draftMarker = L.marker(e.latlng, { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: '<div class="pin" style="background:#14573c"><span>📍</span></div>' }) }).addTo(map);
  $('#rightBody').innerHTML = html`<button class="back" id="cancelNew">‹ Cancel</button><h2>New incident</h2><div class="sub">${e.latlng.lat.toFixed(4)}, ${e.latlng.lng.toFixed(4)}</div>
    <label class="f">What happened?<select id="nk">${Object.entries(KIND).map(([k, [n, em]]) => raw(`<option value="${k}">${em} ${esc(n)}</option>`))}</select></label>
    <label class="f">Short title<input type="text" id="nt" maxlength="120" placeholder="e.g. Trees being cut near river"></label>
    <label class="f">Notes (optional)<textarea id="nn" maxlength="1500" placeholder="What did you see? Who reported it?"></textarea></label>
    <div class="row-item" style="margin-top:12px;border:0;padding:0"><button class="btn primary grow" id="save">Save incident</button></div><div class="note warn" id="nerr" hidden></div>`;
  $('#nt').focus(); $('#cancelNew').onclick = stopMarking;
  $('#nk').onchange = () => { if (!$('#nt').value) $('#nt').placeholder = ({ cutting: 'Trees being cut', fire: 'Smoke / fire seen', encroachment: 'Illegal structure or clearing', pollution: 'Discharge or smoke from a site', other: 'Describe briefly' })[$('#nk').value]; };
  $('#save').onclick = async () => {
    const title = $('#nt').value.trim() || `${KIND[$('#nk').value][0]} at ${e.latlng.lat.toFixed(3)}, ${e.latlng.lng.toFixed(3)}`;
    $('#save').disabled = true;
    try {
      const inc = await api('/incidents', { method: 'POST', body: { title, geometry: { type: 'Point', coordinates: [+e.latlng.lng.toFixed(6), +e.latlng.lat.toFixed(6)] }, evidence: { claim_type: 'human_report', kind: $('#nk').value, note: $('#nn').value.trim() || null, source: 'Marked on the live map' } } });
      marking = false; map.removeLayer(draftMarker); draftMarker = null; toast('Incident saved'); window.dispatchEvent(new Event('rk:changed'));
      await loadIncidents(); showDetail(inc.id);
    } catch (err) { $('#nerr').hidden = false; $('#nerr').textContent = err.status === 403 ? 'Your role cannot mark incidents (reviewer or administrator needed).' : err.message; $('#save').disabled = false; }
  };
}

// the same two decisions as everywhere else: Escalate or False alarm
function incDecision(i) {
  const role = localStorage.getItem('rk.role') || 'reviewer', can = ['admin', 'reviewer', 'officer'].includes(role), dis = can ? '' : 'disabled';
  const esc_ = i.evidence?.escalation;
  let body;
  if (i.status === 'DISMISSED') body = `<div class="banner2 grey"><b>Marked as a false alarm.</b></div><button class="btn" id="incUndo" ${dis}>↩ Undo, review it again</button>`;
  else if (esc_) body = `<div class="banner2 green"><b>Escalated.</b><div class="sub">Telegram: ${esc(({ SENT: 'sent', FAILED: 'failed', NOT_CONFIGURED: 'not set up' })[esc_.telegram?.status] || '–')} · Phone call: ${esc(({ INITIATED: 'placed', FAILED: 'failed', NOT_CONFIGURED: 'not set up' })[esc_.call?.status] || '–')}</div></div><div class="row2"><button class="btn grow" id="incEsc" ${dis}>🚨 Send again</button><button class="btn" id="incUndo" ${dis}>↩ Undo</button></div>`;
  else body = `<div class="decide"><button class="btn danger-fill" id="incEsc" ${dis}><span>🚨</span><b>Escalate</b><small>Tell the response team on Telegram and phone the response contact</small></button><button class="btn ok-line" id="incFalse" ${dis}><span>✓</span><b>False alarm</b><small>Close it. You can undo this</small></button></div>`;
  return `<div class="sect"><h4>Your decision</h4>${body}${can ? '' : `<div class="sub">Your role (${esc(role)}) cannot decide. Switch to Reviewer, Officer or Admin in the full console.</div>`}</div>`;
}
async function escalateIncident(i) {
  const btn = $('#incEsc'); const again = btn.textContent.includes('again');
  if (!btn.dataset.armed) { btn.dataset.armed = '1'; const old = btn.innerHTML; btn.innerHTML = again ? 'Tap again to send' : '<span>🚨</span><b>Tap again to send</b><small>Telegram and a phone call go out now</small>'; setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.innerHTML = old; } }, 6000); return; }
  delete btn.dataset.armed; btn.disabled = true;
  const card = pop({ icon: '🚨', title: 'Escalating the report', steps: ['Sending to Telegram', 'Phoning the response contact', 'Finished'] });
  try {
    const r = await api(`/incidents/${i.id}/escalate`, { method: 'POST' });
    const tg = r.telegram, ok = [], bad = [];
    if (tg.status === 'SENT') { card.note(0, 'Sent to Telegram', 'ok'); ok.push('Telegram'); } else { card.note(0, tg.status === 'NOT_CONFIGURED' ? 'Telegram is not set up' : (tg.error || 'Telegram did not accept it'), 'bad'); bad.push('Telegram'); }
    card.at(1);
    if (r.call.status === 'INITIATED') { const f = await followCall(card, 1, r.call.call_id); (f.ok ? ok : bad).push('Phone call'); }
    else { card.note(1, r.call.status === 'NOT_CONFIGURED' ? 'Phone calls are not set up' : (r.call.error || 'The call did not go through'), 'bad'); bad.push('Phone call'); }
    if (!ok.length) card.fail('Nothing could be sent. See the notes above.'); else card.done(`${ok.join(' and ')} done${bad.length ? `; ${bad.join(' and ')} did not work` : ''}.`);
    await loadIncidents(); showDetail(i.id);
  } catch (e) { card.fail(e.status === 403 ? 'Your role is not allowed to do this. Use Reviewer, Officer or Admin.' : e.message); }
}

async function showDetail(id) {
  marking = false; document.body.classList.remove('mark-mode'); setHint('');
  selected = id; drawPins();
  const i = incidents.find((x) => x.id === id) || (await api('/incidents/' + id)); const [kn, em] = kindOf(i), [st, c] = STATUS[i.status] || [i.status, ''];
  const ll = center(i.geometry); map.flyTo(ll, Math.max(map.getZoom(), 9), { duration: 0.6 });
  const near = fires.map((f) => ({ f, d: km(ll, [f[0], f[1]]) })).filter((x) => x.d <= 10).sort((a, b) => a.d - b.d);
  const site = i.evidence?.site;
  $('#rightBody').innerHTML = html`<button class="back" id="incBack">‹ All incidents</button>
    <div class="row-item" style="border:0;padding:0"><span style="font-size:26px">${em}</span><span class="grow"><h2 style="font-size:17px">${i.title}</h2><div class="sub">${kn} · ${ago(new Date(i.created_at).getTime())}</div></span></div>
    <div style="margin:8px 0"><span class="chip ${c}">${st}</span> <span class="sub">${ll[0].toFixed(4)}, ${ll[1].toFixed(4)}</span></div>
    
    <div class="sect simlink"><h4>🧊 3D simulation</h4><div class="sub">Walk through a simulated forest, fire or clearing inside this incident's boundary. It is invented, not real mapping or a forecast.</div><a class="btn primary" id="sim3d" href="/app#/ar-explorer/${encodeURIComponent(i.id)}" style="margin-top:8px;text-align:center;display:block">Open in Forest Explorer</a></div>
    ${raw(incDecision(i))}
    <div class="sect"><h4>🔥 Fires nearby</h4>${raw(!fireMeta ? '<span class="sub">Fire data is not loaded.</span>' : near.length ? `<b>${near.length}</b> hotspot${near.length > 1 ? 's' : ''} within 10 km in the last ${fireDays * 24} h. Nearest: ${near[0].d.toFixed(1)} km${near[0].f[4] != null ? `, ${esc(near[0].f[4])} MW` : ''}.` : `None within 10 km in the last ${fireDays * 24} h.`)}</div>
    <div class="sect"><h4>🏭 Factory and emissions</h4><div id="siteBox">${raw(siteHtml(site))}</div>
      <div id="airBox" style="margin-top:10px"><span class="sub"><span class="spin"></span>Loading air quality…</span></div></div>
    <div class="sect"><h4>📄 Report</h4><div class="sub">PDF with the details above, the evidence, and the audit trail.</div><div style="display:flex;gap:8px;margin-top:8px"><button class="btn primary grow" id="rep">Generate report</button></div><div class="sub" id="repOut"></div></div>
    <details class="more"><summary>More</summary><div class="row2" style="margin-top:8px"><button class="btn sm danger" id="incDel">− Remove this incident</button></div></details>
    <p class="fine"><a href="/app#/investigations/${encodeURIComponent(i.id)}">Open the full case in the console →</a></p>`;
  $('#incBack').onclick = () => { selected = null; drawPins(); paintList(); };
  $('#incEsc')?.addEventListener('click', () => escalateIncident(i));
  $('#incFalse')?.addEventListener('click', async () => { try { await api(`/incidents/${i.id}/false-alarm`, { method: 'POST' }); toast('Marked as a false alarm'); await loadIncidents(); showDetail(i.id); } catch (e) { toast(e.message, 'error'); } });
  $('#incUndo')?.addEventListener('click', async () => { try { await api(`/incidents/${i.id}/reopen`, { method: 'POST' }); toast('Back in review'); await loadIncidents(); showDetail(i.id); } catch (e) { toast(e.message, 'error'); } });
  $('#incDel').onclick = async (e) => { const b = e.target; if (!b.dataset.armed) { b.dataset.armed = '1'; b.textContent = 'Tap again to remove'; setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = '− Remove this incident'; } }, 5000); return; } try { await api(`/incidents/${i.id}`, { method: 'DELETE' }); selected = null; toast('Incident removed'); await loadIncidents(); paintList(); } catch (err) { toast(err.status === 403 ? 'Only a Reviewer or Admin can remove an incident' : err.message, 'error'); } };
  $('#siteEdit')?.addEventListener('click', () => siteDialog(i)); $('#siteAdd')?.addEventListener('click', () => siteDialog(i));
  $('#siteDel')?.addEventListener('click', async () => { try { await api(`/incidents/${i.id}/site`, { method: 'DELETE' }); toast('Factory info removed'); await loadIncidents(); showDetail(i.id); } catch (e) { toast(e.message, 'error'); } });
  $('#rep').onclick = async () => {
    const b = $('#rep'); b.disabled = true; b.textContent = 'Generating…';
    try { const r = await api(`/incidents/${i.id}/reports`, { method: 'POST' }); await download(`/reports/${r.pdf_id}/download`, `VanRakshak-report-${i.id.slice(0, 8)}.pdf`); $('#repOut').innerHTML = html`Report downloaded. <a href="#" id="jl">Get the JSON evidence package</a>`; $('#jl').onclick = (ev) => { ev.preventDefault(); download(`/reports/${r.id}/download`, `VanRakshak-evidence-${i.id.slice(0, 8)}.json`); }; }
    catch (e) { toast(e.message, 'error'); } finally { b.disabled = false; b.textContent = 'Generate report'; }
  };
  api(`/live/air?lat=${ll[0]}&lon=${ll[1]}`).then((a) => { if (selected !== id) return; const row = (n, s) => `<tr><td>${n}</td><td><b>${s.now ?? '–'}</b> ${esc(s.unit)}</td><td>${s.max_24h ?? '–'}</td></tr>`; $('#airBox').innerHTML = `<div class="sub" style="font-weight:700;color:#1b2924">Air quality here</div><table class="t"><tr><th></th><th>Now</th><th>Max 24 h</th></tr>${row('PM2.5', a.pm2_5)}${row('PM10', a.pm10)}${row('NO₂', a.no2)}${row('SO₂', a.so2)}</table><div class="note">${esc(a.kind)}. ${esc(a.attribution)}</div>`; }).catch((e) => { if (selected === id) $('#airBox').innerHTML = `<div class="note warn">Air quality unavailable: ${esc(e.message)}</div>`; });
}
function siteHtml(site) {
  if (!site) return '<div class="sub">Add a nearby factory if it applies.</div><div style="margin-top:8px"><button class="btn" id="siteAdd">Add factory info</button></div>';
  const rows = (site.emissions || []).map((e) => `<tr><td>${esc(e.pollutant)}</td><td><b>${esc(e.value)}</b> ${esc(e.unit)}</td><td>${esc(e.source)}${e.as_of ? ' (' + esc(e.as_of) + ')' : ''}</td></tr>`).join('');
  return `<b>${esc(site.name)}</b> <span class="chip">${esc(site.kind)}</span>${site.note ? `<div class="sub">${esc(site.note)}</div>` : ''}${rows ? `<table class="t"><tr><th>Pollutant</th><th>Reported</th><th>Source</th></tr>${rows}</table>` : '<div class="sub" style="margin-top:6px">No emission figures entered.</div>'}<div class="sub">Entered by a person (${esc(site.entered_by_role)}), not verified.</div><div style="margin-top:8px;display:flex;gap:8px"><button class="btn" id="siteEdit">Edit</button><button class="btn danger" id="siteDel">Remove</button></div>`;
}

function siteDialog(inc) {
  const s = inc.evidence?.site || { name: '', kind: 'factory', note: '', emissions: [] };
  const rowHtml = (e = {}) => `<div class="em" style="display:grid;grid-template-columns:1.1fr .8fr .7fr;gap:6px;margin-top:6px"><input type="text" class="p" placeholder="Pollutant (e.g. SO2)" value="${esc(e.pollutant || '')}"><input type="number" class="v" step="any" min="0" placeholder="Value" value="${esc(e.value ?? '')}"><input type="text" class="u" placeholder="Unit (mg/Nm3)" value="${esc(e.unit || '')}"><input type="text" class="s" style="grid-column:1/-1" placeholder="Source of this figure (permit, monitoring report…)" value="${esc(e.source || '')}"></div>`;
  $('#sheet').innerHTML = `<h2>Factory info</h2><div class="sub">Optional. Enter only what you can source. Nothing is estimated.</div>
    <label class="f">Name<input type="text" id="sn" maxlength="160" value="${esc(s.name)}" placeholder="Name of the factory or site"></label>
    <label class="f">Type<select id="sk">${['factory', 'mining', 'quarry', 'power', 'other'].map((k) => `<option ${k === s.kind ? 'selected' : ''}>${k}</option>`).join('')}</select></label>
    <label class="f">Note<input type="text" id="so" maxlength="500" value="${esc(s.note || '')}"></label>
    <div style="margin-top:12px;font-weight:700">Reported emission figures</div><div id="ems">${(s.emissions.length ? s.emissions : [{}]).map(rowHtml).join('')}</div>
    <button class="btn" id="addEm" style="margin-top:8px">＋ Add another figure</button><div class="note warn" id="serr" hidden style="margin-top:10px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" id="sc">Cancel</button><button class="btn primary" id="ss">Save</button></div>`;
  $('#modal').hidden = false; $('#sn').focus();
  $('#addEm').onclick = () => $('#ems').insertAdjacentHTML('beforeend', rowHtml());
  $('#sc').onclick = () => ($('#modal').hidden = true);
  $('#ss').onclick = async () => {
    const em = $$('#ems .em').map((r) => ({ pollutant: $('.p', r).value.trim(), value: $('.v', r).value, unit: $('.u', r).value.trim(), source: $('.s', r).value.trim() })).filter((x) => x.pollutant || x.value !== '');
    const bad = em.find((x) => !x.pollutant || x.value === '' || !x.unit || x.source.length < 2);
    if (bad) { $('#serr').hidden = false; $('#serr').textContent = 'Each figure needs a pollutant, value, unit and the source it came from.'; return; }
    try { await api(`/incidents/${inc.id}/site`, { method: 'POST', body: { name: $('#sn').value.trim(), kind: $('#sk').value, note: $('#so').value.trim() || null, emissions: em.map((x) => ({ ...x, value: +x.value })) } }); $('#modal').hidden = true; toast('Factory info saved'); await loadIncidents(); showDetail(inc.id); }
    catch (e) { $('#serr').hidden = false; $('#serr').textContent = e.status === 403 ? 'Your role cannot edit factory info.' : e.message; }
  };
}

// ------------------------------------------------------------------ panels, keys
$$('.collapse, .tab').forEach((b) => (b.onclick = () => { const p = $('#' + b.dataset.for), hide = !p.classList.contains('hidden'); p.classList.toggle('hidden', hide); $(`.tab[data-for=${b.dataset.for}]`).classList.toggle('show', hide); }));
if (!wide) { $('#left').classList.add('hidden'); $('#right').classList.add('hidden'); $$('.tab').forEach((t) => t.classList.add('show')); }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (!$('#modal').hidden) $('#modal').hidden = true; else if (marking) stopMarking(); else if (draftMarker) stopMarking(); } });
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; });
map.on('click', () => { if (!marking && selected) { /* keep detail open; clicking the map does not discard it */ } });

paintList();
loadIncidents().then(paintList);
window.rkMap = map;   // handy for tests and debugging
FIRE.init({ map });
FOREST.init({ map });
INFRA.init({ map });
CASES.init({ map });
FACTORY.init({ map });
SEARCH.init({ map });
FIRE.switchTab('tree');

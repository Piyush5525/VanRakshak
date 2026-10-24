// Tree-loss panel for the live map: scan an area, watch tree cover shrink (90 / 30 / 7 days ago → today),
// click a polygon to review it, find the responsible office and route, and send an alert, with the result shown right there.
import { api, esc, html, raw, toast } from './core.js';
import { pop, historyHtml } from './progress.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SNAP = [[90, '90 days ago'], [30, '30 days ago'], [7, '7 days ago'], [0, 'Today']];
const D = (iso) => new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
const AGE = (d) => (d <= 7 ? '#d7263d' : d <= 30 ? '#f4802a' : '#e9b949');
const ICON = { ok: '✓', failed: '✗', retried: '↻', skipped: '–', info: '•', warning: '!' };
const S = { map: null, win: Number(localStorage.getItem('rk.win')) || 30, custom: null, status: null, snaps: [], snapDays: 0, records: [], sel: null, run: null, poll: null, evPoll: null, lastEv: 0, layers: {}, on: { cover: false, lost: false, scanned: false }, play: null, busy: false };

export function init({ map }) {
  S.map = map;
  for (const k of ['cover', 'lost', 'records', 'route', 'was', 'scanned', 'area', 'drawn']) S.layers[k] = L.layerGroup().addTo(map);
  map.createPane('cover'); map.getPane('cover').style.zIndex = 330;
  map.on('moveend', drawArea);
  window.addEventListener('rk:watch-run', (e) => { S.busy = true; track(e.detail.id, pop({ icon: '🛰', title: 'Watching real change near the factory', steps: ['Finding satellite images', 'Reading optical and radar images', 'Looking for tree loss', 'Done'] })); });
  map.on('draw:created', (e) => { S.drawn = e.layer.toGeoJSON().geometry; S.drawing = false; showDrawn(); paint(); });
  window.addEventListener('rk:tab', (e) => show(e.detail === 'tree'));
  window.addEventListener('rk:layers', (e) => { S.on.cover = S.on.lost = S.on.scanned = !!e.detail.cover; draw(); paint(); });
  api('/forest-change/status').then((s) => { S.status = s; paint(); }).catch(() => {});
  api('/forest-change/runs?limit=3').then((rs) => { const r = rs.find((x) => ['QUEUED', 'RUNNING'].includes(x.status) && Date.now() - new Date(r0(x)).getTime() < 3600e3); if (r) track(r.id); }).catch(() => {});
  reload();
  paint();
}
const r0 = (x) => x.created_at;

function show(on) {
  for (const k of Object.keys(S.layers)) { if (on) S.layers[k].addTo(S.map); else S.map.removeLayer(S.layers[k]); }
  if (on) { drawArea(); draw(); }
}
const active = () => $('#forestBody') && !$('#forestBody').hidden;

// ------------------------------------------------------------------ data
async function reload() {
  try {
    const [tl, rec] = await Promise.all([api('/forest-change/timeline'), api(`/forest-change/records?days=${Math.max(S.win, 90)}&include_false=true`)]);
    S.snaps = tl; S.records = rec;
  } catch (e) { toast(e.message, 'error'); }
  if (S.sel && !S.records.find((r) => r.id === S.sel)) S.sel = null;
  paint(); draw();
}
const windowRecords = () => {
  const cut = S.custom ? S.custom.start : new Date(Date.now() - S.win * 864e5).toISOString().slice(0, 10);
  const end = S.custom ? S.custom.end : '9999';
  return S.records.filter((r) => r.est_date >= cut && r.est_date <= end);
};
function totals() {
  const at = (d) => { let forest = 0, lost = 0; for (const run of S.snaps) { const s = [...run.snapshots].filter((x) => x.days_ago >= d).sort((a, b) => a.days_ago - b.days_ago)[0]; if (s) { forest += s.forest_ha; lost += s.lost_ha; } } return { forest, lost }; };
  return { now: at(0), then: at(Math.max(S.snapDays, 0)), first: at(90) };
}

// ------------------------------------------------------------------ map drawing
function areaBounds() {
  if (S.drawn) { const b = L.geoJSON(S.drawn).getBounds(); return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]; }
  const b = S.map.getBounds(), c = b.getCenter(), cap = 3.6;
  const dLat = Math.min((b.getNorth() - b.getSouth()) / 2 * 0.8, cap / 110.57), dLon = Math.min((b.getEast() - b.getWest()) / 2 * 0.8, cap / (111.32 * Math.cos((c.lat * Math.PI) / 180)));
  return [c.lng - dLon, c.lat - dLat, c.lng + dLon, c.lat + dLat];
}
const areaKm2 = (b) => Math.abs(b[2] - b[0]) * 111.32 * Math.cos((((b[1] + b[3]) / 2) * Math.PI) / 180) * Math.abs(b[3] - b[1]) * 110.57;
const drawnKm2 = () => { if (!S.drawn) return null; const rings = S.drawn.type === 'Polygon' ? [S.drawn.coordinates] : S.drawn.coordinates; return rings.reduce((t, r) => t + L.GeometryUtil.geodesicArea(r[0].map(([x, y]) => L.latLng(y, x))), 0) / 1e6; };
function areaText() {
  const k = S.drawn ? drawnKm2() : areaKm2(areaBounds()), max = S.status?.max_area_km2 || 240, sharp = S.status?.sharp_area_km2 || 60;
  return { k, ok: k <= max, text: `${k.toFixed(k < 10 ? 1 : 0)} km² · ${k > max ? `too large, the limit is ${max} km²` : k <= sharp ? 'sharp view (30 m)' : 'wide view (60 m)'}` };
}
function showDrawn() {
  S.layers.drawn.clearLayers();
  if (S.drawn) { L.geoJSON(S.drawn, { style: { color: '#0a5c36', weight: 2.5, dashArray: '7 5', fillColor: '#0a5c36', fillOpacity: 0.06 }, interactive: false }).addTo(S.layers.drawn); }
}
function drawArea() {
  S.layers.area.clearLayers();
  const el = $('#areaInfo'); if (el) { const a = areaText(); el.textContent = a.text; el.style.color = a.ok ? '' : '#b3122a'; }
  if (!active() || S.busy || !$('#scanBox')?.open || S.sel || S.drawn || S.drawing) return;
  const b = areaBounds();
  L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#0a5c36', weight: 2, dashArray: '6 6', fill: true, fillOpacity: 0.04, interactive: false }).addTo(S.layers.area);
}
function startDraw(kind) {
  S.drawer?.disable(); S.drawn = null; S.drawing = true; showDrawn(); drawArea();
  const style = { color: '#0a5c36', weight: 2.5, fillOpacity: 0.08 };
  S.drawer = kind === 'rect' ? new L.Draw.Rectangle(S.map, { shapeOptions: style }) : new L.Draw.Polygon(S.map, { allowIntersection: false, shapeOptions: style, showArea: false });
  S.drawer.enable(); toast(kind === 'rect' ? 'Click and drag on the map to draw a rectangle' : 'Click each corner, then click the first point to finish', 'info');
}

function snapAt(run, d) { return [...run.snapshots].filter((x) => x.days_ago >= d).sort((a, b) => a.days_ago - b.days_ago)[0]; }
function draw() {
  for (const k of ['cover', 'lost', 'records', 'was', 'scanned', 'route']) S.layers[k].clearLayers();
  if (S.on.cover || S.on.lost) {
    for (const run of S.snaps) {
      if (S.on.scanned) { const b = run.bbox; L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#5b6b66', weight: 1, dashArray: '3 5', fill: false, interactive: false }).addTo(S.layers.scanned); }
      const s = snapAt(run, S.snapDays);
      if (!s) continue;
      if (S.on.cover && s.forest) L.geoJSON(s.forest, { pane: 'cover', style: { color: '#075e2f', weight: 1, fillColor: '#0f8a43', fillOpacity: 0.5 }, interactive: false }).addTo(S.layers.cover);
      if (S.on.lost && s.lost) L.geoJSON(s.lost, { pane: 'cover', style: { color: '#b3122a', weight: 0.8, fillColor: '#d7263d', fillOpacity: 0.55 }, interactive: false }).addTo(S.layers.lost);
      const first = snapAt(run, 90);
      if (S.snapDays < 90 && first?.forest && S.on.cover) L.geoJSON(first.forest, { pane: 'cover', style: { color: '#3b3b3b', weight: 1, dashArray: '4 4', fill: false }, interactive: false }).addTo(S.layers.was);
    }
  }
  const r = S.records.find((x) => x.id === S.sel);   // the map shows a spot only once it has been chosen
  if (r) {
    const noRec = r.verification?.class === 'NO_RECORD_FOUND';
    L.geoJSON(r.geometry, { style: { color: noRec ? '#b3122a' : '#2a6fdb', weight: 3, dashArray: noRec ? '6 4' : null, fillColor: AGE(r.age_days), fillOpacity: 0.55 } }).addTo(S.layers.records);
    L.marker([r.lat, r.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: `<div class="pin tree ${esc(r.severity)}"><span>🪓</span></div>` }), title: r.code }).addTo(S.layers.records);
    if (r.route?.geometry) L.geoJSON(r.route.geometry, { style: { color: '#0b57d0', weight: 5, opacity: 0.85 } }).addTo(S.layers.route);
    if (r.authority?.office) L.marker([r.authority.office.lat, r.authority.office.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: '<div class="pin" style="background:#14573c"><span>🏛</span></div>' }), title: r.authority.office.name }).addTo(S.layers.route);
  }
}

// ------------------------------------------------------------------ panel
function paint() {
  const box = $('#forestBody'); if (!box) return;
  const cur = S.sel && S.records.find((r) => r.id === S.sel);
  if (cur) return paintRecord(box, cur);
  const t = totals(), recs = windowRecords().filter((r) => r.status !== 'FALSE_POSITIVE').sort((a, b) => b.area_ha - a.area_ha);
  const haveData = S.snaps.length > 0;
  const label = SNAP.find((x) => x[0] === S.snapDays)[1];
  box.innerHTML = html`
    <div class="side-head" style="padding-left:0"><div><h2>Tree loss</h2><div class="sub">Choose a spot to see it on the map</div></div></div>
    <div class="pills" id="win">${raw([7, 30, 90].map((d) => `<button data-d="${d}" class="${!S.custom && S.win === d ? 'on' : ''}">${d} days</button>`).join('') + `<button data-d="c" class="${S.custom ? 'on' : ''}">Custom</button>`)}</div>
    ${raw(S.custom ? `<div class="row2" id="cust"><input type="date" id="cFrom" value="${esc(S.custom.start)}" max="${new Date().toISOString().slice(0, 10)}"><input type="date" id="cTo" value="${esc(S.custom.end)}" max="${new Date().toISOString().slice(0, 10)}"></div>` : '')}
    <h3>Loss spots ${S.custom ? `${S.custom.start} → ${S.custom.end}` : `in the last ${S.win} days`} <span class="sub">(${recs.length})</span></h3>
    <div class="list">${recs.length ? raw(recs.map((r) => `<button class="row-item" data-id="${esc(r.id)}"><span class="dot2" style="background:${AGE(r.age_days)}"></span><span class="grow"><b>${esc(r.code)} · ${r.area_ha} ha</b><small>${esc(D(r.est_date))} · ${esc(r.age_days)} d ago${r.verification?.class === 'NO_RECORD_FOUND' ? ' · no permit record' : ''}</small></span><span class="chip ${r.status === 'CONFIRMED' || r.status === 'REPORTED' ? 'red' : r.status === 'FIELD_CHECK' ? 'amber' : ''}">${esc(label2(r.status))}</span></button>`).join('')) : raw(haveData ? '<div class="empty"><b>No loss spots in this time</b>Either nothing was cleared, or clouds hid the area. Try 90 days.</div>' : '<div class="empty"><b>Nothing checked yet</b>Open “Check a new area” below.</div>')}</div>
    ${raw(haveData ? `<h3>Tree cover over time</h3>
    <div class="sub">Turn on <b>Tree cover history</b> in the layers panel, or step through here.</div>
    <div class="pills" id="snap">${SNAP.map(([d, n]) => `<button data-s="${d}" class="${S.snapDays === d && S.on.cover ? 'on' : ''}">${n}</button>`).join('')}</div>
    <div class="row2"><button class="btn sm" id="play">▶ Play</button><input type="range" id="slide" min="0" max="3" step="1" value="${SNAP.findIndex((x) => x[0] === S.snapDays)}" style="flex:1"></div>
    <div class="stat3"><div><b>${t.then.forest.toFixed(1)} ha</b><span>tree cover ${S.snapDays === 0 ? 'today' : label}</span></div><div><b class="${t.then.lost > 0 ? 'bad' : ''}">${t.then.lost.toFixed(1)} ha</b><span>trees lost by then</span></div><div><b class="${t.first.forest - t.then.forest > 0 ? 'bad' : ''}">${t.first.forest - t.then.forest > 0 ? '−' : ''}${(t.first.forest - t.then.forest).toFixed(1)} ha</b><span>change since 90 days ago</span></div></div>
    <div class="legend2"><span><i style="background:#0f8a43"></i>Tree cover</span><span><i style="background:#d7263d"></i>Trees lost</span><span><i class="dash"></i>Cover 90 days ago</span></div>` : '')}
    <details id="scanBox" class="scanbox" ${S.scanOpen || !haveData ? 'open' : ''}><summary>＋ Check a new area</summary>
      <div class="sub">Draw a rectangle or any shape around a forest anywhere in India, or just use the middle of the map.</div>
      <div class="row2" style="margin-top:6px"><button class="btn sm" id="dRect">▭ Draw a rectangle</button><button class="btn sm" id="dPoly">⬠ Draw a shape</button>${S.drawn ? raw('<button class="btn sm" id="dClear">✕ Clear</button>') : ''}</div>
      <div class="sub" style="margin-top:6px"><b id="areaInfo"></b></div>
      <button class="btn primary" id="scan" ${S.busy ? 'disabled' : ''} style="width:100%;margin-top:8px">${S.busy ? 'Checking satellites…' : '🛰 Check this area for tree loss'}</button>
      <div id="prog"></div></details>
    `;
  bind(box);
  drawArea();
}
const label2 = (s) => ({ NEEDS_REVIEW: 'New', CONFIRMED: 'Confirmed', FALSE_POSITIVE: 'Dismissed', FIELD_CHECK: 'Field check', REPORTED: 'Reported', RESOLVED: 'Resolved' }[s] || s);

function bind(box) {
  $('#scanBox summary', box)?.addEventListener('click', () => setTimeout(() => { S.scanOpen = $('#scanBox').open; drawArea(); }, 0));
  $('#win', box).onclick = (e) => {
    const d = e.target.dataset.d; if (!d) return;
    if (d === 'c') { const t = new Date().toISOString().slice(0, 10), f = new Date(Date.now() - 45 * 864e5).toISOString().slice(0, 10); S.custom = { start: f, end: t }; }
    else { S.custom = null; S.win = Number(d); localStorage.setItem('rk.win', d); }
    paint(); draw();
  };
  $('#cFrom', box)?.addEventListener('change', (e) => { S.custom.start = e.target.value; paint(); draw(); });
  $('#cTo', box)?.addEventListener('change', (e) => { S.custom.end = e.target.value; paint(); draw(); });
  $('#scan', box).onclick = startScan;
  $('#dRect', box).onclick = () => startDraw('rect'); $('#dPoly', box).onclick = () => startDraw('poly');
  $('#dClear', box)?.addEventListener('click', () => { S.drawn = null; showDrawn(); paint(); });
  $('#snap', box)?.addEventListener('click', (e) => { const d = e.target.dataset.s; if (d !== undefined) setSnap(Number(d)); });
  $('#slide', box)?.addEventListener('input', (e) => setSnap(SNAP[Number(e.target.value)][0]));
  $('#play', box)?.addEventListener('click', playSnaps);
  $$('.row-item', box).forEach((b) => (b.onclick = () => select(b.dataset.id, true)));
}
function setSnap(d) { S.snapDays = d; S.on.cover = S.on.lost = true; const cb = document.querySelector('#showCover'); if (cb) cb.checked = true; paint(); draw(); }
function playSnaps() {
  clearInterval(S.play); let i = 0; setSnap(SNAP[0][0]);
  S.play = setInterval(() => { i++; if (i >= SNAP.length) { clearInterval(S.play); return; } setSnap(SNAP[i][0]); }, 1600);
}

// ------------------------------------------------------------------ scanning
async function startScan() {
  const bbox = areaBounds();
  if (!areaText().ok) return toast('That area is too large. Draw a smaller one.', 'error');
  const body = { bbox, window_days: Math.max(S.custom ? Math.round((new Date(S.custom.end) - new Date(S.custom.start)) / 864e5) : S.win, 3) };
  if (S.custom) { body.start = S.custom.start; body.end = S.custom.end; }
  if (S.drawn) body.polygon = S.drawn;
  try { const r = await api('/forest-change/runs', { method: 'POST', body }); S.busy = true; paint(); track(r.run_id, pop({ icon: '🛰', title: 'Checking the area for tree loss', steps: ['Finding satellite images', 'Reading optical and radar images', 'Looking for tree loss', 'Done'] })); }
  catch (e) { toast(e.message, 'error'); }
}
function track(id, card) {
  S.busy = true; paint();
  clearInterval(S.poll);
  const tick = async () => {
    const r = await api('/forest-change/runs/' + id).catch(() => null); if (!r) return;
    card?.at(r.progress < 0.1 ? 0 : r.progress < 0.8 ? 1 : 2);
    const el = $('#prog'); const last = r.steps[r.steps.length - 1];
    if (el) el.innerHTML = `<div class="progress"><div style="width:${Math.round((r.progress || 0.02) * 100)}%"></div></div><div class="sub">${esc(last ? friendly(last.message) : 'Starting…')}</div>`;
    if (r.status === 'COMPLETE' || r.status === 'FAILED') {
      clearInterval(S.poll); S.busy = false;
      if (r.status === 'FAILED') { card?.fail(r.error || 'The check failed.'); toast('The check failed: ' + (r.error || 'see message'), 'error'); await reload(); const e2 = $('#prog'); if (e2) e2.innerHTML = `<div class="banner2 red">${esc(r.error || 'The check failed')}</div>`; return; }
      const s = r.summary; card?.done(s.patches ? `${s.loss_ha} ha of tree loss found in ${s.patches} spot${s.patches > 1 ? 's' : ''}.` : 'No tree loss found in this area.');
      S.scanOpen = false; await reload(); S.snapDays = 0; S.on.cover = S.on.lost = true; const cb = document.querySelector('#showCover'); if (cb) cb.checked = true; paint(); draw();
      const b = r.bbox; S.map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [30, 30] });
      const e2 = $('#prog');
      if (e2) e2.innerHTML = `<div class="banner2 ${s.patches ? 'amber' : 'green'}"><b>${s.patches ? `${s.loss_ha} ha of tree loss found in ${s.patches} spot${s.patches > 1 ? 's' : ''}` : 'No tree loss found'}</b> (${s.loss_pct_of_forest ?? 0}% of the forest here).${s.warnings.length ? `<div class="sub">${esc(s.warnings.join(' '))}</div>` : ''}</div>`;
    }
  };
  S.poll = setInterval(tick, 2500); tick();
}
const friendly = (m) => m.replace(/^sentinel-2/i, 'Optical images').replace(/^sentinel-1/i, 'Radar images');

// ------------------------------------------------------------------ one record
async function select(id, zoom) {
  S.sel = id; S.lastEv = 0;
  const full = await api('/forest-change/records/' + id).catch((e) => { toast(e.message, 'error'); return null; });
  if (!full) return;
  const k = S.records.findIndex((r) => r.id === id); if (k >= 0) S.records[k] = { ...S.records[k], ...full };
  if (zoom !== false) { const b = L.geoJSON(full.geometry).getBounds(); S.map.fitBounds(b.pad(1.5), { maxZoom: 17 }); }
  $('#right')?.classList.remove('hidden'); document.body.classList.remove('hide-right');
  paint(); draw();
  clearInterval(S.evPoll); S.evPoll = setInterval(pollEvents, 2500); pollEvents();
}
async function pollEvents() {
  if (!S.sel) return clearInterval(S.evPoll);
  const evs = await api(`/forest-change/events?record_id=${S.sel}&since_id=0&limit=100`).catch(() => null);
  const el = $('#recAct'); if (el && evs) el.innerHTML = historyHtml(evs, 'Nothing has happened yet.');
}
function paintRecord(box, r) {
  const st = S.status, ch = st?.channels || {}, v = r.verification, role = localStorage.getItem('rk.role') || 'reviewer';
  const canSend = ['admin', 'officer'].includes(role);
  const a = r.authority, rt = r.route, hist = r.notify?.history || [];
  const cats = st?.categories || {};
  box.innerHTML = html`
    <button class="btn sm" id="back">‹ All loss spots</button>
    <div class="row-item" style="border:0;padding:6px 0"><span class="dot2" style="background:${AGE(r.age_days)};width:14px;height:14px"></span><span class="grow"><h2 style="font-size:18px">${r.code} · ${r.area_ha} ha</h2><div class="sub">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</div></span><span class="chip ${r.severity === 'HIGH' ? 'red' : r.severity === 'MEDIUM' ? 'amber' : ''}">${r.severity} size</span></div>
    <div class="facts"><div><b>${D(r.est_date)}</b><span>about when (±${Math.round(r.date_uncertainty_days || 0)} d)</span></div><div><b>${r.age_days} days</b><span>ago</span></div><div><b>${r.seen_count}×</b><span>seen in scans</span></div></div>
    <div class="sub">Seen by: ${r.evidence === 'optical_radar_agree' ? 'optical and radar satellites agree' : r.evidence === 'optical' ? 'optical satellite' : 'radar satellite'}.</div>
    <div class="sect"><h4>Is there a permit?</h4>${raw(v ? `<div class="banner2 ${v.class === 'NO_RECORD_FOUND' ? 'red' : v.class === 'AUTHORIZED_RECORD_MATCH' ? 'green' : 'amber'}"><b>${esc(v.meaning)}</b><div class="sub">${esc(v.next_step)}</div></div>` : '<div class="sub">Not checked.</div>')}</div>
    <div class="sect"><h4>What is it? <span class="sub">(change it any time)</span></h4>
      <label class="f">Cause<select id="cat"><option value="">Not decided</option>${raw(Object.entries(cats).map(([k, n]) => `<option value="${esc(k)}" ${r.category === k ? 'selected' : ''}>${esc(n)}</option>`).join(''))}</select></label>
      <div class="pills3" id="stat">${raw([['CONFIRMED', '✔ Real loss'], ['FIELD_CHECK', '🥾 Needs a visit'], ['FALSE_POSITIVE', '✖ Not a loss']].map(([k, n]) => `<button data-s="${k}" class="${r.status === k ? 'on' : ''}">${n}</button>`).join(''))}</div>
      <label class="f">Notes<input id="notes" type="text" value="${esc(r.notes || '')}" placeholder="What you saw or know"></label></div>
    <div class="sect"><h4>Who should go, and how?</h4>
      ${raw(a?.office ? `<div class="banner2 ${a.status === 'OK' ? 'green' : 'amber'}"><b>${esc(a.office.name)}</b><div class="sub">${a.office.straight_line_km} km away · unverified map data${a.office.phone ? ' · phone ' + esc(a.office.phone) : ''}</div>${rt?.status === 'OK' ? `<div><b>${rt.distance_km} km · about ${Math.round(rt.duration_min)} min</b> by road (${esc(rt.provider)}) <a href="${esc(rt.navigation_url)}" target="_blank" rel="noopener">Open in Google Maps</a></div><div class="sub">${esc(rt.caveats[1])}</div>` : `<div class="sub">${esc(a.reason || '')}</div>`}</div>` : a?.status === 'NO_AUTHORITY_FOUND' ? `<div class="banner2 amber">${esc(a.reason)}</div>` : '<div class="sub">Not looked up yet.</div>')}
      <button class="btn sm" id="findOff">${a?.office ? '↻ Look again' : '🔎 Find the forest office and route'}</button><div id="offRes"></div></div>
    <div class="sect"><h4>Send an alert</h4>
      ${raw(!['CONFIRMED', 'FIELD_CHECK', 'REPORTED'].includes(r.status) ? '<div class="banner2 amber">Mark it “Real loss” or “Needs a visit” first.</div>' : '')}
      <div class="row2 tog">${raw([['telegram', 'Telegram'], ['ivr', 'Phone call'], ['email', 'E-mail'], ['dashboard', 'Dashboard']].map(([k, n]) => `<label title="${ch[k]?.ready ? '' : 'Not set up: ' + esc((ch[k]?.missing || []).join(', '))}"><input type="checkbox" class="chn" value="${k}" ${['dashboard', 'telegram'].includes(k) ? 'checked' : ''} ${ch[k]?.ready ? '' : 'disabled'}> ${n}${ch[k]?.ready ? '' : ' <small>(not set up)</small>'}</label>`).join(''))}</div>
      <details style="margin:6px 0"><summary>What the phone call says</summary><div id="script" class="script">Loading…</div><button class="btn sm" id="hear" style="margin-top:6px">▶ Hear it</button></details>
      ${raw(canSend ? '' : '<div class="sub">Your role (' + esc(role) + ') cannot send alerts. Switch to Officer or Admin in the full console.</div>')}
      <button class="btn primary" id="send" style="width:100%" ${canSend ? '' : 'disabled'}>Send alert</button>
      <div id="sendRes"></div>
      <div class="sub" style="margin-top:6px">${raw(hist.length ? hist.slice(-4).map((h) => `${esc(h.channel)}: <b>${esc(label3(h.status))}</b>`).join(' · ') : '')}</div></div>
    <div class="sect"><h4>Record</h4><div id="recAct"></div></div>
    <button class="btn sm" id="geo">⬇ Export all as GeoJSON</button>`;
  $('#back', box).onclick = () => { S.sel = null; clearInterval(S.evPoll); paint(); draw(); };
  $('#cat', box).onchange = (e) => patch({ category: e.target.value });
  $('#notes', box).onchange = (e) => patch({ notes: e.target.value });
  $('#stat', box).onclick = (e) => { const s = e.target.dataset.s; if (s) patch({ status: s }); };
  $('#findOff', box).onclick = findOffice;
  api(`/forest-change/records/${r.id}/call-script`).then((x) => { S.script = x.script; const el = $('#script'); if (el) el.textContent = x.script; }).catch(() => { const el = $('#script'); if (el) el.textContent = 'The message could not be prepared.'; });
  $('#hear', box).onclick = () => { if (!('speechSynthesis' in window) || !S.script) return toast('This browser cannot play the message', 'error'); if (speechSynthesis.speaking) { speechSynthesis.cancel(); return; } const u = new SpeechSynthesisUtterance(S.script); u.lang = 'en-IN'; u.rate = 0.95; speechSynthesis.speak(u); };
  $('#send', box).onclick = send;
  $('#geo', box).onclick = () => import('./core.js').then((c) => c.download('/forest-change/export.geojson?days=90', 'rakshak-forest-change.geojson'));
}
async function patch(body) {
  try { const r = await api('/forest-change/records/' + S.sel, { method: 'PATCH', body }); upd(r); toast('Saved'); } catch (e) { toast(e.message, 'error'); }
}
function upd(r) { const k = S.records.findIndex((x) => x.id === r.id); if (k >= 0) S.records[k] = { ...S.records[k], ...r }; paint(); draw(); pollEvents(); }
async function findOffice() {
  const b = $('#findOff'); b.disabled = true; const out = $('#offRes'); out.innerHTML = '<div class="sub">Looking up forest offices and road routes…</div>';
  try { await api(`/forest-change/records/${S.sel}/authority`, { method: 'POST', body: {} }); const full = await api('/forest-change/records/' + S.sel); upd(full); if (full.route?.geometry) S.map.fitBounds(L.geoJSON(full.route.geometry).getBounds().pad(0.2)); }
  catch (e) { out.innerHTML = `<div class="banner2 red">${esc(e.message)}</div>`; b.disabled = false; }
  pollEvents();
}
const label3 = (st) => ({ SENT: 'sent', INITIATED: 'calling', NOT_CONFIGURED: 'not set up', FAILED: 'failed', REFUSED: 'refused', SIMULATED: 'previewed', DUPLICATE_SUPPRESSED: 'already sent' }[st] || st.toLowerCase());
async function send() {
  const channels = $$('.chn').filter((c) => c.checked && !c.disabled).map((c) => c.value);
  if (!channels.length) return toast('Choose at least one way to send', 'error');
  const btn = $('#send'), out = $('#sendRes');
  if (!btn.dataset.armed) {   // two taps: the first asks, the second sends
    btn.dataset.armed = '1'; btn.textContent = `Tap again to send (${channels.map((c) => ({ telegram: 'Telegram', ivr: 'phone call', email: 'e-mail', dashboard: 'dashboard' }[c])).join(', ')})`;
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = 'Send alert'; } }, 6000); return;
  }
  delete btn.dataset.armed; btn.disabled = true; btn.textContent = 'Sending…'; out.innerHTML = '';
  const NAME = { telegram: 'Telegram', ivr: 'Phone call', email: 'E-mail', dashboard: 'Dashboard' };
  const card = pop({ icon: '📣', title: 'Sending your alert', steps: [...channels.map((c) => NAME[c]), 'Done'] });
  try {
    const r = await api(`/forest-change/records/${S.sel}/notify`, { method: 'POST', body: { channels, mode: 'real', confirm_real: true } });
    const bad = r.results.filter((x) => !/SENT|INITIATED/.test(x.status));
    out.innerHTML = r.results.map((x) => `<div class="res ${/SENT|INITIATED/.test(x.status) ? 'ok' : 'bad'}"><b>${esc(NAME[x.channel])}</b> · ${esc(label3(x.status))}${x.error ? ' · ' + esc(x.error) : ''}</div>`).join('');
    if (bad.length) card.fail(bad.map((x) => `${NAME[x.channel]}: ${label3(x.status)}${x.error ? ' (' + x.error + ')' : ''}`).join('. ')); else card.done('Everything was accepted by its service.');
    const k = S.records.findIndex((q) => q.id === r.record.id); if (k >= 0) S.records[k] = { ...S.records[k], ...r.record };
    if (r.results.some((x) => x.channel === 'ivr' && x.status === 'INITIATED')) followCall(S.sel);
    draw();
  } catch (e) { out.innerHTML = `<div class="banner2 red">${esc(e.message)}</div>`; card.fail(e.message); }
  btn.disabled = false; btn.textContent = 'Send alert'; pollEvents();
}

// follow a real phone call through ringing -> answered -> finished using the provider's own status
function followCall(id) {
  const card = pop({ icon: '📞', title: 'Calling the response contact', steps: ['Placing the call', 'Ringing', 'Answered', 'Call finished'] });
  const rank = { ivr_initiated: 0, ivr_ringing: 1, ivr_answered: 2 };
  let n = 0;
  const t = setInterval(async () => {
    n++;
    const evs = await api(`/forest-change/events?record_id=${id}&since_id=0&limit=100`).catch(() => []);
    const calls = evs.filter((e) => e.event_type.startsWith('ivr_') && e.event_type !== 'ivr_retry').reverse();
    for (const e of calls.slice().reverse()) if (e.event_type in rank) card.at(rank[e.event_type]);
    const last = calls[0]?.event_type;
    if (last === 'ivr_completed') { card.done('The call finished.'); clearInterval(t); }
    else if (['ivr_failed', 'ivr_busy', 'ivr_no_answer'].includes(last)) { card.fail({ ivr_busy: 'The line was busy.', ivr_no_answer: 'Nobody answered.' }[last] || 'The call did not go through.'); clearInterval(t); }
    else if (n > 45) { card.fail('No final status from the phone provider yet.'); clearInterval(t); }
    if (S.sel === id) pollEvents();
  }, 3000);
}

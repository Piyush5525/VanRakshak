// Tree-loss panel for the live map. List of loss spots by time window; "Check a new area" (draw a shape, scan the last 90 days);
// choose a spot to see it on the map, its permits, and make the one decision that matters: Escalate or False alarm.
import { api, esc, html, raw, toast } from './core.js';
import { pop, historyHtml } from './progress.js';
import { followCall, label as sendLabel } from './escalate.js';
import { drawRoute, journeyHtml, journeyText, legendHtml, walkLink, ICON as WAY } from './routes.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const SNAP = [[90, '90 days ago'], [30, '30 days ago'], [7, '7 days ago'], [0, 'Today']];
const D = (iso) => (iso ? new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : '–');
const Dshort = (iso) => new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
const AGE = (d) => (d <= 7 ? '#d7263d' : d <= 30 ? '#f4802a' : '#e9b949');
const S = { map: null, win: Number(localStorage.getItem('rk.win')) || 30, custom: null, status: null, snaps: [], snapDays: 0, records: [], sel: null, poll: null, evPoll: null, layers: {}, on: { cover: false, lost: false, scanned: false }, play: null, busy: false, scanOpen: false, drawn: null, drawing: false, drawKind: null, drawer: null, permitForm: false, runs: [], area: null, view: 'spots', grids: {}, cell: null, fromCell: false };
const role = () => localStorage.getItem('rk.role') || 'reviewer';
const canJudge = () => ['admin', 'reviewer', 'officer'].includes(role());

export function init({ map }) {
  S.map = map;
  for (const k of ['cover', 'lost', 'grid', 'cell', 'records', 'route', 'was', 'scanned', 'area', 'drawn', 'extent']) S.layers[k] = L.layerGroup().addTo(map);
  map.createPane('cover'); map.getPane('cover').style.zIndex = 330;
  map.on('moveend', drawArea);
  map.on('draw:created', (e) => {
    if (!S.drawing) return;   // another panel may be drawing
    const g = e.layer.toGeoJSON().geometry; S.drawing = false; S.drawKind = null; S.drawer = null;
    S.drawn = g; const k = drawnKm2();
    if (!(k >= 0.02)) { S.drawn = null; toast('That shape is too small. Draw a larger one.', 'error'); }   // a click without a drag is not an area
    showDrawn(); paint();
  });
  map.on('draw:drawstop', () => { if (S.drawing) { S.drawing = false; S.drawKind = null; S.drawer = null; drawArea(); paint(); } });   // Esc or a cancelled shape
  window.addEventListener('rk:watch-run', (e) => { S.busy = true; track(e.detail.id, scanCard('Watching real change near the factory')); });
  window.addEventListener('rk:tab', (e) => { if (e.detail !== 'tree') cancelDraw(); show(e.detail === 'tree'); });
  window.addEventListener('rk:layers', (e) => { S.on.cover = S.on.lost = S.on.scanned = !!e.detail.cover; if (e.detail.cover && S.runs.length) { S.area = S.area || S.runs[0].id; S.view = 'grid'; S.sel = null; S.cell = null; loadGrid(S.area).then(() => drawEverything()); } if (!e.detail.cover && S.view === 'grid') S.view = 'spots'; drawEverything(); });
  api('/forest-change/status').then((s) => { S.status = s; paint(); }).catch(() => {});
  api('/forest-change/runs?limit=3').then((rs) => { const r = rs.find((x) => ['QUEUED', 'RUNNING'].includes(x.status)); if (r) track(r.id, scanCard('Checking the area for tree loss')); }).catch(() => {});
  reload(); paint();
}
const scanCard = (title) => pop({ icon: '🛰', title, steps: ['Finding satellite images', 'Reading optical and radar images', 'Looking for tree loss', 'Done'] });

function show(on) {
  for (const k of Object.keys(S.layers)) { if (on) S.layers[k].addTo(S.map); else S.map.removeLayer(S.layers[k]); }
  if (on) { drawArea(); draw(); }
}
const active = () => $('#forestBody') && !$('#forestBody').hidden;
const cur = () => S.sel && S.records.find((r) => r.id === S.sel);

// ------------------------------------------------------------------ data
async function loadRuns() {
  try { const seen = new Set(); S.runs = (await api('/forest-change/runs?limit=60')).filter((r) => r.status === 'COMPLETE' && (r.summary?.forest_ha ?? 0) >= 1).filter((r) => { const k = JSON.stringify([r.bbox, r.polygon]); if (seen.has(k)) return false; seen.add(k); return true; }); } catch { /* the list still works without it */ }
  if (S.area && !S.runs.find((r) => r.id === S.area)) S.area = null;
}
const drawEverything = () => { paint(); draw(); };
async function reload() {
  await loadRuns();
  try {
    const [tl, rec] = await Promise.all([api('/forest-change/timeline'), api(`/forest-change/records?days=${Math.max(S.win, 90)}&include_false=true`)]);
    S.snaps = tl; S.records = rec.map((r) => ({ ...(S.records.find((x) => x.id === r.id) || {}), ...r }));
  } catch (e) { toast(e.message, 'error'); }
  if (S.sel && !cur()) S.sel = null;
  paint(); draw();
}
const areaOf = () => S.runs.find((r) => r.id === S.area) || null;
const areaGeom = (r) => r.polygon || { type: 'Polygon', coordinates: [[[r.bbox[0], r.bbox[1]], [r.bbox[2], r.bbox[1]], [r.bbox[2], r.bbox[3]], [r.bbox[0], r.bbox[3]], [r.bbox[0], r.bbox[1]]]] };
function inside(g, lon, lat) {   // ray casting over a GeoJSON Polygon / MultiPolygon
  const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
  return polys.some((rings) => { let c = false; for (const ring of rings) for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) { const [xi, yi] = ring[i], [xj, yj] = ring[j]; if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) c = !c; } return c; });
}
const inArea = (r) => { const a = areaOf(); return !a || inside(areaGeom(a), r.lon, r.lat); };
const areaLabel = (r, k) => `Area ${S.runs.length - k} · ${r.area_km2} km² · ${new Date(r.created_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })}${r.summary ? ` · ${r.summary.loss_ha} ha lost` : ''}`;
const windowRecords = () => {
  const cut = S.custom ? S.custom.start : new Date(Date.now() - S.win * 864e5).toISOString().slice(0, 10);
  const end = S.custom ? S.custom.end : '9999';
  return S.records.filter((r) => r.est_date >= cut && r.est_date <= end && inArea(r));
};
function totals() {
  const at = (d) => { let forest = 0, lost = 0; for (const run of S.snaps) { const s = [...run.snapshots].filter((x) => x.days_ago >= d).sort((a, b) => a.days_ago - b.days_ago)[0]; if (s) { forest += s.forest_ha; lost += s.lost_ha; } } return { forest, lost }; };
  return { then: at(Math.max(S.snapDays, 0)), first: at(90) };
}

// ------------------------------------------------------------------ map drawing
function areaBounds() {
  if (S.drawn) { const b = L.geoJSON(S.drawn).getBounds(); return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]; }
  const b = S.map.getBounds(), c = b.getCenter(), cap = 3.6;
  const dLat = Math.min((b.getNorth() - b.getSouth()) / 2 * 0.8, cap / 110.57), dLon = Math.min((b.getEast() - b.getWest()) / 2 * 0.8, cap / (111.32 * Math.cos((c.lat * Math.PI) / 180)));
  return [c.lng - dLon, c.lat - dLat, c.lng + dLon, c.lat + dLat];
}
const areaKm2 = (b) => Math.abs(b[2] - b[0]) * 111.32 * Math.cos((((b[1] + b[3]) / 2) * Math.PI) / 180) * Math.abs(b[3] - b[1]) * 110.57;
const drawnKm2 = () => { const rings = S.drawn.type === 'Polygon' ? [S.drawn.coordinates] : S.drawn.coordinates; return rings.reduce((t, r) => t + L.GeometryUtil.geodesicArea(r[0].map(([x, y]) => L.latLng(y, x))), 0) / 1e6; };
function areaText() {
  const k = S.drawn ? drawnKm2() : areaKm2(areaBounds()), max = S.status?.max_area_km2 || 240, sharp = S.status?.sharp_area_km2 || 60;
  return { k, ok: k <= max, text: `${k.toFixed(k < 10 ? 1 : 0)} km² · ${k > max ? `too large, the limit is ${max} km²` : k <= sharp ? 'sharp view (30 m)' : 'wide view (60 m)'}` };
}
function showDrawn() {
  S.layers.drawn.clearLayers();
  if (S.drawn) L.geoJSON(S.drawn, { style: { color: '#0a5c36', weight: 2.5, dashArray: '7 5', fillColor: '#0a5c36', fillOpacity: 0.06 }, interactive: false }).addTo(S.layers.drawn);
}
function drawArea() {
  S.layers.area.clearLayers();
  const el = $('#areaInfo'); if (el) { const a = areaText(); el.textContent = a.text; el.style.color = a.ok ? '' : '#b3122a'; }
  if (!active() || S.busy || !$('#scanBox')?.open || S.sel || S.drawn || S.drawing) return;
  const b = areaBounds();
  L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#0a5c36', weight: 2, dashArray: '6 6', fill: true, fillOpacity: 0.04, interactive: false }).addTo(S.layers.area);
}
function cancelDraw() { S.drawer?.disable(); S.drawer = null; S.drawing = false; S.drawKind = null; }
function startDraw(kind) {
  if (S.drawing && S.drawKind === kind) { cancelDraw(); drawArea(); paint(); return; }   // pressing the same tool again puts it down
  S.drawer?.disable(); S.drawn = null; S.drawing = true; S.drawKind = kind; showDrawn(); drawArea();
  const style = { color: '#0a5c36', weight: 2.5, fillOpacity: 0.08 };
  S.drawer = kind === 'rect' ? new L.Draw.Rectangle(S.map, { shapeOptions: style }) : new L.Draw.Polygon(S.map, { allowIntersection: false, shapeOptions: style, showArea: false });
  S.drawer.enable(); paint(); toast(kind === 'rect' ? 'Click and drag on the map to draw a rectangle. Press Esc to stop' : 'Click each corner, then click the first point to finish. Press Esc to stop');
}
function snapAt(run, d) { return [...run.snapshots].filter((x) => x.days_ago >= d).sort((a, b) => a.days_ago - b.days_ago)[0]; }
const lossColor = (v, max) => (v <= 0 ? '#cfe3d6' : (() => { const t = Math.min(1, v / Math.max(max, 0.01)); const a = [255, 213, 158], b = [179, 18, 42]; return `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * t)).join(',')})`; })());
const gridDays = () => (S.custom ? 90 : S.win);
async function loadGrid(id) {
  if (!id) return null;
  if (!S.grids[id]) S.grids[id] = await api(`/forest-change/runs/${id}/grid`).catch((e) => { toast(e.message, 'error'); return null; });
  return S.grids[id];
}
function drawGrid() {
  S.layers.grid.clearLayers(); S.layers.cell.clearLayers(); S.layers.extent.clearLayers();
  const a = areaOf();
  if (a) L.geoJSON(areaGeom(a), { style: { color: '#44524b', weight: 2, dashArray: '8 6', fill: false }, interactive: false }).addTo(S.layers.extent);
  const g = a && S.grids[a.id]; if (!g || S.view !== 'grid' || cur()) return;
  const w = String(gridDays()), max = Math.max(...g.cells.map((c) => c.loss_ha[w]), 0.01);
  for (const c of g.cells) {
    const v = c.loss_ha[w], sel = c.id === S.cell;
    L.geoJSON(c.polygon, { style: { color: sel ? '#111' : v > 0 ? '#7a1020' : '#8aa89a', weight: sel ? 4 : 1, dashArray: v > 0 || sel ? null : '3 4', fillColor: lossColor(v, max), fillOpacity: sel ? 0 : S.cell ? (v > 0 ? 0.22 : 0.05) : v > 0 ? 0.55 : 0.1 } }).on('click', () => selectCell(c.id)).bindTooltip(`${c.id} · ${v} ha lost in the last ${w} days`, { sticky: true }).addTo(S.layers.grid);
    if (v > 0) L.marker(L.geoJSON(c.polygon).getBounds().getCenter(), { icon: L.divIcon({ className: '', iconSize: [40, 18], iconAnchor: [20, 9], html: `<div class="cell-tag ${sel ? 'sel' : ''}">${esc(c.id)}</div>` }), interactive: false }).addTo(S.layers.grid);
  }
}
function draw() {
  drawGrid();
  for (const k of ['cover', 'lost', 'records', 'was', 'scanned', 'route']) S.layers[k].clearLayers();
  if (S.on.cover || S.on.lost) {
    for (const run of S.snaps) {
      if (S.on.scanned) { const b = run.bbox; L.rectangle([[b[1], b[0]], [b[3], b[2]]], { color: '#5b6b66', weight: 1, dashArray: '3 5', fill: false, interactive: false }).addTo(S.layers.scanned); }
      const s = snapAt(run, S.snapDays); if (!s) continue;
      if (S.on.cover && s.forest) L.geoJSON(s.forest, { pane: 'cover', style: { color: '#075e2f', weight: 1, fillColor: '#0f8a43', fillOpacity: 0.5 }, interactive: false }).addTo(S.layers.cover);
      if (S.on.lost && s.lost) L.geoJSON(s.lost, { pane: 'cover', style: { color: '#b3122a', weight: 0.8, fillColor: '#d7263d', fillOpacity: 0.55 }, interactive: false }).addTo(S.layers.lost);
      const first = snapAt(run, 90);
      if (S.snapDays < 90 && first?.forest && S.on.cover) L.geoJSON(first.forest, { pane: 'cover', style: { color: '#3b3b3b', weight: 1, dashArray: '4 4', fill: false }, interactive: false }).addTo(S.layers.was);
    }
  }
  const r = cur();   // the map shows a spot only once it has been chosen
  if (!r) return;
  const none = !(r.verification?.permits || []).length;
  L.geoJSON(r.geometry, { style: { color: none ? '#b3122a' : '#2a6fdb', weight: 3, dashArray: none ? '6 4' : null, fillColor: AGE(r.age_days), fillOpacity: 0.55 } }).addTo(S.layers.records);
  L.marker([r.lat, r.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: `<div class="pin tree ${esc(r.severity)}"><span>🪓</span></div>` }), title: r.code }).addTo(S.layers.records);
  const list = responders(r), pick = S.resp === 'none' ? null : list.find((x) => x.id === S.resp) || list[0];
  for (const x of list) {   // every responder is a pin; the one you chose gets its route
    const on = !pick || x === pick;
    L.marker([x.lat, x.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: `<div class="pin" style="background:${x.kind === 'police_station' ? '#7b1fa2' : '#14573c'};${on ? '' : 'opacity:.7;transform:scale(.85)'}"><span>${x.icon || '🏛'}</span></div>` }), title: x.name })
      .on('click', () => { S.resp = x.id; paint(); draw(); }).addTo(S.layers.route);
  }
  const cop = list.find((x) => x.kind === 'police_station' && !x.main);
  const line = (x, color) => (hasLine(x) ? drawRoute(S.layers.route, x, { color }) : L.polyline([[x.lat, x.lon], [r.lat, r.lon]], { color, weight: 3, dashArray: '3 8', opacity: 0.9 }).bindTooltip('Straight line: no road line is available for this one').addTo(S.layers.route));
  if (pick && cop && cop !== pick) line(cop, '#7b1fa2');   // the nearest police station's route is shown beside the main one
  if (pick) line(pick, pick.kind === 'police_station' ? '#7b1fa2' : '#0b57d0');
}

// everyone who can respond to a spot: the main responder first, then the nearest police station, then others
const hasLine = (x) => !!(x.geometry?.coordinates?.length || (x.segments || []).some((g) => g.geometry?.coordinates?.length));
function responders(r) {
  const a = r.authority, rt = r.route;
  if (!a?.office) return [];
  const main = { ...a.office, ...(rt?.status === 'OK' ? { distance_km: rt.distance_km, duration_min: rt.duration_min, drive_km: rt.drive_km, walk_km: rt.walk_km, drive_min: rt.drive_min, walk_min: rt.walk_min, geometry: rt.geometry, segments: rt.segments, walk_url: rt.walk_url, navigation_url: rt.navigation_url } : {}), main: true };
  const pol = a.police && !a.police.same_as_main ? [a.police] : [];
  return [main, ...pol, ...(a.others || [])];
}

// ------------------------------------------------------------------ the list
const label2 = (s) => ({ NEEDS_REVIEW: 'New', CONFIRMED: 'Escalated', FIELD_CHECK: 'Escalated', REPORTED: 'Escalated', FALSE_POSITIVE: 'False alarm', SUPERSEDED: 'Not seen again', RESOLVED: 'Resolved' }[s] || s);
function gridPanel() {
  const a = areaOf(), g = a && S.grids[a.id], w = String(gridDays());
  if (!a) return '<div class="empty"><b>Choose an area</b>Pick one of the areas you have checked.</div>';
  if (!g) return '<div class="sub"><span class="spin"></span> Dividing the area into cells…</div>';
  const cells = g.cells.filter((c) => c.loss_ha[w] > 0).sort((x, y) => y.loss_ha[w] - x.loss_ha[w]), max = Math.max(...cells.map((c) => c.loss_ha[w]), 0.01);
  return `<h3>Grid cells with loss in the last ${w} days <span class="sub">(${cells.length} of ${g.cells.length}, cells of ${g.cell_m} m)</span></h3>
    <div class="legend2"><span><i style="background:#ffd59e"></i>a little</span><span><i style="background:#b3122a"></i>most loss</span><span><i style="background:#cfe3d6"></i>none</span></div>
    <div class="list">${cells.length ? cells.map((c) => `<button class="row-item" data-cell="${esc(c.id)}"><span class="cell-badge" style="background:${lossColor(c.loss_ha[w], max)}">${esc(c.id)}</span><span class="grow"><b>${c.loss_ha[w]} ha lost</b><small>${c.spots.length} spot${c.spots.length === 1 ? '' : 's'} · ${c.cover_ha['0']} ha of tree cover left</small></span></button>`).join('') : '<div class="empty"><b>No loss in this time</b>No cell lost tree cover in the last ' + w + ' days. Try a longer time.</div>'}</div>`;
}
function paint() {
  const box = $('#forestBody'); if (!box) return;
  if (cur()) return paintRecord(box, cur());
  if (S.cell && areaOf() && S.grids[S.area]?.cells.find((c) => c.id === S.cell)) return paintCell(box);
  const recs = windowRecords().filter((r) => !['FALSE_POSITIVE', 'SUPERSEDED'].includes(r.status)).sort((a, b) => b.area_ha - a.area_ha);
  const haveData = S.snaps.length > 0, grid = S.view === 'grid' && S.runs.length;
  box.innerHTML = html`
    <div class="side-head" style="padding-left:0"><div><h2>Tree loss</h2><div class="sub">${grid ? 'Choose a cell to see how it changed' : 'Choose a spot to see it on the map'}</div></div></div>
    <div class="pills" id="win">${raw([7, 30, 90].map((d) => `<button data-d="${d}" class="${!S.custom && S.win === d ? 'on' : ''}">${d} days</button>`).join('') + `<button data-d="c" class="${S.custom ? 'on' : ''}">Custom</button>`)}</div>
    ${raw(S.custom ? `<div class="row2" id="cust"><input type="date" id="cFrom" value="${esc(S.custom.start)}" max="${new Date().toISOString().slice(0, 10)}"><input type="date" id="cTo" value="${esc(S.custom.end)}" max="${new Date().toISOString().slice(0, 10)}"></div>` : '')}
    ${raw(S.runs.length ? `<label class="f areasel">Area<select id="areaSel"><option value="">Everywhere I have checked</option>${S.runs.map((r, k) => `<option value="${esc(r.id)}" ${S.area === r.id ? 'selected' : ''}>${esc(areaLabel(r, k))}</option>`).join('')}</select></label>
      <div class="pills" id="viewTabs"><button data-v="spots" class="${S.view === 'spots' ? 'on' : ''}">Loss spots (${recs.length})</button><button data-v="grid" class="${S.view === 'grid' ? 'on' : ''}">Grid</button></div>` : '')}
    ${raw(grid ? gridPanel() : `<h3>Loss spots ${S.custom ? `${S.custom.start} → ${S.custom.end}` : `in the last ${S.win} days`}${areaOf() ? ' in this area' : ''} <span class="sub">(${recs.length})</span></h3>
    <div class="list">${recs.length ? recs.map((r) => `<button class="row-item" data-id="${esc(r.id)}"><span class="dot2" style="background:${AGE(r.age_days)}"></span><span class="grow"><b>${esc(r.code)} · ${r.area_ha} ha</b><small>${esc(Dshort(r.est_date))} · ${esc(r.age_days)} d ago${(r.verification?.permits || []).length ? ' · permit found' : ''}</small></span><span class="chip ${['CONFIRMED', 'FIELD_CHECK', 'REPORTED'].includes(r.status) ? 'red' : ''}">${esc(label2(r.status))}</span></button>`).join('') : (haveData ? '<div class="empty"><b>No loss spots here in this time</b>Either nothing was cleared, or clouds hid the area. Try 90 days.</div>' : '<div class="empty"><b>Nothing checked yet</b>Open “Check a new area” below.</div>')}</div>`)}
    <details id="scanBox" class="scanbox" ${S.scanOpen || !haveData ? 'open' : ''}><summary><span>Check a new area</span></summary>
      ${raw(S.busy ? `<div class="scanprog"><div id="prog"><div class="progress"><div style="width:4%"></div></div><div class="sub">Starting…</div></div><div class="row2"><button class="btn sm danger" id="scanCancel">✕ Cancel this check</button></div></div>` : `<div class="sub">Draw a rectangle or any shape around a forest anywhere in India, or use the middle of the map.</div>
      <div class="row2" style="margin-top:8px"><button class="btn sm ${S.drawing && S.drawKind === 'rect' ? 'primary' : ''}" id="dRect" aria-pressed="${S.drawing && S.drawKind === 'rect'}">${S.drawing && S.drawKind === 'rect' ? '✕ Stop drawing' : '▭ Draw a rectangle'}</button><button class="btn sm ${S.drawing && S.drawKind === 'poly' ? 'primary' : ''}" id="dPoly" aria-pressed="${S.drawing && S.drawKind === 'poly'}">${S.drawing && S.drawKind === 'poly' ? '✕ Stop drawing' : '⬠ Draw a shape'}</button>${S.drawn ? '<button class="btn sm" id="dClear">✕ Clear shape</button>' : ''}</div>
      <div class="sub" style="margin-top:6px"><b id="areaInfo"></b></div>
      <button class="btn primary big" id="scan" ${S.drawing ? 'disabled' : ''}>🛰 Check this area for tree loss</button><div id="prog"></div>`)}</details>`;
  bind(box); drawArea();
}
// ---- one grid cell: how it changed over 7 / 30 / 90 days, and the spots inside it
async function selectCell(id) {
  S.cell = id; S.sel = null; S.view = 'grid';
  const g = S.grids[S.area], c = g?.cells.find((x) => x.id === id); if (!c) return;
  S.map.fitBounds(L.geoJSON(c.polygon).getBounds().pad(0.6), { maxZoom: 17 });
  drawEverything();
}
function paintCell(box) {
  const c = S.grids[S.area].cells.find((x) => x.id === S.cell), l = c.loss_ha, cv = c.cover_ha, end = S.grids[S.area].window_end;
  const bar = (v, max) => `<div class="bar"><i style="width:${Math.min(100, (v / Math.max(max, 0.01)) * 100)}%"></i></div>`;
  box.innerHTML = html`
    <button class="back" id="cellBack">‹ All cells</button>
    <div class="row-item" style="border:0;padding:6px 0"><span class="cell-badge big" style="background:${lossColor(l['90'], l['90'])}">${c.id}</span><span class="grow"><h2 style="font-size:18px">Cell ${c.id}</h2><div class="sub">${c.area_ha} ha · checked up to ${D(end)}</div></span></div>
    <div class="sect"><h4>Tree loss in this cell</h4>
      <div class="period"><span>Last 7 days</span><b>${l['7']} ha</b>${raw(bar(l['7'], l['90']))}</div>
      <div class="period"><span>Last 30 days</span><b>${l['30']} ha</b>${raw(bar(l['30'], l['90']))}</div>
      <div class="period"><span>Last 90 days</span><b>${l['90']} ha</b>${raw(bar(l['90'], l['90']))}</div></div>
    <div class="sect"><h4>Tree cover left</h4>
      <div class="stat4"><div><b>${cv['90']} ha</b><span>90 days ago</span></div><div><b>${cv['30']} ha</b><span>30 days ago</span></div><div><b>${cv['7']} ha</b><span>7 days ago</span></div><div><b>${cv['0']} ha</b><span>today</span></div></div>
      <div class="sub">${cv['90'] > cv['0'] ? `Down ${(cv['90'] - cv['0']).toFixed(2)} ha since 90 days ago.` : 'No drop in tree cover in this cell.'}</div></div>
    <div class="sect"><h4>Loss spots in this cell (${c.spots.length})</h4>
      <div class="list">${raw(c.spots.length ? c.spots.map((s2) => `<button class="row-item" data-id="${esc(s2.id)}"><span class="dot2" style="background:${AGE(s2.age_days)}"></span><span class="grow"><b>${esc(s2.code)} · ${s2.area_ha} ha</b><small>${esc(Dshort(s2.est_date))} · ${s2.age_days} d ago</small></span><span class="chip ${['CONFIRMED', 'FIELD_CHECK', 'REPORTED'].includes(s2.status) ? 'red' : ''}">${esc(label2(s2.status))}</span></button>`).join('') : '<div class="sub">No single clearing is large enough to list; the loss is made of small patches.</div>')}</div></div>`;
  $('#cellBack', box).onclick = () => { S.cell = null; S.layers.cell.clearLayers(); drawEverything(); };
  $$('.row-item', box).forEach((b) => (b.onclick = () => { S.fromCell = true; select(b.dataset.id, true); }));
}
function bind(box) {
  $('#scanBox summary', box)?.addEventListener('click', () => setTimeout(() => { S.scanOpen = $('#scanBox').open; drawArea(); }, 0));
  $('#win', box).onclick = (e) => {
    const d = e.target.dataset.d; if (!d) return;
    if (d === 'c') { const t = new Date().toISOString().slice(0, 10), f = new Date(Date.now() - 45 * 864e5).toISOString().slice(0, 10); S.custom = { start: f, end: t }; }
    else { S.custom = null; S.win = Number(d); localStorage.setItem('rk.win', d); }
    paint(); draw();
  };
  $('#cFrom', box)?.addEventListener('change', (e) => { S.custom.start = e.target.value; paint(); });
  $('#cTo', box)?.addEventListener('change', (e) => { S.custom.end = e.target.value; paint(); });
  $('#areaSel', box)?.addEventListener('change', async (e) => { S.area = e.target.value || null; S.cell = null; drawEverything(); if (S.area) { await loadGrid(S.area); const a = areaOf(); if (a) S.map.fitBounds(L.geoJSON(areaGeom(a)).getBounds().pad(0.1)); drawEverything(); } });
  $('#viewTabs', box)?.addEventListener('click', async (e) => { const v = e.target.dataset.v; if (!v) return; S.view = v; S.cell = null; if (v === 'grid') { S.area = S.area || S.runs[0]?.id; S.on.cover = S.on.lost = true; const cb = $('#showCover'); if (cb) cb.checked = true; await loadGrid(S.area); const a = areaOf(); if (a) S.map.fitBounds(L.geoJSON(areaGeom(a)).getBounds().pad(0.1)); } drawEverything(); });
  $$('[data-cell]', box).forEach((b) => (b.onclick = () => selectCell(b.dataset.cell)));
  if ($('#scan', box)) $('#scan', box).onclick = startScan;
  $('#dRect', box) && ($('#dRect', box).onclick = () => startDraw('rect')); $('#dPoly', box) && ($('#dPoly', box).onclick = () => startDraw('poly'));
  $('#scanCancel', box)?.addEventListener('click', cancelScan);
  $('#dClear', box)?.addEventListener('click', () => { cancelDraw(); S.drawn = null; showDrawn(); drawArea(); paint(); });
  $$('.row-item[data-id]', box).forEach((b) => (b.onclick = () => select(b.dataset.id, true)));
}
function setSnap(d) { S.snapDays = d; S.on.cover = S.on.lost = true; const cb = $('#showCover'); if (cb) cb.checked = true; paint(); draw(); }
function playSnaps() { clearInterval(S.play); let i = 0; setSnap(SNAP[0][0]); S.play = setInterval(() => { i++; if (i >= SNAP.length) return clearInterval(S.play); setSnap(SNAP[i][0]); }, 1600); }

// ------------------------------------------------------------------ scanning
async function startScan() {
  const bbox = areaBounds();
  if (!areaText().ok) return toast('That area is too large. Draw a smaller one.', 'error');
  const body = { bbox, window_days: Math.max(S.custom ? Math.round((new Date(S.custom.end) - new Date(S.custom.start)) / 864e5) : S.win, 3) };
  if (S.custom) { body.start = S.custom.start; body.end = S.custom.end; }
  if (S.drawn) body.polygon = S.drawn;
  try { const r = await api('/forest-change/runs', { method: 'POST', body }); S.busy = true; paint(); track(r.run_id, scanCard('Checking the area for tree loss')); }
  catch (e) { toast(e.message, 'error'); }
}
async function cancelScan() {
  try { await api(`/forest-change/runs/${S.runId}/cancel`, { method: 'POST' }); toast('Check cancelled'); } catch (e) { toast(e.message, 'error'); }
}
function track(id, card) {
  S.runId = id; S.busy = true; paint(); clearInterval(S.poll);
  const tick = async () => {
    const r = await api('/forest-change/runs/' + id).catch(() => null); if (!r) return;
    card?.at(r.progress < 0.1 ? 0 : r.progress < 0.88 ? 1 : 2);
    const last = r.steps[r.steps.length - 1], el = $('#prog');
    if (el) el.innerHTML = `<div class="progress"><div style="width:${Math.round((r.progress || 0.02) * 100)}%"></div></div><div class="sub">${esc(last ? friendly(last.message) : 'Starting…')}</div>`;
    card?.note(r.progress < 0.88 ? 1 : 2, last ? friendly(last.message) : '');
    if (r.status === 'CANCELLED') { clearInterval(S.poll); S.busy = false; card?.fail('Check cancelled'); paint(); return; }
    if (r.status !== 'COMPLETE' && r.status !== 'FAILED') return;
    clearInterval(S.poll); S.busy = false;
    if (r.status === 'FAILED') { card?.fail(r.error || 'The check failed.'); await reload(); const e2 = $('#prog'); if (e2) e2.innerHTML = `<div class="banner2 red">${esc(r.error || 'The check failed')}</div>`; return; }
    const s = r.summary;
    const msg = s.forest_ha < 1 ? 'No forest was found in this area (it looks like farmland or built-up land), so there is nothing to check.' : s.patches ? `${s.loss_ha} ha of tree loss found in ${s.patches} spot${s.patches > 1 ? 's' : ''} (${s.loss_pct_of_forest ?? 0}% of ${Math.round(s.forest_ha)} ha of forest).` : `No tree loss found in ${Math.round(s.forest_ha)} ha of forest.`;
    card?.done(msg);
    S.scanOpen = false; S.drawn = null; showDrawn(); reload().then(async () => { S.area = id; S.cell = null; S.view = 'spots'; S.win = 90; S.custom = null; await loadGrid(id); S.snapDays = 0; S.on.cover = S.on.lost = s.forest_ha >= 1; const cb = $('#showCover'); if (cb) cb.checked = S.on.cover; paint(); draw(); const e2 = $('#prog'); if (e2) e2.innerHTML = `<div class="banner2 ${s.patches ? 'amber' : 'green'}">${esc(msg)}${s.warnings.length ? `<div class="sub">${esc(s.warnings.join(' '))}</div>` : ''}</div>`; });
    const b = r.bbox; S.map.fitBounds([[b[1], b[0]], [b[3], b[2]]], { padding: [30, 30] });
  };
  S.poll = setInterval(tick, 2500); tick();
}
const friendly = (m) => m.replace(/^sentinel-2/i, 'Optical images').replace(/^sentinel-1/i, 'Radar images');

// ------------------------------------------------------------------ one spot
async function select(id, zoom) {
  S.sel = id; S.permitForm = false; S.resp = null;
  const full = await api('/forest-change/records/' + id).catch((e) => { toast(e.message, 'error'); return null; });
  if (!full) return;
  const k = S.records.findIndex((r) => r.id === id); if (k >= 0) S.records[k] = { ...S.records[k], ...full };
  if (zoom !== false) S.map.fitBounds(L.geoJSON(full.geometry).getBounds().pad(1.5), { maxZoom: 17 });
  document.body.classList.remove('hide-right');
  paint(); draw();
  clearInterval(S.evPoll);
  if (!full.authority || !full.route || (full.authority.status === 'OK' && full.authority.v !== 2)) findResponders(id);
}
// Show who would respond (forest office or police station) and the road/walking route as soon as a spot is opened
async function findResponders(id) {
  S.finding = id; paint();
  try { await api(`/forest-change/records/${id}/authority`, { method: 'POST', body: {} }); } catch { /* shown below as "not found" */ }
  if (S.sel !== id) return;
  const full = await api('/forest-change/records/' + id).catch(() => null);
  S.finding = null;
  if (full) { const k = S.records.findIndex((r) => r.id === id); if (k >= 0) S.records[k] = { ...S.records[k], ...full }; }
  const armed = $('#fcEsc')?.dataset.armed ? $('#fcEsc').innerHTML : null;   // keep a half-done "tap again" through the repaint
  paint(); draw();
  const b = $('#fcEsc'); if (armed && b) { b.dataset.armed = '1'; b.innerHTML = armed; }
}
async function pollEvents() {
  if (!S.sel) return clearInterval(S.evPoll);
  const evs = await api(`/forest-change/events?record_id=${S.sel}&since_id=0&limit=100`).catch(() => null);
  const el = $('#recAct'); if (el && evs) el.innerHTML = historyHtml(evs, 'Nothing has happened yet.');
}
const STATUS_WORD = { issued: 'Issued', approved: 'Approved', granted: 'Granted', conditional_approval: 'Approved with conditions', pending: 'Pending', rejected: 'Rejected', revoked: 'Revoked', expired: 'Expired' };
function permitCard(p) {
  const tone = p.class === 'AUTHORIZED_RECORD_MATCH' ? 'ok' : p.class === 'EXPIRED_OR_REVOKED_RECORD' ? 'bad' : 'warn';
  return `<div class="permit ${tone}"><div class="permit-h"><b>${esc(p.project || 'Permit')}</b><span class="chip ${tone === 'ok' ? 'green' : tone === 'bad' ? 'red' : 'amber'}">${esc(STATUS_WORD[p.status] || p.status)}</span></div>
    <dl class="kv"><dt>Permit no.</dt><dd>${esc(p.number || 'not given')}</dd><dt>Issued by</dt><dd>${esc(p.authority || 'not given')}</dd><dt>Valid</dt><dd>${esc(D(p.valid_from))} → ${esc(D(p.valid_to))}</dd><dt>Covers</dt><dd>${p.overlap != null ? Math.round(p.overlap * 100) + '% of this spot' : '–'}</dd></dl>
    <div class="permit-f">${p.source_url ? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener">Open the source document</a>` : '<span class="sub">No source link</span>'}${p.entered_by ? `<span class="sub"> · entered by ${esc(p.entered_by)}</span><button class="link-x" data-rm="${esc(p.id)}" title="Remove this permit">− Remove</button>` : ''}</div></div>`;
}
const field = (id, label, ph = '', type = 'text', extra = '') => `<label class="f">${label}<input id="${id}" type="${type}" placeholder="${esc(ph)}" ${extra}></label>`;
function permitSection(r) {
  const permits = r.verification?.permits || [];
  const form = S.permitForm ? `<div class="permit-form"><b>Add a permit you know of</b>
      ${field('pAuth', 'Issued by *', 'e.g. State Forest Department')}${field('pNo', 'Permit / proposal number *', 'as printed on the document')}${field('pName', 'Project name', '')}
      <label class="f">Status<select id="pStatus">${Object.entries(STATUS_WORD).map(([k, n]) => `<option value="${k}">${n}</option>`).join('')}</select></label>
      <div class="grid2">${field('pFrom', 'Valid from', '', 'date')}${field('pTo', 'Valid to', '', 'date')}</div>${field('pUrl', 'Link to the document', 'https://…')}
      <div class="sub">The permit is taken to cover this spot, as you enter it. The authority’s own boundary is not available here.</div>
      <div class="row2" style="margin-top:8px"><button class="btn primary grow" id="pSave">Save permit</button><button class="btn" id="pCancel">Cancel</button></div><div id="pErr"></div></div>` : '';
  const cls = r.verification?.class;
  const ok = cls === 'AUTHORIZED_RECORD_MATCH';
  const why = ok ? '' : cls === 'EXPIRED_OR_REVOKED_RECORD' ? 'The permit found is expired or revoked' : cls === 'PARTIAL_OR_UNCLEAR_RECORD' ? 'A permit nearby may not cover this spot' : '';
  return `<div class="sect permit-sect"><div class="pstat ${ok ? 'yes' : 'no'}"><span class="pi">${ok ? '✓' : '✕'}</span><span class="pt"><b>${ok ? 'Government approved' : 'Not government approved'}</b><small>${ok ? 'A permit covers this spot' : esc(why) || 'No permit found for this spot'}</small></span></div>
    ${permits.length ? permits.map(permitCard).join('') : ''}
    ${S.permitForm ? form : '<button class="btn sm" id="pAdd" style="margin-top:8px">＋ Add a permit I know of</button>'}</div>`;
}
function decisionSection(r) {
  const escd = ['CONFIRMED', 'FIELD_CHECK', 'REPORTED'].includes(r.status), fa = r.status === 'FALSE_POSITIVE';
  const done = (r.notify?.history || []).filter((h) => h.mode === 'real');
  const last = (ch) => [...done].reverse().find((h) => h.channel === ch);
  const dis = canJudge() ? '' : 'disabled';
  return `<div class="sect"><h4>Your decision</h4>
    ${fa ? `<div class="banner2 grey"><b>Marked as a false alarm.</b></div><button class="btn" id="fcUndo" ${dis}>↩ Undo, review it again</button>`
      : escd ? `<div class="banner2 green"><b>Escalated.</b><div class="sub">${['telegram', 'ivr'].map((c) => { const h = last(c); return h ? `${c === 'telegram' ? 'Telegram' : 'Phone call'}: ${esc(sendLabel(h.status))}` : ''; }).filter(Boolean).join(' · ') || 'Marked as a real loss.'}</div></div>
        <div class="row2"><button class="btn grow" id="fcEsc" ${dis}>🚨 Send again</button><button class="btn" id="fcUndo" ${dis}>↩ Undo</button></div>`
        : `<div class="decide"><button class="btn danger-fill" id="fcEsc" ${dis}><span>🚨</span><b>Escalate</b><small>Tell the forest office on Telegram and phone the response contact</small></button>
        <button class="btn ok-line" id="fcFalse" ${dis}><span>✓</span><b>False alarm</b><small>Close it. You can undo this</small></button></div>${canJudge() ? '' : `<div class="sub">Your role (${esc(role())}) is not allowed to decide.</div>`}`}</div>`;
}
function historySection() {
  if (!S.snaps.length) return '';
  const t = totals(), label = SNAP.find((x) => x[0] === S.snapDays)[1], lostMore = t.first.forest - t.then.forest;
  return `<div class="sect"><h4>Tree cover over time</h4>
    <div class="pills" id="snap">${SNAP.map(([d, n]) => `<button data-s="${d}" class="${S.snapDays === d && S.on.cover ? 'on' : ''}">${n}</button>`).join('')}</div>
    <div class="row2"><button class="btn sm" id="play">▶ Play</button><input type="range" id="slide" min="0" max="3" step="1" value="${SNAP.findIndex((x) => x[0] === S.snapDays)}" style="flex:1"></div>
    <div class="stat3"><div><b>${t.then.forest.toFixed(1)} ha</b><span>tree cover ${S.snapDays === 0 ? 'today' : label}</span></div><div><b class="${t.then.lost > 0 ? 'bad' : ''}">${t.then.lost.toFixed(1)} ha</b><span>trees lost by then</span></div><div><b class="${lostMore > 0 ? 'bad' : ''}">${lostMore > 0 ? '−' : ''}${lostMore.toFixed(1)} ha</b><span>change since 90 days ago</span></div></div>
    <div class="legend2"><span><i style="background:#0f8a43"></i>Tree cover</span><span><i style="background:#d7263d"></i>Trees lost</span><span><i class="dash"></i>Cover 90 days ago</span></div></div>`;
}
function viewReport(url, code) {
  document.getElementById('rptView')?.remove();
  const el = document.createElement('div'); el.id = 'rptView'; el.className = 'rptview';
  el.innerHTML = `<div class="rptbar"><b>${esc(code)} report</b><span class="grow"></span><a class="btn sm primary" href="${url}" download="${esc(code)}-report.pdf">⬇ Download</a><button class="btn sm" id="rptClose">✕ Close</button></div><iframe title="Report" src="${url}"></iframe>`;
  document.body.appendChild(el);
  const close = () => { el.remove(); URL.revokeObjectURL(url); document.removeEventListener('keydown', esc2); };
  const esc2 = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc2);
  el.querySelector('#rptClose').onclick = close;
}
function responderSection(r) {
  if (S.finding === r.id) return '<div class="sect"><h4>Who can respond</h4><div class="sub">Finding the nearest forest office and police station, and the road route…</div></div>';
  const a = r.authority, list = responders(r);
  if (!list.length) return a?.reason ? `<div class="sect"><h4>Who can respond</h4><div class="sub">${esc(a.reason)}</div></div>` : '';
  const pick = S.resp === 'none' ? null : list.find((x) => x.id === S.resp) || list[0];
  const has = (x) => x.distance_km != null;
  const row = (x) => `<button class="resp ${x === pick ? 'on' : ''}" data-resp="${esc(x.id)}"><span class="ri">${esc(x.icon || '📍')}</span><span class="rn"><b>${esc(x.name)}</b><small>${esc(x.kind_label || '')}${x.main ? ' · main' : ''}</small></span><span class="rm">${has(x) ? `${Math.round(x.duration_min)} min<small>${x.distance_km} km${x.walk_km ? ' · 🚶' : ''}</small>` : `${x.straight_line_km} km<small>straight</small>`}</span></button>`;
  const extra = (a?.status === 'OK' && a.police?.same_as_main) ? '<div class="sub">No forest office is close, so this police station is the main responder.</div>' : '';
  return `<div class="sect"><h4>Who can respond <span class="gnote">tap to show or hide its route</span></h4><div class="resps">${list.map(row).join('')}</div>${extra}
    ${!pick ? '' : has(pick) ? `<div class="respd">${journeyHtml(pick)}${hasLine(pick) ? '' : '<div class="sub">The map provider gave time and distance only, so the map shows a straight dashed line.</div>'}${legendHtml}<div class="sub"><a href="${esc(pick.navigation_url)}" target="_blank" rel="noopener">🚗 Directions by road</a>${pick.walk_url ? ' · ' + walkLink(pick) : ''}</div>${pick.phone ? `<div class="sub">📞 …${esc(String(pick.phone).slice(-3))}</div>` : ''}${!pick.main && pick.id ? `<button class="btn sm" data-use="${esc(pick.id)}">Make this the main responder</button>` : ''}</div>` : `<div class="sub" style="margin-top:6px">${esc(a?.reason || 'No road route could be calculated for this one.')}</div>`}</div>`;
}
function paintRecord(box, r) {
  const a = r.authority, rt = r.route;
  box.innerHTML = html`
    <button class="back" id="fcBack">‹ ${S.fromCell ? 'Back to cell ' + S.cell : 'All loss spots'}</button>
    <div class="row-item" style="border:0;padding:6px 0"><span class="dot2" style="background:${AGE(r.age_days)};width:14px;height:14px"></span><span class="grow"><h2 style="font-size:18px">${r.code} · ${r.area_ha} ha</h2><div class="sub">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</div></span><span class="chip ${r.severity === 'HIGH' ? 'red' : r.severity === 'MEDIUM' ? 'amber' : ''}">${r.severity} size</span></div>
    <div class="facts"><div title="About when it happened"><b>${Dshort(r.est_date)}</b></div><div title="How long ago"><b>${r.age_days} days ago</b></div><div title="Seen in this many scans"><b>${r.seen_count}×</b></div></div>
    ${raw(responderSection(r))}
    ${raw(permitSection(r))}
    ${raw(decisionSection(r))}
    ${raw(historySection())}
    <div class="sect"><h4>3D simulation</h4><div class="sub">A simulated forest inside this spot's exact boundary: clearing, fire or both. Invented, not evidence.</div><a class="btn primary" id="sim3d" href="/app#/ar-explorer/forest:${r.id}" style="margin-top:8px;text-align:center;display:block">Open in Forest Explorer</a></div>
    <div class="sect"><h4>Report</h4><div class="sub">Everything about this spot in one file: live satellite pictures, map, routes and clickable links.</div><div class="row2" style="margin-top:8px"><button class="btn grow" id="fcView">👁 View report</button><button class="btn primary grow" id="fcPdf">⬇ Download</button></div></div>
    <details class="more"><summary>More</summary><div class="row2" style="margin-top:8px"><button class="btn sm" id="fcGeo">⬇ Export GeoJSON</button><button class="btn sm danger" id="fcDel">− Remove this spot</button></div></details>`;
  $('#fcBack', box).onclick = () => { S.sel = null; clearInterval(S.evPoll); if (!S.fromCell) S.cell = null; S.fromCell = false; drawEverything(); if (S.cell) selectCell(S.cell); };
  $('#fcEsc', box)?.addEventListener('click', escalate);
  $('#fcFalse', box)?.addEventListener('click', falseAlarm);
  $('#fcUndo', box)?.addEventListener('click', () => patch({ status: 'NEEDS_REVIEW' }, 'Back in review'));
  $('#pAdd', box)?.addEventListener('click', () => { S.permitForm = true; paint(); });
  $('#pCancel', box)?.addEventListener('click', () => { S.permitForm = false; paint(); });
  $('#pSave', box)?.addEventListener('click', savePermit);
  $$('[data-rm]', box).forEach((b) => (b.onclick = () => removePermit(b.dataset.rm)));
  $$('[data-resp]', box).forEach((b) => (b.onclick = () => { const was = (S.resp === 'none' ? null : S.resp || responders(r)[0]?.id) === b.dataset.resp; S.resp = was ? 'none' : b.dataset.resp; paint(); draw(); }));
  $$('[data-use]', box).forEach((b) => (b.onclick = async () => { b.disabled = true; try { await api(`/forest-change/records/${r.id}/authority`, { method: 'POST', body: { station_id: b.dataset.use } }); toast('Responder changed'); await select(r.id); } catch (e) { toast(e.message, 'error'); b.disabled = false; } }));
  $('#snap', box)?.addEventListener('click', (e) => { const d = e.target.dataset.s; if (d === undefined) return; if (Number(d) === S.snapDays && S.on.cover) { S.on.cover = S.on.lost = false; const cb = $('#showCover'); if (cb) cb.checked = false; paint(); draw(); } else setSnap(Number(d)); });
  $('#slide', box)?.addEventListener('input', (e) => setSnap(SNAP[Number(e.target.value)][0]));
  $('#play', box)?.addEventListener('click', playSnaps);
  $('#fcView', box).onclick = async (e) => {   // opens the report inside the page: no pop-up window to be blocked
    e.target.disabled = true; e.target.textContent = 'Preparing…';
    try { const b = await api(`/forest-change/records/${r.id}/report.pdf`, { blob: true }); viewReport(URL.createObjectURL(b), r.code); }
    catch (err) { toast(err.message, 'error'); } finally { e.target.disabled = false; e.target.textContent = '👁 View report'; }
  };
  $('#fcPdf', box).onclick = () => import('./core.js').then((c) => c.download(`/forest-change/records/${r.id}/report.pdf`, `${r.code}-report.pdf`));
  $('#fcGeo', box).onclick = () => import('./core.js').then((c) => c.download('/forest-change/export.geojson?days=90', 'rakshak-forest-change.geojson'));
  $('#fcDel', box).onclick = (e) => { const b = e.target; if (!b.dataset.armed) { b.dataset.armed = '1'; b.textContent = 'Tap again to remove'; setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = '− Remove this spot'; } }, 5000); return; } removeSpot(); };
}

// ---- the two decisions
function upd(r) { const k = S.records.findIndex((x) => x.id === r.id); if (k >= 0) S.records[k] = { ...S.records[k], ...r }; paint(); draw(); pollEvents(); }
async function patch(body, ok) {
  try { upd(await api('/forest-change/records/' + S.sel, { method: 'PATCH', body })); if (ok) toast(ok); } catch (e) { toast(e.message, 'error'); }
}
async function falseAlarm() { await patch({ status: 'FALSE_POSITIVE' }, 'Marked as a false alarm'); }
async function escalate() {
  const btn = $('#fcEsc'); const resend = btn.textContent.includes('again');
  if (!btn.dataset.armed) {   // one confirming tap: this sends real messages and phones a real number
    btn.dataset.armed = '1'; const old = btn.innerHTML;
    btn.innerHTML = resend ? 'Tap again to send' : '<span>🚨</span><b>Tap again to send</b><small>Telegram and a phone call go out now</small>';
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.innerHTML = old; } }, 6000); return;
  }
  delete btn.dataset.armed; btn.disabled = true;
  const r = cur(), NO = 'not set up';
  const card = pop({ icon: '🚨', title: `Escalating ${r.code}`, steps: ['Marking it as a real loss', 'Finding the forest office and route', 'Sending to Telegram', 'Phoning the response contact', 'Finished'] });
  const fails = [], oks = [];
  try {
    if (!['CONFIRMED', 'FIELD_CHECK', 'REPORTED'].includes(r.status)) await api('/forest-change/records/' + r.id, { method: 'PATCH', body: { status: 'CONFIRMED' } });
    card.note(0, 'Marked as a real loss', 'ok'); card.at(1);
    let full = await api('/forest-change/records/' + r.id);
    const routeText = (f) => `${f.authority.office.name}: ${journeyText(f.route)}`;
    if (full.route?.status === 'OK') card.note(1, routeText(full), 'ok');
    else {
      card.note(1, 'Looking up forest offices and the road route…');
      try { const a = await api(`/forest-change/records/${r.id}/authority`, { method: 'POST', body: {} }); full = await api('/forest-change/records/' + r.id); upd(full); card.note(1, full.route?.status === 'OK' ? routeText(full) : (a.reason || 'No road route found'), full.route?.status === 'OK' ? 'ok' : 'bad'); }
      catch (e) { card.note(1, 'No office found: ' + e.message, 'bad'); }
    }
    card.at(2);
    const r1 = await api(`/forest-change/records/${r.id}/notify`, { method: 'POST', body: { channels: ['dashboard', 'telegram'], mode: 'real', confirm_real: true, resend } });
    const tg = r1.results.find((x) => x.channel === 'telegram');
    if (['SENT', 'DUPLICATE_SUPPRESSED'].includes(tg.status)) { card.note(2, tg.status === 'SENT' ? 'Sent to Telegram' : 'Already sent earlier', 'ok'); oks.push('Telegram'); }
    else { card.note(2, tg.status === 'NOT_CONFIGURED' ? 'Telegram is ' + NO : (tg.error || 'Telegram did not accept it'), 'bad'); fails.push('Telegram'); }
    upd(r1.record); card.at(3);
    const r2 = await api(`/forest-change/records/${r.id}/notify`, { method: 'POST', body: { channels: ['ivr'], mode: 'real', confirm_real: true, resend } });
    const iv = r2.results[0];
    if (iv.status === 'INITIATED') { const f = await followCall(card, 3, iv.provider_call_id); (f.ok ? oks : fails).push('Phone call'); }
    else if (iv.status === 'DUPLICATE_SUPPRESSED') { card.note(3, 'Already phoned earlier', 'ok'); oks.push('Phone call'); }
    else { card.note(3, iv.status === 'NOT_CONFIGURED' ? 'Phone calls are ' + NO : (iv.error || 'The call did not go through'), 'bad'); fails.push('Phone call'); }
    upd(r2.record);
    if (!oks.length) card.fail('Nothing could be sent. See the notes above.');
    else card.done(`${oks.join(' and ')} done${fails.length ? `; ${fails.join(' and ')} did not work` : ''}.`);
  } catch (e) { card.fail(e.status === 403 ? 'Your role is not allowed to do this. Use Reviewer, Officer or Admin.' : e.message); }
  const b = $('#fcEsc'); if (b) b.disabled = false;
}

// ---- permits and removal
async function savePermit() {
  const v = (id) => $('#' + id)?.value.trim() || null;
  const body = { authority: v('pAuth'), permit_number: v('pNo'), project_name: v('pName'), status: $('#pStatus').value, valid_from: v('pFrom'), valid_to: v('pTo'), source_url: v('pUrl') };
  if (!body.authority || !body.permit_number) { $('#pErr').innerHTML = '<div class="banner2 red">The issuing authority and the permit number are needed.</div>'; return; }
  try { await api(`/forest-change/records/${S.sel}/permits`, { method: 'POST', body }); S.permitForm = false; upd(await api('/forest-change/records/' + S.sel)); toast('Permit added'); }
  catch (e) { $('#pErr').innerHTML = `<div class="banner2 red">${esc(e.message)}</div>`; }
}
async function removePermit(pid) {
  try { await api(`/forest-change/records/${S.sel}/permits/${pid}`, { method: 'DELETE' }); upd(await api('/forest-change/records/' + S.sel)); toast('Permit removed'); } catch (e) { toast(e.message, 'error'); }
}
async function removeSpot() {
  try { await api('/forest-change/records/' + S.sel, { method: 'DELETE' }); S.sel = null; clearInterval(S.evPoll); toast('Spot removed'); await reload(); } catch (e) { toast(e.status === 403 ? 'Only a Reviewer or Admin can remove a spot' : e.message, 'error'); }
}

// New construction and land change over the last 365 days (Sentinel-2). Pick a ready-made Jaipur area or draw your own patch,
// see every changed patch coloured by type (road, buildings, industrial…), open one to compare before / after and read its impact,
// and download a report for one change or for the whole check.
import { api, esc, html, raw, toast } from './core.js';
import { pop } from './progress.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const S = { map: null, status: null, runs: [], run: null, changes: [], sel: null, hidden: new Set(), view: 'start', drawn: null, drawing: false, drawKind: null, drawer: null, poll: null, busy: false, layers: {}, showLayer: false, imgs: {}, cmp: 50 };
const role = () => localStorage.getItem('rk.role') || 'reviewer';
const canEdit = () => ['admin', 'reviewer', 'officer', 'analyst'].includes(role());
const D = (iso) => (iso ? new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : '–');
const ha = (v) => `${Number(v ?? 0).toFixed(Number(v) >= 10 ? 1 : 2)} ha`;
const cls = (k) => S.status?.classes?.[k] || { label: k, color: '#868e96' };
const active = () => $('#infraBody') && !$('#infraBody').hidden;

export function init({ map }) {
  S.map = map;
  for (const k of ['poly', 'hl', 'extent', 'drawn']) S.layers[k] = L.layerGroup();
  map.on('draw:created', (e) => {
    if (!S.drawing) return;
    S.drawn = e.layer.toGeoJSON().geometry; S.drawing = false; S.drawKind = null; S.drawer = null;
    if (!(drawnKm2() >= 0.1)) { S.drawn = null; toast('That shape is too small. Draw a larger one.', 'error'); }
    showDrawn(); paint();
  });
  map.on('moveend', () => { if (active() && S.view === 'start' && !S.drawn) updateTarget(); });
  map.on('draw:drawstop', () => { if (S.drawing) { S.drawing = false; S.drawKind = null; S.drawer = null; paint(); } });
  window.addEventListener('rk:tab', (e) => { if (e.detail !== 'infra') cancelDraw(); drawMap(); });
  window.addEventListener('rk:infra-layer', (e) => {
    S.showLayer = !!e.detail.on;
    if (S.showLayer) document.querySelector('#rtabs button[data-t=infra]')?.click(); else drawMap();
  });
  api('/infra/status').then((s) => { S.status = s; paint(); }).catch(() => {});
  load().then(() => { const r = S.runs.find((x) => ['QUEUED', 'RUNNING'].includes(x.status)); if (r) track(r.id, card()); });
}
const card = () => pop({ icon: '🏗', title: 'Looking for new construction', steps: ['Finding satellite images', 'Comparing a year ago with now', 'Sorting the changes by type', 'Done'] });

// ------------------------------------------------------------------ data
async function load() {
  try { S.runs = await api('/infra/runs?limit=30'); } catch (e) { S.runs = []; }
  if (S.run && !S.runs.find((r) => r.id === S.run.id)) { S.run = null; S.changes = []; S.sel = null; S.view = 'start'; }
  paint(); drawMap();
}
async function open(id, fit = true) {
  const run = S.runs.find((r) => r.id === id) || (await api('/infra/runs/' + id));
  S.run = run; S.sel = null; S.view = 'list'; S.hidden = new Set();
  try { S.changes = await api(`/infra/runs/${id}/changes?include_hidden=true`); } catch (e) { toast(e.message, 'error'); S.changes = []; }
  paint(); drawMap();
  if (fit && run.bbox) S.map.fitBounds([[run.bbox[1], run.bbox[0]], [run.bbox[3], run.bbox[2]]], { padding: [30, 30] });
}
const openOnes = () => S.changes.filter((c) => c.status !== 'NOT_A_CHANGE');
const setAside = () => S.changes.filter((c) => c.status === 'NOT_A_CHANGE');
const visible = () => openOnes().filter((c) => !S.hidden.has(c.cls));

// ------------------------------------------------------------------ map
function drawMap() {
  const on = active() || S.showLayer;
  for (const k of Object.keys(S.layers)) { if (on) S.layers[k].addTo(S.map); else S.map.removeLayer(S.layers[k]); }
  S.layers.poly.clearLayers(); S.layers.hl.clearLayers(); S.layers.extent.clearLayers();
  if (!on || !S.run) return;
  if (S.run.bbox) L.rectangle([[S.run.bbox[1], S.run.bbox[0]], [S.run.bbox[3], S.run.bbox[2]]], { color: '#0a5c36', weight: 1.5, dashArray: '6 6', fill: false, interactive: false }).addTo(S.layers.extent);
  for (const c of visible()) {
    const sel = c.id === S.sel;
    L.geoJSON(c.geometry, { style: { color: sel ? '#111' : c.color, weight: sel ? 3 : 2, fillColor: c.color, fillOpacity: sel ? 0.55 : 0.38 } })
      .bindTooltip(`${c.code} · ${c.cls_label} · ${ha(c.area_ha)}`, { sticky: true }).on('click', () => select(c.id)).addTo(S.layers.poly);
  }
  const c = S.changes.find((x) => x.id === S.sel);
  if (c) L.circleMarker([c.lat, c.lon], { radius: 16, color: '#111', weight: 2, fill: false, interactive: false }).addTo(S.layers.hl);
}
const bboxKm2 = (b) => Math.abs(b[2] - b[0]) * 111.32 * Math.cos((((b[1] + b[3]) / 2) * Math.PI) / 180) * Math.abs(b[3] - b[1]) * 110.57;
const drawnKm2 = () => { const rings = S.drawn.type === 'Polygon' ? [S.drawn.coordinates] : S.drawn.coordinates; return rings.reduce((t, r) => t + L.GeometryUtil.geodesicArea(r[0].map(([x, y]) => L.latLng(y, x))), 0) / 1e6; };
function showDrawn() {
  S.layers.drawn.clearLayers();
  if (S.drawn) L.geoJSON(S.drawn, { style: { color: '#0a5c36', weight: 2.5, dashArray: '7 5', fillColor: '#0a5c36', fillOpacity: 0.06 }, interactive: false }).addTo(S.layers.drawn);
}
function cancelDraw() { S.drawer?.disable(); S.drawer = null; S.drawing = false; S.drawKind = null; }
function startDraw(kind) {
  if (S.drawing && S.drawKind === kind) { cancelDraw(); paint(); return; }   // pressing the same tool again puts it down
  S.drawer?.disable(); S.drawn = null; S.drawing = true; S.drawKind = kind; showDrawn();
  const style = { color: '#0a5c36', weight: 2.5, fillOpacity: 0.08 };
  S.drawer = kind === 'rect' ? new L.Draw.Rectangle(S.map, { shapeOptions: style }) : new L.Draw.Polygon(S.map, { allowIntersection: false, shapeOptions: style, showArea: false });
  S.drawer.enable(); paint(); toast(kind === 'rect' ? 'Click and drag on the map to draw a rectangle. Press Esc to stop' : 'Click each corner, then click the first point to finish. Press Esc to stop');
}
function updateTarget() {   // the "map view" area changes as you pan and zoom: refresh just the size line and the button
  const el = $('#iArea'), btn = $('#icheck'); if (!el || !btn) return;
  const st = S.status, t = target(), max = st?.max_area_km2 || 450, sharp = st?.sharp_area_km2 || 130, min = st?.min_area_km2 || 0.3, ok = t.km2 >= min && t.km2 <= max;
  el.textContent = `${t.km2 > max ? `Over ${max} km²` : t.km2.toFixed(t.km2 < 10 ? 1 : 0) + ' km²'} · ${t.km2 > max ? 'too large: zoom in or draw a smaller area' : t.km2 < min ? 'too small' : t.km2 <= sharp ? 'sharp view (10 m)' : 'wide view (20 m)'}`;
  el.style.color = ok ? '' : '#b3122a'; btn.disabled = !(ok && !S.busy && !S.drawing && canEdit());
}
function target() {   // what the next check would cover
  if (S.drawn) { const b = L.geoJSON(S.drawn).getBounds(); return { bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], polygon: S.drawn, km2: drawnKm2() }; }
  const b = S.map.getBounds(); const bb = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  return { bbox: bb, polygon: null, km2: bboxKm2(bb) };
}

// ------------------------------------------------------------------ panel
export function paint() {
  const box = $('#infraBody'); if (!box) return;
  if (S.view === 'detail' && S.sel) return paintDetail(box);
  if (S.view === 'list' && S.run) return paintList(box);
  paintStart(box);
}
const runFor = (preset) => S.runs.find((r) => r.preset === preset && r.status === 'COMPLETE');
const label = (s) => ({ COMPLETE: 'Done', RUNNING: 'Checking…', QUEUED: 'Waiting…', FAILED: 'Failed', CANCELLED: 'Cancelled' }[s] || s);

function paintStart(box) {
  const st = S.status, t = S.map ? target() : { km2: 0 }, max = st?.max_area_km2 || 450, sharp = st?.sharp_area_km2 || 130;
  const ok = t.km2 >= (st?.min_area_km2 || 0.3) && t.km2 <= max;
  const presets = Object.entries(st?.presets || {});
  const past = S.runs;
  box.innerHTML = html`<div class="side-head" style="padding-left:0"><div><h2>New construction</h2><div class="sub">What has been built or cleared in the last 365 days</div></div></div>
    ${raw(S.busy ? `<div class="scanprog"><div id="iprog"><div class="progress"><div style="width:4%"></div></div><div class="sub">Starting…</div></div><div class="row2"><button class="btn sm danger" id="icancel">✕ Cancel this check</button></div></div>` : '')}
    <h3 class="grp">Jaipur · ready to check</h3>
    <div class="list">${raw(presets.map(([k, p]) => { const r = runFor(k); return `<div class="row-item" style="cursor:default"><span style="font-size:20px">🏙</span><span class="grow"><b>${esc(p.name)}</b><small>${p.area_km2} km²${r?.summary ? ` · ${r.summary.patches} changes found` : ''}</small></span>${r ? `<button class="btn sm primary" data-open="${esc(r.id)}">View</button>` : `<button class="btn sm primary" data-preset="${esc(k)}" ${S.busy ? 'disabled' : ''}>Check</button>`}</div>`; }).join(''))}</div>
    <h3 class="grp">Or choose your own patch</h3>
    <div class="sub">Draw a rectangle or any shape anywhere in India, or use what the map shows now.</div>
    <div class="row2" style="margin-top:8px"><button class="btn sm ${S.drawing && S.drawKind === 'rect' ? 'primary' : ''}" id="iRect">${S.drawing && S.drawKind === 'rect' ? '✕ Stop drawing' : '▭ Draw a rectangle'}</button><button class="btn sm ${S.drawing && S.drawKind === 'poly' ? 'primary' : ''}" id="iPoly">${S.drawing && S.drawKind === 'poly' ? '✕ Stop drawing' : '⬠ Draw a shape'}</button>${raw(S.drawn ? '<button class="btn sm" id="iClear">✕ Clear shape</button>' : '')}</div>
    <div class="sub" style="margin-top:6px"><b id="iArea" style="${ok ? '' : 'color:#b3122a'}">${t.km2 > max ? `Over ${max} km²` : t.km2.toFixed(t.km2 < 10 ? 1 : 0) + ' km²'} · ${t.km2 > max ? 'too large: zoom in or draw a smaller area' : t.km2 < (st?.min_area_km2 || 0.3) ? 'too small' : t.km2 <= sharp ? 'sharp view (10 m)' : 'wide view (20 m)'}</b>${S.drawn ? '' : ' (the map view)'}</div>
    <button class="btn primary big" id="icheck" ${ok && !S.busy && !S.drawing && canEdit() ? '' : 'disabled'}>🏗 Check this area</button>
    <h3 class="grp">Past checks</h3>
    ${raw(past.length ? `<div class="list">${past.map((r) => `<div class="row-item" data-run="${esc(r.id)}"><span style="font-size:18px">${r.status === 'COMPLETE' ? '🗂' : r.status === 'FAILED' ? '⚠' : '⏳'}</span><span class="grow"><b>${esc(r.name || 'Area')}</b><small>${esc(D(r.created_at.slice(0, 10)))} · ${r.summary ? `${r.summary.patches} changes · ${ha(r.summary.total_ha)}` : esc(label(r.status))}</small></span>${r.status === 'COMPLETE' ? '<span class="chip">Open</span>' : ''}</div>`).join('')}</div>` : '<div class="sub">None yet.</div>')}
    <details class="more" style="margin-top:10px"><summary>How this works</summary><div class="sub" style="margin-top:6px">Four clear Sentinel-2 satellite pictures are compared: a year ago, now, and two in between. Ground is reported only if it was green or dark soil a year ago and has not turned green again, so a harvested field is not mistaken for construction. Pixels are 10 m, so narrow roads and single houses can be missed.</div></details>`;
  $('#iRect', box).onclick = () => startDraw('rect'); $('#iPoly', box).onclick = () => startDraw('poly');
  $('#iClear', box)?.addEventListener('click', () => { S.drawn = null; showDrawn(); paint(); });
  $('#icheck', box).onclick = () => { const x = target(); startRun({ bbox: x.bbox, polygon: x.polygon, name: x.polygon ? 'Shape I drew' : 'Area I chose' }); };
  $('#icancel', box)?.addEventListener('click', cancelRun);
  $$('[data-preset]', box).forEach((b) => (b.onclick = () => startRun({ preset: b.dataset.preset })));
  $$('[data-open]', box).forEach((b) => (b.onclick = () => open(b.dataset.open)));
  $$('.row-item[data-run]', box).forEach((b) => (b.onclick = () => { const r = S.runs.find((x) => x.id === b.dataset.run); if (r?.status === 'COMPLETE') open(r.id); else if (r?.error) toast(r.error, 'error'); }));
}

function paintList(box) {
  const r = S.run, sm = r.summary || {};
  const live = openOnes(), rows = visible(), aside = setAside();
  const by = {}; for (const c of live) { const e = (by[c.cls] ||= { n: 0, ha: 0 }); e.n += 1; e.ha += c.area_ha; }
  const sum = (f) => live.reduce((t, c) => t + (f(c) || 0), 0);
  box.innerHTML = html`<button class="back" id="iback">‹ All checks</button>
    <div class="side-head" style="padding-left:0"><div><h2>${r.name || 'Checked area'}</h2><div class="sub">${D(sm.before)} → ${D(sm.after)} · ${sm.area_km2} km² at ${sm.resolution_m} m</div></div></div>
    <div class="stat4"><div><b>${live.length}</b><span>changes</span></div><div><b>${ha(sum((c) => c.area_ha))}</b><span>changed</span></div><div><b>${ha(sum((c) => c.impact?.vegetation_lost_ha))}</b><span>green lost</span></div><div><b>${ha(sum((c) => c.impact?.hard_surface_added_ha))}</b><span>hard surface</span></div></div>
    <div class="chips" id="classChips">${raw(Object.entries(by).sort((a, b) => b[1].ha - a[1].ha).map(([k, v]) => `<button class="cchip ${S.hidden.has(k) ? 'off' : ''}" data-cls="${esc(k)}" title="Show or hide"><i style="background:${esc(cls(k).color)}"></i>${esc(cls(k).label)} <b>${v.n}</b></button>`).join(''))}</div>
    <div class="list" id="chlist">${raw(rows.length ? rows.map((c) => `<button class="row-item" data-id="${esc(c.id)}"><span class="dot2" style="background:${esc(c.color)};width:14px;height:14px"></span><span class="grow"><b>${esc(c.code)} · ${esc(c.cls_label)}</b><small>${esc(c.name || '')}${c.name ? ' · ' : ''}${ha(c.area_ha)}${c.impact?.vegetation_lost_ha >= 0.05 ? ` · 🌿 ${ha(c.impact.vegetation_lost_ha)} lost` : ''}</small></span></button>`).join('') : '<div class="empty"><b>Nothing to show</b>No changes of the types you have switched on.</div>')}</div>
    ${raw(aside.length ? `<details class="more" id="aside"><summary>Set aside as not a change (${aside.length})</summary><div class="list" style="margin-top:6px">${aside.map((c) => `<div class="row-item" style="cursor:default"><span class="dot2" style="background:${esc(c.color)};width:12px;height:12px"></span><span class="grow"><b>${esc(c.code)} · ${esc(c.cls_label)}</b><small>${ha(c.area_ha)}</small></span><button class="btn sm" data-back="${esc(c.id)}">↩ Put back</button></div>`).join('')}</div></details>` : '')}
    <div class="sect"><h4>Report</h4><div class="sub">Every change with before and after pictures, impact and map links.</div><div class="row2" style="margin-top:8px"><button class="btn grow" id="rview">👁 View</button><button class="btn primary grow" id="rpdf">⬇ Download</button></div><div class="row2" style="margin-top:6px"><button class="btn sm" id="rgeo">⬇ GeoJSON</button><button class="btn sm danger" id="rdel">− Remove this check</button></div></div>`;
  $('#iback', box).onclick = () => { S.run = null; S.changes = []; S.sel = null; S.view = 'start'; paint(); drawMap(); };
  $$('.cchip', box).forEach((b) => (b.onclick = () => { const k = b.dataset.cls; S.hidden.has(k) ? S.hidden.delete(k) : S.hidden.add(k); paint(); drawMap(); }));
  $$('.row-item[data-id]', box).forEach((b) => (b.onclick = () => select(b.dataset.id)));
  $$('[data-back]', box).forEach((b) => (b.onclick = () => mark(S.changes.find((x) => x.id === b.dataset.back), 'OPEN')));
  $('#rview', box).onclick = (e) => viewPdf(`/infra/runs/${r.id}/report.pdf`, 'Infrastructure change report', e.target);
  $('#rpdf', box).onclick = () => import('./core.js').then((c) => c.download(`/infra/runs/${r.id}/report.pdf`, `infrastructure-change-${r.id.slice(0, 8)}.pdf`));
  $('#rgeo', box).onclick = () => import('./core.js').then((c) => c.download(`/infra/runs/${r.id}/export.geojson`, `infrastructure-change-${r.id.slice(0, 8)}.geojson`));
  $('#rdel', box).onclick = (e) => { const b = e.target; if (!b.dataset.armed) { b.dataset.armed = '1'; b.textContent = 'Tap again to remove'; setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = '− Remove this check'; } }, 4000); return; } removeRun(r.id); };
}

async function select(id) {
  S.sel = id; S.view = 'detail'; S.cmp = 50;
  const c = S.changes.find((x) => x.id === id); if (!c) return;
  if (!c.evidence) { try { Object.assign(c, await api('/infra/changes/' + id)); } catch (e) { toast(e.message, 'error'); } }
  document.body.classList.remove('hide-right');
  paint(); drawMap();
  const b = L.geoJSON(c.geometry).getBounds(); S.map.fitBounds(b.pad(2.5), { maxZoom: 18 });
  loadImages(c);
}
async function loadImages(c) {
  if (!S.imgs[c.id]) {
    try {
      const [b, a] = await Promise.all([api(`/infra/changes/${c.id}/image/before.jpg`, { blob: true }), api(`/infra/changes/${c.id}/image/after.jpg`, { blob: true })]);
      S.imgs[c.id] = { b: URL.createObjectURL(b), a: URL.createObjectURL(a) };
    } catch (e) { S.imgs[c.id] = { err: e.message }; }
  }
  if (S.sel === c.id) paint();
}

function paintDetail(box) {
  const c = S.changes.find((x) => x.id === S.sel); if (!c) return paintList(box);
  const imp = c.impact || {}, ef = imp.effects || {}, ev = c.evidence || {}, im = S.imgs[c.id], sm = S.run?.summary || {};
  const gone = c.status === 'NOT_A_CHANGE';
  box.innerHTML = html`<button class="back" id="dback">‹ All changes</button>
    <div class="row-item" style="border:0;padding:6px 0"><span class="dot2" style="background:${c.color};width:14px;height:14px"></span><span class="grow"><h2 style="font-size:18px">${c.code} · ${c.cls_label}</h2><div class="sub">${c.name ? c.name + ' · ' : ''}${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}</div></span><span class="chip">${ha(c.area_ha)}</span></div>
    ${raw(gone ? '<div class="banner2 grey"><b>Set aside as not a change.</b> It is hidden from the map and the report.</div>' : '')}
    <div class="cmp" id="cmp">${raw(!im ? '<div class="sub" style="padding:30px;text-align:center">Loading the satellite pictures…</div>' : im.err ? `<div class="sub" style="padding:20px">${esc(im.err)}</div>` : `<img src="${im.a}" alt="After"><img class="top" id="cmpTop" src="${im.b}" alt="Before" style="clip-path:inset(0 ${100 - S.cmp}% 0 0)"><span class="cl l">Before · ${esc(D(sm.before))}</span><span class="cl r">After · ${esc(D(sm.after))}</span><span class="cbar" id="cbar" style="left:${S.cmp}%"></span><input type="range" id="cslide" min="0" max="100" value="${S.cmp}" aria-label="Drag to compare before and after">`)}</div>
    <div class="sub" style="margin-top:4px">Drag the handle: left is a year ago, right is now. The outline is the changed patch. Sentinel-2, ${ev.resolution_m || 10} m pixels.</div>
    <div class="sect"><h4>What was found</h4><div class="sub"><b>${c.cls_label}</b> · ${c.class_source}</div>
      <div class="sub">${ev.detected_as ? 'Seen as: ' + ev.detected_as + '. ' : ''}${ev.length_m ? `About ${ev.length_m} m long and ${ev.width_m} m wide.` : ''}</div></div>
    <div class="sect"><h4>Impact (measured)</h4><ul class="eff">${raw((ef.measured || []).map((x) => `<li><i>${x.icon}</i><span>${esc(x.text)}</span></li>`).join('') || '<li>Nothing measured.</li>')}</ul>
      ${raw(imp.land_before ? `<div class="sub" style="margin-top:6px">Land before (ESA WorldCover 2021): ${Object.entries(imp.land_before).map(([k, v]) => `${esc(k)} ${v} ha`).join(' · ')}</div>` : '')}</div>
    <details class="more"><summary>Typical effects of this kind of change</summary><ul class="eff" style="margin-top:8px">${raw((ef.typical || []).map((x) => `<li><i>${x.icon}</i><span>${esc(x.text)}</span></li>`).join('') || '<li>None listed.</li>')}</ul><div class="sub">General knowledge, not measured here.</div></details>
    <div class="sect"><h4>Report</h4><div class="sub">Before and after pictures, impact, evidence and clickable links.</div><div class="row2" style="margin-top:8px"><button class="btn grow" id="cview">👁 View</button><button class="btn primary grow" id="cpdf">⬇ Download</button></div>
      <div class="row2" style="margin-top:6px"><a class="btn sm" href="https://www.google.com/maps?q=${c.lat},${c.lon}" target="_blank" rel="noopener">📍 Google Maps</a>${raw(canEdit() ? (gone ? '<button class="btn sm" id="cundo">↩ Undo, it is a change</button>' : '<button class="btn sm" id="cnot">✕ Not a change</button>') : '')}</div></div>`;
  $('#dback', box).onclick = () => { S.sel = null; S.view = 'list'; paint(); drawMap(); };
  const sl = $('#cslide', box);
  if (sl) sl.oninput = () => { S.cmp = Number(sl.value); $('#cmpTop', box).style.clipPath = `inset(0 ${100 - S.cmp}% 0 0)`; $('#cbar', box).style.left = S.cmp + '%'; };
  $('#cview', box).onclick = (e) => viewPdf(`/infra/changes/${c.id}/report.pdf`, `${c.code} report`, e.target);
  $('#cpdf', box).onclick = () => import('./core.js').then((x) => x.download(`/infra/changes/${c.id}/report.pdf`, `${c.code}-report.pdf`));
  $('#cnot', box)?.addEventListener('click', () => mark(c, 'NOT_A_CHANGE'));
  $('#cundo', box)?.addEventListener('click', () => mark(c, 'OPEN'));
}
async function mark(c, status) {
  try {
    await api('/infra/changes/' + c.id, { method: 'PATCH', body: { status } });
    c.status = status; S.sel = null; S.view = 'list';
    toast(status === 'OPEN' ? 'Put back in the list' : 'Set aside as not a change. You can put it back under the list.');
    paint(); drawMap();
  } catch (e) { toast(e.message, 'error'); }
}

// ------------------------------------------------------------------ running a check
async function startRun(body) {
  try { const r = await api('/infra/runs', { method: 'POST', body }); S.busy = true; paint(); await load(); track(r.run_id, card()); }
  catch (e) { toast(e.message, 'error'); }
}
async function cancelRun() { const r = S.runs.find((x) => ['QUEUED', 'RUNNING'].includes(x.status)); if (!r) return; try { await api(`/infra/runs/${r.id}/cancel`, { method: 'POST' }); toast('Check cancelled'); } catch (e) { toast(e.message, 'error'); } }
async function removeRun(id) {
  try { await api('/infra/runs/' + id, { method: 'DELETE' }); toast('Check removed'); S.run = null; S.changes = []; S.sel = null; S.view = 'start'; await load(); }
  catch (e) { toast(e.message, 'error'); }
}
function track(id, c) {
  S.busy = true; paint(); clearInterval(S.poll);
  const tick = async () => {
    const r = await api('/infra/runs/' + id).catch(() => null); if (!r) return;
    c?.at(r.progress < 0.08 ? 0 : r.progress < 0.78 ? 1 : 2);
    const last = r.steps[r.steps.length - 1], el = $('#iprog');
    if (el) el.innerHTML = `<div class="progress"><div style="width:${Math.round((r.progress || 0.02) * 100)}%"></div></div><div class="sub">${esc(last ? last.message : 'Starting…')}</div>`;
    c?.note(r.progress < 0.78 ? 1 : 2, last ? last.message : '');
    if (r.status === 'CANCELLED') { clearInterval(S.poll); S.busy = false; c?.fail('Check cancelled'); await load(); return; }
    if (r.status !== 'COMPLETE' && r.status !== 'FAILED') return;
    clearInterval(S.poll); S.busy = false;
    if (r.status === 'FAILED') { c?.fail(r.error || 'The check failed.'); await load(); const e2 = $('#iprog'); if (e2) e2.innerHTML = `<div class="banner2 red">${esc(r.error || 'The check failed')}</div>`; return; }
    c?.done(`${r.summary.patches} change${r.summary.patches === 1 ? '' : 's'} found, ${r.summary.total_ha} ha in total.`);
    await load(); await open(id);
  };
  S.poll = setInterval(tick, 3000); tick();
}

// ------------------------------------------------------------------ reports shown inside the page
async function viewPdf(path, title, btn) {
  const t = btn?.textContent; if (btn) { btn.disabled = true; btn.textContent = 'Preparing…'; }
  try {
    const b = await api(path, { blob: true }), url = URL.createObjectURL(b);
    document.getElementById('rptView')?.remove();
    const el = document.createElement('div'); el.id = 'rptView'; el.className = 'rptview';
    el.innerHTML = `<div class="rptbar"><b>${esc(title)}</b><span class="grow"></span><a class="btn sm primary" href="${url}" download="report.pdf">⬇ Download</a><button class="btn sm" id="rptClose">✕ Close</button></div><iframe title="Report" src="${url}"></iframe>`;
    document.body.appendChild(el);
    const close = () => { el.remove(); URL.revokeObjectURL(url); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey); el.querySelector('#rptClose').onclick = close;
  } catch (e) { toast(e.message, 'error'); } finally { if (btn) { btn.disabled = false; btn.textContent = t; } }
}

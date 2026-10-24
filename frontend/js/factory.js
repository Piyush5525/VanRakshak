// Factory check for the Reports tab: place a factory on the map, describe it (or upload its PDF), and see what it does to the
// forest, air, water and people around it, now and 7 / 30 / 90 days after work starts.
import { api, esc, html, raw, toast, waitJob } from './core.js';
import { pop } from './progress.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const LEVEL = { high: ['High', '#d7263d'], medium: ['Medium', '#f4802a'], low: ['Low', '#2e9e5b'] };
const S = { map: null, layer: null, pin: null, pos: null, opts: null, form: blank(), mode: 'form', doc: null, res: null, day: 0, on: { forest: true, edge: true, smoke: true, rivers: true, villages: true, reserves: true }, timer: null, back: null, placing: false };
const fmt = (n, d = 1) => (n == null ? '–' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: d }));
function blank() { return { name: '', kind: 'cement', footprint_ha: '', stack_height_m: '', water_kld: '', emissions: [], clearing_days: '', operation_start_day: '' }; }

export function init({ map }) {
  S.map = map; S.layer = L.layerGroup().addTo(map);
  window.addEventListener('rk:tab', (e) => { if (e.detail !== 'inc') clear(); });
  map.on('click', (e) => { if (S.placing) place(e.latlng.lat, e.latlng.lng); });
}
export function clear() { S.layer?.clearLayers(); S.pin = null; S.res = null; S.placing = false; document.body.classList.remove('placing'); clearInterval(S.timer); }

// ------------------------------------------------------------------ entry points shown in the Reports tab
export function mountEntry(el, onOpen) {
  el.innerHTML = `<button class="fac-entry" id="facNew"><span class="fac-ic">🏭</span><span class="grow"><b>Check a factory</b><small>Place it on the map, describe it or upload its PDF, and see what it does to the forest, air and water.</small></span><span class="fac-go">›</span></button>`;
  $('#facNew', el).onclick = onOpen;
}
export async function mountList(el, onBack) {
  try {
    const list = await api('/factory/checks');
    el.innerHTML = list.length ? list.map((c) => `<button class="row-item" data-id="${esc(c.id)}"><span style="font-size:22px">🏭</span><span class="grow"><b>${esc(c.name)}</b><small>${esc(c.kind_label || c.kind)} · ${c.status === 'COMPLETE' ? Object.entries(c.levels).filter(([, v]) => v === 'high').map(([k]) => k).join(', ') || 'no high concerns' : esc(c.status.toLowerCase())}</small></span></button>`).join('') : '';
    $$('.row-item', el).forEach((b) => (b.onclick = () => openCheck(b.dataset.id, onBack)));
  } catch { el.innerHTML = ''; }
}

// ------------------------------------------------------------------ the form
export async function start(back) {
  S.back = back; S.res = null; S.doc = null; S.mode = 'form'; S.form = blank(); clear();
  if (!S.opts) S.opts = await api('/factory/options').catch(() => ({ kinds: { other: 'Other industry' }, max_pdf_mb: 25 }));
  const c = S.map.getCenter(); S.pos = null;
  paintForm();
}
function setHint() { document.body.classList.toggle('placing', S.placing); }
function place(lat, lon) {
  S.pos = { lat, lon }; S.placing = false; setHint();
  if (S.pin) S.pin.setLatLng([lat, lon]);
  else S.pin = L.marker([lat, lon], { draggable: true, icon: L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 34], html: '<div class="case-pin" style="font-size:30px">🏭</div>' }) }).on('dragend', (e) => { const p = e.target.getLatLng(); S.pos = { lat: p.lat, lon: p.lng }; paintPos(); }).addTo(S.layer);
  paintPos();
}
function paintPos() {
  const el = $('#facPos'); if (!el) return;
  el.innerHTML = S.pos ? `<b>${S.pos.lat.toFixed(5)}, ${S.pos.lon.toFixed(5)}</b> <span class="sub">drag the pin to adjust</span>` : '<span class="sub">Not placed yet</span>';
  const go = $('#facRun'); if (go) go.disabled = !S.pos || !$('#fName').value.trim();
}
function readForm() {
  const f = S.form;
  f.name = $('#fName').value.trim(); f.kind = $('#fKind').value;
  for (const [id, k] of [['fArea', 'footprint_ha'], ['fStack', 'stack_height_m'], ['fWater', 'water_kld'], ['fClear', 'clearing_days'], ['fOp', 'operation_start_day']]) f[k] = $('#' + id).value;
  f.emissions = $$('.fem').map((r) => ({ pollutant: $('.p', r).value.trim(), value: $('.v', r).value, unit: $('.u', r).value.trim(), source: $('.s', r).value.trim() })).filter((x) => x.pollutant || x.value !== '');
}
const field = (id, label, val, ph, type = 'text', extra = '') => `<label class="f">${label}<input id="${id}" type="${type}" value="${esc(val ?? '')}" placeholder="${esc(ph)}" ${extra}></label>`;
function paintForm() {
  const box = $('#rightBody'), f = S.form, d = S.doc;
  const found = (k) => (d?.fields?.[k] ? `<span class="found" title="${esc(d.fields[k].snippet || '')}">from document${d.fields[k].page ? ', p.' + d.fields[k].page : ''}</span>` : '');
  box.innerHTML = html`<button class="back" id="fback">‹ Back</button>
    <div class="side-head" style="padding:0"><div><h2>Check a factory</h2><div class="sub">What it would do to the forest and its surroundings</div></div></div>
    <div class="step"><span class="n">1</span><b>Where will it be?</b></div>
    <div class="row2"><button class="btn ${S.placing ? 'primary' : ''}" id="fPlace">📍 ${S.placing ? 'Click the map…' : 'Place on the map'}</button><button class="btn sm" id="fMid">Use map centre</button></div>
    <div id="facPos" style="margin:6px 0"></div>
    <div class="step"><span class="n">2</span><b>Tell us about it</b></div>
    <div class="pills3" id="fMode"><button data-m="form" class="${S.mode === 'form' ? 'on' : ''}">Fill in</button><button data-m="pdf" class="${S.mode === 'pdf' ? 'on' : ''}">Upload a PDF</button></div>
    ${raw(S.mode === 'pdf' ? `<label class="drop" id="fDrop"><input type="file" id="fFile" accept="application/pdf,.pdf" hidden><span class="drop-ic">📄</span><b>${d ? esc(d.filename) : 'Drop a PDF here or tap to choose'}</b><small>${d ? `${d.pages_read} page${d.pages_read === 1 ? '' : 's'} read · tap to replace` : 'Project report, EIA summary, permit or brochure · up to ' + (S.opts?.max_pdf_mb || 25) + ' MB'}</small></label><div id="fRead"></div>` : '')}
    ${raw(d ? `<div class="found-list"><b>Found in the document</b>${d.warnings.map((w) => `<div class="banner2 amber">${esc(w)}</div>`).join('')}${Object.keys(d.fields).length ? '' : '<div class="sub">Nothing usable was found. Fill the details in by hand.</div>'}${d.mentions?.length ? `<div class="chips">${d.mentions.map((m) => `<span class="chip">${esc(m)}</span>`).join('')}</div>` : ''}${d.fields.coordinates ? `<button class="btn sm" id="fUseLoc">📍 Place it where the document says (${d.fields.coordinates.value.lat.toFixed(4)}, ${d.fields.coordinates.value.lon.toFixed(4)})</button>` : d.fields.place ? `<div class="sub">The document names ${esc(Object.values(d.fields.place.value).join(', '))}. Place the pin there on the map.</div>` : ''}</div>` : '')}
    <div class="grid2">
      ${raw(field('fName', 'Name ' + found('name'), f.name, 'e.g. Sunrise Cement Unit'))}
      <label class="f">Type ${raw(found('kind'))}<select id="fKind">${raw(Object.entries(S.opts.kinds).map(([k, n]) => `<option value="${esc(k)}" ${f.kind === k ? 'selected' : ''}>${esc(n)}</option>`).join(''))}</select></label>
      ${raw(field('fArea', 'Site area (ha) ' + found('footprint_ha'), f.footprint_ha, 'typical for this type', 'number', 'min="0" step="any"'))}
      ${raw(field('fStack', 'Chimney height (m) ' + found('stack_height_m'), f.stack_height_m, 'optional', 'number', 'min="0" step="any"'))}
      ${raw(field('fWater', 'Water use (KLD) ' + found('water_kld'), f.water_kld, 'optional', 'number', 'min="0" step="any"'))}
    </div>
    <details class="more" ${f.emissions.length || f.clearing_days || f.operation_start_day ? 'open' : ''}><summary>More details (optional)</summary>
      <div class="sub" style="margin:6px 0">Emission figures you can source. Nothing is estimated for you.</div>
      <div id="fEms">${raw(f.emissions.map(emRow).join(''))}</div><button class="btn sm" id="fAddEm">＋ Add a figure</button>
      <div class="grid2" style="margin-top:8px">${raw(field('fClear', 'Clearing takes (days)', f.clearing_days, '45', 'number', 'min="1"'))}${raw(field('fOp', 'Starts running on day', f.operation_start_day, '60', 'number', 'min="0"'))}</div></details>
    <button class="btn primary big" id="facRun" disabled>Check the effect</button><div id="facErr"></div>`;
  paintPos();
  $('#fback').onclick = () => { clear(); S.back?.(); };
  $('#fPlace').onclick = () => { S.placing = !S.placing; setHint(); paintForm(); if (S.placing) toast('Click the map where the factory will go'); };
  $('#fMid').onclick = () => { const c = S.map.getCenter(); place(c.lat, c.lng); };
  $('#fMode').onclick = (e) => { const m = e.target.dataset.m; if (m) { readForm(); S.mode = m; paintForm(); } };
  $('#fFile')?.addEventListener('change', (e) => upload(e.target.files[0]));
  const drop = $('#fDrop'); if (drop) { drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); }; drop.ondragleave = () => drop.classList.remove('over'); drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files[0]); }; }
  $('#fUseLoc')?.addEventListener('click', () => { const c = S.doc.fields.coordinates.value; place(c.lat, c.lon); S.map.flyTo([c.lat, c.lon], 14); });
  $('#fAddEm').onclick = () => { readForm(); S.form.emissions.push({ pollutant: '', value: '', unit: '', source: '' }); paintForm(); };
  $$('#rightBody input, #rightBody select').forEach((i) => i.addEventListener('input', () => { const go = $('#facRun'); if (go) go.disabled = !S.pos || !$('#fName').value.trim(); }));
  $('#facRun').onclick = run;
}
const emRow = (e) => `<div class="fem"><input class="p" placeholder="Pollutant" value="${esc(e.pollutant)}"><input class="v" type="number" step="any" placeholder="Value" value="${esc(e.value)}"><input class="u" placeholder="Unit" value="${esc(e.unit)}"><input class="s" placeholder="Source (permit, report page)" value="${esc(e.source)}"></div>`;

async function upload(file) {
  if (!file) return;
  readForm();
  const card = pop({ icon: '📄', title: 'Reading the document', steps: ['Uploading', 'Reading the text', 'Finding the details'] });
  try {
    const fd = new FormData(); fd.append('file', file);
    card.at(1);
    const d = await api('/factory/extract', { method: 'POST', form: fd });
    card.at(2); S.doc = d;
    const f = d.fields, g = S.form;
    if (f.name && !g.name) g.name = f.name.value;
    if (f.kind) g.kind = f.kind.value;
    if (f.footprint_ha) g.footprint_ha = f.footprint_ha.value;
    if (f.stack_height_m) g.stack_height_m = f.stack_height_m.value;
    if (f.water_kld) g.water_kld = f.water_kld.value;
    if (f.emissions) g.emissions = f.emissions.value.map((e) => ({ pollutant: e.pollutant, value: e.value, unit: e.unit, source: e.source }));
    if (f.coordinates && !S.pos) place(f.coordinates.value.lat, f.coordinates.value.lon);
    card.done(`${Object.keys(f).length} detail${Object.keys(f).length === 1 ? '' : 's'} found. Please check them.`);
    paintForm();
  } catch (e) { card.fail(e.message); }
}

async function run() {
  readForm();
  const f = S.form, n = (v) => (v === '' || v == null ? null : Number(v));
  const body = { name: f.name, kind: f.kind, lat: S.pos.lat, lon: S.pos.lon, footprint_ha: n(f.footprint_ha), stack_height_m: n(f.stack_height_m), water_kld: n(f.water_kld), clearing_days: n(f.clearing_days), operation_start_day: n(f.operation_start_day), source: S.doc ? 'pdf' : 'form', document: S.doc ? { filename: S.doc.filename, sha256: S.doc.sha256 } : null,
    emissions: f.emissions.filter((e) => e.pollutant && e.value !== '' && e.unit && e.source.length >= 2).map((e) => ({ ...e, value: Number(e.value) })) };
  const btn = $('#facRun'); btn.disabled = true; btn.textContent = 'Checking…';
  const card = pop({ icon: '🏭', title: `Checking ${f.name}`, steps: ['Reading land cover', 'Finding rivers, villages and reserves', 'Working out wind and smoke zones', 'Writing the check'] });
  try {
    const j = await api('/factory/checks', { method: 'POST', body });
    let k = 0; const tick = setInterval(() => { k++; card.at(Math.min(3, Math.floor(k / 2))); }, 3500);
    try { await waitJob(j.job_id, null, 2000, 6 * 60 * 1000); } finally { clearInterval(tick); }
    const c = await api('/factory/checks/' + j.id);
    if (c.result?.status !== 'ok') throw new Error(c.result?.reason || 'The check could not be completed');
    card.done('The check is ready.'); S.res = c; S.day = 90; show();
  } catch (e) { card.fail(e.message); btn.disabled = false; btn.textContent = 'Check the effect'; $('#facErr').innerHTML = `<div class="banner2 red">${esc(e.message)}</div>`; }
}

export async function openCheck(id, back) {
  try { S.res = await api('/factory/checks/' + id); } catch (e) { return toast(e.message, 'error'); }
  if (S.res.result?.status !== 'ok') return toast(S.res.result?.reason || 'That check did not finish', 'error');
  S.back = back; S.day = 90; S.pos = { lat: S.res.lat, lon: S.res.lon }; show();
}

// ------------------------------------------------------------------ the result
function show() {
  S.layer.clearLayers(); S.fitted = false;
  const r = S.res.result, c = r.layers.footprint.coordinates[0];
  S.map.flyTo([S.res.lat, S.res.lon], 13, { duration: 0.8 });
  paintResult(); setTimeout(draw, 350);
}
const scaleAbout = (g, cx, cy, k) => ({ type: g.type, coordinates: g.type === 'Polygon' ? g.coordinates.map((ring) => ring.map(([x, y]) => [cx + (x - cx) * k, cy + (y - cy) * k])) : g.coordinates.map((p) => p.map((ring) => ring.map(([x, y]) => [cx + (x - cx) * k, cy + (y - cy) * k]))) });
function draw() {
  S.layer.clearLayers();
  const res = S.res, r = res.result, ly = r.layers, h = S.day ? r.projection.horizons.find((x) => x.day === S.day) : null;
  const add = (g, opt, tip) => { if (!g) return; const l = L.geoJSON(g, opt).addTo(S.layer); if (tip) l.bindTooltip(tip, { sticky: true }); return l; };
  if (S.on.forest) add(ly.forest, { style: { color: '#075e2f', weight: 1, fillColor: '#0f8a43', fillOpacity: 0.4 }, interactive: false });
  if (S.on.rivers) ly.rivers.forEach((v) => add(v.line, { style: { color: '#2a7fdb', weight: 3, opacity: 0.85 } }, `${v.name || 'River / stream'} · ${v.distance_m} m from the site`));
  if (h && S.on.edge) add(ly.edge, { style: { color: '#e9b949', weight: 0, fillColor: '#e9b949', fillOpacity: 0.12 + 0.45 * (h.edge_affected_ha / Math.max(r.site.edge_zone_forest_ha, 1)) } }, `Edge zone: ${fmt(h.edge_affected_ha)} ha of forest affected by now`);
  if (S.on.smoke && h?.operating) ly.plumes.forEach((p, k) => add(p, { style: { color: '#7b4fb0', weight: 1, fillColor: '#7b4fb0', fillOpacity: [0.28, 0.18, 0.1][k] * Math.min(1, h.operating_days / 30) } }, ['Closest smoke ring', 'Middle smoke ring (3 km)', 'Outer smoke ring'][k]));
  const frac = h ? h.cleared_pct / 100 : 0;
  const cx = res.lon, cy = res.lat;
  if (!h) { add(ly.footprint, { style: { color: '#444', weight: 1.5, dashArray: '5 5', fill: false }, interactive: false }); }
  else { add(scaleAbout(ly.footprint, cx, cy, Math.sqrt(Math.max(frac, 0.02))), { style: { color: '#7a4310', weight: 2, fillColor: h.operating ? '#2f3b45' : '#b5651d', fillOpacity: 0.85 } }, h.operating ? 'Factory (running)' : `Cleared so far: ${fmt(h.cleared_ha)} ha`); add(ly.footprint, { style: { color: '#444', weight: 1, dashArray: '4 4', fill: false }, interactive: false }); }
  L.marker([cy, cx], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 15], html: `<div class="case-pin">🏭</div>` }), interactive: false }).addTo(S.layer);
  if (S.on.villages) ly.villages.forEach((v) => L.circleMarker([v.lat, v.lon], { radius: 5, color: '#fff', weight: 1.5, fillColor: v.downwind ? '#d7263d' : '#5b6b66', fillOpacity: 1 }).bindTooltip(`${v.name} · ${(v.distance_m / 1000).toFixed(1)} km${v.downwind ? ' · downwind' : ''}`).addTo(S.layer));
  if (S.on.reserves) ly.reserves.forEach((v) => L.marker([v.lat, v.lon], { icon: L.divIcon({ className: '', iconSize: [24, 24], iconAnchor: [12, 12], html: '<div class="case-pin" style="font-size:18px">🌳</div>' }) }).bindTooltip(`${v.name} · about ${(v.distance_m / 1000).toFixed(1)} km`).addTo(S.layer));
}
const rose = (w) => {
  if (w.status !== 'ok') return '';
  const max = Math.max(...w.rose.map((x) => x.pct), 1);
  return `<div class="rose" title="How often the wind blows from each direction over the last year">${w.rose.map((x) => `<div class="rb ${x.from === w.from_name ? 'top' : ''}"><i style="height:${Math.max(6, (x.pct / max) * 38)}px"></i><span>${x.from.replace('north', 'N').replace('south', 'S').replace('east', 'E').replace('west', 'W').replace('-', '')}</span></div>`).join('')}</div>`;
};
function paintResult() {
  const box = $('#rightBody'), res = S.res, r = res.result, hs = r.projection.horizons, h = S.day ? hs.find((x) => x.day === S.day) : null;
  const m = h || { cleared_ha: 0, edge_affected_ha: 0, carbon_t_co2: 0, smoke_forest_ha: 0, phase: 'Today', operating: false, water_pathway: 'not yet running' };
  box.innerHTML = html`<button class="back" id="rback">‹ ${S.back ? 'Back' : 'Reports'}</button>
    <div class="row-item" style="border:0;padding:0"><span style="font-size:26px">🏭</span><span class="grow"><h2 style="font-size:17px">${res.name}</h2><div class="sub">${r.inputs.kind_label} · ${res.lat.toFixed(4)}, ${res.lon.toFixed(4)}</div></span></div>
    <div class="pills3" id="fday">${raw([[0, 'Today'], [7, '7 days'], [30, '30 days'], [90, '90 days']].map(([d, n]) => `<button data-d="${d}" class="${S.day === d ? 'on' : ''}">${n}</button>`).join(''))}</div>
    <div class="row2"><button class="btn sm" id="fplay">▶ Play</button><input type="range" id="fslide" min="0" max="3" value="${[0, 7, 30, 90].indexOf(S.day)}" style="flex:1"></div>
    <div class="banner2 ${h?.operating ? 'red' : 'amber'}" style="margin-top:8px"><b>${h ? `After ${h.day} days · ${h.phase}` : 'Today, before any work'}</b><div class="sub">${h ? `${h.cleared_pct}% of the site cleared${h.operating ? `, running for ${h.operating_days} days` : ''}. ${h.water_pathway === 'open' ? 'Waste water can reach the nearest river.' : ''}` : `${fmt(r.site.forest_in_footprint_ha)} ha of the ${fmt(r.inputs.footprint_ha)} ha site is forest.`}</div></div>
    <div class="stat4"><div><b>${fmt(m.cleared_ha)} ha</b><span>cleared</span></div><div><b>${fmt(m.edge_affected_ha)} ha</b><span>edge-affected</span></div><div><b>${fmt(m.carbon_t_co2, 0)}</b><span>t CO₂ released</span></div><div><b>${fmt(m.smoke_forest_ha)} ha</b><span>forest in smoke zone</span></div></div>
    <div class="row2 tog">${raw([['forest', 'Forest'], ['edge', 'Edge zone'], ['smoke', 'Smoke zones'], ['rivers', 'Rivers'], ['villages', 'Villages'], ['reserves', 'Reserves']].map(([k, n]) => `<label><input type="checkbox" data-k="${k}" ${S.on[k] ? 'checked' : ''}> ${n}</label>`).join(''))}</div>
    <div class="sect"><h4>What this does to the surroundings</h4>
      ${raw(r.effects.map((e) => { const [lv, col] = LEVEL[e.level]; return `<div class="eff"><span class="eff-ic">${e.icon}</span><div><b>${esc(e.title)}</b><div class="sub">${esc(e.detail)}</div></div><span class="lvl" style="background:${col}">${lv}</span></div>`; }).join(''))}</div>
    ${raw(r.wind.status === 'ok' ? `<div class="sect"><h4>Which way the wind blows</h4><div class="sub">From the ${esc(r.wind.from_name)} ${r.wind.share_prevailing_pct}% of the time (last year).</div>${rose(r.wind)}</div>` : '')}
    ${raw(r.inputs.emissions?.length ? `<div class="sect"><h4>Figures you supplied</h4>${r.inputs.emissions.map((e) => `<div class="sub">${esc(e.pollutant)}: <b>${esc(e.value)} ${esc(e.unit)}</b> · ${esc(e.source)}</div>`).join('')}</div>` : '')}
    <div class="row2" style="margin-top:10px"><button class="btn primary grow" id="ftrack">🛰 Watch real change nearby (90 days)</button></div>
    <div class="row2" style="margin-top:6px"><button class="btn grow" id="fpdf">⬇ Download report</button><button class="btn" id="fnew">Check another</button></div>
    <details class="sect"><summary><b>Sources and limits</b></summary><ul class="sub"><li>${raw(Object.values(r.sources).map(esc).join('</li><li>'))}</li><li>${esc(r.projection.assumptions.note)}</li></ul></details>`;
  $('#rback').onclick = () => { clear(); S.back?.(); };
  $('#fday').onclick = (e) => { const d = e.target.dataset.d; if (d !== undefined) go(Number(d)); };
  $('#fslide').oninput = (e) => go([0, 7, 30, 90][Number(e.target.value)]);
  $('#fplay').onclick = play;
  $$('input[data-k]', box).forEach((i) => (i.onchange = () => { S.on[i.dataset.k] = i.checked; draw(); }));
  $('#fpdf').onclick = () => import('./core.js').then((c) => c.download(`/factory/checks/${res.id}/report.pdf`, `factory-check-${res.name.replace(/\W+/g, '-')}.pdf`));
  $('#fnew').onclick = () => start(S.back);
  $('#ftrack').onclick = track;
}
function go(d) { S.day = d; paintResult(); draw(); }
function play() { clearInterval(S.timer); go(0); let k = 0; const seq = [0, 7, 30, 90]; S.timer = setInterval(() => { k++; if (k >= seq.length) return clearInterval(S.timer); go(seq[k]); }, 2000); }

// run the real change scan around the site and hand over to the Trees tab
async function track() {
  const { lat, lon } = S.res, dLat = 2.5 / 110.54, dLon = 2.5 / (111.32 * Math.cos((lat * Math.PI) / 180));
  const poly = { type: 'Polygon', coordinates: [[[lon - dLon, lat - dLat], [lon + dLon, lat - dLat], [lon + dLon, lat + dLat], [lon - dLon, lat + dLat], [lon - dLon, lat - dLat]]] };
  try {
    const r = await api('/forest-change/runs', { method: 'POST', body: { bbox: [lon - dLon, lat - dLat, lon + dLon, lat + dLat], polygon: poly, window_days: 90 } });
    document.querySelector('#rtabs button[data-t=tree]').click();
    window.dispatchEvent(new CustomEvent('rk:watch-run', { detail: { id: r.run_id } }));
  } catch (e) { toast(e.message, 'error'); }
}

// Worked example cases for the Reports tab: what changes when a factory is built beside, or by clearing, a forest.
// The map redraws the forest, cleared land, structures, influence zones and waste-water path for each stage, with the
// numbers (forest left, edge-affected land, forest pieces, carbon) beside it.
import { api, esc, html, raw, toast } from './core.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const S = { map: null, layer: null, c: null, i: 0, on: { rings: true, edge: true, corridor: true }, timer: null, back: null };
const LEVEL = { high: ['High', '#d7263d'], medium: ['Medium', '#f4802a'], low: ['Low', '#2e9e5b'] };
const fmt = (n) => Number(n).toLocaleString('en-IN', { maximumFractionDigits: 1 });

export function init({ map }) {
  S.map = map; S.layer = L.layerGroup().addTo(map);
  window.addEventListener('rk:tab', (e) => { if (e.detail !== 'inc') clear(); });
}
export function clear() { S.layer?.clearLayers(); clearInterval(S.timer); S.c = null; }

export async function mountList(el, onOpen) {
  try {
    const list = await api('/forest-change/cases');
    el.innerHTML = list.map((c) => `<button class="case-card" data-id="${esc(c.id)}"><span class="case-ic">🏭</span><span class="grow"><b>${esc(c.title)}</b><small>${esc(c.where)}</small><em>${esc(c.summary)}</em></span><span class="tag">Example</span></button>`).join('');
    $$('.case-card', el).forEach((b) => (b.onclick = () => onOpen(b.dataset.id)));
  } catch (e) { el.innerHTML = `<div class="sub">${esc(e.message)}</div>`; }
}

export async function open(id, back) {
  try { S.c = await api('/forest-change/cases/' + id); } catch (e) { return toast(e.message, 'error'); }
  S.i = 0; S.back = back;
  S.map.flyTo([S.c.center.lat, S.c.center.lon], 13, { duration: 0.8 });
  paint(); setTimeout(draw, 300);
}

function style(color, o = {}) { return { color, weight: 1.5, fillColor: color, fillOpacity: 0.5, ...o }; }
function draw() {
  S.layer.clearLayers();
  const c = S.c; if (!c) return;
  const st = c.stages[S.i], L_ = st.layers, first = c.stages[0].layers;
  const add = (g, opt, tip) => { if (!g) return; const l = L.geoJSON(g, opt).addTo(S.layer); if (tip) l.bindTooltip(tip, { sticky: true }); return l; };
  if (S.i > 0) add(first.forest, { style: { color: '#3b3b3b', weight: 1.2, dashArray: '5 5', fill: false }, interactive: false });   // how big the forest used to be
  add(L_.forest, { style: style('#0f8a43', { color: '#075e2f', fillOpacity: 0.5 }) }, `Forest: ${fmt(st.metrics.forest_ha)} ha`);
  add(L_.river, { style: { color: '#2a7fdb', weight: 4, opacity: 0.85 } }, 'River');
  if (S.on.corridor) add(L_.corridor, { style: { color: '#8a5a2b', weight: 3, dashArray: '2 8', opacity: 0.95 } }, 'Animal corridor (illustrative)');
  if (S.on.edge) add(L_.edge, { style: style('#e9b949', { weight: 0, fillOpacity: 0.55 }), interactive: true }, `Edge zone: forest within 300 m of cleared land (${fmt(st.metrics.edge_affected_ha)} ha)`);
  add(L_.cleared, { style: style('#b5651d', { color: '#7a4310', fillOpacity: 0.75 }) }, 'Cleared ground');
  add(L_.road, { style: style('#555', { weight: 1, fillOpacity: 0.8 }) }, 'Access road');
  if (L_.plant) { const l = add(L_.plant, { style: style('#2f3b45', { color: '#111', weight: 2, fillOpacity: 0.9 }) }, 'Factory'); const b = l.getBounds().getCenter(); L.marker(b, { icon: L.divIcon({ className: '', iconSize: [28, 28], iconAnchor: [14, 14], html: '<div class="case-pin">🏭</div>' }), interactive: false }).addTo(S.layer); }
  if (S.on.rings && L_.plumes) L_.plumes.forEach((p, k) => add(p, { style: style('#7b4fb0', { weight: 1, fillOpacity: [0.28, 0.18, 0.1][k] }) }, ['Closest 1.5 km downwind', 'Within 3 km downwind', 'Within 5 km downwind'][k] + ' (illustrative)'));
  if (L_.effluent) add(L_.effluent, { style: { color: '#d7263d', weight: 4, dashArray: '8 6' } }, 'Waste-water path to the river');
  (L_.chimneys || []).forEach((p) => L.marker([p.lat, p.lon], { icon: L.divIcon({ className: '', iconSize: [18, 30], iconAnchor: [9, 30], html: '<div class="case-chim"><i></i></div>' }) }).bindTooltip('Chimney').addTo(S.layer));
  const b = L.geoJSON(first.forest).getBounds();
  if (!S.fitted) { S.map.fitBounds(b.pad(-0.1)); S.fitted = true; }
}

function paint() {
  const box = $('#rightBody'), c = S.c; if (!box || !c) return;
  S.fitted = false;
  const st = c.stages[S.i], m0 = c.stages[0].metrics, m = st.metrics, last = S.i === c.stages.length - 1;
  const delta = (k, unit = ' ha') => { const d = m[k] - m0[k]; return d === 0 ? '' : `<small class="${d > 0 && k !== 'forest_ha' ? 'bad' : d < 0 ? 'bad' : ''}">${d > 0 ? '+' : '−'}${fmt(Math.abs(d))}${unit}</small>`; };
  box.innerHTML = html`<button class="back" id="cback">‹ All incidents</button>
    <div class="row-item" style="border:0;padding:0"><span style="font-size:26px">🏭</span><span class="grow"><h2 style="font-size:17px">${c.title}</h2><div class="sub">${c.where}</div></span><span class="tag">Example</span></div>
    <div class="pills3" id="cst">${raw(c.stages.map((s, k) => `<button data-k="${k}" class="${k === S.i ? 'on' : ''}">${esc(s.label)}<small>${esc(s.year)}</small></button>`).join(''))}</div>
    <div class="row2"><button class="btn sm" id="cplay">▶ Play</button><input type="range" id="cslide" min="0" max="${c.stages.length - 1}" value="${S.i}" style="flex:1"></div>
    <div class="banner2 ${last ? 'red' : 'amber'}" style="margin-top:8px"><b>${esc(st.year)} · ${esc(st.label)}</b><div class="sub">${esc(st.caption)}</div></div>
    <div class="stat4">
      <div><b>${fmt(m.forest_ha)} ha</b><span>forest left</span>${raw(delta('forest_ha'))}</div>
      <div><b>${fmt(m.lost_ha)} ha</b><span>cleared (${m.lost_pct}%)</span></div>
      <div><b>${fmt(m.edge_affected_ha)} ha</b><span>edge-affected</span></div>
      <div><b>${m.patches}</b><span>forest piece${m.patches === 1 ? '' : 's'}</span>${raw(m.patches > m0.patches ? `<small class="bad">was ${m0.patches}</small>` : '')}</div>
    </div>
    <div class="row2 tog"><label><input type="checkbox" id="tr" ${S.on.rings ? 'checked' : ''}> Smoke zones</label><label><input type="checkbox" id="te" ${S.on.edge ? 'checked' : ''}> Edge zone</label><label><input type="checkbox" id="tc" ${S.on.corridor ? 'checked' : ''}> Animal corridor</label></div>
    <div class="legend2"><span><i style="background:#0f8a43"></i>Forest</span><span><i style="background:#b5651d"></i>Cleared</span><span><i style="background:#2f3b45"></i>Factory</span><span><i style="background:#e9b949"></i>Edge zone</span><span><i style="background:#7b4fb0"></i>Smoke zone</span><span><i style="background:#d7263d"></i>Waste water</span><span><i class="dash"></i>Forest before</span></div>
    <div class="sect"><h4>What this does to the ecosystem</h4>
      ${raw(c.effects.map((e) => { const live = !(['Water', 'Air'].includes(e.area) && !last) && !(S.i === 0); const [lv, col] = LEVEL[e.level]; return `<div class="eff ${live ? '' : 'off'}"><span class="eff-ic">${e.icon}</span><div><b>${esc(e.title)}</b><div class="sub">${esc(e.detail)}</div></div><span class="lvl" style="background:${col}">${lv}</span></div>`; }).join(''))}
    </div>
    <details class="sect"><summary><b>How these numbers are made</b></summary><div class="sub" style="margin-top:6px">${esc(c.notice)}</div><ul class="sub"><li>${esc(c.assumptions.edge_note)}</li><li>${esc(c.assumptions.plume_note)}</li><li>Carbon: ${fmt(m.carbon_t_co2.low)} – ${fmt(m.carbon_t_co2.high)} t CO₂ (central ${fmt(m.carbon_t_co2.central)}). ${esc(c.assumptions.carbon_density_t_c_per_ha.note)}</li></ul></details>`;
  $('#cback').onclick = () => { clear(); S.back?.(); };
  $('#cst').onclick = (e) => { const b = e.target.closest('button[data-k]'); if (b) go(Number(b.dataset.k)); };
  $('#cslide').oninput = (e) => go(Number(e.target.value));
  $('#cplay').onclick = play;
  for (const [id, k] of [['tr', 'rings'], ['te', 'edge'], ['tc', 'corridor']]) $('#' + id).onchange = (e) => { S.on[k] = e.target.checked; draw(); };
}
function go(i) { S.i = i; paint(); draw(); }
function play() { clearInterval(S.timer); go(0); let k = 0; S.timer = setInterval(() => { k++; if (k >= S.c.stages.length) return clearInterval(S.timer); go(k); }, 2200); }

// Forest Explorer home: pick an incident to see a 3D simulation of it, or open a whole-region analysis.
// Everything the simulation draws is invented from the incident's boundary; the cards say so.
import { api, html, raw, esc, $, $$ } from '../core.js';

const D = (iso) => (iso ? new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : '');
const nice = (v) => String(v || 'unknown').replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase());
const KINDS = { cutting: 'Illegal cutting', fire: 'Forest fire', encroachment: 'Encroachment', pollution: 'Industrial pollution', other: 'Other' };
const GROUPS = [['all', 'All'], ['fire', 'Fire'], ['loss', 'Tree loss'], ['reported', 'Reported']];

async function load() {
  const get = (p) => api(p).catch(() => []);
  const [inc, fires, spots, regions] = await Promise.all([get('/incidents'), get('/fire/incidents'), get('/forest-change/records?days=365&include_false=false'), get('/regions')]);
  const cards = [];
  (inc || []).forEach((i) => {
    const fire = i.evidence?.kind === 'fire' || !!i.wildfire_id;
    cards.push({ group: fire ? 'fire' : 'reported', id: i.id, title: i.title, type: KINDS[i.evidence?.kind] || (i.change_id ? 'Satellite change' : fire ? 'Forest fire' : 'Reported incident'), status: nice(i.status), when: i.created_at, geom: i.geometry?.type === 'Point' ? 'Point' : 'Boundary', scenario: fire ? 'Fire spread' : 'Deforestation' });
  });
  (fires || []).filter((f) => !f.is_demo).forEach((f) => cards.push({ group: 'fire', id: 'fire:' + f.id, title: `${f.code}${f.detection?.place?.name ? ' · ' + f.detection.place.name : ''}`, type: 'Satellite fire hotspot', status: f.status_label || nice(f.status), when: f.created_at, geom: 'Point', scenario: 'Fire spread' }));
  (spots || []).slice(0, 40).forEach((r) => cards.push({ group: 'loss', id: 'forest:' + r.id, title: `${r.code} · ${r.area_ha} ha of tree loss`, type: 'Tree loss (satellite candidate)', status: nice(r.status), when: r.created_at || r.est_date, geom: 'Exact boundary', scenario: 'Deforestation' }));
  cards.sort((a, b) => String(b.when).localeCompare(String(a.when)));
  return { cards, regions: regions || [] };
}

export async function renderHome(root) {
  const { cards, regions } = await load();
  const S = { group: 'all', q: '' };
  root.innerHTML = html`
    <div class="fx-head"><div class="fx-title"><span class="mode-chip" id="modeChip">Forest Explorer</span><h2 class="fx-h">See what could happen inside an incident</h2>
      <div class="fx-sub">Pick an incident and watch a simulated deforestation or fire play out inside its boundary.</div></div>
      <div class="fx-actions"><a class="button subtle sm" href="/live">Live map</a></div></div>
    <div class="fx-note"><b>Everything here is simulated.</b> It is a browser-generated picture based on the incident's boundary. It is not real mapping, satellite evidence, a forecast, or a legal determination.</div>
    <div class="fx-how"><div><b>1</b><span>Choose an incident below</span></div><div><b>2</b><span>Pick deforestation, fire spread, or both</span></div><div><b>3</b><span>Press Play and watch it unfold</span></div></div>
    <div class="panel fx-pick"><div class="row" style="flex-wrap:wrap;gap:10px"><div class="pill-btns" id="fxGroups" role="group" aria-label="Filter incidents">${raw(GROUPS.map(([k, n]) => `<button data-g="${k}" class="${k === 'all' ? 'on' : ''}" aria-pressed="${k === 'all'}">${n}</button>`).join(''))}</div>
      <label class="grow" style="min-width:200px"><span class="sr-only">Search incidents</span><input type="search" id="fxQ" placeholder="Search incidents by name or place" aria-label="Search incidents" style="width:100%"></label></div>
      <div id="fxList" class="fx-cards" role="list"></div></div>
    <div class="panel" style="margin-top:14px"><h4>Or analyse a whole region</h4><p class="sub">Real elevation and ESA WorldCover tree cover for a monitored region, with stored candidate loss and hypothetical clearing.</p>
      <div class="fx-cards">${raw(regions.length ? regions.map((r) => `<a class="fx-card-link" href="#/ar-explorer/${esc(r.id)}"><b>${esc(r.name)}</b><span>${esc(r.jurisdiction || 'Region')}</span><span class="fx-go">Open region analysis →</span></a>`).join('') : '<div class="help">No monitored regions yet. Create one under Forest Monitoring.</div>')}</div></div>`;

  const paint = () => {
    const q = S.q.trim().toLowerCase();
    const rows = cards.filter((c) => (S.group === 'all' || c.group === S.group) && (!q || (c.title + ' ' + c.type).toLowerCase().includes(q)));
    $('#fxList').innerHTML = rows.length ? rows.map((c) => `<a class="fx-card-link" role="listitem" href="#/ar-explorer/${esc(encodeURIComponent(c.id))}"><span class="fx-kind ${esc(c.group)}">${esc(c.type)}</span><b>${esc(c.title)}</b><span>${esc(c.status)} · ${esc(D(c.when))}</span><span class="fx-meta">${esc(c.geom)} · opens on ${esc(c.scenario)}</span><span class="fx-go">Open 3D simulation →</span></a>`).join('')
      : `<div class="empty-state" style="grid-column:1/-1"><strong>${cards.length ? 'Nothing matches' : 'No incidents yet'}</strong><span>${cards.length ? 'Try another filter or search.' : 'Report an incident, or run a tree-loss check, on the <a href="/live">Live map</a>. It will appear here.'}</span></div>`;
  };
  $$('#fxGroups button').forEach((b) => (b.onclick = () => { S.group = b.dataset.g; $$('#fxGroups button').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', String(x === b)); }); paint(); }));
  $('#fxQ').oninput = (e) => { S.q = e.target.value; paint(); };
  paint();
  return { destroy() {} };
}

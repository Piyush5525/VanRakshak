// Console home: what is happening now, what needs attention, and one click to everything. Data comes from stored checks and records.
import { api, html, raw, esc, fmt, empty, $ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

const tile = (href, icon, v, l, sub = '') => `<a class="ov-tile" href="${esc(href)}"><span class="ov-ic" aria-hidden="true">${icon}</span><b>${esc(v)}</b><span class="ov-l">${esc(l)}</span>${sub ? `<span class="ov-s">${esc(sub)}</span>` : ''}<span class="ov-go">Open →</span></a>`;
const act = (href, icon, t, d) => `<a class="ov-act" href="${esc(href)}"><span class="ov-ai" aria-hidden="true">${icon}</span><span><b>${esc(t)}</b><small>${esc(d)}</small></span></a>`;

export async function render(root) {
  const get = (p) => api(p).catch(() => null);
  const [s, regions, changes, incidents, fires, sites, hazards, audits, spots, fireInc, infra] = await Promise.all([
    api('/dashboard/summary'), api('/regions'), api('/forest/changes?limit=200'), api('/incidents'), api('/wildfires'), api('/industrial/sites').catch(() => []), api('/navigation/hazards').catch(() => []),
    get('/audit/events?limit=40'), get('/forest-change/records?days=30&include_false=false'), get('/fire/incidents'), get('/infra/runs?limit=20'),
  ]);
  const lossHa = (spots || []).reduce((t, r) => t + (r.area_ha || 0), 0);
  const realFires = (fireInc || []).filter((f) => !f.is_demo && !f.detection?.archive);
  const infraDone = (infra || []).filter((r) => r.status === 'COMPLETE' && r.summary), built = infraDone.reduce((t, r) => t + r.summary.patches, 0);
  const hour = new Date().getHours(), greet = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';
  const attention = [
    ...incidents.filter((i) => ['DETECTED', 'NEEDS_REVIEW'].includes(i.status)).slice(0, 3).map((i) => ({ href: '#/investigations/' + i.id, t: i.title, d: 'Incident waiting for review', k: 'Review' })),
    ...(spots || []).filter((r) => r.status === 'NEEDS_REVIEW').slice(0, 4).map((r) => ({ href: '#/ar-explorer/forest:' + r.id, t: `${r.code} · ${r.area_ha} ha of tree loss`, d: 'Tree-loss spot to review · opens its 3D simulation', k: '3D' })),
  ].slice(0, 6);
  const n = (spots || []).length;
  const WORDS = { incident_created: 'New incident reported', incident_deleted: 'An incident was removed', infra_scan_completed: 'A construction check finished', forest_record_notified: 'An alert was sent for a tree-loss spot', incident_reviewed: 'An incident was reviewed', forest_escalated: 'A tree-loss spot was escalated' };
  const ago = (iso) => { const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000)); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`; };
  const latest = (audits || []).filter((a) => WORDS[a.action]).slice(0, 5).map((a) => ({ t: WORDS[a.action], when: ago(a.created_at) }));

  root.innerHTML = html`
    <div class="ov-hero"><div><h2>${greet}</h2><p>Here is what is happening in the forests you watch. Everything below comes from stored satellite checks and records.</p></div>
      <div class="ov-actions">${raw(act('/live', '🗺', 'Live map', 'Fires, tree loss and incidents') + act('#/ar-explorer', '🧊', 'Forest Explorer', '3D simulation of an incident') + act('/live', '🏗', 'New construction', 'What was built in 365 days') + act('/replanting/', '🌱', 'Replanting', 'What to plant and how it grows'))}</div></div>
    <div class="ov-tiles">${raw(
      tile('/live', '🔥', realFires.length, 'Live fire incidents', 'Hotspots you are investigating')
      + tile('/live', '🌲', `${lossHa.toFixed(1)} ha`, 'Tree loss, last 30 days', `${n} spot${n === 1 ? '' : 's'} found`)
      + tile('#/investigations', '⚠', s.pending_incidents, 'Incidents to review', s.pending_incidents ? 'Waiting for a decision' : 'Nothing waiting')
      + tile('/live', '🏗', built, 'New construction found', `${infraDone.length} area${infraDone.length === 1 ? '' : 's'} checked`))}</div>
    <div class="grid split" style="margin-top:16px">
      <div class="panel"><div class="row"><h3 class="grow" style="margin:0">Needs your attention</h3><a class="button subtle sm" href="/live">Open live map</a></div>
        ${raw(attention.length ? `<ul class="ov-list">${attention.map((x) => `<li><a href="${esc(x.href)}"><span><b>${esc(x.t)}</b><small>${esc(x.d)}</small></span><span class="chip">${esc(x.k)}</span></a></li>`).join('')}</ul>` : '<div class="empty-state"><strong>All clear</strong><span>Nothing is waiting for review right now.</span></div>')}</div>
      <div class="panel"><h3>Latest</h3>${raw(latest.length ? `<ul class="ov-feed">${latest.map((x) => `<li><b>${esc(x.t)}</b><small>${esc(x.when)}</small></li>`).join('')}</ul>` : '<div class="empty-state"><strong>Nothing yet</strong><span>Reports, alerts and checks will show up here.</span></div>')}</div>
    </div>
    <div class="panel" style="margin-top:16px"><h3>Monitoring map</h3><p class="sub">Regions, candidate loss, incidents and fire records from stored data. Use the layer control at the top right of the map.</p>
      <div id="map" class="map-box tall" role="application" aria-label="Monitoring map"></div>
      ${raw(legend([['#1d6b4b', 'Region'], ['#c73b25', 'Candidate forest loss'], ['#bf731f', 'Incident'], ['#ff6a2b', 'Fire record'], ['#8da0b3', 'Industrial site', true], ['#b04bd6', 'Hazard']]))}</div>`;

  const { map, group } = await createMap($('#map'));
  const layers = [];
  const g = (name, v = true) => group(name, v);
  regions.forEach((r) => layers.push(addGeo(g('Regions'), r.geometry, STYLE.region, popup(r.name, [r.jurisdiction]))));
  const loss = g('Candidate loss');
  changes.filter((c) => c.disturbance_type === 'CANDIDATE_FOREST_LOSS').forEach((c) => layers.push(addGeo(loss, c.geometry, STYLE.loss, popup('Candidate forest loss (model)', [`${c.area_hectares} ha`, `${fmt.date(c.baseline_date)} → ${fmt.date(c.comparison_date)}`]))));
  incidents.forEach((i) => layers.push(addGeo(g('Incidents'), i.geometry, STYLE.incident, popup(i.title, [fmt.status(i.status)]))));
  fires.forEach((f) => layers.push(addGeo(g('Fire records'), f.geometry, f.observation_type === 'camera_detection' ? STYLE.camera : STYLE.fire, popup(fmt.status(f.observation_type), [f.source, fmt.dt(f.observed_at), `Review: ${fmt.status(f.status)}`]))));
  sites.forEach((x) => layers.push(addGeo(g('Industrial sites', false), x.geometry, STYLE.site, popup(x.name, [fmt.status(x.status)]))));
  hazards.forEach((h) => layers.push(addGeo(g('Hazards'), h.geometry, STYLE.hazard, popup(`${fmt.status(h.kind)} (${h.verified ? 'verified' : 'unverified'})`, [h.source, `${h.age_hours} h ago`]))));
  fit(map, layers);
  return { destroy: () => map.remove() };
}

import { api, html, raw, esc, fmt, tag, empty, errorBox, $ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

export async function render(root) {
  const [s, regions, changes, incidents, fires, sites, hazards] = await Promise.all([
    api('/dashboard/summary'), api('/regions'), api('/forest/changes?limit=200'), api('/incidents'), api('/wildfires'), api('/industrial/sites').catch(() => []), api('/navigation/hazards').catch(() => []),
  ]);
  const audits = await api('/audit/events?limit=8').catch(() => null);
  const stat = (v, l, extra = '') => html`<div class="panel stat"><b>${v}</b><span>${l}</span>${raw(extra)}</div>`;
  root.innerHTML = html`
    <div class="grid g4">
      ${stat(fmt.ha(s.monitored_area_hectares), `Monitored forest area · ${s.monitored_regions} region(s)`)}
      ${stat(fmt.ha(s.candidate_loss_hectares_last_365d), 'Candidate loss detected, last 365 days', '<span class="help">Model candidates, not verified loss</span>')}
      ${stat(s.pending_incidents, 'Incidents awaiting verification')}
      ${stat(s.wildfire_alerts_needing_review, 'Fire records awaiting review')}
    </div>
    <div class="grid g4" style="margin-top:16px">
      ${stat(s.authorization_checks_needing_review, 'Authorisation checks needing review')}
      ${stat(s.environmental_assessments, 'Environmental assessments')}
      ${stat(s.industrial_sites, 'Industrial sites tracked')}
      ${stat(s.forecast_model_usable ? 'Available' : 'Not available', 'Spatial risk model', `<span class="help">${s.forecast_model_usable ? 'Evaluated and usable' : 'Needs more verified detection history'}</span>`)}
    </div>
    <div class="grid split" style="margin-top:16px">
      <div class="panel"><h3>Monitoring map</h3><p class="sub">Everything shown is read from stored records. Toggle layers with the control at the top right of the map.</p>
        <div id="map" class="map-box tall" role="application" aria-label="Monitoring map"></div>
        ${raw(legend([['#1d6b4b', 'Region'], ['#c73b25', 'Candidate forest loss'], ['#bf731f', 'Incident'], ['#ff6a2b', 'Fire record'], ['#8da0b3', 'Industrial site', true], ['#b04bd6', 'Hazard']]))}
      </div>
      <div class="stack">
        <div class="panel"><h3>Recent activity</h3>${audits === null ? raw(empty('Audit trail restricted', 'Your role cannot read the audit log.')) : audits.length ? raw(`<ul class="timeline">${audits.map((a) => `<li><b>${esc(fmt.status(a.action))}</b>${esc(a.resource_type)} · ${esc(a.actor_role)} · ${esc(fmt.dt(a.created_at))}</li>`).join('')}</ul>`) : raw(empty('No activity yet', 'Actions are recorded here as they happen.'))}</div>
        <div class="panel"><h3>Recent notifications</h3>${s.recent_notifications.length ? raw(`<ul class="timeline">${s.recent_notifications.map((n) => `<li><b>${esc(n.channel)}</b>${esc(fmt.status(n.status))} · ${esc(fmt.dt(n.created_at))}</li>`).join('')}</ul>`) : raw(empty('None sent or captured', 'Dispatches appear here with their delivery status.'))}</div>
        <div class="note-box">Elevated-risk areas: ${s.forecast_model_usable ? 'see Forest Forecasts.' : 'not shown because no evaluated spatial risk model exists. This panel will not display placeholder risk.'}</div>
      </div>
    </div>`;

  const { map, group } = await createMap($('#map'));
  const layers = [];
  const g = (name, v = true) => group(name, v);
  regions.forEach((r) => layers.push(addGeo(g('Regions'), r.geometry, STYLE.region, popup(r.name, [r.jurisdiction]))));
  const loss = g('Candidate loss');
  changes.filter((c) => c.disturbance_type === 'CANDIDATE_FOREST_LOSS').forEach((c) => layers.push(addGeo(loss, c.geometry, STYLE.loss, popup('Candidate forest loss (model)', [`${c.area_hectares} ha`, `${fmt.date(c.baseline_date)} → ${fmt.date(c.comparison_date)}`, 'Needs verification']))));
  incidents.forEach((i) => layers.push(addGeo(g('Incidents'), i.geometry, STYLE.incident, popup(i.title, [fmt.status(i.status)]))));
  fires.forEach((f) => layers.push(addGeo(g('Fire records'), f.geometry, f.observation_type === 'camera_detection' ? STYLE.camera : STYLE.fire, popup(fmt.status(f.observation_type), [f.source, fmt.dt(f.observed_at), `Review: ${fmt.status(f.status)}`]))));
  sites.forEach((x) => layers.push(addGeo(g('Industrial sites', false), x.geometry, STYLE.site, popup(x.name, [fmt.status(x.status)]))));
  hazards.forEach((h) => layers.push(addGeo(g('Hazards'), h.geometry, STYLE.hazard, popup(`${fmt.status(h.kind)} (${h.verified ? 'verified' : 'unverified'})`, [h.source, `${h.age_hours} h ago`]))));
  fit(map, layers);
  return { destroy: () => map.remove() };
}

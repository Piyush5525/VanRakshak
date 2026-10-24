import { api, html, raw, esc, fmt, tag, classTag, empty, toast, can, waitJob, kv, download, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup } from '../map.js';

const TITLES = { forest_change: 'Forest change (candidate)', fragmentation: 'Fragmentation', terrain: 'Terrain (elevation, slope)', erosion_susceptibility: 'Erosion susceptibility (screening)', rainfall: 'Rainfall', water_runoff_screening: 'Water and runoff (screening)', nearby_features_osm: 'Nearby mapped features (OpenStreetMap)', biodiversity: 'Biodiversity and habitat', carbon: 'Carbon and climate', watershed: 'Rivers and watersheds' };

function sectionBody(name, s) {
  const skip = new Set(['evidence_class', 'attribution', 'status', 'claim_type']);
  if (s.evidence_class === 'not_available') return `<div class="warn-box">${esc(s.reason)}</div>`;
  const pick = (o) => Object.fromEntries(Object.entries(o).filter(([k]) => !skip.has(k)));
  if (name === 'terrain') { const r = s.region || {}, l = s.within_candidate_loss || {}; return `<div class="grid g2"><div><b>Whole region</b>${kvS({ 'elevation m': r.elevation_m && `${r.elevation_m.min}–${r.elevation_m.max} (mean ${r.elevation_m.mean})`, 'mean slope °': r.slope_deg?.mean, '90th pct slope °': r.slope_deg?.p90 })}</div><div><b>Within candidate loss</b>${l.status === 'ok' ? kvS({ 'elevation m': `${l.elevation_m.min}–${l.elevation_m.max}`, 'mean slope °': l.slope_deg.mean, 'share steeper than 15°': fmt.pct(l.share_steeper_than_screen) }) : '<span class="help">n/a</span>'}</div></div><div class="help">${esc(r.source || '')}</div>`; }
  if (name === 'nearby_features_osm') return `${kvS({ 'nearest road': s.nearest_road && `${s.nearest_road.name || s.nearest_road.type} · ${s.nearest_road.distance_m} m`, 'nearest settlement': s.nearest_settlement && `${s.nearest_settlement.name || '?'} · ${s.nearest_settlement.distance_m} m`, 'nearest waterway': s.nearest_waterway && `${s.nearest_waterway.name || s.nearest_waterway.type} · ${s.nearest_waterway.distance_m} m`, 'protected areas': s.protected_areas_status === 'ok' ? (s.protected_areas_containing_centroid.map((p) => p.name).join(', ') || 'none mapped here') : s.protected_areas_status })}<div class="help">${esc(s.attribution)}. ${esc(s.caveat)}</div>`;
  if (name === 'rainfall') return kvS({ 'last 7 days mm': s.last_7_days_mm, 'last 30 days mm': s.last_30_days_mm, '5-yr mean (same 30 d) mm': s.climatology_30d_mm, 'anomaly ratio': s.anomaly_ratio, 'next 7 days forecast mm': s.next_7_days_forecast_mm }) + `<div class="help">${esc(s.attribution)}. ${esc(s.note)}</div>`;
  const flat = pick(s); const note = flat.note; delete flat.note;
  return kvS(flat) + (note ? `<div class="help" style="margin-top:6px">${esc(note)}</div>` : '');
}
const kvS = (o) => `<dl class="kv">${Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${typeof v === 'object' ? `<code>${esc(JSON.stringify(v))}</code>` : esc(v)}</dd>`).join('')}</dl>`;

export async function render(root, { params }) {
  const regions = await api('/regions');
  if (!regions.length) { root.innerHTML = empty('No regions yet', 'Create a monitoring region first.', '❦'); return; }
  let past = await api('/environment/assessments');
  root.innerHTML = html`
    <div class="grid split">
      <div class="stack">
        <div class="panel"><h3>Generate an environmental impact assessment</h3><p class="sub">Combines stored detections with elevation (Copernicus DEM), rainfall (Open-Meteo) and OpenStreetMap features. Each section says whether it is measured, computed, a screening indicator, or unavailable. Nothing is estimated from area alone.</p>
          <form id="f" class="stack"><label class="f">Region<select name="r">${raw(regions.map((r) => `<option value="${esc(r.id)}" ${r.id === params[0] ? 'selected' : ''}>${esc(r.name)}</option>`).join(''))}</select></label>
          <label class="f">Detection (optional)<select name="c" id="chSel"><option value="">Latest candidate loss for the region</option></select></label>
          <div class="row"><button class="button primary" id="go" ${can('admin', 'reviewer', 'analyst', 'officer') ? '' : 'disabled'}>Generate assessment</button><span class="help" id="st"></span></div></form></div>
        <div class="panel"><h3>Previous assessments</h3><div id="past"></div></div>
      </div>
      <div class="panel"><h3>Study area</h3><div id="map" class="map-box"></div></div>
    </div>
    <div id="report" style="margin-top:16px"></div>`;
  const { map, group } = await createMap($('#map')); const g = group('Region');
  const fillChanges = async () => { const r = $('#f [name=r]').value; const cs = (await api(`/forest/changes?region_id=${r}&limit=50`)).filter((c) => c.disturbance_type === 'CANDIDATE_FOREST_LOSS'); $('#chSel').innerHTML = '<option value="">Latest candidate loss for the region</option>' + cs.map((c) => `<option value="${esc(c.id)}" ${c.id === params[1] ? 'selected' : ''}>${esc(fmt.date(c.baseline_date))} → ${esc(fmt.date(c.comparison_date))} · ${esc(fmt.ha(c.area_hectares))}</option>`).join(''); const reg = regions.find((x) => x.id === r); g.clearLayers(); const ls = [addGeo(g, reg.geometry, STYLE.region)]; cs.forEach((c) => ls.push(addGeo(g, c.geometry, STYLE.loss))); fit(map, ls); };
  $('#f [name=r]').onchange = fillChanges; await fillChanges();
  const paintPast = () => { $('#past').innerHTML = past.length ? html`<table class="t"><tbody>${past.map((a) => raw(`<tr data-id="${esc(a.id)}"><td><b>${esc(a.region_name)}</b><div class="help">${esc(fmt.dt(a.created_at))} · ${esc(fmt.status(a.status))}</div></td></tr>`))}</tbody></table>` : empty('None yet', 'Generated assessments are stored here.', '❦'); $$('#past tr[data-id]').forEach((tr) => (tr.onclick = async () => show(await api('/environment/assessments/' + tr.dataset.id)))); };

  function show(a) {
    const order = Object.keys(a.sections);
    $('#report').innerHTML = html`<div class="panel"><div class="row"><div class="grow"><h3>Environmental impact report — ${a.region_name}</h3><p class="sub">Generated ${fmt.dt(a.generated_at)} · ${fmt.ha(a.region_area_hectares)} region${a.change_id ? '' : ' · no detection linked'}</p></div><button class="button primary" id="pdf">Download PDF</button><button class="button subtle" id="csv">Download CSV</button></div>
      ${raw(a.warnings.length ? `<div class="warn-box">${a.warnings.map(esc).join('<br>')}</div>` : '')}
      <div class="note-box" style="margin:10px 0"><b>Executive summary.</b> ${raw(esc(summary(a)))}</div>
      <div class="grid g2">${order.map((k) => raw(`<div class="panel" style="box-shadow:none"><div class="row"><h4 class="grow">${esc(TITLES[k] || fmt.status(k))}</h4>${classTag(a.sections[k].evidence_class).s}</div>${sectionBody(k, a.sections[k])}</div>`))}</div>
      <h4 style="margin-top:14px">Areas requiring field investigation and recommended actions</h4><ul class="steps">${a.recommended_follow_up.map((x) => raw(`<li>${esc(x)}</li>`))}</ul>
      <div class="help" style="margin-top:10px">Method: ${a.method_notes.map(esc).join(' · ')}</div></div>`;
    $('#pdf').onclick = () => download(`/environment/assessments/${a.id}/report.pdf`, `rakshak-environment-${a.id.slice(0, 8)}.pdf`);
    $('#csv').onclick = () => download(`/environment/assessments/${a.id}/report.csv`, `rakshak-environment-${a.id.slice(0, 8)}.csv`);
    $('#report').scrollIntoView({ behavior: 'smooth' });
  }
  const summary = (a) => { const s = a.sections, fc = s.forest_change; const bits = []; bits.push(fc.evidence_class === 'computed' ? `A rule-based Sentinel-1/2 analysis found ${fc.candidate_loss_hectares} ha of candidate forest loss between ${fmt.date(fc.baseline_date)} and ${fmt.date(fc.comparison_date)} (unverified).` : 'No detected change is stored for this region.'); if (s.erosion_susceptibility.level) bits.push(`Slope-based erosion screening is ${s.erosion_susceptibility.level}.`); if (s.rainfall.evidence_class === 'measured' && s.rainfall.anomaly_ratio) bits.push(`Recent 30-day rainfall is ${s.rainfall.anomaly_ratio}× the 5-year mean.`); bits.push('Biodiversity, carbon and watershed effects are not assessed because the required datasets are not available.'); return bits.join(' '); };

  $('#f').onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target), b = $('#go'); b.disabled = true;
    try { const j = await api('/environment/assessments', { method: 'POST', body: { region_id: f.get('r'), change_id: f.get('c') || null } }); const r = await waitJob(j.job_id, (_, s) => ($('#st').textContent = `Fetching terrain, rainfall and map features… ${s}s`), 3000); $('#st').textContent = ''; past = await api('/environment/assessments'); paintPast(); show(r); } catch (err) { toast(err.message, 'error'); $('#st').textContent = err.message; } finally { b.disabled = false; }
  };
  paintPast(); if (past[0]) show(await api('/environment/assessments/' + past[0].id));
  return { destroy: () => map.remove() };
}

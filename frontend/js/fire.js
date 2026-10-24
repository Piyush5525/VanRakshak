// Fire response workspace for the live map: one incident record drives forecast, impact, route, Telegram, IVR and the event log.
import { api, esc, html, raw, toast, waitJob } from './core.js';
import { pop, historyHtml } from './progress.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const FLOW = ['NEEDS_VERIFICATION', 'CONFIRMED', 'DISPATCH_REQUESTED', 'DISPATCHED', 'MONITORING', 'RESOLVED'];
const STATUS_CHIP = { DETECTED: 'amber', NEEDS_VERIFICATION: 'amber', INCONCLUSIVE: 'amber', CONFIRMED: 'red', DISPATCH_REQUESTED: 'red', DISPATCHED: 'green', MONITORING: 'green', RESOLVED: '', REJECTED: '', CANCELLED: '', FAILED: 'red' };
const ICON = { ok: '✓', failed: '✗', pending: '⏳', retried: '↻', skipped: '⊘', info: '•' };
const S = { list: [], sel: null, tab: 'overview', hz: '12', all: false, events: [], lastId: 0, filters: new Set(), scope: 'all', status: null, ctx: null, layer: null, timers: {} };
const curRole = () => localStorage.getItem('rk.role') || 'reviewer';
const age = (t) => { const m = Math.max(0, Math.round((Date.now() - new Date(t).getTime()) / 60000)); return m < 60 ? `${m} min` : m < 2880 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`; };
const lab = (c, t) => `<span class="lab ${c}">${esc(t)}</span>`;

export function init(ctx) {
  S.ctx = ctx; S.layer = L.layerGroup().addTo(ctx.map);
  $('#rtabs').onclick = (e) => { const t = e.target.dataset.t; if (t) switchTab(t); };
  refreshAll(); setInterval(pollEvents, 3000); pollEvents();
}
export function switchTab(t) {
  $$('#rtabs button').forEach((b) => b.classList.toggle('on', b.dataset.t === t));
  $('#rightBody').hidden = t !== 'inc'; $('#fireBody').hidden = t !== 'fire'; $('#forestBody').hidden = t !== 'tree';
  window.dispatchEvent(new CustomEvent('rk:tab', { detail: t }));
  if (t === 'fire') { paint(); } else { S.layer.clearLayers(); }
}
export async function investigate(f) {
  try {
    const r = await api('/fire/incidents', { method: 'POST', body: { source: 'firms', lat: f[0], lon: f[1], observed_epoch_s: f[2] } });
    toast(r.created ? `Incident ${r.incident.code} created. It needs verification: a hotspot is not a confirmed fire.` : `Already investigating as ${r.incident.code}`);
    await refreshAll(); switchTab('fire'); select(r.incident.id);
    $('#right').classList.remove('hidden');
  } catch (e) { toast(e.message, 'error'); }
}

async function refreshAll() {
  try { S.list = await api('/fire/incidents'); } catch (e) { toast(e.message, 'error'); }
  S.status = await api('/fire/status').catch(() => null);
  if ($('#fireBody') && !$('#fireBody').hidden) paint();
  drawPins();
}
async function reload(id = S.sel) {
  const i = await api('/fire/incidents/' + id); const k = S.list.findIndex((x) => x.id === id);
  if (k >= 0) S.list[k] = i; else S.list.unshift(i);
  paint(); drawPins(); return i;
}
const cur = () => S.list.find((i) => i.id === S.sel);

function drawPins() {
  if (!S.ctx) return;
  S.layer.clearLayers();
  const i = cur();
  if (!i || $('#fireBody').hidden) return;   // the map stays clear until an incident is chosen
  L.marker([i.lat, i.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: '<div class="pin fire"><span>🔥</span></div>' }), title: i.code }).addTo(S.layer);
  drawIncidentLayers(i);
}

function drawIncidentLayers(i) {
  const m = S.ctx.map;
  (i.detection?.hotspot_history || []).forEach((h) => L.circleMarker([h.lat, h.lon], { radius: 5, color: '#fff', weight: 1, fillColor: '#e5252a', fillOpacity: 0.9 }).bindPopup(`Hotspot<br>${esc(h.observed_at)}${h.frp_mw != null ? '<br>FRP ' + esc(h.frp_mw) + ' MW' : ''}`).addTo(S.layer));
  const fc = i.forecast;
  if (fc?.horizons) {
    const draw = (h, solid) => { const c = fc.horizons[h].contours; [['possible_10', '#e3a14b', 0.18], ['likely_50', '#d9382a', 0.32]].forEach(([k, col, op]) => c[k] && L.geoJSON(c[k], { style: { color: col, weight: solid ? 2 : 1.5, fillColor: col, fillOpacity: solid ? op : 0, dashArray: solid ? null : '6 5' }, interactive: false }).addTo(S.layer)); };
    if (S.all) ['3', '6', '12'].filter((h) => h !== S.hz).forEach((h) => draw(h, false)); draw(S.hz, true);
  }
  const r = i.route;
  if (r?.status === 'OK') {
    (r.alternatives || []).forEach((a) => a.geometry && L.geoJSON(a.geometry, { style: { color: '#6b7c74', weight: 4, opacity: 0.7, dashArray: '8 8' }, interactive: false }).addTo(S.layer));
    L.geoJSON(r.geometry, { style: { color: '#14573c', weight: 6, opacity: 0.9 }, interactive: false }).addTo(S.layer);
    L.marker([r.station.lat, r.station.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: '<div class="pin" style="background:#14573c"><span>🚒</span></div>' }) }).bindPopup(`<b>${esc(r.station.name)}</b><br>${esc(r.station.data_note)}<br>${esc(r.distance_km)} km, about ${Math.round(r.duration_min)} min by road`).addTo(S.layer);
  }
}

async function select(id) {
  S.sel = id; S.tab = 'overview'; const i = await reload(id).catch(() => cur());
  S.ctx.map.flyTo([i.lat, i.lon], Math.max(S.ctx.map.getZoom(), 10), { duration: 0.6 }); drawPins();
}

// ------------------------------------------------------------------ rendering
function paint() {
  const box = $('#fireBody'); if (!box || box.hidden) return;
  if (!S.sel) return paintList(box);
  paintDetail(box);
}
function paintList(box) {
  const list = S.list.filter((i) => !['REJECTED', 'CANCELLED'].includes(i.status));
  const row = (i) => `<button class="row-item" data-id="${esc(i.id)}"><span style="font-size:20px">🔥</span><span class="grow"><b>${esc(i.detection?.place?.name || i.code)}</b><small>${esc(i.code)} · ${esc(age(i.created_at))} ago${i.is_demo ? ' · <span class="tag">Scenario</span>' : ''}</small></span><span class="chip ${STATUS_CHIP[i.status] || ''}">${esc(i.status_label)}</span></button>`;
  box.innerHTML = html`<div class="side-head" style="padding-left:0"><div><h2>Fire incidents</h2><div class="sub">Choose one to see it on the map</div></div></div>
    <div class="list">${list.length ? raw(list.map(row).join('')) : raw('<div class="empty"><b>No fire incidents yet</b>Add the example incidents below, or turn on NASA fire hotspots on the left, click a hotspot and press Investigate.</div>')}</div>
    <div class="row2" style="margin-top:10px"><button class="btn sm" id="loadScn">＋ Add example incidents</button>${raw(list.some((i) => i.is_demo) ? '<button class="btn sm" id="rmScn">Remove examples</button>' : '')}</div>
    <p class="fine">A satellite hotspot is not a confirmed fire. Nothing is sent until a person confirms it.</p>`;
  $('#loadScn').onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api('/fire/scenarios/load', { method: 'POST' }); await refreshAll(); toast(r.created ? `${r.created} incidents added. Forecasts and routes are being prepared.` : 'The example incidents are already here.'); if (r.job_id) waitJob(r.job_id, null, 3000, 30 * 60 * 1000).then(refreshAll).catch(() => {}); }
    catch (err) { toast(err.message, 'error'); } finally { e.target.disabled = false; }
  };
  $('#rmScn')?.addEventListener('click', () => sheet('Remove example incidents', '<p>Removes only the example incidents. Real incidents are not touched.</p>', [{ label: 'Remove', cls: 'warn', run: async () => { await api('/fire/demo/reset', { method: 'POST' }); S.sel = null; await refreshAll(); } }]));
  $$('#fireBody .row-item[data-id]').forEach((b) => (b.onclick = () => select(b.dataset.id)));
}

function paintDetail(box) {
  const i = cur(); if (!i) { S.sel = null; return paintList(box); }
  const tabs = [['overview', 'Overview'], ['forecast', 'Forecast'], ['impact', 'Impact'], ['route', 'Route'], ['comms', 'Alert']];
  box.innerHTML = html`<button class="back" id="fback">‹ All fire incidents</button>
    <div class="row-item" style="border:0;padding:0"><span style="font-size:24px">🔥</span><span class="grow"><h2 style="font-size:17px">${i.detection?.place?.name || i.code}</h2><div class="sub">${i.code} · ${i.lat.toFixed(4)}, ${i.lon.toFixed(4)} ${raw(i.is_demo ? '<span class="tag">Scenario</span>' : '')}</div></span><span class="chip ${STATUS_CHIP[i.status] || ''}">${i.status_label}</span></div>
    <div class="sub-tabs" id="stabs">${raw(tabs.map(([k, n]) => `<button data-k="${k}" class="${S.tab === k ? 'on' : ''}">${n}</button>`).join(''))}</div>
    <div id="pane"></div>`;
  $('#fback').onclick = () => { S.sel = null; drawPins(); paint(); };
  $('#stabs').onclick = (e) => { const k = e.target.dataset.k; if (k) { S.tab = k; paintDetail(box); drawPins(); const i2 = cur(); if (k === 'route' && i2.route?.status === 'OK') S.ctx.map.fitBounds(L.geoJSON(i2.route.geometry).getBounds().pad(0.15)); if (k === 'forecast' && i2.forecast?.horizons) { const g = i2.forecast.horizons[S.hz].contours.possible_10; if (g) S.ctx.map.fitBounds(L.geoJSON(g).getBounds().pad(0.2)); } } };
  ({ overview, forecast, impact, route, comms })[S.tab](i, $('#pane'));
}

// ---- overview
function overview(i, pane) {
  const d = i.detection || {}; const cur_ = d.currency;
  const curLab = { current: ['obs', 'CURRENT'], delayed: ['est', 'DELAYED'], historical: ['na', 'HISTORICAL'], demo: ['na', 'SCENARIO'], 'n/a': ['na', 'MANUAL REPORT'] }[cur_] || ['na', cur_ || '?'];
  const roles = curRole(); const canAct = ['admin', 'reviewer', 'officer'].includes(roles), canDispatch = ['admin', 'officer'].includes(roles);
  const actions = [];
  if (['NEEDS_VERIFICATION', 'INCONCLUSIVE', 'DETECTED'].includes(i.status)) actions.push(`<div class="sub">Verify before anything is sent. A thermal anomaly can also be crop burning, a kiln or industrial heat.</div><input type="text" id="vnote" placeholder="Evidence / note (e.g. ranger saw smoke)" style="margin:6px 0"><div class="row2"><button class="btn primary sm" data-v="confirm" ${canAct ? '' : 'disabled'}>Confirm fire</button><button class="btn sm" data-v="inconclusive" ${canAct ? '' : 'disabled'}>Inconclusive</button><button class="btn sm danger" data-v="reject" ${canAct ? '' : 'disabled'}>Reject</button></div>`);
  if (i.status === 'DISPATCH_REQUESTED') actions.push(`<div class="sub">No responder has confirmed yet. Record an acknowledgement only when one has actually been received.</div><div class="row2"><input type="text" id="anote" placeholder="e.g. Station officer confirmed by radio" class="grow"><button class="btn sm" id="ack" ${canDispatch ? '' : 'disabled'}>Record acknowledgement</button></div>`);
  if (['DISPATCHED', 'MONITORING'].includes(i.status)) actions.push(`<div class="row2"><input type="text" id="unote" placeholder="Field update" class="grow"><button class="btn sm" id="upd">Add update</button></div><button class="btn sm danger" id="resolve" style="margin-top:6px" ${canDispatch ? '' : 'disabled'}>Mark resolved</button>`);
  if (!['RESOLVED', 'REJECTED', 'CANCELLED'].includes(i.status)) actions.push('<button class="btn sm" id="cancel" style="margin-top:6px">Cancel incident</button>');
  pane.innerHTML = html`<div class="sect"><h4>${raw(lab(curLab[0], curLab[1]))} Detection</h4><dl class="kv">
      <dt>Source</dt><dd>${d.product_name || d.source || i.source}</dd><dt>Satellite</dt><dd>${d.satellite || '—'}</dd>
      <dt>Detected</dt><dd>${d.observed_at ? new Date(d.observed_at).toLocaleString() : '—'}${d.age_hours_at_creation != null ? ` (${d.age_hours_at_creation} h old when opened)` : ''}</dd>
      <dt>Fire power</dt><dd>${d.frp_mw != null ? d.frp_mw + ' MW' : 'not provided'}</dd>
      </dl>
      <div class="note">${d.note || ''}</div></div>
    <div class="sect"><h4>Decision</h4>${raw(actions.join('') || '<div class="sub">No actions available.</div>')}</div>
    <div class="sect"><h4>Record</h4><div id="activity">${raw(activity(i.id))}</div></div>
    <div class="sect"><h4>Nearby</h4><div id="nearby" class="sub">Loading registered fire stations…</div><button class="btn sm" id="ctxMore" style="margin-top:6px">Load terrain and map features</button><div id="ctxOut"></div></div>
    <div class="sect"><h4>Field updates</h4>${raw((i.field_updates || []).length ? `<ul class="steps" style="padding-left:16px;margin:0;font-size:12.5px">${i.field_updates.map((u) => `<li>${esc(new Date(u.at).toLocaleTimeString())} · ${esc(u.by)}: ${esc(u.text)}</li>`).join('')}</ul>` : '<div class="sub">None.</div>')}</div>
    <div class="row2"><button class="btn sm" id="pdf">⬇ Report (PDF)</button></div>`;
  $$('[data-v]', pane).forEach((b) => (b.onclick = () => act(() => api(`/fire/incidents/${i.id}/verify`, { method: 'POST', body: { decision: b.dataset.v, note: $('#vnote').value } }), 'Decision recorded')));
  $('#auth')?.addEventListener('click', () => sheet('Authorise response notification', `<p>This authorises <b>${esc(i.code)}</b> for notification. It does <b>not</b> contact anyone by itself and does <b>not</b> dispatch a station. You choose each notification separately.</p><label class="f">Note<input type="text" id="authnote" placeholder="Reason / authority"></label>`, [{ label: 'Authorise', cls: 'primary', run: async () => { await api(`/fire/incidents/${i.id}/authorise`, { method: 'POST', body: { note: $('#authnote').value } }); await reload(); } }]));
  $('#ack')?.addEventListener('click', () => act(() => api(`/fire/incidents/${i.id}/acknowledge`, { method: 'POST', body: { note: $('#anote').value } }), 'Acknowledgement recorded'));
  $('#upd')?.addEventListener('click', () => act(() => api(`/fire/incidents/${i.id}/field-update`, { method: 'POST', body: { note: $('#unote').value } }), 'Update added'));
  $('#resolve')?.addEventListener('click', () => sheet('Mark resolved', '<p>Close this incident as resolved?</p>', [{ label: 'Mark resolved', cls: 'primary', run: async () => { await api(`/fire/incidents/${i.id}/resolve`, { method: 'POST', body: { note: 'resolved' } }); await reload(); } }]));
  $('#cancel')?.addEventListener('click', () => sheet('Cancel incident', '<p>Cancel this incident? This cannot be undone.</p>', [{ label: 'Cancel incident', cls: 'warn', run: async () => { await api(`/fire/incidents/${i.id}/cancel`, { method: 'POST', body: { note: 'cancelled' } }); await reload(); } }]));
  $('#pdf').onclick = () => import('./core.js').then((c) => c.download(`/fire/incidents/${i.id}/report.pdf`, `${i.code}-report.pdf`));
  api(`/fire/context?lat=${i.lat}&lon=${i.lon}&parts=stations`).then((c) => { const n = $('#nearby'); if (!n) return; n.innerHTML = c.stations.length ? `${c.stations.map((s) => `<div>🚒 <b>${esc(s.name)}</b> <span class="sub">${s.straight_line_km} km straight line · ${esc(s.source)}${s.verified ? '' : ' · unverified'}</span></div>`).join('')}<div class="sub">Straight-line distance only; use the Route tab for road distance and time.</div>` : '<div class="sub">No stations registered near here yet. Open the Route tab and press “Find stations”.</div>'; });
  $('#ctxMore').onclick = async () => { $('#ctxOut').innerHTML = '<div class="sub"><span class="spin"></span>Loading…</div>'; const c = await api(`/fire/context?lat=${i.lat}&lon=${i.lon}&parts=terrain,osm`).catch((e) => ({ err: e.message })); const o = c.osm || {}, t = c.terrain || {}; $('#ctxOut').innerHTML = c.err ? `<div class="note warn">${esc(c.err)}</div>` : `<dl class="kv"><dt>Elevation</dt><dd>${t.elevation_m ?? '—'} m ${lab('obs', 'OBSERVED')}</dd><dt>Slope</dt><dd>${t.slope_deg ?? '—'}°</dd><dt>Nearest road</dt><dd>${o.nearest_road ? esc((o.nearest_road.name || o.nearest_road.type) + ' · ' + o.nearest_road.distance_m + ' m') : 'none mapped'}</dd><dt>Nearest settlement</dt><dd>${o.nearest_settlement ? esc((o.nearest_settlement.name || '?') + ' · ' + o.nearest_settlement.distance_m + ' m') : 'none mapped'}</dd></dl><div class="sub">${esc(o.status === 'unavailable' ? 'Map features unavailable: ' + o.reason : 'OpenStreetMap + Copernicus DEM; mapping is incomplete.')}</div>`; };
}

// ---- forecast
function forecast(i, pane) {
  const fc = i.forecast;
  const head = `<div class="banner2 red"><b>PREDICTED RISK FOOTPRINT, not a fire perimeter.</b> Prototype model, not validated for Indian forests.${i.is_demo ? ' Weather is assumed for this scenario.' : ''}</div>`;
  if (!fc) { pane.innerHTML = head + '<div class="empty"><b>No forecast yet</b>Run the 3, 6 and 12 hour model for this incident.</div><button class="btn primary block" id="runfc">Generate forecast</button>'; $('#runfc').onclick = () => job('/fire/incidents/' + i.id + '/forecast', 'Forecast generated'); return; }
  if (fc.status === 'UNAVAILABLE') { pane.innerHTML = head + `<div class="banner2 amber">Forecast unavailable: ${esc(fc.reason)}</div><button class="btn block" id="runfc">Try again</button>`; $('#runfc').onclick = () => job('/fire/incidents/' + i.id + '/forecast', 'Forecast generated'); return; }
  const v = fc.horizons[S.hz]; const w = fc.inputs.weather;
  pane.innerHTML = html`${raw(head)}${raw(fc.status !== 'OK' ? `<div class="banner2 amber"><b>${esc(fc.status)}</b>: ${esc((fc.degradation_notes || []).join('; '))}</div>` : '')}
    <div class="hz" id="hz">${raw(['3', '6', '12'].map((h) => `<button data-h="${h}" class="${S.hz === h ? 'on' : ''}">${h} h</button>`).join(''))}</div>
    <label class="switch"><input type="checkbox" id="allh" ${S.all ? 'checked' : ''}><span>Compare all horizons</span></label>
    <div class="sect"><h4>${raw(lab('est', 'ESTIMATED'))} ${S.hz}-hour potential exposure</h4><dl class="kv"><dt>Median area</dt><dd><b>${v.area_ha.p50} ha</b> <span class="sub">(P10–P90 ${v.area_ha.p10}–${v.area_ha.p90})</span></dd><dt>Likely (≥50% of runs)</dt><dd>${v.contour_area_ha.likely_50} ha</dd><dt>Possible (≥10%)</dt><dd>${v.contour_area_ha.possible_10} ha</dd><dt>Head spread</dt><dd>${v.head_distance_km.p50} km (P10–P90 ${v.head_distance_km.p10}–${v.head_distance_km.p90})</dd><dt>Direction</dt><dd>towards ${v.spread_toward_deg}° (wind from ${v.wind_from_deg}°)</dd></dl>
      <div class="legend"><span><i style="background:#d9382a"></i>likely ≥50%</span><span><i style="background:#e3a14b"></i>possible ≥10%</span></div></div>
    <div class="sect"><h4>Inputs ${raw(lab(w.illustrative || fc.demo ? 'est' : 'mod', w.illustrative ? 'SCENARIO WEATHER' : 'MODELLED'))}</h4><dl class="kv"><dt>Wind</dt><dd>${v.wind_kmh} km/h</dd><dt>Temp / RH</dt><dd>${v.temp_c} °C / ${v.rh_percent}%</dd><dt>Drought factor</dt><dd>${v.drought_factor} / 10</dd><dt>Slope along spread</dt><dd>${v.slope_deg_along_spread}°</dd><dt>Weather source</dt><dd>${w.source}</dd><dt>Generated</dt><dd>${new Date(fc.generated_at).toLocaleString()}</dd><dt>Seed</dt><dd>${fc.seed}</dd></dl></div>
    <details><summary>Assumptions and limitations</summary><ul class="steps" style="font-size:12px;padding-left:16px">${raw(fc.assumptions.concat(fc.limitations).map((a) => `<li>${esc(a)}</li>`).join(''))}</ul></details>
    <div class="row2" style="margin-top:8px"><button class="btn sm" id="runfc">Regenerate</button><button class="btn sm" id="fcimg">Preview image</button></div><div id="fcprev"></div>`;
  $('#hz').onclick = (e) => { const h = e.target.dataset.h; if (h) { S.hz = h; paintDetail($('#fireBody')); drawPins(); } };
  $('#allh').onchange = (e) => { S.all = e.target.checked; drawPins(); };
  $('#runfc').onclick = () => job('/fire/incidents/' + i.id + '/forecast', 'Forecast generated');
  $('#fcimg').onclick = async () => { const b = await api(`/fire/incidents/${i.id}/map.png?kind=forecast&horizon=${S.hz}`, { blob: true }).catch((e) => toast(e.message, 'error')); if (b) $('#fcprev').innerHTML = `<img class="img-prev" src="${URL.createObjectURL(b)}" alt="Forecast map">`; };
}

// ---- impact
function impact(i, pane) {
  const m = i.impact;
  if (!m) { pane.innerHTML = '<div class="empty"><b>No impact estimate yet</b>Uses the forecast, nearby places, modelled air quality and optional burned-area inputs.</div><button class="btn primary block" id="runimp">Generate impact estimate</button>'; $('#runimp').onclick = () => job('/fire/incidents/' + i.id + '/impact', 'Impact estimate generated', {}); return; }
  const pe = m.potential_exposure, aq = m.air_quality, c = m.carbon, n = m.nearby;
  pane.innerHTML = html`<div class="sect"><h4>${raw(lab('est', 'ESTIMATED'))} Potential exposure area</h4><div class="note warn">${pe.label || pe.reason}. It is not burned area. No fire perimeter is available.</div>${raw(pe.by_horizon_h ? `<table class="t"><tr><th>Horizon</th><th>Median</th><th>P10–P90</th></tr>${Object.entries(pe.by_horizon_h).map(([h, v]) => `<tr><td>${esc(h)} h</td><td><b>${esc(v.area_ha_p50)} ha</b></td><td>${esc(v.area_ha_p10_p90.join('–'))}</td></tr>`).join('')}</table>` : '')}</div>
    <div class="sect"><h4>${raw(lab('mod', 'MODELLED'))} Air quality</h4>${raw(aq.status === 'ok' ? `<table class="t"><tr><th></th><th>Now</th><th>Max 24 h</th></tr><tr><td>PM2.5</td><td><b>${esc(aq.pm2_5_ug_m3.now)}</b> µg/m³</td><td>${esc(aq.pm2_5_ug_m3.max_24h)}</td></tr><tr><td>PM10</td><td><b>${esc(aq.pm10_ug_m3.now)}</b> µg/m³</td><td>${esc(aq.pm10_ug_m3.max_24h)}</td></tr></table><div class="note">${esc(aq.kind)} ${esc(aq.measured_stations)}</div>${(aq.potentially_downwind_settlements || []).length ? `<div style="margin-top:6px"><b>Potentially downwind (within 25 km):</b> ${aq.potentially_downwind_settlements.map((p) => esc(p.name + ' ' + p.distance_km + ' km')).join(', ')}</div><div class="sub">${esc(aq.downwind_note)}</div>` : ''}` : `<div class="banner2 amber">Air quality unavailable: ${esc(aq.reason)}</div>`)}</div>
    <div class="sect"><h4>Places near the incident</h4>${raw(n.status === 'ok' ? `<table class="t">${n.settlements.slice(0, 8).map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.type)}</td><td>${esc(p.distance_km)} km</td><td>${p.inside_possible_footprint ? '<span class="chip red">inside footprint</span>' : p.downwind ? '<span class="chip amber">downwind</span>' : ''}</td></tr>`).join('')}</table><div class="sub">${esc(n.source)}</div>` : `<div class="banner2 amber">Unavailable: ${esc(n.reason)}</div>`)}</div>
    <div class="sect"><h4>${raw(lab(c.evidence_class === 'illustrative' ? 'est' : c.evidence_class === 'modelled' ? 'mod' : 'na', (c.evidence_class || 'n/a').toUpperCase()))} Carbon (CO₂)</h4>${raw(c.status === 'CALCULATED' ? `<div><b>${esc(c.tonnes_co2.central)} t CO₂</b> <span class="sub">(low ${esc(c.tonnes_co2.low)} – high ${esc(c.tonnes_co2.high)})</span></div><div class="sub">${esc(c.method)}; area ${esc(c.inputs.area_ha)} ha, fuel ${esc(c.inputs.fuel_load_t_dm_per_ha)} t/ha. Area basis: ${esc(c.inputs.area_basis)}.</div><div class="note warn">${esc(c.uncertainty)}</div>` : `<div class="banner2 amber">${esc(c.reason)}</div>`)}
      <details><summary>Enter burned area and fuel load</summary><div class="grid2" style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px"><input type="number" id="cha" min="0" step="any" placeholder="Burned area (ha)"><input type="number" id="cfl" min="0" step="any" placeholder="Fuel load (t/ha)"></div><div class="sub">From a field estimate or mapped perimeter, not from hotspot counts.</div><button class="btn sm" id="ccalc" style="margin-top:6px">Calculate</button></details></div>
    <button class="btn sm" id="runimp">Regenerate</button>`;
  $('#runimp').onclick = () => job('/fire/incidents/' + i.id + '/impact', 'Impact estimate generated', {});
  $('#ccalc').onclick = () => { const a = parseFloat($('#cha').value), f = parseFloat($('#cfl').value); if (!(a > 0 && f > 0)) return toast('Enter both values', 'error'); job('/fire/incidents/' + i.id + '/impact', 'Carbon calculated', { burned_area_ha: a, fuel_load_t_ha: f }); };
}

// ---- route
function route(i, pane) {
  const r = i.route;
  const find = '<div class="row2"><button class="btn sm" id="findst">Find stations</button><button class="btn primary sm" id="calcrt">Calculate route</button></div>';
  if (!r) { pane.innerHTML = '<div class="empty"><b>No route yet</b>Looks up fire stations near the incident and compares road routes.</div>' + find; wireRoute(i); return; }
  if (r.status !== 'OK') { pane.innerHTML = `<div class="banner2 red"><b>${esc(r.status)}</b>: ${esc(r.reason || '')}</div>${find}`; wireRoute(i); return; }
  pane.innerHTML = html`<div class="sect"><h4>🚒 ${r.station.name}</h4><div class="sub">${r.station.data_note} · ${r.station.source}</div><div class="big" style="margin:6px 0"><b style="font-size:26px;color:#14573c">${r.distance_km} km</b><span>about ${Math.round(r.duration_min)} min by road</span></div>
      <div class="sub">Objective: ${r.objective}. Provider: ${r.provider}. Calculated ${new Date(r.calculated_at).toLocaleTimeString()}.</div>${raw(r.selected_manually ? '<div class="sub">Station chosen manually.</div>' : '')}
      <div class="row2" style="margin-top:6px"><a class="btn sm" href="${esc(r.navigation_url)}" target="_blank" rel="noopener">Open in Google Maps</a></div></div>
    <div class="sect"><h4>Candidates compared</h4><table class="t"><tr><th>Station</th><th>Straight</th><th>Road</th><th></th></tr>${raw(r.candidates.map((c) => `<tr><td>${esc(c.station_name)}</td><td>${esc(c.straight_line_km)} km</td><td>${c.distance_km != null ? esc(c.distance_km + ' km · ' + Math.round(c.duration_min) + ' min') : '<span class="chip red">' + esc(c.status) + '</span>'}</td><td>${c.selected ? '✓' : c.distance_km != null ? `<button class="btn sm" data-st="${esc(c.station_id)}">Use</button>` : ''}</td></tr>`).join(''))}</table></div>
    ${raw((r.reasons || []).length ? r.reasons.map((x) => `<div class="banner2 amber">${esc(x.text)}</div>`).join('') : '')}
    <div class="note warn"><b>Not verified:</b> ${raw(r.caveats.map(esc).join(' '))}</div>${find}`;
  wireRoute(i);
  $$('[data-st]', pane).forEach((b) => (b.onclick = () => job(`/fire/incidents/${i.id}/route`, 'Route recalculated', { station_id: b.dataset.st })));
}
function wireRoute(i) {
  $('#findst')?.addEventListener('click', () => job(`/fire/incidents/${i.id}/stations/discover`, 'Station lookup finished'));
  $('#calcrt')?.addEventListener('click', () => job(`/fire/incidents/${i.id}/route`, 'Route calculated', {}));
}

// ---- alert (Telegram + phone call)
const SETUP_HINT = { OMNIDIM_API_KEY: 'OMNIDIM_API_KEY', OMNIDIM_AGENT_ID: 'OMNIDIM_AGENT_ID', 'IVR_PROVIDER=omnidimension': 'IVR_PROVIDER=omnidimension', TWILIO_ACCOUNT_SID: 'Twilio account SID', TWILIO_AUTH_TOKEN: 'Twilio auth token', TWILIO_FROM_NUMBER: 'Twilio phone number', IVR_RESPONSE_NUMBER: 'the number to call', IVR_APPROVED_NUMBERS: 'approved numbers', 'IVR_PROVIDER=twilio': 'IVR_PROVIDER=twilio' };
function comms(i, pane) {
  const tg = i.notify?.telegram || [], calls = i.ivr?.calls || [], s = S.status;
  const confirmed = ['CONFIRMED', 'DISPATCH_REQUESTED', 'DISPATCHED', 'MONITORING'].includes(i.status), canD = ['admin', 'officer'].includes(curRole());
  const dis = confirmed && canD ? '' : 'disabled';
  const chip = (st) => `<span class="chip ${/SENT|COMPLETED|ANSWERED/.test(st) ? 'green' : /FAILED|BUSY|NO_ANSWER/.test(st) ? 'red' : 'amber'}">${esc(({ SENT: 'Sent', COMPLETED: 'Call finished', ANSWERED: 'Answered', INITIATED: 'Calling', RINGING: 'Ringing', QUEUED: 'Queued', FAILED: 'Failed', BUSY: 'Busy', NO_ANSWER: 'No answer' })[st] || st)}</span>`;
  const realCalls = calls.filter((c) => !c.simulated), realTg = tg.filter((m) => m.mode === 'real');
  const missing = (s?.ivr.missing || []).map((m) => SETUP_HINT[m] || m);
  pane.innerHTML = html`${raw(!confirmed ? '<div class="banner2 amber">Confirm the fire on the Overview tab before alerting anyone.</div>' : !canD ? '<div class="banner2 amber">Only an Officer or Admin can send alerts. Change the role in the full console.</div>' : '')}
    <div class="sect"><h4>✈ Telegram</h4><div class="sub">${s?.telegram.ready_to_send ? 'Goes to your response chat with three map images and a PDF report.' : 'Not set up yet: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env.'}</div>
      <button class="btn primary" id="tgreal" style="width:100%;margin:8px 0" ${dis} ${s?.telegram.ready_to_send ? '' : 'disabled'}>Send to Telegram</button>
      ${raw(realTg.length ? `<table class="t">${realTg.map((m) => `<tr><td>${esc(m.kind === 'message' ? 'Message' : m.kind === 'report' ? 'PDF report' : m.kind + ' map')}</td><td>${chip(m.status)}</td><td class="sub">${m.error ? esc(m.error) : ''}</td></tr>`).join('')}</table>` : '')}
      ${raw(i.notify?.acknowledged ? `<div class="banner2 green" style="margin-top:6px">Acknowledged by ${esc(i.notify.acknowledged.by)}.</div>` : '')}</div>
    <div class="sect"><h4>📞 Phone call</h4><div class="sub">Calls <b>${esc(s?.ivr.response_contact || 'the response contact')}</b> (${esc(s?.ivr.response_number || 'number not set')}) and reads out the location, size, spread, nearest station and distance.</div>
      <details style="margin:6px 0"><summary>What the call says</summary><div id="script" class="script">Loading…</div></details>
      <div class="row2" style="margin:8px 0"><button class="btn" id="hear">▶ Hear it</button><button class="btn warn grow" id="callnow" ${dis} ${s?.ivr.real_ready ? '' : 'disabled'}>Call now</button></div>
      ${raw(s?.ivr.real_ready ? '' : `<div class="banner2 amber">Phone calls are not set up yet. Add to .env: ${esc(missing.join(', ') || 'Twilio details')}.${s?.ivr.dimension ? ' ' + esc(s.ivr.dimension) : ''}</div>`)}
      ${raw(realCalls.map((c) => `<div class="callrow">${chip(c.status)} <span class="sub">${c.history.map((h) => esc(h.status.toLowerCase())).join(' → ')}</span></div>`).join(''))}</div>
    <div class="sect"><h4>Record</h4><div id="activity">${raw(activity(i.id))}</div></div>`;
  api(`/fire/incidents/${i.id}/ivr/script`).then((r) => { const el = $('#script'); if (el) el.textContent = r.script; S.script = r.script; }).catch(() => { const el = $('#script'); if (el) el.textContent = 'The message could not be prepared.'; });
  $('#hear').onclick = () => { if (!('speechSynthesis' in window) || !S.script) return toast('This browser cannot play the message', 'error'); if (speechSynthesis.speaking) { speechSynthesis.cancel(); return; } const u = new SpeechSynthesisUtterance(S.script); u.lang = 'en-IN'; u.rate = 0.95; speechSynthesis.speak(u); };
  const approve = async () => { if (i.status === 'CONFIRMED') await api(`/fire/incidents/${i.id}/authorise`, { method: 'POST', body: { note: 'approved from the alert panel' } }); };
  $('#tgreal').onclick = () => sheet('Send to Telegram', `<p>This sends the incident, three map images and a PDF report to <b>${esc(s.telegram.chat_masked)}</b>.</p><p class="sub">It will not be sent twice unless you choose to.</p>`, [{ label: 'Send now', cls: 'primary', run: async () => {
    await approve();
    const card = pop({ icon: '✈', title: 'Sending to Telegram', steps: ['Preparing map images and report', 'Sending to Telegram', 'Telegram accepted it'] });
    S.watch = { id: i.id, card };
    try {
      const j = await api(`/fire/incidents/${i.id}/notify/telegram`, { method: 'POST', body: { mode: 'real', confirm_real: true } });
      waitJob(j.job_id, null, 1500).then(async () => {
        const f = await reload(i.id); const last = (f.notify?.telegram || []).filter((m) => m.mode === 'real' && m.kind === 'message').pop();
        if (last?.status === 'SENT') card.done('Delivered to Telegram with the map images and report.'); else card.fail(last?.error || 'Telegram did not accept the message.');
      }).catch((e) => card.fail(e.message));
    } catch (e) { card.fail(e.message); throw e; }
  } }]);
  $('#callnow').onclick = () => sheet('Call now', `<p>This phones <b>${esc(s.ivr.response_contact)}</b> (${esc(s.ivr.response_number)}) and reads out the message above.</p>`, [{ label: 'Place call', cls: 'primary', run: async () => {
    await approve();
    const card = pop({ icon: '📞', title: `Calling ${s.ivr.response_contact}`, steps: ['Placing the call', 'Ringing', 'Answered', 'Call finished'] });
    try { await api(`/fire/incidents/${i.id}/ivr`, { method: 'POST', body: { mode: 'real', confirm_real: true } }); trackCalls(i.id, card); }
    catch (e) { card.fail(e.message); throw e; }
  } }]);
}
const CALL_STEP = { REQUESTED: 0, QUEUED: 0, INITIATED: 0, RINGING: 1, ANSWERED: 2 };
function trackCalls(id, card) {
  clearInterval(S.timers.calls);
  let n = 0;
  S.timers.calls = setInterval(async () => {
    n++;
    const i = await reload(id).catch(() => null); const c = (i?.ivr?.calls || []).filter((x) => !x.simulated).pop();
    if (c && c.status in CALL_STEP) card?.at(CALL_STEP[c.status]);
    if (c?.status === 'COMPLETED') { card?.done('The call finished.'); clearInterval(S.timers.calls); }
    else if (c && ['FAILED', 'BUSY', 'NO_ANSWER', 'CANCELLED'].includes(c.status)) { card?.fail({ BUSY: 'The line was busy.', NO_ANSWER: 'Nobody answered.' }[c.status] || 'The call did not go through.'); clearInterval(S.timers.calls); }
    else if (n > 60) { card?.fail('No final status from the phone provider yet.'); clearInterval(S.timers.calls); }
  }, 2000);
}

// ------------------------------------------------------------------ helpers
async function act(fn, msg) { try { await fn(); toast(msg); await reload(); } catch (e) { toast(e.message, 'error'); } }
async function job(path, msg, body, after) {
  const btns = $$('#pane button'); btns.forEach((b) => (b.disabled = true));
  try {
    const j = await api(path, { method: 'POST', body: body === undefined ? undefined : body });
    toast('Working…'); const res = await waitJob(j.job_id, null, 1500);
    toast(msg); await reload(); await after?.(res);
  } catch (e) { toast(e.message, 'error'); await reload().catch(() => {}); } finally { btns.forEach((b) => (b.disabled = false)); }
}
function sheet(title, body, actions) {
  $('#sheet').innerHTML = `<h2>${esc(title)}</h2>${body}<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button class="btn" id="sx">Cancel</button>${actions.map((a, k) => `<button class="btn ${a.cls}" data-a="${k}">${esc(a.label)}</button>`).join('')}</div><div class="note warn" id="serr2" hidden></div>`;
  $('#modal').hidden = false; $('#sx').onclick = () => ($('#modal').hidden = true);
  $$('#sheet [data-a]').forEach((b) => (b.onclick = async () => { b.disabled = true; try { await actions[+b.dataset.a].run(); $('#modal').hidden = true; } catch (e) { $('#serr2').hidden = false; $('#serr2').textContent = e.message; b.disabled = false; } }));
}

// ------------------------------------------------------------------ live activity (shown inside the incident, where it happens)
async function pollEvents() {
  try {
    const r = await api(`/fire/events?since_id=${S.lastId}&limit=300`);
    if (r.events.length) {
      S.events.push(...r.events); S.events = S.events.slice(-800); S.lastId = r.last_id;
      if (S.watch && !S.watch.card.over) for (const e of r.events) if (e.incident_id === S.watch.id) { if (e.event_type === 'attachment_built') S.watch.card.at(0); if (e.event_type === 'telegram_request') S.watch.card.at(1); }
    }
    if (r.events.some((e) => e.incident_id === S.sel) && S.sel) {
      const i = await api('/fire/incidents/' + S.sel).catch(() => null);
      if (i) { const k = S.list.findIndex((x) => x.id === i.id); if (k >= 0 && JSON.stringify(S.list[k]) !== JSON.stringify(i)) { S.list[k] = i; if (!$('#modal') || $('#modal').hidden) paint(); drawPins(); } }
      const box = $('#activity'); if (box) box.innerHTML = activity(S.sel);
    }
  } catch { /* best effort */ }
}
function activity(id) {
  return historyHtml(S.events.filter((e) => e.incident_id === id), 'Nothing has happened yet.');
}

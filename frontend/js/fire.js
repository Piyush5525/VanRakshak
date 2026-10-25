// Fire response workspace for the live map: one incident record drives forecast, impact, route, Telegram, IVR and the event log.
import { api, esc, html, raw, toast, waitJob } from './core.js';
import { pop, historyHtml } from './progress.js';
import { followCall } from './escalate.js';
import { drawRoute, journeyHtml, journeyText, legendHtml, walkLink, ICON as WAY } from './routes.js';

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
  $('#rightBody').hidden = t !== 'inc'; $('#fireBody').hidden = t !== 'fire'; $('#forestBody').hidden = t !== 'tree'; if ($('#infraBody')) $('#infraBody').hidden = t !== 'infra';
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
    drawRoute(S.layer, r);
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
  const live = list.filter((i) => !i.detection?.archive && !i.is_demo), past = list.filter((i) => i.detection?.archive), scn = list.filter((i) => i.is_demo);
  const when = (i) => (i.detection?.archive ? new Date(i.observed_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : `${age(i.created_at)} ago`);
  const row = (i) => `<button class="row-item" data-id="${esc(i.id)}"><span style="font-size:20px">🔥</span><span class="grow"><b>${esc(i.detection?.place?.name || i.code)}</b><small>${esc(i.code)} · ${esc(when(i))}${i.is_demo ? ' · <span class="tag">Scenario</span>' : ''}</small></span><span class="chip ${STATUS_CHIP[i.status] || ''}">${esc(i.status_label)}</span></button>`;
  box.innerHTML = html`<div class="side-head" style="padding-left:0"><div><h2>Fire incidents</h2><div class="sub">Choose one to see it on the map</div></div></div>
    <h3 class="grp">Happening now</h3>
    ${raw(live.length ? `<div class="list">${live.map(row).join('')}</div>` : '<div class="calm">🌿 <b>No forest fire is being recorded right now.</b><span>Turn on NASA fire hotspots on the left, click a hotspot and press Investigate to record one.</span></div>')}
    <h3 class="grp">Past fires <span class="gnote">examples from NASA's archive</span></h3>
    ${raw(past.length ? `<div class="list">${past.map(row).join('')}</div>` : '<div class="sub">None added yet. These are real fires that already happened, to look at between live fires.</div>')}
    <div class="row2" style="margin-top:8px">${raw(past.length ? '<button class="btn sm" id="rmPast">− Remove past fires</button>' : '<button class="btn sm" id="loadPast">🕘 Add past fires</button>')}</div>
    ${raw(scn.length ? `<h3 class="grp">Scenarios</h3><div class="list">${scn.map(row).join('')}</div>` : '')}
    ${raw(S.status?.examples_enabled ? `<div class="row2" style="margin-top:10px"><button class="btn sm" id="loadScn">＋ Add example incidents</button>${scn.length ? '<button class="btn sm" id="rmScn">− Remove examples</button>' : ''}</div>` : '')}
    `;
  $('#loadPast')?.addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'Fetching from NASA…';
    try { const r = await api('/fire/past/load', { method: 'POST' }); await refreshAll(); toast(r.created ? `${r.created} past fires added. Routes are being prepared.` : 'The past fires are already here.'); if (r.job_id) waitJob(r.job_id, null, 3000, 30 * 60 * 1000).then(refreshAll).catch(() => {}); }
    catch (err) { toast(err.message, 'error'); e.target.disabled = false; e.target.textContent = '🕘 Add past fires'; }
  });
  $('#rmPast')?.addEventListener('click', async () => { try { const r = await api('/fire/past/remove', { method: 'POST' }); S.sel = null; await refreshAll(); toast(`${r.removed} past fires removed`); } catch (err) { toast(err.message, 'error'); } });
  if ($('#loadScn')) $('#loadScn').onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await api('/fire/scenarios/load', { method: 'POST' }); await refreshAll(); toast(r.created ? `${r.created} incidents added. Forecasts and routes are being prepared.` : 'The example incidents are already here.'); if (r.job_id) waitJob(r.job_id, null, 3000, 30 * 60 * 1000).then(refreshAll).catch(() => {}); }
    catch (err) { toast(err.message, 'error'); } finally { e.target.disabled = false; }
  };
  $('#rmScn')?.addEventListener('click', () => sheet('Remove example incidents', '<p>Removes only the example incidents. Real incidents are not touched.</p>', [{ label: 'Remove', cls: 'warn', run: async () => { await api('/fire/demo/reset', { method: 'POST' }); S.sel = null; await refreshAll(); } }]));
  $$('#fireBody .row-item[data-id]').forEach((b) => (b.onclick = () => select(b.dataset.id)));
}

function paintDetail(box) {
  const i = cur(); if (!i) { S.sel = null; return paintList(box); }
  const tabs = [['overview', 'Overview'], ['forecast', 'Forecast'], ['impact', 'Impact'], ['route', 'Route'], ['comms', 'Messages']];
  box.innerHTML = html`<button class="back" id="fireBack">‹ All fire incidents</button>
    <div class="row-item" style="border:0;padding:0"><span style="font-size:24px">🔥</span><span class="grow"><h2 style="font-size:17px">${i.detection?.place?.name || i.code}</h2><div class="sub">${i.code} · ${i.lat.toFixed(4)}, ${i.lon.toFixed(4)} ${raw(i.is_demo ? '<span class="tag">Scenario</span>' : '')}</div></span><span class="chip ${STATUS_CHIP[i.status] || ''}">${i.status_label}</span></div>
    <div class="row2" style="margin:8px 0"><a class="btn sm primary" id="sim3d" href="/app#/ar-explorer/fire:${encodeURIComponent(i.id)}">🧊 3D simulation · Open in Forest Explorer</a></div>
    <div class="sub-tabs" id="stabs">${raw(tabs.map(([k, n]) => `<button data-k="${k}" class="${S.tab === k ? 'on' : ''}">${n}</button>`).join(''))}</div>
    <div id="pane"></div>`;
  $('#fireBack').onclick = () => { S.sel = null; drawPins(); paint(); };
  $('#stabs').onclick = (e) => { const k = e.target.dataset.k; if (k) { S.tab = k; paintDetail(box); drawPins(); const i2 = cur(); if (k === 'route' && i2.route?.status === 'OK') S.ctx.map.fitBounds(L.geoJSON(i2.route.geometry).getBounds().pad(0.15)); if (k === 'forecast' && i2.forecast?.horizons) { const g = i2.forecast.horizons[S.hz].contours.possible_10; if (g) S.ctx.map.fitBounds(L.geoJSON(g).getBounds().pad(0.2)); } } };
  ({ overview, forecast, impact, route, comms })[S.tab](i, $('#pane'));
}

// ---- overview
function overview(i, pane) {
  const d = i.detection || {}; const cur_ = d.currency;
  const curLab = { current: ['obs', 'CURRENT'], delayed: ['est', 'DELAYED'], historical: ['na', 'HISTORICAL'], demo: ['na', 'SCENARIO'], 'n/a': ['na', 'MANUAL REPORT'] }[cur_] || ['na', cur_ || '?'];
  pane.innerHTML = html`<div class="sect"><h4>${raw(lab(curLab[0], curLab[1]))} Detection</h4><dl class="kv">
      <dt>Source</dt><dd>${d.product_name || d.source || i.source}</dd><dt>Satellite</dt><dd>${d.satellite || '—'}</dd>
      <dt>Detected</dt><dd>${d.observed_at ? new Date(d.observed_at).toLocaleString() : '—'}${d.age_hours_at_creation != null ? ` (${d.age_hours_at_creation} h old when opened)` : ''}</dd>
      <dt>Fire power</dt><dd>${d.frp_mw != null ? d.frp_mw + ' MW' : 'not provided'}</dd>
      </dl>
      <div class="note">${d.note || ''}</div></div>
    ${raw(fireDecision(i))}
    <div class="sect"><h4>Record</h4><div id="activity">${raw(activity(i.id))}</div></div>
    <div class="sect"><h4>Nearby</h4><div id="nearby" class="sub">Loading registered fire stations…</div><button class="btn sm" id="ctxMore" style="margin-top:6px">Load terrain and map features</button><div id="ctxOut"></div></div>
    <div class="sect"><h4>Field updates</h4>${raw((i.field_updates || []).length ? `<ul class="steps" style="padding-left:16px;margin:0;font-size:12.5px">${i.field_updates.map((u) => `<li>${esc(new Date(u.at).toLocaleTimeString())} · ${esc(u.by)}: ${esc(u.text)}</li>`).join('')}</ul>` : '<div class="sub">None.</div>')}</div>
    <div class="row2"><button class="btn sm" id="pdf">⬇ Report (PDF)</button></div>`;
  $('#fEsc')?.addEventListener('click', () => escalateFire(i));
  $('#fFalse')?.addEventListener('click', () => act(() => api(`/fire/incidents/${i.id}/verify`, { method: 'POST', body: { decision: 'reject', note: 'False alarm' } }), 'Marked as a false alarm'));
  $('#fUndo')?.addEventListener('click', () => act(() => api(`/fire/incidents/${i.id}/verify`, { method: 'POST', body: { decision: 'needs_verification', note: 'Reopened' } }), 'Back in review'));
  $('#fResolve')?.addEventListener('click', () => act(() => api(`/fire/incidents/${i.id}/resolve`, { method: 'POST', body: { note: 'Resolved' } }), 'Marked as resolved'));
  $('#pdf').onclick = () => import('./core.js').then((c) => c.download(`/fire/incidents/${i.id}/report.pdf`, `${i.code}-report.pdf`));
  api(`/fire/context?lat=${i.lat}&lon=${i.lon}&parts=stations`).then((c) => { const n = $('#nearby'); if (!n) return; n.innerHTML = c.stations.length ? `${c.stations.map((s) => `<div>🚒 <b>${esc(s.name)}</b> <span class="sub">${s.straight_line_km} km straight line · ${esc(s.source)}${s.verified ? '' : ' · unverified'}</span></div>`).join('')}` : '<div class="sub">No stations registered near here yet. Open the Route tab and press “Find stations”.</div>'; });
  $('#ctxMore').onclick = async () => { $('#ctxOut').innerHTML = '<div class="sub"><span class="spin"></span>Loading…</div>'; const c = await api(`/fire/context?lat=${i.lat}&lon=${i.lon}&parts=terrain,osm`).catch((e) => ({ err: e.message })); const o = c.osm || {}, t = c.terrain || {}; $('#ctxOut').innerHTML = c.err ? `<div class="note warn">${esc(c.err)}</div>` : `<dl class="kv"><dt>Elevation</dt><dd>${t.elevation_m ?? '—'} m ${lab('obs', 'OBSERVED')}</dd><dt>Slope</dt><dd>${t.slope_deg ?? '—'}°</dd><dt>Nearest road</dt><dd>${o.nearest_road ? esc((o.nearest_road.name || o.nearest_road.type) + ' · ' + o.nearest_road.distance_m + ' m') : 'none mapped'}</dd><dt>Nearest settlement</dt><dd>${o.nearest_settlement ? esc((o.nearest_settlement.name || '?') + ' · ' + o.nearest_settlement.distance_m + ' m') : 'none mapped'}</dd></dl><div class="sub">${esc(o.status === 'unavailable' ? 'Map features unavailable: ' + o.reason : 'OpenStreetMap + Copernicus DEM; mapping is incomplete.')}</div>`; };
}

// ---- the two decisions: Escalate or False alarm
function fireDecision(i) {
  const dis = ['admin', 'reviewer', 'officer'].includes(curRole()) ? '' : 'disabled';
  const sent = ['DISPATCH_REQUESTED', 'DISPATCHED', 'MONITORING'].includes(i.status);
  const tg = (i.notify?.telegram || []).filter((m) => m.mode === 'real' && m.kind === 'message').pop(), call = (i.ivr?.calls || []).filter((c) => !c.simulated).pop();
  const say = { SENT: 'sent', FAILED: 'failed', COMPLETED: 'finished', INITIATED: 'placed', RINGING: 'ringing', ANSWERED: 'answered', BUSY: 'busy', NO_ANSWER: 'not answered' };
  let body;
  if (i.status === 'REJECTED' || i.status === 'CANCELLED') body = `<div class="banner2 grey"><b>Marked as a false alarm.</b></div><button class="btn" id="fUndo" ${dis}>↩ Undo, review it again</button>`;
  else if (i.status === 'RESOLVED') body = '<div class="banner2 grey"><b>Resolved.</b></div>';
  else if (sent) body = `<div class="banner2 green"><b>Escalated.</b><div class="sub">${[tg ? `Telegram: ${say[tg.status] || tg.status.toLowerCase()}` : '', call ? `Phone call: ${say[call.status] || call.status.toLowerCase()}${call.duration_s != null ? ` (${call.duration_s} s)` : ''}` : ''].filter(Boolean).join(' · ') || 'Response approved.'}</div></div>
      <div class="row2"><button class="btn grow" id="fEsc" ${dis}>🚨 Send again</button><button class="btn" id="fResolve" ${dis}>✓ Mark resolved</button></div>`;
  else body = `<div class="decide"><button class="btn danger-fill" id="fEsc" ${dis}><span>🚨</span><b>Escalate</b><small>Confirm the fire, tell the station on Telegram and phone the response contact</small></button>
      <button class="btn ok-line" id="fFalse" ${dis}><span>✓</span><b>False alarm</b><small>Close it. You can undo this</small></button></div>`;
  return `<div class="sect"><h4>Your decision</h4>${body}${dis ? `<div class="sub">Your role (${esc(curRole())}) cannot decide. Switch to Reviewer, Officer or Admin in the full console.</div>` : ''}</div>`;
}
async function escalateFire(i0) {
  const btn = $('#fEsc'); const again = btn.textContent.includes('again');
  if (!btn.dataset.armed) {
    btn.dataset.armed = '1'; const old = btn.innerHTML;
    btn.innerHTML = again ? 'Tap again to send' : '<span>🚨</span><b>Tap again to send</b><small>Telegram and a phone call go out now</small>';
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.innerHTML = old; } }, 6000); return;
  }
  delete btn.dataset.armed; btn.disabled = true;
  const id = i0.id;
  const card = pop({ icon: '🚨', title: `Escalating ${i0.code}`, steps: ['Confirming the fire', 'Forecast and route to the nearest station', 'Approving the response', 'Sending to Telegram', 'Phoning the response contact', 'Finished'] });
  const oks = [], fails = [];
  const run = async (path, body) => { const j = await api(path, { method: 'POST', body }); return waitJob(j.job_id, null, 1500); };
  try {
    let i = await api('/fire/incidents/' + id);
    if (i.status === 'DETECTED') i = await api(`/fire/incidents/${id}/verify`, { method: 'POST', body: { decision: 'needs_verification', note: 'Escalated' } });
    if (['NEEDS_VERIFICATION', 'INCONCLUSIVE'].includes(i.status)) i = await api(`/fire/incidents/${id}/verify`, { method: 'POST', body: { decision: 'confirm', note: 'Confirmed by ' + curRole() + ' when escalating' } });
    card.note(0, 'Confirmed as a fire', 'ok'); card.at(1);
    if (!i.forecast || i.forecast.status === 'UNAVAILABLE') { card.note(1, 'Working out the spread…'); await run(`/fire/incidents/${id}/forecast`).catch(() => {}); }
    if (i.route?.status !== 'OK') {
      card.note(1, 'Finding the nearest fire station and the road route…');
      await run(`/fire/incidents/${id}/stations/discover`, {}).catch(() => {}); await run(`/fire/incidents/${id}/route`, {}).catch(() => {});
    }
    i = await api('/fire/incidents/' + id); S.list[S.list.findIndex((x) => x.id === id)] = i;
    card.note(1, i.route?.status === 'OK' ? `${i.route.station.name}: ${journeyText(i.route)}` : 'No route could be found', i.route?.status === 'OK' ? 'ok' : 'bad'); card.at(2);
    if (i.status === 'CONFIRMED') await api(`/fire/incidents/${id}/authorise`, { method: 'POST', body: { note: 'Approved when escalating' } });
    card.note(2, 'Response approved', 'ok'); card.at(3);
    const tgBefore = ((await api('/fire/incidents/' + id)).notify?.telegram || []).length;
    await run(`/fire/incidents/${id}/notify/telegram`, { mode: 'real', confirm_real: true, resend: again });
    i = await api('/fire/incidents/' + id); const tg = (i.notify?.telegram || []).filter((m) => m.mode === 'real' && m.kind === 'message').pop();
    if (tg?.status === 'SENT') { card.note(3, 'Sent to Telegram with the map images and report', 'ok'); oks.push('Telegram'); } else { card.note(3, tg?.error || 'Telegram did not accept it', 'bad'); fails.push('Telegram'); }
    card.at(4);
    const e = await api(`/fire/incidents/${id}/ivr`, { method: 'POST', body: { mode: 'real', confirm_real: true } });
    if (e.provider_call_id && e.status !== 'FAILED') { const f = await followCall(card, 4, e.provider_call_id); (f.ok ? oks : fails).push('Phone call'); }
    else { card.note(4, e.status === 'FAILED' ? 'The call did not go through' : 'Could not place the call', 'bad'); fails.push('Phone call'); }
    await reload(id);
    if (!oks.length) card.fail('Nothing could be sent. See the notes above.'); else card.done(`${oks.join(' and ')} done${fails.length ? `; ${fails.join(' and ')} did not work` : ''}.`);
  } catch (err) { card.fail(err.status === 403 ? 'Your role is not allowed to do this. Use Reviewer, Officer or Admin.' : err.message); await reload(id).catch(() => {}); }
}

// ---- forecast
function forecast(i, pane) {
  const fc = i.forecast;
  const head = `<div class="banner2 red"><b>Predicted spread area</b>, not a fire perimeter.</div>`;
  if (!fc) { pane.innerHTML = head + '<div class="empty"><b>No forecast yet</b>Run the 3, 6 and 12 hour model for this incident.</div><button class="btn primary block" id="runfc">Generate forecast</button>'; $('#runfc').onclick = () => job('/fire/incidents/' + i.id + '/forecast', 'Forecast generated'); return; }
  if (fc.status === 'UNAVAILABLE') { pane.innerHTML = head + `<div class="banner2 amber">Forecast unavailable: ${esc(fc.reason)}</div><button class="btn block" id="runfc">Try again</button>`; $('#runfc').onclick = () => job('/fire/incidents/' + i.id + '/forecast', 'Forecast generated'); return; }
  const v = fc.horizons[S.hz]; const w = fc.inputs.weather;
  pane.innerHTML = html`${raw(head)}${raw(fc.status !== 'OK' ? `<div class="banner2 amber"><b>${esc(fc.status)}</b>: ${esc((fc.degradation_notes || []).join('; '))}</div>` : '')}
    <div class="hz" id="hz">${raw(['3', '6', '12'].map((h) => `<button data-h="${h}" class="${S.hz === h ? 'on' : ''}">${h} h</button>`).join(''))}</div>
    <label class="switch"><input type="checkbox" id="allh" ${S.all ? 'checked' : ''}><span>Compare all horizons</span></label>
    <div class="sect"><h4>${raw(lab('est', 'ESTIMATED'))} ${S.hz}-hour potential exposure</h4><dl class="kv"><dt>Median area</dt><dd><b>${v.area_ha.p50} ha</b> <span class="sub">(P10–P90 ${v.area_ha.p10}–${v.area_ha.p90})</span></dd><dt>Likely (≥50% of runs)</dt><dd>${v.contour_area_ha.likely_50} ha</dd><dt>Possible (≥10%)</dt><dd>${v.contour_area_ha.possible_10} ha</dd><dt>Head spread</dt><dd>${v.head_distance_km.p50} km (P10–P90 ${v.head_distance_km.p10}–${v.head_distance_km.p90})</dd><dt>Direction</dt><dd>towards ${v.spread_toward_deg}° (wind from ${v.wind_from_deg}°)</dd></dl>
      <div class="legend"><span><i style="background:#d9382a"></i>likely ≥50%</span><span><i style="background:#e3a14b"></i>possible ≥10%</span></div></div>
    <div class="sect"><h4>Inputs ${raw(lab(w.illustrative || fc.demo ? 'est' : 'mod', w.illustrative ? 'SCENARIO WEATHER' : 'MODELLED'))}</h4><dl class="kv"><dt>Wind</dt><dd>${v.wind_kmh} km/h</dd><dt>Temp / RH</dt><dd>${v.temp_c} °C / ${v.rh_percent}%</dd><dt>Drought factor</dt><dd>${v.drought_factor} / 10</dd><dt>Slope along spread</dt><dd>${v.slope_deg_along_spread}°</dd><dt>Weather source</dt><dd>${w.source}</dd><dt>Generated</dt><dd>${new Date(fc.generated_at).toLocaleString()}</dd></dl></div>
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
  pane.innerHTML = html`<div class="sect"><h4>${raw(lab('est', 'ESTIMATED'))} Potential exposure area</h4><div class="sub">Land the fire could reach, not burned area.</div>${raw(pe.by_horizon_h ? `<table class="t"><tr><th>Horizon</th><th>Median</th><th>P10–P90</th></tr>${Object.entries(pe.by_horizon_h).map(([h, v]) => `<tr><td>${esc(h)} h</td><td><b>${esc(v.area_ha_p50)} ha</b></td><td>${esc(v.area_ha_p10_p90.join('–'))}</td></tr>`).join('')}</table>` : '')}</div>
    <div class="sect"><h4>${raw(lab('mod', 'MODELLED'))} Air quality</h4>${raw(aq.status === 'ok' ? `<table class="t"><tr><th></th><th>Now</th><th>Max 24 h</th></tr><tr><td>PM2.5</td><td><b>${esc(aq.pm2_5_ug_m3.now)}</b> µg/m³</td><td>${esc(aq.pm2_5_ug_m3.max_24h)}</td></tr><tr><td>PM10</td><td><b>${esc(aq.pm10_ug_m3.now)}</b> µg/m³</td><td>${esc(aq.pm10_ug_m3.max_24h)}</td></tr></table>${(aq.potentially_downwind_settlements || []).length ? `<div style="margin-top:6px"><b>Potentially downwind (within 25 km):</b> ${aq.potentially_downwind_settlements.map((p) => esc(p.name + ' ' + p.distance_km + ' km')).join(', ')}</div>` : ''}` : `<div class="banner2 amber">Air quality unavailable: ${esc(aq.reason)}</div>`)}</div>
    <div class="sect"><h4>Places near the incident</h4>${raw(n.status === 'ok' ? `<table class="t">${n.settlements.slice(0, 8).map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.type)}</td><td>${esc(p.distance_km)} km</td><td>${p.inside_possible_footprint ? '<span class="chip red">inside footprint</span>' : p.downwind ? '<span class="chip amber">downwind</span>' : ''}</td></tr>`).join('')}</table>` : `<div class="banner2 amber">Unavailable: ${esc(n.reason)}</div>`)}</div>
    <div class="sect"><h4>${raw(lab(c.evidence_class === 'illustrative' ? 'est' : c.evidence_class === 'modelled' ? 'mod' : 'na', (c.evidence_class || 'n/a').toUpperCase()))} Carbon (CO₂)</h4>${raw(c.status === 'CALCULATED' ? `<div><b>${esc(c.tonnes_co2.central)} t CO₂</b> <span class="sub">(low ${esc(c.tonnes_co2.low)} – high ${esc(c.tonnes_co2.high)})</span></div><div class="sub">${esc(c.method)}; area ${esc(c.inputs.area_ha)} ha, fuel ${esc(c.inputs.fuel_load_t_dm_per_ha)} t/ha. Area basis: ${esc(c.inputs.area_basis)}.</div>` : `<div class="banner2 amber">${esc(c.reason)}</div>`)}
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
  pane.innerHTML = html`<div class="sect"><h4>${r.station.kind === 'police_station' ? '👮' : '🚒'} ${r.station.name}</h4><div class="sub">${r.station.data_note} · ${r.station.source}</div>${raw(journeyHtml(r))}<div class="sub">Total ${r.distance_km} km, about ${Math.round(r.duration_min)} min</div>${raw(legendHtml)}
      <div class="sub">Objective: ${r.objective}. Provider: ${r.provider}. Calculated ${new Date(r.calculated_at).toLocaleTimeString()}.</div>${raw(r.selected_manually ? '<div class="sub">Station chosen manually.</div>' : '')}
      <div class="row2" style="margin-top:6px"><a class="btn sm" href="${esc(r.navigation_url)}" target="_blank" rel="noopener">${WAY.drive} Directions by road</a>${raw(r.walk_url ? `<a class="btn sm" href="${esc(r.walk_url)}" target="_blank" rel="noopener">${WAY.walk} Walking directions</a>` : '')}</div></div>
    <div class="sect"><h4>Candidates compared</h4><table class="t"><tr><th>Station</th><th>Straight</th><th>Road</th><th></th></tr>${raw(r.candidates.map((c) => `<tr><td>${esc(c.icon || '🚒')} ${esc(c.station_name)}</td><td>${esc(c.straight_line_km)} km</td><td>${c.distance_km != null ? esc(c.distance_km + ' km · ' + Math.round(c.duration_min) + ' min') + (c.walk_km ? ' ' + WAY.walk : '') : '<span class="chip red">' + esc(c.status) + '</span>'}</td><td>${c.selected ? '✓' : c.distance_km != null ? `<button class="btn sm" data-st="${esc(c.station_id)}">Use</button>` : ''}</td></tr>`).join(''))}</table></div>
    ${raw((r.reasons || []).length ? r.reasons.map((x) => `<div class="banner2 amber">${esc(x.text)}</div>`).join('') : '')}
    <div class="sub">Typical driving time, not live traffic.</div>${find}`;
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
      <details style="margin:6px 0"><summary>What the call says</summary><div id="fireScript" class="script">Loading…</div></details>
      <div class="row2" style="margin:8px 0"><button class="btn" id="fireHear">▶ Hear it</button><button class="btn warn grow" id="callnow" ${dis} ${s?.ivr.real_ready ? '' : 'disabled'}>Call now</button></div>
      ${raw(s?.ivr.real_ready ? '' : `<div class="banner2 amber">Phone calls are not set up yet. Add to .env: ${esc(missing.join(', ') || 'Twilio details')}.${s?.ivr.dimension ? ' ' + esc(s.ivr.dimension) : ''}</div>`)}
      ${raw(realCalls.map((c) => `<div class="callrow">${chip(c.status)} <span class="sub">${c.history.map((h) => esc(h.status.toLowerCase())).join(' → ')}</span></div>`).join(''))}</div>
    <div class="sect"><h4>Record</h4><div id="activity">${raw(activity(i.id))}</div></div>`;
  api(`/fire/incidents/${i.id}/ivr/script`).then((r) => { const el = $('#fireScript'); if (el) el.textContent = r.script; S.script = r.script; }).catch(() => { const el = $('#fireScript'); if (el) el.textContent = 'The message could not be prepared.'; });
  $('#fireHear').onclick = () => { if (!('speechSynthesis' in window) || !S.script) return toast('This browser cannot play the message', 'error'); if (speechSynthesis.speaking) { speechSynthesis.cancel(); return; } const u = new SpeechSynthesisUtterance(S.script); u.lang = 'en-IN'; u.rate = 0.95; speechSynthesis.speak(u); };
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
    const card = pop({ icon: '📞', title: `Calling ${s.ivr.response_contact}`, steps: s.ivr.provider === 'omnidimension' ? ['Placing the call', 'Call connected', 'Call finished'] : ['Placing the call', 'Ringing', 'Answered', 'Call finished'] });
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
    if (c && c.status in CALL_STEP) card?.at(c.provider === 'omnidimension' && c.status === 'ANSWERED' ? 1 : CALL_STEP[c.status]);
    if (c?.status === 'COMPLETED') { card?.done(c.duration_s != null ? `The call finished. It lasted ${c.duration_s} seconds.` : 'The call finished.'); clearInterval(S.timers.calls); }
    else if (c && ['FAILED', 'BUSY', 'NO_ANSWER', 'CANCELLED'].includes(c.status)) { card?.fail({ BUSY: 'The line was busy.', NO_ANSWER: 'Nobody answered.' }[c.status] || 'The call did not go through.'); clearInterval(S.timers.calls); }
    else if (n > 120) { card?.fail('No final status from the phone provider yet.'); clearInterval(S.timers.calls); }
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

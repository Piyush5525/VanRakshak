import { api, html, raw, esc, fmt, tag, empty, toast, dialog, can, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

export const REC = { recommended: ['Recommended', 'ready'], recommended_with_caution: ['Recommended with caution', 'configure'], alternative: ['Alternative', 'neutral'], not_recommended_blocked: ['Not recommended: blocked', 'red'] };

export async function render(root, { params }) {
  let [incidents, stations, hazards] = await Promise.all([api('/incidents'), api('/stations').catch(() => []), api('/navigation/hazards')]);
  let incident = incidents.find((i) => i.id === params[0]) || incidents.find((i) => ['APPROVED_FOR_INSPECTION', 'ASSIGNED', 'DISPATCHED', 'ACKNOWLEDGED'].includes(i.status)) || incidents[0];
  let origin = null, plan = null, selIdx = null, picking = false;
  root.innerHTML = html`
    <div class="grid split">
      <div class="panel"><div class="row"><div class="grow"><h3>Route planning</h3><p class="sub">Roads come from Mapbox (fallback: Google Maps via SerpAPI). Recorded hazards and rainfall advisories are overlaid and every penalty is explained. Forest tracks may be missing from road data.</p></div></div>
        <div id="map" class="map-box tall"></div>${raw(legend([['#1d6b4b', 'Recommended route'], ['#6b7c74', 'Alternative', true], ['#b8321f', 'Blocked by recorded hazard', true], ['#b04bd6', 'Hazard'], ['#37474f', 'Station']]))}</div>
      <div class="stack">
        <div class="panel"><h4>1 · Incident and starting point</h4><div class="stack">
          <label class="f">Incident<select id="inc">${raw(incidents.map((i) => `<option value="${esc(i.id)}" ${i.id === incident?.id ? 'selected' : ''}>${esc(i.title.slice(0, 60))} · ${esc(fmt.status(i.status))}</option>`).join(''))}</select></label>
          <div class="grid g2"><label class="f">Start latitude<input type="number" step="any" id="olat"></label><label class="f">Start longitude<input type="number" step="any" id="olon"></label></div>
          <div class="row"><button class="button subtle sm" id="geo">Use my location</button><button class="button subtle sm" id="pick">Pick on map</button>${stations.length ? raw('<select id="stSel"><option value="">Or choose a station…</option>' + stations.map((s) => `<option value="${esc(s.location.coordinates.join(','))}">${esc(s.name)}</option>`).join('') + '</select>') : ''}</div>
          <div class="help" id="oHelp">${stations.length ? 'Leave blank to start from the nearest registered station.' : 'No stations are registered; enter a starting point.'}</div>
          <button class="button primary" id="plan" ${incident ? '' : 'disabled'}>Calculate routes</button></div></div>
        <div class="panel"><h4>2 · Route options</h4><div id="routes">${raw(empty('No plan yet', 'Choose an incident and a starting point.', '➤'))}</div></div>
        <div class="panel"><div class="row"><div class="grow"><h4>Hazards</h4><p class="sub">Reported closures, floods and access restrictions. Unverified reports only penalise a route; verified, recent closures block it.</p></div>${can('admin', 'reviewer', 'officer') ? raw('<button class="button subtle sm" id="addHz">Report hazard</button>') : ''}</div><div id="hz"></div></div>
        <div class="panel"><div class="row"><div class="grow"><h4>Stations</h4></div>${can('admin') ? raw('<button class="button subtle sm" id="addSt">Add station</button>') : ''}</div><div id="st"></div></div>
      </div>
    </div>`;
  const { map, group } = await createMap($('#map'));
  const gStations = group('Stations'), gHaz = group('Hazards'), gRoute = group('Routes'), gInc = group('Incident');

  function paintBase() {
    gStations.clearLayers(); gHaz.clearLayers(); gInc.clearLayers();
    stations.forEach((s) => addGeo(gStations, s.location, { color: '#37474f', fillColor: '#37474f', fillOpacity: 0.9, weight: 1 }, popup(s.name, [s.jurisdiction, s.availability_note || 'Availability not known']), { radius: 8 }));
    hazards.forEach((h) => addGeo(gHaz, h.geometry, STYLE.hazard, popup(`${fmt.status(h.kind)} · ${h.verified ? 'verified' : 'unverified'}`, [h.source, `${h.age_hours} h ago${h.stale ? ' (may be stale)' : ''}`, h.note || ''].filter(Boolean))));
    if (incident) addGeo(gInc, incident.geometry, STYLE.incident, popup(incident.title, [fmt.status(incident.status)]));
    $('#hz').innerHTML = hazards.length ? html`<table class="t"><tbody>${hazards.map((h) => raw(`<tr><td>${tag(fmt.status(h.kind), h.verified ? 'red' : 'configure').s}<div class="help">${h.verified ? 'verified' : 'unverified'} · ${esc(h.source)} · ${esc(h.age_hours)} h ago${h.stale ? ' · stale' : ''}</div></td><td class="row">${can('admin', 'reviewer') && !h.verified ? `<button class="button subtle sm" data-v="${esc(h.id)}">Verify</button>` : ''}${can('admin', 'reviewer', 'officer') ? `<button class="button subtle sm" data-x="${esc(h.id)}">Resolve</button>` : ''}</td></tr>`))}</tbody></table>` : empty('No active hazards', 'None recorded. That does not mean routes are clear.', '◌');
    $$('#hz [data-v]').forEach((b) => (b.onclick = async () => { await api(`/navigation/hazards/${b.dataset.v}/verify`, { method: 'POST' }); reload(); }));
    $$('#hz [data-x]').forEach((b) => (b.onclick = async () => { await api(`/navigation/hazards/${b.dataset.x}/resolve`, { method: 'POST' }); reload(); }));
    $('#st').innerHTML = stations.length ? html`<table class="t"><tbody>${stations.map((s) => raw(`<tr><td><b>${esc(s.name)}</b><div class="help">${esc(s.jurisdiction)} · ${esc(s.availability_note || 'availability unknown')}</div></td></tr>`))}</tbody></table>` : empty('No stations registered', 'Stations must be entered by an administrator from an authoritative source; none are assumed.', '⌂');
  }
  async function reload() { [stations, hazards] = await Promise.all([api('/stations').catch(() => []), api('/navigation/hazards')]); paintBase(); }

  const setOrigin = (lon, lat, label) => { origin = [lon, lat]; $('#olat').value = lat.toFixed(6); $('#olon').value = lon.toFixed(6); $('#oHelp').textContent = label; };
  $('#geo').onclick = () => navigator.geolocation ? navigator.geolocation.getCurrentPosition((p) => setOrigin(p.coords.longitude, p.coords.latitude, `Device location, accuracy ±${Math.round(p.coords.accuracy)} m`), (e) => toast('Location unavailable: ' + e.message + '. Enter a point or pick on the map.', 'error'), { enableHighAccuracy: true, timeout: 15000 }) : toast('This browser has no geolocation', 'error');
  $('#pick').onclick = () => { picking = true; toast('Click the map to set the start point'); };
  map.on('click', (e) => { if (picking) { picking = false; setOrigin(e.latlng.lng, e.latlng.lat, 'Picked on map'); } });
  $('#stSel')?.addEventListener('change', (e) => { if (e.target.value) { const [lo, la] = e.target.value.split(',').map(Number); setOrigin(lo, la, 'Registered station'); } });
  ['olat', 'olon'].forEach((id) => ($('#' + id).oninput = () => { const la = parseFloat($('#olat').value), lo = parseFloat($('#olon').value); origin = Number.isFinite(la) && Number.isFinite(lo) ? [lo, la] : null; }));
  $('#inc').onchange = (e) => { incident = incidents.find((i) => i.id === e.target.value); plan = null; gRoute.clearLayers(); $('#routes').innerHTML = empty('No plan yet', 'Press Calculate routes.', '➤'); paintBase(); };

  $('#plan').onclick = async () => {
    const b = $('#plan'); b.disabled = true; b.textContent = 'Calculating…';
    try {
      plan = await api('/navigation/routes', { method: 'POST', body: { incident_id: incident.id, origin: origin ? { type: 'Point', coordinates: origin } : null } });
      paintPlan();
    } catch (e) { $('#routes').innerHTML = html`<div class="error-box"><strong>No route</strong><span>${e.message}</span></div>`; } finally { b.disabled = false; b.textContent = 'Calculate routes'; }
  };

  function paintPlan() {
    gRoute.clearLayers();
    if (plan.status !== 'ok') { $('#routes').innerHTML = html`<div class="warn-box"><b>Routing ${plan.status}.</b> ${plan.reason}${plan.details ? raw('<br>' + esc(plan.details.join(' · '))) : ''}<br><span class="help">Use manual route review; no route is implied.</span></div>`; return; }
    const ev = plan.evaluation; selIdx = ev.recommended_index;
    const layers = [];
    ev.routes.forEach((r) => { if (r.geometry) { const st = r.index === ev.recommended_index ? STYLE.route : r.status === 'blocked' ? STYLE.routeBlocked : STYLE.routeAlt; layers.push(L.geoJSON(r.geometry, { style: st }).addTo(gRoute)); } });
    ev.routes.forEach((r) => (r.segments || []).filter((sg) => sg.mode === 'walk' && sg.geometry).forEach((sg) => L.geoJSON(sg.geometry, { style: { color: '#e8590c', weight: 5, dashArray: '2 9' } }).bindTooltip('🚶 On foot: ' + sg.distance_km + ' km').addTo(gRoute)));
    addGeo(gRoute, plan.origin, { color: '#1d6b4b', fillColor: '#fff', fillOpacity: 1, weight: 3 }, popup('Start', [plan.origin_source]), { radius: 8 });
    fit(map, layers.length ? layers : [L.geoJSON(plan.destination)], { padding: [40, 40], maxZoom: 15 });
    $('#routes').innerHTML = html`<div class="help" style="margin-bottom:8px">Provider: ${plan.provider} · start: ${plan.origin_source} · planned ${fmt.dt(plan.planned_at)}${plan.fallback_note ? ' · ' + plan.fallback_note : ''}</div>
      ${raw(ev.message ? `<div class="error-box" style="margin-bottom:8px"><strong>${esc(ev.message)}</strong></div>` : '')}
      ${ev.routes.map((r) => raw(`<div class="route-card ${r.status === 'blocked' ? 'blocked' : ''} ${r.index === selIdx ? 'sel' : ''}" data-i="${r.index}"><div class="row"><b>Route ${r.index + 1}</b>${tag(...REC[r.recommendation]).s}<span class="grow"></span><b>${esc(fmt.km(r.distance_km))}</b>${r.walk_km ? `<span title="On foot">🚗 ${esc(fmt.km(r.drive_km))} + 🚶 ${esc(fmt.km(r.walk_km))}</span>` : ''}<span>${esc(r.adjusted_duration_min ? fmt.min(r.adjusted_duration_min) : fmt.min(r.duration_min))}</span></div>${r.via ? `<div class="help">via ${esc(r.via)}</div>` : ''}${r.status === 'penalised' && r.adjusted_duration_min !== r.duration_min ? `<div class="help">${esc(fmt.min(r.duration_min))} drive + hazard penalty</div>` : ''}${r.reasons.map((x) => `<div class="reason ${x.code === 'weather_advisory' ? 'adv' : ''}">${esc(x.text)}</div>`).join('')}${r.steps?.length && r.index === selIdx ? `<details><summary class="help">Turn-by-turn (${r.steps.length})</summary><ol class="steps">${r.steps.map((s) => `<li>${esc(s.instruction)} <span class="help">${esc(s.distance_m)} m</span></li>`).join('')}</ol></details>` : ''}</div>`))}
      <div class="note-box" style="margin-top:8px">${ev.disclaimer} ${plan.warning ? '<br><b>' + esc(plan.warning) + '</b>' : ''}</div>
      <div class="row" style="margin-top:8px">${['APPROVED_FOR_INSPECTION', 'ASSIGNED', 'DISPATCHED', 'ACKNOWLEDGED', 'FIELD_VERIFICATION_PENDING'].includes(incident.status) && can('admin', 'officer') && ev.recommended_index != null ? raw(`<a class="button primary" href="#/ar-navigation/${esc(incident.id)}">Start guided navigation →</a>`) : raw('<span class="help">Guided navigation needs a reviewed incident, an officer role and at least one unblocked route.</span>')}</div>`;
    $$('.route-card').forEach((c) => (c.onclick = () => { selIdx = +c.dataset.i; $$('.route-card').forEach((x) => x.classList.toggle('sel', x === c)); }));
  }

  $('#addHz')?.addEventListener('click', () => dialog('Report a hazard', `<div class="help">Click the map after pressing Save to place a point hazard; for area or line hazards, use the GeoJSON box.</div>
    <label class="f">Type<select name="kind"><option value="flood">Flooded road</option><option value="waterlogging">Waterlogging</option><option value="closure">Road closure</option><option value="washout">Washout / landslide</option><option value="restricted_access">Restricted access</option><option value="other">Other</option></select></label>
    <label class="f">Reported by / source<input type="text" name="source" required minlength="2" placeholder="e.g. Range officer, phone call 14:20"></label>
    <label class="f">Valid for (hours)<input type="number" name="h" value="48" min="1" max="2160"></label><label class="f">Note<input type="text" name="note"></label>
    <label class="f">Geometry (GeoJSON, optional — leave blank to click the map)<textarea name="geom" rows="3"></textarea></label>`, async (f) => {
    const kindV = f.get('kind'), src = f.get('source'), h = +f.get('h'), note = f.get('note') || null;
    const post = (geometry) => api('/navigation/hazards', { method: 'POST', body: { kind: kindV, geometry, source: src, expires_in_hours: h, note } }).then(() => { toast('Hazard recorded (unverified until a reviewer verifies it)'); reload(); });
    if (f.get('geom')) return post(JSON.parse(f.get('geom')));
    toast('Click the map to place the hazard');
    map.once('click', (e) => post({ type: 'Point', coordinates: [e.latlng.lng, e.latlng.lat] }).catch((er) => toast(er.message, 'error')));
  }, 'Save'));
  $('#addSt')?.addEventListener('click', () => dialog('Register a ranger station', `<div class="help">Only enter stations you can source from the forest department. No contact details are shown back in the UI.</div><label class="f">Name<input type="text" name="name" required></label><label class="f">Jurisdiction<input type="text" name="j" required></label><div class="grid g2"><label class="f">Latitude<input type="number" step="any" name="lat" required></label><label class="f">Longitude<input type="number" step="any" name="lon" required></label></div><label class="f">Telegram chat id (optional)<input type="text" name="tg"></label><label class="f">Email (optional)<input type="text" name="em"></label><label class="f">Availability note<input type="text" name="av" placeholder="e.g. staffed 06:00–18:00 (unverified)"></label>`, async (f) => {
    const contacts = {}; if (f.get('tg')) contacts.telegram_chat_id = f.get('tg'); if (f.get('em')) contacts.email = f.get('em');
    await api('/stations', { method: 'POST', body: { name: f.get('name'), jurisdiction: f.get('j'), location: { type: 'Point', coordinates: [+f.get('lon'), +f.get('lat')] }, contacts, availability_note: f.get('av') || null } }); toast('Station added'); reload();
  }));
  paintBase(); fit(map, [L.geoJSON(incident?.geometry || { type: 'Point', coordinates: [79, 22.5] })], { maxZoom: 12 });
  return { destroy: () => map.remove() };
}

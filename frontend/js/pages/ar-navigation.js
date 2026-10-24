// Guided field navigation: GPS + compass (+ camera backdrop when permitted). This is NOT world-anchored AR:
// browsers cannot align arbitrary outdoor routes to the camera reliably, so the arrow is compass-guided and labelled so.
import { api, html, raw, esc, fmt, tag, empty, toast, can, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';
import { deviceReport, deviceHtml } from '../device.js';
import { REC } from './rangers.js';

const REVIEWED = ['APPROVED_FOR_INSPECTION', 'ASSIGNED', 'DISPATCHED', 'ACKNOWLEDGED', 'FIELD_VERIFICATION_PENDING'];
const compass = (d) => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(d / 45) % 8];

export async function render(root, { params }) {
  const incidents = (await api('/incidents')).filter((i) => REVIEWED.includes(i.status));
  const dev = await deviceReport();
  let incident = incidents.find((i) => i.id === params[0]) || incidents[0], pos = null, watchId = null, plan = null, selIdx = null, mapObj = null, session = null;
  if (!incidents.length) { root.innerHTML = html`<div class="panel">${raw(empty('No incident is ready for field response', 'Navigation is only available for incidents that a reviewer has approved. Review a case in Investigations first.', '⌁'))}<div class="row" style="margin-top:12px"><a class="button primary" href="#/investigations">Go to Investigations</a></div></div><div class="panel" style="margin-top:14px"><h4>Device compatibility</h4>${deviceHtml(dev)}</div>`; return; }
  root.innerHTML = html`
    <div class="warn-box"><b>Safety.</b> Stop in a safe place before using the device. Guidance can be wrong: GPS and compass errors are common under canopy, route data may omit forest tracks, and recorded hazards are only those someone has reported. This tool does not replace official procedures or your judgement.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel"><div class="row"><div class="grow"><h3>Route preview</h3></div></div><div id="map" class="map-box tall"></div>${raw(legend([['#1d6b4b', 'Recommended'], ['#6b7c74', 'Alternative', true], ['#b8321f', 'Blocked', true], ['#1976d2', 'You'], ['#bf731f', 'Incident']]))}</div>
      <div class="stack">
        <div class="panel"><h4>1 · Incident</h4><label class="f"><select id="inc">${raw(incidents.map((i) => `<option value="${esc(i.id)}" ${i.id === incident.id ? 'selected' : ''}>${esc(i.title.slice(0, 70))}</option>`).join(''))}</select></label>
          <div class="help" id="incInfo"></div></div>
        <div class="panel"><h4>2 · Your position</h4><div id="gps" class="note-box">Location not requested yet.</div>
          <div class="row" style="margin-top:8px"><button class="button primary sm" id="locate">Use GPS</button><span class="help">or enter manually:</span></div>
          <div class="grid g2" style="margin-top:6px"><label class="f">Latitude<input type="number" step="any" id="mlat"></label><label class="f">Longitude<input type="number" step="any" id="mlon"></label></div><button class="button subtle sm" id="mset" style="margin-top:6px">Use these coordinates</button></div>
        <div class="panel"><h4>3 · Routes</h4><div class="row"><button class="button primary" id="plan" disabled>Calculate routes</button></div><div id="routes" style="margin-top:8px"></div></div>
        <div class="panel"><h4>4 · Start</h4><label class="row" style="align-items:flex-start"><input type="checkbox" id="consent" style="margin-top:3px"><span class="help">I consent to my device location being sent to this server while navigating. Only my latest position and summary figures are kept; no track is stored, and the last position is deleted when I end the session.</span></label>
          <div class="row" style="margin-top:8px"><button class="button primary" id="start" disabled ${can('admin', 'officer') ? '' : 'title="Officer role required"'}>Start guidance</button></div><div class="help" id="startHelp">${can('admin', 'officer') ? '' : 'Your role cannot start navigation sessions (forest officer or administrator).'}</div></div>
        <div class="panel"><h4>Device compatibility</h4>${deviceHtml(dev)}</div>
      </div>
    </div>`;
  const { map, group } = await createMap($('#map')); mapObj = map;
  const gR = group('Routes'), gM = group('You'), gI = group('Incident'), gH = group('Hazards');
  const showInc = () => { gI.clearLayers(); addGeo(gI, incident.geometry, STYLE.incident, popup(incident.title, [fmt.status(incident.status)])); $('#incInfo').textContent = `Status ${fmt.status(incident.status)} · opened ${fmt.dt(incident.created_at)} · evidence: ${incident.change_id ? 'satellite candidate (unverified)' : incident.wildfire_id ? 'fire observation' : 'manual case'}`; fit(map, [L.geoJSON(incident.geometry)], { maxZoom: 14 }); };
  showInc(); api('/navigation/hazards').then((hz) => hz.forEach((h) => addGeo(gH, h.geometry, STYLE.hazard, popup(`${fmt.status(h.kind)} · ${h.verified ? 'verified' : 'unverified'}`, [h.source, `${h.age_hours} h ago`])))).catch(() => {});

  const setPos = (lon, lat, acc, label) => { pos = { lon, lat, acc, at: Date.now() }; gM.clearLayers(); addGeo(gM, { type: 'Point', coordinates: [lon, lat] }, { color: '#1976d2', fillColor: '#1976d2', fillOpacity: 0.9, weight: 3 }, null, { radius: 8 }); if (acc) L.circle([lat, lon], { radius: acc, weight: 1, color: '#1976d2', fillOpacity: 0.08 }).addTo(gM); $('#gps').innerHTML = html`<b>${label}</b><br>${lat.toFixed(5)}, ${lon.toFixed(5)}${acc != null ? ` · accuracy ±${Math.round(acc)} m` : ''} · updated ${new Date().toLocaleTimeString()}${acc > 50 ? raw('<br><span style="color:#b8321f">Poor accuracy: guidance may be unreliable.</span>') : ''}`; $('#plan').disabled = false; $('#mlat').value = lat.toFixed(6); $('#mlon').value = lon.toFixed(6); };
  $('#locate').onclick = () => {
    if (!navigator.geolocation) return ($('#gps').innerHTML = '<span style="color:#b8321f">This browser has no geolocation. Enter coordinates manually.</span>');
    $('#gps').textContent = 'Requesting location permission…';
    navigator.geolocation.getCurrentPosition((p) => setPos(p.coords.longitude, p.coords.latitude, p.coords.accuracy, 'GPS fix'), (e) => ($('#gps').innerHTML = `<span style="color:#b8321f">${e.code === 1 ? 'Location permission was denied.' : e.code === 3 ? 'No GPS fix yet (timed out).' : 'Location unavailable.'}</span> Enter coordinates manually, or allow location in browser settings.`), { enableHighAccuracy: true, timeout: 20000, maximumAge: 5000 });
  };
  $('#mset').onclick = () => { const la = parseFloat($('#mlat').value), lo = parseFloat($('#mlon').value); if (!Number.isFinite(la) || !Number.isFinite(lo)) return toast('Enter valid coordinates', 'error'); setPos(lo, la, null, 'Manual position'); };
  $('#inc').onchange = (e) => { incident = incidents.find((i) => i.id === e.target.value); plan = null; gR.clearLayers(); $('#routes').innerHTML = ''; $('#start').disabled = true; showInc(); };

  $('#plan').onclick = async () => {
    const b = $('#plan'); b.disabled = true; b.textContent = 'Calculating…';
    try { plan = await api('/navigation/routes', { method: 'POST', body: { incident_id: incident.id, origin: { type: 'Point', coordinates: [pos.lon, pos.lat] } } }); paintPlan(); } catch (e) { $('#routes').innerHTML = html`<div class="error-box"><strong>No route</strong><span>${e.message}</span></div>`; } finally { b.disabled = false; b.textContent = 'Calculate routes'; }
  };
  function paintPlan() {
    gR.clearLayers(); if (plan.status !== 'ok') { $('#routes').innerHTML = html`<div class="warn-box">Routing ${plan.status}: ${plan.reason}. No route is implied; use manual route review.</div>`; $('#start').disabled = true; return; }
    const ev = plan.evaluation; selIdx = ev.recommended_index; const ls = [];
    ev.routes.forEach((r) => r.geometry && ls.push(L.geoJSON(r.geometry, { style: r.index === ev.recommended_index ? STYLE.route : r.status === 'blocked' ? STYLE.routeBlocked : STYLE.routeAlt }).addTo(gR))); fit(map, ls.length ? ls : [], { padding: [40, 40], maxZoom: 15 });
    $('#routes').innerHTML = html`${raw(ev.message ? `<div class="error-box"><strong>${esc(ev.message)}</strong></div>` : '')}${ev.routes.map((r) => raw(`<div class="route-card ${r.status === 'blocked' ? 'blocked' : ''} ${r.index === selIdx ? 'sel' : ''}" data-i="${r.index}"><div class="row"><b>Route ${r.index + 1}</b>${tag(...REC[r.recommendation]).s}<span class="grow"></span><b>${esc(fmt.km(r.distance_km))}</b><span>${esc(fmt.min(r.adjusted_duration_min ?? r.duration_min))}</span></div>${r.reasons.map((x) => `<div class="reason ${x.code === 'weather_advisory' ? 'adv' : ''}">${esc(x.text)}</div>`).join('')}</div>`))}<div class="help">${esc(ev.disclaimer)}</div>`;
    $$('.route-card').forEach((c) => (c.onclick = () => { const i = +c.dataset.i; if (ev.routes[i].status === 'blocked') return toast('That route is blocked by a recorded hazard', 'error'); selIdx = i; $$('.route-card').forEach((x) => x.classList.toggle('sel', x === c)); updStart(); }));
    updStart();
  }
  const updStart = () => { $('#start').disabled = !(plan?.status === 'ok' && selIdx != null && $('#consent').checked && can('admin', 'officer')); };
  $('#consent').onchange = updStart;

  // ------------------------------------------------------------------ guidance HUD
  $('#start').onclick = async () => {
    try {
      const s = await api('/navigation/sessions', { method: 'POST', body: { incident_id: incident.id, origin: { type: 'Point', coordinates: [pos.lon, pos.lat] }, consent_location_use: true, route_index: selIdx } });
      session = s; await hud(s);
    } catch (e) { toast(e.message, 'error'); $('#startHelp').textContent = e.message; }
  };

  async function hud(s) {
    const el = document.createElement('div'); el.className = 'nav-hud'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Guided navigation');
    el.innerHTML = `<video id="cam" playsinline muted></video>
      <div class="nav-top layer"><div class="chip" id="hMode"></div><div class="chip" id="hGps"></div><button class="button subtle" id="hExit" style="min-height:44px">Exit</button></div>
      <div class="nav-mid layer"><svg class="arrow" id="arrow" viewBox="0 0 100 100" aria-hidden="true"><path d="M50 5 L88 85 L50 66 L12 85 Z" fill="#7dff9e" stroke="#06361a" stroke-width="4" stroke-linejoin="round"/></svg></div>
      <div class="nav-bottom layer"><div id="hWarn"></div><div class="nav-instr" id="hInstr">Starting…</div><div class="nav-meta"><span id="hDist"></span><span id="hRem"></span><span id="hEta"></span></div><div class="progress"><i id="hProg" style="width:0"></i></div>
        <div class="row"><button class="button subtle" id="hRe" style="min-height:44px">Recalculate</button><button class="button subtle" id="hVoice" style="min-height:44px">🔈 Voice off</button><button class="button subtle" id="hMan" style="min-height:44px" hidden>Update position…</button><span class="help" id="hNote" style="color:#cfe"></span></div></div>`;
    document.body.appendChild(el);
    let heading = null, compassOn = false, stream = null, voice = false, lastSpoken = '', lastPost = 0, ended = false, lastInfo = null, wake = null;
    // camera backdrop (optional)
    try { stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false }); const v = $('#cam', el); v.srcObject = stream; await v.play(); $('#hMode', el).textContent = 'Camera + compass guidance'; } catch { $('#hMode', el).textContent = 'Compass/map guidance (camera unavailable)'; $('#cam', el).remove(); el.style.background = '#0e1a14'; }
    try { wake = await navigator.wakeLock?.request('screen'); } catch { /* optional */ }
    // compass
    const onOri = (e) => { let h = null; if (typeof e.webkitCompassHeading === 'number') h = e.webkitCompassHeading; else if (e.absolute && e.alpha != null) h = (360 - e.alpha) % 360; else if (e.alpha != null && e.type === 'deviceorientationabsolute') h = (360 - e.alpha) % 360; if (h != null) { heading = h; compassOn = true; draw(); } };
    try { if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') { const r = await DeviceOrientationEvent.requestPermission(); if (r !== 'granted') throw new Error('denied'); } window.addEventListener('deviceorientationabsolute', onOri, true); window.addEventListener('deviceorientation', onOri, true); } catch { $('#hNote', el).textContent = 'Compass permission denied: showing bearings only.'; }
    // GPS
    const manual = !navigator.geolocation;
    if (manual) $('#hMan', el).hidden = false;
    const send = async (lon, lat, acc, hd) => {
      if (ended || Date.now() - lastPost < 2500) return; lastPost = Date.now();
      try { lastInfo = await api(`/navigation/sessions/${s.session_id}/position`, { method: 'POST', body: { lon, lat, accuracy_m: acc ?? null, heading_deg: hd ?? null } }); draw(); if (lastInfo.arrived) arrived(); } catch (e) { $('#hNote', el).textContent = 'Server unreachable: ' + e.message + ' (last guidance may be stale)'; }
    };
    if (!manual) watchId = navigator.geolocation.watchPosition((p) => { pos = { lon: p.coords.longitude, lat: p.coords.latitude, acc: p.coords.accuracy, at: Date.now() }; $('#hGps', el).textContent = `GPS ±${Math.round(p.coords.accuracy)} m`; send(pos.lon, pos.lat, pos.acc, p.coords.heading); }, (e) => { $('#hGps', el).textContent = 'GPS lost'; $('#hNote', el).textContent = e.code === 1 ? 'Location permission denied' : 'No GPS signal. Move to open sky or enter position manually.'; $('#hMan', el).hidden = false; }, { enableHighAccuracy: true, maximumAge: 3000, timeout: 20000 });
    else $('#hGps', el).textContent = 'Manual position';
    $('#hMan', el).onclick = async () => { const v = prompt('Enter "latitude, longitude":', pos ? `${pos.lat.toFixed(6)}, ${pos.lon.toFixed(6)}` : ''); if (!v) return; const [la, lo] = v.split(',').map((x) => parseFloat(x)); if (!Number.isFinite(la) || !Number.isFinite(lo)) return; pos = { lon: lo, lat: la, acc: null, at: Date.now() }; lastPost = 0; await send(lo, la, null, null); };
    const speak = (t) => { if (voice && t && t !== lastSpoken && 'speechSynthesis' in window) { lastSpoken = t; speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(t)); } };
    $('#hVoice', el).onclick = () => { voice = !voice; $('#hVoice', el).textContent = voice ? '🔊 Voice on' : '🔈 Voice off'; };
    function draw() {
      const i = lastInfo; if (!i) return; const rot = (i.next_step?.bearing_deg ?? i.bearing_to_destination_deg) - (heading ?? 0);
      $('#arrow', el).style.transform = compassOn ? `rotate(${rot}deg)` : `rotate(${i.next_step?.bearing_deg ?? i.bearing_to_destination_deg}deg)`;
      const instr = i.next_step?.instruction || 'Continue toward the destination'; $('#hInstr', el).textContent = compassOn ? instr : `${instr} (bearing ${Math.round(i.next_step?.bearing_deg ?? i.bearing_to_destination_deg)}° ${compass(i.next_step?.bearing_deg ?? i.bearing_to_destination_deg)})`;
      $('#hDist', el).textContent = i.next_step ? `Next: ${i.next_step.distance_m} m` : ''; $('#hRem', el).textContent = `Remaining ${(i.distance_remaining_m / 1000).toFixed(2)} km`;
      const sp = s.route.distance_km * 1000 / (s.route.duration_min * 60); $('#hEta', el).textContent = `~${fmt.min(i.distance_remaining_m / sp / 60)} at route pace`; $('#hProg', el).style.width = `${Math.round(i.progress * 100)}%`;
      const w = []; if (!i.on_route) w.push(`<div class="nav-warn">OFF ROUTE by ${Math.round(i.off_route_m)} m — tap Recalculate</div>`); else w.push('<div class="nav-ok">On route</div>');
      i.warnings.forEach((x) => w.push(`<div class="nav-warn">${esc(x)}</div>`)); if (!compassOn) w.push('<div class="nav-warn" style="background:#8a5a12">Compass unavailable: arrow shows absolute bearing, not relative to where you face.</div>');
      $('#hWarn', el).innerHTML = w.join(''); speak(instr); if (!i.on_route && navigator.vibrate) navigator.vibrate([200, 100, 200]);
    }
    function arrived() { if (ended) return; $('#hInstr', el).textContent = 'You have arrived near the incident location.'; $('#hWarn', el).innerHTML = '<div class="nav-ok">Verify the area is safe before approaching. Follow official procedures.</div>'; }
    $('#hRe', el).onclick = async () => { if (!pos) return; try { const r = await api(`/navigation/sessions/${s.session_id}/reroute`, { method: 'POST', body: { lon: pos.lon, lat: pos.lat } }); if (!r.rerouted) { $('#hWarn', el).innerHTML = `<div class="nav-warn">${esc(r.message)}</div>`; return; } s.route = r.route; lastPost = 0; $('#hNote', el).textContent = `Route recalculated (${r.route.distance_km} km).`; await send(pos.lon, pos.lat, pos.acc, null); } catch (e) { $('#hNote', el).textContent = e.message; } };
    $('#hExit', el).onclick = async () => {
      const outcome = lastInfo?.arrived ? 'arrived' : 'aborted'; ended = true;
      navigator.geolocation?.clearWatch(watchId); window.removeEventListener('deviceorientation', onOri, true); window.removeEventListener('deviceorientationabsolute', onOri, true); stream?.getTracks().forEach((t) => t.stop()); wake?.release?.().catch(() => {}); speechSynthesis?.cancel?.();
      el.remove();
      try { const r = await api(`/navigation/sessions/${s.session_id}/end`, { method: 'POST', body: { outcome } }); summary(r.summary); } catch (e) { toast(e.message, 'error'); }
    };
    // first guidance immediately from the planned start position
    if (pos) { lastPost = 0; send(pos.lon, pos.lat, pos.acc, null); }
  }

  function summary(sm) {
    const d = document.createElement('dialog'); d.innerHTML = `<form method="dialog"><div class="dialog-header"><h3>Navigation session summary</h3></div><dl class="kv"><dt>Outcome</dt><dd>${esc(fmt.status(sm.outcome))}</dd><dt>Reroutes</dt><dd>${esc(sm.reroutes)}</dd><dt>Largest deviation from route</dt><dd>${esc(Math.round(sm.max_off_route_m))} m</dd><dt>Route length</dt><dd>${esc(sm.route_distance_km)} km</dd><dt>Route status at end</dt><dd>${esc(fmt.status(sm.route_status_at_end))}</dd></dl><div class="help">Your last position was deleted from the server. No track was stored. Record field findings in the incident (Investigations).</div><div class="dialog-actions"><a class="button subtle" href="#/investigations/${esc(incident.id)}">Open case</a><button class="button primary" value="ok">Done</button></div></form>`;
    document.body.appendChild(d); d.showModal(); d.addEventListener('close', () => d.remove());
  }
  return { destroy: () => { if (watchId != null) navigator.geolocation?.clearWatch(watchId); document.querySelectorAll('.nav-hud').forEach((n) => n.remove()); map.remove(); } };
}

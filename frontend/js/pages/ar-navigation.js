// Guided field navigation: GPS + compass (+ camera backdrop when permitted). This is NOT world-anchored AR:
// browsers cannot align arbitrary outdoor routes to the camera reliably, so the arrow is compass-guided and labelled so.
import { api, html, raw, esc, fmt, tag, empty, toast, can, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';
import { deviceReport, deviceHtml } from '../device.js';
import { REC } from './rangers.js';

const REVIEWED = ['APPROVED_FOR_INSPECTION', 'ASSIGNED', 'DISPATCHED', 'ACKNOWLEDGED', 'FIELD_VERIFICATION_PENDING'];
const compass = (d) => ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(d / 45) % 8];

const GEOM = { Point: 'Point', Polygon: 'Polygon', MultiPolygon: 'MultiPolygon' };
const evidenceOf = (i) => ({ cutting: 'Illegal cutting report', fire: 'Fire observation', encroachment: 'Encroachment report', pollution: 'Pollution report' }[i.evidence?.kind] || (i.change_id ? 'Satellite candidate (unverified)' : i.wildfire_id ? 'Fire observation' : 'Reported incident'));
const toDeg = (r) => (r * 180) / Math.PI;
const bearingTo = (a, b) => { const p1 = (a.lat * Math.PI) / 180, p2 = (b.lat * Math.PI) / 180, dl = ((b.lon - a.lon) * Math.PI) / 180; return (toDeg(Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl))) + 360) % 360; };

export async function render(root, { params }) {
  const reviewed = (await api('/incidents')).filter((i) => REVIEWED.includes(i.status));
  // a link from the live map names one incident: load it even if it is not approved yet, so its place and route can still be seen
  const wanted = params[0] ? await api('/incidents/' + encodeURIComponent(params[0])).catch(() => null) : null;
  const incidents = wanted && !reviewed.some((i) => i.id === wanted.id) ? [wanted, ...reviewed] : reviewed;
  const dev = await deviceReport();
  let incident = wanted || incidents[0], pos = null, watchId = null, plan = null, selIdx = null, mapObj = null, session = null, link = null, linkSeq = 0;
  if (!incidents.length) { root.innerHTML = html`<div class="panel">${raw(empty('No incident is ready for field response', 'Navigation is only available for incidents that a reviewer has approved. Review a case in Investigations first.', '⌁'))}<div class="row" style="margin-top:12px"><a class="button primary" href="#/investigations">Go to Investigations</a></div></div><div class="panel" style="margin-top:14px"><h4>Device compatibility</h4>${deviceHtml(dev)}</div>`; return; }
  root.innerHTML = html`
    <div class="warn-box"><b>Safety.</b> Stop in a safe place before using the device. Guidance can be wrong: GPS and compass errors are common under canopy, route data may omit forest tracks, and recorded hazards are only those someone has reported. This tool does not replace official procedures or your judgement.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel"><div class="row"><div class="grow"><h3>Route preview</h3></div></div><div id="map" class="map-box tall" role="application" aria-label="Route preview map"></div><div id="legendBox">${raw(legend([['#1d6b4b', 'Recommended route'], ['#6b7c74', 'Alternative route', true], ['#b8321f', 'Blocked route', true], ['#e8590c', 'Walking segment', true], ['#1976d2', 'You'], ['#bf731f', 'Incident']]))}</div>
        <div id="qrPanel" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line,#dfe8e2)"><h4 style="margin:0 0 8px">Open on ranger phone</h4><div id="qrBody" class="help">Choose a route and approved incident to get a QR code.</div></div></div>
      <div class="stack">
        <div class="panel"><h4>1 · Incident</h4><label class="f"><span class="sr-only">Choose an incident</span><select id="inc">${raw(incidents.map((i) => `<option value="${esc(i.id)}" ${i.id === incident.id ? 'selected' : ''}>${esc(i.title.slice(0, 70))}${REVIEWED.includes(i.status) ? '' : ' (not approved yet)'}</option>`).join(''))}</select></label>
          <dl class="kv" id="incInfo" style="margin-top:8px"></dl><div id="approveBox"></div></div>
        <div class="panel"><h4>2 · Your position</h4><div id="gps" class="note-box">Location not requested yet.</div>
          <div class="row" style="margin-top:8px"><button class="button primary sm" id="locate">Use GPS</button><span class="help">or enter manually:</span></div>
          <div class="grid g2" style="margin-top:6px"><label class="f">Latitude<input type="number" step="any" id="mlat"></label><label class="f">Longitude<input type="number" step="any" id="mlon"></label></div><button class="button subtle sm" id="mset" style="margin-top:6px">Use these coordinates</button></div>
        <div class="panel"><h4>3 · Routes</h4><div class="help" id="station" aria-live="polite">Looking for the nearest station…</div><div class="row" style="margin-top:6px"><button class="button primary" id="plan" disabled>Recalculate from my position</button></div><div id="routes" style="margin-top:8px"></div></div>        <div class="panel"><h4>4 · Start</h4><label class="row" style="align-items:flex-start"><input type="checkbox" id="consent" style="margin-top:3px"><span class="help">I consent to my device location being sent to this server while navigating. Only my latest position and summary figures are kept; no track is stored, and the last position is deleted when I end the session.</span></label>
          <div class="row" style="margin-top:8px"><button class="button primary" id="start" disabled ${can('admin', 'officer') ? '' : 'title="Officer role required"'}>Start guidance</button></div><div class="help" id="startHelp">${can('admin', 'officer') ? '' : 'Your role cannot start navigation sessions (forest officer or administrator).'}</div></div>
        <div class="panel"><h4>Device compatibility</h4>${deviceHtml(dev)}</div>
      </div>
    </div>`;
  const { map, group } = await createMap($('#map')); mapObj = map; root.__navMap = map;   // handle for tests
  const gR = group('Routes'), gM = group('You'), gI = group('Incident'), gH = group('Hazards');
  const approved = () => REVIEWED.includes(incident.status);
  const showInc = () => {
    gI.clearLayers();
    const area = incident.geometry.type !== 'Point';   // the real polygon, never a rectangle; a point is a marker
    const layer = addGeo(gI, incident.geometry, area ? { ...STYLE.incident, weight: 3, fillColor: '#bf731f', fillOpacity: 0.28 } : STYLE.incident, popup(incident.title, [fmt.status(incident.status)]), { radius: 9 });
    $('#incInfo').innerHTML = html`<dt>Title</dt><dd>${incident.title}</dd><dt>Status</dt><dd>${fmt.status(incident.status)}</dd><dt>Evidence</dt><dd>${evidenceOf(incident)}</dd><dt>Geometry</dt><dd>${GEOM[incident.geometry.type] || incident.geometry.type}${area ? '' : ' (marker)'}</dd><dt>Opened</dt><dd>${fmt.dt(incident.created_at)}</dd>`;
    fit(map, [layer], { maxZoom: 15, padding: [40, 40] });
    paintApprove();
  };
  function paintApprove() {
    const box = $('#approveBox'); if (!box) return;
    if (approved()) { box.innerHTML = ''; return; }
    const ok = can('admin', 'reviewer');
    box.innerHTML = html`<div class="warn-box" style="margin-top:8px"><b>Not approved for field response yet.</b> You can see where it is and the route, but a reviewer must approve it before guidance or a phone link can start.</div>${raw(ok ? '<button class="button primary sm" id="approve" style="margin-top:8px">Approve for field inspection</button>' : '')}`;
    $('#approve')?.addEventListener('click', async (e) => { e.target.disabled = true; try { await api(`/incidents/${incident.id}/review`, { method: 'POST', body: { decision: 'approve', notes: 'Approved from Ranger AR Navigation' } }); incident = await api('/incidents/' + incident.id); const k = incidents.findIndex((x) => x.id === incident.id); if (k >= 0) incidents[k] = incident; toast('Approved for field inspection'); showInc(); paintQr(); } catch (err) { toast(err.message, 'error'); e.target.disabled = false; } });
  }
  showInc(); api('/navigation/hazards').then((hz) => hz.forEach((h) => addGeo(gH, h.geometry, STYLE.hazard, popup(`${fmt.status(h.kind)} · ${h.verified ? 'verified' : 'unverified'}`, [h.source, `${h.age_hours} h ago`])))).catch(() => {});

  const setPos = (lon, lat, acc, label) => { pos = { lon, lat, acc, at: Date.now() }; gM.clearLayers(); addGeo(gM, { type: 'Point', coordinates: [lon, lat] }, { color: '#1976d2', fillColor: '#1976d2', fillOpacity: 0.9, weight: 3 }, null, { radius: 8 }); if (acc) L.circle([lat, lon], { radius: acc, weight: 1, color: '#1976d2', fillOpacity: 0.08 }).addTo(gM); $('#gps').innerHTML = html`<b>${label}</b><br>${lat.toFixed(5)}, ${lon.toFixed(5)}${acc != null ? ` · accuracy ±${Math.round(acc)} m` : ''} · updated ${new Date().toLocaleTimeString()}${acc > 50 ? raw('<br><span style="color:#b8321f">Poor accuracy: guidance may be unreliable.</span>') : ''}`; $('#plan').disabled = false; $('#mlat').value = lat.toFixed(6); $('#mlon').value = lon.toFixed(6); updStart(); };
  $('#locate').onclick = () => {
    if (!navigator.geolocation) return ($('#gps').innerHTML = '<span style="color:#b8321f">This browser has no geolocation. Enter coordinates manually.</span>');
    $('#gps').textContent = 'Requesting location permission…';
    navigator.geolocation.getCurrentPosition((p) => setPos(p.coords.longitude, p.coords.latitude, p.coords.accuracy, 'GPS fix'), (e) => ($('#gps').innerHTML = `<span style="color:#b8321f">${e.code === 1 ? 'Location permission was denied.' : e.code === 3 ? 'No GPS fix yet (timed out).' : 'Location unavailable.'}</span> Enter coordinates manually, or allow location in browser settings.`), { enableHighAccuracy: true, timeout: 20000, maximumAge: 5000 });
  };
  $('#mset').onclick = () => { const la = parseFloat($('#mlat').value), lo = parseFloat($('#mlon').value); if (!Number.isFinite(la) || !Number.isFinite(lo)) return toast('Enter valid coordinates', 'error'); setPos(lo, la, null, 'Manual position'); };
  const gS = group('Start');
  $('#inc').onchange = (e) => { incident = incidents.find((i) => i.id === e.target.value); plan = null; selIdx = null; gR.clearLayers(); gS.clearLayers(); $('#routes').innerHTML = ''; $('#start').disabled = true; showInc(); runPlan(); paintQr(); };

  // routes appear as soon as an incident is chosen: from the phone/GPS position if known, otherwise from the nearest registered station
  async function runPlan() {
    const b = $('#plan'); b.disabled = true; b.textContent = 'Calculating…'; $('#station').textContent = 'Working out the route…';
    try { plan = await api('/navigation/routes', { method: 'POST', body: { incident_id: incident.id, ...(pos ? { origin: { type: 'Point', coordinates: [pos.lon, pos.lat] } } : {}) } }); paintPlan(); }
    catch (e) { plan = null; selIdx = null; gR.clearLayers(); $('#station').textContent = ''; $('#routes').innerHTML = html`<div class="warn-box"><b>No route could be calculated.</b> ${e.message} The incident is still shown on the map.</div>`; paintQr(); }
    finally { b.disabled = !pos; b.textContent = 'Recalculate from my position'; }
  }
  $('#plan').onclick = runPlan;
  function paintPlan() {
    gR.clearLayers(); gS.clearLayers();
    if (plan.status !== 'ok') { $('#station').textContent = ''; $('#routes').innerHTML = html`<div class="warn-box"><b>Routing is unavailable</b> (${plan.status}${plan.reason ? ': ' + plan.reason : ''}). No route is implied. The incident stays on the map; use manual route review.</div>`; $('#start').disabled = true; paintQr(); return; }
    $('#station').innerHTML = html`<b>Starting from:</b> ${plan.origin_source}${plan.fallback_note ? raw('<br><span class="help">' + esc(plan.fallback_note) + '</span>') : ''}`;
    addGeo(gS, plan.origin, { color: '#1d6b4b', fillColor: '#1d6b4b', fillOpacity: 0.9, weight: 3 }, popup('Start of route', [plan.origin_source]), { radius: 8 });
    const ev = plan.evaluation; selIdx = ev.recommended_index; const ls = [];
    ev.routes.forEach((r) => r.geometry && ls.push(L.geoJSON(r.geometry, { style: r.index === ev.recommended_index ? STYLE.route : r.status === 'blocked' ? STYLE.routeBlocked : STYLE.routeAlt }).addTo(gR))); ev.routes.forEach((r) => (r.segments || []).filter((sg) => sg.mode === 'walk' && sg.geometry).forEach((sg) => L.geoJSON(sg.geometry, { style: { color: '#e8590c', weight: 5, dashArray: '2 9' } }).bindTooltip('🚶 On foot: ' + sg.distance_km + ' km').addTo(gR)));
    fit(map, ls.length ? ls : [], { padding: [40, 40], maxZoom: 15 });
    $('#routes').innerHTML = html`${raw(ev.message ? `<div class="error-box"><strong>${esc(ev.message)}</strong></div>` : '')}${ev.routes.map((r) => raw(`<div class="route-card ${r.status === 'blocked' ? 'blocked' : ''} ${r.index === selIdx ? 'sel' : ''}" data-i="${r.index}"><div class="row"><b>Route ${r.index + 1}</b>${tag(...REC[r.recommendation]).s}<span class="grow"></span><b>${esc(fmt.km(r.distance_km))}</b>${r.walk_km ? `<span title="On foot">🚗 ${esc(fmt.km(r.drive_km))} + 🚶 ${esc(fmt.km(r.walk_km))}</span>` : ''}<span>${esc(fmt.min(r.adjusted_duration_min ?? r.duration_min))}</span></div>${r.reasons.map((x) => `<div class="reason ${x.code === 'weather_advisory' ? 'adv' : ''}">${esc(x.text)}</div>`).join('')}</div>`))}<div class="help">${esc(ev.disclaimer)}</div>`;
    $$('.route-card').forEach((c) => (c.onclick = () => { const i = +c.dataset.i; if (ev.routes[i].status === 'blocked') return toast('That route is blocked by a recorded hazard', 'error'); selIdx = i; $$('.route-card').forEach((x) => x.classList.toggle('sel', x === c)); updStart(); paintQr(); }));
    updStart(); paintQr();
  }

  // ------------------------------------------------------------------ QR code for the ranger's phone
  async function paintQr() {
    const box = $('#qrBody'); if (!box) return;
    const seq = ++linkSeq; link = null;
    if (!plan || plan.status !== 'ok' || selIdx == null) { box.className = 'help'; box.textContent = 'A QR code appears here once a route has been calculated.'; return; }
    if (!approved()) { box.className = 'help'; box.textContent = 'Approve the incident for field inspection first. Then a QR code appears here.'; return; }
    if (!can('admin', 'reviewer', 'officer')) { box.className = 'help'; box.textContent = 'Your role cannot create a phone link (reviewer, officer or administrator).'; return; }
    box.className = 'help'; box.textContent = 'Creating the phone link…';
    try {
      const r = await api('/navigation/mobile-links', { method: 'POST', body: { incident_id: incident.id, route_index: selIdx } });
      if (seq !== linkSeq) return;   // a newer choice replaced this one
      link = r;
      const { qrSvg } = await import('../qr.js');
      box.className = '';
      box.innerHTML = html`<div class="qr-wrap"><div class="qr-img" id="qrImg" role="img" aria-label="QR code that opens guidance for ${incident.title} on a phone">${raw(qrSvg(r.url))}</div>
        <div class="qr-side"><b>${incident.title}</b><div class="help">Route ${selIdx + 1} · ${GEOM[r.geometry_type] || r.geometry_type} incident</div>
          <div class="qr-url" id="qrUrl">${r.url}</div>
          <div class="row" style="margin-top:8px"><button class="button subtle sm" id="qrCopy">Copy link</button><a class="button primary sm" id="qrOpen" href="${r.url}" target="_blank" rel="noopener">Open mobile view</a></div></div></div>
        ${raw(r.warning ? `<div class="warn-box" style="margin-top:8px" id="qrWarn">${esc(r.warning)}</div>` : '')}
        <div class="help" id="qrExp" style="margin-top:6px">This link works for about ${esc(r.expires_in_minutes)} minutes (until ${esc(new Date(r.expires_at).toLocaleTimeString())}). Scan it with the phone camera.</div>
        <div class="help">Privacy: the link carries only the incident and an expiry time, no password. The phone shares its position only while guiding, and nothing is kept after it exits.</div>`;
      $('#qrCopy').onclick = async () => { try { await navigator.clipboard.writeText(r.url); } catch { const t = document.createElement('textarea'); t.value = r.url; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); } toast('Link copied'); };
    } catch (e) { if (seq === linkSeq) { box.className = 'help'; box.textContent = 'Could not create the phone link: ' + e.message; } }
  }
  const updStart = () => { $('#start').disabled = !(plan?.status === 'ok' && selIdx != null && pos && approved() && $('#consent').checked && can('admin', 'officer')); };
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
  runPlan();
  return { destroy: () => { linkSeq++; if (watchId != null) navigator.geolocation?.clearWatch(watchId); document.querySelectorAll('.nav-hud').forEach((n) => n.remove()); map.remove(); } };
}

// Ranger phone page: opened from a QR code. Camera (when allowed) + GPS + compass guidance toward one incident.
// It is guidance only, not world-anchored AR: the arrow is compass-guided. Only the latest position is sent; nothing is stored on the phone.
const $ = (s) => document.querySelector(s);
const TOKEN = decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
const API = `/api/v1/mobile/ar/${encodeURIComponent(TOKEN)}`;
const CARD = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const rad = (d) => (d * Math.PI) / 180;
const cardinal = (d) => CARD[Math.round(d / 45) % 8];
const S = { info: null, pos: null, session: null, last: null, dest: null, heading: null, compass: false, stream: null, watch: null, wake: null, lastPost: 0, ended: false, onOri: null, route: null };

function show(id) { ['s-load', 's-error', 's-start', 's-guide', 's-end'].forEach((x) => { $('#' + x).hidden = x !== id; }); }
async function call(path, body) {
  const r = await fetch(API + path, { method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
  let data = null; try { data = await r.json(); } catch { /* not json */ }
  if (!r.ok) { const e = new Error(typeof data?.detail === 'string' ? data.detail : r.status === 410 ? 'This link has expired.' : r.status === 401 ? 'This link is not valid.' : `Request failed (${r.status})`); e.status = r.status; throw e; }
  return data;
}
function distM(a, b) { const p = rad(b.lat - a.lat), l = rad(b.lon - a.lon), h = Math.sin(p / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(l / 2) ** 2; return 12742000 * Math.asin(Math.sqrt(h)); }
function bearingM(a, b) { const p1 = rad(a.lat), p2 = rad(b.lat), dl = rad(b.lon - a.lon); return ((Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) * 180) / Math.PI + 360) % 360; }
const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
const geomWords = { Point: 'A point on the map', Polygon: 'An area (polygon)', MultiPolygon: 'Several areas (multipolygon)' };

async function boot() {
  try { S.info = await call(''); } catch (e) { $('#errMsg').textContent = e.message; show('s-error'); return; }
  const i = S.info.incident;
  $('#incTitle').textContent = i.title; $('#incStatus').textContent = i.status.replace(/_/g, ' ').toLowerCase(); $('#incEvidence').textContent = i.evidence_type;
  $('#incGeom').textContent = geomWords[i.geometry_type] || i.geometry_type; $('#incExp').textContent = new Date(S.info.expires_at).toLocaleTimeString();
  const insecure = !window.isSecureContext;
  if (insecure) { const w = $('#secureWarn'); w.hidden = false; w.textContent = 'This page is not on a secure (HTTPS) address, so the browser may refuse the camera, location and compass. Ask for an HTTPS link if guidance cannot start.'; }
  if (!S.info.reviewed) { $('#startMsg').textContent = 'This incident is no longer approved for field response.'; $('#startMsg').className = 'msg bad'; }
  $('#consent').onchange = () => { $('#go').disabled = !$('#consent').checked || !S.info.reviewed; };
  $('#go').onclick = start;
  $('#mgo').onclick = () => { const la = parseFloat($('#mlat').value), lo = parseFloat($('#mlon').value); if (!Number.isFinite(la) || !Number.isFinite(lo)) return msg('Enter a valid latitude and longitude.', true); begin({ lat: la, lon: lo, acc: null }, false); };
  show('s-start');
}
function msg(t, bad) { const m = $('#startMsg'); m.textContent = t; m.className = 'msg' + (bad ? ' bad' : ''); }

// Starting needs a tap: that is when the browser may ask for the compass, location and camera.
async function start() {
  $('#go').disabled = true; msg('Asking for permissions…');
  try { if (typeof DeviceOrientationEvent !== 'undefined' && typeof DeviceOrientationEvent.requestPermission === 'function') { const r = await DeviceOrientationEvent.requestPermission(); if (r !== 'granted') throw new Error('denied'); } listenCompass(); } catch { S.compassDenied = true; }
  if (!navigator.geolocation) { msg('This phone browser has no location service. Enter your position by hand.', true); $('#manualBox').hidden = false; $('#manualBox').open = true; $('#go').disabled = false; return; }
  navigator.geolocation.getCurrentPosition((p) => begin({ lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy }, true),
    (e) => { msg(e.code === 1 ? 'Location permission was denied. Allow it in the browser settings, or enter your position by hand.' : e.code === 3 ? 'No GPS fix yet. Move to open sky and try again, or enter your position by hand.' : 'Location is unavailable. Enter your position by hand.', true); $('#manualBox').hidden = false; $('#manualBox').open = true; $('#go').disabled = !$('#consent').checked; },
    { enableHighAccuracy: true, timeout: 20000, maximumAge: 5000 });
}
function listenCompass() {
  if (S.onOri) return;
  S.onOri = (e) => { let h = null; if (typeof e.webkitCompassHeading === 'number') h = e.webkitCompassHeading; else if ((e.absolute || e.type === 'deviceorientationabsolute') && e.alpha != null) h = (360 - e.alpha) % 360; if (h != null) { S.heading = h; S.compass = true; draw(); } };
  window.addEventListener('deviceorientationabsolute', S.onOri, true); window.addEventListener('deviceorientation', S.onOri, true);
}

async function begin(first, live) {
  msg('Starting guidance…'); S.pos = { ...first, at: Date.now() };
  try { S.session = await call('/session', { origin: { type: 'Point', coordinates: [first.lon, first.lat] }, consent_location_use: true }); }
  catch (e) { msg(e.status === 410 || e.status === 401 ? e.message : `Could not start guidance: ${e.message}`, true); $('#go').disabled = !$('#consent').checked; return; }
  S.route = S.session.route; const d = S.session.destination.coordinates; S.dest = { lon: d[0], lat: d[1] };
  $('#hTitle').textContent = S.info.incident.title;
  show('s-guide');
  await camera();
  if (!S.compassDenied) listenCompass();
  setChip('#hCompass', S.compassDenied ? 'Compass unavailable: arrow shows absolute bearing' : 'Compass: waiting for a reading…');
  try { S.wake = await navigator.wakeLock?.request('screen'); } catch { /* optional */ }
  if (live && navigator.geolocation) S.watch = navigator.geolocation.watchPosition((p) => { S.pos = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy, at: Date.now() }; send(); },
    (e) => { setChip('#hGps', 'GPS lost'); warn(e.code === 1 ? 'Location permission denied.' : 'No GPS signal. Move to open sky.'); }, { enableHighAccuracy: true, maximumAge: 3000, timeout: 20000 });
  else setChip('#hGps', 'Manual position');
  S.lastPost = 0; send();
}
async function camera() {
  try { S.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false }); const v = $('#cam'); v.srcObject = S.stream; await v.play(); setChip('#hMode', 'Camera and compass guidance'); }
  catch { setChip('#hMode', 'Camera unavailable: map-free guidance view'); $('#cam').remove(); $('#s-guide').style.background = '#0e1a14'; }
}
const setChip = (sel, t) => { $(sel).textContent = t; };
const warn = (t) => { $('#hWarn').innerHTML = ''; const d = document.createElement('div'); d.className = 'warn'; d.textContent = t; $('#hWarn').appendChild(d); };

async function send() {
  if (S.ended || !S.session || Date.now() - S.lastPost < 2500) return; S.lastPost = Date.now();
  const p = S.pos; setChip('#hGps', p.acc != null ? `GPS ±${Math.round(p.acc)} m` : 'Manual position');
  try { S.last = await call(`/sessions/${S.session.session_id}/position`, { lon: p.lon, lat: p.lat, accuracy_m: p.acc ?? null, heading_deg: S.heading ?? null }); draw(); }
  catch (e) { if (e.status === 410) { warn('The link has expired. Exit and ask for a new QR code.'); } else warn('Server unreachable: ' + e.message + '. Guidance may be out of date.'); draw(); }
}

function draw() {
  const p = S.pos, i = S.last; if (!p || !S.dest) return;
  const direct = distM(p, S.dest), brgDest = bearingM(p, S.dest), target = i?.next_step?.bearing_deg ?? i?.bearing_to_destination_deg ?? brgDest;
  const rot = S.compass && S.heading != null ? target - S.heading : target;
  $('#arrow').style.transform = `rotate(${rot}deg)`;
  $('#hDist').textContent = fmtDist(i ? Math.max(direct, 0) : direct); $('#hBearing').textContent = `${Math.round(brgDest)}° ${cardinal(brgDest)}`; $('#hAcc').textContent = p.acc != null ? `±${Math.round(p.acc)} m` : 'manual';
  setChip('#hCompass', S.compass ? `Compass ${Math.round(S.heading)}° ${cardinal(S.heading)}` : 'Compass unavailable: arrow shows absolute bearing');
  const prog = Math.round((i?.progress ?? 0) * 100); $('#hProg').style.width = prog + '%'; $('#hRoute').textContent = `Route progress ${prog}%${i ? ` · ${(i.distance_remaining_m / 1000).toFixed(2)} km of route left` : ''}`; document.querySelector('.bar').setAttribute('aria-valuenow', String(prog));
  const instr = i?.next_step?.instruction || 'Continue toward the incident'; $('#hInstr').textContent = i?.arrived ? 'You have arrived near the incident location.' : S.compass ? instr : `${instr} (bearing ${Math.round(target)}° ${cardinal(target)})`;
  const w = $('#hWarn'); w.innerHTML = '';
  const add = (cls, t) => { const d = document.createElement('div'); d.className = cls; d.textContent = t; w.appendChild(d); };
  if (i) { if (!i.on_route) add('warn', `OFF ROUTE by ${Math.round(i.off_route_m)} m. Tap Recalculate.`); else add('ok', 'On route'); (i.warnings || []).forEach((x) => add('warn', x)); if (i.arrived) add('ok', 'Verify the area is safe before approaching. Follow official procedures.'); }
  if (p.acc != null && p.acc > 30 && !(i?.warnings || []).some((x) => /accuracy/i.test(x))) add('warn', `GPS accuracy is poor (±${Math.round(p.acc)} m). Guidance may be unreliable.`);
  if (!S.compass) add('warn amber', 'Compass unavailable: the arrow shows the absolute bearing, not where you are facing.');
  if (i && !i.on_route && navigator.vibrate) navigator.vibrate([200, 100, 200]);
}

$('#hRe').onclick = async () => {
  if (!S.pos || !S.session) return; $('#hRe').disabled = true;
  try { const r = await call(`/sessions/${S.session.session_id}/reroute`, { lon: S.pos.lon, lat: S.pos.lat }); if (!r.rerouted) warn(r.message || 'No route is available.'); else { S.route = r.route; S.lastPost = 0; await send(); } }
  catch (e) { warn('Could not recalculate: ' + e.message); } finally { $('#hRe').disabled = false; }
};
async function finish(outcome, keepalive) {
  if (S.ended) return; S.ended = true;
  if (S.watch != null) navigator.geolocation.clearWatch(S.watch);
  if (S.onOri) { window.removeEventListener('deviceorientation', S.onOri, true); window.removeEventListener('deviceorientationabsolute', S.onOri, true); }
  S.stream?.getTracks().forEach((t) => t.stop()); S.wake?.release?.().catch(() => {});
  if (S.session) { try { await fetch(`${API}/sessions/${S.session.session_id}/end`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ outcome }), keepalive: !!keepalive }); } catch { /* the server drops the position when the session ends; if it cannot be reached the link simply expires */ } }
  S.session = null;
}
$('#hExit').onclick = async () => { await finish(S.last?.arrived ? 'arrived' : 'aborted'); $('#endMsg').textContent = S.last?.arrived ? 'You arrived near the incident.' : 'You left before arriving.'; show('s-end'); };
window.addEventListener('pagehide', () => { finish('aborted', true); });
window.__mobileAr = S;   // read-only handle for tests
boot();

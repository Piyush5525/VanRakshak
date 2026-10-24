// Place search for the live map: type the first letters, pick a suggestion, the map jumps there.
// Uses Mapbox place search when the page has a token, otherwise OpenStreetMap's Nominatim. Nothing is stored except recent searches in this browser.
import { esc } from './core.js';
import { config } from './map.js';

const $ = (s, r = document) => r.querySelector(s);
const ZOOM = { country: 5, region: 7, district: 9, place: 12, postcode: 13, locality: 13, neighborhood: 14, address: 17, poi: 16 };
const ICON = { country: '🌍', region: '🗺', district: '🗺', place: '🏙', postcode: '✉', locality: '🏘', neighborhood: '🏘', address: '🏠', poi: '📍', coords: '🧭', recent: '🕘', here: '🎯' };
const S = { map: null, token: null, items: [], active: -1, abort: null, timer: null, pin: null };
const recents = () => { try { return JSON.parse(localStorage.getItem('rk.searches') || '[]'); } catch { return []; } };
const remember = (it) => { try { localStorage.setItem('rk.searches', JSON.stringify([it, ...recents().filter((r) => r.name !== it.name)].slice(0, 5))); } catch { /* private mode */ } };

export async function init({ map }) {
  S.map = map;
  const cfg = await config().catch(() => ({}));
  S.token = cfg.mapbox_public_token || null;
  const box = $('#search'), input = $('#q'), list = $('#sugg'), clear = $('#qClear');
  input.addEventListener('input', () => { clear.hidden = !input.value; clearTimeout(S.timer); S.timer = setTimeout(() => lookup(input.value.trim()), 220); });
  input.addEventListener('focus', () => lookup(input.value.trim()));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); pick(S.active >= 0 ? S.active : 0); }
    else if (e.key === 'Escape') { close(); input.blur(); }
  });
  clear.onclick = () => { input.value = ''; clear.hidden = true; clearPin(); lookup(''); input.focus(); };
  list.addEventListener('mousedown', (e) => { const li = e.target.closest('[data-i]'); if (li) { e.preventDefault(); pick(Number(li.dataset.i)); } });
  document.addEventListener('mousedown', (e) => { if (!box.contains(e.target)) close(); });
  document.addEventListener('click', (e) => { if (e.target.id === 'pinX') { clearPin(); input.value = ''; clear.hidden = true; } });
  document.addEventListener('keydown', (e) => { if (e.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); input.focus(); } });
}

function render() {
  const list = $('#sugg'); if (!list) return;
  list.hidden = !S.items.length;
  list.innerHTML = S.items.map((it, i) => `<li role="option" data-i="${i}" class="${i === S.active ? 'on' : ''}" aria-selected="${i === S.active}"><span class="si">${ICON[it.kind] || '📍'}</span><span class="st"><b>${esc(it.name)}</b><small>${esc(it.detail || '')}</small></span></li>`).join('');
}
function move(d) { if (!S.items.length) return; S.active = (S.active + d + S.items.length) % S.items.length; render(); $('#sugg .on')?.scrollIntoView({ block: 'nearest' }); }
function close() { S.items = []; S.active = -1; render(); }

// "26.85, 75.8" jumps straight to coordinates
function asCoords(q) {
  const m = q.match(/^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/);
  if (!m) return null;
  const a = Number(m[1]), b = Number(m[2]);
  return a >= -90 && a <= 90 && b >= -180 && b <= 180 ? { name: `${a}, ${b}`, detail: 'Go to these coordinates', kind: 'coords', lat: a, lon: b, zoom: 15 } : null;
}

async function lookup(q) {
  S.abort?.abort();
  if (!q) { S.items = [{ name: 'Use my location', detail: 'Centre the map on where you are', kind: 'here' }, ...recents().map((r) => ({ ...r, kind: 'recent', detail: r.detail || 'Recent search' }))]; S.active = -1; render(); return; }
  const c = asCoords(q);
  if (c) { S.items = [c]; S.active = 0; render(); return; }
  if (q.length < 2) { S.items = []; render(); return; }
  S.abort = new AbortController();
  try {
    const b = S.map.getCenter();
    S.items = S.token ? await mapbox(q, b, S.abort.signal) : await nominatim(q, S.abort.signal);
    if (!S.items.length) S.items = [{ name: 'No place found', detail: 'Check the spelling or try a nearby town', kind: 'none', none: true }];
    S.active = S.items[0].none ? -1 : 0;
  } catch (e) { if (e.name === 'AbortError') return; S.items = [{ name: 'Search is not available right now', detail: 'Check your connection and try again', kind: 'none', none: true }]; S.active = -1; }
  render();
}
async function mapbox(q, c, signal) {
  const r = await fetch(`https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(q)}.json?access_token=${S.token}&autocomplete=true&country=in&limit=7&language=en&proximity=${c.lng},${c.lat}`, { signal });
  if (!r.ok) throw new Error('search failed');
  return (await r.json()).features.map((f) => ({ name: f.text, detail: f.place_name.replace(new RegExp(`^${f.text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')},?\\s*`), ''), kind: f.place_type[0], lat: f.center[1], lon: f.center[0], bbox: f.bbox, zoom: ZOOM[f.place_type[0]] || 12 }));
}
async function nominatim(q, signal) {
  const r = await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1&limit=7&countrycodes=in&q=${encodeURIComponent(q)}`, { signal, headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error('search failed');
  return (await r.json()).map((x) => {
    const parts = x.display_name.split(', '), kind = x.addresstype === 'state' ? 'region' : x.addresstype === 'county' || x.addresstype === 'state_district' ? 'district' : ['city', 'town', 'village'].includes(x.addresstype) ? 'place' : 'poi';
    return { name: x.name || parts[0], detail: parts.slice(1, 4).join(', '), kind, lat: Number(x.lat), lon: Number(x.lon), bbox: x.boundingbox ? [Number(x.boundingbox[2]), Number(x.boundingbox[0]), Number(x.boundingbox[3]), Number(x.boundingbox[1])] : null, zoom: ZOOM[kind] || 12 };
  });
}

function pick(i) {
  const it = S.items[i]; if (!it || it.none) return;
  if (it.kind === 'here') { locate(); return; }
  go(it); remember({ name: it.name, detail: it.detail, lat: it.lat, lon: it.lon, bbox: it.bbox, zoom: it.zoom });
  $('#q').value = it.name; $('#qClear').hidden = false; close(); $('#q').blur();
}
function go(it) {
  if (it.bbox && it.zoom <= 14) S.map.fitBounds([[it.bbox[1], it.bbox[0]], [it.bbox[3], it.bbox[2]]], { maxZoom: Math.max(it.zoom, 13), padding: [40, 40] });
  else S.map.flyTo([it.lat, it.lon], it.zoom || 14, { duration: 0.9 });
  clearPin();
  S.pin = L.marker([it.lat, it.lon], { icon: L.divIcon({ className: '', iconSize: [30, 30], iconAnchor: [15, 30], html: '<div class="pin" style="background:#1565c0"><span>📍</span></div>' }), title: it.name }).addTo(S.map)
    .bindPopup(`<b>${esc(it.name)}</b><br>${esc(it.detail || '')}<br><button class="btn sm" id="pinX" style="margin-top:6px">✕ Remove this pin</button>`).openPopup();
}
function clearPin() { if (S.pin) { S.pin.closePopup(); S.map.removeLayer(S.pin); S.pin = null; } }
function locate() {
  if (!navigator.geolocation) { $('#q').placeholder = 'Location is not available in this browser'; return; }
  navigator.geolocation.getCurrentPosition((p) => { go({ name: 'My location', detail: `${p.coords.latitude.toFixed(4)}, ${p.coords.longitude.toFixed(4)}`, lat: p.coords.latitude, lon: p.coords.longitude, zoom: 15 }); close(); }, () => { S.items = [{ name: 'Could not get your location', detail: 'Allow location access in the browser', kind: 'none', none: true }]; render(); }, { timeout: 8000 });
}

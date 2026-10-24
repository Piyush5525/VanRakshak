// One way to show a journey everywhere: 🚗 the part a vehicle can drive, 🚶 the part that has to be walked.
import { esc } from './core.js';

export const ICON = { drive: '🚗', walk: '🚶' };
const num = (v) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 1 });

// "🚗 12.4 km · 30 min by road   🚶 0.4 km · 10 min on foot"
export function journeyHtml(r) {
  if (!r) return '';
  const driveKm = r.drive_km ?? r.distance_km, walkKm = r.walk_km || 0;
  const parts = [];
  if (driveKm > 0 || !walkKm) parts.push(`<span class="leg drive" title="By road"><i>${ICON.drive}</i><b>${num(driveKm)} km</b><small>${Math.round(r.drive_min ?? r.duration_min)} min by road</small></span>`);
  if (walkKm > 0) parts.push(`<span class="leg walk" title="On foot"><i>${ICON.walk}</i><b>${num(walkKm)} km</b><small>${Math.round(r.walk_min)} min on foot</small></span>`);
  return `<div class="journey">${parts.join('<span class="plus">+</span>')}</div>`;
}
// short text for a line of notes: "12.4 km, about 40 min (🚗 12 km road + 🚶 0.4 km on foot)"
export function journeyText(r) {
  if (!r) return '';
  const base = `${num(r.distance_km)} km, about ${Math.round(r.duration_min)} min`;
  return r.walk_km ? `${base} (${ICON.drive} ${num(r.drive_km)} km by road + ${ICON.walk} ${num(r.walk_km)} km on foot)` : base;
}
export const legendHtml = `<div class="legend2 routeleg"><span><i class="ln drive"></i>🚗 Road</span><span><i class="ln walk"></i>🚶 Walking path</span></div>`;

// draw a route on a Leaflet layer group: roads solid, walking legs dashed with a small walker on the middle
export function drawRoute(group, route, opts = {}) {
  const segs = route?.segments?.length ? route.segments : (route?.geometry ? [{ mode: 'drive', geometry: route.geometry }] : []);
  for (const s of segs) {
    if (!s.geometry?.coordinates?.length) continue;
    if (s.mode === 'walk') {
      const line = L.geoJSON(s.geometry, { style: { color: '#e8590c', weight: 5, opacity: 0.95, dashArray: '2 9', lineCap: 'round' }, interactive: true }).bindTooltip(`${ICON.walk} On foot: ${num(s.distance_km)} km, about ${Math.round(s.duration_min)} min${s.estimated ? ' (straight-line estimate, no mapped path)' : ''}`, { sticky: true }).addTo(group);
      const c = s.geometry.coordinates, mid = c[Math.floor(c.length / 2)];
      L.marker([mid[1], mid[0]], { icon: L.divIcon({ className: '', iconSize: [26, 26], iconAnchor: [13, 13], html: '<div class="seg-ic walk">🚶</div>' }), interactive: false }).addTo(group);
    } else {
      L.geoJSON(s.geometry, { style: { color: opts.color || '#14573c', weight: 6, opacity: 0.9 }, interactive: true }).bindTooltip(`${ICON.drive} By road: ${num(s.distance_km)} km, about ${Math.round(s.duration_min)} min`, { sticky: true }).addTo(group);
      const c = s.geometry.coordinates, mid = c[Math.floor(c.length / 2)];
      L.marker([mid[1], mid[0]], { icon: L.divIcon({ className: '', iconSize: [26, 26], iconAnchor: [13, 13], html: '<div class="seg-ic drive">🚗</div>' }), interactive: false }).addTo(group);
    }
  }
}
export const walkLink = (r) => (r?.walk_url ? `<a href="${esc(r.walk_url)}" target="_blank" rel="noopener">${ICON.walk} Walking directions</a>` : '');

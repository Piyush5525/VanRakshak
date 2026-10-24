// Leaflet helpers: base layers (OSM + optional Mapbox satellite), geometry drawing, fitting.
import { api, esc } from './core.js';

let clientConfig;
export async function config() {
  if (!clientConfig) clientConfig = await api('/system/client-config').catch(() => ({}));
  return clientConfig;
}

export async function createMap(el, { center = [22.5, 79], zoom = 5, layerControl = true } = {}) {
  const map = L.map(el, { zoomControl: true, attributionControl: true }).setView(center, zoom);
  const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
  const base = { OpenStreetMap: osm };
  const cfg = await config();
  if (cfg.mapbox_public_token) {
    base['Satellite (Mapbox)'] = L.tileLayer(`https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=${cfg.mapbox_public_token}`, { maxZoom: 19, attribution: '&copy; Mapbox &copy; OpenStreetMap contributors &copy; Maxar' });
  }
  const overlays = {};
  const control = layerControl ? L.control.layers(base, overlays, { collapsed: true }).addTo(map) : null;
  const group = (name, visible = true) => { const g = L.layerGroup(); overlays[name] = g; control?.addOverlay(g, name); if (visible) g.addTo(map); return g; };
  return { map, group, control, osm, base };
}

export const STYLE = {
  region: { color: '#1d6b4b', weight: 2, fillOpacity: 0.04 },
  loss: { color: '#b8321f', weight: 1, fillColor: '#d9482f', fillOpacity: 0.55 },
  lossAgree: { color: '#8f1d10', weight: 1.5, fillColor: '#c73b25', fillOpacity: 0.65 },
  lossDisagree: { color: '#bf731f', weight: 1.5, fillColor: '#e3a14b', fillOpacity: 0.55 },
  incident: { color: '#bf731f', weight: 2, fillOpacity: 0.1 },
  fire: { color: '#c0392b', weight: 1, fillColor: '#ff6a2b', fillOpacity: 0.85 },
  camera: { color: '#6b3fa0', weight: 1, fillColor: '#a47bd4', fillOpacity: 0.85 },
  site: { color: '#4b5d70', weight: 2, fillColor: '#8da0b3', fillOpacity: 0.3, dashArray: '5 4' },
  hazard: { color: '#7a1fa2', weight: 3, fillColor: '#b04bd6', fillOpacity: 0.4 },
  route: { color: '#1d6b4b', weight: 6, opacity: 0.9 },
  routeAlt: { color: '#6b7c74', weight: 4, opacity: 0.8, dashArray: '8 8' },
  routeBlocked: { color: '#b8321f', weight: 4, opacity: 0.85, dashArray: '2 8' },
};

export function addGeo(group, geometry, style, popup, point = { radius: 7 }) {
  const layer = L.geoJSON(geometry, { style, pointToLayer: (_, ll) => L.circleMarker(ll, { ...style, ...point }) });
  if (popup) layer.bindPopup(popup);
  layer.addTo(group);
  return layer;
}

export function fit(map, layers, opts = { padding: [24, 24], maxZoom: 15 }) {
  let b = null;
  for (const l of layers) { const lb = l.getBounds?.(); if (lb?.isValid()) b = b ? b.extend(lb) : lb; }
  if (b) map.fitBounds(b, opts);
  return !!b;
}

// Let the user draw a polygon or rectangle; resolves with GeoJSON geometry, or null if cancelled.
export function drawShape(map, kind = 'polygon') {
  return new Promise((resolve) => {
    const opts = { shapeOptions: { color: '#1d6b4b', weight: 2 }, showArea: false };
    const h = kind === 'rectangle' ? new L.Draw.Rectangle(map, opts) : new L.Draw.Polygon(map, opts);
    let done = false;
    const finish = (v) => { if (done) return; done = true; map.off('draw:created', onCreate); map.off('draw:drawstop', onStop); document.removeEventListener('keydown', onKey); resolve(v); };
    const onCreate = (e) => finish(e.layer.toGeoJSON().geometry);
    const onStop = () => setTimeout(() => finish(null), 50);
    const onKey = (e) => { if (e.key === 'Escape') { h.disable(); finish(null); } };
    map.on('draw:created', onCreate); map.on('draw:drawstop', onStop); document.addEventListener('keydown', onKey);
    h.enable();
  });
}

export const popup = (title, lines = []) => `<strong>${esc(title)}</strong>${lines.map((l) => `<br>${esc(l)}`).join('')}`;
export const legend = (items) => `<div class="legend">${items.map(([c, t, dashed]) => `<span><i style="background:${dashed ? 'transparent' : c};border:2px ${dashed ? 'dashed' : 'solid'} ${c}"></i>${esc(t)}</span>`).join('')}</div>`;

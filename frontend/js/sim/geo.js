// Geometry helpers for the incident simulation. Pure functions, no browser or Three.js needed, so they can be tested on their own.
// Everything here is SYNTHETIC scene building from an incident's boundary; none of it is a measurement or a forecast.

// ---- deterministic randomness: the same incident id always gives the same forest
export function hashString(s) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// ---- geometry
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const validRing = (r) => Array.isArray(r) && r.length >= 4 && r.every((p) => Array.isArray(p) && isNum(p[0]) && isNum(p[1]));

/** GeoJSON Polygon / MultiPolygon to a list of polygons; each polygon is [outerRing, ...holes]; rings are [lon, lat] pairs. Anything else gives []. */
export function polygonsOf(geometry) {
  if (!geometry) return [];
  if (geometry.type === 'Polygon') return Array.isArray(geometry.coordinates) && geometry.coordinates.every(validRing) && geometry.coordinates.length ? [geometry.coordinates] : [];
  if (geometry.type === 'MultiPolygon') return (geometry.coordinates || []).filter((p) => Array.isArray(p) && p.length && p.every(validRing));
  return [];
}
function allPoints(geometry) {
  const out = [];
  const walk = (c) => { if (Array.isArray(c) && c.length === 2 && isNum(c[0]) && isNum(c[1])) out.push(c); else if (Array.isArray(c)) c.forEach(walk); };
  walk(geometry?.coordinates);
  return out;
}
export function centerOf(geometry) {
  const pts = allPoints(geometry);
  if (!pts.length) return null;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
}

/** Metres east (x) and metres south (z) of `center` ([lon, lat]). Good for the few kilometres an incident covers. */
export function project(lon, lat, center) {
  const k = 111320 * Math.cos((center[1] * Math.PI) / 180);
  return { x: (lon - center[0]) * k, z: -(lat - center[1]) * 110540 };
}
export function unproject(x, z, center) {
  const k = 111320 * Math.cos((center[1] * Math.PI) / 180);
  return { lon: center[0] + x / k, lat: center[1] - z / 110540 };
}

/**
 * The boundary the simulation is drawn inside.
 * Polygon / MultiPolygon: the exact incident geometry.
 * Point (or anything else): a synthetic irregular boundary around it, generated from the incident id so it never changes.
 * Returns { kind: 'polygon' | 'synthetic', polygons (local metres), center: [lon, lat], note, bbox, areaHa }.
 */
export function boundaryFor(geometry, incidentId, opts = {}) {
  const polys = polygonsOf(geometry);
  const center = centerOf(geometry);
  if (!center) throw new Error('This incident has no usable location.');
  let kind = 'polygon', note = 'Exact incident boundary.', local;
  if (polys.length) {
    local = polys.map((p) => p.map((ring) => ring.map(([lon, lat]) => { const q = project(lon, lat, center); return [q.x, q.z]; })));
  } else {
    kind = 'synthetic';
    note = 'Synthetic boundary generated from point incident.';
    const rnd = mulberry32(hashString(String(incidentId)) ^ 0x9e3779b9);
    const R = opts.radiusM || 320, n = 44, ring = [];
    const lobes = [[2 + Math.floor(rnd() * 3), 0.12 + rnd() * 0.1, rnd() * 6.28], [5 + Math.floor(rnd() * 3), 0.05 + rnd() * 0.05, rnd() * 6.28]];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      const r = R * (1 + lobes.reduce((t, [f, amp, ph]) => t + amp * Math.sin(f * a + ph), 0));
      ring.push([Math.cos(a) * r, Math.sin(a) * r]);
    }
    ring.push(ring[0]);
    local = [[ring]];
  }
  const xs = [], zs = [];
  local.forEach((p) => p[0].forEach(([x, z]) => { xs.push(x); zs.push(z); }));
  const bbox = { minX: Math.min(...xs), maxX: Math.max(...xs), minZ: Math.min(...zs), maxZ: Math.max(...zs) };
  return { kind, polygons: local, center, note, bbox, areaHa: areaHectares(local) };
}

function ringArea(r) { let a = 0; for (let i = 0; i < r.length - 1; i++) a += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]; return a / 2; }
/** Area in hectares of local polygons (holes subtracted). */
export function areaHectares(polys) {
  return polys.reduce((t, p) => t + Math.abs(ringArea(p[0])) - p.slice(1).reduce((h, r) => h + Math.abs(ringArea(r)), 0), 0) / 1e4;
}

function inRing(x, z, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, zi] = ring[i], [xj, zj] = ring[j];
    if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}
/** Is the point inside any polygon (and outside that polygon's holes)? Works for Polygon and MultiPolygon shapes. */
export function pointInPolygons(x, z, polys) {
  for (const p of polys) {
    if (!inRing(x, z, p[0])) continue;
    let hole = false;
    for (let h = 1; h < p.length; h++) if (inRing(x, z, p[h])) { hole = true; break; }
    if (!hole) return true;
  }
  return false;
}

/**
 * Simulated tree positions inside the boundary: a jittered grid so the forest looks natural but never overlaps the edge.
 * About `perHa` trees per hectare, never more than `max`. Deterministic for the same incident id.
 */
export function generateTrees(boundary, incidentId, { perHa = 260, max = 9000, min = 160 } = {}) {
  const { bbox, polygons, areaHa } = boundary;
  const want = Math.max(min, Math.min(max, Math.round(areaHa * perHa)));
  const area = Math.max(areaHa * 1e4, 1);
  const spacing = Math.max(1.5, Math.sqrt(area / want));
  const rnd = mulberry32(hashString(String(incidentId)) ^ 0x51ed270b);
  const trees = [];
  for (let z = bbox.minZ + spacing / 2; z < bbox.maxZ; z += spacing) {
    for (let x = bbox.minX + spacing / 2; x < bbox.maxX; x += spacing) {
      const px = x + (rnd() - 0.5) * spacing * 0.85, pz = z + (rnd() - 0.5) * spacing * 0.85;
      const s = 0.75 + rnd() * 0.6, d = rnd(), species = rnd() < 0.7 ? 0 : 1;
      if (pointInPolygons(px, pz, polygons)) trees.push({ x: px, z: pz, s, d, species });
      else rnd();
    }
  }
  return { trees, spacing, wanted: want };
}

// The incident simulation: when each simulated tree burns or is cleared. A deterministic wavefront over a grid of cells.
// This is an ILLUSTRATION built from the incident's boundary. It is not a fire-spread model, not a forecast, and not evidence.
import { hashString, mulberry32, pointInPolygons } from './geo.js';

export const STATE = { HEALTHY: 0, BURNING: 1, BURNED: 2, CLEARED: 3 };
export const SCENARIOS = {
  deforestation: { id: 'deforestation', label: 'Deforestation', banner: 'SIMULATED DEFORESTATION — NOT AN OBSERVATION', seconds: 26 },
  fire: { id: 'fire', label: 'Fire spread', banner: 'SIMULATED FIRE SPREAD — NOT A FORECAST', seconds: 30 },
  combined: { id: 'combined', label: 'Combined', banner: 'SIMULATED FIRE, THEN CLEARING — NOT AN OBSERVATION OR A FORECAST', seconds: 42 },
};
export const WIND_DIRECTIONS = [['N', 0], ['NE', 45], ['E', 90], ['SE', 135], ['S', 180], ['SW', 225], ['W', 270], ['NW', 315]];
export const WIND_STRENGTH = { calm: 0, moderate: 0.4, strong: 0.7 };

const NEIGH = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];

/** Turn arrival times into 0..1 progress that advances steadily: mostly by rank (so the covered area grows evenly), a little by distance. Order is kept. */
function evenly(arr, keep) {
  const idx = []; for (let i = 0; i < arr.length; i++) if (keep(i)) idx.push(i);
  idx.sort((a, b) => arr[a] - arr[b]);
  const out = new Float32Array(arr.length).fill(Infinity), mx = idx.length ? arr[idx[idx.length - 1]] : 1;
  idx.forEach((c, r) => { out[c] = 0.75 * (idx.length > 1 ? r / (idx.length - 1) : 0) + 0.25 * (arr[c] / Math.max(mx, 1e-6)); });
  return out;
}

class Heap {   // tiny binary heap for the wavefront
  constructor() { this.a = []; }
  push(k, v) { const a = this.a; a.push([k, v]); let i = a.length - 1; while (i > 0) { const p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break; [a[p], a[i]] = [a[i], a[p]]; i = p; } }
  pop() { const a = this.a; if (!a.length) return null; const top = a[0], last = a.pop(); if (a.length) { a[0] = last; let i = 0; for (;;) { let l = 2 * i + 1, r = l + 1, m = i; if (l < a.length && a[l][0] < a[m][0]) m = l; if (r < a.length && a[r][0] < a[m][0]) m = r; if (m === i) break; [a[m], a[i]] = [a[i], a[m]]; i = m; } } return top; }
  get size() { return this.a.length; }
}

/** Build the cell grid over the boundary and mark which cells are part of the forest. */
function makeGrid(boundary, trees, spacing) {
  const { bbox, polygons } = boundary;
  const w = bbox.maxX - bbox.minX, h = bbox.maxZ - bbox.minZ;
  const cell = Math.max(spacing * 3, Math.max(w, h) / 80, 2);
  const gw = Math.max(2, Math.ceil(w / cell)), gh = Math.max(2, Math.ceil(h / cell));
  const inside = new Uint8Array(gw * gh), count = new Uint16Array(gw * gh);
  const cellOf = (x, z) => Math.min(gh - 1, Math.max(0, Math.floor((z - bbox.minZ) / cell))) * gw + Math.min(gw - 1, Math.max(0, Math.floor((x - bbox.minX) / cell)));
  trees.forEach((t) => { const c = cellOf(t.x, t.z); count[c]++; t.cell = c; inside[c] = 1; });
  for (let r = 0; r < gh; r++) for (let q = 0; q < gw; q++) {
    const i = r * gw + q;
    if (!inside[i] && pointInPolygons(bbox.minX + (q + 0.5) * cell, bbox.minZ + (r + 0.5) * cell, polygons)) inside[i] = 1;
  }
  return { gw, gh, cell, inside, count, minX: bbox.minX, minZ: bbox.minZ, cx: (q) => bbox.minX + (q + 0.5) * cell, cz: (r) => bbox.minZ + (r + 0.5) * cell };
}

/** Arrival time of a wavefront at every inside cell (Infinity where it cannot reach). `wind`: {rad (direction it blows towards), k (0..0.8)} or null. */
function wave(grid, seeds, rnd, noise, wind) {
  const { gw, gh, inside } = grid;
  const t = new Float32Array(gw * gh).fill(Infinity), heap = new Heap();
  seeds.forEach(([c, t0]) => { if (inside[c] && t0 < t[c]) { t[c] = t0; heap.push(t0, c); } });
  while (heap.size) {
    const [d, c] = heap.pop();
    if (d > t[c]) continue;
    const q = c % gw, r = (c / gw) | 0;
    for (const [dq, dr] of NEIGH) {
      const nq = q + dq, nr = r + dr;
      if (nq < 0 || nr < 0 || nq >= gw || nr >= gh) continue;
      const n = nr * gw + nq;
      if (!inside[n]) continue;
      const len = dq && dr ? 1.414 : 1;
      let cost = len * (0.75 + 0.5 * (noise[c] + noise[n]) / 2);
      if (wind && wind.k > 0) { const ang = Math.atan2(dr, dq) - (wind.rad - Math.PI / 2); cost *= Math.min(1.7, Math.max(0.3, 1 - wind.k * Math.cos(ang) * 1.2)); }
      const nd = d + cost;
      if (nd < t[n]) { t[n] = nd; heap.push(nd, n); }
    }
  }
  return t;
}

/** Make sure every inside cell is reachable (a MultiPolygon can have parts the wave cannot cross): start a late spot in each missing part. */
function reachAll(grid, t, rnd, noise, wind, lateAt) {
  const { gw, gh, inside } = grid;
  for (let c = 0; c < gw * gh; c++) {
    if (inside[c] && !Number.isFinite(t[c])) {
      const extra = wave(grid, [[c, lateAt]], rnd, noise, wind);
      for (let i = 0; i < t.length; i++) if (extra[i] < t[i]) t[i] = extra[i];
    }
  }
}

const nearestInside = (grid, x, z) => {
  const { gw, gh, inside, minX, minZ, cell } = grid;
  const q0 = Math.min(gw - 1, Math.max(0, Math.floor((x - minX) / cell))), r0 = Math.min(gh - 1, Math.max(0, Math.floor((z - minZ) / cell)));
  let best = -1, bd = Infinity;
  for (let r = 0; r < gh; r++) for (let q = 0; q < gw; q++) { const i = r * gw + q; if (!inside[i]) continue; const d = (q - q0) ** 2 + (r - r0) ** 2; if (d < bd) { bd = d; best = i; } }
  return best;
};

/**
 * Build a simulation. Returns an object to pass to stateAt().
 * opts: { boundary, trees, spacing, incidentId, scenario, wind: {deg, strength}, clearAfterFire }
 */
export function buildSim({ boundary, trees, spacing, incidentId, scenario, wind = { deg: 90, strength: 0.4 }, clearAfterFire = true }) {
  const seed = hashString(String(incidentId)) ^ hashString(scenario);
  const rnd = mulberry32(seed);
  const grid = makeGrid(boundary, trees, spacing);
  const n = grid.gw * grid.gh;
  const noise = new Float32Array(n); for (let i = 0; i < n; i++) noise[i] = rnd();
  const wv = { rad: (wind.deg * Math.PI) / 180, k: wind.strength };
  const cx = (boundary.bbox.minX + boundary.bbox.maxX) / 2, cz = (boundary.bbox.minZ + boundary.bbox.maxZ) / 2;
  const ext = Math.max(boundary.bbox.maxX - boundary.bbox.minX, boundary.bbox.maxZ - boundary.bbox.minZ);
  const sim = { scenario, grid, trees, boundary, wind: wv, n, ignitions: [], noise, doFire: scenario !== 'deforestation', doClear: scenario !== 'fire' && (scenario !== 'combined' || clearAfterFire) };
  const tf = new Float32Array(n).fill(Infinity), tc = new Float32Array(n).fill(Infinity);   // fire arrival, clearing arrival (relative)
  const fireLimit = new Float32Array(1), burnCell = new Uint8Array(n);

  if (sim.doFire) {
    // ignition: a point upwind of the centre (so the wind carries the fire across the forest), plus a second one in large areas
    const ux = -Math.sin(wv.rad) * ext * 0.22 * (wv.k > 0 ? 1 : 0), uz = Math.cos(wv.rad) * ext * 0.22 * (wv.k > 0 ? 1 : 0);
    const a = nearestInside(grid, cx + ux + (rnd() - 0.5) * ext * 0.1, cz + uz + (rnd() - 0.5) * ext * 0.1);
    const seeds = [[a, 0]]; sim.ignitions.push(a);
    if (boundary.areaHa > 6) { const b = nearestInside(grid, cx + (rnd() - 0.5) * ext * 0.7, cz + (rnd() - 0.5) * ext * 0.7); if (b !== a) { seeds.push([b, 3 + rnd() * 3]); sim.ignitions.push(b); } }
    const arr = wave(grid, seeds, rnd, noise, wv); reachAll(grid, arr, rnd, noise, wv, 8 + rnd() * 6);
    // the fire is "contained" after reaching part of the forest, so the end state shows both burned and unburned ground
    const finite = []; for (let i = 0; i < n; i++) if (Number.isFinite(arr[i])) finite.push(arr[i]);
    finite.sort((x, y) => x - y);
    const frac = scenario === 'combined' ? 0.42 : 0.72;
    const lim = finite[Math.max(0, Math.min(finite.length - 1, Math.floor(finite.length * frac)))] || 1;
    fireLimit[0] = lim;
    const prog = evenly(arr, (i) => Number.isFinite(arr[i]) && arr[i] <= lim);
    for (let i = 0; i < n; i++) if (Number.isFinite(prog[i])) { tf[i] = prog[i]; burnCell[i] = 1; }
  }
  if (sim.doClear) {
    let seeds;
    if (scenario === 'combined') {   // clearing starts at the edge of the burned ground
      seeds = [];
      for (let r = 0; r < grid.gh; r++) for (let q = 0; q < grid.gw; q++) {
        const i = r * grid.gw + q;
        if (!grid.inside[i] || burnCell[i]) continue;
        if (NEIGH.some(([dq, dr]) => { const nq = q + dq, nr = r + dr; return nq >= 0 && nr >= 0 && nq < grid.gw && nr < grid.gh && burnCell[nr * grid.gw + nq]; })) seeds.push([i, rnd() * 1.5]);
      }
      if (!seeds.length) seeds = [[nearestInside(grid, cx, cz), 0]];
    } else {
      seeds = [[nearestInside(grid, cx + (rnd() - 0.5) * ext * 0.1, cz + (rnd() - 0.5) * ext * 0.1), 0]];
      if (boundary.areaHa > 4) seeds.push([nearestInside(grid, cx + (rnd() - 0.5) * ext * 0.6, cz + (rnd() - 0.5) * ext * 0.6), 4 + rnd() * 4]);
    }
    const arr = wave(grid, seeds, rnd, noise, null); reachAll(grid, arr, rnd, noise, null, 10);
    const prog = evenly(arr, (i) => Number.isFinite(arr[i]) && (scenario !== 'combined' || !burnCell[i]));
    for (let i = 0; i < n; i++) if (Number.isFinite(prog[i])) tc[i] = prog[i];
  }
  sim.tf = tf; sim.tc = tc; sim.burnCell = burnCell;
  // when each phase happens on the 0..1 timeline
  sim.phase = scenario === 'fire' ? { f0: 0.03, f1: 0.82, burn: 0.16 } : scenario === 'combined' ? { f0: 0.03, f1: 0.52, burn: 0.11, c0: 0.6, c1: 0.97 } : { c0: 0.04, c1: 0.94 };
  sim.fell = 0.05;   // how long a tree takes to fall
  // per-tree event times
  const nt = trees.length;
  sim.treeFire = new Float32Array(nt).fill(Infinity); sim.treeClear = new Float32Array(nt).fill(Infinity);
  trees.forEach((t, i) => {
    const jit = t.d * 0.02;
    if (sim.doFire && Number.isFinite(tf[t.cell])) sim.treeFire[i] = sim.phase.f0 + tf[t.cell] * (sim.phase.f1 - sim.phase.f0) + jit;
    if (sim.doClear && Number.isFinite(tc[t.cell]) && !(scenario === 'combined' && burnCell[t.cell])) sim.treeClear[i] = sim.phase.c0 + tc[t.cell] * (sim.phase.c1 - sim.phase.c0) + jit;
  });
  sim.cellFire = new Float32Array(n).fill(Infinity); sim.cellClear = new Float32Array(n).fill(Infinity);
  for (let i = 0; i < n; i++) {
    if (Number.isFinite(tf[i])) sim.cellFire[i] = sim.phase.f0 + tf[i] * (sim.phase.f1 - sim.phase.f0);
    if (Number.isFinite(tc[i])) sim.cellClear[i] = sim.phase.c0 + tc[i] * (sim.phase.c1 - sim.phase.c0);
  }
  sim.insideCells = grid.inside.reduce((s, v) => s + v, 0);
  return sim;
}

/** State of one tree at time t (0..1): { s: STATE, p: progress inside that state 0..1 }. */
export function treeStateAt(sim, i, t) {
  const f = sim.treeFire[i], c = sim.treeClear[i];
  if (Number.isFinite(c) && t >= c) return { s: STATE.CLEARED, p: Math.min(1, (t - c) / sim.fell) };
  if (Number.isFinite(f) && t >= f) {
    const burnEnd = f + sim.phase.burn;
    return t < burnEnd ? { s: STATE.BURNING, p: (t - f) / sim.phase.burn } : { s: STATE.BURNED, p: 1 };
  }
  return { s: STATE.HEALTHY, p: 0 };
}

/** Fill `out` ({ state: Uint8Array, prog: Float32Array }) for every tree and return running totals for the labels. */
export function stateAt(sim, t, out) {
  let healthy = 0, burning = 0, burned = 0, cleared = 0;
  for (let i = 0; i < sim.trees.length; i++) {
    const r = treeStateAt(sim, i, t);
    out.state[i] = r.s; out.prog[i] = r.p;
    if (r.s === STATE.HEALTHY) healthy++; else if (r.s === STATE.BURNING) burning++; else if (r.s === STATE.BURNED) burned++; else if (r.s === STATE.CLEARED) cleared++;
  }
  const total = Math.max(1, sim.trees.length);
  return { healthy, burning, burned, cleared, total, clearedPct: Math.round((100 * cleared) / total), burnedPct: Math.round((100 * (burned + burning)) / total) };
}

/** Ground colour state of a cell at time t: 0 healthy, 1 burning, 2 burned, 3 cleared (-1 outside the forest). */
export function cellStateAt(sim, c, t) {
  if (!sim.grid.inside[c]) return -1;
  const cf = sim.cellFire[c], cc = sim.cellClear[c];
  if (Number.isFinite(cc) && t >= cc) return 3;
  if (Number.isFinite(cf) && t >= cf) return t < cf + sim.phase.burn ? 1 : 2;
  return 0;
}

/** Edges of the burning region at time t, as a flat [x1, z1, x2, z2, ...] list (the simulated fire perimeter). */
export function perimeterAt(sim, t) {
  if (!sim.doFire) return [];
  const { gw, gh, inside, cell, minX, minZ } = sim.grid, out = [];
  const lit = (c) => Number.isFinite(sim.cellFire[c]) && t >= sim.cellFire[c] && t < sim.cellFire[c] + sim.phase.burn * 1.4;
  const ignited = (c) => Number.isFinite(sim.cellFire[c]) && t >= sim.cellFire[c];
  for (let r = 0; r < gh; r++) for (let q = 0; q < gw; q++) {
    const c = r * gw + q; if (!inside[c] || !ignited(c)) continue;
    const x0 = minX + q * cell, z0 = minZ + r * cell;
    const edge = (nq, nr, x1, z1, x2, z2) => {
      if (nq < 0 || nr < 0 || nq >= gw || nr >= gh) return;
      const n = nr * gw + nq;
      if (inside[n] && !ignited(n) && lit(c)) out.push(x1, z1, x2, z2);
    };
    edge(q + 1, r, x0 + cell, z0, x0 + cell, z0 + cell); edge(q - 1, r, x0, z0, x0, z0 + cell);
    edge(q, r + 1, x0, z0 + cell, x0 + cell, z0 + cell); edge(q, r - 1, x0, z0, x0 + cell, z0);
  }
  return out;
}

export const progressText = (sim, stats, t) => {
  if (sim.scenario === 'deforestation') return `Simulated clearing: ${stats.clearedPct}%`;
  if (sim.scenario === 'fire') return `Simulated fire: ${stats.burnedPct}% of trees burning or burned`;
  return t < sim.phase.c0 ? `Simulated fire: ${stats.burnedPct}% of trees burning or burned` : `Simulated clearing after the fire: ${stats.clearedPct}% cleared`;
};

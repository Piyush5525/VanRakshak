// Forest Explorer, incident simulation mode.
// A synthetic 3D forest is built inside the selected incident's boundary and can be played through a deforestation, a fire-spread or a
// combined scenario. EVERYTHING here is simulated: it is not mapping, not satellite evidence, not a forecast and not a legal finding.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { html, raw, esc, tag, toast, $, $$ } from '../core.js';
import { boundaryFor, generateTrees, unproject, mulberry32, hashString } from '../sim/geo.js';
import { SCENARIOS, WIND_DIRECTIONS, WIND_STRENGTH, STATE, buildSim, stateAt, cellStateAt, perimeterAt, progressText } from '../sim/engine.js';

export const DISCLAIMER = 'This is a browser-generated simulation based on the selected incident geometry. It is not real mapping, satellite evidence, a forecast, or a legal determination.';
const D = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'not recorded');
const GEOM_LABEL = { Polygon: 'Polygon', MultiPolygon: 'MultiPolygon', Point: 'Point', LineString: 'Line', MultiPoint: 'Multiple points' };

/** Small particle system: smoke, embers, dust. Particles are recycled; nothing is allocated per frame. */
class Emitter {
  constructor(THREE_, n, material, rnd) {
    this.n = n; this.rnd = rnd; this.pos = new Float32Array(n * 3).fill(-1e6); this.vel = new Float32Array(n * 3); this.life = new Float32Array(n); this.max = new Float32Array(n).fill(1); this.next = 0;
    const g = new THREE_.BufferGeometry(); g.setAttribute('position', new THREE_.BufferAttribute(this.pos, 3)); this.points = new THREE_.Points(g, material); this.points.frustumCulled = false;
  }
  spawn(x, y, z, vx, vy, vz, life) { const i = this.next; this.next = (this.next + 1) % this.n; this.pos.set([x, y, z], i * 3); this.vel.set([vx, vy, vz], i * 3); this.life[i] = life; this.max[i] = life; }
  step(dt) {
    for (let i = 0; i < this.n; i++) {
      if (this.life[i] <= 0) { this.pos[i * 3 + 1] = -1e6; continue; }
      this.life[i] -= dt; this.pos[i * 3] += this.vel[i * 3] * dt; this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt; this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt;
    }
    this.points.geometry.attributes.position.needsUpdate = true;
  }
  clear() { this.life.fill(0); this.pos.fill(-1e6); this.points.geometry.attributes.position.needsUpdate = true; }
}

export async function renderIncident(root, src) {
  const bnd = boundaryFor(src.geometry, src.id);
  const gen = generateTrees(bnd, src.id);
  const trees = gen.trees;
  const ext = Math.max(bnd.bbox.maxX - bnd.bbox.minX, bnd.bbox.maxZ - bnd.bbox.minZ);
  const treeH = Math.min(40, Math.max(5, gen.spacing * 2.6));
  const cx = (bnd.bbox.minX + bnd.bbox.maxX) / 2, cz = (bnd.bbox.minZ + bnd.bbox.maxZ) / 2;
  const st = { scenario: src.defaultScenario, windDeg: 90, windKey: 'moderate', clearAfter: true, speed: 1, t: 0, playing: false, dirty: true, sim: null, stats: null, trees: true, perim: true, parts: true };
  let disposed = false, tickAt = 0, statTxt = '';

  root.innerHTML = html`
    <div class="note-box"><span class="mode-chip sim" id="modeChip">Incident simulation mode</span> <b>Simulated forest.</b> ${DISCLAIMER}</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel" style="padding:12px">
        <div class="ar-stage" id="stage" role="img" aria-label="3D simulated forest inside the incident boundary">
          <div class="ar-hud"><div class="ar-label" id="lab"></div><div class="ar-card" id="info">Drag to turn, scroll to zoom. Click the ground to inspect a spot.</div></div>
          <div class="ar-hud" style="top:auto;bottom:12px"><div class="ar-card" id="prog" aria-live="off"></div></div>
        </div>
        <div class="sim-legend"><span><i style="background:#2f7d4f"></i>Healthy</span><span><i style="background:#ff7a1a"></i>Burning</span><span><i style="background:#2a2622"></i>Burned</span><span><i style="background:#8a6a45"></i>Cleared</span><span><i class="ln"></i>Incident boundary</span></div>
        <div class="row" style="margin-top:8px"><input type="range" id="tl" min="0" max="1000" value="0" step="1" class="grow" aria-label="Simulation time"><span class="help" id="tlLab">0%</span></div>
      </div>
      <div class="stack">
        <div class="panel"><h4>Incident</h4><div class="stack" id="incPanel"></div></div>
        <div class="panel"><h4>Scenario</h4><div class="stack">
          <label class="f">Simulate<select id="scn">${raw(Object.values(SCENARIOS).map((s) => `<option value="${s.id}" ${s.id === st.scenario ? 'selected' : ''}>${esc(s.label)}</option>`).join(''))}</select></label>
          <div class="help" id="scnHelp"></div>
          <div id="windBox" class="grid g2"><label class="f">Wind blows towards<select id="wdir">${raw(WIND_DIRECTIONS.map(([k, d]) => `<option value="${d}" ${d === st.windDeg ? 'selected' : ''}>${k}</option>`).join(''))}</select></label>
            <label class="f">Wind strength<select id="wstr">${raw(Object.keys(WIND_STRENGTH).map((k) => `<option value="${k}" ${k === st.windKey ? 'selected' : ''}>${k[0].toUpperCase() + k.slice(1)}</option>`).join(''))}</select></label></div>
          <label id="clrBox" class="check" style="display:none"><input type="checkbox" id="clrAfter" checked> Add clearing after the fire</label></div></div>
        <div class="panel"><h4>Simulation controls</h4><div class="stack">
          <div class="row"><button class="button primary sm" id="play">Play</button><button class="button subtle sm" id="pause" disabled>Pause</button><button class="button subtle sm" id="reset">Reset</button><button class="button subtle sm" id="restart">Restart</button></div>
          <div class="row"><label class="help">Speed <select id="spd"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select></label></div>
          <div class="help" id="counts" role="status"></div></div></div>
        <div class="panel"><h4>View</h4><div class="stack"><div class="row"><label><input type="checkbox" id="vTrees" checked> Trees</label><label><input type="checkbox" id="vPerim" checked> Fire perimeter</label><label><input type="checkbox" id="vParts" checked> Smoke and dust</label></div>
          <div class="row"><button class="button subtle sm" id="cam">Reset camera</button><a class="button subtle sm" href="#/ar-explorer">Region analysis</a>${src.backHref ? raw(`<a class="button subtle sm" href="${esc(src.backHref)}">${esc(src.backLabel || 'Back to the incident')}</a>`) : ''}</div></div></div>
        <div class="panel"><div class="warn-box"><b>Not real.</b> ${DISCLAIMER}</div></div>
      </div>
    </div>`;

  $('#incPanel').innerHTML = html`<div class="row"><b class="grow">${src.title}</b>${raw(tag(src.statusLabel || src.status || 'Unknown', 'configure').s)}</div>
    <dl class="kv"><dt>Type</dt><dd>${src.typeLabel}</dd><dt>Geometry</dt><dd>${GEOM_LABEL[src.geometry?.type] || src.geometry?.type || 'unknown'}</dd><dt>Created</dt><dd>${D(src.createdAt)}</dd><dt>Boundary</dt><dd>${bnd.note}</dd><dt>Area</dt><dd>${bnd.areaHa < 10 ? bnd.areaHa.toFixed(2) : bnd.areaHa.toFixed(1)} ha${bnd.kind === 'synthetic' ? ' (synthetic)' : ''}</dd><dt>Simulated trees</dt><dd>${trees.length.toLocaleString()}</dd></dl>
    ${raw(bnd.kind === 'synthetic' ? '<div class="help">Synthetic boundary generated from point incident.</div>' : '')}`;

  // ------------------------------------------------------------ three.js scene
  const stage = $('#stage');
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.setClearColor(0x13241c, 1);
  stage.prepend(renderer.domElement);
  const camera = new THREE.PerspectiveCamera(50, 1, 1, Math.max(5000, ext * 12));
  const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.49;
  const world = new THREE.Scene(); world.fog = new THREE.Fog(0x13241c, ext * 2.2, ext * 7);
  const sun = new THREE.DirectionalLight(0xffffff, 2.2); sun.position.set(-ext, ext * 1.6, ext * 0.8); world.add(sun, new THREE.HemisphereLight(0xcfe8ff, 0x3a4a30, 1.1));
  const grp = new THREE.Group(); world.add(grp);
  const home = () => { camera.position.set(cx + ext * 0.42, ext * 0.5, cz + ext * 0.66); controls.target.set(cx, 0, cz); controls.update(); };
  home();
  const resize = () => { const w = stage.clientWidth, h = stage.clientHeight; renderer.setSize(w, h); camera.aspect = w / Math.max(h, 1); camera.updateProjectionMatrix(); };
  new ResizeObserver(resize).observe(stage); resize();

  // floor: the exact boundary shape, coloured cell by cell from the simulation
  const sim0 = buildSim({ boundary: bnd, trees, spacing: gen.spacing, incidentId: src.id, scenario: st.scenario, wind: { deg: st.windDeg, strength: WIND_STRENGTH[st.windKey] }, clearAfterFire: st.clearAfter });
  const gw = sim0.grid.gw, gh = sim0.grid.gh, gtex = new Uint8Array(gw * gh * 4);
  const tex = new THREE.DataTexture(gtex, gw, gh, THREE.RGBAFormat); tex.magFilter = THREE.LinearFilter; tex.minFilter = THREE.LinearFilter; tex.colorSpace = THREE.SRGBColorSpace; tex.needsUpdate = true;
  const floorMat = new THREE.MeshLambertMaterial({ map: tex });
  const W = bnd.bbox.maxX - bnd.bbox.minX, H = bnd.bbox.maxZ - bnd.bbox.minZ;
  bnd.polygons.forEach((poly) => {
    const shape = new THREE.Shape(poly[0].map(([x, z]) => new THREE.Vector2(x, -z)));
    poly.slice(1).forEach((r) => shape.holes.push(new THREE.Path(r.map(([x, z]) => new THREE.Vector2(x, -z)))));
    const g = new THREE.ShapeGeometry(shape); const p = g.getAttribute('position'), uv = new Float32Array(p.count * 2);
    for (let i = 0; i < p.count; i++) { uv[i * 2] = (p.getX(i) - bnd.bbox.minX) / W; uv[i * 2 + 1] = (-p.getY(i) - bnd.bbox.minZ) / H; }
    g.setAttribute('uv', new THREE.BufferAttribute(uv, 2)); g.rotateX(-Math.PI / 2);
    const m = new THREE.Mesh(g, floorMat); m.userData.floor = true; grp.add(m);
  });
  // the boundary: a bright outline on the ground plus a faint curtain so it reads from any angle
  const lineMat = new THREE.LineBasicMaterial({ color: 0xffe28a }), curtainMat = new THREE.MeshBasicMaterial({ color: 0xffe28a, transparent: true, opacity: 0.1, side: THREE.DoubleSide, depthWrite: false });
  bnd.polygons.forEach((poly) => poly.forEach((ring) => {
    const pts = ring.map(([x, z]) => new THREE.Vector3(x, 0.8, z)); grp.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat));
    const pos = [], ch = treeH * 1.2;
    for (let i = 0; i < ring.length - 1; i++) { const [x1, z1] = ring[i], [x2, z2] = ring[i + 1]; pos.push(x1, 0, z1, x2, 0, z2, x2, ch, z2, x1, 0, z1, x2, ch, z2, x1, ch, z1); }
    const cg = new THREE.BufferGeometry(); cg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); grp.add(new THREE.Mesh(cg, curtainMat));
  }));

  // trees: trunks and canopies as instanced meshes (a few draw calls for thousands of trees)
  const trunkGeo = new THREE.CylinderGeometry(0.05, 0.075, 0.28, 6); trunkGeo.translate(0, 0.14, 0);
  const c1 = new THREE.ConeGeometry(0.34, 0.62, 7); c1.translate(0, 0.52, 0);
  const c2 = new THREE.ConeGeometry(0.24, 0.52, 7); c2.translate(0, 0.86, 0);
  const crownGeo = merge([c1, c2]);
  const nT = Math.max(1, trees.length);
  const trunkMesh = new THREE.InstancedMesh(trunkGeo, new THREE.MeshLambertMaterial({ color: 0xffffff }), nT), crownMesh = new THREE.InstancedMesh(crownGeo, new THREE.MeshLambertMaterial({ color: 0xffffff }), nT);
  [trunkMesh, crownMesh].forEach((m) => { m.instanceMatrix.setUsage(THREE.DynamicDrawUsage); m.frustumCulled = false; grp.add(m); });
  const out = { state: new Uint8Array(nT), prog: new Float32Array(nT) };
  const col = new THREE.Color(), tcol = new THREE.Color(), dummy = new THREE.Object3D();

  // fire perimeter, ignition marks, particles
  const maxSeg = gw * gh * 4;
  const perim = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xffb02e }));
  perim.geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(maxSeg * 6), 3)); perim.geometry.setDrawRange(0, 0); perim.frustumCulled = false; grp.add(perim);
  const rnd = mulberry32(hashString(src.id) ^ 0xc0ffee);
  const smoke = new Emitter(THREE, 900, new THREE.PointsMaterial({ color: 0x4a4a4a, size: treeH * 1.2, transparent: true, opacity: 0.32, depthWrite: false }), rnd);
  const embers = new Emitter(THREE, 500, new THREE.PointsMaterial({ color: 0xff9a3c, size: Math.max(1.5, treeH * 0.2), transparent: true, opacity: 0.95, depthWrite: false, blending: THREE.AdditiveBlending }), rnd);
  const dust = new Emitter(THREE, 500, new THREE.PointsMaterial({ color: 0xa9835a, size: treeH * 0.7, transparent: true, opacity: 0.4, depthWrite: false }), rnd);
  grp.add(smoke.points, embers.points, dust.points);
  const marks = new THREE.Group(); grp.add(marks);

  function merge(gs) { const pos = [], nor = [], idx = []; let off = 0; gs.forEach((g) => { const p = g.getAttribute('position'), n = g.getAttribute('normal'); for (let i = 0; i < p.count; i++) { pos.push(p.getX(i), p.getY(i), p.getZ(i)); nor.push(n.getX(i), n.getY(i), n.getZ(i)); } if (g.index) for (let i = 0; i < g.index.count; i++) idx.push(g.index.getX(i) + off); else for (let i = 0; i < p.count; i++) idx.push(i + off); off += p.count; }); const m = new THREE.BufferGeometry(); m.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); m.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3)); m.setIndex(idx); return m; }

  // ------------------------------------------------------------ simulation
  function rebuild() {
    st.sim = buildSim({ boundary: bnd, trees, spacing: gen.spacing, incidentId: src.id, scenario: st.scenario, wind: { deg: st.windDeg, strength: WIND_STRENGTH[st.windKey] }, clearAfterFire: st.clearAfter });
    st.t = 0; st.playing = false; st.dirty = true; [smoke, embers, dust].forEach((e) => e.clear());
    marks.clear();
    st.sim.ignitions.forEach((c) => { const m = new THREE.Mesh(new THREE.ConeGeometry(treeH * 0.35, treeH * 1.2, 8), new THREE.MeshBasicMaterial({ color: 0xffd23c })); m.position.set(st.sim.grid.cx(c % gw), treeH * 0.6, st.sim.grid.cz((c / gw) | 0)); m.userData.ignition = true; marks.add(m); });
    const sc = SCENARIOS[st.scenario];
    $('#lab').textContent = sc.banner; $('#lab').style.background = 'rgba(180,100,10,.92)';
    $('#windBox').style.display = st.scenario === 'deforestation' ? 'none' : '';
    $('#clrBox').style.display = st.scenario === 'combined' ? '' : 'none';
    $('#scnHelp').textContent = st.scenario === 'deforestation' ? 'Clearing spreads out from the middle of the boundary in a steady, irregular wave.' : st.scenario === 'fire' ? 'Fire starts at a simulated ignition point and spreads with the wind you choose until the simulation ends. Invented, not a forecast.' : 'Fire first, leaving burned ground; then clearing of the forest that is left.';
    syncButtons(); paintFrame(true);
  }

  const HEALTHY = [new THREE.Color(0x2f7d4f), new THREE.Color(0x3a8a3c)];
  function paintFrame(force) {
    const sim = st.sim, t = st.t, time = performance.now() / 1000;
    const s = stateAt(sim, t, out); st.stats = s;
    const show = st.trees;
    const burning = [];
    for (let i = 0; i < trees.length; i++) {
      const tr = trees[i], state = out.state[i], p = out.prog[i], sz = treeH * tr.s * (tr.species ? 1.0 : 0.9);
      let sy = sz, sxz = sz * (tr.species ? 1.25 : 1), tilt = 0, hide = !show;
      if (state === STATE.HEALTHY) { col.copy(HEALTHY[tr.species]).offsetHSL((tr.d - 0.5) * 0.04, 0, (tr.d - 0.5) * 0.08); tcol.set(0x5b3a1e); }
      else if (state === STATE.BURNING) { const pulse = 1 + 0.07 * Math.sin(time * 11 + tr.d * 6.28); col.setRGB(1, 0.28 + 0.35 * (1 - p), 0.05); col.multiplyScalar(0.9 + 0.2 * Math.sin(time * 9 + tr.d * 9)); tcol.set(0x3a1a08); sy *= pulse; sxz *= pulse; burning.push(i); }
      else if (state === STATE.BURNED) { col.set(0x201c19).offsetHSL(0, 0, tr.d * 0.03); tcol.set(0x15110f); sy *= 0.82; sxz *= 0.7; }
      else { const k = 1 - p; col.set(0x6e5a34); tcol.set(0x4a3418); sy *= Math.max(0.001, k); sxz *= Math.max(0.001, k); tilt = p * 1.35; if (p >= 1) hide = true; }
      dummy.position.set(tr.x, 0, tr.z); dummy.rotation.set(tilt * (tr.d - 0.5) * 2, tr.d * 6.28, tilt); dummy.scale.set(hide ? 0.0001 : sxz, hide ? 0.0001 : sy, hide ? 0.0001 : sxz); dummy.updateMatrix();
      crownMesh.setMatrixAt(i, dummy.matrix); trunkMesh.setMatrixAt(i, dummy.matrix); crownMesh.setColorAt(i, col); trunkMesh.setColorAt(i, tcol);
    }
    crownMesh.instanceMatrix.needsUpdate = trunkMesh.instanceMatrix.needsUpdate = true; crownMesh.instanceColor.needsUpdate = trunkMesh.instanceColor.needsUpdate = true;
    // ground, cell by cell
    const g = sim.grid, base = new THREE.Color();
    for (let c = 0; c < gw * gh; c++) {
      const cs = cellStateAt(sim, c, t), n = sim.noise[c];
      if (cs < 0) { gtex[c * 4 + 3] = 0; continue; }
      if (cs === 0) base.setRGB(0.2 + n * 0.06, 0.34 + n * 0.08, 0.17 + n * 0.05);
      else if (cs === 1) base.setRGB(0.9, 0.3 + 0.25 * Math.sin(time * 7 + n * 9) ** 2, 0.06);
      else if (cs === 2) base.setRGB(0.1 + n * 0.04, 0.09 + n * 0.03, 0.08 + n * 0.03);
      else base.setRGB(0.46 + n * 0.1, 0.34 + n * 0.08, 0.2 + n * 0.05);
      base.convertLinearToSRGB(); gtex[c * 4] = base.r * 255; gtex[c * 4 + 1] = base.g * 255; gtex[c * 4 + 2] = base.b * 255; gtex[c * 4 + 3] = 255;
    }
    tex.needsUpdate = true;
    // perimeter of the burning region
    const seg = st.perim && sim.doFire ? perimeterAt(sim, t) : [], pa = perim.geometry.getAttribute('position');
    const nseg = Math.min(seg.length / 4, maxSeg);
    for (let k = 0; k < nseg; k++) { pa.setXYZ(k * 2, seg[k * 4], 1.6, seg[k * 4 + 1]); pa.setXYZ(k * 2 + 1, seg[k * 4 + 2], 1.6, seg[k * 4 + 3]); }
    pa.needsUpdate = true; perim.geometry.setDrawRange(0, nseg * 2); perim.visible = st.perim && nseg > 0;
    marks.visible = sim.doFire && t < 0.55; marks.children.forEach((m) => { m.scale.setScalar(1 + 0.15 * Math.sin(time * 8)); });
    st.burning = burning; st.clearing = s.cleared;
    $('#prog').textContent = progressText(sim, s, t);
    $('#tl').value = Math.round(t * 1000); $('#tlLab').textContent = Math.round(t * 100) + '%';
    if (force || time - tickAt > 0.25) { tickAt = time; const txt = `Trees: ${s.healthy.toLocaleString()} healthy · ${s.burning.toLocaleString()} burning · ${s.burned.toLocaleString()} burned · ${s.cleared.toLocaleString()} cleared (all simulated)`; if (txt !== statTxt) { statTxt = txt; $('#counts').textContent = txt; } }
    st.dirty = false;
  }

  function particles(dt) {
    const on = st.parts;
    smoke.points.visible = embers.points.visible = dust.points.visible = on;
    if (!on) return;
    if (st.playing && st.burning?.length) {
      const n = Math.min(14, 2 + Math.floor(st.burning.length / 120));
      for (let k = 0; k < n; k++) { const tr = trees[st.burning[(rnd() * st.burning.length) | 0]]; smoke.spawn(tr.x, treeH * 0.9, tr.z, (rnd() - 0.3) * 2.5, treeH * (0.6 + rnd() * 0.5), (rnd() - 0.5) * 2.5, 4 + rnd() * 3); if (rnd() < 0.6) embers.spawn(tr.x, treeH * 0.8, tr.z, (rnd() - 0.5) * 6, treeH * (0.9 + rnd()), (rnd() - 0.5) * 6, 1.6 + rnd() * 1.5); }
    }
    if (st.playing && st.sim.doClear && st.clearing > 0 && st.clearing < trees.length) {
      for (let k = 0; k < 6; k++) { const tr = trees[(rnd() * trees.length) | 0], r = treeStateOf(tr); if (r === STATE.CLEARED) dust.spawn(tr.x, 1, tr.z, (rnd() - 0.5) * 3, 1.5 + rnd() * 2, (rnd() - 0.5) * 3, 2.5 + rnd() * 2); }
    }
    smoke.step(dt); embers.step(dt); dust.step(dt);
  }
  const idxOf = new Map(); trees.forEach((tr, i) => idxOf.set(tr, i));
  const treeStateOf = (tr) => out.state[idxOf.get(tr)];

  // ------------------------------------------------------------ controls
  function syncButtons() { $('#play').disabled = st.playing; $('#pause').disabled = !st.playing; }
  const play = () => { if (st.t >= 1) st.t = 0; st.playing = true; syncButtons(); };
  const pause = () => { st.playing = false; syncButtons(); };
  const reset = () => { st.playing = false; st.t = 0; st.dirty = true; [smoke, embers, dust].forEach((e) => e.clear()); syncButtons(); };
  $('#play').onclick = play; $('#pause').onclick = pause; $('#reset').onclick = reset; $('#restart').onclick = () => { reset(); play(); };
  $('#spd').onchange = (e) => { st.speed = +e.target.value; };
  $('#tl').oninput = (e) => { st.t = +e.target.value / 1000; st.playing = false; st.dirty = true; syncButtons(); };
  $('#scn').onchange = (e) => { st.scenario = e.target.value; rebuild(); };
  $('#wdir').onchange = (e) => { st.windDeg = +e.target.value; rebuild(); };
  $('#wstr').onchange = (e) => { st.windKey = e.target.value; rebuild(); };
  $('#clrAfter').onchange = (e) => { st.clearAfter = e.target.checked; rebuild(); };
  $('#vTrees').onchange = (e) => { st.trees = e.target.checked; st.dirty = true; };
  $('#vPerim').onchange = (e) => { st.perim = e.target.checked; st.dirty = true; };
  $('#vParts').onchange = (e) => { st.parts = e.target.checked; };
  $('#cam').onclick = home;

  // click the ground to inspect
  const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
  renderer.domElement.addEventListener('pointerdown', (e) => { renderer.domElement._d = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    const d = renderer.domElement._d; if (!d || Math.hypot(e.clientX - d[0], e.clientY - d[1]) > 5) return;
    const rc = renderer.domElement.getBoundingClientRect(); mouse.set(((e.clientX - rc.left) / rc.width) * 2 - 1, -((e.clientY - rc.top) / rc.height) * 2 + 1);
    ray.setFromCamera(mouse, camera); const hit = ray.intersectObjects(grp.children.filter((o) => o.userData.floor))[0]; if (!hit) return;
    const sim = st.sim, g = sim.grid, q = Math.min(g.gw - 1, Math.max(0, Math.floor((hit.point.x - g.minX) / g.cell))), r = Math.min(g.gh - 1, Math.max(0, Math.floor((hit.point.z - g.minZ) / g.cell)));
    const cs = cellStateAt(sim, r * g.gw + q, st.t), ll = unproject(hit.point.x, hit.point.z, bnd.center);
    const word = ['Simulated healthy forest', 'Simulated burning', 'Simulated burned ground', 'Simulated cleared ground'][cs] || 'Outside the boundary';
    $('#info').innerHTML = html`<b>${word}</b><br>${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}<br>Not observed. Simulation only.`;
  });

  // ------------------------------------------------------------ loop
  let last = performance.now();
  renderer.setAnimationLoop(() => {
    if (disposed) return;
    const now = performance.now(), dt = Math.min(0.1, (now - last) / 1000); last = now;
    if (st.playing) { st.t = Math.min(1, st.t + (dt * st.speed) / SCENARIOS[st.scenario].seconds); if (st.t >= 1) { st.playing = false; syncButtons(); } paintFrame(false); }
    else if (st.dirty) paintFrame(true);
    particles(dt); controls.update(); renderer.render(world, camera);
  });
  rebuild();
  // test hooks and a quick way to look at one moment without waiting
  root.__sim = { st, trees, bnd, gen, setTime: (t) => { st.t = t; st.playing = false; st.dirty = true; syncButtons(); } };
  return { destroy: () => { disposed = true; delete root.__sim; renderer.setAnimationLoop(null); controls.dispose(); renderer.dispose(); grp.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); }); tex.dispose(); } };
}

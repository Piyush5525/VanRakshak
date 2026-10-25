// AR Forest Explorer: real DEM terrain + WorldCover tree-cover density + stored detection timeline.
// Tree markers are SIMULATED placements (the scene says so). Hypothetical scenarios are labelled, never "forecasts".
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { api, html, raw, esc, fmt, tag, empty, toast, can, $, $$ } from '../core.js';
import { deviceReport, deviceHtml } from '../device.js';
import { renderIncident } from './ar-incident.js';

const LAYER_A = 3, LAYER_B = 4; // layers 1/2 are reserved by WebXR for the eyes

const rng = (seed) => () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);

const nice = (v) => String(v || 'unknown').replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase());
const KINDS = { cutting: 'Illegal cutting', fire: 'Forest fire', encroachment: 'Encroachment', pollution: 'Industrial pollution', other: 'Other' };

/**
 * An incident the Forest Explorer can simulate, from a link of the form #/ar-explorer/<id>.
 * <id> is a reported incident. "fire:<id>" is a satellite fire incident and "forest:<id>" is a tree-loss spot from the live map.
 * Returns null when the parameter is not an incident we can find.
 */
export async function resolveIncident(param) {
  const p = decodeURIComponent(param || ''), m = p.match(/^(fire|forest):(.+)$/), id = m ? m[2] : p;
  try {
    if (m?.[1] === 'fire') {
      const i = await api('/fire/incidents/' + encodeURIComponent(id)), place = i.detection?.place?.name;
      return { source: 'fire', id: 'fire:' + i.id, title: `${i.code}${place ? ' · ' + place : ''}`, status: i.status, statusLabel: i.status_label, typeLabel: 'Wildfire (satellite hotspot)', createdAt: i.created_at, geometry: { type: 'Point', coordinates: [i.lon, i.lat] }, defaultScenario: 'fire', backHref: '/live', backLabel: 'Back to the live map' };
    }
    if (m?.[1] === 'forest') {
      const r = await api('/forest-change/records/' + encodeURIComponent(id));
      return { source: 'forest', id: 'forest:' + r.id, title: `${r.code} · ${r.area_ha} ha of tree loss`, status: r.status, statusLabel: nice(r.status), typeLabel: 'Tree loss (satellite candidate)', createdAt: r.created_at, geometry: r.geometry, defaultScenario: 'deforestation', backHref: '/live', backLabel: 'Back to the live map' };
    }
    const i = await api('/incidents/' + encodeURIComponent(id));
    const kind = i.evidence?.kind, type = KINDS[kind] || (i.change_id ? 'Satellite change' : i.wildfire_id ? 'Forest fire' : 'Other');
    return { source: 'incident', id: i.id, title: i.title, status: i.status, statusLabel: nice(i.status), typeLabel: type, createdAt: i.created_at, geometry: i.geometry, defaultScenario: kind === 'fire' || i.wildfire_id ? 'fire' : 'deforestation', backHref: '#/investigations/' + i.id, backLabel: 'Back to the case' };
  } catch (e) { return null; }
}

export async function render(root, { params }) {
  const regions = await api('/regions');
  const wanted = params[0];
  if (wanted && !regions.some((r) => r.id === wanted)) {
    const inc = await resolveIncident(wanted);
    if (inc) return renderIncident(root, inc);
    toast('That incident could not be found. Showing the region view instead.', 'error');
  }
  if (!regions.length) { root.innerHTML = html`<div class="note-box"><span class="mode-chip">Region analysis mode</span></div>` + empty('No regions yet', 'Create a monitoring region and run change detection to build a scene.', '⬡'); return; }
  const dev = await deviceReport();
  let scene3 = null, S = null, raf = 0, xrSession = null, disposed = false;
  root.innerHTML = html`
    <div class="note-box"><span class="mode-chip" id="modeChip">Region analysis mode</span> <b>What you are looking at.</b> Terrain is real (Copernicus DEM). Tree density is the ESA WorldCover 2021 reference map. Red areas are <i>candidate</i> loss from stored detections (unverified). Individual trees are <b>simulated markers</b> placed in proportion to that density, not surveyed trees. Anything drawn as a hypothetical clearing is your assumption, not an observation and not a forecast.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel" style="padding:12px"><div class="ar-stage" id="stage"><div class="ar-hud"><div class="ar-label" id="lab">Loading…</div><div class="ar-card" id="info">Click the terrain to inspect a location.</div></div><div id="hudOverlay" class="ar-hud" style="top:auto;bottom:12px"></div></div>
        <div class="row" style="margin-top:10px"><button class="button subtle sm" id="play">▶ Play</button><button class="button subtle sm" id="reset">⟲ Reset</button>
          <label class="help">Speed <select id="spd"><option value="0.4">0.5×</option><option value="0.8" selected>1×</option><option value="1.6">2×</option></select></label>
          <input type="range" id="tl" min="0" max="0" value="0" step="1" class="grow" aria-label="Timeline"><span class="help" id="tlLab"></span></div></div>
      <div class="stack">
        <div class="panel"><h4>Forest</h4><div class="stack"><label class="f">Region<select id="reg">${raw(regions.map((r) => `<option value="${esc(r.id)}" ${r.id === params[0] ? 'selected' : ''}>${esc(r.name)}</option>`).join(''))}</select></label><div class="help" id="loadMsg"></div></div></div>
        <div class="panel"><h4>Timeline and scenarios</h4><div id="items"></div>
          <div class="help" style="margin-top:6px">Spatial forecast: <span id="fcMsg"></span></div></div>
        <div class="panel"><h4>View</h4><div class="stack"><div class="row"><label><input type="checkbox" id="lTrees" checked> Trees</label><label><input type="checkbox" id="lLoss" checked> Loss</label><label><input type="checkbox" id="lRisk"> Risk</label></div>
          <label class="f">Vertical exaggeration <input type="range" id="exag" min="1" max="4" step="0.5" value="1.5"></label>
          <div class="row"><button class="button subtle sm" id="cmp">Compare with reference</button><button class="button primary sm" id="ar" ${dev.ar ? '' : 'disabled'}>${dev.ar ? 'Enter AR' : 'AR unavailable'}</button></div></div></div>
        <div class="panel"><h4>Hypothetical clearing</h4><p class="sub">Click “Place”, then click the terrain. Creates a labelled scenario circle; it is stored as a simulation, not a prediction.</p>
          <div class="grid g2"><label class="f">Radius m<input type="number" id="hr" value="150" min="30" max="1500"></label><label class="f">Horizon<select id="hh"><option>7</option><option selected>30</option><option>90</option></select></label></div><button class="button subtle" id="place" ${can('admin', 'reviewer') ? '' : 'disabled title="Reviewer or administrator role required"'}>Place on terrain</button></div>
        <div class="panel"><h4>Device compatibility</h4>${deviceHtml(dev)}</div>
      </div>
    </div>`;

  const stage = $('#stage');
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.setScissorTest(true); renderer.setClearColor(0x13241c, 1);
  stage.prepend(renderer.domElement);
  const camera = new THREE.PerspectiveCamera(50, 1, 5, 60000);
  const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.49;
  const world = new THREE.Scene();
  const sun = new THREE.DirectionalLight(0xffffff, 2.2); sun.position.set(-1500, 2500, 1200); const amb = new THREE.HemisphereLight(0xcfe8ff, 0x3a4a30, 1.1);
  [sun, amb].forEach((l) => { l.layers.enable(LAYER_A); l.layers.enable(LAYER_B); l.layers.enable(0); world.add(l); });
  const root3 = new THREE.Group(); world.add(root3);
  const ray = new THREE.Raycaster(); const mouse = new THREE.Vector2();
  let compare = false, placing = false, playing = false, tcur = 0, tmax = 0, items = [], sel = 0;

  const resize = () => { const w = stage.clientWidth, h = stage.clientHeight; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); };
  new ResizeObserver(resize).observe(stage); resize();

  // ------------------------------------------------------------ scene construction
  function build(sc) {
    root3.clear();
    const T = sc.terrain, W = T.width, H = T.height, c = T.cell_size_m, heights = T.heights_m, dens = sc.tree_density.values;
    const base = T.min_m; let exag = +$('#exag').value;
    const wx = (col) => (col - (W - 1) / 2) * c, wz = (row) => (row - (H - 1) / 2) * c; // north = -z
    const hAt = (x, z) => { const col = x / c + (W - 1) / 2, row = z / c + (H - 1) / 2; const c0 = Math.max(0, Math.min(W - 2, Math.floor(col))), r0 = Math.max(0, Math.min(H - 2, Math.floor(row))); const fx = Math.min(1, Math.max(0, col - c0)), fz = Math.min(1, Math.max(0, row - r0)); const g = (r, cc) => heights[r][cc]; return (g(r0, c0) * (1 - fx) * (1 - fz) + g(r0, c0 + 1) * fx * (1 - fz) + g(r0 + 1, c0) * (1 - fx) * fz + g(r0 + 1, c0 + 1) * fx * fz) - base; };
    // items: reference, each detected frame, each hypothetical (on top of all detections)
    const cellFirst = (arr) => { const m = new Int16Array(W * H).fill(-1); arr.forEach(([idx, cells]) => cells.forEach((i) => { if (m[i] < 0 || idx < m[i]) m[i] = idx; })); return m; };
    items = sc.timeline.map((f, i) => ({ kind: i === 0 ? 'reference' : 'detected', label: f.label, claim: f.claim_type, frame: f, upto: i, hyp: null }));
    const lastDet = sc.timeline.length - 1;
    sc.hypothetical_scenarios.forEach((h) => items.push({ kind: 'hypothetical', label: h.label, claim: h.claim_type, frame: h, upto: lastDet, hyp: h }));
    const detCells = sc.timeline.slice(1).map((f, i) => [i + 1, f.newly_cleared_cells]);
    S = { W, H, c, base, heights, dens, wx, wz, hAt, detMap: cellFirst(detCells), hypMaps: new Map(), exag, T, risk: sc.forecast_risk };
    sc.hypothetical_scenarios.forEach((h) => S.hypMaps.set(h.id, new Set(h.cells)));
    // terrain meshes (A = reference, B = selected state)
    const mkTerrain = (layer) => {
      const g = new THREE.BufferGeometry(); const pos = new Float32Array(W * H * 3), col = new Float32Array(W * H * 3), idx = [];
      for (let r = 0; r < H; r++) for (let q = 0; q < W; q++) { const i = r * W + q; pos[i * 3] = wx(q); pos[i * 3 + 1] = (heights[r][q] - base) * exag; pos[i * 3 + 2] = wz(r); }
      for (let r = 0; r < H - 1; r++) for (let q = 0; q < W - 1; q++) { const a = r * W + q; idx.push(a, a + W, a + 1, a + 1, a + W, a + W + 1); }
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3)); g.setAttribute('color', new THREE.BufferAttribute(col, 3)); g.setIndex(idx); g.computeVertexNormals();
      const m = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ vertexColors: true })); m.layers.set(layer); return m;
    };
    S.tA = mkTerrain(LAYER_A); S.tB = mkTerrain(LAYER_B); root3.add(S.tA, S.tB);
    // trees (simulated placement), budget-limited
    const budget = 14000; const meanD = dens.reduce((a, b) => a + b, 0) / Math.max(1, dens.length) || 0.001;
    const perCell = Math.max(0.5, Math.min(40, budget / (W * H * meanD)));
    const rand = rng(12345), trees = [];
    for (let r = 0; r < H; r++) for (let q = 0; q < W; q++) { const d = dens[r * W + q]; if (d <= 0.02) continue; let n = d * perCell; n = Math.floor(n) + (rand() < n % 1 ? 1 : 0); for (let k = 0; k < n; k++) { const x = wx(q) + (rand() - 0.5) * c, z = wz(r) + (rand() - 0.5) * c; trees.push({ x, z, y: hAt(x, z), cell: r * W + q, s: 0.8 + rand() * 0.6, d: rand() }); } }
    S.trees = trees; S.perCell = perCell;
    const crown = new THREE.ConeGeometry(14, 46, 6); crown.translate(0, 23 + 8, 0); const trunk = new THREE.CylinderGeometry(2.4, 3, 12, 5); trunk.translate(0, 6, 0);
    const merged = mergeGeoms([crown, trunk]);
    const mkTrees = (layer) => { const m = new THREE.InstancedMesh(merged, new THREE.MeshLambertMaterial({ color: 0x2f7d4f }), Math.max(1, trees.length)); m.layers.set(layer); m.instanceMatrix.setUsage(THREE.DynamicDrawUsage); m.frustumCulled = false; return m; };
    S.mA = mkTrees(LAYER_A); S.mB = mkTrees(LAYER_B); root3.add(S.mA, S.mB);
    const col = new THREE.Color(); trees.forEach((t, i) => { col.setHSL(0.30 + (t.d - 0.5) * 0.05, 0.5, 0.26 + t.d * 0.1); S.mA.setColorAt(i, col); S.mB.setColorAt(i, col); });
    S.dummy = new THREE.Object3D();
    const extent = Math.max(W, H) * c; camera.position.set(extent * 0.55, extent * 0.55, extent * 0.75); controls.target.set(0, (T.max_m - base) * exag * 0.3, 0); controls.update();
    camera.far = extent * 8; camera.updateProjectionMatrix();
    paintItems(); setItem(Math.min(lastDet, items.length - 1), true);
  }

  function mergeGeoms(gs) { const pos = [], nor = [], idx = []; let off = 0; gs.forEach((g) => { const gi = g.index ? g : g.toNonIndexed(); const p = g.getAttribute('position'), n = g.getAttribute('normal'); for (let i = 0; i < p.count; i++) { pos.push(p.getX(i), p.getY(i), p.getZ(i)); nor.push(n.getX(i), n.getY(i), n.getZ(i)); } if (g.index) for (let i = 0; i < g.index.count; i++) idx.push(g.index.getX(i) + off); else for (let i = 0; i < p.count; i++) idx.push(i + off); off += p.count; }); const m = new THREE.BufferGeometry(); m.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); m.setAttribute('normal', new THREE.Float32BufferAttribute(nor, 3)); m.setIndex(idx); return m; }

  // state of a cell at (possibly fractional) time t for an item: returns first-clear time or -1
  const clearTime = (item, cell) => { const d = S.detMap[cell]; let t = d > 0 && d <= item.upto ? d : -1; if (item.hyp && S.hypMaps.get(item.hyp.id).has(cell)) { const ht = item.upto + 1; t = t < 0 ? ht : Math.min(t, ht); } return t; };
  function applyState(terrain, mesh, item, time, withAnim) {
    const { W, H, dens, dummy } = S; const col = terrain.geometry.getAttribute('color'); const showLoss = $('#lLoss').checked, showRisk = $('#lRisk').checked && S.risk;
    const riskMap = new Map((S.risk?.cells || []).map((c) => [c.index, c.risk])); const c = new THREE.Color();
    for (let i = 0; i < W * H; i++) {
      const h = (S.heights[(i / W) | 0][i % W] - S.base) / Math.max(1, S.T.max_m - S.T.min_m); const d = dens[i];
      c.setRGB(0.55 + 0.25 * h - 0.25 * d, 0.5 + 0.12 * h + 0.12 * d, 0.32 + 0.1 * h - 0.1 * d);
      const ct = clearTime(item, i);
      if (showLoss && ct >= 0 && time >= ct) c.set(item.hyp && S.hypMaps.get(item.hyp.id).has(i) && (S.detMap[i] < 0 || S.detMap[i] > item.upto) ? 0xe08a2e : 0xb23a26);
      else if (showRisk && riskMap.has(i)) c.lerp(new THREE.Color(0xe3a14b), Math.min(0.85, 0.25 + riskMap.get(i) * 6));
      col.setXYZ(i, c.r, c.g, c.b);
    }
    col.needsUpdate = true;
    const show = $('#lTrees').checked; const exag = S.exag;
    S.trees.forEach((t, i) => {
      const ct = clearTime(item, t.cell); let s = t.s, tilt = 0;
      if (ct >= 0 && time >= ct) { const p = withAnim ? Math.min(1, Math.max(0, (time - ct) * 1.5 - t.d * 0.6)) : 1; s = t.s * (1 - p); tilt = p * 1.3; }
      dummy.position.set(t.x, t.y * exag, t.z); dummy.rotation.set(tilt * (t.d - 0.5) * 2, 0, tilt); dummy.scale.setScalar(show ? Math.max(0.001, s) : 0.001); dummy.updateMatrix(); mesh.setMatrixAt(i, dummy.matrix);
    });
    mesh.instanceMatrix.needsUpdate = true;
  }
  const refItem = () => items[0];

  function setItem(i, jump) {
    sel = i; const it = items[i]; tcur = jump ? it.upto + (it.kind === 'hypothetical' ? 1.5 : 0.5) : tcur; tmax = it.upto + (it.hyp ? 2 : 1);
    $('#tl').max = items.length - 1; $('#tl').value = i; $('#tlLab').textContent = it.label;
    const lab = it.kind === 'reference' ? 'REFERENCE MAP (WorldCover 2021)' : it.kind === 'detected' ? 'CANDIDATE DETECTION — UNVERIFIED' : 'HYPOTHETICAL SCENARIO — NOT A FORECAST';
    $('#lab').textContent = lab; $('#lab').style.background = it.kind === 'hypothetical' ? 'rgba(180,100,10,.9)' : it.kind === 'detected' ? 'rgba(160,40,25,.9)' : 'rgba(40,90,60,.9)';
    $$('#items [data-i]').forEach((el) => el.classList.toggle('sel', +el.dataset.i === i));
    refresh();
  }
  const refresh = () => { applyState(S.tB, S.mB, items[sel], tcur, true); applyState(S.tA, S.mA, refItem(), 0, false); };

  function paintItems() {
    $('#items').innerHTML = html`<div class="stack">${items.map((it, i) => raw(`<div class="route-card" data-i="${i}"><div class="row"><b class="grow">${esc(it.label)}</b>${it.kind === 'reference' ? tag('reference map', 'neutral').s : it.kind === 'detected' ? tag('model candidate', 'configure').s : tag('hypothetical', 'purple').s}</div><div class="help">${it.kind === 'detected' ? esc(fmt.ha(it.frame.cumulative_loss_hectares)) + ' candidate loss · observed ' + esc(it.frame.date) : it.kind === 'hypothetical' ? esc(fmt.ha(it.hyp.area_hectares)) + ' assumed clearing' : 'Tree-cover density before any detected change'}</div></div>`))}</div>`;
    $$('#items [data-i]').forEach((el) => (el.onclick = () => setItem(+el.dataset.i, true)));
    const fr = S.risk; $('#fcMsg').textContent = fr ? `risk layer for ${fr.horizon_days} days exists (toggle “Risk”). Estimated, not observed.` : 'no validated spatial forecast exists for this region, so none is shown. Use a hypothetical clearing to explore a scenario.';
    if (!fr) { $('#lRisk').disabled = true; }
  }

  // ------------------------------------------------------------ interaction
  function frame() {
    controls.update();
    if (playing && items.length) { tcur += 0.016 * +$('#spd').value; if (tcur >= tmax) { tcur = tmax; playing = false; $('#play').textContent = '▶ Play'; } refresh(); }
    const w = stage.clientWidth, h = stage.clientHeight;
    renderer.setViewport(0, 0, w, h); renderer.setScissor(0, 0, w, h); renderer.clear();
    if (compare) {
      const half = w / 2;
      renderer.setScissor(0, 0, half, h); camera.layers.set(LAYER_A); renderer.render(world, camera);
      renderer.setScissor(half, 0, half, h); camera.layers.set(LAYER_B); renderer.render(world, camera);
    } else { camera.layers.set(LAYER_B); renderer.render(world, camera); }
  }
  renderer.setAnimationLoop(() => { if (!disposed) frame(); });

  renderer.domElement.addEventListener('pointerdown', (e) => { renderer.domElement._d = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    const d = renderer.domElement._d; if (!d || Math.hypot(e.clientX - d[0], e.clientY - d[1]) > 5 || !S) return;
    const rc = renderer.domElement.getBoundingClientRect(); mouse.set(((e.clientX - rc.left) / rc.width) * 2 - 1, -((e.clientY - rc.top) / rc.height) * 2 + 1);
    camera.layers.set(LAYER_B); ray.layers.set(LAYER_B); ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObject(S.tB)[0]; if (!hit) return;
    const col = Math.round(hit.point.x / S.c + (S.W - 1) / 2), row = Math.round(hit.point.z / S.c + (S.H - 1) / 2); const cell = Math.max(0, Math.min(S.H - 1, row)) * S.W + Math.max(0, Math.min(S.W - 1, col));
    if (placing) { place(hit.point); return; }
    const it = items[sel], ct = clearTime(it, cell), inHyp = it.hyp && S.hypMaps.get(it.hyp.id).has(cell);
    const status = ct >= 0 && tcur >= ct ? (inHyp && !(S.detMap[cell] > 0 && S.detMap[cell] <= it.upto) ? 'Hypothetical clearing (assumption)' : `Candidate loss, ${scene3.timeline[S.detMap[cell]]?.label || ''} (unverified)`) : (S.dens[cell] > 0.3 ? 'Tree cover in the 2021 reference map' : 'Little or no tree cover in the reference map');
    const ll = toLonLat(hit.point.x, hit.point.z);
    $('#info').innerHTML = html`<b>${status}</b><br>Elevation ${Math.round(S.heights[(cell / S.W) | 0][cell % S.W])} m · tree density ${Math.round(S.dens[cell] * 100)}%<br>${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`;
  });
  const toLonLat = (x, z) => { const o = scene3.terrain.origin; const north = -z; return { lon: o.lon + x / (111320 * Math.cos((o.lat * Math.PI) / 180)), lat: o.lat + north / 110540 }; };

  async function place(point) {
    placing = false; renderer.domElement.style.cursor = '';
    const r = +$('#hr').value, h = +$('#hh').value, ll = toLonLat(point.x, point.z); const ring = [];
    for (let a = 0; a <= 36; a++) { const t = (a / 36) * 2 * Math.PI; const dx = Math.cos(t) * r, dz = Math.sin(t) * r; const p = toLonLat(point.x + dx, point.z + dz); ring.push([+p.lon.toFixed(7), +p.lat.toFixed(7)]); }
    try { await api('/forest/simulations', { method: 'POST', body: { region_id: $('#reg').value, geometry: { type: 'Polygon', coordinates: [ring] }, horizon_days: h, mode: 'user_defined', assumptions: { source: 'AR explorer click', radius_m: r } } }); toast('Hypothetical clearing stored. Rebuilding scene…'); await load(); setItem(items.length - 1, true); } catch (e) { toast(e.message, 'error'); }
  }

  async function load() {
    const id = $('#reg').value; $('#loadMsg').textContent = 'Fetching real elevation and tree cover (first load can take about a minute)…';
    try { scene3 = await api('/ar/forest-scenes/' + id); $('#loadMsg').innerHTML = `${esc(scene3.attribution)}.${scene3.notes.length ? '<br>' + esc(scene3.notes.join(' ')) : ''}`; build(scene3); $('#loadMsg').innerHTML += `<br>${S.trees.length.toLocaleString()} simulated tree markers (about ${S.perCell.toFixed(1)} per ${Math.round(S.c)} m cell at full cover), enlarged for visibility.`; }
    catch (e) { $('#loadMsg').innerHTML = `<span style="color:#b8321f">${esc(e.message)}</span>`; $('#lab').textContent = 'Scene unavailable'; }
  }

  $('#reg').onchange = load;
  $('#play').onclick = () => { if (!S) return; if (!playing) { if (tcur >= tmax) tcur = 0; playing = true; $('#play').textContent = '⏸ Pause'; } else { playing = false; $('#play').textContent = '▶ Play'; } };
  $('#reset').onclick = () => { playing = false; tcur = 0; $('#play').textContent = '▶ Play'; refresh(); };
  $('#tl').oninput = (e) => setItem(+e.target.value, true);
  ['lTrees', 'lLoss', 'lRisk'].forEach((id) => ($('#' + id).onchange = () => S && refresh()));
  $('#exag').oninput = () => { if (!scene3) return; const keep = sel; S.exag = +$('#exag').value; const pos = (t) => { const p = t.geometry.getAttribute('position'); for (let r = 0; r < S.H; r++) for (let q = 0; q < S.W; q++) p.setY(r * S.W + q, (S.heights[r][q] - S.base) * S.exag); p.needsUpdate = true; t.geometry.computeVertexNormals(); }; pos(S.tA); pos(S.tB); controls.target.y = (S.T.max_m - S.base) * S.exag * 0.3; setItem(keep, false); };
  $('#cmp').onclick = () => { compare = !compare; $('#cmp').textContent = compare ? 'Single view' : 'Compare with reference'; if (compare) toast('Left: reference map. Right: selected timeline item.'); };
  $('#place').onclick = () => { placing = true; renderer.domElement.style.cursor = 'crosshair'; toast('Click on the terrain to place the clearing'); };

  // ------------------------------------------------------------ WebXR AR
  $('#ar').onclick = async () => {
    if (!dev.ar || !S) return;
    try {
      renderer.xr.enabled = true; renderer.setScissorTest(false);
      xrSession = await navigator.xr.requestSession('immersive-ar', { requiredFeatures: ['hit-test'], optionalFeatures: ['dom-overlay'], domOverlay: { root: $('#hudOverlay') } });
      await renderer.xr.setSession(xrSession); renderer.setClearColor(0x000000, 0);
      const extent = Math.max(S.W, S.H) * S.c, table = 0.9 / extent; let scale = table, placed = false;
      root3.scale.setScalar(scale); root3.visible = false; S.tA.visible = S.mA.visible = false;
      const reticle = new THREE.Mesh(new THREE.RingGeometry(0.08, 0.1, 32).rotateX(-Math.PI / 2), new THREE.MeshBasicMaterial({ color: 0x2f8f67 })); reticle.matrixAutoUpdate = false; reticle.visible = false; reticle.layers.set(LAYER_B); world.add(reticle);
      const refSpace = await xrSession.requestReferenceSpace('local'), viewer = await xrSession.requestReferenceSpace('viewer'), hit = await xrSession.requestHitTestSource({ space: viewer });
      $('#hudOverlay').innerHTML = `<div class="ar-card">Point at a flat surface, tap to place the forest. <button id="xS" class="button subtle sm">Smaller</button><button id="xL" class="button subtle sm">Larger</button><button id="xR" class="button subtle sm">Rotate</button><button id="xP" class="button subtle sm">▶/⏸</button><button id="xX" class="button primary sm">Exit AR</button></div>`;
      $('#hudOverlay').style.pointerEvents = 'auto';
      $('#xS').onclick = () => root3.scale.multiplyScalar(0.8); $('#xL').onclick = () => root3.scale.multiplyScalar(1.25); $('#xR').onclick = () => (root3.rotation.y += Math.PI / 6); $('#xP').onclick = () => $('#play').click(); $('#xX').onclick = () => xrSession.end();
      xrSession.addEventListener('select', () => { if (reticle.visible) { root3.position.setFromMatrixPosition(reticle.matrix); root3.visible = true; placed = true; } });
      xrSession.addEventListener('end', () => { renderer.xr.enabled = false; renderer.setScissorTest(true); renderer.setClearColor(0x13241c, 1); world.remove(reticle); root3.scale.setScalar(1); root3.position.set(0, 0, 0); root3.rotation.set(0, 0, 0); root3.visible = true; S.tA.visible = S.mA.visible = true; $('#hudOverlay').innerHTML = ''; xrSession = null; resize(); });
      renderer.setAnimationLoop((t, xrFrame) => {
        if (xrFrame) { const hr = xrFrame.getHitTestResults(hit); if (hr.length) { const pose = hr[0].getPose(refSpace); reticle.visible = !placed || true; reticle.matrix.fromArray(pose.transform.matrix); } else reticle.visible = false;
          if (playing) { tcur += 0.016 * +$('#spd').value; if (tcur >= tmax) { tcur = tmax; playing = false; } refresh(); }
          camera.layers.set(LAYER_B); renderer.render(world, camera); } else if (!disposed) frame();
      });
    } catch (e) { toast('Could not start AR: ' + e.message + '. The 3D view still works.', 'error'); renderer.xr.enabled = false; renderer.setScissorTest(true); }
  };

  await load();
  return { destroy: () => { disposed = true; playing = false; renderer.setAnimationLoop(null); xrSession?.end().catch(() => {}); controls.dispose(); renderer.dispose(); root3.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); }); } };
}

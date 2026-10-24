import { api, html, raw, esc, fmt, tag, empty, toast, can, waitJob, kv, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

const EVID = { optical_radar_agree: ['Sentinel-1 and Sentinel-2 agree', 'red'], optical_radar_disagree: ['Radar does not support', 'configure'], optical_only_radar_insufficient: ['Optical only (radar insufficient)', 'neutral'], optical_only: ['Optical only', 'neutral'] };

export async function render(root, { params }) {
  const regions = await api('/regions');
  if (!regions.length) { root.innerHTML = empty('No regions to analyse', 'Create a monitoring region first (Forest Monitoring).', '◐'); return; }
  let region = regions.find((r) => r.id === params[0]) || regions[0], change = null, cmp = null, chart = null;
  const today = new Date(); const y = (n) => `${today.getFullYear() - n}-02-15`;
  root.innerHTML = html`
    <div class="grid split">
      <div class="stack">
        <div class="panel"><h3>Run change detection</h3><p class="sub">Sentinel-2 optical (dNBR) with Sentinel-1 SAR (VH backscatter) as supporting evidence. Compare the same season in different years.</p>
          <form id="run" class="stack">
            <label class="f">Region<select name="region">${raw(regions.map((r) => `<option value="${esc(r.id)}" ${r.id === region.id ? 'selected' : ''}>${esc(r.name)}</option>`).join(''))}</select></label>
            <div class="grid g2"><label class="f">Baseline date<input type="date" name="b" value="${y(1)}" required></label><label class="f">Comparison date<input type="date" name="c" value="${y(0)}" required></label></div>
            <div class="grid g2"><label class="f">Compositing window ± days<input type="number" name="w" value="30" min="5" max="90"></label><label class="f">Max cloud cover %<input type="number" name="cc" value="60" min="0" max="100"></label></div>
            <div class="row"><button class="button primary" id="go" ${can('admin', 'reviewer') ? '' : 'disabled title="Reviewer or administrator role required"'}>Run analysis</button><span class="help" id="runState"></span></div>
            <div class="progress" id="prog" hidden><i style="width:15%"></i></div>
          </form></div>
        <div class="panel"><h3>Detections for this region</h3><div id="history"></div><div class="chart-box" style="height:200px"><canvas id="histChart"></canvas></div></div>
      </div>
      <div class="stack"><div class="panel" id="summary"><h3>Result</h3><p class="sub">Run an analysis or select a stored detection.</p></div></div>
    </div>
    <div class="panel" style="margin-top:16px"><div class="row"><div class="grow"><h3>Change map</h3><p class="sub">Red = candidate loss where radar supports the optical signal; amber = radar does not support it. These are model candidates, not confirmed deforestation.</p></div>
      <div class="pill-btns" id="viewMode"><button data-m="loss" class="on">Detected change</button><button data-m="compare">Before / after</button></div></div>
      <div id="map" class="map-box tall"></div><div id="cmpCtl"></div>
      ${raw(legend([['#c73b25', 'Candidate loss, optical + radar agree'], ['#e3a14b', 'Candidate loss, radar disagrees'], ['#1d6b4b', 'Region']]))}</div>
    <div class="grid g2" style="margin-top:16px"><div class="panel" id="patches"><h3>Detected patches</h3></div><div class="panel" id="prov"><h3>Evidence and provenance</h3></div></div>`;
  const { map, group } = await createMap($('#map'));
  const regionG = group('Region'), lossG = group('Candidate loss'), cmpG = group('Imagery', false);

  async function loadHistory() {
    const list = (await api(`/forest/changes?region_id=${region.id}&limit=100`)).filter((c) => c.disturbance_type !== 'x');
    $('#history').innerHTML = list.length ? html`<table class="t"><thead><tr><th>Interval</th><th>Status</th><th>Candidate loss</th></tr></thead><tbody>${list.map((c) => raw(`<tr data-id="${esc(c.id)}" class="${change?.id === c.id ? 'sel' : ''}"><td>${esc(fmt.date(c.baseline_date))} → ${esc(fmt.date(c.comparison_date))}</td><td>${tag(fmt.status(c.status), c.status === 'CANDIDATES_FOUND' ? 'configure' : 'neutral').s}</td><td>${esc(fmt.ha(c.area_hectares))}</td></tr>`))}</tbody></table>` : empty('No detections yet', 'Results of each run are stored with their evidence.', '◐');
    $$('#history tr[data-id]').forEach((tr) => (tr.onclick = () => show(list.find((c) => c.id === tr.dataset.id))));
    const ok = list.filter((c) => c.status === 'CANDIDATES_FOUND' || c.status === 'NO_CANDIDATES').sort((a, b) => a.comparison_date.localeCompare(b.comparison_date));
    chart?.destroy();
    chart = ok.length ? new Chart($('#histChart'), { type: 'bar', data: { labels: ok.map((c) => fmt.date(c.comparison_date)), datasets: [{ label: 'Candidate loss (ha)', data: ok.map((c) => c.area_hectares), backgroundColor: '#c73b25' }] }, options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, title: { display: true, text: 'ha' } } } } }) : null;
    return list;
  }

  async function show(c) {
    change = c; $$('#history tr').forEach((t) => t.classList.toggle('sel', t.dataset.id === c.id));
    lossG.clearLayers(); regionG.clearLayers(); cmpG.clearLayers();
    const rl = addGeo(regionG, region.geometry, STYLE.region);
    const prov = c.provenance || {}, scores = prov.polygon_scores || [];
    const layers = [rl];
    if (c.disturbance_type === 'CANDIDATE_FOREST_LOSS' && c.geometry.type === 'MultiPolygon') {
      c.geometry.coordinates.forEach((poly, i) => { const s = scores[i] || {}; const st = s.evidence_level === 'optical_radar_agree' ? STYLE.lossAgree : STYLE.lossDisagree; layers.push(addGeo(lossG, { type: 'Polygon', coordinates: poly }, st, popup(`Patch ${i + 1}`, [`${s.area_hectares ?? '?'} ha`, (EVID[s.evidence_level] || ['Unlabelled'])[0], `dNBR score ${s.mean_score ?? '—'} (uncalibrated)`, s.radar_support_fraction != null ? `Radar support ${fmt.pct(s.radar_support_fraction)}` : ''].filter(Boolean)))); });
    }
    fit(map, layers);
    const q = prov.data_quality || {}, bf = prov.baseline_forest || {};
    const baseHa = bf.pixels ? bf.pixels * 0.04 : null;
    const regionHa = null;
    $('#summary').innerHTML = html`<h3>${fmt.date(c.baseline_date)} → ${fmt.date(c.comparison_date)}</h3>
      <div class="row">${tag(fmt.status(c.status), c.status === 'CANDIDATES_FOUND' ? 'configure' : 'neutral')} ${tag('model candidate · unverified', 'purple')}</div>
      <div class="grid g2" style="margin-top:12px"><div class="stat"><b>${fmt.ha(c.area_hectares)}</b><span>Candidate forest loss</span></div><div class="stat"><b>${scores.length}</b><span>Detected patches</span></div>
        <div class="stat"><b>${baseHa ? fmt.ha(baseHa) : '—'}</b><span>Baseline forest in region (WorldCover ∩ vegetation)</span></div><div class="stat"><b>${baseHa ? ((c.area_hectares / baseHa) * 100).toFixed(2) + '%' : '—'}</b><span>Share of baseline forest</span></div>
        <div class="stat"><b>${baseHa ? fmt.ha(baseHa - c.area_hectares) : '—'}</b><span>Remaining forest after candidate loss</span></div><div class="stat"><b>Not assessed</b><span>Regrowth / gain (detector looks for loss only)</span></div></div>
      ${raw(q.reason ? `<div class="warn-box" style="margin-top:12px">${esc(q.reason)}</div>` : '')}
      ${raw(prov.seasonal_warning ? `<div class="warn-box" style="margin-top:12px">${esc(prov.seasonal_warning)}</div>` : '')}
      <div class="row" style="margin-top:14px">${can('admin', 'reviewer') && c.area_hectares > 0 ? raw('<button class="button primary" id="mkInc">Open investigation case</button>') : ''}${can('admin', 'reviewer', 'analyst') && c.area_hectares > 0 ? raw('<button class="button subtle" id="mkSite">Flag as potential land-use change</button><button class="button subtle" id="mkEnv">Environmental assessment</button>') : ''}</div>`;
    $('#mkInc')?.addEventListener('click', async () => { try { const i = await api('/incidents', { method: 'POST', body: { title: `Candidate forest loss ${c.area_hectares} ha (${fmt.date(c.comparison_date)})`, change_id: c.id, geometry: c.geometry, evidence: { claim_type: 'candidate_inference' } } }); toast('Case opened; it needs review before any dispatch'); window.dispatchEvent(new Event('rk:changed')); location.hash = '#/investigations/' + i.id; } catch (e) { toast(e.message, 'error'); } });
    $('#mkSite')?.addEventListener('click', async () => { try { await api(`/industrial/sites/from-change/${c.id}`, { method: 'POST' }); toast('Added to Industrial Development as a potential land-use change (verification required)'); } catch (e) { toast(e.message, 'error'); } });
    $('#mkEnv')?.addEventListener('click', () => { location.hash = `#/environment/${region.id}/${c.id}`; });
    $('#patches').innerHTML = html`<h3>Detected patches</h3>${scores.length ? raw(`<table class="t"><thead><tr><th>#</th><th>Area</th><th>dNBR</th><th>Evidence</th></tr></thead><tbody>${scores.map((s, i) => `<tr data-i="${i}"><td>${i + 1}</td><td>${esc(s.area_hectares)} ha</td><td>${esc(s.mean_score)}</td><td>${tag(...(EVID[s.evidence_level] || [s.evidence_level, 'neutral'])).s}${s.radar_support_fraction != null ? `<div class="help">radar ${esc(fmt.pct(s.radar_support_fraction))}</div>` : ''}</td></tr>`).join('')}</tbody></table>`) : empty('No patches', c.status === 'CANDIDATES_FOUND' ? 'Patch detail not stored.' : 'Nothing detected, or the analysis was withheld (see status).')}`;
    $$('#patches tr[data-i]').forEach((tr) => (tr.onclick = () => { const l = lossG.getLayers()[+tr.dataset.i]; if (l) { map.fitBounds(l.getBounds().pad(2), { maxZoom: 17 }); l.openPopup(); } }));
    const obs = prov.observations || {};
    $('#prov').innerHTML = html`<h3>Evidence and provenance</h3>${kv({ method: prov.method, claim: prov.claim_type, valid_pixel_fraction: q.valid_pixel_fraction, cloud_fraction: q.cloud_fraction, baseline_forest_source: bf.source, radar: prov.radar?.used ? `used (${prov.radar.feature}, ≥ ${prov.radar.threshold_db} dB)` : prov.radar?.reason, seasonal_offset_days: prov.seasonal_offset_days })}
      <h4 style="margin-top:12px">Observations used</h4>${Object.keys(obs).length ? raw(`<table class="t"><tbody>${Object.entries(obs).map(([k, o]) => `<tr><td><b>${esc(k.replace('_', ' '))}</b><div class="help">${esc(o.status)}${o.cache_hit ? ' · cached' : ''}</div></td><td>${esc(fmt.date(o.acquired?.[0]))} → ${esc(fmt.date(o.acquired?.[1]))}<div class="help">${(o.scenes || []).length} scene(s) · clear ${esc(fmt.pct(o.valid_pixel_fraction))}</div></td></tr>`).join('')}</tbody></table>`) : raw('<p class="help">No stored satellite observations (analysis ran on supplied rasters or was unavailable).</p>')}
      <div class="note-box" style="margin-top:12px">Thresholds are configuration, not validated operating points. NDVI/NBR decreases can also be caused by fire, harvest, plantation rotation, flooding or seasonal change; verify before concluding deforestation.</div>`;
    setupCompare(obs);
  }

  function setupCompare(obs) {
    cmp = { b: obs.before_s2?.id, a: obs.after_s2?.id };
    $$('#viewMode button').forEach((b) => (b.disabled = b.dataset.m === 'compare' && !(cmp.b && cmp.a)));
  }

  async function enterCompare() {
    cmpG.clearLayers(); map.addLayer(cmpG);
    const [mb, ma] = await Promise.all([api(`/observations/${cmp.b}/preview-meta`), api(`/observations/${cmp.a}/preview-meta`)]);
    const [bb, ba] = await Promise.all([api(`/observations/${cmp.b}/preview.png`, { blob: true }), api(`/observations/${cmp.a}/preview.png`, { blob: true })]);
    const before = L.imageOverlay(URL.createObjectURL(bb), mb.bounds).addTo(cmpG), after = L.imageOverlay(URL.createObjectURL(ba), ma.bounds).addTo(cmpG);
    map.fitBounds(ma.bounds);
    $('#cmpCtl').innerHTML = html`<div class="compare-ctl"><span>Baseline ${fmt.date(mb.acquired_from)}</span><input type="range" min="0" max="100" value="50" id="split" aria-label="Before and after split"><span>Comparison ${fmt.date(ma.acquired_from)}</span></div>`;
    const clip = () => {
      const r = +$('#split').value / 100, size = map.getSize(), nw = map.containerPointToLayerPoint([0, 0]), se = map.containerPointToLayerPoint(size), x = nw.x + size.x * r;
      before.getElement().style.clip = `rect(${nw.y}px, ${x}px, ${se.y}px, ${nw.x}px)`; after.getElement().style.clip = `rect(${nw.y}px, ${se.x}px, ${se.y}px, ${x}px)`;
    };
    $('#split').oninput = clip; map.on('move zoom viewreset', clip); clip();
    cmp.off = () => map.off('move zoom viewreset', clip);
  }

  $('#viewMode').onclick = async (e) => {
    const m = e.target.dataset.m; if (!m || e.target.disabled) return;
    $$('#viewMode button').forEach((b) => b.classList.toggle('on', b === e.target));
    if (m === 'compare') { try { await enterCompare(); } catch (err) { toast(err.message, 'error'); } } else { cmp?.off?.(); map.removeLayer(cmpG); $('#cmpCtl').innerHTML = ''; }
  };
  $('#run [name=region]').onchange = async (e) => { region = regions.find((r) => r.id === e.target.value); change = null; await loadHistory(); regionG.clearLayers(); lossG.clearLayers(); fit(map, [addGeo(regionG, region.geometry, STYLE.region)]); };
  $('#run').onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target); const btn = $('#go'); btn.disabled = true; $('#prog').hidden = false;
    try {
      const j = await api('/forest/change-detection', { method: 'POST', body: { region_id: f.get('region'), baseline_date: f.get('b') + 'T00:00:00Z', comparison_date: f.get('c') + 'T00:00:00Z', provider: 'planetary_computer', window_days: +f.get('w'), max_cloud_cover: +f.get('cc') } });
      const res = await waitJob(j.run_id, (job, s) => { $('#runState').textContent = `Acquiring Sentinel-1/2 and processing… ${s}s`; $('#prog i').style.width = Math.min(90, 15 + s / 2) + '%'; });
      toast(`Finished: ${fmt.status(res.status)}${res.candidate_incident_id ? ' · a review case was opened' : ''}`); window.dispatchEvent(new Event('rk:changed'));
      const list = await loadHistory(); show(list.find((c) => c.id === res.id) || res);
    } catch (err) { toast(err.message, 'error'); $('#runState').textContent = err.message; } finally { btn.disabled = false; $('#prog').hidden = true; }
  };

  const list = await loadHistory();
  fit(map, [addGeo(regionG, region.geometry, STYLE.region)]);
  if (list.length) show(list[0]);
  return { destroy: () => { cmp?.off?.(); chart?.destroy(); map.remove(); } };
}

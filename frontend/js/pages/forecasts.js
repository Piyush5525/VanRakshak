import { api, html, raw, esc, fmt, tag, empty, toast, dialog, can, waitJob, kv, download, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

export async function render(root) {
  const regions = await api('/regions');
  if (!regions.length) { root.innerHTML = empty('No regions yet', 'Forecasts need a monitored region with stored detections.', '⟿'); return; }
  let region = regions[0], horizon = 30, proj = null, charts = [];
  root.innerHTML = html`
    <div class="panel"><div class="row"><label class="f grow" style="max-width:340px">Region<select id="region">${raw(regions.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join(''))}</select></label>
      <div><div class="help">Horizon</div><div class="pill-btns" id="hz"><button data-h="7">7 days</button><button data-h="30" class="on">30 days</button><button data-h="90">90 days</button></div></div></div>
      <div class="warn-box" style="margin-top:12px"><b>Read this first.</b> Two different things are shown. The <b>projection</b> extrapolates the historical rate of detected loss in this region; it says <i>how much</i>, not <i>where</i>. The <b>spatial risk layer</b> says <i>where</i> new loss is more likely, but only exists when a model has been trained and evaluated on enough verified history. Neither claims to know exactly where cutting will happen. Scenarios are hypothetical multipliers you can change.</div></div>
    <div class="grid split" style="margin-top:16px">
      <div class="stack">
        <div class="panel"><div class="row"><div class="grow"><h3>Projection from historical change rate</h3><p class="sub" id="projSub"></p></div><button class="button primary" id="mkProj">Compute projection</button></div>
          <details style="margin:8px 0"><summary class="help">Scenario multipliers (hypothetical)</summary><div class="grid g3" style="margin-top:8px"><label class="f">Continued disturbance<input type="number" step="0.1" min="0" max="5" id="m1" value="1" disabled></label><label class="f">Effective protection<input type="number" step="0.1" min="0" max="5" id="m2" value="0.4"></label><label class="f">Restoration focus<input type="number" step="0.1" min="0" max="5" id="m3" value="0.2"></label></div><div class="help">Multiplies the historical rate. These are assumptions you choose, not model outputs.</div></details>
          <div id="projBody"></div><div class="chart-box"><canvas id="projChart"></canvas></div></div>
        <div class="panel"><div class="row"><div class="grow"><h3>History used</h3><p class="sub">Interval-by-interval candidate loss stored for this region.</p></div>${can('admin', 'analyst', 'reviewer') ? raw('<button class="button subtle" id="build">Build yearly history…</button>') : ''}</div><div id="intervals"></div><div class="chart-box" style="height:220px"><canvas id="rateChart"></canvas></div></div>
      </div>
      <div class="stack">
        <div class="panel"><h3>Current vs projected</h3><p class="sub">Current candidate loss and, when available, the spatial risk layer for the selected horizon.</p><div id="map" class="map-box"></div><div id="mapLegend"></div></div>
        <div class="panel"><div class="row"><div class="grow"><h3>Spatial risk model</h3></div>${can('admin') ? raw('<button class="button subtle sm" id="train">Train…</button>') : ''}<button class="button subtle sm" id="risk">Generate layer</button></div><div id="model"></div></div>
      </div>
    </div>`;
  const { map, group } = await createMap($('#map'));
  const lossG = group('Candidate loss (current)'), riskG = group('Spatial risk');

  async function loadAll() {
    const [list, changes, models, evals] = await Promise.all([api(`/forest/projections?region_id=${region.id}`), api(`/forest/changes?region_id=${region.id}&limit=100`), api('/system/models'), api('/evaluations')]);
    proj = list[0] || null; paintProj(); paintHistory(changes); paintModel(models, evals);
    lossG.clearLayers(); const ls = [addGeo(lossG, region.geometry, STYLE.region)]; changes.filter((c) => c.disturbance_type === 'CANDIDATE_FOREST_LOSS').forEach((c) => ls.push(addGeo(lossG, c.geometry, STYLE.loss, popup('Candidate loss', [fmt.ha(c.area_hectares), fmt.date(c.comparison_date)])))); fit(map, ls);
    $('#mapLegend').innerHTML = legend([['#c73b25', 'Current candidate loss'], ['#1d6b4b', 'Region'], ['#e3a14b', 'Elevated spatial risk (if model usable)']]);
  }

  function paintProj() {
    charts.forEach((c) => c.destroy()); charts = [];
    if (!proj) { $('#projSub').textContent = 'No projection computed yet.'; $('#projBody').innerHTML = empty('Not computed', 'Needs at least one stored change-detection interval for this region.', '⟿'); return; }
    $('#projSub').textContent = `Computed ${fmt.dt(proj.created_at)} · ${proj.status === 'OK' ? 'baseline extrapolation + hypothetical scenarios' : proj.status}`;
    if (proj.status !== 'OK') { $('#projBody').innerHTML = html`<div class="warn-box">${proj.reason}</div>`; return; }
    const names = Object.keys(proj.projections), hs = ['7', '30', '90'];
    $('#projBody').innerHTML = html`<table class="t"><thead><tr><th>Scenario</th>${hs.map((h) => raw(`<th${+h === horizon ? ' style="color:#1d6b4b"' : ''}>${h} days</th>`))}</tr></thead><tbody>${names.map((n) => { const p = proj.projections[n]; return raw(`<tr><td><b>${esc(fmt.status(n))}</b><div class="help">${esc(p.kind.replace('_', ' '))} · ×${esc(p.multiplier)}</div></td>${hs.map((h) => { const v = p.by_horizon_days[h]; return `<td${+h === horizon ? ' style="background:#f1f8f3"' : ''}>${esc(v.expected_additional_loss_ha)} ha${v.range_ha ? `<div class="help">${esc(v.range_ha[0])}–${esc(v.range_ha[1])} (90%)</div>` : '<div class="help">no range</div>'}</td>`; }).join('')}</tr>`); })}</tbody></table>
      ${raw(proj.warnings.map((w) => `<div class="warn-box" style="margin-top:8px">${esc(w)}</div>`).join(''))}
      <div class="note-box" style="margin-top:8px"><b>Assumptions:</b> ${proj.assumptions.join(' · ')}<br><b>Backtest:</b> ${proj.backtest.status ? 'not possible — ' + proj.backtest.reason : `predicted ${proj.backtest.predicted_ha} ha vs observed ${proj.backtest.observed_ha} ha (error ${proj.backtest.abs_error_ha} ha)`}</div>
      <div class="row" style="margin-top:10px"><button class="button subtle sm" id="pdf">Download PDF report</button></div>`;
    $('#pdf').onclick = () => download(`/forest/projections/${proj.id}/report.pdf`, `rakshak-projection.pdf`);
    const palette = { continued_disturbance: '#c73b25', effective_protection: '#2f8f67', restoration_focus: '#6b8e9f' };
    charts.push(new Chart($('#projChart'), { type: 'bar', data: { labels: hs.map((h) => h + ' days'), datasets: names.map((n) => ({ label: fmt.status(n) + (proj.projections[n].kind === 'baseline' ? '' : ' (hypothetical)'), data: hs.map((h) => proj.projections[n].by_horizon_days[h].expected_additional_loss_ha), backgroundColor: palette[n] || '#888' })) }, options: { maintainAspectRatio: false, plugins: { title: { display: true, text: 'Expected additional candidate loss (ha)' } }, scales: { y: { beginAtZero: true } } } }));
  }

  function paintHistory(changes) {
    const ok = changes.filter((c) => ['CANDIDATES_FOUND', 'NO_CANDIDATES'].includes(c.status)).sort((a, b) => a.comparison_date.localeCompare(b.comparison_date));
    $('#intervals').innerHTML = ok.length ? html`<table class="t"><thead><tr><th>Interval</th><th>Loss</th><th>Rate</th></tr></thead><tbody>${ok.map((c) => { const d = (new Date(c.comparison_date) - new Date(c.baseline_date)) / 864e5; return raw(`<tr><td>${esc(fmt.date(c.baseline_date))} → ${esc(fmt.date(c.comparison_date))}</td><td>${esc(fmt.ha(c.area_hectares))}</td><td>${esc((c.area_hectares / d).toFixed(4))} ha/day</td></tr>`); })}</tbody></table>` : empty('No intervals', 'Run change detection or build a yearly history.', '◐');
    if (ok.length) charts.push(new Chart($('#rateChart'), { type: 'line', data: { labels: ok.map((c) => fmt.date(c.comparison_date)), datasets: [{ label: 'ha/day', data: ok.map((c) => c.area_hectares / ((new Date(c.comparison_date) - new Date(c.baseline_date)) / 864e5)), borderColor: '#c73b25', tension: 0.2, pointRadius: 4 }] }, options: { maintainAspectRatio: false, plugins: { legend: { display: false }, title: { display: true, text: 'Candidate loss rate per interval' } }, scales: { y: { beginAtZero: true } } } }));
  }

  function paintModel(models, evals) {
    const risk = models.filter((m) => m.name === 'forest-risk');
    const usable = risk.filter((m) => m.usable);
    $('#model').innerHTML = html`${usable.length ? raw(`<p>${tag('usable', 'ready').s} ${usable.map((m) => `${esc(m.horizon_days)} d model`).join(', ')}</p>`) : raw(`<div class="warn-box">No spatial risk model is usable. ${esc(risk.find((m) => !m.usable)?.reason || 'Not enough verified detection history has been recorded to train and validate one.')} Nothing is drawn as risk until this changes.</div>`)}
      ${evals.length ? raw(`<h4 style="margin-top:10px">Evaluations</h4><table class="t"><thead><tr><th>Horizon</th><th>PR-AUC</th><th>Baseline</th><th>ECE</th></tr></thead><tbody>${evals.slice(0, 5).map((e) => `<tr><td>${esc(e.horizon_days)} d</td><td>${esc(e.metrics.pr_auc)}</td><td>${esc(e.metrics.baseline_pr_auc)}</td><td>${esc(e.metrics.expected_calibration_error)}</td></tr>`).join('')}</tbody></table><div class="help">Time-ordered split with embargo; no spatial hold-out.</div>`) : ''}`;
  }

  $('#region').onchange = (e) => { region = regions.find((r) => r.id === e.target.value); riskG.clearLayers(); loadAll(); };
  $('#hz').onclick = (e) => { const h = +e.target.dataset.h; if (!h) return; horizon = h; $$('#hz button').forEach((b) => b.classList.toggle('on', +b.dataset.h === h)); paintProj(); };
  $('#mkProj').onclick = async () => {
    try { proj = await api('/forest/projections', { method: 'POST', body: { region_id: region.id, scenario_multipliers: { effective_protection: +$('#m2').value, restoration_focus: +$('#m3').value } } }); toast(proj.status === 'OK' ? 'Projection computed' : 'Not enough data: ' + proj.reason); paintProj(); } catch (e) { toast(e.message, 'error'); }
  };
  $('#risk').onclick = async () => {
    try {
      const f = await api('/forest/forecasts', { method: 'POST', body: { region_id: region.id, horizon_days: horizon } });
      riskG.clearLayers();
      if (f.status !== 'OK') { toast(`${horizon}-day risk layer: ${fmt.status(f.status)} — ${f.provenance.reason}`, 'error'); return; }
      const l = await api(`/forest/forecasts/${f.id}/layers`); const h = 0.005;
      l.layers.forEach((c) => L.rectangle([[c.lat - h, c.lon - h], [c.lat + h, c.lon + h]], { weight: 0, fillColor: '#e3a14b', fillOpacity: Math.min(0.8, c.risk * 4 + 0.05) }).bindPopup(popup('Estimated risk of new loss', [`${(c.risk * 100).toFixed(1)}% within ${horizon} days`, 'Model-estimated; not an observation'])).addTo(riskG));
      toast('Risk layer drawn');
    } catch (e) { toast(e.message, 'error'); }
  };
  $('#train')?.addEventListener('click', async () => { try { const j = await api('/forest/forecasts/train', { method: 'POST', body: { horizon_days: horizon, region_id: region.id } }); toast('Training started'); const r = await waitJob(j.job_id, null, 3000); toast(r.status === 'TRAINED' ? `Trained (${r.usable ? 'usable' : 'not usable: did not beat baseline or failed calibration'})` : `${r.status}: ${r.reason}`, r.status === 'TRAINED' ? 'info' : 'error'); loadAll(); } catch (e) { toast(e.message, 'error'); } });
  $('#build')?.addEventListener('click', () => dialog('Build yearly history', `<div class="help">Runs change detection between consecutive yearly dates (same season) using Sentinel-1/2. Each interval takes about 3 minutes. 2–7 dates.</div><label class="f">Latest date<input type="date" name="end" required></label><label class="f">Number of years back<input type="number" name="n" value="3" min="1" max="6"></label>`, async (f) => {
    const end = new Date(f.get('end')), n = +f.get('n'); const dates = []; for (let i = n; i >= 0; i--) { const d = new Date(end); d.setFullYear(d.getFullYear() - i); dates.push(d.toISOString()); }
    const j = await api('/forest/history/build', { method: 'POST', body: { region_id: region.id, dates } }); toast('History build started; this runs in the background');
    waitJob(j.job_id, null, 8000, 60 * 60 * 1000).then(() => { toast('History built'); loadAll(); }).catch((e) => toast(e.message, 'error'));
  }, 'Start'));
  await loadAll();
  return { destroy: () => { charts.forEach((c) => c.destroy()); map.remove(); } };
}

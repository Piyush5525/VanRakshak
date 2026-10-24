import { api, html, raw, esc, fmt, tag, empty, toast, can, download, $, $$ } from '../core.js';
import { createMap, addGeo, fit, popup, legend } from '../map.js';

const EV = { optical_radar_agree: ['Optical + radar agree', '#b8321f'], optical: ['Optical only', '#d9822b'], radar_optical_blind: ['Radar (optical blind)', '#7a3fa0'], optical_radar_unsupported: ['Optical, radar unsupported', '#9aa5a0'], radar_only_optical_clear: ['Radar only', '#9aa5a0'] };
const MONSOON = ['06', '07', '08', '09'];
const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`);
const dd = (v) => (v == null ? '—' : `${v} d`);

export async function render(root) {
  const regions = await api('/regions');
  if (!regions.length) { root.innerHTML = empty('No regions yet', 'Create a monitoring region first.', '◔'); return; }
  let runs = [], run = null, alerts = [], chart = null, poll = null, month = null, mapObj = null, selectedChanges = [];
  const y = new Date().getFullYear() - 1;
  root.innerHTML = html`
    <div class="note-box"><b>Monthly alert service.</b> Every Sentinel-2 and Sentinel-1 acquisition in the year is scanned. A change must be seen in <i>two consecutive</i> clear observations to be confirmed, so each alert is issued when its confirming observation arrives and uses only data up to that date. During the monsoon, Sentinel-2 is often blind; Sentinel-1 radar keeps watching. Loss dates are the midpoint between the last unchanged and first changed observation, with the half-gap shown as the uncertainty. Alerts are ranked so a team can work the top 50. All alerts are model candidates, not findings.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="stack">
        <div class="panel"><h3>Run the service</h3>
          <form id="f" class="stack"><label class="f">Region<select name="r">${raw(regions.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join(''))}</select></label>
            <div class="grid g2"><label class="f">Year<input type="number" name="y" value="${y}" min="2017" max="2100"></label><label class="f">Months to alert<input type="number" name="n" value="12" min="1" max="12"></label></div>
            <label class="f">Baseline months before January<input type="number" name="b" value="3" min="2" max="6"></label>
            <label class="row"><input type="checkbox" name="radar" checked> <span class="help">Use Sentinel-1 radar (turn off to see the optical-only baseline)</span></label>
            <label class="row"><input type="checkbox" name="ho"> <span class="help">This region was not used to choose thresholds (held-out)</span></label>
            <div class="row"><button class="button primary" id="go" ${can('admin', 'reviewer', 'analyst') ? '' : 'disabled title="Analyst, reviewer or administrator role required"'}>Start run</button></div>
            <div class="help">First run for a year reads every scene (about 30–60 minutes for a 4 km block); repeats use the cache.</div></form></div>
        <div class="panel"><h3>Runs</h3><div id="runs"></div></div>
      </div>
      <div class="panel" id="summary"><h3>Select or start a run</h3></div>
    </div>
    <div class="panel" style="margin-top:16px"><div class="row"><div class="grow"><h3>Alert map</h3><p class="sub" id="mapSub"></p></div><div class="pill-btns" id="months"></div></div><div id="map" class="map-box tall"></div>${raw(legend(Object.values(EV).slice(0, 3).map(([t, c]) => [c, t])))}</div>
    <div class="panel" style="margin-top:16px"><div class="row"><div class="grow"><h3>Ranked alerts</h3><p class="sub">Score = evidence weight × √area × date-certainty. It orders work; it is not a probability.</p></div><button class="button subtle sm" id="csv">CSV</button><button class="button subtle sm" id="gj">GeoJSON</button></div><div id="tbl"></div></div>
    <div class="panel" style="margin-top:16px"><div class="row"><div class="grow"><h3>Evaluation against public alert dates</h3><p class="sub">Compares this run with GLAD-L alerts (Global Forest Watch). GLAD-L is an independent Landsat product, not field truth.</p></div></div>
      <div class="row"><label class="f" style="min-width:320px">Phase 1 two-date detection to compare with<select id="p1"><option value="">None (skip F1 comparison)</option></select></label><button class="button primary" id="ev" ${can('admin', 'reviewer', 'analyst') ? '' : 'disabled'}>Evaluate</button><span class="help" id="evSt"></span></div><div id="evOut" style="margin-top:12px"></div></div>`;
  const { map, group } = await createMap($('#map')); mapObj = map; const g = group('Alerts'), rg = group('Region');

  const regionOf = (id) => regions.find((r) => r.id === id);
  async function loadRuns() {
    runs = await api('/alerts/runs'); $('#runs').innerHTML = runs.length ? html`<table class="t"><tbody>${runs.map((r) => raw(`<tr data-id="${esc(r.id)}" class="${run?.id === r.id ? 'sel' : ''}"><td><b>${esc(regionOf(r.region_id)?.name || r.region_id.slice(0, 8))} · ${esc(r.year)}</b><div class="help">${esc(fmt.dt(r.created_at))} · ${r.use_radar ? 'S1+S2' : 'S2 only'}${r.held_out ? ' · held-out' : ''}</div></td><td>${tag(fmt.status(r.status), r.status === 'COMPLETE' ? 'ready' : r.status === 'FAILED' ? 'red' : 'configure').s}${r.status === 'RUNNING' ? `<div class="progress"><i style="width:${Math.round(r.progress * 100)}%"></i></div>` : ''}${r.summary ? `<div class="help">${esc(r.summary.alerts)} alerts</div>` : ''}</td></tr>`))}</tbody></table>` : empty('No runs yet', 'Start one to generate alerts.', '◔');
    $$('#runs tr[data-id]').forEach((tr) => (tr.onclick = () => select(tr.dataset.id)));
  }

  async function select(id) {
    clearInterval(poll); run = await api('/alerts/runs/' + id); await loadRuns();
    const reg = regionOf(run.region_id); rg.clearLayers(); const rl = addGeo(rg, reg.geometry, { color: '#1d6b4b', weight: 2, fillOpacity: 0.03 });
    if (run.status === 'RUNNING') {
      $('#summary').innerHTML = html`<h3>Running…</h3><div class="progress"><i style="width:${Math.round(run.progress * 100)}%"></i></div><pre class="help" style="white-space:pre-wrap;max-height:300px;overflow:auto">${(run.result?.log || []).join('\n')}</pre>`;
      fit(map, [rl]); poll = setInterval(async () => { const r = await api('/alerts/runs/' + id).catch(() => null); if (!r) return; if (r.status !== 'RUNNING') { clearInterval(poll); select(id); } else { $('#summary pre').textContent = (r.result?.log || []).join('\n'); $('#summary .progress i').style.width = Math.round(r.progress * 100) + '%'; } }, 6000); return;
    }
    if (run.status === 'FAILED') { $('#summary').innerHTML = html`<h3>Run failed</h3><div class="error-box"><strong>${run.result?.error || 'Unknown error'}</strong></div>`; return; }
    alerts = await api(`/alerts/runs/${id}/alerts?limit=1000`); month = null; paintSummary(); paintAlerts(); fit(map, [rl]);
    const ch = await api(`/forest/changes?region_id=${run.region_id}&limit=100`);
    $('#p1').innerHTML = '<option value="">None (skip F1 comparison)</option>' + ch.filter((c) => c.disturbance_type === 'CANDIDATE_FOREST_LOSS' || c.status === 'NO_CANDIDATES').map((c) => `<option value="${esc(c.id)}">${esc(fmt.date(c.baseline_date))} → ${esc(fmt.date(c.comparison_date))} · ${esc(fmt.ha(c.area_hectares))}</option>`).join('');
    const evs = await api(`/alerts/runs/${id}/evaluations`); $('#evOut').innerHTML = evs.length ? '' : ''; if (evs.length) showEval(evs[0]);
  }

  function paintSummary() {
    const r = run.result, s = r.summary, bm = r.by_month, cov = r.data_coverage, months = Object.keys(cov);
    const mixed = {}; alerts.forEach((a) => (mixed[a.evidence] = (mixed[a.evidence] || 0) + 1));
    $('#summary').innerHTML = html`<h3>${regionOf(run.region_id)?.name} · ${run.year}</h3><p class="sub">${run.params.use_radar ? 'Sentinel-2 + Sentinel-1' : 'Sentinel-2 only'} · ${r.n_optical_dates} optical and ${r.n_radar_dates} radar acquisition dates · parameter set ${run.params.digest}${run.held_out ? ' · declared held-out' : ''}</p>
      <div class="grid g4"><div class="stat"><b>${s.alerts}</b><span>Alerts issued</span></div><div class="stat"><b>${fmt.ha(s.area_ha)}</b><span>Total area</span></div><div class="stat"><b>${alerts.filter((a) => a.evidence === 'radar_optical_blind' || a.evidence === 'optical_radar_agree').length}</b><span>With radar evidence</span></div><div class="stat"><b>${s.suppressed_low_confidence_pixels}</b><span>Low-confidence pixels withheld</span></div></div>
      <div class="chart-box" style="height:250px;margin-top:10px"><canvas id="mc"></canvas></div>
      <div class="help">Bars: alerts issued per month. Line: share of the region visible to Sentinel-2 (clear pixels). The monsoon dip is why radar is needed.</div>
      ${raw(Object.keys(mixed).length ? `<div class="row" style="margin-top:8px">${Object.entries(mixed).map(([k, v]) => tag(`${EV[k]?.[0] || k}: ${v}`, 'neutral').s).join(' ')}</div>` : '')}`;
    chart?.destroy();
    const clear = months.map((m) => cov[m].mean_clear_fraction);
    chart = new Chart($('#mc'), { data: { labels: months, datasets: [{ type: 'bar', label: 'Alerts issued', data: months.map((m) => bm[m]?.alerts || 0), backgroundColor: months.map((m) => (MONSOON.includes(m.slice(5)) ? '#7a3fa0' : '#c73b25')), yAxisID: 'y' }, { type: 'line', label: 'Sentinel-2 clear fraction', data: clear, borderColor: '#2f7d4f', yAxisID: 'y2', tension: 0.25 }] }, options: { maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: 'alerts' }, ticks: { precision: 0 } }, y2: { position: 'right', min: 0, max: 1, grid: { drawOnChartArea: false }, title: { display: true, text: 'clear share' } } }, plugins: { legend: { position: 'bottom' } } } });
    $('#months').innerHTML = html`<button class="on" data-m="">All</button>${months.map((m) => raw(`<button data-m="${esc(m)}" title="${esc(cov[m].optical_dates)} optical / ${esc(cov[m].radar_dates)} radar dates">${esc(m.slice(5))}${MONSOON.includes(m.slice(5)) ? '☂' : ''}</button>`))}`;
    $('#months').onclick = (e) => { if (e.target.dataset.m === undefined) return; month = e.target.dataset.m || null; $$('#months button').forEach((b) => b.classList.toggle('on', b === e.target)); paintAlerts(); };
  }

  function paintAlerts() {
    g.clearLayers(); const rows = alerts.filter((a) => !month || a.issue_month === month); const ls = [];
    rows.forEach((a) => { const [t, c] = EV[a.evidence] || [a.evidence, '#888']; ls.push(addGeo(g, a.geometry, { color: c, weight: 2, fillColor: c, fillOpacity: 0.55 }, popup(`Alert #${a.rank} · ${t}`, [`${a.area_ha} ha`, `Loss date ≈ ${a.est_date} ± ${a.date_uncertainty_days} d`, `Issued ${a.issue_date}`, 'Candidate — verify before acting']))); });
    $('#mapSub').textContent = `${rows.length} alert(s)${month ? ' issued in ' + month : ''}. Click a polygon for its estimated loss date.`;
    if (ls.length) fit(map, ls, { maxZoom: 16 });
    $('#tbl').innerHTML = rows.length ? html`<table class="t"><thead><tr><th>#</th><th>Issued</th><th>Estimated loss date</th><th>Area</th><th>Evidence</th><th>Score</th><th></th></tr></thead><tbody>${rows.slice(0, 50).map((a) => raw(`<tr><td>${esc(a.rank)}</td><td>${esc(a.issue_date)}${a.monsoon ? ' ☂' : ''}</td><td>${esc(a.est_date)} <span class="help">± ${esc(a.date_uncertainty_days)} d</span></td><td>${esc(a.area_ha)} ha</td><td>${tag(EV[a.evidence]?.[0] || a.evidence, a.evidence === 'optical_radar_agree' ? 'red' : 'configure').s}</td><td>${esc(a.score)}</td><td class="row"><button class="button subtle sm" data-z="${esc(a.id)}">Zoom</button>${can('admin', 'reviewer') ? `<button class="button subtle sm" data-c="${esc(a.id)}">Open case</button>` : ''}</td></tr>`))}</tbody></table>${raw(rows.length > 50 ? `<div class="help">Showing the top 50 of ${rows.length}.</div>` : '')}` : empty('No alerts', month ? 'No alerts were issued this month.' : 'Nothing met the confirmation rules.', '◔');
    $$('#tbl [data-z]').forEach((b) => (b.onclick = () => { const a = alerts.find((x) => x.id === b.dataset.z); map.fitBounds(L.geoJSON(a.geometry).getBounds().pad(2), { maxZoom: 17 }); }));
    $$('#tbl [data-c]').forEach((b) => (b.onclick = async () => { const a = alerts.find((x) => x.id === b.dataset.c); try { const i = await api('/incidents', { method: 'POST', body: { title: `Monthly alert #${a.rank}: ${a.area_ha} ha, loss ≈ ${a.est_date} (${EV[a.evidence]?.[0] || a.evidence})`, geometry: a.geometry, evidence: { claim_type: 'candidate_inference', alert_id: a.id, run_id: run.id, estimated_loss_date: a.est_date, date_uncertainty_days: a.date_uncertainty_days } } }); toast('Case opened for review'); location.hash = '#/investigations/' + i.id; } catch (e) { toast(e.message, 'error'); } }));
  }

  function showEval(e) {
    const f = e.fused_s1_s2, o = e.optical_only_s2, gn = e.radar_gain, ph = e.phase1_check, d = f.date_estimate_error, p50 = f.patches.precision_at_k;
    const card = (n, title, ok, body) => `<div class="panel" style="box-shadow:none"><div class="row"><h4 class="grow">${n}. ${esc(title)}</h4>${ok === 'measured' ? tag('measured', 'neutral').s : ok === null ? tag('not computed', 'neutral').s : ok ? tag('met', 'ready').s : tag('not met', 'red').s}</div>${body}</div>`;
    const seasonRow = (name, label) => `<tr><td>${label}</td><td>${esc(gn[name].fused.precision != null ? pct(gn[name].fused.precision) : '—')} / ${esc(pct(gn[name].optical_only.precision))}</td><td>${esc(pct(gn[name].fused.recall))} / ${esc(pct(gn[name].optical_only.recall))}</td><td><b>${esc(pct(gn[name].fused.timely_recall_45d))}</b> / ${esc(pct(gn[name].optical_only.timely_recall_45d))}</td><td>${esc(dd(f.season[name].issue_delay_days.median_abs_error_days))} / ${esc(dd(o.season[name].issue_delay_days.median_abs_error_days))}</td></tr>`;
    const covered = Object.keys(run.result.data_coverage).length;
    $('#evOut').innerHTML = html`<div class="grid g2">
      ${raw(card(1, 'Monthly alerts for a full year, including monsoon', covered === 12 ? true : null, `<p>${covered} of 12 months processed; ${Object.keys(run.result.data_coverage).filter((m) => MONSOON.includes(m.slice(5))).length} monsoon months. ${run.result.summary.alerts} alerts issued.</p><p class="help">${covered === 12 ? '' : 'Run all 12 months to meet this item.'}</p>`))}
      ${raw(card(2, 'Radar gain in the monsoon (fused S1+S2 vs S2 only)', gn.monsoon.timely_recall_45d_gain == null ? null : gn.monsoon.timely_recall_45d_gain > 0, `<table class="t"><thead><tr><th></th><th>Precision</th><th>Eventual recall</th><th>Recall within 45 d</th><th>Median delay</th></tr></thead><tbody>${seasonRow('monsoon', 'Jun–Sep ☂')}${seasonRow('non_monsoon', 'Other months')}</tbody></table><p class="help">Each cell: with radar / optical only. Reference loss in monsoon: ${esc(f.season.monsoon.reference_px)} px; outside: ${esc(f.season.non_monsoon.reference_px)} px. Gain in timely recall (monsoon): <b>${esc(gn.monsoon.timely_recall_45d_gain == null ? '—' : (gn.monsoon.timely_recall_45d_gain * 100).toFixed(0) + ' points')}</b></p>`))}
      ${raw(card(3, 'Date of loss: median error against public alert dates', d.median_abs_error_days == null ? null : 'measured', `<dl class="kv"><dt>Median absolute error</dt><dd><b>${esc(dd(d.median_abs_error_days))}</b> (n = ${esc(d.n)} pixels)</dd><dt>Median signed error</dt><dd>${esc(dd(d.median_signed_error_days))} (positive = we date it later than GLAD-L)</dd><dt>90th percentile</dt><dd>${esc(dd(d.p90_abs_error_days))}</dd></dl><p class="help">GLAD-L dates are Landsat detection dates and carry their own timing error.</p>`))}
      ${raw(card(4, `Precision of the top ${p50.k}`, p50.spatial == null ? null : 'measured', `<dl class="kv"><dt>Spatially confirmed</dt><dd><b>${esc(pct(p50.spatial))}</b> of top ${esc(p50.k)}${p50.k < p50.k_requested ? ` (only ${esc(p50.k)} alerts exist; requested ${esc(p50.k_requested)})` : ''}</dd><dt>…and dated within 60 days</dt><dd>${esc(pct(p50.spatial_and_date_within_60d))}</dd><dt>All alerts</dt><dd>${esc(pct(f.patches.precision_all))} of ${esc(f.patches.n)}</dd></dl><p class="help">${f.patches.reliable_sample ? '' : 'Small sample: not statistically reliable.'}</p>`))}
      ${raw(card(5, 'Phase 1 F1 kept within 5 points', ph.status || (ph.phase1_two_date_optical.f1 ?? 0) < 0.05 ? (ph.status ? null : 'measured') : ph.within_5_points, ph.status ? `<div class="warn-box">${esc(ph.reason)}</div>` : `<dl class="kv"><dt>Phase 1 (two-date optical) F1</dt><dd>${esc(pct(ph.phase1_two_date_optical.f1))}</dd><dt>Phase 2 (all alerts) F1</dt><dd>${esc(pct(ph.phase2_all_alerts.f1))}</dd><dt>Difference</dt><dd><b>${esc(ph.delta_f1_points)} points</b></dd></dl>${(ph.phase1_two_date_optical.f1 ?? 0) < 0.05 ? '<div class="warn-box">Phase 1 F1 is under 5%, so staying within 5 points is not informative here. Needs a region with enough reference loss.</div>' : ''}`))}
    </div>
    <div class="note-box" style="margin-top:10px"><b>Reference:</b> ${esc(e.reference.source)} · ${esc(e.reference.reference_alert_pixels_in_forest)} reference pixels in forest, ${esc(e.reference.reference_patches)} patches, ${esc(e.reference.period[0])} to ${esc(e.reference.period[1])} · parameter set ${esc(e.params_digest)}${e.held_out_declared ? ' · region declared held-out' : ' · region NOT declared held-out'}</div>
    ${raw(e.warnings.map((w) => `<div class="warn-box" style="margin-top:8px">${esc(w)}</div>`).join(''))}`;
  }

  $('#go').onclick = async (ev) => {
    ev.preventDefault(); const f = new FormData($('#f'));
    try { const r = await api('/alerts/runs', { method: 'POST', body: { region_id: f.get('r'), year: +f.get('y'), n_months: +f.get('n'), ref_months: +f.get('b'), use_radar: !!f.get('radar'), held_out: !!f.get('ho') } }); toast('Run started; it continues in the background'); await select(r.run_id); } catch (e) { toast(e.message, 'error'); }
  };
  $('#ev').onclick = async () => {
    if (!run || run.status !== 'COMPLETE') return toast('Select a completed run first', 'error');
    $('#ev').disabled = true; $('#evSt').textContent = 'Fetching reference alerts and scoring…';
    try { const j = await api(`/alerts/runs/${run.id}/evaluate`, { method: 'POST', body: { phase1_change_id: $('#p1').value || null, top_k: 50 } }); for (;;) { const job = await api('/jobs/' + j.job_id); if (job.status === 'completed') { showEval(job.result); break; } if (job.status === 'failed') throw new Error(job.error?.message); await new Promise((r) => setTimeout(r, 2500)); } $('#evSt').textContent = ''; } catch (e) { toast(e.message, 'error'); $('#evSt').textContent = e.message; } finally { $('#ev').disabled = false; }
  };
  $('#csv').onclick = () => run && download(`/alerts/runs/${run.id}/export.csv`, `rakshak-alerts-${run.id.slice(0, 8)}.csv`);
  $('#gj').onclick = () => run && download(`/alerts/runs/${run.id}/export.geojson`, `rakshak-alerts-${run.id.slice(0, 8)}.geojson`);
  await loadRuns(); fit(map, [addGeo(rg, regions[0].geometry, { color: '#1d6b4b', weight: 2, fillOpacity: 0.03 })]);
  if (runs[0]) await select(runs[0].id);
  return { destroy: () => { clearInterval(poll); chart?.destroy(); map.remove(); } };
}

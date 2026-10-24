import { api, html, raw, esc, fmt, tag, empty, toast, dialog, can, kv, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup, legend } from '../map.js';

export async function render(root) {
  const [regions, vision] = await Promise.all([api('/regions'), api('/wildfires/vision/status')]);
  let fires = await api('/wildfires'), clusters = await api('/wildfires/correlations'), region = regions[0] || null, sel = null;
  root.innerHTML = html`
    <div class="note-box">Two independent pipelines feed one review queue: <b>satellite thermal anomalies</b> (NASA FIRMS, VIIRS/MODIS) and <b>camera fire/smoke detection</b>. A thermal anomaly is not a confirmed wildfire and a camera detection can be fog, dust or glare. Records are only grouped when they are close in space and time; they are never merged automatically.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel"><div class="row"><div class="grow"><h3>Fire map</h3></div><div class="pill-btns" id="src"><button data-s="all" class="on">All</button><button data-s="sat">Satellite</button><button data-s="cam">Camera</button></div></div><div id="map" class="map-box tall"></div>${raw(legend([['#ff6a2b', 'Satellite thermal anomaly'], ['#a47bd4', 'Camera detection (camera position)'], ['#1d6b4b', 'Region']]))}</div>
      <div class="stack">
        <div class="panel"><h3>Satellite ingestion (NASA FIRMS)</h3>${regions.length ? raw(`<div class="stack"><label class="f">Region<select id="fr">${regions.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')}</select></label><div class="grid g2"><label class="f">Instrument<select id="fs"><option value="VIIRS_SNPP_NRT">VIIRS S-NPP (375 m)</option><option value="VIIRS_NOAA20_NRT">VIIRS NOAA-20 (375 m)</option><option value="VIIRS_NOAA21_NRT">VIIRS NOAA-21 (375 m)</option><option value="MODIS_NRT">MODIS (1 km)</option></select></label><label class="f">Days back<select id="fd"><option>1</option><option selected>2</option><option>3</option><option>5</option></select></label></div><button class="button primary" id="ing" ${can('admin', 'reviewer') ? '' : 'disabled'}>Fetch detections</button><span class="help" id="ingOut"></span></div>`) : raw(empty('Add a region first', 'FIRMS queries use your region as the search box.', '♨'))}</div>
        <div class="panel"><h3>Camera / video detection</h3>${vision.usable ? raw(`<div class="note-box">Model: ${esc(vision.model)} · licence ${esc(vision.licence)}</div><form id="cam" class="stack"><label class="f">Image frame<input type="file" name="file" accept="image/*" required></label><div class="grid g2"><label class="f">Camera ID<input type="text" name="camera_id" required></label><label class="f">Bearing °<input type="number" name="bearing_deg" min="0" max="360"></label><label class="f">Camera latitude<input type="number" step="any" name="lat" required></label><label class="f">Camera longitude<input type="number" step="any" name="lon" required></label></div><button class="button primary">Analyse frame</button><div class="help">A candidate is raised only after 3 of the last 5 frames from the same camera show fire or smoke.</div><div id="camOut"></div></form>`) : raw(`<div class="warn-box"><b>Computer-vision detection is not configured.</b><br>${esc(vision.reason)}<br><span class="help">Set FIRE_MODEL_PATH (and FIRE_MODEL_META) to an ONNX detector whose licence you have verified. Nothing is simulated meanwhile.</span></div>`)}</div>
        <div class="panel"><h3>Possible same-event groups</h3><div id="clusters"></div></div>
      </div>
    </div>
    <div class="panel" style="margin-top:16px"><h3>Fire records</h3><div id="tbl"></div></div>`;
  const { map, group } = await createMap($('#map'));
  const fg = group('Fire records'), rg = group('Regions');
  let filter = 'all';

  function paint() {
    fg.clearLayers(); rg.clearLayers();
    const ls = regions.map((r) => addGeo(rg, r.geometry, STYLE.region));
    const rows = fires.filter((f) => filter === 'all' || (filter === 'cam') === (f.observation_type === 'camera_detection'));
    rows.forEach((f) => ls.push(addGeo(fg, f.geometry, f.observation_type === 'camera_detection' ? STYLE.camera : STYLE.fire, popup(fmt.status(f.observation_type), [f.source, fmt.dt(f.observed_at), f.confidence != null ? `Source confidence ${fmt.pct(f.confidence)}` : 'No confidence value', `Review: ${fmt.status(f.status)}`]), { radius: 6 })));
    if (!sel) fit(map, ls, { padding: [30, 30], maxZoom: 12 });
    $('#tbl').innerHTML = rows.length ? html`<table class="t"><thead><tr><th>Observed</th><th>Type</th><th>Source</th><th>Confidence</th><th>Status</th><th></th></tr></thead><tbody>${rows.slice(0, 60).map((f) => raw(`<tr data-id="${esc(f.id)}"><td>${esc(fmt.dt(f.observed_at))}</td><td>${esc(fmt.status(f.observation_type))}${f.evidence?.frp_mw ? `<div class="help">FRP ${esc(f.evidence.frp_mw)} MW</div>` : ''}${f.evidence?.location_kind ? `<div class="help">${esc(f.evidence.location_kind)}</div>` : ''}</td><td>${esc(f.source)}</td><td>${esc(f.confidence != null ? fmt.pct(f.confidence) : '—')}</td><td>${tag(fmt.status(f.status), f.status === 'CONFIRMED' ? 'red' : f.status === 'DISMISSED' ? 'neutral' : 'configure').s}</td><td class="row">${f.status === 'NEEDS_REVIEW' && can('admin', 'reviewer', 'officer') ? `<button class="button subtle sm" data-r="confirm" data-i="${esc(f.id)}">Confirm</button><button class="button subtle sm" data-r="dismiss" data-i="${esc(f.id)}">Dismiss</button>` : ''}${f.status === 'CONFIRMED' && can('admin', 'reviewer', 'officer') ? `<button class="button primary sm" data-inc="${esc(f.id)}">Open incident</button>` : ''}</td></tr>`))}</tbody></table>${rows.length > 60 ? `<div class="help">Showing 60 of ${rows.length}.</div>` : ''}` : empty('No fire records', 'Fetch FIRMS detections for a region or analyse a camera frame.', '♨');
    $$('#tbl [data-r]').forEach((b) => (b.onclick = async () => { try { await api(`/wildfires/${b.dataset.i}/review`, { method: 'POST', body: { decision: b.dataset.r, notes: '' } }); fires = await api('/wildfires'); toast('Review recorded'); paint(); window.dispatchEvent(new Event('rk:changed')); } catch (e) { toast(e.message, 'error'); } }));
    $$('#tbl [data-inc]').forEach((b) => (b.onclick = async () => { try { const r = await api(`/wildfires/${b.dataset.inc}/create-incident`, { method: 'POST' }); toast('Incident opened for review'); location.hash = '#/investigations/' + r.incident_id; } catch (e) { toast(e.message, 'error'); } }));
    $$('#tbl tr[data-id]').forEach((tr) => (tr.ondblclick = () => { const f = fires.find((x) => x.id === tr.dataset.id); sel = f; map.setView([f.geometry.coordinates[1], f.geometry.coordinates[0]], 12); }));
    $('#clusters').innerHTML = clusters.length ? html`<table class="t"><tbody>${clusters.filter((c) => c.member_ids.length > 1).slice(0, 8).map((c) => raw(`<tr><td>${c.multi_source ? tag('camera + satellite', 'purple').s : ''} ${esc(c.member_ids.length)} records<div class="help">${esc(fmt.dt(c.first_observed))} → ${esc(fmt.dt(c.last_observed))} · ${esc(c.criteria)}</div></td></tr>`))}</tbody></table><div class="help">${esc(clusters[0].caveat)}</div>` : empty('Nothing to group', 'No nearby records.', '◌');
    if (!clusters.some((c) => c.member_ids.length > 1) && clusters.length) $('#clusters').innerHTML = empty('No groups', 'No records are close enough in space and time to be grouped.', '◌');
  }

  $('#src').onclick = (e) => { const s = e.target.dataset.s; if (!s) return; filter = s; $$('#src button').forEach((b) => b.classList.toggle('on', b === e.target)); paint(); };
  $('#ing')?.addEventListener('click', async () => {
    const b = $('#ing'); b.disabled = true; $('#ingOut').textContent = 'Querying FIRMS…';
    try { const r = await api('/wildfires/ingest-firms', { method: 'POST', body: { region_id: $('#fr').value, source: $('#fs').value, days: +$('#fd').value } }); $('#ingOut').textContent = `${r.received} detection(s): ${r.inserted} new, ${r.duplicates} already stored.`; fires = await api('/wildfires'); clusters = await api('/wildfires/correlations'); paint(); window.dispatchEvent(new Event('rk:changed')); }
    catch (e) { $('#ingOut').textContent = e.message; toast(e.message, 'error'); } finally { b.disabled = false; }
  });
  $('#cam')?.addEventListener('submit', async (e) => {
    e.preventDefault(); const fd = new FormData(e.target);
    try { const r = await api('/wildfires/vision/analyze', { method: 'POST', form: fd }); $('#camOut').innerHTML = html`<div class="${r.candidate_incident_created ? 'warn-box' : 'note-box'}">${r.detections.length ? r.detections.map((d) => `${d.class} ${(d.confidence * 100).toFixed(0)}%`).join(', ') : 'No fire or smoke detected in this frame.'}<br>Temporal filter: ${r.temporal_filter.hits} of last ${r.temporal_filter.window} frames (needs ${r.temporal_filter.required}).<br>${r.candidate_incident_created ? 'Candidate raised for review.' : 'No candidate raised yet.'}</div>`; fires = await api('/wildfires'); paint(); }
    catch (err) { toast(err.message, 'error'); }
  });
  paint();
  return { destroy: () => map.remove() };
}

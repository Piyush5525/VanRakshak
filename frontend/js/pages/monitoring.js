import { api, html, raw, esc, fmt, tag, empty, toast, dialog, can, waitJob, $, $$ } from '../core.js';
import { createMap, addGeo, fit, drawShape, STYLE, popup } from '../map.js';

export async function render(root) {
  let regions = await api('/regions');
  let selected = null, drawn = null, overlay = null;
  root.innerHTML = html`
    <div class="grid split">
      <div class="panel"><div class="row"><div class="grow"><h3>Select or draw a study area</h3><p class="sub">Draw a polygon or rectangle on the map, or pick a saved region. Regions are limited to about 40 km² for satellite processing.</p></div>
        <button class="button subtle" id="drawPoly">Draw polygon</button><button class="button subtle" id="drawRect">Draw rectangle</button></div>
        <div id="map" class="map-box tall"></div>
        <div id="overlayCtl" class="row" style="margin-top:10px"></div></div>
      <div class="stack">
        <div class="panel" id="drawnPanel" hidden><h3>New region</h3>
          <form id="newRegion" class="stack"><label class="f">Name<input type="text" name="name" required minlength="3" placeholder="e.g. Wayanad forest block"></label><label class="f">Jurisdiction<input type="text" name="jurisdiction" required placeholder="State · District"></label><div id="drawnInfo" class="help"></div><div class="row"><button class="button primary" type="submit">Save region</button><button class="button subtle" type="button" id="discard">Discard</button></div></form></div>
        <div class="panel"><h3>Monitored regions</h3><div id="regionList"></div></div>
        <div class="panel" id="detail"><h3>Region detail</h3><p class="sub">Select a region to see its satellite observations.</p></div>
      </div>
    </div>`;
  const { map, group } = await createMap($('#map'));
  const regionLayer = group('Regions'), drawLayer = group('New region'), obsLayer = group('Satellite preview');

  const draw = async (kind) => {
    $('#drawPoly').disabled = $('#drawRect').disabled = true;
    const g = await drawShape(map, kind);
    $('#drawPoly').disabled = $('#drawRect').disabled = false;
    if (!g) return;
    drawn = g; drawLayer.clearLayers(); addGeo(drawLayer, g, { color: '#bf731f', weight: 3, fillOpacity: 0.08 });
    $('#drawnPanel').hidden = false;
    $('#drawnInfo').textContent = `${g.coordinates[0].length - 1} vertices drawn. Area is computed on the server when you save.`;
  };
  $('#drawPoly').onclick = () => draw('polygon'); $('#drawRect').onclick = () => draw('rectangle');
  $('#discard').onclick = () => { drawn = null; drawLayer.clearLayers(); $('#drawnPanel').hidden = true; };
  $('#newRegion').onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target);
    try { const r = await api('/regions', { method: 'POST', body: { name: f.get('name'), jurisdiction: f.get('jurisdiction'), geometry: drawn } }); toast('Region saved'); drawn = null; drawLayer.clearLayers(); $('#drawnPanel').hidden = true; e.target.reset(); regions = await api('/regions'); paint(); select(r.id); window.dispatchEvent(new Event('rk:changed')); } catch (err) { toast(err.message, 'error'); }
  };

  function paint() {
    regionLayer.clearLayers();
    const ls = regions.map((r) => addGeo(regionLayer, r.geometry, STYLE.region, popup(r.name, [r.jurisdiction])));
    $('#regionList').innerHTML = regions.length ? html`<table class="t"><tbody>${regions.map((r) => raw(`<tr data-id="${esc(r.id)}" class="${selected?.id === r.id ? 'sel' : ''}"><td><b>${esc(r.name)}</b><div class="help">${esc(r.jurisdiction)}</div></td><td>${esc(fmt.date(r.created_at))}</td></tr>`))}</tbody></table>` : empty('No regions yet', 'Draw an area on the map and save it to start monitoring.', '⌖');
    $$('#regionList tr[data-id]').forEach((tr) => (tr.onclick = () => select(tr.dataset.id)));
    if (!selected && ls.length) fit(map, ls);
  }

  async function select(id) {
    selected = regions.find((r) => r.id === id); paint();
    const bounds = L.geoJSON(selected.geometry).getBounds(); map.fitBounds(bounds, { padding: [30, 30] });
    const obs = await api(`/regions/${id}/observations-ok`);
    $('#detail').innerHTML = html`<h3>${selected.name}</h3><p class="sub">${selected.jurisdiction}</p>
      <h4>Satellite observations</h4>${obs.length ? raw(`<table class="t"><thead><tr><th>Sensor</th><th>Acquired</th><th>Clear</th><th></th></tr></thead><tbody>${obs.map((o) => `<tr><td>${esc(o.sensor)}<div class="help">${o.scenes} scene(s)</div></td><td>${esc(fmt.date(o.acquired_from))}${o.acquired_to !== o.acquired_from ? ' → ' + esc(fmt.date(o.acquired_to)) : ''}</td><td>${esc(fmt.pct(o.valid_pixel_fraction))}</td><td><button class="button subtle sm" data-obs="${esc(o.id)}">Show</button></td></tr>`).join('')}</tbody></table>`) : raw(empty('No observations stored', 'Fetch imagery below, or run change detection (it fetches and caches composites).', '◫'))}
      ${can('admin', 'reviewer', 'analyst') ? raw('<div class="row" style="margin-top:12px"><button class="button primary" id="fetchObs">Fetch imagery…</button></div>') : ''}`;
    $$('[data-obs]').forEach((b) => (b.onclick = () => showObs(b.dataset.obs)));
    $('#fetchObs')?.addEventListener('click', fetchDialog);
  }

  async function showObs(oid) {
    try {
      const m = await api(`/observations/${oid}/preview-meta`);
      const blob = await api(`/observations/${oid}/preview.png`, { blob: true });
      obsLayer.clearLayers(); overlay = L.imageOverlay(URL.createObjectURL(blob), m.bounds, { opacity: 0.85 }).addTo(obsLayer);
      map.fitBounds(m.bounds);
      $('#overlayCtl').innerHTML = html`<span class="tag ready">${m.sensor}</span> <span class="help">${raw(esc(m.legend))}</span><label class="help">Opacity <input type="range" min="0.2" max="1" step="0.05" value="0.85" id="op"></label><button class="button subtle sm" id="hide">Hide</button>`;
      $('#op').oninput = (e) => overlay.setOpacity(+e.target.value); $('#hide').onclick = () => { obsLayer.clearLayers(); $('#overlayCtl').innerHTML = ''; };
    } catch (e) { toast(e.message, 'error'); }
  }

  function fetchDialog() {
    dialog('Fetch satellite imagery', `<label class="f">Sensor<select name="sensor"><option value="sentinel-2">Sentinel-2 L2A (optical)</option><option value="sentinel-1">Sentinel-1 RTC (SAR)</option></select></label>
      <label class="f">From<input type="date" name="from" required></label><label class="f">To<input type="date" name="to" required></label>
      <label class="f">Max cloud cover % (Sentinel-2)<input type="number" name="cloud" value="60" min="0" max="100"></label>
      <div class="help">Builds a median composite of clear observations in the window. Takes 1–5 minutes; you can keep working.</div>`, async (f) => {
      const job = await api(`/regions/${selected.id}/observations`, { method: 'POST', body: { provider: 'planetary_computer', acquisition_start: f.get('from') + 'T00:00:00Z', acquisition_end: f.get('to') + 'T00:00:00Z', processing_config: { sensor: f.get('sensor'), max_cloud_cover: +f.get('cloud') } } });
      toast('Imagery job started');
      waitJob(job.job_id, null, 4000).then((r) => { toast(r.status === 'OK' ? `Composite ready: ${r.source_scene_ids.length} scene(s)` : `Finished with status ${r.status}: ${(r.provenance.warnings || []).join('; ') || 'no scenes in window'}`, r.status === 'OK' ? 'info' : 'error'); select(selected.id); }).catch((e) => toast(e.message, 'error'));
    }, 'Start');
  }

  paint();
  return { destroy: () => map.remove() };
}

import { api, html, raw, esc, fmt, tag, empty, toast, can, $ } from '../core.js';

export async function render(root) {
  const paint = (d) => {
    root.innerHTML = html`
      <div class="note-box">Live reachability checks (cached 5 minutes) for every external source RAKSHAK uses. “Configured” means a key or setting exists; keys are never shown. A source that is down degrades only the features that depend on it.</div>
      <div class="panel" style="margin-top:14px"><div class="row"><div class="grow"><h3>Source health</h3><p class="sub">Checked ${fmt.dt(d.checked_at)}${d.cached ? ' (cached)' : ''}</p></div>${can('admin') ? raw('<button class="button subtle" id="re">Re-check now</button>') : ''}</div>
        <table class="t"><thead><tr><th>Source</th><th>Used for</th><th>Configured</th><th>Reachable</th><th>Latency</th><th>Detail</th></tr></thead><tbody>${d.sources.map((s) => raw(`<tr><td><b>${esc(s.name)}</b></td><td>${esc(s.purpose)}</td><td>${s.configured ? tag('yes', 'ready').s : tag('no', 'neutral').s}</td><td>${s.reachable === null ? tag('not checked', 'neutral').s : s.reachable ? tag('up', 'ready').s : tag('down', 'red').s}</td><td>${s.latency_ms != null ? esc(s.latency_ms) + ' ms' : '—'}</td><td class="help">${esc(s.detail)}</td></tr>`))}</tbody></table></div>
      <div class="panel" style="margin-top:14px"><h3>Last data updates</h3>${raw(`<dl class="kv"><dt>Satellite composite stored</dt><dd>${esc(fmt.dt(d.last_updates.satellite_composite))}</dd><dt>Change detection run</dt><dd>${esc(fmt.dt(d.last_updates.change_detection))}</dd><dt>Fire records ingested</dt><dd>${esc(fmt.dt(d.last_updates.fire_ingest))}</dd></dl>`)}</div>
      <div class="panel" style="margin-top:14px"><h3>Attribution</h3><ul class="steps"><li>Contains modified Copernicus Sentinel-1 and Sentinel-2 data, accessed through the Microsoft Planetary Computer.</li><li>ESA WorldCover 2021 © ESA (CC BY 4.0). Copernicus DEM GLO-30 © DLR / ESA.</li><li>Active-fire data: NASA FIRMS (LANCE).</li><li>Weather and air quality: Open-Meteo.com (CC BY 4.0).</li><li>Map data © OpenStreetMap contributors (ODbL). Directions: Mapbox; fallback Google Maps data via SerpAPI.</li></ul></div>`;
    $('#re')?.addEventListener('click', async () => { $('#re').disabled = true; try { paint(await api('/data-sources?force=true')); } catch (e) { toast(e.message, 'error'); } });
  };
  paint(await api('/data-sources'));
}

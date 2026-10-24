import { api, html, raw, esc, fmt, tag, classTag, empty, toast, dialog, can, waitJob, download, $, $$ } from '../core.js';
import { createMap, addGeo, fit, drawShape, STYLE, popup } from '../map.js';

export async function render(root) {
  let sites = await api('/industrial/sites'), regions = await api('/regions'), sel = null, drawn = null;
  root.innerHTML = html`
    <div class="note-box">Satellite imagery alone cannot show that a factory exists or what it emits. Sites enter either from a user-registered permit or field report, or as a <b>potential land-use change — verification required</b> promoted from a detection. The assessment reports proximity and overlap facts; it never states that a site caused pollution or damage.</div>
    <div class="grid split" style="margin-top:14px">
      <div class="panel"><div class="row"><div class="grow"><h3>Sites</h3></div>${can('admin', 'reviewer', 'analyst') ? raw('<button class="button subtle sm" id="draw">Draw a site</button>') : ''}</div><div id="map" class="map-box tall"></div></div>
      <div class="stack"><div class="panel" id="newSite" hidden><h4>Register drawn site</h4><form id="nf" class="stack"><label class="f">Name<input type="text" name="name" required minlength="2"></label><label class="f">Type<select name="kind"><option value="unclassified">Unclassified</option><option value="factory">Factory</option><option value="mining">Mining</option><option value="quarry">Quarry</option><option value="construction">Construction</option><option value="road">Road</option><option value="power">Power</option><option value="other">Other</option></select></label><label class="f">Source of information<select name="source"><option value="user_drawn">Drawn from imagery (unverified)</option><option value="permit_record">Permit / clearance record</option><option value="field_report">Field report</option></select></label><label class="f">Permit reference (optional)<input type="text" name="ref"></label><div class="row"><button class="button primary">Register</button><button type="button" class="button subtle" id="cancel">Cancel</button></div></form></div>
        <div class="panel"><h4>Registered sites</h4><div id="list"></div></div>
        <div class="panel" id="detail"><h4>Assessment</h4><p class="sub">Select a site.</p></div></div>
    </div>`;
  const { map, group } = await createMap($('#map')); const g = group('Sites'), d = group('Drawing');

  function paint() {
    g.clearLayers(); const ls = sites.map((s) => addGeo(g, s.geometry, STYLE.site, popup(s.name, [fmt.status(s.status)])));
    $('#list').innerHTML = sites.length ? html`<table class="t"><tbody>${sites.map((s) => raw(`<tr data-id="${esc(s.id)}" class="${sel?.id === s.id ? 'sel' : ''}"><td><b>${esc(s.name)}</b><div class="help">${esc(fmt.status(s.kind))} · ${esc(fmt.status(s.status))}</div></td></tr>`))}</tbody></table>` : empty('No sites', 'Draw a site, or flag a detection as a potential land-use change from Change Detection.', '▥');
    $$('#list tr[data-id]').forEach((tr) => (tr.onclick = () => select(sites.find((s) => s.id === tr.dataset.id))));
    if (!sel) fit(map, ls, { maxZoom: 13 });
  }

  function select(s) {
    sel = s; paint(); map.fitBounds(L.geoJSON(s.geometry).getBounds().pad(1.5), { maxZoom: 15 });
    const a = s.result;
    $('#detail').innerHTML = html`<h4>${s.name}</h4><div class="row">${tag(fmt.status(s.status), s.source === 'detected_change' ? 'configure' : 'neutral')}${can('admin', 'reviewer', 'analyst', 'officer') ? raw('<button class="button primary sm" id="assess">Run assessment</button>') : ''}${a ? raw('<button class="button subtle sm" id="pdf">PDF</button>') : ''}</div><div class="help" id="ast"></div>
      ${a ? raw(`<div class="note-box" style="margin:10px 0">${esc(a.classification_note)}</div><p class="help">Site area ${esc(fmt.ha(a.site_area_hectares))} · assessed ${esc(fmt.dt(a.generated_at))}</p>${Object.entries(a.sections).map(([k, v]) => `<div style="margin:8px 0"><div class="row"><b class="grow">${esc(fmt.status(k))}</b>${classTag(v.evidence_class).s}</div>${section(k, v)}</div>`).join('')}<h4>Recommended follow-up</h4><ul class="steps">${a.recommended_follow_up.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`) : raw('<p class="help">Not assessed yet.</p>')}`;
    $('#assess')?.addEventListener('click', async () => { const b = $('#assess'); b.disabled = true; try { const j = await api(`/industrial/sites/${s.id}/assess`, { method: 'POST' }); await waitJob(j.job_id, (_, t) => ($('#ast').textContent = `Gathering terrain, air quality and map data… ${t}s`), 3000); sites = await api('/industrial/sites'); select(sites.find((x) => x.id === s.id)); toast('Assessment complete'); } catch (e) { toast(e.message, 'error'); b.disabled = false; } });
    $('#pdf')?.addEventListener('click', () => download(`/industrial/sites/${s.id}/report.pdf`, `rakshak-site-${s.id.slice(0, 8)}.pdf`));
  }
  function section(k, v) {
    if (v.evidence_class === 'not_available') return `<div class="warn-box">${esc(v.reason)}</div>`;
    if (k === 'forest_loss') return `<table class="t"><tbody>${v.rows.map((r) => `<tr><td>${esc(r.zone)}</td><td>${esc(r.candidate_loss_hectares)} ha</td></tr>`).join('')}</tbody></table><div class="help">${esc(v.note)}</div>`;
    if (k === 'permit_and_clearance_records') return `${v.intersecting_records.length ? `<ul class="steps">${v.intersecting_records.map((p) => `<li>${esc(p.authority)} · ${esc(p.project || p.authorization_id.slice(0, 8))} · ${esc(p.status)}</li>`).join('')}</ul>` : '<span class="help">No intersecting stored record.</span>'}<div class="help">${esc(v.coverage)}. ${esc(v.warning)}</div>`;
    if (k === 'nearby_features_osm') return `<dl class="kv">${['nearest_road', 'nearest_settlement', 'nearest_waterway'].map((n) => `<dt>${esc(n.replace('_', ' '))}</dt><dd>${v[n] ? esc(`${v[n].name || v[n].type} · ${v[n].distance_m} m`) : 'none mapped'}</dd>`).join('')}<dt>protected areas</dt><dd>${v.protected_areas_status === 'ok' ? esc(v.protected_areas_containing_centroid.map((p) => p.name).join(', ') || 'none mapped here') : esc(v.protected_areas_status)}</dd></dl>`;
    const flat = Object.entries(v).filter(([kk, vv]) => !['evidence_class', 'attribution', 'status', 'source'].includes(kk) && typeof vv !== 'object' && vv != null);
    return `<dl class="kv">${flat.map(([kk, vv]) => `<dt>${esc(kk.replaceAll('_', ' '))}</dt><dd>${esc(vv)}</dd>`).join('')}</dl>${v.attribution ? `<div class="help">${esc(v.attribution)}</div>` : ''}`;
  }

  $('#draw')?.addEventListener('click', async () => { const geo = await drawShape(map, 'polygon'); if (!geo) return; drawn = geo; d.clearLayers(); addGeo(d, geo, { color: '#bf731f', weight: 3, fillOpacity: 0.1 }); $('#newSite').hidden = false; });
  $('#cancel')?.addEventListener('click', () => { drawn = null; d.clearLayers(); $('#newSite').hidden = true; });
  $('#nf')?.addEventListener('submit', async (e) => { e.preventDefault(); const f = new FormData(e.target); try { const s = await api('/industrial/sites', { method: 'POST', body: { name: f.get('name'), geometry: drawn, kind: f.get('kind'), source: f.get('source'), permit_ref: f.get('ref') || null } }); toast('Site registered'); drawn = null; d.clearLayers(); $('#newSite').hidden = true; sites = await api('/industrial/sites'); select(sites.find((x) => x.id === s.id)); } catch (err) { toast(err.message, 'error'); } });
  paint(); if (sites[0]) select(sites[0]);
  return { destroy: () => map.remove() };
}

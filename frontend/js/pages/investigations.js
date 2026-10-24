import { api, html, raw, esc, fmt, tag, classTag, empty, toast, dialog, can, kv, download, $, $$ } from '../core.js';
import { createMap, addGeo, fit, STYLE, popup } from '../map.js';

export const STATUS_LABEL = { DETECTED: 'Newly detected', PROCESSING: 'Processing', NEEDS_REVIEW: 'Under review', APPROVED_FOR_INSPECTION: 'Verification required · approved for inspection', ASSIGNED: 'Referred to responder (assigned)', DISPATCHED: 'Notification sent', ACKNOWLEDGED: 'Acknowledged by responder', FIELD_VERIFICATION_PENDING: 'Field inspection in progress', CONFIRMED: 'Confirmed by authorised officer', UNCONFIRMED: 'Not confirmed in the field', DISMISSED: 'Dismissed', CLOSED: 'Closed' };
const AUTH_TEXT = { NO_RECORD_FOUND: 'No matching authorisation found — verification required', POTENTIAL_MATCH: 'Potential match — needs review', MATCH_REQUIRES_REVIEW: 'Record overlaps and looks valid — a person must confirm scope and conditions', AUTHORIZATION_STATUS_UNCLEAR: 'Authorisation status unclear', POSSIBLE_SCOPE_OR_CONDITION_MISMATCH: 'Possible scope or condition mismatch', RECORD_EXPIRED_OR_REVOKED_CANDIDATE: 'Record may be expired or revoked', INSUFFICIENT_EVIDENCE: 'Insufficient evidence' };
const kind = (s) => (['CONFIRMED'].includes(s) ? 'red' : ['CLOSED', 'DISMISSED', 'UNCONFIRMED'].includes(s) ? 'neutral' : 'configure');

export async function render(root, { params }) {
  let incidents = await api('/incidents'), map = null;
  const open = params[0];
  const stations = await api('/stations').catch(() => []);

  function list() {
    root.innerHTML = html`<div class="panel"><div class="row"><div class="grow"><h3>Illegal-cutting investigations</h3><p class="sub">A detection is a lead, not proof. A missing permit record never establishes illegality; people decide.</p></div>
      <select id="flt" aria-label="Filter by status"><option value="">All statuses</option>${raw(Object.entries(STATUS_LABEL).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join(''))}</select></div>
      <div id="tbl"></div></div>`;
    const paint = () => {
      const f = $('#flt').value, rows = incidents.filter((i) => !f || i.status === f);
      $('#tbl').innerHTML = rows.length ? html`<table class="t"><thead><tr><th>Case</th><th>Status</th><th>Origin</th><th>Updated</th></tr></thead><tbody>${rows.map((i) => raw(`<tr data-id="${esc(i.id)}"><td><b>${esc(i.title)}</b><div class="help">${esc(i.id.slice(0, 8))}</div></td><td>${tag(STATUS_LABEL[i.status] || i.status, kind(i.status)).s}</td><td>${esc(i.change_id ? 'Satellite change candidate' : i.wildfire_id ? 'Fire observation' : 'Manually created')}</td><td>${esc(fmt.dt(i.updated_at))}</td></tr>`))}</tbody></table>` : empty('No cases', 'Cases appear when a detection meets the criteria or someone opens one from Change Detection.', '⚠');
      $$('#tbl tr[data-id]').forEach((tr) => (tr.onclick = () => (location.hash = '#/investigations/' + tr.dataset.id)));
    };
    $('#flt').onchange = paint; paint();
  }

  async function detail(id) {
    const c = await api(`/incidents/${id}/case`);
    const inc = c.incident, ch = c.change, ev = inc.evidence || {};
    const a = c.authorization_assessments[0];
    root.innerHTML = html`
      <div class="row"><a class="button subtle sm" href="#/investigations">← All cases</a><h3 style="margin:0">${inc.title}</h3>${tag(STATUS_LABEL[inc.status] || inc.status, kind(inc.status))}<span class="help">${inc.id}</span></div>
      <div class="grid split" style="margin-top:14px">
        <div class="stack">
          <div class="panel"><div class="row"><div class="grow"><h4>Location and imagery evidence</h4></div>${ch && c.observations.length ? raw('<div class="pill-btns" id="imgMode"><button class="on" data-m="geom">Boundary</button><button data-m="cmp">Before / after</button></div>') : ''}</div><div id="map" class="map-box"></div><div id="cmpCtl"></div>
            ${ch ? raw(`<div class="help" style="margin-top:6px">Satellite candidate · ${esc(fmt.ha(ch.area_hectares))} · observed ${esc(fmt.date(ch.baseline_date))} → ${esc(fmt.date(ch.comparison_date))} · score ${esc(ch.score ?? '—')} (uncalibrated)</div>`) : ''}</div>
          <div class="panel"><div class="row"><div class="grow"><h4>Authorisation check</h4><p class="sub">Searches records stored in this system by location, validity dates and status. Coverage of official portals is not established.</p></div>${ch && can('admin', 'reviewer') ? raw('<button class="button primary sm" id="match">Run check</button>') : ''}${can('admin', 'reviewer', 'analyst') ? raw('<button class="button subtle sm" id="addRec">Register official record</button><button class="button subtle sm" id="leads">Web leads…</button>') : ''}</div>
            <div id="auth">${a ? raw(authHtml(a)) : raw(empty('Not checked yet', ch ? 'Run the check to compare this change against stored authorisation records.' : 'No satellite change is linked to this case.', '⚖'))}</div></div>
          <div class="panel"><div class="row"><div class="grow"><h4>Evidence files</h4></div>${can('admin', 'reviewer', 'officer', 'analyst') ? raw('<label class="button subtle sm" style="cursor:pointer">Attach file<input type="file" id="file" hidden accept=".png,.jpg,.jpeg,.pdf,.txt,.csv,.json,.geojson"></label>') : ''}</div>
            ${c.evidence_files.length ? raw(`<table class="t"><tbody>${c.evidence_files.map((f) => `<tr><td><b>${esc(f.filename)}</b><div class="help">${esc(fmt.dt(f.created_at))} · ${esc(f.uploaded_by)} · sha256 ${esc(f.sha256.slice(0, 12))}…</div></td><td><button class="button subtle sm" data-ev="${esc(f.id)}" data-name="${esc(f.filename)}">Download</button></td></tr>`).join('')}</tbody></table>`) : raw('<p class="help">No files attached.</p>')}<div class="help">PNG/JPG/PDF/TXT/CSV/JSON/GeoJSON, 10 MB max. Access is logged.</div></div>
        </div>
        <div class="stack">
          <div class="panel"><h4>Decision workflow</h4><p class="sub">Only people with the right role can move a case. Dispatch is impossible until a reviewer approves and a case is assigned.</p>
            <div class="stack" id="actions">${raw(actions(inc))}</div></div>
          <div class="panel"><h4>Notifications</h4>${c.notifications.length ? raw(`<table class="t"><tbody>${c.notifications.map((n) => `<tr><td>${esc(n.channel)}</td><td>${esc(fmt.status(n.status))}<div class="help">${esc(fmt.dt(n.created_at))}</div></td></tr>`).join('')}</tbody></table><div class="help">“Sent to provider” means accepted for delivery, not read. Only an authenticated acknowledgement counts as acknowledged.</div>`) : raw('<p class="help">None yet.</p>')}</div>
          <div class="panel"><h4>Reports</h4><div class="row"><button class="button subtle sm" id="rep">Generate evidence package (PDF + JSON)</button></div><div id="repOut" class="help"></div></div>
          <div class="panel"><h4>Audit trail</h4>${c.audit_trail.length ? raw(`<ul class="timeline">${c.audit_trail.map((e) => `<li><b>${esc(fmt.status(e.action))}</b>${esc(e.actor_role)} · ${esc(fmt.dt(e.created_at))}${e.details?.notes ? '<br>' + esc(e.details.notes) : ''}</li>`).join('')}</ul>`) : raw('<p class="help">No events.</p>')}</div>
        </div>
      </div>`;
    wire(c);
  }

  const authHtml = (a) => `<div class="${a.status === 'NO_RECORD_FOUND' ? 'warn-box' : 'note-box'}"><b>${esc(AUTH_TEXT[a.status] || a.status)}</b><br>${esc(a.rationale.warning)}<br><span class="help">${esc(a.rationale.records_searched)}</span></div>${a.rationale.overlapping_candidates?.length ? `<table class="t" style="margin-top:8px"><thead><tr><th>Record</th><th>Overlap</th><th>Validity</th><th>State</th></tr></thead><tbody>${a.rationale.overlapping_candidates.map((x) => `<tr><td>${esc(x.authorization_id.slice(0, 8))} · ${esc(x.record_status)}</td><td>${esc(fmt.pct(x.overlap_fraction))}</td><td>${esc(x.temporal)}</td><td>${esc(AUTH_TEXT[x.state] || x.state)}</td></tr>`).join('')}</tbody></table>` : ''}<div class="help">Checked ${esc(fmt.dt(a.created_at))}. This is a workflow state for human review, not a legal determination.</div>`;

  const actions = (inc) => {
    const s = inc.status, btn = (id, label, cls = 'subtle') => `<button class="button ${cls}" id="${id}">${label}</button>`;
    const out = [];
    if (['DETECTED', 'NEEDS_REVIEW'].includes(s)) out.push(`<div class="row">${can('admin', 'reviewer') ? btn('approve', 'Approve for field inspection', 'primary') + btn('dismiss', 'Dismiss (false alarm)') : '<span class="help">Reviewer role required to decide.</span>'}</div>`);
    if (s === 'APPROVED_FOR_INSPECTION') out.push(`<div class="row">${can('admin', 'officer') ? `<select id="stn" aria-label="Station"><option value="">Assign to…</option>${stations.map((x) => `<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('')}<option value="__other">Other (type a name)</option></select>${btn('assign', 'Assign', 'primary')}` : '<span class="help">Officer role required to assign.</span>'}</div>`);
    if (s === 'ASSIGNED') out.push(`<div class="grid g2"><label class="f">Channel<select id="ch"><option value="test">Test (captured, nothing sent)</option><option value="telegram">Telegram</option><option value="email">Email</option><option value="voice">Voice call</option></select></label><label class="f">Recipient<input type="text" id="rcpt" placeholder="chat id / email / phone"></label></div><div class="help">Real delivery needs the server in production mode with notifications enabled; otherwise the message is captured only.</div>${can('admin', 'officer') ? btn('dispatch', 'Notify responder', 'primary') : ''}`);
    if (['DISPATCHED', 'ACKNOWLEDGED', 'FIELD_VERIFICATION_PENDING'].includes(s)) out.push(`<div class="grid g2"><label class="f">Field outcome<select id="fv"><option value="pending">Inspection in progress</option><option value="confirmed">Confirmed on the ground</option><option value="unconfirmed">Not confirmed</option></select></label><label class="f">Notes<input type="text" id="fvn"></label></div>${can('admin', 'officer') ? btn('verify', 'Record field verification', 'primary') : ''}`);
    if (['CONFIRMED', 'UNCONFIRMED', 'FIELD_VERIFICATION_PENDING', 'ACKNOWLEDGED'].includes(s) && can('admin', 'officer')) out.push(btn('close', 'Close case'));
    if (['APPROVED_FOR_INSPECTION', 'ASSIGNED', 'DISPATCHED', 'ACKNOWLEDGED'].includes(s)) out.push(`<a class="button subtle" href="#/rangers/${esc(inc.id)}">Plan route →</a>`);
    return out.join('') || '<p class="help">No actions available in this state.</p>';
  };

  async function act(label, fn, id) { try { await fn(); toast(label); window.dispatchEvent(new Event('rk:changed')); await detail(id); } catch (e) { toast(e.message, 'error'); } }

  async function wire(c) {
    const inc = c.incident, id = inc.id, ch = c.change;
    ({ map } = await createMap($('#map'))); const g = L.layerGroup().addTo(map);
    const ls = [];
    if (ch) ch.geometry.coordinates && ls.push(addGeo(g, ch.geometry, STYLE.loss, popup('Candidate loss', [fmt.ha(ch.area_hectares)])));
    ls.push(addGeo(g, inc.geometry, STYLE.incident, popup(inc.title))); fit(map, ls, { padding: [30, 30], maxZoom: 16 });
    if ($('#imgMode')) $('#imgMode').onclick = (e) => cmpMode(e, c);
    const body = (d) => ({ decision: d, notes: prompt('Add a note for the audit trail (optional):', '') || '' });
    $('#approve')?.addEventListener('click', () => act('Approved for field inspection', () => api(`/incidents/${id}/review`, { method: 'POST', body: body('approve') }), id));
    $('#dismiss')?.addEventListener('click', () => act('Case dismissed', () => api(`/incidents/${id}/review`, { method: 'POST', body: body('dismiss') }), id));
    $('#assign')?.addEventListener('click', () => { let v = $('#stn').value; if (v === '__other') v = prompt('Responder or station name:'); if (!v) return toast('Choose or enter an assignee', 'error'); act('Assigned', () => api(`/incidents/${id}/assign`, { method: 'POST', body: { assignee: v } }), id); });
    $('#dispatch')?.addEventListener('click', () => { const r = $('#rcpt').value.trim(); if (!r) return toast('Enter a recipient', 'error'); act('Notification recorded', () => api(`/incidents/${id}/dispatch`, { method: 'POST', body: { channel: $('#ch').value, recipient: r } }), id); });
    $('#verify')?.addEventListener('click', () => act('Field verification recorded', () => api(`/incidents/${id}/field-verification`, { method: 'POST', body: { outcome: $('#fv').value, notes: $('#fvn').value } }), id));
    $('#close')?.addEventListener('click', () => act('Case closed', () => api(`/incidents/${id}/close`, { method: 'POST' }), id));
    $('#match')?.addEventListener('click', () => act('Authorisation check complete', () => api('/authorizations/match-assessment', { method: 'POST', body: { change_id: ch.id } }), id));
    $('#rep').onclick = async () => { try { const r = await api(`/incidents/${id}/reports`, { method: 'POST' }); $('#repOut').innerHTML = html`Generated. <a href="#" id="dl1">PDF</a> · <a href="#" id="dl2">JSON</a><br>SHA-256 ${r.package_sha256.slice(0, 20)}…`; $('#dl1').onclick = (e) => { e.preventDefault(); download(`/reports/${r.pdf_id}/download`, `rakshak-${id.slice(0, 8)}.pdf`); }; $('#dl2').onclick = (e) => { e.preventDefault(); download(`/reports/${r.id}/download`, `rakshak-${id.slice(0, 8)}.json`); }; } catch (e) { toast(e.message, 'error'); } };
    $('#file')?.addEventListener('change', async (e) => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append('file', f); try { await api(`/incidents/${id}/evidence`, { method: 'POST', form: fd }); toast('Evidence attached'); detail(id); } catch (err) { toast(err.message, 'error'); } });
    $$('[data-ev]').forEach((b) => (b.onclick = () => download(`/evidence/${b.dataset.ev}/download`, b.dataset.name)));
    $('#addRec')?.addEventListener('click', () => dialog('Register an official authorisation record', `<div class="help">Enter details exactly as they appear on the official source. Geometry is GeoJSON (WGS84).</div>
      <label class="f">Issuing authority<input type="text" name="authority" required></label><label class="f">Project / permit reference<input type="text" name="ref"></label><label class="f">Project name<input type="text" name="name"></label>
      <label class="f">Record status<select name="status"><option value="issued">Issued / approved</option><option value="application">Application</option><option value="pending">Pending</option><option value="recommended">Recommended</option><option value="expired">Expired</option><option value="rejected">Rejected</option><option value="revoked">Revoked</option></select></label>
      <div class="grid g2"><label class="f">Valid from<input type="date" name="from"></label><label class="f">Valid to<input type="date" name="to"></label></div>
      <label class="f">Official source URL<input type="text" name="url" placeholder="https://parivesh.nic.in/…"></label><label class="f">Boundary (GeoJSON Polygon)<textarea name="geom" rows="4" required placeholder='{"type":"Polygon","coordinates":[...]}'></textarea></label>`, async (f) => {
      const d = {}; if (f.get('from')) d.valid_from = f.get('from') + 'T00:00:00+00:00'; if (f.get('to')) d.valid_to = f.get('to') + 'T00:00:00+00:00';
      await api('/authorizations/ingest', { method: 'POST', body: { authority: f.get('authority'), project_identifier: f.get('ref') || null, project_name: f.get('name') || null, status: f.get('status'), geometry: JSON.parse(f.get('geom')), dates: d, source_url: f.get('url') || null, evidence: { entered_by_role: 'reviewer' } } }); toast('Record stored. Re-run the check.');
    }, 'Store record'));
    $('#leads')?.addEventListener('click', () => dialog('Search the web for official-record leads', `<div class="help">Uses a web-search provider restricted to government domains. Results are leads to open and verify on the official portal; they are not records and are not used in matching.</div><label class="f">Search terms<input type="text" name="q" required placeholder="project or place name, e.g. forest clearance Wayanad"></label><div id="leadOut"></div>`, async (f) => {
      const r = await api('/authorizations/web-leads', { method: 'POST', body: { query: f.get('q') } });
      $('#dialogError').innerHTML = ''; $('#leadOut').innerHTML = r.leads.length ? html`<ul class="steps">${r.leads.map((l) => raw(`<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.title)}</a> ${l.official_domain ? tag('gov domain', 'ready').s : tag('other', 'neutral').s}<div class="help">${esc(l.snippet || '')}</div></li>`))}</ul>` : '<p class="help">No results. This proves nothing about permits.</p>';
      throw new Error(r.notice);
    }, 'Search'));
  }

  async function cmpMode(e, c) {
    const m = e.target.dataset.m; if (!m) return; $$('#imgMode button').forEach((b) => b.classList.toggle('on', b === e.target));
    $('#cmpCtl').innerHTML = '';
    if (map._cmp) { map._cmp.forEach((l) => map.removeLayer(l)); map._cmp = null; if (map._cmpOff) map.off('move zoom viewreset', map._cmpOff); }
    if (m !== 'cmp') return;
    const b = c.observations.find((o) => o.role === 'before_s2'), a = c.observations.find((o) => o.role === 'after_s2'); if (!b || !a) return toast('Imagery not stored for this case', 'error');
    const [mb, ma, ib, ia] = await Promise.all([api(`/observations/${b.observation_id}/preview-meta`), api(`/observations/${a.observation_id}/preview-meta`), api(`/observations/${b.observation_id}/preview.png`, { blob: true }), api(`/observations/${a.observation_id}/preview.png`, { blob: true })]);
    const L1 = L.imageOverlay(URL.createObjectURL(ib), mb.bounds).addTo(map), L2 = L.imageOverlay(URL.createObjectURL(ia), ma.bounds).addTo(map); map._cmp = [L1, L2];
    $('#cmpCtl').innerHTML = html`<div class="compare-ctl"><span>${fmt.date(mb.acquired_from)}</span><input type="range" id="sp" min="0" max="100" value="50" aria-label="Before and after split"><span>${fmt.date(ma.acquired_from)}</span></div>`;
    const clip = () => { const r = $('#sp').value / 100, size = map.getSize(), nw = map.containerPointToLayerPoint([0, 0]), se = map.containerPointToLayerPoint(size), x = nw.x + size.x * r; L1.getElement().style.clip = `rect(${nw.y}px,${x}px,${se.y}px,${nw.x}px)`; L2.getElement().style.clip = `rect(${nw.y}px,${se.x}px,${se.y}px,${x}px)`; };
    $('#sp').oninput = clip; map._cmpOff = clip; map.on('move zoom viewreset', clip); clip(); map.fitBounds(ma.bounds);
  }

  if (open) await detail(open); else list();
  return { destroy: () => map?.remove() };
}

import { api, html, raw, esc, fmt, tag, empty, toast, can, state, $, $$ } from '../core.js';

export async function render(root) {
  const [caps, models, providers] = await Promise.all([api('/system/capabilities'), api('/system/models'), api('/system/providers')]);
  const audits = await api('/audit/events?limit=100').catch(() => null);
  const token = !!localStorage.getItem('rk.token');
  root.innerHTML = html`
    <div class="grid g2">
      <div class="panel"><h3>Access</h3>
        <dl class="kv"><dt>Identity mode</dt><dd>${token ? 'Bearer token (stored in this browser)' : 'Development role header'}</dd><dt>Acting role</dt><dd>${token ? 'from token' : state.role}</dd></dl>
        <div class="note-box" style="margin-top:10px">Roles: <b>Administrator</b> (everything), <b>Reviewer</b> (approve, dismiss, verify hazards, record authorisations), <b>Forest officer / ranger</b> (assign, dispatch, navigate, field verification), <b>Environmental analyst</b> (analysis, assessments, projections), <b>Read-only</b>. The server enforces every permission; this page only hides buttons.</div>
        <div class="help" style="margin-top:8px">Production deployments must use <code>AUTH_MODE=jwt</code>; the role header is a development convenience and is refused when <code>ENVIRONMENT=production</code>.</div></div>
      <div class="panel"><h3>Capabilities</h3><dl class="kv">${Object.entries(caps.features).map(([k, v]) => raw(`<dt>${esc(fmt.status(k))}</dt><dd>${v ? tag('yes', 'ready').s : tag('no', 'neutral').s}</dd>`))}</dl><div class="help" style="margin-top:8px">${caps.safe_mode ? 'Safe mode: notifications are captured, not sent.' : 'Production mode: notifications can reach real recipients.'}</div></div>
      <div class="panel"><h3>Models</h3><table class="t"><tbody>${models.map((m) => raw(`<tr><td><b>${esc(m.name)}</b><div class="help">${esc(m.version)}${m.horizon_days ? ' · ' + m.horizon_days + ' d' : ''}</div></td><td>${m.usable ? tag('usable', 'ready').s : tag('not usable', 'neutral').s}</td><td class="help">${esc(m.reason || m.kind || '')}</td></tr>`))}</tbody></table></div>
      <div class="panel"><h3>Providers</h3><table class="t"><tbody>${providers.map((p) => raw(`<tr><td><b>${esc(p.name)}</b></td><td>${p.usable ? tag('usable', 'ready').s : p.configured ? tag('configured', 'configure').s : tag('not configured', 'neutral').s}</td><td class="help">${esc(p.reason)}</td></tr>`))}</tbody></table></div>
    </div>
    <div class="panel" style="margin-top:16px"><h3>Audit ledger</h3><p class="sub">Append-only record of reviews, authorisation decisions, dispatches, evidence access and exports.</p>
      ${audits === null ? raw(empty('Restricted', 'Your role cannot read the audit log.', '◫')) : audits.length ? raw(`<table class="t"><thead><tr><th>When</th><th>Action</th><th>Resource</th><th>Role</th></tr></thead><tbody>${audits.map((a) => `<tr><td>${esc(fmt.dt(a.created_at))}</td><td>${esc(fmt.status(a.action))}</td><td>${esc(a.resource_type)}${a.resource_id ? ' · ' + esc(String(a.resource_id).slice(0, 8)) : ''}</td><td>${esc(a.actor_role)}</td></tr>`).join('')}</tbody></table>`) : empty('No events yet', '', '◫')}</div>`;
}

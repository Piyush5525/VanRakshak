// Shared helpers: API client, safe HTML templating, toasts, dialogs, jobs, formatting.
export const state = { role: localStorage.getItem('rk.role') || 'reviewer' };

export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(s);
// Tagged template: every interpolation is escaped unless wrapped with raw(); arrays are joined.
export const html = (strings, ...vals) => new Raw(strings.reduce((out, s, i) => {
  let v = vals[i - 1];
  if (Array.isArray(v)) v = v.map((x) => (x instanceof Raw ? x.s : esc(x))).join('');
  else if (v instanceof Raw) v = v.s;
  else v = esc(v);
  return out + (i ? v : '') + s;
}, ''));

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export async function api(path, { method = 'GET', body, form, blob } = {}) {
  const token = localStorage.getItem('rk.token');
  const headers = token ? { Authorization: 'Bearer ' + token } : { 'X-Role': state.role };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch('/api/v1' + path, { method, headers, body: form || (body !== undefined ? JSON.stringify(body) : undefined) });
  if (blob) { if (!res.ok) throw new Error(await errText(res)); return res.blob(); }
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const e = new Error(detail(data) || res.statusText); e.status = res.status; throw e;
  }
  return data;
}
const errText = async (r) => detail(await r.json().catch(() => null)) || r.statusText;
function detail(d) {
  if (!d) return '';
  if (typeof d === 'string') return d;
  if (Array.isArray(d.detail)) return d.detail.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ');
  return d.detail || d.message || '';
}

export function toast(message, kind = 'info') {
  const el = $('#toast'); el.textContent = message; el.className = 'toast show ' + kind;
  clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), 4500);
}

// Poll a background job until it finishes. onTick receives the job record.
export async function waitJob(jobId, onTick, every = 4000, maxMs = 20 * 60 * 1000) {
  const start = Date.now();
  for (;;) {
    const job = await api('/jobs/' + jobId);
    onTick?.(job, Math.round((Date.now() - start) / 1000));
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw new Error(job.error?.message || 'Job failed');
    if (Date.now() - start > maxMs) throw new Error('Timed out waiting for the job');
    await new Promise((r) => setTimeout(r, every));
  }
}

export function dialog(title, bodyHtml, onSubmit, submitLabel = 'Save') {
  const d = $('#dialog'), f = $('#dialogForm');
  f.innerHTML = html`<div class="dialog-header"><h3>${title}</h3><button class="icon-button" value="cancel" aria-label="Close">×</button></div>${bodyHtml instanceof Raw ? bodyHtml : raw(bodyHtml)}<div id="dialogError" class="form-error"></div><div class="dialog-actions"><button class="button subtle" value="cancel">Cancel</button>${onSubmit ? raw(`<button class="button primary" value="default">${esc(submitLabel)}</button>`) : ''}</div>`;
  f.onsubmit = async (e) => {
    if (e.submitter?.value === 'cancel' || !onSubmit) return;
    e.preventDefault();
    const btn = e.submitter; btn.disabled = true;
    try { await onSubmit(new FormData(f)); d.close(); } catch (err) { $('#dialogError').textContent = err.message; } finally { btn.disabled = false; }
  };
  d.showModal();
}

export const fmt = {
  ha: (v) => (v == null ? '—' : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })} ha`),
  date: (v) => (v ? new Date(v).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—'),
  dt: (v) => (v ? new Date(v).toLocaleString() : '—'),
  pct: (v) => (v == null ? '—' : `${Math.round(v * 100)}%`),
  km: (v) => (v == null ? '—' : `${v} km`),
  min: (v) => (v == null ? '—' : v >= 90 ? `${Math.floor(v / 60)} h ${Math.round(v % 60)} min` : `${Math.round(v)} min`),
  status: (s) => String(s || '').replaceAll('_', ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase()),
};

export const tag = (text, kind = 'neutral') => raw(`<span class="tag ${esc(kind)}">${esc(text)}</span>`);
export const classTag = (c) => ({ measured: tag('measured', 'ready'), computed: tag('computed', 'ready'), screening: tag('screening', 'configure'), not_available: tag('not available', 'neutral'), observation: tag('observation', 'ready'), candidate_inference: tag('model candidate', 'configure'), hypothetical_assumption: tag('hypothetical', 'purple'), forecast_estimate: tag('forecast', 'blue'), reference_map: tag('reference map', 'neutral') }[c] || tag(c || 'unlabelled'));

export const empty = (title, text, icon = '◌') => html`<div class="empty-state"><div class="empty-icon">${icon}</div><strong>${title}</strong><span>${text}</span></div>`;
export const loading = (text = 'Loading…') => html`<div class="skeleton-block" aria-busy="true"><span class="spinner"></span> ${text}</div>`;
export const errorBox = (e) => html`<div class="error-box" role="alert"><strong>${e.status === 503 ? 'Service unavailable' : e.status === 403 ? 'Not permitted for this role' : 'Something went wrong'}</strong><span>${e.message}</span></div>`;

export const can = (...roles) => roles.includes(state.role) || !!localStorage.getItem('rk.token');

export function download(path, filename) {
  return api(path, { blob: true }).then((b) => { const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000); }).catch((e) => toast(e.message, 'error'));
}
window.rkDownload = download;

export const kv = (obj) => html`<dl class="kv">${Object.entries(obj).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => raw(`<dt>${esc(fmt.status(k))}</dt><dd>${typeof v === 'object' ? `<code>${esc(JSON.stringify(v))}</code>` : esc(v)}</dd>`))}</dl>`;

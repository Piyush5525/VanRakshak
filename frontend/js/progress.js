// Live progress cards ("something is happening") and a short record of milestones ("what has happened").
// Only meaningful actions are listed: a permit check result, a status decision, a route, a message sent, a call placed.
// Clicks, edits and technical steps are never listed.
import { esc } from './core.js';

const host = () => {
  let h = document.getElementById('pops');
  if (!h) { h = document.createElement('div'); h.id = 'pops'; h.setAttribute('aria-live', 'polite'); document.body.appendChild(h); }
  return h;
};

// pop({ icon, title, steps }) shows a card that steps through `steps`; the current step spins, finished steps get a tick.
export function pop({ icon, title, steps }) {
  const el = document.createElement('div');
  el.className = 'pop';
  el.innerHTML = `<div class="pop-h"><span class="pop-ic">${icon}</span><b>${esc(title)}</b><button class="pop-x" aria-label="Close">×</button></div>
    <ol class="pop-steps">${steps.map((s) => `<li><i></i><span class="st"><b>${esc(s)}</b><small></small></span></li>`).join('')}</ol><div class="pop-msg"></div>`;
  host().appendChild(el);
  const items = [...el.querySelectorAll('li')];
  let cur = -1, over = false;
  const close = () => { el.classList.add('out'); setTimeout(() => el.remove(), 300); };
  const paint = (i) => { items.forEach((x, k) => { x.className = k < i ? 'done' : k === i ? 'now' : ''; }); cur = i; };
  const msg = (t) => { el.querySelector('.pop-msg').textContent = t || ''; };
  const ctl = {
    at(i) { if (!over && i > cur) paint(Math.min(i, items.length - 1)); return ctl; },
    done(t) { if (over) return; over = true; items.forEach((x) => (x.className = 'done')); el.classList.add('ok'); msg(t); setTimeout(close, 6000); },
    fail(t) { if (over) return; over = true; if (cur >= 0) items[cur].className = 'bad'; el.classList.add('bad'); msg(t); setTimeout(close, 12000); },
    // a short line under a step: what actually happened there ("Sent to Telegram", "Ringing", "Call finished, 14 s")
    note(i, t, tone) { const el = items[i]?.querySelector('small'); if (el) { el.textContent = t || ''; el.className = tone || ''; } return ctl; },
    get over() { return over; },
    close,
  };
  el.querySelector('.pop-x').onclick = close;
  paint(0);
  return ctl;
}

// ---- what counts as a milestone, and how it is worded
const STATUS_WORDS = { DETECTED: 'Detected', NEEDS_VERIFICATION: 'Needs checking', CONFIRMED: 'Confirmed as a fire', DISPATCH_REQUESTED: 'Response approved', DISPATCHED: 'Responders dispatched', MONITORING: 'Being monitored', RESOLVED: 'Resolved', REJECTED: 'Rejected', CANCELLED: 'Cancelled', INCONCLUSIVE: 'Inconclusive' };
const MILESTONES = {
  incident_created: () => ['🔥', 'Incident opened', 'ok'],
  status_changed: (e) => ['✔', STATUS_WORDS[e.details?.to] || e.message, 'ok'],
  dispatch_authorised: () => ['✔', 'Response approved', 'ok'],
  forecast_generated: () => ['🌬', 'Spread forecast ready (3, 6 and 12 hours)', 'ok'],
  route_calculated: (e) => ['🛣', e.message.replace(/^Route calculated:\s*/, 'Route to the nearest station: ').replace(/\s*\(\w+\)\s*$/, ''), 'ok'],
  telegram_sent: () => ['✈', 'Sent to Telegram', 'ok'],
  telegram_failed: (e) => ['✈', `Telegram did not accept it${e.error ? ': ' + e.error : ''}`, 'failed'],
  ivr_initiated: () => ['📞', 'Call placed', 'ok'],
  ivr_ringing: () => ['📞', 'Phone is ringing', 'ok'],
  ivr_answered: () => ['📞', 'Call answered', 'ok'],
  ivr_completed: () => ['📞', 'Call completed', 'ok'],
  ivr_failed: (e) => ['📞', `Call failed${e.error ? ': ' + e.error : ''}`, 'failed'],
  ivr_busy: () => ['📞', 'Line busy', 'failed'],
  ivr_no_answer: () => ['📞', 'Call not answered', 'failed'],
  email_sent: () => ['✉', 'E-mail sent', 'ok'],
  email_failed: () => ['✉', 'E-mail failed', 'failed'],
  dashboard_notified: () => ['🖥', 'Flagged on the dashboard', 'ok'],
  acknowledged: () => ['🤝', 'A responder acknowledged', 'ok'],
  record_verified: (e) => ['📋', e.message, 'ok'],
  record_reviewed: (e) => ['✔', e.message, 'ok'],
  authority_assigned: (e) => ['🏛', e.message, 'ok'],
};
// fire call events are named ivr_<status>; map the provider words onto the ones above
const ALIAS = { ivr_queued: 'ivr_initiated', ivr_requested: 'ivr_initiated' };
export function milestone(e) {
  const f = MILESTONES[ALIAS[e.event_type] || e.event_type];
  if (!f) return null;
  const [icon, text, tone] = f(e);
  return { icon, text, tone, time: String(e.ts).slice(11, 16) };
}
export function historyHtml(events, empty = 'Nothing has happened yet.') {
  const seen = new Set(), rows = [];
  for (const e of events.slice().reverse()) {
    const m = milestone(e); if (!m) continue;
    const k = m.text + m.time; if (seen.has(k)) continue; seen.add(k);
    rows.push(`<div class="hist ${m.tone}"><span class="hi">${m.icon}</span><span>${esc(m.text)}</span><time>${esc(m.time)}</time></div>`);
  }
  return rows.slice(0, 12).join('') || `<div class="sub">${esc(empty)}</div>`;
}

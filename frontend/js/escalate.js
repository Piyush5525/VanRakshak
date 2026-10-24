// Shared by every "Escalate" button: follow a phone call through the provider's own status, and word what happened.
import { api } from './core.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const SAY = { RINGING: 'Phone is ringing', ANSWERED: 'Call answered', COMPLETED: 'Call finished', BUSY: 'The line was busy', NO_ANSWER: 'Nobody answered', FAILED: 'The call did not go through', CANCELLED: 'The call was cancelled' };

// Polls the provider until the call ends. Updates line `i` of the progress card as the call moves along.
export async function followCall(card, i, callId, maxSeconds = 240) {
  card.note(i, 'Call placed, waiting for the phone network…');
  let seen = null;
  for (let n = 0; n < Math.ceil(maxSeconds / 3); n++) {
    await sleep(3000);
    const d = await api('/calls/status?call_id=' + encodeURIComponent(callId)).catch(() => null);
    if (!d?.status) continue;
    if (d.status !== seen) { seen = d.status; card.note(i, SAY[d.status] || d.status.toLowerCase()); }
    if (d.status === 'COMPLETED') { const t = d.duration_s != null ? `Call finished (${d.duration_s} s)` : 'Call finished'; card.note(i, t, 'ok'); return { ok: true, text: t }; }
    if (['BUSY', 'NO_ANSWER', 'FAILED', 'CANCELLED'].includes(d.status)) { card.note(i, SAY[d.status], 'bad'); return { ok: false, text: SAY[d.status] }; }
  }
  card.note(i, 'The phone provider has not reported a final result yet', 'bad');
  return { ok: false, text: 'No final status from the phone provider yet' };
}

export const label = (st) => ({ SENT: 'sent', INITIATED: 'placed', NOT_CONFIGURED: 'not set up', FAILED: 'failed', REFUSED: 'refused', SIMULATED: 'previewed', DUPLICATE_SUPPRESSED: 'already sent earlier' }[st] || String(st || '').toLowerCase());

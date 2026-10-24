import { $, $$, api, state, html, esc, toast, errorBox, loading } from './core.js';

export const PAGES = [
  { id: 'overview', title: 'Overview', group: 'Operations', icon: '◈' },
  { id: 'monitoring', title: 'Forest Monitoring', group: 'Operations', icon: '⌖' },
  { id: 'change', title: 'Change Detection', group: 'Operations', icon: '◐' },
  { id: 'alerts', title: 'Monthly Alerts', group: 'Operations', icon: '◔' },
  { id: 'forecasts', title: 'Forest Forecasts', group: 'Operations', icon: '⟿' },
  { id: 'investigations', title: 'Investigations', group: 'Operations', icon: '⚠', badge: true },
  { id: 'wildfire', title: 'Wildfire Detection', group: 'Operations', icon: '♨' },
  { id: 'rangers', title: 'Ranger Ops & Routing', group: 'Operations', icon: '➤' },
  { id: 'environment', title: 'Environmental Impact', group: 'Analysis', icon: '❦' },
  { id: 'industrial', title: 'Industrial Development', group: 'Analysis', icon: '▥' },
  { id: 'reports', title: 'Reports', group: 'Analysis', icon: '▤' },
  { id: 'ar-explorer', title: 'AR Forest Explorer', group: 'AR', icon: '⬡' },
  { id: 'ar-navigation', title: 'AR Ranger Navigation', group: 'AR', icon: '⌁' },
  { id: 'datasources', title: 'Data Sources', group: 'System', icon: '☷' },
  { id: 'settings', title: 'Settings & Admin', group: 'System', icon: '⚙' },
];

const NAV_PAGES = PAGES.filter((page) => page.group === 'AR');

let current = null;

function buildNav() {
  $('#nav').innerHTML = `
    <div class="nav-section nav-section-primary">
      <a class="nav-item nav-item-external" href="/live"><span class="nav-icon">◉</span><span class="nav-label">Live map</span><span class="nav-arrow">↗</span></a>
      <a class="nav-item nav-item-external" href="/replanting/"><span class="nav-icon">✦</span><span class="nav-label">Replanting intelligence</span><span class="nav-arrow">↗</span></a>
    </div>
    <div class="nav-section nav-section-ar">
      <div class="nav-group">AR tools</div>
      ${NAV_PAGES.map((p) => `<a class="nav-item" href="#/${p.id}" data-page="${p.id}"><span class="nav-icon">${p.icon}</span><span class="nav-label">${esc(p.title.replace(/^AR\s+/, ''))}</span></a>`).join('')}
    </div>`;
}

async function route() {
  const [, id = 'overview', ...rest] = location.hash.split('/');
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  $$('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.page === page.id));
  $('#pageTitle').textContent = page.title; $('#crumb').textContent = page.title; document.title = `${page.title} · RAKSHAK`;
  $('#sidebar').classList.remove('open');
  const view = $('#view');
  current?.destroy?.();
  current = null;
  view.innerHTML = loading('Loading ' + page.title.toLowerCase() + '…');
  try {
    const mod = await import(`./pages/${page.id}.js`);
    view.innerHTML = '';
    current = (await mod.render(view, { params: rest })) || null;
    view.focus({ preventScroll: true });
  } catch (e) {
    console.error(e);
    view.innerHTML = errorBox(e);
  }
}

async function health() {
  try {
    const [h, caps] = await Promise.all([fetch('/health').then((r) => r.json()), api('/system/capabilities')]);
    $('#healthText').textContent = 'System operational';
    $('#modeLabel').textContent = h.environment === 'production' ? 'Production mode' : h.environment === 'development' ? 'Development mode · notifications captured' : h.environment;
    $('#healthChip').classList.remove('bad');
    const issues = [];
    if (!caps.features.real_notifications) issues.push('Real notifications are disabled (messages are captured, not sent).');
    if (issues.length && h.environment !== 'production') { /* informational only: shown on Data Sources */ }
  } catch {
    $('#healthText').textContent = 'API unreachable'; $('#healthChip').classList.add('bad');
  }
}

export async function refreshBadges() {
  try {
    const s = await api('/dashboard/summary');
    const b = $('#badge-investigations'); b.textContent = s.pending_incidents; b.hidden = !s.pending_incidents;
  } catch { /* badge is cosmetic */ }
}

function init() {
  buildNav();
  const sel = $('#roleSelect'); sel.value = state.role;
  sel.addEventListener('change', () => { state.role = sel.value; localStorage.setItem('rk.role', state.role); toast(`Acting as ${sel.options[sel.selectedIndex].text} (development header)`); route(); });
  $('#tokenBtn').addEventListener('click', () => {
    const cur = localStorage.getItem('rk.token');
    const t = prompt(cur ? 'A bearer token is set. Paste a new one, or leave empty to remove it:' : 'Paste a bearer token (needed when the server runs with AUTH_MODE=jwt):', '');
    if (t === null) return;
    if (t.trim()) localStorage.setItem('rk.token', t.trim()); else localStorage.removeItem('rk.token');
    toast(t.trim() ? 'Token saved in this browser' : 'Token removed'); health(); route();
  });
  $('#menuBtn').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  window.addEventListener('hashchange', route);
  window.addEventListener('rk:changed', refreshBadges);
  health(); refreshBadges(); route();
}
init();

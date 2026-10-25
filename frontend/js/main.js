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
  const item = (href, icon, label, ext, page) => `<a class="nav-item${ext ? ' nav-item-external' : ''}" href="${href}"${page ? ` data-page="${page}"` : ''}><span class="nav-icon" aria-hidden="true">${icon}</span><span class="nav-label">${esc(label)}</span>${ext ? '<span class="nav-arrow" aria-hidden="true">↗</span>' : ''}</a>`;
  const ar = Object.fromEntries(NAV_PAGES.map((p) => [p.id, p]));
  $('#nav').innerHTML = `
    <div class="nav-section"><div class="nav-group">Home</div>${item('#/overview', '◈', 'Overview', false, 'overview')}</div>
    <div class="nav-section"><div class="nav-group">Explore</div>
      ${item('/live', '◉', 'Live map', true)}
      ${item('#/ar-explorer', '⬡', 'Forest Explorer', false, 'ar-explorer')}
      ${item('/replanting/', '✦', 'Replanting', true)}
      ${ar['ar-navigation'] ? item('#/ar-navigation', '⌁', 'Ranger Navigation', false, 'ar-navigation') : ''}
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
    const h = await fetch('/health').then((r) => r.json());
    const m = $('#modeLabel');
    if (m) m.textContent = h.environment === 'production' ? 'Production mode' : h.environment === 'development' ? 'Development mode · notifications captured' : h.environment;
  } catch { /* the sidebar note is cosmetic */ }
}

export async function refreshBadges() {
  try {
    const s = await api('/dashboard/summary');
    const b = $('#badge-investigations'); b.textContent = s.pending_incidents; b.hidden = !s.pending_incidents;
  } catch { /* badge is cosmetic */ }
}

function init() {
  buildNav();
  $('#menuBtn').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  window.addEventListener('hashchange', route);
  window.addEventListener('rk:changed', refreshBadges);
  health(); refreshBadges(); route();
}
init();

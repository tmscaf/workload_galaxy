// The suite bar: a static brand, the module selector, and the selected module in a frame below.
//
// The module in view is kept in the address (#esxi) so a link or a reload comes back to it, and
// remembered for the next visit. Modules are shown through their embed mode (?embed=1).
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = { modules: [], selected: null, shown: null, every: 20, loaded: false };
const button = $('modButton'), menu = $('modMenu'), frame = $('frame'), notice = $('notice');

const levelOf = (m) => (m.ok === false ? 'down' : m.ok == null ? 'off' : m.level || 'ok');

function statusText(m) {
  if (m.ok === false) return `<span class="down">not reachable</span>`;
  if (m.ok == null) return 'checking…';
  if (m.problems == null) return 'starting…';
  if (!m.problems) return '<span class="ok">nothing needs attention</span>';
  const cls = m.critical ? 'critical' : 'warning';
  return `<span class="${cls}">${m.problems} to look at</span>${m.critical ? `<br><span class="critical">${m.critical} critical</span>` : ''}`;
}

function embedUrl(m) {
  return m.url + (m.url.includes('?') ? '&' : '?') + 'embed=1';
}

function pick(id, { remember = true } = {}) {
  if (!state.modules.some((m) => m.id === id)) return;
  state.selected = id;
  if (remember) {
    try { localStorage.setItem('wg.module', id); } catch { /* storage unavailable */ }
    if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
  }
  render();
}

function initialChoice() {
  const ids = state.modules.map((m) => m.id);
  const fromHash = location.hash.slice(1);
  if (ids.includes(fromHash)) return fromHash;
  let last = null;
  try { last = localStorage.getItem('wg.module'); } catch { /* storage unavailable */ }
  if (ids.includes(last)) return last;
  return (state.modules.find((m) => m.ok) || state.modules[0])?.id || null;
}

function render() {
  const cur = state.modules.find((m) => m.id === state.selected) || null;

  // The selector button: the module in view, its state, and how much it has waiting.
  $('modName').textContent = cur ? cur.title : state.loaded ? 'No modules' : 'Modules';
  $('modDot').className = `dot ${cur ? levelOf(cur) : 'off'}`;
  const badge = $('modBadge');
  badge.hidden = !(cur && cur.problems);
  badge.textContent = cur?.problems || '';
  badge.className = `badge ${cur?.critical ? 'critical' : ''}`;
  badge.title = cur?.problems ? `${cur.problems} thing${cur.problems === 1 ? '' : 's'} to look at in ${cur.title}` : '';
  button.disabled = !state.modules.length;

  menu.innerHTML = state.modules.map((m) => `<li role="option" tabindex="-1" data-id="${esc(m.id)}" aria-selected="${m.id === state.selected}">
      <span class="dot ${levelOf(m)}"></span>
      <span class="t">${esc(m.title)}${m.name ? `<small>${esc(m.name)}</small>` : ''}</span>
      <span class="r">${statusText(m)}</span>
      <span class="s">${esc(m.ok === false ? (m.error || 'not reachable') : m.summary || '')}</span></li>`).join('')
    || '<li class="empty">No modules are set up.</li>';

  // What the rest of the bar says about the module in view.
  const where = $('modWhere');
  where.hidden = !(cur && cur.name);
  where.textContent = cur?.name || '';
  $('modState').innerHTML = !cur ? '' : cur.ok === false ? '<span class="warn">not reachable</span>'
    : [cur.version ? `v${esc(cur.version)}` : '', cur.mode && cur.mode !== 'live' ? `${esc(cur.mode)} data` : '',
      cur.stale ? '<span class="warn">its last refresh failed</span>' : ''].filter(Boolean).join(' · ');
  const open = $('modOpen');
  open.hidden = !cur;
  if (cur) open.href = cur.url;
  document.title = cur ? `${cur.title} · Workload Galaxy` : 'Workload Galaxy';

  // The stage: the module's own page, or why there is nothing to show.
  if (!state.loaded) return;
  if (!state.modules.length) {
    showNotice('No modules are set up', 'Tell the shell where each module runs, as id=URL pairs in the order they should be listed:',
      'GALAXY_MODULES=kubernetes=http://host:8090,network=http://host:8091,esxi=http://host:8092');
  } else if (cur && cur.ok === false) {
    showNotice(`${cur.title} is not reachable`, `The shell could not read ${cur.url} (${cur.error || 'no answer'}). It checks again every ${state.every} seconds and will show the module as soon as it answers.`,
      null, true);
  } else if (cur) {
    notice.hidden = true;
    frame.hidden = false;
    frame.title = cur.title;
    if (state.shown !== cur.id) { state.shown = cur.id; frame.src = embedUrl(cur); }
  }
}

function showNotice(title, text, code = null, retry = false) {
  frame.hidden = true;
  state.shown = null;
  frame.removeAttribute('src');
  notice.hidden = false;
  notice.innerHTML = `<h1>${esc(title)}</h1><p>${esc(text)}${code ? `<code>${esc(code)}</code>` : ''}</p>
    ${retry ? '<button class="btn" id="retry">Check again now</button>' : ''}`;
  $('retry')?.addEventListener('click', () => load());
}

async function load() {
  try {
    const res = await fetch('api/v1/modules', { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    state.modules = data.modules || [];
    state.every = data.checkEverySeconds || 20;
    const first = !state.loaded;
    state.loaded = true;
    if (first || !state.modules.some((m) => m.id === state.selected)) {
      state.selected = initialChoice();
      if (state.selected) pick(state.selected, { remember: false });
    }
    render();
  } catch (err) {
    if (!state.loaded) { state.loaded = true; showNotice('The shell is not answering', `Could not load the list of modules: ${err.message}.`, null, true); }
  }
}

// ------------------------------------------------------------------ the dropdown
function openMenu(open) {
  menu.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) (menu.querySelector('[aria-selected="true"]') || menu.querySelector('li[data-id]'))?.focus();
}
button.addEventListener('click', (ev) => { ev.stopPropagation(); openMenu(menu.hidden); });
button.addEventListener('keydown', (ev) => {
  if (ev.key === 'ArrowDown') { ev.preventDefault(); openMenu(true); }
});
menu.addEventListener('click', (ev) => {
  const li = ev.target.closest('li[data-id]');
  if (!li) return;
  openMenu(false);
  pick(li.dataset.id);
  button.focus();
});
menu.addEventListener('keydown', (ev) => {
  const items = [...menu.querySelectorAll('li[data-id]')];
  const i = items.indexOf(document.activeElement);
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    items[(i + (ev.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus();
  } else if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    document.activeElement?.click();
  } else if (ev.key === 'Escape' || ev.key === 'Tab') {
    openMenu(false);
    button.focus();
  }
});
document.addEventListener('click', (ev) => { if (!menu.hidden && !menu.contains(ev.target)) openMenu(false); });
// A click inside the module's frame never reaches this page: close the menu when the frame takes focus.
window.addEventListener('blur', () => { if (!menu.hidden) openMenu(false); });
window.addEventListener('hashchange', () => { const id = location.hash.slice(1); if (id && id !== state.selected) pick(id); });

load();
(function poll() { setTimeout(async () => { await load(); poll(); }, Math.max(5, state.every) * 1000); })();

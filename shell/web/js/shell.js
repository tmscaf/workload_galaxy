// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// The suite bar: a static brand, the module selector, and the selected module in a frame below.
//
// The module in view is kept in the address (#esxi) so a link or a reload comes back to it, and
// remembered for the next visit. Modules are shown through their embed mode (?embed=1).
// #settings (or #settings/esxi) opens the settings area in place of the module.
// A module that is turned off (in Settings) stays out of the selector; Settings still lists it.
import { SettingsView } from './settings.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = { modules: [], selected: null, shown: null, every: 20, loaded: false, settings: false, persistent: true };
const turnedOn = () => state.modules.filter((m) => m.enabled !== false);
const button = $('modButton'), menu = $('modMenu'), frame = $('frame'), notice = $('notice'), settingsEl = $('settings');
const settingsView = new SettingsView(settingsEl, {
  modules: () => state.modules,
  persistent: () => state.persistent,
  onModule(id) { history.replaceState(null, '', `#settings/${id}`); },
  onSwitched() { load(); setTimeout(load, 1500); },   // the selector follows at once; a module just turned on reports in shortly
  onChanged() { setTimeout(load, 1500); },          // the module starts a fresh reading; show its new state soon
});

function showSettings(on, id) {
  state.settings = on;
  $('settingsButton').setAttribute('aria-pressed', String(on));
  settingsEl.hidden = !on;
  if (on) {
    frame.hidden = true;
    notice.hidden = true;
    const target = id || state.selected;
    history.replaceState(null, '', `#settings${target ? '/' + target : ''}`);
    settingsView.open(target);
  } else {
    if (state.selected) history.replaceState(null, '', '#' + state.selected);
    render();
  }
}

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
  if (!turnedOn().some((m) => m.id === id)) return;
  state.selected = id;
  if (remember && state.settings) { state.settings = false; $('settingsButton').setAttribute('aria-pressed', 'false'); settingsEl.hidden = true; }
  if (remember) {
    try { localStorage.setItem('wg.module', id); } catch { /* storage unavailable */ }
    if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
  }
  render();
}

function initialChoice() {
  const on = turnedOn();
  const ids = on.map((m) => m.id);
  const fromHash = location.hash.slice(1).replace(/^settings\/?/, '');
  if (ids.includes(fromHash)) return fromHash;
  let last = null;
  try { last = localStorage.getItem('wg.module'); } catch { /* storage unavailable */ }
  if (ids.includes(last)) return last;
  return (on.find((m) => m.ok) || on[0])?.id || null;
}

function render() {
  const on = turnedOn();
  const cur = on.find((m) => m.id === state.selected) || null;

  // The selector button: the module in view, its state, and how much it has waiting.
  $('modName').textContent = cur ? cur.title : !state.loaded ? 'Modules' : state.modules.length ? 'All modules are off' : 'No modules';
  $('modDot').className = `dot ${cur ? levelOf(cur) : 'off'}`;
  const badge = $('modBadge');
  badge.hidden = !(cur && cur.problems);
  badge.textContent = cur?.problems || '';
  badge.className = `badge ${cur?.critical ? 'critical' : ''}`;
  badge.title = cur?.problems ? `${cur.problems} thing${cur.problems === 1 ? '' : 's'} to look at in ${cur.title}` : '';
  button.disabled = !on.length;

  menu.innerHTML = on.map((m) => `<li role="option" tabindex="-1" data-id="${esc(m.id)}" aria-selected="${m.id === state.selected}">
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
  if (state.settings) { frame.hidden = true; notice.hidden = true; return; }
  if (!state.modules.length) {
    showNotice('No modules are set up', 'Tell the shell where each module runs, as id=URL pairs in the order they should be listed:',
      { code: 'GALAXY_MODULES=kubernetes=http://host:8090,network=http://host:8091,esxi=http://host:8092' });
  } else if (!on.length) {
    showNotice('Every module is turned off', 'Turn on the ones you use in Settings. A module that is off is not listed here and is not checked.',
      { button: 'Open Settings', run: () => showSettings(true) });
  } else if (cur && cur.ok === false) {
    showNotice(`${cur.title} is not reachable`, `The shell could not read ${cur.url} (${cur.error || 'no answer'}). It checks again every ${state.every} seconds and will show the module as soon as it answers.`,
      { button: 'Check again now', run: () => load() });
  } else if (cur) {
    notice.hidden = true;
    frame.hidden = false;
    frame.title = cur.title;
    if (state.shown !== cur.id) { state.shown = cur.id; frame.src = embedUrl(cur); }
  }
}

function showNotice(title, text, { code = null, button: label = null, run = null } = {}) {
  frame.hidden = true;
  state.shown = null;
  frame.removeAttribute('src');
  notice.hidden = false;
  notice.innerHTML = `<h1>${esc(title)}</h1><p>${esc(text)}${code ? `<code>${esc(code)}</code>` : ''}</p>
    ${label ? `<button class="btn" id="noticeAct">${esc(label)}</button>` : ''}`;
  if (run) $('noticeAct')?.addEventListener('click', run);
}

async function load() {
  try {
    const res = await fetch('api/v1/modules', { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const data = await res.json();
    state.modules = data.modules || [];
    state.every = data.checkEverySeconds || 20;
    state.persistent = data.persistent !== false;
    const first = !state.loaded;
    state.loaded = true;
    if (first || !turnedOn().some((m) => m.id === state.selected)) {      // first visit, or the module in view was turned off
      state.selected = initialChoice();
      if (state.selected) pick(state.selected, { remember: false });
    }
    if (first && location.hash.startsWith('#settings')) showSettings(true, location.hash.split('/')[1]);
    else if (state.settings) settingsView.refresh();      // keeps the list of modules fresh, and leaves a form being typed in alone
    render();
  } catch (err) {
    if (!state.loaded) {
      state.loaded = true;
      showNotice('The shell is not answering', `Could not load the list of modules: ${err.message}.`, { button: 'Check again now', run: () => load() });
    }
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
window.addEventListener('hashchange', () => {
  const h = location.hash.slice(1);
  if (h.startsWith('settings')) { if (!state.settings) showSettings(true, h.split('/')[1]); return; }
  if (state.settings) { showSettings(false); }
  if (h && h !== state.selected) pick(h);
});
$('settingsButton').addEventListener('click', () => showSettings(!state.settings));

load();
(function poll() { setTimeout(async () => { await load(); poll(); }, Math.max(5, state.every) * 1000); })();

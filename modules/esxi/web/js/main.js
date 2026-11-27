// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// Wiring: data polling, URL state, selection, and the embedding API.
//
// URL parameters (all optional):
//   ns=a,b        VM groups to show (group ids)        focus=1     problems only
//   view=triage|strata   start in another view         select=<id> preselect a VM, datastore, network or host
//   embed=1       compact chrome for iframes           rail=0      hide the left rail
//   api=<url>     collector base URL (default: same origin and path as this page)
//
// postMessage (when embedded): emits {source:'esxi-galaxy', type:'select'|'ready', ...};
// accepts {type:'esxi-galaxy:command', command:'select'|'setGroups'|'setView'|'clear', ...}.
import { GalaxyScene } from './scene.js';
import { UI } from './ui.js';
import { Triage } from './triage.js';
import { keepFocus } from './format.js';

const params = new URLSearchParams(location.search);
// The build of the code this tab is running (from its own versioned URL /v/<build>/js/main.js).
const UI_BUILD = (import.meta.url.match(/\/v\/([^/]+)\/js\//) || [])[1] || null;
const API = (params.get('api') || '').replace(/\/?$/, params.get('api') ? '/' : '');
const embedded = params.get('embed') === '1';
if (embedded) document.body.classList.add('embed');
if (params.get('rail') === '0') document.body.classList.add('rail-hidden');

const VIEW_NAMES = ['triage', 'galaxy', 'strata'];      // an unknown view name opens the galaxy
const PANELS = ['triage'];                              // dashboards drawn over the paused map
const isPanel = (v) => PANELS.includes(v);
const state = {
  namespaces: params.get('ns') ? params.get('ns').split(',').filter(Boolean) : loadPref('ns', null),
  // The view from the URL, else the one used last time (someone who lives in Triage lands in Triage).
  view: VIEW_NAMES.includes(params.get('view')) ? params.get('view') : VIEW_NAMES.includes(loadPref('view', '')) ? loadPref('view', '') : 'galaxy',
  mapView: 'galaxy',
  selection: null,
  detail: null,       // the selected node fetched from the server, when the page's rolled-up graph doesn't hold it
  // Groups opened in place when the map is rolled up (problems only): ns:<group id>; oldest first.
  expand: (params.get('expand') || '').split(',').filter(Boolean),
  focus: params.has('focus') ? params.get('focus') === '1' : loadPref('focus', false),     // problems only
  graph: null,
  meta: null,
  allNamespaces: [],
  lastOk: 0,
  refreshSeconds: 15,
  inflight: null,
};

const ui = new UI({
  onNamespaces(list) {
    state.namespaces = list;
    savePref('ns', list);
    ui.setNamespaces(state.allNamespaces, list);
    syncUrl();
    load();
  },
  onSelect(sel, focus) { select(sel, focus); },
  onView(v) { setView(v); },
  onSection(s) { setView(isPanel(s) ? s : state.mapView); },
  onOptions(o) { scene.setOptions(o); },
  onClear() { select(null); },
  onExpand(token, open = true) { return setExpand(token, open); },
  onFocus(on) { state.focus = on; savePref('focus', on); syncUrl(); return load(); },
  onReveal(sel) { setView(isPanel(state.view) ? state.mapView : state.view); return reveal(sel); },
  search(q) { return getJSON('api/v1/search?q=' + encodeURIComponent(q)); },
  copy: copyText,
});

function copyText(text) {
  // navigator.clipboard needs a secure context; an internal dashboard is often plain http.
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).catch(() => {}); return; }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } catch { /* nothing else to try */ }
  ta.remove();
}

const triage = new Triage(document.getElementById('triage'), {
  onSelect(sel) { select(sel, false); },
  onShowOnMap(sel) { setView(state.mapView === 'triage' ? 'galaxy' : state.mapView); reveal(sel); },
  onGroup(ns) { ui.ctx.onNamespaces(ns ? [ns] : null); },
  scope() { return { namespaces: state.namespaces }; },
  fetchTable(kind, o) {
    const q = scopeQuery();
    q.delete('expand'); q.delete('focus');
    for (const [k, v] of Object.entries(o)) if (v !== '' && v != null) q.set(k, String(v));
    return getJSON(`api/v1/${kind}?${q}`);
  },
  async fetchPattern(key) {
    const q = scopeQuery();
    q.delete('focus');
    q.set('pattern', key);
    return { key, ...(await getJSON(`api/v1/triage?${q}`)) };
  },
  copy: copyText,
});

function scopeQuery() {
  const q = new URLSearchParams();
  if (state.namespaces && state.namespaces.length) q.set('ns', state.namespaces.join(','));
  if (state.expand.length) q.set('expand', state.expand.join(','));
  if (state.focus) q.set('focus', 'true');
  return q;
}

// Open a rolled-up group in place (or close it again).
function setExpand(token, open = true) {
  if (!token) return;
  const groupOf = new Map(state.allNamespaces.map((n) => [n.name, n.group]));
  let next = state.expand.filter((x) => x !== token);
  if (open) next.push(token);
  else if (token.startsWith('grp:')) next = next.filter((x) => !(x.startsWith('ns:') && groupOf.get(x.slice(3)) === token.slice(4)));
  state.expand = token === '*' ? [] : next;
  syncUrl();
  return load();
}

const scene = new GalaxyScene(document.getElementById('stage'), document.getElementById('labels'), {
  onSelect(hit) { select(hit, false); },
  onOpen(hit) {
    const n = state.graph?.nodes.find((x) => x.id === (hit.type === 'pod' ? hit.wid : hit.id));
    if (hit.type === 'pod' && hit.pod?.pseudo) setExpand(hit.pod.expand || n?.expand);
    else if (n?.aggregate && n.expand) setExpand(n.expand);
  },
  onBackground() { select(null); },
  onHover(hit, at) { ui.tooltip(hit, at); },
});

function applyInsets(instant = false) {
  const rail = document.getElementById('rail');
  const drawer = document.getElementById('drawer');
  const railW = rail && getComputedStyle(rail).display !== 'none' ? rail.getBoundingClientRect().right : 0;
  const wide = window.innerWidth > 820;   // on narrow screens the drawer is a bottom sheet
  const drawerW = wide && drawer && !drawer.hidden ? window.innerWidth - drawer.getBoundingClientRect().left : 0;
  scene.setInsets(railW, drawerW, instant);
}
window.addEventListener('resize', () => applyInsets(true));
applyInsets(true);

function applyView(v) {
  document.body.classList.toggle('view-triage', v === 'triage');
  document.getElementById('triage').hidden = v !== 'triage';
  ui.setView(v);
  scene.setPaused(isPanel(v));               // nothing to draw behind a dashboard
  if (v === 'triage') { ui.tooltip(null); triage.render(state.graph, state.meta); }
  else { state.mapView = v; scene.setView(v); }
}

function setView(v) {
  if (!VIEW_NAMES.includes(v)) v = 'galaxy';
  if (state.view === v) return;
  state.view = v;
  savePref('view', v);
  applyView(v);
  applyInsets(true);
  syncUrl();
}

// Show something that is folded into a rolled-up group on the map: open its group, then select it.
async function reveal(sel) {
  const id = sel.type === 'pod' ? sel.wid : sel.id;
  if (state.graph?.view?.collapsed && !inGraph(id)) {
    const ns = (id.match(/^(?:wl|entry|svc):([^/]+)\//) || [])[1];
    if (ns) await setExpand(`ns:${ns}`);
  }
  return select(sel, true);
}

const inGraph = (id) => !!state.graph && (state.graph.nodes.some((n) => n.id === id) || state.graph.edges.some((e) => e.id === id));

async function fetchDetail(id) {
  const q = scopeQuery();
  q.set('id', id);
  return getJSON(`api/v1/node?${q}`);
}

async function select(sel, focus) {
  // A member of a rolled-up group stands for a real VM; a slot or disk stands for the VM it belongs to.
  if (sel && sel.type === 'pod' && sel.pod?.pseudo) sel = { type: 'node', id: sel.pod.child };
  else if (sel && sel.type === 'pod') sel = { type: 'node', id: sel.wid || sel.key.split('#')[0] };
  const id = sel && (sel.type === 'pod' ? (sel.wid || sel.key.split('#')[0]) : sel.id);
  let detail = null;
  if (id && (sel.type === 'node' || sel.type === 'edge' || sel.type === 'pod') && state.graph && !inGraph(id)) {
    // Not on the page: the server has it. The drawer shows the real thing; the map highlights what it is drawn as.
    try { detail = await fetchDetail(id); } catch (err) { ui.toast(`Cannot open ${id}: ${err.message}`); return; }
  }
  state.selection = sel;
  state.detail = detail;
  const drawn = !detail ? sel : detail.at ? { type: detail.at.includes('->') ? 'edge' : 'node', id: detail.at } : null;
  scene.setSelection(drawn);
  ui.renderDrawer(sel, false, detail);
  if (detail) { focus = focus && !!drawn; sel = drawn || sel; }
  document.body.classList.toggle('drawer-open', !document.getElementById('drawer').hidden);
  applyInsets();
  if (state.graph) ui.renderRail(state.graph, scene.layerCounts(), state.selection);
  if (sel && focus && !isPanel(state.view)) {
    if (sel.type === 'node') scene.focus(sel.id);
    else if (sel.type === 'pod') scene.focus(sel.wid || sel.key.split('#')[0]);
    else if (sel.type === 'edge') scene.focusEdge(sel.id);
  }
  if (!sel) ui.tooltip(null);
  const picked = state.selection;
  post({ type: 'select', selection: picked ? { ...picked, node: picked.id ? (detail?.nodes.find((n) => n.id === picked.id) || state.graph?.nodes.find((n) => n.id === picked.id)) : undefined } : null });
  syncUrl();
}

async function getJSON(path) {
  const res = await fetch(API + path, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* ignore */ }
    throw new Error(`${res.status} ${detail || res.statusText}`);
  }
  return res.json();
}

async function load() {
  const q = scopeQuery();
  const token = Symbol('load');
  state.inflight = token;
  try {
    const [meta, nsResp] = await Promise.all([getJSON('api/v1/meta'), getJSON('api/v1/namespaces')]);
    let graph;
    if (state.namespaces && state.namespaces.length === 0) {
      graph = { nodes: [], edges: [], hotspots: [], paths: [], findings: [], changes: [], namespaces: nsResp.namespaces || [],
        sources: meta.sources, estate: null };
    } else {
      graph = await getJSON('api/v1/topology?' + q.toString());
    }
    if (state.inflight !== token) return;
    // The page keeps running across redeploys; notice when the server underneath changed,
    // or when this tab's code is not the build the server is running.
    const serverBuild = meta.assetBuild || `${meta.version}-${meta.build}`;
    if (!state.loadedVersion) {
      state.loadedVersion = { version: meta.version, build: meta.build, asset: serverBuild };
      if (UI_BUILD && UI_BUILD !== serverBuild) ui.showUpdate({ version: '?', build: UI_BUILD }, meta);
    } else if (serverBuild !== state.loadedVersion.asset) {
      ui.showUpdate(state.loadedVersion, meta);
    }
    meta.uiBuild = UI_BUILD;
    state.meta = meta;
    state.refreshSeconds = meta.refreshSeconds || 15;
    state.allNamespaces = nsResp.namespaces || [];
    state.graph = graph;
    state.lastOk = Date.now();
    ui.setNamespaces(state.allNamespaces, state.namespaces);
    ui.meta = meta;
    ui.setMeta(meta, graph);
    scene.setData(graph);
    // The server may have closed expansions to stay within what one screen can show.
    if (graph.view) state.expand = graph.view.expanded || [];
    // Keep the selection if it still exists.
    if (state.selection && state.detail) {
      const s = state.selection;
      try {
        state.detail = await fetchDetail(s.type === 'pod' ? (s.wid || s.key.split('#')[0]) : s.id);
        const at = state.detail.at;
        scene.setSelection(at ? { type: at.includes('->') ? 'edge' : 'node', id: at } : null);
      } catch { state.selection = null; state.detail = null; scene.setSelection(null); }
    } else if (state.selection) {
      const s = state.selection;
      const still = s.type === 'node' ? graph.nodes.some((n) => n.id === s.id)
        : s.type === 'edge' ? graph.edges.some((e) => e.id === s.id)
        : s.type === 'pod' ? graph.nodes.some((n) => n.id === (s.wid || s.key.split('#')[0])) : true;
      if (!still) state.selection = null;
      scene.setSelection(state.selection);
    } else if (params.get('select') && !state._preselected) {
      state._preselected = true;
      const id = params.get('select');
      select({ type: id.includes('->') && !graph.nodes.some((n) => n.id === id) ? 'edge' : 'node', id }, true);
    }
    keepFocus(() => {
      ui.renderRail(graph, scene.layerCounts(), state.selection);
      ui.renderDrawer(state.selection, true, state.detail);
      document.body.classList.toggle('drawer-open', !document.getElementById('drawer').hidden);
      ui.setTriageBadge(Triage.badge(graph));
      if (state.view === 'triage') triage.render(graph, meta);
    });
    applyInsets();
    ui.toast(meta.error ? `Last refresh failed: ${meta.error}` : null);
    if (!state._ready) { state._ready = true; post({ type: 'ready' }); }
  } catch (err) {
    if (state.inflight !== token) return;
    ui.toast(`Cannot load the map: ${err.message}`);
  }
}

function tickLive() {
  if (!state.lastOk) { ui.setLive('', 'connecting…'); return; }
  const age = Math.round((Date.now() - state.lastOk) / 1000);
  const stale = age > state.refreshSeconds * 3;
  ui.setLive(stale ? 'stale' : 'on', stale ? `stale · ${age}s` : `live · ${age}s ago`);
}

function syncUrl() {
  if (embedded) return;
  const q = new URLSearchParams(location.search);
  if (state.namespaces && state.namespaces.length) q.set('ns', state.namespaces.join(',')); else q.delete('ns');
  q.delete('platform'); q.delete('scenario');
  if (state.view !== 'galaxy') q.set('view', state.view); else q.delete('view');
  if (state.selection && (state.selection.type === 'node' || state.selection.type === 'edge')) q.set('select', state.selection.id); else q.delete('select');
  if (state.expand.length) q.set('expand', state.expand.join(',')); else q.delete('expand');
  if (state.focus) q.set('focus', '1'); else q.delete('focus');
  const s = q.toString();
  history.replaceState(null, '', location.pathname + (s ? '?' + s : ''));
}

function loadPref(k, dflt) {
  try { const v = localStorage.getItem('eg.' + k); return v == null ? dflt : JSON.parse(v); } catch { return dflt; }
}
function savePref(k, v) {
  try { localStorage.setItem('eg.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ }
}

function post(msg) {
  if (window.parent !== window) window.parent.postMessage({ source: 'esxi-galaxy', ...msg }, '*');
}

window.addEventListener('message', (ev) => {
  const m = ev.data;
  if (!m || m.type !== 'esxi-galaxy:command') return;
  if (m.command === 'select') select(m.id ? { type: 'node', id: m.id } : null, true);
  else if (m.command === 'clear') select(null);
  else if (m.command === 'setView') setView(m.view);
  else if (m.command === 'setGroups' || m.command === 'setNamespaces') ui.ctx.onNamespaces(m.groups ?? m.namespaces ?? null);
});

document.getElementById('tFocus').checked = state.focus;
applyView(state.view);
syncUrl();      // drops parameters this app does not have
load();
(function poll() {
  setTimeout(async () => { await load(); poll(); }, state.refreshSeconds * 1000);
})();
setInterval(tickLive, 1000);

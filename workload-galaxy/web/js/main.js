// Wiring: data polling, URL state, selection, and the embedding API.
//
// URL parameters (all optional):
//   ns=a,b        namespaces / projects to show        platform=1  include platform namespaces
//   view=strata|cascade  start in another view             select=<id> preselect a node
//   embed=1       compact chrome for iframes           rail=0      hide the left rail
//   api=<url>     collector base URL (default: same origin and path as this page)
//
// postMessage (when embedded): emits {source:'workload-galaxy', type:'select'|'ready', ...};
// accepts {type:'workload-galaxy:command', command:'select'|'setNamespaces'|'setView'|'clear', ...}.
import { GalaxyScene } from './scene.js';
import { UI } from './ui.js';

const params = new URLSearchParams(location.search);
// The build of the code this tab is running (from its own versioned URL /v/<build>/js/main.js).
const UI_BUILD = (import.meta.url.match(/\/v\/([^/]+)\/js\//) || [])[1] || null;
const API = (params.get('api') || '').replace(/\/?$/, params.get('api') ? '/' : '');
const embedded = params.get('embed') === '1';
if (embedded) document.body.classList.add('embed');
if (params.get('rail') === '0') document.body.classList.add('rail-hidden');

const state = {
  namespaces: params.get('ns') ? params.get('ns').split(',').filter(Boolean) : loadPref('ns', null),
  platform: params.has('platform') ? params.get('platform') === '1' : loadPref('platform', false),
  view: ['strata', 'cascade', 'gibson'].includes(params.get('view')) ? params.get('view') : 'galaxy',
  selection: null,
  graph: null,
  meta: null,
  allNamespaces: [],
  lastOk: 0,
  refreshSeconds: 15,
  inflight: null,
};

const ui = new UI({
  onNamespaces(list, platform) {
    state.namespaces = list;
    state.platform = platform;
    savePref('ns', list); savePref('platform', platform);
    ui.setNamespaces(state.allNamespaces, list, platform);
    syncUrl();
    load();
  },
  onSelect(sel, focus) { select(sel, focus); },
  onView(v) { setView(v); },
  onOptions(o) { scene.setOptions(o); },
  onClear() { select(null); },
  onScenarios(ids) { applyScenarios(ids); },
});

async function applyScenarios(ids) {
  try {
    const res = await fetch(API + 'api/v1/demo/scenarios', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active: ids }) });
    if (!res.ok) throw new Error(`${res.status} ${(await res.json()).detail || ''}`);
    state.scenarios = await res.json();
    ui.setScenarios(state.scenarios);
    syncUrl();
    await load();
  } catch (err) {
    ui.toast(`Scenario change failed: ${err.message}`);
  }
}

const scene = new GalaxyScene(document.getElementById('stage'), document.getElementById('labels'), {
  // In the Gibson view a clicked tower is brought front and center.
  onSelect(hit) { select(hit, state.view === 'gibson'); },
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

function setView(v) {
  if (state.view === v) return;
  state.view = v;
  document.body.classList.toggle('view-cascade', v === 'cascade');
  document.body.classList.toggle('view-gibson', v === 'gibson');
  ui.setView(v);
  scene.setView(v);
  syncUrl();
}

function select(sel, focus) {
  state.selection = sel;
  scene.setSelection(sel);
  ui.renderDrawer(sel);
  applyInsets();
  if (state.graph) ui.renderRail(state.graph, scene.layerCounts(), sel);
  if (sel && focus) {
    if (sel.type === 'node') scene.focus(sel.id);
    else if (sel.type === 'pod') scene.focus(sel.wid || sel.key.split('#')[0]);
    else if (sel.type === 'edge') scene.focus(sel.id.split('->')[1]);
    // A cascade is about the whole spread: frame the view instead of zooming into the root.
    else if (sel.type === 'cascade') scene.resetCamera(false);
  }
  if (!sel) ui.tooltip(null);
  post({ type: 'select', selection: sel ? { ...sel, node: sel.id ? state.graph?.nodes.find((n) => n.id === sel.id) : undefined } : null });
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
  const q = new URLSearchParams();
  if (state.namespaces && state.namespaces.length) q.set('ns', state.namespaces.join(','));
  if (state.platform) q.set('platform', 'true');
  const token = Symbol('load');
  state.inflight = token;
  try {
    const [meta, nsResp] = await Promise.all([getJSON('api/v1/meta'), getJSON('api/v1/namespaces')]);
    let graph;
    if (state.namespaces && state.namespaces.length === 0) {
      graph = { nodes: [], edges: [], hotspots: [], paths: [], namespaces: [], sources: meta.sources, cluster: meta.cluster };
    } else {
      graph = await getJSON('api/v1/topology?' + q.toString());
    }
    if (meta.mode === 'demo') {
      if (params.get('scenario') != null && !state._scenarioFromUrl) {
        state._scenarioFromUrl = true;
        await applyScenarios(params.get('scenario').split(',').filter(Boolean));
        return;
      }
      state.scenarios = await getJSON('api/v1/demo/scenarios');
    }
    if (state.inflight !== token) return;
    ui.setScenarios(meta.mode === 'demo' ? state.scenarios : null);
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
    state.allNamespaces = nsResp.namespaces;
    state.graph = graph;
    state.lastOk = Date.now();
    ui.setNamespaces(state.allNamespaces, state.namespaces, state.platform);
    ui.setMeta(meta, graph);
    scene.setData(graph);
    // Keep the selection if it still exists.
    if (state.selection) {
      const s = state.selection;
      const still = s.type === 'node' ? graph.nodes.some((n) => n.id === s.id)
        : s.type === 'edge' ? graph.edges.some((e) => e.id === s.id)
        : s.type === 'pod' ? graph.nodes.some((n) => n.id === (s.wid || s.key.split('#')[0]))
        : s.type === 'cascade' ? (graph.cascades || []).some((c) => c.root === s.root) : true;
      if (!still) state.selection = null;
      scene.setSelection(state.selection);
    } else if (params.get('select') && !state._preselected) {
      state._preselected = true;
      select({ type: 'node', id: params.get('select') }, true);
    }
    ui.renderRail(graph, scene.layerCounts(), state.selection);
    ui.renderDrawer(state.selection);
    applyInsets();
    ui.toast(meta.error ? `Last refresh failed: ${meta.error}` : null);
    if (!state._ready) { state._ready = true; post({ type: 'ready' }); }
  } catch (err) {
    if (state.inflight !== token) return;
    ui.toast(`Cannot load topology: ${err.message}`);
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
  if (state.platform) q.set('platform', '1'); else q.delete('platform');
  if (state.view !== 'galaxy') q.set('view', state.view); else q.delete('view');
  if (state.selection && state.selection.type === 'node') q.set('select', state.selection.id); else q.delete('select');
  if (state.scenarios && state.scenarios.active.length) q.set('scenario', state.scenarios.active.join(',')); else q.delete('scenario');
  const s = q.toString();
  history.replaceState(null, '', location.pathname + (s ? '?' + s : ''));
}

function loadPref(k, dflt) {
  try { const v = localStorage.getItem('wg.' + k); return v == null ? dflt : JSON.parse(v); } catch { return dflt; }
}
function savePref(k, v) {
  try { localStorage.setItem('wg.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ }
}

function post(msg) {
  if (window.parent !== window) window.parent.postMessage({ source: 'workload-galaxy', ...msg }, '*');
}

window.addEventListener('message', (ev) => {
  const m = ev.data;
  if (!m || m.type !== 'workload-galaxy:command') return;
  if (m.command === 'select') select(m.id ? { type: 'node', id: m.id } : null, true);
  else if (m.command === 'clear') select(null);
  else if (m.command === 'setView') setView(['strata', 'cascade', 'gibson'].includes(m.view) ? m.view : 'galaxy');
  else if (m.command === 'setNamespaces') ui.ctx.onNamespaces(m.namespaces ?? null, !!m.platform);
});

document.body.classList.toggle('view-cascade', state.view === 'cascade');
document.body.classList.toggle('view-gibson', state.view === 'gibson');
ui.setView(state.view);
scene.setView(state.view);
load();
(function poll() {
  setTimeout(async () => { await load(); poll(); }, state.refreshSeconds * 1000);
})();
setInterval(tickLive, 1000);

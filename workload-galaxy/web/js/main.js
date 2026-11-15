// Wiring: data polling, URL state, selection, and the embedding API.
//
// URL parameters (all optional):
//   ns=a,b        namespaces / projects to show        platform=1  include platform namespaces
//   view=triage|strata|cascade|gibson  start in another view   select=<id> preselect a node
//   embed=1       compact chrome for iframes           rail=0      hide the left rail
//   api=<url>     collector base URL (default: same origin and path as this page)
//
// postMessage (when embedded): emits {source:'workload-galaxy', type:'select'|'ready', ...};
// accepts {type:'workload-galaxy:command', command:'select'|'setNamespaces'|'setView'|'clear', ...}.
import { GalaxyScene } from './scene.js';
import { UI } from './ui.js';
import { Triage } from './triage.js';

const params = new URLSearchParams(location.search);
// The build of the code this tab is running (from its own versioned URL /v/<build>/js/main.js).
const UI_BUILD = (import.meta.url.match(/\/v\/([^/]+)\/js\//) || [])[1] || null;
const API = (params.get('api') || '').replace(/\/?$/, params.get('api') ? '/' : '');
const embedded = params.get('embed') === '1';
if (embedded) document.body.classList.add('embed');
if (params.get('rail') === '0') document.body.classList.add('rail-hidden');

const VIEW_NAMES = ['triage', 'galaxy', 'strata', 'cascade', 'gibson'];
const state = {
  namespaces: params.get('ns') ? params.get('ns').split(',').filter(Boolean) : loadPref('ns', null),
  platform: params.has('platform') ? params.get('platform') === '1' : loadPref('platform', false),
  // The view from the URL, else the one used last time (someone who lives in Triage lands in Triage).
  view: VIEW_NAMES.includes(params.get('view')) ? params.get('view') : VIEW_NAMES.includes(loadPref('view', '')) ? loadPref('view', '') : 'galaxy',
  mapView: 'galaxy',
  selection: null,
  detail: null,       // the selected node fetched from the server, when the page's rolled-up graph doesn't hold it
  // Units opened in place on a large estate: grp:<business unit>, ns:<project>; oldest first.
  expand: (params.get('expand') || '').split(',').filter(Boolean),
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
  onSection(s) { setView(s === 'triage' ? 'triage' : state.mapView); },
  onOptions(o) { scene.setOptions(o); },
  onClear() { select(null); },
  onScenarios(ids) { applyScenarios(ids); },
  onExpand(token, open = true) { return setExpand(token, open); },
  onReveal(sel) { setView(state.view === 'triage' ? state.mapView : state.view); return reveal(sel); },
  search(q) { return getJSON('api/v1/search?q=' + encodeURIComponent(q)); },
  async fetchLogs(ns, pod, container, previous) {
    const q = new URLSearchParams({ ns, pod, previous: previous ? 'true' : 'false' });
    if (container) q.set('container', container);
    return getJSON('api/v1/logs?' + q.toString());
  },
  copy: copyText,
});

function copyText(text) {
  // navigator.clipboard needs a secure context; dashboards are often plain http on a LAN.
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
  onProject(ns) { ui.ctx.onNamespaces([ns], state.platform); },
  scope() { return { namespaces: state.namespaces, platform: state.platform }; },
  fetchTable(kind, o) {
    const q = scopeQuery();
    q.delete('expand');
    for (const [k, v] of Object.entries(o)) if (v !== '' && v != null) q.set(k, String(v));
    return getJSON(`api/v1/${kind}?${q}`);
  },
  async fetchPattern(key) {
    const q = scopeQuery();
    q.set('pattern', key);
    return { key, ...(await getJSON(`api/v1/triage?${q}`)) };
  },
  async onPlatformNode(id) {      // platform workloads are outside the default filter: bring them in, then open it
    if (!state.platform) { state.platform = true; savePref('platform', true); ui.setNamespaces(state.allNamespaces, state.namespaces, true); await load(); }
    select({ type: 'node', id }, false);
  },
  copy: copyText,
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

function scopeQuery() {
  const q = new URLSearchParams();
  if (state.namespaces && state.namespaces.length) q.set('ns', state.namespaces.join(','));
  if (state.platform) q.set('platform', 'true');
  if (state.expand.length) q.set('expand', state.expand.join(','));
  return q;
}

// Open a rolled-up project / business unit in place (or close it again).
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
  // In the Gibson view a clicked tower is brought front and center.
  onSelect(hit) { select(hit, state.view === 'gibson'); },
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
  document.body.classList.toggle('view-cascade', v === 'cascade');
  document.body.classList.toggle('view-gibson', v === 'gibson');
  document.body.classList.toggle('view-triage', v === 'triage');
  document.getElementById('triage').hidden = v !== 'triage';
  ui.setView(v);
  scene.setPaused(v === 'triage');           // nothing to draw behind the dashboard
  if (v === 'triage') { ui.tooltip(null); triage.render(state.graph, state.meta); } else { state.mapView = v; scene.setView(v); }
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

// Show something that is folded into a project or unit on the map: open its project, then select it.
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
  // A child of a rolled-up unit (drawn as a pod of it) stands for a real workload or project.
  if (sel && sel.type === 'pod' && sel.pod?.pseudo) sel = { type: 'node', id: sel.pod.child };
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
  if (sel && focus && state.view !== 'triage') {
    if (sel.type === 'node') scene.focus(sel.id);
    else if (sel.type === 'pod') scene.focus(sel.wid || sel.key.split('#')[0]);
    else if (sel.type === 'edge') scene.focus(sel.id.split('->')[1]);
    // A cascade is about the whole spread: frame the view instead of zooming into the root.
    else if (sel.type === 'cascade') scene.resetCamera(false);
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
        : s.type === 'pod' ? graph.nodes.some((n) => n.id === (s.wid || s.key.split('#')[0]))
        : s.type === 'cascade' ? (graph.cascades || []).some((c) => c.root === s.root) : true;
      if (!still) state.selection = null;
      scene.setSelection(state.selection);
    } else if (params.get('select') && !state._preselected) {
      state._preselected = true;
      select({ type: 'node', id: params.get('select') }, true);
    }
    ui.renderRail(graph, scene.layerCounts(), state.selection);
    ui.meta = meta;
    ui.renderDrawer(state.selection, true, state.detail);
    document.body.classList.toggle('drawer-open', !document.getElementById('drawer').hidden);
    ui.setTriageBadge(Triage.badge(graph));
    if (state.view === 'triage') triage.render(graph, meta);
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
  if (state.expand.length) q.set('expand', state.expand.join(',')); else q.delete('expand');
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
  else if (m.command === 'setView') setView(m.view);
  else if (m.command === 'setNamespaces') ui.ctx.onNamespaces(m.namespaces ?? null, !!m.platform);
});

applyView(state.view);
load();
(function poll() {
  setTimeout(async () => { await load(); poll(); }, state.refreshSeconds * 1000);
})();
setInterval(tickLive, 1000);

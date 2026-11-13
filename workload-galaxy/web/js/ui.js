// DOM panels around the 3D view.
import { esc, fmtAge, fmtBytes, fmtCpu, fmtMs, fmtNum, fmtPct, edgeMetric, LEVEL_COLOR, nsColor, podLevel } from './format.js';

const $ = (sel) => document.querySelector(sel);
const KIND_LABEL = { workload: null, entry: null, external: 'External dependency', clients: 'Outside the cluster',
  service: 'Service without pods', host: 'Host' };

export class UI {
  constructor(ctx) {
    this.ctx = ctx;   // { onNamespaces(list|null, platform), onSelect(sel, focus), onView(v), onOptions(o), onClear() }
    this.graph = null;
    this._bindTop();
    this._bindToggles();
    $('#drawerClose').addEventListener('click', () => ctx.onClear());
  }

  // ------------------------------------------------------------------ top bar
  _bindTop() {
    document.querySelectorAll('.seg button').forEach((b) => b.addEventListener('click', () => this.ctx.onView(b.dataset.view)));
    const btn = $('#nsButton'), menu = $('#nsMenu');
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute('aria-expanded', String(!menu.hidden));
      if (!menu.hidden) $('#nsFilter').focus();
    });
    document.addEventListener('click', (ev) => {
      if (!menu.hidden && !menu.contains(ev.target)) { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
      const sr = $('#searchResults');
      if (!sr.hidden && !sr.contains(ev.target) && ev.target !== $('#search')) sr.hidden = true;
    });
    $('#nsFilter').addEventListener('input', () => this._renderNsList());
    $('#nsAll').addEventListener('click', () => this.ctx.onNamespaces(null, this.platform));
    $('#nsNone').addEventListener('click', () => this.ctx.onNamespaces([], this.platform));
    $('#platformToggle').addEventListener('change', (ev) => this.ctx.onNamespaces(this.selectedNs, ev.target.checked));
    const search = $('#search');
    search.addEventListener('input', () => this._renderSearch());
    search.addEventListener('focus', () => this._renderSearch());
    search.addEventListener('keydown', (ev) => {
      const items = [...document.querySelectorAll('#searchResults li')];
      let i = items.findIndex((x) => x.classList.contains('active'));
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        items[i]?.classList.remove('active');
        i = ev.key === 'ArrowDown' ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
        items[i]?.classList.add('active');
      } else if (ev.key === 'Enter') {
        (items[i] || items[0])?.click();
      } else if (ev.key === 'Escape') {
        $('#searchResults').hidden = true; search.blur();
      }
    });
    window.addEventListener('keydown', (ev) => {
      if (ev.key === '/' && document.activeElement.tagName !== 'INPUT') { ev.preventDefault(); search.focus(); }
      if (ev.key === 'Escape' && document.activeElement.tagName !== 'INPUT') this.ctx.onClear();
    });
  }

  _bindToggles() {
    const map = { tPods: 'pods', tInferred: 'inferred', tResponses: 'responses', tLabels: 'labels', tRotate: 'rotate' };
    for (const [id, key] of Object.entries(map)) {
      $('#' + id).addEventListener('change', (ev) => this.ctx.onOptions({ [key]: ev.target.checked }));
    }
  }

  // ------------------------------------------------------------------ scenarios (demo mode)
  _bindScenarios() {
    const btn = $('#scenarioButton'), menu = $('#scenarioMenu');
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      menu.hidden = !menu.hidden;
      btn.setAttribute('aria-expanded', String(!menu.hidden));
    });
    document.addEventListener('click', (ev) => {
      if (!menu.hidden && !menu.contains(ev.target) && !btn.contains(ev.target)) { menu.hidden = true; }
    });
    $('#scenarioClear').addEventListener('click', () => this.ctx.onScenarios([]));
  }

  setScenarios(info) {
    if (!this._scenariosBound) { this._bindScenarios(); this._scenariosBound = true; }
    this.scenarios = info;
    $('#scenarioPicker').hidden = !info;
    if (!info) { $('#scenarioGuide').hidden = true; return; }
    const active = new Set(info.active);
    const LC = { users: '#4fd1ff', services: '#4f7dff', pods: '#8b5cf6', hosts: '#19c9a0' };
    $('#scenarioSummary').textContent = !info.active.length ? 'Baseline'
      : info.active.length === 1 ? info.available.find((s) => s.id === info.active[0]).title : `${info.active.length} scenarios`;
    $('#scenarioList').innerHTML = info.available.map((s) => `<li><label>
      <input type="checkbox" data-id="${esc(s.id)}" ${active.has(s.id) ? 'checked' : ''}>
      <span class="sc-title">${esc(s.title)}</span><span class="sc-sum">${esc(s.summary)}</span>
      <span class="tags">${s.layers.map((l) => `<span class="ltag" style="--c:${LC[l]}">${l}</span>`).join('')}</span>
      </label></li>`).join('');
    $('#scenarioList').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
      const next = [...$('#scenarioList').querySelectorAll('input:checked')].map((x) => x.dataset.id);
      this.ctx.onScenarios(next);
    }));
    // Rail guide: what each strata layer shows for the active scenarios.
    const guide = $('#scenarioGuide');
    const act = info.available.filter((s) => active.has(s.id) && s.guide);
    guide.hidden = !act.length;
    const ORDER = [['users', 'End users'], ['services', 'Services'], ['pods', 'Pods & containers'], ['hosts', 'Hosts']];
    guide.innerHTML = `<h2>What to look for</h2>` + act.map((s) => `<div class="guide">
      <div class="gt">${esc(s.title)}<button class="link" data-focus="${esc(s.guide.focus || '')}">Show in strata</button></div>
      <ol>${ORDER.map(([k, label]) => `<li class="${s.layers.includes(k) ? 'root' : ''}" style="--c:${LC[k]}"><span class="bar"></span>
        <span><b>${label}</b>${esc(s.guide.layers[k] || '')}</span></li>`).join('')}</ol></div>`).join('');
    guide.querySelectorAll('[data-focus]').forEach((b) => b.addEventListener('click', () => {
      this.ctx.onView('strata');
      if (b.dataset.focus) this.ctx.onSelect({ type: 'node', id: b.dataset.focus }, true);
    }));
  }

  setView(view) {
    document.querySelectorAll('.seg button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  }

  setNamespaces(all, selected, platform) {
    this.allNs = all || [];
    this.selectedNs = selected;
    this.platform = platform;
    $('#platformToggle').checked = platform;
    const label = selected === null ? (platform ? 'All namespaces' : 'All applications')
      : selected.length === 0 ? 'None' : selected.length <= 2 ? selected.join(', ') : `${selected.length} selected`;
    $('#nsSummary').textContent = label;
    this._renderNsList();
  }

  _renderNsList() {
    const q = $('#nsFilter').value.trim().toLowerCase();
    const list = (this.allNs || []).filter((n) => (this.platform || !n.platform || (this.selectedNs || []).includes(n.name))
      && (!q || n.name.toLowerCase().includes(q)));
    const sel = this.selectedNs;
    $('#nsList').innerHTML = list.map((n) => {
      const checked = sel === null ? true : sel.includes(n.name);
      const dot = n.critical ? LEVEL_COLOR.critical : n.warning ? LEVEL_COLOR.warning : nsColor(n.name, n.platform);
      return `<li><label><input type="checkbox" data-ns="${esc(n.name)}" ${checked ? 'checked' : ''}>
        <span class="swatch" style="background:${dot}"></span><span>${esc(n.name)}</span>
        ${n.platform ? '<span class="platform">platform</span>' : ''}
        <span class="count">${n.workloads} wl · ${n.pods} pods</span></label></li>`;
    }).join('') || '<li class="empty">No matching namespaces</li>';
    $('#nsList').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
      const visible = list.map((n) => n.name);
      let next = sel === null ? visible.slice() : sel.slice();
      if (cb.checked) next.push(cb.dataset.ns); else next = next.filter((x) => x !== cb.dataset.ns);
      next = [...new Set(next)];
      this.ctx.onNamespaces(next, this.platform);
    }));
  }

  _renderSearch() {
    const q = $('#search').value.trim().toLowerCase();
    const box = $('#searchResults');
    if (!q || !this.graph) { box.hidden = true; return; }
    const hits = [];
    for (const n of this.graph.nodes) {
      if (n.name.toLowerCase().includes(q) || (n.namespace || '').toLowerCase().includes(q)) {
        hits.push({ sel: { type: 'node', id: n.id }, name: n.name, ns: n.namespace, kind: n.subkind || n.kind });
      }
      if (n.kind === 'workload') for (const p of n.pods) {
        if (p.name.toLowerCase().includes(q) || (p.ip || '').startsWith(q)) {
          hits.push({ sel: { type: 'pod', key: `${n.id}#${p.name}`, wid: n.id, pod: p }, name: p.name, ns: n.namespace, kind: 'Pod' });
        }
      }
    }
    box.innerHTML = hits.slice(0, 14).map((h, i) => `<li data-i="${i}"><span>${esc(h.name)}</span>
      <span class="ns">${esc(h.ns || '')}</span><span class="kind">${esc(h.kind)}</span></li>`).join('')
      || '<li class="empty">Nothing found</li>';
    box.hidden = false;
    box.querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const h = hits[+li.dataset.i];
      box.hidden = true;
      $('#search').value = '';
      this.ctx.onSelect(h.sel, true);
    }));
  }

  // What is running: collector version/build, and whether every agent matches it.
  setVersion(meta) {
    const el = $('#version');
    if (!meta || !meta.version) { el.hidden = true; return; }
    const agents = meta.sources?.agent?.agents || [];
    const odd = agents.filter((a) => a.version !== meta.version || (a.build && meta.build && a.build !== meta.build));
    const serverBuild = meta.assetBuild || `${meta.version}-${meta.build}`;
    const staleUi = meta.uiBuild && meta.uiBuild !== serverBuild;
    el.hidden = false;
    el.className = `chip version ${odd.length || staleUi ? 'warn' : 'subtle'}`;
    el.textContent = `v${meta.version}${staleUi ? ' · page out of date' : ''}${odd.length ? ` · agent ${odd.map((a) => 'v' + a.version).join(', ')}` : ''}`;
    el.title = [`Collector v${meta.version} · build ${meta.build || '-'}`,
      `This page's code: ${meta.uiBuild || 'unversioned'}${staleUi ? ' (reload to update)' : ''}`,
      meta.startedAt ? `started ${meta.startedAt}` : '',
      ...agents.map((a) => `Agent on ${a.node}: v${a.version}${a.build ? ' · build ' + a.build : ''} · last report ${Math.round(a.lastReportS)}s ago`),
      odd.length ? 'Agents and collector differ: a rollout is in progress or one side failed to update.' : ''].filter(Boolean).join('\n');
  }

  showUpdate(from, to) {
    $('#updateText').textContent = `Workload Galaxy was updated (v${from.version} build ${from.build} → v${to.version} build ${to.build}).`;
    $('#updateBanner').hidden = false;
    $('#updateReload').onclick = () => location.reload();
  }

  setMeta(meta, graph) {
    this.setVersion(meta);
    $('#cluster').textContent = graph?.cluster ? `${graph.cluster.openshift ? 'OpenShift' : 'Kubernetes'} · ${graph.cluster.name}` : '';
    const s = graph?.sources || meta?.sources || {};
    const chips = [];
    const agent = s.agent || {};
    chips.push(agent.nodes ? `<span class="chip ok" title="Live socket and conntrack telemetry from ${agent.nodes} node(s)">● Flows ${agent.nodes} node${agent.nodes > 1 ? 's' : ''}</span>`
      : `<span class="chip off" title="No node agent reporting: edges come from API objects and config only">○ Flows</span>`);
    chips.push(s.metrics ? '<span class="chip ok" title="metrics.k8s.io CPU/memory">● Metrics</span>' : '<span class="chip off">○ Metrics</span>');
    const prom = s.prometheus || {};
    chips.push(prom.enabled && !prom.error ? `<span class="chip ok" title="Request latency from service mesh metrics">● Mesh${prom.edges ? ' ' + prom.edges : ''}</span>`
      : prom.error ? `<span class="chip warn" title="${esc(prom.error)}">◐ Mesh</span>` : '<span class="chip off" title="Set GALAXY_PROMETHEUS_URL for request-level latency">○ Mesh</span>');
    // Demo mode is already announced by the Simulate picker.
    const warn = (s.kubernetes?.warnings || []).length;
    if (warn) chips.push(`<span class="chip warn" title="${esc(s.kubernetes.warnings.join('\n'))}">${warn} API warning${warn > 1 ? 's' : ''}</span>`);
    $('#sources').innerHTML = chips.join('');
  }

  setLive(state, text) {
    const el = $('#live');
    el.className = `live ${state}`;
    $('#liveText').textContent = text;
  }

  toast(msg) {
    const t = $('#toast');
    if (!msg) { t.hidden = true; return; }
    t.textContent = msg; t.hidden = false;
  }

  // ------------------------------------------------------------------ rail
  renderRail(graph, counts, selection) {
    this.graph = graph;
    const layers = [
      ['End users', '#4fd1ff', counts.users], ['Services', '#4f7dff', counts.services],
      ['Pods & containers', '#8b5cf6', counts.pods], ['Hosts', '#19c9a0', counts.hosts]];
    document.querySelector('#layers .layers').innerHTML = layers.map(([name, c, k]) => `<li style="--c:${c}">
      <span class="bar"></span><span class="name">${name}</span>
      <span class="meta">${k.shown} shown${k.critical ? ` · <span class="c">${k.critical} critical</span>` : ''}${k.warning ? ` · <span class="w">${k.warning} warning</span>` : ''}${k.evicted ? ` · <span class="e">${k.evicted} evicted</span>` : ''}${k.unscheduled ? ` · <span class="c">${k.unscheduled} unscheduled</span>` : ''}</span></li>`).join('');

    const cascades = graph.cascades || [];
    const KIND = { host: 'node', workload: 'workload', call: 'call', service: 'service', entry: 'route' };
    $('#cascades').innerHTML = cascades.length ? cascades.slice(0, 8).map((c, i) => `<li data-i="${i}"
      class="${selection?.type === 'cascade' && selection.root === c.root ? 'sel' : ''}">
      <div class="row1"><span class="lvl" style="background:${LEVEL_COLOR[c.level]}"></span>
      <span class="nm">${esc(c.name)}</span><span class="tag">${KIND[c.kind] || c.kind}</span></div>
      <div class="why">→ ${c.count} degraded dependent${c.count === 1 ? '' : 's'}${c.dominant.length ? ` · main cause for ${c.dominant.length}` : ''}
      ${c.userFacing ? ' · <span class="tag users">reaches users</span>' : ''}</div>
      ${c.reasons[0] ? `<div class="why ns">${esc(c.reasons[0])}</div>` : ''}</li>`).join('')
      : '<li class="empty">No failure is spreading to other workloads.</li>';
    $('#cascades').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const c = cascades[+li.dataset.i];
      this.ctx.onView('cascade');
      this.ctx.onSelect({ type: 'cascade', root: c.root }, true);
    }));

    const selId = selection?.id;
    const hot = graph.hotspots || [];
    $('#hotspots').innerHTML = hot.length ? hot.slice(0, 12).map((h, i) => `<li data-i="${i}" class="${selId === h.id ? 'sel' : ''}">
      <div class="row1"><span class="lvl" style="background:${LEVEL_COLOR[h.level]}"></span>
      <span class="nm">${esc(h.name)}</span><span class="score">${Math.round(h.score * 100)}</span></div>
      <div class="why">${esc(h.reasons[0] || '')}</div>
      ${h.namespace ? `<div class="why ns">${esc(h.namespace)}${h.type === 'edge' ? ' · call' : ''}</div>` : ''}</li>`).join('')
      : '<li class="empty">No latency or availability risks detected.</li>';
    $('#hotspots').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const h = hot[+li.dataset.i];
      this.ctx.onSelect(h.type === 'edge' ? { type: 'edge', id: h.id } : { type: 'node', id: h.id }, true);
    }));

    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const paths = (graph.paths || []).filter((p) => p.score > 0 || p.latencyMs);
    $('#paths').innerHTML = paths.length ? paths.slice(0, 6).map((p, i) => {
      const lvl = p.score >= 0.6 ? 'critical' : p.score >= 0.25 ? 'warning' : 'ok';
      const crumbs = p.nodes.map((id) => {
        const n = byId.get(id);
        const bad = n && n.risk && n.risk.level !== 'ok';
        return bad ? `<b>${esc(n.name)}</b>` : esc(n ? n.name : id);
      }).join(' › ');
      const lat = p.latencyMs != null ? `≈${fmtMs(p.latencyMs)} over ${p.measuredHops}/${p.hops} measured hops` : `${p.hops} hops, no latency data`;
      return `<li data-i="${i}" class="${selection?.type === 'path' && selection.idx === i ? 'sel' : ''}">
        <div class="row1"><span class="lvl" style="background:${LEVEL_COLOR[lvl]}"></span><span class="nm">${esc(lat)}</span></div>
        <div class="crumbs">${crumbs}</div></li>`;
    }).join('') : '<li class="empty">No request paths from outside the cluster in view.</li>';
    $('#paths').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const p = paths[+li.dataset.i];
      this.ctx.onSelect({ type: 'path', idx: +li.dataset.i, nodes: p.nodes, edges: p.edges }, false);
    }));
  }

  // ------------------------------------------------------------------ tooltip
  tooltip(hit, at) {
    const tip = $('#tooltip');
    if (!hit || !at || !this.graph) { tip.hidden = true; return; }
    let html = '';
    if (hit.type === 'pod') {
      const p = hit.pod;
      const lvl = podLevel(p);
      html = `<div class="t1">${esc(p.name)}</div><div class="t2">${esc(p.phase)}${p.reason ? ' · ' + esc(p.reason) : ''}
        · ${p.ready ? 'ready' : 'not ready'} · ${p.node ? 'on ' + esc(p.node) : '<span style="color:var(--crit)">no node</span>'}</div>
        <div class="t2">cpu ${fmtCpu(p.cpu)}${p.cpuLimit ? ' / ' + fmtCpu(p.cpuLimit) : ''} · mem ${fmtBytes(p.mem)}${p.memLimit ? ' / ' + fmtBytes(p.memLimit) : ''} · ${p.restarts} restarts</div>
        ${(p.issues || []).map((x) => `<div class="t2 issue ${lvl}">${esc(x)}</div>`).join('')}`;
    } else {
      const n = this.graph.nodes.find((x) => x.id === hit.id);
      if (!n) { tip.hidden = true; return; }
      const r = n.risk || {};
      html = `<div class="t1">${esc(n.name)}</div><div class="t2">${esc(n.subkind || n.kind)}${n.namespace ? ' · ' + esc(n.namespace) : ''}${n.ghost ? ' · outside filter' : ''}</div>`;
      if (n.kind === 'workload') html += `<div class="t2">${n.replicas.ready}/${n.replicas.desired} ready · ${n.pods.length} pods</div>`;
      if (r.reasons && r.reasons.length) html += `<div class="t2" style="color:${LEVEL_COLOR[r.level]}">${esc(r.reasons[0].text)}</div>`;
      else if (r.impactLevel && r.impactLevel !== 'ok') html += `<div class="t2" style="color:${LEVEL_COLOR[r.impactLevel]}">Slowed by a downstream dependency</div>`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, at.x + 16) + 'px';
    tip.style.top = Math.min(window.innerHeight - h - 8, at.y + 14) + 'px';
  }

  // ------------------------------------------------------------------ drawer
  renderDrawer(sel) {
    const d = $('#drawer');
    if (!sel || !this.graph || sel.type === 'path') { d.hidden = true; return; }
    const byId = new Map(this.graph.nodes.map((n) => [n.id, n]));
    let html = '';
    if (sel.type === 'cascade') {
      // Show the root itself (node or call); the blast radius section is appended below.
      const isEdge = sel.root.includes('->') && !byId.has(sel.root);
      sel = isEdge ? { type: 'edge', id: sel.root } : { type: 'node', id: sel.root };
    }
    if (sel.type === 'edge') {
      const e = this.graph.edges.find((x) => x.id === sel.id);
      if (!e) { d.hidden = true; return; }
      html = this._edgeHtml(e, byId);
    } else {
      const id = sel.type === 'pod' ? sel.wid || sel.key.split('#')[0] : sel.id;
      const n = byId.get(id);
      if (!n) { d.hidden = true; return; }
      html = this._nodeHtml(n, byId, sel.type === 'pod' ? sel.key.split('#')[1] : null);
    }
    html += this._blast(sel.type === 'edge' ? sel.id : sel.type === 'node' ? sel.id : null, byId);
    $('#drawerBody').innerHTML = html;
    d.hidden = false;
    d.querySelectorAll('[data-cascade]').forEach((el) => el.addEventListener('click', () => {
      this.ctx.onView('cascade');
      this.ctx.onSelect({ type: 'cascade', root: el.dataset.cascade }, true);
    }));
    d.querySelectorAll('[data-node]').forEach((el) => el.addEventListener('click', () => this.ctx.onSelect({ type: 'node', id: el.dataset.node }, true)));
    d.querySelectorAll('[data-edge]').forEach((el) => el.addEventListener('click', () => this.ctx.onSelect({ type: 'edge', id: el.dataset.edge }, false)));
    d.querySelectorAll('[data-pod]').forEach((el) => el.addEventListener('click', () => {
      const [wid, pod] = el.dataset.pod.split('#');
      this.ctx.onSelect({ type: 'pod', key: el.dataset.pod, wid }, false);
    }));
  }

  // If this node/call is the root of a failure cascade: who it is slowing down.
  _blast(id, byId) {
    const c = id && (this.graph.cascades || []).find((x) => x.root === id);
    if (!c) return '';
    const dom = new Set(c.dominant);
    const chips = c.impacted.map((nid) => {
      const n = byId.get(nid);
      return n ? `<a data-node="${esc(nid)}" class="${dom.has(nid) ? 'dom' : ''}" title="${dom.has(nid) ? 'main cause' : 'contributing'}">${esc(n.name)}</a>` : '';
    }).join('');
    return `<div class="d-sec"><h3>Blast radius · ${c.count} degraded dependent${c.count === 1 ? '' : 's'}${c.userFacing ? ' · reaches users' : ''}</h3>
      <div class="blast">${chips}</div>
      <div style="margin-top:8px"><a class="link" data-cascade="${esc(c.root)}" style="cursor:pointer;color:var(--cyan)">Show this cascade ▸</a></div></div>`;
  }

  _risk(r) {
    if (!r) return '';
    const okText = r.impactLevel && r.impactLevel !== 'ok' ? 'No issues of its own' : 'Healthy';
    let html = `<div class="risk-badge ${r.level}">${r.level === 'ok' ? okText : `${r.level === 'warning' ? 'At risk' : 'Critical'} · ${Math.round(r.score * 100)}`}</div>`;
    if (r.reasons && r.reasons.length) {
      html += `<div class="d-sec"><h3>Why</h3><ul class="reasons">${r.reasons.map((x) => `<li><div>${esc(x.text)}
        <div class="cat">${esc(x.category)}</div></div><div class="meter" style="--c:${x.weight >= 0.6 ? LEVEL_COLOR.critical : x.weight >= 0.25 ? LEVEL_COLOR.warning : LEVEL_COLOR.ok}">
        <i style="width:${Math.round(x.weight * 100)}%"></i></div></li>`).join('')}</ul></div>`;
    }
    if (r.impactLevel && r.impactLevel !== 'ok' && r.impactFrom) {
      const from = r.impactFrom;
      const isEdge = from.includes('->');
      const isHost = from.startsWith('host:');
      const name = isEdge ? from.split('->').map((x) => x.split('/').pop()).join(' → ')
        : isHost ? (from === 'host:(unscheduled)' ? 'pods waiting to be scheduled' : from.slice(5)) : from.split('/').pop();
      const lead = isHost ? 'Risk inherited from the infrastructure underneath' : 'Latency risk inherited from downstream';
      html += `<div class="impact">${lead} (${Math.round(r.impact * 100)}):
        root cause <a ${isEdge ? `data-edge="${esc(from)}"` : `data-node="${esc(from)}"`}>${esc(name)}</a></div>`;
    }
    return html;
  }

  _peers(list, byId, dir) {
    if (!list.length) return '<div class="empty">None</div>';
    return `<ul class="peers">${list.map((e) => {
      const other = byId.get(dir === 'up' ? e.source : e.target);
      const name = other ? other.name : (dir === 'up' ? e.source : e.target);
      const metric = edgeMetric(e);
      const lvl = e.risk.level;
      return `<li data-edge="${esc(e.id)}"><div class="p1"><span class="st" style="background:${LEVEL_COLOR[lvl]}"></span>
        <span class="nm">${esc(name)}</span>${other && other.namespace ? `<span class="ns" style="color:var(--faint);font-size:11px">${esc(other.namespace)}</span>` : ''}</div>
        <div class="p2">${e.evidence.map((x) => `<span class="ev ${x}">${x}</span>`).join('')}${metric ? `<span>${esc(metric)}</span>` : ''}
        ${e.via.length ? `<span>via ${esc(e.via.join(', '))}</span>` : ''}</div></li>`;
    }).join('')}</ul>`;
  }

  _nodeHtml(n, byId, podName) {
    const inbound = this.graph.edges.filter((e) => e.target === n.id);
    const outbound = this.graph.edges.filter((e) => e.source === n.id);
    const kind = KIND_LABEL[n.kind] || n.subkind;
    let html = `<div class="d-kind">${esc(kind)}</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${n.namespace ? esc(n.namespace) : ''}${n.ghost ? ' · outside current filter' : ''}</div>`;
    html += this._risk(n.risk);
    if (n.kind === 'workload') {
      const u = n.usage || {};
      html += `<div class="d-sec"><h3>Status</h3><dl class="kv">
        <dt>Replicas</dt><dd>${n.replicas.ready}/${n.replicas.desired} ready${n.replicas.current !== n.replicas.desired ? ` (${n.replicas.current} running)` : ''}${n.idle ? ' · scaled to zero' : ''}</dd>
        ${n.hpa ? `<dt>Autoscaler</dt><dd>${n.hpa.current ?? '–'} of ${n.hpa.min}–${n.hpa.max}</dd>` : ''}
        <dt>CPU / limit</dt><dd>${fmtPct(u.cpuRatio)}</dd><dt>Memory / limit</dt><dd>${fmtPct(u.memRatio)}</dd>
        <dt>Images</dt><dd class="mono">${n.images.map(esc).join('<br>') || '–'}</dd>
        <dt>Hosts</dt><dd>${n.hosts.map((h) => `<a data-node="host:${esc(h)}" style="cursor:pointer;color:var(--cyan)">${esc(h)}</a>`).join(', ') || '–'}</dd>
        ${n.schedule ? `<dt>Schedule</dt><dd class="mono">${esc(n.schedule)}</dd>` : ''}
      </dl></div>`;
      if (n.services.length) {
        html += `<div class="d-sec"><h3>Services</h3><dl class="kv">${n.services.map((s) => `<dt>${esc(s.name)}</dt>
          <dd>${esc(s.type)} ${esc(s.ports.join(', '))}${s.lbIPs.length ? ' @ ' + esc(s.lbIPs.join(', ')) : ''}
          <span style="color:var(--faint)">· ${s.readyEndpoints} ready${s.notReadyEndpoints ? `, <span style="color:var(--warn)">${s.notReadyEndpoints} not ready</span>` : ''}</span></dd>`).join('')}</dl></div>`;
      }
    }
    if (n.kind === 'entry') {
      html += `<div class="d-sec"><h3>Hosts</h3><div class="mono">${n.hosts.map(esc).join('<br>') || '*'}</div>
        <div class="d-sub" style="margin-top:4px">${n.tls ? 'TLS terminated at the edge' : 'Plain HTTP'}</div></div>`;
    }
    if (n.kind === 'external' && n.addresses?.length) {
      html += `<div class="d-sec"><h3>Addresses</h3><div class="mono">${n.addresses.slice(0, 12).map(esc).join('<br>')}</div></div>`;
    }
    if (n.kind === 'host' && n.pseudo) {
      html += `<div class="d-sec"><h3>Waiting for a node (${n.pending.length})</h3><ul class="peers">${n.pending.map((p) => `
        <li data-pod="${esc(p.workload)}#${esc(p.pod)}"><div class="p1"><span class="st" style="background:var(--crit)"></span>
        <span class="nm">${esc(p.pod)}</span><span style="color:var(--faint);font-size:11px">${esc(p.namespace)}</span></div>
        <div class="p2">requests ${fmtCpu(p.cpuRequest)} · ${fmtBytes(p.memRequest)}</div>
        <div class="p2 issue critical">${esc(p.message || p.reason)}</div></li>`).join('')}</ul></div>`;
      return html;
    }
    if (n.kind === 'host') {
      const gauge = (ratio, used, color) => ratio == null ? '' : `<div class="gauge" style="--c:${color}">
        <i style="width:${Math.min(100, Math.round(ratio * 100))}%"></i>${used != null ? `<u style="left:${Math.min(100, Math.round(used * 100))}%"></u>` : ''}</div>`;
      const rc = (r) => (r >= 0.9 ? 'var(--crit)' : r >= 0.75 ? 'var(--warn)' : 'var(--teal)');
      const net = n.net || {};
      html += `<div class="d-sec"><h3>Node</h3><dl class="kv"><dt>Ready</dt><dd>${n.ready ? 'yes' : `<span style="color:var(--crit)">${esc(n.readyStatus)}</span>${n.readyMessage ? ' · ' + esc(n.readyMessage) : ''}`}</dd>
        ${n.pressure.length ? `<dt>Conditions</dt><dd style="color:var(--crit)">${esc(n.pressure.join(', '))}</dd>` : ''}
        <dt>Roles</dt><dd>${esc(n.roles.join(', '))}</dd>${n.zone ? `<dt>Zone</dt><dd>${esc(n.zone)}</dd>` : ''}
        ${n.taints && n.taints.length ? `<dt>Taints</dt><dd class="mono">${n.taints.map(esc).join('<br>')}</dd>` : ''}
        <dt>Pods</dt><dd>${n.podCount}${n.unschedulable ? ' · cordoned' : ''}</dd>
        <dt>Kubelet</dt><dd>${esc(n.kubelet)}</dd><dt>OS</dt><dd>${esc(n.os)}</dd></dl></div>
        <div class="d-sec"><h3>Memory · reserved vs used</h3><dl class="kv">
        <dt>Requested</dt><dd>${fmtBytes(n.memRequested)} of ${fmtBytes(n.memAllocatable)} (${fmtPct(n.memRequestRatio)})${gauge(n.memRequestRatio, n.memRatio, rc(n.memRequestRatio || 0))}</dd>
        <dt>In use</dt><dd>${fmtBytes(n.memUsed)} (${fmtPct(n.memRatio)})</dd>
        <dt>Limits total</dt><dd>${fmtBytes(n.memLimits)} (${n.memLimitRatio != null ? n.memLimitRatio.toFixed(2) + '× allocatable' : '–'})${n.podsWithoutMemLimit ? ` · ${n.podsWithoutMemLimit} pod(s) unlimited` : ''}</dd>
        ${n.memSystemReserved ? `<dt>System reserved</dt><dd>${fmtBytes(n.memSystemReserved)} of ${fmtBytes(n.memCapacity)} capacity</dd>` : ''}
        </dl></div>
        <div class="d-sec"><h3>CPU · reserved vs used</h3><dl class="kv">
        <dt>Requested</dt><dd>${fmtCpu(n.cpuRequested)} of ${fmtCpu(n.cpuAllocatable)} (${fmtPct(n.cpuRequestRatio)})${gauge(n.cpuRequestRatio, n.cpuRatio, rc(n.cpuRequestRatio || 0))}</dd>
        <dt>In use</dt><dd>${fmtCpu(n.cpuUsed)} (${fmtPct(n.cpuRatio)})</dd></dl></div>
        ${net.connections ? `<div class="d-sec"><h3>Network (from this node's pods)</h3><dl class="kv">
        <dt>Connections</dt><dd>${net.connections}</dd>
        <dt>Retransmitting</dt><dd>${net.retransConnections} (${fmtPct(net.retransShare)} of cluster)</dd>
        <dt>Slower than peers</dt><dd>${net.slowConnections}</dd>
        <dt>RTT p50 / p95</dt><dd>${fmtMs(net.rttP50Ms)} / ${fmtMs(net.rttP95Ms)} · cluster p50 ${fmtMs(net.clusterRttP50Ms)}</dd></dl></div>`
        : n.agentStale ? '<div class="d-sec"><h3>Network</h3><div class="empty">No agent report from this node.</div></div>' : ''}`;
      const wls = this.graph.nodes.filter((w) => w.kind === 'workload' && w.hosts.includes(n.name));
      html += `<div class="d-sec"><h3>Workloads here</h3><ul class="peers">${wls.map((w) => `<li data-node="${esc(w.id)}"><div class="p1">
        <span class="st" style="background:${LEVEL_COLOR[w.risk.level]}"></span><span class="nm">${esc(w.name)}</span>
        <span style="color:var(--faint);font-size:11px">${esc(w.namespace)}</span></div></li>`).join('')}</ul></div>`;
      return html;
    }
    if (n.kind !== 'clients') html += `<div class="d-sec"><h3>Upstream · who calls it (${inbound.length})</h3>${this._peers(inbound, byId, 'up')}</div>`;
    html += `<div class="d-sec"><h3>Downstream · what it calls (${outbound.length})</h3>${this._peers(outbound, byId, 'down')}</div>`;
    if (n.kind === 'workload' && n.pods.length) {
      html += `<div class="d-sec"><h3>Pods (${n.pods.length})</h3><table class="pods"><tr><th>Pod</th><th>Node</th><th>CPU</th><th>Mem</th><th>↻</th><th>Age</th></tr>
        ${n.pods.map((p) => {
          const lvl = podLevel(p);
          return `<tr data-pod="${esc(n.id)}#${esc(p.name)}" ${p.name === podName ? 'style="background:rgba(79,209,255,0.08)"' : ''}>
          <td class="nm" title="${esc(p.name)}${p.reason ? ' · ' + esc(p.reason) : ''}"><span class="st" style="background:${LEVEL_COLOR[lvl]}"></span>${esc(p.name)}</td>
          <td>${p.node ? esc(p.node) : '<span style="color:var(--crit)">none</span>'}</td><td>${p.cpu != null ? fmtCpu(p.cpu) : '–'}</td><td>${p.mem != null ? fmtBytes(p.mem) : '–'}</td>
          <td>${p.restarts}</td><td>${fmtAge(p.ageSeconds)}</td></tr>
          ${(p.issues || []).length ? `<tr data-pod="${esc(n.id)}#${esc(p.name)}"><td colspan="6" class="issue ${lvl}" style="border-top:0;padding-top:0">${esc(p.issues.join(' · '))}</td></tr>` : ''}`;
        }).join('')}</table></div>`;
    }
    return html;
  }

  _edgeHtml(e, byId) {
    const s = byId.get(e.source), t = byId.get(e.target);
    const L = e.latency, H = e.health, T = e.traffic;
    const typeLabel = { calls: 'Call', route: 'Route', exposes: 'Exposure' }[e.type] || e.type;
    let html = `<div class="d-kind">${typeLabel}</div><div class="d-title">${esc(s?.name)} → ${esc(t?.name)}</div>
      <div class="d-sub">${e.evidence.map((x) => `<span class="ev ${x}">${x}</span>`).join(' ')}</div>`;
    html += this._risk(e.risk);
    html += `<div class="d-sec"><h3>Traffic & latency</h3><dl class="kv">
      ${T.rps != null ? `<dt>Requests</dt><dd>${fmtNum(T.rps)}/s</dd>` : ''}
      ${T.connections != null ? `<dt>Connections</dt><dd>${T.connections}</dd>` : ''}
      ${T.bytesPerSec ? `<dt>Throughput</dt><dd>${fmtBytes(T.bytesPerSec)}/s</dd>` : ''}
      ${T.lastActiveS != null ? `<dt>Last packet</dt><dd>${T.lastActiveS < 2 ? 'just now' : fmtAge(Math.round(T.lastActiveS)) + ' ago'}${T.idle ? ' <span style="color:var(--faint)">· connection open but idle</span>' : ''}</dd>` : ''}
      ${L.p50Ms != null ? `<dt>Request p50</dt><dd>${fmtMs(L.p50Ms)}</dd>` : ''}
      ${L.p95Ms != null ? `<dt>Request p95</dt><dd>${fmtMs(L.p95Ms)}</dd>` : ''}
      ${L.rttMinMs != null ? `<dt title="Kernel minimum RTT: the network path itself">Network path RTT</dt><dd>${fmtMs(L.rttMinMs)}</dd>` : ''}
      ${L.rttP50Ms != null ? `<dt title="Kernel smoothed RTT. Includes the peer's delayed ACKs on quiet connections, so it can read far above the path RTT without anything being wrong.">Smoothed RTT p50 / p95</dt><dd>${fmtMs(L.rttP50Ms)} / ${fmtMs(L.rttP95Ms)} <span style="color:var(--faint)">incl. ACK delay</span></dd>` : ''}
      ${L.connectP50Ms != null ? `<dt>Connect p50</dt><dd>${fmtMs(L.connectP50Ms)}</dd>` : ''}
      ${H.errorRate ? `<dt>5xx rate</dt><dd>${fmtPct(H.errorRate)}</dd>` : ''}
      ${H.retrans ? `<dt>Retransmitting</dt><dd>${H.retrans} segments</dd>` : ''}
      ${H.sendQ ? `<dt>Unacked send queue</dt><dd>${fmtBytes(H.sendQ)}</dd>` : ''}
      ${H.recvQ ? `<dt>Unread at receiver</dt><dd>${fmtBytes(H.recvQ)}</dd>` : ''}
      ${H.pending ? `<dt>Unanswered connects</dt><dd>${H.pending}</dd>` : ''}
      ${!Object.keys(T).filter((k) => k !== 'level').length && L.estimateMs == null ? '<dt>Data</dt><dd>No live traffic observed for this link</dd>' : ''}
    </dl></div>`;
    html += `<div class="d-sec"><h3>Wiring</h3><dl class="kv">
      ${e.via.length ? `<dt>Via service</dt><dd>${esc(e.via.join(', '))}</dd>` : ''}
      ${e.ports.length ? `<dt>Ports</dt><dd>${esc(e.ports.join(', '))}</dd>` : ''}
      </dl>${e.details.length ? `<h3 style="margin-top:10px">Found in</h3><ul class="details">${e.details.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>`;
    html += `<div class="d-sec"><ul class="peers">
      <li data-node="${esc(e.source)}"><div class="p1"><span class="arrow">from</span><span class="nm">${esc(s?.name)}</span></div></li>
      <li data-node="${esc(e.target)}"><div class="p1"><span class="arrow">to</span><span class="nm">${esc(t?.name)}</span></div></li></ul></div>`;
    return html;
  }
}

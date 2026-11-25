// DOM panels around the 3D view: top bar, left rail, tooltip and the detail drawer.
//
// Field names follow the graph format the map engine reads (namespace = VM group, a node of kind
// "workload" = a virtual machine, its "pods" = its slot on the host and its disks). Nothing of that
// reaches the screen.
import { esc, fmtAgo, fmtBits, fmtByteRate, fmtDur, fmtGB, fmtNum, fmtPct, edgeRate, isOffline,
  LEVEL_COLOR, nsColor, optionsHtml, plural } from './format.js';
import { LAYERS } from './scene.js';

const $ = (sel) => document.querySelector(sel);
const KIND_WORD = { Datastore: 'Datastore', 'Port group': 'Port group', Uplink: 'Physical network card', Project: 'Group',
  'Virtual machine': 'Virtual machine', Template: 'Template', 'ESXi host': 'ESXi host',
  external: 'Datastore or network', workload: 'Virtual machine', host: 'Host' };
const kindWord = (k) => KIND_WORD[k] || k || '';

export class UI {
  constructor(ctx) {
    this.ctx = ctx;   // { onNamespaces(list|null), onSelect(sel, focus), onView(v), onOptions(o), onClear(), search(q), ... }
    this.graph = null;
    this.meta = null;
    this.allNs = [];
    this._bindTop();
    this._bindToggles();
    $('#drawerClose').addEventListener('click', () => ctx.onClear());
  }

  // ------------------------------------------------------------------ names
  // The display name of a VM group ("Linux VMs" for linux).
  groupTitle(id) {
    if (!id) return '';
    const hit = (this.allNs || []).find((g) => g.name === id) || (this.graph?.namespaces || []).find((g) => g.name === id);
    if (hit?.title) return hit.title;
    return (this.meta?.groups || []).find((g) => g.id === id)?.title || id;
  }

  catTitle(id) {
    return (this.meta?.categories || []).find((c) => c.id === id)?.title || id || '';
  }

  nodeName(n) {
    if (!n) return '';
    return n.aggregate && n.kind === 'workload' ? this.groupTitle(n.project || n.namespace || n.name) : n.name;
  }

  // ------------------------------------------------------------------ top bar
  _bindTop() {
    document.querySelectorAll('#topbar .seg button').forEach((b) => b.addEventListener('click', () => this.ctx.onView(b.dataset.view)));
    document.querySelectorAll('#topbar .sections button').forEach((b) => b.addEventListener('click', () => this.ctx.onSection(b.dataset.section)));
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
    $('#nsAll').addEventListener('click', () => this.ctx.onNamespaces(null));
    $('#nsNone').addEventListener('click', () => this.ctx.onNamespaces([]));
    const search = $('#search');
    search.addEventListener('input', () => this._renderSearch());
    search.addEventListener('focus', () => this._renderSearch());
    search.addEventListener('keydown', (ev) => {
      const items = [...document.querySelectorAll('#searchResults li[data-i]')];
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
    const map = { tLabels: 'labels', tRotate: 'rotate' };
    for (const [id, key] of Object.entries(map)) {
      $('#' + id).addEventListener('change', (ev) => this.ctx.onOptions({ [key]: ev.target.checked }));
    }
    $('#tFocus').addEventListener('change', (ev) => this.ctx.onFocus(ev.target.checked));
  }

  setView(view) {
    // Two sections: Triage (a dashboard) and the map. The view switcher belongs to the map only.
    const section = view === 'triage' ? view : 'topology';
    document.querySelectorAll('#topbar .sections button').forEach((b) => b.classList.toggle('on', b.dataset.section === section));
    $('#viewSeg').hidden = section !== 'topology';
    if (section === 'topology') document.querySelectorAll('#topbar .seg button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  }

  // Things waiting in the Triage view, shown on its tab whatever view is open.
  setTriageBadge({ count, level }) {
    const el = $('#triageBadge');
    el.hidden = !count;
    el.textContent = count;
    el.className = `badge ${level}`;
    el.title = `${count} thing${count === 1 ? '' : 's'} to look at`;
  }

  setNamespaces(all, selected) {
    this.allNs = all || [];
    this.selectedNs = selected;
    const label = selected == null ? 'All'
      : selected.length === 0 ? 'None' : selected.length <= 2 ? selected.map((id) => this.groupTitle(id)).join(', ') : `${selected.length} selected`;
    $('#nsSummary').textContent = label;
    this._renderNsList();
  }

  _renderNsList() {
    const q = $('#nsFilter').value.trim().toLowerCase();
    const list = (this.allNs || []).filter((n) => !q || (n.title || n.name).toLowerCase().includes(q) || n.name.toLowerCase().includes(q));
    const sel = this.selectedNs;
    $('#nsList').innerHTML = list.map((n) => {
      const checked = sel == null ? true : sel.includes(n.name);
      const dot = n.critical ? LEVEL_COLOR.critical : n.warning ? LEVEL_COLOR.warning : nsColor(n.name);
      const count = n.workloads ?? ((n.online || 0) + (n.offline || 0));
      return `<li><label><input type="checkbox" data-ns="${esc(n.name)}" ${checked ? 'checked' : ''}>
        <span class="swatch" style="background:${dot}"></span><span>${esc(n.title || n.name)}</span>
        <span class="count">${plural(count, 'VM')}${n.offline ? ` · ${n.offline} off` : ''}</span></label></li>`;
    }).join('') || '<li class="empty">No matching groups</li>';
    $('#nsList').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
      const every = (this.allNs || []).map((n) => n.name);
      let next = sel == null ? every.slice() : sel.slice();
      if (cb.checked) next.push(cb.dataset.ns); else next = next.filter((x) => x !== cb.dataset.ns);
      next = [...new Set(next)];
      this.ctx.onNamespaces(every.length && every.every((x) => next.includes(x)) ? null : next);
    }));
  }

  // Search asks the collector (it knows every VM, address and datastore, whatever the map shows);
  // what is on the page answers at once and stands in if the request fails.
  _renderSearch() {
    const q = $('#search').value.trim().toLowerCase();
    const box = $('#searchResults');
    if (!q || !this.graph) { box.hidden = true; return; }
    const local = this._localHits(q);
    if (box.hidden || !this._hitsFor || !q.startsWith(this._hitsFor)) this._showHits(local);
    clearTimeout(this._searchT);
    this._searchT = setTimeout(async () => {
      let hits = null;
      try { hits = (await this.ctx.search(q)).hits; } catch { /* keep what the page found */ }
      if ($('#search').value.trim().toLowerCase() !== q) return;
      if (!hits) { this._showHits(local); return; }
      const seen = new Set(hits.map((h) => h.id));
      const merged = hits.map((h) => ({ name: this._hitName(h), ip: h.ip, ns: h.ns, kind: kindWord(h.kind), sel: { type: 'node', id: h.id } }))
        .concat(local.filter((h) => !seen.has(h.sel.id)));
      this._hitsFor = q;
      this._showHits(merged);
    }, 160);
  }

  _hitName(h) {
    return h.id?.startsWith('proj:') ? this.groupTitle(h.name) : h.name;
  }

  _localHits(q) {
    const hits = [];
    const has = (v) => String(v || '').toLowerCase().includes(q);
    for (const n of this.graph.nodes) {
      const v = n.vm || {};
      const match = has(n.name) || has(this.groupTitle(n.namespace)) || (v.ips || []).some(has) || has(v.hostname) || has(v.os)
        || (v.nics || []).some((x) => has(x.mac)) || (n.addresses || []).some(has);
      if (match) hits.push({ sel: { type: 'node', id: n.id }, name: this.nodeName(n), ip: v.ip || (n.addresses || [])[0] || '', ns: n.namespace, kind: kindWord(n.subkind || n.kind) });
    }
    return hits;
  }

  _showHits(hits) {
    const box = $('#searchResults');
    box.innerHTML = hits.slice(0, 14).map((h, i) => `<li data-i="${i}"><span class="nm">${esc(h.name)}</span>
      ${h.ip ? `<span class="ip">${esc(h.ip)}</span>` : ''}
      <span class="kind">${esc([h.kind, this.groupTitle(h.ns)].filter(Boolean).join(' · '))}</span></li>`).join('')
      || '<li class="empty">Nothing found</li>';
    box.hidden = false;
    box.querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const h = hits[+li.dataset.i];
      box.hidden = true;
      $('#search').value = '';
      this.ctx.onReveal ? this.ctx.onReveal(h.sel) : this.ctx.onSelect(h.sel, true);
    }));
  }

  // What is running: collector version/build, and whether this page's code matches it.
  setVersion(meta) {
    const el = $('#version');
    if (!meta || !meta.version) { el.hidden = true; return; }
    const serverBuild = meta.assetBuild || `${meta.version}-${meta.build}`;
    const staleUi = meta.uiBuild && meta.uiBuild !== serverBuild;
    el.hidden = false;
    el.className = `chip version ${staleUi ? 'warn' : 'subtle'}`;
    el.textContent = `v${meta.version}${staleUi ? ' · page out of date' : ''}`;
    el.title = [`ESXi Galaxy v${meta.version} · build ${meta.build || '-'}`,
      `This page's code: ${meta.uiBuild || 'unversioned'}${staleUi ? ' (reload to update)' : ''}`,
      meta.startedAt ? `started ${meta.startedAt}` : ''].filter(Boolean).join('\n');
  }

  showUpdate(from, to) {
    $('#updateText').textContent = `ESXi Galaxy was updated (v${from.version} build ${from.build} → v${to.version} build ${to.build}).`;
    $('#updateBanner').hidden = false;
    $('#updateReload').onclick = () => location.reload();
  }

  // Where the picture comes from, and whether each source is delivering.
  static sourceChips(s, mode) {
    const chips = [];
    if (mode === 'sample') chips.push({ cls: 'warn', mark: '◆', text: 'sample data', title: 'A made-up host for trying the dashboard out. Nothing here is a real machine.' });
    const v = s?.vsphere;
    if (v) {
      const what = v.product || 'the host';
      if (!v.enabled) chips.push({ cls: 'off', mark: '○', text: 'host · not set up', title: 'No host is configured, so there is nothing to show.' });
      else if (v.error) chips.push({ cls: 'warn', mark: '▲', text: 'host · error', title: `The host could not be read: ${v.error}` });
      else if (!v.hosts) chips.push({ cls: 'off', mark: '◌', text: 'host · waiting', title: 'Connecting to the host.' });
      else chips.push({ cls: 'ok', mark: '●', text: mode === 'sample' ? 'vSphere API' : (v.host || 'vSphere API'),
        title: `Reading ${what} through the vSphere API (read-only): ${fmtNum(v.vms)} VMs${v.ageS != null ? `, read ${fmtAgo(v.ageS)}` : ''}${v.fetchSeconds ? ` in ${v.fetchSeconds} s` : ''}.` });
    }
    const h = s?.history;
    if (h && h.watchedS != null && h.watchedS < 3600) chips.push({ cls: 'off', mark: '◌', text: 'learning usage', title: 'Sizing advice needs an hour of watching how busy each VM gets.' });
    return chips;
  }

  setMeta(meta, graph) {
    this.meta = meta;
    this.setVersion(meta);
    const name = graph?.estate?.name || meta?.estate?.name || '';
    $('#netName').textContent = name;
    $('#netName').hidden = !name;
    const s = graph?.sources || meta?.sources || {};
    $('#sources').innerHTML = UI.sourceChips(s, meta?.mode).map((c) => `<span class="chip ${c.cls}" title="${esc(c.title)}">${c.mark} ${esc(c.text)}</span>`).join('');
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
    this._pageNodes = new Map(graph.nodes.map((n) => [n.id, n]));
    this.renderEstate(graph.estate);

    const v = graph.view;
    document.querySelector('#layers .layers').innerHTML = LAYERS.map((L) => {
      const k = counts[L.key] || {};
      return `<li style="--c:${L.color}"><span class="bar"></span><span class="name">${esc(L.label)}</span>
        <span class="meta">${esc(k.text || '')}${k.critical ? ` · <span class="c">${k.critical} critical</span>` : ''}${k.warning ? ` · <span class="w">${k.warning} warning</span>` : ''}</span></li>`;
    }).join('');

    // When the map is rolled up (problems only, or a very large estate), say what is being shown.
    const scale = $('#scaleBox');
    scale.hidden = !(v && v.collapsed);
    if (v && v.collapsed) {
      const strip = (x) => this.groupTitle(x.replace(/^(grp|ns):/, ''));
      scale.innerHTML = `<h2>${v.focus ? 'Problems only' : 'Rolled up'}</h2>
        ${v.focus ? `<div class="sc-txt">${v.focusTotal ? `<b>${v.focusShown}</b>${v.focusShown < v.focusTotal ? ` worst of <b>${fmtNum(v.focusTotal)}</b>` : ''} VM${v.focusTotal === 1 ? '' : 's'} with something to look at, each with what it is attached to.` : '<b>Nothing needs attention.</b>'}
          The other ${plural(v.focusRest || 0, 'VM')} ${v.focusRest === 1 ? 'is' : 'are'} rolled up into ${v.focusRest === 1 ? 'its group' : 'their groups'}. Double-click a group to open it.</div>`
    : `<div class="sc-txt"><b>${fmtNum(v.workloads)}</b> VMs drawn as <b>${plural(v.projects, 'group')}</b>. Double-click one to open it.</div>`}
        ${(v.expanded || []).length ? `<div class="sc-open">${v.expanded.map((x) => `<button class="chip" data-close="${esc(x)}" title="Close">${esc(strip(x))} ✕</button>`).join('')}
          <button class="link" data-close="*">Close all</button></div>` : ''}
        ${(v.dropped || []).length ? `<div class="sc-txt warn">Closed ${v.dropped.map((x) => esc(strip(x))).join(', ')} to keep the map readable.</div>` : ''}`;
      scale.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => this.ctx.onExpand(b.dataset.close, false)));
    }

    const selId = selection?.id;
    const hot = graph.hotspots || [];
    const isEdge = (h) => h.kind === 'call' || h.type === 'edge' || (String(h.id).includes('->') && !this._pageNodes.has(h.id));
    $('#hotspots').innerHTML = hot.length ? hot.slice(0, 12).map((h, i) => {
      const r = (h.reasons || [])[0];
      const why = typeof r === 'string' ? r : r?.text || '';
      const cat = typeof r === 'object' && r?.category ? this.catTitle(r.category) : '';
      return `<li data-i="${i}" class="${selId === h.id ? 'sel' : ''}">
        <div class="row1"><span class="lvl" style="background:${LEVEL_COLOR[h.level] || LEVEL_COLOR.warning}"></span>
        <span class="nm">${esc(h.name)}</span></div>
        <div class="why">${esc(why)}</div>
        <div class="why ns">${esc([cat, this.groupTitle(h.namespace), isEdge(h) ? 'attachment' : ''].filter(Boolean).join(' · '))}</div></li>`;
    }).join('') : '<li class="empty">Nothing needs attention.</li>';
    $('#hotspots').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const h = hot[+li.dataset.i];
      const sel = isEdge(h) ? { type: 'edge', id: h.id } : { type: 'node', id: h.id };
      this.ctx.onReveal ? this.ctx.onReveal(sel) : this.ctx.onSelect(sel, true);
    }));

    const paths = (graph.paths || []).filter((p) => p.bytesPerSec > 0 || p.rateText).slice(0, 8);
    const top = Math.max(1, ...paths.map((p) => p.bytesPerSec || 0));
    const edgeLvl = new Map(graph.edges.map((e) => [e.id, e.risk?.level || 'ok']));
    $('#paths').innerHTML = paths.length ? paths.map((p, i) => {
      const lvl = (p.edges || []).map((id) => edgeLvl.get(id)).find((l) => l && l !== 'ok');
      const on = selection?.type === 'edge' && (p.edges || []).includes(selection.id);
      const name = p.name || (p.nodes || []).map((id) => this.nodeName(this._pageNodes.get(id)) || id).join(' → ');
      return `<li data-i="${i}" class="${on ? 'sel' : ''}">
        <div class="row1">${lvl ? `<span class="lvl" style="background:${LEVEL_COLOR[lvl]}" title="${lvl}"></span>` : ''}<span class="nm">${esc(name)}</span>
          <span class="score">${esc(p.rateText || fmtByteRate(p.bytesPerSec))}</span></div>
        <div class="meter"><i style="width:${Math.max(2, Math.round(((p.bytesPerSec || 0) / top) * 100))}%"></i></div></li>`;
    }).join('') : '<li class="empty">Nothing is moving data right now.</li>';
    $('#paths').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const p = paths[+li.dataset.i];
      if ((p.edges || []).length === 1) this.ctx.onSelect({ type: 'edge', id: p.edges[0] }, false);
      else this.ctx.onSelect({ type: 'path', idx: +li.dataset.i, nodes: p.nodes || [], edges: p.edges || [] }, false);
    }));
  }

  // The host at a glance.
  renderEstate(e) {
    const box = $('#netBox');
    box.hidden = !e;
    if (!e) { box.innerHTML = ''; return; }
    const cls = (r, warn, crit) => (r == null ? '' : r >= crit ? 'c' : r >= warn ? 'w' : '');
    const tile = (v, k, title = '', c = '') => `<div class="net-tile" title="${esc(title)}"><div class="v ${c}">${v}</div><div class="k">${k}</div></div>`;
    const st = e.storage || {};
    box.innerHTML = `<h2>Host <span class="hint">${esc(e.name || '')}</span></h2>
      <div class="net-grid">
        ${tile(`${fmtNum(e.on ?? 0)}<small> of ${fmtNum(e.vms ?? 0)}</small>`, `VMs running${e.off ? ` · ${fmtNum(e.off)} off` : ''}`, 'Virtual machines powered on, out of every VM registered on the host')}
        ${tile(fmtPct(e.cpuPct), `CPU · ${fmtNum(e.cores ?? 0)} cores`, `The host's processors in use right now. ${fmtNum(e.vcpus ?? 0)} vCPUs are given to running VMs: ${e.vcpuRatio ?? '–'} per core.`, cls(e.cpuPct, 0.75, 0.9))}
        ${tile(fmtPct(e.memPct), `memory · ${esc(fmtGB(e.memBytes))}`, `${fmtGB(e.memUsedBytes)} of ${fmtGB(e.memBytes)} in use`, cls(e.memPct, 0.9, 0.97))}
        ${tile(e.memAllocRatio != null ? `${e.memAllocRatio.toFixed(2)}×` : '–', 'memory promised', `Running VMs are allocated ${fmtGB(e.memAllocated)} against ${fmtGB(e.memBytes)} in the host. Above 1× the host relies on VMs not using all they were given.`, cls(e.memAllocRatio, 1.0, 1.5))}
        ${tile(fmtPct(st.usedRatio), `storage · ${esc(fmtGB(st.capacity))}`, `${fmtGB(st.used)} used, ${fmtGB(st.free)} free across ${plural(st.datastores || 0, 'datastore')}`, cls(st.usedRatio, 0.8, 0.9))}
        ${tile(fmtNum(e.snapshots?.count ?? 0), `snapshot${e.snapshots?.count === 1 ? '' : 's'}${e.snapshots?.bytes ? ` · ${esc(fmtGB(e.snapshots.bytes))}` : ''}`, `On ${plural(e.snapshots?.vms || 0, 'VM')}. Snapshots grow for as long as they are kept.`)}
      </div>
      <div class="net-note">${esc([e.model, e.version ? `ESXi ${e.version}` : '', e.uptimeS != null ? `up ${fmtDur(e.uptimeS)}` : ''].filter(Boolean).join(' · '))}</div>`;
  }

  // ------------------------------------------------------------------ tooltip
  tooltip(hit, at) {
    const tip = $('#tooltip');
    if (!hit || !at || !this.graph) { tip.hidden = true; return; }
    const byId = this._pageNodes || new Map();
    let html = '';
    if (hit.type === 'pod' && hit.pod?.pseudo) {
      const p = hit.pod;
      html = `<div class="t1">${esc(p.name)}</div><div class="t2">in ${esc(this.nodeName(byId.get(hit.wid)) || 'a group')}</div>
        ${(p.issues || []).map((x) => `<div class="t2 issue ${p.level}">${esc(x)}</div>`).join('')}<div class="t2">click for details · double-click to open the group</div>`;
    } else if (hit.type === 'pod') {
      const w = byId.get(hit.wid), on = byId.get(hit.pod?.hostId);
      html = hit.pod?.part === 'disk'
        ? `<div class="t1">${esc(hit.pod.name)}</div><div class="t2">a disk of ${esc(w?.name || 'a VM')}${on ? ` · on ${esc(on.name)}` : ''}</div><div class="t2">${esc(hit.pod.summary || '')}</div>`
        : `<div class="t1">${esc(w?.name || 'A VM')} · running</div><div class="t2">${esc(hit.pod?.summary || '')}${on ? ` · on ${esc(on.name)}` : ''}</div>`;
    } else if (hit.type === 'edge') {
      const e = this.graph.edges.find((x) => x.id === hit.id);
      if (!e) { tip.hidden = true; return; }
      const s = byId.get(e.source), t = byId.get(e.target);
      const what = { attached: 'network adapter', stores: 'disks', uplink: 'uplink' }[e.type] || 'attachment';
      html = `<div class="t1">${esc(this.nodeName(s))} → ${esc(this.nodeName(t))}</div>
        <div class="t2">${esc(what)} · ${esc(edgeRate(e) || ((e.evidence || []).includes('observed') ? 'quiet' : 'not active'))}</div>
        ${(e.details || []).slice(0, 3).map((x) => `<div class="t2">${esc(x)}</div>`).join('')}`;
    } else {
      const n = byId.get(hit.id);
      if (!n) { tip.hidden = true; return; }
      html = `<div class="t1">${esc(this.nodeName(n))}</div>`;
      if (n.aggregate && n.kind === 'workload') {
        const k = n.counts || {};
        html += `<div class="t2">Group · ${plural(k.workloads || 0, 'VM')}${k.failing ? ` · ${k.failing} need${k.failing === 1 ? 's' : ''} attention` : ''}</div><div class="t2">double-click to open</div>`;
      } else if (n.kind === 'workload') {
        const v = n.vm || {};
        html += `<div class="t2">${esc(v.os || n.subkind)} · ${esc(this.groupTitle(n.namespace))}</div>`;
        html += !v.on ? `<div class="t2">powered off · holds ${esc(fmtGB(v.committed))}</div>`
          : `<div class="t2">${fmtPct(v.cpuPct)} of ${plural(v.cpus, 'vCPU')} · ${esc(fmtGB(v.memActiveMB * 1048576))} active of ${esc(fmtGB(v.memMB * 1048576))}</div>
             <div class="t2">${esc(v.ip || 'no address reported')} · ↓ ${esc(fmtBits(v.netRxBps))} · ↑ ${esc(fmtBits(v.netTxBps))}</div>`;
      } else if (n.ds) {
        html += `<div class="t2">Datastore · ${esc(n.ds.type || '')}</div><div class="t2">${fmtPct(n.ds.usedRatio)} used · ${esc(fmtGB(n.ds.free))} free of ${esc(fmtGB(n.ds.capacity))}</div>`;
      } else if (n.pg) {
        html += `<div class="t2">Port group · ${esc(n.pg.vswitch)} · ${esc(n.pg.vlanText)}</div><div class="t2">${plural(n.pg.vms || 0, 'VM')} · ${esc(fmtByteRate(n.pg.bytesPerSec))}</div>`;
      } else if (n.nic) {
        html += `<div class="t2">Physical network card · ${esc(n.nic.vswitch)}</div><div class="t2">${esc(n.nic.speedText)} · ${esc(fmtByteRate(n.nic.bytesPerSec))}</div>`;
      } else if (n.kind === 'host') {
        const e = n.esx || {};
        html += `<div class="t2">ESXi ${esc(e.version || '')} · ${plural(e.vmsOn || 0, 'VM')} running</div>
          <div class="t2">CPU ${fmtPct(e.cpuPct)} · memory ${fmtPct(e.memPct)} of ${esc(fmtGB(e.memBytes))}</div>`;
      } else {
        html += `<div class="t2">${esc(kindWord(n.subkind || n.kind))}</div>`;
      }
      const f = (this.graph.findings || []).find((x) => x.subject === n.id && x.severity !== 'info' && !x.effectOf);
      const r = n.risk || {};
      if (f) html += `<div class="t2" style="color:${LEVEL_COLOR[f.severity]}">${esc(f.title)}</div>`;
      else if (r.reasons && r.reasons.length && r.level !== 'ok') html += `<div class="t2" style="color:${LEVEL_COLOR[r.level]}">${esc(r.reasons[0].text || r.reasons[0])}</div>`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, at.x + 16) + 'px';
    tip.style.top = Math.min(window.innerHeight - h - 8, at.y + 14) + 'px';
  }

  // ------------------------------------------------------------------ drawer
  renderDrawer(sel, refresh = false, detail = null) {
    const d = $('#drawer');
    const scroll = refresh ? d.scrollTop : 0;
    // When the map is rolled up the page holds groups; the server supplies the real VM, its
    // attachments and neighbours. Render from that, then put the page's graph back.
    const pageGraph = this.graph;
    if (detail && this.graph) this.graph = { ...this.graph, nodes: detail.nodes || [], edges: detail.edges || [], findings: detail.findings || [] };
    try { this._renderDrawer(sel, d, scroll, detail); } finally { this.graph = pageGraph; }
  }

  _renderDrawer(sel, d, scroll, detail) {
    if (!sel || !this.graph || sel.type === 'path') { d.hidden = true; return; }
    const byId = new Map(this.graph.nodes.map((n) => [n.id, n]));
    let html = '';
    if (sel.type === 'edge') {
      const e = this.graph.edges.find((x) => x.id === sel.id);
      if (!e) { d.hidden = true; return; }
      html = this._edgeHtml(e, byId);
    } else {
      const id = sel.type === 'pod' ? sel.wid || sel.key.split('#')[0] : sel.id;
      const n = byId.get(id);
      if (!n) { d.hidden = true; return; }
      html = this._nodeHtml(n, byId);
    }
    if (detail) {
      const on = detail.at && this.nodeName(this._pageNodes?.get(detail.at));
      html = `<div class="d-fold">${on ? `Shown on the map inside <b>${esc(on)}</b>.` : 'Not drawn on the map right now.'}
        <a class="link" data-reveal="1">Open it on the map ▸</a></div>` + html;
    }
    $('#drawerBody').innerHTML = html;
    d.querySelectorAll('[data-reveal]').forEach((el) => el.addEventListener('click', () => this.ctx.onReveal(sel)));
    d.querySelectorAll('[data-expand]').forEach((el) => el.addEventListener('click', () => this.ctx.onExpand(el.dataset.expand, el.dataset.close == null)));
    d.querySelectorAll('[data-scope]').forEach((el) => el.addEventListener('click', () => this.ctx.onNamespaces([el.dataset.scope])));
    d.hidden = false;
    d.scrollTop = scroll;
    d.querySelectorAll('[data-copy]').forEach((el) => el.addEventListener('click', () => {
      this.ctx.copy(el.dataset.copy); el.textContent = 'copied'; setTimeout(() => { el.textContent = 'copy'; }, 1200);
    }));
    d.querySelectorAll('[data-node]').forEach((el) => el.addEventListener('click', () => this.ctx.onSelect({ type: 'node', id: el.dataset.node }, true)));
    d.querySelectorAll('[data-edge]').forEach((el) => el.addEventListener('click', () => this.ctx.onSelect({ type: 'edge', id: el.dataset.edge }, false)));
  }

  _badge(cls, text) {
    return `<div class="risk-badge ${cls}">${esc(text)}</div>`;
  }

  // The findings about one or more subjects: what, why, and what to do.
  _diagnosis(ids) {
    const want = new Set([].concat(ids));
    const fs = (this.graph.findings || []).filter((f) => want.has(f.subject));
    if (!fs.length) return '';
    const real = fs.filter((f) => f.severity !== 'info');
    const info = fs.filter((f) => f.severity === 'info');
    const card = (f) => `<div class="d-find ${f.severity}"><div class="cat">${esc(this.catTitle(f.category))}</div>
      <div class="t">${esc(f.title)}</div>
      ${f.detail ? `<p>${esc(f.detail)}</p>` : ''}${f.cause ? `<p><b>Why</b>${esc(f.cause)}</p>` : ''}
      ${f.options?.length ? optionsHtml(f) : ''}${f.fix && !f.options?.length ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : f.fix ? `<p class="fix"><b>In short</b>${esc(f.fix)}</p>` : ''}
      ${(f.evidence || []).filter((e) => !/:\s*$/.test(e)).map((e) => `<pre class="ev-msg">${esc(e)}</pre>`).join('')}</div>`;
    return (real.length ? `<div class="d-sec"><h3>What needs attention</h3>${real.map(card).join('')}</div>` : '')
      + (info.length ? `<div class="d-sec"><h3>Worth knowing</h3>${info.map(card).join('')}</div>` : '');
  }

  _meter(ratio, text, warn = 0.8, crit = 0.9) {
    const r = Math.max(0, Math.min(1, ratio || 0));
    const c = r >= crit ? 'var(--crit)' : r >= warn ? 'var(--warn)' : 'var(--cyan)';
    return `<div class="use"><div class="meter"><i style="width:${Math.max(1, Math.round(r * 100))}%;background:${c}"></i></div><span>${text}</span></div>`;
  }

  _vmsOn(n) {
    // Every VM attached to this datastore, port group or host.
    const ids = new Set(this.graph.edges.filter((e) => e.target === n.id).map((e) => e.source));
    return (this.graph?.nodes || []).filter((w) => w.kind === 'workload' && !w.aggregate
      && (ids.has(w.id) || (n.kind === 'host' && ((w.pods || []).some((p) => p.hostId === n.id) || !w.vm?.on))));
  }

  _vmList(list, right) {
    if (!list.length) return '<div class="empty">None in view.</div>';
    const sorted = [...list].sort((a, b) => (isOffline(a) ? 1 : 0) - (isOffline(b) ? 1 : 0) || (b.vm?.cpuMhz || 0) - (a.vm?.cpuMhz || 0) || a.name.localeCompare(b.name));
    return `<ul class="peers">${sorted.map((w) => {
      const off = isOffline(w), v = w.vm || {};
      return `<li data-node="${esc(w.id)}"><div class="p1">
        <span class="st" style="background:${off ? LEVEL_COLOR.offline : LEVEL_COLOR[w.risk?.level || 'ok']}"></span><span class="nm">${esc(w.name)}</span>
        <span class="where">${off ? 'off' : esc(v.ip || '')}</span>
        <span class="rate">${esc(right ? right(w) : off ? fmtGB(v.committed) : `${fmtPct(v.cpuPct)} CPU`)}</span></div>
        <div class="p2">${esc(off ? `${plural(v.cpus, 'vCPU')} · ${fmtGB(v.memMB * 1048576)} if started` : `${plural(v.cpus, 'vCPU')} · ${fmtGB(v.memMB * 1048576)} · ${fmtGB(v.memActiveMB * 1048576)} active`)}</div></li>`;
    }).join('')}</ul>`;
  }

  _nodeHtml(n, byId) {
    if (n.aggregate && n.kind === 'workload') return this._aggregateHtml(n);
    if (n.kind === 'workload') return this._vmHtml(n, byId);
    if (n.kind === 'host') return this._hostHtml(n);
    if (n.ds) return this._datastoreHtml(n);
    if (n.pg) return this._portGroupHtml(n, byId);
    if (n.nic) return this._uplinkHtml(n, byId);
    return `<div class="d-kind">${esc(kindWord(n.subkind || n.kind))}</div><div class="d-title">${esc(n.name)}</div>${this._diagnosis(n.id)}`;
  }

  // ---- a virtual machine
  _vmHtml(n, byId) {
    const v = n.vm || {};
    const off = !v.on;
    const lvl = n.risk?.level || 'ok';
    const MB = 1048576;
    const host = (n.pods || []).map((p) => byId.get(p.hostId)).find((h) => h?.kind === 'host');
    const link = (name, prefix) => {
      const hit = [...byId.values()].find((x) => x.name === name && x.id.startsWith(prefix));
      return hit ? `<a class="in" data-node="${esc(hit.id)}">${esc(name)}</a>` : esc(name || '–');
    };
    let html = `<div class="d-kind">${esc(n.subkind || 'Virtual machine')}</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc([this.groupTitle(n.namespace), v.os].filter(Boolean).join(' · '))}</div>`;
    html += off ? this._badge('offline', `Powered off · holds ${fmtGB(v.committed)}`)
      : this._badge(lvl, lvl === 'ok' ? 'Running' : lvl === 'warning' ? 'Running · worth a look' : 'Running · needs attention');
    html += this._diagnosis(n.id);

    if (!off) {
      const ready = v.readyPct;
      html += `<div class="d-sec"><h3>Right now</h3><dl class="kv">
        <dt>CPU</dt><dd>${this._meter(v.cpuPct, `${fmtPct(v.cpuPct)} of ${plural(v.cpus, 'vCPU')} <span class="dim">· ${fmtNum(v.cpuMhz)} MHz</span>`, 0.85, 0.95)}</dd>
        <dt title="Time its vCPUs were ready to run but no physical core was free">Waiting for a core</dt><dd>${ready == null ? '–' : `<span class="${ready >= 0.1 ? 'c' : ready >= 0.05 ? 'w' : ''}">${(ready * 100).toFixed(1)}%</span> <span class="dim">· per vCPU</span>`}</dd>
        <dt title="Memory the guest has touched recently, as the host estimates it">Memory in active use</dt><dd>${this._meter(v.memActivePct, `${esc(fmtGB(v.memActiveMB * MB))} of ${esc(fmtGB(v.memMB * MB))}`, 0.9, 0.97)}</dd>
        <dt title="Physical memory the host has set aside for this VM">Held by the host</dt><dd>${esc(fmtGB(v.memHostMB * MB))}</dd>
        ${v.balloonMB || v.swappedMB ? `<dt>Taken back</dt><dd><span class="w">${esc(fmtGB(v.balloonMB * MB))} ballooned · ${esc(fmtGB(v.swappedMB * MB))} swapped</span></dd>` : ''}
        <dt>Network</dt><dd>↓ ${esc(fmtBits(v.netRxBps))} · ↑ ${esc(fmtBits(v.netTxBps))}</dd>
        <dt>Disk</dt><dd>read ${esc(fmtByteRate(v.diskReadBps, true))} · write ${esc(fmtByteRate(v.diskWriteBps, true))}${v.diskLatencyMs != null ? ` <span class="dim">· up to ${fmtNum(v.diskLatencyMs)} ms</span>` : ''}</dd>
      </dl></div>`;
      if (v.watchedS >= 3600 && v.peakCpuPct != null) {
        html += `<div class="d-sec"><h3>Busiest it has been</h3><dl class="kv">
          <dt>CPU</dt><dd>${fmtPct(v.peakCpuPct)} of its vCPUs</dd>
          <dt>Memory</dt><dd>${esc(fmtGB(v.peakMemActiveMB * MB))} in active use</dd></dl>
          <div class="d-sub">Over the ${esc(fmtDur(Math.min(v.watchedS, 14 * 86400)))} this page has been watching it. Sizing advice is based on these.</div></div>`;
      }
    }

    html += `<div class="d-sec"><h3>Virtual machine</h3><dl class="kv">
      <dt>Guest OS</dt><dd>${esc(v.os || 'not reported')}</dd>
      ${off ? '' : `<dt>Address</dt><dd class="mono">${(v.ips || []).length ? v.ips.map(esc).join('<br>') : '<span class="dim">not reported</span>'}</dd>`}
      ${v.hostname ? `<dt>Host name</dt><dd class="mono">${esc(v.hostname)}</dd>` : ''}
      <dt>Size</dt><dd>${plural(v.cpus, 'vCPU')} · ${esc(fmtGB(v.memMB * MB))} memory</dd>
      ${off ? '' : `<dt>Running for</dt><dd>${esc(fmtDur(v.uptimeS))}</dd>`}
      <dt>VMware Tools</dt><dd>${esc(v.toolsLabel || 'unknown')}${off ? '' : v.toolsRunning ? ' <span class="dim">· running</span>' : ' <span class="w">· not running</span>'}</dd>
      ${host ? `<dt>Runs on</dt><dd><a class="in" data-node="${esc(host.id)}">${esc(host.name)}</a></dd>` : ''}
      <dt>Hardware version</dt><dd>${esc(v.hwVersion || '–')}${v.firmware ? ` <span class="dim">· ${esc(v.firmware.toUpperCase())}</span>` : ''}</dd>
      ${v.annotation ? `<dt>Notes</dt><dd>${esc(v.annotation)}</dd>` : ''}
    </dl></div>`;

    const disks = v.disks || [];
    html += `<div class="d-sec"><h3>Disks (${disks.length})</h3><ul class="details">${disks.map((x) => `<li><b>${esc(x.label)}</b> · ${esc(fmtGB(x.capacity))} ·
      ${x.thin ? 'thin' : 'thick'} · on ${link(x.datastore, 'ext:ds:')}</li>`).join('') || '<li>None</li>'}</ul>
      <dl class="kv" style="margin-top:8px"><dt>Space it holds</dt><dd>${esc(fmtGB(v.committed))} <span class="dim">· of ${esc(fmtGB(v.provisioned))} given${v.snapshotBytes ? `, plus snapshots` : ''}</span></dd></dl>
      ${(v.guestDisks || []).length ? `<div class="d-sub" style="margin-top:8px">Inside the guest</div>${v.guestDisks.map((g) => {
        const used = g.capacity ? 1 - g.free / g.capacity : 0;
        return `<div class="fs"><span class="mono">${esc(g.path)}</span>${this._meter(used, `${fmtPct(used)} · ${esc(fmtGB(g.free))} free of ${esc(fmtGB(g.capacity))}`, 0.9, 0.95)}</div>`;
      }).join('')}` : ''}</div>`;

    const nics = v.nics || [];
    html += `<div class="d-sec"><h3>Network adapters (${nics.length})</h3><ul class="details">${nics.map((x) => `<li><b>${esc(x.label)}</b> · ${link(x.network, 'ext:net:')}
      · <span class="mono">${esc(x.mac)}</span> · ${esc(x.type)}${!off && !x.connected ? ' · <span class="w">not connected</span>' : ''}</li>`).join('') || '<li>None</li>'}</ul></div>`;

    const snaps = v.snapshots || [];
    if (snaps.length) {
      html += `<div class="d-sec"><h3>Snapshots (${snaps.length})</h3><ul class="details">${snaps.map((s) => `<li>${'↳ '.repeat(Math.min(3, s.depth))}<b>${esc(s.name)}</b>
        ${s.ageDays != null ? ` · taken ${esc(fmtDur(s.ageDays * 86400))} ago` : ''}${s.memory ? ' · with memory' : ''}</li>`).join('')}</ul>
        <div class="d-sub">Together they hold about ${esc(fmtGB(v.snapshotBytes))} on top of the disks.</div></div>`;
    }
    const cds = (v.cdroms || []).filter((c) => c.iso);
    if (cds.length) html += `<div class="d-sec"><h3>Media attached</h3><ul class="details">${cds.map((c) => `<li>${esc(c.label)}: <span class="mono">${esc(c.iso)}</span>${c.connected ? '' : ' <span class="dim">· not connected now</span>'}</li>`).join('')}</ul></div>`;
    return html;
  }

  // ---- a datastore
  _datastoreHtml(n) {
    const d = n.ds || {};
    const lvl = n.risk?.level || 'ok';
    const vms = this._vmsOn(n);
    let html = `<div class="d-kind">Datastore</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc([d.type, plural(d.vms ?? vms.length, 'VM')].filter(Boolean).join(' · '))}</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>Space</h3>${this._meter(d.usedRatio, `${fmtPct(d.usedRatio)} used`, 0.8, 0.9)}<dl class="kv" style="margin-top:8px">
      <dt>Capacity</dt><dd>${esc(fmtGB(d.capacity))}</dd>
      <dt>Used</dt><dd>${esc(fmtGB(d.used))}</dd>
      <dt>Free</dt><dd>${esc(fmtGB(d.free))}</dd>
      <dt title="What is used now plus what thin disks may still grow to">Promised</dt><dd>${esc(fmtGB(d.provisioned))} <span class="${d.provisionedRatio > 1 ? 'w' : 'dim'}">· ${fmtPct(d.provisionedRatio)} of capacity</span></dd>
      ${d.snapshotBytes ? `<dt>Held by snapshots</dt><dd>${esc(fmtGB(d.snapshotBytes))}</dd>` : ''}
      ${d.offBytes ? `<dt>Held by powered-off VMs</dt><dd>${esc(fmtGB(d.offBytes))}</dd>` : ''}
      <dt>Reads and writes now</dt><dd>${esc(fmtByteRate(d.bytesPerSec, true))}</dd>
    </dl></div>`;
    html += `<div class="d-sec"><h3>VMs with disks on it (${vms.length})</h3>${this._vmList(vms, (w) => fmtGB(w.vm?.committed))}</div>`;
    return html;
  }

  // ---- a port group
  _portGroupHtml(n, byId) {
    const g = n.pg || {};
    const lvl = n.risk?.level || 'ok';
    const vms = this._vmsOn(n);
    const sec = g.security || {};
    const flag = (on, name) => `${name}: ${on ? '<span class="w">accept</span>' : 'reject'}`;
    let html = `<div class="d-kind">Port group</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">A virtual network on ${esc(g.vswitch || 'a virtual switch')} that VMs plug into.</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>Network</h3><dl class="kv">
      <dt>Virtual switch</dt><dd>${esc(g.vswitch || '–')}</dd>
      <dt>VLAN</dt><dd>${esc(g.vlanText || '–')}</dd>
      <dt>Leaves the host by</dt><dd>${(g.uplinks || []).map((u) => { const x = byId.get(`ext:nic:${u.toLowerCase()}`); return x ? `<a class="in" data-node="${esc(x.id)}">${esc(u)}</a>` : esc(u); }).join(', ') || '<span class="w">no uplink: VMs on it reach only each other</span>'}</dd>
      <dt>Traffic now</dt><dd>${esc(fmtByteRate(g.bytesPerSec))}</dd>
      ${g.vmk ? `<dt>Host's own address</dt><dd><span class="mono">${esc(g.vmkIp || '–')}</span> <span class="dim">· ${esc(g.vmk)}</span></dd>` : ''}
      <dt>Security</dt><dd>${flag(sec.promiscuous, 'promiscuous mode')}<br>${flag(sec.macChanges, 'MAC address changes')}<br>${flag(sec.forgedTransmits, 'forged transmits')}</dd>
    </dl></div>`;
    html += `<div class="d-sec"><h3>VMs on it (${vms.length})</h3>${this._vmList(vms, (w) => (w.vm?.on ? fmtBits((w.vm.netRxBps || 0) + (w.vm.netTxBps || 0)) : 'off'))}</div>`;
    return html;
  }

  // ---- a physical network card carrying a virtual switch
  _uplinkHtml(n, byId) {
    const c = n.nic || {};
    const lvl = n.risk?.level || 'ok';
    const groups = this.graph.edges.filter((e) => e.target === n.id).map((e) => byId.get(e.source)).filter(Boolean);
    let html = `<div class="d-kind">Physical network card</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">Carries ${esc(c.vswitch || 'a virtual switch')} out of the host.</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>Link</h3><dl class="kv">
      <dt>Speed</dt><dd>${c.linkUp ? esc(c.speedText) : '<span class="c">no link</span>'}${c.linkUp ? ` <span class="dim">· ${c.duplex ? 'full' : 'half'} duplex</span>` : ''}</dd>
      <dt>Traffic now</dt><dd>${esc(fmtByteRate(c.bytesPerSec))}</dd>
      <dt>Hardware address</dt><dd class="mono">${esc(c.mac || '–')}</dd>
      <dt>Driver</dt><dd>${esc(c.driver || '–')}</dd>
    </dl></div>`;
    html += `<div class="d-sec"><h3>Networks using it (${groups.length})</h3><ul class="peers">${groups.map((g) => `<li data-node="${esc(g.id)}"><div class="p1">
      <span class="st" style="background:${LEVEL_COLOR[g.risk?.level || 'ok']}"></span><span class="nm">${esc(g.name)}</span>
      <span class="where">${esc(g.pg?.vlanText || '')}</span><span class="rate">${plural(g.pg?.vms || 0, 'VM')}</span></div></li>`).join('')}</ul></div>`;
    return html;
  }

  // ---- the ESXi host
  _hostHtml(n) {
    const e = n.esx || {};
    const lvl = n.risk?.level || 'ok';
    const vms = this._vmsOn(n);
    const used = new Set((e.vswitches || []).flatMap((s) => s.uplinks || []));
    let html = `<div class="d-kind">ESXi host</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc([e.model && e.vendor ? `${e.vendor} ${e.model}` : e.model || '', e.cpuModel].filter(Boolean).join(' · '))}</div>`;
    html += this._badge(lvl, lvl === 'ok' ? 'Healthy' : lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>Capacity</h3><dl class="kv">
      <dt>CPU</dt><dd>${this._meter(e.cpuPct, `${fmtPct(e.cpuPct)} of ${fmtNum(e.cores)} cores <span class="dim">· ${fmtNum(e.threads)} threads</span>`, 0.75, 0.9)}</dd>
      <dt>vCPUs given out</dt><dd>${fmtNum(e.vcpus)} <span class="dim">· ${e.vcpuRatio ?? '–'} per core</span></dd>
      <dt>Memory</dt><dd>${this._meter(e.memPct, `${esc(fmtGB(e.memUsedBytes))} of ${esc(fmtGB(e.memBytes))}`, 0.9, 0.97)}</dd>
      <dt>Memory given out</dt><dd>${esc(fmtGB(e.memAllocated))} to running VMs <span class="${e.memAllocRatio > 1 ? 'w' : 'dim'}">· ${e.memAllocRatio != null ? e.memAllocRatio.toFixed(2) : '–'}× what the host has</span></dd>
      <dt>If everything ran</dt><dd>${esc(fmtGB(e.memAllocatedAll))} <span class="dim">· counting powered-off VMs</span></dd>
      <dt>Network</dt><dd>↓ ${esc(fmtBits(e.netRxBps))} · ↑ ${esc(fmtBits(e.netTxBps))}</dd>
      <dt>Disk</dt><dd>read ${esc(fmtByteRate(e.diskReadBps, true))} · write ${esc(fmtByteRate(e.diskWriteBps, true))}</dd>
    </dl></div>`;
    const svc = (e.services || []).filter((s) => ['TSM-SSH', 'TSM', 'ntpd'].includes(s.key));
    const skew = e.clockSkewS;
    html += `<div class="d-sec"><h3>Host</h3><dl class="kv">
      <dt>Version</dt><dd>${esc(e.product?.fullName || `ESXi ${e.version || ''}`)}</dd>
      <dt>Licence</dt><dd>${esc(e.licence || '–')}</dd>
      <dt>Up for</dt><dd>${esc(fmtDur(e.uptimeS))}</dd>
      <dt>Name it gives itself</dt><dd class="mono">${esc(e.hostName || '–')}</dd>
      <dt>Clock</dt><dd>${skew == null ? '–' : Math.abs(skew) < 60 ? 'in step' : `<span class="w">${Math.round(Math.abs(skew) / 60)} min ${skew > 0 ? 'fast' : 'slow'}</span>`}
        <span class="dim">· NTP ${e.ntp?.running ? 'running' : 'off'}${(e.ntp?.servers || []).length ? ` (${esc(e.ntp.servers.join(', '))})` : ''}</span></dd>
      ${svc.map((s) => `<dt>${esc({ 'TSM-SSH': 'SSH', TSM: 'ESXi Shell', ntpd: 'NTP service' }[s.key])}</dt><dd>${s.running ? (s.key === 'ntpd' ? 'running' : '<span class="w">running</span>') : 'stopped'} <span class="dim">· ${s.policy === 'on' ? 'starts with the host' : 'started by hand'}</span></dd>`).join('')}
      <dt>Lockdown mode</dt><dd>${e.lockdown && e.lockdown !== 'lockdownDisabled' ? 'on' : 'off'}</dd>
    </dl></div>`;
    html += `<div class="d-sec"><h3>Virtual switches</h3><ul class="details">${(e.vswitches || []).map((s) => `<li><b>${esc(s.name)}</b> · uplinks: ${esc((s.uplinks || []).join(', ') || 'none')}
      · networks: ${esc((s.portgroups || []).join(', ') || 'none')}</li>`).join('')}</ul></div>`;
    html += `<div class="d-sec"><h3>Physical network cards</h3><ul class="details">${(e.pnics || []).map((p) => `<li><b>${esc(p.device)}</b> ·
      ${p.linkUp ? esc(p.speedMb >= 1000 ? `${p.speedMb / 1000} Gbps` : `${p.speedMb} Mbps`) : '<span class="dim">no link</span>'}
      ${used.has(p.device) ? '' : ' · <span class="dim">not used by any virtual switch</span>'} <span class="dim mono">· ${esc(p.mac)}</span></li>`).join('')}</ul></div>`;
    const open = e.firewall?.open || [];
    if (open.length) {
      html += `<div class="d-sec"><h3>Open in the host's firewall (${open.length})</h3><table class="mini">${open.slice(0, 24).map((r) => `<tr><td>${esc(r.label)}</td>
        <td class="mono">${esc((r.ports || []).join(', '))}</td><td class="num">${r.allIp ? 'any address' : esc((r.allowed || []).join(', ') || 'limited')}</td></tr>`).join('')}</table>
        <div class="d-sub">${fmtNum(e.firewall.enabled)} of ${fmtNum(e.firewall.total)} rule sets are switched on; these accept incoming connections.</div></div>`;
    }
    html += `<div class="d-sec"><h3>VMs on it (${vms.length})</h3>${this._vmList(vms)}</div>`;
    return html;
  }

  // ---- a rolled-up group: what it holds, what needs attention inside, and how to open it
  _aggregateHtml(n) {
    const k = n.counts || {};
    const title = this.nodeName(n);
    const info = (this.allNs || []).find((g) => g.name === (n.project || n.namespace));
    const fs = (this.graph.findings || []).filter((f) => f.at === n.id && f.severity !== 'info' && f.root && !f.effectOf).slice(0, 6);
    const bar = (x, c) => (x ? `<i style="flex:${x};background:${c}"></i>` : '');
    const children = n.children || [];
    return `<div class="d-kind">Group</div><div class="d-title">${esc(title)}</div>
      <div class="d-sub">${plural(k.workloads || 0, 'VM')}${info ? ` · ${fmtNum(info.online ?? 0)} running${info.offline ? `, ${fmtNum(info.offline)} off` : ''}` : ''}</div>
      ${this._badge(n.risk?.level || 'ok', k.failing ? `${k.failing} need${k.failing === 1 ? 's' : ''} attention` : 'Nothing wrong')}
      <div class="d-sec"><div class="agg-bar">${bar(k.healthy, 'var(--ok)')}${bar(k.degraded, 'var(--warn)')}${bar(k.down, 'var(--crit)')}</div>
        ${info ? `<div class="d-sub">${plural(info.vcpus || 0, 'vCPU')} · ${esc(fmtGB((info.memMB || 0) * 1048576))} of memory given to its running VMs</div>` : ''}
        <div style="margin-top:9px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn small" data-expand="${esc(n.expand)}">Open on the map ▸</button>
          <button class="btn small" data-scope="${esc(n.project || n.namespace)}">Show only this group</button></div>
        <div class="d-sub" style="margin-top:6px">Double-clicking it on the map opens it too.</div></div>
      ${fs.length ? `<div class="d-sec"><h3>Needs attention inside</h3>${fs.map((f) => `<div class="d-find ${f.severity}" data-node="${esc(f.subject)}" style="cursor:pointer">
        <div class="cat">${esc(this.catTitle(f.category))}</div><div class="t">${esc(f.title)}</div>
        ${f.fix ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : ''}</div>`).join('')}</div>` : ''}
      <div class="d-sec"><h3>VMs (${n.childrenTotal ?? children.length})</h3>
        <ul class="peers">${children.map((c) => `<li ${c.expand ? `data-expand="${esc(c.expand)}"` : `data-node="${esc(c.id)}"`}><div class="p1">
          <span class="st" style="background:${LEVEL_COLOR[c.level] || LEVEL_COLOR.ok}"></span><span class="nm">${esc(c.name)}</span>
          <span class="where">${esc(c.kind || '')}</span></div>
          ${c.issue ? `<div class="p2 issue ${c.level}">${esc(c.issue)}</div>` : ''}</li>`).join('')}</ul>
        ${(n.childrenTotal || 0) > children.length ? `<div class="d-sub" style="margin-top:6px">+${n.childrenTotal - children.length} more: open the group, or use the VMs table in Triage.</div>` : ''}</div>`;
  }

  // ---- an attachment: a VM's adapters on a port group, its disks on a datastore, or an uplink
  _edgeHtml(e, byId) {
    const s = byId.get(e.source), t = byId.get(e.target);
    const active = (e.evidence || []).includes('observed');
    const lvl = e.risk?.level || 'ok';
    const kind = { attached: 'Network adapter', stores: 'Disks on a datastore', uplink: 'Uplink' }[e.type] || 'Attachment';
    const sub = e.type === 'attached' ? (active ? 'Plugged into this port group.' : 'Set up, but not connected right now.')
      : e.type === 'stores' ? (active ? 'This VM\'s disks live on this datastore.' : 'The VM is off; its disks still take the space.')
        : (active ? 'This network leaves the host through this card.' : 'The card has no link.');
    let html = `<div class="d-kind">${esc(kind)}</div>
      <div class="d-title">${esc(this.nodeName(s) || e.source)} → ${esc(this.nodeName(t) || e.target)}</div><div class="d-sub">${esc(sub)}</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(e.id);
    html += `<div class="d-sec"><h3>Right now</h3><dl class="kv">
      <dt>${e.type === 'stores' ? 'Reads and writes' : 'Traffic'}</dt><dd>${esc(edgeRate(e) || (active ? 'quiet' : 'nothing'))}</dd></dl>
      ${(e.details || []).length ? `<ul class="details" style="margin-top:8px">${e.details.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>`;
    const end = (id, n, word) => `<li data-node="${esc(id)}"><div class="p1"><span class="arrow">${word}</span><span class="nm">${esc(this.nodeName(n) || id)}</span>
      <span class="where">${esc(!n ? '' : kindWord(n.subkind || n.kind))}</span></div></li>`;
    html += `<div class="d-sec"><ul class="peers">${end(e.source, s, 'from')}${end(e.target, t, 'to')}</ul></div>`;
    return html;
  }
}

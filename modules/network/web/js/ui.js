// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// DOM panels around the 3D view: top bar, left rail, tooltip and the detail drawer.
//
// Field names follow the graph format the map engine reads (namespace = device group, a node of
// kind "workload" = a device, its one "pod" = the address it holds). Nothing of that reaches the screen.
import { esc, fmtAgo, fmtBits, fmtByteRate, fmtBytes, fmtDur, fmtNum, fmtWhen, edgeRate, deviceType, isOffline,
  linkHtml, LEVEL_COLOR, nsColor, optionsHtml, plural } from './format.js';
import { LAYERS } from './scene.js';

const $ = (sel) => document.querySelector(sel);
const KIND_WORD = { External: 'Internet service', Internet: 'The internet', Project: 'Group',
  external: 'Internet service', clients: 'The internet', workload: 'Device', host: 'Host', service: 'Service', entry: 'Entry point' };
const kindWord = (k) => KIND_WORD[k] || k || '';

export class UI {
  constructor(ctx) {
    this.ctx = ctx;   // { actions, onNamespaces(list|null), onSelect(sel, focus), onView(v), onOptions(o), onClear(), search(q), ... }
    this.actions = ctx.actions;
    this.graph = null;
    this.meta = null;
    this.allNs = [];
    this._bindTop();
    this._bindToggles();
    $('#drawerClose').addEventListener('click', () => ctx.onClear());
  }

  // ------------------------------------------------------------------ names
  // The display name of a device group ("Phones & tablets" for phones-tablets).
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
    // Three sections: Triage and VPN (dashboards) and the map. The view switcher belongs to the map only.
    const section = view === 'triage' || view === 'vpn' || view === 'wifi' || view.startsWith('plugin:') ? view : 'topology';
    document.querySelectorAll('#topbar .sections button').forEach((b) => b.classList.toggle('on', b.dataset.section === section));
    $('#viewSeg').hidden = section !== 'topology';
    if (section === 'topology') document.querySelectorAll('#topbar .seg button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  }

  // Things waiting in the Triage view, shown on its tab whatever view is open.
  setWifiBadge({ count, level, title }) {
    const el = $('#wifiBadge');
    el.hidden = !count;
    el.textContent = count;
    el.className = `badge ${level || ''}`;
    el.title = title || '';
  }

  setVpnBadge({ count, title }) {
    const el = $('#vpnBadge');
    el.hidden = !count;
    el.textContent = count;
    el.title = title || '';
  }

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
        <span class="count">${plural(count, 'device')}${n.offline ? ` · ${n.offline} offline` : ''}</span></label></li>`;
    }).join('') || '<li class="empty">No matching groups</li>';
    $('#nsList').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
      const every = (this.allNs || []).map((n) => n.name);
      let next = sel == null ? every.slice() : sel.slice();
      if (cb.checked) next.push(cb.dataset.ns); else next = next.filter((x) => x !== cb.dataset.ns);
      next = [...new Set(next)];
      this.ctx.onNamespaces(every.length && every.every((x) => next.includes(x)) ? null : next);
    }));
  }

  // Search asks the collector (it knows every device, address and service, whatever the map shows);
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
      const d = n.device || {}, x = n.ext || {};
      const match = has(n.name) || has(this.groupTitle(n.namespace)) || has(d.ip) || has(d.mac) || has(d.vendor) || has(d.typeLabel)
        || (x.names || []).some(has) || (n.addresses || []).some(has) || (x.apps || []).some(has) || has(n.infra?.ip);
      if (match) hits.push({ sel: { type: 'node', id: n.id }, name: this.nodeName(n), ip: d.ip || n.infra?.ip || '', ns: n.namespace, kind: kindWord(n.subkind || n.kind) });
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
    el.title = [`Network Galaxy v${meta.version} · build ${meta.build || '-'}`,
      `This page's code: ${meta.uiBuild || 'unversioned'}${staleUi ? ' (reload to update)' : ''}`,
      meta.startedAt ? `started ${meta.startedAt}` : ''].filter(Boolean).join('\n');
  }

  showUpdate(from, to) {
    $('#updateText').textContent = `Network Galaxy was updated (v${from.version} build ${from.build} → v${to.version} build ${to.build}).`;
    $('#updateBanner').hidden = false;
    $('#updateReload').onclick = () => location.reload();
  }

  // Where the picture comes from, and whether each source is delivering.
  static sourceChips(s, mode) {
    const chips = [];
    if (mode === 'sample') chips.push({ cls: 'warn', mark: '◆', text: 'sample data', title: 'A made-up network for trying the dashboard out. Nothing here is real traffic.' });
    const nt = s?.ntopng;
    if (nt) {
      if (!nt.enabled) chips.push({ cls: 'off', mark: '○', text: 'ntopng', title: 'ntopng is not set up, so there is no live traffic picture.' });
      else if (nt.error) chips.push({ cls: 'warn', mark: '▲', text: 'ntopng · error', title: `ntopng could not be read: ${nt.error}` });
      else if (!nt.hosts && !nt.flows) chips.push({ cls: 'off', mark: '◌', text: 'ntopng · waiting', title: 'Connected to ntopng; waiting for its first answer.' });
      else chips.push({ cls: 'ok', mark: '●', text: 'ntopng', title: `Live traffic from ntopng: ${fmtNum(nt.hosts)} addresses, ${fmtNum(nt.flows)} connections${nt.ageS != null ? `, read ${fmtAgo(nt.ageS)}` : ''}.` });
    }
    const sl = s?.syslog;
    if (sl) {
      if (!sl.enabled) chips.push({ cls: 'off', mark: '○', text: 'router logs', title: 'Not listening for the router\'s firewall log, so blocked connection attempts are not shown.' });
      else if (sl.error) chips.push({ cls: 'warn', mark: '▲', text: 'router logs · error', title: `The router's log could not be received: ${sl.error}` });
      else if (!sl.received) chips.push({ cls: 'off', mark: '◌', text: 'router logs · waiting', title: `Listening${sl.port ? ` on port ${sl.port}` : ''}; the router has not sent anything yet.` });
      else chips.push({ cls: 'ok', mark: '●', text: 'router logs', title: `Firewall log from the router: ${fmtNum(sl.received)} messages${sl.lastMessageS != null ? `, the last one ${fmtAgo(sl.lastMessageS)}` : ''}${sl.leases ? `, ${fmtNum(sl.leases)} address leases` : ''}.` });
    }
    return chips;
  }

  setMeta(meta, graph) {
    this.meta = meta;
    this.setVersion(meta);
    const name = meta?.network?.name || graph?.network?.name || '';
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
    this.renderNetBox(graph.network);

    const v = graph.view;
    document.querySelector('#layers .layers').innerHTML = LAYERS.map((L) => {
      const k = counts[L.key] || {};
      return `<li style="--c:${L.color}"><span class="bar"></span><span class="name">${esc(L.label)}</span>
        <span class="meta">${esc(k.text || '')}${k.critical ? ` · <span class="c">${k.critical} critical</span>` : ''}${k.warning ? ` · <span class="w">${k.warning} warning</span>` : ''}</span></li>`;
    }).join('');

    // When the map is rolled up (problems only, or a very large network), say what is being shown.
    const scale = $('#scaleBox');
    scale.hidden = !(v && v.collapsed);
    if (v && v.collapsed) {
      const strip = (x) => this.groupTitle(x.replace(/^(grp|ns):/, ''));
      scale.innerHTML = `<h2>${v.focus ? 'Problems only' : 'Rolled up'}</h2>
        ${v.focus ? `<div class="sc-txt">${v.focusTotal ? `<b>${v.focusShown}</b>${v.focusShown < v.focusTotal ? ` worst of <b>${fmtNum(v.focusTotal)}</b>` : ''} device${v.focusTotal === 1 ? '' : 's'} with something to look at, each with what it talks to.` : '<b>Nothing needs attention.</b>'}
          The other ${plural(v.focusRest || 0, 'device')} ${v.focusRest === 1 ? 'is' : 'are'} rolled up into ${v.focusRest === 1 ? 'its group' : 'their groups'}. Double-click a group to open it.</div>`
    : `<div class="sc-txt"><b>${fmtNum(v.workloads)}</b> devices drawn as <b>${plural(v.projects, 'group')}</b>. Double-click one to open it.</div>`}
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
        <div class="why ns">${esc([cat, this.groupTitle(h.namespace), isEdge(h) ? 'connection' : ''].filter(Boolean).join(' · '))}</div></li>`;
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
    }).join('') : '<li class="empty">No traffic to speak of right now.</li>';
    $('#paths').querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('click', () => {
      const p = paths[+li.dataset.i];
      if ((p.edges || []).length === 1) this.ctx.onSelect({ type: 'edge', id: p.edges[0] }, false);
      else this.ctx.onSelect({ type: 'path', idx: +li.dataset.i, nodes: p.nodes || [], edges: p.edges || [] }, false);
    }));
  }

  // The network at a glance.
  renderNetBox(net) {
    const box = $('#netBox');
    box.hidden = !net;
    if (!net) { box.innerHTML = ''; return; }
    const tile = (v, k, title = '') => `<div class="net-tile" title="${esc(title)}"><div class="v">${v}</div><div class="k">${k}</div></div>`;
    const fw = net.firewall;
    const win = fw ? fmtDur(fw.windowSeconds || 3600) : '';
    box.innerHTML = `<h2>Network <span class="hint">${esc(net.name || '')}${this.selectedNs ? ' · all groups' : ''}</span></h2>
      <div class="net-grid">
        ${tile(`${fmtNum(net.online ?? 0)}<small> of ${fmtNum(net.devices ?? 0)}</small>`, `devices online${net.offline ? ` · ${fmtNum(net.offline)} offline` : ''}`, 'Devices on the network now, out of every device seen recently')}
        ${tile(fmtNum(net.flows ?? 0), 'active connections', 'Connections open through the router right now')}
        ${tile(`<span class="ar">↓</span>${esc(fmtBits(net.downloadBps ?? 0))}`, 'download', 'Coming in from the internet, all devices together')}
        ${tile(`<span class="ar">↑</span>${esc(fmtBits(net.uploadBps ?? 0))}`, 'upload', 'Going out to the internet, all devices together')}
        ${tile(fmtNum(net.services ?? 0), 'internet services in use', `${fmtNum(net.remoteHosts ?? 0)} outside addresses, grouped by the service they belong to`)}
        ${tile(fmtNum(net.forwards ?? 0), `port forward${net.forwards === 1 ? '' : 's'} open`, 'Doors the router keeps open from the internet to a device')}
      </div>
      ${fw ? `<button class="net-fw" data-node="host:router" title="Details on the router">
        <b>${fmtNum(fw.blockedIn ?? 0)}</b> connection attempt${fw.blockedIn === 1 ? '' : 's'} turned away in the last ${esc(win)}${fw.blockedInSources ? `<span> · ${plural(fw.blockedInSources, 'source')}${fw.blockedInKnownBad ? `, ${fmtNum(fw.blockedInKnownBad)} from known-bad addresses` : ''}</span>` : ''}</button>` : ''}
      ${this._blockedLine()}
      ${net.vpn ? `<button class="net-fw vpn-line" data-goto-vpn title="Open the VPN panel">
        <span class="dot ${net.vpn.connected ? 'on' : ''}"></span><b>${fmtNum(net.vpn.connected)}</b> on the VPN${net.vpn.failed24h ? `<span> · <span class="w">${plural(net.vpn.failed24h, 'failed login')}</span> in 24 h</span>` : ''}</button>` : ''}
      ${net.wifi ? `<button class="net-fw vpn-line" data-goto-wifi title="Open the Wi-Fi panel: where connections break">
        <span class="dot ${net.wifi.level === 'bad' || net.wifi.apsDown || net.wifi.bad ? 'crit' : net.wifi.level === 'warn' || net.wifi.warn ? 'warnd' : 'on'}"></span>${net.wifi.level === 'bad' || net.wifi.level === 'warn' ? `<span class="w">${esc(net.wifi.title)}</span>`
          : net.wifi.enabled ? `<b>${fmtNum(net.wifi.clients)}</b> on Wi-Fi${net.wifi.bad + net.wifi.warn ? `<span> · <span class="w">${fmtNum(net.wifi.bad + net.wifi.warn)} with a poor connection</span></span>` : '<span> · all connections healthy</span>'}`
          : `Path to the internet healthy${(net.wifi.hops || []).find((h) => h.id === 'internet')?.p50Ms != null ? `<span> · ${Math.round(net.wifi.hops.find((h) => h.id === 'internet').p50Ms)} ms</span>` : ''}`}</button>` : ''}
      ${net.learning ? '<div class="net-note">Still learning what is normal for each device.</div>' : ''}`;
    box.querySelector('[data-goto-wifi]')?.addEventListener('click', () => this.ctx.onWifi?.(null));
    box.querySelector('[data-goto-blocks]')?.addEventListener('click', () => this.ctx.onBlocks?.());
    box.querySelector('[data-goto-vpn]')?.addEventListener('click', () => this.ctx.onSection?.('vpn'));
    box.querySelectorAll('[data-node]').forEach((el) => el.addEventListener('click', () => {
      if (this._pageNodes?.has(el.dataset.node)) this.ctx.onSelect({ type: 'node', id: el.dataset.node }, false);
    }));
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
      const w = byId.get(hit.wid);
      const via = byId.get(hit.pod?.hostId);
      html = `<div class="t1">${esc(hit.pod?.ip || hit.pod?.name)}</div>
        <div class="t2">the address ${esc(w?.name || 'a device')} holds now${via ? ` · through ${esc(via.name)}` : ''}</div>`;
    } else if (hit.type === 'edge') {
      const e = this.graph.edges.find((x) => x.id === hit.id);
      if (!e) { tip.hidden = true; return; }
      const s = byId.get(e.source), t = byId.get(e.target);
      const net = e.net || {};
      const rate = edgeRate(e);
      const l2 = [rate || (e.type === 'exposes' ? 'no traffic seen' : 'quiet'), e.traffic?.connections ? plural(e.traffic.connections, 'connection') : ''].filter(Boolean).join(' · ');
      const l3 = [(e.ports || []).slice(0, 4).join(', '), (net.apps || []).slice(0, 3).join(', ')].filter(Boolean).join(' · ');
      html = `<div class="t1">${esc(this.nodeName(s))} → ${esc(this.nodeName(t))}</div>
        <div class="t2">${e.type === 'exposes' ? 'port forward · ' : ''}${esc(l2)}</div>${l3 ? `<div class="t2">${esc(l3)}</div>` : ''}`;
      const f = (this.graph.findings || []).find((x) => x.subject === e.id && x.severity !== 'info');
      if (f) html += `<div class="t2" style="color:${LEVEL_COLOR[f.severity]}">${esc(f.title)}</div>`;
    } else {
      const n = byId.get(hit.id);
      if (!n) { tip.hidden = true; return; }
      html = `<div class="t1">${esc(this.nodeName(n))}</div>`;
      if (n.aggregate && n.kind === 'workload') {
        const k = n.counts || {};
        html += `<div class="t2">Group · ${plural(k.workloads || 0, 'device')}${k.failing ? ` · ${k.failing} need${k.failing === 1 ? 's' : ''} attention` : ''}</div><div class="t2">double-click to open</div>`;
      } else if (n.kind === 'workload') {
        const d = n.device || {};
        html += `<div class="t2">${esc(deviceType(n))} · ${esc(this.groupTitle(n.namespace))}${n.ghost ? ' · outside the groups shown' : ''}</div>`;
        html += isOffline(n) ? `<div class="t2">offline${d.lastSeenS != null ? ` · last seen ${esc(fmtAgo(d.lastSeenS))}` : ''}${d.ip ? ` · was ${esc(d.ip)}` : ''}</div>`
          : `<div class="t2">${esc(d.ip || 'no address')} · ↓ ${esc(fmtBits(d.downBps || 0))} · ↑ ${esc(fmtBits(d.upBps || 0))}</div>`;
      } else if (n.kind === 'external' && n.scope === 'defence') {
        html += `<div class="t2">Defence</div><div class="t2">${esc(n.defence?.summary || '')}</div>`;
      } else if (n.kind === 'external') {
        const x = n.ext || {};
        const users = this.graph.edges.filter((e) => e.target === n.id).length;
        html += `<div class="t2">Internet service${x.addressCount ? ` · ${plural(x.addressCount, 'address', 'addresses')}` : ''}</div>
          <div class="t2">${esc(fmtByteRate(x.bytesPerSec || 0))} · used by ${plural(users, 'device')}</div>`;
        if (x.allBlocked || (x.blocked || []).length) html += `<div class="t2 blk-t">⛔ ${x.allBlocked ? 'Blocked' : 'Partly blocked'} by you</div>`;
        else if (x.blacklisted) html += `<div class="t2" style="color:${LEVEL_COLOR.critical}">On a known-bad address list</div>`;
      } else if (n.kind === 'clients') {
        const fw = this.graph.edges.filter((e) => e.source === n.id && e.type === 'exposes');
        html += `<div class="t2">Anyone outside reaching in</div><div class="t2">${plural(fw.length, 'port forward')} open${n.seenFrom?.length ? ` · ${plural(n.seenFrom.length, 'outside address', 'outside addresses')} seen` : ''}</div>`;
      } else if (n.kind === 'host') {
        const inf = n.infra || {};
        html += `<div class="t2">${esc(n.subkind || 'Host')}${inf.ip ? ` · ${esc(inf.ip)}` : ''}</div>`;
        const dev = this._devicesOn(n).length;
        html += `<div class="t2">${[inf.throughputBps != null ? fmtBits(inf.throughputBps) : '', inf.flows != null ? plural(inf.flows, 'connection') : '', plural(inf.devices ?? dev, 'device')].filter(Boolean).map(esc).join(' · ')}</div>`;
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
    // When the map is rolled up the page holds groups; the server supplies the real device, its
    // connections and neighbours. Render from that, then put the page's graph back.
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
    d.querySelectorAll('[data-goto-section]').forEach((el) => el.addEventListener('click', () => this.ctx.onSection?.(el.dataset.gotoSection)));
    d.querySelectorAll('[data-goto-blocks]').forEach((el) => el.addEventListener('click', () => this.ctx.onBlocks?.()));
    d.querySelectorAll('[data-goto-vpn]').forEach((el) => el.addEventListener('click', () => this.ctx.onSection?.('vpn')));
    d.querySelectorAll('[data-goto-wifi]').forEach((el) => el.addEventListener('click', () => this.ctx.onWifi?.(el.dataset.gotoWifi || null)));
    d.querySelectorAll('[data-edge]').forEach((el) => el.addEventListener('click', () => this.ctx.onSelect({ type: 'edge', id: el.dataset.edge }, false)));
  }

  _badge(cls, text) {
    return `<div class="risk-badge ${cls}">${esc(text)}</div>`;
  }

  // The findings about one or more subjects: what, why, and what to do.
  // "N addresses blocked", with whether the router has the list.
  _blockedLine() {
    const b = this.actions?.blocks;
    if (!b || (!b.count && !this.actions.enabled)) return '';
    const st = this.actions.routerStatus();
    return `<button class="net-fw blk-line" data-goto-blocks title="${esc(st?.text || '')}">
      <span class="dot ${st?.level || 'ok'}"></span><b>${fmtNum(b.count)}</b> address${b.count === 1 ? '' : 'es'} blocked${b.count && st && st.level !== 'ok' ? `<span> · ${esc(st.level === 'crit' ? 'the router refused some' : 'not on the router yet')}</span>` : ''}</button>`;
  }

  // The Block control for a finding with addresses to block (and its blocked state).
  _findingBlock(f, skip) {
    if (!this.actions || !(f.block || []).length) return '';
    if (skip && f.block.every((t) => skip.has(t.target))) return '';
    return this.actions.renderControl({ key: `f:${f.id}`, targets: f.block, reason: f.title, finding: f.id,
      label: f.subjectName || '' });
  }

  _diagnosis(ids, skipBlock = null) {
    const want = new Set([].concat(ids));
    const fs = (this.graph.findings || []).filter((f) => want.has(f.subject));
    if (!fs.length) return '';
    const real = fs.filter((f) => f.severity !== 'info');
    const info = fs.filter((f) => f.severity === 'info');
    const card = (f) => `<div class="d-find ${f.severity}"><div class="cat">${esc(this.catTitle(f.category))}</div>
      <div class="t">${esc(f.title)}</div>
      ${f.detail ? `<p>${esc(f.detail)}</p>` : ''}${f.cause ? `<p><b>Why</b>${esc(f.cause)}</p>` : ''}
      ${this._findingBlock(f, skipBlock)}
      ${f.options?.length ? optionsHtml(f) : ''}${f.fix && !f.options?.length ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : f.fix ? `<p class="fix"><b>In short</b>${esc(f.fix)}</p>` : ''}
      ${(f.evidence || []).filter((e) => !/:\s*$/.test(e)).map((e) => `<pre class="ev-msg">${esc(e)}</pre>`).join('')}</div>`;
    return (real.length ? `<div class="d-sec"><h3>What needs attention</h3>${real.map(card).join('')}</div>` : '')
      + (info.length ? `<div class="d-sec"><h3>Worth knowing</h3>${info.map(card).join('')}</div>` : '');
  }

  // A list of connections of one node, busiest first. dir: which end the *other* party is.
  _links(edges, byId, self) {
    if (!edges.length) return '<div class="empty">Nothing right now.</div>';
    const sorted = [...edges].sort((a, b) => (b.traffic?.bytesPerSec || 0) - (a.traffic?.bytesPerSec || 0));
    return `<ul class="peers">${sorted.map((e) => {
      const outgoing = e.source === self;
      const other = byId.get(outgoing ? e.target : e.source);
      const name = other ? this.nodeName(other) : (outgoing ? e.target : e.source);
      const lvl = e.risk?.level || 'ok';
      const rate = edgeRate(e);
      const blocked = other?.kind === 'external' && (other.ext?.allBlocked || (other.ext?.blocked || []).length);
      const where = !other ? '' : blocked ? 'blocked' : other.scope === 'defence' ? 'defence' : other.kind === 'external' ? 'internet' : other.kind === 'clients' ? 'port forward' : other.kind === 'workload' ? this.groupTitle(other.namespace) : kindWord(other.subkind);
      const bits = e.blocked ? [] : [e.traffic?.connections ? plural(e.traffic.connections, 'connection') : '', (e.ports || []).slice(0, 3).join(', '), (e.net?.apps || []).filter((a) => a !== name).slice(0, 2).join(', ')].filter(Boolean);
      return `<li data-edge="${esc(e.id)}"><div class="p1"><span class="st" style="background:${LEVEL_COLOR[lvl]}"></span>
        <span class="arrow" title="${outgoing ? 'this one opened the connection' : 'the other side opened the connection'}">${outgoing ? '→' : '←'}</span>
        <span class="nm">${esc(name)}</span><span class="where${blocked ? ' blk-t' : ''}">${blocked ? '⛔ ' : ''}${esc(where)}</span>
        <span class="rate">${esc(e.blocked ? `${fmtNum(e.stopped || 0)} stopped` : rate || (e.type === 'exposes' ? 'no traffic seen' : 'quiet'))}</span></div>
        ${bits.length ? `<div class="p2">${esc(bits.join(' · '))}</div>` : ''}</li>`;
    }).join('')}</ul>`;
  }

  _devicesOn(host) {
    return (this.graph?.nodes || []).filter((w) => w.kind === 'workload' && !w.aggregate
      && ((w.pods || []).some((p) => p.hostId === host.id) || (w.hosts || []).some((h) => `host:${h}` === host.id)));
  }

  _deviceList(list) {
    const rate = (w) => (w.device?.downBps || 0) + (w.device?.upBps || 0);
    const sorted = [...list].sort((a, b) => (isOffline(a) ? 1 : 0) - (isOffline(b) ? 1 : 0) || rate(b) - rate(a) || a.name.localeCompare(b.name));
    return `<ul class="peers">${sorted.map((w) => {
      const off = isOffline(w);
      return `<li data-node="${esc(w.id)}"><div class="p1">
        <span class="st" style="background:${off ? LEVEL_COLOR.offline : LEVEL_COLOR[w.risk?.level || 'ok']}"></span><span class="nm">${esc(w.name)}</span>
        <span class="where">${esc(deviceType(w))}</span>
        <span class="rate">${off ? 'offline' : esc(w.device?.ip || '')}</span></div>
        ${off ? '' : `<div class="p2">↓ ${esc(fmtBits(w.device?.downBps || 0))} · ↑ ${esc(fmtBits(w.device?.upBps || 0))}</div>`}</li>`;
    }).join('')}</ul>`;
  }

  _nodeHtml(n, byId) {
    if (n.aggregate && n.kind === 'workload') return this._aggregateHtml(n);
    if (n.kind === 'workload') return this._deviceHtml(n, byId);
    if (n.kind === 'external' && n.scope === 'defence') return this._defenceHtml(n, byId);
    if (n.kind === 'external') return this._externalHtml(n, byId);
    if (n.kind === 'clients') return this._inboundHtml(n, byId);
    if (n.kind === 'host') return this._hostHtml(n, byId);
    return `<div class="d-kind">${esc(kindWord(n.subkind || n.kind))}</div><div class="d-title">${esc(n.name)}</div>${this._diagnosis(n.id)}`;
  }

  // ---- a defence: DNS blocking, the firewall, your block list
  _defenceHtml(n, byId) {
    const d = n.defence || {};
    const lines = this.graph.edges.filter((e) => e.target === n.id && e.blocked).sort((a, b) => (b.stopped || 0) - (a.stopped || 0));
    const rows = (list, word) => `<ul class="peers">${list.map((e) => {
      const o = byId.get(e.source);
      return `<li data-node="${esc(e.source)}"><div class="p1"><span class="st" style="background:#b06a78"></span>
        <span class="nm">${esc(o ? this.nodeName(o) : e.source)}</span><span class="where">${esc(o?.device?.ip || '')}</span>
        <span class="rate">${fmtNum(e.stopped || 0)} ${word}</span></div></li>`;
    }).join('')}</ul>`;
    const why = String(n.scopeWhy || '');
    let html = `<div class="d-kind">Defence</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc(why.charAt(0).toUpperCase() + why.slice(1))}.</div>`;
    if (d.type === 'dns') {
      html += this._badge('ok', d.total ? `${fmtNum(d.total)} blocked ${d.total === 1 ? 'connection' : 'connections'} now` : 'Nothing is being blocked right now');
      html += `<div class="d-sec"><h3>Devices being blocked now</h3>${lines.length ? rows(lines, 'blocked')
        : '<div class="empty">No device is connecting to the block page right now.</div>'}
        <div class="d-sub">Counted from connections to the block page, so which names were asked for is not shown here.</div></div>`;
      if (d.adblock) {
        html += `<div class="d-sec"><h3>Ad blocker</h3><div class="d-sub">${d.adblock.ready ? `${fmtNum(d.adblock.names)} names are being published for the router to block.`
          : 'Its list is still being put together.'}</div><button class="btn" data-goto-section="plugin:adblock">Open the Ad blocker ▸</button></div>`;
      }
    } else if (d.type === 'firewall') {
      const out = lines.filter((e) => e.source !== 'clients');
      html += this._badge('ok', d.blockedIn ? `${fmtNum(d.blockedIn)} turned away in the last ${d.windowMinutes} min` : `Nothing turned away in the last ${d.windowMinutes} min`);
      html += `<div class="d-sec"><h3>At the door</h3><dl class="kv">
        <dt>Turned away</dt><dd>${fmtNum(d.blockedIn || 0)} attempt${d.blockedIn === 1 ? '' : 's'} from outside <span class="dim">· ${plural(d.sources || 0, 'source')}</span></dd>
        <dt>On a known-bad list</dt><dd>${d.knownBad ? `<span class="w">${fmtNum(d.knownBad)}</span> <span class="dim">· ${plural(d.knownBadSources || 0, 'source')}</span>` : 'none'}</dd>
        <dt>Refused going out</dt><dd>${d.blockedOut ? `<span class="w">${fmtNum(d.blockedOut)}</span>` : 'none'}</dd></dl>
        <div class="d-sub">Outside addresses are not on the map, so what they sent shows as one count${byId.has('clients') ? ' and one line from the internet' : ''}.</div></div>`;
      if (out.length) html += `<div class="d-sec"><h3>Devices refused on the way out</h3>${rows(out, 'refused')}</div>`;
      if ((d.topSources || []).length) {
        html += `<div class="d-sec"><h3>Knocking most</h3><ul class="details">${d.topSources.map((s) => `<li><span class="mono">${esc(s.ip)}</span>
          <span class="dim">· ${plural(s.count, 'attempt')}${s.knownBad ? ' · on a known-bad list' : ''}</span></li>`).join('')}</ul></div>`;
      }
      html += '<div class="d-sec"><button class="btn" data-node="host:router">Details on the router ▸</button></div>';
    } else if (d.type === 'blocks') {
      html += this._badge('ok', `${plural(d.entries || 0, 'address', 'addresses')} blocked by you`);
      html += `<div class="d-sec"><h3>The list</h3><dl class="kv">
        <dt>Addresses and ranges</dt><dd>${fmtNum(d.entries || 0)}</dd>
        <dt>Applied on the router</dt><dd>${d.applied === d.entries ? 'all of them' : `<span class="w">${fmtNum(d.applied || 0)} of ${fmtNum(d.entries || 0)}</span> <span class="dim">· the rest at the router's next fetch</span>`}</dd></dl></div>`;
      if ((d.services || []).length) {
        html += `<div class="d-sec"><h3>Blocked services on the map</h3><ul class="peers">${d.services.map((s) => `<li data-node="${esc(s.id)}"><div class="p1">
          <span class="st" style="background:#b06a78"></span><span class="nm">${esc(s.name)}</span><span class="where blk-t">⛔ blocked</span></div></li>`).join('')}</ul>
          <div class="d-sub">Still on the map because a device is still trying to reach it.</div></div>`;
      }
      html += '<div class="d-sec"><button class="btn" data-goto-blocks>Manage the block list ▸</button></div>';
    }
    return html;
  }

  // ---- a device
  // ---- a VPN session
  _vpnHtml(n, byId) {
    const v = n.vpn || {};
    const edges = this.graph.edges.filter((e) => e.source === n.id || e.target === n.id);
    let html = `<div class="d-kind">VPN session</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">Connected to your network through the router's VPN${v.server ? ` (${esc(v.server)})` : ''}.</div>`;
    html += this._badge('ok', `Connected${v.durationS != null ? ` · ${fmtDur(v.durationS)}` : ''}`);
    html += `<div class="d-sec"><h3>Session</h3><dl class="kv">
      <dt>User</dt><dd>${esc(v.user || '–')}${v.cert && v.cert !== v.user ? ` <span class="dim">· certificate ${esc(v.cert)}</span>` : ''}</dd>
      <dt>Device</dt><dd>${esc([v.platform, v.app].filter(Boolean).join(' · ') || 'not identified')}</dd>
      <dt>Connecting from</dt><dd><span class="mono">${esc(v.real || '–')}</span>${v.where ? ` <span class="dim">· ${esc(v.where)}</span>` : ''}</dd>
      <dt>VPN address</dt><dd class="mono">${esc(v.tunnel || '–')}</dd>
      <dt>Since</dt><dd>${esc(fmtWhen(v.since))}</dd>
      ${v.cipher ? `<dt>Encryption</dt><dd>${esc(v.cipher)}</dd>` : ''}
    </dl></div>`;
    html += `<div class="d-sec"><h3>Traffic through the tunnel</h3><dl class="kv">
      <dt>To it</dt><dd>${esc(fmtBits(v.downBps || 0))}</dd>
      <dt>From it</dt><dd>${esc(fmtBits(v.upBps || 0))}</dd>
      <dt>So far</dt><dd>${esc(fmtBytes(v.bytesOut || 0))} sent to it · ${esc(fmtBytes(v.bytesIn || 0))} from it</dd>
    </dl><div class="d-sub">Totals come from the VPN server, so they include everything the session did, home devices and internet alike.</div></div>`;
    html += `<div class="d-sec"><h3>Home devices it is using (${edges.length})</h3>${this._links(edges, byId, n.id)}
      <div class="d-sub">From what ntopng sees of its VPN address on your home network.</div></div>`;
    html += '<div class="d-sec"><button class="btn" data-goto-vpn>Open the VPN panel ▸</button></div>';
    return html;
  }

  _deviceHtml(n, byId) {
    if (n.vpn) return this._vpnHtml(n, byId);
    const d = n.device || {};
    const off = isOffline(n);
    const lvl = n.risk?.level || 'ok';
    const edges = this.graph.edges.filter((e) => e.source === n.id || e.target === n.id);
    const usual = (now, avg) => `${esc(fmtBits(now || 0))}${avg != null ? ` <span class="dim">· usually ${esc(fmtBits(avg))}</span>` : ''}`;
    let html = `<div class="d-kind">${esc(deviceType(n))}</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc(this.groupTitle(n.namespace))}${n.ghost ? ' · outside the groups shown' : ''}</div>`;
    html += off ? this._badge('offline', `Offline${d.lastSeenS != null ? ` · last seen ${fmtAgo(d.lastSeenS)}` : ''}`)
      : this._badge(lvl, lvl === 'ok' ? 'Online' : lvl === 'warning' ? 'Online · worth a look' : 'Online · needs attention');
    html += this._diagnosis(n.id);

    const via = (n.hosts || []).map((h) => byId.get(`host:${h}`)).filter(Boolean);
    html += `<div class="d-sec"><h3>Device</h3><dl class="kv">
      <dt>${off ? 'Last address' : 'Address'}</dt><dd class="mono">${esc(d.ip || '–')}</dd>
      <dt>Hardware address</dt><dd><span class="mono">${esc(d.mac || '–')}</span>${d.randomMac ? ' <span class="tag" title="The device makes up a new hardware address for each network to avoid being tracked, so the manufacturer cannot be read from it.">private address</span>' : ''}</dd>
      <dt>Made by</dt><dd>${esc(d.vendor || (d.randomMac ? 'hidden by the private address' : 'unknown'))}</dd>
      <dt>First seen</dt><dd>${esc(fmtWhen(d.firstSeen))}</dd>
      ${off ? `<dt>Last seen</dt><dd>${esc(fmtWhen(d.lastSeen))}</dd>` : ''}
      ${via.length ? `<dt>Connected through</dt><dd>${via.map((h) => `<a class="in" data-node="${esc(h.id)}">${esc(h.name)}</a>`).join(', ')}</dd>` : ''}
    </dl></div>`;

    const wf = d.wifi;
    if (wf && !off) {
      const cls = wf.level === 'bad' ? 'c' : wf.level === 'warn' ? 'w' : '';
      html += `<div class="d-sec"><h3>Wi-Fi</h3><dl class="kv">
        <dt>Connection</dt><dd><span class="${cls}">${esc(wf.title)}</span></dd>
        <dt>Access point</dt><dd>${esc(wf.ap || '–')}${wf.ssid ? ` <span class="dim">· “${esc(wf.ssid)}”</span>` : ''}</dd>
        <dt>Radio</dt><dd>${esc([wf.band, wf.channel ? 'channel ' + wf.channel : '', wf.proto].filter(Boolean).join(' · ') || '–')}</dd>
        <dt>Signal</dt><dd>${wf.signal == null ? '–' : Math.round(wf.signal) + ' dBm'}${wf.retryPct != null ? ` <span class="dim">· ${Math.round(wf.retryPct)}% of frames re-sent</span>` : ''}</dd>
        <dt>Ping</dt><dd>${wf.pingMs == null ? '–' : Math.round(wf.pingMs * 10) / 10 + ' ms'}${wf.lossPct ? ` <span class="dim">· ${Math.round(wf.lossPct)}% lost</span>` : ''}</dd>
        <dt>Drops in 24 h</dt><dd>${fmtNum(wf.drops24h || 0)}${wf.roams24h ? ` <span class="dim">· ${plural(wf.roams24h, 'move')} between access points</span>` : ''}</dd>
      </dl><button class="btn" data-goto-wifi="${esc(wf.mac)}">Where does its connection break? ▸</button></div>`;
    }

    if (off) {
      html += `<div class="d-sec"><h3>Traffic</h3><div class="d-sub">Not on the network right now, so there is nothing to measure. When it is here it usually moves
        ↓ ${esc(fmtBits(d.downAvgBps || 0))} and ↑ ${esc(fmtBits(d.upAvgBps || 0))}.</div></div>`;
    } else {
      html += `<div class="d-sec"><h3>Traffic</h3><dl class="kv">
        <dt>Download</dt><dd>${usual(d.downBps, d.downAvgBps)}</dd>
        <dt>Upload</dt><dd>${usual(d.upBps, d.upAvgBps)}</dd>
        <dt>Connections</dt><dd>${fmtNum(d.flows ?? 0)} open${d.peers != null ? ` · to ${plural(d.peers, 'other address', 'other addresses')}` : ''}${d.peersAvg != null ? ` <span class="dim">· usually ${fmtNum(Math.round(d.peersAvg * 10) / 10)}</span>` : ''}</dd>
        ${d.bytesRecvd != null || d.bytesSent != null ? `<dt>So far</dt><dd>${esc(fmtBytes(d.bytesRecvd || 0))} received · ${esc(fmtBytes(d.bytesSent || 0))} sent</dd>` : ''}
        ${d.dnsBlocked > 0 ? `<dt>Blocked lookups</dt><dd><span class="w">${fmtNum(d.dnsBlocked)}</span> <span class="dim">· names on the block list it asked for</span></dd>` : ''}
        ${d.ntopScore >= 1500 ? `<dt>ntopng score</dt><dd><span class="w">${fmtNum(d.ntopScore)}</span> <span class="dim">· ntopng finds its traffic unusual</span></dd>` : ''}
        ${d.blacklisted ? '<dt>Threat list</dt><dd><span class="c">in contact with a known-bad address</span></dd>' : ''}
      </dl></div>`;
    }

    const apps = (d.apps || []).filter((a) => a && a.name).slice(0, 6);
    if (apps.length) {
      const top = Math.max(1, ...apps.map((a) => a.bytes || 0));
      html += `<div class="d-sec"><h3>Top apps</h3><ul class="bars">${apps.map((a) => `<li><span class="nm">${esc(a.name)}</span>
        <span class="val">${esc(fmtBytes(a.bytes || 0))}</span><div class="meter"><i style="width:${Math.max(2, Math.round(((a.bytes || 0) / top) * 100))}%"></i></div></li>`).join('')}</ul></div>`;
    }

    html += `<div class="d-sec"><h3>What it talks to (${edges.length})</h3>${this._links(edges, byId, n.id)}</div>`;

    const fw = d.forwards || [];
    if (fw.length) {
      html += `<div class="d-sec"><h3>Port forwards pointing at it</h3><ul class="details">${fw.map((f) => `<li><b>${esc(f.name || 'Unnamed forward')}</b>:
        internet port ${esc(f.port)}/${esc(String(f.proto || '').toUpperCase())} → ${esc(f.target)}:${esc(f.targetPort)}</li>`).join('')}</ul>
        <div class="d-sub">Anyone on the internet can open a connection to this device through ${fw.length === 1 ? 'this port' : 'these ports'}.</div></div>`;
    }
    if (d.ntopngUrl) html += `<div class="d-sec">${linkHtml(d.ntopngUrl, 'Open in ntopng', 'btn out')}</div>`;
    return html;
  }

  // ---- an internet service
  _externalHtml(n, byId) {
    const x = n.ext || {};
    const users = this.graph.edges.filter((e) => e.target === n.id || e.source === n.id);
    const lvl = n.risk?.level || 'ok';
    const count = x.addressCount ?? (n.addresses || []).length;
    let html = `<div class="d-kind">Internet service</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${[count ? plural(count, 'address', 'addresses') + ' in use' : '', x.services > 1 ? `${fmtNum(x.services)} services folded together` : ''].filter(Boolean).join(' · ') || 'On the public internet'}</div>`;
    if (x.blacklisted) {
      html += `<div class="d-warn"><b>On a known-bad list.</b> An address behind this name is on a threat list ntopng tracks. A device talking to it deserves a look.</div>`;
    } else if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    // Only plain addresses can be blocked from here: a named service (Netflix, a CDN) shares its
    // addresses with others, so the server offers no targets for it and there is no button.
    const targets = [...new Set([...(x.blockTargets || []), ...(x.blocked || []).map((e) => e.target)])];
    const ctl = targets.length && this.actions ? this.actions.renderControl({ key: `x:${n.id}`, label: n.name,
      reason: x.blacklisted ? `${n.name} is on a known-bad address list` : `Blocked from the ${n.name} page`,
      targets: targets.map((t) => ({ target: t, blocked: (x.blocked || []).find((e) => e.target === t) || null })) }) : '';
    if (ctl) html += `<div class="d-sec"><h3>Block</h3>${ctl}</div>`;
    html += this._diagnosis([n.id, ...users.map((e) => e.id)], new Set(targets));
    html += `<div class="d-sec"><h3>Traffic</h3><dl class="kv">
      <dt>Rate now</dt><dd>${esc(fmtByteRate(x.bytesPerSec || 0))}</dd>
      ${x.bytes != null ? `<dt>So far</dt><dd>${esc(fmtBytes(x.bytes))}</dd>` : ''}
      ${(x.apps || []).length ? `<dt>Apps</dt><dd>${esc(x.apps.join(', '))}</dd>` : ''}
    </dl></div>`;
    html += `<div class="d-sec"><h3>Devices using it (${users.length})</h3>${this._links(users, byId, n.id)}</div>`;
    const members = x.members || [];
    if (members.length) {
      html += `<div class="d-sec"><h3>Services folded in here (${members.length})</h3><ul class="details">${members.slice(0, 40).map((m) => {
        if (typeof m === 'string') return `<li>${esc(m)}</li>`;
        const r = m.bytesPerSec != null ? fmtByteRate(m.bytesPerSec) : m.bps != null ? fmtBits(m.bps) : m.rateText || '';
        return `<li>${esc(m.name || m.id || '')}${r ? ` <span class="dim">· ${esc(r)}</span>` : ''}</li>`;
      }).join('')}</ul>${members.length > 40 ? `<div class="d-sub">+${members.length - 40} more</div>` : ''}
        <div class="d-sub">Small services are drawn as one to keep the map readable.</div></div>`;
    }
    const names = x.names || [];
    if (names.length) {
      html += `<div class="d-sec"><h3>Known names</h3><div class="mono">${names.slice(0, 12).map(esc).join('<br>')}${names.length > 12 ? `<br>+${names.length - 12} more` : ''}</div></div>`;
    }
    const addrs = n.addresses || [];
    if (addrs.length) {
      html += `<div class="d-sec"><h3>Addresses</h3><div class="mono">${addrs.slice(0, 12).map(esc).join('<br>')}${count > Math.min(12, addrs.length) ? `<br>+${count - Math.min(12, addrs.length)} more` : ''}</div></div>`;
    }
    return html;
  }

  // ---- the internet reaching in
  _inboundHtml(n, byId) {
    const fw = this.graph.edges.filter((e) => e.source === n.id).sort((a, b) => (b.traffic?.bytesPerSec || 0) - (a.traffic?.bytesPerSec || 0));
    let html = `<div class="d-kind">The internet</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${esc(n.scopeWhy ? n.scopeWhy.charAt(0).toUpperCase() + n.scopeWhy.slice(1) : 'Anyone on the internet opening a connection to your network')}.</div>`;
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>What is open to the internet (${fw.length})</h3>${fw.length ? `<ul class="peers">${fw.map((e) => {
      const t = byId.get(e.target);
      const rate = edgeRate(e);
      const seen = (e.evidence || []).includes('observed');
      return `<li data-edge="${esc(e.id)}"><div class="p1"><span class="st" style="background:${LEVEL_COLOR[t?.risk?.level || e.risk?.level || 'ok']}"></span>
        <span class="nm">${esc((e.via || [])[0] || (e.ports || []).join(', ') || 'Port forward')}</span>
        <span class="where">→ ${esc(t?.name || e.target)}</span>
        <span class="rate">${esc(rate || (seen ? 'quiet' : 'no traffic seen'))}</span></div>
        ${(e.details || []).map((x) => `<div class="p2">${esc(x)}</div>`).join('') || ((e.ports || []).length ? `<div class="p2">${esc(e.ports.join(', '))}</div>` : '')}</li>`;
    }).join('')}</ul>` : '<div class="empty">No port forwards: nothing on your network can be reached from outside.</div>'}</div>`;
    const from = n.seenFrom || [];
    html += `<div class="d-sec"><h3>Seen coming in from</h3>${from.length ? `<div class="mono">${from.slice(0, 20).map(esc).join('<br>')}${from.length > 20 ? `<br>+${from.length - 20} more` : ''}</div>
      <div class="d-sub">Outside addresses with a connection open through a port forward now.</div>` : '<div class="empty">Nobody is connected from outside right now.</div>'}</div>`;
    return html;
  }

  // ---- the router, or another host devices hang off
  _hostHtml(n, byId) {
    const inf = n.infra || {};
    const lvl = n.risk?.level || 'ok';
    const isRouter = n.id === 'host:router' || n.subkind === 'Router';
    const devices = this._devicesOn(n);
    let html = `<div class="d-kind">${esc(n.subkind || 'Host')}</div><div class="d-title">${esc(n.name)}</div>
      <div class="d-sub">${isRouter ? 'Everything between your network and the internet passes through it.' : `${plural(devices.length, 'device')} run${devices.length === 1 ? 's' : ''} on it.`}</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    if (isRouter) {
      html += `<div class="d-sec"><h3>Right now</h3><dl class="kv">
        ${inf.ip ? `<dt>Address</dt><dd class="mono">${esc(inf.ip)}</dd>` : ''}
        ${inf.throughputBps != null ? `<dt>Traffic</dt><dd>${esc(fmtBits(inf.throughputBps))}</dd>` : ''}
        ${inf.flows != null ? `<dt>Connections</dt><dd>${fmtNum(inf.flows)} open</dd>` : ''}
        ${inf.devices != null ? `<dt>Devices</dt><dd>${fmtNum(inf.devices)}</dd>` : ''}
        ${inf.hosts != null ? `<dt>Addresses seen</dt><dd>${fmtNum(inf.hosts)}${inf.localHosts != null ? ` <span class="dim">· ${fmtNum(inf.localHosts)} on your network</span>` : ''}</dd>` : ''}
        ${inf.drops ? `<dt>Dropped packets</dt><dd><span class="w">${fmtNum(inf.drops)}</span> <span class="dim">· ntopng could not keep up</span></dd>` : ''}
      </dl></div>`;
      html += this._firewallHtml(inf);
    } else if (inf.ip) {
      html += `<div class="d-sec"><dl class="kv"><dt>Address</dt><dd class="mono">${esc(inf.ip)}</dd></dl></div>`;
    }
    html += this._diagnosis(n.id);
    html += `<div class="d-sec"><h3>${isRouter ? 'Devices connected to it' : 'Devices on it'} (${devices.length})</h3>${devices.length ? this._deviceList(devices) : '<div class="empty">None in view.</div>'}</div>`;
    return html;
  }

  // What the firewall turned away, from the router's log.
  _firewallHtml(inf) {
    const fw = this.graph.network?.firewall;
    if (!fw && inf.blockedIn == null) {
      return '<div class="d-sec"><h3>Firewall</h3><div class="empty">The router\'s firewall log is not connected, so blocked connection attempts are not shown.</div></div>';
    }
    const win = fw ? `last ${fmtDur(fw.windowSeconds || 3600)}` : 'recently';
    const blockedIn = fw?.blockedIn ?? inf.blockedIn ?? 0, sources = fw?.blockedInSources ?? inf.blockedInSources;
    const bad = fw?.blockedInKnownBad ?? inf.blockedInKnownBad, out = fw?.blockedOut ?? inf.blockedOut ?? 0;
    let html = `<div class="d-sec"><h3>Firewall · ${esc(win)}</h3><dl class="kv">
      <dt>Turned away</dt><dd>${fmtNum(blockedIn)} attempt${blockedIn === 1 ? '' : 's'} from outside${sources != null ? ` <span class="dim">· ${plural(sources, 'source')}</span>` : ''}</dd>
      ${bad != null ? `<dt>Known-bad</dt><dd>${fmtNum(bad)} of them${fw?.knownBadSources != null ? ` <span class="dim">· ${plural(fw.knownBadSources, 'listed address', 'listed addresses')}</span>` : ''}</dd>` : ''}
      <dt>Blocked going out</dt><dd>${out ? `<span class="w">${fmtNum(out)}</span> <span class="dim">· a device tried to reach something the rules forbid</span>` : 'none'}</dd>
    </dl></div>`;
    if (!fw) return html;
    const rows = (list, cell) => list.map(cell).join('');
    const src = (fw.topSources || []).slice(0, 8), ports = (fw.topPorts || []).slice(0, 8);
    if (src.length) {
      html += `<div class="d-sec"><h3>Who was turned away most</h3><table class="mini">${rows(src, (s) => `<tr><td class="mono">${esc(s.ip)}</td>
        <td>${s.knownBad ? '<span class="tag bad">known-bad</span>' : ''}</td><td class="num">${fmtNum(s.count)}×</td></tr>`)}</table></div>`;
    }
    if (ports.length) {
      html += `<div class="d-sec"><h3>Ports they tried</h3><table class="mini">${rows(ports, (p) => `<tr><td class="mono">${esc(p.port)}</td><td></td><td class="num">${fmtNum(p.count)}×</td></tr>`)}</table></div>`;
    }
    const scan = fw.scanners || [];
    if (scan.length) {
      html += `<div class="d-sec"><h3>Port scans</h3><ul class="details">${scan.slice(0, 8).map((s) => `<li><span class="mono">${esc(s.ip)}</span> tried ${plural(s.ports, 'different port')}</li>`).join('')}</ul></div>`;
    }
    const pairs = fw.blockedOutPairs || [];
    if (pairs.length) {
      html += `<div class="d-sec"><h3>Blocked going out</h3><ul class="details">${pairs.slice(0, 10).map((p) => {
        if (typeof p === 'string') return `<li>${esc(p)}</li>`;
        const from = p.name || p.device || p.src || p.source || '?', to = p.dst || p.dest || p.target || p.ip || '?';
        return `<li>${esc(from)} → <span class="mono">${esc(to)}${p.port ? ':' + esc(p.port) : ''}</span>${p.count ? ` <span class="dim">· ${fmtNum(p.count)}×</span>` : ''}</li>`;
      }).join('')}</ul></div>`;
    }
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
      <div class="d-sub">${plural(k.workloads || 0, 'device')}${info ? ` · ${fmtNum(info.online ?? 0)} online${info.offline ? `, ${fmtNum(info.offline)} offline` : ''}` : ''}</div>
      ${this._badge(n.risk?.level || 'ok', k.failing ? `${k.failing} need${k.failing === 1 ? 's' : ''} attention` : 'Nothing wrong')}
      <div class="d-sec"><div class="agg-bar">${bar(k.healthy, 'var(--ok)')}${bar(k.degraded, 'var(--warn)')}${bar(k.down, 'var(--crit)')}</div>
        ${info ? `<div class="d-sub">↓ ${esc(fmtBits(info.downBps || 0))} · ↑ ${esc(fmtBits(info.upBps || 0))} right now</div>` : ''}
        <div style="margin-top:9px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn small" data-expand="${esc(n.expand)}">Open on the map ▸</button>
          <button class="btn small" data-scope="${esc(n.project || n.namespace)}">Show only this group</button></div>
        <div class="d-sub" style="margin-top:6px">Double-clicking it on the map opens it too.</div></div>
      ${fs.length ? `<div class="d-sec"><h3>Needs attention inside</h3>${fs.map((f) => `<div class="d-find ${f.severity}" data-node="${esc(f.subject)}" style="cursor:pointer">
        <div class="cat">${esc(this.catTitle(f.category))}</div><div class="t">${esc(f.title)}</div>
        ${f.fix ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : ''}</div>`).join('')}</div>` : ''}
      <div class="d-sec"><h3>Devices (${n.childrenTotal ?? children.length})</h3>
        <ul class="peers">${children.map((c) => `<li ${c.expand ? `data-expand="${esc(c.expand)}"` : `data-node="${esc(c.id)}"`}><div class="p1">
          <span class="st" style="background:${LEVEL_COLOR[c.level] || LEVEL_COLOR.ok}"></span><span class="nm">${esc(c.name)}</span>
          <span class="where">${esc(c.kind || '')}</span></div>
          ${c.issue ? `<div class="p2 issue ${c.level}">${esc(c.issue)}</div>` : ''}</li>`).join('')}</ul>
        ${(n.childrenTotal || 0) > children.length ? `<div class="d-sub" style="margin-top:6px">+${n.childrenTotal - children.length} more: open the group, or use the Devices table in Triage.</div>` : ''}</div>`;
  }

  // ---- a connection, or a port forward
  _edgeHtml(e, byId) {
    const s = byId.get(e.source), t = byId.get(e.target);
    const T = e.traffic || {}, net = e.net || {};
    const forward = e.type === 'exposes';
    const seen = (e.evidence || []).includes('observed');
    const lvl = e.risk?.level || 'ok';
    const rate = edgeRate(e);
    if (e.blocked) {
      const side = (id, n, word) => `<li data-node="${esc(id)}"><div class="p1"><span class="arrow">${word}</span><span class="nm">${esc(this.nodeName(n) || id)}</span></div></li>`;
      return `<div class="d-kind">Stopped</div><div class="d-title">${esc(this.nodeName(s) || e.source)} → ${esc(this.nodeName(t) || e.target)}</div>
        <div class="d-sub">Stopped at ${esc(this.nodeName(t) || 'a defence')}. This line counts what was stopped; none of it got through.</div>
        <div class="d-sec"><h3>What was stopped</h3><dl class="kv"><dt>Count</dt><dd>${fmtNum(e.stopped || 0)}</dd></dl>
        ${(e.details || []).length ? `<ul class="details" style="margin-top:8px">${e.details.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>
        <div class="d-sec"><ul class="peers">${side(e.source, s, 'from')}${side(e.target, t, 'stopped at')}</ul></div>`;
    }
    let html = `<div class="d-kind">${forward ? 'Port forward' : 'Connection'}</div>
      <div class="d-title">${esc(this.nodeName(s) || e.source)} → ${esc(this.nodeName(t) || e.target)}</div>
      <div class="d-sub">${forward ? (seen ? 'Open to the internet, and in use right now.' : 'Open to the internet. Nobody is using it right now.')
        : t?.kind === 'external' ? 'A device on your network talking to a service on the internet.' : 'Two devices on your network talking to each other.'}</div>`;
    if (lvl !== 'ok') html += this._badge(lvl, lvl === 'warning' ? 'Worth a look' : 'Needs attention');
    html += this._diagnosis(e.id);
    html += `<div class="d-sec"><h3>Traffic</h3><dl class="kv">
      <dt>Rate now</dt><dd>${esc(rate || (seen ? 'quiet' : 'no traffic seen'))}</dd>
      ${T.bytes != null ? `<dt>So far</dt><dd>${esc(fmtBytes(T.bytes))}</dd>` : ''}
      <dt>Open connections</dt><dd>${fmtNum(T.connections ?? 0)}</dd>
      ${T.lastActiveS != null ? `<dt>Last active</dt><dd>${T.lastActiveS < 5 ? 'just now' : esc(fmtAgo(T.lastActiveS))}${T.idle ? ' <span class="dim">· open but quiet</span>' : ''}</dd>` : ''}
      ${(e.ports || []).length ? `<dt>Ports</dt><dd class="mono">${esc(e.ports.join(', '))}</dd>` : ''}
      ${(net.apps || []).length ? `<dt>Apps</dt><dd>${esc(net.apps.join(', '))}</dd>` : ''}
      ${(net.names || []).length ? `<dt>Server names</dt><dd class="mono">${net.names.slice(0, 10).map(esc).join('<br>')}${net.names.length > 10 ? `<br>+${net.names.length - 10} more` : ''}</dd>` : ''}
    </dl></div>`;
    if (forward) {
      html += `<div class="d-sec"><h3>The forward</h3><dl class="kv">
        ${(e.via || []).length ? `<dt>Rule</dt><dd>${esc(e.via.join(', '))}</dd>` : ''}
        ${(e.ports || []).length ? `<dt>Internet port</dt><dd class="mono">${esc(e.ports.join(', '))}</dd>` : ''}
        ${t?.device?.ip ? `<dt>Goes to</dt><dd><span class="mono">${esc(t.device.ip)}</span> (${esc(t.name)})</dd>` : ''}</dl>
        ${(e.details || []).length ? `<ul class="details" style="margin-top:8px">${e.details.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>`;
    } else if ((e.details || []).length) {
      html += `<div class="d-sec"><h3>Details</h3><ul class="details">${e.details.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>`;
    }
    const end = (id, n, word) => `<li data-node="${esc(id)}"><div class="p1"><span class="arrow">${word}</span><span class="nm">${esc(this.nodeName(n) || id)}</span>
      <span class="where">${esc(!n ? '' : n.kind === 'workload' ? [deviceType(n), n.device?.ip].filter(Boolean).join(' · ') : n.kind === 'external' ? 'internet service' : kindWord(n.subkind))}</span></div></li>`;
    html += `<div class="d-sec"><ul class="peers">${end(e.source, s, 'from')}${end(e.target, t, 'to')}</ul></div>`;
    return html;
  }
}

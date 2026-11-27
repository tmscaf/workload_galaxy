// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// The Triage view: one screen that answers "is anything on my network worth worrying about, and
// what do I do about it".
//
//   verdict + sources     how many things need attention; is the data arriving?
//   network               the numbers: who is online, how much is moving, what was turned away
//   groups                one tile per device group
//   Needs attention       findings, by category, each with why and what to do
//   Devices / Connections the in-depth tables (filtered, sorted and paged by the collector)
//   Recently joined       devices that appeared in the last day
//
// It renders the same graph the map uses (graph.findings / network / namespaces / changes).
import { esc, fmtAgo, fmtBits, fmtDur, fmtNum, LEVEL_COLOR, nsColor, optionsHtml, plural } from './format.js';
import { UI } from './ui.js';
import { keepFocus } from './actions.js';

const SEV = { critical: 0, warning: 1, info: 2 };
const SEV_COLOR = { critical: 'var(--crit)', warning: 'var(--warn)', info: 'var(--muted)', ok: 'var(--ok)' };
const TEXT_SORT = new Set(['name', 'ns', 'type', 'ip', 'from', 'to']);

export class Triage {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;     // { onSelect(sel), onShowOnMap(sel), onGroup(id), scope(), fetchTable(kind, opts), fetchPattern(key), copy(text) }
    this.tab = 'problems';
    this.category = null;
    this.showInfo = true;         // open ports and blocked attempts are worth seeing even when nothing is wrong
    this.query = '';
    this.open = new Set();        // <details> the user opened; survive live refreshes
    this.shown = 30;              // cards rendered; "Show more" adds a page
    this.pattern = null;          // a repeated finding opened to list every device it is on
    this.remote = {};             // server-paged tables: kind -> {key, rows, total, ...}
    this.onlyProblems = false;
    this.sort = { workloads: ['total', -1], calls: ['bps', -1] };
    this.actions = ctx.actions;
    el.innerHTML = `<div class="tg-head"></div><section class="tg-blocks"></section><div class="tg-net"></div><div class="tg-projects"></div>
      <div class="tg-body"><div class="tg-main"><div class="tg-tabs"></div><div class="tg-content"></div></div>
      <aside class="tg-side"><h2>Recently joined <span class="hint">last day, newest first</span></h2><ol class="tg-changes"></ol></aside></div>`;
    el.addEventListener('click', (ev) => this._click(ev));
    el.addEventListener('toggle', (ev) => {
      const key = ev.target.dataset?.key;
      if (key) { if (ev.target.open) this.open.add(key); else this.open.delete(key); }
    }, true);
    el.addEventListener('input', (ev) => {
      if (ev.target.id === 'tgSearch') {
        this.query = ev.target.value.trim().toLowerCase();
        clearTimeout(this._qT);
        this._qT = setTimeout(() => this._content(), this.tab === 'problems' ? 0 : 200);
      }
    });
  }

  render(graph, meta) {
    if (!graph) return;
    this.graph = graph;
    this.meta = meta;
    for (const k of Object.keys(this.remote)) this.remote[k].stale = true;
    this.byId = new Map((graph.nodes || []).map((n) => [n.id, n]));
    this.edgeById = new Map((graph.edges || []).map((e) => [e.id, e]));
    this.titles = new Map((graph.namespaces || []).map((g) => [g.name, g.title || g.name]));
    for (const g of meta?.groups || []) if (!this.titles.has(g.id)) this.titles.set(g.id, g.title || g.id);
    this.cats = (meta?.categories || []).slice();
    this._groups();
    this._head();
    this._blocks();
    this._network();
    this._scorecards();
    this._tabs();
    this._content();
    this._changes();
  }

  groupTitle(id) { return this.titles?.get(id) || id || ''; }

  catTitle(id) { return (this.cats || []).find((c) => c.id === id)?.title || id || ''; }

  // Badge for the tab in the top bar: how many things need attention.
  static badge(graph) {
    const roots = (graph?.findings || []).filter((f) => f.root && !f.effectOf && f.severity !== 'info');
    const s = graph?.summary;
    const count = s ? s.problems : new Set(roots.map((f) => f.subject || f.id)).size;      // one per thing that is wrong
    if (s) return { count, level: s.problemsCritical ? 'critical' : count ? 'warning' : 'ok' };
    return { count, level: roots.some((f) => f.severity === 'critical') ? 'critical' : roots.length ? 'warning' : 'ok' };
  }

  // ------------------------------------------------------------------ model
  // One card per subject per category: the findings of that kind about one device, connection or the router.
  _groups() {
    const all = this.graph.findings || [];
    const groups = new Map();
    for (const f of all) {
      const k = `${f.category}|${f.subject || f.id}`;
      if (!groups.has(k)) groups.set(k, { key: k, subject: f.subject, category: f.category, findings: [] });
      groups.get(k).findings.push(f);
    }
    this.groups = [...groups.values()].map((g) => {
      g.findings.sort((a, b) => SEV[a.severity] - SEV[b.severity] || (b.weight || 0) - (a.weight || 0));
      const real = g.findings.filter((f) => f.severity !== 'info');
      g.severity = real.length ? (real.some((f) => f.severity === 'critical') ? 'critical' : 'warning') : 'info';
      g.head = g.findings[0];
      return g;
    }).sort((a, b) => SEV[a.severity] - SEV[b.severity] || (b.head.weight || 0) - (a.head.weight || 0)
      || String(a.head.subjectName || '').localeCompare(String(b.head.subjectName || '')));
  }

  // ------------------------------------------------------------------ header
  _head() {
    const g = this.graph;
    // Totals come from the collector: it counts every finding in scope, not just the ones on this page.
    const S = g.summary || {};
    const real = this.groups.filter((x) => x.severity !== 'info');
    const total = S.problems ?? new Set(real.map((x) => x.subject)).size;
    const crit = S.problemsCritical ?? new Set(real.filter((x) => x.severity === 'critical').map((x) => x.subject)).size;
    const warn = Math.max(0, total - crit), nGroups = S.projects ?? 0;
    const infoN = S.info ?? this.groups.filter((x) => x.severity === 'info').length;
    const level = crit ? 'critical' : warn ? 'warning' : 'ok';
    const verdict = !total ? 'Nothing needs attention' : `${fmtNum(total)} thing${total === 1 ? '' : 's'} to look at`;
    const sub = !total ? `Every device in view is behaving as usual${infoN ? `. ${plural(infoN, 'note')} below, for information` : ''}.`
      : [crit ? `<b class="c">${fmtNum(crit)} critical</b>` : '', warn ? `<b class="w">${fmtNum(warn)} warning${warn === 1 ? '' : 's'}</b>` : '',
        nGroups ? `in ${plural(nGroups, 'group')}` : '', infoN ? `${plural(infoN, 'note')} for information` : ''].filter(Boolean).join(' · ');
    // The top bar already shows the sources; here only the ones that are not delivering (they limit what can be found).
    const chips = UI.sourceChips(g.sources || this.meta?.sources || {}, null).filter((c) => c.cls !== 'ok')
      .map((c) => `<span class="chip ${c.cls}" title="${esc(c.title)}">${c.mark} ${esc(c.text)}</span>`);
    const inv = (g.sources || this.meta?.sources || {}).inventory;
    if (inv?.learning) chips.push('<span class="chip subtle" title="The dashboard has not watched this network for long. Its idea of what is usual for each device gets better over the first days.">◌ still learning what is normal</span>');
    this.el.querySelector('.tg-head').innerHTML = `
      <div class="tg-verdict ${level}"><span class="orb"></span><div><div class="v1">${verdict}</div><div class="v2">${sub}</div></div></div>
      <div class="tg-plat">${chips.join('')}</div>`;
  }

  // ------------------------------------------------------------------ blocked addresses
  _blocks() {
    const box = this.el.querySelector('.tg-blocks');
    const html = this.actions ? this.actions.sectionHtml() : '';
    box.hidden = !html;
    box.innerHTML = html;
    const det = box.querySelector('details[data-key]');
    if (det && this.open.has(det.dataset.key)) det.open = true;
  }

  // Block state changed (or a confirm panel opened): redraw only what shows it.
  refreshActions() {
    if (!this.graph) return;
    keepFocus(() => {
      this._blocks();
      if (this.tab === 'problems') this._content();
    });
  }

  // ------------------------------------------------------------------ network summary
  _network() {
    const box = this.el.querySelector('.tg-net');
    const net = this.graph.network;
    box.hidden = !net;
    if (!net) { box.innerHTML = ''; return; }
    const fw = net.firewall;
    const tile = (v, k, sub = '', attr = '') => `<div class="tg-stat" ${attr}><div class="v">${v}</div><div class="k">${k}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
    const tiles = [
      tile(`${fmtNum(net.online ?? 0)}<small> of ${fmtNum(net.devices ?? 0)}</small>`, 'devices online', net.offline ? `${fmtNum(net.offline)} offline` : 'none offline'),
      tile(`<span class="ar">↓</span>${esc(fmtBits(net.downloadBps ?? 0))}`, 'download', 'from the internet'),
      tile(`<span class="ar">↑</span>${esc(fmtBits(net.uploadBps ?? 0))}`, 'upload', 'to the internet'),
      tile(fmtNum(net.flows ?? 0), 'active connections', 'through the router'),
      tile(fmtNum(net.services ?? 0), 'internet services in use', `${fmtNum(net.remoteHosts ?? 0)} outside addresses`),
      tile(fmtNum(net.forwards ?? 0), `port forward${net.forwards === 1 ? '' : 's'}`, 'open to the internet'),
    ];
    if (fw) {
      tiles.push(tile(fmtNum(fw.blockedIn ?? 0), 'attempts turned away', `last ${esc(fmtDur(fw.windowSeconds || 3600))}${fw.blockedInKnownBad ? ` · ${fmtNum(fw.blockedInKnownBad)} known-bad` : ''}`,
        'data-node="host:router" role="button" title="Details on the router"'));
      if (fw.blockedOut) tiles.push(tile(`<span class="w">${fmtNum(fw.blockedOut)}</span>`, 'blocked going out', 'devices stopped by a rule', 'data-node="host:router" role="button"'));
    }
    const talkers = (net.topTalkers || []).slice(0, 5), services = (net.topServices || []).slice(0, 5);
    const tMax = Math.max(1, ...talkers.map((t) => (t.downBps || 0) + (t.upBps || 0)));
    const sMax = Math.max(1, ...services.map((s) => s.bps || 0));
    const pct = (v, max) => Math.max(2, Math.round((v / max) * 100));
    const scoped = !!(this.ctx.scope ? this.ctx.scope() : {}).namespaces?.length;
    box.innerHTML = `<div class="tg-stats">${tiles.join('')}${scoped ? '<div class="tg-scope">These numbers cover the whole network, not only the groups picked.</div>' : ''}</div>
      <div class="tg-tops">
        <div class="tg-top"><h2>Busiest devices <span class="hint">right now</span></h2>${talkers.length ? `<ul class="bars">${talkers.map((t) => `<li data-node="${esc(t.id)}">
          <span class="nm">${esc(t.name)}</span><span class="val">↓ ${esc(fmtBits(t.downBps || 0))} · ↑ ${esc(fmtBits(t.upBps || 0))}</span>
          <div class="meter"><i style="width:${pct((t.downBps || 0) + (t.upBps || 0), tMax)}%"></i></div></li>`).join('')}</ul>` : '<div class="empty">Nothing is moving much.</div>'}</div>
        <div class="tg-top"><h2>Busiest internet services <span class="hint">right now</span></h2>${services.length ? `<ul class="bars">${services.map((s) => `<li data-node="${esc(s.id)}">
          <span class="nm">${esc(s.name)}</span><span class="val">${esc(fmtBits(s.bps || 0))}</span>
          <div class="meter gold"><i style="width:${pct(s.bps || 0, sMax)}%"></i></div></li>`).join('')}</ul>` : '<div class="empty">Nothing is reaching out to the internet.</div>'}</div>
      </div>`;
  }

  // ------------------------------------------------------------------ group scorecards
  _scorecards() {
    const scope = this.ctx.scope ? this.ctx.scope() : {};
    const rows = (this.graph.namespaces || []).filter((p) => (scope.namespaces?.length ? scope.namespaces.includes(p.name) : true) && p.workloads);
    const fc = (p) => p.findings || {};
    const rank = (p) => (fc(p).critical || p.critical ? 0 : fc(p).warning || p.warning ? 1 : 2);
    rows.sort((a, b) => rank(a) - rank(b) || (b.downBps || 0) + (b.upBps || 0) - (a.downBps || 0) - (a.upBps || 0) || String(a.title || a.name).localeCompare(String(b.title || b.name)));
    const scoped = !!scope.namespaces?.length;
    const tile = (p) => {
      const lvl = ['critical', 'warning', 'ok'][rank(p)];
      const bar = (n, c) => (n > 0 ? `<i style="flex:${n};background:${c}"></i>` : '');
      const off = p.offline || 0, crit = p.critical || 0, warn = p.warning || 0;
      const fine = Math.max(0, (p.workloads || 0) - off - crit - warn);
      const online = p.online ?? Math.max(0, (p.workloads || 0) - off);
      return `<button class="tg-proj ${lvl}" data-group="${esc(p.name)}" title="Show only ${esc(p.title || p.name)}">
        <div class="p1"><span class="sw" style="background:${nsColor(p.name)}"></span><span class="nm">${esc(p.title || p.name)}</span>
          <span class="cnt">${plural(p.workloads || 0, 'device')}</span></div>
        <div class="bar" title="${fine} fine · ${warn} warning · ${crit} critical · ${off} offline">${bar(fine, 'var(--ok)')}${bar(warn, 'var(--warn)')}${bar(crit, 'var(--crit)')}${bar(off, 'var(--faint)')}</div>
        <div class="p2">${fmtNum(online)} online${off ? ` · ${fmtNum(off)} offline` : ''} · ↓ ${esc(fmtBits(p.downBps || 0))} · ↑ ${esc(fmtBits(p.upBps || 0))}</div>
        <div class="p3 ${p.top ? p.top.severity : ''}">${p.top ? esc(p.top.title) : 'Nothing to report'}</div></button>`;
    };
    const box = this.el.querySelector('.tg-projects');
    box.className = 'tg-projects';
    box.innerHTML = (rows.map(tile).join('') || '<div class="empty">No groups in view.</div>')
      + (scoped ? '<button class="tg-proj all" data-group="" title="Back to every group"><div class="p1"><span class="nm">Show all groups ▸</span></div><div class="p2">You are looking at part of the network.</div></button>' : '');
  }

  // ------------------------------------------------------------------ tabs
  _tabs() {
    const real = this.groups.filter((g) => g.severity !== 'info');
    const S = this.graph.summary;
    const devices = this.remote.workloads?.scope ?? this.graph.view?.workloads ?? this.graph.network?.devices ?? '';
    const conns = this.remote.calls?.scope ?? (this.graph.view?.collapsed ? '' : (this.graph.edges || []).filter((e) => e.type !== 'exposes').length);
    const counts = new Map();
    for (const g of (this.showInfo ? this.groups : real)) counts.set(g.category, (counts.get(g.category) || 0) + 1);
    const order = this._catOrder().filter((c) => counts.has(c));
    const tab = (id, label, n) => `<button class="${this.tab === id ? 'on' : ''}" data-tab="${id}">${label} <span>${n}</span></button>`;
    const had = document.activeElement?.id === 'tgSearch';
    this.el.querySelector('.tg-tabs').innerHTML = `
      <div class="seg">${tab('problems', 'Needs attention', S ? fmtNum(S.problems) : new Set(real.map((g) => g.subject)).size)}${tab('workloads', 'Devices', devices === '' ? '' : fmtNum(devices))}${tab('calls', 'Connections', conns === '' ? '' : fmtNum(conns))}</div>
      ${this.tab !== 'problems' ? `<label class="tg-info" title="Hide rows with nothing to report"><input type="checkbox" id="tgOnly" ${this.onlyProblems ? 'checked' : ''}> Only with an issue</label>` : ''}
      ${this.tab === 'problems' ? `<div class="cats"><button class="${!this.category ? 'on' : ''}" data-cat="">All</button>
        ${order.map((c) => `<button class="${this.category === c ? 'on' : ''}" data-cat="${esc(c)}">${esc(this.catTitle(c))} <span>${counts.get(c)}</span></button>`).join('')}</div>
        <label class="tg-info" title="Things that are not a problem but are worth knowing: open ports, blocked attempts, unidentified devices"><input type="checkbox" id="tgInfo" ${this.showInfo ? 'checked' : ''}> Include notes</label>` : ''}
      <input id="tgSearch" type="search" placeholder="Filter…" value="${esc(this.query)}" autocomplete="off">`;
    if (had) { const s = this.el.querySelector('#tgSearch'); s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
  }

  // Categories in the collector's order, then any it did not announce.
  _catOrder() {
    const order = (this.cats || []).map((c) => c.id);
    for (const g of this.groups) if (!order.includes(g.category)) order.push(g.category);
    return order;
  }

  _content() {
    const box = this.el.querySelector('.tg-content');
    box.innerHTML = this.tab === 'workloads' ? this._table('workloads') : this.tab === 'calls' ? this._table('calls') : this._problems();
  }

  // ------------------------------------------------------------------ findings
  _problems() {
    const q = this.query;
    if (this.pattern) return this._patternMembers();
    const match = (text) => !q || text.toLowerCase().includes(q);
    const patterns = (this.graph.patterns || []).filter((p) => (this.showInfo || p.severity !== 'info')
      && (!this.category || p.category === this.category) && match(`${p.label} ${p.cause}`));
    const list = this.groups.filter((g) => (this.showInfo || g.severity !== 'info')
      && (!this.category || g.category === this.category)
      && match([g.head.subjectName, this.groupTitle(g.head.namespace), ...g.findings.map((f) => `${f.title} ${f.cause} ${f.detail}`)].join(' ')));
    if (!list.length && !patterns.length) {
      return `<div class="tg-clear">${(this.graph.findings || []).length ? 'Nothing matches this filter.'
        : '<b>Nothing needs attention.</b> No device is talking to a known-bad address, nothing risky is open to the internet, and nobody is behaving unusually.'}</div>`;
    }
    const shown = list.slice(0, this.shown);
    const more = list.length - shown.length;
    const hidden = (this.graph.findingsMatching || 0) - (this.graph.findings || []).length;
    let html = patterns.length ? `<h3 class="tg-h">Seen on several devices <span class="hint">the same finding more than once, shown once</span></h3>${patterns.map((p) => this._pattern(p)).join('')}` : '';
    for (const cat of this._catOrder()) {
      const cards = shown.filter((g) => g.category === cat);
      if (!cards.length) continue;
      const worst = cards.some((g) => g.severity === 'critical') ? 'critical' : cards.some((g) => g.severity === 'warning') ? 'warning' : 'info';
      html += `<h3 class="tg-h cat ${worst}"><span class="sev" style="background:${SEV_COLOR[worst]}"></span>${esc(this.catTitle(cat))} <span class="hint">${cards.length}</span></h3>`
        + cards.map((g) => this._card(g)).join('');
    }
    return html
      + (more > 0 ? `<button class="btn tg-more" data-more="1">Show ${Math.min(30, more)} more of ${more}</button>` : '')
      + (hidden > 0 ? `<div class="tg-note">Showing the ${this.graph.findings.length} most serious of ${fmtNum(this.graph.findingsMatching)} findings. Pick a group to see the rest.</div>` : '');
  }

  // One finding, many subjects: "6 devices: admin services open to the internet".
  _pattern(p) {
    const noun = { workload: 'device', call: 'connection', host: 'host', external: 'internet service' }[p.kind] || 'thing';
    const units = (p.units || []).map((u) => `${esc(this.groupTitle(u.name))} ${u.count}`).join(' · ');
    return `<article class="tg-card tg-pat ${p.severity}">
      <header><span class="sev" style="background:${SEV_COLOR[p.severity]}"></span>
        <span class="n">${fmtNum(p.count)}</span><span class="nm2">${noun}${p.count === 1 ? '' : 's'}${p.projects ? ` in ${plural(p.projects, 'group')}` : ''}: ${esc(p.label)}</span>
        <button class="link map" data-pattern="${esc(p.key)}">Show all ${fmtNum(p.count)} ▸</button></header>
      <section class="tg-f ${p.severity}"><div class="t"><span class="cat">${esc(this.catTitle(p.category))}</span>
        <span class="title sub">${units ? `Most in: ${units}` : ''}</span></div>
        ${p.cause ? `<p class="why"><b>Why</b>${esc(p.cause)}</p>` : ''}${p.fix ? `<p class="fix"><b>Next</b>${esc(p.fix)}</p>` : ''}
        <div class="pods ex"><span>e.g.</span>${(p.examples || []).map((x) => `<a ${String(x.subject).includes('->') ? 'data-edge' : 'data-node'}="${esc(x.subject)}" title="${esc(x.title)}">${esc(x.subjectName)}${x.namespace ? `<i>${esc(this.groupTitle(x.namespace))}</i>` : ''}</a>`).join('')}</div>
      </section></article>`;
  }

  _patternMembers() {
    const st = this.patternData;
    const p = (this.graph.patterns || []).find((x) => x.key === this.pattern);
    const head = `<div class="tg-back"><button class="link" data-pattern="">← Everything</button>
      <b>${p ? `${fmtNum(p.count)} × ${esc(p.label)}` : 'This is no longer happening.'}</b></div>`;
    if (!st || st.key !== this.pattern || st.stale) {
      if (!st || st.key !== this.pattern) this.patternData = { key: this.pattern, loading: true, groups: [] };
      else st.stale = false;
      this.ctx.fetchPattern(this.pattern).then((res) => {
        if (this.pattern !== res.key) return;
        const keep = this.graph.findings;
        this.graph.findings = res.findings || [];
        this._groups();
        this.patternData = { key: res.key, groups: this.groups, total: res.findingsMatching };
        this.graph.findings = keep;
        this._groups();
        this._content();
      }).catch(() => {});
      if (!st || st.key !== this.pattern) return head + '<div class="tg-clear">Loading…</div>';
    }
    const groups = (this.patternData.groups || []).filter((g) => !this.query
      || `${g.head.subjectName} ${this.groupTitle(g.head.namespace)}`.toLowerCase().includes(this.query));
    const more = groups.length - this.shown;
    return head + groups.slice(0, this.shown).map((g) => this._card(g)).join('')
      + (more > 0 ? `<button class="btn tg-more" data-more="1">Show ${Math.min(30, more)} more of ${more}</button>` : '');
  }

  _card(g) {
    const h = g.head;
    const node = this.byId.get(g.subject);
    const edge = this.edgeById.get(g.subject);
    const isEdge = !!edge || (!node && (h.subjectKind === 'call' || String(g.subject || '').includes('->')));
    const sel = !g.subject ? '' : isEdge ? `data-edge="${esc(g.subject)}"` : `data-node="${esc(g.subject)}"`;
    const kind = node ? (node.kind === 'workload' ? (node.device?.typeLabel || node.subkind) : node.kind === 'external' ? 'Internet service' : node.subkind)
      : isEdge ? (edge?.type === 'exposes' ? 'Port forward' : 'Connection')
      : { workload: 'Device', host: 'Host', external: 'Internet service' }[h.subjectKind] || '';
    const name = h.subjectName || this.groupTitle(h.namespace) || 'The network';
    const offline = node?.kind === 'workload' && node.device?.present === false;
    return `<article class="tg-card ${g.severity}">
      <header><span class="sev" style="background:${SEV_COLOR[g.severity]}"></span>
        <a class="nm" ${sel} title="Open details">${esc(name)}</a><span class="kind">${esc(kind || '')}</span>
        ${h.namespace ? `<span class="ns" style="--c:${nsColor(h.namespace)}">${esc(this.groupTitle(h.namespace))}</span>` : ''}
        ${offline ? '<span class="tag">offline now</span>' : ''}
        ${sel ? `<button class="link map" ${sel} data-map="1" title="Show it on the map">Show on map ▸</button>` : ''}</header>
      ${g.findings.filter((f) => this.showInfo || f.severity !== 'info' || g.severity === 'info').map((f) => this._finding(f)).join('')}</article>`;
  }

  _finding(f) {
    const key = `f:${f.id}`;
    const evidence = (f.evidence || []).filter((e) => !/:\s*$/.test(e));
    return `<section class="tg-f ${f.severity}">
      <div class="t"><span class="title">${esc(f.title)}</span>
        ${f.sinceS != null ? `<span class="since">${esc(fmtAgo(f.sinceS))}</span>` : ''}</div>
      ${f.detail ? `<p class="det">${esc(f.detail)}</p>` : ''}
      ${f.cause ? `<p class="why"><b>Why</b>${esc(f.cause)}</p>` : ''}
      ${f.fix ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : ''}
      ${this.actions && (f.block || []).length ? this.actions.renderControl({ key: `f:${f.id}`, targets: f.block, reason: f.title, finding: f.id, label: f.subjectName || '' }) : ''}
      ${f.options?.length ? optionsHtml(f) : ''}
      ${evidence.length ? `<details data-key="${esc(key)}" ${this.open.has(key) ? 'open' : ''}><summary>What this is based on</summary>
        ${evidence.map((e) => `<pre class="ev-msg">${esc(e)}</pre>`).join('')}</details>` : ''}</section>`;
  }

  // ------------------------------------------------------------------ tables
  // The collector filters, sorts and pages; this keeps what it has while a refresh is on its way.
  _table(kind) {
    const only = this.onlyProblems;
    const [col, dir] = this.sort[kind];
    const key = JSON.stringify([col, dir, this.query, only, this.ctx.scope()]);
    let st = this.remote[kind];
    if (!st || st.key !== key || st.stale) {
      const fresh = !st || st.key !== key;
      if (fresh) st = this.remote[kind] = { key, rows: st?.rows || [], total: st?.total || 0, scope: st?.scope, loading: true, first: !st };
      st.stale = false;
      this._fetchTable(kind, key, 0, fresh ? 100 : Math.max(100, st.rows.length), true);
    }
    const body = kind === 'workloads' ? this._deviceTable(st.rows) : this._connTable(st.rows);
    const left = st.total - st.rows.length;
    const noun = kind === 'workloads' ? 'devices' : 'connections';
    const count = st.error ? `Could not load the ${noun}: ${esc(st.error)}`
      : st.loading && st.first ? 'Loading…'
      : `${fmtNum(st.total)} ${only ? 'with an issue' : this.query ? 'matching' : 'shown'} of ${fmtNum(st.scope ?? st.total)} ${noun}`;
    return `<div class="tg-count">${count}</div>${body}
      ${!st.loading && !st.rows.length && !st.error ? `<div class="tg-clear">${only ? `No ${noun} with an issue.` : this.query ? 'Nothing matches this filter.' : `No ${noun} to show.`}</div>` : ''}
      ${left > 0 ? `<button class="btn tg-more" data-page="${kind}">Show 100 more of ${fmtNum(left)}</button>` : ''}`;
  }

  _fetchTable(kind, key, offset, limit, replace) {
    const [col, dir] = this.sort[kind];
    this.ctx.fetchTable(kind, { sort: col, desc: dir < 0, q: this.query, problems: this.onlyProblems, offset, limit }).then((res) => {
      const st = this.remote[kind];
      if (!st || st.key !== key) return;
      st.rows = replace ? (res.rows || []) : st.rows.concat(res.rows || []);
      Object.assign(st, { total: res.total ?? st.rows.length, scope: res.scope, loading: false, first: false, error: null });
      if (this.tab === kind) { this._tabs(); this._content(); }
    }).catch((err) => {
      const st = this.remote[kind];
      if (!st || st.key !== key) return;
      Object.assign(st, { loading: false, first: false, error: err.message });
      if (this.tab === kind) this._content();
    });
  }

  _th(table, id, label, title = '', num = false) {
    const [col, dir] = this.sort[table];
    return `<th data-sort="${table}:${id}" class="${col === id ? 'on' : ''}${num ? ' num' : ''}" title="${esc(title)}">${label}${col === id ? (dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
  }

  _deviceTable(rows) {
    const th = (...a) => this._th('workloads', ...a);
    const rate = (v) => `<td class="num ${v ? '' : 'dim'}">${v ? esc(fmtBits(v)) : '–'}</td>`;
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by how serious the issue is')}${th('name', 'Device')}${th('type', 'Type')}${th('ns', 'Group')}
      ${th('ip', 'Address')}${th('seen', 'Status', 'Online, or how long ago it was last seen')}
      ${th('down', 'Download', 'Coming in to the device right now', true)}${th('up', 'Upload', 'Going out from the device right now', true)}
      ${th('flows', 'Connections', 'Connections it has open right now', true)}${th('risk', 'Issue', 'The most serious finding about it')}</tr></thead><tbody>
      ${rows.map((r) => {
        const off = r.present === false;
        const lvl = off && r.level === 'ok' ? 'offline' : r.level;
        const issueLvl = r.issueLvl && r.issueLvl !== 'info' ? r.issueLvl : 'ok';
        return `<tr data-node="${esc(r.id)}" class="${off ? 'off' : ''}"><td><span class="dot ${esc(lvl || 'ok')}" title="${off ? 'offline' : esc(r.level || 'ok')}"></span></td>
          <td class="nm">${esc(r.name)}${r.vendor ? `<span class="k">${esc(r.vendor)}</span>` : ''}</td>
          <td class="nw">${esc(r.type || r.kind || '')}</td>
          <td><span class="ns" style="--c:${nsColor(r.ns)}">${esc(this.groupTitle(r.ns))}</span></td>
          <td class="mono">${esc(r.ip || '–')}</td>
          <td class="${off ? 'dim' : ''}">${off ? `offline${r.seen != null ? ` · ${esc(fmtDur(r.seen))}` : ''}` : 'online'}</td>
          ${off ? '<td class="num dim">–</td><td class="num dim">–</td><td class="num dim">–</td>' : `${rate(r.down)}${rate(r.up)}<td class="num ${r.flows ? '' : 'dim'}">${r.flows ? fmtNum(r.flows) : '–'}</td>`}
          <td class="issue ${esc(issueLvl)}">${esc(r.issue || '')}</td></tr>`;
      }).join('')}</tbody></table>`;
  }

  _connTable(rows) {
    const th = (...a) => this._th('calls', ...a);
    const notes = rows.some((r) => r.note);
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by how serious the issue is')}${th('from', 'From')}${th('to', 'To')}
      ${th('bps', 'Rate', 'Traffic on this connection right now', true)}${th('conns', 'Connections', 'How many connections are open between the two', true)}
      ${th('ports', 'Ports')}${th('apps', 'Apps')}${notes ? th('risk', 'Issue') : ''}</tr></thead><tbody>
      ${rows.map((r) => `<tr data-edge="${esc(r.id)}"><td><span class="dot ${esc(r.level || 'ok')}"></span></td>
          <td class="nm">${esc(r.from)}<span class="k">${esc(this._where(r.fromNs))}</span></td>
          <td class="nm">${esc(r.to)}<span class="k">${esc(this._where(r.toNs))}</span></td>
          <td class="num ${r.bps ? '' : 'dim'}">${r.bps ? esc(fmtBits(r.bps)) : '–'}</td>
          <td class="num ${r.conns > 0 ? '' : 'dim'}">${r.conns > 0 ? fmtNum(r.conns) : '–'}</td>
          <td class="mono">${esc(Array.isArray(r.ports) ? r.ports.join(', ') : r.ports || '')}</td>
          <td>${esc(Array.isArray(r.apps) ? r.apps.join(', ') : r.apps || '')}${r.type === 'exposes' ? '<span class="tag">port forward</span>' : ''}</td>
          ${notes ? `<td class="issue ${r.level === 'ok' ? 'ok' : esc(r.level)}">${esc(r.note || '')}</td>` : ''}</tr>`).join('')}</tbody></table>`;
  }

  // Where one end of a connection lives: a group title, or the internet.
  _where(ns) {
    if (!ns) return '';
    if (ns === 'internet' || ns === 'external') return 'internet';
    if (ns === 'clients') return 'from outside';
    return this.groupTitle(ns);
  }

  // ------------------------------------------------------------------ recently joined
  _changes() {
    const list = this.graph.changes || [];
    this.el.querySelector('.tg-changes').innerHTML = list.length ? list.slice(0, 40).map((c) => `<li class="${esc(c.level || 'info')}" ${c.subject ? `data-node="${esc(c.subject)}"` : ''}>
      <span class="ic" title="${c.kind === 'joined' ? 'joined the network' : esc(c.kind || '')}">${c.kind === 'joined' ? '+' : '•'}</span>
      <div><div class="c1">${esc(c.title || c.name || '')}</div>${c.detail ? `<div class="c2">${esc(c.detail)}</div>` : ''}
      <div class="c3">${esc([fmtAgo(c.ageS), this.groupTitle(c.namespace)].filter(Boolean).join(' · '))}</div></div></li>`).join('')
      : '<li class="empty">No new devices in the last day.</li>';
  }

  // ------------------------------------------------------------------ interaction
  _click(ev) {
    const t = ev.target.closest('[data-copy],[data-tab],[data-cat],[data-sort],[data-group],[data-pattern],[data-more],[data-page],[data-edge],[data-node],#tgInfo,#tgOnly');
    if (!t) return;
    const d = t.dataset;
    if (d.copy != null) { this.ctx.copy(d.copy); t.textContent = 'copied'; setTimeout(() => { t.textContent = 'copy'; }, 1200); return; }
    if (d.tab) { this.tab = d.tab; this.shown = 30; this._tabs(); this._content(); return; }
    if (d.cat != null) { this.category = d.cat || null; this.shown = 30; this._tabs(); this._content(); return; }
    if (t.id === 'tgInfo') { this.showInfo = t.checked; this._tabs(); this._content(); return; }
    if (t.id === 'tgOnly') { this.onlyProblems = t.checked; this._tabs(); this._content(); return; }
    if (d.pattern != null) { this.pattern = d.pattern || null; this.shown = 30; this._content(); this.el.scrollTop = Math.min(this.el.scrollTop, this.el.querySelector('.tg-body').offsetTop); return; }
    if (d.more) { this.shown += 30; this._content(); return; }
    if (d.page) { const st = this.remote[d.page]; if (st) this._fetchTable(d.page, st.key, st.rows.length, 100, false); return; }
    if (d.sort) {
      const [table, col] = d.sort.split(':');
      const cur = this.sort[table];
      this.sort[table] = [col, cur[0] === col ? -cur[1] : (TEXT_SORT.has(col) ? 1 : -1)];
      this._content(); return;
    }
    if (d.group != null) { this.ctx.onGroup(d.group || null); return; }
    const sel = d.edge ? { type: 'edge', id: d.edge } : d.node ? { type: 'node', id: d.node } : null;
    if (!sel) return;
    if (d.map) this.ctx.onShowOnMap(sel); else this.ctx.onSelect(sel);
  }
}

export { SEV_COLOR, LEVEL_COLOR };

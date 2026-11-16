// The Triage view: one screen that answers "what is wrong, why, and what do I do".
//
//   verdict + platform strip   is the cluster itself healthy?
//   projects                   one tile per project, worst first
//   Problems                   root causes, each with its effects and downstream symptoms folded in
//   Workloads / Calls          the in-depth tables: every workload, every call with its latency
//   What changed               rollouts, restarts, events, node transitions - newest first
//
// It renders the same graph the 3D views use (graph.findings / changes / platform / namespaces).
import { esc, fmtAge, fmtMs, fmtNum, fmtPct, LEVEL_COLOR, nsColor, optionsHtml } from './format.js';

const SEV = { critical: 0, warning: 1, info: 2 };
const SEV_COLOR = { critical: 'var(--crit)', warning: 'var(--warn)', info: 'var(--muted)', ok: 'var(--ok)' };
const CATEGORY = {
  rollout: 'Rollout', crash: 'Crashing', image: 'Image', config: 'Config', admission: 'Admission', quota: 'Quota',
  scheduling: 'Scheduling', storage: 'Storage', probes: 'Probes', availability: 'Availability', network: 'Network',
  latency: 'Latency & errors', saturation: 'Saturation', node: 'Nodes', platform: 'Platform', batch: 'Jobs',
  sizing: 'Right-sizing', hygiene: 'Hygiene',
};
const ago = (s) => (s == null ? '' : s < 5 ? 'now' : `${fmtAge(Math.round(s))} ago`);

export class Triage {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;     // { onSelect(sel), onShowOnMap(sel), onProject(ns), onPlatformNode(id), copy(text) }
    this.tab = 'problems';
    this.category = null;
    this.showInfo = false;
    this.query = '';
    this.open = new Set();        // <details> the user opened; survive live refreshes
    this.shown = 30;              // problem cards rendered; "Show more" adds a page
    this.pattern = null;          // a fleet pattern opened to list its members
    this.remote = {};             // server-paged tables (large estates): kind -> {key, rows, total, ...}
    this.onlyProblems = null;     // tables: null = default for the estate's size
    this.sort = { workloads: ['risk', -1], calls: ['risk', -1] };
    el.innerHTML = `<div class="tg-head"></div><div class="tg-projects"></div>
      <div class="tg-body"><div class="tg-main"><div class="tg-tabs"></div><div class="tg-content"></div></div>
      <aside class="tg-side"><h2>What changed <span class="hint">newest first</span></h2><ol class="tg-changes"></ol></aside></div>`;
    el.addEventListener('click', (ev) => this._click(ev));
    el.addEventListener('toggle', (ev) => {
      const key = ev.target.dataset?.key;
      if (key) { if (ev.target.open) this.open.add(key); else this.open.delete(key); }
    }, true);
    el.addEventListener('input', (ev) => {
      if (ev.target.id === 'tgSearch') { this.query = ev.target.value.trim().toLowerCase(); this._content(); }
    });
  }

  render(graph, meta) {
    if (!graph) return;
    this.graph = graph;
    this.meta = meta;
    this.big = !!graph.view?.collapsed;       // the page holds a rolled-up picture: tables come from the server
    for (const k of Object.keys(this.remote)) this.remote[k].stale = true;
    this.byId = new Map(graph.nodes.map((n) => [n.id, n]));
    this.edgeById = new Map(graph.edges.map((e) => [e.id, e]));
    this._groups();
    this._head();
    this._projects();
    this._tabs();
    this._content();
    this._changes();
  }

  // Badge for the tab in the top bar: how many root causes need attention.
  static badge(graph) {
    const roots = (graph?.findings || []).filter((f) => f.root && !f.effectOf && f.severity !== 'info');
    const s = graph?.summary;
    const count = s ? s.problems : new Set(roots.map((f) => f.subject || f.id)).size;      // one per thing that is wrong
    if (s) return { count, level: s.problemsCritical ? 'critical' : count ? 'warning' : 'ok' };
    return { count, level: roots.some((f) => f.severity === 'critical') ? 'critical' : roots.length ? 'warning' : 'ok' };
  }

  // ------------------------------------------------------------------ model
  // One card per thing that is wrong: its findings (causes first, then effects) and the symptoms it causes elsewhere.
  _groups() {
    const all = this.graph.findings || [];
    const groups = new Map();
    const key = (f) => f.subject || f.id;
    for (const f of all) {
      if (!f.root) continue;
      const k = key(f);
      if (!groups.has(k)) groups.set(k, { key: k, subject: f.subject, findings: [], symptoms: [] });
      groups.get(k).findings.push(f);
    }
    const orphans = [];
    for (const f of all) {
      if (f.root) continue;
      const g = groups.get(f.causedBy);
      if (g) g.symptoms.push(f); else orphans.push(f);
    }
    for (const f of orphans) {     // the cause is outside the current filter: still show the symptom
      const k = key(f);
      if (!groups.has(k)) groups.set(k, { key: k, subject: f.subject, findings: [], symptoms: [], orphan: true });
      groups.get(k).findings.push(f);
    }
    this.groups = [...groups.values()].map((g) => {
      g.findings.sort((a, b) => (a.effectOf ? 1 : 0) - (b.effectOf ? 1 : 0) || SEV[a.severity] - SEV[b.severity] || b.weight - a.weight);
      const real = g.findings.filter((f) => f.severity !== 'info');
      g.severity = real.length ? (real.some((f) => f.severity === 'critical') ? 'critical' : 'warning') : 'info';
      g.blast = Math.max(0, ...g.findings.map((f) => f.blast || 0));
      g.userFacing = g.findings.some((f) => f.userFacing);
      g.head = g.findings[0];
      g.categories = new Set(real.length ? real.map((f) => f.category) : g.findings.map((f) => f.category));
      g.sizing = g.findings.some((f) => f.options?.length);       // the sizing advisor has concrete fixes for it
      return g;
    }).sort((a, b) => SEV[a.severity] - SEV[b.severity] || (a.orphan ? 1 : 0) - (b.orphan ? 1 : 0)
      || (b.userFacing ? 1 : 0) - (a.userFacing ? 1 : 0) || b.blast - a.blast || b.head.weight - a.head.weight);
  }

  // ------------------------------------------------------------------ header
  _head() {
    const g = this.graph;
    // Totals come from the server: it counts every finding in scope, not just the ones on this page.
    const S = g.summary || {};
    const total = S.problems ?? this.groups.filter((x) => x.severity !== 'info').length;
    const crit = S.problemsCritical ?? 0, warn = total - crit, nProj = S.projects ?? 0, users = S.userFacing ?? 0;
    const fleet = (g.patterns || []).filter((p) => p.severity !== 'info');
    const level = crit ? 'critical' : warn ? 'warning' : 'ok';
    const verdict = !total ? 'No problems found' : `${fmtNum(total)} problem${total === 1 ? '' : 's'} to look at`;
    const sub = !total ? 'Every workload in view is running as declared, and no call is slow or failing.'
      : [crit ? `<b class="c">${fmtNum(crit)} critical</b>` : '', warn ? `<b class="w">${fmtNum(warn)} warning${warn === 1 ? '' : 's'}</b>` : '',
        nProj ? `in ${nProj} project${nProj === 1 ? '' : 's'}` : '',
        users ? `<b class="c">${fmtNum(users)} reach${users === 1 ? 'es' : ''} users</b>` : '',
        fleet.length ? `<b>${fleet.length} shared cause${fleet.length === 1 ? '' : 's'}</b>` : ''].filter(Boolean).join(' · ');
    const p = g.platform || {};
    const chips = [];
    const nd = p.nodes;
    if (nd) {
      chips.push(this._chip(nd.ready === nd.total ? 'ok' : 'crit', `Nodes ${nd.ready}/${nd.total} ready`,
        nd.ready === nd.total ? 'Every node reports Ready' : 'A node that is not Ready takes its pods with it'));
      if (nd.pressure.length) chips.push(this._chip('crit', nd.pressure.join(', '), 'Node pressure: the kubelet is evicting pods'));
      if (nd.cordoned) chips.push(this._chip('warn', `${nd.cordoned} cordoned`, 'Cordoned nodes accept no new pods'));
    }
    const os = p.openshift;
    if (os) {
      const bad = [...os.unavailable, ...os.degraded];
      chips.push(this._chip(os.unavailable.length ? 'crit' : os.degraded.length ? 'warn' : 'ok',
        bad.length ? `Operators: ${bad.join(', ')} ${os.unavailable.length ? 'down' : 'degraded'}` : `Operators ${os.operators} healthy`,
        'OpenShift cluster operators. A degraded operator explains failures that are not in any one project.'));
      if (os.version?.updating) chips.push(this._chip('warn', `Upgrading to ${os.version.version}`, 'Cluster upgrade in progress: pod restarts and node reboots are expected'));
      for (const pool of os.pools || []) {
        if (pool.degraded) chips.push(this._chip('crit', `Pool ${pool.name} degraded`, 'A node failed to apply its machine config'));
        else if (pool.updating) chips.push(this._chip('warn', `Pool ${pool.name} updating ${pool.updated}/${pool.machines}`, 'Nodes are drained and rebooted one at a time'));
      }
    }
    const ROLE = { dns: 'DNS', ingress: 'Ingress', network: 'Pod network', metrics: 'Metrics', registry: 'Registry', api: 'Control plane' };
    for (const c of p.components || []) {
      chips.push(`<button class="chip ${c.level === 'critical' ? 'crit' : 'warn'}" data-platform="${esc(c.id)}"
        title="${esc(c.namespace)}/${esc(c.name)}: ${esc(c.reason)}">${ROLE[c.role] || c.role}: ${esc(c.name)} ▸</button>`);
    }
    const src = g.sources || {};
    const warns = src.kubernetes?.warnings || [];
    const blind = warns.filter((w) => /events|persistentvolumeclaims|resourcequotas|networkpolicies/.test(w));
    if (blind.length) chips.push(this._chip('warn', 'Diagnostics limited', `The collector may not read: ${blind.join('; ')}. Update its ClusterRole.`));
    if (!src.agent?.nodes) chips.push(this._chip('off', 'No flow agent', 'Without the node agent there is no socket-level latency, retransmit or listening-port data'));
    this.el.querySelector('.tg-head').innerHTML = `
      <div class="tg-verdict ${level}"><span class="orb"></span><div><div class="v1">${verdict}</div><div class="v2">${sub}</div></div></div>
      <div class="tg-plat">${chips.join('')}</div>`;
  }

  _chip(cls, text, title) {
    return `<span class="chip ${cls}" title="${esc(title || '')}">${cls === 'ok' ? '● ' : cls === 'off' ? '○ ' : '▲ '}${esc(text)}</span>`;
  }

  // ------------------------------------------------------------------ projects
  _projects() {
    const scope = this.ctx.scope ? this.ctx.scope() : {};
    const rows = (this.graph.namespaces || []).filter((p) => (scope.namespaces?.length ? scope.namespaces.includes(p.name) : (scope.platform || !p.platform)) && p.workloads);
    const rank = (p) => (p.down ? 0 : p.findings.critical ? 1 : p.findings.warning || p.degraded ? 2 : 3);
    rows.sort((a, b) => rank(a) - rank(b) || b.down - a.down || b.findings.critical - a.findings.critical || a.name.localeCompare(b.name));
    const tile = (p) => {
      const lvl = rank(p) <= 1 ? 'critical' : rank(p) === 2 ? 'warning' : 'ok';
      const bar = (n, c) => (n ? `<i style="flex:${n};background:${c}"></i>` : '');
      return `<button class="tg-proj ${lvl}" data-project="${esc(p.name)}" title="Show only ${esc(p.name)}">
        <div class="p1"><span class="sw" style="background:${nsColor(p.group || p.name, p.platform)}"></span><span class="nm">${esc(p.name)}</span>
          ${p.rolling ? '<span class="tag roll" title="A rollout is in progress or stuck">rolling</span>' : ''}
          ${p.quota >= 0.9 ? `<span class="tag q" title="Project quota">quota ${Math.round(p.quota * 100)}%</span>` : ''}</div>
        <div class="bar">${bar(p.healthy, 'var(--ok)')}${bar(p.degraded, 'var(--warn)')}${bar(p.down, 'var(--crit)')}</div>
        <div class="p2">${p.healthy}/${p.workloads} workloads healthy${p.down ? ` · <b class="c">${p.down} down</b>` : ''} · ${p.podsReady}/${p.pods} pods ready${p.restarts1h ? ` · <b class="w">${p.restarts1h} restarts</b>` : ''}</div>
        <div class="p3 ${p.top ? p.top.severity : ''}">${p.top ? `${esc(p.top.subjectName)}: ${esc(p.top.title)}` : 'No problems'}</div></button>`;
    };
    const box = this.el.querySelector('.tg-projects');
    if (rows.length <= 10) { box.className = 'tg-projects'; box.innerHTML = rows.map(tile).join('') || '<div class="empty">No projects in view.</div>'; return; }
    // Many projects: the worst few as tiles, and the whole estate as one square per project, by business unit.
    const bad = rows.filter((p) => rank(p) < 3);
    const units = new Map();
    for (const p of rows) { const k = p.group || p.name; if (!units.has(k)) units.set(k, []); units.get(k).push(p); }
    const unitRows = [...units.entries()].map(([name, ps]) => ({ name, ps, bad: ps.filter((p) => rank(p) < 2).length }))
      .sort((a, b) => b.bad - a.bad || b.ps.length - a.ps.length || a.name.localeCompare(b.name));
    box.className = 'tg-projects estate';
    box.innerHTML = `<div class="tg-estate"><h2>Estate <span class="hint">${rows.length} projects in ${units.size} business unit${units.size === 1 ? '' : 's'} · one square per project · click to scope</span>
        <span class="key"><i class="ok"></i>healthy <i class="warning"></i>degraded <i class="critical"></i>failing</span></h2>
      <div class="units">${unitRows.map((u) => `<div class="unit"><div class="un" title="${u.ps.length} projects">${esc(u.name)}<span>${u.bad ? `${u.bad} failing` : ''}</span></div>
        <div class="sq">${u.ps.map((p) => `<button class="${rank(p) <= 1 ? 'critical' : rank(p) === 2 ? 'warning' : 'ok'}" data-project="${esc(p.name)}"
          title="${esc(p.name)} · ${p.healthy}/${p.workloads} workloads healthy${p.top ? ' · ' + esc(p.top.subjectName) + ': ' + esc(p.top.title) : ''}"></button>`).join('')}</div></div>`).join('')}</div></div>
      <div class="tg-worst"><h2>Worst projects <span class="hint">${bad.length} of ${rows.length} have problems</span></h2>
      <div class="tiles">${bad.slice(0, 8).map(tile).join('') || '<div class="empty">No project has problems.</div>'}</div></div>`;
  }

  // ------------------------------------------------------------------ tabs
  _tabs() {
    const real = this.groups.filter((g) => g.severity !== 'info');
    const info = this.groups.length - real.length;
    const wl = this.graph.view?.workloads ?? this.graph.nodes.filter((n) => n.kind === 'workload' && !n.ghost).length;
    const calls = this.big ? (this.remote.calls?.scope ?? '') : this.graph.edges.filter((e) => e.type !== 'exposes').length;
    const S = this.graph.summary;
    const only = this.onlyProblems ?? this.big;
    const cats = new Map();
    for (const g of (this.showInfo ? this.groups : real)) for (const c of g.categories) cats.set(c, (cats.get(c) || 0) + 1);
    const order = Object.keys(CATEGORY).filter((c) => cats.has(c));
    const sized = this.groups.filter((g) => g.sizing).length + (this.graph.patterns || []).filter((p) => p.sizing).length;
    const tab = (id, label, n) => `<button class="${this.tab === id ? 'on' : ''}" data-tab="${id}">${label} <span>${n}</span></button>`;
    this.el.querySelector('.tg-tabs').innerHTML = `
      <div class="seg">${tab('problems', 'Problems', S ? fmtNum(S.problems) : real.length)}${tab('workloads', 'Workloads', fmtNum(wl))}${tab('calls', 'Calls', calls === '' ? '' : fmtNum(calls))}</div>
      ${this.tab !== 'problems' ? `<label class="tg-info" title="Hide rows that are healthy"><input type="checkbox" id="tgOnly" ${only ? 'checked' : ''}> Only problems</label>` : ''}
      ${this.tab === 'problems' ? `<div class="cats"><button class="${!this.category ? 'on' : ''}" data-cat="">All</button>
        ${sized ? `<button class="sz ${this.category === '_sizing' ? 'on' : ''}" data-cat="_sizing" title="Too little or too much CPU, memory, quota or replicas: each with ways to fix it">Wrong-sized <span>${sized}</span></button>` : ''}
        ${order.map((c) => `<button class="${this.category === c ? 'on' : ''}" data-cat="${c}">${CATEGORY[c]} <span>${cats.get(c)}</span></button>`).join('')}</div>
        <label class="tg-info" title="Hardening gaps and other things that are not failures"><input type="checkbox" id="tgInfo" ${this.showInfo ? 'checked' : ''}> Show informational</label>` : ''}
      <input id="tgSearch" type="search" placeholder="Filter…" value="${esc(this.query)}" autocomplete="off">`;
  }

  _content() {
    const box = this.el.querySelector('.tg-content');
    box.innerHTML = this.tab === 'workloads' ? this._table('workloads') : this.tab === 'calls' ? this._table('calls') : this._problems();
  }

  // ------------------------------------------------------------------ problems
  _problems() {
    const q = this.query;
    if (this.pattern) return this._patternMembers();
    const match = (text) => !q || text.toLowerCase().includes(q);
    const sizing = this.category === '_sizing';        // not a category: everything the sizing advisor has fixes for
    const patterns = (this.graph.patterns || []).filter((p) => (sizing ? p.sizing : (this.showInfo || p.severity !== 'info')
      && (!this.category || p.category === this.category)) && match(`${p.label} ${p.cause}`));
    const list = this.groups.filter((g) => (sizing ? g.sizing : (this.showInfo || g.severity !== 'info')
      && (!this.category || g.categories.has(this.category)))
      && match([g.head.subjectName, g.head.namespace, ...g.findings.map((f) => f.title + ' ' + f.cause)].join(' ')));
    if (!list.length && !patterns.length) {
      return `<div class="tg-clear">${(this.graph.summary?.problems || this.groups.some((g) => g.severity !== 'info')) ? 'Nothing matches this filter.'
        : '<b>Nothing is failing.</b> Rollouts are complete, pods are ready, calls are answering in time.'}</div>`;
    }
    const more = list.length - this.shown;
    const hidden = (this.graph.findingsMatching || 0) - (this.graph.findings || []).length;
    return (patterns.length ? `<h3 class="tg-h">Shared causes <span class="hint">the same problem on several workloads, shown once</span></h3>${patterns.map((p) => this._pattern(p)).join('')}` : '')
      + (list.length && patterns.length ? '<h3 class="tg-h">Individual problems</h3>' : '')
      + list.slice(0, this.shown).map((g) => this._card(g)).join('')
      + (more > 0 ? `<button class="btn tg-more" data-more="1">Show ${Math.min(30, more)} more of ${more}</button>` : '')
      + (hidden > 0 ? `<div class="tg-note">Showing the ${this.graph.findings.length} most severe of ${fmtNum(this.graph.findingsMatching)} findings. Scope to a project or business unit to see the rest.</div>` : '');
  }

  // One cause, many subjects: "40 workloads in 38 projects are crash-looping: application error (exit 1)".
  _pattern(p) {
    const noun = { workload: 'workload', call: 'call', host: 'node', entry: 'route', service: 'Service' }[p.kind] || 'project';
    const units = p.units.filter((u) => u.name !== 'cluster').map((u) => `${esc(u.name)} ${u.count}`).join(' · ');
    return `<article class="tg-card tg-pat ${p.severity}">
      <header><span class="sev" style="background:${SEV_COLOR[p.severity]}"></span>
        <span class="n">${fmtNum(p.count)}</span><span class="nm2">${noun}${p.count === 1 ? '' : 's'}${p.projects ? ` in ${p.projects} project${p.projects === 1 ? '' : 's'}` : ''}: ${esc(p.label)}</span>
        ${p.userFacing ? `<span class="tag users">${p.userFacing} reach users</span>` : ''}
        <button class="link map" data-pattern="${esc(p.key)}">Show all ${fmtNum(p.count)} ▸</button></header>
      <section class="tg-f ${p.severity}"><div class="t"><span class="cat">${esc(CATEGORY[p.category] || p.category)}</span>
        <span class="title sub">${units ? `Most in: ${units}` : ''}</span></div>
        ${p.cause ? `<p class="why"><b>Why</b>${esc(p.cause)}</p>` : ''}${p.fix ? `<p class="fix"><b>Next</b>${esc(p.fix)}</p>` : ''}
        <div class="pods ex"><span>e.g.</span>${p.examples.map((x) => `<a ${x.subject.includes('->') ? 'data-edge' : 'data-node'}="${esc(x.subject)}" title="${esc(x.title)}">${esc(x.subjectName)}${x.namespace ? `<i>${esc(x.namespace)}</i>` : ''}</a>`).join('')}</div>
      </section></article>`;
  }

  _patternMembers() {
    const st = this.patternData;
    const p = (this.graph.patterns || []).find((x) => x.key === this.pattern);
    const head = `<div class="tg-back"><button class="link" data-pattern="">← All problems</button>
      <b>${p ? `${fmtNum(p.count)} × ${esc(p.label)}` : 'This problem is no longer occurring.'}</b></div>`;
    if (!st || st.key !== this.pattern || st.stale) {
      if (!st || st.key !== this.pattern) this.patternData = { key: this.pattern, loading: true, groups: [] };
      else st.stale = false;
      this.ctx.fetchPattern(this.pattern).then((res) => {
        if (this.pattern !== res.key) return;
        const keep = this.graph.findings;
        this.graph.findings = res.findings;
        this._groups();
        this.patternData = { key: res.key, groups: this.groups, total: res.findingsMatching };
        this.graph.findings = keep;
        this._groups();
        this._content();
      }).catch(() => {});
      if (!st || st.key !== this.pattern) return head + '<div class="tg-clear">Loading…</div>';
    }
    const groups = (this.patternData.groups || []).filter((g) => !this.query
      || `${g.head.subjectName} ${g.head.namespace}`.toLowerCase().includes(this.query));
    const more = groups.length - this.shown;
    return head + groups.slice(0, this.shown).map((g) => this._card(g)).join('')
      + (more > 0 ? `<button class="btn tg-more" data-more="1">Show ${Math.min(30, more)} more of ${more}</button>` : '');
  }

  _card(g) {
    const h = g.head;
    const node = this.byId.get(g.subject);
    const edge = this.edgeById.get(g.subject);
    const roll = node?.rollout;
    const chips = [];
    if (g.userFacing) chips.push('<span class="tag users">reaches users</span>');
    if (g.blast) chips.push(`<span class="tag" title="Workloads degraded because of this">slows ${g.blast} other${g.blast === 1 ? '' : 's'}</span>`);
    if (roll && roll.ageSeconds != null && roll.ageSeconds < 7200 && roll.state !== 'complete') chips.push(`<span class="tag roll">rollout ${roll.state}</span>`);
    else if (roll && roll.ageSeconds != null && roll.ageSeconds < 3600) chips.push(`<span class="tag chg" title="A recent change is the first suspect">deployed ${ago(roll.ageSeconds)}</span>`);
    if (g.orphan) chips.push(`<span class="tag">symptom of ${esc(h.causedByName || 'something outside this view')}</span>`);
    const sel = node ? `data-node="${esc(node.id)}"` : edge ? `data-edge="${esc(edge.id)}"` : '';
    const kind = node ? (node.subkind || node.kind) : edge ? 'Call' : h.category === 'platform' ? 'Platform' : 'Project';
    const name = h.subjectName || (h.namespace ? h.namespace : 'Cluster');
    return `<article class="tg-card ${g.severity}">
      <header><span class="sev" style="background:${SEV_COLOR[g.severity]}"></span>
        <a class="nm" ${sel} title="Open details">${esc(name)}</a><span class="kind">${esc(kind)}</span>
        ${h.namespace ? `<span class="ns" style="--c:${nsColor(h.namespace, h.platform)}">${esc(h.namespace)}</span>` : ''}
        ${chips.join('')}
        ${sel ? `<button class="link map" ${sel} data-map="1" title="Show it in the topology">Show on map ▸</button>` : ''}</header>
      ${g.findings.filter((f) => this.showInfo || f.severity !== 'info' || g.severity === 'info' || (this.category === '_sizing' && f.options?.length)).map((f) => this._finding(f)).join('')}
      ${g.symptoms.length ? this._symptoms(g) : ''}</article>`;
  }

  _finding(f) {
    const key = `f:${f.id}`;
    const more = f.evidence.length || f.commands.length || f.pods.length || f.detail;
    return `<section class="tg-f ${f.severity}${f.effectOf ? ' effect' : ''}">
      <div class="t"><span class="cat">${esc(CATEGORY[f.category] || f.category)}</span>
        <span class="title">${f.effectOf ? '<span class="eff">↳ so:</span> ' : ''}${esc(f.title)}</span>
        ${f.sinceS != null ? `<span class="since">${ago(f.sinceS)}</span>` : ''}</div>
      ${f.cause ? `<p class="why"><b>Why</b>${esc(f.cause)}</p>` : ''}
      ${f.options?.length ? optionsHtml(f) : f.fix ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : ''}
      ${more ? `<details data-key="${esc(key)}" ${this.open.has(key) ? 'open' : ''}><summary>Evidence & commands</summary>
        ${f.detail ? `<div class="det">${esc(f.detail)}</div>` : ''}
        ${f.evidence.map((e) => `<pre class="ev-msg">${esc(e)}</pre>`).join('')}
        ${f.pods.length ? `<div class="pods">${f.pods.map((p) => `<a data-pod="${esc(f.subject)}#${esc(p)}">${esc(p)}</a>`).join('')}${f.podCount > f.pods.length ? ` +${f.podCount - f.pods.length} more` : ''}</div>` : ''}
        ${f.commands.map((c) => `<div class="cmd"><code>${esc(c)}</code><button class="link" data-copy="${esc(c)}">copy</button></div>`).join('')}
      </details>` : ''}</section>`;
  }

  _symptoms(g) {
    const key = `s:${g.key}`;
    const by = new Map();
    for (const s of g.symptoms) { if (!by.has(s.subject)) by.set(s.subject, []); by.get(s.subject).push(s); }
    const rows = [...by.entries()].map(([sid, fs]) => `<li><a ${this.byId.has(sid) ? 'data-node' : 'data-edge'}="${esc(sid)}">${esc(fs[0].subjectName)}</a>
      <span>${fs.map((f) => esc(f.title.includes(': ') ? f.title.split(': ').slice(1).join(': ') : f.title)).join(' · ')}</span></li>`);
    return `<details class="tg-sym" data-key="${esc(key)}" ${this.open.has(key) ? 'open' : ''}>
      <summary>${by.size} downstream symptom${by.size === 1 ? '' : 's'} — these clear when the cause above is fixed</summary><ul>${rows.join('')}</ul></details>`;
  }

  // ------------------------------------------------------------------ tables
  // Small estates: rows are computed here from the graph. Large ones: the server filters, sorts and pages.
  _table(kind) {
    const only = this.onlyProblems ?? this.big;
    const [col, dir] = this.sort[kind];
    if (!this.big) {
      let rows = (kind === 'workloads' ? this._localWorkloads() : this._localCalls())
        .filter((r) => (!only || (r.level !== 'ok' && r.level !== 'inherit')) && (!this.query || r.text.includes(this.query)));
      rows.sort((a, b) => (typeof a[col] === 'string' ? a[col].localeCompare(b[col]) * -dir : (a[col] - b[col]) * dir) || b.risk - a.risk);
      return kind === 'workloads' ? this._workloadTable(rows) : this._callTable(rows);
    }
    const key = JSON.stringify([col, dir, this.query, only, this.ctx.scope()]);
    let st = this.remote[kind];
    if (!st || st.key !== key || st.stale) {
      const fresh = !st || st.key !== key;
      if (fresh) st = this.remote[kind] = { key, rows: [], total: 0, loading: true };
      st.stale = false;
      this._fetchTable(kind, key, 0, fresh ? 100 : Math.max(100, st.rows.length), true);
    }
    const body = kind === 'workloads' ? this._workloadTable(st.rows) : this._callTable(st.rows);
    const left = st.total - st.rows.length;
    return `<div class="tg-count">${st.loading && !st.rows.length ? 'Loading…' : `${fmtNum(st.total)} ${only ? 'with problems' : 'matching'} of ${fmtNum(st.scope || 0)} ${kind}`}</div>${body}
      ${left > 0 ? `<button class="btn tg-more" data-page="${kind}">Show 100 more of ${fmtNum(left)}</button>` : ''}`;
  }

  _fetchTable(kind, key, offset, limit, replace) {
    const [col, dir] = this.sort[kind];
    this.ctx.fetchTable(kind, { sort: col, desc: dir < 0, q: this.query, problems: this.onlyProblems ?? this.big, offset, limit }).then((res) => {
      const st = this.remote[kind];
      if (!st || st.key !== key) return;
      st.rows = replace ? res.rows : st.rows.concat(res.rows);
      Object.assign(st, { total: res.total, scope: res.scope, loading: false });
      if (this.tab === kind) { this._tabs(); this._content(); }
    }).catch(() => {});
  }

  _localWorkloads() {
    const edges = this.graph.edges;
    return this.graph.nodes.filter((n) => n.kind === 'workload' && !n.ghost && !n.aggregate).map((n) => {
      const inbound = edges.filter((e) => e.target === n.id && e.type !== 'exposes');
      const worstOut = edges.filter((e) => e.source === n.id && e.latency.estimateMs != null).sort((a, b) => b.latency.estimateMs - a.latency.estimateMs)[0];
      const top = (this.graph.findings || []).find((f) => f.subject === n.id && f.severity !== 'info' && !f.effectOf);
      const inherit = n.risk.impactLevel !== 'ok' && n.risk.level === 'ok';
      const issue = top ? top.title : inherit ? `Slowed by ${this._name(n.risk.impactFrom)}` : '';
      return {
        id: n.id, name: n.name, ns: n.namespace, kind: n.subkind, platform: n.platform, risk: n.risk.score + (n.risk.impact || 0) * 0.3,
        level: n.risk.level !== 'ok' ? n.risk.level : inherit ? 'inherit' : 'ok',
        readyN: n.replicas.ready, desired: n.replicas.desired, ready: n.replicas.desired ? n.replicas.ready / n.replicas.desired : 1,
        rollout: n.rollout || null, age: n.rollout?.ageSeconds ?? 1e12,
        restarts: n.pods.reduce((x, p) => x + (p.terminal ? 0 : p.recentRestarts || 0), 0),
        cpu: n.usage?.cpuRatio ?? -1, mem: n.usage?.memRatio ?? -1,
        inP95: Math.max(-1, ...inbound.map((e) => e.latency.p95Ms ?? -1)), inErr: Math.max(0, ...inbound.map((e) => e.health.errorRate || 0)),
        rps: inbound.reduce((x, e) => x + (e.traffic.rps || 0), 0),
        out: worstOut ? worstOut.latency.estimateMs : -1, outName: worstOut ? this.byId.get(worstOut.target)?.name : '',
        issue, issueLvl: top ? top.severity : inherit ? n.risk.impactLevel : 'ok', text: `${n.name} ${n.namespace} ${issue}`.toLowerCase(),
      };
    });
  }

  _workloadTable(rows) {
    const [col, dir] = this.sort.workloads;
    const th = (id, label, title = '') => `<th data-sort="workloads:${id}" class="${col === id ? 'on' : ''}" title="${esc(title)}">${label}${col === id ? (dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
    const pct = (r) => (r < 0 ? '<td class="num dim">–</td>' : `<td class="num ${r >= 0.9 ? 'c' : r >= 0.75 ? 'w' : ''}">${Math.round(r * 100)}%</td>`);
    const ROLL = { complete: '', progressing: 'w', stuck: 'c', failed: 'c', paused: 'w' };
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by severity')}${th('name', 'Workload')}${th('ns', 'Project')}
      ${th('ready', 'Ready', 'Ready pods / desired')}${th('age', 'Last rollout', 'State of the latest rollout and when it started')}
      ${th('restarts', 'Restarts', 'Container restarts in the last hour')}${th('cpu', 'CPU', 'Highest pod CPU as a share of its limit')}${th('mem', 'Mem', 'Highest pod memory as a share of its limit')}
      ${th('rps', 'Req/s', 'Inbound requests per second (mesh)')}${th('inP95', 'p95 in', 'Slowest inbound request p95: what callers experience')}${th('inErr', 'Err in', 'Highest inbound 5xx rate')}
      ${th('out', 'Slowest dependency', 'Its slowest downstream call')}<th>Top issue</th></tr></thead><tbody>
      ${rows.map((r) => {
        const ro = r.rollout;
        return `<tr data-node="${esc(r.id)}"><td><span class="dot ${r.level}" title="${r.level === 'inherit' ? 'Healthy itself; slowed by a dependency' : r.level}"></span></td>
          <td class="nm">${esc(r.name)}<span class="k">${esc(r.kind)}</span></td>
          <td><span class="ns" style="--c:${nsColor(r.ns, r.platform)}">${esc(r.ns)}</span></td>
          <td class="num ${r.ready === 0 && r.desired ? 'c' : r.ready < 1 ? 'w' : ''}">${r.readyN}/${r.desired}</td>
          <td class="${ro ? ROLL[ro.state] || '' : 'dim'}">${ro ? `${ro.state === 'complete' ? '' : ro.state + ' · '}${ro.revision ? 'rev ' + ro.revision + ' · ' : ''}${ro.ageSeconds != null ? ago(ro.ageSeconds) : ''}` : '–'}</td>
          <td class="num ${r.restarts >= 3 ? 'c' : r.restarts ? 'w' : 'dim'}">${r.restarts || '–'}</td>${pct(r.cpu)}${pct(r.mem)}
          <td class="num ${r.rps ? '' : 'dim'}">${r.rps ? fmtNum(r.rps) : '–'}</td>
          <td class="num ${r.inP95 > 1000 ? 'c' : r.inP95 > 300 ? 'w' : r.inP95 < 0 ? 'dim' : ''}">${r.inP95 < 0 ? '–' : fmtMs(r.inP95)}</td>
          <td class="num ${r.inErr > 0.05 ? 'c' : r.inErr > 0.01 ? 'w' : 'dim'}">${r.inErr ? fmtPct(r.inErr) : '–'}</td>
          <td class="${r.out > 300 ? 'w' : r.out < 0 ? 'dim' : ''}">${r.out < 0 ? '–' : `${esc(r.outName || '')} ${fmtMs(r.out)}`}</td>
          <td class="issue ${r.issueLvl}">${esc(r.issue)}</td></tr>`;
      }).join('')}</tbody></table>`;
  }

  _name(id) {
    if (!id) return 'a dependency';
    const n = this.byId.get(id);
    if (n) return n.name;
    const e = this.edgeById.get(id);
    return e ? `${this.byId.get(e.source)?.name} → ${this.byId.get(e.target)?.name}` : id.split('/').pop();
  }

  _localCalls() {
    return this.graph.edges.filter((e) => e.type !== 'exposes').map((e) => {
      const s = this.byId.get(e.source), t = this.byId.get(e.target);
      const f = (this.graph.findings || []).find((x) => x.subject === e.id && x.severity !== 'info');
      const note = f ? (f.root ? f.title.split(': ').slice(1).join(': ') || f.title : `symptom of ${f.causedByName}`)
        : e.traffic.idle ? 'idle' : (!e.evidence.includes('observed') && !e.evidence.includes('mesh')) ? 'declared in config, no traffic seen' : '';
      return {
        id: e.id, risk: e.risk.score, level: e.risk.level, from: s?.name || '', to: t?.name || '',
        fromNs: s?.namespace || s?.kind || '', toNs: t?.namespace || (t?.kind === 'external' ? (t.scope || 'external') : t?.kind) || '',
        external: t?.kind === 'external', rps: e.traffic.rps ?? -1, conns: e.traffic.connections ?? -1,
        p50: e.latency.p50Ms ?? -1, p95: e.latency.p95Ms ?? -1, err: e.health.errorRate || 0, rtt: e.latency.rttMinMs ?? -1,
        pending: e.health.pending || 0, retrans: e.health.retrans || 0, bad: (e.health.retrans || 0) + (e.health.pending || 0) * 10,
        evidence: e.evidence, note, text: `${s?.name} ${t?.name} ${note}`.toLowerCase(),
      };
    });
  }

  _callTable(rows) {
    const [col, dir] = this.sort.calls;
    const th = (id, label, title = '') => `<th data-sort="calls:${id}" class="${col === id ? 'on' : ''}" title="${esc(title)}">${label}${col === id ? (dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
    const ms = (v, warn, crit) => (v < 0 ? '<td class="num dim">–</td>' : `<td class="num ${v > crit ? 'c' : v > warn ? 'w' : ''}">${fmtMs(v)}</td>`);
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by severity')}${th('from', 'Caller (upstream)')}${th('to', 'Callee (downstream)')}
      ${th('rps', 'Req/s')}${th('conns', 'Conns', 'Open connections seen by the node agent')}${th('p50', 'p50')}${th('p95', 'p95', 'Request latency from mesh metrics')}
      ${th('err', '5xx')}${th('rtt', 'Path RTT', 'Network round trip (kernel minimum): the path, not the app')}${th('bad', 'Loss', 'Retransmitting segments / unanswered connection attempts')}
      <th>Evidence</th><th>Note</th></tr></thead><tbody>
      ${rows.map((r) => `<tr data-edge="${esc(r.id)}"><td><span class="dot ${r.level}"></span></td>
          <td class="nm">${esc(r.from)}<span class="k">${esc(r.fromNs)}</span></td>
          <td class="nm">${esc(r.to)}<span class="k">${esc(r.toNs)}</span></td>
          <td class="num ${r.rps < 0 ? 'dim' : ''}">${r.rps < 0 ? '–' : fmtNum(r.rps)}</td><td class="num ${r.conns < 0 ? 'dim' : ''}">${r.conns < 0 ? '–' : r.conns}</td>
          ${ms(r.p50, 100, 500)}${ms(r.p95, 300, 1000)}
          <td class="num ${r.err > 0.05 ? 'c' : r.err > 0.01 ? 'w' : 'dim'}">${r.err ? fmtPct(r.err) : '–'}</td>
          ${r.external ? `<td class="num ${r.rtt < 0 ? 'dim' : ''}">${r.rtt < 0 ? '–' : fmtMs(r.rtt)}</td>` : ms(r.rtt, 2, 10)}
          <td class="num ${r.pending ? 'c' : r.retrans ? 'w' : 'dim'}">${r.pending ? r.pending + ' unanswered' : r.retrans ? r.retrans + ' retrans' : '–'}</td>
          <td>${r.evidence.map((x) => `<span class="ev ${x}">${x}</span>`).join(' ')}</td>
          <td class="issue ${r.level === 'ok' ? 'ok' : r.level}">${esc(r.note)}</td></tr>`).join('')}</tbody></table>`;
  }

  // ------------------------------------------------------------------ what changed
  _changes() {
    const ICON = { rollout: '⟳', restart: '↻', event: '!', node: '▣', scale: '⇅', job: '✕' };
    const list = this.graph.changes || [];
    this.el.querySelector('.tg-changes').innerHTML = list.length ? list.map((c) => `<li class="${c.level}" ${c.subject ? `data-node="${esc(c.subject)}"` : ''}>
      <span class="ic" title="${esc(c.kind)}">${ICON[c.kind] || '•'}</span>
      <div><div class="c1">${esc(c.title)}</div>${c.detail ? `<div class="c2">${esc(c.detail)}</div>` : ''}
      <div class="c3">${ago(c.ageS)}${c.namespace ? ` · ${esc(c.namespace)}` : ''}</div></div></li>`).join('')
      : '<li class="empty">Nothing has changed recently: no rollouts, restarts or warning events in the last hours.</li>';
  }

  // ------------------------------------------------------------------ interaction
  _click(ev) {
    const t = ev.target.closest('[data-copy],[data-tab],[data-cat],[data-sort],[data-project],[data-platform],[data-pattern],[data-more],[data-page],[data-pod],[data-edge],[data-node],#tgInfo,#tgOnly');
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
      this.sort[table] = [col, cur[0] === col ? -cur[1] : (col === 'name' || col === 'ns' || col === 'from' || col === 'to' ? 1 : -1)];
      this._content(); return;
    }
    if (d.project) { this.ctx.onProject(d.project); return; }
    if (d.platform) { this.ctx.onPlatformNode(d.platform); return; }
    const sel = d.pod ? { type: 'pod', key: d.pod, wid: d.pod.split('#')[0] } : d.edge ? { type: 'edge', id: d.edge } : d.node ? { type: 'node', id: d.node } : null;
    if (!sel) return;
    if (d.map) this.ctx.onShowOnMap(sel); else this.ctx.onSelect(sel);
  }
}

export { CATEGORY, SEV_COLOR, LEVEL_COLOR };

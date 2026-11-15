// The Triage view: one screen that answers "what is wrong, why, and what do I do".
//
//   verdict + platform strip   is the cluster itself healthy?
//   projects                   one tile per project, worst first
//   Problems                   root causes, each with its effects and downstream symptoms folded in
//   Workloads / Calls          the in-depth tables: every workload, every call with its latency
//   What changed               rollouts, restarts, events, node transitions - newest first
//
// It renders the same graph the 3D views use (graph.findings / changes / platform / namespaces).
import { esc, fmtAge, fmtMs, fmtNum, fmtPct, LEVEL_COLOR, nsColor } from './format.js';

const SEV = { critical: 0, warning: 1, info: 2 };
const SEV_COLOR = { critical: 'var(--crit)', warning: 'var(--warn)', info: 'var(--muted)', ok: 'var(--ok)' };
const CATEGORY = {
  rollout: 'Rollout', crash: 'Crashing', image: 'Image', config: 'Config', admission: 'Admission', quota: 'Quota',
  scheduling: 'Scheduling', storage: 'Storage', probes: 'Probes', availability: 'Availability', network: 'Network',
  latency: 'Latency & errors', saturation: 'Saturation', node: 'Nodes', platform: 'Platform', batch: 'Jobs',
  hygiene: 'Hygiene',
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
    const count = new Set(roots.map((f) => f.subject || f.id)).size;      // one per thing that is wrong
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
      return g;
    }).sort((a, b) => SEV[a.severity] - SEV[b.severity] || (a.orphan ? 1 : 0) - (b.orphan ? 1 : 0)
      || (b.userFacing ? 1 : 0) - (a.userFacing ? 1 : 0) || b.blast - a.blast || b.head.weight - a.head.weight);
  }

  // ------------------------------------------------------------------ header
  _head() {
    const g = this.graph;
    const real = this.groups.filter((x) => x.severity !== 'info');
    const crit = real.filter((x) => x.severity === 'critical').length;
    const warn = real.length - crit;
    const projects = new Set(real.map((x) => x.head.namespace).filter(Boolean));
    const users = real.filter((x) => x.userFacing).length;
    const level = crit ? 'critical' : warn ? 'warning' : 'ok';
    const verdict = !real.length ? 'No problems found'
      : `${real.length} problem${real.length === 1 ? '' : 's'} to look at`;
    const sub = !real.length ? 'Every workload in view is running as declared, and no call is slow or failing.'
      : [crit ? `<b class="c">${crit} critical</b>` : '', warn ? `<b class="w">${warn} warning${warn === 1 ? '' : 's'}</b>` : '',
        projects.size ? `in ${projects.size} project${projects.size === 1 ? '' : 's'}` : '',
        users ? `<b class="c">${users} reach${users === 1 ? 'es' : ''} users</b>` : ''].filter(Boolean).join(' · ');
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
    const inView = new Set(this.graph.nodes.filter((n) => n.kind === 'workload' && !n.ghost).map((n) => n.namespace));
    const rows = (this.graph.namespaces || []).filter((p) => inView.has(p.name));
    const rank = (p) => (p.down ? 0 : p.findings.critical ? 1 : p.findings.warning || p.degraded ? 2 : 3);
    rows.sort((a, b) => rank(a) - rank(b) || b.findings.critical - a.findings.critical || a.name.localeCompare(b.name));
    this.el.querySelector('.tg-projects').innerHTML = rows.map((p) => {
      const lvl = rank(p) <= 1 ? 'critical' : rank(p) === 2 ? 'warning' : 'ok';
      const bar = (n, c) => (n ? `<i style="flex:${n};background:${c}"></i>` : '');
      return `<button class="tg-proj ${lvl}" data-project="${esc(p.name)}" title="Show only ${esc(p.name)}">
        <div class="p1"><span class="sw" style="background:${nsColor(p.name, p.platform)}"></span><span class="nm">${esc(p.name)}</span>
          ${p.rolling ? '<span class="tag roll" title="A rollout is in progress or stuck">rolling</span>' : ''}
          ${p.quota >= 0.9 ? `<span class="tag q" title="Project quota">quota ${Math.round(p.quota * 100)}%</span>` : ''}</div>
        <div class="bar">${bar(p.healthy, 'var(--ok)')}${bar(p.degraded, 'var(--warn)')}${bar(p.down, 'var(--crit)')}</div>
        <div class="p2">${p.healthy}/${p.workloads} workloads healthy${p.down ? ` · <b class="c">${p.down} down</b>` : ''} · ${p.podsReady}/${p.pods} pods ready${p.restarts1h ? ` · <b class="w">${p.restarts1h} restarts</b>` : ''}</div>
        <div class="p3 ${p.top ? p.top.severity : ''}">${p.top ? `${esc(p.top.subjectName)}: ${esc(p.top.title)}` : 'No problems'}</div></button>`;
    }).join('') || '<div class="empty">No projects in view.</div>';
  }

  // ------------------------------------------------------------------ tabs
  _tabs() {
    const real = this.groups.filter((g) => g.severity !== 'info');
    const info = this.groups.length - real.length;
    const wl = this.graph.nodes.filter((n) => n.kind === 'workload' && !n.ghost).length;
    const calls = this.graph.edges.filter((e) => e.type !== 'exposes').length;
    const cats = new Map();
    for (const g of (this.showInfo ? this.groups : real)) for (const c of g.categories) cats.set(c, (cats.get(c) || 0) + 1);
    const order = Object.keys(CATEGORY).filter((c) => cats.has(c));
    const tab = (id, label, n) => `<button class="${this.tab === id ? 'on' : ''}" data-tab="${id}">${label} <span>${n}</span></button>`;
    this.el.querySelector('.tg-tabs').innerHTML = `
      <div class="seg">${tab('problems', 'Problems', real.length)}${tab('workloads', 'Workloads', wl)}${tab('calls', 'Calls', calls)}</div>
      ${this.tab === 'problems' ? `<div class="cats"><button class="${!this.category ? 'on' : ''}" data-cat="">All</button>
        ${order.map((c) => `<button class="${this.category === c ? 'on' : ''}" data-cat="${c}">${CATEGORY[c]} <span>${cats.get(c)}</span></button>`).join('')}</div>
        <label class="tg-info" title="Hardening gaps and other things that are not failures"><input type="checkbox" id="tgInfo" ${this.showInfo ? 'checked' : ''}> Show ${info} informational</label>` : ''}
      <input id="tgSearch" type="search" placeholder="Filter…" value="${esc(this.query)}" autocomplete="off">`;
  }

  _content() {
    const box = this.el.querySelector('.tg-content');
    box.innerHTML = this.tab === 'workloads' ? this._workloads() : this.tab === 'calls' ? this._calls() : this._problems();
  }

  // ------------------------------------------------------------------ problems
  _problems() {
    const q = this.query;
    const list = this.groups.filter((g) => (this.showInfo || g.severity !== 'info')
      && (!this.category || g.categories.has(this.category))
      && (!q || [g.head.subjectName, g.head.namespace, ...g.findings.map((f) => f.title + ' ' + f.cause)].join(' ').toLowerCase().includes(q)));
    if (!list.length) {
      return `<div class="tg-clear">${this.groups.some((g) => g.severity !== 'info') ? 'Nothing matches this filter.'
        : '<b>Nothing is failing.</b> Rollouts are complete, pods are ready, calls are answering in time.'}</div>`;
    }
    return list.map((g) => this._card(g)).join('');
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
      ${g.findings.filter((f) => this.showInfo || f.severity !== 'info' || g.severity === 'info').map((f) => this._finding(f)).join('')}
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
      ${f.fix ? `<p class="fix"><b>Next</b>${esc(f.fix)}</p>` : ''}
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

  // ------------------------------------------------------------------ workloads table
  _workloads() {
    const edges = this.graph.edges;
    const rows = this.graph.nodes.filter((n) => n.kind === 'workload' && !n.ghost).map((n) => {
      const inbound = edges.filter((e) => e.target === n.id && e.type !== 'exposes');
      const outbound = edges.filter((e) => e.source === n.id);
      const worstOut = outbound.filter((e) => e.latency.estimateMs != null).sort((a, b) => b.latency.estimateMs - a.latency.estimateMs)[0];
      const top = (this.graph.findings || []).find((f) => f.subject === n.id && f.severity !== 'info' && !f.effectOf);
      const inherit = n.risk.impactLevel !== 'ok' && n.risk.level === 'ok';
      return {
        n, risk: n.risk.score + (n.risk.impact || 0) * 0.3, name: n.name, ns: n.namespace,
        ready: n.replicas.desired ? n.replicas.ready / n.replicas.desired : 1,
        restarts: n.pods.reduce((s, p) => s + (p.terminal ? 0 : p.recentRestarts || 0), 0),
        cpu: n.usage?.cpuRatio ?? -1, mem: n.usage?.memRatio ?? -1,
        inP95: Math.max(-1, ...inbound.map((e) => e.latency.p95Ms ?? -1)),
        inErr: Math.max(0, ...inbound.map((e) => e.health.errorRate || 0)),
        rps: inbound.reduce((s, e) => s + (e.traffic.rps || 0), 0),
        out: worstOut ? worstOut.latency.estimateMs : -1, outName: worstOut ? this.byId.get(worstOut.target)?.name : '',
        age: n.rollout?.ageSeconds ?? 1e12,
        issue: top ? top.title : inherit ? `Slowed by ${this._name(n.risk.impactFrom)}` : '',
        issueLvl: top ? top.severity : inherit ? n.risk.impactLevel : 'ok',
      };
    }).filter((r) => !this.query || `${r.name} ${r.ns} ${r.issue}`.toLowerCase().includes(this.query));
    const [col, dir] = this.sort.workloads;
    rows.sort((a, b) => (typeof a[col] === 'string' ? a[col].localeCompare(b[col]) * -dir : (a[col] - b[col]) * dir) || b.risk - a.risk);
    const th = (id, label, title = '') => `<th data-sort="workloads:${id}" class="${col === id ? 'on' : ''}" title="${esc(title)}">${label}${col === id ? (dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
    const pct = (r) => (r < 0 ? '<td class="num dim">–</td>' : `<td class="num ${r >= 0.9 ? 'c' : r >= 0.75 ? 'w' : ''}">${Math.round(r * 100)}%</td>`);
    const ROLL = { complete: '', progressing: 'w', stuck: 'c', failed: 'c', paused: 'w' };
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by severity')}${th('name', 'Workload')}${th('ns', 'Project')}
      ${th('ready', 'Ready', 'Ready pods / desired')}${th('age', 'Last rollout', 'State of the latest rollout and when it started')}
      ${th('restarts', 'Restarts', 'Container restarts in the last hour')}${th('cpu', 'CPU', 'Highest pod CPU as a share of its limit')}${th('mem', 'Mem', 'Highest pod memory as a share of its limit')}
      ${th('rps', 'Req/s', 'Inbound requests per second (mesh)')}${th('inP95', 'p95 in', 'Slowest inbound request p95: what callers experience')}${th('inErr', 'Err in', 'Highest inbound 5xx rate')}
      ${th('out', 'Slowest dependency', 'Its slowest downstream call')}<th>Top issue</th></tr></thead><tbody>
      ${rows.map((r) => {
        const n = r.n, ro = n.rollout;
        const lvl = n.risk.level !== 'ok' ? n.risk.level : n.risk.impactLevel !== 'ok' ? 'inherit' : 'ok';
        return `<tr data-node="${esc(n.id)}"><td><span class="dot ${lvl}" title="${lvl === 'inherit' ? 'Healthy itself; slowed by a dependency' : lvl}"></span></td>
          <td class="nm">${esc(n.name)}<span class="k">${esc(n.subkind)}</span></td>
          <td><span class="ns" style="--c:${nsColor(n.namespace, n.platform)}">${esc(n.namespace)}</span></td>
          <td class="num ${r.ready === 0 && n.replicas.desired ? 'c' : r.ready < 1 ? 'w' : ''}">${n.replicas.ready}/${n.replicas.desired}</td>
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

  // ------------------------------------------------------------------ calls table (latency, upstream → downstream)
  _calls() {
    const rows = this.graph.edges.filter((e) => e.type !== 'exposes').map((e) => {
      const s = this.byId.get(e.source), t = this.byId.get(e.target);
      const f = (this.graph.findings || []).find((x) => x.subject === e.id && x.severity !== 'info');
      return {
        e, s, t, risk: e.risk.score, from: s?.name || '', to: t?.name || '', rps: e.traffic.rps ?? -1,
        p50: e.latency.p50Ms ?? -1, p95: e.latency.p95Ms ?? -1, err: e.health.errorRate || 0, rtt: e.latency.rttMinMs ?? -1,
        bad: (e.health.retrans || 0) + (e.health.pending || 0) * 10, conns: e.traffic.connections ?? -1,
        note: f ? (f.root ? f.title.split(': ').slice(1).join(': ') || f.title : `symptom of ${f.causedByName}`)
          : e.traffic.idle ? 'idle' : (!e.evidence.includes('observed') && !e.evidence.includes('mesh')) ? 'declared in config, no traffic seen' : '',
      };
    }).filter((r) => !this.query || `${r.from} ${r.to} ${r.note}`.toLowerCase().includes(this.query));
    const [col, dir] = this.sort.calls;
    rows.sort((a, b) => (typeof a[col] === 'string' ? a[col].localeCompare(b[col]) * -dir : (a[col] - b[col]) * dir) || b.p95 - a.p95);
    const th = (id, label, title = '') => `<th data-sort="calls:${id}" class="${col === id ? 'on' : ''}" title="${esc(title)}">${label}${col === id ? (dir < 0 ? ' ▾' : ' ▴') : ''}</th>`;
    const ms = (v, warn, crit) => (v < 0 ? '<td class="num dim">–</td>' : `<td class="num ${v > crit ? 'c' : v > warn ? 'w' : ''}">${fmtMs(v)}</td>`);
    return `<table class="tg-table"><thead><tr>${th('risk', '', 'Sort by severity')}${th('from', 'Caller (upstream)')}${th('to', 'Callee (downstream)')}
      ${th('rps', 'Req/s')}${th('conns', 'Conns', 'Open connections seen by the node agent')}${th('p50', 'p50')}${th('p95', 'p95', 'Request latency from mesh metrics')}
      ${th('err', '5xx')}${th('rtt', 'Path RTT', 'Network round trip (kernel minimum): the path, not the app')}${th('bad', 'Loss', 'Retransmitting segments / unanswered connection attempts')}
      <th>Evidence</th><th>Note</th></tr></thead><tbody>
      ${rows.map((r) => {
        const e = r.e;
        return `<tr data-edge="${esc(e.id)}"><td><span class="dot ${e.risk.level}"></span></td>
          <td class="nm">${esc(r.from)}<span class="k">${esc(r.s?.namespace || r.s?.kind || '')}</span></td>
          <td class="nm">${esc(r.to)}<span class="k">${esc(r.t?.namespace || (r.t?.kind === 'external' ? (r.t.scope || 'external') : r.t?.kind) || '')}</span></td>
          <td class="num ${r.rps < 0 ? 'dim' : ''}">${r.rps < 0 ? '–' : fmtNum(r.rps)}</td><td class="num ${r.conns < 0 ? 'dim' : ''}">${r.conns < 0 ? '–' : r.conns}</td>
          ${ms(r.p50, 100, 500)}${ms(r.p95, 300, 1000)}
          <td class="num ${r.err > 0.05 ? 'c' : r.err > 0.01 ? 'w' : 'dim'}">${r.err ? fmtPct(r.err) : '–'}</td>
          ${r.t?.kind === 'external' ? `<td class="num ${r.rtt < 0 ? 'dim' : ''}">${r.rtt < 0 ? '–' : fmtMs(r.rtt)}</td>` : ms(r.rtt, 2, 10)}
          <td class="num ${e.health.pending ? 'c' : e.health.retrans ? 'w' : 'dim'}">${e.health.pending ? e.health.pending + ' unanswered' : e.health.retrans ? e.health.retrans + ' retrans' : '–'}</td>
          <td>${e.evidence.map((x) => `<span class="ev ${x}">${x}</span>`).join(' ')}</td>
          <td class="issue ${e.risk.level === 'ok' ? 'ok' : e.risk.level}">${esc(r.note)}</td></tr>`;
      }).join('')}</tbody></table>`;
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
    const t = ev.target.closest('[data-copy],[data-tab],[data-cat],[data-sort],[data-project],[data-platform],[data-pod],[data-edge],[data-node],#tgInfo');
    if (!t) return;
    const d = t.dataset;
    if (d.copy != null) { this.ctx.copy(d.copy); t.textContent = 'copied'; setTimeout(() => { t.textContent = 'copy'; }, 1200); return; }
    if (d.tab) { this.tab = d.tab; this._tabs(); this._content(); return; }
    if (d.cat != null) { this.category = d.cat || null; this._tabs(); this._content(); return; }
    if (t.id === 'tgInfo') { this.showInfo = t.checked; this._tabs(); this._content(); return; }
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

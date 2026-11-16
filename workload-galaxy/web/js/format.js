// Formatting and small shared helpers.

export const LEVEL_COLOR = { ok: '#5ee6a8', warning: '#ffb547', critical: '#ff4d6d', evicted: '#6b7591' };

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// "Ways to fix it": the sizing advisor's options for a finding, each with what it is based on, what
// would stop it working, and a command to copy. Shared by the Triage cards and the drawer.
export function optionsHtml(f) {
  if (!f.options || !f.options.length) return '';
  const row = (o) => `<li><div class="w-t">${esc(o.title)}${o.admin ? '<span class="w-adm" title="Needs rights most project owners do not have">cluster admin</span>' : ''}</div>
    ${o.detail ? `<p>${esc(o.detail)}</p>` : ''}
    ${o.note ? `<p class="w-note">${esc(o.note)}</p>` : ''}
    ${o.command ? `<div class="cmd"><code>${esc(o.command)}</code><button class="link" data-copy="${esc(o.command)}">copy</button></div>` : ''}</li>`;
  return `<div class="ways"><div class="ways-h">Ways to fix it</div><ol>${f.options.map(row).join('')}</ol>
    ${f.optionsNote ? `<p class="w-foot">${esc(f.optionsNote)}</p>` : ''}</div>`;
}

export function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return '–';
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(1) + 'G';
  if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (a >= 1e4) return (n / 1e3).toFixed(0) + 'k';
  if (a >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  if (a >= 100 || Number.isInteger(n)) return Math.round(n).toString();
  return n.toFixed(1);
}

export function fmtMs(ms) {
  if (ms == null) return '–';
  if (ms >= 1000) return (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + ' s';
  if (ms >= 100) return Math.round(ms) + ' ms';
  if (ms >= 10) return ms.toFixed(0) + ' ms';
  return ms.toFixed(ms >= 1 ? 1 : 2) + ' ms';
}

export function fmtBytes(b) {
  if (b == null) return '–';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (b >= 10 || i === 0 ? Math.round(b) : b.toFixed(1)) + ' ' + u[i];
}

export function fmtRate(bps) {
  if (!bps) return null;
  return fmtBytes(bps) + '/s';
}

export function fmtCpu(c) {
  if (c == null) return '–';
  return c >= 1 ? c.toFixed(2) + ' cores' : Math.round(c * 1000) + 'm';
}

export function fmtPct(r) {
  return r == null ? '–' : Math.round(r * 100) + '%';
}

export function fmtAge(s) {
  if (s == null) return '–';
  if (s < 120) return s + 's';
  if (s < 7200) return Math.round(s / 60) + 'm';
  if (s < 172800) return Math.round(s / 3600) + 'h';
  return Math.round(s / 86400) + 'd';
}

// Stable string hash -> [0,1)
export function hash01(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 100000) / 100000;
}

// Namespace hue: stable, spread around the wheel, avoiding the warning/critical reds.
export function nsColor(ns, platform = false) {
  if (!ns) return '#9fb3ff';
  const hue = 170 + hash01(ns) * 150; // 170..320: teal -> blue -> violet -> magenta
  return `hsl(${hue.toFixed(0)}, ${platform ? 25 : 85}%, ${platform ? 62 : 66}%)`;
}

// Short metric line for a node label: inbound rate · latency, like "31.5k rps · p95 1.8 ms".
export function nodeMetric(node, inbound) {
  if (!inbound || !inbound.length) return '';
  let rps = 0, conns = 0, lat = null, basis = '';
  for (const e of inbound) {
    rps += e.traffic.rps || 0;
    conns += e.traffic.connections || 0;
    const l = e.latency.p95Ms ?? e.latency.rttMinMs ?? e.latency.connectP50Ms;
    if (l != null && (lat == null || l > lat)) { lat = l; basis = e.latency.p95Ms != null ? 'p95' : (e.latency.rttMinMs != null ? 'rtt' : 'conn'); }
  }
  const parts = [];
  if (rps) parts.push(fmtNum(rps) + ' rps');
  else if (conns) parts.push(fmtNum(conns) + ' conn');
  if (lat != null) parts.push(`${basis} ${fmtMs(lat)}`);
  return parts.join(' · ');
}

export function edgeMetric(e) {
  const parts = [];
  if (e.traffic.idle) parts.push(`idle ${fmtAge(Math.round(e.traffic.lastActiveS))}`);
  if (e.traffic.rps) parts.push(fmtNum(e.traffic.rps) + ' rps');
  else if (e.traffic.connections) parts.push(fmtNum(e.traffic.connections) + ' conn');
  if (e.traffic.bytesPerSec) parts.push(fmtRate(e.traffic.bytesPerSec));
  if (e.latency.p95Ms != null) parts.push('p95 ' + fmtMs(e.latency.p95Ms));
  else if (e.latency.rttMinMs != null) parts.push('rtt ' + fmtMs(e.latency.rttMinMs));
  else if (e.latency.connectP50Ms != null) parts.push('connect ' + fmtMs(e.latency.connectP50Ms));
  if (e.health && e.health.errorRate) parts.push(fmtPct(e.health.errorRate) + ' 5xx');
  return parts.join(' · ');
}

export function podLevel(p) {
  if (p.level) return p.level;   // server-side diagnosis (risk.score_pod)
  const bad = ['CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 'OOMKilled', 'Unschedulable', 'RunContainerError'];
  if (bad.includes(p.reason)) return 'critical';
  if (!p.ready || p.recentRestarts > 0 || p.phase !== 'Running') return 'warning';
  return 'ok';
}

// The pods of a workload that are drawn. A DaemonSet on 400 nodes or a 300-replica Deployment is
// still one star / one tower: the worst pods are shown, the rest are counted in the label and drawer.
export function visiblePods(n, max = 36) {
  if (!n.pods || n.pods.length <= max) return n.pods || [];
  const rank = { critical: 0, warning: 1, evicted: 2, ok: 3 };
  return [...n.pods].sort((a, b) => (rank[podLevel(a)] ?? 3) - (rank[podLevel(b)] ?? 3)).slice(0, max);
}

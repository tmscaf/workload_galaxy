// Formatting and small shared helpers.

export const LEVEL_COLOR = { ok: '#5ee6a8', warning: '#ffb547', critical: '#ff4d6d', evicted: '#6b7591', offline: '#6b7591' };

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------------ numbers
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

const plural = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : (many || one + 's')}`;
export { plural };

// A rate in BITS per second: "850 bps", "24.4 kbps", "1.2 Mbps", "1.05 Gbps".
export function fmtBits(bps) {
  if (bps == null || Number.isNaN(bps)) return '–';
  const a = Math.abs(bps);
  if (a < 1) return '0 bps';
  if (a < 1e3) return Math.round(bps) + ' bps';
  if (a < 1e6) return (bps / 1e3).toFixed(a >= 1e5 ? 0 : 1) + ' kbps';
  if (a < 1e9) return (bps / 1e6).toFixed(a >= 1e8 ? 0 : 1) + ' Mbps';
  return (bps / 1e9).toFixed(2) + ' Gbps';
}

// A rate the back end gives in BYTES per second. Network rates are shown in bits per second, as
// people quote link speeds; disk rates in bytes per second, as people quote disks.
export function fmtByteRate(bytesPerSec, disk = false) {
  if (bytesPerSec == null) return '–';
  if (!disk) return fmtBits(bytesPerSec * 8);
  const a = Math.abs(bytesPerSec);
  if (a < 1) return '0 B/s';
  if (a < 1e3) return Math.round(bytesPerSec) + ' B/s';
  if (a < 1e6) return (bytesPerSec / 1e3).toFixed(a >= 1e5 ? 0 : 1) + ' kB/s';
  if (a < 1e9) return (bytesPerSec / 1e6).toFixed(a >= 1e8 ? 0 : 1) + ' MB/s';
  return (bytesPerSec / 1e9).toFixed(2) + ' GB/s';
}

// A share given as 0..1: "7%", "0.4%".
export function fmtPct(x) {
  if (x == null || Number.isNaN(x)) return '–';
  const v = x * 100;
  return (v > 0 && v < 1 ? v.toFixed(1) : Math.round(v)) + '%';
}

// Memory and disk sizes in bytes, the way vSphere shows them: binary units labelled MB / GB / TB.
export function fmtGB(b) {
  if (b == null || Number.isNaN(b)) return '–';
  const gb = b / 1073741824;
  if (gb >= 1024) return trim(gb / 1024, 2) + ' TB';
  if (gb >= 10) return Math.round(gb) + ' GB';
  if (gb >= 1) return trim(gb, 1) + ' GB';
  return Math.round(b / 1048576) + ' MB';
}

// An amount of data in bytes: "512 B", "3.4 GB".
export function fmtBytes(b) {
  if (b == null || Number.isNaN(b)) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? Math.round(b) : trim(b, b >= 100 ? 0 : 1)) + ' ' + u[i];
}

function trim(v, digits) {
  const s = v.toFixed(digits);
  return digits && s.endsWith('.' + '0'.repeat(digits)) ? s.slice(0, -digits - 1) : s;
}

// A length of time in seconds: "40 s", "5 min", "3 h", "2 days".
export function fmtDur(s) {
  if (s == null || Number.isNaN(s)) return '–';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' s';
  if (s < 5400) return Math.round(s / 60) + ' min';
  if (s < 129600) return Math.round(s / 3600) + ' h';
  const d = Math.round(s / 86400);
  return d + (d === 1 ? ' day' : ' days');
}

export function fmtAgo(s) {
  if (s == null || Number.isNaN(s)) return '';
  return s < 5 ? 'just now' : fmtDur(s) + ' ago';
}

// A moment given as epoch seconds: "12 Nov 2026, 14:05".
export function fmtWhen(epoch) {
  if (!epoch) return '–';
  const d = new Date(epoch * 1000);
  if (Number.isNaN(d.getTime())) return '–';
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export const isUrl = (s) => /^https?:\/\//i.test(String(s || ''));

// A link that leaves the dashboard (always a new tab).
export function linkHtml(url, text, cls = 'out') {
  return `<a class="${cls}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>`;
}

function linkLabel(url) {
  try { return 'Open ' + new URL(url).host; } catch { return 'Open link'; }
}

// "Ways to fix it": the options attached to a finding, each with what it involves and what to check
// first. A command is shown to copy; a link is shown as a link. Shared by the Triage cards and the drawer.
export function optionsHtml(f) {
  if (!f.options || !f.options.length) return '';
  const cmd = (c) => (isUrl(c) ? `<div class="w-link">${linkHtml(c, linkLabel(c))}</div>`
    : `<div class="cmd"><code>${esc(c)}</code><button class="link" data-copy="${esc(c)}">copy</button></div>`);
  const row = (o) => `<li><div class="w-t">${esc(o.title)}</div>
    ${o.detail ? `<p>${esc(o.detail)}</p>` : ''}
    ${o.note ? `<p class="w-note">${esc(o.note)}</p>` : ''}
    ${o.command ? cmd(o.command) : ''}</li>`;
  return `<div class="ways"><div class="ways-h">Ways to fix it</div><ol>${f.options.map(row).join('')}</ol>
    ${f.optionsNote ? `<p class="w-foot">${esc(f.optionsNote)}</p>` : ''}</div>`;
}

// Stable string hash -> [0,1)
export function hash01(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 100000) / 100000;
}

// Group hue: stable, spread around the wheel, avoiding the warning/critical reds.
export function nsColor(ns, muted = false) {
  if (!ns) return '#9fb3ff';
  const hue = 170 + hash01(ns) * 150; // 170..320: teal -> blue -> violet -> magenta
  return `hsl(${hue.toFixed(0)}, ${muted ? 25 : 85}%, ${muted ? 62 : 66}%)`;
}

// ------------------------------------------------------------------ graph helpers
// A VM that is registered but powered off. Not a failure.
export const isOffline = (n) => !!n && n.kind === 'workload' && !n.aggregate && n.present === false;

// Rate now on an attachment, as text.
export function edgeRate(e) {
  const t = e?.traffic || {};
  if (!t.bytesPerSec) return '';
  return e.net?.rateText || fmtByteRate(t.bytesPerSec, e.type === 'stores');
}

// The short line under a node's name on the map.
export function nodeMetric(n) {
  if (!n) return '';
  if (n.kind === 'workload') {
    const v = n.vm;
    if (!v) return '';
    if (!v.on) return 'powered off';
    return `${fmtPct(v.cpuPct)} CPU · ${fmtGB(v.memMB * 1048576)}`;
  }
  if (n.ds) return `${fmtPct(n.ds.usedRatio)} used · ${fmtGB(n.ds.free)} free`;
  if (n.pg) return [n.pg.vlan ? n.pg.vlanText : '', n.pg.bytesPerSec ? fmtByteRate(n.pg.bytesPerSec) : ''].filter(Boolean).join(' · ');
  if (n.nic) return n.nic.speedText || '';
  return '';
}

export function podLevel(p) {
  return p.level || 'ok';
}

// The members of a node that are drawn (a VM's slot and disks, or the VMs of a rolled-up group). A big group is still one star: the worst members are shown, the rest are counted.
export function visiblePods(n, max = 36) {
  if (!n.pods || n.pods.length <= max) return n.pods || [];
  const rank = { critical: 0, warning: 1, evicted: 2, ok: 3 };
  return [...n.pods].sort((a, b) => (rank[podLevel(a)] ?? 3) - (rank[podLevel(b)] ?? 3)).slice(0, max);
}

// Re-render something without losing the text field the person is typing in.
export function keepFocus(fn) {
  const a = document.activeElement;
  const key = a?.dataset?.focus;
  const sel = key && typeof a.selectionStart === 'number' ? [a.selectionStart, a.selectionEnd] : null;
  fn();
  if (!key) return;
  const el = document.querySelector(`[data-focus="${CSS.escape(key)}"]`);
  if (el && el !== document.activeElement) {
    el.focus();
    if (sel) try { el.setSelectionRange(sel[0], sel[1]); } catch { /* not a text field */ }
  }
}

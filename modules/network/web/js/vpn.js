// The VPN panel: who is connected to the router's VPN now, and what the VPN server has seen lately.
//
// Data: GET api/v1/vpn (the router's once-a-minute status report plus its forwarded VPN log).
// Read-only: nothing here can disconnect anyone or change the VPN.
import { esc, fmtAgo, fmtBits, fmtBytes, fmtDur, fmtNum, fmtWhen, plural } from './format.js';

const KIND = {
  connected: { icon: '●', cls: 'ok', text: 'connected' },
  disconnected: { icon: '○', cls: 'off', text: 'disconnected' },
  failed: { icon: '✕', cls: 'bad', text: 'failed login' },
};

export class VpnPanel {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;          // { onShowOnMap(nodeId) }
    this.data = null;
  }

  // Badge on the VPN tab: how many are connected right now.
  static badge(data) {
    const n = data?.connected || 0;
    return { count: n, level: 'vpn', title: n ? `${plural(n, 'VPN connection')} right now` : 'Nobody on the VPN' };
  }

  render(data, graph) {
    this.data = data;
    if (!data) { this.el.innerHTML = '<div class="vp-empty">Loading…</div>'; return; }
    const nodeFor = new Map((graph?.nodes || []).filter((n) => n.vpn?.key).map((n) => [n.vpn.key, n]));
    const now = Date.now() / 1000;
    const rep = data.report || {};
    const log = data.log || {};
    const srv = data.server || {};
    const n = data.connected || 0;

    let source;
    if (rep.lastS == null) source = '<span class="w">No status report from the router yet.</span> Its sync script reports once a minute.';
    else if (rep.stale) source = `<span class="w">The router stopped reporting ${esc(fmtAgo(rep.lastS))}.</span> Who is shown may be out of date.`;
    else source = `Updated by the router ${esc(fmtAgo(rep.lastS))}.`;
    const logText = log.lastS == null ? 'The VPN log has not arrived yet, so connects and failed logins show only with the next report.'
      : `VPN log arriving (last line ${esc(fmtAgo(log.lastS))}).`;

    const tile = (v, k, cls = '', title = '') => `<div class="vp-tile ${cls}" title="${esc(title)}"><div class="v">${v}</div><div class="k">${k}</div></div>`;

    let html = `<div class="vp-head">
        <div class="vp-verdict ${n ? 'on' : ''}"><span class="orb"></span><div>
          <div class="v1">${n ? `${plural(n, 'connection')} on the VPN` : 'Nobody is on the VPN'}</div>
          <div class="v2">${esc(srv.name || 'OpenVPN server')}${srv.port ? ` · port ${esc(srv.port)}` : ''} · ${source}</div>
        </div></div>
        <div class="vp-tiles">
          ${tile(fmtNum(n), 'connected now', n ? 'on' : '')}
          ${tile(fmtNum(data.failed24h || 0), 'failed logins · 24 h', data.failed24h ? 'w' : '', 'Someone reached the VPN with a valid key but the wrong user name or password, or the server refused the login')}
          ${tile(fmtNum(data.probes24h || 0), 'knocks turned away · 24 h', '', 'Packets from the internet without your VPN key. The server ignores them; this is normal background noise.')}
        </div>
      </div>
      <div class="vp-note">${logText}</div>`;

    html += '<h2 class="vp-h">Connected now</h2>';
    if (!n) {
      html += '<div class="vp-empty">No one is connected. When someone connects it shows here and on the map, in the <b>VPN connections</b> group next to the router.</div>';
    } else {
      html += `<div class="vp-sessions">${data.sessions.map((s) => {
        const node = nodeFor.get(s.key);
        const total = (s.bytesIn || 0) + (s.bytesOut || 0);
        return `<div class="vp-card">
          <div class="c1"><span class="dot on"></span><span class="who">${esc(s.user || 'unknown user')}</span>
            <span class="what">${esc([s.platform, s.app].filter(Boolean).join(' · ') || 'device not identified')}</span>
            ${!s.confirmed ? '<span class="tag" title="Seen in the VPN log; the router\'s next status report confirms it">just connected</span>' : ''}</div>
          <dl class="kv">
            <dt>Connected</dt><dd>${s.durationS != null ? `${esc(fmtDur(s.durationS))} <span class="dim">· since ${esc(fmtWhen(s.since))}</span>` : '–'}</dd>
            <dt>From</dt><dd><span class="mono">${esc(s.real || '–')}</span>${s.where ? ` <span class="dim">· ${esc(s.where)}</span>` : ''}</dd>
            <dt>VPN address</dt><dd class="mono">${esc(s.tunnel || '–')}</dd>
            <dt>Right now</dt><dd>↓ ${esc(fmtBits(s.outBps || 0))} to it · ↑ ${esc(fmtBits(s.inBps || 0))} from it</dd>
            <dt>Data so far</dt><dd>${esc(fmtBytes(total))} <span class="dim">· ${esc(fmtBytes(s.bytesOut || 0))} sent to it, ${esc(fmtBytes(s.bytesIn || 0))} from it</span></dd>
            ${s.cert && s.cert !== s.user ? `<dt>Certificate</dt><dd class="mono">${esc(s.cert)}</dd>` : ''}
            ${s.cipher ? `<dt>Encryption</dt><dd>${esc(s.cipher)}</dd>` : ''}
          </dl>
          ${node ? `<button class="btn small" data-map="${esc(node.id)}">Show on the map ▸</button>` : ''}
        </div>`;
      }).join('')}</div>`;
    }

    const events = data.events || [];
    html += '<h2 class="vp-h">Recent activity</h2>';
    if (!events.length) {
      html += '<div class="vp-empty">Nothing yet. Connects, disconnects and failed logins appear here as the router reports them.</div>';
    } else {
      html += `<ul class="vp-events">${events.slice(0, 60).map((e) => {
        const k = KIND[e.kind] || { icon: '·', cls: '', text: e.kind };
        const bits = [
          e.real ? `from ${e.real}${e.where ? ` (${e.where})` : ''}` : '',
          e.platform ? e.platform : '',
          e.tunnel && e.kind === 'connected' ? `VPN address ${e.tunnel}` : '',
          e.duration != null ? `after ${fmtDur(e.duration)}` : '',
          e.data ? `${fmtBytes(e.data)} used` : '',
          e.note && e.kind === 'failed' ? e.note : '',
        ].filter(Boolean);
        return `<li class="${k.cls}"><span class="ic">${k.icon}</span>
          <span class="t"><b>${esc(e.user || 'someone')}</b> ${esc(k.text)}</span>
          <span class="d">${esc(bits.join(' · '))}</span>
          <span class="when" title="${esc(fmtWhen(e.at))}">${esc(fmtAgo(Math.max(0, now - e.at)))}</span></li>`;
      }).join('')}</ul>`;
    }

    const knocks = data.probeSources || [];
    if (knocks.length) {
      html += `<h2 class="vp-h">Knocking on the VPN port</h2>
        <div class="vp-note">Addresses on the internet that sent packets without your VPN key since the app started. The VPN server drops them unanswered,
        so this is background noise from scanners, not a sign anyone got in.</div>
        <ul class="vp-knocks">${knocks.map((k) => `<li><span class="mono">${esc(k.ip)}</span><span class="dim">${plural(k.count, 'packet')}</span></li>`).join('')}</ul>`;
    }

    this.el.innerHTML = html;
    this.el.querySelectorAll('[data-map]').forEach((b) => b.addEventListener('click', () => this.ctx.onShowOnMap(b.dataset.map)));
  }
}

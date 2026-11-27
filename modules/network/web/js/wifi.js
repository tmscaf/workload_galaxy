// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// The Wi-Fi panel: where does a connection break?
//
// Data: GET api/v1/wifi (the path every device shares, access points, Wi-Fi devices, events) and
// GET api/v1/wifi/device?mac= (one device: verdict, path hop by hop, 24 hours of history).
// The only things it can change are which devices are pinged more often ("watch") and running a
// short ping test; neither touches the Wi-Fi controller or the network.
import { esc, fmtAgo, fmtBits, fmtDur, fmtNum, fmtWhen, plural } from './format.js';

const STATE = {
  ok: { icon: '●', cls: 'ok', text: 'healthy' },
  warn: { icon: '▲', cls: 'warn', text: 'worth a look' },
  bad: { icon: '✕', cls: 'bad', text: 'problem' },
  down: { icon: '✕', cls: 'bad', text: 'not answering' },
  unknown: { icon: '○', cls: 'off', text: 'no data yet' },
};
const WHERE = {
  none: 'Healthy', signal: 'Weak signal', air: 'Crowded or noisy air', ap: 'Access point', drops: 'Keeps dropping',
  device: 'The device itself', router: 'Router', isp: 'Internet line', internet: 'Internet', dns: 'Name lookups', offline: 'Off the Wi-Fi',
};
const EVENT = {
  connected: { icon: '●', cls: 'ok', text: 'joined' }, disconnected: { icon: '○', cls: 'off', text: 'left' },
  reconnected: { icon: '↻', cls: 'warn', text: 'dropped and re-joined' }, roamed: { icon: '⇄', cls: '', text: 'moved' },
  'gear-offline': { icon: '✕', cls: 'bad', text: 'went offline' }, 'gear-online': { icon: '●', cls: 'ok', text: 'came back' },
  'gear-restarted': { icon: '↻', cls: 'warn', text: 'restarted' }, channel: { icon: '≈', cls: '', text: 'changed channel' },
  interference: { icon: '▲', cls: 'warn', text: 'interference' }, radar: { icon: '▲', cls: 'warn', text: 'radar detected' },
};

const ms = (v) => (v == null ? '–' : v >= 100 ? `${Math.round(v)} ms` : v >= 10 ? `${v.toFixed(0)} ms` : `${v.toFixed(1)} ms`);
const pct = (v) => (v == null ? '–' : `${v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)}%`);
const clock = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
const chip = (state, text) => {
  const s = STATE[state] || STATE.unknown;
  return `<span class="wf-chip ${s.cls}"><span class="ic">${s.icon}</span>${esc(text || s.text)}</span>`;
};
const sigClass = (v, lim) => (v == null ? '' : v <= lim.signalBad ? 'bad' : v <= lim.signalWarn ? 'warn' : 'ok');

export class WifiPanel {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;          // { getJSON(path), send(path, body), onShowOnMap(nodeId), nodeFor(mac), onState() }
    this.data = null;
    this.mac = null;         // the device whose detail is open
    this.detail = null;
    this.test = null;        // { mac, running, result, error }
    this.filter = '';
    this.onlyProblems = false;
    this.charts = new Map(); // chart id -> { rows, x0, x1, w, pad, fmt }
    this.tip = document.createElement('div');
    this.tip.className = 'wf-tip';
    this.tip.hidden = true;
    document.body.appendChild(this.tip);
    el.addEventListener('click', (ev) => this._click(ev));
    el.addEventListener('input', (ev) => {
      if (ev.target.id === 'wfFilter') { this.filter = ev.target.value; this._rows(); }
    });
    el.addEventListener('mousemove', (ev) => this._hover(ev));
    el.addEventListener('mouseleave', () => { this.tip.hidden = true; });
  }

  // Badge on the Wi-Fi tab: what is wrong right now.
  static badge(data) {
    if (!data) return { count: 0, level: '', title: '' };
    const up = data.upstream?.level;
    const s = data.summary || {};
    const count = (up === 'bad' || up === 'warn' ? 1 : 0) + (s.apsDown || 0) + (s.bad || 0);
    const level = up === 'bad' || s.apsDown ? 'crit' : count ? 'warn' : '';
    return { count, level, title: count ? `${plural(count, 'connection problem')} right now` : 'No connection problems right now' };
  }

  // Open one device's detail (from the map's drawer, or a row here).
  async open(mac) {
    this.mac = mac;
    this.detail = null;
    this.test = null;
    this.ctx.onState?.(mac);
    this.render(this.data);
    await this._loadDetail();
  }

  async _loadDetail() {
    const mac = this.mac;
    if (!mac) return;
    try {
      const d = await this.ctx.getJSON('api/v1/wifi/device?mac=' + encodeURIComponent(mac));
      if (this.mac !== mac) return;
      this.detail = d;
    } catch (err) {
      if (this.mac !== mac) return;
      this.detail = { error: err.message };
    }
    this.render(this.data);
  }

  render(data, refetch = false) {
    this.data = data;
    if (refetch && this.mac) { this._loadDetail(); return; }
    const scroll = this.el.scrollTop;
    this.charts.clear();
    this.tip.hidden = true;
    if (!data) this.el.innerHTML = '<div class="vp-empty">Loading…</div>';
    else this.el.innerHTML = this.mac ? this._detailHtml() : this._overviewHtml();
    this.el.scrollTop = scroll;
    if (!this.mac) this._rows();
  }

  // ------------------------------------------------------------------ overview
  _overviewHtml() {
    const d = this.data;
    const up = d.upstream || { hops: [] };
    const s = d.summary || {};
    const ctl = d.controller || {};
    const poor = (s.bad || 0) + (s.warn || 0);
    let level = up.level === 'bad' || s.apsDown || s.bad ? 'bad' : up.level === 'warn' || s.warn || s.gearProblems ? 'warn' : up.level === 'unknown' && !s.clients ? 'unknown' : 'ok';
    let title;
    if (up.level === 'bad' || up.level === 'warn') title = up.title;
    else if (s.apsDown) title = `${plural(s.apsDown, 'access point')} offline`;
    else if (s.bad) title = `${plural(s.bad, 'Wi-Fi device')} with a bad connection`;
    else if (poor || s.gearProblems) title = `${poor ? plural(poor, 'Wi-Fi device') : plural(s.gearProblems, 'access point')} worth a look`;
    else if (level === 'unknown') title = 'Taking the first measurements…';
    else if (!ctl.enabled || ctl.error) title = 'The path to the internet looks healthy; the Wi-Fi itself is not being read yet';
    else title = 'Wi-Fi and the path to the internet look healthy';
    let source;
    if (!ctl.enabled) source = 'No Wi-Fi controller is connected, so only the path to the internet is measured.';
    else if (ctl.error) source = `<span class="w">Wi-Fi data is not arriving: ${esc(ctl.error)}.</span>`;
    else source = `UniFi controller${ctl.version ? ' ' + esc(ctl.version) : ''} · read ${esc(fmtAgo(ctl.ageS))}`;
    const pings = d.pings || {};
    const pingNote = pings.error ? `<span class="w">Pings are not running: ${esc(pings.error)}.</span>`
      : `Pinging ${plural(pings.targets || 0, 'address', 'addresses')} from a wired machine.`;
    const tile = (v, k, cls = '', t = '') => `<div class="vp-tile ${cls}" title="${esc(t)}"><div class="v">${v}</div><div class="k">${k}</div></div>`;

    let html = `<div class="vp-head">
        <div class="wf-verdict ${STATE[level].cls}"><span class="orb"></span><div>
          <div class="v1">${esc(title)}</div>
          <div class="v2">${source} · ${pingNote}</div>
        </div></div>
        <div class="vp-tiles">
          ${tile(`${fmtNum((s.aps || 0) - (s.apsDown || 0))}<small> of ${fmtNum(s.aps || 0)}</small>`, 'access points up', s.apsDown ? 'w' : '')}
          ${tile(fmtNum(s.clients || 0), 'devices on Wi-Fi', '', d.wired ? `${d.wired} more are wired` : '')}
          ${tile(fmtNum(poor), 'with a poor connection', s.bad ? 'c' : poor ? 'w' : '', `${s.bad || 0} bad, ${s.warn || 0} worth a look`)}
          ${tile(fmtNum(s.drops24h || 0), 'drops · 24 h', '', 'Times a device fell off the Wi-Fi and came back within ten minutes')}
        </div>
      </div>`;
    if ((up.level === 'bad' || up.level === 'warn') && up.detail) {
      html += `<div class="wf-callout ${STATE[up.level].cls}"><p>${esc(up.detail)}</p>${up.fix ? `<p class="fix"><b>What to do:</b> ${esc(up.fix)}</p>` : ''}</div>`;
    }

    html += `<h2 class="vp-h">The path every device shares</h2>
      <div class="vp-note">Each hop is pinged every few seconds. The first one that answers late or not at all is where a break starts; everything to its right inherits it.</div>
      <div class="wf-path">${up.hops.map((h, i) => `${!i ? '' : h.id === 'dns' ? '<span class="wf-gap"></span>' : '<span class="wf-arrow" aria-hidden="true">→</span>'}${this._hopCard(h)}`).join('') || '<div class="vp-empty">No hops are being measured yet.</div>'}</div>`;

    const gear = d.gear || [];
    if (gear.length) {
      const lim = d.limits || {};
      html += `<h2 class="vp-h">Access points and switches</h2><div class="wf-gear">${gear.map((g) => {
        const upl = g.uplink || {};
        const link = upl.type === 'mesh' ? `mesh (no cable)${upl.signal ? ` · ${Math.round(upl.signal)} dBm` : ''}`
          : upl.type === 'wired' ? `cable${upl.speed ? ` · ${upl.speed >= 1000 ? upl.speed / 1000 + ' Gbps' : upl.speed + ' Mbps'}` : ''}` : (upl.type || '–');
        const p = g.ping || {};
        return `<div class="wf-card ${STATE[g.level]?.cls || ''}">
          <div class="c1"><span class="who">${esc(g.name)}</span><span class="what">${esc(g.kindLabel)}${g.model ? ' · ' + esc(g.model) : ''}</span>${chip(g.online ? g.level : 'bad', g.online ? (g.level === 'ok' ? 'healthy' : STATE[g.level].text) : g.stateText)}</div>
          ${g.issues?.length ? `<ul class="wf-issues">${g.issues.map((x) => `<li>${esc(x[0].toUpperCase() + x.slice(1))}.</li>`).join('')}</ul>` : ''}
          <dl class="kv">
            <dt>Address</dt><dd class="mono">${esc(g.ip || '–')}</dd>
            <dt>Link to the network</dt><dd>${esc(link)}</dd>
            <dt>Answers pings in</dt><dd>${ms(p.p50Ms)} <span class="dim">· ${pct(p.lossPct)} lost in 5 min${p.hourLossPct ? `, ${pct(p.hourLossPct)} over the hour` : ''}</span></dd>
            ${g.isAp ? `<dt>Devices on it</dt><dd>${fmtNum(g.clients)}</dd>` : ''}
            ${g.online ? `<dt>Running for</dt><dd>${esc(fmtDur(g.uptimeS))}</dd>` : ''}
          </dl>
          ${(g.radios || []).map((r) => `<div class="wf-radio" title="${esc(`${r.band} channel ${r.channel}${r.width ? `, ${r.width} MHz wide` : ''}: ${r.util == null ? 'no figure' : Math.round(r.util) + '% of airtime in use'}${r.utilOwn ? `, ${Math.round(r.utilOwn)}% by this access point` : ''}`)}">
            <span class="b">${esc(r.band)}</span><span class="dim">ch ${esc(r.channel ?? '–')}</span>
            <span class="meter ${r.util >= lim.utilBad ? 'bad' : r.util >= lim.utilWarn ? 'warn' : ''}"><i style="width:${Math.max(2, Math.min(100, r.util || 0))}%"></i></span>
            <span class="n">${r.util == null ? '–' : Math.round(r.util) + '% busy'}</span><span class="dim">${plural(r.clients || 0, 'device')}</span></div>`).join('')}
        </div>`;
      }).join('')}</div>`;
    }

    html += `<h2 class="vp-h">Wi-Fi devices</h2>`;
    if (!(d.clients || []).length) {
      html += `<div class="vp-empty">${ctl.enabled && !ctl.error ? 'The controller lists no Wi-Fi devices right now.' : 'Wi-Fi devices appear here once the app can read the UniFi controller: which access point each is on, its signal, how often frames are re-sent, drops, and ping times.'}</div>`;
    } else {
      html += `<div class="wf-tools"><input id="wfFilter" type="search" placeholder="Filter by name, address or access point" value="${esc(this.filter)}" autocomplete="off">
          <label><input type="checkbox" data-act="problems" ${this.onlyProblems ? 'checked' : ''}> Problems only</label>
          <span class="dim">Worst first. Click a device for where its connection breaks and its last 24 hours.</span></div>
        <div class="wf-tablewrap"><table class="wf-table"><thead><tr><th>Device</th><th>Where the trouble is</th><th>Access point</th><th class="num">Signal</th>
          <th class="num" title="Share of frames the access point had to send again">Re-sent</th><th class="num" title="Fell off the Wi-Fi and came back within ten minutes">Drops 24 h</th>
          <th class="num" title="Round trip of a ping from a wired machine, and the share that went unanswered in the last 5 minutes">Ping</th><th></th></tr></thead>
          <tbody id="wfRows"></tbody></table></div>`;
    }

    const events = d.events || [];
    html += `<h2 class="vp-h">Recent Wi-Fi events</h2>`;
    html += events.length ? this._eventsHtml(events.slice(0, 60))
      : '<div class="vp-empty">Nothing in the last 24 hours. Devices joining, leaving, dropping and moving between access points show here.</div>';
    return html;
  }

  _hopCard(h) {
    const s = STATE[h.state] || STATE.unknown;
    const id = `spark-${h.id}`;
    return `<div class="wf-hop ${s.cls}">
      <div class="h1"><span class="nm">${esc(h.name)}</span>${chip(h.state, h.state === 'ok' ? 'healthy' : s.text)}</div>
      <div class="ip mono">${esc(h.ip || '')}</div>
      <div class="big">${ms(h.p50Ms)}<small> typical</small></div>
      <div class="sub">${pct(h.lossPct)} lost · 5 min${h.hourLossPct != null ? ` <span class="dim">· ${pct(h.hourLossPct)} over the hour</span>` : ''}</div>
      ${this._chart(id, h.spark || [], { w: 220, h: 54, compact: true, unit: 'ms' })}
      <div class="cap dim">last hour</div>
    </div>`;
  }

  _rows() {
    const body = this.el.querySelector('#wfRows');
    if (!body || !this.data) return;
    const lim = this.data.limits || {};
    const q = this.filter.trim().toLowerCase();
    const rows = (this.data.clients || []).filter((c) => (!this.onlyProblems || c.diag.level !== 'ok')
      && (!q || `${c.name} ${c.ip} ${c.mac} ${c.apName} ${c.ssid || ''} ${c.band || ''}`.toLowerCase().includes(q)));
    body.innerHTML = rows.map((c) => {
      const p = c.ping || {};
      const lossy = p.lossPct >= 15;
      return `<tr class="${STATE[c.diag.level]?.cls || ''}" data-mac="${esc(c.mac)}" tabindex="0">
        <td><div class="nm">${esc(c.name)}${c.watched ? ' <span class="tag" title="Pinged every few seconds">watched</span>' : ''}</div><div class="dim mono">${esc(c.ip || c.mac)}</div></td>
        <td>${chip(c.diag.level, WHERE[c.diag.where] || c.diag.where)}<div class="why">${esc(c.diag.level === 'ok' ? '' : c.diag.title)}</div></td>
        <td>${c.online === false ? '<span class="dim">–</span>' : `${esc(c.apName || '–')}<div class="dim">${esc([c.band, c.channel ? 'ch ' + c.channel : '', c.proto].filter(Boolean).join(' · '))}</div>`}</td>
        <td class="num ${sigClass(c.signal, lim)}">${c.signal == null ? '–' : Math.round(c.signal) + ' dBm'}</td>
        <td class="num ${c.retryPct >= lim.retryBad ? 'bad' : c.retryPct >= lim.retryWarn ? 'warn' : ''}">${pct(c.retryPct)}</td>
        <td class="num ${c.drops24h >= 6 ? 'warn' : ''}">${fmtNum(c.drops24h || 0)}${c.roams24h ? `<div class="dim">${plural(c.roams24h, 'move')}</div>` : ''}</td>
        <td class="num">${ms(p.p50Ms)}<div class="${lossy ? 'warn' : 'dim'}">${p.sent ? pct(p.lossPct) + ' lost' : 'not measured'}</div></td>
        <td class="go">▸</td></tr>`;
    }).join('') || `<tr><td colspan="8" class="dim">No device matches.</td></tr>`;
  }

  _eventsHtml(events) {
    const now = Date.now() / 1000;
    return `<ul class="vp-events wf-events">${events.map((e) => {
      const k = EVENT[e.kind] || { icon: '·', cls: '', text: e.kind };
      const bits = [e.kind === 'roamed' && e.from ? `from ${e.from} to ${e.apName || 'another access point'}` : '',
        e.kind !== 'roamed' && e.apName && !e.kind.startsWith('gear') ? `on ${e.apName}` : '', e.band || '',
        e.duration ? `after ${fmtDur(e.duration)}` : '', e.signal != null ? `last signal ${Math.round(e.signal)} dBm` : ''].filter(Boolean);
      return `<li class="${k.cls}" title="${esc(e.detail || '')}"><span class="ic">${k.icon}</span>
        <span class="t"><b>${esc(e.name || e.mac || 'something')}</b> ${esc(k.text)}</span>
        <span class="d">${esc(bits.join(' · ') || e.detail || '')}</span>
        <span class="when" title="${esc(fmtWhen(e.at))}">${esc(fmtAgo(Math.max(0, now - e.at)))}</span></li>`;
    }).join('')}</ul>`;
  }

  // ------------------------------------------------------------------ one device
  _detailHtml() {
    const back = '<button class="link wf-back" data-act="back">◂ All Wi-Fi devices</button>';
    const d = this.detail;
    if (!d) return `${back}<div class="vp-empty">Loading…</div>`;
    if (d.error) return `${back}<div class="vp-empty">Cannot show this device: ${esc(d.error)}</div>`;
    const c = d.device;
    const g = c.diag;
    const lim = this.data?.limits || {};
    const node = this.ctx.nodeFor?.(c.mac);
    const t = this.test && this.test.mac === c.mac ? this.test : null;
    let html = `${back}
      <div class="wf-dhead"><div>
          <div class="d-kind">Wi-Fi device${c.online === false ? ' · off the Wi-Fi right now' : ''}</div>
          <div class="wf-dtitle">${esc(c.name)}</div>
          <div class="dim"><span class="mono">${esc(c.ip || '–')}</span> · <span class="mono">${esc(c.mac)}</span>${c.vendor ? ' · ' + esc(c.vendor) : ''}</div>
        </div>
        <div class="wf-acts">
          <button class="btn" data-act="test" ${t?.running || c.online === false ? 'disabled' : ''} title="Ping the device, its access point, the router and the internet side by side for a few seconds">${t?.running ? 'Testing…' : 'Test now'}</button>
          <button class="btn" data-act="watch" title="${c.watched ? 'Go back to one ping every half minute' : 'Ping this device every few seconds, and keep pinging while it is off the Wi-Fi, for a sharper history'}">${c.watched ? 'Stop watching' : 'Watch closely'}</button>
          ${node ? `<button class="btn" data-act="map" data-node="${esc(node.id)}">Show on the map ▸</button>` : ''}
        </div></div>
      <div class="wf-callout ${STATE[g.level]?.cls || ''}">
        <div class="t">${chip(g.level, WHERE[g.where] || g.where)}<b>${esc(g.title)}</b></div>
        ${g.detail ? `<p>${esc(g.detail)}</p>` : ''}
        ${g.fix ? `<p class="fix"><b>What to do:</b> ${esc(g.fix)}</p>` : ''}
        ${g.evidence?.length ? `<p class="wf-ev">${g.evidence.map(esc).join(' · ')}</p>` : ''}
      </div>`;

    html += `<h2 class="vp-h">Its path, hop by hop</h2><div class="wf-path wide">${(d.path || []).map((h, i, all) => {
      const s = STATE[h.state] || STATE.unknown;
      let link = '';
      if (i) {
        const prev = all[i - 1];
        const last = i === all.length - 1;
        const label = last && h.signal != null ? `Wi-Fi · ${Math.round(h.signal)} dBm${h.band ? ' · ' + h.band : ''}` : h.uplink ? (h.uplink.type === 'mesh' ? 'mesh link' : h.uplink.speed ? `cable · ${h.uplink.speed >= 1000 ? h.uplink.speed / 1000 + ' Gbps' : h.uplink.speed + ' Mbps'}` : 'cable') : prev.id === 'internet' ? '' : '';
        const lc = last && h.signal != null ? sigClass(h.signal, lim) : h.uplink?.type === 'mesh' || (h.uplink?.speed && h.uplink.speed < 1000) ? 'warn' : '';
        link = `<span class="wf-link ${lc} ${last && h.signal != null ? 'air' : ''}"><i></i>${label ? `<span>${esc(label)}</span>` : ''}</span>`;
      }
      return `${link}<div class="wf-hop small ${s.cls}"><div class="h1"><span class="nm">${esc(h.name)}</span></div>
        <div class="ip mono">${esc(h.ip || '')}</div>${chip(h.state, h.why)}
        <div class="sub">${h.hourLossPct != null ? `${pct(h.hourLossPct)} lost over the hour` : '&nbsp;'}</div></div>`;
    }).join('')}</div>
      <div class="vp-note">Read it from the left: the first hop that is not healthy is where the break is. Pings come from a wired machine next to the router, so a healthy router and access point with a struggling device points at the radio link between them.</div>`;

    if (t && (t.result || t.error)) {
      html += `<h2 class="vp-h">Test just now</h2>`;
      html += t.error ? `<div class="vp-empty">The test could not run: ${esc(t.error)}</div>`
        : `<div class="wf-tablewrap"><table class="wf-table plain"><thead><tr><th>Hop</th><th class="num">Answered</th><th class="num">Lost</th><th class="num">Fastest</th><th class="num">Typical</th><th class="num">Slowest</th><th>Each ping</th></tr></thead><tbody>
          ${t.result.hops.map((h) => `<tr><td>${esc(h.name)} <span class="dim mono">${esc(h.ip)}</span></td><td class="num">${h.received}/${h.sent}</td>
            <td class="num ${h.lossPct >= 20 ? 'bad' : h.lossPct > 0 ? 'warn' : ''}">${pct(h.lossPct)}</td><td class="num">${ms(h.minMs)}</td><td class="num">${ms(h.p50Ms)}</td><td class="num">${ms(h.maxMs)}</td>
            <td class="dots">${(h.samples || []).map((v) => `<i class="${v == null ? 'lost' : ''}" title="${v == null ? 'no answer' : ms(v)}"></i>`).join('')}</td></tr>`).join('')}
          </tbody></table></div>
          <div class="vp-note">All hops were pinged at the same moments, so a ping lost at the device but answered by its access point was lost on the radio link.</div>`;
    }

    html += `<h2 class="vp-h">What the access point sees</h2><div class="wf-facts"><dl class="kv">
        <dt>Access point</dt><dd>${esc(c.apName || '–')}${c.ssid ? ` <span class="dim">· network “${esc(c.ssid)}”</span>` : ''}</dd>
        <dt>Radio</dt><dd>${esc([c.band, c.channel ? 'channel ' + c.channel : '', c.proto, c.streams ? c.streams + ' streams' : ''].filter(Boolean).join(' · ') || '–')}</dd>
        <dt>Signal</dt><dd class="${sigClass(c.signal, lim)}">${c.signal == null ? '–' : `${Math.round(c.signal)} dBm`} <span class="dim">· weak below ${lim.signalWarn}, very weak below ${lim.signalBad}</span></dd>
        <dt>Link speed</dt><dd>${c.txMbps ? `↓ ${c.txMbps} Mbps to it · ↑ ${c.rxMbps ?? '–'} Mbps from it` : '–'}</dd>
        <dt>Frames re-sent</dt><dd class="${c.retryPct >= lim.retryBad ? 'bad' : c.retryPct >= lim.retryWarn ? 'warn' : ''}">${pct(c.retryPct)} <span class="dim">· noisy above ${lim.retryWarn}%</span></dd>
      </dl><dl class="kv">
        <dt>Connected for</dt><dd>${c.uptimeS ? esc(fmtDur(c.uptimeS)) : '–'}</dd>
        <dt>Drops</dt><dd>${fmtNum(c.drops1h || 0)} in the last hour · ${fmtNum(c.drops24h || 0)} in 24 h</dd>
        <dt>Moves between access points</dt><dd>${fmtNum(c.roams1h || 0)} in the last hour · ${fmtNum(c.roams24h || 0)} in 24 h</dd>
        <dt>Using right now</dt><dd>↓ ${esc(fmtBits(c.downBps || 0))} · ↑ ${esc(fmtBits(c.upBps || 0))}${c.idleS > 60 ? ` <span class="dim">· idle for ${esc(fmtDur(c.idleS))}</span>` : ''}</dd>
        ${c.experience != null ? `<dt>Controller's score</dt><dd>${Math.round(c.experience)}%</dd>` : ''}
      </dl></div>`;

    const h = d.history || {};
    const span = 86400;
    html += `<h2 class="vp-h">Last 24 hours: who stopped answering first?</h2>
      <div class="vp-note">Ping round trip (line) and the share of pings lost (bars underneath), on the same clock for each hop. A gap or bars on the device only is its radio link; on the access point too, the access point or its cable; on the router or internet too, further upstream.</div>
      <div class="wf-multi">
        ${this._multi('m-dev', c.name, h.day, span)}
        ${d.ap ? this._multi('m-ap', d.ap.name, h.apDay, span) : ''}
        ${this._multi('m-router', 'Router', h.routerDay, span)}
        ${this._multi('m-inet', 'Internet', h.internetDay, span)}
      </div>`;
    const sig = (d.signal || []).filter((r) => r[1] != null).map((r) => [r[0], r[1]]);
    if (sig.length > 1) {
      html += `<h2 class="vp-h">Signal strength, last 24 hours</h2>
        ${this._chart('c-signal', sig, { w: 900, h: 150, unit: 'dBm', min: -90, max: -30, span, marks: [[lim.signalWarn, 'weak'], [lim.signalBad, 'very weak']], title: `${c.name}: signal in dBm` })}`;
    }
    const out = d.outages || [];
    if (out.length) {
      html += `<h2 class="vp-h">When it stopped answering</h2><ul class="wf-out">${out.slice(0, 12).map((o) => `<li><b>${esc(fmtWhen(o.from))}</b> for ${plural(o.minutes, 'minute')}</li>`).join('')}</ul>
        <div class="vp-note">Minutes in which most pings to this device went unanswered. Phones and tablets do this whenever they sleep; for a device that is always on, each one is a break.</div>`;
    }
    html += `<h2 class="vp-h">Its Wi-Fi events, last 24 hours</h2>`;
    html += (d.events || []).length ? this._eventsHtml(d.events) : '<div class="vp-empty">No joins, drops or moves recorded for this device.</div>';
    return html;
  }

  _multi(id, name, rows, span) {
    return `<div class="wf-mrow"><div class="lbl" title="${esc(name)}">${esc(name)}</div>
      ${this._chart(id, rows || [], { w: 900, h: 86, unit: 'ms', span, title: `${name}: ping round trip and loss` })}</div>`;
  }

  // ------------------------------------------------------------------ charts
  // rows: [[when, value]] or [[when, average ms, worst ms, loss %]]. One measure per chart: latency is
  // the line; loss is its own strip of bars underneath on the same clock (never a second axis).
  _chart(id, rows, o) {
    const W = o.w, H = o.h, compact = !!o.compact;
    const withLoss = rows.some((r) => r.length > 3);
    const padL = compact ? 2 : 44, padR = compact ? 2 : 8, padT = compact ? 3 : 8;
    const lossH = withLoss ? (compact ? 7 : 14) : 0;
    const axisH = compact ? 0 : 16;
    const plotB = H - axisH - lossH - (withLoss ? 3 : 0);
    const now = Date.now() / 1000;
    const x1 = rows.length ? Math.max(now, rows[rows.length - 1][0]) : now;
    const x0 = o.span ? x1 - o.span : (rows.length ? Math.min(rows[0][0], x1 - 600) : x1 - 3600);
    const vals = rows.map((r) => r[1]).filter((v) => v != null);
    if (!vals.length && !rows.some((r) => r[3] > 0)) {
      return `<div class="wf-chart empty" style="${compact ? `height:${H}px` : ''}"><span>${compact ? 'no measurements yet' : 'No measurements in this period yet.'}</span></div>`;
    }
    let lo = o.min ?? 0;
    let hi = o.max ?? niceMax(Math.max(1, ...vals) * 1.15);
    if (o.min != null) { lo = Math.min(lo, ...vals); hi = Math.max(hi, ...vals); }
    const X = (t) => padL + ((t - x0) / (x1 - x0 || 1)) * (W - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo || 1)) * (plotB - padT);
    let svg = '';
    if (!compact) {
      for (const v of [lo, lo + (hi - lo) / 2, hi]) {
        svg += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
          <text class="ax" x="${padL - 6}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end">${fmtTick(v, o.unit)}</text>`;
      }
      for (const [v, label] of o.marks || []) {
        svg += `<line class="mark" x1="${padL}" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
          <text class="ax" x="${W - padR - 2}" y="${(Y(v) - 3).toFixed(1)}" text-anchor="end">${esc(label)}</text>`;
      }
      const ticks = 6;
      for (let i = 0; i <= ticks; i++) {
        const t = x0 + ((x1 - x0) * i) / ticks;
        svg += `<text class="ax" x="${X(t).toFixed(1)}" y="${H - 3}" text-anchor="${i === 0 ? 'start' : i === ticks ? 'end' : 'middle'}">${esc(clock(t))}</text>`;
      }
    }
    // The line breaks wherever nothing answered or nothing was measured.
    const step = rows.length > 1 ? Math.min(...rows.slice(1).map((r, i) => r[0] - rows[i][0])) : 60;
    let path = '', pen = false, prev = null;
    for (const r of rows) {
      if (r[1] == null || r[0] < x0) { pen = false; prev = r; continue; }
      if (pen && prev && r[0] - prev[0] > step * 2.5) pen = false;
      path += `${pen ? 'L' : 'M'}${X(r[0]).toFixed(1)} ${Y(r[1]).toFixed(1)}`;
      pen = true; prev = r;
    }
    svg += `<path class="ln" d="${path}"/>`;
    const lone = rows.filter((r, i) => r[1] != null && r[0] >= x0 && (rows[i - 1]?.[1] == null) && (rows[i + 1]?.[1] == null));
    svg += lone.map((r) => `<circle class="pt" cx="${X(r[0]).toFixed(1)}" cy="${Y(r[1]).toFixed(1)}" r="1.6"/>`).join('');
    if (withLoss) {
      const top = plotB + 3;
      const bw = Math.max(1.5, ((W - padL - padR) * step) / (x1 - x0 || 1) - 1);
      if (!compact) svg += `<text class="ax" x="${padL - 6}" y="${top + lossH - 3}" text-anchor="end">lost</text>`;
      svg += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${top + lossH}" y2="${top + lossH}"/>`;
      for (const r of rows) {
        if (!(r[3] > 0) || r[0] < x0) continue;
        const hgt = Math.max(2, (Math.min(100, r[3]) / 100) * lossH);
        svg += `<rect class="loss ${r[3] >= 50 ? 'hi' : ''}" x="${(X(r[0]) - bw / 2).toFixed(1)}" y="${(top + lossH - hgt).toFixed(1)}" width="${bw.toFixed(1)}" height="${hgt.toFixed(1)}" rx="1"/>`;
      }
    }
    svg += `<line class="cross" y1="${padT}" y2="${H - axisH}" x1="0" x2="0" visibility="hidden"/>`;
    this.charts.set(id, { rows, x0, x1, W, padL, padR, unit: o.unit, withLoss });
    return `<div class="wf-chart ${compact ? 'compact' : ''}" data-chart="${esc(id)}"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.title || 'Ping round trip over time')}">${svg}</svg></div>`;
  }

  _hover(ev) {
    const box = ev.target.closest?.('[data-chart]');
    const c = box && this.charts.get(box.dataset.chart);
    for (const el of this.el.querySelectorAll('.cross')) if (!box || !box.contains(el)) el.setAttribute('visibility', 'hidden');
    if (!c || !c.rows.length) { this.tip.hidden = true; return; }
    const rect = box.getBoundingClientRect();
    const fx = ((ev.clientX - rect.left) / rect.width) * c.W;
    const t = c.x0 + ((fx - c.padL) / (c.W - c.padL - c.padR)) * (c.x1 - c.x0);
    let best = c.rows[0];
    for (const r of c.rows) if (Math.abs(r[0] - t) < Math.abs(best[0] - t)) best = r;
    const x = c.padL + ((best[0] - c.x0) / (c.x1 - c.x0 || 1)) * (c.W - c.padL - c.padR);
    const cross = box.querySelector('.cross');
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    const value = c.unit === 'dBm' ? `${Math.round(best[1])} dBm` : best[1] == null ? 'no answer' : `${ms(best[1])} typical`;
    this.tip.innerHTML = `<b>${esc(clock(best[0]))}</b> ${esc(value)}${c.withLoss && best[2] != null ? ` · slowest ${esc(ms(best[2]))}` : ''}${c.withLoss ? ` · ${esc(pct(best[3] || 0))} lost` : ''}`;
    this.tip.hidden = false;
    const tw = this.tip.offsetWidth;
    this.tip.style.left = `${Math.max(8, Math.min(window.innerWidth - tw - 8, ev.clientX + 12))}px`;
    this.tip.style.top = `${Math.max(8, ev.clientY - 34)}px`;
  }

  // ------------------------------------------------------------------ actions
  async _click(ev) {
    const act = ev.target.closest?.('[data-act]')?.dataset.act;
    if (act === 'back') { this.mac = null; this.detail = null; this.ctx.onState?.(null); this.render(this.data); this.el.scrollTop = 0; return; }
    if (act === 'problems') { this.onlyProblems = ev.target.checked; this._rows(); return; }
    if (act === 'map') { this.ctx.onShowOnMap(ev.target.closest('[data-node]').dataset.node); return; }
    if (act === 'watch' && this.detail?.device) {
      const c = this.detail.device;
      try { await this.ctx.send('api/v1/wifi/watch', { mac: c.mac, on: !c.watched }); } catch (err) { this.ctx.toast?.(`Cannot change watching: ${err.message}`); }
      await this.ctx.reload?.();
      return this._loadDetail();
    }
    if (act === 'test' && this.detail?.device) {
      const mac = this.detail.device.mac;
      this.test = { mac, running: true };
      this.render(this.data);
      try { this.test = { mac, result: await this.ctx.send('api/v1/wifi/test', { mac }) }; } catch (err) { this.test = { mac, error: err.message }; }
      if (this.mac === mac) this.render(this.data);
      return;
    }
    const row = ev.target.closest?.('tr[data-mac]');
    if (row) { this.el.scrollTop = 0; this.open(row.dataset.mac); }
  }
}

function niceMax(v) {
  const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
  return steps.find((s) => s >= v) || Math.ceil(v / 1000) * 1000;
}

function fmtTick(v, unit) {
  if (unit === 'dBm') return `${Math.round(v)}`;
  return v >= 10 || v === 0 ? `${Math.round(v)} ms` : `${v.toFixed(1)} ms`;
}

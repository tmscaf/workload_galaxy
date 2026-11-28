// Copyright (C) 2026 Troy Scafuro
// SPDX-License-Identifier: AGPL-3.0-only

// The ad blocker's page: what is being published for the router to block, where it comes from,
// whether the router is fetching it, and which devices are running into it.
//
// Data: GET api/v1/plugins/adblock. Read-only: the lists and names are changed in the suite's Settings.
//
// A plugin's page is a module like this one: its default export takes (element, ctx), has
// render(data, graph), and may have a static badge(data) for its tab.
import { esc, fmtAgo, fmtNum, plural } from '../format.js';

const SETTINGS = 'Settings > Network > Plugin: Ad blocker';

export default class AdBlockPanel {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;          // { base, copy(text), onShowOnMap(nodeId) }
  }

  // Badge on the tab: lists that are failing, if any.
  static badge(data) {
    const failing = (data?.lists || []).filter((l) => l.error).length;
    return { count: failing, level: 'warning', title: failing ? `${plural(failing, 'block list')} could not be fetched` : '' };
  }

  render(data) {
    if (!data) { this.el.innerHTML = '<div class="vp-empty">Loading…</div>'; return; }
    const lists = data.lists || [];
    const own = data.own || { block: [], allow: [], rejected: [] };
    const r = data.router || {};
    const b = data.blocked || { devices: [] };
    const failing = lists.filter((l) => l.error).length;
    const tile = (v, k, cls = '', title = '') => `<div class="vp-tile ${cls}" title="${esc(title)}"><div class="v">${v}</div><div class="k">${k}</div></div>`;

    let verdict, sub;
    if (!lists.length && !own.block.length) {
      verdict = 'Nothing to block yet';
      sub = `Add a block list under ${SETTINGS}.`;
    } else if (!data.ready && data.waitingFor?.length) {
      verdict = 'Getting the lists';
      sub = `Waiting for ${data.waitingFor.map(esc).join(', ')}. Until then the router is told to keep the list it has.`;
    } else if (!data.count) {
      verdict = 'Nothing to block yet';
      sub = 'No list has been fetched yet.';
    } else {
      verdict = `${fmtNum(data.count)} ${data.count === 1 ? 'name' : 'names'} being published`;
      sub = `From ${plural(lists.length, 'list')}${own.block.length ? ` and ${plural(own.block.length, 'name')} of your own` : ''}`
        + (data.builtS != null ? ` · put together ${fmtAgo(data.builtS)}` : '');
    }

    let pull, pullCls = '';
    if (!r.configured) { pull = 'not set up'; pullCls = 'w'; }
    else if (r.lastPullS == null) { pull = 'not yet'; pullCls = 'w'; }
    else pull = fmtAgo(r.lastPullS);

    let html = `<div class="vp-head">
        <div class="vp-verdict ${data.ready && data.count ? 'on' : ''}"><span class="orb"></span><div>
          <div class="v1">${verdict}</div>
          <div class="v2">${sub}${data.mode === 'sample' ? ' · sample data: made-up lists, nothing is fetched' : ''}</div>
        </div></div>
        <div class="vp-tiles">
          ${tile(fmtNum(data.count || 0), 'names published', data.count ? 'on' : '')}
          ${tile(`${fmtNum(lists.length - failing)}<small> of ${fmtNum(lists.length)}</small>`, 'lists up to date', failing ? 'w' : '')}
          ${tile(esc(pull), 'last fetched by the router', pullCls, 'The router fetches the list on its own schedule; this module never connects to it')}
          ${b.counted ? tile(fmtNum(b.total || 0), 'blocked connections now', '', 'Connections devices made to the blocking page instead of the name they asked for') : ''}
        </div>
      </div>`;

    // ---- the router
    html += '<h2 class="vp-h">The router</h2>';
    const feedUrl = new URL((this.ctx.base || '') + data.feedPath, location.href).href + '?token=';
    let line;
    if (!r.configured) {
      line = `<span class="w">The router is not set up to fetch this list.</span> Set the Router token under Settings &gt; Network &gt; Blocking, then give the router the address below.`;
    } else if (r.lastPullS == null) {
      line = '<span class="w">The router has not fetched the list yet.</span> Add the address below to its DNS blocking; it then fetches on its own schedule.';
    } else {
      line = `The router last fetched the list ${esc(fmtAgo(r.lastPullS))}${r.from ? `, from <span class="mono">${esc(r.from)}</span>` : ''}. `
        + (r.current ? 'It has the list as it is now.' : 'The list has changed since; the router gets the new one at its next update.');
    }
    html += `<div class="pl-box"><div>${line}</div>
        <dl class="kv pl-kv"><dt>Address for the router</dt>
          <dd><span class="mono">${esc(feedUrl)}<span class="dim">your router token</span></span>
            <button class="btn small" data-copy="${esc(feedUrl)}" title="Copy the address; add the router token to the end">Copy</button></dd></dl>
        <div class="vp-note">The router pulls the list and decides what to load. This module holds no router login and cannot change the router.</div>
      </div>`;

    // ---- where the names come from
    html += '<h2 class="vp-h">Block lists</h2>';
    if (!lists.length) {
      html += `<div class="vp-empty">No block list is set. Add the address of one under ${SETTINGS}; each list is fetched from its publisher and stays under its publisher's licence.</div>`;
    } else {
      html += `<table class="pl-table"><thead><tr><th>List</th><th class="n">Names</th><th>Fetched</th><th>State</th></tr></thead><tbody>${lists.map((l) => {
        let st;
        if (l.error && l.names == null) st = `<span class="c">never fetched</span> <span class="dim">· ${esc(l.error)}</span>`;
        else if (l.error) st = `<span class="w">could not be refreshed</span> <span class="dim">· ${esc(l.error)} · the last good copy is still published</span>`;
        else if (l.fromCache) st = 'kept from before the restart <span class="dim">· refreshed when it is next due</span>';
        else st = '<span class="ok">up to date</span>';
        return `<tr><td class="mono" title="${esc(l.url)}">${esc(l.short)}</td><td class="n">${l.names == null ? '–' : fmtNum(l.names)}</td>
          <td>${l.ageS == null ? '–' : esc(fmtAgo(l.ageS))}</td><td>${st}</td></tr>`;
      }).join('')}</tbody></table>
      <div class="vp-note">Fetched again every ${plural(data.refreshHours, 'hour')}; a list that fails is tried again every 10 minutes.
        ${data.truncated ? `<span class="w">${fmtNum(data.truncated)} names were left out</span> to stay within the ${fmtNum(data.max)} this plugin is set to publish.` : ''}</div>`;
    }

    // ---- your own names
    const chips = (names, cls) => names.map((n) => `<li class="${cls}"><span class="mono">${esc(n)}</span></li>`).join('');
    html += '<h2 class="vp-h">Your own names</h2><div class="pl-own">';
    html += `<div><div class="pl-k">Always block</div>${own.block.length ? `<ul class="vp-knocks">${chips(own.block, '')}</ul>` : '<div class="dim">None.</div>'}</div>`;
    html += `<div><div class="pl-k">Never block <span class="dim">· wins over every list, and covers everything under the name${data.removedByAllow ? ` · taking ${plural(data.removedByAllow, 'name')} off the list now` : ''}</span></div>
      ${own.allow.length ? `<ul class="vp-knocks">${chips(own.allow, 'ok')}</ul>` : '<div class="dim">None.</div>'}</div>`;
    if (own.rejected.length) {
      html += `<div><div class="pl-k w">Left out</div><ul class="pl-rej">${own.rejected.map((x) => `<li><span class="mono">${esc(x.name)}</span> <span class="dim">· ${esc(x.why)}</span></li>`).join('')}</ul></div>`;
    }
    html += `</div><div class="vp-note">Change these under ${SETTINGS}. If something stops working after a list is added, put its name under Never block.</div>`;

    // ---- who is being blocked
    html += '<h2 class="vp-h">Devices being blocked now</h2>';
    if (!b.counted) {
      html += '<div class="vp-empty">Blocked lookups are not being counted. Set <b>DNS block page addresses</b> under Settings &gt; Network &gt; Firewall and DNS blocking to the address the router sends blocked names to.</div>';
    } else if (!b.devices.length) {
      html += '<div class="vp-empty">No device is connecting to the blocking page right now.</div>';
    } else {
      html += `<table class="pl-table"><thead><tr><th>Device</th><th>Address</th><th class="n">Blocked connections</th><th></th></tr></thead><tbody>${b.devices.map((d) => `<tr>
          <td>${esc(d.name)}</td><td class="mono">${esc(d.ip)}</td><td class="n">${fmtNum(d.hits)}</td>
          <td><button class="btn small" data-map="${esc(d.id)}">Show on the map ▸</button></td></tr>`).join('')}</tbody></table>
        ${b.moreDevices ? `<div class="vp-note">And ${plural(b.moreDevices, 'more device')}.</div>` : ''}
        <div class="vp-note">Counted from connections to the blocking page, so the names themselves are not shown here; the router's own DNS blocking report has them.</div>`;
    }

    html += `<div class="vp-note pl-limits">What blocking by name cannot do: stop ads that come from the same name as the content (a video site's own ads),
      or reach a device that looks names up somewhere else over an encrypted connection instead of asking the router.</div>`;

    this.el.innerHTML = html;
    this.el.querySelectorAll('[data-map]').forEach((x) => x.addEventListener('click', () => this.ctx.onShowOnMap(x.dataset.map)));
    this.el.querySelectorAll('[data-copy]').forEach((x) => x.addEventListener('click', () => {
      this.ctx.copy(x.dataset.copy);
      x.textContent = 'Copied';
      setTimeout(() => { x.textContent = 'Copy'; }, 1500);
    }));
  }
}

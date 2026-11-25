// Sign-in and Block / Unblock.
//
// Viewing is open to everyone on the LAN; blocking needs the admin password. The app never talks
// to the router: the router pulls the block list from the app about once a minute, so every entry
// shows whether the router has loaded it yet.
//
// One instance is shared by the top bar, the drawer and Triage. Panels are rendered as HTML strings
// by whoever owns the page area (renderControl / sectionHtml) and are wired here through one
// document-level listener on [data-act], so they keep working across the live re-renders. What the
// user has typed or picked lives here, not in the DOM, so a refresh never loses it.
import { esc, fmtAgo, fmtDur, plural } from './format.js';

const HEADER = { 'X-Requested-With': 'network-galaxy' };
const EXPIRY = [['1d', '1 day'], ['7d', '7 days'], ['30d', '30 days'], ['never', 'Permanent']];
const EXPIRY_TEXT = { '1d': 'for 1 day', '7d': 'for 7 days', '30d': 'for 30 days', never: 'permanently' };
const WARNING = 'Blocks all traffic to and from these addresses for every device on your network.';

// ------------------------------------------------------------------ addresses
function ipNum(s) {
  const p = String(s).split('.');
  if (p.length !== 4) return null;
  let n = 0;
  for (const x of p) {
    if (!/^\d{1,3}$/.test(x) || +x > 255) return null;
    n = n * 256 + +x;
  }
  return n;
}

// Does a block-list entry ("203.0.113.0/24" or "203.0.113.9") cover this address?
function covers(entry, target) {
  if (entry === target) return true;
  const [net, bits] = String(entry).split('/');
  if (bits == null) return false;
  const a = ipNum(net), b = ipNum(target);
  if (a == null || b == null) return false;
  const span = 2 ** (32 - Math.max(0, Math.min(32, +bits)));
  return Math.floor(a / span) === Math.floor(b / span);
}

// A clock time for something that happened today, else a short date.
function fmtClock(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

const now = () => Date.now() / 1000;
// Time left on an entry: "6 days", "5 h".
const fmtLeft = (s) => (s >= 82800 ? `${Math.round(s / 86400)} day${Math.round(s / 86400) === 1 ? '' : 's'}` : fmtDur(s));

export class Actions {
  constructor({ base = '', onChange = () => {}, reloadSoon = () => {} } = {}) {
    this.base = base;
    this.onChange = onChange;          // re-render whatever shows block state
    this.reloadSoon = reloadSoon;      // the graph is rebuilt in the background after a change
    this.session = null;               // {signedIn, actions, routerSync, lockedFor}
    this.blocks = null;                // the /api/v1/blocks snapshot
    this.fetchedAt = 0;
    this.panels = new Map();           // key -> {reason, expires}: confirm panels the user opened
    this.specs = new Map();            // key -> {targets, reason, finding, label}: what each control blocks
    this.busy = new Set();             // keys with a request in flight
    this.manual = { target: '', reason: '', expires: '7d' };
    this.pendingSignIn = null;         // what to carry on with after a successful sign-in
    this._bindDom();
  }

  get enabled() { return !!this.session?.actions; }
  get signedIn() { return !!this.session?.signedIn; }

  // ------------------------------------------------------------------ server
  async refresh() {
    // Each is kept as it was when its request fails; the map's own refresh reports a dead server.
    const [s, b] = await Promise.allSettled([this._get('api/v1/session'), this._get('api/v1/blocks')]);
    if (s.status === 'fulfilled' && s.value) this.session = s.value;
    if (b.status === 'fulfilled') this._setBlocks(b.value);
    this._renderSession();
  }

  _setBlocks(b) {
    if (!b || !Array.isArray(b.entries)) return;
    this.blocks = b;
    this.fetchedAt = now();
  }

  async _get(path) {
    const res = await fetch(this.base + path, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  // Every action carries the X-Requested-With header; without it the server refuses (403).
  async _send(method, path, body) {
    let res;
    try {
      res = await fetch(this.base + path, {
        method, credentials: 'same-origin',
        headers: { ...HEADER, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      return { ok: false, status: 0, detail: `cannot reach Network Galaxy (${err.message})` };
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    const detail = data && (typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((d) => d.msg || d).join('; ') : '');
    return { ok: res.ok, status: res.status, data, detail: detail || res.statusText };
  }

  // ------------------------------------------------------------------ lookups
  entryFor(target, fallback = null) {
    if (!this.blocks) return fallback;
    return this.blocks.entries.find((e) => covers(e.target, target)) || null;
  }

  // The router's view of the list, as one line of text and a level.
  routerStatus() {
    const b = this.blocks, s = this.session;
    if (!b) return null;
    if (s && !s.routerSync) return { level: 'warn', text: 'Router not connected: it is not set up to pull this list yet' };
    const r = b.router || {};
    const age = r.lastFetchS != null ? r.lastFetchS + (now() - this.fetchedAt) : null;
    const parts = [];
    let level = 'ok';
    if (age == null) { level = 'warn'; parts.push('The router has not fetched the list yet'); }
    else if (age > 300) { level = 'warn'; parts.push(`Router hasn't fetched the list in ${fmtDur(age)}`); }
    else if (!b.inSync) { level = 'warn'; parts.push(`Router has not loaded the latest list yet · last checked ${fmtAgo(age)}`); }
    else parts.push(`Router up to date · last checked ${fmtAgo(age)}`);
    if (r.rejected > 0) { level = 'crit'; parts.push(`Router refused ${plural(r.rejected, 'line')}`); }
    return { level, text: parts.join(' · ') };
  }

  _entryState(e) {
    if (!e) return '';
    const by = e.by === 'admin' || !e.by ? 'you' : e.by;
    const where = e.appliedAt ? `on the router since ${fmtClock(e.appliedAt)}`
      : this.session && !this.session.routerSync ? 'the router is not set up to pull this list yet' : 'waiting for the router…';
    return `Blocked by ${esc(by)} ${esc(fmtAgo(now() - e.at))} · <span class="${e.appliedAt ? 'ok' : 'wait'}">${esc(where)}</span>`
      + (e.expiresAt ? ` · <span class="dim">ends in ${esc(fmtLeft(e.expiresAt - now()))}</span>` : '');
  }

  // ------------------------------------------------------------------ the Block control
  // spec: {key, targets:[{target, blocked}], reason, finding, label}. Returns '' when there is
  // nothing to show (no targets, or actions are off and nothing is blocked).
  renderControl(spec) {
    const items = (spec.targets || []).filter((t) => t && t.target).map((t) => ({ target: t.target, entry: this.entryFor(t.target, t.blocked || null) }));
    if (!items.length) return '';
    const key = spec.key;
    this.specs.set(key, spec);
    const open = items.filter((t) => !t.entry).map((t) => t.target);
    const entries = [...new Map(items.filter((t) => t.entry).map((t) => [t.entry.target, t.entry])).values()];
    if (!this.enabled && !entries.length) return '';
    const busy = this.busy.has(key);
    let html = '';
    if (entries.length) {
      html += `<div class="blk-on">${entries.map((e) => `<div class="blk-row"><span class="blk-mark">⛔</span>
        <div class="blk-txt"><b class="mono">${esc(e.target)}</b> ${this._entryState(e)}</div>
        ${this.enabled ? `<button class="btn small" data-act="unblock" data-key="${esc(key)}" data-targets="${esc(e.target)}" ${busy ? 'disabled' : ''}>${busy ? 'Working…' : 'Unblock'}</button>` : ''}</div>`).join('')}</div>`;
    }
    if (open.length && this.enabled) {
      const p = this.panels.get(key);
      if (!p) {
        html += `<button class="btn small blk-btn" data-act="open" data-key="${esc(key)}" title="${esc(WARNING)}">⛔ Block ${open.length === 1 ? esc(open[0]) : `${open.length} addresses`}…</button>`;
      } else {
        html += `<div class="blk-panel">
          <div class="blk-h">Block ${open.length === 1 ? 'this address' : `these ${open.length} addresses`}</div>
          <div class="blk-list mono">${open.map(esc).join('<br>')}</div>
          <label class="blk-l">Reason<input type="text" data-act-input="reason" data-key="${esc(key)}" data-focus="r:${esc(key)}" value="${esc(p.reason)}" maxlength="300"></label>
          <div class="blk-l">For</div>${this._expiryHtml(key, p.expires)}
          <p class="blk-warn">${esc(WARNING)}</p>
          ${this.session && !this.session.routerSync ? '<p class="blk-note">The router is not set up to pull this list yet, so the block takes effect once it is.</p>' : ''}
          <div class="blk-btns"><button class="btn small danger" data-act="confirm" data-key="${esc(key)}" ${busy ? 'disabled' : ''}>${busy ? 'Blocking…' : 'Block'}</button>
            <button class="btn small" data-act="cancel" data-key="${esc(key)}" ${busy ? 'disabled' : ''}>Cancel</button></div></div>`;
      }
    }
    return `<div class="blk" data-blk="${esc(key)}">${html}</div>`;
  }

  _expiryHtml(key, value) {
    return `<div class="blk-exp" role="radiogroup">${EXPIRY.map(([v, label]) => `<button type="button" role="radio" aria-checked="${v === value}"
      class="${v === value ? 'on' : ''}" data-act="expiry" data-key="${esc(key)}" data-value="${v}">${label}</button>`).join('')}</div>`;
  }

  // ------------------------------------------------------------------ the Blocked section (Triage)
  sectionHtml() {
    const b = this.blocks;
    if (!b) return '';
    const entries = b.entries || [], hist = b.history || [];
    if (!this.enabled && !entries.length && !hist.length) return '';
    const st = this.routerStatus();
    const busy = (k) => this.busy.has(k);
    const rows = entries.map((e) => {
      const k = `u:${e.target}`;
      return `<tr><td class="mono nw">${esc(e.target)}</td>
        <td>${e.label ? `<b>${esc(e.label)}</b>` : ''}${e.label && e.reason ? '<br>' : ''}<span class="dim">${esc(e.reason || (e.label ? '' : 'no reason given'))}</span></td>
        <td class="nw" title="${esc(new Date(e.at * 1000).toLocaleString())}">${esc(fmtAgo(now() - e.at))}</td>
        <td class="nw">${e.expiresAt ? `in ${esc(fmtLeft(e.expiresAt - now()))}` : 'never'}</td>
        <td class="nw">${e.appliedAt ? `<span class="ok">since ${esc(fmtClock(e.appliedAt))}</span>` : `<span class="wait">${this.session && !this.session.routerSync ? 'router not connected' : 'waiting for the router…'}</span>`}</td>
        <td class="num">${this.enabled ? `<button class="btn small" data-act="unblock" data-key="${esc(k)}" data-targets="${esc(e.target)}" ${busy(k) ? 'disabled' : ''}>${busy(k) ? 'Working…' : 'Unblock'}</button>` : ''}</td></tr>`;
    }).join('');
    const m = this.manual;
    const form = !this.enabled ? '' : this.signedIn ? `<form class="blk-manual" data-act-form="manual">
        <div class="blk-h">Block an address…</div>
        <div class="blk-mrow">
          <input type="text" placeholder="203.0.113.9 or 203.0.113.0/24" data-act-input="m-target" data-focus="m:target" value="${esc(m.target)}" autocomplete="off" spellcheck="false">
          <input type="text" placeholder="Reason" data-act-input="m-reason" data-focus="m:reason" value="${esc(m.reason)}" maxlength="300">
          ${this._expiryHtml('manual', m.expires)}
          <button class="btn small danger" type="submit" ${busy('manual') ? 'disabled' : ''}>${busy('manual') ? 'Blocking…' : 'Block'}</button></div>
        <p class="blk-warn">${esc(WARNING)} Ranges up to /${b.minPrefix || 24}. Your own network and the router can never be blocked.</p></form>`
      : `<p class="blk-signin"><button class="link" data-act="signin">Sign in</button> to block an address yourself.</p>`;
    const why = (h) => h.how === 'expired' ? 'expired' : `removed${h.removedBy && h.removedBy !== 'admin' ? ` by ${h.removedBy}` : ' by you'}`;
    return `<h2 class="tg-h">Blocked <span class="hint">${plural(entries.length, 'address', 'addresses')} · the router pulls this list about once a minute</span></h2>
      ${st ? `<div class="blk-status ${st.level}"><span class="dot"></span>${esc(st.text)}</div>` : ''}
      ${entries.length ? `<table class="tg-table blk-table"><thead><tr><th>Address</th><th>What and why</th><th>Blocked</th><th>Ends</th><th>On the router</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>` : '<div class="blk-empty">Nothing is blocked. Use Block on a finding or an internet service, or type an address below.</div>'}
      ${form}
      ${hist.length ? `<details class="blk-hist" data-key="blk-hist"><summary>Recently removed (${hist.length})</summary><ul>${hist.map((h) => `<li>
        <span class="mono">${esc(h.target)}</span> <span class="dim">${esc(h.label || h.reason || '')}</span>
        <span class="when">${esc(why(h))} ${esc(fmtAgo(now() - (h.removedAt || h.at)))}</span></li>`).join('')}</ul></details>` : ''}`;
  }

  // ------------------------------------------------------------------ DOM
  _bindDom() {
    document.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-act]');
      if (!el || el.disabled) return;
      ev.preventDefault();
      ev.stopPropagation();
      this._act(el.dataset.act, el);
    });
    document.addEventListener('input', (ev) => {
      const el = ev.target.closest('[data-act-input]');
      if (!el) return;
      const k = el.dataset.actInput;
      if (k === 'reason') { const p = this.panels.get(el.dataset.key); if (p) p.reason = el.value; }
      else if (k === 'm-target') this.manual.target = el.value;
      else if (k === 'm-reason') this.manual.reason = el.value;
    });
    document.addEventListener('submit', (ev) => {
      const f = ev.target.closest('[data-act-form]');
      if (!f) return;
      ev.preventDefault();
      if (f.dataset.actForm === 'manual') this._manual();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && ev.target.matches?.('[data-act-input="reason"]')) { ev.preventDefault(); this._act('confirm', ev.target); }
      if (ev.key === 'Escape' && !document.getElementById('signin').hidden) this._closeSignIn();
    });
    const form = document.getElementById('signinForm');
    form.addEventListener('submit', (ev) => { ev.preventDefault(); this._login(); });
    document.getElementById('signinCancel').addEventListener('click', () => this._closeSignIn());
    document.getElementById('signin').addEventListener('click', (ev) => { if (ev.target.id === 'signin') this._closeSignIn(); });
  }

  _act(act, el) {
    const key = el.dataset.key;
    if (act === 'open') {
      const spec = this.specs.get(key);
      this.panels.set(key, { reason: spec?.reason || '', expires: '7d' });
      this._changed(`r:${key}`);
    } else if (act === 'cancel') {
      this.panels.delete(key); this._changed();
    } else if (act === 'expiry') {
      if (key === 'manual') this.manual.expires = el.dataset.value;
      else { const p = this.panels.get(key); if (p) p.expires = el.dataset.value; }
      this._changed();
    } else if (act === 'confirm') {
      this._withSession(() => this._block(key), this._describe(key));
    } else if (act === 'unblock') {
      const targets = (el.dataset.targets || '').split(',').filter(Boolean);
      this._withSession(() => this._unblock(key, targets), `unblock ${targets.join(', ')}`);
    } else if (act === 'signin') {
      this.openSignIn();
    } else if (act === 'signout') {
      this._logout();
    }
  }

  _describe(key) {
    const spec = this.specs.get(key);
    const open = (spec?.targets || []).map((t) => t.target).filter((t) => !this.entryFor(t));
    return `block ${open.length === 1 ? open[0] : plural(open.length, 'address', 'addresses')}`;
  }

  _changed(focus) {
    this.onChange();
    this._renderSession();
    if (focus) {
      const el = document.querySelector(`[data-focus="${CSS.escape(focus)}"]`);
      if (el) { el.focus(); el.select?.(); }
    }
  }

  // Run an action now if signed in, otherwise ask for the password first and then run it.
  _withSession(fn, what) {
    if (this.signedIn) return fn();
    this.openSignIn(fn, what);
  }

  async _block(key) {
    const spec = this.specs.get(key), p = this.panels.get(key);
    if (!spec || !p || this.busy.has(key)) return;
    const targets = spec.targets.map((t) => t.target).filter((t) => !this.entryFor(t));
    if (!targets.length) { this.panels.delete(key); this._changed(); return; }
    this.busy.add(key); this._changed();
    const res = await this._send('POST', 'api/v1/blocks', { targets, reason: p.reason.trim(), expires: p.expires,
      ...(spec.finding ? { finding: spec.finding } : {}), ...(spec.label ? { label: spec.label } : {}) });
    this.busy.delete(key);
    if (res.status === 401) return this._signedOut(() => this._block(key), this._describe(key));
    if (res.ok) {
      this.panels.delete(key);
      this._setBlocks(res.data);
      this._afterBlock(res.data.added || [], res.data.errors || [], p.expires);
    } else this.notify(`Could not block: ${res.detail}`, 'err');
    this._changed();
  }

  async _manual() {
    const m = this.manual;
    if (!m.target.trim()) { this.notify('Type an address or range to block.', 'err'); return; }
    if (this.busy.has('manual')) return;
    if (!this.signedIn) { this.openSignIn(() => this._manual(), `block ${m.target.trim()}`); return; }
    this.busy.add('manual'); this._changed();
    const targets = m.target.split(/[\s,]+/).filter(Boolean);
    const res = await this._send('POST', 'api/v1/blocks', { targets, reason: m.reason.trim(), expires: m.expires, label: 'Added by hand' });
    this.busy.delete('manual');
    if (res.status === 401) return this._signedOut(() => this._manual(), `block ${m.target.trim()}`);
    if (res.ok) {
      this._setBlocks(res.data);
      const failed = new Set((res.data.errors || []).map((e) => e.target));
      this.manual.target = targets.filter((t) => failed.has(t)).join(' ');
      if (!this.manual.target) this.manual.reason = '';
      this._afterBlock(res.data.added || [], res.data.errors || [], m.expires);
    } else this.notify(`Could not block: ${res.detail}`, 'err');
    this._changed();
  }

  _afterBlock(added, errors, expires) {
    if (added.length) {
      const what = added.length === 1 ? added[0].target : plural(added.length, 'address', 'addresses');
      const when = this.session && !this.session.routerSync ? 'The router is not set up to pull this list yet.' : 'The router picks it up within a minute.';
      this.notify(`Blocked ${what} ${EXPIRY_TEXT[expires] || ''}. ${when}`, 'ok');
      this.reloadSoon();
    }
    if (errors.length) this.notify(errors.map((e) => `${e.target}: ${e.error}`).join(' · '), 'err');
  }

  async _unblock(key, targets) {
    if (this.busy.has(key) || !targets.length) return;
    this.busy.add(key); this._changed();
    const done = [], failed = [];
    for (const t of targets) {
      const res = await this._send('DELETE', 'api/v1/blocks/' + encodeURIComponent(t));
      if (res.status === 401) { this.busy.delete(key); return this._signedOut(() => this._unblock(key, targets), `unblock ${targets.join(', ')}`); }
      if (res.ok) { done.push(t); this._setBlocks(res.data); } else failed.push(`${t}: ${res.detail}`);
    }
    this.busy.delete(key);
    if (done.length) { this.notify(`Unblocked ${done.join(', ')}. The router drops it on its next check.`, 'ok'); this.reloadSoon(); }
    if (failed.length) this.notify(`Could not unblock ${failed.join(' · ')}`, 'err');
    this._changed();
  }

  _signedOut(retry, what) {
    if (this.session) this.session.signedIn = false;
    this._changed();
    this.openSignIn(retry, what, 'Your session has ended. Sign in again to carry on.');
  }

  // ------------------------------------------------------------------ sign-in
  openSignIn(then = null, what = '', note = '') {
    this.pendingSignIn = then;
    const box = document.getElementById('signin');
    document.getElementById('signinWhat').textContent = note || (what ? `Sign in to ${what}.` : 'Sign in to block and unblock addresses.');
    this._signinError(this.session?.lockedFor ? `Too many wrong passwords; try again in ${fmtDur(this.session.lockedFor)}.` : '');
    box.hidden = false;
    const pw = document.getElementById('signinPassword');
    pw.value = '';
    setTimeout(() => pw.focus(), 0);
  }

  _closeSignIn() {
    document.getElementById('signin').hidden = true;
    this.pendingSignIn = null;
  }

  _signinError(text) {
    const el = document.getElementById('signinError');
    el.textContent = text;
    el.hidden = !text;
  }

  async _login() {
    const pw = document.getElementById('signinPassword'), btn = document.getElementById('signinSubmit');
    if (!pw.value) { this._signinError('Type the password.'); pw.focus(); return; }
    btn.disabled = true; btn.textContent = 'Signing in…';
    const res = await this._send('POST', 'api/v1/login', { password: pw.value });
    btn.disabled = false; btn.textContent = 'Sign in';
    if (!res.ok) {
      this._signinError(res.status === 401 ? 'Wrong password.' : res.status === 429 ? cap(res.detail) + '.' : `Could not sign in: ${res.detail}`);
      pw.select();
      return;
    }
    this.session = { ...(this.session || {}), signedIn: true, lockedFor: 0 };
    const then = this.pendingSignIn;
    this.pendingSignIn = null;
    document.getElementById('signin').hidden = true;
    this.notify('Signed in. Block and Unblock are available for 12 hours.', 'ok');
    this._changed();
    if (then) await then();
  }

  async _logout() {
    const res = await this._send('POST', 'api/v1/logout');
    if (!res.ok) { this.notify(`Could not sign out: ${res.detail}`, 'err'); return; }
    if (this.session) this.session.signedIn = false;
    this.panels.clear();
    this.notify('Signed out.', 'ok');
    this._changed();
  }

  // The top-bar control: nothing when actions are off, else "Sign in" or "Signed in · Sign out".
  _renderSession() {
    const el = document.getElementById('session');
    if (!el) return;
    el.hidden = !this.enabled;
    if (!this.enabled) { el.innerHTML = ''; return; }
    el.innerHTML = this.signedIn
      ? '<span class="chip ok si-chip" title="You can block and unblock addresses">● Signed in · <button class="link" data-act="signout">Sign out</button></span>'
      : '<button class="btn small" data-act="signin" title="Viewing is open to everyone; blocking needs the password">Sign in</button>';
  }

  // ------------------------------------------------------------------ feedback
  notify(text, kind = 'ok') {
    const host = document.getElementById('notices');
    if (!host) return;
    const el = document.createElement('div');
    el.className = `notice glass ${kind}`;
    el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 400); }, kind === 'err' ? 8000 : 4500);
  }
}

function cap(s) { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); }

// Keep the caret where it was when a live refresh rebuilds a panel the user is typing in.
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

// The settings area: where each module gets its data from.
//
// One sign-in for the suite. Each module's form is drawn from what the module itself says it takes
// (its fields, labels and help), so a new module needs no page of its own. Passwords are write-only:
// the page is told whether one is set, never what it is.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'workload-galaxy' };

async function api(path, method = 'GET', body) {
  const res = await fetch(path, { method, credentials: 'same-origin', headers: method === 'GET' ? { Accept: 'application/json' } : HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) { const err = new Error(data?.detail || `${res.status} ${res.statusText}`); err.status = res.status; throw err; }
  return data;
}

const SOURCE = { saved: ['saved here', 'saved'], environment: ['set where the module is deployed', ''], default: ['default', ''] };

export class SettingsView {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;               // { modules(): [...], onModule(id), onChanged() }
    this.session = null;
    this.module = null;           // id of the module whose form is open
    this.form = null;             // what the module said about its settings
    this.message = null;          // { cls, text } under the buttons
    this.busy = '';
    el.addEventListener('click', (ev) => this._click(ev));
    el.addEventListener('submit', (ev) => { ev.preventDefault(); this._submit(ev.target); });
  }

  async open(id) {
    const mods = this.ctx.modules();
    this.module = mods.some((m) => m.id === id) ? id : this.module && mods.some((m) => m.id === this.module) ? this.module : mods[0]?.id || null;
    this.form = null;
    this.message = null;
    this.render();
    try { this.session = await api('api/v1/session'); } catch (err) { this.session = { error: err.message }; }
    this.render();
    if (this.session?.signedIn) await this._load();
  }

  async _load() {
    if (!this.module) return;
    const id = this.module;
    this.form = null;
    this.render();
    try {
      const form = await api(`api/v1/modules/${encodeURIComponent(id)}/settings`);
      if (this.module === id) this.form = form;
    } catch (err) {
      if (err.status === 401) { this.session = { ...this.session, signedIn: false }; }
      else if (this.module === id) this.form = { error: err.message };
    }
    this.render();
  }

  // ------------------------------------------------------------------ drawing
  render() {
    const mods = this.ctx.modules();
    const s = this.session;
    const nav = mods.map((m) => `<li data-module="${esc(m.id)}" aria-current="${m.id === this.module}" tabindex="0">
        <span class="dot ${m.ok === false ? 'down' : m.ok == null ? 'off' : m.level || 'ok'}"></span><span class="t">${esc(m.title)}</span>
        <span class="s">${esc(m.ok === false ? 'not reachable' : m.name || m.summary || '')}</span></li>`).join('');
    this.el.innerHTML = `<div class="st-wrap">
      <div class="st-head"><h1>Settings</h1><p>Where each module gets its data from. Changes are saved by the module and used straight away.</p>
        ${s?.signedIn ? '<button class="link" data-signout>Sign out</button>' : ''}</div>
      <ul class="st-nav" aria-label="Modules">${nav || '<li class="s">No modules are set up.</li>'}</ul>
      <div class="st-panel">${this._panel(mods.find((m) => m.id === this.module))}</div></div>`;
  }

  _panel(mod) {
    const s = this.session;
    if (!s) return '<p class="lead">Loading…</p>';
    if (s.error) return `<h2>Settings are not available</h2><p class="lead">${esc(s.error)}</p>`;
    if (!s.enabled) {
      return `<h2>The settings area is turned off</h2>
        <p class="lead">It opens once the shell has been given an admin password. Set <code>GALAXY_ADMIN_PASSWORD</code> on the shell, and the same
        <code>GALAXY_SETTINGS_TOKEN</code> on the shell and on each module (as its own settings token), then restart them.</p>
        <p class="st-note">Until then every module keeps the settings it was deployed with.</p>`;
    }
    if (!s.signedIn) {
      return `<h2>Sign in</h2><p class="lead">Settings hold the addresses and accounts the modules use, so they are behind the suite's admin password. Viewing the modules needs none.</p>
        <form class="st-sign" data-form="signin" autocomplete="on">
          <input type="text" name="username" value="admin" autocomplete="username" hidden>
          <input type="password" name="password" placeholder="Admin password" autocomplete="current-password" aria-label="Admin password" ${s.lockedFor ? 'disabled' : ''} required>
          <div><button class="st-btn primary" type="submit" ${s.lockedFor ? 'disabled' : ''}>Sign in</button></div>
          ${this.message ? `<p class="st-msg ${this.message.cls}" role="alert">${esc(this.message.text)}</p>` : s.lockedFor ? `<p class="st-msg bad">Too many wrong passwords. Try again in ${s.lockedFor} s.</p>` : ''}
        </form>`;
    }
    if (!mod) return '<p class="lead">Pick a module.</p>';
    const f = this.form;
    if (!f) return `<h2>${esc(mod.title)}</h2><p class="lead">Loading…</p>`;
    if (f.error) return `<h2>${esc(mod.title)}</h2><p class="st-msg bad">${esc(f.error)}</p><div class="st-actions"><button class="st-btn" data-reload>Try again</button></div>`;
    if (!f.supported) {
      return `<h2>${esc(mod.title)}</h2><p class="lead">This module does not take settings from here yet. It uses what it was deployed with
        (its environment and config files).</p><p class="st-note">It is running version ${esc(mod.version || 'unknown')}.</p>`;
    }
    const fields = f.fields || [];
    const main = fields.filter((x) => !x.advanced), more = fields.filter((x) => x.advanced);
    const grouped = (list) => {
      const out = [];
      let last = null;
      for (const x of list) {
        if (x.group !== last) { out.push(`<h3>${esc(x.group || 'General')}</h3>`); last = x.group; }
        out.push(this._field(x, f.canSave));
      }
      return out.join('');
    };
    const why = f.canSave ? '' : !s.canChange
      ? 'The shell has no settings token, so these can be read here but not changed.'
      : 'This module was deployed without a settings token, so it does not accept changes from here.';
    return `<form data-form="module" autocomplete="off"><h2>${esc(mod.title)}</h2>
      <p class="lead">${esc(mod.summary ? mod.summary + '.' : '')}${f.mode === 'sample' ? ' Showing sample data now.' : ''}</p>
      ${(f.missing || []).length ? `<p class="st-msg warn">Still needed before it can read anything: ${esc(f.missing.join(', '))}.</p>` : ''}
      ${grouped(main)}
      ${more.length ? `<details class="st-more"><summary>More settings (${more.length})</summary>${grouped(more)}</details>` : ''}
      <div class="st-actions">
        <button class="st-btn" type="button" data-test ${f.canSave && !this.busy ? '' : 'disabled'}>${this.busy === 'test' ? 'Testing…' : 'Test connection'}</button>
        <button class="st-btn primary" type="submit" ${f.canSave && !this.busy ? '' : 'disabled'}>${this.busy === 'save' ? 'Saving…' : 'Save and apply'}</button>
        <span class="st-note">${esc(why || (f.persistent ? 'Saved settings survive a restart.' : 'This module has no data folder: saved settings last until it restarts.'))}</span>
      </div>
      ${this.message ? `<p class="st-msg ${this.message.cls}" role="status">${esc(this.message.text)}</p>` : ''}</form>`;
  }

  _field(x, editable) {
    const id = `st-${x.name}`;
    const dis = editable ? '' : 'disabled';
    const [srcText, srcCls] = SOURCE[x.source] || ['', ''];
    let ctl;
    if (x.secret) {
      ctl = `<input type="password" id="${id}" name="${esc(x.name)}" autocomplete="new-password" ${dis}
        placeholder="${x.set ? 'Saved. Leave empty to keep it.' : 'Not set'}">`;
    } else if (x.type === 'bool') {
      ctl = `<span class="check"><input type="checkbox" id="${id}" name="${esc(x.name)}" ${x.value ? 'checked' : ''} ${dis}> <label for="${id}" style="padding:0;font-weight:400">On</label></span>`;
    } else if ((x.choices || []).length) {
      ctl = `<select id="${id}" name="${esc(x.name)}" ${dis}>${x.choices.map((c) => `<option ${c === x.value ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
    } else if (x.type === 'text' || x.type === 'json') {
      ctl = `<textarea id="${id}" name="${esc(x.name)}" spellcheck="false" ${dis}>${esc(x.value || '')}</textarea>`;
    } else if (x.type === 'int') {
      ctl = `<input type="number" id="${id}" name="${esc(x.name)}" value="${esc(x.value ?? '')}" ${x.minimum != null ? `min="${x.minimum}"` : ''} ${dis}>`;
    } else {
      ctl = `<input type="text" id="${id}" name="${esc(x.name)}" value="${esc(Array.isArray(x.value) ? x.value.join(', ') : x.value ?? '')}" spellcheck="false" ${dis}>`;
    }
    return `<div class="st-field"><label for="${id}">${esc(x.label)}${x.required ? ' *' : ''}<span class="src ${srcCls}">${esc(x.secret ? (x.set ? srcText : 'not set') : srcText)}</span></label>
      <div class="ctl">${ctl}${x.help ? `<p class="help">${esc(x.help)}</p>` : ''}</div></div>`;
  }

  // What the form holds that differs from what the module has. Empty password boxes are left out:
  // empty means "keep it".
  _changes(form) {
    const out = {};
    for (const x of this.form.fields || []) {
      const el = form.elements[x.name];
      if (!el) continue;
      if (x.secret) { if (el.value) out[x.name] = el.value; continue; }
      const now = x.type === 'bool' ? el.checked : x.type === 'int' ? (el.value === '' ? '' : Number(el.value)) : el.value;
      const was = x.type === 'bool' ? !!x.value : Array.isArray(x.value) ? x.value.join(', ') : x.value ?? '';
      if (String(now).trim() !== String(was).trim()) out[x.name] = now;
    }
    return out;
  }

  // ------------------------------------------------------------------ actions
  _click(ev) {
    const t = ev.target.closest('[data-module],[data-signout],[data-reload],[data-test]');
    if (!t) return;
    if (t.dataset.module) { this.message = null; this.module = t.dataset.module; this.ctx.onModule(this.module); if (this.session?.signedIn) this._load(); else this.render(); return; }
    if (t.dataset.signout != null) { api('api/v1/logout', 'POST', {}).catch(() => {}).then(() => { this.message = null; this.open(this.module); }); return; }
    if (t.dataset.reload != null) { this._load(); return; }
    if (t.dataset.test != null) this._send(t.closest('form'), 'test');
  }

  async _submit(form) {
    if (form.dataset.form === 'signin') {
      try {
        await api('api/v1/login', 'POST', { password: form.elements.password.value });
        this.message = null;
        await this.open(this.module);
      } catch (err) {
        this.message = { cls: 'bad', text: err.status === 401 ? 'That is not the admin password.' : err.message };
        try { this.session = await api('api/v1/session'); } catch { /* keep the last one */ }
        this.render();
        this.el.querySelector('input[type="password"]')?.focus();
      }
      return;
    }
    await this._send(form, 'save');
  }

  async _send(form, what) {
    const values = this._changes(form);
    if (what === 'save' && !Object.keys(values).length) { this.message = { cls: 'warn', text: 'Nothing was changed.' }; this.render(); return; }
    const typed = new Map([...form.elements].filter((e) => e.name).map((e) => [e.name, e.type === 'checkbox' ? e.checked : e.value]));
    this.busy = what;
    this.message = null;
    this.render();
    const path = `api/v1/modules/${encodeURIComponent(this.module)}/settings${what === 'test' ? '/test' : ''}`;
    try {
      const res = await api(path, what === 'test' ? 'POST' : 'PUT', { values });
      if (what === 'test') {
        this.message = { cls: res.ok ? 'ok' : 'bad', text: res.message || (res.ok ? 'It works.' : 'It did not work.') };
      } else {
        this.form = res;
        const forgot = (res.forgot || []).length ? ` ${res.forgot.join(', ')} was forgotten because what it belongs to changed: enter it again.` : '';
        const how = res.restarting ? 'The module is restarting with the new settings; give it a few seconds.' : 'The module is reading again with the new settings.';
        this.message = { cls: forgot ? 'warn' : 'ok', text: `Saved. ${how}${forgot}` };
        this.ctx.onChanged();
      }
    } catch (err) {
      if (err.status === 401) { this.session = { ...this.session, signedIn: false }; this.message = { cls: 'bad', text: 'Your sign-in ran out. Sign in again.' }; }
      else this.message = { cls: 'bad', text: err.message };
    }
    this.busy = '';
    this.render();
    // A test, or a save that failed, leaves what was typed in place.
    if (what === 'test' || this.message?.cls === 'bad') {
      const f = this.el.querySelector('form[data-form="module"]');
      if (f) for (const [name, v] of typed) { const e = f.elements[name]; if (e) { if (e.type === 'checkbox') e.checked = v; else e.value = v; } }
    }
  }
}

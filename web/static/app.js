// Stage Bulbs web UI (Alpine.js, no build step).
/* global Alpine */

function kelvinToRgb(k) {
  // Tanner Helland's approximation, good enough for a swatch.
  const t = k / 100;
  let r, g, b;
  if (t <= 66) { r = 255; g = 99.47 * Math.log(t) - 161.12; b = t <= 19 ? 0 : 138.52 * Math.log(t - 10) - 305.04; }
  else { r = 329.7 * Math.pow(t - 60, -0.1332); g = 288.12 * Math.pow(t - 60, -0.0755); b = 255; }
  const c = (x) => Math.max(0, Math.min(255, Math.round(x)));
  return [c(r), c(g), c(b)];
}

function hsvToRgb(h, s, v) {
  s /= 100; v /= 100;
  const f = (n) => { const k = (n + h / 60) % 6; return v - v * s * Math.max(0, Math.min(k, 4 - k, 1)); };
  return [f(5), f(3), f(1)].map((x) => Math.round(x * 255));
}

function app() {
  return {
    loaded: false,
    session: { setup_needed: false, authenticated: false },
    pw: '', pw2: '', ssid: '', wifiPw: '', loginError: '',
    tabs: [{ id: 'live', label: 'Live' }, { id: 'bulbs', label: 'Bulbs' }, { id: 'control', label: 'Control' }, { id: 'setup', label: 'Setup' }],
    tab: 'live',
    cfg: null, warnings: [], nextFree: null, fwImages: [], settings: null,
    live: null, fps: 0, _frames: null, _framesT: 0, ws: null, _wsRetry: 1000,
    view: 'map', selectMode: false, selected: [], editLayout: false, sheet: null,
    found: null, discovering: false, newGroup: '', lookName: '',
    color: { h: 30, s: 80, v: 70 }, temp: 3200, mode: 'hsv', _sendTimer: null,
    powerOn: { k: 2700, v: 80 }, pwChange: { current: '', next: '' },
    toast: null, _toastTimer: null, _drag: null,
    presets: [
      { label: 'Warm', k: 2700 }, { label: 'Neutral', k: 4000 }, { label: 'Cool', k: 6000 },
      { label: 'Red', h: 0, s: 100 }, { label: 'Blue', h: 240, s: 100 }, { label: 'Off', off: true },
    ],

    // ---------- startup, session ----------
    async init() {
      try { this.tab = localStorage.getItem('tab') || 'live'; } catch (e) { /* storage blocked */ }
      await this.refreshSession();
      this.loaded = true;
      if (this.session.authenticated) await this.start();
    },
    async refreshSession() {
      const r = await fetch('/api/session');
      this.session = await r.json();
    },
    async start() {
      await this.loadState();
      this.connect();
      this.$nextTick(() => this.drawWheel());
    },
    async doSetup() {
      this.loginError = '';
      if (this.pw !== this.pw2) { this.loginError = "The passwords don't match."; return; }
      try {
        await this.api('POST', '/api/setup', { password: this.pw, ssid: this.ssid, wifi_password: this.wifiPw });
        this.pw = this.pw2 = this.wifiPw = '';
        await this.refreshSession(); await this.start();
      } catch (e) { this.loginError = e.message; }
    },
    async doLogin() {
      this.loginError = '';
      try {
        await this.api('POST', '/api/login', { password: this.pw });
        this.pw = '';
        await this.refreshSession(); await this.start();
      } catch (e) { this.loginError = e.message; }
    },
    async logout() {
      await this.api('POST', '/api/logout');
      if (this.ws) this.ws.close();
      this.cfg = null;
      await this.refreshSession();
    },
    setTab(t) {
      this.tab = t;
      try { localStorage.setItem('tab', t); } catch (e) { /* ignore */ }
      if (t === 'control') this.$nextTick(() => this.drawWheel());
    },

    // ---------- API ----------
    async api(method, path, body) {
      const r = await fetch(path, {
        method, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
      if (r.status === 401 && !path.startsWith('/api/login')) {
        this.session.authenticated = false;
      }
      let data = null;
      try { data = await r.json(); } catch (e) { /* no body */ }
      if (!r.ok) {
        const d = data && data.detail;
        throw new Error(Array.isArray(d) ? d.join('; ') : (d || `${r.status} ${r.statusText}`));
      }
      return data;
    },
    async act(promise, ok) {
      try { const r = await promise; if (ok) this.say(ok); return r; }
      catch (e) { this.say(e.message, true); return null; }
    },
    say(text, error = false) {
      this.toast = { text, error };
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast = null; }, error ? 6000 : 2500);
    },
    async loadState() {
      const s = await this.api('GET', '/api/state');
      this.cfg = s.config; this.warnings = s.warnings; this.nextFree = s.next_free_channel;
      this.fwImages = s.firmware_images; this.live = s.live;
      this.settings = JSON.parse(JSON.stringify({
        input: s.config.input, sender: s.config.sender, dmx_loss: s.config.dmx_loss, network: s.config.network,
      }));
    },
    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/api/live`);
      this.ws = ws;
      ws.onmessage = (ev) => {
        const d = JSON.parse(ev.data);
        const now = Date.now();
        if (this._frames != null && now - this._framesT >= 1000) {
          this.fps = Math.max(0, Math.round((d.input.frames - this._frames) * 1000 / (now - this._framesT)));
          this._frames = d.input.frames; this._framesT = now;
        } else if (this._frames == null) { this._frames = d.input.frames; this._framesT = now; }
        this.live = d;
        this._wsRetry = 1000;
      };
      ws.onclose = (ev) => {
        if (ev.code === 4401) { this.session.authenticated = false; return; }
        if (!this.session.authenticated) return;
        setTimeout(() => this.connect(), this._wsRetry);
        this._wsRetry = Math.min(this._wsRetry * 2, 10000);
      };
    },

    // ---------- bulb data ----------
    bulbList() {
      return Object.entries(this.cfg.bulbs).map(([mac, b]) => ({ mac, ...b }))
        .sort((a, b) => (a.channel ?? 999) - (b.channel ?? 999) || a.name.localeCompare(b.name));
    },
    placedBulbs() { return this.bulbList().filter((b) => b.pos); },
    unplacedBulbs() { return this.bulbList().filter((b) => !b.pos); },
    liveOf(mac) { return (this.live && this.live.bulbs[mac]) || {}; },
    isOnline(mac) { return !!this.liveOf(mac).online; },
    onlineCount() { return this.live ? this.live.online : 0; },
    channelLabel(b) {
      if (b.follow) { const g = this.cfg.groups[b.follow]; return `${b.follow} (${g && g.channel ? g.channel : '–'})`; }
      return b.channel ? `${b.channel}–${b.channel + 2}` : 'unpatched';
    },
    sourceLabel(mac) {
      const s = this.liveOf(mac).source;
      const b = this.cfg.bulbs[mac];
      if (b && !b.dmx) return s === 'dmx' ? 'DMX off' : 'manual (DMX off)';
      return { dmx: 'DMX', manual: 'manual', look: 'look', loss: 'DMX lost' }[s] || '–';
    },
    badge(mac) {
      const l = this.liveOf(mac);
      if (l.backoff) return 'slow';
      if (l.source === 'manual' || l.source === 'look') return 'M';
      return '';
    },
    inputProblems() {
      const i = this.live ? this.live.input : {};
      const parts = [];
      if (i.malformed) parts.push(`${i.malformed} bad frames`);
      if (i.restarts) parts.push(`${i.restarts} restarts`);
      return parts.join(', ') || 'input errors';
    },
    cssFromState(st, solid = false) {
      if (!st) return 'background:#222';
      if (st.v === 0) return 'background:#111';
      const rgb = st.k != null ? kelvinToRgb(st.k) : hsvToRgb(st.h, st.s, 100);
      const level = solid ? 0.35 + 0.65 * st.v / 100 : 0.3 + 0.7 * st.v / 100;
      const [r, g, b] = rgb.map((x) => Math.round(x * level));
      return `background:rgb(${r},${g},${b}); --glow: rgba(${rgb.join(',')},${(st.v / 100) * 0.6})`;
    },
    lampStyle(mac, solid = false) { return this.cssFromState(this.liveOf(mac).state, solid); },

    // ---------- selection, map ----------
    toggleSelect(mac) {
      const i = this.selected.indexOf(mac);
      if (i >= 0) this.selected.splice(i, 1); else this.selected.push(mac);
    },
    selectGroup(g) {
      this.selected = this.bulbList().filter((b) => b.groups.includes(g)).map((b) => b.mac);
      this.selectMode = true;
      this.say(`${this.selected.length} bulbs in ${g}`);
    },
    tapBulb(mac) {
      if (this._drag && this._drag.moved) return;
      if (this.editLayout) return;
      if (this.selectMode) { this.toggleSelect(mac); return; }
      this.openSheet(mac);
    },
    openSheet(mac) {
      const st = this.liveOf(mac).state;
      if (st) {
        if (st.k != null) { this.temp = st.k; this.color.v = st.v; this.mode = 'temp'; }
        else { this.color = { h: st.h, s: st.s, v: st.v }; this.mode = 'hsv'; }
      }
      this.sheet = mac;
    },
    openControlFor(mac) { this.selected = [mac]; this.sheet = null; this.setTab('control'); },
    dragStart(ev, mac) {
      if (!this.editLayout) { this._drag = null; return; }
      ev.preventDefault();
      const stage = this.$refs.stage;
      const el = ev.currentTarget;
      el.setPointerCapture(ev.pointerId);
      this._drag = { mac, moved: false };
      const move = (e) => {
        const r = stage.getBoundingClientRect();
        const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
        const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
        this._drag.moved = true;
        this.cfg.bulbs[mac].pos = [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000];
      };
      const up = async () => {
        el.removeEventListener('pointermove', move);
        el.removeEventListener('pointerup', up);
        if (this._drag.moved) await this.act(this.api('PATCH', `/api/bulbs/${mac}`, { pos: this.cfg.bulbs[mac].pos }));
        setTimeout(() => { this._drag = null; }, 0);
      };
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', up);
    },

    // ---------- bulbs tab ----------
    async discover() {
      this.discovering = true;
      const r = await this.act(this.api('POST', '/api/discover'));
      this.discovering = false;
      if (r) this.found = r.found;
    },
    newFound() { return (this.found || []).filter((f) => !f.known); },
    async addBulb(f) {
      const r = await this.act(this.api('POST', '/api/bulbs', { mac: f.mac, ip: f.ip, name: f.alias, channel: 'next' }), `Added ${f.alias}`);
      if (r) { f.known = true; await this.loadState(); }
    },
    async editBulb(mac, change) {
      const r = await this.act(this.api('PATCH', `/api/bulbs/${mac}`, change));
      await this.loadState();
      return r;
    },
    setPatchMode(b, v) {
      if (v === 'own') return this.editBulb(b.mac, { follow: null, channel: b.channel || this.nextFree });
      if (v === 'none') return this.editBulb(b.mac, { follow: null, channel: null });
      return this.editBulb(b.mac, { follow: v.slice(2) });
    },
    toggleGroup(b, g) {
      const groups = b.groups.includes(g) ? b.groups.filter((x) => x !== g) : [...b.groups, g];
      return this.editBulb(b.mac, { groups });
    },
    async removeBulb(b) {
      if (this._confirmRemove !== b.mac) { this._confirmRemove = b.mac; this.say(`Press Remove again to remove ${b.name}`); return; }
      this._confirmRemove = null;
      await this.act(this.api('DELETE', `/api/bulbs/${b.mac}`), `Removed ${b.name}`);
      this.selected = this.selected.filter((m) => m !== b.mac);
      await this.loadState();
    },
    async autoAssign() {
      const start = this.nextFree || 1;
      await this.act(this.api('POST', '/api/bulbs/auto-assign', { macs: this.selected, start }), `Assigned from channel ${start}`);
      await this.loadState();
    },
    async identify(mac) { await this.act(this.api('POST', `/api/bulbs/${mac}/identify`), 'Blinking…'); },
    async addGroup() {
      const name = this.newGroup.trim();
      if (!name) return;
      await this.act(this.api('PUT', `/api/groups/${encodeURIComponent(name)}`, { channel: null }));
      this.newGroup = '';
      await this.loadState();
    },
    async putGroup(name, channel) {
      await this.act(this.api('PUT', `/api/groups/${encodeURIComponent(name)}`, { channel }));
      await this.loadState();
    },
    async deleteGroup(name) {
      await this.act(this.api('DELETE', `/api/groups/${encodeURIComponent(name)}`), `Deleted ${name}`);
      await this.loadState();
    },
    async setPowerOn() {
      const macs = this.targets(true);
      const r = await this.act(this.api('POST', '/api/power-on', { macs, state: { k: this.powerOn.k, v: this.powerOn.v } }));
      if (r) {
        const bad = Object.entries(r.results).filter(([, ok]) => !ok).length;
        this.say(bad ? `${bad} bulb(s) didn't confirm` : `Power-on default set on ${macs.length} bulb(s)`, !!bad);
      }
    },
    fwJob(mac) { return this.live && this.live.firmware && this.live.firmware[mac]; },
    async updateFirmware(mac) {
      if (this._confirmFw !== mac) { this._confirmFw = mac; this.say('Press Firmware again to update this bulb (it goes dark for about a minute)'); return; }
      this._confirmFw = null;
      await this.act(this.api('POST', `/api/bulbs/${mac}/firmware`, {}), 'Updating firmware…');
    },

    // ---------- control ----------
    targets(forPowerOn = false) {
      if (this.sheet && !forPowerOn) return [this.sheet];
      return this.selected.length ? [...this.selected] : Object.keys(this.cfg.bulbs);
    },
    targetLabel() {
      const t = this.targets();
      if (t.length === Object.keys(this.cfg.bulbs).length) return `all ${t.length} bulbs`;
      if (t.length === 1) return this.cfg.bulbs[t[0]].name;
      return `${t.length} bulbs`;
    },
    queueSend(state) {
      clearTimeout(this._sendTimer);
      this._sendTimer = setTimeout(() => this.act(this.api('POST', '/api/control', { macs: this.targets(), state })), 60);
    },
    sendColor() {
      if (this.mode === 'temp') { this.queueSend({ k: this.temp, v: this.color.v }); return; }
      this.queueSend({ h: this.color.h, s: this.color.s, v: this.color.v });
    },
    sendTemp() { this.mode = 'temp'; this.queueSend({ k: this.temp, v: this.color.v }); },
    applyPreset(p) {
      if (p.off) { this.color.v = 0; this.sendColor(); return; }
      if (this.color.v === 0) this.color.v = 70;
      if (p.k) { this.temp = p.k; this.sendTemp(); return; }
      this.mode = 'hsv'; this.color.h = p.h; this.color.s = p.s; this.sendColor();
    },
    drawWheel() {
      const c = this.$refs.wheel;
      if (!c) return;
      const ctx = c.getContext('2d');
      const n = c.width, r = n / 2;
      const img = ctx.createImageData(n, n);
      for (let y = 0; y < n; y++) {
        for (let x = 0; x < n; x++) {
          const dx = x - r, dy = y - r, d = Math.sqrt(dx * dx + dy * dy);
          const i = (y * n + x) * 4;
          if (d > r) { img.data[i + 3] = 0; continue; }
          const h = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
          const [R, G, B] = hsvToRgb(h, (d / r) * 100, 100);
          img.data[i] = R; img.data[i + 1] = G; img.data[i + 2] = B; img.data[i + 3] = 255;
        }
      }
      ctx.putImageData(img, 0, 0);
    },
    wheelPick(ev) {
      const c = this.$refs.wheel;
      const rect = c.getBoundingClientRect();
      const dx = ev.clientX - rect.left - rect.width / 2, dy = ev.clientY - rect.top - rect.height / 2;
      const d = Math.min(1, Math.sqrt(dx * dx + dy * dy) / (rect.width / 2));
      this.mode = 'hsv';
      this.color.h = Math.round((Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360);
      this.color.s = Math.round(d * 100);
      if (this.color.v === 0) this.color.v = 70;
      this.sendColor();
    },
    async saveLook() {
      const name = this.lookName.trim();
      if (!name) { this.say('Give the look a name first', true); return; }
      const r = await this.act(this.api('POST', '/api/looks', { name, macs: this.selected.length ? this.selected : null }));
      if (r) { this.say(`Saved "${name}" (${r.bulbs} bulbs)`); this.lookName = ''; await this.loadState(); }
    },
    async recallLook(name) { await this.act(this.api('POST', `/api/looks/${encodeURIComponent(name)}/recall`), `Recalled ${name}`); },
    async deleteLook(name) {
      await this.act(this.api('DELETE', `/api/looks/${encodeURIComponent(name)}`), `Deleted ${name}`);
      await this.loadState();
    },

    // ---------- setup ----------
    async saveSettings() {
      const r = await this.act(this.api('PUT', '/api/settings', this.settings), 'Settings saved');
      if (r && r.input_restarted) this.say('Settings saved; DMX input restarted');
      await this.loadState();
    },
    async restore(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        await this.act(this.api('POST', '/api/restore', data), 'Backup restored');
        await this.loadState();
      } catch (e) { this.say("That file isn't a valid backup", true); }
      ev.target.value = '';
    },
    async changePassword() {
      const r = await this.act(this.api('POST', '/api/password', { current: this.pwChange.current, new: this.pwChange.next }), 'Password changed');
      if (r) this.pwChange = { current: '', next: '' };
    },
  };
}

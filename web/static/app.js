// DMX Smart Bulbs web UI (Alpine.js, no build step).
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
    tabs: [{ id: 'live', label: 'Live' }, { id: 'bulbs', label: 'Bulbs' }, { id: 'board', label: 'Control Board' }, { id: 'setup', label: 'Setup' }],
    tab: 'live',
    cfg: null, warnings: [], overlaps: [], nextFree: null, fwImages: [], settings: null,
    live: null, fps: 0, _frames: null, _framesT: 0, ws: null, _wsRetry: 1000,
    view: 'map', selectMode: false, selected: [], editLayout: false, sheet: null,
    found: null, discovering: false, newGroup: '', lookName: '',
    info: {}, bulbFilter: '', bulbGroupFilter: '', bulbStatusFilter: '', menuFor: null, menuPos: { x: 0, y: 0 }, groupPopup: null, groupSheet: null,
    dmxPopup: false, assign: null, delayPop: false, bulkGroups: false, confirmDlg: null,
    board: null, boardMode: 'color', boardSel: [], boardColor: { h: 30, s: 80, v: 70 }, boardTemp: 3200,
    boardSpeed: 1, boardShowTarget: 'all', _boardTimers: {},
    color: { h: 30, s: 80, v: 70 }, temp: 3200, mode: 'hsv', _sendTimer: null,
    powerOn: { mode: 'white', k: 2700, h: 30, s: 80, v: 80 }, pwChange: { current: '', next: '' },
    toast: null, _toastTimer: null, _drag: null,
    presets: [
      { label: 'Warm', k: 2700 }, { label: 'Neutral', k: 4000 }, { label: 'Cool', k: 6000 },
      { label: 'Red', h: 0, s: 100 }, { label: 'Blue', h: 240, s: 100 }, { label: 'Off', off: true },
    ],

    // ---------- startup, session ----------
    async init() {
      try { this.tab = localStorage.getItem('tab') || 'live'; } catch (e) { /* storage blocked */ }
      if (!this.tabs.some((t) => t.id === this.tab)) this.tab = 'live';
      await this.refreshSession();
      this.loaded = true;
      if (this.session.authenticated) await this.start();
    },
    async refreshSession() {
      const r = await fetch('/api/session');
      this.session = await r.json();
      document.title = this.pageTitle();
    },
    pageTitle() {
      return (this.cfg && this.cfg.ui && this.cfg.ui.title) || this.session.title || 'DMX Smart Bulbs';
    },
    async start() {
      await this.loadState();
      if (this.tab === 'board') this.setTab('board');   // reopened on the Control Board tab
      this.connect();
      this.$watch('selected', () => this.syncPowerOn());
      // Firmware, signal and power-on defaults change slowly; refresh them now
      // and then while the Bulbs tab is open (not on Setup, so edits survive).
      if (!this._infoTimer) {
        this._infoTimer = setInterval(() => {
          if (this.session.authenticated && this.tab === 'bulbs' && !this.menuFor && !this.groupPopup) this.loadState();
        }, 30000);
      }
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
    visibleTabs() {
      const enttec = this.live && this.live.board && this.live.board.available;
      return this.tabs.filter((t) => t.id !== 'board' || enttec || this.tab === 'board');
    },
    setTab(t) {
      this.tab = t;
      try { localStorage.setItem('tab', t); } catch (e) { /* ignore */ }
      if (t === 'board') this.loadBoard().then(() => this.$nextTick(() => this.drawWheel(this.$refs.boardwheel)));
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
      this.cfg = s.config; this.warnings = s.warnings; this.nextFree = s.next_free_channel; this.overlaps = s.overlaps || [];
      this.fwImages = s.firmware_images; this.live = s.live; this.info = s.info || {};
      // The DMX input is set up on the Pi (config file / setup script), not here.
      this.settings = JSON.parse(JSON.stringify({
        sender: s.config.sender, dmx_loss: s.config.dmx_loss, network: s.config.network, identify: s.config.identify,
        ui: s.config.ui,
      }));
      document.title = this.pageTitle();
      this.syncPowerOn();
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
      return b.channel ? `${b.channel}–${b.channel + this.bulbSize(b) - 1}` : 'unpatched';
    },
    groupSize(name) {
      return Object.values(this.cfg.bulbs).some((b) => b.follow === name && b.mode === 'hsic') ? 4 : 3;
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
    dmxPillClass() {
      if (!this.live) return '';
      if (!this.live.dmx_enabled) return 'warn';
      return this.live.dmx ? 'ok' : 'bad';
    },
    dmxPillText() {
      if (!this.live) return 'DMX';
      if (!this.live.dmx_enabled) return 'DMX ignored';
      if (this.live.dmx) return 'DMX ' + this.fps + ' fps';
      return this.live.loss ? 'No DMX (loss action)' : 'No DMX';
    },
    async setDmxEnabled(on) {
      const r = await this.act(this.api('POST', '/api/dmx', { enabled: on }), on ? 'DMX controls the bulbs again' : 'DMX is now ignored');
      if (r && this.live) this.live.dmx_enabled = r.enabled;
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
      // Draw it as light: a dimmed bulb is the same color, less intense, not a darker
      // shade. Only very low levels fade toward black; the glow carries the brightness.
      const rgb = st.k != null ? kelvinToRgb(st.k) : hsvToRgb(st.h, st.s, 100);
      const level = Math.min(1, 0.25 + 1.5 * st.v / 100);
      const [r, g, b] = rgb.map((x) => Math.round(x * level));
      return `background:rgb(${r},${g},${b}); --glow: rgba(${rgb.join(',')},${0.15 + (st.v / 100) * 0.65})`;
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
    closeSheet() { this.sheet = null; this.groupSheet = null; },
    groupMembers(name) { return Object.keys(this.cfg.bulbs).filter((m) => this.cfg.bulbs[m].groups.includes(name)); },
    groupSwatch(name) {
      const m = this.groupMembers(name).find((x) => this.liveOf(x).state);
      return m ? this.lampStyle(m, true) : 'background:#2a2f33';
    },
    openGroupSheet(name) {
      const m = this.groupMembers(name).find((x) => this.liveOf(x).state);
      const st = m && this.liveOf(m).state;
      if (st) {
        if (st.k != null) { this.temp = st.k; this.color.v = st.v; this.mode = 'temp'; }
        else { this.color = { h: st.h, s: st.s, v: st.v }; this.mode = 'hsv'; }
      }
      this.sheet = null;
      this.groupSheet = name;
    },
    dragStart(ev, mac) {
      if (!this.editLayout) { this._drag = null; return; }
      ev.preventDefault();
      // Listen on the window: the bulb's element is re-created when it moves
      // between the tray and the stage, which would end an element-bound drag.
      const startPos = this.cfg.bulbs[mac].pos;
      this._drag = { mac, moved: false };
      const inStage = (e) => {
        const r = this.$refs.stage.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
        return { inside: x >= 0 && x <= 1 && y >= 0 && y <= 1, x, y };
      };
      const move = (e) => {
        const p = inStage(e);
        this._drag.moved = true;
        // Only onto the map while the pointer is over it; outside, it waits in the tray.
        this.cfg.bulbs[mac].pos = p.inside ? [Math.round(p.x * 1000) / 1000, Math.round(p.y * 1000) / 1000] : null;
      };
      const up = async (e) => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
        if (this._drag && this._drag.moved) {
          const pos = inStage(e).inside ? this.cfg.bulbs[mac].pos : null;    // dropped off the map = back to the tray
          this.cfg.bulbs[mac].pos = pos;
          if (JSON.stringify(pos) !== JSON.stringify(startPos)) {
            await this.act(this.api('PATCH', `/api/bulbs/${mac}`, { pos }));
          }
        }
        setTimeout(() => { this._drag = null; }, 0);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    },

    // ---------- bulbs tab: list, status, firmware, power-on ----------
    filteredBulbs() {
      const q = this.bulbFilter.trim().toLowerCase();
      return this.bulbList().filter((b) => {
        if (q) {
          const hay = [b.name, b.ip || '', b.mac, b.mac.match(/../g).join(':'), b.channel != null ? String(b.channel) : '',
            b.follow || '', ...b.groups].join(' ').toLowerCase();
          if (!hay.includes(q)) return false;
        }
        if (this.bulbGroupFilter === '__none' && b.groups.length) return false;
        if (this.bulbGroupFilter && this.bulbGroupFilter !== '__none' && !b.groups.includes(this.bulbGroupFilter)) return false;
        if (this.bulbStatusFilter === 'offline' && this.isOnline(b.mac)) return false;
        if (this.bulbStatusFilter === 'update' && !this.fwUpdatable(b.mac)) return false;
        if (this.bulbStatusFilter === 'manual' && !['manual', 'look'].includes(this.liveOf(b.mac).source)) return false;
        return true;
      });
    },
    selectShown(on) {
      const shown = this.filteredBulbs().map((b) => b.mac);
      this.selected = on ? [...new Set([...this.selected, ...shown])] : this.selected.filter((m) => !shown.includes(m));
    },
    stateText(mac) {
      const st = this.liveOf(mac).state;
      if (!st) return 'no color yet';
      if (st.v === 0) return 'off';
      return st.k != null ? `${st.k} K, ${st.v}%` : `hue ${st.h}°, sat ${st.s}%, ${st.v}%`;
    },
    fwText(mac) {
      const fw = this.info[mac] && this.info[mac].fw;
      return fw ? 'fw ' + fw.split(' ')[0] : 'fw ?';
    },
    fwTitle(mac) {
      if (!this.fwUpdatable(mac)) return '';
      const inf = this.info[mac];
      const img = this.fwImages.find((i) => i.model === inf.model && i.hw_ver === inf.hw_ver);
      return `Update available: ${img.version.split(' ')[0]} (⋮ menu → Update firmware)`;
    },
    fwUpdatable(mac) {
      const inf = this.info[mac];
      if (!inf || !inf.fw) return false;
      const img = this.fwImages.find((i) => i.model === inf.model && i.hw_ver === inf.hw_ver);
      return !!img && img.version.split(' ')[0] !== inf.fw.split(' ')[0];
    },
    powerText(mac) {
      const inf = this.info[mac];
      if (!inf || inf.power_mw == null) return 'not read yet';
      const age = inf.power_t ? Math.round(Date.now() / 1000 - inf.power_t) : null;
      return `${(inf.power_mw / 1000).toFixed(1)} W, ${inf.lumens ?? '?'} lm` + (age != null && age > 60 ? ` (${Math.round(age / 60)} min ago)` : '');
    },
    totalPower() {
      const w = Object.keys(this.cfg.bulbs).map((m) => this.info[m] && this.info[m].power_mw).filter((x) => x != null);
      return w.length ? (w.reduce((a, b) => a + b, 0) / 1000).toFixed(0) : null;
    },
    powerOnOf(mac) { return this.info[mac] && this.info[mac].power_on; },
    powerOnText(mac) { return this.describePowerOn(this.powerOnOf(mac)); },
    describePowerOn(po) {
      if (!po) return 'not read yet';
      if (po.mode === 'last') return 'last state';
      if (po.v === 0) return 'off';
      return po.s > 0 && !po.k ? `color: hue ${po.h}°, sat ${po.s}%, ${po.v}%` : `white ${po.k} K, ${po.v}%`;
    },
    powerOnSummary() {
      const macs = this.targets(true);
      const vals = macs.map((m) => this.powerOnOf(m));
      if (!vals.length || vals.some((v) => !v)) return { text: vals.some((v) => v) ? 'not read for every bulb yet' : 'not read yet', uniform: null };
      const first = JSON.stringify(vals[0]);
      if (vals.every((v) => JSON.stringify(v) === first)) return { text: this.describePowerOn(vals[0]) + (macs.length > 1 ? ' (all the same)' : ''), uniform: vals[0] };
      return { text: 'mixed', uniform: null };
    },
    syncPowerOn() {
      // When every targeted bulb has the same white power-on default, show it in the controls.
      const u = this.powerOnSummary().uniform;
      if (!u) return;
      if (u.mode === 'last') { this.powerOn.mode = 'last'; return; }
      if (u.s > 0 && !u.k) Object.assign(this.powerOn, { mode: 'color', h: u.h, s: u.s, v: u.v || this.powerOn.v });
      else if (u.k) Object.assign(this.powerOn, { mode: 'white', k: u.k, v: u.v || this.powerOn.v });
    },

    // ---------- bulbs tab: actions ----------
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
      if (r && r.device) {
        if (r.device === 'renamed') this.say('Renamed (saved on the bulb too)');
        else this.say(`Renamed here, but not on the bulb (${r.device}). It will keep its old device name.`, true);
      }
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
    askConfirm({ title, body = '', ok = 'OK', danger = false }) {
      // A modal yes/no; resolves true on confirm, false on cancel/Escape/backdrop.
      if (this.confirmDlg) this.confirmDlg.resolve(false);
      return new Promise((resolve) => { this.confirmDlg = { title, body, ok, danger, resolve }; });
    },
    closeConfirm(answer) {
      const d = this.confirmDlg;
      this.confirmDlg = null;
      if (d) d.resolve(answer);
    },
    async removeBulb(b) {
      return this.removeBulbs([b.mac]);
    },
    async removeBulbs(macs) {
      const names = macs.map((m) => (this.cfg.bulbs[m] || {}).name || m);
      const one = macs.length === 1;
      const yes = await this.askConfirm({
        title: one ? `Remove ${names[0]}?` : `Remove ${macs.length} bulbs?`,
        body: (one ? '' : names.slice(0, 8).join(', ') + (names.length > 8 ? ` and ${names.length - 8} more` : '') + '. ')
          + 'Their channel, groups and stage position are forgotten. The bulbs themselves are not changed; Find bulbs can add them back.',
        ok: one ? 'Remove' : `Remove ${macs.length}`, danger: true,
      });
      if (!yes) return;
      const run = async () => { for (const m of macs) await this.api('DELETE', `/api/bulbs/${m}`); };
      await this.act(run(), one ? `Removed ${names[0]}` : `Removed ${macs.length} bulbs`);
      this.selected = this.selected.filter((m) => !macs.includes(m));
      await this.loadState();
    },
    setModes(macs, mode) {
      const run = async () => { for (const m of macs) await this.api('PATCH', `/api/bulbs/${m}`, { mode }); };
      const label = mode === 'hsic' ? 'HSIC (4 channels)' : 'HSI (3 channels)';
      return this.act(run(), `${macs.length === 1 ? (this.cfg.bulbs[macs[0]] || {}).name : macs.length + ' bulbs'} set to ${label}`)
        .then(() => this.loadState());
    },
    bulbSize(b) { return b.mode === 'hsic' ? 4 : 3; },
    bulbStats(mac) {
      const l = this.liveOf(mac), i = this.info[mac] || {};
      const pct = l.sends ? (100 * Math.min(l.replies, l.sends) / l.sends).toFixed(1) + '%' : '–';
      const out = [
        ['Reply time', l.rtt != null ? l.rtt + ' ms' : '–'],
        ['Replies', l.sends ? `${pct} of ${l.sends}` : '–'],
        ['Missed replies', l.misses ?? '–'],
        ['Updates every', l.interval != null ? l.interval + ' ms' + (l.backoff ? ' (slowed)' : '') : '–'],
        ['WiFi signal', i.rssi != null ? i.rssi + ' dBm' : '–'],
        ['Power', i.power_mw != null ? (i.power_mw / 1000).toFixed(1) + ' W' : '–'],
        ['IP', (this.cfg.bulbs[mac] || {}).ip || '–'],
        ['MAC', mac.match(/../g).join(':')],
        ['Channels', (this.cfg.bulbs[mac] || {}).mode === 'hsic' ? 'HSIC (4)' : 'HSI (3)'],
        ['Firmware', ((i.fw || '?').split(' ')[0]) + (this.fwUpdatable(mac) ? ' ⬆' : ''), this.fwUpdatable(mac) ? 'warn-text' : '', this.fwTitle(mac)],
      ];
      return out;
    },
    delayHist() {
      const h = this.live && this.live.sender.latency_hist;
      if (!h) return { total: 0, rows: [] };
      const e = h.edges_ms, c = h.counts, total = c.reduce((a, b) => a + b, 0), top = Math.max(1, ...c);
      const round = this.live.sender.frame_period_ms || 0;
      const rows = c.map((n, i) => ({
        label: i === 0 ? `<${e[0]}` : i === e.length ? `≥${e[e.length - 1]}` : `${e[i - 1]}–${e[i]}`,
        count: n, pct: (100 * n) / top, slow: (i === e.length ? e[e.length - 1] : e[i]) > Math.max(round, 33) * 1.5,
      }));
      // Trim empty buckets at the high end, keeping at least up to the sync round.
      while (rows.length > 4 && rows[rows.length - 1].count === 0) rows.pop();
      return { total, rows };
    },
    rateSummary() {
      const snd = (this.settings && this.settings.sender) || {};
      const n = Object.keys((this.cfg && this.cfg.bulbs) || {}).length, budget = snd.budget_pps || 1, min = snd.min_interval_ms || 1;
      const round = Math.max(min, (1000 * n) / budget);
      const why = round > min ? 'limited by the budget' : 'limited by the per-bulb interval';
      return `With ${n} bulbs: each bulb gets up to ${(1000 / round).toFixed(1)} commands/sec, `
        + `and a DMX change waits up to ${Math.round(round)} ms to go out (${why}).`;
    },
    openMenu(b, el) {
      if (this.menuFor === b.mac) { this.menuFor = null; return; }
      // Fixed position from the button, flipped up when it would run off the bottom.
      const r = el.getBoundingClientRect(), w = 220, h = 330;
      const x = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
      const y = r.bottom + h + 8 > window.innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4;
      this.menuPos = { x, y };
      this.menuFor = b.mac;
    },
    openAssign() { this.assign = { start: this.nextFree || 1 }; },
    assignPreview() {
      // Mirror the server: skip channels used by bulbs that aren't being reassigned, and by groups.
      const used = new Set();
      const mark = (ch, n) => { if (ch) for (let c = ch; c < ch + n; c++) used.add(c); };
      for (const [mac, b] of Object.entries(this.cfg.bulbs)) {
        if (this.selected.includes(mac)) continue;
        if (b.follow) mark((this.cfg.groups[b.follow] || {}).channel, this.groupSize(b.follow));
        else mark(b.channel, this.bulbSize(b));
      }
      for (const [name, g] of Object.entries(this.cfg.groups)) mark(g.channel, this.groupSize(name));
      let ch = Math.max(1, this.assign ? this.assign.start || 1 : 1);
      return this.bulbList().filter((b) => this.selected.includes(b.mac)).map((b) => {
        const n = this.bulbSize(b), last = 513 - n;
        const busy = (c) => { for (let i = 0; i < n; i++) if (used.has(c + i)) return true; return false; };
        while (ch <= last && busy(ch)) ch++;
        const row = { mac: b.mac, name: b.name, ch: ch <= last ? ch : null, size: n };
        if (row.ch) { mark(ch, n); ch += n; }
        return row;
      });
    },
    async autoAssign() {
      const start = this.assign ? this.assign.start || 1 : this.nextFree || 1;
      const order = this.bulbList().filter((b) => this.selected.includes(b.mac)).map((b) => b.mac);
      const r = await this.act(this.api('POST', '/api/bulbs/auto-assign', { macs: order, start }), `Channels assigned from ${start}`);
      if (r) this.assign = null;
      await this.loadState();
    },
    async identify(mac) { await this.act(this.api('POST', `/api/bulbs/${mac}/identify`), 'Blinking…'); },
    bulkGroupCount(g) { return this.selected.filter((m) => this.cfg.bulbs[m] && this.cfg.bulbs[m].groups.includes(g)).length; },
    bulkGroupState(g) {
      const n = this.bulkGroupCount(g);
      return n === 0 ? 'none' : n === this.selected.length ? 'all' : 'some';
    },
    async setBulkGroup(g, on) {
      const todo = this.selected.filter((m) => this.cfg.bulbs[m] && on !== this.cfg.bulbs[m].groups.includes(g));
      // One at a time: each edit is a read-modify-write of the config.
      const run = async () => {
        for (const m of todo) {
          const cur = this.cfg.bulbs[m].groups;
          await this.api('PATCH', `/api/bulbs/${m}`, { groups: on ? [...cur, g] : cur.filter((x) => x !== g) });
        }
      };
      await this.act(run(), `${on ? 'Added' : 'Removed'} ${todo.length} bulb${todo.length === 1 ? '' : 's'} ${on ? 'to' : 'from'} ${g}`);
      await this.loadState();
    },
    async addBulkGroup() {
      const name = this.newGroup.trim();
      if (!name) return;
      if (!this.cfg.groups[name]) await this.act(this.api('PUT', `/api/groups/${encodeURIComponent(name)}`, { channel: null }));
      this.newGroup = '';
      await this.loadState();
      await this.setBulkGroup(name, true);
    },
    async addGroup(assignTo) {
      const name = this.newGroup.trim();
      if (!name) return;
      await this.act(this.api('PUT', `/api/groups/${encodeURIComponent(name)}`, { channel: null }));
      this.newGroup = '';
      if (assignTo && this.cfg.bulbs[assignTo]) {
        await this.act(this.api('PATCH', `/api/bulbs/${assignTo}`, { groups: [...this.cfg.bulbs[assignTo].groups, name] }));
      }
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
      const p = this.powerOn;
      const state = p.mode === 'last' ? { mode: 'last' } : p.mode === 'color' ? { h: p.h, s: p.s, v: p.v } : { k: p.k, v: p.v };
      const r = await this.act(this.api('POST', '/api/power-on', { macs, state }));
      if (r) {
        const bad = Object.entries(r.results).filter(([, ok]) => !ok).length;
        this.say(bad ? `${bad} bulb(s) didn't confirm` : `Power-on default set on ${macs.length} bulb(s)`, !!bad);
        await this.loadState();               // the Pi read the new defaults back
      }
    },
    fwJob(mac) { return this.live && this.live.firmware && this.live.firmware[mac]; },
    async updateFirmware(mac) {
      const b = this.cfg.bulbs[mac] || {}, inf = this.info[mac] || {};
      const img = this.fwImages.find((i) => i.model === inf.model && i.hw_ver === inf.hw_ver);
      const yes = await this.askConfirm({
        title: `Update ${b.name || mac}?`,
        body: `Firmware ${(inf.fw || '?').split(' ')[0]} → ${img ? img.version.split(' ')[0] : 'latest'}. `
          + 'The bulb downloads the update from this Pi, restarts and is dark for about a minute. Don\'t switch its power off meanwhile.',
        ok: 'Update firmware',
      });
      if (!yes) return;
      await this.act(this.api('POST', `/api/bulbs/${mac}/firmware`, {}), 'Updating firmware…');
    },

    // ---------- control ----------
    targets(forPowerOn = false) {
      if (this.sheet && !forPowerOn) return [this.sheet];
      if (this.groupSheet && !forPowerOn) return this.groupMembers(this.groupSheet);
      return this.selected.length ? [...this.selected] : Object.keys(this.cfg.bulbs);
    },
    targetLabel() {
      const t = this.targets();
      if (t.length === Object.keys(this.cfg.bulbs).length) return `all ${t.length} bulbs`;
      if (t.length === 1) return this.cfg.bulbs[t[0]].name;
      return `${t.length} bulbs`;
    },
    queueSend(state) {
      const macs = this.targets();
      this.boardThrottle('control', () => this.act(this.api('POST', '/api/control', { macs, state })));
    },
    previewPowerOn() {
      // Show the power-on look on the bulbs while the sliders move (like a manual set).
      if (this.powerOn.mode === 'last') return;
      const p = this.powerOn;
      const state = p.mode === 'white' ? { k: p.k, v: p.v } : { h: p.h, s: p.s, v: p.v };
      const macs = this.targets(true);
      this.boardThrottle('poweron', () => this.act(this.api('POST', '/api/control', { macs, state })));
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
    drawWheel(c) {
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
    wheelAt(ev) {
      const rect = ev.currentTarget.getBoundingClientRect();
      const dx = ev.clientX - rect.left - rect.width / 2, dy = ev.clientY - rect.top - rect.height / 2;
      return { h: Math.round((Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360),
        s: Math.round(Math.min(1, Math.sqrt(dx * dx + dy * dy) / (rect.width / 2)) * 100) };
    },
    wheelPick(ev) {
      const rect = ev.currentTarget.getBoundingClientRect();
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

    // ---------- control board ----------
    async loadBoard() {
      const b = await this.act(this.api('GET', '/api/board'));
      if (b) {
        this.board = b;
        if (!this.boardSel.length) this.boardSel = b.fixtures.map((f) => f.channel);
      }
    },
    toggleBoardSel(ch) {
      const i = this.boardSel.indexOf(ch);
      if (i >= 0) this.boardSel.splice(i, 1); else this.boardSel.push(ch);
    },
    fixtureBulbs(ch) {
      // Bulbs listening to the address that starts at ch: solo bulbs on it, and
      // followers of a group whose shared channel it is.
      return Object.entries(this.cfg.bulbs).filter(([, b]) => b.follow
        ? (this.cfg.groups[b.follow] || {}).channel === ch : b.channel === ch).map(([mac]) => mac);
    },
    fixtureSwatch(f) {
      const mac = this.fixtureBulbs(f.channel).find((m) => this.liveOf(m).state) || null;
      return mac ? this.lampStyle(mac, true) : this.boardSwatch(f);
    },
    boardSwatch(f) {
      const h = Math.round(f.h / 255 * 360), sat = Math.round(f.s / 255 * 100), v = Math.round(f.v / 255 * 100);
      // HSIC at saturation 0 is white at the C channel's temperature (2500-6500 K), as the engine does it.
      if (f.size === 4 && f.c != null && sat === 0) return this.cssFromState({ k: 2500 + (f.c / 255) * 4000, v }, true);
      return this.cssFromState({ h, s: sat, v }, true);
    },
    satTrack(h) {
      const [r, g, b] = hsvToRgb(h, 100, 100);
      return `background: linear-gradient(to right, #fff, rgb(${r},${g},${b}))`;
    },
    boardThrottle(key, fn) {
      // Send while dragging, not only when the drag pauses: one request in flight per
      // key, the newest value wins, and the next goes as soon as the last one is back.
      const t = this._boardTimers[key] || (this._boardTimers[key] = { busy: false, next: null });
      if (t.busy) { t.next = fn; return; }
      t.busy = true;
      Promise.resolve(fn()).finally(() => {
        t.busy = false;
        const next = t.next; t.next = null;
        if (next) this.boardThrottle(key, next);
      });
    },
    boardFader(ch, value) {
      this.boardThrottle('ch' + ch, async () => {
        await this.act(this.api('POST', '/api/board/channels', { values: { [ch]: value } }));
        if (this.board) this.board.active = true;
      });
    },
    boardWheelPick(ev) {
      const p = this.wheelAt(ev);
      this.boardColor.h = p.h; this.boardColor.s = p.s;
      if (this.boardColor.v === 0) this.boardColor.v = 70;
      this.boardSendColor();
    },
    boardSendColor() {
      if (!this.boardSel.length) { this.say('Pick at least one fixture', true); return; }
      const body = { channels: this.boardSel, ...this.boardColor };
      this.boardThrottle('color', async () => {
        await this.act(this.api('POST', '/api/board/color', body));
        if (this.board) this.board.active = true;
      });
    },
    boardHsicSelected() {
      return this.board ? this.board.fixtures.filter((f) => f.size === 4 && this.boardSel.includes(f.channel)).length : 0;
    },
    boardSendCct() {
      const body = { channels: this.boardSel, k: this.boardTemp };
      this.boardThrottle('cct', async () => {
        await this.act(this.api('POST', '/api/board/cct', body));
        if (this.board) this.board.active = true;
      });
    },
    boardRunOn(target) { return this.board ? this.board.runs.find((r) => r.target === target) : null; },
    async boardShow(name) {
      await this.act(this.api('POST', '/api/board/show', { name, speed: this.boardSpeed, target: this.boardShowTarget }));
      await this.loadBoard();
    },
    async boardStopShow(id) {
      await this.act(this.api('POST', '/api/board/stop-show', id ? { id } : {}));
      await this.loadBoard();
    },
    async boardBlackout() { await this.act(this.api('POST', '/api/board/blackout'), 'Blackout'); await this.loadBoard(); },
    async boardRelease() { await this.act(this.api('POST', '/api/board/release'), 'Released: no longer sending DMX'); await this.loadBoard(); },

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

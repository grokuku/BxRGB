#!/usr/bin/env node
/**
 * Harnais de validation headless BxRGB (vagues 1-3).
 *
 * Sert BxRGB/static + mocke l'API /api/* + injecte les scripts de test
 * (?test=1|save|save2|kraken|themes) et d'aide (?tab=…, ?pack=…).
 *
 * Usage (depuis n'importe où) :
 *   node tests/front/server.js [port]        (défaut 8811)
 *
 * La racine servie est déduite de l'emplacement du fichier
 * (tests/front/*.js → repo BxRGB) ; surchargeable via BXRGB_DIR.
 *
 * Endpoints de contrôle :
 *   GET /__stats   → { apiCalls, thumbRequests, previewGenerations, themesParam }
 *
 * Pièges connus (carnet) : le serveur GARDE son état entre deux runs
 * chromium (comme un F5) → démarrer un serveur neuf par scénario.
 */
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.argv[2] || '8811', 10);
// Racine du repo BxRGB déduite de tests/front/ (surcharge : BXRGB_DIR).
const BXRGB = process.env.BXRGB_DIR || path.resolve(__dirname, '..', '..');
const STATIC_DIR = path.join(BXRGB, 'static');
const FIXTURES = path.join(__dirname, 'fixtures');

const PNG_1x1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');

const REAL_THEMES = [
  { key: 'data_center', label: 'Data Center', subtitle: 'Bleu Technique', default: true },
  { key: 'overclock', label: 'Overclock', subtitle: 'Rouge Agressif', default: false },
  { key: 'fluid_flow', label: 'Fluid Flow', subtitle: 'Bleu Pastel', default: false },
];
const FAKE_THEMES = Array.from({ length: 9 }, (_, i) => ({
  key: `test_${String(i + 1).padStart(2, '0')}`,
  label: `Test ${String(i + 1).padStart(2, '0')}`,
  subtitle: `Variante ${String(i + 1).padStart(2, '0')}`,
  default: false,
}));

function freshState() {
  const makeColors = (base) =>
    Array.from({ length: 8 }, (_, i) => [(base[0] + i * 7) & 255, base[1], (base[2] + i * 3) & 255]);
  const sticks = [
    { id: 'stick_0', bus_num: 0, address: 0x30, num_leds: 8,
      brightness: 255, version: 'A1', colors: makeColors([10, 20, 30]) },
    { id: 'stick_1', bus_num: 0, address: 0x31, num_leds: 8,
      brightness: 200, version: 'A1', colors: makeColors([200, 10, 40]) },
  ];
  return {
    sticks,
    saved: savedPayload(sticks),
    animation: { running: false, effect: null, speed: 1.0, framerate: 30, refresh: 20 },
    lcd: { brightness: 80, orientation: 0, mode: 'liquid' },
    display: { running: false, mode: null, theme: 'data_center',
               options: ['cpu', 'gpu', 'ram', 'vram', 'disks', 'liquid'],
               interval: 10.0 },
    galleryFiles: [],
    themesParam: '3',
    apiCalls: [],
    thumbRequests: [],
    previewGenerations: 0,
  };
}

function savedPayload(sticks) {
  const colors = {};
  sticks.forEach((s) => { colors[s.id] = s.colors.map((c) => c.slice()); });
  return {
    colors,
    brightness: sticks[0] ? sticks[0].brightness : 255,
    stick_order: sticks.map((s) => `${s.bus_num}:0x${s.address.toString(16)}`),
    animation: { speed: 1.0, framerate: 30, refresh: 20, enabled: false, type: 'static' },
    kraken: {
      lcd: { brightness: 80, orientation: 0, mode: 'liquid' },
      display: { mode: null, theme: 'data_center',
                 options: ['cpu', 'gpu', 'ram', 'vram', 'disks', 'liquid'],
                 interval: 10.0 },
    },
  };
}

let S = freshState();

function themesForRequest() {
  const many = S.themesParam === 'many' || parseInt(S.themesParam, 10) > 3;
  return many ? REAL_THEMES.concat(FAKE_THEMES) : REAL_THEMES.slice();
}

function publicStick(s) {
  return {
    id: s.id,
    label: `i2c-${s.bus_num} @ 0x${s.address.toString(16).padStart(2, '0')}`,
    bus_num: s.bus_num,
    address: '0x' + s.address.toString(16),
    version: s.version,
    num_leds: s.num_leds,
    brightness: s.brightness,
  };
}

function json(res, code, obj, headers) {
  const body = Buffer.from(JSON.stringify(obj));
  res.writeHead(code, Object.assign({
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': body.length,
  }, headers || {}));
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (_) { resolve({}); }
    });
  });
}

function logCall(req, urlPath, body) {
  S.apiCalls.push({ method: req.method, path: urlPath, body: body || null, at: Date.now() });
}

/* ── API mockée ─────────────────────────────────────────────── */

async function handleApi(req, res, u) {
  const p = u.pathname;
  const body = (req.method === 'POST' || req.method === 'PUT') ? await readBody(req) : {};
  logCall(req, p, body);
  const m = (re) => p.match(re);

  if (p === '/api/ws-status') return json(res, 200, { websocket: false });

  if (p === '/api/status') return json(res, 200, { sticks: S.sticks.map(publicStick) });

  if (p === '/api/config') {
    const colors = {};
    S.sticks.forEach((s) => { colors[s.id] = s.colors; });
    return json(res, 200, { sticks: S.sticks.map(publicStick), colors });
  }
  if (p === '/api/config' && req.method === 'PUT') return json(res, 200, { status: 'ok' });

  let mm = m(/^\/api\/sticks\/([^/]+)$/);
  if (mm) {
    const s = S.sticks.find((x) => x.id === mm[1]);
    if (!s) return json(res, 404, { detail: 'stick inconnu' });
    return json(res, 200, {
      stick_id: s.id, num_leds: s.num_leds, colors: s.colors,
      brightness: s.brightness, version: s.version, label: publicStick(s).label,
    });
  }
  mm = m(/^\/api\/sticks\/([^/]+)\/colors$/);
  if (mm && req.method === 'PUT') {
    const s = S.sticks.find((x) => x.id === mm[1]);
    if (!s) return json(res, 404, { detail: 'stick inconnu' });
    s.colors = (body.leds || []).map((c) => c.slice());
    return json(res, 200, { status: 'ok', stick_id: s.id });
  }
  mm = m(/^\/api\/sticks\/([^/]+)\/brightness$/);
  if (mm && req.method === 'PUT') {
    const s = S.sticks.find((x) => x.id === mm[1]);
    if (!s) return json(res, 404, { detail: 'stick inconnu' });
    s.brightness = body.level;
    return json(res, 200, { status: 'ok', stick_id: s.id, level: s.brightness });
  }

  if (p === '/api/apply' && req.method === 'POST') return json(res, 200, { status: 'ok' });
  if (p === '/api/rescan' && req.method === 'POST') {
    return json(res, 200, { status: 'ok', sticks: S.sticks.map(publicStick) });
  }

  if (p === '/api/saved') return json(res, 200, S.saved);
  if (p === '/api/save' && req.method === 'POST') {
    // Fige l'état courant + réglages d'affichage/écran.
    const colors = {};
    S.sticks.forEach((s) => { colors[s.id] = s.colors.map((c) => c.slice()); });
    const order = Array.isArray(body.stick_order) && body.stick_order.length
      ? body.stick_order
      : S.sticks.map((s) => `${s.bus_num}:0x${s.address.toString(16)}`);
    S.saved = {
      colors, brightness: S.sticks[0] ? S.sticks[0].brightness : 255,
      stick_order: order,
      animation: Object.assign({}, S.animation),
      kraken: {
        lcd: Object.assign({}, S.lcd),
        display: Object.assign({}, S.display),
      },
    };
    S.saved.animation.enabled = false;
    return json(res, 200, { status: 'ok', reference: S.saved });
  }
  if (p === '/api/restore' && req.method === 'POST') {
    // Ré-applique la référence (best-effort : couleurs + luminosité + réglages).
    S.sticks.forEach((s) => {
      if (S.saved.colors[s.id]) s.colors = S.saved.colors[s.id].map((c) => c.slice());
      s.brightness = S.saved.brightness;
    });
    Object.assign(S.animation, S.saved.animation);
    Object.assign(S.lcd, S.saved.kraken.lcd);
    Object.assign(S.display, S.saved.kraken.display);
    return json(res, 200, { status: 'ok', reference: S.saved });
  }

  if (p === '/api/animation/status') return json(res, 200, S.animation);
  if (p === '/api/animation/start' && req.method === 'POST') {
    S.animation.running = true;
    S.animation.effect = body.effect || 'incandescence';
    return json(res, 200, { status: 'ok', effect: S.animation.effect });
  }
  if (p === '/api/animation/stop' && req.method === 'POST') {
    S.animation.running = false;
    return json(res, 200, { status: 'ok' });
  }
  if (p === '/api/animation/speed' && req.method === 'POST') {
    S.animation.speed = body.speed;
    return json(res, 200, { status: 'ok', speed: S.animation.speed });
  }
  if (p === '/api/animation/refresh' && req.method === 'POST') {
    S.animation.refresh = body.rate;
    return json(res, 200, { status: 'ok', rate: S.animation.refresh });
  }

  if (p === '/api/update/check') {
    return json(res, 200, { current: '0.0.1', latest: '0.0.1', update_available: false });
  }

  if (p === '/api/kraken/status') {
    return json(res, 200, {
      available: true, detected: true, devices: ['NZXT Kraken Z53'],
      status: { liquid_temperature: 32.4, pump_speed: 2100, fan_speed: 1200 },
      status_raw: '', error: null,
    });
  }
  if (p === '/api/kraken/initialize' && req.method === 'POST') return json(res, 200, { ok: true });
  if (p === '/api/kraken/lcd/settings') return json(res, 200, S.lcd);
  if (p === '/api/kraken/lcd/brightness' && req.method === 'POST') {
    S.lcd.brightness = body.value;
    return json(res, 200, { ok: true, message: 'ok', error: null });
  }
  if (p === '/api/kraken/lcd/orientation' && req.method === 'POST') {
    S.lcd.orientation = body.value;
    return json(res, 200, { ok: true, message: 'ok', error: null });
  }
  if (p === '/api/kraken/lcd/mode' && req.method === 'POST') {
    S.lcd.mode = body.mode;
    return json(res, 200, { ok: true, message: 'ok', error: null });
  }
  if (p === '/api/kraken/lcd/image' && req.method === 'POST') {
    return json(res, 200, { ok: true, message: 'ok', error: null, path: '/tmp/screen.png' });
  }

  if (p === '/api/kraken/display/status') {
    return json(res, 200, {
      running: S.display.running,
      mode: S.display.mode,
      theme: S.display.theme,
      options: S.display.options,
      interval: S.display.interval,
    });
  }
  if (p === '/api/kraken/display/update' && req.method === 'POST') {
    if (body.interval !== undefined) S.display.interval = body.interval;
    if (body.theme !== undefined) S.display.theme = body.theme;
    if (body.options !== undefined) S.display.options = body.options;
    return json(res, 200, Object.assign({ ok: true, restarted: false }, S.display));
  }
  if (p === '/api/kraken/display/stop' && req.method === 'POST') {
    S.display.running = false;
    S.display.mode = null;
    return json(res, 200, { ok: true });
  }
  if (p === '/api/kraken/monitor/start' && req.method === 'POST') {
    S.display.running = true;
    S.display.mode = 'monitor';
    if (body.theme !== undefined) S.display.theme = body.theme;
    if (body.options !== undefined) S.display.options = body.options;
    if (body.interval !== undefined) S.display.interval = body.interval;
    return json(res, 200, { ok: true });
  }
  if (p === '/api/kraken/gallery/start' && req.method === 'POST') {
    S.display.running = true;
    S.display.mode = 'gallery';
    if (body.interval !== undefined) S.display.interval = body.interval;
    return json(res, 200, { ok: true });
  }
  if (p === '/api/kraken/gallery' && req.method === 'GET') {
    return json(res, 200, { ok: true, files: S.galleryFiles, error: null });
  }
  if (p === '/api/kraken/gallery/add' && req.method === 'POST') {
    S.galleryFiles.push({ name: body.filename || 'x.png', size: 10, is_gif: false });
    return json(res, 200, { ok: true, name: body.filename });
  }
  if (p === '/api/kraken/gallery/delete' && req.method === 'POST') {
    return json(res, 200, { ok: true });
  }
  if (p === '/api/kraken/pending') {
    return json(res, 200, { ok: true, screens: [], gallery_added: [],
                            gallery_deleted: [], count: 0, error: null });
  }
  if (p === '/api/kraken/monitor/preview' && req.method === 'POST') {
    S.previewGenerations += 1;
    return json(res, 200, { ok: true, path: '/tmp/monitor_preview.png', error: null });
  }
  if (p === '/api/kraken/monitor/preview.png') {
    return sendFixture(res, 'preview.png', 'image/png', 640, 640);
  }

  if (p === '/api/kraken/themes' && req.method === 'GET') {
    const themes = themesForRequest();
    return json(res, 200, { ok: true, themes, count: themes.length, error: null });
  }
  mm = m(/^\/api\/kraken\/themes\/([^/]+)\/thumb\.png$/);
  if (mm) {
    const key = decodeURIComponent(mm[1]);
    const known = themesForRequest().some((t) => t.key === key);
    if (!known) return json(res, 404, { detail: `Thème inconnu : ${key}` });
    S.thumbRequests.push({ key, at: Date.now() });
    return sendFixture(res, `${key}.png`, 'image/png', 150, 150);
  }

  return json(res, 404, { detail: `route mock inconnue : ${req.method} ${p}` });
}

function sendFixture(res, name, type, w, h) {
  const file = path.join(FIXTURES, name);
  const data = fs.existsSync(file) ? fs.readFileSync(file) : PNG_1x1;
  res.writeHead(200, {
    'Content-Type': type,
    'Content-Length': data.length,
    'Cache-Control': 'public, max-age=3600',
  });
  res.end(data);
}

/* ── Statique + injection des scripts ───────────────────────── */

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

function serveStatic(req, res, u) {
  let rel = u.pathname === '/' ? '/index.html' : u.pathname;
  let file = path.join(STATIC_DIR, rel);
  if (!file.startsWith(STATIC_DIR)) return json(res, 403, { detail: 'hors static' });
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    return json(res, 404, { detail: 'fichier introuvable' });
  }
  const ext = path.extname(file);
  if (ext === '.html') {
    // Mémorise le paramètre ?themes= pour /api/kraken/themes.
    const themesParam = u.searchParams.get('themes');
    if (themesParam) S.themesParam = themesParam;
    let html = fs.readFileSync(file, 'utf8');
    html = html.replace('</head>',
      '<script src="/__test.js"></script></head>');
    html = html.replace('</body>',
      '<script src="/__helper.js"></script></body>');
    const body = Buffer.from(html);
    res.writeHead(200, { 'Content-Type': MIME['.html'], 'Content-Length': body.length });
    return res.end(body);
  }
  const data = fs.readFileSync(file);
  res.writeHead(200, {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Content-Length': data.length,
    'Cache-Control': 'no-store',
  });
  res.end(data);
}

const helperJs = `
/* Helper harnais : applique ?tab= et ?pack= une fois l'app prête. */
(function () {
  if (!location.search) return;
  const params = new URLSearchParams(location.search);
  const tab = params.get('tab');
  const pack = params.get('pack');
  if (!tab && !pack) return;
  async function ready() {
    for (let i = 0; i < 200 && !window.__appReady; i++) {
      await new Promise((r) => setTimeout(r, 50));
    }
    if (pack && window.BxRGBPacks) window.BxRGBPacks.apply(pack);
    if (tab) {
      const btn = document.querySelector('.tab-btn[data-tab="' + tab + '"]');
      if (btn) btn.click();
    }
  }
  window.addEventListener('load', ready);
})();
`;

/* ── Serveur ────────────────────────────────────────────────── */

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/__stats') {
    return json(res, 200, {
      apiCalls: S.apiCalls,
      thumbRequests: S.thumbRequests,
      previewGenerations: S.previewGenerations,
      themesParam: S.themesParam,
    });
  }
  if (u.pathname === '/__test.js') {
    const data = fs.readFileSync(path.join(__dirname, 'test.js'));
    res.writeHead(200, { 'Content-Type': MIME['.js'], 'Content-Length': data.length });
    return res.end(data);
  }
  if (u.pathname === '/__helper.js') {
    const data = Buffer.from(helperJs);
    res.writeHead(200, { 'Content-Type': MIME['.js'], 'Content-Length': data.length });
    return res.end(data);
  }
  if (u.pathname.startsWith('/api/')) {
    handleApi(req, res, u).catch((err) => json(res, 500, { detail: String(err) }));
    return;
  }
  serveStatic(req, res, u);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`harnais BxRGB prêt : http://127.0.0.1:${PORT} (static=${STATIC_DIR})`);
});

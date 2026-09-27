/**
 * Assertions du harnais BxRGB (?test=1|save|save2|kraken|kraken-empty|themes).
 * Injecté en <head> par server.js AVANT app.js pour :
 *   - collecter les erreurs JS dès le premier script ;
 *   - capter le log « Ballistix RGB Controller prêt » (window.__appReady).
 * Le résultat final est écrit dans <pre id="__result"> (dump-dom).
 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  const mode = params.get('test');
  window.__bxErrors = [];
  window.addEventListener('error', (e) =>
    window.__bxErrors.push('error: ' + (e.message || e.type || e)));
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    window.__bxErrors.push('rejection: ' + (r && r.message ? r.message : String(r)));
  });

  const origLog = console.log.bind(console);
  console.log = function (...args) {
    try {
      if (String(args[0]).indexOf('Ballistix RGB Controller prêt') !== -1) {
        window.__appReady = true;
      }
    } catch (_) { /* jamais bloquant */ }
    return origLog(...args);
  };

  if (!mode) return;

  const results = [];
  function check(name, cond, extra) {
    results.push({
      name: name,
      pass: !!cond,
      extra: extra === undefined ? undefined : String(extra),
    });
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, timeout = 8000, step = 40) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      try { if (fn()) return true; } catch (_) { /* retente */ }
      await sleep(step);
    }
    return false;
  }
  async function stats() {
    const r = await fetch('/__stats');
    return r.json();
  }
  function calls(state, path) {
    return state.apiCalls.filter((c) => c.path === path);
  }

  function finish() {
    const pass = results.filter((r) => r.pass).length;
    const payload = {
      mode: mode,
      pass: pass,
      total: results.length,
      failures: results.filter((r) => !r.pass),
      jsErrors: window.__bxErrors,
    };
    const pre = document.createElement('pre');
    pre.id = '__result';
    pre.textContent = JSON.stringify(payload, null, 2);
    document.body.appendChild(pre);
    origLog('RESULT ' + mode + ' ' + pass + '/' + results.length);
  }

  /* ═══════════════════ ?test=1 — socle ═══════════════════ */
  async function suiteBase() {
    check('app prête', window.__appReady === true);
    check('2 barrettes rendues', document.querySelectorAll('.dnd-stick').length === 2);
    check('4 slots DnD', document.querySelectorAll('.dnd-slot').length === 4);
    check('connexion REST (mock sans WS)',
      document.getElementById('connection-mode').textContent.indexOf('REST') !== -1);
    check('statut connecté affiché',
      document.getElementById('status-text').textContent.trim().length > 0);
    check('palette rapide peuplée',
      document.querySelectorAll('#quick-colors button, #quick-colors .quick-color-btn').length > 0);
    check('onglets = 2', document.querySelectorAll('.tab-btn').length === 2);

    const st = await stats();
    check('GET /status appelé', calls(st, '/api/status').length >= 1);
    check('GET /saved appelé', calls(st, '/api/saved').length >= 1);
    check('GET /kraken/status appelé', calls(st, '/api/kraken/status').length >= 1);
    check('GET /ws-status appelé', calls(st, '/api/ws-status').length >= 1);

    // Onglet Kraken
    document.querySelector('.tab-btn[data-tab="kraken"]').click();
    await sleep(120);
    check('onglet Kraken actif', document.getElementById('tab-kraken').classList.contains('active'));
    await waitFor(() => document.getElementById('kraken-badge').textContent.indexOf('Détecté') !== -1, 3000);
    check('Kraken détecté', document.getElementById('kraken-badge').textContent.indexOf('Détecté') !== -1);
    check('contrôles Kraken actifs', document.getElementById('kraken-btn-monitor').disabled === false);
    check('température liquide affichée',
      document.getElementById('kraken-status').textContent.indexOf('32.4') !== -1);

    // Thèmes UI (packs) — noms complets « bxrgb-* » attendus par apply().
    check('BxRGBPacks expose 4 packs',
      !!(window.BxRGBPacks && window.BxRGBPacks.list && window.BxRGBPacks.list().length === 4));
    window.BxRGBPacks.apply('bxrgb-clair');
    await sleep(80);
    check('pack clair appliqué',
      document.documentElement.getAttribute('data-pack') === 'clair',
      document.documentElement.getAttribute('data-pack'));
    window.BxRGBPacks.apply('bxrgb-neon');
    await sleep(80);
    check('retour pack néon',
      document.documentElement.getAttribute('data-pack') === 'neon');
  }

  /* ═══════════════ ?test=save — modèle Save/Cancel ═══════════════ */
  async function suiteSave() {
    await waitFor(() =>
      document.querySelectorAll('#layout-gallery .layout-thumb').length >= 3, 5000);

    check('repos : Enregistrer désactivé', document.getElementById('btn-save').disabled === true);
    check('repos : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);

    // Sélectionne la barrette #1 puis change la luminosité (temps réel).
    document.querySelectorAll('.dnd-stick')[0].click();
    await sleep(60);
    check('barrette sélectionnée', document.querySelectorAll('.dnd-stick.selected').length === 1);
    const slider = document.getElementById('brightness-slider');
    slider.value = '111';
    slider.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => document.getElementById('btn-save').disabled === false, 2500);
    check('modification → Enregistrer actif', document.getElementById('btn-save').disabled === false);
    check('modification → badge dirty visible', document.getElementById('dirty-badge').hidden === false);
    check('PUT luminosité immédiat (temps réel)',
      calls(await stats(), '/api/sticks/stick_0/brightness').length >= 1);

    // Palette LCD : temps réel débouncé.
    document.querySelector('#palette-gallery .palette-card[data-palette="overclock"]').click();
    await sleep(700);
    const updates = calls(await stats(), '/api/kraken/display/update');
    const lastUpdate = updates[updates.length - 1];
    check('palette appliquée en temps réel (display/update)',
      !!lastUpdate && lastUpdate.body && lastUpdate.body.palette === 'overclock',
      lastUpdate && lastUpdate.body && lastUpdate.body.palette);

    // Save fige la référence.
    document.getElementById('btn-save').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 5000);
    check('Enregistrer → POST /api/save', calls(await stats(), '/api/save').length >= 1);
    check('après save : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);

    // Nouvelle modif → Cancel restaure la référence.
    slider.value = '33';
    slider.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => document.getElementById('btn-save').disabled === false, 2500);
    check('nouvelle modif → dirty', document.getElementById('btn-save').disabled === false);
    document.getElementById('btn-cancel').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 6000);
    check('Annuler → POST /api/restore', calls(await stats(), '/api/restore').length >= 1);
    await waitFor(() => slider.value === '111', 4000);
    check('luminosité restaurée (111)', slider.value === '111', slider.value);
    check('palette restaurée (overclock)',
      document.getElementById('palette-key').textContent === 'overclock',
      document.getElementById('palette-key').textContent);
    check('après cancel : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);
  }

  /* ═══════════ ?test=save2 — F5 : hydratation depuis la référence ═══════════ */
  async function suiteSave2() {
    await waitFor(() =>
      document.querySelectorAll('#layout-gallery .layout-thumb').length >= 3, 5000);
    check('palette hydratée depuis la référence (overclock)',
      document.getElementById('palette-key').textContent === 'overclock',
      document.getElementById('palette-key').textContent);
    const selected = document.querySelector('#palette-gallery .palette-card.selected');
    check('carte palette selected = overclock',
      !!selected && selected.dataset.palette === 'overclock');
    check('disposition hydratée (duo)',
      document.getElementById('layout-key').textContent === 'duo',
      document.getElementById('layout-key').textContent);
    check('luminosité hydratée (111)',
      document.getElementById('brightness-slider').value === '111',
      document.getElementById('brightness-slider').value);
    check('repos : Enregistrer désactivé', document.getElementById('btn-save').disabled === true);
    check('repos : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);
    check('capteur CPU coché', document.getElementById('kraken-cpu').checked === true);
    check('capteur liquid coché', document.getElementById('kraken-liquid-temp').checked === true);
    check('intervalle hydraté (10 s)', document.getElementById('kraken-interval').value === '10');
  }

  /* ═══════════ ?test=kraken-empty — statut illisible : diagnostic visible ═══════════ */
  async function suiteKrakenEmpty() {
    document.querySelector('.tab-btn[data-tab="kraken"]').click();
    await waitFor(() =>
      document.getElementById('kraken-badge').textContent.indexOf('Détecté') !== -1, 3000);
    const el = document.getElementById('kraken-status');
    check('badge Détecté (device vu)',
      document.getElementById('kraken-badge').textContent.indexOf('Détecté') !== -1);
    const values = Array.prototype.slice.call(el.querySelectorAll('.kraken-stat-value'));
    check('les 3 valeurs restent en tiret',
      values.length === 3 && values.every((v) => v.textContent.trim() === '—'),
      values.map((v) => v.textContent.trim()).join('|'));
    check('bandeau « statut illisible » affiché',
      el.querySelector('.kraken-status-error') !== null);
    check('cause remontée par le serveur affichée',
      el.textContent.indexOf('Sortie liquidctl non reconnue') !== -1);
    check('commande de diagnostic affichée',
      el.textContent.indexOf('liquidctl --match Kraken status') !== -1);
    const raw = el.querySelector('details.kraken-raw pre');
    check('sortie brute consultable',
      raw !== null && raw.textContent.indexOf('Liquid temperature') !== -1);
    check('contrôles encore actifs (device détecté)',
      document.getElementById('kraken-btn-monitor').disabled === false);
  }
  /* ═══════════ ?test=kraken — temps réel vague 2 ═══════════ */
  async function suiteKraken() {
    document.querySelector('.tab-btn[data-tab="kraken"]').click();
    await waitFor(() =>
      document.querySelectorAll('#layout-gallery .layout-thumb').length >= 3, 5000);
    await waitFor(() =>
      document.getElementById('kraken-badge').textContent.indexOf('Détecté') !== -1, 3000);
    check('statut complet : aucun avertissement affiché',
      document.querySelectorAll('#kraken-status .kraken-status-error').length === 0);
    check('statut complet : pas de note de valeur manquante',
      document.querySelectorAll('#kraken-status .kraken-status-note').length === 0);

    // Capteurs : décocher GPU + liquid → display/update débouncé 400 ms.
    document.getElementById('kraken-gpu').click();
    document.getElementById('kraken-liquid-temp').click();
    await sleep(750);
    let updates = calls(await stats(), '/api/kraken/display/update');
    let last = updates[updates.length - 1];
    check('décocher capteurs → display/update', updates.length >= 1);
    check('options transmises sans gpu/liquid',
      !!last && last.body && Array.isArray(last.body.options) &&
      last.body.options.indexOf('gpu') === -1 &&
      last.body.options.indexOf('liquid') === -1,
      last && last.body && JSON.stringify(last.body.options));

    // Intervalle 2 s.
    const iv = document.getElementById('kraken-interval');
    iv.value = '2';
    iv.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(750);
    updates = calls(await stats(), '/api/kraken/display/update');
    last = updates[updates.length - 1];
    check('intervalle 2 s transmis', !!last && Number(last.body.interval) === 2,
      last && last.body && last.body.interval);

    // Option « asap ».
    const asap = document.getElementById('kraken-interval-asap');
    asap.checked = true;
    asap.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(750);
    updates = calls(await stats(), '/api/kraken/display/update');
    last = updates[updates.length - 1];
    check('« asap » transmis', !!last && last.body.interval === 'asap',
      last && last.body && last.body.interval);
    check('slider désactivé en asap', iv.disabled === true);

    // Démarrage monitoring.
    document.getElementById('kraken-btn-monitor').click();
    await waitFor(() => document.getElementById('kraken-btn-stop-display').disabled === false, 3000);
    check('monitoring → bouton Arrêter actif',
      document.getElementById('kraken-btn-stop-display').disabled === false);
    const starts = calls(await stats(), '/api/kraken/monitor/start');
    check('POST monitor/start', starts.length >= 1);
    check('monitor/start porte palette + disposition + options',
      !!starts[starts.length - 1] && !!starts[starts.length - 1].body.palette &&
      !!starts[starts.length - 1].body.layout &&
      Array.isArray(starts[starts.length - 1].body.options));

    // Aperçu manuel 👁.
    const before = (await stats()).previewGenerations;
    document.getElementById('kraken-btn-preview').click();
    await sleep(900);
    check('👁 → POST preview', (await stats()).previewGenerations > before);
    check('aperçu affiché',
      document.getElementById('kraken-preview-img').classList.contains('hidden') === false);
    check('src aperçu = endpoint PNG',
      document.getElementById('kraken-preview-img').src.indexOf('/api/kraken/monitor/preview.png') !== -1);

    // Arrêt.
    document.getElementById('kraken-btn-stop-display').click();
    await waitFor(() => document.getElementById('kraken-btn-stop-display').disabled === true, 3000);
    check('arrêt → bouton Arrêter désactivé',
      document.getElementById('kraken-btn-stop-display').disabled === true);
    check('POST display/stop', calls(await stats(), '/api/kraken/display/stop').length >= 1);
  }

  /* ═══ ?test=themes — deux sélecteurs : palette + disposition ═══ */
  async function suiteThemes() {
    const many = params.get('themes') === 'many';
    const expPalettes = many ? 12 : 5;
    const expLayouts = many ? 6 : 3;
    document.querySelector('.tab-btn[data-tab="kraken"]').click();

    const loaded = await waitFor(() =>
      document.querySelectorAll('#layout-gallery .layout-thumb').length >= 3, 6000);
    check('vignettes de disposition <img> servies', loaded);

    const imgs = Array.prototype.slice.call(
      document.querySelectorAll('#layout-gallery .layout-thumb'));
    check('src = /api/kraken/layouts/{key}/thumb.png',
      imgs.length > 0 && imgs.every((i) =>
        i.getAttribute('src').indexOf('/api/kraken/layouts/') === 0 &&
        i.getAttribute('src').slice(-10) === '/thumb.png'));
    check('loading=lazy + decoding=async',
      imgs.every((i) => i.loading === 'lazy' && i.decoding === 'async'));
    check('simulation CSS retirée après chargement',
      document.querySelectorAll('#layout-gallery .lcd').length === 0);

    const paletteCards = document.querySelectorAll('#palette-gallery .palette-card');
    check('nuancier palette = catalogue',
      paletteCards.length === expPalettes, paletteCards.length);
    check('chaque palette a 6 couleurs',
      Array.from(paletteCards).every((c) =>
        c.querySelectorAll('.palette-swatches i').length === 6));

    const st = await stats();
    check('requêtes de vignettes observées côté serveur',
      st.thumbRequests.length >= Math.min(expLayouts, 8), st.thumbRequests.length);
    check('compteur palettes · dispositions',
      document.getElementById('theme-count').textContent.trim() ===
        expPalettes + ' palettes · ' + expLayouts + ' dispositions',
      document.getElementById('theme-count').textContent.trim());
    check('cartes disposition = catalogue',
      document.querySelectorAll('#layout-gallery .layout-card').length === expLayouts);

    // Sélection palette (non défaut) → temps réel + aperçu + dirty.
    const beforePreview = (await stats()).previewGenerations;
    const palTarget = document.querySelector(
      '#palette-gallery .palette-card[data-palette="graphite"]') || paletteCards[2];
    palTarget.click();
    await sleep(1000);
    check('palette : sélection unique',
      document.querySelectorAll('#palette-gallery .palette-card.selected').length === 1);
    check('badge palette mis à jour',
      document.getElementById('palette-key').textContent === palTarget.dataset.palette,
      document.getElementById('palette-key').textContent);
    check('aperçu rafraîchi (palette)',
      (await stats()).previewGenerations > beforePreview);
    check('display/update porte la palette',
      calls(await stats(), '/api/kraken/display/update')
        .some((c) => c.body && c.body.palette === palTarget.dataset.palette));
    check('dirty alimenté après palette',
      document.getElementById('btn-save').disabled === false &&
      document.getElementById('dirty-badge').hidden === false);

    // Sélection disposition → temps réel + aperçu + dirty.
    const beforeLay = (await stats()).previewGenerations;
    const layCards = document.querySelectorAll('#layout-gallery .layout-card');
    const layTarget = layCards[2] || layCards[1];
    layTarget.click();
    await sleep(1000);
    check('disposition : sélection unique',
      document.querySelectorAll('#layout-gallery .layout-card.selected').length === 1);
    check('badge disposition mis à jour',
      document.getElementById('layout-key').textContent === layTarget.dataset.layout,
      document.getElementById('layout-key').textContent);
    check('aperçu rafraîchi (disposition)',
      (await stats()).previewGenerations > beforeLay);
    check('display/update porte la disposition',
      calls(await stats(), '/api/kraken/display/update')
        .some((c) => c.body && c.body.layout === layTarget.dataset.layout));

    // Cancel restaure palette + disposition de la référence.
    document.getElementById('btn-cancel').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 6000);
    check('Cancel restaure la palette (data_center)',
      document.getElementById('palette-key').textContent === 'data_center',
      document.getElementById('palette-key').textContent);
    check('Cancel restaure la disposition (duo)',
      document.getElementById('layout-key').textContent === 'duo',
      document.getElementById('layout-key').textContent);

    // Densité / débordement.
    check('page sans débordement horizontal',
      document.documentElement.scrollWidth <= 1440, document.documentElement.scrollWidth);
    check('vignette disposition ≈ 140 px',
      Math.round(document.querySelector('.layout-thumb').getBoundingClientRect().width) === 140,
      Math.round(document.querySelector('.layout-thumb').getBoundingClientRect().width));
    check('pas de requête de vignette inattendue',
      (await stats()).thumbRequests.every((r) => Array.from(
        document.querySelectorAll('#layout-gallery .layout-card'))
        .some((c) => c.dataset.layout === r.key)));

    if (many) {
      const gallery = document.getElementById('palette-gallery');
      check('scroll interne palette disponible',
        gallery.scrollHeight > gallery.clientHeight + 10,
        gallery.scrollHeight + '>' + gallery.clientHeight);
    }
  }

  const suites = { '1': suiteBase, save: suiteSave, save2: suiteSave2,
                   kraken: suiteKraken, 'kraken-empty': suiteKrakenEmpty,
                   themes: suiteThemes };

  window.addEventListener('load', async () => {
    const ready = await waitFor(() => window.__appReady === true, 15000);
    check('init app terminée', ready);
    const suite = suites[mode];
    if (suite) {
      try { await suite(); } catch (e) {
        check('suite sans exception', false, e && e.message);
      }
    } else {
      check('mode de test connu', false, mode);
    }
    check('aucune erreur JS', window.__bxErrors.length === 0, window.__bxErrors.join(' | '));
    finish();
  });
})();

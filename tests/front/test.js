/**
 * Assertions du harnais BxRGB (?test=1|save|save2|kraken|kraken-empty|themes|anim|anim2|timezone).
 * Injecté en <head> par server.js AVANT app.js pour :
 *   - collecter les erreurs JS dès le premier script ;
 *   - capter le log « Ballistix RGB Controller prêt » (window.__appReady).
 * Le résultat final est écrit dans <pre id="__result"> (dump-dom).
 * anim : hot-swap des modes, dirty/Save/Cancel avec animation, vitesse en
 * temps réel, gestion du 400. anim2 : « F5 », réhydratation sans faux dirty.
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

  /* ═══ ?test=anim — refonte du sous-système animations (étape D) ═══ */
  async function suiteAnim() {
    // Catalogue dynamique : la source est GET /api/animation/effects.
    const loaded = await waitFor(() =>
      document.querySelectorAll('#anim-modes .anim-mode-btn').length >= 2, 6000);
    check('boutons de mode générés depuis /animation/effects', loaded);
    const incBtn = document.querySelector('#anim-modes .anim-mode-btn[data-mode="incandescence"]');
    const rainbowBtn = document.querySelector('#anim-modes .anim-mode-btn[data-mode="rainbow"]');
    const fakeBtn = document.querySelector('#anim-modes .anim-mode-btn[data-mode="pulse_fake"]');
    check('libellés issus du registre (Incandescence / Rainbow)',
      !!incBtn && !!rainbowBtn && incBtn.textContent === 'Incandescence' &&
      rainbowBtn.textContent === 'Rainbow',
      incBtn && incBtn.textContent + '|' + (rainbowBtn && rainbowBtn.textContent));
    check('catalogue test : effet factice présent (?anim=fake)', !!fakeBtn);
    check('repos : Stop désactivé', document.getElementById('btn-anim-stop').disabled === true);
    check('repos : aucun mode actif',
      document.querySelectorAll('#anim-modes .anim-mode-btn.active').length === 0);

    // 1) Démarrage incandescence.
    incBtn.click();
    await waitFor(() => document.getElementById('btn-anim-stop').disabled === false, 5000);
    let st = await stats();
    let starts = calls(st, '/api/animation/start');
    check('démarrage → POST /animation/start', starts.length >= 1);
    check('start porte mode=incandescence',
      !!starts[0] && starts[0].body.mode === 'incandescence',
      JSON.stringify(starts[0] && starts[0].body));
    check('start porte le framerate du status (24, non codé en dur)',
      !!starts[0] && Number(starts[0].body.framerate) === 24,
      starts[0] && starts[0].body.framerate);
    check('incandescence active (aria-pressed)',
      incBtn.getAttribute('aria-pressed') === 'true');
    check('libellé dirty exact « Éclairage : Statique → Incandescence (en marche) »',
      await waitFor(() => document.getElementById('dirty-badge').title.indexOf(
        'Éclairage : Statique → Incandescence (en marche)') !== -1, 2500),
      document.getElementById('dirty-badge').title);
    check('moteur en marche : Stop actif',
      document.getElementById('btn-anim-stop').disabled === false);
    check('paramètres d\'effet rendus (détails visibles)',
      document.getElementById('anim-params-wrap').hidden === false);
    check('paramètre cycle_seconds rendu',
      document.querySelector('#anim-params input[data-param-id="cycle_seconds"]') !== null);

    // Paramètre modifié → hot-swap /start avec params (rendu générique).
    const paramInput = document.querySelector(
      '#anim-params input[data-param-id="cycle_seconds"]');
    if (paramInput) {
      paramInput.value = '4';
      paramInput.dispatchEvent(new Event('input', { bubbles: true }));
      paramInput.dispatchEvent(new Event('change', { bubbles: true }));
      await sleep(500);
      st = await stats();
      const paramStarts = calls(st, '/api/animation/start').filter((c) =>
        c.body && c.body.params && Number(c.body.params.cycle_seconds) === 4);
      check('paramètre d\'effet appliqué à chaud (params.cycle_seconds=4)',
        paramStarts.length >= 1, 'starts=' + paramStarts.length);
    } else {
      check('paramètre d\'effet appliqué à chaud (params.cycle_seconds=4)',
        false, 'input param absent');
    }

    // 2) Hot-swap vers Rainbow SANS Stop (symptôme 1).
    rainbowBtn.click();
    await waitFor(() => rainbowBtn.getAttribute('aria-pressed') === 'true', 5000);
    st = await stats();
    starts = calls(st, '/api/animation/start');
    check('hot-swap : nouveau /start sans aucun Stop préalable',
      starts.length >= 2 && calls(st, '/api/animation/stop').length === 0,
      'starts=' + starts.length);
    check('hot-swap porte mode=rainbow',
      !!starts[starts.length - 1] && starts[starts.length - 1].body.mode === 'rainbow',
      JSON.stringify(starts[starts.length - 1] && starts[starts.length - 1].body));
    check('rainbow obtient le badge actif',
      rainbowBtn.getAttribute('aria-pressed') === 'true');
    check('incandescence perd son badge actif',
      incBtn.getAttribute('aria-pressed') === 'false');
    check('boutons de mode jamais désactivés en marche',
      incBtn.disabled === false && rainbowBtn.disabled === false);

    // 3) Dirty + Save avec l'animation (symptôme 2, décision A).
    await waitFor(() => document.getElementById('btn-save').disabled === false, 3000);
    check('animation → Enregistrer actif',
      document.getElementById('btn-save').disabled === false);
    const dirtyBadge = document.getElementById('dirty-badge');
    await waitFor(() => dirtyBadge.title.indexOf('Éclairage') !== -1, 2500);
    check('item d\'éclairage dans le dirty',
      dirtyBadge.title.indexOf('Éclairage') !== -1, dirtyBadge.title);
    await waitFor(() => dirtyBadge.title.indexOf('Rainbow') !== -1 &&
      dirtyBadge.title.indexOf('en marche') !== -1, 2500);
    check('libellé « Rainbow … (en marche) »',
      dirtyBadge.title.indexOf('Rainbow') !== -1 &&
      dirtyBadge.title.indexOf('en marche') !== -1, dirtyBadge.title);
    document.getElementById('btn-save').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 6000);
    check('Save part réellement (POST /api/save)',
      calls(await stats(), '/api/save').length >= 1);
    check('après Save : plus de dirty',
      document.getElementById('btn-save').disabled === true);

    // 4) Vitesse en temps réel + durée de cycle (symptôme 3).
    const speedEl = document.getElementById('anim-speed');
    const cycleBefore = document.getElementById('anim-cycle').textContent;
    check('avant : borne slider conforme backend (0.1–10)',
      speedEl.min === '0.1' && speedEl.max === '10');
    const refreshEl = document.getElementById('anim-refresh');
    check('avant : bornes refresh conformes backend (1–30)',
      refreshEl.min === '1' && refreshEl.max === '30');
    speedEl.value = '3';
    speedEl.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(700);
    const updates = calls(await stats(), '/api/animation/update');
    check('slider vitesse → POST /animation/update (temps réel)', updates.length >= 1);
    check('update porte speed=3',
      !!updates[updates.length - 1] && Number(updates[updates.length - 1].body.speed) === 3,
      JSON.stringify(updates[updates.length - 1] && updates[updates.length - 1].body));
    const cycleText = document.getElementById('anim-cycle').textContent;
    check('durée de cycle recalculée', cycleText !== cycleBefore, cycleText);
    check('cycle = 6 s / 3 = « 2,0 s »', cycleText.indexOf('2,0 s') !== -1, cycleText);
    check('libellé vitesse 3.0×',
      document.getElementById('anim-speed-value').textContent === '3.0×',
      document.getElementById('anim-speed-value').textContent);
    check('modif vitesse → dirty actif',
      document.getElementById('btn-save').disabled === false);

    // 5) Cancel restaure mode + état + vitesse de la référence.
    document.getElementById('btn-cancel').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 8000);
    check('Cancel part réellement (POST /api/restore)',
      calls(await stats(), '/api/restore').length >= 1);
    check('Cancel restaure Rainbow en marche',
      rainbowBtn.getAttribute('aria-pressed') === 'true');
    check('Cancel restaure la vitesse 1×',
      document.getElementById('anim-speed').value === '1',
      document.getElementById('anim-speed').value);
    check('Cancel restaure le cycle (6,0 s)',
      document.getElementById('anim-cycle').textContent.indexOf('6,0 s') !== -1,
      document.getElementById('anim-cycle').textContent);

    // 6) Couleurs éditées PENDANT l'animation : base_colors + dirty.
    document.querySelectorAll('.dnd-stick')[0].click();
    await sleep(80);
    document.querySelector('#quick-colors .quick-color-btn').click();
    await waitFor(() => document.getElementById('btn-save').disabled === false, 4000);
    check('couleur éditée en marche → PUT couleurs',
      calls(await stats(), '/api/sticks/stick_0/colors').length >= 1);
    check('couleur éditée en marche → dirty couleurs',
      document.getElementById('dirty-badge').title.indexOf('Couleurs') !== -1,
      document.getElementById('dirty-badge').title);
    document.getElementById('btn-cancel').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 8000);
    check('Cancel restaure aussi les couleurs de base',
      document.getElementById('btn-save').disabled === true);

    // 7) 400 « mode inconnu » : état inchangé + message clair.
    fakeBtn.click();
    const toastFound = await waitFor(() => Array.prototype.some.call(
      document.querySelectorAll('.holaf-toast__message'),
      (el) => el.textContent.indexOf('refusé') !== -1), 4000);
    const toastTexts = Array.prototype.map.call(
      document.querySelectorAll('.holaf-toast__message'), (el) => el.textContent);
    check('400 : message d\'erreur clair affiché', toastFound,
      toastTexts.join(' | '));
    const fakeStarts = calls(await stats(), '/api/animation/start')
      .filter((c) => c.body && c.body.mode === 'pulse_fake');
    check('mode inconnu : la requête part bien (réponse 400)', fakeStarts.length >= 1);
    check('400 : mode actif inchangé (Rainbow)',
      rainbowBtn.getAttribute('aria-pressed') === 'true');
    check('400 : le moteur tourne toujours',
      document.getElementById('btn-anim-stop').disabled === false);

    // 8) Densité / débordement.
    check('page sans débordement horizontal',
      document.documentElement.scrollWidth <= 1440, document.documentElement.scrollWidth);
  }

  /* ═══ ?test=anim2 — « F5 » : réhydratation, aucun faux dirty ═══ */
  async function suiteAnim2() {
    const loaded = await waitFor(() =>
      document.querySelectorAll('#anim-modes .anim-mode-btn').length >= 2, 6000);
    check('F5 : catalogue rechargé', loaded);
    const rainbowBtn = document.querySelector('#anim-modes .anim-mode-btn[data-mode="rainbow"]');
    await waitFor(() => rainbowBtn &&
      rainbowBtn.getAttribute('aria-pressed') === 'true', 6000);
    check('F5 : mode courant réhydraté (Rainbow actif)',
      !!rainbowBtn && rainbowBtn.getAttribute('aria-pressed') === 'true');
    check('F5 : moteur repris (Stop actif)',
      document.getElementById('btn-anim-stop').disabled === false);
    check('F5 : vue matrice resynchronisée',
      document.getElementById('mode-indicator').textContent.indexOf('matrice') !== -1,
      document.getElementById('mode-indicator').textContent);
    check('F5 : vitesse réhydratée (1×)',
      document.getElementById('anim-speed').value === '1',
      document.getElementById('anim-speed').value);
    check('F5 : cycle affiché (6,0 s)',
      document.getElementById('anim-cycle').textContent.indexOf('6,0 s') !== -1,
      document.getElementById('anim-cycle').textContent);
    check('F5 : paramètres d\'effet rendus',
      document.getElementById('anim-params-wrap').hidden === false);
    check('F5 : aucun faux dirty (Enregistrer désactivé)',
      document.getElementById('btn-save').disabled === true);
    check('F5 : badge dirty masqué',
      document.getElementById('dirty-badge').hidden === true);
    check('F5 : page sans débordement horizontal',
      document.documentElement.scrollWidth <= 1440,
      document.documentElement.scrollWidth);
  }

  /* ═══ ?test=timezone — fuseau horaire de l'horloge LCD ═══ */
  async function suiteTimezone() {
    const empty = params.get('tz') === 'empty';
    document.querySelector('.tab-btn[data-tab="kraken"]').click();
    await waitFor(() =>
      document.querySelectorAll('#layout-gallery .layout-thumb').length >= 3, 6000);

    const input = document.getElementById('kraken-tz-input');
    const statusEl = document.getElementById('kraken-tz-status');
    const datalist = document.getElementById('kraken-tz-list');
    const options = datalist ? datalist.querySelectorAll('option') : [];
    check('champ fuseau présent', !!input);
    check('datalist peuplée (local + fuseaux)', options.length >= 2, options.length);
    check('option « local » disponible',
      Array.prototype.some.call(options, (o) => o.value === 'local'));
    check('statut initial = heure locale du processus',
      statusEl.textContent.toLowerCase().indexOf('locale du processus') !== -1,
      statusEl.textContent);
    if (empty) {
      // Catalogue serveur vide (tzdata absente) : repli JS + hint visible.
      check('repli sans catalogue : hint visible',
        document.getElementById('kraken-tz-hint').hidden === false);
      check('repli sans catalogue : liste de décalages utilisable',
        options.length >= 10, options.length);
    } else {
      check('Europe/Paris proposé',
        Array.prototype.some.call(options, (o) => o.value === 'Europe/Paris'));
      check('décalage UTC affiché dans la liste',
        Array.prototype.some.call(options, (o) => /UTC[+-]\d{2}:\d{2}/.test(o.textContent)));
      check('pas de hint quand le catalogue est complet',
        document.getElementById('kraken-tz-hint').hidden === true);
    }

    // Sélection → temps réel (débounce 400 ms) + statut + dirty.
    input.value = 'Europe/Paris';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(900);
    let updates = calls(await stats(), '/api/kraken/display/update');
    let last = updates[updates.length - 1];
    check('display/update porte timezone Europe/Paris',
      !!last && last.body && last.body.timezone === 'Europe/Paris',
      last && JSON.stringify(last.body));
    check('statut affiche le fuseau sélectionné',
      statusEl.textContent.indexOf('Europe/Paris') !== -1, statusEl.textContent);
    check('statut affiche le décalage UTC',
      /UTC[+-]\d{2}:\d{2}/.test(statusEl.textContent), statusEl.textContent);
    check('statut affiche l\'heure résultante',
      /\d{2}:\d{2}/.test(statusEl.textContent), statusEl.textContent);
    check('dirty alimenté après fuseau',
      document.getElementById('btn-save').disabled === false &&
      document.getElementById('dirty-badge').hidden === false);
    check('badge dirty mentionne « Fuseau horaire … → Europe/Paris »',
      document.getElementById('dirty-badge').title.indexOf('Fuseau horaire') !== -1 &&
      document.getElementById('dirty-badge').title.indexOf('Europe/Paris') !== -1,
      document.getElementById('dirty-badge').title);

    // L'aperçu part avec le fuseau → le PNG reflète la nouvelle heure.
    document.getElementById('kraken-btn-preview').click();
    await sleep(900);
    const previews = calls(await stats(), '/api/kraken/monitor/preview');
    check('aperçu : POST avec le fuseau sélectionné',
      previews.length >= 1 &&
      previews[previews.length - 1].body.timezone === 'Europe/Paris');
    check('aperçu affiché',
      document.getElementById('kraken-preview-img').classList.contains('hidden') === false);

    // Démarrage monitoring : le fuseau voyage avec les autres réglages.
    document.getElementById('kraken-btn-monitor').click();
    await waitFor(() =>
      document.getElementById('kraken-btn-stop-display').disabled === false, 3000);
    const starts = calls(await stats(), '/api/kraken/monitor/start');
    check('monitor/start porte le fuseau',
      starts.length >= 1 && starts[starts.length - 1].body.timezone === 'Europe/Paris',
      starts.length && JSON.stringify(starts[starts.length - 1].body));

    // Save fige le fuseau ; un autre fuseau est annulé par Cancel.
    document.getElementById('btn-save').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 6000);
    check('save : dirty retombé',
      document.getElementById('btn-save').disabled === true);
    input.value = 'Asia/Tokyo';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(900);
    updates = calls(await stats(), '/api/kraken/display/update');
    last = updates[updates.length - 1];
    check('nouveau fuseau → display/update Asia/Tokyo',
      !!last && last.body.timezone === 'Asia/Tokyo');
    check('nouveau fuseau → dirty',
      document.getElementById('btn-save').disabled === false);
    document.getElementById('btn-cancel').click();
    await waitFor(() => document.getElementById('btn-save').disabled === true &&
      document.getElementById('dirty-badge').hidden === true, 8000);
    check('Cancel restaure le fuseau de la référence',
      input.value === 'Europe/Paris', input.value);
    check('Cancel restaure le statut (Europe/Paris)',
      statusEl.textContent.indexOf('Europe/Paris') !== -1, statusEl.textContent);

    // Bouton ⟲ : retour à l'heure locale du processus.
    document.getElementById('kraken-tz-reset').click();
    await sleep(900);
    updates = calls(await stats(), '/api/kraken/display/update');
    last = updates[updates.length - 1];
    check('reset ⟲ → display/update timezone local',
      !!last && last.body.timezone === 'local',
      last && JSON.stringify(last.body));
    check('reset ⟲ → champ vide', input.value === '', input.value);
    check('reset ⟲ → statut heure locale',
      statusEl.textContent.toLowerCase().indexOf('locale du processus') !== -1,
      statusEl.textContent);

    // Nom inconnu : refusé côté front, aucune requête, champ restauré.
    const beforeKo = calls(await stats(), '/api/kraken/display/update').length;
    input.value = 'Nope/Nope';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    const toastFound = await waitFor(() => Array.prototype.some.call(
      document.querySelectorAll('.holaf-toast__message'),
      (el) => el.textContent.indexOf('inconnu') !== -1), 3000);
    check('fuseau inconnu → toast d\'erreur', toastFound,
      Array.prototype.map.call(document.querySelectorAll('.holaf-toast__message'),
        (el) => el.textContent).join(' | '));
    check('fuseau inconnu → champ restauré', input.value === '', input.value);
    await sleep(600);
    check('fuseau inconnu → aucune requête display/update',
      calls(await stats(), '/api/kraken/display/update').length === beforeKo,
      calls(await stats(), '/api/kraken/display/update').length + ' vs ' + beforeKo);

    // Fin propre : la référence (Europe/Paris) revient.
    document.getElementById('btn-cancel').click();
    await waitFor(() => input.value === 'Europe/Paris' &&
      document.getElementById('btn-save').disabled === true, 8000);
    check('fin : fuseau de référence restauré', input.value === 'Europe/Paris', input.value);
    document.getElementById('kraken-btn-stop-display').click();

    check('page sans débordement horizontal',
      document.documentElement.scrollWidth <= 1440, document.documentElement.scrollWidth);
  }

  const suites = { '1': suiteBase, save: suiteSave, save2: suiteSave2,
                   kraken: suiteKraken, 'kraken-empty': suiteKrakenEmpty,
                   themes: suiteThemes, anim: suiteAnim, anim2: suiteAnim2,
                   timezone: suiteTimezone };

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

/**
 * Assertions du harnais BxRGB (?test=1|save|save2|kraken|themes).
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
      document.querySelectorAll('#theme-gallery .theme-thumb').length >= 3, 5000);

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

    // Thème LCD : temps réel débouncé.
    document.querySelectorAll('#theme-gallery .theme-card')[1].click();
    await sleep(700);
    const updates = calls(await stats(), '/api/kraken/display/update');
    const lastUpdate = updates[updates.length - 1];
    check('thème appliqué en temps réel (display/update)',
      !!lastUpdate && lastUpdate.body && lastUpdate.body.theme === 'overclock',
      lastUpdate && lastUpdate.body && lastUpdate.body.theme);

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
    check('thème restauré (overclock)',
      document.getElementById('theme-key').textContent === 'overclock',
      document.getElementById('theme-key').textContent);
    check('après cancel : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);
  }

  /* ═══════════ ?test=save2 — F5 : hydratation depuis la référence ═══════════ */
  async function suiteSave2() {
    await waitFor(() =>
      document.querySelectorAll('#theme-gallery .theme-thumb').length >= 3, 5000);
    check('thème hydraté depuis la référence (overclock)',
      document.getElementById('theme-key').textContent === 'overclock',
      document.getElementById('theme-key').textContent);
    const selected = document.querySelector('#theme-gallery .theme-card.selected');
    check('carte selected = overclock', !!selected && selected.dataset.theme === 'overclock');
    check('luminosité hydratée (111)',
      document.getElementById('brightness-slider').value === '111',
      document.getElementById('brightness-slider').value);
    check('repos : Enregistrer désactivé', document.getElementById('btn-save').disabled === true);
    check('repos : badge dirty masqué', document.getElementById('dirty-badge').hidden === true);
    check('capteur CPU coché', document.getElementById('kraken-cpu').checked === true);
    check('capteur liquid coché', document.getElementById('kraken-liquid-temp').checked === true);
    check('intervalle hydraté (10 s)', document.getElementById('kraken-interval').value === '10');
  }

  /* ═══════════ ?test=kraken — temps réel vague 2 ═══════════ */
  async function suiteKraken() {
    document.querySelector('.tab-btn[data-tab="kraken"]').click();
    await waitFor(() =>
      document.querySelectorAll('#theme-gallery .theme-thumb').length >= 3, 5000);

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
    check('monitor/start porte thème + options',
      !!starts[starts.length - 1] && !!starts[starts.length - 1].body.theme &&
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

  /* ═══════════ ?test=themes — vraies vignettes + galerie compacte ═══════════ */
  async function suiteThemes() {
    const many = params.get('themes') === 'many';
    const expected = many ? 12 : 3;
    document.querySelector('.tab-btn[data-tab="kraken"]').click();

    const loaded = await waitFor(() =>
      document.querySelectorAll('#theme-gallery .theme-thumb').length >= 3, 6000);
    check('vignettes <img> servies par l’endpoint', loaded);

    const imgs = Array.prototype.slice.call(
      document.querySelectorAll('#theme-gallery .theme-thumb'));
    check('src = /api/kraken/themes/{key}/thumb.png',
      imgs.length > 0 && imgs.every((i) =>
        i.getAttribute('src').indexOf('/api/kraken/themes/') === 0 &&
        i.getAttribute('src').slice(-10) === '/thumb.png'));
    check('loading=lazy + decoding=async',
      imgs.every((i) => i.loading === 'lazy' && i.decoding === 'async'));
    check('simulation CSS retirée après chargement',
      document.querySelectorAll('#theme-gallery .lcd').length === 0);

    const st = await stats();
    check('requêtes de vignettes observées côté serveur',
      st.thumbRequests.length >= Math.min(expected, 8), st.thumbRequests.length);
    check('compteur de thèmes',
      document.getElementById('theme-count').textContent.trim() === expected + ' thèmes',
      document.getElementById('theme-count').textContent.trim());
    check('cartes = nombre de thèmes',
      document.querySelectorAll('#theme-gallery .theme-card').length === expected);

    // Sélection d'une vignette → aperçu auto (débounce ~400 ms).
    const beforePreview = (await stats()).previewGenerations;
    const gallery = document.getElementById('theme-gallery');
    const cards = Array.prototype.slice.call(gallery.querySelectorAll('.theme-card'));
    const target = cards[2];
    const childrenBefore = Array.prototype.slice.call(gallery.children);
    target.click();
    await sleep(1000);
    check('sélection unique', document.querySelectorAll('#theme-gallery .theme-card.selected').length === 1);
    check('carte sélectionnée = cliquée',
      document.querySelector('#theme-gallery .theme-card.selected').dataset.theme === target.dataset.theme);
    check('badge thème mis à jour',
      document.getElementById('theme-key').textContent === target.dataset.theme);
    check('aperçu rafraîchi automatiquement',
      (await stats()).previewGenerations > beforePreview);
    check('aperçu visible',
      document.getElementById('kraken-preview-img').classList.contains('hidden') === false);
    check('display/update temps réel avec le thème cliqué',
      calls(await stats(), '/api/kraken/display/update')
        .some((c) => c.body && c.body.theme === target.dataset.theme));

    const childrenAfter = Array.prototype.slice.call(gallery.children);
    check('sélection sans re-render du DOM',
      childrenBefore.length === childrenAfter.length &&
      childrenBefore.every((n, i) => n === childrenAfter[i]));

    // Densité : vignettes entièrement visibles dans le conteneur plafonné.
    const gRect = gallery.getBoundingClientRect();
    const fullyVisible = cards.filter((c) => {
      const r = c.getBoundingClientRect();
      return r.top >= gRect.top - 1 && r.bottom <= gRect.bottom + 1;
    }).length;
    if (many) {
      check('≥ 8 vignettes entièrement visibles', fullyVisible >= 8, fullyVisible);
    } else {
      check('toutes les vignettes visibles', fullyVisible === cards.length, fullyVisible);
    }
    check('vignette ≈ 96 px',
      Math.round(document.querySelector('.theme-thumb').getBoundingClientRect().width) === 96,
      Math.round(document.querySelector('.theme-thumb').getBoundingClientRect().width));
    check('page sans débordement horizontal',
      document.documentElement.scrollWidth <= 1440, document.documentElement.scrollWidth);

    if (many) {
      check('scroll interne disponible', gallery.scrollHeight > gallery.clientHeight + 10,
        gallery.scrollHeight + '>' + gallery.clientHeight);
      const pageY = window.scrollY;
      gallery.scrollTop = 140;
      await sleep(150);
      check('scroll interne actif', gallery.scrollTop > 0, gallery.scrollTop);
      check('la page n’a pas défilé', window.scrollY === pageY);
    }
    // Le compteur de vignettes ne doit pas dépasser le nombre de cartes.
    check('pas de requête de vignette inattendue',
      (await stats()).thumbRequests.every((r) =>
        cards.some((c) => c.dataset.theme === r.key)));
  }

  const suites = { '1': suiteBase, save: suiteSave, save2: suiteSave2,
                   kraken: suiteKraken, themes: suiteThemes };

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

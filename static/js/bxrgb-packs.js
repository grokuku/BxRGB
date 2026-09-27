/**
 * Ballistix RGB Controller — Packs d'interface holaf-tokens
 * ═══════════════════════════════════════════════════════════
 * Enregistre les 4 packs hôte de BxRGB dans la brique `tokens` 0.3.0
 * (registre VOLATILE : ré-enregistrés à chaque boot), puis rejoue le pack
 * mémorisé AVANT le premier rendu (anti-flash — script classique en tête,
 * chargé juste après vendor/holaf/holaf-tokens.js).
 *
 * - Packs : bxrgb-neon (défaut) / bxrgb-clean / bxrgb-studio / bxrgb-clair.
 *   Définitions reprises VERBATIM de la maquette validée
 *   design-proposals/proposal-a-holaftokens-v03.html (extends + derive).
 * - La brique ne fournit ni widget, ni persistance, ni transition : le
 *   sélecteur (pastilles 🎨 du header), localStorage, l'anti-flash et la
 *   classe `theme-anim` sont à la charge de l'hôte (ce fichier).
 * - Clé de persistance : `bxrgb-pack` (nom du pack, ex. "bxrgb-neon").
 * - Exposé pour les tests/intégrations : window.BxRGBPacks.
 */
(function () {
  'use strict';

  var HT = window.HolafTokens;
  if (!HT || typeof HT.registerPreset !== 'function') {
    console.error('[bxrgb-packs] holaf-tokens 0.3.0 introuvable — le front ' +
      'affiche son repli CSS (valeurs néon par défaut).');
    return;
  }

  var STORAGE_KEY = 'bxrgb-pack';
  var DEFAULT_PACK = 'bxrgb-neon';
  var TRANSITION_MS = 220;

  /* ══════════════════════════════════════════════════════════
     Les 4 packs hôte (identiques à la maquette v03 validée).
     Chaque pack étend un preset intégré (base complète) et n'apporte
     que ses différences ; les clés optionnelles sont dérivées
     automatiquement (options.derive) — sauf txt-glow et danger-gradient,
     fournis explicitement (opt-in).
     ══════════════════════════════════════════════════════════ */
  var PACKS = [
    {
      key: 'neon', name: 'bxrgb-neon', label: 'Néon',
      tokens: {
        surface: '#05060b', 'surface-elev': '#141624', 'surface-raised': '#1c1e30',
        border: '#2a2d3a', text: '#e9ecff', 'text-muted': '#8e97bc',
        accent: '#e94560', 'accent-hover': '#7a5cff', 'accent-text': '#0a0510',
        danger: '#ff4d6d', 'danger-hover': '#d2264b', 'danger-text': '#ffffff',
        ok: '#26e6a5', warn: '#ffb020',
        radius: '16px', shadow: '0 10px 34px rgba(0,0,0,.42)',
        'bg-image': 'radial-gradient(1100px 560px at 12% -12%, var(--holaf-accent-soft), transparent 62%), radial-gradient(900px 480px at 92% -6%, rgba(255,45,120,.13), transparent 58%), radial-gradient(1000px 760px at 50% 122%, rgba(120,0,255,.12), transparent 62%)',
        'txt-glow': '0 0 12px var(--holaf-accent-glow)',
        'danger-gradient': 'linear-gradient(135deg,#ff4d6d,#c01d3c)'
      },
      options: { extends: 'indigo-dark', derive: true }
    },
    {
      key: 'clean', name: 'bxrgb-clean', label: 'Clean',
      tokens: {
        surface: '#131519', 'surface-elev': '#191c22', 'surface-raised': '#1e2229',
        border: '#282d36', text: '#e6e9ef', 'text-muted': '#9aa2b1',
        accent: '#5b8cff', 'accent-hover': '#7aa2ff', 'accent-text': '#0b0d10',
        danger: '#e05e6e', 'danger-text': '#e05e6e',
        ok: '#4ec98a', warn: '#d9a03a',
        radius: '10px', shadow: 'none',
        'bg-image': 'none',
        'txt-glow': 'none',
        'danger-gradient': 'transparent'
      },
      options: { extends: 'indigo-dark', derive: true }
    },
    {
      key: 'studio', name: 'bxrgb-studio', label: 'Studio',
      tokens: {
        surface: '#0b0d0f', 'surface-elev': '#121518', 'surface-raised': '#171b1f',
        border: '#3a424b', text: '#d7dee6', 'text-muted': '#8a939d',
        accent: '#39d98a', 'accent-hover': '#2fbf7a', 'accent-text': '#c9ffe3',
        danger: '#e04b4b', 'danger-text': '#ffb3b3',
        ok: '#39d98a', warn: '#f0a020',
        radius: '4px', shadow: 'inset 0 1px 0 rgba(255,255,255,.04),0 2px 6px rgba(0,0,0,.4)',
        'bg-image': 'repeating-linear-gradient(0deg,transparent,transparent 27px,rgba(255,255,255,.012) 27px,rgba(255,255,255,.012) 28px)',
        'font-sans': "ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace",
        'font-mono': "ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace",
        'txt-glow': 'none',
        'danger-gradient': 'linear-gradient(180deg,#2a1414,#1a0d0d)'
      },
      options: { extends: 'indigo-dark', derive: true }
    },
    {
      key: 'clair', name: 'bxrgb-clair', label: 'Clair',
      tokens: {
        surface: '#eef1f6', 'surface-elev': '#ffffff', 'surface-raised': '#f4f6fa',
        border: '#d8dee8', text: '#1c2333', 'text-muted': '#454e5f',
        accent: '#2f6fe4', 'accent-hover': '#1d4fd8', 'accent-text': '#ffffff',
        danger: '#c62b45', 'danger-text': '#c62b45',
        ok: '#1d9a63', warn: '#b45309',
        radius: '12px', shadow: '0 1px 2px rgba(30,45,80,.06),0 4px 14px rgba(30,45,80,.05)',
        'bg-image': 'radial-gradient(900px 420px at 15% -10%, rgba(47,111,228,.10), transparent 60%), radial-gradient(700px 360px at 95% -8%, rgba(120,80,220,.07), transparent 55%)',
        'txt-glow': 'none',
        'danger-gradient': '#ffffff'
      },
      options: { extends: 'indigo-light', derive: true }
    }
  ];

  var byName = {};
  var byKey = {};
  PACKS.forEach(function (p) {
    byName[p.name] = p;
    byKey[p.key] = p;
  });

  /* ── Enregistrement des packs AVANT tout setTheme (registre volatil) ── */
  PACKS.forEach(function (p) {
    try {
      HT.registerPreset(p.name, p.tokens, p.options);
    } catch (err) {
      console.error('[bxrgb-packs] registerPreset ' + p.name + ' : ' + err.message);
    }
  });

  var current = null;
  var animTimer = null;

  /** Met à jour le sélecteur (aria/état) et les libellés de pack. */
  function syncWidget(name) {
    var pack = byName[name];
    if (!pack) return;
    var group = document.getElementById('ui-theme');
    if (group) {
      var btns = group.querySelectorAll('.uit-btn');
      for (var i = 0; i < btns.length; i++) {
        var on = btns[i].getAttribute('data-pack') === pack.key;
        btns[i].setAttribute('aria-checked', on ? 'true' : 'false');
        btns[i].setAttribute('tabindex', on ? '0' : '-1');
      }
    }
    var nameEl = document.getElementById('uit-name');
    if (nameEl) nameEl.textContent = pack.label;
    var foot = document.getElementById('footer-pack');
    if (foot) foot.textContent = name;
  }

  /**
   * Applique un pack : setTheme (brique) + data-pack (décor hôte).
   * @param {string} name — "bxrgb-neon" | "bxrgb-clean" | "bxrgb-studio" | "bxrgb-clair"
   * @param {{animate?:boolean,persist?:boolean}} [opts]
   */
  function applyPack(name, opts) {
    opts = opts || {};
    if (!byName[name]) name = DEFAULT_PACK;
    var root = document.documentElement;

    if (opts.animate) {
      root.classList.add('theme-anim');
      if (animTimer) clearTimeout(animTimer);
      animTimer = setTimeout(function () {
        root.classList.remove('theme-anim');
      }, TRANSITION_MS);
    }

    // `current` posé AVANT setTheme : l'événement holaf-tokens-changed est
    // dispatché de façon SYNCHRONE par la brique — le listener ci-dessous
    // ne doit pas rappeler applyPack en ré-entrance.
    var previous = current;
    current = name;
    try {
      HT.setTheme(name);
    } catch (err) {
      current = previous;
      console.error('[bxrgb-packs] setTheme ' + name + ' : ' + err.message);
      return;
    }
    root.setAttribute('data-pack', byName[name].key);

    syncWidget(name);
    if (opts.persist) {
      try {
        localStorage.setItem(STORAGE_KEY, name);
      } catch (err) { /* stockage indisponible : le choix reste volatil */ }
    }
  }

  /* ── Anti-flash : rejouer le pack mémorisé AVANT le premier rendu ── */
  var saved = null;
  try {
    saved = localStorage.getItem(STORAGE_KEY);
  } catch (err) { /* file:// ou stockage bloqué : pack par défaut */ }
  applyPack(byName[saved] ? saved : DEFAULT_PACK, { animate: false, persist: false });
  document.documentElement.setAttribute('data-brick-version', HT.VERSION);

  /* ── Sélecteur de packs (pastilles du header) ── */
  function initPicker() {
    var group = document.getElementById('ui-theme');
    if (!group || typeof group.querySelectorAll !== 'function') return;
    var btns = Array.prototype.slice.call(group.querySelectorAll('.uit-btn'));

    // Resynchronise l'état statique du HTML sur le pack réellement appliqué
    // au boot (le applyPack anti-flash s'exécute en tête, avant le DOM).
    syncWidget(current);

    function packOfKey(key) { return byKey[key] || null; }

    btns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pack = packOfKey(btn.getAttribute('data-pack'));
        if (pack) applyPack(pack.name, { persist: true, animate: true });
      });
    });

    group.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      var idx = -1;
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].getAttribute('data-pack') === byName[current].key) idx = i;
      }
      var next = btns[(idx + (e.key === 'ArrowRight' ? 1 : -1) + btns.length) % btns.length];
      var pack = packOfKey(next.getAttribute('data-pack'));
      if (pack) {
        e.preventDefault();
        applyPack(pack.name, { persist: true, animate: true });
        next.focus();
      }
    });

    // La brique reste la source de vérité : si le thème change hors sélecteur
    // (setTheme direct, futur widget…), le sélecteur se resynchronise.
    document.addEventListener('holaf-tokens-changed', function (e) {
      var t = e && e.detail && e.detail.theme;
      if (typeof t === 'string' && byName[t] && t !== current) {
        applyPack(t, { animate: false, persist: false });
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPicker);
  } else {
    initPicker();
  }

  /* ── API hôte (tests, intégrations) ── */
  window.BxRGBPacks = {
    version: HT.VERSION,
    list: function () { return PACKS.map(function (p) { return p.name; }); },
    current: function () { return current; },
    apply: function (name, opts) { applyPack(name, opts); }
  };
})();

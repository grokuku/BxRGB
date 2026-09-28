/**
 * Ballistix RGB Controller — Application principale
 * ═══════════════════════════════════════════════════════════
 * Point d'entrée : State manager, WebSocket, REST API,
 * initialisation des modules.
 */

/* ═══════════════════════════════════════════════════════════
   State Central (Observateur)
   ═══════════════════════════════════════════════════════════ */

const state = {
  /** @type {Array<{id:string, label:string, num_leds:number, bus_num:number, address:string, brightness:number}>} */
  sticks: [],

  /** @type {Object<string, Array<[number,number,number]>>} stick_id → [[R,G,B], ...] */
  colors: {},

  /** @type {{stick_id:string, led_idx:number}|null} */
  selected: null,

  /** @type {string|null} */
  selected_stick: null,

  /** @type {boolean} */
  connected: false,

  /** @type {number} */
  brightness: 255,

  /** @type {Object<string, Array<[number,number,number]>>} Couleurs de BASE
      (couche persistable). Les frames d'animation ne réécrivent QUE
      state.colors ; la couche base reste éditable et comparable à la
      référence, même moteur en marche. */
  baseColors: {},

  /** @type {{mode:string, running:boolean, speed:number, framerate:number,
             refresh:number, params:Object}} État lumière courant (miroir de
      GET /api/animation/status). Source unique du dirty « éclairage ». */
  lighting: {
    mode: 'static', running: false, speed: 1.0,
    framerate: 30, refresh: 20, params: {},
  },

  /** @type {Array<{id:string,label:string,params:Array<Object>}>}
      Catalogue des effets (GET /api/animation/effects) — l'UI ne code
      plus les modes en dur. */
  effects: [],

  /** @private Listeners internes */
  _listeners: {},

  /** @type {string} */
  orientation: 'vertical',

  /** @type {string} Dernière orientation avant de passer en mode matrice */
  lastOrientation: 'vertical',

  /**
   * S'abonne à un événement.
   * @param {string} event — Nom de la propriété ou '*' pour tout
   * @param {Function} callback
   */
  on(event, callback) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(callback);
  },

  /**
   * Déclenche les callbacks d'un événement.
   * @param {string} event
   */
  emit(event) {
    (this._listeners[event] || []).forEach((cb) => cb());
    (this._listeners['*'] || []).forEach((cb) => cb(event));
  },

  /**
   * Notifie tous les observateurs après modification.
   * Appelé automatiquement après les mutations.
   */
  notify() {
    this.emit('sticks');
    this.emit('colors');
    this.emit('selected');
    this.emit('selected_stick');
    this.emit('brightness');
    this.emit('connected');
  },
};

/* ═══════════════════════════════════════════════════════════
   Thèmes holaf — dérivés des tokens --holaf-* (holaf-tokens 0.3.0)
   ═══════════════════════════════════════════════════════════ */

/** Variables --ht-* de HolafToast branchées sur la palette holaf-tokens.
    Les valeurs sont des var(--holaf-…) avec repli : la brique les résout
    à la volée sur l'élément, donc toast/toast suivent le pack d'interface. */
const TOAST_THEME = {
  '--ht-bg': 'var(--holaf-surface-elev, #1c2333)',
  '--ht-fg': 'var(--holaf-text, #ffffff)',
  '--ht-border': 'var(--holaf-border, #484f58)',
  '--ht-accent-info': 'var(--holaf-accent, #e94560)',
  '--ht-accent-success': 'var(--holaf-ok, #2ecc71)',
  '--ht-accent-warning': 'var(--holaf-warn, #f39c12)',
  '--ht-accent-error': 'var(--holaf-danger, #e74c3c)',
  '--ht-shadow': 'var(--holaf-shadow, 0 4px 20px rgba(0, 0, 0, 0.5))',
  '--ht-radius': 'var(--holaf-radius-sm, 10px)',
};

/** Variables --hm-* de HolafModal branchées sur la palette holaf-tokens. */
const MODAL_THEME = {
  '--hm-bg': 'var(--holaf-surface-elev, #1c2333)',
  '--hm-bg-secondary': 'var(--holaf-surface, #161b22)',
  '--hm-bg-input': 'var(--holaf-surface, #0d1117)',
  '--hm-text': 'var(--holaf-text, #ffffff)',
  '--hm-text-secondary': 'var(--holaf-text-muted, #c9d1d9)',
  '--hm-border': 'var(--holaf-border, #30363d)',
  '--hm-accent': 'var(--holaf-accent, #e94560)',
  '--hm-accent-hover': 'var(--holaf-accent-hover, #ff6b81)',
  '--hm-accent-text': 'var(--holaf-accent-text, #ffffff)',
  '--hm-danger': 'var(--holaf-danger, #e74c3c)',
  '--hm-danger-hover': 'var(--holaf-danger-hover, #c7324a)',
  '--hm-danger-text': 'var(--holaf-danger-text, #ffffff)',
  '--hm-overlay-bg': 'rgba(0, 0, 0, 0.7)',
  '--hm-radius': 'var(--holaf-radius-sm, 10px)',
  '--hm-shadow': '0 8px 40px rgba(0, 0, 0, 0.6)',
  '--hm-busy-bg': 'var(--holaf-surface, rgba(28, 35, 51, 0.82))',
};

/* ═══════════════════════════════════════════════════════════
   Toast / Notifications (brique HolafToast)
   ═══════════════════════════════════════════════════════════ */

/**
 * Affiche une notification via la brique HolafToast.
 * Conserve la signature des ~50 appels existants : toast(message, type).
 * @param {string} message
 * @param {'info'|'success'|'error'} type
 */
function toast(message, type = 'info') {
  if (!window.HolafToast) {
    console.warn('[toast] HolafToast indisponible :', message);
    return;
  }
  return window.HolafToast.show({
    message: String(message),
    type,
    duration: 3000,
    theme: TOAST_THEME,
  });
}

/* ═══════════════════════════════════════════════════════════
   WebSocket
   ═══════════════════════════════════════════════════════════ */

let ws = null;
let wsReconnectTimer = null;
const WS_URL = `ws://${window.location.host}/ws`;
// Fallback si on ouvre le fichier localement
const WS_FALLBACK_URL = 'ws://localhost:8080/ws';

/**
 * Envoie un message JSON au serveur via WebSocket.
 * @param {Object} msg
 */
function sendWS(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  } else {
    // Fallback : appel REST direct
    handleFallback(msg);
  }
}

/**
 * Fallback REST quand WS n'est pas disponible.
 */
async function handleFallback(msg) {
  try {
    switch (msg.type) {
      case 'set_led': {
        const stick = state.sticks.find(s => s.id === msg.stick_id);
        if (!stick) return;
        const colors = (state.baseColors[msg.stick_id]
          || state.colors[msg.stick_id] || []).map((c) => c.slice());
        colors[msg.led_idx] = msg.color.slice();
        state.baseColors[msg.stick_id] = colors;
        state.colors[msg.stick_id] = colors.map((c) => c.slice());
        await apiPutColors(msg.stick_id, colors);
        state.notify();
        break;
      }
      case 'set_all_leds': {
        const stick = state.sticks.find(s => s.id === msg.stick_id);
        if (!stick) return;
        const colors = Array(stick.num_leds).fill(null).map(() => msg.color.slice());
        state.baseColors[msg.stick_id] = colors;
        state.colors[msg.stick_id] = colors.map((c) => c.slice());
        await apiPutColors(msg.stick_id, colors);
        state.notify();
        break;
      }
      case 'set_brightness': {
        await apiPutBrightness(msg.stick_id, msg.level);
        break;
      }
      case 'set_all_brightness': {
        for (const stick of state.sticks) {
          await apiPutBrightness(stick.id, msg.level);
        }
        state.brightness = msg.level;
        state.emit('brightness');
        state.notify();
        break;
      }
      case 'apply_colors': {
        for (const [stickId, leds] of Object.entries(msg.colors)) {
          await apiPutColors(stickId, leds);
        }
        await apiApply();
        break;
      }
    }
  } catch (err) {
    console.error('Fallback REST error:', err);
  }
}

/**
 * Charge l'état initial via REST (fallback quand WS pas disponible).
 */
async function loadInitialState() {
  try {
    const statusData = await apiFetch('/status');
    state.sticks = (statusData.sticks || []).map((s) => ({
      ...s,
      brightness: s.brightness ?? 255,
    }));
    state.notify();
    // Charger les couleurs de chaque stick
    for (const stick of state.sticks) {
      await fetchStickInfo(stick.id);
    }
    if (state.sticks.length === 0) {
      toast('Aucun stick détecté. Cliquez sur "Re-scan".', 'info');
    } else {
      toast(`${state.sticks.length} stick(s) détecté(s)`, 'success');
    }
    adoptBrightnessFromSticks();
  } catch (err) {
    console.error('Erreur chargement état initial REST:', err);
    toast('Erreur de connexion au serveur', 'error');
  }
}

/**
 * Met à jour le libellé du mode de connexion (tag du header).
 * @param {string} label — « ⚡ Temps réel » ou « ⚡ REST »
 * @param {'ok'|'warn'} tone
 */
function setConnectionMode(label, tone) {
  const el = document.getElementById('connection-mode');
  if (!el) return;
  el.textContent = label;
  el.style.color = tone === 'ok' ? 'var(--holaf-ok, #2ecc71)' : 'var(--holaf-warn, #e94560)';
}

/**
 * Vérifie si WebSocket est disponible et initialise la connexion.
 */
async function initConnection() {
  try {
    const status = await apiFetch('/ws-status');

    if (status.websocket) {
      console.log('✅ WebSocket disponible, connexion...');
      setConnectionMode('⚡ Temps réel', 'ok');
      connectWS();
    } else {
      console.log('⚠ WebSocket non disponible, mode REST');
      setConnectionMode('⚡ REST', 'warn');
      state.connected = true;
      state.emit('connected');
      updateConnectionStatus(true);
      // Charger l'état initial via REST
      await loadInitialState();
    }
  } catch (err) {
    // Serveur pas encore prêt ? Essayer en REST direct
    console.log('⚠ Statut WS inaccessible, fallback REST:', err);
    setConnectionMode('⚡ REST', 'warn');
    state.connected = true;
    state.emit('connected');
    updateConnectionStatus(true);
    await loadInitialState();
  }
}

/**
 * Établit la connexion WebSocket.
 */
function connectWS() {
  // Nettoyer l'ancienne connexion
  if (ws) {
    ws.onclose = null;
    ws.onerror = null;
    ws.onmessage = null;
    try { ws.close(); } catch (_) {}
  }

  const url = window.location.host ? WS_URL : WS_FALLBACK_URL;

  try {
    ws = new WebSocket(url);
  } catch (err) {
    console.error('WebSocket creation error:', err);
    updateConnectionStatus(false);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log('✅ WebSocket connecté');
    state.connected = true;
    state.emit('connected');
    updateConnectionStatus(true);
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
  };

  ws.onclose = (event) => {
    console.log('WebSocket déconnecté, reconnexion dans 3s...', event.code);
    state.connected = false;
    state.emit('connected');
    updateConnectionStatus(false);
    setTimeout(connectWS, 3000);
  };

  ws.onerror = (event) => {
    console.warn('WebSocket error (non bloquant):', event);
    state.connected = false;
    state.emit('connected');
    updateConnectionStatus(false);
    // Ne pas showToast — le onclose va gérer la reconnexion
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleWSMessage(data);
    } catch (e) {
      console.warn('Message WS invalide:', e);
    }
  };
}

/**
 * Planifie une reconnexion WebSocket.
 */
function scheduleReconnect() {
  if (wsReconnectTimer) return;
  console.log('🔄 Reconnexion WebSocket dans 3s...');
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWS();
  }, 3000);
}

/**
 * Met à jour l'indicateur de connexion dans le DOM.
 */
function updateConnectionStatus(connected) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (dot) {
    dot.className = 'status-dot' + (connected ? ' connected' : ' disconnected anim');
  }
  if (text) {
    text.textContent = connected ? 'Connecté' : 'Déconnecté';
  }
}

/* ═══════════════════════════════════════════════════════════
   Gestion des messages WebSocket
   ═══════════════════════════════════════════════════════════ */

function handleWSMessage(data) {
  switch (data.type) {
    case 'status': {
      // Préserver l'ordre existant si les sticks n'ont pas changé
      const oldIds = state.sticks.map(s => s.id).join(',');
      const newIds = (data.sticks || []).map(s => s.id).join(',');
      
      if (oldIds === newIds && state.sticks.length > 0) {
        // Mêmes sticks : juste mettre à jour les infos (brightness etc) sans changer l'ordre
        const newSticks = (data.sticks || []).map((s) => ({ ...s, brightness: s.brightness ?? 255 }));
        const oldById = {};
        state.sticks.forEach(s => oldById[s.id] = s);
        state.sticks = newSticks.map(s => {
          const old = oldById[s.id];
          return old ? { ...s } : s;
        });
        // En fait garder l'ancien ordre
        state.sticks = state.sticks; // no-op, l'ordre est déjà préservé
      } else {
        // Sticks différents : charger la nouvelle liste
        state.sticks = (data.sticks || []).map((s) => ({
          ...s,
          brightness: s.brightness ?? 255,
        }));
      }
      
      // Récupérer les couleurs pour les sticks qui n'en ont pas encore
      for (const stick of state.sticks) {
        if (!state.colors[stick.id]) {
          fetchStickInfo(stick.id);
        }
      }
      adoptBrightnessFromSticks();
      state.notify();
      console.log(`📦 Status reçu : ${state.sticks.length} stick(s)`);
      break;
    }

    case 'color_applied': {
      if (data.leds && data.stick_id) {
        const base = data.leds.map((c) => [Number(c[0]) || 0, Number(c[1]) || 0, Number(c[2]) || 0]);
        state.baseColors[data.stick_id] = base;
        state.colors[data.stick_id] = base.map((c) => c.slice());
        state.emit('colors');
        // Mettre à jour le brightness du stick
        const stick = state.sticks.find(s => s.id === data.stick_id);
        if (stick) {
          // Re-fetch brightness si possible
          fetchStickInfo(data.stick_id);
        }
      }
      break;
    }

    case 'applied': {
      // Ne PAS re-fetch : ça écrase l'ordre des sticks
      toast('✅ Couleurs synchronisées', 'success');
      break;
    }

    case 'rescan': {
      // Préserver l'ordre existant si possible
      const oldOrder = state.sticks.map(s => s.id);
      const newSticksList = (data.sticks || []).map((s) => ({
        ...s,
        brightness: s.brightness ?? 255,
      }));
      
      // Réordonner selon l'ancien ordre
      if (oldOrder.length > 0) {
        const ordered = [];
        const used = new Set();
        for (const oldId of oldOrder) {
          const found = newSticksList.find(s => s.id === oldId);
          if (found) {
            ordered.push(found);
            used.add(found.id);
          }
        }
        // Ajouter les nouveaux sticks non triés
        for (const s of newSticksList) {
          if (!used.has(s.id)) {
            ordered.push(s);
          }
        }
        state.sticks = ordered;
      } else {
        state.sticks = newSticksList;
      }
      
      // Nettoyer les couleurs des sticks disparus (les deux couches)
      const newIds = new Set(state.sticks.map(s => s.id));
      for (const sid of Object.keys(state.colors)) {
        if (!newIds.has(sid)) {
          delete state.colors[sid];
          delete state.baseColors[sid];
        }
      }

      if (state.selected && !newIds.has(state.selected.stick_id)) {
        state.selected = null;
      }
      if (state.selected_stick && !newIds.has(state.selected_stick)) {
        state.selected_stick = null;
      }

      state.notify();

      // Charger les couleurs des sticks nouveaux
      for (const stick of state.sticks) {
        if (!state.colors[stick.id]) {
          fetchStickInfo(stick.id);
        }
      }

      toast('🔍 Scan terminé — ' + state.sticks.length + ' stick(s) détecté(s)', 'success');
      break;
    }

    case 'error': {
      toast('⚠ ' + (data.message || 'Erreur inconnue'), 'error');
      break;
    }

    case 'pong':
      // Ignorer
      break;

    case 'animation_frame':
      // Couche transitoire : les frames n'écrivent QUE l'affichage.
      // L'état marche/arrêt vient de animation_started/stopped/status.
      if (data.colors) {
        for (const [stickId, leds] of Object.entries(data.colors)) {
          state.colors[stickId] = leds;
        }
        state.emit('colors');
      }
      break;

    case 'animation_started': {
      applyLightingStatus({ ...data, running: true });
      refreshAnimationUI();
      scheduleDirtyUpdate();
      refreshAnimationState();
      break;
    }

    case 'animation_update': {
      applyLightingStatus(data);
      refreshAnimationUI();
      scheduleDirtyUpdate();
      break;
    }

    case 'animation_stopped': {
      applyLightingStatus({ ...data, running: false, mode: 'static' });
      syncDisplayToBaseColors();
      refreshAnimationUI();
      scheduleDirtyUpdate();
      break;
    }

    case 'settings_saved': {
      // Un autre onglet a enregistré : la référence change, pas l'état courant
      if (data.reference) {
        savedReference = normalizeReference(data.reference);
        refreshDirtyNow();
      } else {
        loadSavedReference();
      }
      break;
    }

    case 'settings_restored': {
      // Un autre onglet a annulé : se resynchroniser complètement
      if (data.reference) savedReference = normalizeReference(data.reference);
      rehydrateAfterRestore()
        .then(() => refreshDirtyNow())
        .catch(() => {});
      break;
    }

    default:
      console.log('📨 Message WS non géré:', data);
  }
}

/* ═══════════════════════════════════════════════════════════
   API REST
   ═══════════════════════════════════════════════════════════ */

const API_BASE = '/api';

/**
 * Appel API JSON via la brique HolafFetch (timeout, erreurs typées, JSON
 * blindé). Conserve la signature des appels existants : apiFetch(path, opts)
 * → Promise<données JSON>. Lève une HolafFetchError en cas d'échec HTTP/réseau.
 * Retry réseau/5xx UNIQUEMENT sur les GET (idempotents) : jamais sur les
 * mutations (POST/PUT/DELETE) pour ne pas dupliquer un effet de bord.
 * @param {string} path — chemin relatif à /api
 * @param {Object} [options] — options HolafFetch (method, body, timeout, retry…)
 * @returns {Promise<any>}
 */
function apiFetch(path, options = {}) {
  if (!window.HolafFetch) {
    return Promise.reject(new Error('HolafFetch indisponible'));
  }
  const url = `${API_BASE}${path}`;
  const method = (options.method || 'GET').toUpperCase();
  const retry = options.retry !== undefined
    ? options.retry
    : (method === 'GET' ? { attempts: 2, backoffMs: 250 } : null);
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  return window.HolafFetch.request(url, { ...options, method, retry, headers });
}

async function fetchStatus() {
  try {
    const data = await apiFetch('/status');
    state.sticks = (data.sticks || []).map((s) => ({
      ...s,
      brightness: s.brightness ?? 255,
    }));
    adoptBrightnessFromSticks();
    state.notify();
    // Récupérer les couleurs de chaque stick
    for (const stick of state.sticks) {
      fetchStickInfo(stick.id);
    }
    return data;
  } catch (err) {
    console.error('Erreur fetch status:', err);
    toast('Erreur de connexion au serveur', 'error');
    return null;
  }
}

async function fetchStickInfo(stickId) {
  try {
    const data = await apiFetch(`/sticks/${stickId}`);
    if (data.colors) {
      const base = data.colors.map((c) => [Number(c[0]) || 0, Number(c[1]) || 0, Number(c[2]) || 0]);
      state.baseColors[stickId] = base;
      state.colors[stickId] = base.map((c) => c.slice());
      state.emit('colors');
    }
    return data;
  } catch (err) {
    console.error(`Erreur fetch stick ${stickId}:`, err);
    return null;
  }
}

async function apiPutColors(stickId, leds) {
  try {
    return await apiFetch(`/sticks/${stickId}/colors`, {
      method: 'PUT',
      body: JSON.stringify({ leds }),
    });
  } catch (err) {
    console.error(`Erreur PUT colors ${stickId}:`, err);
    toast(`Erreur envoi couleurs: ${err.message}`, 'error');
    return null;
  }
}

async function apiPutBrightness(stickId, level) {
  try {
    return await apiFetch(`/sticks/${stickId}/brightness`, {
      method: 'PUT',
      body: JSON.stringify({ level }),
    });
  } catch (err) {
    console.error(`Erreur PUT brightness ${stickId}:`, err);
    return null;
  }
}

async function apiApply() {
  try {
    return await apiFetch('/apply', { method: 'POST' });
  } catch (err) {
    console.error('Erreur POST apply:', err);
    toast(`Erreur apply: ${err.message}`, 'error');
    return null;
  }
}

async function apiRescan() {
  try {
    return await apiFetch('/rescan', { method: 'POST' });
  } catch (err) {
    console.error('Erreur POST rescan:', err);
    toast(`Erreur rescan: ${err.message}`, 'error');
    return null;
  }
}

/* ═══════════════════════════════════════════════════════════
   Kraken NZXT
   ═══════════════════════════════════════════════════════════ */

/** Capteurs du statut Kraken (miroir de ballistix/kraken.py:STATUS_FIELDS). */
const KRAKEN_FIELDS = ['liquid_temperature', 'pump_speed', 'fan_speed'];
const KRAKEN_FIELD_LABELS = {
  liquid_temperature: 'température liquide',
  pump_speed: 'vitesse pompe',
  fan_speed: 'vitesse ventilos',
};

/** Explique les « — » du statut Kraken : cause, sortie brute, commande à lancer.
 *
 * Rétro-compatible : si le serveur ne fournit pas status_missing, les clés
 * absentes sont déduites des valeurs reçues. N'affiche rien quand toutes
 * les valeurs sont présentes. */
function appendKrakenStatusDiagnostic(el, data) {
  const st = data.status || {};
  const missing = Array.isArray(data.status_missing)
    ? data.status_missing
    : KRAKEN_FIELDS.filter((k) => st[k] === null || st[k] === undefined);
  const noneAvailable = missing.length === KRAKEN_FIELDS.length;
  if (data.error || data.status_ok === false || noneAvailable) {
    const cause = data.error || 'Aucune valeur remontée par liquidctl';
    el.innerHTML += `
      <div class="kraken-status-error">
        ⚠ <strong>Statut illisible :</strong> ${escapeHtml(String(cause))}<br>
        Diagnostic dans le container qui exécute BxRGB : <code>liquidctl --match Kraken status</code>
      </div>`;
    if (typeof data.status_raw === 'string' && data.status_raw.trim()) {
      el.innerHTML += `
        <details class="kraken-raw">
          <summary>Sortie brute de liquidctl</summary>
          <pre>${escapeHtml(data.status_raw)}</pre>
        </details>`;
    }
    return;
  }
  if (missing.length) {
    const labels = missing.map((k) => KRAKEN_FIELD_LABELS[k] || k);
    el.innerHTML += `<div class="kraken-status-note">ℹ Non remonté par ce modèle : ${escapeHtml(labels.join(', '))}</div>`;
  }
}

/** Met à jour le statut Kraken affiché dans l'onglet. */
async function refreshKraken() {
  const statusEl = document.getElementById('kraken-status');
  const badgeEl = document.getElementById('kraken-badge');
  if (!statusEl) return;

  let data;
  try {
    data = await apiFetch('/kraken/status');
  } catch (err) {
    statusEl.innerHTML = `<div class="kraken-status-error">⚠ Impossible de contacter le serveur : ${escapeHtml(err.message)}</div>`;
    if (badgeEl) { badgeEl.textContent = 'Erreur'; badgeEl.className = 'badge err'; }
    return;
  }

  if (!data.available) {
    statusEl.innerHTML = `
      <div class="kraken-status-error">
        ⚠ <strong>liquidctl n'est pas installé.</strong><br>
        Installez-le pour contrôler le Kraken :<br>
        <code style="font-size:0.8rem;">sudo pip install liquidctl</code> ou <code style="font-size:0.8rem;">yay -S liquidctl</code>
      </div>`;
    if (badgeEl) { badgeEl.textContent = 'liquidctl manquant'; badgeEl.className = 'badge err'; }
    setKrakenControls(false);
    return;
  }

  if (data.detected) {
    if (badgeEl) { badgeEl.textContent = 'Détecté'; badgeEl.className = 'badge ok'; }
    const st = data.status || {};
    const temp = st.liquid_temperature;
    let tempClass = 'normal';
    if (temp !== null && temp !== undefined) {
      if (temp < 30) tempClass = 'cold';
      else if (temp < 40) tempClass = 'normal';
      else if (temp < 50) tempClass = 'warm';
      else tempClass = 'hot';
    }
    statusEl.innerHTML = `
      <div class="kraken-status-grid">
        <div class="kraken-stat">
          <div class="kraken-stat-label">🌡 Température liquide</div>
          <div class="kraken-stat-value ${tempClass}">${temp !== null && temp !== undefined ? temp + '°C' : '—'}</div>
        </div>
        <div class="kraken-stat">
          <div class="kraken-stat-label">🌀 Vitesse pompe</div>
          <div class="kraken-stat-value">${st.pump_speed !== null && st.pump_speed !== undefined ? st.pump_speed + ' rpm' : '—'}</div>
        </div>
        <div class="kraken-stat">
          <div class="kraken-stat-label">💨 Vitesse ventilos</div>
          <div class="kraken-stat-value">${st.fan_speed !== null && st.fan_speed !== undefined ? st.fan_speed + ' rpm' : '—'}</div>
        </div>
      </div>`;
    appendKrakenStatusDiagnostic(statusEl, data);
    if (data.devices && data.devices.length) {
      statusEl.innerHTML += `<div class="kraken-devices">🔌 ${escapeHtml(data.devices.join(' · '))}</div>`;
    }
    setKrakenControls(true);
  } else {
    statusEl.innerHTML = `
      <div class="kraken-status-error">
        ⚠ <strong>Aucun périphérique NZXT détecté.</strong><br>
        Vérifiez que le Kraken est branché, que la règle udev est en place et relancez une détection.<br>
        <code style="font-size:0.8rem;">liquidctl list</code>
      </div>`;
    if (badgeEl) { badgeEl.textContent = 'Non détecté'; badgeEl.className = 'badge err'; }
    setKrakenControls(false);
  }
}

/** Active/désactive les contrôles LCD selon la détection. */
function setKrakenControls(enabled) {
  krakenControlsEnabled = !!enabled;
  ['kraken-btn-liquid', 'kraken-btn-monitor', 'kraken-btn-gallery', 'kraken-btn-stop-display',
   'kraken-btn-image', 'kraken-btn-gif', 'kraken-file-image', 'kraken-file-gif',
   'kraken-file-gallery', 'kraken-btn-gallery-add', 'kraken-btn-preview',
   'kraken-brightness', 'kraken-orientation', 'kraken-interval',
   'kraken-interval-asap', 'kraken-tz-input', 'kraken-tz-reset'].forEach(id => {
    const el = document.getElementById(id);
    if (el && id !== 'kraken-btn-stop-display') el.disabled = !enabled;
  });
  // Le slider d'intervalle peut aussi être désactivé par l'option « asap ».
  krakenUpdateIntervalLabel();
  const badge = document.getElementById('kraken-lcd-badge');
  if (badge) {
    badge.textContent = enabled ? 'Prêt' : 'Indisponible';
    badge.className = enabled ? 'badge ok' : 'badge err';
  }
}

/** Convertit un fichier en base64. */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Lecture du fichier impossible'));
    reader.readAsDataURL(file);
  });
}

/** Affiche la température liquide sur l'écran. */
async function krakenSetLiquid() {
  await krakenStopDisplay(false);
  try {
    const data = await apiFetch('/kraken/lcd/mode', {
      method: 'POST',
      body: { mode: 'liquid' },
    });
    if (data.ok) {
      krakenLcdMode = 'liquid';
      scheduleDirtyUpdate();
      toast('🌡 Température liquide affichée', 'success');
    } else {
      toast('⚠ ' + (data.error || 'Erreur mode LCD'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Envoie une image (statique) ou un GIF sur l'écran. */
async function krakenUploadImage(isGif) {
  const fileInput = document.getElementById(isGif ? 'kraken-file-gif' : 'kraken-file-image');
  const btn = document.getElementById(isGif ? 'kraken-btn-gif' : 'kraken-btn-image');
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    toast('⚠ Sélectionnez d\'abord un fichier', 'info');
    return;
  }
  const file = fileInput.files[0];
  btn.disabled = true;
  btn.textContent = '⏳ Envoi...';
  try {
    const data = await fileToBase64(file);
    const result = await apiFetch('/kraken/lcd/image', {
      method: 'POST',
      timeout: 120000,
      body: { data, filename: file.name, animated: isGif },
    });
    if (result.ok) {
      toast((isGif ? '🎬 GIF' : '🖼 Image') + ' affiché sur l\'écran', 'success');
      // Annulable : l'ancienne image est sauvegardée côté serveur → dirty.
      await loadPendingChanges();
      refreshDirtyNow();
    } else {
      toast('⚠ ' + (result.error || 'Erreur envoi image'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = isGif ? '🎬 Envoyer le GIF' : '🖼 Envoyer l\'image';
  }
}

/** Règle la luminosité de l'écran (debounced). */
let krakenBrightnessTimer = null;
function krakenSetBrightness(value) {
  clearTimeout(krakenBrightnessTimer);
  const val = parseInt(value, 10) || 0;
  const label = document.getElementById('kraken-brightness-value');
  if (label) label.textContent = val;
  krakenBrightnessTimer = setTimeout(async () => {
    try {
      await apiFetch('/kraken/lcd/brightness', {
        method: 'POST',
        body: { value: val },
      });
    } catch (err) {
      toast('⚠ Erreur luminosité: ' + err.message, 'error');
    }
  }, 400);
}

/** Règle l'orientation de l'écran. */
async function krakenSetOrientation(value) {
  try {
    const data = await apiFetch('/kraken/lcd/orientation', {
      method: 'POST',
      body: { value: parseInt(value, 10) || 0 },
    });
    if (data.ok) toast('🔄 Orientation réglée', 'success');
    else toast('⚠ ' + (data.error || 'Erreur orientation'), 'error');
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Initialise le Kraken (après boot à froid). */
async function krakenInitialize() {
  const btn = document.getElementById('kraken-btn-init');
  btn.disabled = true;
  btn.textContent = '⏳ Initialisation...';
  try {
    const data = await apiFetch('/kraken/initialize', { method: 'POST' });
    if (data.ok) {
      toast('🔧 Initialisation réussie', 'success');
      setTimeout(refreshKraken, 500);
    } else {
      toast('⚠ ' + (data.error || 'Échec initialisation'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '🔧 Initialize';
  }
}

/* ── Modes d'affichage : monitoring / gallery / arrêt ── */

/** Lit l'intervalle sélectionné (secondes, ou 'asap'). */
function krakenInterval() {
  const asapEl = document.getElementById('kraken-interval-asap');
  if (asapEl && asapEl.checked) return 'asap';
  const el = document.getElementById('kraken-interval');
  return el ? parseFloat(el.value) : 10;
}

/** Met à jour le bouton Arrêter selon l'état du thread. */
async function refreshDisplayStatus() {
  try {
    const data = await apiFetch('/kraken/display/status');
    const stopBtn = document.getElementById('kraken-btn-stop-display');
    if (stopBtn) stopBtn.disabled = !(data && data.running);
  } catch (err) { /* silencieux */ }
}

/* ── Écran LCD : deux sélecteurs (Palette × Disposition) ────────
   Les deux catalogues viennent du BACKEND (GET /api/kraken/themes,
   dérivé de ballistix/monitor.py). Le sélecteur DISPOSITION affiche des
   vignettes PNG du VRAI moteur PIL (GET /api/kraken/layouts/{key}/thumb.png) ;
   le sélecteur PALETTE affiche un nuancier (6 couleurs, aucune géométrie).
   Le front ne recopie ni les couleurs ni les stats d'exemple : ajouter une
   palette/disposition dans monitor.py suffit. La simulation CSS ne sert que
   de squelette (chargement) et de repli hors-ligne. La sélection n'est PAS
   un thème d'interface : elle part dans la config monitoring
   (`palette` + `layout`). */

/** Repli hors-ligne des PALETTES (API injoignable) — l'API reste la source. */
const LCD_FALLBACK_PALETTES = [
  { key: 'data_center', label: 'Data Center', subtitle: 'Bleu Technique',
    default: true, is_new: false,
    colors: { bg: '#050f19', text: '#c8e6ff', accent: '#00a0ff',
              gauge_bg: '#0a1e32', gauge_start: '#003c78', gauge_end: '#00b4ff' } },
  { key: 'overclock', label: 'Overclock', subtitle: 'Rouge Agressif',
    is_new: false,
    colors: { bg: '#0f0505', text: '#f0f0f0', accent: '#ff0000',
              gauge_bg: '#2d0a0a', gauge_start: '#960000', gauge_end: '#ff2828' } },
  { key: 'fluid_flow', label: 'Fluid Flow', subtitle: 'Bleu Pastel',
    is_new: false,
    colors: { bg: '#19232d', text: '#e6f5ff', accent: '#78d2ff',
              gauge_bg: '#32465a', gauge_start: '#a0d2ff', gauge_end: '#c8e6ff' } },
  { key: 'graphite', label: 'Graphite', subtitle: 'Mono sobre · nouvelle',
    is_new: true,
    colors: { bg: '#101113', text: '#e8eaee', accent: '#aab1bc',
              gauge_bg: '#262a30', gauge_start: '#606874', gauge_end: '#c4cbd6' } },
  { key: 'amber', label: 'Amber', subtitle: 'Fort contraste · nouvelle',
    is_new: true,
    colors: { bg: '#0a0804', text: '#fff1d6', accent: '#ffb020',
              gauge_bg: '#302008', gauge_start: '#b06000', gauge_end: '#ffc840' } },
];

/** Repli hors-ligne des DISPOSITIONS. */
const LCD_FALLBACK_LAYOUTS = [
  { key: 'duo', label: 'Duo', subtitle: 'Deux colonnes — valeurs XL',
    default: true, is_new: true },
  { key: 'classic', label: 'Classique', subtitle: 'Liste verticale — actuelle',
    is_new: false },
  { key: 'rings', label: 'Anneaux', subtitle: 'Jauges circulaires',
    is_new: true },
];

/** Palettes LCD courantes (repli, puis catalogue serveur). */
let krakenPalettes = LCD_FALLBACK_PALETTES.slice();

/** Dispositions LCD courantes (repli, puis catalogue serveur). */
let krakenLayouts = LCD_FALLBACK_LAYOUTS.slice();

/** Palette LCD sélectionnée — source unique de getKrakenMonitorConfig(). */
let krakenLcdPalette = 'data_center';

/** Disposition LCD sélectionnée. */
let krakenLcdLayout = 'duo';

/** Mode d'écran LCD courant (liquid — état mémoire serveur). */
let krakenLcdMode = 'liquid';

/** Mode d'affichage courant : null | 'monitor' | 'gallery'. */
let krakenDisplayMode = null;

/** Les contrôles Kraken sont-ils disponibles (liquidctl détecté) ? */
let krakenControlsEnabled = false;

/** Capteurs monitoring : clé d'option → id de la checkbox correspondante. */
const KRAKEN_SENSOR_KEYS = ['cpu', 'gpu', 'ram', 'vram', 'disks', 'liquid'];
function krakenSensorEl(key) {
  return document.getElementById(key === 'liquid' ? 'kraken-liquid-temp' : 'kraken-' + key);
}

/* ── Fuseau horaire de l'horloge LCD ─────────────────────
   Approche retenue : champ texte libre + <datalist> peuplée depuis
   GET /api/kraken/timezones. Le filtrage natif du navigateur encaisse
   les ~600 noms IANA sans code de liste custom ; le champ vide vaut
   « heure locale du processus ». La valeur est validée contre le
   catalogue AVANT envoi : un nom inconnu est refusé côté front (toast,
   aucune modification), et le backend re-valide en 400 de toute façon. */

/** Catalogue serveur des fuseaux : [{key, utc_offset}, ...]. */
let krakenTimezones = [];

/** Repli hors-ligne (endpoint injoignable/vide) : quelques fuseaux usuels. */
const TZ_FALLBACK = [
  'UTC', 'Europe/Paris', 'Europe/London', 'America/New_York',
  'America/Los_Angeles', 'Asia/Tokyo', 'Asia/Shanghai', 'Australia/Sydney',
  'UTC-05:00', 'UTC+02:00', 'UTC+09:00',
];

/** Fuseau sélectionné : null = heure locale du processus. */
let krakenLcdTimezone = null;

/** Réglage canonique : null si absent/vide/"local", sinon le nom nettoyé. */
function normalizeTimezoneKey(value) {
  if (typeof value !== 'string') return null;
  const v = value.trim();
  if (!v || v.toLowerCase() === 'local') return null;
  return v;
}

/** Décalage UTC (« UTC+02:00 ») d'un fuseau, '' si indéterminable.
    Intl.DateTimeFormat suit DST ; les clés de repli « UTC±HH:MM » sont
    formatées directement (Intl ne les accepte pas comme timeZone). */
function timezoneOffsetText(tz) {
  if (!tz) {
    const min = new Date().getTimezoneOffset(); // minutes à l'ouest d'UTC
    const abs = Math.abs(min);
    return 'UTC' + (min <= 0 ? '+' : '-') +
      String(Math.floor(abs / 60)).padStart(2, '0') + ':' +
      String(abs % 60).padStart(2, '0');
  }
  const fixed = /^UTC([+-])(\d{2}):(\d{2})$/.exec(tz);
  if (fixed) return 'UTC' + fixed[1] + fixed[2] + ':' + fixed[3];
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, timeZoneName: 'longOffset',
    }).formatToParts(new Date());
    const name = parts.find((p) => p.type === 'timeZoneName');
    return name ? name.value.replace('GMT', 'UTC') : '';
  } catch (err) {
    return '';
  }
}

/** Heure résultante ("14:32") dans un fuseau, '' si indéterminable. */
function timezoneClockText(tz) {
  try {
    const opts = { hour: '2-digit', minute: '2-digit', hour12: false };
    if (tz) opts.timeZone = tz;
    return new Intl.DateTimeFormat('fr-FR', opts).format(new Date());
  } catch (err) {
    return '';
  }
}

/** Met à jour le libellé « fuseau — UTC±HH:MM — heure affichée ». */
function updateKrakenTimezoneStatus() {
  const el = document.getElementById('kraken-tz-status');
  const input = document.getElementById('kraken-tz-input');
  // Ne jamais écraser une saisie en cours (ex. catalogue chargé en retard).
  if (input && document.activeElement !== input &&
      input.value.trim() !== (krakenLcdTimezone || '')) {
    input.value = krakenLcdTimezone || '';
  }
  if (!el) return;
  const offset = timezoneOffsetText(krakenLcdTimezone);
  const clock = timezoneClockText(krakenLcdTimezone);
  let text = 'Fuseau : ' + (krakenLcdTimezone || 'heure locale du processus');
  if (offset) text += ' — ' + offset;
  if (clock) text += ' — ' + clock + ' affiché';
  el.textContent = text;
}

/** Sélectionne un fuseau (null = local) : état + champ + statut + dirty. */
function selectKrakenTimezone(key) {
  krakenLcdTimezone = normalizeTimezoneKey(key);
  updateKrakenTimezoneStatus();
  scheduleDirtyUpdate();
}

/** Entrées affichées/validées : catalogue serveur, sinon repli local. */
function krakenTimezoneEntries() {
  return krakenTimezones.length
    ? krakenTimezones
    : TZ_FALLBACK.map((key) => ({ key, utc_offset: '' }));
}

/** Peuple la <datalist> (catalogue serveur, sinon repli local). */
function renderKrakenTimezoneOptions() {
  const list = document.getElementById('kraken-tz-list');
  if (!list) return;
  const entries = krakenTimezoneEntries();
  const localOffset = timezoneOffsetText(null);
  const options = ['<option value="local">Heure locale du processus' +
    (localOffset ? ' (' + localOffset + ')' : '') + '</option>'];
  entries.forEach((entry) => {
    const label = entry.key + (entry.utc_offset ? ' — UTC' + entry.utc_offset : '');
    options.push('<option value="' + escapeHtml(entry.key) + '">' +
      escapeHtml(label) + '</option>');
  });
  list.innerHTML = options.join('');
  const hint = document.getElementById('kraken-tz-hint');
  if (hint) hint.hidden = krakenTimezones.length > 0;
}

/** Charge le catalogue des fuseaux (GET /kraken/timezones). */
async function refreshKrakenTimezones() {
  if (!document.getElementById('kraken-tz-input')) return;
  try {
    const data = await apiFetch('/kraken/timezones');
    if (data && data.ok && Array.isArray(data.timezones) && data.timezones.length) {
      krakenTimezones = data.timezones;
    } else {
      krakenTimezones = [];
    }
  } catch (err) {
    krakenTimezones = [];
  }
  renderKrakenTimezoneOptions();
  updateKrakenTimezoneStatus();
}

/** Valide puis applique le fuseau saisi (change/blur) — inconnu refusé. */
function commitKrakenTimezone() {
  const input = document.getElementById('kraken-tz-input');
  if (!input) return;
  const raw = input.value.trim();
  if (!raw || raw.toLowerCase() === 'local') {
    selectKrakenTimezone(null);
    scheduleKrakenApply();
    scheduleKrakenPreview();
    return;
  }
  const entries = krakenTimezoneEntries();
  const match = entries.find((e) => e.key === raw) ||
    entries.find((e) => e.key.toLowerCase() === raw.toLowerCase());
  if (!match) {
    toast('⚠ Fuseau horaire inconnu : ' + raw, 'error');
    input.value = krakenLcdTimezone || '';
    return;
  }
  selectKrakenTimezone(match.key);
  scheduleKrakenApply();
  scheduleKrakenPreview();
}

/** Lignes décoratives du squelette CSS (repli hors-ligne d'une vignette). */
const LCD_THUMB_ROWS = [
  ['CPU', 42, '48°C'],
  ['GPU', 37, '51°C'],
  ['RAM', 58, '9.3 Go'],
  ['VRAM', 44, '3.5 Go'],
  ['DISK', 61, '412 Go'],
];

/**
 * Squelette CSS d'une vignette (chargement / repli hors-ligne de la
 * disposition) — mêmes couleurs que la palette courante.
 * @param {object} pal — palette ({colors}) ou palette de repli
 * @param {number} size — taille finale en px
 * @returns {string} HTML de la vignette
 */
function lcdThumbHTML(pal, size) {
  const c = (pal && pal.colors) || pal || {};
  const bg = c.bg || '#0b0e14';
  const text = c.text || '#c8d2e0';
  const accent = c.accent || '#8a93a6';
  const gaugeBg = c.gauge_bg || '#20242e';
  const gaugeStart = c.gauge_start || '#3a4150';
  const gaugeEnd = c.gauge_end || '#8a93a6';
  const rows = LCD_THUMB_ROWS.map((r) =>
    '<div class="lcd-row">' +
      '<div class="lcd-rowtop"><span class="lcd-label">' + r[0] + '</span>' +
      '<span class="lcd-val">' + r[2] + '</span></div>' +
      '<div class="lcd-gauge"><i style="width:' + r[1] + '%"></i></div>' +
    '</div>').join('');
  return '<div class="lcd" style="--lcd:' + size + 'px;--s:' + (size / 640) +
    ';--tbg:' + bg + ';--ttx:' + text + ';--tac:' + accent +
    ';--tgb:' + gaugeBg + ';--tgs:' + gaugeStart + ';--tge:' + gaugeEnd + '"' +
    ' role="img" aria-label="Aperçu d\'écran Kraken">' +
      '<div class="lcd-inner">' +
        '<div class="lcd-title">SYSTEM MONITOR</div>' +
        '<div class="lcd-time">14:32:07</div>' +
        '<div class="lcd-rows">' + rows + '</div>' +
        '<div class="lcd-liquid"><span>Liquid Temperature</span><b>32.4°C</b></div>' +
      '</div></div>';
}

/** Palette du catalogue par sa clé (repli : la première). */
function krakenPalette(key) {
  return krakenPalettes.find((p) => p.key === key) || krakenPalettes[0] || null;
}

/** URL de la vignette PNG d'une disposition (vrai moteur serveur). */
function layoutThumbURL(key) {
  return '/api/kraken/layouts/' + encodeURIComponent(key) + '/thumb.png';
}

/** Nuancier 6 couleurs d'une palette (bg·texte·accent·jauges). */
function paletteSwatchesHTML(pal) {
  const c = (pal && pal.colors) || {};
  const order = ['bg', 'text', 'accent', 'gauge_bg', 'gauge_start', 'gauge_end'];
  const chips = order.map((k) =>
    '<i style="background:' + escapeHtml(c[k] || '#000') + '"></i>').join('');
  return '<span class="palette-swatches" aria-hidden="true">' + chips + '</span>';
}

/**
 * HTML d'une carte de palette (nuancier + libellé + badge actuelle/nouvelle).
 * @param {object} p — {key, label, subtitle, is_new, colors}
 * @returns {string}
 */
function paletteCardHTML(p) {
  const on = p.key === krakenLcdPalette;
  const label = p.label || p.key;
  const sub = p.subtitle || '';
  const badge = p.is_new ? 'nouvelle' : 'actuelle';
  return '<button type="button" class="theme-card palette-card' + (on ? ' selected' : '') + '"' +
    ' data-palette="' + escapeHtml(p.key) + '" role="radio" aria-checked="' + (on ? 'true' : 'false') + '"' +
    ' title="' + escapeHtml(sub ? label + ' — ' + sub : label) + '">' +
    '<span class="theme-check" aria-hidden="true">✓</span>' +
    '<span class="theme-visual">' + paletteSwatchesHTML(p) + '</span>' +
    '<span class="theme-info"><span class="theme-name">' + escapeHtml(label) + '</span>' +
    '<span class="theme-sub">' + escapeHtml(badge) + '</span></span>' +
  '</button>';
}

/**
 * HTML d'une carte de disposition (mini-écran PNG + libellé + sous-titre).
 * @param {object} l — {key, label, subtitle}
 * @param {boolean} useThumbs
 * @returns {string}
 */
function layoutCardHTML(l, useThumbs) {
  const on = l.key === krakenLcdLayout;
  const label = l.label || l.key;
  const sub = l.subtitle || '';
  const visual = useThumbs
    ? '<img class="layout-thumb" data-layout-key="' + escapeHtml(l.key) + '"' +
      ' src="' + layoutThumbURL(l.key) + '"' +
      ' alt="" loading="lazy" decoding="async" width="140" height="140">'
    : lcdThumbHTML(krakenPalette(krakenLcdPalette), 140);
  return '<button type="button" class="theme-card layout-card' + (on ? ' selected' : '') + '"' +
    ' data-layout="' + escapeHtml(l.key) + '" role="radio" aria-checked="' + (on ? 'true' : 'false') + '"' +
    ' title="' + escapeHtml(sub ? label + ' — ' + sub : label) + '">' +
    '<span class="theme-check" aria-hidden="true">✓</span>' +
    '<span class="theme-visual">' + visual + '</span>' +
    '<span class="theme-info"><span class="theme-name">' + escapeHtml(label) + '</span>' +
    '<span class="theme-sub">' + escapeHtml(sub) + '</span></span>' +
  '</button>';
}

/** Peint le sélecteur de palettes et branche les clics. */
function paintKrakenPaletteGallery() {
  const host = document.getElementById('palette-gallery');
  if (!host) return;
  host.innerHTML = krakenPalettes.map(paletteCardHTML).join('');
  host.querySelectorAll('.palette-card').forEach((card) => {
    card.addEventListener('click', () => {
      selectKrakenPalette(card.dataset.palette);
      // Temps réel : applique au thread serveur + rafraîchit l'aperçu 320 px.
      scheduleKrakenApply();
      scheduleKrakenPreview();
    });
  });
  updateThemeCount();
}

/** Peint le sélecteur de dispositions (vignettes PNG + repli CSS). */
function paintKrakenLayoutGallery(useThumbs) {
  const host = document.getElementById('layout-gallery');
  if (!host) return;
  host.innerHTML = krakenLayouts.map((l) => layoutCardHTML(l, useThumbs)).join('');
  host.querySelectorAll('.layout-card').forEach((card) => {
    card.addEventListener('click', () => {
      selectKrakenLayout(card.dataset.layout);
      scheduleKrakenApply();
      scheduleKrakenPreview();
    });
  });
  // Si une vignette PNG échoue (serveur dégradé), replier sur le squelette CSS.
  host.querySelectorAll('img.layout-thumb').forEach((img) => {
    img.addEventListener('error', () => {
      const holder = document.createElement('span');
      holder.innerHTML = lcdThumbHTML(krakenPalette(krakenLcdPalette), 140);
      if (holder.firstChild) img.replaceWith(holder.firstChild);
    });
  });
  updateThemeCount();
}

/** Construit les deux sélecteurs : repli immédiat, puis catalogues serveur. */
function renderKrakenCatalog() {
  krakenPalettes = LCD_FALLBACK_PALETTES.slice();
  krakenLayouts = LCD_FALLBACK_LAYOUTS.slice();
  paintKrakenPaletteGallery();
  paintKrakenLayoutGallery(false);     // squelette de chargement
  refreshKrakenCatalog();              // asynchrone, remplace par les PNG
}

/** Charge palettes + dispositions depuis le backend (GET /kraken/themes). */
async function refreshKrakenCatalog() {
  if (!document.getElementById('palette-gallery')) return;
  try {
    const data = await apiFetch('/kraken/themes');
    if (!data || !data.ok) return;
    if (Array.isArray(data.palettes) && data.palettes.length) {
      krakenPalettes = data.palettes;
    } else if (Array.isArray(data.themes) && data.themes.length) {
      krakenPalettes = data.themes;   // repli : alias historique
    }
    if (Array.isArray(data.layouts) && data.layouts.length) {
      krakenLayouts = data.layouts;
    }
    // Une clé sélectionnée peut ne pas être dans la liste → repli sur le défaut.
    if (!krakenPalettes.some((p) => p.key === krakenLcdPalette)) {
      krakenLcdPalette = (krakenPalettes.find((p) => p.default) || krakenPalettes[0]).key;
    }
    if (!krakenLayouts.some((l) => l.key === krakenLcdLayout)) {
      krakenLcdLayout = (krakenLayouts.find((l) => l.default) || krakenLayouts[0]).key;
    }
    paintKrakenPaletteGallery();
    paintKrakenLayoutGallery(true);
  } catch (err) {
    // Repli hors-ligne : les squelettes CSS restent affichés.
  }
}

/** Compteur « N palettes · M dispositions » de l'en-tête monitoring. */
function updateThemeCount() {
  const el = document.getElementById('theme-count');
  if (el) {
    el.textContent = krakenPalettes.length + ' palette' +
      (krakenPalettes.length > 1 ? 's' : '') + ' · ' +
      krakenLayouts.length + ' disposition' + (krakenLayouts.length > 1 ? 's' : '');
  }
}

/**
 * Sélectionne une palette LCD (état visuel + clé envoyée au daemon).
 * @param {string} key — clé de palette (catalogue serveur)
 */
function selectKrakenPalette(key) {
  if (!key) return;
  krakenLcdPalette = key;
  document.querySelectorAll('#palette-gallery .palette-card').forEach((c) => {
    const on = c.dataset.palette === key;
    c.classList.toggle('selected', on);
    c.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const keyEl = document.getElementById('palette-key');
  if (keyEl) keyEl.textContent = key;
  scheduleDirtyUpdate();
}

/**
 * Sélectionne une disposition LCD.
 * @param {string} key — clé de disposition (catalogue serveur)
 */
function selectKrakenLayout(key) {
  if (!key) return;
  krakenLcdLayout = key;
  document.querySelectorAll('#layout-gallery .layout-card').forEach((c) => {
    const on = c.dataset.layout === key;
    c.classList.toggle('selected', on);
    c.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const keyEl = document.getElementById('layout-key');
  if (keyEl) keyEl.textContent = key;
  scheduleDirtyUpdate();
}

/**
 * Récupère la configuration actuelle du monitoring.
 * @returns {{palette: string, layout: string, options: string[], timezone: string}}
 */
function getKrakenMonitorConfig() {
  const options = KRAKEN_SENSOR_KEYS.filter((key) => {
    const el = krakenSensorEl(key);
    return !!(el && el.checked);
  });
  return { palette: krakenLcdPalette, layout: krakenLcdLayout, options,
           timezone: krakenLcdTimezone || 'local' };
}

/** Libellé humain d'une cadence (jamais de FPS — contrainte liquidctl). */
function formatInterval(value) {
  if (value === 'asap') return 'le plus souvent possible';
  return 'une image toutes les ' + Math.round(Number(value)) + ' s';
}

/** Met à jour le libellé + l'état du slider d'intervalle (option « asap »). */
function krakenUpdateIntervalLabel() {
  const slider = document.getElementById('kraken-interval');
  const asapEl = document.getElementById('kraken-interval-asap');
  const label = document.getElementById('kraken-interval-value');
  const asap = !!(asapEl && asapEl.checked);
  if (slider) slider.disabled = asap || !krakenControlsEnabled;
  if (asapEl) asapEl.disabled = !krakenControlsEnabled;
  if (label) {
    label.textContent = asap
      ? 'le plus souvent possible'
      : 'une image toutes les ' + (slider ? Math.round(parseFloat(slider.value)) : 10) + ' s';
  }
}

/**
 * Applique EN TEMPS RÉEL les réglages de monitoring (capteurs / thème /
 * intervalle) sans changer le mode d'affichage. Débouncé pour ne pas
 * redémarrer le thread serveur en rafale.
 */
let krakenApplyTimer = null;
function scheduleKrakenApply() {
  if (krakenApplyTimer) clearTimeout(krakenApplyTimer);
  krakenApplyTimer = setTimeout(async () => {
    krakenApplyTimer = null;
    const config = getKrakenMonitorConfig();
    try {
      await apiFetch('/kraken/display/update', {
        method: 'POST',
        body: {
          interval: krakenInterval(),
          palette: config.palette,
          layout: config.layout,
          options: config.options,
          timezone: config.timezone,
        },
      });
    } catch (err) {
      // Silencieux : le prochain start reprendra les valeurs de l'UI.
    }
  }, 400);
}

/** Démarre le mode monitoring. */
async function krakenStartMonitor() {
  const config = getKrakenMonitorConfig();
  try {
    const data = await apiFetch('/kraken/monitor/start', {
      method: 'POST',
      body: {
        interval: krakenInterval(),
        palette: config.palette,
        layout: config.layout,
        options: config.options,
        timezone: config.timezone,
      },
    });
    if (data.ok) {
      krakenDisplayMode = 'monitor';
      toast('🖥 Monitoring démarré (' + config.palette + '/' + config.layout +
        ', ' + formatInterval(krakenInterval()) + ')', 'success');
      refreshDisplayStatus();
      scheduleDirtyUpdate();
    } else {
      toast('⚠ ' + (data.error || 'Échec démarrage monitoring'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Démarre le diaporama gallery. */
async function krakenStartGallery() {
  try {
    const data = await apiFetch('/kraken/gallery/start', {
      method: 'POST',
      body: { interval: krakenInterval() },
    });
    if (data.ok) {
      krakenDisplayMode = 'gallery';
      toast('🎞 Diaporama démarré', 'success');
      refreshDisplayStatus();
      scheduleDirtyUpdate();
    } else {
      toast('⚠ ' + (data.error || 'Échec démarrage gallery'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Arrête le thread d'affichage actif. */
async function krakenStopDisplay(notify = true) {
  try {
    const data = await apiFetch('/kraken/display/stop', { method: 'POST' });
    if (data.ok) {
      krakenDisplayMode = null;
      scheduleDirtyUpdate();
    }
    if (notify && data.ok) toast('⏹ Affichage automatique arrêté', 'info');
    refreshDisplayStatus();
  } catch (err) {
    if (notify) toast('⚠ Erreur arrêt: ' + err.message, 'error');
  }
}

/** Génère l'aperçu 320 px du rendu monitoring. `silent` = sans toast (auto). */
async function krakenPreview(silent = false) {
  const config = getKrakenMonitorConfig();
  try {
    const data = await apiFetch('/kraken/monitor/preview', {
      method: 'POST',
      body: {
        palette: config.palette,
        layout: config.layout,
        options: config.options,
        timezone: config.timezone,
      },
    });
    if (data.ok && data.path) {
      if (!silent) toast('👁 Aperçu généré', 'success');
      loadKrakenPreview();
    } else if (!silent) {
      toast('⚠ ' + (data.error || 'Aperçu impossible'), 'error');
    }
  } catch (err) {
    if (!silent) toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Rafraîchit l'aperçu automatiquement après un changement (débounce 400 ms). */
let krakenPreviewTimer = null;
function scheduleKrakenPreview() {
  if (krakenPreviewTimer) clearTimeout(krakenPreviewTimer);
  krakenPreviewTimer = setTimeout(() => {
    krakenPreviewTimer = null;
    krakenPreview(true);
  }, 400);
}

/** Charge l'aperçu monitoring généré par le serveur. */
async function loadKrakenPreview() {
  const img = document.getElementById('kraken-preview-img');
  if (!img) return;
  img.classList.remove('hidden');
  img.src = '/api/kraken/monitor/preview.png?t=' + Date.now();
}

/** Rafraîchit la liste des fichiers de la gallery. */
async function refreshKrakenGallery() {
  const listEl = document.getElementById('kraken-gallery-list');
  const countEl = document.getElementById('kraken-gallery-count');
  if (!listEl) return;
  try {
    const data = await apiFetch('/kraken/gallery');
    if (!data.ok) {
      listEl.innerHTML = `<div class="kraken-status-error">⚠ ${escapeHtml(data.error || 'Erreur')}</div>`;
      return;
    }
    const files = data.files || [];
    if (countEl) countEl.textContent = files.length + ' fichier(s)';
    if (!files.length) {
      listEl.innerHTML = '<div style="color:var(--text-dim);font-size:0.8rem;">Aucune image — ajoutez-en pour le diaporama.</div>';
      return;
    }
    listEl.innerHTML = files.map((f) => `
      <div class="kraken-gallery-item">
        <span>${f.is_gif ? '🎬' : '🖼'}</span>
        <span class="name">${escapeHtml(f.name)}</span>
        <span class="meta">${(f.size / 1024).toFixed(0)} Ko</span>
        <button class="delete" data-name="${escapeHtml(f.name)}" title="Supprimer">✕</button>
      </div>`).join('');
    listEl.querySelectorAll('.delete').forEach((btn) => {
      btn.addEventListener('click', () => krakenDeleteGalleryItem(btn.dataset.name));
    });
  } catch (err) {
    listEl.innerHTML = `<div class="kraken-status-error">⚠ ${escapeHtml(err.message)}</div>`;
  }
}

/** Ajoute les fichiers sélectionnés à la gallery. */
async function krakenAddGalleryFiles() {
  const input = document.getElementById('kraken-file-gallery');
  const btn = document.getElementById('kraken-btn-gallery-add');
  if (!input || !input.files || !input.files.length) {
    toast('⚠ Sélectionnez un ou plusieurs fichiers', 'info');
    return;
  }
  btn.disabled = true;
  btn.textContent = '⏳ Upload...';
  let added = 0;
  try {
    for (const file of input.files) {
      const data = await fileToBase64(file);
      const result = await apiFetch('/kraken/gallery/add', {
        method: 'POST',
        timeout: 120000,
        body: { data, filename: file.name },
      });
      if (result.ok) added++;
    }
    toast('✅ ' + added + ' fichier(s) ajouté(s) à la gallery', 'success');
    input.value = '';
    refreshKrakenGallery();
    // Annulable : les ajouts sont journalisés côté serveur → dirty.
    await loadPendingChanges();
    refreshDirtyNow();
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '➕ Ajouter';
  }
}

/** Supprime un fichier de la gallery. */
async function krakenDeleteGalleryItem(name) {
  const ok = await window.HolafModal.confirm(
    'Supprimer le fichier',
    'Supprimer "' + name + '" de la gallery ?',
    { danger: true, confirmText: 'Supprimer', cancelText: 'Annuler', theme: MODAL_THEME }
  );
  if (!ok) return;
  try {
    const data = await apiFetch('/kraken/gallery/delete', {
      method: 'POST',
      body: { name },
    });
    if (data.ok) {
      toast('🗑 Fichier supprimé', 'info');
      refreshKrakenGallery();
      // Annulable : le fichier part dans la corbeille serveur → dirty.
      await loadPendingChanges();
      refreshDirtyNow();
    } else {
      toast('⚠ ' + (data.error || 'Suppression impossible'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
}

/** Échappe le HTML pour éviter les injections. */
function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ═══════════════════════════════════════════════════════════
   Moteur « dirty » + barre Enregistrer / Annuler
   ────────────────────────────────────────────────────────────
   RÉFÉRENCE = état figé côté serveur (GET /api/saved) : couleurs
   de base par stick, luminosité, ordre, section `lighting`
   (mode/running/speed/framerate/refresh/params) et réglages
   Kraken (lcd + display). L'écart courant ↔ référence est
   recalculé (throttlé) à chaque mutation et rendu dans le footer.

   Les frames d'animation n'écrivent QUE state.colors (affichage) :
   state.baseColors reste la couche persistable, donc les couleurs
   éditées PENDANT une animation sont comparées normalement et le
   dirty ne devient jamais permanent. Le mode et l'état marche/arrêt
   de l'éclairage font partie de la référence (décision A).
   ═══════════════════════════════════════════════════════════ */

/** Référence normalisée (GET /api/saved), ou null tant que non chargée. */
let savedReference = null;
/** Libellés des modifications non enregistrées. */
let dirtyItems = [];
/** Modifications de FICHIERS encore annulables (GET /api/kraken/pending). */
let pendingChanges = null;
/** false tant que la référence initiale n'a pas été chargée. */
let dirtyReady = false;
let dirtyTimer = null;
const DIRTY_DELAY_MS = 120;

/**
 * Normalise la référence serveur (défauts inclus) pour un diff stable.
 * @param {Object} ref
 * @returns {Object}
 */
function normalizeReference(ref) {
  const colors = {};
  Object.entries((ref && ref.colors) || {}).forEach(([key, leds]) => {
    if (Array.isArray(leds)) {
      colors[key] = leds.map((c) => [
        Number(c && c[0]) || 0,
        Number(c && c[1]) || 0,
        Number(c && c[2]) || 0,
      ]);
    }
  });
  const anim = (ref && ref.animation) || {};
  const light = (ref && ref.lighting) || {};
  const krakenRef = (ref && ref.kraken) || {};
  const lcd = krakenRef.lcd || {};
  const display = krakenRef.display || {};
  // Schéma v4 (`lighting`) avec repli sur le miroir legacy (`animation` :
  // type→mode, enabled→running) pour les références pas encore migrées.
  const refMode = typeof light.mode === 'string' ? light.mode
    : (typeof anim.type === 'string' ? anim.type : 'static');
  const refRunning = light.running !== undefined ? !!light.running
    : !!anim.enabled;
  return {
    colors,
    brightness: Number(ref && ref.brightness !== undefined ? ref.brightness : 255),
    stick_order: Array.isArray(ref && ref.stick_order)
      ? ref.stick_order.map(String) : [],
    lighting: {
      mode: refMode === 'static' ? 'static' : refMode,
      running: refMode !== 'static' && refRunning,
      speed: Number(light.speed !== undefined ? light.speed
        : (anim.speed !== undefined ? anim.speed : 1)),
      framerate: Number(light.framerate !== undefined ? light.framerate
        : (anim.framerate !== undefined ? anim.framerate : 30)),
      refresh: Number(light.refresh !== undefined ? light.refresh
        : (anim.refresh !== undefined ? anim.refresh : 20)),
      params: (light.params && typeof light.params === 'object')
        ? { ...light.params } : {},
    },
    kraken: {
      lcd: {
        brightness: Number(lcd.brightness !== undefined ? lcd.brightness : 80),
        orientation: Number(lcd.orientation !== undefined ? lcd.orientation : 0),
        mode: lcd.mode !== undefined && lcd.mode !== null ? lcd.mode : 'liquid',
      },
      display: {
        mode: display.mode !== undefined ? display.mode : null,
        theme: display.theme || display.palette || 'data_center',
        palette: display.palette || display.theme || 'data_center',
        layout: display.layout || 'classic',
        timezone: normalizeTimezoneKey(display.timezone),
        options: Array.isArray(display.options)
          ? display.options.map(String)
          : ['cpu', 'gpu', 'ram', 'vram', 'disks'],
        interval: display.interval === 'asap'
          ? 'asap'
          : Number(display.interval !== undefined ? display.interval : 10),
      },
    },
  };
}

/** Adopte la luminosité matérielle du premier stick (readback serveur). */
function adoptBrightnessFromSticks() {
  if (!state.sticks.length) return;
  const level = state.sticks[0].brightness;
  if (typeof level === 'number' && level !== state.brightness) {
    state.brightness = level;
    state.emit('brightness');
  }
}

/** Deux triplets RGB identiques ? */
function sameColor(a, b) {
  return a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
}

/** Listes de couleurs identiques (les LED manquantes comptent comme noir) ? */
function sameColorList(a, b) {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const ca = a[i] || [0, 0, 0];
    const cb = b[i] || [0, 0, 0];
    if (!sameColor(ca, cb)) return false;
  }
  return true;
}

/** Clé d'identification d'un stick côté référence : "bus:0xaddr". */
function stickKey(stick) {
  return `${stick.bus_num}:${String(stick.address).toLowerCase()}`;
}

/**
 * Calcule la liste des modifications courantes vs référence.
 * @returns {string[]}
 */
function computeDirtyItems() {
  if (!savedReference || !dirtyReady) return [];
  const ref = savedReference;
  const items = [];

  // 1. Couleurs de BASE (jamais les frames : voir state.baseColors)
  state.sticks.forEach((stick, idx) => {
    const fresh = state.baseColors[stick.id] || state.colors[stick.id];
    const saved = ref.colors[stick.id];
    if (!fresh || !saved) return; // non chargé / hors référence → neutre
    if (!sameColorList(fresh, saved)) {
      items.push(`Couleurs — Barrette #${idx + 1}`);
    }
  });

  // 2. Luminosité (globale)
  if (Number(state.brightness) !== ref.brightness) {
    items.push(`Luminosité : ${ref.brightness} → ${state.brightness}`);
  }

  // 3. Ordre des sticks
  if (ref.stick_order.length && state.sticks.length) {
    const current = state.sticks.map(stickKey).join('|');
    const saved = ref.stick_order.map((k) => String(k).toLowerCase()).join('|');
    if (current !== saved) items.push('Ordre des barrettes');
  }

  // 4. Éclairage : mode, état marche/arrêt, réglages (décision A)
  const refLight = ref.lighting;
  const curLight = state.lighting;
  if (refLight.mode !== curLight.mode) {
    let label = `Éclairage : ${lightingModeLabel(refLight.mode)} → `
      + lightingModeLabel(curLight.mode);
    if (refLight.running !== curLight.running) {
      label += curLight.running ? ' (en marche)' : ' (arrêté)';
    }
    items.push(label);
  } else if (refLight.running !== curLight.running) {
    items.push('Éclairage : ' + (refLight.running ? 'en marche' : 'arrêté')
      + ' → ' + (curLight.running ? 'en marche' : 'arrêté'));
  }
  if (Math.abs(curLight.speed - refLight.speed) > 1e-9) {
    items.push(`Vitesse d'animation : ${refLight.speed}× → ${curLight.speed}×`);
  }
  if (Math.round(curLight.refresh) !== Math.round(refLight.refresh)) {
    items.push(`Refresh SMBus : ${refLight.refresh} → ${curLight.refresh}/s`);
  }
  if (Math.round(curLight.framerate) !== Math.round(refLight.framerate)) {
    items.push(`Cadence d'animation : ${refLight.framerate} → ${curLight.framerate} fps`);
  }
  if (curLight.mode === refLight.mode && curLight.mode !== 'static') {
    const keys = new Set([
      ...Object.keys(refLight.params || {}),
      ...Object.keys(curLight.params || {}),
    ]);
    let paramsDiffer = false;
    keys.forEach((key) => {
      const a = Number(curLight.params ? curLight.params[key] : NaN);
      const b = Number(refLight.params ? refLight.params[key] : NaN);
      if (!(a === b || (Number.isNaN(a) && Number.isNaN(b)))) paramsDiffer = true;
    });
    if (paramsDiffer) items.push(`Réglages de ${lightingModeLabel(curLight.mode)}`);
  }

  // 5. Kraken — écran LCD
  const lcd = ref.kraken.lcd;
  const lcdB = document.getElementById('kraken-brightness');
  if (lcdB && parseInt(lcdB.value, 10) !== lcd.brightness) {
    items.push(`Luminosité LCD : ${lcd.brightness} → ${lcdB.value}`);
  }
  const lcdO = document.getElementById('kraken-orientation');
  if (lcdO && parseInt(lcdO.value, 10) !== lcd.orientation) {
    items.push(`Orientation LCD : ${lcd.orientation}° → ${lcdO.value}°`);
  }
  if (lcd.mode !== krakenLcdMode) {
    items.push(`Mode LCD : ${lcd.mode} → ${krakenLcdMode}`);
  }

  // 6. Kraken — affichage (monitoring / gallery)
  const disp = ref.kraken.display;
  const refPalette = disp.palette || disp.theme;
  const refLayout = disp.layout || 'classic';
  if (refPalette !== krakenLcdPalette) {
    items.push(`Palette LCD : ${refPalette} → ${krakenLcdPalette}`);
  }
  if (refLayout !== krakenLcdLayout) {
    items.push(`Disposition LCD : ${refLayout} → ${krakenLcdLayout}`);
  }
  const refTimezone = normalizeTimezoneKey(disp.timezone);
  if (refTimezone !== krakenLcdTimezone) {
    items.push('Fuseau horaire : ' + (refTimezone || 'local') + ' → ' +
      (krakenLcdTimezone || 'local'));
  }
  const currentOptions = getKrakenMonitorConfig().options.join(',');
  if (currentOptions !== disp.options.join(',')) items.push('Capteurs LCD');
  const currentInterval = krakenInterval();
  if (String(currentInterval) !== String(disp.interval)) {
    items.push(`Fréquence LCD : ${formatInterval(disp.interval)} → ${formatInterval(currentInterval)}`);
  }
  if (disp.mode !== krakenDisplayMode) {
    items.push(`Affichage : ${disp.mode || 'arrêté'} → ${krakenDisplayMode || 'arrêté'}`);
  }

  // 7. Fichiers annulables (image d'écran écrasée / gallery ajoutée ou supprimée)
  if (pendingChanges) {
    const screens = pendingChanges.screens || [];
    if (screens.length) {
      items.push(`Image d'écran : ${screens.length} remplacée${screens.length > 1 ? 's' : ''}`);
    }
    const added = pendingChanges.gallery_added || [];
    if (added.length) {
      items.push(`Gallery : ${added.length} ajout${added.length > 1 ? 's' : ''}`);
    }
    const deleted = pendingChanges.gallery_deleted || [];
    if (deleted.length) {
      items.push(`Gallery : ${deleted.length} suppression${deleted.length > 1 ? 's' : ''}`);
    }
  }

  return items;
}

/** Recalcule le dirty (throttlé) et met à jour la barre d'actions. */
function scheduleDirtyUpdate() {
  if (dirtyTimer) return;
  dirtyTimer = setTimeout(() => {
    dirtyTimer = null;
    dirtyItems = computeDirtyItems();
    renderDirtyUI();
  }, DIRTY_DELAY_MS);
}

/** Recalcule et rend immédiatement (sans attendre le throttle). */
function refreshDirtyNow() {
  if (dirtyTimer) {
    clearTimeout(dirtyTimer);
    dirtyTimer = null;
  }
  dirtyItems = computeDirtyItems();
  renderDirtyUI();
}

/** Rend le badge + l'état disabled des boutons Enregistrer / Annuler. */
function renderDirtyUI() {
  const badge = document.getElementById('dirty-badge');
  const saveBtn = document.getElementById('btn-save');
  const cancelBtn = document.getElementById('btn-cancel');
  const n = dirtyItems.length;
  const plural = n > 1 ? 's' : '';
  if (badge) {
    badge.hidden = n === 0;
    badge.textContent = n === 0 ? '' :
      `${n} modification${plural} non enregistrée${plural}`;
    badge.title = dirtyItems.join('\n');
  }
  if (saveBtn) {
    saveBtn.disabled = n === 0;
    saveBtn.title = n === 0
      ? "Fige l'état courant comme nouvelle référence (config.json)"
      : `Enregistrer : ${dirtyItems.join(' · ')}`;
  }
  if (cancelBtn) {
    cancelBtn.disabled = n === 0;
    cancelBtn.title = n === 0
      ? 'Ré-applique au matériel la dernière configuration enregistrée'
      : `Annuler : ${dirtyItems.join(' · ')}`;
  }
}

/** Libellé lisible d'un mode d'éclairage (registre serveur, repli id). */
function lightingModeLabel(mode) {
  if (!mode || mode === 'static') return 'Statique';
  const effect = state.effects.find((e) => e.id === mode);
  return effect ? effect.label : mode;
}

/** Charge la référence persistée (GET /api/saved).
 * @param {{adoptLighting?: boolean}} [opts] — adoptLighting : recopie la
 *   section lighting de la référence dans l'état courant (boot uniquement,
 *   pour éviter un faux dirty avant le retour de /animation/status).
 */
async function loadSavedReference(opts = {}) {
  try {
    savedReference = normalizeReference(await apiFetch('/saved'));
    if (opts.adoptLighting) adoptReferenceLighting();
  } catch (err) {
    console.warn('[dirty] référence non chargée :', err);
    savedReference = null;
  }
  dirtyReady = true;
  refreshDirtyNow();
}

/** Charge les modifications de fichiers encore annulables (Cancel). */
async function loadPendingChanges() {
  try {
    pendingChanges = await apiFetch('/kraken/pending');
  } catch (err) {
    pendingChanges = null;
  }
}

/** Réhydrate les contrôles Kraken depuis l'état serveur (mémoire). */
async function hydrateKrakenControls() {
  try {
    const lcd = await apiFetch('/kraken/lcd/settings');
    if (lcd) {
      krakenLcdMode = lcd.mode || 'liquid';
      const b = document.getElementById('kraken-brightness');
      if (b && lcd.brightness !== undefined && lcd.brightness !== null) {
        b.value = lcd.brightness;
        const label = document.getElementById('kraken-brightness-value');
        if (label) label.textContent = lcd.brightness;
      }
      const o = document.getElementById('kraken-orientation');
      if (o && lcd.orientation !== undefined && lcd.orientation !== null) {
        o.value = String(lcd.orientation);
      }
    }
  } catch (err) { /* silencieux : contrôles laissés en l'état */ }

  try {
    const st = await apiFetch('/kraken/display/status');
    if (st) {
      krakenDisplayMode = st.mode !== undefined ? st.mode : null;
      if (st.palette || st.theme) {
        selectKrakenPalette(st.palette || st.theme);
      }
      if (st.layout) selectKrakenLayout(st.layout);
      if (st.timezone !== undefined) selectKrakenTimezone(st.timezone);
      if (Array.isArray(st.options)) {
        KRAKEN_SENSOR_KEYS.forEach((key) => {
          const el = krakenSensorEl(key);
          if (el) el.checked = st.options.includes(key);
        });
      }
      if (st.interval !== undefined && st.interval !== null) {
        const asap = st.interval === 'asap';
        const asapEl = document.getElementById('kraken-interval-asap');
        if (asapEl) asapEl.checked = asap;
        const i = document.getElementById('kraken-interval');
        if (i && !asap) i.value = String(st.interval);
        krakenUpdateIntervalLabel();
      }
      const stopBtn = document.getElementById('kraken-btn-stop-display');
      if (stopBtn) stopBtn.disabled = !st.running;
    }
  } catch (err) { /* silencieux */ }
}

/* ═══════════════════════════════════════════════════════════
   Sous-système « lighting » côté front
   ────────────────────────────────────────────────────────────
   state.lighting est le miroir de GET /api/animation/status ;
   le catalogue des modes vient de GET /api/animation/effects
   (aucun mode codé en dur ici). Le backend hot-swappe : les
   boutons de mode ne sont JAMAIS désactivés en marche. Le
   framerate n'est plus codé en dur : il vient du status.
   ═══════════════════════════════════════════════════════════ */

/** Marge de débounce des réglages à chaud (cohérente avec Kraken : 400 ms). */
const ANIM_APPLY_DELAY_MS = 400;

/** Durée de cycle de base (indépendante de la vitesse) : permet d'afficher
    la durée de cycle immédiatement pendant le drag, avant la réponse serveur. */
let animCycleBase = null;
/** Timers de débounce (vitesse/refresh ; paramètres d'effet). */
let animSettingsTimer = null;
let animParamsTimer = null;

/** Mode matrice (vue canvas) — implémentation branchée à l'init DOM. */
let matrixMode = false;
let setMatrixMode = function (on) { matrixMode = !!on; };

/** Applique (partiellement) un payload de statut au miroir local. */
function applyLightingStatus(data) {
  const d = data || {};
  const mode = typeof d.mode === 'string' ? d.mode
    : (typeof d.effect === 'string' && d.effect ? d.effect : state.lighting.mode);
  const running = d.running !== undefined ? !!d.running
    : (mode !== 'static' && state.lighting.running);
  state.lighting = {
    mode: mode,
    running: mode !== 'static' && running,
    speed: d.speed !== undefined && d.speed !== null
      ? Number(d.speed) : state.lighting.speed,
    framerate: d.framerate !== undefined && d.framerate !== null
      ? Number(d.framerate) : state.lighting.framerate,
    refresh: d.refresh !== undefined && d.refresh !== null
      ? Number(d.refresh) : state.lighting.refresh,
    params: (d.params && typeof d.params === 'object')
      ? { ...d.params } : state.lighting.params,
  };
  if (d.cycle_seconds !== undefined) {
    animCycleBase = (d.cycle_seconds === null)
      ? null : Number(d.cycle_seconds) * state.lighting.speed;
  }
}

/** Reflète state.lighting dans tous les contrôles d'animation. */
function refreshAnimationUI() {
  updateAnimationSliders();
  updateAnimationButtons();
  renderAnimationParams();
}

/** Positionne les sliders + libellés + durée de cycle sur l'état courant. */
function updateAnimationSliders() {
  const speed = Number(state.lighting.speed);
  const speedEl = document.getElementById('anim-speed');
  if (speedEl && isFinite(speed)) speedEl.value = String(speed);
  const speedLabel = document.getElementById('anim-speed-value');
  if (speedLabel && isFinite(speed)) speedLabel.textContent = speed.toFixed(1) + '×';
  const refresh = Number(state.lighting.refresh);
  const refreshEl = document.getElementById('anim-refresh');
  if (refreshEl && isFinite(refresh)) refreshEl.value = String(refresh);
  const refreshLabel = document.getElementById('anim-refresh-value');
  if (refreshLabel && isFinite(refresh)) refreshLabel.textContent = Math.round(refresh) + '/s';
  updateAnimationCycleLabel();
}

/** Affiche la durée de cycle réelle (« cycle ≈ 3,3 s ») ou « — ». */
function updateAnimationCycleLabel() {
  const el = document.getElementById('anim-cycle');
  if (!el) return;
  if (!state.lighting.running || animCycleBase === null || !isFinite(animCycleBase)) {
    el.textContent = 'cycle ≈ —';
    return;
  }
  const real = animCycleBase / Math.max(state.lighting.speed, 1e-6);
  el.textContent = 'cycle ≈ ' + real.toFixed(1).replace('.', ',') + ' s';
}

/** Réhydrate l'état lumière complet depuis le serveur + met à jour l'UI.
    (Après un F5 le boot serveur a rechargé `lighting` : pas de faux dirty.) */
async function refreshAnimationState() {
  try {
    const data = await apiFetch('/animation/status');
    applyLightingStatus(data);
    if (state.lighting.running) setMatrixMode(true);
    refreshAnimationUI();
    scheduleDirtyUpdate();
  } catch (err) { /* silencieux : UI laissée dans son état connu */ }
  updateAnimationButtons();
}

/** Répercute l'état sur les boutons de mode et le bouton Stop.
    Les boutons de mode ne sont JAMAIS désactivés (hot-swap backend) :
    le mode courant porte .active + aria-pressed. */
function updateAnimationButtons() {
  const running = !!state.lighting.running;
  const mode = state.lighting.mode;
  document.querySelectorAll('.anim-mode-btn').forEach((btn) => {
    const active = running && btn.dataset.mode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const stopBtn = document.getElementById('btn-anim-stop');
  if (stopBtn) stopBtn.disabled = !running;
}

/** Peuple #anim-modes depuis le catalogue serveur (libellés inclus). */
function renderAnimationModes() {
  const host = document.getElementById('anim-modes');
  const hint = document.getElementById('anim-modes-hint');
  if (!host) return;
  host.innerHTML = '';
  state.effects.forEach((effect) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'anim-mode-' + effect.id;
    btn.className = 'btn btn-primary anim-mode-btn';
    btn.dataset.mode = effect.id;
    btn.setAttribute('aria-pressed', 'false');
    btn.textContent = effect.label;
    btn.title = 'Démarrer / basculer sur « ' + effect.label
      + ' » à chaud (sans arrêter l\'animation en cours)';
    btn.addEventListener('click', () => startAnimation(effect.id));
    host.appendChild(btn);
  });
  if (hint) hint.classList.toggle('hidden', state.effects.length > 0);
  updateAnimationButtons();
}

/** Charge le catalogue d'effets (source unique de l'UI). */
async function loadAnimationEffects() {
  try {
    const list = await apiFetch('/animation/effects');
    if (Array.isArray(list)) state.effects = list;
  } catch (err) {
    console.warn('[animation] catalogue indisponible :', err);
  }
  renderAnimationModes();
}

/** Adopte la section lighting de la référence (boot uniquement) : évite un
    faux dirty le temps que /animation/status réponde. */
function adoptReferenceLighting() {
  if (!savedReference) return;
  state.lighting = {
    ...savedReference.lighting,
    params: { ...savedReference.lighting.params },
  };
  animCycleBase = null;
  refreshAnimationUI();
}

/** Miroir d'affichage au repos : les frames ne polluent pas baseColors. */
function syncDisplayToBaseColors() {
  let changed = false;
  for (const stick of state.sticks) {
    const base = state.baseColors[stick.id];
    if (base) {
      state.colors[stick.id] = base.map((c) => c.slice());
      changed = true;
    } else {
      fetchStickInfo(stick.id);
    }
  }
  if (changed) state.emit('colors');
}

/** Applique À CHAUD vitesse + refresh (POST /api/animation/update), débouncé. */
function scheduleAnimationSettings() {
  if (animSettingsTimer) clearTimeout(animSettingsTimer);
  animSettingsTimer = setTimeout(flushAnimationSettings, ANIM_APPLY_DELAY_MS);
}

async function flushAnimationSettings() {
  if (animSettingsTimer) {
    clearTimeout(animSettingsTimer);
    animSettingsTimer = null;
  }
  try {
    const data = await apiFetch('/animation/update', {
      method: 'POST',
      body: { speed: state.lighting.speed, refresh: state.lighting.refresh },
    });
    applyLightingStatus(data);
    refreshAnimationUI();
    scheduleDirtyUpdate();
  } catch (err) {
    // Silencieux : le prochain /start ou /update reprendra les valeurs de l'UI.
  }
}

/** Applique les paramètres d'effet (hot-swap /start, phase réinitialisée). */
function scheduleAnimationParams() {
  if (animParamsTimer) clearTimeout(animParamsTimer);
  animParamsTimer = setTimeout(flushAnimationParams, ANIM_APPLY_DELAY_MS);
}

async function flushAnimationParams() {
  if (animParamsTimer) {
    clearTimeout(animParamsTimer);
    animParamsTimer = null;
  }
  if (!state.lighting.running || state.lighting.mode === 'static') return;
  try {
    const data = await apiFetch('/animation/start', {
      method: 'POST',
      body: {
        mode: state.lighting.mode,
        params: state.lighting.params,
        speed: state.lighting.speed,
        framerate: state.lighting.framerate,
        refresh: state.lighting.refresh,
      },
    });
    applyLightingStatus(data);
    refreshAnimationUI();
    scheduleDirtyUpdate();
  } catch (err) {
    handleAnimationError(err);
  }
}

/** Toast clair pour une erreur 400 (mode inconnu : état inchangé). */
function handleAnimationError(err) {
  if (err && err.status === 400) {
    const detail = (err.data && err.data.detail) || err.message || 'mode inconnu';
    toast('⚠ Mode d\'animation refusé : ' + detail, 'error');
    refreshAnimationState(); // l'état serveur n'a pas bougé → resynchroniser
  } else {
    toast('⚠ Erreur animation : ' + (err && err.message ? err.message : err), 'error');
  }
}

/** Démarre — ou BASCULE À CHAUD vers — un mode (POST /api/animation/start).
    Aucun Stop préalable : le backend hot-swappe. 400 → état inchangé. */
async function startAnimation(mode) {
  const effect = state.effects.find((e) => e.id === mode);
  const label = effect ? effect.label : mode;
  // Un /start annule les applications débouncées en attente (pas de course).
  if (animSettingsTimer) { clearTimeout(animSettingsTimer); animSettingsTimer = null; }
  if (animParamsTimer) { clearTimeout(animParamsTimer); animParamsTimer = null; }
  try {
    const data = await apiFetch('/animation/start', {
      method: 'POST',
      body: {
        mode: mode,
        params: state.lighting.params,
        speed: state.lighting.speed,
        framerate: state.lighting.framerate,
        refresh: state.lighting.refresh,
      },
    });
    // Les animations se regardent en vue matrice (visuel canvas uniquement).
    setMatrixMode(true);
    applyLightingStatus(data);
    refreshAnimationUI();
    scheduleDirtyUpdate();
    toast('🎬 ' + label + ' ' + (data.running ? 'en marche' : 'appliqué'), 'success');
  } catch (err) {
    handleAnimationError(err);
  }
}

/** Arrête réellement l'animation (retour aux couleurs de base). */
async function stopAnimation() {
  try {
    const data = await apiFetch('/animation/stop', { method: 'POST' });
    applyLightingStatus({ ...data, running: false, mode: 'static' });
    syncDisplayToBaseColors();
    refreshAnimationUI();
    scheduleDirtyUpdate();
    toast('⏹ Animation arrêtée', 'success');
  } catch (err) {
    handleAnimationError(err);
  }
}

/** Effet courant du registre (null si statique/inconnu). */
function currentEffect() {
  if (!state.lighting.mode || state.lighting.mode === 'static') return null;
  return state.effects.find((e) => e.id === state.lighting.mode) || null;
}

/** Rend les paramètres de l'effet courant (spec générique du registre :
    type float → slider borné, type int → input number). */
function renderAnimationParams() {
  const wrap = document.getElementById('anim-params-wrap');
  const host = document.getElementById('anim-params');
  if (!wrap || !host) return;
  const effect = currentEffect();
  const specs = effect && Array.isArray(effect.params) ? effect.params : [];
  if (!specs.length) {
    wrap.hidden = true;
    host.innerHTML = '';
    return;
  }
  wrap.hidden = false;
  // Même effet déjà rendu : mettre à jour en place (ne pas remplacer les
  // nœuds pendant qu'un slider est manipulé).
  const sameStructure = host.dataset.effect === effect.id
    && host.querySelectorAll('input[data-param-id]').length === specs.length;
  if (sameStructure) {
    specs.forEach((spec) => {
      const input = host.querySelector('input[data-param-id="'
        + CSS.escape(spec.id) + '"]');
      if (!input || input === document.activeElement) return;
      const current = state.lighting.params[spec.id];
      const next = String(current !== undefined ? current : spec.default);
      if (input.value !== next) input.value = next;
      const valueEl = input.parentElement
        ? input.parentElement.querySelector('b') : null;
      if (valueEl) valueEl.textContent = formatParamValue(spec, current);
    });
    return;
  }
  host.dataset.effect = effect.id;
  host.innerHTML = '';
  specs.forEach((spec) => {
    const row = document.createElement('div');
    row.className = 'slider-row';
    const label = document.createElement('label');
    const name = document.createElement('span');
    name.textContent = spec.label || spec.id;
    const value = document.createElement('b');
    value.textContent = formatParamValue(spec, state.lighting.params[spec.id]);
    label.appendChild(name);
    label.appendChild(value);
    const input = document.createElement('input');
    input.dataset.paramId = spec.id;
    if (spec.type === 'int') {
      input.type = 'number';
      input.step = '1';
    } else {
      input.type = 'range';
      input.step = (Number(spec.max) - Number(spec.min) <= 2) ? '0.01' : '0.1';
    }
    if (spec.min !== undefined) input.min = String(spec.min);
    if (spec.max !== undefined) input.max = String(spec.max);
    const current = state.lighting.params[spec.id];
    input.value = String(current !== undefined ? current : spec.default);
    input.addEventListener('input', () => onAnimationParamInput(spec, input, value));
    // Relâcher le slider applique immédiatement (avant un éventuel Save).
    input.addEventListener('change', flushAnimationParams);
    row.appendChild(label);
    row.appendChild(input);
    host.appendChild(row);
  });
}

/** Valeur formatée d'un paramètre (2 décimales si plage étroite). */
function formatParamValue(spec, value) {
  const v = value !== undefined && value !== null ? value : spec.default;
  if (spec.type === 'int') return String(v);
  const num = Number(v);
  if (!isFinite(num)) return String(v);
  return (Number(spec.max) - Number(spec.min) <= 2)
    ? num.toFixed(2) : num.toFixed(1);
}

function onAnimationParamInput(spec, input, valueEl) {
  let v = spec.type === 'int' ? parseInt(input.value, 10) : parseFloat(input.value);
  if (!isFinite(v)) v = spec.default;
  if (spec.min !== undefined) v = Math.max(Number(spec.min), v);
  if (spec.max !== undefined) v = Math.min(Number(spec.max), v);
  state.lighting.params[spec.id] = v;
  valueEl.textContent = formatParamValue(spec, v);
  scheduleDirtyUpdate();
  scheduleAnimationParams();
}

/** Ré-hydratation complète après Cancel (ou resync multi-onglet) :
 * sticks + couleurs + luminosité, animation, réglages Kraken, fichiers. */
async function rehydrateAfterRestore() {
  try {
    const status = await apiFetch('/status');
    state.sticks = (status.sticks || []).map((s) => ({
      ...s,
      brightness: s.brightness ?? 255,
    }));
  } catch (err) { /* silencieux */ }
  for (const stick of state.sticks) {
    await fetchStickInfo(stick.id);
  }
  // Les ids peuvent être réindexés côté serveur (ordre) : ne pas garder
  // une sélection pointant vers un stick disparu.
  const ids = new Set(state.sticks.map((s) => s.id));
  if (state.selected && !ids.has(state.selected.stick_id)) state.selected = null;
  if (state.selected_stick && !ids.has(state.selected_stick)) state.selected_stick = null;
  adoptBrightnessFromSticks();
  state.notify();
  await refreshAnimationState();
  await hydrateKrakenControls();
  await loadPendingChanges();
  // Les fichiers ont pu changer (image restaurée / gallery ré-apparue).
  refreshKrakenGallery();
}

/** Enregistrer → POST /api/save (fige la référence, purge les sauvegardes). */
async function saveSettings() {
  const btn = document.getElementById('btn-save');
  const cancelBtn = document.getElementById('btn-cancel');
  if (!btn || btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  if (cancelBtn) cancelBtn.disabled = true;
  btn.textContent = '⏳ Enregistrement...';

  // L'ordre drag & drop vit côté front : on le transmet pour qu'il soit figé.
  const currentOrder = state.sticks.map(stickKey);
  const orderChanged = !!(savedReference && savedReference.stick_order.length &&
    currentOrder.join('|') !==
    savedReference.stick_order.map((k) => String(k).toLowerCase()).join('|'));

  try {
    const data = await apiFetch('/save', {
      method: 'POST',
      body: { stick_order: currentOrder },
    });
    savedReference = normalizeReference(
      data && data.reference ? data.reference : await apiFetch('/saved')
    );
    if (orderChanged) {
      // Les ids stick_i sont réindexés côté serveur : réaligner le front
      // (mêmes ids des deux côtés, donc pas de faux dirty couleurs).
      await rehydrateAfterRestore();
    }
    await loadPendingChanges();
    refreshDirtyNow();
    toast('💾 Réglages enregistrés', 'success');
  } catch (err) {
    toast('⚠ Enregistrement impossible : ' + err.message, 'error');
  } finally {
    btn.textContent = label;
    renderDirtyUI();
  }
}

/** Annuler → POST /api/restore puis ré-hydratation complète des contrôles. */
async function cancelSettings() {
  const btn = document.getElementById('btn-cancel');
  const saveBtn = document.getElementById('btn-save');
  if (!btn || btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  if (saveBtn) saveBtn.disabled = true;
  btn.textContent = '⏳ Annulation...';
  try {
    const data = await apiFetch('/restore', { method: 'POST' });
    savedReference = normalizeReference(
      data && data.reference ? data.reference : await apiFetch('/saved')
    );
    await rehydrateAfterRestore();
    refreshDirtyNow();
    toast('↩ Modifications annulées', 'info');
  } catch (err) {
    toast('⚠ Annulation impossible : ' + err.message, 'error');
  } finally {
    btn.textContent = label;
    renderDirtyUI();
  }
}

// Avertissement navigateur si des modifications ne sont pas enregistrées.
window.addEventListener('beforeunload', (event) => {
  if (computeDirtyItems().length > 0) {
    event.preventDefault();
    event.returnValue = '';
    return '';
  }
  return undefined;
});

/* ═══════════════════════════════════════════════════════════
   Initialisation
   ═══════════════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', async () => {
  console.log('🚀 Ballistix RGB Controller — Initialisation');

  // ── Éléments DOM ────────────────────────────────────
  const canvasEl = document.getElementById('led-canvas');
  const resetAllBtn = document.getElementById('btn-reset-all');
  const rescanBtn = document.getElementById('btn-rescan');
  const saveBtn = document.getElementById('btn-save');
  const cancelBtn = document.getElementById('btn-cancel');

  // ── Moteur dirty : recalcul à chaque mutation d'état ──
  state.on('colors', scheduleDirtyUpdate);
  state.on('sticks', scheduleDirtyUpdate);
  state.on('brightness', scheduleDirtyUpdate);
  state.on('*', scheduleDirtyUpdate);

  // ── Canvas ──────────────────────────────────────────
  const ledCanvas = new LedCanvas(canvasEl);
  ledCanvas.onLedClick = (stickId, ledIdx) => {
    state.selected = { stick_id: stickId, led_idx: ledIdx };
    state.selected_stick = null;
    state.emit('selected');
    state.emit('selected_stick');
  };

  // Re-rendre le canvas à chaque changement d'état
  function renderCanvas() {
    ledCanvas.render(state);
  }

  state.on('sticks', renderCanvas);
  state.on('colors', renderCanvas);
  state.on('selected', renderCanvas);

  // ── Color Controls ─────────────────────────────────
  initColorControls(state, sendWS, toast);

  // ── Drag & Drop ────────────────────────────────────
  initDnD(state, (stickId, ledIdx) => {
    state.selected = { stick_id: stickId, led_idx: ledIdx };
    state.selected_stick = null;
    state.emit('selected');
    state.emit('selected_stick');
  });

  // ── Mode classique / matrice ──
  // Bascule purement visuelle du canvas ; l'état est resynchronisé au boot
  // (si une animation est reprise, refreshAnimationState repasse en matrice).
  setMatrixMode = function (on) {
    matrixMode = !!on;
    const btn = document.getElementById('btn-toggle-mode');
    const indicator = document.getElementById('mode-indicator');
    if (btn) btn.textContent = matrixMode ? '⊞' : '⊟';
    if (indicator) {
      indicator.textContent = matrixMode ? 'Mode matrice' : 'Mode classique';
      indicator.className = matrixMode ? 'badge matrix' : 'badge';
    }
    if (canvasEl) canvasEl.style.cursor = matrixMode ? 'default' : 'pointer';
    ledCanvas.setOrientation(matrixMode ? 'matrix'
      : (state.lastOrientation || 'vertical'));
  };

  document.getElementById('btn-toggle-mode').addEventListener('click', () => {
    const next = !matrixMode;
    setMatrixMode(next);
    if (!next) {
      // Mode matrice OFF : l'éclairage repasse RÉELLEMENT au repos (état + UI).
      if (state.lighting.running) {
        stopAnimation();
      } else {
        updateAnimationButtons();
        scheduleDirtyUpdate();
      }
    }
  });

  // ── Animation ──
  // Les boutons de mode viennent de GET /api/animation/effects
  // (renderAnimationModes) : plus aucun mode codé en dur. Le bouton Stop
  // reste disponible et n'est actif que moteur en marche.
  document.getElementById('btn-anim-stop').addEventListener('click', stopAnimation);

  // ── Vitesse / refresh : pilotage EN TEMPS RÉEL via /animation/update ──
  const animSpeedEl = document.getElementById('anim-speed');
  if (animSpeedEl) {
    animSpeedEl.addEventListener('input', (e) => {
      const speed = parseFloat(e.target.value);
      if (!isFinite(speed)) return;
      state.lighting.speed = speed;
      document.getElementById('anim-speed-value').textContent = speed.toFixed(1) + '×';
      updateAnimationCycleLabel();  // estimation immédiate (base / vitesse)
      scheduleDirtyUpdate();
      scheduleAnimationSettings();
    });
    // Relâcher le slider applique immédiatement (avant un éventuel Save).
    animSpeedEl.addEventListener('change', flushAnimationSettings);
  }

  const animRefreshEl = document.getElementById('anim-refresh');
  if (animRefreshEl) {
    animRefreshEl.addEventListener('input', (e) => {
      const rate = parseInt(e.target.value, 10);
      if (!isFinite(rate)) return;
      state.lighting.refresh = rate;
      document.getElementById('anim-refresh-value').textContent = rate + '/s';
      scheduleDirtyUpdate();
      scheduleAnimationSettings();
    });
    animRefreshEl.addEventListener('change', flushAnimationSettings);
  }

  // ── Bouton bascule orientation ─────────────────
  const toggleOrientationBtn = document.getElementById('toggle-orientation');
  toggleOrientationBtn.addEventListener('click', () => {
    const newOrientation = ledCanvas.orientation === 'vertical' ? 'horizontal' : 'vertical';
    state.lastOrientation = newOrientation;
    ledCanvas.setOrientation(newOrientation);
    toggleOrientationBtn.textContent = newOrientation === 'vertical' ? '↔' : '↕';
    toggleOrientationBtn.title = newOrientation === 'vertical'
      ? 'Basculer en mode horizontal'
      : 'Basculer en mode vertical';
  });

  // ── Boutons ────────────────────────────────────────
  // Save / Cancel : seul Enregistrer écrit config.json (plus d'auto-save).
  if (saveBtn) saveBtn.addEventListener('click', saveSettings);
  if (cancelBtn) cancelBtn.addEventListener('click', cancelSettings);

  resetAllBtn.addEventListener('click', async () => {
    resetAllBtn.disabled = true;
    try {
      for (const stick of state.sticks) {
        const black = Array(stick.num_leds).fill(null).map(() => [0, 0, 0]);
        state.baseColors[stick.id] = black;
        state.colors[stick.id] = black.map((c) => c.slice());
        if (ws && ws.readyState === WebSocket.OPEN) {
          sendWS({
            type: 'set_all_leds',
            stick_id: stick.id,
            color: [0, 0, 0],
          });
        } else {
          await apiPutColors(stick.id, black);
        }
      }
      state.notify();
      toast('🔴 Toutes les LEDs éteintes', 'info');
    } catch (err) {
      toast('❌ Erreur reset: ' + err.message, 'error');
    } finally {
      resetAllBtn.disabled = false;
    }
  });

  rescanBtn.addEventListener('click', async () => {
    rescanBtn.disabled = true;
    rescanBtn.textContent = '⏳ Scan...';
    try {
      if (ws && ws.readyState === WebSocket.OPEN) {
        sendWS({ type: 'ping' });
        // Le rescan sera suivi d'un message 'rescan' via WS
        await apiRescan();
      } else {
        const result = await apiRescan();
        if (result && result.sticks) {
          state.sticks = result.sticks.map((s) => ({
            ...s,
            brightness: s.brightness ?? 255,
          }));
          state.colors = {};
          state.baseColors = {};
          state.selected = null;
          state.selected_stick = null;
          state.notify();
          toast('🔍 Scan terminé — ' + state.sticks.length + ' stick(s)', 'success');
        }
      }
    } catch (err) {
      toast('❌ Erreur rescan: ' + err.message, 'error');
    } finally {
      rescanBtn.disabled = false;
      rescanBtn.textContent = '🔄 Re-scan';
    }
  });

  // ── Onglets ────────────────────────────────────────────
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach((b) => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + target));
      if (target === 'kraken') refreshKraken();
    });
  });

  // ── Contrôles Kraken ───────────────────────────────────
  const krakenBtnLiquid = document.getElementById('kraken-btn-liquid');
  if (krakenBtnLiquid) krakenBtnLiquid.addEventListener('click', krakenSetLiquid);

  const krakenBtnMonitor = document.getElementById('kraken-btn-monitor');
  if (krakenBtnMonitor) krakenBtnMonitor.addEventListener('click', krakenStartMonitor);

  const krakenBtnGallery = document.getElementById('kraken-btn-gallery');
  if (krakenBtnGallery) krakenBtnGallery.addEventListener('click', krakenStartGallery);

  const krakenBtnStopDisplay = document.getElementById('kraken-btn-stop-display');
  if (krakenBtnStopDisplay) krakenBtnStopDisplay.addEventListener('click', () => krakenStopDisplay(true));

  const krakenInterval = document.getElementById('kraken-interval');
  if (krakenInterval) {
    krakenInterval.addEventListener('input', () => {
      krakenUpdateIntervalLabel();
      scheduleDirtyUpdate();
      scheduleKrakenApply();   // temps réel : redémarre le thread (débouncé)
    });
  }

  const krakenIntervalAsap = document.getElementById('kraken-interval-asap');
  if (krakenIntervalAsap) {
    krakenIntervalAsap.addEventListener('change', () => {
      krakenUpdateIntervalLabel();
      scheduleDirtyUpdate();
      scheduleKrakenApply();
    });
  }

  const krakenBtnPreview = document.getElementById('kraken-btn-preview');
  if (krakenBtnPreview) krakenBtnPreview.addEventListener('click', () => krakenPreview());

  const krakenBtnGalleryAdd = document.getElementById('kraken-btn-gallery-add');
  if (krakenBtnGalleryAdd) krakenBtnGalleryAdd.addEventListener('click', krakenAddGalleryFiles);

  const krakenBtnImage = document.getElementById('kraken-btn-image');
  if (krakenBtnImage) krakenBtnImage.addEventListener('click', () => krakenUploadImage(false));

  const krakenBtnGif = document.getElementById('kraken-btn-gif');
  if (krakenBtnGif) krakenBtnGif.addEventListener('click', () => krakenUploadImage(true));

  const krakenBrightness = document.getElementById('kraken-brightness');
  if (krakenBrightness) {
    krakenBrightness.addEventListener('input', (e) => {
      krakenSetBrightness(e.target.value);
      scheduleDirtyUpdate();
    });
  }

  const krakenOrientation = document.getElementById('kraken-orientation');
  if (krakenOrientation) {
    krakenOrientation.addEventListener('change', (e) => {
      krakenSetOrientation(e.target.value);
      scheduleDirtyUpdate();
    });
  }

  // Capteurs monitoring : toute coche/décoche est appliquée EN TEMPS RÉEL
  // (redémarrage débouncé du thread côté serveur) et entre dans le dirty.
  KRAKEN_SENSOR_KEYS.forEach((key) => {
    const el = krakenSensorEl(key);
    if (el) el.addEventListener('change', () => {
      scheduleDirtyUpdate();
      scheduleKrakenApply();
    });
  });

  const krakenBtnInit = document.getElementById('kraken-btn-init');
  if (krakenBtnInit) krakenBtnInit.addEventListener('click', krakenInitialize);

  // Fuseau horaire : validation contre le catalogue au commit (change),
  // application temps réel débouncée + aperçu comme les autres réglages.
  const krakenTzInput = document.getElementById('kraken-tz-input');
  if (krakenTzInput) {
    krakenTzInput.addEventListener('change', commitKrakenTimezone);
    krakenTzInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); krakenTzInput.blur(); }
    });
  }
  const krakenTzReset = document.getElementById('kraken-tz-reset');
  if (krakenTzReset) {
    krakenTzReset.addEventListener('click', () => {
      selectKrakenTimezone(null);
      scheduleKrakenApply();
      scheduleKrakenPreview();
    });
  }

  // Sélecteurs Palette × Disposition (remplacent l'ancienne galerie de
  // thèmes) — rendus avant le premier démarrage monitoring pour que l'UI
  // reflète la sélection courante.
  renderKrakenCatalog();
  refreshKrakenTimezones();

  // Vérification silencieuse du Kraken au chargement (ne bloque pas l'init)
  refreshKraken();
  refreshKrakenGallery();
  refreshDisplayStatus();

  // ── Connexion (WebSocket ou REST selon disponibilité) ─
  updateConnectionStatus(false);
  await initConnection();

  // ── Référence persistée + hydratation des contrôles ─
  // (l'ordre compte : la référence d'abord — avec adoption de `lighting`
  // pour éviter un faux dirty au boot — puis le catalogue d'effets, puis
  // l'état courant serveur, pour que le dirty initial compare des valeurs
  // réelles ; le boot serveur a déjà rechargé `lighting`.)
  await loadSavedReference({ adoptLighting: true });
  await loadAnimationEffects();
  await hydrateKrakenControls();
  krakenUpdateIntervalLabel();
  await loadPendingChanges();
  await refreshAnimationState();
  refreshDirtyNow();

  console.log('✅ Ballistix RGB Controller prêt');
});

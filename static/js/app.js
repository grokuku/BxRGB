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
        const colors = state.colors[msg.stick_id] || [];
        colors[msg.led_idx] = msg.color;
        state.colors[msg.stick_id] = colors;
        await apiPutColors(msg.stick_id, colors);
        state.notify();
        break;
      }
      case 'set_all_leds': {
        const stick = state.sticks.find(s => s.id === msg.stick_id);
        if (!stick) return;
        const colors = Array(stick.num_leds).fill(null).map(() => msg.color);
        state.colors[msg.stick_id] = colors;
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
      state.notify();
      console.log(`📦 Status reçu : ${state.sticks.length} stick(s)`);
      break;
    }

    case 'color_applied': {
      if (data.leds && data.stick_id) {
        state.colors[data.stick_id] = data.leds;
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
      
      // Nettoyer les couleurs des sticks disparus
      const newIds = new Set(state.sticks.map(s => s.id));
      for (const sid of Object.keys(state.colors)) {
        if (!newIds.has(sid)) {
          delete state.colors[sid];
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
      // Bug 1 : Mettre à jour le canvas avec les couleurs de l'animation
      if (data.colors) {
        for (const [stickId, leds] of Object.entries(data.colors)) {
          state.colors[stickId] = leds;
        }
        state.emit('colors');
      }
      break;

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
      state.colors[stickId] = data.colors;
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
   Auto-Update (brique HolafModal)
   ═══════════════════════════════════════════════════════════ */

/**
 * Tente de se reconnecter au serveur après une mise à jour.
 * Met à jour le message du bus modal puis recharge la page quand la
 * connexion est rétablie.
 * @param {{set:Function,close:Function}|null} busy - contrôleur HolafModal.busy
 * @param {number} maxAttempts - nombre max de tentatives
 * @param {number} interval - ms entre les tentatives
 */
function reconnectAfterUpdate(busy = null, maxAttempts = 30, interval = 2000) {
  let attempts = 0;

  async function tryReconnect() {
    attempts++;
    console.log(`🔁 Tentative de reconnexion ${attempts}/${maxAttempts}...`);
    try {
      // retry:null → échec rapide, la boucle gère les tentatives.
      await apiFetch('/status', { timeout: 3000, retry: null });
      console.log('✅ Serveur de nouveau accessible ! Rechargement...');
      if (busy) busy.set('Serveur reconnecté ! Rechargement de la page...');
      setTimeout(() => window.location.reload(), 1500);
    } catch (_) {
      if (attempts < maxAttempts) {
        setTimeout(tryReconnect, interval);
      } else if (busy) {
        busy.close();
        window.HolafModal.alert(
          '⬇ Mise à jour',
          'Le serveur ne répond pas après plusieurs tentatives. Rechargez la page manuellement.',
          { theme: MODAL_THEME }
        );
      }
    }
  }

  setTimeout(tryReconnect, interval);
}

/**
 * Lance le processus de mise à jour complet (vérification, confirmation,
 * installation, reconnexion) via HolafModal.busy / confirm / alert.
 */
async function performUpdate() {
  // ── Étape 1 : vérification de la version ──
  const busy = window.HolafModal.busy('Vérification de la version...');

  let checkData;
  try {
    checkData = await apiFetch('/update/check', { timeout: 15000 });
  } catch (err) {
    busy.close();
    await window.HolafModal.alert(
      '⬇ Mise à jour',
      'Échec de la vérification : ' + err.message,
      { theme: MODAL_THEME }
    );
    return;
  }
  busy.close();

  if (!checkData.update_available) {
    toast('✅ Déjà à jour (v' + checkData.current + ')', 'success');
    return;
  }

  // ── Demande de confirmation ──
  const confirmed = await window.HolafModal.confirm(
    '⬇ Mise à jour disponible',
    `Version actuelle : v${checkData.current} — Nouvelle version : v${checkData.latest}. ` +
      `Le service va être redémarré. Continuer ?`,
    { confirmText: 'Mettre à jour', cancelText: 'Annuler', theme: MODAL_THEME }
  );
  if (!confirmed) {
    toast('Mise à jour annulée', 'info');
    return;
  }

  // ── Étape 2 : téléchargement + installation ──
  const installing = window.HolafModal.busy('Téléchargement de la mise à jour...');

  let updateData;
  try {
    // retry:null + timeout large : opération non idempotente, upload potentiel.
    updateData = await apiFetch('/update', { method: 'POST', timeout: 120000, retry: null });
  } catch (err) {
    installing.close();
    await window.HolafModal.alert(
      '⬇ Mise à jour',
      'Échec de la mise à jour : ' + err.message,
      { theme: MODAL_THEME }
    );
    return;
  }

  // ── Cas : déjà à jour (le serveur peut le détecter aussi) ──
  if (updateData.status === 'up-to-date') {
    installing.close();
    toast('✅ Déjà à jour (v' + updateData.current + ')', 'success');
    return;
  }

  // ── Étape 3 : installation réussie, redémarrage ──
  installing.set('Installation du binaire...');
  installing.set('Redémarrage du service...');

  // ── Étape 4 : reconnexion ──
  installing.set('Reconnexion au serveur...');
  toast('✅ Mise à jour installée. Reconnexion...', 'success');
  reconnectAfterUpdate(installing);
}

/* ═══════════════════════════════════════════════════════════
   Kraken NZXT
   ═══════════════════════════════════════════════════════════ */

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
  ['kraken-btn-liquid', 'kraken-btn-monitor', 'kraken-btn-gallery', 'kraken-btn-stop-display',
   'kraken-btn-image', 'kraken-btn-gif', 'kraken-file-image', 'kraken-file-gif',
   'kraken-file-gallery', 'kraken-btn-gallery-add', 'kraken-btn-preview',
   'kraken-brightness', 'kraken-orientation', 'kraken-interval'].forEach(id => {
    const el = document.getElementById(id);
    if (el && id !== 'kraken-btn-stop-display') el.disabled = !enabled;
  });
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
    if (data.ok) toast('🌡 Température liquide affichée', 'success');
    else toast('⚠ ' + (data.error || 'Erreur mode LCD'), 'error');
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
    if (result.ok) toast((isGif ? '🎬 GIF' : '🖼 Image') + ' affiché sur l\'écran', 'success');
    else toast('⚠ ' + (result.error || 'Erreur envoi image'), 'error');
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

/** Lit l'intervalle sélectionné (secondes). */
function krakenInterval() {
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

/* ── Thèmes d'écran LCD : galerie de vignettes (remplace l'ancien dropdown) ──
   Valeurs EXACTES de ballistix/monitor.py:THEMES (rendu PIL 640×640).
   La sélection n'est PAS un thème d'interface : elle part dans la config
   monitoring (POST /api/kraken/monitor/start, champ `theme`). */

/** Les 3 thèmes réels du daemon (ordre historique, data_center par défaut). */
const LCD_THEMES = [
  { key: 'data_center', label: 'Data Center', sub: 'Bleu Technique', def: true,
    bg: '#050f19', text: '#c8e6ff', accent: '#00a0ff',
    gaugeBg: '#0a1e32', gaugeStart: '#003c78', gaugeEnd: '#00b4ff' },
  { key: 'overclock', label: 'Overclock', sub: 'Rouge Agressif',
    bg: '#0f0505', text: '#f0f0f0', accent: '#ff0000',
    gaugeBg: '#2d0a0a', gaugeStart: '#960000', gaugeEnd: '#ff2828' },
  { key: 'fluid_flow', label: 'Fluid Flow', sub: 'Bleu Pastel',
    bg: '#19232d', text: '#e6f5ff', accent: '#78d2ff',
    gaugeBg: '#32465a', gaugeStart: '#a0d2ff', gaugeEnd: '#c8e6ff' },
];

/** Thème LCD sélectionné — source unique lue par getKrakenMonitorConfig(). */
let krakenLcdTheme = 'data_center';

/** Lignes décoratives de la vignette (forme du rendu PIL : titre, heure, jauges). */
const LCD_THUMB_ROWS = [
  ['CPU', 42, '48°C'],
  ['GPU', 37, '51°C'],
  ['RAM', 58, '9.3 Go'],
  ['VRAM', 44, '3.5 Go'],
  ['DISK', 61, '412 Go'],
];

/**
 * Écran LCD simulé (vignette) — même géométrie que le rendu PIL, mis à
 * l'échelle par --s pour rester net à la taille demandée.
 * @param {string} key — clé du thème LCD
 * @param {number} size — taille finale en px
 * @returns {string} HTML de la vignette
 */
function lcdThumbHTML(key, size) {
  const t = LCD_THEMES.find((x) => x.key === key) || LCD_THEMES[0];
  const rows = LCD_THUMB_ROWS.map((r) =>
    '<div class="lcd-row">' +
      '<div class="lcd-rowtop"><span class="lcd-label">' + r[0] + '</span>' +
      '<span class="lcd-val">' + r[2] + '</span></div>' +
      '<div class="lcd-gauge"><i style="width:' + r[1] + '%"></i></div>' +
    '</div>').join('');
  return '<div class="lcd" style="--lcd:' + size + 'px;--s:' + (size / 640) +
    ';--tbg:' + t.bg + ';--ttx:' + t.text + ';--tac:' + t.accent +
    ';--tgb:' + t.gaugeBg + ';--tgs:' + t.gaugeStart + ';--tge:' + t.gaugeEnd + '"' +
    ' role="img" aria-label="Écran Kraken — thème ' + t.label + '">' +
      '<div class="lcd-inner">' +
        '<div class="lcd-title">SYSTEM MONITOR</div>' +
        '<div class="lcd-time">14:32:07</div>' +
        '<div class="lcd-rows">' + rows + '</div>' +
        '<div class="lcd-liquid"><span>Liquid Temperature</span><b>32.4°C</b></div>' +
      '</div></div>';
}

/** Construit la galerie de vignettes dans #theme-gallery (si présente). */
function renderKrakenThemeGallery() {
  const host = document.getElementById('theme-gallery');
  if (!host) return;
  host.innerHTML = LCD_THEMES.map((t) =>
    '<button type="button" class="theme-card' + (t.def ? ' selected' : '') + '"' +
      ' data-theme="' + t.key + '" role="radio" aria-checked="' + (t.def ? 'true' : 'false') + '">' +
      '<span class="theme-led" aria-hidden="true"></span>' +
      '<span class="theme-check" aria-hidden="true">✓</span>' +
      '<span class="theme-badge">actif</span>' +
      '<div class="theme-visual">' + lcdThumbHTML(t.key, 150) + '</div>' +
      '<div class="theme-info"><span class="theme-name">' + t.label + '</span>' +
      '<span class="theme-sub">' + t.sub + '</span></div>' +
    '</button>').join('');
  host.querySelectorAll('.theme-card').forEach((card) => {
    card.addEventListener('click', () => selectKrakenLcdTheme(card.dataset.theme));
  });
}

/**
 * Sélectionne un thème LCD (état visuel + clé envoyée au daemon).
 * @param {string} key — data_center | overclock | fluid_flow
 */
function selectKrakenLcdTheme(key) {
  if (!LCD_THEMES.some((t) => t.key === key)) return;
  krakenLcdTheme = key;
  document.querySelectorAll('#theme-gallery .theme-card').forEach((c) => {
    const on = c.dataset.theme === key;
    c.classList.toggle('selected', on);
    c.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const keyEl = document.getElementById('theme-key');
  if (keyEl) keyEl.textContent = key;
}

/**
 * Récupère la configuration actuelle du monitoring (thème et capteurs).
 * @returns {{theme: string, options: string[]}}
 */
function getKrakenMonitorConfig() {
  const theme = krakenLcdTheme;
  const options = [];
  if (document.getElementById('kraken-cpu').checked) options.push('cpu');
  if (document.getElementById('kraken-gpu').checked) options.push('gpu');
  if (document.getElementById('kraken-ram').checked) options.push('ram');
  if (document.getElementById('kraken-vram').checked) options.push('vram');
  if (document.getElementById('kraken-disks').checked) options.push('disks');
  return { theme, options };
}

/** Démarre le mode monitoring. */
async function krakenStartMonitor() {
  const config = getKrakenMonitorConfig();
  try {
    const data = await apiFetch('/kraken/monitor/start', {
      method: 'POST',
      body: {
        interval: krakenInterval(),
        theme: config.theme,
        options: config.options
      },
    });
    if (data.ok) {
      toast('🖥 Monitoring démarré (' + config.theme + ', ' + krakenInterval() + 's)', 'success');
      refreshDisplayStatus();
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
      toast('🎞 Diaporama démarré', 'success');
      refreshDisplayStatus();
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
    if (notify && data.ok) toast('⏹ Affichage automatique arrêté', 'info');
    refreshDisplayStatus();
  } catch (err) {
    if (notify) toast('⚠ Erreur arrêt: ' + err.message, 'error');
  }
}

/** Génère un aperçu du rendu monitoring. */
async function krakenPreview() {
  const config = getKrakenMonitorConfig();
  const img = document.getElementById('kraken-preview-img');
  try {
    const data = await apiFetch('/kraken/monitor/preview', {
      method: 'POST',
      body: {
        theme: config.theme,
        options: config.options
      },
    });
    if (data.ok && data.path) {
      toast('👁 Aperçu généré', 'success');
      loadKrakenPreview();
    } else {
      toast('⚠ ' + (data.error || 'Aperçu impossible'), 'error');
    }
  } catch (err) {
    toast('⚠ Erreur: ' + err.message, 'error');
  }
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
   Initialisation
   ═══════════════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', async () => {
  console.log('🚀 Ballistix RGB Controller — Initialisation');

  // ── Éléments DOM ────────────────────────────────────
  const canvasEl = document.getElementById('led-canvas');
  const syncBtn = document.getElementById('btn-sync');
  const resetAllBtn = document.getElementById('btn-reset-all');
  const rescanBtn = document.getElementById('btn-rescan');
  // Auto-save : plus de bouton, sauvegarde automatique côté serveur

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
  let matrixMode = false;

  document.getElementById('btn-toggle-mode').addEventListener('click', () => {
    matrixMode = !matrixMode;
    const btn = document.getElementById('btn-toggle-mode');
    const indicator = document.getElementById('mode-indicator');

    if (matrixMode) {
      btn.textContent = '⊞';
      indicator.textContent = 'Mode matrice';
      indicator.className = 'badge matrix';
      canvasEl.style.cursor = 'default';
      ledCanvas.setOrientation('matrix');
    } else {
      // Arrêter l'animation si elle tourne
      apiFetch('/animation/stop', { method: 'POST' }).catch(() => {});
      document.getElementById('btn-anim-start').disabled = false;
      document.getElementById('btn-anim-stop').disabled = true;

      btn.textContent = '⊟';
      indicator.textContent = 'Mode classique';
      indicator.className = 'badge';
      canvasEl.style.cursor = 'pointer';
      ledCanvas.setOrientation(state.lastOrientation || 'vertical');
    }
  });

  // ── Animation ──
  async function startAnimation(effect, label) {
    const speed = parseFloat(document.getElementById('anim-speed').value);
    const framerate = 30;

    // Forcer le mode matrice
    if (!matrixMode) {
      document.getElementById('btn-toggle-mode').click();
    }

    try {
      await apiFetch('/animation/start', {
        method: 'POST',
        body: { effect, speed, framerate }
      });
      document.getElementById('btn-anim-start').disabled = true;
      document.getElementById('btn-anim-rainbow').disabled = true;
      document.getElementById('btn-anim-stop').disabled = false;
      toast(label + ' démarrée', 'success');
    } catch (err) {
      toast('⚠ Erreur démarrage animation: ' + err.message, 'error');
    }
  }

  document.getElementById('btn-anim-start').addEventListener('click', () => startAnimation('incandescence', '🔥 Incandescence'));
  document.getElementById('btn-anim-rainbow').addEventListener('click', () => startAnimation('rainbow', '🌈 Rainbow'));

  document.getElementById('btn-anim-stop').addEventListener('click', async () => {
    try {
      await apiFetch('/animation/stop', { method: 'POST' });
      document.getElementById('btn-anim-start').disabled = false;
      document.getElementById('btn-anim-rainbow').disabled = false;
      document.getElementById('btn-anim-stop').disabled = true;
      toast('⏹ Animation arrêtée', 'success');
    } catch (err) {
      toast('⚠ Erreur: ' + err.message, 'error');
    }
  });

  // ── Vitesse ──
  document.getElementById('anim-speed').addEventListener('input', async (e) => {
    const speed = parseFloat(e.target.value);
    document.getElementById('anim-speed-value').textContent = speed.toFixed(2) + '×';

    try {
      await apiFetch('/animation/speed', {
        method: 'POST',
        body: { speed }
      });
    } catch (err) {
      // Silencieux
    }
  });

  document.getElementById('anim-refresh').addEventListener('input', async (e) => {
    const rate = parseInt(e.target.value);
    document.getElementById('anim-refresh-value').textContent = rate + '/s';

    try {
      await apiFetch('/animation/refresh', {
        method: 'POST',
        body: { rate }
      });
    } catch (err) {
      // Silencieux
    }
  });

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
  syncBtn.addEventListener('click', async () => {
    syncBtn.disabled = true;
    try {
      await apiFetch('/apply', { method: 'POST' });
      toast('✅ LEDs synchronisées', 'success');
    } catch (err) {
      toast('❌ Erreur sync: ' + err.message, 'error');
    } finally {
      syncBtn.disabled = false;
    }
  });

  resetAllBtn.addEventListener('click', async () => {
    resetAllBtn.disabled = true;
    try {
      for (const stick of state.sticks) {
        const black = Array(stick.num_leds).fill([0, 0, 0]);
        state.colors[stick.id] = black;
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

  // ── Bouton Update ───────────────────────────────
  const updateBtn = document.getElementById('btn-update');
  if (updateBtn) {
    updateBtn.addEventListener('click', performUpdate);
  }

  // ── Auto-save : la sauvegarde est automatique côté serveur ──

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
    krakenInterval.addEventListener('input', (e) => {
      const label = document.getElementById('kraken-interval-value');
      if (label) label.textContent = e.target.value + 's';
    });
  }

  const krakenBtnPreview = document.getElementById('kraken-btn-preview');
  if (krakenBtnPreview) krakenBtnPreview.addEventListener('click', krakenPreview);

  const krakenBtnGalleryAdd = document.getElementById('kraken-btn-gallery-add');
  if (krakenBtnGalleryAdd) krakenBtnGalleryAdd.addEventListener('click', krakenAddGalleryFiles);

  const krakenBtnImage = document.getElementById('kraken-btn-image');
  if (krakenBtnImage) krakenBtnImage.addEventListener('click', () => krakenUploadImage(false));

  const krakenBtnGif = document.getElementById('kraken-btn-gif');
  if (krakenBtnGif) krakenBtnGif.addEventListener('click', () => krakenUploadImage(true));

  const krakenBrightness = document.getElementById('kraken-brightness');
  if (krakenBrightness) {
    krakenBrightness.addEventListener('input', (e) => krakenSetBrightness(e.target.value));
  }

  const krakenOrientation = document.getElementById('kraken-orientation');
  if (krakenOrientation) {
    krakenOrientation.addEventListener('change', (e) => krakenSetOrientation(e.target.value));
  }

  const krakenBtnInit = document.getElementById('kraken-btn-init');
  if (krakenBtnInit) krakenBtnInit.addEventListener('click', krakenInitialize);

  // Galerie de thèmes LCD (remplace l'ancien <select>) — rendue avant le
  // premier démarrage monitoring pour que krakenLcdTheme reflète l'UI.
  renderKrakenThemeGallery();

  // Vérification silencieuse du Kraken au chargement (ne bloque pas l'init)
  refreshKraken();
  refreshKrakenGallery();
  refreshDisplayStatus();

  // ── Connexion (WebSocket ou REST selon disponibilité) ─
  updateConnectionStatus(false);
  await initConnection();

  console.log('✅ Ballistix RGB Controller prêt');
});

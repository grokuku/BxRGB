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
   Toast / Notifications
   ═══════════════════════════════════════════════════════════ */

/**
 * Affiche une notification toast en haut à droite.
 * @param {string} message
 * @param {'info'|'success'|'error'} type
 */
function toast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);

  setTimeout(() => {
    if (el.parentNode) el.parentNode.removeChild(el);
  }, 3000);
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
 * Vérifie si WebSocket est disponible et initialise la connexion.
 */
async function initConnection() {
  try {
    const resp = await fetch('/api/ws-status');
    const status = await resp.json();

    if (status.websocket) {
      console.log('✅ WebSocket disponible, connexion...');
      document.getElementById('connection-mode').textContent = '⚡ Temps réel';
      document.getElementById('connection-mode').style.color = '#2ecc71';
      connectWS();
    } else {
      console.log('⚠ WebSocket non disponible, mode REST');
      document.getElementById('connection-mode').textContent = '⚡ REST';
      document.getElementById('connection-mode').style.color = '#e94560';
      state.connected = true;
      state.emit('connected');
      updateConnectionStatus(true);
      // Charger l'état initial via REST
      await loadInitialState();
    }
  } catch (err) {
    // Serveur pas encore prêt ? Essayer en REST direct
    console.log('⚠ Statut WS inaccessible, fallback REST:', err);
    document.getElementById('connection-mode').textContent = '⚡ REST';
    document.getElementById('connection-mode').style.color = '#e94560';
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

async function apiFetch(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return res.json();
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
      fetch('/api/animation/stop', { method: 'POST' }).catch(() => {});
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
      const resp = await fetch('/api/animation/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ effect, speed, framerate })
      });

      if (resp.ok) {
        document.getElementById('btn-anim-start').disabled = true;
        document.getElementById('btn-anim-rainbow').disabled = true;
        document.getElementById('btn-anim-stop').disabled = false;
        toast(label + ' démarrée', 'success');
      } else {
        toast('⚠ Erreur démarrage animation', 'error');
      }
    } catch (err) {
      toast('⚠ Erreur: ' + err.message, 'error');
    }
  }

  document.getElementById('btn-anim-start').addEventListener('click', () => startAnimation('incandescence', '🔥 Incandescence'));
  document.getElementById('btn-anim-rainbow').addEventListener('click', () => startAnimation('rainbow', '🌈 Rainbow'));

  document.getElementById('btn-anim-stop').addEventListener('click', async () => {
    try {
      const resp = await fetch('/api/animation/stop', { method: 'POST' });
      if (resp.ok) {
        document.getElementById('btn-anim-start').disabled = false;
        document.getElementById('btn-anim-rainbow').disabled = false;
        document.getElementById('btn-anim-stop').disabled = true;
        toast('⏹ Animation arrêtée', 'success');
      }
    } catch (err) {
      toast('⚠ Erreur: ' + err.message, 'error');
    }
  });

  // ── Vitesse ──
  document.getElementById('anim-speed').addEventListener('input', async (e) => {
    const speed = parseFloat(e.target.value);
    document.getElementById('anim-speed-value').textContent = speed.toFixed(2) + '×';

    try {
      await fetch('/api/animation/speed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speed })
      });
    } catch (err) {
      // Silencieux
    }
  });

  document.getElementById('anim-refresh').addEventListener('input', async (e) => {
    const rate = parseInt(e.target.value);
    document.getElementById('anim-refresh-value').textContent = rate + '/s';

    try {
      await fetch('/api/animation/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rate })
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
      await fetch('/api/apply', { method: 'POST' });
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

  // ── Auto-save : la sauvegarde est automatique côté serveur ──

  // ── Connexion (WebSocket ou REST selon disponibilité) ─
  updateConnectionStatus(false);
  await initConnection();

  console.log('✅ Ballistix RGB Controller prêt');
});

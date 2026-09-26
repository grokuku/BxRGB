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
   Auto-Update
   ═══════════════════════════════════════════════════════════ */

/**
 * Affiche le modal d'update et gère tout le flux.
 * @param {string} stepId - ID de l'étape à activer
 * @param {'active'|'done'|'error'} state
 */
function setUpdateStep(stepId, state) {
  const step = document.getElementById(stepId);
  if (!step) return;
  step.classList.remove('hidden', 'active', 'done', 'error');
  if (state) step.classList.add(state);
}

/** Affiche le modal d'update. */
function showUpdateModal(title) {
  const overlay = document.getElementById('update-overlay');
  const titleEl = document.getElementById('update-modal-title');
  if (titleEl) titleEl.textContent = title || '⬇ Mise à jour';
  if (overlay) overlay.classList.remove('hidden');
}

/** Cache le modal d'update. */
function hideUpdateModal() {
  const overlay = document.getElementById('update-overlay');
  if (overlay) overlay.classList.add('hidden');
  // Réinitialiser les étapes
  ['update-step-check', 'update-step-download', 'update-step-install',
   'update-step-restart', 'update-step-reconnect'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  });
}

/** Ajoute un message d'erreur dans le modal. */
function showUpdateError(message) {
  const body = document.getElementById('update-modal-body');
  if (!body) return;
  // Retirer l'ancienne erreur
  const oldErr = body.querySelector('.update-step.error-msg');
  if (oldErr) oldErr.remove();
  const errEl = document.createElement('div');
  errEl.className = 'update-step error error-msg';
  errEl.innerHTML = `<span class="update-step-icon">⚠</span><span>${message}</span>`;
  body.appendChild(errEl);
}

/** Ajoute un message de succès dans le modal. */
function showUpdateSuccess(message) {
  const body = document.getElementById('update-modal-body');
  if (!body) return;
  const oldMsg = body.querySelector('.update-step.success-msg');
  if (oldMsg) oldMsg.remove();
  const msgEl = document.createElement('div');
  msgEl.className = 'update-step done success-msg';
  msgEl.innerHTML = `<span class="update-step-icon">✅</span><span>${message}</span>`;
  body.appendChild(msgEl);
}

/**
 * Tente de se reconnecter au serveur après une mise à jour.
 * Recharge la page quand la connexion est rétablie.
 * @param {number} maxAttempts - nombre max de tentatives
 * @param {number} interval - ms entre les tentatives
 */
function reconnectAfterUpdate(maxAttempts = 30, interval = 2000) {
  let attempts = 0;

  function tryReconnect() {
    attempts++;
    console.log(`🔁 Tentative de reconnexion ${attempts}/${maxAttempts}...`);

    fetch('/api/status', { signal: AbortSignal.timeout(3000) })
      .then(resp => {
        if (resp.ok) {
          console.log('✅ Serveur de nouveau accessible ! Rechargement...');
          showUpdateSuccess('Serveur reconnecté ! Rechargement de la page...');
          setTimeout(() => window.location.reload(), 1500);
        } else {
          if (attempts < maxAttempts) {
            setTimeout(tryReconnect, interval);
          } else {
            showUpdateError('Le serveur ne répond pas après plusieurs tentatives. Rechargez la page manuellement.');
          }
        }
      })
      .catch(() => {
        if (attempts < maxAttempts) {
          setTimeout(tryReconnect, interval);
        } else {
          showUpdateError('Impossible de se reconnecter au serveur. Rechargez la page manuellement.');
        }
      });
  }

  setTimeout(tryReconnect, interval);
}

/**
 * Lance le processus de mise à jour complet.
 */
async function performUpdate() {
  showUpdateModal('⬇ Mise à jour');

  // ── Étape 1: Vérification ──
  setUpdateStep('update-step-check', 'active');

  let checkData;
  try {
    const resp = await fetch('/api/update/check');
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(text || `HTTP ${resp.status}`);
    }
    checkData = await resp.json();
  } catch (err) {
    setUpdateStep('update-step-check', 'error');
    showUpdateError('Échec de la vérification: ' + err.message);
    return;
  }

  setUpdateStep('update-step-check', 'done');

  if (!checkData.update_available) {
    showUpdateSuccess(`Déjà à jour (v${checkData.current})`);
    toast('✅ Déjà à jour (v' + checkData.current + ')', 'success');
    setTimeout(hideUpdateModal, 2500);
    return;
  }

  // ── Demander confirmation ──
  const confirmed = confirm(
    `Une mise à jour est disponible !\n\n` +
    `Version actuelle : v${checkData.current}\n` +
    `Nouvelle version : v${checkData.latest}\n\n` +
    `Le service va être redémarré. Continuer ?`
  );
  if (!confirmed) {
    hideUpdateModal();
    toast('Mise à jour annulée', 'info');
    return;
  }

  // ── Étape 2: Téléchargement + Installation ──
  setUpdateStep('update-step-download', 'active');

  let updateData;
  try {
    const resp = await fetch('/api/update', { method: 'POST' });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      // Essayer de parser le JSON d'erreur
      try {
        const errJson = JSON.parse(text);
        throw new Error(errJson.detail || errJson.message || `HTTP ${resp.status}`);
      } catch (parseErr) {
        throw new Error(text || `HTTP ${resp.status}`);
      }
    }
    updateData = await resp.json();
  } catch (err) {
    setUpdateStep('update-step-download', 'error');
    showUpdateError('Échec de la mise à jour: ' + err.message);
    return;
  }

  // ── Cas: déjà à jour (le serveur peut le détecter aussi) ──
  if (updateData.status === 'up-to-date') {
    setUpdateStep('update-step-download', 'done');
    showUpdateSuccess(`Déjà à jour (v${updateData.current})`);
    setTimeout(hideUpdateModal, 2500);
    return;
  }

  // ── Étape 3: Installation réussie ──
  setUpdateStep('update-step-download', 'done');
  setUpdateStep('update-step-install', 'active');
  setUpdateStep('update-step-install', 'done');
  setUpdateStep('update-step-restart', 'active');
  setUpdateStep('update-step-restart', 'done');

  // ── Étape 4: Reconnexion ──
  setUpdateStep('update-step-reconnect', 'active');
  toast('✅ Mise à jour installée. Reconnexion...', 'success');
  reconnectAfterUpdate();
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
    const resp = await fetch('/api/kraken/status');
    data = await resp.json();
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
    const resp = await fetch('/api/kraken/lcd/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'liquid' }),
    });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/lcd/image', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data, filename: file.name, animated: isGif }),
    });
    const result = await resp.json();
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
      await fetch('/api/kraken/lcd/brightness', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: val }),
      });
    } catch (err) {
      toast('⚠ Erreur luminosité: ' + err.message, 'error');
    }
  }, 400);
}

/** Règle l'orientation de l'écran. */
async function krakenSetOrientation(value) {
  try {
    const resp = await fetch('/api/kraken/lcd/orientation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: parseInt(value, 10) || 0 }),
    });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/initialize', { method: 'POST' });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/display/status');
    const data = await resp.json();
    const stopBtn = document.getElementById('kraken-btn-stop-display');
    if (stopBtn) stopBtn.disabled = !(data && data.running);
  } catch (err) { /* silencieux */ }
}

/** Démarre le mode monitoring. */
/**
 * Récupère la configuration actuelle du monitoring (thème et capteurs).
 * @returns {{theme: string, options: string[]}}
 */
function getKrakenMonitorConfig() {
  const theme = document.getElementById('kraken-theme').value;
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
    const resp = await fetch('/api/kraken/monitor/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        interval: krakenInterval(),
        theme: config.theme,
        options: config.options
      }),
    });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/gallery/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval: krakenInterval() }),
    });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/display/stop', { method: 'POST' });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/monitor/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        theme: config.theme,
        options: config.options
      }),
    });
    const data = await resp.json();
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
    const resp = await fetch('/api/kraken/gallery');
    const data = await resp.json();
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
      const resp = await fetch('/api/kraken/gallery/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data, filename: file.name }),
      });
      const result = await resp.json();
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
  if (!confirm('Supprimer "' + name + '" de la gallery ?')) return;
  try {
    const resp = await fetch('/api/kraken/gallery/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await resp.json();
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

  // ── Bouton Update ───────────────────────────────
  const updateBtn = document.getElementById('btn-update');
  const updateModalClose = document.getElementById('update-modal-close');

  if (updateBtn) {
    updateBtn.addEventListener('click', performUpdate);
  }
  if (updateModalClose) {
    updateModalClose.addEventListener('click', hideUpdateModal);
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

  // Vérification silencieuse du Kraken au chargement (ne bloque pas l'init)
  refreshKraken();
  refreshKrakenGallery();
  refreshDisplayStatus();

  // ── Connexion (WebSocket ou REST selon disponibilité) ─
  updateConnectionStatus(false);
  await initConnection();

  console.log('✅ Ballistix RGB Controller prêt');
});

/**
 * Ballistix RGB Controller — Color Picker & Controls
 * ────────────────────────────────────────────────────────────
 * Gère le color picker HTML5, les quick colors, le slider de
 * luminosité et la sélection de sticks/LEDs.
 * Les contrôles sont désactivés si aucune LED/stick n'est
 * sélectionné.
 */

/* ── Presets de Quick Colors (mêmes couleurs que le Tkinter) ── */
const QUICK_COLORS = [
  { name: 'Blanc',   rgb: [255, 255, 255] },
  { name: 'Rouge',   rgb: [255, 0, 0] },
  { name: 'Vert',    rgb: [0, 255, 0] },
  { name: 'Bleu',    rgb: [0, 0, 255] },
  { name: 'Jaune',   rgb: [255, 255, 0] },
  { name: 'Cyan',    rgb: [0, 255, 255] },
  { name: 'Magenta', rgb: [255, 0, 255] },
  { name: 'Orange',  rgb: [255, 165, 0] },
  { name: 'Rose',    rgb: [255, 192, 203] },
  { name: 'Noir',    rgb: [0, 0, 0] },
];

/**
 * Convertit un triplet [R, G, B] (0-255) en chaîne hex #RRGGBB.
 */
function rgbToHex(r, g, b) {
  const toHex = (v) => Math.max(0, Math.min(255, Math.round(v)))
    .toString(16).padStart(2, '0');
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

/**
 * Convertit une chaîne hex #RRGGBB en triplet [R, G, B].
 */
function hexToRgb(hex) {
  const val = hex.replace('#', '');
  return [
    parseInt(val.slice(0, 2), 16),
    parseInt(val.slice(2, 4), 16),
    parseInt(val.slice(4, 6), 16),
  ];
}

/**
 * Formate un triplet RGB en chaîne lisible.
 */
function rgbToString(r, g, b) {
  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

/**
 * Convertit #rgb/#rrggbb en rgba(r, g, b, a).
 * @param {string} hex
 * @param {number} alpha — 0..1
 * @returns {string}
 */
function hexToRgba(hex, alpha) {
  const val = String(hex || '#ffffff').replace('#', '');
  const full = val.length === 3 ? val.split('').map((c) => c + c).join('') : val;
  const r = parseInt(full.slice(0, 2), 16) || 0;
  const g = parseInt(full.slice(2, 4), 16) || 0;
  const b = parseInt(full.slice(4, 6), 16) || 0;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Reflète la couleur LED courante sur les variables HÔTE --pick* (halo de
 * sélection, pastille, canvas). N'écrit JAMAIS de variable --holaf-* : la
 * couleur des LEDs est volontairement dissociée du pack d'interface, qui
 * seul pilote --holaf-accent (sélecteur 🎨 du header).
 * @param {string} hex
 */
function applyPickColor(hex) {
  const root = document.documentElement.style;
  root.setProperty('--pick', hex);
  root.setProperty('--pick-soft', hexToRgba(hex, 0.16));
  root.setProperty('--pick-glow', hexToRgba(hex, 0.5));
  const swatch = document.getElementById('pick-swatch');
  if (swatch) swatch.style.background = hex;
}

/**
 * Met à jour l'état disabled des contrôles selon la sélection.
 * @param {Object} state
 * @param {HTMLInputElement} picker
 * @param {HTMLInputElement} brightnessSlider
 */
function updateControlsDisabled(state, picker, brightnessSlider) {
  const hasSelection = state.selected !== null || state.selected_stick !== null;

  picker.disabled = !hasSelection;
  brightnessSlider.disabled = !hasSelection;

  // Style visuel pour disabled
  picker.style.opacity = hasSelection ? '1' : '0.4';
  picker.style.cursor = hasSelection ? 'pointer' : 'not-allowed';
  brightnessSlider.style.opacity = hasSelection ? '1' : '0.4';
  brightnessSlider.style.cursor = hasSelection ? 'pointer' : 'not-allowed';
}

/**
 * Initialise les contrôles de couleur dans le DOM.
 * @param {Object} state — State central (voir app.js)
 * @param {Function} sendWS — Fonction pour envoyer un msg WebSocket
 * @param {Function} toast — Fonction pour afficher une notification
 */
function initColorControls(state, sendWS, toast) {
  const picker = document.getElementById('color-picker');
  const hexDisplay = document.getElementById('color-hex');
  const quickContainer = document.getElementById('quick-colors');
  const brightnessSlider = document.getElementById('brightness-slider');
  const brightnessValue = document.getElementById('brightness-value');
  const ledInfo = document.getElementById('led-info');

  if (!picker) return;

  // ── Quick Colors ──────────────────────────────────────
  QUICK_COLORS.forEach((qc) => {
    const btn = document.createElement('button');
    btn.className = 'quick-color-btn';
    const [r, g, b] = qc.rgb;
    btn.style.background = rgbToString(r, g, b);
    btn.title = qc.name;
    btn.dataset.r = r;
    btn.dataset.g = g;
    btn.dataset.b = b;

    // Label sous le bouton
    const wrapper = document.createElement('div');
    wrapper.className = 'quick-color-wrapper';
    wrapper.appendChild(btn);

    const label = document.createElement('span');
    label.className = 'quick-color-label';
    label.textContent = qc.name;
    wrapper.appendChild(label);

    btn.addEventListener('click', (e) => {
      // Si rien n'est sélectionné, ne rien faire
      if (!state.selected && !state.selected_stick) {
        toast('Cliquez d\'abord sur une LED ou un stick', 'info');
        return;
      }

      const ctrl = e.ctrlKey || e.metaKey;
      if (ctrl) {
        // Ctrl+click → toutes LEDs de tous les sticks
        state.sticks.forEach((stick) => {
          sendWS({
            type: 'set_all_leds',
            stick_id: stick.id,
            color: [r, g, b],
          });
        });
        toast(`${qc.name} appliqué à tous les sticks`, 'info');
      } else if (state.selected_stick) {
        // Appliquer au stick sélectionné
        sendWS({
          type: 'set_all_leds',
          stick_id: state.selected_stick,
          color: [r, g, b],
        });
      } else if (state.selected) {
        // Appliquer à la LED sélectionnée
        sendWS({
          type: 'set_led',
          stick_id: state.selected.stick_id,
          led_idx: state.selected.led_idx,
          color: [r, g, b],
        });
      } else if (state.sticks.length > 0) {
        // Par défaut, premier stick
        sendWS({
          type: 'set_all_leds',
          stick_id: state.sticks[0].id,
          color: [r, g, b],
        });
      }
    });

    quickContainer.appendChild(wrapper);
  });

  // ── Color Picker natif ────────────────────────────────
  picker.addEventListener('input', () => {
    // Si rien n'est sélectionné, ignorer
    if (!state.selected && !state.selected_stick) return;

    const [r, g, b] = hexToRgb(picker.value);
    hexDisplay.textContent = picker.value;
    applyPickColor(picker.value);

    if (state.selected) {
      sendWS({
        type: 'set_led',
        stick_id: state.selected.stick_id,
        led_idx: state.selected.led_idx,
        color: [r, g, b],
      });
    } else if (state.selected_stick) {
      sendWS({
        type: 'set_all_leds',
        stick_id: state.selected_stick,
        color: [r, g, b],
      });
    }
  });

  // ── Brightness Slider ─────────────────────────────────
  brightnessSlider.addEventListener('input', () => {
    // Si rien n'est sélectionné, ignorer
    if (!state.selected && !state.selected_stick) return;

    const level = parseInt(brightnessSlider.value, 10);
    brightnessValue.textContent = level;
    state.brightness = level;
    // Propage le changement (compteur « modifications non enregistrées »,
    // synchronisation des affichages) — y compris pour une barrette unique.
    state.emit('brightness');

    if (state.selected_stick) {
      sendWS({
        type: 'set_brightness',
        stick_id: state.selected_stick,
        level,
      });
    } else {
      sendWS({ type: 'set_all_brightness', level });
    }
  });

  // ── Mise à jour du color picker selon la sélection ────
  function updatePickerFromSelection() {
    let r = 255, g = 255, b = 255;

    if (state.selected) {
      const colors = state.colors[state.selected.stick_id];
      if (colors && colors[state.selected.led_idx]) {
        [r, g, b] = colors[state.selected.led_idx];
      }
      ledInfo.innerHTML = `LED <strong>#${state.selected.led_idx + 1}</strong> — <strong>Barrette #${state.sticks.findIndex(s => s.id === state.selected.stick_id) + 1}</strong>`;
    } else if (state.selected_stick) {
      const colors = state.colors[state.selected_stick];
      if (colors && colors.length > 0) {
        // Moyenne des couleurs du stick pour approximation
        const avg = colors.reduce(
          (acc, c) => [acc[0] + c[0], acc[1] + c[1], acc[2] + c[2]],
          [0, 0, 0]
        );
        const n = colors.length;
        [r, g, b] = [avg[0] / n, avg[1] / n, avg[2] / n];
      }
      const stick = state.sticks.find(s => s.id === state.selected_stick);
      const idx = state.sticks.findIndex(s => s.id === state.selected_stick);
      ledInfo.innerHTML = `Stick <strong>Barrette #${idx + 1}</strong> — <strong>${stick ? stick.num_leds : '?'} LEDs</strong>`;
    } else {
      ledInfo.innerHTML = '<span style="color:var(--text-dim);">Cliquez sur une LED pour la modifier</span>';
    }

    const hex = rgbToHex(r, g, b);
    picker.value = hex;
    hexDisplay.textContent = hex;
    applyPickColor(hex);

    // Mettre à jour l'état disabled des contrôles
    updateControlsDisabled(state, picker, brightnessSlider);
  }

  // ── Abonnements aux changements d'état ────────────────
  state.on('selected_stick', updatePickerFromSelection);
  state.on('selected', updatePickerFromSelection);
  state.on('colors', updatePickerFromSelection);
  state.on('brightness', () => {
    brightnessSlider.value = state.brightness;
    brightnessValue.textContent = state.brightness;
  });

  // Initial render
  updatePickerFromSelection();
  brightnessSlider.value = state.brightness;
  brightnessValue.textContent = state.brightness;

  // Initial disabled state
  updateControlsDisabled(state, picker, brightnessSlider);
}

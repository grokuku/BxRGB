/**
 * Ballistix RGB Controller — Drag & Drop Matrix
 * ────────────────────────────────────────────────────────────
 * Représente les sticks comme des tuiles draggable dans une
 * grille. L'utilisateur peut réorganiser les sticks, les
 * sélectionner d'un double-clic, ou cliquer sur une LED
 * individuelle.
 */

/**
 * Initialise le système de Drag & Drop.
 * @param {Object} state — State central (voir app.js)
 * @param {Function} onLedClick — Callback (stick_id, led_idx) => void
 */
function initDnD(state, onLedClick) {
  const container = document.getElementById('dnd-container');
  const emptyState = document.getElementById('empty-state');
  if (!container) return;

  function render() {
    container.innerHTML = '';

    if (state.sticks.length === 0) {
      container.innerHTML = '';
      if (emptyState) emptyState.classList.remove('hidden');
      return;
    }
    if (emptyState) emptyState.classList.add('hidden');

    state.sticks.forEach((stick, idx) => {
      const slot = document.createElement('div');
      slot.className = 'dnd-slot';
      slot.dataset.index = idx;

      const tile = document.createElement('div');
      tile.className = 'dnd-stick';
      tile.draggable = true;
      tile.dataset.stickId = stick.id;
      tile.dataset.index = idx;

      if (state.selected_stick === stick.id) {
        tile.classList.add('selected');
      }

      // Label simplifié
      const label = document.createElement('div');
      label.className = 'stick-label';
      label.textContent = `Barrette #${idx + 1}`;
      tile.appendChild(label);

      // Meta (infos techniques en tooltip)
      const meta = document.createElement('div');
      meta.className = 'stick-meta';
      meta.textContent = `${stick.num_leds} LEDs`;
      tile.title = `${stick.id} · bus ${stick.bus_num} · adr ${stick.address} · v${stick.version || '?'}`;
      tile.appendChild(meta);

      // Mini LEDs indicatrices
      const miniLeds = document.createElement('div');
      miniLeds.className = 'stick-leds-indicator';
      const colors = state.colors[stick.id] || [];
      const showCount = Math.min(stick.num_leds, 16);
      for (let i = 0; i < showCount; i++) {
        const dot = document.createElement('div');
        dot.className = 'mini-led';
        if (colors[i]) {
          const [r, g, b] = colors[i];
          if (r > 0 || g > 0 || b > 0) {
            dot.style.background = `rgb(${r},${g},${b})`;
          }
        }
        miniLeds.appendChild(dot);
      }
      tile.appendChild(miniLeds);

      // ── Drag Events ────────────────────────────────
      tile.addEventListener('dragstart', (e) => {
        e.dataTransfer.setData('text/plain', stick.id);
        e.dataTransfer.effectAllowed = 'move';
        tile.classList.add('dragging');
        // Stocker l'index d'origine
        e.dataTransfer.setData('application/x-index', String(idx));
      });

      tile.addEventListener('dragend', () => {
        tile.classList.remove('dragging');
        document.querySelectorAll('.dnd-slot').forEach(s => s.classList.remove('dragover'));
      });

      // ── Drop sur le slot parent ────────────────────
      slot.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        slot.classList.add('dragover');
      });

      slot.addEventListener('dragleave', () => {
        slot.classList.remove('dragover');
      });

      slot.addEventListener('drop', (e) => {
        e.preventDefault();
        slot.classList.remove('dragover');

        const draggedId = e.dataTransfer.getData('text/plain');
        const fromIdx = parseInt(e.dataTransfer.getData('application/x-index'), 10);
        const toIdx = parseInt(slot.dataset.index, 10);

        if (isNaN(fromIdx) || isNaN(toIdx) || fromIdx === toIdx) return;

        // Réordonner state.sticks
        const stick = state.sticks.splice(fromIdx, 1)[0];
        state.sticks.splice(toIdx, 0, stick);

        // Re-rendre le canvas et le DnD
        state.notify();

        // Persister l'ordre dans la configuration
        fetch('/api/colors/save', { method: 'POST' })
          .then(r => {
            if (r.ok) console.log('✅ Ordre des sticks sauvegardé');
            else console.warn('⚠ Échec sauvegarde ordre');
          })
          .catch(err => console.warn('⚠ Erreur sauvegarde ordre:', err));
      });

      // ── Clic sur une LED individuelle (via le canvas) ──
      // Le clic est géré par le canvas, mais on lie
      // le double-clic sur la tuile pour sélectionner le stick
      tile.addEventListener('dblclick', () => {
        state.selected_stick = (state.selected_stick === stick.id) ? null : stick.id;
        if (state.selected_stick) {
          state.selected = null;
        }
        state.notify();
      });

      // Clic simple sur la tuile → sélectionne le stick
      tile.addEventListener('click', (e) => {
        // Ne pas interférer avec le drag
        if (e.target.closest('.dnd-stick')) {
          state.selected_stick = (state.selected_stick === stick.id) ? null : stick.id;
          if (state.selected_stick) {
            state.selected = null;
          }
          state.notify();
        }
      });

      slot.appendChild(tile);
      container.appendChild(slot);
    });

    // Remplir les slots vides
    const totalSlots = 4;
    for (let i = state.sticks.length; i < totalSlots; i++) {
      const slot = document.createElement('div');
      slot.className = 'dnd-slot empty';
      slot.textContent = 'Vide';
      container.appendChild(slot);
    }
  }

  // Réagir aux changements d'état
  state.on('sticks', render);
  state.on('colors', render);
  state.on('selected_stick', render);

  render();
}

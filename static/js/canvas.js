/**
 * Ballistix RGB Controller — LED Canvas Preview
 * ────────────────────────────────────────────────────────────
 * Dessine les sticks en mode vertical (défaut) ou horizontal.
 * Mode vertical   : sticks côte à côte, LEDs de haut en bas.
 * Mode horizontal : sticks empilés, LEDs de gauche à droite.
 */

class LedCanvas {
  /**
   * @param {HTMLCanvasElement} canvasElement
   */
  constructor(canvasElement) {
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext('2d');

    // Orientation par défaut : vertical (comme des barrettes physiques)
    this.orientation = 'vertical'; // 'vertical' | 'horizontal'

    // Inversion verticale : la LED 0 (logique) est affichée en bas
    // (physiquement la LED 0 est en bas sur les barrettes Ballistix).
    this.flipVertical = true;

    // Dimensions des LEDs (carrées)
    this.LED_SIZE = 36;
    this.LED_GAP = 6;
    this.PADDING = 20;

    // Espace pour les labels
    this.LABEL_WIDTH = 90;         // mode horizontal : label à gauche
    this.STICK_LABEL_HEIGHT = 24;  // mode vertical   : label au-dessus

    // État du survol
    this.hovered = null; // { stick_idx, led_idx } | null

    // Callbacks
    this.onLedClick = null; // (stick_id, led_idx) => void

    // Décalage de centrage horizontal (mis à jour au render)
    this._centeringOffset = 0;

    // Forcer la taille CSS du canvas (évite les overrides CSS type width:100%!important)
    this.canvas.style.setProperty('width', '100%', 'important');
    this.canvas.style.setProperty('height', 'auto', 'important');

    // Bind events
    this.canvas.addEventListener('mousemove', this._onMouseMove.bind(this));
    this.canvas.addEventListener('mouseleave', this._onMouseLeave.bind(this));
    this.canvas.addEventListener('click', this._onClick.bind(this));
  }

  /**
   * Change l'orientation et re-rend le canvas.
   * @param {'vertical'|'horizontal'} orientation
   */
  setOrientation(orientation) {
    this.orientation = orientation;
    this.render(this._lastState);
  }

  /**
   * Retourne true si le mode est 'matrix'.
   */
  isMatrixMode() {
    return this.orientation === 'matrix';
  }

  /**
   * Calcule les dimensions totales du canvas selon l'orientation.
   */
  _getDimensions(sticks) {
    const count = sticks.length || 1;

    if (this.orientation === 'vertical') {
      // Sticks côte à côte, LEDs de haut en bas
      const w = this.PADDING * 2
              + count * (this.LED_SIZE + this.LED_GAP * 2 + 16)
              + 20;
      const h = this.PADDING * 2
              + this.STICK_LABEL_HEIGHT + 8
              + 8 * (this.LED_SIZE + this.LED_GAP)
              - this.LED_GAP;
      return { width: Math.max(w, 300), height: Math.max(h, 200) };
    } else {
      // Horizontal : sticks empilés, LEDs de gauche à droite
      const w = this.PADDING + this.LABEL_WIDTH
              + 8 * (this.LED_SIZE + this.LED_GAP) - this.LED_GAP
              + this.PADDING;
      const h = this.PADDING * 2
              + count * (this.LED_SIZE + 30)
              + 10;
      return { width: Math.max(w, 400), height: Math.max(h, 80) };
    }
  }

  /**
   * Retourne la position (x, y) du coin supérieur gauche d'une LED
   * selon son index VISUEL (position à l'écran).
   * Inclut le décalage de centrage (_centeringOffset).
   * @param {number} stickIdx  — Index du stick
   * @param {number} visualIdx — Index VISUEL (0=en haut, 7=en bas)
   */
  _getLedPosition(stickIdx, visualIdx) {
    const offX = this._centeringOffset || 0;
    if (this.orientation === 'vertical') {
      const x = this.PADDING + offX + stickIdx * (this.LED_SIZE + this.LED_GAP * 2 + 16);
      const y = this.PADDING + this.STICK_LABEL_HEIGHT + 8 + visualIdx * (this.LED_SIZE + this.LED_GAP);
      return { x, y };
    } else {
      const x = this.PADDING + this.LABEL_WIDTH + visualIdx * (this.LED_SIZE + this.LED_GAP);
      const y = this.PADDING + stickIdx * (this.LED_SIZE + 30);
      return { x, y };
    }
  }

  /**
   * Centre horizontalement l'ensemble des sticks dans le canvas.
   * Retourne un décalage X à appliquer.
   */
  _getCenteringOffset(sticks, canvasWidth) {
    if (!sticks || sticks.length === 0) return 0;

    if (this.orientation === 'vertical') {
      const totalW = sticks.length * (this.LED_SIZE + this.LED_GAP * 2 + 16) - (this.LED_GAP * 2 + 16) + this.LED_SIZE;
      const contentW = totalW + this.PADDING * 2 + 20;
      if (canvasWidth > contentW) {
        return Math.floor((canvasWidth - contentW) / 2);
      }
    }
    return 0;
  }

  /**
   * Trouve quelle LED est à la position (x, y) du canvas.
   * Parcourt les positions visuelles (0=en haut → 7=en bas)
   * et applique flipVertical pour mapper à l'index logique.
   */
  _hitTest(x, y, sticks) {
    const flip = this.flipVertical !== false;
    for (let si = 0; si < sticks.length; si++) {
      const stick = sticks[si];
      for (let vi = 0; vi < stick.num_leds; vi++) {
        const pos = this._getLedPosition(si, vi);
        if (
          x >= pos.x && x < pos.x + this.LED_SIZE &&
          y >= pos.y && y < pos.y + this.LED_SIZE
        ) {
          // vi est l'index visuel (position à l'écran)
          // ledIdx est l'index logique (qui correspond au registre physique)
          const ledIdx = flip ? (7 - vi) : vi;
          return { stick_idx: si, led_idx: ledIdx, stick_id: stick.id };
        }
      }
    }
    return null;
  }

  /** @private */
  _onMouseMove(event) {
    // Pas de hover/survol en mode matrice
    if (this.orientation === 'matrix') {
      this.hovered = null;
      return;
    }
    console.log('event.offsetX/Y:', event.offsetX, event.offsetY);
    console.log('event.clientX/Y:', event.clientX, event.clientY);
    console.log('canvas attr size :', this.canvas.width, this.canvas.height);
    console.log('canvas style    :', this.canvas.style.width, this.canvas.style.height);
    console.log('devicePixelRatio:', window.devicePixelRatio);

    // ── Conversion robuste clientX → coordonnées de dessin ──
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;

    // Position en pixels CSS dans le canvas
    const cssX = event.clientX - rect.left;
    const cssY = event.clientY - rect.top;

    // Largeur / hauteur CSS du canvas (d'après le DOM)
    const cssWidth = rect.width;
    const cssHeight = rect.height;

    // Largeur / hauteur logique (celle utilisée pour le dessin, avant scale dpr)
    const logicalWidth = this.canvas.width / dpr;
    const logicalHeight = this.canvas.height / dpr;

    // Facteur de conversion si le CSS a redimensionné le canvas
    const scaleX = logicalWidth / cssWidth;
    const scaleY = logicalHeight / cssHeight;

    console.log('rect (CSS px)   :', rect.width, rect.height);
    console.log('logical (draw)  :', logicalWidth, logicalHeight);
    console.log('scale           :', scaleX, scaleY);
    console.log('drawX/Y         :', cssX * scaleX, cssY * scaleY);

    // Coordonnées dans le repère de dessin (logique)
    const drawX = cssX * scaleX;
    const drawY = cssY * scaleY;

    // Hit test
    const hit = this._hitTest(drawX, drawY, this._lastSticks || []);
    this.hovered = hit;
    this.render(this._lastState);
    this.canvas.style.cursor = hit ? 'pointer' : 'default';
  }

  /** @private */
  _onMouseLeave() {
    this.hovered = null;
    this.render(this._lastState);
  }

  /** @private */
  _onClick(e) {
    // Pas de clic en mode matrice
    if (this.orientation === 'matrix') return;
    if (!this.hovered || !this.onLedClick) return;
    this.onLedClick(this.hovered.stick_id, this.hovered.led_idx);
  }

  /**
   * Dessine la grille de LEDs.
   * @param {Object} state — State central (sticks, colors, selected)
   */
  render(state) {
    this._lastState = state;
    const sticks = state.sticks || [];
    this._lastSticks = sticks;

    const dim = this._getDimensions(sticks);
    const dpr = window.devicePixelRatio || 1;
    const parentWidth = this.canvas.parentElement.clientWidth || 500;

    const logicalW = Math.max(parentWidth, dim.width);
    const logicalH = dim.height;

    // Redimensionnement du canvas (physique vs logique)
    this.canvas.width = logicalW * dpr;
    this.canvas.height = logicalH * dpr;
    this.canvas.style.setProperty('width', logicalW + 'px', 'important');
    this.canvas.style.setProperty('height', logicalH + 'px', 'important');
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // DEBUG render
    console.log('=== render() DEBUG ===');
    console.log('logical  :', logicalW, logicalH, '| dpr:', dpr);
    console.log('attr     :', this.canvas.width, this.canvas.height);
    console.log('rect     :', this.canvas.getBoundingClientRect().width, this.canvas.getBoundingClientRect().height);
    console.log('style    :', this.canvas.style.width, this.canvas.style.height);

    const ctx = this.ctx;

    // ── Fond ───────────────────────────────────────────
    ctx.fillStyle = '#16213e';
    ctx.fillRect(0, 0, logicalW, logicalH);

    // ── Pas de sticks détectés ─────────────────────────
    if (sticks.length === 0) {
      ctx.fillStyle = '#8899bb';
      ctx.font = '15px "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('Aucune barrette détectée.', logicalW / 2, logicalH / 2 - 14);
      ctx.fillStyle = '#667799';
      ctx.font = '13px "Segoe UI", sans-serif';
      ctx.fillText('Lancez le diagnostic ou ajoutez un device avec --add-device', logicalW / 2, logicalH / 2 + 14);
      return;
    }

    // Centrage horizontal si le canvas est plus large que le contenu
    const offsetX = this._getCenteringOffset(sticks, logicalW);
    this._centeringOffset = offsetX;  // Stocké pour le hit test

    // ── Rendu selon l'orientation ──────────────────────
    if (this.orientation === 'matrix') {
      this._renderMatrix(ctx, state, logicalW, logicalH);
    } else if (this.orientation === 'vertical') {
      this._renderVertical(ctx, sticks, state, offsetX);
    } else {
      this._renderHorizontal(ctx, sticks, state, offsetX);
    }

    // Infos techniques accessibles via tooltip du canvas-wrapper
    this.canvas.parentElement.title = state.sticks.map((s, i) =>
      `Barrette #${i + 1}: ${s.id} | bus ${s.bus_num} · adr ${s.address} · v${s.version || '?'} | ${s.num_leds} LEDs`
    ).join('\n');
  }

  /**
   * Rendu vertical : sticks côte à côte, LEDs de haut en bas.
   * L'option flipVertical (défaut: true) inverse l'ordre d'affichage
   * pour que la LED 0 (logique) soit en bas — correspondant à la
   * disposition physique des barrettes Ballistix.
   * @private
   */
  _renderVertical(ctx, sticks, state, offsetX) {
    const flip = this.flipVertical !== false;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';

    for (let si = 0; si < sticks.length; si++) {
      const stick = sticks[si];
      const x = this.PADDING + offsetX + si * (this.LED_SIZE + this.LED_GAP * 2 + 16);
      const y = this.PADDING;

      // Label court au-dessus du stick
      ctx.fillStyle = '#e94560';
      ctx.font = 'bold 12px "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText(`#${si + 1}`, x + this.LED_SIZE / 2 + this.LED_GAP, y + this.STICK_LABEL_HEIGHT - 2);

      // 8 LEDs
      const ledColors = state.colors[stick.id] || [];

      for (let vi = 0; vi < stick.num_leds; vi++) {
        // vi = index VISUEL (0=en haut, 7=en bas)
        // ledIdx = index LOGIQUE (correspond au registre physique)
        const ledIdx = flip ? (7 - vi) : vi;

        const lx = x + this.LED_GAP;
        const ly = y + this.STICK_LABEL_HEIGHT + 8 + vi * (this.LED_SIZE + this.LED_GAP);

        const color = ledColors[ledIdx] || null;
        const isSelected =
          state.selected &&
          state.selected.stick_id === stick.id &&
          state.selected.led_idx === ledIdx;
        const isHovered =
          this.hovered &&
          this.hovered.stick_id === stick.id &&
          this.hovered.led_idx === ledIdx;

        this._drawLed(ctx, lx, ly, color, isSelected, isHovered);

        // Petit numéro de LED — afficher le NUMÉRO LOGIQUE (1-8)
        ctx.fillStyle = 'rgba(255,255,255,0.25)';
        ctx.font = '7px "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(String(ledIdx + 1), lx + this.LED_SIZE / 2, ly + this.LED_SIZE + 2);
      }
    }
  }

  /**
   * Rendu horizontal : sticks empilés, LEDs de gauche à droite.
   * @private
   */
  _renderHorizontal(ctx, sticks, state, offsetX) {
    ctx.textBaseline = 'middle';

    for (let si = 0; si < sticks.length; si++) {
      const stick = sticks[si];
      const y = this.PADDING + si * (this.LED_SIZE + 30);

      // Label à gauche
      ctx.fillStyle = '#ffffff';
      ctx.font = '600 14px "Segoe UI", sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(`Barrette #${si + 1}`, this.PADDING + offsetX, y + this.LED_SIZE / 2);

      // 8 LEDs de gauche à droite
      const ledColors = state.colors[stick.id] || [];

      for (let li = 0; li < stick.num_leds; li++) {
        const lx = this.PADDING + this.LABEL_WIDTH + li * (this.LED_SIZE + this.LED_GAP);
        const ly = y;

        const color = ledColors[li] || null;
        const isSelected =
          state.selected &&
          state.selected.stick_id === stick.id &&
          state.selected.led_idx === li;
        const isHovered =
          this.hovered &&
          this.hovered.stick_id === stick.id &&
          this.hovered.led_idx === li;

        this._drawLed(ctx, lx, ly, color, isSelected, isHovered);

        // Petit numéro de LED en bas de chaque LED
        ctx.fillStyle = 'rgba(255,255,255,0.25)';
        ctx.font = '8px "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(String(li + 1), lx + this.LED_SIZE / 2, ly + this.LED_SIZE + 3);
      }
    }
  }

  /**
   * Rendu mode matrice : grille 4×8 unifiée (4 sticks × 8 LEDs).
   * Les lignes = sticks (0-3), les colonnes = LEDs dans chaque stick (0-7).
   * @private
   */
  _renderMatrix(ctx, state, width, height) {
    const matrixCols = 8;  // 8 colonnes (LEDs par stick)
    const matrixRows = 4;  // 4 lignes (sticks)
    const colors = state.colors || {};
    const sticks = state.sticks || [];

    // Calculer la taille d'une LED pour remplir le canvas
    const gap = 4;
    const availableW = width - this.PADDING * 2;
    const availableH = height - this.PADDING * 2;
    const ledSize = Math.min(
      (availableW - gap * (matrixCols - 1)) / matrixCols,
      (availableH - gap * (matrixRows - 1)) / matrixRows,
      50  // max 50px
    );
    const totalW = matrixCols * ledSize + (matrixCols - 1) * gap;
    const totalH = matrixRows * ledSize + (matrixRows - 1) * gap;
    const offsetX = (width - totalW) / 2;
    const offsetY = (height - totalH) / 2;

    for (let row = 0; row < matrixRows; row++) {
      for (let col = 0; col < matrixCols; col++) {
        const ledIdx = row * 8 + col;  // Index dans la matrice (0-31)
        const x = offsetX + col * (ledSize + gap);
        const y = offsetY + row * (ledSize + gap);

        // Trouver la couleur : stick_idx = row (0-3), led dans le stick = col (0-7)
        const stick = sticks[row];
        let color = null;
        if (stick && colors[stick.id]) {
          color = colors[stick.id][col];
        }

        if (color) {
          const [r, g, b] = color;
          ctx.fillStyle = `rgb(${r},${g},${b})`;
          ctx.shadowColor = `rgba(${r},${g},${b},0.6)`;
          ctx.shadowBlur = 8;
        } else {
          ctx.fillStyle = '#333344';
          ctx.shadowBlur = 0;
        }

        this._drawRoundedRect(ctx, x, y, ledSize, ledSize, 4);
        ctx.fill();
        ctx.shadowBlur = 0;
      }
    }
  }

  /**
   * Dessine un rectangle aux coins arrondis.
   * @param {CanvasRenderingContext2D} ctx
   * @param {number} x
   * @param {number} y
   * @param {number} w
   * @param {number} h
   * @param {number} r
   */
  _drawRoundedRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  /**
   * Dessine une LED avec effet glow.
   * @private
   */
  _drawLed(ctx, x, y, color, isSelected, isHovered) {
    const r = 5; // rayon des coins arrondis
    const s = this.LED_SIZE;

    ctx.save();

    // Chemin arrondi
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + s - r, y);
    ctx.quadraticCurveTo(x + s, y, x + s, y + r);
    ctx.lineTo(x + s, y + s - r);
    ctx.quadraticCurveTo(x + s, y + s, x + s - r, y + s);
    ctx.lineTo(x + r, y + s);
    ctx.quadraticCurveTo(x, y + s, x, y + s - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();

    // ── Remplissage ────────────────────────────────────
    if (color) {
      const [cr, cg, cb] = color;
      const isOn = cr > 0 || cg > 0 || cb > 0;

      if (isOn) {
        ctx.shadowColor = `rgb(${cr},${cg},${cb})`;
        ctx.shadowBlur = 14;
        ctx.fillStyle = `rgb(${cr},${cg},${cb})`;
      } else {
        ctx.fillStyle = '#1a1a3e';
      }
    } else {
      ctx.fillStyle = '#1a1a3e';
    }

    ctx.fill();

    // ── Bordure ────────────────────────────────────────
    ctx.shadowBlur = 0;
    ctx.lineWidth = 1.5;
    if (isSelected) {
      ctx.strokeStyle = '#e94560';
      ctx.lineWidth = 2.5;
      ctx.shadowColor = 'rgba(233, 69, 96, 0.5)';
      ctx.shadowBlur = 8;
    } else if (isHovered) {
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.7)';
      ctx.lineWidth = 2;
    } else if (color) {
      const [cr, cg, cb] = color;
      ctx.strokeStyle = `rgba(${cr},${cg},${cb}, 0.4)`;
    } else {
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    }
    ctx.stroke();

    // ── Intérieur : léger reflet ───────────────────────
    if (color) {
      const [cr, cg, cb] = color;
      const isOn = cr > 0 || cg > 0 || cb > 0;
      if (isOn) {
        ctx.shadowBlur = 0;
        const gradient = ctx.createLinearGradient(x, y, x, y + s);
        gradient.addColorStop(0, 'rgba(255,255,255,0.18)');
        gradient.addColorStop(0.4, 'rgba(255,255,255,0.02)');
        gradient.addColorStop(1, 'rgba(0,0,0,0.12)');
        ctx.fillStyle = gradient;
        ctx.fill();
      }
    }

    ctx.restore();
  }
}

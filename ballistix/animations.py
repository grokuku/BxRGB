#!/usr/bin/env python3
"""
ballistix/animations.py — Moteur d'animations pour le mode matrice.

Permet de piloter les 32 LEDs (4 barrettes × 8 LEDs) comme une seule
surface matricielle, avec des effets animés dans un thread séparé.

Effets implémentés :
  - incandescence : flamme/feu avec interpolation linéaire (porté depuis FireV1.ino)
  - rainbow : arc-en-ciel qui défile horizontalement sur la matrice 4×8
"""

import time
import threading
import random
from typing import List, Callable, Optional


class AnimationEngine:
    """Moteur d'animations qui tourne dans un thread séparé.

    Le moteur calcule des couleurs pour chaque LED de la matrice (4×8 = 32 LEDs)
    et appelle un callback pour les appliquer aux sticks physiques.

    Usage :
        def apply(colors):
            for stick_idx, stick in enumerate(sticks):
                for led_idx in range(8):
                    r, g, b = colors[stick_idx * 8 + led_idx]
                    stick.colors[led_idx] = (r, g, b)
                stick.send_direct_colors()

        engine = AnimationEngine(apply)
        engine.start("incandescence")
        ...
        engine.stop()
    """

    def __init__(self, apply_callback: Callable[[List[List[int]]], None]):
        """
        Args:
            apply_callback: fonction appelée à chaque frame avec une liste de
                            32 couleurs [[R,G,B], [R,G,B], ...] pour les 32 LEDs.
        """
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._apply = apply_callback
        self._lock = threading.Lock()
        self._speed = 1.0
        self._framerate = 30
        self._effect = "none"
        self._params: dict = {}

    # ── Contrôle du moteur ────────────────────────────────────

    def start(self, effect: str = "incandescence", params: dict = None,
               speed: float = 1.0, framerate: int = 30):
        """Démarre l'animation dans un thread séparé.

        Args:
            effect: nom de l'effet (ex: "incandescence")
            params: dictionnaire de paramètres spécifiques à l'effet
            speed: multiplicateur de vitesse (0.1 = lent, 10.0 = rapide)
            framerate: images par seconde (max 30 pour SMBus)
        """
        with self._lock:
            if self._running:
                self.stop()
            self._effect = effect
            self._params = params or {}
            self._speed = max(0.05, min(10.0, speed))
            self._framerate = max(1, min(30, framerate))
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        """Arrête l'animation et attend la fin du thread."""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def effect(self) -> str:
        return self._effect

    # ── Ajustements dynamiques ────────────────────────────────

    def set_speed(self, speed: float):
        """Multiplicateur de vitesse (0.1 = lent, 10.0 = rapide, 1.0 = normal)."""
        self._speed = max(0.1, min(10.0, speed))

    def set_framerate(self, fps: int):
        """Images par seconde (1-30 max pour SMBus)."""
        self._framerate = max(1, min(30, fps))

    # ── Boucle principale ─────────────────────────────────────

    def _run(self):
        """Boucle principale — dispatche vers l'effet choisi."""
        try:
            if self._effect == "incandescence":
                self._run_incandescence()
            elif self._effect == "rainbow":
                self._run_rainbow()
            else:
                # Effet inconnu → ne rien faire
                pass
        except Exception as e:
            print(f"⚠ Animation engine error ({self._effect}): {e}")
        finally:
            with self._lock:
                self._running = False

    # ── Effet : Incandescence (feu) ───────────────────────────

    def _run_incandescence(self):
        """Effet d'incandescence/feu — basé sur le script Arduino FireV1.ino.

        Principe :
          1. Générer des couleurs aléatoires autour d'une couleur de base
             pour chaque pixel (état destination).
          2. Interpoler linéairement de l'état précédent (source) vers
             l'état destination sur N pas.
          3. Appliquer les couleurs interpolées aux LEDs à chaque pas.
          4. L'état destination devient la nouvelle source, et on recommence.
        """
        # ── Constantes de l'effet ─────────────────────────────
        BASE_R = 100   # base R
        BASE_G = 20    # base G
        BASE_B = 0     # base B
        RANGE_R = 154  # variation aléatoire R
        RANGE_G = 45   # variation aléatoire G
        RANGE_B = 0    # variation aléatoire B

        NUM_PIXELS = 32  # 4 barrettes × 8 LEDs
        DELAY = 100      # pas d'interpolation (entre 2 et 100)

        # Adapter le nombre de pas selon la vitesse
        # Plus DELAY est grand, plus la transition est longue
        # speed=1.0 → delay_steps = DELAY (40)
        # speed=2.0 → delay_steps = 20 (2× plus rapide)
        # speed=0.5 → delay_steps = 80 (2× plus lent)
        delay_steps = max(2, int(DELAY / self._speed))

        # États source (couleurs actuelles) et destination (cibles aléatoires)
        src_colors = [[0, 0, 0] for _ in range(NUM_PIXELS)]
        dest_colors = [[0, 0, 0] for _ in range(NUM_PIXELS)]

        # Pré-générer des destinations aléatoires pour la première itération
        for i in range(NUM_PIXELS):
            dest_colors[i][0] = max(0, min(255, BASE_R + random.randint(0, RANGE_R)))
            dest_colors[i][1] = max(0, min(255, BASE_G + random.randint(0, RANGE_G)))
            dest_colors[i][2] = max(0, min(255, BASE_B + random.randint(0, RANGE_B)))

        # Debug logging
        frame_count = 0
        last_log = time.time()

        while self._running:
            # ── 1. Générer les couleurs de destination ────────
            # Mélanger aléatoire + couleur précédente pour plus de douceur
            blend = 0.3  # 30% nouveau, 70% ancien
            for i in range(NUM_PIXELS):
                new_r = BASE_R + random.randint(0, RANGE_R)
                new_g = BASE_G + random.randint(0, RANGE_G)
                new_b = BASE_B + random.randint(0, RANGE_B)

                dest_colors[i][0] = max(0, min(255,
                    int(dest_colors[i][0] * (1 - blend) + new_r * blend)))
                dest_colors[i][1] = max(0, min(255,
                    int(dest_colors[i][1] * (1 - blend) + new_g * blend)))
                dest_colors[i][2] = max(0, min(255,
                    int(dest_colors[i][2] * (1 - blend) + new_b * blend)))

            # ── 2. Interpolation linéaire src → dest ──────────
            # Écrire sur SMBus ~10 fois/sec max (pas à chaque frame)
            smbus_write_interval = 1  # Écrire à chaque frame (le garde-fou SMBus gère le débit)

            print(f"  Cycle: {delay_steps} steps, {delay_steps/self._framerate:.1f}s, "
                  f"écriture SMBus tous les {smbus_write_interval} steps")

            for step in range(delay_steps):
                if not self._running:
                    return  # Arrêt demandé

                t = step / delay_steps  # 0.0 → 1.0
                frame = []
                for i in range(NUM_PIXELS):
                    r = int(src_colors[i][0] + (dest_colors[i][0] - src_colors[i][0]) * t)
                    g = int(src_colors[i][1] + (dest_colors[i][1] - src_colors[i][1]) * t)
                    b = int(src_colors[i][2] + (dest_colors[i][2] - src_colors[i][2]) * t)
                    frame.append([
                        max(0, min(255, r)),
                        max(0, min(255, g)),
                        max(0, min(255, b)),
                    ])

                # ── 3. Appliquer via le callback ──────────────
                # N'appliquer au SMBus que toutes les N frames
                if step % smbus_write_interval == 0:
                    try:
                        self._apply(frame)
                    except Exception as e:
                        print(f"⚠ Animation apply error: {e}")

                # ── Attendre selon le framerate ───────────────
                time.sleep(1.0 / self._framerate)

                # ── Debug : logger le frametime réel ──────────
                frame_count += 1
                if time.time() - last_log > 5:
                    actual_fps = frame_count / (time.time() - last_log)
                    print(f"  Animation incandescence: {actual_fps:.1f} fps réel, "
                          f"{self._speed:.2f}× vitesse, delay_steps={delay_steps}, "
                          f"framerate_cible={self._framerate} fps")
                    frame_count = 0
                    last_log = time.time()

            # ── 4. Toujours appliquer la dernière frame ─────
            try:
                self._apply(frame)
            except Exception as e:
                print(f"⚠ Animation apply error: {e}")

            # ── 5. dest devient src pour le prochain cycle ────
            src_colors, dest_colors = dest_colors, src_colors

    # ── Effet : Arc-en-ciel ───────────────────────────────

    def _run_rainbow(self):
        """Arc-en-ciel qui défile horizontalement sur la matrice 4×8.

        Principe :
          - Chaque LED a une teinte (hue) qui dépend de sa position
          - La teinte défile dans le temps pour créer un mouvement
          - Conversion HSV → RGB pour chaque LED à chaque frame
        """
        import math

        NUM_COLS = 8   # 8 LEDs par stick (colonne)
        NUM_ROWS = 4   # 4 sticks (ligne)
        NUM_PIXELS = NUM_COLS * NUM_ROWS  # 32

        # Vitesse de défilement : la teinte avance de X degrés par frame
        # speed=1.0 → 2 degrés/frame → 180 frames pour un cycle complet (6s @ 30fps)
        hue_step = 2.0 * self._speed  # degrés de teinte par frame
        hue_offset = 0.0  # décalage de teinte qui augmente à chaque frame

        # Écriture SMBus ~10 fois/sec max
        smbus_write_interval = 1  # Écrire à chaque frame (le garde-fou SMBus gère le débit)

        frame_count = 0
        last_log = time.time()

        while self._running:
            frame = []
            for row in range(NUM_ROWS):
                for col in range(NUM_COLS):
                    idx = row * NUM_COLS + col
                    # Teinte : dépend de la colonne + défilement temporel
                    hue = (col / NUM_COLS * 360.0 + hue_offset) % 360.0

                    # HSV → RGB (S=1.0, V=1.0)
                    h = hue / 60.0
                    sector = int(h) % 6
                    f = h - int(h)
                    q = 1.0 - f
                    t_val = f

                    if sector == 0:
                        r, g, b = 255, int(t_val * 255), 0
                    elif sector == 1:
                        r, g, b = int(q * 255), 255, 0
                    elif sector == 2:
                        r, g, b = 0, 255, int(t_val * 255)
                    elif sector == 3:
                        r, g, b = 0, int(q * 255), 255
                    elif sector == 4:
                        r, g, b = int(t_val * 255), 0, 255
                    else:
                        r, g, b = 255, 0, int(q * 255)

                    frame.append([r, g, b])

            # Appliquer
            if frame_count % smbus_write_interval == 0:
                try:
                    self._apply(frame)
                except Exception as e:
                    print(f"⚠ Rainbow apply error: {e}")

            # Avancer la teinte
            hue_offset = (hue_offset + hue_step) % 360.0

            # Debug
            frame_count += 1
            if time.time() - last_log > 5:
                actual_fps = frame_count / (time.time() - last_log)
                print(f"  Animation rainbow: {actual_fps:.1f} fps réel, "
                      f"{self._speed:.2f}× vitesse, hue_offset={hue_offset:.0f}°")
                frame_count = 0
                last_log = time.time()

            time.sleep(1.0 / self._framerate)


# ── Test indépendant (si exécuté directement) ──────────────────

if __name__ == "__main__":
    print("🧪 Test AnimationEngine (incandescence) — 3 secondes")
    print("    Appuie sur Ctrl+C pour arrêter plus tôt\n")

    def mock_apply(colors: List[List[int]]):
        """Callback factice : affiche la moyenne des couleurs."""
        avg_r = sum(c[0] for c in colors) // len(colors)
        avg_g = sum(c[1] for c in colors) // len(colors)
        avg_b = sum(c[2] for c in colors) // len(colors)
        print(f"  Frame: 32 LEDs, moyenne RGB({avg_r:3d}, {avg_g:3d}, {avg_b:3d})", end="\r")

    engine = AnimationEngine(mock_apply)
    engine.start("incandescence")

    try:
        time.sleep(3.0)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        print("\n✅ Animation arrêtée")

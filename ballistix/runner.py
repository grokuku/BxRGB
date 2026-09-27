#!/usr/bin/env python3
"""
ballistix/runner.py — Moteur d'animation : boucle + transport, sans rendu.

Le rendu vit dans ``ballistix/effects.py`` (fonctions pures). Ici :

  - ``EffectRunner`` fait tourner un effet dans un thread daemon ;
  - la VITESSE est relue à CHAQUE frame (``phase += dt * speed``) : un
    changement à chaud prend effet immédiatement, sans redémarrer l'effet ;
  - le FRAMERATE cible et le REFRESH (rate-limit d'écriture) sont relus
    à chaque frame également ;
  - l'horloge et le sommeil sont injectables (tests à horloge virtuelle) ;
  - le transport matériel est une simple interface ``writer(colors)``,
    mockable : le runner ne connaît ni SMBus ni FastAPI.

Le runner expose ses statistiques via ``stats()`` : fps réel, phase
courante, durée de cycle réelle (paramètre de cycle ÷ vitesse).
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .effects import (
    cycle_seconds as effect_cycle_seconds,
    normalize_params,
    render,
)

# ── Bornes UNIQUES du sous-système lighting (utilisées par l'API,
#    la config et le moteur : plus de divergence démarrage/à-chaud). ──
SPEED_MIN, SPEED_MAX = 0.1, 10.0
FRAMERATE_MIN, FRAMERATE_MAX = 1, 30
REFRESH_MIN, REFRESH_MAX = 1.0, 30.0

# Une pause d'horloge supérieure à cette valeur ne doit pas provoquer un
# saut de phase visible (mise en veille du process, debugger…).
MAX_FRAME_DT = 1.0

# Cadence de mise à jour des stats fps (fenêtre glissante, en secondes).
FPS_WINDOW = 0.5


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp_speed(value: Any) -> float:
    """Borne une vitesse dans [0.1, 10.0] (défaut 1.0 si illisible)."""
    return max(SPEED_MIN, min(SPEED_MAX, _as_float(value, 1.0)))


def clamp_framerate(value: Any) -> int:
    """Borne un framerate dans [1, 30] (défaut 30 si illisible)."""
    return int(max(FRAMERATE_MIN, min(FRAMERATE_MAX, _as_float(value, 30))))


def clamp_refresh(value: Any) -> float:
    """Borne un refresh SMBus dans [1.0, 30.0] (défaut 20.0 si illisible)."""
    return max(REFRESH_MIN, min(REFRESH_MAX, _as_float(value, 20.0)))


class EffectRunner:
    """Fait tourner un effet pur dans un thread, cadencé et rate-limité.

    Usage :
        runner = EffectRunner("rainbow", {}, base_colors, writer,
                              speed=1.0, framerate=30, refresh=20)
        runner.start()
        ...
        runner.set_speed(2.0)   # appliqué à la frame suivante
        runner.stop()
    """

    def __init__(self,
                 effect_id: str,
                 params: Optional[Dict[str, Any]],
                 base_colors: Union[Sequence[Sequence[int]],
                                    Callable[[], Sequence[Sequence[int]]]],
                 writer: Callable[[List[List[int]]], None],
                 *,
                 speed: float = 1.0,
                 framerate: int = 30,
                 refresh: float = 20.0,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        """
        Args:
            effect_id: identifiant d'effet (registre ``EFFECTS``).
            params: paramètres bruts de l'effet (normalisés ici).
            base_colors: séquence de couleurs OU callable la relisant à
                chaque frame (pour refléter une édition utilisateur à chaud).
            writer: transport matériel ``writer(frame) -> None``, appelé au
                plus ``refresh`` fois par seconde.
            speed: multiplicateur de vitesse (0.1–10.0).
            framerate: cadence de rendu cible (1–30 fps).
            refresh: écritures matérielles max par seconde (1–30 Hz).
            clock: horloge monotone injectable (défaut ``time.monotonic``).
            sleep: sommeil injectable (défaut ``time.sleep``).

        Raises:
            UnknownEffectError: si ``effect_id`` n'existe pas.
        """
        # Valide l'effet AVANT toute allocation/thread : échec explicite.
        self._params = normalize_params(effect_id, params or {})
        self._effect = effect_id
        self._base_colors = base_colors
        self._writer = writer
        self._speed = clamp_speed(speed)
        self._framerate = clamp_framerate(framerate)
        self._refresh = clamp_refresh(refresh)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._error: Optional[BaseException] = None

        # Statistiques (lues/écrites sous GIL : scalaires simples).
        self._phase = 0.0
        self._frames = 0
        self._writes = 0
        self._fps = 0.0
        self._fps_window_start = 0.0
        self._fps_window_frames = 0
        self._last_write_at: Optional[float] = None

    # ── Contrôle ──────────────────────────────────────────────

    def start(self) -> None:
        """Démarre la boucle dans un thread daemon (idempotent)."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._error = None
            self._phase = 0.0
            self._frames = 0
            self._writes = 0
            self._fps = 0.0
            self._last_write_at = None
            now = self._clock()
            self._fps_window_start = now
            self._fps_window_frames = 0
            self._thread = threading.Thread(
                target=self._run, daemon=True, name=f"effect-{self._effect}")
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Demande l'arrêt et attend la fin du thread."""
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                print(f"⚠ Runner {self._effect} : arrêt non terminé "
                      f"(timeout {timeout}s)")
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def effect(self) -> str:
        return self._effect

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def framerate(self) -> int:
        return self._framerate

    @property
    def refresh(self) -> float:
        return self._refresh

    @property
    def error(self) -> Optional[BaseException]:
        """Dernière exception fatale de la boucle (None si tout va bien)."""
        return self._error

    # ── Réglages à chaud (relus à chaque frame) ──────────────

    def set_speed(self, speed: float) -> float:
        """Change la vitesse ; prend effet à la frame suivante."""
        self._speed = clamp_speed(speed)
        return self._speed

    def set_framerate(self, framerate: int) -> int:
        """Change la cadence de rendu cible ; effective immédiatement."""
        self._framerate = clamp_framerate(framerate)
        return self._framerate

    def set_refresh(self, refresh: float) -> float:
        """Change le rate-limit d'écriture ; effectif immédiatement."""
        self._refresh = clamp_refresh(refresh)
        return self._refresh

    # ── Statistiques ──────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Photo des compteurs/ mesures du runner."""
        cycle = effect_cycle_seconds(self._effect, self._params)
        if cycle is not None:
            cycle = cycle / self._speed  # durée RÉELLE à la vitesse courante
        return {
            "effect": self._effect,
            "running": self._running,
            "frames": self._frames,
            "writes": self._writes,
            "fps": round(self._fps, 2),
            "phase": round(self._phase, 4),
            "speed": self._speed,
            "framerate": self._framerate,
            "refresh": self._refresh,
            "cycle_seconds": round(cycle, 4) if cycle is not None else None,
            "error": str(self._error) if self._error else None,
        }

    # ── Boucle ────────────────────────────────────────────────

    def _read_base_colors(self) -> Sequence[Sequence[int]]:
        if callable(self._base_colors):
            return self._base_colors()
        return self._base_colors

    def _run(self) -> None:
        """Boucle de rendu — vitesse/framerate/refresh relus à chaque frame."""
        clock = self._clock
        sleep = self._sleep
        last = clock()
        try:
            while self._running:
                now = clock()
                dt = now - last
                last = now
                if dt < 0.0:
                    dt = 0.0
                elif dt > MAX_FRAME_DT:
                    dt = MAX_FRAME_DT

                # ⚠ LA VITESSE EST LUE ICI, À CHAQUE FRAME.
                self._phase += dt * self._speed

                frame = render(self._effect, self._phase, self._params,
                               self._read_base_colors())
                self._frames += 1

                refresh = self._refresh
                if (self._last_write_at is None
                        or (now - self._last_write_at) >= 1.0 / refresh):
                    try:
                        self._writer(frame)
                        self._writes += 1
                    except Exception as exc:  # transport : on ne tue pas l'effet
                        print(f"⚠ Écriture frame {self._effect}: {exc}")
                    self._last_write_at = now

                # fps réel (fenêtre glissante sur l'horloge moteur).
                self._fps_window_frames += 1
                elapsed = now - self._fps_window_start
                if elapsed >= FPS_WINDOW:
                    self._fps = self._fps_window_frames / elapsed
                    self._fps_window_start = now
                    self._fps_window_frames = 0

                # Cadence cible relue à chaque frame.
                interval = 1.0 / self._framerate
                frame_cost = clock() - now
                wait = interval - frame_cost
                if wait > 0.0:
                    sleep(wait)
        except Exception as exc:
            self._error = exc
            print(f"⚠ Animation engine error ({self._effect}): {exc}")
        finally:
            self._running = False


__all__ = [
    "EffectRunner",
    "SPEED_MIN",
    "SPEED_MAX",
    "FRAMERATE_MIN",
    "FRAMERATE_MAX",
    "REFRESH_MIN",
    "REFRESH_MAX",
    "MAX_FRAME_DT",
    "FPS_WINDOW",
    "clamp_speed",
    "clamp_framerate",
    "clamp_refresh",
]

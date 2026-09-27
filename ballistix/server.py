#!/usr/bin/env python3
"""
ballistix/server.py — Serveur web FastAPI pour le contrôle des LEDs
Crucial Ballistix via API REST + WebSocket.

Usage :
    python -m ballistix.server
    # ou depuis cli.py avec --web
"""

import asyncio
import os
import sys
import time
import threading
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Union, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import CrucialStick, ok, fail, warn
from .detect import detect_sticks
from .effects import (
    MODE_IDS,
    UnknownEffectError,
    cycle_seconds as effect_cycle_seconds,
    describe_effects,
    normalize_params,
)
from .runner import EffectRunner, clamp_framerate, clamp_refresh, clamp_speed
from .kraken import (
    kraken_available, kraken_detect, kraken_status,
    kraken_initialize, kraken_set_lcd_mode, kraken_set_lcd_image,
    kraken_set_lcd_brightness, kraken_set_lcd_orientation,
    kraken_save_image, kraken_decode_b64,
    kraken_gallery_list, kraken_gallery_add, kraken_gallery_delete,
    kraken_gallery_start, kraken_monitor_start, kraken_stop_display,
    kraken_monitor_preview,
    kraken_theme_thumb, kraken_purge_thumbs,
    kraken_palette_thumb, kraken_layout_thumb,
    kraken_get_settings, kraken_update_settings, kraken_display_status,
    kraken_display_reconfigure, kraken_pending_changes,
    kraken_commit_file_changes, kraken_restore_file_changes,
)

# ── Version du daemon (exposée par GET /api/status) ──────────
DAEMON_VERSION = "0.0.1"

# ── Modèles Pydantic ──────────────────────────────────────────

class ColorsBody(BaseModel):
    """Liste de couleurs [[R,G,B], ...] pour un stick."""
    leds: List[List[int]]

class BrightnessBody(BaseModel):
    """Niveau de luminosité 0-255."""
    level: int


class SaveBody(BaseModel):
    """État client optionnel joint à POST /api/save.

    Le réordonnancement drag&drop vit côté front ; ``stick_order``
    (liste de ``"bus:0xaddr"``) permet de l'appliquer au modèle serveur
    juste avant de figer la référence.
    """
    stick_order: Optional[List[str]] = None


# ── Référence persistée (Save / Cancel) ───────────────────────

def _reference_payload(config: dict) -> dict:
    """Construit la RÉFÉRENCE exposée par GET /api/saved.

    Contenu : couleurs de BASE par stick, luminosité globale, ordre des
    sticks, section ``lighting`` (mode/running/speed/framerate/refresh/
    params) + miroir legacy ``animation``, et section kraken (lcd +
    display). Les frames d'animation n'y figurent JAMAIS : c'est exactement
    ce que fige POST /api/save et ce que ré-applique POST /api/restore.
    """
    from .config import lighting_from_animation, normalize_lighting

    lighting = config.get("lighting")
    if not isinstance(lighting, dict) or not lighting:
        # config.json pas passé par load()/migration : repli explicite.
        lighting = lighting_from_animation(config.get("animation"))
    lighting = normalize_lighting(lighting)

    kraken_cfg = config.get("kraken") or {}
    return {
        "colors": {
            str(key): [list(c) for c in value]
            for key, value in (config.get("colors") or {}).items()
            if isinstance(value, (list, tuple))
        },
        "brightness": config.get("brightness", 255),
        "stick_order": [str(item) for item in (config.get("stick_order") or [])],
        "lighting": lighting,
        # Miroir de rétrocompatibilité (ancien nommage, front pas encore migré).
        "animation": {
            "speed": lighting["speed"],
            "framerate": lighting["framerate"],
            "refresh": lighting["refresh"],
            "enabled": lighting["running"],
            "type": lighting["mode"],
        },
        "kraken": {
            "lcd": dict(kraken_cfg.get("lcd") or {}),
            "display": dict(kraken_cfg.get("display") or {}),
        },
    }


def _restore_kraken_hardware(lcd: dict, display: dict) -> None:
    """Ré-applique la référence Kraken au matériel (best-effort).

    liquidctl peut être absent (pas d'erreur fatale) : l'état mémoire est
    aligné séparément par l'appelant.
    """
    try:
        if "brightness" in lcd:
            kraken_set_lcd_brightness(lcd["brightness"])
        if "orientation" in lcd:
            kraken_set_lcd_orientation(lcd["orientation"])
        if lcd.get("mode"):
            kraken_set_lcd_mode(lcd["mode"])
        mode = display.get("mode")
        if mode == "monitor":
            kraken_monitor_start(
                display.get("interval", 10.0),
                theme=display.get("theme"),
                palette=display.get("palette"),
                layout=display.get("layout"),
                options=display.get("options"),
            )
        elif mode == "gallery":
            kraken_gallery_start(display.get("interval", 10.0))
        else:
            kraken_stop_display()
    except Exception as e:
        print(f"⚠ Restauration Kraken (matériel) partielle : {e}")

# ── Gestionnaire WebSocket ────────────────────────────────────

class WSManager:
    """Gère les connexions WebSocket et le broadcast à tous les clients."""

    def __init__(self):
        self.connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, message: dict):
        """Envoie un message JSON à tous les clients connectés."""
        stale = []
        for conn in self.connections[:]:
            try:
                await conn.send_json(message)
            except Exception:
                stale.append(conn)
        for conn in stale:
            self.disconnect(conn)

# ── Gestionnaire SMBus (thread-safe) ──────────────────────────

class SMBusManager:
    """Encapsule l'accès aux bus SMBus avec un lock thread.

    Toutes les opérations SMBus sont bloquantes ; le lock garantit
    qu'une seule opération à la fois traverse le bus, évitant les
    corruptions de registres lorsque plusieurs requêtes arrivent
    simultanément (REST + WebSocket).

    Supporte aussi le mode matrice : un ``EffectRunner`` (ballistix/runner)
    anime les 32 LEDs (4 barrettes × 8 LEDs) et écrit ses frames via
    ``CrucialStick.write_colors`` sans jamais toucher aux couleurs de base.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sticks: List[CrucialStick] = []
        self._bus_map: Dict[str, CrucialStick] = {}
        self._manual_devices: List[dict] = []
        # Moteur d'animation matriciel (None = arrêté).
        self._runner: Optional[EffectRunner] = None
        self._save_timer: Optional[threading.Timer] = None
        self._smbus_refresh_rate: float = 20.0  # écritures SMBus/sec pour les animations
        # Réglages lumière COURANTS (mémorisés même moteur arrêté : la
        # référence persistée les compare dans /api/saved).
        self._anim_speed: float = 1.0
        self._anim_framerate: int = 30
        # Mode courant ("static" ou id d'effet) et paramètres d'effet.
        self._lighting_mode: str = "static"
        self._lighting_params: dict = {}
        # Seams de test : horloge/sommeil injectables (horloge virtuelle).
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep
        # Dernière luminosité connue (secours si aucun stick).
        self._last_brightness: int = 255

    def set_smbus_refresh_rate(self, rate: float) -> float:
        """Définit le taux max d'écriture SMBus en animations (1-30 Hz)."""
        self._smbus_refresh_rate = clamp_refresh(rate)
        if self._runner is not None:
            self._runner.set_refresh(self._smbus_refresh_rate)
        return self._smbus_refresh_rate

    def animation_settings(self) -> dict:
        """Réglages d'animation courants (speed/framerate/refresh)."""
        return {
            "speed": self._anim_speed,
            "framerate": self._anim_framerate,
            "refresh": self._smbus_refresh_rate,
        }

    def lighting_settings(self) -> dict:
        """Section ``lighting`` courante (mode, état, réglages, params)."""
        return {
            "mode": self._lighting_mode,
            "running": self.animation_running,
            "speed": self._anim_speed,
            "framerate": self._anim_framerate,
            "refresh": self._smbus_refresh_rate,
            "params": dict(self._lighting_params),
        }

    def set_lighting_state(self, lighting: dict) -> dict:
        """Copie une section ``lighting`` en mémoire, SANS toucher au moteur.

        Retourne la section normalisée/bornée ; l'appelant décide ensuite de
        démarrer ou d'arrêter l'effet (reprise au boot, POST /api/restore).
        """
        from .config import normalize_lighting

        normalized = normalize_lighting(lighting)
        self._anim_speed = normalized["speed"]
        self._anim_framerate = normalized["framerate"]
        self._smbus_refresh_rate = normalized["refresh"]
        self._lighting_mode = normalized["mode"]
        self._lighting_params = dict(normalized["params"])
        return normalized

    def _schedule_save(self):
        """Programme une sauvegarde différée (debounced 2s).

        ⚠ DÉPRÉCIÉ : l'auto-save a été retiré (la persistance ne se fait QUE
        via POST /api/save). Conservé pour un usage explicite éventuel.
        """
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(2.0, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _do_save(self):
        """Sauvegarde explicite (dépréciée) — délègue à save_current()."""
        try:
            self.save_current()
        except Exception as e:
            print(f"⚠ Auto-save error: {e}")

    def set_manual_devices(self, devices: List[dict]):
        """Définit les devices manuels à ajouter après le scan.

        Args:
            devices: Liste de dicts {"bus": int, "addr": int}
        """
        self._manual_devices = devices

    # ── Scan ──────────────────────────────────────────────────

    def scan(self) -> List[dict]:
        """Scanne les bus SMBus et retourne l'état des sticks détectés.

        Ferme proprement les anciens sticks avant de rescanner.
        Ajoute les devices manuels définis via set_manual_devices()
        et ceux persistés dans le fichier de configuration.

        L'ordre des sticks est restauré depuis ``config["stick_order"]``
        (liste de strings ``"bus:0xaddr"``).

        RÈGLE IMPORTANTE (auto-save coupé) : un re-scan ne doit PAS être un
        Cancel implicite. Les couleurs et la luminosité COURANTES des sticks
        déjà connus (même ``bus:0xaddr``) sont préservées ; la configuration
        persistée n'est appliquée qu'aux sticks NOUVELLEMENT détectés.
        L'ordre courant n'est plus persisté ici — seul POST /api/save le fait.
        """
        with self._lock:
            from .config import load as load_config
            from smbus2 import SMBus

            # ── 0. Mémoriser l'état courant des sticks connus ──
            previous = {
                f"{s.bus_num}:{hex(s.address).lower()}": {
                    "colors": [tuple(c) for c in s.colors],
                    "brightness": s.get_brightness(),
                }
                for s in self._sticks
            }

            # Fermer les anciens bus
            for old in self._sticks:
                try:
                    old.bus.close()
                except Exception:
                    pass
            self._sticks = []
            self._bus_map = {}

            # ── 1. Scan auto ──
            sticks = detect_sticks()

            # ── 2. Charger la config persistée ──
            config = load_config()

            # ── 3. Ajouter les devices manuels depuis la config ──
            for dev in config.get("manual_devices", []):
                bus_num = dev["bus"]
                addr = dev["addr"]
                already = any(s.bus_num == bus_num and s.address == addr for s in sticks)
                if not already:
                    try:
                        bus = SMBus(bus_num)
                        new_stick = CrucialStick(bus, addr, bus_num, version="Config")
                        sticks.append(new_stick)
                        print(f"  → Config : i2c-{bus_num} @ 0x{addr:02X}")
                    except Exception as e:
                        print(f"  ⚠ Config {bus_num}:{hex(addr)} - {e}")

            # ── 4. Ajouter les devices manuels depuis la CLI ──
            for dev in self._manual_devices:
                bus_num = dev["bus"]
                addr = dev["addr"]
                already = any(s.bus_num == bus_num and s.address == addr for s in sticks)
                if not already:
                    try:
                        bus = SMBus(bus_num)
                        new_stick = CrucialStick(bus, addr, bus_num, version="Manuelle")
                        sticks.append(new_stick)
                        print(f"  → Manuel : i2c-{bus_num} @ 0x{addr:02X}")
                    except Exception as e:
                        print(f"  ⚠ Erreur ajout manuel {bus_num}:{hex(addr)} - {e}")

            # ── 5. Restaurer l'ordre des sticks depuis la config ──
            try:
                saved_order = config.get("stick_order", [])
                if saved_order:
                    ordered = []
                    for addr_str in saved_order:
                        try:
                            parts = addr_str.split(":")
                            bus_num = int(parts[0])
                            addr = int(parts[1], 16)
                            for s in sticks:
                                if s.bus_num == bus_num and s.address == addr and s not in ordered:
                                    ordered.append(s)
                                    break
                        except Exception:
                            pass
                    for s in sticks:
                        if s not in ordered:
                            ordered.append(s)
                    sticks = ordered
            except Exception as e:
                print(f"⚠ Erreur restauration ordre: {e}")

            self._sticks = sticks
            self._bus_map = {f"stick_{i}": s for i, s in enumerate(sticks)}

            # ── 6. Couleurs : préserver le COURANT des sticks connus,
            #        n'appliquer la config qu'aux NOUVEAUX sticks ──
            saved_colors = config.get("colors", {})
            for i, stick in enumerate(self._sticks):
                key = f"{stick.bus_num}:{hex(stick.address).lower()}"
                prev = previous.get(key)
                if prev is not None:
                    for led_idx, color in enumerate(prev["colors"][:stick.num_leds]):
                        stick.colors[led_idx] = tuple(color)
                else:
                    stick_id = f"stick_{i}"
                    if stick_id in saved_colors:
                        for led_idx, color in enumerate(saved_colors[stick_id]):
                            if led_idx < stick.num_leds:
                                stick.colors[led_idx] = tuple(color)
                # Ré-appliquer les couleurs (préservées ou persistées) au matériel
                try:
                    stick.send_direct_colors()
                except Exception:
                    pass

            # ── 7. Luminosité : préserver le courant des sticks connus,
            #        appliquer la config persistée aux nouveaux ──
            for stick in self._sticks:
                key = f"{stick.bus_num}:{hex(stick.address).lower()}"
                prev = previous.get(key)
                try:
                    stick.set_brightness(
                        prev["brightness"] if prev is not None
                        else config.get("brightness", 255)
                    )
                except Exception:
                    pass
            if self._sticks:
                try:
                    self._last_brightness = self._sticks[0].get_brightness()
                except Exception:
                    pass

            # Note : l'ordre courant n'est PLUS persisté ici (auto-save
            # supprimé) — il sera écrit au prochain POST /api/save.

            return self._get_status()

    # ── État ──────────────────────────────────────────────────

    def _get_status(self) -> List[dict]:
        """Retourne une liste sérialisable de tous les sticks."""
        return [
            {
                "id": f"stick_{i}",
                "label": s.label,
                "bus_num": s.bus_num,
                "address": hex(s.address),
                "version": s.get_version(),
                "num_leds": s.num_leds,
                "brightness": s.get_brightness(),
            }
            for i, s in enumerate(self._sticks)
        ]

    def get_stick(self, stick_id: str) -> Optional[CrucialStick]:
        """Retourne le stick par son ID (ex: 'stick_0')."""
        return self._bus_map.get(stick_id)

    # ── Opérations thread-safe ─────────────────────────────────

    def apply_colors(self, stick_id: str, leds: List[List[int]]) -> bool:
        """Applique une liste de couleurs à un stick."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            for i, (r, g, b) in enumerate(leds):
                if i < stick.num_leds:
                    stick.colors[i] = (
                        min(255, max(0, int(r))),
                        min(255, max(0, int(g))),
                        min(255, max(0, int(b))),
                    )
            stick.send_direct_colors()
        # NOTE : plus d'auto-save ici — la persistance passe uniquement par
        # POST /api/save (l'application au matériel reste temps réel).
        return True

    def set_led(self, stick_id: str, led_idx: int,
                r: int, g: int, b: int) -> bool:
        """Change la couleur d'une LED unique (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            stick.set_led(led_idx, r, g, b)
        return True

    def set_all_leds(self, stick_id: str, r: int, g: int, b: int) -> bool:
        """Met toutes les LEDs d'un stick à la même couleur (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            stick.set_all_leds(r, g, b)
        return True

    def set_brightness(self, stick_id: str, level: int) -> bool:
        """Change la luminosité d'un stick (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        level = min(255, max(0, int(level)))
        with self._lock:
            stick.set_brightness(level)
        self._last_brightness = level
        return True

    def set_all_brightness(self, level: int):
        """Change la luminosité de tous les sticks (thread-safe)."""
        level = min(255, max(0, int(level)))
        with self._lock:
            for stick in self._sticks:
                stick.set_brightness(level)
        self._last_brightness = level

    def apply_all(self):
        """Applique les couleurs actuelles de tous les sticks (thread-safe)."""
        with self._lock:
            for stick in self._sticks:
                stick.send_direct_colors()

    def get_colors(self) -> Dict[str, List[List[int]]]:
        """Retourne les couleurs actuelles de tous les sticks."""
        with self._lock:
            return {
                f"stick_{i}": [list(c) for c in s.colors]
                for i, s in enumerate(self._sticks)
            }

    # ── Référence persistée : Save / Restore ─────────────────

    def _current_snapshot(self) -> dict:
        """Instantané thread-safe de l'état courant (sans I/O)."""
        with self._lock:
            colors = {
                f"stick_{i}": [list(c) for c in s.colors]
                for i, s in enumerate(self._sticks)
            }
            order = [f"{s.bus_num}:{hex(s.address).lower()}" for s in self._sticks]
            brightness = None
            for s in self._sticks:
                try:
                    brightness = int(s.get_brightness())
                    break
                except Exception:
                    continue
        if brightness is None:
            brightness = self._last_brightness
        return {"colors": colors, "stick_order": order, "brightness": brightness}

    def apply_client_order(self, order: List[str]) -> None:
        """Applique l'ordre des sticks venu du front (drag & drop).

        Les objets sticks sont réordonnés — les couleurs suivent donc leur
        barrette physique ; les ids ``stick_i`` sont réindexés. Aucune
        écriture disque (la persistance reste POST /api/save).
        """
        if not order:
            return
        with self._lock:
            by_key = {
                f"{s.bus_num}:{hex(s.address).lower()}": s
                for s in self._sticks
            }
            ordered = []
            for key in order:
                stick = by_key.pop(str(key).lower(), None)
                if stick is not None and stick not in ordered:
                    ordered.append(stick)
            for stick in self._sticks:
                if stick not in ordered:
                    ordered.append(stick)
            self._sticks = ordered
            self._bus_map = {f"stick_{i}": s for i, s in enumerate(self._sticks)}

    def save_current(self, stick_order: Optional[List[str]] = None) -> dict:
        """Fige l'état courant comme nouvelle RÉFÉRENCE (POST /api/save).

        Écrit couleurs / luminosité / ordre / animation / kraken dans
        config.json, purge les sauvegardes de fichiers (l'état est acté),
        et retourne la référence enregistrée.

        Args:
            stick_order: ordre courant côté front (drag & drop), appliqué
                au modèle avant le snapshot s'il est fourni.
        """
        from .config import load as load_config, save as save_config

        if stick_order:
            self.apply_client_order(stick_order)

        snapshot = self._current_snapshot()
        config = load_config()
        config["colors"] = snapshot["colors"]
        config["stick_order"] = snapshot["stick_order"]
        config["brightness"] = snapshot["brightness"]

        # Couche lighting : le mode ET l'état marche/arrêt font partie de la
        # référence (option A). Les couleurs sauvées ci-dessus sont les
        # base_colors : plus jamais une frame d'animation (le moteur
        # n'écrit jamais dans stick.colors).
        lighting = self.lighting_settings()
        config["lighting"] = lighting
        # Miroir legacy conservé pour les outils plus anciens (schéma ≤ 3).
        config["animation"] = {
            "type": lighting["mode"],
            "enabled": lighting["running"],
            "speed": lighting["speed"],
            "framerate": lighting["framerate"],
            "refresh": lighting["refresh"],
        }

        settings = kraken_get_settings()
        config["kraken"] = {"lcd": settings["lcd"], "display": settings["display"]}

        save_config(config)
        kraken_commit_file_changes()
        return _reference_payload(config)

    def restore_reference(self) -> dict:
        """Ré-applique la RÉFÉRENCE persistée (POST /api/restore).

        Option A : l'état marche/arrêt fait partie de la référence. Restore
        ARRÊTE donc réellement l'animation si ``running=false`` et la
        RELANCE (mode + vitesse de la référence) si ``running=true``.
        Matériel : couleurs de base, luminosité, ordre, réglages LCD,
        relance éventuelle du mode d'affichage. Mémoire : état Kraken aligné
        sur la référence. Fichiers : sauvegardes/corbeille restaurées.
        """
        from .config import load as load_config

        config = load_config()
        ref = _reference_payload(config)

        # 1. Ordre des sticks (les couleurs restent indexées stick_i)
        order = ref.get("stick_order") or []
        with self._lock:
            if order:
                by_key = {
                    f"{s.bus_num}:{hex(s.address).lower()}": s
                    for s in self._sticks
                }
                ordered = []
                for key in order:
                    stick = by_key.pop(str(key).lower(), None)
                    if stick is not None and stick not in ordered:
                        ordered.append(stick)
                for stick in self._sticks:
                    if stick not in ordered:
                        ordered.append(stick)
                self._sticks = ordered
                self._bus_map = {f"stick_{i}": s for i, s in enumerate(self._sticks)}
            pairs = list(enumerate(self._sticks))

        # 2. Lumière : réglages mémoire + arrêt du moteur AVANT d'appliquer
        #    les couleurs de base (aucune frame ne doit gagner la bataille).
        lighting = self.set_lighting_state(ref.get("lighting") or {})
        self.stop_animation()

        # 3. Couleurs de base + luminosité (appliquées immédiatement au matériel)
        colors = ref.get("colors") or {}
        for idx, stick in pairs:
            stick_id = f"stick_{idx}"
            leds = colors.get(stick_id)
            if leds:
                self.apply_colors(stick_id, leds)
            else:
                with self._lock:
                    try:
                        stick.send_direct_colors()
                    except Exception:
                        pass
        self.set_all_brightness(ref.get("brightness", 255))

        # 4. Relance réelle si la référence dit running=true.
        if lighting["running"]:
            self.start_animation(
                lighting["mode"], lighting["params"],
                speed=lighting["speed"],
                framerate=lighting["framerate"],
                refresh=lighting["refresh"],
            )

        # 5. Kraken : matériel (best-effort) puis état mémoire
        kraken_settings = ref.get("kraken") or {}
        lcd = dict(kraken_settings.get("lcd") or {})
        display = dict(kraken_settings.get("display") or {})
        _restore_kraken_hardware(lcd, display)
        kraken_update_settings(lcd=lcd, display=display)

        # 6. Fichiers : les sauvegardes/corbeille reviennent en place
        kraken_restore_file_changes()

        return ref

    # ── Animation engine (mode matrice) ─────────────────────

    def _matrix_base_colors(self) -> List[List[int]]:
        """Couche ``base_colors`` aplatie en matrice de 32 LEDs (4 × 8).

        Lit ``stick.colors`` — que le moteur ne modifie JAMAIS — et complète
        en noir s'il y a moins de 4 barrettes. Relu à chaque frame par le
        runner : une édition de couleur à chaud est donc prise en compte.
        """
        with self._lock:
            colors: List[List[int]] = []
            for stick in self._sticks[:4]:
                for color in stick.colors[:8]:
                    colors.append([int(color[0]), int(color[1]), int(color[2])])
        while len(colors) < 32:
            colors.append([0, 0, 0])
        return colors

    def _write_animation_frame(self, colors_flat: List[List[int]]) -> None:
        """Transport matériel d'une frame : n'écrase JAMAIS les base_colors.

        Appelé par le runner au plus ``refresh`` fois par seconde. Diffuse
        aussi la frame aux clients WebSocket (le front anime le canvas) ; la
        couche persistable (``stick.colors``) reste intacte.
        """
        with self._lock:
            for stick_idx, stick in enumerate(self._sticks):
                start = stick_idx * 8
                frame = [list(c) for c in colors_flat[start:start + 8]]
                if frame:
                    stick.write_colors(frame)

        if loop is None:
            return
        colors_dict = {
            f"stick_{stick_idx}": [
                [min(255, max(0, int(c))) for c in px]
                for px in colors_flat[stick_idx * 8:stick_idx * 8 + 8]
            ]
            for stick_idx in range(len(self._sticks))
        }
        try:
            asyncio.run_coroutine_threadsafe(
                ws_manager.broadcast({
                    "type": "animation_frame",
                    "colors": colors_dict,
                    "matrix": [
                        [min(255, max(0, int(c))) for c in px]
                        for px in colors_flat[:32]
                    ],
                }),
                loop,
            )
        except Exception as e:
            print(f"⚠ WS broadcast: {e}")

    def start_animation(self, effect: str = "incandescence", params: dict = None,
                        speed: Optional[float] = None,
                        framerate: Optional[int] = None,
                        refresh: Optional[float] = None):
        """Démarre (ou hot-swap) un effet matriciel sur les 32 LEDs.

        Toutes les LEDs des 4 barrettes sont traitées comme une seule
        matrice. L'ancien moteur est arrêté AVANT l'installation du nouveau :
        un changement de mode est donc toujours un hot-swap réussi.

        Args:
            effect: id d'effet (``EFFECTS``) ou ``"static"`` (mode repos).
            params: paramètres spécifiques à l'effet (normalisés/bornés).
            speed: multiplicateur de vitesse (0.1–10.0) ; None = inchangé.
            framerate: cadence de rendu (1–30 fps) ; None = inchangé.
            refresh: écritures SMBus/sec (1–30) ; None = inchangé.

        Raises:
            UnknownEffectError: mode inconnu — dans ce cas l'ANCIEN moteur
                reste actif (aucun état modifié).
        """
        mode = effect if effect is not None else "incandescence"
        if not isinstance(mode, str) or mode not in MODE_IDS:
            raise UnknownEffectError(mode)

        if speed is not None:
            self._anim_speed = clamp_speed(speed)
        if framerate is not None:
            self._anim_framerate = clamp_framerate(framerate)
        if refresh is not None:
            self._smbus_refresh_rate = clamp_refresh(refresh)

        if mode == "static":
            # Mode repos : aucun moteur, retour aux couleurs de base.
            self.stop_animation()
            self._lighting_mode = "static"
            self._lighting_params = {}
            return

        normalized_params = normalize_params(mode, params)

        # Hot-swap : arrêter l'ancien moteur avant d'installer le nouveau.
        self.stop_animation()

        self._lighting_mode = mode
        self._lighting_params = normalized_params
        runner = EffectRunner(
            mode,
            normalized_params,
            self._matrix_base_colors,    # relu à chaque frame (édition à chaud)
            self._write_animation_frame,  # transport matériel + WS
            speed=self._anim_speed,
            framerate=self._anim_framerate,
            refresh=self._smbus_refresh_rate,
            clock=self._clock,
            sleep=self._sleep,
        )
        self._runner = runner
        runner.start()

    def stop_animation(self):
        """Arrête le moteur et ré-affiche les couleurs de base sur le matériel.

        Une frame d'animation est transitoire : à l'arrêt, les LEDs
        reprennent la couche ``base_colors`` (seule persistable).
        """
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.stop()
        if self._sticks:
            self.apply_all()

    @property
    def animation_running(self) -> bool:
        """True si un effet tourne réellement (thread vivant)."""
        return self._runner is not None and self._runner.is_running

    @property
    def animation_effect(self) -> Optional[str]:
        """Nom de l'effet en cours (legacy), ou None si arrêté."""
        runner = self._runner
        if runner is not None and runner.is_running:
            return runner.effect
        return None

    @property
    def animation_runner(self) -> Optional[EffectRunner]:
        """Runner actif (stats/diagnostic), ou None si arrêté."""
        return self._runner

    def animation_status(self) -> dict:
        """État complet de la lumière (contrat GET /api/animation/status).

        ``cycle_seconds`` est la durée RÉELLE d'un cycle à la vitesse
        courante (paramètre de cycle ÷ vitesse) : elle change donc dès que
        la vitesse change, ce qui prouve l'effet à chaud. ``effect`` est le
        miroir legacy (None à l'arrêt) du champ ``mode``.
        """
        cycle: Optional[float] = None
        if self._lighting_mode != "static":
            base_cycle = effect_cycle_seconds(self._lighting_mode,
                                              self._lighting_params)
            if base_cycle is not None:
                cycle = base_cycle / self._anim_speed
        stats = self._runner.stats() if self._runner is not None else {}
        return {
            "running": self.animation_running,
            "mode": self._lighting_mode,
            "effect": self.animation_effect,
            "speed": self._anim_speed,
            "framerate": self._anim_framerate,
            "refresh": self._smbus_refresh_rate,
            "params": dict(self._lighting_params),
            "cycle_seconds": round(cycle, 4) if cycle is not None else None,
            "fps": stats.get("fps"),
        }

    def set_animation_speed(self, speed: float) -> float:
        """Modifie la vitesse à chaud (relue à chaque frame par le runner)."""
        self._anim_speed = clamp_speed(speed)
        if self._runner is not None:
            self._runner.set_speed(self._anim_speed)
        return self._anim_speed

    def set_animation_framerate(self, fps: int) -> int:
        """Modifie le framerate à chaud."""
        self._anim_framerate = clamp_framerate(fps)
        if self._runner is not None:
            self._runner.set_framerate(self._anim_framerate)
        return self._anim_framerate

    # ── Cleanup ───────────────────────────────────────────────

    def cleanup(self):
        """Ferme tous les bus SMBus et arrête les animations."""
        self.stop_animation()
        for stick in self._sticks:
            try:
                stick.bus.close()
            except Exception:
                pass
        self._sticks = []
        self._bus_map = {}


# ── Vérification de la disponibilité WebSocket ─────────────────

try:
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    print("⚠ WebSocket non disponible — le fallback REST sera utilisé par le frontend")
    print("  Pour activer le WebSocket : pip install 'uvicorn[standard]'")

# ── Instances globales ────────────────────────────────────────

smbus = SMBusManager()
ws_manager = WSManager()

# ── Boucle asyncio globale (pour le broadcast WS depuis les threads) ──

loop: Optional[asyncio.AbstractEventLoop] = None


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gère le cycle de vie du serveur.

    Au démarrage : scanne les bus SMBus et détecte les sticks.
    À l'arrêt : ferme proprement les bus SMBus.
    """
    global loop
    loop = asyncio.get_event_loop()

    print(f"🔍 Scan des barrettes Ballistix...")
    sticks_info = smbus.scan()
    print(f"  → {len(smbus._sticks)} barrette(s) détectée(s)")
    for s in sticks_info:
        print(f"    • {s['label']}")

    # Reprendre l'état lumière persisté : les réglages exposés par
    # GET /api/animation/status sont justes dès l'ouverture (plus de faux
    # « dirty » au démarrage) et une animation `running=true` est réellement
    # relancée (décision utilisateur).
    try:
        from .config import load as load_config
        lighting = smbus.set_lighting_state(
            (load_config().get("lighting") or {}))
        if lighting["running"]:
            smbus.start_animation(
                lighting["mode"], lighting["params"],
                speed=lighting["speed"],
                framerate=lighting["framerate"],
                refresh=lighting["refresh"],
            )
            print(f"▶ Animation reprise au démarrage : {lighting['mode']} "
                  f"({lighting['speed']:.2f}×)")
    except Exception as e:
        print(f"⚠ Animation non reprise au démarrage : {e}")

    # Aligner l'état mémoire Kraken sur la référence persistée : liquidctl
    # ne remonte aucun réglage, la config est notre seule connaissance de
    # l'état au démarrage.
    try:
        from .config import load as load_config
        kraken_cfg = load_config().get("kraken") or {}
        kraken_update_settings(
            lcd=kraken_cfg.get("lcd"),
            display=kraken_cfg.get("display"),
        )
    except Exception as e:
        print(f"⚠ État Kraken non restauré en mémoire : {e}")

    # Ménage du cache de vignettes : les images d'une ancienne empreinte
    # du moteur de rendu ne seront plus jamais servies.
    try:
        purge = kraken_purge_thumbs()
        if purge.get("removed"):
            print(f"🧹 Vignettes obsolètes purgées : {purge['removed']}")
        elif not purge.get("ok"):
            print(f"⚠ Purge des vignettes ignorée : {purge.get('error')}")
    except Exception as e:
        print(f"⚠ Purge des vignettes impossible : {e}")

    yield
    # Cleanup à l'arrêt
    print("🧹 Fermeture des bus SMBus...")
    smbus.cleanup()


# ── Application FastAPI ───────────────────────────────────────

app = FastAPI(
    title="Ballistix RGB Controller",
    version="2.0.0",
    description=(
        "API REST + WebSocket pour le contrôle des LEDs de RAM "
        "Crucial Ballistix via SMBus."
    ),
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════
# Routes REST
# ═══════════════════════════════════════════════════════════════

@app.get("/api/ws-status")
async def ws_status():
    """Retourne si le WebSocket est disponible."""
    if WS_AVAILABLE:
        return {"websocket": True, "message": "WebSocket disponible"}
    else:
        return {"websocket": False, "message": "Installez: pip install 'uvicorn[standard]'"}


@app.get("/api/status")
async def get_status():
    """État du serveur et liste des sticks détectés."""
    sticks = smbus._get_status()
    return {
        "status": "running",
        "uptime": time.time(),
        "version": DAEMON_VERSION,
        "sticks_count": len(sticks),
        "sticks": sticks,
    }


@app.get("/api/sticks")
async def list_sticks():
    """Liste tous les sticks détectés avec leurs informations."""
    return smbus._get_status()


@app.get("/api/sticks/{stick_id}")
async def get_stick(stick_id: str):
    """Détail complet d'un stick : infos + couleurs actuelles."""
    stick = smbus.get_stick(stick_id)
    if not stick:
        raise HTTPException(404, f"Stick {stick_id} not found")
    return {
        "id": stick_id,
        "label": stick.label,
        "bus_num": stick.bus_num,
        "address": hex(stick.address),
        "version": stick.get_version(),
        "num_leds": stick.num_leds,
        "brightness": stick.get_brightness(),
        "colors": [list(c) for c in stick.colors],
    }


@app.put("/api/sticks/{stick_id}/colors")
async def set_colors(stick_id: str, body: ColorsBody):
    """Applique des couleurs à un stick.

    Body: {"leds": [[R,G,B], [R,G,B], ...]}
    Le nombre de LEDs peut être inférieur à num_leds.
    """
    success = smbus.apply_colors(stick_id, body.leds)
    if not success:
        raise HTTPException(404, f"Stick {stick_id} not found")
    await ws_manager.broadcast({
        "type": "color_applied",
        "stick_id": stick_id,
        "leds": body.leds,
    })
    return {"status": "ok", "stick_id": stick_id}


@app.put("/api/sticks/{stick_id}/brightness")
async def set_brightness(stick_id: str, body: BrightnessBody):
    """Change la luminosité d'un stick (0-255)."""
    success = smbus.set_brightness(stick_id, body.level)
    if not success:
        raise HTTPException(404, f"Stick {stick_id} not found")
    return {"status": "ok", "stick_id": stick_id, "level": body.level}


@app.get("/api/config")
async def get_config():
    """Lit la configuration actuelle (sticks + couleurs)."""
    return {
        "sticks": smbus._get_status(),
        "colors": smbus.get_colors(),
    }


@app.put("/api/config")
async def save_config(body: dict):
    """Applique une configuration de couleurs (application immédiate).

    Body: {"colors": {"stick_0": [[R,G,B], ...], ...}}
    N'écrit RIEN sur disque : la persistance passe par POST /api/save.
    """
    colors = body.get("colors", {})
    for stick_id, leds in colors.items():
        smbus.apply_colors(stick_id, leds)
    return {"status": "ok", "applied": list(colors.keys())}


@app.post("/api/colors/save")
async def save_colors_endpoint():
    """[Legacy] Fige l'état courant comme référence — alias de POST /api/save.

    Conservé pour compatibilité (l'ancien front l'appelait après un
    réordonnancement). Le flux actuel passe uniquement par /api/save.
    """
    reference = smbus.save_current()
    await ws_manager.broadcast({"type": "settings_saved", "reference": reference})
    return {"status": "ok", "saved_colors": len(reference.get("colors", {})),
            "brightness": reference.get("brightness"), "reference": reference}


# ── Référence persistée : Save / Restore ──────────────────────

@app.get("/api/saved")
async def get_saved_reference():
    """Retourne la RÉFÉRENCE persistée (dernier POST /api/save).

    C'est l'état auquel le front compare l'état courant pour calculer
    les modifications non enregistrées, et ce que Cancel ré-applique.
    """
    from .config import load as load_config
    return _reference_payload(load_config())


@app.post("/api/save")
async def save_settings(body: Optional[SaveBody] = None):
    """Fige l'état COURANT comme nouvelle référence (couleurs, luminosité,
    ordre, animation, Kraken) et purge les sauvegardes temporaires.

    Le matériel a déjà été mis à jour en temps réel ; ce seul endpoint
    écrit config.json. Un body optionnel ``{"stick_order": [...]}``
    applique l'ordre drag & drop du front avant de figer la référence.
    """
    order = body.stick_order if body else None
    reference = smbus.save_current(order)
    await ws_manager.broadcast({"type": "settings_saved", "reference": reference})
    return {"status": "ok", "reference": reference}


@app.post("/api/restore")
async def restore_settings():
    """Ré-applique la RÉFÉRENCE au matériel, à l'état mémoire et aux
    fichiers (images d'écran sauvegardées, corbeille gallery).

    C'est l'action « Annuler » du front : rien n'est écrit dans config.json.
    """
    reference = smbus.restore_reference()
    await ws_manager.broadcast({"type": "settings_restored", "reference": reference})
    return {"status": "ok", "reference": reference}


@app.post("/api/apply")
async def apply_colors():
    """Applique les couleurs actuelles de tous les sticks."""
    smbus.apply_all()
    await ws_manager.broadcast({"type": "applied"})
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════
# Routes Animation
# ═══════════════════════════════════════════════════════════════

@app.get("/api/animation/effects")
async def animation_effects():
    """Catalogue des effets disponibles (source unique : ballistix/effects).

    Retourne ``[{id, label, params:[{id,label,type,default,min,max}]}]``.
    """
    return describe_effects()


@app.post("/api/animation/start")
async def start_animation(body: dict = {}):
    """Démarre une animation matricielle (hot-swap garanti).

    Body :
        {
            "mode": "incandescence",  # nouveau nom ("effect" accepté legacy)
            "params": {},
            "speed": 1.0,             # optionnel (0.1–10.0)
            "framerate": 30,          # optionnel (1–30)
            "refresh": 20             # optionnel (1–30)
        }

    Réponse 400 si le mode est inconnu (l'animation en cours continue).
    ``mode="static"`` arrête l'animation et revient aux couleurs de base.
    """
    mode = body.get("mode", body.get("effect", "incandescence"))
    params = body.get("params")
    speed = body.get("speed")
    framerate = body.get("framerate")
    refresh = body.get("refresh")

    try:
        smbus.start_animation(mode, params, speed=speed,
                              framerate=framerate, refresh=refresh)
    except UnknownEffectError as e:
        raise HTTPException(400, str(e))

    status = smbus.animation_status()
    await ws_manager.broadcast({
        "type": "animation_started",
        "effect": status["mode"],  # legacy pour le front pas encore migré
        "mode": status["mode"],
        "speed": status["speed"],
        "framerate": status["framerate"],
    })
    return {"status": "ok", **status}


@app.post("/api/animation/stop")
async def stop_animation(body: dict = {}):
    """Arrête l'animation (retour aux couleurs de base) et retourne l'état."""
    smbus.stop_animation()
    status = smbus.animation_status()
    await ws_manager.broadcast({"type": "animation_stopped"})
    return {"status": "ok", **status}


@app.get("/api/animation/status")
async def animation_status():
    """État de la lumière : mode, marche/arrêt, réglages, cycle réel, fps.

    Contrat : ``{running, mode, speed, framerate, refresh, cycle_seconds}``
    (+ ``effect`` legacy, ``params`` et ``fps``).
    """
    return smbus.animation_status()


@app.post("/api/animation/update")
async def update_animation(body: dict = {}):
    """Applique des réglages À CHAUD, sans redémarrer l'effet.

    Body : ``{"speed"?: float, "framerate"?: int, "refresh"?: float}``.
    """
    if body.get("speed") is not None:
        smbus.set_animation_speed(body["speed"])
    if body.get("framerate") is not None:
        smbus.set_animation_framerate(body["framerate"])
    if body.get("refresh") is not None:
        smbus.set_smbus_refresh_rate(body["refresh"])
    status = smbus.animation_status()
    await ws_manager.broadcast({
        "type": "animation_update",
        "speed": status["speed"],
        "framerate": status["framerate"],
        "refresh": status["refresh"],
    })
    return {"status": "ok", **status}


# ── Routes de compatibilité (front pas encore migré) ─────────

@app.post("/api/animation/speed")
async def set_animation_speed(body: dict = {}):
    """[Compat] Body : {"speed": 1.0} (0.1 = lent, 10.0 = rapide) — borné."""
    speed = smbus.set_animation_speed(body.get("speed", 1.0))
    await ws_manager.broadcast({
        "type": "animation_speed",
        "speed": speed,
    })
    return {"status": "ok", "speed": speed}


@app.post("/api/animation/refresh")
async def set_animation_refresh(body: dict = {}):
    """[Compat] Body : {"rate": 20} — écritures SMBus par seconde (1-30)."""
    rate = smbus.set_smbus_refresh_rate(body.get("rate", 20))
    return {"status": "ok", "rate": rate}


# ═══════════════════════════════════════════════════════════════
# Routes Rescan
# ═══════════════════════════════════════════════════════════════

@app.post("/api/rescan")
async def rescan_buses():
    """Rescanne les bus SMBus pour détecter les sticks."""
    sticks = smbus.scan()
    await ws_manager.broadcast({"type": "rescan", "sticks": sticks})
    return {"status": "ok", "sticks": sticks}


# ═══════════════════════════════════════════════════════════════
# Kraken NZXT (via liquidctl)
# ═══════════════════════════════════════════════════════════════

class KrakenModeBody(BaseModel):
    """Mode de l'écran LCD du Kraken."""
    mode: str

class KrakenImageBody(BaseModel):
    """Image à afficher sur l'écran LCD (base64)."""
    data: str
    filename: str = "screen.png"
    animated: bool = False

class KrakenValueBody(BaseModel):
    """Valeur numérique (luminosité / orientation)."""
    value: int


@app.get("/api/kraken/status")
async def kraken_status_endpoint():
    """Statut du Kraken : liquidctl disponible, détection, capteurs.

    ``status_missing`` liste les capteurs non extraits de la sortie
    liquidctl (indisponibles ≠ zéro) et ``status_ok`` dit si la commande
    a réussi — le front s'en sert pour afficher un diagnostic au lieu de
    tirets muets.
    """
    available = kraken_available()
    detect = kraken_detect() if available else {"devices": [], "error": None}
    status = kraken_status() if available else {"ok": False, "data": {}, "raw": "", "error": None}
    return {
        "available": available,
        "detected": bool(detect.get("devices")),
        "devices": detect.get("devices", []),
        "status": status.get("data", {}) if status.get("ok") else {},
        "status_ok": bool(status.get("ok")),
        "status_missing": list(status.get("missing") or []),
        "status_source": status.get("source"),
        "status_raw": status.get("raw", ""),
        "error": detect.get("error") or status.get("error"),
    }


@app.post("/api/kraken/initialize")
async def kraken_initialize_endpoint():
    """Initialise le Kraken (requis après chaque boot à froid)."""
    return kraken_initialize()


@app.post("/api/kraken/lcd/mode")
async def kraken_lcd_mode(body: KrakenModeBody):
    """Règle le mode de l'écran LCD (liquid)."""
    return kraken_set_lcd_mode(body.mode)


@app.post("/api/kraken/lcd/image")
async def kraken_lcd_image(body: KrakenImageBody):
    """Affiche une image (PNG/JPEG) ou un GIF sur l'écran LCD.

    Body: {"data": "<base64>", "filename": "x.png", "animated": false}
    """
    try:
        img_bytes = kraken_decode_b64(body.data)
    except Exception as e:
        raise HTTPException(400, f"Données base64 invalides : {e}")
    if not img_bytes:
        raise HTTPException(400, "Image vide")

    try:
        path = kraken_save_image(img_bytes, body.animated)
    except Exception as e:
        raise HTTPException(500, f"Sauvegarde de l'image impossible : {e}")

    result = kraken_set_lcd_image(path, body.animated)
    result["path"] = path
    return result


@app.get("/api/kraken/lcd/settings")
async def kraken_lcd_settings_endpoint():
    """Réglages LCD courants (état mémoire — liquidctl ne fait aucun readback)."""
    return kraken_get_settings()["lcd"]


@app.post("/api/kraken/lcd/brightness")
async def kraken_lcd_brightness(body: KrakenValueBody):
    """Règle la luminosité de l'écran LCD (0-100)."""
    return kraken_set_lcd_brightness(body.value)


@app.post("/api/kraken/lcd/orientation")
async def kraken_lcd_orientation(body: KrakenValueBody):
    """Règle l'orientation de l'écran LCD (0/90/180/270)."""
    return kraken_set_lcd_orientation(body.value)


class KrakenGalleryAddBody(BaseModel):
    """Image à ajouter à la gallery (base64)."""
    data: str
    filename: str

class KrakenGalleryDeleteBody(BaseModel):
    """Nom du fichier à supprimer de la gallery."""
    name: str

class KrakenDisplayBody(BaseModel):
    """Paramètres du thread d'affichage.

    ``interval`` : nombre de secondes (2-60) ou la sentinelle ``"asap"``
    (« le plus souvent possible »). Le rendu combine ``palette`` ×
    ``layout`` ; ``theme`` reste accepté (alias historique de palette).
    """
    interval: Union[float, str] = 10.0
    theme: Optional[str] = None
    palette: Optional[str] = None
    layout: Optional[str] = None
    options: Optional[List[str]] = None


@app.get("/api/kraken/gallery")
async def kraken_gallery_endpoint():
    """Liste les images de la gallery."""
    return kraken_gallery_list()


@app.post("/api/kraken/gallery/add")
async def kraken_gallery_add_endpoint(body: KrakenGalleryAddBody):
    """Ajoute une image/GIF à la gallery."""
    try:
        img_bytes = kraken_decode_b64(body.data)
    except Exception as e:
        raise HTTPException(400, f"Données base64 invalides : {e}")
    if not img_bytes:
        raise HTTPException(400, "Image vide")
    return kraken_gallery_add(img_bytes, body.filename)


@app.post("/api/kraken/gallery/delete")
async def kraken_gallery_delete_endpoint(body: KrakenGalleryDeleteBody):
    """Supprime une image de la gallery."""
    return kraken_gallery_delete(body.name)


@app.post("/api/kraken/gallery/start")
async def kraken_gallery_start_endpoint(body: KrakenDisplayBody):
    """Démarre le diaporama gallery."""
    return kraken_gallery_start(body.interval)


@app.post("/api/kraken/monitor/start")
async def kraken_monitor_start_endpoint(body: KrakenDisplayBody):
    """Démarre le mode monitoring (stats système sur l'écran)."""
    return kraken_monitor_start(
        body.interval,
        theme=body.theme,
        palette=body.palette,
        layout=body.layout,
        options=body.options,
    )


@app.post("/api/kraken/display/update")
async def kraken_display_update_endpoint(body: KrakenDisplayBody):
    """Applique en temps réel des réglages d'affichage (sans changer de mode).

    Met à jour palette/disposition/capteurs/intervalle et RELANCE le thread
    monitoring (ou gallery) s'il tourne déjà. Appelé par le front à chaque
    sélection de palette/disposition, coche de capteur ou changement
    d'intervalle (débouncé).
    """
    return kraken_display_reconfigure(
        interval=body.interval,
        theme=body.theme,
        palette=body.palette,
        layout=body.layout,
        options=body.options,
    )


@app.get("/api/kraken/pending")
async def kraken_pending_endpoint():
    """Fichiers de la session encore annulables (écran remplacé / gallery)."""
    return kraken_pending_changes()


@app.post("/api/kraken/display/stop")
async def kraken_display_stop_endpoint():
    """Arrête le thread d'affichage actif (monitoring ou gallery)."""
    return kraken_stop_display()


@app.get("/api/kraken/display/status")
async def kraken_display_status_endpoint():
    """État du thread d'affichage + réglages mémorisés (mode/theme/options/interval)."""
    return kraken_display_status()


@app.get("/api/kraken/themes")
async def kraken_themes_endpoint():
    """Catalogue de l'écran LCD : palettes + dispositions (source unique).

    Le front ne recopie plus ni les couleurs ni les dispositions : les
    catalogues viennent de ``ballistix/monitor.py``. ``themes`` reste un
    alias des palettes (rétrocompatibilité vague 3) ; ``palettes`` (couleurs
    incluses) et ``layouts`` alimentent les deux sélecteurs.
    """
    try:
        from .monitor import list_themes, list_palettes, list_layouts
    except ImportError:
        return {"ok": False, "themes": [], "palettes": [], "layouts": [],
                "count": 0, "error": "Module de monitoring indisponible"}
    themes = list_themes()
    palettes = list_palettes()
    layouts = list_layouts()
    return {
        "ok": True,
        "themes": themes,
        "palettes": palettes,
        "layouts": layouts,
        "count": len(themes),
        "palette_count": len(palettes),
        "layout_count": len(layouts),
        "preview_endpoint": "/api/kraken/monitor/preview",
        "preview_image": "/api/kraken/monitor/preview.png",
        "error": None,
    }


@app.get("/api/kraken/palettes")
async def kraken_palettes_endpoint():
    """Catalogue des palettes (5 entrées, couleurs incluses)."""
    try:
        from .monitor import list_palettes
    except ImportError:
        return {"ok": False, "palettes": [], "count": 0,
                "error": "Module de monitoring indisponible"}
    palettes = list_palettes()
    return {"ok": True, "palettes": palettes, "count": len(palettes),
            "error": None}


@app.get("/api/kraken/layouts")
async def kraken_layouts_endpoint():
    """Catalogue des dispositions (3 entrées)."""
    try:
        from .monitor import list_layouts
    except ImportError:
        return {"ok": False, "layouts": [], "count": 0,
                "error": "Module de monitoring indisponible"}
    layouts = list_layouts()
    return {"ok": True, "layouts": layouts, "count": len(layouts),
            "error": None}


def _serve_thumb(result: dict, key: str):
    """Réponse FileResponse commune aux endpoints de vignette."""
    from fastapi.responses import FileResponse
    if not result["ok"]:
        if result.get("code") == "unknown":
            raise HTTPException(404, result.get("error") or f"Inconnu : {key}")
        raise HTTPException(500, result.get("error") or "Vignette indisponible")
    return FileResponse(
        result["path"],
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/kraken/themes/{key}/thumb.png")
async def kraken_theme_thumb_endpoint(key: str):
    """Vignette PNG d'une palette ou d'une disposition (vrai moteur PIL).

    Cache disque sous ``~/.config/ballistix/kraken/thumbs/`` ; invalidé
    dès que le moteur (monitor.py) change. Clé inconnue → 404.
    """
    return _serve_thumb(kraken_theme_thumb(key), key)


@app.get("/api/kraken/palettes/{key}/thumb.png")
async def kraken_palette_thumb_endpoint(key: str):
    """Vignette PNG d'une palette (rendue en disposition classic)."""
    return _serve_thumb(kraken_palette_thumb(key), key)


@app.get("/api/kraken/layouts/{key}/thumb.png")
async def kraken_layout_thumb_endpoint(key: str):
    """Vignette PNG d'une disposition (rendue avec la palette par défaut)."""
    return _serve_thumb(kraken_layout_thumb(key), key)


@app.post("/api/kraken/monitor/preview")
async def kraken_monitor_preview_endpoint(body: KrakenDisplayBody = None):
    """Génère un aperçu du rendu monitoring (pour la page web)."""
    body = body or KrakenDisplayBody()
    return kraken_monitor_preview(
        theme=body.theme,
        palette=body.palette,
        layout=body.layout,
        options=body.options,
    )


@app.get("/api/kraken/monitor/preview.png")
async def kraken_monitor_preview_image():
    """Sert l'image d'aperçu monitoring générée."""
    from fastapi.responses import FileResponse
    from .kraken import _store_dir
    path = _store_dir() / "monitor_preview.png"
    if not path.is_file():
        raise HTTPException(404, "Aucun aperçu généré — cliquez sur 👁 d'abord")
    return FileResponse(str(path), media_type="image/png")


# ═══════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket pour le contrôle temps réel des LEDs.

    Voir la docstring du module pour le protocole détaillé.
    """
    await ws_manager.connect(websocket)
    try:
        # Envoyer l'état initial au nouveau client
        await websocket.send_json({
            "type": "status",
            "sticks": smbus._get_status(),
            "uptime": time.time(),
        })

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "set_led":
                stick_id = data.get("stick_id", "")
                led_idx = data.get("led_idx", 0)
                color = data.get("color", [0, 0, 0])
                r, g, b = color[0], color[1], color[2]
                success = smbus.set_led(stick_id, led_idx, r, g, b)
                if success:
                    stick = smbus.get_stick(stick_id)
                    colors = [list(c) for c in stick.colors] if stick else []
                    await ws_manager.broadcast({
                        "type": "color_applied",
                        "stick_id": stick_id,
                        "leds": colors,
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Stick {stick_id} not found",
                    })

            elif msg_type == "set_all_leds":
                stick_id = data.get("stick_id", "")
                color = data.get("color", [0, 0, 0])
                r, g, b = color[0], color[1], color[2]
                success = smbus.set_all_leds(stick_id, r, g, b)
                if success:
                    stick = smbus.get_stick(stick_id)
                    colors = [list(c) for c in stick.colors] if stick else []
                    await ws_manager.broadcast({
                        "type": "color_applied",
                        "stick_id": stick_id,
                        "leds": colors,
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Stick {stick_id} not found",
                    })

            elif msg_type == "set_brightness":
                stick_id = data.get("stick_id", "")
                level = data.get("level", 128)
                success = smbus.set_brightness(stick_id, level)
                if not success:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Stick {stick_id} not found",
                    })

            elif msg_type == "set_all_brightness":
                level = data.get("level", 128)
                smbus.set_all_brightness(level)

            elif msg_type == "apply_colors":
                colors = data.get("colors", {})
                for stick_id, leds in colors.items():
                    smbus.apply_colors(stick_id, leds)
                await ws_manager.broadcast({"type": "applied"})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        print(f"⚠ WebSocket error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Erreur WebSocket : {str(e)}",
            })
        except Exception:
            pass
        ws_manager.disconnect(websocket)


# ═══════════════════════════════════════════════════════════════
# Frontend statique (optionnel)
# ═══════════════════════════════════════════════════════════════

# Le dossier static/ doit être résolu correctement
# - En dev : à côté du package ballistix/
# - En PyInstaller : dans _MEIPASS (dossier temporaire extrait)
if getattr(sys, 'frozen', False):
    _base_dir = sys._MEIPASS
else:
    _base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

static_dir = os.path.join(_base_dir, "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    print(f"✅ Interface web servie depuis {static_dir}")
else:
    print(f"ℹ  Dossier static/ non trouvé à {static_dir}")
    print(f"   L'API est accessible, mais le frontend web n'est pas servi.")


# ═══════════════════════════════════════════════════════════════
# Point d'entrée CLI
# ═══════════════════════════════════════════════════════════════

def run_server(host: str = "0.0.0.0",
               port: int = 8080,
               open_browser: bool = False):
    """Lance le serveur web uvicorn.

    Appelable depuis cli.py (mode --web) ou directement :
        python -m ballistix.server

    Args:
        host: Adresse d'écoute (défaut: 0.0.0.0)
        port: Port d'écoute (défaut: 8080)
        open_browser: Ouvrir le navigateur au lancement
    """
    import uvicorn
    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}"
    print(f"🌐 Ballistix RGB Controller")
    print(f"   API REST     : {url}/api/status")
    print(f"   WebSocket    : ws://localhost:{port}/ws")
    print(f"   Interface    : {url} (si dossier static/ présent)")
    print()
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass  # Pas de display (environnement sans écran, SSH, etc.)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()

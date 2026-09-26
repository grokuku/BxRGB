#!/usr/bin/env python3
"""
ballistix/server.py — Serveur web FastAPI pour le contrôle des LEDs
Crucial Ballistix via API REST + WebSocket.

Usage :
    python -m ballistix.server
    # ou depuis cli.py avec --web
"""

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import CrucialStick, ok, fail, warn
from .detect import detect_sticks
from .kraken import (
    kraken_available, kraken_detect, kraken_status,
    kraken_initialize, kraken_set_lcd_mode, kraken_set_lcd_image,
    kraken_set_lcd_brightness, kraken_set_lcd_orientation,
    kraken_save_image, kraken_decode_b64,
    kraken_gallery_list, kraken_gallery_add, kraken_gallery_delete,
    kraken_gallery_start, kraken_monitor_start, kraken_stop_display,
    kraken_monitor_preview, _display_thread_status,
)

# ── Version du daemon (pour l'auto-update) ───────────────────
DAEMON_VERSION = "0.0.1"
GITHUB_REPO = "grokuku/BxRGB"
BINARY_PATH = "/usr/local/bin/ballistixd"
SERVICE_NAME = "ballistix-rgb"
GITHUB_TIMEOUT = 15  # secondes

# ── Modèles Pydantic ──────────────────────────────────────────

class ColorsBody(BaseModel):
    """Liste de couleurs [[R,G,B], ...] pour un stick."""
    leds: List[List[int]]

class BrightnessBody(BaseModel):
    """Niveau de luminosité 0-255."""
    level: int

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

    Supporte aussi le mode matrice avec AnimationEngine pour les
    effets animés sur l'ensemble des 32 LEDs (4 barrettes × 8 LEDs).
    """
    """Encapsule l'accès aux bus SMBus avec un lock thread.

    Toutes les opérations SMBus sont bloquantes ; le lock garantit
    qu'une seule opération à la fois traverse le bus, évitant les
    corruptions de registres lorsque plusieurs requêtes arrivent
    simultanément (REST + WebSocket).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sticks: List[CrucialStick] = []
        self._bus_map: Dict[str, CrucialStick] = {}
        self._manual_devices: List[dict] = []
        self._anim_engine: Optional['AnimationEngine'] = None
        self._save_timer: Optional[threading.Timer] = None
        self._smbus_refresh_rate: float = 20.0  # écritures SMBus/sec pour les animations

    def set_smbus_refresh_rate(self, rate: float):
        """Définit le taux max d'écriture SMBus en animations (1-30 Hz)."""
        self._smbus_refresh_rate = max(1.0, min(30.0, float(rate)))

    def _schedule_save(self):
        """Programme une sauvegarde différée (debounced 2s)."""
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(2.0, self._do_save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _do_save(self):
        """Sauvegarde les couleurs et l'ordre dans la config."""
        try:
            from .config import load as load_config, save as save_config
            config = load_config()
            config["colors"] = {
                f"stick_{i}": [list(c) for c in s.colors]
                for i, s in enumerate(self._sticks)
            }
            config["stick_order"] = [f"{s.bus_num}:{hex(s.address)}" for s in self._sticks]
            if self._sticks:
                config["brightness"] = self._sticks[0].get_brightness()
            save_config(config)
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
        (liste de strings ``"bus:0xaddr"``) puis sauvegardé après
        réordonnancement.
        """
        with self._lock:
            from .config import load as load_config, save as save_config
            from smbus2 import SMBus

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
            # Restaurer l'ordre depuis la config
            try:
                from .config import load as load_config
                config = load_config()
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
                        except:
                            pass
                    for s in sticks:
                        if s not in ordered:
                            ordered.append(s)
                    sticks = ordered
            except Exception as e:
                print(f"⚠ Erreur restauration ordre: {e}")

            self._sticks = sticks
            self._bus_map = {f"stick_{i}": s for i, s in enumerate(sticks)}

            # ── 6. Restaurer les couleurs depuis la config ──
            saved_colors = config.get("colors", {})
            for i, stick in enumerate(self._sticks):
                stick_id = f"stick_{i}"
                if stick_id in saved_colors:
                    for led_idx, color in enumerate(saved_colors[stick_id]):
                        if led_idx < stick.num_leds:
                            stick.colors[led_idx] = tuple(color)
                # Appliquer les couleurs aux LEDs physiques
                try:
                    stick.send_direct_colors()
                except Exception:
                    pass

            # ── 7. Restaurer la luminosité depuis la config ──
            try:
                brightness = config.get("brightness", 255)
                for stick in self._sticks:
                    stick.set_brightness(brightness)
            except Exception:
                pass

            # ── 8. Sauvegarder l'ordre actuel (après réordonnancement) ──
            # Sauvegarder l'ordre
            try:
                from .config import load as load_config, save as save_config
                config = load_config()
                config["stick_order"] = [f"{s.bus_num}:{hex(s.address)}" for s in sticks]
                save_config(config)
            except Exception as e:
                print(f"⚠ Erreur sauvegarde ordre: {e}")

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
        self._schedule_save()
        return True

    def set_led(self, stick_id: str, led_idx: int,
                r: int, g: int, b: int) -> bool:
        """Change la couleur d'une LED unique (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            stick.set_led(led_idx, r, g, b)
        self._schedule_save()
        return True

    def set_all_leds(self, stick_id: str, r: int, g: int, b: int) -> bool:
        """Met toutes les LEDs d'un stick à la même couleur (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            stick.set_all_leds(r, g, b)
        self._schedule_save()
        return True

    def set_brightness(self, stick_id: str, level: int) -> bool:
        """Change la luminosité d'un stick (thread-safe)."""
        stick = self.get_stick(stick_id)
        if not stick:
            return False
        with self._lock:
            stick.set_brightness(min(255, max(0, int(level))))
        self._schedule_save()
        return True

    def set_all_brightness(self, level: int):
        """Change la luminosité de tous les sticks (thread-safe)."""
        level = min(255, max(0, int(level)))
        with self._lock:
            for stick in self._sticks:
                stick.set_brightness(level)
        self._schedule_save()

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

    # ── Animation engine (mode matrice) ─────────────────────

    def start_animation(self, effect: str = "incandescence", params: dict = None,
                          speed: float = 1.0, framerate: int = 30):
        """Démarre une animation en mode matrice.

        Toutes les LEDs des 4 barrettes sont traitées comme une
        seule matrice de 32 LEDs. Le moteur d'animation tourne
        dans un thread séparé et appelle un callback à chaque frame.

        Args:
            effect: nom de l'effet (ex: "incandescence")
            params: dictionnaire de paramètres spécifiques à l'effet
            speed: multiplicateur de vitesse (0.05 = très lent, 10.0 = rapide)
            framerate: images par seconde (max 30 pour éviter de floder SMBus)
        """
        from .animations import AnimationEngine

        _last_smbus_write = 0.0

        def apply_matrix_colors(colors_flat):
            """Reçoit 32 couleurs et les distribue aux 4 sticks.

            Inclut un garde-fou temporel (8 écritures/sec max) pour
            réduire le clignotement causé par l'écriture séquentielle
            R, G, B sur le bus SMBus.

            Utilise send_direct_colors() (méthode confirmée fonctionnelle)
            au lieu de set_led_individual() qui est cassé.
            """
            nonlocal _last_smbus_write
            now = time.time()
            min_interval = 1.0 / self._smbus_refresh_rate  # Taux configurable
            if now - _last_smbus_write < min_interval:
                return  # Trop tôt, on skip cette frame
            _last_smbus_write = now

            with self._lock:
                for stick_idx, stick in enumerate(self._sticks):
                    for led_idx in range(8):
                        matrix_idx = stick_idx * 8 + led_idx
                        if matrix_idx < len(colors_flat):
                            r, g, b = colors_flat[matrix_idx]
                            stick.colors[led_idx] = (min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b)))
                    stick.send_direct_colors()

            # 3. Diffuser les couleurs via WebSocket pour l'interface web
            colors_dict = {}
            for stick_idx, stick in enumerate(self._sticks):
                colors_dict[f"stick_{stick_idx}"] = [list(c) for c in stick.colors]

            try:
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        ws_manager.broadcast({
                            "type": "animation_frame",
                            "colors": colors_dict,
                            "matrix": [[
                                min(255, max(0, int(c)))
                                for c in colors_flat[i]
                            ] for i in range(min(32, len(colors_flat)))],
                        }),
                        loop,
                    )
            except Exception as e:
                print(f"⚠ WS broadcast: {e}")

        # Arrêter l'ancienne animation si elle tourne
        self.stop_animation()

        self._anim_engine = AnimationEngine(apply_matrix_colors)
        # Passer speed et framerate AVANT de démarrer le thread
        self._anim_engine.start(effect, params, speed=speed, framerate=framerate)

    def stop_animation(self):
        """Arrête l'animation et revient en mode classique (LEDs éteintes)."""
        if self._anim_engine:
            self._anim_engine.stop()
            self._anim_engine = None
            self._schedule_save()

    @property
    def animation_running(self) -> bool:
        """Retourne True si une animation est en cours."""
        return self._anim_engine is not None and self._anim_engine.is_running

    @property
    def animation_effect(self) -> Optional[str]:
        """Retourne le nom de l'effet en cours, ou None."""
        if self._anim_engine and self._anim_engine.is_running:
            return self._anim_engine.effect
        return None

    def set_animation_speed(self, speed: float):
        """Modifie la vitesse de l'animation en cours."""
        if self._anim_engine:
            self._anim_engine.set_speed(speed)

    def set_animation_framerate(self, fps: int):
        """Modifie le framerate de l'animation en cours."""
        if self._anim_engine:
            self._anim_engine.set_framerate(fps)

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
    """Sauvegarde une configuration complète.

    Body: {"colors": {"stick_0": [[R,G,B], ...], ...}}
    Applique immédiatement les couleurs aux sticks.
    Note : la persistance disque sera implémentée dans une phase future.
    """
    colors = body.get("colors", {})
    for stick_id, leds in colors.items():
        smbus.apply_colors(stick_id, leds)
    return {"status": "ok", "applied": list(colors.keys())}


@app.post("/api/colors/save")
async def save_colors_endpoint():
    """Sauvegarde les couleurs actuelles dans la configuration persistée.

    Collecte les couleurs de tous les sticks et les écrit dans
    ~/.config/ballistix/config.json pour restauration future.
    """
    from .config import save_colors as persist_colors, save_brightness as persist_brightness

    colors = {}
    with smbus._lock:
        for stick_id, stick in smbus._bus_map.items():
            colors[stick_id] = [list(c) for c in stick.colors]

    persist_colors(colors)

    # Sauvegarder aussi la luminosité courante
    brightness = 255
    if smbus._sticks:
        try:
            brightness = smbus._sticks[0].get_brightness()
        except Exception:
            pass
    persist_brightness(brightness)

    return {"status": "ok", "saved_colors": len(colors), "brightness": brightness}


@app.post("/api/apply")
async def apply_colors():
    """Applique les couleurs actuelles de tous les sticks."""
    smbus.apply_all()
    await ws_manager.broadcast({"type": "applied"})
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════
# Routes Animation
# ═══════════════════════════════════════════════════════════════

@app.post("/api/animation/start")
async def start_animation(body: dict = {}):
    """Démarre une animation en mode matrice.

    Body (optionnel):
        {
            "effect": "incandescence",
            "params": {},
            "speed": 1.0,        # optionnel
            "framerate": 30       # optionnel
        }
    """
    effect = body.get("effect", "incandescence")
    params = body.get("params", {})
    speed = body.get("speed", 1.0)
    framerate = body.get("framerate", 30)

    smbus.start_animation(effect, params, speed=speed, framerate=framerate)

    await ws_manager.broadcast({
        "type": "animation_started",
        "effect": effect,
        "speed": speed,
        "framerate": framerate,
    })
    return {
        "status": "ok",
        "effect": effect,
        "speed": speed,
        "framerate": framerate,
    }


@app.post("/api/animation/stop")
async def stop_animation():
    """Arrête l'animation en cours. Les LEDs restent sur leur dernière couleur."""
    smbus.stop_animation()
    await ws_manager.broadcast({"type": "animation_stopped"})
    return {"status": "ok"}


@app.get("/api/animation/status")
async def animation_status():
    """Retourne l'état de l'animation en cours."""
    return {
        "running": smbus.animation_running,
        "effect": smbus.animation_effect,
    }


@app.post("/api/animation/speed")
async def set_animation_speed(body: dict = {}):
    """Modifie la vitesse de l'animation en cours.

    Body: {"speed": 1.0}  (0.1 = lent, 10.0 = rapide)
    """
    speed = body.get("speed", 1.0)
    smbus.set_animation_speed(speed)
    await ws_manager.broadcast({
        "type": "animation_speed",
        "speed": speed,
    })
    return {"status": "ok", "speed": speed}


@app.post("/api/animation/refresh")
async def set_animation_refresh(body: dict = {}):
    """Modifie le taux de rafraîchissement SMBus (1-30 Hz)."""
    rate = body.get("rate", 20)
    smbus.set_smbus_refresh_rate(rate)
    return {"status": "ok", "rate": rate}


# ═══════════════════════════════════════════════════════════════
# Routes Auto-Update
# ═══════════════════════════════════════════════════════════════

def _fetch_github_release() -> dict:
    """Récupère la dernière release via l'API GitHub.

    Returns:
        dict: réponse JSON de l'API GitHub (tag_name, assets, ...)

    Raises:
        HTTPException: si la requête échoue.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "BxRGB-Updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(404, "Aucune release GitHub trouvée")
        raise HTTPException(502, f"Erreur API GitHub: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Impossible de joindre GitHub: {e.reason}")
    except Exception as e:
        raise HTTPException(500, f"Erreur lors de la récupération de la release: {e}")


def _parse_version(tag: str) -> str:
    """Extrait le numéro de version depuis un tag (ex: 'v0.0.2' → '0.0.2')."""
    return tag.lstrip("vV").strip()


def _version_tuple(v: str) -> tuple:
    """Convertit une version string en tuple comparable (ex: '0.0.2' → (0, 0, 2))."""
    parts = re.split(r'[.\-]', v)
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(0)
    return tuple(result)


@app.get("/api/update/check")
async def update_check():
    """Vérifie si une mise à jour est disponible sur GitHub.

    Ne télécharge rien — compare seulement la version actuelle
    avec la dernière release publiée.
    """
    release = _fetch_github_release()
    latest_tag = release.get("tag_name", "")
    latest_version = _parse_version(latest_tag)
    current_version = DAEMON_VERSION

    update_available = _version_tuple(latest_version) > _version_tuple(current_version)

    return {
        "current": current_version,
        "latest": latest_version,
        "update_available": update_available,
    }


@app.post("/api/update")
async def perform_update():
    """Télécharge et installe la dernière release GitHub.

    Étapes:
    1. Récupère la dernière release GitHub
    2. Vérifie si une mise à jour est nécessaire
    3. Télécharge l'asset tar.gz contenant le binaire ballistixd
    4. Extrait le binaire vers un fichier temporaire
    5. Remplace atomiquement /usr/local/bin/ballistixd
    6. Redémarre le service systemd ballistix-rgb

    En cas d'échec à any étape, le binaire existant n'est pas touché.
    """
    # ── 1. Récupérer la dernière release ──
    release = _fetch_github_release()
    latest_tag = release.get("tag_name", "")
    latest_version = _parse_version(latest_tag)
    current_version = DAEMON_VERSION

    # ── 2. Vérifier si une update est nécessaire ──
    if _version_tuple(latest_version) <= _version_tuple(current_version):
        return {
            "status": "up-to-date",
            "current": current_version,
            "latest": latest_version,
        }

    # ── 3. Trouver l'asset tar.gz ──
    assets = release.get("assets", [])
    download_url = None
    for asset in assets:
        name = asset.get("name", "")
        if name.startswith("bxrgb-") and name.endswith(".tar.gz"):
            download_url = asset.get("browser_download_url")
            break

    if not download_url:
        raise HTTPException(
            404,
            "Aucun asset 'bxrgb-*.tar.gz' trouvé dans la dernière release",
        )

    # ── 4. Télécharger le tar.gz ──
    try:
        dl_req = urllib.request.Request(download_url, headers={
            "User-Agent": "BxRGB-Updater",
        })
        with urllib.request.urlopen(dl_req, timeout=GITHUB_TIMEOUT) as resp:
            tar_data = resp.read()
    except Exception as e:
        raise HTTPException(502, f"Échec du téléchargement: {e}")

    # ── 5. Extraire le binaire ballistixd du tar.gz ──
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="bxrgb_update_")
        tar_path = os.path.join(tmp_dir, "release.tar.gz")
        with open(tar_path, "wb") as f:
            f.write(tar_data)

        # Extraire et chercher le binaire ballistixd
        extracted_binary = None
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                # Chercher un fichier nommé ballistixd (à la racine ou dans un sous-dossier)
                member_name = os.path.basename(member.name)
                if member_name == "ballistixd" and member.isfile():
                    tar.extract(member, path=tmp_dir)
                    extracted_binary = os.path.join(tmp_dir, member.name)
                    break

        if not extracted_binary or not os.path.isfile(extracted_binary):
            raise HTTPException(
                500,
                "Binaire 'ballistixd' introuvable dans l'archive tar.gz",
            )

        # ── 6. Remplacer le binaire atomiquement ──
        if not os.path.isdir(os.path.dirname(BINARY_PATH)):
            raise HTTPException(500, f"Dossier {os.path.dirname(BINARY_PATH)} introuvable")

        new_path = BINARY_PATH + ".new"

        # Copier le binaire extrait vers new_path
        import shutil
        shutil.copy2(extracted_binary, new_path)

        # chmod +x
        os.chmod(new_path, os.stat(new_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        # Remplacement atomique
        os.replace(new_path, BINARY_PATH)

    except HTTPException:
        raise
    except Exception as e:
        # Nettoyer le .new s'il existe — ne jamais corrompre le binaire actuel
        new_path = BINARY_PATH + ".new"
        try:
            if os.path.exists(new_path):
                os.remove(new_path)
        except Exception:
            pass
        raise HTTPException(500, f"Échec de l'installation du binaire: {e}")
    finally:
        # Nettoyer le dossier temporaire
        if tmp_dir:
            import shutil
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass

    # ── 7. Redémarrer le service systemd ──
    try:
        result = subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise HTTPException(
                500,
                f"Binaire mis à jour mais échec du redémarrage du service "
                f"'{SERVICE_NAME}': {stderr or 'erreur inconnue'}",
            )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            504,
            f"Binaire mis à jour mais timeout lors du redémarrage du service "
            f"'{SERVICE_NAME}'. Le service devrait redémarrer automatiquement.",
        )
    except FileNotFoundError:
        raise HTTPException(
            500,
            "systemctl introuvable — le binaire a été mis à jour mais le service "
            "n'a pas pu être redémarré automatiquement. Redémarrez manuellement.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            500,
            f"Binaire mis à jour mais erreur lors du redémarrage: {e}",
        )

    return {
        "status": "updated",
        "current": current_version,
        "latest": latest_version,
        "message": f"Mis à jour de {current_version} → {latest_version}. Service redémarré.",
    }


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
    """Statut du Kraken : liquidctl disponible, détection, températures."""
    available = kraken_available()
    detect = kraken_detect() if available else {"devices": [], "error": None}
    status = kraken_status() if available else {"ok": False, "data": {}, "raw": "", "error": None}
    return {
        "available": available,
        "detected": bool(detect.get("devices")),
        "devices": detect.get("devices", []),
        "status": status.get("data", {}) if status.get("ok") else {},
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
    """Paramètres du thread d'affichage (intervalle en secondes)."""
    interval: float = 10.0


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
    return kraken_gallery_start(max(2.0, body.interval))


@app.post("/api/kraken/monitor/start")
async def kraken_monitor_start_endpoint(body: KrakenDisplayBody):
    """Démarre le mode monitoring (stats système sur l'écran)."""
    return kraken_monitor_start(max(2.0, body.interval))


@app.post("/api/kraken/display/stop")
async def kraken_display_stop_endpoint():
    """Arrête le thread d'affichage actif (monitoring ou gallery)."""
    return kraken_stop_display()


@app.get("/api/kraken/display/status")
async def kraken_display_status_endpoint():
    """État du thread d'affichage actif."""
    return _display_thread_status()


@app.post("/api/kraken/monitor/preview")
async def kraken_monitor_preview_endpoint():
    """Génère un aperçu du rendu monitoring (pour la page web)."""
    return kraken_monitor_preview()


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
            pass  # Pas de display (systemd, SSH, etc.)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()

#!/usr/bin/env python3
"""
ballistix/kraken.py — Contrôle du NZXT Kraken Z53 via liquidctl.

Wrappe les commandes liquidctl en subprocess pour contrôler :
- L'écran LCD 640x640 (mode liquid / image statique / GIF)
- La luminosité et l'orientation de l'écran
- L'initialisation du périphérique (requise après chaque boot à froid)

Aucune dépendance externe : uniquement la stdlib (subprocess, shutil,
base64, pathlib).

Références liquidctl pour les Kraken Z :
    https://github.com/liquidctl/liquidctl
"""

import base64
import shutil
import subprocess
import threading
import time
from pathlib import Path

# ── Constantes ───────────────────────────────────────────────────────

LIQUIDCTL_CMD = "liquidctl"
NZXT_VENDOR_ID = "1e71"
KRAKEN_MATCH = "NZXT"          # Filtre --match pour sélectionner le Kraken
CMD_TIMEOUT = 15               # secondes
STORE_DIR_NAME = "kraken"      # Sous-dossier dans ~/.config/ballistix/
IMAGE_NAME = "screen.png"
GIF_NAME = "screen.gif"
GALLERY_DIR_NAME = "gallery"   # Images du diaporama

# ── Threads d'affichage (monitoring / gallery) ──────────────────────

_display_lock = threading.Lock()
_active_thread = None          # Thread courant (monitoring ou gallery)
_stop_event = None             # threading.Event pour arrêter le thread


# ── Utilitaires ──────────────────────────────────────────────────────

def _run_cmd(args, timeout=CMD_TIMEOUT):
    """Exécute une commande liquidctl sans jamais lever d'exception.

    Retourne un dict {"ok": bool, "code": int, "stdout": str, "stderr": str}.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": "liquidctl introuvable dans le PATH",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": f"Commande liquidctl expirée (> {timeout}s)",
        }
    except Exception as e:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": f"Erreur subprocess : {e}",
        }


def _store_dir() -> Path:
    """Retourne le dossier de stockage des images (~/.config/ballistix/kraken)."""
    d = Path.home() / ".config" / "ballistix" / STORE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gallery_dir() -> Path:
    """Retourne le dossier du diaporama gallery."""
    d = _store_dir() / GALLERY_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Détection ────────────────────────────────────────────────────────

def kraken_available() -> bool:
    """Vérifie si liquidctl est présent dans le PATH."""
    return shutil.which(LIQUIDCTL_CMD) is not None


def kraken_detect() -> dict:
    """Vérifie si un Kraken NZXT est présent sur le bus USB.

    Retourne {"available": bool, "devices": list, "error": str|None}.
    """
    if not kraken_available():
        return {
            "available": False,
            "devices": [],
            "error": "liquidctl n'est pas installé",
        }

    result = _run_cmd([LIQUIDCTL_CMD, "list"])
    if not result["ok"] and result["stderr"]:
        return {"available": True, "devices": [], "error": result["stderr"]}

    devices = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    # Le vendor NZXT (1e71) est présent dans la sortie de liquidctl list
    kraken_found = any(
        NZXT_VENDOR_ID in line or "Kraken" in line or "NZXT" in line.upper()
        for line in devices
    )
    return {
        "available": True,
        "devices": devices,
        "error": None if kraken_found else "Aucun périphérique NZXT détecté",
    }


# ── Statut ───────────────────────────────────────────────────────────

def kraken_status() -> dict:
    """Récupère le statut du Kraken (température liquide, vitesses).

    Retourne {"ok": bool, "data": {...}, "raw": str, "error": str|None}.
    """
    if not kraken_available():
        return {
            "ok": False,
            "data": {},
            "raw": "",
            "error": "liquidctl n'est pas installé",
        }

    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH, "status"])
    if not result["ok"]:
        return {
            "ok": False,
            "data": {},
            "raw": result["stdout"] or result["stderr"],
            "error": result["stderr"] or "liquidctl status a échoué",
        }

    # Parse la sortie texte de liquidctl status :
    #   NZXT Kraken Z (Z53, Z63 or Z73)
    #   ├── Liquid temperature     35.2°C
    #   ├── Fan speed               1200 rpm
    #   └── Pump speed              2100 rpm
    data = {}
    for line in result["stdout"].splitlines():
        line = line.strip().lstrip("├└│─ ")
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        value = parts[1].strip()
        if "temperature" in key.lower() or "temp" in key.lower():
            data["liquid_temperature"] = _parse_float(value)
        elif "fan" in key.lower() and "speed" in key.lower():
            data["fan_speed"] = _parse_float(value)
        elif "pump" in key.lower() and "speed" in key.lower():
            data["pump_speed"] = _parse_float(value)

    return {
        "ok": True,
        "data": data,
        "raw": result["stdout"],
        "error": None,
    }


def _parse_float(value: str):
    """Extrait le nombre flottant d'une chaîne type '35.2°C'."""
    import re
    m = re.search(r"[-+]?\d*\.?\d+", value)
    return float(m.group()) if m else None


# ── Actions ──────────────────────────────────────────────────────────

def kraken_initialize() -> dict:
    """Initialise le Kraken (requis après chaque boot à froid)."""
    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH, "initialize", "all"])
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or "Initialisation terminée",
        "error": None if result["ok"] else (result["stderr"] or "Échec de l'initialisation"),
    }


def kraken_set_lcd_mode(mode: str) -> dict:
    """Règle le mode de l'écran LCD.

    mode = "liquid" : affiche la température du liquide en continu (auto).
    """
    valid = {"liquid"}
    if mode not in valid:
        return {"ok": False, "error": f"Mode LCD invalide : {mode}"}

    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH,
                       "set", "lcd", "screen", mode])
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or f"Mode '{mode}' appliqué",
        "error": None if result["ok"] else (result["stderr"] or "Échec du réglage du mode"),
    }


def kraken_set_lcd_image(image_path: str, animated: bool = False) -> dict:
    """Affiche une image (PNG/JPEG) ou un GIF animé sur l'écran LCD."""
    if not Path(image_path).is_file():
        return {"ok": False, "error": f"Fichier introuvable : {image_path}"}

    action = "gif" if animated else "static"
    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH,
                       "set", "lcd", "screen", action, image_path])
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or f"Image {action} affichée",
        "error": None if result["ok"] else (result["stderr"] or f"Échec de l'affichage {action}"),
    }


def kraken_set_lcd_brightness(value: int) -> dict:
    """Règle la luminosité de l'écran (0-100)."""
    try:
        value = max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"Luminosité invalide : {value}"}

    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH,
                       "set", "lcd", "screen", "brightness", str(value)])
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or f"Luminosité réglée à {value}%",
        "error": None if result["ok"] else (result["stderr"] or "Échec du réglage de luminosité"),
    }


def kraken_set_lcd_orientation(value: int) -> dict:
    """Règle l'orientation de l'écran (0/90/180/270)."""
    valid = {0, 90, 180, 270}
    try:
        value = int(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"Orientation invalide : {value}"}
    if value not in valid:
        return {"ok": False, "error": f"Orientation invalide (0/90/180/270) : {value}"}

    result = _run_cmd([LIQUIDCTL_CMD, "--match", KRAKEN_MATCH,
                       "set", "lcd", "screen", "orientation", str(value)])
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or f"Orientation réglée à {value}°",
        "error": None if result["ok"] else (result["stderr"] or "Échec du réglage d'orientation"),
    }


def kraken_save_image(data: bytes, is_gif: bool) -> str:
    """Sauvegarde une image uploadée dans le dossier de stockage.

    Retourne le chemin absolu du fichier sauvegardé.
    """
    filename = GIF_NAME if is_gif else IMAGE_NAME
    dest = _store_dir() / filename
    dest.write_bytes(data)
    return str(dest)


def kraken_decode_b64(data_b64: str) -> bytes:
    """Décode une chaîne base64 (avec ou sans préfixe data:...;base64,)."""
    if data_b64.startswith("data:"):
        # Format data URI : data:image/png;base64,XXXX
        data_b64 = data_b64.split(",", 1)[1] if "," in data_b64 else data_b64
    return base64.b64decode(data_b64)


# ── Gallery (diaporama) ──────────────────────────────────────────────

def kraken_gallery_list() -> dict:
    """Liste les fichiers du dossier gallery.

    Retourne {"ok": bool, "files": [{"name": str, "size": int, "is_gif": bool}], "error": str|None}.
    """
    d = _gallery_dir()
    files = []
    try:
        for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "is_gif": f.suffix.lower() == ".gif",
                })
    except Exception as e:
        return {"ok": False, "files": [], "error": str(e)}
    return {"ok": True, "files": files, "error": None}


def kraken_gallery_add(data: bytes, filename: str) -> dict:
    """Ajoute une image/GIF dans le dossier gallery.

    Sécurise le nom de fichier (pas de path traversal) et évite
    les doublons en suffixant (2), (3)...
    """
    safe = Path(filename).name  # Ne garde que le nom de base
    if not safe or safe in ("", ".", ".."):
        return {"ok": False, "error": "Nom de fichier invalide"}
    ext = Path(safe).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        return {"ok": False, "error": f"Extension non supportée : {ext}"}

    d = _gallery_dir()
    dest = d / safe
    # Évite les collisions : nom.png → nom(2).png
    counter = 2
    while dest.exists():
        stem = dest.stem
        dest = d / f"{stem}({counter}){ext}"
        counter += 1
    try:
        dest.write_bytes(data)
    except Exception as e:
        return {"ok": False, "error": f"Sauvegarde impossible : {e}"}
    return {"ok": True, "name": dest.name, "error": None}


def kraken_gallery_delete(name: str) -> dict:
    """Supprime un fichier du dossier gallery."""
    safe = Path(name).name
    dest = _gallery_dir() / safe
    if not dest.is_file():
        return {"ok": False, "error": f"Fichier introuvable : {name}"}
    try:
        dest.unlink()
    except Exception as e:
        return {"ok": False, "error": f"Suppression impossible : {e}"}
    return {"ok": True, "error": None}


# ── Threads d'affichage (monitoring / gallery) ──────────────────────

def kraken_stop_display() -> dict:
    """Arrête le thread d'affichage actif (monitoring ou gallery)."""
    global _active_thread, _stop_event
    with _display_lock:
        if _stop_event:
            _stop_event.set()
        thread = _active_thread
        _active_thread = None
    if thread and thread.is_alive():
        thread.join(timeout=CMD_TIMEOUT + 5)
    return {"ok": True, "message": "Affichage automatique arrêté"}


def _display_thread_status() -> dict:
    """État du thread d'affichage actif."""
    global _active_thread
    with _display_lock:
        t = _active_thread
        if t and t.is_alive():
            return {"running": True, "name": t.name}
    return {"running": False, "name": None}


def kraken_gallery_start(interval: float = 15.0) -> dict:
    """Démarre le diaporama : affiche chaque image à tour de rôle."""
    global _active_thread, _stop_event

    gallery = kraken_gallery_list()
    files = [f for f in gallery.get("files", [])]
    if not files:
        return {"ok": False, "error": "La gallery est vide — ajoutez des images d'abord"}

    kraken_stop_display()

    def _gallery_loop():
        idx = 0
        while not _stop_event.is_set():
            f = files[idx % len(files)]
            path = str(_gallery_dir() / f["name"])
            result = kraken_set_lcd_image(path, animated=f.get("is_gif", False))
            if not result["ok"]:
                time.sleep(5)  # Erreur : retente dans 5s
                continue
            idx += 1
            _stop_event.wait(interval)

    with _display_lock:
        _stop_event = threading.Event()
        _active_thread = threading.Thread(
            target=_gallery_loop, name="kraken-gallery", daemon=True
        )
        _active_thread.start()
    return {"ok": True, "message": f"Diaporama démarré ({len(files)} fichier(s), {interval:.0f}s)"}


def kraken_monitor_start(interval: float = 10.0) -> dict:
    """Démarre le monitoring : génère une image de stats et l'envoie.

    Nécessite Pillow + psutil (module ballistix.monitor).
    """
    global _active_thread, _stop_event

    try:
        from .monitor import collect_system_stats, render_monitoring_image, monitor_available
    except ImportError:
        return {"ok": False, "error": "Module de monitoring indisponible (Pillow/psutil manquants)"}

    if not monitor_available():
        return {"ok": False, "error": "Pillow et/ou psutil ne sont pas installés — le mode monitoring est indisponible"}

    kraken_stop_display()
    screen_path = str(_store_dir() / "monitor.png")

    def _monitor_loop():
        while not _stop_event.is_set():
            stats = collect_system_stats()
            # Ajoute la température liquide du Kraken si dispo
            try:
                s = kraken_status()
                if s["ok"] and s["data"].get("liquid_temperature") is not None:
                    stats["liquid_temp"] = s["data"]["liquid_temperature"]
            except Exception:
                pass
            if render_monitoring_image(stats, screen_path, "MONITORING"):
                kraken_set_lcd_image(screen_path, animated=False)
            _stop_event.wait(interval)

    with _display_lock:
        _stop_event = threading.Event()
        _active_thread = threading.Thread(
            target=_monitor_loop, name="kraken-monitor", daemon=True
        )
        _active_thread.start()
    return {"ok": True, "message": f"Monitoring démarré (intervalle {interval:.0f}s)"}


def kraken_monitor_preview() -> dict:
    """Génère une image de monitoring de test (pour l'aperçu web).

    Retourne {"ok": bool, "path": str|None, "error": str|None}.
    """
    try:
        from .monitor import collect_system_stats, render_monitoring_image, monitor_available
    except ImportError:
        return {"ok": False, "path": None, "error": "Module de monitoring indisponible"}
    if not monitor_available():
        return {"ok": False, "path": None, "error": "Pillow/psutil manquants"}

    try:
        stats = collect_system_stats()
        s = kraken_status()
        if s["ok"] and s["data"].get("liquid_temperature") is not None:
            stats["liquid_temp"] = s["data"]["liquid_temperature"]
        path = str(_store_dir() / "monitor_preview.png")
        ok = render_monitoring_image(stats, path, "MONITORING")
        return {"ok": ok, "path": path if ok else None, "error": None if ok else "Rendu impossible"}
    except Exception as e:
        return {"ok": False, "path": None, "error": str(e)}

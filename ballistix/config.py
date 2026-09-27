#!/usr/bin/env python3
"""
ballistix/config.py — Configuration persistante pour Ballistix RGB Controller.

Stocke les devices manuels, couleurs, luminosité et préférences UI
dans ~/.config/ballistix/config.json (format JSON).
La configuration survit aux redémarrages du service.
"""

import copy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path.home() / ".config" / "ballistix"
CONFIG_FILE = CONFIG_DIR / "config.json"
BACKUP_DIR = CONFIG_DIR / "backups"
MAX_BACKUPS = 5

# ── Référence persistée (sémantique Save / Cancel) ───────────────────
# config.json représente la RÉFÉRENCE : le dernier état figé par
# « Enregistrer » (POST /api/save). Les modifications appliquées au
# matériel en temps réel ne touchent JAMAIS ce fichier tant qu'un Save
# n'a pas été demandé.
DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 3,
    "metadata": {
        "created": None,  # Set on first save
        "updated": None,  # Set on each save
    },
    "sticks": [],
    "stick_order": [],  # ["bus:0xaddr", ...] ordre des sticks
    "manual_devices": [],  # [{"bus": int, "addr": int}, ...] pour --add-device
    "colors": {},          # {"stick_id": [[R,G,B], ...], ...}
    "brightness": 255,
    "animation": {
        "type": "static",
        "enabled": False,
        "speed": 1.0,
        "framerate": 30,
        "refresh": 20,   # taux d'écriture SMBus en animation (Hz)
    },
    # Réglages Kraken (aucun readback liquidctl : l'état de référence est
    # ici, l'état « courant » vit en mémoire dans ballistix/kraken.py).
    "kraken": {
        "lcd": {
            "brightness": 80,     # 0-100
            "orientation": 0,      # 0/90/180/270
            "mode": "liquid",      # mode d'écran (liquid)
        },
        "display": {
            "mode": None,          # None | "monitor" | "gallery" (thread actif)
            "theme": "data_center",  # data_center | overclock | fluid_flow
            "options": ["cpu", "gpu", "ram", "vram", "disks", "liquid"],
            "interval": 10.0,      # secondes entre deux mises à jour (ou "asap")
        },
    },
    "ui": {
        "orientation": "vertical",
        "theme": "dark",
    },
}


# ═══════════════════════════════════════════════════════════════
# Utilitaires de dossier
# ═══════════════════════════════════════════════════════════════

def ensure_dirs() -> None:
    """Crée les dossiers de configuration et de backup si inexistants."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# Chargement / Sauvegarde
# ═══════════════════════════════════════════════════════════════

def load() -> dict:
    """Charge la configuration depuis le fichier JSON.

    Si le fichier n'existe pas, retourne la configuration par défaut
    avec le champ ``created`` initialisé.
    Si le fichier est corrompu, retourne la config par défaut après
    avoir affiché un avertissement.

    Returns:
        Dictionnaire de configuration complet (toujours avec toutes les
        clés de DEFAULT_CONFIG).
    """
    ensure_dirs()
    if not CONFIG_FILE.exists():
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["metadata"]["created"] = datetime.now().isoformat()
        return config

    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠ Erreur lecture config: {e}")
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["metadata"]["created"] = datetime.now().isoformat()
        return config

    # Fusionner avec les clés par défaut (pour conserver les nouvelles clés
    # introduites dans des versions ultérieures du schéma). La copie est
    # profonde : DEFAULT_CONFIG ne doit jamais être muté par un appelant.
    merged = copy.deepcopy(DEFAULT_CONFIG)
    _merge_into(merged, config)

    # Garantir les types des collections (le fichier peut être ancien/corrompu)
    if not isinstance(merged.get("manual_devices"), list):
        merged["manual_devices"] = []
    if not isinstance(merged.get("colors"), dict):
        merged["colors"] = {}
    if not isinstance(merged.get("stick_order"), list):
        merged["stick_order"] = []

    return merged


def _merge_into(target: dict, override: dict) -> None:
    """Fusionne récursivement ``override`` dans ``target`` (en place).

    Les dictionnaires sont fusionnés clé par clé ; toute autre valeur
    (liste, scalaire, None) remplace la valeur cible par une copie.
    """
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def save(config: dict) -> None:
    """Sauvegarde la configuration dans le fichier JSON.

    Crée automatiquement un backup de l'ancien fichier avant d'écrire
    le nouveau. Les vieux backups (au-delà de MAX_BACKUPS) sont supprimés.

    Args:
        config: Dictionnaire de configuration à sauvegarder.
    """
    ensure_dirs()
    config["metadata"]["updated"] = datetime.now().isoformat()

    # Backup de l'ancien fichier
    if CONFIG_FILE.exists():
        backup_name = f"config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        backup_path = BACKUP_DIR / backup_name
        try:
            shutil.copy2(CONFIG_FILE, backup_path)
            # Nettoyer les vieux backups
            backups = sorted(BACKUP_DIR.glob("config_*.json"))
            while len(backups) > MAX_BACKUPS:
                backups[0].unlink()
                backups = backups[1:]
        except OSError:
            pass

    # Écrire le nouveau fichier (atomique via écriture temp + rename)
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w") as f:
            json.dump(config, f, indent=2)
        temp_file.rename(CONFIG_FILE)
    except OSError as e:
        print(f"⚠ Erreur écriture config: {e}")
        # Nettoyer le fichier temporaire si l'écriture a échoué
        try:
            temp_file.unlink()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════
# Helpers pour des champs spécifiques
# ═══════════════════════════════════════════════════════════════

def add_manual_device(bus: int, addr: int) -> None:
    """Ajoute un device manuel à la configuration et sauvegarde.

    Évite les doublons (même bus + même adresse).

    Args:
        bus: Numéro du bus I2C/SMBus.
        addr: Adresse I2C du device (0x08-0x77).
    """
    config = load()
    devices: list = config.get("manual_devices", [])

    # Éviter les doublons
    for dev in devices:
        if dev.get("bus") == bus and dev.get("addr") == addr:
            return

    devices.append({"bus": bus, "addr": addr})
    config["manual_devices"] = devices
    save(config)


def remove_manual_device(bus: int, addr: int) -> None:
    """Supprime un device manuel de la configuration et sauvegarde.

    Args:
        bus: Numéro du bus I2C/SMBus.
        addr: Adresse I2C du device.
    """
    config = load()
    config["manual_devices"] = [
        d for d in config.get("manual_devices", [])
        if not (d.get("bus") == bus and d.get("addr") == addr)
    ]
    save(config)


def save_colors(colors: Dict[str, list]) -> None:
    """Sauvegarde les couleurs actuelles de tous les sticks.

    Args:
        colors: Dictionnaire {stick_id: [[R,G,B], ...], ...}
    """
    config = load()
    config["colors"] = colors
    save(config)


def save_brightness(level: int) -> None:
    """Sauvegarde le niveau de luminosité global.

    Args:
        level: Niveau de luminosité (0-255).
    """
    config = load()
    config["brightness"] = max(0, min(255, int(level)))
    save(config)


def save_stick_order(order: List[str]) -> None:
    """Sauvegarde l'ordre des sticks (liste de ``"bus:0xaddr"``)."""
    config = load()
    config["stick_order"] = [str(item) for item in order]
    save(config)


def save_animation(settings: dict) -> None:
    """Sauvegarde la section ``animation`` (speed/framerate/refresh…)."""
    config = load()
    merged = dict(config.get("animation") or {})
    merged.update(settings)
    config["animation"] = merged
    save(config)


def save_kraken(settings: dict) -> None:
    """Sauvegarde la section ``kraken`` (lcd/display), fusion partielle."""
    config = load()
    current = config.get("kraken") or {}
    for section in ("lcd", "display"):
        if isinstance(settings.get(section), dict):
            merged = dict(current.get(section) or {})
            merged.update(settings[section])
            current[section] = merged
    config["kraken"] = current
    save(config)


def save_ui_prefs(orientation: Optional[str] = None,
                  theme: Optional[str] = None) -> None:
    """Sauvegarde les préférences de l'interface utilisateur.

    Args:
        orientation: "vertical" ou "horizontal". Si None, inchangé.
        theme: "dark" ou "light". Si None, inchangé.
    """
    config = load()
    if orientation is not None:
        config["ui"]["orientation"] = orientation
    if theme is not None:
        config["ui"]["theme"] = theme
    save(config)


def get_config_path() -> str:
    """Retourne le chemin absolu du fichier de configuration."""
    return str(CONFIG_FILE)


# ═══════════════════════════════════════════════════════════════
# Exposition publique
# ═══════════════════════════════════════════════════════════════

__all__ = [
    "CONFIG_DIR",
    "CONFIG_FILE",
    "BACKUP_DIR",
    "DEFAULT_CONFIG",
    "ensure_dirs",
    "load",
    "save",
    "add_manual_device",
    "remove_manual_device",
    "save_colors",
    "save_brightness",
    "save_stick_order",
    "save_animation",
    "save_kraken",
    "save_ui_prefs",
    "get_config_path",
]

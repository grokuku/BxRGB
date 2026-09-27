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

from .effects import MODE_IDS, normalize_params
from .runner import clamp_framerate, clamp_refresh, clamp_speed

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
    "version": 4,
    "metadata": {
        "created": None,  # Set on first save
        "updated": None,  # Set on each save
    },
    "sticks": [],
    "stick_order": [],  # ["bus:0xaddr", ...] ordre des sticks
    "manual_devices": [],  # [{"bus": int, "addr": int}, ...] pour --add-device
    "colors": {},          # {"stick_id": [[R,G,B], ...], ...}
    "brightness": 255,
    # ── Section LUMIÈRE (canonique, schéma ≥ 4) ──────────────────
    # Couche persistable de l'état d'éclairage : mode ("static" ou id
    # d'effet), état marche/arrêt, réglages et paramètres d'effet. Les
    # frames d'animation ne sont JAMAIS écrites ici (couche transitoire).
    "lighting": {
        "mode": "static",   # "static" | "incandescence" | "rainbow"
        "running": False,    # moteur d'animation actif ?
        "speed": 1.0,        # 0.1–10.0
        "framerate": 30,     # 1–30 fps de rendu
        "refresh": 20,       # 1–30 écritures SMBus/sec
        "params": {},        # paramètres spécifiques à l'effet
    },
    # Ancienne section, conservée en MIROIR de rétrocompatibilité
    # (schéma ≤ 3). La lecture est migrée vers `lighting` au chargement.
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
            # Rendu = palette × disposition × capteurs.
            # `theme` est conservé comme MIROIR historique de `palette`
            # (rétrocompatibilité) ; la source de vérité est `palette`.
            "theme": "data_center",  # data_center | overclock | fluid_flow | graphite | amber
            "palette": "data_center",
            # Disposition par défaut : « duo » (grand format 2 colonnes).
            "layout": "duo",        # classic | duo | rings
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

    # Migration de schéma AVANT la fusion avec les défauts : un ancien
    # `theme` devient `palette` (à l'identique) + `layout="classic"`,
    # pour ne PAS changer le rendu d'une installation existante.
    _migrate_config(config)

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

    # Valider/borner la section lumière (défauts sains si absente/corrompue).
    merged["lighting"] = normalize_lighting(merged.get("lighting"))

    return merged


def lighting_from_animation(animation: Any) -> dict:
    """Applique la RÈGLE DE MIGRATION ``animation`` → ``lighting`` (schéma ≤ 3 → 4).

    Correspondances :
      - ``type``      → ``mode``      (inconnu/absent → ``"static"``) ;
      - ``enabled``   → ``running``   (forcé à False si le mode est ``static``,
        qui n'a pas de moteur) ;
      - ``speed``     → ``speed``     (défaut 1.0, borné 0.1–10.0) ;
      - ``framerate`` → ``framerate`` (défaut 30, borné 1–30) ;
      - ``refresh``   → ``refresh``   (défaut 20, borné 1–30) ;
      - ``params``    → ``params``    (dict, défaut {}).

    La section ``animation`` d'origine n'est PAS supprimée : elle reste un
    miroir de rétrocompatibilité (le temps de migration du front).
    """
    src = animation if isinstance(animation, dict) else {}
    mode = src.get("type", "static")
    if not isinstance(mode, str) or mode not in MODE_IDS:
        mode = "static"
    running = bool(src.get("enabled", False)) and mode != "static"
    params = src.get("params") if isinstance(src.get("params"), dict) else {}
    return {
        "mode": mode,
        "running": running,
        "speed": clamp_speed(src.get("speed", 1.0)),
        "framerate": clamp_framerate(src.get("framerate", 30)),
        "refresh": clamp_refresh(src.get("refresh", 20)),
        "params": dict(params),
    }


def normalize_lighting(section: Any) -> dict:
    """Valide/borne une section ``lighting`` (ne lève jamais).

    Garantit les clés et types du schéma courant : mode connu (sinon
    ``"static"``), ``running`` forcé à False pour ``static``, réglages
    bornés et paramètres d'effet complétés par leurs défauts.
    """
    src = section if isinstance(section, dict) else {}
    mode = src.get("mode", "static")
    if not isinstance(mode, str) or mode not in MODE_IDS:
        mode = "static"
    running = bool(src.get("running", False)) and mode != "static"
    params = src.get("params") if isinstance(src.get("params"), dict) else {}
    params = {} if mode == "static" else normalize_params(mode, params)
    return {
        "mode": mode,
        "running": running,
        "speed": clamp_speed(src.get("speed", 1.0)),
        "framerate": clamp_framerate(src.get("framerate", 30)),
        "refresh": clamp_refresh(src.get("refresh", 20)),
        "params": params,
    }


def _migrate_config(config: dict) -> None:
    """Migre en place un ``config.json`` ancien vers le schéma courant.

    Règles retenues (documentées) :
      - ancien ``animation`` présent et ``lighting`` absent → construction
        de ``lighting`` par :func:`lighting_from_animation` (``type``→``mode``,
        ``enabled``→``running``, bornes saines sur speed/framerate/refresh) ;
      - ancien ``kraken.display.theme`` présent et ``palette`` absent →
        ``palette = theme`` (mêmes couleurs) et ``layout = "classic"``,
        afin de préserver EXACTEMENT le rendu des installations existantes ;
      - ``palette`` présent sans ``theme`` → ``theme = palette`` (miroir de
        rétrocompatibilité) ;
      - ni l'un ni l'autre → les défauts s'appliquent (``data_center`` +
        ``layout="duo"``), c'est le cas d'une installation neuve.
    """
    if not isinstance(config, dict):
        return

    # 1. Lighting ← animation (indépendant de la présence de `kraken`).
    if not isinstance(config.get("lighting"), dict) or not config.get("lighting"):
        if isinstance(config.get("animation"), dict):
            config["lighting"] = lighting_from_animation(config["animation"])

    # 2. Kraken : theme ↔ palette.
    kraken = config.get("kraken")
    if not isinstance(kraken, dict):
        return
    display = kraken.get("display")
    if not isinstance(display, dict):
        return

    if "theme" in display and "palette" not in display:
        display["palette"] = display["theme"]
        display.setdefault("layout", "classic")
    if "palette" in display and "theme" not in display:
        display["theme"] = display["palette"]


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
    "normalize_lighting",
    "lighting_from_animation",
    "add_manual_device",
    "remove_manual_device",
    "save_colors",
    "save_brightness",
    "save_stick_order",
    "save_kraken",
    "save_ui_prefs",
    "get_config_path",
]

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
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

# ── Constantes ───────────────────────────────────────────────────────

LIQUIDCTL_CMD = "liquidctl"
NZXT_VENDOR_ID = "1e71"
# Filtre --match : toutes les descriptions liquidctl des Kraken contiennent
# « Kraken ». Le seul vendor (« NZXT ») matcherait aussi d'autres devices
# NZXT (RGB controller, HUE…) et rendrait la sélection ambiguë ; la
# description est plus fiable qu'un index de device supposé.
KRAKEN_MATCH = "Kraken"
CMD_TIMEOUT = 15               # secondes
STORE_DIR_NAME = "kraken"      # Sous-dossier dans ~/.config/ballistix/
IMAGE_NAME = "screen.png"
GIF_NAME = "screen.gif"
GALLERY_DIR_NAME = "gallery"   # Images du diaporama
BACKUP_DIR_NAME = "backups"    # Sauvegardes temporaires (annulabilité Save/Cancel)
TRASH_DIR_NAME = "trash"       # Corbeille des suppressions de gallery
SESSION_FILE_NAME = "session.json"  # Journal de session (fichiers ajoutés)
THUMBS_DIR_NAME = "thumbs"     # Cache disque des vignettes de thèmes (API web)
THUMB_ENGINE_VERSION = 2       # À incrémenter si la mécanique de vignette change

# ── Cadence d'actualisation ─────────────────────────────────────────
# liquidctl ne sait pas pousser plusieurs images par seconde : chaque image
# coûte un subprocess (~2-3 s). L'UI reste donc honnête (aucun FPS promis).
ASAP_INTERVAL = "asap"         # « le plus souvent possible » (aucune attente)
MIN_INTERVAL = 2.0            # plancher réaliste (un subprocess par image)
MAX_INTERVAL = 60.0
DEFAULT_INTERVAL = 10.0

# Options d'affichage monitoring connues (miroir de ballistix/monitor.py).
DEFAULT_OPTIONS = ["cpu", "gpu", "ram", "vram", "disks", "liquid"]

# ── État mémoire (liquidctl ne fait AUCUN readback des réglages) ────
# L'état « courant » du Kraken vit ici : il est mis à jour à chaque
# opération RÉUSSIE. La référence persistée est dans config.json
# (section "kraken") ; Save/Cancel comparent/écrivent/réappliquent.
# Les valeurs par défaut miroitent celles de ballistix/config.py.

_state_lock = threading.Lock()
_settings = {
    "lcd": {
        "brightness": 80,
        "orientation": 0,
        "mode": "liquid",
    },
    "display": {
        "mode": None,              # None | "monitor" | "gallery"
        "theme": "data_center",    # miroir historique de `palette`
        "palette": "data_center",  # data_center | overclock | fluid_flow | graphite | amber
        "layout": "duo",           # classic | duo | rings (défaut : grand format)
        "options": list(DEFAULT_OPTIONS),
        "interval": DEFAULT_INTERVAL,
    },
}

# ── Threads d'affichage (monitoring / gallery) ──────────────────────

_display_lock = threading.Lock()
_active_thread = None          # Thread courant (monitoring ou gallery)
_stop_event = None             # threading.Event pour arrêter le thread


def kraken_get_settings() -> dict:
    """Retourne une copie de l'état mémoire (lcd + display)."""
    with _state_lock:
        return {
            "lcd": dict(_settings["lcd"]),
            "display": dict(_settings["display"]),
        }


def kraken_update_settings(lcd: dict = None, display: dict = None) -> dict:
    """Met à jour l'état mémoire (clés partielles acceptées).

    Appelé à chaque opération réussie sur le matériel, et par le serveur
    au démarrage / au restore pour aligner la mémoire sur la référence.

    Rétrocompatibilité « thème » : un ``display`` ne contenant que
    ``theme`` est interprété comme ``palette=theme`` + ``layout="classic"``
    (rendu historique). ``theme`` reste un miroir de ``palette``.

    Returns:
        L'état mémoire complet après mise à jour.
    """
    with _state_lock:
        if isinstance(lcd, dict):
            _settings["lcd"].update(lcd)
        if isinstance(display, dict):
            incoming = dict(display)
            if "theme" in incoming and "palette" not in incoming:
                incoming["palette"] = incoming["theme"]
                incoming.setdefault("layout", "classic")
            if "palette" in incoming and "theme" not in incoming:
                incoming["theme"] = incoming["palette"]
            _settings["display"].update(incoming)
            # Garantit la présence des clés du nouveau modèle.
            disp = _settings["display"]
            if "palette" not in disp:
                disp["palette"] = disp.get("theme", "data_center")
            if "theme" not in disp:
                disp["theme"] = disp["palette"]
            if "layout" not in disp:
                disp["layout"] = "duo"
    return kraken_get_settings()


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


def _backup_dir() -> Path:
    """Sauvegardes temporaires des fichiers écrasés pendant la session.

    Emplacement : ``~/.config/ballistix/kraken/backups/``. Une sauvegarde
    par nom de fichier (la PREMIÈRE version rencontrée dans la session est
    conservée : c'est elle qui correspond à la référence). Purge par
    POST /api/save, restauration par POST /api/restore.
    """
    d = _store_dir() / BACKUP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trash_dir() -> Path:
    """Corbeille des fichiers supprimés de la gallery.

    Emplacement : ``~/.config/ballistix/kraken/trash/``. Même règle de
    première version conservée. Purge par POST /api/save, restauration
    par POST /api/restore.
    """
    d = _store_dir() / TRASH_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _thumbs_dir() -> Path:
    """Cache disque des vignettes de thèmes (galerie web).

    Emplacement : ``~/.config/ballistix/kraken/thumbs/``. Les fichiers
    sont nommés ``<thème>-<empreinte moteur>.png`` : une empreinte
    différente (monitor.py modifié) crée un nouveau fichier, l'ancien
    est nettoyé à la régénération.
    """
    d = _store_dir() / THUMBS_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_engine_key() -> str:
    """Empreinte du moteur de rendu (contenu de ``ballistix/monitor.py``).

    Toute évolution du rendu (polices, géométrie, stats d'exemple, heure
    figée) change l'empreinte et invalide le cache. Le hash de CONTENU est
    plus fiable qu'un mtime (copie/restauration conserve le mtime).
    """
    try:
        from . import monitor as monitor_module
        source = Path(monitor_module.__file__).read_bytes()
        digest = hashlib.sha256(source).hexdigest()[:12]
    except Exception:
        digest = "unknown"
    return f"v{THUMB_ENGINE_VERSION}-{digest}"


def kraken_theme_thumb(key: str) -> dict:
    """Chemin de la vignette PNG d'une PALETTE ou d'une DISPOSITION.

    Un seul endpoint sert les deux catalogues : ``key`` est résolu tour à
    tour comme clé de palette (vignette rendue en disposition « classic »,
    comme les vignettes historiques) ou de disposition (rendue avec la
    palette par défaut). Cache disque + régénération comme avant.

    Retourne ``{"ok", "path", "cached", "error", "code", "kind"}`` ;
    ``code`` vaut ``"unknown"`` (clé inconnue), ``"unavailable"``
    (Pillow absent) ou ``"render"`` (échec de génération). Une vignette
    déjà en cache n'est jamais régénérée tant que l'empreinte du moteur
    ne change pas.
    """
    try:
        from .monitor import (
            PIL_AVAILABLE, PALETTES, LAYOUTS,
            render_theme_thumbnail, render_layout_thumbnail,
        )
    except ImportError:
        return {"ok": False, "path": None, "cached": False, "kind": None,
                "error": "Module de monitoring indisponible",
                "code": "unavailable"}

    if key in PALETTES:
        kind, prefix, renderer = "palette", "", render_theme_thumbnail
    elif key in LAYOUTS:
        kind, prefix, renderer = "layout", "layout-", render_layout_thumbnail
    else:
        return {"ok": False, "path": None, "cached": False, "kind": None,
                "error": f"Palette ou disposition inconnue : {key}",
                "code": "unknown"}
    if not PIL_AVAILABLE:
        return {"ok": False, "path": None, "cached": False, "kind": kind,
                "error": "Pillow manquant", "code": "unavailable"}

    thumbs = _thumbs_dir()
    dest = thumbs / f"{prefix}{key}-{_render_engine_key()}.png"
    if dest.is_file() and dest.stat().st_size > 0:
        return {"ok": True, "path": str(dest), "cached": True,
                "error": None, "code": None, "kind": kind}

    temp = thumbs / f"{dest.name}.tmp"
    if not renderer(key, str(temp)):
        try:
            temp.unlink()
        except OSError:
            pass
        return {"ok": False, "path": None, "cached": False, "kind": kind,
                "error": "Échec du rendu de la vignette", "code": "render"}
    try:
        os.replace(temp, dest)
    except OSError as e:
        try:
            temp.unlink()
        except OSError:
            pass
        return {"ok": False, "path": None, "cached": False, "kind": kind,
                "error": f"Écriture impossible : {e}", "code": "render"}

    # Ménage : les vignettes d'une ancienne empreinte pour cette clé.
    try:
        for old in thumbs.glob(f"{prefix}{key}-*.png"):
            if old.name != dest.name:
                old.unlink()
    except OSError:
        pass

    return {"ok": True, "path": str(dest), "cached": False,
            "error": None, "code": None, "kind": kind}


def kraken_palette_thumb(palette_key: str) -> dict:
    """Vignette d'une palette (alias explicite de l'endpoint générique)."""
    return kraken_theme_thumb(palette_key)


def kraken_layout_thumb(layout_key: str) -> dict:
    """Vignette d'une disposition (alias explicite de l'endpoint générique)."""
    return kraken_theme_thumb(layout_key)


def kraken_purge_thumbs() -> dict:
    """Purge globale du cache des vignettes de thèmes.

    Supprime dans ``~/.config/ballistix/kraken/thumbs/`` les vignettes
    dont l'empreinte du moteur de rendu n'est plus courante (images
    laissées par une ancienne version de ``monitor.py``) ainsi que les
    fichiers temporaires ``*.png.tmp`` d'un rendu interrompu.

    Appelée au démarrage du daemon, elle sert aussi de point d'entrée
    explicite pour une purge manuelle. Retourne
    ``{"ok", "engine", "removed", "kept", "errors", "error"}``.

    Garde-fous : seuls des FICHIERS du dossier de cache sont supprimés
    (liens symboliques et sous-dossiers ignorés), les autres extensions
    ne sont pas touchées, et les erreurs d'E/S sont comptées sans être
    propagées. Si l'empreinte du moteur est indisponible, rien n'est
    supprimé (mieux vaut une purge ratée que des vignettes valides
    effacées).
    """
    engine = _render_engine_key()
    if engine.endswith("-unknown"):
        return {"ok": False, "engine": engine, "removed": 0, "kept": 0,
                "errors": 0, "error": "Empreinte du moteur indisponible"}
    try:
        thumbs = _thumbs_dir()
        base = thumbs.resolve()
        entries = list(thumbs.iterdir())
    except OSError as e:
        return {"ok": False, "engine": engine, "removed": 0, "kept": 0,
                "errors": 1, "error": str(e)}

    current = f"-{engine}.png"
    removed = kept = errors = 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            # Ceinture + bretelles : le fichier doit être DANS le cache.
            if entry.resolve().parent != base:
                continue
            name = entry.name
            if name.endswith(".png.tmp") or (
                name.endswith(".png") and not name.endswith(current)
            ):
                entry.unlink()
                removed += 1
            elif name.endswith(".png"):
                kept += 1
        except OSError:
            errors += 1

    return {"ok": True, "engine": engine, "removed": removed,
            "kept": kept, "errors": errors, "error": None}


def _session_file() -> Path:
    """Journal de session (fichiers ajoutés à la gallery)."""
    return _store_dir() / SESSION_FILE_NAME


def _load_session() -> dict:
    """Lit le journal de session ; robuste à un fichier absent/corrompu."""
    try:
        data = json.loads(_session_file().read_text())
        if isinstance(data, dict) and isinstance(data.get("gallery_added"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"gallery_added": []}


def _save_session(gallery_added: list) -> None:
    """Écrit le journal de session (écriture temp + remplacement atomique)."""
    path = _session_file()
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps({"gallery_added": list(gallery_added)}, indent=2))
        temp.replace(path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def _register_gallery_add(name: str) -> None:
    """Mémorise un fichier ajouté à la gallery (annulable par restore)."""
    session = _load_session()
    added = session["gallery_added"]
    if name not in added:
        added.append(name)
    _save_session(added)


def _backup_existing(path: Path, backup_dir: Path) -> bool:
    """Copie ``path`` dans ``backup_dir`` si aucune sauvegarde n'existe.

    La première version rencontrée dans la session est conservée : les
    écrasements suivants ne modifient pas la sauvegarde (elle représente
    la référence à restaurer). Retourne True si une copie a été créée.
    """
    dest = backup_dir / path.name
    if dest.exists() or not path.is_file():
        return False
    try:
        shutil.copy2(path, dest)
        return True
    except OSError:
        return False


def _unique_path(directory: Path, name: str) -> Path:
    """Retourne un chemin libre dans ``directory`` (suffixe (2), (3)…)."""
    candidate = directory / Path(name).name
    counter = 2
    while candidate.exists():
        candidate = directory / f"{Path(name).stem}({counter}){Path(name).suffix}"
        counter += 1
    return candidate


# ── Cadence : normalisation & ordonnancement à date-butoir ──────────

def normalize_interval(interval):
    """Normalise un intervalle d'actualisation.

    Accepte un nombre (clampé dans [MIN_INTERVAL, MAX_INTERVAL]) ou la
    sentinelle ``"asap"`` (« le plus souvent possible »). Toute valeur
    invalide retombe sur ``DEFAULT_INTERVAL``.
    """
    if isinstance(interval, str) and interval.strip().lower() == ASAP_INTERVAL:
        return ASAP_INTERVAL
    try:
        value = float(interval)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    if value != value:  # NaN
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, value))


def interval_target_seconds(interval) -> float:
    """Cadence cible en secondes (0.0 pour « asap » = aucune attente)."""
    if interval == ASAP_INTERVAL:
        return 0.0
    try:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, float(interval)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL


def next_wait_seconds(interval, elapsed) -> float:
    """Temps d'attente pour ne pas DÉRIVER quand le rendu+push prend du temps.

    Ordonnancement à date-butoir : on retire le temps déjà écoulé dans le
    cycle (collecte + rendu + subprocess liquidctl) de la cadence cible.
    Un cycle plus long que la cible n'attend pas du tout (``max(0, …)``).
    """
    try:
        elapsed = max(0.0, float(elapsed))
    except (TypeError, ValueError):
        elapsed = 0.0
    return max(0.0, interval_target_seconds(interval) - elapsed)


def _normalize_options(options):
    """Copie normalisée de la liste de capteurs à afficher."""
    if options is None:
        return list(DEFAULT_OPTIONS)
    return [str(o) for o in options]


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

# Clés du statut exposées par l'API (le front les affiche toutes).
STATUS_FIELDS = ("liquid_temperature", "pump_speed", "fan_speed")

# Libellés liquidctl → clé interne. Les libellés réels sont multi-mots
# (« Liquid temperature », « Fan speed », « Pump speed ») et ont varié
# selon les versions/modèles (Kraken X plat, Kraken Z en arbre, JSON).
# Les patterns sont testés du plus spécifique au plus générique, sans
# tenir compte de la casse, sur la ligne COMPLÈTE.
_STATUS_LABEL_PATTERNS = {
    "liquid_temperature": (
        "liquid temperature", "coolant temperature", "water temperature",
        "temperature", "temp",
    ),
    "pump_speed": ("pump speed", "pump rpm"),
    "fan_speed": ("fan speed", "fan rpm"),
}

# Libellés français des clés (messages d'avertissement côté serveur).
STATUS_FIELD_LABELS = {
    "liquid_temperature": "température liquide",
    "pump_speed": "vitesse pompe",
    "fan_speed": "vitesse ventilos",
}

# Préfixes d'arbre / puces des sorties liquidctl : « ├── », « └── », « │ »,
# « |-- », « +-- », tirets, espaces (dont insécables).
_TREE_PREFIX_CHARS = " \t\u00a0\u202f│├└┌┐┘┤┬┴─═╌╭╰|+`-–—"

# Nombre : signe optionnel, milliers (espaces fines/insécables) puis
# décimales . ou , (localisation). Le premier groupe évite de tronquer
# « 2 117 rpm » en « 2 » ; le suffixe répétable capture les deux
# séparateurs des formats mixtes (« 1.234,5 » → 1234,5).
_NUMBER_RE = re.compile(
    r"[-+]?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)*"
    r"|[-+]?\d+(?:[.,]\d+)*"
)


def _parse_float(value):
    """Extrait un nombre d'une valeur type ``'35.2°C'``, ``'2 117 rpm'``, ``35.2``.

    Tolère unités collées ou séparées, milliers espacés, virgule décimale,
    signe et espaces insécables. Retourne ``None`` si aucun nombre n'est
    trouvé — jamais d'exception, jamais de zéro inventé.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u00a0", " ").replace("\u202f", " ")
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    return _number_to_float(match.group())


def _number_to_float(token: str):
    """Normalise un jeton numérique (milliers, virgule décimale) en float."""
    token = token.replace(" ", "")
    if "," in token and "." in token:
        # Le DERNIER séparateur est la décimale, l'autre un millier.
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        entier, _, fraction = token.partition(",")
        # « 35,2 » → décimale ; « 2,117 » → milliers. Les valeurs liquidctl
        # n'utilisent jamais de milliers : un groupe de 3 chiffres
        # exactement est la signature d'un séparateur de milliers.
        token = entier + ("." if len(fraction) != 3 else "") + fraction
    try:
        return float(token)
    except ValueError:
        return None


def _classify_status_label(label: str):
    """Associe un libellé liquidctl (« Liquid temperature »…) à une clé."""
    if not label:
        return None
    low = label.lower()
    for key, patterns in _STATUS_LABEL_PATTERNS.items():
        if any(pattern in low for pattern in patterns):
            return key
    return None


def _value_for_label(text: str, key: str):
    """Valeur numérique qui SUIT le libellé ``key`` dans ``text``.

    On cherche après le libellé pour ne jamais capturer un chiffre contenu
    dans le libellé lui-même (ex. « LED 1 »).
    """
    low = text.lower()
    for pattern in _STATUS_LABEL_PATTERNS[key]:
        idx = low.find(pattern)
        if idx >= 0:
            return _parse_float(text[idx + len(pattern):])
    return None


def _parse_status_text(raw: str) -> dict:
    """Extrait les capteurs d'une sortie ``liquidctl status`` en texte.

    Tolérant par construction : arborescence ├──/└──/│ ou « |-- », casse
    libre, espaces multiples, libellés multi-mots, unités collées ou
    séparées, décimales . ou ,. Retourne ``{clé: float}`` pour les seules
    valeurs reconnues ET numériques ; les autres restent ABSENTES (le
    statut les signalera comme indisponibles, sans les confondre avec 0).
    """
    data = {}
    for line in (raw or "").splitlines():
        cleaned = line.strip().lstrip(_TREE_PREFIX_CHARS)
        if not cleaned:
            continue
        key = _classify_status_label(cleaned)
        if key is None or key in data:
            continue
        number = _value_for_label(cleaned, key)
        if number is not None:
            data[key] = number
    return data


def _iter_status_entries(status):
    """Parcourt un statut JSON en paires (libellé, valeur).

    Deux formes acceptées : la forme RÉELLE de ``liquidctl status --json``
    (liste de ``{"key", "value", "unit"}``) et la variante dict
    ``{label: valeur | {"value": …}}`` (robustesse/versions futures).
    """
    if isinstance(status, dict):
        for label, entry in status.items():
            if isinstance(entry, dict) and "value" in entry:
                yield str(label), entry["value"]
            else:
                yield str(label), entry
    elif isinstance(status, list):
        for entry in status:
            if not isinstance(entry, dict):
                continue
            label = entry.get("key", entry.get("label"))
            if label is None:
                continue
            yield str(label), entry.get("value")


def _parse_status_json(raw: str):
    """Extrait les capteurs d'une sortie ``liquidctl status --json``.

    Accepte la liste de devices (format documenté), un objet unique, ou
    un dict direct. Retourne ``(data, reconnu)`` : ``reconnu`` distingue
    « ce n'était pas du JSON liquidctl » (→ repli texte) de « du JSON
    sans les valeurs » (→ indisponibilité explicite).
    """
    if not raw or not raw.strip():
        return {}, False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}, False

    devices = payload if isinstance(payload, list) else [payload]
    data = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        status = device.get("status")
        if not isinstance(status, (dict, list)):
            continue
        for label, value in _iter_status_entries(status):
            key = _classify_status_label(label)
            if key is None or key in data:
                continue
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                continue  # listes (canaux RGB…), None, booléens
            number = _parse_float(value)
            if number is not None:
                data[key] = number
    return data, True


# Support de `liquidctl status --json` (liquidctl ≥ 1.5) : inconnu au
# premier appel, puis mémorisé pour la session — évite un double
# subprocess à chaque interrogation si la version ne le supporte pas.
_json_status_probe = None


def _status_command(use_json: bool):
    """Commande `liquidctl status` (JSON ou texte), filtre Kraken inclus."""
    args = [LIQUIDCTL_CMD, "--match", KRAKEN_MATCH, "status"]
    if use_json:
        args.append("--json")
    return args


def kraken_status() -> dict:
    """Récupère le statut du Kraken (température liquide, vitesses).

    La sortie de ``liquidctl status`` est préférée en JSON (``--json``,
    le plus robuste aux variations de format), avec repli sur le texte
    (parseur tolérant : arbre/plat, libellés multi-mots, unités,
    décimales).

    Retourne ``{"ok", "data", "raw", "error", "missing", "source"}`` :
    - ``ok`` : la commande liquidctl a RÉUSSI (pas « toutes les valeurs
      trouvées ») ; ``data`` reste vide en cas d'échec ;
    - ``data`` : valeurs extraites ; une clé ABSENTE est indisponible —
      jamais remplacée par un zéro (0 est une valeur légitime) ;
    - ``error`` : None si complet, sinon explication (échec commande,
      sortie non reconnue, valeurs non remontées) — aussi journalisée ;
    - ``missing`` : clés de ``STATUS_FIELDS`` non extraites ;
    - ``source`` : "json" | "text" (None si la commande a échoué).
    """
    if not kraken_available():
        return {
            "ok": False, "data": {}, "raw": "",
            "error": "liquidctl n'est pas installé",
            "missing": list(STATUS_FIELDS), "source": None,
        }

    global _json_status_probe
    data, raw, source = {}, "", None

    # 1) Tentative JSON (`liquidctl status --json`) si la version le supporte.
    if _json_status_probe is not False:
        result = _run_cmd(_status_command(True))
        if result["ok"]:
            data, recognized = _parse_status_json(result["stdout"])
            if recognized:
                _json_status_probe = True
                raw, source = result["stdout"], "json"
            else:
                _json_status_probe = False
                _logger.debug(
                    "liquidctl status --json non exploitable (code=%s) : repli texte",
                    result["code"])
        else:
            _json_status_probe = False
            _logger.debug(
                "liquidctl status --json indisponible (code=%s, stderr=%r) : repli texte",
                result["code"], result["stderr"][:200])

    # 2) Repli texte (toutes versions confondues).
    if source is None:
        result = _run_cmd(_status_command(False))
        if not result["ok"]:
            error = result["stderr"] or "liquidctl status a échoué"
            _logger.warning("liquidctl status a échoué (code=%s) : %s",
                            result["code"], error[:300])
            return {
                "ok": False, "data": {}, "raw": result["stdout"] or result["stderr"],
                "error": error, "missing": list(STATUS_FIELDS), "source": None,
            }
        data = _parse_status_text(result["stdout"])
        raw, source = result["stdout"], "text"

    # 3) Indisponibilité EXPLICITE : ne jamais rendre des tirets muets.
    missing = [k for k in STATUS_FIELDS if k not in data]
    error = None
    if len(missing) == len(STATUS_FIELDS):
        error = ("Sortie liquidctl non reconnue : aucune valeur extraite "
                 f"(source={source}) — voir la sortie brute")
    elif missing:
        details = ", ".join(STATUS_FIELD_LABELS[k] for k in missing)
        error = f"Valeurs non remontées par liquidctl : {details}"
    if error:
        _logger.warning("Kraken : %s", error)

    return {
        "ok": True, "data": data, "raw": raw,
        "error": error, "missing": missing, "source": source,
    }


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
    if result["ok"]:
        kraken_update_settings(lcd={"mode": mode})
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
    if result["ok"]:
        kraken_update_settings(lcd={"brightness": value})
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
    if result["ok"]:
        kraken_update_settings(lcd={"orientation": value})
    return {
        "ok": result["ok"],
        "message": result["stdout"] or result["stderr"] or f"Orientation réglée à {value}°",
        "error": None if result["ok"] else (result["stderr"] or "Échec du réglage d'orientation"),
    }


def kraken_save_image(data: bytes, is_gif: bool) -> str:
    """Sauvegarde une image uploadée dans le dossier de stockage.

    Annulabilité (Save/Cancel) : l'ancienne image (screen.png/screen.gif)
    est copiée dans ``~/.config/ballistix/kraken/backups/`` AVANT d'être
    écrasée, et restaurée par POST /api/restore (purgée par /api/save).

    Retourne le chemin absolu du fichier sauvegardé.
    """
    filename = GIF_NAME if is_gif else IMAGE_NAME
    dest = _store_dir() / filename
    _backup_existing(dest, _backup_dir())
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
    dest = _unique_path(d, safe)
    try:
        dest.write_bytes(data)
    except Exception as e:
        return {"ok": False, "error": f"Sauvegarde impossible : {e}"}
    # Annulabilité : mémorise l'ajout (retiré par POST /api/restore,
    # oublié par POST /api/save).
    _register_gallery_add(dest.name)
    return {"ok": True, "name": dest.name, "error": None}


def kraken_gallery_delete(name: str) -> dict:
    """Supprime un fichier du dossier gallery.

    Annulabilité : le fichier est DÉPLACÉ vers la corbeille
    (``~/.config/ballistix/kraken/trash/``) au lieu d'un unlink. La
    purge est faite par POST /api/save, la restauration par /api/restore.
    """
    safe = Path(name).name
    dest = _gallery_dir() / safe
    if not dest.is_file():
        return {"ok": False, "error": f"Fichier introuvable : {name}"}
    try:
        trash = _trash_dir() / safe
        if not trash.exists():
            # Première version rencontrée dans la session = référence.
            shutil.copy2(dest, trash)
        dest.unlink()
    except Exception as e:
        return {"ok": False, "error": f"Suppression impossible : {e}"}
    return {"ok": True, "error": None}


# ── Annulabilité des fichiers (Save / Cancel) ────────────────────────

def kraken_commit_file_changes() -> dict:
    """Purge les sauvegardes temporaires (appelé par POST /api/save).

    L'état courant devient la référence : plus rien à restaurer.
    """
    counts = {"screens": 0, "gallery": 0, "added_forgotten": 0}
    for directory, key in ((_backup_dir(), "screens"), (_trash_dir(), "gallery")):
        for f in directory.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    counts[key] += 1
                except OSError:
                    pass
    counts["added_forgotten"] = len(_load_session().get("gallery_added", []))
    _save_session([])
    return counts


def kraken_restore_file_changes() -> dict:
    """Restaure les fichiers depuis backups/ et trash/ (POST /api/restore).

    1. Les images d'écran sauvegardées reviennent dans le dossier de stockage.
    2. Les fichiers en corbeille reviennent dans la gallery (si un fichier
       du même nom ajouté pendant la session est présent, il cède la place).
    3. Les fichiers ajoutés pendant la session (journal session.json) et
       encore présents sont retirés : l'ajout aussi est annulable.
    """
    counts = {"screens": 0, "gallery": 0, "added_removed": 0}
    store = _store_dir()
    gallery = _gallery_dir()
    session = _load_session()
    added = list(session.get("gallery_added", []))

    # 1) Images d'écran écrasées
    for f in sorted(_backup_dir().iterdir()):
        if not f.is_file():
            continue
        try:
            shutil.move(str(f), str(store / f.name))
            counts["screens"] += 1
        except OSError:
            pass

    # 2) Corbeille gallery
    for f in sorted(_trash_dir().iterdir()):
        if not f.is_file():
            continue
        dest = gallery / f.name
        try:
            if dest.exists():
                if f.name in added:
                    # Ré-ajout pendant la session : l'ajout cède la place
                    dest.unlink()
                    added.remove(f.name)
                else:
                    dest = _unique_path(gallery, f.name)
            shutil.move(str(f), str(dest))
            counts["gallery"] += 1
        except OSError:
            pass

    # 3) Ajouts de la session encore présents
    for name in added:
        p = gallery / Path(name).name
        if p.is_file():
            try:
                p.unlink()
                counts["added_removed"] += 1
            except OSError:
                pass

    _save_session([])
    return counts


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
    kraken_update_settings(display={"mode": None})
    return {"ok": True, "message": "Affichage automatique arrêté"}


def _display_thread_status() -> dict:
    """État du thread d'affichage actif."""
    global _active_thread
    with _display_lock:
        t = _active_thread
        if t and t.is_alive():
            return {"running": True, "name": t.name}
    return {"running": False, "name": None}


def _start_display_thread(target, name):
    """Arrête le thread courant puis démarre ``target(stop_event)``.

    Le ``stop_event`` est créé ICI et passé au thread (jamais lu via la
    globale) : un redémarrage ne peut pas « désarmer » un thread plus
    récent. Retourne l'Event du nouveau thread.
    """
    global _active_thread, _stop_event
    kraken_stop_display()
    stop_event = threading.Event()
    thread = threading.Thread(target=target, args=(stop_event,),
                              name=name, daemon=True)
    with _display_lock:
        _stop_event = stop_event
        _active_thread = thread
        thread.start()
    return stop_event


def kraken_display_status() -> dict:
    """État du thread d'affichage + réglages d'affichage mémorisés.

    Utilisé par GET /api/kraken/display/status : liquidctl ne remonte
    rien, donc mode/theme/options/interval viennent de l'état mémoire.
    """
    status = _display_thread_status()
    with _state_lock:
        status.update(dict(_settings["display"]))
    return status


def kraken_gallery_start(interval: float = DEFAULT_INTERVAL) -> dict:
    """Démarre le diaporama : affiche chaque image à tour de rôle."""
    interval = normalize_interval(interval)

    gallery = kraken_gallery_list()
    files = [f for f in gallery.get("files", [])]
    if not files:
        return {"ok": False, "error": "La gallery est vide — ajoutez des images d'abord"}

    def _gallery_loop(stop_event):
        idx = 0
        while not stop_event.is_set():
            cycle_start = time.monotonic()
            f = files[idx % len(files)]
            path = str(_gallery_dir() / f["name"])
            result = kraken_set_lcd_image(path, animated=f.get("is_gif", False))
            if not result["ok"]:
                stop_event.wait(5)  # Erreur : retente dans 5s
                continue
            idx += 1
            # Date-butoir : le push liquidctl est décompté de la cadence.
            elapsed = time.monotonic() - cycle_start
            stop_event.wait(next_wait_seconds(interval, elapsed))

    _start_display_thread(_gallery_loop, "kraken-gallery")
    kraken_update_settings(display={"mode": "gallery", "interval": interval})
    return {"ok": True, "message": f"Diaporama démarré ({len(files)} fichier(s), {_interval_label(interval)})"}


def _render_monitor_frame(stats, path, palette, layout, options, now=None) -> bool:
    """Appelle le moteur de rendu (palette × disposition).

    Un test peut remplacer ``render_monitoring_image`` par un faux de
    l'ANCIENNE signature (``theme_name`` positionnel) : on adapte l'appel
    selon les paramètres réellement exposés.
    """
    from .monitor import render_monitoring_image
    try:
        params = inspect.signature(render_monitoring_image).parameters
    except (TypeError, ValueError):
        params = {}
    if "layout" in params and "palette" in params:
        return render_monitoring_image(
            stats, path, options=options, now=now, palette=palette, layout=layout
        )
    return render_monitoring_image(stats, path, palette, options)


def kraken_monitor_start(interval: float = DEFAULT_INTERVAL, theme: str = None,
                         options: list = None, palette: str = None,
                         layout: str = None) -> dict:
    """Démarre le monitoring : génère une image de stats et l'envoie.

    Nécessite Pillow + psutil (module ballistix.monitor). ``interval``
    accepte la sentinelle ``"asap"`` (aucune attente entre deux images).
    Le rendu combine ``palette`` × ``layout`` × ``options`` ; l'ancien
    paramètre ``theme`` reste accepté (→ palette, layout inchangé).
    """
    try:
        from .monitor import collect_system_stats, monitor_available
    except ImportError:
        return {"ok": False, "error": "Module de monitoring indisponible (Pillow/psutil manquants)"}

    if not monitor_available():
        return {"ok": False, "error": "Pillow et/ou psutil ne sont pas installés — le mode monitoring est indisponible"}

    with _state_lock:
        current = dict(_settings["display"])
    new_palette = palette or theme or current.get("palette") or "data_center"
    new_layout = layout or current.get("layout") or "duo"
    interval = normalize_interval(interval)
    options = _normalize_options(options)
    screen_path = str(_store_dir() / "monitor.png")

    def _monitor_loop(stop_event):
        while not stop_event.is_set():
            cycle_start = time.monotonic()
            stats = collect_system_stats()
            # Ajoute la température liquide du Kraken si dispo
            try:
                s = kraken_status()
                if s["ok"] and s["data"].get("liquid_temperature") is not None:
                    stats["liquid_temp"] = s["data"]["liquid_temperature"]
            except Exception:
                pass
            if _render_monitor_frame(stats, screen_path, new_palette,
                                     new_layout, options):
                kraken_set_lcd_image(screen_path, animated=False)
            # Date-butoir : le rendu + le push liquidctl sont décomptés.
            elapsed = time.monotonic() - cycle_start
            stop_event.wait(next_wait_seconds(interval, elapsed))

    _start_display_thread(_monitor_loop, "kraken-monitor")
    kraken_update_settings(display={
        "mode": "monitor",
        "theme": new_palette,
        "palette": new_palette,
        "layout": new_layout,
        "options": options,
        "interval": interval,
    })
    return {"ok": True,
            "message": f"Monitoring démarré ({new_palette}/{new_layout}, {_interval_label(interval)})"}


def _interval_label(interval) -> str:
    """Libellé humain d'une cadence (jamais de FPS — contrainte liquidctl)."""
    if interval == ASAP_INTERVAL:
        return "le plus souvent possible"
    return f"une image toutes les {float(interval):.0f}s"


def kraken_display_reconfigure(interval=None, theme=None, options=None,
                               palette=None, layout=None) -> dict:
    """Applique en TEMPS RÉEL des réglages d'affichage (sans changer de mode).

    l'état mémoire est toujours mis à jour (palette/disposition/capteurs/
    intervalle) : c'est lui que POST /api/save fige dans la référence. Si un
    thread d'affichage tourne, il est RELANCÉ avec les nouveaux paramètres
    (le dé-bounce ~400 ms est fait côté front pour éviter la rafale).

    Returns:
        L'état d'affichage résultant + ``restarted`` (thread relancé ?).
    """
    with _state_lock:
        mode = _settings["display"].get("mode")
        current = dict(_settings["display"])

    new_interval = (normalize_interval(interval)
                    if interval is not None
                    else current.get("interval", DEFAULT_INTERVAL))
    new_palette = palette or theme or current.get("palette") or "data_center"
    new_layout = layout or current.get("layout") or "duo"
    new_options = (_normalize_options(options)
                   if options is not None
                   else list(current.get("options") or DEFAULT_OPTIONS))

    kraken_update_settings(display={
        "theme": new_palette,
        "palette": new_palette,
        "layout": new_layout,
        "options": new_options,
        "interval": new_interval,
    })

    running = _display_thread_status().get("running")
    restarted = False
    if running and mode == "monitor":
        res = kraken_monitor_start(new_interval, palette=new_palette,
                                   layout=new_layout, options=new_options)
        restarted = bool(res.get("ok"))
    elif running and mode == "gallery":
        res = kraken_gallery_start(new_interval)
        restarted = bool(res.get("ok"))

    status = kraken_display_status()
    status["ok"] = True
    status["restarted"] = restarted
    return status


def kraken_pending_changes() -> dict:
    """Fichiers modifiés pendant la session et encore annulables (Cancel).

    Miroir de la mécanique backups/trash/session.json : ce que Cancel
    défera. Utilisé par GET /api/kraken/pending pour alimenter le
    compteur « modifications non enregistrées » côté front.
    """
    def _names(directory: Path) -> list:
        try:
            return sorted(f.name for f in directory.iterdir() if f.is_file())
        except OSError:
            return []

    screens = _names(_backup_dir())
    gallery = _gallery_dir()
    gallery_added = [
        n for n in _load_session().get("gallery_added", [])
        if Path(n).name and (gallery / Path(n).name).is_file()
    ]
    gallery_added = sorted(set(gallery_added))
    gallery_deleted = _names(_trash_dir())
    return {
        "ok": True,
        "screens": screens,
        "gallery_added": gallery_added,
        "gallery_deleted": gallery_deleted,
        "count": len(screens) + len(gallery_added) + len(gallery_deleted),
        "error": None,
    }


def kraken_monitor_preview(theme: str = None, options: list = None,
                           palette: str = None, layout: str = None) -> dict:
    """Génère une image de monitoring de test (pour l'aperçu web).

    Combine ``palette`` × ``layout`` × ``options`` (le rendu de la
    combinaison courante). L'ancien paramètre ``theme`` reste accepté.

    Retourne {"ok": bool, "path": str|None, "error": str|None}.
    """
    try:
        from .monitor import collect_system_stats, monitor_available
    except ImportError:
        return {"ok": False, "path": None, "error": "Module de monitoring indisponible"}
    if not monitor_available():
        return {"ok": False, "path": None, "error": "Pillow/psutil manquants"}

    try:
        with _state_lock:
            current = dict(_settings["display"])
        new_palette = palette or theme or current.get("palette") or "data_center"
        new_layout = layout or current.get("layout") or "duo"
        stats = collect_system_stats()
        s = kraken_status()
        if s["ok"] and s["data"].get("liquid_temperature") is not None:
            stats["liquid_temp"] = s["data"]["liquid_temperature"]
        path = str(_store_dir() / "monitor_preview.png")
        ok = _render_monitor_frame(stats, path, new_palette, new_layout, options)
        return {"ok": ok, "path": path if ok else None,
                "error": None if ok else "Rendu impossible"}
    except Exception as e:
        return {"ok": False, "path": None, "error": str(e)}

#!/usr/bin/env python3
"""
ballistix/monitor.py — Monitoring système pour l'écran du Kraken.

Collecte les stats système (CPU, GPU AMD, RAM, VRAM, disques) et
génère une image 640×640 avec Pillow pour l'écran LCD du Kraken Z53.

Modèle de rendu : **palette × disposition × capteurs**.

    - **Palette** (``PALETTES``, 5 entrées) : uniquement des couleurs ;
    - **Disposition** (``LAYOUTS``, 3 entrées) : uniquement de la géométrie,
      rendue par une fonction dédiée (``_render_classic``, ``_render_duo``,
      ``_render_rings``) ;
    - **Capteurs** (``options``, 6 entrées) : cases à cocher CPU/GPU/RAM/
      VRAM/disques/liquide.

Un seul point d'entrée : :func:`render_monitoring_image`.

Rétrocompatibilité : l'ancien paramètre ``theme_name`` (3ᵉ positionnel) est
toujours accepté et interprété comme ``palette=theme_name, layout="classic"``
— les appelants historiques ne changent donc pas de rendu.

Dépendances optionnelles :
    - Pillow (rendu d'image) — requise pour le mode monitoring
    - psutil (stats système) — requise pour le mode monitoring

Si ces libs ne sont pas installées, les fonctions retournent
proprement un flag `ok: False` sans crasher le serveur.
"""

import math
import os
import re
import time
from datetime import datetime
from pathlib import Path

# ── Dépendances optionnelles ────────────────────────────────────────

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None
    PIL_AVAILABLE = False

# ── Constantes ──────────────────────────────────────────────────────

SCREEN_SIZE = (640, 640)          # Écran du Kraken Z53
CENTER = (320, 320)
SAFE_RADIUS = 300                # Zone où le texte est visible

# Plancher de lisibilité (px). Retour terrain sur la dalle 640×640 du Kraken
# Z53 : f16 est illisible, f26 est nettement lisible ; le seuil retenu est
# f22 → toute information sous ce plancher est SUPPRIMÉE ou fusionnée
# (jamais réduite à une taille molle).
MIN_READABLE_SIZE = 22
# Tailles candidates d'une valeur de cellule secondaire, du plus grand au plus
# petit : on prend la première qui ne chevauche pas le libellé (jamais < plancher).
CELL_VALUE_SIZES = (40, 36, 32, 28, 24, MIN_READABLE_SIZE)

# ── Palettes ────────────────────────────────────────────────────────
#
# Une palette ne contient QUE des couleurs (6 valeurs RGB) : plus aucune
# géométrie. Les 3 palettes historiques (data_center, overclock,
# fluid_flow) reprennent EXACTEMENT les valeurs de l'ancien ``THEMES`` — un
# rendu existant est donc inchangé. Graphite (mono sobre) et Amber (fort
# contraste) sont les deux nouvelles palettes validées.

class Palette:
    """Jeu de 6 couleurs d'écran + métadonnées de galerie."""

    def __init__(self, bg, text, accent, gauge_bg, gauge_start, gauge_end,
                 label=None, subtitle=None, is_new=False):
        self.bg = bg
        self.text = text
        self.accent = accent
        self.gauge_bg = gauge_bg
        self.gauge_start = gauge_start
        self.gauge_end = gauge_end
        # Métadonnées exposées par l'API (galerie web) : libellé + sous-titre.
        self.label = label
        self.subtitle = subtitle
        self.is_new = is_new

    def colors(self) -> dict:
        """Palette sous forme hexadécimale (pour l'API / le nuancier web)."""
        return {
            "bg": _hex(self.bg),
            "text": _hex(self.text),
            "accent": _hex(self.accent),
            "gauge_bg": _hex(self.gauge_bg),
            "gauge_start": _hex(self.gauge_start),
            "gauge_end": _hex(self.gauge_end),
        }


# Nom historique conservé (le module exposait ``Theme``).
Theme = Palette

PALETTES = {
    "data_center": Palette(
        bg=(5, 15, 25),
        text=(200, 230, 255),
        accent=(0, 160, 255),
        gauge_bg=(10, 30, 50),
        gauge_start=(0, 60, 120),
        gauge_end=(0, 180, 255),
        label="Data Center",
        subtitle="Bleu Technique",
    ),
    "overclock": Palette(
        bg=(15, 5, 5),
        text=(240, 240, 240),
        accent=(255, 0, 0),
        gauge_bg=(45, 10, 10),
        gauge_start=(150, 0, 0),
        gauge_end=(255, 40, 40),
        label="Overclock",
        subtitle="Rouge Agressif",
    ),
    "fluid_flow": Palette(
        bg=(25, 35, 45),
        text=(230, 245, 255),
        accent=(120, 210, 255),
        gauge_bg=(50, 70, 90),
        gauge_start=(160, 210, 255),
        gauge_end=(200, 230, 255),
        label="Fluid Flow",
        subtitle="Bleu Pastel",
    ),
    "graphite": Palette(
        bg=(16, 17, 19),
        text=(232, 234, 238),
        accent=(170, 177, 188),
        gauge_bg=(38, 42, 48),
        gauge_start=(96, 104, 116),
        gauge_end=(196, 203, 214),
        label="Graphite",
        subtitle="Mono sobre · nouvelle",
        is_new=True,
    ),
    "amber": Palette(
        bg=(10, 8, 4),
        text=(255, 241, 214),
        accent=(255, 176, 32),
        gauge_bg=(48, 32, 8),
        gauge_start=(176, 96, 0),
        gauge_end=(255, 200, 64),
        label="Amber",
        subtitle="Fort contraste · nouvelle",
        is_new=True,
    ),
}

# Palette par défaut (celle du mode monitoring).
DEFAULT_PALETTE = "data_center"

# Alias de rétrocompatibilité (ancien vocabulaire « thème »).
THEMES = PALETTES
DEFAULT_THEME = DEFAULT_PALETTE


# ── Dispositions ────────────────────────────────────────────────────
#
# Une disposition ne contient QUE de la géométrie (positions + tailles de
# police) : elle est rendue par la fonction ``_render_<key>``.

class Layout:
    def __init__(self, label, subtitle=None, is_new=False):
        self.label = label
        self.subtitle = subtitle
        self.is_new = is_new


LAYOUTS = {
    "classic": Layout("Classique", "Liste verticale — actuelle"),
    "duo": Layout("Duo", "Deux colonnes — valeurs XL", is_new=True),
    "rings": Layout("Anneaux", "Jauges circulaires", is_new=True),
}

# Disposition par défaut : le grand format deux colonnes (décision utilisateur).
DEFAULT_LAYOUT = "duo"

# Ordre d'affichage des métriques (lignes classiques / cellules / anneaux).
METRIC_ORDER = ["cpu", "gpu", "ram", "vram", "liquid"]
# Priorité des héros du grand format : les températures d'abord, le liquide
# promu automatiquement si CPU ou GPU est désactivé.
HERO_ORDER = ["cpu", "gpu", "liquid", "ram", "vram"]
# Nombre maximal d'anneaux et emplacements adaptatifs (composition symétrique).
MAX_RINGS = 4
RING_SLOTS = {
    1: [(320, 320)],
    2: [(183, 320), (457, 320)],
    3: [(183, 272), (457, 272), (320, 438)],
    4: [(183, 268), (457, 268), (183, 432), (457, 432)],
}


def hero_metrics(options) -> list:
    """Métriques des 2 héros du grand format (CPU > GPU > LIQUID > RAM > VRAM)."""
    return [m for m in HERO_ORDER if m in options][:2]


def secondary_metrics(options) -> list:
    """Métriques de la grille secondaire du grand format (ordre classique)."""
    heroes = set(hero_metrics(options))
    return [m for m in METRIC_ORDER if m in options and m not in heroes]


def ring_metrics(options) -> list:
    """4 premières métriques actives (ordre CPU, GPU, RAM, VRAM, LIQUID)."""
    return [m for m in METRIC_ORDER if m in options][:MAX_RINGS]


def ring_overflow_metrics(options) -> list:
    """Métriques actives au-delà des 4 anneaux (pied de cercle)."""
    return [m for m in METRIC_ORDER if m in options][MAX_RINGS:]


def ring_slot_set(count: int) -> list:
    """Emplacements des anneaux (1 centré, 2 en ligne, 3 en triangle, 4 en grille)."""
    return list(RING_SLOTS.get(count, RING_SLOTS[MAX_RINGS]))


def list_themes() -> list:
    """Liste ordonnée des « thèmes » (alias historique des palettes).

    Chaque entrée : ``{key, label, subtitle, default}``. Conservé pour la
    rétrocompatibilité d'API ; les nouveaux appelants utiliseront
    :func:`list_palettes` (couleurs incluses) et :func:`list_layouts`.
    """
    def entry(key, pal):
        return {
            "key": key,
            "label": pal.label or key.replace("_", " ").title(),
            "subtitle": pal.subtitle or "",
            "default": key == DEFAULT_PALETTE,
        }

    ordered = []
    if DEFAULT_PALETTE in PALETTES:
        ordered.append((DEFAULT_PALETTE, PALETTES[DEFAULT_PALETTE]))
    ordered.extend((k, p) for k, p in PALETTES.items() if k != DEFAULT_PALETTE)
    return [entry(k, p) for k, p in ordered]


def list_palettes() -> list:
    """Catalogue ordonné des palettes (défaut en tête, couleurs incluses)."""
    entries = []
    for item in list_themes():
        pal = PALETTES[item["key"]]
        entries.append({
            "key": item["key"],
            "label": item["label"],
            "subtitle": item["subtitle"],
            "default": item["default"],
            "is_new": bool(getattr(pal, "is_new", False)),
            "colors": pal.colors(),
        })
    return entries


def list_layouts() -> list:
    """Catalogue ordonné des dispositions (défaut en tête)."""
    def entry(key, layout):
        return {
            "key": key,
            "label": layout.label or key.replace("_", " ").title(),
            "subtitle": layout.subtitle or "",
            "default": key == DEFAULT_LAYOUT,
        }

    ordered = []
    if DEFAULT_LAYOUT in LAYOUTS:
        ordered.append((DEFAULT_LAYOUT, LAYOUTS[DEFAULT_LAYOUT]))
    ordered.extend((k, l) for k, l in LAYOUTS.items() if k != DEFAULT_LAYOUT)
    return [entry(k, l) for k, l in ordered]


# ── Collecte des stats ──────────────────────────────────────────────

def _read_sysfs_int(path) -> int:
    """Lit un entier depuis sysfs, retourne -1 si illisible."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return -1


def _find_amdgpu_hwmon() -> Path:
    """Cherche le dossier hwmon du GPU amdgpu dans /sys/class/drm."""
    drm = Path("/sys/class/drm")
    if not drm.is_dir():
        return None
    for card in drm.glob("card*"):
        hwmon = card / "device" / "hwmon"
        if not hwmon.is_dir():
            continue
        for h in hwmon.iterdir():
            name_file = h / "name"
            try:
                if name_file.read_text().strip() == "amdgpu":
                    return h
            except Exception:
                continue
    return None


def _gpu_temp_amd() -> float:
    """Température GPU via hwmon amdgpu (temp1_input, milli-°C)."""
    hwmon = _find_amdgpu_hwmon()
    if not hwmon:
        return None
    raw = _read_sysfs_int(hwmon / "temp1_input")
    return raw / 1000.0 if raw > 0 else None


def _gpu_vram_amd() -> dict:
    """Mémoire VRAM via mem_info_vram_used/total (amdgpu)."""
    drm = Path("/sys/class/drm")
    for card in drm.glob("card*"):
        dev = card / "device"
        used_file = dev / "mem_info_vram_used"
        total_file = dev / "mem_info_vram_total"
        if used_file.is_file() and total_file.is_file():
            used = _read_sysfs_int(used_file)
            total = _read_sysfs_int(total_file)
            if used >= 0 and total > 0:
                return {"used": used, "total": total, "percent": used * 100.0 / total}
    return None


def _cpu_temp() -> float:
    """Température CPU via psutil sensors (coretemp/k10temp/zenpower)."""
    if not PSUTIL_AVAILABLE:
        return None
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    # Cherche dans les zones classiques CPU
    for zone in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
        if zone in temps:
            entries = temps[zone]
            if entries:
                # k10temp : Tctl/Tdie ; coretemp : Package id 0
                for e in entries:
                    if e.label and ("package" in e.label.lower() or "tctl" in e.label.lower() or "tdie" in e.label.lower()):
                        return e.current
                return entries[0].current
    # Fallback : toutes les températures, prend la max
    best = None
    for zone, entries in temps.items():
        for e in entries:
            if e.current is not None:
                best = max(best, e.current) if best is not None else e.current
    return best


def _disks() -> list:
    """Stats disques : mount, used, total, percent (filtre pseudo-FS)."""
    if not PSUTIL_AVAILABLE:
        return []
    skip_prefixes = ("/proc", "/sys", "/dev", "/run", "/tmp", "/snap",
                     "/var/lib/docker", "/boot/efi", "/boot", "/etc")
    results = []
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        return []
    for p in parts:
        if any(p.mountpoint.startswith(sp) for sp in skip_prefixes):
            continue
        if p.fstype in ("tmpfs", "squashfs", "ramfs", "overlay"):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        results.append({
            "mount": p.mountpoint,
            "used": usage.used,
            "total": usage.total,
            "percent": usage.percent,
            "fstype": p.fstype,
        })
        if len(results) >= 4:
            break
    return results


def collect_system_stats() -> dict:
    """Collecte toutes les stats système pour l'écran.

    Retourne un dict avec : cpu_temp, gpu_temp, ram, vram, disks, cpu_percent.
    Chaque valeur manquante est None (ou liste vide).
    """
    stats = {
        "cpu_temp": None,
        "gpu_temp": None,
        "cpu_percent": None,
        "ram": {"used": None, "total": None, "percent": None},
        "vram": {"used": None, "total": None, "percent": None},
        "disks": [],
    }

    if PSUTIL_AVAILABLE:
        try:
            stats["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            stats["ram"] = {
                "used": vm.used,
                "total": vm.total,
                "percent": vm.percent,
            }
        except Exception:
            pass

    stats["cpu_temp"] = _cpu_temp()
    stats["gpu_temp"] = _gpu_temp_amd()
    stats["vram"] = _gpu_vram_amd() or stats["vram"]
    stats["disks"] = _disks()
    return stats


# ── Polices ─────────────────────────────────────────────────────────

_FONT_CACHE = {}


def _load_font(size: int):
    """Charge une police TTF si dispo, sinon la police par défaut.

    Le résultat est mis en cache par taille : un rendu utilise plusieurs
    tailles et le monitoring en génère plusieurs fois par minute.
    """
    if not ImageFont:
        return None
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    font = None
    for path in candidates:
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ── Helpers couleur / formatage ─────────────────────────────────────

def _hex(rgb) -> str:
    """Couleur RGB → ``#rrggbb``."""
    return "#%02x%02x%02x" % tuple(int(c) for c in rgb)


def _interpolate_color(c1, c2, factor):
    """Interpolation linéaire entre deux couleurs RGB (bornée 0..1)."""
    factor = min(1.0, max(0.0, factor))
    return tuple(int(c1[i] + (c2[i] - c1[i]) * factor) for i in range(3))


def _get_row_width(y):
    """Largeur disponible à l'ordonnée y pour rester dans le cercle sûr."""
    dy = abs(y - CENTER[1])
    if dy >= SAFE_RADIUS:
        return 0
    return 2 * (SAFE_RADIUS**2 - dy**2) ** 0.5


def _fmt_bytes(n) -> str:
    """Formate des octets en chaîne lisible."""
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return "—"


def _fmt_pair(d) -> str:
    """« 9.3 GB / 16.0 GB » à partir d'un dict {used, total}."""
    d = d or {}
    return f"{_fmt_bytes(d.get('used'))} / {_fmt_bytes(d.get('total'))}"


def _fmt_temp(v) -> str:
    return "—" if v is None else f"{v:.0f}°C"


def _fmt_temp_dec(v) -> str:
    return "—" if v is None else f"{v:.1f}°C"


def _time_text(now=None) -> str:
    """Heure ``HH:MM:SS`` à afficher (figée si ``now`` fourni)."""
    if now is None:
        return time.strftime("%H:%M:%S")
    if hasattr(now, "strftime"):
        return now.strftime("%H:%M:%S")
    return str(now)


def _pct_of(stats: dict, metric: str):
    """Pourcentage associé à une métrique, None si non applicable."""
    if metric == "cpu":
        return stats.get("cpu_percent")
    if metric in ("ram", "vram"):
        return (stats.get(metric) or {}).get("percent")
    return None


def _gauge_percent(stats: dict, metric: str):
    """Remplissage de jauge/anneau selon la métrique.

    - CPU / GPU  : température mappée 20–90 °C (la charge est affichée à
      part pour le CPU) ;
    - LIQUID     : température mappée 20–50 °C (plage utile d'un AIO) ;
    - RAM / VRAM : pourcentage utilisé direct.
    """
    if metric in ("cpu", "gpu"):
        t = stats.get(f"{metric}_temp")
        return None if t is None else (t - 20.0) / 70.0 * 100.0
    if metric == "liquid":
        t = stats.get("liquid_temp")
        return None if t is None else (t - 20.0) / 30.0 * 100.0
    return _pct_of(stats, metric)


def _metric_label(metric: str) -> str:
    return {"cpu": "CPU", "gpu": "GPU", "ram": "RAM", "vram": "VRAM",
            "liquid": "LIQUID"}.get(metric, metric.upper())


def _hero_text(stats: dict, metric: str):
    """(valeur, unité) d'une métrique pour une grande valeur (héros/anneau)."""
    if metric in ("cpu", "gpu"):
        return _fmt_temp(stats.get(f"{metric}_temp")).removesuffix("°C"), "°C"
    if metric == "liquid":
        return _fmt_temp_dec(stats.get("liquid_temp")).removesuffix("°C"), "°C"
    p = _pct_of(stats, metric)
    return ("—" if p is None else f"{p:.0f}"), "%"


def _cell_value_text(stats: dict, metric: str) -> str:
    """Valeur compacte d'une cellule secondaire (jamais de sous-valeur).

    Températures formatées comme les héros ; RAM/VRAM en pourcentage seul
    (les capacités « 17.8 GB / 31.2 GB » étaient illisibles sur la dalle).
    """
    if metric in ("cpu", "gpu"):
        return _fmt_temp(stats.get(f"{metric}_temp"))
    if metric == "liquid":
        return _fmt_temp_dec(stats.get("liquid_temp"))
    p = _pct_of(stats, metric)
    return "—" if p is None else f"{p:.0f} %"


def _fit_cell_font(draw, label: str, value: str, width: float, label_font,
                   sizes=CELL_VALUE_SIZES, gap: int = 10):
    """Plus grande taille de valeur qui laisse libellé + valeur tenir en ``width``.

    Évite le chevauchement (ex. « LIQUID » + « 43.1°C ») sans jamais descendre
    sous :data:`MIN_READABLE_SIZE`.
    """
    label_w = _text_len(draw, label, label_font)
    for size in sizes:
        font = _load_font(size)
        if label_w + _text_len(draw, value, font) + gap <= width:
            return font
    return _load_font(MIN_READABLE_SIZE)


# ── Helpers de dessin ───────────────────────────────────────────────

def _text_len(draw, s, font):
    return draw.textlength(s, font=font)


def _text_height(font) -> int:
    """Hauteur d'une ligne de texte (ascender + descender de la police)."""
    asc, desc = font.getmetrics()
    return asc + desc


def _draw_center(draw, cx, y, s, font, fill):
    w = _text_len(draw, s, font)
    draw.text((cx - w / 2, y), s, font=font, fill=fill)


def _draw_right(draw, x_right, y, s, font, fill):
    w = _text_len(draw, s, font)
    draw.text((x_right - w, y), s, font=font, fill=fill)


def _draw_combo(draw, cx, y, main, unit, font_main, font_unit, fill, gap=2):
    """Valeur + unité alignées sur la ligne de base (ancrage 'la')."""
    wm = _text_len(draw, main, font_main)
    wu = _text_len(draw, unit, font_unit) if unit else 0
    total = wm + (gap + wu if unit else 0)
    x = cx - total / 2
    if font_unit:
        asc_m = font_main.getmetrics()[0]
        asc_u = font_unit.getmetrics()[0]
    else:
        asc_m = asc_u = 0
    draw.text((x, y), main, font=font_main, fill=fill)
    if unit:
        draw.text((x + wm + gap, y + asc_m - asc_u), unit, font=font_unit, fill=fill)


def _draw_gauge(draw, x, y, width, height, percent, pal: Palette) -> None:
    """Jauge horizontale arrondie (fond toujours dessiné, remplissage si %)."""
    draw.rounded_rectangle(
        [x, y, x + width, y + height], radius=height // 2, fill=pal.gauge_bg
    )
    if percent is None:
        return
    fill_w = width * min(100.0, max(0.0, percent)) / 100.0
    if fill_w > 0:
        color = _interpolate_color(pal.gauge_start, pal.gauge_end, percent / 100.0)
        draw.rounded_rectangle(
            [x, y, x + fill_w, y + height], radius=height // 2, fill=color
        )


def _draw_ring(draw, cx, cy, radius, stroke, percent, pal: Palette) -> None:
    """Anneau de progression (départ 12 h, sens horaire)."""
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    draw.ellipse(bbox, outline=pal.gauge_bg, width=stroke)
    if percent is None:
        return
    p = min(100.0, max(0.0, percent))
    if p <= 0.5:
        return
    draw.arc(
        bbox, -90, -90 + 3.6 * p,
        fill=_interpolate_color(pal.gauge_start, pal.gauge_end, p / 100.0),
        width=stroke,
    )


def _short_mount(mount) -> str:
    """Libellé COURT et lisible d'un point de montage (dalle 640×640).

    Règle retenue : la racine reste « / » ; sinon on ne garde que le DERNIER
    segment du chemin (``/root/.config/ballistix`` → ``ballistix``). Le
    montage complet était illisible et, en disposition ``classic``, il
    chevauchait la valeur alignée à droite. Un segment trop long est tronqué
    avec une ellipse (14 caractères max).
    """
    if not mount:
        return "?"
    text = str(mount)
    if text.strip("/") == "":
        return "/"
    segments = [s for s in text.rstrip("/").split("/") if s]
    short = segments[-1] if segments else text
    return short if len(short) <= 14 else short[:13] + "…"


def _disk_line_render(draw, disk, size, compact):
    """Segments et largeur totale d'une ligne disque (sans rien dessiner)."""
    font = _load_font(size)
    marker = "●"
    mount = _short_mount(disk.get("mount", "?"))
    pct = disk.get("percent") or 0
    rest = f"  {pct:.0f}%" if compact else f"  {_fmt_pair(disk)} ({pct:.0f}%)"
    w1 = _text_len(draw, marker, font)
    w2 = _text_len(draw, mount, font)
    w3 = _text_len(draw, rest, font)
    return marker, mount, rest, font, w1, w2, w3, w1 + 4 + w2 + w3


def _disk_line_width(draw, disk, size=None, compact=False) -> float:
    """Largeur totale d'une ligne disque (pour vérifier qu'elle tient)."""
    if size is None:
        size = MIN_READABLE_SIZE
    return _disk_line_render(draw, disk, size, compact)[-1]


def _draw_disk_line(draw, cx, y, disk, pal: Palette, size=None, compact=False,
                    max_width=None):
    """Ligne disque : marqueur + libellé court en accent, valeurs en texte.

    ``compact=True`` n'affiche que le pourcentage (pied de ``rings``, où la
    place est comptée à l'intérieur du cercle). ``max_width`` (optionnel) est
    la largeur dessinable : si la forme complète la dépasse, on retombe sur la
    forme compacte plutôt que de réduire la police sous le plancher (doctrine
    « supprimer/fusionner, jamais rétrécir ») ; si même la forme compacte ne
    tient pas, la ligne est supprimée. Retourne la largeur dessinée pour que
    l'appelant puisse vérifier qu'elle tient dans la corde disponible.
    """
    if size is None:
        size = MIN_READABLE_SIZE
    marker, mount, rest, font, w1, w2, w3, total = _disk_line_render(
        draw, disk, size, compact)
    if max_width is not None and total > max_width:
        if not compact:
            return _draw_disk_line(draw, cx, y, disk, pal, size=size,
                                   compact=True, max_width=max_width)
        return 0.0
    x = cx - total / 2
    draw.text((x, y), marker, font=font, fill=pal.accent)
    draw.text((x + w1 + 4, y), mount, font=font, fill=pal.accent)
    draw.text((x + w1 + 4 + w2, y), rest, font=font, fill=pal.text)
    return total


# ── Disposition 1 : classique (référence historique, corrigée) ──────

def _render_classic(draw, pal: Palette, stats: dict, options: list, now_text: str):
    """Liste verticale historique — géométrie conservée, corrections ciblées.

    1. Marquage ``●`` (DejaVu) au lieu des emojis 🌡/🎮/💾/💿 qui ne sont pas
       dessinables par DejaVuSans-Bold (carrés vides) ;
    2. largeur de ligne = ``min(chord(y), chord(y+44)) − 4`` : couvre le texte
       ET la jauge, sans plus déborder du cercle sûr (avant : ~6 px de
       dépassement aux coins hauts, jusqu'à 79 pixels hors du cercle) ;
    3. tous les textes sont relevés au plancher f22 ; la bande liquide du bas
       est supprimée si la dernière ligne empiète dessus ou si le texte ne
       tient plus dans la corde.
    """
    f_title = _load_font(32)
    f_label = _load_font(24)
    f_value = _load_font(24)
    f_small = _load_font(MIN_READABLE_SIZE)

    y = 110
    _draw_center(draw, CENTER[0], y, "SYSTEM MONITOR", f_title, pal.accent)
    _draw_center(draw, CENTER[0], y + 35, now_text, f_small, pal.text)
    y += 60

    row_h = 70
    last_bottom = None

    def add_row(label, value_str, percent, marker="●"):
        nonlocal y, last_bottom
        # Correction : la largeur doit couvrir TOUTE la ligne (texte
        # y..y+23 puis jauge y+30..y+44), pas seulement y+20.
        width = min(_get_row_width(y), _get_row_width(y + 44)) - 4
        if width < 100:
            return
        x_start = CENTER[0] - width / 2
        # Repli lisibilité : si libellé + valeur ne tiennent plus côte à côte
        # (montage long + capacité), on fusionne au pourcentage seul (que la
        # jauge matérialise déjà) — jamais de textes superposés.
        label_w = _text_len(draw, label, f_label)
        value_w = _text_len(draw, value_str, f_value)
        if 30 + label_w + value_w + 10 > width:
            value_str = "" if percent is None else f"{percent:.0f}%"
            value_w = _text_len(draw, value_str, f_value)
            if 30 + label_w + value_w + 10 > width:
                value_str = ""
        draw.text((x_start, y), marker, font=f_value, fill=pal.accent)
        if value_str:
            _draw_right(draw, x_start + width, y, value_str, f_value, pal.text)
        draw.text((x_start + 30, y), label, font=f_label, fill=pal.text)
        _draw_gauge(draw, x_start + 30, y + 30, width - 60, 14, percent, pal)
        last_bottom = y + 44
        y += row_h

    if "cpu" in options:
        add_row("CPU", _fmt_temp(stats.get("cpu_temp")),
                stats.get("cpu_percent"))
    if "gpu" in options:
        add_row("GPU", _fmt_temp(stats.get("gpu_temp")), None)
    if "ram" in options:
        add_row("RAM", _fmt_pair(stats.get("ram")), _pct_of(stats, "ram"))
    if "vram" in options:
        add_row("VRAM", _fmt_pair(stats.get("vram")), _pct_of(stats, "vram"))
    if "disks" in options:
        for d in (stats.get("disks") or [])[:2]:
            add_row(_short_mount(d.get("mount", "?")),
                    _fmt_pair(d), d.get("percent"))

    if "liquid" in options:
        liquid = stats.get("liquid_temp")
        y_liq = 500
        # Bande liquide : supprimée si la dernière ligne (ex. 2 disques)
        # empiète dessus, ou si le texte ne tient plus dans la corde à f22 —
        # la doctrine supprime plutôt que de superposer ou de rétrécir.
        if (liquid is not None
                and (last_bottom is None or last_bottom <= y_liq)):
            text = f"Liquid Temperature: {liquid:.1f}°C"
            limit = min(_get_row_width(y_liq),
                        _get_row_width(y_liq + _text_height(f_small))) - 4
            if _text_len(draw, text, f_small) <= limit:
                _draw_center(draw, CENTER[0], y_liq, text, f_small, pal.accent)


# ── Disposition 2 : grand format deux colonnes ──────────────────────

def _render_duo(draw, pal: Palette, stats: dict, options: list, now_text: str):
    """Deux colonnes, valeurs principales XL, secondaires LISIBLES.

    Refonte lisibilité (retour terrain Kraken Z53, dalle 640×640 très dense) :
    tout est ramené au plancher ``MIN_READABLE_SIZE`` (f22). Les éléments qui
    ne passaient pas le plancher ont été **supprimés** plutôt que réduits :

    - la légende « charge 7 % » (f15) et la ligne « échelle 20–50 °C » (f15) ;
    - les sous-valeurs « 17.8 GB / 31.2 GB » (f15) : RAM/VRAM s'affichent
      désormais en **grand pourcentage** seul ;
    - les disques gardent un **libellé court** (``_short_mount``) au lieu du
      montage complet, et abandonnent la capacité au profit du pourcentage
      seul quand la ligne complète ne tient plus dans la corde.

    Héros (2 emplacements) : premières métriques actives dans l'ordre
    CPU > GPU > LIQUID > RAM > VRAM (le liquide est promu si CPU/GPU est
    désactivé). Les secondaires tiennent dans une grille 2×2 (cellule orpheline
    centrée) ; les disques occupent une bande basse (≤ 1 ligne).
    """
    f_title = _load_font(28)
    f_time = _load_font(MIN_READABLE_SIZE)
    f_hero_label = _load_font(24)
    f_hero_main = _load_font(76)
    f_hero_unit = _load_font(28)
    f_hero_pct = _load_font(32)
    f_cell_label = _load_font(MIN_READABLE_SIZE)

    _draw_center(draw, CENTER[0], 94, "SYSTEM MONITOR", f_title, pal.accent)
    _draw_center(draw, CENTER[0], 128, now_text, f_time, pal.text)

    heroes = hero_metrics(options)
    rest = secondary_metrics(options)
    disks = (stats.get("disks") or [])[:1] if "disks" in options else []

    hero_x = {0: 180, 1: 460}
    for i, metric in enumerate(heroes):
        cx = CENTER[0] if len(heroes) == 1 else hero_x[i]
        _draw_center(draw, cx, 166, _metric_label(metric),
                     f_hero_label, pal.accent)

        main, unit = _hero_text(stats, metric)
        unit_font = f_hero_unit if unit == "°C" else f_hero_pct
        _draw_combo(draw, cx, 196, main, unit, f_hero_main, unit_font, pal.text)

        gauge_p = _gauge_percent(stats, metric)
        if gauge_p is not None:
            _draw_gauge(draw, cx - 105, 302, 210, 16, gauge_p, pal)

    # Séparateur
    draw.line([(150, 338), (490, 338)], fill=pal.gauge_bg, width=2)

    # Grille secondaire : grand pourcentage, aucune sous-valeur.
    cols = [(76, 232), (332, 232)]
    rows = [360, 434]
    asc_label = f_cell_label.getmetrics()[0]
    for idx, metric in enumerate(rest[:4]):
        col, row = idx % 2, idx // 2
        if idx == len(rest) - 1 and len(rest) % 2 == 1:
            col = None  # cellule orpheline → centrée
        x, w = cols[col if col is not None else 0]
        if col is None:
            x = CENTER[0] - w / 2
        yy = rows[row]
        label = _metric_label(metric)
        value = _cell_value_text(stats, metric)
        draw.text((x, yy), label, font=f_cell_label, fill=pal.text)
        vfont = _fit_cell_font(draw, label, value, w, f_cell_label)
        _draw_right(draw, x + w, yy + asc_label - vfont.getmetrics()[0],
                    value, vfont, pal.text)
        _draw_gauge(draw, x, yy + 46, w, 12, _gauge_percent(stats, metric), pal)

    # Bande disques (libellé court, f22) : corde bornée au bas du texte ;
    # si la ligne complète « 562.6 GB / 931.2 GB (61%) » ne tient plus, la
    # forme compacte « ballistix  61% » est dessinée — jamais de police
    # sous le plancher.
    f_disk = _load_font(MIN_READABLE_SIZE)
    disk_h = _text_height(f_disk)
    dy = 504
    for d in disks:
        limit = min(_get_row_width(dy),
                    _get_row_width(dy + disk_h)) - 4
        _draw_disk_line(draw, CENTER[0], dy, d, pal, size=MIN_READABLE_SIZE,
                        max_width=limit)
        dy += 24


# ── Disposition 3 : anneaux de progression ──────────────────────────

def _render_rings(draw, pal: Palette, stats: dict, options: list, now_text: str):
    """Grille adaptative d'anneaux (r=80, trait 14), valeur au centre.

    L'anneau représente la fraction de plage utile : température mappée
    20–90 °C (CPU/GPU), 20–50 °C (liquide), pourcentage direct (RAM/VRAM).
    Les 4 premières métriques actives (ordre CPU, GPU, RAM, VRAM, LIQUID)
    occupent les emplacements ; la 5ᵉ (le liquide en pratique) et les
    disques s'affichent en pied de cercle.

    Refonte lisibilité : le libellé passe de f17 à ``MIN_READABLE_SIZE`` et
    les sous-valeurs f14 (« charge 42% », « 9.3 GB/16.0 GB ») sont supprimées
    — sous le plancher, elles n'étaient que du bruit. Les disques du pied
    n'affichent plus que libellé court + pourcentage (``compact``).
    """
    f_title = _load_font(26)
    f_time = _load_font(MIN_READABLE_SIZE)
    f_label = _load_font(MIN_READABLE_SIZE)
    f_main = _load_font(36)
    f_unit = _load_font(MIN_READABLE_SIZE)

    _draw_center(draw, CENTER[0], 96, "SYSTEM MONITOR", f_title, pal.accent)
    _draw_center(draw, CENTER[0], 126, now_text, f_time, pal.text)

    # Emplacements adaptatifs : 1 centré, 2 en ligne, 3 en triangle, 4 en grille.
    rings = ring_metrics(options)
    overflow = ring_overflow_metrics(options)
    slots = ring_slot_set(len(rings))

    for idx, metric in enumerate(rings):
        cx, cy = slots[idx]
        _draw_ring(draw, cx, cy, 80, 14, _gauge_percent(stats, metric), pal)
        _draw_center(draw, cx, cy - 48, _metric_label(metric), f_label, pal.accent)

        main, unit = _hero_text(stats, metric)
        _draw_combo(draw, cx, cy - 14, main, unit, f_main, f_unit, pal.text)

    # Pied de cercle : liquide (si pas d'anneau) puis disques en texte.
    footer_y = 524
    f_footer = _load_font(MIN_READABLE_SIZE)
    footer_h = _text_height(f_footer)
    if "liquid" in overflow and stats.get("liquid_temp") is not None:
        text = f"Liquid Temperature: {stats['liquid_temp']:.1f}°C"
        limit = min(_get_row_width(footer_y),
                    _get_row_width(footer_y + footer_h)) - 4
        if _text_len(draw, text, f_footer) <= limit:
            _draw_center(draw, CENTER[0], footer_y, text, f_footer, pal.accent)
            footer_y = 550
    if "disks" in options:
        for d in (stats.get("disks") or [])[:2]:
            # Compacité imposée par la place restante dans le cercle : on
            # s'arrête dès que la ligne suivante n'y tiendrait plus (largeur
            # compacte MESURÉE, jamais de police réduite sous le plancher).
            limit = min(_get_row_width(footer_y),
                        _get_row_width(footer_y + footer_h)) - 4
            if _disk_line_width(draw, d, MIN_READABLE_SIZE, compact=True) > limit:
                break
            _draw_disk_line(draw, CENTER[0], footer_y, d, pal,
                            size=MIN_READABLE_SIZE, compact=True)
            footer_y += 24


# Dispatch disposition → fonction de rendu.
RENDERERS = {
    "classic": _render_classic,
    "duo": _render_duo,
    "rings": _render_rings,
}


# ── Point d'entrée ──────────────────────────────────────────────────

DEFAULT_OPTIONS = ["cpu", "gpu", "ram", "vram", "disks", "liquid"]


def render_monitoring_image(stats: dict, output_path: str,
                            theme_name: str = None, options: list = None,
                            now=None, palette: str = None,
                            layout: str = None) -> bool:
    """Génère l'image 640×640 de monitoring (palette × disposition).

    Args:
        stats: Dictionnaire des stats système.
        output_path: Chemin de sauvegarde.
        theme_name: **Rétrocompatibilité** — ancien nom du thème, interprété
            comme ``palette=theme_name`` + ``layout="classic"``.
        options: Liste des capteurs à afficher (ex: ``['cpu', 'gpu']``).
        now: Heure à afficher (datetime/chaîne). None = heure courante ;
            une valeur figée rend le PNG reproductible (vignettes en cache).
        palette: Clé de palette (``PALETTES``). Prioritaire sur ``theme_name``.
        layout: Clé de disposition (``LAYOUTS``). Par défaut ``DEFAULT_LAYOUT``
            (« duo ») pour un nouveau rendu, sauf en mode rétrocompatible
            ``theme_name`` où l'on conserve « classic ».

    Un appel positionnel de style ``(stats, path, palette, layout, options,
    now)`` est également reconnu (le 3ᵉ argument est une palette connue et le
    4ᵉ une disposition connue).
    """
    if not PIL_AVAILABLE:
        return False

    # Appel positionnel « nouveau style » : (palette, layout[, options[, now]]).
    if isinstance(options, str) and options in LAYOUTS and theme_name in PALETTES:
        palette, layout, options, now = theme_name, options, now, palette
        theme_name = None

    if palette is None:
        palette = theme_name if theme_name else DEFAULT_PALETTE
    if layout is None:
        layout = "classic" if theme_name is not None else DEFAULT_LAYOUT

    pal = PALETTES.get(palette) or PALETTES[DEFAULT_PALETTE]
    if layout not in RENDERERS:
        layout = DEFAULT_LAYOUT
    if options is None:
        options = list(DEFAULT_OPTIONS)

    img = Image.new("RGB", SCREEN_SIZE, pal.bg)
    draw = ImageDraw.Draw(img)
    RENDERERS[layout](draw, pal, stats, list(options), _time_text(now))

    try:
        img.save(output_path, "PNG")
        return True
    except Exception:
        return False


_OUTSIDE_MASK_CACHE = {}


def _outside_safe_mask(width: int, height: int, tol: int):
    """Masque binaire « hors du cercle sûr » (construit une fois par taille)."""
    key = (width, height, tol, SAFE_RADIUS, CENTER)
    mask = _OUTSIDE_MASK_CACHE.get(key)
    if mask is None:
        mask = Image.new("L", (width, height), 0)
        px = mask.load()
        limit = SAFE_RADIUS ** 2 + tol
        for yy in range(height):
            dy2 = (yy - CENTER[1]) ** 2
            for xx in range(width):
                if (xx - CENTER[0]) ** 2 + dy2 > limit:
                    px[xx, yy] = 255
        _OUTSIDE_MASK_CACHE[key] = mask
    return mask


def count_pixels_outside_safe(img, palette: Palette, tol: int = 16) -> int:
    """Compte les pixels non-fond au-delà du cercle sûr (rayon 300).

    ``tol`` absorbe l'antialiasing des bords (idem prototype de faisabilité).
    Décompte strictement identique au parcours pixel par pixel, mais vectorisé
    (PIL) : assez rapide pour un contrôle exhaustif de milliers de rendus.
    """
    img = img.convert("RGB")
    diff = ImageChops.difference(img, Image.new("RGB", img.size, palette.bg))
    red, green, blue = diff.split()
    nonzero = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    nonzero = nonzero.point(lambda v: 255 if v > 0 else 0)
    mask = _outside_safe_mask(img.size[0], img.size[1], tol)
    return sum(ImageChops.multiply(nonzero, mask).histogram()[1:])


# ── Vignettes (galerie web) ─────────────────────────────────────────

THUMB_SIZE = 150       # Taille (px) des vignettes servies par l'API
# Heure FIGÉE des vignettes : rendu déterministe → cache disque réutilisable.
THUMB_NOW = datetime(2024, 1, 1, 14, 32, 7)

# Stats d'exemple réalistes (et déterministes) des vignettes.
THEME_SAMPLE_STATS = {
    "cpu_temp": 48.0,
    "gpu_temp": 51.0,
    "cpu_percent": 42.0,
    "ram": {"used": 9.3 * 1024**3, "total": 16.0 * 1024**3, "percent": 58.0},
    "vram": {"used": 3.5 * 1024**3, "total": 8.0 * 1024**3, "percent": 44.0},
    "disks": [{
        "mount": "/",
        "used": 412.0 * 1024**3,
        "total": 1000.0 * 1024**3,
        "percent": 41.0,
    }],
    "liquid_temp": 32.4,
}


def _render_thumbnail(palette: str, layout: str, output_path: str,
                      size: int = THUMB_SIZE) -> bool:
    """Rendu 640×640 déterministe puis réduction LANCZOS vers ``size`` px."""
    if not PIL_AVAILABLE:
        return False
    import tempfile
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="bxrgb_thumb_")
        os.close(fd)
        if not render_monitoring_image(
            THEME_SAMPLE_STATS, tmp, options=list(DEFAULT_OPTIONS),
            now=THUMB_NOW, palette=palette, layout=layout,
        ):
            return False
        with Image.open(tmp) as full:
            thumb = full.convert("RGB").resize(
                (size, size), Image.Resampling.LANCZOS
            )
            thumb.save(output_path, "PNG")
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def render_theme_thumbnail(theme_key: str, output_path: str,
                           size: int = THUMB_SIZE) -> bool:
    """Vignette d'une PALETTE (alias historique), rendue en disposition classic.

    Conservé pour la rétrocompatibilité : ``theme_key`` est une clé de
    palette et le rendu utilise la disposition « classic » (comportement
    visuel des vignettes historiques).
    """
    if not PIL_AVAILABLE or theme_key not in PALETTES:
        return False
    return _render_thumbnail(theme_key, "classic", output_path, size)


def render_palette_thumbnail(palette_key: str, output_path: str,
                             size: int = THUMB_SIZE) -> bool:
    """Vignette d'une palette (rendu classic, comme les vignettes d'origine)."""
    return render_theme_thumbnail(palette_key, output_path, size)


def render_layout_thumbnail(layout_key: str, output_path: str,
                            size: int = THUMB_SIZE,
                            palette: str = DEFAULT_PALETTE) -> bool:
    """Vignette d'une disposition, rendue avec la palette par défaut."""
    if not PIL_AVAILABLE or layout_key not in LAYOUTS:
        return False
    return _render_thumbnail(palette, layout_key, output_path, size)


# ── Compatibilité ───────────────────────────────────────────────────

def monitor_available() -> bool:
    """Le mode monitoring est-il disponible (Pillow + psutil) ?"""
    return PIL_AVAILABLE and PSUTIL_AVAILABLE

#!/usr/bin/env python3
"""
ballistix/monitor.py — Monitoring système pour l'écran du Kraken.

Collecte les stats système (CPU, GPU AMD, RAM, VRAM, disques) et
génère une image 640×640 avec Pillow (chiffres + jauges) pour
l'écran LCD du Kraken Z53.

Dépendances optionnelles :
    - Pillow (rendu d'image) — requise pour le mode monitoring
    - psutil (stats système) — requise pour le mode monitoring

Si ces libs ne sont pas installées, les fonctions retournent
proprement un flag `ok: False` sans crasher le serveur.
"""

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
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    PIL_AVAILABLE = False

# ── Constantes ──────────────────────────────────────────────────────

SCREEN_SIZE = (640, 640)          # Écran du Kraken Z53
CENTER = (320, 320)
SAFE_RADIUS = 300                # Zone où le texte est visible

# ── Thèmes ──────────────────────────────────────────────────────

class Theme:
    def __init__(self, bg, text, accent, gauge_bg, gauge_start, gauge_end,
                 label=None, subtitle=None):
        self.bg = bg
        self.text = text
        self.accent = accent
        self.gauge_bg = gauge_bg
        self.gauge_start = gauge_start
        self.gauge_end = gauge_end
        # Métadonnées exposées par l'API (galerie web) : libellé + sous-titre.
        self.label = label
        self.subtitle = subtitle

THEMES = {
    "overclock": Theme(
        bg=(15, 5, 5),
        text=(240, 240, 240),
        accent=(255, 0, 0),
        gauge_bg=(45, 10, 10),
        gauge_start=(150, 0, 0),
        gauge_end=(255, 40, 40),
        label="Overclock",
        subtitle="Rouge Agressif",
    ),
    "data_center": Theme(
        bg=(5, 15, 25),
        text=(200, 230, 255),
        accent=(0, 160, 255),
        gauge_bg=(10, 30, 50),
        gauge_start=(0, 60, 120),
        gauge_end=(0, 180, 255),
        label="Data Center",
        subtitle="Bleu Technique",
    ),
    "fluid_flow": Theme(
        bg=(25, 35, 45),
        text=(230, 245, 255),
        accent=(120, 210, 255),
        gauge_bg=(50, 70, 90),
        gauge_start=(160, 210, 255),
        gauge_end=(200, 230, 255),
        label="Fluid Flow",
        subtitle="Bleu Pastel",
    ),
}

# Thème par défaut (celui du mode monitoring).
DEFAULT_THEME = "data_center"


def list_themes() -> list:
    """Liste ordonnée des thèmes d'écran pour l'API (défaut en tête).

    Chaque entrée : ``{key, label, subtitle, default}``. C'est la source
    unique : ajouter un thème à THEMES suffit pour qu'il apparaisse dans
    la galerie web (vignette comprise, sans toucher au front).
    """
    def entry(key, theme):
        return {
            "key": key,
            "label": theme.label or key.replace("_", " ").title(),
            "subtitle": theme.subtitle or "",
            "default": key == DEFAULT_THEME,
        }

    ordered = []
    if DEFAULT_THEME in THEMES:
        ordered.append((DEFAULT_THEME, THEMES[DEFAULT_THEME]))
    ordered.extend((k, t) for k, t in THEMES.items() if k != DEFAULT_THEME)
    return [entry(k, t) for k, t in ordered]

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


# ── Rendu d'image ───────────────────────────────────────────────────

def _load_font(size: int):
    """Charge une police TTF si dispo, sinon la police par défaut."""
    if not ImageFont:
        return None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _interpolate_color(c1, c2, factor):
    """Interpolation linéaire entre deux couleurs RGB."""
    return tuple(int(c1[i] + (c2[i] - c1[i]) * factor) for i in range(3))


def _get_row_width(y):
    """Calcule la largeur disponible à l'ordonnée y pour rester dans le cercle."""
    dy = abs(y - CENTER[1])
    if dy >= SAFE_RADIUS:
        return 0
    return 2 * (SAFE_RADIUS**2 - dy**2)**0.5


def _draw_gauge(draw, x, y, width, height, percent, theme: Theme) -> None:
    """Dessine une jauge horizontale arrondie avec les couleurs du thème."""
    if percent is None:
        return
    # Fond de la jauge
    draw.rounded_rectangle(
        [x, y, x + width, y + height], radius=height // 2, fill=theme.gauge_bg
    )
    # Remplissage avec interpolation couleur
    fill_w = int(width * min(100.0, max(0.0, percent)) / 100.0)
    if fill_w > 0:
        color = _interpolate_color(theme.gauge_start, theme.gauge_end, percent / 100.0)
        draw.rounded_rectangle(
            [x, y, x + fill_w, y + height], radius=height // 2, fill=color
        )


def _fmt_bytes(n) -> str:
    """Formate des octets en chaîne lisible."""
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return "—"


def render_monitoring_image(stats: dict, output_path: str, theme_name: str = "data_center", options: list = None, now=None) -> bool:
    """Génère l'image 640×640 de monitoring adaptée au cercle.

    Args:
        stats: Dictionnaire des stats système.
        output_path: Chemin de sauvegarde.
        theme_name: Clé dans THEMES.
        options: Liste des stats à afficher (ex: ['cpu', 'gpu', 'liquid']).
        now: Heure à afficher (datetime/chaîne). None = heure courante ;
            une valeur figée rend le PNG reproductible (vignettes en cache).
    """
    if not PIL_AVAILABLE:
        return False

    theme = THEMES.get(theme_name, THEMES["data_center"])
    if options is None:
        options = ["cpu", "gpu", "ram", "vram", "disks", "liquid"]

    img = Image.new("RGB", SCREEN_SIZE, theme.bg)
    draw = ImageDraw.Draw(img)
    
    font_title = _load_font(32)
    font_label = _load_font(24)
    font_value = _load_font(24)
    font_small = _load_font(20)

    # ── Titre et Heure ──
    y = 110
    title_text = "SYSTEM MONITOR"
    tw = draw.textlength(title_text, font=font_title)
    draw.text((CENTER[0] - tw // 2, y), title_text, fill=theme.accent, font=font_title)
    
    if now is None:
        now_text = time.strftime("%H:%M:%S")
    elif hasattr(now, "strftime"):
        now_text = now.strftime("%H:%M:%S")
    else:
        now_text = str(now)
    ntw = draw.textlength(now_text, font=font_small)
    draw.text((CENTER[0] - ntw // 2, y + 35), now_text, fill=theme.text, font=font_small)
    
    y += 60

    # ── Lignes de stats ──
    row_h = 70
    
    def add_row(label, value_str, percent, icon="•"):
        nonlocal y
        width = _get_row_width(y + 20)
        if width < 100:
            return
        
        # Positionnement centré
        x_start = CENTER[0] - width // 2
        
        # Label et Icône
        draw.text((x_start, y), icon, fill=theme.accent, font=font_value)
        draw.text((x_start + 30, y), label, fill=theme.text, font=font_label)
        
        # Valeur à droite
        vw = draw.textlength(value_str, font=font_value)
        draw.text((x_start + width - vw, y), value_str, fill=theme.text, font=font_value)
        
        # Jauge centrée sous le texte
        _draw_gauge(draw, x_start + 30, y + 30, width - 60, 14, percent, theme)
        y += row_h

    # ── CPU ──
    if "cpu" in options:
        cpu_temp = stats.get("cpu_temp")
        cpu_str = f"{cpu_temp:.0f}°C" if cpu_temp is not None else "—"
        add_row("CPU", cpu_str, stats.get("cpu_percent"), "🌡")

    # ── GPU ──
    if "gpu" in options:
        gpu_temp = stats.get("gpu_temp")
        gpu_str = f"{gpu_temp:.0f}°C" if gpu_temp is not None else "—"
        add_row("GPU", gpu_str, None, "🎮")

    # ── RAM ──
    if "ram" in options:
        ram = stats.get("ram") or {}
        ram_str = f"{_fmt_bytes(ram.get('used'))} / {_fmt_bytes(ram.get('total'))}"
        add_row("RAM", ram_str, ram.get("percent"), "💾")

    # ── VRAM ──
    if "vram" in options:
        vram = stats.get("vram") or {}
        vram_str = f"{_fmt_bytes(vram.get('used'))} / {_fmt_bytes(vram.get('total'))}"
        add_row("VRAM", vram_str, vram.get("percent"), "🎮")

    # ── Disques ──
    if "disks" in options:
        for d in (stats.get("disks") or [])[:2]:
            mount = d.get("mount", "?")
            dstr = f"{_fmt_bytes(d.get('used'))} / {_fmt_bytes(d.get('total'))}"
            add_row(mount, dstr, d.get("percent"), "💿")

    # ── Température Liquide (uniquement si l'option est active) ──
    if "liquid" in options:
        liquid = stats.get("liquid_temp")
        if liquid is not None:
            y_liq = 500
            width_liq = _get_row_width(y_liq)
            if width_liq > 100:
                txt = f"Liquid Temperature: {liquid:.1f}°C"
                tw_liq = draw.textlength(txt, font=font_small)
                draw.text((CENTER[0] - tw_liq // 2, y_liq), txt, fill=theme.accent, font=font_small)

    try:
        img.save(output_path, "PNG")
        return True
    except Exception:
        return False


# ── Vignettes des thèmes (galerie web) ──────────────────────────────

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


def render_theme_thumbnail(theme_key: str, output_path: str, size: int = THUMB_SIZE) -> bool:
    """Génère la vignette d'un thème avec le VRAI moteur de rendu.

    Rendu 640×640 déterministe (stats d'exemple + heure figée THUMB_NOW)
    puis réduction LANCZOS vers ``size`` px : mêmes polices, mêmes
    positions et mêmes jauges que l'aperçu. Retourne False si Pillow est
    indisponible ou si le thème est inconnu.
    """
    if not PIL_AVAILABLE or theme_key not in THEMES:
        return False
    import tempfile
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="bxrgb_theme_thumb_")
        os.close(fd)
        if not render_monitoring_image(
            THEME_SAMPLE_STATS, tmp, theme_key, now=THUMB_NOW
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


# ── Compatibilité ───────────────────────────────────────────────────

def monitor_available() -> bool:
    """Le mode monitoring est-il disponible (Pillow + psutil) ?"""
    return PIL_AVAILABLE and PSUTIL_AVAILABLE

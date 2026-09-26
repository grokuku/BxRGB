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
    def __init__(self, bg, text, accent, gauge_bg, gauge_start, gauge_end):
        self.bg = bg
        self.text = text
        self.accent = accent
        self.gauge_bg = gauge_bg
        self.gauge_start = gauge_start
        self.gauge_end = gauge_end

THEMES = {
    "overclock": Theme(
        bg=(15, 5, 5),
        text=(240, 240, 240),
        accent=(255, 0, 0),
        gauge_bg=(45, 10, 10),
        gauge_start=(150, 0, 0),
        gauge_end=(255, 40, 40),
    ),
    "data_center": Theme(
        bg=(5, 15, 25),
        text=(200, 230, 255),
        accent=(0, 160, 255),
        gauge_bg=(10, 30, 50),
        gauge_start=(0, 60, 120),
        gauge_end=(0, 180, 255),
    ),
    "fluid_flow": Theme(
        bg=(25, 35, 45),
        text=(230, 245, 255),
        accent=(120, 210, 255),
        gauge_bg=(50, 70, 90),
        gauge_start=(160, 210, 255),
        gauge_end=(200, 230, 255),
    ),
}

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
                     "/var/lib/docker", "/boot/efi", "/boot")
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


def render_monitoring_image(stats: dict, output_path: str, theme_name: str = "data_center", options: list = None) -> bool:
    """Génère l'image 640×640 de monitoring adaptée au cercle.

    Args:
        stats: Dictionnaire des stats système.
        output_path: Chemin de sauvegarde.
        theme_name: Clé dans THEMES.
        options: Liste des stats à afficher (ex: ['cpu', 'gpu', 'ram', 'vram', 'disks']).
    """
    if not PIL_AVAILABLE:
        return False

    theme = THEMES.get(theme_name, THEMES["data_center"])
    if options is None:
        options = ["cpu", "gpu", "ram", "vram", "disks"]

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
    
    import time
    now = time.strftime("%H:%M:%S")
    ntw = draw.textlength(now, font=font_small)
    draw.text((CENTER[0] + ntw // 2, y + 35), now, fill=theme.text, font=font_small)
    
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

    # ── Température Liquide (toujours en bas si dispo) ──
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


# ── Compatibilité ───────────────────────────────────────────────────

def monitor_available() -> bool:
    """Le mode monitoring est-il disponible (Pillow + psutil) ?"""
    return PIL_AVAILABLE and PSUTIL_AVAILABLE

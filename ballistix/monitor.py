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
BG_COLOR = (13, 17, 23)           # #0d1117 (thème sombre)
CARD_COLOR = (28, 35, 51)         # #1c2333
TEXT_COLOR = (255, 255, 255)
TEXT_DIM = (139, 148, 158)        # #8b949e
COLOR_COLD = (79, 195, 247)       # #4fc3f7
COLOR_OK = (46, 204, 113)         # #2ecc71
COLOR_WARN = (243, 156, 18)       # #f39c12
COLOR_HOT = (231, 76, 60)         # #e74c3c
COLOR_ACCENT = (233, 69, 96)      # #e94560
GAUGE_BG = (48, 54, 61)           # #30363d

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


def _temp_color(value: float) -> tuple:
    """Couleur d'une température : froid → ok → chaud → brûlant."""
    if value is None:
        return TEXT_DIM
    if value < 30:
        return COLOR_COLD
    if value < 50:
        return COLOR_OK
    if value < 70:
        return COLOR_WARN
    return COLOR_HOT


def _percent_color(value: float) -> tuple:
    """Couleur d'un pourcentage : vert → orange → rouge."""
    if value is None:
        return TEXT_DIM
    if value < 60:
        return COLOR_OK
    if value < 85:
        return COLOR_WARN
    return COLOR_HOT


def _draw_gauge(draw, x, y, width, height, percent, color) -> None:
    """Dessine une jauge horizontale (fond + remplissage)."""
    if percent is None:
        return
    draw.rounded_rectangle(
        [x, y, x + width, y + height], radius=height // 2, fill=GAUGE_BG
    )
    fill_w = int(width * min(100.0, max(0.0, percent)) / 100.0)
    if fill_w > 0:
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


def render_monitoring_image(stats: dict, output_path: str, title: str = "MONITORING") -> bool:
    """Génère l'image 640×640 de monitoring avec jauges.

    Layout :
      ┌──────────────────────────────┐
      │  MONITORING     12:34:56     │
      │  🌡 CPU 52°C   [██████░░░░]  │
      │  🌡 GPU 48°C   [█████░░░░░]  │
      │  💾 RAM 8.2/32GB [███░░░░]   │
      │  🎮 VRAM 4.1/8GB [████░░░░]  │
      │  💿 / [████████░░] 78%       │
      │  ...                         │
      │  ── Liquid: 35.2°C ──        │
      └──────────────────────────────┘
    """
    if not PIL_AVAILABLE:
        return False

    img = Image.new("RGB", SCREEN_SIZE, BG_COLOR)
    draw = ImageDraw.Draw(img)
    font_title = _load_font(28)
    font_label = _load_font(22)
    font_value = _load_font(22)
    font_small = _load_font(18)

    margin = 36
    y = 30

    # ── Titre ──
    draw.text((margin, y), title, fill=COLOR_ACCENT, font=font_title)
    import time
    now = time.strftime("%H:%M:%S")
    tw = draw.textlength(now, font=font_title)
    draw.text((SCREEN_SIZE[0] - margin - tw, y), now, fill=TEXT_DIM, font=font_title)
    y += 44

    # Séparateur
    draw.line([(margin, y), (SCREEN_SIZE[0] - margin, y)], fill=CARD_COLOR, width=2)
    y += 18

    row_h = 78

    def add_row(label, value_str, percent, value_color, icon="•"):
        """Ajoute une ligne : icône + label + valeur + jauge."""
        nonlocal y
        if y + row_h > SCREEN_SIZE[1] - 20:
            return
        draw.text((margin, y), icon, fill=value_color, font=font_value)
        draw.text((margin + 34, y), label, fill=TEXT_COLOR, font=font_label)
        # Valeur à droite au-dessus de la jauge
        vw = draw.textlength(value_str, font=font_value)
        draw.text((SCREEN_SIZE[0] - margin - vw, y - 2), value_str, fill=value_color, font=font_value)
        # Jauge en bas de la ligne
        _draw_gauge(draw, margin + 34, y + 34, SCREEN_SIZE[0] - 2 * margin - 34,
                    14, percent, value_color if percent is not None else TEXT_DIM)
        y += row_h

    # ── CPU ──
    cpu_temp = stats.get("cpu_temp")
    cpu_str = f"{cpu_temp:.0f}°C" if cpu_temp is not None else "—"
    add_row("CPU", cpu_str, stats.get("cpu_percent"), _temp_color(cpu_temp), "🌡")

    # ── GPU ──
    gpu_temp = stats.get("gpu_temp")
    gpu_str = f"{gpu_temp:.0f}°C" if gpu_temp is not None else "—"
    add_row("GPU", gpu_str, None, _temp_color(gpu_temp), "🎮")

    # ── RAM ──
    ram = stats.get("ram") or {}
    ram_str = f"{_fmt_bytes(ram.get('used'))} / {_fmt_bytes(ram.get('total'))}"
    add_row("RAM", ram_str, ram.get("percent"),
            _percent_color(ram.get("percent")), "💾")

    # ── VRAM ──
    vram = stats.get("vram") or {}
    vram_str = f"{_fmt_bytes(vram.get('used'))} / {_fmt_bytes(vram.get('total'))}"
    add_row("VRAM", vram_str, vram.get("percent"),
            _percent_color(vram.get("percent")), "🎮")

    # ── Disques ──
    for d in (stats.get("disks") or [])[:3]:
        mount = d.get("mount", "?")
        dstr = f"{_fmt_bytes(d.get('used'))} / {_fmt_bytes(d.get('total'))}"
        add_row(mount, dstr, d.get("percent"),
                _percent_color(d.get("percent")), "💿")

    # ── Ligne liquide (si fournie) ──
    liquid = stats.get("liquid_temp")
    if liquid is not None:
        draw.line([(margin, y), (SCREEN_SIZE[0] - margin, y)], fill=CARD_COLOR, width=2)
        y += 12
        draw.text((margin, y), f"Liquid: {liquid:.1f}°C", fill=_temp_color(liquid), font=font_small)

    try:
        img.save(output_path, "PNG")
        return True
    except Exception:
        return False


# ── Compatibilité ───────────────────────────────────────────────────

def monitor_available() -> bool:
    """Le mode monitoring est-il disponible (Pillow + psutil) ?"""
    return PIL_AVAILABLE and PSUTIL_AVAILABLE

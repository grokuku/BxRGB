#!/usr/bin/env python3
"""Génère les PNG du harnais front (tests/front/fixtures/) avec le VRAI moteur BxRGB.

- vignettes des 3 thèmes réels (data_center, overclock, fluid_flow)
- vignettes de 9 thèmes factices (test_01..test_09, teintes variées)
  utilisés par le mode « beaucoup de thèmes » (scroll)
- preview.png : aperçu 640×640 du thème data_center

Usage (racine BxRGB déduite de tests/front/, surchargeable) :
    python3 tests/front/make_fixtures.py [chemin/vers/BxRGB]
"""

import colorsys
import sys
from pathlib import Path

# Racine du repo BxRGB = deux niveaux au-dessus de tests/front/.
BXRGB = (Path(sys.argv[1]) if len(sys.argv) > 1
         else Path(__file__).resolve().parents[2]).resolve()
sys.path.insert(0, str(BXRGB))

from ballistix import monitor  # noqa: E402

OUT = Path(__file__).parent / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)


def make_theme(label: str, subtitle: str, hue: float) -> monitor.Theme:
    """Thème factice contrasté pour une teinte HSV donnée."""
    def rgb(s, v):
        return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(hue, s, v))
    return monitor.Theme(
        bg=rgb(0.65, 0.08),
        text=(235, 240, 255),
        accent=rgb(0.9, 0.98),
        gauge_bg=rgb(0.5, 0.18),
        gauge_start=rgb(0.7, 0.38),
        gauge_end=rgb(0.85, 0.95),
        label=label,
        subtitle=subtitle,
    )


def main() -> int:
    # 9 thèmes factices : les clés doivent correspondre à FAKE_THEMES (server.js).
    for i in range(1, 10):
        key = f"test_{i:02d}"
        monitor.THEMES[key] = make_theme(
            f"Test {i:02d}", f"Variante {i:02d}", 0.04 + i * 0.095)

    for key in monitor.THEMES:
        dest = OUT / f"{key}.png"
        ok = monitor.render_theme_thumbnail(key, str(dest))
        print(f"  vignette {key:12s} → {dest.name} ({'ok' if ok else 'ÉCHEC'})")
        if not ok:
            return 1

    preview = OUT / "preview.png"
    ok = monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(preview), "data_center",
        now=monitor.THUMB_NOW)
    print(f"  preview 640×640 → {preview.name} ({'ok' if ok else 'ÉCHEC'})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

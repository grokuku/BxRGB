#!/usr/bin/env python3
"""Génère les PNG du harnais front (tests/front/fixtures/) avec le VRAI moteur BxRGB.

Modèle « palette × disposition » :
- vignettes de PALETTE (5 réelles + 7 factices) → ``<key>.png``
  (clés : data_center, overclock, fluid_flow, graphite, amber, test_pal_01..07)
- vignettes de DISPOSITION (3 réelles + 3 factices) → ``layout-<key>.png``
  (clés : duo, classic, rings, test_lay_01..03)
- preview.png : aperçu 640×640 (disposition duo, palette data_center)

Les clés doivent correspondre à server.js (REAL_PALETTES / FAKE_PALETTES /
REAL_LAYOUTS / FAKE_LAYOUTS).

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


def make_palette(label: str, subtitle: str, hue: float) -> monitor.Palette:
    """Palette factice contrastée pour une teinte HSV donnée."""
    def rgb(s, v):
        return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(hue, s, v))
    return monitor.Palette(
        bg=rgb(0.65, 0.08),
        text=(235, 240, 255),
        accent=rgb(0.9, 0.98),
        gauge_bg=rgb(0.5, 0.18),
        gauge_start=rgb(0.7, 0.38),
        gauge_end=rgb(0.85, 0.95),
        label=label,
        subtitle=subtitle,
        is_new=True,
    )


def main() -> int:
    # 7 palettes factices : clés = FAKE_PALETTES (server.js).
    for i in range(1, 8):
        key = f"test_pal_{i:02d}"
        monitor.PALETTES[key] = make_palette(
            f"Palette {i:02d}", f"Variante {i:02d}", (0.04 + i * 0.11) % 1.0)

    # 3 dispositions factices : clés = FAKE_LAYOUTS (server.js).
    for i in range(1, 4):
        key = f"test_lay_{i:02d}"
        monitor.LAYOUTS[key] = monitor.Layout(
            f"Disposition {i:02d}", f"Variante {i:02d}", is_new=True)

    ok = True
    for key in monitor.PALETTES:
        dest = OUT / f"{key}.png"
        good = monitor.render_palette_thumbnail(key, str(dest))
        ok = ok and good
        print(f"  palette {key:14s} → {dest.name} ({'ok' if good else 'ÉCHEC'})")

    palette_cycle = ["data_center", "overclock", "fluid_flow", "graphite"]
    for idx, key in enumerate(monitor.LAYOUTS):
        dest = OUT / f"layout-{key}.png"
        good = monitor.render_layout_thumbnail(
            key, str(dest), palette=palette_cycle[idx % len(palette_cycle)])
        ok = ok and good
        print(f"  layout  {key:14s} → {dest.name} ({'ok' if good else 'ÉCHEC'})")

    preview = OUT / "preview.png"
    good = monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(preview), palette="data_center",
        layout="duo", now=monitor.THUMB_NOW)
    ok = ok and good
    print(f"  preview 640×640 → {preview.name} ({'ok' if good else 'ÉCHEC'})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

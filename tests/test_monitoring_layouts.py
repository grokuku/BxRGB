#!/usr/bin/env python3
"""Tests de la refonte monitoring LCD : modèle PALETTE × DISPOSITION × CAPTEURS.

Matériel inutile (pas de Kraken/SMBus) : on teste le vrai moteur Pillow, le
catalogue, la migration de config et l'API des vignettes.

Lancement :
    python -m pytest tests/ -v
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ballistix import config, kraken, monitor, server

pytestmark = pytest.mark.skipif(
    not monitor.PIL_AVAILABLE, reason="Pillow indisponible"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Valeurs EXACTES des 3 palettes historiques (ancien monitor.py:THEMES) :
# la non-régression visuelle des installations existantes en dépend.
OLD_THEMES = {
    "data_center": ((5, 15, 25), (200, 230, 255), (0, 160, 255),
                    (10, 30, 50), (0, 60, 120), (0, 180, 255)),
    "overclock": ((15, 5, 5), (240, 240, 240), (255, 0, 0),
                  (45, 10, 10), (150, 0, 0), (255, 40, 40)),
    "fluid_flow": ((25, 35, 45), (230, 245, 255), (120, 210, 255),
                   (50, 70, 90), (160, 210, 255), (200, 230, 255)),
}

OPTION_SETS = {
    "default": ["cpu", "gpu", "ram", "vram", "disks", "liquid"],
    "no_disks": ["cpu", "gpu", "ram", "vram", "liquid"],
    "no_liquid": ["cpu", "gpu", "ram", "vram", "disks"],
    "minimal": ["cpu", "liquid"],
    "no_cpu": ["gpu", "ram", "vram", "disks", "liquid"],
    "no_gpu": ["cpu", "ram", "vram", "disks", "liquid"],
    "cpu_only": ["cpu"],
    "ram_only": ["ram"],
    "disks_only": ["disks", "liquid"],
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Dossier kraken + config isolés dans tmp_path (aucune écriture dans ~/)."""
    kstore = tmp_path / "kraken"

    def fake_store_dir() -> Path:
        kstore.mkdir(parents=True, exist_ok=True)
        return kstore

    monkeypatch.setattr(kraken, "_store_dir", fake_store_dir)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "cfg" / "backups")
    return SimpleNamespace(store=kstore, tmp=tmp_path,
                           client=TestClient(server.app))


# ═══════════════════════════════════════════════════════════════
# 1. Catalogues : palettes et dispositions
# ═══════════════════════════════════════════════════════════════

def test_historical_palettes_unchanged():
    """Les 3 palettes historiques gardent leurs valeurs RGB EXACTES."""
    for key, rgb in OLD_THEMES.items():
        pal = monitor.PALETTES[key]
        assert (pal.bg, pal.text, pal.accent, pal.gauge_bg,
                pal.gauge_start, pal.gauge_end) == rgb
    # L'ancien vocabulaire reste un alias strict.
    assert monitor.THEMES is monitor.PALETTES
    assert monitor.DEFAULT_THEME == monitor.DEFAULT_PALETTE == "data_center"


def test_new_palettes_present_and_distinct():
    assert set(monitor.PALETTES) == {
        "data_center", "overclock", "fluid_flow", "graphite", "amber",
    }
    assert len(monitor.PALETTES) == 5
    for key in ("graphite", "amber"):
        assert monitor.PALETTES[key].is_new is True
        assert monitor.PALETTES[key].colors()["bg"].startswith("#")


def test_layout_catalog_and_default_is_duo():
    assert set(monitor.LAYOUTS) == {"classic", "duo", "rings"}
    assert len(monitor.LAYOUTS) == 3
    assert monitor.DEFAULT_LAYOUT == "duo"
    entries = monitor.list_layouts()
    assert entries[0]["key"] == "duo" and entries[0]["default"] is True
    assert sum(1 for e in entries if e["default"]) == 1
    for e in entries:
        assert e["label"] and e["subtitle"]


def test_list_themes_stays_backward_compatible():
    """``list_themes`` garde exactement 4 clés par entrée (API vague 3)."""
    for t in monitor.list_themes():
        assert set(t.keys()) == {"key", "label", "subtitle", "default"}
    palettes = monitor.list_palettes()
    assert len(palettes) == 5
    assert all("colors" in p for p in palettes)


# ═══════════════════════════════════════════════════════════════
# 2. Rendu : dimensions, non-vide, options, cercle sûr
# ═══════════════════════════════════════════════════════════════

def test_every_layout_renders_640_nonempty(tmp_path):
    for layout in monitor.LAYOUTS:
        out = tmp_path / f"{layout}.png"
        assert monitor.render_monitoring_image(
            monitor.THEME_SAMPLE_STATS, str(out), palette="data_center",
            layout=layout, now=monitor.THUMB_NOW)
        with Image.open(out) as img:
            assert img.size == (640, 640)
        assert out.stat().st_size > 0


def test_no_pixel_outside_safe_circle_all_combos(tmp_path):
    """Contrôle automatique : rien ne dépasse le cercle sûr (rayon 300)."""
    checked = 0
    for layout in monitor.LAYOUTS:
        for palette in monitor.PALETTES:
            for name, options in OPTION_SETS.items():
                out = tmp_path / f"{layout}-{palette}-{name}.png"
                assert monitor.render_monitoring_image(
                    monitor.THEME_SAMPLE_STATS, str(out), palette=palette,
                    layout=layout, options=options, now=monitor.THUMB_NOW)
                with Image.open(out) as img:
                    bad = monitor.count_pixels_outside_safe(
                        img.convert("RGB"), monitor.PALETTES[palette])
                assert bad == 0, f"{layout}/{palette}/{name} : {bad} px hors cercle"
                checked += 1
    assert checked == len(monitor.LAYOUTS) * len(monitor.PALETTES) * len(OPTION_SETS)


def test_all_palettes_render(tmp_path):
    for palette in monitor.PALETTES:
        out = tmp_path / f"{palette}.png"
        assert monitor.render_monitoring_image(
            monitor.THEME_SAMPLE_STATS, str(out), palette=palette,
            layout="duo", now=monitor.THUMB_NOW)
        assert out.is_file()


# ═══════════════════════════════════════════════════════════════
# 3. Rétrocompatibilité de theme_name
# ═══════════════════════════════════════════════════════════════

def test_legacy_theme_name_maps_to_palette_classic(tmp_path):
    legacy = tmp_path / "legacy.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(legacy), "overclock",
        now=monitor.THUMB_NOW)
    explicit = tmp_path / "explicit.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(explicit), palette="overclock",
        layout="classic", now=monitor.THUMB_NOW)
    assert legacy.read_bytes() == explicit.read_bytes()


def test_default_call_uses_duo(tmp_path):
    default = tmp_path / "default.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(default), now=monitor.THUMB_NOW)
    duo = tmp_path / "duo.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(duo), palette=monitor.DEFAULT_PALETTE,
        layout="duo", now=monitor.THUMB_NOW)
    assert default.read_bytes() == duo.read_bytes()


def test_positional_new_style_call(tmp_path):
    """(stats, path, palette, layout[, options[, now]]) reste reconnu."""
    out = tmp_path / "positional.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(out), "amber", "rings",
        ["cpu", "liquid"], monitor.THUMB_NOW)
    kw = tmp_path / "kw.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(kw), options=["cpu", "liquid"],
        palette="amber", layout="rings", now=monitor.THUMB_NOW)
    assert out.read_bytes() == kw.read_bytes()


def test_legacy_liquid_option_gated(tmp_path):
    """La ligne liquide du classique reste conditionnée à l'option."""
    stats = {"cpu_temp": 50.0, "cpu_percent": 12.0,
             "ram": {"used": None, "total": None, "percent": None},
             "disks": [], "liquid_temp": 44.4}
    bg = monitor.PALETTES["data_center"].bg

    def liquid_band(path):
        img = Image.open(path).convert("RGB")
        return [img.getpixel((x, y))
                for y in range(495, 525) for x in range(180, 460)
                if img.getpixel((x, y)) != bg]

    without = tmp_path / "without.png"
    monitor.render_monitoring_image(stats, str(without), "data_center", ["cpu"])
    with_liquid = tmp_path / "with.png"
    monitor.render_monitoring_image(stats, str(with_liquid), "data_center",
                                    ["cpu", "liquid"])
    assert liquid_band(without) == []
    assert len(liquid_band(with_liquid)) > 0


# ═══════════════════════════════════════════════════════════════
# 4. Logique des dispositions (héros, anneaux, échelles)
# ═══════════════════════════════════════════════════════════════

def test_hero_metrics_priority():
    all_on = ["cpu", "gpu", "ram", "vram", "disks", "liquid"]
    assert monitor.hero_metrics(all_on) == ["cpu", "gpu"]
    # CPU/GPU désactivé → le liquide est promu.
    assert monitor.hero_metrics(["gpu", "ram", "vram", "liquid"]) == ["gpu", "liquid"]
    assert monitor.hero_metrics(["ram", "vram", "liquid"]) == ["liquid", "ram"]
    assert monitor.hero_metrics(["ram"]) == ["ram"]
    assert monitor.secondary_metrics(all_on) == ["ram", "vram", "liquid"]


def test_ring_metrics_and_slots_are_adaptive():
    assert monitor.ring_metrics(["cpu", "gpu", "ram", "vram", "liquid"]) == \
        ["cpu", "gpu", "ram", "vram"]
    assert monitor.ring_overflow_metrics(["cpu", "gpu", "ram", "vram", "liquid"]) == \
        ["liquid"]
    assert monitor.ring_metrics(["cpu"]) == ["cpu"]
    assert monitor.ring_slot_set(1) == [(320, 320)]
    assert len(monitor.ring_slot_set(2)) == 2
    assert len(monitor.ring_slot_set(3)) == 3
    assert len(monitor.ring_slot_set(4)) == 4


def test_gauge_scales():
    # CPU/GPU : 20–90 °C.
    assert monitor._gauge_percent({"cpu_temp": 20.0}, "cpu") == pytest.approx(0)
    assert monitor._gauge_percent({"cpu_temp": 55.0}, "cpu") == pytest.approx(50)
    assert monitor._gauge_percent({"gpu_temp": 90.0}, "gpu") == pytest.approx(100)
    # Liquide : 20–50 °C.
    assert monitor._gauge_percent({"liquid_temp": 20.0}, "liquid") == pytest.approx(0)
    assert monitor._gauge_percent({"liquid_temp": 50.0}, "liquid") == pytest.approx(100)
    assert monitor._gauge_percent({"liquid_temp": 35.0}, "liquid") == pytest.approx(50)
    # RAM/VRAM : pourcentage direct.
    assert monitor._gauge_percent({"ram": {"percent": 58.0}}, "ram") == 58.0
    assert monitor._gauge_percent({"vram": {"percent": 44.0}}, "vram") == 44.0
    # Valeur absente → pas de remplissage.
    assert monitor._gauge_percent({}, "cpu") is None


def test_duo_heroes_pixels_differ_when_cpu_disabled(tmp_path):
    """Désactiver CPU change le rendu duo (GPU + LIQUID héros)."""
    on = tmp_path / "on.png"
    off = tmp_path / "off.png"
    opts_on = ["cpu", "gpu", "ram", "vram", "disks", "liquid"]
    opts_off = ["gpu", "ram", "vram", "disks", "liquid"]
    monitor.render_monitoring_image(monitor.THEME_SAMPLE_STATS, str(on),
                                    palette="data_center", layout="duo",
                                    options=opts_on, now=monitor.THUMB_NOW)
    monitor.render_monitoring_image(monitor.THEME_SAMPLE_STATS, str(off),
                                    palette="data_center", layout="duo",
                                    options=opts_off, now=monitor.THUMB_NOW)
    assert on.read_bytes() != off.read_bytes()


# ═══════════════════════════════════════════════════════════════
# 5. Migration de config theme → palette + layout
# ═══════════════════════════════════════════════════════════════

def test_migration_old_theme_preserves_rendering(store):
    store.client  # fixture initialise les dossiers via config
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    old = {
        "version": 2,
        "kraken": {
            "display": {"mode": "monitor", "theme": "overclock",
                        "options": ["cpu", "gpu"], "interval": 30.0},
        },
    }
    config.CONFIG_FILE.write_text(json.dumps(old))

    loaded = config.load()
    disp = loaded["kraken"]["display"]
    assert disp["palette"] == "overclock"
    assert disp["layout"] == "classic"     # rendu existant préservé
    assert disp["theme"] == "overclock"    # miroir de rétrocompatibilité


def test_fresh_config_defaults_to_duo(store):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if config.CONFIG_FILE.exists():
        config.CONFIG_FILE.unlink()
    loaded = config.load()
    disp = loaded["kraken"]["display"]
    assert disp["palette"] == "data_center"
    assert disp["layout"] == "duo"
    assert config.DEFAULT_CONFIG["kraken"]["display"]["palette"] == "data_center"


def test_config_without_kraken_gets_new_defaults(store):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"version": 2, "brightness": 100}))
    disp = config.load()["kraken"]["display"]
    assert disp["palette"] == "data_center"
    assert disp["layout"] == "duo"


# ═══════════════════════════════════════════════════════════════
# 6. API : catalogues + vignettes palette/disposition
# ═══════════════════════════════════════════════════════════════

def test_catalog_endpoints(store):
    data = store.client.get("/api/kraken/themes").json()
    assert data["ok"] is True
    assert data["count"] == 5
    assert data["palette_count"] == 5
    assert data["layout_count"] == 3
    assert {p["key"] for p in data["palettes"]} == set(monitor.PALETTES)
    assert {l["key"] for l in data["layouts"]} == set(monitor.LAYOUTS)

    palettes = store.client.get("/api/kraken/palettes").json()
    assert palettes["ok"] and palettes["count"] == 5
    layouts = store.client.get("/api/kraken/layouts").json()
    assert layouts["ok"] and layouts["count"] == 3


def test_thumb_endpoints_palette_and_layout(store):
    for key in monitor.PALETTES:
        for url in (f"/api/kraken/themes/{key}/thumb.png",
                    f"/api/kraken/palettes/{key}/thumb.png"):
            resp = store.client.get(url)
            assert resp.status_code == 200, url
            assert resp.headers["content-type"] == "image/png"
            assert resp.content.startswith(PNG_MAGIC)
            with Image.open(io.BytesIO(resp.content)) as img:
                assert img.size == (monitor.THUMB_SIZE, monitor.THUMB_SIZE)

    for key in monitor.LAYOUTS:
        resp = store.client.get(f"/api/kraken/layouts/{key}/thumb.png")
        assert resp.status_code == 200, key
        assert resp.content.startswith(PNG_MAGIC)
        with Image.open(io.BytesIO(resp.content)) as img:
            assert img.size == (monitor.THUMB_SIZE, monitor.THUMB_SIZE)


def test_thumb_unknown_returns_404(store):
    for url in ("/api/kraken/themes/nope/thumb.png",
                "/api/kraken/palettes/nope/thumb.png",
                "/api/kraken/layouts/nope/thumb.png"):
        resp = store.client.get(url)
        assert resp.status_code == 404
        assert "inconnu" in resp.json()["detail"].lower()


def test_thumb_economy_8_images(store):
    """5 vignettes de palette + 3 de disposition = 8 (pas 15 combinaisons)."""
    for key in monitor.PALETTES:
        store.client.get(f"/api/kraken/palettes/{key}/thumb.png")
    for key in monitor.LAYOUTS:
        store.client.get(f"/api/kraken/layouts/{key}/thumb.png")
    files = sorted((store.store / "thumbs").glob("*.png"))
    assert len(files) == 8


def test_layout_thumb_cache_and_purge(store):
    url = "/api/kraken/layouts/duo/thumb.png"
    assert store.client.get(url).status_code == 200
    out = kraken.kraken_theme_thumb("duo")
    assert out["ok"] and out["kind"] == "layout"
    first = Path(out["path"])
    assert first.is_file()

    # 2ᵉ appel : cache réutilisé (pas de régénération).
    assert kraken.kraken_theme_thumb("duo")["cached"] is True

    # Ajout d'une empreinte obsolète puis purge → seule l'obsolète part.
    (store.store / "thumbs" / "layout-duo-v0-000000000000.png").write_bytes(b"old")
    result = kraken.kraken_purge_thumbs()
    assert result["ok"] is True
    assert result["removed"] >= 1
    assert first.is_file()

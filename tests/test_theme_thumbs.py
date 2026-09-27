#!/usr/bin/env python3
"""Tests des vignettes de thèmes LCD (vague 3) — rendu PIL réel, matériel inutile.

L'environnement n'a pas de Kraken ni de SMBus : ces tests ne touchent que
le rendu d'image et le cache disque. On vérifie :

  1. GET /api/kraken/themes expose TOUS les thèmes de ballistix/monitor.py
     (clé, libellé, sous-titre, défaut) — plus de duplication front ;
  2. GET /api/kraken/themes/{key}/thumb.png renvoie un PNG valide
     (200, content-type, 150×150, > 0 octet) généré par le VRAI moteur ;
  3. thème inconnu → 404 propre ;
  4. le cache disque est réutilisé (2ᵉ appel sans régénération) et invalidé
     quand l'empreinte du moteur de rendu change ;
  5. le rendu est reproductible grâce au temps figé (now=THUMB_NOW) ;
  6. la vignette est fidèle à l'aperçu : mêmes pixels que le rendu 640×640
     officiel réduit en LANCZOS ;
  7. ajouter un thème dans monitor.py le rend visible (liste + vignette)
     sans aucune modification du front ;
  8. la purge globale du cache (démarrage daemon) ne supprime que les
     vignettes obsolètes, avec garde-fous (hors cache, erreurs d'E/S).

Lancement :
    python -m pytest tests/ -v
"""

import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ballistix import kraken, monitor, server

pytestmark = pytest.mark.skipif(
    not monitor.PIL_AVAILABLE, reason="Pillow indisponible"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CACHE_CONTROL = "public, max-age=3600"


# ═══════════════════════════════════════════════════════════════
# Fixture : stockage isolé dans tmp_path (aucune écriture dans ~/)
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def thumbs(tmp_path, monkeypatch):
    """Client FastAPI + dossier kraken redirigé vers tmp_path."""
    store = tmp_path / "kraken"

    def fake_store_dir() -> Path:
        store.mkdir(parents=True, exist_ok=True)
        return store

    monkeypatch.setattr(kraken, "_store_dir", fake_store_dir)
    client = TestClient(server.app)
    return SimpleNamespace(client=client, store=store, tmp=tmp_path)


def thumb_url(key: str) -> str:
    return f"/api/kraken/themes/{key}/thumb.png"


# ═══════════════════════════════════════════════════════════════
# 1. Liste des thèmes servie par le backend
# ═══════════════════════════════════════════════════════════════

def test_themes_endpoint_exposes_all_monitor_themes(thumbs):
    data = thumbs.client.get("/api/kraken/themes").json()
    assert data["ok"] is True
    assert data["error"] is None
    assert data["count"] == len(monitor.THEMES)
    assert {t["key"] for t in data["themes"]} == set(monitor.THEMES)

    # Le thème par défaut est en tête et unique marqué default.
    assert data["themes"][0]["key"] == monitor.DEFAULT_THEME
    assert [t["key"] for t in data["themes"] if t["default"]] == [
        monitor.DEFAULT_THEME
    ]

    # Libellés/sous-titres = exactement ceux déclarés dans monitor.py.
    for t in data["themes"]:
        theme = monitor.THEMES[t["key"]]
        assert t["label"] == theme.label
        assert t["subtitle"] == theme.subtitle
        assert t["label"] and t["subtitle"]


def test_list_themes_fallback_label_and_blank_subtitle(monkeypatch):
    """Un thème sans métadonnées reste exposable (label dérivé de la clé)."""
    bare = monitor.Theme(
        bg=(0, 0, 0), text=(255, 255, 255), accent=(1, 2, 3),
        gauge_bg=(0, 0, 0), gauge_start=(0, 0, 0), gauge_end=(9, 9, 9),
    )
    monkeypatch.setitem(monitor.THEMES, "bare_theme", bare)
    entry = next(t for t in monitor.list_themes() if t["key"] == "bare_theme")
    assert entry == {"key": "bare_theme", "label": "Bare Theme",
                     "subtitle": "", "default": False}


# ═══════════════════════════════════════════════════════════════
# 2. Vignette PNG valide, générée par le vrai moteur
# ═══════════════════════════════════════════════════════════════

def test_theme_thumb_is_valid_png(thumbs):
    for key in monitor.THEMES:
        response = thumbs.client.get(thumb_url(key))
        assert response.status_code == 200, key
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == CACHE_CONTROL
        assert response.content.startswith(PNG_MAGIC)
        assert len(response.content) > 0
        with Image.open(io.BytesIO(response.content)) as img:
            assert img.format == "PNG"
            assert img.size == (monitor.THUMB_SIZE, monitor.THUMB_SIZE)

    # Une vignette par thème, bien matérialisée sur le disque.
    files = sorted((thumbs.store / "thumbs").glob("*.png"))
    assert len(files) == len(monitor.THEMES)
    assert all(f.stat().st_size > 0 for f in files)


def test_unknown_theme_returns_404(thumbs):
    response = thumbs.client.get(thumb_url("does_not_exist"))
    assert response.status_code == 404
    assert "inconnu" in response.json()["detail"].lower()


def test_thumb_matches_official_render_pixels(thumbs):
    """Fidélité : la vignette EST le rendu 640×640 officiel réduit LANCZOS.

    Mêmes stats d'exemple et même heure figée que l'endpoint, puis
    réduction identique : la comparaison pixel à pixel est exacte.
    """
    response = thumbs.client.get(thumb_url("data_center"))
    assert response.status_code == 200

    reference = thumbs.tmp / "reference_640.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(reference), "data_center",
        now=monitor.THUMB_NOW,
    )
    with Image.open(reference) as full:
        expected = full.convert("RGB").resize(
            (monitor.THUMB_SIZE, monitor.THUMB_SIZE),
            Image.Resampling.LANCZOS,
        )
        expected_pixels = expected.tobytes()

    with Image.open(io.BytesIO(response.content)) as got:
        assert got.convert("RGB").tobytes() == expected_pixels


# ═══════════════════════════════════════════════════════════════
# 3. Cache disque : réutilisation + invalidation
# ═══════════════════════════════════════════════════════════════

def test_thumb_cache_reused_without_regeneration(thumbs, monkeypatch):
    calls = []
    original = monitor.render_theme_thumbnail

    def counting(theme_key, output_path, size=monitor.THUMB_SIZE):
        calls.append(theme_key)
        return original(theme_key, output_path, size)

    monkeypatch.setattr(monitor, "render_theme_thumbnail", counting)

    assert thumbs.client.get(thumb_url("data_center")).status_code == 200
    assert calls == ["data_center"]
    path = Path(kraken.kraken_theme_thumb("data_center")["path"])
    stamp = path.stat().st_mtime_ns

    assert thumbs.client.get(thumb_url("data_center")).status_code == 200
    assert calls == ["data_center"]          # pas de 2ᵉ génération
    assert path.stat().st_mtime_ns == stamp
    assert kraken.kraken_theme_thumb("data_center")["cached"] is True


def test_thumb_cache_invalidated_when_engine_key_changes(thumbs, monkeypatch):
    url = thumb_url("overclock")
    assert thumbs.client.get(url).status_code == 200
    first = Path(kraken.kraken_theme_thumb("overclock")["path"])
    assert first.is_file()

    # Simule une évolution du moteur de rendu : nouvelle empreinte.
    monkeypatch.setattr(kraken, "_render_engine_key", lambda: "v999-test")
    out = kraken.kraken_theme_thumb("overclock")
    assert out["ok"] is True
    assert out["cached"] is False
    second = Path(out["path"])
    assert second != first
    assert second.is_file()
    assert not first.exists()                # ancienne version nettoyée

    # L'endpoint sert bien la nouvelle vignette (et la met en cache).
    assert thumbs.client.get(url).status_code == 200
    assert kraken.kraken_theme_thumb("overclock")["cached"] is True


def test_render_engine_key_follows_monitor_source(tmp_path, monkeypatch):
    """L'empreinte change dès que le CONTENU de monitor.py change."""
    fake_monitor = tmp_path / "monitor_fake.py"
    fake_monitor.write_bytes(b"# moteur v1\n")
    monkeypatch.setattr(monitor, "__file__", str(fake_monitor))

    key1 = kraken._render_engine_key()
    fake_monitor.write_bytes(b"# moteur v2\n")
    key2 = kraken._render_engine_key()

    assert key1.startswith("v")
    assert key1 != key2


# ═══════════════════════════════════════════════════════════════
# 4. Reproductibilité (temps figé)
# ═══════════════════════════════════════════════════════════════

def test_render_monitoring_image_frozen_time_reproducible(thumbs):
    stats = monitor.THEME_SAMPLE_STATS
    p1 = thumbs.tmp / "t1.png"
    p2 = thumbs.tmp / "t2.png"
    assert monitor.render_monitoring_image(
        stats, str(p1), "overclock", now=monitor.THUMB_NOW)
    assert monitor.render_monitoring_image(
        stats, str(p2), "overclock", now=monitor.THUMB_NOW)
    assert p1.read_bytes() == p2.read_bytes()

    # Sans `now` (défaut), le rendu reste fonctionnel (heure courante).
    p3 = thumbs.tmp / "t3.png"
    assert monitor.render_monitoring_image(stats, str(p3), "overclock")
    assert p3.is_file() and p3.stat().st_size > 0

    # Une heure différente change bien le dessin (l'heure est dessinée).
    p4 = thumbs.tmp / "t4.png"
    assert monitor.render_monitoring_image(
        stats, str(p4), "overclock", now=datetime(2024, 1, 1, 15, 45, 9))
    assert p4.read_bytes() != p1.read_bytes()


def test_thumb_png_reproducible_via_endpoint(thumbs):
    url = thumb_url("fluid_flow")
    first = thumbs.client.get(url).content
    assert first.startswith(PNG_MAGIC)

    # Purge du cache → régénération complète.
    for f in (thumbs.store / "thumbs").glob("*.png"):
        f.unlink()
    second = thumbs.client.get(url).content
    assert first == second


# ═══════════════════════════════════════════════════════════════
# 5. Ajouter un thème dans monitor.py suffit (aucun front à toucher)
# ═══════════════════════════════════════════════════════════════

def test_new_theme_appears_and_renders(thumbs, monkeypatch):
    added = monitor.Theme(
        bg=(8, 18, 12), text=(220, 255, 225), accent=(0, 220, 130),
        gauge_bg=(15, 40, 25), gauge_start=(0, 90, 50),
        gauge_end=(0, 255, 150),
        label="Test Vague 3", subtitle="Vert Test",
    )
    monkeypatch.setitem(monitor.THEMES, "test_theme", added)

    data = thumbs.client.get("/api/kraken/themes").json()
    assert data["count"] == len(monitor.THEMES)
    entry = next(t for t in data["themes"] if t["key"] == "test_theme")
    assert entry["label"] == "Test Vague 3"
    assert entry["subtitle"] == "Vert Test"
    assert entry["default"] is False

    response = thumbs.client.get(thumb_url("test_theme"))
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)
    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == (monitor.THUMB_SIZE, monitor.THUMB_SIZE)


# ══════════════════════════════════════════════════════════════
# 6. Purge globale du cache (démarrage daemon + point d'entrée)
# ══════════════════════════════════════════════════════════════

def _thumbs_path(thumbs) -> Path:
    return thumbs.store / "thumbs"


def test_purge_removes_stale_keeps_current(thumbs):
    d = _thumbs_path(thumbs)
    d.mkdir(parents=True, exist_ok=True)
    engine = kraken._render_engine_key()
    current = d / f"data_center-{engine}.png"
    current.write_bytes(b"current")
    stale = d / "data_center-v1-000000000000.png"
    stale.write_bytes(b"stale")
    interrupted = d / f"overclock-{engine}.png.tmp"
    interrupted.write_bytes(b"partial")
    foreign = d / "notes.txt"
    foreign.write_text("hors périmètre", encoding="utf-8")

    result = kraken.kraken_purge_thumbs()

    assert result["ok"] is True
    assert result["engine"] == engine
    assert result["removed"] == 2
    assert result["kept"] == 1
    assert result["errors"] == 0
    assert current.is_file()
    assert not stale.exists()
    assert not interrupted.exists()
    assert foreign.is_file()          # extension inconnue : jamais touchée


def test_purge_never_leaves_cache_dir(thumbs, tmp_path):
    """Un lien symbolique pointant hors du cache n'est jamais supprimé."""
    d = _thumbs_path(thumbs)
    d.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "precieux.png"
    outside.write_bytes(b"ne pas toucher")
    link = d / "lien-v1-000000000000.png"
    link.symlink_to(outside)

    result = kraken.kraken_purge_thumbs()

    assert result["ok"] is True
    assert result["removed"] == 0
    assert link.is_symlink() and link.exists()
    assert outside.read_bytes() == b"ne pas toucher"


def test_purge_skips_without_engine_fingerprint(thumbs, monkeypatch):
    """Empreinte indisponible : aucune suppression (vignettes préservées)."""
    d = _thumbs_path(thumbs)
    d.mkdir(parents=True, exist_ok=True)
    thumb = d / "data_center-v1-000000000000.png"
    thumb.write_bytes(b"peut-etre valide")
    monkeypatch.setattr(kraken, "_render_engine_key", lambda: "v1-unknown")

    result = kraken.kraken_purge_thumbs()

    assert result["ok"] is False
    assert result["removed"] == 0
    assert thumb.is_file()


def test_purge_tolerates_io_errors(thumbs, monkeypatch):
    """Erreur d'E/S : comptée, jamais propagée."""
    d = _thumbs_path(thumbs)
    d.mkdir(parents=True, exist_ok=True)
    (d / "data_center-v1-000000000000.png").write_bytes(b"stale")

    def boom(self):
        raise OSError("disque en feu")
    monkeypatch.setattr(Path, "unlink", boom)

    result = kraken.kraken_purge_thumbs()

    assert result["ok"] is True
    assert result["removed"] == 0
    assert result["errors"] == 1


def test_purge_then_regenerate_via_endpoint(thumbs):
    """Après purge, l'endpoint régénère une vignette valide."""
    assert thumbs.client.get(thumb_url("data_center")).status_code == 200
    d = _thumbs_path(thumbs)
    # Simule une empreinte obsolète supplémentaire puis purge.
    (d / "data_center-v0-000000000000.png").write_bytes(b"old")
    result = kraken.kraken_purge_thumbs()
    assert result["ok"] is True
    assert result["removed"] == 1
    assert result["kept"] == 1         # seule data_center a été demandée

    response = thumbs.client.get(thumb_url("data_center"))
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)
    assert kraken.kraken_theme_thumb("data_center")["cached"] is True

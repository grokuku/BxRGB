#!/usr/bin/env python3
"""Fuseau horaire de l'horloge LCD — rendu, migration, API (matériel inutile).

Le conteneur n'a NI Kraken NI liquidctl : le rendu est testé sur le vrai
moteur Pillow et l'API via TestClient. Couverture :

  1. conversion d'un instant CONNU dans des fuseaux à décalage positif et
     négatif (Europe/Paris +01/+02, America/New_York -05/-04, Asia/Tokyo) ;
  2. DST : le fuseau est appliqué À CHAQUE RENDU (hiver ≠ été, sans redémarrage) ;
  3. repli silencieux et sûr : fuseau absent/vide/inconnu/mal typé, zoneinfo
     indisponible → heure locale du processus, jamais d'exception ;
  4. rendu PIXEL des 3 dispositions : le fuseau convertit l'heure comme attendu
     (PNG identique à un rendu avec l'heure locale équivalente, différent d'un
     autre fuseau), et un fuseau invalide ne change pas le rendu ;
  5. migration de config : clé absente/vide/\"local\"/invalide → None (comportement
     historique), valeur IANA conservée ;
  6. API : GET /api/kraken/timezones (liste triée, décalages, fuseau système,
     repli tzdata), POST display/update (nom inconnu → 400 sans modification),
     save/restore du réglage, aperçu qui reçoit le fuseau.

Lancement :
    python -m pytest tests/ -v
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ballistix import config, kraken, monitor, server

pytestmark = pytest.mark.skipif(
    not monitor.PIL_AVAILABLE, reason="Pillow indisponible"
)

UTC = timezone.utc
WINTER = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)   # hiver boréal
SUMMER = datetime(2024, 7, 15, 12, 0, 0, tzinfo=UTC)   # été boréal
OFFSET_RE = re.compile(r"^[+-]\d{2}:\d{2}$")


# ═══════════════════════════════════════════════════════════════
# Fixture : état Kraken + config isolés
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Dossier kraken + config dans tmp_path, état mémoire réinitialisé."""
    kstore = tmp_path / "kraken"

    def fake_store_dir() -> Path:
        kstore.mkdir(parents=True, exist_ok=True)
        return kstore

    monkeypatch.setattr(kraken, "_store_dir", fake_store_dir)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "cfg" / "backups")

    smbus = server.smbus
    smbus.cleanup()
    smbus._sticks = []
    smbus._bus_map = {}
    kraken.kraken_update_settings(
        lcd={"brightness": 80, "orientation": 0, "mode": "liquid"},
        display={"mode": None, "theme": "data_center", "palette": "data_center",
                 "layout": "duo", "timezone": None,
                 "options": ["cpu", "gpu", "ram", "vram", "disks", "liquid"],
                 "interval": 10.0},
    )

    client = TestClient(server.app)
    yield SimpleNamespace(client=client, smbus=smbus, store=kstore, tmp=tmp_path)

    try:
        kraken.kraken_stop_display()
    except Exception:
        pass
    smbus.cleanup()


def render(tmp_path, layout, name, now=None, timezone=None, options=None):
    """Rendu réel d'une disposition, retourne le chemin du PNG."""
    out = tmp_path / f"{name}.png"
    assert monitor.render_monitoring_image(
        monitor.THEME_SAMPLE_STATS, str(out), palette="data_center",
        layout=layout, options=options, now=now, timezone=timezone)
    return out


# ═══════════════════════════════════════════════════════════════
# 1. Conversion d'un instant connu (décalages positif et négatif)
# ═══════════════════════════════════════════════════════════════

def test_time_text_known_instants_positive_and_negative_offsets():
    """Instant connu : +01:00 (Paris), -05:00 (New York), +09:00 (Tokyo)."""
    assert monitor._time_text(WINTER, "Europe/Paris") == "13:00:00"
    assert monitor._time_text(WINTER, "America/New_York") == "07:00:00"
    assert monitor._time_text(WINTER, "Asia/Tokyo") == "21:00:00"
    assert monitor._time_text(WINTER, "UTC") == "12:00:00"
    # Décalage fixe (secours sans tzdata) : même résultat qu'un fuseau IANA.
    assert monitor._time_text(WINTER, "UTC+02:00") == "14:00:00"
    assert monitor._time_text(WINTER, "GMT-5") == "07:00:00"


def test_time_text_dst_winter_vs_summer():
    """Bascule été/hiver : Paris +01:00 en janvier, +02:00 en juillet."""
    assert monitor._time_text(WINTER, "Europe/Paris") == "13:00:00"
    assert monitor._time_text(SUMMER, "Europe/Paris") == "14:00:00"
    assert monitor._time_text(SUMMER, "America/New_York") == "08:00:00"  # -04:00


def test_timezone_applied_at_each_render_not_at_startup(monkeypatch):
    """`now=None` relit l'horloge à chaque appel → DST suivie à chaud."""
    class FrozenDatetime(datetime):
        current = WINTER

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    monkeypatch.setattr(monitor, "datetime", FrozenDatetime)
    assert monitor._time_text(None, "Europe/Paris") == "13:00:00"
    FrozenDatetime.current = SUMMER
    assert monitor._time_text(None, "Europe/Paris") == "14:00:00"


# ═══════════════════════════════════════════════════════════════
# 2. Repli silencieux sur l'heure locale (jamais d'exception)
# ═══════════════════════════════════════════════════════════════

def test_invalid_or_missing_timezone_falls_back_to_local():
    local = WINTER.astimezone().strftime("%H:%M:%S")
    for value in (None, "", "   ", "local", "LOCAL", "Not/AZone",
                  "Europe/Paris;rm -rf", 12345, ["Europe/Paris"], object()):
        assert monitor._time_text(WINTER, value) == local, repr(value)


def test_normalize_timezone_setting():
    assert monitor.normalize_timezone(None) is None
    assert monitor.normalize_timezone("") is None
    assert monitor.normalize_timezone("  local ") is None
    assert monitor.normalize_timezone("Europe/Paris") == "Europe/Paris"
    assert monitor.normalize_timezone(" UTC+02:00 ") == "UTC+02:00"
    assert monitor.normalize_timezone(42) is None


def test_resolve_timezone_never_raises():
    assert monitor.resolve_timezone("Europe/Paris") is not None
    assert monitor.resolve_timezone("UTC-09:00") is not None
    assert monitor.resolve_timezone("Not/AZone") is None
    assert monitor.resolve_timezone(None) is None
    assert monitor.resolve_timezone(0) is None


def test_render_invalid_timezone_is_byte_identical_to_local(tmp_path):
    """Un fuseau inconnu ne change RIEN au rendu (repli local)."""
    base = render(tmp_path, "duo", "base", now=WINTER)
    bogus = render(tmp_path, "duo", "bogus", now=WINTER, timezone="Not/AZone")
    assert base.read_bytes() == bogus.read_bytes()


# ═══════════════════════════════════════════════════════════════
# 3. Rendus réels Pillow : les 3 dispositions, 2 fuseaux
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("layout", ["classic", "duo", "rings"])
def test_render_timezone_switches_clock_every_layout(tmp_path, layout):
    """PNG(tz=Paris, instant UTC) == PNG(heure locale 13:00), ≠ New York."""
    paris = render(tmp_path, layout, f"{layout}-paris", now=WINTER,
                   timezone="Europe/Paris")
    direct = render(tmp_path, layout, f"{layout}-direct",
                    now=datetime(2024, 1, 15, 13, 0, 0))
    ny = render(tmp_path, layout, f"{layout}-ny", now=WINTER,
                timezone="America/New_York")
    tokyo = render(tmp_path, layout, f"{layout}-tokyo", now=WINTER,
                   timezone="Asia/Tokyo")
    assert paris.read_bytes() == direct.read_bytes()
    assert paris.read_bytes() != ny.read_bytes()
    assert paris.read_bytes() != tokyo.read_bytes()
    assert ny.read_bytes() != tokyo.read_bytes()


@pytest.mark.parametrize("layout", ["classic", "duo", "rings"])
def test_render_fixed_offset_timezone(tmp_path, layout):
    """Un décalage fixe « UTC-05:00 » convertit comme New York (hiver)."""
    fixed = render(tmp_path, layout, f"{layout}-fixed", now=WINTER,
                   timezone="UTC-05:00")
    ny = render(tmp_path, layout, f"{layout}-ny", now=WINTER,
                timezone="America/New_York")
    assert fixed.read_bytes() == ny.read_bytes()


def test_render_dimensions_still_640(tmp_path):
    """Le rendu avec fuseau garde la dalle 640×640."""
    out = render(tmp_path, "duo", "dims", now=WINTER, timezone="Europe/Paris")
    with Image.open(out) as img:
        assert img.size == (640, 640)


# ═══════════════════════════════════════════════════════════════
# 4. Migration / persistance de config (clé absente → inchangé)
# ═══════════════════════════════════════════════════════════════

def _write_config(cfg: dict) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps(cfg))


def test_default_config_has_timezone_none(store):
    assert config.DEFAULT_CONFIG["kraken"]["display"]["timezone"] is None


def test_config_without_timezone_key_behaves_as_before(store):
    """Ancien config.json sans la clé → None (heure locale), rendu inchangé."""
    _write_config({"version": 3,
                   "kraken": {"display": {"theme": "overclock"}}})
    loaded = config.load()
    assert loaded["kraken"]["display"]["timezone"] is None
    # Aucune autre clé n'est perdue par la normalisation.
    assert loaded["kraken"]["display"]["palette"] == "overclock"


@pytest.mark.parametrize("stored,expected", [
    ("Europe/Paris", "Europe/Paris"),
    (" UTC+02:00 ", "UTC+02:00"),
    ("local", None),
    ("", None),
    ("   ", None),
    (42, None),
    (["Europe/Paris"], None),
    ({"name": "Europe/Paris"}, None),
])
def test_config_timezone_normalization(store, stored, expected):
    _write_config({"version": 4,
                   "kraken": {"display": {"layout": "duo",
                                          "timezone": stored}}})
    assert config.load()["kraken"]["display"]["timezone"] == expected


# ═══════════════════════════════════════════════════════════════
# 5. API : catalogue des fuseaux
# ═══════════════════════════════════════════════════════════════

def test_timezones_endpoint_iana_list(store):
    data = store.client.get("/api/kraken/timezones").json()
    assert data["ok"] is True and data["error"] is None
    assert data["count"] == len(data["timezones"])
    assert data["count"] > 0
    keys = [z["key"] for z in data["timezones"]]
    if not data["fallback"]:
        assert keys == sorted(keys)          # tri alphabétique strict
        assert data["count"] > 100           # base tzdata présente
        assert "Europe/Paris" in keys
        for z in data["timezones"]:
            assert OFFSET_RE.match(z["utc_offset"]), z
            assert isinstance(z["system"], bool)
    else:  # repli sans tzdata : décalages fixes uniquement
        assert all(re.match(r"^UTC[+-]\d{2}:\d{2}$", k) for k in keys)
    # Fuseau du processus indiqué (nom et/ou décalage).
    if data["system_timezone"] is not None:
        assert OFFSET_RE.match(data["system_offset"])
        assert any(z["system"] for z in data["timezones"] if
                   z["key"] == data["system_timezone"])


def test_timezones_endpoint_fallback_without_tzdata(store, monkeypatch):
    """tzdata absente → liste réduite de décalages fixes, jamais vide."""
    monkeypatch.setattr(monitor, "ZONEINFO_AVAILABLE", False)
    data = store.client.get("/api/kraken/timezones").json()
    assert data["ok"] is True
    assert data["fallback"] is True
    assert data["count"] > 0
    assert all(re.match(r"^UTC[+-]\d{2}:\d{2}$", z["key"])
               for z in data["timezones"])
    assert any(z["key"] == "UTC+02:00" for z in data["timezones"])
    # Et le rendu accepte directement ces clés de repli.
    assert monitor.resolve_timezone("UTC+02:00") is not None


def test_themes_endpoint_exposes_timezone(store):
    store.client.post("/api/kraken/display/update",
                      json={"timezone": "Europe/Paris"})
    data = store.client.get("/api/kraken/themes").json()
    assert data["timezone"] == "Europe/Paris"
    assert data["timezones_endpoint"] == "/api/kraken/timezones"


# ═══════════════════════════════════════════════════════════════
# 6. API : validation et application temps réel
# ═══════════════════════════════════════════════════════════════

def test_unknown_timezone_is_400_and_state_unchanged(store):
    store.client.post("/api/kraken/display/update",
                      json={"timezone": "Europe/Paris"})
    resp = store.client.post("/api/kraken/display/update",
                             json={"timezone": "Not/AZone"})
    assert resp.status_code == 400
    assert "Fuseau horaire inconnu" in resp.json()["detail"]
    # L'état courant n'a PAS bougé.
    assert store.client.get("/api/kraken/display/status").json()["timezone"] \
        == "Europe/Paris"
    # Absent (null) = réglage conservé, pas de remise à zéro implicite.
    store.client.post("/api/kraken/display/update", json={"palette": "amber"})
    assert store.client.get("/api/kraken/display/status").json()["timezone"] \
        == "Europe/Paris"


def test_display_update_applies_and_resets_timezone(store):
    resp = store.client.post("/api/kraken/display/update",
                             json={"timezone": "America/New_York"})
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "America/New_York"
    assert store.client.get("/api/kraken/display/status").json()["timezone"] \
        == "America/New_York"
    # « local » (et "") réinitialisent à l'heure locale du processus.
    store.client.post("/api/kraken/display/update",
                      json={"timezone": "local"})
    assert store.client.get("/api/kraken/display/status").json()["timezone"] is None
    store.client.post("/api/kraken/display/update", json={"timezone": "UTC"})
    store.client.post("/api/kraken/display/update", json={"timezone": ""})
    assert store.client.get("/api/kraken/display/status").json()["timezone"] is None


def test_save_restore_include_timezone(store):
    client = store.client
    client.post("/api/kraken/display/update", json={"timezone": "Europe/Paris"})
    ref = client.post("/api/save").json()["reference"]
    assert ref["kraken"]["display"]["timezone"] == "Europe/Paris"
    assert client.get("/api/saved").json()["kraken"]["display"]["timezone"] \
        == "Europe/Paris"

    # Modification non sauvegardée → Cancel restaure la référence.
    client.post("/api/kraken/display/update", json={"timezone": "Asia/Tokyo"})
    assert client.get("/api/kraken/display/status").json()["timezone"] \
        == "Asia/Tokyo"
    client.post("/api/restore")
    assert client.get("/api/kraken/display/status").json()["timezone"] \
        == "Europe/Paris"


def test_monitor_preview_renders_with_requested_timezone(store, monkeypatch):
    """L'aperçu passe bien le fuseau au moteur de rendu (heure reflétée)."""
    seen = {}

    def fake_render(stats, output_path, theme_name=None, options=None,
                    now=None, palette=None, layout=None, timezone=None):
        seen.update({"timezone": timezone, "layout": layout})
        Path(output_path).write_bytes(b"png")
        return True

    monkeypatch.setattr(monitor, "render_monitoring_image", fake_render)
    resp = store.client.post("/api/kraken/monitor/preview",
                             json={"timezone": "Europe/Paris", "layout": "duo"})
    assert resp.json()["ok"] is True
    assert seen["timezone"] == "Europe/Paris"
    assert seen["layout"] == "duo"

    resp = store.client.post("/api/kraken/monitor/preview",
                             json={"timezone": "Not/AZone"})
    assert resp.status_code == 400


def test_monitor_start_applies_timezone_and_restarts_thread(store, monkeypatch):
    """Le thread monitoring démarre avec le fuseau, et un update le relance."""
    if not monitor.monitor_available():
        pytest.skip("Pillow/psutil indisponibles")
    seen = []

    def fake_render(stats, output_path, theme_name=None, options=None,
                    now=None, palette=None, layout=None, timezone=None):
        seen.append(timezone)
        Path(output_path).write_bytes(b"png")
        return True

    monkeypatch.setattr(monitor, "render_monitoring_image", fake_render)
    resp = store.client.post("/api/kraken/monitor/start",
                             json={"interval": 60, "timezone": "Europe/Paris"})
    assert resp.json()["ok"] is True
    st = store.client.get("/api/kraken/display/status").json()
    assert st["running"] is True and st["timezone"] == "Europe/Paris"
    thread_before = kraken._active_thread

    def waited(expected=None):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if seen and (expected is None or seen[-1] == expected):
                return True
            time.sleep(0.02)
        return bool(seen) and (expected is None or seen[-1] == expected)

    assert waited("Europe/Paris")

    # Mise à jour en temps réel pendant que le monitoring tourne → relance.
    out = store.client.post("/api/kraken/display/update",
                            json={"timezone": "America/New_York"}).json()
    assert out["ok"] and out["restarted"] is True
    assert kraken._active_thread is not thread_before
    assert waited("America/New_York")
    assert store.client.get("/api/kraken/display/status").json()["timezone"] \
        == "America/New_York"

    # Fuseau inconnu pendant que ça tourne : 400, thread et fuseau inchangés.
    resp = store.client.post("/api/kraken/display/update",
                             json={"timezone": "Not/AZone"})
    assert resp.status_code == 400
    assert store.client.get("/api/kraken/display/status").json()["timezone"] \
        == "America/New_York"
    store.client.post("/api/kraken/display/stop")


def test_monitor_start_invalid_timezone_400_and_no_thread(store):
    resp = store.client.post("/api/kraken/monitor/start",
                             json={"interval": 60, "timezone": "Not/AZone"})
    assert resp.status_code == 400
    st = store.client.get("/api/kraken/display/status").json()
    assert st["running"] is False and st["timezone"] is None

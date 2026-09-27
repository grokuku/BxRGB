#!/usr/bin/env python3
"""Tests du socle Save/Cancel (vague 1) — matériel entièrement mocké.

L'environnement de dev/CI n'a NI SMBus NI liquidctl : les sticks sont des
doubles en mémoire et les commandes liquidctl sont interceptées. On teste :

  1. l'auto-save est coupé (un changement ne modifie ni config.json ni /api/saved) ;
  2. POST /api/save fige l'état courant comme référence ;
  3. POST /api/restore ré-applique la référence au matériel + état mémoire ;
  4. la sauvegarde/restauration des FICHIERS (écran, gallery, corbeille) ;
  5. la compatibilité d'un config.json ancien (sans section kraken) ;
  6. un re-scan ne détruit pas les couleurs/luminosité non sauvegardées.

Lancement :
    python -m pytest tests/ -v
"""

import base64
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ballistix import config, kraken, monitor, server


# ═══════════════════════════════════════════════════════════════
# Doubles matériels
# ═══════════════════════════════════════════════════════════════

class FakeBus:
    """Bus SMBus factice (seul close() est utilisé par le manager)."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeStick:
    """CrucialStick factice : couleurs et luminosité en mémoire."""

    def __init__(self, bus_num, address, num_leds=8, brightness=255,
                 version="Test"):
        self.bus = FakeBus()
        self.bus_num = bus_num
        self.address = address
        self.num_leds = num_leds
        self.colors = [(0, 0, 0)] * num_leds
        self._brightness = brightness
        self._version = version
        self.send_count = 0

    def send_direct_colors(self):
        self.send_count += 1

    def set_led(self, idx, r, g, b):
        if 0 <= idx < self.num_leds:
            self.colors[idx] = (r & 0xFF, g & 0xFF, b & 0xFF)
            self.send_direct_colors()

    def set_all_leds(self, r, g, b):
        color = (r & 0xFF, g & 0xFF, b & 0xFF)
        for i in range(self.num_leds):
            self.colors[i] = color
        self.send_direct_colors()

    def set_brightness(self, level):
        self._brightness = max(0, min(255, int(level)))

    def get_brightness(self):
        return self._brightness

    def get_version(self):
        return self._version

    @property
    def label(self):
        return f"i2c-{self.bus_num} @ 0x{self.address:02X}"


def _ok_run_cmd(args, timeout=kraken.CMD_TIMEOUT):
    """liquidctl simulé : toute commande réussit."""
    return {"ok": True, "code": 0, "stdout": "ok", "stderr": ""}


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def bx(tmp_path, monkeypatch):
    """Environnement isolé : config/kraken dans tmp_path, 2 sticks factices."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "cfg" / "backups")

    def fake_store_dir():
        d = tmp_path / "kraken"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(kraken, "_store_dir", fake_store_dir)
    monkeypatch.setattr(kraken, "_run_cmd", _ok_run_cmd)

    smbus = server.smbus
    smbus.cleanup()
    smbus._save_timer = None
    sticks = [FakeStick(0, 0x30), FakeStick(0, 0x31)]
    smbus._sticks = sticks
    smbus._bus_map = {f"stick_{i}": s for i, s in enumerate(sticks)}
    smbus._last_brightness = 255
    smbus._anim_speed = 1.0
    smbus._anim_framerate = 30
    smbus._smbus_refresh_rate = 20.0

    kraken.kraken_update_settings(
        lcd={"brightness": 80, "orientation": 0, "mode": "liquid"},
        display={"mode": None, "theme": "data_center",
                 "options": ["cpu", "gpu", "ram", "vram", "disks", "liquid"],
                 "interval": 10.0},
    )

    client = TestClient(server.app)
    yield SimpleNamespace(client=client, smbus=smbus, tmp=tmp_path)

    try:
        kraken.kraken_stop_display()
    except Exception:
        pass
    smbus.cleanup()


def seed_config(**overrides):
    """Écrit une référence persistée (config.json) pour le test."""
    cfg = config.load()
    cfg.update(overrides)
    config.save(cfg)
    return cfg


def colors_of(stick):
    return [list(c) for c in stick.colors]


# ═══════════════════════════════════════════════════════════════
# 1. Auto-save coupé
# ═══════════════════════════════════════════════════════════════

def test_color_change_does_not_touch_config_or_reference(bx, monkeypatch):
    ref_colors = {"stick_0": [[1, 2, 3]] * 8, "stick_1": [[4, 5, 6]] * 8}
    seed_config(colors=ref_colors, brightness=200,
                stick_order=["0:0x30", "0:0x31"])
    before = config.CONFIG_FILE.read_bytes()

    scheduled = []
    monkeypatch.setattr(server.SMBusManager, "_schedule_save",
                        lambda self: scheduled.append(1))

    # Modification temps réel (couleur + luminosité + vitesse animation)
    assert bx.client.put("/api/sticks/stick_0/colors",
                         json={"leds": [[255, 0, 0]] * 8}).status_code == 200
    assert bx.client.put("/api/sticks/stick_0/brightness",
                         json={"level": 123}).status_code == 200
    bx.client.post("/api/animation/speed", json={"speed": 3.0})

    # Le matériel a bien reçu la modification…
    assert colors_of(bx.smbus._sticks[0]) == [[255, 0, 0]] * 8
    # …mais AUCUNE sauvegarde n'a été déclenchée ni programmée.
    assert scheduled == []
    assert bx.smbus._save_timer is None
    assert config.CONFIG_FILE.read_bytes() == before

    saved = bx.client.get("/api/saved").json()
    assert saved["colors"] == ref_colors
    assert saved["brightness"] == 200
    assert saved["animation"]["speed"] == 1.0  # référence non modifiée


# ═══════════════════════════════════════════════════════════════
# 2. POST /api/save fige l'état courant
# ═══════════════════════════════════════════════════════════════

def test_save_freezes_current_state(bx):
    bx.client.put("/api/sticks/stick_0/colors", json={"leds": [[7, 8, 9]] * 8})
    bx.client.put("/api/sticks/stick_0/brightness", json={"level": 111})
    bx.client.post("/api/animation/speed", json={"speed": 2.0})
    bx.client.post("/api/animation/refresh", json={"rate": 12})
    bx.client.post("/api/kraken/lcd/brightness", json={"value": 42})

    response = bx.client.post("/api/save")
    assert response.status_code == 200
    ref = response.json()["reference"]

    assert ref["colors"]["stick_0"][0] == [7, 8, 9]
    assert ref["brightness"] == 111
    assert ref["animation"]["speed"] == 2.0
    assert ref["animation"]["refresh"] == 12.0
    assert ref["kraken"]["lcd"]["brightness"] == 42
    assert ref["stick_order"] == ["0:0x30", "0:0x31"]

    # /api/saved reflète la référence, et le disque aussi
    assert bx.client.get("/api/saved").json() == ref
    on_disk = json.loads(config.CONFIG_FILE.read_text())
    assert on_disk["colors"]["stick_0"][0] == [7, 8, 9]
    assert on_disk["brightness"] == 111
    assert on_disk["stick_order"] == ["0:0x30", "0:0x31"]
    assert on_disk["kraken"]["lcd"]["brightness"] == 42


# ═══════════════════════════════════════════════════════════════
# 3. POST /api/restore ré-applique la référence
# ═══════════════════════════════════════════════════════════════

def test_restore_reapplies_reference_exactly(bx):
    bx.client.put("/api/sticks/stick_0/colors", json={"leds": [[1, 2, 3]] * 8})
    bx.client.put("/api/sticks/stick_1/colors", json={"leds": [[4, 5, 6]] * 8})
    bx.client.put("/api/sticks/stick_0/brightness", json={"level": 200})
    bx.client.post("/api/kraken/lcd/brightness", json={"value": 60})
    bx.client.post("/api/kraken/lcd/orientation", json={"value": 180})
    bx.client.post("/api/save")
    ref = bx.client.get("/api/saved").json()
    config_before = config.CONFIG_FILE.read_bytes()

    # Modifications non sauvegardées
    bx.client.put("/api/sticks/stick_0/colors", json={"leds": [[200, 0, 0]] * 8})
    bx.client.put("/api/sticks/stick_1/colors", json={"leds": [[0, 200, 0]] * 8})
    bx.client.put("/api/sticks/stick_1/brightness", json={"level": 42})
    bx.client.post("/api/kraken/lcd/brightness", json={"value": 99})
    # Réordonnancement courant (non sauvegardé)
    bx.smbus._sticks.reverse()

    response = bx.client.post("/api/restore")
    assert response.status_code == 200
    assert response.json()["reference"] == ref

    # Ordre restauré + couleurs + luminosité exactement conformes
    assert [s.address for s in bx.smbus._sticks] == [0x30, 0x31]
    assert colors_of(bx.smbus._sticks[0]) == [[1, 2, 3]] * 8
    assert colors_of(bx.smbus._sticks[1]) == [[4, 5, 6]] * 8
    assert bx.smbus._sticks[0].get_brightness() == 200
    assert bx.smbus._sticks[1].get_brightness() == 200
    lcd = bx.client.get("/api/kraken/lcd/settings").json()
    assert lcd == {"brightness": 60, "orientation": 180, "mode": "liquid"}

    # Restore n'écrit PAS dans config.json
    assert config.CONFIG_FILE.read_bytes() == config_before
    assert bx.client.get("/api/saved").json() == ref


# ═══════════════════════════════════════════════════════════════
# 4. Fichiers : image d'écran, gallery, corbeille
# ═══════════════════════════════════════════════════════════════

def _send_image(client, payload: bytes, animated=False):
    return client.post("/api/kraken/lcd/image", json={
        "data": base64.b64encode(payload).decode(),
        "filename": "gif" if animated else "screen.png",
        "animated": animated,
    })


def test_screen_image_backup_restore_and_save_purge(bx):
    client = bx.client
    screen = Path(kraken.kraken_save_image(b"ORIGINAL", False))
    assert screen.read_bytes() == b"ORIGINAL"
    # Première écriture : rien à sauvegarder
    assert not (kraken._backup_dir() / "screen.png").exists()

    assert _send_image(client, b"NEW", False).json()["ok"]
    assert screen.read_bytes() == b"NEW"
    assert (kraken._backup_dir() / "screen.png").read_bytes() == b"ORIGINAL"

    # Deuxième écrasement : la sauvegarde INITIALE (référence) est conservée
    assert _send_image(client, b"THIRD", False).json()["ok"]
    assert screen.read_bytes() == b"THIRD"
    assert (kraken._backup_dir() / "screen.png").read_bytes() == b"ORIGINAL"

    # Cancel → l'ancienne image revient, la sauvegarde est consommée
    assert client.post("/api/restore").status_code == 200
    assert screen.read_bytes() == b"ORIGINAL"
    assert not (kraken._backup_dir() / "screen.png").exists()

    # Save → purge les sauvegardes, l'écrasement est acté
    assert _send_image(client, b"NEW2", False).json()["ok"]
    assert (kraken._backup_dir() / "screen.png").exists()
    assert client.post("/api/save").status_code == 200
    assert not (kraken._backup_dir() / "screen.png").exists()
    assert screen.read_bytes() == b"NEW2"


def test_gallery_delete_trash_restore_and_save_purge(bx):
    client = bx.client
    gallery = kraken._gallery_dir()

    # Fichier présent AVANT la session (donc dans la référence)
    preexisting = gallery / "old.png"
    preexisting.write_bytes(b"OLD")
    assert client.post("/api/kraken/gallery/delete",
                       json={"name": "old.png"}).json()["ok"]
    assert not preexisting.exists()
    assert (kraken._trash_dir() / "old.png").read_bytes() == b"OLD"

    # Cancel → le fichier supprimé revient
    assert client.post("/api/restore").status_code == 200
    assert preexisting.read_bytes() == b"OLD"
    assert not (kraken._trash_dir() / "old.png").exists()

    # Ajout pendant la session → annulé par Cancel (retiré)
    added = client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"NEW").decode(), "filename": "added.png",
    }).json()
    assert added["ok"] and (gallery / added["name"]).is_file()
    assert client.post("/api/restore").status_code == 200
    assert not (gallery / added["name"]).exists()

    # Save → purge corbeille + journal, la suppression est actée
    assert client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"X").decode(), "filename": "x.png",
    }).json()["ok"]
    assert client.post("/api/kraken/gallery/delete",
                       json={"name": "x.png"}).json()["ok"]
    assert (kraken._trash_dir() / "x.png").exists()
    assert client.post("/api/save").status_code == 200
    assert not (kraken._trash_dir() / "x.png").exists()
    assert not (gallery / "x.png").exists()

    # Restore après Save : plus rien dans la corbeille, x.png ne revient pas
    assert client.post("/api/restore").status_code == 200
    assert not (gallery / "x.png").exists()


def test_gallery_add_then_delete_same_session_restores_absence(bx):
    """Add puis delete dans la même session → Cancel doit laisser absent."""
    client = bx.client
    gallery = kraken._gallery_dir()
    added = client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"Y").decode(), "filename": "y.png",
    }).json()
    assert client.post("/api/kraken/gallery/delete",
                       json={"name": added["name"]}).json()["ok"]
    assert client.post("/api/restore").status_code == 200
    assert not (gallery / added["name"]).exists()
    assert not (kraken._trash_dir() / added["name"]).exists()


def test_save_applies_client_stick_order(bx):
    """Le réordonnancement drag&drop (envoyé avec /api/save) est figé."""
    client = bx.client
    client.put("/api/sticks/stick_0/colors", json={"leds": [[1, 1, 1]] * 8})
    client.put("/api/sticks/stick_1/colors", json={"leds": [[2, 2, 2]] * 8})

    response = client.post("/api/save",
                           json={"stick_order": ["0:0x31", "0:0x30"]})
    assert response.status_code == 200
    ref = response.json()["reference"]
    assert ref["stick_order"] == ["0:0x31", "0:0x30"]
    # Les couleurs suivent leur barrette physique (ids réindexés)
    assert ref["colors"]["stick_0"][0] == [2, 2, 2]
    assert ref["colors"]["stick_1"][0] == [1, 1, 1]
    assert [s.address for s in bx.smbus._sticks] == [0x31, 0x30]
    status = client.get("/api/status").json()
    assert [s["address"] for s in status["sticks"]] == ["0x31", "0x30"]

    # Un ré-ordonnancement courant est annulé par restore (référence respectée)
    bx.smbus.apply_client_order(["0:0x30", "0:0x31"])
    client.post("/api/restore")
    assert [s.address for s in bx.smbus._sticks] == [0x31, 0x30]
    assert [list(c) for c in bx.smbus._sticks[0].colors] == [[2, 2, 2]] * 8
    assert [list(c) for c in bx.smbus._sticks[1].colors] == [[1, 1, 1]] * 8


# ═══════════════════════════════════════════════════════════════
# 5. Compatibilité d'un config.json ancien
# ═══════════════════════════════════════════════════════════════

def test_old_config_without_kraken_section(bx):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    old = {
        "version": 2,
        "colors": {"stick_0": [[10, 20, 30]] * 8},
        "brightness": 128,
        "stick_order": ["0:0x30"],
        "animation": {"type": "rainbow", "enabled": True,
                      "speed": 2.5, "framerate": 15},
        "ui": {"orientation": "vertical", "theme": "dark"},
    }
    config.CONFIG_FILE.write_text(json.dumps(old))

    loaded = config.load()
    # Les clés manquantes prennent les défauts, les présentes sont préservées
    assert loaded["kraken"]["lcd"] == {"brightness": 80, "orientation": 0,
                                       "mode": "liquid"}
    assert loaded["kraken"]["display"]["theme"] == "data_center"
    assert loaded["animation"]["refresh"] == 20
    assert loaded["animation"]["speed"] == 2.5
    # …et DEFAULT_CONFIG n'est pas pollué par le merge
    assert config.DEFAULT_CONFIG["animation"]["refresh"] == 20
    assert config.DEFAULT_CONFIG["kraken"]["lcd"]["brightness"] == 80

    ref = bx.client.get("/api/saved").json()
    assert ref["colors"]["stick_0"][0] == [10, 20, 30]
    assert ref["animation"]["speed"] == 2.5

    # Un save crée bien un backup de l'ancien fichier, et le nouveau fichier
    # embarque la section kraken complète.
    assert bx.client.post("/api/save").status_code == 200
    backups = sorted(config.BACKUP_DIR.glob("config_*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["version"] == 2
    new_cfg = json.loads(config.CONFIG_FILE.read_text())
    assert new_cfg["kraken"]["display"]["options"] == ["cpu", "gpu", "ram",
                                                       "vram", "disks",
                                                       "liquid"]


# ═══════════════════════════════════════════════════════════════
# 6. Re-scan : l'état courant n'est pas écrasé
# ═══════════════════════════════════════════════════════════════

def test_rescan_preserves_unsaved_state(bx, monkeypatch):
    # Référence persistée : 0x30 → A, 0x40 → D (stick_1), luminosité 42
    seed_config(
        colors={"stick_0": [[9, 9, 9]] * 8, "stick_1": [[7, 7, 7]] * 8},
        stick_order=["0:0x30", "0:0x40"],
        brightness=42,
    )
    before = config.CONFIG_FILE.read_bytes()

    # État COURANT avant re-scan : 0x30 connu, couleurs/luminosité modifiées
    old = bx.smbus._sticks[0]  # 0x30
    old.colors = [(11, 22, 33)] * 8
    old.set_brightness(200)
    bx.smbus._sticks = [old]
    bx.smbus._bus_map = {"stick_0": old}

    # Le re-scan retrouve 0x30 (nouvel objet) et découvre 0x40 (nouveau)
    new_30, new_40 = FakeStick(0, 0x30), FakeStick(0, 0x40)
    monkeypatch.setattr(server, "detect_sticks", lambda: [new_30, new_40])
    fake_smbus2 = types.ModuleType("smbus2")
    fake_smbus2.SMBus = lambda bus_num: SimpleNamespace(close=lambda: None)
    monkeypatch.setitem(sys.modules, "smbus2", fake_smbus2)

    status = bx.smbus.scan()
    assert [s["address"] for s in status] == ["0x30", "0x40"]

    # 0x30 : couleurs ET luminosité courantes préservées (pas de Cancel implicite)
    assert colors_of(new_30) == [[11, 22, 33]] * 8
    assert new_30.get_brightness() == 200
    # 0x40 : nouvellement détecté → config persistée appliquée
    assert colors_of(new_40) == [[7, 7, 7]] * 8
    assert new_40.get_brightness() == 42

    # Le scan n'écrit PAS dans config.json (ordre compris)
    assert config.CONFIG_FILE.read_bytes() == before


# ═══════════════════════════════════════════════════════════════
# 7. État mémoire Kraken (liquidctl ne fait aucun readback)
# ═══════════════════════════════════════════════════════════════

def test_kraken_memory_and_display_status(bx):
    client = bx.client

    assert client.post("/api/kraken/lcd/brightness",
                       json={"value": 55}).json()["ok"]
    assert client.post("/api/kraken/lcd/orientation",
                       json={"value": 90}).json()["ok"]
    assert client.post("/api/kraken/lcd/mode",
                       json={"mode": "liquid"}).json()["ok"]
    assert client.get("/api/kraken/lcd/settings").json() == {
        "brightness": 55, "orientation": 90, "mode": "liquid",
    }

    status = client.get("/api/kraken/display/status").json()
    assert status["running"] is False
    assert status["mode"] is None
    assert status["theme"] == "data_center"
    assert status["interval"] == 10.0

    # Démarrage gallery → mode/intervalle mémorisés
    assert client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"G").decode(), "filename": "g.png",
    }).json()["ok"]
    assert client.post("/api/kraken/gallery/start",
                       json={"interval": 3600}).json()["ok"]
    status = client.get("/api/kraken/display/status").json()
    assert status["running"] is True
    assert status["mode"] == "gallery"
    assert status["interval"] == 60.0  # clampé dans [2, 60]

    # Arrêt → mode None (les autres réglages restent mémorisés)
    assert client.post("/api/kraken/display/stop").json()["ok"]
    status = client.get("/api/kraken/display/status").json()
    assert status["running"] is False
    assert status["mode"] is None
    assert status["interval"] == 60.0


def test_save_and_restore_include_kraken_settings(bx):
    client = bx.client
    client.post("/api/kraken/lcd/brightness", json={"value": 33})
    client.post("/api/kraken/lcd/orientation", json={"value": 270})
    client.post("/api/save")
    ref = client.get("/api/saved").json()
    assert ref["kraken"]["lcd"] == {"brightness": 33, "orientation": 270,
                                    "mode": "liquid"}

    # Modifications non sauvegardées puis Cancel
    client.post("/api/kraken/lcd/brightness", json={"value": 99})
    client.post("/api/kraken/lcd/orientation", json={"value": 90})
    response = client.post("/api/restore")
    assert response.json()["reference"]["kraken"]["lcd"]["brightness"] == 33
    assert client.get("/api/kraken/lcd/settings").json() == {
        "brightness": 33, "orientation": 270, "mode": "liquid",
    }


# ═══════════════════════════════════════════════════════════════
# 8. Animation : réglages mémorisés même moteur arrêté
# ═══════════════════════════════════════════════════════════════

def test_animation_settings_survive_stop_and_restore(bx):
    client = bx.client
    client.post("/api/animation/speed", json={"speed": 3.5})
    client.post("/api/animation/refresh", json={"rate": 8})
    status = client.get("/api/animation/status").json()
    assert status["running"] is False
    assert status["speed"] == 3.5
    assert status["refresh"] == 8.0

    client.post("/api/save")
    client.post("/api/animation/speed", json={"speed": 1.0})
    client.post("/api/animation/refresh", json={"rate": 30})
    client.post("/api/restore")

    status = client.get("/api/animation/status").json()
    assert status["speed"] == 3.5
    assert status["refresh"] == 8.0


# ═══════════════════════════════════════════════════════════════
# 9. Kraken temps réel (vague 2) : cadence + application immédiate
# ═══════════════════════════════════════════════════════════════

def _wait_until(predicate, timeout=3.0, step=0.02):
    """Attend qu'un prédicat devienne vrai (test de thread déterministe)."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(step)
    return predicate()


def test_interval_helpers_normalize_and_deadline():
    """La logique de cadence (normalisation + date-butoir) est pure."""
    # Normalisation : plancher 2 s, plafond 60 s, sentinelle « asap ».
    assert kraken.normalize_interval(2) == 2.0
    assert kraken.normalize_interval("2") == 2.0
    assert kraken.normalize_interval(1) == 2.0        # sous le plancher
    assert kraken.normalize_interval(0.5) == 2.0
    assert kraken.normalize_interval(120) == 60.0     # au-dessus du plafond
    assert kraken.normalize_interval("asap") == "asap"
    assert kraken.normalize_interval("ASAP") == "asap"
    assert kraken.normalize_interval("n/a") == 10.0   # invalide → défaut
    assert kraken.normalize_interval(None) == 10.0

    # Cadence cible : « asap » = aucune attente.
    assert kraken.interval_target_seconds("asap") == 0.0
    assert kraken.interval_target_seconds(10) == 10.0
    assert kraken.interval_target_seconds(1) == 2.0

    # Date-butoir : on retire le temps déjà écoulé du cycle.
    assert kraken.next_wait_seconds(10, 3) == 7.0
    assert kraken.next_wait_seconds(10, 12) == 0.0    # cycle trop long
    assert kraken.next_wait_seconds("asap", 5) == 0.0
    assert kraken.next_wait_seconds(2, 0) == 2.0


def test_interval_2s_and_asap_accepted_and_clamped(bx):
    """L'API accepte 2 s et « asap » ; toute valeur reste dans les bornes."""
    client = bx.client

    # Intervalle 2 s : accepté tel quel.
    body = {"interval": 2, "theme": "data_center", "options": ["cpu", "liquid"]}
    assert client.post("/api/kraken/display/update", json=body).json()["ok"]
    st = client.get("/api/kraken/display/status").json()
    assert st["interval"] == 2.0
    assert st["theme"] == "data_center"
    assert st["options"] == ["cpu", "liquid"]

    # « asap » : accepté (plus de clamp max(2.0, ...) côté serveur).
    body = {"interval": "asap", "theme": "overclock", "options": ["gpu"]}
    assert client.post("/api/kraken/display/update", json=body).json()["ok"]
    st = client.get("/api/kraken/display/status").json()
    assert st["interval"] == "asap"
    assert st["theme"] == "overclock"
    assert st["options"] == ["gpu"]

    # Valeur aberrante : clampée dans [2, 60].
    client.post("/api/kraken/display/update", json={"interval": 999})
    assert client.get("/api/kraken/display/status").json()["interval"] == 60.0

    # Aucun thread ne tourne : la mise à jour n'en démarre pas un.
    assert client.get("/api/kraken/display/status").json()["running"] is False


def test_monitor_update_restarts_thread_with_new_options(bx, monkeypatch):
    """Cocher/décocher un capteur relance le thread avec les nouvelles options."""
    client = bx.client
    if not monitor.monitor_available():
        pytest.skip("Pillow/psutil indisponibles")

    calls = []

    def fake_render(stats, output_path, theme_name="data_center", options=None):
        calls.append({"theme": theme_name, "options": list(options or [])})
        Path(output_path).write_bytes(b"png")
        return True

    monkeypatch.setattr(monitor, "render_monitoring_image", fake_render)

    # 1) Démarrage monitoring (intervalle 60 s : le thread attend ensuite).
    body = {"interval": 60, "theme": "data_center", "options": ["cpu"]}
    assert client.post("/api/kraken/monitor/start", json=body).json()["ok"]
    st = client.get("/api/kraken/display/status").json()
    assert st["running"] is True and st["mode"] == "monitor"
    thread_before = kraken._active_thread
    assert _wait_until(lambda: any(c["options"] == ["cpu"] for c in calls))

    # 2) Décoche CPU / coche GPU + liquide → application immédiate.
    body = {"interval": 60, "theme": "overclock",
            "options": ["gpu", "liquid"]}
    out = client.post("/api/kraken/display/update", json=body).json()
    assert out["ok"] and out["restarted"] is True
    thread_after = kraken._active_thread
    assert thread_after is not thread_before       # thread relancé
    assert thread_after.is_alive()

    st = client.get("/api/kraken/display/status").json()
    assert st["mode"] == "monitor"                 # le mode est conservé
    assert st["theme"] == "overclock"
    assert st["options"] == ["gpu", "liquid"]

    # Le NOUVEAU thread rend bien avec les nouvelles options.
    assert _wait_until(lambda: any(c["options"] == ["gpu", "liquid"] for c in calls))
    assert calls[-1]["theme"] == "overclock"

    # 3) Intervalle en temps réel pendant que le monitoring tourne.
    out = client.post("/api/kraken/display/update",
                      json={"interval": "asap"}).json()
    assert out["interval"] == "asap"
    assert out["restarted"] is True
    client.post("/api/kraken/display/stop")


def test_render_monitoring_image_liquid_option_gated(tmp_path):
    """La ligne « Liquid Temperature » n'apparaît que si l'option est active."""
    if not monitor.monitor_available():
        pytest.skip("Pillow/psutil indisponibles")
    from PIL import Image

    stats = {"cpu_temp": 50.0, "cpu_percent": 12.0,
             "ram": {"used": None, "total": None, "percent": None},
             "disks": [], "liquid_temp": 44.4}
    bg = monitor.THEMES["data_center"].bg

    without = tmp_path / "without_liquid.png"
    assert monitor.render_monitoring_image(stats, str(without),
                                           "data_center", ["cpu"])
    with_liquid = tmp_path / "with_liquid.png"
    assert monitor.render_monitoring_image(stats, str(with_liquid),
                                           "data_center", ["cpu", "liquid"])

    def liquid_band_pixels(path):
        img = Image.open(path).convert("RGB")
        return [img.getpixel((x, y))
                for y in range(495, 525) for x in range(180, 460)
                if img.getpixel((x, y)) != bg]

    assert liquid_band_pixels(without) == []          # option absente → rien
    assert len(liquid_band_pixels(with_liquid)) > 0   # option active → ligne


def test_save_restore_display_mode_is_part_of_reference(bx):
    """Le mode d'affichage (gallery/monitor/arrêté) fait partie de la référence."""
    client = bx.client
    added = client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"G").decode(), "filename": "g.png",
    }).json()
    assert added["ok"]

    # Diaporama démarré puis figé par Save
    assert client.post("/api/kraken/gallery/start",
                       json={"interval": 60}).json()["ok"]
    ref = client.post("/api/save").json()["reference"]
    assert ref["kraken"]["display"]["mode"] == "gallery"
    assert ref["kraken"]["display"]["interval"] == 60.0

    # Arrêt courant (non sauvegardé) → Cancel doit RELANCER la gallery
    assert client.post("/api/kraken/display/stop").json()["ok"]
    assert client.get("/api/kraken/display/status").json()["mode"] is None

    assert client.post("/api/restore").status_code == 200
    st = client.get("/api/kraken/display/status").json()
    assert st["running"] is True and st["mode"] == "gallery"
    client.post("/api/kraken/display/stop")

    # Le mode « arrêté » de la référence est aussi restaurable.
    assert client.post("/api/save").status_code == 200   # référence = arrêté
    client.post("/api/kraken/gallery/start", json={"interval": 60})
    assert client.get("/api/kraken/display/status").json()["mode"] == "gallery"
    client.post("/api/restore")
    st = client.get("/api/kraken/display/status").json()
    assert st["running"] is False and st["mode"] is None


def test_pending_changes_reflect_file_annulability(bx):
    """GET /api/kraken/pending miroite ce que Cancel défera sur les fichiers."""
    client = bx.client
    gallery = kraken._gallery_dir()

    assert client.get("/api/kraken/pending").json()["count"] == 0

    # Écran écrasé → sauvegarde en attente
    kraken.kraken_save_image(b"ORIGINAL", False)
    _send_image(client, b"NEW", False)
    pending = client.get("/api/kraken/pending").json()
    assert pending["screens"] == ["screen.png"]

    # Ajout + suppression de gallery
    pre = gallery / "old.png"
    pre.write_bytes(b"OLD")
    assert client.post("/api/kraken/gallery/delete",
                       json={"name": "old.png"}).json()["ok"]
    added = client.post("/api/kraken/gallery/add", json={
        "data": base64.b64encode(b"NEW").decode(), "filename": "new.png",
    }).json()
    pending = client.get("/api/kraken/pending").json()
    assert pending["gallery_added"] == [added["name"]]
    assert pending["gallery_deleted"] == ["old.png"]
    assert pending["count"] == 3

    # Save → tout est acté, plus rien en attente
    assert client.post("/api/save").status_code == 200
    assert client.get("/api/kraken/pending").json()["count"] == 0

    # Nouvelle modification puis Cancel → purge aussi le compteur
    _send_image(client, b"THIRD", False)
    assert client.get("/api/kraken/pending").json()["count"] == 1
    assert client.post("/api/restore").status_code == 200
    assert client.get("/api/kraken/pending").json()["count"] == 0


def test_saved_reference_exposes_theme_options_interval(bx):
    """GET /api/saved reflète theme/options/interval figés par Save."""
    client = bx.client
    client.post("/api/kraken/display/update", json={
        "interval": "asap", "theme": "fluid_flow",
        "options": ["cpu", "gpu", "liquid"],
    })
    ref = client.post("/api/save").json()["reference"]
    assert ref["kraken"]["display"]["theme"] == "fluid_flow"
    assert ref["kraken"]["display"]["options"] == ["cpu", "gpu", "liquid"]
    assert ref["kraken"]["display"]["interval"] == "asap"
    assert client.get("/api/saved").json()["kraken"]["display"]["interval"] == "asap"

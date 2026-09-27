#!/usr/bin/env python3
"""Tests du sous-système d'animations LED — moteur refondu (étapes A/B/C).

Ce fichier remplace le filet de caractérisation de l'ANCIEN moteur, dont
les défauts mesurés sous horloge virtuelle étaient :

  - vitesse gelée : ``set_speed(5)`` en cours d'exécution ne changeait RIEN
    (log « Cycle: 100 steps, 3.3s » identique avant/après ; pente de teinte
    rainbow figée à 60 °/s) ;
  - incandescence quasi imperceptible : ΔR moyen ≈ 1.11/frame ;
  - frame d'animation écrite dans ``stick.colors`` → Save figeait une frame
    aléatoire au lieu des couleurs de base ;
  - restore ne relançait/arrêtait jamais l'animation.

Les tests ci-dessous verrouillent le comportement CORRIGÉ, entièrement
déterministe (horloge virtuelle injectée, matériel mocké, aucune attente
réelle significative, aucun ``random`` global).

Lancement :
    python -m pytest tests/test_animations.py -v
"""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ballistix import config, effects, kraken, server
from ballistix.runner import EffectRunner

# ═══════════════════════════════════════════════════════════════
# Horloge virtuelle + doubles matériels
# ═══════════════════════════════════════════════════════════════

class VirtualClock:
    """Horloge déterministe : ``sleep()`` avance le temps (pas de vraie attente).

    Le thread du runner avance la seule horloge du test : les frames
    s'enchaînent exactement à ``1/framerate`` d'intervalle virtuel.
    """

    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self._cv = threading.Condition()

    def now(self) -> float:
        with self._cv:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._cv:
            self._t += max(0.0, float(seconds))
            self._cv.notify_all()
        time.sleep(0.0005)  # laisse le GIL au thread principal


class FakeBus:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeStick:
    """Stick factice : distingue les frames (``write_colors``) des couleurs
    de base (``send_direct_colors``) — exactement les 2 couches cibles."""

    def __init__(self, bus_num, address, num_leds=8):
        self.bus = FakeBus()
        self.bus_num = bus_num
        self.address = address
        self.num_leds = num_leds
        self.colors = [(0, 0, 0)] * num_leds
        self._brightness = 255
        self._version = "Test"
        self.frame_writes = []   # frames transitoires écrites par le moteur
        self.base_writes = 0     # applications des couleurs de base

    def write_colors(self, colors):
        self.frame_writes.append([tuple(int(c) for c in px) for px in colors])

    def send_direct_colors(self, colors=None):
        if colors is not None:
            self.colors = list(colors)
        self.base_writes += 1

    def set_led(self, idx, r, g, b):
        if 0 <= idx < self.num_leds:
            self.colors[idx] = (r & 0xFF, g & 0xFF, b & 0xFF)

    def set_all_leds(self, r, g, b):
        color = (r & 0xFF, g & 0xFF, b & 0xFF)
        for i in range(self.num_leds):
            self.colors[i] = color

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
    return {"ok": True, "code": 0, "stdout": "ok", "stderr": ""}


def _wait_until(predicate, timeout=4.0, step=0.002):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _hue(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    if mx == r:
        return (60 * (g - b) / (mx - mn)) % 360
    if mx == g:
        return 60 * (b - r) / (mx - mn) + 120
    return 60 * (r - g) / (mx - mn) + 240


def _hue_slope_per_second(records, lo, hi, led=0):
    """Pente de teinte (°/s) d'une LED sur la fenêtre temporelle [lo, hi]."""
    points = []
    previous = None
    accumulated = 0.0
    for stamp, frame in records:
        if stamp < lo or stamp > hi:
            continue
        hue = _hue(*frame[led])
        if previous is None:
            previous = hue
        else:
            delta = (hue - previous) % 360
            if delta > 180:
                delta -= 360
            accumulated += delta
            previous = hue
        points.append((stamp, accumulated))
    assert len(points) >= 3, f"fenêtre trop courte: {len(points)} échantillons"
    span = points[-1][0] - points[0][0]
    return (points[-1][1] - points[0][1]) / span


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

BASE_0 = [120, 60, 30]
BASE_1 = [60, 120, 240]


@pytest.fixture()
def anim(tmp_path, monkeypatch):
    """Environnement isolé : config/kraken dans tmp_path, 2 sticks, horloge virtuelle."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "cfg" / "backups")

    def fake_store_dir():
        d = tmp_path / "kraken"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(kraken, "_store_dir", fake_store_dir)
    monkeypatch.setattr(kraken, "_run_cmd", _ok_run_cmd)

    vc = VirtualClock()
    smbus = server.smbus
    smbus.cleanup()
    sticks = [FakeStick(0, 0x30), FakeStick(0, 0x31)]
    sticks[0].colors = [tuple(BASE_0)] * 8
    sticks[1].colors = [tuple(BASE_1)] * 8
    smbus._sticks = sticks
    smbus._bus_map = {f"stick_{i}": s for i, s in enumerate(sticks)}
    smbus._last_brightness = 255
    smbus._anim_speed = 1.0
    smbus._anim_framerate = 30
    smbus._smbus_refresh_rate = 20.0
    smbus._lighting_mode = "static"
    smbus._lighting_params = {}
    smbus._clock = vc.now
    smbus._sleep = vc.sleep

    kraken.kraken_update_settings(
        lcd={"brightness": 80, "orientation": 0, "mode": "liquid"},
        display={"mode": None, "theme": "data_center",
                 "options": ["cpu", "gpu", "ram", "vram", "disks", "liquid"],
                 "interval": 10.0},
    )

    client = TestClient(server.app)
    yield SimpleNamespace(client=client, smbus=smbus, vc=vc,
                          sticks=sticks, tmp=tmp_path)

    try:
        kraken.kraken_stop_display()
    except Exception:
        pass
    smbus.cleanup()
    smbus._lighting_mode = "static"
    smbus._lighting_params = {}
    smbus._anim_speed = 1.0
    smbus._anim_framerate = 30
    smbus._smbus_refresh_rate = 20.0


def total_frame_writes(sticks):
    return sum(len(s.frame_writes) for s in sticks)


# ═══════════════════════════════════════════════════════════════
# 1. Effets purs (ballistix/effects.py)
# ═══════════════════════════════════════════════════════════════

def test_effects_registry_contract():
    described = effects.describe_effects()
    assert [e["id"] for e in described] == ["incandescence", "rainbow"]
    for effect in described:
        assert effect["label"]
        assert effect["params"], "chaque effet expose ses paramètres"
        for param in effect["params"]:
            assert {"id", "label", "type", "default", "min", "max"} <= set(param)
            assert param["min"] <= param["default"] <= param["max"]
    assert effects.MODE_IDS == ("static", "incandescence", "rainbow")


def test_render_is_pure_deterministic_and_seedable():
    base = [[100, 50, 0]] * 32
    first = effects.render("incandescence", 1.234, {"seed": 7}, base)
    assert effects.render("incandescence", 1.234, {"seed": 7}, base) == first
    # aucune dépendance à un RNG global : appels répétés identiques
    for _ in range(3):
        assert effects.render("incandescence", 1.234, {"seed": 7}, base) == first
    other = effects.render("incandescence", 1.234, {"seed": 8}, base)
    assert other != first, "une autre graine donne un autre rendu"
    # t différent → rendu différent (l'effet est bien animé)
    assert effects.render("incandescence", 1.734, {"seed": 7}, base) != first


def test_render_unknown_effect_raises_and_static_is_identity():
    with pytest.raises(effects.UnknownEffectError):
        effects.render("disco", 0.0, None, [[1, 2, 3]])
    with pytest.raises(effects.UnknownEffectError):
        effects.normalize_params("disco")
    assert effects.normalize_params("static") == {}
    assert effects.render("static", 9.0, None, [[1, 2, 3]]) == [[1, 2, 3]]


def test_render_shape_and_bounds():
    base = [[100, 50, 0]] * 32
    for effect_id in ("incandescence", "rainbow"):
        for t in (0.0, 0.25, 3.3, 11.7):
            frame = effects.render(effect_id, t, None, base)
            assert len(frame) == 32
            for px in frame:
                assert len(px) == 3
                assert all(isinstance(c, int) and 0 <= c <= 255 for c in px)


def test_incandescence_uses_base_colors_as_tint():
    frame = effects.render("incandescence", 0.4, {"seed": 1},
                           [[100, 50, 0]] * 32)
    for px in frame:
        # teinte conservée : R ≈ 2×G (base 100/50), canal B nul
        assert abs(px[0] - 2 * px[1]) <= 1
        assert px[2] == 0
    # un pixel éteint reçoit la braise de repli (sinon effet invisible)
    dark = effects.render("incandescence", 0.4, {"seed": 1},
                          [[0, 0, 0]] * 32)
    assert all(px[0] > 0 and px[0] > px[1] > px[2] for px in dark)


def test_incandescence_amplitude_alive_but_not_stroboscopic():
    """Dérive ample (≥ 40 % de l'échelle) ET fluide (pas de saut brutal).

    Avant refonte : ΔR moyen 1.11/frame (quasi statique). Après : chaque
    pixel parcourt l'essentiel de la plage en quelques secondes.
    """
    base = [[255, 255, 255]] * 32
    samples = [effects.render("incandescence", k / 30.0,
                              {"seed": 3, "sparkle": 0.25}, base)
               for k in range(300)]  # 10 s à 30 fps
    spreads = []
    deltas = []
    for pixel in range(32):
        values = [s[pixel][0] for s in samples]
        spreads.append(max(values) - min(values))
        deltas.extend(abs(values[i + 1] - values[i])
                      for i in range(len(values) - 1))
    average_delta = sum(deltas) / len(deltas)
    print(f"\n[chiffres incandescence] spread min={min(spreads)} "
          f"max={max(spreads)} ; Δ moyen/frame={average_delta:.2f} "
          f"max={max(deltas)}")
    assert min(spreads) >= 100, "chaque pixel doit dériver d'au moins ~40 %"
    assert average_delta >= 1.5, "doit bouger nettement plus vite qu'avant (1.11)"
    assert average_delta <= 25.0, "dérive fluide, pas un clignotement par frame"
    assert max(deltas) < 90, "pas de stroboscope"


def test_rainbow_pure_period_and_layout():
    base = [[0, 0, 0]] * 32
    at_zero = effects.render("rainbow", 0.0, None, base)
    assert at_zero[0] == [255, 0, 0]
    # Les lignes (sticks) sont identiques : la teinte ne dépend que de la colonne.
    assert at_zero[0] == at_zero[8] == at_zero[16] == at_zero[24]
    # Une demi-période (3 s sur 6 s) fait tourner la teinte de 180°.
    half = effects.render("rainbow", 3.0, None, base)
    assert half[0] == [0, 255, 255]
    # La période est un paramètre.
    quarter = effects.render("rainbow", 0.75, {"period_seconds": 3.0}, base)
    assert quarter[0] != at_zero[0], "vitesse doublée → teinte décalée"
    assert effects.render("rainbow", 3.0,
                          {"period_seconds": 3.0}, base)[0] == at_zero[0], \
        "t = période → retour à la teinte initiale"


# ═══════════════════════════════════════════════════════════════
# 2. Runner : vitesse/refresh/framerate à chaud (horloge virtuelle)
# ═══════════════════════════════════════════════════════════════

def _make_runner(effect_id="rainbow", params=None, base=None, **kwargs):
    vc = VirtualClock()
    records = []

    def writer(frame):
        records.append((vc.now(), frame))

    runner = EffectRunner(
        effect_id, params, base if base is not None else [[10, 20, 30]] * 32,
        writer,
        speed=kwargs.pop("speed", 1.0),
        framerate=kwargs.pop("framerate", 30),
        refresh=kwargs.pop("refresh", 30.0),
        clock=vc.now, sleep=vc.sleep,
    )
    return runner, vc, records


def test_runner_speed_read_every_frame_and_phase_rate_changes():
    """Preuve instrumentée : la période RÉELLE change quand la vitesse change."""
    runner, _vc, records = _make_runner("rainbow")
    runner.start()
    try:
        assert _wait_until(lambda: runner.stats()["frames"] >= 90)

        # Vitesse 1.0 : période de teinte attendue = 6 s (60 °/s).
        assert runner.stats()["cycle_seconds"] == pytest.approx(6.0, rel=1e-3)
        split = _vc.now()
        slope_before = _hue_slope_per_second(records, 0.2, split)
        period_before = 360.0 / slope_before

        # Changement de vitesse À CHAUD (aucun restart).
        assert runner.set_speed(3.0) == 3.0
        split_after = _vc.now()
        assert _wait_until(lambda: _vc.now() >= split_after + 0.8)

        slope_after = _hue_slope_per_second(records, split_after, _vc.now())
        period_after = 360.0 / slope_after
        stats_after = runner.stats()
        print(f"\n[chiffres vitesse à chaud] période avant={period_before:.2f}s "
              f"après={period_after:.2f}s ; cycle_seconds={stats_after['cycle_seconds']}")

        assert period_before == pytest.approx(6.0, rel=0.1)
        assert period_after == pytest.approx(2.0, rel=0.1)
        assert stats_after["cycle_seconds"] == pytest.approx(2.0, rel=1e-3)
        # La pente de phase est lue à chaque frame : exactement speed×dt.
        assert slope_after == pytest.approx(3.0 * slope_before, rel=0.05)
    finally:
        runner.stop()
    assert not runner.is_running


def test_runner_refresh_rate_limit_applied_hot():
    runner, _vc, records = _make_runner("rainbow", refresh=5.0)
    runner.start()
    try:
        assert _wait_until(lambda: runner.stats()["frames"] >= 120)
        window_start = _vc.now()
        assert _wait_until(lambda: runner.stats()["frames"] >= 240)
        window_end = _vc.now()
        rate_before = _count_writes(records, window_start, window_end) / \
            (window_end - window_start)

        # refresh ×4 à chaud.
        runner.set_refresh(20.0)
        window_start = _vc.now()
        assert _wait_until(lambda: runner.stats()["frames"] >= 400)
        window_end = _vc.now()
        rate_after = _count_writes(records, window_start, window_end) / \
            (window_end - window_start)
        print(f"\n[chiffres refresh à chaud] écritures/s avant={rate_before:.2f} "
              f"après={rate_after:.2f}")
        # Rate-limit quantifié par les frames (30 fps) : ≤ refresh demandé.
        assert 3.5 <= rate_before <= 5.5
        assert rate_after >= 2.5 * rate_before
        assert rate_after <= 20.5
    finally:
        runner.stop()


def _count_writes(records, lo, hi):
    return sum(1 for stamp, _frame in records if lo <= stamp <= hi)


@pytest.mark.parametrize("effect_id,base_cycle", [
    ("incandescence", 2.0),
    ("rainbow", 6.0),
])
def test_runner_cycle_seconds_changes_hot_for_both_effects(effect_id, base_cycle):
    """Les DEUX effets voient leur période réelle divisée par la vitesse à chaud."""
    runner, _vc, _records = _make_runner(effect_id, refresh=30.0)
    runner.start()
    try:
        assert _wait_until(lambda: runner.stats()["frames"] >= 30)
        assert runner.stats()["cycle_seconds"] == pytest.approx(
            base_cycle, rel=1e-3)

        runner.set_speed(4.0)
        first = runner.stats()
        assert _wait_until(
            lambda: runner.stats()["frames"] >= first["frames"] + 40)
        second = runner.stats()
        rate = (second["phase"] - first["phase"]) / \
            (second["frames"] - first["frames"])
        assert rate == pytest.approx(4 / 30, rel=0.05), \
            f"{effect_id}: phase/frame={rate:.5f}"
        assert second["cycle_seconds"] == pytest.approx(
            base_cycle / 4.0, rel=1e-3)
    finally:
        runner.stop()


def test_runner_framerate_read_each_frame():
    runner, _vc, _records = _make_runner("rainbow", framerate=10)
    runner.start()
    try:
        assert _wait_until(lambda: runner.stats()["frames"] >= 40)
        first = runner.stats()
        assert _wait_until(lambda: runner.stats()["frames"] >= first["frames"] + 40)
        second = runner.stats()
        interval_before = (second["phase"] - first["phase"]) / \
            (second["frames"] - first["frames"])

        runner.set_framerate(30)
        third = runner.stats()
        assert _wait_until(lambda: runner.stats()["frames"] >= third["frames"] + 60)
        fourth = runner.stats()
        interval_after = (fourth["phase"] - third["phase"]) / \
            (fourth["frames"] - third["frames"])
        print(f"\n[chiffres framerate] intervalle avant={interval_before:.4f}s "
              f"après={interval_after:.4f}s (fps cible 30)")
        assert interval_before == pytest.approx(0.1, rel=0.05)
        assert interval_after == pytest.approx(1 / 30, rel=0.05)
    finally:
        runner.stop()


def test_runner_reads_base_colors_each_frame():
    base = [[255, 0, 0]] * 4
    runner, _vc, records = _make_runner("incandescence",
                                        base=base, refresh=30.0)
    runner.start()
    try:
        assert _wait_until(lambda: len(records) >= 5)
        assert all(px[0] > 0 and px[1] == 0 for px in records[-1][1])

        # Édition des couleurs de base à chaud : le moteur la voit.
        base[0] = [0, 255, 0]
        assert _wait_until(
            lambda: len(records) >= 10 and records[-1][1][0][0] == 0)
        frame = records[-1][1]
        assert frame[0][1] > 0 and frame[0][0] == 0
        assert frame[1][0] > 0, "les autres pixels gardent l'ancienne base"
    finally:
        runner.stop()


def test_runner_stop_is_final_and_writer_errors_do_not_kill_loop():
    calls = []
    vc = VirtualClock()

    def failing_writer(frame):
        calls.append(frame)
        raise RuntimeError("transport boom")

    runner = EffectRunner("rainbow", None, [[1, 2, 3]] * 32, failing_writer,
                          speed=1.0, framerate=30, refresh=30.0,
                          clock=vc.now, sleep=vc.sleep)
    runner.start()
    assert _wait_until(lambda: runner.stats()["frames"] >= 10)
    assert runner.is_running, "une erreur d'écriture ne doit pas tuer l'effet"
    assert len(calls) >= 5

    runner.stop()
    assert not runner.is_running
    stopped_calls = len(calls)
    time.sleep(0.05)
    assert len(calls) == stopped_calls, "plus aucune écriture après stop()"


# ═══════════════════════════════════════════════════════════════
# 3. API : contrat, hot-swap, compatibilité
# ═══════════════════════════════════════════════════════════════

def test_effects_endpoint_is_ui_source_of_truth(anim):
    data = anim.client.get("/api/animation/effects").json()
    assert [e["id"] for e in data] == ["incandescence", "rainbow"]
    assert data[0]["label"] == "Incandescence"
    param_ids = [p["id"] for p in data[0]["params"]]
    assert "cycle_seconds" in param_ids


def test_start_unknown_mode_returns_400_and_keeps_current(anim):
    assert anim.client.post("/api/animation/start",
                            json={"mode": "rainbow"}).status_code == 200
    runner_before = anim.smbus.animation_runner

    response = anim.client.post("/api/animation/start", json={"mode": "disco"})
    assert response.status_code == 400
    assert "disco" in response.json()["detail"]
    # legacy {effect: …} également validé
    assert anim.client.post("/api/animation/start",
                            json={"effect": "disco"}).status_code == 400
    # L'ancien moteur n'a PAS été touché par les 400.
    assert anim.smbus.animation_runner is runner_before
    assert runner_before.is_running


def test_start_bounds_and_static_mode(anim):
    response = anim.client.post("/api/animation/start", json={
        "mode": "rainbow", "speed": 99, "framerate": 99, "refresh": 99,
    })
    status = response.json()
    assert response.status_code == 200
    assert status["speed"] == 10.0
    assert status["framerate"] == 30
    assert status["refresh"] == 30.0
    assert status["running"] is True and status["mode"] == "rainbow"

    # Bornes basses via /update
    status = anim.client.post("/api/animation/update", json={
        "speed": 0.001, "framerate": 0, "refresh": 0,
    }).json()
    assert status["speed"] == 0.1
    assert status["framerate"] == 1
    assert status["refresh"] == 1.0

    # Mode repos : stop réel + retrait du moteur.
    status = anim.client.post("/api/animation/start",
                              json={"mode": "static"}).json()
    assert status["running"] is False
    assert status["mode"] == "static"
    assert anim.smbus.animation_runner is None


def test_status_contract_and_cycle_seconds(anim):
    status = anim.client.get("/api/animation/status").json()
    assert {"running", "mode", "speed", "framerate", "refresh",
            "cycle_seconds"} <= set(status)
    assert status["running"] is False and status["mode"] == "static"
    assert status["cycle_seconds"] is None

    anim.client.post("/api/animation/start",
                     json={"mode": "rainbow", "speed": 1.0})
    status = anim.client.get("/api/animation/status").json()
    assert status["running"] is True
    assert status["mode"] == "rainbow"
    assert status["effect"] == "rainbow", "miroir legacy pour le front actuel"
    assert status["cycle_seconds"] == pytest.approx(6.0, rel=1e-3)

    status = anim.client.post("/api/animation/update",
                              json={"speed": 2.0}).json()
    assert status["speed"] == 2.0
    assert status["cycle_seconds"] == pytest.approx(3.0, rel=1e-3)


def test_update_applies_hot_without_restart(anim):
    anim.client.post("/api/animation/start",
                     json={"mode": "rainbow", "speed": 1.0})
    runner = anim.smbus.animation_runner
    response = anim.client.post("/api/animation/update", json={
        "speed": 2.5, "refresh": 10, "framerate": 15,
    })
    assert response.status_code == 200
    assert anim.smbus.animation_runner is runner, "pas de redémarrage du runner"
    assert runner.speed == 2.5
    assert runner.refresh == 10.0
    assert runner.framerate == 15
    status = anim.client.get("/api/animation/status").json()
    assert (status["speed"], status["refresh"], status["framerate"]) == \
        (2.5, 10.0, 15)


def test_speed_change_through_api_changes_real_phase_rate(anim):
    anim.client.post("/api/animation/start",
                     json={"mode": "rainbow", "speed": 1.0})
    runner = anim.smbus.animation_runner
    assert _wait_until(lambda: runner.stats()["frames"] >= 60)

    before_a = runner.stats()
    assert _wait_until(
        lambda: runner.stats()["frames"] >= before_a["frames"] + 80)
    before_b = runner.stats()
    rate_before = (before_b["phase"] - before_a["phase"]) / \
        (before_b["frames"] - before_a["frames"])

    assert anim.client.post("/api/animation/update",
                            json={"speed": 4.0}).status_code == 200
    after_a = runner.stats()
    assert _wait_until(
        lambda: runner.stats()["frames"] >= after_a["frames"] + 80)
    after_b = runner.stats()
    rate_after = (after_b["phase"] - after_a["phase"]) / \
        (after_b["frames"] - after_a["frames"])

    print(f"\n[chiffres API] phase/frame avant={rate_before:.5f} "
          f"après={rate_after:.5f} (attendu 1/30 puis 4/30)")
    assert rate_before == pytest.approx(1 / 30, rel=0.05)
    assert rate_after == pytest.approx(4 / 30, rel=0.05)
    # Période d'un cycle rainbow : 6.0 s → 1.5 s.
    assert anim.client.get("/api/animation/status").json()[
        "cycle_seconds"] == pytest.approx(1.5, rel=1e-3)


def test_hot_swap_stops_old_engine_and_starts_new(anim):
    assert anim.client.post("/api/animation/start",
                            json={"mode": "rainbow",
                                  "speed": 1.0}).status_code == 200
    old_runner = anim.smbus.animation_runner
    assert _wait_until(lambda: old_runner.stats()["frames"] >= 10)

    response = anim.client.post("/api/animation/start",
                                json={"mode": "incandescence",
                                      "speed": 1.0,
                                      "params": {"seed": 5}})
    assert response.status_code == 200
    new_runner = anim.smbus.animation_runner
    assert new_runner is not old_runner
    assert not old_runner.is_running, "l'ancien moteur est arrêté"
    assert new_runner.effect == "incandescence" and new_runner.is_running

    # L'ancien moteur n'écrit plus : son compteur est gelé après le swap.
    swapped_writes = old_runner.stats()["writes"]

    # Le nouveau moteur écrit bien, et ses frames sont teintées par la base
    # (signature de l'incandescence : R ≈ 2×G ≈ 4×B pour BASE_0).
    count_before = total_frame_writes(anim.sticks)
    assert _wait_until(lambda: total_frame_writes(anim.sticks) > count_before)
    assert old_runner.stats()["writes"] == swapped_writes
    for px in anim.sticks[0].frame_writes[-1]:
        assert abs(px[0] - 2 * px[1]) <= 2
        assert abs(px[0] - 4 * px[2]) <= 4
    for px in anim.sticks[1].frame_writes[-1]:
        # BASE_1 = (60, 120, 240) : B = 2×G = 4×R
        assert abs(px[2] - 2 * px[1]) <= 2


def test_compat_routes_delegate_and_keep_front_alive(anim):
    # Ancienne forme de /animation/start (effect + speed + framerate).
    response = anim.client.post("/api/animation/start", json={
        "effect": "rainbow", "speed": 1.0, "framerate": 30,
    })
    assert response.status_code == 200
    assert response.json()["effect"] == "rainbow"
    runner = anim.smbus.animation_runner
    assert runner.effect == "rainbow"

    # /animation/speed → délégué, borné, appliqué au runner à chaud.
    assert anim.client.post("/api/animation/speed",
                            json={"speed": 3.5}).json()["speed"] == 3.5
    assert runner.speed == 3.5
    assert anim.client.post("/api/animation/speed",
                            json={"speed": 999}).json()["speed"] == 10.0
    assert runner.speed == 10.0

    # /animation/refresh → délégué, borné, appliqué au runner à chaud.
    assert anim.client.post("/api/animation/refresh",
                            json={"rate": 8}).json()["rate"] == 8.0
    assert runner.refresh == 8.0
    assert anim.client.post("/api/animation/refresh",
                            json={"rate": 0}).json()["rate"] == 1.0

    # /animation/status : miroir `effect` + nouveau `mode`.
    status = anim.client.get("/api/animation/status").json()
    assert status["effect"] == "rainbow" and status["mode"] == "rainbow"

    # /animation/stop sans body : arrêt réel.
    response = anim.client.post("/api/animation/stop")
    assert response.status_code == 200
    assert response.json()["running"] is False
    assert anim.smbus.animation_runner is None
    assert not runner.is_running


# ═══════════════════════════════════════════════════════════════
# 4. Persistance : Save / Restore 3 couches + migration + boot
# ═══════════════════════════════════════════════════════════════

def test_save_freezes_base_colors_never_animation_frame(anim):
    client, sticks = anim.client, anim.sticks
    assert client.post("/api/animation/start", json={
        "mode": "incandescence", "speed": 1.0, "params": {"seed": 5},
    }).status_code == 200
    runner = anim.smbus.animation_runner
    assert _wait_until(lambda: total_frame_writes(sticks) >= 5)

    base_before = [tuple(c) for c in sticks[0].colors]
    last_frame = [list(px) for px in sticks[0].frame_writes[-1]]
    assert last_frame != [list(c) for c in base_before], \
        "le matériel reçoit bien des frames transitoires"

    reference = client.post("/api/save").json()["reference"]
    assert reference["colors"]["stick_0"] == [list(c) for c in base_before]
    assert reference["lighting"]["mode"] == "incandescence"
    assert reference["lighting"]["running"] is True
    assert reference["lighting"]["speed"] == 1.0

    on_disk = json.loads(config.CONFIG_FILE.read_text())
    assert on_disk["colors"]["stick_0"] == [list(c) for c in base_before]
    assert on_disk["lighting"]["running"] is True
    # Miroir legacy aligné sur lighting.
    assert on_disk["animation"]["type"] == "incandescence"
    assert on_disk["animation"]["enabled"] is True
    # Le moteur n'a jamais écrasé la couche base_colors.
    assert [tuple(c) for c in sticks[0].colors] == base_before
    assert runner.is_running


def test_restore_starts_animation_when_reference_running(anim):
    client = anim.client
    client.post("/api/animation/start", json={
        "mode": "rainbow", "speed": 2.0, "refresh": 10,
    })
    reference = client.post("/api/save").json()["reference"]
    assert reference["lighting"] == {
        "mode": "rainbow", "running": True, "speed": 2.0,
        "framerate": 30, "refresh": 10.0,
        "params": {"period_seconds": 6.0, "hue_spread": 1.0},
    }

    client.post("/api/animation/stop")
    assert client.get("/api/animation/status").json()["running"] is False

    client.post("/api/restore")
    status = client.get("/api/animation/status").json()
    assert status["running"] is True
    assert status["mode"] == "rainbow"
    assert status["speed"] == 2.0
    assert status["refresh"] == 10.0
    assert status["cycle_seconds"] == pytest.approx(3.0, rel=1e-3)
    runner = anim.smbus.animation_runner
    assert runner is not None and runner.effect == "rainbow"


def test_restore_stops_animation_when_reference_not_running(anim):
    client = anim.client
    client.post("/api/animation/start", json={"mode": "incandescence"})
    client.post("/api/animation/stop")
    client.post("/api/save")   # référence : incandescence, running=false

    client.post("/api/animation/start",
                json={"mode": "rainbow", "speed": 5.0})
    assert client.get("/api/animation/status").json()["running"] is True

    client.post("/api/restore")
    status = client.get("/api/animation/status").json()
    assert status["running"] is False
    assert status["mode"] == "incandescence"
    assert status["speed"] == 1.0
    assert anim.smbus.animation_runner is None


def test_get_saved_exposes_lighting_and_legacy_mirror(anim):
    old = {
        "version": 3,
        "colors": {"stick_0": [[10, 20, 30]] * 8},
        "brightness": 128,
        "animation": {"type": "rainbow", "enabled": True,
                      "speed": 2.5, "framerate": 15},
        "ui": {"orientation": "vertical", "theme": "dark"},
    }
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps(old))

    payload = anim.client.get("/api/saved").json()
    assert payload["lighting"] == {
        "mode": "rainbow", "running": True, "speed": 2.5,
        "framerate": 15, "refresh": 20.0,
        "params": {"period_seconds": 6.0, "hue_spread": 1.0},
    }
    assert payload["animation"]["type"] == "rainbow"
    assert payload["animation"]["enabled"] is True
    assert payload["animation"]["speed"] == 2.5


# ── Config : migration + normalisation (tests unitaires) ─────

def _write_config(data):
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps(data))
    return config.load()


def test_migration_animation_to_lighting_all_cases(anim):
    # Cas 1 : ancienne section complète → correspondance type/enabled/…
    loaded = _write_config({
        "version": 3,
        "animation": {"type": "rainbow", "enabled": True,
                      "speed": 2.5, "framerate": 15},
    })
    assert loaded["lighting"] == {
        "mode": "rainbow", "running": True, "speed": 2.5,
        "framerate": 15, "refresh": 20.0,
        "params": {"period_seconds": 6.0, "hue_spread": 1.0},
    }
    assert loaded["animation"]["type"] == "rainbow"  # miroir conservé

    # Cas 2 : aucune section animation → défauts sains.
    loaded = _write_config({"version": 3})
    assert loaded["lighting"] == {
        "mode": "static", "running": False, "speed": 1.0,
        "framerate": 30, "refresh": 20.0, "params": {},
    }

    # Cas 3 : section vide → défauts sains.
    loaded = _write_config({"version": 3, "animation": {}})
    assert loaded["lighting"]["mode"] == "static"
    assert loaded["lighting"]["running"] is False
    assert loaded["lighting"]["speed"] == 1.0

    # Cas 4 : valeurs invalides → bornées / repli.
    loaded = _write_config({
        "version": 3,
        "animation": {"type": "disco", "enabled": True, "speed": 999,
                      "framerate": "oops", "refresh": 0,
                      "params": {"period_seconds": 1.2}},
    })
    lighting = loaded["lighting"]
    assert lighting["mode"] == "static", "mode inconnu → static"
    assert lighting["running"] is False, "static n'a pas de moteur"
    assert lighting["speed"] == 10.0
    assert lighting["framerate"] == 30
    assert lighting["refresh"] == 1.0

    # Cas 5 : vitesse/framerate invalides sur un effet valide → params normalisés.
    loaded = _write_config({
        "version": 3,
        "animation": {"type": "incandescence", "enabled": True,
                      "speed": -4, "framerate": 0,
                      "params": {"cycle_seconds": 0.01, "seed": 42,
                                 "inconnu": 1}},
    })
    lighting = loaded["lighting"]
    assert lighting["mode"] == "incandescence"
    assert lighting["running"] is True
    assert lighting["speed"] == 0.1
    assert lighting["framerate"] == 1
    assert lighting["params"]["cycle_seconds"] == 0.2       # borne min
    assert lighting["params"]["seed"] == 42
    assert "inconnu" not in lighting["params"]

    # Cas 6 : une section lighting existante n'est PAS re-migrée.
    loaded = _write_config({
        "version": 4,
        "lighting": {"mode": "rainbow", "running": True, "speed": 3.0},
        "animation": {"type": "incandescence", "enabled": False},
    })
    assert loaded["lighting"]["mode"] == "rainbow"
    assert loaded["lighting"]["speed"] == 3.0


def test_normalize_lighting_direct_unit():
    assert config.normalize_lighting(None)["mode"] == "static"
    assert config.normalize_lighting("nope")["mode"] == "static"
    assert config.normalize_lighting(
        {"mode": "static", "running": True})["running"] is False
    assert config.normalize_lighting(
        {"mode": "rainbow", "running": "yes"})["running"] is True
    normalized = config.normalize_lighting(
        {"mode": "rainbow", "speed": 0, "framerate": 99, "refresh": -5})
    assert normalized["speed"] == 0.1
    assert normalized["framerate"] == 30
    assert normalized["refresh"] == 1.0
    assert config.DEFAULT_CONFIG["lighting"]["mode"] == "static"
    # Le miroir legacy n'est pas pollué par normalisation.
    assert config.DEFAULT_CONFIG["animation"]["type"] == "static"


def test_boot_resumes_animation_from_lighting(anim, monkeypatch):
    """Config `running=true` → l'animation est REPRISE au démarrage du daemon."""
    cfg = config.load()
    cfg["lighting"] = {
        "mode": "rainbow", "running": True, "speed": 2.0,
        "framerate": 30, "refresh": 10,
        "params": {"period_seconds": 6.0, "hue_spread": 1.0},
    }
    config.save(cfg)

    new_sticks = [FakeStick(0, 0x30), FakeStick(0, 0x31)]
    monkeypatch.setattr(server, "detect_sticks", lambda: new_sticks)

    with TestClient(server.app) as client:
        status = client.get("/api/animation/status").json()
        assert status["running"] is True
        assert status["mode"] == "rainbow"
        assert status["speed"] == 2.0
        assert status["refresh"] == 10.0
        assert status["cycle_seconds"] == pytest.approx(3.0, rel=1e-3)

        # Valeurs justes dès l'ouverture : pas de faux « dirty » au démarrage.
        saved = client.get("/api/saved").json()
        assert saved["lighting"]["speed"] == status["speed"]
        assert saved["lighting"]["running"] is True
        assert saved["lighting"]["mode"] == status["mode"]

        # La reprise tourne réellement (frames écrites sur le matériel mocké).
        assert _wait_until(lambda: total_frame_writes(new_sticks) > 0)

    # Après arrêt du lifespan : moteur stoppé.
    assert server.smbus.animation_runner is None


def test_boot_without_running_reference_stays_static(anim, monkeypatch):
    cfg = config.load()
    cfg["lighting"] = {"mode": "incandescence", "running": False,
                       "speed": 4.0, "framerate": 20, "refresh": 5}
    config.save(cfg)

    new_sticks = [FakeStick(0, 0x30), FakeStick(0, 0x31)]
    monkeypatch.setattr(server, "detect_sticks", lambda: new_sticks)

    with TestClient(server.app) as client:
        status = client.get("/api/animation/status").json()
        assert status["running"] is False
        assert status["mode"] == "incandescence"
        # Les réglages persistés sont bien chargés (fini le faux dirty).
        assert status["speed"] == 4.0
        assert status["framerate"] == 20
        assert status["refresh"] == 5.0
        saved = client.get("/api/saved").json()
        assert saved["lighting"]["speed"] == 4.0


def test_dead_animation_module_replaced():
    """Le vieux module (et son RNG global) a disparu au profit d'effects/runner."""
    import importlib

    assert importlib.util.find_spec("ballistix.animations") is None
    assert importlib.util.find_spec("ballistix.effects") is not None
    assert importlib.util.find_spec("ballistix.runner") is not None
    assert not Path(__file__).resolve().parent.parent.joinpath(
        "ballistix", "animations.py").exists()

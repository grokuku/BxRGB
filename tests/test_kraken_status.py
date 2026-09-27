#!/usr/bin/env python3
"""Statut Kraken — parsing des sorties ``liquidctl status`` (simulées).

Le conteneur n'a NI Kraken NI liquidctl ni SMBus : toutes les sorties
testées ici sont des fixtures texte/JSON reproduisant les formats réels
plausibles :

  1. arbre liquidctl récent (Kraken Z53/Z63/Z73, le device de l'utilisateur) ;
  2. format plat hérité (Kraken X, liquidctl < 1.5) — non-régression ;
  3. arbre ASCII « |-- », unités collées, milliers espacés, virgule décimale,
     casse libre ;
  4. sortie JSON de ``liquidctl status --json`` (préférée) ;
  5. sortie vide ou valeurs N/A → indisponibilité EXPLICITE, jamais 0 inventé ;
  6. commande en échec → ``ok=False`` + erreur remontée (zéro silencieux banni) ;
  7. sortie d'un device non-Kraken (ASUS Aura LED) → aucune valeur parasite ;
  8. valeur 0 légitime ≠ valeur indisponible.

Chaque cas vérifie l'absence de crash, les valeurs extraites, et le
caractère explicite de l'indisponibilité (``missing`` + ``error``).

Lancement :
    python3 -m pytest tests/ -v
"""

import json

import pytest
from fastapi.testclient import TestClient

from ballistix import kraken, server


# ═══════════════════════════════════════════════════════════════
# Sorties liquidctl simulées
# ═══════════════════════════════════════════════════════════════

# Format moderne (liquidctl ≥ 1.5) : arbre ├──/└──, libellés multi-mots.
OUT_TREE_KRAKEN_Z = """\
NZXT Kraken Z (Z53, Z63 or Z73)
├── Liquid temperature      35.2  °C
├── Fan speed                780  rpm
└── Pump speed              2117  rpm
"""

# Format hérité (liquidctl < 1.5) : plat, sans arbre.
OUT_FLAT_KRAKEN_X = """\
Device 0, NZXT Kraken X (X53, X63 or X73)
Liquid temperature      29.9  °C
Fan speed                818  rpm
Pump speed              2237  rpm
"""

# Variantes : arbre ASCII, casse libre, unités collées, milliers espacés,
# virgule décimale.
OUT_ASCII_VARIANTS = """\
NZXT Kraken Z (Z53, Z63 or Z73)
|-- LIQUID TEMPERATURE  35,2 °C
|-- Fan speed           1 200 rpm
`-- Pump speed          2 117 rpm
"""

OUT_UNPARSABLE = """\
NZXT Kraken Z (Z53, Z63 or Z73)
├── Liquid temperature  N/A
├── Fan speed           N/A
└── Pump speed          N/A
"""

OUT_PARTIAL = """\
NZXT Kraken Z (Z53, Z63 or Z73)
├── Liquid temperature      35.2  °C
├── Fan speed               N/A
└── Pump speed              2117  rpm
"""

OUT_ZERO_FAN = """\
NZXT Kraken Z (Z53, Z63 or Z73)
├── Liquid temperature      35.2  °C
├── Fan speed                0  rpm
└── Pump speed              2117  rpm
"""

# Sortie d'un device NON-Kraken : aucun capteur reconnu, aucun crash.
OUT_ASUS_AURA = """\
ASUS Aura LED Controller
├── LED 1 color  #ff0000
├── LED 2 color  #00ff00
└── LED 3 color  #0000ff
"""

# Format RÉEL de `liquidctl status --json` (vérifié dans liquidctl 1.16,
# cli.py:_dev_status_obj) : liste de devices, statut = liste de
# {key, value, unit}. Une variante dict est testée plus bas par robustesse.
OUT_JSON_LIST = json.dumps([{
    "bus": "hid",
    "address": "1-4",
    "description": "NZXT Kraken Z (Z53, Z63 or Z73)",
    "status": [
        {"key": "Liquid temperature", "value": 35.2, "unit": "°C"},
        {"key": "Fan speed", "value": 780, "unit": "rpm"},
        {"key": "Pump speed", "value": 2117, "unit": "rpm"},
    ],
}])

OUT_JSON_SCALARS = json.dumps({
    "bus": "hid",
    "description": "NZXT Kraken Z (Z53, Z63 or Z73)",
    "status": {
        "liquid temperature": 35.2,
        "pump speed": 2117,
        "fan speed": 780,
        "LED 1 color": ["#ff0000"],  # liste ignorée, pas un capteur
    },
})

# Variante dict {label: {value, unit}} (robustesse versions futures).
OUT_JSON_DICT_UNITS = json.dumps({
    "description": "NZXT Kraken Z (Z53, Z63 or Z73)",
    "status": {
        "Liquid temperature": {"value": 35.2, "unit": "°C"},
        "Fan speed": {"value": 780, "unit": "rpm"},
        "Pump speed": {"value": 2117, "unit": "rpm"},
    },
})

FAIL_NO_JSON = {
    "ok": False, "code": 2, "stdout": "",
    "stderr": "liquidctl: error: unrecognized arguments: --json",
}
FAIL_NO_DEVICE = {
    "ok": False, "code": 1, "stdout": "",
    "stderr": "Error: no device matches 'Kraken'",
}


def ok_text(raw: str) -> dict:
    """Réponse ``_run_cmd`` réussie avec ``raw`` en stdout."""
    return {"ok": True, "code": 0, "stdout": raw, "stderr": ""}


def fake_run_cmd(json_result, text_result):
    """Fabrique un ``_run_cmd`` simulé : 1er essai JSON, puis texte."""
    calls = []

    def runner(args, timeout=kraken.CMD_TIMEOUT):
        calls.append(list(args))
        return json_result if "--json" in args else text_result

    return runner, calls


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def status_env(monkeypatch):
    """liquidctl présent + cache du support ``--json`` remis à zéro."""
    monkeypatch.setattr(kraken, "kraken_available", lambda: True)
    monkeypatch.setattr(kraken, "_json_status_probe", None)


def status_with(monkeypatch, text_result, json_result=FAIL_NO_JSON):
    """Installe le ``_run_cmd`` simulé et retourne (runner, appels)."""
    runner, calls = fake_run_cmd(json_result, text_result)
    monkeypatch.setattr(kraken, "_run_cmd", runner)
    return runner, calls


# ═══════════════════════════════════════════════════════════════
# 1. Formats texte (le bug : les libellés multi-mots n'étaient pas parsés)
# ═══════════════════════════════════════════════════════════════

def test_modern_tree_format_extracts_all_values(monkeypatch):
    """Kraken Z (device de l'utilisateur) : format arbre moderne."""
    status_with(monkeypatch, ok_text(OUT_TREE_KRAKEN_Z))
    status = kraken.kraken_status()
    assert status["ok"] is True
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }
    assert status["missing"] == []
    assert status["error"] is None
    assert status["source"] == "text"
    assert "Liquid temperature" in status["raw"]


def test_legacy_flat_format_not_regressed(monkeypatch):
    """Format plat hérité (Kraken X, liquidctl < 1.5) : toujours parsé."""
    status_with(monkeypatch, ok_text(OUT_FLAT_KRAKEN_X))
    status = kraken.kraken_status()
    assert status["ok"] is True
    assert status["data"] == {
        "liquid_temperature": 29.9, "fan_speed": 818.0, "pump_speed": 2237.0,
    }
    assert status["missing"] == [] and status["error"] is None


def test_ascii_tree_case_units_and_decimals(monkeypatch):
    """Arbre ASCII, casse libre, « 35,2 °C », « 1 200 rpm »."""
    status_with(monkeypatch, ok_text(OUT_ASCII_VARIANTS))
    status = kraken.kraken_status()
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 1200.0, "pump_speed": 2117.0,
    }
    assert status["missing"] == [] and status["error"] is None


def test_label_order_does_not_matter(monkeypatch):
    """Pump avant fan (autre ordre de sortie) : les deux sont trouvés."""
    raw = ("NZXT Kraken Z (Z53, Z63 or Z73)\n"
           "├── Pump speed              2117  rpm\n"
           "├── Fan speed                780  rpm\n"
           "└── Liquid temperature      35.2  °C\n")
    status_with(monkeypatch, ok_text(raw))
    status = kraken.kraken_status()
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }


# ═══════════════════════════════════════════════════════════════
# 2. Format JSON (`liquidctl status --json`)
# ═══════════════════════════════════════════════════════════════

def test_json_is_preferred_and_avoids_text_call(monkeypatch):
    """JSON reconnu : aucune commande texte de repli lancée."""
    _, calls = status_with(monkeypatch, ok_text("NE DOIT PAS SERVIR"),
                           json_result=ok_text(OUT_JSON_LIST))
    status = kraken.kraken_status()
    assert status["source"] == "json"
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }
    assert status["missing"] == [] and status["error"] is None
    assert len(calls) == 1 and calls[0][-1] == "--json"


def test_json_scalars_and_rgb_lists_ignored(monkeypatch):
    """Variante dict à valeurs scalaires ; listes RGB ignorées."""
    status_with(monkeypatch, ok_text(""), json_result=ok_text(OUT_JSON_SCALARS))
    status = kraken.kraken_status()
    assert status["source"] == "json"
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }


def test_json_dict_value_unit_variant(monkeypatch):
    """Variante dict {label: {value, unit}} : tolérée aussi."""
    status_with(monkeypatch, ok_text(""), json_result=ok_text(OUT_JSON_DICT_UNITS))
    status = kraken.kraken_status()
    assert status["source"] == "json"
    assert status["data"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }


def test_json_unsupported_falls_back_to_text_once(monkeypatch):
    """Version sans ``--json`` : repli texte, puis plus de tentative JSON."""
    _, calls = status_with(monkeypatch, ok_text(OUT_TREE_KRAKEN_Z))
    status = kraken.kraken_status()
    assert status["source"] == "text" and status["data"]["pump_speed"] == 2117.0
    assert len(calls) == 2  # JSON refusé (code 2) puis texte

    status2 = kraken.kraken_status()
    assert status2["source"] == "text"
    assert len(calls) == 3  # uniquement la commande texte
    assert calls[2][-1] == "status"


# ═══════════════════════════════════════════════════════════════
# 3. Indisponibilité EXPLICITE (jamais de zéro inventé, jamais de crash)
# ═══════════════════════════════════════════════════════════════

def test_empty_output_is_explicit(monkeypatch):
    status_with(monkeypatch, ok_text(""))
    status = kraken.kraken_status()
    assert status["ok"] is True          # la commande a réussi…
    assert status["data"] == {}          # …mais aucune valeur
    assert status["missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]
    assert status["error"]               # indisponibilité expliquée
    assert status["source"] == "text"


def test_na_values_explicitly_missing(monkeypatch):
    """« N/A » → non parsé, signalé, pas transformé en 0."""
    status_with(monkeypatch, ok_text(OUT_UNPARSABLE))
    status = kraken.kraken_status()
    assert status["data"] == {}
    assert status["missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]
    assert "non reconnue" in status["error"]


def test_partial_output_lists_missing_field(monkeypatch):
    """Valeur partielle : seule la clé absente est signalée."""
    status_with(monkeypatch, ok_text(OUT_PARTIAL))
    status = kraken.kraken_status()
    assert status["data"] == {"liquid_temperature": 35.2, "pump_speed": 2117.0}
    assert status["missing"] == ["fan_speed"]
    assert "vitesse ventilos" in status["error"]


def test_command_failure_is_reported_not_swallowed(monkeypatch):
    """Commande en échec : ok=False, erreur remontée, aucune valeur."""
    status_with(monkeypatch, FAIL_NO_DEVICE)
    status = kraken.kraken_status()
    assert status["ok"] is False
    assert status["data"] == {}
    assert status["source"] is None
    assert "no device matches" in status["error"]
    assert status["missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]


def test_non_kraken_device_yields_no_parasite_value(monkeypatch):
    """Sortie d'un ASUS Aura LED : rien de parsé, pas de crash."""
    status_with(monkeypatch, ok_text(OUT_ASUS_AURA))
    status = kraken.kraken_status()
    assert status["ok"] is True
    assert status["data"] == {}
    assert status["missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]
    assert status["error"]


def test_zero_speed_is_a_value_not_a_missing(monkeypatch):
    """0 rpm est légitime : présent dans data, absent de missing."""
    status_with(monkeypatch, ok_text(OUT_ZERO_FAN))
    status = kraken.kraken_status()
    assert status["data"]["fan_speed"] == 0.0
    assert "fan_speed" not in status["missing"]
    assert status["error"] is None


def test_liquidctl_not_installed(monkeypatch):
    monkeypatch.setattr(kraken, "kraken_available", lambda: False)
    status = kraken.kraken_status()
    assert status["ok"] is False
    assert "n'est pas installé" in status["error"]
    assert status["missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]


# ═══════════════════════════════════════════════════════════════
# 4. _parse_float : unités, milliers, virgules, robustesse
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("35.2°C", 35.2),
    ("35,2 °C", 35.2),
    ("2 117 rpm", 2117.0),
    ("1.234,5", 1234.5),
    ("1,234.5", 1234.5),
    ("-3.5°C", -3.5),
    ("0 rpm", 0.0),
    ("2117rpm", 2117.0),
    ("N/A", None),
    ("", None),
    (None, None),
    ("n/a", None),
    (42, 42.0),
    (True, None),
])
def test_parse_float(raw, expected):
    assert kraken._parse_float(raw) == expected


# ═══════════════════════════════════════════════════════════════
# 5. Endpoint /api/kraken/status : propagation jusqu'au front
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def kraken_detected(monkeypatch):
    """Détection simulée d'un Kraken Z (sans SMBus ni liquidctl)."""
    monkeypatch.setattr(server, "kraken_available", lambda: True)
    monkeypatch.setattr(server, "kraken_detect", lambda: {
        "available": True,
        "devices": ["Device #0: NZXT Kraken Z (Z53, Z63 or Z73)",
                    "Device #1: ASUS Aura LED Controller"],
        "error": None,
    })


def test_endpoint_exposes_parsed_values(monkeypatch, kraken_detected):
    status_with(monkeypatch, ok_text(OUT_TREE_KRAKEN_Z))
    payload = TestClient(server.app).get("/api/kraken/status").json()
    assert payload["available"] is True and payload["detected"] is True
    assert len(payload["devices"]) == 2
    assert payload["status"] == {
        "liquid_temperature": 35.2, "fan_speed": 780.0, "pump_speed": 2117.0,
    }
    assert payload["status_ok"] is True
    assert payload["status_missing"] == []
    assert payload["error"] is None


def test_endpoint_exposes_missing_and_error(monkeypatch, kraken_detected):
    """Statut vide : le front reçoit de quoi afficher un diagnostic."""
    status_with(monkeypatch, FAIL_NO_DEVICE)
    payload = TestClient(server.app).get("/api/kraken/status").json()
    assert payload["detected"] is True
    assert payload["status"] == {}
    assert payload["status_ok"] is False
    assert payload["status_missing"] == ["liquid_temperature", "pump_speed", "fan_speed"]
    assert "no device matches" in payload["error"]


def test_endpoint_without_liquidctl(monkeypatch):
    monkeypatch.setattr(server, "kraken_available", lambda: False)
    payload = TestClient(server.app).get("/api/kraken/status").json()
    assert payload["available"] is False
    assert payload["detected"] is False
    assert payload["status"] == {} and payload["status_ok"] is False

#!/usr/bin/env python3
"""
ballistix/effects.py — Effets d'animation LED : rendu PUR, sans état.

Ce module contient l'unique source de vérité des effets disponibles
(registre ``EFFECTS``) et une fonction pure :

    render(effect_id, t, params, base_colors) -> List[List[int]]

``t`` est le temps LOCAL à l'effet en secondes (phase accumulée par le
moteur, déjà pondérée par la vitesse). Une même entrée produit toujours
la même sortie : aucune horloge, aucun RNG global, aucun accès matériel.
L'aléa éventuel passe par une graine injectable (``params["seed"]``) et
des générateurs locaux ``random.Random`` dérivés de cette graine.

Architecture 3 couches (rappel) :
  - ``base_colors`` : couleurs utilisateur, persistables, jamais écrasées ;
  - ``frame``       : couleurs transitoires calculées ici → matériel only ;
  - ``lighting``    : section persistée {mode, running, speed, …}.

L'incandescence se sert des ``base_colors`` comme teinte de base (si un
pixel est éteint, un braise chaude de repli est utilisée pour ne pas
rendre l'effet invisible).
"""

import math
import random
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Modes acceptés par le sous-système « lighting ». « static » n'est pas un
# effet animé : c'est le mode repos (couleurs de base, aucun moteur).
STATIC_MODE = "static"

# Couleur de repli d'un pixel éteint pour l'incandescence (braise chaude).
EMBER_FALLBACK: Tuple[int, int, int] = (255, 65, 0)

# Registre des effets — source unique exposée à l'API /api/animation/effects.
EFFECTS: Dict[str, Dict[str, Any]] = {
    "incandescence": {
        "label": "Incandescence",
        "cycle_param": "cycle_seconds",
        "params_spec": [
            {
                "id": "cycle_seconds",
                "label": "Durée de cycle (s)",
                "type": "float",
                "default": 2.0,
                "min": 0.2,
                "max": 30.0,
            },
            {
                "id": "min_brightness",
                "label": "Luminosité minimale",
                "type": "float",
                "default": 0.30,
                "min": 0.0,
                "max": 1.0,
            },
            {
                "id": "max_brightness",
                "label": "Luminosité maximale",
                "type": "float",
                "default": 1.0,
                "min": 0.0,
                "max": 1.0,
            },
            {
                "id": "sparkle",
                "label": "Scintillement",
                "type": "float",
                "default": 0.25,
                "min": 0.0,
                "max": 1.0,
            },
            {
                "id": "seed",
                "label": "Graine aléatoire",
                "type": "int",
                "default": 0,
                "min": 0,
                "max": 2147483647,
            },
        ],
    },
    "rainbow": {
        "label": "Rainbow",
        "cycle_param": "period_seconds",
        "params_spec": [
            {
                "id": "period_seconds",
                "label": "Période (s)",
                "type": "float",
                "default": 6.0,
                "min": 0.5,
                "max": 60.0,
            },
            {
                "id": "hue_spread",
                "label": "Étalement des teintes",
                "type": "float",
                "default": 1.0,
                "min": 0.0,
                "max": 2.0,
            },
        ],
    },
}

EFFECT_IDS: Tuple[str, ...] = tuple(EFFECTS)
MODE_IDS: Tuple[str, ...] = (STATIC_MODE,) + EFFECT_IDS


class UnknownEffectError(ValueError):
    """Effet/mode inconnu — l'API le traduit en HTTP 400."""

    def __init__(self, effect_id: Any):
        self.effect_id = effect_id
        available = ", ".join(MODE_IDS)
        super().__init__(f"Mode d'animation inconnu: {effect_id!r} (disponibles: {available})")


# ═══════════════════════════════════════════════════════════════
# Paramètres
# ═══════════════════════════════════════════════════════════════

def normalize_params(effect_id: str,
                     params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Valide et borne les paramètres d'un effet.

    - clés inconnues ignorées, clés absentes comblées par les défauts ;
    - valeurs non numériques → défaut ; valeurs hors bornes → bornées.

    Returns:
        Dictionnaire complet des paramètres de l'effet (mode « static » : {}).

    Raises:
        UnknownEffectError: si ``effect_id`` n'existe pas.
    """
    if effect_id == STATIC_MODE:
        return {}
    if effect_id not in EFFECTS:
        raise UnknownEffectError(effect_id)
    src = params if isinstance(params, dict) else {}
    out: Dict[str, Any] = {}
    for spec in EFFECTS[effect_id]["params_spec"]:
        key = spec["id"]
        raw = src.get(key, spec["default"])
        try:
            value = int(raw) if spec["type"] == "int" else float(raw)
        except (TypeError, ValueError):
            value = spec["default"]
        if not math.isfinite(value):
            value = spec["default"]
        if value < spec["min"]:
            value = spec["min"]
        elif value > spec["max"]:
            value = spec["max"]
        out[key] = value
    return out


def cycle_seconds(effect_id: str,
                  params: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """Durée d'un cycle complet de l'effet, en secondes de temps local.

    C'est une durée « à vitesse 1.0 » : le moteur la divise par la vitesse
    courante pour obtenir la durée réelle. ``None`` pour le mode static.
    """
    if effect_id == STATIC_MODE:
        return None
    if effect_id not in EFFECTS:
        raise UnknownEffectError(effect_id)
    normalized = normalize_params(effect_id, params)
    return float(normalized[EFFECTS[effect_id]["cycle_param"]])


def describe_effects() -> List[Dict[str, Any]]:
    """Description sérialisable des effets (source unique pour l'UI)."""
    return [
        {
            "id": effect_id,
            "label": spec["label"],
            "params": [dict(param) for param in spec["params_spec"]],
        }
        for effect_id, spec in EFFECTS.items()
    ]


# ═══════════════════════════════════════════════════════════════
# Rendu pur
# ═══════════════════════════════════════════════════════════════

def render(effect_id: str, t: float,
           params: Optional[Dict[str, Any]],
           base_colors: Sequence[Sequence[int]]) -> List[List[int]]:
    """Calcule la frame d'un effet à l'instant local ``t`` (fonction pure).

    Args:
        effect_id: identifiant de l'effet (``EFFECTS``) ou ``"static"``.
        t: temps local à l'effet en secondes (phase déjà pondérée vitesse).
        params: paramètres bruts (normalisés/ bornés ici).
        base_colors: couleurs utilisateur (couche base_colors), une par LED.

    Returns:
        Liste de couleurs ``[R, G, B]`` (0-255), une par LED d'entrée.

    Raises:
        UnknownEffectError: si ``effect_id`` n'existe pas.
    """
    if effect_id == STATIC_MODE:
        return [_clamp_color(color) for color in (base_colors or [])]
    if effect_id not in EFFECTS:
        raise UnknownEffectError(effect_id)
    normalized = normalize_params(effect_id, params)
    colors = list(base_colors) if base_colors else []
    if effect_id == "incandescence":
        return _render_incandescence(t, normalized, colors)
    return _render_rainbow(t, normalized, colors)


def _clamp_color(color: Sequence[int]) -> List[int]:
    return [max(0, min(255, int(channel))) for channel in color[:3]]


def _clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


# ── Incandescence : braise lente par pixel, teintée par base_colors ──

@lru_cache(maxsize=8192)
def _incandescence_profile(seed: int, index: int) -> Tuple[float, float, float, float, float]:
    """Constantes déterministes d'un pixel (périodes, phases, poids).

    Le cache évite de reconstruire un ``random.Random`` à chaque frame ;
    la valeur ne dépend que de (graine, index), donc le rendu reste pur.
    """
    rng = random.Random(f"incandescence:{seed}:{index}")
    fast_period = 1.0 + 0.5 * rng.random()   # période rapide, en cycles
    slow_period = 2.7 + 1.3 * rng.random()   # période lente, en cycles
    fast_phase = rng.random()
    slow_phase = rng.random()
    slow_weight = 0.55 + 0.35 * rng.random()
    return fast_period, slow_period, fast_phase, slow_phase, slow_weight


def _render_incandescence(t: float, params: Dict[str, Any],
                          base_colors: Sequence[Sequence[int]]) -> List[List[int]]:
    """Braise : chaque pixel respire avec deux ondes désynchronisées.

    - amplitude par défaut 30 % → 100 % de la teinte de base (bien visible) ;
    - périodes par pixel autour de ``cycle_seconds`` (aspect organique) ;
    - scintillement déterministe d'environ 12 Hz, pondéré par ``sparkle``.
    """
    cycle = max(1e-6, float(params["cycle_seconds"]))
    low = min(params["min_brightness"], params["max_brightness"])
    high = max(params["min_brightness"], params["max_brightness"])
    sparkle = float(params["sparkle"])
    seed = int(params["seed"])
    cycle_pos = t / cycle
    # Index de scintillement : 12 pas par seconde (sans stroboscope).
    sparkle_tick = int(t * 12.0)

    frame: List[List[int]] = []
    for index, color in enumerate(base_colors):
        base = (int(color[0]), int(color[1]), int(color[2]))
        if max(base) <= 0:
            base = EMBER_FALLBACK
        fast_period, slow_period, fast_phase, slow_phase, slow_weight = \
            _incandescence_profile(seed, index)
        fast = 0.5 + 0.5 * math.sin(
            2.0 * math.pi * (cycle_pos / fast_period + fast_phase))
        slow = 0.5 + 0.5 * math.sin(
            2.0 * math.pi * (cycle_pos / slow_period + slow_phase))
        mix = fast * (1.0 - slow_weight) + slow * slow_weight
        factor = low + (high - low) * mix
        if sparkle > 0.0:
            flicker_rng = random.Random(f"sparkle:{seed}:{index}:{sparkle_tick}")
            factor *= 1.0 - 0.5 * sparkle * flicker_rng.random()
        factor = max(0.0, min(1.0, factor))
        frame.append([
            _clamp_channel(base[0] * factor),
            _clamp_channel(base[1] * factor),
            _clamp_channel(base[2] * factor),
        ])
    return frame


# ── Rainbow : teintes qui défilent horizontalement (8 colonnes) ──

def _render_rainbow(t: float, params: Dict[str, Any],
                    base_colors: Sequence[Sequence[int]]) -> List[List[int]]:
    """Arc-en-ciel défilant : teinte de la colonne + décalage temporel.

    Contrairement à l'incandescence, le rainbow ne dépend pas des
    ``base_colors`` (il impose ses teintes saturées) ; la signature reste
    identique pour tous les effets.
    """
    period = max(1e-6, float(params["period_seconds"]))
    spread = float(params["hue_spread"])
    hue_offset = (t / period * 360.0) % 360.0
    columns = 8
    frame: List[List[int]] = []
    for index in range(len(base_colors)):
        column = index % columns
        hue = (column / columns * 360.0 * spread + hue_offset) % 360.0
        frame.append(_hsv_to_rgb(hue, 1.0, 1.0))
    return frame


def _hsv_to_rgb(hue: float, saturation: float, value: float) -> List[int]:
    """Conversion HSV → RGB (S/V dans [0,1], teinte en degrés)."""
    h = (hue % 360.0) / 60.0
    sector = int(h) % 6
    f = h - int(h)
    q = 1.0 - f
    t_val = f
    if sector == 0:
        r, g, b = 1.0, t_val, 0.0
    elif sector == 1:
        r, g, b = q, 1.0, 0.0
    elif sector == 2:
        r, g, b = 0.0, 1.0, t_val
    elif sector == 3:
        r, g, b = 0.0, q, 1.0
    elif sector == 4:
        r, g, b = t_val, 0.0, 1.0
    else:
        r, g, b = 1.0, 0.0, q
    return [
        _clamp_channel(r * saturation * value * 255.0),
        _clamp_channel(g * saturation * value * 255.0),
        _clamp_channel(b * saturation * value * 255.0),
    ]


__all__ = [
    "EFFECTS",
    "EFFECT_IDS",
    "MODE_IDS",
    "STATIC_MODE",
    "EMBER_FALLBACK",
    "UnknownEffectError",
    "normalize_params",
    "cycle_seconds",
    "describe_effects",
    "render",
]

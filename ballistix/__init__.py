#!/usr/bin/env python3
"""
ballistix — Package de contrôle des LEDs Crucial Ballistix via SMBus.

Protocole reverse-engineered depuis OpenRGB (CrucialController.cpp).
"""

__version__ = "2.0.0-dev"
__author__ = "Crucial Ballistix LED Tester"
__description__ = (
    "Contrôle des LEDs de RAM Crucial Ballistix via SMBus. "
    "Interface graphique Tkinter + CLI avec diagnostic avancé."
)

# Imports propres depuis les sous-modules
from .core import (
    CrucialStick,
    bswap16,
    ok, fail, warn, info, header,
    ANSI_RESET, ANSI_RED, ANSI_GREEN, ANSI_YELLOW, ANSI_BLUE,
    ANSI_MAGENTA, ANSI_CYAN, ANSI_BOLD, ANSI_DIM,
    SCAN_ADDRESSES, CRUCIAL_REG_DEVICE_VERSION,
    CRUCIAL_REG_DIRECT_R, CRUCIAL_REG_DIRECT_G, CRUCIAL_REG_DIRECT_B,
    NUM_LEDS,
)
from .detect import detect_sticks, get_smbus_buses, list_all_buses
from .diagnostics import run_full_diagnostic
from .cli import main

__all__ = [
    # Version
    "__version__",
    # Classes
    "CrucialStick",
    # Fonctions principales
    "main", "detect_sticks", "get_smbus_buses", "list_all_buses",
    "run_full_diagnostic",
    # Utilitaires
    "bswap16",
    "ok", "fail", "warn", "info", "header",
    "ANSI_RESET", "ANSI_RED", "ANSI_GREEN", "ANSI_YELLOW",
    "ANSI_BLUE", "ANSI_MAGENTA", "ANSI_CYAN", "ANSI_BOLD", "ANSI_DIM",
    # Constantes
    "SCAN_ADDRESSES", "CRUCIAL_REG_DEVICE_VERSION",
    "CRUCIAL_REG_DIRECT_R", "CRUCIAL_REG_DIRECT_G", "CRUCIAL_REG_DIRECT_B",
    "NUM_LEDS",
]

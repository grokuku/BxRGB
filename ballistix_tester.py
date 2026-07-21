#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║        CRUCIAL BALLISTIX LED TESTER                         ║
║        Contrôle des LEDs de RAM via SMBus                   ║
║                                                            ║
║  Protocole reverse-engineered depuis OpenRGB                ║
║  (CrucialController.cpp)                                    ║
╚══════════════════════════════════════════════════════════════╝

Wrapper de compatibilité — redirige vers le package ballistix.

Utilisation :
    sudo python3 ballistix_tester.py

Dépendances :
    pip install smbus2
    (Tkinter est inclus avec Python)
"""

import sys
import os

# S'assurer que /projects/RGB est dans le path pour les imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from ballistix.cli import main

main()

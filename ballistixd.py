#!/usr/bin/env python3
"""
ballistixd — Point d'entrée daemon pour le contrôleur Ballistix RGB.

Version allégée sans tkinter, sans auto-sudo (prévu pour tourner en root
via systemd). Sert uniquement le mode web.

Usage :
  sudo ./ballistixd --add-device 9:0x20
  sudo ./ballistixd --host 0.0.0.0 --port 8080 --add-device 9:0x20

PyInstaller :
  pyinstaller ballistixd.spec
  → dist/ballistixd --add-device 9:0x20
"""

import os
import sys
import argparse

# Ajouter le dossier parent au path pour que `ballistix` soit importable
# (utile en dev, ignoré en mode PyInstaller car déjà bundlé)
_project_dir = os.path.dirname(os.path.abspath(__file__))
if _project_dir not in sys.path:
    sys.path.insert(0, _project_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Ballistix RGB Daemon — Serveur web de contrôle des LEDs RAM",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="Hôte d'écoute (défaut: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port d'écoute (défaut: 8080)")
    parser.add_argument("--add-device", action="append",
                        help="Ajouter un device manuel: bus:0xadr (ex: 9:0x20)")
    parser.add_argument("--open-browser", action="store_true",
                        help="Ouvrir le navigateur au lancement")
    args = parser.parse_args()

    # Imports différés (plus rapides pour --help)
    from ballistix.server import run_server, smbus
    from ballistix.config import add_manual_device as persist_manual

    # Ajouter les devices manuels
    if args.add_device:
        manual_devices = []
        for dev_str in args.add_device:
            try:
                parts = dev_str.split(":")
                if len(parts) != 2:
                    print(f"  Format invalide: {dev_str} (attendu bus:0xadr)")
                    continue
                bus_num = int(parts[0])
                addr_str = parts[1]
                if addr_str.startswith("0x") or addr_str.startswith("0X"):
                    addr = int(addr_str, 16)
                else:
                    addr = int(addr_str)
                manual_devices.append({"bus": bus_num, "addr": addr})
            except Exception as e:
                print(f"  Erreur parsing {dev_str}: {e}")

        if manual_devices:
            print(f"  Transmission des devices manuels au serveur...")
            smbus.set_manual_devices(manual_devices)
            for dev in manual_devices:
                persist_manual(dev["bus"], dev["addr"])
                print(f"  → Manuel : i2c-{dev['bus']} @ 0x{dev['addr']:02X}")
            smbus.scan()

    run_server(host=args.host, port=args.port, open_browser=args.open_browser)


if __name__ == "__main__":
    main()
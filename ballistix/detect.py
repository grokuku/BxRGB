#!/usr/bin/env python3
"""
ballistix/detect.py — Détection automatique des barrettes Crucial Ballistix
sur les bus SMBus du chipset.
"""

import os
import sys
import glob
import subprocess
from typing import List, Tuple, Optional

from smbus2 import SMBus

from .core import (
    CrucialStick,
    bswap16,
    ok, fail, warn, info, header,
    _is_repeated_pattern,
    _check_micron_registers,
    _test_write_persistence,
    SCAN_ADDRESSES,
    CRUCIAL_REG_DEVICE_VERSION,
)


# ──────────────────────────────────────────────────────────────
# Parsing i2cdetect
# ──────────────────────────────────────────────────────────────

def _parse_i2cdetect(output: str) -> List[Tuple[int, str, str]]:
    """Parse la sortie de `i2cdetect -l`.

    Format attendu (tab-separated) :
        i2c-N\ttype\tname\t...

    Returns:
        Liste de tuples (bus_num, type, name).
    """
    buses = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        # parts[0] = "i2c-N", parts[1] = type, parts[2] = name
        bus_id = parts[0]
        if not bus_id.startswith("i2c-"):
            continue
        try:
            bus_num = int(bus_id.split("-")[1])
        except (IndexError, ValueError):
            continue
        bus_type = parts[1].strip().lower()
        bus_name = parts[2].strip()
        buses.append((bus_num, bus_type, bus_name))
    return buses


def _get_buses_via_i2cdetect() -> Optional[List[Tuple[int, str, str]]]:
    """Essaie d'obtenir la liste des bus via `i2cdetect -l`.

    Returns:
        Liste de tuples (bus_num, type, name) ou None si la commande échoue.
    """
    try:
        result = subprocess.run(
            ["i2cdetect", "-l"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_i2cdetect(result.stdout)
        return None
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return None


# ──────────────────────────────────────────────────────────────
# Liste des bus SMBus
# ──────────────────────────────────────────────────────────────

def get_smbus_buses() -> List[Tuple[int, str]]:
    """Retourne la liste des (bus_num, name) pour les vrais bus SMBus uniquement.

    Utilise d'abord `i2cdetect -l` (plus fiable), puis lit
    /sys/class/i2c-adapter/i2c-N/name en fallback.

    Ne garde que les bus de type 'smbus' ou dont le nom contient
    'piix4', 'i801', 'fch', 'amd_smbus'.
    Les bus GPU (AMDGPU, NVIDIA, etc.) sont exclus.

    Returns:
        Liste de tuples (bus_num, name) triés par numéro de bus.
    """
    # Essayer i2cdetect d'abord
    parsed = _get_buses_via_i2cdetect()
    if parsed is not None:
        buses = []
        for bus_num, bus_type, bus_name in parsed:
            name_lower = bus_name.lower()
            # Garder si type == "smbus" OU nom évocateur
            if (bus_type == "smbus" or
                "piix4" in name_lower or
                "i801" in name_lower or
                "fch" in name_lower or
                "amd_smbus" in name_lower):
                # Exclure les bus GPU
                if "amdgpu" not in name_lower and "gpu" not in name_lower:
                    buses.append((bus_num, bus_name))
        return buses

    # Fallback : méthode sysfs
    buses = []
    for dev_path in sorted(glob.glob("/dev/i2c-*")):
        bus_num = int(dev_path.split("-")[1])
        name_path = f"/sys/class/i2c-adapter/i2c-{bus_num}/name"
        try:
            with open(name_path) as f:
                name = f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        name_lower = name.lower()
        if ("smbus" in name_lower or
            "piix4" in name_lower or
            "i801" in name_lower or
            "amd" in name_lower or
            "fch" in name_lower):
            if "amdgpu" not in name_lower and "gpu" not in name_lower:
                buses.append((bus_num, name))
    return buses


def list_all_buses() -> List[Tuple[int, str]]:
    """Retourne la liste de tous les bus I2C disponibles avec leurs noms.

    Utilise d'abord `i2cdetect -l` (plus fiable), puis lit
    /sys/class/i2c-adapter/i2c-N/name en fallback.

    Returns:
        Liste de tuples (bus_num, name) triés par numéro de bus.
    """
    # Essayer i2cdetect d'abord
    parsed = _get_buses_via_i2cdetect()
    if parsed is not None:
        return [(num, name) for num, _type, name in parsed]

    # Fallback : méthode sysfs
    buses = []
    for dev_path in sorted(glob.glob("/dev/i2c-*")):
        bus_num = int(dev_path.split("-")[1])
        name_path = f"/sys/class/i2c-adapter/i2c-{bus_num}/name"
        try:
            with open(name_path) as f:
                name = f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            name = "inconnu"
        buses.append((bus_num, name))
    return buses


# ──────────────────────────────────────────────────────────────
# Détection automatique des barrettes
# ──────────────────────────────────────────────────────────────

def _probe_device(bus: SMBus, addr: int) -> Optional[str]:
    """Vérifie si un device Crucial Ballistix RGB répond à l'adresse donnée.

    Tente de lire les 16 octets de version à partir de 0x1000.
    Retourne la chaîne de version si valide (vrai contrôleur RGB), None sinon.

    Validation stricte (7 étapes) :
    1. Lecture brute des 16 octets de version
    2. Rejet des patterns répétés (≤ 3 caractères uniques)
    3. Rejet des données SPD typiques (trop d'octets ≤ 0x0F, aucune lettre)
    4. Construction de la chaîne de version
    5. Vérification du marqueur 'Micron' dans les registres (0x1025/0x1030)
    6. Test d'écriture/lecture avec valeur différente (persistence check)
    7. Vérification que la chaîne a une longueur minimale
    """
    try:
        # ── 1. Lire les octets bruts ────────────────────────────
        raw_bytes: List[int] = []
        for i in range(16):
            bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
            v = bus.read_byte_data(addr, 0x81)
            raw_bytes.append(v)

        # ── 2. Rejeter les patterns répétés (nnnnnn..., xxxx..., \xff\xff...) ──
        if _is_repeated_pattern(raw_bytes):
            return None

        # ── 3. Analyser les octets bruts ─────────────────────────
        printable_chars = [chr(v) for v in raw_bytes if 0x20 <= v <= 0x7E]
        printable_count = len(printable_chars)
        letter_count = sum(1 for c in printable_chars if c.isalpha())

        non_zero = [v for v in raw_bytes if v != 0]
        if not non_zero:
            return None  # Tous nuls → pas de device

        small_bytes = sum(1 for v in non_zero if v <= 0x0F)

        # Rejeter si plus de la moitié des octets non-nuls sont ≤ 0x0F (SPD typique)
        if (small_bytes / len(non_zero)) > 0.5:
            return None

        # Rejeter si aucune lettre (majuscule ou minuscule)
        if letter_count == 0:
            return None

        # Rejeter si moins de 4 vrais caractères imprimables
        if printable_count < 4:
            return None

        # ── 4. Construire la chaîne de version pour l'affichage ──
        chars: List[str] = []
        for v in raw_bytes:
            if 0x20 <= v <= 0x7E:
                chars.append(chr(v))
            elif v == 0:
                break
            else:
                chars.append(f"\\x{v:02x}")

        ver = "".join(chars).rstrip("\\x00? \x00")
        ver = ver.strip()

        if not ver or len(ver) < 2:
            return None

        # ── 5. Vérification du marqueur 'Micron' (0x1025/0x1030) ──
        # Les vrais contrôleurs Crucial Ballistix contiennent "Micron"
        # (nom du fabricant des puces DRAM) dans leurs registres internes.
        # Si absent → REJETER (faux positif probable).
        if not _check_micron_registers(bus, addr):
            return None  # Marqueur Micron absent → pas un vrai Ballistix

        # ── 6. Test d'écriture/lecture renforcé ──────────────────
        # Écrit une valeur DIFFÉRENTE, vérifie la persistance, restaure l'originale
        if not _test_write_persistence(bus, addr):
            return None  # Device read-only (SPD ou faux positif)

        return ver

    except OSError:
        return None
    except Exception:
        return None


def detect_sticks(wide_scan: bool = False) -> List[CrucialStick]:
    """Scanne les vrais bus SMBus (chipset) pour trouver des barrettes Crucial.

    Utilise get_smbus_buses() pour ne scanner que les bus du chipset
    (PIIX4, i801, etc.) en ignorant les bus GPU (AMDGPU, NVIDIA).
    Les sticks détectés sont retournés avec leur bus SMBus ouvert.

    Args:
        wide_scan: Si True, scanne aussi les adresses hors plage standard
                   (0x08-0x77) en utilisant write_quick() pour détecter
                   les devices présents, dans le cadre d'un diagnostic.

    Returns:
        Liste des sticks valides (vrais contrôleurs RGB).
        Les statistiques de rejet sont stockées dans detect_sticks.rejected
        et detect_sticks.total_attempts pour affichage résumé.
    """
    # Initialiser les compteurs de rejet
    detect_sticks.rejected = 0
    detect_sticks.total_attempts = 0
    detect_sticks.spd_count = 0
    detect_sticks.pattern_count = 0

    sticks: List[CrucialStick] = []
    opened_buses: List[SMBus] = []

    # ── Étape 1 : lister tous les bus I2C + filtrer SMBus ────
    all_buses = list_all_buses()
    if not all_buses:
        print("✗ Aucun bus I2C trouvé.")
        print("  → Chargez le module : sudo modprobe i2c-dev")
        print("  → Vérifiez que votre matériel SMBus est présent")
        return sticks

    # Afficher tous les bus I2C
    all_names = [f"i2c-{n}" for n, _ in all_buses]
    print(f"🔍 Bus I2C trouvés : {', '.join(all_names)}")
    for bus_num, name in all_buses:
        print(f"     i2c-{bus_num}: {name}")

    # Filtrer pour ne garder que les vrais bus SMBus (chipset)
    smbus_buses = get_smbus_buses()

    if smbus_buses:
        smbus_nums = [n for n, _ in smbus_buses]
        ignored = [n for n, _ in all_buses if n not in smbus_nums]
        print(f"🔍 Bus SMBus (chipset) : {', '.join(f'i2c-{n}' for n in smbus_nums)}")
        if ignored:
            print(f"   → Bus ignorés (GPU/autres) : i2c-{', i2c-'.join(str(n) for n in ignored)}")
    else:
        # Fallback : si aucun bus SMBus détecté par nom, utiliser tous les bus
        print(f"  {warn('Aucun bus SMBus (chipset) détecté par nom.')}")
        print(f"  {warn('Scan de tous les bus I2C disponibles...')}")
        smbus_buses = all_buses

    print()

    # ── Étape 2 : scanner chaque bus SMBus ───────────────────
    for bus_num, bus_name in smbus_buses:
        bus: Optional[SMBus] = None

        try:
            bus = SMBus(bus_num)
            opened_buses.append(bus)
        except PermissionError:
            print(f"  ⚠ i2c-{bus_num} ({bus_name}) : permission refusée")
            print("    → Relancez avec sudo ou ajoutez l'utilisateur au groupe i2c")
            continue
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"  ⚠ i2c-{bus_num} ({bus_name}) : {e}")
            continue

        found_on_this_bus = False

        # Essayer chaque adresse de la plage
        for addr in SCAN_ADDRESSES:
            # Vérifier d'abord si un device répond (write_quick)
            try:
                bus.write_quick(addr)
            except OSError:
                continue  # Aucun device à cette adresse
            except Exception:
                continue

            # Device répond → tenter la validation
            detect_sticks.total_attempts += 1
            version = _probe_device(bus, addr)
            if version is not None:
                stick_label = f"i2c-{bus_num} ({bus_name}) @ 0x{addr:02X}"
                print(f"  ✓ Trouvée : {stick_label} → {version}")
                stick = CrucialStick(bus, addr, bus_num, version, bus_name)
                sticks.append(stick)
                found_on_this_bus = True
                # Ne pas break : il pourrait y avoir plusieurs sticks
                # sur le même bus à des adresses différentes
            else:
                detect_sticks.rejected += 1

        # ── Fallback wide scan (si activé et normal scan n'a rien trouvé sur ce bus) ──
        if wide_scan and not found_on_this_bus:
            # Scanner toutes les adresses 0x08-0x77 avec write_quick()
            print(f"  ℹ  Scan large i2c-{bus_num} ({bus_name}) (adresses 0x08-0x77)...")
            found_devices: List[int] = []
            for addr in range(0x08, 0x78):
                if addr in SCAN_ADDRESSES:
                    continue  # Déjà testé
                try:
                    bus.write_quick(addr)
                    found_devices.append(addr)
                except OSError:
                    pass
                except Exception:
                    pass

            if found_devices:
                print(f"     Devices supplémentaires trouvés : {', '.join(f'0x{a:02X}' for a in found_devices)}")
                # Tenter de lire la version pour chaque device trouvé (déjà confirmé par write_quick)
                for addr in found_devices:
                    detect_sticks.total_attempts += 1
                    version = _probe_device(bus, addr)
                    if version is not None:
                        stick_label = f"i2c-{bus_num} ({bus_name}) @ 0x{addr:02X}"
                        print(f"  ✓ Trouvée (wide) : {stick_label} → {version}")
                        stick = CrucialStick(bus, addr, bus_num, version, bus_name)
                        sticks.append(stick)
                    else:
                        detect_sticks.rejected += 1
            else:
                print(f"     Aucun device supplémentaire trouvé.")

    # ── Étape 3 : fermer les bus sans stick ───────────────────
    # On garde ouverts les bus qui ont au moins un stick
    for b in opened_buses:
        if not any(s.bus is b for s in sticks):
            try:
                b.close()
            except Exception:
                pass

    return sticks


__all__ = [
    "_parse_i2cdetect", "_get_buses_via_i2cdetect",
    "get_smbus_buses", "list_all_buses",
    "_probe_device",
    "detect_sticks",
]

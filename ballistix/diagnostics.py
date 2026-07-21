#!/usr/bin/env python3
"""
ballistix/diagnostics.py — Diagnostics, tests et rapports pour
le contrôleur Crucial Ballistix RGB.
"""

import os
import sys
import glob
import time
import datetime
import subprocess
import shutil
from typing import List, Tuple, Optional

from .core import (
    CrucialStick,
    bswap16,
    _is_repeated_pattern,
    ok, fail, warn, info, header,
    ANSI_RESET, ANSI_RED, ANSI_GREEN, ANSI_YELLOW, ANSI_BLUE,
    ANSI_MAGENTA, ANSI_CYAN, ANSI_BOLD, ANSI_DIM,
    SCAN_ADDRESSES, DIAG_ADDRESS_RANGES,
    CRUCIAL_REG_DEVICE_VERSION, CRUCIAL_REG_DIRECT_R,
    CRUCIAL_REG_DIRECT_G, CRUCIAL_REG_DIRECT_B,
    CRUCIAL_REG_BRIGHTNESS_VAL, CRUCIAL_REG_BRIGHTNESS_END,
)

from smbus2 import SMBus

from .detect import (
    get_smbus_buses, list_all_buses,
    detect_sticks,
)
from .core import (
    _check_micron_registers, _test_write_persistence,
)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _run_cmd(cmd: str) -> Tuple[int, str, str]:
    """Exécute une commande shell et retourne (code, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=10, text=True)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"
    except Exception as e:
        return -1, "", str(e)


# ── 1. Diagnostic système ─────────────────────────────────────

def diag_system() -> str:
    """Vérifie l'environnement système pour le fonctionnement SMBus."""
    lines: List[str] = []
    lines.append(header("\n═══ 1. DIAGNOSTIC SYSTÈME ═══"))
    lines.append("")

    # Python version
    lines.append(f"  Python : {sys.version.split()[0]}")
    lines.append(f"  OS     : {sys.platform}")
    try:
        import platform
        lines.append(f"  Distro : {platform.platform()}")
    except Exception:
        pass

    # smbus2 version
    try:
        import smbus2
        ver = getattr(smbus2, "__version__", "inconnu")
        lines.append(f"  smbus2 : {ver}")
    except Exception:
        lines.append(f"  {fail('smbus2 : non installé')}")

    lines.append("")

    # i2c-dev module
    code, out, err = _run_cmd("lsmod | grep i2c_dev")
    if out:
        parts = out.split()
        lines.append(f"  {ok('i2c-dev chargé')} ({parts[0]} {parts[1]} {parts[2]} {parts[3] if len(parts) > 3 else ''})")
    else:
        lines.append(f"  {fail('i2c-dev NON chargé')}")
        lines.append(f"    → Chargez-le : {ANSI_BOLD}sudo modprobe i2c-dev{ANSI_RESET}")

    # i2cdetect availability
    i2cdetect_path = shutil.which("i2cdetect")
    if i2cdetect_path:
        code, out, err = _run_cmd("i2cdetect --version 2>&1 | head -1")
        ver_str = out.split()[-1] if out else "présent"
        lines.append(f"  {ok(f'i2cdetect disponible ({ver_str})')}")
        lines.append(f"    Chemin : {i2cdetect_path}")
    else:
        lines.append(f"  {fail('i2cdetect NON disponible')}")
        lines.append(f"    → Installez : {ANSI_BOLD}sudo apt install i2c-tools{ANSI_RESET}")

    # i2cdetect -l
    code, out, err = _run_cmd("i2cdetect -l 2>&1")
    if out:
        lines.append(f"")
        lines.append(f"  Bus I2C détectés par i2cdetect:")
        for line in out.split("\n"):
            lines.append(f"    {line}")
    else:
        lines.append(f"  {warn('i2cdetect -l : aucun bus ou erreur')}")

    # Vérification /dev/i2c-* avec noms détaillés
    all_buses = list_all_buses()
    lines.append("")
    if all_buses:
        lines.append(f"  {ok(f'{len(all_buses)} périphérique(s) I2C trouvé(s)')}")
        for bus_num, name in all_buses:
            lines.append(f"    i2c-{bus_num} → {name}")
        # Distinguer SMBus vs GPU
        smbus = get_smbus_buses()
        if smbus:
            lines.append(f"")
            lines.append(f"  {ok(f'{len(smbus)} bus SMBus (chipset) détecté(s)')}")
            for bus_num, name in smbus:
                lines.append(f"    i2c-{bus_num} → {name}")
            gpu_buses = [(n, nm) for n, nm in all_buses if n not in [b[0] for b in smbus]]
            if gpu_buses:
                lines.append(f"  {info(f'{len(gpu_buses)} bus GPU/autres ignorés')}")
                for bus_num, name in gpu_buses:
                    lines.append(f"    i2c-{bus_num} → {name}")
        else:
            lines.append(f"  {warn('Aucun bus SMBus (chipset) détecté par nom')}")
            lines.append(f"  {warn('Scan complet de tous les bus activé par défaut')}")
    else:
        lines.append(f"  {fail('Aucun /dev/i2c-* trouvé')}")

    # Permissions
    euid = os.geteuid()
    if euid == 0:
        lines.append(f"  {ok('Exécution en tant que root (sudo)')}")
    else:
        lines.append(f"  {warn('Exécution sans root')}")
        # Vérifier le groupe i2c
        import grp
        try:
            i2c_group = grp.getgrnam("i2c").gr_gid
            user_groups = os.getgroups()
            if i2c_group in user_groups:
                lines.append(f"  {ok('Utilisateur dans le groupe i2c')}")
            else:
                lines.append(f"  {warn('Utilisateur PAS dans le groupe i2c')}")
                lines.append(f"    → Ajoutez : {ANSI_BOLD}sudo usermod -aG i2c $USER && newgrp i2c{ANSI_RESET}")
        except KeyError:
            lines.append(f"  {warn('Groupe i2c inexistant sur ce système')}")
            lines.append(f"    → Créez-le : {ANSI_BOLD}sudo groupadd i2c && sudo usermod -aG i2c $USER{ANSI_RESET}")

    # Vérification SMBus dans le BIOS
    lines.append("")
    lines.append(f"  {info('Note : Le contrôleur SMBus doit être activé dans le BIOS.')}")
    lines.append(f"  {info('Sur certaines cartes mères, il est désactivé par défaut.')}")

    # Suggestion i2cdetect
    lines.append("")
    i2cdetect_path = shutil.which("i2cdetect")
    if i2cdetect_path:
        lines.append(f"  {info('Conseil : exécutez i2cdetect sur les BUS SMBus (chipset) uniquement :')}")
        smbus_buses = get_smbus_buses()
        if smbus_buses:
            for bus_num, name in smbus_buses:
                lines.append(f"    sudo i2cdetect -y {bus_num}  ({name})")
        else:
            for dev in sorted(glob.glob("/dev/i2c-*")):
                bus_n = dev.split("-")[1]
                lines.append(f"    sudo i2cdetect -y {bus_n}")
        lines.append(f"")
        lines.append(f"  {info('Sur les chipsets Intel/AMD, le contrôleur DRAM RGB est souvent')}")
        lines.append(f"  {info('sur les bus SMBus du chipset (PIIX4, i801) aux adresses 0x30-0x37.')}")
        lines.append(f"  {info('Les adresses 0x50-0x57 sont les SPD EEPROM (faux positifs).')}")

    return "\n".join(lines)


# ── 1b. Chipset SMBus ─────────────────────────────────────────

def diag_smbus_chipset() -> str:
    """Vérifie quels chipsets SMBus sont présents et les drivers chargés.

    Détecte Intel (i801/i915), AMD (piix4/amd_sfh), ASMedia, NVIDIA.
    """
    lines: List[str] = []
    lines.append(header("\n═══ 1b. CHIPSET SMBus ═══"))
    lines.append("")

    # lsmod | grep -i i2c
    code, out, err = _run_cmd("lsmod | grep -i i2c")
    lines.append(f"  {header('Modules I2C chargés (lsmod | grep i2c)')}")
    if out:
        for line in out.split("\n"):
            parts = line.split()
            if parts:
                lines.append(f"    {parts[0]:20} {parts[1]:>6} {parts[2]:>6}")
    else:
        lines.append(f"    {fail('Aucun module i2c chargé')}")
    lines.append("")

    # lsmod | grep -i smbus
    code, out, err = _run_cmd("lsmod | grep -i smbus")
    lines.append(f"  {header('Modules SMBus chargés (lsmod | grep smbus)')}")
    if out:
        for line in out.split("\n"):
            parts = line.split()
            if parts:
                lines.append(f"    {parts[0]:20} {parts[1]:>6} {parts[2]:>6}")
    else:
        lines.append(f"    {warn('Aucun module smbus spécifique chargé')}")
        lines.append(f"    {info('Essayez : sudo modprobe i2c-i801 (Intel) ou i2c-piix4 (AMD)')}")
    lines.append("")

    # lspci | grep -i smbus
    code, out, err = _run_cmd("lspci | grep -i 'smbus\\|i2c' 2>/dev/null || echo 'lspci non disponible'")
    lines.append(f"  {header('Contrôleurs SMBus/I2C (lspci)')}")
    if out and "non disponible" not in out:
        chipset_type = "Inconnu"
        for line in out.split("\n"):
            lines.append(f"    {line}")
            line_lower = line.lower()
            if "intel" in line_lower or "i801" in line_lower:
                chipset_type = "Intel SMBus (i801)"
            elif "amd" in line_lower or "piix4" in line_lower:
                chipset_type = "AMD SMBus (piix4)"
            elif "nvidia" in line_lower:
                chipset_type = "NVIDIA SMBus"
            elif "asmedia" in line_lower:
                chipset_type = "ASMedia SMBus"
        lines.append(f"")
        lines.append(f"  {header(f'→ Chipset détecté : {chipset_type}')}")
    else:
        lines.append(f"    {fail('Aucun contrôleur SMBus détecté')}")
        lines.append(f"    {info('Le SMBus est peut-être désactivé dans le BIOS')}")
        if "non disponible" in out:
            lines.append(f"    {info('Installez pciutils : sudo apt install pciutils')}")
    lines.append("")

    # Suggestions selon chipset
    lines.append(f"  {info('Suggestions :')}")
    if out and "non disponible" not in out:
        out_lower = out.lower()
        if "intel" in out_lower or "i801" in out_lower:
            lines.append(f"    Intel : sudo modprobe i2c-i801")
            lines.append(f"    Intel PCH : le SMBus est souvent sur i2c-0")
        if "amd" in out_lower:
            lines.append(f"    AMD : sudo modprobe i2c-piix4")
            lines.append(f"    AMD : sudo modprobe amd_sfh (pour AMD SFH)")
            lines.append(f"    AMD Zen : le SMBus est intégré au chipset")
        if "nvidia" in out_lower:
            lines.append(f"    NVIDIA : sudo modprobe nvidia-i2c")
    else:
        lines.append(f"    Essayez : sudo modprobe i2c-i801 (Intel) ou i2c-piix4 (AMD)")

    return "\n".join(lines)


# ── 1c. i2cdetect sur tous les bus ────────────────────────────

def diag_i2cdetect_all() -> str:
    """Exécute i2cdetect sur tous les bus SMBus (chipset) et analyse les résultats."""
    lines: List[str] = []
    lines.append(header("\n═══ 1c. i2cdetect SUR LES BUS SMBus ═══"))
    lines.append("")

    i2cdetect_path = shutil.which("i2cdetect")
    if not i2cdetect_path:
        lines.append(f"  {fail('i2cdetect non installé')}")
        lines.append(f"    → Installez : sudo apt install i2c-tools")
        return "\n".join(lines)

    smbus_buses = get_smbus_buses()
    if not smbus_buses:
        lines.append(f"  {fail('Aucun bus SMBus (chipset) trouvé')}")
        lines.append(f"  {warn('Scan de tous les bus I2C en fallback...')}")
        all_buses = list_all_buses()
        if not all_buses:
            return "\n".join(lines)
        smbus_buses = all_buses

    import re

    for bus_num, bus_name in smbus_buses:
        lines.append(f"  {header(f'i2cdetect -y {bus_num} ({bus_name})')}")
        lines.append("")

        code, out, err = _run_cmd(f"i2cdetect -y {bus_num} 2>&1")
        if err and "Permission denied" in err:
            lines.append(f"    {fail('Permission refusée')}")
            lines.append("    → Exécutez avec sudo ou ajoutez l'utilisateur au groupe i2c")
            lines.append("")
            continue
        if code != 0 and not out:
            err_msg = err if err else "code non nul"
            lines.append(f"    {fail(f'Erreur : {err_msg}')}")
            lines.append("")
            continue

        if not out:
            lines.append(f"    {warn('Aucune sortie (bus vide ou erreur)')}")
            lines.append("")
            continue

        # Analyser la sortie pour trouver les adresses qui répondent
        found_addresses: List[int] = []
        for line in out.split("\n"):
            # Format i2cdetect: lignes "00: -- -- ..." ou "10: XX XX ..."
            # Les adresses sont sur 2 chiffres hex, les -- signifient vide, UU = occupé
            match = re.match(r'^\s*([0-9a-fA-F]0):\s+(.*)$', line)
            if not match:
                continue
            row_prefix = int(match.group(1), 16)
            cells = match.group(2).split()
            for col_idx, cell in enumerate(cells):
                if cell not in ("--", ""):
                    addr = row_prefix + col_idx
                    if addr >= 0x08:
                        found_addresses.append(addr)

        if found_addresses:
            addr_groups: dict = {
                "RGB DRAM (0x20-0x3F)": [],
                "SPD EEPROM (0x50-0x57)": [],
                "Autres": [],
            }
            for addr in found_addresses:
                if 0x20 <= addr <= 0x3F:
                    addr_groups["RGB DRAM (0x20-0x3F)"].append(f"0x{addr:02X}")
                elif 0x50 <= addr <= 0x57:
                    addr_groups["SPD EEPROM (0x50-0x57)"].append(f"0x{addr:02X}")
                else:
                    addr_groups["Autres"].append(f"0x{addr:02X}")

            for category, addrs in addr_groups.items():
                if addrs:
                    icon = " ✅" if "RGB DRAM" in category else ""
                    lines.append(f"      {category}: {', '.join(addrs)}{icon}")

            lines.append("")
            lines.append(f"    Sortie brute :")
            for line in out.split("\n"):
                lines.append(f"      {line}")
        else:
            lines.append(f"    {warn('Aucune adresse ne répond sur ce bus')}")

        lines.append("")

    return "\n".join(lines)


# ── 1d. Scan exhaustif SMBus direct ───────────────────────────

def diag_try_all_buses() -> str:
    """Pour chaque bus SMBus (chipset), tente write_quick() sur chaque adresse 0x08-0x77
    puis lit le registre de version (0x1000).

    Affiche un tableau complet : Bus | Adr | Répond | Version | Type.
    Ignore les bus GPU (AMDGPU, NVIDIA).
    """
    lines: List[str] = []
    lines.append(header("\n═══ 1d. SCAN EXHAUSTIF SMBus DIRECT ═══"))
    lines.append("")

    smbus_buses = get_smbus_buses()
    if not smbus_buses:
        lines.append(f"  {fail('Aucun bus SMBus (chipset) trouvé')}")
        lines.append(f"  {warn('Scan de tous les bus I2C en fallback...')}")
        all_buses = list_all_buses()
        if not all_buses:
            return "\n".join(lines)
        smbus_buses = all_buses

    lines.append(f"  Bus SMBus scannés : {', '.join(f'i2c-{n} ({nm})' for n, nm in smbus_buses)}")
    lines.append("")

    # En-tête du tableau
    sep_line = f"  {'─'*8}┼{'─'*6}┼{'─'*8}┼{'─'*22}┼{'─'*18}"
    hdr = f"  {'Bus':<8}│{'Adr':<6}│{'Répond':<8}│{'FW Version':<22}│{'Type':<18}"
    lines.append(f"  {ANSI_BOLD}{hdr}{ANSI_RESET}")
    lines.append(sep_line)

    found_any = False
    rgb_devices: List[str] = []

    for bus_num, bus_name in smbus_buses:
        try:
            bus = SMBus(bus_num)
        except PermissionError:
            continue
        except Exception:
            continue

        for addr in range(0x08, 0x78):
            responds = False
            try:
                bus.write_quick(addr)
                responds = True
                found_any = True
            except OSError:
                continue
            except Exception:
                continue

            if not responds:
                continue

            # Device répond → tenter de lire la version
            version = ""
            device_type = ""
            try:
                raw_bytes: List[int] = []
                for i in range(16):
                    try:
                        bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
                        v = bus.read_byte_data(addr, 0x81)
                        raw_bytes.append(v)
                    except OSError:
                        break

                if raw_bytes:
                    is_repeated = _is_repeated_pattern(raw_bytes)
                    printable = [chr(v) for v in raw_bytes if 0x20 <= v <= 0x7E]
                    letters = sum(1 for c in printable if c.isalpha())
                    non_zero = [v for v in raw_bytes if v != 0]
                    small = sum(1 for v in non_zero if v <= 0x0F) if non_zero else 0

                    # Construire la chaîne de version
                    chars: List[str] = []
                    for v in raw_bytes:
                        if 0x20 <= v <= 0x7E:
                            chars.append(chr(v))
                        elif v == 0:
                            break
                        else:
                            chars.append(f"\\x{v:02x}")
                    version = "".join(chars).strip()[:20]

                    if is_repeated:
                        device_type = "Pattern répété (faux positif)"
                    elif non_zero and (small / len(non_zero)) > 0.5 and letters == 0:
                        device_type = "SPD EEPROM"
                    elif letters >= 2 and len(printable) >= 4:
                        device_type = "RGB DRAM ✅"
                        # Tentative de test Micron + écriture
                        try:
                            if _check_micron_registers(bus, addr) and _test_write_persistence(bus, addr):
                                rgb_devices.append(f"i2c-{bus_num} @ 0x{addr:02X} [{version}]")
                        except Exception:
                            pass
                    elif any(0x20 <= v <= 0x7E for v in raw_bytes):
                        device_type = "Device inconnu"
                    else:
                        device_type = "Silencieux"
                else:
                    device_type = "Présent (pas version)"
            except Exception:
                device_type = "Présent (lecture impossible)"

            bus_str = f"i2c-{bus_num}"
            addr_str = f"0x{addr:02X}"
            resp_str = ok("Oui")

            lines.append(f"  {bus_str:<8}│{addr_str:<6}│{resp_str:<8}│{version:<22}│{device_type:<18}")

        bus.close()

    if not found_any:
        lines.append(f"  {fail('Aucun device répondant trouvé sur aucun bus')}")
    else:
        lines.append(sep_line)
        lines.append("")
        if rgb_devices:
            lines.append(f"  {ok(f'{len(rgb_devices)} contrôleur(s) RGB potentiel(s) trouvé(s)')}")
            for dev in rgb_devices:
                lines.append(f"    • {dev}")
        else:
            lines.append(f"  {warn('Aucun contrôleur RGB évident trouvé')}")
            lines.append(f"  {info('Les devices présents sont principalement des SPD EEPROM.')}")
            lines.append(f"  {info('Le contrôleur RGB Ballistix peut être sur un bus multiplexé.')}")

    return "\n".join(lines)


# ── 1e. Protocole Aura SMBus (ASUS) ───────────────────────────

def diag_try_aura_smbus() -> str:
    """Tente le protocole Aura SMBus (ASUS) sur les bus SMBus (chipset).

    Les RAM Ballistix utilisent parfois le protocole Aura SMBus d'ASUS :
    écrire bswap16(0x1000) sur cmd 0x00, lire 16 bytes sur cmd 0x81.
    C'est le même que Crucial, mais à des adresses différentes.

    Test spécifique ASUS X570 : i2c-9 (PIIX4) @ 0x30.
    """
    lines: List[str] = []
    lines.append(header("\n═══ 1e. PROTOCOLE AURA SMBus (ASUS) ═══"))
    lines.append("")

    aura_addresses = [0x27, 0x37, 0x3A, 0x3B, 0x2A, 0x2B, 0x38, 0x39]
    # Adresses supplémentaires pour ASUS X570 (0x30 = contrôleur RGB DRAM probable)
    asus_x570_addresses = [0x30, 0x34, 0x35, 0x36, 0x37]
    all_aura_addrs = sorted(set(aura_addresses + asus_x570_addresses))

    smbus_buses = get_smbus_buses()
    if not smbus_buses:
        lines.append(f"  {fail('Aucun bus SMBus (chipset) trouvé')}")
        lines.append(f"  {warn('Scan de tous les bus I2C en fallback...')}")
        all_buses = list_all_buses()
        if not all_buses:
            return "\n".join(lines)
        smbus_buses = all_buses

    lines.append(f"  Adresses testées : {', '.join(f'0x{a:02X}' for a in all_aura_addrs)}")
    lines.append(f"  Bus SMBus scannés : {', '.join(f'i2c-{n} ({nm})' for n, nm in smbus_buses)}")
    lines.append(f"  {info('ASUS X570 : le contrôleur RGB DRAM est souvent sur i2c-9 (PIIX4) @ 0x30')}")
    lines.append("")

    def _try_probe(bus, addr):
        """Tente de lire la version à une adresse donnée.
        Retourne (version, True) si valide, ("", False) sinon.
        Utilise le même protocole que Crucial (0x1000).
        """
        try:
            raw_bytes = []
            for i in range(16):
                bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
                v = bus.read_byte_data(addr, 0x81)
                raw_bytes.append(v)
        except Exception:
            return "", False

        if not raw_bytes:
            return "", False

        is_repeated = _is_repeated_pattern(raw_bytes)
        printable = [chr(v) for v in raw_bytes if 0x20 <= v <= 0x7E]
        letters = sum(1 for c in printable if c.isalpha())
        non_zero = [v for v in raw_bytes if v != 0]
        small = sum(1 for v in non_zero if v <= 0x0F) if non_zero else 0

        if is_repeated:
            return "", False
        if non_zero and (small / len(non_zero)) > 0.5 and letters == 0:
            return "", False
        if letters < 2 or len(printable) < 4:
            return "", False

        chars = []
        for v in raw_bytes:
            if 0x20 <= v <= 0x7E:
                chars.append(chr(v))
            elif v == 0:
                break
            else:
                chars.append(f"\\x{v:02x}")
        version = "".join(chars).strip()
        return version, True

    found_any = False

    for bus_num, bus_name in smbus_buses:
        try:
            bus = SMBus(bus_num)
        except Exception:
            continue

        for addr in all_aura_addrs:
            try:
                bus.write_quick(addr)
            except OSError:
                continue
            except Exception:
                continue

            version, valid = _try_probe(bus, addr)
            if valid:
                lines.append(f"  {ok(f'i2c-{bus_num} ({bus_name}) @ 0x{addr:02X} → {version}')} (Aura/ASUS)")
                found_any = True
            else:
                # Device répond mais pas de version valide — déterminer type
                try:
                    raw_bytes = []
                    for i in range(4):
                        bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
                        v = bus.read_byte_data(addr, 0x81)
                        raw_bytes.append(v)
                    if any(b != 0 and b != 0xFF for b in raw_bytes):
                        lines.append(f"  {info(f'i2c-{bus_num} ({bus_name}) @ 0x{addr:02X} présent mais pas version valide')}")
                except Exception:
                    pass

        bus.close()

    if not found_any:
        lines.append(f"  {fail('Aucun device Aura SMBus trouvé')}")
        lines.append(f"")
        lines.append(f"  {info('Si vous avez une carte mère ASUS, le contrôleur RGB peut être')}")
        lines.append(f"  {info('sur le bus SMBus du chipset. Pour ASUS X570 :')}")
        lines.append(f"  {info('  sudo i2cdetect -y 9  (SMBus PIIX4 port 0)')}")
        lines.append(f"  {info('Les adresses 0x30, 0x34-0x37 sont les candidates.')}")
        lines.append(f"  {info('Certaines cartes ASUS utilisent un SMBus caché')}")
        lines.append(f"  {info('accessible uniquement via le module asus_wmi.')}")
    else:
        lines.append("")
        lines.append(f"  {ok('Scan Aura terminé')}")

    return "\n".join(lines)


# ── 1f. Protocole ENE (contrôleur RGB alternatif) ─────────────

def diag_try_ene_smbus() -> str:
    """Tente le protocole ENE (contrôleur RGB alternatif) sur les bus SMBus (chipset).

    Certaines RAM RGB utilisent un contrôleur ENE au lieu du Crucial.
    Le protocole ENE est différent : registres sur 8 bits, lecture/écriture directe.
    """
    lines: List[str] = []
    lines.append(header("\n═══ 1f. PROTOCOLE ENE (contrôleur RGB alternatif) ═══"))
    lines.append("")

    # Les adresses ENE typiques (certaines RAM Corsair, G.Skill)
    ene_addresses = [0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x2E, 0x2F]

    smbus_buses = get_smbus_buses()
    if not smbus_buses:
        lines.append(f"  {fail('Aucun bus SMBus (chipset) trouvé')}")
        lines.append(f"  {warn('Scan de tous les bus I2C en fallback...')}")
        all_buses = list_all_buses()
        if not all_buses:
            return "\n".join(lines)
        smbus_buses = all_buses

    lines.append(f"  Adresses ENE testées : {', '.join(f'0x{a:02X}' for a in ene_addresses)}")
    lines.append(f"  Bus SMBus scannés : {', '.join(f'i2c-{n} ({nm})' for n, nm in smbus_buses)}")
    lines.append(f"  {info('Note : 0x50-0x57 sont les SPD EEPROM, mais certains ENE sont derrière')}")
    lines.append("")

    found_any = False

    for bus_num, bus_name in smbus_buses:
        try:
            bus = SMBus(bus_num)
        except Exception:
            continue

        for addr in ene_addresses:
            try:
                bus.write_quick(addr)
            except OSError:
                continue
            except Exception:
                continue

            # Tenter le protocole ENE : registre 0xFA = ID
            try:
                bus.write_byte(addr, 0xFA)
                ene_id = bus.read_byte(addr)

                if ene_id != 0 and ene_id != 0xFF:
                    lines.append(f"  {ok(f'i2c-{bus_num} @ 0x{addr:02X} → ENE ID=0x{ene_id:02X}')}")
                    found_any = True

                    # Lire plus de registres ENE
                    ene_info: List[str] = []
                    for reg in [0xFB, 0xFC, 0xFD, 0xFE]:
                        try:
                            bus.write_byte(addr, reg)
                            v = bus.read_byte(addr)
                            ene_info.append(f"0x{reg:02X}=0x{v:02X}")
                        except Exception:
                            ene_info.append(f"0x{reg:02X}=?")
                    lines.append(f"    Registres ENE : {' '.join(ene_info)}")
                else:
                    # Peut-être un SPD, vérifier
                    try:
                        bus.write_byte(addr, 0x00)
                        spd_size = bus.read_byte(addr)
                        if spd_size in [0x80, 0x100, 0x180, 0x200, 0x280]:
                            lines.append(f"  {info(f'i2c-{bus_num} @ 0x{addr:02X} → SPD EEPROM (taille=0x{spd_size:02X})')}")
                        else:
                            lines.append(f"  {info(f'i2c-{bus_num} @ 0x{addr:02X} présent, device inconnu')}")
                    except Exception:
                        lines.append(f"  {info(f'i2c-{bus_num} @ 0x{addr:02X} présent mais protocole ENE incompatible')}")
            except OSError:
                # Device présent mais pas ENE
                try:
                    bus.write_byte(addr, 0x00)
                    spd_size = bus.read_byte(addr)
                    if spd_size in [0x80, 0x100, 0x180, 0x200, 0x280]:
                        lines.append(f"  {info(f'i2c-{bus_num} @ 0x{addr:02X} → SPD EEPROM')}")
                except Exception:
                    pass
            except Exception:
                continue

        bus.close()

    if not found_any:
        lines.append(f"  {fail('Aucun contrôleur ENE trouvé')}")
        lines.append(f"")
        lines.append(f"  {info('Le protocole ENE est utilisé par certaines RAM Corsair Vengeance RGB')}")
        lines.append(f"  {info('et G.Skill Trident Z RGB. Les Ballistix utilisent leur propre protocole.')}")
        lines.append(f"  {info('Ce test est principalement informatif.')}")
    else:
        lines.append("")
        lines.append(f"  {ok('Scan ENE terminé')}")

    return "\n".join(lines)


# ── 2. Scan avancé des bus SMBus ──────────────────────────────

def diag_scan_buses() -> str:
    """Scanne les bus SMBus (chipset) sur une plage étendue d'adresses.

    Utilise la même logique de validation stricte que _probe_device()
    pour éviter les faux positifs SPD EEPROM.
    Ignore les bus GPU (AMDGPU, NVIDIA).
    """
    lines: List[str] = []
    lines.append(header("\n═══ 2. SCAN AVANCÉ DES BUS SMBus ═══"))
    lines.append("")

    smbus_buses = get_smbus_buses()
    if not smbus_buses:
        lines.append(f"  {fail('Aucun bus SMBus (chipset) trouvé')}")
        lines.append(f"  {warn('Scan de tous les bus I2C en fallback...')}")
        all_buses = list_all_buses()
        if not all_buses:
            return "\n".join(lines)
        smbus_buses = all_buses

    lines.append(f"  Bus SMBus scannés : {', '.join(f'i2c-{n} ({nm})' for n, nm in smbus_buses)}")
    lines.append(f"  Plages d'adresses explorées :")
    for name, addrs in DIAG_ADDRESS_RANGES:
        lines.append(f"    {name} : 0x{addrs[0]:02X} - 0x{addrs[-1]:02X}")
    lines.append("")

    # En-tête du tableau
    header_line = f"  {'Bus':<10} {'Adr':<6} {'Répond':<8} {'Version':<20} {'Type probable':<20}"
    lines.append(f"  {ANSI_BOLD}{header_line}{ANSI_RESET}")
    lines.append(f"  {'-'*64}")

    found_any = False

    for bus_num, bus_name in smbus_buses:
        try:
            bus = SMBus(bus_num)
        except Exception:
            continue

        for range_name, addresses in DIAG_ADDRESS_RANGES:
            for addr in addresses:
                version = ""
                is_valid = False
                try:
                    # ── Lire les octets bruts (même logique que _probe_device) ──
                    raw_bytes: List[int] = []
                    for i in range(16):
                        try:
                            bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
                            v = bus.read_byte_data(addr, 0x81)
                            raw_bytes.append(v)
                        except OSError:
                            break

                    if not raw_bytes:
                        continue

                    # Analyse des octets bruts (validation commune)
                    is_repeated = _is_repeated_pattern(raw_bytes)
                    printable_chars = [chr(v) for v in raw_bytes if 0x20 <= v <= 0x7E]
                    letter_count = sum(1 for c in printable_chars if c.isalpha())
                    non_zero = [v for v in raw_bytes if v != 0]
                    small_bytes = sum(1 for v in non_zero if v <= 0x0F)

                    # Validation uniforme avec _probe_device()
                    if is_repeated:
                        is_valid = False  # Pattern répété (nnnnnn..., xxxx...)
                    elif non_zero and (small_bytes / len(non_zero)) > 0.5:
                        is_valid = False  # Trop d'octets ≤ 0x0F → SPD
                    elif letter_count == 0:
                        is_valid = False  # Aucune lettre → pas un firmware RGB
                    elif len(printable_chars) < 4:
                        is_valid = False  # Trop peu de caractères imprimables
                    else:
                        is_valid = True

                    if is_valid:
                        # Construire la version lisible
                        chars_display = []
                        for v in raw_bytes:
                            if 0x20 <= v <= 0x7E:
                                chars_display.append(chr(v))
                            elif v == 0:
                                break
                            else:
                                chars_display.append(f"\\x{v:02x}")
                        version = "".join(chars_display).strip("\\x00? \x00\n\r\t")[:18]

                except Exception:
                    is_valid = False

                if is_valid and version:
                    found_any = True
                    # Déterminer le type probable
                    v_lower = version.lower()
                    if any(k in v_lower for k in ["ballistix", "bl", "crucial"]):
                        ptype = "Ballistix RGB"
                    elif any(k in v_lower for k in ["v1.", "v2.", "ver"]):
                        ptype = "Contrôleur générique"
                    elif any(k in v_lower for k in ["temp", "therm"]):
                        ptype = "Capteur thermique"
                    else:
                        ptype = "Contrôleur RGB probable"

                    bus_str = f"i2c-{bus_num}"
                    addr_str = f"0x{addr:02X}"
                    resp_str = ok("Oui")
                    lines.append(f"  {bus_str:<10} {addr_str:<6} {resp_str:<8} {version:<20} {ptype:<20}")

        bus.close()

    if not found_any:
        lines.append(f"  {fail('Aucun contrôleur RGB trouvé.')}")
        lines.append(f"  {info('Les adresses SPD 0x50-0x57 (faux positifs) sont filtrées.')}")
    else:
        lines.append(f"")
        lines.append(f"  {ok('Scan terminé')}")

    return "\n".join(lines)


# ── 3. Test de communication par registre ─────────────────────

def diag_register_tests(stick: CrucialStick) -> str:
    """Teste la communication par registre avec une barrette."""
    lines: List[str] = []
    lines.append(header("\n═══ 3. TEST DE COMMUNICATION PAR REGISTRE ═══"))
    lines.append(f"  Cible : {stick.label}")
    lines.append("")

    # 3a. Lecture de la version (0x1000, 16 octets)
    lines.append(f"  {header('3a. Version (0x1000)')}")
    try:
        raw: List[int] = []
        for i in range(16):
            try:
                v = stick.register_read(CRUCIAL_REG_DEVICE_VERSION + i)
                raw.append(v)
            except OSError:
                raw.append(0)
        hex_str = " ".join(f"{b:02x}" for b in raw)
        ascii_str = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in raw)
        lines.append(f"    Hex   : [{hex_str}]")
        lines.append(f"    ASCII : [{ascii_str}]")
        ver = stick.get_version()
        if ver and "?" not in ver:
            lines.append(f"    {ok(f'Version décodée : {ver}')}")
        else:
            lines.append(f"    {fail('Version illisible ou vide')}")
    except Exception as e:
        lines.append(f"    {fail(f'Erreur lecture version : {e}')}")

    lines.append("")

    # 3b. Registres Micron check (0x1025, 0x1030)
    lines.append(f"  {header('3b. Registres Micron Check')}")
    for reg in [0x1025, 0x1030]:
        try:
            v = stick.register_read(reg)
            lines.append(f"    Registre 0x{reg:04X} = 0x{v:02X} ({v})")
            if v != 0:
                lines.append(f"      {ok('Valeur non nulle — registre accessible')}")
            else:
                lines.append(f"      {warn('Valeur nulle — peut être normal')}")
        except Exception as e:
            lines.append(f"    Registre 0x{reg:04X} → {fail(str(e))}")

    lines.append("")

    # 3c. Test écriture/lecture pour valider que ce n'est PAS un SPD EEPROM
    lines.append(f"  {header('3c. Test écriture/lecture (validation RGB vs SPD)')}")
    try:
        v0 = stick.register_read(CRUCIAL_REG_DEVICE_VERSION)
        stick.register_write(CRUCIAL_REG_DEVICE_VERSION, v0)
        v1 = stick.register_read(CRUCIAL_REG_DEVICE_VERSION)
        if v0 == v1:
            lines.append(f"    {ok('Écriture/Rélecture OK — device writable (contrôleur RGB)')}")
        else:
            lines.append(f"    {fail('Valeur a changé après écriture — device incertain')}")
    except Exception as e:
        lines.append(f"    {fail(f'Écriture impossible — device read-only (SPD EEPROM probable) : {e}')}")

    lines.append("")

    # 3d. Test écriture/lecture registre inoffensif
    lines.append(f"  {header('3d. Test écriture/lecture (brightness)')}")
    try:
        old_val = stick.register_read(CRUCIAL_REG_BRIGHTNESS_VAL)
        lines.append(f"    Luminosité actuelle : {old_val}")
        test_val = 128 if old_val != 128 else 200
        stick.register_write(CRUCIAL_REG_BRIGHTNESS_VAL, test_val)
        readback = stick.register_read(CRUCIAL_REG_BRIGHTNESS_VAL)
        if readback == test_val:
            lines.append(f"    {ok(f'Écriture/Lecture OK : écrit {test_val}, relu {readback}')}")
        else:
            lines.append(f"    {warn(f'Écrit/Lu différent : écrit {test_val}, relu {readback}')}")
        stick.register_write(CRUCIAL_REG_BRIGHTNESS_VAL, old_val)
        stick.register_write(CRUCIAL_REG_BRIGHTNESS_END, 0x83)
    except Exception as e:
        lines.append(f"    {fail(f'Erreur test écriture/lecture : {e}')}")

    lines.append("")

    # 3e. Test mode direct
    lines.append(f"  {header('3e. Test mode direct (LED 0 → Rouge)')}")
    try:
        old_colors = list(stick.colors)
        r_block = bytes([255] + [0] * (stick.num_leds - 1))
        g_block = bytes([0] * stick.num_leds)
        b_block = bytes([0] * stick.num_leds)
        stick.write_block(CRUCIAL_REG_DIRECT_R, r_block)
        stick.write_block(CRUCIAL_REG_DIRECT_G, g_block)
        stick.write_block(CRUCIAL_REG_DIRECT_B, b_block)
        lines.append(f"    {ok('Mode direct : LED 0 → Rouge (255)')}")
        time.sleep(0.5)
        stick.send_direct_colors(old_colors)
        lines.append(f"    {ok('Couleurs restaurées')}")
    except Exception as e:
        lines.append(f"    {fail(f'Erreur test mode direct : {e}')}")

    lines.append("")

    # 3f. Test mode individuel (0x82E9)
    lines.append(f"  {header('3f. Test mode individuel (0x82E9)')}")
    try:
        old_colors2 = list(stick.colors)
        stick.set_led_individual(0, 0, 255, 0)  # LED 0 → Vert
        lines.append(f"    {ok('Mode individuel : LED 0 → Vert (0, 255, 0)')}")
        time.sleep(0.5)
        stick.send_direct_colors(old_colors2)
        lines.append(f"    {ok('Couleurs restaurées')}")
    except Exception as e:
        lines.append(f"    {fail(f'Erreur test mode individuel : {e}')}")

    return "\n".join(lines)


# ── 4. Test de LEDs automatisé ────────────────────────────────

def diag_led_tests(stick: CrucialStick) -> str:
    """Exécute une séquence de tests sur les LEDs."""
    lines: List[str] = []
    lines.append(header("\n═══ 4. TEST DE LEDs AUTOMATISÉ ═══"))
    lines.append(f"  Cible : {stick.label}")
    lines.append("")

    import math

    try:
        # 4a. Clignotement individuel
        lines.append(f"  {header('4a. Clignotement individuel des LEDs')}")
        lines.append(f"  {info('Séquence : chaque LED → blanc 1s, off 0.5s')}")
        print()
        for i in range(stick.num_leds):
            stick.reset()
            time.sleep(0.05)
            stick.set_led(i, 255, 255, 255)
            print(f"    LED {i + 1}/{stick.num_leds} → BLANC", end="\r")
            time.sleep(1.0)
            stick.set_led(i, 0, 0, 0)
            time.sleep(0.3)
        stick.reset()
        print()
        lines.append(f"  {ok('Clignotement individuel terminé')}")

        lines.append("")

        # 4b. Cycle arc-en-ciel
        lines.append(f"  {header('4b. Cycle arc-en-ciel continu')}")
        lines.append(f"  {info('Arc-en-ciel sur toutes les LEDs (Ctrl+C pour arrêter)')}")
        print()
        try:
            for step in range(60):  # 60 étapes = ~6 secondes
                for i in range(stick.num_leds):
                    hue = ((i / stick.num_leds) * 360 + step * 6) % 360
                    h = hue / 60.0
                    sector = int(h) % 6
                    f = h - int(h)
                    sector_map = {
                        0: (1.0, f, 0.0),
                        1: (1.0 - f, 1.0, 0.0),
                        2: (0.0, 1.0, f),
                        3: (0.0, 1.0 - f, 1.0),
                        4: (f, 0.0, 1.0),
                        5: (1.0, 0.0, 1.0 - f),
                    }
                    r, g, b = sector_map[sector]
                    stick.colors[i] = (int(r * 255), int(g * 255), int(b * 255))
                stick.send_direct_colors()
                time.sleep(0.1)
            lines.append(f"  {ok('Cycle arc-en-ciel terminé')}")
        except KeyboardInterrupt:
            user_msg = "Cycle arc-en-ciel interrompu par l'utilisateur"
            lines.append(f"  {warn(user_msg)}")

        lines.append("")

        # 4c. Test de luminosité
        lines.append(f"  {header('4c. Test de luminosité')}")
        stick.set_all_leds(255, 255, 255)
        time.sleep(0.3)

        for level, label in [(255, "100 %"), (127, "50 %"), (64, "25 %"), (0, "Off"), (255, "100 %")]:
            stick.set_brightness(level)
            print(f"    Luminosité : {label} ({level})")
            time.sleep(0.8)
        lines.append(f"  {ok('Test de luminosité terminé')}")

    except Exception as e:
        lines.append(f"  {fail(f'Erreur pendant le test LEDs : {e}')}")
    finally:
        stick.reset()
        lines.append(f"")
        lines.append(f"  {ok('LEDs remises à zéro')}")

    return "\n".join(lines)


# ── 5. Dump des registres ─────────────────────────────────────

def diag_dump_registers(stick: CrucialStick, filename: str = "") -> str:
    """Dump les registres de 0x8200 à 0x83FF dans un fichier."""
    lines: List[str] = []
    lines.append(header("\n═══ 5. DUMP DES REGISTRES (0x8200 - 0x83FF) ═══"))
    lines.append(f"  Cible : {stick.label}")
    lines.append("")

    if not filename:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ballistix_dump_{ts}.txt"

    data: List[Tuple[int, int]] = []
    interesting: List[str] = []

    lines.append(f"  {info('Lecture des registres...')}")
    print()

    for reg in range(0x8200, 0x8400):
        try:
            v = stick.register_read(reg)
            data.append((reg, v))

            # Signaler les valeurs potentiellement intéressantes
            if v != 0 and v != 0xFF:
                if len(data) >= 2:
                    prev_val = data[-2][1]
                    if abs(v - prev_val) > 1:
                        interesting.append(f"    0x{reg:04X} = 0x{v:02X} ({v})  (diffère du précédent)")
        except Exception:
            data.append((reg, -1))

        if (reg - 0x8200) % 32 == 0:
            print(f"    Progression : 0x{reg:04X} / 0x8400 ({((reg - 0x8200) / 0x200) * 100:.0f}%)")

    print()

    # Générer l'affichage hexdump
    lines.append(f"  Hexdump (0x8200 - 0x83FF):")
    lines.append("")

    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        addr = 0x8200 + offset
        hex_part = " ".join(f"{v:02x}" if v >= 0 else "??" for _, v in chunk)
        ascii_part = "".join(chr(v) if 0x20 <= v <= 0x7E else "." for _, v in chunk if v >= 0)
        lines.append(f"  0x{addr:04X}: {hex_part:<48}  |{ascii_part}|")

    lines.append("")

    if interesting:
        lines.append(f"  {info('Valeurs potentiellement intéressantes :')}")
        for entry in interesting[:30]:
            lines.append(f"    {entry}")
        if len(interesting) > 30:
            lines.append(f"    ... et {len(interesting) - 30} autres")
    else:
        lines.append(f"  {warn('Aucune valeur particulièrement intéressante détectée')}")

    # Sauvegarde dans un fichier
    lines.append("")
    try:
        with open(filename, "w") as f:
            f.write(f"Ballistix Register Dump\n")
            f.write(f"Date: {datetime.datetime.now().isoformat()}\n")
            f.write(f"Device: {stick.label}\n")
            f.write(f"Bus: i2c-{stick.bus_num}, Address: 0x{stick.address:02X}\n")
            f.write(f"Range: 0x8200 - 0x83FF\n")
            f.write("=" * 70 + "\n\n")

            for offset in range(0, len(data), 16):
                chunk = data[offset:offset + 16]
                addr = 0x8200 + offset
                hex_part = " ".join(f"{v:02x}" if v >= 0 else "??" for _, v in chunk)
                ascii_part = "".join(chr(v) if 0x20 <= v <= 0x7E else "." for _, v in chunk if v >= 0)
                f.write(f"0x{addr:04X}: {hex_part:<48} |{ascii_part}|\n")

        lines.append(f"  {ok(f'Dump sauvegardé dans {filename}')}")
    except Exception as e:
        lines.append(f"  {fail(f'Erreur sauvegarde du dump : {e}')}")

    return "\n".join(lines)


# ── 6. Scan exhaustif (--scan-all) ────────────────────────────

def diag_scan_all() -> str:
    """Scan exhaustif des bus SMBus (chipset) sur TOUTES les adresses 0x08-0x77.

    Affiche un tableau complet de tout ce qui répond, avec tentative
    de lecture de version. Utilisable via --scan-all.
    Distingue les bus SMBus chipset des bus GPU.
    """
    lines: List[str] = []
    lines.append(header("\n═══ SCAN EXHAUSTIF — BUS SMBus × TOUTES ADRESSES ═══"))
    lines.append("")

    all_buses = list_all_buses()
    if not all_buses:
        lines.append(fail("Aucun bus I2C trouvé."))
        lines.append("  → Chargez le module : sudo modprobe i2c-dev")
        return "\n".join(lines)

    smbus_buses = get_smbus_buses()
    smbus_nums = [n for n, _ in smbus_buses]
    gpu_buses = [(n, nm) for n, nm in all_buses if n not in smbus_nums]

    lines.append(f"  {header('Bus I2C totaux :')}")
    for bus_num, name in all_buses:
        lines.append(f"    i2c-{bus_num} → {name}")
    lines.append("")
    if smbus_buses:
        lines.append(f"  {ok(f'Bus SMBus (chipset) : {len(smbus_buses)} bus')}")
        for bus_num, name in smbus_buses:
            lines.append(f"    i2c-{bus_num} → {name}")
        if gpu_buses:
            gpu_nums = [n for n, _ in gpu_buses]
            gpu_str = ", i2c-".join(str(n) for n in gpu_nums)
            lines.append(f"  {info(f'Bus GPU ignorés : i2c-{gpu_str}')}")
    else:
        lines.append(f"  {warn('Aucun bus SMBus (chipset) détecté par nom — scan de tous les bus')}")
        smbus_buses = all_buses  # Fallback
    lines.append("")

    lines.append(f"  Adresses scannées : 0x08 - 0x77 (112 adresses par bus)")
    lines.append(f"  Méthode : write_quick() puis lecture version (0x1000)")
    lines.append("")

    # En-tête du tableau
    sep = f"  {'─'*8}┼{'─'*6}┼{'─'*8}┼{'─'*22}┼{'─'*18}"
    hdr = f"  {'Bus':<8}│{'Adr':<6}│{'Répond':<8}│{'FW Version':<22}│{'Type':<18}"
    lines.append(hdr)
    lines.append(sep)

    total_found = 0
    rgb_found = 0
    spd_found = 0
    other_found = 0

    for bus_num, bus_name in smbus_buses:
        try:
            bus = SMBus(bus_num)
        except PermissionError:
            lines.append(f"  i2c-{bus_num:<5} │{'N/A':<6}│{'N/A':<8}│{'Permission refusée':<22}│{'':<18}")
            continue
        except Exception as e:
            lines.append(f"  i2c-{bus_num:<5} │{'N/A':<6}│{'N/A':<8}│{str(e)[:20]:<22}│{'':<18}")
            continue

        for addr in range(0x08, 0x78):
            try:
                bus.write_quick(addr)
            except OSError:
                continue
            except Exception:
                continue

            # Device répond
            total_found += 1
            version = ""
            device_type = ""

            # Lire la version
            try:
                raw_bytes: List[int] = []
                for i in range(16):
                    try:
                        bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_DEVICE_VERSION + i))
                        v = bus.read_byte_data(addr, 0x81)
                        raw_bytes.append(v)
                    except OSError:
                        break

                if raw_bytes:
                    is_repeated = _is_repeated_pattern(raw_bytes)
                    printable = [chr(v) for v in raw_bytes if 0x20 <= v <= 0x7E]
                    letters = sum(1 for c in printable if c.isalpha())
                    non_zero = [v for v in raw_bytes if v != 0]
                    small = sum(1 for v in non_zero if v <= 0x0F) if non_zero else 0

                    # Construire version
                    chars: List[str] = []
                    for v in raw_bytes:
                        if 0x20 <= v <= 0x7E:
                            chars.append(chr(v))
                        elif v == 0:
                            break
                        else:
                            chars.append(f"\\x{v:02x}")
                    version = "".join(chars).strip()[:20]

                    if is_repeated:
                        device_type = "Pattern répété (faux positif)"
                        other_found += 1
                    elif non_zero and (small / len(non_zero)) > 0.5 and letters == 0:
                        device_type = "SPD EEPROM"
                        spd_found += 1
                    elif letters >= 2 and len(printable) >= 4:
                        device_type = "RGB DRAM ✅"
                        rgb_found += 1
                    elif any(0x20 <= v <= 0x7E for v in raw_bytes):
                        device_type = "Device inconnu"
                        other_found += 1
                    else:
                        device_type = "Silencieux"
                        other_found += 1
                else:
                    device_type = "Présent (pas version)"
                    other_found += 1
            except Exception:
                device_type = "Présent (lecture impossible)"
                other_found += 1

            bus_str = f"i2c-{bus_num}"
            addr_str = f"0x{addr:02X}"
            resp_str = "✓"

            lines.append(f"  {bus_str:<8}│{addr_str:<6}│{resp_str:<8}│{version:<22}│{device_type:<18}")

        bus.close()

    # Résumé
    lines.append(sep)
    lines.append("")
    lines.append(f"  {ok(f'Scan terminé : {total_found} device(s) trouvé(s)')}")
    lines.append(f"    - Contrôleurs RGB DRAM : {rgb_found}")
    lines.append(f"    - SPD EEPROM : {spd_found}")
    lines.append(f"    - Autres : {other_found}")

    if rgb_found == 0:
        lines.append("")
        lines.append(f"  {warn('Aucun contrôleur RGB DRAM trouvé. Causes possibles :')}")
        lines.append(f"    - Le contrôleur SMBus est désactivé dans le BIOS")
        lines.append(f"    - Le module du chipset n'est pas chargé")
        lines.append(f"    - Les barrettes ne sont pas des Crucial Ballistix RGB")
        lines.append(f"    - Le contrôleur RGB est sur un bus multiplexé non visible")
        lines.append(f"")
        lines.append(f"  {info('Pour investiguer :')}")
        lines.append(f"    $ sudo modprobe i2c-dev")
        lines.append(f"    $ sudo modprobe i2c-piix4 (AMD)")
        lines.append(f"    $ sudo i2cdetect -l")
        lines.append(f"    $ sudo i2cdetect -y 9  (SMBus PIIX4 - bus chipset)")
        lines.append(f"    $ sudo python3 ballistix_tester.py --diagnostic")

    return "\n".join(lines)


# ── 7. Rapport de diagnostic complet ──────────────────────────

def run_full_diagnostic(sticks: List[CrucialStick]) -> str:
    """Exécute tous les diagnostics et génère un rapport complet."""
    report_parts: List[str] = []

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"ballistix_diagnostic_{ts}.txt"

    # En-tête
    report_parts.append("=" * 70)
    report_parts.append("  CRUCIAL BALLISTIX — RAPPORT DE DIAGNOSTIC")
    report_parts.append(f"  Date : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_parts.append("=" * 70)
    report_parts.append("")

    # 1. Système
    print()
    print(ANSI_BOLD + "Exécution du diagnostic système..." + ANSI_RESET)
    sys_diag = diag_system()
    report_parts.append(sys_diag)
    print(sys_diag)

    # 1b. Chipset SMBus
    print()
    print(ANSI_BOLD + "Analyse du chipset SMBus..." + ANSI_RESET)
    chipset_diag = diag_smbus_chipset()
    report_parts.append(chipset_diag)
    print(chipset_diag)

    # 1c. i2cdetect sur tous les bus
    print()
    print(ANSI_BOLD + "Exécution de i2cdetect sur tous les bus..." + ANSI_RESET)
    i2cdetect_diag = diag_i2cdetect_all()
    report_parts.append(i2cdetect_diag)
    print(i2cdetect_diag)

    # 1d. Scan exhaustif SMBus direct
    print()
    print(ANSI_BOLD + "Scan exhaustif SMBus direct..." + ANSI_RESET)
    try_all_diag = diag_try_all_buses()
    report_parts.append(try_all_diag)
    print(try_all_diag)

    # 1e. Protocole Aura SMBus
    print()
    print(ANSI_BOLD + "Test protocole Aura SMBus (ASUS)..." + ANSI_RESET)
    aura_diag = diag_try_aura_smbus()
    report_parts.append(aura_diag)
    print(aura_diag)

    # 1f. Protocole ENE
    print()
    print(ANSI_BOLD + "Test protocole ENE..." + ANSI_RESET)
    ene_diag = diag_try_ene_smbus()
    report_parts.append(ene_diag)
    print(ene_diag)

    # 2. Scan avancé
    print()
    print(ANSI_BOLD + "Scan avancé des bus SMBus..." + ANSI_RESET)
    scan_diag = diag_scan_buses()
    report_parts.append(scan_diag)
    print(scan_diag)

    if sticks:
        for idx, stick in enumerate(sticks):
            # 3. Tests registres
            print()
            print(ANSI_BOLD + f"Tests de communication sur barrette #{idx + 1}..." + ANSI_RESET)
            reg_diag = diag_register_tests(stick)
            report_parts.append(reg_diag)
            print(reg_diag)

            # 4. Tests LEDs
            print()
            print(ANSI_BOLD + f"Tests LEDs automatisés sur barrette #{idx + 1}..." + ANSI_RESET)
            led_diag = diag_led_tests(stick)
            report_parts.append(led_diag)
            print(led_diag)

            # 5. Dump registres
            print()
            print(ANSI_BOLD + f"Dump des registres sur barrette #{idx + 1}..." + ANSI_RESET)
            dump_filename = f"ballistix_dump_{idx + 1}_{ts}.txt"
            dump_diag = diag_dump_registers(stick, dump_filename)
            report_parts.append(dump_diag)
            print(dump_diag)
    else:
        report_parts.append(header("\n═══ TESTS BARRETTES ═══"))
        report_parts.append(f"  {fail('Aucune barrette détectée — tests impossibles')}")
        print(f"\n{ANSI_RED}✗ Aucune barrette détectée — tests de registres/LEDs/dump ignorés{ANSI_RESET}")

    # Résumé
    report_parts.append("")
    report_parts.append("=" * 70)
    report_parts.append("  RÉSUMÉ")
    report_parts.append("=" * 70)
    report_parts.append("")
    if sticks:
        report_parts.append(f"  ✓ Barrette(s) détectée(s) : {len(sticks)}")
        for s in sticks:
            report_parts.append(f"    - {s.label}")
        report_parts.append(f"  ✓ Tests de communication : effectués")
        report_parts.append(f"  ✓ Tests LEDs : effectués")
        report_parts.append(f"  ✓ Dump registres : effectué")
    else:
        report_parts.append(f"  ✗ Aucune barrette détectée")
        report_parts.append(f"  ⚠ Vérifiez :")
        report_parts.append(f"    - Module i2c-dev chargé")
        report_parts.append(f"    - Permissions (sudo ou groupe i2c)")
        report_parts.append(f"    - BIOS : SMBus activé")
        report_parts.append(f"    - Adresses peut-être hors plage standard")

    report_parts.append("")
    report_parts.append(f"  Fichiers générés :")
    report_parts.append(f"    - {report_filename} (ce rapport)")
    if sticks:
        for idx in range(len(sticks)):
            report_parts.append(f"    - ballistix_dump_{idx + 1}_{ts}.txt")
    report_parts.append("")
    report_parts.append("=" * 70)
    report_parts.append("  FIN DU RAPPORT")
    report_parts.append("=" * 70)

    report = "\n".join(report_parts)

    # Sauvegarde du rapport
    try:
        clean_report = report.replace(ANSI_RESET, "").replace(ANSI_RED, "").replace(ANSI_GREEN, "")
        clean_report = clean_report.replace(ANSI_YELLOW, "").replace(ANSI_BLUE, "").replace(ANSI_MAGENTA, "")
        clean_report = clean_report.replace(ANSI_CYAN, "").replace(ANSI_BOLD, "").replace(ANSI_DIM, "")
        with open(report_filename, "w") as f:
            f.write(clean_report)
        print(f"\n{ok(f'Rapport sauvegardé dans {report_filename}')}")
    except Exception as e:
        print(f"\n{fail(f'Erreur sauvegarde rapport : {e}')}")

    return report


# ── Fonctions CLI pour les diagnostics ────────────────────────

def cli_diagnostic(sticks: List[CrucialStick]) -> None:
    """Exécute le diagnostic complet en mode CLI."""
    print(ANSI_BOLD + ANSI_MAGENTA + "=" * 70 + ANSI_RESET)
    print(ANSI_BOLD + "  CRUCIAL BALLISTIX — MODE DIAGNOSTIC COMPLET" + ANSI_RESET)
    print(ANSI_BOLD + "=" * 70 + ANSI_RESET)
    print()

    report = run_full_diagnostic(sticks)

    print()
    print(ANSI_BOLD + ANSI_GREEN + "=" * 70 + ANSI_RESET)
    print(ANSI_BOLD + "  DIAGNOSTIC TERMINÉ" + ANSI_RESET)
    print(ANSI_BOLD + "=" * 70 + ANSI_RESET)
    print()
    print(f"  Résumé :")

    if sticks:
        print(f"    {ok(f'{len(sticks)} barrette(s) détectée(s)')}")
        for s in sticks:
            print(f"      {s.label}")
        print(f"    {ok('Tests de communication effectués')}")
        print(f"    {ok('Tests LEDs effectués')}")
        print(f"    {ok('Dump registres effectué')}")
    else:
        print(f"    {fail('Aucune barrette détectée')}")
        print(f"    {warn('Voir le rapport pour les pistes de résolution')}")

    print()
    print(f"  {info('Rapport complet disponible dans le fichier de diagnostic.')}")
    print()


def cli_led_test(sticks: List[CrucialStick]) -> None:
    """Exécute uniquement le test de LEDs automatisé."""
    if not sticks:
        print(fail("Aucune barrette détectée — impossible de tester les LEDs"))
        return

    print(header("\nTEST DE LEDs AUTOMATISÉ (CLI)"))
    print(f"  Appareil(s) : {[s.label for s in sticks]}")
    print()

    for idx, stick in enumerate(sticks):
        print(header(f"\n--- Barrette #{idx + 1} ---"))
        result = diag_led_tests(stick)
        print(result)


def cli_dump(sticks: List[CrucialStick]) -> None:
    """Effectue un dump des registres et sauvegarde dans un fichier."""
    if not sticks:
        print(fail("Aucune barrette détectée — impossible de dumper les registres"))
        return

    print(header("\nDUMP DES REGISTRES (CLI)"))
    print()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    for idx, stick in enumerate(sticks):
        print(header(f"--- Barrette #{idx + 1} ---"))
        filename = f"ballistix_dump_{idx + 1}_{ts}.txt"
        result = diag_dump_registers(stick, filename)
        print(result)


__all__ = [
    "_run_cmd",
    "diag_system", "diag_smbus_chipset", "diag_i2cdetect_all",
    "diag_try_all_buses", "diag_try_aura_smbus", "diag_try_ene_smbus",
    "diag_scan_buses", "diag_register_tests", "diag_led_tests",
    "diag_dump_registers", "diag_scan_all",
    "run_full_diagnostic",
    "cli_diagnostic", "cli_led_test", "cli_dump",
]

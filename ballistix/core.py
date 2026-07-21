#!/usr/bin/env python3
"""
ballistix/core.py — Classes et constantes principales pour le contrôle
des LEDs Crucial Ballistix via SMBus.

Protocole reverse-engineered depuis OpenRGB (CrucialController.cpp).
"""

import struct
from typing import List, Tuple, Optional

# ──────────────────────────────────────────────────────────────
# Constantes (issues du reverse-engineering d'OpenRGB)
# ──────────────────────────────────────────────────────────────

# Plage d'adresses SMBus à scanner.
# ATTENTION : les adresses 0x50-0x57 sont celles des SPD EEPROM (faux positifs).
# Le contrôleur RGB Ballistix est généralement sur 0x37, 0x27, 0x3A, etc.
# On ajoute aussi 0x50-0x57 pour compatibilité, mais la validation les filtrera.
SCAN_ADDRESSES = list(range(0x20, 0x60))

# Plage étendue pour le diagnostic
DIAG_ADDRESS_RANGES = [
    ("Plage basse",     list(range(0x20, 0x30))),
    ("Contrôleurs RGB", list(range(0x30, 0x40))),
    ("SPD EEPROM",      list(range(0x50, 0x58))),
    ("SPD étendu",      list(range(0x58, 0x60))),
]

# Registre de version (16 octets)
CRUCIAL_REG_DEVICE_VERSION = 0x1000

# Mode direct : 3 blocs de 8 octets pour R, V, B
CRUCIAL_REG_DIRECT_R = 0x8300   # Canaux rouges, LED 0-7
CRUCIAL_REG_DIRECT_G = 0x8340   # Canaux verts,  LED 0-7
CRUCIAL_REG_DIRECT_B = 0x8380   # Canaux bleus,  LED 0-7

# Luminosité
CRUCIAL_REG_BRIGHTNESS_CTL  = 0x82EE  # = 0xFF pour activer
CRUCIAL_REG_BRIGHTNESS_VAL  = 0x82EF  # valeur 0-255
CRUCIAL_REG_BRIGHTNESS_END  = 0x82F0  # = 0x83 pour valider

# Mode sélection individuelle de LED (pour mémoire)
CRUCIAL_REG_LED_SELECT = 0x82E9  # (1 << led_idx)
CRUCIAL_REG_LED_R      = 0x82ED
CRUCIAL_REG_LED_G      = 0x82EE
CRUCIAL_REG_LED_B      = 0x82EF
CRUCIAL_REG_LED_APPLY  = 0x82F0  # = 0x01

# Nombre de LEDs par défaut
NUM_LEDS = 8


# ──────────────────────────────────────────────────────────────
# Couleurs ANSI pour le terminal
# ──────────────────────────────────────────────────────────────

ANSI_RESET = "\033[0m"
ANSI_RED = "\033[91m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_BLUE = "\033[94m"
ANSI_MAGENTA = "\033[95m"
ANSI_CYAN = "\033[96m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"


def ok(text: str) -> str:
    """Retourne le texte en vert avec un ✓."""
    return f"{ANSI_GREEN}✓ {text}{ANSI_RESET}"


def fail(text: str) -> str:
    """Retourne le texte en rouge avec un ✗."""
    return f"{ANSI_RED}✗ {text}{ANSI_RESET}"


def warn(text: str) -> str:
    """Retourne le texte en jaune avec un ⚠."""
    return f"{ANSI_YELLOW}⚠ {text}{ANSI_RESET}"


def info(text: str) -> str:
    """Retourne le texte en cyan."""
    return f"{ANSI_CYAN}ℹ {text}{ANSI_RESET}"


def header(text: str) -> str:
    """Retourne le texte en gras magenta."""
    return f"{ANSI_BOLD}{ANSI_MAGENTA}{text}{ANSI_RESET}"


# ──────────────────────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────────────────────

def bswap16(val: int) -> int:
    """Swap bytes of a 16-bit value for big-endian register addressing.

    Linux write_word_data() sends LSB first (little-endian on the wire).
    The Crucial controller expects the register address in big-endian.
    By pre-swapping, we get the correct byte order on the bus.

    Example:
        reg = 0x8300
        swapped = 0x0083
        On the wire: [cmd=0x00, 0x83, 0x00]  ← big-endian 0x8300 ✓
    """
    return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)


# ──────────────────────────────────────────────────────────────
# Validation de patterns (faux positifs)
# ──────────────────────────────────────────────────────────────

def _check_micron_registers(bus, addr: int) -> bool:
    """Vérifie la présence du marqueur 'Micron' dans les registres.

    Les vrais contrôleurs Crucial Ballistix contiennent la chaîne 'Micron'
    (nom du fabricant des puces DRAM) dans leurs registres internes.
    On vérifie aux adresses 0x1025 et 0x1030 (6 octets chacune).

    Returns:
        True si 'Micron' est trouvé à l'une des adresses.
    """
    micron_str = "Micron"
    try:
        for start_reg in [0x1025, 0x1030]:
            chars: List[str] = []
            for i in range(len(micron_str)):
                bus.write_word_data(addr, 0x00, bswap16(start_reg + i))
                v = bus.read_byte_data(addr, 0x81)
                if 0x20 <= v <= 0x7E:
                    chars.append(chr(v))
                else:
                    chars.append("?")
            result = "".join(chars)
            if result == micron_str:
                return True
    except Exception:
        pass
    return False


def _test_write_persistence(bus, addr: int) -> bool:
    """Teste si un device accepte l'écriture (contrairement à un SPD read-only).

    Procédure :
    1. Lire la valeur actuelle d'un registre writable (0x82EF = brightness value)
    2. Écrire une valeur différente (complément)
    3. Relire pour vérifier que la valeur a changé
    4. Restaurer la valeur originale

    Returns:
        True si l'écriture persiste (vrai contrôleur RGB), False sinon.
    """
    test_reg = CRUCIAL_REG_BRIGHTNESS_VAL  # 0x82EF
    try:
        # 1. Lire la valeur actuelle
        bus.write_word_data(addr, 0x00, bswap16(test_reg))
        original = bus.read_byte_data(addr, 0x81)

        # 2. Écrire une valeur différente (complément binaire, jamais égal à original)
        test_val = (~original) & 0xFF
        if test_val == original:
            test_val = (original + 1) & 0xFF  # Fallback au cas où ~x == x (x=0x7F? impossible)

        bus.write_word_data(addr, 0x00, bswap16(test_reg))
        bus.write_byte_data(addr, 0x01, test_val)

        # 3. Relire pour vérifier que la valeur a changé
        bus.write_word_data(addr, 0x00, bswap16(test_reg))
        readback = bus.read_byte_data(addr, 0x81)

        if readback == test_val:
            # 4. Restaurer la valeur originale
            bus.write_word_data(addr, 0x00, bswap16(test_reg))
            bus.write_byte_data(addr, 0x01, original)
            # Re-activer la luminosité si nécessaire
            bus.write_word_data(addr, 0x00, bswap16(CRUCIAL_REG_BRIGHTNESS_END))
            bus.write_byte_data(addr, 0x01, 0x83)
            return True

        # La valeur n'a pas changé → device read-only (SPD)
        return False

    except Exception:
        return False  # Échec écriture → read-only (SPD ou inexistant)



def _is_repeated_pattern(raw_bytes: List[int]) -> bool:
    """Vérifie si les octets forment un pattern répété (faux positif).

    Critères de rejet :
    - Moins de 3 caractères uniques différents (hors zéros de fin)
    - Tous les caractères identiques (ex: nnnnnnnnnnnnnnnn)
    - Pattern comme xxxx..., \xff\xff..., etc.

    Returns:
        True si le pattern doit être rejeté (faux positif probable).
    """
    # Ignorer les zéros de fin (padding)
    significant = [v for v in raw_bytes if v != 0]
    if not significant:
        return True  # Tous nuls → rejeter

    # Compter les caractères uniques (valeurs distinctes)
    unique_values = set(significant)
    if len(unique_values) <= 3:
        return True  # Pattern trop répétitif → rejeter

    # Vérifier caractères ASCII imprimables uniques
    printable = [chr(v) for v in significant if 0x20 <= v <= 0x7E]
    unique_printable = set(printable)
    if len(unique_printable) <= 2 and len(significant) >= 4:
        return True  # Très peu de caractères uniques imprimables → rejeter

    return False


# ──────────────────────────────────────────────────────────────
# Pilote SMBus pour une barrette Crucial Ballistix
# ──────────────────────────────────────────────────────────────

class CrucialStick:
    """Représente une barrette Crucial Ballistix et son contrôleur RGB.

    Le protocole SMBus fonctionne en 2 temps :
      1. Écrire l'adresse du registre (16 bits, big-endian) sur le cmd 0x00
      2. Lire/écrire la valeur sur le cmd 0x81 (lecture) ou 0x01 (écriture)

    Pour les blocs, on écrit le registre sur 0x00 puis le bloc sur 0x03.
    """

    def __init__(self, bus, address: int, bus_num: int,
                 version: str = "", bus_name: str = ""):
        self.bus = bus
        self.address = address      # Adresse SMBus (ex: 0x50)
        self.bus_num = bus_num      # Numéro du bus I2C
        self.bus_name = bus_name    # Nom du bus (ex: "SMBus PIIX4 port 0")
        self._version = version     # Chaîne de version (mise en cache)
        self.num_leds = NUM_LEDS    # Nombre de LEDs (ajustable)
        self.colors: List[Tuple[int, int, int]] = [(0, 0, 0)] * self.num_leds
        self._brightness = 255
        self._static_mode_set = False  # Mode static activé au premier send_direct_colors

    # ── Opérations bas niveau ─────────────────────────────────

    def _write_reg(self, reg: int) -> None:
        """Écrit l'adresse du registre (big-endian) sur le bus."""
        self.bus.write_word_data(self.address, 0x00, bswap16(reg))

    def register_write(self, reg: int, val: int) -> None:
        """Écrit un octet (0-255) dans un registre."""
        self._write_reg(reg)
        self.bus.write_byte_data(self.address, 0x01, val & 0xFF)

    def register_read(self, reg: int) -> int:
        """Lit un octet depuis un registre et retourne sa valeur (0-255)."""
        self._write_reg(reg)
        return self.bus.read_byte_data(self.address, 0x81)

    def write_block(self, reg: int, data: bytes) -> None:
        """Écrit un bloc d'octets (max 32) dans un registre.

        Envoie sur le bus : [cmd=0x03, count=N, data[0], ..., data[N-1]]
        """
        self._write_reg(reg)
        self.bus.write_block_data(self.address, 0x03, list(data))

    # ── Contrôle des LEDs (mode direct) ───────────────────────

    def set_mode_static(self) -> None:
        """Désactive les effets et passe en mode statique (couleurs directes).

        Écrit la séquence de registres définie dans OpenRGB (CrucialController.cpp) :
          - 0x820F = 0xCF (CRUCIAL_MODE_STATIC)
          - 0x82EE = 0x00
          - 0x82EF = 0x10 (speed)
          - 0x82F0 = 0x84 (apply/validate)

        Ceci empêche l'animation par défaut du firmware d'écraser les couleurs
        envoyées en mode direct.
        """
        self.register_write(0x820F, 0xCF)  # CRUCIAL_MODE_STATIC
        self.register_write(0x82EE, 0x00)
        self.register_write(0x82EF, 0x10)  # speed
        self.register_write(0x82F0, 0x84)  # apply

    def send_direct_colors(self,
                           colors: Optional[List[Tuple[int, int, int]]] = None
                           ) -> None:
        """Envoie toutes les couleurs des LEDs en mode direct.

        Passe d'abord en mode STATIC pour désactiver les animations du firmware
        (sinon les couleurs seraient écrasées immédiatement).

        Envoie 3 blocs de N octets (N = nombre de LEDs) :
          - 0x8300 : canaux rouges
          - 0x8340 : canaux verts
          - 0x8380 : canaux bleus
        """
        # Désactiver les animations du firmware une seule fois
        if not self._static_mode_set:
            self.set_mode_static()
            self._static_mode_set = True

        if colors is not None:
            self.colors = list(colors)

        cols = self.colors[:self.num_leds]
        # Compléter avec du noir si nécessaire
        while len(cols) < self.num_leds:
            cols.append((0, 0, 0))

        r_block = bytes(c[0] for c in cols)
        g_block = bytes(c[1] for c in cols)
        b_block = bytes(c[2] for c in cols)

        self.write_block(CRUCIAL_REG_DIRECT_R, r_block)
        self.write_block(CRUCIAL_REG_DIRECT_G, g_block)
        self.write_block(CRUCIAL_REG_DIRECT_B, b_block)

    def set_led(self, idx: int, r: int, g: int, b: int) -> None:
        """Change la couleur d'une LED et l'envoie immédiatement."""
        if 0 <= idx < self.num_leds:
            self.colors[idx] = (r & 0xFF, g & 0xFF, b & 0xFF)
            self.send_direct_colors()

    def set_all_leds(self, r: int, g: int, b: int) -> None:
        """Met toutes les LEDs à la même couleur et envoie."""
        color = (r & 0xFF, g & 0xFF, b & 0xFF)
        for i in range(self.num_leds):
            self.colors[i] = color
        self.send_direct_colors()

    def reset(self) -> None:
        """Éteint toutes les LEDs (met tout à 0)."""
        self.set_all_leds(0, 0, 0)

    # ── Sélection individuelle de LED (mode alternatif) ───────

    def set_led_individual(self, idx: int, r: int, g: int, b: int) -> None:
        """Change une LED via le mode sélection individuelle.

        Utile si le mode direct ne fonctionne pas sur certaines versions.
        """
        self.register_write(CRUCIAL_REG_LED_SELECT, 1 << idx)
        self.register_write(CRUCIAL_REG_LED_R, r & 0xFF)
        self.register_write(CRUCIAL_REG_LED_G, g & 0xFF)
        self.register_write(CRUCIAL_REG_LED_B, b & 0xFF)
        self.register_write(CRUCIAL_REG_LED_APPLY, 0x01)

        self.colors[idx] = (r & 0xFF, g & 0xFF, b & 0xFF)

    # ── Luminosité ────────────────────────────────────────────

    def set_brightness(self, level: int) -> None:
        """Règle la luminosité (0 = éteint, 255 = max)."""
        level = max(0, min(255, level))
        self._brightness = level
        self.register_write(CRUCIAL_REG_BRIGHTNESS_CTL, 0xFF)
        self.register_write(CRUCIAL_REG_BRIGHTNESS_VAL, level)
        self.register_write(CRUCIAL_REG_BRIGHTNESS_END, 0x83)

    def get_brightness(self) -> int:
        """Retourne la luminosité courante."""
        return self._brightness

    # ── Version / Identification ──────────────────────────────

    def get_version(self) -> str:
        """Lit la chaîne de version (16 octets à partir de 0x1000).

        Retourne une chaîne lisible pour l'affichage.
        Les octets non imprimables (hors 0x20-0x7E) sont représentés en \\xNN.

        Validation : si la chaîne est un pattern répété (faux positif), 
        retourne une chaîne vide pour signaler l'invalidité.
        """
        if self._version:
            return self._version

        raw_bytes: List[int] = []
        for i in range(16):
            try:
                v = self.register_read(CRUCIAL_REG_DEVICE_VERSION + i)
                raw_bytes.append(v)
            except OSError:
                raw_bytes.append(0)

        # Appliquer la même validation que _probe_device()
        if _is_repeated_pattern(raw_bytes):
            self._version = ""
            return self._version

        chars = []
        for v in raw_bytes:
            if v == 0:
                break                      # Fin de chaîne
            if 0x20 <= v <= 0x7E:         # Vrai caractère ASCII imprimable
                chars.append(chr(v))
            else:
                chars.append(f"\\x{v:02x}")

        ver = "".join(chars).rstrip("\\x00? \x00")
        self._version = ver
        return self._version

    @property
    def label(self) -> str:
        """Libellé court pour l'affichage."""
        ver = self.get_version()
        bus_label = f"i2c-{self.bus_num}"
        if self.bus_name:
            bus_label += f" ({self.bus_name})"
        return f"{bus_label} @ 0x{self.address:02X}  [{ver}]"


__all__ = [
    # Constantes
    "SCAN_ADDRESSES", "DIAG_ADDRESS_RANGES",
    "CRUCIAL_REG_DEVICE_VERSION",
    "CRUCIAL_REG_DIRECT_R", "CRUCIAL_REG_DIRECT_G", "CRUCIAL_REG_DIRECT_B",
    "CRUCIAL_REG_BRIGHTNESS_CTL", "CRUCIAL_REG_BRIGHTNESS_VAL",
    "CRUCIAL_REG_BRIGHTNESS_END",
    "CRUCIAL_REG_LED_SELECT", "CRUCIAL_REG_LED_R", "CRUCIAL_REG_LED_G",
    "CRUCIAL_REG_LED_B", "CRUCIAL_REG_LED_APPLY",
    "NUM_LEDS",
    # ANSI helpers
    "ANSI_RESET", "ANSI_RED", "ANSI_GREEN", "ANSI_YELLOW", "ANSI_BLUE",
    "ANSI_MAGENTA", "ANSI_CYAN", "ANSI_BOLD", "ANSI_DIM",
    "ok", "fail", "warn", "info", "header",
    # Utilitaires
    "bswap16", "_is_repeated_pattern",
    "_check_micron_registers", "_test_write_persistence",
    # Classe principale
    "CrucialStick",
]

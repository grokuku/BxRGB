#!/usr/bin/env python3
"""
ballistix/cli.py — Point d'entrée CLI, auto-élévation sudo,
interface graphique Tkinter, et dispatch des arguments.
"""

from __future__ import annotations

import os
import sys
import glob
import time
import datetime
import subprocess
import shutil
import argparse

# Tkinter est optionnel : le daemon (ballistixd.py) et les modes CLI
# (--diagnostic, --scan-all, --web, etc.) n'en ont pas besoin.
# On importe conditionnellement pour permettre l'usage du package
# sur des systèmes headless sans tkinter installé.
TKINTER_AVAILABLE = False
try:
    import tkinter as tk
    from tkinter import colorchooser, messagebox, ttk
    TKINTER_AVAILABLE = True
except ImportError:
    tk = None  # type: ignore[assignment]
    colorchooser = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

from typing import List, Tuple, Optional

from smbus2 import SMBus

from .core import (
    CrucialStick,
    ok, fail, warn, info, header,
    ANSI_RESET, ANSI_RED, ANSI_GREEN, ANSI_YELLOW, ANSI_BLUE,
    ANSI_MAGENTA, ANSI_CYAN, ANSI_BOLD, ANSI_DIM,
)

from .detect import detect_sticks, list_all_buses, get_smbus_buses
from .diagnostics import (
    diag_smbus_chipset, diag_scan_all, diag_system,
    cli_diagnostic, cli_led_test, cli_dump,
    run_full_diagnostic, _run_cmd,
)


# ──────────────────────────────────────────────────────────────
# Interface graphique Tkinter
# ──────────────────────────────────────────────────────────────

class BallistixApp:
    """Interface graphique pour le contrôle des LEDs Crucial Ballistix."""

    # Palette de couleurs rapides
    QUICK_COLORS = [
        ("Blanc",  255, 255, 255),
        ("Rouge",  255,   0,   0),
        ("Vert",     0, 255,   0),
        ("Bleu",     0,   0, 255),
        ("Jaune",  255, 255,   0),
        ("Cyan",     0, 255, 255),
        ("Magenta",255,   0, 255),
        ("Orange", 255, 128,   0),
        ("Rose",   255,  64, 128),
        ("Noir",     0,   0,   0),
    ]

    def __init__(self, root: tk.Tk, sticks: List[CrucialStick],
                 manual_count: int = 0):
        self.root = root
        self.sticks = sticks
        self.manual_count = manual_count
        self.stick_frames: List[tk.Widget] = []
        self.led_buttons: List[List[tk.Button]] = []
        self.scales: List[tk.Scale] = []

        self.root.title("Crucial Ballistix LED Tester")
        self.root.configure(bg="#1a1a2e")

        # Centrer la fenêtre
        self.root.update_idletasks()
        w, h = 1000, 700
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(800, 500)

        self._build_ui()

    def _build_ui(self) -> None:
        """Construit l'interface complète."""
        # ── En-tête ────────────────────────────────────────────
        header_frame = tk.Frame(self.root, bg="#16213e", pady=12)
        header_frame.pack(fill=tk.X)

        title_frame = tk.Frame(header_frame, bg="#16213e")
        title_frame.pack(fill=tk.X)

        tk.Label(title_frame,
                 text="CRUCIAL BALLISTIX LED TESTER",
                 font=("Helvetica", 20, "bold"),
                 fg="#e94560", bg="#16213e").pack(side=tk.LEFT, padx=(20, 10))

        # Bouton Diagnostic dans le header
        diag_btn = tk.Button(
            title_frame,
            text="🔍 Diagnostic",
            font=("Helvetica", 10, "bold"),
            bg="#2d6a4f", fg="white",
            padx=12, pady=4,
            command=self._run_diagnostic_gui
        )
        diag_btn.pack(side=tk.RIGHT, padx=(10, 20))

        if self.sticks:
            tk.Label(header_frame,
                     text=f"{len(self.sticks)} barrette(s) détectée(s)",
                     font=("Helvetica", 11),
                     fg="#a0a0b0", bg="#16213e").pack()

            if self.manual_count > 0:
                manual_frame = tk.Frame(header_frame, bg="#16213e")
                manual_frame.pack(fill=tk.X)
                tk.Label(manual_frame,
                         text=f"🔧 {self.manual_count} barrette(s) ajoutée(s) manuellement via --add-device",
                         font=("Helvetica", 10, "bold"),
                         fg="#f0c040", bg="#16213e").pack()

            # Vérifier les faux positifs SPD
            spd_suspects = [s for s in self.sticks if "\\x03" in s.get_version() or "\\x00" in s.get_version()]
            if len(self.sticks) > 8 or spd_suspects:
                warn_frame = tk.Frame(header_frame, bg="#16213e")
                warn_frame.pack(fill=tk.X, pady=(4, 0))
                tk.Label(warn_frame,
                         text="⚠ Certains devices détectés ressemblent à des SPD EEPROM (faux positifs).",
                         font=("Helvetica", 10, "bold"),
                         fg="#ffaa00", bg="#16213e").pack()
                tk.Label(warn_frame,
                         text="   Lancez le Diagnostic (🔍) pour investiguer les adresses et bus corrects.",
                         font=("Helvetica", 9),
                         fg="#a0a0b0", bg="#16213e").pack()
        else:
            tk.Label(header_frame,
                     text="⚠ Aucune barrette détectée — vérifiez les permissions",
                     font=("Helvetica", 11),
                     fg="#ff6b6b", bg="#16213e").pack()

        # ── Zone de contenu (scrollable) ──────────────────────
        container = tk.Frame(self.root, bg="#1a1a2e")
        container.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        canvas = tk.Canvas(container, bg="#1a1a2e",
                           highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient=tk.VERTICAL,
                                 command=canvas.yview)
        self.scrollable = tk.Frame(canvas, bg="#1a1a2e")

        self.scrollable.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Activer le scroll à la molette
        def _on_mousewheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Si aucun stick, afficher un message d'aide ────────
        if not self.sticks:
            self._show_no_device_help()
            return

        # ── Créer un panneau par barrette ─────────────────────
        for idx, stick in enumerate(self.sticks):
            self._create_stick_panel(idx, stick)

        # ── Bouton global : Reset All ─────────────────────────
        bottom = tk.Frame(self.root, bg="#1a1a2e", pady=8)
        bottom.pack(fill=tk.X)

        tk.Button(bottom,
                  text="🔴 Reset All (éteindre toutes les LEDs)",
                  font=("Helvetica", 11, "bold"),
                  bg="#e94560", fg="white",
                  padx=20, pady=6,
                  command=self._reset_all).pack(pady=4)

    def _show_no_device_help(self) -> None:
        """Affiche un message d'aide enrichi quand aucun device n'est trouvé."""
        frame = tk.Frame(self.scrollable, bg="#1a1a2e")
        frame.pack(pady=20, fill=tk.X, padx=20)

        tk.Label(frame,
                 text="AUCUNE BARRETTE DÉTECTÉE",
                 font=("Helvetica", 16, "bold"),
                 fg="#e94560", bg="#1a1a2e").pack(pady=(0, 15))

        diag_info = ""

        # Modules i2c chargés
        code, out, err = _run_cmd("lsmod | grep -i i2c")
        if out:
            mods = []
            for line in out.strip().split("\n"):
                parts = line.split()
                if parts:
                    mods.append(parts[0])
            diag_info += f"  Modules I2C : {', '.join(mods)}\n"
        else:
            diag_info += f"  Modules I2C : AUCUN (sudo modprobe i2c-dev)\n"

        # Chipset SMBus
        code, out, err = _run_cmd("lspci 2>/dev/null | grep -i smbus")
        if out:
            for line in out.strip().split("\n"):
                diag_info += f"  Chipset     : {line.strip()}\n"
        else:
            diag_info += f"  Chipset     : non détecté (lspci indisponible ou SMBus désactivé)\n"

        # Bus I2C disponibles avec noms
        all_buses = list_all_buses()
        if all_buses:
            bus_list = [f"i2c-{n}" for n, _ in all_buses]
            diag_info += f"  Bus I2C     : {', '.join(bus_list)}\n"
            for bus_num, name in all_buses:
                diag_info += f"    i2c-{bus_num} → {name}\n"
            smbus_buses = get_smbus_buses()
            if smbus_buses:
                smbus_list = [f"i2c-{n}" for n, _ in smbus_buses]
                diag_info += f"  Bus SMBus   : {', '.join(smbus_list)} (chipset)\n"
                for bus_num, name in smbus_buses:
                    try:
                        test_bus = SMBus(bus_num)
                        test_bus.close()
                        diag_info += f"    i2c-{bus_num} → {name} ✅ accessible\n"
                    except PermissionError:
                        diag_info += f"    i2c-{bus_num} → {name} ❌ Permission refusée\n"
                    except Exception as e:
                        diag_info += f"    i2c-{bus_num} → {name} ❌ {str(e)[:40]}\n"

        i2cdetect_bin = shutil.which("i2cdetect")
        diag_info += f"  i2cdetect   : {'✅ disponible' if i2cdetect_bin else '❌ NON INSTALLÉ'}\n"

        if i2cdetect_bin and all_buses:
            diag_info += f"\n  Commandes utiles :\n"
            smbus_buses = get_smbus_buses()
            if smbus_buses:
                for bus_num, name in smbus_buses:
                    diag_info += f"    sudo i2cdetect -y {bus_num}  ({name})\n"
            else:
                for bus_num, name in all_buses:
                    diag_info += f"    sudo i2cdetect -y {bus_num}  ({name})\n"

        tk.Label(frame,
                 text=diag_info,
                 font=("Courier", 10),
                 fg="#c9d1d9", bg="#0d1117",
                 justify=tk.LEFT,
                 padx=12, pady=12,
                 relief=tk.FLAT, borderwidth=1).pack(fill=tk.X, pady=(0, 12))

        help_text = (
            "\n"
            "VÉRIFICATIONS :\n"
            "\n"
            "  1. Chargez le module i2c-dev :\n"
            "     $ sudo modprobe i2c-dev\n"
            "\n"
            "  2. Exécutez le programme avec sudo :\n"
            "     $ sudo python3 ballistix_tester.py\n"
            "\n"
            "  3. Chargez le driver SMBus du chipset :\n"
            "     Intel : sudo modprobe i2c-i801\n"
            "     AMD   : sudo modprobe i2c-piix4\n"
            "\n"
            "  4. Activez le SMBus dans le BIOS (onglet Advanced/PCH)\n"
            "\n"
            "  5. Scannez les bus SMBus (chipset) avec i2cdetect :\n"
            "     Voir la section \"Commandes utiles\" ci-dessus pour les bus SMBus.\n"
            "     Sur ASUS X570 : sudo i2cdetect -y 9  (PIIX4 port 0)\n"
            "     (cherchez les adresses 0x30-0x37 pour le contrôleur RGB DRAM)\n"
            "\n"
            "  6. Lancez le scan exhaustif :\n"
            "     $ sudo python3 ballistix_tester.py --scan-all\n"
            "\n"
            "  7. Lancez le diagnostic complet :\n"
            "     $ sudo python3 ballistix_tester.py --diagnostic\n"
        )

        tk.Label(frame,
                 text=help_text,
                 font=("Courier", 11),
                 fg="#a0a0b0", bg="#1a1a2e",
                 justify=tk.LEFT).pack()

    def _create_stick_panel(self, idx: int, stick: CrucialStick) -> None:
        """Crée le panneau de contrôle pour une barrette."""
        frame = tk.LabelFrame(
            self.scrollable,
            text=f"  Barrette #{idx + 1} — {stick.label}  ",
            font=("Helvetica", 12, "bold"),
            fg="#e94560", bg="#16213e",
            padx=14, pady=12,
            relief=tk.GROOVE, borderwidth=2
        )
        frame.pack(fill=tk.X, pady=6)
        self.stick_frames.append(frame)

        # ── LEDs ──────────────────────────────────────────────
        leds_frame = tk.Frame(frame, bg="#16213e")
        leds_frame.pack(pady=(0, 8))

        tk.Label(leds_frame, text="LEDs :",
                 font=("Helvetica", 10, "bold"),
                 fg="#a0a0b0", bg="#16213e").grid(row=0, column=0,
                                                   padx=(0, 10), sticky="w")

        btn_row: List[tk.Button] = []
        for led_idx in range(stick.num_leds):
            btn = tk.Button(
                leds_frame,
                width=5, height=2,
                bg="#333344",
                relief=tk.RAISED, borderwidth=2,
                text=str(led_idx + 1),
                fg="#888888",
                font=("Helvetica", 9, "bold"),
                command=lambda s=stick, li=led_idx: self._on_led_click(s, li)
            )
            btn.grid(row=0, column=led_idx + 1, padx=3)
            btn_row.append(btn)

        self.led_buttons.append(btn_row)

        # ── Couleurs rapides ──────────────────────────────────
        quick_frame = tk.Frame(frame, bg="#16213e")
        quick_frame.pack(pady=4)

        tk.Label(quick_frame, text="Rapide :",
                 font=("Helvetica", 10, "bold"),
                 fg="#a0a0b0", bg="#16213e").grid(row=0, column=0,
                                                   padx=(0, 8), sticky="w")

        for ci, (name, r, g, b) in enumerate(self.QUICK_COLORS):
            bg_color = f"#{r:02x}{g:02x}{b:02x}"
            fg_color = "white" if (r * 0.299 + g * 0.587 + b * 0.114) < 128 else "black"
            btn = tk.Button(
                quick_frame,
                text=name[0],
                width=3, height=1,
                bg=bg_color, fg=fg_color,
                font=("Helvetica", 8, "bold"),
                command=lambda s=stick, rr=r, gg=g, bb=b: self._on_quick_color(s, rr, gg, bb)
            )
            btn.grid(row=0, column=ci + 1, padx=1)

        # ── Boutons d'action ──────────────────────────────────
        actions = tk.Frame(frame, bg="#16213e")
        actions.pack(pady=6)

        tk.Button(actions, text="⬜ Test All (Blanc)",
                  font=("Helvetica", 9),
                  bg="#555566", fg="white", padx=8,
                  command=lambda s=stick: self._set_all(s, 255, 255, 255)
                  ).grid(row=0, column=0, padx=3)

        tk.Button(actions, text="🟥 Rouge",
                  font=("Helvetica", 9),
                  bg="#cc3333", fg="white", padx=8,
                  command=lambda s=stick: self._set_all(s, 255, 0, 0)
                  ).grid(row=0, column=1, padx=3)

        tk.Button(actions, text="🟩 Vert",
                  font=("Helvetica", 9),
                  bg="#33cc33", fg="white", padx=8,
                  command=lambda s=stick: self._set_all(s, 0, 255, 0)
                  ).grid(row=0, column=2, padx=3)

        tk.Button(actions, text="🟦 Bleu",
                  font=("Helvetica", 9),
                  bg="#3333cc", fg="white", padx=8,
                  command=lambda s=stick: self._set_all(s, 0, 0, 255)
                  ).grid(row=0, column=3, padx=3)

        tk.Button(actions, text="🔁 Rainbow",
                  font=("Helvetica", 9),
                  bg="#884488", fg="white", padx=8,
                  command=lambda s=stick: self._rainbow(s)
                  ).grid(row=0, column=4, padx=3)

        tk.Button(actions, text="🔄 Reset",
                  font=("Helvetica", 9),
                  bg="#666677", fg="white", padx=8,
                  command=lambda s=stick: self._reset_stick(s)
                  ).grid(row=0, column=5, padx=3)

        # ── Luminosité ────────────────────────────────────────
        bri_frame = tk.Frame(frame, bg="#16213e")
        bri_frame.pack(fill=tk.X, pady=(4, 0))

        tk.Label(bri_frame, text="☀ Luminosité :",
                 font=("Helvetica", 10, "bold"),
                 fg="#a0a0b0", bg="#16213e").pack(side=tk.LEFT, padx=(0, 8))

        scale = tk.Scale(
            bri_frame, from_=0, to=255,
            orient=tk.HORIZONTAL,
            length=300,
            bg="#1a1a2e", fg="#e94560",
            troughcolor="#333344",
            highlightbackground="#16213e",
            command=lambda v, s=stick: self._on_brightness(s, v)
        )
        scale.set(stick.get_brightness())
        scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.scales.append(scale)

        self._update_led_display(idx, stick)

    # ── Callbacks ──────────────────────────────────────────────

    def _on_led_click(self, stick: CrucialStick, led_idx: int) -> None:
        """Ouvre le sélecteur de couleur et applique la couleur choisie."""
        current = stick.colors[led_idx]
        color = colorchooser.askcolor(
            title=f"LED {led_idx + 1} — {stick.label}",
            initialcolor=(current[0], current[1], current[2])
        )
        if color and color[0]:
            r, g, b = [int(c) for c in color[0]]
            stick.set_led(led_idx, r, g, b)
            for idx, s in enumerate(self.sticks):
                if s is stick:
                    self._update_led_display(idx, stick)
                    break

    def _on_quick_color(self, stick: CrucialStick, r: int, g: int, b: int) -> None:
        """Applique une couleur rapide à toutes les LEDs."""
        stick.set_all_leds(r, g, b)
        for idx, s in enumerate(self.sticks):
            if s is stick:
                self._update_led_display(idx, stick)
                break

    def _set_all(self, stick: CrucialStick, r: int, g: int, b: int) -> None:
        """Met toutes les LEDs d'un stick à une couleur."""
        stick.set_all_leds(r, g, b)
        for idx, s in enumerate(self.sticks):
            if s is stick:
                self._update_led_display(idx, stick)
                break

    def _reset_stick(self, stick: CrucialStick) -> None:
        """Éteint toutes les LEDs d'un stick."""
        stick.reset()
        for idx, s in enumerate(self.sticks):
            if s is stick:
                self._update_led_display(idx, stick)
                break

    def _reset_all(self) -> None:
        """Éteint toutes les LEDs de tous les sticks."""
        for stick in self.sticks:
            stick.reset()
        for idx in range(len(self.sticks)):
            self._update_led_display(idx, self.sticks[idx])

    def _rainbow(self, stick: CrucialStick) -> None:
        """Applique un dégradé arc-en-ciel sur les LEDs."""
        import math
        n = stick.num_leds
        for i in range(n):
            hue = (i / n) * 360.0
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
        for idx, s in enumerate(self.sticks):
            if s is stick:
                self._update_led_display(idx, stick)
                break

    def _on_brightness(self, stick: CrucialStick, value: str) -> None:
        """Callback du slider de luminosité."""
        try:
            level = int(float(value))
            stick.set_brightness(level)
        except (ValueError, OSError) as e:
            print(f"⚠ Erreur luminosité : {e}")

    def _run_diagnostic_gui(self) -> None:
        """Lance le diagnostic dans une fenêtre séparée."""
        diag_win = tk.Toplevel(self.root)
        diag_win.title("🔍 Diagnostic — Crucial Ballistix")
        diag_win.configure(bg="#1a1a2e")
        diag_win.geometry("800x600")
        diag_win.minsize(600, 400)

        text_frame = tk.Frame(diag_win, bg="#1a1a2e")
        text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        text_widget = tk.Text(
            text_frame,
            bg="#0d1117", fg="#c9d1d9",
            font=("Courier", 10),
            wrap=tk.WORD,
            padx=10, pady=10,
            relief=tk.FLAT,
            borderwidth=0
        )
        scrollbar = tk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text_widget.yview)
        text_widget.configure(yscrollcommand=scrollbar.set)

        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame = tk.Frame(diag_win, bg="#1a1a2e", pady=8)
        btn_frame.pack(fill=tk.X)

        def _close():
            diag_win.destroy()

        tk.Button(
            btn_frame,
            text="Fermer",
            font=("Helvetica", 10, "bold"),
            bg="#e94560", fg="white",
            padx=20, pady=4,
            command=_close
        ).pack(side=tk.RIGHT, padx=10)

        text_widget.insert(tk.END, "🔍 Diagnostic en cours...\n\n")
        text_widget.see(tk.END)
        diag_win.update()

        import io
        from contextlib import redirect_stdout

        stdout_capture = io.StringIO()

        try:
            with redirect_stdout(stdout_capture):
                report_text = run_full_diagnostic(self.sticks)

            captured = stdout_capture.getvalue()
            text_widget.delete(1.0, tk.END)

            clean_output = captured.replace(ANSI_RESET, "").replace(ANSI_RED, "")
            clean_output = clean_output.replace(ANSI_GREEN, "").replace(ANSI_YELLOW, "")
            clean_output = clean_output.replace(ANSI_BLUE, "").replace(ANSI_MAGENTA, "")
            clean_output = clean_output.replace(ANSI_CYAN, "").replace(ANSI_BOLD, "").replace(ANSI_DIM, "")

            text_widget.insert(tk.END, clean_output)
            text_widget.see(tk.END)

        except Exception as e:
            text_widget.delete(1.0, tk.END)
            text_widget.insert(tk.END, f"Erreur pendant le diagnostic : {e}\n")
            import traceback
            text_widget.insert(tk.END, traceback.format_exc())

    def _update_led_display(self, idx: int, stick: CrucialStick) -> None:
        """Met à jour les couleurs des boutons LED dans l'interface."""
        if idx >= len(self.led_buttons):
            return

        for led_idx in range(min(len(self.led_buttons[idx]), stick.num_leds)):
            r, g, b = stick.colors[led_idx]
            color_hex = f"#{r:02x}{g:02x}{b:02x}"

            try:
                self.led_buttons[idx][led_idx].configure(bg=color_hex)
                brightness = 0.299 * r + 0.587 * g + 0.114 * b
                text_color = "white" if brightness < 128 else "black"
                self.led_buttons[idx][led_idx].configure(
                    fg=text_color,
                    text=str(led_idx + 1)
                )
            except tk.TclError:
                pass


# ──────────────────────────────────────────────────────────────
# Auto-élévation root
# ──────────────────────────────────────────────────────────────

def _needs_root() -> bool:
    """Vérifie si on a vraiment besoin de root en testant l'accès aux bus SMBus."""
    if os.geteuid() == 0:
        return False

    try:
        result = subprocess.run(["i2cdetect", "-l"], capture_output=True, text=True)
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                bus_id = parts[0]
                bus_type = parts[1]
                bus_name = parts[2] if len(parts) > 2 else ""

                if bus_type == "smbus" or "piix4" in bus_name.lower() or "i801" in bus_name.lower():
                    bus_num = int(bus_id.split("-")[1])
                    dev_path = f"/dev/i2c-{bus_num}"
                    try:
                        os.open(dev_path, os.O_RDWR)
                    except PermissionError:
                        return True
                    except:
                        pass
    except FileNotFoundError:
        for dev in sorted(glob.glob("/dev/i2c-*")):
            try:
                fd = os.open(dev, os.O_RDWR)
                os.close(fd)
                return False
            except PermissionError:
                return True
            except:
                continue

    return False


def _elevate_to_root():
    """Se relance avec `sudo -E` si nécessaire, en utilisant `-m ballistix.cli`
    pour préserver les imports relatifs du package.
    """
    if os.geteuid() == 0:
        print(f"  {ok('Élevé en root (conda préservé)')}")
        return

    if not _needs_root():
        return

    python_path = sys.executable
    # Utiliser le répertoire parent de ballistix/ comme cwd pour que
    # `python3 -m ballistix.cli` trouve le package et ses imports relatifs.
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args = sys.argv[1:]

    cmd = ["sudo", "-E", python_path, "-m", "ballistix.cli"] + args

    print(f"{ANSI_BOLD}🔑 Élévation en root nécessaire (accès SMBus)...{ANSI_RESET}")
    print(f"   Commande : {' '.join(cmd)}")
    print(f"   Répertoire : {project_dir}")
    print()

    try:
        result = subprocess.run(cmd, cwd=project_dir)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"{fail('sudo')} n'est pas installé. Installez-le ou ajoutez votre user au groupe i2c :")
        print("  sudo usermod -a -G i2c $USER")
        sys.exit(1)
    except Exception as e:
        msg = f"Échec de l'élévation : {e}"
        print(fail(msg))
        sys.exit(1)


# ──────────────────────────────────────────────────────────────
# Point d'entrée
# ──────────────────────────────────────────────────────────────

def main():
    """Fonction principale avec support des arguments CLI."""
    _elevate_to_root()

    parser = argparse.ArgumentParser(
        description="Crucial Ballistix LED Tester — Diagnostic et contrôle des LEDs de RAM via SMBus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  %(prog)s                          Lance l'interface graphique
  %(prog)s --diagnostic             Mode diagnostic complet (CLI)
  %(prog)s --scan-all               Scan exhaustif tous bus × toutes adresses
  %(prog)s --test-leds              Test automatisé des LEDs (CLI)
  %(prog)s --dump                   Dump des registres dans un fichier (CLI)
  %(prog)s --add-device 9:0x24      Ajouter un device manuellement (i2c-9 @ 0x24)
  %(prog)s --add-device 11:0x10     Ajouter un device manuellement (i2c-11 @ 0x10)
  sudo %(prog)s --diagnostic        Mode diagnostic avec accès root
  sudo %(prog)s --scan-all          Scan exhaustif en root
"""
    )
    parser.add_argument("--diagnostic", action="store_true",
                        help="Mode diagnostic complet (sans interface graphique)")
    parser.add_argument("--scan-all", action="store_true",
                        help="Scan exhaustif de tous les bus I2C sur toutes les adresses (0x08-0x77)")
    parser.add_argument("--test-leds", action="store_true",
                        help="Test automatisé des LEDs (sans interface graphique)")
    parser.add_argument("--dump", action="store_true",
                        help="Dump des registres dans un fichier (sans interface graphique)")
    parser.add_argument("--add-device", action="append",
                        help="Ajouter un device manuellement: bus:0xadr (ex: 9:0x24 ou 11:0x10)")
    parser.add_argument("--web", action="store_true",
                        help="Lancer le serveur web FastAPI")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Hôte du serveur web (défaut: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Port du serveur web (défaut: 8080)")
    parser.add_argument("--open-browser", action="store_true",
                        help="Ouvrir le navigateur au lancement")

    args = parser.parse_args()

    # ── Helper : ajouter les devices manuels ───────────────────
    def _process_manual_devices(sticks: List[CrucialStick]) -> List[CrucialStick]:
        """Ajoute les devices spécifiés via --add-device à la liste des sticks."""
        if not args.add_device:
            return sticks

        print(f"  {info('Ajout manuel de devices (--add-device)...')}")
        for dev_str in args.add_device:
            parts = dev_str.split(":")
            if len(parts) != 2:
                print(f"    {fail(f'Format invalide: {dev_str} (attendu bus:0xadr)')}")
                continue
            try:
                bus_num = int(parts[0])
                addr_str = parts[1]
                if "0x" in addr_str.lower():
                    addr = int(addr_str, 16)
                else:
                    addr = int(addr_str)
            except ValueError:
                print(f"    {fail(f'Format invalide: {dev_str} (attendu bus:0xadr)')}")
                continue

            already_present = any(
                s.bus_num == bus_num and s.address == addr for s in sticks
            )
            if already_present:
                print(f"    {info(f'i2c-{bus_num} @ 0x{addr:02X} déjà détecté, ignoré')}")
                continue

            try:
                bus = SMBus(bus_num)
                stick = CrucialStick(bus, addr, bus_num, version="Manuelle")
                try:
                    ver = stick.get_version()
                    if ver:
                        stick._version = ver
                except Exception:
                    pass
                sticks.append(stick)
                print(f"    {ok(f'Ajouté : i2c-{bus_num} @ 0x{addr:02X}')}")
            except PermissionError:
                print(f"    {fail(f'i2c-{bus_num} @ 0x{addr:02X} : permission refusée')}")
            except Exception as e:
                print(f"    {fail(f'i2c-{bus_num} @ 0x{addr:02X} : {e}')}")

        return sticks

    # ── Mode CLI : diagnostic complet ──────────────────────────
    if args.diagnostic:
        print(ANSI_BOLD + "Recherche de barrettes Crucial Ballistix..." + ANSI_RESET)
        sticks = detect_sticks()
        sticks = _process_manual_devices(sticks)
        cli_diagnostic(sticks)
        for stick in sticks:
            try:
                stick.bus.close()
            except Exception:
                pass
        return

    # ── Mode CLI : test LEDs ───────────────────────────────────
    if args.test_leds:
        print(ANSI_BOLD + "Recherche de barrettes..." + ANSI_RESET)
        sticks = detect_sticks()
        sticks = _process_manual_devices(sticks)
        cli_led_test(sticks)
        for stick in sticks:
            try:
                stick.bus.close()
            except Exception:
                pass
        return

    # ── Mode CLI : dump ────────────────────────────────────────
    if args.dump:
        print(ANSI_BOLD + "Recherche de barrettes..." + ANSI_RESET)
        sticks = detect_sticks()
        sticks = _process_manual_devices(sticks)
        cli_dump(sticks)
        for stick in sticks:
            try:
                stick.bus.close()
            except Exception:
                pass
        return

    # ── Mode CLI : scan exhaustif ──────────────────────────────
    if args.scan_all:
        print(ANSI_BOLD + "=" * 70 + ANSI_RESET)
        print(ANSI_BOLD + "  SCAN EXHAUSTIF — TOUS BUS × TOUTES ADRESSES" + ANSI_RESET)
        print(ANSI_BOLD + "=" * 70 + ANSI_RESET)
        print()

        chipset_diag = diag_smbus_chipset()
        import re
        clean_chipset = re.sub(r'\033\[[0-9;]*m', '', chipset_diag)
        print(clean_chipset)
        print()

        result = diag_scan_all()
        clean_result = re.sub(r'\033\[[0-9;]*m', '', result)
        print(clean_result)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ballistix_scanall_{ts}.txt"
        try:
            with open(filename, "w") as f:
                f.write(clean_chipset + "\n" + clean_result)
            print(f"\n{ok(f'Scan sauvegardé dans {filename}')}")
        except Exception as e:
            print(f"\n{fail(f'Erreur sauvegarde : {e}')}")

        return

    # ── Mode serveur web ──────────────────────────────────────────
    if args.web:
        from .server import run_server, smbus

        # Ajouter les devices manuels avant de démarrer le serveur
        if hasattr(args, 'add_device') and args.add_device:
            manual_devices = []
            for dev_str in args.add_device:
                try:
                    parts = dev_str.split(":")
                    if len(parts) != 2:
                        print(f"  {fail(f'Format invalide: {dev_str} (attendu bus:0xadr)')}")
                        continue
                    bus_num = int(parts[0])
                    addr_str = parts[1]
                    if addr_str.startswith("0x") or addr_str.startswith("0X"):
                        addr = int(addr_str, 16)
                    else:
                        addr = int(addr_str)
                    manual_devices.append({"bus": bus_num, "addr": addr})
                except Exception as e:
                    print(f"  {fail(f'Erreur parsing {dev_str}: {e}')}")

            if manual_devices:
                print(f"  {info('Transmission des devices manuels au serveur...')}")
                smbus.set_manual_devices(manual_devices)
                # Sauvegarder dans la configuration persistée
                from .config import add_manual_device as persist_manual
                for dev in manual_devices:
                    persist_manual(dev["bus"], dev["addr"])
                    print(f"  → Manuel : i2c-{dev['bus']} @ 0x{dev['addr']:02X}")
                smbus.scan()

        run_server(host=args.host, port=args.port, open_browser=args.open_browser)
        return

    # ── Mode normal : interface graphique ──────────────────────
    print("=" * 60)
    print("  CRUCIAL BALLISTIX LED TESTER")
    print("  Contrôle des LEDs de RAM via SMBus")
    print("=" * 60)
    print()

    if os.geteuid() != 0:
        print("⚠ Il est recommandé d'exécuter ce programme avec sudo")
        print("  (accès nécessaire aux bus I2C/SMBus)")
        print("  $ sudo python3 ballistix_tester.py")
        print()

    print("🔍 Recherche de barrettes Crucial Ballistix...")
    print()
    sticks = detect_sticks()
    sticks = _process_manual_devices(sticks)

    print()
    total_attempts = getattr(detect_sticks, 'total_attempts', 0)
    rejected = getattr(detect_sticks, 'rejected', 0)
    manual_count = len(args.add_device) if args.add_device else 0

    if sticks:
        auto_count = len(sticks) - manual_count
        print(f"  {ok(f'{auto_count} device(s) RGB détecté(s) automatiquement')}")
        if manual_count > 0:
            print(f"  {info(f'{manual_count} device(s) ajouté(s) manuellement via --add-device')}")
        for s in sticks:
            print(f"    • {s.label}")
        if total_attempts > len(sticks):
            print(f"  {warn(f'{rejected} device(s) rejeté(s) (SPD/bruit/faux positifs)')}")
    else:
        print(f"  {fail('Aucun vrai contrôleur Crucial Ballistix trouvé.')}")
        if total_attempts > 0:
            print(f"  {warn(f'{total_attempts} device(s) ont répondu mais ont été rejetés (SPD/bruit/faux positifs)')}")
        print()
        print(f"  {info('Pour un diagnostic complet :')}")
        print(f"    $ sudo python3 ballistix_tester.py --scan-all")
        print(f"    $ sudo python3 ballistix_tester.py --diagnostic")
        print()

        import re as _re

        _, mod_out, _ = _run_cmd("lsmod | grep -i i2c")
        if mod_out:
            mods = [l.split()[0] for l in mod_out.strip().split("\n") if l]
            mods_str = ", ".join(mods)
            print(f"  {ok(f'Modules I2C chargés : {mods_str}')}")
        else:
            print(f"  {fail('Aucun module I2C chargé')}")

        _, pci_out, _ = _run_cmd("lspci 2>/dev/null | grep -i smbus")
        if pci_out:
            for line in pci_out.strip().split("\n"):
                cleaned = _re.sub(r'\033\[[0-9;]*m', '', line)
                print(f"  {ok(f'Chipset SMBus : {cleaned.strip()}')}")

        all_buses = list_all_buses()
        if all_buses:
            bus_list = [f"i2c-{n}" for n, _ in all_buses]
            buses_str = ", ".join(bus_list)
            print(f"  {ok(f'Bus I2C trouvés : {buses_str}')}")
            for bus_num, name in all_buses:
                print(f"    i2c-{bus_num} → {name}")
            smbus_buses = get_smbus_buses()
            if smbus_buses:
                print(f"  {ok(f'Bus SMBus (chipset) : {len(smbus_buses)} bus')}")
                for bus_num, name in smbus_buses:
                    print(f"    → sudo i2cdetect -y {bus_num}  ({name})")

        print()
        print(f"  {info('Causes possibles :')}")
        print(f"    1. Module i2c-dev non chargé : sudo modprobe i2c-dev")
        print(f"    2. Contrôleur SMBus désactivé dans le BIOS")
        print(f"    3. Driver SMBus du chipset manquant (i2c-i801 / i2c-piix4)")
        print(f"    4. Pas de barrettes Crucial Ballistix RGB présentes")
        print()

    if not sticks:
        print("✅ Terminé (mode diagnostic).")
        return

    print("🖥️  Lancement de l'interface graphique...")

    if not TKINTER_AVAILABLE:
        no_tk_msg = "Tkinter n'est pas disponible (module tkinter non installé)."
        print(f"  {warn(no_tk_msg)}")
        print(f"  {info('Utilisez --diagnostic, --scan-all ou --web pour le mode CLI/serveur.')}")
        print()
        print("✅ Terminé.")
        return

    display_warning = "Pas d'affichage graphique disponible ($DISPLAY non defini)."
    cli_hint = "Utilisez --diagnostic ou --scan-all pour le mode CLI."
    if "DISPLAY" not in os.environ or not os.environ["DISPLAY"]:
        print(f"  {warn(display_warning)}")
        print(f"  {info(cli_hint)}")
        print(f"  {info('Exemple : sudo python3 ballistix_tester.py --diagnostic')}")
        print()
        print("✅ Terminé.")
        return

    try:
        root = tk.Tk()
    except tk.TclError as e:
        no_display_msg = f"Pas d'affichage graphique disponible (TclError: {e})."
        print(f"  {warn(no_display_msg)}")
        print(f"  {info(cli_hint)}")
        print()
        print("✅ Terminé.")
        return

    style = ttk.Style()
    style.theme_use("clam")

    manual_count = len(args.add_device) if args.add_device else 0
    app = BallistixApp(root, sticks, manual_count=manual_count)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("\nInterruption. Nettoyage...")
    finally:
        for stick in sticks:
            try:
                stick.bus.close()
            except Exception:
                pass

    print("✅ Terminé.")


if __name__ == "__main__":
    main()


__all__ = [
    "BallistixApp",
    "_needs_root", "_elevate_to_root",
    "main",
]

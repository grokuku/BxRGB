#!/bin/bash
# install.sh — Installe ballistixd en service systemd sur CachyOS/Arch
#
# Usage :
#   ./build.sh && sudo ./install.sh
#
# Ce script :
#   1. Copie le binaire dans /usr/local/bin/
#   2. Installe le service systemd
#   3. Active et démarre le service
#   4. Vérifie que le module i2c-dev est chargé

set -e

cd "$(dirname "$0")"

BINARY="dist/ballistixd"
SERVICE_FILE="ballistix-rgb.service"
INSTALL_PATH="/usr/local/bin/ballistixd"
SERVICE_PATH="/etc/systemd/system/ballistix-rgb.service"
USER_NAME="$(logname 2>/dev/null || echo $SUDO_USER)"

echo "╔══════════════════════════════════════════╗"
echo "║  Installation Ballistix RGB Daemon       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Vérifications ──────────────────────────────────────────────
if [ ! -f "$BINARY" ]; then
    echo "❌ Binaire $BINARY introuvable. Lancez ./build.sh d'abord."
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Ce script doit être lancé en root (sudo ./install.sh)"
    exit 1
fi

# ── 1. Module i2c-dev ──────────────────────────────────────────
if ! lsmod | grep -q i2c_dev; then
    echo "📦 Chargement du module i2c-dev..."
    modprobe i2c-dev
    # Persister au reboot
    echo "i2c-dev" > /etc/modules-load.d/i2c-dev.conf
    echo "  ✅ i2c-dev chargé et persisté"
else
    echo "✅ i2c-dev déjà chargé"
fi

# ── 2. Copier le binaire ───────────────────────────────────────
echo "📦 Installation du binaire..."
install -m 755 "$BINARY" "$INSTALL_PATH"
echo "  ✅ $INSTALL_PATH"

# ── 3. Adapter le service avec le bon utilisateur ──────────────
echo "📦 Configuration du service systemd..."
# Remplacer holaf par le vrai utilisateur si différent
if [ -n "$USER_NAME" ] && [ "$USER_NAME" != "holaf" ]; then
    sed "s|/home/holaf|/home/$USER_NAME|g" "$SERVICE_FILE" > /tmp/ballistix-rgb.service
    cp /tmp/ballistix-rgb.service "$SERVICE_PATH"
else
    cp "$SERVICE_FILE" "$SERVICE_PATH"
fi
echo "  ✅ $SERVICE_PATH"

# ── 4. Activer et démarrer ─────────────────────────────────────
echo "📦 Activation du service..."
systemctl daemon-reload
systemctl enable ballistix-rgb
systemctl restart ballistix-rgb
sleep 2

# ── 5. Vérifier ────────────────────────────────────────────────
if systemctl is-active --quiet ballistix-rgb; then
    echo ""
    echo "✅ Service ballistix-rgb démarré avec succès !"
    echo ""
    echo "   Interface web : http://localhost:8080"
    echo "   Logs         : journalctl -u ballistix-rgb -f"
    echo "   Statut       : systemctl status ballistix-rgb"
    echo "   Arrêter      : sudo systemctl stop ballistix-rgb"
    echo "   Désactiver   : sudo systemctl disable ballistix-rgb"
else
    echo ""
    echo "⚠  Le service n'a pas démarré correctement."
    echo "   Logs : journalctl -u ballistix-rgb -n 50"
    exit 1
fi
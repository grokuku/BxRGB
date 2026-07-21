#!/bin/bash
# build.sh — Compile ballistixd en binaire standalone avec PyInstaller
#
# Usage :
#   ./build.sh
#
# Prérequis :
#   pip install pyinstaller
#
# Output :
#   dist/ballistixd  (binaire standalone)

set -e

cd "$(dirname "$0")"

echo "🔨 Build ballistixd avec PyInstaller..."
echo ""

# Vérifier pyinstaller
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "⚠  PyInstaller non installé. Installation..."
    pip install pyinstaller
fi

# Build
pyinstaller ballistixd.spec --clean --noconfirm

echo ""
echo "✅ Build terminé : dist/ballistixd"
echo ""
echo "Pour tester :"
echo "  sudo ./dist/ballistixd --add-device 9:0x20"
echo ""
echo "Pour installer en service systemd :"
echo "  sudo cp dist/ballistixd /usr/local/bin/"
echo "  sudo cp ballistix-rgb.service /etc/systemd/system/"
echo "  sudo systemctl enable --now ballistix-rgb"
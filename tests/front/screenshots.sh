#!/usr/bin/env bash
# Captures 1440×900 de la galerie des thèmes (peu / beaucoup de thèmes).
# Usage (depuis la racine BxRGB) : bash tests/front/screenshots.sh
# Dépendance : /usr/bin/chromium (surcharge : CHROMIUM=…).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8813}"
CHROMIUM="${CHROMIUM:-/usr/bin/chromium}"
OUTDIR="$DIR/screenshots"
mkdir -p "$OUTDIR"

start_server() {
  node "$DIR/server.js" "$PORT" > "$DIR/out/screens-server.log" 2>&1 &
  echo $!
}
stop_server() {
  kill "$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
}
shot() {  # $1 = label, $2 = query
  "$CHROMIUM" --headless=new --no-sandbox --disable-gpu \
    --window-size=1440,900 --virtual-time-budget=25000 \
    --user-data-dir="$(mktemp -d)" \
    --screenshot="$OUTDIR/$1.png" \
    "http://127.0.0.1:${PORT}/?$2" 2>"$OUTDIR/$1.err"
  echo "  $OUTDIR/$1.png"
}

mkdir -p "$DIR/out"
PID="$(start_server)"; sleep 0.5
shot "gallery-3-themes" "tab=kraken&pack=neon"
shot "gallery-3-themes-clair" "tab=kraken&pack=bxrgb-clair"
stop_server "$PID"

PID="$(start_server)"; sleep 0.5
shot "gallery-12-themes" "themes=many&tab=kraken&pack=neon"
stop_server "$PID"

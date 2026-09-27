#!/usr/bin/env bash
# Validation front headless BxRGB — vague 3 (vignettes + galerie compacte).
# Usage (depuis la racine BxRGB) : bash tests/front/runall.sh
# Le serveur mock est redémarré pour chaque scénario SAUF save → save2
# (save2 = « F5 » après save, même état serveur).
# Dépendance : /usr/bin/chromium (surcharge : CHROMIUM=…).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8811}"
CHROMIUM="${CHROMIUM:-/usr/bin/chromium}"
OUTDIR="$DIR/out"
mkdir -p "$OUTDIR"
FAIL=0

start_server() {
  node "$DIR/server.js" "$PORT" > "$OUTDIR/server.log" 2>&1 &
  echo $!
}
stop_server() {
  kill "$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
}

run_mode() {  # $1 = query string, $2 = label
  local query="$1" label="$2"
  local html="$OUTDIR/${label}.html"
  "$CHROMIUM" --headless=new --no-sandbox --disable-gpu \
    --window-size=1440,900 --virtual-time-budget=30000 \
    --user-data-dir="$(mktemp -d)" \
    --dump-dom "http://127.0.0.1:${PORT}/?${query}" > "$html" 2>"$OUTDIR/${label}.err"
  if ! python3 "$DIR/extract.py" "$html" "$label"; then
    FAIL=1
  fi
}

echo "── Scénario socle + Save/Cancel (serveur unique) ──"
PID="$(start_server)"; sleep 0.5
run_mode "test=1" "base"
run_mode "test=save" "save"
run_mode "test=save2" "save2"
stop_server "$PID"

echo "── Scénario Kraken temps réel ──"
PID="$(start_server)"; sleep 0.5
run_mode "test=kraken" "kraken"
stop_server "$PID"

echo "── Scénario Kraken statut illisible (diagnostic) ──"
PID="$(start_server)"; sleep 0.5
run_mode "kraken=empty&test=kraken-empty" "kraken-empty"
stop_server "$PID"

echo "── Scénarios sélecteurs (5 palettes/3 dispositions ; 12/6) ──"
PID="$(start_server)"; sleep 0.5
run_mode "test=themes" "themes-5"
stop_server "$PID"
PID="$(start_server)"; sleep 0.5
run_mode "themes=many&test=themes" "themes-12"
stop_server "$PID"

if [ "$FAIL" -eq 0 ]; then echo "✅ Tous les scénarios sont verts."; else echo "❌ Au moins un scénario a échoué."; fi
exit "$FAIL"

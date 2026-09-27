#!/usr/bin/env python3
"""
animation_probe.py — Outil de mesure TERRAIN du sous-système d'animation LED.

À lancer dans le container qui exécute BxRGB — celui où le SMBus est
accessible (ou depuis n'importe quelle machine pouvant joindre l'API du
container). Dépendances : stdlib Python 3.8+ uniquement (pas de requests) :
rien à installer.

Ce que l'outil mesure, par mode × vitesse (et optionnellement par refresh) :

  - la vitesse RÉELLE du moteur : pente de ``phase`` lue dans
    GET /api/animation/status (doit valoir ~speed demandé) ;
  - la durée de cycle annoncée (``cycle_seconds``) et son évolution à chaud ;
  - le fps de RENDU du serveur (``fps`` du status) ;
  - les ÉCRITURES matérielles / seconde et l'intervalle entre écritures
    (frames WebSocket — le backend ne diffuse qu'au rythme du refresh) ;
  - le delta de couleur entre écritures consécutives (perceptibilité) :
    moyenne/max/percentile 95 par canal, toutes LED confondues.

Lecture des chiffres (seuils détaillés dans docs/ANIMATION-TERRAIN.md) :
  - ``phase/s`` très inférieur à ``speed``  → gel de vitesse (bug) ;
  - ``ΔRVB moy`` < 0.5                      → animation quasi imperceptible ;
  - ``p95 intervalle`` > 2 × moyenne        → à-coups / saccades ;
  - ``fps rendu`` < 10                      → fluidité insuffisante.

Exemples :
    # Catalogue des effets disponibles
    python3 tools/animation_probe.py --list

    # Balayage de vitesses sur tous les effets (6 s par mesure)
    python3 tools/animation_probe.py --speeds 1,3,5

    # Un seul mode, vitesses et refresh comparés, rapport JSON pour archivage
    python3 tools/animation_probe.py --modes rainbow --speeds 1,3,5 \\
        --refreshs 5,20,30 --json > rapport.json

    # Arrêter l'animation courante puis quitter
    python3 tools/animation_probe.py --stop

⚠ L'outil MODIFIE l'état lumière courant (mode/vitesse/refresh). Par défaut il
arrête l'animation en fin de campagne ; ajouter --keep-running pour laisser le
dernier scénario en marche. L'état de référence persisté n'est pas touché :
un « Annuler » (ou Save) depuis l'UI restaure l'état voulu.
"""

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
STATUS_POLL_INTERVAL = 0.25
SETTLE_SECONDS = 0.6


# ═══════════════════════════════════════════════════════════════
# HTTP minimal (stdlib)
# ═══════════════════════════════════════════════════════════════

class ApiError(RuntimeError):
    """Erreur HTTP de l'API BxRGB (statut + corps)."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


def api(base_url, method, path, payload=None, timeout=10.0):
    """Appelle l'API BxRGB et retourne le JSON décodé."""
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise ApiError(exc.code, body) from None
    except urllib.error.URLError as exc:
        raise ApiError(0, str(exc.reason)) from None


# ═══════════════════════════════════════════════════════════════
# Client WebSocket minimal (RFC 6455, texte non fragmenté)
# ═══════════════════════════════════════════════════════════════

class MiniWS:
    """Client WebSocket minimal pour capter les frames ``animation_frame``.

    Gère uniquement ce que le serveur FastAPI/uvicorn émet : trames texte
    non fragmentées, non compressées. Toute erreur de handshake/lecture
    désactive proprement la capture (None) : l'outil continue en HTTP seul.
    """

    def __init__(self, ws_url, timeout=5.0):
        self.sock = None
        self._buf = b""
        parts = urllib.parse.urlparse(ws_url)
        host, port = parts.hostname, parts.port or 80
        path = parts.path or "/ws"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status_line:
            raise RuntimeError(f"handshake WS refusé: {status_line}")

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("connexion WS fermée pendant le handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connexion WS fermée")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv_text(self):
        """Retourne le prochain message texte, None pour les trames ignorées."""
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                raise RuntimeError("WS fermé par le serveur")
            if opcode == 0x9:  # ping → pong
                self._send_pong(payload)
                continue
            if opcode != 0x1:  # texte uniquement
                continue
            return payload.decode("utf-8", "replace")

    def _send_pong(self, payload):
        try:
            frame = bytes([0x8A, len(payload)]) + payload
            self.sock.sendall(frame)
        except OSError:
            pass

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def open_ws(base_url, timeout=3.0):
    """Ouvre le WS de l'API ; retourne None si indisponible."""
    parts = urllib.parse.urlparse(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    ws_url = f"{scheme}://{parts.netloc}/ws"
    try:
        return MiniWS(ws_url, timeout=timeout)
    except Exception as exc:
        print(f"  (capture WS indisponible : {exc})", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════
# Analyse des frames
# ═══════════════════════════════════════════════════════════════

def _percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def frame_color_delta(prev_colors, colors):
    """Delta moyen par canal sur toutes les LED, entre deux dicts de frames.

    ``prev_colors``/``colors`` : {"stick_id": [[r,g,b], ...]}. Retourne
    (mean_delta, max_delta) ; (0,0) si la comparaison est impossible.
    """
    total, count, maximum = 0, 0, 0
    for stick_id, leds in colors.items():
        previous = prev_colors.get(stick_id)
        if not previous:
            continue
        for i, led in enumerate(leds):
            if i >= len(previous):
                continue
            for channel in range(3):
                try:
                    delta = abs(int(led[channel]) - int(previous[i][channel]))
                except (TypeError, ValueError, IndexError):
                    continue
                total += delta
                count += 1
                maximum = max(maximum, delta)
    return (total / count if count else 0.0), maximum


# ═══════════════════════════════════════════════════════════════
# Un scénario de mesure
# ═══════════════════════════════════════════════════════════════

def run_scenario(base_url, mode, speed, refresh, duration, use_ws=True):
    """Démarre (hot-swap) un mode puis mesure pendant ``duration`` secondes."""
    body = {"mode": mode, "speed": speed}
    if refresh is not None:
        body["refresh"] = refresh
    api(base_url, "POST", "/api/animation/start", body)
    time.sleep(SETTLE_SECONDS)

    ws = open_ws(base_url) if use_ws else None
    start_wall = time.monotonic()
    status_first = api(base_url, "GET", "/api/animation/status")
    start_phase = status_first.get("phase")
    start_writes = status_first.get("writes")
    start_frames = status_first.get("frames")

    fps_samples = []
    phase_samples = []
    frame_times = []
    deltas = []
    max_deltas = []
    prev_colors = {}
    last_status = status_first

    ws_timeout = ws.sock.gettimeout() if ws is not None else None
    try:
        while time.monotonic() - start_wall < duration:
            # 1) Draining des frames WS (non bloquant, petite fenêtre).
            if ws is not None:
                ws.sock.settimeout(0.02)
                deadline = time.monotonic() + 0.15
                while time.monotonic() < deadline:
                    try:
                        message = ws.recv_text()
                    except (socket.timeout, TimeoutError):
                        break
                    except RuntimeError:
                        ws.close()
                        ws = None
                        break
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") != "animation_frame" or not data.get("colors"):
                        continue
                    delta, delta_max = frame_color_delta(prev_colors, data["colors"])
                    prev_colors = {k: v for k, v in data["colors"].items()}
                    frame_times.append(time.monotonic())
                    deltas.append(delta)
                    max_deltas.append(delta_max)
            # 2) Échantillon de statut.
            try:
                last_status = api(base_url, "GET", "/api/animation/status")
            except ApiError as exc:
                print(f"  ⚠ status illisible : {exc}", file=sys.stderr)
                break
            if last_status.get("fps") is not None:
                fps_samples.append(float(last_status.get("fps") or 0.0))
            if last_status.get("phase") is not None:
                phase_samples.append((time.monotonic(),
                                      float(last_status["phase"])))
            time.sleep(STATUS_POLL_INTERVAL)
    finally:
        if ws is not None:
            ws.close()

    elapsed = time.monotonic() - start_wall
    end_phase = last_status.get("phase")
    phase_rate = None
    if (phase_samples and start_phase is not None and end_phase is not None
            and elapsed > 0):
        phase_rate = (float(end_phase) - float(start_phase)) / elapsed

    write_intervals = [
        frame_times[i] - frame_times[i - 1] for i in range(1, len(frame_times))
    ]
    writes_measured = len(frame_times) / elapsed if elapsed > 0 else 0.0
    writes_status = None
    if start_writes is not None and last_status.get("writes") is not None:
        writes_status = (last_status["writes"] - start_writes) / elapsed
    frames_status = None
    if start_frames is not None and last_status.get("frames") is not None:
        frames_status = (last_status["frames"] - start_frames) / elapsed

    return {
        "mode": mode,
        "speed_requested": speed,
        "refresh_requested": refresh,
        "actual_speed": last_status.get("speed"),
        "speed_measured": (round(phase_rate, 3)
                           if phase_rate is not None else None),
        "cycle_seconds": last_status.get("cycle_seconds"),
        "framerate_status": last_status.get("framerate"),
        "fps_status_mean": (round(sum(fps_samples) / len(fps_samples), 2)
                            if fps_samples else None),
        "frames_per_s_status": (round(frames_status, 2)
                                if frames_status is not None else None),
        "writes_per_s_status": (round(writes_status, 2)
                                if writes_status is not None else None),
        "writes_per_s_measured": round(writes_measured, 2),
        "ws_frames": len(frame_times),
        "write_interval_mean_ms": (
            round(1000 * sum(write_intervals) / len(write_intervals), 1)
            if write_intervals else None),
        "write_interval_p95_ms": (
            round(1000 * _percentile(write_intervals, 0.95), 1)
            if write_intervals else None),
        "color_delta_mean": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "color_delta_max": max(max_deltas) if max_deltas else None,
        "duration_s": round(elapsed, 2),
    }


# ═══════════════════════════════════════════════════════════════
# Rapport
# ═══════════════════════════════════════════════════════════════

def verdict(result):
    """Conseils lisibles dérivés des mesures."""
    notes = []
    speed = result.get("speed_measured")
    requested = result.get("speed_requested")
    if speed is None:
        notes.append("vitesse non mesurable (status sans phase)")
    elif requested and abs(speed - requested) > max(0.15, 0.2 * requested):
        notes.append(f"⚠ vitesse mesurée {speed}/s ≠ demandée {requested} "
                     "(gel de vitesse ?)")
    dps = result.get("writes_per_s_measured") or 0
    if result.get("ws_frames", 0) == 0:
        notes.append("aucune frame WS captée (refresh ? WS coupé ?)")
    elif dps < 5:
        notes.append("⚠ < 5 écritures/s : risque de saccades")
    delta = result.get("color_delta_mean")
    if delta is not None and delta < 0.5:
        notes.append("⚠ ΔRVB moyen < 0.5 : animation quasi imperceptible")
    p95 = result.get("write_interval_p95_ms")
    mean = result.get("write_interval_mean_ms")
    if p95 and mean and p95 > 2 * mean:
        notes.append("⚠ intervalles irréguliers (p95 > 2× moyenne) : à-coups")
    fps = result.get("fps_status_mean")
    if fps is not None and 0 < fps < 10:
        notes.append("⚠ fps de rendu < 10 : baisser framerate/charge")
    if not notes:
        notes.append("OK")
    return " ; ".join(notes)


def print_report(results):
    header = (f"{'mode':<15} {'speed':>6} {'vit.mes.':>9} {'cycle_s':>8} "
              f"{'fps':>6} {'écr/s':>7} {'ΔRVB moy':>9} {'Δmax':>6} "
              f"{'p95 ms':>7}")
    print()
    print("═" * len(header))
    print(header)
    print("─" * len(header))
    for r in results:
        def fmt(value, digits=2, dash="—"):
            return dash if value is None else f"{value:.{digits}f}"
        print(f"{r['mode']:<15} {fmt(r['speed_requested'], 1):>6} "
              f"{fmt(r['speed_measured'], 2):>9} {fmt(r['cycle_seconds'], 2):>8} "
              f"{fmt(r['fps_status_mean'], 1):>6} "
              f"{fmt(r['writes_per_s_measured'], 1):>7} "
              f"{fmt(r['color_delta_mean'], 3):>9} "
              f"{fmt(r['color_delta_max'], 1):>6} "
              f"{fmt(r['write_interval_p95_ms'], 1):>7}")
    print("═" * len(header))
    print()
    for r in results:
        print(f"• {r['mode']} @ speed {r['speed_requested']} → {verdict(r)}")


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def _csv_floats(raw, default):
    if raw is None:
        return default
    return [float(part) for part in str(raw).split(",") if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mesure terrain du moteur d'animation LED BxRGB.")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"base de l'API (défaut : {DEFAULT_URL})")
    parser.add_argument("--duration", type=float, default=6.0,
                        help="durée de mesure par scénario, en secondes (6)")
    parser.add_argument("--modes", default=None,
                        help="modes à mesurer, séparés par des virgules "
                             "(défaut : tous les effets du catalogue)")
    parser.add_argument("--speeds", default="1,3,5",
                        help="vitesses comparées (défaut : 1,3,5)")
    parser.add_argument("--refreshs", default=None,
                        help="refresh SMBus comparés (ex. 5,20,30), mesurés à "
                             "--refresh-speed ; défaut : aucun balayage")
    parser.add_argument("--refresh-speed", type=float, default=1.0,
                        help="vitesse des scénarios de refresh (défaut : 1)")
    parser.add_argument("--no-ws", action="store_true",
                        help="ne pas ouvrir le WebSocket (mesures status seules)")
    parser.add_argument("--keep-running", action="store_true",
                        help="laisser le dernier scénario en marche (défaut : stop)")
    parser.add_argument("--list", action="store_true",
                        help="afficher le catalogue/statut et quitter")
    parser.add_argument("--stop", action="store_true",
                        help="arrêter l'animation courante et quitter")
    parser.add_argument("--json", action="store_true",
                        help="sortie JSON (rapport machine) au lieu du tableau")
    args = parser.parse_args(argv)

    try:
        if args.list:
            effects = api(args.url, "GET", "/api/animation/effects")
            status = api(args.url, "GET", "/api/animation/status")
            if args.json:
                print(json.dumps({"effects": effects, "status": status},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"États disponibles ({args.url}) :")
                for effect in effects:
                    params = ", ".join(p["id"] for p in effect.get("params", []))
                    print(f"  - {effect['id']:<15} {effect['label']:<15} "
                          f"params: {params or '—'}")
                print(f"\nÉtat courant : mode={status.get('mode')} "
                      f"running={status.get('running')} "
                      f"speed={status.get('speed')} "
                      f"refresh={status.get('refresh')} "
                      f"cycle={status.get('cycle_seconds')} "
                      f"fps={status.get('fps')}")
            return 0

        if args.stop:
            status = api(args.url, "POST", "/api/animation/stop", {})
            print(f"Animation arrêtée (mode={status.get('mode')}, "
                  f"running={status.get('running')}).")
            return 0

        catalogue = api(args.url, "GET", "/api/animation/effects")
        known = [effect["id"] for effect in catalogue]
        if args.modes:
            modes = [m.strip() for m in args.modes.split(",") if m.strip()]
            unknown = [m for m in modes if m not in known]
            if unknown:
                print(f"⚠ modes inconnus : {', '.join(unknown)} "
                      f"(catalogue : {', '.join(known)})", file=sys.stderr)
                return 2
        else:
            modes = known

        speeds = _csv_floats(args.speeds, [1.0, 3.0, 5.0])
        refreshs = _csv_floats(args.refreshs, [])

        scenarios = [(mode, speed, None)
                     for mode in modes for speed in speeds]
        for refresh in refreshs:
            for mode in modes:
                scenarios.append((mode, args.refresh_speed, refresh))

        print(f"Campagne : {len(scenarios)} scénario(s), "
              f"{args.duration:g} s chacun, WS={'non' if args.no_ws else 'oui'}")
        results = []
        for index, (mode, speed, refresh) in enumerate(scenarios, 1):
            label = f"refresh={refresh:g}" if refresh is not None else \
                f"speed={speed:g}"
            print(f"[{index}/{len(scenarios)}] {mode} @ {label} …",
                  flush=True)
            try:
                results.append(run_scenario(args.url, mode, speed, refresh,
                                            args.duration, use_ws=not args.no_ws))
            except ApiError as exc:
                print(f"  ⚠ scénario ignoré : {exc}", file=sys.stderr)

        if not results:
            print("Aucune mesure : vérifier le serveur.", file=sys.stderr)
            return 1

        if not args.keep_running:
            try:
                api(args.url, "POST", "/api/animation/stop", {})
                stopped = True
            except ApiError:
                stopped = False
        else:
            stopped = False

        payload = {
            "url": args.url,
            "duration_s": args.duration,
            "stopped_at_end": stopped,
            "results": results,
            "verdicts": {f"{r['mode']}@{r['speed_requested']}"
                         + (f"/r{r['refresh_requested']}"
                            if r["refresh_requested"] is not None else ""):
                         verdict(r) for r in results},
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_report(results)
            if stopped:
                print("\n(animation arrêtée en fin de campagne — "
                      "--keep-running pour la laisser en marche)")
        return 0
    except ApiError as exc:
        print(f"❌ API injoignable ou en erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

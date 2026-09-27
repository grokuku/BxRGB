# BxRGB — Ballistix RGB LED Controller

Contrôle des LEDs de barrettes Crucial Ballistix (et de l'écran LCD du
Kraken NZXT via `liquidctl`) à travers un serveur web FastAPI : API REST +
WebSocket + front statique servi depuis `static/`.

## Lancement en développement (sans matériel réel)

```bash
python3 -m ballistix.server           # http://localhost:8080
# ou via le daemon (devices manuels) :
python3 ballistixd.py --add-device 9:0x20
```

## Déploiement (conteneur)

Le déploiement réel est **conteneurisé** : il n'y a **ni service systemd ni
installation bare-metal**. Le cycle est :

1. développer, puis `git push` sur `main` ;
2. la CI GitHub Actions **construit et publie l'image Docker** (voir
   `.github/workflows/`) ;
3. l'hôte **récupère l'image** puis **recrée le conteneur** (`down` / `pull` /
   `up -d`) via son outil habituel.

`docker-compose.yml` décrit le service :

| Paramètre | Valeur |
|---|---|
| Image | `ghcr.io/grokuku/bxrgb:latest` |
| Mode réseau | `network_mode: host` (UI sur `http://localhost:8080`) |
| Privilèges | `privileged: true` (accès SMBus/I2C et liquidctl) |
| Montages | `/sys:ro`, `/proc:ro`, `/dev`, `~/.config/ballistix` → `/root/.config/ballistix` |

`config.json` (état persisté) vit dans `~/.config/ballistix` côté hôte. Le
`Dockerfile` (base `python:3.11-slim`) construit l'image et démarre
`python -m ballistix.server`.

> ⚠ Le tag `latest` référencé par `docker-compose.yml` est publié par le
> workflow **Release** (`.github/workflows/release.yml`, manuel). Un simple
> push sur `main` déclenche `ci.yml`, qui publie les tags `test` et
> `sha-<short>` — pas `latest`. Vérifier quel tag votre outil de déploiement
> récupère effectivement.

## Tests

```bash
python3 -m pytest tests/ -q      # backend
bash tests/front/runall.sh       # front headless (Node + Chromium)
```

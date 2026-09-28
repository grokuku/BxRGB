# Harnais de validation front (headless)

Validation E2E du front web BxRGB **sans backend réel** : un serveur mock Node
sert `static/`, simule l'API `/api/*` et injecte les scripts d'assertion dans
la page, exécutée par Chromium headless. Couvre les vagues 1–3 du chantier
« temps réel + Save/Cancel » (socle, save/restore, Kraken temps réel,
sélecteurs Palette × Disposition + galerie compacte) **et** la refonte du
sous-système d'animations LED (hot-swap, dirty lighting, vitesse temps réel).

## Lancement

Depuis la racine du repo `BxRGB/` :

```bash
bash tests/front/runall.sh
```

La suite est verte si la dernière ligne affiche
`✅ Tous les scénarios sont verts.` — le décompte de checks par scénario
(et par assertion) est affiché en direct.

Dépendances : **Node** (mock) et **Chromium** headless (`/usr/bin/chromium`
par défaut, surchargeable via `CHROMIUM=/chemin/chromium`).

## Scénarios — 11 au total, 243 checks

| Scénario | Requête URL | Checks |
|---|---|---|
| `base` | `?test=1` | 20/20 |
| `save` | `?test=save` | 16/16 |
| `save2` | `?test=save2` | 11/11 |
| `kraken` | `?test=kraken` | 17/17 |
| `kraken-empty` | `?kraken=empty&test=kraken-empty` | 9/9 |
| `anim` | `?test=anim&anim=fake` | 46/46 |
| `anim2` | `?test=anim2` | 12/12 |
| `themes-5` | `?test=themes` | 25/25 |
| `themes-12` | `?themes=many&test=themes` | 26/26 |
| `timezone` | `?test=timezone` | 31/31 |
| `timezone-fallback` | `?tz=empty&test=timezone` | 30/30 |
| **Total** | | **243** |

Les paires `save → save2` et `anim → anim2` partagent volontairement le **même
serveur mock** : le second scénario simule un « F5 » après le premier et vérifie
l'hydratation depuis la référence (aucun faux dirty). Tous les autres scénarios
démarrent un serveur neuf.

## Contenu

| Fichier | Rôle |
|---|---|
| `runall.sh` | Lance les 11 scénarios sur des serveurs mock (partagés pour `save/save2` et `anim/anim2`, neufs sinon). |
| `server.js` | Serveur mock : `static/` + API `/api/*` + injection de `test.js`. La racine BxRGB est déduite de `tests/front/` (surcharge : `BXRGB_DIR`). `?tz=empty` simule une base tzdata absente (catalogue de fuseaux vide → repli front). |
| `test.js` | Assertions injectées (`?test=1|save|save2|kraken|kraken-empty|themes|anim|anim2|timezone`), résultat dans `<pre id="__result">`. |
| `extract.py` | Extrait et résume ce JSON depuis le `--dump-dom` de Chromium. |
| `screenshots.sh` | Captures 1440×900 des sélecteurs palette/disposition (5 / 12 palettes) pour revue visuelle. |
| `make_fixtures.py` | Génère les PNG de `fixtures/` avec le **vrai moteur PIL** (`ballistix/monitor.py`) : vignettes de palette (5 réelles + 7 factices) et de disposition (3 réelles + 3 factices). |
| `fixtures/*.png` | Vignettes de palette `<key>.png`, de disposition `layout-<key>.png`, et `preview.png` 640×640. |

## Pièges connus

- **Le serveur mock garde son état entre deux runs Chromium** (comme un F5) :
  changer de scénario sans redémarrer `server.js` ferait fuiter l'état du
  précédent. `runall.sh` redémarre un serveur neuf par scénario (sauf
  `save → save2` et `anim → anim2`, volontairement sur le même serveur).
- Les fixtures dépendent du **moteur de rendu** : elles doivent être
  **régénérées après toute modification de `ballistix/monitor.py`** (ou de
  `make_fixtures.py`) via `python3 tests/front/make_fixtures.py`. Les clés de
  fixtures doivent rester alignées sur `server.js` (`REAL_PALETTES` /
  `FAKE_PALETTES` / `REAL_LAYOUTS` / `FAKE_LAYOUTS`).
- Le cache de vignettes backend s'invalide seul (empreinte du contenu de
  `monitor.py`) ; le mock, lui, sert `fixtures/` tel quel.
- Sorties générées, non versionnées : `out/` (DOM + logs) et `screenshots/`.

## Python / pytest

Aucun `test_*.py` ici : `python3 -m pytest tests/` ne collecte rien dans ce
dossier (les tests backend vivent dans `tests/test_*.py`).

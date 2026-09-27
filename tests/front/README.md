# Harnais de validation front (headless)

Validation E2E du front web BxRGB **sans backend réel** : un serveur mock Node
sert `static/`, simule l'API `/api/*` et injecte les scripts d'assertion dans
la page, exécutée par Chromium headless. Couvre les vagues 1–3 du chantier
« temps réel + Save/Cancel » (socle, save/restore, Kraken temps réel,
sélecteurs Palette × Disposition + galerie compacte).

## Lancement

Depuis la racine du repo `BxRGB/` :

```bash
bash tests/front/runall.sh
```

La suite est verte si la dernière ligne affiche
`✅ Tous les scénarios sont verts.` — le décompte de checks par scénario est
affiché en direct (base, save, save2, kraken, kraken-empty, themes-5, themes-12).

Dépendances : **Node** (mock) et **Chromium** headless (`/usr/bin/chromium`
par défaut, surchargeable via `CHROMIUM=/chemin/chromium`).

## Contenu

| Fichier | Rôle |
|---|---|
| `runall.sh` | Lance les 7 scénarios sur 6 serveurs mock (voir pièges). |
| `server.js` | Serveur mock : `static/` + API `/api/*` + injection de `test.js`. La racine BxRGB est déduite de `tests/front/` (surcharge : `BXRGB_DIR`). |
| `test.js` | Assertions injectées (`?test=1|save|save2|kraken|kraken-empty|themes`), résultat dans `<pre id="__result">`. |
| `extract.py` | Extrait et résume ce JSON depuis le `--dump-dom` de Chromium. |
| `screenshots.sh` | Captures 1440×900 des sélecteurs palette/disposition (5 / 12 palettes) pour revue visuelle. |
| `make_fixtures.py` | Génère les PNG de `fixtures/` avec le **vrai moteur PIL** (`ballistix/monitor.py`) : vignettes de palette (5 réelles + 7 factices) et de disposition (3 réelles + 3 factices). |
| `fixtures/*.png` | Vignettes de palette `<key>.png`, de disposition `layout-<key>.png`, et `preview.png` 640×640. |

## Pièges connus

- **Le serveur mock garde son état entre deux runs Chromium** (comme un F5) :
  changer de scénario sans redémarrer `server.js` ferait fuiter l'état du
  précédent. `runall.sh` redémarre un serveur neuf par scénario (sauf
  `save → save2`, volontairement sur le même serveur).
- Les fixtures doivent être **régénérées après toute modification du rendu**
  (`ballistix/monitor.py`) : `python3 tests/front/make_fixtures.py`.
- Le cache de vignettes backend s'invalide seul (empreinte du contenu de
  `monitor.py`) ; le mock, lui, sert `fixtures/` tel quel.
- Sorties générées, non versionnées : `out/` (DOM + logs) et `screenshots/`.

## Python / pytest

Aucun `test_*.py` ici : `python3 -m pytest tests/` ne collecte rien dans ce
dossier (les tests backend vivent dans `tests/test_*.py`).

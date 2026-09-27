# Protocole de validation terrain — animations LED BxRGB

> But : vérifier **avec le matériel**, **dans le container qui exécute BxRGB**
> (celui où les barrettes RGB et le contrôleur Kraken sont accessibles), que le
> moteur d'animation est perceptible, fluide, et que les réglages (vitesse,
> refresh, paramètres d'effet) agissent **en direct**.
>
> Le container de développement (sans SMBus ni liquidctl) ne le permet pas :
> tout ce document s'exécute **dans le container BxRGB, pas sur le PC hôte**.

Commande pour y accéder : passez par **votre accès habituel au container**
(shell interactif / console). Forme générique d'exemple — la commande exacte
dépend de la façon dont le container est lancé (moteur, nom, options) :

```bash
<outil-container> exec -it <nom-du-container> bash
```

Une fois dans le container, placez-vous à la racine du projet pour les
commandes ci-dessous.

## 0. Pré-requis

- BxRGB en marche **dans son container** — le daemon y tourne selon **votre
  outil habituel de lancement du container** (`python3 ballistixd.py` ou
  `python -m ballistix.server` selon votre montage) — et son UI web est
  accessible.
- Récupérer la version de l'outil : `python3 tools/animation_probe.py --help`
  (stdlib uniquement, aucune installation).
- Avoir des LEDs visibles (barrettes installées, luminosité > 0).

## 1. Contrôles rapides (30 s)

```bash
# Catalogue exposé par le backend (source unique de l'UI)
curl -s localhost:8080/api/animation/effects | python3 -m json.tool

# État courant (mode, running, cycle, fps)
curl -s localhost:8080/api/animation/status | python3 -m json.tool
```

Attendu : 2 effets (`incandescence`, `rainbow`), `mode: "static"` au repos,
`running: false`.

## 2. Campagne de mesures (outil)

```bash
# 1) Vue d'ensemble (6 s par mesure, WS activé) — arrête l'animation à la fin
python3 tools/animation_probe.py --speeds 1,3,5

# 2) Effet unique, vitesses comparées
python3 tools/animation_probe.py --modes rainbow --speeds 1,3,5

# 3) Refresh SMBus comparé (à vitesse 1) : 5 vs 20 vs 30 écritures/s
python3 tools/animation_probe.py --refreshs 5,20,30

# 4) Rapport archivé
python3 tools/animation_probe.py --speeds 1,3,5 --json > /tmp/anim-$(date +%F).json
```

Colonnes du tableau :

| Colonne | Sens | Attendu |
|---|---|---|
| `vit.mes.` | pente réelle de `phase` (phase/s) | ≈ `speed` demandé (±20 %) |
| `cycle_s` | `cycle_seconds` du status | ≈ cycle de base ÷ speed |
| `fps` | fps de **rendu** serveur | ≥ 20 en nominal |
| `écr/s` | **écritures** matérielles/s (frames WS) | ≈ refresh demandé |
| `ΔRVB moy` | écart moyen par canal entre 2 écritures | > 1 (visible) |
| `Δmax` | plus grand écart par canal | > 10 sur un cycle |
| `p95 ms` | intervalle écritures, 95ᵉ percentile | < 2 × moyenne |

## 3. Ce qu'il faut OBSERVER à l'œil

- **Incandescence** : respiration lente, organique, désynchronisée d'une LED à
  l'autre — pas un clignotement global. Si l'ensemble clignote en bloc,
  vérifier que `sparkle` est faible et que la teinte de base n'est pas noire.
- **Rainbow** : **défilement** continu des teintes (dérive), pas de saut
  brutal. Un arc qui « tourne » de façon régulière = OK.
- **Stroboscope** : scintillement perçu comme un clignotement dur →
  baisser `sparkle` (voir §4).
- **Fluidité** : pas de à-coups visibles ; comparer avec `fps` et `écr/s`.
- **Vitesse** : à speed 3, l'œil doit voir une animation nettement plus rapide
  qu'à speed 1 (et le cycle affiché dans l'UI doit diminuer d'autant).

## 4. Où régler amplitude / scintillement / cycle

Deux voies équivalentes :

1. **UI** : carte « 🎬 Animation » → ouvrir **⚙ Réglages de l'effet**.
   - Incandescence : `cycle_seconds` (durée du cycle), `min_brightness` /
     `max_brightness` (amplitude), `sparkle` (scintillement), `seed`.
   - Rainbow : `period_seconds` (période), `hue_spread` (étalement des teintes).
   - Les réglages s'appliquent **à chaud** (hot-swap, phase réinitialisée) et
     entrent dans le dirty → « Enregistrer » pour les figer, « Annuler » pour
     revenir à la référence.
2. **API/cfg** : `POST /api/animation/start {mode, params:{…}}` ; persisté dans
   `config.json` → `lighting.params` via Save.

Réglages recommandés si problème :

| Symptôme | Réglage |
|---|---|
| Scintillement stroboscopique | `sparkle` → 0.05–0.10 |
| Trop sombre / peu visible | `min_brightness` → 0.35, `max_brightness` → 1.0 |
| Respiration trop rapide | `cycle_seconds` → 4–8 s |
| Rainbow trop rapide | `period_seconds` → 12–20 s |
| Défilement trop serré | `hue_spread` → 0.5–0.7 |

## 5. Vérifier que le slider change la vitesse EN DIRECT

1. Lancer un effet, noter la ligne « cycle ≈ x,x s » sous le slider.
2. Glisser **Vitesse** de 1 à 3 (sans Stop) : le libellé passe à `3.0×` et le
   cycle affiché devient ≈ cycle base ÷ 3 (ex. 6,0 s → 2,0 s).
3. Dans le même temps, l'œil doit voir l'accélération **sans redémarrage**
   (pas de coupure de couleur).
4. Confirmer côté mesure : `vit.mes.` ≈ vitesse demandée dans la colonne du
   tableau (sinon → le moteur gèle la vitesse : bug à signaler).
5. Même test pour **Refresh SMBus** : l'œil ne doit pas voir de différence de
   *vitesse* (le refresh change la fréquence d'écriture, pas la phase), mais
   `écr/s` doit suivre le slider.

## 6. Seuils de décision

| Mesure | OK | Alerte | Action |
|---|---|---|---|
| `vit.mes.` vs demandé | ±20 % | écart > 20 % | bug moteur → logs serveur |
| `fps` rendu | ≥ 20 | 10–20 | baisser `framerate` (moins de charge) |
| `fps` rendu | ≥ 20 | < 10 | baisser vitesse/refresh, vérifier charge CPU |
| `écr/s` vs refresh | ±25 % | < 5 | monter le refresh ou vérifier SMBus |
| `ΔRVB moy` | > 1 | < 0.5 | augmenter amplitude (`min/max_brightness`) ou `hue_spread` |
| `p95 ms` | < 2 × moy. | > 2 × moy. | à-coups : baisser refresh/framerate |
| Perception | dérive lente / respiration | stroboscope | baisser `sparkle` |

## 7. Restaurer l'état voulu

L'outil (sauf `--keep-running`) **arrête** l'animation en fin de campagne :
l'écran repasse aux couleurs de base. Pour revenir à l'état voulu :
bouton « ↩ Annuler » (référence enregistrée) ou re-sélectionner le mode.

## 8. Ce qui n'est PAS mesurable ici

- Le rendu optique réel (teintes perçues, gamma des barrettes), la latence
  SMBus physique, la stabilité thermique : se jugent à l'œil, §3.
- Le débit réel du bus SMBus : l'outil mesure ce que le serveur expose
  (`writes`/frames WS), pas les transactions électriques.
- La reprise au boot (`running=true`) : à vérifier par un redémarrage réel
  **du container** — l'UI doit réafficher le mode actif, le cycle et aucun
  faux dirty.

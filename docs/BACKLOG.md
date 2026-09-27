# Backlog BxRGB

Décisions et points ouverts actés, à traiter dans l'ordre voulu.
Dernière mise à jour : 2026-09-27.

---

## 1. « Fausse 24 fps » — animation continue par GIFs (à évaluer avant toute implémentation)

### Objectif

Faire croire à l'écran LCD du Kraken Z53 qu'il joue une animation fluide :
générer (ou re-générer) des GIFs en continu et les **uploader en boucle**
vers l'écran, de sorte que la succession d'images donne l'illusion d'une
lecture vidéo. Le firmware, lui, ne sait jouer qu'un GIF à la fois.

### Condition impérative de faisabilité

**Ne rien implémenter avant d'avoir MESURÉ le temps de bascule entre deux
GIFs via liquidctl.** Si le switch n'est pas quasi immédiat, chaque
changement d'image crée un à-coup et l'animation est hachée : la fonction
est inutilisable (« fonction morte ») et le chantier s'arrête là.

### Protocole de test à mener

1. **Banc de mesure du switch** : uploader le GIF A, attendre stabilisation,
   puis chronométrer `liquidctl ... set lcd screen <gif B>` jusqu'au retour
   de la commande ; répéter ≥ 20 fois (médiane + p95), sur 2–3 tailles de
   GIF (petit 640×640 peu de frames, moyen, lourd).
2. **Mesure du plancher de cadence** : en déduire la fréquence maximale
   réellement atteignable `f_max = 1 / temps_bascule_median` et la durée
   minimale utile d'un GIF (le temps que le firmware met à le démarrer /
   le jouer avant qu'on puisse le remplacer).
3. **Recherche du meilleur compromis fps / durée** : balayer des couples
   (fps cible, durée du GIF, poids, nombre de frames) et retenir le point
   qui maximise la fluidité perçue sans retard cumulé : au-delà d'un seuil,
   le retard de file d'upload rend l'animation en décalage avec la réalité.
4. **Test de bout en bout** : boucle d'upload continue pendant ≥ 5 min,
   vérification visuelle (pas de flash noir, pas de frame sautée) et
   stabilité (pas d'échec liquidctl, pas de fuite mémoire/fichiers).

### Contrainte matérielle (rappel)

- En **monitoring live**, la cadence maximale réelle est ~**1 image / 2–3 s**
  (un subprocess liquidctl par image, coût incompressible) : aucune promesse
  temps réel n'est possible au-delà de cette cadence.
- Le **« 24 FPS »** ne concerne **que la lecture d'un GIF pré-rendu** par le
  firmware (et les limites « ≤ 20 Mo / ≤ 24 FPS » affichées dans l'UI sont
  aujourd'hui purement documentaires côté backend ; voir point 2).

### Critère de décision

- Switch quasi immédiat (à préciser par la mesure : ordre de grandeur de la
  frame, sans à-coup perceptible) → concevoir la fonction sur la base du
  meilleur couple fps/durée mesuré.
- Switch lent (secondes) → fonction morte, ne pas l'implémenter ; le
  monitoring live existant reste la seule voie honnête.

---

## 2. Autres points ouverts

- **Validation des limites GIF côté backend** : `kraken_save_image` /
  `kraken_gallery_add` acceptent n'importe quel fichier (seule l'extension
  est vérifiée) ; les limites « ≤ 20 Mo, ≤ 24 FPS » de l'UI ne sont pas
  appliquées. À décider : les valider côté serveur ou les retirer de l'UI.
- **Readback liquidctl absent** : aucun réglage LCD n'est relisible depuis
  le matériel ; l'état de référence vit dans `config.json` (section
  `kraken`). Toute évolution future doit conserver ce principe.

## Fait récemment (traçabilité)

- **Vagues 1–3 « temps réel + Save/Cancel »** : plus d'auto-save, endpoints
  `GET /api/saved` / `POST /api/save` / `POST /api/restore` + barre
  Save/Cancel, Kraken temps réel (débounce 400 ms), vignettes générées par
  le vrai moteur PIL avec cache disque, galerie compacte.
- **Centrage de l'heure** corrigé dans `ballistix/monitor.py` (elle était
  décalée à droite ; les vignettes en cache se régénèrent seules via
  l'empreinte du contenu du fichier).
- **Harnais de validation front conservé** dans `tests/front/` (au lieu de
  `/tmp`), documenté dans `tests/front/README.md`.
- **Purge du cache de vignettes** : `kraken_purge_thumbs()` supprime au
  démarrage du daemon les vignettes d'une empreinte obsolète et les
  `*.png.tmp` interrompus (garde-fous : jamais hors du dossier de cache,
  erreurs d'E/S tolérées).

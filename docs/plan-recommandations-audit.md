# Plan de mise en œuvre des recommandations de l'audit de cohérence

Source : `docs/audit-coherence-analyses.md` §7. Base : `5e9eec2`.

Règles de conduite, héritées du projet :

* universel : aucun critère ne dépend d'une carte particulière ;
* un commit en anglais par élément, suite `python -m unittest discover -s tests`
  verte à chaque commit ;
* tout test de régression est d'abord exécuté contre l'ancien code ;
* chaque changement de comportement visible entre au `CHANGELOG.md` ;
* la carte de référence reste en lecture seule, `boards/p02_revision_e` n'est pas
  touchée, rien n'est poussé sur origin.

## Ordre et dépendances

| Lot | Recommandation | Dépend de | Effort |
|---|---|---|---|
| 1 | R5 — une seule définition des constantes physiques | — | faible |
| 2 | R4 — espace de noms des règles | — | faible |
| 3 | R6 — recalibrer la confiance | — | faible |
| 4 | R3 — corriger l'agrégation de campagne | 2, 3 | faible à moyen |
| 5 | R1 — un seul moteur DC, avec porte de décision | 1 | moyen à élevé |
| 6 | R2 — dire l'erreur du rasterisé là où il reste | 5 | faible |
| 7 | R8 — partager le DC dans la campagne, EMC sur les rails | 5 | moyen |
| 8 | R7 — barre d'erreur thermique | — | faible |

Les lots 1 à 4 corrigent des défauts qui touchent tous les rapports sans changer
de physique. Le lot 5 est le seul qui déplace tous les chiffres DC ; il passe par
une mesure et une porte avant toute bascule. Le lot 6 vient après parce que son
périmètre dépend de ce qui reste rasterisé.

---

## Lot 1 — Constantes physiques (R5)

**Constat.** Deux résistivités (1,68 et 1,72 × 10⁻⁸ Ω·m), métallisation de repli
0,025 ou 0,020 mm selon le moteur, épaisseurs de repli dupliquées.

**Changements.**

* Module `physical_constants.py` : résistivité du cuivre à 20 °C, coefficient de
  température, conductivité thermique du cuivre, épaisseur de cuivre, épaisseur
  de métallisation de repli, épaisseur de carte de repli, chacune avec sa source.
* Résistivité retenue : **1,72 × 10⁻⁸ Ω·m à 20 °C** (cuivre recuit IEC 60028,
  1,7241 × 10⁻⁸, arrondi ; valeur du chemin déjà validé). Le moteur rasterisé
  passe de 1,68 à 1,72 : ses résistances montent de 2,4 %.
* Métallisation de repli retenue : **0,020 mm**, minimum IPC-6012 classe 2. Une
  valeur de repli ne doit pas flatter le modèle ; elle s'applique aux
  vias et pastilles électriques comme thermiques quand la géométrie ne la donne
  pas.
* Tous les moteurs importent ces constantes ; la limitation DC nomme la
  résistivité et sa température de référence.

**Tests.** Un test qui échoue si une valeur littérale de ces constantes réapparaît
dans le code de production hors du module (même motif que B1).

**Acceptation.** Plus aucune valeur dupliquée ; suite verte ; `CHANGELOG`.

## Lot 2 — Espace de noms des règles (R4)

**Constat.** `DC-001`, `DC-002` et `TH-001` désignent chacun deux règles de
domaines différents.

**Changements.** Les règles EMC en collision sont renommées : `DC-001`/`DC-002`
(découplage) → `DE-001`/`DE-002`, `TH-001` (température) → `TE-001`. La
comparaison de campagnes reconnaît les anciens identifiants EMC pour qu'un avant/après
enjambant le changement ne voie pas un défaut « résolu » puis « nouveau ».

**Tests.** Un test lit l'arbre syntaxique des modules de production (appels
multilignes compris), attribue chaque identifiant à son domaine et échoue si un
identifiant appartient à deux domaines. Il échoue sur le code actuel.

## Lot 3 — Calibrer la confiance (R6)

**Règle écrite dans le contrat.** `DETERMINISTIC` est réservé à un fait sur le
calcul lui-même (non-convergence, fichier différent de la carte ouverte) ou à un
comptage exact. Un verdict de modèle ou un seuil codé en dur n'est jamais
déterministe.

**Changements.**

* Conversion des anciens libellés : `HIGH` → `ESTIMATED` (au lieu de
  `DETERMINISTIC`).
* `AC-001` toujours `ESTIMATED` ; limitation explicite sur l'inductance de
  montage et le dérating DC absents.
* `DC-001` `ESTIMATED`, et sa recommandation dit qu'une rupture peut venir du
  maillage au pas effectif.
* `TH-001` n'est `CRITICAL` que si le θJB du composant ne vient pas du défaut ;
  sinon `HIGH`, `ESTIMATED`, et la description nomme le θJB supposé.
* Les règles schéma sont traitées par la phase 4 de `plan-universalite.md`.

**Tests.** Un test par finding recalibré, construit contre l'ancien code d'abord.

## Lot 4 — Agrégation de campagne (R3)

**Changements.**

* Deux findings décrivent le même problème s'ils partagent **un composant**, ou
  s'ils partagent **un net et un lieu** (même couche, moins de 2 mm). Un net
  commun seul ne suffit plus : aucune liste de noms (`GND`…) n'est nécessaire.
* Regroupement transitif (union-find) et indépendant de l'ordre.
* Score de domaine : pénalité par sévérité × poids de confiance, plafonnée à trois
  findings par règle — la construction que l'EMC utilise déjà. Un domaine qui
  fournit son propre score (l'EMC) le voit repris tel quel : un seul « EMC /100 ».

**Tests.** Le cas de l'audit (AC-001, GP-003, DC-002) ne fusionne plus ;
indépendance de l'ordre ; transitivité ; 159 findings d'une règle ne coûtent pas
plus que trois ; score EMC de campagne égal au score EMC du domaine.

## Lot 5 — Un seul moteur DC (R1)

### 5.1 Ingestion hybride universelle

Le lecteur de fichiers ne garde que le plus grand îlot rempli d'une zone et ignore
les zones déclarées sur plusieurs couches (`(layers …)`). Correctif : tous les
îlots, toutes les couches, chaque îlot maillé. Tests sur zones synthétiques à deux
îlots et sur deux couches ; mesure du nombre de composantes connexes sur les rails
de la carte de référence avant/après.

### 5.2 Performance du maillage de zones

Le cut-cell calcule une intersection Shapely par arête en Python. Vectorisation
des intersections par rangée. Acceptation : branches et conductances identiques à
10⁻⁹ près sur les tests existants ; durée mesurée avant/après sur la carte de
référence.

### 5.3 Banc de comparaison

`validation/dc_engine_comparison.py <board.kicad_pcb> [sidecar]` : la même
géométrie (lue du fichier) passe par les deux moteurs, rail par rail ; chute,
charges isolées, durée. Lecture seule.

### Porte de décision — critères fixés avant la mesure

La bascule de l'analyse DC vers le moteur hybride se fait si **tous** sont vrais :

1. sur les pistes synthétiques de `dc_raster_fidelity.py`, le moteur hybride est
   exact à 1 % près, droites et à 45°, à tout alignement ;
2. sur la carte de référence, chaque rail configuré est résolu par le moteur
   hybride sans charge isolée ;
3. la durée totale hybride ne dépasse pas trois fois la durée rasterisée sur
   ces rails ;
4. aucun rail porté principalement par des plans ne diffère de plus de 10 % entre
   les deux moteurs sans explication établie.

Si la porte échoue, le moteur hybride est livré comme moteur au choix, le
rasterisé reste par défaut, et le lot 6 couvre l'écart sur le DC aussi. Le
résultat de la porte est consigné, qu'il soit positif ou non.

### 5.4 Moteur DC hybride

`HybridDCSolverEngine` respecte le contrat de `DCSolverEngine` (mêmes clés de
résultat) : régulateurs et rails amont/aval, localisation des pastilles par
`rail_sources`, couches ramenées aux identifiants de l'empilage vivant par leur
nom, densité de courant par section de branche quand elle est connue. La chute
jugée par DC-003 devient la **chute au pire point de charge**, la grandeur que le
conseiller dimensionne ; l'écart max−min sur le net reste publié en métrique.

### 5.5 Bascule

Quand le `.kicad_pcb` est connu : DC, thermique, CFD et conseiller passent par le
même moteur, sur le même fichier. Si la carte ouverte diffère du fichier
enregistré, un finding `DC-004` (fait sur le calcul) le dit. Sans fichier, repli
rasterisé explicite. Acceptation : DC-003 et la baseline du conseiller donnent la
même chute sur une carte synthétique.

## Lot 6 — Dire l'erreur du rasterisé (R2)

Pour l'AC, toujours rasterisé, et pour le DC en repli : pas effectif publié,
largeur de la piste la plus fine du réseau rapportée au pas, présence de pistes
obliques ; limitation chiffrée par la mesure de `dc_raster_fidelity.py` quand une
piste fait moins de trois pas.

## Lot 7 — DC partagé et EMC sur les rails (R8)

* Dans une campagne où le DC a tourné, thermique et CFD reprennent ses résultats
  pour les pertes cuivre au lieu de re-résoudre ; le couplage électrothermique
  itératif garde ses propres solves. Provenance publiée.
* Sources EMC sans modèle de pertes : la tension vient du rail configuré qui
  alimente les composants reliés au net source, quand il existe ; la provenance du
  paramètre le dit, et le courant supposé reste marqué comme tel.

## Lot 8 — Barre d'erreur thermique (R7)

Le thermique résout aussi à pas double et publie l'écart du point chaud ; au-delà
de 2 °C, un finding dit que le résultat n'est pas convergé en maillage. Réglage
activé par défaut, déclaré une seule fois (test B1). Mesure de l'écart au
préréglage « Normal (1,0 mm) » sur un modèle synthétique ; la mesure sur carte
réelle demande un lancement depuis KiCad ou Zeo et reste signalée comme à faire.

## Clôture

Harnais relancés (`dc_raster_fidelity.py`, `ingest_corpus.py`, comparaison DC),
état des recommandations reporté dans `audit-coherence-analyses.md` et
`plan-ameliorations.md`, `CHANGELOG`. Le plugin installé n'est pas redéployé sans
accord.

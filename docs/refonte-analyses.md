# Ki-PIDA — Audit des analyses et plan de refonte

Objectif de ce document : identifier ce qui limite aujourd'hui la justesse, la
couverture et l'exploitabilité des analyses de Ki-PIDA, puis décrire les
solutions techniques permettant d'aboutir à un plugin qui produit **une analyse
complète du PCB *et* de son schéma**, un **rapport unique et lisible**, et des
**conseils d'amélioration quantifiés**.

Base analysée : branche `claude/pcb-analysis-tool-40t1dr`, ~35 300 lignes de
Python, 60 modules à la racine, 365 tests unitaires (4 échecs locaux uniquement
dus à l'absence de `kipy`/`wx` dans l'environnement d'audit).

---

## 1. Ce qui est déjà solide

Il faut le dire avant la critique, parce que la refonte doit s'appuyer dessus :

* **Le contrat de résultat** (`analysis_contract.py`) est bon : verdict, findings
  typés, sévérité, **confiance**, métriques, artefacts, **provenance**,
  **limitations**, versionné et sérialisable. C'est exactement la bonne colonne
  vertébrale pour un rapport de qualité — elle est aujourd'hui sous-exploitée.
* **L'honnêteté épistémique** : le code distingue explicitement estimation,
  heuristique, datasheet et mesure, refuse de comparer un spectre relatif à une
  limite réglementaire, et ne peint pas une atténuation de blindage sans courbe
  constructeur. Cette culture est rare et doit être préservée.
* **L'architecture d'exécution** : `application/background_controller.py`
  (un job par analyse, annulation coopérative, capture des objets KiCad sur le
  thread UI puis calcul sur snapshot détaché) est propre et déjà généralisée.
* **L'historique de campagnes** versionné avec `manifest.json` + `result.json`.
* **Deux briques de conseil déjà quantifiées** : `differential_recommender.py`
  et `decoupling_optimizer.py`. Ce sont les modèles à généraliser.

---

## 2. Le plugin ne lit jamais le schéma du projet

`plugin.json` déclare `"scopes": ["pcb"]`, et aucun module du dépôt n'ouvre de
`.kicad_sch`, d'annotation, de netlist ou de propriété de symbole. Le plugin
ouvre le `.kicad_pcb` de l'utilisateur et s'arrête là : il n'exploite que la
moitié des données que KiCad a sous la main. Toute l'intention de conception est
donc reconstruite par expressions régulières sur les **noms de nets** :

| Ce qu'il faut savoir | Comment c'est obtenu aujourd'hui | Fiabilité |
|---|---|---|
| Quels nets sont des rails | `r'(?i)VCC\|VDD\|PWR\|\+.*V\|GND'` (`discovery.py`) | faible |
| La tension d'un rail | regex `3V3 → 3.3` sur le nom | faible |
| Le courant d'une charge | **saisi à la main par l'utilisateur** | manuelle |
| Quels nets sont des horloges | `(CLK\|CLOCK\|MCLK\|BCLK\|SCLK\|XTAL)` | faible |
| La fréquence d'horloge | **constante 25 MHz** par défaut | arbitraire |
| La fréquence de découpage | **constante 500 kHz** par défaut | arbitraire |
| Les paires différentielles | suffixes `_P/_N`, `+/-`, `DP/DM` | moyenne |
| Les découplages | valeur du champ `Value` de l'empreinte | moyenne |
| Les protections/filtres | nom d'empreinte (`emc_analyzer.py` note lui-même : *« low confidence without schematic pin metadata »*) | faible |

C'est précisément ce qui limite l'universalité de l'outil. Tout projet KiCad a
son schéma à côté de son PCB : le lire ne spécialise le plugin sur aucune carte,
cela lui permet au contraire d'en comprendre n'importe laquelle sans que
l'utilisateur ressaisisse à la main ce que le schéma dit déjà. À l'inverse, la
découverte par regex ne fonctionne que sur les cartes dont les nets sont nommés
comme le plugin l'espère.

Conséquences directes :

1. **Le coût de configuration est reporté sur l'utilisateur** (chaque rail,
   chaque source, chaque charge, chaque courant, chaque fréquence). Une analyse
   « complète » demande aujourd'hui une longue saisie manuelle avant le premier
   résultat.
2. **Les valeurs par défaut arbitraires contaminent les résultats** : une
   analyse EMC lancée sans édition manuelle raisonne sur des horloges à 25 MHz
   et des convertisseurs à 500 kHz qui n'existent pas.
3. **Des familles entières de vérifications sont impossibles** : découplage par
   broche d'alimentation d'un circuit, dérating des composants, cohérence
   BOM/PCB, broches non connectées, diviseur de contre-réaction vs. tension
   nominale annoncée, séquencement des rails.

### Solution : une couche d'ingestion unifiée PCB + schéma

Le format `.kicad_sch` est du S-expression texte, lisible hors ligne, et le
dépôt possède **déjà** un lecteur de S-expressions équilibrées
(`GeometryExtractor._balanced_blocks`, utilisé pour relire les polygones de
zones). Il n'y a donc pas de verrou technique.

```
kipida/ingest/
    sexpr.py            # tokenizer S-expression réutilisable (extrait de extractor.py)
    kicad_api.py        # ADAPTATEUR UNIQUE kipy (remplace 6 copies de _get_val)
    board_reader.py     # géométrie, empreintes, vias, zones, empilage
    schematic_reader.py # symboles, champs, broches + type électrique, hiérarchie
    netlist_builder.py  # connectivité logique, classes de nets, alias
    design_model.py     # DesignModel gelé, JSON-sérialisable, hashable
```

Ce que le schéma débloque immédiatement :

* **Type électrique des broches** (`power_in`, `power_out`, `open_collector`,
  `bidirectional`…) → découverte **déterministe** des rails, des sources et des
  charges : une broche `power_out` est une source, une `power_in` est une
  charge. On remplace une heuristique de nommage par une donnée factuelle.
* **Valeurs et MPN réels** → capacité exacte d'un découplage, fréquence d'un
  quartz (`Value = 25MHz`), diélectrique X7R/C0G, tension de service,
  fréquence de découpage d'un convertisseur via son MPN.
* **Rôle des composants** → un condensateur entre un rail et GND *dont une
  broche est reliée à une broche `power_in` d'un circuit* est un découplage de
  ce circuit ; une TVS, une ferrite, un connecteur sont identifiés par symbole
  et non par nom d'empreinte.
* **Hiérarchie des feuilles et classes de nets** → regroupement fonctionnel des
  findings (« le bloc alimentation », « l'étage USB ») au lieu d'une liste plate.
* **Contrôle croisé PCB ↔ schéma** : refdes présents d'un seul côté, DNP montés,
  nets non routés, empreinte incohérente avec le symbole.

Le `DesignModel` doit porter une **empreinte (hash)** de la carte et du schéma :
c'est elle qui rend possible le cache, le recalcul incrémental et la comparaison
de campagnes avant/après correction.

### Cas multi-cartes

Le projet de référence est un projet **multi-cartes** : un schéma parent et
plusieurs PCB sous `boards/`. Cela invalide l'hypothèse implicite de la section
précédente — « le `.kicad_sch` est à côté du `.kicad_pcb` » — et impose quatre
décisions dans la couche d'ingestion.

1. **Résolution de projet explicite.** À partir du board ouvert, il faut remonter
   au projet parent puis déterminer *quelles feuilles* du schéma correspondent à
   cette carte. Ce n'est pas une recherche de fichier voisin mais une étape à
   part entière, avec son échec explicite quand la correspondance est ambiguë.
2. **Le périmètre d'un rail dépasse la carte.** Sur une carte d'alimentation, les
   charges ne sont pas sur la carte : ce sont des connecteurs. Une découverte
   automatique naïve trouvera zéro charge. Le courant doit venir des cartes
   consommatrices, d'un champ de symbole, ou rester saisi. Corollaire : la chute
   calculée sur une carte n'est qu'un maillon ; le budget réel additionne les
   chutes carte par carte, plus les câbles et les contacts de connecteur.
   Le code porte déjà une trace de ce raisonnement — `thermal_mode = AUTO` traite
   les références `J*` comme puissance exportée — mais c'est la seule.
3. **Les noms de nets ne sont pas uniques globalement.** `GND` et `+3V3` existent
   sur chaque carte sans désigner la même instance. Le `DesignModel` a besoin
   d'un identifiant qualifié (`board_id` + net) et d'une table d'appariement
   inter-cartes établie par connecteur.
4. **La configuration sidecar.** `<project>.kipida.json` est aujourd'hui posé à
   côté du `.kicad_pro`. Avec N projets : une configuration par carte — cohérent
   avec l'existant — plus une configuration « système » facultative décrivant les
   liaisons inter-cartes.

C'est aussi une opportunité. Un rapport système qui chaîne le budget de tension
depuis l'alimentation jusqu'à la charge finale, à travers connecteurs et câbles,
est une capacité que peu d'outils libres offrent. Elle doit rester **facultative** :
le mode mono-carte demeure le défaut, et la phase 0 se contente d'une ingestion
mono-carte dont l'étape de résolution de projet est conçue pour le multi-cartes.
Le chaînage système arrive avec le rapport de campagne (phase 3).

---

## 3. Limites du moteur numérique DC (et comment les lever)

C'est le cœur historique de l'outil, et c'est là que se trouvent les écarts de
justesse les plus importants.

### 3.1 Le maillage cartésien uniforme ne conserve pas la section de cuivre

`mesh.py` rasterise le cuivre sur une grille régulière, un nœud par point de
grille à l'intérieur du polygone, et affecte à **chaque arête** la conductance
`g = épaisseur / ρ`. Or `σ·t·w/h` avec `w = h` redonne exactement `t/ρ` : une
chaîne de nœuds large d'un seul point modélise donc **une piste dont la largeur
vaut le pas de grille**, quelle que soit sa largeur réelle.

Avec le pas par défaut de 0,1 mm :

| Piste réelle | Pas effectif | Largeur modélisée | Erreur sur R |
|---|---|---|---|
| 0,15 mm | 0,10 mm | 0,10 mm | **+50 %** |
| 0,15 mm | 0,176 mm (grille auto-dégradée) | 0,176 mm | **−15 %** |
| 0,15 mm | 0,25 mm | 0,25 mm | **−40 %** |

Le phénomène est amplifié par deux mécanismes :

* **`extractor.py` gonfle toute la géométrie** : `safety_buffer = 0.05` mm est
  ajouté au rayon de chaque piste, via et pad (lignes 782, 885–908) « pour
  attraper les points de grille ». Une piste de 0,15 mm est donc extrudée à
  0,25 mm, soit **−40 % de résistance** avant même le maillage. Ce biais est
  systématiquement **non conservatif** : il sous-estime la chute de tension.
* **Le plafond `MAX_ELECTRICAL_NODES = 400 000`** déclenche une dégradation
  automatique du pas. À 0,1 mm on a 100 nœuds/mm² : le plafond correspond à
  **4 000 mm² de cuivre au total, toutes couches confondues**. Une carte
  100 × 80 mm avec deux plans couvrant 70 % dépasse ce budget d'un facteur 3 —
  la grille est alors ramenée à ~0,18 mm et les pistes fines sont perdues.

**Solution — moteur hybride « réduction analytique + volumes finis cut-cell ».**

1. **Les pistes ne se maillent pas, elles se réduisent.** Chaque segment devient
   une résistance analytique exacte `R = ρ·L/(w·t)`, chaque jonction, pad et via
   un nœud. Un net de 300 segments donne ~300 branches au lieu de 200 000 nœuds,
   avec une résistance **exacte** et zéro escalier. Les arcs se discrétisent en
   quelques segments.
2. **Les zones et plans se maillent en cut-cell** : la conductance d'une arête
   est pondérée par la fraction de cuivre réellement traversée
   (`g = σ·t·ℓ_recouvrement/h`) au lieu d'un test binaire dedans/dehors. On
   récupère un ordre de convergence O(h²) et, à erreur égale, 4 à 10 fois moins
   de nœuds. Variante supérieure si le budget le permet : triangulation
   contrainte du polygone (Shapely → maillage Delaunay) et éléments finis P1,
   raffinés autour des pads et des vias — c'est ce que font les outils PI
   commerciaux.
3. **Le raccord piste ↔ plan** se fait par une résistance d'épandage analytique
   `R = ρ/(2πt)·ln(r₂/r₁)`, et non par un nœud unique. Aujourd'hui,
   `_get_best_node_in_radius` accroche tout le courant d'un via sur **un seul
   nœud par couche**, ce qui crée un point chaud artificiel dans les cartes de
   densité de courant.
4. **Injection distribuée** : les courants de charge sont répartis sur l'aire
   réelle du pad. Aujourd'hui `_mesh_nodes` cherche un nœud dans un voisinage de
   ±1 cellule, sur `range(32)` couches en dur, et divise le courant à parts
   égales entre les nœuds trouvés — d'où les avertissements « found NO mesh
   nodes » et des singularités locales.
5. **Supprimer `safety_buffer`** : il n'a plus de raison d'être une fois que la
   géométrie n'est plus échantillonnée ponctuellement.

Gain attendu : erreur de résistance ramenée de 15–50 % à quelques pourcents,
avec **moins** de nœuds et donc un temps de calcul plus court.

### 3.2 On ne simule que l'aller, jamais le retour

`DCSolverEngine.solve` boucle rail par rail : un système de Laplace par net,
sources en Dirichlet, charges en injection de courant. **Le net de masse n'est
jamais dans le système.** Or ce qu'un électronicien doit connaître, c'est la
chute **de boucle** vue par le composant : `ΔV = ΔV_rail + ΔV_retour`. Sur une
carte dense, avec des plans découpés ou une masse partagée avec un étage de
puissance, la remontée de masse représente couramment 30 à 60 % de l'erreur de
tension à la broche.

De plus, la statistique publiée est `max(V) − min(V)` sur **tout le net** — pas
la tension réellement vue par chaque charge.

**Solution.** Assembler un seul système sur l'union `rail ∪ retour`, chaque
charge devenant une source de courant **entre son nœud rail et son nœud de
masse**. Le résultat naturel devient :

```
U7  (VDD_CORE, pin 12) : 3,254 V  (-1,4 %)   dont rail -28 mV / masse +18 mV
U3  (VDD_CORE, pin 4)  : 3,198 V  (-3,1 %)   ✗ cible -3,0 %
```

C'est-à-dire un résultat **par broche de charge**, décomposé aller/retour, au
lieu d'un intervalle global par net. Le même système résolu à plusieurs
fréquences donne gratuitement le chemin de retour HF, base d'analyses EMC bien
plus solides que les règles géométriques actuelles.

### 3.3 Coût d'assemblage

`Mesher` construit la matrice avec une boucle Python par arête
(`for u, v in zip(...): mesh.add_edge_direct(...)`), chaque appel faisant un
`append` sur cinq listes et instanciant un `MeshBranch`. Sur un maillage de
400 000 nœuds, cela représente ~1,5 million d'appels Python et ~8 millions
d'`append` — l'assemblage domine largement la résolution.

**Solution** : produire directement les tableaux NumPy (`np.concatenate` des
indices et des valeurs, `coo_matrix` en une fois), et stocker les branches sous
forme de tableaux structurés plutôt que d'une liste d'objets. Facteur 20 à 100
sur l'assemblage, sans changer la physique.

Côté résolution, ajouter un préconditionneur multigrille algébrique (`pyamg`)
ou une factorisation de Cholesky creuse : sur un laplacien 2D, le gradient
conjugué non préconditionné converge en O(N^1,5) là où l'AMG est quasi linéaire.

### 3.4 Diagnostics DC trop pauvres

Trois règles seulement (`analysis_adapters.adapt_dc_result`) : îlot sans source,
non-convergence, dépassement de la cible de chute. Manquent notamment :

* **Élévation de température par piste et par via selon IPC-2152/IPC-2221** — la
  densité de courant brute actuelle ne dit pas si la piste est acceptable.
* **Ampacité et fiabilité des vias** (nombre de vias en parallèle sur un rail de
  puissance, rapport d'aspect, courant par barillet).
* **Marge de tension par broche de charge** (cf. §3.2).
* **Contribution ordonnée des pertes** : `P = R·I²` par branche, triée, permet
  de dire « 62 % de la chute se joue sur 8 segments entre (X₁,Y₁) et (X₂,Y₂) »
  au lieu de « augmentez la section de cuivre ».

---

## 4. Limites des autres domaines

### 4.1 Impédance différentielle : formules de 1990

`differential_impedance.py` utilise les approximations IPC-D-317A
(`Z₀ = 87/√(εr+1,41)·ln(5,98h/(0,8w+t))`) plus un facteur de correction de
vernis épargne empirique (`+ ratio·0,15·(εr−1)`). Ces formules ont une validité
étroite (w/h ≈ 0,1–3), ignorent la dispersion, les diélectriques multiples, les
plans de référence partiels, la gravure trapézoïdale et la rugosité. Erreur
typique 10 à 25 % — supérieure à la tolérance de fabrication qu'on cherche à
contrôler.

**Solution : un solveur quasi-statique 2D de section.** Résoudre Laplace sur la
section transverse (différences finies sur grille non uniforme, ~200 × 200,
quelques millisecondes), calculer la matrice de capacité **[C]** avec
diélectriques et la matrice **[C₀]** dans l'air, puis `[L] = µ₀ε₀[C₀]⁻¹`. On en
déduit Z₀, Z_diff, Z_commun, modes pair/impair, εeff, retard, et — en ajoutant
l'effet de peau et tanδ — les pertes. Environ 600 lignes, testable contre des
cas de référence publiés, précision de 1 à 3 %. Bénéfice secondaire : la même
matrice à 3 conducteurs donne la **diaphonie NEXT/FEXT quantitative**, et un
chaînage de matrices ABCD donne les **réflexions et l'effet des stubs** — deux
familles d'analyse SI aujourd'hui absentes.

### 4.2 EMI/EMC : bonne ossature, entrées faibles

Une trentaine de règles géométriques traçables (`GP-*`, `SU-*`, `RP-*`, `DP-*`,
`XT-*`…), ce qui est appréciable. Mais elles reposent sur des seuils empiriques
codés en dur et surtout sur des **sources découvertes par regex avec des
paramètres par défaut arbitraires** (§2). Le champ proche est quasi-statique
(charges linéiques + Biot-Savart) sans annulation du courant de retour, ce que
le code documente honnêtement.

**Solutions**, par ordre de rendement :

1. Alimenter les sources depuis le schéma (fréquence de quartz réelle, MPN du
   convertisseur, débit d'interface) — supprime la principale source d'erreur.
2. Calculer le **chemin de retour réel** avec le solveur multi-net AC (§3.2) au
   lieu de le déduire de règles de proximité : surface de boucle, discontinuité
   de retour, courant de mode commun deviennent des grandeurs calculées.
3. Externaliser les seuils dans un profil éditable (prototype / production /
   automobile) plutôt qu'en dur dans les méthodes.

### 4.3 Thermique et CFD

Modèles cohérents et bien documentés (conduction 3D stationnaire, convection
naturelle/forcée, rayonnement linéarisé, CFD laminaire incompressible). Limites
principales : composants réduits à des boîtes à puissance uniforme,
échantillonnage cuivre binaire (même problème d'escalier qu'en DC, moins
critique thermiquement), pas de régime transitoire, pas de modèle de vias
thermiques en réseau discret. Priorité moyenne : ces analyses sont utilisées
en comparaison relative, où le biais s'annule en partie. À traiter après le DC.

---

## 5. Rapport et conseils : ce qui manque pour être exploitable

État actuel : chaque analyse produit un `report.txt`, des PNG et un
`result.json`. Aucun export HTML, PDF ou CSV. **Aucun rapport consolidé
multi-domaines.** Les recommandations sont des chaînes statiques dans les
findings (« Increase copper cross-section or reduce path/load resistance »),
sauf pour le découplage et les paires différentielles qui, eux, chiffrent.

### 5.1 Campagne et rapport unifiés

Introduire un `CampaignResult` = ensemble d'`AnalysisResult` + agrégation :

* un **score de santé par domaine** et un verdict global ;
* un **top-N d'actions inter-domaines dédupliquées** : un même défaut physique
  (un plan fragmenté) génère aujourd'hui un finding DC, un finding EMC et un
  finding différentiel séparés ; ils doivent être fusionnés en une seule action
  avec ses trois conséquences ;
* un **classement par gain / effort**, pas par sévérité seule.

Générateur **HTML autonome** (CSS en ligne, images en base64, aucun serveur) :

```
1. Synthèse            verdict, scores, 10 actions prioritaires, périmètre
2. Actions             fiche par action : cause, preuve, correctif chiffré, gain prédit
3. Par domaine         métriques, cartes, findings filtrables
4. Annexes             hypothèses, provenance, limites, configuration, versions
```

Le contrat de résultat porte déjà `provenance`, `confidence` et `limitations` :
il suffit de les **rendre**, en affichant à côté de chaque chiffre d'où il vient
et à quel point on peut s'y fier. Ajouter l'export CSV des findings et la vue
**comparaison de campagnes** (avant/après correction), rendue possible par
`run_id` + `board_fingerprint` déjà présents.

### 5.2 Un vrai conseiller, pas des phrases toutes faites

Chaque finding doit porter une remédiation **structurée et calculée** :

```python
@dataclass(frozen=True)
class Remediation:
    action: str                 # WIDEN_TRACK, ADD_STITCHING_VIAS, MOVE_CAPACITOR…
    target: TargetRef           # segment / pad / via / composant + coordonnées
    current_value: float        # 0.25 mm
    proposed_value: float       # 0.60 mm
    predicted_gain: str         # "chute 3,1 % → 1,8 %"
    effort: str                 # LOW / MEDIUM / HIGH
    alternatives: tuple[str, ...]
```

Le calcul est direct dans la plupart des cas :

* **DC** : trier les branches par `R·I²`, identifier le chemin critique, en
  déduire la largeur requise `w' = w·(ΔV_actuel/ΔV_cible)` sur les segments
  dominants, puis **re-simuler en incrémental** pour vérifier la prédiction.
* **Thermique** : nombre de vias thermiques nécessaires pour un ΔT visé, obtenu
  par sensibilité sur la conductance verticale.
* **EMC** : pas de couture requis pour la fréquence maximale considérée,
  distance de reroutage, valeur de filtre.
* **Découplage et paires différentielles** : la logique existe déjà, il s'agit
  de l'exposer dans le même format.

Le point clé est le **what-if** : proposer une modification, la re-simuler,
afficher le gain vérifié. C'est ce qui transforme un rapport d'audit en outil
de conception.

---

## 6. Dette technique à traiter pendant la refonte

| Constat | Impact | Correctif |
|---|---|---|
| 60 modules à plat, `try: from .models … except: from models …` partout | imports fragiles, pas de frontière claire | paquet `kipida/` avec sous-paquets `ingest`, `model`, `analysis/*`, `rules`, `report`, `app`, `ui` |
| 6 implémentations de `_get_val`, 29 `except: pass` | les ruptures d'API kipy passent inaperçues et dégradent silencieusement les résultats | un adaptateur `ingest/kicad_api.py` unique, avec échecs explicites et capacités déclarées |
| `config_manager.py` : 44 ko de `to_dict`/`from_dict` manuels (~30 paires) | tout nouveau champ se paie en trois endroits | sérialiseur générique de dataclasses + migrations versionnées (−1 200 lignes environ) |
| `ui/main_dialog.py` : 1 480 lignes, une classe, 78 méthodes | difficile à modifier sans régression | ne garder que la navigation ; un contrôleur par panneau (déjà amorcé dans `application/`) |
| Règles dispersées (EMC en méthodes, DC en `if`, diff dans l'adaptateur) | couverture invisible, seuils non éditables, tests inégaux | registre de règles déclaratif : id, domaine, seuils, référence normative, texte i18n, remédiation, test unitaire |
| CI : `unittest` seul, pas de lint, pas de types, pas de cartes de référence | les régressions numériques ne sont pas détectées | ruff + mypy progressif + `tests/boards/` avec cartes de référence et résultats attendus |

**Validation numérique** — indispensable avant de communiquer des chiffres :
plaque rectangulaire (solution analytique), piste droite (`R = ρL/wt`), via
unique, ligne 50 Ω de géométrie connue, croisement avec un solveur de référence
ou une mesure. Ces bancs deviennent des tests de non-régression.

---

## 7. Couverture visée pour une analyse « complète »

| Famille | Aujourd'hui | À ajouter |
|---|---|---|
| DC / chute de tension | rail seul, 3 règles | boucle rail+retour, résultat par broche, IPC-2152/2221, ampacité vias |
| AC / PDN | balayage Z(f), optimiseur de découplage | dérating DC bias des MLCC, inductance de montage réelle, cavité des plans |
| SI | impédance différentielle, longueurs | solveur 2D de section, diaphonie chiffrée, réflexions/stubs/terminaisons |
| EMC | ~30 règles géométriques + champ proche | retour HF calculé, sources issues du schéma, seuils par profil |
| Thermique / CFD | conduction 3D, CFD laminaire | transitoire, réseau de vias thermiques, θJC datasheet |
| **Schéma** | **rien** | découplage par broche d'alimentation, dérating (V/I/P), broches non connectées, séquencement, contre-réaction vs. tension annoncée, cohérence BOM/PCB/schéma, DNP |
| Fabricabilité | rien | isolement/lignes de fuite, anneau annulaire, courtyard, testabilité |
| Robustesse | rien | Monte-Carlo sur tolérances fab (±10 % diélectrique, ±1 mil gravure) → bandes de confiance sur Z et sur la chute |

---

## 8. Feuille de route proposée

**Phase 0 — Fondations.** Extraire `sexpr.py`, écrire l'adaptateur kipy unique,
lire le schéma, construire le `DesignModel` avec empreinte, remplacer la
sérialisation manuelle. *Rien de visible pour l'utilisateur, tout le reste en
dépend.*

**Phase 1 — Crédibilité numérique.** Moteur DC hybride (pistes analytiques +
plans cut-cell), résolution rail+retour, suppression du `safety_buffer`,
assemblage vectorisé, préconditionneur AMG, bancs de validation analytiques.
*C'est la phase qui rend les chiffres défendables.*

**Phase 2 — Couverture.** Registre de règles déclaratif ; règles issues du
schéma ; solveur 2D de section pour l'impédance, la diaphonie et les
réflexions ; IPC-2152 pour l'ampacité.

**Phase 3 — Restitution.** `CampaignResult`, bouton « tout analyser », rapport
HTML/PDF autonome, conseiller quantifié avec re-simulation what-if, comparaison
de campagnes.

**Phase 4 — Finition.** Découpage de l'UI autour du parcours « configurer →
tout lancer → lire le rapport », recalcul incrémental par empreinte,
localisation, documentation des règles.

### Deux arbitrages tranchés

**Comment mailler les plans de cuivre.** Le *cut-cell* conserve la grille
régulière et pondère chaque liaison par la fraction de cuivre réellement
présente : environ 200 lignes dans `mesh.py`, tout le pipeline aval (cartes,
sondes, densité de courant, couplage thermique) continue de fonctionner. Les
*éléments finis P1* sur triangulation épousent exactement les contours et se
raffinent autour des pads — plus juste et plus rapide à erreur égale, mais
nouveau mailleur, dépendance externe et réécriture de tout ce qui consomme le
maillage. **Retenu : cut-cell en phase 1**, P1 seulement si l'erreur résiduelle
le justifie une fois mesurée sur les bancs de validation.

**Fiabiliser avant d'élargir.** La phase 1 fiabilise les chiffres des analyses
existantes, la phase 2 ajoute les analyses manquantes ; inverser reviendrait à
bâtir de nouvelles règles sur des résultats faux. **Retenu : phase 1 avant
phase 2**, à une exception près — les règles issues du schéma qui ne consomment
aucun résultat de solveur (dérating V/I/P, découplage par broche, cohérence
BOM/PCB/schéma, broches non connectées) peuvent démarrer dès la phase 0.

**Principe à ne pas perdre** : chaque valeur automatiquement déduite du schéma
doit rester **modifiable et tracée** (`source=auto|schematic|manual`,
`confidence`). La force actuelle de Ki-PIDA est de ne jamais présenter une
estimation comme une mesure ; l'automatisation ne doit pas l'effacer.

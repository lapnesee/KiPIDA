# Audit du domaine EMI/EMC

Périmètre : `emc_analyzer.py` (1 349 l.), `em_field_solver.py` (553 l.),
`emc_phase10.py` (1 117 l.), et la restitution par
`analysis_adapters.py::adapt_emc_result`.

Base : `2ea96d0`. Toutes les affirmations ci-dessous sont référencées
`fichier:ligne`. Ce qui relève de la supposition est marqué **[supposition]**.

Cet audit s'appuie sur un run réel : carte d'alimentation 6 couches,
80 × 35 mm, 1 455 pistes, 465 vias, 25 zones. Résultat : 10 findings,
score 76/100, `Emax = 11,44 V/m`, `Hmax = 0,2457 A/m`.

---

## 1. Ce qui est solide

À dire avant la critique, parce que la refonte doit s'appuyer dessus.

* **30 règles traçables** réparties sur 15 catégories, chacune avec un
  identifiant stable, une sévérité, une confiance déclarée et des preuves
  géolocalisées (`EMCEvidence` porte `x_mm`, `y_mm`, `layer_id` —
  `models.py:469-476`). C'est rare et c'est la bonne fondation.

* **Le score est pondéré par la confiance, pas seulement par la sévérité**
  (`emc_analyzer.py:1280-1281`) : `SEVERITY_WEIGHT × CONFIDENCE_WEIGHT`, avec
  `CONFIDENCE_WEIGHT = {HIGH: 1.0, MEDIUM: 0.65, LOW: 0.25}`
  (`emc_analyzer.py:41`). Une heuristique faible ne peut pas dominer le
  verdict. C'est une garantie réelle, et le code la revendique explicitement
  dans ses limitations (`emc_analyzer.py:1322`).

* **Le score est plafonné par règle** (`emc_analyzer.py:1277-1282`,
  `maximum_findings_per_rule_for_score = 3` par défaut,
  `models.py:458`). Une règle qui se déclenche 200 fois ne peut pas écraser
  les 29 autres. C'est un vrai souci de robustesse du classement.

* **Les limitations sont écrites et honnêtes** (`emc_analyzer.py:1317-1323`).
  Le code déclare de lui-même qu'il ne certifie rien, que l'incertitude est
  d'« au moins ±10 à 20 dB », que les vérifications par nom d'empreinte sont
  de faible confiance, et que les fréquences auto-découvertes sont des
  défauts éditables. **Cette liste est exacte.** Peu d'outils s'auto-décrivent
  aussi correctement.

* **Le retour image est implémenté** dans le champ proche
  (`em_field_solver.py:207-213`) : un plan GND adjacent continu est représenté
  par une image idéale de polarité opposée, et les paires différentielles ont
  des polarités opposées (`em_field_solver.py:162-170`).
  **L'audit initial (`docs/refonte-analyses.md` §4.2) affirmait « sans
  annulation du courant de retour » : ce reproche est périmé.**

* **L'enveloppe harmonique est le modèle trapézoïdal standard**
  (`em_field_solver.py:96-97`) : `1/n` avec une décroissance d'ordre 4 au-delà
  de la bande de montée `0,35/tr`. C'est la bonne approximation d'ordre 0 pour
  une enveloppe, correctement implémentée.

* **Le chemin « power-tree »** pour les convertisseurs
  (`emc_analyzer.py:362-380`) tire la fréquence du modèle de pertes réel et
  marque le résultat `parameter_confidence = "MEDIUM"` au lieu de `"LOW"`,
  avec une note traçant l'origine de chaque paramètre. C'est exactement la
  bonne façon de faire, et elle existe déjà — elle n'est simplement pas
  généralisée.

---

## 2. Inventaire des règles

Confiance = valeur passée à `_add(...)`, pas une évaluation de ma part.

| ID | Vérifie | Sur quelle donnée | Confiance | Seuil et origine |
|---|---|---|---|---|
| `GP-002` | Absence de plan de masse | Zones du net de référence | HIGH | Booléen, pas de seuil |
| `GP-003` | Plan de masse fragmenté | Géométrie de zone | HIGH | Nombre de composantes connexes |
| `GP-004` | Couverture de plan faible | Aire zone / aire carte | MEDIUM | Ratio codé en dur |
| `GP-005` | Domaines de masse multiples | Nets de référence | MEDIUM | Comptage |
| `GP-001` | Signal traversant un vide de plan | Intersection piste/zone | HIGH | Géométrique exact |
| `SU-001` | Couches signal adjacentes | Empilage | MEDIUM | Structure d'empilage |
| `SU-002` | Signal loin de sa référence | Empilage | LOW | Distance codée en dur |
| `BE-001` | Piste proche du bord | Distance au contour | HIGH/MEDIUM | Distance codée en dur |
| `CK-001` | Horloge sur couche externe | Couche de la piste | HIGH | Booléen |
| `CK-002` | Route d'horloge longue | Longueur | MEDIUM | Longueur codée en dur |
| `CK-003` | Horloge proche d'un connecteur | Distance | MEDIUM | Distance codée en dur |
| `RP-001` | Transition de couche sans via GND | Vias voisins | HIGH | Rayon codé en dur |
| `DC-001` | Découplage éloigné | **Nom d'empreinte** | LOW | Distance codée en dur |
| `DC-002` | Pas de découplage proche | **Nom d'empreinte** | LOW | Distance codée en dur |
| `IO-001` | Connecteur externe sans protection | **Préfixe de refdes** (`J`,`P`,`CN`) | LOW | `models.py:459` |
| `ES-001` | Chemin ESD absent ou trop long | **Nom d'empreinte** | LOW | Distance codée en dur |
| `ES-002` | TVS sans vias GND proches | Vias voisins | MEDIUM | Comptage |
| `SH-001` | Retour de blindage non identifié | Nets | LOW | Heuristique de nom |
| `VS-001` | Couture de vias éparse | Densité de vias | MEDIUM | Densité codée en dur |
| `SW-001` | Harmoniques dans la bande | Source découverte | MEDIUM | Bande configurée |
| `SW-002` | Grande aire de nœud de découpage | Aire de cuivre | MEDIUM | Aire codée en dur |
| `DP-001` | Désappariement de longueur | Longueurs routées | HIGH | Limite de skew |
| `DP-003` | Changement de couche de référence | Empilage | HIGH | Géométrique |
| `DP-004` | (variante différentielle) | Géométrie | variable | codé en dur |
| `XT-001` | Couplage parallèle long | **Géométrie seule (règle 3H)** | MEDIUM | `emc_analyzer.py:1192` |
| `PD-001` | Impédance PDN hors cible | **Résultat AC réel** | HIGH | Cible AC |
| `PD-002` | PDN dans la cible | Résultat AC réel | HIGH | Cible AC |
| `EE-001` | Résonances de cavité | Dimensions carte | INFO | Formule modale |
| `EE-002` | Enveloppes harmoniques | Sources découvertes | INFO | Modèle trapézoïdal |
| `TH-001` | Température altérant l'EMC | Résultat thermique | MEDIUM | Seuil codé en dur |

**Lecture de ce tableau.** Les règles qui s'appuient sur une donnée factuelle
(`GP-001`, `RP-001`, `DP-001`, `PD-001`) sont HIGH et défendables. Les règles
qui s'appuient sur un **nom** (`DC-001`, `DC-002`, `IO-001`, `ES-001`,
`SH-001`) sont correctement marquées LOW — le code est honnête — mais elles
couvrent précisément les sujets les plus importants d'une pré-conformité :
découplage, protection d'entrée/sortie, ESD, blindage.

---

## 3. Complétude — ce qui manque

| Manque | Pourquoi ça compte | Difficulté |
|---|---|---|
| **Émissions conduites** | La moitié d'une pré-conformité CISPR. Rien dans le code n'aborde le réseau d'alimentation vu depuis l'entrée (LISN), ni le filtre secteur/DC. Un produit échoue au moins autant en conduit qu'en rayonné. | Haute |
| **Comparaison à un gabarit CISPR/FCC** | `standard: "CISPR_32_CLASS_B"` et `market: "EU"` sont **stockés** (`models.py:444-445`) mais je n'ai trouvé aucun code qui compare quoi que ce soit à une limite réglementaire. Le champ est déclaratif. | Moyenne (le refus de comparer un spectre relatif à une limite absolue est **délibéré et correct** — mais alors le réglage ne devrait pas suggérer le contraire) |
| **Courants de mode commun sur les I/O** | C'est le mécanisme dominant d'émission rayonnée réelle : un câble attaché devient l'antenne. Aucune règle n'estime le courant de mode commun injecté dans un connecteur. | Haute |
| **Résonance de câble / longueur d'antenne** | Corollaire du point précédent. Un câble de 1 m résonne vers 75 MHz, en plein dans la bande 30 MHz–1 GHz configurée. | Moyenne |
| **Immunité au-delà du chemin ESD** | `ES-001`/`ES-002` couvrent la présence et le retour d'une TVS. Rien sur EFT/burst, surge, immunité rayonnée, ni sur la coordination des protections. | Haute |
| **Couplage boîtier / fentes** | Explicitement hors périmètre (`em_field_solver.py:59-60`), et honnêtement déclaré. Légitime pour un outil PCB. | Hors périmètre raisonnable |
| **Filtrage d'alimentation** | Aucune règle n'évalue un filtre en π, une ferrite de mode commun, ou l'efficacité d'un filtre d'entrée. `DC-001/002` ne regardent que la proximité de condensateurs. | Moyenne |
| **Diaphonie quantitative** | `XT-001` applique la règle des 3H (`emc_analyzer.py:1192`) — une heuristique géométrique — **alors que `crosstalk_2d.py` calcule NEXT/FEXT quantitatifs et n'est pas appelé.** Voir §6. | Faible |

**Je ne recommande pas d'ajouter ces analyses maintenant.** Voir §6 :
plusieurs grandeurs existantes ne sont pas défendables, et l'ordre correct est
de les fiabiliser d'abord.

---

## 4. Réalisme — les chiffres veulent-ils dire quelque chose ?

### Score /100 — défendable dans sa construction, arbitraire dans son échelle

`risk_score = max(0, 100 - Σ(sévérité × confiance, plafonné par règle))`
(`emc_analyzer.py:1277-1290`).

La **construction** est bonne : pondération par confiance, plafond par règle.
La **calibration** ne repose sur rien : `SEVERITY_WEIGHT = {CRITICAL: 25,
HIGH: 12, MEDIUM: 6, LOW: 2}` (`emc_analyzer.py:40`) et la base 100 sont des
choix sans justification documentée. Le 76/100 du run réel n'a pas de sens
absolu — il ne permet ni de comparer deux cartes différentes, ni de savoir si
la carte passera un test.

**Verdict :** utilisable comme indicateur relatif d'une révision à l'autre sur
la *même* carte. Trompeur si lu comme une note de conformité. L'outil ne le
dit pas explicitement.

### `Emax = 11,44 V/m` / `Hmax = 0,2457 A/m` — non comparable à quoi que ce soit

Le modèle (`em_field_solver.py:1-6`, `:52-61`) est déclaré quasi-statique, et
c'est exact. Trois hypothèses en fixent la valeur absolue :

1. **`LINE_CAPACITANCE_F_M = 100.0e-12`** (`em_field_solver.py:39`) — une
   capacité linéique « nominale » de 100 pF/m convertit la tension source en
   charge linéique. **Le champ E est directement proportionnel à cette
   constante.** Une microstrip réelle varie typiquement de 60 à 150 pF/m selon
   la géométrie : le résultat porte donc un facteur d'incertitude de ~2 avant
   toute autre considération. Aucune justification de la valeur dans le code.

2. **Combinaison par somme quadratique** — les phases relatives entre sources
   sont inconnues, donc RSS (`em_field_solver.py:1-6`). C'est le choix
   raisonnable en l'absence d'information de phase, mais il peut sous-estimer
   d'un facteur allant jusqu'à `√N` si les sources sont cohérentes (cas d'un
   bus synchrone).

3. **Absence d'épandage de courant dans un plan fini, de frontières
   diélectriques et de diffraction** — déclaré (`em_field_solver.py:59-60`).

**Verdict : la valeur absolue est décorative.** Il n'y a ni distance de mesure
normalisée (la hauteur de sonde par défaut est 3 mm —
`models.py:461` — soit du champ proche, sans rapport avec les 3 m ou 10 m d'un
essai), ni bande, ni détecteur (quasi-crête/moyenne). Comparer 11,44 V/m à une
limite CISPR serait une faute.

**Ce que la grandeur vaut réellement :** une **carte de localisation**. Le
maximum situe *où* rayonner le plus sur la carte, ce qui est exactement l'usage
fait par `probe_points` et `test_plan` (`emc_analyzer.py:1302-1306`). C'est un
guide de sondage, pas une mesure. **Le code l'utilise correctement, mais ne
dit nulle part que la valeur en V/m ne doit pas être lue comme une émission.**

### Enveloppes harmoniques (`EE-002`) — correct, mais entrées faibles

Le modèle (`em_field_solver.py:96-97`) est le bon. Sa fiabilité est
entièrement déterminée par la fréquence fondamentale et le temps de montée —
tous deux issus de défauts arbitraires dans la majorité des cas (§5). Marqué
`INFO`, ce qui est cohérent.

### Résonances de cavité (`EE-001`) — correct et honnête

Formule modale sur les dimensions de la carte. Marqué `INFO`, titre
explicitement « estimated ». Rien à redire.

### Phase 10 (SPICE / Palace) — **ne contribue à rien**

`emc_phase10.py` fait 1 117 lignes. Le flux est **à sens unique** : il
*consomme* les findings de l'analyseur pour choisir les régions à simuler
(`emc_phase10.py:637-659`, appelé en `:921`), mais n'en *produit* aucun. Son
résultat n'apparaît que dans `compute_metadata` sous forme de chaînes de
statut (`analysis_adapters.py:107-117`). **Il n'affecte donc ni les findings,
ni le score, ni le verdict.**

Sur le run réel : `MAPPING_VERIFIED_PSPICE_ONLY` (U4) et
`MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE` (U5) — ce dernier signalant une
simulation transitoire instable (`emc_phase10.py:483`). Un modèle SPICE
instable est une information utile, et elle n'atteint pas l'utilisateur
autrement que par une chaîne dans une annexe.

Le run montre aussi `Palace server ready` puis `Uploading…` sans résultat
visible. **[supposition]** — je n'ai pas tracé le cycle de vie complet d'un run
Palace ; il est possible que le résultat soit asynchrone et arrive plus tard.

**Verdict : le meilleur rapport gain/effort de tout le domaine.** Le travail
est fait, il ne manque que le raccordement au contrat de résultat.

---

## 5. Acquisition des entrées

**Constat central : le domaine EMC n'utilise aucune des briques acquises
depuis l'audit initial.** Vérifié par recherche : aucune occurrence de
`ingest`, `symbol_pin`, `ElectricalPinType`, `pintype`, `rules`,
`field_solver_2d`, `crosstalk_2d`, `transmission_line_abcd` dans
`emc_analyzer.py` ni `em_field_solver.py`.

| Entrée | Source actuelle | Source possible | Gain |
|---|---|---|---|
| Fréquence d'horloge | **Défaut 25 MHz** (`emc_analyzer.py:356`) après regex sur le nom du net | Valeur du symbole quartz (`Value = 25MHz`) via `ingest/schematic_reader.py` | Déterministe. Supprime le plus gros défaut arbitraire du domaine |
| Fréquence de découpage | Modèle de pertes si disponible, sinon **défaut 500 kHz** (`emc_analyzer.py:362-364`) | MPN du convertisseur → datasheet | Le chemin « power-tree » existe déjà et marque MEDIUM ; il faut l'étendre |
| Débit différentiel | **Défaut 240 MHz (USB) / 125 MHz** (`emc_analyzer.py:396`) | Interface déclarée + type de broche | Moyen ; le 240 MHz USB est correctement raisonné en commentaire |
| Tension / courant de source | **Défauts 3,3 V / 0,1 A** (`emc_analyzer.py:336`) | Rail parent (tension nominale) + charges configurées | Fort : ces deux nombres multiplient directement E et H |
| Identification des découplages | **Nom d'empreinte** | Broche `EPT_POWER_INPUT` du CI + condensateur au même net et à GND | **`DC-001`/`DC-002` passeraient de LOW à DETERMINISTIC** |
| Protections d'I/O / TVS | **Nom d'empreinte** | Symbole schéma (TVS, ferrite, varistance sont des symboles distincts) | **`IO-001`/`ES-001` passeraient de LOW à HIGH** |
| Connecteurs externes | **Préfixe de refdes** `J`/`P`/`CN` (`models.py:459`) | Type de composant du schéma | Supprime une heuristique de nommage pure |
| Temps de montée | Défauts par catégorie (2 ns horloge, 10 ns découpage, 0,8 ns diff.) | Datasheet du driver | Faible priorité : le temps de montée n'est presque jamais dans une netlist |
| Plans de référence | `reference_net_names` configurable, défaut `["GND","AGND","DGND","PGND"]` | Type de broche `EPT_POWER_INPUT` sur un net sans tension | Faible : le défaut par nom marche bien en pratique |

**Le point le plus important de cet audit** : les cinq règles marquées LOW
(`DC-001`, `DC-002`, `IO-001`, `ES-001`, `SH-001`) reposent toutes sur des
noms, alors que la donnée factuelle qui les rendrait déterministes est
désormais **disponible et déjà lue ailleurs dans le projet**. Ce sont aussi
les règles qui couvrent les sujets les plus décisifs d'une pré-conformité.
Sur le run réel, le seul finding HIGH — « ESD path is missing or too long » —
provient d'une de ces règles.

---

## 6. Propositions, classées par gain/effort

### A. Raccorder Phase 10 au contrat de résultat — **gain très élevé, effort faible**

**Gain :** 1 117 lignes déjà écrites deviennent visibles. Un modèle SPICE
instable (`MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE`, constaté sur U5 dans
le run réel) doit être un finding, pas une chaîne d'annexe.
**Coût :** produire des `EMCFinding` depuis `Phase10Result`, avec confiance
`DATASHEET_BACKED` ou `MEASURED` selon le cas.
**Casse :** rien. Purement additif. Attention à ne pas laisser ces findings
peser sur le score sans réflexion sur leur pondération.

### B. Alimenter les règles de découplage/protection depuis le schéma — **gain très élevé, effort moyen**

**Gain :** `DC-001`, `DC-002`, `IO-001`, `ES-001`, `SH-001` passent de LOW à
déterministe. Comme le score est pondéré par la confiance
(`emc_analyzer.py:1281`), leur poids est aujourd'hui divisé par 4 : les rendre
factuelles **change mécaniquement le score et le classement des actions**.
**Coût :** injecter un `BoardNetlist` (ou les types de broches via
`Pad.symbol_pin.type`, déjà exploité par `ac_model.py`) dans `EMCAnalyzer`.
**Casse :** le score de toutes les cartes déjà analysées change. C'est
souhaitable mais doit être annoncé — la comparaison de campagnes avant/après
deviendrait fausse à cheval sur ce changement.

### C. Brancher `crosstalk_2d.py` sur `XT-001` — **gain élevé, effort faible**

**Gain :** `XT-001` applique la règle des 3H, une heuristique géométrique,
alors que `crosstalk_2d.py` calcule NEXT/FEXT en volts par volt à partir des
modes pair/impair. On remplacerait « ces pistes sont trop proches » par
« la diaphonie proche vaut 4,2 % de l'amplitude agresseur ». C'est
exactement le passage du qualitatif au quantitatif que vise la refonte.
**Coût :** extraire largeur/espacement/hauteur de la section couplée et
appeler `SymmetricCoupledLineSolver`. La géométrie est déjà calculée par
`XT-001` pour son test de proximité.
**Casse :** rien ; `XT-001` peut garder son seuil et gagner une métrique.

### D. Déclarer explicitement ce que le champ proche n'est pas — **gain élevé, effort très faible**

**Gain :** empêche la faute la plus probable d'un utilisateur — lire
`Emax = 11,44 V/m` comme une émission. Aujourd'hui les limitations
(`emc_analyzer.py:1318-1319`) parlent d'incertitude en dB, ce qui **suggère**
une comparabilité à une limite.
**Coût :** une limitation supplémentaire disant que la valeur est un guide de
localisation à 3 mm, sans rapport avec une mesure à 3 m, et que la valeur
absolue dépend linéairement d'une capacité linéique supposée de 100 pF/m.
**Casse :** rien.

### E. Remonter la provenance des paramètres de source dans les findings — **gain moyen, effort faible**

**Gain :** `EMCSignalSource` porte déjà `parameter_confidence` et
`parameter_notes` (`emc_analyzer.py:370-380`), correctement remplis. Ils ne
semblent pas atteindre le rapport. Un finding `SW-001` fondé sur 500 kHz
supposé et un autre fondé sur une fréquence issue du modèle de pertes doivent
être distinguables par le lecteur.
**Coût :** propager ces deux champs dans l'évidence du finding.
**Casse :** rien.

### F. Migrer les règles vers le registre déclaratif `rules/` — **gain moyen, effort élevé**

**Gain :** seuils éditables par profil (prototype / production / automobile),
couverture visible, tests par règle. C'est ce que le plan de refonte prévoit.
**Coût :** 30 règles à porter, dans un fichier de 1 349 lignes fortement
couplé à son snapshot.
**Casse :** risque de régression élevé sur un domaine dont c'est le principal
actif. **À ne faire qu'après A–E**, qui apportent plus pour beaucoup moins.

### G. Émissions conduites, mode commun, immunité — **à ne pas entreprendre maintenant**

Ce sont les vrais manques de couverture (§3), mais les entreprendre avant A–E
reviendrait à bâtir de nouvelles familles de règles sur les mêmes entrées
devinées par regex. L'ordre correct est : fiabiliser les entrées (B),
exploiter ce qui est déjà calculé (A, C), être honnête sur ce qui est
décoratif (D, E), puis élargir.

---

## 7. Respect de la culture du projet

Le projet s'interdit de présenter une estimation comme une mesure. Bilan pour
l'EMC :

**Respecté :** les limitations écrites (`emc_analyzer.py:1317-1323`) sont
exactes et complètes ; `EE-001`/`EE-002` sont marqués `INFO` et titrés
« estimated » ; le refus de comparer le spectre relatif à une limite
réglementaire est explicite (`emc_analyzer.py:1315`) ; les règles fondées sur
des noms sont correctement marquées LOW ; `parameter_notes` trace l'origine
de chaque paramètre de source.

**Non respecté, par ordre de gravité :**

1. **`Emax` en V/m sans avertissement de non-comparabilité.** Une unité
   physique standard, à une valeur plausible, invite à la comparer à une
   limite. Rien n'en dissuade. (→ proposition D)
2. **`standard: "CISPR_32_CLASS_B"` et `market: "EU"` sont stockés sans être
   utilisés** pour aucune comparaison. Le réglage suggère une conformité
   ciblée que l'outil ne vérifie pas.
3. **Le score /100 n'est jamais qualifié.** Ni les limitations ni le rapport
   ne disent qu'il est relatif et non calibré.
4. **`MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE` est une dégradation
   silencieuse.** Une simulation instable atteint l'utilisateur comme une
   chaîne dans une annexe. C'est le même motif que les ports AC exclus ou la
   grille thermique dégradée, tous deux corrigés récemment.

---

## 8. Synthèse

Le domaine EMC est **mieux construit que l'audit initial ne le laissait
croire** : le score est pondéré par la confiance et plafonné par règle, le
retour image existe dans le champ proche, l'enveloppe harmonique est le bon
modèle, et les limitations écrites sont exactes.

Son problème n'est pas sa conception, c'est son **isolement**. Il n'utilise
aucune des cinq briques acquises depuis (ingestion du schéma, registre de
règles, solveur 2D, diaphonie, ABCD), et continue de deviner par regex des
informations que le reste du projet lit désormais de façon factuelle. Cinq de
ses règles — celles qui couvrent découplage, I/O, ESD et blindage, c'est-à-dire
l'essentiel d'une pré-conformité — sont pénalisées d'un facteur 4 dans le
score parce qu'elles reposent sur des noms d'empreintes.

Et 1 117 lignes de vérification SPICE et de solveur Palace ne produisent
aujourd'hui aucun finding.

Les quatre premières propositions (A–D) ne demandent aucune conception
nouvelle : elles raccordent ce qui existe déjà.

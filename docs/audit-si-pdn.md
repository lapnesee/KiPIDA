# Audit — Intégrité du signal et PDN

Périmètre : paires différentielles (`differential_*.py`, `field_solver_2d.py`,
`reference_plane_analyzer.py`), primitives SI non branchées (`crosstalk_2d.py`,
`transmission_line_abcd.py`), et PDN/AC (`ac_model.py`, `ac_solver.py`,
`decoupling_optimizer.py`).

Base : `2ea96d0`. Toutes les affirmations sont référencées `fichier:ligne`. Les
mesures ont été produites en exécutant le code de la branche ; elles sont
reproductibles. Les suppositions non vérifiées sont marquées **[supposition]**.

---

## 1. Ce qui est solide

**Le solveur 2D est numériquement correct.** Deux vérifications exactes le
confirment, exécutées sur cette branche :

*Contrainte du milieu homogène* — une stripline noyée dans un diélectrique
uniforme doit avoir `eps_eff == eps_r` exactement, sans quoi le traitement du
diélectrique est faux. Résultat sur cinq valeurs de 2,2 à 10,2 :

| εr | ε_eff calculé | erreur |
|---|---|---|
| 2,20 | 2,2000 | +0,00 % |
| 4,40 | 4,4000 | +0,00 % |
| 10,20 | 10,2000 | −0,00 % |

*Invariance d'échelle* — l'équation de Laplace est sans échelle : multiplier
toutes les longueurs par un facteur doit laisser Z strictement identique. Sur
une plage de 32× (facteurs 0,25 à 8), masque de soudure désactivé :

```
Zdiff = 89,3985 Ω à tous les facteurs   (écart 0,0000 %)
```

L'erreur de discrétisation est donc **indétectable** sur ce cas. C'est un bon
résultat, et il justifie le remplacement des formules IPC-D-317A.

Attention méthodologique : un premier essai *avec* le masque de soudure actif
montrait 2,47 % de dispersion, que j'ai failli attribuer à la discrétisation.
C'était le masque, dont l'épaisseur `solder_mask_thickness_mm` est **absolue**
(0,02 mm par défaut) et ne suit donc pas la mise à l'échelle. Le comportement
est physiquement correct.

Également solide :

* Le repli IPC-D-317A est **explicite** : `differential_impedance.py:406-409` et
  `:433-436` ajoutent un avertissement citant l'erreur typique ±10-25 %. La règle
  du projet — ne jamais présenter une estimation comme une mesure — est ici
  respectée.
* L'empilage réel est lu (`provenance: stackup / KICAD_IPC / trusted` dans le
  rapport réel), pas remplacé par des défauts.
* Les acquis récents du PDN sont réels : limite quasi-statique calculée,
  signalement du pire cas en bord de fenêtre, résolution de la source par type
  de broche, exclusion tracée des ports déconnectés.

---

## 2. Code mort — la section la plus importante de cet audit

**`crosstalk_2d.py` et `transmission_line_abcd.py` ne sont appelés par aucun
code de production.** Vérifié par recherche exhaustive sur le dépôt : les seuls
importateurs sont leurs propres fichiers de tests.

| Module | Lignes | Importateurs hors tests |
|---|---|---|
| `crosstalk_2d.py` | ~200 | **aucun** |
| `transmission_line_abcd.py` | ~150 | **aucun** |

Conséquence directe : la diaphonie NEXT/FEXT quantitative et les réflexions
par chaînage ABCD, présentées comme livrées, **n'atteignent jamais
l'utilisateur**. Aucun rapport ne peut les contenir, aucune règle ne les
consomme, aucun finding n'en dépend.

Du code testé mais jamais appelé n'apporte rien. Les 15 tests qui les couvrent
donnent une impression de complétude que le produit ne possède pas. C'est le
point que je placerais en tête de toute reprise : soit on les branche, soit on
les retire, mais les laisser en l'état entretient une illusion de couverture.

À noter que la diaphonie *est* traitée ailleurs, mais par une règle purement
géométrique : `emc_analyzer.py:1192` (`XT-001`) compare une distance à un seuil
« 3H » sans aucun calcul de couplage. Le module capable de chiffrer le couplage
existe, et la règle qui en aurait besoin ne l'utilise pas.

---

## 3. Modèle physique

### 3.1 Différentiel

`field_solver_2d.py` résout Laplace sur une grille en volumes finis non
uniforme par relaxation SOR rouge-noir (`solve_potential_sor:51`), avec
extraction de la capacité par l'énergie (`energy_from_potential:100`). La
permittivité effective vient du rapport C/C₀ entre la solution diélectrique et
la solution à vide, et `Zdiff = 1/(c·√(C·C₀))`.

Critère d'arrêt : `tolerance=2.0e-7` sur la variation maximale, plafonné à
`max_iterations=3000` (`field_solver_2d.py:51`). **Le code ne signale pas si le
plafond est atteint sans convergence** — un cas mal conditionné produirait un
nombre sans avertissement. **[supposition]** je n'ai pas construit de cas qui
sature le plafond ; je signale la possibilité, pas un défaut constaté.

Deux géométries : `GroundedCoplanarDifferentialSolver` (coplanaire, masses
latérales) et `EdgeCoupledDifferentialSolver` (microstrip/stripline bord à
bord). Le second est utilisé pour `MICROSTRIP`/`EMBEDDED_MICROSTRIP`
(`differential_impedance.py:396`) et `STRIPLINE`/`ASYMMETRIC_STRIPLINE`
(`:418`). Les formules fermées ne servent plus qu'en repli.

### 3.2 PDN

Maillage résistif du rail et du retour, condensateurs en RLC forfaitaire
(`ac_solver.py:111` pour l'impédance série), balayage fréquentiel avec une
matrice complexe résolue par point.

---

## 4. Réalisme

### 4.1 Troncature du domaine — bug réel, actuellement latent

La marge latérale du domaine vaut `max(4·h, 2·w)` et **ne croît pas avec
l'écartement**. En ouvrant le gap, la frontière miroir se rapproche
proportionnellement et comprime le champ.

Test de la limite de découplage — physiquement, `Zdiff` doit croître de façon
**monotone** vers `2·Z0_simple` quand le gap s'ouvre :

| gap (mm) | marge/gap | Zdiff (Ω) |
|---|---|---|
| 0,15 | 5,33 | 89,4 |
| 1,00 | 0,80 | 121,9 |
| 2,00 | 0,40 | **124,3** ← maximum |
| 8,00 | 0,10 | 119,8 |
| 16,00 | 0,05 | 111,0 |

La courbe **redescend** au-delà de 2 mm. C'est non physique et le mécanisme est
clair.

**Mais l'exclusion des breakouts protège l'utilisateur.**
`_is_breakout_geometry` (`differential_impedance.py:88-90`) écarte tout
`gap > max(0,30 ; 3·w)`. Dans la plage qualifiée, le rapport marge/gap reste
≥ 1,33 et les valeurs sont monotones et plausibles :

| w=0,2 h=0,2 | gap 0,10 | 0,20 | 0,30 | 0,60 (seuil) |
|---|---|---|---|---|
| Zdiff | 78,2 | 96,8 | 106,2 | 117,6 |

Conclusion honnête : **le bug n'altère pas les chiffres actuels de
l'utilisateur**, parce qu'un garde-fou indépendant coupe avant. C'est une
protection fortuite, pas une conception ; élargir le seuil de breakout ou
traiter une paire à fort écartement réveillerait le défaut.

### 4.2 Le solveur 2D contre les formules qu'il remplace

Dans le domaine de validité revendiqué de l'IPC-D-317A (w/h entre 0,1 et 3) :

| w/h | 2D (Ω) | IPC (Ω) | écart |
|---|---|---|---|
| 0,50 | 107,7 | 131,8 | −18,3 % |
| 1,00 | 88,9 | 102,1 | −12,9 % |
| 2,00 | 68,6 | 68,4 | +0,3 % |
| 3,00 | 55,7 | 47,4 | +17,5 % |

Les deux méthodes se croisent vers w/h = 2 et divergent des deux côtés. Cet
écart est **cohérent avec l'erreur de 10-25 % attribuée à l'IPC** et, compte
tenu des deux contrôles exacts de la section 1, le solveur 2D est le plus
crédible des deux. Je ne dispose pas d'un cas de référence publié pour trancher
dans l'absolu — c'est la limite de cette validation, et elle mérite d'être
levée par une comparaison à un solveur commercial ou à une mesure TDR.

### 4.3 « ESTIMATE » ne veut pas dire ce qu'on croit

`result.trustworthy` (`differential_impedance.py:563-567`) exige
`all(section.trustworthy for section in result.sections)` — sur **toutes** les
sections, y compris les breakouts, qui sont marqués `trustworthy=False` par
construction (`:299`).

Toute paire arrivant sur un connecteur possède au moins une section de
breakout, donc bascule automatiquement en `ESTIMATE`. C'est exactement ce
qu'on observe : 4 paires USB sur 5.

Le statut mélange donc deux choses distinctes :
« une section n'a pas pu être qualifiée » et « le nombre est peu fiable ». La
moyenne pondérée est calculée sur les sections `solved` uniquement (`:553-557`),
qui peuvent parfaitement être fiables.

**Et la couverture n'est nulle part rapportée.** `adapt_differential_result`
(`analysis_adapters.py:340`) publie `Zdiff` et le skew, rien sur la fraction de
longueur qualifiée. L'utilisateur ne peut pas savoir si 82,19 Ω couvre 90 % de
la liaison ou 20 %.

### 4.4 Violation de la règle d'honnêteté

`_finding` (`analysis_adapters.py:123-133`) code en dur
`confidence=EvidenceConfidence.DETERMINISTIC` pour **tous** les findings qu'il
construit, dont `SI-DIFF-001`.

Le rapport réel de l'utilisateur affiche donc :

> `SI-DIFF-001` — `J2_ESD_D: Estimate` **(DETERMINISTIC)**

Un finding dont le titre dit « Estimate » est étiqueté « déterministe ». C'est
une contradiction interne visible dans le livrable, et une violation directe de
la règle centrale du projet. Le même défaut affecte les findings DC, CFD et
thermiques construits par ce helper.

### 4.5 Le PDN est optimiste là où ça compte

Trois éléments absents, vérifiés par recherche sur tout le dépôt :

| Élément | Présent ? | Effet sur Z(f) |
|---|---|---|
| Inductance de montage (boucle pad+via) | **non** | sous-estime Z en HF |
| Dérating DC bias des MLCC | **non** | sur-estime C, sous-estime Z |
| Capacité de cavité des plans | **non** | sur-estime Z en HF |

Les deux premiers rendent le PDN **meilleur qu'il n'est** au-dessus de quelques
MHz. L'inductance de montage (typiquement 1-2 nH) domine généralement l'ESL du
boîtier ; l'omettre est le biais le plus lourd.

Or le pire cas de l'utilisateur — 0,358 Ω à 100 MHz — se situe précisément dans
cette zone. **Le chiffre réel est probablement plus élevé**, et rien dans le
rapport ne l'indique. La limitation quasi-statique récemment ajoutée avertit
au-delà de 179 MHz, mais ces trois omissions mordent bien avant.

Le modèle de condensateur lui-même (`ac_model.py:468-472`) :

```python
esr_ohm=0.01,                                    # constante, tous cas
esl_h=self._default_esl(...),                    # table par boîtier
model_source="value-and-package-estimate",
```

**ESR = 0,01 Ω pour tout condensateur**, quelle que soit sa valeur, son boîtier
ou son diélectrique. C'est un ordre de grandeur d'écart selon les cas. L'ESL
vient d'une table boîtier (`:430-436`, 0201→0,25 nH … 1206→1,0 nH), qui est
l'ESL du composant seul.

À décharge : `model_source` est honnêtement renseigné. La donnée existe pour
qui la lit ; elle n'est simplement pas rendue dans le rapport.

---

## 5. Complétude

| Absent | Pourquoi ça compte | Difficulté |
|---|---|---|
| Diaphonie branchée sur la géométrie réelle | `XT-001` est purement géométrique alors que `crosstalk_2d` sait chiffrer | **faible** (le module existe) |
| Réflexions / stubs de vias | `transmission_line_abcd` existe et n'est pas appelé | **faible** (idem) |
| Inductance de montage des découplages | biais optimiste dominant en HF | faible |
| Dérating DC bias MLCC | 50-80 % de C perdue à la tension nominale | faible |
| Pertes (peau, tanδ, rugosité) | pas d'atténuation, donc pas de budget de liaison | moyenne |
| Capacité de cavité des plans | manque le plancher HF du PDN | moyenne |
| Impédance PDN par broche | le port unique ne qualifie pas les autres charges | moyenne |
| Gravure trapézoïdale | biais systématique de quelques % sur Z | moyenne |
| Budget de bruit / œil / gigue | pas de verdict de liaison, seulement Z et skew | haute |
| Anti-résonances inter-condensateurs | pics de Z entre bancs de découplage | haute |

---

## 6. Acquisition des données

| Entrée | Source actuelle | Source possible | Gain |
|---|---|---|---|
| Paires différentielles | regex sur suffixes de noms (`differential_discovery.py:123`) | `EPT_*` + hiérarchie schéma | robustesse hors conventions |
| Interface (USB/PCIe/…) | jetons dans le nom (`:111-127`) | MPN du connecteur, datasheet | déterminisme |
| Impédance cible | table `INTERFACE_DEFAULTS` (`:12-23`) | idem, ou champ schéma | traçabilité |
| Empilage (εr, épaisseurs) | **lu du PCB**, `trusted` | — | déjà correct |
| ESR condensateur | **constante 0,01 Ω** | MPN → datasheet / modèle | premier ordre sur Z(f) |
| ESL condensateur | table boîtier | MPN + géométrie de montage | premier ordre |
| Courant transitoire | 50 % du courant DC configuré | profil de charge, datasheet | cadre la cible |
| Tolérance de rail | saisie (2 % ici) | schéma / spec du régulateur | — |
| Temps de montée | **absent du chemin SI** | interface détectée → standard | débloque diaphonie et réflexions |

Le point le plus rentable est le **MPN des condensateurs**. `ingest/schematic_reader.py`
extrait déjà `Manufacturer_Part_Number` et `Datasheet` ; le PDN ne les consomme
pas. Passer d'un ESR constant à une valeur par référence changerait l'allure de
Z(f) bien plus que tout raffinement de maillage.

---

## 7. Propositions, classées par gain/effort

**1. Brancher `crosstalk_2d` sur la règle `XT-001`.** *Gain :* remplace un seuil
géométrique par une diaphonie chiffrée, sur du code déjà écrit et testé.
*Coût :* faible — il faut la géométrie de couplage (longueur parallèle,
écartement, empilage) que `emc_analyzer` possède déjà, plus un temps de montée.
*Risque :* la règle changera de verdict sur certaines cartes ; c'est le but.

**2. Corriger l'étiquette de confiance de `_finding`.** *Gain :* supprime une
contradiction visible dans le livrable (« Estimate » étiqueté
« DETERMINISTIC ») et rétablit la règle centrale du projet. *Coût :* très
faible — passer la confiance en paramètre. *Risque :* des tests épinglent
peut-être la valeur actuelle. Concerne aussi DC, thermique et CFD.

**3. Rapporter la fraction de longueur qualifiée.** *Gain :* l'utilisateur sait
enfin si `Zdiff` couvre l'essentiel de la liaison ou une minorité. *Coût :*
faible — `total_length` des sections `solved` rapporté à la longueur totale, les
deux sont déjà calculés. *Risque :* nul.

**4. Ajouter l'inductance de montage au modèle de découplage.** *Gain :* corrige
le biais optimiste dominant en HF, là où se situe le pire cas actuel. *Coût :*
faible — géométrie pad/via disponible, formule de boucle classique. *Risque :*
les Z(f) vont **monter** ; des cartes jugées conformes ne le seront plus. C'est
une correction, pas une régression, mais elle doit être annoncée.

**5. Lire ESR/ESL depuis le MPN.** *Gain :* premier ordre sur Z(f). *Coût :*
moyen — nécessite une table ou une source datasheet ; dégradation propre vers
l'estimation actuelle quand le MPN est inconnu, avec `model_source` distinguant
les deux. *Risque :* faible si le repli est explicite.

**6. Faire croître la marge du domaine avec l'écartement.** *Gain :* supprime un
bug latent avant qu'il ne morde. *Coût :* faible — `side_margin` devrait
dépendre de `gap`. *Risque :* domaines plus grands, donc calcul plus lent ; à
mesurer.

**7. Brancher `transmission_line_abcd` sur les stubs de vias.** *Gain :* une
famille d'analyse SI absente devient disponible. *Coût :* moyen — il faut
extraire la longueur de stub réelle depuis l'empilage et les couches du via.
*Risque :* faible.

**8. Signaler la non-convergence du SOR.** *Gain :* évite un nombre silencieux
sur un cas mal conditionné. *Coût :* très faible. *Risque :* nul.

---

## 8. Ce que je n'ai pas pu établir

* **La justesse absolue du solveur 2D.** Les deux contrôles exacts prouvent sa
  cohérence interne, pas sa fidélité au réel. Une comparaison à un solveur
  commercial ou à une mesure TDR sur une carte de référence reste à faire.
* **La saturation du plafond d'itérations SOR** : possible, non constatée.
* **L'ampleur réelle du biais PDN.** Je sais que l'inductance de montage et le
  dérating manquent et dans quel sens ils poussent ; je n'ai pas chiffré de
  combien sur cette carte, ce qui demanderait de les implémenter.

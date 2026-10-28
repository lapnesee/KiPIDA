# Audit thermique et CFD — Ki-PIDA

Audit des domaines thermique 3D, couplage électrothermique et CFD d'enceinte,
à l'état du commit `2ea96d0`.

Contrairement à l'audit initial (`docs/refonte-analyses.md`), celui-ci s'appuie
sur des **exécutions réelles** : carte d'alimentation 6 couches, 80 × 35 mm,
129 empreintes, 1,41 W injectés, avec une étude de convergence de maillage
mesurée.

Convention : les affirmations sont référencées `fichier:ligne`. Ce qui relève
de l'**hypothèse** de ma part est marqué *(supposition)*.

---

## 1. Ce qui est solide

Il faut le dire avant la critique, parce que la base est saine.

**L'empilage réel est lu, pas supposé.** `thermal_mesh.py:434-470` construit les
tranches depuis `stackup` : épaisseur de chaque couche de cuivre
(`info.get("thickness_mm")`) et de chaque diélectrique
(`substrate.get("thickness_mm")`), dans l'ordre physique. Sur la carte de test
cela donne 11 tranches (6 cuivre + 5 diélectriques) aux épaisseurs réelles du
`.kicad_pcb`. Un défaut n'est utilisé qu'en dernier recours (`:457`, `:464-469`).

**Le FR4 est traité comme anisotrope.** `FR4_K_XY = 0.8` et `FR4_K_Z = 0.3`
(`thermal_mesh.py:313-314`) sont appliqués séparément : la conductivité latérale
sert aux branches XY (`:618`, `:651-655`), la verticale aux branches Z
(`:619`, `:663-670`). C'est physiquement correct et souvent omis ailleurs.

**La discrétisation est un vrai volume fini.** Conductances latérales par moyenne
harmonique des conductivités voisines (`:474`, `:651-653`), conductances
verticales par résistances en série demi-épaisseur (`:663-666`). Ce n'est pas un
réseau nodal ad hoc.

**Le couplage électrothermique est réel, pas cosmétique.** `electrothermal.py:224-233` :

```python
scales = np.clip(
    1.0 + float(settings.copper_temp_coefficient_per_c) * (branch_temperatures - 20.0),
    0.2, 3.0,
)
detailed = dc_solver.solve_detailed(..., branch_resistance_scales=scales, ...)
```

Chaque branche électrique reçoit **sa** température locale, lue dans le maillage
thermique (`:217-223`), et sa résistance est corrigée. Les pertes cuivre
mesurées passent de 0,0339 à 0,0372 W sur 8 itérations (+9,7 %), cohérent avec
α ≈ 0,00393 /K et ΔT ≈ 25 K. La boucle converge proprement (Δ 62,5 → 0,07 °C).

**La provenance de la puissance est tracée.** `thermal_model.py:103,108,140,157`
distingue `power-tree-load`, `power-tree-external-load`, `regulator-loss` et
`estimated`. Les connecteurs (`J*`) sont exclus de la dissipation locale par
défaut (`:82-83`), ce qui est le bon choix pour une carte qui exporte sa
puissance.

**La qualité numérique est excellente.** Erreur de bilan énergétique ~10⁻¹⁰ %,
résidu ~10⁻⁸, backend CUDA, matrice CSR mise en cache entre itérations couplées
(`thermal_solver.py:57-65`). Rien à redire sur le solveur en tant que tel.

---

## 2. Modèle physique résolu

Équation stationnaire de conduction avec pertes convectives et radiatives
surfaciques, discrétisée en volumes finis sur une grille cartésienne régulière
en XY et **structurée par tranches** en Z (une tranche par couche de l'empilage,
épaisseur physique variable).

Pour chaque cellule *i* :

```
Σ_j G_ij (T_j − T_i)  +  G_conv,i (T_amb − T_i)  +  Q_i  =  0
```

| Terme | Expression | Code |
|---|---|---|
| Latéral | `G = k_harm · dz` (cellules carrées, aire/distance = dz) | `thermal_mesh.py:651-655` |
| Vertical | `G = dx·dy / (t₁/2k₁ + t₂/2k₂)` | `:663-670` |
| Via | `G = k_Cu · A_annulaire / e_carte` | `:682-685` |
| Surface | `G = (h_conv + h_ray) · dx·dy` | `:687-696`, `:714-718` |
| Source | `Q = P_composant / n_cellules` | `:798-799` |

Assemblage COO → CSR avec `sum_duplicates` (`thermal_solver.py:118-126`),
résolution SPD via `SparseComputeBackend`.

La température de jonction n'est **pas** dans le maillage : elle est ajoutée
après coup (`thermal_solver.py:177`) :

```python
junction = board_temperature + component.power_w * max(0.0, component.theta_jb_c_per_w)
```

---

## 3. Complétude — ce qui manque

| Absent | Pourquoi ça compte | Difficulté |
|---|---|---|
| **Fraction de cuivre par cellule** | Échantillonnage **binaire** (`:615-619`) : une cellule est 385 W/m·K ou 0,8. Cause directe des 7,4 °C d'écart entre maillages (§4.1). | Moyenne |
| **Régime transitoire** | Démarrage, charges pulsées, protections thermiques. Une alim USB-PD a des transitoires de commutation de charge que le stationnaire ignore. | Haute |
| **Modèle de composant** | Boîte à puissance uniforme, θJB forfaitaire post-hoc. Pas de θJC, pas de boîtier, pas de die, pas de modèle 2R ni DELPHI. Le boîtier ne conduit ni ne rayonne. | Haute |
| **Vias en réseau discret** | Un via = **une seule branche** couche 0 → couche N (`:678-685`), court-circuitant les couches intermédiaires. Un via thermique sous un QFN ne dépose donc rien dans les plans internes qu'il traverse. | Moyenne |
| **Résistance de contact / TIM** | Aucune. Brasure, colle, pad thermique, entrefer : tout est parfait. Sous-estime systématiquement ΔT. | Moyenne |
| **Facteur de forme du rayonnement** | `h_ray = 4εσT³` sans facteur de forme (`:691-694`) : rayonnement vers un ambiant infini. Dans une enceinte fermée, faux. | Moyenne |
| **Corrélation de convection** | `NATURAL` renvoie **5,0 W/m²K en dur** (`:432`). Pas de Rayleigh, pas d'orientation, pas de longueur caractéristique. Voir §4.2. | Faible |
| **Couplage CFD ↔ thermique** | Inexistant. Le `h` du thermique ne vient jamais de la CFD (vérifié : aucune référence `cfd` dans `thermal_mesh.py`, `thermal_model.py`, `electrothermal.py`). Voir §6. | Haute |
| **Conductivité réelle du diélectrique** | `FR4_K_XY/K_Z` fixes quel que soit le matériau. La carte de test déclare `JLC_3313`, `JLC_2116`, `JLC_FR4` — noms lus (`:459`) mais jamais exploités. | Faible |
| **Dépendance en température des propriétés** | k(T) du cuivre et du FR4 constants. Effet secondaire ici *(supposition : < 2 % sur cette plage)*. | Faible |

### CFD d'enceinte

- **Laminaire uniquement**, sans modèle de turbulence (`cfd_solver.py` : seule une
  viscosité moléculaire apparaît, `:421`, `:450`). Correctement déclaré dans les
  limitations (`analysis_adapters.py`, `adapt_cfd_result`).
- **Grille cartésienne structurée** (`cfd_mesh.py:20-41`), sans maillage épousant
  les corps ni raffinement de couche limite. Or c'est la couche limite qui fixe
  le transfert thermique.
- Buoyancy de Boussinesq, solveur à projection pseudo-transitoire.
- **Aucune validation** : pas de cas de référence, pas de comparaison analytique
  ou expérimentale dans le dépôt *(supposition : recherche de `tests/test_cfd_*`
  — ils vérifient la mécanique du solveur, pas la justesse physique)*.

---

## 4. Réalisme

### 4.1 Convergence de maillage — le facteur d'erreur dominant

C'est le résultat le plus important de cet audit, et il est **mesuré** :

| Grille | Nœuds | Point chaud |
|---|---|---|
| 0,5 mm | 123 068 | **85,66 °C** |
| 0,1 mm | 3 076 304 | **78,29 °C** |

**7,4 °C d'écart pour un rapport de 5 en pas.** Le maillage à 0,5 mm n'est pas
convergé, et rien ne prouve que celui à 0,1 mm le soit — le troisième point
(0,05 mm) est inaccessible : il projette 12,3 M nœuds contre un plafond de 4 M,
et sera ramené à ~0,089 mm.

La cause est l'échantillonnage binaire (`:615-619`). Une cellule de 0,5 mm qui
recouvre une piste de 0,25 mm est comptée **entièrement cuivre** ou
**entièrement FR4** selon que son centre tombe sur la piste. La conductivité
effective de la carte est donc une fonction en escalier du pas de grille, pas
une quantité convergente.

**L'outil ne dit rien de cette incertitude.** Le rapport annonce
« Hotspot 78,29 °C » avec un statut PASS/WARN et aucune barre d'erreur. Un
lecteur conclut légitimement que la carte monte à 78 °C ; l'étude de convergence
dit que le chiffre bouge de 7 °C selon un réglage de maillage.

> **Manquement à la culture du projet.** Le dépôt s'interdit de présenter une
> estimation comme une mesure. Ici une valeur non convergée est présentée comme
> un résultat, sans que sa sensibilité au maillage soit exposée.

### 4.2 Coefficients de surface — sous-estimés d'un facteur ~1,9

Calcul vérifié (air à ~50 °C de film, L = A/P = 12,2 mm pour cette carte) :

| Grandeur | Code | Physique | Écart |
|---|---|---|---|
| Convection naturelle, face supérieure | 5,0 | **10,9** (Nu = 0,54·Ra^¼, Ra ≈ 5,9×10³) | 2,2× |
| Convection naturelle, face inférieure | 5,0 | **5,4** (Nu = 0,27·Ra^¼) | ≈ 1× |
| Rayonnement (ε = 0,9) | 5,41 (à T_amb = 25 °C) | **8,86** (à T_surf = 78 °C) | 1,64× |
| **Total face supérieure** | **10,4** | **19,9** | **1,9×** |

Deux erreurs distinctes :

1. **La convection naturelle est une constante** (`:432`), indépendante de ΔT, de
   l'orientation et de la taille de la carte. Elle se trouve correcte pour la
   face inférieure et trop faible d'un facteur 2 pour la supérieure.
2. **Le rayonnement est linéarisé autour de l'ambiante** (`:690-694`) :
   `h_ray = 4εσT_amb³`. La linéarisation doit se faire autour de la température
   de **surface**. À 78 °C, cela sous-estime de 64 %.

**Conséquence chiffrée.** Bilan global sur 56 cm² exposés et 1,41 W :

| | h moyen | Élévation moyenne |
|---|---|---|
| Code | 10,4 | 24,2 K → 49,2 °C |
| Physique | 15,4 | 16,3 K → 41,3 °C |

Le point chaud reporté (78,29 °C) se décompose en ~24 K d'élévation moyenne plus
~29 K d'étalement local. En corrigeant les coefficients de surface, l'élévation
moyenne tombe à ~16 K et le point chaud vers **~70 °C**.

**Donc les deux biais principaux vont en sens opposés** : le maillage grossier
surestime, les coefficients de surface sous-estimés surestiment aussi. Le
chiffre de 78,29 °C est probablement **haut de 5 à 10 °C** *(supposition fondée
sur le bilan global ci-dessus ; une vérification exigerait un run avec
corrélation de Rayleigh)*.

### 4.3 L'erreur de bilan énergétique induit en erreur

`analysis_adapters.py` publie :

```python
AnalysisMetric("energy_balance", "Energy balance error", balance, "%",
               "PASS" if abs(balance) <= 5.0 else "WARN")
```

À 8×10⁻¹¹ %, c'est affiché **PASS**. Or cette grandeur mesure uniquement que le
solveur linéaire a convergé — que la puissance entrante égale la puissance
sortant par les frontières. Elle serait tout aussi excellente avec un `h` faux
d'un facteur 10 ou un maillage divergent de 20 °C.

C'est le seul indicateur de « qualité » exposé, et il est **cohérence numérique**
présentée comme **justesse physique**. Un utilisateur non averti lit
« erreur 10⁻¹⁰ % → PASS » et fait confiance au 78,29 °C.

### 4.4 Ce qui n'est pas reporté du tout

`ThermalResult.convection_coefficient_w_m2k` est calculé et stocké
(`thermal_solver.py:227`) mais **jamais publié** dans les métriques. Idem pour :
l'ambiante, l'émissivité, le mode de flux d'air, le pas de grille effectif (sauf
en cas de dégradation, ajouté récemment), et surtout le `thermal_source` de
chaque composant (`estimate` vs datasheet) qui existe sur
`ComponentThermalResult` mais ne remonte pas.

Les hypothèses de premier ordre sont donc invisibles dans le rapport.

### 4.5 Nombres magiques

| Valeur | Lieu | Justification |
|---|---|---|
| `5.0` W/m²K (convection naturelle) | `thermal_mesh.py:432` | aucune |
| `5.7 + 3.8·v` (convection forcée) | `:431` | corrélation plaque plane classique, plausible |
| `1.25 − 0.5·s` (profil le long du flux) | `:715` | **inventé**, aucune corrélation |
| `plating_mm = 0.025` (via) | `:682` | valeur d'atelier plausible, non configurable |
| `FR4_K_XY = 0.8`, `K_Z = 0.3` | `:313-314` | ordre de grandeur correct pour FR4 |
| `θJB = 20.0` °C/W | `models.py:821` | **arbitraire**, aucune source |
| `Tj_max = 125` °C | `models.py:822` | défaut courant, mais arbitraire |
| Boîtier `3 × 3 × 1` mm | `models.py:818-820` | **plancher** arbitraire ; écrase les petits boîtiers (§5) |
| `1.53 / (n−1)` mm (épaisseur diélectrique de repli) | `thermal_mesh.py:457` | carte 1,6 mm supposée |

### 4.6 Le couplage électrothermique pèse peu ici

Les pertes cuivre valent 0,037 W sur 1,41 W, soit **2,6 %**. Le couplage est
correct mais son effet sur cette carte est mineur. Il deviendra significatif sur
une carte où le cuivre dissipe une fraction notable — ce n'est pas le cas d'une
alimentation qui exporte sa puissance par connecteur.

---

## 5. Acquisition des données

| Entrée | Source actuelle | Source possible | Gain |
|---|---|---|---|
| Puissance par composant | **Déduite** : P = V×I du power tree + pertes régulateur (`thermal_model.py:100-140`) | — | déjà bon |
| Épaisseurs de l'empilage | **Déduite** du `.kicad_pcb` (`:434-470`) | — | déjà bon |
| Conductivité du diélectrique | **Défaut** fixe 0,8 / 0,3 | Nom du matériau déjà lu (`:459`) → table k par famille | faible mais gratuit |
| θJB | **Défaut arbitraire** 20 °C/W | Datasheet via champ `Datasheet`/MPN du schéma | **fort** |
| Tj_max | **Défaut arbitraire** 125 °C | idem | **fort** |
| Dimensions du boîtier | **Déduite** de l'encombrement des pads + 0,5 mm, **plancher** à 3×3 mm (`thermal_model.py:324-331`) | Courtyard / `F.Fab` de l'empreinte (contour réel du corps) | moyen |
| Ambiante | Saisie | — | — |
| Émissivité | Défaut 0,9 | — | acceptable |
| Débit / mode d'air | Saisie | CFD (non couplée) | moyen |
| Coefficient de convection | **Calculé, mais par une constante** | Corrélation Rayleigh sur la géométrie réelle | **fort**, facile |

**Aucune datasheet n'est lue par le domaine thermique** (vérifié : aucune
occurrence de `datasheet` dans `thermal_model.py` ni
`application/thermal_controller.py`). Le paquet `ingest/schematic_reader.py`
expose pourtant `datasheet` et `extra_fields` (MPN Mouser, Manufacturer_Part_Number)
sur chaque symbole — le gisement existe et n'est pas exploité.

Les dimensions de boîtier méritent une nuance. `thermal_model.py:324-331` les
dérive bien de la géométrie :

```python
width = max(x) - min(x)   # encombrement des pads
return max(default_width, width + 0.5), max(default_depth, depth + 0.5)
```

Ce n'est donc pas un défaut aveugle, mais deux biais subsistent :

- **L'encombrement des pads n'est pas le corps du composant.** Pour un
  connecteur à pads écartés il surestime ; pour un QFN à pavé thermique il
  approche correctement. Le contour `F.Fab` du boîtier, présent dans
  l'empreinte, serait exact.
- **Le plancher de 3 × 3 mm domine les petits boîtiers.** Une résistance 0402
  (pads ~1,0 × 0,5 mm) se voit attribuer 3 × 3 mm, soit **18 fois** l'aire
  réelle. Sa puissance est donc étalée sur 9 mm² au lieu de 0,5 mm², ce qui
  **sous-estime** son échauffement local. Sur une carte où les pertes se
  concentrent dans quelques passifs, c'est un biais non négligeable.

---

## 6. La CFD d'enceinte : verdict

**Dans son état actuel, elle ne produit rien d'exploitable pour une décision de
conception.** Trois raisons :

1. **Elle n'est couplée à rien.** Le thermique 3D calcule ses températures avec
   un `h` forfaitaire ; la CFD calcule les siennes avec son propre champ de
   conductivité solide. Les deux analyses coexistent sans jamais s'échanger la
   grandeur qui les relierait — le coefficient de transfert local. La valeur
   d'une CFD d'enceinte est précisément de **fournir ce `h`** ; ici elle ne le
   fait pas.

2. **Laminaire sur grille cartésienne, sans raffinement de couche limite.** Le
   transfert thermique se joue dans la couche limite, qui n'est pas résolue. En
   convection naturelle d'enceinte les nombres de Rayleigh dépassent couramment
   10⁶–10⁸, régime transitionnel voire turbulent — hors domaine du modèle.

3. **Aucune validation.** Rien n'établit que les températures produites soient
   justes à mieux qu'un ordre de grandeur.

Les limitations le déclarent honnêtement (« Laminar steady-state model;
turbulence, fan blades, radiation, and transients are excluded »), ce qui est à
porter au crédit du projet. Mais un utilisateur qui lit « Maximum solid
temperature 82 °C » avec un statut PASS retiendra le chiffre, pas la note de bas
de page.

**Coût / valeur : 820 lignes pour un résultat non couplé et non validé.**
Deux issues défendables :

- **Soit** la coupler — la CFD calcule un `h` local par patch de surface, que le
  thermique 3D consomme à la place de sa constante. C'est le seul chemin qui
  justifie son existence, et il rendrait le §4.2 caduc.
- **Soit** la marquer explicitement comme exploratoire dans le rapport
  (statut `NO_DATA` plutôt que PASS/WARN, ou un bandeau « indicatif, non
  validé »), et cesser d'investir dedans.

Le pire est l'état actuel : assez aboutie pour produire des chiffres crédibles,
pas assez pour qu'ils le soient.

---

## 7. Propositions, classées par gain / effort

### Rang 1 — Corrélation de convection réelle *(gain fort, effort faible)*

Remplacer la constante 5,0 par une corrélation Rayleigh utilisant la longueur
caractéristique de la carte et l'orientation de la face :

```
L = A / P                        (longueur caractéristique)
Ra = g·β·ΔT·L³ / (ν·α)
Nu = 0,54·Ra^¼   (face chaude vers le haut)
Nu = 0,27·Ra^¼   (face chaude vers le bas)
h  = Nu·k / L
```

ΔT n'est pas connu a priori : itérer deux ou trois fois (h → T → h) converge
vite, et la boucle électrothermique existe déjà pour l'accueillir.

- **Gain** : supprime un biais mesuré de 2,2× sur la face supérieure. À lui seul
  il déplace le point chaud de ~8 °C.
- **Coût** : ~60 lignes dans `thermal_mesh.convection_coefficient` + une boucle.
- **Risque** : change tous les résultats thermiques publiés. Les tests de
  non-régression thermiques devront être recalibrés — à faire en conscience,
  avec la justification physique en commentaire.

### Rang 2 — Rayonnement linéarisé autour de la surface *(gain fort, effort très faible)*

`h_ray = 4εσT_surf³` au lieu de `T_amb³`, réévalué dans la boucle couplée.

- **Gain** : 1,64× sur le terme radiatif, soit ~40 % du transfert total.
- **Coût** : 5 lignes, la boucle d'itération existe.
- **Risque** : faible. Attention à la stabilité si ε est grand *(supposition :
  la relaxation existante suffit)*.

### Rang 3 — Fraction de cuivre par cellule *(gain fort, effort moyen)*

Remplacer le test binaire `intersects_xy` par une fraction d'aire, et pondérer :
`k_eff = f·k_Cu + (1−f)·k_FR4`. Techniquement identique au *cut-cell* déjà
implémenté pour le maillage DC des zones (`mesh_hybrid.py`), donc le motif
existe dans le dépôt.

- **Gain** : rend la conductivité effective **convergente** en maillage. Devrait
  réduire fortement l'écart de 7,4 °C, et permettrait de faire confiance à un
  maillage grossier — donc **plus rapide**, pas seulement plus juste.
- **Coût** : ~150 lignes ; l'échantillonnage par bandes doit renvoyer une
  fraction au lieu d'un booléen.
- **Risque** : moyen, c'est le cœur du mailleur. Nécessite une étude de
  convergence avant/après pour prouver le gain.

### Rang 4 — Publier les hypothèses dans le rapport *(gain moyen, effort très faible)*

Exposer en métriques : `h` convectif retenu, ambiante, émissivité, pas de grille
effectif, et par composant son `thermal_source`. Requalifier l'erreur de bilan
énergétique en `INFO` avec un libellé disant qu'elle mesure la convergence du
solveur, **pas** la justesse physique.

- **Gain** : rétablit la discipline du projet. Coût quasi nul.
- **Risque** : nul.

### Rang 5 — Boîtier et θJB depuis les données existantes *(gain fort, effort moyen)*

Deux corrections distinctes :

- **Dimensions** : lire le contour `F.Fab` de l'empreinte plutôt que
  l'encombrement des pads, et surtout **abaisser le plancher de 3 × 3 mm** qui
  étale la puissance d'un 0402 sur 18 fois son aire réelle.
- **θJB et Tj_max** : depuis la datasheet via MPN, avec
  `thermal_source = "datasheet"` quand la valeur est trouvée et `"estimate"`
  sinon — l'infrastructure de provenance existe déjà
  (`models.py:828`, `thermal_solver.py:187`).

- **Gain** : θJB pilote directement la température de jonction, seule grandeur
  réellement actionnable du rapport ; le plancher de boîtier fausse les points
  chauds locaux des petits composants.
- **Coût** : faible pour le plancher, moyen pour la datasheet (dépend d'une
  source exploitable).
- **Risque** : faible, dégradation propre vers l'estimation.

### Rang 6 — Étude de convergence automatique *(gain moyen, effort faible)*

Résoudre à *h* et 2*h*, rapporter l'écart, et le publier comme incertitude de
maillage. Trois lignes de conclusion : « point chaud 78,3 °C, sensibilité au
maillage ±7 °C — non convergé ».

- **Gain** : transforme un chiffre nu en chiffre avec barre d'erreur. C'est
  exactement ce que la culture du projet réclame.
- **Coût** : un second solve à maillage double (~4× moins cher que le fin).
- **Risque** : nul.

### Rang 7 — Vias en réseau discret *(gain moyen, effort moyen)*

Connecter chaque via aux couches qu'il traverse plutôt que bas→haut directement.

- **Gain** : réel sous les composants à pavé thermique, où les vias déposent la
  chaleur dans les plans internes. Sur la carte de test *(supposition : effet
  modéré, peu de vias thermiques sous boîtier)*.
- **Coût** : ~40 lignes.
- **Risque** : faible.

### Rang 8 — Trancher le sort de la CFD *(gain variable, effort haut ou nul)*

Coupler (fournir `h` local au thermique) ou déclasser en exploratoire. Voir §6.

### Hors classement — le transitoire

Utile (démarrage, charges pulsées) mais c'est un chantier : matrice de capacité
thermique, schéma d'intégration, nouvelle UI, nouveau format de résultat. À
n'envisager qu'après avoir fiabilisé le stationnaire, sans quoi on bâtirait du
transitoire sur des coefficients de surface faux d'un facteur 2.

---

## 8. Synthèse

Le solveur thermique est **numériquement excellent et physiquement approximatif**.
La discrétisation est correcte, le couplage électrothermique authentique, la
provenance de la puissance tracée. Mais trois hypothèses de premier ordre —
convection constante, rayonnement linéarisé au mauvais point, échantillonnage
binaire du cuivre — placent l'incertitude réelle autour de **±10 °C**, alors que
le rapport affiche un point chaud au centième de degré assorti d'une « erreur »
de 10⁻¹⁰ %.

Les quatre premières propositions (convection, rayonnement, cut-cell, publication
des hypothèses) sont peu coûteuses et attaquent ensemble l'essentiel de cet
écart. Elles feraient passer le thermique de « cohérent » à « défendable ».

La CFD, elle, demande une décision plutôt qu'une amélioration.

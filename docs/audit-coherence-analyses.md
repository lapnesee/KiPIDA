# Audit des analyses — cohérence, coordination, représentation du réel, confiance

Base : `7709d60` (branche `claude/pcb-analysis-tool-40t1dr`), 2026-09-13.

Cet audit ne refait pas les audits de domaine du 2026-09-04
(`audit-emc.md`, `audit-si-pdn.md`, `audit-thermique-cfd.md`) ni la validation
CFD (`validation-cfd.md`). Il porte sur ce qu'aucun d'eux ne couvre : **comment
les analyses se tiennent entre elles**, et **ce que vaut chaque chiffre publié**
face au réel. L'état de leurs propositions est vérifié dans le code actuel (§6).

Convention : **[mesuré]** = produit pour cet audit, reproductible ;
**[cité]** = mesure d'un document antérieur du dépôt, non refaite ;
**[déduit]** = raisonnement sur le code, sans exécution. Références `fichier:ligne`.

---

## 0. Synthèse

1. **Le moteur DC que l'utilisateur lance n'est pas le moteur qui a été validé.**
   L'analyse DC, le balayage AC et les pertes cuivre injectées dans le thermique
   et la CFD passent par le maillage rasterisé `mesh.Mesher`
   (`application/dc_controller.py:336`, `ac_model.py:739`). Le maillage hybride,
   exact sur les pistes et validé analytiquement, ne sert qu'au conseiller.
   Mesuré sur le rasterisé au pas par défaut (0,1 mm) : **−25 % à +50 %** sur une
   piste droite de 0,15 mm, **jusqu'à +112 %** à 45°, et **aucun chemin conducteur**
   pour les pistes fines à 45° dès que la grille est grossie (0,25 mm, ce que fait
   le mailleur de lui-même au-delà de 400 000 nœuds). [mesuré]
2. **Un même rapport peut porter deux chutes de tension pour le même rail** :
   celle du rasterisé (finding DC-003) et celle du maillage hybride (baseline des
   remédiations), avec en plus deux résistivités différentes (1,68 et
   1,72 × 10⁻⁸ Ω·m). [mesuré pour les constantes ; cité pour 7,7 mV contre
   6,558 mV sur +5V_RAIL]
3. **L'agrégation de campagne fusionne des défauts sans rapport** : deux
   findings sont « le même problème » dès qu'ils partagent un net. `GND` et les
   rails étant partout, une impédance PDN, un plan de masse fragmenté et une
   non-convergence DC deviennent une seule action, classée en tête. [mesuré]
4. **Identifiants de règle en collision** : `DC-001` et `DC-002` désignent à la fois
   des findings DC (îlot, non-convergence) et EMC (découplage). [mesuré]
5. **La confiance déclarée est bien construite mais mal calibrée à trois
   endroits** : `DC-001` se dit DETERMINISTIC alors qu'un îlot peut être un
   artefact du maillage ; `AC-001` se dit DETERMINISTIC sur un modèle PDN qui omet
   l'inductance de montage ; la conversion EMC `HIGH → DETERMINISTIC` promeut des
   seuils codés en dur au rang de faits. Et **les scores de campagne ignorent la
   confiance** : un finding heuristique pèse autant qu'un fait. [déduit, vérifié
   dans le code]
6. **La coordination explicite est bonne** (EMC après ses entrées, CFD avant le
   thermique en flux forcé, résultats de la campagne transmis à l'EMC), mais
   **le DC est résolu trois fois** au lieu d'être partagé, **aucune analyse
   physique ne consomme le schéma**, et **le cache incrémental n'est pas branché**.

Ce qu'on peut croire aujourd'hui : le **classement** et la **localisation** des
problèmes ; les impédances différentielles des sections qualifiées ; la chute DC
d'un rail porté par des plans. Ce qu'il ne faut pas lire comme une valeur : la
chute DC d'un rail routé en pistes fines, un point chaud thermique au dixième de
degré, un champ E en V/m, un score sur 100.

---

## 1. Échelles

**Représentation du réel (F)** — ce que le modèle capture de la physique.

| Niveau | Sens |
|---|---|
| F4 | Physique résolue, validée contre une référence externe dans le domaine d'usage |
| F3 | Physique résolue, cohérence interne vérifiée par cas exacts ; référence externe absente ou partielle |
| F2 | Modèle d'ingénieur : hypothèses de premier ordre ou discrétisation non convergée ; ordre de grandeur |
| F1 | Indicateur relatif : classement, localisation ; la valeur absolue ne s'interprète pas |
| F0 | Non atteint par l'utilisateur, ou résultat non exploitable en l'état |

**Confiance justifiée (C)** — ce que les preuves de validation autorisent à croire
du chiffre : *élevée*, *moyenne*, *faible*. **Calibration** — écart entre ce que
le produit **déclare** (`EvidenceConfidence`, statut, limitations) et la
confiance justifiée : *juste*, *sur-déclarée*, *sous-déclarée*.

---

## 2. Grille par analyse

| Analyse | F | C justifiée | Déclarée par le produit | Calibration |
|---|---|---|---|---|
| DC — chute de tension (rasterisé) | **F2** | faible (pistes), moyenne (plans) | DC-003 ESTIMATED ; DC-001 DETERMINISTIC | **sur-déclarée** (DC-001, limitations) |
| DC — conseiller what-if (hybride) | F3 | moyenne | `verified` + re-simulation | juste |
| DC — densité de courant | F1–F2 | faible | ESTIMATED + « pas une certification IPC » | juste |
| AC / PDN | F2 | faible au-dessus de quelques MHz | AC-001 ESTIMATED ou DETERMINISTIC | **sur-déclarée** (cas DETERMINISTIC) |
| Impédance différentielle (sections qualifiées) | F3 | moyenne | ESTIMATED ; statut « ESTIMATE » | juste sur la valeur, **ambiguë** sur le statut |
| Diaphonie XT-001 | F2–F3 | moyenne quand chiffrée | libellés distincts chiffré / espacement | juste |
| EMC — règles et score | F1 | faible sur l'absolu, moyenne sur le classement | HIGH/MEDIUM/LOW → DETERMINISTIC/ESTIMATED/HEURISTIC | **sur-déclarée** (HIGH → DETERMINISTIC) |
| EMC — champ proche E/H | F1 | faible | « quasi-statique, pas full-wave » | **sur-déclarée** (V/m sans avertissement de non-comparabilité) |
| EMC — phase 10 (SPICE / Palace) | F0 | — | statut en métadonnées | sans objet |
| Thermique 3D | F2 | faible à ±10 °C | point chaud PASS/WARN, pas de barre d'erreur | **sur-déclarée** sur la précision |
| Couplage électrothermique | F2 | hérite du DC rasterisé | — | juste |
| CFD d'enceinte | F1–F2 | faible sur les températures | CFD-004 renvoie au thermique | juste depuis `96b699b` |
| Règles schéma SCH-001..005 | **F0** | — | DETERMINISTIC | injoignables ; sur-déclarées si branchées |
| Agrégation de campagne | F1 | faible | verdict et scores | **sur-déclarée** (fusion, poids) |

### 2.1 DC — chute de tension

**Modèle.** Laplace résistif sur le seul net du rail (`solver.py`), sources en
Dirichlet, charges en injection. Chute publiée = `(Vmax − Vmin) / Vmax` sur tout
le net (`analysis_adapters.py:927-928`), pas la tension vue par chaque charge. Le
retour de masse n'est pas modélisé. Résistivité 1,68 × 10⁻⁸ Ω·m
(`extractor.py:337`), sans référence de température ; seule la boucle
électrothermique corrige par α = 0,00393 /K depuis 20 °C.

**Discrétisation — mesure.** `validation/dc_raster_fidelity.py`, piste de 20 mm,
même ρ des deux côtés, alignement de grille balayé sur 8 positions. [mesuré]

| Piste | Pas 0,1 mm (défaut) droite | Pas 0,1 mm à 45° | Pas 0,25 mm droite | Pas 0,25 mm à 45° |
|---|---|---|---|---|
| 0,15 mm | ×0,75 à ×1,50 | ×1,06 à ×2,12 | ×0,60, **ouvert** 3/8 | **ouvert** |
| 0,25 mm | ×0,83 à ×1,25 | ×1,18 à ×1,77 | ×0,50 à ×1,00 | **ouvert** |
| 0,50 mm | ×0,83 à ×1,00 | ×1,18 | ×0,67 à ×1,00 | ×1,41 |
| 1,00 mm | ×0,91 à ×1,00 | ×1,01 à ×1,09 | ×0,80 à ×1,00 | ×1,13 à ×1,41 |
| 2,00 mm | ×0,95 à ×1,00 | ×1,01 à ×1,05 | ×0,89 à ×1,00 | ×1,13 |

Trois conséquences :

* **L'audit initial avait raison sur le signe et l'ordre de grandeur des pistes
  droites, et sous-estimait les pistes à 45°**, qu'il ne mentionnait pas. Le
  mailleur ne relie que les 4 voisins : un tracé diagonal devient un escalier,
  plus long de √2, puis se rompt quand la piste est plus étroite qu'environ
  1,4 pas.
* **La réponse est en escalier.** À alignement fixe, élargir une piste de 0,20 à
  0,39 mm ne change **rien** à la résistance modélisée (1,64 mΩ/mm), puis
  0,40 mm la divise par 1,7. Une comparaison avant/après sur le DC ne voit pas un
  élargissement inférieur au pas, et amplifie celui qui franchit une rangée de
  nœuds. La réponse reste monotone : pas d'inversion de signe constatée. [mesuré]
* **Un îlot peut être fabriqué par le maillage.** Le grossissement automatique
  (`mesh.py:475-496`) remonte le pas au-delà de 400 000 nœuds ; les pistes fines
  à 45° s'ouvrent, leurs charges sont exclues, et le finding `DC-001` annonce
  « contient des charges sans source » avec la confiance DETERMINISTIC et la
  recommandation « réparer la connectivité cuivre » (`analysis_adapters.py:979-985`)
  — une réparation d'un défaut que la carte n'a pas. [déduit de la mesure]

Les plans sont moins exposés : un pas de 0,1 mm sur une zone de plusieurs
millimètres reste dans quelques pourcents (ratio → 1 quand la largeur croît).

**Limitations publiées** (`analysis_adapters.py:1026-1029`) : contacts de
connecteur et densité de courant. Ni l'erreur de discrétisation, ni l'absence de
retour, ni la température de référence. **Calibration : sur-déclarée.**

### 2.2 DC — conseiller

Maillage hybride : résistance exacte des segments (`ingest/track_resistance.py`),
cut-cell sur les zones, validé contre la plaque analytique
(`tests/test_dc_validation.py:477`), re-simulation des propositions (A3,
`plan-ameliorations.md`). F3, calibration juste. Défaut de **cohérence** et non
de fidélité : sa baseline n'est pas la chute que le rapport publie (§3).

### 2.3 AC / PDN

Même maillage rasterisé, au pas `mesh_resolution_mm = 0,5 mm` par défaut
(`models.py:185`) : au vu de la mesure du §2.1, une piste de moins de 0,5 mm à 45°
n'y conduit pas, et un condensateur relié par une telle piste peut se retrouver
hors réseau [déduit ; l'exclusion de port est rapportée, sa cause non].
Condensateurs RLC à **ESR constant 0,01 Ω** (`ac_model.py:468`), ESL par boîtier,
**sans inductance de montage ni dérating DC** [vérifié : absents du code].
Cible dérivée du rail avec provenance, limite quasi-statique signalée : bon.
`AC-001` passe DETERMINISTIC dès que la cible est explicite et qu'aucun modèle
n'est marqué « estimé » (`analysis_adapters.py:283-287`) — or l'omission de
l'inductance de montage biaise toujours dans le même sens (Z sous-estimée en HF).
**Calibration : sur-déclarée dans ce cas.**

### 2.4 Impédance différentielle et diaphonie

Solveur 2D exact en milieu homogène et invariant d'échelle [cité,
`audit-si-pdn.md` §1]. Aucune référence externe. Toujours ouverts : la marge
latérale ne croît pas avec l'écartement (`differential_geometry.py:119,261`,
bug latent protégé par l'exclusion des breakouts), pas de signal de
non-convergence du SOR (`field_solver_2d.py:81`), pas de fraction de longueur
qualifiée publiée. Le statut « ESTIMATE » mélange « une section n'a pas été
qualifiée » et « le nombre est peu fiable ». XT-001 est désormais chiffré par
`crosstalk_2d` quand la géométrie le permet, et dit explicitement quand il ne
l'est pas (`emc_analyzer.py`, XT-001) : bon.

### 2.5 EMC

Construction solide (confiance pondérée, plafond par règle), entrées faibles.
Toujours : horloges à 25 MHz, découpage à 500 kHz faute de modèle de pertes,
paires à 240/125 MHz ; tension et courant **3,3 V / 0,1 A** par défaut pour les
horloges et pour les convertisseurs sans modèle de pertes
(`emc_analyzer.py:393-413`, `models.py` `EMCSignalSource`) — seuls les
convertisseurs du power tree reprennent tension et courant des rails, marqués
MEDIUM ; champ E proportionnel à une capacité linéique supposée de
100 pF/m (`em_field_solver.py:39`) ; phase 10 hors findings ; Palace muet (A5).
Nouveau par rapport à l'audit EMC : **la normalisation `HIGH → DETERMINISTIC`**
(`analysis_contract.py:59`) donne le rang de fait à des règles dont
l'observation est géométrique mais dont le verdict repose sur un seuil codé en
dur (BE-001, CK-001, RP-001…).

### 2.6 Thermique et électrothermique

Progrès réels depuis le 4 septembre : corrélations de plaque au lieu de 5 W/m²K
(`surface_convection.py:98`), rayonnement linéarisé à la température de surface
(`surface_convection.py:128`), bilan énergétique renommé `NUMERICS_OK`. Restent
de premier ordre : **échantillonnage binaire du cuivre** (`thermal_mesh.py:432-436`),
θJB par défaut 20 °C/W (`models.py:821`) qui pilote TH-001 **CRITICAL**, via en
branche unique (`thermal_mesh.py:771`). L'écart de 7,4 °C entre 0,5 et 0,1 mm
[cité] a été mesuré à un pas **plus fin que le préréglage par défaut « Normal
(1,0 mm) »** (`ui/thermal_analysis_panel.py:160-164`) : l'erreur au défaut n'est
pas connue, et rien ne l'indique dans le rapport.

### 2.7 CFD

Écoulement de Poiseuille à −0,4 % (ny = 20), conservation de la masse corrigée
[cité, `validation-cfd.md`]. Transport air → paroi non résolu : 35,6 °C d'écart
solide/air sur la carte de référence (A1), `converged` inatteignable en enceinte
close (A2). CFD-004 renvoie désormais au thermique pour les températures de
composants : calibration juste.

### 2.8 Règles schéma

Injoignables (`application/schematic_controller.py` n'est importé par rien). Si
elles étaient branchées aujourd'hui, sur les démos KiCad 10 [mesuré,
`plan-ameliorations.md` §D] : SCH-005 faux sur 6 cartes (jusqu'à 159 : empreintes
sans schéma et feuilles multi-instances), SCH-002 émet exactement autant de
findings que la carte compte de marqueurs no-connect sur trois cartes
(77/77, 69/69, 63/63), SCH-001 ne reconnaît pas 17 condensateurs sur 19 (cm5).
Toutes marquées DETERMINISTIC ou presque.

---

## 3. Cohérence entre analyses

Une même grandeur physique doit avoir une seule valeur dans un rapport.

| Grandeur | Où elle vit | Écart | Effet |
|---|---|---|---|
| Résistivité du cuivre | `extractor.py:337,444` : 1,68 × 10⁻⁸ ; `ingest/track_resistance.py:10` : 1,72 × 10⁻⁸ | 2,4 % | DC, AC, pertes thermique/CFD d'un côté ; conseiller de l'autre |
| Géométrie du cuivre | **quatre** échantillonneurs : rasterisé 4-voisins (DC/AC), exact + cut-cell (conseiller), binaire au centre de cellule (thermique), grille propre (CFD) | ±25–112 % sur une piste fine selon le moteur | la même piste n'a pas la même résistance dans le finding et dans sa remédiation |
| Chute d'un rail | rasterisé (DC-003) contre hybride (remédiation) | 7,7 contre 6,558 mV sur +5V_RAIL [cité] | un correctif « promet 3,0 mV » sur un rail affiché à 7,7 mV calculé autrement |
| Température de référence | DC à 20 °C implicite ; thermique et CFD à 25 °C ambiant ; électrothermique corrige depuis 20 °C | 5 °C d'origine, ≈ 2 % sur R | faible, mais non dit |
| Rails : tension et courant | DC, AC, thermique lisent la configuration ; l'EMC ne les reprend que pour les convertisseurs du power tree, **3,3 V / 0,1 A** pour les horloges et les convertisseurs sans modèle de pertes | jusqu'à un ordre de grandeur sur le courant | pour ces sources, E et H ignorent les rails que le reste du rapport utilise |
| Retour de masse | absent en DC ; présent en AC (rail + masse) ; par nom en EMC et règles schéma | modèles contradictoires | la chute DC et l'impédance AC d'un même rail ne décrivent pas le même circuit |
| Épaisseur de carte de repli | 1,6 mm (`extractor.py:458`, `mesh_hybrid.py:306`) ; 1,53 mm de diélectrique (`thermal_mesh.py:545`) | < 3 % | négligeable, deux sources de vérité |
| Métallisation de via | 0,025 mm partout, sauf pastilles hybrides 0,020 mm (`mesh_hybrid.py:73`) | 20 % sur la résistance de barillet | pastilles traversantes seulement |
| Identifiants de règle | `DC-001`/`DC-002` : DC (`analysis_adapters.py:980,988`) **et** EMC (`emc_analyzer.py:998,1017`) | collision | lecture, filtres, CSV et identité d'action ambigus |
| Échelles de score | EMC interne : 25/12/6/2 × confiance (`emc_analyzer.py:58-59`) ; campagne : 25/10/4/1 sans confiance (`campaign.py:32-38`) | deux « EMC /100 » différents | l'onglet EMC et la synthèse de campagne ne donnent pas le même score |
| Vocabulaire de confiance | EMC HIGH/MEDIUM/LOW ; contrat à 5 niveaux ; conversion avec perte | — | DETERMINISTIC signifie tantôt « fait observé », tantôt « seuil codé en dur », tantôt « sortie de modèle » |

---

## 4. Coordination

**Ce qui est bien fait.** L'orchestrateur explicite ses dépendances et les
journalise : EMC déplacée après AC, différentiel et thermique, dont elle lit les
résultats de la même campagne (`application/campaign_controller.py:288-320,
410-458`) ; CFD avant thermique **uniquement** en flux forcé, parce qu'en
convection libre la vitesse dépend de la température (`:322-356`) ; un domaine
absent laisse l'entrée intacte au lieu de la vider.

**Ce qui ne l'est pas.**

| Flux | Constat | Conséquence |
|---|---|---|
| DC → thermique, DC → CFD | chacun **re-résout** tout le DC avec son propre moteur (`thermal_controller.py:133-147`, `cfd_controller.py:56-57`) | ~21 s × 3 par campagne [cité, B2] ; cohérent entre eux car même rasterisé, mais sans lien avec le DC publié si ses réglages diffèrent |
| DC → conseiller | re-maillage **par un autre moteur**, depuis le **fichier** et non la carte vivante | deux baselines ; une modification non sauvegardée est analysée d'un côté et pas de l'autre |
| CFD ↔ thermique | une seule grandeur échangée (vitesse libre, flux forcé seulement) ; en convection libre, deux champs de température indépendants pour la même carte | la CFD peut afficher 225 °C de solide pendant que le thermique donne autre chose [cité, A1] |
| Schéma → physique | **aucune** analyse physique ne lit `ingest` ; seules les règles (non branchées) et la géométrie du conseiller | les fréquences, courants et découplages restent devinés (EMC) ou saisis (DC, AC) |
| Cache de campagne | écrit et testé, **non branché** (`ui/main_dialog.py:128` sans `cache=`) | pas de recalcul incrémental. Latent : branché tel quel, un domaine servi par le cache ne transmettrait rien à l'EMC ni au couplage CFD (`campaign_controller.py:576-590`), et la clé hashe le PCB sur disque, pas la carte vivante |
| Agrégation | `_same_problem` = un net **ou** un composant **ou** 2 mm en commun (`campaign.py:163-173`), regroupement glouton dépendant de l'ordre (`:258-265`), rang multiplié par le nombre de domaines (`:309-313`) | démontré : AC-001 (+3V3, GND), GP-003 (GND) et DC-002 (+3V3) fusionnent en une action classée première ; `GND` agit comme un aimant |
| Scores de domaine | pénalité par sévérité, sans confiance, sans plafond par règle (`campaign.py:227-231`) | 159 faux SCH-005 MEDIUM mettraient le domaine à 0 ; un finding heuristique coûte autant qu'un fait |

---

## 5. Mesures produites pour cet audit

| Mesure | Outil | Résultat clé |
|---|---|---|
| Résistance modélisée par le rasterisé | `validation/dc_raster_fidelity.py` | §2.1 : ×0,75–×1,50 (0,15 mm, 0,1 mm, droite) ; jusqu'à ×2,12 à 45° ; ouvert à 45° dès 0,25 mm de pas |
| Réponse à un élargissement | `validation/dc_raster_fidelity.py --staircase` | paliers : 0,20 → 0,39 mm sans effet, puis saut |
| Fusion d'actions de campagne | cas synthétique ci-dessous | trois défauts sans rapport regroupés par `GND`/`+3V3` |
| Constantes partagées | recherche exhaustive hors tests | §3 |
| Invariants d'ingestion | `validation/ingest_corpus.py` | `plan-ameliorations.md` §D |

Reproduction de la fusion, depuis la racine du dépôt :

```python
from analysis_contract import AnalysisResult, AnalysisFinding, AnalysisStatus, FindingSeverity as S
from campaign import CampaignResult

def f(rule, nets=(), comps=()):
    return AnalysisFinding(rule_id=rule, category="X", severity=S.HIGH, title=rule,
                           description="", nets=list(nets), components=list(comps))

results = [
    AnalysisResult("AC", "AC", status=AnalysisStatus.WARN, findings=[f("AC-001", ["+3V3", "GND"], ["C12"])]),
    AnalysisResult("EMC", "EMC", status=AnalysisStatus.WARN, findings=[f("GP-003", ["GND"])]),
    AnalysisResult("DC", "DC", status=AnalysisStatus.WARN, findings=[f("DC-002", ["+3V3"])]),
]
for action in CampaignResult.from_results(results).actions:
    print(action.domains, action.rule_ids)
# ['AC', 'EMC', 'DC'] ['AC-001', 'GP-003', 'DC-002']
```

---

## 6. État des propositions du 2026-09-04

| Proposition | État |
|---|---|
| SI : étiquette de confiance de `_finding` | **fait** (`79875e4`) |
| SI : brancher `crosstalk_2d` sur XT-001 | **fait** (`4aee8a7`) |
| SI : fraction de longueur qualifiée | ouvert |
| SI : inductance de montage, ESR/ESL par MPN, dérating DC | ouvert |
| SI : marge du domaine selon l'écartement | ouvert (latent) |
| SI : `transmission_line_abcd` sur les stubs | ouvert (toujours non importé) |
| SI : signal de non-convergence SOR | ouvert |
| EMC A : phase 10 → findings | ouvert |
| EMC B : découplage/protections depuis le schéma | ouvert |
| EMC D : avertissement de non-comparabilité du champ | **partiel** : « amplitudes relatives seulement » pour les marqueurs harmoniques (`emc_analyzer.py:1467`), rien sur les E/H en V/m |
| EMC E : provenance des paramètres de source dans les findings | non vérifié |
| Thermique 1 : corrélation de convection | **fait** (`7aa27c3`) |
| Thermique 2 : rayonnement à la température de surface | **fait** |
| Thermique 3 : fraction de cuivre par cellule | ouvert |
| Thermique 4 : publier les hypothèses ; requalifier le bilan | **partiel** (bilan renommé, métriques de surface publiées) |
| Thermique 5 : plancher de boîtier, θJB par datasheet | ouvert |
| Thermique 6 : étude de convergence automatique | ouvert |
| Thermique 7 : vias en réseau discret | ouvert |
| Thermique 8 : sort de la CFD | **décidé** : couplée en flux forcé, validée sur conduit, limites dites |

---

## 7. Recommandations

Classées par ce qui fait publier quelque chose de faux à *tous* les
utilisateurs, puis par effort. Aucune n'est engagée : elles sont à arbitrer contre
`docs/plan-universalite.md`, dont plusieurs recoupent les phases.

1. **Un seul moteur DC pour le rapport et ses remédiations.** Soit l'analyse DC
   passe au maillage hybride (validé), soit le conseiller re-simule sur le
   rasterisé. La première option corrige aussi l'AC et les pertes thermiques ;
   c'est l'item 3 de `audit-cablage.md`, maintenant chiffré. Préalable : comparer
   les deux sur les rails de la carte de référence et du corpus. *Effort moyen,
   risque réel : toutes les chutes publiées changent.*
2. **Tant que le rasterisé reste, dire son erreur.** Publier le pas effectif et la
   plus petite largeur de piste du rail rapportée au pas ; limitation explicite
   quand une piste fait moins de 3 pas ou court à 45° ; **rétrograder DC-001 en
   ESTIMATED** et changer sa recommandation quand le pas a été grossi. *Effort
   faible.*
3. **Corriger l'agrégation de campagne.** Exclure les nets de référence et les
   rails larges de la clé de regroupement, exiger deux indices (net + lieu, ou
   composant), rendre le regroupement transitif et indépendant de l'ordre,
   pondérer les scores par la confiance et plafonner par règle comme l'EMC le fait
   déjà. *Effort faible à moyen.*
4. **Espace de noms des règles.** Préfixer par domaine (`DCP-`, `EMC-DC-`…) ou
   imposer l'unicité dans le registre, avec un test qui échoue sur une collision.
   *Effort faible.*
5. **Une seule définition des constantes physiques** (résistivité à une
   température de référence déclarée, épaisseurs de repli, métallisation), dans
   un module importé par tous les moteurs, avec le test « une seule source de
   vérité » déjà utilisé pour les défauts de configuration (B1). *Effort faible.*
6. **Recalibrer la confiance.** Séparer « observation » et « verdict » ;
   supprimer la conversion `HIGH → DETERMINISTIC` ; `AC-001` ESTIMATED tant que
   l'inductance de montage manque ; TH-001 ne peut pas être CRITICAL sur un θJB par
   défaut. *Effort faible.*
7. **Barre d'erreur thermique.** Résoudre au pas demandé et au double, publier
   l'écart. Mesurer d'abord l'erreur au préréglage « Normal (1,0 mm) ». *Effort
   faible.*
8. **Partager le DC dans la campagne** au lieu de le résoudre trois fois, et
   brancher l'EMC sur les rails configurés (tension, courant). *Effort moyen.*

---

## 8. Ce que cet audit n'a pas établi

* **L'effet du rasterisé sur un rail réel.** La mesure porte sur des pistes
  isolées ; la part de pistes fines et diagonales dans la chute d'un rail dépend
  de la carte. À mesurer en comparant rasterisé et hybride sur le corpus.
* **L'erreur thermique au pas par défaut** (1,0 mm) : jamais mesurée.
* **L'effet sur l'AC des pistes ouvertes à 0,5 mm** : déduit, non constaté.
* **La justesse absolue du solveur 2D, du champ proche et de la CFD** : aucune
  référence externe disponible dans le dépôt ni sur cette machine.
* **Le comportement sur la carte vivante** : aucune analyse n'a été lancée depuis
  KiCad ou Zeo pour cet audit ; toutes les mesures sont hors ligne.

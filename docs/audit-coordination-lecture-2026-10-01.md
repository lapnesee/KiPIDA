# Audit — coordination des analyses et lecture des résultats (2026-10-01)

Base : `c7f37f6` (branche `claude/pcb-analysis-tool-40t1dr`). Carte de
référence : P02 Rev E, campagne `campaign-20261001-005901.html`, produite par la
copie installée au commit `56dfd92` (le texte « 0.08464 ohm » et la formule
« past that 2 C as well » de TH-004 le datent). Les commits `221f69a`,
`3b0ddf5` et `c7f37f6`, postérieurs au run, changent trois points signalés plus
bas ; c'est dit à chaque fois.

Cet audit ne refait pas les audits de domaine (`audit-2026-09-23.md`) ni l'audit
de cohérence du 13/09 (`audit-coherence-analyses.md`). Il porte sur deux
questions :

1. **Coordination** : les analyses se transmettent-elles ce qu'elles savent, et
   une même grandeur physique a-t-elle une seule valeur d'un bout à l'autre ?
2. **Lecture** : un ingénieur débutant qui ouvre le rapport de campagne
   comprend-il ce qui est grave, pourquoi, avec quelle certitude, et quoi faire ?

Convention : **[mesuré]** = produit pour cet audit (scripts dans
`scratchpad/audit/`) ; **[cité]** = tiré d'un artefact du run ou du sidecar ;
**[déduit]** = raisonnement sur le code. Références `fichier:ligne`.

---

## 0. Synthèse

1. **Une même grandeur a jusqu'à quatre valeurs selon le domaine.** Le courant
   de sortie de U4 vaut 4,36 A (source CEM et modèle d'inductance, saisis),
   4,73 A (cible AC, rails nominaux), 4,746 A (DC et thermique, cuivre compris)
   et 4,49 A en moyenne, 5,55 A en crête (circuit de carte Phase 10, chargé avec
   les 4,36 A saisis). Sa fréquence est saisie trois fois. Aucun domaine ne
   signale ces écarts (CO-1, CO-2).
2. **La simulation la plus fidèle arrive en dernier.** Les circuits de carte de
   la phase 10 tournent dans l'analyse CEM, après l'AC et le thermique, qui ne
   peuvent donc pas s'en servir (CO-3).
3. **Le rapport mélange défauts de la carte et fiabilité du calcul.** TH-004
   (maillage thermique) est la 3ᵉ action, devant sept défauts de la carte, parce
   qu'il est sûr de lui (DETERMINISTIC) ; l'indicateur `calculation_quality`,
   prévu pour cela, n'est posé que par le contrôle de maillage AC (CO-6, LE-3).
4. **Pour un débutant, le rapport ne dit ni pourquoi ni comment.** 13 actions
   sur 13 portent « Effort: MEDIUM » et « No structured remediation was
   computed » ; la rubrique « Consequences » répète le titre ; aucune image ;
   rapport en anglais seulement ; 853 des 1 920 lignes sont des métriques DC
   (LE-1 à LE-6).
5. **Deux chiffres contradictoires sont affichés sans explication à côté** : la
   CFD, verdict PASS, affiche 170,9 °C de solide maximal quand le thermique donne
   119,0 °C ; l'explication (« not usable at this mesh size ») n'est qu'en annexe
   (LE-7).

Ce qui marche et doit rester : l'ordre des domaines expliqué dans le journal,
le DC résolu une seule fois et partagé, le boîtier remis au thermique avant le
calcul, la CEM qui lit l'AC, le différentiel et le thermique de la même
campagne, les seuils heuristiques nommés comme tels dans chaque constat, les
limitations explicites (§4).

---

## 1. Carte de la coordination actuelle

[déduit, `application/campaign_controller.py:311-410, 444-527, 625-675`]

```
                 sidecar (rails, profils, sources CEM, modèles d'inductance)
                    │            │              │                │
 SCHEMATIC          DC ─────────┬──► THERMAL ◄── boîtier ── CFD (après THERMAL
 (seul)             │  pertes   │    (couplé)                  en convection
                    │  cuivre   └──► CFD                       naturelle)
                    │                  │
                 AC (rails nominaux)   │
                    │                  │
 DIFFERENTIAL ──────┼──────────────────┼──► EMC ──► phase 10 : audit SPICE,
                    └──────────────────┘          circuits de carte ─► spectre
```

| Lien | Ce qui passe | État |
|---|---|---|
| DC → THERMAL, CFD | résultat DC (pertes cuivre, départ du couplage) | partagé, journalisé |
| CFD → THERMAL | géométrie du boîtier ; vitesse d'air en convection forcée seulement | partagé |
| AC, DIFF, THERMAL → EMC | balayages, paires, champ thermique | partagé |
| Phase 10 → EMC | fréquence, tension, courant commuté | partagé, interne à l'EMC |
| Phase 10 → AC, THERMAL | — | **absent** (CO-3) |
| THERMAL → AC (température des condensateurs) | — | **absent** (CO-4) |
| CFD → qui que ce soit (convection naturelle) | — | **absent** : la CFD ne nourrit rien (CO-5) |
| DC → sources CEM (courant) | — | **absent** : courant saisi à la main (CO-1) |

---

## 2. Coordination

| Id | Gravité | Constat | Preuve |
|---|---|---|---|
| CO-1 | majeur, cohérence | Le courant d'un convertisseur est saisi ou recalculé séparément par domaine, sans contrôle d'écart. U4 : 4,36 A (source CEM et modèle L1, saisis), 4,73 A (AC, rails nominaux), 4,746 A (DC/thermique), 4,49 A moyen et 5,55 A crête (Phase 10). | [cité] sidecar `emc_profile.sources`, `inductor_models` ; journal 00:49 ; `report.txt` thermique ; journal Phase 10 |
| CO-2 | majeur, cohérence | Fréquence et ondulation saisies plusieurs fois : U4 400 kHz dans le modèle de pertes (calculée depuis R50), la source CEM et le modèle L1 ; ondulation L1 2,016 A (calcul) contre 2,13 A simulés. Les modèles L2/L3 du sidecar portent 0 A / 0 Hz, remplacés à l'exécution par l'arbre d'alimentation (0,536 et 1,637 A) : la valeur affichée dans le panneau n'est pas celle utilisée. | [cité] sidecar ; `report.txt` EMC (ripple 0.53624, 1.6374) |
| CO-3 | majeur, coordination | Les circuits de carte (Phase 10) tournent dans l'EMC, après l'AC et le thermique : la cible AC prend le pas de U4 dans l'arbre (4,73 A) et la fréquence saisie, le thermique ses pertes d'un rendement de repli, alors qu'une simulation de U4 dans son vrai circuit existe. Le circuit de carte lui-même est chargé avec `current_a` de la source CEM (4,36 A), pas avec le courant DC (4,746 A, 8 % de plus). | [déduit] `emc_controller.py` (`simulate_circuits` avant l'analyseur CEM seulement) ; `emc_phase10.py`, `board_converter_bench` (`load_a = source.current_a`) |
| CO-4 | moyen, coordination | La température n'atteint pas l'AC : capacités X5R/X7R prises à 25 °C alors que la campagne connaît la température locale (le thermique tourne après l'AC). TE-001 reste générique : « The latest thermal hotspot is 119.0 C. Verify MLCC DC-bias… », sans nommer de condensateur. | [cité] constat TE-001 ; [déduit] ordre `campaign_controller.py:342-374` |
| CO-5 | moyen, cohérence | En convection naturelle la CFD ne nourrit aucun domaine. Elle affiche une air intérieure de 60,65 °C pendant que le thermique résout à 61,2 °C (bilan Q/(h·A)) — accord de 0,55 K, utile, mais jamais présenté comme un contrôle croisé — et un solide à 170,9 °C qui contredit les 119,0 °C du thermique. | [cité] métriques CFD et THERMAL du rapport |
| CO-6 | majeur, interprétation | `calculation_quality` (« diagnostic du calcul, pas défaut de la carte ») n'est posé que par le contrôle de maillage AC ; TH-004 ne le porte pas : il retire 6 des 13,8 points du score thermique (86/100) et devient l'action n°3, devant sept défauts de la carte (gain 10,0 contre 6,5). | [mesuré] `analysis_adapters.py:332` seul `calculation_quality = True` ; `campaign.py:264-280` ; actions du rapport |
| CO-7 | moyen, interprétation | Le regroupement en actions suit composants et nets ; il ne relie pas les constats qui ont une même cause : Q10/Q11 éloignés de 21,3 mm (SW-003), surface du nœud SW1 (SW-002) et harmoniques (SW-001) sont deux actions distinctes. Un débutant ne voit pas que corriger le placement de Q10/Q11 traite les trois. | [cité] actions 2 et 8 du rapport ; [déduit] `campaign.py:283-307` (regroupement par composant, ou par net *et* même position) |
| CO-8 | moyen, données | Les lacunes de données qui pèsent sur plusieurs domaines sont enfouies dans les limitations : le rendement de U4 est un repli « confidence=low » (93 %, 1,81 W, la plus grosse source de chaleur, « replace with SLVSFC5 Figure 6-1 »). Il conditionne les températures de Q10 et L1 mais n'apparaît dans aucune action. | [cité] limitations THERMAL |
| CO-9 | mineur | Le contexte projet « host unknown … host detection inconclusive » est affiché dans la synthèse alors que le processus parent est Zeo ; c'est un état de conception en attente (plan-universalite, phase 0.1), pas une information pour le lecteur. | [cité] « Scope analysed » |

### Recommandations — coordination

* **R-CO-1 (CO-1, CO-2, CO-3) — une fiche par convertisseur, source unique.**
  Courant de sortie, fréquence, ondulation, tension commutée, rendement : chaque
  grandeur a une valeur courante et sa provenance (saisie, arbre/DC, datasheet,
  simulation). Les domaines lisent la fiche au lieu de leurs copies ; le sidecar
  ne garde que les surcharges volontaires. Un contrôle signale tout écart de plus
  de 10 % entre une saisie et le calcul (ex. « source CEM U4 : 4,36 A saisis,
  4,746 A calculés en DC »).
* **R-CO-2 (CO-3) — une étape « circuits » juste après le DC.** Audit SPICE et
  circuits de carte avant l'AC et le thermique, chargés avec le courant DC ; leurs
  sorties alimentent la fiche ci-dessus (l'EMC les relit déjà ; l'AC et le
  thermique pourraient prendre fréquence et ondulation simulées).
* **R-CO-3 (CO-6) — poser `calculation_quality` sur TH-004** (et sur tout constat
  de maillage ou de solveur), et sortir ces constats des actions vers une rubrique
  « Fiabilité du calcul ».
* **R-CO-4 (CO-5) — montrer les contrôles croisés.** Une ligne « air intérieure :
  thermique 61,2 °C, CFD 60,65 °C, écart 0,55 K » rassure plus que deux chiffres
  séparés ; les maxima CFD non utilisables ne vont pas dans le tableau principal.
* **R-CO-5 (CO-4) — température → composants passifs.** À court terme, TE-001
  nomme les condensateurs et inductances à plus de 85 °C ; à terme, l'AC prend la
  température locale des condensateurs (second passage après le thermique).
* **R-CO-6 (CO-7) — regrouper par cause.** Une règle de regroupement « même étage
  de puissance » (SW-001/002/003 d'un même nœud de commutation) en une action.
* **R-CO-7 (CO-8) — les données à compléter deviennent des actions** (« Saisir la
  courbe de rendement du TPS552882 : 1,81 W de U4 reposent sur 93 % estimés »),
  classées par la chaleur ou le courant qu'elles conditionnent.

---

## 3. Lecture et interprétation (ingénieur débutant)

Mesures sur le texte du rapport (`campaign.txt`, 1 920 lignes, 69 476 caractères).

| Id | Gravité | Constat | Preuve |
|---|---|---|---|
| LE-1 | majeur | Rapport et constats entièrement en anglais ; la traduction (`i18n`) ne sert qu'aux panneaux de l'interface (7 fichiers `ui/`), aucun fichier de `report/`, ni `campaign.py`, ni `analysis_adapters.py`. | [mesuré] recherche des appels de traduction |
| LE-2 | majeur | Les actions ne disent ni **pourquoi** ni **comment vérifier** : 13/13 « No structured remediation was computed for this action » (`report/html_report.py:404-409`), la rubrique « Consequences » répète le titre (« DC: GND pads are cut off from the rest of the net »), 13/13 « Effort: MEDIUM » (valeur par défaut faute de remédiation, `campaign.py:548-551`). | [mesuré] |
| LE-3 | majeur | Le « Gain rank » n'est pas un gain : gravité × poids de confiance × facteur multi-domaine ÷ effort (`campaign.py:578-583`), sans explication ; il place un diagnostic de maillage (TH-004, 10,0) devant des défauts réels (6,5). | [mesuré] |
| LE-4 | majeur | Surcharge : la section DC fait 853 lignes (44 % du rapport), 169 lignes de métriques de rail ; 205 lignes montrent une clé interne (`rail_1_drop`, `hotspot_mesh_sensitivity`…) ; empreinte de carte, identifiants de run et de campagne en clair. | [mesuré] |
| LE-5 | majeur | Aucun visuel : 0 image dans le rapport consolidé alors que chaque domaine produit ses graphiques (carte de risque CEM, champ thermique…) et que le générateur sait les intégrer (`report/html_report.py:481-504`) — les résultats de campagne n'ont pas d'artefacts attachés. | [mesuré] |
| LE-6 | moyen | Jargon non expliqué dans les constats : « drive index 1.46 V*A », « -37.6 dB relative to its low-frequency square-wave envelope », « Richardson, first order », « thetaJB/psiJB », « P99.5 », « least margin », « DETERMINISTIC/ESTIMATED/HEURISTIC » ; règle de verdict énoncée en termes de confiance (« a HIGH one backed by a DETERMINISTIC, MEASURED or DATASHEET_BACKED confidence »). Pas de glossaire ni de « comment lire ce rapport ». | [mesuré] |
| LE-7 | majeur | Chiffres contradictoires sans explication sur place : CFD PASS avec « Maximum solid temperature 170.9 °C NOT_USABLE » ; l'explication est dans l'annexe Limitations. Score CEM « 65/100 » mis en avant alors que sa limitation dit qu'il n'est pas une marge. | [cité] |
| LE-8 | moyen | Notation incohérente : 34 températures écrites « 118.99 C » contre 6 « °C » ; 16 « ohm » et 0 « Ω » ; « mm2 » et « mm² » ; pourcentages à 4 chiffres (« 0.003015 % ») ; libellé tronqué « MEDI 3 ». | [mesuré] |
| LE-9 | moyen | Les confirmations (PD-002 « PDN remains within target », bientôt TH-007 fusibles) sont des constats INFO perdus parmi 9 INFO ; rien ne dit au débutant **ce qui est vérifié et tient**. | [cité] |
| LE-10 | moyen | Le verdict WARN ne répond pas à la question du débutant : « puis-je lancer la fabrication ? ». Rien ne sépare « à corriger avant fabrication » (C88/R10 isolés de GND, un vrai défaut de connexion), « à vérifier sur prototype » (seuils heuristiques CEM) et « données à compléter ». | [déduit] |
| LE-11 | mineur | Chaque constat porte sa confiance en anglais majuscule sans légende ; la gravité HIGH d'un seuil heuristique (SW-003, 20 mm) et celle d'un défaut de connexion (DC-008) ont la même allure. | [cité] |

### Recommandations — lecture

* **R-LE-1 — une page « Décision » en tête** : trois listes (à corriger avant
  fabrication / à vérifier sur prototype / données à compléter), chacune avec une
  phrase par élément, puis « Ce qui est vérifié » (rails dans la cible, paires
  conformes, fusibles dans leur déclassement…).
* **R-LE-2 — une fiche par action** : *Pourquoi c'est un risque* (une phrase de
  physique : « une boucle chaude étendue rayonne en proportion de sa surface »),
  *Où* (composants + vignette de la carte à l'emplacement de la preuve), *Que
  faire*, *Comment le vérifier* (relancer, mesurer à la sonde), *Certitude*
  (« fait mesuré », « estimation », « règle de l'art de l'outil »). Supprimer
  « No structured remediation… » et la fausse rubrique « Consequences » ; masquer
  l'effort tant qu'il n'est pas estimé.
* **R-LE-3 — rapport en français** via l'`i18n` existante : titres, constats et
  recommandations (les gabarits de constats sont déjà centralisés dans les
  adaptateurs).
* **R-LE-4 — trois rubriques distinctes** : défauts de la carte, fiabilité du
  calcul (TH-004, contrôles de maillage, PH-00x), données à compléter.
* **R-LE-5 — alléger** : par domaine, 3 à 5 chiffres clés (pire chute, marge AC la
  plus faible, Tj la plus chaude, air intérieure) ; tableaux complets repliés en
  annexe ; clés internes, empreintes et identifiants en annexe seulement.
* **R-LE-6 — glossaire et « comment lire ce rapport »** : gravités, confiances,
  verdict, et une quinzaine de termes (boucle chaude, PDN, ψJB/θJB, déclassement,
  harmonique, maillage…), chacun lié depuis le texte.
* **R-LE-7 — unités et précision homogènes** : °C, Ω, mm², 3 chiffres
  significatifs sauf exigence contraire.
* **R-LE-8 — vue par composant** (prolonge R-CO-1) : pour Q10, U4, U14… une fiche
  reliant courant (DC/simulation), pertes, température, rôle CEM et constats ;
  c'est la lecture naturelle d'un débutant (« qu'est-ce qui ne va pas avec U4 ? »).

---

## 4. Ce qui fonctionne (à préserver)

* **Ordre et transmissions expliqués** : chaque réordonnancement est justifié
  dans le journal (« EMC runs after AC, DIFFERENTIAL, THERMAL: it reads their
  results rather than recomputing them »).
* **DC résolu une fois** et partagé avec le thermique et la CFD ; le cas où une
  capture diffère est détecté et dit (`campaign_controller.py:601-665`).
* **Boîtier remis au thermique avant le calcul** : l'air intérieure du thermique
  (61,2 °C) et celle de la CFD (60,65 °C) concordent à 0,55 K.
* **Même empreinte de carte** pour les sept domaines (`0c67d17d16…`) : tous
  décrivent la même carte au même instant.
* **Honnêteté des seuils** : « this tool's heuristic, not a standard value »
  accompagne chaque seuil heuristique ; les limitations sont explicites.
* **Circuits de carte → CEM** : fréquence, tension et courant commutés simulés
  remplacent les valeurs saisies dans le spectre, avec les valeurs saisies
  rappelées.

---

## 5. Plan proposé

Par ordre de rapport bénéfice/effort ; effort relatif S/M/L.

| Ordre | Recommandations | Effort | Effet attendu sur P02 |
|---|---|---|---|
| 1 | R-CO-3, R-LE-4 (`calculation_quality` sur TH-004 ; rubrique « Fiabilité du calcul ») | S | TH-004 sort des actions et du score thermique |
| 2 | R-LE-2 (fiche par action, sans « Consequences » ni effort fictif) | M | 13 actions lisibles par un débutant |
| 3 | R-LE-1, R-LE-7 (page « Décision », unités) | M | réponse explicite : C88/R10 à corriger avant fabrication |
| 4 | R-LE-5, R-LE-6 (allègement, glossaire) | M | rapport ramené de ~1 900 lignes à quelques pages en tête |
| 5 | R-CO-1, R-CO-7 (fiche convertisseur, données à compléter) | L | écarts 4,36 / 4,746 A signalés ; rendement de U4 en action |
| 6 | R-CO-2 (étape « circuits » après le DC) | L | AC et thermique sur les valeurs simulées |
| 7 | R-LE-3 (rapport en français) | L | lecture native |
| 8 | R-LE-8, R-CO-4, R-CO-5, R-CO-6 | M à L | vue par composant ; passifs en température ; actions par cause |

### Points déjà traités après le run audité

* `221f69a` : TH-004 passe en LOW quand aucune marge ne dépend du maillage
  (Q10 : 4,02 K de déplacement, 55,2 K de marge) — l'action n°3 descendra, mais
  reste une action tant que R-CO-3 n'est pas fait.
* `3b0ddf5` : « converters » remplacé par « regulators and pass elements » dans
  la provenance AC.
* `c7f37f6` : F1 et F2 jugés sur la courbe de déclassement Bourns (4,53 et
  4,55 A gardés, 2,17 et 3 A portés) : TH-006 devient un TH-007 INFO, premier
  élément de « Ce qui est vérifié » (R-LE-1).

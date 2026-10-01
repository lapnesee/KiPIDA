# Plan d'exécution — audit coordination et lecture du 2026-10-01

Source : `docs/audit-coordination-lecture-2026-10-01.md` (recommandations R-CO-1
à R-CO-7, R-LE-1 à R-LE-8). Règles de travail inchangées : un correctif = un
commit, entrée CHANGELOG, tests ciblés plus un test de non-régression sur un
fait réel de P02 Rev E, suite complète « OK (skipped=3) » avant chaque commit,
aucun push.

## Étapes

| # | Étape | Recommandations | Contenu | Effet attendu sur P02 Rev E |
|---|---|---|---|---|
| 1 | Guide des règles et nature des constats | R-CO-3, R-LE-4, R-CO-7 (tri), R-LE-9 | Module `rule_guide.py` : pour chacune des 72 règles, un nom court, *pourquoi c'est un risque*, *comment le vérifier*, un effort typique et une nature (défaut de la carte, fiabilité du calcul, données à compléter, confirmation, note). La campagne n'en tire des actions que pour les défauts ; les diagnostics de calcul sortent du score (ce que `calculation_quality` prévoyait pour DC-004/005 et CFD-003/004 sans le poser) ; l'effort typique remplace le MEDIUM par défaut. | TH-004 quitte les actions et le score thermique ; les efforts différencient les actions |
| 2 | Rapport : page « Décision », fiches d'action, lecture | R-LE-1, R-LE-2, R-LE-5, R-LE-6, R-LE-7, R-LE-11, CO-9 | Décision en tête (à corriger avant fabrication / à revoir / à vérifier sur prototype / données à compléter / fiabilité du calcul / ce qui est vérifié) ; « comment lire ce rapport » et glossaire ; fiche par action (pourquoi, où, que faire, comment vérifier, certitude en clair, détail technique) sans « Consequences » répétées ni « No structured remediation » ; « priorité » expliquée ; chiffres clés par domaine, tableaux complets repliés ; clés internes, empreintes et contexte d'hôte en annexe ; unités homogènes (°C, Ω, mm²) ; libellés de gravité entiers. | rapport lisible en tête ; C88/R10 en « à corriger avant fabrication » |
| 3 | Images dans le rapport | R-LE-5 | Les graphiques sauvegardés dans l'historique (rendus en arrière-plan, donc absents du résultat en mémoire) sont rattachés par `run_id` avant l'écriture du rapport. | carte de risque CEM, champs thermiques… dans le rapport |
| 4 | Rapport en français | R-LE-3 | Le cadre du rapport et le guide passent par `i18n` (extraction étendue à `report/`, catalogue `fr` complété et compilé). Les descriptions générées des constats restent en anglais (plusieurs centaines de gabarits ; voir « Non retenu »). | rapport en français dans l'interface française |
| 5 | Températures sous chaque empreinte, passifs hors spécification | R-CO-5 (CO-4) | Le thermique donne la température de carte sous toutes les empreintes, pas seulement les sources de chaleur ; TE-001 nomme les condensateurs et inductances au-delà de 85 °C, et TE-002 signale un condensateur au-delà de la température de son diélectrique (X5R 85 °C, X6S 105 °C, X7R 125 °C…). | condensateurs X5R près de U4/U14 vérifiés |
| 6 | Cohérence des convertisseurs | R-CO-1, R-CO-2 (charge) | L'arbre d'alimentation donne, par convertisseur, courant de sortie, fréquence et ondulation ; un constat « données » signale une source CEM ou un modèle d'inductance saisi qui s'en écarte de plus de 10 % ; les circuits de carte de la phase 10 sont chargés avec le courant de l'arbre, pas avec la saisie CEM. | « U4 : 4,36 A saisis, 4,73 A dans l'arbre » ; circuit U4 chargé à 4,73 A |
| 7 | Données à compléter : rendements de repli | R-CO-7 (CO-8) | Un étage fermé par un rendement de repli de confiance faible devient un constat « données à compléter », pondéré par la chaleur qu'il porte. | U4 : 1,81 W sur un rendement estimé à 93 % |
| 8 | Contrôle croisé thermique / CFD | R-CO-4 (CO-5), LE-7 | L'air intérieur du thermique et de la CFD sont comparés dans la synthèse ; les maxima CFD non utilisables quittent le tableau principal avec leur explication sur place. | « air intérieur : 61,2 °C contre 60,65 °C » ; plus de 170,9 °C affiché sous un PASS |
| 9 | Regroupement par étage de commutation | R-CO-6 | Les constats de commutation (SW-001/002/003) d'un même nœud forment une action. | 3 actions sur /SYS_5V_SW1 → 1 |
| 10 | Fiches composants | R-LE-8 | Pour chaque composant cité par un constat ou parmi les plus chauds : puissance, température, marge, règles et domaines qui le citent. | U4, Q10, U14… lus d'un coup d'œil |

## Non retenu, avec la raison

* **Étape « circuits » avant l'AC (R-CO-2, partie ordre).** Les cibles AC sont
  résolues pendant la préparation de la campagne, sur le fil de l'interface,
  avant le lancement des domaines ; y lancer PSpice bloquerait l'interface
  (556 s pour U4 au premier passage). L'effet mesuré sur P02 est faible :
  397 kHz simulés contre 400 kHz saisis pour le plancher de bande. L'étape 6
  traite la partie qui compte, l'écart de charge (4,36 A contre 4,73 A).
* **Dérive en température des condensateurs dans l'AC (R-CO-5, seconde
  moitié).** L'AC tourne avant le thermique et n'a pas de modèle de capacité en
  température ; l'étape 5 signale les condensateurs hors de leur plage, ce qui
  est la conséquence décidable.
* **Traduction des descriptions générées.** Plusieurs centaines de gabarits
  composés de phrases variables ; le guide (étape 1) donne en français le nom,
  le pourquoi et la vérification de chaque règle, ce qu'un débutant lit en
  premier.

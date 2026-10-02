# Premier rapport Ki-PIDA

Ce guide suit un premier lot d'analyses, de son lancement à la lecture du
rapport. Les libellés sont ceux de l'interface en français.

## 1. Lancer le lot

1. Ouvrir le PCB dans KiCad, puis Ki-PIDA.
2. Dans **Projet → Arbre d’alimentation et DC**, vérifier les rails et leurs
   charges : le lot travaille sur ce qui y est déclaré.
3. Cliquer **Lancer le lot d’analyses...** (présent sur chaque page).
4. Le dialogue liste les analyses. Pour chacune, il indique :
   - sur quoi elle tournera (nombre de rails, profils AC, sources CEM) ;
   - une donnée restée à sa valeur par défaut ;
   - sa durée lors du dernier lancement.
5. Cocher les analyses voulues et valider.

Pendant le lot, la page **Application → Diagnostic** affiche le journal.
Une copie en anglais est conservée dans `KiPIDA-results/ki-pida.log`.

À la fin du lot, le rapport s'ouvre dans le navigateur. Si le lot finit par
le DC ou le thermique, le rapport attend d'abord leurs figures, cinq minutes
au plus.

## 2. Les fichiers produits

Tout est écrit dans `KiPIDA-results`, à côté de la carte. `<stamp>` est la
date et l'heure locales du lot.

| Fichier | Contenu |
|---|---|
| `campaign-<stamp>.html` | Le rapport. Il est autonome et s'ouvre hors ligne. À l'impression, les blocs repliés sont dépliés. |
| `campaign-<stamp>.json` | La campagne complète, à laquelle le rapport suivant se compare. |
| `actions-<stamp>.csv` | Une ligne par action, classée, avec sa liste de décision (`fix`, `review`, `prototype`, `data`). |
| `findings-<stamp>.csv` | Une ligne par observation, avec le run et le build qui l'ont produite. |
| `domains-<stamp>.csv` | Une ligne par analyse : statut, score, run, durée. Les analyses non abouties y figurent aussi. |

Le bouton **Générer le rapport consolidé** écrit les mêmes fichiers à partir
des analyses déjà lancées dans la session, sans les relancer.

## 3. Lire la décision

Le rapport commence par **1. Décision**. Le verdict suit une règle simple :

| Verdict | Quand |
|---|---|
| **FAIL** | Au moins une action est à corriger avant fabrication. |
| **WARN** | Il reste quelque chose à revoir ou à vérifier sur prototype, ou une analyse cochée n'a pas abouti. |
| **PASS** | Aucun des cas précédents. |

Les actions sont rangées en quatre listes :

- **À corriger avant fabrication** : un défaut dont les analyses sont assez
  sûres.
- **À revoir avant fabrication** : un défaut probable, qui repose sur une
  estimation.
- **À vérifier sur prototype** : une règle de routage empirique, que seule une
  mesure tranche.
- **Données à compléter** : un résultat qui repose sur une valeur par défaut
  (voir § 4).

Sous les listes, deux parties complètent la lecture :
- **Non abouties** nomme les analyses cochées qui n'ont pas abouti, et pourquoi ;
- **Ce qui tient** dit ce que les analyses confirment, et sur quoi.

Viennent ensuite les sections suivantes :

- **Depuis la campagne précédente** : ce qui est résolu, nouveau, de gravité modifiée
  ou toujours présent, sur les analyses lancées les deux fois. La section dit
  aussi si la carte ou Ki-PIDA ont changé entre les deux campagnes.
- **2. Actions** : une fiche par action. Elle dit pourquoi c'est un risque, que
  faire, comment le vérifier, et donne le degré de certitude.
- **3. Composants** : ce que chaque analyse dit d'un même composant.
- **4. Par domaine** : le détail et les figures de chaque analyse.
- **5. Annexes** : la provenance, les limites et la configuration de chaque
  analyse, ainsi que le build de Ki-PIDA qui l'a produite.

Un sommaire, sous le titre, mène à chaque section.

## 4. Compléter les données

Une action de la liste **Données à compléter** nomme la valeur dont dépend un
résultat. Les cas les plus courants :

| Observation | Où la compléter |
|---|---|
| Coefficient de paroi du boîtier laissé par défaut (TH-009) | **Thermique → CFD du boîtier**, champ *Coefficient h des parois (W/m2K)* |
| Impédance de source d'un rail laissée par défaut (AC-006) | **Intégrité de puissance → Impédance AC** : *Modèle de source*, *Résistance de source (mOhm)*, *Inductance de source (nH)* |
| Perte d'un convertisseur fondée sur un rendement estimé (TH-008) | Dialogue du régulateur, dans **Projet → Arbre d’alimentation et DC** : champ *Courbe de rendement*, un point par ligne, `Iout rendement` (par exemple `0,5 88`) ou `Vin Vout Iout rendement` |
| Source de découpage ou inductance saisie à un autre courant que celui de l'arbre d'alimentation (SW-004) | **EMI / CEM → Pré-conformité** : **Modifier la sélection** pour une source, **Modifier le modèle sélectionné** pour une inductance |

Les champs numériques acceptent la virgule comme le point.

Relancer ensuite les analyses concernées. L'action doit alors passer dans
**Résolus** de la section **Depuis la campagne précédente**.

## 5. Faire de la place

Le dossier `KiPIDA-results` reçoit un `.gitignore` dès sa création ; un
`git add -A` dans le projet ne l'emporte donc pas.

Il grossit vite. Sur P02 Rev E, il a atteint 685 Mo en onze jours. Pour le
réduire :

1. Ouvrir **Résultats → Résultats d’analyse**.
2. Cliquer **Nettoyer…**. Le dialogue indique ce qui sera supprimé et la
   place libérée.
3. Valider.

Le nettoyage conserve :
- les 5 derniers runs de chaque analyse ;
- les 5 dernières campagnes ;
- les runs que ces campagnes citent.

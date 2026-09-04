# Audit — ce qui tourne réellement quand l'utilisateur lance une analyse

Ce document répond à une question simple et gênante : **parmi tout ce qui a été
construit, qu'est-ce que l'utilisateur obtient effectivement ?**

Méthode : recherche des importations depuis le code de production (hors tests,
hors le paquet lui-même) pour chaque module ajouté. Un module que personne
n'importe ne s'exécute jamais, quels que soient ses tests.

---

## Constat central

**Les trois livrables les plus visibles du plan de refonte ne sont pas
atteignables depuis l'interface.**

| Module | Importé par du code de production ? | Conséquence |
|---|---|---|
| `mesh_hybrid.HybridMesher` | Uniquement par `advisor/dc_advisor.py` | Le maillage hybride ne sert jamais à l'analyse DC |
| `advisor/` (conseiller DC, what-if) | **Personne** | Aucun finding ne porte de remédiation |
| `rules/` (registre + règles schéma) | **Personne** | Aucune règle issue du schéma ne s'exécute |
| `application/campaign_controller` | **Personne** | L'orchestrateur « tout lancer » est inaccessible |
| `ingest/` | Seulement via `mesh_hybrid` et `rules/`, eux-mêmes non atteints | La lecture du schéma ne sert à rien en pratique |

Vérifications :

- `application/dc_controller.py:14` → `from mesh import Mesher`
- `application/dc_controller.py:322` → `def __init__(self, mesher_factory=Mesher, ...)`
- `segment_resistance` n'est appelé que depuis `mesh_hybrid.py:172`, lui-même
  appelé seulement depuis `advisor/dc_advisor.py:150`
- Aucune occurrence de `CampaignController` / `CampaignEngine` hors de son
  propre module et de ses tests

### Ce que cela signifie pour les résultats déjà obtenus

L'analyse DC exécutée sur la carte de test a utilisé le **mailleur rasterisé
historique** (`mesh.py`), pas le mailleur hybride. Donc :

- les résistances de piste restent celles d'un escalier de grille, pas les
  `R = ρL/(w·t)` analytiques exactes ;
- le correctif du `safety_buffer` (mis à 0 dans `extractor.py:782`) **est** actif,
  puisqu'il porte sur le chemin rasterisé — c'est le seul gain de la phase 1
  qui atteint réellement l'utilisateur ;
- la densité de courant de 175 A/mm² observée sur `+5V_RAIL`, avec un rapport
  de 7 entre le maximum et le P99,5, vient de ce chemin-là. Le maillage hybride
  aurait pu la corriger ; il n'a pas été consulté.

Le rapport consolidé affichait « No structured remediation was computed for
this action » sur **chaque** action. Ce n'est pas un défaut du rapport : c'est
la conséquence directe du fait que rien ne peuple `finding.remediations`.

---

## Pourquoi c'est arrivé

Les phases ont été construites de bas en haut — ingestion, moteur, règles,
agrégation, restitution — et chacune a été validée par ses propres tests. Un
test unitaire vert prouve qu'un module fonctionne **s'il est appelé**. Aucun
test ne vérifiait qu'il **est** appelé.

C'est un angle mort méthodologique, pas une négligence ponctuelle : la même
erreur s'est produite quatre fois. Le seul livrable qui ait échappé à ce sort
est le bouton « Build Consolidated Report », précisément parce qu'un test
vérifie le câblage lui-même (`tests/test_campaign_button.py`).

---

## Ce qu'il faut faire, par ordre de rendement

### 1. Brancher le conseiller sur les adaptateurs — effort faible, gain immédiat

`analysis_adapters.adapt_dc_result` construit les findings DC. C'est là que
`advisor.dc_advisor.build_dc_remediations` doit être appelé pour peupler
`finding.remediations`. Le rapport sait déjà les rendre (il distingue même
`verified=True` de l'estimation). Le contrat, le calcul et le rendu existent ;
il manque l'appel.

Attention : `build_dc_remediations` a besoin d'un `ParsedBoard`, que le chemin
live ne produit pas — il travaille sur un snapshot kipy. Deux options :
soit le conseiller accepte le snapshot DC existant, soit l'ingestion hors ligne
est branchée en parallèle. La première est plus courte.

### 2. Brancher le registre de règles — effort faible, gain élevé

`rules/schematic_rules.py` implémente cinq règles déterministes (découplage par
broche d'alimentation, broches non connectées, dérating absent, homonymie de
nets inter-cartes, cohérence BOM/PCB). Elles ne nécessitent **aucun solveur** —
seulement `ingest/`. C'est le meilleur rapport gain/effort du lot, et c'est
exactement ce que l'audit initial recommandait de démarrer dès la phase 0.

### 3. Substituer le mailleur hybride au mailleur rasterisé — effort moyen, risque réel

`DCSolverEngine.__init__` accepte déjà un `mesher_factory`, donc l'injection est
prévue. Mais `HybridMesher` consomme un `ParsedBoard` (fichier) là où le pipeline
fournit un snapshot live (kipy). Il faut soit un adaptateur snapshot → ParsedBoard,
soit doter `HybridMesher` d'une seconde entrée.

À faire **après** avoir mesuré : les deux mailleurs doivent être comparés sur la
même carte avant substitution. L'écart attendu est important (l'audit annonçait
15–50 % d'erreur de résistance sur le chemin rasterisé) ; il faut le constater,
pas le postuler.

### 4. Rendre l'orchestrateur atteignable — effort faible

`CampaignController` séquence tous les domaines et alimente le rapport. Le
bouton actuel consolide les résultats déjà publiés, ce qui est utile mais
suppose que l'utilisateur ait lancé chaque analyse à la main.

---

## Garde-fou à ajouter

Un test d'accessibilité, dans l'esprit de `tests/test_campaign_button.py` :
pour chaque paquet livré (`ingest`, `rules`, `advisor`, `mesh_hybrid`,
`application/campaign_controller`), vérifier qu'au moins un module de
production l'importe. Ce test aurait détecté les quatre cas ci-dessus au
moment de leur introduction, et il coûte quelques lignes.

C'est le complément manquant d'une suite qui compte aujourd'hui 696 tests
verts tout en laissant l'essentiel du travail hors d'atteinte.

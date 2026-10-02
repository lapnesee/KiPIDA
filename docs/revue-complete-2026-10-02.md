# Revue complète — code, ergonomie, analyses et parcours (2026-10-02)

Base : `d9aa24d` (build 2fb7b88c), P02 Rev E, campagne du 2026-10-01 22:29
(`campaign-20261001-223902.html`).

## Périmètre et méthode

La revue suit cinq axes : le code ; l'ergonomie et les parcours ; les analyses
DC, AC et différentielles ; les analyses thermique, CFD et CEM ; la campagne et
le rapport.

Deux revues déléguées en lecture seule ont abouti : ergonomie et parcours, puis
campagne et rapport. Les trois autres ont été interrompues deux fois par la
limite d'usage de l'API. Je les ai traitées moi-même, de façon ciblée : les
hypothèses, la couverture et les points de code ci-dessous.

Le réexamen de la physique des solveurs n'a pas été refait. Cela concerne les
formules DC et AC, les corrélations thermiques, la discrétisation CFD et les
modèles d'émission. Les audits par domaine du 2026-09-23 restent la référence
sur ce plan.

Marquage des constats :
- **[V]** : vérifié par moi dans le code ou sur les fichiers de P02 ;
- **[A]** : établi par une revue déléguée à la lecture du code, non rejoué.

Les constats déjà traités par la section « Unreleased » du CHANGELOG et par
l'audit du 2026-10-01 sont écartés.

## 0. Synthèse

Les analyses sont honnêtes sur leurs seuils et sur la fiabilité du calcul. Les
principaux défauts sont ailleurs : dans ce que le rapport ne dit pas, et dans ce
que l'utilisateur ne peut pas corriger depuis l'interface.

Les dix points à traiter d'abord :

1. **Un domaine en échec, sauté ou annulé disparaît du rapport.** Le verdict
   peut être PASS sur un lot incomplet. (§1.1)
2. **Le bandeau de décision contredit les listes.** Il affiche « WARN — À revoir »
   au-dessus de « À corriger avant fabrication (2) ». (§1.2)
3. **Un défaut non signalé porte un verdict « à corriger ».** Le coefficient de
   paroi du boîtier, 5 W/m²K, est la valeur par défaut du code. Il fixe les
   +16,2 K d'air interne, dont dépendent 6 des 13 X5R de TE-002. (§1.3)
4. **Les données à compléter ne se complètent pas depuis l'interface.** TH-008
   revient quelle que soit la valeur saisie dans le dialogue. (§3.1)
5. **La configuration n'est jamais enregistrée automatiquement**, et l'écriture
   n'est pas atomique. (§3.2)
6. **Les règles de schéma ignorent les tensions de l'arbre.** Onze des 17 rails,
   dont tous les 20 V, leur échappent. (§2.1)
7. **Rien ne permet de suivre une révision.** La comparaison de campagnes n'est
   jamais appelée et la campagne n'est pas enregistrée. (§1.6)
8. **Chaque lot recalcule tout (environ 10 min)** : le cache de campagne n'est
   pas branché. (§3.6)
9. **L'historique n'a aucune borne** : 685 Mo et 330 entrées en 11 jours, dans
   le dossier du projet. (§4.1)
10. **L'interface et le rapport « français » restent en partie en anglais**,
    dont la consigne « Que faire » ; la virgule décimale est refusée. (§3.4)

## 1. Fiabilité de la décision (campagne et rapport)

### 1.1 Un domaine absent est invisible — critique [V]

`CampaignResult.from_results` (`campaign.py:730`) ne reçoit que les domaines qui
ont produit un résultat. Les issues d'un domaine (erreur, saut, annulation) ne
partent qu'au journal.

`last_outcomes` est écrit en `application/campaign_controller.py:604` et lu
nulle part. Le tableau des scores (`report/html_report.py:586`) ne liste que les
domaines présents. La revue déléguée a simulé un THERMAL qui lève une exception :
le rapport dit « PASS — No defect found by the analyses that ran » et ne
contient aucune occurrence de « THERMAL ».

Sans sidecar, le DC est sauté avec la raison « no request provided », qui n'est
lisible que dans le journal.

**Correctif :** garder les issues dans la campagne (état, raison, durée) ;
tableau « Couverture » en tête du rapport ; verdict « INCOMPLET », jamais PASS,
quand un domaine coché manque ; statut dans les CSV. Effort moyen.

### 1.2 Bandeau, listes, score et actions suivent des règles différentes — majeur [V]

Les règles divergent :
- le bandeau suit l'état des domaines (`domain_status_from_findings`,
  `campaign.py:111`) : un HIGH estimé donne WARN ;
- les listes suivent la seule gravité : un HIGH va dans « à corriger » ;
- la nature « calcul » est ignorée par l'état, mais exclue du score et des
  actions.

Conséquences :
- **Sur P02 :** « WARN — À revoir avant fabrication » s'affiche au-dessus de
  « À corriger avant fabrication (2) », dont un circuit ouvert (C88/R10).
- **En simulation [A] :** un DC non convergé (HIGH, DETERMINISTIC) donne
  FAIL « Ne pas lancer », un score de 100/100, aucune action et
  « À corriger (0) ».

**Correctif :**
- une seule fonction de décision ;
- le bandeau passe à « à corriger » dès que cette liste n'est pas vide ;
- un état « calcul non fiable » distinct de FAIL.

Effort faible à moyen.

### 1.3 Des hypothèses par défaut portent des verdicts sans être signalées — majeur [V]

**Coefficient de paroi du boîtier.** `wall_heat_transfer_w_m2k` vaut 5 W/m²K
par défaut (`models.py:1161`), et P02 l'a gardé. La hausse d'air interne vaut
Q/(h·A) (`cfd_model.py:26`) : 4,82 W / (5 × 0,0595 m²) = 16,2 K. Elle s'ajoute
à la température de toute la carte.

Pour h = 6,5 W/m²K, la hausse tombe à 12,5 K, soit −3,7 K partout. C68 (+3,9 K),
C8, C25, C50, C26 et C49 repasseraient sous 85 °C. Six des treize lignes
« à corriger » de TE-002 reposent donc sur un défaut que rien ne signale.

**Autres défauts muets [A] :**
- le boîtier par défaut fait 120 × 100 × 50 mm, sans ouverture, quand aucun n'a
  été saisi ;
- toute source CEM manuelle reçoit une confiance HIGH, même aux valeurs par
  défaut (25 MHz, 2 ns, 3,3 V, 0,1 A) ;
- une charge ajoutée vaut 1,0 A d'office.

**Correctif :** un constat « données à compléter » par hypothèse par défaut
déterminante, comme AC-006 l'a fait pour la source AC. À défaut, une colonne
« saisi / par défaut » dans le périmètre du rapport. Effort faible à moyen.

### 1.4 La CEM juge sur des résultats de session, sans le dire — majeur [A]

`_couple_results_into_emc` (`campaign_controller.py:494`) ne remplace que les
entrées produites par la campagne. Un domaine absent laisse ce que la session
avait en mémoire, ce que la docstring assume : « absent is absent ».

THERMAL décoché ou en échec, TE-001 et TE-002 jugent alors « the latest thermal
result » d'un run antérieur, éventuellement d'une autre révision, sans date ni
empreinte. C'est le même schéma pour le boîtier et la vitesse d'air du
thermique quand la CFD n'est pas du lot.

**Correctif :** horodater et empreinter les résultats de session ; vider le
champ, ou l'annoncer en provenance (« thermique : session du …, autre carte »).
Effort moyen.

### 1.5 « Ce qui tient » affirme plus que ce qui est vérifié — majeur [V]

Sur le rapport de 22:29 :
- **AC :** « 3 rails sur 3 dans leur impédance cible », alors que deux de ces
  verdicts reposent sur la source par défaut (AC-006, dans le même rapport).
- **Air intérieur :** « thermique 61.2 °C, CFD 60.65 °C, écart de 0.55 K » se
  présente comme un recoupement. C'est la même fonction `enclosure_air_rise_k`
  des deux côtés (sa docstring le dit), et l'écart ne vient que de la chaleur
  totale passée.
- **DC :** « 14 chutes de rail sur 14 » pour 17 rails ; trois rails sans charge
  (/J2_PP5V, /+3V3AON, /J2_LDO3V3) sont en NO_DATA, sans mention.

**Correctif :**
- ne pas classer « tient » un domaine qui porte un constat « données » ;
- un dénominateur égal aux rails existants, NO_DATA cités ;
- un recoupement d'air sur la moyenne du champ CFD résolu, ou le retirer.

Effort faible à moyen.

### 1.6 Pas de suivi d'une révision à l'autre — majeur [V]

- `compare_campaigns` (`report/comparison.py:55`) n'est appelé que par ses
  tests.
- `CampaignResult.to_json` n'est jamais écrit : le dossier ne reçoit que le
  HTML et deux CSV.
- SCHEMATIC n'a pas d'entrée d'historique.
- [A] Rejouée sur 21:52 → 22:38, la comparaison déclare « introduit TH-008 »
  alors que TH-008 persistait et que seul SW-004 avait disparu : l'identité d'une
  action est celle de son groupe entier.

**Correctif :** écrire `campaign-<stamp>.json` ; comparer constat par constat
(règle, réseau, composant) sur les domaines communs ; ajouter une section
« Depuis la campagne précédente ». Effort moyen à fort.

### 1.7 Deux résultats par domaine : l'archive n'est pas ce qui est agrégé — majeur [V]

Chaque domaine est adapté une fois par le moteur de campagne, puis une seconde
fois par son onglet. Les deux résultats ont le même run_id mais pas le même
contenu.

Sur 22:29 :
- THERMAL est WARN avec 9 chiffres dans l'archive (`20261001-223828-THERMAL`) ;
  il est PASS avec 10 chiffres dans le rapport.
- La CFD archivée affiche 4 chiffres et une durée de 0 s.

Le bouton « Consolider » agrège les résultats d'onglet, sans contexte de projet.

**Correctif :** l'onglet publie l'objet de la campagne ; un seul chemin
d'adaptation. Effort moyen.

### 1.8 Le score suit la version de l'outil, pas la carte — majeur [A, V partiel]

À empreinte de carte constante, le score CEM est passé de 65 à 35, puis 63, puis
65 entre le 30/09 et le 01/10 ; seul l'outil avait changé. Aucun résultat ne
porte de version d'outil. Le build n'est qu'un hachage des sources, sans
commit.

Les seuils maison sont classés ESTIMATED, avec un poids de 0,65 et le libellé
« Estimation par un modèle de la carte ». Leur texte dit pourtant « this tool's
heuristic », que le glossaire réserve à HEURISTIC (poids 0,25) : vérifié sur
SW-003.

**Correctif :**
- version et commit dans chaque résultat et dans la campagne ;
- avertissement à la comparaison quand l'outil a changé ;
- les seuils maison en HEURISTIC.

Effort faible (version) à moyen.

### 1.9 Regroupement transitif des actions — majeur, conditionnel [A]

Deux constats d'au plus six composants qui partagent une pièce sont fusionnés,
de proche en proche (`campaign.py:336`). Un SW-003 « à vérifier sur prototype »
et un DE-001 peuvent ainsi rejoindre un TE-002 « à corriger » dans une action
unique. Ce cas a été observé sur le CSV de 15:53 ; depuis le passage de TE-002
en relevé (plus de six pièces), il ne se produit plus sur P02.

**Correctif :** ne joindre que des constats de même nature ou de même nœud ;
présenter les autres comme liés. Effort moyen.

## 2. Analyses : hypothèses et couverture

### 2.1 Les règles de schéma ne connaissent la tension que par le nom du réseau — majeur [V]

`voltage_hint` vient du seul nom du réseau (`ingest/netlist_builder.py:160`).
Sur P02, 11 des 17 rails du sidecar n'en ont pas, dont /VBUS_SYS, /J1_PD_OUT et
/VBUS_PD à 20 V, et les rails J2 à 5,2 V.

Six résistances ne sont donc jamais vérifiées par SCH-003 et SCH-007, sans
qu'aucune note le dise : R62 (47k sur /VBUS_SYS), R92 et R3 (sur /J1_PD_OUT),
R44, R39 et R59. Leur dissipation est faible (R62 : 8,5 mW), mais la règle se
dit couvrante.

À l'inverse, /MAIN_3V3_FB reçoit 3,3 V par son nom, alors que c'est un nœud de
contre-réaction : l'erreur va dans le sens prudent.

**Correctif :** fournir aux règles de schéma les tensions nominales de l'arbre
d'alimentation, et ne garder le nom qu'en repli. Effort faible.

### 2.2 TE-002 : la marge n'est pas comparée à l'incertitude totale — majeur [V]

d9aa24d compare chaque dépassement au déplacement de maillage. Trois autres
incertitudes du même ordre restent ignorées :
- le coefficient de paroi par défaut (§1.3, environ ±4 K pour ±30 %) ;
- le rendement estimé de U4, qui pèse 38 % de la chaleur (TH-008) ;
- la température ambiante.

Un dépassement de 0,7 K figure dans « à corriger avant fabrication » au même
titre que les 21,8 K de C37.

**Correctif :** grader TE-002 par marge, en HIGH au-delà d'une incertitude
déclarée et MEDIUM « à revoir » en deçà, et citer les entrées qui la portent.
Effort moyen.

### 2.3 AC-006 : les préréglages ne représentent pas les rails concernés — mineur [V]

/+5V_RAIL est alimenté par un eFuse (U15), /+5V_U4 par un shunt (R49).
« Regulator output » (0,05 Ω + 1 µH) dépasse à lui seul la cible de 43 mΩ de
/+5V_RAIL, faute de boucle de régulation dans le modèle R + jωL : il produirait
un faux FAIL. Choisir un préréglage fait aussi disparaître AC-006 et remonte la
confiance, sans donnée nouvelle [A].

Le texte renvoie à « the rail's AC profile », alors que l'écran s'appelle
« AC Impedance › Source model ».

**Correctif :** un préréglage « élément série depuis un rail régulé » (R_on +
impédance amont) ; garder la mention « préréglage non vérifié » ; nommer
l'écran. Effort faible.

### 2.4 Durée : l'AC fait 58 % du lot — mineur, piste [V]

Le lot dure 575 s, dont 336 s pour l'AC (3 rails, LU + GMRES sur CPU). Le choix
du CPU est mesuré et documenté (`ac_solver.py:381`) : le GPU était plus lent sur
P02.

Les trois rails sont indépendants entre eux, et indépendants du thermique, qui
tourne sur GPU.

**Correctif :** paralléliser les profils AC, ou les lancer pendant le thermique.
Effort moyen à fort.

### 2.5 Ce qui n'a pas été réexaminé

Faute de temps machine, les points suivants n'ont pas été refaits :
- la justesse des formules (maillage hybride DC, R + L des branches AC, cavités,
  impédance différentielle) ;
- les corrélations de surface (le bas et les bords donnent tous deux 13,1 W/m²K,
  à confirmer) ;
- la CFD à 6 600 cellules ;
- les modèles d'émission et la Phase 10.

Les audits du 2026-09-23 en sont la dernière vérification ; une revue de
physique dédiée reste à faire.

## 3. Ergonomie et parcours

### Parcours actuel (reconstitué)

1. KiCad ou Zeo lance le plugin, qui ouvre la page « Arbre d'alimentation et DC ».
2. Les rails sont découverts. Un sidecar existant remplace le scan, et aucun
   bouton ne relance la découverte.
3. L'arbre se saisit à la main :
   - source et charges par rail ;
   - régulateurs par dialogue (deux rails, pastilles cochées une à une, rendement
     scalaire).
4. Cinq autres pages portent les réglages : AC, différentiel, CEM, thermique et
   CFD.
5. Les données expertes (courbes de rendement, modèles d'inductance, déclassement
   des fusibles) ne se saisissent que dans le JSON.
6. Le lot se lance depuis la seule page Résultats. Sept cases sont cochées par
   défaut, la progression tient sur une ligne d'état, et le lot dure environ
   10 min.
7. Le rapport HTML (5,5 Mo) et deux CSV s'ouvrent à la fin.
8. On corrige, on relance tout et on compare à l'œil.

### 3.1 La boucle « données à compléter » ne se ferme pas dans l'interface — majeur [V]

TH-008 demande la courbe de rendement « dans l'arbre d'alimentation ». Or le
dialogue de régulateur n'offre qu'un rendement scalaire. Dans `power_loss.py:486`,
tout rendement scalaire d'un convertisseur à découpage reçoit la provenance
« configured efficiency fallback ». TH-008 revient donc quelle que soit la
valeur saisie, alors que sa fiche promet « l'observation ne doit plus revenir »,
pour un effort « faible ».

`efficiency_table`, `inductor_models` (affichés en lecture seule) et
`current_derating` n'existent que dans le sidecar, sans documentation ni
exemple [A]. SW-004 sur un modèle d'inductance ne se corrige qu'à la main dans
le JSON ; c'est ce que nous avons fait pour L1.

**Correctif :**
- un onglet « Pertes » dans le dialogue de régulateur : points (Vin, Vout, Iout,
  η), source et référence ;
- l'édition des modèles d'inductance ;
- un champ « où » (« Arbre › régulateur › Pertes ») pour chaque règle de nature
  « données » ;
- un sidecar d'exemple commenté.

Effort moyen.

### 3.2 La configuration n'est jamais enregistrée seule — majeur [V]

`save_config` n'a qu'un appelant, le bouton « Save Config » de la page Arbre
(`ui/power_tree_panel.py:669`). Ce bouton enregistre pourtant aussi AC,
thermique, CFD et CEM. `on_close` (`ui/main_dialog.py:2328`) n'enregistre rien :
fermer la fenêtre perd toute saisie non enregistrée.

L'écriture est un `open(filepath, 'w')` direct (`config_manager.py:84`), sans
fichier temporaire ni sauvegarde. Une coupure pendant l'écriture laisse un
sidecar tronqué, que le lancement suivant écarte et que le « Save » suivant
écrase.

**Correctif :**
- enregistrer à la fermeture et au lancement d'un lot ;
- écrire dans un fichier temporaire, puis `os.replace`, avec un `.bak` horodaté ;
- placer le bouton dans la barre d'actions commune.

Effort faible.

### 3.3 Le lot est introuvable, l'annulation cachée, et rien ne précède le lot — majeur [V, A]

« Lancer le lot » n'existe que sur la page Résultats (`ui/main_dialog.py:237`).
Pendant un lot, l'affichage bascule sur le Journal, dont la barre d'actions est
vide [A]. Aucun pré-vol n'indique ni l'état de préparation de chaque domaine
(prêt / par défaut / manquant), ni la durée estimée.

L'AC ne balaie que les profils déjà ouverts dans l'onglet AC : 3 rails sur 17
sur P02. Le rapport écrit pourtant « 3 rails sur 3 » [A].

**Correctif :**
- un bouton « Tout analyser » visible partout, et l'annulation à côté ;
- un tableau de pré-vol avant lancement ;
- « 3 profils AC sur 17 rails ».

Effort faible à moyen.

### 3.4 Langue et formats pour un utilisateur francophone — majeur [V]

- **Virgule décimale :** les 71 `float(...GetValue())` de `ui/` la refusent. Un
  dialogue de charge remplace une saisie invalide par 0 A sans message [A].
- **Interface partiellement en anglais :**
  - les libellés de navigation sont figés sans `_()`
    (`ui/workspace_navigation.py:20-36`) : « Power Tree & DC »,
    « Diagnostics » ;
  - la boîte du lot, les énumérations et tout le journal restent en anglais [A] ;
  - les termes ne sont pas cohérents : « EMI / CEM » ; « Phase 10 » visible dans
    l'interface.
- **Rapport :** titres, descriptions et « Que faire » restent en anglais dans
  le rapport français, par choix de conception. Garder titres et descriptions en
  anglais se défend (traçabilité). « Que faire » est en revanche la phrase qu'on
  exécute. Les nombres gardent le point décimal ; l'horodatage est en UTC
  (20:39) pour un fichier nommé 223902.

**Correctif :**
- un `parse_float` unique, qui accepte la virgule et refuse explicitement ;
- `_()` sur la navigation, la boîte du lot et le journal ;
- des recommandations traduites (gabarits centralisés) ;
- l'heure locale dans le rapport.

Effort faible à moyen, fort pour la traduction des recommandations.

### 3.5 Chemins personnels en valeurs par défaut — majeur [V]

`models.py:464` met `spice_library_path` sur
`C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE` pour tout nouveau projet ;
ngspice, PSpice et openEMS ont aussi des chemins fixes. Le sidecar enregistre ces
réglages de machine, dont l'hôte, l'utilisateur et la clé SSH de Palace [A],
alors que le README les dit « hors du projet ».

**Correctif :** des défauts vides avec détection automatique, les réglages de
machine côté exécution, et un avertissement quand un chemin n'existe pas.
Effort faible.

### 3.6 Aucune relance incrémentale — majeur [V]

`CampaignCache` n'est instancié que dans les tests. Chaque lot recalcule les sept
domaines, même pour une seule source CEM modifiée.

Une fois branché, le condensé actuel aurait des failles [A] :
- deux tableaux NumPy de plus de 1 000 éléments donnent le même condensé ;
- la clé ignore les résultats amont et la version de l'outil.

**Correctif :** brancher le cache avec une clé = empreinte de la carte +
configuration du domaine + résultats amont + version, et l'annoncer
(« réutilisé ») dans le pré-vol. Effort moyen.

### 3.7 Mineurs d'ergonomie

| Point | Où | Correctif |
|---|---|---|
| Journal sans enregistrement, filtre ni pause de défilement ; « [INIT] [INIT] » en double | `ui/main_dialog.py`, `ipc_entry.py` [V journal] | Panneau « Lot » par domaine ; journal fichier dans le dossier de campagne |
| Pas de « Rescanner » une fois le sidecar créé ; tension invalide ignorée sans message | `ui/power_tree_panel.py` [A] | « Rescanner et fusionner » ; pastille d'état par rail |
| « Live PCB geometry changed » écrit au premier lot de chaque lancement (signature initiale None) | `ui/main_dialog.py:156, 365` [V] | Message « première capture » |
| Rapport sans sommaire, `<details>` perdus à l'impression, chemin absolu de la machine | `report/html_report.py` [A] | Sommaire, `@media print`, liens relatifs |
| Glossaire « × 1,5 par analyse de plus » contre `1 + 0,5·(n−1)` dans le code | `html_report.py:372`, `campaign.py:648` [V] | Aligner le texte |
| Build non relié à un commit ; README sans lot, rapport, CSV ni guide français | `runtime_environment.py`, `README.md` [A] | `export-subst`, guide « premier rapport » |
| Figures absentes si DC ou THERMAL termine le lot | `ui/main_dialog.py:796` [A] | Attendre les rendus avant d'écrire le rapport |
| CSV sans run_id, date, version, état ni décision | `report/csv_export.py` [A] | Colonnes d'identité ; `domains-<stamp>.csv` |

## 4. Données et historique

### 4.1 L'historique n'a aucune borne — majeur [V]

`KiPIDA-results` de P02 pèse 685 Mo pour 330 entrées, accumulées depuis le
2026-09-21. La répartition [A] :
- le DC fait 64 % du volume, avec 12,6 Mo et 227 PNG par run ;
- la Phase 10 fait 18 %, dont 34 dossiers sans manifest, que `clear()` ne voit
  pas.

Le dossier est dans le dépôt git du projet, ni ignoré ni suivi : un `git add -A`
emporterait 685 Mo.

**Correctif :**
- un `.gitignore` à la création du dossier ;
- une rétention par défaut : les N derniers runs par domaine, plus ceux d'une
  campagne conservée ;
- des PNG DC limités aux vues 3D et aux rails en cause ;
- un manifest pour la Phase 10.

Effort faible (gitignore) à moyen.

## 5. Code

### 5.1 Exceptions avalées — majeur [V]

On compte 147 `except Exception` ou `except:` hors tests, dont 22 dans
`ui/main_dialog.py` et 15 dans `plotter.py`, et 10 suivis d'un simple `pass`.
Un résultat partiel peut ainsi passer pour complet ; c'est le mécanisme qui
rend §1.1 possible au niveau de chaque domaine.

**Correctif :** journaliser chaque exception attrapée, avec le domaine ; ne
garder `pass` qu'avec un commentaire qui justifie l'abandon. Effort moyen.

### 5.2 Code mort et taille des modules — mineur [V]

- **Branché nulle part :** `CampaignCache`, `compare_campaigns` et `last_outcomes`
  (voir §1.1, §1.6 et §3.6).
- **Modules de plus de 2 200 lignes :** `analysis_adapters.py` (2 647),
  `emc_analyzer.py` (2 513), `ui/main_dialog.py` (2 369) et `emc_phase10.py`
  (2 279). Ils mêlent adaptation, décision et texte.
- **Imports :** 45 fichiers portent le double schéma `try: from .x` /
  `except (ImportError, ValueError): from x`.

**Correctif :** découper les adaptateurs par domaine et sortir le pilotage du lot
de `main_dialog`. Pour les imports, choisir un seul mode, paquet ou modules à
plat. Effort fort, sans urgence.

### 5.3 Sécurité — rien d'alarmant [V]

- L'accès Palace passe par le `ssh` du système, en liste d'arguments, avec
  `shlex.quote` sur les chemins distants et une vérification de clé d'hôte
  STRICT par défaut.
- `pickle` ne sert qu'à un cache mémoire de maillages produits par l'outil
  (`ac_model.py:798`).
- Aucun `shell=True` ni `eval`.

## 6. Points forts à préserver

- **La page « Décision » :** cinq listes, « Comment lire », glossaire, et des
  fiches Pourquoi / Que faire / Comment vérifier / Certitude / Effort.
- **L'honnêteté des seuils** (« heuristique de l'outil, pas une norme ») et la
  rubrique « Fiabilité du calcul » séparée des défauts de la carte.
- **Les contrôles de maillage** (DC, AC à mi-pas, thermique à double pas), qui
  sont publiés.
- **Un lot qui isole les échecs par domaine**, garde les domaines finis à
  l'annulation, justifie son ordre dans le journal et partage le DC.
- **La Phase 10 sur circuit de carte :** le courant de l'arbre est imposé, et la
  source est requalifiée par la simulation.
- **Un sidecar sans perte à l'aller-retour**, et une architecture i18n validée par
  test : il manque seulement les appels aux bons endroits.
- **Des choix de performance mesurés et documentés** (AC sur CPU, seuil CUDA de
  la CFD).
- **Une suite de 1 177 tests**, avec des régressions liées à des faits de P02.

## 7. Plan proposé

Chaque lot est indépendant ; l'ordre suit ce qui peut faire dire au rapport
quelque chose de faux.

1. **Vérité du rapport** (faible à moyen) : §1.1 couverture et INCOMPLET, §1.2
   décision unique, §1.5 « ce qui tient », §1.3 constats pour les défauts
   déterminants, §2.1 tensions de l'arbre dans les règles de schéma.
2. **Boucle de données** (moyen) : §3.1 courbe de rendement et modèles
   d'inductance dans l'interface, §3.2 enregistrement automatique et atomique,
   §2.3 préréglage « élément série », §2.2 TE-002 gradué par marge.
3. **Suivi de révision** (moyen à fort) : §1.6 campagne JSON et comparaison, §1.7
   un seul résultat par domaine, §1.8 version dans les résultats, §4.1 rétention
   et `.gitignore`.
4. **Confort francophone et parcours** (faible à moyen) : §3.4 virgule,
   traductions et heure locale, §3.3 lot et pré-vol, §3.5 chemins par défaut,
   §3.7 mineurs.
5. **Performance et code** (moyen à fort) : §3.6 cache, §2.4 AC en parallèle,
   §5.1 exceptions, §5.2 découpage.

Une revue de la physique des solveurs (§2.5) reste à mener quand le temps machine
le permettra.

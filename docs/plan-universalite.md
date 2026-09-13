# Ki-PIDA — Plan d'universalisation

Objectif : que Ki-PIDA produise des résultats justes sur **n'importe quel projet
KiCad ou Zeo**, sans réglage propre à une carte, et que la compatibilité
multi-cartes Zeo ne s'active que là où elle a un sens.

Ce plan s'appuie sur `docs/refonte-analyses.md` (audit), `docs/plan-ameliorations.md`
(backlog) et `docs/audit-cablage.md`, sans les refaire. Les chiffres du §1 ont
été mesurés le 2026-09-13, en lecture seule, sur les projets de démonstration
installés avec KiCad 10 et Zeo 0.3.1.1, et sur le conteneur Zeo de référence.

---

## 0. Décisions actées

1. **Universalité.** Aucun correctif ni critère d'acceptation ne vaut s'il n'est
   vrai que sur la carte de référence. On valide par des invariants, sur un
   corpus, pas par les chiffres d'une carte.
2. **Surcouche Zeo conditionnelle.** Elle ne s'active que si le plugin est lancé
   depuis un conteneur Zeo ou depuis le logiciel Zeo. Dans tous les autres cas :
   mode mono-carte, et les données `multi_board` ne sont pas lues.
3. **`boards/p02_revision_e` est hors du plan** : jamais modifiée, jamais utilisée
   comme cas de validation, aucune décision la concernant.
4. **Pas de rétrocompatibilité KiCad 6/7.** Les fichiers antérieurs à KiCad 8
   sont refusés explicitement par les lecteurs de fichiers ; les analyses qui
   passent par l'API IPC ne sont pas concernées (l'hôte est toujours KiCad 9+
   ou Zeo).
5. **Liens Zeo = `cross_board_nets` du conteneur**, déjà résolus par Zeo. Pas de
   retraçage des fils du `.kicad_mbs`.
6. **Mode conteneur : Zeo seulement pour le moment.** Un conteneur ouvert dans
   KiCad reste en mono-carte. La prise en charge du multi-cartes de KiKit est
   prévue (phase 7), pas planifiée.

---

## 1. Point de départ mesuré

Corpus : 36 projets de démo KiCad 10 (copies Zeo : résultats identiques),
formats KiCad 8 à 10 ; 15 cartes avec PCB et schéma, 18 projets schéma seul,
1 PCB seul. Aucun fichier KiCad 6/7 dans le corpus.

| Défaut | Mesure | Où |
|---|---|---|
| Feuille instanciée plusieurs fois lue une seule fois | complex_hierarchy 73/119 symboles, multichannel 61/169, vme-wren 1 606/2 876 | `schematic_reader` (`visited` par fichier) |
| Appariement empreinte → symbole par chemin | 0 % sur 15/15 cartes ; repli par référence faux en multi-instance (39/68, 36/114) | `netlist_builder` (clé = chemin de feuille, pas chemin d'instance) |
| UUID de symbole seul ambigu | 58/68 et 104/114 empreintes | — (piège pour tout correctif naïf) |
| Nom de projet des instances ≠ nom du fichier | 9 projets (cm5 177 symboles, jetson 67, openair 48…) | projets renommés ou copiés |
| Faux SCH-005 (« empreinte sans symbole ») | multichannel 78, complex_hierarchy 29 ; tiny_tapeout 159 et cm5 28 sur des empreintes placées sans schéma | `rules/schematic_rules.py` |
| SCH-002 signale les no-connect volontaires | pic_programmer 77 marqueurs / 77 findings, kit-dev 69/69, interf_u 63/63 | idem |
| Condensateurs non reconnus (`100n`, `10u`, `4u7`) | cm5 17/19 → SCH-001 ×10 | `_CAPACITOR_VALUE_RE` exige « F » |
| Rails par type de broche | 38/53 nets portés par un symbole d'alimentation ; openair-max 0/7 | `netlist_builder` |
| Faux rails open-collector | 15 (cm5 8, royalblue 4, video 2, kit-dev 1) | `_POWER_TYPES` |
| Résolution par dossier | ecc83 (2 projets) → le premier trouvé, sans erreur | `project_resolver` (glob) |
| Résolution d'une carte Zeo | ne remonte jamais au conteneur (clé `filename` inexistante) ; tout projet Zeo marqué multi-cartes ; liens inter-cartes appariés par nom | `project_resolver`, `multiboard` |
| Découverte des rails par nom | `VCC`, `VDD`, `PWR`, `\+.*V`, `GND` | `discovery.py:27` |
| Constantes inventées | horloge 25 MHz, découpage 500 kHz, paires 240/125 MHz | `emc_analyzer.py:393-433` |
| Empreinte de cache | ne hashe que la feuille racine ; `_board_fingerprint` hashe le PCB sur disque alors que les analyses lisent la carte vivante | `design_model`, `ui/main_dialog.py:447` |
| Échelle | vme-wren 72 Mo : `read_board` 12,5 s, 813 Mo RSS ; schéma 7 s | acceptable, à borner |

Hôte (constaté) :

* Zeo répond à `get_version()` **« 10.0.3 (10.0.3) »** (`~/.codex/tools/zeo-mcp/zeo_mcp.log`) :
  la version IPC ne distingue pas Zeo de KiCad.
* Zeo partage avec KiCad 10 le dossier de plugins
  (`Documents\KiCad\10.0\3rdparty\plugins\KiPIDA-pr`) et l'environnement Python
  (`AppData\Local\KiCad\10.0\python-environments\com.github.kbralten.kipida`) :
  l'emplacement d'installation ne distingue pas non plus les hôtes.
* Cet environnement a été créé par Zeo (`pyvenv.cfg` : `home = C:\Program Files\Zeo\v0.3.0\bin`)
  et porte le chemin de KiCad 10 : `sys.executable` et `sys.base_prefix` disent
  qui a créé l'environnement, pas qui a lancé le plugin.
* Aucune API multi-cartes n'existe dans le kipy livré avec zeo-mcp 0.6.3.
* Interrogé en lecture seule hors plugin (2026-09-13), un Zeo 0.3.0 en cours
  d'exécution répond `get_version` = `10.0.3`, `get_api_version` =
  `10.0.1-0-g2db9e5a72b`, `get_kicad_binary_path("kicad-cli")` =
  `C:/Program Files/Zeo/v0.3.0/bin/kicad-cli.exe`, et `Project.path` = le
  `.kicad_pro` de la carte ouverte. Le chemin du binaire est donc un candidat
  sérieux pour la détection (§3.2) ; il reste à l'observer depuis un lancement
  du plugin, et dans le cas d'une carte ouverte depuis un conteneur.
* Ce que `Project.path` renvoie quand une carte membre est ouverte depuis le
  conteneur dans Zeo **n'est pas connu** : aucun journal existant ne le consigne.

---

## 2. Principes de conception

Ils servent de critère de revue pour chaque commit du plan.

* **P1 — L'hôte donne l'identité.** Projet et carte viennent de l'API IPC
  (`Project.path`, document de la carte), jamais d'une recherche de fichiers ni
  d'un nom deviné. Un chemin explicite n'est accepté que hors plugin
  (validation, tests).
* **P2 — Les clés sont des chemins d'instance et des UUID.** Références et noms
  de nets sont des étiquettes, jamais des clés de jointure.
* **P3 — L'absence est un état.** Schéma seul, PCB seul, empreinte sans symbole,
  feuille manquante, fichier non sauvegardé : chacun est représenté et rapporté,
  jamais avalé par un `except: pass`.
* **P4 — Les déclarations du concepteur passent avant les heuristiques**, avec
  source et confiance : configuration saisie > drapeau `power` des symboles,
  netclasses, règles Zeo > types de broche > noms. Une valeur inconnue reste
  inconnue plutôt que remplacée par une constante.
* **P5 — Le format est détecté par sa version.** Ce qui n'est pas pris en charge
  est signalé, jamais lu à moitié.
* **P6 — Mono-carte par défaut ; Zeo en surcouche conditionnelle** (§3). Aucun
  comportement mono-carte ne dépend de la présence de Zeo.
* **P7 — On valide par invariants sur corpus**, pas par les chiffres d'une carte.

---

## 3. Spécification de la surcouche Zeo

### 3.1 Table de décision

| Hôte détecté | Projet de lancement | Mode | Comportement |
|---|---|---|---|
| KiCad | quelconque, y compris un conteneur ou une carte membre | mono-carte | `multi_board` et `.kicad_mbs` ne sont pas ouverts |
| Zeo | conteneur (`multi_board.container = true`) | Zeo-conteneur | cartes de `sub_projects`, liens validés |
| Zeo | carte dont le conteneur se résout et qui y figure | Zeo-membre | carte analysée, liens vers les autres cartes validés |
| Zeo | carte autonome (`container_project_relative_path` vide) | mono-carte | statut « Zeo, projet autonome » |
| Zeo | conteneur introuvable, ou carte absente de `sub_projects` | mono-carte | limitation explicite (orphelin), avec la raison |
| inconnu | quelconque | mono-carte | détection non concluante consignée |

Le mode, l'hôte et les preuves de détection sont portés par un `ProjectContext`
unique, transmis à toutes les analyses et rendu dans la provenance du rapport.

### 3.2 Détection de l'hôte

Écartés sur preuve : `get_version()`, dossier du plugin, dossier de
l'environnement Python et interpréteur (§1). Écarté par principe : le contenu des fichiers
(`multi_board` est écrit par Zeo mais le fichier peut être ouvert dans KiCad).

Candidats, à départager en phase 0 :

1. l'exécutable du processus hôte qui a lancé le plugin (`Zeo.exe`, `Zeo.app`) ;
2. `get_kicad_binary_path()` et la présence de l'exécutable Zeo dans ce dossier
   (structurel, indépendant du nom du dossier d'installation) ;
3. une variable d'environnement posée par Zeo au lancement des plugins, s'il en
   existe une.

Une seule fonction `detect_host()` rend `{KICAD | ZEO | UNKNOWN, preuves}`.

---

## 4. Phases

Taille relative : S (≤ 1 commit), M (2-4), L (5+). Un commit en anglais par
élément numéroté ; suite verte à chaque commit.

### Phase 0 — Mesurer avant de modifier (S+M)

**0.1 Sonde d'hôte.** Ajouter au journal d'entrée (`kipida_entry.log`) :
`get_version`, `Project.name/path`, document de la carte, `get_kicad_binary_path`,
exécutable du processus parent, **noms** des variables `KICAD_*`/`ZEO_*` (jamais
leurs valeurs : `KICAD_API_TOKEN` est un secret). Aucun changement de comportement.

À lancer par l'utilisateur dans quatre situations :
KiCad 10 avec une démo ; Zeo avec `p02_alimentation` ouverte depuis le conteneur ;
Zeo avec `p02_alimentation` ouverte directement ; Zeo avec un projet non membre
(une démo copiée dans un dossier temporaire).

*Sortie* : règle de détection choisie sur preuves et consignée ici ; valeur de
`Project.path` en mode membre connue.

*État au 2026-09-13* : la sonde est livrée (`host_probe.py`, appelée par
`ipc_entry.py`) et le plugin installé en porte la même version que le dépôt
(fichiers identiques octet pour octet) ; aucun redéploiement n'est nécessaire.
`%TEMP%\kipida_host_probe.log` n'existe pas encore : aucun des quatre lancements
n'a eu lieu. La règle de détection reste à choisir, et la phase 3.2 attend ce
journal. Une observation hors plugin a été écartée : l'instance Zeo qui répond
sur le canal IPC par défaut est probablement celle où `p02_revision_e` est
ouverte, et cette carte est hors du plan.

**0.2 Harnais de corpus.** `validation/ingest_corpus.py`, lecture seule, chemins
en argument, n'écrit rien à côté des projets. Il mesure les invariants de la
phase 1 à 4, pas un second parseur. Mesure initiale consignée dans
`plan-ameliorations.md`.

### Phase 1 — Lecture universelle du schéma (M)

1.1 Parcours par **instance de feuille** (et non par fichier) ; chemin d'instance
    complet pour chaque symbole ; référence prise dans l'entrée dont le chemin
    correspond, quel que soit le nom de projet inscrit.
1.2 Feuille référencée mais absente, séparateur ou casse de `Sheetfile`
    différents : limitation explicite, plus d'omission silencieuse.
1.3 Données collectées pour les phases suivantes : drapeau `power` des
    définitions, `lib_name`, marqueurs `no_connect`, `dnp` / `in_bom` / `on_board`,
    symboles multi-unités.
1.4 Détection de version de format : les fichiers antérieurs à KiCad 8 sont
    refusés avec un message qui nomme la version lue et invite à réenregistrer
    le projet (décision 4). Seuils tirés des constantes de format de KiCad 8.0
    (`SEXPR_SCHEMATIC_FILE_VERSION`, `SEXPR_BOARD_FILE_VERSION`), à vérifier
    avant de les figer : le corpus contient des schémas KiCad 8 (20231120) mais
    aucun PCB KiCad 8.

*Invariants* : sur les 15 cartes du corpus, nombre de symboles égal au parcours
par instance (119, 169, 2 876 sur les cas multi-instances).
*Tests synthétiques* : feuille à deux instances, projet renommé, hiérarchie à
trois niveaux, feuille manquante, `lib_name`, symbole multi-unités.

**Fait le 2026-09-13** (`21b63fe`, `a5c1c23`, `772c91c`, `19b49cd`). Mesuré par
`validation/ingest_corpus.py` sur les 36 démos et le conteneur de référence :

* symboles 119, 169 et 2 876 sur complex_hierarchy, multichannel et vme-wren
  (73, 61 et 1 606 avant) ;
* I1 à 100 % sur les 15 cartes des démos, 0 % avant ; sur la référence,
  p02_alimentation 124/124, LEDS_sub 17/17, main 437/440, les trois empreintes
  restantes (J1, J2, J5) pointant vers des symboles absents de tous ses fichiers
  schéma : carte non mise à jour, pas défaut de lecture ;
* SCH-005 passe de 29 à 0 sur complex_hierarchy et de 78 à 0 sur multichannel ;
* I5 et I6 sont mesurés et retrouvent les comptes faits à la main au §1 :
  SCH-002 sur broches marquées 77/77, 69/69, 63/63, et 119/217 sur video (les
  98 broches sans marqueur de la phase 4) ; nets de symbole d'alimentation
  classés rails 38/53, openair-max 0/7 ;
* aucune feuille manquante ni ancienne dans le corpus.

Deux choix pris sur mesure plutôt que sur l'énoncé :

* **Placement des broches** (marqueurs no-connect) : parmi les huit lectures
  possibles (sens de rotation, axe du miroir, ordre), celle retenue pose 6 756
  des 7 368 broches de symboles tournés ou en miroir sur un fil, une étiquette,
  une jonction ou un marqueur, et gagne dans les sept combinaisons à la fois ;
  la suivante en pose 5 629.
* **Seuils de format (1.4)** : KiCad 7.0.0 écrit 20230121 (schéma) et 20221018
  (PCB), KiCad 8.0.0 20231120 et 20240108 (sources KiCad aux étiquettes). Un
  seuil à 8.0 refuserait une feuille de la démo vme-wren livrée avec KiCad 10
  (20230819, cycle de développement de la 8) : KiCad ne réécrit pas une feuille
  que personne n'a modifiée. Sont donc refusés les fichiers écrits par KiCad 7.0
  ou antérieur. Le refus porte sur la carte et sur la feuille racine, que KiCad
  réécrit à chaque enregistrement ; une sous-feuille ancienne d'un projet actuel
  est lue et signalée (`old_sheet_format`).

### Phase 2 — Lien PCB ↔ schéma et modèle de conception (M)

2.1 Appariement empreinte → symbole par chemin d'instance complet ; empreinte
    sans chemin = état « PCB seul » ; collisions de références diagnostiquées.
2.2 `DesignModel` : hash de tous les fichiers effectivement lus (et des fichiers
    Zeo seulement si la surcouche est active) ; plus de `except Exception: pass`
    dans `design_model` et `project_resolver`.
2.3 Divergence carte vivante / fichier : comparer l'ensemble des empreintes du
    snapshot IPC à celui du fichier ; si différent, limitation « modifications
    non sauvegardées » sur les analyses qui lisent les fichiers. *Faisabilité à
    confirmer sur le snapshot existant.*

*Invariant* : 100 % des empreintes à chemin appariées, zéro repli par référence,
sur les 15 cartes. *Tests* : empreinte sans chemin, collision de référence.

**Fait le 2026-09-13** (`b180996`, `cb14bef`, `7818dc2`).

* 2.1 : le repli par référence n'existe plus. Chaque composant porte son état
  (`linked`, `pcb_only`, `symbol_missing`, `no_schematic`) et la netlist
  diagnostique références en double et références divergentes. Toutes les
  empreintes à chemin sont liées sur les 15 cartes des démos, p02_alimentation
  et LEDS_sub ; main garde ses 3 `symbol_missing`. Seul signalement : références
  en double sur les empreintes posées sans schéma de tiny_tapeout. Aucune règle
  ne change de résultat sur le corpus.
* 2.2 : l'empreinte de hachage couvre toutes les feuilles lues (une modification
  de sous-feuille la laissait inchangée) ; les `except Exception: pass` de
  `design_model` et `project_resolver` sont remplacés par des entrées
  `issues`/`notes`. Les fichiers Zeo restent hachés dès qu'un conteneur est
  résolu : les limiter à la surcouche active demande le `ProjectContext` (3.2).
* 2.3 : faisable sur la capture existante (`DCBoardSnapshot` porte toutes les
  empreintes et leurs pastilles). `saved_footprint_differences` nomme les
  empreintes ajoutées, supprimées ou déplacées ; le moteur des règles schéma en
  fait une limitation « Unsaved changes ». Pas ajouté au DC, que DC-004 couvre
  déjà sur le cuivre de ses rails.

### Phase 3 — Résolution de projet et surcouche Zeo (L)

3.1 Entrée par l'identité hôte (P1) ; statuts complet / schéma seul / PCB seul ;
    dossier ambigu = erreur listant les candidats (hors plugin uniquement).
3.2 `detect_host()` et `ProjectContext` selon §3 ; mode rendu dans la provenance.
3.3 Surcouche Zeo, active seulement selon la table §3.1 :
    * cartes depuis `sub_projects` (autorité), blocs `.kicad_mbs` rattachés ;
    * liens depuis `cross_board_nets` (extrémités carte/composant/broche),
      **validés contre le PCB de chaque carte** : OK / périmé / manquant ;
    * identifiant de net qualifié `(carte, net)` ; net système par union à
      travers les nets locaux ;
    * paires différentielles pointant vers des nets inconnus signalées ;
    * champs inconnus (`current_rules`, `voltage_rules`…) conservés bruts.
3.4 Suppression de l'appariement inter-cartes par nom (`extract_cross_board_nets`).

*Tests* : une ligne de la table §3.1 = un test ; en hôte KiCad, un test vérifie
que ni le conteneur ni le `.kicad_mbs` ne sont ouverts ; lien périmé et broche
manquante sur fixtures synthétiques. *Validation* : corpus entier en mono-carte ;
conteneur de référence (`p02_alimentation`, `main`, `LEDS_sub`) en hôte Zeo simulé,
résultat consigné sans être figé dans un test.

**Fait le 2026-09-13, sauf la règle de détection** (`4cc6b91`, `030bb65`,
`1837cf8`, `77971d7`).

* 3.1 : un `.kicad_pro` est résolu comme lui-même (la remontée vers le conteneur
  par une clé `filename` que Zeo n'écrit pas est supprimée) ; un dossier à
  plusieurs projets est une erreur qui les nomme (I7 : plus de choix silencieux
  sur `ecc83`) ; `is_part_of_multiboard` n'est plus vrai pour tout projet
  enregistré par Zeo ; `BoardProject.status`.
* 3.2 : `ingest/project_context.py` applique la table §3.1, une ligne un test.
  `detect_host()` rassemble les indices (chemin de `kicad-cli` et présence de
  l'exécutable Zeo à côté, ascendance des processus, noms des variables) et
  **répond `unknown`** : la règle reste à choisir sur le journal des quatre
  lancements. D'ici là, tout lancement est en mode mono-carte. Le contexte est
  construit par `ipc_entry`, journalisé, et rendu dans le rapport de campagne
  (hôte et mode, avec la raison).
* 3.3 : `ingest/zeo_container.py` lit `sub_projects`, `cross_board_nets`,
  `cross_board_diff_pairs`, rattache les blocs du `.kicad_mbs` et garde bruts les
  champs non interprétés. `validate_links` vérifie chaque extrémité contre le PCB
  de sa carte (ok, périmé, manquant) et forme les nets système par union de nets
  qualifiés `(carte, net)`. La validation n'a pas lieu au lancement (lecture de
  tous les PCB) mais à la demande.
* 3.4 : `extract_cross_board_nets` est supprimé ; les nets inter-cartes et les
  alias viennent des liens validés.

Validation. Corpus : les 36 démos donnent le mode mono-carte sous les trois
hôtes (en Zeo simulé, « projet autonome »). Conteneur de référence en Zeo
simulé : trois cartes, 16 liens et 40 extrémités tous valides, 15 nets système
(les deux liens `GND` se rejoignent), mêmes 15 nets que l'ancien appariement par
nom sur cette carte. Les deux paires différentielles déclarées sont signalées :
`USB_HOST_DP_+`/`USB_HOST_DM_-` et la paire `USB_DEVISE_…` nomment des nets
qu'aucune carte ne porte, alors que les liens portent `USB_HOST_D_+` et
`USB_HOST_D_-`. p02_alimentation et LEDS_sub donnent le mode membre ;
p02_revision_d et p02_refonte_pd, le mode mono-carte « projet autonome ».

### Phase 4 — Règles fiables, puis branchées (M)

4.1 SCH-001 : condensateur identifié par la classe du composant (préfixe,
    mots-clés de bibliothèque) et valeur à préfixe SI avec ou sans « F » ;
    broches sur nets non connectés exclues.
4.2 SCH-002 : broches portant un marqueur `no_connect` exclues.
4.3 SCH-003 : un constat agrégé par projet au lieu d'un INFO par composant.
4.4 SCH-004 : n'existe qu'en mode Zeo ; porte sur les liens périmés ou manquants,
    plus sur des homonymies.
4.5 SCH-005 : ne compare que les empreintes qui ont un chemin ; les empreintes
    « PCB seul » font l'objet d'un INFO agrégé.
4.6 Rails : un net open-collector n'est plus un rail ; le drapeau `power` devient
    l'indice principal ; chaque classement porte source et confiance.
4.7 Branchement : règles schéma dans le batch via `ProjectContext` ; garde
    d'accessibilité étendue à `rules` et au résolveur.

*Invariants corpus* : SCH-005 = 0 sur les 15 cartes ; SCH-002 = 0 sur
pic_programmer, kit-dev, interf_u ; 53/53 nets à symbole d'alimentation classés ;
0 rail open-collector seul ; 19/19 condensateurs reconnus sur cm5. Le reliquat
SCH-002 de `video` (98 broches sans marqueur) est examiné et documenté, pas figé.

### Phase 5 — Analyses : déclarations du projet au lieu de suppositions (L)

5.1 Rails DC/AC : liste issue du `DesignModel` (P4) ; la regex de `discovery.py`
    devient un indice de dernier recours, confiance basse, affiché comme tel.
5.2 Sources EMC : fréquence lue sur le composant (valeur de quartz ou
    d'oscillateur, champ explicite) ou saisie ; sinon **fréquence inconnue**, et
    les constats qui en dépendent sont rétrogradés au lieu d'être calculés sur
    25 MHz / 500 kHz / 240 MHz inventés.
5.3 Découplage et connecteurs EMC : par symbole et type de broche, plus par nom
    d'empreinte.
5.4 Paires différentielles : aligner la détection sur la convention de suffixes
    de KiCad lui-même (`+`/`-`, `P`/`N`) et sur les netclasses, qui sont la
    déclaration native.
5.5 Masse : net désigné par la configuration ou par le symbole d'alimentation ;
    le nom reste un indice.

*Invariant* : sur le corpus, aucune fréquence ni aucun rail n'apparaît dans un
résultat sans provenance traçable à un champ du projet ou à une saisie.

### Phase 6 — Plateformes et échelle (M)

6.1 Détection d'hôte et chemins sous Windows, macOS (`Zeo.app`) et Linux ;
    systèmes de fichiers sensibles à la casse.
6.2 Budget mémoire et temps sur les grands projets (vme-wren, jetson) : cache par
    empreinte, lecture bornée, message clair au-delà.
6.3 CI : fixtures synthétiques exécutées sur le runner Linux ; harnais de corpus
    exécuté à la main, hors CI.

### Phase 7 — Multi-cartes KiKit (prévue, non planifiée)

KiKit matérialise le multi-cartes autrement que Zeo : **un seul projet, un seul
schéma (une feuille par carte) et un seul `.kicad_pcb`** où les cartes sont
dessinées côte à côte. Chaque carte est désignée par une empreinte d'annotation
`kikit:Board` pointant sur son contour, ou par un rectangle, puis extraite par
`kikit separate --source 'annotation; ref: B1'` (ou `'rectangle; tlx: …'`).
KiKit recommande des labels locaux pour que les nets de chaque carte restent
séparés.

Ce que la prise en charge demandera, à préciser le moment venu :

* activation sur déclaration du concepteur (présence d'annotations `kikit:Board`
  dans la carte ouverte), dans le même esprit que la règle Zeo ;
* partition de la carte ouverte par contour `Edge.Cuts` désigné, puis analyses
  par carte ;
* liens inter-cartes par les connecteurs des feuilles, identifiants qualifiés
  comme en phase 3 ;
* réutilisation du `ProjectContext` (un troisième mode, pas un second chemin
  d'ingestion).

Rien de cette phase n'est codé avant qu'elle soit planifiée.

---

## 5. Stratégie de test

* **`tests/`** : fixtures synthétiques minimales écrites dans les tests, une par
  propriété. Aucun fichier de démo KiCad ni du projet de référence copié dans le
  dépôt (licences, et règle du projet).
* Chaque test de régression est d'abord exécuté contre l'ancien code ; un test qui
  passe sur le code défectueux ne prouve rien (`plan-ameliorations.md`, A3).
* **`validation/`** : harnais en lecture seule sur le corpus installé et sur le
  conteneur de référence ; résultats consignés dans `plan-ameliorations.md`.
* Les invariants ne dépendent d'aucune carte particulière.

---

## 6. Décisions

Toutes les questions ouvertes à la rédaction ont été tranchées le 2026-09-13
(§0, décisions 4 à 6). Plan validé le même jour.

## 7. Hors périmètre

* `boards/p02_revision_e` (décision actée).
* KiCad 6/7 (décision actée).
* Multi-cartes KiKit tant que la phase 7 n'est pas planifiée.
* Chute DC aller + retour, choix du mailleur du DC, items CFD A1/A2 : backlog
  existant.
* Budget de tension système chaîné entre cartes : après la phase 3.
* Restructuration en paquet `kipida/`.
* API IPC schéma : à évaluer après la phase 0, comme alternative éventuelle à la
  lecture des fichiers.

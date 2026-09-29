# Vision long terme — Delivery, Kanban, commissioning et documentation

> **Statut : vision produit / architecture future, avec frontière Delivery stabilisée par ADR-008.**  
> Ce document décrit la direction stratégique Delivery → Verification → intégrations. Le roadmap maître #55 conserve l'ordre d'activation. Pour #362, les décisions structurantes Planning/Delivery sont désormais acceptées dans ADR-008. Le passage de #362 à #363 comporte explicitement une gate **ASTRA-363** avant tout développement Verification.

## 1. Problème à résoudre

RessourcePlanner répond aujourd'hui principalement aux questions de demande, capacité et affectation :

- de quelles ressources un projet a-t-il besoin;
- quand;
- qui est disponible;
- qui est planifié sur quoi et quand.

Le besoin futur est de relier cette planification à l'exécution technique sans recréer un outil généraliste comme Jira.

Dans les projets d'automatisation, le chargé de projet travaille au niveau du projet et du WorkPackage, tandis que le Team Lead et les techniciens doivent souvent découper le travail en livrables et tâches techniques. Les changements d'échéance, de budget ou de portée circulent mal entre ces deux niveaux. Inversement, les réestimations et l'avancement technique ne remontent pas toujours assez tôt au chargé de projet.

Un second problème apparaît à la fin du développement : une fonction développée doit souvent être validée plus tard en FAT, SAT ou commissioning. Si les tests à réaliser ne sont pas capturés au moment où le développement est encore frais, ils peuvent être oubliés au chantier et la documentation de validation doit être reconstruite manuellement après coup.

## 2. Vision produit

À terme, RessourcePlanner relie quatre niveaux distincts :

```text
1. ENGAGEMENT / BESOIN
   Project + WorkPackage + demandes + budgets + échéances

2. CAPACITÉ / PLANIFICATION
   ResourceRequirement + Shift + actifs + véhicules

3. EXÉCUTION TECHNIQUE
   DeliveryPlan + Epic + Story + Kanban/Sprint

4. VÉRIFICATION / PREUVE
   VerificationRequirement + FAT/SAT/Commissioning
   + résultats + preuves + documentation
```

Le `WorkPackage` est le point de jonction naturel entre ces domaines.

## 3. Principe d'architecture

Conserver un **monolithe modulaire** :

- une application React;
- une API FastAPI;
- une base SQL;
- des frontières métier explicites;
- pas de microservices tant qu'une contrainte réelle ne le justifie.

Les domaines futurs doivent rester séparés conceptuellement :

```text
Project / WorkPackage
│
├── Planning
│   ├── main-d'œuvre
│   ├── véhicules
│   └── autres actifs
│
├── Delivery
│   ├── Epics
│   ├── Stories
│   ├── Kanban
│   └── Sprints optionnels
│
├── Verification
│   ├── FAT
│   ├── SAT
│   ├── Commissioning
│   └── preuves / résultats
│
└── Documents / Integrations
    ├── rapports de test
    ├── matrices de traçabilité
    └── Microsoft Planner / Teams
```

### Frontière Planning ↔ Delivery

Un `Shift` signifie :

> une ressource est réservée pendant une durée et une période données.

Une Story signifie :

> un élément de travail technique doit être réalisé.

Ces concepts ne doivent pas être confondus.

**Une Story ne crée pas automatiquement un Shift.**  
Delivery peut lire la capacité planifiée d'un WorkPackage et la comparer au travail restant, mais ne doit pas contourner les commandes du domaine Planning.

## 4. Module Delivery

Issue de référence : #362.

### 4.1 Modèle minimal retenu pour #362

L'analyse de #362 sur `main@4fb40665` confirme une extension additive du monolithe, sans refonte du moteur Planning.

Le MVP reste volontairement petit :

```text
WorkPackage
└── DeliveryPlan
    ├── DeliveryItem [EPIC]
    │   ├── DeliveryItem [STORY]
    │   └── DeliveryItem [STORY]
    └── DeliveryItem [EPIC]
```

Un modèle hiérarchique `DeliveryItem` avec un `type` peut suffire au départ plutôt que de créer plusieurs agrégats spécialisés.

Décisions stabilisées :
- un seul `DeliveryPlan` non archivé par WorkPackage dans le MVP;
- cycle du plan `DRAFT → ACTIVE → ARCHIVED`;
- responsables liés à `AppUser`;
- version de concurrence Delivery indépendante de `planning_version`;
- aucune Story ne crée ou modifie automatiquement un Shift.

Champs indicatifs :

- identifiant stable;
- WorkPackage / parent;
- type `EPIC` ou `STORY`;
- titre;
- description;
- statut;
- priorité;
- responsable;
- estimation;
- travail restant;
- échéance;
- position;
- sprint optionnel;
- identifiants externes éventuels.

### 4.2 Kanban

Board minimal :

```text
Backlog | À faire | En cours | Bloqué | Terminé
```

Les sprints sont facultatifs. Certains projets peuvent utiliser un cadence Scrum légère; d'autres doivent pouvoir rester en Kanban continu.

### 4.3 Flux Team Lead / techniciens

Le Team Lead peut découper un WorkPackage en Epics.

L'équipe peut ensuite :
- détailler un Epic en Stories;
- estimer ou réestimer;
- affecter les Stories;
- les déplacer sur le Kanban;
- marquer les blocages;
- utiliser des sprints seulement lorsque pertinent.

Le but n'est pas de reproduire Jira mais de couvrir le flux réellement utile aux équipes d'automatisation.

## 5. Roll-up vers le chargé de projet

Le chargé de projet doit rester capable de travailler principalement au niveau du WorkPackage.

Les détails des Stories servent à produire des projections consolidées :

```text
Stories
   ↓
Epics
   ↓
DeliveryPlan
   ↓
WorkPackage
```

Le WorkPackage peut alors exposer séparément :

- budget autorisé;
- capacité planifiée;
- effort technique estimé;
- travail restant;
- progression technique;
- forecast d'effort;
- forecast d'échéance;
- progression de vérification;
- risques et écarts.

### Progression

Décision #362 / ADR-008 : le MVP n'utilise pas de pourcentage manuel par Story. Une Story contribue à la progression uniquement lorsqu'elle est `DONE`.

La progression est pondérée par une **estimation de référence stable** :

```text
progression =
somme(estimation_reference des Stories DONE)
/
somme(estimation_reference des Stories incluses)
```

Les Stories non estimées doivent être signalées dans la couverture de l'indicateur. Une réestimation modifie le forecast/travail restant sans réécrire silencieusement l'historique de progression.

Exemple :

```text
WP Programmation

Référence WP          240 h
Capacité planifiée    232 h
Estimation équipe     252 h
Travail restant       136 h

Progression dev        46 %
Forecast effort       252 h
Échéance WP        28 nov.
Forecast équipe      1 déc.

⚠ risque budget / échéancier
```

Les heures actuelles du WorkPackage sont une **référence de planification**, pas un budget autorisé. Un vrai budget autoritaire pourra être ajouté plus tard depuis une source canonique/versionnée, notamment Acumatica. Capacité planifiée, estimation technique, travail restant et éventuel budget autorisé futur restent des valeurs distinctes.

## 6. Communication bidirectionnelle PM ↔ équipe

Le système doit rendre visibles les changements sans les propager silencieusement de façon destructive.

### Changement depuis le WorkPackage

Exemple :

```text
Échéance
28 novembre → 15 novembre
```

Le Team Lead reçoit un signal du type :

```text
Contraintes du WorkPackage modifiées.
Le plan de livraison doit être réévalué.
```

Même principe pour un budget réduit ou un changement de portée.

### Changement depuis Delivery

Si les Stories passent d'une estimation totale de 252 h à 310 h :

- le forecast WorkPackage remonte à 310 h;
- un écart est visible au chargé de projet;
- le budget autorisé n'est pas augmenté automatiquement.

Le produit doit conserver la distinction entre **autorisation** et **état opérationnel / forecast**, cohérente avec les principes établis par #13.

## 7. Module Verification

Issue de référence : #363.

### 7.1 Capture au moment du développement

Lorsqu'une Story est complétée, demander explicitement si une vérification ultérieure est nécessaire :

```text
Aucun test requis
FAT
SAT
Commissioning
Combinaison de phases
```

Le but est de capturer l'intention de test pendant que le développeur connaît encore précisément la fonction.

La Story peut générer plusieurs `VerificationRequirement`.

Exemple :

```text
Story
"Implémenter arrêt basse pression pompe P-101"

        ↓

VerificationRequirement
"Vérifier arrêt P-101 lorsque PT-101 < 20 psi"

Phase : Commissioning
Type : Fonctionnel
Criticité : Haute
Résultat attendu : arrêt P-101 + alarme
```

### 7.2 Test indépendant de la Story

Les vérifications ne doivent pas être stockées comme simple sous-champs de la Story.

Modèle conceptuel :

```text
Story
  ↓ origine
VerificationRequirement
  ↓
TestExecution
  ↓
Résultat / preuve
```

Cette séparation permet notamment :

- plusieurs tests pour une Story;
- exécution bien après la fermeture de la Story;
- répétition/retest;
- tests créés manuellement;
- traçabilité sans rouvrir artificiellement le travail de développement.

### 7.3 Exécution en chantier

Statuts indicatifs :

- `NOT_RUN`;
- `PASS`;
- `FAIL`;
- `BLOCKED`.

Une exécution peut capturer :

- exécutant;
- date/heure;
- action/méthode;
- résultat réel;
- mesures observées;
- commentaire;
- preuve photo/capture/fichier/lien;
- raison de blocage;
- référence à l'exigence de vérification.

### 7.4 Commissioning package

Au début de la phase terrain, RessourcePlanner peut agréger les exigences produites pendant le développement :

```text
WorkPackage
└── Commissioning package

PASS       27
FAIL        2
BLOCKED     2
À faire    11
```

La progression de développement et la progression de vérification restent deux mesures distinctes.

Un WorkPackage peut donc être « développement 100 % » tout en ayant encore des tests commissioning ouverts.

## 8. Documentation automatique

Les données structurées de Verification doivent permettre de produire des documents sans maintenir une seconde source de vérité.

Documents futurs possibles :

- plan de test;
- rapport FAT;
- rapport SAT;
- rapport de commissioning;
- liste de déficiences issue des FAIL/BLOCKED;
- matrice de traçabilité.

Exemple de traçabilité :

```text
WorkPackage
  → Epic
    → Story
      → VerificationRequirement
        → TestExecution
          → résultat / preuve
```

Un rapport généré peut inclure :

- projet;
- WorkPackage;
- système;
- identifiant du test;
- origine Story;
- préconditions;
- action;
- résultat attendu;
- résultat réel;
- PASS/FAIL/BLOCKED;
- exécutant;
- date;
- commentaires;
- références aux preuves.

La génération de document est une **projection** des données métier. Le document généré ne devient pas la source de vérité.

## 9. Comparaison capacité ↔ backlog

Une capacité de #362 est de comparer le travail restant au temps réservé dans le Planning via une projection backend read-only du plan actif/approuvé. Une modification candidate ne doit pas déplacer silencieusement cette capacité avant approbation/activation.

Exemple :

```text
Capacité planifiée — semaine du 9 novembre
Jean      32 h
Marc      24 h
Alex      16 h
Total     72 h

Travail Delivery restant cette semaine
PLC       28 h
HMI       31 h
FAT       22 h
Total     81 h

⚠ écart : 9 h de travail de plus que la capacité planifiée
```

Cette projection doit rester informative au départ; elle ne transforme pas automatiquement les Stories en quarts.

## 10. Microsoft Planner / Teams

Issue de référence : #364.

L'intégration externe est une phase ultérieure, après stabilisation du modèle Delivery interne.

Principe :

```text
RessourcePlanner
= source de vérité Delivery

        ↓ synchronisation

Microsoft Planner / Teams
= surface de travail pratique
```

Mapping indicatif à valider contre l'API réelle :

```text
DeliveryPlan / WorkPackage → Plan Planner
Epic                       → Bucket
Story                      → Planner Task
```

Ce mapping n'est pas encore une décision d'architecture définitive.

RessourcePlanner doit conserver :

- identifiants externes stables;
- synchronisation idempotente;
- politique de conflits explicite;
- audit des changements;
- absence de suppression/inactivation silencieuse;
- indépendance du modèle métier face aux limitations de l'API Microsoft.

## 11. Source de vérité et responsabilités

### WorkPackage

Autoritaire pour :
- rattachement projet;
- contexte/portée projet;
- heures/dates de **référence de planification** selon le modèle actuel.

Les heures actuelles du WorkPackage ne constituent pas un budget approuvé/versionné. Un futur budget canonique devra rester distinct.

### Planning

Autoritaire pour :
- besoins de capacité;
- ressources/actifs;
- quarts/allocations réels;
- capacité réservée.

Un `Shift` n'est pas un actual de travail réalisé. Delivery lit la capacité du WorkPackage du plan actif/approuvé; il ne suit pas une modification candidate non activée.

### Delivery

Autoritaire pour :
- découpage technique;
- statut des Epics/Stories;
- estimation courante et estimation de référence;
- travail restant;
- progression technique;
- forecast technique;
- organisation Kanban/Sprint;
- concurrence propre du board.

Les responsables sont des `AppUser`. Les permissions Delivery sont distinctes des permissions Planning.

### Verification

Autoritaire pour :
- exigences de vérification;
- état FAT/SAT/commissioning;
- résultats et preuves.

### Microsoft Planner

Surface externe synchronisée; pas source de vérité primaire du modèle Delivery.

## 12. Non-objectifs

Cette direction ne vise pas :

- à reproduire Jira;
- à fournir un moteur de workflow générique;
- à transformer chaque Story en quart;
- à forcer Scrum partout;
- à créer immédiatement des microservices;
- à faire de Microsoft Planner la source de vérité;
- à déduire automatiquement une approbation de budget d'une réestimation technique;
- à considérer un document exporté comme la source de vérité.

## 13. Séquence produit envisagée

Cette vision vient **après la stabilisation du cœur planification main-d'œuvre + actifs**.

Ordre indicatif :

```text
socle workforce / WorkPackage stable
        ↓
actifs / véhicules (#291/#292)
        ↓
Delivery interne (#362)
        ↓
ASTRA-363 — analyse d'architecture Verification
        ↓
Verification / commissioning (#363)
        ↓
documents structurés
        ↓
Microsoft Planner / Teams (#364)
        ↓
projections avancées capacité ↔ backlog ↔ forecast
```

Le roadmap maître #55 conserve l'ordre opérationnel réel. Cette séquence future ne doit pas déplacer les tranches actives tant que le socle actuel n'est pas stabilisé.

## 14. Décisions Delivery stabilisées / questions restantes

L'analyse #362 a tranché avant implémentation :

- `DeliveryPlan` par WorkPackage, cycle `DRAFT → ACTIVE → ARCHIVED`;
- `DeliveryItem` à IDs stables avec `EPIC/STORY`;
- responsables liés à `AppUser`;
- statuts Story `BACKLOG/TODO/IN_PROGRESS/BLOCKED/DONE/CANCELLED`;
- progression pondérée par estimation de référence des Stories `DONE`;
- forecast séparé basé sur le travail restant;
- heures actuelles du WorkPackage = référence, pas budget approuvé;
- Shift = capacité réservée, pas actual;
- capacité Delivery issue du plan actif/approuvé;
- concurrence et permissions Delivery distinctes de Planning;
- sprints facultatifs.

Le découpage retenu est **362A → 362B → 362C → 362D → 362E → 362F**. Le roadmap #55 détermine quand #362 devient active.

Avant Verification, **ASTRA-363 est obligatoire** pour stabiliser l'architecture et le découpage avant développement :

- taxonomie FAT/SAT/commissioning;
- versionnement des exigences de test;
- règles de retest;
- modèle des preuves/fichiers;
- signatures/approbations éventuelles;
- format documentaire exigé par les clients.

Avant Planner :

- API Microsoft disponible;
- mapping réel Plan/Bucket/Task;
- permissions Graph;
- webhooks vs polling;
- politique de conflits;
- identité utilisateur Microsoft ↔ AppUser/Resource.

## 15. Références

- roadmap maître #55;
- #362 — Delivery : Epics, Stories, Kanban et progression des WorkPackages;
- #363 — Verification : FAT/SAT/commissioning, preuves et documentation;
- #364 — intégration Microsoft Planner/Teams;
- #13 — séparation autorisation approuvée / état opérationnel;
- #291/#292 — actifs réservables et qualifications;
- ADR-008 — frontière Delivery / Planning, progression technique et projection de capacité WorkPackage.

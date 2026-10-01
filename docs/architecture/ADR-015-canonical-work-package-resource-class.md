# ADR-015 — Classe canonique du WorkPackage distincte de la classification budgétaire de sa tâche ERP

Status: Accepted  
Date: 2026-09-30

## Context

L'issue #524 remplace, dans le parcours métier principal des WorkPackages, l'usage d'un code libre par une classe de ressource canonique sélectionnable.

RessourcePlanner possède déjà plusieurs concepts voisins qui ne doivent pas être confondus :

- `TaskCatalogEntry` représente la tâche ERP persistée et porte notamment les contrats budgétaires de #502;
- `TaskCatalogEntry.resource_class_code` est la classe effective calculée par les standards et overrides de #454 pour la tâche ERP;
- `ResourceClassConfig` est le référentiel canonique existant des classes workforce;
- `WorkPackage` porte le travail moyen terme, sa charge totale, sa répartition hebdomadaire et une version/CAS locale selon ADR-013;
- `RequestLine.required_resource_class` décrit la classe requise d'une demande;
- ADR-008 sépare Delivery de Planning et ADR-010 sépare classe technique et autorité d'approbation.

La classe de la tâche ERP constitue un bon point de départ lors de la création d'un WorkPackage, mais elle ne peut pas rester une dérivation dynamique. La synchronisation ERP et les règles #454 peuvent modifier ultérieurement `TaskCatalogEntry.resource_class_code`, alors que la qualification métier d'un WorkPackage existant doit rester stable tant qu'un utilisateur ne la modifie pas explicitement.

Le champ libre historique `WorkPackage.code` reste nécessaire pour la compatibilité mais ne représente pas cette qualification métier.

## Problem

Le système doit pouvoir répondre sans ambiguïté à deux questions différentes :

1. à quelle tâche ERP le WorkPackage est-il rattaché pour le budget et les contrats #502?
2. quelle est la qualification métier canonique du WorkPackage lui-même?

Une relation dérivée permanente du type :

```text
effective_class =
    WorkPackage.resource_class_code
    ?? TaskCatalogEntry.resource_class_code
```

rendrait la réponse à la deuxième question dépendante d'une donnée ERP mutable. Elle permettrait notamment qu'une synchronisation #454 requalifie silencieusement un WorkPackage existant.

## Decision

### 1. Relation canonique persistée

`WorkPackage` porte une référence canonique nullable vers le référentiel #454 :

```text
WorkPackage.task_catalog_item_id
    → TaskCatalogEntry.id

WorkPackage.resource_class_code
    → ResourceClassConfig.code
```

Le champ recommandé est :

```text
resource_class_code
```

avec les propriétés suivantes :

- type compatible avec `ResourceClassConfig.code`;
- clé étrangère vers `resource_class_configs.code`;
- nullable;
- aucune cascade destructive;
- concurrence gérée par `WorkPackage.version`;
- changements audités via `WorkPackageAudit`.

Aucun second concept ou référentiel `ResourceClass` n'est créé.

### 2. Source d'autorité

Après persistance, `WorkPackage.resource_class_code` est la source d'autorité de la qualification métier du WorkPackage.

`TaskCatalogEntry.resource_class_code` peut uniquement servir :

- de valeur initiale lors de la création;
- de suggestion lors d'un changement de tâche;
- d'information de comparaison ou de diagnostic.

Il n'est jamais un fallback dynamique après enregistrement.

Une synchronisation future du catalogue ERP ne réécrit donc pas automatiquement la classe des WorkPackages existants.

### 3. Tâche ERP et classe WorkPackage restent distinctes

Le modèle cible conserve deux relations indépendantes :

```text
WorkPackage
 ├─ task_catalog_item_id
 │    → rattachement ERP
 │    → budget ERP
 │    → contrats #502
 │
 └─ resource_class_code
      → qualification métier canonique du WorkPackage
```

Les autorités restent :

| Usage | Autorité |
|---|---|
| Qualification métier du WorkPackage | `WorkPackage.resource_class_code` |
| Rattachement ERP | `WorkPackage.task_catalog_item_id` |
| Budget ERP | `TaskCatalogEntry` |
| Heures budgétaires ERP | contrats #502 existants |
| Contexte WorkPackage dans Delivery | classe du WorkPackage |
| Classe requise d'une demande | contrat actuel de `RequestLine` |
| Routage d'approbation #276 | contrat actuel; inchangé |

### 4. Divergence permise

La divergence entre la classification de la tâche ERP et la classe du WorkPackage est permise.

Exemple :

```text
TaskCatalogEntry.resource_class_code = PROGRAMMEUR

WorkPackage.resource_class_code = INSTALLATEUR_AUTOMATISATION
```

Cette divergence n'est pas une erreur par défaut. Elle peut être exposée comme diagnostic informatif.

La classification de la tâche répond aux contrats ERP/budget/routage existants; la classe du WorkPackage décrit la qualification métier courante du WorkPackage.

### 5. Aucun fallback dynamique

Un WorkPackage sans classe reste sans classe :

```text
WorkPackage.resource_class_code = NULL
```

Le système ne remplace pas implicitement cette valeur par `TaskCatalogEntry.resource_class_code` lors d'une lecture.

Cette règle protège la stabilité historique de la qualification WorkPackage lorsqu'une synchronisation #454 modifie une tâche ERP.

### 6. Nullabilité et mutations

`resource_class_code` reste nullable.

Comportement attendu :

```text
classe fournie
→ valider ResourceClassConfig
→ persister

classe omise à la création
→ initialisation possible depuis la classe persistée de la tâche
→ sinon NULL

classe omise au PATCH
→ conserver la valeur existante

classe explicitement mise à NULL
→ déqualification explicite permise
```

Un WorkPackage historique sans classe reste valide et lisible.

#524 n'introduit aucune règle générale imposant une classe à tous les WorkPackages.

### 7. Classes inactives

Une classe inactive ne peut pas être choisie comme nouvelle valeur.

Lorsqu'un WorkPackage référence déjà une classe qui devient ensuite inactive :

- la référence est conservée;
- le WorkPackage reste lisible;
- l'état inactif de la classe est exposé;
- la valeur n'est pas effacée automatiquement;
- une mutation sans rapport avec la classe n'est pas refusée uniquement à cause de cette désactivation.

La clé étrangère protège la suppression physique d'une classe encore référencée.

### 8. Création et initialisation depuis la tâche ERP

Lorsqu'aucune classe n'est fournie explicitement à la création :

```text
WorkPackage.task_catalog_item_id
        ↓
TaskCatalogEntry.resource_class_code
        ↓
ResourceClassConfig existante
        ↓
initialisation possible de WorkPackage.resource_class_code
```

Cette copie est une initialisation ponctuelle.

Elle ne crée aucune relation dérivée permanente.

Si aucune classe canonique exploitable n'est disponible, `resource_class_code` reste `NULL`. Aucune classe n'est fabriquée.

### 9. Changement de tâche ERP

Les restrictions existantes de #502 sur la modification de `task_catalog_item_id` restent inchangées.

Lorsqu'un changement de tâche ERP est autorisé :

- la classe WorkPackage actuelle est conservée par défaut;
- la classe de la nouvelle tâche devient une suggestion;
- aucun remplacement automatique de `WorkPackage.resource_class_code` n'a lieu;
- tâche et classe peuvent être modifiées explicitement dans la même mutation protégée par le CAS `WorkPackage.version`.

Un changement de tâche ou de classe ne redistribue pas automatiquement la charge hebdomadaire du WorkPackage.

### 10. Provenance de la classe

Aucun champ persistant du type :

```text
resource_class_origin = DERIVED | OVERRIDE
```

n'est introduit.

L'état courant est :

```text
WorkPackage.resource_class_code
```

L'historique de la décision et des changements appartient à `WorkPackageAudit`.

### 11. Migration et backfill

L'implémentation future utilise une migration Alembic additive. La baseline existante n'est pas modifiée.

Pour chaque WorkPackage historique, le backfill peut copier la classe de sa tâche uniquement lorsque la relation est démontrable.

Copier uniquement lorsque :

- `task_catalog_item_id` existe;
- la tâche existe;
- la tâche correspond correctement au projet du WorkPackage;
- `TaskCatalogEntry.resource_class_code` est renseigné;
- la classe référencée existe réellement dans `ResourceClassConfig`.

Une classe existante mais inactive peut être copiée afin de préserver la qualification historique disponible.

Laisser `NULL` lorsque :

- aucune tâche ERP n'est liée;
- la tâche n'a pas de classe;
- la classe référencée n'existe pas;
- la relation projet/tâche est incohérente;
- les données sont ambiguës.

La migration ne doit jamais :

- déduire la classe depuis `WorkPackage.code`;
- choisir une classe selon une majorité de `RequestLine`;
- déduire la classe depuis les ressources affectées;
- recalculer les standards #454;
- modifier budget, heures, répartitions hebdomadaires, charge ou snapshots existants.

## Impacts sur les domaines existants

### Moyen terme / #502 / ADR-013

Cette décision complète ADR-013 sans le redessiner.

Le budget demeure porté par la tâche ERP.

La projection #502 conserve ses règles existantes pour :

- budget;
- `planned_hours`;
- charges hebdomadaires;
- capacité workforce globale;
- statuts;
- règles de double comptage.

La projection Moyen terme peut enrichir les lignes WorkPackage avec leur qualification métier.

Un WorkPackage sans classe continue de contribuer aux agrégats existants.

#524 n'introduit pas de nouvelle ventilation charge/capacité par classe. Une projection future pourrait utiliser :

```text
charge WorkPackage
→ classe WorkPackage

capacité disponible
→ classes des ressources
```

mais ce calcul est hors périmètre.

### Delivery / #362 / ADR-008

La classe WorkPackage peut être exposée comme contexte dans les projections Delivery.

Elle ne modifie pas :

- Epic;
- Story;
- workflow;
- transitions;
- permissions;
- progression;
- estimation ou restant;
- provenance de capacité approuvée;
- `delivery_version`.

ADR-008 reste autoritaire.

La classe WorkPackage est une qualification métier courante; elle n'accorde aucune autorisation Planning ou Delivery.

### Demandes / RequestLine / #276 / ADR-010

#524 n'introduit aucune propagation automatique :

```text
WorkPackage.resource_class_code
→ RequestLine.required_resource_class
```

Les autorités existantes restent séparées :

```text
RequestLine.required_resource_class
→ besoin demandé

TaskCatalogEntry.resource_class_code
→ classification utilisée par le routage actuel lorsque applicable

Resource / Shift
→ affectation réelle
```

Le routage #276 et le quorum d'ADR-010 restent inchangés.

Un futur préremplissage de `RequestLine.required_resource_class` depuis un WorkPackage nécessite une décision séparée.

## Devenir de WorkPackage.code

Le champ libre `WorkPackage.code` est conservé :

- en base;
- dans les contrats API existants;
- dans l'audit;
- pour la compatibilité historique;
- dans les lectures et recherches qui en ont encore besoin.

Il n'est cependant plus une donnée à mettre en avant dans le parcours principal de saisie WorkPackage.

Il :

- ne devient pas la classe de ressource;
- ne participe pas au backfill de `resource_class_code`;
- n'est pas supprimé par une migration destructive dans #524.

Les anciens codes restent lisibles et recherchables là où ils le sont actuellement.

## Consequences

### Positive

- la qualification métier d'un WorkPackage devient explicite, persistée et stable;
- une synchronisation ERP ne requalifie pas silencieusement des WorkPackages existants;
- le budget ERP et la qualification du WorkPackage restent séparés;
- le référentiel `ResourceClassConfig` existant est réutilisé sans duplication;
- les WorkPackages historiques restent compatibles grâce à la nullabilité et au backfill prudent;
- Delivery et Moyen terme peuvent exposer la classe comme contexte sans changer leurs autorités;
- l'audit existant suffit pour retracer les changements sans introduire une provenance supplémentaire.

### Trade-offs / negative

- une nouvelle clé étrangère nullable doit être maintenue;
- la classe d'un WorkPackage peut légitimement diverger de celle de sa tâche ERP, ce qui exige un affichage clair;
- une classe historique inactive doit rester représentable;
- certaines créations sans classe explicite nécessitent une initialisation déterministe depuis la tâche;
- les consommateurs ne doivent pas recréer un fallback dynamique de leur côté.

## Alternatives considered

### Dérivation permanente depuis la tâche ERP

Rejetée.

- la classification #454 est mutable;
- la synchronisation peut modifier `TaskCatalogEntry.resource_class_code`;
- toutes les tâches historiques ne garantissent pas une classe;
- elle couplerait qualification WorkPackage et classification budgétaire.

### Dérivation depuis demandes, ressources ou Delivery

Rejetée.

- ces relations peuvent être absentes;
- plusieurs valeurs peuvent coexister;
- aucune source unique n'existe;
- cela introduirait une nouvelle heuristique métier.

### Champ de provenance DERIVED/OVERRIDE

Non retenu maintenant.

- complexité sans besoin actuel;
- l'état courant dans `WorkPackage.resource_class_code` et l'audit existant suffisent.

### Réutiliser WorkPackage.code comme classe

Rejeté.

`code` est un champ libre historique et ne fournit ni identité référentielle, ni validation, ni sémantique stable compatible avec `ResourceClassConfig`.

## Implementation notes

#524 est débloquée en deux tranches après acceptation de cet ADR :

```text
ASTRA-524
   ↓
524A
   ↓
524B
```

### 524A — Persistance et contrats backend

Périmètre prévu :

- migration additive;
- `WorkPackage.resource_class_code`;
- clé étrangère canonique;
- backfill;
- commandes et schémas API;
- repository/service;
- validation;
- CAS via `WorkPackage.version`;
- audit `WorkPackageAudit`;
- compatibilité;
- projections WorkPackage;
- enrichissement Moyen terme;
- enrichissement Delivery;
- diagnostics.

Ne pas modifier :

- budgets #502;
- Planning;
- #276;
- `RequestLine.required_resource_class`.

### 524B — UX et acceptation transversale

Périmètre prévu :

- sélecteur canonique de `ResourceClassConfig`;
- retrait de `WorkPackage.code` de la saisie principale;
- conservation des anciens codes;
- suggestion depuis la tâche ERP;
- gestion d'une classe inactive historique;
- affichage qualification/diagnostics;
- Gantt;
- contexte Delivery;
- contrats TypeScript;
- validation navigateur/E2E.

## References

- #524 — WorkPackage — classe de ressource canonique et simplification du code libre
- #454 — classes de ressources, coûts moyens et standards TaskCD → classe
- #502 — Moyen terme : Gantt budget tâches ERP, WorkPackages et capacité hebdomadaire
- #362 — Delivery : Epics, Stories, Kanban et progression des WorkPackages
- #276 — multi-approbation par ligne
- ADR-008 — Delivery distinct du Planning et projection de capacité WorkPackage
- ADR-010 — Périmètres métier et quorum de multi-approbation par ligne
- ADR-013 — Charge hebdomadaire WorkPackage et séparation budget ERP / Moyen terme / Planning / Delivery

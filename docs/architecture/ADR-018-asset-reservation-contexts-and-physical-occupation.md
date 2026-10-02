# ADR-018 — Contextes de réservation d’actifs, occupation réelle et opérateurs explicites

Status: Accepted  
Date: 2026-10-02

## Context

ADR-007 a établi le modèle durable des actifs réservables : les ressources humaines et les actifs restent des modèles persistants distincts, et toute réservation physique suit la chaîne :

```text
AssetRequirement
    ↓
AssetAllocation
    ↓
Asset
```

ADR-016 a ensuite étendu ce modèle pour permettre une affectation ad hoc appartenant à un vrai `Shift`, sans rendre `AssetAllocation.asset_requirement_id` optionnel et sans créer une seconde autorité de réservation.

#575 ajoute trois besoins complémentaires :

1. réserver directement un actif dans le contexte d’un projet;
2. réserver un actif dans le contexte d’un `ResourceRequirement` humain, avec opérateur explicitement choisi;
3. attribuer directement un actif à une ressource pendant une période, sans faux `Shift`, faux segment ou fausse demande.

En parallèle, le modèle existant doit distinguer explicitement trois dimensions qui ont historiquement été faciles à confondre :

- la **fenêtre autorisée** d’un besoin;
- le **budget d’usage** tel que `usage_hours`;
- les **dates physiques réelles** d’occupation de l’actif.

ASTRA-575 a conclu que ces besoins dépassent ADR-016, qui doit rester spécifique au cycle de vie `SHIFT_AD_HOC`. La nouvelle décision doit conserver les autorités déjà stabilisées par ADR-006, ADR-007, ADR-009, ADR-011 et ADR-016, sans introduire de propriétaire polymorphe, de nouvelle table de réservation physique ni de nouvelle version de concurrence propre aux actifs.

## Decision

### 1. `AssetRequirement` reste obligatoire

Toute réservation physique continue de respecter :

```text
AssetRequirement
    ↓
AssetAllocation
    ↓
Asset
```

`AssetRequirement` devient le support commun de :

- la provenance;
- le contexte propriétaire;
- la fenêtre applicable;
- les contraintes métier de réservation.

Il ne signifie pas nécessairement « besoin issu d’une demande approuvée ».

ADR-016 a déjà établi ce principe pour `SHIFT_AD_HOC`.

Aucune `AssetAllocation` orpheline n’est introduite.

### 2. `AssetAllocation` demeure l’unique autorité physique

`AssetAllocation` reste l’autorité de la réservation physique réelle.

Aucune nouvelle table de réservation physique n’est créée par :

- projet;
- ressource;
- segment;
- `Shift`;
- demande.

La disponibilité globale, l’occupation et le double booking continuent de partir des `AssetAllocation`.

### 3. Cinq origines explicites

`AssetRequirementOrigin` doit couvrir conceptuellement :

```text
REQUEST
SHIFT_AD_HOC
PROJECT_DIRECT
SEGMENT
RESOURCE_PERIOD
```

Le sens de chaque origine est :

- `REQUEST` : besoin ASSET provenant du workflow Demandes;
- `SHIFT_AD_HOC` : propriété ad hoc d’un `Shift` selon ADR-016;
- `PROJECT_DIRECT` : réservation directe dans le contexte d’un projet, sans demande ni `Shift` artificiel;
- `SEGMENT` : réservation dans le contexte d’un `ResourceRequirement` humain, avec opérateur explicitement choisi;
- `RESOURCE_PERIOD` : attribution autonome d’un actif à une ressource pour une période.

Les données historiques `REQUEST` et `SHIFT_AD_HOC` conservent leur provenance existante et ne sont pas reclassées.

### 4. Provenance par clés étrangères explicites

La provenance utilise de vraies relations SQL.

En plus des relations historiques, la cible conceptuelle prévoit :

```text
AssetRequirement.resource_requirement_id
    → ResourceRequirement.id

AssetRequirement.context_resource_id
    → Resource.id
```

et réutilise les relations existantes comme :

```text
project_id
shift_id
```

selon l’origine.

Un couple polymorphe :

```text
owner_type
owner_id
```

n’est pas adopté comme substitut à des clés étrangères réelles.

### 5. Propriétaire et opérateur sont distincts

La matrice de propriété est :

| Origine | Contexte propriétaire | Opérateur |
|---|---|---|
| `REQUEST` | entrée ASSET approuvée | facultatif sur `AssetAllocation` |
| `SHIFT_AD_HOC` | `Shift` | `Shift.resource_id`, synchronisé atomiquement |
| `PROJECT_DIRECT` | `Project` | facultatif |
| `SEGMENT` | `ResourceRequirement` | explicite et obligatoire |
| `RESOURCE_PERIOD` | `context_resource_id` | explicite et obligatoire |

Le propriétaire du requirement et l’opérateur réel de l’allocation sont deux concepts distincts.

En particulier, ADR-001 reste applicable :

```text
ResourceRequirement.assigned_resource_id
≠
opérateur réel de l’actif
```

Pour une origine `SEGMENT`, l’opérateur n’est jamais inféré automatiquement depuis la cible automatique du segment.

### 6. Les dates réelles restent sur `AssetAllocation`

L’autorité physique des dates d’occupation reste :

```text
AssetAllocation.start_date
AssetAllocation.end_date
```

Aucun doublon `actual_start_date` / `actual_end_date` n’est introduit tant que ces champs remplissent déjà ce rôle.

Le modèle distingue donc :

```text
fenêtre du requirement
≠
dates physiques de l’allocation
```

Règles par origine :

- `REQUEST` : les dates de l’allocation sont un sous-intervalle explicite de la fenêtre autorisée;
- `SHIFT_AD_HOC` : les dates sont dérivées du jour du `Shift`;
- `SEGMENT` : les dates sont un sous-intervalle explicite inclus dans la fenêtre du segment;
- `PROJECT_DIRECT` : la période de réservation est explicite;
- `RESOURCE_PERIOD` : la période d’attribution est explicite.

### 7. Une extension de fenêtre n’étend jamais automatiquement la réservation

La règle durable est :

```text
extension de fenêtre
≠
extension de réservation
```

Si une fenêtre autorisée s’élargit, une `AssetAllocation` existante garde ses dates physiques.

Changer l’unité sans nouvelles dates conserve également les dates physiques existantes.

Une réduction de fenêtre qui exclurait une réservation physique protégée doit être refusée plutôt que déplacer, raccourcir ou supprimer silencieusement l’allocation.

### 8. Budget d’usage et occupation sont indépendants

`usage_hours` reste distinct de la durée physique d’occupation.

Des états comme les suivants sont valides :

```text
fenêtre = 7 jours
usage_hours = 8 h
allocation = 1 jour
```

ou :

```text
fenêtre = 7 jours
usage_hours = 8 h
allocation = 3 jours
```

si les dates sont autorisées et que l’actif est disponible.

Le domaine ne convertit pas automatiquement une occupation de N jours en N × 8 heures.

`usage_hours` n’est pas un compteur d’usage réel.

### 9. Réservation `PROJECT_DIRECT`

Le modèle cible est :

```text
Project
  ↓
AssetRequirement[PROJECT_DIRECT]
  ↓
AssetAllocation
```

avec :

- projet obligatoire;
- opérateur facultatif;
- aucune demande;
- aucun segment;
- aucun `Shift`;
- dates explicites.

Le requirement support et l’allocation sont créés atomiquement.

Lorsque la réservation directe est entièrement libérée, son requirement support peut être supprimé dans la même transaction. L’historique demeure dans l’audit.

### 10. Réservation `SEGMENT`

Le modèle cible est :

```text
ResourceRequirement
        ↓ contexte
AssetRequirement[SEGMENT]
        ↓
AssetAllocation
        ↓
operator_resource_id explicite
```

L’opérateur est obligatoire et explicitement choisi.

Un même segment peut porter plusieurs réservations indépendantes avec des opérateurs différents.

La création ultérieure de `Shift` ne transfère pas la propriété des réservations `SEGMENT`.

La cible automatique du segment n’est jamais utilisée comme opérateur implicite.

### 11. Réservation `RESOURCE_PERIOD`

Le modèle cible est :

```text
Resource
  ↓ contexte
AssetRequirement[RESOURCE_PERIOD]
  ↓
AssetAllocation
```

avec :

- ressource de contexte obligatoire;
- opérateur obligatoire;
- dates explicites;
- projet facultatif;
- aucune demande;
- aucun `Shift`;
- aucun segment.

Dans la portée de #575 :

```text
context_resource_id
==
operator_resource_id
```

après chaque commande réussie.

Les deux champs restent néanmoins conceptuellement distincts : le premier représente le contexte/bénéficiaire; le second reste l’opérateur utilisé par la qualification et les projections.

### 12. `project_id` n’est nullable que lorsque l’origine le permet

`AssetRequirement.project_id` peut devenir nullable uniquement afin de supporter `RESOURCE_PERIOD` sans projet.

La politique est :

| Origine | Projet |
|---|---|
| `REQUEST` | obligatoire |
| `SHIFT_AD_HOC` | obligatoire |
| `PROJECT_DIRECT` | obligatoire |
| `SEGMENT` | obligatoire et cohérent avec le segment |
| `RESOURCE_PERIOD` | facultatif |

Aucun projet artificiel n’est créé pour satisfaire le schéma.

### 13. Qualification : conserver #292 et étendre la preuve de contexte

Les règles de #292 restent applicables :

- ressource active;
- compétences canoniques;
- validation backend;
- comportement fail-closed.

La preuve de contexte varie selon l’origine :

- `REQUEST` : preuve historique fondée sur le chevauchement humain projet/demande;
- `SHIFT_AD_HOC` : exactement le `Shift` propriétaire;
- `PROJECT_DIRECT` : désignation explicite de l’opérateur dans le contexte du projet;
- `SEGMENT` : désignation explicite de l’opérateur dans le contexte du segment;
- `RESOURCE_PERIOD` : attribution explicite à cette ressource pour cette période.

Les nouveaux parcours ne nécessitent pas un faux `Shift`.

Un opérateur peut rester absent pour `REQUEST` et `PROJECT_DIRECT`. Si le type d’actif exige une compétence, l’état `MISSING_OPERATOR` peut être projeté sans libérer la réservation physique.

L’opérateur reste obligatoire pour `SEGMENT` et `RESOURCE_PERIOD`.

### 14. ADR-016 reste l’autorité du cycle `SHIFT_AD_HOC`

ADR-016 n’est ni remplacé ni réinterprété.

Ses règles restent applicables :

- move du `Shift` → actif déplacé atomiquement;
- split → actif conservé sur la source;
- duplicate → actif non copié;
- retour AUTO refusé tant que l’affectation ad hoc demeure;
- delete → nettoyage allocation → requirement → `Shift`;
- release → `Shift` humain conservé.

Les nouvelles origines ne sont jamais traitées comme `SHIFT_AD_HOC`.

### 15. Cycle de vie protégé des réservations `SEGMENT`

La politique durable est :

| Mutation du segment | Réservation `SEGMENT` |
|---|---|
| modification descriptive | conservée |
| changement cible automatique | conservée |
| création/suppression/move des `Shift` | conservée |
| extension de fenêtre | dates physiques inchangées |
| réduction contenant l’allocation | conservée |
| réduction excluant l’allocation | refus |
| réapprobation compatible | conservée |
| remplacement/retrait/changement projet incompatible | refus tant que réservation non résolue |
| annulation logique ordinaire | refus si dépendance matérielle active |
| acceptation #399 | libération explicite dans le périmètre |

Cette politique protège les réservations physiques contre un rebuild ou une mutation logique destructive.

### 16. Interaction avec #399

Lorsqu’une annulation de demande est acceptée, les réservations `SEGMENT` réellement possédées par les segments du périmètre annulé sont libérées explicitement.

La transaction supprime :

```text
AssetAllocation SEGMENT ciblée
+
AssetRequirement SEGMENT support
```

Elle ne supprime pas `PROJECT_DIRECT` ni `RESOURCE_PERIOD` uniquement parce que ces réservations partagent le même projet.

La durée physique de l’allocation ne change pas la propriété d’annulation : une réservation `REQUEST` d’un seul jour reste rattachée à sa demande.

### 17. Rebuild et réapprobation ne suppriment jamais silencieusement une réservation

La politique est :

| Origine | Rebuild / réapprobation |
|---|---|
| `REQUEST` | synchronise le besoin; allocation compatible préservée |
| `SHIFT_AD_HOC` | cycle de vie propriétaire du `Shift` |
| `PROJECT_DIRECT` | hors rebuild |
| `SEGMENT` | dépendance vérifiée, aucune réaffectation automatique |
| `RESOURCE_PERIOD` | hors rebuild |

Une réservation physique devenue incompatible est soit préservée, soit la mutation est bloquée explicitement.

Elle n’est jamais supprimée silencieusement.

### 18. Même concurrence globale `planning_version`

Aucune version `AssetAllocation.version` ni `AssetRequirement.version` n’est ajoutée.

Toutes les nouvelles mutations matérielles réutilisent ADR-006 et participent au même CAS global avant leurs lectures décisionnelles.

La séquence conceptuelle reste :

```text
auth / permissions
→ replay idempotent éventuel
→ CAS planning_version
→ relecture des objets
→ validation contexte
→ validation dates
→ validation qualification
→ validation disponibilité
→ mutation
→ audit
→ reçu idempotent
→ commit
```

### 19. Double booking et indisponibilités restent globaux

Toutes les allocations, quelle que soit leur origine, participent au même contrôle global d’intervalles.

Aucune origine ne bénéficie d’une exemption de disponibilité.

Toute nouvelle réservation ou extension doit vérifier les indisponibilités chevauchantes.

Si une indisponibilité est créée après une réservation existante, celle-ci n’est pas supprimée silencieusement; le conflit est projeté explicitement.

### 20. Complétion d’un besoin `REQUEST`

La complétion matérielle d’un besoin ASSET ne doit plus exiger une allocation couvrant toute la fenêtre du requirement.

Une allocation explicitement choisie et compatible pour le slot constitue la couverture matérielle pertinente.

Cette couverture ne prouve pas que le budget `usage_hours` a réellement été consommé.

### 21. Provenance navigable dans les read models

Les projections backend exposent une provenance explicite permettant à React de naviguer sans heuristique.

Conceptuellement :

```text
origin
request_id?
request_number?
request_line_id?
project_id?
project_number?
resource_requirement_id?
segment_reference?
shift_id?
context_resource_id?
```

Les valeurs absentes sont `null`.

React ne reconstruit pas la provenance depuis un nom, les dates, l’opérateur ou le projet affiché.

### 22. Scope personnel

Pour une réservation avec projet, le scope projet canonique existant est utilisé.

Pour un `RESOURCE_PERIOD` sans projet, l’entrée est visible dans `mine` uniquement lorsque :

```text
context_resource_id
==
ressource personnelle résolue canoniquement
```

L’occupation physique reste malgré tout comptée globalement pour la disponibilité, même lorsqu’elle est hors périmètre d’affichage, conformément à #561.

### 23. API : commandes explicites par intention

L’API ne doit pas exposer un endpoint générique acceptant librement un `origin` et un ensemble arbitraire de clés étrangères.

Chaque parcours possède une commande métier explicite, réutilisant un moteur commun de validation.

Le serveur dérive ou valide la provenance persistée.

Les surfaces restent conceptuellement distinctes :

```text
REQUEST
→ parcours requirement/réservation existant

SHIFT_AD_HOC
→ parcours shift/assignment existant

PROJECT_DIRECT
→ commande projet direct

SEGMENT
→ commande segment

RESOURCE_PERIOD
→ commande ressource-période
```

L’ADR ne fige pas inutilement les URL exactes.

### 24. Permissions

Les nouvelles commandes de réservation appartiennent au domaine Planning et requièrent `MANAGE_PLANNING`.

Elles ne doivent pas tomber accidentellement sur le fallback catalogue `MANAGE_RESOURCES`.

Les permissions supplémentaires déjà requises par des workflows destructifs comme #399 restent applicables.

### 25. Audit

Les mutations réutilisent :

```text
PlanningChangeHistory
SqlPlanningAuditJournal
```

Lorsque pertinent, l’audit rend explicites :

- origine;
- contextes;
- IDs requirement/allocation;
- ancien/nouvel actif;
- ancien/nouvel opérateur;
- anciennes/nouvelles dates;
- ancien/nouveau projet;
- bénéficiaire;
- références d’autorisation;
- version Planning;
- corrélation idempotente.

Aucune seconde autorité d’audit des réservations n’est créée.

### 26. Migration et compatibilité

La migration future est additive.

La cible conceptuelle comprend notamment :

```text
resource_requirement_id nullable FK
context_resource_id nullable FK
project_id nullable conditionnellement
CHECK origins/provenance étendus
indexes segment/resource
```

La migration doit conserver :

- données `REQUEST` existantes;
- données `SHIFT_AD_HOC` existantes;
- dates historiques;
- opérateurs;
- IDs;
- approbations;
- empreintes existantes.

Aucune reclassification historique n’est effectuée.

Aucune réservation historiquement plus longue que la nouvelle interprétation attendue n’est raccourcie automatiquement.

Un downgrade ne doit pas supprimer les nouvelles colonnes lorsqu’il existe encore des réservations des nouvelles origines. Une fois ces données créées, l’ancien binaire n’est pas garanti capable de les interpréter.

## Alternatives considered

### Allocation sans `AssetRequirement`

Rejetée.

Elle introduirait des allocations orphelines, dupliquerait les contrats de provenance/contexte et fragiliserait projections, audit et nettoyage.

### Tables de réservation distinctes par mode

Rejetées.

Elles créeraient plusieurs autorités physiques, compliqueraient le double booking et casseraient le calcul unifié de disponibilité.

### Propriétaire polymorphe `owner_type + owner_id`

Rejeté.

Cette approche réduit l’intégrité référentielle et rend migrations, requêtes et diagnostics plus difficiles alors que des clés étrangères explicites sont disponibles.

### Faux `Shift`, faux segment ou fausse demande

Rejetés.

Ces objets déformeraient les concepts métier uniquement afin de satisfaire le schéma.

### Conversion heures ↔ jours

Rejetée.

Budget d’usage et occupation physique représentent deux dimensions différentes.

### Réécrire ADR-016

Rejeté.

ADR-016 reste correct et autoritaire pour `SHIFT_AD_HOC`. ADR-018 étend le modèle commun sans remplacer ce cycle de vie.

## Consequences

### Positive

- toutes les réservations physiques restent dans une seule autorité;
- les trois nouveaux contextes métier peuvent être représentés sans faux objets;
- la provenance reste navigable et vérifiable par SQL;
- fenêtre autorisée, budget d’usage et occupation réelle deviennent explicitement séparés;
- les opérateurs sont déterministes dans les contextes qui l’exigent;
- la qualification, le double booking, l’idempotence, l’audit et la concurrence restent uniformes;
- les réservations `SEGMENT` sont protégées contre les suppressions implicites;
- les parcours historiques `REQUEST` et `SHIFT_AD_HOC` restent compatibles.

### Trade-offs / negative

- `AssetRequirement` devient un modèle à cinq formes avec contraintes conditionnelles plus riches;
- `project_id` doit devenir nullable de façon contrôlée;
- plusieurs nouvelles clés étrangères et index seront nécessaires;
- les read models doivent exposer davantage de provenance explicite;
- les mutations de segments et d’annulation doivent détecter des dépendances matérielles avant de modifier leur contexte;
- les garanties de contraintes et de concurrence devront être validées sur SQL Server conformément à ADR-011;
- un ancien binaire peut ne pas comprendre les nouvelles origines une fois celles-ci créées.

## Implementation notes

L’acceptation de cet ADR ferme le préalable documentaire d’ASTRA-575.

Le découpage DEV retenu reste :

```text
575A — persistance et lecteurs compatibles
575B — réservation canonique et dates REQUEST
575C — projet direct et ressource-période
575D — réservation SEGMENT et protections de cycle de vie
575E — intégration transverse et acceptation
```

Cet ADR ne constitue aucune implémentation de 575A.

Les détails exacts de migration, contraintes, index, routes HTTP et DTO appartiennent aux tranches DEV tant qu’ils respectent les invariants ci-dessus.

## References

- #55 — roadmap maître / `COCKPIT_PIPELINE_V1`
- #575 — Planning actifs — trois modes de réservation, dates réelles d’occupation et liaison ressource
- #13 — périodes et enveloppe approuvée
- #291 — ressources réservables non humaines
- #292 — qualification véhicule/équipement ↔ compétence
- #331 — séparation besoin/cible et ressources réelles
- #332 — partage et duplication atomiques de quarts
- #333 — extension de fenêtre et déplacement
- #399 — annulation et résolution coordonnateur
- #496 — UX actifs et visibilité sur les ressources
- #538 — Quick Shift et affectation d’actif
- #560 — affectation ad hoc depuis un `Shift`
- #561 — occupation détaillée et périmètre personnel
- ADR-001 — Séparer la cible automatique du besoin de l’affectation réelle des quarts
- ADR-003 — Révision approuvée immuable comme preuve d’autorisation
- ADR-004 — Séparer proposition candidate, autorisation approuvée et plan actif
- ADR-006 — Révision globale des mutations de planning
- ADR-007 — Ressources réservables non humaines et occupation Planning commune
- ADR-009 — Intention d’annulation persistante
- ADR-011 — SQL Server comme base de référence d’exploitation
- ADR-016 — Affectation d’actifs ad hoc appartenant à un quart

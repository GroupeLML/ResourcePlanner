# Architecture RessourcePlanner

Ce dossier regroupe les décisions d'architecture durables de RessourcePlanner.

L'objectif n'est pas de dupliquer toute la documentation technique du dépôt, mais de rendre faciles à retrouver :

- l'architecture actuelle;
- les décisions structurantes;
- les raisons derrière ces décisions;
- les conséquences importantes pour les développements futurs.

Les conversations ChatGPT, analyses ponctuelles et discussions de PR peuvent aider à prendre une décision, mais une décision d'architecture durable ne devrait pas rester uniquement dans une conversation.

---

## 1. Documentation d'architecture existante

Plusieurs documents existants décrivent déjà des parties importantes de l'architecture.

### Runtime Web V2

Voir :

- `../REACT_V2_DEV.md`
- `../../PLANNING_ENGINE_CUTOVER.md`

Principes actuels importants :

```text
React
  ↓
FastAPI
  ↓
Application / Domain
  ↓
Infrastructure
  ↓
SQLAlchemy / base de données / intégrations externes
```

Le backend Python reste autoritaire pour les règles métier de planification et de capacité.

### Demandes V2

Voir :

- `../DEMANDS_V2_ARCHITECTURE.md`
- `ADR-001-separate-requirement-target-from-shift-assignment.md`
- `ADR-002-request-line-periods-and-identities.md`
- `ADR-003-immutable-approved-authorization.md`
- `ADR-004-candidate-approval-and-active-plan.md`
- `ADR-005-canonical-demand-requester-identity.md`
- `ADR-006-global-planning-mutation-version.md`
- `ADR-007-reservable-non-human-resources.md`
- `ADR-008-delivery-planning-boundary.md`
- `ADR-009-persistent-cancellation-intent.md`
- `ADR-010-line-approval-scopes-and-quorum.md`
- `ADR-011-sql-server-authoritative-database.md`
- `ADR-012-preprovision-app-users-before-oidc-link.md`
- `ADR-013-work-package-weekly-load-and-medium-term-boundaries.md`
- `ADR-014-production-break-glass-administrator.md`
- `ADR-015-canonical-work-package-resource-class.md`
- `ADR-016-shift-owned-ad-hoc-asset-assignment.md`
- `ADR-017-erp-project-manager-and-rp-co-managers.md`
- `ADR-018-asset-reservation-contexts-and-physical-occupation.md`
- `ADR-019-work-package-load-intervals-and-derived-lifecycle.md`
- `ADR-020-operational-responsibility-hierarchy-and-approved-context.md`
- `ADR-021-class-scoped-holiday-availability-rules.md`
- `ADR-022-operational-planning-window-overrides.md`

Chaîne métier actuelle :

```text
Project
└── WorkforceRequest
    └── RequestLine
        └── ResourceRequirement
            └── Shift
```

`WorkPackage` est une référence de contexte/portée optionnelle, notamment portée par `RequestLine` pour les demandes multi-lignes. Les périodes appartiennent métier à la ligne. Le besoin/budget, la cible automatique et les ressources réellement affectées restent des concepts distincts.

Depuis #13, la demande candidate, la révision approuvée immuable et le plan actif sont également séparés. Depuis #328, le demandeur canonique est distinct de l'acteur authentifié, de la ressource planifiable et des contacts métier. #329 compose ces éléments dans une projection de détail backend sans nouvel agrégat persistant.

### Concurrence des mutations de planning

Voir :

- `ADR-006-global-planning-mutation-version.md`

Tant que `rebuild()` reste global, les mutations concurrentes pertinentes du planning participent à une révision persistante globale acquise par CAS SQL **avant** leurs lectures décisionnelles. Cette garde protège la cohérence transactionnelle des opérations composites comme #332; elle ne remplace pas les versions métier plus locales lorsqu'elles portent une sémantique distincte.


### Disponibilité et dérogation de fenêtre Planning

Voir :

- `ADR-021-class-scoped-holiday-availability-rules.md`
- `ADR-022-operational-planning-window-overrides.md`

ADR-021 fixe la sémantique des fériés ciblés par classes sans modifier les fériés globaux ou individuels historiques. ADR-022 introduit une dérogation opérationnelle persistante de fenêtre, distincte de l'approbation de demande et du consentement hors horaire, tout en conservant les contrats de concurrence et de quorum existants.

### Ressources réservables non humaines

Voir :

- `ADR-007-reservable-non-human-resources.md`
- `ADR-016-shift-owned-ad-hoc-asset-assignment.md`
- `ADR-018-asset-reservation-contexts-and-physical-occupation.md`

ADR-007 est la décision structurante de #291 : les modèles humains et actifs restent distincts, toute réservation physique passe par `AssetRequirement → AssetAllocation`, et la disponibilité physique est unifiée sur les allocations.

ADR-016 étend ce modèle pour `SHIFT_AD_HOC` : un `AssetRequirement` peut appartenir explicitement à un vrai `Shift`, tandis que `AssetAllocation` reste l’unique autorité physique et que le cycle de vie move/split/duplicate/release/delete demeure atomique avec le quart propriétaire.

ADR-018 étend le même modèle à cinq origines explicites (`REQUEST`, `SHIFT_AD_HOC`, `PROJECT_DIRECT`, `SEGMENT`, `RESOURCE_PERIOD`) avec des FK de provenance réelles. Il fixe `AssetAllocation.start_date/end_date` comme dates d’occupation physique, sépare fenêtre autorisée, budget `usage_hours` et occupation, conserve `operator_resource_id` comme autorité opérateur et réutilise le `planning_version` global sans version Asset parallèle.

### Authentification et autorisation

Voir :

- `../AUTH_RBAC.md`
- `../OIDC_ACUMATICA_VALIDATION.md`
- `../IDENTITY_PREPROVISIONING.md`
- `ADR-012-preprovision-app-users-before-oidc-link.md`

ADR-012 fixe la cible de pré-provisionnement : `AppUser` existe avant le premier login OIDC, la paire `(issuer, subject)` est optionnelle jusqu'à la première liaison, `erp_user_id` persiste le `RP_Users.UserID` choisi par l'ADMIN, et le login OIDC devient une opération de liaison uniquement.

### Moyen terme / WorkPackages

Voir :

- `ADR-013-work-package-weekly-load-and-medium-term-boundaries.md`
- `ADR-015-canonical-work-package-resource-class.md`
- `ADR-019-work-package-load-intervals-and-derived-lifecycle.md`

ADR-013 fixe la frontière de #502 : `TaskCatalogEntry` est la tâche ERP de référence des WorkPackages, `planned_hours` reste leur charge totale, tandis que le budget ERP, la charge Moyen terme, les `Shift` du Planning et le forecast Delivery restent des autorités distinctes. ADR-019 supersède partiellement son ancien modèle de répartition hebdomadaire obligatoire : l’intention persistée devient une collection facultative d’intervalles datés, le solde non explicitement positionné est dérivé automatiquement sur toute la période et le statut WorkPackage devient une projection read-only du cycle de vie.

ADR-015 fixe la qualification métier du WorkPackage comme une référence canonique persistée vers `ResourceClassConfig`, distincte de `TaskCatalogEntry.resource_class_code`. La classe de la tâche ERP peut initialiser ou suggérer une valeur, mais ne constitue jamais un fallback dynamique après persistance.

### Base de données et migration

Voir :

- `../SQL_SCHEMA_V1.md`
- `../SQL_CUTOVER_RUNBOOK.md`
- `../PRE_CUTOVER_MODEL_FREEZE.md`

### Acumatica

Voir :

- `../ACUMATICA_PHASE1.md`
- `../ACUMATICA_RESOURCE_FOUNDATION.md`
- `../ACUMATICA_EMBEDDING.md`

### Responsabilité opérationnelle

Voir :

- `ADR-020-operational-responsibility-hierarchy-and-approved-context.md`
- `ADR-017-erp-project-manager-and-rp-co-managers.md`

ADR-020 étend #289 avec des overrides projet, besoin/segment et quart. Pour les plans issus d'une demande approuvée, l'héritage est capturé à l'approbation et ne relit pas dynamiquement la demande candidate, la tâche, le projet local ou le principal ERP. ADR-017 reste autoritaire pour définir et résoudre le principal ERP et les co-chargés; ADR-020 remplace uniquement l'ancienne hiérarchie limitée du responsable opérationnel.

### Communications et intégrations

Voir :

- `../M365_GRAPH_COMMUNICATIONS.md`
- les modules sous `app/infrastructure/m365/`
- les modules sous `app/infrastructure/smtp/`

### Delivery / Verification

Voir :

- `../FUTURE_DELIVERY_VERIFICATION.md`
- `ADR-008-delivery-planning-boundary.md`

La vision long terme reste décrite dans `FUTURE_DELIVERY_VERIFICATION.md`, mais la frontière structurante de #362 est désormais acceptée dans ADR-008 : Delivery reste distinct de Planning, les Shift représentent de la capacité réservée plutôt que des actuals, les heures actuelles du WorkPackage ne sont pas un budget approuvé, et Delivery possède sa propre progression, concurrence et autorisation. Verification (#363) reste une tranche distincte à construire après Delivery.

---

## 2. Architecture Decision Records (ADR)

Un ADR documente une décision d'architecture importante et durable.

Un ADR ne sert pas à décrire tout ce qui a été développé. Il sert à conserver la réponse à une question du type :

> « Pourquoi l'application fonctionne-t-elle de cette façon plutôt que selon une autre approche raisonnable? »

Exemples de sujets appropriés :

- séparation entre deux concepts métier;
- choix d'une frontière entre frontend et backend;
- structure d'un modèle de données;
- stratégie d'authentification;
- choix d'une intégration externe;
- stratégie de migration;
- changement important d'un contrat API;
- choix affectant plusieurs fonctionnalités futures.

Exemples qui ne nécessitent normalement pas d'ADR :

- correction de bug locale;
- renommage de variable;
- ajout d'un champ simple;
- changement UX limité;
- optimisation interne sans conséquence architecturale;
- détail d'implémentation entièrement contenu dans une issue.

---

### ADR acceptés actuels

| ADR | Décision |
|---|---|
| ADR-001 | séparer cible automatique du besoin et affectation réelle des quarts |
| ADR-002 | périodes métier par `RequestLine` et identités stables |
| ADR-003 | révision approuvée immuable comme preuve d'autorisation |
| ADR-004 | séparer demande candidate, autorisation approuvée et plan actif |
| ADR-005 | identité canonique du demandeur distincte de l'acteur authentifié |
| ADR-006 | sérialiser les mutations concurrentes du planning par une révision globale persistante/CAS SQL tant que le rebuild reste global |
| ADR-007 | séparer les actifs réservables des ressources humaines tout en partageant l'orchestration Planning; occupation initiale exclusive à la journée |
| ADR-008 | séparer Delivery de Planning; WorkPackage comme jonction, capacité Planning read-only, progression/forecast et concurrence Delivery propres |
| ADR-009 | persister l’intention d’annulation sur WorkforceRequest, décider selon la matérialisation réelle et séparer CAS de demande de CAS planning |
| ADR-010 | routage par `ApprovalScope` et quorum ET entre lignes / OU entre approbateurs admissibles, sans matérialisation partielle |
| ADR-011 | SQL Server comme base de référence intégration/staging/production; SQLite réservé au local/test lorsque sa sémantique suffit |
| ADR-012 | pré-provisionner `AppUser` avant OIDC; paire OIDC optionnelle 0..1, `erp_user_id` explicite et premier login limité à la liaison |
| ADR-013 | rattacher les WorkPackages à `TaskCatalogEntry`, persister leur charge hebdomadaire et séparer budget ERP, Moyen terme, Planning et Delivery |
| ADR-014 | conserver un `AppUser ADMIN` d'urgence distinct d'OIDC/dev, avec credential hashé, session serveur/CSRF communs, audit et protection du dernier accès |
| ADR-015 | persister la classe canonique du WorkPackage indépendamment de la classification de sa tâche ERP, sans fallback dynamique |
| ADR-016 | conserver `AssetAllocation` comme autorité physique et représenter les affectations d’actifs ad hoc par un `AssetRequirement` appartenant au `Shift` |
| ADR-017 | conserver le principal sous autorité ERP, persister seulement les co-chargés RP et résoudre les deux par une projection backend canonique |
| ADR-018 | conserver `AssetRequirement → AssetAllocation` pour cinq contextes explicites, séparer fenêtre/budget/occupation et garder opérateur, disponibilité et concurrence sous les autorités Planning existantes |
| ADR-019 | persister des intervalles WorkPackage explicites facultatifs, dériver le solde automatique sur toute la période et projeter le statut depuis le cycle de vie |
| ADR-020 | résoudre le responsable opérationnel par une hiérarchie Shift → besoin → demande → tâche → projet local → principal ERP, en figeant l’héritage approuvé |

Ces vingt ADR sont en statut `Accepted`. ADR-006 reste le socle de concurrence globale. ADR-012 guide les prochaines tranches identité : `AppUser` est l'autorité locale une fois créé et aucune identité OIDC ne crée elle-même un compte. ADR-011 établit SQL Server comme base de référence d'exploitation et réserve SQLite aux usages local/test adaptés. ADR-010 guide #276 pour la multi-approbation par ligne et ses référentiels. ADR-007 guide #291 pour les actifs réservables. ADR-008 guide #362 : `DeliveryPlan`/Epics/Stories restent distincts des `Shift`, les heures WorkPackage actuelles sont une référence de planification et non un budget approuvé, et Delivery consomme une projection read-only du plan actif/approuvé. ADR-015 guide #524 : `WorkPackage.resource_class_code` devient l'autorité de qualification métier du WorkPackage, distincte de la classification ERP de sa tâche. ADR-016 guide #560 : toute réservation physique reste un `AssetAllocation` porté par un `AssetRequirement`, y compris lorsqu’il est créé ad hoc depuis un `Shift`. ADR-017 guide #573 : le principal demeure sous autorité ERP, les co-chargés sont des nominations RP distinctes, et les scopes/projections doivent partager une résolution canonique sans dériver de permissions. ADR-018 guide #575 : les contextes d’actifs restent des `AssetRequirement` explicites, `AssetAllocation` reste l’autorité physique des dates et de l’unité, et fenêtre autorisée, budget d’usage et occupation réelle ne sont jamais confondus. ADR-019 guide #591 : les intervalles explicites WorkPackage sont facultatifs, le solde est dérivé sans seconde autorité persistée et le statut est read-only et dérivé. ADR-020 guide #594 : le responsable opérationnel suit une hiérarchie unique `Shift → ResourceRequirement → WorkforceRequest → TaskCatalogEntry → Project local → principal ERP`, l’héritage d’un plan approuvé est figé dans son contexte autorisé, et ADR-017 reste autoritaire pour le principal ERP et les co-chargés hors de ce remplacement ciblé.

---

## 3. Convention de nommage

Les ADR sont numérotés séquentiellement :

```text
ADR-001-titre-court.md
ADR-002-titre-court.md
ADR-003-titre-court.md
```

Utiliser :

- un numéro sur trois chiffres;
- un titre court;
- des mots séparés par des tirets;
- un nom décrivant la décision, pas l'issue.

Exemple :

```text
ADR-004-separate-requirement-target-from-shift-assignment.md
```

Le numéro ADR ne correspond pas au numéro de GitHub Issue.

---

## 4. Statut d'un ADR

Chaque ADR possède un statut.

Valeurs recommandées :

- `Proposed` — décision encore en discussion;
- `Accepted` — décision retenue et applicable;
- `Superseded` — remplacée par une décision plus récente;
- `Deprecated` — encore présente historiquement mais ne doit plus guider de nouveaux développements.

Ne pas modifier silencieusement une décision `Accepted` importante pour lui faire dire autre chose.

Si l'architecture change substantiellement, créer généralement un nouvel ADR et marquer l'ancien comme `Superseded`.

---

## 5. Format recommandé

Utiliser le format suivant.

```md
# ADR-NNN — Titre

Status: Proposed | Accepted | Superseded | Deprecated
Date: YYYY-MM-DD

## Context

Quel problème ou quelle ambiguïté architecturale doit être résolu?

Décrire uniquement le contexte nécessaire pour comprendre la décision.

## Decision

Quelle décision a été prise?

Cette section doit être suffisamment précise pour guider un développeur futur.

## Alternatives considered

Quelles autres options réalistes ont été considérées?

### Option A

Résumé.

### Option B

Résumé.

## Consequences

### Positive

- conséquence;
- conséquence.

### Trade-offs / negative

- compromis;
- contrainte.

## Implementation notes

Informations importantes pour l'implémentation, sans transformer l'ADR en spécification détaillée.

## References

- GitHub Issue #...
- PR #...
- document lié
```

Toutes les sections ne sont pas obligatoires si elles n'apportent rien, mais `Context`, `Decision` et `Consequences` devraient presque toujours être présentes.

---

## 6. Workflow ADR

Le workflow recommandé est :

```text
question architecturale
        ↓
analyse
        ↓
ADR Proposed
        ↓
décision
        ↓
ADR Accepted
        ↓
implémentation
        ↓
PR / tests / CI
```

Pour une décision simple, l'ADR peut être créé directement en statut `Accepted`.

Pour une décision importante ou contestable :

1. créer ou rédiger l'ADR en `Proposed`;
2. faire l'analyse architecturale;
3. mettre à jour la section `Decision`;
4. passer le statut à `Accepted`;
5. seulement ensuite laisser le développement dépendant de cette décision se poursuivre.

---

## 7. Utilisation avec les agents

### Product Owner

Le Product Owner décide quand une question mérite une décision durable.

Il n'a pas besoin de rédiger lui-même tous les détails techniques de l'ADR.

### Architecte

Lorsqu'une analyse architecturale aboutit à une décision durable, l'architecte devrait produire ou proposer le contenu de l'ADR.

Le rapport d'analyse complet peut être beaucoup plus long que l'ADR.

L'ADR doit conserver seulement :

- le contexte utile;
- la décision;
- les alternatives importantes;
- les conséquences.

### Développeur

Avant un changement architectural significatif, le développeur doit consulter ce dossier et les documents liés.

Si un ADR `Accepted` couvre déjà la question, il doit le suivre.

Si l'implémentation nécessite de contredire un ADR `Accepted`, le développeur doit s'arrêter et demander une décision plutôt que modifier silencieusement l'architecture.

---

## 8. Relation avec les GitHub Issues

Les GitHub Issues restent la source principale pour :

- les besoins;
- les travaux à effectuer;
- le découpage;
- les priorités;
- l'état d'avancement.

Les ADR répondent à une autre question :

```text
GitHub Issue
    ↓
Que doit-on construire?

ADR
    ↓
Quelle décision architecturale doit guider sa construction?
```

Une issue peut référencer zéro, un ou plusieurs ADR.

Un ADR peut être pertinent pour plusieurs issues.

---

## 9. Relation avec AGENTS.md

`AGENTS.md` définit comment un agent doit travailler dans le dépôt.

Ce dossier définit les décisions architecturales qu'il doit respecter.

En résumé :

```text
GitHub Issues  → quoi construire
AGENTS.md      → comment travailler
architecture/  → pourquoi l'architecture est ainsi
code + tests   → ce qui est réellement implémenté
```

---

## 10. Principe de maintenance

Ne pas créer un ADR pour chaque changement.

Créer un ADR lorsqu'une décision :

- est structurante;
- aura probablement un impact sur plusieurs développements futurs;
- possède plusieurs options raisonnables;
- serait difficile à comprendre uniquement en lisant le code;
- mérite d'être connue par un futur développeur avant de modifier le système.

Un petit nombre d'ADR utiles et maintenus vaut mieux qu'une grande collection de décisions triviales ou obsolètes.

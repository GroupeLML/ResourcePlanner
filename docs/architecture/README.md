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


### Ressources réservables non humaines

Voir :

- `ADR-007-reservable-non-human-resources.md`

ADR-007 est la décision structurante de #291. L'implémentation n'est pas encore livrée au moment de l'acceptation de l'ADR : elle doit conserver les modèles persistants humains et actifs distincts, utiliser des lignes `RequestLine[ASSET]`, matérialiser `AssetRequirement → AssetAllocation` et réutiliser la même orchestration d'approbation, de concurrence, d'idempotence et d'audit. La première granularité retenue est une occupation exclusive à la journée, sans partage intrajournalier ni sélection automatique d'une unité.

### Authentification et autorisation

Voir :

- `../AUTH_RBAC.md`
- `../OIDC_ACUMATICA_VALIDATION.md`
- `../IDENTITY_PREPROVISIONING.md`
- `ADR-012-preprovision-app-users-before-oidc-link.md`

ADR-012 fixe la cible de pré-provisionnement : `AppUser` existe avant le premier login OIDC, la paire `(issuer, subject)` est optionnelle jusqu'à la première liaison, `erp_user_id` persiste le `RP_Users.UserID` choisi par l'ADMIN, et le login OIDC devient une opération de liaison uniquement.

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

Ces douze ADR sont en statut `Accepted`. ADR-006 reste le socle de concurrence globale. ADR-012 guide les prochaines tranches identité : `AppUser` est l'autorité locale une fois créé et aucune identité OIDC ne crée elle-même un compte. ADR-011 établit SQL Server comme base de référence d'exploitation et réserve SQLite aux usages local/test adaptés. ADR-010 guide #276 pour la multi-approbation par ligne et ses référentiels. ADR-007 guide #291 pour les actifs réservables. ADR-008 guide #362 : `DeliveryPlan`/Epics/Stories restent distincts des `Shift`, les heures WorkPackage actuelles sont une référence de planification et non un budget approuvé, et Delivery consomme une projection read-only du plan actif/approuvé.

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

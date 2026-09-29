# ADR-012 — Pré-provisionner AppUser avant la liaison OIDC

Status: Accepted
Date: 2026-09-29

## Context

Le socle identité actuel a été construit par #218 puis étendu par #223, #224, #233, #256, #447 et #468.

Le comportement livré par #223 permet actuellement au premier login OIDC de résoudre `preferred_username → RP_Users.UserID`, puis de créer/provisionner un `AppUser` à partir de l'annuaire ERP local lorsque les conditions d'admissibilité sont satisfaites. Cette approche a permis de valider le contrat OIDC réel, mais elle donne encore au login la responsabilité de créer le compte RessourcePlanner.

La décision produit change : un ADMIN doit pouvoir créer, configurer, activer et voir un véritable `AppUser` avant son premier login OIDC. Le premier login doit seulement lier une identité authentifiée à un compte RessourcePlanner pré-provisionné et déjà autorisé.

Les identifiants ERP `UserID` et `EmployeID` sont distincts. Plusieurs `RP_Users.UserID` peuvent référencer le même `EmployeID`; la sélection administrative du compte ERP doit donc être persistée explicitement.

Cette ADR formalise la décision uniquement. IDENTITY-A ne modifie aucun modèle SQL, migration, repository, route, service OIDC, session, frontend ni test fonctionnel runtime.

## Decision

### 1. AppUser reste l'identité interne stable

`AppUser.id` demeure l'identité interne stable de RessourcePlanner.

Le modèle cible conserve notamment :

```text
id
display_name
email
employee_external_id
business_contact_id
roles
active
issuer
subject
erp_user_id
```

Le renommage physique éventuel de `roles_json` ou d'autres colonnes existantes n'est pas décidé ici; la sémantique autoritaire est celle de l'agrégat `AppUser`.

### 2. La paire OIDC devient optionnelle mais atomique

`issuer` et `subject` restent portés par `AppUser` pour le besoin actuel.

La cardinalité cible est :

```text
AppUser → 0..1 identité OIDC
```

L'invariant est :

```sql
(issuer IS NULL AND subject IS NULL)
OR
(issuer IS NOT NULL AND subject IS NOT NULL)
```

Une valeur vide ou un placeholder n'est jamais une identité valide. Un `AppUser` sans paire OIDC est un compte RessourcePlanner pré-provisionné qui n'a pas encore complété sa première liaison OIDC.

Aucune table `AppUserIdentity` n'est introduite pour le besoin actuel. Une évolution future vers plusieurs identités externes par compte est hors périmètre.

### 3. Le compte ERP sélectionné est persisté explicitement

Le modèle cible ajoute conceptuellement :

```text
AppUser.erp_user_id
    →
erp_user_directory.user_id
```

`erp_user_id` représente le `RP_Users.UserID` choisi explicitement par l'ADMIN.

Il est distinct de :

```text
AppUser.employee_external_id
    →
RP_Users.EmployeID / RP_Employees.EmployeID
```

`UserID` identifie un compte ERP. `EmployeID` identifie un employé. Plusieurs `UserID` peuvent donc référencer le même `EmployeID` sans que RessourcePlanner déduise automatiquement quel compte ERP doit posséder le compte local.

### 4. AppUser devient autoritaire pour activation et rôles après provisionnement

Avant création d'un `AppUser`, `erp_user_directory` peut conserver une configuration préparatoire d'activation et de rôles.

Dès que l'`AppUser` est créé, les valeurs autoritaires sont :

```text
AppUser.active
AppUser.roles_json
```

Les surfaces ADMIN « RP_Users » et « utilisateurs RessourcePlanner » devront ultimement appeler la même politique/commande métier afin d'éviter deux autorités concurrentes.

La synchronisation ERP ne doit pas activer, réactiver ni attribuer des rôles à un `AppUser`.

### 5. Le premier login OIDC devient une liaison uniquement

Le contrat externe confirmé reste :

```text
preferred_username.strip()
    →
RP_Users.UserID
```

puis :

```text
RP_Users.EmployeID
    →
AppUser.employee_external_id
```

Avant de lier une nouvelle paire OIDC, le compte local candidat doit également satisfaire :

```text
AppUser.erp_user_id == RP_Users.UserID
```

Le flux cible est :

```text
token OIDC validé
    ↓
chercher (issuer, subject)
    ↓
si déjà lié → résoudre le même AppUser
    ↓
sinon preferred_username → RP_Users.UserID
    ↓
retrouver le compte AppUser pré-provisionné
    ↓
vérifier erp_user_id
    ↓
vérifier EmployeID
    ↓
vérifier AppUser actif
    ↓
vérifier admissibilité ERP
    ↓
vérifier absence de conflit
    ↓
lier (issuer, subject)
    ↓
audit
    ↓
session
```

Le login OIDC ne doit pas :

- créer un `AppUser`;
- attribuer ou modifier des rôles;
- activer ou réactiver un compte;
- déplacer une paire OIDC entre comptes;
- joindre par nom, courriel ou display name.

Une fois liée, la paire `(issuer, subject)` demeure l'identité d'authentification autoritaire du compte.

### 6. Invariants d'identité

Les tranches d'implémentation suivantes devront préserver au minimum :

- `AppUser.id` stable;
- `employee_external_id` unique lorsqu'il est présent;
- `erp_user_id` unique lorsqu'il est présent;
- `(issuer, subject)` unique lorsqu'elle est présente;
- paire OIDC complètement absente ou complètement présente;
- aucune valeur vide utilisée comme identité;
- désactiver un compte ne libère ni `erp_user_id`, ni `employee_external_id`, ni la paire OIDC;
- aucune liaison OIDC ne déplace silencieusement une paire vers un autre `AppUser`;
- un changement de `EmployeID` dans la source ERP ne transfère jamais automatiquement le compte local;
- plusieurs `UserID` pour un même `EmployeID` exigent une sélection administrative explicite;
- aucun login OIDC ne crée ni n'active lui-même un compte.

### 7. Sessions inchangées

Le contrat suivant reste valide :

```text
AuthSession.user_id → AppUser.id
```

La cardinalité OIDC `0..1` n'exige aucun nouveau modèle de session.

Les sessions existantes devront ultimement être invalidées ou cesser de résoudre un principal actif lorsque :

- `AppUser.active` devient faux;
- le compte ERP lié devient non admissible.

Cette logique n'est pas implémentée dans IDENTITY-A.

### 8. BusinessContact et Resource restent distincts

Les décisions existantes sont conservées :

```text
AppUser.employee_external_id ↔ Resource.external_id
```

reste un rapprochement métier, et :

```text
AppUser.business_contact_id → BusinessContact
```

reste la relation vers le contact métier.

Pré-provisionner un utilisateur RessourcePlanner ne crée ni n'active automatiquement une `Resource`. Un utilisateur autorisé et une ressource planifiable restent deux concepts distincts.

### 9. Sécurité de la liaison

Le futur service de liaison devra être fail-closed au minimum pour :

- collision `(issuer, subject)`;
- collision `EmployeID`;
- plusieurs `UserID` pour le même employé sans sélection explicite;
- deuxième identité OIDC tentant de prendre un compte déjà lié;
- changement de `preferred_username`;
- premier login concurrent;
- login d'un `AppUser` désactivé;
- login d'un utilisateur ERP non admissible;
- tentative de réactivation silencieuse;
- fallback historique capable d'auto-provisionner un compte.

Le fallback générique `RESOURCEPLANNER_OIDC_AUTO_PROVISION` est incompatible avec le nouveau principe :

```text
aucune identité OIDC ne crée elle-même un compte
```

Sa suppression ou sa neutralisation relève d'une tranche ultérieure, prévue avec IDENTITY-D.

### 10. Audit durable séparé du Planning

Un audit durable d'identité/administration sera nécessaire au minimum pour :

- pré-provisionnement d'un compte par ADMIN;
- activation/désactivation;
- modification des rôles;
- première liaison OIDC réussie.

L'historique Planning ne doit pas être réutilisé pour ces événements.

IDENTITY-A ne choisit pas de structure SQL d'audit supplémentaire.

### 11. UX cible

Les deux surfaces peuvent être conservées :

- `RP_Users` : découverte et sélection des comptes ERP candidats;
- utilisateurs RessourcePlanner : comptes `AppUser` réellement créés.

L'état doit permettre de distinguer au minimum :

```text
Compte : Actif
OIDC   : En attente
```

puis :

```text
Compte : Actif
OIDC   : Lié
```

Une refonte complète de la page n'est pas requise.

## Alternatives considered

### Conserver le provisionnement au premier login

Rejeté. Cela empêche un ADMIN de matérialiser entièrement le compte local, ses rôles et son activation avant la première authentification, et laisse le login responsable d'une décision d'autorisation locale.

### Créer une table AppUserIdentity immédiatement

Reporté. La cardinalité nécessaire est seulement `0..1` identité OIDC par `AppUser`. Une table dédiée ajouterait une abstraction et une migration sans besoin produit actuel. Elle pourra être introduite plus tard si plusieurs identités externes par compte deviennent nécessaires.

### Utiliser une identité OIDC fictive avant le premier login

Rejeté. Les placeholders affaiblissent les invariants d'unicité et rendent ambiguë la distinction entre compte pré-provisionné et identité réellement liée.

### Identifier le compte par EmployeID seulement

Rejeté. `EmployeID` identifie l'employé, pas le compte ERP. Plusieurs `UserID` peuvent référencer le même employé; `erp_user_id` doit donc persister le choix de compte.

## Consequences

### Positive

- l'autorisation locale est décidée avant l'authentification;
- un ADMIN peut attribuer `ADMIN` ou tout autre rôle avant le premier login;
- le login devient une opération de liaison d'identité, plus petite et plus fail-closed;
- `UserID`, `EmployeID`, `AppUser.id`, `Resource.external_id` et `BusinessContact` gardent des responsabilités distinctes;
- les UUID `AppUser.id` existants peuvent être conservés pendant la future migration;
- le modèle de session actuel reste utilisable.

### Trade-offs / negative

- `issuer` et `subject` devront devenir nullables ensemble;
- une contrainte d'intégrité de paire et de nouvelles contraintes d'unicité seront nécessaires;
- `erp_user_id` devra être persisté et migré pour les comptes existants;
- les commandes ADMIN actuelles autour de `erp_user_directory.local_active/roles_json` devront converger vers `AppUser`;
- le service OIDC #223 devra être modifié pour ne plus créer ni réactiver de compte;
- le fallback d'auto-provisionnement historique devra être neutralisé;
- une politique explicite de concurrence/idempotence sera nécessaire au premier lien;
- un audit durable identité/admin reste à concevoir et implémenter.

## Implementation notes

- IDENTITY-A est uniquement documentaire.
- Les modifications SQL, Alembic, repositories, routes, services OIDC, frontend, sessions et tests runtime sont volontairement reportées.
- La migration future doit conserver les `AppUser.id` existants.
- Pour un compte déjà lié, `erp_user_id` devra être backfillé de façon déterministe ou signalé comme conflit; aucune correspondance par nom/courriel n'est permise.
- Les contraintes et transactions devront être compatibles SQL Server, base de référence selon ADR-011.
- Le contrat détaillé et la matrice d'acceptation sont dans `docs/IDENTITY_PREPROVISIONING.md`.

## References

- #218 — authentification OIDC et RBAC local
- #223 — contrat OIDC Acumatica réel
- #224 — administration utilisateurs/rôles
- #232 — contrat Acumatica réel
- #233 — fondation locale identité/ressources
- #256 — Employees/Users et liaison identité
- #447 — bootstrap RP_Employees / RP_Users
- #468 — stabilisation données réelles et relations EmployeID
- ADR-011 — SQL Server base de référence
- `docs/AUTH_RBAC.md`
- `docs/OIDC_ACUMATICA_VALIDATION.md`
- `docs/integrations/acumatica/RP_EMPLOYEES_USERS.md`
- `docs/IDENTITY_PREPROVISIONING.md`

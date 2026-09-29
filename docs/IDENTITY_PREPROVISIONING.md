# Identité — pré-provisionnement AppUser et première liaison OIDC

Status: Target contract implemented through IDENTITY-D
Date: 2026-09-29
ADR: `docs/architecture/ADR-012-preprovision-app-users-before-oidc-link.md`

## 1. Portée

Ce document fixe les contrats fonctionnels et de sécurité qui guideront les tranches d'implémentation suivant IDENTITY-A.

IDENTITY-A a fixé ce contrat de manière documentaire. IDENTITY-B a livré le schéma/persistance, IDENTITY-C le pré-provisionnement ADMIN autoritaire et IDENTITY-D remplace maintenant le provisionnement au premier login par une liaison OIDC uniquement.

La projection/UX finale et la reprise/acceptation transversale restent suivies par les tranches suivantes du roadmap.

## 2. Concepts et identités

### AppUser

`AppUser.id` est l'identité interne stable de RessourcePlanner.

Le modèle cible conserve conceptuellement :

```text
AppUser
  id
  display_name
  email
  employee_external_id
  business_contact_id
  roles_json
  active
  issuer
  subject
  erp_user_id
```

### Compte ERP

```text
AppUser.erp_user_id
    →
erp_user_directory.user_id
    →
RP_Users.UserID
```

`erp_user_id` identifie le compte ERP explicitement sélectionné par l'ADMIN.

### Employé ERP

```text
AppUser.employee_external_id
    →
RP_Users.EmployeID
    =
RP_Employees.EmployeID
```

`employee_external_id` identifie l'employé.

`UserID` et `EmployeID` ne sont pas interchangeables. Plusieurs `UserID` peuvent référencer le même `EmployeID`.

### Identité OIDC

`issuer` et `subject` restent sur `AppUser` et représentent zéro ou une identité OIDC liée.

Aucune table `AppUserIdentity` n'est requise pour cette tranche architecturale.

## 3. États cibles d'un compte

### Pré-provisionné, jamais lié

```text
AppUser.id            = présent
AppUser.erp_user_id   = présent
AppUser.active        = true ou false selon décision ADMIN
AppUser.roles_json    = rôles locaux configurés
AppUser.issuer        = null
AppUser.subject       = null
```

Cet état est valide et représente un véritable compte RessourcePlanner.

### Lié OIDC

```text
AppUser.id            = inchangé
AppUser.erp_user_id   = inchangé
AppUser.employee_external_id = cohérent avec l'annuaire ERP
AppUser.issuer        = issuer validé
AppUser.subject       = subject validé
```

Le premier login ne remplace pas l'UUID et ne recrée pas le compte.

## 4. Autorités

### Avant la création de AppUser

`erp_user_directory` peut conserver une préparation administrative, notamment activation locale et rôles destinés au futur compte.

### Après la création de AppUser

Les valeurs autoritaires deviennent :

```text
AppUser.active
AppUser.roles_json
```

Une synchronisation `RP_Users` ne doit jamais :

- activer un `AppUser`;
- réactiver un `AppUser`;
- modifier ses rôles;
- créer un `AppUser` uniquement parce qu'un `UserID` existe.

Les deux surfaces ADMIN devront réutiliser la même commande/politique métier lorsqu'elles modifient un compte déjà provisionné.

## 5. Flux ADMIN cible

```text
RP_Users synchronisé
    ↓
ADMIN sélectionne un UserID ERP
    ↓
ADMIN attribue les rôles et active l'utilisateur
    ↓
création immédiate d'un véritable AppUser
    ↓
AppUser visible dans les utilisateurs RessourcePlanner
    ↓
OIDC = En attente
```

Le pré-provisionnement doit persister explicitement :

- le `UserID` choisi via `erp_user_id`;
- l'`EmployeID` correspondant via `employee_external_id`;
- les rôles locaux;
- l'état actif/inactif;
- les attributs descriptifs permis par le contrat courant.

Aucune identité OIDC fictive n'est créée.

## 6. Premier login OIDC cible

Le token doit d'abord être validé selon les règles OIDC existantes. Ensuite :

```text
token OIDC validé
    ↓
chercher (issuer, subject)
    ↓
si déjà lié → résoudre le même AppUser
    ↓
sinon preferred_username.strip() → RP_Users.UserID
    ↓
retrouver le AppUser pré-provisionné
    ↓
vérifier AppUser.erp_user_id == RP_Users.UserID
    ↓
vérifier AppUser.employee_external_id == RP_Users.EmployeID
    ↓
vérifier AppUser.active
    ↓
vérifier admissibilité ERP
    ↓
vérifier absence de conflit
    ↓
lier atomiquement (issuer, subject)
    ↓
écrire l'audit
    ↓
créer la session
```

Le login ne copie aucun rôle et ne modifie aucun état d'activation.

## 7. Résolution et clés interdites

La seule chaîne de rapprochement du premier lien est :

```text
preferred_username.strip()
    →
RP_Users.UserID
    →
AppUser.erp_user_id

RP_Users.EmployeID
    →
AppUser.employee_external_id
```

Il est interdit de lier un compte à partir de :

- nom;
- courriel;
- display name;
- similarité de chaîne;
- convention dérivée de `subject`;
- hypothèse `subject == UserID`.

Après liaison, `(issuer, subject)` est l'identité d'authentification autoritaire.

## 8. Invariants

Les invariants cibles sont :

1. `AppUser.id` ne change pas lors du pré-provisionnement, de la liaison OIDC, d'une désactivation ou d'une réactivation.
2. `employee_external_id` est unique lorsqu'il est présent.
3. `erp_user_id` est unique lorsqu'il est présent.
4. `(issuer, subject)` est unique lorsqu'elle est présente.
5. `issuer` et `subject` sont tous deux `NULL` ou tous deux non `NULL`.
6. Après normalisation, aucune chaîne vide ne peut être persistée comme identifiant.
7. Désactiver un compte ne libère aucune identité.
8. Une paire OIDC déjà liée ne peut pas être déplacée implicitement.
9. Un compte déjà lié ne peut pas accepter silencieusement une deuxième paire OIDC.
10. Un changement source de `EmployeID` ne transfère pas automatiquement un compte.
11. Plusieurs `UserID` pour un même `EmployeID` ne déclenchent aucun choix automatique.
12. Le login OIDC ne crée pas, n'active pas et ne réactive pas de compte.
13. Les rôles OIDC ou ERP ne deviennent jamais des rôles RessourcePlanner.
14. Une synchronisation ERP ne modifie pas l'autorisation locale.
15. Les UUID existants doivent être conservés lors de la future migration.

## 9. Concurrence et idempotence

### Replay du même login

Un replay avec la même paire `(issuer, subject)` doit résoudre le même `AppUser.id`, sous réserve que le compte demeure actif et ERP-admissible.

Il ne crée aucun nouvel utilisateur et ne réécrit pas les rôles.

### Premier login concurrent

Deux callbacks concurrents pour le même compte non lié doivent produire au plus une première liaison réussie.

Le résultat acceptable est :

- un callback acquiert la liaison;
- l'autre relit/résout ensuite le même compte si la paire est identique, ou échoue fail-closed si elle est différente.

Aucune fenêtre de course ne doit permettre deux paires OIDC sur le même compte ni la même paire sur deux comptes.

Les contraintes SQL et la transaction applicative devront être conçues ensemble; la stratégie concrète relève d'IDENTITY-B ou d'une tranche d'implémentation ultérieure.

## 10. Changement de preferred_username

Une fois une paire `(issuer, subject)` liée, cette paire reste autoritaire pour les logins suivants.

Un changement ultérieur de `preferred_username` ne doit pas déplacer le compte.

Le runtime peut vérifier la cohérence ERP courante selon la politique définie par l'implémentation future, mais il ne doit jamais réaffecter automatiquement la paire à un autre `AppUser`.

Pour un compte encore non lié, `preferred_username` doit toujours résoudre exactement le `erp_user_id` pré-provisionné.

## 11. Changement de EmployeID

Si `RP_Users.UserID` reste le même mais que son `EmployeID` change, RessourcePlanner ne transfère pas automatiquement le compte vers un autre employé.

Le changement doit être détecté comme une divergence nécessitant une résolution administrative explicite.

Aucune recherche par nom/courriel ne peut servir à « réparer » la relation.

## 12. ERP non admissible

Un utilisateur ERP lié ou candidat qui devient non admissible doit être refusé au login.

La synchronisation ne doit pas effacer les identités locales ni réactiver le compte lorsque l'ERP redevient admissible.

La réactivation locale reste une action explicite de l'ADMIN si le compte local a été désactivé.

## 13. Sessions

Le modèle reste :

```text
AuthSession.user_id → AppUser.id
```

Aucune modification structurelle de session n'est requise par la cardinalité OIDC `0..1`.

À terme, une session existante doit cesser d'autoriser l'accès lorsque :

- `AppUser.active == false`;
- le `RP_Users.UserID` lié devient ERP-non-admissible.

IDENTITY-A ne développe pas cette invalidation.

## 14. Resource et BusinessContact

Le rapprochement métier reste :

```text
AppUser.employee_external_id ↔ Resource.external_id
```

Il ne signifie pas qu'un `AppUser` possède nécessairement une `Resource`.

Le lien contact reste :

```text
AppUser.business_contact_id → BusinessContact
```

Pré-provisionner un utilisateur :

- ne crée pas une `Resource`;
- n'active pas une `Resource`;
- ne rend pas automatiquement l'employé planifiable.

## 15. Audit

Un audit durable, séparé de l'historique Planning, devra enregistrer au minimum :

- pré-provisionnement : acteur ADMIN, `AppUser.id`, `erp_user_id`, résultat;
- activation/désactivation : acteur, ancien état, nouvel état;
- modification des rôles : acteur, ancienne valeur, nouvelle valeur;
- première liaison OIDC réussie : `AppUser.id`, identité OIDC liée, résultat.

Le format SQL n'est pas décidé dans IDENTITY-A.

## 16. UX cible

Dans la surface utilisateurs RessourcePlanner :

```yaml
Compte: Actif
OIDC: En attente
```

doit être possible dès le pré-provisionnement.

Après première liaison :

```yaml
Compte: Actif
OIDC: Lié
```

Le panneau `RP_Users` reste la surface de découverte/sélection des candidats ERP. Le panneau utilisateurs RessourcePlanner reste la surface des `AppUser` réellement créés.

Aucune refonte complète n'est nécessaire.

## 17. Fallback historique

Le fallback générique capable de créer un compte OIDC à partir d'une identité externe est incompatible avec le contrat cible.

Le principe autoritaire devient :

```text
aucune identité OIDC ne crée elle-même un compte RessourcePlanner
```

IDENTITY-D neutralise ce fallback dans le callback OIDC : même si la configuration legacy `RESOURCEPLANNER_OIDC_AUTO_PROVISION` est encore parseable pour compatibilité, elle ne peut plus créer un `AppUser` lors d'un login.

## 18. Matrice d'acceptation pour les tranches suivantes

| # | Scénario | Précondition / action | Résultat attendu |
|---|---|---|---|
| 1 | Activation d'un RP_User jamais connecté | ADMIN choisit un `UserID` admissible, assigne des rôles et active | Un véritable `AppUser` est créé immédiatement avec `erp_user_id`, `employee_external_id`, rôles et état local; `issuer/subject = NULL`. |
| 2 | Apparition immédiate comme AppUser actif | Après scénario 1 | Le compte apparaît dans la liste des utilisateurs RessourcePlanner sans attendre un login OIDC; état OIDC = En attente. |
| 3 | Rôle ADMIN avant premier login | ADMIN inclut `ADMIN` au pré-provisionnement | `AppUser.roles_json` contient `ADMIN` avant OIDC; aucun claim OIDC n'est requis pour ce rôle. |
| 4 | Premier login réussi | Token valide, `preferred_username` correspond à `erp_user_id`, EmployeID cohérent, AppUser actif, ERP admissible, aucune collision | La paire `(issuer, subject)` est liée au même `AppUser.id`, un audit est écrit, puis une session est créée. Aucun rôle/activation n'est modifié. |
| 5 | Replay du même login | Même paire sur un compte déjà lié | Le même `AppUser.id` est résolu de manière idempotente; aucune nouvelle liaison ni duplication. |
| 6 | Second (issuer, subject) pour le même compte | Compte déjà lié, nouvelle paire tente de viser le même `erp_user_id` | Refus fail-closed; aucune paire n'est remplacée et aucune identité n'est libérée. |
| 7 | Même (issuer, subject) pour un autre compte | Paire déjà liée à A, tentative contre B | Refus fail-closed; la paire reste liée à A. |
| 8 | Plusieurs UserID pour le même EmployeID | Annuaire contient plusieurs comptes ERP pour un employé | Aucun choix automatique; seul le `erp_user_id` explicitement sélectionné peut lier le compte. Les autres restent distincts. |
| 9 | Utilisateur ERP devenu inactif | `AppUser` local actif mais compte ERP non admissible | Nouveau login refusé; aucune désactivation/réactivation silencieuse ni libération d'identité. Les sessions existantes devront ultimement être invalidées selon la politique future. |
| 10 | Désactivation locale | ADMIN met `AppUser.active=false` | Les nouveaux logins sont refusés; identité et UUID conservés; les sessions existantes devront ultimement cesser d'autoriser l'accès. |
| 11 | Réactivation explicite | ADMIN réactive un compte précédemment désactivé et ERP admissible | Le même `AppUser.id`, `erp_user_id` et la même paire OIDC sont conservés; aucun nouveau compte. |
| 12 | Changement de rôle avant premier login | Compte pré-provisionné non lié; ADMIN change les rôles | `AppUser.roles_json` est mis à jour immédiatement. Le futur login utilise ces rôles sans copie depuis OIDC/ERP. |
| 13 | Changement de rôle après premier login | Compte lié; ADMIN change les rôles | La valeur autoritaire devient immédiatement celle de `AppUser.roles_json`; la paire OIDC ne change pas. L'effet précis sur sessions déjà émises suit la politique d'autorisation/session existante ou future. |
| 14 | Nouvelle synchronisation ERP | Re-sync de `RP_Users` après pré-provisionnement ou liaison | Attributs source et admissibilité peuvent être rafraîchis, mais la sync ne modifie ni `AppUser.active`, ni rôles, ni paire OIDC, ni UUID. |
| 15 | Changement d'EmployeID | Même `UserID` ERP pointe désormais vers un EmployeID différent | Aucun transfert automatique. La divergence est exposée/refusée jusqu'à résolution administrative explicite. |
| 16 | Compte sans Resource | AppUser possède un EmployeID sans Resource locale active/présente | Le compte utilisateur peut exister selon la politique d'accès; aucune Resource n'est créée/activée automatiquement. La planifiabilité reste un domaine séparé. |
| 17 | Conservation des UUID existants lors de la future migration | Migration d'AppUser déjà existants vers `erp_user_id` + OIDC nullable | Chaque compte conserve son `AppUser.id`. Tout backfill ambigu est refusé/signalé; aucune nouvelle identité n'est inventée pour contourner un conflit. |

## 19. Critères de sécurité transversaux

Les tranches suivantes ne seront pas acceptables si elles permettent l'un des comportements suivants :

- deux `AppUser` avec le même `employee_external_id` non nul;
- deux `AppUser` avec le même `erp_user_id` non nul;
- deux `AppUser` avec la même paire OIDC;
- `issuer` sans `subject` ou inversement;
- chaînes vides comme identités;
- compte OIDC créant implicitement un `AppUser`;
- compte ERP ou OIDC accordant implicitement des rôles;
- sync ERP réactivant un compte local;
- paire OIDC déplacée après désactivation;
- rapprochement par nom, courriel ou display name;
- changement d'EmployeID transférant un compte;
- course de premier login produisant deux liaisons;
- fallback générique contournant le pré-provisionnement.

## 20. Compatibilité avec les décisions existantes

Cette décision conserve :

- #218 : FastAPI reste frontière d'autorisation; `(issuer, subject)` reste l'identité externe authentifiée;
- #224 : rôles RessourcePlanner locaux et désactivation explicite;
- #232/#256/#447/#468 : `UserID` et `EmployeID` sont des identités ERP distinctes; aucune jointure nom/courriel;
- #233 : `AppUser` et `Resource` restent distincts;
- #223 : `preferred_username.strip() → RP_Users.UserID` et la paire `(issuer, subject)` restent le contrat OIDC.

Elle remplace uniquement la responsabilité du premier login : la création du compte et l'autorisation locale doivent être terminées avant OIDC.

## 21. Écarts runtime connus au moment de IDENTITY-A

Sur `main` au départ de cette tranche, #223 est encore le comportement implémenté :

- le premier login peut créer/provisionner l'`AppUser`;
- les rôles sont encore lus depuis la configuration préparatoire de `erp_user_directory`;
- `issuer/subject` sont encore requis par le modèle courant d'`AppUser`;
- `erp_user_id` n'est pas encore un champ persistant d'`AppUser`;
- le fallback `RESOURCEPLANNER_OIDC_AUTO_PROVISION` existe encore pour certains providers/fixtures.

Ces écarts sont attendus et volontairement non corrigés par IDENTITY-A.


## 22. État après IDENTITY-D

La tranche IDENTITY-D implémente désormais le flux cible du premier login :

```text
OIDC validé
  → paire déjà liée : validation AppUser + ERP puis résolution
  → sinon preferred_username.strip() → RP_Users.UserID
  → AppUser.erp_user_id exact requis
  → EmployeID exact requis
  → AppUser actif + rôles locaux requis
  → ERP admissible requis
  → bind_external_identity() atomique
  → audit OIDC_IDENTITY_LINKED
  → session
```

Le callback ne crée, n'active, ne réactive et ne rerôle plus aucun `AppUser`. Il ne met pas à jour le nom/courriel depuis les claims OIDC. Un replay de la même paire réutilise le même UUID. Une deuxième paire pour le même compte, une paire déjà liée visant un autre `erp_user_id`, un mismatch d'EmployeID ou un compte ERP inadmissible sont refusés fail-closed.

La configuration legacy d'auto-provisionnement reste temporairement lisible afin d'éviter un changement de configuration adjacent, mais n'est plus consultée par le callback.

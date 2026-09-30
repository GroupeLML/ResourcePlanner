# Authentification et RBAC — socle local

Ce document décrit la première tranche de sécurité de RessourcePlanner. Elle introduit une identité locale, une matrice de permissions et une frontière d'autorisation FastAPI sans prétendre que le vrai OIDC Acumatica est déjà configuré.

## Principes

- FastAPI est la frontière d'autorisation autoritaire.
- React pourra masquer des actions selon les permissions, mais cela ne remplace jamais les contrôles serveur.
- Les rôles ne sont jamais acceptés depuis un header envoyé par le navigateur.
- Une identité externe est mappée localement par la paire stable `(issuer, subject)`.
- Les rôles métier restent dans RessourcePlanner, même lorsque l'authentification interactive viendra d'Acumatica.
- Les credentials de synchronisation Acumatica restent distincts de l'identité de l'utilisateur connecté.

## Rôles initiaux

| Rôle | Usage initial |
|---|---|
| `ADMIN` | administration complète, ressources et synchronisation ERP |
| `COORDINATOR` | planification, demandes, approbations, WorkPackages, ressources |
| `PROJECT_MANAGER` | demandes et WorkPackages |
| `MANAGER` | lecture et approbations |
| `TECHNICIAN` | lecture |

La matrice exacte est codée dans `app/application/security.py`. Une permission effective est calculée côté backend à partir des rôles; le frontend n'a pas à reconstruire cette matrice.

## Mode local de développement

Tant que le vrai OIDC n'est pas branché, le runtime configuré utilise explicitement :

```text
RESOURCEPLANNER_AUTH_MODE=local
```

Le mode `local` crée une identité de développement côté serveur. Les valeurs optionnelles sont :

```text
RESOURCEPLANNER_LOCAL_AUTH_NAME=Administrateur local
RESOURCEPLANNER_LOCAL_AUTH_ROLES=ADMIN
RESOURCEPLANNER_LOCAL_AUTH_EMAIL=
```

Plusieurs rôles peuvent être séparés par une virgule.

### Protection réseau

Le mode local est prévu pour `127.0.0.1`, `localhost` ou `::1`. Si le serveur écoute sur une autre interface, le démarrage est refusé par défaut.

Pour un environnement de test interne explicitement assumé seulement :

```text
RESOURCEPLANNER_ALLOW_LOCAL_AUTH_NETWORK=true
```

Ce contournement n'est pas une authentification de production. Il ne doit pas être utilisé pour rendre l'application accessible à des utilisateurs finaux.

## Endpoint courant

Une fois authentifié :

```text
GET /api/v1/auth/me
```

retourne notamment :

- identité locale;
- `issuer` / `subject`;
- nom et courriel;
- rôles;
- permissions effectives;
- mode d'authentification.

## Règles HTTP initiales

- toute lecture `/api/v1/...` exige `read`;
- ressources/disponibilités exigent `manage_resources`;
- WorkPackages exigent `manage_work_packages`;
- mutations de demandes exigent `manage_demands`;
- approbation/correction exigent `approve_demands`;
- segments/quarts/Quick Shift/rebuild exigent `manage_planning`;
- synchronisation de projets Acumatica exige `sync_projects`;
- une future mutation API non classifiée est refusée par défaut jusqu'à ce qu'une permission explicite lui soit assignée.

`/health` et les fichiers statiques du frontend restent publics afin de permettre le health check et le chargement de l'application. Les données métier restent derrière `/api/v1`.

## Modèle SQL

La table `app_users` stocke :

- identifiant local;
- `issuer`;
- `subject`;
- nom d'affichage;
- courriel optionnel;
- rôles locaux;
- état actif/inactif.

La paire `(issuer, subject)` est unique. Un utilisateur inactif ne peut pas être résolu en principal authentifié.

## Pré-provisionnement et liaison OIDC — ADR-012

IDENTITY-A a fixé l'évolution; IDENTITY-B à IDENTITY-D l'ont amenée dans le runtime :

- `AppUser.id` reste l'identité interne stable;
- un ADMIN doit pouvoir créer et activer un véritable `AppUser` avant le premier login OIDC;
- la paire `(issuer, subject)` devient optionnelle mais atomique : les deux valeurs sont absentes ou présentes ensemble;
- `AppUser.erp_user_id` doit persister explicitement le `RP_Users.UserID` sélectionné;
- `AppUser.employee_external_id` reste l'identité employé `EmployeID`, distincte du compte ERP `UserID`;
- une fois l'`AppUser` créé, `AppUser.active` et `AppUser.roles_json` sont autoritaires;
- le premier login OIDC ne crée, n'active, ne réactive ni n'attribue de rôles : il lie seulement `(issuer, subject)` au compte pré-provisionné après vérifications fail-closed;
- `AuthSession.user_id → AppUser.id` reste inchangé;
- le pré-provisionnement d'un utilisateur ne crée ni n'active automatiquement une `Resource`.

Le contrat détaillé, les invariants de sécurité, la concurrence du premier login et la matrice d'acceptation sont documentés dans `IDENTITY_PREPROVISIONING.md`. Le callback IDENTITY-D utilise désormais uniquement le compte pré-provisionné et `bind_external_identity()`; l'ancien auto-provisionnement n'est plus un chemin d'accès.

Depuis IDENTITY-E, les projections ADMIN distinguent explicitement `active` de `oidc_state`. `pending` signifie seulement que la première liaison OIDC n'a pas encore eu lieu; `linked` signifie que la paire est persistée; `conflict` signale une ambiguïté/incohérence réellement observable. Un compte actif peut donc être `pending`, et un compte inactif peut rester `linked`, sans réactivation implicite.

IDENTITY-F ferme le cycle automatisé sans nouvelle décision d'architecture ni migration Alembic post-baseline : après le squash #457C, la reprise historique est une opération de données explicite. `tools/diagnose_identity_recovery.py` est read-only par défaut; `--apply-deterministic` ne renseigne que `AppUser.erp_user_id` pour une correspondance EmployeID → UserID unique et non possédée. Les ambiguïtés restent fail-closed. L'acceptation transversale vérifie le même `AppUser` de l'activation ADMIN à la liaison OIDC, au replay, à la désactivation et à la resynchronisation ERP.

## Audit

Lorsque le contexte utilisateur existe, la façade applicative et l'idempotence reçoivent son nom d'affichage comme acteur. Les services métier n'ont pas besoin de connaître FastAPI ou OIDC.

## Prochaine tranche OIDC

Le vrai branchement Acumatica devra :

1. configurer l'issuer, le client et les redirect URIs de l'instance réelle;
2. utiliser Authorization Code Flow;
3. utiliser PKCE S256 lorsque configuré/supporté;
4. valider signature, issuer, audience, état/nonce;
5. établir une session côté serveur;
6. résoudre `(issuer, subject)` vers `app_users`;
7. refuser un utilisateur absent ou inactif;
8. gérer le logout.

Aucun issuer/client/secret Acumatica réel n'est inscrit dans le dépôt.

Refs : #55, #207, #208, #218.

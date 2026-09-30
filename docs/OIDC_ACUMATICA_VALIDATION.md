# Validation réelle OIDC Acumatica

L'implémentation OIDC de RessourcePlanner est testée avec un fournisseur simulé et le contrat d'identité a maintenant été observé sur un compte Acumatica réel. Le login interactif complet post-implémentation a également été validé sur ce compte, avec résolution d'un vrai `AppUser` et `/api/v1/auth/me`. Restent à valider un deuxième compte ainsi que le comportement HTTPS/cookies Secure sur l'environnement cible.

> **ADR-012 / IDENTITY-F.** Le runtime cible et son acceptation automatisée transversale sont couverts : l'ADMIN pré-provisionne d'abord un véritable `AppUser`; le premier login OIDC lie seulement `(issuer, subject)` au compte existant après vérification de `preferred_username → RP_Users.UserID → AppUser.erp_user_id` et de l'EmployeID. Le callback ne crée, n'active, ne réactive ni ne rerôle un compte. Les surfaces ADMIN affichent séparément l'état du compte et l'état OIDC dérivé. Après le squash Alembic #457C, la reprise historique est une opération de données explicite, déterministe et fail-closed.

## Paramètres requis

- URL de découverte OIDC de l'instance;
- client ID enregistré pour RessourcePlanner;
- credential client si le type de client retenu l'exige;
- redirect URI autorisé vers `/api/v1/auth/callback`;
- scopes autorisés incluant `openid`;
- valeur réelle de l'issuer et format du `subject` retourné.

Ne jamais inscrire de secret ou token réel dans ce document ou dans Git.

## Préparation RessourcePlanner

1. exécuter `alembic upgrade head` afin d'inclure `0010_app_users_identity` et `0011_oidc_sessions`;
2. synchroniser `RP_Users`, puis activer explicitement l'utilisateur cible et lui attribuer au moins un rôle RessourcePlanner local;
3. configurer `RESOURCEPLANNER_AUTH_MODE=oidc`;
4. configurer les variables `RESOURCEPLANNER_OIDC_*` uniquement dans l'environnement d'exécution;
5. utiliser HTTPS et conserver le cookie sécurisé en environnement partagé.

## Smoke attendu

1. ouvrir RessourcePlanner sans session : l'interface doit proposer la connexion;
2. démarrer `/api/v1/auth/login` et vérifier la redirection vers Acumatica;
3. avant le premier login, vérifier dans Utilisateurs que le même `AppUser` est **Compte = Actif** et **OIDC = En attente de première connexion**;
4. terminer le login et revenir sur `/api/v1/auth/callback` puis `/`;
5. vérifier dans Utilisateurs que le même `app_user_id` / `erp_user_id` est désormais **OIDC = Lié**, sans changement de rôles ni d'activation;
6. vérifier `/api/v1/auth/me` : issuer, subject, utilisateur local, rôles et permissions attendus;
7. vérifier qu'un utilisateur Acumatica non provisionné est refusé;
8. vérifier les permissions avec au moins deux rôles locaux différents;
9. se déconnecter et confirmer que l'ancienne session ne permet plus `/api/v1/auth/me`;
10. redémarrer l'application et confirmer que les sessions SQL non expirées restent résolubles;
11. confirmer les attributs de cookie en HTTPS (`HttpOnly`, `Secure`, `SameSite=Lax`);
12. documenter uniquement les métadonnées non sensibles retenues : issuer, discovery URL, redirect URI, scopes et procédure de provisionnement.

## Diagnostic temporaire des claims validés

Pour le smoke réel seulement, le serveur peut exposer une projection filtrée des claims déjà validés en activant explicitement :

```text
RESOURCEPLANNER_OIDC_CLAIM_DIAGNOSTICS=true
```

La valeur par défaut est `false`. Lorsque le flag est actif :

- un utilisateur local déjà reconnu peut lire `GET /api/v1/auth/oidc-claims` avec sa session OIDC valide;
- si l'identité OIDC est valide mais non provisionnée localement, le callback reste refusé avec `403 oidc_user_not_registered`, mais la projection filtrée est disponible dans `error.context.oidc_claim_diagnostics`;
- la projection contient `claim_names` et uniquement les candidats d'identité explicitement autorisés : `iss`, `sub`, `preferred_username`, `name`, `email`, `unique_name`, `username`, `user_id`, `userid`, `employee_id`, lorsqu'ils existent;
- aucune valeur de token, secret, authorization code, cookie, CSRF, PKCE, state ou nonce de transaction n'est exposée;
- aucune projection n'est écrite dans les logs généraux et aucun token n'est persisté;
- la projection liée à une session reconnue reste uniquement en mémoire du processus et est retirée au logout.

Le mapping normal `preferred_username → RP_Users.UserID` est indépendant de ce flag. La configuration normale reste `RESOURCEPLANNER_OIDC_CLAIM_DIAGNOSTICS=false`.

## Séparation des credentials

L'OIDC interactif ne doit pas réutiliser `RESOURCEPLANNER_ACUMATICA_ACCESS_TOKEN`. Ce dernier appartient à la synchronisation ERP serveur-à-serveur et reste une frontière séparée.

## Contrat d'identité confirmé par #223

Le smoke réel a confirmé sur un compte et le PO a accepté le contrat suivant pour l'implémentation :

- `(issuer, sub)` reste l'identité d'authentification persistée dans `AppUser`;
- `preferred_username.strip()` est le pont exact vers `RP_Users.UserID`;
- `RP_Users.EmployeID` devient `AppUser.employee_external_id`;
- après pré-provisionnement, les rôles et l'activation autoritaires proviennent uniquement de `AppUser.roles_json` et `AppUser.active`; l'annuaire ERP fournit le UserID, l'EmployeID et l'admissibilité source;
- `name`, `email` et les display names sont descriptifs uniquement et ne sont jamais des clés de jointure;
- `sub == UserID` ne doit jamais être supposé et aucune convention comme `@Company` ne doit être inversée.

Le client OIDC extrait `preferred_username` pour le fonctionnement normal même lorsque les diagnostics de claims sont désactivés. Depuis IDENTITY-D, `RESOURCEPLANNER_OIDC_AUTO_PROVISION` est un réglage legacy sans pouvoir de création dans le callback : aucune identité OIDC ne peut créer implicitement un `AppUser`.

Validation réelle historique acquise sur un compte avec le runtime antérieur : login, callback, provisionnement contrôlé, session et `/api/v1/auth/me`. Le nouveau flux IDENTITY-D doit être re-smoké avec un compte pré-provisionné avant de considérer la validation environnementale équivalente. Validation réelle encore requise : deuxième compte OIDC, logout/révocation complet, cookies HTTPS/Secure sur l'environnement cible, production et SQL Server. Le correctif CSRF du proxy avec port non standard a été fusionné séparément via PR #476; son smoke de mutation ADMIN post-correctif reste à exécuter.


## Acceptation automatisée IDENTITY-F

La CI couvre un parcours transversal avec fournisseur OIDC simulé et frontières FastAPI/SQL réelles :

1. synchronisation d'un `RP_Users` admissible;
2. activation et attribution de rôle par la surface ADMIN, avec `AppUser` immédiatement `pending`;
3. premier callback OIDC vers le même `AppUser.id`, conservation de `erp_user_id`, `employee_external_id`, `BusinessContact`, rôles et activation, puis création de `AuthSession`;
4. replay de la même paire sans duplication d'`AppUser` ni nouvel audit de première liaison;
5. désactivation/changement de rôle après liaison avec état OIDC toujours `linked`;
6. resynchronisation `RP_Users` sans réactivation, rerôlage ou déplacement de l'identité OIDC.

La reprise historique est testée séparément : le diagnostic est read-only, les cas ambigus restent intacts et `--apply-deterministic` ne renseigne que le `erp_user_id` exact sur le même AppUser, sans toucher au BusinessContact, à l'OIDC, aux rôles, à l'activation, aux sessions ou aux Resources.

Cette preuve est locale/CI. Elle ne constitue pas une validation contre l'instance Acumatica réelle ni contre SQL Server réel.

## Validations environnementales encore ouvertes après IDENTITY-F

Restent à exécuter ou confirmer sur les environnements réels :

- un deuxième compte OIDC Acumatica réel afin de confirmer le contrat sur plus d'une identité;
- le smoke réel `Compte = Actif / OIDC = En attente` → première connexion → `OIDC = Lié`;
- une mutation ADMIN sous vraie session OIDC/proxy après le correctif CSRF;
- HTTPS réel et attributs de cookies `HttpOnly` / `Secure` / `SameSite`;
- le smoke production;
- la validation SQL Server environnementale lorsque la CI de compilation/readiness ne couvre pas le comportement réel de l'instance.

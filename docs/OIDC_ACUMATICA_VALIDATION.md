# Validation réelle OIDC Acumatica

L'implémentation OIDC de RessourcePlanner est testée avec un fournisseur simulé et le contrat d'identité a maintenant été observé sur un compte Acumatica réel. Le login complet reste à revalider après ce branchement applicatif, notamment sur un deuxième compte et sur HTTPS réel.

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
3. terminer le login et revenir sur `/api/v1/auth/callback` puis `/`;
4. vérifier `/api/v1/auth/me` : issuer, subject, utilisateur local, rôles et permissions attendus;
5. vérifier qu'un utilisateur Acumatica non provisionné est refusé;
6. vérifier les permissions avec au moins deux rôles locaux différents;
7. se déconnecter et confirmer que l'ancienne session ne permet plus `/api/v1/auth/me`;
8. redémarrer l'application et confirmer que les sessions SQL non expirées restent résolubles;
9. confirmer les attributs de cookie en HTTPS (`HttpOnly`, `Secure`, `SameSite=Lax`);
10. documenter uniquement les métadonnées non sensibles retenues : issuer, discovery URL, redirect URI, scopes et procédure de provisionnement.

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
- les rôles proviennent uniquement de `erp_user_directory.roles_json` et l'activation locale ADMIN demeure obligatoire;
- `name`, `email` et les display names sont descriptifs uniquement et ne sont jamais des clés de jointure;
- `sub == UserID` ne doit jamais être supposé et aucune convention comme `@Company` ne doit être inversée.

Le client OIDC extrait `preferred_username` pour le fonctionnement normal même lorsque les diagnostics de claims sont désactivés. Le provisionnement contrôlé est distinct de l'ancien `RESOURCEPLANNER_OIDC_AUTO_PROVISION`; ce dernier ne peut pas contourner une correspondance `preferred_username` présente mais non autorisée.

Validation réelle encore requise : deuxième compte OIDC, cookies HTTPS/Secure sur l'environnement cible, production et SQL Server. Aucun de ces points n'est déclaré validé par cette livraison.

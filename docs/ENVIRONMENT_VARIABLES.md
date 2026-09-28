# Variables d'environnement du runtime V2

Ce document inventorie les variables du runtime Web. Il indique **quoi configurer**, pas les valeurs réelles. Les secrets ne doivent pas être committés dans Git, copiés dans les logs ou exposés par `/health` et `/ready`.

## Base de données et processus

| Variable | Usage | Requise |
| --- | --- | --- |
| `RESOURCEPLANNER_DATABASE_URL` | URL SQLAlchemy de la base | Oui pour `python -m app.server`; `Lancer_Web.bat` fournit SQLite local si absente |
| `RESOURCEPLANNER_HOST` | Adresse d'écoute | Non, défaut `127.0.0.1` |
| `RESOURCEPLANNER_PORT` | Port HTTP | Non, défaut `8000` |
| `RESOURCEPLANNER_LOG_LEVEL` | Niveau Uvicorn | Non, défaut `info` |
| `RESOURCEPLANNER_ACTOR_NAME` | Nom technique de l'acteur local | Non |
| `RESOURCEPLANNER_FRONTEND_DIST` | Build React servi par FastAPI | Défini automatiquement par `Lancer_Web.bat` |

## Authentification locale

| Variable | Usage |
| --- | --- |
| `RESOURCEPLANNER_AUTH_MODE` | `local` ou `oidc`; défaut `local` |
| `RESOURCEPLANNER_LOCAL_AUTH_NAME` | Nom affiché en mode local |
| `RESOURCEPLANNER_LOCAL_AUTH_EMAIL` | Courriel descriptif local |
| `RESOURCEPLANNER_LOCAL_AUTH_ROLES` | Rôles locaux |
| `RESOURCEPLANNER_ALLOW_LOCAL_AUTH_NETWORK` | Autorise explicitement le mode local hors loopback |
| `RESOURCEPLANNER_DEV_USER_SWITCHER` | Active le sélecteur d’identités de développement; défaut `false`, accepté uniquement avec `RESOURCEPLANNER_AUTH_MODE=local` |

Le mode local refuse par défaut une écoute réseau. Ne pas utiliser l'override comme substitut à l'authentification réelle lors d'une exposition réseau.

Avec `RESOURCEPLANNER_DEV_USER_SWITCHER=false`, le principal local est résolu directement depuis `RESOURCEPLANNER_LOCAL_AUTH_*` et ne nécessite aucun `AppUser` persistant. Ce mécanisme reste strictement local/développement et n'est pas le compte break-glass de production prévu par #457.

Le sélecteur de développement crée une session locale HttpOnly vers un `AppUser` existant et réutilise les rôles/permissions backend. Il n'émule pas OIDC et le serveur refuse de démarrer si le switcher est demandé en mode `oidc`. Le Compose Synology le force explicitement à `false`.

## OIDC

Ces variables sont requises ou pertinentes lorsque `RESOURCEPLANNER_AUTH_MODE=oidc`.

| Variable | Usage | Secret |
| --- | --- | --- |
| `RESOURCEPLANNER_OIDC_DISCOVERY_URL` | Document de découverte OIDC | Non |
| `RESOURCEPLANNER_OIDC_CLIENT_ID` | Identifiant client | Non |
| `RESOURCEPLANNER_OIDC_CLIENT_SECRET` | Credential client lorsque requis | **Oui** |
| `RESOURCEPLANNER_OIDC_REDIRECT_URI` | Callback de l'application | Non |
| `RESOURCEPLANNER_OIDC_SCOPES` | Scopes, incluant obligatoirement `openid` | Non |
| `RESOURCEPLANNER_OIDC_COOKIE_NAME` | Nom du cookie de session | Non |
| `RESOURCEPLANNER_OIDC_SESSION_HOURS` | Durée de session | Non |
| `RESOURCEPLANNER_OIDC_SECURE_COOKIE` | Force le cookie Secure | Non |
| `RESOURCEPLANNER_OIDC_AUTO_PROVISION` | Active la politique d'auto-provisionnement prévue | Non |

Le préflight de démarrage vérifie la **configuration** OIDC, mais ne contacte pas le fournisseur d'identité. La validation réelle reste un smoke séparé.

## Acumatica

### Projet OData actuellement implémenté

Les synchronisations métier Acumatica utilisent **OData**. Le contrat projet est documenté dans [ACUMATICA_ODATA_CONTRACT.md](ACUMATICA_ODATA_CONTRACT.md) et le feed projet est fixé à `/oDATA/RP_Projects`.

| Variable | Usage actuel | Secret |
| --- | --- | --- |
| `RESOURCEPLANNER_ACUMATICA_BASE_URL` | Base de l'instance Acumatica; active la source projet OData | Non, mais ne pas exposer l'URL réelle dans les diagnostics publics |
| `RESOURCEPLANNER_ACUMATICA_USERNAME` | Compte OData utilisé par HTTP Basic; compte nominatif temporaire en développement, compte de service cible | Donnée sensible |
| `RESOURCEPLANNER_ACUMATICA_PASSWORD` | Mot de passe HTTP Basic OData | **Oui** |
| `RESOURCEPLANNER_ACUMATICA_PAGE_SIZE` | Taille de page cliente pour `$top/$skip`; défaut 100 | Non |
| `RESOURCEPLANNER_ACUMATICA_TIMEOUT_SECONDS` | Timeout HTTP du lecteur OData; défaut 30 s | Non |

Le smoke réel #207B du 2026-09-24 a confirmé **HTTP Basic** pour `/oDATA/RP_Projects`. Le runtime exige donc username + mot de passe ensemble lorsque l'intégration est activée. Ils sont transmis à `httpx.BasicAuth` et ne sont jamais inclus dans `safe_summary()`, les routes de statut ou les logs.

La pagination réelle observée utilise `$orderby=ProjectId asc` avec `$top` et `$skip`; aucun lien Atom `rel="next"` n'a été observé. La valeur par défaut 100 est une taille de lot cliente configurable, pas une affirmation sur une limite maximale serveur.

### Variables REST historiques

Les variables suivantes restent reconnues comme noms historiques afin de ne pas casser brutalement des environnements ou tests périphériques, mais elles **ne pilotent plus le nouveau chemin projet OData** :

- `RESOURCEPLANNER_ACUMATICA_ACCESS_TOKEN`;
- `RESOURCEPLANNER_ACUMATICA_ENDPOINT`;
- `RESOURCEPLANNER_ACUMATICA_VERSION`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_ENTITY`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_NUMBER_FIELD`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_NAME_FIELD`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_CLIENT_FIELD`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_MANAGER_FIELD`;
- `RESOURCEPLANNER_ACUMATICA_PROJECT_STATUS_FIELD`;
- `RESOURCEPLANNER_ACUMATICA_PAGE_SIZE`.

Le nom d'hôte réel, les comptes nominatifs temporaires et le futur compte de service restent dans la configuration d'environnement/secrets, jamais dans Git. L'absence de `RESOURCEPLANNER_ACUMATICA_BASE_URL` laisse l'intégration Acumatica désactivée et n'empêche pas les fonctions locales.

## Configuration chiffrée / SMTP

| Variable | Usage | Secret |
| --- | --- | --- |
| `RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY` | Clé maîtresse Fernet utilisée pour chiffrer les secrets configurés depuis l’interface administrateur | **Oui** |

La clé maîtresse reste **hors de la base de données** et ne doit pas être committée. Pour en générer une, utiliser un outil d’exploitation sécurisé produisant une clé Fernet URL-safe de 32 octets. La page **Configuration** peut enregistrer les paramètres SMTP non secrets sans cette variable, mais elle refuse d’enregistrer ou de déchiffrer un credential SMTP lorsque la clé est absente.

Les paramètres SMTP (hôte, port, STARTTLS ou SSL/TLS, expéditeur, Reply-To, activation) sont ensuite administrés dans l’application. Le secret SMTP est chiffré avant stockage SQL et l’API retourne uniquement `password_configured=true/false`, jamais sa valeur.

## Microsoft 365 / Graph

| Variable | Usage | Secret |
| --- | --- | --- |
| `RESOURCEPLANNER_M365_TENANT_ID` | Tenant | Non |
| `RESOURCEPLANNER_M365_CLIENT_ID` | Application cliente | Non |
| `RESOURCEPLANNER_M365_CLIENT_SECRET` | Credential applicatif | **Oui** |
| `RESOURCEPLANNER_M365_MAILBOX` | Boîte utilisée pour les brouillons | Donnée de configuration |
| `RESOURCEPLANNER_M365_GRAPH_BASE_URL` | Base Graph | Non |
| `RESOURCEPLANNER_M365_AUTHORITY_HOST` | Autorité OAuth | Non |
| `RESOURCEPLANNER_M365_TIMEOUT_SECONDS` | Timeout Graph | Non |

M365 reste optionnel pour la planification locale. Le runtime ne contacte pas Graph pendant `/health` ou `/ready`.

## Embedding / Teams

| Variable | Usage |
| --- | --- |
| `RESOURCEPLANNER_FRAME_ANCESTORS` | Origines explicitement autorisées par CSP `frame-ancestors` |
| `RESOURCEPLANNER_OIDC_COOKIE_SAMESITE` | `lax`, `strict` ou `none` |

Aucun wildcard n'est accepté pour `frame-ancestors`. Hors localhost, les origines doivent être HTTPS.

## Outils de vérification

`Verifier_Web.bat` utilise aussi :

| Variable | Usage |
| --- | --- |
| `RESOURCEPLANNER_BASE_URL` | Origine à tester; défaut `http://127.0.0.1:8000` |

Cette variable appartient au smoke d'exploitation, pas au serveur.

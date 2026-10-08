# Runbook — FastAPI / SQL et runtime Web


> **Production / premier go-live :** le runtime ne doit pas être initialisé avec les seeds de démonstration. Avant production, suivre #457 et [SQL_CUTOVER_RUNBOOK.md](SQL_CUTOVER_RUNBOOK.md) pour créer la baseline propre, la base vide et le bootstrap administrateur break-glass indépendant d'OIDC.

Ce document décrit la frontière serveur canonique de RessourcePlanner. Le backend FastAPI ne dépend ni de NiceGUI, ni d'Excel. Le mode d'exploitation Web complet est détaillé dans [`WEB_RUNTIME.md`](WEB_RUNTIME.md).

## État actuel

Deux modes utilisent le même backend :

1. **API seule** : `python -m app.server`, sans frontend si `RESOURCEPLANNER_FRONTEND_DIST` est absente;
2. **Web déployé** : React/Nginx + FastAPI via Docker Compose; en CLI locale, `python -m app.server` peut servir React avec `RESOURCEPLANNER_FRONTEND_DIST`.

Deux modes d'identité sont disponibles :

- `local` pour le développement/test explicite;
- `oidc` pour l'Authorization Code Flow vers Acumatica avec PKCE S256 et session serveur.

En production OIDC, #457 ajoute un **login break-glass opt-in** qui authentifie un `AppUser ADMIN` réservé sans contacter le fournisseur OIDC. Ce n'est pas un troisième `RESOURCEPLANNER_AUTH_MODE`. Voir [BREAK_GLASS_ADMIN.md](BREAK_GLASS_ADMIN.md).

SQL Server est la base de référence pour les environnements intégrés/staging/production (ADR-011). SQLite reste un dialecte local/test. La validation réelle ENV-162 sur SQL Server 2017 a confirmé les migrations, le préflight FastAPI, le runtime Uvicorn, commit/rollback et le CAS global. Le packaging Docker embarque désormais le même chemin driver validé : `pyodbc==5.3.0` et Microsoft ODBC Driver 18. L'implémentation OIDC est couverte par un fournisseur simulé en tests; la validation contre l'instance Acumatica réelle reste dépendante de ses paramètres issuer/client/redirect.

## 1. Dépendances serveur

Le profil canonique est :

```bat
python -m pip install -r requirements-server.txt -c constraints-release.txt
```

`requirements-server.txt` reste indépendant de NiceGUI, xlwings et openpyxl. Il inclut `pyodbc==5.3.0`, version validée sur la VM Ubuntu cible. L'image backend installe `msodbcsql18` depuis le dépôt Debian officiel Microsoft; la sous-version système exacte n'est pas figée dans Git afin de rester compatible avec la version Debian portée par `python:3.12-slim`.

Pour une installation hors Docker, créer un environnement Python isolé, installer le profil ci-dessus, puis dans `frontend/` exécuter `npm install --no-audit --no-fund` et `npm run build`. Aucun installeur BAT n'est requis (voir [REACT_V2_DEV.md](REACT_V2_DEV.md)).

## 2. Configuration par variables d'environnement

Le serveur n'utilise pas `app_config.json`. Sa configuration d'exploitation vient de variables d'environnement.

| Variable | Requise | Défaut | Rôle |
|---|---:|---|---|
| `RESOURCEPLANNER_DATABASE_URL` | oui pour `python -m app.server` | — | URL SQLAlchemy de la base autoritaire |
| `RESOURCEPLANNER_HOST` | non | `127.0.0.1` | interface d'écoute Uvicorn |
| `RESOURCEPLANNER_PORT` | non | `8000` | port TCP |
| `RESOURCEPLANNER_LOG_LEVEL` | non | `info` | niveau Uvicorn |
| `RESOURCEPLANNER_ACTOR_NAME` | non | `api` | identité technique de fallback |
| `RESOURCEPLANNER_FRONTEND_DIST` | non | — | build React à servir; s'il est défini, le build doit être valide |
| `RESOURCEPLANNER_AUTH_MODE` | non | `local` | `local` ou `oidc` |

Les variables Acumatica de synchronisation projets documentées dans `ACUMATICA_PHASE1.md` restent séparées de l'authentification utilisateur OIDC.

`RESOURCEPLANNER_DATABASE_URL`, les credentials OIDC et les tokens d'intégration peuvent contenir des secrets. Ne jamais les committer.

## 3. Authentification locale

Le mode local sert uniquement au développement et aux smokes contrôlés. Il crée un principal local avec les rôles configurés côté serveur; aucun rôle envoyé par le navigateur n'est accepté.

Variables utiles :

- `RESOURCEPLANNER_LOCAL_AUTH_NAME`;
- `RESOURCEPLANNER_LOCAL_AUTH_EMAIL`;
- `RESOURCEPLANNER_LOCAL_AUTH_ROLES`;
- `RESOURCEPLANNER_ALLOW_LOCAL_AUTH_NETWORK`.

Par défaut, le mode local refuse une écoute réseau non loopback. Une ouverture réseau exige un opt-in explicite et ne constitue pas le mode de production cible.

## 4. Authentification OIDC Acumatica

Pour activer OIDC, définir `RESOURCEPLANNER_AUTH_MODE` à `oidc` et configurer :

- `RESOURCEPLANNER_OIDC_DISCOVERY_URL`;
- `RESOURCEPLANNER_OIDC_CLIENT_ID`;
- `RESOURCEPLANNER_OIDC_CLIENT_SECRET` lorsque le client enregistré l'exige;
- `RESOURCEPLANNER_OIDC_REDIRECT_URI`;
- `RESOURCEPLANNER_OIDC_SCOPES` — doit contenir `openid`, défaut `openid profile email`;
- `RESOURCEPLANNER_OIDC_COOKIE_NAME` — optionnel;
- `RESOURCEPLANNER_OIDC_SESSION_HOURS` — optionnel, défaut 8;
- `RESOURCEPLANNER_OIDC_SECURE_COOKIE` — optionnel, par défaut activé si le redirect URI est HTTPS.

Le flux utilise :

1. découverte OIDC;
2. Authorization Code;
3. PKCE S256;
4. validation de la signature JWKS, issuer, audience, expiration et nonce;
5. résolution de `(issuer, subject)` vers `app_users`;
6. création d'une session opaque côté serveur;
7. cookie `HttpOnly`, `SameSite=Lax` et `Secure` selon la configuration.

Seul le hash SHA-256 du token de session opaque est persisté. L'access token et l'id token OIDC ne sont ni stockés dans React ni persistés dans les tables de session RessourcePlanner.

### Provisionnement utilisateur

L'identité externe n'accorde jamais elle-même les rôles métier. Avant la première connexion réelle, l'utilisateur doit exister dans `app_users` avec le couple exact `(issuer, subject)`, être actif et posséder au moins un rôle RessourcePlanner.

Les rôles/permissions restent autoritaires dans RessourcePlanner. Un utilisateur Acumatica valide mais non provisionné reçoit un refus explicite et aucune session applicative.

### Séparation avec la synchro ERP

Les credentials OIDC utilisateur sont indépendants de `RESOURCEPLANNER_ACUMATICA_ACCESS_TOKEN`, utilisé par la synchronisation serveur-à-serveur des projets. Ne pas réutiliser un token utilisateur comme credential de synchronisation ERP.

## 4.1. Administrateur break-glass

Le bootstrap et la rotation sont des commandes d'exploitation séparées des migrations et des seeds :

```bash
python tools/bootstrap_production_admin.py
python tools/bootstrap_production_admin.py --rotate-secret
```

Le runtime n'expose ce login que lorsque `RESOURCEPLANNER_BREAK_GLASS_ENABLED=true` en mode `oidc`. Le secret est fourni hors Git, hashé avec scrypt puis oublié; le backend normal ne reçoit pas sa valeur en clair.

Les sessions break-glass utilisent `auth_sessions`, le cookie opaque serveur et le CSRF existants. La politique initiale bloque le credential après 5 échecs dans 15 minutes pendant 15 minutes. Les succès/échecs/rotations sont audités sans secret.

La procédure complète de création, test, rotation/réinitialisation, panne OIDC et rollback est dans [BREAK_GLASS_ADMIN.md](BREAK_GLASS_ADMIN.md).

## 5. Migrations

Le serveur Python normal n'exécute jamais Alembic automatiquement :

```bat
python -m alembic upgrade head
```

Pour le premier go-live, l'historique Alembic pré-production est remplacé par la baseline statique unique `v2_production_baseline` (#457C).
Elle représente le schéma canonique complet et ne crée aucune identité, donnée `DEMO-*` ni credential break-glass; seul le singleton technique `planning_mutation_state/GLOBAL` est initialisé.
Après le premier go-live, cette baseline devient immuable et toute évolution de schéma reprend sous forme de migration additive normale.

Le contrat détaillé est documenté dans [`SQL_CUTOVER_RUNBOOK.md`](SQL_CUTOVER_RUNBOOK.md).

Les anciens fallbacks SQLite des launchers BAT ont été retirés. Configurer `RESOURCEPLANNER_DATABASE_URL` explicitement et appliquer les migrations via `python -m alembic upgrade head` seulement dans l'environnement prévu. En production SQL Server, la migration requiert la procédure et la sauvegarde d'exploitation.

## 6. Démarrage API seul

Exemple :

```bat
set RESOURCEPLANNER_DATABASE_URL=sqlite:///C:/Temp/resourceplanner_server.db
python -m app.server
```

Endpoints principaux :

- `/health`;
- `/api/v1/...`;
- `/docs`;
- `/openapi.json`.

Dans ce mode, `/` retourne 404 si aucun build frontend n'est configuré.

## 7. Démarrage Web autonome

Le runtime normal utilise `docker compose up -d --build` (React/Nginx + FastAPI). En CLI locale, après le build `frontend/dist`, définir `RESOURCEPLANNER_FRONTEND_DIST` sur son chemin absolu et exécuter `python -m app.server` avec une base configurée et les migrations explicitement appliquées. Sans cette variable, le serveur est API seule; un build explicitement demandé mais absent empêche le démarrage.

En mode OIDC, `/api/v1/auth/me` retourne `authentication_required` sans session. La connexion passe par `/api/v1/auth/login`, la déconnexion par `POST /api/v1/auth/logout`.

## 8. Préflights et smokes

### Backend configuré

```bat
python tools\check_server_runtime.py
```

Ce préflight vérifie notamment `/health`, les lectures de base et OpenAPI contre la base configurée.

### Isolation des dépendances serveur

```bat
python tools\check_server_dependency_isolation.py
```

La CI exécute ce test avec uniquement `requirements-server.txt` installé afin de garantir que FastAPI/SQL ne dépend pas du runtime V1.

### Runtime Web complet

Après `npm run build` :

```bat
python tools\check_web_runtime.py
```

Ce smoke vérifie le `index.html`, un asset Vite réel, `/health` et l'isolation du namespace `/api/v1`.

### OIDC

La CI utilise un fournisseur OIDC simulé et couvre :

- URL Authorization Code + PKCE S256;
- id token signé;
- issuer/audience/expiration/nonce;
- refus d'un algorithme non signé;
- transaction state à usage unique;
- utilisateur local autorisé/non autorisé;
- session valide, expirée et révoquée;
- login, `/me` et logout;
- cookie HttpOnly et absence de token OIDC dans React.

Ce smoke simulé ne remplace pas la validation finale contre l'instance Acumatica réelle.

## 9. SQL Server — validation réelle

L'accès au serveur SQL cible est disponible depuis le 2026-09-29. #162 est maintenant exécutable comme validation environnementale réelle :

1. identifier le driver ODBC SQL Server et sa version;
2. installer/valider `pyodbc`;
3. tester une URL SQLAlchemy SQL Server sans l'inscrire dans Git;
4. exécuter `alembic upgrade head` sur une base de développement dédiée;
5. exécuter `python tools\check_server_runtime.py`;
6. démarrer le runtime Web sur la **VM Ubuntu cible via Docker Compose**; aucun launcher BAT Windows n'est requis;
7. valider `/`, `/health`, les lectures et au moins une mutation métier;
8. épingler le driver retenu après validation.

## 10. Relation avec le cutover SQL Server

Le premier go-live est décrit dans [`SQL_CUTOVER_RUNBOOK.md`](SQL_CUTOVER_RUNBOOK.md). Le runtime Web autonome ne déclare pas à lui seul SQL Server autoritaire.

La décision actuelle est de **ne pas importer l'historique Excel/V1**. Le premier go-live utilise une base SQL Server neuve et la baseline V2 propre de #457.

Ordre de haut niveau :

1. terminer la baseline et les garde-fous pré-go-live de #457;
2. créer une base SQL Server neuve et vide;
3. appliquer explicitement `alembic upgrade head`;
4. exécuter #492 pour transférer uniquement les données SQLite V2 retenues, avec IDs stables et exclusions dev/sessions/secrets;
5. exécuter le bootstrap administrateur distinct des seeds;
6. alimenter/réconcilier les référentiels réels nécessaires;
7. exécuter les préflights et smokes lecture/mutation/rollback/concurrence;
8. démarrer React + FastAPI sur SQL Server;
9. seulement ensuite déclarer SQL Server autoritaire pour les nouvelles opérations selon #208.

SQLite reste un outil local/test et ne constitue pas un fallback de production après le cutover.

## 11. Cible de déploiement production

La cible de déploiement privilégiée est désormais une **VM Ubuntu dédiée hébergée sur le Synology**, et non Synology Container Manager exécutant directement les conteneurs.

Le principe d'exploitation est :

```text
Synology
└── VM Ubuntu
    ├── Docker Engine
    ├── Docker Compose
    ├── frontend React / Nginx
    └── backend FastAPI
            ↓
        SQL Server externe
```

Cette séparation permet de conserver un environnement Linux standard, de simplifier les mises à jour Docker et de découpler l'application du runtime DSM.

Le NAS reste l'hôte de virtualisation et peut fournir les ressources de stockage/sauvegarde nécessaires, mais il ne constitue plus le runtime applicatif direct.

Voir [`DEPLOYMENT_UBUNTU_VM.md`](DEPLOYMENT_UBUNTU_VM.md) pour la procédure détaillée.

## 12. Runtime V1 legacy

Le runtime NiceGUI/Excel V1 et ses anciens launchers Windows ont été retirés en 336B. Les commandes supportées sont Compose et la CLI FastAPI/Vite documentées ci-dessus.

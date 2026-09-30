# Runbook — FastAPI / SQL et runtime Web


> **Production / premier go-live :** le runtime ne doit pas être initialisé avec les seeds de démonstration. Avant production, suivre #457 et [SQL_CUTOVER_RUNBOOK.md](SQL_CUTOVER_RUNBOOK.md) pour créer la baseline propre, la base vide et le bootstrap administrateur break-glass indépendant d'OIDC.

Ce document décrit la frontière serveur canonique de RessourcePlanner. Le backend FastAPI ne dépend ni de NiceGUI, ni d'Excel. Le mode d'exploitation Web complet est détaillé dans [`WEB_RUNTIME.md`](WEB_RUNTIME.md).

## État actuel

Deux modes utilisent le même backend :

1. **API seule** : `python -m app.server`, sans frontend si `RESOURCEPLANNER_FRONTEND_DIST` est absente;
2. **Web autonome** : `Lancer_Web.bat`, qui sert le build React et l'API sur la même origine.

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

Pour une installation Web Windows reproductible, utiliser plutôt :

```bat
Installer_Web.bat
```

L'installateur crée `.venv-web` et construit `frontend\dist` avec Node.js 22.

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

La migration `0011_oidc_sessions` ajoute les transactions de login OIDC à usage unique et les sessions serveur.

`Lancer_Web.bat` et `Lancer_Serveur.bat` conservent une exception de commodité **uniquement pour leur fallback SQLite local**, lorsque `RESOURCEPLANNER_DATABASE_URL` n'était pas définie avant le lancement.

Avec une base explicitement configurée — notamment SQL Server — les migrations restent toujours explicites.

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

Après :

```bat
Installer_Web.bat
```

lancer :

```bat
Lancer_Web.bat
```

Le lanceur définit `RESOURCEPLANNER_FRONTEND_DIST` vers `frontend\dist`, vérifie le build, puis démarre le même `app.server`. L'interface est disponible par défaut à `http://127.0.0.1:8000/`.

Le runtime Web refuse de démarrer si le build React demandé est absent ou incomplet; il ne retombe pas silencieusement en API seule.

En mode OIDC, une requête sans session vers `/api/v1/auth/me` retourne `authentication_required`; React propose alors la connexion via `/api/v1/auth/login`. La déconnexion passe par `POST /api/v1/auth/logout`, qui révoque la session SQL avant de supprimer le cookie.

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
6. démarrer le runtime Web dans la **VM Ubuntu cible via Docker Compose**; `Lancer_Web.bat` reste un chemin local Windows, pas le mode de production privilégié;
7. valider `/`, `/health`, les lectures et au moins une mutation métier;
8. épingler le driver retenu après validation.

## 10. Relation avec le cutover SQL Server

Le premier go-live est décrit dans [`SQL_CUTOVER_RUNBOOK.md`](SQL_CUTOVER_RUNBOOK.md). Le runtime Web autonome ne déclare pas à lui seul SQL Server autoritaire.

La décision actuelle est de **ne pas importer l'historique Excel/V1**. Le premier go-live utilise une base SQL Server neuve et la baseline V2 propre de #457.

Ordre de haut niveau :

1. terminer la baseline et les garde-fous pré-go-live de #457;
2. créer une base SQL Server neuve et vide;
3. appliquer explicitement `alembic upgrade head`;
4. exécuter le bootstrap administrateur distinct des seeds;
5. alimenter les référentiels réels nécessaires;
6. exécuter les préflights et smokes lecture/mutation/rollback/concurrence;
7. démarrer React + FastAPI sur SQL Server;
8. seulement ensuite déclarer SQL Server autoritaire pour les nouvelles opérations selon #208.

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

`Lancer_Application.bat` est conservé comme alias de compatibilité explicite vers `Lancer_Application_Legacy.bat`.

La V1 utilise `.venv`, `main.py`, NiceGUI et Excel. Elle reste disponible uniquement pour la transition et n'est plus le mode cible.

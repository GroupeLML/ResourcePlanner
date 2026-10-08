# V2 — exploitation locale, diagnostic et rollback

Ce runbook décrit les procédures d'exploitation après le cutover SQL Server autoritaire. Le runtime de production est React/Nginx + FastAPI sous Docker Compose; les anciennes commandes BAT ne sont plus supportées.

## 1. Modèle de probes

Le runtime distingue trois concepts.

### `GET /health` — liveness

`/health` confirme uniquement que le processus FastAPI répond :

```json
{"status": "ok", "api": "v1"}
```

Il ne contacte ni SQL, ni OIDC, ni Acumatica, ni Microsoft 365. Un problème de base ne doit donc pas transformer un processus vivant en faux « down ».

### `GET /ready` — readiness locale obligatoire

`/ready` vérifie :

- que la base est joignable;
- qu'elle est interrogeable;
- que `alembic_version` existe;
- que la révision correspond exactement à la tête Alembic du code déployé.

Si une de ces conditions échoue, le probe retourne HTTP 503 avec une raison technique sûre, par exemple `migration_required` ou `database_unavailable`. L'URL de connexion et les détails DBAPI ne sont jamais retournés.

### Dépendances externes

`/ready` expose uniquement des booléens de configuration pour OIDC, Acumatica et M365. Il **ne les ping pas**.

Acumatica et M365 sont optionnels pour les fonctions locales. Leur panne ne doit pas empêcher la consultation et la planification locales. OIDC est nécessaire à l'authentification lorsqu'il est choisi, mais son accès réseau est validé par un smoke d'authentification séparé plutôt que par le probe de readiness.

## 2. Installation et construction

Le runtime autoritaire est construit et déployé avec Docker Compose, sur la VM cible. Avant toute mise en service, configurer l'URL SQL Server, les secrets et le réseau **hors Git** selon [DEPLOYMENT_UBUNTU_VM.md](DEPLOYMENT_UBUNTU_VM.md).

```bash
docker compose config
docker compose up -d --build
```

Pour un poste de développement hors Docker (Python 3.12, Node 22), installer le profil serveur dans un environnement Python isolé et construire React :

```bash
python -m pip install -r requirements-server.txt -c constraints-release.txt
cd frontend
npm install --no-audit --no-fund
npm run build
cd ..
python tools/check_installed_web.py
```

Le profil `requirements-server.txt` ne doit pas installer NiceGUI, xlwings ni openpyxl. Le chemin natif n'est pas le déploiement de production.

## 3. Démarrage

### Exploitation Compose

```bash
docker compose up -d --build
docker compose ps
```

Le service `migrate` exécute Alembic avant le backend. Sur SQL Server, préparer et approuver toute migration et une sauvegarde restaurable **avant** le déploiement. Aucun seed de démonstration n'est exécuté au démarrage normal.

### CLI de développement : base explicitement choisie

Définir `RESOURCEPLANNER_DATABASE_URL` pour la base locale visée. Une base SQLite de **développement** peut être configurée en PowerShell :

```powershell
$env:RESOURCEPLANNER_DATABASE_URL = "sqlite:///./resourceplanner_server.db"
python -m alembic upgrade head
python tools/check_server_runtime.py
python -m app.server
```

Pour servir le build React en plus de l'API, définir `RESOURCEPLANNER_FRONTEND_DIST` vers le chemin absolu de `frontend/dist` avant de démarrer FastAPI. Sans cette variable, l'API seule reste disponible. Il n'existe plus de fallback SQLite implicite; les migrations SQL Server sont toujours une étape d'exploitation explicite.

## 4. Diagnostics de démarrage

`check_server_runtime.py` distingue les familles suivantes :

- `configuration_error` : variable requise absente ou valeur invalide;
- `driver_error` : dialecte/DBAPI/ODBC indisponible;
- `connectivity_error` : driver chargé mais base non joignable/interrogeable;
- `migration_error` : schéma non initialisé ou révision Alembic incorrecte;
- `readiness_error` : runtime créé mais contrat de readiness non satisfait;
- `technical_error` : erreur non classée.

Les erreurs inattendues n'affichent pas le message brut d'une exception DBAPI afin d'éviter de divulguer une chaîne de connexion, un utilisateur, un host ou un secret.

En mode OIDC, le préflight n'essaie pas de simuler une session utilisateur et ne contacte pas l'IdP. Les lectures métier authentifiées sont alors marquées `skipped_oidc`.

## 5. Smoke après installation et démarrage

Pour vérifier un build React local hors Docker :

```bash
python tools/check_installed_web.py
python tools/check_web_runtime.py
```

Le premier contrôle vérifie le profil serveur et le build; le second exerce `/`, les assets, `/health`, `/ready` et l'isolation du namespace API avec une base de test.

Contre une instance déjà démarrée, exécuter :

```bash
python tools/smoke_running_web.py --base-url http://127.0.0.1:8080
```

Utiliser `http://127.0.0.1:8000` pour une instance FastAPI locale servant le frontend. `RESOURCEPLANNER_BASE_URL` peut également être fourni au script. Aucun compte utilisateur n'est nécessaire pour les probes publics; l'authentification et les mutations sont validées séparément.

## 6. Logs et métriques techniques

Le journal de performance par défaut se trouve sous :

```text
%LOCALAPPDATA%\RessourcePlanner\logs\performance.jsonl
```

À défaut de `LOCALAPPDATA`, le runtime utilise le répertoire utilisateur `.resourceplanner/logs`.

Politique actuelle :

- fichier JSONL;
- rotation approximative à 1 000 000 octets;
- trois archives conservées : `.1`, `.2`, `.3`;
- `.1` est l'archive la plus récente;
- le lecteur de diagnostics traverse maintenant les archives et le fichier courant.

Le schéma contient uniquement des données techniques : route **template**, statuts, durées, compteurs SQL, fingerprints unidirectionnels, compteurs externes et type d'erreur. Les paramètres SQL, identifiants métier, noms de ressource, numéros de projet et payloads externes ne doivent jamais être écrits dans ce journal.

Pour les diagnostics courants sous Compose, consulter `docker compose logs --tail=100 backend` et la collecte de logs configurée. Les outils historiques de lecture de performances NiceGUI ont été retirés; ne pas les recréer dans le chemin supporté.

## 7. Sauvegarde SQLite locale

Pour une sauvegarde de développement cohérente :

1. arrêter le serveur local (`python -m app.server`), sans écriture concurrente;
2. créer un répertoire de sauvegarde hors du fichier actif;
3. utiliser l'API de backup SQLite, même si une simple copie fonctionnerait normalement après arrêt;
4. vérifier l'intégrité de la sauvegarde;
5. conserver le commit/release applicatif associé.

Exemple :

```bat
mkdir backups
.venv-web\Scripts\python.exe -c "import sqlite3; s=sqlite3.connect(r'resourceplanner_server.db'); d=sqlite3.connect(r'backups\resourceplanner_server_backup.db'); s.backup(d); d.close(); s.close()"
.venv-web\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect(r'backups\resourceplanner_server_backup.db'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

Le résultat attendu du second appel est `ok`.

## 8. Restauration SQLite locale

1. arrêter le runtime;
2. sauvegarder/renommer la base actuelle pour permettre un retour arrière;
3. vérifier `PRAGMA integrity_check` sur le fichier à restaurer;
4. remplacer `resourceplanner_server.db`;
5. vérifier la révision :

```bat
.venv-web\Scripts\python.exe -m alembic current
```

6. exécuter :

```bat
.venv-web\Scripts\python.exe tools\check_server_runtime.py
```

7. démarrer la CLI locale puis lancer `python tools/smoke_running_web.py --base-url http://127.0.0.1:8000`.

Ne jamais écraser une base active pendant qu'Uvicorn l'utilise.

## 9. Remplacement de SQLite par SQL Server

La bascule n'est pas une conversion implicite du fichier SQLite.

Procédure :

1. arrêter le runtime et conserver une sauvegarde SQLite;
2. valider le driver ODBC approuvé sur la cible;
3. installer `pyodbc` seulement après cette validation;
4. configurer `RESOURCEPLANNER_DATABASE_URL` vers SQL Server;
5. exécuter `tools\check_sqlserver_readiness.py`;
6. exécuter `alembic upgrade head` sur la nouvelle base;
7. exécuter `tools\check_server_runtime.py`;
8. exécuter les smokes SQL Server lecture seule et rollback;
9. migrer/importer les données métier selon la procédure de cutover, séparément du schéma;
10. démarrer l'application puis exécuter `python tools/smoke_running_web.py --base-url <origine-deployee>`.

Voir aussi `V2_SQLSERVER_READINESS.md`.

## 10. Mise à jour applicative

Séquence recommandée :

1. annoncer la fenêtre, stopper les écritures et identifier le tag d'image / commit déployé;
2. sauvegarder la base SQL Server et vérifier que la restauration est possible;
3. valider les changements Alembic, les permissions et l'absence de seed dev;
4. déployer l'image versionnée via Compose (`docker compose config`, puis `docker compose up -d --build`);
5. vérifier `/ready`, `/health`, les logs techniques, l'authentification et les parcours métier;
6. confirmer le go/no-go avant de rouvrir les écritures.

Éviter `docker compose down -v` sur des volumes à conserver. Les builds CLI locaux ne constituent pas une procédure de release.

## 11. Rollback

Le rollback applicatif et celui des données doivent rester coordonnés. Si le schéma et les écritures restent compatibles, revenir au tag applicatif précédent et réexécuter les probes. Si la migration a rendu le schéma ou les données incompatibles, arrêter, restaurer **ensemble** l'ancienne application et la sauvegarde SQL Server correspondante, vérifier Alembic puis les smokes.

Ne pas utiliser `alembic downgrade` automatiquement comme stratégie de rollback; évaluer chaque migration avant la fenêtre de déploiement.

## 12. Checklist de mise en production interne

- [ ] version d'image/tag/commit identifiée, PR et CI requise vertes;
- [ ] Docker Compose configuré sans profil démo;
- [ ] `requirements-server.txt` isolé du runtime V1;
- [ ] `RESOURCEPLANNER_DATABASE_URL` cible SQL Server réelle et driver ODBC validés;
- [ ] secrets et identifiants de production stockés hors Git;
- [ ] authentification OIDC et procédure break-glass validées;
- [ ] sauvegarde restaurable prise avant les migrations;
- [ ] révision `alembic current` conforme à `alembic heads`;
- [ ] `/health` et `/ready` retournent 200;
- [ ] smoke `tools/smoke_running_web.py` vert sur l'origine déployée;
- [ ] parcours de lecture/mutation/rollback SQL Server vérifiés;
- [ ] logs sans PII ni secrets et procédure de rollback connue;
- [ ] responsable go/no-go identifié.

Voir [SQL_CUTOVER_RUNBOOK.md](SQL_CUTOVER_RUNBOOK.md), [BREAK_GLASS_ADMIN.md](BREAK_GLASS_ADMIN.md) et ADR-011/ADR-014.

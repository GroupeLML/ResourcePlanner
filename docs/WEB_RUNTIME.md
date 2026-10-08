# Runtime Web supporté — React/Nginx, FastAPI et SQL Server

En exploitation, React est servi par Nginx et FastAPI expose les routes `/api/v1/*`, `/health`, `/ready` et `/docs`. Docker Compose sur la VM cible constitue le chemin de démarrage supporté; les launchers Windows BAT et NiceGUI/Excel V1 ont été retirés. SQL Server est autoritaire en production (ADR-011); SQLite reste réservé au développement/test.

## Compose

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

En local, ouvrir `http://127.0.0.1:8080/`; `/health` vérifie le processus, `/ready` la base et Alembic. Arrêt : `docker compose down`. **Ne pas** utiliser `down -v` sur des volumes à conserver. Le service `migrate` applique les migrations avant les services dépendants : en production, effectuer sauvegarde et revue de migration avant tout déploiement. Les secrets restent hors Git; le démarrage normal ne crée aucune donnée de démonstration.

## API seule ou same-origin en CLI locale

Avec Python 3.12 et le profil `requirements-server.txt`, configurer `RESOURCEPLANNER_DATABASE_URL` explicitement et migrer la base **locale** avec `python -m alembic upgrade head` avant `python -m app.server`.

Sans `RESOURCEPLANNER_FRONTEND_DIST`, FastAPI fonctionne en API seule. Pour servir React depuis FastAPI à `http://127.0.0.1:8000/`, construire `frontend/dist` via `cd frontend && npm run build`, définir `RESOURCEPLANNER_FRONTEND_DIST` vers son chemin absolu et lancer `python -m app.server`. Un build explicitement demandé mais absent bloque le démarrage (aucun retour silencieux en API seule).

Contrôles utiles après build :

```bash
python tools/check_installed_web.py
python tools/check_server_runtime.py
python tools/check_web_runtime.py
python tools/smoke_running_web.py --base-url http://127.0.0.1:8000
```

Le dernier contrôle vise un processus déjà démarré; sous Compose, utiliser l'origine `http://127.0.0.1:8080`. Pour Vite/HMR, voir [REACT_V2_DEV.md](REACT_V2_DEV.md).

## Import et jeu de démonstration

Les outils d'import ERP sont conservés dans `Dockerfile.importer` et `requirements-importer.txt`. Prévisualisation (montage `imports/` en lecture seule) :

```bash
docker compose run --rm import-projects /imports/projets.xlsx
docker compose run --rm import-tasks /imports/taches.xlsx
```

Ajouter `--apply` uniquement après contrôle de la prévisualisation. Le jeu démo reste volontaire et SQLite-only : `docker compose --profile demo run --rm seed-dev`; il n'est jamais un bootstrap production (#457 / ADR-014).

Voir [V2_RUNTIME_OPERATIONS.md](V2_RUNTIME_OPERATIONS.md), [SERVER_RUNBOOK.md](SERVER_RUNBOOK.md) et [DEPLOYMENT_UBUNTU_VM.md](DEPLOYMENT_UBUNTU_VM.md) pour la mise en service et le rollback.

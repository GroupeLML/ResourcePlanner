# V2 — exploitation et rollback du runtime Web

**Runtime supporté :** React/Nginx + FastAPI sous Docker Compose; SQL Server en production, SQLite uniquement pour développement/tests (ADR-011). Les anciens launchers/installeurs Windows BAT ne sont plus supportés. Voir [WEB_RUNTIME.md](WEB_RUNTIME.md), [SERVER_RUNBOOK.md](SERVER_RUNBOOK.md) et [DEPLOYMENT_UBUNTU_VM.md](DEPLOYMENT_UBUNTU_VM.md).

## Probes et diagnostics

- `GET /health` : liveness du processus FastAPI, sans requête DB, OIDC, M365 ou Acumatica.
- `GET /ready` : accès SQL, présence de `alembic_version`, révision Alembic attendue. Retourne 503 si indisponible.
- Les probes ne doivent jamais divulguer les URL de connexion, secrets ou messages DBAPI.

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 backend frontend
```

Contrôler `/health`, `/ready` et les parcours métier autorisés sur l'origine déployée. Depuis un environnement Python local, `python tools/check_installed_web.py`, `python tools/check_server_runtime.py` et `python tools/check_web_runtime.py` valident respectivement l'isolation, le préflight et le build React. Un smoke du serveur déjà lancé est disponible par `python tools/smoke_running_web.py --base-url http://127.0.0.1:8080`.

## Développement natif et Vite

Avec Python 3.12, Node 22 et une base **locale** explicitement configurée par `RESOURCEPLANNER_DATABASE_URL` :

```bash
python -m pip install -r requirements-server.txt -c constraints-release.txt
cd frontend
npm install --no-audit --no-fund
npm run build
cd ..
python -m alembic upgrade head
python tools/check_server_runtime.py
python -m app.server
```

Cette commande démarre FastAPI en API seule. Pour React same-origin sur le port 8000, définir `RESOURCEPLANNER_FRONTEND_DIST` au chemin absolu de `frontend/dist`. Pour Vite/HMR, garder l'API seule et lancer `cd frontend && npm run dev` dans un autre terminal. En production, la base SQL Server, ses migrations et les secrets sont gérés par le runbook d'exploitation, jamais par un fallback SQLite implicite.

## Imports ERP et démonstration

Prévisualiser les imports avec `docker compose run --rm import-projects /imports/projets.xlsx` ou `docker compose run --rm import-tasks /imports/taches.xlsx`; le montage `imports/` est en lecture seule. L'import reste transactionnel, **sans écriture en prévisualisation**; ajouter `--apply` uniquement après vérification. Le même import peut être lancé via `python tools/import_erp_projects.py <fichier>` et `python tools/import_erp_tasks.py <fichier>` avec une URL DB explicite.

Le seed `docker compose --profile demo run --rm seed-dev` est **opt-in et SQLite-only**; jamais sur SQL Server ni comme bootstrap administrateur. Les identités `DEMO-*` / `urn:resourceplanner:dev` sont interdites au flux de production (ADR-014, #457).

## Mise à jour, sauvegarde, restauration et rollback

1. Identifier le commit/release validé, réserver une fenêtre et rendre les écritures quiescentes.
2. Sauvegarder SQL Server avec l'outil DB approprié et **tester** la restauration; conserver l'image/tag précédent.
3. Examiner les migrations Alembic et confirmer la compatibilité du retour arrière **avant** `docker compose up -d --build`. Le service `migrate` exécute `alembic upgrade head` avant le backend.
4. Vérifier `/ready`, `/health`, l'authentification et les parcours métier indispensables avant réouverture.
5. En cas de rollback, revenir au tag précédent seulement si le schéma est compatible; sinon restaurer **ensemble** la sauvegarde de données et le code précédent.

Pour une sauvegarde **SQLite de développement seulement**, arrêter Uvicorn, utiliser `sqlite3.Connection.backup` et vérifier `PRAGMA integrity_check` = `ok`. Ne pas écraser une base active, ne pas exécuter `alembic downgrade` à l'aveugle et ne jamais utiliser `docker compose down -v` pour une base à conserver.

Les logs doivent contenir des métriques techniques et non des credentials, paramètres SQL ou données métier identifiantes. Le bootstrap break-glass de production est distinct du seed démo; voir [BREAK_GLASS_ADMIN.md](BREAK_GLASS_ADMIN.md).

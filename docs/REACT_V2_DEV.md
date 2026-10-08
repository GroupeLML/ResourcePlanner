# Développement Web V2 — API FastAPI + React/Vite

Le workflow officiel utilise les commandes CLI, sans lanceur Windows. Docker Compose reste le runtime de référence pour l'exploitation; SQLite est réservé au développement/test (ADR-011).

## Préparer le poste

Avec Python 3.12, Node 22 et un environnement Python local activé, depuis la racine :

```bash
python -m pip install -r requirements-server.txt -c constraints-release.txt
cd frontend
npm install --no-audit --no-fund
cd ..
```

## Démarrer l'API en développement SQLite

Exemple PowerShell (terminal 1) :

```powershell
$env:RESOURCEPLANNER_DATABASE_URL = "sqlite:///./resourceplanner_server.db"
python -m alembic upgrade head
python tools/check_server_runtime.py
python -m app.server
```

Sur bash, utiliser `export RESOURCEPLANNER_DATABASE_URL=sqlite:///./resourceplanner_server.db`. La configuration de base et les migrations sont **explicites**; le serveur ne bascule pas silencieusement vers SQLite. Pour SQL Server, suivre le runbook d'exploitation.

## React avec Vite/HMR

Terminal 2 :

```bash
cd frontend
npm run dev
```

Ouvrir `http://127.0.0.1:5173`. Vite relaie `/api` et `/health` vers `http://127.0.0.1:8000`. Garder `RESOURCEPLANNER_FRONTEND_DIST` non définie pour l'API seule.

## Démonstration, exclusivement locale

```bash
docker compose --profile demo run --rm seed-dev
```

Alternative CLI seulement après migration d'une base SQLite locale sélectionnée explicitement :

```bash
python tools/seed_demo_data.py --confirm-dev-only
```

Les données `DEMO-*` et identités `urn:resourceplanner:dev` ne sont jamais destinées à la production ni au bootstrap administrateur (#457, ADR-014).

## Build, smokes et same-origin local

```bash
cd frontend
npm run build
cd ..
python tools/check_installed_web.py
python tools/check_web_runtime.py
```

Pour servir le build React localement par FastAPI, définir `RESOURCEPLANNER_FRONTEND_DIST` vers le chemin absolu de `frontend/dist` (PowerShell : `$env:RESOURCEPLANNER_FRONTEND_DIST = (Resolve-Path .\frontend\dist).Path`) puis démarrer `python -m app.server`. Sans la variable, le serveur expose uniquement l'API. Le runtime Compose sert React par Nginx; il n'utilise pas Vite en exploitation.

Voir [WEB_RUNTIME.md](WEB_RUNTIME.md) et [V2_RUNTIME_OPERATIONS.md](V2_RUNTIME_OPERATIONS.md).

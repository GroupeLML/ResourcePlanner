# RessourcePlanner

RessourcePlanner est une application Web de planification de main-d’œuvre et de capacité pour transformer des besoins projet en demandes, besoins planifiables et quarts réels, avec workflow d’approbation, capacité par ressource et audit.

Le runtime cible est maintenant **React + FastAPI + SQLAlchemy/Alembic**. Le moteur métier Python reste autoritaire pour la planification et la capacité.

> Le README décrit l’état supporté du produit. Le détail des priorités et travaux en cours vit dans le roadmap maître GitHub **#55**.

---

## État actuel

Le socle V2 comprend notamment :

- frontend **React + TypeScript + Vite**;
- API **FastAPI**;
- services métier et moteur de planification en Python;
- persistance **SQLAlchemy 2.x + Alembic**;
- **SQLite** pour le développement local et les tests;
- **SQL Server** comme cible de production;
- packaging **Docker / Docker Compose**, avec **VM Ubuntu dédiée** comme cible de déploiement privilégiée; le NAS Synology héberge la VM mais n'exécute plus directement le runtime applicatif;
- authentification locale de développement, sessions serveur, RBAC et préparation OIDC;
- imports ERP temporaires depuis fichiers Excel/CSV derrière des outils dédiés;
- communications projet avec préparation Microsoft Graph et envoi SMTP explicite;
- CI couvrant backend, frontend, tests navigateur, isolation serveur, readiness SQL Server et smoke Docker.

Le branchement réel à SQL Server reste suivi dans **#162**. Le cutover SQL autoritaire est suivi dans **#208**.

---

## Fonctions principales

RessourcePlanner couvre aujourd’hui les parcours suivants :

- gestion des projets et WorkPackages;
- demandes de main-d’œuvre avec plusieurs lignes de besoins;
- workflow Brouillon / Soumise / approbation / correction;
- périodes, confirmations et règles de réapprobation;
- ressources, classes, compétences et disponibilités;
- transformation d’une demande approuvée en besoins planifiables;
- planning opérationnel avec capacité par ressource et par jour;
- création, déplacement, verrouillage et retour automatique des quarts;
- drag-and-drop dans le Planning React;
- Quick Shift / besoins ad hoc;
- recommandations de ressources;
- vues contextualisées selon l’utilisateur et son rôle;
- contacts métier, responsable opérationnel et coordonnateur;
- historique et audit des mutations métier;
- comparaison du plan actuel avec le plan proposé avant réapprobation;
- préparation et suivi des communications de confirmation de main-d’œuvre.

Les fonctions et priorités détaillées évoluent dans les GitHub Issues; voir **#55** pour l’ordre de travail actuel.

---

## Architecture

### Vue logique

```text
React
  ↓
FastAPI
  ↓
Application / Domain
  ↓
Infrastructure
  ↓
SQLAlchemy / base de données / intégrations externes
```

Principes importants :

- **FastAPI est la frontière de mutation et d’autorisation** du frontend;
- le navigateur ne parle jamais directement à SQL Server, Acumatica ou Microsoft Graph;
- le **backend Python reste autoritaire** pour les règles métier, la planification et la capacité;
- React projette l’état et déclenche des commandes, mais ne duplique pas les règles métier critiques;
- le runtime Web/SQL canonique reste indépendant de NiceGUI et Excel;
- les migrations sont explicites avec Alembic.

La documentation d’architecture et la convention ADR se trouvent dans [`docs/architecture/`](docs/architecture/).

---

## Modèle métier principal

Le modèle actuel sépare l’entête de demande, les lignes planifiables, les besoins matérialisés et les quarts réels :

```text
Project
├── WorkPackage (optionnel selon le besoin)
└── WorkforceRequest
    └── RequestLine
        └── ResourceRequirement
            └── Shift
```

Un chemin ad hoc existe également :

```text
Project
  └── ResourceRequirement [QUICK_SHIFT / AD_HOC]
      └── Shift
```

Quelques distinctions importantes :

- `WorkforceRequest` porte le workflow de la demande;
- `RequestLine` représente un besoin planifiable à l’intérieur de la demande;
- `ResourceRequirement` matérialise le besoin/budget de planification;
- `Shift` représente les heures réellement placées sur une ressource et une date;
- `AppUser`, `Resource` et les contacts métier sont des concepts distincts.

### Vision long terme

Après stabilisation de la planification main-d’œuvre et des actifs, la vision produit prévoit des domaines supplémentaires reliés au `WorkPackage` sans confondre leurs sources de vérité :

```text
WorkPackage
├── Planning      → besoins, capacité, quarts, actifs
├── Delivery      → Epics, Stories, Kanban/Sprints, forecast
└── Verification  → FAT, SAT, commissioning, résultats et preuves
```

Le but n’est pas de reproduire Jira : le module Delivery doit relier l’exécution technique au budget, aux échéances et à la capacité du WorkPackage. Le module Verification doit permettre de capturer les tests requis pendant le développement puis de générer les checklists et rapports FAT/SAT/commissioning à partir des données structurées.

Voir [`docs/FUTURE_DELIVERY_VERIFICATION.md`](docs/FUTURE_DELIVERY_VERIFICATION.md). Cette vision n’est pas encore le runtime implémenté; l’ordre réel reste défini par #55.

---

## Démarrage rapide avec Docker

Le chemin Docker est le moyen le plus reproductible de démarrer l’application complète en local.

Prérequis :

- Docker Engine ou Docker Desktop;
- Docker Compose v2.

Depuis la racine :

```bash
docker compose up -d --build
```

Ouvrir :

```text
http://127.0.0.1:8080/
```

Probes :

```text
http://127.0.0.1:8080/health
http://127.0.0.1:8080/ready
```

Arrêt :

```bash
docker compose down
```

Pour supprimer également les volumes locaux, dont la base SQLite de développement :

```bash
docker compose down -v
```

Cette dernière commande est destructive pour les données Docker locales.

Le démarrage Docker normal migre la base SQLite jusqu’à `alembic head` puis démarre le backend/frontend **sans charger de données de démonstration**. En mode local, l’accès utilise le principal ADMIN statique configuré par `RESOURCEPLANNER_LOCAL_AUTH_*`; aucun `AppUser` seedé ni Dev User Switcher n’est requis.

Le jeu de données de démonstration reste volontairement opt-in pour le développement SQLite :

```bash
docker compose --profile demo run --rm seed-dev
```

Ce seed n’est ni un bootstrap de production ni le futur mécanisme break-glass suivi dans #457.

Voir [`docs/DEPLOYMENT_UBUNTU_VM.md`](docs/DEPLOYMENT_UBUNTU_VM.md) pour la cible de déploiement actuelle sur VM Ubuntu. [`docs/DOCKER_SYNOLOGY.md`](docs/DOCKER_SYNOLOGY.md) est conservé comme référence historique du déploiement direct sur DSM/Container Manager.

---

## Dev Cockpit local

Le Dev Cockpit est un outil de développement isolé sous `dev-cockpit/`. Il observe GitHub en lecture seule et n'est pas intégré au frontend/backend métier.

Le runtime RessourcePlanner normal reste inchangé :

```bash
docker compose up -d --build
```

RessourcePlanner reste disponible sur `http://127.0.0.1:8080`.

Pour démarrer RessourcePlanner et le Dev Cockpit :

```bash
docker compose --profile dev-tools up -d --build
```

Le cockpit est alors disponible sur `http://127.0.0.1:8081` et n'est publié que sur loopback par défaut. Son service utilise `restart: "no"` afin de rester explicitement opt-in.

Configurer le token GitHub local dans le `.env` racine :

```dotenv
DEV_COCKPIT_GITHUB_TOKEN=
DEV_COCKPIT_REPOSITORY=tchi99/RessourcePlanner
DEV_COCKPIT_ROADMAP_ISSUE=55
DEV_COCKPIT_STALLED_AFTER_MINUTES=30
DEV_COCKPIT_HTTP_PORT=8081
```

Le token doit rester local, en lecture seule, et n'est transmis qu'au backend du cockpit. Sans token, le cockpit affiche une erreur de configuration explicite.

Les cartes de rôles/conversations se configurent directement dans l'interface du cockpit. Leur configuration locale est conservée dans le volume Docker `dev-cockpit-data`; aucune édition manuelle de JSON n'est requise. `docker compose down -v` supprime également cette configuration locale.

Le sous-dossier `dev-cockpit/firefox-companion/` fournit une extension Firefox locale optionnelle qui relaie uniquement l'état `working/idle` des conversations ChatGPT associées aux rôles. Elle n'utilise aucune API OpenAI et ne transmet pas le contenu des messages.

Sur la VM Ubuntu de production, ne pas activer le profil `dev-tools` et ne pas provisionner les variables `DEV_COCKPIT_*`. Les workflows GitHub Actions valident ce chemin Docker, mais ne déploient pas automatiquement la VM.

Voir [`dev-cockpit/README.md`](dev-cockpit/README.md) pour l'architecture, la logique `STALLED`, les tests et le développement hors Docker.

## Runtime Web local et développement HMR

Le runtime supporté en exploitation est **Docker Compose** (voir « Démarrage rapide avec Docker »). Les anciens lanceurs et installateurs BAT sont retirés. Pour un environnement Python 3.12 + Node 22 local hors Docker :

```bash
python -m pip install -r requirements-server.txt -c constraints-release.txt
cd frontend
npm install --no-audit --no-fund
npm run build
cd ..
```

Configurer `RESOURCEPLANNER_DATABASE_URL` explicitement, puis exécuter `python -m alembic upgrade head` (base de développement), `python tools/check_server_runtime.py` et `python -m app.server`. Pour servir React sur le même port, définir `RESOURCEPLANNER_FRONTEND_DIST` vers le chemin **absolu** de `frontend/dist`.

Exemple PowerShell **SQLite de développement seulement**, après le build :

```powershell
$env:RESOURCEPLANNER_DATABASE_URL = "sqlite:///./resourceplanner_server.db"
python -m alembic upgrade head
$env:RESOURCEPLANNER_FRONTEND_DIST = (Resolve-Path .\frontend\dist).Path
python tools/check_server_runtime.py
python -m app.server
```

La production SQL Server n'utilise ni fallback SQLite, ni migration implicite, ni seed. Pour une démo SQLite opt-in :

```bash
docker compose --profile demo run --rm seed-dev
```

### Vite / HMR

Lancer FastAPI en mode API seule (`RESOURCEPLANNER_FRONTEND_DIST` non définie), après migration locale :

```bash
python -m app.server
```

Dans un second terminal :

```bash
cd frontend
npm run dev
```

Ouvrir `http://127.0.0.1:5173/`. Vite relaie `/api` et `/health` vers le port 8000. Voir [`docs/REACT_V2_DEV.md`](docs/REACT_V2_DEV.md), [`docs/WEB_RUNTIME.md`](docs/WEB_RUNTIME.md) et [`docs/V2_RUNTIME_OPERATIONS.md`](docs/V2_RUNTIME_OPERATIONS.md).

---

## Base de données

### Développement local

SQLite reste le dialecte principal pour :

- développement local;
- tests;
- smoke Docker;
- CI fonctionnelle.

### Production cible

SQL Server est la cible de production. Le dépôt valide déjà :

- compilation SQLAlchemy avec le dialecte MSSQL;
- génération DDL Alembic MSSQL;
- migrations offline;
- requêtes critiques;
- contraintes de schéma compatibles SQL Server.

La validation réelle ODBC / `pyodbc` / réseau / permissions / migrations sur le serveur cible reste suivie dans **#162**.

Voir :

- [`docs/V2_SQLSERVER_READINESS.md`](docs/V2_SQLSERVER_READINESS.md);
- [`docs/SQL_CUTOVER_RUNBOOK.md`](docs/SQL_CUTOVER_RUNBOOK.md);
- [`docs/SQL_SCHEMA_V1.md`](docs/SQL_SCHEMA_V1.md).

---

## Authentification et autorisation

FastAPI est la frontière d’autorisation.

Le projet supporte :

- identités locales pour développement;
- plusieurs rôles et permissions métier;
- sessions serveur;
- sélecteur d’identité dev pour les tests multi-utilisateurs;
- préparation OIDC pour l’environnement réel.

Le mode local ne constitue pas une configuration de production exposée sur le réseau.

Les rôles métier sont conservés dans RessourcePlanner; l’identité externe ne décide pas à elle seule des autorisations.

Voir [`docs/AUTH_RBAC.md`](docs/AUTH_RBAC.md) et [`docs/OIDC_ACUMATICA_VALIDATION.md`](docs/OIDC_ACUMATICA_VALIDATION.md).

---

## Imports ERP

Le bootstrap de test utilise les chemins canoniques suivants.

### Projets — OData Acumatica

Les projets sont synchronisés par le chemin livré dans #207 :
`ODataProjectSource → ProjectSourcePort → ProjectSyncService`.

Après avoir configuré les variables `RESOURCEPLANNER_ACUMATICA_*` du backend, un
administrateur peut ouvrir **Configuration → Projets Acumatica** et lancer
**Synchroniser les projets**. Le résultat affiche les nombres reçus, créés, mis à jour,
inchangés et les erreurs.

L'ancien import Excel de projets reste un outil de compatibilité dev/démo; il n'est pas le
chemin recommandé pour le bootstrap PO.

### Tâches — export temporaire XLSX/CSV

Prévisualisation :

```bash
docker compose run --rm import-tasks "/imports/Tâches de projet.xlsx"
```

Application :

```bash
docker compose run --rm import-tasks "/imports/Tâches de projet.xlsx" --apply
```

Les fichiers d'import sont montés en lecture seule dans le conteneur. Le format minimal,
les diagnostics de doublons/lignes invalides et la sémantique de replay sont documentés dans
[`docs/ERP_TASK_CATALOG.md`](docs/ERP_TASK_CATALOG.md).

---

## Communications

Le workflow de communications projet sépare explicitement préparation, approbation et envoi :

- Microsoft Graph peut créer des brouillons;
- l’approbation métier ne déclenche pas d’envoi externe silencieux;
- SMTP utilise une action d’envoi distincte et auditée;
- les destinataires et ressources proviennent des données métier résolues côté backend.

La validation sur environnement Microsoft 365 réel reste suivie séparément.

Voir [`docs/M365_GRAPH_COMMUNICATIONS.md`](docs/M365_GRAPH_COMMUNICATIONS.md).

---

## Structure du dépôt

```text
frontend/                    React / TypeScript / Vite
app/server/                  FastAPI, routes, auth et composition HTTP
app/application/             services applicatifs et cas d’usage
app/domain/                  règles métier et moteur de planification
app/infrastructure/sql/      modèles et repositories SQLAlchemy
app/infrastructure/acumatica/
app/infrastructure/m365/
app/infrastructure/smtp/
migrations/                  migrations Alembic
tests/                       tests Python
tools/                       validations, imports, benchmarks et maintenance
deploy/                      artefacts de déploiement
docs/                        documentation technique et opérationnelle
docs/architecture/           décisions d’architecture et convention ADR
```

---

## Validation et CI

La CI principale se trouve dans `.github/workflows/syntax-check.yml`.

Elle couvre notamment :

- isolation du runtime serveur;
- tests Python en shards;
- compilation Python;
- build TypeScript/Vite;
- tests Playwright;
- smoke du runtime Web;
- readiness SQL Server;
- benchmark V2;
- scan de confidentialité;
- smoke Docker.

Commandes utiles :

```bash
python -m compileall -q app tests tools migrations main.py
python tools/check_server_dependency_isolation.py
python tools/check_sqlserver_readiness.py
python tools/privacy_scan.py
```

Frontend :

```bash
cd frontend
npm install --prefer-offline --no-audit --no-fund
npm run build
npm run test:e2e
```

Les règles complètes de travail pour les agents et contributeurs automatisés sont dans [`AGENTS.md`](AGENTS.md).

---

## Sources de vérité du projet

Pour éviter de dupliquer un roadmap ou des décisions dans plusieurs fichiers :

- **code + tests sur `main`** : état réellement implémenté;
- **GitHub Issues** : périmètre et état des travaux;
- **issue #55** : roadmap maître et ordre des travaux;
- [`AGENTS.md`](AGENTS.md) : règles permanentes de développement;
- [`docs/architecture/`](docs/architecture/) : décisions d’architecture et ADR;
- **`docs/`** : runbooks et documentation technique.

Le README reste volontairement une vue d’ensemble stable plutôt qu’un journal de toutes les fonctionnalités ou issues.

---

## Transition depuis la V1 NiceGUI / Excel

Le dépôt conserve encore temporairement des artefacts de l’ancienne architecture NiceGUI/Excel afin de permettre le cutover contrôlé.

Le runtime V1 :

- utilise `main.py`, NiceGUI et Excel;
- n’est plus le runtime Web cible;
- ne doit plus recevoir d’investissement structurant;
- doit être retiré après validation du cutover SQL réel.

Le retrait du runtime V1 et le nettoyage des artefacts legacy sont suivis dans **#208** et **#336**.

Les dépendances Web/SQL canoniques restent séparées des dépendances legacy.

---

## Documentation utile

- [`docs/architecture/README.md`](docs/architecture/README.md) — architecture et convention ADR;
- [`docs/DEMANDS_V2_ARCHITECTURE.md`](docs/DEMANDS_V2_ARCHITECTURE.md) — architecture actuelle des demandes après #13/#328/#329 et frontière de la tranche React #330;
- [`docs/FUTURE_DELIVERY_VERIFICATION.md`](docs/FUTURE_DELIVERY_VERIFICATION.md) — vision long terme WorkPackage → Delivery → Verification/commissioning;
- [`docs/REACT_V2_DEV.md`](docs/REACT_V2_DEV.md) — développement React + FastAPI;
- [`docs/WEB_RUNTIME.md`](docs/WEB_RUNTIME.md) — runtime Web same-origin;
- [`docs/DEPLOYMENT_UBUNTU_VM.md`](docs/DEPLOYMENT_UBUNTU_VM.md) — déploiement Docker cible sur VM Ubuntu hébergée par le Synology;
- [`docs/DOCKER_SYNOLOGY.md`](docs/DOCKER_SYNOLOGY.md) — ancienne cible directe Synology/Container Manager, conservée comme référence historique;
- [`docs/V2_RUNTIME_OPERATIONS.md`](docs/V2_RUNTIME_OPERATIONS.md) — exploitation et diagnostic;
- [`docs/ENVIRONMENT_VARIABLES.md`](docs/ENVIRONMENT_VARIABLES.md) — configuration;
- [`docs/AUTH_RBAC.md`](docs/AUTH_RBAC.md) — authentification et RBAC;
- [`docs/V2_SQLSERVER_READINESS.md`](docs/V2_SQLSERVER_READINESS.md) — préparation SQL Server;
- [`docs/V2_PERFORMANCE_BASELINE.md`](docs/V2_PERFORMANCE_BASELINE.md) — performance;
- [`docs/M365_GRAPH_COMMUNICATIONS.md`](docs/M365_GRAPH_COMMUNICATIONS.md) — communications M365;
- [`docs/V1_SQL_CUTOVER_INVENTORY.md`](docs/V1_SQL_CUTOVER_INVENTORY.md) — inventaire de transition V1 → Web/SQL.

---

## Roadmap

Le roadmap opérationnel n’est pas dupliqué dans ce fichier.

Consulter **GitHub Issue #55 — Roadmap maître V2.x** pour :

- l’ordre de travail;
- les dépendances;
- les décisions structurantes;
- les travaux terminés;
- les prochains blocs à développer.

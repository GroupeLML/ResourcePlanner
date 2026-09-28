# Docker / Synology — runtime V2

> **Statut : cible historique / non privilégiée.** La décision d'exploitation actuelle est de ne plus déployer RessourcePlanner directement dans Synology Container Manager. Le NAS Synology héberge désormais une **VM Ubuntu dédiée**, et Docker/Compose s'exécute dans cette VM. Voir [`DEPLOYMENT_UBUNTU_VM.md`](DEPLOYMENT_UBUNTU_VM.md) pour la procédure cible.
>
> Les artefacts `deploy/synology/` restent temporairement présents pour compatibilité et historique; ils ne définissent plus la cible de production privilégiée.

Cette procédure couvre la conteneurisation de RessourcePlanner et la cible Synology Container Manager prévue par #263.

Le chemin Docker ne dépend ni de NiceGUI/Excel, ni d'une installation globale de Python/Node/npm sur la machine qui exécute les conteneurs.

## Architecture

```text
Navigateur
    |
    v
Nginx / React :8080
    | même origine
    +---- /api/*, /health, /ready, /docs, /openapi.json
    v
FastAPI :8000 (réseau Docker seulement)
    |
    +---- SQLite /data/resourceplanner.db        [local / smoke]
    |
    +---- SQL Server externe                     [production après #162]
```

SQL Server n'est pas conteneurisé par cette tranche.

## 1. Démarrage local reproductible

Prérequis : Docker Engine ou Docker Desktop avec Docker Compose v2.

Depuis la racine :

```bash
docker compose up -d --build
```

Le Compose local :

1. construit l'image backend depuis `requirements-server.txt`;
2. construit React avec Node 22 puis sert `dist` avec Nginx;
3. exécute le service one-shot `migrate` sur la base SQLite locale;
4. ne démarre le backend qu'après la migration réussie;
5. ne démarre le frontend qu'après le healthcheck du backend.

Le démarrage normal ne lance aucun seed. Une base SQLite neuve contient donc uniquement le schéma migré et aucune donnée métier `DEMO-*`.

URL par défaut :

```text
http://127.0.0.1:8080/
```

Probes :

```text
http://127.0.0.1:8080/health
http://127.0.0.1:8080/ready
```

Smoke rapide :

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
curl -fsS http://127.0.0.1:8080/
```

Diagnostic :

```bash
docker compose ps
docker compose logs --tail=200 backend
docker compose logs --tail=200 frontend
docker compose logs migrate
```

Arrêt :

```bash
docker compose down
```

Pour supprimer aussi la base SQLite locale Docker :

```bash
docker compose down -v
```

Cette dernière commande est destructive pour les volumes Docker locaux.

## 2. Configuration locale

Copier `.env.example` vers `.env` seulement si une surcharge est nécessaire.

Le fichier `.env` réel est ignoré par Git. Ne jamais y committer de secret.

Le Compose local active explicitement le mode d'authentification local sur le réseau Docker et utilise par défaut le principal ADMIN statique `RESOURCEPLANNER_LOCAL_AUTH_*`. Ce principal n'est pas persisté dans `app_users` et reste utilisable sur une base fraîche sans aucun seed. `RESOURCEPLANNER_DEV_USER_SWITCHER` est désactivé par défaut.

Ce mode sert uniquement au développement/smoke sur une machine de confiance. Il ne constitue ni une configuration de production exposée sur le LAN ni le futur mécanisme break-glass de #457.

### Données de démonstration opt-in

Le service `seed-dev` est conservé sous le profil Compose explicite `demo`. Pour charger volontairement les données de démonstration dans la SQLite locale :

```bash
docker compose --profile demo run --rm seed-dev
```

Le script reste limité à SQLite. Il ne fait jamais partie de `docker compose up` normal et ne sert pas à créer l'administrateur local. Si un scénario multi-utilisateur de démonstration exige le Dev User Switcher, activer aussi explicitement `RESOURCEPLANNER_DEV_USER_SWITCHER=true` après avoir chargé ces `AppUser` de démo.

## 3. Migrations Alembic

### Local Docker

La migration SQLite locale est matérialisée par un service Compose distinct nommé `migrate`. Elle reste visible et testable séparément :

```bash
docker compose run --rm migrate
```

### Synology / production

Le Compose Synology **n'exécute aucune migration au démarrage du backend** et force `RESOURCEPLANNER_DEV_USER_SWITCHER=false`. Le runtime refuse également toute tentative d'activer ce switcher lorsque `RESOURCEPLANNER_AUTH_MODE=oidc`.

Avant une promotion :

```bash
docker compose -f deploy/synology/compose.yml run --rm --no-deps backend \
  python -m alembic upgrade head
```

Ensuite seulement :

```bash
docker compose -f deploy/synology/compose.yml up -d --no-build
```

Ne jamais automatiser `alembic downgrade` pendant un rollback applicatif.

## 4. Images et versionnement

Les deux images sont identifiables par tag :

```text
resourceplanner-backend:<tag>
resourceplanner-frontend:<tag>
```

Pour une promotion basée sur un commit Git :

```bash
export RESOURCEPLANNER_IMAGE_TAG="git-$(git rev-parse --short HEAD)"
docker compose -f deploy/synology/compose.yml build
```

Le tag `local` est acceptable pour le développement. Sur Synology, utiliser un tag explicite de release ou de commit.

La cible suivante pourra publier exactement ces images dans un registry après CI verte. Le Compose reste compatible avec cette évolution : il suffit de définir `RESOURCEPLANNER_BACKEND_IMAGE` et `RESOURCEPLANNER_FRONTEND_IMAGE` vers les noms du registry, puis de démarrer avec `--no-build`.

## 5. Préflight du NAS Synology

Avant le premier déploiement réel, relever et conserver dans le dossier d'exploitation :

```bash
uname -m
docker version
docker compose version
free -h
df -h
```

À valider sur le NAS réel :

- modèle exact du NAS;
- architecture CPU;
- RAM installée et RAM libre pendant le smoke;
- version DSM;
- disponibilité de Container Manager;
- espace disponible pour images, volumes et logs;
- accès réseau sortant du backend vers SQL Server, OIDC, Acumatica et Microsoft 365 selon les fonctions activées.

Les images utilisées ici reposent sur des bases officielles Python/Node/Nginx disponibles sur les architectures Docker courantes. La validation du modèle Synology réel reste obligatoire avant de déclarer ce critère de #263 terminé.

## 6. Création du projet dans Synology Container Manager

Le fichier cible est :

```text
deploy/synology/compose.yml
```

Copier :

```text
deploy/synology/.env.example
```

vers :

```text
deploy/synology/.env
```

puis renseigner les valeurs réelles directement sur le NAS.

Les secrets SQL/OIDC/Acumatica/M365 ainsi que `RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY` ne doivent jamais être intégrés dans les images ni committés. La clé de chiffrement SMTP doit être conservée uniquement dans le `.env`/gestionnaire de secrets du NAS et injectée au backend.

Pour une première phase pilotée par Git/SSH :

```bash
git pull
export RESOURCEPLANNER_IMAGE_TAG="git-$(git rev-parse --short HEAD)"
docker compose -f deploy/synology/compose.yml build
docker compose -f deploy/synology/compose.yml run --rm --no-deps backend python -m alembic upgrade head
docker compose -f deploy/synology/compose.yml up -d --no-build
```

Cette séquence est volontairement explicite : un push Git ne déploie rien silencieusement.

## 7. SQLite temporaire sur Synology avant #162

Le Compose Synology possède un volume `/data` afin de permettre un smoke isolé avant l'accès SQL Server.

Pour ce smoke seulement, une URL de type suivant peut être utilisée :

```text
sqlite+pysqlite:////data/resourceplanner.db
```

Il faut alors exécuter la migration manuelle décrite plus haut.

Si le backend est exposé sur le LAN avec `RESOURCEPLANNER_AUTH_MODE=local`, l'autorisation réseau locale doit être activée explicitement. Ce mode ne doit pas devenir le réglage de production permanent.

## 8. Passage à SQL Server

#162 reste la dépendance bloquante pour :

- choisir et valider le driver ODBC réel;
- ajouter/épingler `pyodbc` ou le driver retenu dans l'image backend;
- vérifier la connectivité du conteneur vers SQL Server;
- exécuter `alembic upgrade head` sur une base de développement SQL Server;
- exécuter le smoke lecture/transaction SQL Server;
- valider le rollback réel.

Le backend Docker reste volontairement SQLite-only tant que ce choix n'est pas validé.

## 9. Same-origin et proxy Nginx

Le navigateur ne contacte pas directement le port FastAPI.

Nginx sert React et proxifie vers `backend:8000` :

- `/api/*`;
- `/health`;
- `/ready`;
- `/docs`;
- `/openapi.json`.

Le frontend continue donc d'utiliser ses URLs relatives sans CORS additionnel.

La CSP `frame-ancestors` du frontend Nginx est configurée par `RESOURCEPLANNER_FRAME_ANCESTORS`. La valeur par défaut est `'self'`. Les origines Teams devront être ajoutées uniquement après leur validation.

## 10. Logs

Les logs Uvicorn et Nginx sont disponibles via les logs de conteneurs/Container Manager :

```bash
docker compose -f deploy/synology/compose.yml logs --tail=200 backend
docker compose -f deploy/synology/compose.yml logs --tail=200 frontend
```

Les métriques techniques JSONL du backend utilisent aussi un volume Docker persistant `resourceplanner-logs`.

## 11. Mise à jour

Séquence recommandée :

1. choisir un commit/release dont la CI est verte;
2. construire ou récupérer les deux images avec un tag immuable;
3. conserver le tag actuellement en production;
4. vérifier la compatibilité de la migration;
5. exécuter explicitement `alembic upgrade head`;
6. démarrer la nouvelle version;
7. vérifier `/health`, `/ready`, la page React et un appel API;
8. conserver l'image précédente jusqu'à la fin de la fenêtre de validation.

## 12. Rollback

Avant chaque promotion, noter :

```text
CURRENT_TAG=<tag actuel>
NEXT_TAG=<tag à promouvoir>
ALEMBIC_REVISION=<révision actuelle>
```

En cas de rollback applicatif :

1. vérifier que le schéma actuel est compatible avec l'ancienne image;
2. remettre `RESOURCEPLANNER_IMAGE_TAG` sur `CURRENT_TAG`;
3. vérifier que les deux images existent encore;
4. lancer :

```bash
docker compose -f deploy/synology/compose.yml up -d --no-build
```

5. refaire les probes et le smoke métier.

Ne jamais rétrograder automatiquement la base avec le code.

## 13. État de #263

Automatisable sans le NAS/SQL Server réel :

- packaging backend Docker;
- build React/Nginx;
- Compose local SQLite;
- séparation liveness/readiness;
- secrets hors images/Git;
- migration locale contrôlée;
- smoke Docker CI;
- configuration Synology prête à importer;
- versionnement des images par tag;
- procédure de mise à jour/rollback.

À valider physiquement avant fermeture complète :

- modèle/CPU/RAM du NAS;
- Container Manager réel;
- smoke sur le NAS;
- connectivité SQL Server et driver ODBC (#162);
- migration SQL Server réelle;
- rollback réel sur le NAS.

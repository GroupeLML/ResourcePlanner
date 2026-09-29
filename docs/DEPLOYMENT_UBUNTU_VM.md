# Déploiement cible — VM Ubuntu sur Synology

## Décision

RessourcePlanner n'est plus destiné à être déployé directement dans **Synology Container Manager**.

La cible d'exploitation privilégiée est maintenant une **VM Ubuntu dédiée**, hébergée sur le Synology, dans laquelle tournent Docker Engine et Docker Compose.

Le Synology agit donc comme hôte de virtualisation; le runtime applicatif reste un environnement Linux standard indépendant de DSM.

## Architecture cible

```text
Utilisateurs
    |
    v
Synology
└── VM Ubuntu
    ├── Docker Engine
    ├── Docker Compose
    ├── frontend React / Nginx :8080
    ├── backend FastAPI :8000 (réseau Docker)
    └── services/outils auxiliaires explicitement activés
            |
            +---- SQL Server externe
            +---- Acumatica / OIDC
            +---- Microsoft 365 / SMTP
```

SQL Server reste externe à la VM sauf décision explicite contraire.

## Pourquoi cette cible

Cette approche permet notamment :

- un environnement Linux standard et reproductible;
- une séparation nette entre DSM et le runtime applicatif;
- une installation Docker/Compose classique;
- des mises à jour et diagnostics indépendants des particularités de Synology Container Manager;
- une restauration ou reconstruction de la VM plus prévisible;
- une meilleure isolation des outils de développement ou d'administration.

## Préparation de la VM

Base recommandée :

- Ubuntu Server LTS;
- Docker Engine;
- plugin Docker Compose v2;
- Git seulement si le mode de promotion retenu utilise un checkout local;
- synchronisation horaire/NTP;
- stockage persistant prévu pour les volumes applicatifs;
- accès réseau sortant vers SQL Server et les intégrations nécessaires.

À relever avant mise en production :

```bash
uname -a
lsb_release -a
docker version
docker compose version
free -h
df -h
```

## Déploiement

Le packaging applicatif reste Docker/Compose.

La promotion de production reste **explicite** :

1. choisir un commit/release dont la CI est verte;
2. utiliser des images identifiables par tag;
3. conserver le tag actuellement en production;
4. vérifier la compatibilité des migrations;
5. exécuter explicitement `alembic upgrade head`;
6. démarrer les conteneurs;
7. vérifier `/health`, `/ready`, le frontend et un appel API;
8. conserver l'image précédente pendant la fenêtre de validation.

Le déploiement ne doit pas être déclenché silencieusement par chaque push Git.

## Configuration et secrets

Les secrets restent hors Git et hors images Docker.

La VM fournit les variables d'environnement requises pour :

- SQL Server;
- OIDC;
- Acumatica;
- Microsoft 365;
- SMTP;
- clés de chiffrement;
- paramètres réseau/site.

Le mode d'authentification local et le sélecteur d'identité de développement ne doivent pas être activés en production.

## Ports

Le runtime RessourcePlanner conserve son port HTTP public configuré, actuellement typiquement :

```text
8080
```

Les ports backend internes restent confinés au réseau Docker lorsque possible.

Les outils de développement auxiliaires, comme le Dev Cockpit, doivent utiliser un port distinct et ne pas être publiés par défaut sur le LAN de production.

## Dev Cockpit

Le Dev Cockpit est intégré au `docker-compose.yml` principal comme **outil local/dev optionnel**. Il n'est pas une dépendance du runtime métier et ne fait pas partie de la promotion de production.

Le service `dev-cockpit` est placé sous le profil Compose `dev-tools`. Par conséquent, le lancement normal :

```bash
docker compose up -d --build
```

démarre RessourcePlanner sans le cockpit.

Sur une VM ou un poste de développement, la commande explicite :

```bash
docker compose --profile dev-tools up -d --build
```

démarre RessourcePlanner et le cockpit ensemble. Les publications par défaut restent limitées à la loopback de l'hôte :

```text
RessourcePlanner  127.0.0.1:8080
Dev Cockpit       127.0.0.1:8081
```

Le cockpit a `restart: "no"` : après un redémarrage de Docker ou de la VM, il doit être relancé explicitement avec le profil `dev-tools`.

### Règles de production

Sur la VM Ubuntu de production :

- ne pas activer le profil `dev-tools`;
- ne pas provisionner `DEV_COCKPIT_GITHUB_TOKEN` ni les autres variables `DEV_COCKPIT_*` dans la configuration de production;
- ne pas publier le port 8081 sur le LAN;
- ne pas ajouter le cockpit aux procédures de promotion, rollback ou démarrage du runtime métier;
- conserver les déploiements explicites : les workflows GitHub Actions du dépôt valident le Compose et les smokes, mais ne déploient pas automatiquement la VM.

Le token GitHub du cockpit reste uniquement dans l'environnement du backend cockpit. Il n'est ni injecté dans le build React, ni retourné par l'API.

Pour un diagnostic ponctuel sur une **VM de développement**, conserver l'écoute loopback et utiliser au besoin un tunnel SSH plutôt que d'ouvrir le port 8081 sur le réseau.

## SQL Server

La validation réelle #162 a confirmé le runtime cible avec SQL Server 2017, SQLAlchemy 2.0.52, `pyodbc 5.3.0` et Microsoft ODBC Driver 18.

L'image backend installe elle-même le paquet Debian `msodbcsql18` depuis le dépôt Microsoft et `requirements-server.txt` épingle `pyodbc==5.3.0`. La CI `docker-smoke` vérifie que l'image finale expose bien `ODBC Driver 18 for SQL Server` via `pyodbc.drivers()`.

La sous-version système du paquet ODBC 18 n'est pas figée dans Git : le build suit le paquet `msodbcsql18` compatible avec la version Debian de l'image `python:3.12-slim`. Le secret SQL et les paramètres de connexion restent injectés à l'exécution via `RESOURCEPLANNER_DATABASE_URL`.

## Mise à jour et rollback

Avant une promotion, noter :

```text
CURRENT_TAG=<tag actuel>
NEXT_TAG=<tag cible>
ALEMBIC_REVISION=<révision actuelle>
```

En cas de rollback applicatif :

1. vérifier la compatibilité du schéma courant avec l'ancienne image;
2. restaurer le tag applicatif précédent;
3. redémarrer les conteneurs;
4. rejouer les probes et smokes métier.

Ne jamais automatiser un `alembic downgrade` lors d'un rollback de code.

## Sauvegarde / restauration

La stratégie de sauvegarde doit considérer séparément :

- la VM Ubuntu et sa configuration;
- les volumes Docker persistants;
- les secrets/configuration site;
- la base SQL Server externe;
- les images/tags applicatifs.

La sauvegarde du NAS ou de la VM ne remplace pas la stratégie de sauvegarde SQL Server.

## Ancienne cible directe Synology

La procédure précédente de déploiement direct dans Synology Container Manager est conservée dans [`DOCKER_SYNOLOGY.md`](DOCKER_SYNOLOGY.md) uniquement comme référence historique pendant la transition.

Les artefacts `deploy/synology/` pourront être renommés ou retirés dans une tranche dédiée lorsque le nouveau chemin Ubuntu aura été validé en environnement réel.

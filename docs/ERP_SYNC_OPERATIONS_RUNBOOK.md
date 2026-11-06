# 643C — Runbook d'activation et d'exploitation de la synchronisation ERP

> **Cible canonique :** VM Ubuntu → Docker Engine / Docker Compose → FastAPI + React,
> SQL Server externe, Acumatica externe. Le Synology peut héberger la VM, mais
> Container Manager / `deploy/synology/` reste historique et n'est pas l'autorité du scheduler.
>
> **Important :** ce runbook rend le dépôt prêt à activer. Il n'autorise pas à lui seul
> l'activation de la synchronisation automatique en production.

## 1. Autorités et invariants

La synchronisation ERP automatique utilise exclusivement le scheduler FastAPI ADR-033.
Ne jamais ajouter un cron DSM, un cron Ubuntu, une Scheduled Task ou un second worker
extérieur qui appelle directement les imports.

L'autorité durable est SQL Server :

- `erp_sync_control` : propriétaire, génération, lease, heartbeat, protocole/config;
- `erp_sync_occurrences` : occurrence DAILY/MANUAL unique;
- `erp_sync_attempts` : tentatives et phases;
- tables 643B : source/checkpoint, baselines, exceptions, recovery et compensation;
- projection 643C : lecture de ces autorités, jamais une seconde machine d'état.

Les migrations restent explicites : le scheduler ne lance jamais Alembic.

## 2. Configuration Compose

Le service `backend` de `docker-compose.yml` reçoit la configuration typée du runtime.

### Acumatica serveur-à-serveur

Requis avant activation :

- `RESOURCEPLANNER_ACUMATICA_BASE_URL`;
- `RESOURCEPLANNER_ACUMATICA_USERNAME`;
- `RESOURCEPLANNER_ACUMATICA_PASSWORD`;
- `RESOURCEPLANNER_ACUMATICA_PAGE_SIZE` (défaut 100);
- `RESOURCEPLANNER_ACUMATICA_TIMEOUT_SECONDS` (défaut 30).

Le compte doit être un **compte de service dédié**, confirmé hors Git, avec uniquement
les permissions de lecture attendues. Ne pas réutiliser volontairement un compte personnel
interactif. Aucun mot de passe/token réel ne doit être committé.

### Scheduler

- `RESOURCEPLANNER_ERP_SYNC_ENABLED=false` tant que l'étape d'activation finale n'est pas autorisée;
- `RESOURCEPLANNER_ERP_SYNC_TIME=00:00`;
- `RESOURCEPLANNER_ERP_SYNC_TIMEZONE=America/Toronto`;
- `RESOURCEPLANNER_ERP_SYNC_CATCHUP_HOURS=4`;
- `RESOURCEPLANNER_ERP_SYNC_FRESHNESS_SLA_HOURS=30`.

Lease (300 s), heartbeat (60 s), poll/retry et durée maximale utilisent actuellement le
contrat typé ADR-033 interne; ne pas inventer de variables d'environnement parallèles.

### Validation / preuve 643B

- `RESOURCEPLANNER_ERP_SYNC_MIN_SOURCE_RATIO=0.50`;
- `RESOURCEPLANNER_ERP_SYNC_MIN_PROJECTS=1`;
- `RESOURCEPLANNER_ERP_SYNC_MIN_TASK_ROWS=1`;
- `RESOURCEPLANNER_ERP_SYNC_MAX_EVIDENCE_MIB=256`;
- `RESOURCEPLANNER_ERP_SYNC_EVIDENCE_BUDGET_MIB=2048`;
- `RESOURCEPLANNER_ERP_SYNC_FAILURE_RETENTION_DAYS=14`;
- `RESOURCEPLANNER_ERP_SYNC_HISTORY_DAYS=90`;
- `RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY=<secret Fernet hors Git>`.

Ne jamais afficher la clé dans les journaux ni dans un ticket.

## 3. Préflight avant activation

Garder l'automatisation désactivée. **Ne pas** lancer un `docker compose up -d`
générique avant le drain de la section 5 : cette commande peut recréer un writer avant
que l'ancien processus ait cessé ses admissions.

Après le drain des writers courants et le déploiement des images compatibles en mode
désactivé, exécuter :

```bash
export RESOURCEPLANNER_ERP_SYNC_ENABLED=false
docker compose exec -T backend \
  python tools/check_erp_activation_readiness.py \
  --dedicated-account-confirmed
```

Le préflight échoue fermé si l'un des éléments suivants manque :

- connexion DB ou tête Alembic;
- `ZoneInfo("America/Toronto")`;
- source Acumatica serveur-à-serveur;
- attestation du compte ERP dédié;
- clé de chiffrement utilisable;
- singleton/protocole ERP attendu;
- fingerprint de configuration incompatible;
- baseline PROJECTS ou TASKS du policy fingerprint courant;
- revue de plausibilité non résolue;
- recovery `REQUIRED`;
- capacité automatique 643B.

Le flag `--dedicated-account-confirmed` est une attestation opérateur; il ne déduit jamais
l'identité dédiée du simple fait qu'un username/password existe.

## 4. Checklist de pré-activation

Avant toute bascule :

- [ ] image/tag backend et frontend identifiés;
- [ ] sauvegarde/DR traitée selon la procédure SQL existante;
- [ ] `python -m alembic upgrade head` exécuté explicitement;
- [ ] `python tools/check_sqlserver_readiness.py` vert;
- [ ] `python tools/check_server_runtime.py` vert;
- [ ] `python tools/check_web_runtime.py` vert lorsque le runtime Web est disponible;
- [ ] backend Docker contient ODBC Driver 18 et tzdata;
- [ ] Toronto IANA se résout dans le conteneur;
- [ ] SQL Server joignable et tête Alembic exacte;
- [ ] protocole/config ERP compatibles;
- [ ] aucun ancien writer incompatible en fonctionnement;
- [ ] endpoint/feed Acumatica configuré;
- [ ] compte de service ERP dédié et permissions de lecture vérifiés hors Git;
- [ ] credential injecté hors Git;
- [ ] crypto/evidence prêt et budget stockage sain;
- [ ] baselines PROJECTS/TASKS approuvées pour le policy courant;
- [ ] aucune revue de plausibilité non résolue;
- [ ] recovery différent de `REQUIRED`;
- [ ] `/health` et `/ready` sains;
- [ ] page/API Opérations ERP lisible par un opérateur autorisé;
- [ ] RUN_NOW exercé de façon contrôlée si autorisé;
- [ ] `RESOURCEPLANNER_ERP_SYNC_ENABLED=false` encore en vigueur.

## 5. Drain avant déploiement

### 5.1 Principe

Modifier `.env` ou exporter `RESOURCEPLANNER_ERP_SYNC_ENABLED=false` pour **le prochain**
conteneur ne reconfigure jamais un backend déjà en cours. Le drain exige donc une action
sur chaque writer actuel avant migration.

Dans la topologie Compose canonique, `backend` est le writer FastAPI. Le drain utilise
la même image actuellement déployée, sans migration et sans nouvelle image, mais la
recrée avec l'automatisation désactivée. Le `stop_grace_period: 2h5m` du service laisse
le shutdown FastAPI attendre le travail géré pendant au plus la durée bornée de tentative
(2 h) plus une marge de lease (5 min). Cette grâce ne garantit pas qu'un travail arbitraire
finira : après expiration, Docker peut envoyer SIGKILL et les règles SQL de lease/recovery
restent alors l'autorité.

### 5.2 Procédure exécutable — writers courants

1. **Avant de modifier le tag cible**, relever et conserver le tag/image backend réellement
   en cours ainsi que le nombre d'instances writer :

   ```bash
   docker compose ps backend
   docker compose images backend
   docker compose ps -q backend
   ```

   Pour plusieurs backends, noter le nombre `N` et vérifier que toutes les instances
   appartiennent à cette topologie. Un writer géré hors de ce Compose doit être drainé
   par son mécanisme d'exploitation avant de continuer.

2. Lire l'autorité SQL via **Opérations ERP** et noter :
   `current_owner_attempt_id`, `current_attempt_id`, génération, lease, phase et recovery.
   Un owner valide peut terminer; ne pas voler sa lease, la raccourcir ni le marquer FAILED.

3. Arrêter explicitement les **writers actuellement en cours** avec la grâce complète,
   même s'ils ont été créés avant que `stop_grace_period` soit présent dans Compose :

   ```bash
   docker compose stop -t 7500 backend
   ```

   Le signal d'arrêt ferme le lifespan FastAPI : le polling scheduler cesse d'admettre de
   nouveaux travaux sur ces processus, puis l'exécuteur local attend le travail géré.
   Ne poursuivre que lorsque la vérification SQL de l'étape 5 confirme la fin normale,
   ou lorsque les sémantiques lease/recovery ont classifié une interruption forcée.

4. Épingler explicitement l'image **courante** et démarrer les writers de remplacement
   avec l'automatisation désactivée, sans migration ni build :

   ```bash
   export RESOURCEPLANNER_IMAGE_TAG=<TAG_BACKEND_ACTUEL>
   export RESOURCEPLANNER_ERP_SYNC_ENABLED=false
   docker compose up -d --no-deps --no-build --force-recreate backend
   ```

   Si `backend` est volontairement scalé, préserver le nombre d'instances :

   ```bash
   docker compose up -d --no-deps --no-build --force-recreate \
     --scale backend=<N> backend
   ```

   Le backend de remplacement utilise la même version/protocole, mais
   `RESOURCEPLANNER_ERP_SYNC_ENABLED=false`.

5. Vérifier que toutes les anciennes instances ont disparu et que tous les backends
   courants sont ceux recréés en mode désactivé :

   ```bash
   docker compose ps backend
   ```

6. Revenir à **Opérations ERP**. La preuve de drain est SQL, pas le PID local :
   - aucun nouveau DAILY automatique ne doit apparaître après la recréation désactivée;
   - si un travail a fini pendant la grâce, l'owner doit être libéré normalement;
   - si le processus a dû être tué avant achèvement, attendre les règles existantes de
     lease/owner-deadline et la reconciliation; suivre l'état `INTERRUPTED`/recovery
     avant toute migration;
   - ne continuer que lorsque `current_owner_attempt_id` ne désigne plus un writer
     incompatible actif et qu'aucun ancien conteneur writer n'existe.

Un arrêt de conteneur ne prouve jamais qu'aucune transaction antérieure n'a été committée.
SIGTERM/SIGKILL ne déclenche aucune compensation automatique. Une compensation ERP reste
une opération 643B distincte, explicite et auditée.

## 6. Promotion

Ordre obligatoire, à ne pas inverser :

1. **drainer les writers courants** selon la section 5;
2. vérifier l'autorité SQL durable et l'absence de writer ancien/incompatible actif;
3. confirmer la sauvegarde/DR selon la procédure SQL existante;
4. appliquer explicitement la migration avec l'image cible, sans redémarrer les anciens writers;
5. déployer les images compatibles avec `RESOURCEPLANNER_ERP_SYNC_ENABLED=false`;
6. exécuter `docker compose config`, puis les probes SQL/Web/schema/protocole;
7. lire `GET /api/v1/integrations/acumatica/erp-sync/operations`;
8. exécuter le préflight 643C en mode désactivé;
9. exercer RUN_NOW manuellement seulement si cette validation est autorisée;
10. seulement après autorisation opérationnelle Product Owner : passer
    `RESOURCEPLANNER_ERP_SYNC_ENABLED=true`, recréer/redémarrer **tous** les backends,
    puis exécuter le gate final :

```bash
docker compose exec -T backend   python tools/check_erp_activation_readiness.py   --activation   --dedicated-account-confirmed
```

Si plusieurs backends existent, tous doivent partager protocole et fingerprint avant reprise
des départs automatiques. Aucun backend de génération/protocole antérieur ne peut rester
en parallèle lors de l'activation.

Après migration explicite, le déploiement désactivé peut être effectué sans relancer
implicitement `migrate`, par exemple en ciblant les services déjà migrés :

```bash
export RESOURCEPLANNER_ERP_SYNC_ENABLED=false
docker compose up -d --no-deps --no-build backend
docker compose up -d --no-deps --no-build frontend
```

Les tags d'images doivent alors être ceux explicitement validés pour la promotion.

## 7. Rollback

1. exécuter à nouveau le **current-writer drain** de la section 5; une simple modification
   de `.env` ne désactive pas les processus déjà lancés;
2. vérifier dans SQL qu'aucun owner incompatible actif ne subsiste et que l'image cible
   comprend le schéma/protocole actuellement persisté;
3. ne jamais lancer automatiquement un `alembic downgrade` destructif;
4. ne jamais effacer tentatives, occurrences, baselines, evidence ou recovery pour faciliter
   le rollback;
5. ne jamais faire tourner simultanément un ancien writer pré-lease/pré-generation avec le
   protocole courant;
6. conserver SQL Server et l'historique de preuves;
7. déployer l'image compatible précédente;
8. revalider readiness avant tout redémarrage automatique.

**Rollback code ≠ rollback de données.** La compensation 643B reste une action ERP explicite,
prévisualisée, autorisée, auditée et CAS-protégée; elle n'est pas un mécanisme de rollback
de déploiement.

Le dépôt ne contient actuellement pas de scripts versionnés `build.sh`,
`deploy-dev.sh`, `promote-prod.sh` ou rollback WNW faisant autorité pour cette VM.
Si le site possède des scripts hors dépôt, ils doivent respecter exactement le contrat
drain → migration explicite → images compatibles → readiness → activation contrôlée.
Leur comportement n'est pas déclaré « testé » par ce runbook.

## 8. Scénarios d'acceptation

### Redémarrage à 00:37 America/Toronto

Les tests scheduler couvrent 00:37 : l'occurrence de la date métier courante est en CATCHUP
dans la fenêtre de quatre heures. La clé quotidienne SQL reste unique. Le Docker smoke
matérialise une occurrence durable, redémarre le backend et vérifie qu'elle survit sans
duplication. Aucun backlog historique n'est rejoué en rafale.

### Redémarrage pendant un travail actif

SQL Server live couvre :

- work projet déjà committé reste committé;
- transaction non committée ne devient pas un faux succès;
- génération stale ne peut plus écrire/publier;
- remplacement seulement après lease/recovery;
- child run stale interrompu;
- aucune compensation globale aveugle.

### Deux backends

La suite SQL Server réelle utilise plusieurs sessions/acteurs contre la même autorité :
un seul owner est sélectionné, la génération suivante fence l'ancien owner, un peer sain
ne révoque pas une lease valide, et les conflits manuel/automatique restent soumis au
même contrôle SQL.

### Rolling deployment même protocole

Un second backend sain peut démarrer sans interrompre le propriétaire vivant. La reprise
automatique n'est autorisée qu'après accord du protocole/config fingerprint et readiness.
La page d'opérations reste une lecture de SQL, pas l'autorité d'élection.

### Ancien writer incompatible

Les tests protocole injectent une version/fingerprint incompatibles : claim/guard/automatic
capability échouent fermé. Une exécution d'un binaire historique n'est pas nécessaire pour
prouver ce contrat; la simulation utilise les mêmes colonnes/protections SQL.

### ERP indisponible

Une erreur source rend la tentative FAILED avec diagnostic sûr tant que SQL reste
disponible. Le Web/operations history reste lisible, le dernier succès complet n'avance
pas et aucune credential/XML brut ne doit être journalisé.

### SQL indisponible

Sans SQL, aucun owner/attempt persistant ne peut être fabriqué. `/health` garde sa
sémantique de liveness existante, `/ready`/operations dégradent selon le contrat actuel,
et aucun fallback mémoire de scheduler n'est autorisé.

### Partiel A/B/C

L'acceptation 643B/643C conserve les succès A/C, marque B stale/rejeté et ne déplace pas
le pointeur global de succès complet. Opérations ERP expose les qualités par projet.

### Recovery REQUIRED

`REQUIRED` bloque les nouveaux travaux. L'opérateur autorisé peut obtenir un preview si
la preuve est valide; un utilisateur sans `admin_settings` ne peut pas compenser.
Confirmation réussie résout l'autorité selon 643B; un redémarrage ne supprime pas l'état SQL.

### Expiration / pression preuve

Les états EXPIRED/PINNED/PRESSURE/UNAVAILABLE sont visibles sans purger les données métier.
La capacité automatique est fail-closed lorsque les garanties requises ne sont plus réunies.

## 9. CI / preuve

Le PR 643C doit exécuter :

- Python shard 0/1;
- frontend-validation et vrai navigateur FastAPI;
- server-isolation;
- docker-smoke (Compose, ODBC, tzdata, restart durable);
- `check_sqlserver_readiness.py`;
- SQL Server live réel pour lease/generation/concurrency/recovery.

Les résultats CI prouvent le comportement du dépôt et du SQL Server éphémère CI.
Ils **ne prouvent pas** :

- que le compte de service production a été créé;
- que ses permissions Acumatica réelles sont correctes;
- que le secret production a été injecté;
- qu'une sauvegarde production récente existe;
- que tous les backends réels de la VM ont été drainés;
- que la production a été activée.

Ces points restent à vérifier au moment du déploiement.

## 10. Activation réelle

Aucune activation automatique réelle n'est incluse dans le dépôt. Le défaut Compose reste :

```text
RESOURCEPLANNER_ERP_SYNC_ENABLED=false
```

L'activation production nécessite une instruction opérationnelle explicite du Product Owner,
le préflight vert, le drain/promotion ci-dessus et le contrôle post-activation via la page
Opérations ERP.

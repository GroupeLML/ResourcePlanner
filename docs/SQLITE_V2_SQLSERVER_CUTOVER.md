# Cutover SQLite V2 vers SQL Server (#492)

Ce runbook décrit l'outil de conservation des données RessourcePlanner V2 pré-go-live lors du premier passage vers SQL Server.

Il ne remplace ni #457 ni #208. #457 possède la baseline SQL Server propre et le bootstrap break-glass; #492 transfère uniquement les données V2 explicitement retenues; #208 déclare ensuite SQL Server autoritaire. Aucun import Excel/V1 ne fait partie de ce parcours.

## Garde #457

Le développement et les dry-runs de #492 peuvent précéder #457, mais aucun apply de production ne doit être exécuté avant que #457 ait stabilisé la baseline cible.

L'outil rend cette dépendance explicite :

- la matrice ci-dessous doit couvrir exactement toutes les tables de Base.metadata;
- la cible doit déjà avoir été créée par Alembic/baseline;
- la révision de alembic_version doit être égale à la tête Alembic du code exécuté;
- --expected-target-revision est obligatoire en mode apply;
- --baseline-ready est obligatoire en mode apply et constitue l'attestation opérateur que #457 est réellement prête;
- une nouvelle table ou un changement de schéma rend la matrice invalide jusqu'à revalidation.

L'historique Alembic pré-production courant n'est donc jamais traité comme un contrat définitif.

## Flux

~~~text
SQLite V2 gelé
  lecture seule + backup vérifié
        ↓
inventaire + schéma + révision
        ↓
filtrage DEMO/dev + classification KEEP/REBUILD/DROP
        ↓
validation dépendances / uniques / cible propre
        ↓
SQL Server déjà initialisé par #457/Alembic
        ↓
copie SQLAlchemy dans l'ordre FK
        ↓
validation comptes + IDs + liens métier
        ↓
commit atomique
        ↓
rapport
        ↓
bootstrap break-glass + RESYNC Acumatica
        ↓
#208 : SQL autoritaire
~~~

## Inventaire courant et matrice

La matrice est codée explicitement dans tools/cutover_sqlite_v2_to_sqlserver.py. Une divergence entre cette liste et Base.metadata bloque le cutover.

| Table | Classe | Traitement |
|---|---|---|
| approval_decisions | KEEP | décisions d'approbation métier |
| approval_requirement_approvers | KEEP | snapshot des approbateurs admissibles |
| approval_requirements | KEEP | exigences d'approbation par ligne |
| approval_scopes | KEEP | configuration locale |
| approval_scope_approvers | KEEP | configuration locale |
| resource_class_approval_scope_mappings | KEEP | routage local |
| task_approval_scope_mappings | KEEP | overrides locaux |
| asset_types | KEEP | catalogue d'actifs |
| asset_type_competencies | KEEP | qualifications des actifs |
| assets | KEEP | actifs réservables |
| asset_unavailability | KEEP | indisponibilités |
| asset_requirements | KEEP | besoins d'actifs |
| asset_allocations | KEEP | allocations d'actifs |
| app_users | KEEP | rôles/activation locaux; urn:resourceplanner:dev exclu |
| erp_user_directory | KEEP + RESYNC | conserver UserID et état local, puis resynchroniser RP_Users |
| identity_admin_audit | KEEP | audit durable |
| business_contacts | KEEP | contacts métier référencés |
| auth_login_transactions | DROP | nonce/PKCE/login temporaire |
| auth_sessions | DROP | token/CSRF/session |
| command_idempotency_receipts | DROP | reçus techniques de replay pré-go-live |
| communication_batches | KEEP | workflow de communication |
| communication_contacts | KEEP | destinataires configurés |
| communication_messages | KEEP | messages préparés/validés |
| communication_snapshot_lines | KEEP | snapshot métier |
| communication_deliveries | KEEP | historique de livraison |
| competencies | KEEP | catalogue local |
| delivery_plans | KEEP | plans Delivery |
| delivery_items | KEEP | Epics/Stories |
| delivery_change_history | KEEP | historique Delivery |
| planning_change_history | KEEP | audit Planning |
| planning_mutation_state | REBUILD | singleton technique créé par la baseline |
| projects | KEEP + RESYNC | préserver UUID/FK et champs locaux, puis resynchroniser RP_Projects |
| request_approval_cycles | KEEP | cycles d'approbation |
| request_approval_references | KEEP | références approuvées |
| request_approval_revisions | KEEP | révisions approuvées |
| request_operational_states | KEEP | choix opérationnels locaux |
| request_lines | KEEP | lignes de demandes |
| request_line_competencies | KEEP | compétences par ligne |
| resource_requirement_competencies | KEEP | snapshot des compétences |
| resource_class_configs | KEEP | configuration locale |
| project_task_class_overrides | KEEP | overrides projet/tâche |
| resources | KEEP + RESYNC | préserver UUID, activation/config locale, puis resynchroniser RP_Employees |
| work_packages | KEEP | WorkPackages locaux |
| work_package_audit | KEEP | audit durable des mutations WorkPackage / version CAS |
| workforce_requests | KEEP | demandes V2 |
| workforce_request_competencies | KEEP | compétences historiques |
| workforce_request_history | KEEP | historique des demandes |
| workforce_request_periods | KEEP | périodes |
| workforce_request_period_selections | KEEP | sélections de périodes |
| workforce_request_period_requirements | KEEP | liens période/besoin |
| resource_availability_rules | KEEP | disponibilités/horaires |
| resource_competencies | KEEP | compétences des ressources |
| resource_requirements | KEEP | besoins/budgets matérialisés |
| smtp_configuration | DROP | contient le credential chiffré; reconfiguration explicite |
| smtp_configuration_audit | KEEP | audit sans secret |
| shifts | KEEP | affectations réelles |
| task_catalog_items | KEEP + RESYNC | préserver UUID référencés, puis resynchroniser RP_ProjectTasks |
| task_catalog_project_sync_state | REBUILD | télémétrie/cursor de synchro |
| task_class_standards | KEEP | configuration locale |

alembic_version n'est jamais copié : il appartient à la baseline cible.

## Pourquoi KEEP + RESYNC pour les référentiels ERP

Les référentiels Acumatica ne sont pas considérés comme autoritaires parce qu'ils ont été copiés depuis SQLite.

En revanche, certains contiennent déjà des identités applicatives ou choix locaux référencés par des objets métier :

- projects.id est référencé par demandes, WorkPackages, besoins et actifs;
- resources.id est référencé par besoins, quarts, actifs et disponibilités;
- erp_user_directory.user_id est référencé par app_users.erp_user_id et porte local_active/roles_json;
- task_catalog_items.id est référencé par lignes, approbations et besoins matérialisés.

Les supprimer puis les recréer avec de nouveaux UUID briserait les relations. Le cutover conserve donc ces ancrages, puis les synchronisations autoritaires réconcilient les attributs ERP avec leurs clés externes stables.

Après le transfert et le bootstrap break-glass, exécuter :

1. RP_Projects / #207;
2. RP_Employees / #256;
3. RP_Users / #256/#468;
4. RP_ProjectTasks / contrat tâche/budget courant.

Les états locaux d'activation/rôles/configuration ne doivent pas être écrasés par ces synchros.

## Filtrage DEMO/dev

Avant toute écriture, le dry-run exclut :

- tout AppUser dont issuer == urn:resourceplanner:dev;
- les lignes dont une identité structurante/PK/FK porte un préfixe DEMO-;
- les enfants obligatoires d'un parent déjà exclu.

Si une FK nullable pointe vers une identité exclue, la FK est mise à NULL dans la copie et la sanitisation est inscrite au rapport.

Si une dépendance KEEP obligatoire est réellement absente de la source, le transfert bloque : elle n'est ni inventée ni contournée. Les FK ne sont jamais désactivées.

## Compatibilité de la source pré-baseline 0048

Le cutover réel ENV-492 a identifié la SQLite V2 gelée à la révision
`0048_identity_admin_audit`. Cette source précède volontairement le squash
`v2_production_baseline` et certaines évolutions postérieures.

L'outil accepte cette révision historique par un profil de compatibilité
**explicite et fail-closed**. Il ne modifie jamais le fichier source :

- `auth_security_audit` et `break_glass_credentials` peuvent être absentes;
  elles sont classées DROP et sont recréées/repartent proprement sur la cible;
- `work_package_audit` et `work_package_weekly_loads` peuvent être absentes
  et sont interprétées comme des ensembles vides, puisqu'elles n'existaient pas
  encore à cette révision;
- `auth_sessions.auth_mode` peut être absent; les sessions sont DROP;
- les WorkPackages 0048 sont normalisés **en mémoire seulement** avec
  `task_catalog_item_id = NULL`, `version = 1` et
  `weekly_load_origin = NULL`.

Tout autre écart de table ou de colonne reste bloquant. Le même schéma
historique portant une autre révision Alembic non approuvée reste également
bloqué.

Après #502C, la tête Alembic courante est
`0002_work_package_weekly_loads`. Le jour du cutover, utiliser la tête réelle
du code exécuté comme `--expected-target-revision`; ne pas supposer que
`v2_production_baseline` est encore la tête.

## Bridge temporaire de la SQLite live 0048

Le snapshot gelé utilisé par ENV-492 doit rester à `0048_identity_admin_audit`. Il ne doit jamais être migré, stampé ou utilisé comme runtime.

Si le cutover SQL Server est temporairement bloqué et qu'il faut continuer à exécuter le `main` courant sur SQLite, utiliser uniquement `tools/bridge_sqlite_0048_runtime.py` sur la **SQLite live**. Ce bridge est distinct du transfert #492 :

- il refuse toute révision autre que `0048_identity_admin_audit`;
- il vérifie le profil exact de schéma 0048 approuvé;
- il refuse un WAL actif;
- en mode apply, il exige un backup bit-à-bit distinct;
- il travaille sur une copie temporaire dans le même répertoire;
- il rejoue uniquement les deltas archivés 0049/0050 nécessaires à la baseline;
- il marque ensuite la copie à `v2_production_baseline` et exécute Alembic jusqu'au head courant;
- il valide la révision et les tables/colonnes courantes;
- il ne remplace la SQLite live qu'après succès complet et après avoir revérifié que la source n'a pas changé;
- le remplacement final utilise `os.replace` sur le même filesystem;
- en cas d'échec avant ce remplacement, la SQLite live reste inchangée.

Le flag `--confirm-live-runtime` est obligatoire avec `--apply` afin d'éviter d'utiliser accidentellement le snapshot ENV-492.

Exécution opérateur, application arrêtée :

~~~bash
docker compose down

docker compose run --rm --no-deps migrate \
  python tools/bridge_sqlite_0048_runtime.py \
  --database /data/resourceplanner.db
~~~

Le dry-run doit annoncer la révision source 0048 et le head courant. Ensuite seulement :

~~~bash
docker compose run --rm --no-deps migrate \
  python tools/bridge_sqlite_0048_runtime.py \
  --database /data/resourceplanner.db \
  --backup /data/resourceplanner-before-runtime-bridge-0048.db \
  --apply \
  --confirm-live-runtime
~~~

Puis vérifier :

~~~bash
docker compose run --rm --no-deps migrate python -m alembic current
docker compose up -d
~~~

La révision doit être le head courant du code. Les fichiers gelés sous `backups/sql-cutover/` restent à 0048 et continuent d'être les seules sources autorisées pour ENV-492.

## Source SQLite et backup

La source est ouverte via SQLite URI mode=ro avec PRAGMA query_only=ON.

Le dry-run calcule un SHA-256 avant et après l'inventaire. Le mode apply revérifie le hash juste avant commit.

Un fichier source-wal non vide est bloquant : la source doit être gelée/checkpointée afin que la sauvegarde soit autonome.

En mode apply :

- --backup est obligatoire;
- le backup doit être un fichier distinct;
- son SHA-256 doit être identique à celui de la source gelée avant toute mutation cible.

## Cible et replay

La stratégie de #492 est clean-target only.

La cible doit :

- être créée par la baseline/Alembic;
- avoir la révision exacte attendue;
- être vide pour toutes les tables KEEP et DROP;
- contenir exactement le singleton baseline planning_mutation_state.id = GLOBAL;
- ne contenir aucune ligne dans task_catalog_project_sync_state.

Le transfert n'est pas un merge/upsert.

Un replay après échec ou après un transfert déjà commité consiste à recréer la base cible propre, réappliquer la baseline #457, relancer le dry-run, puis relancer le transfert.

Une cible non vide est bloquée et les collisions de PK détectables sont rapportées.

## Ordre FK et transaction

L'ordre est dérivé de Base.metadata.sorted_tables, filtré aux tables KEEP.

Toutes les insertions KEEP sont exécutées dans une seule transaction cible. Avant commit, l'outil vérifie :

- le hash de la source;
- les comptes source éligibles vers cible;
- l'ensemble exact des PK;
- les liens des agrégats Planning;
- les liens WorkPackage/Delivery;
- les liens AssetRequirement/AssetAllocation.

Toute exception déclenche un rollback de toute la transaction. Les lignes REBUILD déjà créées par la baseline restent intactes.

## Dry-run

La variable RESOURCEPLANNER_DATABASE_URL est requise même en dry-run, mais aucune écriture n'est effectuée.

~~~bash
export RESOURCEPLANNER_DATABASE_URL='<secret SQL Server hors Git>'
python tools/cutover_sqlite_v2_to_sqlserver.py \
  --source /chemin/resourceplanner_server.db \
  --backup /chemin/resourceplanner_server.backup.db \
  --expected-target-revision <revision-baseline-courante> \
  --report /chemin/cutover-dry-run.json
~~~

Le rapport contient notamment : informations source/cible désensibilisées; révision source, cible et tête du code; tables ORM et classification; comptes source/éligibles/exclus/cible; exclusions DEMO/dev; sanitisation de FK; dépendances manquantes; collisions; anomalies; ordre de transfert; actions RESYNC post-cutover.

Aucune URL de connexion, credential, token ou secret n'est écrit au rapport.

## Apply — seulement après #457

~~~bash
export RESOURCEPLANNER_DATABASE_URL='<secret SQL Server hors Git>'
python tools/cutover_sqlite_v2_to_sqlserver.py \
  --source /chemin/resourceplanner_server.db \
  --backup /chemin/resourceplanner_server.backup.db \
  --expected-target-revision <revision-baseline-courante> \
  --baseline-ready \
  --apply \
  --report /chemin/cutover-final.json
~~~

Ne jamais utiliser --baseline-ready tant que #457 n'est pas réellement terminée et revalidée sur SQL Server.

## Rapport final

Après commit, le rapport ajoute : comptes transférés par table; contrôles d'identifiants; contrôles des liens métier; état transactionnel committed; révision Alembic cible; durée; actions RESYNC à exécuter.

En cas d'erreur d'insertion ou de validation, l'état transactionnel est rolled_back.

## Validation runtime après transfert réel

Sur la base SQL Server résultante, exécuter au minimum :

~~~bash
python tools/check_sqlserver_readiness.py
python tools/smoke_sqlserver_readonly.py
python tools/smoke_sqlserver_transaction.py
python tools/check_web_runtime.py
~~~

Puis valider le bootstrap break-glass #457, les synchros Acumatica et les scénarios métier de #208 avant de déclarer SQL Server autoritaire.

## État au développement initial #492

Le code #492 peut être fusionné avant la baseline finale, mais le cutover réel reste en attente de #457.

À la livraison initiale, aucune donnée utilisateur réelle n'est copiée vers SQL Server et aucune base active RPlanner n'est détruite ou modifiée.

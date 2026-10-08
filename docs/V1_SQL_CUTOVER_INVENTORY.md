# Inventaire de cutover V1 → SQL

Ce document suit la **fin du runtime NiceGUI/Excel**, pas la migration des données.

L'import one-shot Excel → SQL est déjà livré par #158 / PR #161 avec `tools/cutover_excel_to_sql.py` et `docs/SQL_CUTOVER_RUNBOOK.md`. Il ne doit pas être réécrit dans ce chantier.

## État de départ

Le produit possède maintenant deux chemins clairement distincts.

### Runtime Web/SQL cible

```text
React
  ↓ REST
FastAPI
  ↓
ApplicationFacade / services / moteur pur
  ↓
repositories SQLAlchemy
  ↓
SQLite dev/test aujourd'hui
SQL Server cible
```

Les packages canoniques protégés sont :

- `app/domain`;
- `app/application`;
- `app/infrastructure/sql`;
- `app/infrastructure/acumatica`;
- `app/server`.

Ils ne doivent pas acquérir de nouvelle dépendance vers NiceGUI, xlwings, openpyxl ou les modules V1 historiques.

L'inventaire initial a détecté **une dette déjà existante et explicitement baselinée** : `app/application/runtime_services.py` construit encore les services NiceGUI V1 avec les adapters `app.infrastructure.excel`. Ce bridge est utilisé uniquement par la composition/UI historique et doit disparaître avant le retrait final de V1. Le garde-fou fonctionne comme un *ratchet* : cette dette précise reste visible, mais toute nouvelle violation fait échouer la CI.

### Runtime V1 encore actif

Le lanceur historique reste :

```text
Lancer_Application.bat
  ↓
main.py
  ├─ NiceGUI
  ├─ app.runtime_composition
  ├─ ExcelRepository
  └─ PlannerUI
       ↓
       classeur Excel
```

`app/runtime_composition.py` rend explicite la dette de transition : compatibilités, modules V1 versionnés, pages NiceGUI et anciennes communications sont encore installés pour l'application historique.

## Profils de dépendances — 6B

Les dépendances sont maintenant séparées selon leur usage.

### `requirements-server.txt`

Profil **canonique Web/SQL** :

- SQLAlchemy;
- Alembic;
- FastAPI;
- Uvicorn;
- httpx.

Il ne contient ni `nicegui`, ni `xlwings`, ni `openpyxl`. C'est le profil à utiliser pour le futur runtime serveur.

### `requirements-legacy.txt`

Profil **V1 historique temporaire** :

- inclut `requirements-server.txt`;
- ajoute `nicegui`;
- ajoute `xlwings`;
- ajoute `openpyxl`.

`Installer.bat` utilise explicitement ce profil parce qu'il installe encore `Lancer_Application.bat` / `main.py`.

### `requirements.txt`

Profil agrégé de compatibilité pour la CI complète et les usages historiques existants. Il conserve temporairement les dépendances V1 de manière explicite afin que l'inventaire 6A continue de les mesurer. Il **ne doit pas être utilisé comme preuve des dépendances nécessaires au serveur**.

### Preuve CI d'isolation

La job `server-isolation` installe **uniquement** `requirements-server.txt`, puis :

1. compile les couches Web/SQL canoniques;
2. exécute le garde-fou d'architecture;
3. vérifie que `nicegui`, `xlwings` et `openpyxl` sont absents de l'environnement;
4. crée une application FastAPI sur SQLite en mémoire;
5. vérifie `/health`;
6. vérifie la présence des routes essentielles dans OpenAPI.

Cette job complète la CI historique : le projet doit désormais passer à la fois avec le profil serveur minimal et avec le profil agrégé tant que V1 existe.

## Inventaire automatique

Exécuter :

```bat
python tools\cutover_inventory.py
```

Pour le rapport JSON :

```bat
python tools\cutover_inventory.py --format json
```

Pour le garde-fou CI :

```bat
python tools\cutover_inventory.py --check-boundaries
```

La commande distingue :

- la dette de frontière **connue** au début du cutover;
- les violations **inattendues**.

Elle retourne un code d'échec uniquement pour une régression au-delà de la baseline explicite. Une baseline ne constitue pas une permission architecturale : elle doit rétrécir au fur et à mesure du cutover et ne doit jamais être élargie pour faire passer une PR.

L'inventaire expose séparément :

- les entrypoints V1 encore présents;
- les dépendances runtime historiques;
- les modules `v*.py`;
- les modules `*_compat.py`;
- les modules UI NiceGUI;
- les modules/adapters Excel;
- le nombre et les catégories des étapes de `runtime_composition`;
- l'outil de migration one-shot, qui demeure volontairement disponible jusqu'au cutover réel;
- la dette de frontière connue et les nouvelles violations éventuelles.

## Ordre de retrait recommandé

### 6A — frontières et inventaire ✅

Empêcher toute nouvelle dépendance Web/SQL → V1 et mesurer la dette existante, sans supprimer de comportement utilisateur.

### 6B — dépendances séparées

Valider le serveur avec un profil minimal sans NiceGUI/xlwings/openpyxl, tout en conservant un profil V1 explicite jusqu'au cutover.

### 6C — parité utile, pas parité historique aveugle

Comparer uniquement les fonctions NiceGUI encore réellement nécessaires au jour du cutover avec les surfaces React/FastAPI actuelles. Chaque manque sera classé :

- **migrer** : nécessaire à l'exploitation;
- **supprimer** : comportement historique devenu inutile;
- **reporter/remplacer** : fonctionnalité couverte par une architecture cible différente.

Les anciennes communications Outlook/Thunderbird ne doivent notamment pas être recopiées automatiquement dans React si #40/M365 constitue la cible retenue.

Le bridge `app/application/runtime_services.py` fait partie de cette dette : il pourra être déplacé/supprimé lorsque ses derniers appelants NiceGUI ne seront plus nécessaires.

### 6D — runtime Web autonome

Le lancement normal devra démarrer le backend FastAPI et servir/utiliser le frontend React sans `main.py`, `PlannerUI`, `ExcelRepository` ou `runtime_composition`.

L'ancien lanceur pourra être conservé temporairement sous un nom explicitement legacy pendant la transition.

### 6E — cutover réel

Cette étape attend la validation SQL Server #162 :

1. geler la V1;
2. archiver le classeur final;
3. exécuter l'import #158/#161;
4. réconcilier le rapport;
5. démarrer le runtime Web sur SQL Server;
6. effectuer les smoke tests lecture/mutation;
7. déclarer SQL autoritaire;
8. conserver Excel uniquement pour import/export/archive;
9. supprimer enfin le runtime V1 et les dépendances devenues inutiles.

## Règles non négociables

- aucune double écriture Excel + SQL;
- aucun fallback silencieux vers Excel après le cutover;
- aucun secret dans les fichiers versionnés;
- FastAPI reste la frontière de mutation métier;
- React n'implémente pas les règles de capacité, approbation ou non-double-comptage;
- les outils one-shot de migration peuvent conserver une dépendance Excel isolée sans que cette dépendance appartienne au runtime serveur;
- la baseline de dette de frontière doit seulement diminuer, jamais augmenter pour contourner le garde-fou;
- `pyodbc` ne sera ajouté au profil serveur qu'après la validation réelle SQL Server #162.

Refs : #15 #55 #158 #161 #162 #208 #209

---

## 336A — Inventaire définitif post-cutover (2026-10-08)

**Référence observée :** \`main@4462a1b5f790faf8b8ecb0944ef6ca95d6929c98\`, roadmap #55 (\`COCKPIT_PIPELINE_V1\`, \`336A READY / PARALLEL\`), issue #336, ADR-011 et ADR-014. #162 et ENV-208 sont terminés : **SQL Server est autoritaire en production**; SQLite reste autorisé pour le développement et les tests. Les paragraphes historiques « avant cutover » ci-dessus sont conservés comme témoignage de #208, **pas** comme procédure de production actuelle.

Cette tranche est **documentaire et non destructive**. Examen statique de l'arbre Git (\`main\`, 1 139 entrées, non tronqué), des points d'entrée, dépendances, imports/recherches de consommateurs, tests, workflows CI, Compose/Docker et runbooks. Les décisions ci-dessous fixent les destinations pour 336B–336D; **aucune de ces opérations n'est effectuée par 336A**. La recherche statique ne prouve pas l'absence de chargement dynamique : les contrôles de retrait ci-dessous restent obligatoires. Le garde-fou \`tools/cutover_inventory.py\` et son test ont été examinés; **il n'a pas été exécuté dans cet environnement d'inspection distante**. Ne pas assimiler l'analyse statique à un résultat de CI ou de test local.

**Légende :** **KEEP** = conserver fonctionnellement; **REPLACE** = conserver la capacité, remplacer son support obsolète; **ARCHIVE** = conserver la valeur historique hors du chemin actif; **DELETE** = retirer après suppression vérifiée de ses consommateurs. Une ligne **DELETE** n'autorise aucune suppression dans 336A. La mention « cible » désigne la tranche future autorisée.

### A. Runtime et modules : décision par famille, avec exceptions explicites

| Artefact(s) constaté(s) sur \`main\` | Décision | Preuve / destinataire et condition |
| --- | --- | --- |
| \`main.py\`, \`app/runtime_composition.py\`, \`app/ui.py\`, \`app/ui_context.py\` | **DELETE — 336B** | \`main.py\` charge \`nicegui\`, \`ExcelRepository\`, \`PlannerUI\` et les installateurs V1. Le serveur canonique démarre par \`python -m app.server\`; mettre à jour les tests du manifeste. |
| \`app/v13*.py\`, \`app/v14*.py\`, \`app/v15*.py\`, \`app/v16*.py\`, \`app/v172_nicegui_compat.py\`, \`app/v18*.py\`, \`app/bugfixes.py\`, \`app/features.py\`, \`app/features_runtime.py\` | **DELETE — 336B** | Installateurs et corrections versionnées mobilisés par \`runtime_composition\`; ne pas les confondre avec les modules \`app/domain/\` et \`app/application/\` qui restent canoniques. |
| \`app/*_ui.py\`, \`app/demand_requests_page.py\`, \`app/medium_term_page.py\`, \`app/segments_page.py\`, \`app/operational_planning_*.py\`, \`app/quick_shift_ui.py\`, \`app/shift_confirmation_ui.py\` | **DELETE — 336B** | Pages, renderers et événements NiceGUI V1; \`frontend/\` et les routes FastAPI couvrent l'UI supportée. Vérifier les imports indirects et préserver les tests métier indépendants. |
| \`app/excel_repository.py\`, \`app/infrastructure/excel/**\`, \`app/segment_repository.py\`, \`app/services.py\`, \`app/config.py\` | **DELETE — 336B** | Persistance classeur, schémas et configuration workbook/Windows V1. Plusieurs tests les importent encore; seuls ces consommateurs de compatibilité doivent être retirés. |
| \`app/application/runtime_services.py\` | **REPLACE — 336B** | Dette connue exacte de \`tools/cutover_inventory.py\` : imports \`app.infrastructure.excel\` par cette composition V1. Retirer le bridge et **réduire** \`KNOWN_BOUNDARY_DEBT\` à zéro, jamais l'élargir; garder \`app/application/{demand,planning,segment}_service.py\` et leurs contrats métier. |
| \`app/planning_cutover.py\`, \`app/planning_shadow.py\`, \`app/location_projection.py\`, \`app/medium_term_capacity.py\`, \`app/medium_term_renderer_compat.py\`, \`app/performance_diagnostics.py\`, \`app/runtime_performance_compat.py\` | **DELETE — 336B** | Adaptation Excel, shadow V1, projection/affichage NiceGUI et journal de performances V1; retirer également les outils exclusivement consommateurs identifiés ci-dessous. **KEEP** pour \`app/domain/planning_engine.py\`, \`planning_snapshot.py\`, \`plan_comparison.py\`, \`capacity_projection.py\`. |
| \`app/communication_*.py\`, \`app/outlook_drafts.py\`, \`app/thunderbird_*.py\`, \`app/communication_transport_excel.py\` | **DELETE — 336B** | Transports COM/Excel, UI Outlook/Thunderbird et native messaging V1. **KEEP** \`app/application/communications.py\`, \`app/domain/communication*.py\`, \`app/infrastructure/m365/**\`, \`app/infrastructure/smtp/**\` : Graph/SMTP Web ne doit pas disparaître. |
| Autres compatibilités racine \`app/*_compat.py\`, \`app/effort_identity_*.py\`, \`app/resource_local_preferences.py\`, \`app/resource_management_compat.py\`, \`app/resource_profile_migration_compat.py\`, \`app/schema_migration_compat.py\`, \`app/ui_mutation_guard.py\` | **DELETE — 336B** | Couches d'adaptation, préférences locales et migrations du classeur. Examiner usages/cas de test par fichier; ne jamais supprimer un invariant de \`app/domain/\` sous prétexte de nom « compat ». |
| \`app/infrastructure/migration/**\`, \`tools/cutover_excel_to_sql.py\`, \`tools/check_planning_shadow.py\` | **DELETE — 336B** | Import **historique** Excel V1 → SQL et diagnostic shadow : le premier go-live #457/#208 a retenu une base SQL Server neuve, sans import V1 obligatoire. Conserver la preuve/runbook historique; vérifier absence de dépendance de reprise active avant retrait. Ce n'est **pas** l'import ERP. |
| \`app/infrastructure/erp_export/**\`, \`tools/import_erp_projects.py\`, \`tools/import_erp_tasks.py\` | **KEEP** | Imports XLSX/CSV ERP vers SQL utilisés hors serveur via \`Dockerfile.importer\` et services Compose profil \`tools\`. Les imports sont distincts du classeur de planification V1. |
| \`app/application/**\`, \`app/domain/**\`, \`app/infrastructure/sql/**\`, \`app/infrastructure/acumatica/**\`, \`app/infrastructure/m365/**\`, \`app/infrastructure/smtp/**\`, \`app/server/**\` | **KEEP**, sauf bridge nommé ci-dessus | Frontières canoniques Python/SQL/OIDC/Graph/SMTP; garder aussi les diagnostics de récupération \`project_manager_legacy_diagnostic.py\` et leurs tests SQL même si « legacy » figure dans le nom. |
| \`frontend/**\`, \`migrations/**\`, \`alembic.ini\`, \`app/domain/cutover_policy.py\` | **KEEP** | React, schéma SQL et contrats purs; ni les migrations Alembic ni les invariants métier ne sont des reliques V1. |

Les motifs précédents sont **des groupes de fichiers actuels**, pas des commandes de suppression \`glob\` : en 336B, générer une liste exacte depuis le vrai \`main\` et exclure explicitement tout fichier avec consommateur actif. Les dossiers canoniques ne doivent pas être nettoyés par nommage heuristique.

### B. Installation, BAT, packaging, import et outil de développement

| Artefact(s) | Décision | Remplacement ou contrainte |
| --- | --- | --- |
| \`Lancer_Application.bat\`, \`Lancer_Application_Legacy.bat\`, \`Installer.bat\` | **DELETE — 336B** | Chaîne NiceGUI → \`main.py\` → workbook; ne constitue plus un démarrage supporté. |
| \`.github/workflows/windows-release.yml\` | **DELETE — 336B** | Packaging \`nicegui-pack main.py\` **et** \`PyInstaller tools/thunderbird_native_host.py\`; vérifier les références au native host/artefacts avant suppression. Aucun remplacement EXE de production requis : Docker/Compose fait autorité. |
| \`tools/thunderbird_native_host.py\` | **DELETE — 336B** | Exécutable/bridge spécifique au package Windows; retire les tests du protocole natif en même temps que le bridge V1. |
| \`Installer_Web.bat\`, \`Lancer_Web.bat\`, \`Verifier_Web.bat\` | **REPLACE — 336C** | Windows/Python global, \`.venv-web\`, build local et smoke. Capacités de production couvertes par Compose + probes; conserver les commandes \`python -m app.server\`, \`python tools/check_installed_web.py\`, \`python tools/smoke_running_web.py\` pour diagnostic local. |
| \`Lancer_Serveur.bat\` | **REPLACE — 336C** | Usage **réel** de dev API seul + Vite/HMR, documenté dans \`docs/REACT_V2_DEV.md\`. Garder le workflow \`python -m app.server\` + \`cd frontend && npm run dev\`, pas nécessairement le wrapper BAT. |
| \`Charger_Donnees_Demo.bat\` | **REPLACE — 336C** | Garder seed local explicite via \`docker compose --profile demo run --rm seed-dev\` ou CLI protégée \`--confirm-dev-only\`, **jamais** en démarrage/bootstrap de production (ADR-014 / #457). |
| \`Importer_Projets_ERP.bat\`, \`Importer_Taches_ERP.bat\`, \`Installer_Importateur_Projets.bat\` | **REPLACE — 336C** | Garder outils Python d'import et Compose \`import-projects\`/\`import-tasks\`; abandonner GUI Tkinter/venv Windows comme chemin d'exploitation. Préserver l'opt-in \`--apply\` et la prévisualisation. |
| \`Dockerfile.backend\`, \`frontend/Dockerfile\`, \`Dockerfile.importer\`, \`docker-compose.yml\`, \`deploy/synology/**\` | **KEEP** | Image backend installe \`requirements-server.txt\` et copie actuellement tout \`app/\`, y compris V1. En 336B, confirmer que les modules retirés ne sont plus dans l'image sans casser l'import SQL; Compose conserve import, migrations, backend et frontend. |
| \`tools/seed_demo_data.py\`, \`tests/test_demo_seed.py\` | **KEEP** | Code/dev fixtures \`DEMO-*\`, \`urn:resourceplanner:dev\`, profil \`demo\` **optionnel**; test refuse un moteur non-SQLite. Ne jamais l'utiliser pour créer un ADMIN production : \`tools/bootstrap_production_admin.py\` reste indépendant. |
| \`tools/bridge_sqlite_0048_runtime.py\`, \`tools/cutover_sqlite_v2_to_sqlserver.py\`, \`tools/check_sqlserver_readiness.py\`, \`tools/bootstrap_production_admin.py\` | **KEEP** | Migration/diagnostic SQL V2, préparation SQL Server et bootstrap break-glass; l'ancienne migration **SQLite V2** n'est pas l'import Excel **V1**. Examiner l'utilité future des outils one-shot séparément, sans les supprimer au titre de 336A. |
| \`tools/benchmark_planning_engine.py\`, \`tools/benchmark_v2_api.py\`, \`tools/privacy_scan.py\`, \`tools/check_server_dependency_isolation.py\`, \`tools/check_web_runtime.py\`, \`tools/ci_change_classifier.py\`, \`tools/cutover_inventory.py\` | **KEEP**, avec **REPLACE** des anciennes références dans le classifieur en 336D | Validation métier, sécurité, serveur et garde-fou du cutover. \`cutover_inventory.py\` protège la frontière pendant le retrait; ne pas effacer le ratchet pour faciliter la suppression. |
| \`tools/performance_report.py\` | **DELETE — 336B** | Lit \`app/performance_diagnostics.py\` (journal V1), pas les métriques canoniques FastAPI; préserver l'observabilité serveur existante. |
| \`requirements-legacy.txt\` | **DELETE — 336B** | Dépendances NiceGUI/\`xlwings\` propres au lanceur V1. |
| \`requirements.txt\`, \`constraints-release.txt\` | **REPLACE — 336D** | Le profil agrégé de tests installe toujours \`nicegui\`/\`xlwings\`/\`openpyxl\`; le fichier de contraintes comporte du packaging Windows. Recomposer selon la suite réellement conservée et retirer les pins inutiles seulement après 336B. |
| \`requirements-server.txt\`, \`requirements-importer.txt\` | **KEEP** | Serveur **sans** \`nicegui\`, \`xlwings\` ni \`openpyxl\`; importer **avec** \`openpyxl\`, de manière isolée. Pas de régression SQL Server/\`pyodbc\`. |

### C. Tests et automatisation : préserver les protections, pas le runtime V1

| Tests / CI constatés | Décision | Justification / cible |
| --- | --- | --- |
| \`tests/test_runtime_composition.py\`, \`test_nicegui_global_mutations.py\`, \`test_demand_editor_ui.py\`, \`test_segment_editor_ui.py\`, \`test_quick_shift_confirmation_ui.py\`, \`test_ui_context.py\`, \`test_ui_mutation_guard.py\`, \`test_operational_planning_runtime.py\`, \`test_operational_planning_runtime_syntax.py\` | **DELETE — 336B**, avec vérification des éventuelles assertions transverses | Couplées aux pages, installateurs, contrôles ou fichiers V1. Les invariants métier couverts ailleurs doivent rester testés dans \`tests/\`. |
| \`tests/test_excel_command_adapters.py\`, \`test_schema_migrations.py\`, \`test_resource_profile_migrations.py\`, \`test_communication_schema_migrations.py\`, \`test_planning_read_repository.py\`, \`test_v171_adapter_extraction.py\` | **DELETE — 336B** pour les tests du classeur; **REPLACE** si tests mêlés | Couvrent adapters/migrations Excel historiques; préserver toute vérification pure réutilisable au lieu de supprimer un fichier mixte aveuglément. |
| \`tests/test_thunderbird_*.py\`, \`tests/test_outlook_drafts.py\`, \`tests/test_cutover_cli.py\`, \`tests/test_cutover_preflight.py\`, \`tests/test_excel_cutover_extraction.py\`, \`tests/test_sql_cutover_importer*.py\` | **DELETE — 336B**, sous réserve de conserver les tests de contrats SQL non-V1 | Native host, Outlook COM, migration one-shot workbook. Les tests métier indépendants doivent être déplacés si nécessaire. |
| \`tests/test_repository_ports.py\`, \`tests/test_demand_service.py\`, \`tests/test_planning_service.py\`, \`tests/test_ownership_projections.py\` | **REPLACE — 336B** | **Mixte** : des assertions utilisent \`app.infrastructure.excel\` ou \`runtime_services\`, mais les tests de ports, services et règles métier sont à conserver. |
| \`tests/test_v1_web_parity_contract.py\`, \`tests/test_react_planning_nicegui_parity_contract.py\`, \`tests/test_shadow_equivalence.py\` | **KEEP**, renommage/actualisation possible en 336D | Vérifient des surfaces React/SQL et/ou comparaisons pures; leur nom V1/« shadow » **ne prouve pas** qu'ils sont obsolètes. |
| \`tests/test_cutover_inventory.py\`, \`tests/test_cutover_architecture.py\`, tests de frontière serveur, contrats API, migrations Alembic, Playwright | **KEEP / REPLACE les attentes V1 — 336B/336D** | \`test_cutover_inventory.py\` **affirme actuellement la présence** de \`main.py\`, des BAT, du profil legacy et de la dette connue : adapter ces assertions à l'**absence** attendue sans affaiblir la détection de régression. |
| \`tests/test_project_manager_legacy_diagnostic.py\`, \`tests/test_project_manager_legacy_static_audit.py\` | **KEEP** | Diagnostics sur relations historiques de données **SQL**, indépendants de NiceGUI/Excel. |
| \`.github/workflows/syntax-check.yml\`, \`tools/ci_change_classifier.py\`, \`docs/CI_PATH_MATRIX.md\` | **REPLACE les références — 336D** | \`syntax-check.yml\` surveille les BAT/\`main.py\`, compile \`main.py\` et installe \`requirements.txt\`; le classifieur reprend ces chemins. Retirer références **dans la même PR** que les fichiers concernés ou conserver provisoirement les vérifications jusqu'à 336D; ne jamais laisser une CI verte par omission. |
| \`.github/workflows/dev-cockpit.yml\`, \`dev-cockpit/**\`, suites SQL Server, tests de contrôle d'accès et Docker smoke | **KEEP** | Workflow opt-in, CI canonique et contrats post-cutover non dépendants de V1. |

La liste ci-dessus classe les **familles identifiées et les tests représentatifs**, sans décréter que tout test mentionnant \`excel\`, \`legacy\`, \`nicegui\` ou \`cutover\` doit être supprimé. Avant la suppression, rechercher les imports/lectures de fichiers et ventiler les tests mixtes **par assertion**.

### D. Documentation, métadonnées et racine

| Document / métadonnée | Décision | Trajectoire |
| --- | --- | --- |
| \`AUDIT_POST_V18.md\`, \`RELEASE_NOTES_V1.7.md\`, \`RELEASE_NOTES_V1.7.2.md\`, \`release/v1.7.1.json\`, \`release/v1.7.2.json\`, \`VERSION.txt\` | **ARCHIVE — 336D** | Histoire de la distribution Windows/NiceGUI; conserver uniquement la trace pertinente dans \`docs/archive/\` ou l'historique Git, hors racine active. |
| \`CODE_SIGNING.md\` | **ARCHIVE — 336D** | Explique les releases Authenticode EXE V1 et renvoie à \`windows-release.yml\`; ne plus la présenter comme publication supportée après 336B. |
| \`PLANNING_ENGINE_CUTOVER.md\` | **REPLACE — 336D** | L'architecture y fait encore référence : extraire les principes de moteur pur toujours vrais vers documentation active, conserver la chronologie V1 en archive seulement si utile. Corriger le lien dans \`docs/architecture/README.md\`. |
| \`README.md\`, \`docs/WEB_RUNTIME.md\`, \`docs/REACT_V2_DEV.md\`, \`docs/V2_RUNTIME_OPERATIONS.md\`, \`docs/SERVER_RUNBOOK.md\`, \`docs/V1_WEB_PARITY.md\`, \`docs/architecture/README.md\` | **REPLACE la prose obsolète — 336C/336D** | Ils documentent encore \`.venv-web\`, BAT et/ou un cutover « à venir ». Documenter Compose/VM Ubuntu/SQL Server autoritaire en exploitation, tout en conservant Vite/HMR pour dev. |
| \`docs/DEPLOYMENT_UBUNTU_VM.md\`, \`docs/SQL_CUTOVER_RUNBOOK.md\`, \`docs/SQLITE_V2_SQLSERVER_CUTOVER.md\`, \`docs/BREAK_GLASS_ADMIN.md\`, \`AGENTS.md\`, ADR acceptés | **KEEP** | Procédures et décisions importantes pour production, promotion, rollback, SQL, identité; annoter les anciennes étapes historiques sans effacer l'historique des décisions. |
| \`docs/V1_SQL_CUTOVER_INVENTORY.md\`, \`tools/cutover_inventory.py\` | **KEEP durant 336B–336D** | Traçabilité du retrait et contrôle frontière. À la clôture de #336, conserver une preuve minimale de la disparition des consommateurs V1. |

### E. Diff projeté, ordre et preuves de non-régression

Ce tableau est une **simulation de périmètre**, **pas un diff déjà appliqué** :

| Lot ultérieur | Diff cible | Critères de sortie |
| --- | --- | --- |
| **336B — runtime V1** | Retirer \`main.py\`, \`app/\` exclusivement NiceGUI/Excel, profil \`requirements-legacy.txt\`, BAT V1, workflow \`windows-release.yml\`, bridge Thunderbird/COM et tests exclusivement V1; corriger imports/tests/garde-fou. | Aucun chemin importé par FastAPI/ERP perdu; \`KNOWN_BOUNDARY_DEBT\` rétréci (cible : zéro); compilations et tests canoniques conservés; \`Dockerfile.backend\` ne livre plus le code V1 supprimé. |
| **336C — wrappers Windows** | Retirer les 8 BAT Web/dev/demo/import obsolètes; remplacer références et instructions par Compose/CLI et Vite/HMR. | Import ERP + \`--apply\` opérationnels; seed \`demo\` opt-in SQLite seulement, absent du bootstrap production; HMR reste possible; aucun launcher Windows exigé en exploitation. |
| **336D — finition** | Ajuster \`syntax-check.yml\`, \`ci_change_classifier.py\`, \`requirements.txt\`, contraintes de release, documents/archives, profils des tests. | Aucun chemin supprimé référencé dans CI/scripts/docs actifs; isolation serveur, shards Python, \`compileall\`, build React, E2E, \`docker compose config\`, smoke images, readiness SQL et privacy scan validés. |

**Recherche/contrôles à répéter sur le vrai \`main\` juste avant chaque retrait :**
1. \`python tools/cutover_inventory.py --format json\` puis \`python tools/cutover_inventory.py --check-boundaries\`; comparer compteurs/dette avant-après, ne pas augmenter la baseline.
2. Recherche globale de \`main.py\`, \`runtime_composition\`, \`app.infrastructure.excel\`, \`nicegui\`, \`xlwings\`, \`ThunderbirdHost\`, \`.bat\`, \`DEMO-\`, \`urn:resourceplanner:dev\` dans code, CI, tests, conteneurs et docs **en distinguant les tests négatifs légitimes**.
3. Valider \`python -m compileall -q app tests tools migrations\` après retrait de \`main.py\`, les shards Python et les contrats API; \`npm run build\` et Playwright pour React.
4. Valider \`python tools/check_server_dependency_isolation.py\`, \`python tools/check_sqlserver_readiness.py\`, \`docker compose config\`, smoke Docker/SQL et \`python tools/privacy_scan.py\`; **SQL Server réel** pour les exigences qui dépendent du moteur.
5. Tester explicitement l'import ERP, le mode Vite/HMR, le seed démo isolé, le bootstrap break-glass et le rollback SQL. Aucun fallback Excel silencieux ni réimport historique du classeur de planification.

**État de livraison 336A :** inventaire et décisions de conservation/retrait documentés; **zéro suppression, zéro modification runtime/CI/migration/test, zéro changement de roadmap**. L'exécution et la vérification des retraits restent réservées à 336B–336D après validation GitHub/DevCockpit.

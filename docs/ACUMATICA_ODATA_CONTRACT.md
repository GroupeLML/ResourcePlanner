# Acumatica — contrat OData cible

## Décision d'intégration

RessourcePlanner n'utilisera pas le **Contract-Based REST API** d'Acumatica pour ses synchronisations métier.

La direction retenue est :

```text
Acumatica OData
    ↓
adaptateurs Python
    ↓
ports applicatifs
    ↓
services de synchronisation
    ↓
SQL
```

Le navigateur React ne communique jamais directement avec Acumatica.

L'authentification OIDC des utilisateurs, lorsqu'elle est utilisée, reste un sujet distinct du protocole OData servant à lire les données ERP.

## Projets — RP_Projects

La vue OData cible pour les projets est :

```text
/oDATA/RP_Projects
```

Le nom d'hôte réel de l'ERP est une donnée d'environnement et n'est pas commité dans ce dépôt public.

Le flux observé est un feed **OData Atom/XML** avec notamment :

- namespace Atom;
- namespace `d` pour les propriétés;
- namespace `m` pour les métadonnées;
- entité `PX.Data.RP_Projects`;
- valeurs nulles via `m:null="true"`;
- chaînes pouvant contenir des espaces significatifs via `xml:space="preserve"`;
- dates typées `Edm.DateTime`.

Un échantillon anonymisé fidèle au contrat réel a été fourni pour guider l'implémentation et les tests.

## Identité projet

La clé externe stable retenue pour RessourcePlanner est :

```text
Project.erp_external_id = RP_Projects.ProjectId
```

`ProjectId` est l'identifiant unique du projet dans la base Acumatica.

`ProjectCode` n'est **pas** la clé technique : il correspond au numéro de projet utilisé à l'interne et doit rester la valeur métier affichée/recherchée.

Les URI Atom `self` / `edit` peuvent exposer d'autres composantes de clé OData, mais RessourcePlanner ne doit pas en déduire son identité métier tant que `ProjectId` est disponible comme identifiant stable Acumatica.

## Mapping RP_Projects → RessourcePlanner

| RP_Projects | Sémantique | Cible RessourcePlanner |
| --- | --- | --- |
| `ProjectId` | clé DB Acumatica unique | `erp_external_id` / identité externe stable |
| `ProjectCode` | numéro de projet interne | numéro de projet |
| `ProjectName` | nom/description du projet | nom |
| `CustomerID` | identifiant client ERP | donnée ERP descriptive/référence si utile |
| `CustomerName` | nom du client | client |
| `ProjectManagerId` | `EmployeID` ERP du chargé de projet | `Project.project_manager_external_id`, puis résolution stable vers `AppUser.employee_external_id → business_contact_id` |
| `ProjectManagerName` | nom affiché du chargé de projet | chargé de projet descriptif |
| `Status` | statut ERP | statut projet |
| `StartDate` | date de début | date de début si utilisée |
| `EndDate` | date de fin nullable | date de fin si utilisée |
| `DefaultBranchCode` | division | code de division |
| `DefaultBranchCode_Desc` | libellé de division | libellé descriptif |
| `LastModifiedDateTime` | dernière modification ERP | candidat pour synchro incrémentale |
| `BaseType` | type ERP du projet | classification ERP; règle de filtrage à confirmer |

## Divisions

Le mapping métier connu de `DefaultBranchCode` est :

| Code | Division |
| --- | --- |
| `110` | Électrique |
| `210` | Automatisation |
| `310` | Excavation |
| `510` | Mécanique industrielle |
| `910` | Interne |

Les codes présents dans l'échantillon anonymisé ne doivent pas être interprétés comme le mapping de production.

## Normalisation du feed

L'adaptateur OData devra au minimum :

- parser Atom/XML sans dépendre des préfixes exacts, mais selon les namespaces;
- convertir `m:null="true"` en `None`;
- appliquer `strip()` aux identifiants/codes textuels susceptibles d'être paddés;
- préserver correctement Unicode et caractères accentués;
- convertir les `Edm.DateTime` en valeurs temporelles Python cohérentes;
- exiger `ProjectId`, `ProjectCode` et `ProjectName` pour un projet exploitable;
- ne jamais utiliser le nom du projet ou du chargé de projet comme clé d'identité.

Le mapping chargé de projet confirmé est :

```text
trim(RP_Projects.ProjectManagerId)
        =
trim(RP_Employees.EmployeID)
        =
trim(RP_Users.EmployeID)
        ↓
AppUser.employee_external_id
        ↓
AppUser.business_contact_id
        ↓
Project.project_manager_contact_id
```

`RP_Users.UserID` reste l'identité du compte ERP et ne participe jamais à cette jointure. En absence d'`AppUser`/contact correspondant, l'identité et le libellé ERP sont conservés et le contact projet reste nul; aucun fallback par nom ou courriel n'est autorisé.

## Statuts

L'échantillon anonymisé couvre notamment :

- `Actif`;
- `En planification`;
- `Complété`;
- `Suspendu`;
- `Annulé`.

Ces valeurs servent au contrat de parsing. La liste exhaustive et les règles exactes d'admissibilité à la synchronisation doivent être confirmées contre le flux réel.

## Synchronisation

La sémantique applicative existante reste souhaitée :

- lecture Acumatica en lecture seule;
- rapprochement prioritaire par `ProjectId`;
- mise à jour idempotente;
- absence d'un projet dans un pull ≠ suppression/inactivation locale implicite;
- statut explicitement retourné par Acumatica peut mettre à jour le projet local;
- transaction SQL atomique pour l'application d'un snapshot;
- aucune donnée métier sensible dans les logs.

`LastModifiedDateTime` reste un candidat pour une future synchronisation incrémentale. Le smoke réel du 2026-09-24 a confirmé qu'il est exposé comme `Edm.DateTime`, filtrable avec des littéraux `datetime'YYYY-MM-DDTHH:MM:SS[.fff]'` (`eq`, `ge`, `gt`) et utilisable avec `$orderby`. L'heure OData observée était quatre heures en avance sur l'interface Acumatica au Québec (13:56 OData contre 09:56 UI), cohérente avec UTC versus EDT ce jour-là. Cette observation ne suffit pas à figer un algorithme incrémental ni une garantie générale de timezone.

Le même smoke a confirmé `$filter` sur `ProjectId` et `ProjectCode`, ainsi qu'une pagination déterministe par `$orderby=ProjectId asc`, `$top` et `$skip`. Trois fenêtres successives de cinq projets ont produit trois groupes distincts et aucun lien Atom `rel="next"` n'a été observé. La limite maximale acceptée par le serveur n'a pas été mesurée; le client utilise donc une taille de page configurable et accumule toutes les pages avant de remettre le snapshot au service applicatif.

## Authentification OData

Pendant la validation de développement, un compte utilisateur nominatif peut être utilisé temporairement pour accéder au feed OData.

Garde-fous obligatoires :

- aucun mot de passe, cookie, token ou autre credential dans Git;
- aucun credential dans les images Docker;
- aucun credential dans les logs ou routes de statut;
- configuration injectée uniquement par l'environnement/secrets du runtime;
- le compte nominatif est temporaire.

La cible d'exploitation est un **compte de service ERP dédié à RessourcePlanner** avec les permissions minimales de lecture nécessaires.

Le smoke réel du 2026-09-24 a confirmé **HTTP Basic** sur cette instance. Le runtime utilise un username et un mot de passe injectés uniquement par l'environnement/secrets; aucun credential ni hostname réel n'est exposé par les diagnostics sûrs. Le compte nominatif utilisé pour le smoke reste temporaire et doit être remplacé par un compte de service dédié avant exploitation durable.

## Ressources / employés / utilisateurs

Le contrat Employee/User est maintenant connu et documenté dans [integrations/acumatica/RP_EMPLOYEES_USERS.md](integrations/acumatica/RP_EMPLOYEES_USERS.md).

### RP_Employees

Feed :

```text
/oDATA/RP_Employees
```

Décision PO confirmée :

```text
RP_Employees.EmployeID = clé unique Employee
Resource.external_id = trim(EmployeID)
```

Le feed expose notamment `DisplayName`, `Email`, `Status`, département, classe ERP, superviseur, branche et `ContactID`.

`ContactID` n'est pas l'identité canonique RessourcePlanner.

Fixture anonymisée :

```text
tests/fixtures/acumatica/rp_employees_atom.xml
```

### RP_Users

Feed :

```text
/oDATA/RP_Users
```

Requête de synchronisation complète utilisée par 468B :

```text
/oDATA/RP_Users?$orderby=UserID asc
```

Le filtre historique `EmployeStatus eq 'Actif'` n'est plus appliqué par le runtime : `UserActif` et `EmployeStatus` sont conservés comme états source distincts, tandis que l'activation et les rôles RessourcePlanner restent locaux. Cette modification est validée contre fixtures/tests locaux; aucun nouveau smoke réel Acumatica n'est déclaré par 468B.

Décisions PO confirmées :

```text
RP_Users.UserID = clé unique User
RP_Users.EmployeID = FK logique vers RP_Employees.EmployeID
```

La relation métier autoritaire est donc `UserID → EmployeID → Resource.external_id`; le nom et le courriel ne servent jamais de clé ou jointure autoritaire.

Fixture anonymisée :

```text
tests/fixtures/acumatica/rp_users_atom.xml
```

### Activation RessourcePlanner

L'état source Acumatica et l'activation locale RessourcePlanner sont distincts.

- une ressource importée depuis `RP_Employees` est désactivée localement par défaut;
- un utilisateur découvert depuis `RP_Users` est désactivé localement par défaut;
- une synchronisation ERP ne doit pas activer automatiquement l'un ou l'autre;
- une ressource est effectivement planifiable uniquement si son statut ERP est admissible **et** si un ADMIN l'a activée localement;
- un utilisateur peut utiliser RessourcePlanner uniquement si son état source est admissible, s'il a été activé localement, si son identité OIDC est résolue et si des rôles locaux lui ont été attribués;
- aucun rôle privilégié n'est dérivé automatiquement de l'ERP.

La relation exacte `OIDC (issuer, subject) → RP_Users.UserID` reste à confirmer; ne pas supposer que `sub == UserID`.

Le développement reste contract-first : le développeur n'a pas besoin d'accès direct à Acumatica.

### Tâches / budgets — RP_ProjectTasks

Le feed est maintenant connu :

```text
/oDATA/RP_ProjectTasks
```

Requête utilisée pour l'échantillon contractuel :

```text
/oDATA/RP_ProjectTasks?$top=1000&$filter=Status eq 'Actif'
```

Le PO a observé environ **41 073 entrées** sans filtre. Le runtime ne doit donc pas synchroniser le feed global à haute fréquence ni interroger Acumatica lors de chaque affichage de liste.

Décisions PO confirmées :

```text
RP_ProjectTasks.TaskID   = clé unique/stable de la tâche ERP
RP_ProjectTasks.ProjetID = RP_Projects.ProjectId
Project.erp_external_id  = RP_Projects.ProjectId
Project.number           = trim(RP_Projects.ProjectCode)
BudgetAmount             = montant CAD
BudgetActual             = montant réalisé CAD
```

Le smoke réel du 2026-09-28 a confirmé que `ProjetID = ProjectId`, que `ProjectID_2` porte actuellement la même valeur numérique et que `ProjectID` est le code métier paddé correspondant à `ProjectCode`. `ProjectID_2` reste non canonique tant qu'aucun besoin concret ne justifie de l'utiliser comme clé.

La jointure technique ERP et le numéro métier affiché sont donc deux concepts distincts. `TaskCatalogItem.project_number` continue d'utiliser `Project.number`, jamais l'identifiant numérique ERP.

`TaskCD` est le code métier utilisé par les standards workforce administrables (#454). Exemples initiaux : 117 → installateur électrique, 216 → programmeur, 217 → installateur automatisation. Les standards peuvent être surchargés ou exclus par projet et restent distincts des `ApprovalScope` #276.

La synchronisation cible est **par projet + cache SQL local**. Après récupération d'un projet, seules les tâches ayant une classe workforce effective sont retenues pour le bootstrap opérationnel. Aucun pull global fréquent des ~41k lignes.

Pour une tâche classée :

```text
budget_hours = BudgetAmount_CAD / average_hourly_cost_CAD
```

Le coût moyen est configuré par ADMIN au niveau de la classe. Les heures calculées sont une projection budgétaire et ne réécrivent jamais silencieusement une demande ou un plan approuvé. `BudgetActual` reste un montant CAD distinct.

Aucun `LastModifiedDateTime` métier n'est présent dans le sample; ne pas inventer de curseur incrémental à partir du champ Atom `updated`.

Fixture :

```text
tests/fixtures/acumatica/rp_project_tasks_atom.xml
```

Documentation détaillée : [integrations/acumatica/RP_PROJECT_TASKS.md](integrations/acumatica/RP_PROJECT_TASKS.md).

Restent à valider : filtre OData par `ProjetID`, filtre `AccountGroup=DEPMO`, pagination/ordre stable sur `TaskID`, présence éventuelle de `rel=next`, taille maximale de page et cas représentatifs 117/216/217. L'absence de `LastModifiedDateTime` métier demeure inchangée.

## Outillage contractuel local

Les enveloppes Atom/OData communes sont centralisées dans `app/infrastructure/acumatica/odata_atom.py`. Cette primitive couvre uniquement la structure commune réellement réutilisable : namespaces Atom/`d`/`m`, `m:properties`, `m:null`, type `m:type`, `xml:space`, métadonnées d'entité/liens et classification HTTP stable.

Elle ne porte ni mapping métier, ni clé d'entité, ni pagination générique. `ODataProjectSource` conserve donc ses règles propres à `RP_Projects`.

Les contrats anonymisés peuvent être inspectés sans réseau avec :

```bash
python tools/inspect_odata_contract.py <sample.xml>
```

Le workflow complet et le template de nouveau contrat sont documentés dans `ACUMATICA_CONTRACT_WORKFLOW.md` et `docs/integrations/acumatica/CONTRACT_TEMPLATE.md`.

## Implémentation locale

Le runtime projet compose `ODataProjectSource`, un lecteur Atom/XML dédié derrière le `ProjectSourcePort` existant. `ProjectId`, `ProjectCode`, `ProjectName`, client, chargé de projet et statut sont projetés vers `ExternalProjectRecord` sans modifier `ProjectSyncService`.

`StartDate`, `EndDate`, `DefaultBranchCode`, `DefaultBranchCode_Desc`, `LastModifiedDateTime`, `CustomerID` et `BaseType` sont parsés dans le record d'infrastructure mais ne sont pas ajoutés au modèle SQL `Project`. Les valeurs `BaseType` observées sur l'instance sont `P` et `R`; les entrées `R` semblent correspondre à des templates. Cette interprétation reste à confirmer fonctionnellement et aucune règle de filtrage `BaseType` n'est appliquée dans #207B.

L'ancien `AcumaticaProjectSource` REST/JSON reste présent uniquement comme compatibilité historique; il n'est plus le chemin composé par le runtime projet.

## Validation restante

#207 est terminé. Le smoke réel du 2026-09-24 et la synchronisation de développement ont confirmé :

1. HTTP Basic avec credentials hors dépôt;
2. GET réel de `RP_Projects` en Atom/XML;
3. `ProjectId` comme identité stable et `ProjectCode` comme numéro métier;
4. `$filter`, `$orderby`, pagination `$top/$skip` sans `rel="next"`, et le comportement décrit ci-dessus de `LastModifiedDateTime`;
5. une première synchronisation de 1286 projets puis un replay idempotent avec 1286 entrées inchangées, sans doublon ni suppression locale implicite.

La signification métier exacte de `BaseType=R` et le remplacement du compte nominatif par un compte de service restent des sujets séparés du contrat projet livré; ils ne rouvrent pas #207.

La validation encore structurante pour les ressources est le **contract gate #232** : feed Employee/User validé par le PO, clé externe stable, admissibilité à la planification, relation User ↔ Employee, capacités de synchronisation propres à ce feed et fixture anonymisée fidèle. Une fois ce paquet fourni, #256 peut être développé sans accès ERP direct.

Refs : #207 #232 #256

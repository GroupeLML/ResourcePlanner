# Acumatica — contrat RP_ProjectTasks

## Décision produit

`RP_ProjectTasks` devient la source OData cible du catalogue de tâches projet et des informations de budget associées.

Le fallback Excel/CSV livré dans #271 reste disponible uniquement tant que l'adaptateur OData n'est pas livré et validé.

Le volume observé par le PO impose une stratégie de synchronisation **ciblée et mise en cache**. Le runtime ne doit pas relire le feed complet à chaque consultation de l'application.

## Feed

Chemin :

```text
/oDATA/RP_ProjectTasks
```

Requête utilisée pour produire l'échantillon anonymisé :

```text
/oDATA/RP_ProjectTasks?$top=1000&$filter=Status eq 'Actif'
```

Le PO a observé environ **41 073 entrées** lorsque le feed est interrogé sans filtre. Cette valeur est une observation de l'instance au 2026-09-25, pas une constante de contrat.

Format observé : OData Atom/XML.

Type d'entité :

```text
PX.Data.RP_ProjectTasks
```

Fixture anonymisée :

```text
tests/fixtures/acumatica/rp_project_tasks_atom.xml
```

## Champs observés

| Champ OData | Type observé | Sémantique / usage |
| --- | --- | --- |
| `ProjetCD` | string paddée | code projet descriptif observé; non autoritaire pour la jointure |
| `TaskCD` | string paddée | code tâche métier |
| `AccountGroup` | string paddée | groupe de compte du budget |
| `ProjetID` | `Edm.Int32` | identifiant technique projet autoritaire pour le ciblage/jointure |
| `TaskID` | `Edm.Int32` | identifiant tâche candidat |
| `TaskDescription` | string | libellé tâche |
| `Status` | string | état tâche; `Actif` observé |
| `StartDate` | `Edm.DateTime` | début |
| `EndDate` | `Edm.DateTime` / null | fin |
| `PlannedEndDate` | `Edm.DateTime` / null | fin planifiée |
| `Type` | string | type de tâche |
| `BudgetAmount` | `Edm.Decimal` | montant budgétaire de la ligne |
| `BudgetActual` | `Edm.Decimal` | réalisé budgétaire de la ligne |
| `BaseType` | string | type de base ERP |
| `ProjectID` | string paddée | numéro métier descriptif correspondant à `RP_Projects.ProjectCode` |
| `ProjectID_2` | `Edm.Int32` | valeur numérique observée égale à `ProjectId`; utile au diagnostic mais non canonique |
| `ProjectTaskID` | `Edm.Int32` | identifiant technique candidat de tâche projet |
| `CostCode` | string | code coût |
| `InventoryID` | string paddée | article/inventaire ou `<N/A>` |

Les chaînes paddées doivent être normalisées avec `strip()` lorsqu'elles servent de codes/identifiants.

Les montants doivent rester des `Decimal`; ne pas les convertir en `float`.

## Identité tâche et relation projet

Décisions PO confirmées :

```text
RP_ProjectTasks.TaskID   = clé unique/stable de la tâche ERP
RP_ProjectTasks.ProjetID = RP_Projects.ProjectId
```

Le smoke réel du 2026-09-28 a confirmé sur un même projet que `ProjetID` et `ProjectID_2` portent la valeur numérique de `RP_Projects.ProjectId`, tandis que `ProjectID` porte le numéro métier paddé correspondant à `RP_Projects.ProjectCode`.

Côté modèle local, les concepts restent séparés :

```text
Project.erp_external_id = RP_Projects.ProjectId
Project.number          = trim(RP_Projects.ProjectCode)
TaskCatalogItem.project_number = Project.number
```

`ProjectID_2` n'est pas promu en clé canonique. Les champs texte `ProjectID`/`ProjectCD` restent utiles pour la cohérence et le diagnostic, mais ne portent plus la relation technique autoritaire.

L'identifiant Atom expose plusieurs colonnes techniques de la vue OData, mais ces colonnes ne remplacent pas `TaskID` comme identité fonctionnelle de la tâche.

Le catalogue #271 utilise actuellement `(project_number, task_code)`. La transition OData doit donc ajouter un identifiant ERP `TaskID` nullable/stable sans casser les références historiques issues du fallback fichier.

## Budgets

Décision PO confirmée :

- `BudgetAmount` est un montant monétaire en **CAD**;
- `BudgetActual` est un montant monétaire réalisé en **CAD**;
- un budget peut être négatif;
- le réalisé peut dépasser le budget;
- pour les usages workforce, **seules les lignes dont `trim(AccountGroup) == "DEPMO"` sont importées**;
- `DEPMO` représente les lignes de main-d'œuvre : après ce filtre, `BudgetAmount` ne contient pas de matériel ni d'autres coûts à distinguer.

Les montants restent des `Decimal`.

Le filtre `AccountGroup = DEPMO` est une **règle métier autoritaire**, pas seulement une optimisation de performance. Le PO a observé environ **5 075 lignes** après application de ce filtre sur le feed au 2026-09-25; ce volume est indicatif et ne constitue pas une constante de contrat.

### Conversion budget → heures workforce

Pour les tâches associées à une classe de ressource, RessourcePlanner calcule une projection d'heures à partir du coût horaire moyen administré pour la classe :

```text
budget_hours = BudgetAmount_CAD / average_hourly_cost_CAD
```

Cette projection n'est pas un actual d'heures et ne doit jamais être confondue avec un Shift ou une ligne approuvée.

`BudgetActual` demeure un montant CAD réel. Il ne devient pas automatiquement un nombre d'heures autoritaire.

Si le coût est absent/0 ou si le budget produit une valeur incohérente pour Planning, le système doit afficher un diagnostic plutôt que fabriquer silencieusement un effort valide.

Voir #454.

## Classification workforce et import ciblé

Le produit ne doit pas importer/configurer manuellement toutes les tâches de tous les projets.

Un référentiel ADMIN séparé (#454) fournit :

- les classes de ressources;
- un coût horaire moyen CAD par classe;
- les standards globaux `TaskCD → classe`;
- les exceptions par projet.

Exemples initiaux :

```text
117 → Installateur électrique
216 → Programmeur
217 → Installateur automatisation
```

Ces correspondances sont administrables et **ne sont pas codées en dur**.

Résolution :

```text
override projet
    ↓ si présent
classe spécifique / EXCLUDE
    ↓ sinon
standard TaskCD
    ↓ sinon
non classé
```

Pour un projet ciblé, le pipeline workforce applique les filtres dans cet ordre :

1. conserver uniquement les lignes dont `trim(AccountGroup) == "DEPMO"`;
2. résoudre `TaskCD` via le standard global #454;
3. appliquer l'override projet éventuel;
4. ne conserver dans le catalogue workforce que les tâches ayant une classe effective;
5. agréger/calculer les budgets et projections d'heures uniquement à partir des lignes `DEPMO`.

Le filtre `DEPMO` doit être appliqué côté requête OData lorsque la capacité est validée, **et rester réappliqué côté adaptateur** comme garde-fou métier avant persistance. Cela évite de charger des coûts non-MO et laisse les exceptions projet possibles.

Cette classification workforce reste distincte des `ApprovalScope` de #276/ADR-010. Une même autorité d'approbation peut couvrir plusieurs classes de ressources différentes.

## Performance et stratégie de synchronisation

### Interdit

Ne pas :

- charger les ~41k lignes à chaque ouverture de l'application;
- appeler Acumatica à chaque rendu de dropdown;
- utiliser uniquement `$top=1000` comme pseudo-pagination;
- considérer l'absence d'une tâche dans un résultat filtré `Status eq 'Actif'` comme une preuve de désactivation;
- exécuter une synchronisation complète en boucle courte.

### Stratégie cible

Le catalogue local SQL reste la source de lecture du frontend.

La synchronisation OData doit être **ciblée par projet** et mise en cache.

Flux recommandé :

```text
Utilisateur ouvre/sélectionne un projet
        ↓
catalogue local disponible immédiatement
        ↓
si snapshot du projet absent ou expiré
        ↓
refresh OData ciblé du projet
        ↓
upsert transactionnel local
        ↓
nouveau last_synced_at projet
```

Pour un refresh projet, préférer un snapshot de **toutes les tâches du projet**, et non uniquement `Status eq 'Actif'`, afin que les passages Actif → Inactif puissent être observés explicitement sans interpréter une absence d'un résultat partiel.

La relation technique autoritaire est `RP_ProjectTasks.ProjetID → RP_Projects.ProjectId`. Le filtre serveur candidat est donc numérique, par exemple `ProjetID eq 5469`, sans guillemets. Le numéro métier `Project.number` reste utilisé pour la projection locale du catalogue et l'affichage.

### Cache

Prévoir un état de synchronisation par projet, par exemple :

- dernier succès;
- dernière tentative;
- dernière erreur;
- nombre de lignes reçues;
- nombre de tâches agrégées;
- durée;
- statut stale/fresh.

Le TTL doit être configurable. Pour le bootstrap/test initial, une valeur prudente telle que **24 h** avec bouton ADMIN « Rafraîchir maintenant » est acceptable; ne pas figer ce nombre comme contrainte métier durable.

### Pagination

Même avec un filtre projet, l'adaptateur doit supporter plusieurs pages.

Valider sur `RP_ProjectTasks` :

- `$orderby`;
- `$top`;
- `$skip`;
- absence/présence d'un `rel="next"`;
- limite maximale de page.

Utiliser un ordre déterministe. Ne pas supposer que les capacités déjà validées sur `RP_Projects` sont identiques avant smoke réel.

## Incrémentalité

Le sample ne contient pas de propriété métier `LastModifiedDateTime`.

Le champ Atom `<updated>` observé ne doit pas être traité comme un curseur métier fiable sans validation explicite.

En l'absence de curseur incrémental fiable, préférer :

- refresh ciblé par projet;
- TTL/cache;
- refresh manuel ADMIN;
- éventuellement une réconciliation périodique des projets réellement utilisés.

Éviter un polling global fréquent.

## Intégration avec #271 / #447

Le modèle durable de catalogue #271 doit être réutilisé.

La future source :

```text
ODataProjectTaskSource
        ↓
TaskCatalogSourcePort
        ↓
TaskCatalogSyncService
        ↓
TaskCatalogRepository
```

peut remplacer le fallback fichier sans changer les consommateurs.

Cependant, le contrat actuel `TaskCatalogSourcePort.list_tasks()` représente un snapshot global et le service rejette les doublons `(project_number, task_code)`. Pour `RP_ProjectTasks`, une extension ciblée par projet et une étape d'agrégation des lignes budgétaires seront probablement nécessaires.

Ne pas faire passer les lignes budgétaires brutes directement dans le service actuel comme autant de tâches.


## État d'implémentation #452

La tranche réutilise maintenant directement le référentiel #454 fusionné; aucun second modèle `TaskCD → classe` n'est introduit.

- parser Atom/XML `RP_ProjectTasks` avec `ProjetID` (Int32), `ProjectID` descriptif, `TaskID`, `TaskCD`, `AccountGroup`, `BudgetAmount` et `BudgetActual`;
- source OData ciblée par `Project.erp_external_id` avec pagination `$top/$skip` et ordre candidat `TaskID asc`, tout en recevant séparément `Project.number`;
- filtre serveur candidat `ProjetID + DEPMO`, puis garde-fou applicatif autoritaire sur `ProjetID` et `DEPMO`; le code projet texte reste une vérification secondaire;
- agrégation des lignes budgétaires `DEPMO` par `TaskID`;
- adoption additive du `TaskID` sur une ligne historique #271 ayant le même `(project_number, TaskCD)`, sans changer son identifiant SQL local;
- résolution de classe via les standards et overrides de #454;
- seules les nouvelles tâches ayant une classe effective entrent dans le catalogue workforce; une tâche historique devenue exclue/non classée reste persistée pour préserver les références mais disparaît des recherches workforce actives;
- persistance en `Decimal` de `BudgetAmount`, `BudgetActual`, du coût moyen utilisé et de la projection `budget_hours`;
- coût absent/0, budget nul/négatif, exclusion et absence de standard produisent des diagnostics explicites sans fabriquer d'effort Planning;
- métadonnées locales de dernier snapshot réussi par projet (lignes source, tâches agrégées, lignes rejetées, durée);
- aucune désactivation implicite en cas d'absence d'une tâche dans un snapshot;
- aucune projection n'est matérialisée en `Shift` et aucune demande/ligne approuvée n'est réécrite.

Le branchement HTTP réel dans `ServerSettings` / les routes FastAPI reste volontairement différé tant que le smoke réel ci-dessous n'a pas confirmé les capacités de `RP_ProjectTasks`.

### Smoke PO Acumatica restant avant branchement runtime

Sur un projet test connu contenant plusieurs lignes `DEPMO`, valider successivement :

1. `/oDATA/RP_ProjectTasks?$filter=ProjetID eq <PROJECT_ID>` ne retourne que le projet ciblé;
2. ajouter `and AccountGroup eq 'DEPMO'` et confirmer que les lignes retournées sont strictement `DEPMO`;
3. ajouter `$orderby=TaskID asc&$top=2&$skip=0`, puis `$skip=2`, et vérifier qu'aucune ligne n'est perdue ou dupliquée entre pages;
4. répéter avec une taille de page qui coupe plusieurs lignes partageant le même `TaskID`, afin de confirmer que `TaskID asc` suffit comme ordre stable ou d'identifier les champs secondaires nécessaires;
5. vérifier si le feed expose un lien Atom `rel="next"`; s'il existe, confirmer sa sémantique et sa compatibilité avec `$top/$skip`;
6. confirmer la limite maximale de `$top` réellement acceptée par cette vue.

Ces smokes doivent conserver le même mécanisme d'authentification Basic déjà validé par #207B et ne doivent jamais être remplacés par une supposition fondée uniquement sur `RP_Projects`.

## Questions encore ouvertes

Avant implémentation complète :

1. poursuivre la validation d'une requête OData ciblée sur **un seul projet** via `ProjetID`;
2. valider le filtre serveur `AccountGroup eq 'DEPMO'`; même s'il est supporté, conserver le garde-fou applicatif `trim(AccountGroup) == "DEPMO"`;
3. valider `$orderby=TaskID asc`, `$top/$skip` et la présence éventuelle de `rel=next`;
4. déterminer si un champ LastModified fiable peut être ajouté à la vue OData;
5. configurer les classes/coûts/standards #454 nécessaires dans l'environnement cible; leur modèle et leur résolution sont déjà livrés.

## Références

- #271 — catalogue tâches
- #232 — contrat Acumatica
- #447 — bootstrap données réalistes
- [ACUMATICA_ODATA_CONTRACT.md](../../ACUMATICA_ODATA_CONTRACT.md)
- [ACUMATICA_CONTRACT_WORKFLOW.md](../../ACUMATICA_CONTRACT_WORKFLOW.md)

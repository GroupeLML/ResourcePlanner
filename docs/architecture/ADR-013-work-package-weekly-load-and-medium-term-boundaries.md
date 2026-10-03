# ADR-013 — Charge hebdomadaire WorkPackage et séparation budget ERP / Moyen terme / Planning / Delivery

Status: Accepted
Date: 2026-09-29

> **Supersession partielle — ADR-019.** ADR-019 remplace les décisions de cette ADR concernant WorkPackageWeeklyLoad comme intention primaire, le lundi comme identité de saisie, l’exigence somme persistée = planned_hours, le modèle AUTO persisté, l’invalidité d’une répartition partielle et le statut librement fourni. Cette ADR reste Accepted et autoritaire pour ses autres frontières : tâche ERP, budget, capacité workforce, CAS WorkPackage, séparation Planning/Delivery et frontière de lecture hebdomadaire.

## Context

L'issue #502 fait évoluer la vue **Moyen terme** sans changer sa nature : elle reste un Gantt de pilotage des `WorkPackage`.

Deux lectures doivent y coexister sans devenir une nouvelle autorité de Planning :

1. la comparaison entre le budget de main-d'œuvre d'une tâche ERP et les heures structurées dans les WorkPackages liés;
2. la comparaison entre la charge hebdomadaire prévue des WorkPackages et la capacité hebdomadaire disponible des ressources humaines.

Le catalogue de tâches ERP existe déjà sous la forme `TaskCatalogEntry`, alimenté par `RP_ProjectTasks` (#452). La projection persistée `budget_hours` est dérivée de `BudgetAmount / average_hourly_cost` selon #454.

Le modèle actuel possède déjà `WorkPackage.planned_hours`, mais ne possède ni relation persistante WorkPackage → tâche ERP, ni version propre au WorkPackage, ni répartition hebdomadaire persistée. Les `RequestLine` peuvent déjà référencer séparément un WorkPackage et une tâche du catalogue.

Delivery (#362 / ADR-008) possède ses propres notions de référence, capacité réservée, travail restant, progression, forecast et concurrence. Planning conserve les `Shift` comme affectations/capacité réservée, jamais comme actuals.

La décision doit éviter quatre autorités concurrentes :

~~~text
Budget ERP par tâche
WorkPackage / Moyen terme
Planning / Shift
Delivery / Stories et forecast
~~~

## Decision

### 1. `TaskCatalogEntry` est l'identité de tâche ERP réutilisée par les WorkPackages

#502 ne crée pas de second modèle `ProjectTask`.

Le lien cible est :

~~~text
WorkPackage.task_catalog_item_id
    → TaskCatalogEntry.id
~~~

`TaskCatalogEntry.id` est l'identité locale stable utilisée pour la relation persistante. `TaskCD`, libellé, nom et proximité de dates ne sont jamais des preuves suffisantes de rattachement.

La cardinalité est :

~~~text
TaskCatalogEntry 1 ← 0..N WorkPackage
~~~

Plusieurs WorkPackages peuvent donc structurer le budget d'une même tâche ERP.

La cohérence de projet reste un invariant backend :

~~~text
TaskCatalogEntry.project_number
    == Project.number du WorkPackage
~~~

Aucune suppression en cascade d'une tâche référencée n'est permise.

### 2. Les nouveaux WorkPackages exigent une tâche ERP; l'historique reste nullable

Le schéma conserve `task_catalog_item_id` nullable afin de préserver les WorkPackages historiques.

Cependant, le contrat applicatif cible exige un rattachement explicite à une tâche ERP pour tout nouveau WorkPackage.

Aucun backfill automatique n'est autorisé à partir du `TaskCD`, d'un libellé, d'un nom, d'une demande dominante ou d'une ancienne révision portant seulement un code.

Un WorkPackage historique sans tâche reste valide, visible et régularisable explicitement.

### 3. La cohérence RequestLine ↔ WorkPackage ↔ tâche est un invariant backend

Lorsqu'une `RequestLine` référence un WorkPackage classé :

~~~text
RequestLine.work_package_id = W
W.task_catalog_item_id = T
~~~

alors la tâche effective de la ligne doit être `T`.

Le comportement cible est :

- tâche seule : accepté;
- WorkPackage classé seul : le backend dérive la tâche;
- WorkPackage classé + même tâche : accepté;
- WorkPackage classé + autre tâche : rejet explicite;
- ancien WorkPackage non classé : compatibilité conservée, sans déduction silencieuse.

Cette règle doit s'appliquer avant les décisions qui utilisent la tâche pour le routage ou l'approbation. Les anciennes révisions approuvées immuables ne sont pas réécrites.

### 4. `planned_hours` reste la charge totale du WorkPackage

Aucun nouveau champ `total_hours` n'est créé en parallèle. `WorkPackage.planned_hours` reste la référence de charge totale du WorkPackage pour #502.

Cette valeur n'est ni le budget ERP, ni un actual, ni une somme de Shift, ni le travail restant Delivery.

### 5. Le WorkPackage possède sa propre concurrence

Le WorkPackage devient un agrégat versionné avec une version persistante propre. Les mutations de tâche, dates, charge et répartition hebdomadaire utilisent un CAS sur cette version.

Une mutation purement WorkPackage n'acquiert pas `planning_version` et n'incrémente pas `delivery_version`.

Les opérations qui créent une nouvelle dépendance vers un WorkPackage et celles qui tentent de changer son projet ou sa tâche doivent partager une garde transactionnelle sur le même WorkPackage afin d'éviter une course entre la vérification « inutilisé » et la création d'une nouvelle référence.

Aucun mutex Python ni nouvelle version globale n'est introduit.

### 6. Le WorkPackage possède une répartition hebdomadaire persistante

La charge moyen terme est persistée sous le WorkPackage sous forme d'une collection hebdomadaire.

Le modèle cible est conceptuellement :

~~~text
WorkPackage
└── WorkPackageWeeklyLoad
      work_package_id
      week_start
      hours
~~~

L'identité d'une ligne est `(work_package_id, week_start)`. `week_start` représente le lundi de la semaine.

Une répartition validée respecte au minimum :

- une seule ligne par WorkPackage/semaine;
- `hours >= 0`;
- les semaines appartiennent à la fenêtre du WorkPackage;
- dates de début/fin présentes;
- charge totale connue et non négative;
- somme des semaines égale exactement à `planned_hours` selon la précision retenue.

Le WorkPackage porte aussi l'origine de la répartition :

~~~text
NULL   → aucune répartition validée
AUTO   → proposition backend acceptée
MANUAL → répartition explicitement ajustée
~~~

`AUTO` ne signifie jamais « recalculer automatiquement ».

### 7. Aucune redistribution silencieuse

Une répartition, y compris une proposition automatique déjà acceptée, représente une intention utilisateur persistante.

Modifier les dates, `planned_hours`, la tâche ou le statut ne redistribue jamais silencieusement les heures.

Si une mutation rend la répartition invalide, la commande doit exiger une nouvelle répartition cohérente ou être rejetée. La génération d'une proposition initiale reste une opération explicite et distincte de sa sauvegarde.

### 8. Budget et charge hebdomadaire sont deux agrégats distincts

La comparaison budgétaire utilise le budget de la tâche ERP et tous les WorkPackages budgétairement pertinents liés à cette tâche, indépendamment de l'horizon actuellement affiché dans le Gantt.

~~~text
planned_wp_hours =
    Σ WorkPackage.planned_hours budgétairement inclus

remaining_budget_hours =
    TaskCatalogEntry.budget_hours - planned_wp_hours
~~~

Une modification de fenêtre temporelle ne doit pas modifier artificiellement le total structuré contre le budget.

La charge hebdomadaire courante utilise uniquement les répartitions hebdomadaires persistées des WorkPackages actifs dans le périmètre.

### 9. Les statuts fermés restent dans le budget, mais sortent de la charge courante

Le comportement cible sépare la lecture budgétaire de la lecture de capacité :

- `planned` / `active` : inclus dans le budget et dans la charge hebdomadaire;
- `closed` et alias historiques équivalents : inclus dans le budget, exclus de la charge hebdomadaire courante;
- `cancelled` et alias annulés : exclus du budget et de la charge courante.

Fermer un WorkPackage ne doit donc pas libérer artificiellement du budget ni recréer un indicateur « budget sans WorkPackage ». Les alias de statut doivent être centralisés et réutilisés, pas recodés dans React.

### 10. Le budget ERP reste autoritaire sur la tâche

Le Gantt Moyen terme utilise la projection budgétaire persistée sur le catalogue de tâches ERP. Le budget n'est jamais copié comme autorité dans `WorkPackage`.

Une synchronisation ERP peut modifier le budget et ses diagnostics, mais ne modifie pas automatiquement les WorkPackages, leur charge hebdomadaire, les demandes approuvées, les Shift ou Delivery.

Une diminution du budget ERP peut donc produire un diagnostic de dépassement sans rendre rétroactivement illégal un plan déjà approuvé.

### 11. La capacité hebdomadaire est la capacité workforce disponible avant déduction des Shift

Pour #502, la capacité hebdomadaire compare la charge globale prévue des WorkPackages à la capacité humaine disponible issue des règles autoritaires existantes : ressources localement actives et ERP actives, horaire standard applicable, dates et jours applicables, vacances, jours fériés et autres indisponibilités déjà prises en charge par la politique commune.

Une ressource sans horaire applicable ne reçoit pas un fallback implicite à 8 h/jour.

La capacité de #502 n'inclut pas les actifs non humains, ne déduit pas les Shift, n'ajoute pas d'heures supplémentaires supposées et ne déduit pas Delivery.

Cette définition évite de comparer une charge globale WorkPackage à une capacité déjà diminuée par une autre représentation du même travail.

### 12. Le filtre projet ne crée pas un pool de capacité projet fictif

Un filtre projet réduit la charge WorkPackage affichée. Il ne réduit pas automatiquement la capacité workforce globale puisqu'aucune relation autoritaire ne définit actuellement un pool de ressources propre à chaque projet.

La présentation doit donc distinguer explicitement `charge du projet / capacité workforce` d'une hypothétique utilisation d'une équipe dédiée au projet.

Aucune relation « ressources pertinentes au projet » ne doit être inférée des Shift, des noms, des classes ou des approbateurs.

### 13. Moyen terme, Planning et Delivery restent séparés

La frontière est :

~~~text
Budget ERP
    → référence financière convertie en heures

WorkPackage.planned_hours
    → charge totale structurée

WorkPackageWeeklyLoad
    → placement temporel de cette charge à moyen terme

Shift
    → affectation réelle / capacité réservée du Planning

Delivery
    → découpage technique, progression, travail restant et forecast propre
~~~

Une édition de charge hebdomadaire WorkPackage ne crée ni ne déplace de Shift, ne change aucune enveloppe approuvée, ne modifie pas automatiquement les besoins, ne modifie pas les Stories et ne remplace pas le forecast Delivery.

La charge Moyen terme répond à « quand pensons-nous consommer la charge du WorkPackage? ». Le forecast Delivery répond à « combien de travail technique reste-t-il et quelle est sa couverture? ».

### 14. Le read model Moyen terme est dédié

Le snapshot Planning existant n'est pas redéfini pour porter cette nouvelle sémantique.

#502 introduit ou étend une projection dédiée au Gantt Moyen terme qui assemble côté backend la hiérarchie projet → tâche ERP → WorkPackages, le budget et les diagnostics, les répartitions hebdomadaires ainsi que la capacité et la charge hebdomadaires.

React reste responsable de la présentation du Gantt, pas des règles de budget, d'inclusion, de capacité ou de non-double-comptage.

### 15. #539 utilise le lundi courant comme frontière de lecture entre Actual ERP et charge future

Pour la vue Projet #539, le backend dérive une référence hebdomadaire unique à partir de sa date métier courante :

~~~text
reference_week_start = lundi de la semaine courante
actual_through_date = reference_week_start - 1 jour
~~~

Le contrat produit considère `TaskCatalogEntry.budget_actual_cad` comme la consommation ERP déjà absorbée avant `reference_week_start`. Le solde financier peut être converti en heures avec le même coût horaire moyen canonique #454 déjà projeté avec la tâche.

La charge future comparée à ce solde provient exclusivement des `WorkPackageWeeklyLoad` valides des WorkPackages courants liés par `task_catalog_item_id`, avec :

~~~text
week_start >= reference_week_start
~~~

Les semaines antérieures sont considérées comme déjà absorbées par `BudgetActual`; elles ne sont pas soustraites une seconde fois. Une répartition hebdomadaire pertinente absente ou incohérente rend la comparaison future incomplète au lieu de déclencher une redistribution implicite.

Cette lecture ne change pas la sémantique #502 de `remaining_budget_hours = budget_hours - planned_wp_hours`, qui reste une mesure de structuration totale distincte du solde basé sur `BudgetActual`.

## Alternatives considered

### Créer un nouveau modèle `ProjectTask`

Rejeté. `TaskCatalogEntry` représente déjà la tâche ERP persistée et possède une identité locale stable.

### Lier un WorkPackage par `TaskCD`

Rejeté. Le code de tâche n'est pas une identité suffisante pour une relation durable et ne garantit pas à lui seul la cohérence de projet.

### Backfiller automatiquement les WorkPackages historiques

Rejeté. Les données historiques disponibles ne constituent pas toujours une preuve suffisante du rattachement correct.

### Recalculer la répartition à chaque lecture ou changement de dates

Rejeté. Cela détruirait les ajustements manuels et transformerait un affichage en ordonnanceur implicite.

### Déduire les Shift de la capacité avant de comparer les WorkPackages

Rejeté. Le numérateur est déjà une charge globale de moyen terme; déduire les Shift du dénominateur risquerait de compter deux fois une même intention de travail.

### Utiliser le forecast Delivery comme charge Moyen terme

Rejeté. Delivery mesure le travail technique restant et sa couverture, tandis que la répartition WorkPackage représente le placement temporel prévu de la charge de référence.

### Réutiliser `planning_version` pour les mutations WorkPackage

Rejeté. Une mutation purement WorkPackage ne constitue pas une mutation globale du Planning et nécessite une concurrence locale propre.

## Consequences

### Positive

- le budget ERP reste unique et autoritaire sur la tâche;
- les WorkPackages peuvent être agrégés sans heuristique;
- la charge hebdomadaire devient explicite et ajustable;
- les modifications utilisateur ne sont pas écrasées par une redistribution implicite;
- la capacité est calculée avec les règles workforce existantes;
- Planning et Delivery conservent leurs autorités et versions propres;
- les WorkPackages historiques restent compatibles;
- le backend peut exposer un read model déterministe au Gantt.

### Trade-offs / negative

- une migration additive est nécessaire pour le lien tâche et la version WorkPackage;
- une table de charges hebdomadaires est nécessaire;
- les anciens WorkPackages non classés nécessitent une régularisation explicite;
- la création de nouveaux WorkPackages devient plus stricte;
- certaines mutations doivent protéger transactionnellement les références concurrentes vers le WorkPackage;
- les semaines sans répartition complète devront être signalées comme données incomplètes;
- la validation multi-session SQL Server est nécessaire pour les nouvelles garanties de concurrence.

## Implementation notes

- 502A introduit le lien `WorkPackage → TaskCatalogEntry`, la version/CAS, l'audit, les invariants de demandes et les protections de mutation.
- 502B introduit la comparaison budget ↔ WorkPackages et la première projection Gantt backend.
- 502C introduit la répartition hebdomadaire persistante et la capacité hebdomadaire backend.
- 502D livre le Gantt React, les indicateurs et les parcours d'acceptation.
- La table hebdomadaire utilise une précision cohérente avec `WorkPackage.planned_hours`; les calculs métier utilisent `Decimal`, pas `float`.
- Les migrations restent additives; aucune migration partagée existante n'est réécrite.
- SQL Server est la base de référence pour les tests de concurrence selon ADR-011.
- Les seuils visuels, noms précis des diagnostics, limites de fenêtre et détails d'endpoint restent dans #502 et ses sous-tranches; ils ne constituent pas des décisions structurantes de cette ADR.

## References

- #502 — Moyen terme : Gantt budget tâches ERP, WorkPackages et capacité hebdomadaire
- #539 — Vue Projet : référence hebdomadaire, BudgetActual et charge WorkPackage future
- #452 — RP_ProjectTasks OData, budgets et synchronisation ciblée
- #454 — classes de ressources, coûts moyens et standards TaskCD
- #362 — Delivery WorkPackage / Epics / Stories / Kanban
- ADR-006 — concurrence des mutations de Planning
- ADR-008 — frontière Delivery / Planning
- ADR-011 — SQL Server comme base de référence
- `docs/FUTURE_DELIVERY_VERIFICATION.md`

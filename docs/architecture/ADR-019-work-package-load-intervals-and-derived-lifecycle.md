# ADR-019 — Intervalles de charge WorkPackage, solde automatique et cycle de vie dérivé

Status: Accepted
Date: 2026-10-02

## Context

L’issue #591 fait évoluer la planification moyen terme des WorkPackages après les retours d’usage de #502.

ADR-013 a établi WorkPackage.planned_hours comme charge totale de référence, une répartition hebdomadaire persistée WorkPackageWeeklyLoad identifiée par un lundi, une somme persistée exactement égale à planned_hours, ainsi qu’un modèle AUTO/MANUAL représentant une intention acceptée. Ces décisions ont permis de livrer le Gantt moyen terme, mais imposent une saisie complète et hebdomadaire même lorsque l’utilisateur souhaite seulement positionner une partie de la charge.

Le besoin accepté par ASTRA-591 est désormais de permettre une intention explicite facultative et partielle, puis de dériver automatiquement le solde sans introduire une seconde autorité persistée. La même décision rend aussi le statut WorkPackage dérivé de faits de cycle de vie plutôt que librement éditable.

Cette ADR supersède donc partiellement ADR-013 uniquement pour les points explicitement listés ci-dessous. Les frontières budget ERP / WorkPackage / Planning / Delivery, la concurrence locale WorkPackage, la capacité workforce et les autres décisions d’ADR-013 restent autoritaires sauf indication contraire.

ADR-011 reste autoritaire pour SQL Server comme base de référence. ADR-015 reste autoritaire pour la classe canonique du WorkPackage.

## Decision

### 1. WorkPackage.planned_hours reste l’unique charge totale

Le WorkPackage conserve une seule autorité de charge totale :

~~~text
P = WorkPackage.planned_hours
E = somme des heures des intervalles explicites
A = P - E

0 <= E <= P
~~~

Aucune nouvelle colonne de total parallèle n’est créée.

Une collection d’intervalles explicites vide est valide. Une collection partielle est valide. Une somme explicite supérieure à planned_hours est invalide.

Le solde automatique A est dérivé. Il n’est jamais persisté comme seconde autorité.

### 2. L’intention persistée devient une collection d’intervalles explicites stables

La nouvelle intention persistée appartient au WorkPackage et porte au minimum :

~~~text
id
work_package_id
start_date
end_date
hours
~~~

L’identifiant de ligne est stable et indépendant des dates. start_date et end_date ne servent jamais d’identité persistée.

Les heures utilisent une précision SQL compatible avec le quantum de 0,01 h, par exemple Numeric(12,2).

Un intervalle explicite représente une intention utilisateur persistée. Il ne doit jamais être déplacé, tronqué, redimensionné ou supprimé silencieusement par une modification du WorkPackage.

### 3. Le solde automatique est projeté sur toute la période WorkPackage

Lorsque planned_hours et une période suffisante permettent la projection, le solde automatique A est réparti uniformément sur toute la période du WorkPackage.

La convention est :

- granularité journalière conceptuelle;
- jours calendaires;
- samedi, dimanche et jours fériés inclus;
- aucune dépendance aux disponibilités ou à la capacité workforce pour déterminer ces jours;
- les jours couverts par un intervalle explicite restent aussi admissibles au solde automatique;
- charge explicite et charge automatique sont additives;
- la projection complète est ensuite agrégée du lundi au dimanche pour les lectures hebdomadaires.

Les intervalles explicites ne retirent donc jamais leurs dates de la période utilisée pour répartir le solde automatique.

Lorsque les dates nécessaires à une projection automatique sont absentes ou incohérentes, le backend doit exposer un diagnostic d’incomplétude plutôt que d’inventer une fenêtre.

### 4. La précision métier est de 0,01 h

Les calculs utilisent Decimal ou une représentation équivalente en centièmes entiers. float n’est jamais une autorité métier pour ces calculs.

Lorsqu’une division du solde automatique laisse des centièmes résiduels, ces centièmes sont attribués de façon déterministe aux premiers jours chronologiques.

La projection est toujours calculée sur la période complète du WorkPackage avant tout filtrage de l’horizon affiché. Changer de semaine dans le Gantt filtre une projection existante; cela ne redistribue jamais les heures.

### 5. Les propriétés WorkPackage et les intervalles forment une mutation atomique

Une même commande doit pouvoir modifier atomiquement :

~~~text
propriétés WorkPackage
+
collection d’intervalles explicites
~~~

La concurrence réutilise WorkPackage.version et le CAS local existant. Les changements restent audités par WorkPackageAudit.

Les comportements autoritaires sont :

- augmenter planned_hours conserve les intervalles et augmente le solde automatique;
- diminuer planned_hours tout en restant supérieur ou égal à E conserve les intervalles et réduit le solde;
- diminuer planned_hours sous E est rejeté, sauf si la même commande réduit aussi les intervalles;
- étendre la période conserve les intervalles et recalcule le solde sur la nouvelle période;
- réduire ou déplacer la période de façon à exclure un intervalle est rejeté, sauf si la même commande corrige explicitement la collection;
- supprimer un intervalle retourne ses heures dans le solde automatique;
- supprimer tous les intervalles produit une répartition entièrement automatique.

Une mutation purement WorkPackage ne touche ni planning_version ni delivery_version.

### 6. Le statut WorkPackage est une projection de cycle de vie

Le statut n’est plus une valeur librement fournie par les commandes ou l’UI.

Le backend projette :

~~~text
annulation explicite enregistrée
    -> cancelled

clôture explicite enregistrée
    -> closed

WorkPackage ouvert + date de début atteinte
    -> active

WorkPackage ouvert + début futur ou absent
    -> planned
~~~

Clôturer et Annuler deviennent des actions métier dédiées, autorisées, validées et auditées.

Une date de fin dépassée ne ferme jamais automatiquement le WorkPackage. Elle peut produire un indicateur de retard.

Le statut active signifie que la période de planification a commencé; il ne prouve pas l’exécution terrain.

Clôturer ou annuler un WorkPackage :

- ne supprime aucun Shift;
- n’annule aucune WorkforceRequest;
- ne modifie pas automatiquement Delivery.

Le statut exposé aux API, projections et composants React est en lecture seule.

Pour les données historiques :

- les anciens alias closed/cancelled deviennent des faits terminaux migrés sans inventer une date réelle de clôture ou d’annulation;
- les anciennes valeurs planned/active restent historiques, mais la projection courante applique le nouveau contrat;
- toute valeur inconnue produit un diagnostic explicite plutôt qu’une normalisation silencieuse.

### 7. La migration de WorkPackageWeeklyLoad préserve l’intention historique

Les anciennes lignes AUTO et MANUAL ne deviennent pas du nouveau solde automatique. Dans ADR-013, AUTO représentait une proposition explicitement acceptée; elle est donc une intention historique persistée.

Chaque ancienne semaine valide devient un intervalle explicite :

~~~text
start_date = max(WP.start_date, week_start)
end_date   = min(WP.end_date, week_start + 6 jours)
hours      = anciennes heures de la semaine
~~~

Les heures historiques de chaque semaine sont conservées exactement. Une ancienne répartition complète produit donc un solde automatique nul.

La provenance historique AUTO/MANUAL doit rester traçable. La répartition journalière interne à une ancienne semaine est une convention de migration/projection; elle ne doit pas être présentée comme une donnée historique retrouvée.

Un WorkPackage sans ancienne répartition adopte le nouveau solde automatique lorsque planned_hours et des dates suffisantes permettent une projection.

Avant migration, un précontrôle détecte les anomalies. Aucune anomalie n’est corrigée silencieusement par suppression, écrêtement, redistribution ou normalisation arbitraire.

La migration est additive. La migration historique 0002_work_package_weekly_loads n’est jamais réécrite.

### 8. Les projections #502, #537, #539 et #556 convergent vers un read model backend commun

Les consommateurs utilisent une même projection autoritaire :

~~~text
charge projetée
= charge explicite
+ charge automatique
~~~

Pour #502 et #537 :

- le budget ERP reste distinct;
- la capacité workforce reste calculée côté backend selon les contrats existants;
- la capacité n’est pas diminuée implicitement des Shift.

Pour #539 :

~~~text
reference_week_start = lundi courant
~~~

La charge future est la partie de la projection complète située à partir de cette frontière. Les fractions antérieures ne sont jamais redistribuées artificiellement vers le futur.

Le passage d’une semaine dans l’UI filtre la projection; il ne recalcule pas sa répartition.

Pour #556, le read model peut exposer distinctement :

~~~text
charge explicite
charge automatique
charge totale
~~~

React ne recalcule aucune règle métier critique de répartition, d’inclusion ou de non-double-comptage.

### 9. Les frontières ERP / WorkPackage / Planning / Delivery restent séparées

ADR-019 conserve les frontières structurantes d’ADR-013 :

~~~text
Budget ERP
    -> référence financière externe

WorkPackage
    -> charge structurée moyen terme

Shift
    -> affectation réelle / capacité réservée Planning

Delivery
    -> découpage, progression, restant et forecast propre
~~~

En conséquence :

- éditer un intervalle ne crée ni ne déplace de Shift;
- aucune demande ni révision approuvée n’est modifiée automatiquement;
- aucun actual ERP n’est inventé;
- aucun transfert d’autorité vers Delivery n’est introduit;
- la classe canonique WorkPackage reste gouvernée par ADR-015;
- WorkPackage.version reste la concurrence applicable aux mutations pures de cet agrégat.

### 10. ADR-019 supersède partiellement ADR-013

Pour les nouveaux développements, ADR-019 remplace uniquement les décisions d’ADR-013 qui imposaient :

1. WorkPackageWeeklyLoad hebdomadaire comme intention primaire;
2. week_start = lundi comme identité de saisie persistée;
3. la somme des lignes persistées exactement égale à planned_hours;
4. l’ancien modèle AUTO accepté puis persisté comme répartition;
5. l’idée qu’une répartition explicite partielle serait invalide;
6. un statut fourni librement par les mutations ou l’UI.

ADR-013 reste Accepted et autoritaire pour ses autres décisions, notamment le rattachement à TaskCatalogEntry, le budget ERP, la capacité workforce, la version/CAS WorkPackage, la séparation Planning/Delivery et la frontière de lecture du lundi courant, telles qu’interprétées par la présente ADR.

## Alternatives considered

### Conserver une répartition hebdomadaire obligatoire

Rejeté. Cela maintiendrait l’obligation de décrire toute la charge alors que le besoin produit est de pouvoir positionner seulement les portions significatives.

### Persister le solde automatique

Rejeté. Cela créerait une seconde autorité devant être resynchronisée à chaque changement de planned_hours, de période ou d’intervalle.

### Distribuer seulement du lundi au vendredi

Rejeté. La convention PO est un étalement conceptuel en jours calendaires, indépendant des calendriers workforce.

### Utiliser disponibilité ou capacité workforce pour placer le solde

Rejeté. Cela transformerait le WorkPackage moyen terme en ordonnanceur de Planning et couplerait deux autorités distinctes.

### Convertir les anciens AUTO en nouveau solde automatique

Rejeté. Les AUTO historiques étaient des propositions explicitement acceptées et doivent rester une intention persistée.

### Fermer automatiquement un WorkPackage à sa date de fin

Rejeté. Une fin de période dépassée n’est pas une preuve de clôture métier.

### Conserver un champ status librement éditable

Rejeté. Le statut doit refléter des faits de cycle de vie cohérents et audités, pas une valeur arbitraire.

### Recalculer seulement sur l’horizon visible du Gantt

Rejeté. Le résultat dépendrait alors de la fenêtre d’affichage et pourrait déplacer des centièmes ou redistribuer la charge en changeant de semaine.

## Consequences

### Positive

- la répartition explicite devient facultative et partielle;
- planned_hours reste l’unique charge totale persistée;
- le solde automatique est déterministe sans seconde autorité;
- les changements de total ou de période ont un comportement transactionnel explicite;
- les intentions historiques AUTO/MANUAL sont conservées;
- les projections #502/#537/#539/#556 peuvent partager une seule logique backend;
- le statut WorkPackage devient cohérent, read-only et auditable;
- Planning, Delivery et budget ERP restent séparés.

### Trade-offs / negative

- une migration additive est nécessaire pour remplacer l’intention hebdomadaire primaire;
- la projection demande des calculs au centième sur une période complète;
- les anomalies historiques doivent être diagnostiquées avant migration plutôt que réparées automatiquement;
- les API et l’UI doivent distinguer actions de clôture/annulation et propriétés éditables;
- les consommateurs existants de WorkPackageWeeklyLoad doivent migrer vers le nouveau read model commun;
- une validation SQL Server est nécessaire pour les garanties de concurrence et de migration selon ADR-011.

## Implementation notes

- L’implémentation #591 doit utiliser une migration Alembic additive et ne doit pas réécrire 0002_work_package_weekly_loads.
- Les calculs métier de charge utilisent Decimal ou des centièmes entiers et conservent exactement les totaux au quantum de 0,01 h.
- Les commandes de modification doivent appliquer les invariants de collection avant commit et protéger la mutation avec le CAS WorkPackage existant.
- WorkPackageAudit reste le journal des mutations WorkPackage; la représentation exacte des faits terminaux et de la provenance de migration doit préserver leur auditabilité sans créer d’autorité parallèle.
- Les API de lecture exposent le statut dérivé; les mutations utilisent des actions dédiées pour Clôturer et Annuler.
- Les projections hebdomadaires sont dérivées côté backend à partir de la projection complète avant filtrage d’horizon.
- Les contrats de budget, capacité, classe WorkPackage, Planning et Delivery ne sont pas redessinés par #591.

## References

- #591 — Moyen terme — répartition WorkPackage facultative, intervalles datés et solde automatique
- #55 — roadmap maître
- #502 — Gantt budget tâches ERP, WorkPackages et capacité hebdomadaire
- #537 — projections Moyen terme associées
- #539 — Vue Projet : référence hebdomadaire, BudgetActual et charge WorkPackage future
- #556 — Gantt Moyen terme et regroupements
- ADR-011 — SQL Server comme base de référence
- ADR-013 — Charge hebdomadaire WorkPackage et séparation budget ERP / Moyen terme / Planning / Delivery
- ADR-015 — Classe canonique du WorkPackage

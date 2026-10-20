# ADR-031 — Exclusions de dates persistantes pour Quick Shift

Status: Accepted
Date: 2026-10-20

## Context

L'issue #755 modifie la sémantique de suppression d'un quart appartenant à un `ResourceRequirement` d'origine canonique `QUICK_SHIFT`.

Le besoin produit est le suivant : lorsqu'un coordonnateur supprime un Shift d'un Quick Shift multi-jour, les heures exactes du Shift supprimé doivent être retirées de `ResourceRequirement.planned_hours`, et le jour supprimé doit rester vide. Exemple : un Quick Shift lundi→jeudi de 32 h, réparti en quatre quarts de 8 h, devient 24 h si le mercredi est supprimé; lundi, mardi et jeudi restent en place et le mercredi ne doit pas être recréé.

Une simple réduction de `planned_hours` est insuffisante. ADR-001 conserve les Shift automatiques comme sorties remplaçables : lors d'un rebuild ultérieur, le moteur peut redistribuer les heures restantes et recréer un quart sur la date supprimée. Verrouiller silencieusement les survivants changerait également leur sémantique métier et n'est pas acceptable.

Il faut donc distinguer deux besoins :

1. préserver exactement les Shift survivants pendant le rebuild déclenché par la suppression;
2. mémoriser durablement que la date supprimée est exclue de la génération automatique pour ce Quick Shift précis.

Cette décision ne modifie pas la disponibilité générale de la ressource et ne s'applique pas aux besoins `REQUEST` ni aux origines génériques `AD_HOC`.

## Decision

### 1. Exclusion persistante par requirement et date

Introduire une relation additive persistante associant :

- l'identité canonique `ResourceRequirement.id`;
- une date exclue de génération automatique.

Cette relation représente une intention du coordonnateur :

> pour ce Quick Shift précis, aucune génération automatique ne doit produire de travail à cette date.

L'exclusion est propre au requirement. Elle ne rend pas la ressource indisponible pour d'autres besoins, ne modifie aucun `ResourceAvailabilityRule` et n'est pas une règle d'absence globale.

L'implémentation utilise une migration additive, sans modifier ou réécrire les migrations existantes.

### 2. Application à tous les chemins de génération automatique

Toute génération automatique liée au requirement doit respecter les dates exclues, notamment :

- répartition automatique normale;
- génération en heures supplémentaires;
- placeholders ou sorties de manque/capacité insuffisante lorsque ces sorties sont liées à une date de génération.

Une exclusion ne doit pas être contournée par un autre chemin automatique du même moteur.

Un placement manuel explicitement autorisé sur une date exclue reste possible selon les règles existantes de capacité, budget, permission et concurrence. L'exclusion interdit la génération automatique, pas toute utilisation humaine de cette date.

### 3. Préservation temporaire des survivants lors de la suppression

La suppression d'un Shift QUICK_SHIFT utilise un input backend étroit et temporaire vers le rebuild afin de préserver les Shift survivants du même requirement pendant cette mutation.

Le rebuild de suppression préserve au minimum :

- identité du Shift;
- ressource;
- date;
- heures;
- confirmation;
- `locked`;
- `source`.

Ces heures existantes comptent exactement une fois dans la consommation du besoin. Le moteur ne doit ni supprimer les lignes puis les recréer différemment, ni allouer une seconde fois par-dessus ces heures.

Cette préservation est spécifique à la mutation de suppression. Elle ne transforme pas les Shift automatiques survivants en Shift manuels/verrouillés permanents.

Après la transaction, les Shift automatiques restent automatiques et redeviennent remplaçables sur les dates autorisées selon ADR-001.

### 4. Suppression atomique et concurrence

La mutation de suppression d'un Shift QUICK_SHIFT :

1. acquiert la garde Planning/CAS existante avant les lectures décisionnelles;
2. charge le Shift et son requirement parent;
3. capture les heures exactes du Shift en `Decimal`;
4. si le requirement reste actif après suppression, réduit `planned_hours` exactement de ces heures;
5. crée ou conserve l'exclusion de la date supprimée;
6. exécute le nettoyage d'actifs existant;
7. supprime le Shift;
8. lance le rebuild avec la préservation temporaire des survivants;
9. persiste le tout dans la même transaction.

L'idempotence existante est réutilisée. Une répétition ou une course concurrente ne doit jamais soustraire deux fois les mêmes heures ni créer plusieurs exclusions équivalentes.

Une contrainte d'unicité logique sur `(resource_requirement_id, excluded_date)` doit empêcher les doublons.

### 5. Rebuilds ultérieurs

Lors de tout rebuild ultérieur, y compris un rebuild global déclenché par une mutation Planning sans rapport avec #755 :

- les Shift automatiques peuvent être recalculés sur les dates permises;
- la date exclue ne doit pas être réutilisée par la génération automatique de ce Quick Shift;
- une sauvegarde ordinaire ou un rebuild ne doit pas effacer silencieusement l'exclusion.

La durée de vie de l'exclusion suit celle du requirement actif. Une opération métier explicite pourra ultérieurement retirer l'exclusion si un besoin produit le demande; aucun effacement implicite n'est introduit par #755.

### 6. Dernier Shift et annulation canonique

`ResourceRequirement.planned_hours` conserve son invariant strictement positif.

Si la suppression retire les dernières heures actives du Quick Shift, ne pas écrire `planned_hours = 0` et ne pas supprimer physiquement le requirement.

Réutiliser le cycle de vie canonique `Annulé`, avec les contrôles d'actifs et les invariants existants. Le requirement annulé conserve ses heures historiques positives, mais ne contribue plus au Planning actif.

Cette annulation ne doit pas emporter du travail survivant incohérent par effet de bord. Si l'état historique est contradictoire, la mutation échoue de façon sûre plutôt que d'inventer une valeur négative ou nulle.

### 7. Scope strict

La décision s'applique uniquement à l'origine canonique `ORIGIN_QUICK_SHIFT`.

Ne pas inférer cette sémantique depuis :

- l'absence de `WorkforceRequest`;
- l'absence de numéro de demande;
- `AD_HOC`;
- la vue React utilisée;
- un libellé d'origine.

Les comportements `REQUEST` et génériques `AD_HOC` restent inchangés.

### 8. Frontend

React ne devient pas autoritaire pour :

- la soustraction des heures;
- la création des exclusions;
- la décision d'annulation;
- la préservation des survivants.

Le navigateur déclenche la commande existante et reflète le nouvel état retourné par le backend.

## Alternatives considered

### Réduire seulement `planned_hours`

Rejeté : un rebuild ultérieur peut recréer la date supprimée en redistribuant le reliquat automatique.

### Verrouiller tous les Shift survivants

Rejeté : cela transforme des sorties automatiques en décisions manuelles persistantes et contredit ADR-001.

### Modifier la disponibilité de la ressource sur la date supprimée

Rejeté : la décision concerne un Quick Shift précis, pas la capacité de la ressource à travailler pour d'autres besoins.

### Rétrécir automatiquement la fenêtre du requirement

Rejeté : une suppression au milieu de la fenêtre ne doit pas changer les dates du segment et cette stratégie ne couvre pas les trous internes.

### Sauter le rebuild après suppression

Rejeté : contourne le mécanisme canonique de cohérence Planning et laisse des risques de dérive avec les autres projections.

### Autoriser un requirement à zéro heure

Rejeté : contredit l'invariant persistant `planned_hours > 0` et introduirait un nouvel état métier inutile alors que le cycle `Annulé` existe.

## Consequences

### Positive

- l'intention utilisateur de supprimer une journée d'un Quick Shift survit aux rebuilds futurs;
- les heures du besoin restent cohérentes avec la suppression exacte;
- les Shift automatiques survivants conservent leur nature automatique;
- la disponibilité globale des ressources n'est pas polluée par une exclusion locale;
- la logique reste backend-authoritative et transactionnelle;
- la décision est compatible avec ADR-001, ADR-006 et le nettoyage d'actifs existant.

### Trade-offs / negative

- une nouvelle table/relation persistante et une migration sont nécessaires;
- les chemins de génération automatique doivent tous consulter les exclusions;
- le rebuild accepte un input de préservation temporaire supplémentaire pour la mutation de suppression;
- les tests doivent vérifier le comportement sur plusieurs rebuilds, pas seulement l'état immédiatement après suppression.

## Implementation notes

- ajouter une table/relation additive de type `QuickShiftPlanningExclusion` ou équivalent, référencée par `ResourceRequirement.id` et date, avec unicité sur la paire;
- ne pas coupler cette table à `ResourceAvailabilityRule`;
- intégrer les exclusions au modèle/input pur du moteur afin que tous les chemins automatiques les respectent;
- garder le mécanisme de préservation des survivants étroit et généré côté backend;
- réutiliser `planning_version`, l'idempotence `manual_allocation.delete`, le nettoyage `SHIFT_AD_HOC` existant et la transaction actuelle;
- couvrir une deuxième reconstruction déclenchée par une mutation Planning non liée;
- couvrir mixed locked/automatic, heures inégales, rollback, course concurrente/retry, actifs, annulation finale, REQUEST et AD_HOC inchangés.

## References

- GitHub Issue #755
- GitHub roadmap #55
- GitHub Issues #95, #331, #576, #659
- ADR-001 — séparation besoin cible / affectations Shift
- ADR-006 — version globale des mutations Planning
- ADR-016 — actifs ad hoc possédés par Shift
- `app/infrastructure/sql/command_adapters.py`
- `app/domain/allocation_rules.py`

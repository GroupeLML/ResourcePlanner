# ADR-022 — Dérogation opérationnelle persistante de fenêtre Planning

Status: Accepted  
Date: 2026-10-05

## Context

#613 demande qu'un coordonnateur autorisé puisse confirmer explicitement une extension de fenêtre lorsqu'un quart ou un segment doit être déplacé hors de l'enveloppe actuellement approuvée.

Le code actuel refuse volontairement les changements de dates qui sortent de l'autorisation approuvée et impose qu'un quart manuel reste dans la fenêtre de son segment. #333 fournit déjà un parcours de proposition/réapprobation pour les acteurs qui ne disposent pas d'une autorité spéciale.

L'analyse ASTRA-613 confirme qu'un simple assouplissement de `approve_demands` ou des validations de fenêtre serait incorrect : l'approbation actuelle passe par le quorum par ligne d'ADR-010/#276, et une extension opérationnelle ne doit ni approuver implicitement la candidate ni modifier la preuve approuvée.

## Decision

### 1. Une dérogation de dates est distincte d'une approbation

RessourcePlanner introduit une **dérogation opérationnelle persistante de fenêtre** liée au plan actif.

Cette dérogation :

- ne modifie pas la demande candidate;
- ne modifie pas les périodes candidates;
- ne réécrit pas la révision approuvée;
- ne modifie aucun vote ni quorum;
- ne transfère aucun budget, effort, quantité, qualification, projet, tâche, WorkPackage ou cible automatique.

La révision approuvée reste immuable. La fenêtre opérationnelle effective est dérivée de l'entrée approuvée et de sa dérogation active.

### 2. Permission dédiée

Une permission dédiée `override_planning_window` est introduite.

L'opération exige à la fois :

- `manage_planning`;
- `override_planning_window`.

La permission est attribuée explicitement aux rôles `COORDINATOR` et `ADMIN`.

L'autorité appartient à un `AppUser` authentifié et actif. Être nommé coordonnateur métier dans un `BusinessContact`, responsable opérationnel ou approbateur admissible d'une ligne ne confère aucune autorité implicite.

La permission reste globale selon le RBAC Planning actuel. #613 n'introduit pas un nouveau périmètre « coordonnateur assigné ».

### 3. Identité et preuve persistante

La dérogation cible un segment REQUEST précis et une entrée approuvée stable.

Elle référence au minimum :

- la révision approuvée active;
- l'identité canonique de la ligne/période/entrée autorisée;
- l'ancien intervalle;
- le nouvel intervalle;
- l'auteur stable;
- la date;
- le motif;
- une corrélation/idempotency key.

Elle refuse notamment :

- les références `LEGACY_UNKNOWN`;
- les segments REQUEST orphelins;
- les alternatives non actives.

### 4. Une dérogation élargit seulement la fenêtre

La dérogation n'autorise qu'un **élargissement** de l'intervalle approuvé.

Pour un déplacement hors fenêtre, l'intervalle opérationnel final est l'élargissement minimal contenant :

- l'ancienne fenêtre;
- la nouvelle date nécessaire au déplacement.

Une dérogation ne traduit pas silencieusement la fenêtre et ne la réduit pas.

Le quart manuel doit toujours rester dans la fenêtre finale de son segment.

### 5. Fenêtre opérationnelle effective commune

Tous les contrôles de fenêtre et projections concernés consultent la même autorité effective :

```text
entrée approuvée immuable
        +
dérogation opérationnelle active
        ↓
fenêtre effective du segment
```

La préparation commune du plan applique cette dérogation avant la validation des verrous.

Une resynchronisation ne reconstruit donc pas la fenêtre depuis la seule révision approuvée en perdant l'exception.

### 6. Réapprobation et alternatives

Lors d'une nouvelle approbation :

- si la nouvelle fenêtre approuvée couvre l'extension, la dérogation devient **absorbée** et reste historique;
- sinon, la dérogation n'est jamais transportée silencieusement vers la nouvelle révision;
- si sa disparition rend des quarts verrouillés incompatibles, la finalisation est refusée jusqu'à résolution explicite.

Un changement d'alternative ne transporte jamais la dérogation vers une autre entrée approuvée.

L'annulation conserve les protections existantes. Supprimer un quart déplacé ne réduit pas automatiquement la fenêtre dérogée.

### 7. Quick Shift et ad hoc restent autonomes

Les origines authentiques `QUICK_SHIFT` et `AD_HOC` conservent leur parcours existant.

Aucune demande, révision approuvée ou dérogation de demande artificielle n'est créée pour ces chemins.

### 8. Commande composite atomique

Les parcours « extension explicite du segment » et « déplacement/édition d'un quart » utilisent une frontière transactionnelle commune.

L'ordre logique est :

1. vérifier permissions et replay idempotent;
2. acquérir le CAS global Planning avant les lectures décisionnelles;
3. relire segment, quart éventuel, révision, entrée approuvée et état opérationnel;
4. valider motif, consentements nécessaires et état final projeté;
5. persister la dérogation, étendre le segment et effectuer le mouvement éventuel;
6. convertir un quart automatique déplacé en quart manuel verrouillé;
7. exécuter un seul rebuild, vérifier les invariants, écrire audit et reçu idempotent;
8. commit ou rollback intégral.

L'idempotence est liée à l'identité stable de l'acteur. Le replay d'un succès doit rester possible même si la version attendue fournie par le client est ensuite devenue ancienne.

Aucune nouvelle version propre au Shift ou au segment n'est ajoutée : `planning_version` et `RequestOperationalState.version` restent les mécanismes de concurrence.

### 9. Concurrence

Une modification purement candidate ne bloque pas une dérogation contre le plan actif.

Une réapprobation concurrente, en revanche, doit être détectée par :

- la garde Planning;
- la référence approuvée attendue.

Les scénarios de concurrence significatifs sont validés sur SQL Server conformément à ADR-011.

### 10. Pré-évaluation sans écriture

Le serveur peut exposer une évaluation préalable, sans mutation, qui distingue :

- étendre/déplacer dans l'autorisation actuelle;
- déroger explicitement;
- proposer une extension à réapprouver.

Le serveur revalide toutes les conditions lors de la commande réelle. React ne devient jamais l'autorité de la décision.

### 11. Consentement hors horaire séparé

La dérogation de fenêtre et le consentement explicite hors horaire de #536 sont deux décisions indépendantes.

Une extension de fenêtre n'autorise pas automatiquement un travail sur un férié applicable ou hors horaire. Inversement, un consentement hors horaire n'étend aucune enveloppe approuvée.

### 12. Responsabilité opérationnelle et actifs

ADR-020 reste autoritaire pour l'override explicite du responsable de quart et le contexte hérité capturé.

ADR-016 et ADR-018 restent autoritaires pour les actifs :

- une réservation `SEGMENT` conserve ses dates physiques propres;
- un actif `SHIFT_AD_HOC` suit le quart atomiquement;
- un conflit matériel fait échouer toute l'opération.

### 13. Relation avec les ADR d'approbation et de concurrence

Cette décision introduit une exception opérationnelle explicite aux contraintes de fenêtre, mais ne remplace pas les contrats suivants :

- ADR-003 : révision approuvée immuable;
- ADR-004 : séparation candidate / autorisation / plan actif;
- ADR-006 : CAS global des mutations Planning;
- ADR-010 : quorum d'approbation par ligne.

`KEEP_EXCEPTION` pour l'excédent d'heures n'est pas réutilisé implicitement pour les dates.

## Alternatives considered

### Réutiliser uniquement le parcours #333 / réapprobation

Préserve tous les contrats actuels, mais ne fournit pas la dérogation immédiate demandée au coordonnateur.

### Produire une nouvelle révision approuvée avec autorité spéciale limitée aux dates

Possible, mais exige de modifier le contrat de finalisation d'ADR-010/#276 et augmente le risque de confondre approbation de demande et décision opérationnelle.

### Réutiliser `approve_demands`

Rejeté. Cette permission ne garantit pas l'autorité globale nécessaire et détournerait le quorum existant.

### Modifier seulement `ResourceRequirement.start_date/end_date`

Rejeté. La resynchronisation reconstruirait les dates depuis la révision approuvée et perdrait l'exception.

## Consequences

### Positive

- l'approbation et la dérogation opérationnelle restent explicitement distinctes;
- la preuve approuvée et les votes restent immuables;
- l'exception survit aux resynchronisations;
- le déplacement hors fenêtre reste compatible avec l'invariant du quart manuel;
- le serveur conserve une seule autorité effective de fenêtre;
- audit, concurrence, replay et rollback sont déterministes;
- les acteurs sans permission spéciale conservent le parcours #333.

### Trade-offs / negative

- un nouvel état persistant de dérogation doit être modélisé et migré;
- une permission RBAC supplémentaire doit être gérée;
- la réapprobation doit traiter explicitement absorption, disparition et incompatibilités;
- les commandes de déplacement/extension deviennent plus riches;
- les tests doivent couvrir concurrence, idempotence, resynchronisation et actifs;
- l'UX doit exposer clairement motif, dérogation et consentement hors horaire comme décisions distinctes.

## Implementation notes

Le découpage autoritaire de #613 et #55 est :

```text
613A — jours fériés ciblés par classes
613B — modèle persistant, permission et fenêtre effective
613C — commandes atomiques, cycle de vie et concurrence
613D — API/React et acceptation transverse
```

613B–613D implémentent cette ADR séquentiellement après 613A.

## References

- #13 — séparation demande / approbation / plan actif
- #276 — quorum d'approbation par ligne
- #333 — extension de fenêtre contrôlée et déplacement
- #399 — annulation du plan matérialisé
- #536 — consentement explicite hors horaire
- #613 — jours fériés par classe et dérogation coordonnateur
- ADR-003 — révision approuvée immuable
- ADR-004 — candidate, approbation et plan actif
- ADR-006 — version globale des mutations Planning
- ADR-010 — périmètres et quorum d'approbation
- ADR-011 — SQL Server autoritaire
- ADR-016 — actifs ad hoc appartenant au Shift
- ADR-018 — contextes de réservation d'actifs
- ADR-020 — hiérarchie du responsable opérationnel et contexte approuvé

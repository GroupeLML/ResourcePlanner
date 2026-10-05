# ADR-021 — Jours fériés ciblés par classes de ressources

Status: Accepted  
Date: 2026-10-05

## Context

Les règles de disponibilité actuelles permettent des jours fériés globaux ou rattachés à une ressource individuelle, mais ne permettent pas de viser une ou plusieurs classes de ressources.

#613 demande de conserver le comportement historique tout en permettant, par exemple, qu'un férié s'applique à une classe comme « Installation électrique » sans affecter les autres classes. Le calcul de disponibilité et de capacité doit rester backend-authoritative et cohérent entre moteur, validations Planning, grille opérationnelle, recommandations et Moyen terme.

La décision doit aussi éviter qu'une migration ou la désactivation d'une classe transforme silencieusement la portée d'un férié existant.

## Decision

### 1. Ciblage multiclasse par relation explicite

Un jour férié peut référencer zéro, une ou plusieurs classes via une association M:N vers `ResourceClassConfig.code`.

La relation utilise le **code stable** de classe comme identité; les libellés restent de l'affichage.

L'association impose une unicité du couple règle/classe et des clés étrangères cohérentes avec le catalogue des classes.

### 2. Sémantique canonique d'application

| Configuration | Application |
|---|---|
| Férié sans ressource, aucune classe | Toutes les ressources, y compris non classées |
| Férié sans ressource, une ou plusieurs classes | Ressources dont `Resource.resource_class` appartient à la sélection |
| Férié individuel historique | Ressource désignée, comportement conservé |
| Ressource individuelle + sélection de classes | Refusé comme portée ambiguë |
| Horaire standard ou vacances + classes | Refusé dans #613 |

La classe évaluée est **la classe de la ressource concernée**. La classe demandée, la classe du WorkPackage et l'`ApprovalScope` ne déterminent pas l'applicabilité d'un férié.

### 3. Classes inconnues et inactives

Une classe inconnue est invalide et ne signifie jamais « toutes les classes ».

Une classe inactive déjà référencée par un férié reste conservée et visible. Sa désactivation ne transforme pas la règle en férié global.

Les nouvelles sélections administratives proposent uniquement les classes actives.

### 4. Contrat PATCH

Pour la sélection de classes :

- champ absent : sélection inchangée;
- liste vide : retour explicite à un férié global;
- liste non vide : remplacement explicite par les codes fournis après validation.

### 5. Compatibilité et migration

La migration est additive.

Les anciennes règles restent sans association de classes :

- les fériés globaux historiques demeurent globaux;
- les fériés individuels demeurent individuels;
- aucun backfill vers « toutes les classes actuelles » n'est effectué.

Un backfill vers les classes actuelles serait incorrect puisqu'il exclurait implicitement toute classe créée ultérieurement.

La migration ne déclenche aucun rebuild de planning et ne réécrit aucune migration déjà partagée.

### 6. Politique backend commune

`availability_state_for_day()` et les politiques communes de disponibilité restent l'autorité.

Le raccord doit couvrir le snapshot moteur, les validations manuelles, la grille opérationnelle, les recommandations et les capacités Moyen terme. Les associations de classes sont chargées en lot; aucun chemin ne doit introduire une requête par ressource et par journée.

React n'implémente aucun calcul parallèle d'applicabilité.

### 7. Consentement hors horaire inchangé

La politique de #536 reste indépendante :

- un férié applicable peut être franchi avec le consentement explicite prévu hors horaire;
- les vacances et l'absence d'horaire valide ne deviennent pas contournables;
- le ciblage par classe ne crée aucun nouveau mécanisme de dérogation.

### 8. Autorisation d'administration

L'administration réutilise la permission existante `manage_resources`.

Aucune nouvelle permission n'est introduite pour le ciblage de classes des fériés.

## Alternatives considered

### Dupliquer un férié par classe

Rejeté. Cette option multiplie les règles, complique leur cycle de vie et rend la portée globale difficile à exprimer durablement.

### Backfiller les fériés globaux vers toutes les classes actuelles

Rejeté. Une future classe ne serait alors plus couverte, ce qui changerait silencieusement la sémantique historique du férié global.

### Utiliser les libellés de classe

Rejeté. Les libellés sont mutables et destinés à l'affichage; les relations utilisent des codes stables.

### Recalculer la portée côté React

Rejeté. La disponibilité et la capacité restent backend-authoritative.

## Consequences

### Positive

- la portée des fériés devient explicite et stable;
- les données historiques conservent leur sémantique;
- les ressources non classées restent correctement couvertes par les fériés globaux;
- une classe inactive ne provoque aucun élargissement silencieux;
- toutes les projections peuvent partager la même politique backend;
- l'administration demeure compatible avec le RBAC existant.

### Trade-offs / negative

- une table d'association et des chargements batch supplémentaires sont nécessaires;
- les validations API doivent distinguer champ absent et liste vide;
- les consommateurs de disponibilité doivent recevoir explicitement le contexte de classe;
- les tests doivent couvrir la parité moteur/grille/Moyen terme et les classes inactives.

## Implementation notes

Le découpage autoritaire de #613 place cette décision dans **613A**.

613A doit couvrir le schéma/migration, les contrats API d'administration, la politique commune d'applicabilité, les projections concernées et l'acceptation transverse de cette politique. Elle ne doit pas introduire la dérogation de fenêtre Planning d'ADR-022.

## References

- #55 — roadmap maître / `COCKPIT_PIPELINE_V1`
- #211 — disponibilité / règles historiques
- #536 — consentement explicite hors horaire
- #537 — capacité Moyen terme par classe
- #613 — jours fériés par classe et dérogation coordonnateur

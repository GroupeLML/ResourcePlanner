# ADR-025 — Ressource attitrée par tâche projet et classement déterministe des recommandations

Status: Accepted  
Date: 2026-10-05

## Context

#617 ajoute une **ressource attitrée** à une tâche donnée d'un projet et veut l'utiliser comme signal prioritaire lors de la recommandation de ressources. Cette notion doit rester distincte du responsable opérationnel, du coordonnateur, du RBAC, de la cible automatique d'un besoin et des ressources réellement affectées aux quarts.

L'analyse ASTRA-617 a été réalisée en lecture seule sur `main@1f8d95705716d123157668d93aaa9a0291a5d240`. Elle confirme que `TaskCatalogEntry` possède déjà l'identité locale stable appropriée pour porter cette préférence et que le recommender actuel doit être restructuré par catégories plutôt que prolongé par un simple bonus de score.

La décision humaine accepte les contrats suivants avant d'autoriser 617A.

## Decision

### 1. La ressource attitrée est une préférence locale de la tâche projet

`TaskCatalogEntry` porte une FK nullable vers l'identité locale stable `Resource.id`, exposée métier comme **Ressource attitrée**.

Le nom d'implémentation recommandé est `preferred_resource_id`.

Règles :

- une seule ressource attitrée facultative par tâche projet;
- `null` signifie « aucune préférence »;
- aucune déduction depuis responsable opérationnel, coordonnateur, chargé de projet, anciens quarts ou anciennes cibles de besoins;
- la FK utilise `Resource.id`, jamais un nom, un courriel ou une identité utilisateur;
- la migration est additive et initialise les données historiques à `NULL`;
- aucune cascade ne supprime silencieusement une nomination.

Une préférence globale par `TaskCD` est interdite : deux projets peuvent avoir des ressources attitrées différentes pour le même code de tâche.

### 2. La nomination est locale, durable et sans propagation

La synchronisation ERP d'une tâche ou d'une ressource conserve la nomination locale.

Une ressource inactive ou `erp_active = false` reste référencée pour conserver l'historique et permettre un diagnostic, mais ne reçoit aucun avantage de classement et n'est pas sélectionnable.

Une réactivation peut rendre la préférence applicable après réévaluation.

Un changement de classe ou de compétences ne supprime pas la nomination : l'éligibilité est toujours évaluée contre le besoin courant.

La nomination :

- ne modifie pas `ResourceRequirement.assigned_resource_id`;
- ne crée, ne déplace et ne verrouille aucun `Shift`;
- ne déclenche aucun rebuild;
- ne dérive aucun droit RBAC;
- ne modifie ni responsable opérationnel ni coordonnateur.

### 3. L'administration possède sa concurrence propre

L'administration de la préférence réutilise l'autorité `manage_resources`.

Elle possède une version locale/CAS dédiée et un audit atomique contenant au minimum l'acteur stable, la tâche, l'ancienne ressource, la nouvelle ressource et la version.

Cette version n'est pas `planning_version` : modifier une préférence ne réserve aucune capacité et ne constitue pas une mutation Planning.

Une nouvelle nomination exige une ressource existante, localement active et active côté ERP. Sa disponibilité à une date précise ne conditionne pas l'enregistrement de la préférence.

### 4. La tâche de recommandation vient du contexte actif approuvé

Pour un segment `REQUEST`, la tâche de référence est `approved_task_catalog_item_id`, avec validation du projet et de la provenance.

Il est interdit de relire comme autorité la tâche d'une `RequestLine` candidate qui peut avoir changé pendant une réapprobation.

Deux temporalités restent volontairement distinctes :

- la tâche du besoin est celle du contexte approuvé actif;
- la préférence de cette tâche est lue dans son état courant au moment de la recommandation.

Modifier la préférence influence donc les recommandations futures sans modifier la révision approuvée, la cible automatique ou les quarts existants.

Pour `LEGACY_UNKNOWN`, une référence invalide ou un parcours sans tâche canonique réelle, aucune préférence n'est inventée.

### 5. Le classement utilise des catégories ordonnées, pas une somme de bonus

Le backend classe les candidats dans les catégories suivantes, dans cet ordre :

1. ressource attitrée, bonne classe, toutes les compétences, capacité prudente suffisante;
2. autres ressources avec bonne classe, toutes les compétences et capacité prudente suffisante;
3. bonne classe, capacité prudente suffisante, compétences incomplètes;
4. bonne classe, toutes les compétences, capacité positive mais insuffisante;
5. bonne classe, compétences incomplètes et capacité partielle;
6. bonne classe, aucune capacité prudente;
7. classe différente ou qualification non vérifiable.

Une ressource inactive n'est pas un candidat sélectionnable. Si l'attitré est inactif ou sans horaire, son identité et le motif restent visibles comme diagnostic de contexte.

**L'attitré ne reçoit aucun passe-droit.** S'il n'appartient pas à la catégorie 1, il rejoint sa catégorie réelle avec le badge « Attitré » et ne dépasse pas un meilleur candidat.

### 6. Le départage est déterministe

À l'intérieur d'une catégorie, l'ordre est :

1. nombre de compétences manquantes croissant;
2. capacité prudente restante décroissante;
3. charge tentative croissante;
4. nom normalisé;
5. `Resource.id`.

Le backend attribue les rangs; React conserve cet ordre.

Le badge « Recommandé » est réservé au premier candidat pleinement compatible des catégories 1 ou 2.

Si 1 et 2 sont vides, le premier candidat de la catégorie 3 peut être présenté comme **« Repli à confirmer »**. Aucun candidat partiel ou indisponible ne reçoit le badge « Recommandé ».

### 7. La disponibilité est calculée sur toute la fenêtre opérationnelle effective

L'évaluation porte sur toute la fenêtre opérationnelle effective du segment et sur son reliquat automatique courant, notamment `automatic_rebuild_hours`, pas uniquement sur la semaine affichée.

La capacité est calculée par ressource et par jour puis agrégée en réutilisant la politique commune de disponibilité et de consommation.

Les règles existantes restent applicables :

- exclure les sorties automatiques remplaçables du segment évalué;
- conserver la consommation des quarts verrouillés sur leurs ressources réelles;
- appliquer vacances, horaires et fériés par classe selon les mêmes contrats que les autres surfaces Planning;
- distinguer capacité prudente complète, capacité disponible seulement après retrait des tentatives, capacité partielle et absence de capacité.

La projection de recommandation est une estimation de l'état courant. Elle n'est ni une réservation ni une garantie du résultat d'un rebuild global.

### 8. Classe et compétences utilisent leurs références canoniques

La classe requise vient du besoin actif.

Les compétences utilisent les références canoniques, notamment `ResourceRequirementCompetency` lorsqu'elles existent. Les IDs ont priorité sur les noms et textes historiques.

Le contrat distingue explicitement :

- aucune contrainte renseignée;
- contrainte renseignée et satisfaite;
- contrainte renseignée et non satisfaite;
- contrainte historique non résolue.

Une donnée inconnue ne devient jamais automatiquement compatible. La classe courante du WorkPackage ou de la tâche ne remplace pas silencieusement celle du besoin.

### 9. La sélection manuelle reste une commande Planning distincte

Le parcours « Utiliser comme cible » conserve la sémantique d'ADR-001 : il modifie la cible du besoin et déclenche le comportement Planning autoritaire; il ne représente pas une affectation exclusive immédiate de tous les quarts.

Au clic, le backend revalide transactionnellement :

- l'identité du besoin et son contexte actif;
- l'état Planning selon ADR-006;
- l'existence et l'activation locale de la ressource;
- `erp_active`;
- les conditions nécessaires au choix demandé.

Un repli avec compétences manquantes requiert une confirmation explicite.

Un changement de préférence entre la lecture et le clic ne rend pas à lui seul invalide une sélection manuelle d'une ressource encore admissible.

Choisir une autre ressource ne modifie jamais automatiquement la nomination.

### 10. Performance et déterminisme du read model

Le recommender charge en lot le contexte de tâche, les compétences, la disponibilité et les consommations.

Les charges sont indexées en mémoire au minimum par `(resource_id, date)`. Aucun appel ERP et aucune requête par candidat/journée ne sont autorisés.

Le calcul de catégorie/rang doit pouvoir être isolé en fonction pure du domaine, tandis que le repository SQL prépare les données.

Le benchmark général `benchmark_v2_api.py` ne constitue pas une preuve suffisante : une mesure dédiée du chemin de recommandation doit être ajoutée.

## Alternatives considered

### Ajouter seulement un bonus « attitré » au score actuel

Rejeté. Le score actuel place certaines compétences avant la disponibilité, peut marquer `recommended=True` sans capacité suffisante et ne garantit pas les priorités métier de #617.

### Persister une table séparée de préférences

Rejeté pour la portée actuelle. Une préférence unique sans périodes de validité ni collection appartient naturellement à `TaskCatalogEntry`. Une table distincte pourra être reconsidérée si le domaine exige plusieurs préférences ou un historique temporel actif.

### Utiliser TaskCD comme clé de préférence

Rejeté. L'identité doit rester projet+tâche via `TaskCatalogEntry.id`.

### Donner un passe-droit à l'attitré

Rejeté. La préférence ne remplace ni disponibilité, ni classe, ni compétences, ni validation Planning.

### Déduire la compatibilité depuis des libellés

Rejeté lorsque les références canoniques existent. Les noms restent des libellés/diagnostics, pas des identités métier.

## Consequences

### Positive

- la préférence est locale, stable et distincte des responsabilités/permissions;
- les synchronisations ERP ne détruisent pas la nomination;
- le classement devient explicable, déterministe et conforme à l'ordre métier;
- l'attitré n'écrase jamais un candidat réellement meilleur;
- les diagnostics partiels/indisponibles restent visibles sans devenir des recommandations;
- la sélection manuelle reste sous les gardes Planning existantes;
- aucune nouvelle autorité de capacité ou d'affectation n'est introduite;
- le read model peut être optimisé sans N+1.

### Trade-offs / negative

- une FK nullable, une version locale et un audit supplémentaires sont nécessaires;
- le recommender doit être restructuré au-delà d'un simple ajustement de score;
- les cas historiques sans IDs canoniques doivent produire des diagnostics explicites;
- la disponibilité doit être évaluée sur la fenêtre complète, ce qui exige une préparation de données plus riche;
- une mesure de performance dédiée est nécessaire.

## Implementation notes

Découpage accepté de #617 :

```text
617A — modèle et administration ressource attitrée
617B — moteur recommandation ordonné
617C — UX et acceptation recommandation
```

Après acceptation d'ASTRA-617, **617A devient READY / PARALLEL**. 617B et 617C restent BLOCKED séquentiellement.

Scénarios d'acceptation importants :

- même `TaskCD` sur deux projets avec attitrés différents;
- synchronisation ERP préservant la nomination;
- désactivation locale/ERP puis réactivation;
- changement de tâche candidate sans effet sur le contexte approuvé actif;
- attitré indisponible derrière un candidat pleinement compatible;
- repli disponible devant un candidat compétent mais indisponible;
- compétences multiples par IDs, renommages et homonymes;
- fériés ciblés, tentatives, verrous et sorties automatiques remplaçables;
- égalités départagées par UUID;
- nomination sans rebuild, droit supplémentaire ou modification de quart;
- conflit concurrent de nomination et sélection devenue périmée;
- migration et CAS validés sur SQL Server.

La matérialisation de cet ADR est documentaire et ne constitue pas le démarrage de 617A.

## References

- #55 — roadmap maître
- #617 — ressource attitrée par tâche projet et recommandation ordonnée
- #273 — recommandations de ressources
- #454 — classes workforce
- #573 / ADR-017 — principal ERP et co-chargés RP
- #594 / ADR-020 — responsabilité opérationnelle
- ADR-001 — cible automatique distincte des quarts
- ADR-003 / ADR-004 — contexte approuvé actif
- ADR-006 — concurrence Planning
- ADR-011 — SQL Server autoritaire
- ADR-015 — classe canonique du WorkPackage
- ADR-021 — fériés ciblés par classes
- Analyse ASTRA-617, acceptée par décision humaine le 2026-10-05

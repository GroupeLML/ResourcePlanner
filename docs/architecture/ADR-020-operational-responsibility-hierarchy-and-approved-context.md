# ADR-020 — Hiérarchie du responsable opérationnel et contexte approuvé figé

Status: Accepted  
Date: 2026-10-04

## Context

#289 a introduit une résolution hiérarchique du responsable opérationnel, un comportement fail-closed pour les références explicites invalides/inactives et un contexte capturé sur `ResourceRequirement` afin qu'une modification candidate ne réécrive pas rétroactivement le plan déjà approuvé.

#573 et ADR-017 ont ensuite séparé le chargé principal ERP des co-chargés RessourcePlanner et confirmé que le dernier fallback du responsable opérationnel doit viser le principal ERP canonique, jamais un co-chargé arbitraire.

Le besoin de #594 ajoute trois niveaux locaux plus proches de l'exécution — projet, segment/besoin et quart — et exige que leur cycle de vie reste compatible avec la séparation demande candidate / autorisation approuvée / plan actif d'ADR-003 et ADR-004, la concurrence globale d'ADR-006, les décisions appartenant au Shift d'ADR-016 et les opérations atomiques de #332/#333/#399.

La décision doit éviter deux ambiguïtés :

- une hiérarchie dynamique qui ferait varier rétroactivement un plan déjà autorisé après modification d'une tâche, d'un projet ou du principal ERP;
- la confusion entre une décision locale explicite et le responsable effectif hérité à un instant donné.

## Decision

### 1. Trois overrides locaux explicites

RessourcePlanner ajoute conceptuellement trois relations distinctes et optionnelles :

```text
Project.operational_responsible_override_contact_id
ResourceRequirement.operational_responsible_override_contact_id
Shift.operational_responsible_override_contact_id
```

Ces relations sont des FK nullables vers `BusinessContact.id`.

Elles sont distinctes :

- du chargé principal ERP;
- des co-chargés RP définis par ADR-017;
- de `AppUser`;
- de l'override de `WorkforceRequest` capturé lors de l'approbation.

Pour ces trois relations, `null` signifie **hériter**. Il ne signifie ni « aucun responsable » ni « copier le responsable effectif ».

Aucun backfill ne doit être fabriqué à partir du principal ERP, d'un co-chargé, d'un nom, d'un courriel ou d'une ressource planifiable.

### 2. Hiérarchie canonique unique

La résolution canonique du responsable opérationnel est :

```text
Shift override
→ Segment / ResourceRequirement override
→ WorkforceRequest override
→ TaskCatalogEntry override
→ Project override local
→ Principal ERP canonique
```

Le premier niveau explicitement configuré et valide gagne.

Une référence explicitement configurée mais invalide ou inactive est **fail-closed** : elle produit un diagnostic et arrête la résolution. Elle ne déclenche pas un fallback silencieux vers un niveau inférieur.

La hiérarchie du coordonnateur demeure distincte et n'est pas modifiée par cette ADR.

### 3. Séparer candidat courant et contexte approuvé

Pour un plan issu d'une demande approuvée, les éléments hérités de responsabilité sont figés lors de l'approbation.

Une modification ultérieure de la demande candidate, de la tâche, de l'override projet ou du principal ERP ne modifie pas rétroactivement le responsable hérité du plan actif déjà autorisé.

Le contexte capturé doit représenter explicitement au minimum :

- l'identité retenue, ou un état non résolu explicite;
- la source métier ayant gagné la hiérarchie;
- l'entité source;
- la provenance et la version du mécanisme de capture;
- les diagnostics pertinents.

Une absence connue lors de l'approbation reste une absence connue pour cette autorisation; elle ne peut pas devenir plus tard un nouveau fallback opportuniste.

La capture couvre aussi les alternatives approuvées qui peuvent être matérialisées ultérieurement, afin qu'une sélection opérationnelle future reste liée à l'autorisation réellement approuvée.

### 4. Identité historique et état courant sont distincts

La projection doit distinguer :

- l'identité historiquement retenue;
- l'état actif/inactif actuel du `BusinessContact`;
- ses coordonnées actuelles.

Si un contact historiquement retenu devient inactif, son identité historique est conservée et son état courant est diagnostiqué. Aucun remplaçant n'est sélectionné automatiquement.

Cette séparation permet de préserver la preuve historique sans figer nécessairement des coordonnées qui doivent refléter la fiche contact actuelle.

### 5. Overrides opérationnels après approbation

Les overrides de `ResourceRequirement` et de `Shift` sont des mutations opérationnelles auditées sous la permission `manage_planning`.

Ils ne nécessitent pas une nouvelle approbation puisqu'ils ne changent pas le budget, la fenêtre, la qualification ni le quorum.

L'override de `WorkforceRequest` conserve le workflow existant de réapprobation sous `manage_demands`.

Retirer un override local signifie revenir au niveau inférieur du **contexte actif capturé**. Une telle suppression ne relit jamais la demande candidate courante pour reconstruire l'héritage.

### 6. Override d'un quart automatique

Poser directement un override de responsable sur un quart automatique constitue une décision manuelle portée par le `Shift`.

L'opération doit donc convertir explicitement ce quart en quart manuel verrouillé en réutilisant le parcours canonique de stabilisation d'un vrai `Shift`.

Conséquences :

- retirer ensuite l'override ne déverrouille pas automatiquement le quart;
- un retour en automatique est refusé tant qu'un override `Shift` subsiste;
- aucune persistance ni récupération par heuristique `date + ressource + libellé` n'est autorisée.

### 7. Cycle de vie des décisions locales

Le cycle de vie préserve l'intention explicitement persistée :

- modifier l'override d'un segment/besoin fait évoluer les `Shift` qui héritent de ce segment;
- un override `Shift` reste limité à ce quart;
- lors d'une réapprobation avec segment réutilisé, la nouvelle autorisation fournit une nouvelle base héritée alors que les overrides locaux explicites sont conservés;
- un nouveau segment ne récupère aucun override par heuristique;
- un rebuild préserve les décisions locales identifiées canoniquement;
- MOVE et extension + MOVE conservent l'override du `Shift`;
- SPLIT et DUPLICATE copient **l'override explicite lui-même**, y compris `null`, jamais le responsable effectif hérité;
- l'annulation/suppression de #399 reste autoritaire pour la destruction explicitement autorisée et l'information supprimée demeure auditable.

### 8. Quick Shift et ad hoc

Les chemins sans demande restent authentiques.

Leur hiérarchie est :

```text
Shift
→ Segment / ResourceRequirement
→ Projet local
→ Principal ERP
```

Un niveau tâche n'est utilisé que si une référence canonique réelle existe.

Le contexte hérité est capturé lors de la création opérationnelle avec une provenance adaptée au chemin ad hoc. Aucune `RequestApprovalRevision` n'est fabriquée et aucune approbation inexistante n'est simulée.

### 9. Concurrence, idempotence et sécurité

Pour les mutations segment/besoin et quart, ADR-006 reste autoritaire :

```text
replay idempotent
→ CAS expected_planning_version
→ relecture et validation du contexte
→ mutation
→ audit + reçu
→ commit transactionnel
```

Le CAS SQL de `planning_version` doit être acquis avant les lectures décisionnelles. La validation du contact et du contexte intervient après cette acquisition. Mutation, audit et reçu idempotent appartiennent à la même transaction et tout échec provoque un rollback complet.

Aucune version dédiée supplémentaire de `Shift` ou `ResourceRequirement` n'est introduite pour cette responsabilité.

L'override projet possède en revanche une version locale propre à cette configuration, avec CAS, audit et idempotence. Cette version ne détourne pas `Project.co_managers_version`, qui conserve la sémantique distincte définie par ADR-017.

Permissions architecturales :

```text
Project override      → manage_resources
Request override      → manage_demands
Segment / Shift       → manage_planning
```

Nommer un responsable opérationnel ne lui confère aucun rôle RBAC, aucun vote d'approbation, aucun scope projet et aucune permission implicite.

### 10. Communications et dashboards

Les décisions de #290, #573 et #593 restent applicables :

```text
To                      → principal ERP canonique
CC                      → ressources réellement affectées
Responsable opérationnel → contexte seulement
```

Le responsable opérationnel et les co-chargés ne sont jamais ajoutés automatiquement aux destinataires par cette hiérarchie. Un changement de responsable opérationnel ne déclenche pas à lui seul un envoi.

Lorsque plusieurs `Shift` d'une même tâche et d'une même journée ont des responsables différents, la projection doit préserver l'association **responsable ↔ affectation**. Une liste globale de noms qui perd cette correspondance n'est pas suffisante.

Les dashboards réutilisent la même résolution batch. Cette décision n'élargit pas implicitement « Mon périmètre » du coordonnateur.

### 11. Migration historique

La migration est additive et distingue explicitement trois provenances historiques :

1. responsabilité réellement capturée historiquement;
2. responsabilité **observée à la migration** lorsque le contexte existant peut être déterminé de manière fiable;
3. `LEGACY_UNKNOWN` lorsqu'il est impossible de reconstruire honnêtement l'histoire.

Une valeur observée à la migration ne doit jamais être présentée comme une « valeur approuvée autrefois ».

La migration ne doit jamais :

- relire la demande candidate pour fabriquer l'histoire;
- appliquer rétroactivement un nouvel override projet aux contextes déjà stabilisés;
- déclencher un rebuild;
- inventer une identité historique.

### 12. Relation avec ADR-017

ADR-020 remplace uniquement la hiérarchie de résolution du responsable opérationnel décrite dans ADR-017 §7 et les passages qui supposaient la chaîne limitée `Demande → Tâche → Principal ERP`.

ADR-017 reste **Accepted** et autoritaire pour :

- l'autorité ERP du principal;
- les co-chargés RP;
- la séparation entre principal et co-chargés;
- la résolution canonique de ces identités;
- le scope projet associé aux chargés;
- les effets des chargés sur les autres surfaces non remplacées;
- le principal ERP comme destinataire `To` et l'absence d'ajout automatique des co-chargés.

Le dernier fallback d'ADR-020 réutilise donc le **principal ERP canonique d'ADR-017**; il ne redéfinit pas sa résolution.

## Alternatives considered

### Laisser les fallbacks tâche/projet dynamiques après approbation

Rejeté. Une modification ultérieure d'une tâche, du projet ou du principal ERP pourrait modifier rétroactivement un plan déjà autorisé et mélanger demande candidate, autorisation approuvée et plan actif.

### Ajouter seulement trois FK sans contexte historique capturé

Rejeté. Les FK locales répondraient à l'édition courante, mais pas à la non-rétroactivité ni aux alternatives approuvées matérialisées plus tard. L'héritage d'un plan existant resterait dépendant de données courantes mutables.

### Persister le responsable effectif au lieu de l'override explicite

Rejeté. La persistance perdrait la distinction entre décision locale et héritage. SPLIT/DUPLICATE/rebuild ne pourraient plus savoir si une valeur doit suivre son parent ou rester une décision propre.

### Rattacher les overrides de quarts automatiques après rebuild par heuristique

Rejeté. Un rapprochement par date, ressource, libellé ou autre attribut mutable n'est pas une identité stable. Il risquerait d'attacher une décision manuelle au mauvais quart ou de la perdre silencieusement.

## Consequences

### Positive

- une seule hiérarchie backend détermine la responsabilité opérationnelle;
- les plans approuvés restent stables malgré les changements candidats ou organisationnels ultérieurs;
- projet, segment et quart peuvent exprimer une intention locale sans détourner le principal ERP ni les co-chargés;
- les décisions locales survivent aux opérations de Planning selon des identités canoniques;
- les chemins sans demande restent honnêtes sur leur provenance;
- les communications et dashboards peuvent expliquer précisément quelle source a gagné;
- les références invalides restent visibles au lieu d'être masquées par un fallback.

### Trade-offs / negative

- un stockage versionné supplémentaire est nécessaire pour le contexte capturé et sa provenance;
- les mutations projet et Planning ont des protocoles de concurrence distincts à respecter;
- un override de quart automatique a un effet durable sur la nature manuelle/verrouillée du `Shift`;
- les projections de communication doivent conserver l'association responsable ↔ affectation;
- les données historiques nécessitent une stratégie explicite `capturé / observé / LEGACY_UNKNOWN`;
- plusieurs consommateurs de #289 doivent converger vers la résolution commune étendue.

## Implementation notes

Le découpage autoritaire demeure celui de #594 et #55 :

```text
594A — schéma persistant, provenance et stratégie de reprise
594B — snapshot immuable, résolveur commun et resynchronisation
594C — commandes, concurrence, audit et cycle de vie Planning
594D — API/React, communications, dashboards et acceptation transverse
```

Cette ADR ne commence aucune de ces tranches.

Points à préserver pendant l'implémentation :

- migrations additives SQLite/SQL Server;
- aucun backfill heuristique et aucun rebuild pendant la migration;
- un seul résolveur canonique batch, pas un second moteur parallèle;
- source métier et provenance temporelle distinctes;
- `BusinessContact.id` comme identité des overrides;
- fail-closed pour toute référence explicite invalide/inactive;
- ADR-006 pour les mutations Planning;
- version locale dédiée pour l'override projet, distincte de `co_managers_version`;
- audit durable et replay idempotent;
- conversion explicite AUTO → MANUAL/verrouillé pour un override `Shift`;
- aucune règle RBAC ni hiérarchie métier critique dupliquée dans React;
- validation SQL Server réelle pour les migrations et concurrences concernées.

## References

- #55 — roadmap maître / `COCKPIT_PIPELINE_V1`
- #289 — contacts métier et responsable opérationnel
- #290 — communications projet
- #331 — besoin/budget vs affectations réelles
- #332 — partage et duplication atomiques de quarts
- #333 — extension de fenêtre et déplacement
- #399 — annulation et libération explicite du planning
- #573 — principal ERP et co-chargés RP
- #593 — destinataires ressources canoniques et navigation vers demande
- #594 — hiérarchie complète du responsable opérationnel
- ADR-003 — révision approuvée immuable
- ADR-004 — proposition candidate, autorisation approuvée et plan actif
- ADR-006 — révision globale des mutations de planning
- ADR-016 — décisions ad hoc appartenant au Shift
- ADR-017 — autorité du chargé principal ERP et co-chargés RessourcePlanner

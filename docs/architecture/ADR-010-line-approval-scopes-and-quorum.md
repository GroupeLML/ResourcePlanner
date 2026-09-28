# ADR-010 — Périmètres métier et quorum de multi-approbation par ligne

Status: Accepted  
Date: 2026-09-24

## Context

Après #288, une WorkforceRequest peut contenir plusieurs RequestLine portant des tâches ERP et des responsabilités métier différentes. Le workflow historique approuve pourtant la demande en une seule transition. #276 doit introduire plusieurs autorités possibles sans confondre identité applicative, contact métier, classe technique de ressource, révision approuvée #13 et plan actif.

## Decision

Le quorum de #276 est **ET entre les lignes et OU entre les approbateurs admissibles d'une ligne**.

Une demande n'est globalement approuvée que lorsque chaque ligne active exigeant une approbation est satisfaite. Une ligne est satisfaite dès qu'au moins un AppUser appartenant à son ensemble admissible a enregistré une décision positive valide. Un même acteur admissible peut satisfaire plusieurs lignes dans une seule action atomique.

Aucune décision partielle ne crée de RequestApprovalRevision, ne matérialise un besoin et ne modifie le plan.

### ApprovalScope

ApprovalScope est le périmètre métier stable qui porte l'autorité d'approbation. Il possède une identité et un code stables, un libellé d'affichage, un état actif/inactif et une version de concurrence.

ApprovalScope reste distinct de RequestLine.required_resource_class. La classe technique décrit le besoin du moteur; le périmètre d'approbation décrit qui possède l'autorité métier. Aucun des deux ne devient l'alias de l'autre.

Les premiers périmètres attendus sont « Installation électrique » et « Automatisation ». Les conventions de codes de tâches 110–119 et 210–219 servent uniquement de classement/préremplissage; aucun parsing de libellé ERP ne crée une autorité.

Depuis l'extension 276E, le chemin normal de résolution réutilise la **classe effective #454 déjà persistée sur `TaskCatalogEntry.resource_class_code`** pour résoudre un `ApprovalScope` via une relation administrable `ResourceClass → ApprovalScope`. La classe technique et le périmètre restent deux concepts distincts. L'association explicite `TaskCatalogEntry → ApprovalScope` est conservée comme override métier prioritaire pour les exceptions.

### Identité des approbateurs

Les approbateurs sont exclusivement des AppUser actifs possédant approve_demands.

BusinessContact reste l'identité de contact de #289 et ne confère aucune autorité. Un coordonnateur métier n'est donc pas automatiquement approbateur.

ADMIN n'est pas une autorité universelle implicite : un administrateur doit lui aussi être présent dans l'ensemble admissible de la ligne pour voter.

L'auto-approbation est autorisée lorsque le demandeur est réellement un AppUser admissible; #276 n'introduit pas de séparation des responsabilités supplémentaire.

### Résolution et future autorité ressource

Le contrat de résolution est :

    Eligible(line) = Approvers(ApprovalScope) ∪ Approvers(ProposedResource)

avec déduplication par AppUser.id et conservation de la provenance.

La persistance Resource ↔ AppUser approver est différée; 276A ne persiste que les approbateurs de périmètre.

La résolution du périmètre suit l'ordre suivant :

1. si une tâche possède exactement un mapping explicite actif `TaskCatalogEntry → ApprovalScope`, ce mapping est l'override autoritaire;
2. sinon, utiliser `TaskCatalogEntry.resource_class_code` — classe effective déjà produite par #454 après standard global et éventuel override projet — puis résoudre `ResourceClass → ApprovalScope`;
3. si aucun scope unique et actif ne peut être résolu, produire un diagnostic bloquant.

Plusieurs mappings explicites, plusieurs scopes pour une même classe, une tâche/classe/périmètre inactif, une classe absente ou l'absence d'un approbateur actif possédant `approve_demands` constituent des diagnostics bloquants explicites. Aucun ordre SQL, nom, courriel ou libellé libre ne décide silencieusement du résultat.

### Extension 276E — routage par classe effective #454

Le catalogue #454 résout déjà les standards `TaskCD → ResourceClass` et les overrides projet, puis persiste la classe effective dans `TaskCatalogEntry.resource_class_code`. #276 réutilise cette projection pour éviter une configuration d'approbation répétée tâche par tâche sur chaque projet.

Exemples :

```text
216 → PROGRAMMEUR                  → AUTOMATION              → AppUser X
217 → INSTALLATEUR_AUTOMATISATION → AUTOMATION              → AppUser X
117 → INSTALLATEUR_ELECTRIQUE     → ELECTRICAL_INSTALLATION → AppUser Y
```

Le mapping `ResourceClass → ApprovalScope` est administrable et appartient à #276. #454 reste autoritaire sur la résolution de la classe workforce; #276 reste autoritaire sur l'autorité d'approbation.

Une association explicite `TaskCatalogEntry → ApprovalScope` a priorité comme exception métier. Elle ne doit pas devenir nécessaire pour chaque occurrence d'un même TaskCD dans chaque projet.

Le résultat résolu — scope et AppUser admissibles — continue d'être figé dans le `RequestApprovalCycle` à la soumission. Modifier ensuite un standard #454, un override projet, un mapping classe→scope ou les approbateurs ne réécrit jamais un cycle déjà ouvert.

### Snapshot à la soumission

Lors du futur 276B, les approbateurs admissibles seront figés au moment de la soumission dans un RequestApprovalCycle enfant de WorkforceRequest, avec une exigence par ligne et les AppUser.id admissibles.

Une modification ultérieure des référentiels n'altérera pas rétroactivement les votes d'un cycle déjà commencé.

Le snapshot d'approbateurs n'est pas une RequestApprovalRevision #13. Il prouve qui peut voter sur le sujet soumis; la révision #13 reste la preuve immuable de l'autorisation métier et n'est créée ou activée qu'au quorum complet.

### Relation avec #13 et Planning

ADR-002 à ADR-004 restent inchangés. Le futur quorum complet déclenchera une unique finalisation vers le mécanisme existant de #13.

Une réapprobation laisse l'ancienne autorisation et le plan actif en place jusqu'à ce nouveau quorum. Le moteur Planning ne connaît pas les votes.

ADR-006 reste applicable uniquement lorsque la finalisation complète peut modifier le plan. Un vote partiel n'acquiert pas planning_version et ne déclenche aucun rebuild.

### Urgence

La priorité ou dérogation d'urgence ne contourne pas le quorum pour une nouvelle matérialisation. L'urgence peut accélérer le traitement, mais elle ne crée pas d'autorité d'approbation supplémentaire.

## Alternatives considered

### Une approbation globale choisie parmi les lignes

Rejetée. Elle permettrait à l'autorité d'une ligne d'approuver implicitement d'autres lignes qu'elle ne couvre pas.

### Tous les approbateurs d'une ligne doivent voter

Rejetée. Le produit exige un OU à l'intérieur d'un périmètre afin que plusieurs personnes puissent être substituables.

### Utiliser BusinessContact ou le coordonnateur #289

Rejetée. Le contact métier et l'identité d'autorisation ont des responsabilités différentes et ne doivent pas être fusionnés.

### Utiliser required_resource_class comme périmètre

Rejetée. La classe appartient au moteur technique et n'est pas une identité durable d'autorité métier.

L'extension 276E n'invalide pas cette décision : elle utilise la **classe effective du catalogue #454 comme entrée de routage vers un ApprovalScope distinct**, et non comme périmètre d'autorité en soi. Le champ libre/candidat `RequestLine.required_resource_class` ne décide pas directement de l'autorité.

### Recalculer dynamiquement les approbateurs après chaque vote

Rejetée. Une modification de configuration pourrait changer les règles d'un cycle déjà commencé et rendre l'historique ambigu.

## Consequences

### Positive

- les demandes multi-métiers peuvent avoir plusieurs autorités sans matérialisation partielle;
- les identités d'autorisation restent stables et séparées des contacts métier;
- les changements de configuration futurs n'altèrent pas rétroactivement un cycle soumis;
- la frontière avec #13 et le Planning reste claire;
- l'autorité ressource pourra être ajoutée sans remplacer le contrat de résolution.

### Trade-offs / negative

- #276 nécessite un cycle persistant et des décisions par ligne avant d'activer le nouveau workflow;
- l'administration doit maintenir les associations classe effective → périmètre et périmètre → approbateurs; les associations tâche → périmètre restent disponibles seulement comme overrides explicites;
- une configuration ambiguë ou incomplète bloque volontairement la constitution d'un cycle au lieu d'utiliser un fallback implicite.

## References

- GitHub Issue #276
- GitHub Issue #13
- GitHub Issue #288
- GitHub Issue #289
- GitHub Issue #327
- GitHub Issue #328
- GitHub Issue #454
- ADR-002
- ADR-003
- ADR-004
- ADR-005
- ADR-006

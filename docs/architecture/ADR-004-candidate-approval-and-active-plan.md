# ADR-004 — Séparer proposition candidate, autorisation approuvée et plan actif

Status: Accepted  
Date: 2026-09-22

## Context

Lorsqu'une demande approuvée est modifiée, RessourcePlanner doit conserver l'ancien plan opérationnel jusqu'à la nouvelle approbation.

En parallèle, certaines opérations sont légitimes sans réapprobation : confirmer une période déjà autorisée, sélectionner une autre alternative approuvée, modifier la répartition opérationnelle dans les fenêtres autorisées ou changer une cible automatique sans modifier l'autorisation métier.

Une unique représentation mutable ne suffit pas à distinguer :

- la proposition en cours d'édition;
- ce qui est officiellement autorisé;
- ce qui est effectivement actif dans le plan.

Les protections des quarts verrouillés doivent également être identiques quel que soit le chemin de synchronisation.

## Decision

RessourcePlanner maintient trois états distincts :

1. **demande candidate** — définition proposée et éditable;
2. **révision approuvée active** — preuve immuable de ce qui est autorisé;
3. **plan opérationnel actif** — besoins matérialisés, choix opérationnels et quarts réels.

Les sélections d'alternatives et confirmations opérationnelles sont contextualisées par l'autorisation active et ne doivent pas être confondues avec des choix candidats encore non approuvés.

Une resynchronisation dans l'enveloppe s'appuie sur la révision approuvée active et sur les choix opérationnels actifs. Elle ne relit pas silencieusement toute la demande candidate.

Une réapprobation active une nouvelle révision de manière transactionnelle :

1. validation des versions;
2. validation de la proposition;
3. validation des verrous et de la faisabilité;
4. création de la révision;
5. matérialisation;
6. recalcul;
7. bascule de la référence active;
8. audit.

En cas d'échec, aucune bascule partielle n'est conservée.

Les mutations de périodes, sélections et opérations sensibles utilisent des versions attendues et signalent les conflits explicitement.

Le moteur de planification reste ignorant du workflow d'approbation. Une couche de préparation commune transforme l'autorisation et les choix opérationnels en spécifications planifiables pour :

- approbation;
- sélection opérationnelle;
- réapprobation;
- preview / plan delta;
- projections de charge pertinentes.

Les quarts verrouillés sont protégés avant toute resynchronisation, y compris dans les parcours historiques.

## Operational rules

Par défaut :

- une confirmation seule à l'intérieur de l'enveloppe est une mutation opérationnelle auditée sans réapprobation;
- sélectionner une autre alternative déjà approuvée est opérationnel, sous réserve des verrous;
- modifier le plan actif pendant qu'une réapprobation est en attente reste permis uniquement contre l'ancienne révision approuvée active;
- une réduction opérationnelle temporaire peut conserver l'autorisation initiale;
- une réduction définitive du besoin modifie la proposition et suit le workflow de réapprobation;
- projet, site, type/classe, compétences, tâche/WorkPackage et effort sont structurants par défaut;
- les jours actifs restent une préférence de distribution;
- une ressource proposée reste une suggestion et non une contrainte d'autorisation;
- les ressources réellement affectées restent portées par `Shift.resource_id` conformément à ADR-001;
- un `PROJECT_MANAGER` dispose d'une tolérance déléguée de **+20 % inclusivement** sur l'effort/heures d'un besoin, calculée depuis la dernière révision approuvée locale et sans composition successive;
- au-delà de cette tolérance, une réapprobation `COORDINATOR`/`ADMIN` est requise;
- `ADMIN` et `COORDINATOR`, déjà habilités à approuver, peuvent élargir eux-mêmes l'autorisation sans second workflow; une nouvelle révision approuvée est alors créée et activée atomiquement.

## Alternatives considered

### Modifier immédiatement le plan puis corriger après réapprobation

Rejeté. Une proposition non autorisée ne doit jamais devenir temporairement le plan actif.

### Bloquer tout changement opérationnel pendant une réapprobation

Rejeté. Cela figerait inutilement l'exploitation alors que l'ancienne autorisation reste valide.

### Cloner l'intégralité du plan pour chaque proposition

Non retenu pour #13. Le besoin principal est de versionner l'autorisation et les choix opérationnels nécessaires, pas chaque sortie automatique du moteur.

### Utiliser la sélection candidate comme sélection active

Rejeté. Cela permettrait à une modification non approuvée de modifier le plan avant approbation.

## Consequences

### Positive

- l'exploitation peut continuer pendant une réapprobation;
- une modification candidate ne double pas la charge du plan actif;
- confirmation et sélection peuvent évoluer sans réapprobation lorsqu'elles restent autorisées;
- les previews et deltas peuvent distinguer besoin, affectations et autorisation;
- les protections de verrous deviennent cohérentes;
- #332/#333 disposent d'une frontière claire pour leurs intentions de planning.

### Trade-offs / negative

- les sélections actives et candidates doivent être distinguées;
- la concurrence doit être gérée explicitement;
- les previews doivent être revalidés lors de l'exécution;
- certaines routes historiques par demande devront converger vers les contrats par ligne.

## KEEP_EXCEPTION and delegated changes

`KEEP_EXCEPTION` est une décision opérationnelle explicite, distincte d'une augmentation durable de l'autorisation.

Règles retenues :

- pour un `PROJECT_MANAGER`, une hausse d'effort jusqu'à +20 % inclusivement de la dernière base approuvée peut être appliquée sans nouvelle approbation si toute la portée métier reste inchangée;
- la référence du seuil reste toujours la dernière révision approuvée, ce qui interdit les augmentations composées successives;
- au-delà de +20 %, la demande candidate doit être réapprouvée par un `COORDINATOR` ou `ADMIN` avant de remplacer l'autorisation active;
- un `ADMIN` ou `COORDINATOR` qui modifie lui-même l'autorisation n'est pas soumis à un aller-retour d'auto-approbation : la modification crée directement une nouvelle révision approuvée et auditée;
- une véritable `KEEP_EXCEPTION` conserve la révision approuvée et matérialise seulement un excédent opérationnel explicite; elle ne doit jamais masquer un changement de fenêtre, projet, site, type/classe, compétences, tâche/WorkPackage ou topologie d'alternatives.

Le plan actif peut continuer à évoluer contre l'ancienne révision approuvée pendant qu'une modification hors tolérance attend sa réapprobation.

## References

- GitHub Issue #13
- GitHub Issue #38
- GitHub Issue #55
- GitHub Issue #331
- GitHub Issue #332
- GitHub Issue #333
- ADR-001
- ADR-002
- ADR-003


---

## Amendment proposed for #781 — proposed resource remains advisory

Amendment status: Proposed  
Date: 2026-10-31  
WorkItem: #781

### Context

The #781 triage established that explicit versus default-derived hours do not select different approval or materialization paths. Both become the canonical total `RequestLine.estimated_hours`.

The first meaningful split is whether `RequestLine.proposed_resource_id` is populated. The current approved-materialization path can promote that proposal into `ResourceRequirement.assigned_resource_id`, which ADR-001 treats as the automatic-generation target. Rebuild may then create real `Shift` rows on that resource and consume the approved residual.

This behavior is internally consistent with the original ADR-004 wording that a proposed resource is not an authorization constraint, but it does not enforce the stricter product rule now selected for #781: approval authorizes the need; it does not select the proposed person as the operational target.

### Amendment decision

After this amendment is accepted:

1. **A proposed resource remains advisory after approval.**
   - Preserve `RequestLine.proposed_resource_id` and the immutable approved snapshot.
   - The proposal may remain visible as recommendation/candidate context.
   - Approval, rebuild, resync and compatible reapproval must not automatically promote that proposal into an operational target.

2. **A newly approved slot without an applicable existing operational decision starts unselected.**
   - Use the existing durable `RequestRequirementPlanningIntent`; do not introduce a second targeting authority.
   - An unselected target is represented by the existing intent model, conceptually `AUTO + EXPLICIT_NONE`, without manufacturing a Return Segment operation.

3. **Only an authorized Planning decision establishes the operational target.**
   - Dated and undated assignment commands that select a resource must persist the durable target choice.
   - Updating only the projected `ResourceRequirement.assigned_resource_id` is insufficient because later rebuild/resync may replace that projection.
   - Public segment-target mutations must be reviewed so every explicit selection survives rebuild, resync and compatible reapproval.

4. **Approval and operational targeting remain separate authorities.**
   - ADR-003 remains authoritative for immutable approved authorization.
   - ADR-001 remains authoritative for the distinction between automatic target and actual `Shift.resource_id`.
   - Creating or editing an individual manual Shift remains distinct from retargeting the automatic residual.

5. **File de Travail remains residual-based.**
   - Approval replaces the candidate representation with the approved residual projection.
   - For an unselected approved slot with `H > C`, the residual remains eligible for File de Travail under #693.
   - Retaining the advisory proposal must not preserve a duplicate candidate charge or double-count work.

6. **Compatible reapproval preserves operational decisions.**
   - Existing durable target, cutoff, releases and reductions survive compatible reapproval.
   - Changing the proposed resource alone neither overwrites nor clears an operational target.
   - New unselected slots remain unselected.
   - Incompatible rebinding continues to fail closed under the existing stable-slot/authorization rules.

7. **SAME_AS_PERIOD remains governed by ADR-029.**
   - Existing explicit component decisions remain applicable.
   - Proposed-resource inheritance alone is not an operational SAME_AS_PERIOD decision.

8. **Legacy approved plans are preserved during transition.**
   - Deployment must not automatically unassign resources, delete Shifts, return work to File de Travail or alter counted coverage.
   - Existing operational targets created under the former approval-implies-target behavior are preserved as legacy operational choices.
   - Where durable intent backfill is required, bind by stable approved-slot/current operational identity, not names or resource-equality heuristics.
   - Preserve explicit-none and queued states.
   - Ambiguous bindings require reconciliation and must fail closed.
   - Transition provenance must remain truthful: do not retroactively claim that a human explicitly selected a legacy target.

9. **Existing boundaries remain unchanged.**
   - ADR-034 coverage protections, releases, reductions and survivor rules remain authoritative.
   - ADR-035 window/cutoff semantics remain unchanged.
   - ADR-006 concurrency/CAS and ADR-027 authorization/visibility remain unchanged.

### Alternatives considered

#### Keep approval-implies-automatic-target behavior

Rejected for #781. It makes the proposal operational as a side effect of approval and can consume the residual before an explicit Planning decision.

#### Clear only the projected requirement target after approval

Rejected. `ResourceRequirement.assigned_resource_id` is a projection of operational intent in modern flows; clearing only the projection would allow rebuild/resync to reintroduce stale or ambiguous behavior.

#### Introduce a second proposal/target authority

Rejected. The existing `RequestRequirementPlanningIntent` already provides the durable operational authority needed for explicit target choices.

### Consequences

#### Positive

- approval no longer silently chooses the proposed person as the operational target;
- advisory recommendation and operational decision become unambiguous;
- approved residual work remains visible until covered or explicitly targeted;
- proposal changes cannot overwrite an existing operational choice;
- rebuild, resync and reapproval share one durable target authority.

#### Trade-offs / negative

- current materialization tests that encode approval-implies-target behavior must be updated;
- every explicit assignment path must persist durable intent consistently;
- legacy target preservation/backfill requires careful stable-slot reconciliation;
- some historical data may require explicit reconciliation when binding is ambiguous.

### Implementation notes

The dependent #781 implementation must, at minimum:

- remove proposal promotion from line, period and legacy approved-materialization/resync paths;
- ensure dated and undated explicit assignment commands persist `EXPLICIT_RESOURCE` (or equivalent current canonical target intent);
- preserve legacy coverage, Shift identity, locks, assets, releases and reductions;
- add regression coverage for all four hours/proposal combinations;
- prove proposal changes do not alter operational choices;
- cover rebuild/resync/compatible reapproval, SAME_AS_PERIOD, queued suppression and ambiguous legacy binding;
- keep the coordinator's separate “4 h + no proposed resource disappears” observation as a controlled reproduction task until a concrete backend boundary is demonstrated.

### References

- GitHub Issue #781
- GitHub Issue #55
- GitHub Issue #654
- GitHub Issue #693
- ADR-001
- ADR-003
- ADR-006
- ADR-027
- ADR-029
- ADR-034
- ADR-035

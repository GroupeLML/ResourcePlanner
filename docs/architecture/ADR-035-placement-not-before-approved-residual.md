# ADR-035 — Début opérationnel du placement du reliquat approuvé

Status: Accepted
Date: 2026-10-30
Accepted: 2026-10-30
WorkItem: ASTRA-777

Le Product Owner accepte la décision d'architecture issue du gate #777 sur `main@51f07b0191d68ee14e2d567cf2185e4aaf17fa8e`.

Cette ADR complète ADR-001, ADR-003, ADR-004, ADR-006, ADR-022, ADR-027, ADR-029 et ADR-034. **ADR-034 reste acceptée et inchangée. ADR-022 reste une autorité de widening uniquement.**

## Context

Le Planning permet déjà de persister une cible opérationnelle pour un `ResourceRequirement`, mais il n'existe pas d'autorité durable exprimant « ne pas placer ce reliquat avant cette date ».

Pour #777, la coordonnatrice veut déposer un besoin approuvé depuis la File de Travail sur une cellule ressource/jour. Ce geste doit représenter atomiquement :

- la ressource cible;
- une date de début opérationnelle de placement;
- sans modifier l'autorisation approuvée;
- sans invalider les Shift survivants antérieurs déjà comptés;
- sans permettre à React de reconstruire les règles de fenêtre, capacité ou besoin effectif.

Réutiliser `ResourceRequirement.start_date/end_date` comme cutoff serait incorrect : ces champs restent la projection de la fenêtre opérationnelle autorisée. Les réduire ferait apparaître des Shift survivants historiques comme hors fenêtre alors qu'ils restent légitimes.

L'architecture doit donc distinguer l'autorisation de fenêtre de l'intention de placement du reliquat.

## Decision

### 1. Autorité persistante

Étendre `RequestRequirementPlanningIntent` avec un champ SQL nullable :

```text
placement_not_before_date DATE NULL
```

Cette valeur appartient à l'identité stable du slot approuvé, au même niveau que sa cible opérationnelle.

Elle doit survivre :

- au remplacement physique du `ResourceRequirement`;
- aux rebuilds;
- aux redémarrages;
- à la synchronisation depuis l'approbation;
- aux réapprobations compatibles.

`NULL` signifie : aucune restriction de début supplémentaire.

Réutiliser :

- la version opérationnelle existante;
- l'audit existant;
- l'idempotence existante.

Ne pas créer :

- une nouvelle table de fenêtre parallèle;
- une seconde horloge de concurrence;
- une pseudo-exclusion pour représenter ce cutoff.

### 2. Fenêtre autorisée, intervalle de placement et besoin effectif

Conserver trois concepts distincts :

```text
W = fenêtre opérationnelle autorisée
  = fenêtre approuvée immuable + widening ADR-022 applicable

G = intervalle admissible pour nouvelle génération / placement
  = W ∩ [placement_not_before_date, +∞)

H = besoin courant effectif
  = base opérationnelle légitime − réductions durables actives
```

Si `placement_not_before_date IS NULL`, alors :

```text
G = W
```

Disponibilité, capacité, exclusions/libérations, qualification, actifs et SAME_AS_PERIOD peuvent encore réduire les possibilités à l'intérieur de `G`, mais ne peuvent jamais l'élargir.

`ResourceRequirement.start_date/end_date` restent la projection de `W`. L'intervalle `G` est transmis séparément aux entrées moteur, previews et recommandations.

### 3. Portée du cutoff

Le cutoff s'applique à **toute nouvelle mise en place** du slot, notamment :

- génération automatique;
- placement contrôlé du reliquat;
- génération overtime/fallback lorsqu'elle est autrement autorisée.

Aucun chemin ordinaire de placement ne peut le contourner silencieusement.

Les Shift déjà comptés avant le cutoff restent valides s'ils sont encore dans `W`.

Ils conservent :

- identité;
- ressource;
- date;
- heures;
- source;
- lock;
- confirmation;
- actifs.

Une édition descriptive seule reste autorisée.

Créer un nouveau placement avant le cutoff exige une décision explicite de date plus tôt ou un reset du cutoff. Ne pas créer de `PlanningWorkQueueRelease` ou d'exclusion de date pour représenter cette intention.

### 4. Modes de génération après drop daté

Le drop daté respecte les modes d'intention ADR-034.

| Situation initiale | Résultat |
|---|---|
| `AUTO` sans couverture comptée | reste `AUTO`; génération limitée par le cutoff |
| couverture comptée existante | préserver les survivants, placer uniquement le reliquat, terminer en `PRESERVE_EXISTING` |
| `QUEUED` ou `PRESERVE_EXISTING` | un placement contrôlé du reliquat, puis `PRESERVE_EXISTING` |

Le passage à `PRESERVE_EXISTING` ne convertit pas les survivants en manuel ou `locked=True`.

Retourner un Shift ou un segment selon ADR-034 conserve le cutoff.

Modifier ou effacer uniquement le cutoff ne doit pas, à lui seul, relancer la génération d'un slot `QUEUED` ou `PRESERVE_EXISTING`.

### 5. Réapprobation compatible

Lorsqu'un slot stable et son sujet d'autorisation restent compatibles, conserver exactement le cutoff.

Recalculer `W` selon la nouvelle approbation et ADR-022.

Règles :

- si le nouveau début autorisé est plus tard que le cutoff, conserver le cutoff; la génération commence au nouveau début autorisé;
- si la nouvelle fin autorisée est avant le cutoff et que `H > 0`, il n'existe plus d'intervalle admissible : refuser l'activation avec un conflit explicite de réconciliation;
- changement d'identité, topologie, sujet d'autorisation ou rebinding ambigu : fail closed; ne jamais copier le cutoff heuristiquement;
- les requirements terminaux ou à besoin effectif nul conservent l'intention dormante sans être rouverts;
- les protections `reduction_rebase_required` d'ADR-034 restent autoritaires lorsqu'une réduction est concernée.

### 6. Déplacer ou effacer le cutoff

Un nouveau drop daté **remplace** le cutoff courant par la nouvelle date choisie.

Il peut avancer ou reculer dans `W`.

Ne pas appliquer :

```text
max(old_date, new_date)
```

Le reset est une action explicite :

```text
placement_not_before_date = NULL
```

Il restaure l'éligibilité depuis `W.start`, sans modifier l'autorisation elle-même.

Le reset utilise les mêmes :

- permissions;
- versions;
- transaction;
- audit.

Pour compatibilité, une assignation ressource seulement qui omet la date **préserve** le cutoff. Omission ne signifie jamais reset.

### 7. Drop hors fenêtre

Le backend résout `W` avant toute mutation.

| Date déposée | Décision |
|---|---|
| dans `W` | placement autorisable sans élargir l'autorisation |
| avant `W.start` | rejet |
| après `W.end` | aucune mutation de placement; exposer le workflow explicite d'override/réapprobation existant |

Une date antérieure au début de l'approbation originale peut seulement être admissible si un widening ADR-022 déjà valide l'inclut dans `W`.

Le payload de drag ordinaire n'accorde aucun droit de widening.

Après extension/réapprobation séparée, exiger un nouveau drop avec des versions fraîches. Ne pas mémoriser et exécuter automatiquement un geste ancien après approbation différée, conformément à #333.

### 8. Commande atomique dédiée

Introduire une commande dédiée, conceptuellement :

```text
assign_segment_from_drop(
    segment_id,
    resource_id,
    dropped_date,
    expected_planning_version,
    expected_approval_revision_id,
    expected_operational_version,
    idempotency_key
)
```

Ordre attendu :

1. authentification et replay idempotent;
2. acquisition du Planning CAS avant les lectures décisionnelles;
3. validation de l'autorisation active;
4. validation de la version opérationnelle;
5. validation ADR-027 du segment et de la ressource;
6. résolution de `W`, `G`, `H`, couverture et reliquat;
7. validation des effets projetés;
8. persistance atomique de la cible + cutoff;
9. **un seul rebuild**;
10. validation de l'état final;
11. audit + reçu idempotent;
12. commit.

L'audit capture au minimum :

- ancienne/nouvelle cible;
- ancien/nouveau cutoff;
- ancien/nouveau mode;
- identités des Shift préservés et générés;
- `H/C/R` résultants.

Toute erreur rollback l'ensemble.

### 9. Reliquat, libérations ADR-034 et capacité partielle

Pour :

```text
H = 40
C = 32
R = 8
```

un drop sur Bob/jeudi :

- conserve les 32 h survivantes;
- génère au plus 8 h;
- ne génère rien avant jeudi;
- termine avec une projection fidèle du reliquat restant.

Une capacité insuffisante ne crée ni overtime implicite ni autorisation supplémentaire.

Exemple :

```text
H = 40
C = 0
capacité jeudi-vendredi = 16
=> couverture max = 16
=> reliquat = 24
```

Les libérations/exclusions ADR-034 restent actives.

Un drop n'est pas une restauration globale :

- résoudre une libération exige son identité canonique et la validation de la quantité encore restaurable;
- Bob couvrant le reliquat ne résout pas la libération d'Alice;
- les preuves de restauration d'un sibling ne sont pas consommées.

### 10. SAME_AS_PERIOD

ADR-029 reste autoritaire.

La ressource choisie doit être validée atomiquement pour toute composante SAME_AS_PERIOD affectée.

Les Shift réels déjà préservés restent contraignants.

Le cutoff est **local au slot sélectionné**. Sa date n'est pas copiée aux périodes sœurs.

Refuser une cible qui entre en conflit avec les affectations réelles préservées du composant.

### 11. Sécurité et projections backend

La commande exige :

- identité authentifiée active;
- `manage_planning`;
- visibilité/scope ADR-027;
- segment et ressource canoniquement visibles;
- autorisation approuvée active;
- Planning CAS;
- operational CAS.

Refuser les besoins :

- non approuvés;
- annulés;
- complétés;
- à reliquat nul.

Le preview backend expose au minimum :

- fenêtre autorisée `W`;
- cutoff;
- intervalle admissible `G`;
- besoin/couverture/reliquat;
- quantité plaçable;
- composante affectée;
- motifs de refus machine-readable.

React transmet :

- IDs canoniques;
- date de la cellule;
- versions requises.

React ne calcule ni :

- l'éligibilité de date;
- `H`;
- la capacité;
- le reliquat;
- l'effet SAME_AS_PERIOD.

### 12. Migration et rollback

La migration est additive.

Le nouveau champ est nullable, sans backfill inventé.

Les données existantes restent équivalentes à :

```text
placement_not_before_date = NULL
```

Le downgrade doit refuser de supprimer la colonne si des valeurs non nulles existent, afin de ne pas perdre silencieusement une intention de placement durable.

La validation SQL Server couvre les courses :

- deux drops concurrents;
- drop versus réapprobation;
- drop versus retour ADR-034;
- drop versus réduction durable ADR-034.

Les budgets de croissance des requêtes existants restent applicables.

## Alternatives considered

### A. Modifier `ResourceRequirement.start_date`

Rejeté : ce champ représente la fenêtre opérationnelle autorisée. Le rétrécir confond autorisation et intention de placement et peut invalider visuellement ou logiquement des Shift survivants antérieurs.

### B. Utiliser ADR-022 comme cutoff de placement

Rejeté : ADR-022 est une autorité de widening de fenêtre. Le réutiliser comme restriction de génération introduirait une double sémantique contradictoire.

### C. Persister une exclusion de date ou une libération ADR-034

Rejeté : une libération signifie qu'un travail réellement placé a été retourné. Le cutoff exprime une intention prospective « pas avant », pas un retrait historique.

### D. Conserver la date seulement dans React ou dans le geste de drag

Rejeté : rebuild, restart, resync et réapprobation perdraient l'intention et le backend ne resterait pas autoritaire.

### E. Créer un nouvel objet/table de fenêtre de placement

Rejeté : le slot stable ADR-034 possède déjà l'autorité opérationnelle appropriée et sa version CAS. Une nouvelle autorité créerait duplication et concurrence inutile.

## Consequences

### Positive

- le drop ressource/jour devient une décision durable et atomique;
- l'autorisation approuvée reste immuable;
- ADR-022 reste widening-only;
- les Shift historiques survivants restent valides;
- rebuild, restart, resync et réapprobation relisent la même intention;
- la File de Travail peut placer uniquement le reliquat sans double couverture;
- la concurrence réutilise les CAS existants;
- React reste non autoritaire.

### Trade-offs / negative

- `RequestRequirementPlanningIntent` gagne une nouvelle donnée durable;
- tous les chemins de génération/placement doivent respecter `G`;
- les réapprobations compatibles doivent réconcilier explicitement le cutoff;
- les cas `W.end < cutoff` deviennent des conflits de réconciliation visibles;
- migrations et downgrade doivent protéger les intents déjà peuplés;
- une couverture partielle après drop peut laisser un reliquat visible, ce qui est voulu.

## Implementation notes

Avant reprise DEV de #777 :

1. merger cette ADR en `main`;
2. ajouter la migration additive et la projection du cutoff sur le slot stable;
3. centraliser la résolution `W/G/H/C/R` dans les autorités backend existantes;
4. faire respecter `G` par génération automatique, placement contrôlé et fallback;
5. implémenter la commande atomique datée avec un seul rebuild;
6. préserver les comportements ADR-034 des survivants, libérations et réductions;
7. brancher le preview/read model puis React;
8. ajouter les preuves HTTP, Playwright et SQL Server.

Acceptation minimale :

- drop réel ressource/jour;
- aucun Shift nouveau avant cutoff;
- survivants antérieurs préservés;
- placement partiel avec reliquat fidèle;
- rebuild/restart/resync;
- réapprobation compatible et conflit sans intervalle;
- mouvement plus tôt/plus tard et reset explicite;
- outside-window et ADR-022;
- isolation des releases ADR-034;
- zéro/terminal;
- SAME_AS_PERIOD;
- replay/rollback;
- courses SQL Server;
- downgrade protégé;
- budgets de requêtes inchangés.

## References

- GitHub Issue #777
- GitHub Issue #55
- GitHub Issue #333
- GitHub Issue #693
- GitHub Issue #778
- ADR-001 — séparation besoin / affectation réelle
- ADR-003 — autorisation approuvée immuable
- ADR-004 — candidat / approuvé / plan actif
- ADR-006 — Planning CAS
- ADR-022 — operational planning window overrides
- ADR-027 — visibilité Planning
- ADR-029 — SAME_AS_PERIOD
- ADR-034 — retours File de Travail et réduction durable
- `RequestRequirementPlanningIntent`
- `app/infrastructure/sql/period_approved_sync.py`
- `app/infrastructure/sql/command_adapters.py`
- `app/domain/planning_engine.py`
- `frontend/src/PlanningPage.tsx`
- `frontend/src/planningDragDrop.ts`

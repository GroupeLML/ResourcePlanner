# ADR-027 — Visibilité Planning par rôle et périmètre autorisé

Status: Accepted  
Date: 2026-10-06

## Context

Le contrat historique de visibilité du Planning issu de #277 permet encore à certains utilisateurs possédant la permission de lecture d'accéder à une vue globale, alors que #618 exige désormais des périmètres plus stricts pour les chargés de projet et les techniciens, ainsi qu'un `scope=mine` coordonnateur fondé sur des relations métier explicites.

Le besoin est une règle de sécurité et d'autorisation, pas un simple filtre d'interface. Les routes Planning exposent plusieurs projections — quarts, ressources, demandes, segments, capacité, recommandations, actifs, diagnostics et détails accessibles par identifiant — de sorte qu'un filtrage uniquement React ou uniquement sur les `Shift` laisserait des voies de contournement.

Les identités et relations nécessaires existent déjà :
- principal ERP et co-chargés RP via #573 / ADR-017;
- résolution coordonnateur et contexte matérialisé via #410/#289;
- cycles et approbateurs figés via #276 / ADR-010;
- séparation demande candidate / autorisation approuvée / plan actif via ADR-004;
- responsabilité opérationnelle via ADR-020;
- séparation occupation réelle / détails exposés pour les actifs via ADR-026.

## Decision

### 1. Le backend est l'autorité de visibilité Planning

Toute lecture Planning applique l'intersection suivante :

```text
permission RBAC de lecture
∩ périmètre Planning autorisé
∩ filtres demandés
```

Un filtre client peut réduire un résultat autorisé, jamais l'élargir.

Le backend doit résoudre une politique Planning dédiée avant toute projection ou agrégation sensible. Une simple liste de `project_ids` n'est pas suffisante : la politique doit pouvoir représenter des ressources coordonnées, des demandes visibles par provenance, des couples projet/jour technicien et des capacités de détail/navigation.

Aucun paramètre fourni par le client, notamment un indicateur du type `source=planning`, ne constitue une preuve d'autorisation.

### 2. Politique par rôle et utilisateurs multirôles

- `ADMIN` conserve la vue globale autorisée.
- `COORDINATOR` conserve la vue globale autorisée par RBAC et dispose d'un `scope=mine` défini ci-dessous.
- `MANAGER` conserve son accès actuel; #618 ne redéfinit pas implicitement ce rôle.
- `PROJECT_MANAGER` seul n'a pas de vue globale et voit uniquement ses projets effectivement gérés selon le principal ERP et les co-chargés RP de #573.
- `TECHNICIAN` seul n'a pas de vue globale et voit son propre Planning plus les voisins autorisés par couple projet/jour.
- `DELIVERY_CONTRIBUTOR` seul n'obtient aucun accès global implicite; un contexte personnel doit être explicitement justifié, sinon le résultat est vide.

Pour un utilisateur multirôle, les droits explicitement accordés sont combinés par union. Une restriction propre au rôle technicien ne retire donc pas un droit global explicitement accordé par un autre rôle transverse.

### 3. Contrat HTTP fail-closed

- `scope` absent : le serveur calcule le défaut autorisé pour l'utilisateur.
- `scope=global` demandé sans autorisation : réponse `403`, sans conversion silencieuse vers un autre scope.
- périmètre personnel vide : résultat vide avec diagnostic approprié, jamais fallback global.
- identité métier absente ou ambiguë : aucune résolution par nom ou courriel.
- dépendance de résolution indisponible : échec fermé; une absence de filtre résolu ne doit jamais signifier « global ».

### 4. Périmètre technicien par couple exact projet/jour

Le lien d'identité canonique reste :

```text
AppUser.employee_external_id → Resource.external_id
```

Pour une fenêtre `W`, définir :

```text
K(u,W) = {(project_id, work_date) |
          un vrai Shift de la Resource liée à u existe dans W}
```

Le technicien peut voir :
- ses propres `Shift` dans `W`;
- les `Shift` d'autres ressources uniquement lorsque leur couple exact `(project_id, work_date)` appartient à `K(u,W)`.

Il est interdit de construire séparément une liste de projets et une liste de jours puis d'en prendre le produit cartésien.

Les vrais quarts tentatifs ou confirmés, automatiques ou manuels, peuvent établir le couple autorisé. Les lignes synthétiques de manque, les demandes simplement proposées et une cible `ResourceRequirement.assigned_resource_id` sans vrai `Shift` ne l'établissent pas.

La visibilité est recalculée depuis le plan courant après déplacement, suppression ou réaffectation.

Un quart voisin n'autorise qu'une projection minimale utile : identité d'affichage, projet, jour, heures du quart et confirmation pertinente. Il ne donne pas accès automatiquement à toute la demande, à la semaine complète du collègue, à ses coordonnées, notes internes, capacité globale, historique ou autres périodes.

### 5. `COORDINATOR + scope=mine` est une union de relations explicites

Le périmètre coordonnateur est l'union backend de sources distinctes :

1. ressources actives directement coordonnées, selon `AppUser.business_contact_id = Resource.coordinator_contact_id`, même lorsqu'elles n'ont aucun quart dans la fenêtre;
2. demandes dont l'utilisateur est coordonnateur effectif selon #410/#289;
3. demandes pour lesquelles son `AppUser.id` est un approbateur figé dans le cycle d'approbation ouvert courant de #276;
4. autres relations personnelles déjà autorisées, notamment #573 et la ressource personnelle lorsqu'elles sont réellement applicables.

Une ressource directement coordonnée peut exposer son Planning dans la fenêtre, y compris sur un projet que le coordonnateur ne gère pas.

À l'inverse, une ressource seulement présente parce qu'elle participe à une demande visible n'élargit pas le scope à tous ses autres quarts ni à toutes les demandes du projet. Les relations visibles ne se propagent pas transitivement.

La résolution #410 du contexte matérialisé reste autoritaire et ne doit pas être remplacée par une relecture d'un candidat non approuvé. ADR-020 ne transforme pas la hiérarchie du responsable opérationnel en hiérarchie de coordination.

### 6. Visibilité d'approbation distincte du droit de voter

La source canonique d'une visibilité fondée sur l'approbation est :

```text
WorkforceRequest
→ RequestApprovalCycle OPEN
→ ApprovalRequirement
→ ApprovalRequirementApprover
```

L'utilisateur doit être actif, posséder `approve_demands`, la demande doit être soumise, le cycle ouvert doit être le cycle courant et l'utilisateur doit être présent parmi ses approbateurs figés.

La demande reste visible par ce motif pendant tout le cycle ouvert, même lorsque l'exigence propre à cet utilisateur a déjà été satisfaite. Le droit d'exécuter l'action d'approbation reste, lui, limité aux exigences encore à satisfaire.

Le fallback historique de progression d'approbation ne doit pas être utilisé pour autoriser une lecture. À la clôture ou à l'invalidation du cycle, le motif de visibilité par approbation disparaît; une autre relation indépendante peut toutefois maintenir la visibilité.

### 7. Calcul complet, publication minimale

Les calculs de disponibilité, d'occupation et de conflits peuvent continuer à utiliser le Planning complet. Un quart ou une réservation masqué ne devient jamais artificiellement disponible.

Cette connaissance interne ne permet pas de publier tous les détails :
- un technicien ne reçoit pas la capacité hebdomadaire complète des collègues, leurs motifs d'absence ou des agrégats globaux;
- un chargé de projet peut recevoir une disponibilité utile neutralisée sans révéler les engagements détaillés hors de son périmètre;
- coordonnateur et administration conservent les informations globales effectivement autorisées;
- la visibilité d'un actif ou d'un badge associé à un quart ne donne pas automatiquement accès à la réservation complète.

ADR-026 reste applicable pour séparer occupation réelle et détails exposés.

### 8. Service applicatif et surfaces protégées

L'implémentation doit centraliser cette politique dans un service applicatif dédié, conceptuellement `PlanningVisibilityService`, produisant une résolution typée comprenant au minimum :
- scopes autorisés;
- projets gérés;
- ressource personnelle;
- ressources directement coordonnées;
- demandes visibles avec provenance;
- couples projet/jour autorisés pour le technicien;
- capacités de détail et de navigation.

Cette résolution est consommée par les read models SQL et doit être réutilisée par #654. Elle ne devient pas une table persistante d'ACL dérivées ni une nouvelle autorité métier.

La politique s'applique aux surfaces Planning pertinentes, notamment :
- snapshot, liste des quarts, actions et capacité;
- segments, détails et historiques;
- demandes, périodes et plan-delta;
- recommandations;
- actifs et réservations associés;
- comptages, diagnostics et filtres;
- futures cartes candidates de #654.

Les routes partagées avec d'autres modules doivent empêcher qu'un accès par identifiant restitue des détails Planning interdits.

### 9. Performance, cache et identité frontend

La première livraison résout l'autorisation par requête sans cache partagé d'autorisation. `planning_version` ne suffit pas comme clé d'invalidation puisque co-chargés et cycles d'approbation peuvent changer sans mutation Planning.

Les lectures SQL devraient privilégier des prédicats `EXISTS`, des chargements groupés et des requêtes évitant de multiplier les quarts ou les heures. Les ressources coordonnées sont résolues indépendamment de l'existence de quarts.

Aucune migration métier n'est requise a priori; d'éventuels index sont ajoutés seulement après mesure pertinente, notamment sous SQL Server.

React consomme la politique projetée par le serveur. Les clés de chargement sensibles incluent l'identité; un changement d'utilisateur vide les données et dialogues sensibles et les réponses réseau tardives de l'identité précédente sont ignorées. Les catalogues d'édition ne sont chargés que si la capacité correspondante est accordée.

## Alternatives considered

### Filtrer uniquement dans React

Rejeté. Le client ne constitue pas une frontière de sécurité et les routes de détail ou les réponses JSON complètes resteraient contournables.

### Réutiliser uniquement `project_ids`

Rejeté. Ce modèle ne représente ni les couples projet/jour du technicien, ni les ressources coordonnées sans quart, ni les demandes visibles par coordination ou approbation sans élargir excessivement le scope.

### Faire dominer le rôle le plus restrictif chez les utilisateurs multirôles

Rejeté. Cela retirerait silencieusement des droits explicitement accordés par un rôle transverse. L'union des droits explicites est retenue.

### Utiliser les demandes encore « approvables » comme source unique de visibilité

Rejeté. Cette liste exclut les exigences déjà satisfaites et confondrait « assigné au cycle courant » avec « peut encore voter ».

### Persister des ACL Planning dérivées

Rejeté. Les relations canoniques existent déjà et évoluent indépendamment. Une ACL dérivée créerait une seconde autorité et un problème de synchronisation.

## Consequences

### Positive

- la sécurité Planning devient backend-authoritative;
- les vues chargés de projet, techniciens et coordonnateurs ont des périmètres explicites et testables;
- les exceptions de visibilité ne se propagent pas transitivement;
- #654 peut réutiliser une projection commune au lieu de reconstruire `Mon périmètre`;
- les calculs métier complets restent corrects sans exposer les données sous-jacentes.

### Trade-offs / negative

- les read models et routes de détail doivent appliquer une politique plus riche qu'un simple filtre projet;
- certaines réponses existantes devront réduire leur projection selon le contexte;
- les clés de cache/frontend doivent tenir compte de l'identité;
- certains tests historiques de vue globale, catalogue global ou capacité globale devront être adaptés parce que le contrat de sécurité change explicitement.

## Implementation notes

Le découpage de #618 reste :
1. **618A** — politique backend et protection des lectures;
2. **618B** — React/navigation et hygiène identité/cache;
3. **618C** — acceptation sécurité transverse.

618C doit notamment prouver le refus du global non autorisé, l'absence de fuite quand `scope` est omis, la correspondance exacte projet/jour, l'absence de propagation transitive, les scopes distincts de deux coordonnateurs, la visibilité d'une ressource coordonnée sans quart, le cycle d'approbation ouvert/clos/réapprouvé, l'absence d'accès indirect par identifiant et l'absence de fuite dans le JSON complet.

## References

- GitHub Issue #618
- GitHub Issue #277
- GitHub Issue #410
- GitHub Issue #573
- GitHub Issue #276
- GitHub Issue #654
- ADR-004 — Candidate approval and active plan
- ADR-010 — Line approval scopes and quorum
- ADR-011 — SQL Server authoritative database
- ADR-017 — ERP project manager and RP co-managers
- ADR-020 — Operational responsibility hierarchy and approved context
- ADR-026 — Asset assignment authority and computed shift propagation

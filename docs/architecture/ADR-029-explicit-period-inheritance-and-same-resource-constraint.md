# ADR-029 — Héritage explicite des périodes et contrainte de même ressource réelle

Status: Accepted
Date: 2026-10-07

## Context

L'issue #655 fait évoluer les périodes d'une `RequestLine` sur trois axes liés :

- le nombre de ressources d'une période ne doit plus constituer une autorité éditable distincte du besoin maître;
- la confirmation et la ressource proposée doivent pouvoir hériter explicitement du maître ou être surchargées;
- une période cumulative doit pouvoir imposer qu'une autre période utilise la même ressource réelle, et non seulement recopier une proposition au moment de l'édition.

Le modèle existant distingue déjà l'identité logique d'une période de sa version physique SQL, sépare la cible automatique d'un `ResourceRequirement` de la ressource réelle des `Shift`, et sépare demande candidate, autorisation approuvée immuable et plan actif.

Une simple copie de `proposed_resource_id` ne suffit donc pas : elle perdrait l'intention d'héritage, ne survivrait pas correctement aux remplacements de versions de périodes et ne garantirait pas l'identité de la ressource réellement affectée.

Cette décision doit rester compatible avec ADR-001 à ADR-004, ADR-006, ADR-010, ADR-011, ADR-022 et ADR-025.

## Decision

### 1. La ligne propriétaire reste l'autorité maître

Pour les périodes modernes, la `RequestLine` propriétaire est l'autorité du besoin planifiable conformément à ADR-002.

Pour les demandes historiques qui n'utilisent pas le modèle multi-lignes moderne, un adaptateur explicite peut continuer à lire les champs historiques de `WorkforceRequest`. Il ne doit pas créer une chaîne de fallbacks implicites entre demande, ligne et période.

Le nombre de ressources effectif d'une période est dérivé du maître applicable. Les nouvelles écritures ne persistent aucune surcharge de quantité au niveau période.

Les anciennes données restent lisibles selon leur contrat historique; elles ne sont pas réinterprétées automatiquement comme de l'héritage.

### 2. Les modes d'héritage sont persistés explicitement

La confirmation d'une période utilise l'un des modes suivants :

- `INHERIT_MASTER`;
- `EXPLICIT`.

La ressource proposée d'une période utilise l'un des modes suivants :

- `INHERIT_MASTER`;
- `EXPLICIT`;
- `SAME_AS_PERIOD`.

En mode `EXPLICIT`, une ressource explicite nulle signifie explicitement « aucune ressource proposée ». `NULL` ne doit jamais signifier à la fois « aucune valeur » et « hériter ».

Une référence `SAME_AS_PERIOD` utilise un `period_id` logique stable, correspondant au `period_key` dans la même ligne. Elle ne référence jamais la clé primaire SQL d'une version physique remplaçable.

Les combinaisons incohérentes de mode, valeur explicite et référence sont refusées par le backend.

### 3. Un résolveur commun produit valeur effective et provenance

Le backend possède une résolution commune des valeurs effectives de période. Elle expose au minimum :

- la valeur effective;
- son mode;
- sa provenance maître ou explicite;
- la période cible et la racine lorsqu'une contrainte `SAME_AS_PERIOD` existe.

Cette résolution alimente les projections, les snapshots d'approbation, le routage et la préparation du plan.

React affiche les valeurs et provenances retournées par le backend et ne recalcule pas l'autorité.

L'héritage reste dynamique dans son contexte :

- le candidat hérite du maître candidat;
- le plan actif hérite du contexte approuvé/actif;
- une modification candidate du maître ne traverse jamais silencieusement une réapprobation en attente.

Les snapshots approuvés conservent les modes, références et valeurs résolues nécessaires pour expliquer l'autorisation accordée.

### 4. Première version de SAME_AS_PERIOD

La première version de `SAME_AS_PERIOD` est volontairement limitée aux périodes :

- de type humain `WORKFORCE`;
- appartenant à la même demande et à la même `RequestLine`;
- cumulatives;
- dont la quantité effective vaut exactement 1.

Les chaînes de références sont permises, mais les cycles directs ou transitifs sont interdits.

Une composante liée, par exemple `B → A` et `C → B`, forme un seul groupe de contrainte. La direction décrit la dépendance; l'invariant porte sur toute la composante.

Les alternatives, les références inter-lignes et les quantités supérieures à 1 sont hors du premier contrat. Elles nécessitent une décision métier supplémentaire avant extension.

### 5. La contrainte porte sur les Shift réels

L'invariant de `SAME_AS_PERIOD` est :

> Tous les quarts réels actifs des périodes d'une même composante utilisent le même `Resource.id`.

Cette contrainte ne remplace pas ADR-001 pour les autres besoins. Elle constitue une restriction locale et explicite du comportement multi-ressources normalement permis.

`ResourceRequirement.assigned_resource_id` reste une cible automatique; il ne suffit jamais à prouver que l'invariant est respecté.

Une indisponibilité ne provoque aucun repli automatique vers plusieurs personnes.

### 6. Préparation collective, disponibilité et mutations atomiques

Les périodes liées sont préparées collectivement avant génération des quarts, indépendamment de leur ordre physique de création.

La préparation :

1. résout les composantes liées;
2. tient compte des quarts manuels ou verrouillés existants;
3. recherche les ressources admissibles sur l'ensemble des fenêtres;
4. évalue leur capacité commune avec les règles Planning applicables;
5. fournit une affectation cohérente au moteur;
6. revalide l'invariant avant commit.

Deux tests indépendants « disponible sur A » et « disponible sur B » ne suffisent pas. Lorsque les fenêtres se chevauchent, les charges doivent être évaluées ensemble dans le même calendrier de capacité. Les jours entre deux périodes distinctes ne sont pas réservés.

Une pénurie de capacité peut laisser le groupe non affecté ou avec reliquat et diagnostic. Elle ne doit pas faire échouer globalement le Planning.

À l'inverse, une mutation qui laisserait des périodes liées sur des ressources différentes est invalide et doit être refusée.

Tout changement de ressource visant une composante `SAME_AS_PERIOD` est atomique à l'échelle du groupe. Les gardes s'appliquent aux chemins de création, édition, déplacement, split, duplication, changement de cible, release, rebuild et réapprobation.

Les mutations réutilisent `planning_version` et le contrat CAS/idempotence d'ADR-006; aucune version de concurrence parallèle n'est introduite.

### 7. Les verrous existants contraignent le groupe

Un quart verrouillé existant fixe la ressource candidate de toute la composante.

Si plusieurs quarts verrouillés de la même composante utilisent déjà des ressources différentes, l'activation ou la mutation incompatible est refusée. Aucune correction silencieuse d'un verrou n'est permise.

Si la période racine n'est pas encore matérialisée, le groupe est préparé ensemble et ne dépend pas d'un ordre SQL.

Si aucune affectation réelle n'existe encore, la contrainte peut rester non résolue; aucune ressource n'est inventée depuis un libellé ou un ancien quart.

### 8. SAME_AS_PERIOD fait partie de l'autorisation

La topologie `SAME_AS_PERIOD` fait partie du fingerprint d'autorisation. Ajouter, retirer ou changer un lien est une modification structurante qui exige une nouvelle approbation avant activation.

Le fingerprint du sujet soumis conserve aussi les modes et références pertinents ainsi que les ressources proposées résolues utilisées pour le routage.

Deux états qui affichent aujourd'hui la même valeur, par exemple `INHERIT_MASTER(R1)` et `EXPLICIT(R1)`, ne sont pas équivalents : leur comportement futur diffère et leur mode doit rester observable.

Une confirmation modifiée dans l'enveloppe autorisée peut rester une mutation opérationnelle auditée selon les règles existantes. Une ressource proposée simple demeure une suggestion et ne devient pas une affectation réelle obligatoire.

### 9. Le quorum reste au niveau ligne

Le contrat de quorum d'ADR-010 reste inchangé : ET entre lignes, OU entre approbateurs admissibles d'une ligne.

Le routage d'une ligne résout les propositions effectives de ses périodes autorisées, y compris les alternatives selon leur contrat courant, tout en conservant la priorité des overrides de tâche et des règles de classe.

Une ligne doit aboutir à un périmètre d'approbation unique et cohérent. Si les propositions de périodes conduisent à des périmètres incompatibles, le backend refuse explicitement le cycle au lieu d'inventer un quorum par période.

### 10. Migration additive et historique préservé

La migration est additive :

- les anciennes versions de périodes et révisions approuvées restent inchangées;
- les anciens snapshots restent lisibles selon leur format;
- les nouvelles colonnes de mode/référence et la version de contrat sont ajoutées;
- les nouvelles écritures utilisent le nouveau contrat.

Une ancienne période dont la quantité historique diffère du contrat moderne n'est jamais convertie silencieusement vers une quantité de 1 ou vers l'héritage.

Une normalisation historique incompatible exige un choix explicite, avec aperçu des changements et workflow d'autorisation applicable.

Les colonnes historiques peuvent rester nécessaires à la relecture du passé, mais ne sont plus une autorité éditable pour les nouvelles périodes.

### 11. API et projections rendent l'autorité explicite

Les réponses de lecture exposent séparément :

- modes;
- valeurs explicites;
- valeurs effectives;
- provenance;
- cible et racine `SAME_AS_PERIOD`;
- état de la contrainte;
- ressources réellement mobilisées;
- diagnostics;
- versions attendues.

L'interface retire « Ressources simultanées » des cartes de périodes, distingue « ressource suggérée » de « même personne obligatoire » et utilise des IDs stables pour les sélecteurs et références.

### 12. Séquencement de livraison

Le découpage #655 reste en trois tranches :

1. **655A** — modes et résolveur commun, IDs, quantité dérivée, compatibilité historique, snapshots versionnés, comparaison d'enveloppe et routage;
2. **655B** — graphe `SAME_AS_PERIOD`, préparation collective, affectation commune, contrôles des mutations, rebuild, diagnostics, CAS et idempotence;
3. **655C** — React, projections explicites, acceptation transverse et preuves SQL Server.

655A peut persister et projeter les contrats nécessaires à la suite, mais ne doit pas permettre d'activer effectivement `SAME_AS_PERIOD` avant que 655B sache faire respecter l'invariant sur les quarts réels.

## Alternatives considered

### Copier proposed_resource_id d'une période source

Rejeté. Une copie ponctuelle perd l'intention d'héritage et ne garantit pas la ressource réellement utilisée après rebuild ou mutation manuelle.

### Référencer la version SQL physique d'une période

Rejeté. Les éditions remplacent les versions physiques; une relation durable doit viser l'identité logique de période dans sa ligne.

### Appliquer SAME_AS_PERIOD à n'importe quelle quantité

Rejeté dans la première version. Pour plusieurs ressources, « même ressource » est ambigu sans contrat supplémentaire sur l'équipe, l'ensemble ou l'appariement entre slots.

### Introduire un quorum d'approbation par période

Rejeté. Cela étendrait substantiellement le modèle de #276 et n'est pas requis pour satisfaire #655.

### Migrer automatiquement les anciennes valeurs égales au maître vers INHERIT_MASTER

Rejeté. L'égalité actuelle ne prouve pas l'intention historique d'hériter dynamiquement.

## Consequences

### Positive

- l'autorité maître et les surcharges de période deviennent explicites et auditables;
- les changements du maître suivent correctement les périodes héritées sans copier de valeurs périmées;
- la contrainte de même ressource survit aux rebuilds et aux mutations parce qu'elle porte sur les `Shift` réels;
- les références restent stables malgré le versionnement physique des périodes;
- approbation, routage, Planning et projections consomment la même résolution;
- l'historique reste lisible sans réinterprétation silencieuse.

### Trade-offs / negative

- la préparation Planning doit désormais résoudre des composantes de périodes et évaluer une capacité commune multi-fenêtres;
- les mutations manuelles doivent connaître et valider la topologie `SAME_AS_PERIOD`;
- certaines données historiques exigent une normalisation explicite avant adoption du nouveau contrat;
- les périodes multi-ressources et les alternatives liées restent volontairement hors scope;
- un routage incohérent entre périodes d'une ligne devient une erreur explicite au lieu d'être aplati automatiquement.

## Implementation notes

Les validations de #655 doivent couvrir notamment :

- héritage dynamique, override et retour à l'héritage;
- isolation entre candidat modifié et plan actif approuvé;
- conservation de l'identité après remplacement d'une version SQL;
- cycles, cible supprimée et référence inter-lignes;
- racine non matérialisée et verrous contradictoires;
- capacité commune sur fenêtres chevauchantes;
- contournements par DnD, split, duplication ou release;
- changement de mode à valeur effective identique;
- migration des quantités et snapshots historiques;
- concurrence SQL Server, replay idempotent et rollback transactionnel.

Les migrations et scénarios de concurrence doivent être validés sur SQL Server conformément à ADR-011.

## References

- GitHub Issue #655
- GitHub Roadmap #55
- ADR-001 — Separate requirement target from shift assignment
- ADR-002 — Request-line periods and identities
- ADR-003 — Immutable approved authorization
- ADR-004 — Candidate approval and active plan
- ADR-006 — Global planning mutation version
- ADR-010 — Line approval scopes and quorum
- ADR-011 — SQL Server authoritative database
- ADR-022 — Operational planning window overrides
- ADR-025 — Project-task preferred resource and deterministic recommendation ranking
- #276 — multi-approbation par ligne
- #288 — demandes multi-lignes
- #331 — besoin/budget vs affectation réelle
- #333 — mutations Planning contrôlées

# ADR-016 — Affectation d’actifs ad hoc appartenant à un quart

Status: Accepted  
Date: 2026-10-01

## Context

ADR-007 et #291 ont introduit les ressources réservables non humaines sans les représenter comme de faux employés. Le modèle durable sépare le besoin d’actif de sa réservation physique :

```text
RequestLine[ASSET] approuvée
    ↓
AssetRequirement
    ↓
AssetAllocation
    ↓
Asset
```

`AssetRequirement` représente le besoin opérationnel matérialisé et `AssetAllocation` la réservation de l’unité physique. L’occupation initiale est exclusive à la journée. #292 ajoute la qualification par opérateur humain, sans fusionner les modèles humain et actif.

#538 a ajouté un raccourci UX permettant d’affecter un actif depuis un quart lorsqu’un `AssetRequirement` existe déjà. #560 doit maintenant permettre le même geste lorsqu’aucun besoin ASSET n’existe dans la demande d’origine, tout en affichant l’affectation canonique sur la carte et la popup du `Shift`.

Cette extension ne doit pas créer une seconde autorité de réservation, détourner les approbations des demandes, ni affaiblir les contrats de concurrence, d’idempotence, d’audit, de qualification, de disponibilité et de rollback déjà stabilisés par ADR-006, ADR-007, ADR-009, #292, #332, #333 et #399.

## Problem

Le système doit pouvoir répondre sans ambiguïté aux deux situations suivantes :

1. un besoin d’actif provient d’une ligne ASSET approuvée d’une demande;
2. un coordonnateur décide directement depuis un `Shift` qu’un actif doit lui être attaché, alors qu’aucune ligne ASSET approuvée n’existe.

Rendre `AssetAllocation.asset_requirement_id` optionnel ou lier directement une allocation au `Shift` introduirait deux formes concurrentes de réservation physique. Fabriquer une ligne ASSET ou une approbation fictive introduirait au contraire une fausse autorisation métier.

L’association implicite par demande, opérateur et date n’est pas non plus une identité stable : plusieurs quarts peuvent partager un opérateur, une date ou un contexte de demande, et les opérations de move, split, duplicate ou suppression rendent cette heuristique insuffisante.

## Decision

### 1. `AssetRequirement` reste obligatoire

Toute `AssetAllocation` reste liée à un `AssetRequirement`.

Le contrat suivant demeure obligatoire :

```text
AssetAllocation.asset_requirement_id → AssetRequirement.id
```

`AssetAllocation.asset_requirement_id` ne devient pas nullable.

Aucune seconde forme de réservation directe :

```text
AssetAllocation → Shift
```

n’est introduite en parallèle.

### 2. `AssetRequirement` distingue son origine

`AssetRequirement` évolue pour distinguer au minimum :

```text
REQUEST
SHIFT_AD_HOC
```

La relation conceptuelle devient :

```text
RequestLine ASSET approuvée ─┐
                             ├─> AssetRequirement ─> AssetAllocation ─> Asset
Shift ── SHIFT_AD_HOC ───────┘
```

Les deux formes sont mutuellement exclusives selon leur origine.

#### Origine `REQUEST`

Le comportement historique reste inchangé.

Le propriétaire métier est la ligne ASSET approuvée. Les références de demande, ligne, révision et entrée approuvée déjà nécessaires à la provenance et à l’autorisation sont conservées.

Direction de schéma :

```ini
origin = REQUEST
shift_id = NULL
workforce_request_id = NOT NULL
source_request_line_id = NOT NULL
approved_entry_key = NOT NULL
```

Les autres références historiques, notamment `approved_revision_id`, suivent leurs contrats existants.

#### Origine `SHIFT_AD_HOC`

Le propriétaire métier est le véritable `Shift`.

`AssetRequirement.shift_id` est une clé étrangère explicite vers `Shift.id`.

Aucune fausse ligne ASSET, entrée approuvée ou approbation n’est créée.

Direction de schéma :

```ini
origin = SHIFT_AD_HOC
shift_id = NOT NULL
workforce_request_id = NULL
source_request_line_id = NULL
approved_entry_key = NULL
approved_revision_id = NULL
```

Pour ce requirement :

- la fenêtre d’occupation est la journée du quart;
- l’opérateur qualifiant est `Shift.resource_id`;
- la portée minimale autorise une seule affectation ad hoc par quart.

Les détails exacts de migration, contraintes SQL et index appartiennent à 560A. L’ADR stabilise cependant l’exclusivité des deux formes et l’identité métier du propriétaire.

### 3. `AssetAllocation` reste l’autorité de réservation physique

Toutes les réservations concrètes d’actifs continuent d’utiliser une seule table d’allocations physiques.

Les contrats existants sont conservés :

- disponibilité globale de l’actif;
- protection contre le double booking;
- qualification #292;
- unicité d’allocation par requirement;
- moteur d’occupation #291;
- occupation exclusive à la journée dans le périmètre actuel.

Aucune réservation ad hoc n’est stockée dans une nouvelle table ni directement dans le `Shift`.

### 4. La première affectation ad hoc protège le `Shift`

La première assignation d’un actif ad hoc transforme le quart propriétaire en décision Planning persistante protégée.

Lorsqu’un quart automatique est admissible à cette conversion :

```text
AUTO
→ MANUAL
→ locked = True
```

La conversion conserve :

- le même `Shift.id`;
- la même ressource;
- la même date;
- les autres données métier du quart.

Elle ne modifie pas silencieusement la cible automatique du besoin humain qui a pu produire le quart.

Le but est d’empêcher un rebuild de supprimer/recréer le `Shift` propriétaire et de perdre l’affectation ad hoc.

La synchronisation des `AssetRequirement` provenant des demandes réconcilie uniquement les requirements d’origine `REQUEST`. Elle ne compare ni ne supprime les requirements `SHIFT_AD_HOC`.

### 5. Cycle de vie canonique

#### Assign

Le parcours est :

```text
Shift sans actif
→ créer AssetRequirement SHIFT_AD_HOC
→ créer AssetAllocation
→ operator = Shift.resource_id
```

Qualification, disponibilité, protection du quart, audit, idempotence et CAS Planning appartiennent à une seule transaction.

Les commandes publiques actuelles `reserve()` puis `set_operator()` ne doivent pas être chaînées comme deux succès indépendants pour réaliser cette opération. L’état final doit être validé et appliqué comme une commande composite atomique.

#### Change

Pour :

```text
Asset A → Asset B
```

le requirement et l’allocation conservent leurs identités lorsque le contrat existant le permet.

Le nouvel état complet est validé avant mutation, notamment disponibilité et qualification.

#### Release

Pour :

```text
Asset A → aucun actif
```

si l’origine est `SHIFT_AD_HOC` :

1. supprimer l’`AssetAllocation`;
2. supprimer également l’`AssetRequirement` ad hoc;
3. conserver le `Shift` manuel/verrouillé.

Un requirement ad hoc vide ne doit pas subsister comme faux besoin « À affecter ».

La libération de l’actif ne convertit pas implicitement le quart vers le mode automatique.

#### Move

L’actif ad hoc suit atomiquement le déplacement du quart.

La commande revalide au minimum :

- la nouvelle date;
- la disponibilité de l’actif;
- la qualification de `Shift.resource_id`;
- les règles humaines existantes du move.

En cas de conflit matériel, de qualification ou de règle humaine :

```text
rollback complet du Shift + de l’actif
```

Aucun état intermédiaire déplacé ou réservé ne peut être committé.

#### Split

Pour :

```text
Shift source avec Asset A
→ source conserve Asset A
→ nouveau fragment sans actif
```

la réservation n’est jamais copiée implicitement.

#### Duplicate

Pour :

```text
Shift source avec Asset A
→ source conserve Asset A
→ copie sans actif
```

une affectation sur la copie constitue une nouvelle décision explicite.

#### Delete

La suppression réelle du quart propriétaire nettoie explicitement :

```text
AssetAllocation
→ AssetRequirement SHIFT_AD_HOC
→ Shift
```

dans la même transaction.

Le rebuild ne constitue pas le mécanisme de nettoyage de cette propriété.

### 6. Interaction avec #399 et ADR-009

L’acceptation d’une demande d’annulation conserve la sémantique d’ADR-009 : la simple intention d’annuler ne modifie pas le plan et seule l’acceptation explicite effectue les suppressions destructives autorisées.

Lors d’une acceptation, le nettoyage doit libérer explicitement :

1. les allocations provenant des requirements `REQUEST` de la demande;
2. les allocations `SHIFT_AD_HOC` dont le `Shift` propriétaire fait partie des quarts supprimés dans le périmètre de cette demande.

Le nettoyage est transactionnel et suit ADR-006 / ADR-009.

Aucun rebuild global immédiat n’est utilisé comme autorité de suppression.

### 7. Rebuild et synchronisation

Un `AssetRequirement[SHIFT_AD_HOC]` :

- n’est jamais comparé à une entrée ASSET approuvée inexistante;
- n’est jamais supprimé par la synchronisation des requirements `REQUEST`;
- survit aux rebuilds parce que son `Shift` propriétaire est manuel/verrouillé.

Le retour d’un quart vers le mode automatique est refusé tant qu’un actif ad hoc lui est attaché.

L’utilisateur doit d’abord libérer explicitement l’actif. La politique de conversion éventuelle vers un quart automatique après cette libération reste distincte de cette décision.

### 8. Concurrence, idempotence et audit

Les mutations ad hoc réutilisent sans nouveau compteur :

- `planning_version`;
- le CAS global Planning d’ADR-006;
- le mécanisme d’idempotence existant;
- l’audit existant.

L’ordre logique reste :

```text
auth / permission
→ recherche reçu idempotence
→ replay éventuel
→ CAS Planning
→ relecture état décisionnel
→ validations
→ mutations
→ vérification invariants
→ audit
→ reçu
→ commit
```

Le replay idempotent précède donc le rejet d’une ancienne `planning_version`, conformément au contrat existant.

Aucune version propre aux actifs n’est ajoutée.

### 9. Permissions

`MANAGE_PLANNING` reste la permission métier requise pour :

- assigner un actif ad hoc;
- changer l’actif;
- libérer l’actif.

#399 conserve ses permissions supplémentaires pour accepter une annulation destructrice.

Un scope d’affichage, notamment un futur scope personnel, ne devient pas une permission de mutation.

### 10. Persistance et migration

La cible de persistance ajoute conceptuellement :

```text
AssetRequirement.origin
AssetRequirement.shift_id
```

avec exclusivité conditionnelle entre les formes `REQUEST` et `SHIFT_AD_HOC`.

560A déterminera les détails exacts :

- migration Alembic additive;
- valeurs/enum ou contraintes de domaine;
- clés étrangères;
- contraintes `CHECK`;
- index;
- unicité garantissant une seule affectation ad hoc par quart;
- compatibilité SQLite pour les tests;
- validation SQL Server selon ADR-011.

Cette migration ne doit pas :

- réinterpréter ou reclassifier les allocations historiques;
- backfiller artificiellement un `shift_id`;
- inventer un lien vers une demande;
- rendre `AssetAllocation.asset_requirement_id` optionnel.

Les données existantes conservent leur provenance historique.

### 11. Projection backend et UX

La carte Planning et la popup du quart consomment une projection backend canonique de l’affectation d’actif du `Shift`.

Le frontend ne déduit plus une propriété ad hoc par une heuristique du type :

```text
demande + opérateur + date
```

Pour une réservation `SHIFT_AD_HOC`, la propriété du quart vient du lien explicite `AssetRequirement.shift_id`.

Les réservations historiques `REQUEST` peuvent continuer d’être exposées comme réservations reliées au travail et à l’opérateur lorsqu’elles sont pertinentes à la projection, mais elles ne sont pas converties artificiellement en propriété du quart.

## Invariants

Les invariants durables sont :

1. toute `AssetAllocation` possède exactement un `AssetRequirement`;
2. `AssetAllocation.asset_requirement_id` reste obligatoire;
3. un `AssetRequirement` est soit `REQUEST`, soit `SHIFT_AD_HOC`, jamais les deux;
4. `REQUEST` conserve sa provenance de demande/ligne/autorisation et n’a pas de `shift_id`;
5. `SHIFT_AD_HOC` appartient à un vrai `Shift.id` et ne possède aucune provenance d’approbation fictive;
6. une seule affectation ad hoc existe par quart dans le périmètre initial;
7. `AssetAllocation` demeure l’unique autorité de réservation physique;
8. disponibilité et double booking restent globaux au modèle d’actif;
9. la qualification #292 utilise `Shift.resource_id` comme opérateur du requirement ad hoc;
10. la première affectation ad hoc protège le quart en décision manuelle/verrouillée sans changer son identité;
11. release supprime le requirement ad hoc vide mais conserve le quart protégé;
12. move est atomique sur quart et actif;
13. split et duplicate ne copient jamais implicitement la réservation;
14. delete nettoie explicitement allocation puis requirement ad hoc avant le quart;
15. la synchronisation des besoins ASSET approuvés ne réconcilie que l’origine `REQUEST`;
16. aucun rebuild ne transforme ou supprime silencieusement un `SHIFT_AD_HOC`;
17. aucune mutation ad hoc n’introduit un compteur de concurrence distinct de `planning_version`;
18. les mutations Planning restent fail-closed et transactionnelles.

## Interactions avec les ADR et contrats existants

### ADR-006 — Révision globale des mutations de planning

Cette décision réutilise la garde globale Planning, le CAS avant lectures décisionnelles, le replay idempotent avant conflit de version et la transaction composite. Elle ne crée aucune concurrence parallèle propre aux actifs.

### ADR-007 — Ressources réservables non humaines

ADR-007 reste l’autorité sur la séparation humain/actif, `AssetRequirement → AssetAllocation`, l’occupation exclusive à la journée, la disponibilité et l’absence de sélection automatique silencieuse.

ADR-016 étend uniquement l’identité du propriétaire d’un `AssetRequirement` afin qu’un besoin opérationnel puisse également provenir explicitement d’un `Shift`.

### ADR-009 — Intention d’annulation persistante

ADR-009 reste inchangé : demander/refuser une annulation ne modifie pas le plan. L’acceptation destructive doit maintenant nettoyer également les allocations ad hoc appartenant aux quarts qu’elle supprime.

### ADR-011 — SQL Server autoritaire

Les contraintes de persistance qui implémenteront l’exclusivité `REQUEST` / `SHIFT_AD_HOC` devront être validées sur SQL Server. SQLite reste suffisant seulement pour les tests dont la sémantique ne dépend pas des contraintes ou de la concurrence du moteur cible.

### #292

La qualification reste portée par le contrat existant `AssetAllocation ↔ Resource`. Pour `SHIFT_AD_HOC`, l’opérateur canonique est `Shift.resource_id`; aucune seconde logique de qualification n’est introduite.

### #332 et #333

Les règles de transaction, état projeté, split, duplicate, move, idempotence, audit et rollback sont réutilisées. Les opérations ne doivent pas composer plusieurs commandes publiques produisant des commits ou succès intermédiaires.

### #399

La suppression autorisée par annulation reste explicite et circonscrite au périmètre supprimé. Les allocations ad hoc sont découvertes par leur `Shift` propriétaire et nettoyées dans la même transaction.

### #538

Le raccourci UX d’affectation d’actif reste une surface Planning. Il évoluera en 560A pour cibler une commande canonique capable de créer un requirement ad hoc lorsque le quart n’a pas de requirement d’actif préexistant.

## Alternatives considered

### 1. `AssetRequirement SHIFT_AD_HOC` appartenant au `Shift`

Retenue.

Elle conserve une seule autorité de réservation physique et réutilise les contrats de disponibilité, qualification, concurrence, idempotence et audit. La provenance ad hoc est explicite sans fabriquer d’approbation.

### 2. `AssetAllocation` liée directement au `Shift` avec requirement optionnel

Rejetée.

Elle introduirait deux modèles de réservation, rendrait `asset_requirement_id` nullable et obligerait les projections, la disponibilité, l’annulation et les mutations à gérer deux autorités parallèles.

### 3. Création d’une fausse ligne ASSET dans la demande

Rejetée.

Elle fabriquerait une demande, une entrée approuvée ou une autorisation qui n’a jamais été exprimée par le workflow métier. Elle couplerait en plus un geste Planning local à une mutation artificielle de la demande candidate/approuvée.

### 4. Association implicite par opérateur et date

Rejetée.

`resource_id + date`, même enrichi d’une demande ou d’un projet, n’est pas une identité stable d’un `Shift`. Cette heuristique devient ambiguë avec plusieurs quarts, move, split, duplicate et suppressions et ne fournit aucune propriété persistante fiable.

## Consequences

### Positive

- une seule table d’allocations reste autoritaire pour toutes les réservations physiques;
- l’affectation ad hoc est explicitement propriétaire d’un vrai `Shift`;
- aucune fausse approbation ou ligne ASSET n’est créée;
- les règles #291/#292 sont réutilisées au lieu d’être dupliquées;
- rebuild et synchronisation des demandes peuvent distinguer sans ambiguïté les deux provenances;
- move/delete/annulation disposent d’un lien stable pour effectuer leur nettoyage atomique;
- carte et popup peuvent consommer une projection canonique au lieu d’une heuristique frontend;
- les contrats de concurrence, idempotence et audit restent uniformes.

### Trade-offs / negative

- `AssetRequirement` devient un modèle à deux formes avec contraintes conditionnelles;
- le premier attach ad hoc peut transformer durablement un quart automatique en décision manuelle/verrouillée;
- release ne suffit pas à rendre automatiquement le quart automatique;
- les opérations move/delete/#399 doivent inclure explicitement le cycle de vie du requirement ad hoc;
- la migration et les contraintes devront être validées sur SQL Server;
- les réservations `REQUEST` historiques restent distinctes des propriétés de quart ad hoc, ce qui exige une projection backend claire.

## Implementation notes

L’acceptation de cet ADR termine la sortie documentaire d’ASTRA-560. Elle ne livre aucune fonctionnalité 560A.

Le découpage attendu devient :

```text
ASTRA-560
   ↓
560A
```

560A pourra implémenter la plus petite extension cohérente nécessaire, notamment persistance, commande composite, projection backend, raccord popup/carte et tests.

Cette ADR ne fixe pas les détails exacts de migration, contraintes SQL, index, routes HTTP ou forme finale des DTO. Ces choix doivent respecter les invariants ci-dessus sans redessiner ADR-006, ADR-007, ADR-009, ADR-011, #291, #292, #332, #333, #399 ou #538.

## References

- #55 — Roadmap maître et `COCKPIT_PIPELINE_V1`
- #560 — Planning actifs — affectation ad hoc depuis un quart et visibilité de l’actif
- #291 — ressources réservables non humaines
- #292 — qualification véhicule/équipement ↔ compétence
- #332 — partage et duplication atomiques de quarts
- #333 — extension de fenêtre et déplacement
- #399 — demande d’annulation et résolution coordonnateur
- #538 — raccourcis Quick Shift et affectation d’actif
- ADR-006 — Révision globale des mutations de planning
- ADR-007 — Ressources réservables non humaines et occupation Planning commune
- ADR-009 — Persistent cancellation intent before destructive planning changes
- ADR-011 — SQL Server comme base de référence d’exploitation

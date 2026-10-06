# ADR-026 — Autorité d’attribution d’actifs et propagation calculée vers les quarts

Status: Accepted  
Date: 2026-10-06

## Context

ADR-007, ADR-016 et ADR-018 ont stabilisé le modèle des actifs réservables : toute réservation physique reste portée par `AssetRequirement → AssetAllocation`, avec cinq origines explicites (`REQUEST`, `SHIFT_AD_HOC`, `PROJECT_DIRECT`, `SEGMENT`, `RESOURCE_PERIOD`) et une occupation physique déterminée par `AssetAllocation.start_date/end_date`.

Après la livraison de #575, #615 précise trois dimensions qui ne sont pas entièrement couvertes par ADR-018 :

1. l’autorité nécessaire pour attribuer directement une unité physique;
2. la projection d’une réservation autonome vers les `Shift` pertinents sans créer une deuxième réservation;
3. les invariants corrigés des modes `SEGMENT` et `RESOURCE_PERIOD`, incluant la compatibilité avec les données historiques déjà créées.

L’analyse ASTRA-615 a été effectuée en lecture seule sur `main@1470821d4b085910a4b7c539f98d8207d3f5550b`. Elle confirme que le modèle physique existant peut être prolongé sans nouvelle table de réservation.

ADR-026 succède partiellement à ADR-018 uniquement sur les décisions explicitement modifiées ci-dessous. ADR-018 reste autoritaire pour l’unicité de `AssetAllocation`, les cinq origines, les dates physiques, la disponibilité globale, le double booking, l’audit et le CAS Planning. ADR-016 reste autoritaire pour la propriété et le cycle de vie de `SHIFT_AD_HOC`, mais sa règle de permission `MANAGE_PLANNING` seule est restreinte par le présent ADR.

## Decision

### 1. Une seule autorité physique demeure

Aucune nouvelle table de réservation ni relation persistante `Shift ↔ Asset` n’est ajoutée pour représenter l’héritage.

Le modèle reste :

```text
AssetRequirement
    ↓
AssetAllocation
    ↓
Asset
```

Une même allocation peut être projetée sur plusieurs quarts pertinents, mais elle reste une seule réservation physique et une seule preuve d’occupation.

Les badges, associations ou références exposés sur les `Shift` sont des read models calculés. Ils ne constituent ni une propriété persistante du quart ni une utilisation physique supplémentaire.

### 2. Attribution directe = Planning + admissibilité sur l’unité réelle

Toute décision manuelle qui attribue, remplace ou libère une unité physique exige :

```text
AppUser authentifié et actif
ET manage_planning
ET admissible selon l’autorité #534 pour l’unité réellement concernée
```

L’admissibilité réutilise le système existant :

```text
Approvers(ApprovalScope résolu depuis AssetType)
∪
Approvers spécifiques de Asset
```

avec déduplication par `AppUser.id` et comportement fail-closed.

Un rôle `COORDINATOR`, un rôle `ADMIN`, un contact métier, un responsable opérationnel, un scope d’affichage ou le périmètre `mine` ne donnent aucune autorité implicite sur une unité.

Un approbateur d’actif qui ne possède pas `manage_planning` peut participer au workflow de demande, mais ne peut pas attribuer directement l’unité.

Pour un remplacement `A → B`, l’acteur doit être admissible sur **A et B**, car la même commande libère une unité et en réserve une autre.

### 3. L’autorité courante et le snapshot d’approbation sont distincts

Une attribution directe résout l’autorité **courante** sur l’unité réellement choisie.

Un vote d’approbation continue d’utiliser le snapshot immuable du `RequestApprovalCycle` défini par #534 / ADR-010.

Aucun cycle d’approbation fictif n’est créé pour une réservation directe. Un changement ultérieur d’allocation ne reroute jamais rétroactivement un cycle déjà soumis.

### 4. Toutes les surfaces de mutation manuelle partagent la même politique

La politique d’autorité commune couvre au minimum :

- création directe;
- affectation ou changement d’actif depuis un `Shift`;
- sélection manuelle d’une unité pour un besoin `REQUEST`;
- désignation ou changement d’opérateur;
- remplacement;
- libération.

Les effets automatiques d’un workflow déjà autorisé, notamment le nettoyage destructif de #399, restent distingués de ces décisions manuelles et conservent leurs propres permissions.

React ne contient aucune matrice de rôles lui permettant de décider cette autorité. Le backend retourne les capacités et diagnostics nécessaires.

### 5. L’administration des droits d’actifs est protégée par `admin_settings`

Les mutations capables de changer l’autorité d’attribution doivent être séparées de la simple gestion descriptive du catalogue.

Sont protégés au minimum par `admin_settings` :

- ajout/retrait des approbateurs spécifiques d’une unité;
- modification du rattachement d’un type d’actif à un scope;
- changement du type d’une unité lorsqu’il modifie le scope d’autorité applicable.

Le simple fallback catalogue `manage_resources` ne suffit plus pour modifier ces éléments d’autorité.

Cette règle ferme le défaut où un coordonnateur possédant `manage_resources` pourrait autrement s’ajouter lui-même comme approbateur spécifique avant d’effectuer une attribution directe.

### 6. Les changements d’autorité sont coordonnés avec les commandes Planning

Les commandes d’attribution ne se fient pas à une vérification faite lors de l’ouverture d’un formulaire.

L’autorité est relue et vérifiée dans le chemin transactionnel de la mutation matérielle.

Les mutations d’autorité et les mutations Planning doivent employer une coordination cohérente évitant un `check-then-use` concurrent. L’implémentation peut utiliser une garde transactionnelle commune ou un verrouillage SQL cohérent, mais elle ne peut pas considérer une ancienne lecture d’autorité comme preuve suffisante.

ADR-006 reste applicable à la mutation Planning : replay idempotent, CAS global avant lectures décisionnelles, revalidation, mutation, audit et commit atomique.

### 7. Les actifs hérités sur les quarts sont des projections calculées

Le champ ou contrat représentant l’actif **possédé** par un `Shift` demeure réservé à `SHIFT_AD_HOC`.

Les autres réservations pertinentes sont exposées séparément comme réservations liées/héritées.

Association recommandée, toujours limitée aux **dates physiques** de l’allocation :

| Origine | Association au quart |
|---|---|
| `RESOURCE_PERIOD` | même ressource bénéficiaire/opérateur et date couverte |
| `PROJECT_DIRECT` avec opérateur | même ressource, même projet et date couverte |
| `PROJECT_DIRECT` sans opérateur | aucun héritage individuel; réservation pertinente du projet |
| `SEGMENT` avec opérateur | même segment, même opérateur et date couverte |
| `SEGMENT` sans opérateur | aucun opérateur inféré; visibilité dans son contexte segment |
| `SHIFT_AD_HOC` | FK explicite vers le quart propriétaire |
| `REQUEST` | contrat existant de réservation liée au travail |

Pour `SEGMENT`, l’héritage est limité au segment propriétaire et ne se propage pas à tous les travaux du même projet.

La projection expose au minimum l’identité de l’allocation/requirement, l’origine, le propriétaire/contexte, les dates physiques, l’opérateur éventuel, la qualification et les actions autorisées.

Elle distingue explicitement « hérité pour cette ressource » de « réservé dans le contexte du travail ».

Plusieurs réservations héritées peuvent être pertinentes pour un même quart. Elles ne sont jamais réduites arbitrairement à un champ singulier.

### 8. Rebuild, split, duplicate, move et delete ne copient pas la réservation autonome

Les associations calculées sont recalculées depuis l’état canonique.

- création/rebuild d’un quart : recalcul des héritages, aucune nouvelle allocation;
- changement de ressource/projet/date du quart : recalcul des associations applicables;
- split/duplicate : recalcul sur chaque fragment, sans copie de l’allocation physique;
- suppression du quart : les réservations `PROJECT_DIRECT`, `SEGMENT` et `RESOURCE_PERIOD` autonomes restent en place;
- `SHIFT_AD_HOC` : le cycle de vie atomique d’ADR-016 demeure;
- changement de cible automatique d’un segment : ne change pas l’opérateur de l’actif;
- extension de fenêtre, y compris #613 : n’étend jamais les dates physiques;
- réduction incompatible ou changement de projet du segment : mutation refusée jusqu’à résolution explicite;
- perte ultérieure de qualification : occupation conservée, diagnostic projeté;
- annulation acceptée #399 : nettoyage par propriétaire réel, sans supprimer une réservation autonome partageant seulement le projet.

### 9. Le sélecteur projet réutilise une réservation existante

Une réservation `PROJECT_DIRECT` déjà pertinente ne rend pas globalement l’actif « libre ».

Le backend distingue au minimum :

| Situation | Action |
|---|---|
| Actif libre | créer une nouvelle réservation si autorisé |
| Réservation projet pertinente sans opérateur | désigner explicitement un opérateur sur cette allocation existante |
| Réservation déjà héritée par la ressource | afficher l’association existante |
| Réservation attribuée à quelqu’un d’autre | afficher l’état, sans réaffectation implicite |
| Occupation incompatible / hors périmètre | indisponible, détails protégés |

La désignation cible l’`AssetAllocation` existante par son ID et revalide projet, dates, qualification, disponibilité et droits.

### 10. La désignation d’opérateur porte sur toute la réservation

Le modèle conserve un seul `operator_resource_id` par `AssetAllocation`.

Lorsqu’un utilisateur choisit depuis un quart une réservation projet sans opérateur, il désigne explicitement la ressource comme opérateur **pour toute la période de cette réservation**, après présentation claire de cette portée dans l’UX.

Les autres quarts compatibles de cette ressource héritent ensuite de la même allocation.

Aucune allocation supplémentaire n’est créée.

Un besoin futur « opérateur seulement pour ce quart ou cette journée » nécessiterait un contrat d’utilisation datée distinct et ne doit pas être improvisé dans #615.

### 11. `SEGMENT` peut exister sans opérateur

ADR-018 est modifié sur ce point.

Une réservation `SEGMENT` peut être persistée sans opérateur.

Qualification :

- aucun skill requis : état canonique `SATISFIED`;
- skill requis et aucun opérateur : réservation conservée, état `MISSING_OPERATOR`;
- opérateur fourni mais inactif ou non qualifié : mutation refusée;
- aucun opérateur n’est inféré depuis `ResourceRequirement.assigned_resource_id`;
- aucun faux `Shift` n’est créé pour satisfaire la qualification.

Une réservation non qualifiée reste physiquement occupante mais peut bloquer une confirmation de communication selon les diagnostics existants.

### 12. `RESOURCE_PERIOD` n’accepte plus de nouveau projet, sans casser l’historique

Pour les **nouvelles créations** `RESOURCE_PERIOD` :

- ressource/bénéficiaire obligatoire;
- opérateur obligatoire et égal à la ressource de contexte;
- projet absent;
- toute valeur `project_id` non nulle est rejetée côté backend;
- React désactive le projet et élimine les valeurs résiduelles lorsqu’on change vers ce mode.

Les réservations historiques déjà persistées avec un projet restent lisibles et conservées telles quelles.

Elles ne sont ni effacées, ni réinterprétées, ni élargies silencieusement.

Une éventuelle normalisation future doit être une action explicite et auditée.

Aucune contrainte SQL immédiate `project_id IS NULL` ne peut être ajoutée tant que ces données historiques existent sans stratégie de reprise compatible.

### 13. Communications et qualification utilisent la même projection canonique

Les diagnostics et communications doivent inclure les réservations héritées pertinentes sans :

- omettre un `RESOURCE_PERIOD` valide parce qu’il n’a pas de projet;
- propager un défaut de qualification `SEGMENT` à tout le projet;
- reconstruire une qualification différente de la logique canonique d’actifs.

Les read models batch doivent charger explicitement les nouvelles origines pertinentes; modifier uniquement la sérialisation finale est insuffisant.

### 14. Périmètre d’affichage et occupation physique restent séparés

Un actif hors scope d’affichage continue d’occuper physiquement sa période et de participer au double booking.

Une projection héritée ne révèle pas le contexte protégé d’une réservation auquel l’utilisateur n’a pas accès.

Le backend peut exposer un état d’indisponibilité ou des capacités sans divulguer les détails sensibles de l’autre réservation.

### 15. Audit

L’audit d’une mutation matérielle conserve la preuve de décision :

- acteur;
- unité;
- origine;
- contexte propriétaire;
- opérateur;
- dates;
- provenance de l’autorité;
- IDs requirement/allocation;
- corrélation idempotente;
- version Planning pertinente.

Il n’est pas nécessaire de journaliser chaque badge calculé affiché sur chaque quart.

## Alternatives considered

### Nouvelle table de liaison physique Shift ↔ Asset

Rejetée : elle introduirait une seconde autorité physique, dupliquerait l’occupation et fragiliserait disponibilité, audit, nettoyage et concurrence.

### Rendre un actif « disponible » lorsqu’il est déjà réservé au même projet

Rejeté : une réservation existante doit être réutilisée explicitement; ignorer son occupation permettrait des doubles réservations ou des changements implicites d’opérateur.

### `MANAGE_PLANNING` seul comme autorité d’attribution

Rejeté : cette permission donne la capacité d’opérer le Planning, mais elle ne prouve pas l’autorité métier sur l’unité réelle. #534 fournit déjà le mécanisme d’admissibilité actif/type à réutiliser.

### Exception implicite pour ADMIN ou COORDINATOR

Rejetée : les rôles applicatifs ne remplacent pas l’autorité configurée sur l’actif. L’administration des droits elle-même est protégée séparément.

### `RESOURCE_PERIOD.project_id` interdit par contrainte SQL immédiate

Rejeté : des réservations historiques valides selon l’ancien contrat peuvent déjà contenir un projet. La compatibilité de lecture est donc obligatoire avant toute normalisation explicite.

### Opérateur `SEGMENT` inféré depuis la ressource assignée au besoin humain

Rejeté : la cible/affectation humaine et l’opérateur réel de l’actif restent des concepts distincts conformément à ADR-001 et ADR-018.

## Consequences

### Positive

- aucune nouvelle autorité physique n’est introduite;
- les réservations autonomes deviennent visibles sur les quarts pertinents sans double booking;
- l’autorité d’attribution devient cohérente avec les approbateurs d’actifs déjà configurés;
- un coordonnateur ne peut pas s’auto-accorder l’autorité via le catalogue;
- le sélecteur projet réutilise une réservation existante au lieu de la contourner;
- `SEGMENT` sans opérateur devient représentable sans faux `Shift`;
- `RESOURCE_PERIOD` suit le nouveau contrat produit tout en préservant les données historiques;
- React consomme des capacités et projections backend sans recopier le RBAC ou la qualification;
- le cycle de vie existant de `SHIFT_AD_HOC` reste intact.

### Trade-offs / negative

- les mutations d’actifs doivent désormais résoudre une autorité plus fine que `MANAGE_PLANNING`;
- les changements d’autorité doivent être coordonnés avec les mutations Planning concurrentes;
- les read models de quarts doivent charger plusieurs origines et plusieurs allocations héritées;
- la désignation d’un opérateur projet s’applique à toute la période de l’allocation, ce qui doit être explicite dans l’UX;
- les anciennes réservations `RESOURCE_PERIOD` avec projet nécessitent une compatibilité durable jusqu’à une éventuelle normalisation explicite;
- les communications doivent distinguer les diagnostics par contexte au lieu de les agréger naïvement au projet.

## Implementation notes

Le découpage retenu pour #615 reste séquentiel :

```text
ASTRA-615
   ↓
615A — politique d’autorité commune, administration protégée,
       couverture des mutations et concurrence des droits
   ↓
615B — projection d’héritage, réutilisation des réservations projet,
       désignation explicite, qualification et communications
   ↓
615C — invariants backend/API/React des trois modes,
       compatibilité RESOURCE_PERIOD historique et acceptation transverse
```

615C n’est pas une tranche exclusivement frontend.

L’acceptation doit couvrir au minimum :

- refus d’un coordonnateur hors autorité et impossibilité de s’autoattribuer les droits;
- stabilité des snapshots #534 après changement d’autorité;
- `RESOURCE_PERIOD` créé avant ses quarts puis visible sans nouvelle allocation;
- absence d’attribution automatique pour `PROJECT_DIRECT` sans opérateur;
- réutilisation explicite d’une réservation projet sans double booking;
- `SEGMENT` sans opérateur avec `MISSING_OPERATOR` si une compétence est requise;
- move/split/duplicate/delete/rebuild sans changement du propriétaire physique;
- compatibilité des anciennes réservations `RESOURCE_PERIOD` avec projet;
- concurrence SQL Server : attribution concurrente, retrait d’autorité, replay et rollback.

## References

- #55 — roadmap maître / `COCKPIT_PIPELINE_V1`
- #615 — Actifs post-575 — autorité coordonnateur et propagation projet / segment / ressource
- #534 — approbation des actifs par type et unité
- #575 — trois modes de réservation d’actifs
- #292 — qualification véhicule/équipement ↔ compétence
- #399 — annulation et nettoyage transactionnel
- #613 — fenêtre Planning et dérogations
- ADR-001 — cible automatique distincte de l’affectation réelle
- ADR-006 — CAS global des mutations Planning
- ADR-007 — ressources réservables non humaines
- ADR-010 — scopes, approbateurs et snapshots
- ADR-016 — affectation d’actifs ad hoc appartenant à un quart
- ADR-018 — contextes de réservation d’actifs et occupation physique
- ADR-022 — dérogations opérationnelles de fenêtre Planning

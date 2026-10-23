# ADR-032 — Attribution continue de véhicule par RESOURCE_PERIOD ouvert

Status: Accepted — amended after Architect review
Date: 2026-10-23
Amended: 2026-10-23

## Context

ADR-018 a établi `AssetRequirement → AssetAllocation → Asset` comme modèle unique de réservation physique et a introduit `RESOURCE_PERIOD` pour l'attribution directe d'un actif à une ressource pendant une période explicite.

ADR-026 a ensuite fixé que :

- `AssetAllocation` demeure l'unique autorité d'occupation physique;
- une attribution directe exige `manage_planning` et l'autorité d'attribution de l'unité selon #534/#615A;
- une réservation `RESOURCE_PERIOD` est projetée vers les `Shift` de la ressource sans créer de seconde allocation;
- les nouvelles réservations `RESOURCE_PERIOD` sont sans projet;
- `context_resource_id == operator_resource_id` reste l'invariant normal de ce mode.

#743 / #744 ajoutent un besoin métier durable : un véhicule peut être **attitré** à une ressource à partir d'une date, sans date de fin connue, et rester physiquement indisponible pour toute réservation concurrente jusqu'à une révocation ou un transfert explicite.

Le modèle actuel impose cependant `end_date NOT NULL` sur `AssetRequirement` et `AssetAllocation`. Utiliser une fausse date comme 2099 créerait une donnée artificielle, rendrait les requêtes et audits trompeurs et compliquerait la révocation.

La décision doit donc définir :

- la représentation d'une période ouverte;
- les règles de chevauchement et de disponibilité;
- révocation et transfert;
- qualification et autorité;
- interaction avec réparations / indisponibilités;
- concurrence, idempotence et audit;
- projection dans les quarts et lectures;
- compatibilité SQL Server / SQLite;
- limites de portée pour #744 et #762.

## Decision

### 1. `RESOURCE_PERIOD` reste le seul modèle de l'attribution continue

Aucune nouvelle table de réservation physique ni champ de type :

```text
Asset.assigned_resource_id
Asset.current_resource_id
Asset.is_assigned
```

n'est introduit comme autorité parallèle.

Le modèle reste :

```text
Resource
  ↓ context_resource_id
AssetRequirement[RESOURCE_PERIOD]
  ↓
AssetAllocation
  ↓
Asset
```

`AssetAllocation` demeure l'unique autorité d'occupation physique.

La case **Attitré** de #762 sera une projection/commande de ce contrat, jamais une seconde source de vérité.

### 2. Une attribution continue est un intervalle ouvert avec `end_date = NULL`

Pour `RESOURCE_PERIOD` uniquement, une attribution sans fin connue est représentée par :

```text
start_date = date réelle de début
end_date   = NULL
```

sur :

- `AssetRequirement.end_date`;
- `AssetAllocation.end_date`.

`NULL` signifie ici **ouvert vers le futur / +∞**, et non « date inconnue ».

Aucune date sentinelle telle que :

```text
2099-12-31
9999-12-31
```

n'est utilisée.

Pour les autres origines, `end_date` reste obligatoire.

### 3. Requirement et allocation restent alignés pour RESOURCE_PERIOD

Pour une réservation `RESOURCE_PERIOD`, le requirement support et l'allocation physique utilisent la même borne de début et la même borne de fin :

```text
requirement.start_date == allocation.start_date
requirement.end_date   == allocation.end_date
```

Pour une attribution continue active :

```text
requirement.end_date IS NULL
allocation.end_date   IS NULL
```

La fenêtre support ne doit pas devenir une seconde durée différente de l'occupation physique.

### 4. Invariants de schéma

La migration de #744 est additive.

`AssetRequirement.end_date` et `AssetAllocation.end_date` deviennent nullable.

Les CHECKs doivent exprimer au minimum :

```text
end_date IS NULL OR end_date >= start_date
```

Pour `AssetRequirement`, le schéma doit également garantir que `end_date IS NULL` n'est permis que pour l'origine `RESOURCE_PERIOD`.

`AssetAllocation` ne porte pas l'origine directement; l'application doit donc garantir que toute allocation à `end_date NULL` appartient à un requirement `RESOURCE_PERIOD` lui-même ouvert. Les migrations/tests de readiness doivent détecter toute donnée violant cet invariant.

Aucune donnée historique bornée n'est convertie automatiquement en période ouverte.

### 4A. Provenance persistante CONTINUOUS vs BOUNDED

`RESOURCE_PERIOD` seul ne suffit pas à protéger l'historique : après révocation, une ancienne attribution continue possède une `end_date` non nulle et devient visuellement semblable à une réservation ressource-période bornée ordinaire.

#744 doit donc persister sur le **requirement support** une provenance explicite et durable distinguant au minimum :

```text
RESOURCE_PERIOD / BOUNDED
RESOURCE_PERIOD / CONTINUOUS
```

Le nom exact du champ peut suivre les conventions du modèle, mais sa sémantique doit être stable. Par exemple :

```text
resource_period_mode = BOUNDED | CONTINUOUS
```

Cette provenance :

- n'est **pas** une seconde autorité d'occupation;
- ne remplace jamais `AssetAllocation`;
- reste `CONTINUOUS` après révocation ou transfert historique;
- permet aux commandes et migrations de reconnaître qu'un intervalle aujourd'hui borné provient d'un attitrage continu.

Les `RESOURCE_PERIOD` historiques existants sont classés `BOUNDED` par migration déterministe, ou par une convention persistée équivalente qui ne dépend jamais de l'état actuel de `end_date`.

Les autres origines n'utilisent pas cette provenance.

### 4B. Protection de l'historique dans toutes les mutations

Toute mutation générique existante qui pourrait supprimer ou réécrire un `RESOURCE_PERIOD / CONTINUOUS` doit être protégée.

En particulier :

- le chemin générique de **release** ne doit pas supprimer un requirement/allocation `CONTINUOUS`, même après que sa fin a été bornée par révocation;
- le chemin générique d'**update bounded resource-period** ne doit pas remplacer l'actif, le bénéficiaire ou convertir la sémantique d'un ancien `CONTINUOUS`;
- une attribution `CONTINUOUS` ne peut être modifiée que par les commandes explicites de création, révocation, transfert/remplacement, annulation planifiée ou correction historique définies par ce contrat;
- la fermeture d'une attribution conserve requirement + allocation comme historique, sauf annulation avant prise d'effet telle que définie plus bas.

Une ancienne attribution continue ne redevient donc jamais une réservation bornée générique simplement parce que `end_date` n'est plus `NULL`.

### 5. Sémantique d'intervalle commune

Toutes les lectures d'occupation d'actifs doivent utiliser la même sémantique :

```text
[start_date, end_date]
```

avec `NULL = +∞`.

Pour une fenêtre de lecture bornée `[query_start, query_end]`, une allocation chevauche la fenêtre si :

```text
allocation.start_date <= query_end
AND
(
  allocation.end_date IS NULL
  OR allocation.end_date >= query_start
)
```

Pour un candidat borné `[candidate_start, candidate_end]`, un conflit existe si :

```text
existing.start_date <= candidate_end
AND
(
  existing.end_date IS NULL
  OR existing.end_date >= candidate_start
)
```

Pour un candidat ouvert `[candidate_start, +∞)`, un conflit existe si :

```text
existing.end_date IS NULL
OR existing.end_date >= candidate_start
```

Le runtime doit centraliser cette logique autant que possible afin d'éviter des prédicats divergents entre :

- service de réservation;
- requêtes Planning;
- scope/occupation;
- communications;
- SQL Server readiness;
- projections de Shift;
- diagnostics de disponibilité.

### 6. Un véhicule attitré reste indisponible globalement

Une attribution continue ouverte consomme la capacité physique du véhicule sur toute période future tant qu'elle n'est pas close.

Toute nouvelle réservation concurrente, quelle que soit son origine, doit échouer si son intervalle chevauche l'attribution continue.

L'origine `RESOURCE_PERIOD` ne bénéficie d'aucun bypass de double booking.

Inversement, la création d'une attribution continue doit échouer si une réservation physique future existante chevauche la période ouverte.

### 7. Indisponibilités et réparations restent distinctes de l'attitrage

`AssetUnavailability` reste une autorité distincte d'indisponibilité opérationnelle, notamment pour #639.

Une attribution continue ne doit pas être révoquée automatiquement parce qu'une indisponibilité ou réparation est créée après coup.

Le véhicule peut donc être simultanément :

```text
attitré à une ressource
+
temporairement indisponible pour réparation
```

Le read model doit pouvoir exprimer ce conflit/état sans libérer l'attribution.

Pendant l'indisponibilité, l'actif n'est pas considéré comme utilisable même si l'attitrage demeure. À la fin de l'indisponibilité, l'attribution continue reprend naturellement puisqu'elle n'a pas été révoquée.

Lors de la création ou de l'extension d'une attribution, les indisponibilités déjà connues qui chevauchent l'intervalle restent bloquantes conformément à ADR-018.

#639 ne doit pas supprimer ni recréer silencieusement une attribution continue.

### 8. Qualification

À la création, au remplacement ou au transfert d'une attribution continue :

- la ressource de contexte doit exister et être active;
- `context_resource_id == operator_resource_id`;
- la qualification de l'actif reste évaluée par le mécanisme canonique existant;
- les compétences requises du type d'actif restent obligatoires;
- une qualification invalide bloque la mutation.

Une perte ultérieure de compétence ou une désactivation de la ressource ne supprime pas silencieusement une réservation historique/active. Elle doit être diagnostiquée par les projections existantes et résolue explicitement.

### 9. Autorité d'attribution

ADR-026 reste autoritaire.

Les commandes d'attribution continue exigent :

```text
manage_planning
+
autorité #534/#615A sur l'unité physique concernée
```

La création exige l'autorité sur le véhicule à attribuer.

La révocation exige l'autorité sur le véhicule actuellement attribué.

Un transfert/remplacement atomique exige l'autorité sur toute unité libérée et toute unité nouvellement attribuée.

Aucun bypass ADMIN/COORDINATOR implicite n'est ajouté.

Une ambiguïté AssetType → ApprovalScope continue d'échouer fermée.

### 10. Révocation : fermeture réelle, pas suppression historique

Une attribution continue active est révoquée par une commande explicite avec une **date effective de fin d'attitrage**.

Le contrat métier utilise :

```text
effective_from
```

comme premier jour où l'ancien attitrage ne s'applique plus.

Pour une attribution ayant commencé avant `effective_from` :

```text
end_date = effective_from - 1 jour
```

sur le requirement et l'allocation.

Ainsi, un transfert effectif le 2026-11-10 produit :

```text
ancien attitrage : ... → 2026-11-09
nouvel attitrage : 2026-11-10 → NULL
```

La réservation historique bornée est conservée; elle n'est pas supprimée.

Si une révocation est effective au plus tard à la date de début et qu'aucune journée d'occupation ne subsiste, la commande peut annuler le requirement et retirer l'allocation dans la même transaction, tout en conservant l'audit/idempotency receipt. Elle ne doit jamais créer un intervalle invalide.

### 10A. État effectif à une date donnée

L'état **Attitré** n'est jamais dérivé du seul fait que `end_date IS NULL`.

Pour une date métier `D`, une attribution continue est :

```text
SCHEDULED
si start_date > D

ACTIVE
si start_date <= D
et (end_date IS NULL ou D <= end_date)

ENDED
si end_date IS NOT NULL
et end_date < D
```

Le read model backend doit exposer cette sémantique de façon autoritaire, directement ou via des champs équivalents suffisamment explicites.

Conséquences :

- une attribution avec une fin prévue le mois prochain reste **ACTIVE aujourd'hui**;
- une attribution ouverte commençant la semaine prochaine est **SCHEDULED**, pas ACTIVE;
- l'UI #762 ne doit pas interpréter `NULL` ou non-`NULL` seule pour décider si la case **Attitré** est cochée aujourd'hui.

Les lectures peuvent accepter une date `as_of` lorsqu'une surface historique/future l'exige. En l'absence de date explicite, la surface opérationnelle utilise la date métier courante du backend.

### 10B. Transitions planifiées, annulation et replanification

Une attribution future peut être créée comme `SCHEDULED`.

Une transition future doit être visible dans le read model afin d'éviter qu'une future attribution ou révocation reste cachée jusqu'au jour d'effet.

Règles :

- une attribution future qui n'a encore produit aucune journée d'occupation peut être **annulée explicitement**; la suppression requirement/allocation est permise dans ce seul cas, avec audit et idempotence;
- une attribution future peut être **replanifiée** vers une autre date future par une commande explicite, après revalidation complète des conflits, indisponibilités, qualification et autorité;
- une révocation future peut être annulée ou replanifiée avant sa prise d'effet par une commande explicite, avec restauration atomique de la borne précédente;
- les commandes ordinaires refusent les corrections rétroactives qui modifieraient une période déjà effective ou historique.

### 10C. Backdating et correction historique

Les commandes opérationnelles ordinaires doivent rejeter une création, révocation, transfert ou replanification dont la date effective est antérieure à la date métier courante lorsqu'elle réécrirait de l'historique déjà effectif.

Une correction historique, si nécessaire, relève d'un **processus explicite et audité distinct** avec permission appropriée, justification obligatoire, validation des conflits sur les intervalles résultants et traçabilité avant/après.

#744 n'a pas besoin de livrer une UI générale de correction historique, mais son modèle et ses protections ne doivent pas rendre possible un backdating silencieux via les endpoints ordinaires.

### 11. Transfert et remplacement sont atomiques

Le transfert est une commande métier composite sous un seul CAS Planning et une seule transaction.

Elle peut couvrir conceptuellement :

- même véhicule → nouvelle ressource;
- même ressource → nouveau véhicule;
- véhicule A / ressource A → véhicule B / ressource B.

La séquence logique est :

1. replay idempotent;
2. CAS `planning_version`;
3. relecture des allocations concernées;
4. validation des autorités;
5. validation de la date effective;
6. validation de la nouvelle ressource et qualification;
7. validation de la disponibilité future;
8. fermeture de l'ancienne attribution à `effective_from - 1`;
9. création de la nouvelle attribution à `effective_from`;
10. audit des deux côtés;
11. commit atomique.

Aucun état intermédiaire visible ne doit laisser le véhicule doublement réservé ou libre entre deux commandes.

### 11A. Validation de conflit sur l'état final proposé

La validation d'un transfert doit raisonner sur les **intervalles finaux proposés**, pas sur l'état pré-mutation brut.

Pour un transfert du même véhicule de la ressource A vers la ressource B à `effective_from` :

```text
ancienne allocation A : ... → effective_from - 1
nouvelle allocation B : effective_from → ...
```

Lors de la validation de la nouvelle allocation, l'ancienne allocation qui est fermée dans **la même transaction** ne doit pas être traitée comme un conflit pour la portion qui disparaît dans l'état final.

Conceptuellement, le moteur de disponibilité valide :

```text
proposed_final_state
=
existing_allocations
- allocation_being_closed_as_current
+ proposed_closed_interval
+ proposed_new_interval
```

ou un mécanisme équivalent garantissant la même propriété.

Cette exception est strictement limitée à l'allocation concernée par la fermeture atomique. Restent toujours bloquants :

- toute autre `AssetAllocation` chevauchante;
- toute `AssetUnavailability` chevauchante;
- toute seconde mutation concurrente détectée par CAS;
- toute incompatibilité de qualification ou d'autorité.

Si n'importe quelle validation échoue, **aucune** fermeture ni création n'est persistée : la transaction entière est rollback.

### 12. Modification d'une attribution ouverte

Une attribution ouverte peut être modifiée explicitement, mais aucune commande ne doit transformer silencieusement :

- une réservation bornée ordinaire en attribution continue;
- une attribution continue en période bornée;
- le bénéficiaire sans appliquer la sémantique de transfert.

Les intentions API doivent rester distinctes :

```text
create continuous assignment
revoke continuous assignment
transfer/replace continuous assignment
update bounded resource-period reservation
```

Le serveur peut réutiliser des primitives communes, mais l'API ne doit pas rendre ambiguë l'intention métier.

Les endpoints/services historiques de `RESOURCE_PERIOD` borné doivent refuser toute mutation qui viserait un requirement portant la provenance `CONTINUOUS`. Cette protection reste vraie après révocation, lorsque l'intervalle est devenu borné.

### 13. Projection vers les Shift

ADR-026 reste inchangé : une attribution `RESOURCE_PERIOD` est **projetée** vers les Shift de sa ressource sans créer de nouvelle `AssetAllocation`.

Pour une allocation ouverte, l'association vaut lorsque :

```text
allocation.start_date <= shift.work_date
AND
(
  allocation.end_date IS NULL
  OR shift.work_date <= allocation.end_date
)
AND
requirement.context_resource_id == shift.resource_id
AND
allocation.operator_resource_id == shift.resource_id
```

La création d'un Shift ne duplique donc jamais le véhicule attitré.

La suppression/reconstruction d'un Shift ne modifie jamais l'attribution continue.

### 14. Actifs libres et disponibilité UI

Une allocation continue ouverte doit être prise en compte dans le calcul global d'occupation.

Le véhicule ne doit pas apparaître comme « libre » pour une réservation concurrente normale.

Les surfaces UI peuvent indiquer :

```text
Attitré à <ressource>
```

ou un diagnostic équivalent, mais la décision de disponibilité reste backend.

#762 doit lire l'état « Attitré » depuis la réservation `RESOURCE_PERIOD` active, pas depuis un booléen sur `Asset`.

### 15. Cardinalité

Le double booking garantit qu'un même véhicule ne peut pas être physiquement attitré à deux ressources sur des périodes qui se chevauchent.

Cette ADR n'introduit **pas** de nouvelle contrainte « une seule unité attitrée par ressource ».

Si le produit souhaite plus tard limiter chaque ressource à un seul véhicule attitré simultanément, cette règle doit être décidée explicitement; elle n'est pas inférée ici.

### 16. Concurrence et idempotence

Toutes les mutations de #744 utilisent le CAS global `planning_version` d'ADR-006.

Le replay idempotent est vérifié avant de refaire les lectures décisionnelles, selon les conventions actuelles.

Aucune version spécifique à `AssetAllocation` n'est ajoutée.

Une mutation concurrente ne peut pas écraser silencieusement une attribution créée, révoquée ou transférée dans une autre session.

### 17. Audit

L'audit existant reste l'autorité.

Pour une attribution continue, journaliser au minimum :

- requirement/allocation IDs;
- asset ID;
- resource ID;
- start date;
- end date avant/après;
- caractère ouvert ou borné dérivé de `end_date`;
- action CREATE / REVOKE / TRANSFER / REPLACE;
- ancienne/nouvelle ressource;
- ancien/nouvel actif;
- date effective;
- preuve d'autorité;
- planning version;
- clé/corrélation idempotente.

Aucune seconde table d'historique métier n'est requise.

### 18. Lecture historique

Une attribution révoquée reste une réservation `RESOURCE_PERIOD` bornée et doit rester visible dans les lectures historiques lorsque la fenêtre demandée la chevauche.

Une attribution active ouverte apparaît dans toute fenêtre future chevauchant sa date de début.

Les read models ne doivent pas présenter `end_date = NULL` comme donnée manquante; ils doivent l'exposer explicitement comme période continue / « jusqu'à révocation ».

Les read models doivent également distinguer l'état effectif `SCHEDULED / ACTIVE / ENDED` pour la date de lecture et exposer les transitions futures pertinentes sans exiger que le client reconstruise cette logique.

### 18A. Confidentialité inter-scope

ADR-027 reste autoritaire sur les détails exposés.

Une attribution cachée du scope courant doit néanmoins continuer à participer **globalement** aux calculs de disponibilité et de double booking.

Le backend peut donc répondre qu'une unité est indisponible/réservée sans révéler :

- le nom de la ressource bénéficiaire;
- le projet ou contexte caché;
- les dates détaillées au-delà de ce qui est nécessaire;
- toute autre donnée interdite par le scope de lecture.

Principe :

```text
occupation globale
≠
droit de voir les détails de l'occupation
```

Les diagnostics de conflit doivent rester suffisamment précis pour l'action utilisateur sans devenir une fuite de données inter-scope.

### 19. Compatibilité SQL Server / SQLite

La migration doit être additive et compatible avec les deux dialectes.

Les tests doivent couvrir les prédicats nullable sur SQL Server réel pour les scénarios sensibles de chevauchement et concurrence.

Les indexes existants sur :

```text
(asset_id, start_date, end_date)
```

peuvent être conservés initialement; #744 doit mesurer/valider les requêtes réelles avant d'introduire un index supplémentaire.

Aucun index filtré spécifique n'est imposé par l'ADR tant que le besoin de performance n'est pas démontré.

### 20. Données historiques

Les `RESOURCE_PERIOD` bornés existants restent valides.

Aucun backfill ne convertit automatiquement une ancienne réservation en attribution continue.

Les autres origines gardent une `end_date` métier obligatoire.

La migration ne doit pas réinterpréter les anciennes dates ni modifier les allocations historiques.

### 20A. Déploiement progressif compatible

L'introduction de `end_date = NULL` nécessite une séquence de déploiement compatible.

Avant d'autoriser la première écriture ouverte, **tous les readers runtime déployés** susceptibles de lire `AssetRequirement.end_date` ou `AssetAllocation.end_date` doivent accepter la nullabilité et appliquer `NULL = +∞`.

La séquence attendue est conceptuellement :

1. déployer le schéma nullable et des readers compatibles;
2. valider les chemins de lecture, disponibilité, Planning, communications et outils SQL;
3. seulement ensuite activer les commandes capables d'écrire des attributions ouvertes.

#744 peut réaliser cette compatibilité dans un seul déploiement atomique si l'exploitation garantit qu'aucun ancien binaire incompatible ne continue à servir du trafic. Sinon, utiliser un feature flag ou une activation séparée.

Aucun writer ne doit produire `end_date = NULL` tant qu'un reader actif peut supposer `end_date NOT NULL`.

### 20B. Rollback et downgrade

Un rollback applicatif vers une version qui sait lire les périodes ouvertes est permis.

En revanche, un downgrade de schéma qui restaurerait `end_date NOT NULL` doit être **refusé** tant qu'il existe une attribution ouverte.

Le rollback ne doit jamais résoudre cette incompatibilité en :

- supprimant les attributions ouvertes;
- inventant une date de fin;
- convertissant silencieusement une attribution continue en réservation bornée;
- tronquant l'historique.

Un outil/check de downgrade ou une garde de migration doit détecter la présence d'assignments ouverts et échouer explicitement avec diagnostic opératoire.

La restauration vers une version réellement incompatible avec les lecteurs nullable nécessite d'abord une opération métier explicite de fermeture/révocation des attributions concernées, pas une mutation technique cachée.

### 21. Portée de #744

#744 implémente le contrat backend/API/React minimal pour :

- créer une attribution continue de véhicule depuis une ressource;
- afficher l'attribution active;
- révoquer explicitement;
- transférer/remplacer atomiquement;
- rendre le véhicule indisponible aux réservations concurrentes;
- projeter l'attribution sur les Shift sans duplication;
- préserver audit/CAS/idempotence/qualification/autorité;
- supporter les périodes `RESOURCE_PERIOD` bornées historiques.

La portée initiale concerne les types d'actifs de catégorie canonique :

```text
VEHICLE
```

Une généralisation à toutes les catégories d'actifs n'est pas implicite.

### 22. Portée de #762

#762 peut ensuite ajouter sur la fiche d'une unité véhicule :

```text
[ ] Attitré
Ressource : [ ... ]
```

Cette UX :

- lit l'état depuis le `RESOURCE_PERIOD` actif;
- crée/transfère/révoque via les commandes #744;
- ne persiste aucun booléen d'attitrage;
- ne contourne jamais disponibilité, qualification ou autorité.

## Alternatives considered

### Date sentinelle 2099/9999

Rejetée.

Elle transforme une absence réelle de fin en fausse donnée métier, rend les audits trompeurs et introduit des cas spéciaux partout.

### Nouvelle table `ResourceVehicleAssignment` comme autorité physique

Rejetée.

Elle dupliquerait `AssetAllocation`, casserait l'unicité du calcul d'occupation et exigerait une synchronisation permanente entre deux autorités.

### Booléen `Asset.is_assigned` + `assigned_resource_id`

Rejeté.

Ce modèle ne représente ni les dates, ni l'historique, ni les transferts, ni les conflits physiques.

### Suppression de l'allocation lors d'une révocation

Rejetée comme comportement normal.

La révocation doit conserver l'historique réel en bornant l'allocation. La suppression reste réservée au cas où l'attribution est annulée avant d'avoir produit une journée valide d'occupation.

### Création d'une allocation par Shift

Rejetée.

ADR-026 impose une projection calculée; multiplier les allocations créerait des doubles réservations et un cycle de vie incohérent.

### Nouvelle version de concurrence propre aux actifs

Rejetée.

ADR-006 fournit déjà le CAS global requis pour les mutations Planning.

## Consequences

### Positives

- aucune fausse date de fin;
- `AssetAllocation` reste l'unique autorité physique;
- les réservations bornées existantes restent compatibles;
- révocation et transfert conservent l'historique;
- #762 peut implémenter son UX sans second modèle;
- les Shift héritent du véhicule sans duplication;
- les réparations restent orthogonales à l'attitrage.

### Costs

- les colonnes `end_date` deviennent nullable;
- tous les prédicats d'occupation doivent être audités pour la sémantique `NULL = +∞`;
- certains types/read models TypeScript/Python devront accepter une fin nullable;
- les tests SQL Server deviennent importants pour les chevauchements ouverts;
- les consumers historiques supposant `end_date` non-null doivent être corrigés.

### Risks

Le principal risque est d'oublier un consommateur qui traite `NULL` comme « ne chevauche rien ».

#744 doit donc effectuer un audit exhaustif des comparaisons `AssetAllocation.end_date` et `AssetRequirement.end_date` dans le runtime, tests, communications, projections et outils SQL.

## Acceptance for #744

La livraison de #744 devra prouver au minimum :

1. création d'un `RESOURCE_PERIOD` véhicule avec `end_date = NULL`;
2. blocage d'une réservation concurrente future;
3. blocage d'une attribution ouverte si une réservation future existe;
4. projection sur des Shift futurs sans nouvelle allocation;
5. révocation effective fermant l'intervalle;
6. transfert atomique A → B sans chevauchement ni trou;
7. qualification et autorité fail-closed;
8. conflit CAS entre deux sessions;
9. replay idempotent;
10. lecture d'une période ouverte dans plusieurs fenêtres futures;
11. conservation d'une attribution révoquée dans l'historique;
12. interaction avec une `AssetUnavailability` ajoutée après l'attribution;
13. non-régression des `RESOURCE_PERIOD` bornés historiques;
14. SQL Server + SQLite;
15. React/E2E du parcours ressource;
16. provenance persistante `CONTINUOUS` conservée après révocation et protection contre les chemins génériques release/update;
17. attribution avec fin future encore `ACTIVE` avant sa fin;
18. attribution future ouverte exposée `SCHEDULED` et non active;
19. annulation et replanification d'une attribution future avant prise d'effet;
20. rejet du backdating par les commandes ordinaires;
21. transfert même-véhicule validé contre l'état final proposé sans faux conflit avec l'allocation fermée;
22. échec d'un transfert si une autre réservation ou indisponibilité chevauche, avec rollback atomique complet;
23. lectures d'intervalles ouverts bornées par une fenêtre de requête, sans génération ni expansion de dates jusqu'à l'infini;
24. attribution hors scope qui bloque la disponibilité sans divulguer les détails protégés;
25. readers compatibles nullable déployés avant activation des writes ouverts;
26. downgrade `NOT NULL` refusé tant qu'une attribution ouverte existe, sans suppression ni date sentinelle.

## References

- #55
- #291
- #575
- #615
- #743
- #744
- #762
- #639
- #708
- ADR-006
- ADR-007
- ADR-016
- ADR-018
- ADR-026
- ADR-027

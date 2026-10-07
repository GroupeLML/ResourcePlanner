# ADR-028 — Regroupement des compétences par classe et capacité analytique non additive

Status: Accepted
Date: 2026-10-06

## Context

Le Moyen terme possède déjà une capacité hebdomadaire autoritaire par classe de ressource et un catalogue administrable de compétences attribuées aux ressources humaines.

L'issue #619 demande d'ajouter une lecture plus fine par compétence dans les groupes de classes, sans transformer le rattachement d'une compétence à une classe en règle d'éligibilité. La décision doit aussi éviter deux erreurs de lecture :

- répartir arbitrairement la charge WorkPackage entre des compétences alors que cette ventilation n'existe pas dans le modèle canonique;
- additionner des charges ou capacités de compétences lorsque les mêmes besoins et les mêmes ressources peuvent apparaître dans plusieurs compétences.

Les contrats Moyen terme existants d'ADR-013 et ADR-024 doivent rester distincts du Planning opérationnel, tandis que les règles de disponibilité par classe réelle d'ADR-021 et les politiques de visibilité effectivement livrées au moment de l'implémentation continuent de s'appliquer.

## Decision

### 1. Rattachement facultatif compétence → classe

Ajouter une référence nullable de Competency vers ResourceClassConfig.code.

Cette relation sert uniquement au regroupement et à l'analyse Moyen terme. Elle ne modifie jamais Resource.resource_class, la classe requise d'une demande ou d'un WorkPackage, l'éligibilité d'une ressource, le routage d'approbation ou les permissions.

Les compétences existantes restent sans classe après migration. Une classe inexistante est refusée lors d'un nouveau rattachement. Une classe devenue inactive peut rester référencée par l'historique et doit être signalée plutôt que supprimée implicitement. Les compétences sans classe sont conservées dans un groupe explicite.

### 2. La charge par compétence provient des demandes humaines courantes

La ligne de classe existante conserve sa sémantique actuelle : charge WorkPackage comparée à la capacité de classe.

Une sous-ligne de compétence représente séparément les heures demandées pour cette compétence, selon les règles de projection de demande d'ADR-024 :

- proposition courante enregistrée et non annulée;
- révision remplacée non additionnée;
- périodes détaillées substituées aux heures simples;
- périodes cumulatives additionnées;
- alternatives sélectionnées comptées une seule fois;
- heures d'actifs exclues;
- aucun second multiplicateur de quantité;
- valeurs inconnues conservées comme inconnues.

Les demandes humaines du périmètre sélectionné sont incluses même sans WorkPackage. Les Quick Shifts et autres besoins ad hoc sans demande restent hors de cette mesure.

La charge WorkPackage n'est pas ventilée entre compétences sans donnée métier explicite.

### 3. La capacité par compétence reste une capacité brute selon disponibilités

Pour une compétence et une semaine, la capacité est la somme, une seule fois par ressource, des disponibilités des ressources qui possèdent cette compétence par ID canonique.

Le calcul conserve les règles existantes de disponibilité : activation locale et ERP, horaire applicable, vacances et jours fériés, aucune capacité inventée en l'absence d'horaire, et semaines du lundi au dimanche.

Les Shift et Delivery ne sont pas déduits. Cette valeur est une **capacité théorique selon disponibilités**, pas des heures encore libres.

Les jours fériés sont évalués selon la classe réelle de la ressource conformément à ADR-021, jamais selon la classe de regroupement de la compétence.

### 4. Les exigences multi-compétences sont en logique ET et les lignes ne sont pas additives

Si une ligne de 16 h exige les compétences A **et** B, elle représente un effort unique de 16 h et une exigence de 16 h sur A ainsi que de 16 h sur B.

Il est interdit de diviser arbitrairement les 16 h entre A et B, d'additionner 16 h + 16 h pour annoncer un effort total de 32 h, ou d'additionner les capacités des compétences pour produire une capacité globale mobilisable.

Une même ressource peut contribuer à plusieurs analyses de compétences. Cela ne multiplie pas sa capacité réellement mobilisable. Le read model doit donc exposer explicitement que ces lignes sont non additives.

Pour les combinaisons réellement demandées, la projection peut calculer un diagnostic de **qualification commune** : capacité des ressources possédant toutes les compétences exigées par la ligne, et respectant sa classe requise lorsqu'elle existe. La classe de regroupement du catalogue ne participe pas à ce test.

Ce diagnostic ne constitue pas une preuve de faisabilité ni un solveur d'affectation : plusieurs besoins peuvent se disputer les mêmes ressources.

### 5. Projection hebdomadaire et alternatives

La projection des demandes conserve les règles de demand_periods et ne copie pas la répartition WorkPackage d'ADR-019.

Une alternative non résolue pouvant tomber dans plusieurs semaines peut apparaître dans chacune comme enveloppe prudente, avec diagnostic approprié. Le total de période est toujours recalculé depuis les alternatives sources et ne provient jamais de la somme naïve des cellules hebdomadaires.

Le mode Budget initial / Budget restant et son cutoff ERP ne modifient ni les heures demandées par compétence ni cette capacité.

### 6. Read model, filtres et sécurité

La projection Moyen terme reçoit une collection dédiée aux compétences exposant au minimum identité et nom de compétence, activation, classe de regroupement et son activation, semaine, heures demandées, capacité, utilisation/état/diagnostics, source de charge, base de capacité et indicateurs de couverture/non-additivité.

Le sens de work_package_hours et de la capacité de classe existante reste inchangé.

Le filtrage doit distinguer la classe métier de la charge WorkPackage et la classe de regroupement des compétences. Une compétence classée dans une classe de regroupement peut être exigée par une demande d'une autre classe métier.

Les agrégations utilisent les IDs canoniques, évitent les jointures multiplicatives et les appels par compétence × ressource × jour. Les références historiques non résolues sont diagnostiquées et ne sont jamais assimilées à l'absence d'exigence.

Les règles de visibilité et d'autorisation effectivement livrées au moment de l'implémentation sont appliquées avant publication des agrégats et diagnostics. Cette évolution ne doit pas créer une nouvelle surface d'exposition de détails individuels.

## Alternatives considered

### Répartir la charge WorkPackage entre compétences

Rejeté pour #619. Le WorkPackage ne possède pas de ventilation canonique par compétence; toute répartition serait une nouvelle saisie métier ou une approximation arbitraire.

### Calculer les heures encore libres après Shift / Delivery

Rejeté dans cette tranche. Cela demanderait de réconcilier plusieurs représentations de charge et créerait un risque de double déduction. La capacité brute Moyen terme reste l'autorité de comparaison.

### Relation plusieurs-à-plusieurs entre compétences et classes

Rejetée. Le besoin demandé est un regroupement facultatif unique par compétence; une relation M:N augmenterait les apparitions et l'ambiguïté sans valeur métier établie.

### Additionner les lignes de compétences

Rejeté. Les mêmes besoins et les mêmes ressources peuvent légitimement apparaître dans plusieurs compétences. Une somme donnerait une capacité ou une charge fictive.

## Consequences

### Positive

- le Moyen terme gagne une lecture de tension par compétence sans changer l'éligibilité Planning;
- les contrats WorkPackage et demandes restent séparés et compréhensibles;
- les ressources polyvalentes et besoins multi-compétences sont représentés sans double comptage déclaré;
- les diagnostics peuvent identifier un manque de qualification commune sans prétendre résoudre l'affectation;
- les évolutions restent additives au modèle et au read model existants.

### Trade-offs / negative

- les sous-lignes de compétences ne peuvent pas être additionnées pour retrouver la ligne de classe;
- une capacité suffisante sur chaque compétence séparément ne prouve pas qu'une affectation réalisable existe;
- les Quick Shifts et besoins ad hoc sans demande ne sont pas couverts par la mesure « heures demandées »;
- les alternatives non résolues peuvent apparaître comme enveloppes prudentes sur plusieurs semaines;
- le calcul nécessite une projection commune des demandes qui ne peut pas se limiter aux WorkPackages sélectionnés.

## Implementation notes

Le découpage de #619 reste séquentiel :

1. **619A** — migration additive, FK nullable, contrats d'administration, concurrence/audit du rattachement et sélection de classe dans le catalogue;
2. **619B** — projection commune demandes/périodes, compétences canoniques, capacité brute, diagnostics multi-compétences, filtres et tests;
3. **619C** — groupes de classes repliés par défaut, sous-lignes de compétences, libellés distinguant charge WorkPackage et heures demandées, avertissements de non-additivité et acceptation navigateur.

La migration et les contrats de concurrence doivent être validés avec SQL Server conformément à ADR-011.

## References

- GitHub Issue #619
- GitHub Roadmap #55
- ADR-011 — SQL Server authoritative database
- ADR-013 — WorkPackage weekly load and medium-term boundaries
- ADR-019 — WorkPackage load intervals and derived lifecycle
- ADR-021 — Class-scoped holiday availability rules
- ADR-024 — Medium-term WorkPackage cycle, demand projection and ERP cutoff
- ADR-027 — Planning visibility role and authorized scope
- #272 — catalogue de compétences
- #537 — capacité Moyen terme par classe
- #556 — UX Moyen terme compacte

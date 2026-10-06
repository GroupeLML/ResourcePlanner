# ADR-024 — Moyen terme : cycle WorkPackage réversible, demandes projetées et budget restant après cutoff ERP

Status: Accepted  
Date: 2026-10-05

## Context

#614 regroupe plusieurs évolutions du Moyen terme après #537, #539, #556 et #591 :

- filtrer une même tâche ERP transversalement à plusieurs projets;
- réouvrir ou annuler explicitement un WorkPackage terminal;
- créer une demande à partir d'un WorkPackage sans créer un parcours parallèle;
- projeter les demandes liées dans le Gantt par ligne et par période;
- comparer les heures demandées aux heures prévues du WorkPackage;
- rendre la bascule Budget initial / Budget restant cohérente en heures et avec la charge WorkPackage pertinente.

Les contrats existants séparent déjà le budget ERP de tâche, la charge WorkPackage, les Shift Planning et Delivery. ADR-019 a aussi rendu la répartition WorkPackage facultative, dérive le solde automatique sur toute la période et projette le statut depuis le cycle de vie.

L'analyse ASTRA-614 sur `main@73af3b711537b7970c27fc38798a91dcfafc12c4` confirme qu'aucune refonte de Planning, Delivery ou du modèle WorkPackage n'est nécessaire. Les changements restent additifs, mais exigent de préciser le cycle terminal, la lecture des demandes et la provenance du cutoff ERP.

La décision humaine accepte les trois arbitrages structurants proposés par l'analyse :

1. réouverture sans propagation et annulation explicite d'un WorkPackage clôturé;
2. heures demandées = proposition courante enregistrée, sans addition avec l'ancienne approbation remplacée;
3. cutoff ERP par tâche à la date métier incluse, après projection journalière complète de la charge.

## Decision

### 1. Le filtre transversal de tâche regroupe par TaskCD sans changer les identités

Le filtre Moyen terme peut sélectionner un `TaskCD` transversal, par exemple `216 - Programmation`, dans le périmètre des projets visibles.

Cette sélection est une lecture et un regroupement. Elle ne remplace jamais l'identité persistante `TaskCatalogEntry.id`.

Pour un même `TaskCD` :

- chaque tâche reste sous son propre projet;
- chaque budget reste celui de sa `TaskCatalogEntry`;
- chaque rattachement WorkPackage reste par ID stable;
- les restrictions de visibilité projet continuent de s'appliquer;
- le libellé ne détermine pas l'appartenance au groupe;
- le code est traité comme texte afin de préserver d'éventuels zéros initiaux.

Le backend expose un critère distinct de filtre par code. Si un filtre par ID et un filtre par code sont fournis ensemble, la sémantique retenue est l'intersection.

Ce filtre ne crée aucune capacité workforce dédiée aux projets sélectionnés.

### 2. Le cycle WorkPackage devient explicitement réversible

Les actions métier autorisées sont :

| État courant | Action | Résultat |
|---|---|---|
| `planned` / `active` | Clôturer | `closed` |
| `planned` / `active` | Annuler | `cancelled` |
| `closed` | Réouvrir | état ouvert dérivé des dates |
| `closed` | Annuler | `cancelled` |
| `cancelled` | Réouvrir | état ouvert dérivé des dates |
| `cancelled` | Clôturer directement | refus |

Une réouverture ne choisit pas librement `planned` ou `active`. Elle retire le fait terminal puis réapplique la politique dérivée d'ADR-019 :

- début atteint → `active`;
- début futur ou absent → `planned`.

Une fin dépassée ne reclôt pas automatiquement le WorkPackage.

La réouverture doit neutraliser dans la même transaction les deux sources terminales historiques utilisées par la projection actuelle, afin qu'un ancien `status=closed/cancelled` ne réapparaisse pas par fallback après retrait de `terminal_status`.

Les anciennes transitions restent conservées dans l'audit.

Chaque mutation utilise :

- la permission backend `manage_work_packages`;
- `expected_version` et CAS WorkPackage;
- une validation de transition après acquisition de la garde;
- un audit de l'ancien état, du nouvel état, de l'acteur et de l'action;
- une idempotence dont l'identité inclut l'action, le WorkPackage et ses paramètres.

Ces actions ne modifient pas `planning_version`, `delivery_version` ou `verification_version`.

### 3. Réouvrir ou annuler un WorkPackage ne propage rien vers les autres workflows

Réouvrir un WorkPackage :

- ne réouvre aucune demande;
- ne réactive aucun besoin;
- ne recrée aucun Shift;
- ne modifie aucun état Delivery ou Verification.

Annuler un WorkPackage ne supprime pas davantage les engagements liés.

L'interface peut signaler les engagements existants, mais leur résolution reste sous l'autorité de leurs workflows respectifs.

`closed → cancelled` est autorisé sous la même autorité métier et reste audité. L'utilisateur doit être averti que cette action retire le WorkPackage de la structuration budgétaire.

Règles de projection budgétaire :

| État | Budget initial structuré | Charge courante / après cutoff |
|---|---:|---:|
| ouvert | incluse | incluse |
| clôturé | incluse | exclue |
| annulé | exclue | exclue |

### 4. Créer une demande depuis un WorkPackage réutilise le parcours canonique

L'action « Créer une demande » depuis un WorkPackage ouvre le parcours canonique de création avec un contexte initial contenant :

- le projet;
- la référence stable du WorkPackage;
- la tâche seulement lorsqu'elle est déterministe par les invariants existants;
- éventuellement les dates et la classe comme valeurs initiales.

Le backend conserve les invariants actuels :

- WorkPackage classé seul → tâche dérivée;
- WorkPackage et même tâche → accepté;
- WorkPackage et tâche contradictoire → rejeté;
- WorkPackage historique non classé → aucune tâche inventée.

L'utilisateur complète ensuite les dates, heures et besoins; la demande est créée par la commande canonique puis le détail canonique est ouvert.

Aucune demande vide n'est créée au simple affichage du formulaire.

`planned_hours` du WorkPackage ne devient pas automatiquement le budget de la nouvelle demande.

Cette action est disponible pour un WorkPackage ouvert. Si cette condition devient une règle générale, elle est imposée côté serveur sous la garde WorkPackage et pas seulement masquée dans React.

### 5. Les demandes liées au Gantt reposent sur une projection backend par ligne et période

Le read model Moyen terme ajoute une projection dédiée commune à l'affichage Gantt et au calcul des heures demandées.

Elle porte au minimum :

- identité de demande;
- identité de ligne;
- identité de période;
- WorkPackage concerné;
- dates demandées;
- heures;
- provenance candidate / approuvée;
- diagnostics.

Les demandes simples sont normalisées en mémoire selon ADR-002.

La projection doit couvrir correctement :

- une demande répartie entre plusieurs WorkPackages;
- des périodes cumulatives;
- des alternatives;
- une modification candidate changeant de WorkPackage alors que l'ancien plan demeure actif;
- les lignes humaines et les lignes d'actifs.

Pour le Gantt :

- chaque WorkPackage peut afficher une sous-ligne de demande distincte;
- chaque période pertinente est dessinée séparément sans remplir artificiellement les trous;
- les alternatives restent identifiables;
- les dates demandées restent distinctes des dates de Shift et des fenêtres opérationnelles dérogatoires;
- le clic ouvre le détail canonique de la demande.

Le diagnostic « hors WorkPackage » est calculé côté backend sur les dates complètes avant découpage visuel à l'horizon. Il distingue dépassement avant, après ou des deux côtés. Des dates WorkPackage absentes rendent la comparaison indisponible et non conforme par défaut.

### 6. Les heures demandées représentent la proposition courante enregistrée

Pour un WorkPackage, « heures demandées » signifie :

> total de la proposition courante enregistrée, non annulée, rattachée au WorkPackage, avec les brouillons explicitement identifiés.

Une proposition modifiée remplace l'ancienne proposition dans ce total. Une demande de 60 h approuvée puis modifiée à 80 h donne donc 80 h demandées, jamais 140 h.

Les heures approuvées peuvent être exposées séparément lorsque utile, mais ne sont pas additionnées au total demandé.

Les règles de non-double-comptage existantes restent autoritaires :

- périodes cumulatives additionnées;
- alternative sélectionnée comptée une seule fois;
- groupe alternatif non sélectionné → maximum des options avec diagnostic de choix non résolu;
- heures simples non ajoutées aux périodes détaillées;
- aucun facteur supplémentaire `resource_count` lorsque les heures représentent déjà l'effort total;
- heures d'actifs exclues de l'indicateur workforce.

Le ratio `heures demandées / heures prévues` compare :

- numérateur : total demandé selon cette convention;
- dénominateur : `WorkPackage.planned_hours`.

Il est indépendant de l'horizon visible et du mode budgétaire ERP. Une valeur inconnue reste `null` avec diagnostic et n'est jamais convertie en zéro.

### 7. La bascule Budget initial / Budget restant sélectionne un ensemble cohérent

Moyen terme expose deux lectures cohérentes :

| Mode | Budget | Charge WorkPackage soustraite | Solde |
|---|---|---|---|
| Initial | `budget_hours` canonique | total des WorkPackages budgétairement inclus | budget initial − charge totale |
| Restant | `(BudgetAmount - BudgetActual) / coût horaire canonique` | charge des WorkPackages ouverts à compter du cutoff de la tâche | budget restant − charge après cutoff |

Le coût canonique est celui projeté sur la tâche ERP. Il n'est pas remplacé par la classe du WorkPackage ni par le coût des ressources réellement affectées.

Les montants CAD restent la source financière et sont conservés comme éléments d'explication. Une conversion impossible reste indisponible. Un budget restant négatif reste négatif.

La bascule choisit ensemble le budget, la charge, le solde et les diagnostics correspondants. Elle ne modifie pas le calcul de capacité hebdomadaire et ne redistribue pas les barres du Gantt.

### 8. Le cutoff ERP est spécifique à la tâche, inclusif et journalier

Pour chaque tâche ERP `T` :

```text
cutoff(T) = date métier de son dernier rafraîchissement ERP réussi

charge_après_cutoff(T)
  = somme des contributions journalières des WorkPackages ouverts liés à T
    pour les dates >= cutoff(T)
```

Le jour du cutoff est inclus.

La date métier est dérivée côté serveur dans un fuseau explicite. Pour le déploiement québécois, le fuseau retenu est `America/Toronto`; l'application ne dépend pas implicitement du fuseau du conteneur.

La précision est journalière. Aucun prorata intrajournalier n'est inventé depuis l'heure de synchronisation.

### 9. La charge est projetée sur tous ses jours avant application du cutoff

La primitive journalière d'ADR-019 devient la base commune des lectures hebdomadaire et après cutoff.

L'ordre est obligatoire :

1. répartir chaque intervalle explicite sur ses jours calendaires;
2. répartir le solde automatique sur toute la période WorkPackage;
3. attribuer les centièmes résiduels de façon déterministe;
4. additionner les contributions journalières;
5. conserver les jours dont la date est supérieure ou égale au cutoff.

Il est interdit de tronquer d'abord un intervalle puis de redistribuer toutes ses heures sur la portion restante.

Un cutoff en milieu de semaine ne retire ni ne conserve arbitrairement toute la semaine.

### 10. La fraîcheur budgétaire ERP doit être persistée par tâche

Le timestamp de succès au niveau projet ne prouve pas qu'une tâche donnée a été effectivement reçue et intégrée.

Chaque `TaskCatalogEntry` concernée par le budget ERP reçoit donc une provenance nullable, par exemple `erp_budget_last_success_at`.

Cette valeur est mise à jour :

- lorsque la tâche a réellement été reçue et intégrée;
- même si ses valeurs reçues sont identiques aux précédentes;
- dans la même transaction que les valeurs budgétaires concernées.

Elle n'est jamais avancée par :

- une édition locale;
- un recalcul de classification;
- un import de secours;
- une tentative ERP échouée;
- un run où la tâche était absente ou rejetée.

Les anciennes lignes ne reçoivent aucun cutoff rétroactif sans preuve. Elles restent sans lecture « budget restant après cutoff » jusqu'à un rafraîchissement réussi de la tâche.

Les métadonnées de synchronisation projet restent utiles pour suivre le run global.

La date de synchronisation n'est pas présentée comme une preuve que `BudgetActual` couvre comptablement toute consommation jusqu'à cette date. C'est une convention produit de référence, pas une garantie ERP sur la période comptable.

### 11. Les nouveaux contrats ne réinterprètent pas silencieusement les champs existants

Pendant l'introduction du nouveau read model :

- les champs existants de #539 conservent leur signification;
- la lecture après synchronisation utilise des champs distincts;
- elle expose sa `reference_basis`, son cutoff et ses diagnostics par tâche;
- un champ `reference_week_start` ne doit pas être réutilisé pour une date pouvant tomber un mercredi.

ADR-024 supersède uniquement les parties d'ADR-013 et d'ADR-019 incompatibles avec :

- le nouveau cycle terminal explicitement réversible;
- la lecture Budget restant après cutoff journalier spécifique à la tâche.

Leurs autres frontières restent applicables.

### 12. Concurrence, migrations et performance

La persistance du timestamp par tâche est additive.

Les nouvelles transitions WorkPackage, gardes de dépendances et migrations sensibles à la concurrence doivent être validées sur SQL Server conformément à ADR-011.

La projection Moyen terme charge les demandes, lignes, périodes, tâches et WorkPackages en lots. Elle ne doit pas appeler un endpoint de détail une fois par demande ou WorkPackage.

## Alternatives considered

### Utiliser TaskCD comme nouvelle identité persistante

Rejeté. Un même code existe sur plusieurs projets et ne remplace pas l'identité stable de `TaskCatalogEntry`.

### Garder les WorkPackages terminaux irréversibles

Rejeté. Le besoin produit exige la correction explicite d'une clôture ou annulation, sans réécriture libre du statut et sans propagation destructive.

### Réouvrir automatiquement les demandes et Shift liés

Rejeté. Ces objets appartiennent à d'autres workflows et possèdent leurs propres autorités, audits et règles de concurrence.

### Compter l'ancienne approbation et la nouvelle proposition ensemble

Rejeté pour le libellé « heures demandées ». Cette somme mesure des engagements ou états historiques différents et double compterait une révision 60 h → 80 h.

### Utiliser la dernière synchronisation du projet comme cutoff de toutes les tâches

Rejeté. Une tâche absente ou rejetée du résultat peut conserver un ancien budget alors que le run projet a réussi.

### Utiliser le lundi de la semaine courante comme référence Budget restant

Supersédé pour cette nouvelle lecture. Le besoin produit exige une référence attachée à la dernière synchronisation réussie de chaque tâche ERP.

### Couper un intervalle avant d'en redistribuer les heures

Rejeté. Cela modifierait la densité de charge et produirait un résultat différent de la projection journalière déterministe d'ADR-019.

## Consequences

### Positive

- le filtre tâche devient transversal sans perdre l'identité projet/tâche;
- le cycle WorkPackage peut être corrigé explicitement et audité;
- aucune réouverture ne déclenche de propagation destructive;
- création et consultation de demandes réutilisent les parcours canoniques;
- le Gantt et les heures demandées partagent une projection backend commune;
- le total demandé ne double compte pas les remplacements et alternatives;
- Budget initial et Budget restant deviennent chacun cohérents entre budget, charge et solde;
- le cutoff est vérifiable par tâche et précis au jour;
- les intervalles chevauchant le cutoff conservent la même formule journalière qu'ADR-019;
- les données historiques sans preuve de fraîcheur ne reçoivent pas de date inventée.

### Trade-offs / negative

- une nouvelle provenance ERP persistante par tâche est nécessaire;
- le read model Moyen terme devient plus riche;
- les transitions terminales exigent une migration comportementale prudente autour du fallback historique de statut;
- le fuseau métier doit devenir une configuration explicite côté serveur;
- certaines anciennes tâches afficheront temporairement un budget restant indisponible jusqu'à une synchronisation réussie;
- les projections de demandes doivent traiter lignes, périodes et alternatives plutôt qu'un simple regroupement par en-tête;
- les cas de concurrence importants doivent être exercés sur SQL Server.

## Implementation notes

Découpage accepté de #614 :

```text
614A — filtre tâche ERP transversal aux projets
614B — réouvrir ou annuler WorkPackage terminal
614C — créer demande depuis WorkPackage
614D — afficher demandes en Gantt et alerte hors WorkPackage
614E — heures demandées sur heures prévues WorkPackage
614F — budget restant en heures et solde depuis dernière sync ERP
```

L'ordre reste séquentiel dans la lane PARALLEL : après acceptation d'ASTRA-614, `614A` devient la première tranche DEV autorisable; `614B` à `614F` restent bloquées jusqu'à leur prédécesseur.

ASTRA-619 peut devenir READY parce que sa dépendance explicite « ASTRA-614 DONE » est maintenant satisfaite. Comme toute gate d'architecture, son exécution reste soumise à une autorisation humaine explicite.

La matérialisation de cette ADR est documentaire et ne constitue pas le démarrage de 614A.

## References

- #55 — roadmap maître
- #614 — Moyen terme : tâches ERP transverses, cycle WorkPackage, demandes Gantt et budget en heures
- #539 — référence budget ERP restant et charge WorkPackage future
- #556 — contrats Moyen terme liés au budget et aux WorkPackages
- #591 — répartition facultative WorkPackage et cycle dérivé
- ADR-002 — périodes métier par RequestLine
- ADR-011 — SQL Server autoritaire
- ADR-013 — WorkPackage, tâche ERP, budget et frontières Moyen terme
- ADR-015 — classe canonique WorkPackage distincte de la tâche ERP
- ADR-019 — intervalles WorkPackage et cycle de vie dérivé
- Analyse ASTRA-614, acceptée par décision humaine le 2026-10-05

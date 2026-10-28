# ADR-034 — Retours vers la File de Travail et réduction durable du besoin après approbation

Status: Accepted
Date: 2026-10-28
Accepted: 2026-10-28
WorkItem: ASTRA-778

Le Product Owner accepte la décision d'architecture ASTRA-778 issue de la révision produit du 2026-10-28. Cette ADR formalise trois commandes métier distinctes pour les `Shift` issus d'un besoin `REQUEST` : retourner un quart à la File de Travail, retourner tout le segment à la File de Travail, et supprimer définitivement les heures d'un quart lorsque le travail n'est plus requis.

Cette décision complète ADR-001, ADR-003, ADR-004, ADR-006, ADR-016, ADR-018, ADR-020, ADR-026, ADR-027, ADR-029, ADR-030 et ADR-031. Elle ne remplace pas le contrat QUICK_SHIFT de #755 / ADR-031.

## Context

Le Planning opérationnel matérialise une autorisation approuvée en `ResourceRequirement`, puis en `Shift`. La File de Travail de #693 expose le reliquat non couvert à partir du besoin courant et des vrais Shift comptés.

La coordonnatrice doit pouvoir distinguer trois intentions qui étaient auparavant confondues sous le terme « supprimer » :

1. retirer un seul Shift d'une ressource et rendre ses heures à planifier;
2. retirer toute l'affectation opérationnelle d'un segment et rendre tout le segment à planifier;
3. retirer définitivement les heures d'un Shift parce que le travail a été complété en moins d'heures que prévu.

Les mécanismes existants ne suffisent pas :

- `delete_manual()` peut supprimer puis reconstruire un besoin REQUEST, ce qui permet au moteur de recréer le travail;
- une exclusion de date seule n'empêche pas le moteur de replacer le reliquat sur un autre jour;
- `assigned_resource_id = NULL` seul ne suffit pas, car une rematérialisation peut rétablir une cible proposée;
- `sync_operational_choices()` repart de l'autorisation approuvée et peut réécrire `planned_hours`;
- les overrides de budget existants opèrent au niveau de l'entrée approuvée et ne garantissent pas une réduction ciblée d'un slot;
- une simple mutation de `RequestLine.estimated_hours` est une modification candidate susceptible d'exiger réapprobation et ne remplace pas l'autorité approuvée active.

L'architecture doit donc séparer durablement :

- l'autorisation approuvée historique;
- l'intention opérationnelle de génération/placement d'un slot;
- les décisions de retour vers la File;
- les réductions définitives post-approbation.

## Decision

### 1. Trois commandes métier distinctes

Les commandes canoniques sont conceptuellement :

```text
return_shift_to_work_queue(
  shift_id,
  expected_planning_version,
  expected_active_approval_revision_id,
  expected_operational_version,
  reason = RETURN_SHIFT_TO_WORK_QUEUE
)

return_segment_to_work_queue(
  resource_requirement_id,
  expected_planning_version,
  expected_active_approval_revision_id,
  expected_operational_version
)

reduce_completed_requirement_hours(
  shift_id,
  expected_planning_version,
  expected_request_version,
  expected_active_approval_revision_id,
  expected_operational_version,
  reason = LESS_WORK_REQUIRED,
  comment?
)
```

Les URLs HTTP sont des adaptateurs de ces commandes. Elles ne sont pas trois variantes d'un DELETE générique.

Les trois commandes exigent une `Idempotency-Key`.

Le client ne fournit jamais comme autorité :

- le delta d'heures;
- la source demande/ligne;
- la quantité courante;
- la révision à modifier.

Le serveur relit les entités canoniques avant mutation.

### 2. Sémantique quantitative

On définit :

```text
H = besoin courant effectif du slot
C = couverture réelle comptée depuis les vrais Shift
R = max(H - C, 0)
h = heures exactes du Shift ciblé
```

| Action | Shift ciblé | Autres Shift | Besoin H | File de Travail |
|---|---|---|---:|---:|
| Retourner ce quart | supprimé | conservés | inchangé | le reliquat augmente de h |
| Retourner le segment | tous les Shift du requirement supprimés | aucun conservé dans ce requirement | inchangé | H au total |
| Supprimer définitivement | supprimé | conservés s'ils restent valides | H - h | aucun nouveau reliquat créé |

La suppression définitive ne signifie pas que la File devient nécessairement vide. Un reliquat antérieur reste un reliquat.

Exemples :

```text
H=40, C=40, R=0; suppression définitive h=8
=> H=32, C=32, R=0

H=40, C=32, R=8; suppression définitive d'un Shift couvert de 8 h
=> H=32, C=24, R=8
```

Les commandes visant un quart exigent un vrai `Shift` contribuant à la couverture, pas une ligne diagnostique.

Une surcouverture préalable bloque retour d'un quart et réduction définitive jusqu'à régularisation explicite. Le retour du segment entier peut retirer toute sa couverture sans modifier H.

### 3. Identité stable du slot approuvé

Introduire une autorité persistante de type `RequestRequirementPlanningIntent` représentant l'identité stable d'un slot approuvé.

Elle lie au minimum :

- demande;
- ligne / entrée approuvée;
- révision source;
- `period_key` logique éventuel;
- slot de quantité;
- requirement courant.

Cette identité survit :

- rematérialisation;
- resync;
- réduction à zéro;
- remplacement du `ResourceRequirement` SQL.

`approved_entry_key` seul ne suffit pas lorsque plusieurs slots proviennent de la même entrée. L'appariement ne doit jamais dépendre de l'ordre de création, du nom, de la ressource ou du libellé.

Réutiliser la version CAS de `RequestOperationalState`. Ne pas créer une deuxième horloge de concurrence opérationnelle.

### 4. Modes d'intention opérationnelle

Chaque slot porte un mode :

```text
AUTO
PRESERVE_EXISTING
QUEUED
```

et une cible distinguant explicitement :

```text
INHERIT
EXPLICIT_RESOURCE(resource_id)
EXPLICIT_NONE
```

Pour les besoins REQUEST couverts par cette ADR, `assigned_resource_id` devient une projection synchronisée de cette intention, pas l'autorité unique.

#### AUTO

Comportement normal de génération automatique autorisé.

#### PRESERVE_EXISTING

Utilisé après « Retourner ce quart à la File de Travail ».

Les Shift survivants du requirement sont conservés tels quels comme vraie couverture :

- mêmes identités;
- mêmes dates;
- mêmes heures;
- mêmes ressources;
- mêmes sources;
- mêmes états locked/manual;
- mêmes confirmations.

Le moteur ne génère spontanément aucun reliquat de ce slot :

- ni placement normal;
- ni overtime;
- ni fallback;
- ni ligne automatique de manque.

Les survivants ne sont pas convertis secrètement en `MANUAL` ou `locked=True`.

Le reliquat reste néanmoins explicitement disponible pour la File de Travail et un placement contrôlé.

#### QUEUED

Utilisé après « Retourner le segment à la File de Travail ».

Tous les Shift opérationnels du `ResourceRequirement`, toutes ressources confondues, sont retirés atomiquement. La cible devient `EXPLICIT_NONE`.

Le requirement conserve :

- heures effectives;
- projet;
- fenêtre;
- override de fenêtre;
- classe;
- compétences;
- priorité;
- confirmation;
- provenance approuvée.

Le moteur ne rétablit pas automatiquement l'ancienne ressource, une ressource proposée, une préférence TaskCatalog ou une propagation SAME_AS_PERIOD.

### 5. PlanningWorkQueueRelease

Introduire un événement persistant de type `PlanningWorkQueueRelease` pour « Retourner ce quart ».

Il conserve au minimum :

- slot stable;
- requirement;
- demande;
- révision / entrée approuvée;
- ressource;
- date du Shift;
- heures;
- snapshot du Shift supprimé;
- motif `RETURN_SHIFT_TO_WORK_QUEUE`;
- AppUser stable;
- corrélation;
- statut actif/résolu;
- timestamps/audit.

Le `shift_id` historique ne doit pas imposer une FK empêchant la suppression physique du Shift.

L'exclusion de génération se déduit des libérations actives sur :

```text
requirement + resource + date
```

Elle n'interdit :

- ni la date à toutes les ressources;
- ni toute la disponibilité de cette ressource.

Plusieurs fragments libérés le même jour doivent pouvoir coexister. Les heures de l'événement sont une preuve historique, pas un deuxième solde planifiable.

### 6. Retourner un quart

La commande :

1. relit et valide le Shift réel;
2. crée la libération persistante;
3. passe le slot en `PRESERVE_EXISTING`;
4. nettoie les actifs possédés par le Shift selon ADR-016/018/026;
5. supprime exactement le Shift ciblé;
6. reconstruit une seule fois en conservant les survivants;
7. projette H/C/R;
8. audite et enregistre le reçu idempotent;
9. commit atomiquement.

Le besoin courant H ne change pas.

Le moteur ne peut ni recréer ce Shift sur la même ressource/date ni déplacer automatiquement h sur une autre journée.

Un placement explicite ultérieur peut consommer uniquement R et doit préserver les survivants.

Un placement explicite sur la même ressource/date doit reconnaître la libération concernée; une génération automatique ne peut la contourner.

### 7. Restauration d'un retour de quart

Prévoir une commande explicite :

```text
restore_shift_return(release_id, versions...)
```

Elle relit :

- besoin courant;
- reliquat;
- capacité;
- droits;
- fenêtre;
- SAME_AS_PERIOD.

Elle peut recréer les heures avec une nouvelle identité de Shift et une référence d'audit seulement si le reliquat disponible le permet.

Si une autre ressource couvre déjà ces heures, la restauration échoue sans retirer ce travail.

Résoudre une libération n'efface pas les autres libérations actives du même requirement/date.

### 8. Retourner le segment

La commande :

1. relit le requirement et tous ses Shift;
2. valide le scope sur tous les objets impactés;
3. passe le slot en `QUEUED` et cible `EXPLICIT_NONE`;
4. nettoie les actifs possédés;
5. retire tous les Shift opérationnels et sorties diagnostiques du requirement;
6. reconstruit sans régénération automatique;
7. projette H/C/R;
8. audite l'ancienne cible et les Shift retirés;
9. écrit le reçu idempotent;
10. commit atomiquement.

Le retour du segment vise le `ResourceRequirement` entier, même s'il est réparti sur plusieurs ressources.

Il ne crée pas une interdiction permanente de l'ancienne ressource.

Segment déjà `QUEUED`, sans Shift, avec versions fraîches : no-op explicite.

### 9. Réduction définitive post-approbation

Introduire un registre append-only de type `RequestCompletionReduction`.

Chaque événement conserve au minimum :

- demande;
- révision d'autorisation source;
- ligne moderne si présente;
- entrée approuvée;
- slot stable;
- requirement;
- snapshot du Shift;
- quantité décimale positive exacte;
- besoin avant/après;
- motif `LESS_WORK_REQUIRED`;
- AppUser stable;
- date;
- corrélation;
- identité de commande/idempotence;
- état de reprise/absorption éventuelle.

Une suppression ne produit qu'une réduction.

Les événements historiques ne sont jamais effacés.

La réduction n'est :

- ni une nouvelle approbation implicite;
- ni une mutation de l'ancienne `RequestApprovalRevision`;
- ni une simple édition candidate;
- ni une feuille de temps;
- ni une écriture ERP;
- ni une modification WorkPackage.

### 10. Résolveur partagé du besoin effectif

Introduire une règle canonique unique :

```text
B_entry = budget nominal approuvé ou override opérationnel légitime existant
B_slot  = répartition canonique de B_entry entre slots
D_slot  = réductions applicables au slot, chacune comptée exactement une fois
H_slot  = B_slot - D_slot, avec H_slot >= 0
```

La réduction est appliquée après la répartition nominale.

Exemple :

```text
80 h approuvées
=> slots nominaux 40 / 40
=> réduction 8 h sur le premier slot
=> besoin courant 32 / 40
```

Elle ne devient jamais 36 / 36.

Ce résolveur doit être utilisé par :

- préparation approuvée;
- matérialisation;
- `sync_operational_choices()`;
- moteur Planning;
- validations de budget/override;
- read models demande/ligne/segment;
- deltas;
- File de Travail;
- recommandations.

`ResourceRequirement.planned_hours` est une projection positive de H, pas une autorité concurrente.

Aucune branche de resync ne peut reprendre directement la quantité approuvée brute comme besoin courant.

### 11. Réduction d'un Shift

Le serveur déduit h du Shift relu.

Avant commit, il vérifie :

- rattachement exact au slot;
- H courant;
- survivants;
- absence de surcouverture incompatible;
- versions;
- droits;
- actifs;
- topologie SAME;
- source approuvée.

Les survivants doivent tenir dans `H - h`. Sinon refus atomique; la commande ne supprime pas d'autres Shift pour faire rentrer la réduction.

La réduction ne change pas à elle seule le mode de génération :

- `AUTO` reste AUTO;
- `PRESERVE_EXISTING` reste protégé;
- `QUEUED` reste en attente.

Les heures supprimées ne peuvent revenir puisque H est réduit par le résolveur.

### 12. Candidat, besoin courant et affichage

Ne pas écraser aveuglément `RequestLine.estimated_hours`.

Les API et écrans doivent distinguer :

- autorisation historique nominale;
- réduction post-approbation;
- besoin courant autoritaire;
- proposition candidate éventuelle.

Exemple :

```text
autorisation historique = 40 h
réduction = 8 h
besoin courant = 32 h
candidat éventuel = valeur distincte
```

Les écrans de besoin courant doivent réellement afficher 32 h.

### 13. Réapprobation et absorption

Par défaut, une réapprobation compatible conserve :

- les réductions;
- les intentions de retour;
- le slot stable.

Une nouvelle autorisation nominale inchangée à 40 h garde donc H=32 h.

Chaque nouvelle autorisation doit capturer une réconciliation versionnée :

- correspondance des slots;
- réductions reprises;
- borne des événements déjà absorbés.

Une absorption explicite peut transformer 32 h en nouvelle base nominale et marquer les 8 h historiques comme déjà absorbées, donnant toujours 32 h.

Cette absorption :

- appartient au workflow normal d'approbation;
- exige le quorum habituel;
- n'est jamais déclenchée par le bouton de suppression;
- n'est jamais implicite.

Si le candidat propose 32 h alors qu'une réduction 8 h reste active, l'impact effectif 24 h doit être affiché explicitement avant approbation.

Changement incompatible de topologie, quantité de slots, type, projet ou identité logique : imposer une réconciliation explicite, par exemple `reduction_rebase_required`.

Aucune migration heuristique de réduction vers une nouvelle entrée.

Une réduction n'approuve aucun changement candidat en attente. Elle incrémente la version demande et la borne de réduction. Un cycle d'approbation devenu obsolète doit être invalidé/rafraîchi selon le workflow existant.

### 14. File de Travail avec candidature pendante

La File doit pouvoir identifier le reliquat du plan actuellement autorisé même si une modification candidate est soumise.

Adapter les lectures pour distinguer :

- autorisation courante;
- candidat;
- capacité de placement.

Afficher le reliquat autorisé ne donne jamais le droit d'activer ou planifier la proposition non approuvée.

### 15. Versions, CAS et idempotence

Ordre de gardes lorsqu'elles sont requises :

```text
Planning CAS
→ demande CAS si réduction
→ état opérationnel CAS
→ relecture décisionnelle
→ mutation
```

Les retours opérationnels ne nécessitent pas un CAS candidat pour une modification descriptive concurrente, mais contrôlent :

- Planning CAS;
- révision active;
- version opérationnelle.

La réduction contrôle en plus la version de demande.

Réutiliser l'infrastructure de reçus idempotents avec un scope distinct par commande et une identité AppUser stable.

Même clé + même empreinte :

- retourner le résultat initial;
- ne pas supprimer/réduire une seconde fois;
- fonctionner même si le Shift a depuis disparu.

Même clé + empreinte différente : conflit.

Une nouvelle clé sur une cible disparue n'est pas un succès implicite.

Version périmée : 409, sans retry automatique d'une nouvelle intention.

### 16. Autorisation

Les trois commandes exigent :

- utilisateur authentifié actif;
- identité AppUser stable;
- `manage_planning`;
- visibilité/scope Planning ADR-027 sur tous les objets impactés.

Retourner un segment exige le scope sur tous les Shift qu'il retirera, y compris ceux d'une autre ressource.

Pour la réduction définitive, `manage_planning` seul est insuffisant.

Exiger en plus :

- `read_demands`;
- `approve_demands`;
- admissibilité actuelle sur toutes les exigences/scopes d'approbation de la source concernée;
- résolution dans le contexte de l'entrée actuellement autorisée selon les mécanismes canoniques ADR-010/020.

La candidature pendante n'est pas le contexte d'autorité.

Être seulement éditeur, PM, coordonnateur, ancien approbateur ou ADMIN n'est pas un bypass implicite.

Cette règle constitue une délégation ciblée de correction à la baisse pour travail non requis, sans nouveau vote au moment du clic, uniquement pour un acteur qui possède également l'autorité d'approbation nécessaire.

Si la coordonnatrice ne possède pas ces droits, l'action est indisponible avec motif. Cette ADR ne lui attribue pas automatiquement de nouveaux droits.

Respecter ADR-030 pour les lectures indirectes et ADR-026 pour les actifs.

### 17. Atomicité

Une seule transaction applicative et un seul rebuild.

Aucun commit intermédiaire.

#### Retour quart

```text
validate
→ guards/CAS
→ write intent/release
→ clean owned assets
→ delete target Shift
→ rebuild preserving survivors
→ project H/C/R
→ audit/idempotency receipt
→ commit
```

#### Retour segment

```text
validate all scope
→ guards/CAS
→ QUEUED + EXPLICIT_NONE
→ clean owned assets
→ delete all requirement Shift
→ rebuild without regeneration
→ project H/C/R
→ audit/idempotency receipt
→ commit
```

#### Réduction définitive

```text
validate Planning + demand authority
→ guards/CAS
→ read Shift/slot/requirement/request/line/revision/reductions
→ compute exact H-h
→ append reduction
→ delete target Shift with canonical cleanup
→ rematerialize effective need
→ rebuild/project
→ audit/idempotency receipt
→ commit
```

Toute erreur d'autorité, CAS, capacité, FK, actif, résolution de source, audit ou rebuild rollback l'ensemble de la transaction.

Les calculs utilisent la précision décimale canonique.

### 18. Actifs

ADR-016/018/026 restent autoritaires.

- `SHIFT_AD_HOC` possédé par le Shift supprimé : nettoyage canonique `AssetAllocation → AssetRequirement SHIFT_AD_HOC → Shift` dans la même transaction.
- `PROJECT_DIRECT` / `RESOURCE_PERIOD` hérités : retirer seulement la projection humaine; ne pas supprimer la réservation physique autonome.
- `SEGMENT` : retour à besoin/fenêtre inchangés conserve le propriétaire/réservation.
- une réduction partielle ne prorate pas arbitrairement dates/heures physiques;
- si une réduction à zéro rend une réservation incompatible, refuser jusqu'à adaptation/libération explicite;
- restaurer un Shift ne ressuscite pas automatiquement un actif ad hoc.

### 19. SAME_AS_PERIOD

ADR-029 reste contraignant.

Le retour vise uniquement le `ResourceRequirement` choisi, pas tout le composant SAME.

Les autres membres et Shift restent intacts.

Le slot `QUEUED` ne reçoit pas à nouveau sa cible par propagation.

Si Alice couvre encore un membre SAME lié, Bob peut être refusé pour le reliquat conformément à la contrainte existante.

Ne pas élargir silencieusement le retour à tous les membres.

Une réduction à zéro conserve la provenance et la topologie logique.

### 20. Split, duplicate, fenêtres et surcouverture

Les commandes agissent sur l'identité, la date et les heures persistées actuelles du Shift.

Un fragment retourné n'efface pas un frère le même jour.

Une duplication ayant créé de la surcouverture impose régularisation avant retour d'un quart ou réduction.

Les overrides de fenêtre opérationnels sont conservés.

Une modification ultérieure incompatible avec des survivants protégés exige diagnostic et décision explicite; ne jamais déplacer/supprimer silencieusement les survivants pour rétablir AUTO.

### 21. Besoin effectif zéro

Ne jamais écrire `planned_hours = 0` dans une table qui impose une quantité positive.

Si H devient zéro :

- conserver la décision de besoin effectif nul;
- conserver l'historique;
- marquer le requirement `Annulé` avec sa dernière quantité positive historique;
- s'il est déjà terminal `Terminé`, conserver ce statut terminal;
- ne plus créer de spécification active;
- exposer 0 h dans les lectures courantes;
- exposer 0 couverture et 0 reliquat courant;
- empêcher le matcher de recréer un requirement « manquant ».

Le total ligne/demande reflète zéro pour ce slot sans annuler automatiquement les autres lignes.

### 22. Projet/demande terminés

Les retours sont interdits lorsqu'ils créeraient une File de Travail non planifiable.

La réduction définitive reste possible comme correction auditée de fin de travail si :

- la dernière autorisation applicable est non ambiguë;
- le slot stable est résolu;
- les droits renforcés sont satisfaits.

La réduction ne réouvre pas projet/demande et ne relance pas la génération.

Une demande annulée avec annulation acceptée n'a plus de besoin courant à réduire : refus.

### 23. Contrat React

Pour un vrai Shift d'origine REQUEST, AUTO ou manuel, au bas de `ShiftEditor.tsx` :

1. **Retourner ce quart à la File de Travail**
2. **Retourner le segment à la File de Travail**
3. **Supprimer définitivement ce quart** — danger

Les capacités et motifs de blocage viennent du backend.

Pour REQUEST, les anciens boutons ambigus :

- `Revenir à l'automatique`;
- `Supprimer le quart manuel`;

ne doivent pas offrir un contournement. Ils sont retirés/neutralisés pour REQUEST et leurs endpoints historiques refusent l'usage ambigu.

QUICK_SHIFT conserve #755 / ADR-031.

AD_HOC conserve son contrat actuel.

### 24. Confirmation obligatoire de la suppression définitive

La suppression définitive ouvre un second dialog React.

Aucun `window.confirm()`.

Le dialog affiche l'aperçu backend réel :

- heures;
- segment;
- demande/ligne ou entrée approuvée;
- besoin courant avant/après.

Texte conceptuel :

> **Supprimer définitivement ce quart ?**
>
> Vous êtes sur le point de supprimer définitivement ce quart de 8 h. Ces 8 h seront également retirées du segment SEG-… et de la demande DMO-… / ligne …. Elles ne retourneront pas dans la File de Travail et ne seront plus planifiées. Voulez-vous vraiment continuer ?

Actions :

- **Annuler**
- **Supprimer définitivement** — danger

Annuler n'émet aucune mutation.

Le retry de la même confirmation conserve la même clé d'idempotence.

Un 409 exige rafraîchissement et nouvelle revue d'impact.

Le serveur relit toutes les autorités au commit même après un aperçu réussi.

### 25. Read model d'aperçu

Prévoir un endpoint de lecture non mutateur fournissant :

- capacités;
- motifs de blocage;
- identités visibles autorisées;
- H/C/R avant/après;
- portée segment;
- actifs/libérations pertinents;
- impact de réduction.

L'aperçu ne réserve rien et n'autorise pas le commit.

### 26. Migration et compatibilité

Migration additive.

Valeur par défaut des nouveaux slots : `AUTO`.

Ne pas inventer d'exclusions ou réductions pour les données existantes.

Établir les identités stables depuis les provenances/spécifications actuelles puis les conserver.

Si un rapprochement ne peut être établi sans ambiguïté :

- conserver les données;
- rendre les nouvelles actions indisponibles;
- exiger régularisation;
- ne pas deviner.

`LEGACY_UNKNOWN` n'est pas une preuve d'approbation.

Prévoir :

- contraintes de précision/positivité;
- unicité des commandes/réductions;
- indexes slot/libérations;
- FKs appropriées;
- rétention d'audit sans cascade destructrice;
- inventaires SQL et migration bridge à jour;
- validation SQLite et SQL Server.

Ne jamais modifier une migration déjà partagée.

## Alternatives considered

### A. Réduire directement `ResourceRequirement.planned_hours`

Rejeté : le resync approuvé peut restaurer la quantité nominale et `planned_hours` deviendrait une autorité concurrente.

### B. Modifier `RequestLine.estimated_hours`

Rejeté comme autorité de réduction : ce champ peut représenter une proposition candidate non approuvée et peut déclencher un cycle de réapprobation sans modifier immédiatement le plan actif.

### C. Créer automatiquement une nouvelle approbation

Rejeté : le bouton de correction opérationnelle ne doit pas fabriquer vote/quorum ou réécrire l'historique d'approbation.

### D. Utiliser uniquement les overrides de budget existants

Rejeté : ils opèrent au niveau de l'entrée et répartissent entre slots; ils ne préservent pas une réduction ciblée comme `40/40 → 32/40`.

### E. Exclusion de date seule

Rejeté : elle empêche une date mais ne bloque pas la redistribution automatique du reliquat sur d'autres jours.

### F. Faux requirement de reliquat, ressource synthétique ou copie persistée des Shift survivants

Rejeté : crée des autorités parallèles et brouille la séparation ADR-001 entre besoin, cible et affectation réelle.

## Consequences

### Positive

- les trois intentions utilisateur ont des contrats distincts et non ambigus;
- l'autorisation approuvée historique reste immuable;
- les retours survivent aux rebuilds et resyncs;
- le reliquat reste planifiable explicitement sans déplacement automatique des survivants;
- une réduction post-approbation cible exactement le slot concerné;
- les réapprobations futures peuvent absorber explicitement les réductions sans double soustraction;
- la File de Travail conserve une lecture correcte du plan actuellement autorisé;
- la concurrence repose sur les versions/CAS existants plutôt que de nouvelles horloges parallèles;
- les actifs et SAME_AS_PERIOD restent sous leurs autorités existantes.

### Trade-offs / negative

- nouvelle persistance pour identité de slot, intention, libérations et réductions;
- le résolveur du besoin effectif doit devenir transversal à plusieurs lecteurs/materializers;
- les parcours de réapprobation doivent gérer reprise/absorption/rebase;
- les permissions de réduction sont plus strictes que celles d'un simple changement Planning;
- les cas legacy ambigus peuvent rendre les nouvelles actions indisponibles jusqu'à régularisation;
- la validation SQL Server multi-session devient indispensable pour la concurrence.

## Implementation notes

Séquence de livraison attendue après autorisation DEV :

1. ajouter identité stable de slot, persistance/migrations, résolveur du besoin effectif et garde de réconciliation;
2. brancher d'abord lecteurs, matérialisation, resync, File et recommandations pour qu'aucun chemin ne contourne l'autorité;
3. implémenter les commandes atomiques, idempotence, audit, actifs, SAME et neutralisation des anciens endpoints REQUEST;
4. adapter le placement explicite du reliquat en cohérence avec #777 sans reconstruire les survivants;
5. brancher les trois actions React et l'aperçu/confirmation;
6. fournir les preuves backend, SQL Server réel, React/Playwright, migrations, server boundaries et régressions #693/#755/#765/#777.

Tests obligatoires :

- scénarios retour quart, retour segment, réduction définitive;
- rebuild non lié, restart, resync approuvé;
- AUTO/manual/locked, multiressource, split/duplicate, fragments même jour, surcouverture;
- restauration, Bob couvrant le reliquat, libérations multiples;
- multi-lignes/périodes/slots, `80 → 32/40`, zéro;
- candidat pendant, réapprobation, absorption explicite, rebase;
- actifs, SAME_AS_PERIOD, fenêtres;
- idempotence et versions périmées;
- concurrence SQL Server deux sessions;
- UI, confirmation, Annuler sans mutation, 409 + nouvel aperçu;
- Planning détaché #765.

## References

- GitHub Issue #778
- GitHub Issue #55
- GitHub Issue #693
- GitHub Issue #755
- GitHub Issue #777
- ADR-001 — séparation besoin / affectation réelle
- ADR-003 — autorisation approuvée immuable
- ADR-004 — candidat / approuvé / plan actif
- ADR-006 — Planning CAS
- ADR-010 — scopes et quorum d'approbation
- ADR-016 — actifs ad hoc possédés par Shift
- ADR-018 — occupation physique des actifs
- ADR-020 — contexte approuvé et responsabilité opérationnelle
- ADR-026 — autorité d'attribution des actifs
- ADR-027 — scope Planning
- ADR-029 — SAME_AS_PERIOD
- ADR-030 — capacités de lecture
- ADR-031 — exclusions QUICK_SHIFT
- `frontend/src/ShiftEditor.tsx`
- `app/infrastructure/sql/command_adapters.py`
- `app/domain/planning_engine.py`
- `app/infrastructure/sql/query_repository.py`
- `app/infrastructure/sql/period_approved_sync.py`
- `app/infrastructure/sql/approval_revision_repository.py`
- `app/application/demand_service.py`

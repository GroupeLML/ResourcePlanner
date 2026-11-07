# ADR-034 — Retours vers la File de Travail et réduction durable du besoin après approbation

Status: Accepted
Date: 2026-10-28
Accepted: 2026-10-28
WorkItem: ASTRA-778
Amendment #793: Proposed — pending Product Owner acceptance
Amendment proposed: 2026-11-07

Le contrat initial reste accepté depuis le 2026-10-28. Le présent texte intègre une **proposition d'amendement #793**, en attente d'acceptation explicite par le Product Owner. Les nouvelles règles de protection durable des survivants ne sont pas déclarées acceptées ni implémentées par cette proposition. Cet amendement ne change ni le statut de livraison de #793 ni celui de PR #803.

Le Product Owner accepte la décision d'architecture ASTRA-778 issue de la révision produit du 2026-10-28. Cette ADR formalise trois commandes métier distinctes pour les `Shift` issus d'un besoin `REQUEST` : retourner un quart à la File de Travail, retourner tout le segment à la File de Travail, et supprimer définitivement les heures d'un quart lorsque le travail n'est plus requis.

Cette décision complète ADR-001, ADR-003, ADR-004, ADR-006, ADR-016, ADR-018, ADR-020, ADR-026, ADR-027, ADR-029, ADR-030 et ADR-031. Elle ne remplace pas le contrat QUICK_SHIFT de #755 / ADR-031. L’amendement #793 préserve également la séparation de fenêtre et de cutoff établie par ADR-035.

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

#793 a établi qu'après deux réductions successives sur des slots distincts, les survivants AUTO de la première réduction pouvaient être supprimés puis redistribués par le rebuild de la seconde. Une liste transitoire `preserve_shift_ids` ne porte pas une décision durable. Le Product Owner demande que la réduction protège également le placement survivant, tout en maintenant AUTO pour le besoin encore non couvert. Cette extension est distincte du correctif d'identité `Resource.id` et du correctif de précision, qui reste une correction d'implémentation sous les exigences décimales existantes.

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
| Supprimer définitivement | supprimé | placement compté valide protégé durablement selon §11 | H - h | aucun nouveau reliquat créé |

La suppression définitive ne signifie pas que la File devient nécessairement vide. Un reliquat antérieur reste un reliquat.

Exemples :

```text
H=40, C=40, R=0; suppression définitive h=8
=> H=32, C=32, R=0

H=40, C=32, R=8; suppression définitive d'un Shift couvert de 8 h
=> H=32, C=24, R=8
```

La protection porte sur un sous-ensemble des vrais Shift comptés, sans ajouter une deuxième quantité de couverture. Lors d'un rebuild AUTO, `C_retained` est la couverture des survivants protégés et des autres placements déjà conservés par les autorités canoniques, chaque Shift physique compté une seule fois. Le moteur ne peut générer que `max(H - C_retained, 0)`; les sorties AUTO non protégées restent recalculables. `PRESERVE_EXISTING` et `QUEUED` conservent leur suppression de génération propre.

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

La clé logique est `(workforce_request_id, approved_entry_key, slot_index)`, portée par l'identité persistante `RequestRequirementPlanningIntent`. Les références de protection suivent cette identité; le seul `current_resource_requirement_id` physique ne suffit pas. Un remplacement compatible de requirement conserve les mêmes Shift survivants et leur rattachement au même slot, sans deviner un appariement vers un frère.

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

Comportement normal de génération automatique autorisé pour le besoin restant après la couverture conservée. Une réduction peut protéger durablement certains Shift sans changer ce mode. AUTO continue donc à générer un reliquat autorisé; il ne peut pas redistribuer ces survivants protégés pour le faire.

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

La protection issue d'une réduction (§11) est distincte de ce mode : elle protège des références de Shift précises sans supprimer la génération du reliquat AUTO. Une réduction seule ne fait jamais `AUTO → PRESERVE_EXISTING`.

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

Si le Shift retourné porte une protection de réduction, son lien actif est retiré ou supersédé avant la suppression du Shift, dans la même transaction, avec provenance auditée. Les autres liens restent intacts. Les heures retournées suivent la libération et le mode `PRESERVE_EXISTING`; elles ne diminuent pas H et ne constituent pas une nouvelle réduction. Une restauration explicite conserve sa propre identité de libération et ne réactive pas implicitement un ancien lien de protection.

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

Le retour du segment vise le `ResourceRequirement` entier, même s'il est réparti sur plusieurs ressources. Tous les liens actifs de protection de ce slot sont retirés ou supersédés atomiquement avant suppression des Shift; aucun lien orphelin n'est conservé. Les réductions historiques et leur effet sur H restent intacts. Aucun autre slot n'est affecté.

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

Une suppression ne produit qu'une réduction. La même transaction établit les références de protection des survivants comptés valides (§11); le registre des réductions et les références n'introduisent ni copie du plan ni nouveau solde d'heures.

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

Les heures supprimées ne peuvent revenir puisque H est réduit par le résolveur. La protection de placement décrite ci-dessous complète cette permanence quantitative.

#### 11.1. Règle de durabilité des survivants — amendement #793

**La réduction définitive protège durablement le placement compté valide des Shift survivants. Les rebuilds ultérieurs et les synchronisations compatibles conservent ces survivants jusqu'à ce qu'une opération explicite autorisée modifie ce placement. Le mode de génération reste inchangé; AUTO ne peut générer que le besoin encore non couvert après la couverture conservée.**

La protection conserve les mêmes identités, dates, heures, ressources, sources, états locked/manual, confirmations et relations d'actifs. Elle ne fabrique pas un verrou manuel ni un nouvel état de génération. Un rebuild sans rapport, un redémarrage, un resync ou la réduction d'un frère ne sont pas une autorisation de la retirer.

#### 11.2. Références persistantes et provenance

Étendre l'intention opérationnelle du slot par des références durables vers les Shift survivants réels, conceptuellement :

```text
planning_intent_id
shift_id
originating_reduction_id
```

- `planning_intent_id` désigne l'identité stable de §3; `originating_reduction_id` désigne un vrai `RequestCompletionReduction` de ce même slot.
- Le serveur détermine les survivants depuis les Shift du slot relu sous les gardes. Un client ne fournit pas une liste faisant autorité et aucun Shift frère ne peut être attaché.
- Une référence active est unique par couple intention/Shift; un même Shift ne peut être protégé sous deux slots. Les FKs, unicités et contrôles transactionnels assurent la cohérence entre réduction, intention, requirement courant et Shift.
- Les dates, heures, Resource, source, lock et propriété d'actifs restent exclusivement dans les entités canoniques. Les références ne contiennent pas une copie mutable de ce plan ni de ses actifs.
- Une réduction ultérieure du même slot conserve les liens existants et leur origine, ajoute seulement les liens manquants pour ses survivants, et retire/supersède le lien de son Shift ciblé s'il était protégé. Elle n'attribue pas rétroactivement les anciens liens à la nouvelle réduction.
- Création, maintien, modification et fin des liens sont auditables avec acteur, commande et réduction d'origine. Retirer un lien actif n'efface ni le registre append-only ni son historique. L'audit historique ne porte pas une FK empêchant la suppression explicite du Shift.

L'existence d'une réduction ne suffit jamais à inférer ces références : il faut des liens explicites vers les survivants établis dans la transaction ou par réconciliation probante de §26.

#### 11.3. Rebuild et reliquat AUTO

Tout chemin de calcul/rebuild, y compris les previews et deltas pertinents, doit :

1. résoudre les références depuis le slot stable et valider leur rattachement courant;
2. préserver les Shift référencés et leurs relations, sans suppression/recréation;
3. compter leur couverture et capacité exactement une fois, même si un Shift est aussi manuel, locked ou promu par un actif;
4. générer uniquement `max(H - C_retained, 0)` pour un slot AUTO, sous les règles normales de capacité, disponibilité, qualification, fenêtre, cutoff, actifs et SAME.

```text
Avant : H=24; Lun8 / Mar8 / Mer8
Réduction de Mer8 : H=16; survivants protégés Lun8 / Mar8
Rebuild ultérieur : mêmes Lun8 / Mar8; C_retained=16; génération=0

Cas partiel : H=20; couverture protégée=16; aucune autre couverture conservée
AUTO peut générer 4 h; les 16 h protégées restent en place
```

Une génération de 5,33/5,33/5,34 remplaçant Lun8/Mar8 est interdite dans le premier cas. Dans le cas partiel, le reliquat suit le moteur normal; la date d'un Shift réduit n'est pas globalement interdite. La protection ne crée ni `PlanningWorkQueueRelease`, ni exclusion QUICK_SHIFT, ni retour du besoin à la File, ni cutoff ADR-035.

ADR-035 reste autoritaire : `W` est la fenêtre approuvée avec widening ADR-022 applicable, `G = W ∩ [placement_not_before_date, +∞)` (ou `G = W` sans cutoff), et `H` reste le besoin effectif de §10. La protection ne redéfinit ni W ni G. `ResourceRequirement.start_date/end_date` restent la projection de W; ne jamais les rétrécir pour imposer le placement survivant.

`preserve_shift_ids` peut rester un moyen de transmettre des références au moteur pendant un appel. Il ne constitue pas l'autorité durable et ne dispense aucun rebuild ultérieur de relire cette autorité.

#### 11.4. Validité et refus de réconciliation implicite

La protection ne dispense d'aucune règle canonique. Sa validité tient compte :

- du même slot logique, de l'autorisation active applicable et de la compatibilité de révision;
- du besoin effectif et de la fenêtre autorisée, sans surcouverture nouvelle implicite;
- de l'éligibilité de la ressource, des qualifications, de la disponibilité et de la capacité selon leur temporalité canonique;
- des actifs, des exceptions déjà légitimement autorisées et de SAME_AS_PERIOD;
- du cutoff ADR-035 : un Shift déjà compté avant le cutoff reste valide dans W; toute nouvelle mise en place ou modification assimilée à un placement respecte G;
- de l'état terminal : l'historique `Terminé` reste protégé sans réouverture ni génération; il n'est pas invalidé comme une affectation future par le seul changement ultérieur d'éligibilité d'une ressource.

Une protection n'autorise ni nouvelle surallocation ni extension d'une dérogation existante. Une incompatibilité introduite par une mutation/revision doit produire un diagnostic structuré et un refus transactionnel jusqu'à réconciliation explicite. Si une évolution externe révèle déjà une incompatibilité, conserver les données/preuves et diagnostiquer; ne pas déplacer, supprimer ou recréer silencieusement les survivants pour la masquer.

Une référence active dont la cible est absente, dont le rattachement est orphelin/ambigu, ou qui vise un frère exige un diagnostic et un refus; elle n'est pas ignorée pour poursuivre le rebuild. La réconciliation ne peut pas être une remise automatique en AUTO sans protection.

#### 11.5. Modification explicite du placement

Seule une commande autorisée dont l'impact revu porte réellement sur les Shift protégés peut modifier/retirer leur protection : déplacement, édition manuelle, réaffectation explicite de ces Shift, retour, ou réconciliation canonique de réapprobation. Elle respecte permissions/scope, versions, capacité, cutoff, SAME et actifs.

Un déplacement/une édition valide conservant l'identité du Shift maintient le lien et audite le changement des entités réelles. Si la commande supprime ou remplace explicitement une identité, elle retire/supersède les liens concernés et établit, pour le placement survivant remplaçant, les références nécessaires de façon atomique et traçable. Aucune cascade technique ne vaut décision métier.

Changer uniquement une cible automatique, un cutoff, une quantité ou lancer un drag/drop de reliquat n'autorise pas à déplacer les survivants ni à retirer leurs liens. Une commande qui entend aussi changer un survivant doit déclarer cet impact explicitement et le valider. Une nouvelle allocation ne devient pas protégée par simple voisinage ou parce que le slot possède une réduction historique.

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
- les références de protection des survivants;
- le slot stable.

Une nouvelle autorisation nominale inchangée à 40 h garde donc H=32 h. `sync_operational_choices()`, la préparation approuvée et une rematérialisation compatible conservent les liens et les mêmes Shift. Si le requirement physique est remplacé, leur rattachement est transféré au nouveau requirement du même slot stable, avec les relations d'actifs, dans la transaction; les Shift ne sont pas recréés. Un transfert impossible ou ambigu exige réconciliation.

Une révision incompatible avec les survivants ne s'active pas partiellement : ancienne référence active, liens, cible, budget, réductions et Shift restent inchangés. La réconciliation explicite doit préciser les changements de placement autorisés; aucune protection ne migre heuristiquement vers une autre entrée/slot. L'absorption d'une réduction quantitative ne retire pas à elle seule les protections de placement.

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

La réduction contrôle en plus la version de demande. Les liens de protection participent à ces mêmes gardes; aucune horloge CAS parallèle n'est créée. Leur création ou supersession et le reçu appartiennent à la même transaction. Un replay conserve le résultat initial et ne crée aucun lien supplémentaire, même après un changement ultérieur de placement. Un conflit CAS ne conserve aucune mutation partielle des liens, réductions, Shift ou actifs.

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
→ write intent/release and retire/supersede targeted survivor protection
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
→ QUEUED + EXPLICIT_NONE and retire/supersede slot survivor protections
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
→ append reduction and establish/retain valid survivor protections
→ retire/supersede target protection if present
→ delete target Shift with canonical cleanup
→ rematerialize effective need
→ rebuild/project
→ audit/idempotency receipt
→ commit
```

Toute erreur d'autorité, CAS, capacité, FK, actif, résolution de source, audit ou rebuild rollback l'ensemble de la transaction.

Toute erreur de rattachement ou de validité d'une protection rollback également l'ensemble. Tous les rebuilds relisent les protections durables; aucun commit intermédiaire ne peut séparer réduction et liens.

Les calculs utilisent la précision décimale canonique. Le défaut de répartition/persistance fractionnaire observé pendant #793 reste un correctif DEV séparé sous cette exigence existante; cet amendement ne choisit ni nouvel algorithme d'arrondi ni assouplissement des contraintes de quantité.

### 18. Actifs

ADR-016/018/026 restent autoritaires. Un survivant protégé conserve ses propriétaires, réservations, liens d'actifs et locks canoniques; aucune copie de ces états n'est stockée dans sa référence de protection. Un retrait/modification explicite passe par le nettoyage et la réconciliation canoniques dans la transaction.

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

La protection de réduction est propre au slot ciblé. Elle ne retarget, ne réduit, ne met en attente et ne protège aucun frère par propagation. Les contraintes de composante sur les vraies affectations continuent à s'appliquer; un conflit exige décision explicite, sans déplacement silencieux d'un survivant protégé.

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

L'implémentation de l'amendement #793 exigera une nouvelle migration additive des références de protection, leurs FKs/unicités/indexes et les inventaires/compatibilités SQLite et SQL Server. Aucun schéma ou backfill n'est implémenté par ce gate.

Ne pas fabriquer une histoire de protection pour les anciennes réductions dont les survivants ont déjà disparu. Les Shift AUTO actuels régénérés ne prouvent pas le placement survivant d'origine. La migration conserve les données et l'audit, sans inventer de lien ni de réduction. Une réparation historique ne peut établir une protection qu'à partir de preuves canoniques non ambiguës, par une réconciliation explicite autorisée/versionnée/auditée qui conserve la vraie provenance; elle ne prétend pas qu'une protection avait été créée lors de l'ancienne commande. Les cas sans preuve restent diagnostiqués et à régulariser, sans être déclarés réparés.

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

### G. Réduction quantitative seule avec préservation transitoire

Rejeté par l'amendement #793 : respecte H mais laisse un rebuild ultérieur redistribuer le placement que le Product Owner demande de conserver. Une liste d'IDs passée au seul rebuild immédiat ne suffit pas.

### H. Passer systématiquement AUTO en PRESERVE_EXISTING

Rejeté : supprimerait aussi la génération du reliquat pourtant autorisé. Les références de protection sont orthogonales au mode.

### I. Déduire les survivants de l'existence d'une réduction ou interdire toute sa date

Rejeté : ne prouve pas les identités survivantes, confond les fragments et ne remplace pas la protection de placement. Les références ciblent les vraies lignes, pas une copie du plan rejetée en F.

## Consequences

### Positive

- les trois intentions utilisateur ont des contrats distincts et non ambigus;
- l'autorisation approuvée historique reste immuable;
- les retours survivent aux rebuilds et resyncs;
- le reliquat reste planifiable explicitement sans déplacement automatique des survivants;
- une réduction post-approbation cible exactement le slot concerné;
- ses survivants comptés valides restent durables sans convertir AUTO ni perdre le reliquat générable;
- la protection s'appuie sur les vraies affectations et conserve leur provenance, sans plan parallèle;
- les réapprobations futures peuvent absorber explicitement les réductions sans double soustraction;
- la File de Travail conserve une lecture correcte du plan actuellement autorisé;
- la concurrence repose sur les versions/CAS existants plutôt que de nouvelles horloges parallèles;
- les actifs et SAME_AS_PERIOD restent sous leurs autorités existantes.

### Trade-offs / negative

- nouvelle persistance pour identité de slot, intention, libérations, réductions et références de protection;
- les commandes qui changent un placement protégé doivent gérer explicitement le cycle de vie des liens;
- une incompatibilité future et les historiques sans preuve demandent réconciliation plutôt que réparation heuristique;
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

### Contrat d'acceptation de l'amendement #793

Après acceptation PO puis autorisation DEV, les régressions doivent exercer les chemins canoniques, sans simuler un rebuild réussi ni supposer les survivants protégés par le fixture. Contrôler les identités, dates, heures, Resource IDs, sources, locks, actifs, couverture et références relues en session fraîche.

| Cas obligatoire | Preuve attendue |
|---|---|
| Réductions successives sur A puis B | A conserve ses Mon8/Mar8 et leurs IDs après la réduction de B; H_A=16, pas de mercredi régénéré ni de lien frère |
| Rebuild sans rapport / redémarrage | Même slot, mêmes liens et mêmes Shift sans dépendre de `preserve_shift_ids` du premier appel |
| Resync compatible | Ni perte des références ni réécriture des survivants |
| Réapprobation/rematérialisation compatible | Slot stable, réductions et mêmes Shift conservés, y compris si le requirement physique change |
| Réapprobation incompatible | Diagnostic/réconciliation; aucune activation ou mutation partielle du plan précédent |
| AUTO partiel H=20, couverture protégée=16 | Génération normale de 4 h, sans redistribution des 16 h ni passage à PRESERVE_EXISTING |
| Capacité protégée / locked / actif | Chaque Shift physique consomme sa capacité une fois; sources et locks inchangés |
| Return Shift protégé | Lien ciblé retiré/supersédé, libération réelle et reliquat corrects, autres protections intactes |
| Return Segment | Tous les liens actifs du slot traités atomiquement; aucun orphelin ni effet sur un frère |
| Déplacement/édition/réaffectation explicite | Impact autorisé et audité, protection du placement résultant cohérente; changement de cible/reliquat seul sans déplacement implicite |
| Replay idempotent | Un seul événement de réduction; aucune référence ni mutation supplémentaire |
| Conflit CAS et échec après écriture des liens | Refus et rollback intégral du besoin, liens, Shift, réductions et actifs |
| SAME_AS_PERIOD | Contrainte réelle conservée, aucune propagation de protection/réduction/queue vers les frères |
| Survivant portant un actif | Propriétaires, IDs et réservation conservés; retrait explicite via nettoyage canonique |
| ADR-035 | Survivant pré-cutoff encore valide dans W conservé; nouvelle génération dans G; dates du requirement inchangées |
| Terminé | Survivants historiques conservés sans réouverture ni génération |
| Données historiques non prouvées | Aucun lien déduit d'un Shift régénéré; diagnostic/réconciliation explicite |
| Isolation multi-lignes/périodes/slots | Impossibilité d'attacher le Shift d'un frère, même demande/entrée/classe/nom |

La preuve de persistance, d'intégrité référentielle et de concurrence doit inclure le vrai SQL Server et SQLite; un job live sauté n'est pas une preuve. La précision canonique reste exigée et validée dans son correctif séparé. Cette matrice ne demande aucune implémentation dans la PR d'architecture.

## References

- GitHub Issue #778
- GitHub Issue #793 — durabilité des survivants après réductions successives
- PR #803 — caractérisation et correction d’identité Resource; implémentation de durabilité non livrée par cet amendement
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
- ADR-035 — début opérationnel de placement distinct de la fenêtre autorisée
- `frontend/src/ShiftEditor.tsx`
- `app/infrastructure/sql/command_adapters.py`
- `app/domain/planning_engine.py`
- `app/infrastructure/sql/query_repository.py`
- `app/infrastructure/sql/period_approved_sync.py`
- `app/infrastructure/sql/approval_revision_repository.py`
- `app/application/demand_service.py`

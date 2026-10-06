# ADR-017 — Autorité du chargé principal ERP et co-chargés RessourcePlanner

Status: Accepted  
Date: 2026-10-02

## Context

RessourcePlanner doit représenter deux faits métier distincts autour des chargés de projet :

1. le **chargé principal** provient d’Acumatica et reste sous autorité ERP;
2. des **co-chargés additionnels** peuvent être nommés localement dans RessourcePlanner.

Le modèle historique de `Project` conserve aujourd’hui trois représentations liées au chargé :

```text
Project.project_manager_external_id
Project.project_manager_name
Project.project_manager_contact_id
```

`project_manager_external_id` porte l’identité employé ERP et alimente déjà le périmètre personnel. `project_manager_contact_id`, au contraire, peut être écrit localement et est encore consommé par plusieurs projections opérationnelles. Ces deux représentations peuvent donc diverger.

Les besoins introduits par #573 imposent que :

- le principal ERP reste unique et ne puisse pas être remplacé localement;
- plusieurs co-chargés RessourcePlanner puissent être associés explicitement au projet;
- tous les chargés effectifs puissent voir le projet dans leur périmètre;
- les permissions restent gouvernées par le RBAC;
- les surfaces qui nécessitent un chargé unique utilisent toujours le principal ERP;
- l’absence d’un `AppUser`, d’un `BusinessContact` ou d’une `Resource` locale ne fasse pas disparaître l’identité ERP du principal.

Cette décision doit aussi rester cohérente avec :

- ADR-005 pour la séparation entre acteur authentifié et demandeur métier;
- ADR-012 pour l’identité locale `AppUser` et le pré-provisionnement;
- ADR-013 pour les frontières Moyen terme;
- #277 pour les vues contextuelles;
- #289 pour la résolution du responsable opérationnel;
- #290 pour les communications projet;
- #328 pour la création/édition de demandes;
- #556 pour le regroupement Moyen terme par chargé.

Au moment de cette ADR, `main@26edf498cbe4b3bed8c9e0ab05be6b14d20340e0` contient déjà #556. Cette livraison regroupe encore le Moyen terme à partir du contact projet historique. Ce comportement est une compatibilité transitoire : #573 devra le faire converger vers l’autorité définie ici sans redessiner les contrats budgétaires d’ADR-013.

## Decision

### 1. Le principal ERP reste persisté sur `Project`

Le chargé principal d’un projet est défini par :

```text
Project.project_manager_external_id
Project.project_manager_name
```

`project_manager_external_id` est l’identité durable du principal dans le domaine ERP `EmployeID`.

Il reste nullable et normalisé, mais ne devient pas une clé étrangère vers `AppUser`, `BusinessContact` ou `Resource`.

L’identité ERP doit pouvoir exister même lorsqu’aucune représentation locale n’est encore disponible.

`project_manager_name` reste un libellé descriptif associé à cette identité ERP. Il n’est jamais une identité de jointure.

### 2. Les co-chargés RP sont des nominations locales explicites

Les co-chargés sont représentés par une relation persistante dédiée, conceptuellement :

```text
ProjectCoManager
  project_id
  business_contact_id
  created_at
  created_by_user_id
```

L’identité stable de la relation est :

```text
(project_id, business_contact_id)
```

Cette table signifie exclusivement : « ce contact a été explicitement nommé co-chargé dans RessourcePlanner ».

Elle ne contient pas le principal ERP et n’introduit pas :

- `is_primary`;
- de second principal local;
- de ligne d’origine ERP;
- d’assignment polymorphe;
- de dépendance obligatoire vers `Resource`.

Un projet peut posséder zéro à plusieurs co-chargés. Un même contact peut être co-chargé de plusieurs projets.

Une nouvelle nomination exige un `BusinessContact` existant et actif. La nomination n’entraîne jamais la création implicite d’un `AppUser`.

### 3. La résolution canonique combine principal ERP et nominations RP

Le backend expose une projection canonique commune, conceptuellement :

```text
EffectiveProjectManagers
  project_id
  primary
    source = ERP
    employee_external_id
    erp_display_name
    app_user_id?
    business_contact_id?
    display_name
    user_active?
    contact_active?
    resolution_status
    diagnostics[]
  co_managers[]
    source = RP
    business_contact_id
    app_user_id?
    display_name
    user_active?
    contact_active?
    diagnostics[]
  diagnostics[]
```

Le principal est résolu exclusivement par la chaîne :

```text
Project.project_manager_external_id
→ AppUser.employee_external_id
→ AppUser.business_contact_id
→ BusinessContact.id
```

Aucune résolution par `UserID`, nom ou courriel n’est autorisée.

L’existence, l’activation ou le caractère planifiable d’une `Resource` n’est pas une précondition à la résolution du principal.

Le principal existe conceptuellement dès que `project_manager_external_id` existe, même si son utilisateur ou son contact local ne peut pas être résolu.

La projection doit distinguer explicitement les diagnostics pertinents, notamment :

- identité ERP absente;
- utilisateur RP non lié;
- contact non lié;
- référence invalide;
- utilisateur inactif;
- contact inactif;
- conflit d’identité.

### 4. Une personne peut avoir deux provenances sans être dupliquée

Si la même personne est à la fois :

- principal ERP;
- et co-chargé nommé explicitement dans RP;

la projection effective ne l’affiche pas deux fois.

La nomination RP reste néanmoins persistée comme fait indépendant.

Ainsi, lors d’un changement ERP de A vers B :

- B devient principal;
- A cesse d’être principal;
- A reste co-chargé uniquement si une nomination RP explicite d’A existe;
- une nomination RP préexistante de B est conservée;
- aucune nomination RP n’est supprimée par la synchronisation ERP.

L’ancien principal n’est jamais promu automatiquement comme co-chargé.

### 5. `Project.project_manager_contact_id` est déprécié comme autorité

La colonne historique `Project.project_manager_contact_id` ne doit plus constituer l’autorité pour :

- le principal;
- le périmètre utilisateur;
- le fallback opérationnel;
- le destinataire principal des communications;
- le regroupement portefeuille.

Pendant la transition :

- sa valeur physique peut être conservée pour diagnostic et reprise;
- les écritures administratives qui prétendent remplacer localement le principal doivent disparaître;
- les consommateurs doivent migrer vers la projection canonique;
- un ancien contrat ayant encore besoin d’un contact principal doit recevoir le contact résolu du principal ERP, pas relire cette colonne comme autorité.

La suppression physique de la colonne est différée jusqu’à disparition des lecteurs historiques et traitement des divergences.

Aucun backfill général de `ProjectCoManager` n’est permis à partir de cette FK : sa provenance historique ne prouve pas une intention de co-gestion.

### 6. Le périmètre projet est principal ERP ∪ co-chargés RP

Un projet est « géré » par un utilisateur lorsque l’une des conditions suivantes est vraie :

```text
le principal ERP correspond à cet utilisateur
OU
une nomination ProjectCoManager correspond à son BusinessContact
```

Cette même règle métier doit exister sous deux formes cohérentes :

- projection détaillée en lot pour les écrans et diagnostics;
- prédicat SQL efficace pour les scopes, en privilégiant `EXISTS` pour les co-chargés.

`managed_project_ids`, `participating_project_ids` et `personal_project_ids` conservent leurs responsabilités actuelles.

Un co-chargé local peut avoir un projet géré sans `employee_external_id` et sans `Resource`.

La relation chargé/co-chargé est un fait métier, pas une permission. Elle n’accorde automatiquement aucun rôle ni aucune permission comme `manage_demands`.

### 7. Le responsable opérationnel réutilise uniquement le principal ERP comme fallback final

**Remplacement partiel — ADR-020 (2026-10-04).** La hiérarchie limitée documentée lors de l'acceptation d'ADR-017 (`Demande → Tâche → Principal ERP`) est remplacée par la hiérarchie complète d'ADR-020 :

```text
Shift override
→ Segment / ResourceRequirement override
→ WorkforceRequest override
→ TaskCatalogEntry override
→ Project override local
→ Principal ERP canonique
```

ADR-020 est autoritaire pour l'ordre de résolution, le contexte approuvé figé et le cycle de vie des overrides opérationnels. ADR-017 reste autoritaire pour définir et résoudre le **principal ERP canonique** et les co-chargés.

Les co-chargés RP ne sont jamais choisis automatiquement comme fallback opérationnel.

Les garanties fail-closed existantes sont conservées : un override ou un responsable de tâche explicitement configuré mais invalide/inactif ne doit pas être masqué par le fallback projet.

Si un principal ERP existe mais que son contact local n’est pas résolu, le résultat doit distinguer ce cas de l’absence de principal :

```text
source = PROJECT_MANAGER
status = UNRESOLVED
identité/libellé ERP disponibles
coordonnées absentes
diagnostic explicite
```

Les contextes approuvés existants, les valeurs `LEGACY_UNKNOWN` et les historiques ne sont pas réécrits par cette évolution.

### 8. Les communications #290 utilisent les chargés effectifs comme destinataires To

> Amendement ciblé #611 (2026-10-05) : cette section remplace la règle initiale
> qui limitait `To` au seul principal ERP. Les autres décisions d’ADR-017 restent inchangées.

Pour les communications projet :

- `To` = principal ERP canonique résolu et communicable, puis co-chargés RP effectifs et communicables;
- les destinataires `To` sont dédupliqués par identité canonique et adresse explicite, jamais par nom;
- un co-chargé inactif ou sans courriel explicite produit un diagnostic bloquant;
- `CC` = ressources réellement affectées selon les règles existantes, hors adresses déjà présentes en `To`;
- le corps peut inclure les responsables opérationnels issus de #289, sans en faire des destinataires implicites.

Les snapshots, empreintes et contrôles d’obsolescence existants sont conservés. Un changement réel du principal, de ses coordonnées ou de la collection effective des co-chargés modifie l’empreinte de communication et peut rendre un snapshot obsolète.

### 9. Le regroupement Moyen terme reste unique par principal ERP

La visibilité d’un projet peut être multi-chargés, mais son regroupement portefeuille reste unique.

Le contrat est :

```text
1. sélectionner les projets visibles selon principal ERP ∪ co-chargés RP;
2. affecter chaque projet à un seul groupe;
3. dériver ce groupe uniquement du principal ERP canonique.
```

Plusieurs utilisateurs peuvent donc voir le même projet, mais le projet, son budget et ses WorkPackages apparaissent une seule fois dans le portefeuille.

La collection des co-chargés ne doit jamais être jointe directement aux lignes budgétaires avant agrégation.

#556, déjà livré lors de l’acceptation de cette ADR, utilise encore le contact projet historique pour son groupe chargé. Cette représentation doit converger vers le principal ERP canonique dans #573; les règles de budget, charge et capacité d’ADR-013 restent inchangées.

### 10. La synchronisation ERP ne modifie jamais les nominations RP

Un changement ERP de principal modifie uniquement le fait ERP porté par `Project`.

La synchronisation :

- ne crée pas automatiquement de co-chargé;
- ne supprime pas de co-chargé;
- ne transforme pas l’ancien principal en co-chargé;
- conserve les nominations RP lors des replays et changements de principal.

Tant que le contrat source ne distingue pas explicitement omission et effacement, une valeur ERP omise ou inexploitable conserve la sémantique prudente actuelle.

Pour le couple identité/libellé :

```text
identifiant inchangé + libellé absent
→ conserver le libellé connu

identifiant changé + libellé absent
→ ne pas conserver le nom de l’ancien principal
→ exposer au minimum le nouvel identifiant
```

Les changements réels d’identité principale doivent être audités; un replay identique ne crée pas un nouvel événement.

### 11. Les mutations de co-chargés possèdent une concurrence locale

La collection locale des co-chargés possède une version dédiée, conceptuellement :

```text
Project.co_managers_version >= 1
```

Cette version protège uniquement les ajouts et retraits de co-chargés.

Les commandes utilisent un CAS SQL :

```text
expected_version
→ CAS co_managers_version
→ validation
→ ajout/retrait
→ audit
→ commit
```

Une version périmée produit un conflit explicite.

`planning_version` n’est pas utilisé : une nomination de co-chargé ne modifie ni les quarts, ni les budgets, ni une enveloppe approuvée.

### 12. Les changements de chargés possèdent un audit projet dédié

Un journal projet durable doit couvrir au minimum :

- ajout d’un co-chargé;
- retrait d’un co-chargé;
- changement réel du principal ERP;
- régularisation explicite d’un ancien lien.

L’audit conserve l’acteur lorsqu’il s’agit d’une action humaine, l’origine ERP/RP, l’ancienne/nouvelle valeur, la date et, pour les mutations RP, la version locale résultante.

Le journal doit survivre au retrait physique d’une relation `ProjectCoManager`.

Les historiques de Planning, de demandes ou de quarts ne sont pas détournés pour cette fonction.

### 13. La migration est additive et prudente

L’introduction de ce modèle ne réécrit ni les historiques approuvés ni les identités existantes.

La reprise doit préserver notamment :

- les UUID existants;
- les contacts existants;
- les références approuvées;
- les snapshots de communication;
- les valeurs `LEGACY_UNKNOWN`.

Une association automatique ne peut jamais être créée à partir d’un nom, d’un courriel ou d’une correspondance approximative.

Une ancienne `project_manager_contact_id` concordante avec la résolution ERP peut confirmer la résolution actuelle du principal, mais ne crée pas une nomination RP.

Une ancienne FK divergente ou non résolue doit être conservée pour diagnostic/régularisation explicite, sans être utilisée comme principal canonique.

Un identifiant stable peut prouver quelle personne est référencée; il ne prouve pas qu’une nomination additionnelle a été intentionnellement décidée.

Les migrations et garanties de concurrence doivent être validées sur SQL Server conformément à ADR-011.

## Alternatives considered

### Une table unique pour principal ERP et co-chargés

Rejeté. Elle imposerait de dupliquer le fait ERP, garantir artificiellement exactement un principal, représenter un principal non résolu et synchroniser en permanence deux autorités pour la même donnée.

### Conserver `project_manager_contact_id` comme cache autoritaire permanent

Rejeté. Cette FK dépend de la disponibilité des identités locales, peut diverger du fait ERP et demanderait une invalidation transversale lors du provisionnement, de l’inactivation ou des changements ERP.

### Lier directement le principal à `AppUser` ou `Resource`

Rejeté. L’identité ERP doit survivre à l’absence de compte local et un chargé de projet n’a pas besoin d’être une ressource planifiable.

### Promouvoir automatiquement l’ancien principal comme co-chargé

Rejeté. Un changement ERP n’est pas une preuve d’une décision locale de co-gestion.

### Backfiller les co-chargés depuis l’ancienne FK

Rejeté. La FK historique ne conserve pas l’intention ni la provenance de son affectation; elle peut refléter une ancienne résolution ERP, une sélection locale ou un état devenu obsolète.

### Utiliser les co-chargés comme fallback opérationnel ou destinataires automatiques

Rejeté. Cela confondrait visibilité de projet, responsabilité opérationnelle et politique de communication.

### Regrouper le portefeuille par toutes les personnes qui voient le projet

Rejeté. Un projet multi-chargés serait dupliqué dans les agrégations et pourrait compter plusieurs fois son budget ou ses WorkPackages.

## Consequences

### Positive

- l’ERP reste l’unique autorité du principal;
- RP peut représenter plusieurs co-chargés sans dupliquer le fait ERP;
- un principal reste identifiable même avant provisionnement local;
- le scope utilisateur devient multi-chargés sans devenir un mécanisme RBAC;
- les surfaces nécessitant un chargé unique obtiennent une réponse déterministe;
- les changements ERP ne détruisent pas les décisions locales;
- #289, #290 et #556 peuvent partager une même autorité de principal;
- la migration peut préserver les données historiques sans inventer de relations.

### Trade-offs / negative

- une nouvelle relation persistante, une version locale et un audit sont nécessaires;
- plusieurs consommateurs historiques de `project_manager_contact_id` doivent être migrés de façon cohérente;
- les principaux ERP non résolus exigent des diagnostics explicites;
- la parité entre projection batch et prédicat SQL doit être testée;
- les divergences historiques de la FK projet doivent être régularisées explicitement;
- le regroupement #556 livré avant cette ADR nécessite une adaptation dans #573;
- la suppression physique de l’ancienne FK est différée.

## Implementation notes

Le découpage de référence issu de la gate ASTRA-573 est :

```text
573A — décision durable, schéma co-chargés, concurrence et audit
573B — résolution canonique batch et autorité ERP
573C — intégration scopes, projections, #289, #290 et contrat #556
573D — administration API/React et UX d’héritage explicite
573E — reprise des données et acceptation transversale SQL Server
```

Cette ADR documente la décision; elle ne constitue pas l’implémentation de 573A.

Points d’implémentation à préserver :

- chargements backend en lot, sans résolution par projet ou par quart;
- index inverse sur les nominations par contact;
- `EXISTS` pour le scope afin d’éviter la multiplication des lignes projet;
- pas de nouvel annuaire d’identité;
- pas d’assignment polymorphe;
- pas de nouvelle permission dérivée du rôle de chargé;
- mutations d’administration protégées par la permission existante appropriée, selon le contrat de sécurité de #573;
- migration additive, sans réécriture d’une migration partagée;
- validation SQL Server pour migration et concurrence.

## References

- #55 — roadmap maître / `COCKPIT_PIPELINE_V1`
- #573 — V2 Projets — chargé principal ERP, co-chargés RP et autorité canonique
- #232 — contrat Acumatica / identités ERP
- #277 — association utilisateur / ressource et vues contextuelles
- #289 — contacts métier et responsable opérationnel
- #290 — communications projet
- #328 — demandeur et délégation
- #468 — stabilisation des relations ERP / identités
- #556 — Moyen terme — regroupement par chargé de projet
- ADR-005 — identité canonique du demandeur
- ADR-011 — SQL Server comme base de référence
- ADR-012 — pré-provisionnement `AppUser`
- ADR-013 — frontières WorkPackage / Moyen terme / Planning / Delivery

# ADR-030 — Capacités de lecture par module et fermeture des accès indirects

Status: Accepted
Date: 2026-10-08

## Context

L'issue #706 interdit au technicien possédant seulement le rôle `TECHNICIAN` d'accéder aux modules Demandes, Projets et WorkPackages, y compris par API, détails ou projections indirectes, tout en préservant Mon horaire et les lectures Planning réduites d'ADR-027.

Sur `main@2f0262506b0427f69ae4f43cc5a1a2fdcd297008`, le rôle `TECHNICIAN` possède la permission générique `read`. Le middleware autorise encore la plupart des GET à partir de celle-ci. Les catalogues `/projects`, `/demands` et `/work-packages` utilisent un scope `global` par défaut, que `UserViewContextService.available_scopes()` expose également aux détenteurs de `read`. Sur certaines routes de détail, omettre `scope` contourne les restrictions spécifiques au Planning. Les projections Moyen terme, Delivery, Assets et de responsabilité opérationnelle peuvent exposer des équivalents des données interdites.

ADR-027 protège le Planning et les couples projet/jour des collègues, mais ne remplace pas le contrat d'autorisation des modules métier. ADR-008 sépare les droits Delivery de ceux de Planning. Il faut fermer les accès non autorisés sans retirer `read` aux vues personnelles, ni retirer silencieusement les droits légitimes des utilisateurs multirôles.

## Decision

### 1. Capacités positives de lecture par module

La politique RBAC backend introduit les permissions distinctes :

- `read_demands` : lecture des Demandes et de leurs projections complètes autorisées;
- `read_projects` : lecture des Projets, tâches et contextes métier complets autorisés;
- `read_work_packages` : lecture des WorkPackages et de leurs projections complètes autorisées.

Ces capacités sont vérifiées **avant l'accès aux données**, à la frontière HTTP/application, selon la surface réellement exposée. Les règles de périmètre métier s'appliquent ensuite lorsqu'elles existent. Le nom d'une route ou le seul verbe GET ne constitue plus une autorisation suffisante.

La permission historique `read` reste disponible pour les vues personnelles et les lectures Planning qu'ADR-027 autorise; elle n'accorde **aucune** des trois nouvelles capacités par implication. Les permissions `manage_*` restent distinctes de la lecture; une permission de mutation n'est pas utilisée comme substitut général à une capacité de lecture.

### 2. Attribution explicite et union des droits multirôles

`TECHNICIAN` seul ne reçoit aucune des trois nouvelles permissions. La présence du rôle `TECHNICIAN` ne retire pas une capacité explicitement accordée par un autre rôle. Les rôles historiques non techniciens reçoivent explicitement les capacités nécessaires pour conserver leurs **lectures actuellement légitimes**, sans leur donner de nouvelles permissions de mutation.

Ainsi, les combinaisons `TECHNICIAN + PROJECT_MANAGER`, `+ COORDINATOR`, `+ MANAGER` ou `+ ADMIN` conservent les permissions propres au second rôle et leurs périmètres respectifs, sans transformer un accès restreint Planning en accès global. L'autorisation ne peut pas être déduite de `roles.length === 1`, d'un simple nom de rôle côté React ou de la participation à un projet.

**Arbitrage Delivery.** `DELIVERY_CONTRIBUTOR` conserve explicitement, dans cette tranche, les lectures historiques de catalogues Projets/WorkPackages nécessaires à sa navigation Delivery, y compris en combinaison avec `TECHNICIAN`. Ce rôle ne gagne aucun droit Planning implicite. Une future politique « seulement mes Stories, sans catalogue général » exigerait une décision et une évolution de projection Delivery distinctes; #706 ne les introduit pas.

### 3. Interdiction indépendante des paramètres et du chemin d'accès

Pour une lecture relevant d'un des trois modules, l'absence de la capacité requise bloque la requête indépendamment de :

- `scope` absent, `mine` ou `global`, ou autre filtre client;
- `source=planning` ou d'une provenance déclarée par le navigateur;
- l'identifiant d'un projet, d'une demande, d'un WorkPackage, d'un segment, d'un plan ou d'un objet connu;
- l'appartenance effective du technicien au projet ou au quart.

Un chemin de détail, un historique, des périodes, un état d'approbation, un plan-delta ou des workflow-actions ne doit pas réouvrir les données lorsque `scope` est omis. L'accès par identifiant direct ou une URL imbriquée n'est pas un passe-droit. La politique d'ADR-027 continue de s'appliquer **en plus** lorsqu'une réponse concerne le Planning.

Refuser avec `403 permission_denied` une capacité de module absente, **avant** de rechercher l'objet. Si la capacité de module existe mais que l'objet est hors du périmètre autorisé, préserver le masquage `404` du contrat applicable. Ne pas convertir un refus en `200 []`.

### 4. Protéger les lectures équivalentes et les projections indirectes

La classification de 706A couvre les lectures directes et toute projection restituant essentiellement les mêmes informations : budget Moyen terme et segments non liés, résumés WorkPackage, Delivery board par `plan_id` ou `work_package_id`, exigences et allocations d'actifs, responsables opérationnels, tâches et contextes projet, documents, exportations et autres lectures imbriquées pertinentes.

L'autorisation des **actions** Delivery ne vaut pas automatiquement autorisation de **lecture** du board. Les autorisations indépendantes de Verification doivent être préservées : seule une réponse contenant des données régies par cette décision doit recevoir la garde correspondante. Une projection strictement limitée déjà autorisée (par exemple libellé du projet ou de la tâche sur un quart personnel, ou voisin Planning projet/jour) demeure permise **sans** ouvrir la fiche source ni le catalogue global.

La classification doit se faire par contenu exposé et par droit positif, et non en bloquant aveuglément des préfixes API. Elle ne demande pas de refonte générale Delivery, Assets ou Verification.

### 5. Frontend et sessions

React consomme les capacités résolues par le serveur; il ne recalcule pas de matrice rôle→permissions. Pour une capacité absente, la navigation, le montage de page, les chargements asynchrones et les callbacks d'ouverture sont bloqués. Une bascule d'identité vide les données/dialogues sensibles, et une réponse réseau tardive de l'identité précédente est ignorée.

La page Mon horaire reste servie par son endpoint personnel et l'identité ressource existante. Les exceptions de visibilité Planning d'ADR-027 (voisins sur **couples exacts projet/jour**, projections minimales, absence de propagation) sont conservées, même lorsque Demandes/Projets/WorkPackages ne sont pas consultables.

## Alternatives considered

### Masquer uniquement les menus React

Rejeté : les GET, les ID connus et le JSON restent accessibles.

### Retirer la permission générique `read` au technicien ou imposer `manage_*` pour consulter

Rejeté : casse Mon horaire/Planning et confond lecture et mutation.

### Appliquer les restrictions Planning à tous les modules ou refuser toute identité contenant TECHNICIAN

Rejeté : contredit les droits propres des modules, ADR-008 et l'union multirôle d'ADR-027.

### Restreindre immédiatement DELIVERY_CONTRIBUTOR à ses Stories

Non retenu dans #706 : cela modifie un contrat distinct et requiert une projection Delivery spécifique et une autorisation produit ultérieure.

## Consequences

### Positive

- les refus sont appliqués par le serveur et non contournables avec un changement de scope ou un identifiant;
- les données indirectes suivent la même frontière de confidentialité que leur module d'origine;
- les vues personnelles/Planning et les permissions multirôles légitimes sont préservées;
- les nouvelles capacités peuvent être résolues depuis le principal courant, sans migration métier ni requête SQL additionnelle obligatoire.

### Trade-offs / negative

- inventaire systématique des routes et read models nécessaire pour les surfaces partagées;
- les tests historiques qui assimilent `GET /projects` à `read` doivent être mis à jour pour le contrat volontairement changé;
- les capacités exposées à React et l'invalidation lors d'un changement d'identité doivent rester cohérentes avec le serveur.

## Implementation notes

- **706A (backend/API)** : définir et attribuer les capacités, classer listes/détails/projections/direct-ID/exports, protéger avant divulgation, garder les mutations existantes et leurs règles; ajouter des tests API ciblés.
- **706B (React)** : piloter menus, montages, callbacks et requêtes par capacités serveur; fermer les détails imbriqués; neutraliser les réponses en vol d'une ancienne identité.
- **706C (acceptation transverse)** : tester `scope` absent/`mine`/`global`, faux `source`, objets réels/inexistants/du projet du technicien, `plan_id`, documents et JSON complets, réservations, mutations, multirôles, identité sans ressource, Planning voisin projet/jour et changement d'identité. Les refus doivent prouver **l'absence du contenu interdit dans la réponse**.

Pas de nouvelle infrastructure, migration métier ni changement du moteur de planification. Chaque tranche garde son périmètre autorisé. L'acceptation de cet ADR ne constitue **pas** la livraison de 706A/B/C.

## References

- GitHub Issue #706 (ASTRA-706, 706A, 706B, 706C)
- GitHub roadmap #55, `COCKPIT_PIPELINE_V1`
- GitHub Issues #218, #235, #277, #409, #618
- ADR-008 — Delivery/Planning boundary
- ADR-027 — Planning visibility by role and authorized scope
- `app/application/security.py`; `app/server/security.py`; `app/application/user_view_context.py`
- `app/server/routes_reads.py`; `app/server/routes_delivery.py`; `app/server/routes_assets.py`; `app/server/routes_operational_responsibility.py`
- `frontend/src/App.tsx`

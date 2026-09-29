# Delivery — guide opérationnel

Ce document décrit l'utilisation et la maintenance du domaine **Delivery** livré par l'issue #362.
**ADR-008 reste la décision architecturale autoritaire.**

## Frontière Delivery / Planning

Le WorkPackage relie deux domaines distincts :

- **Planning** est autoritaire pour les besoins, les affectations et la capacité réservée (ResourceRequirement, Shift, actifs);
- **Delivery** est autoritaire pour le découpage technique (Epic, Story), les estimations, le travail restant, les statuts, la progression et le forecast.

Une Story ne crée, ne déplace et ne supprime jamais un Shift. Une mutation purement Delivery n'utilise pas planning_version et ne doit pas acquérir le CAS Planning. Les Shift représentent une **capacité réservée**; ils ne sont pas des heures réellement travaillées.

La capacité affichée par Delivery est une projection backend en lecture seule du plan Planning **actif/approuvé**. Une demande candidate non approuvée ne déplace donc pas la capacité visible. Après une modification réelle du Planning actif, la lecture suivante de Delivery reflète la nouvelle capacité et sa nouvelle observed_planning_version.

## Rôles et permissions

Les permissions Delivery sont séparées des permissions Planning.

| Acteur | Autorité Delivery | Autorité Planning acquise par Delivery |
| --- | --- | --- |
| Chargé de projet / administrateur | créer, activer, archiver le plan; choisir le Team Lead; gérer les actions de plan prévues par le backend | aucune permission Planning supplémentaire |
| Team Lead du DeliveryPlan | structurer Epics/Stories, ordonner, affecter, estimer, gérer statuts/blocages/sprint selon les actions backend | aucune; le rôle contextuel de lead n'accorde pas manage_planning |
| Technicien autorisé Delivery | statut, travail restant et blocage sur ses propres Stories | aucune; TECHNICIAN reste lecture seule par défaut et l'autorisation Delivery utilise DELIVERY_CONTRIBUTOR |

Le backend combine les permissions globales et la relation contextuelle au plan/à la Story. React consomme uniquement les actions retournées par le backend; il ne reconstruit pas une matrice rôle → permission.

## Progression, forecast et capacité

La **progression** est calculée côté backend à partir des Stories DONE, pondérées par leur estimation de référence stable. L'estimation de référence est capturée lors du premier engagement de la Story; une réestimation ultérieure peut changer l'estimation courante sans réécrire cette référence.

Une Story non estimée réduit la couverture de l'indicateur. Si aucune estimation de référence exploitable n'existe, le backend retourne un diagnostic et **aucun pourcentage n'est inventé**.

Le **forecast** est distinct de la progression. Il utilise remaining_hours des Stories non terminées. Modifier remaining_hours peut donc changer le forecast sans réécrire la progression historique.

La **capacité réservée** vient de Planning. Le rapprochement capacité ↔ travail restant est un forecast de couverture, pas une mesure d'actuals ou d'earned value.

## Concurrence et retry

Chaque DeliveryPlan possède son propre delivery_version. Toute mutation de board envoie expected_delivery_version.

En cas de conflit, le backend retourne delivery_version_conflict (HTTP 409). Le client React recharge alors le board et le résumé depuis le backend, affiche le conflit et demande à l'utilisateur de réessayer son action sur la version courante. Il ne tente pas de fusionner silencieusement deux éditions concurrentes.

planning_version reste indépendant et n'est jamais utilisé pour une mutation purement Delivery.

## Archivage, identités et historique

Le cycle de vie initial est DRAFT → ACTIVE → ARCHIVED.

L'archivage conserve :

- l'identité stable du DeliveryPlan;
- les IDs stables des Epics/Stories;
- l'état persistant des items;
- delivery_change_history, incluant acteur, action, item concerné, version et détails utiles.

Les Stories suivies sont conservées; CANCELLED sert à les retirer des agrégats courants sans effacer leur identité. Un ancien WorkPackage sans DeliveryPlan reste valide et lisible.

## Limites actuelles

Delivery n'intègre pas encore de source autoritaire d'heures réellement consommées. Les Shift ne doivent donc jamais être interprétés comme des actuals.

FAT, SAT, commissioning, preuves et exigences de vérification appartiennent à la future issue **#363 Verification**. Ces concepts ne doivent pas être ajoutés comme champs de Story dans #362.

## Validation de maintenance

Pour un changement touchant Delivery, exécuter d'abord les tests ciblés :

    python -m unittest \
      tests.test_delivery_domain \
      tests.test_delivery_persistence \
      tests.test_delivery_security \
      tests.test_delivery_api \
      tests.test_delivery_projection \
      tests.test_react_delivery_contract -v

Puis valider le frontend :

    cd frontend
    npm run build
    npm run test:e2e

La CI du dépôt reste autoritaire pour les deux shards Python, l'isolation serveur, le navigateur React/FastAPI/SQLite et le smoke Docker.

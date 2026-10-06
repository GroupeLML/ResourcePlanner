# ADR-023 — Cycle Verification, fermeture Story et preuves versionnées

Status: Accepted  
Date: 2026-10-05

## Context

#363 doit fermer la boucle entre Delivery et les essais FAT/SAT/commissioning sans fusionner Verification avec Delivery ou Planning.

Le socle Delivery d'ADR-008 fournit des identités persistantes de Stories, une progression propre et une concurrence distincte de Planning. Une Story peut aujourd'hui être créée ou transitionnée directement vers `DONE`; son audit ne reconstruit pas une ancienne méthode de test ni un ancien résultat attendu. Les essais terrain peuvent être réalisés par un autre acteur que le développeur initial, parfois après archivage du DeliveryPlan.

L'analyse ASTRA-363 sur `main@ac3512eb394d2c82a807b214055d690465365f46` confirme qu'une extension additive du monolithe modulaire est suffisante, à condition de rendre explicites la décision de vérification à la fermeture d'une Story, la révision des exigences, les retests, la concurrence, les permissions et la conservation des preuves.

## Decision

### 1. Verification reste un domaine distinct

Verification demeure distinct de Delivery et Planning.

Le périmètre persistant minimal est porté par le WorkPackage via un `VerificationScope` ou responsabilité équivalente, avec une version de concurrence `verification_version` propre.

Le commissioning package est une projection de ce périmètre; il n'introduit pas un second état métier à maintenir.

Aucune opération purement Verification ne modifie `planning_version`, les `Shift` ou les formules Delivery.

### 2. Une Story terminée exige une décision explicite, pas un test déjà réussi

La fermeture d'une Story doit enregistrer atomiquement une décision de vérification avant le passage à `DONE`.

Deux catégories sont retenues :

- `NO_TEST_REQUIRED`, avec justification obligatoire;
- `TESTS_DEFINED`, avec au moins une exigence active et correctement définie.

La présence de la décision est obligatoire pour fermer la Story. L'obtention future d'un `PASS` ne bloque pas cette fermeture.

La même règle doit couvrir tous les chemins de fermeture, y compris une création directe en `DONE` ou une mise à jour générique. Une simple validation React n'est pas suffisante.

Les Stories historiques déjà terminées sans décision restent terminées. Leur couverture est projetée comme non documentée; l'absence de décision n'est jamais convertie automatiquement en « aucun test requis ».

Une réouverture conserve décisions et essais antérieurs. À la fermeture suivante, la décision est reconfirmée ou modifiée explicitement.

### 3. Une exigence appartient à une seule phase

Une `VerificationRequirement` possède une identité stable et une seule phase parmi FAT, SAT ou commissioning.

Un contrôle requis dans plusieurs phases est représenté par plusieurs exigences distinctes, même si l'interface peut aider à les préremplir ensemble.

La provenance principale reste la Story. Le backend vérifie que la FK cible bien une `STORY` du même WorkPackage. Les tests manuels hors Story restent une extension ultérieure.

### 4. Les définitions de test sont révisées de façon immuable

La définition métier d'une exigence est versionnée par une révision immuable contenant notamment :

- objet du test;
- méthode/action;
- résultat attendu;
- prérequis;
- criticité utile.

Modifier la méthode, le résultat attendu ou les prérequis crée une nouvelle révision. Un ancien résultat ne devient jamais automatiquement valide pour une nouvelle révision.

### 5. Les exécutions sont append-only et liées à la révision testée

Chaque `TestExecution` référence exactement la révision réellement testée et conserve :

- résultat;
- exécutant;
- date métier éventuelle;
- date d'enregistrement serveur;
- mesures structurées;
- commentaires;
- preuves associées.

Les résultats successifs ne s'écrasent pas. Une correction administrative conserve l'original et son motif de remplacement ou d'invalidation.

Pour la révision courante, la dernière tentative applicable ordonnée par une séquence serveur détermine `PASS`, `FAIL` ou `BLOCKED`.

`NOT_RUN` est un état projeté d'une exigence sans résultat applicable; ce n'est pas une fausse exécution persistée.

Une demande explicite de retest remet l'exigence « à exécuter » sans effacer son historique.

### 6. Fermeture Story atomique et concurrence séparée

La fermeture d'une Story avec sa décision Verification est une commande composite transactionnelle.

Elle doit :

1. vérifier l'autorisation de terminer la Story;
2. traiter le replay idempotent;
3. acquérir les gardes nécessaires;
4. relire les états décisionnels;
5. enregistrer la décision et les exigences;
6. passer la Story à `DONE`;
7. écrire audits et reçu idempotent;
8. commit ou rollback intégral.

Les opérations de révision, affectation et résultat utilisent le CAS Verification.

La fermeture Story combine CAS Delivery et Verification dans la même transaction SQL.

L'ordre d'acquisition des gardes est fixe : WorkPackage lorsque nécessaire, puis Delivery, puis Verification.

La première dépendance Verification participe à la garde WorkPackage d'ADR-013. Les scénarios de concurrence significatifs sont validés sur SQL Server conformément à ADR-011.

### 7. Permissions Verification distinctes et contextuelles

Les permissions Verification sont distinctes des permissions Delivery et Planning.

Autorités cibles :

- l'auteur autorisé de la fermeture peut enregistrer la décision et les exigences issues de sa Story;
- le responsable Verification du WorkPackage peut définir, réviser, affecter, retirer avec motif et demander un retest;
- un exécutant explicitement affecté et autorisé peut enregistrer résultats et preuves pour les essais concernés;
- les chargés/co-chargés autorisés peuvent piloter ou consulter selon leur périmètre;
- l'administrateur agit explicitement et de façon auditée.

Une affectation seule ne crée aucune permission. Être ressource planifiée, responsable opérationnel ou auteur initial ne rend pas automatiquement l'utilisateur exécuteur habilité.

Le responsable Verification doit rester résolvable après archivage du DeliveryPlan. Il peut être initialisé depuis le lead Delivery mais ne dépend pas d'une recherche du seul plan actif.

### 8. Cycle de vie non destructif

Archiver Delivery, annuler une Story ou clôturer un WorkPackage ne supprime pas les exigences ou exécutions Verification.

Une exigence peut être retirée uniquement par une action motivée et reste visible dans l'historique.

Le statut WorkPackage ne constitue jamais une preuve de réussite terrain et n'est pas piloté automatiquement par les essais.

### 9. Preuves externes par liens pour la première livraison

Le premier périmètre accepte des références de preuves externes par liens HTTPS.

L'application stocke la provenance et la référence mais ne prétend pas garantir l'intégrité des octets externes et ne télécharge pas arbitrairement les URLs côté serveur.

L'upload natif de fichiers est reporté à une tranche explicite couvrant stockage privé durable, métadonnées SQL, empreinte, contrôle d'accès, limites, sauvegarde et restauration. Le disque éphémère du conteneur n'est pas un stockage de preuve acceptable.

### 10. Documents comme projections cohérentes

Les plans de test, rapports FAT/SAT/commissioning et matrices de traçabilité sont des projections des données Verification structurées.

La première livraison documentaire cible :

- HTML imprimable;
- CSV de traçabilité.

PDF serveur, DOCX, signatures et acceptation client restent hors du contrat initial.

Chaque export identifie son périmètre, sa date, les révisions et les exécutions sélectionnées et doit lire un ensemble cohérent.

### 11. Indicateurs distincts

Les projections exposent séparément :

- couverture des décisions des Stories terminées;
- exigences actives par phase;
- `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`;
- exigences retirées;
- taux de réussite des exigences courantes.

Un périmètre sans exigence affiche « aucun test défini », jamais 100 %.

Des Stories terminées sans décision rendent la couverture incomplète même si tous les tests connus sont passés.

## Alternatives considered

### Stocker FAT/SAT/commissioning directement dans la Story

Rejeté. Cela fusionnerait le cycle Delivery avec l'exécution terrain, compliquerait les retests et empêcherait une conservation indépendante après archivage.

### Utiliser le statut WorkPackage ou Delivery pour représenter la réussite terrain

Rejeté. ADR-019 interdit déjà de traiter le statut WorkPackage comme une preuve d'exécution terrain, et Delivery possède sa propre progression.

### Réutiliser `delivery_version` comme concurrence Verification

Rejeté. Les essais ont un cycle de vie indépendant et doivent pouvoir évoluer sans conflit artificiel avec Delivery.

### Mettre à jour en place la définition du test et son dernier résultat

Rejeté. Cette approche rendrait les anciens rapports trompeurs et empêcherait de prouver quelle méthode a réellement été testée.

### Héberger immédiatement les preuves binaires

Reporté. Cette option exige une infrastructure durable de stockage, autorisation, sauvegarde et restauration qui dépasse le premier périmètre cohérent.

## Consequences

### Positive

- la fermeture d'une Story ne peut plus perdre silencieusement la décision de test;
- Delivery, Planning et Verification gardent des autorités et concurrences séparées;
- un ancien `PASS` ne couvre jamais implicitement une nouvelle définition;
- l'historique de retests reste complet et auditable;
- les essais peuvent continuer après archivage Delivery;
- les acteurs terrain peuvent être distincts du développeur;
- les documents sont reproductibles depuis les données structurées;
- le modèle reste additif dans le monolithe existant.

### Trade-offs / negative

- la fermeture Story devient une commande composite Delivery + Verification;
- une version de concurrence Verification et de nouveaux audits persistants sont nécessaires;
- les données historiques terminées sans décision doivent être projetées explicitement comme non documentées;
- les permissions deviennent plus fines;
- les exports exigent des lectures cohérentes;
- les liens externes ne garantissent pas la conservation des octets;
- les validations de concurrence importantes doivent être exercées sur SQL Server.

## Implementation notes

Le découpage accepté de #363 est :

```text
363A — ADR et contrats métier : décisions, phases, révisions, retests, permissions
363B — persistance additive, contraintes, CAS, audit et idempotence
363C — commande atomique de fermeture Story et reprise des décisions historiques
363D — exécutions, affectations, preuves par liens et projections
363E — parcours React : fermeture, package, essais et historique
363F — documents, acceptation transverse et validation SQL Server
```

L'ordre est séquentiel sur la lane MAIN. Après acceptation d'ASTRA-363, `363A` est la première tranche DEV autorisable; `363B` à `363F` restent bloquées jusqu'à leur prédécesseur.

La matérialisation de cette ADR est documentaire et ne constitue pas le démarrage d'une tranche DEV.

## References

- #55 — roadmap maître
- #363 — Verification : FAT/SAT/commissioning, preuves et documentation
- #362 — Delivery WorkPackage / Epics / Stories / Kanban
- ADR-006 — version globale des mutations Planning
- ADR-008 — frontière Delivery / Planning
- ADR-011 — SQL Server autoritaire
- ADR-013 — WorkPackage et frontières moyen terme
- ADR-017 — principal ERP et co-chargés RP
- ADR-019 — cycle de vie WorkPackage dérivé
- ADR-020 — responsabilité opérationnelle et contexte approuvé
- Analyse ASTRA-363, acceptée par décision humaine le 2026-10-05

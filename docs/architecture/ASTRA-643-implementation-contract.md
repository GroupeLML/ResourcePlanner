# ASTRA-643 — Contrat de livraison et d'exploitation

Status: Proposed, avec [ADR-033](ADR-033-daily-acumatica-sync-and-safe-recovery.md)
Date: 2026-10-24

Ce contrat complète la décision, sans implémenter 643A/B/C. La hiérarchie métier reste #207, #525/#540, ADR-024 et les modifications explicites d'ADR-033. Les valeurs ci-dessous sont des politiques initiales explicites, non des propriétés supposées d'Acumatica.

## Configuration et calendrier

Utiliser les conventions `RESOURCEPLANNER_*`, parsing/validation de `app/server/runtime.py`, paramètres injectés au runtime et exemples sans secrets. Le profil source, l'horaire et la version de politique sont figés par occurrence/tentative. Les processus d'un même site doivent annoncer la même empreinte de configuration, hors secrets, au contrôle SQL; un désaccord est bloquant et visible.

| Variable | Défaut | Contrat |
|---|---|---|
| `RESOURCEPLANNER_ERP_SYNC_ENABLED` | `false` | Autorise uniquement les occurrences automatiques; ne retire pas les commandes manuelles autorisées. |
| `RESOURCEPLANNER_ERP_SYNC_TIME` | `00:00` | Heure locale `HH:MM`, 00:00–23:59, résolue pour chaque date. |
| `RESOURCEPLANNER_ERP_SYNC_TIMEZONE` | `America/Toronto` | Identifiant IANA valide; aucun fallback silencieux vers UTC ou TZ du conteneur. |
| `RESOURCEPLANNER_ERP_SYNC_CATCHUP_HOURS` | `4` | Durée écoulée > 0 et ≤ 12 h depuis l'échéance; borne finale exclue pour tout nouveau départ automatique. |
| `RESOURCEPLANNER_ERP_SYNC_MIN_SOURCE_RATIO` | `0.50` | Ratio > 0 et ≤ 1 contre le dernier baseline réussi comparable; seuil bloquant, pas seuil d'effacement. |
| `RESOURCEPLANNER_ERP_SYNC_MIN_PROJECTS` | `1` | Entier ≥ 0; zéro exige approbation explicite du profil/baseline vide. Sinon feed vide bloqué sauf exception manuelle liée à la source. |
| `RESOURCEPLANNER_ERP_SYNC_MIN_TASK_ROWS` | `1` | Entier ≥ 0, seulement si des cibles ERP actives existent; source DEPMO, pas nombre de tâches Standards; zéro soumis à approbation de baseline. |
| `RESOURCEPLANNER_ERP_SYNC_MAX_EVIDENCE_MIB` | `256` | Maximum déchiffré par tentative; entier 16–1024 MiB; inclut sources et checkpoints. |
| `RESOURCEPLANNER_ERP_SYNC_EVIDENCE_BUDGET_MIB` | `2048` | Budget SQL total, au moins le maximum d'une tentative et ≤ 16384 MiB; réserver sous contrôle SQL, mesurer aussi ciphertext/manifeste. |
| `RESOURCEPLANNER_ERP_SYNC_FAILURE_RETENTION_DAYS` | `14` | 1–90 jours pour payloads non épinglés d'échec/interruption. |
| `RESOURCEPLANNER_ERP_SYNC_HISTORY_DAYS` | `90` | 30–365 jours de détail; tombstones quotidiens 400 jours et pointeurs de preuve conservés indépendamment. |
| `RESOURCEPLANNER_ERP_SYNC_FRESHNESS_SLA_HOURS` | `30` | Entier 1–168; âge du dernier succès complet, aucune remise à zéro sur tentative échouée. |

Les constantes de protocole initiales sont timer 30 s, heartbeat 60 s, lease 300 s, deadline de tentative 2 h, retry automatique après 15 min et maximum 3 tentatives. Elles sont versionnées/testées, sans ajouter un panneau de réglages de concurrence non nécessaire.

Les paramètres OData existants restent `RESOURCEPLANNER_ACUMATICA_BASE_URL`, `USERNAME`, `PASSWORD`, `PAGE_SIZE`, `TIMEOUT_SECONDS`; les noms courts de cette phrase se rapportent au même préfixe Acumatica. Une valeur de configuration invalide avec automatisation demandée bloque l'activation et produit un diagnostic sûr. Les variables REST historiques ne remplacent pas les credentials Basic.

Le timer local est seulement un réveil : date métier, échéance et expiration sont revalidées depuis l'heure UTC de SQL Server au claim. Le watermark empêche la création automatique de dates antérieures déjà évaluées; il n'empêche pas les retries bornés d'une occurrence courante existante.

Le fuseau d'ordonnancement ne redéfinit pas le calcul budgétaire : ADR-024 utilise toujours le fuseau métier canonique du budget, actuellement America/Toronto. Une autre zone de scheduler est explicitement affichée; les instants source restent UTC et les dates de cutoff restent dérivées par la primitive budgétaire canonique.

En mode désactivé, afficher `DISABLED`, prochaine échéance automatique `null`, absence de SLA d'occurrence ratée; conserver l'âge réel des données et les diagnostics d'import manuel. Réconciliation des leases expirées et traitement d'un travail manuel accepté restent disponibles. Désactiver le timer n'efface pas les runs; arrêt d'un import actif = commande explicite de drain, pas un effet caché de la case Enabled.

## Préconditions et intégrité de source

Avant application, prouver et persister :

1. configuration du scope demandé, authentification/lecture OData requise, accès SQL, révision Alembic et protocole compatibles;
2. occurrence acceptée, propriétaire et génération valides, budget de preuve/chiffrement disponible;
3. fin des pages projets, mapping ProjectId/ProjectCode unique et statut ERP explicite; aucune suppression d'un absent;
4. cible active figée après superposition du snapshot projets, sans utiliser un nom comme identité;
5. fin des pages tâches globales DEPMO, partition correcte et agrégation des lignes financières cohérentes;
6. résultats structurels, compteurs distincts (pages, lignes brutes, TaskID distincts, lignes rejetées, tâches admissibles/exclues), baseline et seuils comparables;
7. snapshot normalisé immuable, versions des DTO et empreintes de politique/Standards/coûts utilisées;
8. manifeste de checkpoint prêt, capacité réservée et vérification de déchiffrement.

La lecture des premières pages peut servir de preuve de reachability sans inventer un endpoint de ping ERP; elle reste une lecture source sans mutation métier. L'échec du preflight peut écrire run/audit/diagnostic, mais aucune ligne projet/tâche/budget.

Un feed incomplet n'est jamais un snapshot valide. Un XML invalide, une incohérence d'identité globale ou une pagination qui répète/ne progresse pas invalide toute la source. Les comparaisons de volume globales précèdent les commits projets. Les anomalies localisables sur un projet tâches sont enregistrées comme échec de ce projet; aucune de ses lignes ne passe avec un faux succès partiel.

L'import conserve la sémantique existante d'absence : pas de delete/désactivation par soustraction de listes. Une inactivation de projet vient d'un état ERP explicite. Les statuts de tâche reçus et la politique canonique d'admissibilité peuvent désactiver explicitement une ligne existante; la planification nocturne n'ajoute pas de nouvelle règle d'effacement des absents.

Une exception de plausibilité exige `sync_projects + admin_settings`, raison, empreinte source figée, seuils concernés et expiration à la fin de la tentative. Le frontend ne peut transmettre `skip_validation=true`. Le bootstrap présente les comptes et diagnostics de sources complètes avant approbation; aucune activation automatique sans ce baseline. Revalider la politique de référence dans chaque transaction d'import; verrouiller les références lues lorsque nécessaire pour éviter un check-then-use SQL. Un changement de politique en cours n'est pas absorbé silencieusement : terminer en échec explicite, conserver les commits déjà prouvés et exiger une nouvelle tentative cohérente.

Classification minimale après import :

| Classe | Exemples | Effet |
|---|---|---|
| Échec global dur | Identités externes dupliquées/incohérentes, référence persistée cassée, projet/task ownership erroné non isolable, résultats manquants/doublés, compteur incompatible avec reçus, publication incohérente | FAILED; pas de succès complet, recovery REQUIRED si données déjà committées. |
| Échec local dur | Transaction SQL d'un projet tâches refusée, identité ou snapshot localement invalide | Projet non appliqué/rollbacké, reçu d'échec; parent SUCCEEDED_WITH_ERRORS si les autres phases/contrôles globaux sont valides. |
| Avertissement / exclusion métier | Projet inactif ou local sans identité ERP, TaskCD non admissible selon Standards, conversion horaire indisponible accompagnée du diagnostic canonique, variation de volume restant dans les seuils | Compteurs/diagnostics explicites, pas de faux échec structurel ni budget calculé par défaut. |

La validation globale vérifie les identités et références affectées, l'ensemble attendu de cibles actives, un reçu par cible traitée, la somme des compteurs et leur correspondance aux lignes effectivement acceptées, la cohérence des statuts et des provenances. Elle n'infère pas une complétude en comparant uniquement deux totaux égaux.

## Frontières transactionnelles et preuve de commit

| Unité | Ce qui commit ensemble | Échec |
|---|---|---|
| Claim / commande de lancement | Occurrence/tentative, génération, reçu idempotent d'acceptation | Aucun worker autorisé; le gagnant est relu si conflit. |
| Capture source | Chunks puis manifeste SEALED, métriques et hashes après fin de pagination | Artefact non SEALED inutilisable; nettoyage sûr et aucun import. |
| Snapshot projets | Garde, before-images, upserts/CAS principal, audit, post-validation projets et reçu de phase | Rollback de la phase; tâches non commencées. |
| Projet tâches | Garde, before-images, upserts, validations, fraîcheur prouvée, résultat projet #540 et reçu | Rollback du projet; diagnostic séparé sous garde; autres projets conservés. |
| Publication complète | Validation finale, statut terminal parent, références de réussite complète et baseline accepté | Aucun succès complet; réconciliation depuis preuves, pas depuis logs. |
| Compensation d'un projet | Garde, hashes/CAS attendus, champs ERP autorisés, validation, provenance/qualité, audit et reçu | Aucune compensation partielle de ce projet; autres unités explicitement suivies. |

Déduire les compteurs agrégés des résultats uniques par `(task_run_id, project_id)` ou les maintenir atomiquement avec eux. Ne pas les incrémenter une seconde fois après un replay. Un crash entre commit métier et publication de résultat ne doit plus être possible pour les nouvelles tentatives.

Les mutations ERP de principal respectent `SqlProjectSyncRepository` et `ProjectManagerAudit`; les co-chargés et autres autorités locales ne sont pas restaurés depuis un dump ERP. Les imports fichier/commandes d'exploitation pouvant écrire les mêmes lignes doivent prendre le même contrôle ou être refusés pendant un run. Un outil hors ligne ancien non coordonné nécessite arrêt/drain documenté, jamais exécution parallèle supposée sûre.

## Contrats exacts des tranches

### 643A — scheduler et orchestration, automatisation non activée

Dépendance : ADR-033 accepté, gate réconciliée; aucune tranche B/C démarrée implicitement.

Livrer :

- migration occurrence/tentative/contrôle, rattachement au run tâches existant, contraintes et index; migrations/checks/inventaires associés;
- résolution quotidienne timezone-aware, identités DAILY/MANUAL, configuration figée, modes disabled et missed, rattrapage/retry bornés;
- orchestrateur partagé et worker hors requête, création/suivi durables, commandes RUN_NOW/RETRY et idempotence d'acceptation;
- claim/lease/génération sur toutes les écritures et coordination des chemins manuel/global/ciblé; remplacement de la réconciliation startup aveugle;
- ordre projets puis tâches, frontière par projet, atomicité données + résultats de progression; réutilisation des primitives, pas un deuxième moteur;
- service applicatif injectable pour gardes B, sans considérer des gardes absentes comme validées; indicateur de capacité bloquant l'exécution automatique avant B;
- adaptation minimale du frontend existant au suivi asynchrone projets/tâches et maintien explicite des contrats de compatibilité.

Preuves d'acceptation :

1. minuit Toronto, rattrapage 00:37, midi MISSED, date entière manquée sans burst; fenêtres testées en instants UTC;
2. printemps/automne et heure configurée dans le trou/repli, une seule occurrence par date; modification d'horaire, TZ absente/invalide et recul d'horloge;
3. deux processus/connexions **SQL Server réel** tentant même date puis commandes manuelles concurrentes : un seul propriétaire;
4. worker A expire, B prend la nouvelle génération, A revient : toutes les écritures métier/statut/progression d'A échouent; B reste intact;
5. startup de B pendant run sain d'A : aucun `INTERRUPTED` indu; processus arrêté puis lease expirée : ancienne tentative interrompue, nouvelle identité de tentative;
6. erreurs projets sans tâches; projets tâches A/C réussis, B en erreur : commits A/C conservés, parent avec erreurs et résultat de B;
7. crash aux frontières projets/resultat projet/publication, unicités et compteurs exacts après retry; commande identique rejouée sans nouveau run;
8. aucune dépendance au temps d'attente HTTP ou un deuxième executor non gardé; worker shutdown sans interrompre les autres instances;
9. smoke Docker restart et image avec données IANA; capacité B manquante + Enabled demandé = activation refusée.

Les tests de scheduler peuvent injecter des phases sûres fictives. Ils ne constituent pas une autorisation de lancer un import réel avec checkpoint/validation no-op. Les commandes manuelles historiques restent protégées par leurs garanties #207/#540 et le nouveau contrôle; l'automatisation demeure désactivée entre A et B/C.

### 643B — validation, checkpoint, qualité et compensation sûre

Dépendance : 643A livré. Tout import automatique reste désactivé avant preuve des garde-fous.

Livrer :

- capture normalisée bornée des deux sources avant import complet, validation structurelle et de plausibilité, baseline/exceptions auditables;
- manifeste/chunks chiffrés, before/after-images des champs possédés, admission/réservation des tailles, rétention/purge idempotente;
- validation locale avant commit et globale avant publication, classes de rejet/warning définies;
- succès/tentative par projet, provenance effective par tâche, liens de qualité et lecture Moyen terme sans faux budget restant fiable;
- service de preview/confirmation de compensation, gardes de génération/hashes/dépendances/autorité; aucune restauration automatique;
- reprise par nouvelle lecture et replay des services existants, états de preuves/pinning/expiration;
- protocole de capacité sûr annoncé seulement une fois tous ces guards exécutables.

Preuves d'acceptation :

1. page projet manquante/erreur, doublon ProjectId intra/inter-page, identité locale conflictuelle, feed vide et 1 300 → 8 : zéro mutation métier;
2. page tâches manquante ou invalide : ni projets ni tâches appliqués au run complet; pagination répétée détectée;
3. lignes financières cohérentes multiples d'un TaskID agrégées une seule fois, y compris frontière de page; TaskCD hors Standards et DEPMO distingués;
4. incohérence tâche/projet isolable : rollback du projet, autres projets committés, aucune fraîcheur de la tâche rejetée; anomalie non attribuable = source globale refusée;
5. absence de baseline, profil différent, baisse globale/locale, exception de volume valide puis périmée/source changée; aucune exception aux erreurs structurelles;
6. checkpoint/chiffrement/key/budget indisponible avant mutation : refus sûr; dépassement imprévu : unité courante rollbackée et état global non réussi;
7. before-image antérieure à l'écriture, résultat/provenance atomiques avec elle, nouvelle ligne marquée absente avant, unchanged réellement reçu rafraîchi;
8. erreur SQL locale, crash après quelques commits, anomalie globale post-validation : pas de rollback automatique A/C, preuves retenues et qualité correcte;
9. preview périmé, changement local/concurrent, nouvelle dépendance ou principal modifié : compensation refusée sans écrasement; retry idempotent sans double audit d'effet;
10. compensation ERP seule, champs locaux conservés, création compensée sans delete destructif, fraîcheur restaurée/invalidate non avancée;
11. budget global de stockage concurrent, purge après succès publié seulement, échec conservé, preuve REQUIRED épinglée, expiration explicite;
12. aucun raw XML/credential dans logs, résultat, manifeste public ou API; un utilisateur sans les deux permissions ne peut autoriser exception/restauration;
13. **SQL Server réel** pour transaction/rollback/fencing/CAS/contraintes et compatibilité SQLite pour tests locaux, migrations et bridge/cutover; aucune preuve SQLite présentée comme preuve de concurrence SQL Server.

### 643C — observabilité, contrôles et activation d'exploitation

Dépendance : 643B livré; finalisation de la capacité d'exploitation avant activation production.

Livrer :

- read model et API protégés, UI dernière tentative/succès/phase/next run/compteurs/projets échoués/qualité/âge/checkpoint;
- contrôle manuel RUN_NOW/RETRY, bootstrap/exception de plausibilité et preview/confirmation de compensation selon capacités backend;
- états DISABLED/MISSED/FAILED/PARTIAL/STALE/QUARANTINED et signaux heartbeat/SLA/échecs répétés; logs structurés sûrs;
- wiring des options dans les profils Compose utilisés, exemples d'environnement sans secrets, vérification de tzdata, compatibilité des images/protocoles;
- runbook dédié Ubuntu/Compose avec historique Synology correctement qualifié, activation, drain, promotion/rollback et diagnostics SQL/ERP;
- fermeture du parcours E2E et preuves d'exploitation, sans communication automatique aux tiers.

Preuves d'acceptation :

1. dernière tentative change sur échec, dernière réussite complète reste inchangée; un état partiel montre A/C fraîches et B stale sans succès global inventé;
2. freshness UNKNOWN initiale, donnée ancienne, prochain run DST/catch-up, scheduler DISABLED distinct de heartbeat absent et schema incompatible;
3. retry en cours/succès/échec, RUN_NOW après succès quotidien, deux clics même clé, contrôle de conflit d'un autre run;
4. droits `sync_projects`, droits supplémentaires de recovery/exception, JSON complet sans fuite, acteur système impossible à fournir par client;
5. erreurs ERP sûres sans rendre le Web non-live; SQL indisponible sans fausse preuve persistée d'une tentative;
6. **Docker + SQL Server réel** : restart 00:37, restart pendant travail, deux backends, déploiement/promotions de même protocole, refus d'ancien writer concurrent;
7. frontend/API/E2E existants compatibles avec les adaptateurs, données fraîches réellement intégrées, provenance/cutoff ADR-024 inchangés dans leur calcul;
8. restauration contrôlée, preuves expirées et budget saturé correctement expliqués; aucune suppression de données métier depuis le contrôle de purge;
9. activation seulement après compte ERP dédié confirmé hors Git, schéma/crypto/stockage/horloge prêts, baseline approuvé et A/B/C validés.

## Scénarios opérationnels normatifs

| Scénario | Persistance et données | Reprise / fraîcheur / visibilité |
|---|---|---|
| A — ERP indisponible à minuit, SQL disponible | Occurrence unique et tentative FAILED/preflight, code sûr; aucune mutation métier. | Nouvelle tentative à +15 min, maximum 3 départs dans la fenêtre. À 00:37 après arrêt backend, rattrapage si date encore admissible. Après épuisement/délai : retry manuel; dernière réussite inchangée, diagnostic visible. |
| B — HTTP 200, 8 projets contre environ 1 300 | Snapshot conservé comme preuve de refus, validation de volume FAILED, aucun upsert ni phase tâches. | Pas de retry automatique pour contourner le seuil. Preview puis exception liée à cette source si un opérateur confirme la baisse; nouvelle source = nouvelle revue. |
| C — projets réussis, tâches du projet 37 en erreur SQL | Projets committés/validés; transaction tâches 37 rollbackée, autres projets conservés; reçu de rejet sûr de 37. | Parent SUCCEEDED_WITH_ERRORS, succès locaux des autres, 37 stale, dernier succès complet inchangé. Retry manuel relit les sources et rejoue; pas de restore des succès. |
| D — crash après commit projets, avant tâches | Reçu PROJECTS_VALIDATED et checkpoint projets durables; tâche enfant non exécutée. | Après expiration, ancienne tentative INTERRUPTED; nouvelle tentative sous génération suivante, nouvelles sources, replay projets puis tâches. Pas de succès complet lors du crash. |
| E — crash au milieu des projets tâches | Chaque projet committé a données + résultat + fraîcheur atomiques; transaction en cours rollbackée par SQL à perte de connexion. | Réconciliation lease-aware #540, nouvelles lectures/replay global par projet. Commits valides conservés; pas de curseur de reprise mémoire, pas d'écrasement par worker périmé. |
| F — deux instances à minuit | Même clé d'occurrence; verrou de contrôle SQL et contrainte unique; une seule génération propriétaire. | Le perdant observe le run gagnant, ne crée ni child indépendant ni seconde occurrence; un restart ne tue pas le gagnant. |
| G — repli DST | Même business_date malgré deux heures locales identiques; première heure résolue retenue, claim unique. | Une occurrence, éventuellement plusieurs tentatives explicitement bornées. Au printemps, heure inexistante déplacée au premier instant valide; aucune logique UTC fixe/24 h. |
| H — invariant global invalide après commits tâches | Transaction courante seulement rollbackée si encore ouverte; anciens commits conservés; parent FAILED, recovery REQUIRED, preuves épinglées. | Aucune restauration automatique; publications de la tentative quarantinées, imports suivants bloqués jusqu'à résolution. Preview/confirmation manuels; pas de fraîcheur globale publiée. |
| I — backend redémarre à midi / journée entière hors ligne | Date courante MISSED si activé; anciennes dates ne génèrent pas une rafale de runs ni timestamps fictifs. | RUN_NOW ou RETRY explicite; montrer lacunes/âge du dernier succès. |
| J — SQL indisponible avant claim | Aucun claim/durable diagnostic SQL possible; aucun write métier autorisé. | Logs/probes sûrs; au retour, matérialiser seulement l'observation courante et appliquer le rattrapage de la date courante. |
| K — panne après publication, avant purge | SUCCEEDED/publication déjà durables; checkpoint encore présent. | Redémarrage rejoue uniquement la purge idempotente; ne relance pas l'import du jour. |
| L — saturation ou preuve de recovery expirée | Aucun effacement d'une preuve épinglée pour libérer de la place; état EXPIRED explicite pour une preuve non épinglée purgée par rétention. | Import bloqué par budget si nécessaire; recovery expirée refuse le restore. Nouvelle lecture manuelle possible après résolution, jamais faux succès. |

## Exploitation et migration

643A/B ajoutent des migrations forward; aucune migration partagée n'est réécrite. Les DTO preuves sont versionnés et les binaires refusent un protocole futur inconnu. Les tables et colonnes nouvelles doivent être inscrites aux inventaires exhaustifs, profils historiques SQLite et matrice cutover selon leur nature; l'exhaustivité fail-closed reste intacte.

Le runtime de référence est SQL Server 2017/SQLAlchemy/pyodbc/ODBC 18, tel que documenté dans le dépôt. Le scheduler ne requiert ni privilège BACKUP DATABASE ni accès admin à SQL. Les probes distinguent accès technique, capacité d'import sûr et SLA métier. NTP reste une précondition VM; l'heure serveur SQL arbitre la lease et les timestamps de contrôle, normalisés UTC côté Python.

Les credentials et preuves de configuration du site sont des préconditions d'activation, pas des secrets à demander dans une PR. Vérifier réellement l'instance au déploiement; cette gate documentaire ne prétend avoir exécuté ni concurrence SQL Server ni smoke de production.

Promotion/rollback : arrêter les nouveaux départs, drainer ou constater l'interruption du propriétaire, ne pas raccourcir une lease pour forcer un passage; vérifier versions/migration; déployer les images; probes; réactiver après contrôles. SQL externe conserve occurrences et preuves. Ne jamais automatiser de downgrade ni exécuter un ancien importeur ne respectant pas le protocole pendant un run nouveau.

## Acceptation et réconciliation roadmap

La gate a été autorisée; la proposition doit être acceptée explicitement selon le workflow ADR du dépôt. Le PR documentaire peut être revu sans lancer DEV; il ne faut pas interpréter son existence comme une livraison de #643.

Après **acceptation et livraison documentaire prouvée** : réconcilier ASTRA-643 DONE, promouvoir **643A seulement** READY, laisser 643B/643C et ASTRA-644 BLOCKED selon la séquence existante; #641 demeure bloqué par la livraison complète #643. Conserver les identités et l'ordre de `COCKPIT_PIPELINE_V1`, et aligner les passages humains décrivant l'état courant. Ce n'est pas un changement de décomposition ni une permission de commencer la tranche suivante dans cette session.

Corriger dès cette analyse la phrase obsolète de #643 disant #693 READY / ASTRA-643 BLOCKED : #55 prouve #693 DONE et ASTRA-643 READY. Conserver la provenance de la priorité du 2026-10-07 comme historique; ajouter la référence de la proposition sans déclarer la gate DONE avant son acceptation. Le livrable de cette session est documentaire uniquement.

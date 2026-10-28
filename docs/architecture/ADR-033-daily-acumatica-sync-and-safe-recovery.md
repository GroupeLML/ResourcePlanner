# ADR-033 — Synchronisation quotidienne Acumatica et reprise sûre

Status: Accepted
Date: 2026-10-24
Accepted: 2026-10-28
WorkItem: ASTRA-643

Le Product Owner a accepté cette décision sans amendement architectural le 2026-10-28 et autorisé le workflow d'acceptation/fusion de PR #774. Cette acceptation ne lance aucune tranche DEV. Les contrats d'acceptation et scénarios opérationnels font partie de cette décision dans [ASTRA-643-implementation-contract.md](ASTRA-643-implementation-contract.md).

## Context

Analyse de `main@608cb4ec3075437f2f99d65024a5afdc9c517703`, #643 et #55. Le bloc canonique de #55 place **ASTRA-643 READY / MAIN**, après #693 livré; le dernier paragraphe de #643 indiquant encore #693 READY est obsolète. La séquence existante reste ASTRA-643 → 643A → 643B → 643C → ASTRA-644. #643 doit être livré avant ASTRA-641/Copilot.

Le code impose les choix suivants :

| Source inspectée | Fait déterminant |
|---|---|
| `app/application/project_sync.py`, `app/infrastructure/sql/project_sync_repository.py` | Snapshot projet matérialisé avant upsert; identité ProjectId; absence du feed sans suppression; protection CAS du principal ERP et audit spécifique. |
| `app/infrastructure/acumatica/odata_project_source.py` | Atom/XML, pagination `$orderby=ProjectId asc` et `$top/$skip`, HTTP Basic via secrets. |
| `app/infrastructure/acumatica/odata_project_task_source.py`, `app/application/task_catalog.py` | Lecture DEPMO globale paginée, partition par identité ERP, agrégation financière par TaskID; Standards TaskCD reste un filtre métier distinct. |
| `app/server/acumatica_project_task_sync.py`, modèles SQL et migration `0005_task_sync_runs` | Run durable #540, index unique actif, exécuteur local; chaque projet commit indépendamment. Le résultat projet est actuellement enregistré après son commit métier; le démarrage interrompt indistinctement les runs actifs. Ces deux points doivent évoluer pour la reprise multi-instance. |
| `app/infrastructure/sql/task_catalog_repository.py`, ADR-024 | Fraîcheur par tâche même pour une valeur inchangée, métadonnées par projet, cutoff journalier inclusif en America/Toronto. Un succès global ne remplace pas cette provenance. |
| `app/server/http.py`, `app/server/runtime.py`, `Dockerfile.backend`, Compose et `docs/DEPLOYMENT_UBUNTU_VM.md` | Lifespan FastAPI, Python 3.12, worker de synchronisation existant, conteneurs `unless-stopped`. La cible privilégiée est Docker/Compose dans une VM Ubuntu hébergée sur Synology, avec SQL Server externe; Container Manager et `deploy/synology/` sont historiques. |

Les scripts de promotion particuliers au site ne sont pas versionnés dans ce snapshot. Cette décision ne présume pas leur contenu. Elle s'appuie sur le contrat versionné : migration explicite, images identifiables, rollback de code après vérification de compatibilité, jamais de downgrade automatique.

## Decision

### 1. Scheduler spécifique dans le backend existant

Retenir une boucle légère dans le lifespan FastAPI : évaluation au démarrage puis toutes les 30 secondes, travail bloquant confié à l'exécuteur dédié existant. Le timer réveille un service applicatif d'orchestration; il n'implémente ni import ni décision métier dans la couche HTTP. Aucun appel HTTP interne au backend n'est nécessaire.

Chaque processus autorisé peut évaluer l'échéance. **SQL Server décide de l'occurrence et du propriétaire**, jamais le nombre de workers ou un mutex Python. La boucle est annulée proprement au shutdown; elle ne ferme que son propre travail. Une absence de configuration ERP avec automatisation désactivée n'empêche pas le démarrage du Web.

Ce choix réutilise le cycle de vie et l'exécuteur réellement déployés, sans nouveau conteneur, cron DSM ou service de queue. Un processus dédié pourra consommer le même contrat plus tard sans changer l'autorité SQL; il n'est pas requis ici.

### 2. Une occurrence par date métier, calendrier et rattrapage borné

Identité quotidienne unique dans la base du site : `(ERP_PROJECTS_TASKS_DAILY, business_date)`. Le fuseau, l'heure, la révision de configuration et l'échéance UTC résolue sont figés sur l'occurrence, mais le fuseau n'est pas un deuxième namespace permettant de recréer la même date après une modification de configuration.

Défaut : **00:00 America/Toronto**. Calculer l'échéance de chaque date avec `zoneinfo`, et non par ajout de 24 heures à l'exécution précédente. Heure ambiguë : première occurrence (`fold=0`); heure inexistante : premier instant local valide après le trou, déterminé par aller-retour UTC. L'unicité SQL interdit un deuxième départ lors du repli DST. L'image doit contenir les données IANA requises, vérifiées au startup.

Après retour à 00:37, une occurrence absente de la date courante est rattrapée. Un nouveau départ automatique est permis de l'échéance incluse jusqu'à **4 heures écoulées après l'échéance, borne exclue**; le calcul du délai utilise UTC et reste limité à la date métier courante, même si un horaire tardif ferait dépasser minuit à la fenêtre théorique. Un run commencé à temps peut terminer après cette borne. À midi pour l'horaire par défaut, enregistrer `MISSED` pour la date courante et attendre une commande manuelle. Ne jamais lancer les dates passées en rafale. Le journal expose les journées sans succès, sans inventer des heures de tentative pendant l'arrêt.

Une erreur transitoire de configuration/réseau/SQL avant application peut être retentée toutes les 15 minutes : **trois tentatives automatiques au maximum par occurrence**, et uniquement dans cette fenêtre. L'interruption par perte du worker consomme aussi une tentative. Erreur de validation, erreur projet isolée ou anomalie post-import : aucune relance automatique. La limite concerne les départs; elle n'ajoute pas trois occurrences.

Conserver un watermark durable de la dernière date évaluée. Un recul d'horloge ou un désaccord avec la configuration active ne recrée pas une ancienne occurrence : diagnostic et résolution opérateur. Les modifications d'horaire/fuseau prennent effet à la prochaine date non matérialisée, après arrêt ou fin du run actif.

### 3. Claim SQL, lease et exclusion du propriétaire périmé

Un contrôle SQL précréé par migration, clé `ERP_PROJECTS_TASKS`, sérialise **tous** les imports projets/tâches du site : global, ciblé, manuel, automatique et restauration. Il porte propriétaire de tentative, génération monotone `BIGINT`, expiration de lease, révision du protocole et références de publication. L'unicité quotidienne est une contrainte SQL indépendante de ce contrôle.

Claim dans une transaction courte : verrouiller la ligne de contrôle avec `UPDLOCK, HOLDLOCK`, lire l'heure SQL `SYSUTCDATETIME()`, recalculer depuis cet instant la date métier et l'admissibilité de l'échéance, vérifier propriétaire/expiration et occurrence, puis attribuer une nouvelle génération. L'horloge locale d'un nœud ne peut déclencher une occurrence en avance. La ligne est garantie présente; ne pas baser la sécurité sur une lecture d'absence non verrouillée. Un conflit d'unicité relit le gagnant.

Lease de 5 minutes, renouvelée toutes les 60 secondes par une connexion distincte sous condition `(attempt_id, generation)`. À défaut de renouvellement, le worker cesse de commencer du travail. Chaque tentative a aussi une durée maximale de 2 heures : le watchdog cesse de renouveler une tentative bloquée; le renouvellement seul ne prouve pas la progression.

**Chaque transaction d'écriture** verrouille d'abord la même ligne et vérifie propriétaire, génération, lease et deadline avant ses lectures décisionnelles. Elle garde ce verrou jusqu'au commit/rollback. Cela couvre données métier, checkpoints, résultats, progression, statuts et fraîcheur. Aucun fetch ERP n'a lieu dans cette transaction. Un ancien worker revenu après takeover ne peut écrire ni résultat tardif ni statut terminal du nouveau propriétaire. Les renouvellements et takeovers attendent une transaction déjà protégée; ils ne la chevauchent pas.

Le niveau normal `READ COMMITTED` convient avec ces verrous explicites; tester également une base avec RCSI. Ne pas supposer qu'un `with_for_update()` générique produit les hints SQL Server requis. Définir timeouts SQL bornés et ordre de verrous constant : contrôle ERP, puis gardes métier existantes. Une perte SQL ne donne jamais une permission locale d'importer.

Le CAS ERP n'est pas un remplacement d'ADR-006 et ne garde pas le Planning global verrouillé pendant tout l'import. Les commandes nécessitant déjà CAS Planning ou CAS du principal projet les conservent dans leur transaction respective.

### 4. Un orchestrateur, intentions manuelles explicites

Le bouton global de tâches et le nouveau déclenchement quotidien exécutent `PROJECTS_AND_ACTIVE_TASKS`, avec projets appliqués et validés avant tâches. Un bouton projets seul peut conserver l'intention `PROJECTS_ONLY`, avec le même moteur de phases, claim, validation, checkpoint et audit; il ne publie jamais un succès complet projets+tâches. L'outil ciblé sur un projet conserve sa portée explicite et les mêmes gardes; il ne vaut pas synchronisation globale. Aucun chemin d'écriture historique ne peut contourner le contrôle partagé.

Les lancements globaux renvoient rapidement une identité durable (`202`) puis exposent progression/résultat par GET; aucun thread de requête HTTP ne possède le cycle de vie du run. Le chemin global #540 et ses statuts restent compatibles par adaptation. La création d'un run tâches enfant n'autorise pas un second worker indépendant : le parent appelle les primitives de traitement avec son contexte de propriété.

Pour le bouton projets, introduire un endpoint asynchrone et migrer le frontend dans la même tranche. L'ancien `POST /projects/sync` avec réponse de compteurs `200` reste un adaptateur de compatibilité à la même intention `PROJECTS_ONLY`, sous les mêmes gardes et suivi durable; il ne contient plus une implémentation d'import indépendante. Une déconnexion du client n'annule pas le travail accepté. Le nouveau parcours UI n'attend jamais cette réponse longue. Le retrait de l'ancien contrat nécessite une évolution explicite distincte.

Une commande manuelle `RUN_NOW` crée une occurrence MANUAL distincte, identifiée par UUID et clé d'idempotence, même si la date quotidienne a réussi. Elle ne consomme pas artificiellement l'occurrence quotidienne. `RETRY(occurrence_id)` crée une nouvelle tentative de cette occurrence échouée/interrompue/manquée; une occurrence déjà réussie renvoie son résultat. Forcer un nouveau rafraîchissement après succès est `RUN_NOW`, jamais l'effacement de la preuve quotidienne.

Une répétition de la même commande retourne la même identité via les reçus existants, sans garder leur transaction ouverte pendant l'import. Une commande de portée différente rencontrant un import actif reçoit `409` avec référence de suivi autorisée; elle n'est pas empilée dans une queue cachée. Une répétition de la même occurrence retourne son état actuel.

### 5. Modèle durable spécifique et responsabilités

Ajouter les concepts SQL suivants par migrations futures; les noms exacts de tables suivent les conventions du dépôt :

| Concept | Autorité / contenu minimal |
|---|---|
| Contrôle ERP singleton | Propriétaire, génération, lease, heartbeat du scheduler, watermark, version de protocole/configuration, pointeurs de publications validées. |
| Occurrence ERP | Type DAILY/MANUAL, date métier, clé unique, configuration figée, échéance, décision MISSED/DISABLED, référence de dernière tentative et de succès. |
| Tentative d'orchestration | Occurrence + numéro unique, déclencheur SCHEDULED/CATCHUP/MANUAL/RETRY, acteur, timestamps UTC, phase, statut, métriques projets, validations, erreur sûre, état de checkpoint et publication, run tâches enfant éventuel. |
| Éléments de preuve bornés | Manifestes versionnés et chunks normalisés de source/checkpoint, identifiants de lignes, hashes, tailles, intégrité, état SEALED/READY/EXPIRED/PURGED et lien à la tentative. |

Réutiliser les runs et résultats projet de #540 pour **la phase tâches**; le parent décide seulement du workflow complet. Ne pas recopier leur détail comme seconde autorité dans un JSON parent. Ajouter leur rattachement à la tentative et un token de génération. L'unicité active #540 reste une défense, mais le parent n'utilise pas sa libération pour libérer le contrôle ERP trop tôt.

Les statuts enfant restent `PENDING/RUNNING/COMPLETED/COMPLETED_WITH_ERRORS/FAILED/INTERRUPTED`; `COMPLETED` signifie uniquement phase tâches terminée. Le parent ne devient `SUCCEEDED` que si ses propres phases et validations sont toutes réussies. Les projets locaux sans identité ERP et les projets inactifs sont hors cible, comptés comme exclusions explicites; ils ne produisent ni succès tâches fabriqué ni appels ERP dédiés.

États des tentatives : `PENDING`, `RUNNING`, puis exactement un terminal `SUCCEEDED`, `SUCCEEDED_WITH_ERRORS`, `FAILED` ou `INTERRUPTED`. Les terminaux ne sont pas rouverts; retry crée une tentative liée. `SUCCEEDED_WITH_ERRORS` signifie projets valides et phase tâches terminée avec des échecs locaux identifiés. `FAILED` couvre source/preflight invalide, phase projets échouée ou anomalie globale. Le besoin de récupération est un champ séparé `NONE/AVAILABLE/REQUIRED/RESOLVED/EXPIRED`, pas un faux succès.

### 6. Phases, snapshots complets et transactions

Ordre durable : `PREFLIGHT → SOURCES_VALIDATED → CHECKPOINT_READY → PROJECTS_VALIDATED → TASKS_FINISHED → POST_VALIDATED → PUBLICATION_COMMITTED`. Une phase est acquise uniquement par un reçu SQL committé, jamais par un curseur Python ou une ligne de log.

Le preflight vérifie configuration OData/credentials, accès aux feeds requis, SQL et schéma/protocole attendu, timezone, absence de propriétaire incompatible, occurrence, budget de stockage et capacité de produire/lire un checkpoint. Les preuves techniques sont relues à la frontière transactionnelle. Une connexion ERP réussie ne valide pas le contenu source.

Capturer et valider **les deux feeds complets avant la première mutation métier** d'un run complet. Déduire les cibles tâches de l'état projet attendu après upserts : statuts ERP explicites superposés aux projets locaux, absence du feed sans suppression. Figer les couples ProjectId/ProjectCode; résoudre les IDs locaux après l'upsert. Une collision ou un changement d'identité invalide le plan, sans rapprochement heuristique par nom. Inactifs exclus; projet actif local sans identité ERP signalé comme non éligible, jamais enrichi par hypothèse.

Les lectures projets et tâches ne constituent pas un snapshot transactionnel Acumatica commun : enregistrer leurs heures d'observation, pages et empreintes séparément. Aucun incrémental `LastModifiedDateTime` ni garantie de snapshot serveur non observée n'est inventé. Les sources normalisées SEALED sont immuables pour la tentative.

Appliquer les projets dans **une transaction de snapshot projet**, conservant le repository #207, son CAS principal et ses audits. Validation persistée des projets avant commit; échec = rollback de cette phase et aucune tâche. Appliquer ensuite chaque projet tâches dans sa propre transaction #540. Checkpoint de ses lignes, import, validation locale, résultat projet et provenance de fraîcheur sont atomiques. Corriger la fenêtre actuelle « commit métier puis résultat dans une autre transaction ». L'échec local fait rollback de ce projet; son diagnostic est enregistré après rollback sous garde valide, et les autres projets continuent.

La validation globale finale et le pointeur de réussite complète sont committés ensemble. Aucun HTTP long, appel ERP ou backup SQL dans une transaction d'application. Ne pas remplacer la frontière par projet par un commit géant.

### 7. Validation source et seuils

Projets : fin réelle de pagination, champs requis, unicité ProjectId **dans et entre les pages**, cohérence ProjectCode/identité locale, statuts explicites. Tâches : pagination DEPMO complète, cohérence ProjectId/ProjectCode, TaskID/TaskCD, ownership, champs requis et agrégation financière canonique. Plusieurs lignes budgétaires cohérentes d'un TaskID sont légitimes; ne pas les rejeter comme doublons ni sommer deux fois une page répétée. Si une identité stable de ligne budgétaire n'est pas disponible, une page répétée/non progressive ou une intégrité de pagination incertaine fait échouer la source au lieu de dédupliquer arbitrairement.

Standards TaskCD reste l'admissibilité workforce, **DEPMO reste le filtre financier distinct**. Des lignes valides hors Standards sont des exclusions métier expliquées, pas des erreurs de transport. Une ligne malformée non attribuable à un projet est une erreur globale; une anomalie de tâche isolable invalide le projet concerné avant son application. Ne pas appeler un snapshot amputé « réussi » parce que l'adaptateur a compté des rejets.

Seuils bloquants par défaut : au moins 1 projet source; au moins 1 ligne DEPMO lorsqu'il existe des cibles actives; compte source au moins **50 % du dernier snapshot réussi comparable**. Comparer séparément projets, source globale DEPMO et comptes par projet actif; les compteurs de tâches distinctes restent séparés des lignes financières. Une baisse locale n'invalide que ce projet, une baisse globale invalide la source entière. Un baseline nul n'a pas de ratio défini : appliquer le floor approuvé, sans division par zéro. Aucune tâche cible = phase explicitement vide, pas une fausse panne de feed. Un site légitimement vide peut adopter un floor zéro après approbation explicite du profil et du baseline, sans désactiver les validations structurelles.

Le ratio est un garde-fou conservateur configurable, pas une estimation statistique. Une hausse est avertie/limitée par la taille maximale; elle n'est pas rejetée pour sa seule valeur. Le premier baseline exige une revue manuelle des sources complètes; l'automatisation reste non activable sans baseline approuvé. Le baseline dépend du profil source/filtres/Standards figé : pas de comparaison silencieuse de populations différentes.

Un opérateur avec `sync_projects` **et** `admin_settings` peut approuver une exception de volume après preview. Elle est limitée à une empreinte source, une tentative et des seuils/projets explicitement nommés; raison, acteur et valeurs sont audités. Elle n'autorise jamais pagination partielle, identité incohérente, absence de checkpoint, erreur SQL ou bypass de claim. Une nouvelle source exige une nouvelle revue. Un feed de 4 ou 8 projets après 1 300 bloque avant mutation.

### 8. Checkpoint logique ciblé, transactions conservées

Retenir un checkpoint logique des **champs ERP réellement modifiables**, et non un backup complet chaque nuit. Les transactions restent le mécanisme de rollback technique. Le checkpoint sert à préparer une compensation contrôlée après un import committé présentant une anomalie; il ne garantit pas une restauration physique de toute la base.

Un manifeste READY et un budget réservé sont requis avant toute mutation. Dans chaque transaction d'application, capturer la before-image des lignes concernées **avant leur première écriture**, puis l'empreinte après import et le reçu de commit dans la même transaction. Une ligne créée a une preuve « absente avant », avec nouvel ID stable. Une tâche inchangée dont la provenance change est concernée. Aucun résultat n'annonce un checkpoint récupérable si sa transaction a rollbacké.

Allowlist : champs ERP possédés par le repository projets, champs ERP/budget des TaskCatalogEntry reçues, provenance de fraîcheur et métadonnées de succès par projet. Documenter la version de chaque DTO. Préserver les relations locales (co-chargés, contacts/responsabilités locaux, ressource préférée/version), WorkPackages, demandes, approbations, Shift et communications. Capturer les valeurs dérivées pour preuve; une compensation les recalcule par la politique canonique courante, sans restaurer silencieusement un ancien référentiel Standards/coûts. Les audits ERP/principal existants ne sont jamais effacés.

Stocker ces DTO normalisés dans SQL, en chunks d'au plus 1 MiB avant chiffrement; intégrité, version, nombre de lignes et tailles déclarées sont vérifiés à la lecture. Pas de dump XML, credential, URL secrète ou export de base. Réutiliser le chiffrement Fernet déjà disponible et la clé de site hors SQL/Git pour les payloads métier; le manifeste public ne contient que des métadonnées sûres. Une clé absente/illisible échoue au preflight; sa rotation doit conserver la capacité de lire les preuves retenues avant retrait de l'ancienne clé.

Limites initiales : **256 MiB déchiffrés par tentative**, toutes preuves comprises, et **2 GiB persistés au total**. Réserver avant import une enveloppe source + checkpoint estimée sur les lignes candidates; recontrôler la limite exacte avant chaque écriture, sans troncature. Un dépassement avant mutation bloque le run; un dépassement imprévu ultérieur rollbacke l'unité courante, conserve les commits précédents et exige résolution opérateur. Ce défaut ne devient jamais un succès global.

### 9. Reprise et restauration explicites

Au startup et à chaque tick, réconcilier seulement une propriété expirée sous verrou SQL, jamais tous les `RUNNING` d'un autre processus. Marquer l'ancienne tentative `INTERRUPTED` et son run tâches enfant de même, conserver résultats/checkpoints, retirer sa capacité d'écriture par nouvelle génération. Ne pas réutiliser le curseur mémoire.

Choisir la reprise la plus petite : **nouvelle tentative avec nouvelles lectures complètes**, replay idempotent des projets puis des tâches depuis le début. Les commits valides antérieurs restent en place et leurs reçus restent historiques; les nouvelles lectures peuvent légitimement rafraîchir les mêmes tâches. Ne pas estampiller « aujourd'hui » une ancienne source simplement relue du checkpoint. Cette règle évite un protocole de reprise de pages OData non garanti.

L'automatisation reprend une interruption uniquement dans la fenêtre et la limite de tentatives définies plus haut. Sinon, état observable et retry manuel. Une tentative en anomalie globale `recovery=REQUIRED` bloque les imports suivants jusqu'à décision opérateur; une erreur locale de projet reste isolée et n'impose pas une restauration globale.

La résolution opérateur choisit une compensation ciblée ou l'acceptation de l'état courant **après revalidation déterministe réussie des invariants**. Elle conserve raison, acteur et preuve de validation, marque le besoin de recovery résolu et permet un nouveau run. Elle ne convertit pas le terminal de l'ancienne tentative en succès, ne contourne pas une erreur structurelle et n'avance pas les horodatages ERP; une nouvelle lecture réussie reste nécessaire pour un nouveau succès complet.

**Aucune restauration automatique de données committées.** Prévoir preview puis confirmation explicite, avec `sync_projects + admin_settings`, sous le même contrôle ERP et une nouvelle identité idempotente. Le preview nomme lignes/champs, dépendances, provenance et conséquences sur la fraîcheur; sa confirmation est liée aux hashes courants et à la génération. Toute modification intervenante invalide le preview et provoque conflit.

Appliquer uniquement les champs ERP dont l'after-image correspond encore; jamais écraser une édition locale ou un import ultérieur. Le changement de principal projet conserve sa garde/audit canonique. Une ligne créée depuis l'import n'est pas supprimée : compensation par inactivation explicite uniquement si les dépendances le permettent, sinon refus et résolution dédiée. Aucun delete de WorkPackage/demande/Shift. Traiter une unité projet de compensation atomiquement; un lot multi-projets conserve des résultats explicites par unité, sans prétendre être un rollback global atomique. Toute compensation invalide la publication globale, remet les provenances concernées à leur état prouvé antérieur et ajoute un audit; elle ne les avance jamais.

### 10. Fraîcheur : tentative, succès local et succès complet

Conserver `TaskCatalogEntry.erp_budget_last_success_at` comme preuve de réception/intégration effective de **cette tâche**, y compris si inchangée, dans la même transaction que son budget. Fournir au repository l'heure d'observation du snapshot ERP effectivement intégré; stocker séparément l'heure de commit. Une reprise depuis un ancien artefact ne peut fabriquer une date récente. Le cutoff ADR-024 reste la date métier incluse de cette provenance, actuellement America/Toronto; il n'est ni la date de l'occurrence ni la date du succès global.

Les métadonnées projet portent dernière tentative, dernier succès validé, résultat/erreur courant et tentative productrice. Rendre `last_success_at` nullable pour un premier échec; ne pas initialiser un succès fictif. Les échecs actualisent la tentative/erreur sans avancer le succès. Une tâche absente, rejetée, hors import de secours ou seulement recalculée localement ne reçoit aucun nouveau cutoff.

Si les tâches de B échouent et celles de A/C réussissent : projets valides = rafraîchis; A/C = succès locaux; B = stale avec erreur; parent `SUCCEEDED_WITH_ERRORS`; **dernier succès complet tâches et projets+tâches inchangé**. Un nouveau run complet réussi avance le pointeur complet seulement après tous les contrôles.

Une anomalie globale post-import marque les publications de la tentative non fiables, sans rembobiner automatiquement les données. Relier les provenances productrices à cet état de qualité et exposer `VALID/STALE/UNKNOWN/QUARANTINED`. Les lecteurs Moyen terme et futurs outils #641 doivent consulter cette qualité : un cutoff issu d'une tentative quarantinée ne peut soutenir un budget restant présenté comme fiable; garder la preuve historique, afficher l'indisponibilité/diagnostic jusqu'à résolution. L'horodatage seul ne constitue pas un indicateur de santé.

### 11. Rétention, visibilité et exploitation

Après `SUCCEEDED` et publication atomique, purger les payloads source/checkpoint de façon idempotente; garder hashes, compteurs et preuve de purge. Après erreur/interruption, conserver les payloads 14 jours. Les preuves `recovery=REQUIRED` restent épinglées jusqu'à résolution explicite; leur budget est inclus dans les 2 GiB, et une saturation bloque les nouveaux imports plutôt que supprimer une preuve nécessaire.

Conserver les tentatives/résultats détaillés 90 jours, les tombstones d'occurrence/idempotence quotidienne 400 jours et le watermark durable. Préserver les références de dernier succès/qualité nécessaires au-delà de cette rétention sous forme de métadonnées compactes. Après expiration, retry d'une vieille occurrence refuse la réexécution historique; l'opérateur utilise `RUN_NOW`. La rétention des reçus de commande existants n'est pas raccourcie. Purge limitée par lot, sous gardes, sans supprimer un run actif, une preuve épinglée ou un audit métier.

Read model opérationnel réservé à `sync_projects` : scheduler configuré/désactivé/incomplet, dernière évaluation/heartbeat, dernière tentative, dernier succès complet et par projet autorisé, trigger, occurrence/date/fuseau, phase, début/fin/durée, échéance suivante, compteurs source/agrégés/acceptés/rejetés/créés/modifiés/inchangés/désactivés, projets échoués, âge/qualité, état et expiration des preuves, possibilité de retry/recovery. Aucun payload métier de checkpoint dans cette réponse.

Alertes minimales calculées : `MISSED`, `FAILED`, `SUCCEEDED_WITH_ERRORS`, absence de heartbeat pendant 5 minutes lorsque le service devrait être actif, âge du dernier succès complet > 30 heures, trois dernières tentatives non réussies. Si aucun succès prouvé : `UNKNOWN`, pas une date synthétique. Statut visible dans l'application et endpoint opérationnel, transitions structurées dans les logs existants. Pas d'envoi automatique mail/Teams ajouté par cette gate.

Un ERP indisponible dégrade l'état de synchronisation, pas la liveness du Web. `/health` ne provoque pas une boucle de restart pour une panne ERP. Si SQL est lui-même inaccessible, **aucun stockage SQL de diagnostic n'est possible** : logs sûrs et probes le signalent; après retour, enregistrer l'observation présente et le rattrapage sans inventer la chronologie de panne.

### 12. Identité, sécurité et configuration

Acteur planifié explicite `system:erp-daily-sync` dans le journal existant et la tentative; aucun faux AppUser. Les commandes manuelles gardent l'AppUser authentifié actif et `sync_projects`; activation/exception de volume/restauration exigent en plus `admin_settings`. L'UI ne fournit jamais un acteur système arbitraire. Les capacités/erreurs détaillées restent backend et respectent ADR-027/030; un utilisateur sans permission ne reçoit pas les projets échoués ni leurs données.

Secrets Acumatica/SQL/chiffrement injectés seulement par l'environnement du site. Provisionner un compte ERP dédié, lecture seule, avant activation production; une authentification HTTP Basic réussie ne prouve pas à elle seule qu'il s'agit d'un compte de service. Les diagnostics n'exposent ni credentials, host, SQL brut, stack trace sensible ni corps OData.

La configuration et ses bornes exactes figurent dans le contrat lié. Les profils Compose effectifs doivent transmettre username/password Basic, taille de page/timeout et nouvelles options; l'ancien Compose Synology limité aux variables REST historiques ne suffit pas. Tout désaccord de version/configuration entre instances empêche le claim; ne pas laisser chaque nœud appliquer son propre horaire.

### 13. Migrations et activation graduelle

Migrations additives, schéma/protocole versionnés, FKs réelles, unicités occurrence/tentative/projet, index de lecture/expiration, nullabilité de première réussite et liens de provenance. Seed du singleton dans la migration. Mettre à jour inventaires de schéma, matrice cutover, bridge historique et readiness SQL Server de façon exhaustive; ne pas classer les preuves métier durables comme simples caches effaçables.

Arrêter/drainer les anciens writers #540 avant la première activation; ils ne savent pas respecter une génération de lease. Tous les processus qui importent doivent supporter le protocole avant réactivation. Migration explicite avant démarrage; pas de `create_all` ni upgrade implicite dans le scheduler.

643A livre les primitives et la coordination mais **n'active pas l'automatisation d'import en production**. Le pipeline sûr annonce une version de capacité seulement lorsque 643B fournit les validations et checkpoints obligatoires; un garde non livré ne devient jamais un no-op. 643C livre les contrôles d'exploitation, le bootstrap du baseline et le runbook d'activation. Activer après validation de l'ensemble A/B/C; défaut désactivé jusque-là. Les adaptateurs manuels existants gardent leurs garanties historiques et la coordination dès A; B ajoute les nouveaux garde-fous communs avant activation de l'automatisation.

Promotion : désactivation/drain, migration, images de même protocole, probes SQL/schema/timezone et smoke, activation contrôlée. Un restart conserve occurrence et historique SQL externes. Rollback : désactiver/drainer d'abord et vérifier la compatibilité du binaire; une image pré-lease ne peut écrire en parallèle des nouveaux workers. Ne pas effacer les runs ni faire `alembic downgrade` automatiquement. Une migration descendante destructive de l'historique/preuves n'est pas une stratégie de rollback.

## Alternatives considered

| Alternative | Décision |
|---|---|
| Cron DSM/host déclenchant une URL | Rejeté : configuration externe au runtime versionné, dépendance HTTP/auth supplémentaire, aucune garantie de claim/reprise en soi. |
| Conteneur scheduler dédié dès V1 | Non retenu : même besoin de lease SQL, nouveau service/secret/promotion sans nécessité dans la topologie actuelle. |
| Timer UTC fixe / `sleep(86400)` | Rejeté : calendrier métier et DST incorrects. |
| Mutex mémoire / nombre de workers supposé égal à 1 | Rejeté : plusieurs processus/restarts contournent la garantie. |
| Lease sans garde de génération à chaque écriture | Rejeté : un ancien worker peut revenir écrire après expiration. |
| Transaction SQL globale des projets et de toutes les tâches | Rejetée : annule l'isolation par projet #540 et prolonge les locks. |
| Transactions seules comme reprise opérationnelle | Insuffisant : aucun before-image après des commits valides partiels. Elles restent obligatoires pour le rollback technique. |
| Backup SQL Server complet chaque nuit par l'application | Rejeté ici : privilèges/stockage/restauration de toute la base pour quelques champs ERP; la DR SQL Server externe reste une responsabilité d'exploitation indépendante. |
| Restore automatique sur tout échec | Rejeté : écraserait des succès locaux et des changements dépendants. |
| Réutiliser uniquement le run tâches #540 comme succès global ERP | Rejeté : confondrait projets, tâches et publication; parent spécifique avec enfant existant. |
| Celery/Redis/Kubernetes ou plateforme générique | Rejeté : aucun besoin établi par ce flux quotidien et ce déploiement. |

## Consequences

L'automatisation n'est plus liée à une requête HTTP ou à un seul processus. Les garanties #207/#525/#540 et le cutoff #614 sont préservés, la reprise est déterministe et les données partiellement rafraîchies restent identifiables. Les sauvegardes SQL Server demeurent la DR; les checkpoints ERP permettent seulement une compensation ciblée.

Coûts assumés : nouvelles métadonnées SQL, garde transactionnelle de tous les writers concernés, preuves chiffrées bornées, qualité de provenance à intégrer aux lectures budgétaires, tests réels SQL Server et discipline de déploiement multi-instance. La disponibilité SQL reste nécessaire; aucune garantie d'exécution à minuit pendant une panne complète n'est promise.

## Implementation notes

Le [contrat ASTRA-643](ASTRA-643-implementation-contract.md) fixe les paramètres, responsabilités A/B/C, tests et réponses aux scénarios de panne. Il est normatif pour cette décision; il ne crée aucune tranche supplémentaire ni changement d'ordre dans #55.

Cette ADR complète #207/#525/#540, précise la provenance de fraîcheur lors d'une orchestration/reprise, et conserve les règles de calcul ADR-024. Elle ne synchronise pas employés, utilisateurs ERP, classifications, Planner/Teams et n'écrit jamais vers Acumatica.

## References

- #55, #643, #207, #525, #540, #614, #641, #644, #693.
- [ADR-006](ADR-006-global-planning-mutation-version.md), [ADR-011](ADR-011-sql-server-authoritative-database.md), [ADR-017](ADR-017-erp-project-manager-and-rp-co-managers.md), [ADR-024](ADR-024-medium-term-work-package-cycle-demand-projection-and-erp-cutoff.md), [ADR-027](ADR-027-planning-visibility-role-and-authorized-scope.md), [ADR-030](ADR-030-module-read-capabilities-and-indirect-data-access.md).
- [Contrat OData](../ACUMATICA_ODATA_CONTRACT.md), [déploiement Ubuntu](../DEPLOYMENT_UBUNTU_VM.md), [procédure Synology historique](../DOCKER_SYNOLOGY.md).
- [Python 3.12 zoneinfo](https://docs.python.org/3.12/library/zoneinfo.html).
- [SQL Server table hints](https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table?view=sql-server-2017), [SYSUTCDATETIME](https://learn.microsoft.com/en-us/sql/t-sql/functions/sysutcdatetime-transact-sql?view=sql-server-ver15).

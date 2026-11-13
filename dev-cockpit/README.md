# RessourcePlanner Dev Cockpit

Outil de développement local, isolé fonctionnellement de l'application métier RessourcePlanner. Il observe GitHub, dérive l'état de développement et génère des contextes de reprise pour ChatGPT. Il n'intègre aucune API IA/OpenAI et ne pilote aucun agent. La seule mutation GitHub supportée est un **Safe Writeback explicite** du bloc canonique configuré, décrit plus bas; toutes les autres vues restent observationnelles.

## Source de vérité

Le cockpit ne crée aucun roadmap parallèle. Il distingue maintenant :

- le **roadmap humain** GitHub (`#55`);
- la **source machine configurée** pour `COCKPIT_PIPELINE_V1`.

Pendant **#842 Phase 2A**, les deux identités configurées pointent encore par défaut vers #55. L'issue **#851** existe uniquement comme **NON-CANONICAL MIGRATION CANDIDATE**; aucun cutover n'a encore eu lieu.

Il lit notamment :

- le roadmap humain GitHub configuré (`#55` par défaut);
- l'issue active et ses sous-tranches;
- les PR, branches, commits, Actions et jobs GitHub;
- `AGENTS.md` pour déterminer si l'enchaînement d'un même bloc est documenté;
- `docs/architecture/` pour signaler les ADR disponibles/applicables.

Une branche ou un commit n'est jamais considéré comme une preuve de `DONE`. Une PR fusionnée alors que l'issue/roadmap n'est pas explicitement à jour est signalée comme un décalage à régulariser.

## Architecture

```text
React + TypeScript + Vite
          ↓ same-origin /api
FastAPI Dev Cockpit
          ↓
GitHub REST API
```

Le build React est servi directement par FastAPI dans l'image finale. Aucune base SQL n'est utilisée.

## Docker Compose

Le service est disponible uniquement avec le profil `dev-tools` :

```bash
docker compose --profile dev-tools up -d --build
```

Adresses locales :

```text
RessourcePlanner  http://127.0.0.1:8080
Dev Cockpit       http://127.0.0.1:8081
```

Le service `dev-cockpit` est publié sur `127.0.0.1` seulement par défaut et utilise `restart: "no"`. Il doit donc être activé explicitement; un redémarrage de Docker/Ubuntu ne doit pas en faire un service persistant par accident.

Le runtime métier normal reste inchangé :

```bash
docker compose up -d --build
```

ne démarre pas le cockpit.

La cible de production est la VM Ubuntu documentée dans [`docs/DEPLOYMENT_UBUNTU_VM.md`](../docs/DEPLOYMENT_UBUNTU_VM.md). Sur cette VM de production, ne pas activer `dev-tools`, ne pas provisionner de `DEV_COCKPIT_GITHUB_TOKEN` et ne pas intégrer le cockpit aux commandes de promotion/rollback.

## Configuration

Variables lues depuis le `.env` racine par Docker Compose :

```dotenv
DEV_COCKPIT_GITHUB_TOKEN=
DEV_COCKPIT_REPOSITORY=tchi99/RessourcePlanner
DEV_COCKPIT_ROADMAP_ISSUE=55
DEV_COCKPIT_STALLED_AFTER_MINUTES=30
DEV_COCKPIT_HTTP_PORT=8081
```

`DEV_COCKPIT_GITHUB_TOKEN` peut rester un token GitHub local en lecture seule pour toutes les fonctions d'observation (Issues, PR, Actions, branches, détails). Pour utiliser **Appliquer à #55**, le même token doit en plus posséder l'autorisation **Issues: write** sur le dépôt surveillé. Le token :

- n'est jamais envoyé à React;
- n'est jamais retourné par l'API;
- n'est pas écrit dans les logs applicatifs;
- ne doit jamais être committé.

Sans token, `/api/dashboard` retourne une erreur de configuration explicite plutôt que d'essayer silencieusement un accès non authentifié.

## Rôles et conversations ChatGPT

Le cockpit affiche une petite équipe visuelle configurable (Product Owner, Developer et Architecte par défaut).

La configuration se fait entièrement dans l'interface avec **Gérer les rôles**. Aucun fichier JSON n'a besoin d'être modifié manuellement. Pour chaque rôle, l'UI permet de :

- modifier le nom et l'identifiant;
- choisir un avatar prédéfini;
- associer l'URL d'une conversation ChatGPT;
- activer/désactiver le rôle;
- réordonner ou supprimer le rôle;
- ajouter de nouveaux rôles.

Le lien ChatGPT ouvre la conversation dans un nouvel onglet. Le cockpit n'embarque pas ChatGPT dans un iframe et n'utilise aucune API OpenAI.

Les bulles affichées sur les cartes sont dérivées des données déjà connues du cockpit. Le rôle avec l'avatar `developer` reflète notamment READY, travail en cours, CI, mergeabilité et stalls; son avatar reçoit une animation CSS légère quand une activité de développement est observée. Les autres rôles affichent des informations contextuelles simples provenant du roadmap, des ADR ou de la CI.

Cette configuration est uniquement une **préférence locale de présentation**. Elle ne devient jamais une source de vérité sur l'état produit ou le roadmap.

### Panneaux interactifs par rôle

Les cartes de rôles servent aussi de points d'entrée vers un panneau latéral contextuel. Le panneau s'ouvre depuis l'avatar, le nom ou le bouton **Voir détails**.

Les vues spécialisées chargent maintenant le détail GitHub **à la demande** :

- **Product Owner** : roadmap maître complet; chaque issue devient un accordéon qui charge son corps GitHub réel, ses sections et sa documentation référencée;
- **Developer** : section exacte de la sous-tranche active extraite de l'issue parent, contexte complet de l'issue, HEAD de la branche active, statistiques/fichiers du commit, documentation et ADR associés, PR/CI, diagnostic de stall et prompt de reprise;
- **Architecte** : contenu réel de `docs/architecture/README.md`, contexte de la sous-tranche active, ADR pertinents puis autres ADR, tous lisibles dans des accordéons sans quitter le cockpit;
- **Reviewer** : jobs CI en échec et PR ouvertes;
- **Generic** : vue minimale et état de la conversation associée.

Le `Dashboard` reste volontairement léger. Les panneaux utilisent des routes de détail séparées :

```text
GET /api/details/roadmap
GET /api/details/issues/{number}
GET /api/details/commits/{sha}
GET /api/details/architecture
```

Ces routes utilisent le même accès GitHub en lecture seule et ne sont invoquées que lorsque la vue concernée est ouverte. GitHub demeure la source de vérité; aucun contenu n'est recopié dans une base locale.

Le panneau peut être fermé avec le bouton ×, en cliquant à l'extérieur ou avec la touche `Escape`. Sur mobile, il occupe toute la largeur.

### Firefox Companion — état réel des conversations

Le sous-dossier [`firefox-companion/`](firefox-companion/) contient une WebExtension Firefox locale. Elle permet d'animer n'importe quel rôle à partir de l'état réel de la conversation associée, sans API OpenAI.

Le matching se fait par l'URL de conversation configurée dans `chat_url` :

```text
rôle Product Owner
chat_url = https://chatgpt.com/c/abc
          ↓
Firefox Companion heartbeat pour /c/abc
          ↓
Product Owner → ChatGPT · working
```

États navigateur exposés par le cockpit :

- `working` : contrôle Stop de génération détecté;
- `idle` : conversation ouverte sans génération;
- `possible_stall` : la dernière trace était `working`, mais aucun heartbeat depuis 30 s;
- `disconnected` : la dernière trace était `idle`, puis le heartbeat a disparu.

Un passage `working → idle` conserve aussi brièvement un signal « réponse terminée ».

Le navigateur et GitHub restent deux sources séparées : un rôle Developer peut être animé par GitHub, par ChatGPT, ou par les deux. Pour les autres rôles, l'état ChatGPT suffit à déclencher l'animation.

Installation Firefox détaillée : [`firefox-companion/README.md`](firefox-companion/README.md).

### Persistance

Le backend sauvegarde la configuration dans :

```text
/data/roles.json
```

avec une écriture atomique. En Docker, `/data` est relié au volume nommé :

```text
dev-cockpit-data
```

Ainsi, un rebuild ou un redémarrage du conteneur conserve les rôles et liens configurés.

```bash
docker compose down
```

conserve le volume. En revanche :

```bash
docker compose down -v
```

supprime les volumes locaux, **incluant la configuration des rôles du cockpit**.

Les endpoints locaux correspondants sont `GET /api/roles` et `PUT /api/roles`. Ils restent servis uniquement par le backend cockpit sur l'interface loopback exposée par Compose.

### Résolution de la branche de travail active

Le cockpit ne doit pas utiliser le dernier commit de `main` pour déterminer l'activité du Developer.

Pour la tranche active, la résolution suit cet ordre :

1. branche `head` de la PR correspondante, si une PR existe;
2. sinon, recherche d'une branche dont le nom correspond à la clé de travail active (`332A`, `13C`, etc.);
3. la recherche parcourt **toutes les pages de branches GitHub**, et non seulement les 100 premières;
4. si plusieurs branches correspondent, celle dont le HEAD a le commit le plus récent est retenue.

La clé de travail active vient d'abord de l'état documenté dans l'issue/roadmap. Les sous-tranches marquées `✅` ou explicitement `DONE` sont considérées terminées; le resolver avance donc vers la première sous-tranche restante avant de rechercher sa branche.

Le commit de `main` affiché dans le footer est uniquement une référence sur l'état du dépôt. Les états `IN_PROGRESS`, `STALLED`, la dernière activité, les workflows associés et le détail Developer utilisent le HEAD de la branche active ou de la PR active.

## Pipeline canonique — source humaine et source machine séparées

GitHub reste la source de vérité du produit. Le cockpit ne conserve aucun état produit local.

Le backend distingue maintenant deux identités et un mode de source explicite :

- `roadmap_issue` — roadmap humain, actuellement **#55**;
- `canonical_pipeline_issue` — source machine de `COCKPIT_PIPELINE_V1`;
- `canonical_pipeline_source_mode` — contrat repository-owned valant `LEGACY_EMBEDDED` ou `DEDICATED_STRICT`.

Le mode ne dépend jamais de l'égalité numérique entre le roadmap humain et la source machine. `LEGACY_EMBEDDED` est réservé à la source historique embarquée **#55**; une combinaison incompatible échoue fermée. `DEDICATED_STRICT` exige un unique contrat V1 valide, sans fallback Markdown et sans marqueurs de versions concurrentes/non supportées.

**Pré-cutover #842 Phase 2A :** `canonical_pipeline_issue` reste fixé à **#55** avec `LEGACY_EMBEDDED`. L'issue **#851** est supportée pour validation explicite seulement et demeure **NON-CANONICAL**. Le changement de source active vers #851 et `DEDICATED_STRICT` est réservé à Phase 2B après autorisation PO.

Quand la source machine configurée contient le bloc versionné `COCKPIT_PIPELINE_V1`, ce bloc est autoritaire pour déterminer le focus courant, les horizons Maintenant/Ensuite, les gates, le prompt Developer et la clé utilisée pour rechercher une PR ou une branche. Le texte humain détaillé de #55 reste de la documentation et ne peut jamais remplacer la source machine en mode dédié.

Format exact :

```text
<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
ASTRA-399 | ARCHITECTURE_GATE | DONE | #399 | MAIN | analyse architecture de l'annulation
399A | WORK | READY | #399 | MAIN | état persistant + politique commune
399B | WORK | BLOCKED | #399 | MAIN | acceptation atomique humain + actif
408 | WORK | READY | #408 | PARALLEL | cycle de vie et filtres
ENV-263 | ENVIRONMENT_GATE | BLOCKED | #263 | MAIN | validation environnement
<!-- /COCKPIT_PIPELINE_V1 -->
```

Les six colonnes sont obligatoires et séparées par `|` :

- `KEY` est l'identité stable de l'étape. Un WORK utilise une issue ou sous-tranche (`399`, `399A`). Une gate d'architecture utilise `ASTRA-<issue>`; une gate environnementale utilise `ENV-<issue>`. Un numéro de PR, de CI ou de commit n'est jamais une identité d'étape.
- `TYPE` vaut exactement `WORK`, `ARCHITECTURE_GATE` ou `ENVIRONMENT_GATE`.
- `STATUS` vaut exactement `DONE`, `READY` ou `BLOCKED`.
- `PARENT` vaut exactement `#<issue>`. Pour un WORK, la partie numérique de la clé doit être égale au parent; `399A` appartient donc à `#399`.
- `LANE` vaut exactement `MAIN` ou `PARALLEL`.
- `TITLE` est le libellé humain court de l'étape et ne doit pas contenir `|`.

La lane `MAIN` est strictement ordonnée. Tant qu'elle contient une étape non terminée, elle doit avoir **exactement une** étape `READY`; toutes les étapes MAIN avant elle sont `DONE` et toutes celles après elle sont `BLOCKED`. Une lane MAIN entièrement terminée ne contient aucune étape `READY`. La lane `PARALLEL` peut exposer plusieurs étapes `READY` sans changer le focus principal.

Le parser valide notamment :

- marqueurs de début/fin uniques;
- en-tête et nombre de colonnes;
- clés uniques;
- types, statuts et lanes connus;
- syntaxe de `PARENT`;
- cohérence clé/parent;
- rejet explicite des identités PR/CI;
- ordre MAIN non ambigu et unicité du READY principal.

### Fail closed

Si au moins un marqueur `COCKPIT_PIPELINE_V1` est présent mais que le bloc est incomplet, invalide, ambigu ou incohérent, le cockpit **ne revient jamais** aux heuristiques Markdown historiques. Il retourne `pipeline.valid = false`, aucune étape `now`, aucune issue/tranche active, et un message demandant de corriger la source machine configurée. Le Developer ne reçoit aucun prompt lui demandant de démarrer une tranche inventée.

Si le bloc canonique est complètement absent **dans le mode historique où la source machine est encore #55**, le resolver historique reste disponible comme fallback de compatibilité. En revanche, une **source machine dédiée** utilise un resolver strict : bloc absent, version inconnue, marqueurs dupliqués, syntaxe invalide ou parent WorkItem introuvable rendent le pipeline invalide. Il n'existe alors aucun fallback vers le Markdown humain de #55 ni vers son ancien bloc embarqué.

Aucun état local n'enregistre qu'une étape ou une gate est terminée. Toute évolution du produit doit mettre à jour la **source machine configurée**. Pendant Phase 2A cette source reste #55; après un éventuel cutover Phase 2B, #55 restera le roadmap humain et seule l'issue machine dédiée portera ces transitions. Après fusion d'une tranche, son statut passe à `DONE`, la prochaine étape MAIN passe à `READY`, et les suivantes restent `BLOCKED`.

Le dashboard principal affiche une projection compacte sous **Maintenant / Parallèle disponible / Ensuite**. Le panneau **Product Owner** conserve la trajectoire détaillée sous **Maintenant / En parallèle / Ensuite / Plus tard**. Une gate READY reste une gate : elle ne devient jamais une tranche DEV et le prompt Developer interdit explicitement de lancer du travail applicatif tant qu'elle n'est pas passée à `DONE`.

### Roadmap Reconciler

Lorsque `COCKPIT_PIPELINE_V1` est valide, le dashboard compare désormais le contrat canonique à des preuves GitHub observables sans changer la source de vérité.

Le reconciler :

- inspecte les PR DEV correspondant aux étapes `WORK` non terminées;
- en mode canonique, la résolution de la PR active et des PR fusionnées réutilise la même **identité stricte de livraison** que le Reconciler : clé présente dans le titre ou la branche, ou référence structurée explicite (`Refs`, `Closes`, `Fixes`, `Slice`, `Tranche`) dans le body; une mention incidente telle que « 276D is intentionally out of scope » ne correspond jamais à 276D;
- exclut les PR documentation-only de la preuve de livraison;
- considère une étape `READY` comme **roadmap stale** seulement si une PR DEV correspondante est fusionnée et que les workflows observés sur son HEAD sont terminés et verts;
- signale une PR ouverte sur une étape future `BLOCKED` comme travail hors ordre, sans promouvoir cette étape;
- signale une PR fusionnée sur une étape `BLOCKED` sans réordonner le pipeline;
- ne déduit jamais l'état d'une `ARCHITECTURE_GATE` ou d'une `ENVIRONMENT_GATE` depuis une PR;
- ne s'exécute pas sur un pipeline canonique invalide et ne remplace jamais le comportement fail-closed.

Lorsqu'une étape MAIN `READY` est prouvée livrée, le cockpit génère une proposition déterministe qui :

1. passe cette étape de `READY` à `DONE`;
2. promeut la prochaine étape MAIN non terminée de `BLOCKED` à `READY`;
3. conserve l'ordre, les identités, les lanes et les titres;
4. produit le bloc `COCKPIT_PIPELINE_V1` complet à appliquer à la source machine configurée.

Une étape `PARALLEL READY` prouvée livrée peut être proposée `DONE` sans modifier la lane MAIN.

Le bloc proposé reste disponible à la copie manuelle. Lorsque le token local possède **Issues: write**, le cockpit permet aussi un Safe Writeback explicite.

### Safe Writeback de la source machine

Le Safe Writeback ferme la boucle du Reconciler sans créer de second état produit. Il cible **uniquement l'issue machine configurée** et ne peut écrire que lorsque le pipeline canonique est valide, que le Reconciler retourne `stale` et qu'une proposition déterministe existe déjà. Pendant Phase 2A, cette cible active reste #55; #851 n'est utilisée que dans des validations/configurations explicites.

Le parcours comporte deux étapes obligatoires :

1. **Voir le diff** appelle `POST /api/roadmap-writeback/preview`. Le backend reconstruit le dashboard depuis GitHub, relit l'issue machine configurée et vérifie que le bloc analysé correspond encore exactement au bloc courant. La réponse contient le diff, `updated_at`, le SHA-256 du body complet et le SHA-256 de la proposition.
2. **Confirmer et appliquer à #<pipeline>** appelle `POST /api/roadmap-writeback/apply`. Le backend relit l'issue machine, vérifie `updated_at + body SHA-256`, recalcule le Reconciler, vérifie que la proposition n'a pas changé, relit encore immédiatement avant le PATCH, puis remplace uniquement `COCKPIT_PIPELINE_V1`.
3. Après PATCH, le backend effectue un **GET indépendant** et exige le body exact, le SHA-256 exact, un parse V1 valide et le bloc proposé exact avant de rapporter le succès.

Invariants :

- aucun bloc envoyé par le navigateur n'est utilisé comme autorité : l'apply recalcule la proposition côté serveur;
- le Markdown avant et après `COCKPIT_PIPELINE_V1` reste octet-pour-octet identique;
- un pipeline legacy, invalide ou non-stale ne peut pas être écrit;
- aucune gate d'architecture/environnement n'est automatiquement déclarée terminée;
- une modification concurrente de la source machine configurée produit `ROADMAP_CHANGED` (HTTP 409) et aucune écriture;
- une proposition différente de celle prévisualisée produit `PROPOSAL_CHANGED` et exige un nouveau diff;
- la réponse PATCH est validée puis une relecture GET indépendante doit confirmer exactement le résultat attendu;
- aucune mutation n'est déclenchée par polling, refresh ou Attention Center : l'utilisateur doit cliquer sur la confirmation finale.

GitHub Issues ne fournit pas au cockpit un champ CAS applicatif transactionnel. La protection est donc un verrou optimiste fort basé sur **`updated_at + SHA-256 du body complet`**, vérifié deux fois avant le PATCH, puis vérifié après l'écriture. Le cockpit échoue fermé dès qu'un écart est observé.

Le texte humain de #55 n'est volontairement pas réécrit par ce mécanisme une fois la source machine séparée : le Safe Writeback est limité au contrat machine `COCKPIT_PIPELINE_V1`. Pendant Phase 2A, l'autorité active reste néanmoins le bloc embarqué de #55 jusqu'au cutover explicitement autorisé.

### Attention Center

Le dashboard commence maintenant par un **Attention Center** qui agrège les signaux déjà dérivés par le pipeline canonique, le Roadmap Reconciler, le Contrôleur d'exécution et les Handoff Packs.

Il n'ajoute aucun état produit : la file est recalculée à chaque lecture GitHub.

Trois niveaux seulement sont exposés :

- **ACTION** : une intervention humaine est requise maintenant;
- **WATCH** : aucun geste immédiat n'est requis, mais un état doit être surveillé;
- **CLEAR** : aucun signal actionnable n'est présent.

Exemples :

- CI rouge / stall confirmé → ACTION Developer;
- PR verte et mergeable → ACTION Reviewer;
- roadmap stale ou pipeline invalide → ACTION Product Owner;
- gate architecture → ACTION Architecte;
- CI en cours / possible stall / développement en cours → WATCH;
- PR ouverte sur une étape canonique BLOCKED → WATCH Product Owner;
- Handoff Pack PARTIAL → WATCH du rôle uniquement si aucun signal plus fort ne couvre déjà le même rôle et la même tranche.

La déduplication se fait par **rôle + sujet** et conserve toujours le signal le plus fort (`ACTION > WATCH`). Une CI rouge ne produit donc pas trois entrées distinctes parce qu'elle est également visible dans les missions et le Handoff Pack.

Chaque entrée propose **Préparer la reprise**, qui copie le Handoff Pack déjà dérivé pour le rôle, ainsi que le lien GitHub primaire lorsqu'il existe. Il n'existe aucun bouton dismiss : l'entrée disparaît uniquement lorsque la condition GitHub qui l'a créée disparaît.

Le résumé supérieur affiche le nombre d'actions et d'attentes ainsi qu'une ventilation par rôle. Quand aucun signal ne subsiste, l'état devient explicitement **CLEAR — Aucune intervention requise**.

### Contrôleur d'exécution

Le cockpit dérive maintenant une **phase d'exécution** à partir du contrat canonique, du Roadmap Reconciler et des artefacts GitHub de la tranche active. Cette phase n'est jamais persistée localement.

Phases principales :

- `PIPELINE_INVALID` : le contrat #55 est invalide; aucun nouveau travail ne doit être lancé;
- `ROADMAP_UPDATE_REQUIRED` : une livraison GitHub vérifiée n'est pas encore reflétée dans #55;
- `ARCHITECTURE_GATE` / `ENVIRONMENT_GATE` : la prochaine étape est une gate, pas du développement applicatif;
- `READY` : la tranche canonique est prête et aucune branche/PR active n'est détectée;
- `DEVELOPING` : une branche ou un travail actif existe sans PR prête;
- `PR_OPEN` : une PR correspondante est ouverte;
- `CI_RUNNING` : les checks de la tranche sont en cours;
- `CI_RED` : les checks ont échoué et doivent être corrigés;
- `STALLED` / `POSSIBLE_STALL` : le signal GitHub indique une interruption probable ou possible;
- `READY_TO_MERGE` : la PR est verte et mergeable;
- `DELIVERY_UNVERIFIED` : une PR est fusionnée mais la preuve de livraison n'est pas assez forte pour promouvoir le roadmap;
- `NO_ACTIVE_WORK` : aucune étape MAIN exécutable n'est déclarée.

Chaque phase expose un label, un résumé, la prochaine action, le rôle responsable, un prompt de reprise et un lien primaire lorsque GitHub en fournit un.

Le contrôleur donne toujours priorité à la cohérence du roadmap : si le reconciler retourne `stale`, la phase devient `ROADMAP_UPDATE_REQUIRED` même si une autre activité GitHub est observable. Le cockpit demande alors de réconcilier #55 avant de lancer implicitement la tranche suivante.

### Missions par rôle et handoff ChatGPT

Le même état dérive une mission pour chaque avatar standard :

- **Product Owner** : cohérence du pipeline, décisions/gates et réconciliation #55;
- **Developer** : démarrage, reprise, correction CI ou attente selon la phase;
- **Architecte** : gate architecture active ou disponibilité sans nouvelle décision;
- **Reviewer** : jobs rouges, revue finale ou attente des checks;
- **Generic** : reprend l'action du contrôleur sans logique métier supplémentaire.

Ces missions ne sont pas stockées dans `roles.json`; seul le nom, l'avatar et le lien ChatGPT restent des préférences locales. Les missions sont recalculées à chaque lecture du dashboard.

Les cartes de rôles affichent la mission courante et son état (`action`, `waiting`, `clear`, `blocked`). Le bouton de handoff copie le prompt de mission et, si une conversation ChatGPT est configurée, ouvre cette conversation dans un nouvel onglet. Le panneau de détail de chaque rôle expose aussi la mission et le même handoff.

### Handoff Pack par rôle

Le bouton de reprise ne copie plus seulement la mission courte. Le cockpit génère maintenant un **Handoff Pack** éphémère pour chaque rôle à partir du même état GitHub utilisé par le dashboard.

Le pack contient, lorsqu'ils sont disponibles :

- l'étape canonique, son type/statut et la phase d'exécution;
- l'issue parent et la sous-tranche exacte;
- la prochaine action dérivée;
- la branche active, la PR, le dernier commit et la CI observée;
- les jobs rouges;
- les ADR référencés par l'issue/roadmap actif;
- la chaîne de sous-tranches restante lorsque l'enchaînement est explicitement permis;
- la section exacte de l'issue parent correspondant à la sous-tranche;
- les contraintes durables pertinentes d'AGENTS.md;
- une consigne de reprise adaptée au rôle qui interdit de refaire inutilement l'exploration déjà disponible.

Aucun de ces éléments n'est inventé. Une branche, PR, section ou ADR absent reste absent du pack.

Chaque pack expose une confiance :

- **COMPLETE** : le contexte observable est suffisant pour reprendre directement la mission;
- **PARTIAL** : le pack est exploitable, mais une preuve attendue manque, par exemple une tranche marquée en développement sans branche ni PR observable;
- **BLOCKED** : la mission ne doit pas démarrer/reprendre dans l'état actuel, par exemple pipeline invalide, roadmap stale ou gate active pour le Developer.

La confiance est calculée par rôle : un roadmap stale peut produire un pack Developer **BLOCKED** tout en laissant au Product Owner un pack **COMPLETE** pour effectuer la réconciliation.

Le bouton **Préparer la reprise** copie le pack complet. Si une conversation ChatGPT est associée au rôle, le cockpit copie d'abord le pack puis ouvre la conversation dans un nouvel onglet. Il n'envoie aucune donnée via une API IA et ne persiste pas le pack dans `roles.json`.

### Timeline GitHub

Le contrôleur reconstruit une timeline courte uniquement depuis les horodatages GitHub disponibles :

- dernière mise à jour du roadmap;
- dernier commit de la branche/PR active;
- création de la PR lorsque GitHub fournit `created_at`;
- démarrage et fin des workflows GitHub Actions;
- fusion d'une PR active détectée comme non encore matérialisée dans le roadmap.

La timeline n'invente aucune date de création de branche : l'API de branches utilisée par le cockpit n'en fournit pas. Elle reste donc une projection éphémère des événements observables, sans journal local ni second stockage d'état.

### Flow Analytics / Delivery History

La vue **Santé du flux** reconstruit un historique de livraison à partir des seules preuves GitHub observables. Elle ne crée aucun journal local et n'est volontairement pas chargée dans le polling principal du dashboard.

L'analyse est disponible via :

```text
GET /api/flow-analytics?repo=owner/repo&limit=12
```

Elle est chargée à la demande depuis l'interface avec **Charger l'analyse du flux** / **Rafraîchir l'analyse**. Le refresh du dashboard toutes les 60 secondes n'appelle pas cet endpoint.

Périmètre :

- seules les étapes `WORK DONE` du `COCKPIT_PIPELINE_V1` sont candidates;
- la PR de livraison doit correspondre à l'identité stricte déjà utilisée par le Roadmap Reconciler;
- les PR docs-only sont exclues des livraisons DEV;
- le premier commit observable vient de l'historique de commits de la PR;
- les cycles CI viennent des workflows GitHub Actions attachés aux SHA de la PR;
- le merge vient du timestamp `merged_at` GitHub.

Pour chaque livraison, le cockpit peut calculer lorsque les timestamps existent :

- premier commit → ouverture PR;
- ouverture PR → première tentative entièrement verte;
- CI verte → merge;
- durée totale observable premier commit → merge;
- nombre de SHA ayant déclenché au moins un workflow;
- nombre de tentatives rouges;
- délai observable entre la fin d'une tentative rouge et le début de la tentative suivante;
- segment observable le plus long comme principal ralentissement descriptif.

Une tentative de validation correspond à **un SHA de la PR avec au moins un workflow**. Elle est `green` seulement si tous les workflows sont terminés sans échec et qu'au moins un workflow conclut `success`; elle est `red` si au moins un workflow conclut en échec; sinon elle reste `pending/unknown`.

Le résumé expose des médianes et moyennes sur les livraisons analysables. Une tendance PR → CI verte n'est produite qu'avec au moins six livraisons comparables, en comparant descriptivement la moitié ancienne et la moitié récente. Aucun score de performance opaque n'est introduit.

Limites assumées :

- aucune date de création de branche n'est inventée;
- les stalls historiques ne sont pas reconstruits, car le cockpit ne les persiste pas;
- le timestamp exact de réconciliation historique de #55 n'est pas déduit de l'`updated_at` courant de l'issue;
- si l'historique de workflow d'un commit n'est pas lisible, la livraison devient partielle plutôt que de fabriquer une mesure;
- pour limiter les appels GitHub, l'analyse CI d'une très grosse PR est bornée aux 20 derniers commits, tout en conservant le premier commit de la PR pour la durée totale.

Ainsi, Flow Analytics reste une projection analytique éphémère de GitHub, cohérente avec la règle générale du cockpit : **mesurer ce qui est observable et afficher indisponible plutôt que d'estimer silencieusement**.

### PR d'architecture vs travail Developer

Une PR liée à l'issue active n'est pas automatiquement considérée comme une PR d'implémentation. Le Cockpit inspecte les fichiers changés des PR qui correspondent à la tranche active :

- une PR dont tous les fichiers sont documentaires (`docs/**` ou fichiers Markdown) reste visible dans les listes générales mais n'est pas utilisée comme `primary_pr` du Developer;
- une PR docs-only fusionnée ne déclenche pas l'état « PR fusionnée mais tranche non terminée »;
- une PR qui touche du code, des tests, des migrations, de la configuration ou tout autre fichier non documentaire reste une PR DEV;
- si la liste des fichiers d'une PR n'est pas disponible, le Cockpit reste conservateur et la considère comme potentiellement DEV plutôt que de masquer un vrai travail.

Cela permet à une analyse d'architecture de matérialiser un ADR dans une PR sans faire croire que la première sous-tranche DEV a commencé ou a été livrée.

Quand `AGENTS.md` autorise l'enchaînement et que l'issue documente un ordre obligatoire, le prompt Developer énumère désormais explicitement la chaîne restante (par exemple `291A → 291B → …`) au lieu d'un simple « poursuivre le bloc ».

## Détection du travail interrompu

Le cockpit ne considère pas le dernier commit global du dépôt comme un signal de stall. Il observe uniquement l'activité GitHub pertinente pour la tranche active.

Le même seuil `DEV_COCKPIT_STALLED_AFTER_MINUTES` alimente trois niveaux :

```text
STALLED_CONFIRMED
CI rouge + aucun workflow actif + aucune reprise/commit depuis le seuil

STALLED
branche ou PR de la tranche active + aucune activité pertinente depuis le seuil
+ aucun workflow actif

POSSIBLE_STALL
tranche explicitement marquée 🟡 / en cours dans GitHub
+ aucune branche/PR/workflow associé visible
+ aucun changement pertinent depuis le seuil
```

L'activité pertinente est dérivée du commit de la branche active, de la PR, des workflows du SHA actif et, lorsqu'aucun artefact de travail n'existe encore, de la mise à jour explicite de l'issue active. Un workflow en cours empêche toujours l'état de stall.

Cette logique est volontairement prudente : elle peut signaler qu'un Dev ChatGPT semble arrêté, mais elle ne prétend pas connaître l'état de l'interface ChatGPT ni un travail non publié qui n'existe pas dans GitHub.

## Développement local hors Docker

Backend :

```bash
python -m pip install -r dev-cockpit/backend/requirements.txt
cd dev-cockpit/backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Frontend :

```bash
cd dev-cockpit/frontend
npm install
npm run dev
```

Vite écoute sur `127.0.0.1:5174` et relaie `/api` vers `127.0.0.1:8001`.

## Tests

```bash
cd dev-cockpit/backend
python -m unittest discover -s tests -v
cd ../frontend
npm install --no-audit --no-fund
npm run build
```

Validation Compose :

```bash
docker compose config
docker compose --profile dev-tools config
```

Smoke ciblé du chemin réellement documenté :

```bash
docker compose --profile dev-tools up -d --build
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:8081/api/health
curl http://127.0.0.1:8081/api/config
```

La CI vérifie également que `docker compose config --services` n'inclut pas `dev-cockpit` sans profil, alors que `docker compose --profile dev-tools config --services` l'inclut.

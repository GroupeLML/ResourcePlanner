# Reprise 573E — ancienne FK du chargé de projet

## Autorités finales

ADR-017 reste l'unique décision architecturale applicable :

- le principal est le fait ERP porté par `Project.project_manager_external_id` et
  `Project.project_manager_name`;
- sa résolution locale suit exclusivement
  `project_manager_external_id -> AppUser.employee_external_id ->
  AppUser.business_contact_id -> BusinessContact`;
- les co-chargés sont des nominations explicites `ProjectCoManager`;
- `Project.project_manager_contact_id` est une donnée historique de diagnostic,
  jamais une autorité métier.

Aucune résolution du principal par nom, courriel, `UserID`, `Resource.external_id`
ou ancienne FK n'est autorisée.

## Audit statique de départ

L'audit de 573E sur `main@f4457eab6bf457bf59b4d8a0eaf263292ce357d2`
a trouvé **61 occurrences textuelles exactes** de
`project_manager_contact_id` dans les fichiers déjà présents. Elles ont été
classées selon leur rôle réel :

| Catégorie | Exemples | Décision |
| --- | --- | --- |
| `SCHEMA` | `app/infrastructure/sql/models.py` | KEEP — colonne historique conservée |
| `MIGRATION` | baseline 0001, bridge SQLite 0048 | KEEP — compatibilité de schéma |
| `HISTORICAL_COMPATIBILITY` | ADR-017 et documentation d'intégration | KEEP — explique la transition |
| `DIAGNOSTIC` | outil 573E et son repository | KEEP — lecture read-only explicite |
| `API_COMPATIBILITY_DERIVED` | Moyen terme, admin contacts | KEEP — valeur dérivée du principal canonique |
| `TEST_FIXTURE` | tests de divergence, seed et E2E | KEEP — prouve que la legacy n'est pas autoritaire |
| `ACTIVE_BUSINESS_DECISION` | aucun | **0** |

Les champs de compatibilité conservés sont alimentés depuis
`ProjectManagerResolutionService`. Un champ de réponse encore nommé
`project_manager_contact_id` n'est donc pas équivalent à une lecture de la
colonne persistée historique.

Les fixtures qui renseignent encore la FK sont conservées uniquement lorsqu'elles
simulent l'histoire, la divergence ou une compatibilité. Elles ne constituent pas
une source métier.

## Diagnostic read-only

Le diagnostic opérateur est :

```bash
RESOURCEPLANNER_DATABASE_URL="..." \
python tools/diagnose_project_manager_legacy_links.py --json
```

Pour rendre une divergence bloquante dans une procédure contrôlée :

```bash
RESOURCEPLANNER_DATABASE_URL="..." \
python tools/diagnose_project_manager_legacy_links.py --json --fail-on-review
```

Le mode par défaut et le mode `--fail-on-review` sont tous deux strictement
read-only. L'outil ne contient aucune commande `INSERT`, `UPDATE` ou
`DELETE`, ne crée aucun audit métier et n'entretient pas l'ancienne FK.

Le rapport expose uniquement les identifiants utiles, les libellés métier déjà
nécessaires au diagnostic, les états d'activation et les codes de diagnostic.
Il ne publie pas de courriels ni de numéros de téléphone.

## Classifications

- `LEGACY_ABSENT` : la FK historique est nulle; cet état est acceptable.
- `LEGACY_MATCHES_CANONICAL` : la FK historique et le contact du principal
  canonique concordent; aucune nomination RP n'est créée.
- `LEGACY_DIVERGES_FROM_CANONICAL` : la FK historique pointe vers une autre
  personne; le principal canonique reste inchangé.
- `CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE` : l'identité ERP existe mais
  sa chaîne locale n'est pas résolue; la legacy n'est jamais utilisée comme
  fallback.
- `LEGACY_REFERENCE_BROKEN` : la FK historique pointe vers un contact absent.
- `LEGACY_PROJECT_MANAGER_CONTACT_INACTIVE` : diagnostic additionnel lorsqu'un
  contact historique existe mais est inactif.

Le résumé fournit :

```text
projects_scanned
legacy_null
legacy_matching
legacy_divergent
legacy_broken
canonical_unresolved
legacy_inactive
```

## Procédure de reprise humaine

Une divergence n'est jamais transformée automatiquement en co-chargé. Le traitement
opérateur consiste à :

1. confirmer le principal dans l'ERP et corriger la donnée ERP si nécessaire;
2. corriger explicitement la chaîne `AppUser.employee_external_id ->
   business_contact_id` lorsqu'une identité locale manque;
3. si une vraie décision de co-gestion est requise, utiliser les commandes
   d'administration 573D pour nommer explicitement le co-chargé;
4. conserver la legacy comme historique tant qu'aucune décision séparée ne justifie
   son effacement.

« Effacer l'historique » et « nommer un co-chargé » sont deux intentions distinctes.
573E n'ajoute volontairement aucune commande de mutation de la legacy.

## Performance et SQL Server

Le diagnostic charge les projets en lot, appelle
`ProjectManagerResolutionService.resolve_projects(...)` une seule fois, puis
charge les contacts legacy en lot. Il n'appelle jamais `resolve_project(...)`
dans une boucle.

La preuve finale 573E couvre aussi SQL Server réel : migration au head, diagnostic
read-only et concurrence multi-session des co-chargés. Une validation offline
`check_sqlserver_readiness.py` ne remplace pas cette preuve live.

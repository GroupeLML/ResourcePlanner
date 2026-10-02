# Acumatica — contrat RP_Employees / RP_Users

## Décision produit

Les employés et les utilisateurs Acumatica représentent deux concepts distincts dans RessourcePlanner :

- **RP_Employees** fournit les personnes qui peuvent devenir des ressources planifiables;
- **RP_Users** fournit les personnes qui peuvent avoir accès à RessourcePlanner via OIDC.

Une personne peut exister comme employé sans être utilisateur, et un utilisateur reste soumis à l'autorisation locale RessourcePlanner.

La synchronisation ERP ne doit jamais activer automatiquement une ressource ni autoriser automatiquement un utilisateur dans RessourcePlanner.

## RP_Employees

### Feed

```text
/oDATA/RP_Employees
```

Format observé : OData Atom/XML.

Type d'entité observé :

```text
PX.Data.RP_Employees
```

Fixture contractuelle anonymisée :

```text
tests/fixtures/acumatica/rp_employees_atom.xml
```

### Identité

Décision confirmée par le PO :

```text
RP_Employees.EmployeID = clé unique Employee
```

La clé RessourcePlanner est donc :

```text
Resource.external_id = trim(RP_Employees.EmployeID)
```

`ContactID` n'est pas la clé de ressource RessourcePlanner. Il peut être conservé comme référence ERP secondaire si un besoin métier concret l'exige plus tard.

### Champs observés

| Champ OData | Sémantique | Usage cible |
| --- | --- | --- |
| `EmployeID` | clé unique employé | identité externe stable de la ressource |
| `DisplayName` | nom affiché | nom descriptif |
| `Email` | courriel | descriptif, jamais clé |
| `Status` | état employé ERP | état source ERP |
| `DepartmentCodeDescription` | libellé département/poste | descriptif / filtre futur |
| `DepartementCode` | code département | attribut organisationnel |
| `EmployeeClass` | classe ERP | attribut ERP; ne remplace pas automatiquement la classe Planning locale sans décision explicite |
| `SupervisordID` | EmployeID du superviseur, nullable | relation hiérarchique ERP |
| `Telephone` | téléphone, nullable | descriptif |
| `BranchCode` | division/branche | attribut organisationnel; appliquer `strip()` |
| `ContactID` | identifiant contact ERP Int32 | référence secondaire |

Les champs paddés avec `xml:space="preserve"` doivent être normalisés avec `strip()` lorsqu'ils représentent des identifiants/codes.

## RP_Users

### Feed / synchronisation complète

Chemin de base :

```text
/oDATA/RP_Users
```

Requête utilisée par la synchronisation complète 468B :

```text
/oDATA/RP_Users?$orderby=UserID asc
```

Le runtime ne filtre plus le feed sur `EmployeStatus`. Il importe l'annuaire retourné avec ses états source afin de conserver séparément `UserActif`, `EmployeStatus`, `local_active` et les rôles locaux. L'admissibilité reste calculée localement; une entrée ERP inactive n'est ni supprimée ni activée localement par la synchronisation.

Cette évolution est couverte par fixtures/tests locaux. Elle ne constitue pas un nouveau smoke réel Acumatica.

Format observé : OData Atom/XML.

Type d'entité observé :

```text
PX.Data.RP_Users
```

Fixture contractuelle anonymisée :

```text
tests/fixtures/acumatica/rp_users_atom.xml
```

La fixture anonymisée contient volontairement plusieurs variantes d'état afin de tester le parser; elle ne doit pas être interprétée comme la sortie exacte du filtre de production.

### Identité et relation Employee

Décisions confirmées par le PO :

```text
RP_Users.UserID = clé unique User
RP_Users.EmployeID = FK logique vers RP_Employees.EmployeID
```

La relation canonique est donc :

```text
RP_Users.UserID
        │
        └── RP_Users.EmployeID
                ↓
        RP_Employees.EmployeID
                ↓
        Resource.external_id
```

Le nom et le courriel ne doivent jamais être utilisés comme jointure autoritaire.

## Relations projet / employé confirmées

Le feed `RP_Projects` expose le chargé de projet dans le même domaine d'identité `EmployeID`.

Mapping réel confirmé :

```text
trim(RP_Projects.ProjectManagerId)
        =
trim(RP_Employees.EmployeID)
        =
trim(RP_Users.EmployeID)
```

Exemple observé :

```text
RP_Projects.ProjectManagerId = "TROTJCHA  "
RP_Users.EmployeID           = "TROTJCHA  "
RP_Employees.EmployeID       = "TROTJCHA  "
RP_Users.UserID              = "JCTROTTIER"
```

Conséquences :

- `ProjectManagerId` doit être interprété comme un `EmployeID`;
- `RP_Users.UserID` reste la clé du compte utilisateur ERP et ne participe pas à la jointure projet → employé;
- `ProjectManagerName`, `UserDisplayName`, `DisplayName` et les courriels sont descriptifs uniquement;
- tous les identifiants paddés doivent être normalisés avec `strip()`;
- `RP_Employees.SupervisordID` référence lui aussi un `EmployeID` et appartient au même domaine d'identité.

Le chemin de rattachement métier RessourcePlanner est :

```text
RP_Projects.ProjectManagerId
        ↓ EmployeID
Project.project_manager_external_id
        ↓ résolution à la lecture
AppUser.employee_external_id
        ↓
AppUser.business_contact_id
        ↓
BusinessContact
```

Si aucun `AppUser` ou `BusinessContact` local correspondant n'existe, la synchronisation conserve l'identité ERP et les libellés descriptifs sans inventer une relation par nom ou courriel.

468B a initialement matérialisé ce rattachement dans `Project.project_manager_contact_id`. ADR-017 / 573C remplace ce comportement : lorsque `ProjectManagerId` est explicitement fourni, il est normalisé avec `strip()` puis stocké comme `Project.project_manager_external_id`; la résolution `AppUser.employee_external_id → business_contact_id → BusinessContact` est effectuée par le resolver canonique 573B au moment de la lecture. La synchronisation ne crée aucun `AppUser`, aucun `BusinessContact`, ne touche aucun `ProjectCoManager` et ne met plus à jour `project_manager_contact_id`. Un import incomplet qui omet l'identité du chargé conserve le principal ERP existant.

### Champs observés

| Champ OData | Sémantique | Usage cible |
| --- | --- | --- |
| `UserID` | clé unique utilisateur ERP | identité externe de l'entrée User ERP |
| `EmployeID` | FK logique Employee | liaison vers la ressource via `trim()` |
| `UserDisplayName` | nom affiché utilisateur | descriptif |
| `UserFirstName` | prénom/libellé ERP | descriptif |
| `UserLastName` | nom | descriptif |
| `UserEmail` | courriel | descriptif, jamais clé |
| `UserActif` | état du compte utilisateur ERP | état source ERP |
| `EmployeName` | nom employé | descriptif |
| `EmployeStatus` | état Employee ERP | diagnostic / garde-fou |

`UserActif` et `EmployeStatus` sont deux états distincts et ne doivent pas être fusionnés.

## Activation locale RessourcePlanner

### Principe commun

L'état provenant d'Acumatica et l'autorisation locale RessourcePlanner sont deux dimensions différentes.

Une nouvelle entrée synchronisée doit être **désactivée par défaut dans RessourcePlanner**, même lorsque son état ERP est actif.

La synchronisation ne doit pas écraser silencieusement la décision locale de l'administrateur.

### Ressources

Conceptuellement :

```text
ERP employee status       local RP enabled
        │                        │
        └──────────┬─────────────┘
                   ↓
          effective plannable
```

Une ressource est effectivement planifiable seulement si :

```text
employee ERP actif
AND
ressource activée localement par un ADMIN
```

Lors de la première synchronisation d'un `EmployeID` inconnu :

- créer/importer la ressource candidate;
- conserver les attributs ERP possédés par la source;
- initialiser l'activation locale RessourcePlanner à `false`;
- ne jamais activer automatiquement la ressource.

Une ressource devenue inactive dans l'ERP ne peut plus être planifiée, même si son autorisation locale était active. L'autorisation locale reste une décision RessourcePlanner distincte afin qu'une synchronisation ERP ne modifie pas silencieusement un choix administratif.

### Utilisateurs

`RP_Users` est un annuaire ERP des utilisateurs candidats; il ne doit pas être confondu avec `AppUser`.

Avec ADR-012, `AppUser.id` reste l'identité interne stable. La paire OIDC `(issuer, subject)` reste l'identité d'authentification autoritaire une fois liée, mais elle devient optionnelle avant le premier login afin de permettre un vrai compte pré-provisionné. Le compte ERP sélectionné par l'ADMIN doit être persisté séparément par `AppUser.erp_user_id → RP_Users.UserID`; `AppUser.employee_external_id` continue de représenter `EmployeID`.

Le modèle cible doit conserver au minimum :

- `UserID` ERP stable;
- `EmployeID` lié;
- attributs descriptifs;
- état source `UserActif`;
- configuration préparatoire locale tant qu'aucun `AppUser` n'existe;
- `AppUser.erp_user_id = UserID` dès le pré-provisionnement;
- `AppUser.employee_external_id = EmployeID`;
- `AppUser.active` et `AppUser.roles_json` comme autorités locales après création;
- paire OIDC éventuellement absente jusqu'au premier login.

Un utilisateur peut accéder à RessourcePlanner uniquement si :

- un `AppUser` a déjà été explicitement pré-provisionné par un ADMIN;
- son compte source ERP est admissible/actif;
- `AppUser.active` est vrai;
- ses rôles RessourcePlanner ont été attribués localement dans `AppUser.roles_json`;
- son identité OIDC a été liée de manière autoritaire au premier login, ou résolue par la paire déjà liée lors des logins suivants.

Aucun rôle privilégié n'est dérivé automatiquement de `RP_Users`.

## Administration

Une surface ADMIN doit permettre de gérer séparément :

### Ressources ERP

Afficher au minimum :

- nom;
- `EmployeID`;
- département;
- branche/division;
- statut ERP;
- activation RessourcePlanner.

Actions :

- activer/désactiver dans RessourcePlanner;
- conserver les attributs Planning locaux (classe locale, compétences, horaires, notes) séparés de la synchronisation ERP.

### Utilisateurs ERP

Afficher au minimum :

- nom;
- `UserID`;
- `EmployeID` lié;
- état utilisateur ERP;
- état employé ERP;
- activation RessourcePlanner;
- rôles RessourcePlanner;
- état/lien OIDC lorsqu'il est disponible.

Actions :

- activer/désactiver l'accès RessourcePlanner;
- attribuer/modifier les rôles locaux;
- voir la ressource liée via `EmployeID`;
- ne jamais joindre automatiquement par nom/courriel.

## Impact sur la fondation existante

Le comportement actuel où `ExternalEmployeeRecord.active` peut écrire directement `Resource.active` ne représente plus correctement la décision produit si ce champ mélange état ERP et activation locale.

L'implémentation #256 doit donc séparer explicitement :

- état source ERP;
- activation locale RessourcePlanner;
- état effectif utilisable.

Le nom exact des colonnes SQL est une décision d'implémentation, mais ces trois sémantiques ne doivent pas être confondues.

De même, `RP_Users` ne doit pas créer un `AppUser` actif uniquement parce qu'une ligne existe dans OData.

## OIDC

Le smoke réel #223 a confirmé le pont suivant sur un compte réel, et le PO l'a accepté comme contrat d'implémentation :

- OIDC `(issuer, sub)` = identité d'authentification autoritaire;
- `preferred_username.strip()` = correspondance exacte vers `RP_Users.UserID`;
- `RP_Users.UserID → EmployeID → RP_Employees.EmployeID → Resource.external_id`.

`preferred_username` sert uniquement de pont. Ne jamais utiliser `name`, le courriel ou un display name comme clé, ne pas supposer que `sub == UserID` et ne pas dériver le `sub` ou le `UserID` depuis une convention de chaîne.

La cible ADR-012 conserve ce contrat de rapprochement mais change la responsabilité du premier login. L'ADMIN doit d'abord créer un `AppUser` avec `erp_user_id = RP_Users.UserID`, `employee_external_id = RP_Users.EmployeID`, ses rôles locaux et son état actif. La paire `(issuer, sub)` est alors absente. Au premier login, RessourcePlanner vérifie `preferred_username → RP_Users.UserID`, `AppUser.erp_user_id`, `EmployeID`, l'activation locale et l'admissibilité ERP, puis lie atomiquement `(issuer, sub)` au compte existant. Aucun rôle ni état d'activation n'est copié pendant le login.

Une fois l'`AppUser` créé, `AppUser.active` et `AppUser.roles_json` sont les valeurs autoritaires; `erp_user_directory.local_active/roles_json` ne peuvent rester qu'une préparation avant création du compte. Aucune synchronisation ERP ne réactive ni ne modifie les rôles d'un `AppUser`.

Le runtime actuellement livré par #223 provisionne encore au premier login; cette divergence est volontairement reportée aux tranches d'implémentation postérieures à IDENTITY-A. Le fallback générique d'auto-provisionnement OIDC est incompatible avec ADR-012 et doit être neutralisé plus tard.

Cette relation a été observée sur un compte OIDC réel; elle reste à confirmer sur un deuxième compte réel.
## Tâches / budgets

Le PO a confirmé qu'un feed OData Acumatica existe également pour les tâches et inclut les budgets.

Le contrat exact de ce feed — chemin, clé stable, champs, sémantique des budgets, statuts et fixture anonymisée — n'est pas encore documenté ici. Il doit suivre le même workflow contract-first avant de remplacer le fallback Excel/CSV de #271.

Ne pas inventer le mapping budget avant réception du sample anonymisé et confirmation de la clé.

## Références

- #232 — contrat Acumatica réel
- #256 — adaptateurs Employees/Users
- #447 — bootstrap données réalistes / tests PO
- #223 — OIDC réel
- #271 — catalogue tâches
- [ACUMATICA_ODATA_CONTRACT.md](../../ACUMATICA_ODATA_CONTRACT.md)
- [ACUMATICA_CONTRACT_WORKFLOW.md](../../ACUMATICA_CONTRACT_WORKFLOW.md)

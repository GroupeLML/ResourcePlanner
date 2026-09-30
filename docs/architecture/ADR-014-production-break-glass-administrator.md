# ADR-014 — Administrateur production break-glass distinct d'OIDC et des identités de développement

Status: Accepted
Date: 2026-09-29

## Context

Le premier go-live SQL Server doit rester administrable lorsque le fournisseur OIDC/Acumatica est indisponible, sans réutiliser le mode `local`, le Dev User Switcher ni les données `DEMO-*`.

ADR-012 a déjà fixé `AppUser` comme autorité locale pour l'activation et les rôles, avec une identité OIDC optionnelle jusqu'à la liaison. Les sessions Web normales sont déjà opaques, persistées côté serveur et protégées par CSRF.

Le mécanisme d'urgence doit donc fournir une preuve d'authentification alternative sans créer un second annuaire utilisateur ni contourner le RBAC canonique.

## Decision

### 1. Le break-glass authentifie un `AppUser` réservé

Un credential break-glass référence un `AppUser` actif portant `ADMIN`.

Cet `AppUser` est créé sans :

- `issuer` / `subject` OIDC;
- `erp_user_id`;
- `employee_external_id`;
- dépendance à Acumatica;
- dépendance à une `Resource`.

Le compte reste donc un compte local RessourcePlanner réservé à l'urgence, mais il utilise exactement les mêmes rôles et permissions backend que les autres `AppUser`.

### 2. Le credential est persistant, dédié et hashé

Le secret n'est jamais stocké en clair. Il est persisté sous forme de hash scrypt versionné avec sel aléatoire.

Le credential possède une identité stable, une version, un état actif, un compteur/fenêtre d'échecs, un verrou temporaire et les dates de rotation/succès nécessaires à l'exploitation.

Le nom de login est normalisé et unique. Ce mécanisme n'est pas destiné à créer un annuaire local général.

### 3. Le bootstrap est une commande d'exploitation explicite

Aucune migration Alembic ne crée d'administrateur.

Le bootstrap production est exécuté explicitement après `alembic upgrade head`. Il est :

- idempotent pour un login et un secret inchangés;
- capable de réconcilier l'état actif et le rôle `ADMIN`;
- incapable de transformer silencieusement un compte OIDC/ERP en compte break-glass;
- capable de remplacer le secret uniquement avec une option de rotation explicite;
- audité.

Le secret provient d'un mécanisme hors Git, par exemple une variable d'environnement injectée par l'opérateur ou une saisie interactive masquée. Il n'est jamais accepté comme argument de ligne de commande.

### 4. Les sessions serveur existantes restent autoritaires

Un login break-glass réussi crée une `AuthSession` opaque existante avec `auth_mode=break_glass`.

Les cookies, la durée de session, la révocation et le token CSRF réutilisent le même modèle que les sessions OIDC. Les mutations sont soumises à la même validation CSRF.

L'identité projetée dans `AuthPrincipal` utilise un issuer interne de session `urn:resourceplanner:break-glass`, sans écrire une fausse liaison OIDC dans `AppUser`.

### 5. Le chemin fonctionne sans appel au fournisseur OIDC

Le endpoint break-glass ne contacte ni le document de découverte, ni le token endpoint, ni Acumatica.

En production il est activé explicitement en mode `oidc`; une panne du fournisseur n'empêche donc ni le login break-glass ni la résolution de sa session SQL.

Le mode `local` et le Dev User Switcher restent strictement des outils de développement distincts.

### 6. Les tentatives sont limitées et auditées

La politique initiale est :

- 5 échecs;
- dans une fenêtre de 15 minutes;
- verrouillage pendant 15 minutes.

Les succès, échecs, verrouillages, créations/reconciliations et rotations produisent des événements d'audit. L'audit conserve un hash du nom de login et des codes de raison, jamais le secret ni son hash.

### 7. Le dernier accès break-glass ADMIN est protégé

Tant qu'un `AppUser` est le seul compte actif possédant à la fois un credential break-glass actif et le rôle `ADMIN`, l'administration utilisateur refuse :

- sa désactivation;
- le retrait de son rôle `ADMIN`.

Les FK empêchent également une suppression physique qui laisserait un credential orphelin. Plusieurs comptes break-glass peuvent être bootstrapés explicitement, mais aucun mécanisme automatique n'en crée.

## Alternatives considered

### Réutiliser `RESOURCEPLANNER_AUTH_MODE=local`

Rejeté. Le mode local est une identité statique de développement et peut contourner la persistance `AppUser`; l'exposer comme secours de production mélangerait les frontières dev/prod.

### Réutiliser le Dev User Switcher

Rejeté. Il sert à simuler des identités/roles en développement et est volontairement interdit en mode OIDC.

### Stocker un mot de passe directement sur chaque `AppUser`

Rejeté. Cela créerait un second annuaire local général alors que le besoin concerne seulement un petit nombre de comptes d'urgence.

### Créer l'admin dans une migration Alembic

Rejeté. Les migrations définissent le schéma, pas les credentials d'exploitation. Cela rendrait la création implicite, difficile à auditer et dangereuse pour la rotation.

### Utiliser un secret en clair dans un fichier de configuration versionné

Rejeté. Les secrets d'exploitation doivent rester hors Git et hors logs.

## Consequences

### Positive

- l'administration reste accessible lors d'une panne OIDC/Acumatica;
- le RBAC et les sessions existants restent uniques;
- aucun seed de développement n'est nécessaire en production;
- le bootstrap est rejouable, traçable et indépendant d'ERP;
- la rotation d'un credential ne modifie pas l'identité métier;
- la protection du dernier accès réduit le risque de verrouillage administratif.

### Trade-offs / negative

- deux nouvelles tables de sécurité sont nécessaires;
- `AuthSession` doit persister l'origine d'authentification;
- les opérateurs doivent conserver et faire tourner un secret break-glass hors Git;
- le chemin d'urgence doit faire partie des smokes de go-live et des procédures d'exploitation.

## References

- #457 — Pré-go-live : baseline DB propre, retrait seeds dev et admin break-glass
- #208 — cutover SQL autoritaire
- #218 — identité / RBAC / sessions
- #224 — administration utilisateurs / rôles
- #447 — bootstrap données réalistes
- ADR-011 — SQL Server authoritative database
- ADR-012 — pré-provisionnement AppUser avant liaison OIDC
- `docs/BREAK_GLASS_ADMIN.md`

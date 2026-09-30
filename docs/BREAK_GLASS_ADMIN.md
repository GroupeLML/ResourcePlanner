# Runbook — administrateur break-glass de production

Ce runbook décrit l'accès administratif d'urgence livré par #457. Il est distinct du mode `local`, du Dev User Switcher et des seeds `DEMO-*`.

## 1. Invariants

- le compte est un `AppUser` local réservé avec le rôle `ADMIN`;
- il ne possède ni identité OIDC, ni `erp_user_id`, ni `employee_external_id`;
- le secret est hashé avec scrypt et n'est jamais stocké ou journalisé en clair;
- le bootstrap n'est jamais exécuté par Alembic;
- une session break-glass utilise la même table `auth_sessions`, les mêmes cookies opaques et le même CSRF que le runtime OIDC;
- le dernier accès break-glass ADMIN actif ne peut pas être désactivé ni perdre `ADMIN` depuis l'administration utilisateur.

## 2. Préconditions

1. utiliser la base cible explicitement choisie;
2. appliquer le schéma avec `python -m alembic upgrade head`;
3. vérifier que `RESOURCEPLANNER_DATABASE_URL` pointe vers la bonne base;
4. ne jamais exécuter le bootstrap sur une base utile non identifiée;
5. conserver le secret dans un gestionnaire de secrets ou un canal opérateur hors Git.

Le CLI refuse SQLite par défaut. `--allow-sqlite` existe uniquement pour les validations locales/tests.

## 3. Premier bootstrap

Variables :

```bash
export RESOURCEPLANNER_DATABASE_URL='<URL SQLAlchemy injectée hors Git>'
export RESOURCEPLANNER_BREAK_GLASS_LOGIN='admin-secours'
export RESOURCEPLANNER_BREAK_GLASS_SECRET='<secret fort injecté hors Git>'
```

Exécution directe :

```bash
python tools/bootstrap_production_admin.py
```

Ou avec Docker Compose :

```bash
docker compose --profile ops run --rm bootstrap-admin
```

Le résultat affiche uniquement l'action, les IDs techniques et la version du credential. Le secret et son hash ne sont jamais imprimés.

Après usage :

```bash
unset RESOURCEPLANNER_BREAK_GLASS_SECRET
```

Un replay avec le même login et le même secret est idempotent et retourne `reconciled`.

## 4. Activation du login de secours

Le runtime de production reste en mode OIDC :

```bash
RESOURCEPLANNER_AUTH_MODE=oidc
RESOURCEPLANNER_BREAK_GLASS_ENABLED=true
```

Le login de secours n'est pas un troisième `RESOURCEPLANNER_AUTH_MODE`. Il est un chemin d'authentification d'urgence disponible en parallèle du mode OIDC.

Dans React, lorsque la session est absente, ouvrir **Accès administrateur de secours** puis saisir le login et le secret.

Le endpoint correspondant est `POST /api/v1/auth/break-glass`. Il ne contacte aucun endpoint OIDC ou Acumatica.

## 5. Protection brute-force

Politique initiale :

- maximum : 5 échecs;
- fenêtre : 15 minutes;
- verrouillage : 15 minutes.

L'essai qui atteint le seuil est refusé comme un mauvais credential. Les essais suivants pendant le verrouillage reçoivent un refus temporaire avec `Retry-After`.

Les événements sont persistés dans `auth_security_audit` avec :

- type d'événement;
- succès/échec;
- IDs techniques lorsque connus;
- hash SHA-256 du login normalisé;
- code de raison;
- horodatage.

Ni le secret ni son hash scrypt n'y sont enregistrés.

## 6. Rotation / réinitialisation

Pour remplacer le secret ou réinitialiser un credential verrouillé, injecter le nouveau secret hors Git puis exécuter explicitement :

```bash
python tools/bootstrap_production_admin.py --rotate-secret
```

Avec Compose :

```bash
docker compose --profile ops run --rm bootstrap-admin \
  python tools/bootstrap_production_admin.py --rotate-secret
```

La rotation :

- remplace le hash;
- incrémente `credential_version`;
- réactive le credential;
- remet à zéro les compteurs/verrous d'échec;
- crée un événement d'audit.

Sans `--rotate-secret`, un secret différent est refusé afin d'empêcher une rotation accidentelle lors d'un replay.

## 7. Panne OIDC / Acumatica

Le runtime doit déjà être configuré avec `RESOURCEPLANNER_BREAK_GLASS_ENABLED=true`.

En cas de panne du fournisseur :

1. ouvrir RessourcePlanner normalement;
2. utiliser **Accès administrateur de secours**;
3. vérifier `/api/v1/auth/me` ou l'interface : `auth_mode` doit être `break_glass`;
4. effectuer uniquement les actions administratives nécessaires;
5. se déconnecter dès la fin de l'intervention.

La création et la résolution de cette session ne requièrent aucun appel réseau vers OIDC/Acumatica.

## 8. Perte ou suspicion de compromission

- ne pas inscrire l'ancien ou le nouveau secret dans une issue, un commit ou un log;
- effectuer une rotation explicite depuis un environnement opérateur maîtrisé;
- vérifier les entrées `auth_security_audit`;
- révoquer les sessions actives concernées si nécessaire selon la procédure d'exploitation;
- renouveler le secret stocké dans le gestionnaire de secrets.

## 9. Dernier accès administratif de secours

L'administration utilisateur refuse de désactiver le dernier `AppUser` disposant d'un credential break-glass actif et du rôle `ADMIN`, ou de lui retirer `ADMIN`.

Pour remplacer ce compte :

1. bootstrapper explicitement un second login break-glass;
2. valider sa connexion;
3. seulement ensuite modifier l'ancien compte.

Aucun compte d'urgence supplémentaire n'est créé automatiquement.

## 10. Rollback

Désactiver l'exposition du login de secours avec :

```bash
RESOURCEPLANNER_BREAK_GLASS_ENABLED=false
```

Cela ne supprime pas le credential ni son audit. Ne supprimer ou désactiver le dernier accès de secours qu'après avoir validé un autre chemin administratif fonctionnel.

Le downgrade de schéma n'est pas une procédure normale de rotation/récupération.

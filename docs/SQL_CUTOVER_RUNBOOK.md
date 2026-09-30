# Runbook — premier go-live SQL Server sur base propre

Ce document décrit le premier basculement de RessourcePlanner vers SQL Server autoritaire.

La décision produit est de **ne pas importer l'historique Excel/V1**. La production démarre sur une base SQL Server neuve créée par la baseline canonique; les données métier SQLite V2 explicitement retenues sont ensuite transférées par #492, avant le bootstrap final et les référentiels resynchronisés depuis les sources réelles.

Voir #457 pour la baseline de schéma, le retrait des seeds de développement et l'accès administrateur break-glass.

Le transfert contrôlé des données métier déjà présentes dans SQLite V2 est documenté dans `docs/SQLITE_V2_SQLSERVER_CUTOVER.md` (#492). Il intervient après création de la baseline propre et avant le bootstrap/synchronisations finales; il ne réintroduit aucun import Excel/V1.

## Principes

- SQL Server est la base de référence intégration/staging/production conformément à ADR-011;
- SQLite reste réservé au développement local et aux tests rapides lorsque sa sémantique suffit;
- aucune période hybride Excel/SQL;
- aucune double écriture;
- aucun fallback silencieux vers Excel;
- aucune donnée `DEMO-*` ou identité `urn:resourceplanner:dev` en production;
- aucun seed de développement dans le workflow de mise en service;
- Alembic reste le mécanisme de migration;
- avant le premier go-live, l'historique de migrations pré-production est remplacé par une **baseline V2 unique**;
- après cette baseline, toute nouvelle évolution de schéma utilise de nouveau des migrations additives normales;
- le bootstrap admin est séparé des migrations et des seeds;
- un accès administrateur break-glass doit fonctionner sans dépendre d'Acumatica/OIDC, tant que RessourcePlanner et sa base sont accessibles.

## 1. Geler le schéma pré-go-live

Ne pas créer la baseline trop tôt.

Avant de squasher les migrations de développement :

1. terminer les évolutions de modèle jugées nécessaires au premier go-live;
2. confirmer que les migrations restantes ne servent encore aucune base de production;
3. exécuter la CI complète;
4. conserver le commit Git servant de référence à la baseline.

L'objectif n'est pas de supprimer Alembic, mais de remplacer la chaîne historique de développement par un nouveau point zéro de production.

## 2. Créer la baseline V2

À partir du schéma canonique courant :

1. retirer les anciennes révisions Alembic pré-production devenues inutiles;
2. créer une migration baseline unique représentant le schéma complet;
3. vérifier que `Base.metadata` et la baseline sont cohérents;
4. exécuter `alembic upgrade head` depuis une base vide;
5. vérifier contraintes, index, types et valeurs par défaut;
6. exécuter les validations SQLite/offline MSSQL;
7. répéter ensuite sur SQL Server réel.

Après ce point, les migrations futures repartent normalement à partir de cette baseline.

La baseline de production retenue porte la révision Alembic `v2_production_baseline` et vit dans l'unique fichier `migrations/versions/0001_v2_production_baseline.py`. Elle est autonome : elle n'importe pas les modèles applicatifs et ne dépend d'aucune révision `0001` → `0050` de développement. La validation automatisée couvre un upgrade SQLite depuis zéro et la génération/compilation MSSQL offline; la validation SQL Server réelle reste l'étape ENV-457 avant fermeture de #457.

## 3. Retirer les seeds de développement du chemin production

Le workflow production ne doit pas appeler ni dépendre de :

- `tools/seed_demo_data.py`;
- `Charger_Donnees_Demo.bat`;
- identités `urn:resourceplanner:dev`;
- projets, ressources, demandes ou autres lignes `DEMO-*`;
- sélecteur d'identité de développement.

Le seed de démonstration peut être supprimé du dépôt ou conservé uniquement dans un workflow local explicitement isolé, selon la décision finale de #336. Dans les deux cas, son exécution contre SQL Server/production doit être impossible ou explicitement refusée.

## 4. Créer la base SQL Server de production

Créer une base RessourcePlanner neuve et vide.

Le compte de déploiement doit pouvoir :

- créer/modifier le schéma via Alembic;
- lire/écrire les tables RessourcePlanner nécessaires au bootstrap;
- exécuter les transactions de validation.

Configurer la connexion via secret/environnement; ne jamais stocker la chaîne réelle dans Git.

Puis exécuter :

```bash
python -m alembic upgrade head
python -m alembic current
```

Le `current` final doit correspondre exactement au `head` de la baseline courante.

## 5. Bootstrap administrateur

Le premier compte administrateur n'est **pas un seed de démonstration** et ne doit pas être créé implicitement par une migration de schéma.

Utiliser un bootstrap explicite, idempotent et auditable qui :

- crée ou réconcilie l'identité administrateur réservée;
- attribue le rôle `ADMIN`;
- ne dépend d'aucun projet, employé ou ressource ERP;
- n'est jamais désactivé par une synchro Acumatica;
- protège le dernier accès administratif de secours;
- ne journalise aucun secret.

Les credentials de secours sont configurés hors Git. Lorsqu'un secret doit être persisté, seule une empreinte/hash robuste est stockée.

## 6. Valider l'accès break-glass

Le mode OIDC reste le chemin normal des utilisateurs.

En plus, le bootstrap admin doit posséder un chemin d'authentification local **strictement réservé au secours administratif**, distinct du sélecteur dev.

Valider au minimum :

1. connexion admin avec OIDC disponible;
2. connexion break-glass avec OIDC volontairement indisponible;
3. refus avec secret incorrect;
4. limitation des tentatives;
5. session/cookie protégés;
6. CSRF sur mutations;
7. audit connexion/action sans secret;
8. rotation ou réinitialisation documentée.

Ce mécanisme n'est pas un second annuaire général.

## 7. Alimenter les référentiels réels

Une fois la base et l'administration validées :

1. synchroniser les projets Acumatica;
2. synchroniser les ressources/utilisateurs lorsque leurs tranches sont prêtes;
3. synchroniser les tâches/budgets selon les contrats validés;
4. configurer les paramètres site nécessaires;
5. ne jamais importer le classeur historique V1 comme étape de go-live.

## 8. Smokes avant autorité SQL

Exécuter :

- readiness SQL Server;
- lecture seule;
- transaction + rollback;
- authentification OIDC;
- authentification break-glass;
- permissions ADMIN;
- lectures/mutations métier représentatives;
- concurrence/CAS pertinente;
- vérification qu'aucune donnée de démonstration n'est présente.

SQL Server ne devient autoritaire qu'après validation explicite de ces contrôles.

## 9. Rollback avant go-live

Tant que SQL n'est pas déclaré autoritaire, le rollback recommandé reste simple :

1. arrêter le runtime Web;
2. supprimer/recréer la base si nécessaire;
3. corriger le code/configuration;
4. réappliquer la baseline;
5. rejouer #492 depuis la sauvegarde SQLite vérifiée lorsque des données V2 doivent être conservées;
6. relancer bootstrap + synchronisations + smokes.

Ne pas corriger manuellement la base pour contourner une baseline ou un bootstrap défectueux.

## 10. Après le premier go-live

Une fois SQL déclaré autoritaire :

- ne plus modifier la baseline historique;
- toute évolution de schéma devient une migration additive normale;
- conserver des sauvegardes/restaurations SQL Server testées;
- retirer progressivement le runtime et les artefacts legacy selon #336;
- conserver le compte break-glass opérationnel, rotatable et audité.

## Références

- #162 — validation SQL Server réelle;
- #208 — mise en service SQL autoritaire;
- #218/#224 — identité, RBAC et administration utilisateurs;
- #336 — nettoyage post-cutover;
- #457 — baseline propre, retrait seeds dev et admin break-glass;
- `docs/architecture/ADR-011-sql-server-authoritative-database.md` — autorité SQL Server et rôle de SQLite.

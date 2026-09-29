# ADR-011 — SQL Server comme base de référence d'exploitation

Status: Accepted
Date: 2026-09-29

## Context

RessourcePlanner utilise SQLAlchemy 2.x et Alembic afin de garder la couche de persistance indépendante du transport HTTP et du frontend.

SQLite a permis de développer et tester rapidement le backend Web/SQL sans dépendre de l'environnement cible. Le projet possède maintenant un accès au serveur SQL Server réel et les travaux #162, #208 et #457 définissent déjà la trajectoire de validation et de mise en service sur une base neuve.

Certaines propriétés importantes de RessourcePlanner — concurrence multi-session, transactions composites, contraintes, migrations, verrouillage/CAS, rollback et performance — ne doivent pas être considérées comme validées pour la production uniquement parce qu'elles fonctionnent sous SQLite.

## Decision

SQL Server devient la **base de référence pour les environnements intégrés, de validation/staging et de production** de RessourcePlanner.

SQLite reste autorisé comme dialecte de **développement local et de tests rapides**, lorsque le scénario testé ne dépend pas d'une sémantique spécifique au moteur de base de données.

Conséquences obligatoires :

- le runtime de production utilise SQL Server;
- les validations de #162 doivent être exécutées sur SQL Server réel avant de considérer le raccord serveur terminé;
- tout comportement sensible à la concurrence, à l'isolation transactionnelle, aux contraintes/index, aux migrations ou aux performances doit avoir une validation SQL Server pertinente;
- les mesures SQLite ne sont pas représentatives des performances de production;
- SQLAlchemy et Alembic restent l'abstraction et le mécanisme de migration canoniques;
- éviter d'introduire du T-SQL spécifique dans le domaine ou l'application; toute dépendance spécifique au dialecte doit rester localisée dans l'infrastructure et être justifiée;
- le premier go-live suit #457 et #208 : base SQL Server neuve, baseline V2 propre, aucun import historique Excel/V1 obligatoire;
- après go-live, SQL Server est autoritaire pour les nouvelles opérations de RessourcePlanner.

## Alternatives considered

### Conserver SQLite comme base de production

Rejeté. La simplicité opérationnelle ne compense pas les limites pour un runtime multi-utilisateur avec transactions concurrentes et validations de production.

### Traiter SQLite et SQL Server comme deux cibles de production équivalentes

Rejeté. Cela augmenterait durablement la matrice de compatibilité et risquerait de masquer des différences de concurrence, types, migrations et performances.

### Utiliser SQL Server partout, y compris pour chaque test local

Non retenu comme exigence générale. Cela ralentirait inutilement la boucle de développement. SQLite reste utile pour les tests rapides tant que ceux-ci ne prétendent pas prouver un comportement propre à SQL Server.

## Consequences

### Positive

- les validations importantes s'effectuent sur le moteur réellement utilisé en exploitation;
- la concurrence, les transactions et les migrations sont testées dans un environnement représentatif;
- le backend reste portable au niveau SQLAlchemy sans faire de SQLite une contrainte de production;
- le chemin de go-live #162 → #457/#208 devient explicite.

### Trade-offs / negative

- l'environnement intégré nécessite driver ODBC, `pyodbc`, configuration réseau et secrets;
- une partie des tests d'intégration devra être exécutée contre SQL Server en plus de la suite rapide SQLite;
- les différences de dialecte découvertes pendant #162 devront être corrigées sans affaiblir les invariants métier.

## Implementation notes

- `RESOURCEPLANNER_DATABASE_URL` demeure la frontière de configuration du backend.
- `tools/check_sqlserver_readiness.py`, Alembic et les smokes du runtime doivent rester dans le chemin de validation.
- SQLite peut rester utilisé par la CI et les tests unitaires lorsque sa sémantique est suffisante, mais une réussite SQLite seule ne ferme pas une exigence explicitement SQL Server.
- Le serveur SQL réel est disponible depuis le 2026-09-29; #162 devient donc une validation environnementale exécutable plutôt qu'un blocage d'accès.

## References

- GitHub Issue #55 — roadmap maître
- GitHub Issue #162 — validation SQL Server réelle
- GitHub Issue #208 — mise en service SQL autoritaire
- GitHub Issue #457 — baseline DB propre, retrait seeds dev et admin break-glass
- `docs/SERVER_RUNBOOK.md`
- `docs/SQL_CUTOVER_RUNBOOK.md`

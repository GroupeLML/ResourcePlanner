from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
import sys

from sqlalchemy.engine import make_url

from app.application.break_glass import BreakGlassBootstrapService
from app.infrastructure.sql.secret_hashing import ScryptSecretHasher
from app.infrastructure.sql import (
    SqlBreakGlassRepository,
    SqlUserIdentityRepository,
    create_session_factory,
    create_sql_engine,
)


DATABASE_URL_ENV = "RESOURCEPLANNER_DATABASE_URL"
LOGIN_ENV = "RESOURCEPLANNER_BREAK_GLASS_LOGIN"
SECRET_ENV = "RESOURCEPLANNER_BREAK_GLASS_SECRET"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or reconcile the dedicated RessourcePlanner production "
            "break-glass administrator. The secret is read from the environment "
            "or from an interactive prompt and is never accepted on the command line."
        )
    )
    parser.add_argument(
        "--login",
        default=None,
        help=f"Break-glass login name. Defaults to {LOGIN_ENV}.",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Local AppUser display name. Existing values are preserved when omitted.",
    )
    parser.add_argument("--email", default=None)
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help="Explicitly replace the persisted secret hash and increment its version.",
    )
    parser.add_argument(
        "--allow-sqlite",
        action="store_true",
        help="Allow SQLite only for local validation/tests. Production uses SQL Server.",
    )
    return parser


def _secret() -> str:
    configured = os.getenv(SECRET_ENV)
    if configured is not None:
        return configured
    if not sys.stdin.isatty():
        raise SystemExit(
            f"{SECRET_ENV} est requis hors d'une session interactive."
        )
    return getpass.getpass("Secret break-glass: ")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = str(os.getenv(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        raise SystemExit(f"{DATABASE_URL_ENV} est requis.")

    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite" and not args.allow_sqlite:
        raise SystemExit(
            "Le bootstrap production refuse SQLite sans --allow-sqlite."
        )

    login = str(args.login or os.getenv(LOGIN_ENV) or "").strip()
    if not login:
        raise SystemExit(
            f"Un login est requis via --login ou {LOGIN_ENV}."
        )

    secret = _secret()
    engine = create_sql_engine(database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            result = BreakGlassBootstrapService(
                SqlUserIdentityRepository(session),
                SqlBreakGlassRepository(session),
                ScryptSecretHasher(),
            ).bootstrap(
                login_name=login,
                secret=secret,
                display_name=args.display_name,
                email=args.email,
                rotate_secret=bool(args.rotate_secret),
                now=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "status": "ok",
                "action": result.action,
                "user_id": result.user_id,
                "credential_id": result.credential_id,
                "credential_version": result.credential_version,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4
import unittest

from sqlalchemy import delete, select

from app.application.errors import ApplicationConflictError
from app.infrastructure.sql import (
    Competency,
    CompetencyResourceClassAudit,
    ResourceClassConfig,
    SqlCompetencyCatalogRepository,
    create_session_factory,
    create_sql_engine,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(
    IS_MSSQL,
    "Validation multi-session réservée à SQL Server via RESOURCEPLANNER_SQLSERVER_TEST_URL.",
)
class CompetencyResourceClassSqlServerConcurrencyTests(unittest.TestCase):
    def test_two_grouping_updates_with_same_expected_version_have_one_winner(self) -> None:
        marker = uuid4().hex[:12]
        competency_id = f"619A-C-{marker}"
        actor_id = f"619A-U-{marker}"
        class_codes = (f"619A-RC1-{marker}", f"619A-RC2-{marker}")

        engine = create_sql_engine(DATABASE_URL)
        factory = create_session_factory(engine)
        try:
            with factory.begin() as session:
                session.add_all(
                    [
                        ResourceClassConfig(
                            code=class_codes[0],
                            label=f"619A classe 1 {marker}",
                            average_hourly_cost_cad="100.00",
                            active=True,
                            version=1,
                        ),
                        ResourceClassConfig(
                            code=class_codes[1],
                            label=f"619A classe 2 {marker}",
                            average_hourly_cost_cad="110.00",
                            active=True,
                            version=1,
                        ),
                        Competency(
                            id=competency_id,
                            name=f"619A compétence {marker}",
                            active=True,
                            sort_order=0,
                        ),
                    ]
                )

            def assign(class_code: str) -> str:
                with factory.begin() as session:
                    try:
                        SqlCompetencyCatalogRepository(
                            session
                        ).set_competency_resource_class(
                            competency_id,
                            class_code,
                            actor_user_id=actor_id,
                            expected_version=1,
                        )
                    except ApplicationConflictError as exc:
                        if exc.code != "competency_resource_class_version_conflict":
                            raise
                        return "conflict"
                    return "committed"

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(assign, class_codes))

            self.assertEqual(sorted(outcomes), ["committed", "conflict"])
            with factory() as session:
                competency = session.get(Competency, competency_id)
                assert competency is not None
                self.assertEqual(competency.resource_class_version, 2)
                self.assertIn(competency.resource_class_code, class_codes)
                audits = list(
                    session.scalars(
                        select(CompetencyResourceClassAudit).where(
                            CompetencyResourceClassAudit.competency_id
                            == competency_id
                        )
                    ).all()
                )
                self.assertEqual(len(audits), 1)
                self.assertEqual(audits[0].resulting_version, 2)
                self.assertEqual(
                    audits[0].new_resource_class_code,
                    competency.resource_class_code,
                )
        finally:
            try:
                with factory.begin() as session:
                    session.execute(
                        delete(CompetencyResourceClassAudit).where(
                            CompetencyResourceClassAudit.competency_id
                            == competency_id
                        )
                    )
                    session.execute(
                        delete(Competency).where(
                            Competency.id == competency_id
                        )
                    )
                    session.execute(
                        delete(ResourceClassConfig).where(
                            ResourceClassConfig.code.in_(class_codes)
                        )
                    )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()

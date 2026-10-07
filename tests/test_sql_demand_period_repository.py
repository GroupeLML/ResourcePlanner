from __future__ import annotations

from datetime import date
import unittest

from sqlalchemy import func, select

from app.domain.demand_periods import (
    CONFIRMATION_MODE_INHERIT_MASTER,
    PERIOD_INHERITANCE_CONTRACT_VERSION,
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
    PERIOD_PROVENANCE_LEGACY,
    PERIOD_PROVENANCE_MASTER,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
    DemandPeriodDefinition,
)
from app.infrastructure.sql import (
    Base,
    Project,
    RequestLine,
    Resource,
    SqlDemandPeriodRepository,
    WorkforceRequest,
    WorkforceRequestPeriod,
    WorkforceRequestPeriodSelection,
    create_session_factory,
    create_sql_engine,
    transactional_session,
)


DAY_1 = date(2026, 9, 7)
DAY_2 = date(2026, 9, 8)


class SqlDemandPeriodRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with transactional_session(self.factory) as session:
            session.add(Project(id="P1", number="P-1", name="Projet 1"))
            session.add_all(
                [
                    Resource(id="R-A", name="Technicien A"),
                    Resource(id="R-B", name="Technicien B"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    WorkforceRequest(
                        id="D1",
                        legacy_demand_number="DMO-1",
                        project_id="P1",
                        desired_start=DAY_1,
                        desired_end=DAY_2,
                    ),
                    WorkforceRequest(
                        id="D2",
                        legacy_demand_number="DMO-2",
                        project_id="P1",
                        desired_start=DAY_1,
                        desired_end=DAY_2,
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    RequestLine(
                        id="D1",
                        workforce_request_id="D1",
                        position=0,
                        kind="WORKFORCE",
                    ),
                    RequestLine(
                        id="D2",
                        workforce_request_id="D2",
                        position=0,
                        kind="WORKFORCE",
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    @staticmethod
    def alternatives(hours_b: float = 8) -> tuple[DemandPeriodDefinition, ...]:
        return (
            DemandPeriodDefinition(
                period_id="OPT-A",
                start_date=DAY_1,
                end_date=DAY_1,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="TECH-DAY",
                proposed_resource="Technicien A",
            ),
            DemandPeriodDefinition(
                period_id="OPT-B",
                start_date=DAY_2,
                end_date=DAY_2,
                hours=hours_b,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="TECH-DAY",
                proposed_resource="Technicien B",
            ),
        )

    def test_replace_persists_two_unselected_alternatives_with_distinct_resources(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlDemandPeriodRepository(session)
            rows = repository.replace_for_demand("DMO-1", self.alternatives())

            self.assertEqual([row.period_id for row in rows], ["OPT-A", "OPT-B"])
            self.assertEqual(
                [row.proposed_resource for row in rows],
                ["Technicien A", "Technicien B"],
            )
            self.assertFalse(any(row.selected for row in rows))
            self.assertEqual(repository.selections_for_demand("DMO-1"), {})
            physical = session.scalars(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.workforce_request_id == "D1"
                )
            ).all()
            self.assertEqual({row.request_line_id for row in physical}, {"D1"})

    def test_master_inheritance_is_dynamic_and_quantity_is_derived(self) -> None:
        with transactional_session(self.factory) as session:
            line = session.get(RequestLine, "D1")
            assert line is not None
            line.slot_count = 3
            line.confirmation = "Confirmée"
            line.proposed_resource_id = "R-A"
            repository = SqlDemandPeriodRepository(session)
            rows = repository.replace_for_demand(
                "DMO-1",
                (
                    DemandPeriodDefinition(
                        period_id="P1",
                        start_date=DAY_1,
                        end_date=DAY_2,
                        hours=12,
                        kind=PERIOD_KIND_CUMULATIVE,
                        confirmation="Tentative",
                        confirmation_mode=CONFIRMATION_MODE_INHERIT_MASTER,
                        proposed_resource="Technicien B",
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
                        resource_count=99,
                    ),
                ),
            )

            self.assertEqual(rows[0].resource_count, 3)
            self.assertEqual(rows[0].resource_count_provenance, PERIOD_PROVENANCE_MASTER)
            self.assertEqual(rows[0].confirmation, "Confirmée")
            self.assertEqual(rows[0].confirmation_provenance, PERIOD_PROVENANCE_MASTER)
            self.assertEqual(rows[0].proposed_resource, "Technicien A")
            self.assertEqual(rows[0].proposed_resource_provenance, PERIOD_PROVENANCE_MASTER)
            physical = session.scalar(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.workforce_request_id == "D1",
                    WorkforceRequestPeriod.active.is_(True),
                )
            )
            assert physical is not None
            self.assertEqual(
                physical.inheritance_contract_version,
                PERIOD_INHERITANCE_CONTRACT_VERSION,
            )
            self.assertEqual(physical.resource_count, 3)

            line.slot_count = 2
            line.confirmation = "Tentative"
            line.proposed_resource_id = "R-B"
            session.flush()
            refreshed = repository.list_for_demand("DMO-1")
            self.assertEqual(refreshed[0].resource_count, 2)
            self.assertEqual(refreshed[0].confirmation, "Tentative")
            self.assertEqual(refreshed[0].proposed_resource, "Technicien B")

    def test_historical_row_keeps_legacy_period_authority(self) -> None:
        with transactional_session(self.factory) as session:
            line = session.get(RequestLine, "D1")
            assert line is not None
            line.slot_count = 7
            line.confirmation = "Confirmée"
            line.proposed_resource_id = "R-B"
            session.add(
                WorkforceRequestPeriod(
                    period_key="LEGACY-P1",
                    workforce_request_id="D1",
                    request_line_id="D1",
                    sequence=1,
                    kind=PERIOD_KIND_CUMULATIVE,
                    start_date=DAY_1,
                    end_date=DAY_2,
                    hours=8,
                    confirmation="Tentative",
                    proposed_resource_id="R-A",
                    resource_count=4,
                    active=True,
                )
            )
            session.flush()

            row = SqlDemandPeriodRepository(session).list_for_demand("DMO-1")[0]
            self.assertEqual(row.resource_count, 4)
            self.assertEqual(row.resource_count_provenance, PERIOD_PROVENANCE_LEGACY)
            self.assertEqual(row.confirmation, "Tentative")
            self.assertEqual(row.confirmation_provenance, PERIOD_PROVENANCE_LEGACY)
            self.assertEqual(row.proposed_resource, "Technicien A")
            self.assertEqual(row.proposed_resource_provenance, PERIOD_PROVENANCE_LEGACY)

    def test_same_as_contract_is_persisted_for_655b_resolution(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlDemandPeriodRepository(session)
            rows = repository.replace_for_demand(
                "DMO-1",
                (
                    DemandPeriodDefinition(
                        period_id="P1",
                        start_date=DAY_1,
                        end_date=DAY_1,
                        hours=4,
                        proposed_resource="Technicien A",
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_EXPLICIT,
                    ),
                    DemandPeriodDefinition(
                        period_id="P2",
                        start_date=DAY_2,
                        end_date=DAY_2,
                        hours=4,
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
                        same_as_period_id="P1",
                    ),
                    DemandPeriodDefinition(
                        period_id="P3",
                        start_date=DAY_2,
                        end_date=DAY_2,
                        hours=4,
                        proposed_resource_mode=PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
                        same_as_period_id="P2",
                    ),
                ),
            )

            by_id = {row.period_id: row for row in rows}
            self.assertEqual(by_id["P2"].proposed_resource_mode, "SAME_AS_PERIOD")
            self.assertEqual(by_id["P2"].same_as_period_id, "P1")
            self.assertEqual(by_id["P2"].same_as_root_period_id, "P1")
            self.assertEqual(by_id["P2"].same_as_state, "ACTIVE")
            self.assertIsNone(by_id["P2"].proposed_resource)
            self.assertEqual(by_id["P3"].same_as_period_id, "P2")
            self.assertEqual(by_id["P3"].same_as_root_period_id, "P1")
            physical = session.scalar(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.period_key == "P2",
                    WorkforceRequestPeriod.active.is_(True),
                )
            )
            assert physical is not None
            self.assertEqual(
                physical.proposed_resource_mode,
                PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
            )
            self.assertEqual(physical.same_as_period_key, "P1")

    def test_selection_is_unique_per_group_and_can_switch_options(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlDemandPeriodRepository(session, actor_name="coord-test-user")
            repository.replace_for_demand("DMO-1", self.alternatives())

            repository.select_alternative("DMO-1", "TECH-DAY", "OPT-A")
            self.assertEqual(
                repository.selections_for_demand("DMO-1"),
                {"TECH-DAY": "OPT-A"},
            )
            rows = repository.list_for_demand("DMO-1")
            self.assertEqual(
                [row.period_id for row in rows if row.selected],
                ["OPT-A"],
            )

            repository.select_alternative("DMO-1", "TECH-DAY", "OPT-B")
            self.assertEqual(
                repository.selections_for_demand("DMO-1"),
                {"TECH-DAY": "OPT-B"},
            )
            rows = repository.list_for_demand("DMO-1")
            self.assertEqual(
                [row.period_id for row in rows if row.selected],
                ["OPT-B"],
            )
            selection_count = session.scalar(
                select(func.count()).select_from(WorkforceRequestPeriodSelection)
            )
            self.assertEqual(selection_count, 1)
            selection = session.scalar(select(WorkforceRequestPeriodSelection))
            assert selection is not None
            self.assertEqual(selection.request_line_id, "D1")

    def test_replacing_definition_versions_rows_and_clears_old_selection(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlDemandPeriodRepository(session)
            repository.replace_for_demand("DMO-1", self.alternatives())
            repository.select_alternative("DMO-1", "TECH-DAY", "OPT-A")

            replacement = repository.replace_for_demand(
                "DMO-1", self.alternatives(hours_b=6)
            )

            self.assertEqual(repository.selections_for_demand("DMO-1"), {})
            self.assertFalse(any(row.selected for row in replacement))
            self.assertEqual(
                next(row.hours for row in replacement if row.period_id == "OPT-B"),
                6.0,
            )

            physical_rows = session.scalars(
                select(WorkforceRequestPeriod).where(
                    WorkforceRequestPeriod.workforce_request_id == "D1"
                )
            ).all()
            self.assertEqual(len(physical_rows), 4)
            self.assertEqual(sum(1 for row in physical_rows if row.active), 2)
            self.assertEqual(sum(1 for row in physical_rows if not row.active), 2)
            self.assertEqual(
                sorted(row.period_key for row in physical_rows),
                ["OPT-A", "OPT-A", "OPT-B", "OPT-B"],
            )

    def test_selection_cannot_target_another_demands_period(self) -> None:
        with transactional_session(self.factory) as session:
            repository = SqlDemandPeriodRepository(session)
            repository.replace_for_demand("DMO-1", self.alternatives())
            repository.replace_for_demand(
                "DMO-2",
                (
                    DemandPeriodDefinition(
                        period_id="OTHER-A",
                        start_date=DAY_1,
                        end_date=DAY_1,
                        hours=8,
                        kind=PERIOD_KIND_ALTERNATIVE,
                        alternative_group="OTHER-GROUP",
                    ),
                    DemandPeriodDefinition(
                        period_id="OTHER-B",
                        start_date=DAY_2,
                        end_date=DAY_2,
                        hours=8,
                        kind=PERIOD_KIND_ALTERNATIVE,
                        alternative_group="OTHER-GROUP",
                    ),
                ),
            )

            with self.assertRaises(KeyError):
                repository.select_alternative("DMO-1", "OTHER-GROUP", "OTHER-A")


if __name__ == "__main__":
    unittest.main()

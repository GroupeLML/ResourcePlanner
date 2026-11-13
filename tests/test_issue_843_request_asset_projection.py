from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
import unittest

from app.infrastructure.sql.asset_shift_projection import (
    ASSOCIATION_INHERITED_PROJECT_DIRECT,
    ASSOCIATION_INHERITED_REQUEST,
    ASSOCIATION_INHERITED_RESOURCE_PERIOD,
    ASSOCIATION_INHERITED_SEGMENT,
    ASSOCIATION_OWNED_SHIFT,
    ASSOCIATION_RELATED_REQUEST,
    asset_shift_association,
)


DAY = date(2026, 11, 12)


def request_requirement(*, project_id: str = "P1"):
    return SimpleNamespace(
        status="Planifié",
        origin="REQUEST",
        workforce_request_id="D1",
        project_id=project_id,
        start_date=DAY,
        end_date=DAY + timedelta(days=2),
    )


def human_requirement(*, request_id: str = "D1", project_id: str = "P1", identifier: str = "RR1"):
    return SimpleNamespace(
        id=identifier,
        workforce_request_id=request_id,
        project_id=project_id,
    )


def shift(*, resource_id: str = "R1", work_date: date = DAY):
    return SimpleNamespace(
        id=f"S-{resource_id}-{work_date.isoformat()}",
        resource_id=resource_id,
        work_date=work_date,
    )


def allocation(*, operator_resource_id: str | None = "R1", start_date: date = DAY, end_date: date | None = DAY + timedelta(days=2)):
    return SimpleNamespace(
        operator_resource_id=operator_resource_id,
        start_date=start_date,
        end_date=end_date,
    )


class Issue843RequestAssetProjectionTests(unittest.TestCase):
    def test_explicit_request_operator_is_resource_inherited_only_for_matching_shift(self) -> None:
        requirement = request_requirement()
        current = allocation(operator_resource_id="R1")

        self.assertEqual(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1"),
                human_requirement=human_requirement(),
            ),
            ASSOCIATION_INHERITED_REQUEST,
        )
        self.assertIsNone(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R2"),
                human_requirement=human_requirement(identifier="RR2"),
            )
        )

    def test_request_operator_change_moves_projection_without_changing_allocation_identity(self) -> None:
        requirement = request_requirement()
        current = allocation(operator_resource_id="R1")
        allocation_identity = id(current)

        self.assertEqual(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1"),
                human_requirement=human_requirement(),
            ),
            ASSOCIATION_INHERITED_REQUEST,
        )
        current.operator_resource_id = "R2"
        self.assertEqual(id(current), allocation_identity)
        self.assertIsNone(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1"),
                human_requirement=human_requirement(),
            )
        )
        self.assertEqual(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R2"),
                human_requirement=human_requirement(identifier="RR2"),
            ),
            ASSOCIATION_INHERITED_REQUEST,
        )

    def test_request_date_and_project_boundaries_are_preserved(self) -> None:
        requirement = request_requirement()
        current = allocation(operator_resource_id="R1", start_date=DAY + timedelta(days=1))

        self.assertIsNone(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1", work_date=DAY),
                human_requirement=human_requirement(),
            )
        )
        self.assertEqual(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1", work_date=DAY + timedelta(days=1)),
                human_requirement=human_requirement(),
            ),
            ASSOCIATION_INHERITED_REQUEST,
        )
        self.assertIsNone(
            asset_shift_association(
                requirement=requirement,
                allocation=current,
                shift=shift(resource_id="R1", work_date=DAY + timedelta(days=1)),
                human_requirement=human_requirement(project_id="P2"),
            )
        )

    def test_operatorless_request_remains_related_work_context(self) -> None:
        self.assertEqual(
            asset_shift_association(
                requirement=request_requirement(),
                allocation=allocation(operator_resource_id=None),
                shift=shift(resource_id="R2"),
                human_requirement=human_requirement(identifier="RR2"),
            ),
            ASSOCIATION_RELATED_REQUEST,
        )

    def test_existing_modes_keep_their_association_contracts(self) -> None:
        project = human_requirement()
        current_shift = shift(resource_id="R1")

        self.assertEqual(
            asset_shift_association(
                requirement=SimpleNamespace(status="Planifié", origin="SHIFT_AD_HOC", shift_id=current_shift.id),
                allocation=allocation(operator_resource_id="R1"),
                shift=current_shift,
                human_requirement=project,
            ),
            ASSOCIATION_OWNED_SHIFT,
        )
        self.assertEqual(
            asset_shift_association(
                requirement=SimpleNamespace(status="Planifié", origin="PROJECT_DIRECT", project_id="P1"),
                allocation=allocation(operator_resource_id="R1"),
                shift=current_shift,
                human_requirement=project,
            ),
            ASSOCIATION_INHERITED_PROJECT_DIRECT,
        )
        self.assertEqual(
            asset_shift_association(
                requirement=SimpleNamespace(status="Planifié", origin="SEGMENT", resource_requirement_id="RR1"),
                allocation=allocation(operator_resource_id="R1"),
                shift=current_shift,
                human_requirement=project,
            ),
            ASSOCIATION_INHERITED_SEGMENT,
        )
        self.assertEqual(
            asset_shift_association(
                requirement=SimpleNamespace(status="Planifié", origin="RESOURCE_PERIOD", context_resource_id="R1"),
                allocation=allocation(operator_resource_id="R1"),
                shift=current_shift,
                human_requirement=project,
            ),
            ASSOCIATION_INHERITED_RESOURCE_PERIOD,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import date
import unittest

from app.domain.demand_periods import (
    CONFIRMATION_MODE_EXPLICIT,
    CONFIRMATION_MODE_INHERIT_MASTER,
    PERIOD_INHERITANCE_CONTRACT_VERSION,
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
    PERIOD_PROVENANCE_EXPLICIT,
    PERIOD_PROVENANCE_LEGACY,
    PERIOD_PROVENANCE_MASTER,
    PERIOD_PROVENANCE_SAME_AS,
    PROPOSED_RESOURCE_MODE_EXPLICIT,
    PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
    DemandPeriodDefinition,
    effective_period_ids,
    projected_hours_without_double_counting,
    resolve_period_authority,
    validate_period_definitions,
)


DAY_1 = date(2026, 9, 7)
DAY_2 = date(2026, 9, 8)


class DemandPeriodPolicyTests(unittest.TestCase):
    def alternatives(self) -> tuple[DemandPeriodDefinition, DemandPeriodDefinition]:
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
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="TECH-DAY",
                proposed_resource="Technicien B",
            ),
        )

    def test_legacy_resolution_preserves_period_authority(self) -> None:
        resolved = resolve_period_authority(
            contract_version=None,
            stored_resource_count=3,
            stored_confirmation="Tentative",
            stored_proposed_resource="R-OLD",
            confirmation_mode=None,
            proposed_resource_mode=None,
            same_as_period_id=None,
            master_resource_count=9,
            master_confirmation="Confirmée",
            master_proposed_resource="R-MASTER",
        )

        self.assertEqual(resolved.resource_count, 3)
        self.assertEqual(resolved.confirmation, "Tentative")
        self.assertEqual(resolved.proposed_resource, "R-OLD")
        self.assertEqual(resolved.resource_count_provenance, PERIOD_PROVENANCE_LEGACY)
        self.assertEqual(resolved.confirmation_provenance, PERIOD_PROVENANCE_LEGACY)
        self.assertEqual(resolved.proposed_resource_provenance, PERIOD_PROVENANCE_LEGACY)

    def test_modern_inheritance_derives_master_authority(self) -> None:
        resolved = resolve_period_authority(
            contract_version=PERIOD_INHERITANCE_CONTRACT_VERSION,
            stored_resource_count=99,
            stored_confirmation="Tentative",
            stored_proposed_resource="R-STALE",
            confirmation_mode=CONFIRMATION_MODE_INHERIT_MASTER,
            proposed_resource_mode=PROPOSED_RESOURCE_MODE_INHERIT_MASTER,
            same_as_period_id=None,
            master_resource_count=4,
            master_confirmation="Confirmée",
            master_proposed_resource="R-MASTER",
        )

        self.assertEqual(resolved.resource_count, 4)
        self.assertEqual(resolved.confirmation, "Confirmée")
        self.assertEqual(resolved.proposed_resource, "R-MASTER")
        self.assertEqual(resolved.resource_count_provenance, PERIOD_PROVENANCE_MASTER)
        self.assertEqual(resolved.confirmation_provenance, PERIOD_PROVENANCE_MASTER)
        self.assertEqual(resolved.proposed_resource_provenance, PERIOD_PROVENANCE_MASTER)

    def test_explicit_none_resource_does_not_inherit_master(self) -> None:
        resolved = resolve_period_authority(
            contract_version=PERIOD_INHERITANCE_CONTRACT_VERSION,
            stored_resource_count=1,
            stored_confirmation="Tentative",
            stored_proposed_resource=None,
            confirmation_mode=CONFIRMATION_MODE_EXPLICIT,
            proposed_resource_mode=PROPOSED_RESOURCE_MODE_EXPLICIT,
            same_as_period_id=None,
            master_resource_count=2,
            master_confirmation="Confirmée",
            master_proposed_resource="R-MASTER",
        )

        self.assertIsNone(resolved.proposed_resource)
        self.assertEqual(resolved.proposed_resource_provenance, PERIOD_PROVENANCE_EXPLICIT)
        self.assertEqual(resolved.resource_count, 2)

    def test_same_as_period_is_only_projected_until_655b(self) -> None:
        resolved = resolve_period_authority(
            contract_version=PERIOD_INHERITANCE_CONTRACT_VERSION,
            stored_resource_count=1,
            stored_confirmation="Tentative",
            stored_proposed_resource=None,
            confirmation_mode=CONFIRMATION_MODE_EXPLICIT,
            proposed_resource_mode=PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
            same_as_period_id="P-ROOT",
            master_resource_count=1,
            master_confirmation="Tentative",
            master_proposed_resource="R-MASTER",
        )

        self.assertIsNone(resolved.proposed_resource)
        self.assertEqual(resolved.proposed_resource_provenance, PERIOD_PROVENANCE_SAME_AS)
        self.assertEqual(resolved.same_as_period_id, "P-ROOT")
        self.assertEqual(resolved.same_as_state, "DEFERRED_655B")

    def test_unresolved_alternative_group_materializes_neither_option(self) -> None:
        periods = self.alternatives()
        self.assertEqual(effective_period_ids(periods, {}), ())

    def test_selecting_one_alternative_never_materializes_the_other(self) -> None:
        periods = self.alternatives()
        self.assertEqual(
            effective_period_ids(periods, {"TECH-DAY": "OPT-A"}),
            ("OPT-A",),
        )
        self.assertEqual(
            effective_period_ids(periods, {"TECH-DAY": "OPT-B"}),
            ("OPT-B",),
        )

    def test_unresolved_group_projects_once_instead_of_summing_both_options(self) -> None:
        periods = (
            self.alternatives()[0],
            DemandPeriodDefinition(
                period_id="OPT-B",
                start_date=DAY_2,
                end_date=DAY_2,
                hours=6,
                kind=PERIOD_KIND_ALTERNATIVE,
                alternative_group="TECH-DAY",
                proposed_resource="Technicien B",
            ),
        )
        self.assertEqual(projected_hours_without_double_counting(periods), 8.0)
        self.assertEqual(
            projected_hours_without_double_counting(
                periods, {"TECH-DAY": "OPT-B"}
            ),
            6.0,
        )

    def test_cumulative_periods_add_to_one_projected_alternative(self) -> None:
        periods = (
            DemandPeriodDefinition(
                period_id="FIXED",
                start_date=DAY_1,
                end_date=DAY_1,
                hours=4,
                kind=PERIOD_KIND_CUMULATIVE,
            ),
            *self.alternatives(),
        )
        self.assertEqual(projected_hours_without_double_counting(periods), 12.0)
        self.assertEqual(
            effective_period_ids(periods, {"TECH-DAY": "OPT-A"}),
            ("FIXED", "OPT-A"),
        )

    def test_singleton_alternative_group_is_invalid(self) -> None:
        with self.assertRaisesRegex(ValueError, "au moins deux options"):
            validate_period_definitions((self.alternatives()[0],))

    def test_selection_must_belong_to_requested_group(self) -> None:
        periods = self.alternatives()
        with self.assertRaisesRegex(ValueError, "n'appartient pas"):
            effective_period_ids(periods, {"AUTRE-GROUPE": "OPT-A"})


if __name__ == "__main__":
    unittest.main()

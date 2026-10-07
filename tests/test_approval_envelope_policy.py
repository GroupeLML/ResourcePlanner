from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.domain.approval_envelope import (
    CHANGE_ALTERNATIVE_SELECTION,
    CHANGE_CONFIRMATION,
    DECISION_APPROVAL_REFERENCE_UNKNOWN,
    DECISION_REAPPROVAL_REQUIRED,
    DECISION_WITHIN_ENVELOPE,
    REASON_BUDGET_INCREASE,
    REASON_DELEGATED_TOLERANCE,
    REASON_OPERATIONAL_ONLY,
    REASON_TOPOLOGY_CHANGED,
    REASON_WINDOW_CHANGED,
    ROLE_PROJECT_MANAGER,
    EnvelopeLineDefinition,
    EnvelopePeriodDefinition,
    compare_approval_envelopes,
    normalize_approval_envelope,
)
from app.domain.demand_periods import (
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
)


DAY_1 = date(2026, 9, 21)
DAY_2 = date(2026, 9, 22)
DAY_3 = date(2026, 9, 23)
DAY_4 = date(2026, 9, 24)


def simple_line(
    *,
    hours: float | Decimal = 8,
    start: date = DAY_1,
    end: date = DAY_2,
    confirmation: str = "Tentative",
    proposed_resource_id: str | None = None,
    desired_active_days: int | None = None,
    project_id: str = "PROJECT-1",
    site_id: str | None = "SITE-1",
    competency_ids: tuple[str, ...] = ("COMP-B", "COMP-A"),
) -> EnvelopeLineDefinition:
    return EnvelopeLineDefinition(
        line_id="LINE-1",
        project_id=project_id,
        site_id=site_id,
        required_resource_class="AUTOMATION",
        competency_ids=competency_ids,
        task_ref="TASK-1",
        work_package_ref="WP-1",
        start_date=start,
        end_date=end,
        hours=hours,
        confirmation=confirmation,
        proposed_resource_id=proposed_resource_id,
        desired_active_days=desired_active_days,
    )


def alternative_line(
    *,
    selected: str = "A",
    confirmation_a: str = "Tentative",
    confirmation_b: str = "Tentative",
) -> EnvelopeLineDefinition:
    return EnvelopeLineDefinition(
        line_id="LINE-ALT",
        project_id="PROJECT-1",
        hours=999,
        start_date=DAY_1,
        end_date=DAY_4,
        periods=(
            EnvelopePeriodDefinition(
                period_key="OPT-A",
                start_date=DAY_1,
                end_date=DAY_1,
                hours=8,
                kind=PERIOD_KIND_ALTERNATIVE,
                group_key="GROUP-1",
                confirmation=confirmation_a,
                selected=selected == "A",
            ),
            EnvelopePeriodDefinition(
                period_key="OPT-B",
                start_date=DAY_3,
                end_date=DAY_3,
                hours=6,
                kind=PERIOD_KIND_ALTERNATIVE,
                group_key="GROUP-1",
                confirmation=confirmation_b,
                selected=selected == "B",
            ),
        ),
    )


class ApprovalEnvelopeNormalizationTests(unittest.TestCase):
    def test_simple_line_normalizes_to_one_line_identity(self) -> None:
        envelope = normalize_approval_envelope((simple_line(hours=12),))

        self.assertEqual(len(envelope.entries), 1)
        entry = envelope.entries[0]
        self.assertEqual(entry.identity.line_id, "LINE-1")
        self.assertIsNone(entry.identity.period_key)
        self.assertEqual(entry.hours, Decimal("12.00"))
        self.assertEqual(entry.competency_ids, ("COMP-A", "COMP-B"))

    def test_explicit_periods_replace_simple_line_budget_instead_of_adding_it(self) -> None:
        line = EnvelopeLineDefinition(
            line_id="LINE-1",
            project_id="PROJECT-1",
            hours=100,
            start_date=DAY_1,
            end_date=DAY_4,
            periods=(
                EnvelopePeriodDefinition(
                    period_key="FIXED",
                    start_date=DAY_1,
                    end_date=DAY_1,
                    hours=4,
                    kind=PERIOD_KIND_CUMULATIVE,
                ),
                EnvelopePeriodDefinition(
                    period_key="OPT-A",
                    start_date=DAY_2,
                    end_date=DAY_2,
                    hours=8,
                    kind=PERIOD_KIND_ALTERNATIVE,
                    group_key="GROUP-1",
                    selected=True,
                ),
                EnvelopePeriodDefinition(
                    period_key="OPT-B",
                    start_date=DAY_3,
                    end_date=DAY_3,
                    hours=6,
                    kind=PERIOD_KIND_ALTERNATIVE,
                    group_key="GROUP-1",
                ),
            ),
        )

        envelope = normalize_approval_envelope((line,))

        self.assertEqual(
            [entry.identity.period_key for entry in envelope.entries],
            ["FIXED", "OPT-A", "OPT-B"],
        )
        self.assertEqual(
            sum((entry.hours for entry in envelope.entries), Decimal("0")),
            Decimal("18.00"),
        )
        self.assertNotEqual(
            sum((entry.hours for entry in envelope.entries), Decimal("0")),
            Decimal("118.00"),
        )

    def test_alternative_group_rejects_two_active_selections(self) -> None:
        line = EnvelopeLineDefinition(
            line_id="LINE-ALT",
            project_id="PROJECT-1",
            periods=(
                EnvelopePeriodDefinition(
                    period_key="OPT-A",
                    start_date=DAY_1,
                    end_date=DAY_1,
                    hours=8,
                    kind=PERIOD_KIND_ALTERNATIVE,
                    group_key="GROUP-1",
                    selected=True,
                ),
                EnvelopePeriodDefinition(
                    period_key="OPT-B",
                    start_date=DAY_2,
                    end_date=DAY_2,
                    hours=8,
                    kind=PERIOD_KIND_ALTERNATIVE,
                    group_key="GROUP-1",
                    selected=True,
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "plus d'une option sélectionnée"):
            normalize_approval_envelope((line,))


class ApprovalEnvelopeComparisonTests(unittest.TestCase):
    def test_confirmation_only_is_operational_and_keeps_same_authorization(self) -> None:
        approved = normalize_approval_envelope(
            (simple_line(confirmation="Tentative"),)
        )
        candidate = normalize_approval_envelope(
            (simple_line(confirmation="Confirmée"),)
        )

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_WITHIN_ENVELOPE)
        self.assertEqual(decision.reason, REASON_OPERATIONAL_ONLY)
        self.assertIn(CHANGE_CONFIRMATION, {row.code for row in decision.changes})
        self.assertEqual(
            approved.authorization_fingerprint,
            candidate.authorization_fingerprint,
        )

    def test_period_inheritance_mode_change_requires_reapproval(self) -> None:
        def envelope(mode: str):
            return normalize_approval_envelope(
                (
                    EnvelopeLineDefinition(
                        line_id="LINE-INHERIT",
                        project_id="PROJECT-1",
                        start_date=DAY_1,
                        end_date=DAY_2,
                        periods=(
                            EnvelopePeriodDefinition(
                                period_key="P1",
                                start_date=DAY_1,
                                end_date=DAY_2,
                                hours=8,
                                confirmation="Tentative",
                                confirmation_mode=mode,
                                proposed_resource_mode="EXPLICIT",
                            ),
                        ),
                    ),
                )
            )

        approved = envelope("INHERIT_MASTER")
        candidate = envelope("EXPLICIT")
        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)
        self.assertIn(REASON_TOPOLOGY_CHANGED, {row.code for row in decision.changes})
        self.assertNotEqual(
            approved.authorization_fingerprint,
            candidate.authorization_fingerprint,
        )

    def test_switching_between_preapproved_alternatives_is_operational(self) -> None:
        approved = normalize_approval_envelope((alternative_line(selected="A"),))
        candidate = normalize_approval_envelope((alternative_line(selected="B"),))

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_WITHIN_ENVELOPE)
        self.assertEqual(decision.reason, REASON_OPERATIONAL_ONLY)
        self.assertEqual(
            sum(
                1
                for row in decision.changes
                if row.code == CHANGE_ALTERNATIVE_SELECTION
            ),
            2,
        )
        self.assertEqual(
            approved.authorization_fingerprint,
            candidate.authorization_fingerprint,
        )

    def test_disjoint_windows_cannot_be_merged_into_one_authorized_window(self) -> None:
        approved = normalize_approval_envelope(
            (
                EnvelopeLineDefinition(
                    line_id="LINE-1",
                    project_id="PROJECT-1",
                    periods=(
                        EnvelopePeriodDefinition(
                            period_key="P1",
                            start_date=DAY_1,
                            end_date=DAY_1,
                            hours=4,
                        ),
                        EnvelopePeriodDefinition(
                            period_key="P2",
                            start_date=DAY_3,
                            end_date=DAY_3,
                            hours=4,
                        ),
                    ),
                ),
            )
        )
        candidate = normalize_approval_envelope(
            (
                EnvelopeLineDefinition(
                    line_id="LINE-1",
                    project_id="PROJECT-1",
                    periods=(
                        EnvelopePeriodDefinition(
                            period_key="P1",
                            start_date=DAY_1,
                            end_date=DAY_2,
                            hours=4,
                        ),
                        EnvelopePeriodDefinition(
                            period_key="P2",
                            start_date=DAY_3,
                            end_date=DAY_3,
                            hours=4,
                        ),
                    ),
                ),
            )
        )

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)
        self.assertIn(REASON_WINDOW_CHANGED, {row.code for row in decision.changes})

    def test_equal_total_hours_cannot_transfer_budget_between_periods(self) -> None:
        def request(first: int, second: int) -> tuple[EnvelopeLineDefinition, ...]:
            return (
                EnvelopeLineDefinition(
                    line_id="LINE-1",
                    project_id="PROJECT-1",
                    periods=(
                        EnvelopePeriodDefinition(
                            period_key="P1",
                            start_date=DAY_1,
                            end_date=DAY_1,
                            hours=first,
                        ),
                        EnvelopePeriodDefinition(
                            period_key="P2",
                            start_date=DAY_2,
                            end_date=DAY_2,
                            hours=second,
                        ),
                    ),
                ),
            )

        approved = normalize_approval_envelope(request(8, 8))
        candidate = normalize_approval_envelope(request(6, 10))

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)
        self.assertEqual(
            sum((entry.hours for entry in approved.entries), Decimal("0")),
            sum((entry.hours for entry in candidate.entries), Decimal("0")),
        )

    def test_project_manager_can_increase_one_local_budget_to_twenty_percent(self) -> None:
        approved = normalize_approval_envelope((simple_line(hours=100),))
        candidate = normalize_approval_envelope((simple_line(hours=120),))

        decision = compare_approval_envelopes(
            approved,
            candidate,
            actor_role=ROLE_PROJECT_MANAGER,
        )

        self.assertEqual(decision.decision, DECISION_WITHIN_ENVELOPE)
        self.assertEqual(decision.reason, REASON_DELEGATED_TOLERANCE)
        change = next(
            row
            for row in decision.changes
            if row.code == REASON_DELEGATED_TOLERANCE
        )
        self.assertEqual(change.reference_hours, Decimal("100.00"))
        self.assertEqual(change.candidate_hours, Decimal("120.00"))
        self.assertEqual(change.delta_percent, Decimal("20.00"))

    def test_project_manager_tolerance_does_not_compound_past_approved_reference(self) -> None:
        approved = normalize_approval_envelope((simple_line(hours=100),))
        candidate = normalize_approval_envelope((simple_line(hours=144),))

        decision = compare_approval_envelopes(
            approved,
            candidate,
            actor_role=ROLE_PROJECT_MANAGER,
        )

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)
        self.assertIn(REASON_BUDGET_INCREASE, {row.code for row in decision.changes})

    def test_project_manager_budget_above_twenty_percent_requires_reapproval(self) -> None:
        approved = normalize_approval_envelope((simple_line(hours=100),))
        candidate = normalize_approval_envelope(
            (simple_line(hours=Decimal("120.01")),)
        )

        decision = compare_approval_envelopes(
            approved,
            candidate,
            actor_role=ROLE_PROJECT_MANAGER,
        )

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)
        self.assertIn(REASON_BUDGET_INCREASE, {row.code for row in decision.changes})

    def test_scope_change_requires_reapproval_even_when_effort_is_unchanged(self) -> None:
        approved = normalize_approval_envelope((simple_line(project_id="P-1"),))
        candidate = normalize_approval_envelope((simple_line(project_id="P-2"),))

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_REAPPROVAL_REQUIRED)

    def test_distribution_preference_and_proposed_resource_do_not_widen_authority(self) -> None:
        approved = normalize_approval_envelope(
            (
                simple_line(
                    proposed_resource_id="R-A",
                    desired_active_days=2,
                ),
            )
        )
        candidate = normalize_approval_envelope(
            (
                simple_line(
                    proposed_resource_id="R-B",
                    desired_active_days=1,
                ),
            )
        )

        decision = compare_approval_envelopes(approved, candidate)

        self.assertEqual(decision.decision, DECISION_WITHIN_ENVELOPE)
        self.assertEqual(decision.reason, REASON_OPERATIONAL_ONLY)
        self.assertEqual(
            approved.authorization_fingerprint,
            candidate.authorization_fingerprint,
        )

    def test_missing_historical_reference_is_explicit(self) -> None:
        candidate = normalize_approval_envelope((simple_line(),))

        decision = compare_approval_envelopes(
            None,
            candidate,
            approval_reference_known=False,
        )

        self.assertEqual(
            decision.decision,
            DECISION_APPROVAL_REFERENCE_UNKNOWN,
        )

    def test_snapshot_payload_keeps_trace_fields_without_making_them_authority(self) -> None:
        envelope = normalize_approval_envelope(
            (
                alternative_line(
                    selected="A",
                    confirmation_a="Confirmée",
                ),
            )
        )

        payload = envelope.to_snapshot_payload()

        self.assertEqual(payload["format_version"], 1)
        entries = payload["entries"]
        self.assertTrue(any(row["selected"] for row in entries))
        self.assertTrue(any(row["confirmation"] == "Confirmée" for row in entries))
        self.assertEqual(
            payload["authorization_fingerprint"],
            envelope.authorization_fingerprint,
        )


if __name__ == "__main__":
    unittest.main()

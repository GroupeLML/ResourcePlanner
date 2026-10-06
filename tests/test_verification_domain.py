from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import inspect
import unittest

from app.domain.verification import (
    StoryVerificationDecision,
    VerificationDecisionKind,
    VerificationExecutionResult,
    VerificationPhase,
    VerificationProjectedStatus,
    VerificationRequirement,
    VerificationRequirementRevision,
    VerificationRequirementState,
    VerificationRetestRequest,
    VerificationScope,
    adopt_requirement_revision,
    validate_requirement_revision_chain,
    validate_retest_request,
    validate_story_verification_decision,
    withdraw_requirement,
)


def revision(
    identifier: str,
    *,
    requirement_id: str = "REQ-1",
    number: int = 1,
    method: str = "Actionner la pompe",
) -> VerificationRequirementRevision:
    return VerificationRequirementRevision(
        id=identifier,
        requirement_id=requirement_id,
        revision_number=number,
        objective="Valider le démarrage",
        method=method,
        expected_result="La pompe démarre sans alarme",
        prerequisites=("Alimentation disponible",),
        criticality="HIGH",
    )


def requirement(
    identifier: str = "REQ-1",
    *,
    story_id: str = "STORY-1",
    current_revision_id: str = "REV-1",
    phase: VerificationPhase = VerificationPhase.FAT,
) -> VerificationRequirement:
    return VerificationRequirement(
        id=identifier,
        verification_scope_id="VS-1",
        story_id=story_id,
        phase=phase,
        current_revision_id=current_revision_id,
    )


class VerificationDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = VerificationScope(
            id="VS-1",
            work_package_id="WP-1",
            lead_user_id="USER-LEAD",
        )

    def test_scope_has_independent_verification_version(self) -> None:
        self.assertEqual(self.scope.verification_version, 1)
        names = {field.name for field in fields(VerificationScope)}
        self.assertNotIn("planning_version", names)
        self.assertNotIn("delivery_version", names)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            VerificationScope(
                id="VS-2",
                work_package_id="WP-1",
                verification_version=0,
            )

    def test_requirement_has_one_phase_and_stable_story_origin(self) -> None:
        item = requirement(phase=VerificationPhase.COMMISSIONING)
        self.assertEqual(item.phase, VerificationPhase.COMMISSIONING)
        self.assertEqual(item.story_id, "STORY-1")
        with self.assertRaises(ValueError):
            VerificationRequirement(
                id="REQ-X",
                verification_scope_id="VS-1",
                story_id="STORY-1",
                phase="FAT,SAT",
                current_revision_id="REV-X",
            )

    def test_requirement_revision_is_immutable_and_requires_complete_definition(
        self,
    ) -> None:
        item = revision("REV-1")
        self.assertEqual(item.prerequisites, ("Alimentation disponible",))
        with self.assertRaises(FrozenInstanceError):
            item.method = "Nouvelle méthode"  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "expected_result"):
            VerificationRequirementRevision(
                id="REV-X",
                requirement_id="REQ-1",
                revision_number=1,
                objective="Objectif",
                method="Méthode",
                expected_result="",
            )

    def test_revision_chain_is_contiguous_and_current_revision_is_latest(self) -> None:
        req = requirement(current_revision_id="REV-2")
        revisions = (
            revision("REV-1", number=1),
            revision(
                "REV-2",
                number=2,
                method="Actionner puis couper la pompe",
            ),
        )
        validate_requirement_revision_chain(req, revisions)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            validate_requirement_revision_chain(
                requirement(current_revision_id="REV-3"),
                (
                    revision("REV-1", number=1),
                    revision("REV-3", number=3),
                ),
            )
        with self.assertRaisesRegex(ValueError, "latest"):
            validate_requirement_revision_chain(
                requirement(current_revision_id="REV-1"),
                revisions,
            )

    def test_revision_adoption_never_mutates_previous_definition(self) -> None:
        req = requirement(current_revision_id="REV-1")
        old = revision("REV-1", number=1)
        new = revision("REV-2", number=2, method="Méthode révisée")
        revised = adopt_requirement_revision(req, old, new)
        self.assertEqual(req.current_revision_id, "REV-1")
        self.assertEqual(revised.current_revision_id, "REV-2")
        self.assertEqual(old.method, "Actionner la pompe")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            adopt_requirement_revision(
                req,
                old,
                revision("REV-3", number=3),
            )

    def test_no_test_required_requires_justification_and_no_requirements(
        self,
    ) -> None:
        decision = StoryVerificationDecision(
            id="DEC-1",
            verification_scope_id="VS-1",
            story_id="STORY-1",
            kind=VerificationDecisionKind.NO_TEST_REQUIRED,
            justification="Modification documentaire seulement",
        )
        validate_story_verification_decision(self.scope, decision, (), ())
        with self.assertRaisesRegex(ValueError, "justification"):
            StoryVerificationDecision(
                id="DEC-X",
                verification_scope_id="VS-1",
                story_id="STORY-1",
                kind=VerificationDecisionKind.NO_TEST_REQUIRED,
            )
        with self.assertRaisesRegex(ValueError, "cannot reference"):
            StoryVerificationDecision(
                id="DEC-Y",
                verification_scope_id="VS-1",
                story_id="STORY-1",
                kind=VerificationDecisionKind.NO_TEST_REQUIRED,
                justification="Aucun test",
                requirement_ids=("REQ-1",),
            )

    def test_tests_defined_requires_active_well_defined_requirements_for_same_story(
        self,
    ) -> None:
        req = requirement()
        rev = revision("REV-1")
        decision = StoryVerificationDecision(
            id="DEC-1",
            verification_scope_id="VS-1",
            story_id="STORY-1",
            kind=VerificationDecisionKind.TESTS_DEFINED,
            requirement_ids=("REQ-1",),
        )
        validate_story_verification_decision(
            self.scope,
            decision,
            (req,),
            (rev,),
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            StoryVerificationDecision(
                id="DEC-X",
                verification_scope_id="VS-1",
                story_id="STORY-1",
                kind=VerificationDecisionKind.TESTS_DEFINED,
            )
        with self.assertRaisesRegex(ValueError, "another Story"):
            validate_story_verification_decision(
                self.scope,
                decision,
                (requirement(story_id="STORY-2"),),
                (rev,),
            )

    def test_withdrawal_is_motivated_and_preserves_requirement_identity(self) -> None:
        req = requirement()
        withdrawn = withdraw_requirement(
            req,
            reason="Test devenu hors périmètre",
        )
        self.assertEqual(withdrawn.id, req.id)
        self.assertEqual(
            withdrawn.current_revision_id,
            req.current_revision_id,
        )
        self.assertEqual(
            withdrawn.state,
            VerificationRequirementState.WITHDRAWN,
        )
        self.assertEqual(
            withdrawn.withdrawal_reason,
            "Test devenu hors périmètre",
        )
        with self.assertRaisesRegex(ValueError, "withdrawal reason"):
            withdraw_requirement(req, reason=" ")

    def test_not_run_is_projection_only_not_an_execution_result(self) -> None:
        self.assertNotIn(
            "NOT_RUN",
            {result.value for result in VerificationExecutionResult},
        )
        self.assertIn(
            "NOT_RUN",
            {status.value for status in VerificationProjectedStatus},
        )

    def test_retest_targets_current_revision_without_erasing_history_contract(
        self,
    ) -> None:
        req = requirement(current_revision_id="REV-2")
        request = VerificationRetestRequest(
            id="RETEST-1",
            requirement_id="REQ-1",
            revision_id="REV-2",
            after_execution_sequence=12,
            reason="Correction appliquée après FAIL",
        )
        validate_retest_request(req, request)
        self.assertEqual(request.after_execution_sequence, 12)
        with self.assertRaisesRegex(
            ValueError,
            "current requirement revision",
        ):
            validate_retest_request(
                req,
                VerificationRetestRequest(
                    id="RETEST-2",
                    requirement_id="REQ-1",
                    revision_id="REV-1",
                    after_execution_sequence=12,
                    reason="Retest ancien contrat",
                ),
            )

    def test_pure_verification_contracts_do_not_accept_planning_or_delivery_versions(
        self,
    ) -> None:
        for function in (
            validate_story_verification_decision,
            validate_retest_request,
            withdraw_requirement,
            adopt_requirement_revision,
        ):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("planning_version", parameters)
            self.assertNotIn("delivery_version", parameters)


if __name__ == "__main__":
    unittest.main()

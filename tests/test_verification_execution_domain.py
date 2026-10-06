from __future__ import annotations

import unittest

from app.domain.verification import (
    VerificationExecutionResult,
    VerificationProjectedStatus,
)
from app.domain.verification_execution import (
    VerificationEvidenceLink,
    VerificationExecution,
    normalize_https_evidence_url,
    normalize_verification_measurements,
    project_requirement_status,
)


class VerificationExecutionDomainTests(unittest.TestCase):
    def test_external_evidence_accepts_https_only(self) -> None:
        self.assertEqual(
            normalize_https_evidence_url(" https://evidence.example/fat/1 "),
            "https://evidence.example/fat/1",
        )
        for invalid in (
            "http://evidence.example/fat/1",
            "file:///tmp/proof.jpg",
            "https://user:secret@example.test/proof",
            "example.test/proof",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_https_evidence_url(invalid)

    def test_measurements_are_flat_json_scalars(self) -> None:
        self.assertEqual(
            normalize_verification_measurements(
                {"pressure_bar": 5.2, "alarm": False, "note": "stable"}
            ),
            {"pressure_bar": 5.2, "alarm": False, "note": "stable"},
        )
        with self.assertRaises(ValueError):
            normalize_verification_measurements(
                {"nested": {"value": 1}}  # type: ignore[arg-type]
            )

    def test_projection_uses_latest_current_revision_after_retest_boundary(self) -> None:
        history = (
            VerificationExecution(
                id="EX-1",
                requirement_id="REQ-1",
                revision_id="REV-1",
                sequence=1,
                result=VerificationExecutionResult.PASS,
                executor_user_id="TECH",
            ),
            VerificationExecution(
                id="EX-2",
                requirement_id="REQ-1",
                revision_id="REV-1",
                sequence=2,
                result=VerificationExecutionResult.FAIL,
                executor_user_id="TECH",
            ),
            VerificationExecution(
                id="EX-3",
                requirement_id="REQ-1",
                revision_id="REV-2",
                sequence=3,
                result=VerificationExecutionResult.BLOCKED,
                executor_user_id="TECH",
            ),
        )
        self.assertEqual(
            project_requirement_status(
                current_revision_id="REV-1",
                executions=history,
            ),
            VerificationProjectedStatus.FAIL,
        )
        self.assertEqual(
            project_requirement_status(
                current_revision_id="REV-1",
                executions=history,
                retest_after_sequence=2,
            ),
            VerificationProjectedStatus.NOT_RUN,
        )
        self.assertEqual(
            project_requirement_status(
                current_revision_id="REV-2",
                executions=history,
            ),
            VerificationProjectedStatus.BLOCKED,
        )

    def test_evidence_link_keeps_provenance_without_fetching_content(self) -> None:
        evidence = VerificationEvidenceLink(
            id="EV-1",
            execution_id="EX-1",
            url="https://share.example/proof?id=1",
            label="Capture FAT",
            provenance="sharepoint-reference",
            added_by_user_id="TECH",
        )
        self.assertEqual(evidence.url, "https://share.example/proof?id=1")
        self.assertEqual(evidence.provenance, "sharepoint-reference")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactDemandWorkflowContractTests(unittest.TestCase):
    def test_workflow_is_contextual_inside_unified_demand_detail(self) -> None:
        workspace = (ROOT / "frontend" / "src" / "DemandsWorkspace.tsx").read_text(
            encoding="utf-8"
        )
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        main = (ROOT / "frontend" / "src" / "main.tsx").read_text(encoding="utf-8")

        self.assertIn("<DemandsPage />", workspace)
        self.assertNotIn("<DemandWorkflowPage />", workspace)
        self.assertIn("<DemandWorkflowPage", detail)
        self.assertIn("embedded", detail)
        self.assertIn('import "./demand-workflow.css"', main)

    def test_workflow_client_uses_authoritative_backend_policy(self) -> None:
        source = (ROOT / "frontend" / "src" / "demandWorkflowApi.ts").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("/workflow-actions", source)
        self.assertIn("getDemandWorkflowState", source)
        self.assertIn("expected_version", source)
        self.assertIn("/api/v1/demands/${encodeURIComponent(number)}/${action}", source)
        self.assertIn("currentWorkflowState?.available_actions", page)
        self.assertIn("getDemandDetail", page)
        self.assertIn("canonicalDetail.workflow as DemandWorkflowState", page)
        self.assertIn("canonicalDetail?.demand ?? selectedDemand", page)
        self.assertNotIn("getDemandWorkflowState", page)
        self.assertIn("currentWorkflowState?.version ?? currentDemand.version", page)
        self.assertNotIn("function expectedActions", page)
        post_count = source.count('method: "POST"')
        self.assertGreater(post_count, 0)
        self.assertEqual(post_count, source.count("...csrfHeaders()"))
        self.assertEqual(post_count, source.count('credentials: "include"'))

    def test_submit_and_cancel_are_visible_primary_backend_actions(self) -> None:
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-testid="demand-workflow-section"', detail)
        self.assertIn('<details className="demand-detail-section" open', detail)
        self.assertIn('data-testid="primary-demand-actions"', page)
        self.assertIn('actions.includes("submit")', page)
        self.assertIn('actions.includes("cancel")', page)
        self.assertIn('onClick={() => runAction("submit")}', page)
        self.assertIn('onClick={() => runAction("cancel")}', page)
        self.assertIn("await refreshAfterMutation(result.demand_number)", page)

    def test_workflow_separates_approval_from_confirmation(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Approbation / statut", source)
        self.assertIn("Confirmation", source)
        self.assertIn("Approbation ≠ confirmation.", source)
        self.assertIn("currentDemand.confirmation", source)
        self.assertIn("currentDemand.status", source)

    def test_workflow_blocks_mutations_while_any_editor_is_dirty(self) -> None:
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )
        demands = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("hasUnsavedChanges={editorDirty || contextDirty}", demands)
        self.assertIn("hasUnsavedChanges={hasUnsavedChanges}", detail)
        self.assertIn("if (hasUnsavedChanges)", page)
        self.assertIn("Enregistre les modifications avant de poursuivre.", page)
        self.assertIn("disabled={busy || hasUnsavedChanges}", page)

    def test_workflow_requires_correction_comment_and_guards_double_clicks(self) -> None:
        source = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("if (!currentDemand || pendingAction) return", source)
        self.assertIn('action === "correction" && !correctionComment.trim()', source)
        self.assertIn("disabled={busy || hasUnsavedChanges || !correctionComment.trim()}", source)
        self.assertIn("const busy = pendingAction !== null", source)
        self.assertIn("await refreshAfterMutation(result.demand_number)", source)
        self.assertIn("result.planning", source)

    def test_materialized_cancellation_uses_backend_actions_versions_and_idempotency(self) -> None:
        client = (ROOT / "frontend" / "src" / "demandWorkflowApi.ts").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )
        detail = (ROOT / "frontend" / "src" / "DemandDetail.tsx").read_text(
            encoding="utf-8"
        )
        demands = (ROOT / "frontend" / "src" / "DemandsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('"request-cancellation"', client)
        self.assertIn('"accept-cancellation"', client)
        self.assertIn('"reject-cancellation"', client)
        self.assertIn('"Idempotency-Key": idempotencyKey', client)
        self.assertIn("expected_planning_version: expectedPlanningVersion", client)
        self.assertIn("getPlanningSnapshot", page)
        self.assertIn("currentWorkflowState?.available_actions", page)
        self.assertIn("currentDemand.cancellation_request_id", page)
        self.assertIn('currentDemand.cancellation_state === "PENDING"', page)
        self.assertIn("Annulation demandée", page)
        self.assertIn("Traiter l’annulation", page)
        self.assertIn("Annuler la demande et libérer le planning", page)
        self.assertIn("canonicalDetail.materialized_plan", page)
        self.assertIn("acceptRetry.current?.fingerprint === fingerprint", page)
        self.assertIn("await refreshAfterMutation(result.demand_number)", page)
        self.assertIn('detail.demand.cancellation_state === "PENDING"', detail)
        self.assertIn('demand.cancellation_state === "PENDING"', demands)

    def test_cancellation_reason_resolution_and_dirty_guard_are_required(self) -> None:
        page = (ROOT / "frontend" / "src" / "DemandWorkflowPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Une raison est requise pour demander l’annulation.", page)
        self.assertIn("Un commentaire de résolution est requis.", page)
        self.assertIn("!cancellationReason.trim()", page)
        self.assertIn("!cancellationResolutionComment.trim()", page)
        self.assertGreaterEqual(page.count("if (hasUnsavedChanges)"), 3)
        self.assertIn("Enregistre les modifications avant de poursuivre.", page)


if __name__ == "__main__":
    unittest.main()

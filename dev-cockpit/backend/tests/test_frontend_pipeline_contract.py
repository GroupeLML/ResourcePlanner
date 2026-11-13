from pathlib import Path
import unittest


class FrontendPipelineContractTests(unittest.TestCase):
    def test_dashboard_shows_canonical_horizons_and_po_keeps_extended_pipeline(self):
        cockpit_root = Path(__file__).resolve().parents[2]
        app = (cockpit_root / "frontend" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        panel = (cockpit_root / "frontend" / "src" / "RoleDetailPanel.tsx").read_text(
            encoding="utf-8"
        )
        roles = (cockpit_root / "frontend" / "src" / "RoleCards.tsx").read_text(
            encoding="utf-8"
        )
        types = (cockpit_root / "frontend" / "src" / "types.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn("data.pipeline.now", app)
        self.assertIn("data.pipeline.parallel", app)
        self.assertIn("data.pipeline.valid", app)
        self.assertIn("data.pipeline_source.number", app)
        self.assertIn("COHÉRENCE ROADMAP", app)
        self.assertIn("Voir le diff", app)
        self.assertIn("SAFE WRITEBACK · PREVIEW OBLIGATOIRE", app)
        self.assertIn("Confirmer et appliquer à #", app)
        self.assertIn("pipelineIssue", app)
        self.assertIn("/api/roadmap-writeback/preview", app)
        self.assertIn("/api/roadmap-writeback/apply", app)
        self.assertIn("ROADMAP_CHANGED", app)
        self.assertIn("data.reconciliation", app)
        self.assertIn("ATTENTION CENTER", app)
        self.assertIn("Préparer la reprise", app)
        self.assertIn("data.attention", app)
        self.assertIn("CONTRÔLEUR D'EXÉCUTION", app)
        self.assertIn("TIMELINE GITHUB", app)
        self.assertIn("SANTÉ DU FLUX", app)
        self.assertIn("Flow Analytics · Delivery History", app)
        self.assertIn("/api/flow-analytics", app)
        self.assertIn("Charger l'analyse du flux", app)
        self.assertIn("CYCLES CI OBSERVABLES", app)
        self.assertIn("TIMELINE DE LIVRAISON", app)
        self.assertIn("Cette analyse est chargée séparément du polling du", app)
        self.assertNotIn("setInterval(() => void loadAnalytics", app)
        self.assertIn("data.execution", app)
        self.assertIn("Copier le prompt de reprise", app)
        self.assertIn("PARALLÈLE DISPONIBLE", app)
        self.assertIn("ENSUITE", app)

        self.assertIn("Trajectoire produit", panel)
        for label in ("Maintenant", "En parallèle", "Ensuite", "Plus tard"):
            self.assertIn(label, panel)
        self.assertIn("dashboard.pipeline.now", panel)
        self.assertIn("dashboard.pipeline.parallel", panel)
        self.assertIn("dashboard.pipeline.next", panel)
        self.assertIn("dashboard.pipeline.later", panel)
        self.assertIn("PipelineHorizonGroup", panel)
        self.assertIn("PipelineStepAccordion", panel)
        self.assertIn("PipelineUnavailable", panel)
        self.assertIn("MissionBlock", panel)
        self.assertIn("Mission courante", panel)
        self.assertIn("HANDOFF PACK", panel)
        self.assertIn("Préparer la reprise + ouvrir ChatGPT", panel)
        self.assertIn("handoff-confidence", panel)
        self.assertIn("/api/details/issues/", panel)
        self.assertIn("exactSection", panel)

        self.assertIn("'WORK'", types)
        self.assertIn("'ARCHITECTURE_GATE'", types)
        self.assertIn("'ENVIRONMENT_GATE'", types)
        self.assertIn("status: string | null", types)
        self.assertIn("rationale: string | null", types)
        self.assertIn("lane: 'MAIN' | 'PARALLEL'", types)
        self.assertIn("parallel: PipelineStep[]", types)
        self.assertIn("valid: boolean", types)
        self.assertIn("source: 'canonical_v1' | 'legacy'", types)
        self.assertIn("errors: string[]", types)
        self.assertIn("active_issue: number | null", types)
        self.assertIn("PipelineReconciliation", types)
        self.assertIn("reconciliation: PipelineReconciliation", types)
        self.assertIn("'coherent' | 'stale' | 'attention' | 'invalid' | 'legacy'", types)
        self.assertIn("pipeline_block: string", types)
        self.assertIn("RoadmapWritebackPreview", types)
        self.assertIn("RoadmapWritebackResult", types)
        self.assertIn("expected_body_sha256: string", types)
        self.assertIn("proposal_sha256: string", types)
        self.assertIn("canonical_pipeline_issue: number", types)
        self.assertIn("migration_candidate_pipeline_issue: number", types)
        self.assertIn("pipeline_source:", types)
        self.assertIn("pipeline_issue: number", types)
        self.assertIn("ExecutionControl", types)
        self.assertIn("ExecutionTimelineEvent", types)
        self.assertIn("RoleMission", types)
        self.assertIn("execution: ExecutionControl", types)
        self.assertIn("HandoffPack", types)
        self.assertIn("HandoffProjection", types)
        self.assertIn("handoff: HandoffProjection", types)
        self.assertIn("'COMPLETE' | 'PARTIAL' | 'BLOCKED'", types)
        self.assertIn("AttentionCenter", types)
        self.assertIn("AttentionItem", types)
        self.assertIn("attention: AttentionCenter", types)
        self.assertIn("'ACTION' | 'WATCH' | 'CLEAR'", types)
        self.assertIn("FlowAnalyticsReport", types)
        self.assertIn("FlowAnalyticsDelivery", types)
        self.assertIn("FlowAnalyticsAttempt", types)
        self.assertIn("'complete' | 'partial' | 'unavailable'", types)
        self.assertIn("pr_to_green_minutes", types)

        self.assertIn("dashboard?.execution.missions", roles)
        self.assertIn("dashboard?.handoff.packs", roles)
        self.assertIn("Préparer la reprise + ouvrir", roles)
        self.assertIn("handoffRole", roles)


if __name__ == "__main__":
    unittest.main()

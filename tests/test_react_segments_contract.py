from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactSegmentsContractTests(unittest.TestCase):
    def test_demands_workspace_exposes_segments_without_recreating_v1_grid(self) -> None:
        workspace = (ROOT / "frontend" / "src" / "DemandsWorkspace.tsx").read_text(
            encoding="utf-8"
        )
        page = (ROOT / "frontend" / "src" / "DemandSegmentsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn('import DemandSegmentsPage from "./DemandSegmentsPage"', workspace)
        self.assertIn('view === "segments"', workspace)
        self.assertIn("<DemandSegmentsPage />", workspace)
        self.assertIn("Segments / besoins ressources", page)
        self.assertIn("Segment = besoin ressource", (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(encoding="utf-8"))
        self.assertNotIn("xlsx", page.lower())
        self.assertNotIn("nicegui", page.lower())

    def test_shift_editor_opens_the_parent_resource_requirement(self) -> None:
        shift = (ROOT / "frontend" / "src" / "ShiftEditor.tsx").read_text(encoding="utf-8")

        self.assertIn('import SegmentEditor from "./SegmentEditor"', shift)
        self.assertIn("Modifier le segment parent", shift)
        self.assertIn("segmentId={shift.requirement_id || shift.segment_id}", shift)
        self.assertIn("setSegmentOpen(true)", shift)
        self.assertIn("onSaved={onSaved}", shift)
        self.assertIn("onOpenDemand={onOpenDemand}", shift)

    def test_segment_api_uses_existing_fastapi_contract(self) -> None:
        api = (ROOT / "frontend" / "src" / "segments-api.ts").read_text(encoding="utf-8")
        guarded_api = (ROOT / "frontend" / "src" / "manualOverallocationApi.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn('"/api/v1/segments"', api)
        self.assertIn("/api/v1/segments/${encodeURIComponent(segmentId)}", api)
        self.assertIn("/cancel", api)
        self.assertIn("/assign", api)
        self.assertIn('"Idempotency-Key"', api)
        self.assertIn('"PATCH"', api)
        self.assertIn("include_cancelled", api)
        self.assertIn("/api/v1/segments/${encodeURIComponent(segmentId)}", guarded_api)
        self.assertIn('"PATCH"', guarded_api)

    def test_editor_keeps_business_mutations_authoritative_and_patch_partial(self) -> None:
        editor = (ROOT / "frontend" / "src" / "SegmentEditor.tsx").read_text(encoding="utf-8")

        self.assertIn("await updateSegmentWithOverallocation(", editor)
        self.assertIn("segmentId,", editor)
        self.assertIn("editablePayload,", editor)
        self.assertIn("allowLockedOverallocation,", editor)
        self.assertIn("const createPayload: SegmentWrite", editor)
        self.assertIn("await createSegment(createPayload, key)", editor)
        self.assertIn("await assignSegment(savedSegmentId, selectedTargetResourceId)", editor)
        self.assertIn("await cancelSegment(segmentId)", editor)
        self.assertIn("source_effort_id: null", editor)
        update_block = editor.split("const editablePayload", 1)[1].split("const createPayload: SegmentWrite", 1)[0]
        self.assertNotIn("source_effort_id", update_block)
        self.assertIn("onSaved()", editor)
        self.assertNotIn("capacity_hours", editor)
        self.assertNotIn("utilization_pct", editor)
        self.assertNotIn("residual_hours", editor)

    def test_backend_segment_routes_remain_the_authoritative_surface(self) -> None:
        commands = (ROOT / "app" / "server" / "routes_commands.py").read_text(encoding="utf-8")
        reads = (ROOT / "app" / "server" / "routes_reads.py").read_text(encoding="utf-8")

        self.assertIn('@router.post("/segments", status_code=status.HTTP_201_CREATED)', commands)
        self.assertIn('@router.patch("/segments/{segment_id}")', commands)
        self.assertIn('@router.post("/segments/{segment_id}/cancel")', commands)
        self.assertIn('@router.post("/segments/{segment_id}/assign")', commands)
        self.assertIn('@router.get("/segments")', reads)
        self.assertIn('@router.get("/segments/{segment_id}")', reads)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactDirectAssetReservationContractTests(unittest.TestCase):
    def test_planning_exposes_direct_and_segment_reservation_contexts(self) -> None:
        form = (ROOT / "frontend/src/DirectAssetReservationForm.tsx").read_text(
            encoding="utf-8"
        )
        panel = (ROOT / "frontend/src/AssetPlanningPanel.tsx").read_text(
            encoding="utf-8"
        )
        api = (ROOT / "frontend/src/assetApi.ts").read_text(encoding="utf-8")

        self.assertIn("Réserver un actif", form)
        self.assertIn('"PROJECT_DIRECT"', form)
        self.assertIn('"RESOURCE_PERIOD"', form)
        self.assertIn('"SEGMENT"', form)
        self.assertIn("Une ressource pour une période", form)
        self.assertIn("Un segment", form)
        self.assertIn("<SearchableCombobox", form)
        self.assertIn("operator_resource_id: operatorId", form)
        self.assertIn("resource_id: resourceId!", form)
        self.assertNotIn("project_id: projectId,", form)
        self.assertNotIn("segmentId && operatorId", form)
        self.assertIn('aria-label="Projet dérivé du segment"', form)

        self.assertIn('"/api/v1/assets/project-reservations"', api)
        self.assertIn('"/api/v1/assets/resource-period-reservations"', api)
        self.assertIn('"/api/v1/assets/segment-reservations"', api)
        self.assertIn("createSegmentReservation", form)
        self.assertIn("updateSegmentReservation", panel)
        self.assertIn("releaseSegmentReservation", panel)
        self.assertIn("releaseProjectDirectReservation", panel)
        self.assertIn("releaseResourcePeriodReservation", panel)
        self.assertIn('requirement.origin === "PROJECT_DIRECT"', panel)
        self.assertIn('requirement.origin === "RESOURCE_PERIOD"', panel)
        self.assertIn('requirement.origin === "SEGMENT"', panel)
        self.assertIn("l’opérateur reste facultatif dans le contexte segment", panel)
        self.assertIn("La ressource de contexte reste obligatoirement", panel)
        self.assertIn("Ouvrir la demande source", panel)

        planning = (ROOT / "frontend/src/PlanningPage.tsx").read_text(encoding="utf-8")
        self.assertIn("onOpenDemand={canReadDemands ? setDetailDemandNumber : undefined}", planning)


if __name__ == "__main__":
    unittest.main()

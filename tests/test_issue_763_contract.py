from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue763ContractTests(unittest.TestCase):
    def test_react_renders_backend_subgroup_codes_without_erp_mapping(self) -> None:
        planning = (ROOT / "frontend" / "src" / "PlanningPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("planning_subgroup_code", planning)
        self.assertIn("planning_subgroup_label", planning)
        self.assertIn("INSTALL_ELEC::", planning)
        self.assertIn('className === "INSTALL_ELEC"', planning)
        self.assertIn("INSTALL_ELEC_SUBGROUP_ORDER", planning)

        for erp_code in ("ELCOMP", "ELAPP1", "ELAPP2", "ELAPP3", "ELAPP4"):
            self.assertNotIn(erp_code, planning)
        self.assertNotIn("INSTALL_ELEC_COMPAGNON", planning)
        self.assertNotIn("INSTALL_ELEC_APPRENTI", planning)

    def test_backend_owns_authoritative_employee_class_mapping(self) -> None:
        classifier = (
            ROOT / "app" / "domain" / "planning_resource_subgroups.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"ELCOMP"', classifier)
        for erp_code in ("ELAPP1", "ELAPP2", "ELAPP3", "ELAPP4"):
            self.assertIn(f'"{erp_code}"', classifier)
        self.assertIn('INSTALL_ELEC_CLASS = "INSTALL_ELEC"', classifier)
        self.assertIn('"Autres / Non classé"', classifier)
        self.assertNotIn("competenc", classifier.lower())


if __name__ == "__main__":
    unittest.main()

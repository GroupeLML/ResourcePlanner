from __future__ import annotations

from pathlib import Path
import unittest


class MediumTermCapacityArchitectureTests(unittest.TestCase):
    def test_projection_policy_is_ui_and_storage_neutral(self) -> None:
        app_dir = Path(__file__).resolve().parents[1] / "app"
        source = (app_dir / "domain" / "capacity_projection.py").read_text(encoding="utf-8")
        for forbidden in ("nicegui", "xlwings", "sqlalchemy", "ExcelRepository", "v18"):
            self.assertNotIn(forbidden, source)



if __name__ == "__main__":
    unittest.main()

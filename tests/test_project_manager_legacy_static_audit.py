from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectManagerLegacyStaticAuditTests(unittest.TestCase):
    def test_only_diagnostic_repository_reads_legacy_project_fk_in_application_code(self) -> None:
        occurrences: list[str] = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "Project.project_manager_contact_id" in source:
                occurrences.append(path.relative_to(ROOT).as_posix())

        self.assertEqual(
            occurrences,
            [
                "app/infrastructure/sql/"
                "project_manager_legacy_diagnostic_repository.py"
            ],
        )

    def test_canonical_manager_resolver_has_no_forbidden_identity_join_keys(self) -> None:
        source = (
            ROOT
            / "app"
            / "infrastructure"
            / "sql"
            / "project_manager_resolution_repository.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "Project.project_manager_contact_id",
            "AppUser.email",
            "AppUser.erp_user_id",
            "Resource.external_id",
        ):
            self.assertNotIn(forbidden, source)

        self.assertIn("Project.project_manager_external_id", source)
        self.assertIn("AppUser.employee_external_id", source)
        self.assertIn("AppUser.business_contact_id", source)

    def test_legacy_diagnostic_repository_contains_no_mutation_statement(self) -> None:
        source = (
            ROOT
            / "app"
            / "infrastructure"
            / "sql"
            / "project_manager_legacy_diagnostic_repository.py"
        ).read_text(encoding="utf-8").lower()

        self.assertNotIn("update(", source)
        self.assertNotIn("delete(", source)
        self.assertNotIn("insert(", source)


if __name__ == "__main__":
    unittest.main()

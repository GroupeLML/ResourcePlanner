from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReactIssue653ContractTests(unittest.TestCase):
    def test_project_open_uses_stable_detail_ref_scroll_and_focus(self) -> None:
        page = (ROOT / "frontend" / "src" / "ProjectsPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("const projectDetailRef = useRef<HTMLElement>(null);", page)
        self.assertIn("openProjectDetail(project.number)", page)
        self.assertIn(
            'detail.scrollIntoView({ behavior: "smooth", block: "start" });',
            page,
        )
        self.assertIn("detail.focus({ preventScroll: true });", page)
        self.assertIn(
            'className="admin-card project-contact-admin" ref={projectDetailRef} tabIndex={-1}',
            page,
        )

    def test_contact_select_reuses_searchable_combobox_and_preserves_history(self) -> None:
        common = (ROOT / "frontend" / "src" / "BusinessContactUi.tsx").read_text(
            encoding="utf-8"
        )
        resources = (ROOT / "frontend" / "src" / "ResourcesPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'import SearchableCombobox, { type ComboboxOption } from "./SearchableCombobox"',
            common,
        )
        self.assertIn(".filter((contact) => contact.active)", common)
        self.assertIn(
            "searchText: [contact.display_name, contact.email, contact.phone]",
            common,
        )
        self.assertIn('label: selectedContact', common)
        self.assertIn("disabled: true", common)
        self.assertIn("value={value ?? inheritValue}", common)
        self.assertIn("nextValue === inheritValue ? null : nextValue", common)
        self.assertIn(
            'inheritLabel="Aucun coordonnateur de ressource · utiliser la tâche"',
            resources,
        )
        self.assertIn('label="Coordonnateur"', resources)

    def test_new_standard_schedule_defaults_to_0700_1500_only(self) -> None:
        page = (ROOT / "frontend" / "src" / "ResourcesPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'useState(shortTime(rule?.start_time ?? null) || "07:00")',
            page,
        )
        self.assertIn(
            'useState(shortTime(rule?.end_time ?? null) || "15:00")',
            page,
        )
        self.assertNotIn(
            'useState(shortTime(rule?.end_time ?? null) || "15:30")',
            page,
        )
        self.assertIn("rule?.end_time", page)


if __name__ == "__main__":
    unittest.main()

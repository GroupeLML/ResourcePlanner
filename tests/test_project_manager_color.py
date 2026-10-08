from __future__ import annotations

import unittest

from app.application.project_manager_color import project_manager_color_marker
from app.application.project_managers import (
    EffectiveProjectManager,
    EffectiveProjectManagers,
)


def _manager(
    name: str,
    *,
    employee: str | None = None,
    contact: str | None = None,
    user: str | None = None,
) -> EffectiveProjectManager:
    return EffectiveProjectManager(
        sources=("ERP",) if employee else ("RP",),
        employee_external_id=employee,
        erp_display_name=name if employee else None,
        app_user_id=user,
        business_contact_id=contact,
        display_name=name,
        user_active=True,
        contact_active=True,
        resolution_status="RESOLVED",
        diagnostics=(),
    )


def _effective(
    *,
    primary: EffectiveProjectManager | None = None,
    co_managers: tuple[EffectiveProjectManager, ...] = (),
) -> EffectiveProjectManagers:
    return EffectiveProjectManagers(
        project_id="P1",
        co_managers_version=1,
        primary=primary,
        co_managers=co_managers,
        diagnostics=(),
    )


class ProjectManagerColorTests(unittest.TestCase):
    def test_principal_wins_and_name_change_does_not_change_color(self) -> None:
        co_manager = _manager("Co", contact="C-CO")
        first = project_manager_color_marker(
            _effective(primary=_manager("Ancien", employee="E-1"), co_managers=(co_manager,))
        )
        renamed = project_manager_color_marker(
            _effective(primary=_manager("Nouveau", employee="E-1"), co_managers=(co_manager,))
        )
        self.assertEqual(first[0], renamed[0])
        self.assertNotEqual(first[0], project_manager_color_marker(_effective(primary=co_manager))[0])
        self.assertEqual(renamed[1], "Nouveau")

    def test_primary_and_co_manager_share_token_for_same_employee(self) -> None:
        primary = _manager("ERP", employee="E-1")
        co_manager = _manager("RP", employee="E-1", contact="C-1")
        self.assertEqual(
            project_manager_color_marker(_effective(primary=primary))[0],
            project_manager_color_marker(_effective(co_managers=(co_manager,)))[0],
        )

    def test_co_manager_choice_is_stable_under_reordering_and_rename(self) -> None:
        a = _manager("Alpha", contact="C-A")
        b = _manager("Beta", contact="C-B")
        first = project_manager_color_marker(_effective(co_managers=(b, a)))
        second = project_manager_color_marker(
            _effective(co_managers=(_manager("Nouveau", contact="C-A"), b))
        )
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], "Alpha")
        self.assertEqual(second[1], "Nouveau")

    def test_absent_manager_returns_neutral_marker(self) -> None:
        self.assertEqual(project_manager_color_marker(None), (None, None))
        self.assertEqual(project_manager_color_marker(_effective()), (None, None))


if __name__ == "__main__":
    unittest.main()

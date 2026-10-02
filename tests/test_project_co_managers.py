from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import func, select

from app.application.errors import (
    ApplicationConflictError,
    ApplicationValidationError,
)
from app.application.project_co_managers import (
    PROJECT_CO_MANAGER_ADDED,
    PROJECT_CO_MANAGER_REMOVED,
)
from app.infrastructure.sql import (
    AppUser,
    Base,
    BusinessContact,
    CommandIdempotencyReceipt,
    Project,
    ProjectCoManager,
    ProjectManagerAudit,
    SqlProjectCoManagerRepository,
    create_session_factory,
    create_sql_engine,
)


class ProjectCoManagerRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        path = Path(self._directory.name) / "project-co-managers.db"
        self.engine = create_sql_engine(f"sqlite:///{path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            session.add_all(
                [
                    AppUser(
                        id="U-ACTOR",
                        display_name="Admin",
                        email=None,
                        roles_json='["ADMIN"]',
                        active=True,
                    ),
                    BusinessContact(
                        id="C-1",
                        display_name="Contact 1",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-2",
                        display_name="Contact 2",
                        active=True,
                    ),
                    BusinessContact(
                        id="C-HIST",
                        display_name="Contact historique",
                        active=True,
                    ),
                    Project(
                        id="P-1",
                        number="P-573-A",
                        name="Projet A",
                        project_manager_external_id="ERP-PM-A",
                        project_manager_name="Principal ERP A",
                        project_manager_contact_id="C-HIST",
                    ),
                    Project(
                        id="P-2",
                        number="P-573-B",
                        name="Projet B",
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self._directory.cleanup()

    def _version(self, project_id: str = "P-1") -> int:
        with self.factory() as session:
            value = session.scalar(
                select(Project.co_managers_version).where(Project.id == project_id)
            )
            assert value is not None
            return int(value)

    def _count(self, model) -> int:
        with self.factory() as session:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

    def test_multiple_co_managers_and_same_contact_on_multiple_projects(self) -> None:
        with self.factory.begin() as session:
            repo = SqlProjectCoManagerRepository(session)
            first = repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
            )
            second = repo.add_co_manager(
                "P-1",
                "C-2",
                actor_user_id="U-ACTOR",
                expected_version=2,
            )
            third = repo.add_co_manager(
                "P-2",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
            )
            self.assertEqual(first.version, 2)
            self.assertEqual(second.version, 3)
            self.assertEqual(third.version, 2)
            self.assertEqual(
                [row.business_contact_id for row in repo.list_co_managers("P-1")],
                ["C-1", "C-2"],
            )

        self.assertEqual(self._count(ProjectCoManager), 3)
        self.assertEqual(self._count(ProjectManagerAudit), 3)

    def test_duplicate_is_rejected_without_consuming_version(self) -> None:
        with self.factory.begin() as session:
            repo = SqlProjectCoManagerRepository(session)
            repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
            )
            with self.assertRaises(ApplicationConflictError) as raised:
                repo.add_co_manager(
                    "P-1",
                    "C-1",
                    actor_user_id="U-ACTOR",
                    expected_version=2,
                )
            self.assertEqual(raised.exception.code, "project_co_manager_exists")

        self.assertEqual(self._version(), 2)
        self.assertEqual(self._count(ProjectCoManager), 1)
        self.assertEqual(self._count(ProjectManagerAudit), 1)

    def test_stale_version_rejects_without_mutation(self) -> None:
        with self.factory.begin() as session:
            with self.assertRaises(ApplicationConflictError) as raised:
                SqlProjectCoManagerRepository(session).add_co_manager(
                    "P-1",
                    "C-1",
                    actor_user_id="U-ACTOR",
                    expected_version=2,
                )
            self.assertEqual(
                raised.exception.code,
                "project_co_managers_version_conflict",
            )

        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(ProjectCoManager), 0)
        self.assertEqual(self._count(ProjectManagerAudit), 0)

    def test_error_after_cas_rolls_back_version_association_and_audit(self) -> None:
        with self.factory.begin() as session:
            contact = session.get(BusinessContact, "C-1")
            assert contact is not None
            contact.active = False

        with self.factory.begin() as session:
            with self.assertRaises(ApplicationValidationError) as raised:
                SqlProjectCoManagerRepository(session).add_co_manager(
                    "P-1",
                    "C-1",
                    actor_user_id="U-ACTOR",
                    expected_version=1,
                )
            self.assertEqual(
                raised.exception.code,
                "project_co_manager_contact_inactive",
            )

        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(ProjectCoManager), 0)
        self.assertEqual(self._count(ProjectManagerAudit), 0)

    def test_remove_inactive_contact_is_allowed_and_audit_survives(self) -> None:
        with self.factory.begin() as session:
            repo = SqlProjectCoManagerRepository(session)
            added = repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
            )
            self.assertEqual(added.action, PROJECT_CO_MANAGER_ADDED)

        with self.factory.begin() as session:
            contact = session.get(BusinessContact, "C-1")
            assert contact is not None
            contact.active = False

        with self.factory.begin() as session:
            removed = SqlProjectCoManagerRepository(session).remove_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=2,
            )
            self.assertEqual(removed.action, PROJECT_CO_MANAGER_REMOVED)
            self.assertEqual(removed.version, 3)

        self.assertEqual(self._count(ProjectCoManager), 0)
        self.assertEqual(self._count(ProjectManagerAudit), 2)
        with self.factory() as session:
            audits = list(
                session.scalars(
                    select(ProjectManagerAudit)
                    .where(ProjectManagerAudit.project_id == "P-1")
                    .order_by(ProjectManagerAudit.resulting_version)
                ).all()
            )
            self.assertEqual(
                [row.action for row in audits],
                [PROJECT_CO_MANAGER_ADDED, PROJECT_CO_MANAGER_REMOVED],
            )
            self.assertEqual([row.actor_user_id for row in audits], ["U-ACTOR", "U-ACTOR"])
            self.assertEqual([row.source for row in audits], ["RP", "RP"])
            self.assertEqual([row.resulting_version for row in audits], [2, 3])
            self.assertEqual(json.loads(audits[0].before_json)["assigned"], False)
            self.assertEqual(json.loads(audits[0].after_json)["assigned"], True)
            self.assertEqual(json.loads(audits[1].before_json)["assigned"], True)
            self.assertEqual(json.loads(audits[1].after_json)["assigned"], False)

    def test_primary_erp_contact_cannot_be_persisted_as_co_manager(self) -> None:
        with self.factory.begin() as session:
            session.add(
                AppUser(
                    id="U-PRIMARY",
                    display_name="Principal",
                    email=None,
                    employee_external_id="ERP-PM-A",
                    business_contact_id="C-1",
                    roles_json='["PROJECT_MANAGER"]',
                    active=True,
                )
            )

        with self.factory.begin() as session:
            with self.assertRaises(ApplicationValidationError) as raised:
                SqlProjectCoManagerRepository(session).add_co_manager(
                    "P-1",
                    "C-1",
                    actor_user_id="U-ACTOR",
                    expected_version=1,
                )
            self.assertEqual(
                raised.exception.code,
                "project_co_manager_primary_forbidden",
            )

        self.assertEqual(self._version(), 1)
        self.assertEqual(self._count(ProjectCoManager), 0)

    def test_principal_fields_are_unchanged_by_co_manager_mutations(self) -> None:
        with self.factory.begin() as session:
            repo = SqlProjectCoManagerRepository(session)
            repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
            )
            repo.remove_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=2,
            )

        with self.factory() as session:
            project = session.get(Project, "P-1")
            assert project is not None
            self.assertEqual(project.project_manager_external_id, "ERP-PM-A")
            self.assertEqual(project.project_manager_name, "Principal ERP A")
            self.assertEqual(project.project_manager_contact_id, "C-HIST")

    def test_idempotent_replay_does_not_repeat_cas_or_audit(self) -> None:
        with self.factory.begin() as session:
            repo = SqlProjectCoManagerRepository(session)
            first = repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
                idempotency_key="add-c1",
            )
            replay = repo.add_co_manager(
                "P-1",
                "C-1",
                actor_user_id="U-ACTOR",
                expected_version=1,
                idempotency_key="add-c1",
            )
            self.assertEqual(replay, first)

            with self.assertRaises(ApplicationConflictError) as raised:
                repo.add_co_manager(
                    "P-1",
                    "C-2",
                    actor_user_id="U-ACTOR",
                    expected_version=1,
                    idempotency_key="add-c1",
                )
            self.assertEqual(raised.exception.code, "idempotency_key_conflict")

        self.assertEqual(self._version(), 2)
        self.assertEqual(self._count(ProjectCoManager), 1)
        self.assertEqual(self._count(ProjectManagerAudit), 1)
        self.assertEqual(self._count(CommandIdempotencyReceipt), 1)


if __name__ == "__main__":
    unittest.main()

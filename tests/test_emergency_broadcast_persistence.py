from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from sqlalchemy import func, select

from app.application.emergency_broadcasts import EmergencyBroadcastService
from app.application.errors import ApplicationConflictError, ApplicationValidationError
from app.domain.emergency_broadcast import (
    CreateEmergencyBroadcastCommand,
    EndEmergencyBroadcastCommand,
)
from app.infrastructure.sql import (
    Base,
    AppUser,
    Project,
    Shift,
    WorkforceRequest,
    create_session_factory,
    create_sql_engine,
)
from app.infrastructure.sql.emergency_broadcast_models import (
    EmergencyBroadcast,
    EmergencyBroadcastAcknowledgment,
)
from app.infrastructure.sql.emergency_broadcast_repository import (
    SqlEmergencyBroadcastRepository,
)


class EmergencyBroadcastPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_sql_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = create_session_factory(self.engine)
        with self.factory.begin() as session:
            self.admin = AppUser(
                display_name="Admin",
                roles_json='["ADMIN"]',
                active=True,
            )
            self.mary = AppUser(
                display_name="Mary",
                roles_json='["EMPLOYEE"]',
                active=True,
            )
            self.john = AppUser(
                display_name="John",
                roles_json='["EMPLOYEE"]',
                active=True,
            )
            session.add_all((self.admin, self.mary, self.john))
        self.repository = SqlEmergencyBroadcastRepository(self.factory)
        self.service = EmergencyBroadcastService(self.repository)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _create(
        self,
        *,
        actor_user_id: str | None = None,
        command_id: str | None = None,
        title: str = "AVIS IMPORTANT",
        message: str = "Sauvegardez votre travail.",
    ):
        return self.service.create(
            CreateEmergencyBroadcastCommand(
                actor_user_id=actor_user_id or self.admin.id,
                creation_command_id=command_id or str(uuid4()),
                title=title,
                message=message,
            )
        )

    def test_create_enforces_single_active_and_idempotent_creation(self) -> None:
        command_id = str(uuid4())
        first = self._create(command_id=command_id)

        replay = self._create(command_id=command_id)
        self.assertEqual(replay.id, first.id)

        with self.assertRaises(ApplicationConflictError) as changed:
            self._create(command_id=command_id, message="Autre contenu")
        self.assertEqual(
            changed.exception.code,
            "emergency_broadcast_idempotency_conflict",
        )

        with self.assertRaises(ApplicationConflictError) as second:
            self._create(command_id=str(uuid4()))
        self.assertEqual(
            second.exception.code,
            "emergency_broadcast_already_active",
        )

        with self.factory() as session:
            active_count = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcast)
                .where(EmergencyBroadcast.state == "ACTIVE")
            )
        self.assertEqual(active_count, 1)

    def test_end_is_idempotent_and_preserves_first_actor_and_time(self) -> None:
        created = self._create()

        first_end = self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.admin.id,
                broadcast_id=created.id,
            )
        )
        second_end = self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.john.id,
                broadcast_id=created.id,
            )
        )

        self.assertEqual(first_end.state, "ENDED")
        self.assertEqual(first_end.ended_by_user_id, self.admin.id)
        self.assertEqual(second_end.ended_by_user_id, self.admin.id)
        self.assertEqual(second_end.ended_at, first_end.ended_at)

    def test_acknowledgment_is_idempotent_and_keeps_first_timestamp(self) -> None:
        created = self._create()

        first = self.service.acknowledge(
            broadcast_id=created.id,
            authenticated_user_id=self.mary.id,
        )
        duplicate = self.service.acknowledge(
            broadcast_id=created.id,
            authenticated_user_id=self.mary.id,
        )

        self.assertEqual(duplicate.acknowledged_at, first.acknowledged_at)
        with self.factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == created.id,
                    EmergencyBroadcastAcknowledgment.user_id == self.mary.id,
                )
            )
        self.assertEqual(count, 1)

    def test_acknowledgment_after_end_is_allowed_and_old_ack_does_not_affect_new(self) -> None:
        old = self._create()
        self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.admin.id,
                broadcast_id=old.id,
            )
        )
        late = self.service.acknowledge(
            broadcast_id=old.id,
            authenticated_user_id=self.mary.id,
        )
        self.assertEqual(late.broadcast_id, old.id)

        new = self._create()
        self.assertIsNone(
            self.service.acknowledgment(
                broadcast_id=new.id,
                authenticated_user_id=self.mary.id,
            )
        )

    def test_retention_selection_and_delete_never_remove_active_broadcast(self) -> None:
        ended = self._create()
        self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.admin.id,
                broadcast_id=ended.id,
            )
        )
        with self.factory.begin() as session:
            row = session.get(EmergencyBroadcast, ended.id)
            row.ended_at = datetime.now(timezone.utc) - timedelta(days=91)

        active = self._create()
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        eligible = self.service.expired_ended_ids(ended_before=cutoff)

        self.assertEqual(eligible, (ended.id,))
        deleted = self.repository.delete_empty_expired_broadcasts(
            ended_before=cutoff,
        )
        self.assertEqual(deleted, 1)
        self.assertIsNotNone(self.service.get(active.id))
        self.assertEqual(self.service.get(active.id).state, "ACTIVE")

        self.service.acknowledge(
            broadcast_id=active.id,
            authenticated_user_id=self.mary.id,
        )
        deleted_receipts = self.repository.delete_acknowledgments_for_expired(
            ended_before=cutoff,
        )
        self.assertEqual(deleted_receipts, 0)
        self.assertIsNotNone(
            self.service.acknowledgment(
                broadcast_id=active.id,
                authenticated_user_id=self.mary.id,
            )
        )

    def test_feature_operations_do_not_mutate_planning_business_tables(self) -> None:
        with self.factory() as session:
            before = (
                session.scalar(select(func.count()).select_from(Project)),
                session.scalar(select(func.count()).select_from(Shift)),
                session.scalar(select(func.count()).select_from(WorkforceRequest)),
            )

        created = self._create()
        self.service.acknowledge(
            broadcast_id=created.id,
            authenticated_user_id=self.mary.id,
        )
        self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.admin.id,
                broadcast_id=created.id,
            )
        )

        with self.factory() as session:
            after = (
                session.scalar(select(func.count()).select_from(Project)),
                session.scalar(select(func.count()).select_from(Shift)),
                session.scalar(select(func.count()).select_from(WorkforceRequest)),
            )
        self.assertEqual(after, before)

    def test_text_validation_uses_utf16_units_and_rejects_blank_content(self) -> None:
        with self.assertRaises(ApplicationValidationError):
            self._create(title="   ")

        with self.assertRaises(ApplicationValidationError):
            self._create(title="😀" * 81)


if __name__ == "__main__":
    unittest.main()

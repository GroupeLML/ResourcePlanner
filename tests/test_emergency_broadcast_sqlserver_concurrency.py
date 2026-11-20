from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4

from sqlalchemy import delete, func, select, text

from app.application.emergency_broadcasts import EmergencyBroadcastService
from app.application.errors import ApplicationConflictError
from app.domain.emergency_broadcast import CreateEmergencyBroadcastCommand
from app.infrastructure.sql import AppUser, create_session_factory, create_sql_engine
from app.infrastructure.sql.emergency_broadcast_models import (
    EmergencyBroadcast,
    EmergencyBroadcastAcknowledgment,
)
from app.infrastructure.sql.emergency_broadcast_repository import (
    SqlEmergencyBroadcastRepository,
)


DATABASE_URL = str(os.getenv("RESOURCEPLANNER_SQLSERVER_TEST_URL") or "").strip()
IS_MSSQL = DATABASE_URL.lower().startswith("mssql")


@unittest.skipUnless(IS_MSSQL, "requires RESOURCEPLANNER_SQLSERVER_TEST_URL")
class EmergencyBroadcastSqlServerConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_sql_engine(DATABASE_URL)
        cls.factory = create_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        self.user_ids = [str(uuid4()) for _ in range(12)]
        with self.factory.begin() as session:
            for index, user_id in enumerate(self.user_ids):
                session.add(
                    AppUser(
                        id=user_id,
                        display_name=f"SQL894 User {index}",
                        roles_json='["ADMIN"]' if index < 2 else '["EMPLOYEE"]',
                        active=True,
                    )
                )
        self.repository = SqlEmergencyBroadcastRepository(self.factory)
        self.service = EmergencyBroadcastService(self.repository)

    def tearDown(self) -> None:
        with self.factory.begin() as session:
            session.execute(
                delete(EmergencyBroadcastAcknowledgment).where(
                    EmergencyBroadcastAcknowledgment.user_id.in_(self.user_ids)
                )
            )
            session.execute(
                delete(EmergencyBroadcast).where(
                    EmergencyBroadcast.created_by_user_id.in_(self.user_ids)
                )
            )
            session.execute(delete(AppUser).where(AppUser.id.in_(self.user_ids)))

    def _create(self, actor_id: str, suffix: str):
        return EmergencyBroadcastService(
            SqlEmergencyBroadcastRepository(self.factory)
        ).create(
            CreateEmergencyBroadcastCommand(
                actor_user_id=actor_id,
                creation_command_id=str(uuid4()),
                title=f"SQL894 {suffix}",
                message="Concurrent emergency broadcast proof.",
            )
        )

    def test_runtime_connection_has_filtered_index_set_options(self) -> None:
        with self.engine.connect() as connection:
            values = connection.execute(
                text(
                    "SELECT "
                    "SESSIONPROPERTY('ANSI_NULLS'), "
                    "SESSIONPROPERTY('ANSI_PADDING'), "
                    "SESSIONPROPERTY('ANSI_WARNINGS'), "
                    "SESSIONPROPERTY('ARITHABORT'), "
                    "SESSIONPROPERTY('CONCAT_NULL_YIELDS_NULL'), "
                    "SESSIONPROPERTY('QUOTED_IDENTIFIER'), "
                    "SESSIONPROPERTY('NUMERIC_ROUNDABORT')"
                )
            ).one()
        self.assertEqual(tuple(int(value) for value in values), (1, 1, 1, 1, 1, 1, 0))

    def test_same_user_concurrent_acknowledgment_has_one_canonical_receipt(self) -> None:
        broadcast = self._create(self.user_ids[0], "same-user")
        user_id = self.user_ids[2]

        def acknowledge():
            return EmergencyBroadcastService(
                SqlEmergencyBroadcastRepository(self.factory)
            ).acknowledge(
                broadcast_id=broadcast.id,
                authenticated_user_id=user_id,
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = tuple(pool.map(lambda _index: acknowledge(), range(3)))

        self.assertEqual(len({row.acknowledged_at for row in rows}), 1)
        with self.factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == broadcast.id,
                    EmergencyBroadcastAcknowledgment.user_id == user_id,
                )
            )
        self.assertEqual(count, 1)

    def test_distinct_users_acknowledge_without_shared_counter(self) -> None:
        broadcast = self._create(self.user_ids[0], "distinct-users")
        users = tuple(self.user_ids[2:10])

        def acknowledge(user_id: str):
            return EmergencyBroadcastService(
                SqlEmergencyBroadcastRepository(self.factory)
            ).acknowledge(
                broadcast_id=broadcast.id,
                authenticated_user_id=user_id,
            )

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            rows = tuple(pool.map(acknowledge, users))

        self.assertEqual({row.user_id for row in rows}, set(users))
        with self.factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(EmergencyBroadcastAcknowledgment.broadcast_id == broadcast.id)
            )
        self.assertEqual(count, len(users))

    def test_concurrent_active_creation_has_one_winner_and_one_conflict(self) -> None:
        def create(index: int) -> str:
            try:
                self._create(self.user_ids[index], f"create-{index}")
                return "winner"
            except ApplicationConflictError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(create, (0, 1)))

        self.assertEqual(outcomes.count("winner"), 1)
        self.assertEqual(outcomes.count("emergency_broadcast_already_active"), 1)
        with self.factory() as session:
            active_count = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcast)
                .where(EmergencyBroadcast.state == "ACTIVE")
            )
        self.assertEqual(active_count, 1)


if __name__ == "__main__":
    unittest.main()

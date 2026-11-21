from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.application.emergency_broadcasts import EmergencyBroadcastService
from app.application.errors import ApplicationConflictError
from app.application.security import AuthPrincipal, ROLE_TECHNICIAN
from app.domain.emergency_broadcast import (
    CreateEmergencyBroadcastCommand,
    EndEmergencyBroadcastCommand,
)
from app.infrastructure.sql import AppUser, create_session_factory, create_sql_engine
from app.infrastructure.sql.emergency_broadcast_models import (
    EmergencyBroadcast,
    EmergencyBroadcastAcknowledgment,
)
from app.infrastructure.sql.emergency_broadcast_repository import (
    SqlEmergencyBroadcastRepository,
)
from app.server.http import create_api_app
from app.server.security import static_auth_resolver


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
                    "CAST(SESSIONPROPERTY('ANSI_NULLS') AS int), "
                    "CAST(SESSIONPROPERTY('ANSI_PADDING') AS int), "
                    "CAST(SESSIONPROPERTY('ANSI_WARNINGS') AS int), "
                    "CAST(SESSIONPROPERTY('ARITHABORT') AS int), "
                    "CAST(SESSIONPROPERTY('CONCAT_NULL_YIELDS_NULL') AS int), "
                    "CAST(SESSIONPROPERTY('QUOTED_IDENTIFIER') AS int), "
                    "CAST(SESSIONPROPERTY('NUMERIC_ROUNDABORT') AS int)"
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

    def test_same_user_concurrent_http_acknowledgment_returns_three_200s(self) -> None:
        broadcast = self._create(self.user_ids[0], "same-user-http")
        user_id = self.user_ids[2]

        def make_client() -> TestClient:
            principal = AuthPrincipal.from_roles(
                local_user_id=user_id,
                issuer="urn:sql894",
                subject=user_id,
                display_name="SQL894 User",
                email=None,
                roles=(ROLE_TECHNICIAN,),
                auth_mode="local",
            )
            app = create_api_app(
                DATABASE_URL,
                auth_resolver=static_auth_resolver(principal),
                api_docs_enabled=False,
            )
            return TestClient(app)

        def acknowledge(_index: int):
            with make_client() as client:
                response = client.post(
                    f"/api/v1/runtime/broadcasts/{broadcast.id}/acknowledgment",
                    json={},
                )
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = tuple(pool.map(acknowledge, range(3)))

        self.assertEqual([item[0] for item in responses], [200, 200, 200])
        timestamps = {
            item[1]["acknowledgment"]["acknowledged_at"]
            for item in responses
        }
        self.assertEqual(len(timestamps), 1)
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

    def test_retention_ack_delete_is_bounded_and_sqlserver_compatible(self) -> None:
        expired = self._create(self.user_ids[0], "retention-expired")
        self.service.acknowledge(
            broadcast_id=expired.id,
            authenticated_user_id=self.user_ids[2],
        )
        self.service.acknowledge(
            broadcast_id=expired.id,
            authenticated_user_id=self.user_ids[3],
        )
        self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.user_ids[0],
                broadcast_id=expired.id,
            )
        )
        historical_activated_at = datetime.now(timezone.utc) - timedelta(days=92)
        historical_ended_at = datetime.now(timezone.utc) - timedelta(days=91)
        with self.factory.begin() as session:
            row = session.get(EmergencyBroadcast, expired.id)
            row.created_at = historical_activated_at
            row.activated_at = historical_activated_at
            row.ended_at = historical_ended_at

        recent = self._create(self.user_ids[0], "retention-recent")
        self.service.acknowledge(
            broadcast_id=recent.id,
            authenticated_user_id=self.user_ids[4],
        )
        self.service.end(
            EndEmergencyBroadcastCommand(
                actor_user_id=self.user_ids[0],
                broadcast_id=recent.id,
            )
        )

        active = self._create(self.user_ids[0], "retention-active")
        self.service.acknowledge(
            broadcast_id=active.id,
            authenticated_user_id=self.user_ids[5],
        )

        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        first_deleted = self.repository.delete_acknowledgments_for_expired(
            ended_before=cutoff,
            limit=1,
        )
        self.assertEqual(first_deleted, 1)

        with self.factory() as session:
            expired_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == expired.id
                )
            )
            recent_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == recent.id
                )
            )
            active_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == active.id
                )
            )
        self.assertEqual(expired_remaining, 1)
        self.assertEqual(recent_remaining, 1)
        self.assertEqual(active_remaining, 1)

        second_deleted = self.repository.delete_acknowledgments_for_expired(
            ended_before=cutoff,
            limit=1000,
        )
        self.assertEqual(second_deleted, 1)

        with self.factory() as session:
            expired_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == expired.id
                )
            )
            recent_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == recent.id
                )
            )
            active_remaining = session.scalar(
                select(func.count())
                .select_from(EmergencyBroadcastAcknowledgment)
                .where(
                    EmergencyBroadcastAcknowledgment.broadcast_id == active.id
                )
            )
        self.assertEqual(expired_remaining, 0)
        self.assertEqual(recent_remaining, 1)
        self.assertEqual(active_remaining, 1)

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

from __future__ import annotations

import json
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.application.security import ROLE_ADMIN, ROLE_COORDINATOR
from app.infrastructure.sql.approval_scope_models import (
    ApprovalScope,
    ApprovalScopeApprover,
    AssetTypeApprovalScopeMapping,
    TaskApprovalScopeMapping,
    ResourceClassApprovalScopeMapping,
)
from app.infrastructure.sql.identity_models import AppUser
from app.infrastructure.sql.models import Project, TaskCatalogEntry
from app.infrastructure.sql.resource_class_models import ResourceClassConfig


TEST_ADMIN_USER_ID = "TEST-ADMIN-APP-USER"
TEST_COORDINATOR_USER_ID = "TEST-COORD-APP-USER"
TEST_APPROVAL_SCOPE_ID = "TEST-APPROVAL-SCOPE"
TEST_APPROVAL_TASK_CODE = "APPROVAL"


def _ensure_user(
    session: Session,
    *,
    user_id: str,
    subject: str,
    display_name: str,
    role: str,
) -> None:
    if session.get(AppUser, user_id) is not None:
        return
    session.add(
        AppUser(
            id=user_id,
            issuer="urn:resourceplanner:test",
            subject=subject,
            display_name=display_name,
            email=None,
            employee_external_id=None,
            roles_json=json.dumps([role]),
            active=True,
        )
    )
    session.flush()


def seed_test_approval_routing(
    session: Session,
    *,
    task_ids: Iterable[str] = (),
    include_default_task: bool = True,
    map_existing_tasks: bool = False,
    admin_display_name: str = "Administrateur de test explicite",
    coordinator_display_name: str = "Coordonnateur de test explicite",
) -> None:
    """Seed explicit 276 routing primitives for HTTP integration fixtures.

    This helper deliberately lives in tests. Production routing remains fail-closed:
    no task mapping or no admissible AppUser still blocks cycle initialization.
    """

    _ensure_user(
        session,
        user_id=TEST_ADMIN_USER_ID,
        subject="explicit-test-admin",
        display_name=admin_display_name,
        role=ROLE_ADMIN,
    )
    _ensure_user(
        session,
        user_id=TEST_COORDINATOR_USER_ID,
        subject="explicit-test-coordinator",
        display_name=coordinator_display_name,
        role=ROLE_COORDINATOR,
    )

    if session.get(ApprovalScope, TEST_APPROVAL_SCOPE_ID) is None:
        session.add(
            ApprovalScope(
                id=TEST_APPROVAL_SCOPE_ID,
                code="TEST_APPROVAL",
                label="Approbation tests",
                active=True,
                version=1,
            )
        )
        session.flush()
    for user_id in (TEST_ADMIN_USER_ID, TEST_COORDINATOR_USER_ID):
        if (
            session.get(
                ApprovalScopeApprover,
                (TEST_APPROVAL_SCOPE_ID, user_id),
            )
            is None
        ):
            session.add(
                ApprovalScopeApprover(
                    approval_scope_id=TEST_APPROVAL_SCOPE_ID,
                    app_user_id=user_id,
                )
            )

    mapped_ids = {str(value).strip() for value in task_ids if str(value).strip()}
    if map_existing_tasks:
        mapped_ids.update(
            row.id for row in session.query(TaskCatalogEntry).all()
        )
    if include_default_task:
        projects = session.query(Project).all()
        for index, project in enumerate(projects, start=1):
            task = (
                session.query(TaskCatalogEntry)
                .filter(
                    TaskCatalogEntry.project_number == project.number,
                    TaskCatalogEntry.task_code == TEST_APPROVAL_TASK_CODE,
                )
                .one_or_none()
            )
            if task is None:
                task = TaskCatalogEntry(
                    id=f"TEST-APPROVAL-TASK-{index}",
                    project_number=project.number,
                    task_code=TEST_APPROVAL_TASK_CODE,
                    label="Approbation test",
                    status="Actif",
                    active=True,
                    time_entry_enabled=True,
                    expenses_enabled=False,
                )
                session.add(task)
                session.flush()
            mapped_ids.add(task.id)

    for task_id in sorted(mapped_ids):
        if session.get(TaskCatalogEntry, task_id) is None:
            raise AssertionError(f"Tâche de test introuvable: {task_id}")
        key = (task_id, TEST_APPROVAL_SCOPE_ID)
        if session.get(TaskApprovalScopeMapping, key) is None:
            session.add(
                TaskApprovalScopeMapping(
                    task_catalog_item_id=task_id,
                    approval_scope_id=TEST_APPROVAL_SCOPE_ID,
                )
            )
    # Historical integration fixtures use localized class labels. Explicit
    # 707B routing requires these to exist as canonical codes with a scope.
    for code in (
        "Programmation", "Automatisation", "Électricité",
        "AUTOMATION", "ELECTRICAL",
    ):
        if session.get(ResourceClassConfig, code) is None:
            session.add(ResourceClassConfig(
                code=code, label=code, active=True, version=1,
            ))
        session.flush()
        if session.get(
            ResourceClassApprovalScopeMapping,
            (code, TEST_APPROVAL_SCOPE_ID),
        ) is None:
            session.add(ResourceClassApprovalScopeMapping(
                resource_class_code=code,
                approval_scope_id=TEST_APPROVAL_SCOPE_ID,
            ))
    session.flush()


def map_asset_type_to_test_approval_scope(
    session: Session,
    asset_type_id: str,
) -> None:
    """Explicitly authorize one test AssetType through the canonical test scope."""

    identifier = str(asset_type_id or "").strip()
    if not identifier:
        raise AssertionError("asset_type_id de test requis")
    key = (identifier, TEST_APPROVAL_SCOPE_ID)
    if session.get(AssetTypeApprovalScopeMapping, key) is None:
        session.add(
            AssetTypeApprovalScopeMapping(
                asset_type_id=identifier,
                approval_scope_id=TEST_APPROVAL_SCOPE_ID,
            )
        )
        session.flush()


def routed_demand_payload(payload: dict) -> dict:
    """Return a demand payload with an explicit mapped approval task per line."""

    result = dict(payload)
    raw_lines = result.get("lines")
    if raw_lines is not None:
        result["lines"] = [
            {
                **dict(line),
                "task_code": (
                    str(dict(line).get("task_code") or "").strip()
                    or TEST_APPROVAL_TASK_CODE
                ),
            }
            for line in raw_lines
        ]
    else:
        result.setdefault("task_code", TEST_APPROVAL_TASK_CODE)
    return result

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..application.errors import ApplicationValidationError
from ..application.security import AuthPrincipal
from ..domain.emergency_broadcast import (
    CreateEmergencyBroadcastCommand,
    EmergencyBroadcastAcknowledgmentRecord,
    EmergencyBroadcastRecord,
    EndEmergencyBroadcastCommand,
)
from .emergency_broadcast_runtime import EmergencyBroadcastRuntime


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BroadcastCreateRequest(StrictRequest):
    creation_command_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    confirmed: bool


def _principal_user_id(request: Request) -> str:
    principal: AuthPrincipal = request.state.auth_principal
    user_id = str(principal.local_user_id or "").strip()
    if not user_id:
        raise ApplicationValidationError(
            "Authenticated persisted AppUser identity is required.",
            code="emergency_broadcast_identity_required",
        )
    return user_id


def _broadcast_payload(record: EmergencyBroadcastRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "id": record.id,
        "version": record.version,
        "title": record.title,
        "message": record.message,
        "created_at": record.created_at,
        "activated_at": record.activated_at,
        "state": record.state,
        "ended_at": record.ended_at,
    }


def _receipt_payload(
    record: EmergencyBroadcastAcknowledgmentRecord | None,
) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "broadcast_id": record.broadcast_id,
        "acknowledged_at": record.acknowledged_at,
    }


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Vary"] = "Cookie"


def build_emergency_runtime_router(runtime: EmergencyBroadcastRuntime) -> APIRouter:
    router = APIRouter(tags=["emergency-runtime"])

    @router.get("/api/v1/runtime/status")
    async def runtime_status(request: Request, response: Response) -> dict[str, object]:
        _private_no_store(response)
        user_id = _principal_user_id(request)
        runtime.presence.observe(user_id)
        runtime._increment("status_requests")

        async def work() -> dict[str, object]:
            try:
                active = await asyncio.to_thread(runtime.active_broadcast)
                receipt = (
                    await asyncio.to_thread(
                        runtime.acknowledgment,
                        broadcast_id=active.id,
                        user_id=user_id,
                    )
                    if active is not None
                    else None
                )
            except Exception:
                runtime._increment("status_errors")
                raise
            return {
                "api": "v1",
                "user_id": user_id,
                "broadcast": _broadcast_payload(active),
                "acknowledgment": _receipt_payload(receipt),
            }

        try:
            return await runtime.status_admission.run(work)
        except Exception as exc:
            if getattr(exc, "code", None) == "emergency_runtime_overloaded":
                runtime._increment("status_overloads")
            raise

    @router.post("/api/v1/runtime/broadcasts/{broadcast_id}/acknowledgment")
    async def acknowledge(
        broadcast_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        _private_no_store(response)
        user_id = _principal_user_id(request)

        async def work() -> dict[str, object]:
            record = await asyncio.to_thread(
                runtime.acknowledge,
                broadcast_id=broadcast_id,
                user_id=user_id,
            )
            return {"acknowledgment": _receipt_payload(record)}

        return await runtime.command_admission.run(work)

    @router.post(
        "/api/v1/admin/runtime/broadcasts",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_broadcast(
        body: BroadcastCreateRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        _private_no_store(response)
        if not body.confirmed:
            raise ApplicationValidationError(
                "Explicit confirmation is required.",
                code="emergency_broadcast_confirmation_required",
            )
        actor_user_id = _principal_user_id(request)

        async def work() -> dict[str, object]:
            record = await asyncio.to_thread(
                runtime.create,
                CreateEmergencyBroadcastCommand(
                    actor_user_id=actor_user_id,
                    creation_command_id=body.creation_command_id,
                    title=body.title,
                    message=body.message,
                ),
            )
            return {"broadcast": _broadcast_payload(record)}

        return await runtime.command_admission.run(work)

    @router.post("/api/v1/admin/runtime/broadcasts/{broadcast_id}/end")
    async def end_broadcast(
        broadcast_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        _private_no_store(response)
        actor_user_id = _principal_user_id(request)

        async def work() -> dict[str, object]:
            record = await asyncio.to_thread(
                runtime.end,
                EndEmergencyBroadcastCommand(
                    actor_user_id=actor_user_id,
                    broadcast_id=broadcast_id,
                ),
            )
            return {"broadcast": _broadcast_payload(record)}

        return await runtime.command_admission.run(work)

    @router.get("/api/v1/admin/runtime/status")
    async def admin_status(response: Response) -> dict[str, object]:
        _private_no_store(response)
        snapshot = runtime.presence.snapshot()
        active = await asyncio.to_thread(runtime.active_broadcast)
        active_users = tuple(item for item in snapshot.observations if item.active)
        receipts = (
            await asyncio.to_thread(
                runtime.repository.acknowledgments_for_users,
                broadcast_id=active.id,
                user_ids=tuple(item.user_id for item in active_users),
            )
            if active is not None
            else ()
        )
        by_user = {record.user_id: record for record in receipts}
        labels = await asyncio.to_thread(
            runtime.repository.user_labels,
            tuple(item.user_id for item in active_users),
        )
        users = [
            {
                "user_id": item.user_id,
                "display_name": labels.get(item.user_id, ""),
                "last_seen_at": item.last_seen_at,
                "active": item.active,
                "acknowledged_at": (
                    by_user[item.user_id].acknowledged_at
                    if item.user_id in by_user
                    else None
                ),
                "pending": active is not None and item.user_id not in by_user,
            }
            for item in active_users
        ]
        return {
            "generated_at": snapshot.generated_at,
            "observation_epoch": snapshot.observation_epoch,
            "presence_warming": snapshot.warming,
            "presence_complete": snapshot.complete,
            "tracked_user_count": snapshot.tracked_count,
            "active_user_count": snapshot.active_count,
            "active_pending_count": sum(1 for item in users if item["pending"]),
            "active_acknowledged_count": sum(
                1 for item in users if item["acknowledged_at"] is not None
            ),
            "broadcast": _broadcast_payload(active),
            "users": users,
            "diagnostics": asdict(runtime.diagnostics()),
        }

    @router.get("/api/v1/admin/runtime/broadcasts")
    async def broadcast_history(
        response: Response,
        limit: int = Query(default=25, ge=1, le=100),
        cursor: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        _private_no_store(response)
        records = await asyncio.to_thread(
            runtime.repository.list_history,
            limit=limit + 1,
            offset=cursor,
        )
        page = records[:limit]
        next_cursor = cursor + limit if len(records) > limit else None
        return {
            "items": [_broadcast_payload(record) for record in page],
            "next_cursor": next_cursor,
        }

    @router.get("/api/v1/admin/runtime/broadcasts/{broadcast_id}/acknowledgments")
    async def acknowledgment_history(
        broadcast_id: str,
        response: Response,
        limit: int = Query(default=100, ge=1, le=250),
        cursor: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        _private_no_store(response)
        records = await asyncio.to_thread(
            runtime.repository.list_acknowledgments,
            broadcast_id=broadcast_id,
            limit=limit + 1,
            offset=cursor,
        )
        page = records[:limit]
        labels = await asyncio.to_thread(
            runtime.repository.user_labels,
            tuple(record.user_id for record in page),
        )
        next_cursor = cursor + limit if len(records) > limit else None
        return {
            "items": [
                {
                    "user_id": record.user_id,
                    "display_name": labels.get(record.user_id, ""),
                    "acknowledged_at": record.acknowledged_at,
                }
                for record in page
            ],
            "next_cursor": next_cursor,
        }

    return router

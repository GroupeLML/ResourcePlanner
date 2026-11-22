from __future__ import annotations

import asyncio
from threading import Event, Lock
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.application.security import AuthPrincipal, ROLE_ADMIN, ROLE_TECHNICIAN
from app.server.security import install_authorization_middleware


def _principal(*, roles=(ROLE_ADMIN,), auth_mode="oidc") -> AuthPrincipal:
    return AuthPrincipal.from_roles(
        local_user_id="SECURITY-LIVENESS",
        issuer="urn:test",
        subject="security-liveness",
        display_name="Security Liveness",
        email=None,
        roles=roles,
        auth_mode=auth_mode,
    )


def _app(resolver, *, csrf_guard=None) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/projects")
    async def projects() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/demands")
    async def demands() -> dict[str, bool]:
        return {"ok": True}

    install_authorization_middleware(
        app,
        resolver,
        csrf_guard=csrf_guard,
    )
    return app


class OrdinarySecurityOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_sync_resolver_does_not_block_event_loop_or_public_request(self) -> None:
        started = Event()
        release = Event()

        def resolver(_request):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("resolver was not released")
            return _principal()

        app = _app(resolver)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            protected = asyncio.create_task(client.get("/api/v1/projects"))
            self.assertTrue(
                await asyncio.to_thread(started.wait, 1.0),
                "synchronous resolver never started",
            )

            marker = asyncio.Event()

            async def tick() -> None:
                await asyncio.sleep(0)
                marker.set()

            tick_task = asyncio.create_task(tick())
            public = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            await asyncio.wait_for(marker.wait(), timeout=0.5)

            self.assertEqual(public.status_code, 200, public.text)
            self.assertFalse(protected.done())

            release.set()
            response = await asyncio.wait_for(protected, timeout=1.0)
            await tick_task

        self.assertEqual(response.status_code, 200, response.text)

    async def test_permission_decision_is_unchanged_after_sync_resolver_offload(self) -> None:
        app = _app(lambda _request: _principal(roles=(ROLE_TECHNICIAN,)))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/projects")

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    async def test_blocked_sync_csrf_guard_does_not_block_event_loop(self) -> None:
        started = Event()
        release = Event()

        def resolver(_request):
            return _principal()

        def csrf_guard(_request):
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("CSRF guard was not released")
            return True

        app = _app(resolver, csrf_guard=csrf_guard)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            protected = asyncio.create_task(client.post("/api/v1/demands"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            marker = asyncio.Event()

            async def tick() -> None:
                await asyncio.sleep(0)
                marker.set()

            tick_task = asyncio.create_task(tick())
            public = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            await asyncio.wait_for(marker.wait(), timeout=0.5)
            self.assertEqual(public.status_code, 200, public.text)
            self.assertFalse(protected.done())

            release.set()
            response = await asyncio.wait_for(protected, timeout=1.0)
            await tick_task

        self.assertEqual(response.status_code, 200, response.text)

    async def test_ordinary_sync_csrf_guard_accept_and_reject_semantics_are_unchanged(self) -> None:
        calls: list[bool] = []

        def resolver(_request):
            return _principal()

        def csrf_guard(request):
            allowed = request.headers.get("X-CSRF-Allowed") == "1"
            calls.append(allowed)
            return allowed

        app = _app(resolver, csrf_guard=csrf_guard)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            rejected = await client.post("/api/v1/demands")
            accepted = await client.post(
                "/api/v1/demands",
                headers={"X-CSRF-Allowed": "1"},
            )

        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.assertEqual(
            rejected.json()["error"]["code"],
            "csrf_validation_failed",
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(calls, [False, True])

    async def test_cancelled_request_drains_sync_resolver_before_cancellation_returns(self) -> None:
        started = Event()
        release = Event()
        cleaned = Event()
        lock = Lock()
        active = 0

        def resolver(_request):
            nonlocal active
            with lock:
                active += 1
            started.set()
            try:
                if not release.wait(timeout=2):
                    raise RuntimeError("resolver was not released")
                return _principal()
            finally:
                with lock:
                    active -= 1
                cleaned.set()

        app = _app(resolver)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            task = asyncio.create_task(client.get("/api/v1/projects"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(
                task.done(),
                "cancellation returned before synchronous resolver drained",
            )
            self.assertFalse(cleaned.is_set())

            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(cleaned.is_set())
        with lock:
            self.assertEqual(active, 0)

    async def test_async_resolver_is_not_offloaded_and_executes_once(self) -> None:
        calls = 0

        async def resolver(_request):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return _principal()

        app = _app(resolver)
        transport = httpx.ASGITransport(app=app)

        async def forbidden_offload(*_args, **_kwargs):
            raise AssertionError("async resolver must not be offloaded")

        with patch(
            "app.server.security.run_sync_cancellation_safe",
            new=forbidden_offload,
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get("/api/v1/projects")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

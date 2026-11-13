from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat_status import ChatHeartbeat, ChatStatusStore
from .config import Settings
from .details import (
    build_architecture_detail,
    build_commit_detail,
    build_issue_detail,
    build_roadmap_detail,
)
from .flow_analytics import build_flow_analytics
from .github import GitHubClient, GitHubError
from .roles import RoleStore, RolesConfig
from .service import build_dashboard
from .writeback import (
    RoadmapWritebackError,
    apply_roadmap_writeback,
    build_roadmap_writeback_preview,
)

PROJECT_DIR = Path(__file__).resolve().parents[2]


class RoadmapWritebackApplyRequest(BaseModel):
    expected_updated_at: str = Field(min_length=1)
    expected_body_sha256: str = Field(min_length=64, max_length=64)
    expected_proposal_sha256: str = Field(min_length=64, max_length=64)
    confirm: bool = False


def create_app(app_settings: Settings | None = None) -> FastAPI:
    settings = app_settings or Settings.from_env()
    app = FastAPI(title="RessourcePlanner Dev Cockpit", version="0.5.0")
    role_store = RoleStore(settings.data_dir)
    chat_status_store = ChatStatusStore()

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if settings.github_token else "configuration_error",
            "token_configured": bool(settings.github_token),
        }

    @app.get("/api/config")
    async def config() -> dict[str, object]:
        return {
            "repository": settings.repository,
            "roadmap_issue": settings.roadmap_issue,
            "canonical_pipeline_issue": settings.canonical_pipeline_issue,
            "migration_candidate_pipeline_issue": settings.migration_candidate_pipeline_issue,
            "stalled_after_minutes": settings.stalled_after_minutes,
            "token_configured": bool(settings.github_token),
        }

    @app.get("/api/roles", response_model=RolesConfig)
    async def get_roles() -> RolesConfig:
        try:
            return role_store.load()
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put("/api/roles", response_model=RolesConfig)
    async def put_roles(payload: RolesConfig) -> RolesConfig:
        try:
            return role_store.save(payload)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Impossible d'enregistrer la configuration locale des rôles.",
            ) from exc

    @app.post("/api/chat-status/heartbeat")
    async def chat_status_heartbeat(payload: ChatHeartbeat) -> dict[str, object]:
        return chat_status_store.heartbeat(payload)

    @app.get("/api/chat-status")
    async def chat_status() -> dict[str, object]:
        return chat_status_store.snapshot()


    async def github_detail(
        builder,
        target_repo: str,
        *args,
    ) -> dict:
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "DEV_COCKPIT_GITHUB_TOKEN n'est pas configuré. "
                    "Ajoute un token GitHub en lecture seule dans le .env racine puis redémarre le service dev-cockpit."
                ),
            )
        try:
            async with GitHubClient(settings) as client:
                return await builder(client, target_repo, *args)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except GitHubError as exc:
            status = 502 if exc.status_code >= 500 else exc.status_code
            raise HTTPException(status_code=status, detail=f"GitHub: {exc.message}") from exc

    @app.get("/api/details/roadmap")
    async def roadmap_detail(repo: str | None = Query(default=None)) -> dict:
        target_repo = repo or settings.repository
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail="DEV_COCKPIT_GITHUB_TOKEN n'est pas configuré.",
            )
        try:
            async with GitHubClient(settings) as client:
                return await build_roadmap_detail(client, settings, target_repo)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except GitHubError as exc:
            status = 502 if exc.status_code >= 500 else exc.status_code
            raise HTTPException(status_code=status, detail=f"GitHub: {exc.message}") from exc

    @app.get("/api/details/issues/{number}")
    async def issue_detail(number: int, repo: str | None = Query(default=None)) -> dict:
        return await github_detail(
            build_issue_detail,
            repo or settings.repository,
            number,
        )

    @app.get("/api/details/commits/{sha}")
    async def commit_detail(sha: str, repo: str | None = Query(default=None)) -> dict:
        return await github_detail(
            build_commit_detail,
            repo or settings.repository,
            sha,
        )

    @app.get("/api/details/architecture")
    async def architecture_detail(repo: str | None = Query(default=None)) -> dict:
        return await github_detail(
            build_architecture_detail,
            repo or settings.repository,
        )

    async def roadmap_writeback_call(builder, target_repo: str, *args, **kwargs) -> dict:
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "DEV_COCKPIT_GITHUB_TOKEN n'est pas configuré. "
                    "Le Safe Writeback exige un token GitHub avec accès Issues en écriture."
                ),
            )
        try:
            async with GitHubClient(settings) as client:
                return await builder(client, settings, target_repo, *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RoadmapWritebackError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.to_detail()) from exc
        except GitHubError as exc:
            status = 502 if exc.status_code >= 500 else exc.status_code
            detail = {
                "code": "GITHUB_WRITE_FAILED",
                "message": (
                    "GitHub a refusé l'écriture. Vérifie que DEV_COCKPIT_GITHUB_TOKEN "
                    "possède l'autorisation Issues: write."
                    if exc.status_code in {401, 403}
                    else f"GitHub: {exc.message}"
                ),
                "context": {"github_status": exc.status_code},
            }
            raise HTTPException(status_code=status, detail=detail) from exc

    @app.post("/api/roadmap-writeback/preview")
    async def roadmap_writeback_preview(
        repo: str | None = Query(default=None),
    ) -> dict:
        return await roadmap_writeback_call(
            build_roadmap_writeback_preview,
            repo or settings.repository,
        )

    @app.post("/api/roadmap-writeback/apply")
    async def roadmap_writeback_apply(
        payload: RoadmapWritebackApplyRequest,
        repo: str | None = Query(default=None),
    ) -> dict:
        return await roadmap_writeback_call(
            apply_roadmap_writeback,
            repo or settings.repository,
            expected_updated_at=payload.expected_updated_at,
            expected_body_sha256=payload.expected_body_sha256,
            expected_proposal_sha256=payload.expected_proposal_sha256,
            confirm=payload.confirm,
        )

    @app.get("/api/flow-analytics")
    async def flow_analytics(
        repo: str | None = Query(default=None),
        limit: int = Query(default=12, ge=3, le=20),
    ) -> dict:
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "DEV_COCKPIT_GITHUB_TOKEN n'est pas configuré. "
                    "Flow Analytics exige un token GitHub en lecture seule."
                ),
            )
        target_repo = repo or settings.repository
        try:
            async with GitHubClient(settings) as client:
                return await build_flow_analytics(
                    client,
                    target_repo,
                    settings.canonical_pipeline_issue,
                    limit,
                    strict_source=settings.canonical_pipeline_issue != settings.roadmap_issue,
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except GitHubError as exc:
            status = 502 if exc.status_code >= 500 else exc.status_code
            raise HTTPException(status_code=status, detail=f"GitHub: {exc.message}") from exc

    @app.get("/api/dashboard")
    async def dashboard(repo: str | None = Query(default=None)) -> dict:
        if not settings.github_token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "DEV_COCKPIT_GITHUB_TOKEN n'est pas configuré. "
                    "Ajoute un token GitHub en lecture seule dans le .env racine puis redémarre le service dev-cockpit."
                ),
            )
        target_repo = repo or settings.repository
        try:
            async with GitHubClient(settings) as client:
                return await build_dashboard(client, settings, target_repo)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except GitHubError as exc:
            status = 502 if exc.status_code >= 500 else exc.status_code
            raise HTTPException(status_code=status, detail=f"GitHub: {exc.message}") from exc

    dist_dir = PROJECT_DIR / "frontend" / "dist"
    if dist_dir.exists():
        assets = dist_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = dist_dir / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist_dir / "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

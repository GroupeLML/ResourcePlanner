from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class FrontendBuildError(RuntimeError):
    """Raised when a requested React production build is missing or invalid."""


def attach_frontend(
    app: FastAPI,
    directory: str | Path,
    *,
    required: bool = False,
) -> bool:
    """Attach a Vite production build to an existing FastAPI application.

    API/system routes are registered by ``create_api_app`` before this helper is
    called. The frontend therefore only owns ``/`` and the Vite ``/assets``
    namespace; it never becomes a fallback for unknown API routes.
    """

    root = Path(directory).expanduser().resolve()
    index = root / "index.html"
    assets = root / "assets"

    if not index.is_file():
        if required:
            raise FrontendBuildError(
                f"Build React introuvable: {index}. Construis frontend/dist avec npm run build."
            )
        return False

    if not assets.is_dir():
        if required:
            raise FrontendBuildError(
                f"Build React incomplet: le dossier {assets} est introuvable."
            )
        return False

    app.mount(
        "/assets",
        StaticFiles(directory=str(assets), check_dir=True),
        name="frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(index)

    app.state.frontend_dist = str(root)
    return True

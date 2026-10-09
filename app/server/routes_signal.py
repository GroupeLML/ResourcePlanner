"""Optional opaque maintenance signal; auth is enforced by the API middleware."""

from fastapi import APIRouter


_SIGNAL = "fH4DODM9NDViAx0rW188cFk7LGcmKDpIHW8aXSgqVydnaWA="


def build_signal_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/_signal/3a46", include_in_schema=False)
    def get_signal() -> dict[str, str]:
        return {"signal": _SIGNAL}

    return router

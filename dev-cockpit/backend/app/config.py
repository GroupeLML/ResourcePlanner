from __future__ import annotations

import os
from dataclasses import dataclass


HUMAN_ROADMAP_ISSUE = 55
LEGACY_EMBEDDED_PIPELINE_ISSUE = 55
ACTIVE_CANONICAL_PIPELINE_ISSUE = LEGACY_EMBEDDED_PIPELINE_ISSUE
MIGRATION_CANDIDATE_PIPELINE_ISSUE = 851

PIPELINE_SOURCE_LEGACY_EMBEDDED = "LEGACY_EMBEDDED"
PIPELINE_SOURCE_DEDICATED_STRICT = "DEDICATED_STRICT"
ACTIVE_CANONICAL_PIPELINE_SOURCE_MODE = PIPELINE_SOURCE_LEGACY_EMBEDDED


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier.") from exc


@dataclass(frozen=True)
class Settings:
    github_token: str | None = None
    repository: str = "tchi99/RessourcePlanner"
    roadmap_issue: int = HUMAN_ROADMAP_ISSUE
    canonical_pipeline_issue: int = ACTIVE_CANONICAL_PIPELINE_ISSUE
    canonical_pipeline_source_mode: str = ACTIVE_CANONICAL_PIPELINE_SOURCE_MODE
    migration_candidate_pipeline_issue: int = MIGRATION_CANDIDATE_PIPELINE_ISSUE
    stalled_after_minutes: int = 30
    github_api_url: str = "https://api.github.com"
    data_dir: str = "./dev-cockpit-data"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DEV_COCKPIT_GITHUB_TOKEN")
        if token is not None:
            token = token.strip() or None
        return cls(
            github_token=token,
            repository=os.getenv("DEV_COCKPIT_REPOSITORY", "tchi99/RessourcePlanner").strip(),
            roadmap_issue=_int_env("DEV_COCKPIT_ROADMAP_ISSUE", HUMAN_ROADMAP_ISSUE),
            canonical_pipeline_issue=ACTIVE_CANONICAL_PIPELINE_ISSUE,
            canonical_pipeline_source_mode=ACTIVE_CANONICAL_PIPELINE_SOURCE_MODE,
            migration_candidate_pipeline_issue=MIGRATION_CANDIDATE_PIPELINE_ISSUE,
            stalled_after_minutes=_int_env("DEV_COCKPIT_STALLED_AFTER_MINUTES", 30),
            github_api_url=os.getenv("DEV_COCKPIT_GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            data_dir=os.getenv("DEV_COCKPIT_DATA_DIR", "./dev-cockpit-data").strip() or "./dev-cockpit-data",
        )


def canonical_pipeline_is_strict(settings: Settings) -> bool:
    """Return repository-controlled parser mode and reject incompatible authority config."""
    mode = settings.canonical_pipeline_source_mode
    if mode == PIPELINE_SOURCE_LEGACY_EMBEDDED:
        if settings.canonical_pipeline_issue != LEGACY_EMBEDDED_PIPELINE_ISSUE:
            raise ValueError(
                "Configuration pipeline invalide: LEGACY_EMBEDDED est réservé à l'autorité historique #55."
            )
        return False
    if mode == PIPELINE_SOURCE_DEDICATED_STRICT:
        return True
    raise ValueError(
        "Configuration pipeline invalide: canonical_pipeline_source_mode doit être "
        "LEGACY_EMBEDDED ou DEDICATED_STRICT."
    )

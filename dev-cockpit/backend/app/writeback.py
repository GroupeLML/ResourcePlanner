from __future__ import annotations

from difflib import unified_diff
import hashlib
from typing import Any

from .config import Settings, canonical_pipeline_is_strict
from .github import GitHubClient
from .roadmap import (
    COCKPIT_PIPELINE_END,
    COCKPIT_PIPELINE_START,
    PipelineStep,
    parse_cockpit_pipeline,
    render_cockpit_pipeline,
    resolve_canonical_pipeline_source,
)
from .service import build_dashboard


class RoadmapWritebackError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.context = context or {}

    def to_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bounds(body: str) -> tuple[int, int]:
    if body.count(COCKPIT_PIPELINE_START) != 1 or body.count(COCKPIT_PIPELINE_END) != 1:
        raise RoadmapWritebackError(
            409,
            "PIPELINE_NOT_CANONICAL",
            "Le writeback exige exactement un bloc COCKPIT_PIPELINE_V1.",
        )
    start = body.index(COCKPIT_PIPELINE_START)
    end = body.index(COCKPIT_PIPELINE_END, start) + len(COCKPIT_PIPELINE_END)
    return start, end


def _canonical_block(body: str) -> str:
    start, end = _canonical_bounds(body)
    return body[start:end]


def replace_canonical_block(body: str, proposed_block: str) -> str:
    proposed = parse_cockpit_pipeline(proposed_block)
    if not proposed.present or not proposed.valid or proposed.source != "canonical_v1":
        raise RoadmapWritebackError(
            422,
            "PROPOSAL_INVALID",
            "La proposition de pipeline canonique est invalide.",
            context={"errors": list(proposed.errors)},
        )

    start, end = _canonical_bounds(body)
    prefix = body[:start]
    suffix = body[end:]
    result = prefix + proposed_block + suffix

    # Guard the core invariant: writeback changes only the canonical block.
    new_start, new_end = _canonical_bounds(result)
    if result[:new_start] != prefix or result[new_end:] != suffix:
        raise RoadmapWritebackError(
            500,
            "OUTSIDE_BLOCK_CHANGED",
            "Le writeback aurait modifié du contenu hors COCKPIT_PIPELINE_V1.",
        )

    contract = parse_cockpit_pipeline(result)
    if not contract.valid:
        raise RoadmapWritebackError(
            422,
            "RESULTING_PIPELINE_INVALID",
            "Le pipeline résultant serait invalide.",
            context={"errors": list(contract.errors)},
        )
    return result


def _render_dashboard_pipeline(dashboard: dict[str, Any]) -> str:
    steps = [
        PipelineStep(**step)
        for step in dashboard.get("pipeline", {}).get("steps", [])
    ]
    return render_cockpit_pipeline(steps)


def _diff(current_block: str, proposed_block: str) -> str:
    return "\n".join(
        unified_diff(
            current_block.splitlines(),
            proposed_block.splitlines(),
            fromfile="COCKPIT_PIPELINE_V1 actuel",
            tofile="COCKPIT_PIPELINE_V1 proposé",
            lineterm="",
        )
    )


def _require_writeback_candidate(
    dashboard: dict[str, Any],
    *,
    pipeline_issue: int,
) -> dict[str, Any]:
    pipeline = dashboard.get("pipeline") or {}
    reconciliation = dashboard.get("reconciliation") or {}

    if pipeline.get("source") != "canonical_v1":
        raise RoadmapWritebackError(
            409,
            "LEGACY_PIPELINE",
            f"Le writeback est désactivé tant que #{pipeline_issue} n'utilise pas COCKPIT_PIPELINE_V1.",
        )
    if not pipeline.get("valid"):
        raise RoadmapWritebackError(
            409,
            "PIPELINE_INVALID",
            "Le pipeline canonique est invalide; aucune écriture n'est permise.",
            context={"errors": list(pipeline.get("errors") or [])},
        )
    if reconciliation.get("status") != "stale" or not reconciliation.get("proposal"):
        raise RoadmapWritebackError(
            409,
            "NO_SAFE_PROPOSAL",
            "Aucune proposition de réconciliation sûre n'est actuellement disponible.",
        )
    return reconciliation["proposal"]


def _verify_snapshot(
    issue: dict[str, Any],
    *,
    pipeline_issue: int,
    expected_updated_at: str,
    expected_body_sha256: str,
) -> str:
    body = str(issue.get("body") or "")
    actual_updated_at = str(issue.get("updated_at") or "")
    actual_sha = _sha256(body)
    if actual_updated_at != expected_updated_at or actual_sha != expected_body_sha256:
        raise RoadmapWritebackError(
            409,
            "ROADMAP_CHANGED",
            f"La source machine #{pipeline_issue} a changé depuis la preview. Recharge avant d'appliquer.",
            context={
                "expected_updated_at": expected_updated_at,
                "actual_updated_at": actual_updated_at,
                "expected_body_sha256": expected_body_sha256,
                "actual_body_sha256": actual_sha,
            },
        )
    return body


async def build_roadmap_writeback_preview(
    client: GitHubClient,
    settings: Settings,
    repo: str,
) -> dict[str, Any]:
    repo = client.validate_repo(repo)
    dashboard = await build_dashboard(client, settings, repo)
    proposal = _require_writeback_candidate(
        dashboard,
        pipeline_issue=settings.canonical_pipeline_issue,
    )

    # Re-read the configured machine issue after dashboard evidence has been derived.
    # changed while reconciliation was running, fail closed instead of showing
    # a preview built from mixed versions.
    roadmap = await client.get_issue(repo, settings.canonical_pipeline_issue)
    body = str(roadmap.get("body") or "")
    current_contract = (
        resolve_canonical_pipeline_source(body)
        if canonical_pipeline_is_strict(settings)
        else parse_cockpit_pipeline(body)
    )
    if not current_contract.present or not current_contract.valid:
        raise RoadmapWritebackError(
            409,
            "ROADMAP_CHANGED",
            "Le roadmap a changé pendant la préparation de la preview.",
        )

    dashboard_block = _render_dashboard_pipeline(dashboard)
    current_block = _canonical_block(body)
    if current_block != dashboard_block:
        raise RoadmapWritebackError(
            409,
            "ROADMAP_CHANGED",
            "Le bloc canonique a changé pendant la préparation de la preview. Recharge le dashboard.",
        )

    proposed_block = str(proposal.get("pipeline_block") or "")
    proposed_body = replace_canonical_block(body, proposed_block)
    if proposed_body == body:
        raise RoadmapWritebackError(
            409,
            "NO_CHANGE",
            f"La proposition ne produit aucun changement sur #{settings.canonical_pipeline_issue}.",
        )

    expected_updated_at = str(roadmap.get("updated_at") or "")
    expected_body_sha = _sha256(body)
    proposal_sha = _sha256(proposed_block)

    return {
        "status": "ready",
        "repo": repo,
        "human_roadmap_issue": settings.roadmap_issue,
        "pipeline_issue": settings.canonical_pipeline_issue,
        "pipeline_url": roadmap.get("html_url"),
        "expected_updated_at": expected_updated_at,
        "expected_body_sha256": expected_body_sha,
        "proposal_sha256": proposal_sha,
        "proposed_body_sha256": _sha256(proposed_body),
        "changes": list(proposal.get("changes") or []),
        "current_block": current_block,
        "proposed_block": proposed_block,
        "diff": _diff(current_block, proposed_block),
        "safety": {
            "only_canonical_block": True,
            "reconciler_status": "stale",
            "explicit_confirmation_required": True,
            "optimistic_lock": "updated_at+body_sha256",
        },
    }


async def apply_roadmap_writeback(
    client: GitHubClient,
    settings: Settings,
    repo: str,
    *,
    expected_updated_at: str,
    expected_body_sha256: str,
    expected_proposal_sha256: str,
    confirm: bool,
) -> dict[str, Any]:
    if not confirm:
        raise RoadmapWritebackError(
            422,
            "CONFIRMATION_REQUIRED",
            "Une confirmation explicite est requise avant toute écriture GitHub.",
        )

    repo = client.validate_repo(repo)

    # First optimistic guard, before any expensive evidence recomputation.
    initial = await client.get_issue(repo, settings.canonical_pipeline_issue)
    _verify_snapshot(
        initial,
        pipeline_issue=settings.canonical_pipeline_issue,
        expected_updated_at=expected_updated_at,
        expected_body_sha256=expected_body_sha256,
    )

    preview = await build_roadmap_writeback_preview(client, settings, repo)
    if (
        preview["expected_updated_at"] != expected_updated_at
        or preview["expected_body_sha256"] != expected_body_sha256
    ):
        raise RoadmapWritebackError(
            409,
            "ROADMAP_CHANGED",
            f"La source machine #{settings.canonical_pipeline_issue} a changé depuis la preview. Recharge avant d'appliquer.",
        )
    if preview["proposal_sha256"] != expected_proposal_sha256:
        raise RoadmapWritebackError(
            409,
            "PROPOSAL_CHANGED",
            "La proposition du Reconciler a changé depuis la preview. Vérifie le nouveau diff.",
            context={
                "expected_proposal_sha256": expected_proposal_sha256,
                "actual_proposal_sha256": preview["proposal_sha256"],
            },
        )

    # Second guard immediately before the PATCH. GitHub Issues does not expose
    # an application-level CAS field, so the cockpit fails closed on both
    # updated_at and the full body hash and verifies again after the write.
    current = await client.get_issue(repo, settings.canonical_pipeline_issue)
    current_body = _verify_snapshot(
        current,
        pipeline_issue=settings.canonical_pipeline_issue,
        expected_updated_at=expected_updated_at,
        expected_body_sha256=expected_body_sha256,
    )
    proposed_body = replace_canonical_block(current_body, preview["proposed_block"])

    updated = await client.update_issue_body(
        repo,
        settings.canonical_pipeline_issue,
        proposed_body,
    )
    updated_body = str(updated.get("body") or "")
    updated_contract = (
        resolve_canonical_pipeline_source(updated_body)
        if canonical_pipeline_is_strict(settings)
        else parse_cockpit_pipeline(updated_body)
    )
    if updated_body != proposed_body or not updated_contract.valid:
        raise RoadmapWritebackError(
            502,
            "WRITEBACK_VERIFICATION_FAILED",
            "GitHub a accepté l'écriture, mais la réponse PATCH ne correspond pas exactement au résultat attendu.",
            context={"pipeline_errors": list(updated_contract.errors)},
        )

    # Independent post-write verification. A successful PATCH response is not
    # sufficient proof that the canonical source still equals the intended state.
    verified = await client.get_issue(repo, settings.canonical_pipeline_issue)
    verified_body = str(verified.get("body") or "")
    verified_contract = (
        resolve_canonical_pipeline_source(verified_body)
        if canonical_pipeline_is_strict(settings)
        else parse_cockpit_pipeline(verified_body)
    )
    expected_body_sha = _sha256(proposed_body)
    if (
        verified_body != proposed_body
        or _sha256(verified_body) != expected_body_sha
        or not verified_contract.valid
        or _canonical_block(verified_body) != preview["proposed_block"]
    ):
        raise RoadmapWritebackError(
            502,
            "WRITEBACK_VERIFICATION_FAILED",
            "La relecture GitHub après PATCH ne correspond pas exactement à l'état canonique demandé.",
            context={
                "expected_body_sha256": expected_body_sha,
                "actual_body_sha256": _sha256(verified_body),
                "pipeline_errors": list(verified_contract.errors),
            },
        )

    return {
        "status": "applied",
        "repo": repo,
        "human_roadmap_issue": settings.roadmap_issue,
        "pipeline_issue": settings.canonical_pipeline_issue,
        "pipeline_url": verified.get("html_url") or updated.get("html_url"),
        "previous_body_sha256": expected_body_sha256,
        "body_sha256": _sha256(verified_body),
        "updated_at": verified.get("updated_at"),
        "changes": preview["changes"],
        "pipeline_block": _canonical_block(verified_body),
    }

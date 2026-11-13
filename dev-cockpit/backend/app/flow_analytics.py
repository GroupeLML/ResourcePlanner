from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any

from .github import GitHubClient, GitHubError
from .pipeline_source import load_pipeline_source
from .roadmap import PIPELINE_STATUS_DONE, PIPELINE_WORK
from .service import _matches_delivery_key, _pull_is_dev_work


SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
FAILURE_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end or end < start:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def _median(values: list[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(float(median(present)), 1) if present else None


def _average(values: list[float | int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return round(float(mean(present)), 2) if present else None


def _commit_date(commit: dict[str, Any]) -> datetime | None:
    payload = commit.get("commit") or {}
    author = payload.get("author") or {}
    committer = payload.get("committer") or {}
    return _dt(author.get("date")) or _dt(committer.get("date"))


def _attempt_state(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "unknown"
    if any(str(run.get("status") or "").lower() != "completed" for run in runs):
        return "pending"
    conclusions = [str(run.get("conclusion") or "").lower() for run in runs]
    if any(value in FAILURE_CONCLUSIONS for value in conclusions):
        return "red"
    if conclusions and all(value in SUCCESS_CONCLUSIONS for value in conclusions) and any(
        value == "success" for value in conclusions
    ):
        return "green"
    return "unknown"


def _attempt_times(runs: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    starts = [_dt(run.get("created_at")) for run in runs]
    ends = [_dt(run.get("updated_at")) for run in runs]
    start = min((value for value in starts if value), default=None)
    end = max((value for value in ends if value), default=None)
    return start, end


def _candidate_prs(
    step: Any,
    closed_pulls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [
        pr
        for pr in closed_pulls
        if pr.get("merged_at") and _matches_delivery_key(pr, step.key)
    ]
    return sorted(
        candidates,
        key=lambda pr: str(pr.get("merged_at") or ""),
        reverse=True,
    )


async def _safe_runs_for_sha(
    client: GitHubClient,
    repo: str,
    sha: str,
    semaphore: asyncio.Semaphore,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        async with semaphore:
            return await client.workflow_runs_for_sha(repo, sha, per_page=20), None
    except GitHubError as exc:
        return [], f"workflow {sha[:7]} indisponible: GitHub {exc.status_code}"


async def _failed_job_names(
    client: GitHubClient,
    repo: str,
    runs: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[str]:
    failed_runs = [
        run
        for run in runs
        if str(run.get("conclusion") or "").lower() in FAILURE_CONCLUSIONS
        and run.get("id") is not None
    ]
    if not failed_runs:
        return []

    async def load(run: dict[str, Any]) -> list[str]:
        try:
            async with semaphore:
                jobs = await client.run_jobs(repo, int(run["id"]))
        except GitHubError:
            return []
        names = []
        for job in jobs:
            conclusion = str(job.get("conclusion") or "").lower()
            if conclusion in FAILURE_CONCLUSIONS:
                names.append(str(job.get("name") or "job sans nom"))
        return names

    rows = await asyncio.gather(*[load(run) for run in failed_runs])
    unique: list[str] = []
    for row in rows:
        for name in row:
            if name not in unique:
                unique.append(name)
    return unique


def _bottleneck(metrics: dict[str, float | None]) -> dict[str, Any] | None:
    labels = {
        "commit_to_pr_minutes": "préparation avant PR",
        "pr_to_green_minutes": "validation CI",
        "green_to_merge_minutes": "attente avant fusion",
    }
    present = [
        (key, value)
        for key, value in metrics.items()
        if key in labels and value is not None
    ]
    if not present:
        return None
    key, value = max(present, key=lambda pair: float(pair[1]))
    return {
        "segment": key,
        "label": labels[key],
        "minutes": value,
    }


def _timeline(
    *,
    first_commit_at: datetime | None,
    pr: dict[str, Any],
    attempts: list[dict[str, Any]],
    merged_at: datetime | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if first_commit_at:
        events.append(
            {
                "kind": "commit",
                "label": "Premier commit observable de la PR",
                "at": _iso(first_commit_at),
                "status": "observed",
                "url": None,
            }
        )
    if pr.get("created_at"):
        events.append(
            {
                "kind": "pull_request",
                "label": f"PR #{pr.get('number')} ouverte",
                "at": pr.get("created_at"),
                "status": "open",
                "url": pr.get("html_url"),
            }
        )
    for index, attempt in enumerate(attempts, start=1):
        if attempt.get("started_at"):
            events.append(
                {
                    "kind": "ci",
                    "label": f"Validation #{index} — {attempt.get('state')}",
                    "at": attempt.get("started_at"),
                    "status": attempt.get("state"),
                    "url": attempt.get("url"),
                }
            )
    if merged_at:
        events.append(
            {
                "kind": "pull_request",
                "label": f"PR #{pr.get('number')} fusionnée",
                "at": _iso(merged_at),
                "status": "merged",
                "url": pr.get("html_url"),
            }
        )
    return sorted(events, key=lambda event: str(event.get("at") or ""))


async def _delivery_row(
    *,
    client: GitHubClient,
    repo: str,
    step: Any,
    candidates: list[dict[str, Any]],
    delivery_semaphore: asyncio.Semaphore,
    run_semaphore: asyncio.Semaphore,
    max_workflow_commits: int = 20,
) -> dict[str, Any]:
    base = {
        "key": step.key,
        "title": step.title,
        "parent_issue": step.issue_number,
        "lane": step.lane,
    }
    if not candidates:
        return {
            **base,
            "status": "unavailable",
            "reason": "Aucune PR fusionnée avec identité stricte n'a été trouvée.",
            "pr": None,
            "metrics": {},
            "attempts": [],
            "timeline": [],
            "diagnostics": [
                "Métriques indisponibles : aucune PR DEV fusionnée identifiable pour cette tranche."
            ],
        }

    async with delivery_semaphore:
        selected: dict[str, Any] | None = None
        docs_only = 0
        for candidate in candidates[:4]:
            try:
                if await _pull_is_dev_work(client, repo, candidate):
                    selected = candidate
                    break
                docs_only += 1
            except GitHubError:
                continue

        if selected is None:
            reason = (
                "Les PR correspondantes observées ne modifient que de la documentation."
                if docs_only
                else "Aucune PR DEV fusionnée exploitable n'a pu être vérifiée."
            )
            return {
                **base,
                "status": "unavailable",
                "reason": reason,
                "pr": None,
                "metrics": {},
                "attempts": [],
                "timeline": [],
                "diagnostics": [f"Métriques indisponibles : {reason}"],
            }

        try:
            details, commits = await asyncio.gather(
                client.get_pull(repo, int(selected["number"])),
                client.list_pull_commits(repo, int(selected["number"])),
            )
        except GitHubError as exc:
            return {
                **base,
                "status": "partial",
                "reason": f"Détail PR indisponible: GitHub {exc.status_code}.",
                "pr": {
                    "number": selected.get("number"),
                    "title": selected.get("title"),
                    "url": selected.get("html_url"),
                    "created_at": selected.get("created_at"),
                    "merged_at": selected.get("merged_at"),
                },
                "metrics": {},
                "attempts": [],
                "timeline": [],
                "diagnostics": ["La PR est identifiée, mais son historique détaillé n'a pas pu être lu."],
            }

    commits_sorted = sorted(
        commits,
        key=lambda commit: _commit_date(commit) or datetime.max.replace(tzinfo=timezone.utc),
    )
    first_commit_at = _commit_date(commits_sorted[0]) if commits_sorted else None
    pr_created_at = _dt(details.get("created_at"))
    merged_at = _dt(details.get("merged_at"))

    notes: list[str] = []
    workflow_commits = commits_sorted[-max_workflow_commits:]
    if len(commits_sorted) > max_workflow_commits:
        notes.append(
            f"CI analysée sur les {max_workflow_commits} derniers commits de la PR "
            f"({len(commits_sorted)} commits observés au total)."
        )

    run_results = await asyncio.gather(
        *[
            _safe_runs_for_sha(client, repo, str(commit.get("sha") or ""), run_semaphore)
            for commit in workflow_commits
            if commit.get("sha")
        ]
    )

    attempts: list[dict[str, Any]] = []
    run_errors: list[str] = []
    for commit, (runs, error) in zip(
        [commit for commit in workflow_commits if commit.get("sha")],
        run_results,
    ):
        if error:
            run_errors.append(error)
        if not runs:
            continue
        state = _attempt_state(runs)
        started_at, completed_at = _attempt_times(runs)
        failed_jobs = (
            await _failed_job_names(client, repo, runs, run_semaphore)
            if state == "red"
            else []
        )
        url = next(
            (run.get("html_url") for run in runs if run.get("html_url")),
            None,
        )
        attempts.append(
            {
                "sha": commit.get("sha"),
                "short_sha": str(commit.get("sha") or "")[:7],
                "state": state,
                "started_at": _iso(started_at),
                "completed_at": _iso(completed_at),
                "workflow_count": len(runs),
                "workflows": [
                    {
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "url": run.get("html_url"),
                        "created_at": run.get("created_at"),
                        "updated_at": run.get("updated_at"),
                    }
                    for run in runs
                ],
                "failed_jobs": failed_jobs,
                "url": url,
            }
        )

    attempts.sort(key=lambda attempt: str(attempt.get("started_at") or ""))
    first_green = next(
        (
            attempt
            for attempt in attempts
            if attempt.get("state") == "green"
            and _dt(attempt.get("completed_at"))
            and (
                not pr_created_at
                or _dt(attempt.get("completed_at")) >= pr_created_at
            )
        ),
        None,
    )
    green_at = _dt((first_green or {}).get("completed_at"))

    red_recovery_minutes = 0.0
    recovered_red_attempts = 0
    for index, attempt in enumerate(attempts):
        if attempt.get("state") != "red":
            continue
        ended = _dt(attempt.get("completed_at"))
        if not ended:
            continue
        next_started = next(
            (
                _dt(next_attempt.get("started_at"))
                for next_attempt in attempts[index + 1 :]
                if _dt(next_attempt.get("started_at"))
                and _dt(next_attempt.get("started_at")) >= ended
            ),
            None,
        )
        if next_started:
            red_recovery_minutes += max(
                0.0,
                (next_started - ended).total_seconds() / 60.0,
            )
            recovered_red_attempts += 1

    metrics = {
        "commit_to_pr_minutes": _minutes(first_commit_at, pr_created_at),
        "pr_to_green_minutes": _minutes(pr_created_at, green_at),
        "green_to_merge_minutes": _minutes(green_at, merged_at),
        "total_observed_minutes": _minutes(first_commit_at, merged_at),
        "pr_to_merge_minutes": _minutes(pr_created_at, merged_at),
        "validation_attempts": len(attempts),
        "red_attempts": sum(1 for attempt in attempts if attempt.get("state") == "red"),
        "red_recovery_minutes": (
            round(red_recovery_minutes, 1) if recovered_red_attempts else None
        ),
        "recovered_red_attempts": recovered_red_attempts,
    }
    bottleneck = _bottleneck(metrics)

    diagnostics: list[str] = []
    if bottleneck:
        diagnostics.append(
            f"Principal ralentissement observable : {bottleneck['label']} "
            f"({bottleneck['minutes']} min)."
        )
    if metrics["validation_attempts"]:
        diagnostics.append(
            f"{metrics['validation_attempts']} tentative(s) de validation, "
            f"dont {metrics['red_attempts']} rouge(s)."
        )
    else:
        diagnostics.append(
            "Métrique CI indisponible : aucun workflow GitHub Actions observable sur les SHA analysés."
        )
    if metrics["red_attempts"] and metrics["red_recovery_minutes"] is None:
        diagnostics.append(
            "Temps de récupération rouge indisponible : aucune tentative suivante horodatée n'est observable."
        )
    if not first_commit_at:
        diagnostics.append(
            "Début observable indisponible : aucun timestamp de commit exploitable dans la PR."
        )
    diagnostics.extend(run_errors[:3])
    diagnostics.extend(notes)

    required = [
        metrics["commit_to_pr_minutes"],
        metrics["pr_to_green_minutes"],
        metrics["green_to_merge_minutes"],
        metrics["total_observed_minutes"],
    ]
    status = "complete" if all(value is not None for value in required) else "partial"

    return {
        **base,
        "status": status,
        "reason": None,
        "pr": {
            "number": details.get("number"),
            "title": details.get("title"),
            "url": details.get("html_url"),
            "head": (details.get("head") or {}).get("ref"),
            "created_at": details.get("created_at"),
            "merged_at": details.get("merged_at"),
        },
        "first_commit_at": _iso(first_commit_at),
        "first_green_at": _iso(green_at),
        "metrics": metrics,
        "bottleneck": bottleneck,
        "attempts": attempts,
        "timeline": _timeline(
            first_commit_at=first_commit_at,
            pr=details,
            attempts=attempts,
            merged_at=merged_at,
        ),
        "diagnostics": diagnostics,
    }


def _trend(deliveries: list[dict[str, Any]]) -> dict[str, Any] | None:
    comparable = [
        delivery
        for delivery in deliveries
        if delivery.get("metrics", {}).get("pr_to_green_minutes") is not None
        and delivery.get("pr", {}).get("merged_at")
    ]
    if len(comparable) < 6:
        return None
    comparable.sort(key=lambda item: str(item["pr"]["merged_at"]))
    half = len(comparable) // 2
    older = comparable[:half]
    recent = comparable[-half:]
    older_median = _median(
        [row["metrics"]["pr_to_green_minutes"] for row in older]
    )
    recent_median = _median(
        [row["metrics"]["pr_to_green_minutes"] for row in recent]
    )
    if older_median is None or recent_median is None:
        return None
    delta = round(recent_median - older_median, 1)
    if delta < 0:
        description = (
            f"La médiane PR → CI verte est passée de {older_median} à {recent_median} min "
            f"sur la moitié récente ({abs(delta)} min plus rapide)."
        )
    elif delta > 0:
        description = (
            f"La médiane PR → CI verte est passée de {older_median} à {recent_median} min "
            f"sur la moitié récente ({delta} min plus lente)."
        )
    else:
        description = (
            f"La médiane PR → CI verte est stable à {recent_median} min "
            "entre les deux moitiés observées."
        )
    return {
        "metric": "pr_to_green_minutes",
        "older_median": older_median,
        "recent_median": recent_median,
        "delta_minutes": delta,
        "sample_size": len(comparable),
        "description": description,
    }


async def build_flow_analytics(
    client: GitHubClient,
    repo: str,
    pipeline_issue: int,
    limit: int = 12,
    *,
    strict_source: bool = False,
) -> dict[str, Any]:
    repo = client.validate_repo(repo)
    limit = max(3, min(int(limit), 20))

    source, contract = await load_pipeline_source(
        client,
        repo,
        pipeline_issue,
        strict=strict_source,
        validate_references=strict_source,
    )
    closed_pulls = await client.list_pulls(repo, "closed", 100)
    if contract.source != "canonical_v1":
        return {
            "status": "unavailable",
            "repo": repo,
            "pipeline_issue": pipeline_issue,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reason": "Flow Analytics exige COCKPIT_PIPELINE_V1.",
            "summary": None,
            "deliveries": [],
            "notes": [
                "Aucune métrique historique n'est déduite du Markdown legacy."
            ],
        }
    if not contract.valid:
        return {
            "status": "unavailable",
            "repo": repo,
            "pipeline_issue": pipeline_issue,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reason": f"Le pipeline canonique #{pipeline_issue} est invalide.",
            "summary": None,
            "deliveries": [],
            "notes": list(contract.errors),
        }

    done_steps = [
        step
        for step in contract.steps
        if step.kind == PIPELINE_WORK
        and step.status == PIPELINE_STATUS_DONE
    ]

    ranked: list[tuple[str, Any, list[dict[str, Any]]]] = []
    for step in done_steps:
        candidates = _candidate_prs(step, closed_pulls)
        latest_merge = str((candidates[0] if candidates else {}).get("merged_at") or "")
        ranked.append((latest_merge, step, candidates))
    ranked.sort(key=lambda row: row[0], reverse=True)

    delivery_semaphore = asyncio.Semaphore(4)
    run_semaphore = asyncio.Semaphore(8)
    rows = await asyncio.gather(
        *[
            _delivery_row(
                client=client,
                repo=repo,
                step=step,
                candidates=candidates,
                delivery_semaphore=delivery_semaphore,
                run_semaphore=run_semaphore,
            )
            for _, step, candidates in ranked[:limit]
        ]
    )

    deliveries = sorted(
        rows,
        key=lambda row: str((row.get("pr") or {}).get("merged_at") or ""),
        reverse=True,
    )
    analyzable = [row for row in deliveries if row.get("pr")]
    unavailable = [row for row in deliveries if not row.get("pr")]

    summary = {
        "requested_limit": limit,
        "delivery_count": len(deliveries),
        "analyzable_count": len(analyzable),
        "unavailable_count": len(unavailable),
        "complete_count": sum(1 for row in deliveries if row.get("status") == "complete"),
        "partial_count": sum(1 for row in deliveries if row.get("status") == "partial"),
        "median_total_observed_minutes": _median(
            [row.get("metrics", {}).get("total_observed_minutes") for row in analyzable]
        ),
        "median_commit_to_pr_minutes": _median(
            [row.get("metrics", {}).get("commit_to_pr_minutes") for row in analyzable]
        ),
        "median_pr_to_green_minutes": _median(
            [row.get("metrics", {}).get("pr_to_green_minutes") for row in analyzable]
        ),
        "median_green_to_merge_minutes": _median(
            [row.get("metrics", {}).get("green_to_merge_minutes") for row in analyzable]
        ),
        "average_validation_attempts": _average(
            [row.get("metrics", {}).get("validation_attempts") for row in analyzable]
        ),
        "average_red_attempts": _average(
            [row.get("metrics", {}).get("red_attempts") for row in analyzable]
        ),
        "trend": _trend(analyzable),
    }

    status = (
        "unavailable"
        if not analyzable
        else "partial"
        if unavailable or any(row.get("status") != "complete" for row in analyzable)
        else "complete"
    )
    return {
        "status": status,
        "repo": repo,
        "pipeline_issue": pipeline_issue,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reason": None,
        "summary": summary,
        "deliveries": deliveries,
        "notes": [
            "Les durées utilisent uniquement des timestamps GitHub observables; aucune date de création de branche n'est inventée.",
            "Le temps historique de stall et le délai exact de réconciliation #55 ne sont pas reconstruits : l'API actuelle ne fournit pas une chronologie fiable de ces états.",
            "Une tentative de validation correspond à un SHA de PR possédant au moins un workflow GitHub Actions.",
        ],
    }

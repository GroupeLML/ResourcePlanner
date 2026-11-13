from __future__ import annotations

import asyncio
from dataclasses import replace
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .config import Settings
from .attention import build_attention_center
from .derive import build_next_action_and_prompt, commit_summary, derive_states, matches_work_key
from .execution import build_execution_control
from .handoff import build_handoff_packs
from .github import GitHubClient, GitHubError
from .pipeline_source import load_pipeline_source
from .roadmap import (
    active_block,
    agents_allow_chaining,
    extract_declared_active,
    first_unfinished,
    focus_items,
    has_explicit_block_order,
    merge_pipeline_work_status,
    merge_subitems,
    numeric_issue,
    PIPELINE_MAIN,
    PIPELINE_PARALLEL,
    PIPELINE_STATUS_BLOCKED,
    PIPELINE_STATUS_DONE,
    PIPELINE_STATUS_READY,
    PIPELINE_WORK,
    PipelineStep,
    pipeline_window,
    render_cockpit_pipeline,
    resolve_product_pipeline,
    referenced_adrs,
    referenced_issue_numbers,
    subitems_from_text,
    top_level_items,
)


def _issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "url": issue.get("html_url"),
        "updated_at": issue.get("updated_at"),
    }


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "url": job.get("html_url"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
    }


def _run_summary(run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "url": run.get("html_url"),
        "run_number": run.get("run_number"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "jobs": [_job_summary(job) for job in jobs],
    }


async def _runs_for_sha(
    client: GitHubClient,
    repo: str,
    sha: str | None,
) -> list[dict[str, Any]]:
    if not sha:
        return []
    runs_raw = await client.workflow_runs_for_sha(repo, sha, per_page=5)
    jobs_by_run = await asyncio.gather(
        *[client.run_jobs(repo, int(run["id"])) for run in runs_raw[:5]],
        return_exceptions=True,
    )
    runs: list[dict[str, Any]] = []
    for run, jobs in zip(runs_raw[:5], jobs_by_run):
        runs.append(_run_summary(run, jobs if isinstance(jobs, list) else []))
    return runs


async def _pr_summary(client: GitHubClient, repo: str, pr: dict[str, Any]) -> dict[str, Any]:
    details = await client.get_pull(repo, int(pr["number"]))
    head = details.get("head") or {}
    sha = head.get("sha")
    runs = await _runs_for_sha(client, repo, sha)
    return {
        "number": details.get("number"),
        "title": details.get("title"),
        "body": details.get("body") or "",
        "state": details.get("state"),
        "merged": bool(details.get("merged")),
        "draft": bool(details.get("draft")),
        "mergeable": details.get("mergeable"),
        "mergeable_state": details.get("mergeable_state"),
        "url": details.get("html_url"),
        "head": head.get("ref"),
        "head_sha": sha,
        "base": (details.get("base") or {}).get("ref"),
        "created_at": details.get("created_at"),
        "updated_at": details.get("updated_at"),
        "merged_at": details.get("merged_at"),
        "runs": runs,
    }


def _branch_summary(repo: str, branch: dict[str, Any] | None) -> dict[str, Any] | None:
    if not branch:
        return None
    name = branch.get("name")
    commit = branch.get("commit") or {}
    if not name:
        return None
    return {
        "name": name,
        "sha": commit.get("sha"),
        "url": f"https://github.com/{repo}/tree/{quote(name, safe='/')}",
    }


async def _most_recent_matching_branch(
    client: GitHubClient,
    repo: str,
    branches: list[dict[str, Any]],
    key: str,
) -> dict[str, Any] | None:
    candidates = [
        branch
        for branch in branches
        if branch.get("name") != "main"
        and matches_work_key(str(branch.get("name") or ""), key)
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    commits = await asyncio.gather(
        *[
            client.get_commit(repo, str((branch.get("commit") or {}).get("sha")))
            for branch in candidates
            if (branch.get("commit") or {}).get("sha")
        ],
        return_exceptions=True,
    )
    scored: list[tuple[str, str, dict[str, Any]]] = []
    commit_index = 0
    for branch in candidates:
        sha = (branch.get("commit") or {}).get("sha")
        info = None
        if sha:
            commit = commits[commit_index]
            commit_index += 1
            if isinstance(commit, dict):
                info = commit_summary(commit)
        scored.append(
            (
                str((info or {}).get("date") or ""),
                str(branch.get("name") or ""),
                branch,
            )
        )
    return max(scored, key=lambda row: (row[0], row[1]))[2]


def _pipeline_next_action_and_prompt(
    *,
    pipeline_now: dict[str, Any] | None,
    block_done: bool,
    active_key: str,
    roadmap_issue: int,
) -> tuple[str, str] | None:
    if not pipeline_now:
        return None

    kind = str(pipeline_now.get("kind") or "")
    title = str(pipeline_now.get("title") or pipeline_now.get("key") or "étape suivante")
    key = str(pipeline_now.get("key") or "")

    if kind == "ARCHITECTURE_GATE":
        return (
            f"Gate d'architecture requise : {title}.",
            (
                "Aucune tranche DEV ne doit être démarrée à cette étape. "
                f"Le pipeline GitHub #{roadmap_issue} exige d'abord la gate d'architecture « {title} ». "
                f"Attends qu'elle soit explicitement documentée comme satisfaite dans #{roadmap_issue}; "
                "ne l'exécute pas comme une tranche d'implémentation."
            ),
        )

    if kind == "ENVIRONMENT_GATE":
        return (
            f"Gate environnementale requise : {title}.",
            (
                "Aucune tranche DEV produit ne doit être inventée à cette étape. "
                f"Le pipeline GitHub #{roadmap_issue} indique la gate environnementale « {title} ». "
                f"Son état doit rester porté par GitHub/#{roadmap_issue}; "
                "ne transforme pas cette validation d'environnement en travail applicatif implicite."
            ),
        )

    if block_done and kind == "WORK" and key and key != active_key:
        return (
            f"Prochaine tranche DEV du pipeline : {title}.",
            (
                f"Le bloc actif précédent est terminé. Le pipeline GitHub #{roadmap_issue} "
                f"place maintenant « {title} » comme prochain travail DEV. "
                "Synchronise l'issue et le roadmap si nécessaire, puis poursuis selon AGENTS.md "
                "sans sauter une gate précédente."
            ),
        )

    return None


def _documentation_only_pull(files: list[dict[str, Any]]) -> bool:
    if not files:
        return False
    for entry in files:
        path = str(entry.get("filename") or "").strip()
        if not path:
            return False
        if path.startswith("docs/") or path.lower().endswith(".md"):
            continue
        return False
    return True


async def _pull_is_dev_work(
    client: GitHubClient,
    repo: str,
    pr: dict[str, Any],
) -> bool:
    number = pr.get("number")
    if not isinstance(number, int):
        return True
    try:
        files = await client.list_pull_files(repo, number)
    except GitHubError:
        # Fail conservative: an unavailable files projection must not hide
        # a real implementation PR from the Developer workflow.
        return True
    return not _documentation_only_pull(files)


async def _first_matching_dev_pr(
    client: GitHubClient,
    repo: str,
    prs: list[dict[str, Any]],
    key: str,
    *,
    strict_identity: bool = False,
) -> dict[str, Any] | None:
    for pr in prs:
        matches = (
            _matches_delivery_key(pr, key)
            if strict_identity
            else matches_work_key(pr, key)
        )
        if not matches:
            continue
        if await _pull_is_dev_work(client, repo, pr):
            return pr
    return None


def _merged_pr_summary(pr: dict[str, Any] | None) -> dict[str, Any] | None:
    if not pr:
        return None
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "merged_at": pr.get("merged_at"),
    }


def _delivery_run_state(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "unknown"
    if any(str(run.get("status") or "").lower() != "completed" for run in runs):
        return "pending"
    conclusions = [str(run.get("conclusion") or "").lower() for run in runs]
    if any(value not in {"success", "neutral", "skipped"} for value in conclusions):
        return "failed"
    return "green" if any(value == "success" for value in conclusions) else "unknown"


def _pull_evidence(pr: dict[str, Any], *, run_state: str | None = None) -> dict[str, Any]:
    suffix = ""
    if run_state == "green":
        suffix = " · CI verte"
    elif run_state == "failed":
        suffix = " · CI non verte"
    elif run_state == "pending":
        suffix = " · CI en cours"
    elif run_state == "unknown":
        suffix = " · CI non vérifiable"
    return {
        "kind": "pull_request",
        "label": f"PR #{pr.get('number')}{suffix}",
        "url": pr.get("url") or pr.get("html_url"),
        "state": run_state,
    }


def _matches_delivery_key(pr: dict[str, Any], key: str) -> bool:
    title = str(pr.get("title") or "")
    head_raw = pr.get("head")
    if isinstance(head_raw, dict):
        head = str(head_raw.get("ref") or "")
    else:
        head = str(head_raw or "")
    if matches_work_key(title, key) or matches_work_key(head, key):
        return True

    body = str(pr.get("body") or "")
    explicit_reference = re.compile(
        rf"(?im)^\s*(?:refs?|closes?|fixes?|slice|tranche)\s*:?[ \t]+#?{re.escape(key)}(?![A-Z0-9])"
    )
    return bool(explicit_reference.search(body))


async def _matching_dev_pr_summary(
    client: GitHubClient,
    repo: str,
    prs: list[dict[str, Any]],
    key: str,
    *,
    merged_only: bool,
) -> dict[str, Any] | None:
    for pr in prs:
        if merged_only and not pr.get("merged_at"):
            continue
        if not _matches_delivery_key(pr, key):
            continue
        if not await _pull_is_dev_work(client, repo, pr):
            continue
        return await _pr_summary(client, repo, pr)
    return None


def _proposed_pipeline(
    steps: list[PipelineStep],
    *,
    completed_ready_keys: set[str],
) -> tuple[list[PipelineStep], list[dict[str, str]]]:
    proposed = list(steps)
    changes: list[dict[str, str]] = []

    main_ready_index = next(
        (
            index
            for index, step in enumerate(proposed)
            if step.lane == PIPELINE_MAIN
            and step.status == PIPELINE_STATUS_READY
        ),
        None,
    )
    main_ready_completed = bool(
        main_ready_index is not None
        and proposed[main_ready_index].key in completed_ready_keys
    )

    for index, step in enumerate(proposed):
        if (
            step.lane == PIPELINE_PARALLEL
            and step.status == PIPELINE_STATUS_READY
            and step.key in completed_ready_keys
        ):
            proposed[index] = replace(
                step,
                done=True,
                marker="✅",
                status=PIPELINE_STATUS_DONE,
            )
            changes.append(
                {"key": step.key, "from": PIPELINE_STATUS_READY, "to": PIPELINE_STATUS_DONE}
            )

    if main_ready_completed and main_ready_index is not None:
        current = proposed[main_ready_index]
        proposed[main_ready_index] = replace(
            current,
            done=True,
            marker="✅",
            status=PIPELINE_STATUS_DONE,
        )
        changes.append(
            {"key": current.key, "from": PIPELINE_STATUS_READY, "to": PIPELINE_STATUS_DONE}
        )
        next_index = next(
            (
                index
                for index in range(main_ready_index + 1, len(proposed))
                if proposed[index].lane == PIPELINE_MAIN
                and proposed[index].status != PIPELINE_STATUS_DONE
            ),
            None,
        )
        if next_index is not None:
            following = proposed[next_index]
            proposed[next_index] = replace(
                following,
                done=False,
                marker=None,
                status=PIPELINE_STATUS_READY,
            )
            changes.append(
                {
                    "key": following.key,
                    "from": str(following.status or PIPELINE_STATUS_BLOCKED),
                    "to": PIPELINE_STATUS_READY,
                }
            )

    return proposed, changes


async def _reconcile_canonical_pipeline(
    client: GitHubClient,
    repo: str,
    *,
    pipeline_contract: Any,
    pipeline_issue: int = 55,
    open_raw: list[dict[str, Any]],
    closed_raw: list[dict[str, Any]],
) -> dict[str, Any]:
    if pipeline_contract.source != "canonical_v1":
        return {
            "status": "legacy",
            "summary": "Reconciliation disponible seulement avec COCKPIT_PIPELINE_V1.",
            "findings": [],
            "proposal": None,
        }
    if not pipeline_contract.valid:
        return {
            "status": "invalid",
            "summary": "Pipeline canonique invalide; aucune reconciliation GitHub n'est inferee.",
            "findings": [],
            "proposal": None,
        }

    steps: list[PipelineStep] = pipeline_contract.steps
    work_steps = [
        step
        for step in steps
        if step.kind == PIPELINE_WORK and step.status != PIPELINE_STATUS_DONE
    ]
    findings: list[dict[str, Any]] = []
    completed_ready_keys: set[str] = set()

    for step in work_steps:
        open_pr, merged_pr = await asyncio.gather(
            _matching_dev_pr_summary(
                client,
                repo,
                open_raw,
                step.key,
                merged_only=False,
            ),
            _matching_dev_pr_summary(
                client,
                repo,
                closed_raw,
                step.key,
                merged_only=True,
            ),
        )
        if step.status == PIPELINE_STATUS_READY and merged_pr:
            run_state = _delivery_run_state(merged_pr.get("runs") or [])
            if run_state == "green":
                completed_ready_keys.add(step.key)
                findings.append(
                    {
                        "code": "ready_merged_green",
                        "severity": "stale",
                        "key": step.key,
                        "message": (
                            f"{step.key} est encore READY dans #{pipeline_issue}, mais une PR DEV correspondante "
                            "est fusionnee et ses workflows observes sont verts."
                        ),
                        "evidence": [_pull_evidence(merged_pr, run_state=run_state)],
                    }
                )
            else:
                findings.append(
                    {
                        "code": "ready_merged_unverified",
                        "severity": "attention",
                        "key": step.key,
                        "message": (
                            f"{step.key} est READY et une PR DEV est fusionnee, mais la CI "
                            "n'est pas entierement verte/verifiable; aucune promotion n'est proposee."
                        ),
                        "evidence": [_pull_evidence(merged_pr, run_state=run_state)],
                    }
                )

        if step.status == PIPELINE_STATUS_BLOCKED and open_pr:
            findings.append(
                {
                    "code": "blocked_open_pr",
                    "severity": "attention",
                    "key": step.key,
                    "message": (
                        f"{step.key} est BLOCKED dans la lane {step.lane}, mais une PR DEV ouverte "
                        "lui correspond. Le cockpit conserve l'ordre canonique."
                    ),
                    "evidence": [_pull_evidence(open_pr)],
                }
            )

        if step.status == PIPELINE_STATUS_BLOCKED and merged_pr:
            run_state = _delivery_run_state(merged_pr.get("runs") or [])
            findings.append(
                {
                    "code": "blocked_merged_pr",
                    "severity": "attention",
                    "key": step.key,
                    "message": (
                        f"{step.key} est BLOCKED dans #{pipeline_issue}, mais une PR DEV correspondante est deja fusionnee. "
                        "Aucune reorganisation automatique du pipeline n'est effectuee."
                    ),
                    "evidence": [_pull_evidence(merged_pr, run_state=run_state)],
                }
            )

    proposed_steps, changes = _proposed_pipeline(
        steps,
        completed_ready_keys=completed_ready_keys,
    )
    proposal = (
        {
            "changes": changes,
            "pipeline_block": render_cockpit_pipeline(proposed_steps),
        }
        if changes
        else None
    )

    status = (
        "stale"
        if changes
        else "attention"
        if findings
        else "coherent"
    )
    summary = {
        "stale": f"Le contrat #{pipeline_issue} semble en retard sur une livraison GitHub verifiee.",
        "attention": "Des ecarts GitHub meritent une verification sans changer automatiquement l'ordre.",
        "coherent": "Aucun ecart actionnable detecte entre le pipeline canonique et GitHub.",
    }[status]
    return {
        "status": status,
        "summary": summary,
        "findings": findings,
        "proposal": proposal,
    }


def _architecture_entries(architecture_raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": entry.get("name"),
            "url": entry.get("html_url"),
        }
        for entry in architecture_raw
        if str(entry.get("name") or "").startswith("ADR-")
        and str(entry.get("name") or "").endswith(".md")
    ]


def _dashboard_without_active_work(
    *,
    repo: str,
    settings: Settings,
    roadmap_raw: dict[str, Any],
    pipeline_raw: dict[str, Any],
    latest_repo_commit: dict[str, Any],
    architecture_raw: list[dict[str, Any]],
    top_items: list[Any],
    legacy_declared_key: str | None,
    pipeline_contract: Any,
    pipeline_projection: dict[str, Any],
    agents_text: str,
) -> dict[str, Any]:
    invalid = not pipeline_contract.valid
    errors = list(pipeline_contract.errors)
    if invalid:
        next_action = (
            f"Pipeline canonique GitHub #{settings.canonical_pipeline_issue} invalide — corriger COCKPIT_PIPELINE_V1."
        )
        dev_prompt = (
            f"Le bloc COCKPIT_PIPELINE_V1 de la source machine GitHub #{settings.canonical_pipeline_issue} est invalide. "
            "Ne démarre, ne reprends et n'associe aucune tranche DEV tant que le contrat n'est pas corrigé. "
            + " ".join(errors)
        )
        warnings = [
            "Pipeline canonique invalide : " + error
            for error in errors
        ]
    else:
        next_action = (
            f"Le pipeline canonique GitHub #{settings.canonical_pipeline_issue} ne contient aucune étape MAIN active."
        )
        dev_prompt = (
            f"Aucune tranche DEV active n'est déclarée dans COCKPIT_PIPELINE_V1 de "
            f"GitHub #{settings.canonical_pipeline_issue}. Ne déduis pas une prochaine tranche depuis le texte humain."
        )
        warnings = []

    adr_entries = _architecture_entries(architecture_raw)
    reconciliation = {
        "status": "invalid" if invalid else "coherent",
        "summary": (
            "Pipeline canonique invalide; la reconciliation GitHub est suspendue."
            if invalid
            else "Aucune etape MAIN active; aucun ecart actionnable detecte."
        ),
        "findings": [],
        "proposal": None,
    }
    execution = build_execution_control(
        roadmap_issue=settings.canonical_pipeline_issue,
        roadmap_url=pipeline_raw.get("html_url"),
        roadmap_updated_at=pipeline_raw.get("updated_at"),
        pipeline_valid=pipeline_contract.valid,
        pipeline_now=pipeline_projection.get("now"),
        reconciliation=reconciliation,
        active_work=None,
        fallback_next_action=next_action,
        fallback_dev_prompt=dev_prompt,
    )
    handoff = build_handoff_packs(
        roadmap_issue=settings.roadmap_issue,
        roadmap_url=roadmap_raw.get("html_url"),
        canonical_pipeline_issue=settings.canonical_pipeline_issue,
        canonical_pipeline_url=pipeline_raw.get("html_url"),
        pipeline_now=pipeline_projection.get("now"),
        execution=execution,
        active_work=None,
        issue_body="",
        referenced_adrs=[],
        agents_text=agents_text,
    )
    attention = build_attention_center(
        execution=execution,
        reconciliation=reconciliation,
        handoff=handoff,
        active_work=None,
        pipeline_now=pipeline_projection.get("now"),
    )
    return {
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "roadmap_issue": settings.roadmap_issue,
            "canonical_pipeline_issue": settings.canonical_pipeline_issue,
            "migration_candidate_pipeline_issue": settings.migration_candidate_pipeline_issue,
            "stalled_after_minutes": settings.stalled_after_minutes,
            "token_configured": bool(settings.github_token),
        },
        "pipeline": {
            "steps": [step.to_dict() for step in pipeline_contract.steps],
            "valid": pipeline_contract.valid,
            "source": pipeline_contract.source,
            "errors": errors,
            **pipeline_projection,
        },
        "pipeline_source": {
            "number": pipeline_raw.get("number") or settings.canonical_pipeline_issue,
            "title": pipeline_raw.get("title"),
            "url": pipeline_raw.get("html_url"),
            "updated_at": pipeline_raw.get("updated_at"),
            "strict": settings.canonical_pipeline_issue != settings.roadmap_issue,
        },
        "reconciliation": reconciliation,
        "execution": execution,
        "handoff": handoff,
        "attention": attention,
        "roadmap": {
            "number": roadmap_raw.get("number"),
            "title": roadmap_raw.get("title"),
            "url": roadmap_raw.get("html_url"),
            "updated_at": roadmap_raw.get("updated_at"),
            "declared_active": legacy_declared_key,
            "active_issue": None,
            "effective_active": None,
            "items": [item.to_dict() for item in top_items],
        },
        "active_work": None,
        "architecture": {
            "path": "docs/architecture/",
            "url": f"https://github.com/{repo}/tree/main/docs/architecture",
            "adrs": adr_entries,
            "referenced_adrs": [],
        },
        "open_prs": [],
        "related_issues": [],
        "latest_commit": commit_summary(latest_repo_commit),
        "next_action": next_action,
        "dev_prompt": dev_prompt,
        "warnings": warnings,
    }


async def build_dashboard(client: GitHubClient, settings: Settings, repo: str) -> dict[str, Any]:
    repo = client.validate_repo(repo)
    roadmap_raw, open_raw, closed_raw, latest_repo_commit, agents_text, architecture_raw, branches_raw = await asyncio.gather(
        client.get_issue(repo, settings.roadmap_issue),
        client.list_pulls(repo, "open", 30),
        client.list_pulls(repo, "closed", 50),
        client.latest_commit(repo),
        client.get_text_file(repo, "AGENTS.md"),
        client.list_directory(repo, "docs/architecture"),
        client.list_all_branches(repo, per_page=100),
    )

    roadmap_body = roadmap_raw.get("body") or ""
    top_items = top_level_items(roadmap_body)
    legacy_declared_key = extract_declared_active(roadmap_body)
    if settings.canonical_pipeline_issue == settings.roadmap_issue:
        pipeline_raw = roadmap_raw
        pipeline_contract = resolve_product_pipeline(roadmap_body)
    else:
        pipeline_raw, pipeline_contract = await load_pipeline_source(
            client,
            repo,
            settings.canonical_pipeline_issue,
            strict=True,
            validate_references=True,
        )
    pipeline_steps = pipeline_contract.steps
    pipeline_projection = (
        pipeline_window(pipeline_steps)
        if pipeline_contract.valid
        else {
            "completed_count": 0,
            "now": None,
            "parallel": [],
            "next": [],
            "later": [],
        }
    )
    pipeline_now = pipeline_projection.get("now")

    if (not pipeline_contract.valid) or (
        pipeline_contract.present and pipeline_now is None
    ):
        return _dashboard_without_active_work(
            repo=repo,
            settings=settings,
            roadmap_raw=roadmap_raw,
            pipeline_raw=pipeline_raw,
            latest_repo_commit=latest_repo_commit,
            architecture_raw=architecture_raw,
            top_items=top_items,
            legacy_declared_key=legacy_declared_key,
            pipeline_contract=pipeline_contract,
            pipeline_projection=pipeline_projection,
            agents_text=agents_text,
        )

    canonical_mode = pipeline_contract.present
    if canonical_mode:
        parent_issue = int(pipeline_now.get("issue_number"))
        declared_key = str(pipeline_now.get("key"))
    else:
        pipeline_work_key = (
            str(pipeline_now.get("key"))
            if pipeline_now and pipeline_now.get("kind") == "WORK"
            else None
        )
        declared_key = pipeline_work_key or legacy_declared_key
        declared_parent = numeric_issue(declared_key or "")
        if declared_parent is None:
            first_top = first_unfinished(top_items)
            declared_parent = (
                numeric_issue(first_top.key)
                if first_top
                else settings.roadmap_issue
            )
        parent_issue = declared_parent or settings.roadmap_issue

    active_issue_raw = await client.get_issue(repo, parent_issue)
    issue_body = active_issue_raw.get("body") or ""
    roadmap_block = active_block(roadmap_body, parent_issue)
    issue_subitems = subitems_from_text(issue_body, parent_issue)
    roadmap_subitems = subitems_from_text(roadmap_block, parent_issue)
    subitems = merge_subitems(issue_subitems, roadmap_subitems)
    if not canonical_mode:
        pipeline_steps = merge_pipeline_work_status(pipeline_steps, subitems)
        pipeline_projection = pipeline_window(pipeline_steps)
        pipeline_now = pipeline_projection.get("now")

    pipeline_active_key = (
        str(pipeline_now.get("key"))
        if (
            pipeline_now
            and pipeline_now.get("kind") == "WORK"
            and pipeline_now.get("issue_number") == parent_issue
        )
        else None
    )

    parent_item = next((item for item in top_items if item.key == str(parent_issue)), None)
    if canonical_mode:
        block_done = False
        active_key = str(pipeline_now.get("key"))
        active_subitem = (
            next(
                (item for item in subitems if item.key == active_key),
                None,
            )
            if pipeline_now.get("kind") == "WORK"
            and active_key != str(parent_issue)
            else None
        )
    else:
        block_done = bool(
            active_issue_raw.get("state") == "closed"
            or (subitems and all(item.done for item in subitems))
            or (parent_item and parent_item.done)
        )
        if block_done:
            active_subitem = None
        elif pipeline_active_key and pipeline_active_key != str(parent_issue):
            active_subitem = next(
                (item for item in subitems if item.key == pipeline_active_key),
                first_unfinished(subitems),
            )
        else:
            active_subitem = first_unfinished(subitems)
        active_key = (
            pipeline_active_key
            if pipeline_active_key and not block_done
            else active_subitem.key if active_subitem else str(parent_issue)
        )
    explicit_in_progress = (
        False
        if canonical_mode
        else bool(
            (
                active_subitem
                and (
                    active_subitem.marker == "🟡"
                    or "en cours" in active_subitem.title.lower()
                )
            )
            or (
                active_subitem is None
                and parent_item
                and parent_item.marker == "🟡"
            )
        )
    )

    open_prs = await asyncio.gather(*[_pr_summary(client, repo, pr) for pr in open_raw[:12]]) if open_raw else []
    primary_pr = await _first_matching_dev_pr(
        client,
        repo,
        open_prs,
        active_key,
        strict_identity=canonical_mode,
    )
    if not canonical_mode and not primary_pr and active_subitem is None:
        primary_pr = await _first_matching_dev_pr(
            client,
            repo,
            open_prs,
            str(parent_issue),
        )

    merged_but_unmarked_raw = None
    has_active_work_identity = bool(
        (canonical_mode and pipeline_now and pipeline_now.get("kind") == "WORK")
        or (not canonical_mode and active_subitem)
    )
    if not block_done and not primary_pr and has_active_work_identity:
        merged_candidates = [
            pr
            for pr in closed_raw
            if pr.get("merged_at")
        ]
        merged_but_unmarked_raw = await _first_matching_dev_pr(
            client,
            repo,
            merged_candidates,
            active_key,
            strict_identity=canonical_mode,
        )
    merged_but_unmarked = _merged_pr_summary(merged_but_unmarked_raw)

    branch_raw = None
    if primary_pr:
        branch_raw = next((branch for branch in branches_raw if branch.get("name") == primary_pr.get("head")), None)
    if not branch_raw and not block_done:
        branch_raw = await _most_recent_matching_branch(
            client,
            repo,
            branches_raw,
            active_key,
        )
    active_branch = _branch_summary(repo, branch_raw)
    if not active_branch and primary_pr and primary_pr.get("head"):
        active_branch = {
            "name": primary_pr.get("head"),
            "sha": primary_pr.get("head_sha"),
            "url": f"https://github.com/{repo}/tree/{quote(str(primary_pr.get('head')), safe='/')}",
        }

    active_sha = (primary_pr or {}).get("head_sha") or (active_branch or {}).get("sha")
    active_commit = await client.get_commit(repo, active_sha) if active_sha else None
    active_commit_info = commit_summary(active_commit)
    active_runs = (
        (primary_pr or {}).get("runs") or []
        if primary_pr
        else await _runs_for_sha(client, repo, active_sha)
    )

    blocked_by_roadmap = (
        False
        if canonical_mode
        else bool(
            (active_subitem and (active_subitem.marker == "⏳" or "bloqu" in active_subitem.title.lower()))
            or (parent_item and parent_item.marker == "⏳")
        )
    )
    derived = derive_states(
        block_done=block_done,
        primary_pr=primary_pr,
        active_branch=active_branch,
        active_commit_date=(active_commit_info or {}).get("date"),
        stalled_after_minutes=settings.stalled_after_minutes,
        active_runs=active_runs,
        explicit_in_progress=explicit_in_progress,
        issue_updated_at=active_issue_raw.get("updated_at"),
        roadmap_updated_at=roadmap_raw.get("updated_at"),
        blocked_by_roadmap=blocked_by_roadmap,
    )

    if canonical_mode:
        current_index = next(
            (
                index
                for index, step in enumerate(pipeline_steps)
                if step.key == active_key and step.lane == "MAIN"
            ),
            None,
        )
        remaining_subitems = (
            [
                step.key
                for step in pipeline_steps[current_index:]
                if (
                    current_index is not None
                    and step.lane == "MAIN"
                    and step.kind == "WORK"
                    and step.issue_number == parent_issue
                    and not step.done
                )
            ]
            if current_index is not None
            else [active_key]
        )
        can_chain_block = bool(
            len(remaining_subitems) > 1
            and agents_allow_chaining(agents_text)
        )
    else:
        remaining_subitems = [item.key for item in subitems if not item.done]
        can_chain_block = bool(
            not block_done
            and has_explicit_block_order(issue_body, roadmap_block, subitems)
            and agents_allow_chaining(agents_text)
        )

    next_action, dev_prompt = build_next_action_and_prompt(
        parent_issue=parent_issue,
        active_key=active_key,
        block_done=block_done,
        can_chain_block=can_chain_block,
        primary_pr=primary_pr,
        active_branch=active_branch,
        derived=derived,
        roadmap_issue=settings.roadmap_issue,
        merged_but_unmarked_pr=merged_but_unmarked,
        remaining_subitems=remaining_subitems,
    )
    pipeline_prompt = _pipeline_next_action_and_prompt(
        pipeline_now=pipeline_projection.get("now"),
        block_done=block_done,
        active_key=active_key,
        roadmap_issue=settings.canonical_pipeline_issue,
    )
    if pipeline_prompt:
        next_action, dev_prompt = pipeline_prompt

    issue_refs = referenced_issue_numbers(roadmap_body, parent_issue)
    related_raw = await asyncio.gather(
        *[client.get_issue(repo, number) for number in issue_refs if number not in {settings.roadmap_issue, parent_issue}],
        return_exceptions=True,
    )
    related_issues = [
        _issue_summary(issue)
        for issue in related_raw
        if isinstance(issue, dict) and "pull_request" not in issue
    ]

    adr_entries = _architecture_entries(architecture_raw)
    adr_names = [str(entry["name"]) for entry in adr_entries if entry.get("name")]
    referenced = referenced_adrs(issue_body + "\n" + roadmap_block, adr_names)
    referenced_adr_entries = [
        entry
        for entry in adr_entries
        if str(entry.get("name") or "") in referenced
    ]

    reconciliation = await _reconcile_canonical_pipeline(
        client,
        repo,
        pipeline_contract=pipeline_contract,
        pipeline_issue=settings.canonical_pipeline_issue,
        open_raw=open_raw,
        closed_raw=closed_raw,
    )

    active_work_projection = {
        "key": active_key,
        "issue_number": parent_issue,
        "title": (
            str(pipeline_now.get("title"))
            if canonical_mode and pipeline_now
            else active_subitem.title if active_subitem else active_issue_raw.get("title")
        ),
        "issue": _issue_summary(active_issue_raw),
        "subitem_key": (
            active_key
            if (
                canonical_mode
                and pipeline_now
                and pipeline_now.get("kind") == "WORK"
                and active_key != str(parent_issue)
            )
            else active_subitem.key if active_subitem else None
        ),
        "block_done": block_done,
        "can_chain_block": can_chain_block,
        "remaining_subitems": remaining_subitems,
        "primary_pr": primary_pr,
        "active_branch": active_branch,
        "last_commit": active_commit_info,
        "states": derived["states"],
        "stalled": derived["stalled"],
        "stall_level": derived["stall_level"],
        "stalled_details": derived["stalled_details"],
        "failed_jobs": derived["failed_jobs"],
        "explicit_in_progress": explicit_in_progress,
        "active_runs": active_runs,
        "merged_but_unmarked_pr": merged_but_unmarked,
    }
    execution = build_execution_control(
        roadmap_issue=settings.canonical_pipeline_issue,
        roadmap_url=pipeline_raw.get("html_url"),
        roadmap_updated_at=pipeline_raw.get("updated_at"),
        pipeline_valid=pipeline_contract.valid,
        pipeline_now=pipeline_projection.get("now"),
        reconciliation=reconciliation,
        active_work=active_work_projection,
        fallback_next_action=next_action,
        fallback_dev_prompt=dev_prompt,
    )
    handoff = build_handoff_packs(
        roadmap_issue=settings.roadmap_issue,
        roadmap_url=roadmap_raw.get("html_url"),
        canonical_pipeline_issue=settings.canonical_pipeline_issue,
        canonical_pipeline_url=pipeline_raw.get("html_url"),
        pipeline_now=pipeline_projection.get("now"),
        execution=execution,
        active_work=active_work_projection,
        issue_body=issue_body,
        referenced_adrs=referenced_adr_entries,
        agents_text=agents_text,
    )
    attention = build_attention_center(
        execution=execution,
        reconciliation=reconciliation,
        handoff=handoff,
        active_work=active_work_projection,
        pipeline_now=pipeline_projection.get("now"),
    )

    warnings: list[str] = []
    if merged_but_unmarked:
        warnings.append(
            f"La PR #{merged_but_unmarked['number']} correspond à {active_key} et est fusionnée, "
            f"mais {active_key} n'est pas explicitement terminée dans GitHub."
        )

    return {
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "roadmap_issue": settings.roadmap_issue,
            "canonical_pipeline_issue": settings.canonical_pipeline_issue,
            "migration_candidate_pipeline_issue": settings.migration_candidate_pipeline_issue,
            "stalled_after_minutes": settings.stalled_after_minutes,
            "token_configured": bool(settings.github_token),
        },
        "pipeline": {
            "steps": [step.to_dict() for step in pipeline_steps],
            "valid": pipeline_contract.valid,
            "source": pipeline_contract.source,
            "errors": list(pipeline_contract.errors),
            **pipeline_projection,
        },
        "pipeline_source": {
            "number": pipeline_raw.get("number") or settings.canonical_pipeline_issue,
            "title": pipeline_raw.get("title"),
            "url": pipeline_raw.get("html_url"),
            "updated_at": pipeline_raw.get("updated_at"),
            "strict": settings.canonical_pipeline_issue != settings.roadmap_issue,
        },
        "reconciliation": reconciliation,
        "execution": execution,
        "handoff": handoff,
        "attention": attention,
        "roadmap": {
            "number": roadmap_raw.get("number"),
            "title": roadmap_raw.get("title"),
            "url": roadmap_raw.get("html_url"),
            "updated_at": roadmap_raw.get("updated_at"),
            "declared_active": legacy_declared_key,
            "active_issue": parent_issue,
            "effective_active": active_key,
            "items": [item.to_dict() for item in focus_items(top_items, subitems, parent_issue)],
        },
        "active_work": active_work_projection,
        "architecture": {
            "path": "docs/architecture/",
            "url": f"https://github.com/{repo}/tree/main/docs/architecture",
            "adrs": adr_entries,
            "referenced_adrs": referenced,
        },
        "open_prs": open_prs,
        "related_issues": related_issues,
        "latest_commit": commit_summary(latest_repo_commit),
        "next_action": next_action,
        "dev_prompt": dev_prompt,
        "warnings": warnings,
    }

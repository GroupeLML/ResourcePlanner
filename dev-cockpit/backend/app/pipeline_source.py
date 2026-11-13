from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from .github import GitHubClient, GitHubError
from .roadmap import PipelineContract, resolve_canonical_pipeline_source, resolve_product_pipeline

MAX_CANONICAL_PARENT_ISSUES = 100
_REFERENCE_CONCURRENCY = 8


def _missing_source_issue(issue_number: int) -> dict[str, Any]:
    return {
        "number": issue_number,
        "title": None,
        "state": None,
        "body": "",
        "html_url": None,
        "updated_at": None,
    }


async def _reference_errors(
    client: GitHubClient,
    repo: str,
    contract: PipelineContract,
) -> list[str]:
    parents = sorted(
        {
            int(step.issue_number)
            for step in contract.steps
            if step.issue_number is not None
        }
    )
    if len(parents) > MAX_CANONICAL_PARENT_ISSUES:
        return [
            "Source canonique invalide: trop de parents GitHub distincts "
            f"({len(parents)} > {MAX_CANONICAL_PARENT_ISSUES})."
        ]

    semaphore = asyncio.Semaphore(_REFERENCE_CONCURRENCY)

    async def load(number: int) -> tuple[int, dict[str, Any] | Exception]:
        try:
            async with semaphore:
                return number, await client.get_issue(repo, number)
        except Exception as exc:  # fail closed for source-integrity verification
            return number, exc

    rows = await asyncio.gather(*[load(number) for number in parents])
    errors: list[str] = []
    for number, result in rows:
        if isinstance(result, Exception):
            if isinstance(result, GitHubError) and result.status_code == 404:
                errors.append(
                    f"Source canonique invalide: WorkItem parent GitHub #{number} introuvable."
                )
            else:
                errors.append(
                    f"Source canonique non verifiable: impossible de valider le parent GitHub #{number}."
                )
            continue
        if result.get("pull_request"):
            errors.append(
                f"Source canonique invalide: #{number} reference une pull request, pas un WorkItem Issue."
            )
            continue
        if int(result.get("number") or -1) != number:
            errors.append(
                f"Source canonique invalide: le parent GitHub #{number} ne correspond pas a la ressource relue."
            )
    return errors


async def load_pipeline_source(
    client: GitHubClient,
    repo: str,
    issue_number: int,
    *,
    strict: bool,
    validate_references: bool = True,
) -> tuple[dict[str, Any], PipelineContract]:
    try:
        issue = await client.get_issue(repo, issue_number)
    except GitHubError as exc:
        if strict and exc.status_code == 404:
            return (
                _missing_source_issue(issue_number),
                PipelineContract(
                    present=False,
                    valid=False,
                    source="canonical_v1",
                    steps=[],
                    errors=[
                        f"Source canonique invalide: issue machine #{issue_number} introuvable."
                    ],
                ),
            )
        raise

    body = str(issue.get("body") or "")
    contract = (
        resolve_canonical_pipeline_source(body)
        if strict
        else resolve_product_pipeline(body)
    )
    if strict and validate_references and contract.valid:
        errors = await _reference_errors(client, repo, contract)
        if errors:
            contract = replace(contract, valid=False, errors=errors)
    return issue, contract

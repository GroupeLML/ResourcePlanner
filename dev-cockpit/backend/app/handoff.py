from __future__ import annotations

from typing import Any

from .details import markdown_sections


ROLE_LABELS = {
    "product-owner": "Product Owner",
    "developer": "Developer",
    "architect": "Architecte",
    "reviewer": "Reviewer",
    "generic": "Generic",
}


def _active_section(issue_body: str, active_key: str | None) -> dict[str, Any] | None:
    if not issue_body or not active_key:
        return None
    for section in markdown_sections(issue_body):
        if section.get("work_key") == active_key:
            return {
                "title": section.get("title"),
                "content": str(section.get("content") or "")[:6000],
            }
    return None


def _agents_constraints(agents_text: str) -> list[str]:
    checks = [
        (
            "continue autonomously through",
            "Poursuivre le cycle approuvé jusqu'aux tests, PR, CI verte et fusion sauf condition d'arrêt explicite.",
        ),
        (
            "Do not modify a failing test merely to make CI green.",
            "Ne pas affaiblir ou modifier un test valide uniquement pour rendre la CI verte.",
        ),
        (
            "A PR should represent one coherent issue or sub-issue.",
            "Garder la PR centrée sur une issue ou sous-tranche cohérente.",
        ),
        (
            "Always synchronize with ",
            "Synchroniser avec main après la fusion précédente avant d'enchaîner la sous-tranche suivante.",
        ),
        (
            "Do not automatically consume arbitrary backlog issues outside the approved block.",
            "Ne pas consommer d'autres issues du backlog hors du bloc explicitement approuvé.",
        ),
        (
            "A task is not complete merely because code exists on a branch.",
            "Une branche ou du code existant ne suffit pas : la définition de terminé inclut validation et fusion.",
        ),
    ]
    return [summary for marker, summary in checks if marker in agents_text]


def _source(label: str, url: str | None, kind: str) -> dict[str, str] | None:
    if not url:
        return None
    return {"label": label, "url": url, "kind": kind}


def _confidence(
    *,
    role: str,
    mission: dict[str, Any],
    phase: str,
    active_work: dict[str, Any] | None,
    section: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    if mission.get("state") == "blocked":
        return "BLOCKED", [mission.get("detail") or "La mission est bloquée par l'état courant."]

    missing: list[str] = []
    work = active_work or {}
    active_key = work.get("key")

    if not active_key and role in {"developer", "architect", "reviewer"}:
        missing.append("identité de tranche active")

    if work.get("subitem_key") and not section:
        missing.append(f"section exacte de {work.get('subitem_key')} dans l'issue parent")

    if role == "developer":
        if phase in {"DEVELOPING", "STALLED", "POSSIBLE_STALL"} and not (
            work.get("active_branch") or work.get("primary_pr")
        ):
            missing.append("branche ou PR active observable")
        if phase in {"PR_OPEN", "CI_RUNNING", "CI_RED", "READY_TO_MERGE"} and not work.get(
            "primary_pr"
        ):
            missing.append("PR active")
        if phase == "CI_RED" and not work.get("failed_jobs"):
            missing.append("jobs CI en échec")

    if role == "reviewer" and phase in {"PR_OPEN", "CI_RUNNING", "CI_RED", "READY_TO_MERGE"}:
        if not work.get("primary_pr"):
            missing.append("PR à réviser")

    return ("PARTIAL", missing) if missing else ("COMPLETE", [])


def _fact_lines(
    *,
    execution: dict[str, Any],
    active_work: dict[str, Any] | None,
    pipeline_now: dict[str, Any] | None,
    roadmap_issue: int,
    canonical_pipeline_issue: int,
    referenced_adrs: list[dict[str, str]],
    section: dict[str, Any] | None,
) -> list[str]:
    work = active_work or {}
    issue = work.get("issue") or {}
    pr = work.get("primary_pr") or {}
    branch = work.get("active_branch") or {}
    commit = work.get("last_commit") or {}
    lines = [
        f"- Roadmap humain : #{roadmap_issue}",
        f"- Pipeline canonique : #{canonical_pipeline_issue}",
        f"- Étape canonique : {(pipeline_now or {}).get('key') or work.get('key') or 'aucune'}",
        f"- Type/statut : {(pipeline_now or {}).get('kind') or '—'} / {(pipeline_now or {}).get('status') or '—'}",
        f"- Phase d'exécution : {execution.get('phase')}",
        f"- Prochaine action : {execution.get('next_action')}",
    ]
    if issue.get("number"):
        lines.append(f"- Issue parent : #{issue.get('number')} — {issue.get('title')}")
    if branch.get("name"):
        lines.append(f"- Branche : {branch.get('name')}")
    if pr.get("number"):
        lines.append(
            f"- PR : #{pr.get('number')} — {pr.get('title')} — état {pr.get('state')}"
        )
    if commit.get("short_sha") or commit.get("sha"):
        lines.append(
            f"- Dernier commit : {commit.get('short_sha') or str(commit.get('sha'))[:7]} — {commit.get('message')}"
        )
    failed_jobs = work.get("failed_jobs") or []
    if failed_jobs:
        lines.append("- Jobs CI rouges : " + ", ".join(str(job) for job in failed_jobs))
    runs = work.get("active_runs") or pr.get("runs") or []
    if runs:
        run_bits = []
        for run in runs[:4]:
            run_bits.append(
                f"{run.get('name') or 'CI'} #{run.get('run_number') or run.get('id')}={run.get('status')}/{run.get('conclusion') or '—'}"
            )
        lines.append("- CI observée : " + " ; ".join(run_bits))
    if referenced_adrs:
        lines.append(
            "- ADR référencés : "
            + ", ".join(str(adr.get("name") or "") for adr in referenced_adrs)
        )
    if work.get("can_chain_block") and work.get("remaining_subitems"):
        lines.append("- Chaîne autorisée restante : " + " → ".join(work["remaining_subitems"]))
    if section:
        lines.append(f"- Section exacte chargée : {section.get('title')}")
    return lines


def _role_guidance(
    role: str,
    *,
    roadmap_issue: int,
    canonical_pipeline_issue: int,
) -> str:
    if role == "developer":
        return (
            "Reprends l'état existant. Ne recommence pas l'analyse déjà matérialisée dans GitHub. "
            "Inspecte seulement ce qui manque pour exécuter la mission, puis poursuis le cycle AGENTS.md."
        )
    if role == "reviewer":
        return (
            "Centre la revue sur le diff/PR, les tests, la CI, les invariants et les risques de régression. "
            "Ne redessine pas l'architecture sans contradiction démontrée."
        )
    if role == "architect":
        return (
            "Réutilise strictement les ADR et décisions déjà stabilisés. N'ouvre une nouvelle décision "
            "que si le code ou les exigences démontrent une contradiction réelle."
        )
    if role == "product-owner":
        return (
            f"Roadmap humain : #{roadmap_issue}. "
            f"Pipeline canonique COCKPIT_PIPELINE_V1 : #{canonical_pipeline_issue}. "
            "Utilise le roadmap humain pour le contexte produit et le pipeline canonique pour l'état machine "
            "et la réconciliation; ne confonds pas les deux autorités et ne réordonne pas le pipeline sans décision explicite."
        )
    return "Utilise le contexte transmis comme point de reprise et évite de refaire l'exploration déjà disponible."


def build_handoff_packs(
    *,
    roadmap_issue: int,
    roadmap_url: str | None,
    canonical_pipeline_issue: int | None = None,
    canonical_pipeline_url: str | None = None,
    pipeline_now: dict[str, Any] | None,
    execution: dict[str, Any],
    active_work: dict[str, Any] | None,
    issue_body: str,
    referenced_adrs: list[dict[str, str]],
    agents_text: str,
) -> dict[str, Any]:
    pipeline_issue = canonical_pipeline_issue or roadmap_issue
    pipeline_url = canonical_pipeline_url or roadmap_url
    active_key = str((active_work or {}).get("key") or (pipeline_now or {}).get("key") or "")
    section = _active_section(issue_body, active_key)
    constraints = _agents_constraints(agents_text)

    shared_sources: list[dict[str, str]] = []
    for candidate in [
        _source(f"Roadmap humain #{roadmap_issue}", roadmap_url, "roadmap"),
        _source(f"Pipeline canonique #{pipeline_issue}", pipeline_url, "roadmap"),
        _source(
            f"Issue #{(active_work or {}).get('issue_number')}",
            ((active_work or {}).get("issue") or {}).get("url"),
            "issue",
        ),
        _source(
            f"PR #{((active_work or {}).get('primary_pr') or {}).get('number')}",
            ((active_work or {}).get("primary_pr") or {}).get("url"),
            "pull_request",
        ),
        _source(
            str(((active_work or {}).get("active_branch") or {}).get("name") or "Branche"),
            ((active_work or {}).get("active_branch") or {}).get("url"),
            "branch",
        ),
        _source(
            str(((active_work or {}).get("last_commit") or {}).get("short_sha") or "Commit"),
            ((active_work or {}).get("last_commit") or {}).get("url"),
            "commit",
        ),
    ]:
        if candidate:
            shared_sources.append(candidate)
    for adr in referenced_adrs:
        candidate = _source(str(adr.get("name") or "ADR"), adr.get("url"), "adr")
        if candidate:
            shared_sources.append(candidate)

    facts = _fact_lines(
        execution=execution,
        active_work=active_work,
        pipeline_now=pipeline_now,
        roadmap_issue=roadmap_issue,
        canonical_pipeline_issue=pipeline_issue,
        referenced_adrs=referenced_adrs,
        section=section,
    )

    packs: dict[str, Any] = {}
    for role, label in ROLE_LABELS.items():
        mission = execution.get("missions", {}).get(role) or execution.get("missions", {}).get(
            "generic", {}
        )
        confidence, missing = _confidence(
            role=role,
            mission=mission,
            phase=str(execution.get("phase") or ""),
            active_work=active_work,
            section=section,
        )

        parts = [
            f"# Handoff Pack — {label}",
            "",
            f"Confiance : {confidence}",
        ]
        if missing:
            parts.append("Contexte manquant/bloquant : " + " ; ".join(missing))
        parts.extend(
            [
                "",
                "## Mission",
                str(mission.get("title") or execution.get("label") or "Mission courante"),
                str(mission.get("detail") or execution.get("summary") or ""),
                "",
                "## Contexte GitHub transmis",
                *facts,
            ]
        )
        if constraints:
            parts.extend(["", "## Contraintes AGENTS.md", *[f"- {row}" for row in constraints]])
        if section:
            parts.extend(
                [
                    "",
                    "## Section exacte de l'issue",
                    f"### {section.get('title')}",
                    str(section.get("content") or ""),
                ]
            )
        parts.extend(
            [
                "",
                "## Consigne de reprise",
                _role_guidance(
                    role,
                    roadmap_issue=roadmap_issue,
                    canonical_pipeline_issue=pipeline_issue,
                ),
                "",
                "## Instruction",
                str(mission.get("prompt") or execution.get("prompt") or ""),
            ]
        )

        packs[role] = {
            "role": role,
            "role_label": label,
            "confidence": confidence,
            "missing": missing,
            "title": mission.get("title") or execution.get("label"),
            "prompt": "\n".join(parts).strip(),
            "sources": shared_sources,
            "section": section,
        }

    return {
        "active_key": active_key or None,
        "packs": packs,
    }

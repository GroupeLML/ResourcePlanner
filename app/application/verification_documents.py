from __future__ import annotations

import csv
from html import escape
from io import StringIO
import json
from typing import Any, Mapping


_PHASE_LABELS = {
    "FAT": "FAT",
    "SAT": "SAT",
    "COMMISSIONING": "Commissioning",
}


def _text(value: object | None, fallback: str = "—") -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _selected_execution(requirement: Mapping[str, Any]) -> Mapping[str, Any] | None:
    selected_id = requirement.get("latest_execution_id")
    if not selected_id:
        return None
    for execution in requirement.get("executions") or ():
        if execution.get("id") == selected_id:
            return execution
    return None


def _story(snapshot: Mapping[str, Any], story_id: str) -> Mapping[str, Any]:
    stories = snapshot.get("context", {}).get("stories", {})
    return stories.get(story_id, {}) if isinstance(stories, Mapping) else {}


def _latest_decisions(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for decision in snapshot.get("context", {}).get("story_decisions", ()):
        latest[str(decision.get("story_id") or "")] = decision
    return latest


def _html_shell(title: str, snapshot: Mapping[str, Any], body: str) -> str:
    context = snapshot.get("context", {})
    project = context.get("project", {})
    work_package = context.get("work_package", {})
    package = snapshot.get("package", {})
    generated_at = _text(snapshot.get("generated_at"))
    verification_version = _text(package.get("verification_version"), "non matérialisée")
    scope_id = _text(package.get("verification_scope_id"), "non matérialisé")
    project_label = f"{_text(project.get('number'))} — {_text(project.get('name'))}"
    work_package_label = _text(work_package.get("code"), _text(work_package.get("id")))
    work_package_name = _text(work_package.get("name"))
    coverage = package.get("story_decision_coverage", {})
    documented = int(coverage.get("documented_done_story_count") or 0)
    total = int(coverage.get("done_story_count") or 0)
    coverage_label = f"{documented} / {total}" if total else "aucune Story terminée"
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  :root {{ color-scheme: light; font-family: Arial, Helvetica, sans-serif; color: #18212f; }}
  body {{ max-width: 1100px; margin: 0 auto; padding: 28px; line-height: 1.42; }}
  h1, h2, h3 {{ margin: 0 0 10px; }}
  header {{ border-bottom: 2px solid #18212f; margin-bottom: 22px; padding-bottom: 14px; }}
  .meta {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 20px; font-size: 13px; }}
  .summary {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }}
  .pill {{ border: 1px solid #bcc6d4; border-radius: 999px; padding: 5px 10px; font-size: 12px; }}
  .test {{ break-inside: avoid; border: 1px solid #ccd4df; border-radius: 8px; padding: 14px; margin: 0 0 14px; }}
  .test dl {{ display: grid; grid-template-columns: 170px 1fr; gap: 5px 10px; margin: 10px 0 0; }}
  .test dt {{ color: #536174; font-weight: 700; }}
  .test dd {{ margin: 0; white-space: pre-wrap; }}
  .execution {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #dde3eb; }}
  .empty {{ border: 1px dashed #aeb9c7; padding: 16px; }}
  footer {{ margin-top: 22px; color: #536174; font-size: 11px; }}
  a {{ color: inherit; overflow-wrap: anywhere; }}
  @media print {{ body {{ max-width: none; padding: 0; }} .test {{ border-color: #777; }} }}
</style>
</head>
<body>
<header>
  <h1>{escape(title)}</h1>
  <div class="meta">
    <div><strong>Projet :</strong> {escape(project_label)}</div>
    <div><strong>WorkPackage :</strong> {escape(work_package_label)} — {escape(work_package_name)}</div>
    <div><strong>Périmètre Verification :</strong> {escape(scope_id)}</div>
    <div><strong>Version Verification :</strong> {escape(verification_version)}</div>
    <div><strong>Généré :</strong> {escape(generated_at)}</div>
    <div><strong>Décisions Stories documentées :</strong> {escape(coverage_label)}</div>
  </div>
</header>
{body}
<footer>
  Projection générée depuis les données structurées RessourcePlanner. Le document n'est pas une seconde source de vérité.
  Révision et exécution affichées : celles sélectionnées dans le snapshot Verification identifié ci-dessus.
</footer>
</body>
</html>"""


def _test_block(snapshot: Mapping[str, Any], requirement: Mapping[str, Any], *, report: bool) -> str:
    revision = requirement.get("current_revision") or {}
    story = _story(snapshot, str(requirement.get("story_id") or ""))
    selected = _selected_execution(requirement)
    prerequisites = revision.get("prerequisites") or ()
    prerequisites_label = "\n".join(f"• {_text(item)}" for item in prerequisites) or "—"
    status = _text(requirement.get("projected_status"), "retirée")
    story_label = _text(story.get("title"), _text(requirement.get("story_id")))
    epic_label = _text(story.get("epic_title"), "—")
    evidence_html = ""
    if selected:
        links = selected.get("evidence_links") or ()
        if links:
            evidence_html = "<ul>" + "".join(
                f'<li><a href="{escape(_text(link.get("url")), quote=True)}">{escape(_text(link.get("label"), _text(link.get("url"))))}</a></li>'
                for link in links
            ) + "</ul>"
        else:
            evidence_html = "—"
    execution_html = ""
    if report:
        if selected:
            measurements = json.dumps(selected.get("measurements") or {}, ensure_ascii=False, sort_keys=True)
            execution_html = f"""
<div class="execution">
  <h3>Exécution sélectionnée</h3>
  <dl>
    <dt>ID / séquence</dt><dd>{escape(_text(selected.get("id")))} / {escape(_text(selected.get("sequence")))}</dd>
    <dt>Révision testée</dt><dd>{escape(_text(selected.get("revision_id")))}</dd>
    <dt>Résultat</dt><dd>{escape(_text(selected.get("result")))}</dd>
    <dt>Exécutant</dt><dd>{escape(_text(selected.get("executor_user_id")))}</dd>
    <dt>Date métier</dt><dd>{escape(_text(selected.get("executed_at")))}</dd>
    <dt>Enregistré</dt><dd>{escape(_text(selected.get("recorded_at")))}</dd>
    <dt>Mesures</dt><dd>{escape(measurements)}</dd>
    <dt>Commentaires</dt><dd>{escape(_text(selected.get("comments")))}</dd>
    <dt>Preuves</dt><dd>{evidence_html}</dd>
  </dl>
</div>"""
        else:
            execution_html = '<div class="execution"><strong>Aucune exécution sélectionnée pour la révision courante.</strong></div>'
    return f"""
<article class="test">
  <h2>{escape(_text(revision.get("objective"), _text(requirement.get("id"))))}</h2>
  <div class="summary">
    <span class="pill">{escape(_text(requirement.get("phase")))}</span>
    <span class="pill">{escape(status)}</span>
    <span class="pill">Exigence {escape(_text(requirement.get("id")))}</span>
  </div>
  <dl>
    <dt>Epic</dt><dd>{escape(epic_label)}</dd>
    <dt>Story</dt><dd>{escape(story_label)} ({escape(_text(requirement.get("story_id")))})</dd>
    <dt>Révision sélectionnée</dt><dd>{escape(_text(revision.get("revision_number")))} — {escape(_text(revision.get("id")))}</dd>
    <dt>Méthode / action</dt><dd>{escape(_text(revision.get("method")))}</dd>
    <dt>Résultat attendu</dt><dd>{escape(_text(revision.get("expected_result")))}</dd>
    <dt>Prérequis</dt><dd>{escape(prerequisites_label)}</dd>
    <dt>Criticité</dt><dd>{escape(_text(revision.get("criticality")))}</dd>
  </dl>
  {execution_html}
</article>"""


def render_test_plan_html(snapshot: Mapping[str, Any], *, phase: str | None = None) -> str:
    normalized_phase = str(phase or "").strip().upper() or None
    requirements = [
        requirement
        for requirement in snapshot.get("package", {}).get("requirements", ())
        if requirement.get("state") == "ACTIVE"
        and (normalized_phase is None or requirement.get("phase") == normalized_phase)
    ]
    title = "Plan de test Verification"
    if normalized_phase:
        title += f" — {_PHASE_LABELS.get(normalized_phase, normalized_phase)}"
    body = "".join(_test_block(snapshot, requirement, report=False) for requirement in requirements)
    if not body:
        body = '<div class="empty">Aucun test défini pour ce périmètre. Ce cas n’est pas présenté comme 100 % réussi.</div>'
    return _html_shell(title, snapshot, body)


def render_phase_report_html(snapshot: Mapping[str, Any], phase: str) -> str:
    normalized_phase = str(phase or "").strip().upper()
    requirements = [
        requirement
        for requirement in snapshot.get("package", {}).get("requirements", ())
        if requirement.get("state") == "ACTIVE"
        and requirement.get("phase") == normalized_phase
    ]
    phase_summary = snapshot.get("package", {}).get("phases", {}).get(normalized_phase, {})
    body = (
        '<div class="summary">'
        f'<span class="pill">PASS {int(phase_summary.get("PASS") or 0)}</span>'
        f'<span class="pill">FAIL {int(phase_summary.get("FAIL") or 0)}</span>'
        f'<span class="pill">BLOCKED {int(phase_summary.get("BLOCKED") or 0)}</span>'
        f'<span class="pill">NOT_RUN {int(phase_summary.get("NOT_RUN") or 0)}</span>'
        '</div>'
    )
    body += "".join(_test_block(snapshot, requirement, report=True) for requirement in requirements)
    if not requirements:
        body += '<div class="empty">Aucun test défini pour cette phase.</div>'
    label = _PHASE_LABELS.get(normalized_phase, normalized_phase)
    return _html_shell(f"Rapport Verification — {label}", snapshot, body)


_TRACEABILITY_COLUMNS = (
    "project_number", "project_name", "work_package_id", "work_package_code", "work_package_name",
    "verification_scope_id", "verification_version", "generated_at", "epic_id", "epic_title",
    "story_id", "story_title", "decision_id", "decision_kind", "decision_justification",
    "decision_created_at", "selected_by_latest_decision", "requirement_id", "phase",
    "requirement_state", "withdrawal_reason", "current_revision_id", "current_revision_number",
    "objective", "method", "expected_result", "prerequisites", "criticality", "projected_status",
    "selected_execution_id", "selected_execution_sequence", "selected_execution_revision_id",
    "selected_result", "executor_user_id", "executed_at", "recorded_at", "measurements_json",
    "comments", "evidence_urls",
)


def render_traceability_csv(snapshot: Mapping[str, Any]) -> str:
    context = snapshot.get("context", {})
    project = context.get("project", {})
    work_package = context.get("work_package", {})
    package = snapshot.get("package", {})
    latest_decisions = _latest_decisions(snapshot)
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_TRACEABILITY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    requirements = list(package.get("requirements") or ())
    stories_with_requirements: set[str] = set()

    for requirement in requirements:
        story_id = str(requirement.get("story_id") or "")
        stories_with_requirements.add(story_id)
        story = _story(snapshot, story_id)
        decision = latest_decisions.get(story_id, {})
        selected = _selected_execution(requirement) or {}
        revision = requirement.get("current_revision") or {}
        evidence_urls = " | ".join(
            _text(link.get("url"), "")
            for link in selected.get("evidence_links") or ()
            if _text(link.get("url"), "")
        )
        selected_ids = {str(value) for value in decision.get("requirement_ids") or ()}
        writer.writerow({
            "project_number": project.get("number"),
            "project_name": project.get("name"),
            "work_package_id": work_package.get("id"),
            "work_package_code": work_package.get("code"),
            "work_package_name": work_package.get("name"),
            "verification_scope_id": package.get("verification_scope_id"),
            "verification_version": package.get("verification_version"),
            "generated_at": snapshot.get("generated_at"),
            "epic_id": story.get("epic_id"),
            "epic_title": story.get("epic_title"),
            "story_id": story_id,
            "story_title": story.get("title"),
            "decision_id": decision.get("id"),
            "decision_kind": decision.get("kind"),
            "decision_justification": decision.get("justification"),
            "decision_created_at": decision.get("created_at"),
            "selected_by_latest_decision": requirement.get("id") in selected_ids,
            "requirement_id": requirement.get("id"),
            "phase": requirement.get("phase"),
            "requirement_state": requirement.get("state"),
            "withdrawal_reason": requirement.get("withdrawal_reason"),
            "current_revision_id": revision.get("id"),
            "current_revision_number": revision.get("revision_number"),
            "objective": revision.get("objective"),
            "method": revision.get("method"),
            "expected_result": revision.get("expected_result"),
            "prerequisites": " | ".join(str(item) for item in revision.get("prerequisites") or ()),
            "criticality": revision.get("criticality"),
            "projected_status": requirement.get("projected_status"),
            "selected_execution_id": selected.get("id"),
            "selected_execution_sequence": selected.get("sequence"),
            "selected_execution_revision_id": selected.get("revision_id"),
            "selected_result": selected.get("result"),
            "executor_user_id": selected.get("executor_user_id"),
            "executed_at": selected.get("executed_at"),
            "recorded_at": selected.get("recorded_at"),
            "measurements_json": (
                json.dumps(selected.get("measurements") or {}, ensure_ascii=False, sort_keys=True)
                if selected else ""
            ),
            "comments": selected.get("comments"),
            "evidence_urls": evidence_urls,
        })

    for story_id, decision in latest_decisions.items():
        if decision.get("kind") != "NO_TEST_REQUIRED" or story_id in stories_with_requirements:
            continue
        story = _story(snapshot, story_id)
        writer.writerow({
            "project_number": project.get("number"),
            "project_name": project.get("name"),
            "work_package_id": work_package.get("id"),
            "work_package_code": work_package.get("code"),
            "work_package_name": work_package.get("name"),
            "verification_scope_id": package.get("verification_scope_id"),
            "verification_version": package.get("verification_version"),
            "generated_at": snapshot.get("generated_at"),
            "epic_id": story.get("epic_id"),
            "epic_title": story.get("epic_title"),
            "story_id": story_id,
            "story_title": story.get("title"),
            "decision_id": decision.get("id"),
            "decision_kind": decision.get("kind"),
            "decision_justification": decision.get("justification"),
            "decision_created_at": decision.get("created_at"),
            "selected_by_latest_decision": True,
            "projected_status": "NO_TEST_REQUIRED",
        })

    return output.getvalue()

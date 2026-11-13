from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


EXECUTION_PHASES = {
    "PIPELINE_INVALID",
    "ROADMAP_UPDATE_REQUIRED",
    "ARCHITECTURE_GATE",
    "ENVIRONMENT_GATE",
    "READY",
    "DEVELOPING",
    "PR_OPEN",
    "CI_RUNNING",
    "CI_RED",
    "STALLED",
    "POSSIBLE_STALL",
    "READY_TO_MERGE",
    "DELIVERY_UNVERIFIED",
    "NO_ACTIVE_WORK",
}


def _link(label: str, url: str | None) -> dict[str, str] | None:
    if not url:
        return None
    return {"label": label, "url": url}


def _mission(
    state: str,
    title: str,
    detail: str,
    prompt: str,
    *,
    link: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "title": title,
        "detail": detail,
        "prompt": prompt,
        "primary_link": link,
    }


def _timeline(
    *,
    roadmap_issue: int,
    roadmap_url: str | None,
    roadmap_updated_at: str | None,
    reconciliation: dict[str, Any],
    active_work: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    if roadmap_updated_at:
        events.append(
            {
                "kind": "roadmap",
                "label": f"Pipeline #{roadmap_issue} mis à jour",
                "detail": reconciliation.get("summary") or "État canonique GitHub.",
                "at": roadmap_updated_at,
                "url": roadmap_url,
                "status": reconciliation.get("status"),
            }
        )

    if not active_work:
        return events[:8]

    commit = active_work.get("last_commit") or {}
    if commit.get("date"):
        events.append(
            {
                "kind": "commit",
                "label": f"Commit {commit.get('short_sha') or str(commit.get('sha') or '')[:7]}",
                "detail": commit.get("message") or "Commit actif",
                "at": commit.get("date"),
                "url": commit.get("url"),
                "status": "commit",
            }
        )

    primary_pr = active_work.get("primary_pr") or {}
    if primary_pr.get("created_at"):
        events.append(
            {
                "kind": "pull_request",
                "label": f"PR #{primary_pr.get('number')} ouverte",
                "detail": primary_pr.get("title") or "Pull request active",
                "at": primary_pr.get("created_at"),
                "url": primary_pr.get("url"),
                "status": "open",
            }
        )

    merged = active_work.get("merged_but_unmarked_pr") or {}
    if merged.get("merged_at"):
        events.append(
            {
                "kind": "pull_request",
                "label": f"PR #{merged.get('number')} fusionnée",
                "detail": merged.get("title") or "Livraison détectée",
                "at": merged.get("merged_at"),
                "url": merged.get("url"),
                "status": "merged",
            }
        )

    runs = active_work.get("active_runs") or primary_pr.get("runs") or []
    for run in runs[:4]:
        name = str(run.get("name") or "CI")
        number = run.get("run_number")
        suffix = f" #{number}" if number is not None else ""
        if run.get("created_at"):
            events.append(
                {
                    "kind": "ci",
                    "label": f"{name}{suffix} démarrée",
                    "detail": "Workflow GitHub Actions démarré.",
                    "at": run.get("created_at"),
                    "url": run.get("url"),
                    "status": "running",
                }
            )
        if run.get("status") == "completed" and run.get("updated_at"):
            conclusion = str(run.get("conclusion") or "terminée")
            events.append(
                {
                    "kind": "ci",
                    "label": f"{name}{suffix} terminée",
                    "detail": f"Conclusion : {conclusion}.",
                    "at": run.get("updated_at"),
                    "url": run.get("url"),
                    "status": conclusion,
                }
            )

    def sort_key(event: dict[str, Any]) -> tuple[datetime, str]:
        raw = str(event.get("at") or "")
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        return stamp, str(event.get("label") or "")

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (str(event.get("at") or ""), str(event.get("label") or ""))
        unique[key] = event
    return sorted(unique.values(), key=sort_key, reverse=True)[:8]


def _phase(
    *,
    pipeline_valid: bool,
    pipeline_now: dict[str, Any] | None,
    reconciliation: dict[str, Any],
    active_work: dict[str, Any] | None,
) -> str:
    if not pipeline_valid:
        return "PIPELINE_INVALID"
    if reconciliation.get("status") == "stale":
        return "ROADMAP_UPDATE_REQUIRED"
    if pipeline_now and pipeline_now.get("kind") == "ARCHITECTURE_GATE":
        return "ARCHITECTURE_GATE"
    if pipeline_now and pipeline_now.get("kind") == "ENVIRONMENT_GATE":
        return "ENVIRONMENT_GATE"
    if not active_work:
        return "NO_ACTIVE_WORK"

    states = set(active_work.get("states") or [])
    if "STALLED_CONFIRMED" in states or "STALLED" in states:
        return "STALLED"
    if "POSSIBLE_STALL" in states:
        return "POSSIBLE_STALL"
    if "CI_RED" in states:
        return "CI_RED"
    if "CI_RUNNING" in states:
        return "CI_RUNNING"
    if "MERGEABLE" in states:
        return "READY_TO_MERGE"

    primary_pr = active_work.get("primary_pr")
    if primary_pr and primary_pr.get("state") == "open":
        return "PR_OPEN"
    if active_work.get("merged_but_unmarked_pr"):
        return "DELIVERY_UNVERIFIED"
    if active_work.get("active_branch") or "IN_PROGRESS" in states:
        return "DEVELOPING"
    if "READY" in states:
        return "READY"
    return "READY"


def _control_copy(
    phase: str,
    *,
    roadmap_issue: int,
    roadmap_url: str | None,
    active_work: dict[str, Any] | None,
    reconciliation: dict[str, Any],
    fallback_next_action: str,
    fallback_dev_prompt: str,
) -> tuple[str, str, str, str, dict[str, str] | None]:
    key = str((active_work or {}).get("key") or "la tranche active")
    issue = (active_work or {}).get("issue") or {}
    primary_pr = (active_work or {}).get("primary_pr") or {}
    branch = (active_work or {}).get("active_branch") or {}
    failed_jobs = (active_work or {}).get("failed_jobs") or []
    pr_number = primary_pr.get("number")

    if phase == "PIPELINE_INVALID":
        return (
            "Pipeline invalide",
            f"Le contrat canonique #{roadmap_issue} doit être corrigé avant tout nouveau travail.",
            f"Corriger COCKPIT_PIPELINE_V1 dans #{roadmap_issue}.",
            (
                f"Le pipeline canonique GitHub #{roadmap_issue} est invalide. "
                "Ne démarre aucune tranche DEV. Corrige d'abord le contrat canonique, "
                "puis vérifie que le cockpit résout une unique étape MAIN READY."
            ),
            _link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )

    if phase == "ROADMAP_UPDATE_REQUIRED":
        changes = (reconciliation.get("proposal") or {}).get("changes") or []
        change_text = " · ".join(
            f"{row.get('key')}: {row.get('from')} → {row.get('to')}" for row in changes
        )
        return (
            "Roadmap à réconcilier",
            "Une livraison GitHub vérifiée n'est pas encore reflétée dans #55.",
            f"Mettre à jour #{roadmap_issue}" + (f" : {change_text}" if change_text else "."),
            (
                f"Ne démarre pas la tranche suivante tant que le pipeline #{roadmap_issue} n'est pas réconcilié. "
                "Applique la proposition déterministe du Roadmap Reconciler, garde le bloc "
                "COCKPIT_PIPELINE_V1 autoritaire et vérifie ensuite la nouvelle étape READY."
            ),
            _link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )

    if phase == "ARCHITECTURE_GATE":
        return (
            "Gate architecture",
            f"{key} est une gate d'architecture, pas une tranche DEV.",
            "Compléter et documenter la gate architecture.",
            fallback_dev_prompt,
            _link(f"Issue #{issue.get('number')}", issue.get("url")),
        )

    if phase == "ENVIRONMENT_GATE":
        return (
            "Gate environnement",
            f"{key} attend une validation d'environnement explicite.",
            "Compléter la validation environnementale requise.",
            fallback_dev_prompt,
            _link(f"Issue #{issue.get('number')}", issue.get("url")),
        )

    if phase == "STALLED":
        jobs = ", ".join(str(job) for job in failed_jobs)
        detail = (
            f"{key} semble interrompu; des échecs CI restent visibles : {jobs}."
            if jobs
            else f"{key} semble interrompu et aucune reprise GitHub récente n'est détectée."
        )
        return (
            "Travail probablement interrompu",
            detail,
            f"Reprendre {key} depuis le dernier état GitHub observable.",
            (
                f"Reprends {key} à partir de la branche/PR et du dernier commit visibles. "
                + (f"Commence par les jobs rouges ({jobs}). " if jobs else "")
                + "Ne recommence pas l'implémentation depuis zéro : inspecte l'état courant, poursuis la plus petite correction "
                "nécessaire et amène le cycle AGENTS.md jusqu'à CI verte puis fusion."
            ),
            _link(f"PR #{pr_number}", primary_pr.get("url")) if pr_number else _link(str(branch.get("name") or key), branch.get("url")),
        )

    if phase == "POSSIBLE_STALL":
        return (
            "Reprise à confirmer",
            f"{key} est marqué en cours mais aucun signal GitHub récent ne confirme une activité.",
            "Vérifier la conversation/branche puis reprendre seulement à partir de l'état réel.",
            (
                f"Vérifie l'état réel de {key} avant de créer du nouveau travail. S'il existe une branche ou une PR, reprends-la; "
                "sinon relis l'issue active et démarre uniquement la prochaine action non effectuée. Publie rapidement un signal GitHub "
                "observable (commit ou PR) pour sortir de l'état possiblement interrompu."
            ),
            _link(f"Issue #{issue.get('number')}", issue.get("url")),
        )

    if phase == "CI_RED":
        jobs = ", ".join(str(job) for job in failed_jobs) or "jobs rouges"
        return (
            "CI rouge",
            f"{key} a une CI en échec : {jobs}.",
            f"Corriger la CI de {key} et relancer les checks.",
            (
                f"Reprends {key}"
                + (f" dans la PR #{pr_number}" if pr_number else "")
                + f". Analyse uniquement les jobs rouges ({jobs}), corrige la cause sans affaiblir les tests, "
                "relance la CI et poursuis jusqu'à vert conformément à AGENTS.md."
            ),
            _link(f"PR #{pr_number}", primary_pr.get("url")) if pr_number else None,
        )

    if phase == "CI_RUNNING":
        return (
            "CI en cours",
            f"Les checks GitHub de {key} sont en cours.",
            "Surveiller la CI; intervenir seulement si un check échoue.",
            (
                f"La CI de {key} est en cours. N'invente pas de nouveau travail pendant l'attente. "
                "Si un check échoue, diagnostique-le et corrige la cause; s'ils deviennent tous verts, "
                "poursuis le cycle AGENTS.md jusqu'à fusion."
            ),
            _link(f"PR #{pr_number}", primary_pr.get("url")) if pr_number else None,
        )

    if phase == "READY_TO_MERGE":
        return (
            "Prête à fusionner",
            f"La PR de {key} est verte et mergeable.",
            "Vérifier la PR puis fusionner si aucune anomalie ne reste.",
            (
                f"Vérifie la PR #{pr_number} de {key}. Confirme que les changements correspondent à l'issue, "
                "que les checks requis sont verts et qu'aucun défaut bloquant ne reste. "
                "Si tout est conforme, laisse le cycle AGENTS.md aller jusqu'à fusion puis réconciliation de #55."
            ),
            _link(f"PR #{pr_number}", primary_pr.get("url")) if pr_number else None,
        )

    if phase == "PR_OPEN":
        return (
            "PR ouverte",
            f"{key} possède une PR ouverte, mais elle n'est pas encore prête à fusionner.",
            f"Amener la PR #{pr_number} jusqu'à CI verte et mergeable.",
            (
                f"Reprends {key} dans la PR #{pr_number}. Termine l'implémentation et les validations pertinentes, "
                "traite les commentaires ou checks restants, puis poursuis jusqu'à CI verte et fusion selon AGENTS.md."
            ),
            _link(f"PR #{pr_number}", primary_pr.get("url")),
        )

    if phase == "DELIVERY_UNVERIFIED":
        merged = (active_work or {}).get("merged_but_unmarked_pr") or {}
        return (
            "Livraison à vérifier",
            f"Une PR correspondant à {key} est fusionnée, mais la preuve de livraison complète n'est pas encore vérifiée.",
            "Vérifier la CI et réconcilier #55 seulement si la livraison est confirmée.",
            (
                f"Une PR fusionnée correspond à {key}, mais le Roadmap Reconciler ne dispose pas encore d'une preuve "
                "CI suffisamment forte pour promouvoir l'étape. Vérifie les checks GitHub et ne démarre pas "
                "implicitement la tranche suivante."
            ),
            _link(f"PR #{merged.get('number')}", merged.get("url")),
        )

    if phase == "DEVELOPING":
        branch_name = branch.get("name")
        return (
            "Développement en cours",
            f"{key} a du travail GitHub actif" + (f" sur {branch_name}" if branch_name else "") + ".",
            "Poursuivre l'implémentation et ouvrir/mettre à jour la PR.",
            fallback_dev_prompt,
            _link(str(branch_name), branch.get("url")) if branch_name else _link(f"Issue #{issue.get('number')}", issue.get("url")),
        )

    if phase == "READY":
        return (
            "Prête à démarrer",
            f"{key} est la tranche canonique READY et aucun travail actif n'est détecté.",
            f"Démarrer {key}.",
            fallback_dev_prompt,
            _link(f"Issue #{issue.get('number')}", issue.get("url")),
        )

    return (
        "Aucun travail actif",
        "Le pipeline ne fournit actuellement aucune tranche exécutable.",
        fallback_next_action,
        fallback_dev_prompt,
        _link(f"Pipeline #{roadmap_issue}", roadmap_url),
    )


def _missions(
    *,
    phase: str,
    control: dict[str, Any],
    pipeline_now: dict[str, Any] | None,
    reconciliation: dict[str, Any],
    active_work: dict[str, Any] | None,
    roadmap_issue: int,
    roadmap_url: str | None,
) -> dict[str, dict[str, Any]]:
    key = str((active_work or {}).get("key") or (pipeline_now or {}).get("key") or "—")
    primary_pr = (active_work or {}).get("primary_pr") or {}
    failed_jobs = (active_work or {}).get("failed_jobs") or []
    controller_link = control.get("primary_link")

    if phase == "PIPELINE_INVALID":
        po = _mission(
            "blocked",
            "Corriger le pipeline canonique",
            f"#{roadmap_issue} est invalide; aucune prochaine tranche ne doit être déduite.",
            control["prompt"],
            link=_link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )
    elif phase == "ROADMAP_UPDATE_REQUIRED":
        po = _mission(
            "action",
            "Réconcilier #55",
            reconciliation.get("summary") or "Le roadmap est en retard sur GitHub.",
            control["prompt"],
            link=_link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )
    elif reconciliation.get("status") == "attention":
        po = _mission(
            "action",
            "Vérifier les écarts GitHub",
            reconciliation.get("summary") or "Un écart mérite une validation.",
            (
                f"Examine les écarts signalés par le Roadmap Reconciler pour #{roadmap_issue}. "
                "Ne change l'ordre canonique que si les preuves GitHub justifient réellement une mise à jour."
            ),
            link=_link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )
    else:
        po = _mission(
            "clear",
            "Aucune décision produit requise",
            f"Le flux canonique reste centré sur {key}.",
            (
                f"Surveille le pipeline canonique #{roadmap_issue}. Aucune décision produit n'est requise pour {key} "
                "tant qu'un nouvel écart ou une gate n'apparaît pas."
            ),
            link=_link(f"Pipeline #{roadmap_issue}", roadmap_url),
        )

    if phase in {"PIPELINE_INVALID", "ROADMAP_UPDATE_REQUIRED", "DELIVERY_UNVERIFIED"}:
        developer = _mission(
            "blocked",
            "Attendre la cohérence GitHub",
            control["summary"],
            control["prompt"],
            link=controller_link,
        )
    elif phase == "CI_RUNNING":
        developer = _mission("waiting", "Surveiller la CI", control["summary"], control["prompt"], link=controller_link)
    elif phase == "READY_TO_MERGE":
        developer = _mission(
            "waiting",
            "PR prête; ne pas ouvrir une nouvelle tranche",
            "La livraison attend la revue/fusion avant toute suite.",
            control["prompt"],
            link=controller_link,
        )
    elif phase in {"ARCHITECTURE_GATE", "ENVIRONMENT_GATE"}:
        developer = _mission("blocked", "Gate active", control["summary"], control["prompt"], link=controller_link)
    elif phase == "NO_ACTIVE_WORK":
        developer = _mission("clear", "Aucune tranche DEV active", control["summary"], control["prompt"], link=controller_link)
    else:
        developer = _mission("action", control["label"], control["next_action"], control["prompt"], link=controller_link)

    if pipeline_now and pipeline_now.get("kind") == "ARCHITECTURE_GATE":
        architect = _mission(
            "action",
            "Gate d'architecture active",
            str(pipeline_now.get("title") or key),
            (
                f"Analyse et documente la gate d'architecture {pipeline_now.get('key')} demandée par le pipeline #{roadmap_issue}. "
                "Ne transforme pas la gate en implémentation applicative; documente la décision et mets #55 à jour quand elle est satisfaite."
            ),
            link=_link(f"Issue #{pipeline_now.get('issue_number')}", (active_work or {}).get("issue", {}).get("url")),
        )
    else:
        architect = _mission(
            "clear",
            "Aucune gate architecture active",
            f"La tranche courante {key} peut réutiliser les ADR applicables sans nouvelle décision d'architecture.",
            (
                f"Reste disponible pour {key}. N'ouvre une nouvelle décision d'architecture que si une contradiction réelle "
                "avec les ADR ou le code actuel apparaît."
            ),
        )

    pr_number = primary_pr.get("number")
    if phase in {"CI_RED", "STALLED"} and failed_jobs:
        jobs = ", ".join(str(job) for job in failed_jobs) or "jobs en échec"
        reviewer = _mission(
            "action",
            "Examiner les échecs CI",
            f"{jobs}.",
            (
                f"Examine les jobs CI en échec de {key} ({jobs}). Identifie la cause et distingue régression réelle, "
                "test de contrat et problème d'infrastructure. Ne recommande pas d'affaiblir un test valide."
            ),
            link=controller_link,
        )
    elif phase == "READY_TO_MERGE":
        reviewer = _mission(
            "action",
            "Revue finale de la PR",
            f"PR #{pr_number} verte et mergeable.",
            (
                f"Effectue la revue finale de la PR #{pr_number} pour {key}: portée, tests, risques et respect d'AGENTS.md. "
                "Si aucun défaut bloquant ne reste, la PR peut être fusionnée."
            ),
            link=controller_link,
        )
    elif phase in {"PR_OPEN", "CI_RUNNING"}:
        reviewer = _mission(
            "waiting",
            "Attendre la fin des checks",
            f"{key} n'est pas encore prêt pour une revue finale.",
            f"Surveille {key}; effectue la revue finale lorsque la PR et les checks deviennent stables.",
            link=controller_link,
        )
    else:
        reviewer = _mission(
            "clear",
            "Aucune revue bloquante",
            f"Aucun signal de revue urgente pour {key}.",
            f"Reste disponible pour la revue de {key} lorsqu'une PR ou un échec CI le nécessite.",
        )

    generic = _mission(
        "clear",
        "Suivre le contrôleur d'exécution",
        control["next_action"],
        control["prompt"],
        link=controller_link,
    )

    return {
        "product-owner": po,
        "developer": developer,
        "architect": architect,
        "reviewer": reviewer,
        "generic": generic,
    }


def build_execution_control(
    *,
    roadmap_issue: int,
    roadmap_url: str | None,
    roadmap_updated_at: str | None,
    pipeline_valid: bool,
    pipeline_now: dict[str, Any] | None,
    reconciliation: dict[str, Any],
    active_work: dict[str, Any] | None,
    fallback_next_action: str,
    fallback_dev_prompt: str,
) -> dict[str, Any]:
    phase = _phase(
        pipeline_valid=pipeline_valid,
        pipeline_now=pipeline_now,
        reconciliation=reconciliation,
        active_work=active_work,
    )
    if phase not in EXECUTION_PHASES:
        raise ValueError(f"Phase d'exécution inconnue: {phase}")

    label, summary, next_action, prompt, primary_link = _control_copy(
        phase,
        roadmap_issue=roadmap_issue,
        roadmap_url=roadmap_url,
        active_work=active_work,
        reconciliation=reconciliation,
        fallback_next_action=fallback_next_action,
        fallback_dev_prompt=fallback_dev_prompt,
    )
    control = {
        "phase": phase,
        "label": label,
        "summary": summary,
        "next_action": next_action,
        "prompt": prompt,
        "responsible_role": (
            "product-owner"
            if phase in {"PIPELINE_INVALID", "ROADMAP_UPDATE_REQUIRED"}
            else "reviewer"
            if phase == "READY_TO_MERGE"
            else "architect"
            if phase == "ARCHITECTURE_GATE"
            else "developer"
        ),
        "primary_link": primary_link,
        "timeline": _timeline(
            roadmap_issue=roadmap_issue,
            roadmap_url=roadmap_url,
            roadmap_updated_at=roadmap_updated_at,
            reconciliation=reconciliation,
            active_work=active_work,
        ),
    }
    control["missions"] = _missions(
        phase=phase,
        control=control,
        pipeline_now=pipeline_now,
        reconciliation=reconciliation,
        active_work=active_work,
        roadmap_issue=roadmap_issue,
        roadmap_url=roadmap_url,
    )
    return control

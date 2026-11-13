from __future__ import annotations

from typing import Any


LEVEL_ORDER = {"CLEAR": 0, "WATCH": 1, "ACTION": 2}
ROLE_LABELS = {
    "product-owner": "Product Owner",
    "developer": "Developer",
    "architect": "Architecte",
    "reviewer": "Reviewer",
    "generic": "Generic",
}


def _link(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not value or not value.get("url"):
        return None
    return {
        "label": str(value.get("label") or "GitHub"),
        "url": str(value["url"]),
    }


def _subject_key(
    *,
    active_work: dict[str, Any] | None,
    pipeline_now: dict[str, Any] | None,
) -> str:
    return str(
        (active_work or {}).get("key")
        or (pipeline_now or {}).get("key")
        or "roadmap"
    )


def _item(
    *,
    code: str,
    level: str,
    role: str,
    key: str,
    title: str,
    detail: str,
    action: str,
    primary_link: dict[str, str] | None,
    handoff_confidence: str | None,
) -> dict[str, Any]:
    return {
        "id": f"{role}:{key}:{code}",
        "code": code,
        "level": level,
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "key": key,
        "title": title,
        "detail": detail,
        "action": action,
        "primary_link": primary_link,
        "handoff_confidence": handoff_confidence,
    }


def _execution_item(
    *,
    execution: dict[str, Any],
    active_work: dict[str, Any] | None,
    pipeline_now: dict[str, Any] | None,
    handoff: dict[str, Any],
) -> dict[str, Any] | None:
    phase = str(execution.get("phase") or "")
    key = _subject_key(active_work=active_work, pipeline_now=pipeline_now)
    role = str(execution.get("responsible_role") or "developer")
    confidence = (
        ((handoff.get("packs") or {}).get(role) or {}).get("confidence")
    )
    primary_link = _link(execution.get("primary_link"))

    mapping: dict[str, tuple[str, str]] = {
        "PIPELINE_INVALID": ("ACTION", "Pipeline canonique invalide"),
        "ROADMAP_UPDATE_REQUIRED": ("ACTION", "Roadmap à réconcilier"),
        "ARCHITECTURE_GATE": ("ACTION", "Gate architecture à traiter"),
        "ENVIRONMENT_GATE": ("ACTION", "Gate environnement à traiter"),
        "READY": ("ACTION", "Tranche prête à démarrer"),
        "DEVELOPING": ("WATCH", "Développement en cours"),
        "PR_OPEN": ("ACTION", "PR ouverte à faire avancer"),
        "CI_RUNNING": ("WATCH", "CI en cours"),
        "CI_RED": ("ACTION", "CI rouge"),
        "STALLED": ("ACTION", "Travail probablement interrompu"),
        "POSSIBLE_STALL": ("WATCH", "Reprise à confirmer"),
        "READY_TO_MERGE": ("ACTION", "PR prête à fusionner"),
        "DELIVERY_UNVERIFIED": ("ACTION", "Livraison fusionnée à vérifier"),
    }
    if phase not in mapping:
        return None

    level, title = mapping[phase]
    return _item(
        code=f"execution_{phase.lower()}",
        level=level,
        role=role,
        key=key,
        title=title,
        detail=str(execution.get("summary") or ""),
        action=str(execution.get("next_action") or ""),
        primary_link=primary_link,
        handoff_confidence=str(confidence) if confidence else None,
    )


def _reconciliation_items(
    *,
    reconciliation: dict[str, Any],
    handoff: dict[str, Any],
    canonical_pipeline_issue: int,
) -> list[dict[str, Any]]:
    if reconciliation.get("status") != "attention":
        return []

    packs = handoff.get("packs") or {}
    rows: list[dict[str, Any]] = []
    for finding in reconciliation.get("findings") or []:
        code = str(finding.get("code") or "reconciliation")
        key = str(finding.get("key") or "roadmap")
        if code == "blocked_open_pr":
            level = "WATCH"
            role = "product-owner"
            title = "Travail détecté hors ordre canonique"
            action = (
                f"Vérifier que cette PR future est volontaire sans modifier l'ordre "
                f"du pipeline #{canonical_pipeline_issue}."
            )
        elif code == "blocked_merged_pr":
            level = "ACTION"
            role = "product-owner"
            title = "Travail futur déjà fusionné"
            action = (
                f"Vérifier la livraison et décider explicitement si le pipeline "
                f"#{canonical_pipeline_issue} doit être réconcilié."
            )
        elif code == "ready_merged_unverified":
            level = "WATCH"
            role = "reviewer"
            title = "Livraison à valider"
            action = (
                f"Vérifier les checks de la PR fusionnée avant toute promotion du pipeline "
                f"#{canonical_pipeline_issue}."
            )
        else:
            level = "WATCH"
            role = "product-owner"
            title = "Écart GitHub à vérifier"
            action = "Vérifier l'écart sans changer implicitement le pipeline canonique."

        evidence = finding.get("evidence") or []
        primary_link = None
        for entry in evidence:
            if entry.get("url"):
                primary_link = {
                    "label": str(entry.get("label") or "GitHub"),
                    "url": str(entry["url"]),
                }
                break
        confidence = ((packs.get(role) or {}).get("confidence"))
        rows.append(
            _item(
                code=code,
                level=level,
                role=role,
                key=key,
                title=title,
                detail=str(finding.get("message") or ""),
                action=action,
                primary_link=primary_link,
                handoff_confidence=str(confidence) if confidence else None,
            )
        )
    return rows


def _handoff_items(
    *,
    handoff: dict[str, Any],
    active_key: str,
    phase: str,
) -> list[dict[str, Any]]:
    if phase == "NO_ACTIVE_WORK":
        return []
    rows: list[dict[str, Any]] = []
    for role, pack in (handoff.get("packs") or {}).items():
        if pack.get("confidence") != "PARTIAL":
            continue
        missing = [str(value) for value in pack.get("missing") or []]
        rows.append(
            _item(
                code="handoff_partial",
                level="WATCH",
                role=str(role),
                key=active_key,
                title="Handoff incomplet",
                detail=(
                    "Contexte de reprise partiel : " + "; ".join(missing)
                    if missing
                    else "Le Handoff Pack est partiel."
                ),
                action="Vérifier le contexte manquant avant de reprendre la mission.",
                primary_link=None,
                handoff_confidence="PARTIAL",
            )
        )
    return rows


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        bucket = (str(item.get("role")), str(item.get("key")))
        previous = chosen.get(bucket)
        if previous is None:
            chosen[bucket] = item
            continue
        if LEVEL_ORDER[str(item["level"])] > LEVEL_ORDER[str(previous["level"])]:
            chosen[bucket] = item
    return sorted(
        chosen.values(),
        key=lambda item: (
            -LEVEL_ORDER[str(item["level"])],
            str(item.get("role_label") or ""),
            str(item.get("key") or ""),
            str(item.get("title") or ""),
        ),
    )


def build_attention_center(
    *,
    execution: dict[str, Any],
    reconciliation: dict[str, Any],
    handoff: dict[str, Any],
    active_work: dict[str, Any] | None,
    pipeline_now: dict[str, Any] | None,
    canonical_pipeline_issue: int = 55,
) -> dict[str, Any]:
    key = _subject_key(active_work=active_work, pipeline_now=pipeline_now)
    candidates: list[dict[str, Any]] = []

    execution_item = _execution_item(
        execution=execution,
        active_work=active_work,
        pipeline_now=pipeline_now,
        handoff=handoff,
    )
    if execution_item:
        candidates.append(execution_item)

    # A stale/invalid pipeline is already represented by the execution controller.
    # Extra reconciliation findings are useful only for the non-stale attention state.
    candidates.extend(
        _reconciliation_items(
            reconciliation=reconciliation,
            handoff=handoff,
            canonical_pipeline_issue=canonical_pipeline_issue,
        )
    )
    candidates.extend(
        _handoff_items(
            handoff=handoff,
            active_key=key,
            phase=str(execution.get("phase") or ""),
        )
    )

    items = _deduplicate(candidates)
    action_count = sum(1 for item in items if item["level"] == "ACTION")
    watch_count = sum(1 for item in items if item["level"] == "WATCH")
    status = "ACTION" if action_count else "WATCH" if watch_count else "CLEAR"

    roles = {
        role: {
            "label": label,
            "actions": sum(
                1
                for item in items
                if item["role"] == role and item["level"] == "ACTION"
            ),
            "watches": sum(
                1
                for item in items
                if item["role"] == role and item["level"] == "WATCH"
            ),
        }
        for role, label in ROLE_LABELS.items()
        if role != "generic"
    }

    return {
        "status": status,
        "action_count": action_count,
        "watch_count": watch_count,
        "summary": (
            "Aucune intervention requise."
            if status == "CLEAR"
            else f"{action_count} action(s) · {watch_count} attente(s)"
        ),
        "roles": roles,
        "items": items,
    }

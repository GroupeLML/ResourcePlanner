from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AvailabilityRuleResourceClass


def availability_class_codes_by_rule(
    session: Session,
    rule_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Load all class scopes for availability rules in one query."""

    identifiers = tuple(dict.fromkeys(str(value) for value in rule_ids if str(value)))
    if not identifiers:
        return {}
    grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    rows = session.execute(
        select(
            AvailabilityRuleResourceClass.availability_rule_id,
            AvailabilityRuleResourceClass.resource_class_code,
        )
        .where(AvailabilityRuleResourceClass.availability_rule_id.in_(identifiers))
        .order_by(
            AvailabilityRuleResourceClass.availability_rule_id,
            AvailabilityRuleResourceClass.resource_class_code,
        )
    ).all()
    for rule_id, code in rows:
        grouped.setdefault(str(rule_id), []).append(str(code))
    return {
        rule_id: tuple(codes)
        for rule_id, codes in grouped.items()
    }

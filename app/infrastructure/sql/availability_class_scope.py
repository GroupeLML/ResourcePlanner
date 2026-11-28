from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AvailabilityRuleResourceClass


def availability_class_codes_by_rule(
    session: Session,
    rule_ids: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Load all class scopes for availability rules in bounded queries."""

    identifiers = tuple(dict.fromkeys(str(value) for value in rule_ids if str(value)))
    if not identifiers:
        return {}
    grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    seen = set()
    for offset in range(0, len(identifiers), 900):
        chunk = identifiers[offset:offset + 900]
        rows = session.execute(
            select(
                AvailabilityRuleResourceClass.availability_rule_id,
                AvailabilityRuleResourceClass.resource_class_code,
            )
            .where(AvailabilityRuleResourceClass.availability_rule_id.in_(chunk))
            .order_by(
                AvailabilityRuleResourceClass.availability_rule_id,
                AvailabilityRuleResourceClass.resource_class_code,
            )
        ).all()
        for rule_id, code in rows:
            # Preserve the complete SQL-ordered group from its first match.
            identity = (rule_id, code)
            if identity not in seen:
                seen.add(identity)
                grouped.setdefault(str(rule_id), []).append(str(code))
    return {
        rule_id: tuple(codes)
        for rule_id, codes in grouped.items()
    }

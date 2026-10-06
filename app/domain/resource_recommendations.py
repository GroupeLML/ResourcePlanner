from __future__ import annotations

from dataclasses import dataclass
import unicodedata


COMPETENCY_NOT_REQUIRED = "NOT_REQUIRED"
COMPETENCY_SATISFIED = "SATISFIED"
COMPETENCY_MISSING = "MISSING"
COMPETENCY_UNRESOLVED = "UNRESOLVED"

CAPACITY_PRUDENT_FULL = "PRUDENT_FULL"
CAPACITY_PRUDENT_PARTIAL = "PRUDENT_PARTIAL"
CAPACITY_TENTATIVE_ONLY = "TENTATIVE_ONLY"
CAPACITY_NONE = "NONE"

_EPSILON = 0.01


def _normalized_name(value: str) -> str:
    text = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return " ".join(text.casefold().split())


@dataclass(frozen=True, slots=True)
class RecommendationCandidate:
    """Candidate signals prepared by a repository for deterministic ranking."""

    resource_id: str
    resource_name: str
    preferred: bool
    class_match: bool
    competency_state: str
    missing_competency_count: int
    prudent_free: float
    free_after_confirmed: float
    tentative_hours: float
    required_hours: float


@dataclass(frozen=True, slots=True)
class RankedRecommendationCandidate:
    candidate: RecommendationCandidate
    category: int
    rank: int
    recommended: bool
    fallback_requires_confirmation: bool
    capacity_state: str


def recommendation_category(candidate: RecommendationCandidate) -> int:
    """Return the ADR-025 category for one candidate.

    Categories are deliberately ordinal.  No additive score may move a candidate
    across these business categories.
    """

    if not candidate.class_match or candidate.competency_state == COMPETENCY_UNRESOLVED:
        return 7

    skills_complete = candidate.competency_state in {
        COMPETENCY_NOT_REQUIRED,
        COMPETENCY_SATISFIED,
    }
    enough_prudent = (
        candidate.prudent_free + _EPSILON >= max(candidate.required_hours, 0.0)
    )

    if enough_prudent:
        if skills_complete:
            return 1 if candidate.preferred else 2
        return 3

    if candidate.prudent_free > _EPSILON:
        return 4 if skills_complete else 5

    return 6


def capacity_state(candidate: RecommendationCandidate) -> str:
    required = max(candidate.required_hours, 0.0)
    if candidate.prudent_free + _EPSILON >= required:
        return CAPACITY_PRUDENT_FULL
    if candidate.prudent_free > _EPSILON:
        return CAPACITY_PRUDENT_PARTIAL
    if candidate.free_after_confirmed > _EPSILON:
        return CAPACITY_TENTATIVE_ONLY
    return CAPACITY_NONE


def rank_recommendation_candidates(
    candidates: tuple[RecommendationCandidate, ...],
) -> tuple[RankedRecommendationCandidate, ...]:
    """Rank candidates according to ADR-025, with deterministic tie-breaking."""

    decorated = tuple(
        (recommendation_category(candidate), candidate)
        for candidate in candidates
    )
    ordered = sorted(
        decorated,
        key=lambda item: (
            item[0],
            max(int(item[1].missing_competency_count), 0),
            -float(item[1].prudent_free),
            float(item[1].tentative_hours),
            _normalized_name(item[1].resource_name),
            item[1].resource_id,
        ),
    )

    result: list[RankedRecommendationCandidate] = []
    for index, (category, candidate) in enumerate(ordered, start=1):
        result.append(
            RankedRecommendationCandidate(
                candidate=candidate,
                category=category,
                rank=index,
                recommended=index == 1 and category in {1, 2},
                fallback_requires_confirmation=index == 1 and category == 3,
                capacity_state=capacity_state(candidate),
            )
        )
    return tuple(result)

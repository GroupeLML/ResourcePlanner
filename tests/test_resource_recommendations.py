from __future__ import annotations

import unittest

from app.domain.resource_recommendations import (
    CAPACITY_NONE,
    CAPACITY_PRUDENT_FULL,
    CAPACITY_PRUDENT_PARTIAL,
    CAPACITY_TENTATIVE_ONLY,
    COMPETENCY_MISSING,
    COMPETENCY_SATISFIED,
    COMPETENCY_UNRESOLVED,
    RecommendationCandidate,
    rank_recommendation_candidates,
)


def candidate(
    resource_id: str,
    *,
    name: str | None = None,
    preferred: bool = False,
    class_match: bool = True,
    competency_state: str = COMPETENCY_SATISFIED,
    missing: int = 0,
    prudent: float = 16.0,
    free_after_confirmed: float | None = None,
    tentative: float = 0.0,
    required: float = 8.0,
) -> RecommendationCandidate:
    return RecommendationCandidate(
        resource_id=resource_id,
        resource_name=name or resource_id,
        preferred=preferred,
        class_match=class_match,
        competency_state=competency_state,
        missing_competency_count=missing,
        prudent_free=prudent,
        free_after_confirmed=(
            prudent if free_after_confirmed is None else free_after_confirmed
        ),
        tentative_hours=tentative,
        required_hours=required,
    )


class ResourceRecommendationRankingTests(unittest.TestCase):
    def test_categories_follow_adr_025_without_preference_bypass(self) -> None:
        rows = rank_recommendation_candidates(
            (
                candidate("preferred-full", preferred=True),
                candidate("full"),
                candidate(
                    "missing-full",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                ),
                candidate("skilled-partial", prudent=4.0),
                candidate(
                    "missing-partial",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                    prudent=4.0,
                ),
                candidate("no-prudent", prudent=0.0),
                candidate("wrong-class", class_match=False, prudent=40.0),
                candidate(
                    "preferred-missing",
                    preferred=True,
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                ),
                candidate(
                    "unresolved",
                    competency_state=COMPETENCY_UNRESOLVED,
                    missing=1,
                    prudent=40.0,
                ),
            )
        )

        self.assertEqual(
            [(row.candidate.resource_id, row.category) for row in rows],
            [
                ("preferred-full", 1),
                ("full", 2),
                ("missing-full", 3),
                ("preferred-missing", 3),
                ("skilled-partial", 4),
                ("missing-partial", 5),
                ("no-prudent", 6),
                ("wrong-class", 7),
                ("unresolved", 7),
            ],
        )
        self.assertTrue(rows[0].recommended)
        self.assertFalse(rows[7].recommended)

    def test_category_three_is_fallback_only_when_no_full_candidate_exists(self) -> None:
        with_full = rank_recommendation_candidates(
            (
                candidate("full"),
                candidate(
                    "fallback",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                ),
            )
        )
        self.assertFalse(with_full[1].fallback_requires_confirmation)

        fallback_only = rank_recommendation_candidates(
            (
                candidate(
                    "fallback",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                ),
                candidate("partial", prudent=4.0),
            )
        )
        self.assertEqual(fallback_only[0].category, 3)
        self.assertFalse(fallback_only[0].recommended)
        self.assertTrue(fallback_only[0].fallback_requires_confirmation)

    def test_tie_break_is_missing_capacity_tentative_name_then_id(self) -> None:
        rows = rank_recommendation_candidates(
            (
                candidate(
                    "id-z",
                    name="Émile",
                    competency_state=COMPETENCY_MISSING,
                    missing=2,
                    prudent=20.0,
                ),
                candidate(
                    "id-b",
                    name="Zoé",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                    prudent=20.0,
                    tentative=2.0,
                ),
                candidate(
                    "id-a",
                    name="Álix",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                    prudent=20.0,
                    tentative=2.0,
                ),
                candidate(
                    "id-c",
                    name="Alix",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                    prudent=20.0,
                    tentative=2.0,
                ),
                candidate(
                    "id-d",
                    name="Zed",
                    competency_state=COMPETENCY_MISSING,
                    missing=1,
                    prudent=24.0,
                    tentative=4.0,
                ),
            )
        )
        self.assertEqual(
            [row.candidate.resource_id for row in rows],
            ["id-d", "id-a", "id-c", "id-b", "id-z"],
        )

    def test_capacity_diagnostics_distinguish_prudent_partial_tentative_and_none(self) -> None:
        rows = rank_recommendation_candidates(
            (
                candidate("full", prudent=8.0, free_after_confirmed=8.0),
                candidate("partial", prudent=4.0, free_after_confirmed=4.0),
                candidate("tentative", prudent=0.0, free_after_confirmed=8.0),
                candidate("none", prudent=0.0, free_after_confirmed=0.0),
            )
        )
        by_id = {row.candidate.resource_id: row.capacity_state for row in rows}
        self.assertEqual(by_id["full"], CAPACITY_PRUDENT_FULL)
        self.assertEqual(by_id["partial"], CAPACITY_PRUDENT_PARTIAL)
        self.assertEqual(by_id["tentative"], CAPACITY_TENTATIVE_ONLY)
        self.assertEqual(by_id["none"], CAPACITY_NONE)


if __name__ == "__main__":
    unittest.main()

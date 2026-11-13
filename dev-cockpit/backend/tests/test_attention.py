import unittest

from app.attention import build_attention_center


def handoff(**overrides):
    packs = {
        "product-owner": {"confidence": "COMPLETE", "missing": []},
        "developer": {"confidence": "COMPLETE", "missing": []},
        "architect": {"confidence": "COMPLETE", "missing": []},
        "reviewer": {"confidence": "COMPLETE", "missing": []},
        "generic": {"confidence": "COMPLETE", "missing": []},
    }
    for role, value in overrides.items():
        packs[role] = value
    return {"active_key": "276A", "packs": packs}


def execution(
    phase,
    *,
    role="developer",
    summary="Résumé",
    action="Action attendue",
    link=None,
):
    return {
        "phase": phase,
        "label": phase,
        "summary": summary,
        "next_action": action,
        "responsible_role": role,
        "primary_link": link,
        "missions": {},
    }


class AttentionCenterTests(unittest.TestCase):
    def test_ci_red_is_single_action_for_developer(self):
        result = build_attention_center(
            execution=execution(
                "CI_RED",
                summary="276A a une CI en échec.",
                action="Corriger la CI.",
                link={"label": "PR #440", "url": "https://github.test/pull/440"},
            ),
            reconciliation={
                "status": "coherent",
                "summary": "Aucun écart.",
                "findings": [],
                "proposal": None,
            },
            handoff=handoff(),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(result["status"], "ACTION")
        self.assertEqual(result["action_count"], 1)
        self.assertEqual(result["watch_count"], 0)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["level"], "ACTION")
        self.assertEqual(item["role"], "developer")
        self.assertEqual(item["key"], "276A")
        self.assertEqual(item["title"], "CI rouge")
        self.assertEqual(item["primary_link"]["label"], "PR #440")

    def test_ready_to_merge_is_action_for_reviewer(self):
        result = build_attention_center(
            execution=execution(
                "READY_TO_MERGE",
                role="reviewer",
                summary="PR verte et mergeable.",
                action="Vérifier puis fusionner.",
            ),
            reconciliation={
                "status": "coherent",
                "findings": [],
                "proposal": None,
            },
            handoff=handoff(),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(result["items"][0]["role"], "reviewer")
        self.assertEqual(result["items"][0]["level"], "ACTION")
        self.assertEqual(result["roles"]["reviewer"]["actions"], 1)

    def test_stale_roadmap_is_action_for_product_owner(self):
        result = build_attention_center(
            execution=execution(
                "ROADMAP_UPDATE_REQUIRED",
                role="product-owner",
                summary="Le roadmap est en retard.",
                action="Réconcilier #55.",
            ),
            reconciliation={
                "status": "stale",
                "findings": [
                    {
                        "code": "ready_merged_green",
                        "key": "276A",
                        "message": "Fusion vérifiée.",
                        "evidence": [],
                    }
                ],
                "proposal": {"changes": []},
            },
            handoff=handoff(),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(result["action_count"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["role"], "product-owner")
        self.assertEqual(result["items"][0]["title"], "Roadmap à réconcilier")

    def test_architecture_gate_is_action_for_architect(self):
        result = build_attention_center(
            execution=execution(
                "ARCHITECTURE_GATE",
                role="architect",
                action="Compléter la gate.",
            ),
            reconciliation={"status": "coherent", "findings": [], "proposal": None},
            handoff=handoff(),
            active_work={"key": "ASTRA-362"},
            pipeline_now={"key": "ASTRA-362"},
        )

        self.assertEqual(result["status"], "ACTION")
        self.assertEqual(result["items"][0]["role"], "architect")

    def test_ci_running_and_possible_stall_are_watch(self):
        for phase in ("CI_RUNNING", "POSSIBLE_STALL"):
            with self.subTest(phase=phase):
                result = build_attention_center(
                    execution=execution(phase),
                    reconciliation={
                        "status": "coherent",
                        "findings": [],
                        "proposal": None,
                    },
                    handoff=handoff(),
                    active_work={"key": "276A"},
                    pipeline_now={"key": "276A"},
                )
                self.assertEqual(result["status"], "WATCH")
                self.assertEqual(result["watch_count"], 1)
                self.assertEqual(result["items"][0]["level"], "WATCH")

    def test_blocked_open_pr_is_watch_for_product_owner(self):
        result = build_attention_center(
            execution=execution("DEVELOPING"),
            reconciliation={
                "status": "attention",
                "summary": "Écart.",
                "proposal": None,
                "findings": [
                    {
                        "code": "blocked_open_pr",
                        "key": "410",
                        "message": "410 est BLOCKED mais une PR est ouverte.",
                        "evidence": [
                            {
                                "label": "PR #441",
                                "url": "https://github.test/pull/441",
                            }
                        ],
                    }
                ],
            },
            handoff=handoff(),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(result["status"], "WATCH")
        self.assertEqual(result["watch_count"], 2)
        by_key = {item["key"]: item for item in result["items"]}
        self.assertEqual(by_key["410"]["role"], "product-owner")
        self.assertEqual(by_key["410"]["code"], "blocked_open_pr")

    def test_split_source_reconciliation_actions_target_canonical_pipeline(self):
        result = build_attention_center(
            execution=execution("DEVELOPING"),
            reconciliation={
                "status": "attention",
                "summary": "Écart.",
                "proposal": None,
                "findings": [
                    {
                        "code": "blocked_open_pr",
                        "key": "410",
                        "message": "410 est BLOCKED mais une PR est ouverte.",
                        "evidence": [],
                    },
                    {
                        "code": "blocked_merged_pr",
                        "key": "411",
                        "message": "411 est BLOCKED mais déjà fusionné.",
                        "evidence": [],
                    },
                    {
                        "code": "ready_merged_unverified",
                        "key": "412",
                        "message": "412 est fusionné sans preuve complète.",
                        "evidence": [],
                    },
                ],
            },
            handoff=handoff(),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
            canonical_pipeline_issue=851,
        )
        by_key = {item["key"]: item for item in result["items"]}
        self.assertIn("pipeline #851", by_key["410"]["action"])
        self.assertIn("pipeline #851", by_key["411"]["action"])
        self.assertIn("pipeline #851", by_key["412"]["action"])
        self.assertNotIn("#55", by_key["410"]["action"])
        self.assertNotIn("#55", by_key["411"]["action"])
        self.assertNotIn("#55", by_key["412"]["action"])

    def test_partial_handoff_is_deduplicated_by_role_and_subject(self):
        result = build_attention_center(
            execution=execution("CI_RUNNING"),
            reconciliation={
                "status": "coherent",
                "findings": [],
                "proposal": None,
            },
            handoff=handoff(
                developer={
                    "confidence": "PARTIAL",
                    "missing": ["PR active"],
                }
            ),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["code"], "execution_ci_running")
        self.assertEqual(result["items"][0]["handoff_confidence"], "PARTIAL")

    def test_partial_handoff_adds_watch_when_role_has_no_stronger_signal(self):
        result = build_attention_center(
            execution=execution("READY", role="developer"),
            reconciliation={
                "status": "coherent",
                "findings": [],
                "proposal": None,
            },
            handoff=handoff(
                reviewer={
                    "confidence": "PARTIAL",
                    "missing": ["PR à réviser"],
                }
            ),
            active_work={"key": "276A"},
            pipeline_now={"key": "276A"},
        )

        self.assertEqual(result["status"], "ACTION")
        self.assertEqual(result["action_count"], 1)
        self.assertEqual(result["watch_count"], 1)
        roles = {(item["role"], item["level"]) for item in result["items"]}
        self.assertIn(("developer", "ACTION"), roles)
        self.assertIn(("reviewer", "WATCH"), roles)

    def test_no_signals_is_clear(self):
        result = build_attention_center(
            execution=execution("NO_ACTIVE_WORK", role="developer"),
            reconciliation={
                "status": "coherent",
                "findings": [],
                "proposal": None,
            },
            handoff=handoff(),
            active_work=None,
            pipeline_now=None,
        )

        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["summary"], "Aucune intervention requise.")


if __name__ == "__main__":
    unittest.main()

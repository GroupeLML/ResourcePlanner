import unittest
from datetime import datetime, timezone

from app.flow_analytics import (
    _trend,
    build_flow_analytics,
)


ROADMAP = """
# Roadmap

<!-- COCKPIT_PIPELINE_V1 -->
KEY | TYPE | STATUS | PARENT | LANE | TITLE
101A | WORK | DONE | #101 | MAIN | first delivery
101B | WORK | DONE | #101 | MAIN | second delivery
101C | WORK | DONE | #101 | MAIN | docs only delivery
101D | WORK | READY | #101 | MAIN | current work
<!-- /COCKPIT_PIPELINE_V1 -->
"""


class FakeGitHub:
    def __init__(self):
        self.issue = {
            "number": 55,
            "body": ROADMAP,
            "updated_at": "2026-09-25T12:00:00Z",
        }
        self.closed = [
            {
                "number": 501,
                "title": "feat(101A): first delivery",
                "body": "Refs #101A",
                "head": {"ref": "feat/101a", "sha": "sha-a2"},
                "merged_at": "2026-09-25T10:40:00Z",
                "created_at": "2026-09-25T10:05:00Z",
                "html_url": "https://github.test/pull/501",
            },
            {
                "number": 502,
                "title": "feat(101B): second delivery",
                "body": "Refs #101B",
                "head": {"ref": "feat/101b", "sha": "sha-b1"},
                "merged_at": "2026-09-25T11:30:00Z",
                "created_at": "2026-09-25T11:00:00Z",
                "html_url": "https://github.test/pull/502",
            },
            {
                "number": 503,
                "title": "docs(101C): explain delivery",
                "body": "Refs #101C",
                "head": {"ref": "docs/101c", "sha": "sha-c1"},
                "merged_at": "2026-09-25T11:40:00Z",
                "created_at": "2026-09-25T11:35:00Z",
                "html_url": "https://github.test/pull/503",
            },
            {
                "number": 504,
                "title": "feat(101A): old unrelated wording",
                "body": "101B is intentionally out of scope.",
                "head": {"ref": "feat/101a-old", "sha": "sha-old"},
                "merged_at": "2026-09-25T09:00:00Z",
                "created_at": "2026-09-25T08:30:00Z",
                "html_url": "https://github.test/pull/504",
            },
        ]
        self.files = {
            501: [{"filename": "app/first.py"}],
            502: [{"filename": "app/second.py"}],
            503: [{"filename": "docs/101C.md"}],
            504: [{"filename": "app/old.py"}],
        }
        self.details = {
            number: {
                **pr,
                "state": "closed",
                "merged": True,
                "draft": False,
                "head": {
                    "ref": pr["head"]["ref"],
                    "sha": pr["head"]["sha"],
                },
                "base": {"ref": "main"},
            }
            for number, pr in [(pr["number"], pr) for pr in self.closed]
        }
        self.commits = {
            501: [
                {
                    "sha": "sha-a1",
                    "commit": {
                        "author": {"date": "2026-09-25T10:00:00Z"},
                        "committer": {"date": "2026-09-25T10:00:00Z"},
                    },
                },
                {
                    "sha": "sha-a2",
                    "commit": {
                        "author": {"date": "2026-09-25T10:20:00Z"},
                        "committer": {"date": "2026-09-25T10:20:00Z"},
                    },
                },
            ],
            502: [
                {
                    "sha": "sha-b1",
                    "commit": {
                        "author": {"date": "2026-09-25T10:55:00Z"},
                        "committer": {"date": "2026-09-25T10:55:00Z"},
                    },
                }
            ],
            504: [],
        }
        self.runs = {
            "sha-a1": [
                {
                    "id": 9001,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": "2026-09-25T10:06:00Z",
                    "updated_at": "2026-09-25T10:10:00Z",
                    "html_url": "https://github.test/actions/9001",
                }
            ],
            "sha-a2": [
                {
                    "id": 9002,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "created_at": "2026-09-25T10:25:00Z",
                    "updated_at": "2026-09-25T10:30:00Z",
                    "html_url": "https://github.test/actions/9002",
                }
            ],
            "sha-b1": [],
        }
        self.jobs = {
            9001: [
                {
                    "name": "backend tests",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        }

    def validate_repo(self, repo):
        return repo

    async def get_issue(self, repo, number):
        return self.issue

    async def list_pulls(self, repo, state="open", per_page=20):
        return self.closed if state == "closed" else []

    async def list_pull_files(self, repo, number, **kwargs):
        return self.files.get(number, [])

    async def get_pull(self, repo, number):
        return self.details[number]

    async def list_pull_commits(self, repo, number, **kwargs):
        return self.commits.get(number, [])

    async def workflow_runs_for_sha(self, repo, sha, per_page=10):
        return self.runs.get(sha, [])

    async def run_jobs(self, repo, run_id):
        return self.jobs.get(run_id, [])


class FlowAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_mapping_docs_only_and_observable_durations(self):
        report = await build_flow_analytics(
            FakeGitHub(),
            "tchi99/RessourcePlanner",
            55,
            12,
        )

        self.assertEqual(report["status"], "partial")
        by_key = {row["key"]: row for row in report["deliveries"]}

        first = by_key["101A"]
        self.assertEqual(first["pr"]["number"], 501)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["metrics"]["commit_to_pr_minutes"], 5.0)
        self.assertEqual(first["metrics"]["pr_to_green_minutes"], 25.0)
        self.assertEqual(first["metrics"]["green_to_merge_minutes"], 10.0)
        self.assertEqual(first["metrics"]["total_observed_minutes"], 40.0)
        self.assertEqual(first["metrics"]["validation_attempts"], 2)
        self.assertEqual(first["metrics"]["red_attempts"], 1)
        self.assertEqual(first["metrics"]["red_recovery_minutes"], 15.0)
        self.assertEqual(first["attempts"][0]["failed_jobs"], ["backend tests"])
        self.assertEqual(first["bottleneck"]["segment"], "pr_to_green_minutes")

        second = by_key["101B"]
        self.assertEqual(second["pr"]["number"], 502)
        self.assertEqual(second["status"], "partial")
        self.assertIsNone(second["metrics"]["pr_to_green_minutes"])
        self.assertIn("aucun workflow", " ".join(second["diagnostics"]).lower())

        docs = by_key["101C"]
        self.assertEqual(docs["status"], "unavailable")
        self.assertIsNone(docs["pr"])
        self.assertIn("documentation", docs["reason"].lower())

        # PR #504 mentions 101B only in an out-of-scope sentence; it must not
        # become the delivery identity for 101B.
        self.assertNotEqual(second["pr"]["number"], 504)

    async def test_post_cutover_analytics_reads_issue_851_in_strict_mode(self):
        fake = FakeGitHub()
        fake.issue["number"] = 851
        report = await build_flow_analytics(
            fake,
            "tchi99/RessourcePlanner",
            851,
            12,
            strict_source=True,
        )
        self.assertEqual(report["pipeline_issue"], 851)
        self.assertNotEqual(report["status"], "unavailable")
        self.assertTrue(
            any("réconciliation #851" in note for note in report["notes"])
        )

    async def test_no_branch_creation_or_reconciliation_timestamp_is_invented(self):
        report = await build_flow_analytics(
            FakeGitHub(),
            "tchi99/RessourcePlanner",
            55,
            12,
        )
        serialized = str(report)
        self.assertNotIn("branch_created", serialized)
        self.assertNotIn("roadmap_reconciliation_at", serialized)
        self.assertTrue(
            any("réconciliation #55" in note for note in report["notes"])
        )

    async def test_legacy_pipeline_is_explicitly_unavailable(self):
        fake = FakeGitHub()
        fake.issue["body"] = "# Roadmap legacy\n- 🟡 #101 active"
        report = await build_flow_analytics(
            fake,
            "tchi99/RessourcePlanner",
            55,
            12,
        )
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("COCKPIT_PIPELINE_V1", report["reason"])
        self.assertEqual(report["deliveries"], [])

    def test_trend_requires_six_comparable_deliveries(self):
        five = [
            {
                "metrics": {"pr_to_green_minutes": float(value)},
                "pr": {"merged_at": f"2026-09-2{index}T10:00:00Z"},
            }
            for index, value in enumerate([30, 28, 25, 20, 18], start=0)
        ]
        self.assertIsNone(_trend(five))

        six = five + [
            {
                "metrics": {"pr_to_green_minutes": 15.0},
                "pr": {"merged_at": "2026-09-25T10:00:00Z"},
            }
        ]
        trend = _trend(six)
        self.assertIsNotNone(trend)
        self.assertEqual(trend["sample_size"], 6)
        self.assertLess(trend["recent_median"], trend["older_median"])
        self.assertIn("plus rapide", trend["description"])


if __name__ == "__main__":
    unittest.main()

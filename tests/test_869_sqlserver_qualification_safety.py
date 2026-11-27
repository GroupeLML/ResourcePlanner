"""Offline Q2a safety contracts: no application, DBAPI or SQL Server fixtures."""
from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import qualification_869_safety as s
from tools import qualify_869_sqlserver as cli


NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
ROW_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"


def approval():
    return dict(schema_version=1, source_head="a"*40, source_tree="b"*40,
                protected=dict(s.PROTECTED),
                target=dict(server="fixture-host", database="q869_test", classification="DEDICATED_ISOLATED"),
                routing=dict(host="fixture-host", port=None),
                prerequisites=dict(version="14.0.3445.2", alembic_revision="revision", driver="driver-build"),
                mode="WORKLOAD", window=dict(start="2030-01-01T11:55:00Z", end="2030-01-01T12:35:00Z"),
                recovery_until="2030-01-01T12:45:00Z", manifest_digest=None,
                grants=[dict(scenario="A06", tier="T0")],
                fixture_limits={"T0": {"Project": 2}},
                limits=dict(s.CANDIDATE_LIMITS),
                performance={k: dict(metric=v[0], samples=v[1], target=v[2], maximum=v[3])
                             for k, v in s.CANDIDATE_PERFORMANCE.items()},
                monitoring=dict(authority="IT-01", reserved=True, monitoring_available=True, recovery_authorized=True),
                operator="operator-01", approval_ref="PO-01", it_approval_ref="IT-01",
                recovery_approval_ref="REC-01", capacity_tiers={}, deadlock_approval=None)


def plan(a):
    return dict(schema_version=1, run_id=RUN_ID, source_head=a["source_head"],
                target=copy.deepcopy(a["target"]), tier="T0", scenarios=["A06"],
                rows=[dict(identity=dict(model="Project", key=dict(id=ROW_ID)),
                           alternate_keys=[dict(number="Q869-project")],
                           expected_row_digest=s.digest({"synthetic": True}), references=[])])


def source(a):
    return dict(head=a["source_head"], tree=a["source_tree"], clean=True, protected=dict(s.PROTECTED))


def env():
    return {s.OPT_ENV: s.OPT_IN, s.URL_ENV: "mssql+pyodbc://fixture-host/q869_test"}


def preflight(a):
    return dict(target=copy.deepcopy(a["target"]), version=a["prerequisites"]["version"],
                edition="Standard", compatibility=140, collation="SQL_Latin1_General_CP1_CI_AI",
                snapshot="ON", rcsi=False, recovery="FULL", alembic_revision="revision",
                python="3.12.14", sqlalchemy="2.0.52", pyodbc="5.3.0", alembic="1.19.1",
                driver="driver-build", constraints_verified=True, permissions_verified=True,
                reservation_verified=True, monitoring_verified=True, no_consumers=True,
                no_operational_records=True)


def telemetry():
    return dict(sql_cpu=30, sql_cpu_baseline=25, app_cpu=20, memory_mib=2048,
                disk_free_percent=60, disk_free_mib=8192, tempdb_free_percent=60,
                tempdb_free_mib=8192, version_store_growth_mib=0, log_percent=20,
                blocking_seconds=0, external_activity=False, available=True)


class QualificationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.a = approval()
        self.p = plan(self.a)
        self.a["manifest_digest"] = s.digest(self.p)
        self.key = s.row_key(self.p["rows"][0]["identity"])
        self.absent = {self.key: dict(exists=False, actual_digest=None, alternate_collision=False,
                                     unexpected_references=False, independent=True, transaction_confirmed=False)}
        self.present = {self.key: dict(self.absent[self.key], exists=True,
                                       actual_digest=self.p["rows"][0]["expected_row_digest"],
                                       transaction_confirmed=True)}

    def committed(self):
        j = s.initial_journal(self.p)
        j = s.transition(self.p, j, "SETUP_PENDING", pending=[self.key], observations=self.absent)
        return s.transition(self.p, j, "COMMITTED", confirmed=[self.key], independent=True,
                            observations=self.present)

    def controller(self):
        c = s.SafetyController(self.a)
        self.assertTrue(c.sample(telemetry(), NOW))
        return c

    def test_01_dedicated_opt_in(self):
        for e in ({}, {s.URL_ENV: env()[s.URL_ENV]}, dict(env(), **{s.OPT_ENV: "yes"})):
            with self.assertRaises(s.SafetyError):
                s.validate_configuration(e, self.a["target"], self.a["routing"])
        s.validate_configuration(env(), self.a["target"], self.a["routing"])

    def test_02_generic_and_conflicting_config(self):
        for name in ("RESOURCEPLANNER_DATABASE_URL", "RESOURCEPLANNER_SQLSERVER_TEST_URL", "DB_URI"):
            with self.assertRaises(s.SafetyError):
                s.validate_configuration(dict(env(), **{name: env()[s.URL_ENV]}))

    def test_03_routing_and_opaque_options(self):
        for value in ("mssql+pyodbc://wrong-host/q869_test",
                      "mssql+pyodbc://fixture-host:1433/q869_test",
                      "mssql+pyodbc://fixture-host/other",
                      "mssql+pyodbc://fixture-host/q869_test?odbc_connect=opaque",
                      "mssql+pyodbc://fixture-host/q869_test?driver=x%3BDatabase%3Dother"):
            with self.assertRaises(s.SafetyError):
                s.validate_configuration(dict(env(), **{s.URL_ENV: value}), self.a["target"], self.a["routing"])

    def test_04_wrong_source_dirty_and_protected(self):
        s.verify_source(self.a, source(self.a))
        for change in (dict(head="c"*40), dict(tree="d"*40), dict(clean=False), dict(protected={})):
            with self.assertRaises(s.SafetyError):
                s.verify_source(self.a, dict(source(self.a), **change))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(s.SafetyError):
                s.observe_source(root)

    def test_05_strict_approval_and_duplicate_json(self):
        for change in (dict(schema_version=True), dict(extra=True), dict(operator=""),
                       dict(protected={}), dict(recovery_approval_ref=None)):
            with self.assertRaises(s.SafetyError):
                s.validate_approval(dict(self.a, **change))
        with tempfile.TemporaryDirectory() as root:
            f = Path(root)/"approval.json"
            f.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            with self.assertRaises(s.SafetyError):
                s.read_json(f)

    def test_06_expired_workload_and_recovery(self):
        a = s.validate_approval(self.a)
        s.authorize_window(a, NOW)
        after = NOW + timedelta(minutes=36)
        with self.assertRaises(s.SafetyError):
            s.authorize_window(a, after)
        s.authorize_window(a, after, recovery=True)
        with self.assertRaises(s.SafetyError):
            s.authorize_window(a, NOW+timedelta(minutes=46), recovery=True)
        a["manifest_digest"] = None
        with self.assertRaises(s.SafetyError):
            s.authorize_window(a, after, recovery=True)

    def test_07_prohibited_target(self):
        for name in ("rplanner", "RPLANNER_DEV", "master", "tempdb"):
            with self.assertRaises(s.SafetyError):
                s.validate_target(dict(self.a["target"], database=name))
        with self.assertRaises(s.SafetyError):
            s.validate_target(dict(self.a["target"], classification="POPULATED_DEV"))

    def test_08_actual_identity_and_prerequisites(self):
        s.validate_preflight(self.a, preflight(self.a))
        for change in (dict(target=dict(self.a["target"], server="other")), dict(snapshot="OFF"),
                       dict(rcsi=True), dict(compatibility=150), dict(driver="other"),
                       dict(alembic_revision="other"), dict(pyodbc="5.2.0")):
            with self.assertRaises(s.SafetyError):
                s.validate_preflight(self.a, dict(preflight(self.a), **change))
        p = preflight(self.a)
        del p["constraints_verified"]
        with self.assertRaises(s.SafetyError):
            s.validate_preflight(self.a, p)

    def test_09_monitoring_and_reservation(self):
        for key in ("reserved", "monitoring_available", "recovery_authorized"):
            a = copy.deepcopy(self.a)
            a["monitoring"][key] = False
            with self.assertRaises(s.SafetyError):
                s.validate_approval(a)
        for key in ("monitoring_verified", "reservation_verified", "no_consumers", "no_operational_records", "permissions_verified"):
            with self.assertRaises(s.SafetyError):
                s.validate_preflight(self.a, dict(preflight(self.a), **{key: False}))

    def test_10_manifest_binding(self):
        self.assertEqual(s.validate_plan(self.a, self.p), {self.key})
        p = copy.deepcopy(self.p)
        p["rows"][0]["expected_row_digest"] = "d"*64
        with self.assertRaisesRegex(s.SafetyError, "PLAN_DIGEST_MISMATCH"):
            s.validate_plan(self.a, p)
        s.validate_plan(self.a, p, bound=False)

    def test_11_model_key_and_fixture_limits(self):
        for identity in (dict(model="Anything", key=dict(id=ROW_ID)),
                         dict(model="Project", key=dict(number="Q869-project")),
                         dict(model="Project", key=dict(id="not-a-uuid"))):
            with self.assertRaises(s.SafetyError):
                s.row_key(identity)
        a = copy.deepcopy(self.a)
        a["fixture_limits"]["T0"] = {"Resource": 1}
        with self.assertRaisesRegex(s.SafetyError, "FIXTURE_LIMIT"):
            s.validate_plan(a, self.p, bound=False)

    def test_12_relationships_and_alternate_keys(self):
        p = copy.deepcopy(self.p)
        p["rows"][0]["references"] = [dict(column="project_manager_contact_id",
                                            identity=dict(model="BusinessContact", key=dict(id=ROW_ID)))]
        with self.assertRaisesRegex(s.SafetyError, "UNOWNED_REFERENCE"):
            s.validate_plan(self.a, p, bound=False)
        p = copy.deepcopy(self.p)
        p["rows"][0]["alternate_keys"] = []
        with self.assertRaisesRegex(s.SafetyError, "MISSING_ALTERNATE_KEY"):
            s.validate_plan(self.a, p, bound=False)

    def test_13_collision_before_setup(self):
        j = s.initial_journal(self.p)
        with self.assertRaises(s.SafetyError):
            s.transition(self.p, j, "SETUP_PENDING", pending=[self.key], observations=self.present)
        o = copy.deepcopy(self.absent)
        o[self.key]["alternate_collision"] = True
        with self.assertRaises(s.SafetyError):
            s.reconcile(self.p, j, o)

    def test_14_unconfirmed_ownership(self):
        j = s.initial_journal(self.p)
        j = s.transition(self.p, j, "SETUP_PENDING", pending=[self.key], observations=self.absent)
        for change in (dict(transaction_confirmed=False), dict(independent=False), dict(actual_digest="f"*64)):
            o = {self.key: dict(self.present[self.key], **change)}
            with self.assertRaises(s.SafetyError):
                s.transition(self.p, j, "COMMITTED", confirmed=[self.key], independent=True, observations=o)
        with self.assertRaises(s.SafetyError):
            s.reconcile(self.p, j, self.present)

    def test_15_unexpected_reference_or_changed_row(self):
        j = self.committed()
        for change in (dict(unexpected_references=True), dict(actual_digest="f"*64), dict(independent=False)):
            with self.assertRaises(s.SafetyError):
                s.reconcile(self.p, j, {self.key: dict(self.present[self.key], **change)})
        self.assertEqual(s.reconcile(self.p, j, self.present), {self.key})

    def test_16_journal_transitions_and_non_owned_intent(self):
        j = s.initial_journal(self.p)
        with self.assertRaises(s.SafetyError):
            s.transition(self.p, j, "RUNNING")
        with self.assertRaises(s.SafetyError):
            s.transition(self.p, j, "SETUP_PENDING", pending=["f"*64], observations=self.absent)
        j = self.committed()
        j = s.transition(self.p, j, "RUNNING", confirmed=[self.key])
        j = s.transition(self.p, j, "CLEANUP_PENDING", confirmed=[self.key], pending=[self.key],
                         independent=True, observations=self.present)
        j = s.transition(self.p, j, "VERIFIED_CLEAN", independent=True, observations=self.absent)
        self.assertEqual(j["state"], "VERIFIED_CLEAN")
        s.validate_journal(self.p, j)

    def test_17_atomic_journal_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"journal.json"
            j = s.store_journal(path, self.p)
            self.assertEqual(s.read_json(path), j)
            nxt = s.store_journal(path, self.p, previous=j, state="SETUP_PENDING",
                                  pending=[self.key], observations=self.absent)
            self.assertEqual(s.read_json(path), nxt)
            with self.assertRaises(s.SafetyError):
                s.store_journal(path, self.p, previous=j, state="SETUP_PENDING",
                                pending=[self.key], observations=self.absent)
            self.assertEqual(s.read_json(path), nxt)

    def test_18_interrupted_setup_is_terminal_uncertain(self):
        j = s.transition(self.p, s.initial_journal(self.p), "SETUP_PENDING", pending=[self.key],
                         observations=self.absent)
        j = s.transition(self.p, j, "RECOVERY_REQUIRED", pending=[self.key])
        self.assertTrue(j["uncertain"])
        with self.assertRaises(s.SafetyError):
            s.reconcile(self.p, j, self.present)
        with self.assertRaises(s.SafetyError):
            s.transition(self.p, j, "CLEANUP_PENDING", independent=True, observations=self.present)

    def test_19_partial_cleanup_and_uncertain_release(self):
        j = s.transition(self.p, self.committed(), "CLEANUP_PENDING", confirmed=[self.key],
                         pending=[self.key], independent=True, observations=self.present)
        with self.assertRaises(s.SafetyError):
            s.transition(self.p, j, "VERIFIED_CLEAN", independent=True, observations=self.present)
        j = s.transition(self.p, j, "RECOVERY_REQUIRED", confirmed=[self.key], pending=[self.key])
        with self.assertRaises(s.SafetyError):
            s.reconcile(self.p, j, self.absent)

    def test_20_mutation_authorization_and_recovery_window(self):
        args = (self.a, self.p, source(self.a), preflight(self.a), env())
        s.authorize_mutation(*args, NOW)
        after = NOW+timedelta(minutes=36)
        with self.assertRaises(s.SafetyError):
            s.authorize_mutation(*args, after)
        s.authorize_mutation(*args, after, recovery=True)
        a = dict(self.a, mode="RECOVERY_ONLY")
        with self.assertRaises(s.SafetyError):
            s.authorize_mutation(a, self.p, source(a), preflight(a), env(), NOW)

    def test_21_concurrency_and_explicit_grants(self):
        c = self.controller()
        with self.assertRaises(s.SafetyError):
            c.admit("B01", "T0", NOW)
        with self.assertRaises(s.SafetyError):
            c.admit("A06", "T1", NOW)
        tickets = [c.admit("A06", "T0", NOW) for _ in range(3)]
        with self.assertRaisesRegex(s.SafetyError, "CONCURRENCY_LIMIT"):
            c.admit("A06", "T0", NOW)
        for ticket in tickets:
            c.finish(ticket, physical_release_confirmed=True)
        a = copy.deepcopy(self.a)
        a["grants"] = [dict(scenario="E10", tier="T0")]
        with self.assertRaisesRegex(s.SafetyError, "DEADLOCK_NOT_APPROVED"):
            s.validate_approval(a)

    def test_22_monitoring_loss_and_environmental_abort(self):
        c = self.controller()
        self.assertFalse(c.check_deadlines(NOW+timedelta(seconds=6)))
        self.assertEqual(c.reason, "MONITORING_LOST")
        c = self.controller()
        self.assertFalse(c.sample(dict(telemetry(), memory_mib=1), NOW+timedelta(seconds=5)))
        self.assertEqual(c.reason, "ENVIRONMENT_ABORT")
        with self.assertRaises(s.SafetyError):
            c.admit("A06", "T0", NOW+timedelta(seconds=5))

    def test_23_timeout_and_cancellation_bookkeeping(self):
        c = self.controller()
        ticket = c.admit("A06", "T0", NOW)
        c.finish(ticket, physical_release_confirmed=False, timeout=True)
        self.assertTrue(c.cancel_requested.is_set())
        self.assertTrue(c.uncertain)
        self.assertEqual(c.reason, "TIMEOUT")
        with self.assertRaises(s.SafetyError):
            c.admit("A06", "T0", NOW)

    def test_24_performance_and_abort_classification(self):
        rule = dict(metric="median", samples=3, target=8, maximum=12)
        self.assertEqual(s.performance_result(rule, [1, 7, 12]), "PASS")
        self.assertEqual(s.performance_result(rule, [1, 7, 13]), "FAIL")
        self.assertEqual(s.performance_result(rule, [1]), "NOT RUN")
        self.assertEqual(s.performance_result(rule, [1, 2, 3], environmental_abort=True), "ABORTED")
        record = s.performance_evidence(rule, [1, 7, 12])
        self.assertEqual((record["sample_count"], record["observed_metric_seconds"], record["observed_maximum_seconds"]), (3, 7, 12))

    def test_25_evidence_allowlist_and_redaction(self):
        record = s.run_record(self.a, self.p)
        text = json.dumps(record)
        for private in ("fixture-host", "q869_test", "operator-01", "IT-01"):
            self.assertNotIn(private, text)
        self.assertEqual(record["status"], "NOT RUN")
        for metrics in ({"raw_sql": "untrusted"}, {"elapsed_seconds": "untrusted"},
                        {"sample_count": True}):
            with self.assertRaises(s.SafetyError):
                s.evidence_record("A06", "T0", "PASS", metrics)
        self.assertEqual(s.sql_metric_record("CAN", RUN_ID, 900, 0.1)["total_binds"], 900)

    def test_26_cli_disabled_import_and_offline_plan(self):
        with patch.dict(os.environ, env(), clear=True), patch.object(s, "observe_source", side_effect=AssertionError("no source access")):
            for command in ("preflight", "run", "cleanup", "recover", "verify-clean"):
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main([command]), 3)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("LIVE_COMMAND_UNAVAILABLE", err.getvalue())
        with tempfile.TemporaryDirectory() as root:
            ap, mp = Path(root)/"approval.json", Path(root)/"manifest.json"
            ap.write_text(json.dumps(self.a), encoding="utf-8")
            mp.write_text(json.dumps(self.p), encoding="utf-8")
            with patch.dict(os.environ, env(), clear=True), patch.object(s, "observe_source", return_value=source(self.a)), patch.object(cli, "datetime") as clock:
                clock.now.return_value = NOW
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(["plan", "--approval-file", str(ap), "--manifest", str(mp)]), 0)
                self.assertEqual(json.loads(out.getvalue())["status"], "NOT RUN")
        child = subprocess.run([sys.executable, "-c",
                                "import sys; from tools import qualify_869_sqlserver; "
                                "assert 'sqlalchemy' not in sys.modules and 'pyodbc' not in sys.modules and 'app.server' not in sys.modules"],
                               capture_output=True, text=True, check=False,
                               cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, "")
        self.assertEqual(child.stderr, "")


if __name__ == "__main__":
    unittest.main()

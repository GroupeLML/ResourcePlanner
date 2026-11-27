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


def prior_review(ch):
    return dict(status="PASS", evidence_digest=ch*64, review_ref="REVIEW-01")


def approval():
    return dict(schema_version=2, source_head="a"*40, source_tree="b"*40,
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
                recovery_approval_ref="REC-01", capacity_tiers={}, deadlock_approval=None,
                environment=dict(reservation_ref="RES-01", reservation_until="2030-01-01T13:00:00Z",
                                 quiescence_ref="QUI-01", credential_scope_ref="CRED-01",
                                 backup_ref="BACKUP-01", backup_at="2030-01-01T10:00:00Z",
                                 backup_max_age_seconds=14400, log_backup_ref="LOG-01",
                                 restore_capability_ref="RESTORE-01", restore_evidence_kind="RESTORE_REHEARSAL",
                                 recovery_owner="IT-RECOVERY",
                                 recovery_procedure_ref="PROC-01"),
                scope=dict(week_start="2030-01-07", availability_digest="1"*64,
                           competency_digest="2"*64, background_digest="3"*64,
                           preservation_digest="4"*64,
                           expectations=[dict(scenario="A06", tier="T0", status="READY",
                                              expected_digest="5"*64, max_active=1, concurrency_ref=None)]),
                progression=dict(stage="T0", admission_ref="STAGE-01",
                                 prior_evidence={"PILOT": prior_review("6")}, monitoring_ref="MON-01", monitoring_connections=1,
                                 contention_ref=None),
                shared_limits=dict(io_latency_ms=100, prod_latency_ms=200, erp_latency_ms=200),
                probe_permit=None)


def plan(a):
    return dict(schema_version=2, run_id=RUN_ID, source_head=a["source_head"],
                target=copy.deepcopy(a["target"]), tier="T0", scenarios=["A06"],
                rows=[dict(identity=dict(model="Project", key=dict(id=ROW_ID)),
                           alternate_keys=[dict(number="Q869-project")],
                           expected_row_digest=s.digest({"synthetic": True}), references=[])], probe=None)


def source(a):
    return dict(head=a["source_head"], tree=a["source_tree"], clean=True, protected=dict(s.PROTECTED))


def env():
    return {s.OPT_ENV: s.OPT_IN, s.URL_ENV: "mssql+pyodbc://fixture-host/q869_test"}


def preflight(a):
    return dict(schema_version=2, approval_digest=s.digest(a), target=copy.deepcopy(a["target"]), version=a["prerequisites"]["version"],
                edition="Standard", compatibility=140, collation="SQL_Latin1_General_CP1_CI_AI",
                snapshot="ON", rcsi=False, recovery="FULL", alembic_revision="revision",
                python="3.12.14", sqlalchemy="2.0.52", pyodbc="5.3.0", alembic="1.19.1",
                driver="driver-build", constraints_verified=True, permissions_verified=True,
                reservation_verified=True, monitoring_verified=True, no_consumers=True,
                environment={key: True for key in s.ENV_FACTS},
                scope=dict({key: a["scope"][key] for key in s.SCOPE_DIGESTS + ("week_start",)},
                           **{key: True for key in s.SCOPE_FACTS},
                           expectations_digest=s.digest(a["scope"]["expectations"])),
                active_probe_count=0, probe_constraints_verified=True, probe_triggers_verified=True,
                probe_workflow_excluded=True, monitoring_connection_count=1)


def telemetry():
    return dict(sql_cpu=30, sql_cpu_baseline=25, app_cpu=20, memory_mib=2048,
                disk_free_percent=60, disk_free_mib=8192, tempdb_free_percent=60,
                tempdb_free_mib=8192, version_store_growth_mib=0, log_percent=20,
                blocking_seconds=0, external_activity=False, available=True,
                io_latency_ms=5, prod_latency_ms=20, erp_latency_ms=20,
                business_impact=False, ownership_uncertain=False, unexpected_consumers=False,
                **{key: approval()["scope"][key] for key in s.SCOPE_DIGESTS})


def configure_grant(a, scenario, tier="T0", *, cap=1):
    a["grants"] = [dict(scenario=scenario, tier=tier)]
    a["fixture_limits"] = {tier: {"Project": 2, "CommunicationBatchRow": 2}}
    a["scope"]["expectations"] = [dict(scenario=scenario, tier=tier, status="READY",
                                        expected_digest="5"*64, max_active=cap,
                                        concurrency_ref="CONC-01" if cap > 1 else None)]
    a["progression"]["stage"] = tier
    a["progression"]["prior_evidence"] = {"PILOT": prior_review("6")}
    if tier != "T0":
        a["progression"]["prior_evidence"]["T0"] = prior_review("7")
    if tier not in {"T0", "T1"}:
        a["progression"]["prior_evidence"]["T1"] = prior_review("8")


def populated():
    a = approval()
    a["target"] = dict(server="fixture-host", database="rplanner_dev", classification="POPULATED_DEV")
    p = plan(a)
    a["manifest_digest"] = s.digest(p)
    e = dict(env(), **{s.URL_ENV: "mssql+pyodbc://fixture-host/rplanner_dev"})
    return a, p, e


def probe_case(mode="COMMITTED_STORAGE_ONLY", scenario="F01"):
    a, p, e = populated()
    configure_grant(a, scenario)
    p["scenarios"] = [scenario]
    identity = dict(model="CommunicationBatchRow", key=dict(id=RUN_ID))
    p["rows"].append(dict(identity=identity, alternate_keys=[], references=[],
                          expected_row_digest=s.digest({"synthetic_probe": True})))
    p["probe"] = dict(mode=mode, identity=identity, **s.PROBE_VALUES)
    bind_probe(a, p)
    return a, p, e


def bind_probe(a, p):
    a["manifest_digest"] = s.digest(p)
    a["probe_permit"] = dict(mode=p["probe"]["mode"], po_ref="PO-PROBE", it_ref="IT-PROBE",
                             source_head=a["source_head"], source_tree=a["source_tree"],
                             target=copy.deepcopy(a["target"]), run_id=p["run_id"],
                             manifest_digest=a["manifest_digest"], start="2030-01-01T11:59:00Z",
                             end="2030-01-01T12:05:00Z", scenarios=list(p["scenarios"]))


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
        for key in ("monitoring_verified", "reservation_verified", "no_consumers", "permissions_verified"):
            with self.assertRaises(s.SafetyError):
                s.validate_preflight(self.a, dict(preflight(self.a), **{key: False}))
        # An empty entire database is no longer a prerequisite; the reserved
        # weekly scope must independently be clear.
        o = preflight(self.a)
        o["scope"]["week_clear"] = False
        with self.assertRaises(s.SafetyError):
            s.validate_preflight(self.a, o)

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
        ticket = c.admit("A06", "T0", NOW)
        with self.assertRaisesRegex(s.SafetyError, "CONCURRENCY_LIMIT"):
            c.admit("A06", "T0", NOW)
        c.finish(ticket, physical_release_confirmed=True)
        # R1 supersedes three concurrent rollback-pilot readers; exercise the
        # unchanged maximum-three assertion on an expressly approved G10 pair.
        a = copy.deepcopy(self.a)
        configure_grant(a, "G10", "T1", cap=3)
        c = s.SafetyController(a)
        self.assertTrue(c.sample(telemetry(), NOW))
        tickets = [c.admit("G10", "T1", NOW) for _ in range(3)]
        with self.assertRaisesRegex(s.SafetyError, "CONCURRENCY_LIMIT"):
            c.admit("G10", "T1", NOW)
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

    def test_r1_01_populated_dev_complete_authorization(self):
        a, p, e = populated()
        s.authorize_mutation(a, p, source(a), preflight(a), e, NOW)
        self.assertEqual(s.run_record(a, p)["status"], "NOT RUN")
        with self.assertRaises(s.SafetyError):
            s.validate_approval(dict(a, schema_version=1))
        with self.assertRaises(s.SafetyError):
            s.validate_plan(a, dict(p, schema_version=1), bound=False)
        with self.assertRaises(s.SafetyError):
            s.validate_preflight(a, dict(preflight(a), schema_version=1))

    def test_r1_02_environment_and_backup_fail_closed(self):
        a, p, e = populated()
        for key in s.ENV_FACTS:
            for omit in (True, False):
                o = preflight(a)
                if omit:
                    del o["environment"][key]
                else:
                    o["environment"][key] = False
                with self.subTest(key=key, omit=omit), self.assertRaises(s.SafetyError):
                    s.authorize_mutation(a, p, source(a), o, e, NOW)
        for key in a["environment"]:
            b = copy.deepcopy(a)
            del b["environment"][key]
            with self.subTest(key=key), self.assertRaises(s.SafetyError):
                s.validate_approval(b)
        for change in (dict(backup_at="2029-12-31T00:00:00Z"),
                       dict(reservation_until="2030-01-01T12:40:00Z"),
                       dict(restore_capability_ref=""), dict(restore_evidence_kind="VERIFYONLY")):
            b = dict(a, environment=dict(a["environment"], **change))
            with self.assertRaises(s.SafetyError):
                s.validate_approval(b)

    def test_r1_03_exact_dev_target_and_routing(self):
        a, p, e = populated()
        for db in ("rplanner", "rplanner_prod", "ERP", "acumatica_erp", "master", "model", "msdb", "tempdb", "other", "RPLANNER_DEV"):
            with self.subTest(db=db), self.assertRaises(s.SafetyError):
                s.validate_target(dict(a["target"], database=db))
        for value in ("mssql+pyodbc://other/rplanner_dev",
                      "mssql+pyodbc://fixture-host/rplanner",
                      "mssql+pyodbc://fixture-host/rplanner_dev?odbc_connect=opaque"):
            with self.assertRaises(s.SafetyError):
                s.authorize_mutation(a, p, source(a), preflight(a), dict(e, **{s.URL_ENV: value}), NOW)
        with self.assertRaises(s.SafetyError):
            s.authorize_mutation(a, p, source(a), dict(preflight(a), target=dict(a["target"], server="other")), e, NOW)
        b = dict(a, prerequisites=dict(a["prerequisites"], version="14.0.9999.1"))
        with self.assertRaises(s.SafetyError):
            s.validate_approval(b)

    def test_r1_04_scope_background_and_blocked_expectations(self):
        a, p, e = populated()
        for key in s.SCOPE_FACTS:
            o = preflight(a)
            o["scope"][key] = False
            with self.subTest(key=key), self.assertRaises(s.SafetyError):
                s.authorize_mutation(a, p, source(a), o, e, NOW)
        for key in s.SCOPE_DIGESTS + ("expectations_digest",):
            o = preflight(a)
            o["scope"][key] = "f"*64
            with self.subTest(key=key), self.assertRaisesRegex(s.SafetyError, "BACKGROUND_DRIFT"):
                s.authorize_mutation(a, p, source(a), o, e, NOW)
        a["scope"]["expectations"][0].update(status="BLOCKED", expected_digest=None)
        s.validate_approval(a)
        with self.assertRaisesRegex(s.SafetyError, "SCENARIO_BLOCKED"):
            s.authorize_mutation(a, p, source(a), preflight(a), e, NOW)
        c = s.SafetyController(a)
        self.assertTrue(c.sample(telemetry(), NOW))
        with self.assertRaisesRegex(s.SafetyError, "SCENARIO_BLOCKED"):
            c.admit("A06", "T0", NOW)
        self.assertEqual(s.evidence_record("A06", "T0", "BLOCKED", {})["status"], "BLOCKED")

    def test_r1_05_approval_scope_and_stage_binding(self):
        a, p, e = populated()
        for change in (dict(week_start="2030-01-08"), dict(expectations=[])):
            b = dict(a, scope=dict(a["scope"], **change))
            with self.assertRaises(s.SafetyError):
                s.validate_approval(b)
        for change in (dict(stage="T1"), dict(prior_evidence={}), dict(prior_evidence={"PILOT": dict(prior_review("6"), status="FAIL")}),
                       dict(monitoring_ref=""), dict(monitoring_connections=2)):
            b = dict(a, progression=dict(a["progression"], **change))
            with self.assertRaises(s.SafetyError):
                s.validate_approval(b)
        b = copy.deepcopy(a)
        configure_grant(b, "E09", cap=2)
        with self.assertRaises(s.SafetyError):
            s.validate_approval(b)
        b["progression"]["contention_ref"] = dict(po_ref="PO-E09", it_ref="IT-E09", max_connections=2,
                                                    lock_seconds=1, mutation_seconds=5)
        s.validate_approval(b)
        c = s.SafetyController(b)
        self.assertTrue(c.sample(telemetry(), NOW))
        ticket = c.admit("E09", "T0", NOW, kind="mutation")
        self.assertFalse(c.check_operation_deadline(ticket, "lock", NOW, NOW+timedelta(seconds=1)))
        self.assertEqual(c.reason, "TIMEOUT")
        configure_grant(b, "A06", "T3")
        b["progression"]["contention_ref"] = None
        with self.assertRaises(s.SafetyError):
            s.validate_approval(b)
        b["capacity_tiers"] = {"T3": dict(po_ref="PO-T3", it_ref="IT-T3")}
        s.validate_approval(b)
        with self.assertRaises(s.SafetyError):
            s.validate_preflight(a, dict(preflight(a), monitoring_connection_count=2))

    def test_r1_06_rollback_and_committed_probe_separate(self):
        for mode in ("ROLLBACK_ONLY", "COMMITTED_STORAGE_ONLY"):
            a, p, e = probe_case(mode)
            s.authorize_mutation(a, p, source(a), preflight(a), e, NOW)
            self.assertEqual(s.run_record(a, p)["status"], "NOT RUN")
        record = s.evidence_record("F01", "T0", "NOT RUN", {"rollback_check_passed": True}, probe_mode="ROLLBACK_ONLY")
        self.assertFalse(record["committed_acceptance"])
        with self.assertRaisesRegex(s.SafetyError, "COMMITTED_PROBE_REQUIRED"):
            s.evidence_record("F01", "T0", "PASS", {}, probe_mode="ROLLBACK_ONLY")
        with self.assertRaises(s.SafetyError):
            s.evidence_record("F01", "T0", "PASS", {})
        a, p, e = probe_case("ROLLBACK_ONLY", "F05")
        with self.assertRaisesRegex(s.SafetyError, "COMMITTED_PROBE_REQUIRED"):
            s.validate_approval(a)

    def test_r1_07_missing_mismatched_and_expired_probe_permit(self):
        a, p, e = probe_case()
        for change in (None, dict(a["probe_permit"], po_ref=""),
                       dict(a["probe_permit"], it_ref=""),
                       dict(a["probe_permit"], manifest_digest="f"*64),
                       dict(a["probe_permit"], source_tree="f"*40),
                       dict(a["probe_permit"], target=dict(a["target"], server="other")),
                       dict(a["probe_permit"], scenarios=["F05"])):
            with self.assertRaises(s.SafetyError):
                s.validate_approval(dict(a, probe_permit=change))
        b = copy.deepcopy(a)
        b["probe_permit"]["run_id"] = ROW_ID
        with self.assertRaises(s.SafetyError):
            s.authorize_mutation(b, p, source(b), preflight(b), e, NOW)
        after = NOW+timedelta(minutes=6)
        with self.assertRaisesRegex(s.SafetyError, "PROBE_WINDOW_CLOSED"):
            s.authorize_mutation(a, p, source(a), preflight(a), e, after)
        s.authorize_mutation(a, p, source(a), preflight(a), e, after, recovery=True)
        with tempfile.TemporaryDirectory() as root:
            ap, mp = Path(root)/"approval.json", Path(root)/"manifest.json"
            ap.write_text(json.dumps(a), encoding="utf-8")
            mp.write_text(json.dumps(p), encoding="utf-8")
            with patch.dict(os.environ, e, clear=True), patch.object(s, "observe_source", return_value=source(a)), patch.object(cli, "datetime") as clock:
                clock.now.return_value = after
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main(["plan", "--approval-file", str(ap), "--manifest", str(mp)]), 2)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue(), "Q869 OFFLINE_PLAN_REJECTED\n")

    def test_r1_08_probe_cardinality_and_nonoperational_shape(self):
        a, p, e = probe_case()
        for change in (dict(status="PREPARED"), dict(kind="project"),
                       dict(model_version="project_v2"), dict(column="other"),
                       dict(no_children=False), dict(no_workflow=False),
                       dict(identity=p["rows"][0]["identity"])):
            b, q = copy.deepcopy(a), copy.deepcopy(p)
            q["probe"].update(change)
            bind_probe(b, q)
            with self.assertRaises(s.SafetyError):
                s.authorize_mutation(b, q, source(b), preflight(b), e, NOW)
        b, q = copy.deepcopy(a), copy.deepcopy(p)
        second = copy.deepcopy(q["rows"][-1])
        second["identity"]["key"]["id"] = "33333333-3333-4333-8333-333333333333"
        q["rows"].append(second)
        bind_probe(b, q)
        with self.assertRaises(s.SafetyError):
            s.validate_plan(b, q)
        for change in (dict(active_probe_count=1), dict(active_probe_count=2),
                       dict(probe_constraints_verified=False), dict(probe_triggers_verified=False),
                       dict(probe_workflow_excluded=False)):
            with self.assertRaises(s.SafetyError):
                s.authorize_mutation(a, p, source(a), dict(preflight(a), **change), e, NOW)
        c = s.SafetyController(a)
        self.assertTrue(c.sample(telemetry(), NOW))
        ticket = c.admit("F01", "T0", NOW)
        with self.assertRaisesRegex(s.SafetyError, "CONCURRENCY_LIMIT"):
            c.admit("F01", "T0", NOW)
        c.finish(ticket, physical_release_confirmed=True)

    def test_r1_09_shared_metrics_and_immediate_stops(self):
        a, _, _ = populated()
        for key in s.SHARED_LIMITS:
            b = copy.deepcopy(a)
            del b["shared_limits"][key]
            with self.assertRaises(s.SafetyError):
                s.validate_approval(b)
            c = s.SafetyController(a)
            sample = telemetry()
            del sample[key]
            self.assertFalse(c.sample(sample, NOW))
            self.assertEqual(c.reason, "MONITORING_LOST")
            c = s.SafetyController(a)
            self.assertFalse(c.sample(dict(telemetry(), **{key: a["shared_limits"][key]}), NOW))
            self.assertEqual(c.reason, "ENVIRONMENT_ABORT")
        for key in ("business_impact", "ownership_uncertain", "unexpected_consumers"):
            c = s.SafetyController(a)
            self.assertFalse(c.sample(dict(telemetry(), **{key: True}), NOW))
            self.assertTrue(c.cancel_requested.is_set())
            with self.assertRaises(s.SafetyError):
                c.admit("A06", "T0", NOW)
        for key in s.SCOPE_DIGESTS:
            c = s.SafetyController(a)
            self.assertFalse(c.sample(dict(telemetry(), **{key: "f"*64}), NOW))

    def test_r1_10_operation_deadlines_and_progressive_concurrency(self):
        for op, seconds in (("lock", 2), ("barrier", 5), ("statement", 15)):
            c = self.controller()
            ticket = c.admit("A06", "T0", NOW)
            # Refresh telemetry on schedule for the longer deadline.
            for elapsed in range(5, seconds+1, 5):
                self.assertTrue(c.sample(telemetry(), NOW+timedelta(seconds=elapsed)))
            self.assertFalse(c.check_operation_deadline(ticket, op, NOW, NOW+timedelta(seconds=seconds)))
            self.assertEqual(c.reason, "TIMEOUT")
        a = copy.deepcopy(self.a)
        a["scope"]["expectations"][0].update(max_active=2, concurrency_ref="CONC-01")
        with self.assertRaises(s.SafetyError):
            s.validate_approval(a)

    def test_r1_11_recovery_only_restart_gate(self):
        a = dict(self.a, mode="RECOVERY_ONLY")
        after = NOW+timedelta(minutes=36)
        s.authorize_mutation(a, self.p, source(a), preflight(a), env(), after, recovery=True)
        with self.assertRaises(s.SafetyError):
            s.authorize_mutation(a, self.p, source(a), preflight(a), env(), NOW)
        j = self.committed()
        j = s.transition(self.p, j, "CLEANUP_PENDING", confirmed=[self.key], pending=[self.key],
                         independent=True, observations=self.present)
        clean = s.transition(self.p, j, "VERIFIED_CLEAN", independent=True, observations=self.absent)
        o = dict(approval_digest=s.digest(a), plan_digest=s.digest(self.p), target=a["target"],
                 run_id=self.p["run_id"], source=source(a), independent_absence=True,
                 preserved_digest=a["scope"]["preservation_digest"], no_active_transactions=True,
                 no_active_probe=True, reservation_held=True, operator_checks_passed=True,
                 restart_ref="RESTART-01")
        s.validate_restart(a, self.p, clean, o, after)
        with self.assertRaises(s.SafetyError):
            s.validate_restart(a, self.p, j, o, after)
        for key in ("independent_absence", "no_active_transactions", "no_active_probe",
                    "reservation_held", "operator_checks_passed"):
            with self.assertRaises(s.SafetyError):
                s.validate_restart(a, self.p, clean, dict(o, **{key: False}), after)
        with self.assertRaises(s.SafetyError):
            s.validate_restart(a, self.p, clean, dict(o, preserved_digest="f"*64), after)
        with self.assertRaises(s.SafetyError):
            s.validate_restart(a, self.p, clean, dict(o, source=dict(source(a), clean=False)), after)
        with self.assertRaises(s.SafetyError):
            s.validate_restart(a, self.p, clean, o, NOW+timedelta(minutes=46))

    def test_r1_12_populated_evidence_redaction_and_live_cli(self):
        a, p, e = populated()
        payload = json.dumps(s.run_record(a, p))
        for private in ("rplanner_dev", "fixture-host", "BACKUP-01", "PROC-01", "IT-RECOVERY"):
            self.assertNotIn(private, payload)
        with patch.dict(os.environ, e, clear=True), patch.object(s, "observe_source", side_effect=AssertionError):
            for command in ("preflight", "run", "cleanup", "recover", "verify-clean"):
                with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cli.main([command]), 3)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue(), "Q869 LIVE_COMMAND_UNAVAILABLE\n")


if __name__ == "__main__":
    unittest.main()

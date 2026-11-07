"""m2c report, unit-gate progress, unit promotions, draft attempts and worker telemetry."""

import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import _support
from _support import VERSION, make_project, now, write_json

import processes
import providers
import snapshots


def python_process(pid, *args, parent=1, elapsed=5):
    return {"pid": pid, "parent": parent, "elapsed_seconds": elapsed, "state": "R", "argv": ["python", *map(str, args)]}


class FakeProcesses:
    """Patch the shared process scan with fixed rows."""

    def __init__(self, rows):
        self.patcher = patch.object(processes, "shared_snapshot", lambda: None if rows is None else [dict(row) for row in rows])

    def __enter__(self):
        self.patcher.start()
        return self

    def __exit__(self, *exc):
        self.patcher.stop()


class JsonServer:
    """A loopback HTTP server answering GET paths from a dict, for model and executor probes."""

    def __init__(self, routes):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(routes.get(self.path, {"error": "missing"})).encode()
                self.send_response(200 if self.path in routes else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class GateProgressTests(unittest.TestCase):
    def test_active_run_gets_numeric_promotion_loop_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            support = Path(folder)
            write_json(support / "refinement-runs" / "run-1" / "results.json", {"unit_gate": {
                "ready_units": 105, "owner_trials_run": 77, "promoted": True, "gates_run": "3", "unknown": 9},
                "auto_promote": False})
            report = {"active_runs": [{"run_id": "run-1", "stage": "finalizing"},
                                      {"run_id": "run-1", "stage": "generation"},
                                      {"run_id": "../escape", "stage": "unit_gate"},
                                      {"run_id": "missing", "stage": "unit_gate"}]}
            snapshots.attach_gate_progress(support, report)
            self.assertEqual(report["active_runs"][0]["unit_gate"], {"ready_units": 105, "owner_trials_run": 77})
            self.assertIs(report["active_runs"][0]["auto_promote"], False)
            for run in report["active_runs"][1:]:
                self.assertNotIn("unit_gate", run)
            live = {"active_runs": [{"run_id": "run-1", "stage": "unit_gate", "unit_gate": {"ready_units": 3, "promoted": 1}}]}
            snapshots.attach_gate_progress(support, live)
            self.assertEqual(live["active_runs"][0]["unit_gate"], {"ready_units": 3, "promoted": 1})  # harness value wins
            current = {"active_runs": [{"run_id": "missing", "stage": "finalizing", "auto_promote": True, "unit_gate": {"promoted": 2}}]}
            snapshots.attach_gate_progress(support, current)
            self.assertEqual(current["active_runs"][0]["unit_gate"], {"promoted": 2})

    def test_visible_summary_and_live_progress(self):
        self.assertIsNone(snapshots.visible_summary(None))
        self.assertEqual(snapshots.visible_summary({"state": "promoted", "units": 2, "functions": 3, "commit": "abc1234",
                                                    "error": None, "extra": [1]}),
                         {"units": 2, "functions": 3, "state": "promoted", "commit": "abc1234"})
        self.assertIsNone(snapshots.visible_summary({"state": "bogus", "units": True}))
        with tempfile.TemporaryDirectory() as folder:
            support = Path(folder)
            gate = support / "unit-gates" / "abc"
            write_json(support / "unit-gates" / "active.json", {"output": str(gate), "phase": "visible_units"})
            write_json(gate / "visible-units" / "result.json", {"kind": "m2c_visible_units", "phase": "verify",
                                                                "units_considered": 20, "processed_units": 12,
                                                                "verified_units": 9, "verified_functions": 40})
            run = {"run_id": "r1", "stage": "visible_units", "updated_at": "2000-01-01T00:00:00+00:00"}
            snapshots.attach_gate_progress(support, {"active_runs": [run]})
            self.assertEqual(run["visible_progress"], {"units_considered": 20, "processed_units": 12, "verified_units": 9,
                                                       "verified_functions": 40, "phase": "verify"})
            self.assertGreater(run["heartbeat_at"], run["updated_at"])
            write_json(support / "unit-gates" / "active.json", {"output": str(Path(folder).parent)})
            outside = {"run_id": "r1", "stage": "visible_units"}
            snapshots.attach_gate_progress(support, {"active_runs": [outside]})
            self.assertNotIn("visible_progress", outside)


class M2cReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_absent_artifacts_give_an_empty_report(self):
        self.assertEqual(snapshots.m2c_snapshot(self.root, VERSION), {})

    def test_stored_report_goes_through_the_harness_loader(self):
        make_project(self.root)
        with FakeProcesses([]):
            report = snapshots.m2c_snapshot(self.root, VERSION)
        self.assertEqual(report["latest"]["run_id"], "run-1")
        self.assertEqual((report["active_runs"], report["activity_available"]), ([], True))
        with FakeProcesses(None):
            self.assertFalse(snapshots.m2c_snapshot(self.root, VERSION)["activity_available"])

    def test_active_pipeline_runner_is_reported_from_a_portable_process_scan(self):
        make_project(self.root)
        support = self.root / "build" / "decomp-support" / VERSION
        write_json(support / "pipeline-state.json", {"status": "active", "stage": "generation"})
        write_json(support / "batches" / "run-2" / "manifest.json", {"selection": "unmatched", "processed": 3, "total": 10,
                                                                     "updated_at": now()})
        runner = python_process(4242, Path("harness") / "m2c_pipeline_runner.py", "--root", self.root, "--version", VERSION)
        other = python_process(4343, Path("harness") / "m2c_pipeline_runner.py", "--root", self.root / "elsewhere")
        with FakeProcesses([runner, other]):
            report = snapshots.m2c_snapshot(self.root, VERSION)
        [run] = report["active_runs"]
        self.assertEqual((run["pid"], run["stage"], run["processed"], run["total"]), (4242, "generation", 3, 10))
        card = snapshots.m2c_card(_support.project(self.root), VERSION, report)
        self.assertEqual((card["active"]["stage"], card["latest"]["clean"], card["latest"]["scored"]), ("generation", 8, 6))
        self.assertEqual(card["latest"]["promoted"], 1)
        self.assertEqual(card["latest"]["visible"], {"state": "nothing_to_promote"})

    def test_m2c_report_module_uses_the_shared_scan(self):
        module = snapshots.m2c_report_module()
        row = python_process(5, "m2c_drafts.py")
        with FakeProcesses([row]):
            self.assertEqual(module.process_snapshot(), [row])


class PromotionTests(unittest.TestCase):
    def test_no_queue_folder_and_verified_and_stale_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            make_project(root)
            self.assertEqual(snapshots.unit_promotions(root, VERSION)["units"], [])
            source = root / "src" / "unit_a.c"
            raw = source.read_bytes()
            record = write_json(Path(folder) / "evidence" / "record.json", {
                "unit": "main/unit_a", "state": "object_verified", "compiled": True, "object_verified": True,
                "inventory": {"project_root": str(root)},
                "inputs": {str(source.absolute()): {"sha256": hashlib.sha256(raw).hexdigest(), "resolved_path": str(source.resolve())}}})
            import m2c_unit_queue
            m2c_unit_queue.publish(root, VERSION, record)
            value = snapshots.unit_promotions(root, VERSION)
            [unit] = value["units"]
            self.assertEqual((unit["unit"], unit["state"], unit["milestones"]["object_verified"]), ("main/unit_a", "object_verified", True))
            source.write_text("int fn_a(void) { return 1; }\n")  # changed input revokes the evidence
            [stale] = snapshots.unit_promotions(root, VERSION)["records"]
            self.assertEqual(stale["state"], "stale")

    def test_harness_failures_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "build/decomp-support" / VERSION / "unit-promotions").mkdir(parents=True)
            import m2c_unit_queue
            with patch.object(m2c_unit_queue, "snapshot", side_effect=ImportError("no module")):
                value = snapshots.unit_promotions(Path(folder), VERSION)
            self.assertFalse(value["available"])
            self.assertIn("no module", value["reason"])


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = _support.project(self.root)

    def state(self, host):
        write_json(self.root / "build/decomp-support" / VERSION / "attempts" / "state.json", {
            "version": VERSION, "batch": "run-1", "model": "local-model", "host": host, "current": "fn_b",
            "updated_at": now(), "target_total": 5, "draft_batch_finished": True,
            "items": {"fn_a": {"symbol": "fn_a", "best_pct": 100, "attempts": [
                          {"index": 0, "at": "2026-01-01T00:00:00Z", "pct": 90, "compiled": True, "profile": "p"},
                          {"index": 1, "at": "2026-01-01T00:01:00Z", "pct": 100, "delta": 10, "compiled": True, "profile": "p"}]},
                      "fn_b": {"symbol": "fn_b", "best_pct": 50, "attempts": [
                          {"index": 0, "at": "2026-01-01T00:02:00Z", "pct": 50, "compiled": True, "profile": "p"}]}}})

    def worker(self, *extra):
        return python_process(77, "m2c_draft_attempts.py", "--root", self.root, "--version", VERSION, *extra)

    def test_ledger_summary_and_unprobed_remote_model(self):
        self.state("http://model-host.invalid/v1")
        tracker = snapshots.draft_attempt_tracker(self.project, VERSION, [self.worker()], None, providers.Settings())
        self.assertEqual((tracker["attempted"], tracker["exact"], tracker["total"], tracker["current"]), (1, 1, 5, "fn_b"))
        self.assertEqual([row["symbol"] for row in tracker["recent"]], ["fn_b", "fn_a", "fn_a"])
        self.assertEqual(tracker["draft_validation"], {"checked": 2, "compiled": 2, "scored": 2, "scorer_exact": 0})
        runtime = tracker["model_runtime"]
        self.assertEqual((runtime["status"], runtime["probed"], runtime["worker"]["pid"]), ("not_probed", False, 77))
        stopped = snapshots.draft_attempt_tracker(self.project, VERSION, [], None, providers.Settings())
        self.assertEqual(stopped["model_runtime"]["status"], "stopped")

    def test_loopback_model_rpc_is_probed(self):
        with JsonServer({"/v1/models": {"data": [{"id": "local-model", "owned_by": "llama", "meta": {"n_ctx": 4096}}]}}) as rpc:
            self.state(rpc.url + "/v1")
            tracker = snapshots.draft_attempt_tracker(self.project, VERSION, [self.worker()], None, providers.Settings())
        runtime = tracker["model_runtime"]
        self.assertEqual((runtime["status"], runtime["backend"], runtime["context_size"]), ("running", "llama", 4096))

    def test_worker_attribution_by_root_version_or_default(self):
        self.state("")
        rows = [python_process(1, "m2c_draft_attempts.py", f"--root={self.root}", "--version", "TEST02"),
                python_process(2, "unrelated.py", "--root", self.root)]
        self.assertIsNone(snapshots.draft_worker(self.project, VERSION, rows, None))
        default = [python_process(3, "m2c_draft_attempts.py")]
        self.assertEqual(snapshots.draft_worker(self.project, VERSION, default, "proj")["pid"], 3)
        self.assertIsNone(snapshots.draft_worker(self.project, VERSION, default, "other"))

    def test_oversized_or_missing_ledgers(self):
        self.assertEqual(snapshots.draft_attempt_tracker(self.project, VERSION, [], None, providers.Settings()), {})
        self.state("")
        with patch.object(snapshots, "MAX_JSON_BYTES", 16):
            self.assertEqual(snapshots.draft_attempt_tracker(self.project, VERSION, [], None, providers.Settings()),
                             {"oversized_state": True})


class TelemetryTests(unittest.TestCase):
    def test_harness_processes_attempts_and_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = _support.project(root)
            values = {"proj": {"versions": {VERSION: {
                "m2c": {"active_runs": [{"pid": 10, "stage": "validation", "current_function": "fn_c", "processed": 2, "total": 4}],
                        "latest": {"counts": {"ok": 3}, "total": 4}},
                "draft_attempts": {"model": "local-model", "current": "fn_b", "model_runtime": {},
                                   "recent": [{"symbol": "fn_a", "index": 1, "delta": 2.0, "at": "2026-01-01T00:01:00Z"},
                                              {"symbol": "fn_b", "index": 0, "delta": None, "at": "2026-01-01T00:02:00Z"}]}}}}}
            rows = [python_process(10, "m2c_pipeline_runner.py", "--root", root),
                    python_process(11, "m2c_validate_drafts.py", "--root", root, parent=10),  # a runner's child
                    python_process(12, "m2c_draft_attempts.py", "--root", root, "--version", VERSION),
                    python_process(13, "m2c_drafts.py", "--root", root / "elsewhere"),
                    python_process(14, "some_other_tool.py", "--root", root),
                    {**python_process(15, "m2c_drafts.py"), "state": "Z"}]
            with FakeProcesses(rows):
                value = snapshots.telemetry([project], "proj", values)
            by_pid = {row["pid"]: row for row in value["active"]}
            self.assertEqual(sorted(by_pid), [10, 12, 13])
            self.assertEqual((by_pid[10]["phase"], by_pid[10]["symbol"], by_pid[10]["project"]), ("validation", "fn_c", "proj"))
            self.assertEqual((by_pid[12]["symbol"], by_pid[12]["phase"], by_pid[12]["worker"]), ("fn_b", "attempt", "local-model"))
            self.assertIsNone(by_pid[13]["project"])
            self.assertEqual([row["symbol"] for row in value["recent"]], ["fn_b", "fn_a"])
            [card] = value["m2c_projects"]
            self.assertEqual((card["active"]["stage"], card["latest"]["clean"]), ("validation", 3))
            with FakeProcesses(None):
                self.assertFalse(snapshots.telemetry([project], "proj", values)["processes_available"])

    def test_executor_health_is_cached_and_failures_reported(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            if "18767" in url:
                raise OSError("refused")
            return {"status": "ok", "worker": "w1", "tool_revision": "r", "tool_fingerprint": "f" * 64}

        health = snapshots.ExecutorHealth(["http://127.0.0.1:18766=4", "http://127.0.0.1:18767"], ttl=60, fetch=fetch, background=False)
        rows = health()
        self.assertEqual([(row["status"], row["slots"]) for row in rows], [("ok", 4), ("unreachable", None)])
        self.assertEqual(rows[0]["tool_fingerprint"], "f" * 16)
        health()
        self.assertEqual(len(calls), 2)
        gate = threading.Event()
        background = snapshots.ExecutorHealth(["http://127.0.0.1:18766"], fetch=lambda url, timeout: gate.wait(5) and fetch(url, timeout))
        self.assertEqual(background()[0]["status"], "checking")  # never blocks the caller
        gate.set()
        deadline = time.monotonic() + 5
        while background()[0]["status"] == "checking" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(background()[0]["status"], "ok")

    def test_executor_health_uses_the_harness_remote_client(self):
        with JsonServer({"/v1/health": {"schema_version": 1, "status": "ok", "worker": "w", "tool_fingerprint": "abc"}}) as remote:
            [row] = snapshots.ExecutorHealth([remote.url], background=False)()
        self.assertEqual((row["status"], row["worker"]), ("ok", "w"))


class ProjectSnapshotTests(unittest.TestCase):
    def test_full_snapshot_is_read_only_for_the_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_project(Path(folder) / "project")
            before = _support.tree_digest(root)
            store = providers.HistoryStore(Path(folder) / "cache")
            with FakeProcesses([]):
                value = snapshots.project_snapshot(_support.project(root), store, providers.Settings(), "proj")
            data = value["versions"][VERSION]
            self.assertEqual(data["build"]["status"], "exact")
            self.assertEqual(data["measures"]["matched_functions"], 10)
            self.assertEqual(len(data["progress_history"]), 1)
            self.assertEqual(data["m2c"]["latest"]["run_id"], "run-1")
            self.assertEqual(data["unit_promotions"]["units"], [])
            self.assertEqual(data["draft_attempts"], {})
            self.assertEqual(_support.tree_digest(root), before)
            json.dumps(value, allow_nan=False)

    def test_one_failing_reader_does_not_hide_the_others(self):
        with tempfile.TemporaryDirectory() as folder:
            root = make_project(Path(folder))
            with FakeProcesses([]), patch.object(snapshots, "m2c_snapshot", side_effect=RuntimeError("broken")):
                data = snapshots.version_snapshot(_support.project(root), VERSION, providers.HistoryStore(None),
                                                  providers.Settings(), None, [])
            self.assertFalse(data["m2c"]["available"])
            self.assertIn("broken", data["m2c"]["reason"])
            self.assertEqual(data["build"]["status"], "exact")


if __name__ == "__main__":
    unittest.main()

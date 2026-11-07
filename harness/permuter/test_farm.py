from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from permuter import farm
except ImportError:  # run directly from this directory
    import farm

# Stands in for decomp-permuter: reports scores, then searches "forever".
FAKE_PERMUTER = """
import time
print("base score = 120", flush=True)
print("found new best score! (40 vs 120)", flush=True)
time.sleep(60)
"""


class FarmTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        permuter = self.root / "decomp-permuter"
        permuter.mkdir()
        (permuter / "permuter.py").write_text(FAKE_PERMUTER, encoding="utf-8")
        saved = (farm.FARM_ROOT, farm.PERMUTER, farm.UNITS, farm.STATE_DIR, farm.OUTBOX, farm.LOGS)
        self.addCleanup(lambda: setattr_all(saved))
        farm.configure(self.root / "farm", self.root / "results", permuter)
        for folder in (farm.UNITS, farm.STATE_DIR, farm.LOGS, farm.OUTBOX / "nearwins"):
            folder.mkdir(parents=True, exist_ok=True)

    def unit(self, fn: str) -> Path:
        folder = farm.UNITS / fn
        folder.mkdir(parents=True)
        (folder / "base.c").write_text("int fn_a(void) { return 0; }\n", encoding="utf-8")
        return folder

    def test_worker_scores_harvests_wins_and_stops_its_process_tree(self) -> None:
        unit = self.unit("fn_80001234")
        worker = farm.Worker("fn_80001234", unit, budget=60)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                worker.read_scores()
                if worker.best_score == 40:
                    break
                time.sleep(0.1)
            self.assertEqual((worker.base_score, worker.best_score), (120, 40))
            self.assertIsNone(worker.poll())
        finally:
            worker.stop()
        self.assertIsNotNone(worker.proc.poll())
        self.assertFalse(worker.tmp_dir.exists())

        winner = unit / "output-0-1"
        winner.mkdir()
        (winner / "source.c").write_text("int fn_80001234(void) { return 1; }\n", encoding="utf-8")
        won, best = farm.harvest(worker, {"unit": "main/unit", "size": 8}, nearwin_threshold=25)
        self.assertEqual((won, best), (True, 0))
        summary = json.loads((farm.OUTBOX / "fn_80001234" / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["won"])
        self.assertEqual(summary["unit"], "main/unit")

    def test_status_is_a_dashboard_snapshot_with_the_farm_heartbeat(self) -> None:
        for fn in ("fn_a", "fn_b", "fn_c", "fn_d"):
            self.unit(fn)
        win = farm.OUTBOX / "fn_a"
        win.mkdir()
        (win / "summary.json").write_text(json.dumps({"fn": "fn_a", "won": True, "ts": "t",
                                                      "unit": "main/a", "base_score": 50}),
                                          encoding="utf-8")
        (farm.OUTBOX / "nearwins" / "fn_c_score7").mkdir()
        state = {"done": {"fn_a": {"won": True, "rounds": 1, "base": 50, "best": 0},
                          "fn_b": {"bad": "permuter exited rc=1 before base score"},
                          "fn_c": {"rounds": 1, "last_round": 0, "base": 30, "best": 7}},
                 "round": 0}
        status = farm.build_status(state=state, workers={}, queue_order=["fn_a", "fn_b", "fn_c", "fn_d"],
                                   metas={"fn_d": {"unit": "main/d", "size": 16, "base_score": 9}},
                                   target_workers=4, round_no=0)
        self.assertEqual((status["kind"], status["schema_version"]), ("permuter_farm", 1))
        self.assertTrue(status["reachable"])
        self.assertEqual(status["farm"]["wins"], 1)
        self.assertEqual(status["farm"]["bad"], 1)
        self.assertEqual(status["farm"]["target_workers"], 4)
        self.assertTrue(status["farm"]["alive"])
        states = {unit["fn"]: unit["state"] for unit in status["units"]}
        self.assertEqual(states, {"fn_a": "won", "fn_b": "bad", "fn_c": "done", "fn_d": "queued"})
        self.assertEqual(status["counts"], {"active": 0, "queued": 1, "won": 1, "done": 1, "bad": 1})
        self.assertEqual(status["improved_units"], 2)
        self.assertEqual([win["fn"] for win in status["wins"]], ["fn_a"])
        self.assertEqual(status["nearwins"], ["fn_c_score7"])
        self.assertEqual(status["units"][3]["base_score"], 9)
        json.dumps(status)

    def test_stale_results_are_pruned_and_manifest_order_is_kept(self) -> None:
        for fn in ("fn_b", "fn_a"):
            self.unit(fn)
        (farm.UNITS / "manifest.json").write_text(json.dumps([
            {"fn": "fn_b", "status": "ok"}, {"fn": "fn_a", "status": "ok"},
            {"fn": "fn_missing", "status": "ok"}, {"fn": "fn_skip", "status": "skip"}]),
            encoding="utf-8")
        metas, order = farm.load_manifest_queue(farm.UNITS)
        self.assertEqual(order, ["fn_b", "fn_a"])
        state = {"done": {"fn_a": {}, "fn_gone": {}}}
        self.assertEqual(farm.prune_stale_results(state, order), ["fn_gone"])
        self.assertEqual(set(state["done"]), {"fn_a"})

    def test_status_flag_prints_the_status_file(self) -> None:
        (farm.STATE_DIR / "status.json").write_text(json.dumps({"kind": "permuter_farm"}),
                                                    encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            farm.main(["--farm-root", str(farm.FARM_ROOT), "--results-dir", str(farm.OUTBOX),
                       "--permuter", str(farm.PERMUTER.parent), "--status"])
        self.assertEqual(json.loads(output.getvalue())["kind"], "permuter_farm")


def setattr_all(saved) -> None:
    (farm.FARM_ROOT, farm.PERMUTER, farm.UNITS, farm.STATE_DIR, farm.OUTBOX, farm.LOGS) = saved


if __name__ == "__main__":
    unittest.main()

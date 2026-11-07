"""Configuration, report, build, history and permuter readers."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _support
from _support import MEASURES, VERSION, make_project, write_json

import harness_paths
import providers


class SettingsTests(unittest.TestCase):
    def test_defaults_bind_loopback(self):
        settings = providers.parse_settings(None)
        self.assertEqual((settings.host, settings.port, settings.cache_seconds), ("127.0.0.1", 8765, 2))
        self.assertIsNone(settings.permuter_status)
        self.assertEqual(settings.m2c_executors, [])

    def test_section_is_validated(self):
        for bad in ({"port": 70000}, {"port": True}, {"cache_seconds": 0}, {"host": ""},
                    {"m2c_executors": ["ftp://127.0.0.1"]}, {"m2c_executors": "http://127.0.0.1"},
                    {"permuter_project": "../x"}, {"permuter_status": ""}, {"workers": 0}, []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                providers.parse_settings(bad)

    def test_paths_resolve_through_the_harness_and_executors_dedupe(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = providers.parse_settings(
                {"permuter_status": "farm/status.json", "m2c_executors": ["http://127.0.0.1:18766=4"] * 2},
                resolve=lambda value: (Path(folder) / value).resolve())
            self.assertEqual(settings.permuter_status, (Path(folder) / "farm/status.json").resolve())
            self.assertEqual(settings.m2c_executors, ["http://127.0.0.1:18766=4"])

    def test_permuter_farm_root_supplies_the_status_file_when_present(self):
        with tempfile.TemporaryDirectory() as folder:
            resolve = lambda value: (Path(folder) / value).resolve()  # noqa: E731
            self.assertIsNone(providers.parse_settings({}, resolve, "farm").permuter_status)
            write_json(Path(folder) / "farm/state/status.json", {"alive": True, "ts": "x"})
            self.assertEqual(providers.parse_settings({}, resolve, "farm").permuter_status,
                             (Path(folder) / "farm/state/status.json").resolve())

    def test_projects_come_from_harness_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = harness_paths.Project("proj", Path(folder), VERSION, "Project", (VERSION, "TEST02"))
            [project] = providers.make_projects([entry])
            self.assertEqual((project.versions, project.default_version), ((VERSION, "TEST02"), VERSION))
            for bad in (harness_paths.Project("../x", Path(folder), VERSION),
                        harness_paths.Project("ok", Path(folder), "../escape", "", ("../escape",))):
                with self.subTest(bad=bad.id), self.assertRaises(ValueError):
                    providers.make_projects([bad])
            with self.assertRaises(ValueError):
                providers.make_projects([entry, entry])

    def test_load_harness_config_reads_projects_and_dashboard_section(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = harness_paths.Project("proj", Path(folder), VERSION, "Project", (VERSION,))
            section = {"dashboard": {"port": 9000, "m2c_executors": ["http://127.0.0.1:18766"]}}
            with patch.object(harness_paths, "projects", return_value=[entry]), \
                    patch.object(harness_paths, "CONFIG", section), patch.object(harness_paths, "PROJECT_ID", "proj"):
                projects, settings, default = providers.load_harness_config()
            self.assertEqual(([item.id for item in projects], settings.port, default), (["proj"], 9000, "proj"))

    def test_public_config_has_no_local_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            project = _support.project(Path(folder))
            settings = providers.Settings(permuter_status=Path(folder) / "status.json")
            public = providers.public_config([project], settings, "proj")
            self.assertNotIn(str(Path(folder).resolve()), json.dumps(public))
            self.assertEqual(public["permuter_farm"]["url"], "/api/permuter")
            self.assertEqual(public["projects"][0]["versions"], [VERSION])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_report_measures_categories_and_provenance(self):
        make_project(self.root)
        report, source = providers.read_report(self.root, VERSION)
        self.assertEqual(source["status"], "available")
        self.assertEqual(len(source["sha256"]), 64)
        self.assertEqual(providers.measures(report), MEASURES)
        self.assertEqual(providers.categories(report)[0]["measures"], {"fuzzy_match_percent": 70.0, "total_code": 100})

    def test_missing_invalid_and_non_finite_reports(self):
        self.assertEqual(providers.read_report(self.root, VERSION)[1]["status"], "missing")
        path = self.root / "build" / VERSION / "report.json"
        path.parent.mkdir(parents=True)
        for text in ("{", '{"measures": {}}', '{"measures": {"x": NaN}}', '{"measures": {"x": 1e999}}', "[]"):
            with self.subTest(text=text):
                path.write_text(text)
                report, source = providers.read_report(self.root, VERSION)
                self.assertEqual((report, source["status"]), ({}, "invalid"))

    def test_build_check_uses_manifest_path(self):
        make_project(self.root)
        self.assertEqual(providers.build_check(self.root, VERSION)["status"], "exact")
        self.assertEqual(providers.build_check(self.root, VERSION)["scope"], "main.dol")
        (self.root / "build" / VERSION / "main.dol").write_bytes(b"changed")
        self.assertEqual(providers.build_check(self.root, VERSION)["status"], "mismatch")
        other = self.root / "build" / VERSION / "game.elf"
        other.write_bytes(b"elf")
        import hashlib
        (self.root / "config" / VERSION / "build.sha1").write_text(
            f"{hashlib.sha1(b'elf').hexdigest()}  build/{VERSION}/game.elf\n")
        self.assertEqual(providers.build_check(self.root, VERSION)["scope"], "game.elf")
        self.assertEqual(providers.build_check(self.root, VERSION)["status"], "exact")
        (self.root / "config" / VERSION / "build.sha1").write_text("f" * 40 + "  ../../outside.bin\n")
        self.assertEqual(providers.build_check(self.root, VERSION)["scope"], "main.dol")  # escapes fall back
        self.assertEqual(providers.build_check(self.root, "TEST02")["status"], "not_built")
        (self.root / "config" / VERSION / "build.sha1").write_text("not-a-hash\n")
        self.assertEqual(providers.build_check(self.root, VERSION)["status"], "unknown")

    def test_report_behind_source_uses_newest_edit(self):
        make_project(self.root)
        source = self.root / "src" / "unit_a.c"
        later = time.time() + 60
        os.utime(source, (later, later))
        found = providers.newest(self.root, ("src", "include", "configure.py"))
        self.assertEqual(found["path"], "src/unit_a.c")


class HistoryTests(unittest.TestCase):
    def test_records_distinct_exact_builds_in_its_own_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            store = providers.HistoryStore(Path(folder) / "cache")
            source = {"status": "available", "report_at": "2026-01-01T00:00:00+00:00"}
            exact = {"status": "exact"}
            values = {"total_code": 100, "matched_code": 10, "total_functions": 4, "matched_functions": 1}
            self.assertEqual(store.record("p", VERSION, source, values, {"status": "mismatch"}), [])
            self.assertEqual(len(store.record("p", VERSION, source, values, exact)), 1)
            self.assertEqual(len(store.record("p", VERSION, source, values, exact)), 1)  # unchanged
            later = {**source, "report_at": "2026-01-02T00:00:00+00:00"}
            points = store.record("p", VERSION, later, {**values, "matched_code": 20}, exact)
            self.assertEqual([row["matched_code"] for row in points], [10, 20])
            older = {**source, "report_at": "2025-12-31T00:00:00+00:00"}
            self.assertEqual(len(store.record("p", VERSION, older, {**values, "matched_code": 5}, exact)), 2)
            self.assertTrue((Path(folder) / "cache" / "history" / f"p-{VERSION}.json").is_file())
            delta = providers.stats_delta(points, {**values, "matched_code": 20}, later["report_at"])
            self.assertEqual(delta["measures"]["matched_code"], 10)
            self.assertEqual(delta["from_at"], source["report_at"])
            self.assertIsNone(providers.stats_delta(points[:1], values, "x"))

    def test_project_history_is_merged_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "build/decomp-support" / VERSION / "progress-history.json",
                       {"points": [{"at": "2025-01-01", "matched_code": 1, "private": "x"}, {"bad": 1}]})
            merged = providers.merge_history(providers.project_history(root, VERSION), [{"at": "2025-02-01", "matched_code": 2}])
            self.assertEqual(merged, [{"at": "2025-01-01", "matched_code": 1}, {"at": "2025-02-01", "matched_code": 2}])


class PermuterTests(unittest.TestCase):
    def test_poller_status_is_served_as_is_and_farm_heartbeat_is_wrapped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_json(Path(folder) / "status.json", {"kind": "permuter_farm", "farm": {"alive": True}, "units": []})
            self.assertEqual(providers.permuter_snapshot(path, "proj")["project"], "proj")
            write_json(path, {"alive": True, "ts": "2026-01-01T00:00:00Z", "workers": 2, "target_workers": 4, "wins": 1,
                              "active": {"fn_a": {"base": 90, "best": 40, "elapsed": 5}, "fn_b": {"base": 10, "best": 10}}})
            value = providers.permuter_snapshot(path)
            self.assertEqual((value["kind"], value["farm"]["workers"], value["counts"]["active"]), ("permuter_farm", 2, 2))
            self.assertEqual(value["improved_units"], 1)
            self.assertEqual(value["units"][0], {"fn": "fn_a", "state": "active", "base_score": 90, "best_score": 40,
                                                 "elapsed_seconds": 5})
            write_json(path, {"kind": "other"})
            with self.assertRaises(ValueError):
                providers.permuter_snapshot(path)


if __name__ == "__main__":
    unittest.main()

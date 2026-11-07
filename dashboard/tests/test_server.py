"""HTTP routes, caching, bootstrap and read-only behaviour of the dashboard server."""

import gzip
import http.client
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import _support
from _support import VERSION, make_project, write_json

import processes
import providers
from server import DashboardCache, DashboardServer, main


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = make_project(self.folder / "checkout")
        self.projects = [_support.project(self.root, "proj", (VERSION, "TEST02")),
                         _support.project(self.folder / "missing", "future", ("FUTURE01",))]
        patcher = patch.object(processes, "shared_snapshot", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, settings=None, **options):
        settings = settings or providers.Settings(cache_seconds=0.1, telemetry_seconds=0.1)
        self.server = DashboardServer(("127.0.0.1", 0), self.projects, settings, "proj",
                                      cache_dir=self.folder / "cache", **options)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)

    def get(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        result = response.status, body, dict(response.getheaders())
        connection.close()
        return result

    def ready(self, route):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, body, _ = self.get(route)
            self.assertEqual(status, 200, body)
            value = json.loads(body)
            if value["_cache"]["status"] != "loading":
                return value
            time.sleep(0.02)
        self.fail(f"snapshot for {route} never completed")

    def test_config_registry_has_no_local_paths(self):
        self.start()
        status, body, _ = self.get("/api/config")
        value = json.loads(body)
        self.assertEqual((status, value["default_project"], value["permuter_farm"]), (200, "proj", None))
        self.assertEqual([item["id"] for item in value["projects"]], ["proj", "future"])
        self.assertNotIn(str(self.folder.resolve()), body.decode())

    def test_project_snapshot_measures_m2c_and_version_filter(self):
        self.start()
        value = self.ready("/api/projects/proj")
        data = value["versions"][VERSION]
        self.assertEqual((data["build"]["status"], data["measures"]["matched_functions"]), ("exact", 10))
        self.assertEqual(data["m2c"]["latest"]["run_id"], "run-1")
        self.assertEqual(value["versions"]["TEST02"]["report_source"]["status"], "missing")
        scoped = self.ready(f"/api/projects/proj?version={VERSION}")
        self.assertEqual(list(scoped["versions"]), [VERSION])
        future = self.ready("/api/projects/future")
        self.assertEqual(future["versions"]["FUTURE01"]["build"]["status"], "not_built")
        self.assertTrue((self.folder / "cache" / "history" / f"proj-{VERSION}.json").is_file())

    def test_etag_revalidation_and_gzip(self):
        self.start(providers.Settings(cache_seconds=30, telemetry_seconds=30))
        self.ready("/api/projects/proj")
        status, body, headers = self.get("/api/projects/proj", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((status, headers.get("Content-Encoding")), (200, "gzip"))
        self.assertIn("versions", json.loads(gzip.decompress(body)))
        status, body, _ = self.get("/api/projects/proj", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, body), (304, b""))
        scoped = self.get(f"/api/projects/proj?version={VERSION}")[2]["ETag"]
        self.assertNotEqual(scoped, headers["ETag"])
        self.assertEqual(self.get(f"/api/projects/proj?version={VERSION}", headers={"If-None-Match": scoped})[0], 304)

    def test_unchanged_artifacts_keep_the_same_etag_across_refreshes(self):
        self.start()
        self.ready("/api/projects/proj")
        time.sleep(0.3)
        self.get("/api/projects/proj")
        time.sleep(0.3)
        first = self.server.caches["project", "proj"].revision
        time.sleep(0.5)
        self.get("/api/projects/proj")
        time.sleep(0.3)
        self.assertEqual(self.server.caches["project", "proj"].revision, first)

    def test_routes_reject_unknown_and_unsafe_requests(self):
        outside = self.folder / "secret.json"
        outside.write_text('{"secret": true}')
        self.start()
        for route, expected in (("/api/projects/nope", 404), ("/api/projects/..", 400), ("/api/nope", 404),
                                ("/?project=nope", 404), ("/api/projects/proj?version=NO", 400),
                                ("/api/projects/proj?version=../TEST01", 400), ("/api/projects/proj?version=a&version=b", 400),
                                ("/%2e%2e/secret.json", 400), ("/..%5csecret.json", 400), ("/.hidden", 404),
                                ("/missing.js", 404), ("/api/permuter", 404),
                                ("/app.js", 200), ("/style.css", 200), ("/", 200), ("/?project=future", 200)):
            with self.subTest(route=route):
                self.assertEqual(self.get(route)[0], expected)
        self.assertEqual(self.get("/api/config", "POST")[0], 405)
        self.assertEqual(self.get("/api/config", "DELETE")[0], 405)
        status, body, headers = self.get("/app.js", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))

    def test_bootstrap_is_escaped_and_selects_the_project(self):
        self.projects[1] = providers.Project(id="future", name='</script><script>alert("x")</script>', root=self.folder,
                                             versions=("FUTURE01",), default_version="FUTURE01")
        self.start()
        status, body, _ = self.get("/?project=future")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('"selected_project":"future"', text)
        self.assertNotIn("</script><script>alert", text)
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertLess(text.index("window.DashboardConfig"), text.index('src="/app.js"'))
        self.assertNotIn("<!--DASHBOARD_CONFIG-->", text)

    def test_telemetry_reports_cards_executors_and_processes(self):
        settings = providers.Settings(cache_seconds=0.1, telemetry_seconds=0.1, m2c_executors=["http://127.0.0.1:18766"])
        health = lambda: [{"executor": "http://127.0.0.1:18766", "status": "ok"}]  # noqa: E731
        self.start(settings, executor_health=health)
        self.ready("/api/projects/proj")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.ready("/api/telemetry")
            if any(card["available"] for card in value["m2c_projects"]):
                break
            time.sleep(0.05)
        self.assertEqual(value["executors"], [{"executor": "http://127.0.0.1:18766", "status": "ok"}])
        self.assertTrue(value["processes_available"])
        self.assertEqual(value["active"], [])
        self.assertIn(("proj", VERSION), {(card["project"], card["version"]) for card in value["m2c_projects"]})
        self.assertEqual(json.loads(self.get("/api/config")[1])["m2c_executors"], 1)

    def test_permuter_status_is_served_read_only(self):
        status = write_json(self.folder / "farm" / "status.json",
                            {"schema_version": 1, "kind": "permuter_farm", "reachable": True,
                             "farm": {"alive": True, "workers": 2}, "units": [], "wins": []})
        before = status.read_bytes()
        self.start(providers.Settings(cache_seconds=0.1, permuter_status=status, permuter_project="proj"))
        value = self.ready("/api/permuter")
        self.assertEqual((value["farm"]["workers"], value["project"]), (2, "proj"))
        self.assertEqual(json.loads(self.get("/api/config")[1])["permuter_farm"], {"url": "/api/permuter", "project": "proj"})
        self.assertEqual(status.read_bytes(), before)

    def test_serving_never_writes_into_checkouts(self):
        before = _support.tree_digest(self.root)
        self.start()
        self.ready("/api/projects/proj")
        self.ready("/api/telemetry")
        self.get("/")
        self.assertEqual(_support.tree_digest(self.root), before)

    def test_main_reports_configuration_errors(self):
        with patch("server.load_harness_config", side_effect=ValueError("dashboard.port must be 0-65535")), \
                patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            main(["--port", "0"])
        self.assertEqual(raised.exception.code, 2)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown, wait=True)

    def settle(self, cache):
        deadline = time.monotonic() + 5
        while cache.refreshing and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_failure_keeps_last_good_value_and_etag_tracks_content(self):
        values = [{"n": 1}, {"n": 1}, RuntimeError("boom"), {"n": 2}]
        clock = [0.0]

        def loader():
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        cache = DashboardCache(loader, self.executor, ttl=1, clock=lambda: clock[0])
        self.assertEqual(cache.get()["_cache"]["status"], "loading")
        self.settle(cache)
        first, tag = cache.get_with_tag()
        self.assertEqual((first["n"], first["_cache"]["status"]), (1, "fresh"))
        clock[0] = 1.5
        cache.get(); self.settle(cache)
        self.assertEqual(cache.get_with_tag()[1], tag)  # identical content keeps its ETag
        clock[0] = 3.0
        cache.get(); self.settle(cache)
        failed, failed_tag = cache.get_with_tag()
        self.assertEqual((failed["n"], failed["_cache"]["status"]), (1, "stale"))
        self.assertIn("boom", failed["_cache"]["error"])
        self.assertNotEqual(failed_tag, tag)
        clock[0] = 10.0
        cache.get(); self.settle(cache)
        self.assertEqual(cache.get()["n"], 2)

    def test_one_refresh_at_a_time(self):
        started, release = Event(), Event()
        calls = []

        def loader():
            calls.append(1)
            started.set()
            release.wait(5)
            return {"ok": True}

        cache = DashboardCache(loader, self.executor, ttl=0.1)
        cache.get()
        started.wait(5)
        for _ in range(5):
            cache.get()
        release.set()
        self.settle(cache)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

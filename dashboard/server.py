#!/usr/bin/env python3
"""Read-only dashboard for the harness's m2c / permuter stack and draft workers (Python 3.11+, stdlib only).

Usage: python dashboard/server.py [--host 127.0.0.1] [--port 8765] [--open]

Projects and settings come from the harness config (DECOMP_HARNESS_CONFIG or <repo>/harness.local.json,
section "dashboard"). The server only reads project checkouts; its own measurement history is kept in
dashboard/.cache/ (or --cache-dir).
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import socket
import threading
import time
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from providers import (MAX_JSON_BYTES, TOKEN, HistoryStore, Project, Settings, contained, json_bytes,
                       load_harness_config, permuter_snapshot, public_config, unavailable, utc_now)
import snapshots

HERE = Path(__file__).resolve().parent


class DashboardCache:
    """One background refresh at a time per key; keeps the last success and reports its age."""

    MAX_WAIT = 30  # a slow snapshot never delays the next refresh by more than this
    MAX_FAILURE_WAIT = 60

    def __init__(self, loader, executor, ttl=2.0, initial=None, clock=time.monotonic):
        self.loader, self.executor, self.ttl, self.clock = loader, executor, ttl, clock
        self.lock = Lock()
        self.value = initial or unavailable("Snapshot loading")
        self.updated = self.updated_at = self.attempted = self.error = self.digest = None
        self.refreshing = False
        self.revision = 0
        self.duration = 0.0
        self.failures = 0

    def wait(self):
        """Seconds between refreshes: the interval, stretched to match a slow success; failures back off."""
        if self.error:
            return min(self.ttl * 2 ** min(self.failures, 5), max(self.ttl, self.MAX_FAILURE_WAIT))
        return min(max(self.ttl, self.duration), max(self.ttl, self.MAX_WAIT))

    def refresh(self):
        started = self.clock()
        try:
            value = self.loader()
            if not isinstance(value, dict):
                raise ValueError("snapshot must be an object")
            raw = json_bytes(value)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("snapshot exceeds size limit")
            digest = hashlib.blake2b(raw, digest_size=12).digest()
        except Exception as error:  # noqa: BLE001 - the previous good snapshot stays served
            with self.lock:
                self.error = f"{type(error).__name__}: {error}"[:240]
                self.failures += 1
        else:
            with self.lock:
                self.value, self.updated, self.updated_at = value, self.clock(), utc_now()
                self.error, self.failures = None, 0
                if digest != self.digest:
                    self.digest, self.revision = digest, self.revision + 1
                self.duration = self.updated - started
        finally:
            with self.lock:
                self.refreshing = False
                self.attempted = self.clock()

    def peek(self):
        """The current value without scheduling a refresh."""
        with self.lock:
            return self.value

    def get_with_tag(self):
        """Snapshot envelope plus an ETag that changes only when the served content does."""
        with self.lock:
            now = self.clock()
            if not self.refreshing and (self.attempted is None or now - self.attempted >= self.wait()):
                self.refreshing = True
                self.executor.submit(self.refresh)
            age = None if self.updated is None else max(0, now - self.updated)
            status = ("loading" if self.updated is None and not self.error else
                      "error" if self.updated is None else
                      "stale" if self.error or age >= self.wait() * 2 else "fresh")
            value = {**self.value, "_cache": {"status": status, "refreshing": self.refreshing,
                                              "updated_at": self.updated_at, "age_seconds": age,
                                              "error": self.error}}
            error = hashlib.blake2b((self.error or "").encode(), digest_size=4).hexdigest()
            return value, f'"{self.revision}-{status}-{error}"'

    def get(self):
        return self.get_with_tag()[0]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # a page load opens several connections at once

    def __init__(self, address, projects: list[Project], settings: Settings, default: str | None = None,
                 web_root=None, cache_dir=None, project_loader=None, telemetry_loader=None, executor_health=None):
        self.projects = {project.id: project for project in projects}
        self.settings = settings
        self.default = default if default in self.projects else next(iter(self.projects), "")
        self.web_root = Path(web_root or HERE / "web").resolve()
        self.history = HistoryStore(cache_dir)
        self.executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="snapshot")
        self.encoded: dict = {}
        self.encoded_lock = Lock()
        project_loader = project_loader or (lambda project: snapshots.project_snapshot(
            project, self.history, self.settings, self.default))
        self.caches = {("project", project.id): DashboardCache(lambda p=project: project_loader(p), self.executor,
                                                              settings.cache_seconds, snapshots.empty_project(project))
                       for project in projects}
        health = executor_health or snapshots.ExecutorHealth(settings.m2c_executors)
        telemetry_loader = telemetry_loader or (lambda: snapshots.telemetry(
            projects, self.default, self.project_values(), health))
        self.caches["telemetry", None] = DashboardCache(
            telemetry_loader, self.executor, settings.telemetry_seconds,
            {**unavailable("Telemetry loading"), "active": [], "recent": [], "m2c_projects": [], "executors": []})
        if settings.permuter_status:
            self.caches["permuter", None] = DashboardCache(
                lambda: permuter_snapshot(settings.permuter_status, settings.permuter_project), self.executor, 2,
                {**unavailable("Permuter farm status loading"), "kind": "permuter_farm"})
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, DashboardHandler)

    def project_values(self):
        """Latest project snapshots for the worker strip; asking also keeps them refreshing."""
        return {key: cache.get() for (kind, key), cache in self.caches.items() if kind == "project"}

    def public(self, selected=None):
        return public_config(list(self.projects.values()), self.settings, self.default, selected)

    def server_close(self):
        super().server_close()
        self.executor.shutdown(wait=False, cancel_futures=True)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):  # noqa: A002 - keep the console quiet; errors still surface
        pass

    def send_body(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_snapshot(self, key, value, tag):
        """Serve a cached snapshot, honouring If-None-Match and gzip; encodings are reused per ETag."""
        headers = {"ETag": tag, "Vary": "Accept-Encoding"}
        if (value.get("_cache") or {}).get("updated_at"):
            headers["X-Snapshot-At"] = value["_cache"]["updated_at"]
        offered = {item.strip().removeprefix("W/") for item in self.headers.get("If-None-Match", "").split(",")}
        if tag in offered or "*" in offered:
            self.send_body(304, b"", headers=headers)
            return
        with self.server.encoded_lock:
            entry = self.server.encoded.get(key)
        if entry is None or entry[0] != tag:
            raw = json_bytes(value)
            packed = None
            if len(raw) > 2048:
                compressor = zlib.compressobj(1, zlib.DEFLATED, 31)
                packed = compressor.compress(raw) + compressor.flush()
            entry = (tag, raw, packed)
            with self.server.encoded_lock:
                self.server.encoded[key] = entry
        _, raw, packed = entry
        if packed is not None and "gzip" in self.headers.get("Accept-Encoding", ""):
            self.send_body(200, packed, headers={**headers, "Content-Encoding": "gzip"})
        else:
            self.send_body(200, raw, headers=headers)

    def error(self, status, message):
        self.send_body(status, json_bytes({"error": message}))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except (ValueError, UnicodeError):
            self.error(400, "Invalid request path or query")
        except OSError:
            self.error(404, "Resource unavailable")

    def do_POST(self):
        self.error(405, "The dashboard is read-only")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def route(self):
        url = urlsplit(self.path)
        path = unquote(url.path, errors="strict")
        if "\x00" in path or "\\" in path or ".." in path.split("/") or path.startswith("//"):
            self.error(400, "Invalid request path")
            return
        query = parse_qs(url.query, keep_blank_values=True, max_num_fields=16)
        if any(len(values) != 1 for values in query.values()):
            self.error(400, "Duplicate query parameters")
            return
        server = self.server
        if path == "/api/config":
            self.send_body(200, json_bytes(server.public()))
            return
        if path == "/api/telemetry":
            value, tag = server.caches["telemetry", None].get_with_tag()
            self.send_snapshot(("telemetry", None), value, tag)
            return
        if path == "/api/permuter":
            if ("permuter", None) not in server.caches:
                self.error(404, "Permuter farm status is not configured")
                return
            value, tag = server.caches["permuter", None].get_with_tag()
            self.send_snapshot(("permuter", None), value, tag)
            return
        if path.startswith("/api/projects/"):
            key = path.removeprefix("/api/projects/")
            project = server.projects.get(key) if TOKEN.fullmatch(key) else None
            if project is None:
                self.error(404, "Unknown project")
                return
            value, tag = server.caches["project", key].get_with_tag()
            scope = None
            if "version" in query:
                version = query["version"][0]
                if version not in project.versions:
                    self.error(400, "Unknown version")
                    return
                value = {**value, "versions": {version: (value.get("versions") or {}).get(version, {})}}
                scope, tag = version, f'{tag[:-1]}-{version}"'
            self.send_snapshot(("project", key, scope), value, tag)
            return
        if path.startswith("/api/"):
            self.error(404, "Unknown API route")
            return
        selected = query.get("project", [server.default])[0]
        if path in ("/", "/index.html") and selected not in server.projects and server.projects:
            self.error(404, "Unknown project")
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        if any(part.startswith(".") for part in Path(relative).parts):
            self.error(404, "Resource unavailable")
            return
        file = contained(server.web_root, relative)
        if not file.is_file() or file.stat().st_size > MAX_JSON_BYTES:
            self.error(404, "Resource unavailable")
            return
        body = file.read_bytes()
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if file.suffix == ".js":
            mime = "text/javascript"
        if file.suffix == ".html":
            bootstrap = (json_bytes(server.public(selected)).decode()
                         .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
            script = f"<script>window.DashboardConfig={bootstrap};</script>"
            html = body.decode("utf-8")
            if "<!--DASHBOARD_CONFIG-->" in html:
                html = html.replace("<!--DASHBOARD_CONFIG-->", script)
            else:
                index = html.lower().find("<head>")
                html = html[:index + 6] + script + html[index + 6:] if index >= 0 else script + html
            body = html.encode("utf-8")
        self.send_body(200, body, mime + ("; charset=utf-8" if mime.startswith("text/") else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="interface to bind (default: dashboard.host, else 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default: dashboard.port, else 8765; 0 picks a free one)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--cache-dir", type=Path, default=HERE / ".cache",
                        help="where the dashboard keeps its own measurement history")
    args = parser.parse_args(argv)
    try:
        projects, settings, default = load_harness_config()
    except ValueError as error:
        parser.error(f"cannot load the dashboard configuration: {error}")
    if not projects:
        print("No projects configured: add them to harness.local.json (see harness.example.json).", flush=True)
    host = args.host or settings.host
    port = settings.port if args.port is None else args.port
    server = DashboardServer((host, port), projects, settings, default, cache_dir=args.cache_dir)
    for (kind, _), cache in server.caches.items():  # start the first snapshots before the first visitor
        cache.get()
    address = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{server.server_port}/"
    print(f"Dashboard: {address}", flush=True)
    if args.open:
        threading.Timer(0.5, webbrowser.open, (address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

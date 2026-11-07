#!/usr/bin/env python3
"""Serve an exact m2c checkout over a small hash-bound HTTP protocol.

The server binds 127.0.0.1 unless --host says otherwise; expose it to other machines only
through a trusted tunnel or network. Runs on POSIX and Windows.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


SCHEMA_VERSION = 1
MAX_REQUEST = 32 * 1024 * 1024


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(tool: Path) -> tuple[str, str]:
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tool,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    if not revision:
        raise ValueError("m2c checkout revision is unavailable")
    digest = hashlib.sha256(revision.encode())
    files = [tool / "m2c.py", *sorted((tool / "m2c").rglob("*.py")),
             *sorted((tool / "m2c_pycparser").glob("*.py"))]
    for path in files:
        digest.update(str(path.relative_to(tool)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return revision, digest.hexdigest()


class Worker:
    def __init__(self, tool: Path, cache: Path):
        self.tool = tool.resolve()
        self.cache = cache.resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.revision, self.fingerprint = fingerprint(self.tool)
        self.identity = f"{socket.gethostname()}:{os.getpid()}"

    def run(self, payload: dict) -> dict:
        supplied_hash = payload.get("job_sha256")
        unsigned = {key: value for key, value in payload.items() if key != "job_sha256"}
        actual_hash = hashlib.sha256(canonical(unsigned)).hexdigest()
        if supplied_hash != actual_hash:
            raise ValueError("Job SHA-256 does not match payload")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported job schema")
        if payload.get("tool_fingerprint") != self.fingerprint:
            raise ValueError("Worker m2c fingerprint does not match requested tool")
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol or len(symbol) > 512:
            raise ValueError("Invalid function symbol")
        timeout = min(max(int(payload.get("timeout") or 45), 1), 300)
        saved = self.cache / actual_hash / "response.json"
        if saved.is_file():
            result = json.loads(saved.read_text(encoding="utf-8"))
            result["cache_origin_worker"] = result.get("worker")
            result["worker"] = self.identity
            result["cache_hit"] = True
            return result
        with tempfile.TemporaryDirectory(prefix="m2c-worker-") as temporary:
            root = Path(temporary)
            target, context = root / "target.s", root / "context.c"
            # Byte-exact inputs: no platform newline translation.
            target.write_bytes((payload.get("assembly") or "").encode("utf-8"))
            context.write_bytes((payload.get("context") or "").encode("utf-8"))
            options = payload.get("options") or {}
            command = [sys.executable, str(self.tool / "m2c.py"), "-t", "ppc-mwcc-c",
                       "--no-cache", "--valid-syntax"]
            if not options.get("infer_unknowns"):
                command.append("--no-unk-inference")
            if options.get("explicit_fields"):
                command.append("--explicit-field-access")
            if options.get("deterministic_vars"):
                command.append("--deterministic-vars")
            declaration_mode = options.get("declaration_mode")
            if declaration_mode:
                command.extend(["--globals", str(declaration_mode)])
            if options.get("typed"):
                command.extend(["--context", str(context)])
            command.extend(["-f", symbol, str(target)])
            started = time.monotonic()
            try:
                process = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", timeout=timeout,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                returncode, stdout, stderr = process.returncode, process.stdout, process.stderr
            except subprocess.TimeoutExpired as error:
                returncode = 124
                stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
                stderr = f"m2c exceeded {timeout} seconds"
            result = {"schema_version": SCHEMA_VERSION, "job_sha256": actual_hash,
                      "tool_fingerprint": self.fingerprint, "tool_revision": self.revision,
                      "worker": self.identity, "executor": "remote", "cache_hit": False,
                      "execution_seconds": round(time.monotonic() - started, 6),
                      "returncode": returncode, "stdout": stdout, "stderr": stderr}
        saved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=saved.parent, delete=False,
                                         encoding="utf-8") as output:
            json.dump(result, output, indent=2)
        Path(output.name).replace(saved)
        return result


class Handler(BaseHTTPRequestHandler):
    server_version = "m2c-worker/1"

    def reply(self, status: int, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/v1/health":
            self.reply(404, {"error": "not found"})
            return
        worker = self.server.worker
        self.reply(200, {"schema_version": SCHEMA_VERSION, "status": "ok",
                         "worker": worker.identity, "tool_revision": worker.revision,
                         "tool_fingerprint": worker.fingerprint})

    def do_POST(self) -> None:
        if self.path != "/v1/decompile":
            self.reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_REQUEST:
                raise ValueError("Invalid request size")
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            result = self.server.worker.run(json.loads(body))
            self.reply(200, result)
        except Exception as error:
            self.reply(400, {"schema_version": SCHEMA_VERSION,
                             "error": f"{type(error).__name__}: {error}"})

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    worker = Worker(args.tool, args.cache)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.worker = worker
    print(json.dumps({"status": "ready", "worker": worker.identity,
                      "tool_fingerprint": worker.fingerprint, "port": args.port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

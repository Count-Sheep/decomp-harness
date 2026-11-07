"""Hash-verified execution pool for local and remote m2c workers."""

from __future__ import annotations

import gzip
import hashlib
import json
import queue
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any


SCHEMA_VERSION = 1
_pool: queue.Queue[str] | None = None
_specs: list[dict[str, Any]] = []
_lock = threading.Lock()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def job_payload(symbol: str, assembly: bytes, context: str, options: dict,
                tool_fingerprint: str, timeout: int) -> dict:
    payload = {"schema_version": SCHEMA_VERSION, "symbol": symbol,
               "assembly": assembly.decode("utf-8"), "context": context,
               "options": options, "tool_fingerprint": tool_fingerprint,
               "timeout": timeout}
    payload["job_sha256"] = hashlib.sha256(canonical(payload)).hexdigest()
    return payload


def parse_specs(values: list[str]) -> list[dict[str, Any]]:
    result = []
    for value in values:
        name, separator, raw_slots = value.rpartition("=")
        if not separator or not name or not raw_slots.isdigit() or int(raw_slots) < 1:
            raise ValueError("Executor must be local=SLOTS or URL=SLOTS")
        if name != "local" and not name.startswith(("http://", "https://")):
            raise ValueError("Remote executor must use an HTTP(S) URL")
        result.append({"executor": name.rstrip("/"), "slots": int(raw_slots)})
    if not result:
        raise ValueError("At least one executor is required")
    return result


def configure(values: list[str] | None) -> list[dict[str, Any]]:
    """Install a process-wide slot pool before a batch starts."""
    global _pool, _specs
    with _lock:
        if not values:
            _pool, _specs = None, []
            return []
        specs = parse_specs(values)
        pool: queue.Queue[str] = queue.Queue()
        for spec in specs:
            for _ in range(spec["slots"]):
                pool.put(spec["executor"])
        _pool, _specs = pool, specs
        return [dict(row) for row in specs]


def slots() -> int:
    return sum(row["slots"] for row in _specs)


def remote_health(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/v1/health", timeout=timeout) as response:
        return json.loads(response.read())


def verify_executors(expected_fingerprint: str, timeout: float = 5) -> list[dict]:
    """Fail before a batch starts if any configured remote is unavailable or stale."""
    health = []
    for executor in dict.fromkeys(row["executor"] for row in _specs):
        if executor == "local":
            continue
        result = remote_health(executor, timeout)
        if result.get("schema_version") != SCHEMA_VERSION or result.get("status") != "ok":
            raise RuntimeError(f"Remote m2c worker is unhealthy: {executor}")
        if result.get("tool_fingerprint") != expected_fingerprint:
            raise RuntimeError(f"Remote m2c worker has the wrong tool fingerprint: {executor}")
        health.append({"executor": executor, "worker": result.get("worker"),
                       "tool_fingerprint": result.get("tool_fingerprint")})
    return health


def remote_execute(url: str, payload: dict, timeout: int) -> dict:
    body = gzip.compress(canonical(payload), compresslevel=3)
    request = urllib.request.Request(url.rstrip("/") + "/v1/decompile", data=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout + 15) as response:
        result = json.loads(response.read())
    if result.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Remote m2c worker returned an unsupported schema")
    if result.get("job_sha256") != payload["job_sha256"]:
        raise RuntimeError("Remote m2c worker returned a different job")
    if result.get("tool_fingerprint") != payload["tool_fingerprint"]:
        raise RuntimeError("Remote m2c worker used a different tool tree")
    result.setdefault("transport_seconds", round(time.monotonic() - started, 6))
    return result


def execute(payload: dict, local: Callable[[], dict]) -> dict:
    """Run on the next available weighted executor; never silently cross executors."""
    pool = _pool
    if pool is None:
        result = local()
        result["execution"] = {"executor": "local"}
        return result
    executor = pool.get()
    try:
        if executor == "local":
            result = local()
            result["execution"] = {"executor": "local"}
            return result
        result = remote_execute(executor, payload, int(payload["timeout"]))
        return {"returncode": int(result["returncode"]), "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "execution": {key: result.get(key) for key in
                              ("executor", "worker", "job_sha256", "tool_fingerprint",
                               "cache_hit", "cache_origin_worker", "execution_seconds",
                               "transport_seconds")}}
    finally:
        pool.put(executor)

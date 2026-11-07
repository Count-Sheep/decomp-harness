"""Read-only m2c / draft / promotion / worker snapshots for configured projects.

The harness owns the artifacts and their summarisers: ``local_campaign_m2c_report.load_report`` builds
the m2c report (history, active runs and stages) and ``m2c_unit_queue.snapshot`` verifies unit-promotion
evidence. This module only calls them, adds the unit-gate progress of active runs, and reads the draft
attempt ledger. Process observation goes through ``processes.shared_snapshot`` on every platform.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import processes
from providers import (HARNESS_DIR, HistoryStore, MAX_JSON_BYTES, Project, Settings, build_check, categories,
                       measures, merge_history, newest, project_history, read_json, read_object,
                       read_report, stats_delta, unavailable)

_m2c_report = None
_report_lock = threading.Lock()


def m2c_report_module():
    """The harness's m2c report module, with its process scan replaced by the portable shared one."""
    global _m2c_report
    with _report_lock:
        if _m2c_report is None:
            import local_campaign_m2c_report as module
            # The harness lists runners with `ps`, which Windows lacks; share one portable scan instead.
            module.process_snapshot = process_rows
            _m2c_report = module
        return _m2c_report


def process_rows():
    """The shared portable process scan (``None`` when processes cannot be observed)."""
    return processes.shared_snapshot()


def support_dir(root: Path, version: str) -> Path:
    return root / "build" / "decomp-support" / version


# ── m2c report and unit-gate progress ───────────────────────────────────────────────────────


def m2c_snapshot(root: Path, version: str) -> dict[str, Any]:
    support = support_dir(root, version)
    if not (support / "m2c-report.json").is_file() and not any((support / "batches").glob("*/manifest.json")):
        return {}
    try:
        module = m2c_report_module()
    except Exception as error:  # noqa: BLE001 - an optional harness module must not break the page
        return {**read_object(support / "m2c-report.json"), "activity_available": False,
                "harness_error": f"{type(error).__name__}: {error}"[:240]}
    report = module.load_report(support.resolve())
    # The check time changes on every call; the cache's own timestamp says when it was taken. Dropping it keeps
    # the snapshot (and its ETag) unchanged while the artifacts are.
    report.pop("activity_checked_at", None)
    attach_gate_progress(support, report)
    return report


GATE_STAGES = ("refinement", "owner_trial", "asm_backed_trial", "unit_gate", "promotion", "candidate_bank",
               "visible_units", "finalizing")
GATE_FIELDS = ("ready_units", "owner_trials_run", "owner_objects_verified", "owner_links_verified", "owner_promoted",
               "gates_run", "links_verified", "promoted", "candidate_banked", "candidate_linked", "candidate_visible")
VISIBLE_STATES = ("promoted", "nothing_to_promote", "rejected")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def attach_gate_progress(support: Path, report: dict[str, Any]) -> None:
    """Keep the promotion loop's totals and auto-promote setting on each active run.

    Current harnesses put ``unit_gate`` and ``auto_promote`` on the live run; otherwise they are read
    from the run's refinement results. Gate totals are reduced to their numeric fields.
    """
    for run in report.get("active_runs") or []:
        run_id = str(run.get("run_id") or "")
        if run.get("stage") not in GATE_STAGES or not _RUN_ID.fullmatch(run_id):
            continue
        gate = run.get("unit_gate")
        if not isinstance(gate, dict) or not isinstance(run.get("auto_promote"), bool):
            results = read_object(support / "refinement-runs" / run_id / "results.json")
            if not isinstance(run.get("auto_promote"), bool) and isinstance(results.get("auto_promote"), bool):
                run["auto_promote"] = results["auto_promote"]
            if not isinstance(gate, dict):
                gate = results.get("unit_gate")
        if isinstance(gate, dict):
            visible = visible_summary(gate.get("visible_units"))
            run["unit_gate"] = {key: gate[key] for key in GATE_FIELDS if _integer(gate.get(key))}
            if visible:
                run["unit_gate"]["visible_units"] = visible
        if run.get("stage") == "visible_units":
            attach_visible_progress(support, run)


def visible_summary(value: Any) -> dict[str, Any] | None:
    """The gate's visible-units outcome, reduced to display fields."""
    if not isinstance(value, dict):
        return None
    summary = {key: value[key] for key in ("units", "functions") if _integer(value.get(key))}
    if value.get("state") in VISIBLE_STATES:
        summary["state"] = value["state"]
    for key in ("commit", "error"):
        if isinstance(value.get(key), str) and value[key]:
            summary[key] = value[key][:240]
    return summary or None


def attach_visible_progress(support: Path, run: dict[str, Any]) -> None:
    """Live progress and heartbeat of the visible-units step, which rewrites its result file as it works."""
    gates = (support / "unit-gates").resolve()
    output = Path(str(read_object(support / "unit-gates" / "active.json").get("output") or ""))
    try:
        path = output.resolve() / "visible-units" / "result.json"
        path.relative_to(gates)
        modified = path.stat().st_mtime
    except (OSError, ValueError):
        return
    progress = read_object(path)
    if progress.get("kind") != "m2c_visible_units":
        return
    run["visible_progress"] = {key: progress[key] for key in
                               ("units_considered", "processed_units", "verified_units", "verified_functions")
                               if _integer(progress.get(key))}
    if isinstance(progress.get("phase"), str):
        run["visible_progress"]["phase"] = progress["phase"][:40]
    beat = datetime.fromtimestamp(modified, UTC).isoformat()
    if beat > str(run.get("heartbeat_at") or run.get("updated_at") or ""):
        run["heartbeat_at"] = beat


def m2c_card(project: Project, version: str, report: dict[str, Any]) -> dict[str, Any]:
    """Compact per-project m2c progress for the worker strip."""
    latest = report.get("latest") or None
    validation = (latest or {}).get("validation_progress") or (latest or {}).get("validation") or {}
    counts = validation.get("counts") or {}
    gate = ((latest or {}).get("validation") or {}).get("refinement", {}).get("unit_gate") or {}
    runs = report.get("active_runs") or []
    summary = None
    if latest:
        scored = None
        if any(_integer(counts.get(key)) for key in ("non_exact", "scorer_exact")):
            scored = int(counts.get("non_exact") or 0) + int(counts.get("scorer_exact") or 0)
        summary = {"clean": (latest.get("counts") or {}).get("ok"), "total": latest.get("total"),
                   "compiled": validation.get("compiler_succeeded"), "validation_total": validation.get("total"),
                   "scored": scored, "scorer_exact": counts.get("scorer_exact"),
                   "promoted": gate.get("promoted") if _integer(gate.get("promoted")) else None,
                   "visible": visible_summary(gate.get("visible_units"))}
    active = runs[0] if runs else None
    if active:
        active = {key: active.get(key) for key in ("stage", "processed", "total", "updated_at", "heartbeat_at",
                                                   "unit_gate", "auto_promote", "state")}
    return {"project": project.id, "name": project.name, "version": version,
            "available": bool(report), "active": active, "latest": summary}


# ── Unit promotions ─────────────────────────────────────────────────────────────────────────


def unit_promotions(root: Path, version: str) -> dict[str, Any]:
    folder = support_dir(root, version) / "unit-promotions"
    if not folder.is_dir():
        return {"records": [], "units": [], "unit_count": 0}
    try:
        import m2c_unit_queue
        return m2c_unit_queue.snapshot(root, version)
    except Exception as error:  # noqa: BLE001 - optional harness provider
        return {**unavailable(f"Unit promotion evidence unavailable: {type(error).__name__}: {error}"[:300]),
                "records": [], "units": []}


# ── Draft attempt ledger (m2c_draft_attempts.py) ────────────────────────────────────────────

DRAFT_SCRIPT = "m2c_draft_attempts.py"


def harness_scripts() -> set[str]:
    names = {path.name for folder in (HARNESS_DIR, HARNESS_DIR / "permuter") for path in folder.glob("*.py")
             if not path.name.startswith("test_") and path.name != "__init__.py"}
    return names or {DRAFT_SCRIPT}


def process_target(row: dict[str, Any], projects: list[Project], default: str | None) -> tuple[Project | None, str | None]:
    """The configured project and version a harness process works on, from its --root/--version."""
    argv = row.get("argv") or []
    root, version = processes.option(argv, "--root"), processes.option(argv, "--version")
    project = None
    if root:
        try:
            resolved = Path(root).resolve()
        except (OSError, ValueError):
            resolved = None
        project = next((item for item in projects if item.root == resolved), None)
    elif default:
        # Without --root the harness uses its configured default project.
        project = next((item for item in projects if item.id == default), None)
    if project is None:
        return None, version
    return project, version or project.default_version


def draft_worker(project: Project, version: str, rows: list[dict[str, Any]] | None,
                 default: str | None) -> dict[str, Any] | None:
    for row in rows or []:
        if processes.script(row.get("argv") or [], {DRAFT_SCRIPT}) is None or str(row.get("state", "")).startswith("Z"):
            continue
        owner, owner_version = process_target(row, [project], default)
        if owner is project and owner_version == version:
            return {"pid": row["pid"], "elapsed_seconds": row.get("elapsed_seconds")}
    return None


def attempt_state(root: Path, version: str) -> dict[str, Any]:
    path = support_dir(root, version) / "attempts" / "state.json"
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return {"oversized_state": True}
        value = read_json(path)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _loopback(host: str) -> bool:
    name = urllib.parse.urlsplit(host).hostname or ""
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


_model_probes: dict[str, tuple[float, tuple]] = {}
_probe_lock = threading.Lock()
MODEL_PROBE_SECONDS = 10.0


def probe_models(host: str) -> tuple[list[dict[str, Any]], str | None, int | None]:
    """``GET <host>/models`` of an OpenAI-compatible RPC: (model rows, error, latency ms), cached per host.

    The cache keeps an unreachable host (which can take the whole timeout to fail) from slowing every refresh.
    """
    with _probe_lock:
        cached = _model_probes.get(host)
        if cached and time.monotonic() - cached[0] < MODEL_PROBE_SECONDS:
            return cached[1]
    started, result = time.monotonic(), ([], None, None)
    try:
        with urllib.request.urlopen(host + "/models", timeout=1.5) as response:
            payload = json.loads(response.read(1024 * 1024))
        data = (payload.get("data") or []) if isinstance(payload, dict) else []
        result = ([row for row in data if isinstance(row, dict)], None, round((time.monotonic() - started) * 1000))
    except (OSError, ValueError, AttributeError, urllib.error.URLError, TimeoutError) as caught:
        result = ([], str(caught)[:240], None)
    with _probe_lock:
        _model_probes[host] = (time.monotonic(), result)
    return result


def model_runtime(state: dict[str, Any], worker: dict[str, Any] | None, probe_remote: bool = False) -> dict[str, Any]:
    """Whether the attempt worker and its OpenAI-compatible model RPC are live.

    Only loopback model hosts are probed unless ``dashboard.probe_remote_model_hosts`` is set.
    """
    host = str(state.get("host") or "").rstrip("/")
    model = str(state.get("model") or "")
    if not host and not model:
        return {}
    probed = bool(host) and host.startswith(("http://", "https://")) and (probe_remote or _loopback(host))
    rows, error, latency = probe_models(host) if probed else ([], None, None)
    loaded = [str(row.get("id")) for row in rows if row.get("id")]
    selected = next((row for row in rows if row.get("id") == model), {})
    backend, context_size = selected.get("owned_by"), (selected.get("meta") or {}).get("n_ctx")
    reachable = probed and error is None
    model_loaded = bool(model and model in loaded)
    status = ("running" if reachable and model_loaded and worker else "model_ready" if reachable and model_loaded
              else "model_not_loaded" if reachable else "rpc_unavailable" if worker and probed
              else "not_probed" if worker else "stopped")
    return {"status": status, "rpc_reachable": reachable, "model_loaded": model_loaded, "model": model or None,
            "probed": probed, "backend": backend, "context_size": context_size, "latency_ms": latency,
            "worker": worker, "rpc_error": error or state.get("rpc_error")}


def draft_attempt_tracker(project: Project, version: str, rows: list[dict[str, Any]] | None,
                          default: str | None, settings: Settings) -> dict[str, Any]:
    state = attempt_state(project.root, version)
    if not state:
        return {}
    if state.get("oversized_state"):
        return {"oversized_state": True}
    items = [item for item in (state.get("items") or {}).values() if isinstance(item, dict)]
    attempts = [(item, attempt) for item in items for attempt in item.get("attempts") or [] if isinstance(attempt, dict)]
    draft_checks = [attempt for _, attempt in attempts if attempt.get("index") == 0]
    recent = sorted(({"symbol": item.get("symbol"), "index": attempt.get("index"), "at": attempt.get("at"),
                      "pct": attempt.get("pct"), "delta": attempt.get("delta"), "compiled": attempt.get("compiled"),
                      "profile": attempt.get("profile")} for item, attempt in attempts),
                    key=lambda row: str(row["at"] or ""), reverse=True)[:12]
    selected = state.get("current") or (recent[0]["symbol"] if recent else None)
    current = (state.get("items") or {}).get(selected) or {}
    last = (current.get("attempts") or [{}])[-1]
    worker = draft_worker(project, version, rows, default)
    return {"model": state.get("model"), "current": state.get("current"), "current_pct": current.get("best_pct"),
            "current_delta": last.get("delta") if isinstance(last, dict) else None,
            "updated_at": state.get("updated_at"), "batch": state.get("batch"),
            "model_runtime": model_runtime(state, worker, settings.probe_remote_model_hosts),
            "draft_batch_finished": state.get("draft_batch_finished"),
            "attempted": sum(any(isinstance(row, dict) and (row.get("index") or 0) > 0 for row in item.get("attempts") or [])
                             for item in items),
            "exact": sum(_float(item.get("best_pct")) == 100 for item in items),
            "total": state.get("target_total", len(items)), "recent": recent,
            "draft_validation": {"checked": len(draft_checks),
                                 "compiled": sum(bool(row.get("compiled")) for row in draft_checks),
                                 "scored": sum(row.get("profile") is not None for row in draft_checks),
                                 "scorer_exact": sum(row.get("profile") is not None and _float(row.get("pct")) == 100
                                                     for row in draft_checks)}}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Project snapshot ────────────────────────────────────────────────────────────────────────


def version_snapshot(project: Project, version: str, history: HistoryStore, settings: Settings,
                     default: str | None, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    root = project.root
    report, source = read_report(root, version)
    build = build_check(root, version)
    values = measures(report)
    if source["status"] == "available":
        edited = newest(root, ("src", "include", f"config/{version}", "configure.py"))
        source["report_behind_source"] = edited["mtime_ns"] > source["report_mtime_ns"] if edited else None
    points = merge_history(project_history(root, version), history.record(project.id, version, source, values, build))
    data = {"measures": values, "categories": categories(report), "report_source": source, "build": build,
            "progress_history": points, "stats_delta": stats_delta(points, values, source.get("report_at"))}
    for key, reader in (("m2c", lambda: m2c_snapshot(root, version)),
                        ("unit_promotions", lambda: unit_promotions(root, version)),
                        ("draft_attempts", lambda: draft_attempt_tracker(project, version, rows, default, settings))):
        try:
            data[key] = reader()
        except Exception as error:  # noqa: BLE001 - one broken artifact must not hide the rest
            data[key] = unavailable(f"{type(error).__name__}: {error}"[:300])
    return data


def project_snapshot(project: Project, history: HistoryStore, settings: Settings, default: str | None) -> dict[str, Any]:
    rows = processes.shared_snapshot()
    return {"project": project.id, "name": project.name, "default_version": project.default_version,
            "processes_available": rows is not None,
            "versions": {version: version_snapshot(project, version, history, settings, default, rows)
                         for version in project.versions}}


def empty_project(project: Project) -> dict[str, Any]:
    return {"project": project.id, "name": project.name, "default_version": project.default_version,
            "versions": {version: {"measures": {}, "categories": [], "progress_history": [], "stats_delta": None,
                                   "build": {"status": "unknown"},
                                   "report_source": {"status": "unavailable", "version": version},
                                   "m2c": {}, "unit_promotions": {"records": [], "units": []}, "draft_attempts": {}}
                         for version in project.versions}}


# ── Telemetry: harness processes, recent attempts, m2c cards, remote executors ──────────────


class ExecutorHealth:
    """``/v1/health`` of configured remote m2c executors (harness ``m2c_remote``), at most every ``ttl`` seconds.

    Probes run on a background thread (``background=True``) so an unreachable executor, which can take the
    whole timeout to fail, never delays telemetry; until the first probe finishes rows say ``checking``.
    """

    def __init__(self, executors: list[str], ttl: float = 10, timeout: float = 2, fetch=None, background: bool = True):
        self.executors, self.ttl, self.timeout, self.background = executors, ttl, timeout, background
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.checked: float | None = None
        self.running = False
        self.rows = [{**self.spec(item), "status": "checking"} for item in executors]

    @staticmethod
    def spec(value: str) -> dict[str, Any]:
        url, separator, slots = value.rpartition("=")
        if not separator or not slots.isdigit():
            url, slots = value, ""
        return {"executor": url.rstrip("/"), "slots": int(slots) if slots else None}

    @staticmethod
    def _fetch(url: str, timeout: float) -> dict[str, Any]:
        try:
            from m2c_remote import remote_health
        except ImportError:
            remote_health = None
        if remote_health is not None:
            return remote_health(url, timeout)
        with urllib.request.urlopen(url.rstrip("/") + "/v1/health", timeout=timeout) as response:
            return json.loads(response.read(64 * 1024))

    def probe(self) -> None:
        rows = []
        for item in self.executors:
            row, started = self.spec(item), time.monotonic()
            try:
                value = self.fetch(row["executor"], self.timeout)
                if not isinstance(value, dict):
                    raise ValueError("health response is not an object")
                row.update(status="ok" if value.get("status") == "ok" else "unhealthy",
                           worker=value.get("worker"), tool_revision=value.get("tool_revision"),
                           tool_fingerprint=str(value.get("tool_fingerprint") or "")[:16] or None,
                           latency_ms=round((time.monotonic() - started) * 1000))
            except Exception as error:  # noqa: BLE001 - network faults are reported, not raised
                row.update(status="unreachable", error=f"{type(error).__name__}: {error}"[:200])
            rows.append(row)
        with self.lock:
            self.rows, self.checked, self.running = rows, time.monotonic(), False

    def __call__(self) -> list[dict[str, Any]]:
        if not self.executors:
            return []
        with self.lock:
            due = not self.running and (self.checked is None or time.monotonic() - self.checked >= self.ttl)
            if due:
                self.running = True
        if due:
            if self.background:
                threading.Thread(target=self.probe, name="executor-health", daemon=True).start()
            else:
                self.probe()
        with self.lock:
            return [dict(row) for row in self.rows]


def telemetry(projects: list[Project], default: str | None, project_values: dict[str, dict[str, Any]],
              executors: ExecutorHealth | None = None) -> dict[str, Any]:
    """Global worker summary: active harness processes, recent draft attempts and per-project m2c cards.

    ``project_values`` are the latest project snapshots (from the server's caches), so m2c reports are
    not rebuilt here. No prompts, model responses or source text are included.
    """
    rows = processes.shared_snapshot()
    names = harness_scripts()
    active, recent, cards = [], [], []
    runs_by_pid = {}
    for project in projects:
        for version, data in ((project_values.get(project.id) or {}).get("versions") or {}).items():
            report = data.get("m2c") or {}
            for run in report.get("active_runs") or []:
                runs_by_pid[run.get("pid")] = run
            cards.append(m2c_card(project, version, report))
            attempts = data.get("draft_attempts") or {}
            for row in attempts.get("recent") or []:
                recent.append({"project": project.id, "version": version, "worker": attempts.get("model"),
                               "symbol": row.get("symbol"), "attempt": row.get("index"),
                               "delta": row.get("delta"), "at": row.get("at")})
    for row in rows or []:
        found = processes.script(row.get("argv") or [], names)
        if found is None or str(row.get("state", "")).startswith("Z"):
            continue
        name, _ = found
        project, version = process_target(row, projects, default)
        run = runs_by_pid.get(row["pid"]) or {}
        item = {"project": project.id if project else None, "version": version, "script": name, "pid": row["pid"],
                "elapsed_seconds": row.get("elapsed_seconds"), "phase": run.get("stage") or "running",
                "symbol": run.get("current_function"), "updated_at": run.get("updated_at")}
        if name == DRAFT_SCRIPT and project and version in project.versions:
            state = (((project_values.get(project.id) or {}).get("versions") or {}).get(version) or {}).get("draft_attempts") or {}
            item.update(symbol=state.get("current"), worker=state.get("model"),
                        phase="rpc_retry" if (state.get("model_runtime") or {}).get("rpc_error") else "attempt",
                        updated_at=state.get("updated_at"))
        active.append(item)
    # A runner's child processes (m2c, compilers) are not separate workers.
    pids = {item["pid"] for item in active}
    parents = {row["pid"]: row.get("parent") for row in rows or []}
    active = [item for item in active if parents.get(item["pid"]) not in pids]
    active.sort(key=lambda item: (str(item.get("project") or ""), item["pid"]))
    recent.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return {"processes_available": rows is not None, "active": active,
            "recent": recent[:30], "m2c_projects": cards, "executors": executors() if executors else []}

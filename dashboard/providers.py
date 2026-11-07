"""Configuration, strict JSON helpers and the read-only report / build / permuter readers.

The dashboard reuses the harness configuration (``DECOMP_HARNESS_CONFIG`` or ``<repo>/harness.local.json``,
loaded by ``harness/harness_paths.py``): its ``projects`` are the selectable projects and its optional
``dashboard`` section holds the server settings. Nothing in this module writes into a project checkout.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HARNESS_DIR = HERE.parent / "harness"
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

MAX_JSON_BYTES = 32 * 1024 * 1024
EXPONENT = re.compile(rb"[0-9][eE][+-]?0*[0-9]{3}")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MEASURE_KEYS = ("fuzzy_match_percent", "matched_code", "matched_code_percent", "matched_functions",
                "matched_functions_percent", "complete_code", "complete_code_percent", "total_code",
                "total_functions")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def parse_json(raw: bytes | str) -> Any:
    def invalid(_value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, parse_constant=invalid)


def read_json(path: Path) -> Any:
    """Parse a JSON file of at most ``MAX_JSON_BYTES``, rejecting NaN/Infinity and overflowing numbers."""
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds size limit")
    value = parse_json(raw)
    if EXPONENT.search(raw):  # only a three-digit exponent can overflow to inf
        json_bytes(value)
    return value


def read_object(path: Path) -> dict[str, Any]:
    """A JSON object from ``path``, or ``{}`` when it is missing, invalid or not an object."""
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def contained(root: Path, relative: str | Path) -> Path:
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must be relative and contained")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes configured root")
    return path


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "status": "unavailable", "reason": reason}


# ── Configuration ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: Path
    versions: tuple[str, ...]
    default_version: str


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    cache_seconds: float = 2.0
    telemetry_seconds: float = 1.0
    permuter_status: Path | None = None
    permuter_project: str | None = None
    m2c_executors: list[str] = field(default_factory=list)
    probe_remote_model_hosts: bool = False
    workers: int = 4


def _number(raw: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"dashboard.{key} must be a number from {low} to {high}")
    return value


def parse_settings(raw: Any, resolve=lambda value: Path(value).resolve(), farm_root: Any = None) -> Settings:
    """Validate the harness config's ``dashboard`` section. ``resolve`` maps configured paths."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("dashboard settings must be an object")
    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise ValueError("dashboard.host must be a string")
    port = raw.get("port", 8765)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("dashboard.port must be 0-65535")
    executors = raw.get("m2c_executors") or []
    if not isinstance(executors, list) or any(not isinstance(item, str) or not re.match(r"https?://", item)
                                              for item in executors):
        raise ValueError("dashboard.m2c_executors must be a list of http(s) URLs")
    status = raw.get("permuter_status")
    if status is not None and (not isinstance(status, str) or not status):
        raise ValueError("dashboard.permuter_status must be a path")
    if status is None and isinstance(farm_root, str) and farm_root:
        candidate = resolve(farm_root) / "state" / "status.json"
        status = str(candidate) if candidate.is_file() else None
    project = raw.get("permuter_project")
    if project is not None and (not isinstance(project, str) or not TOKEN.fullmatch(project)):
        raise ValueError("dashboard.permuter_project must be a project id")
    workers = raw.get("workers", 4)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("dashboard.workers must be 1-8")
    return Settings(host=host, port=port, cache_seconds=_number(raw, "cache_seconds", 2, 0.1, 3600),
                    telemetry_seconds=_number(raw, "telemetry_seconds", 1, 0.1, 3600),
                    permuter_status=resolve(status) if status else None, permuter_project=project,
                    m2c_executors=list(dict.fromkeys(executors)),
                    probe_remote_model_hosts=raw.get("probe_remote_model_hosts") is True, workers=workers)


def make_projects(entries) -> list[Project]:
    """Dashboard projects from harness ``Project`` objects (id, name, root, version(s))."""
    result, seen = [], set()
    for entry in entries:
        if not TOKEN.fullmatch(str(entry.id)) or entry.id in seen:
            raise ValueError(f"invalid or duplicate project id {entry.id!r}")
        seen.add(entry.id)
        versions = tuple(dict.fromkeys(str(item) for item in (entry.versions or (entry.version,)) if item))
        if any(not TOKEN.fullmatch(version) for version in versions):
            raise ValueError(f"project {entry.id!r} has a version that is not a path-safe identifier")
        default = entry.version if entry.version in versions else (versions[0] if versions else "")
        result.append(Project(id=entry.id, name=str(entry.name or entry.id), root=Path(entry.root).resolve(),
                              versions=versions, default_version=default))
    return result


def load_harness_config() -> tuple[list[Project], Settings, str]:
    """Projects, dashboard settings and the default project id from the harness configuration."""
    import harness_paths

    configured = harness_paths.projects()
    if not configured and harness_paths.SELECTED.version:
        configured = [harness_paths.SELECTED]  # DECOMP_ROOT alone, without a config file
    projects = make_projects(configured)
    # The farm's own default location, so a farm started without config is still found.
    farm_root = harness_paths.setting("permuter", "farm_root") or \
        str(harness_paths.SELECTED.root / "build" / "permuter-farm")
    settings = parse_settings(harness_paths.setting("dashboard"), harness_paths.resolve, farm_root)
    ids = [project.id for project in projects]
    default = harness_paths.PROJECT_ID if harness_paths.PROJECT_ID in ids else (ids[0] if ids else "")
    return projects, settings, default


def public_config(projects: list[Project], settings: Settings, default: str, selected: str | None = None):
    """The registry the browser sees: no roots or other local paths."""
    return {"schema_version": 1, "default_project": default, "selected_project": selected or default,
            "projects": [{"id": p.id, "name": p.name, "versions": list(p.versions),
                          "default_version": p.default_version, "project_url": f"/api/projects/{p.id}"}
                         for p in projects],
            "permuter_farm": ({"url": "/api/permuter", "project": settings.permuter_project}
                              if settings.permuter_status else None),
            "m2c_executors": len(settings.m2c_executors)}


# ── Report, build and history ───────────────────────────────────────────────────────────────


def read_report(root: Path, version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """``build/<VERSION>/report.json`` and its provenance; an empty report unless it is complete and stable."""
    path = root / "build" / version / "report.json"
    source = {"path": str(path), "version": version, "status": "missing", "report_at": None,
              "report_mtime_ns": None, "sha256": None, "report_behind_source": None}
    try:
        path = contained(root, f"build/{version}/report.json")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("report exceeds size limit")
            after = os.fstat(handle.fileno())
        identity = lambda stat: (stat.st_size, stat.st_mtime_ns)  # noqa: E731
        if identity(before) != identity(after) or identity(after) != identity(path.stat()):
            source["status"] = "changing"
            return {}, source
        report = parse_json(raw)
        json_bytes(report)
        if not isinstance(report, dict) or not isinstance(report.get("measures"), dict) or not report["measures"]:
            raise ValueError("report measures unavailable")
        source.update(status="available", sha256=hashlib.sha256(raw).hexdigest(), report_mtime_ns=after.st_mtime_ns,
                      report_at=datetime.fromtimestamp(after.st_mtime, UTC).isoformat())
        return report, source
    except FileNotFoundError:
        return {}, source
    except (OSError, ValueError):
        source["status"] = "invalid"
        return {}, source


def measures(report: dict[str, Any]) -> dict[str, float | int]:
    """The headline report measures, numbers only."""
    result = {}
    for key in MEASURE_KEYS:
        value = (report.get("measures") or {}).get(key)
        if isinstance(value, str):
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = value
    return result


def categories(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in report.get("categories") or []:
        if isinstance(item, dict) and item.get("id"):
            rows.append({"id": str(item["id"]), "name": str(item.get("name") or item["id"]),
                         "measures": measures(item)})
    return rows


def newest(root: Path, paths) -> dict[str, Any] | None:
    """The most recently modified file below ``paths`` (relative to ``root``)."""
    latest = None
    resolved = root.resolve()
    for name in paths:
        try:
            path = contained(root, name)
        except ValueError:
            continue
        candidates = [path] if path.is_file() else (
            Path(folder) / filename for folder, _, files in os.walk(path) for filename in files)
        for candidate in candidates:
            try:
                if candidate.is_symlink() and not candidate.resolve().is_relative_to(resolved):
                    continue
                changed = candidate.stat().st_mtime_ns
            except OSError:
                continue
            if latest is None or changed > latest[0]:
                latest = changed, candidate
    if latest is None:
        return None
    return {"at": datetime.fromtimestamp(latest[0] / 1e9, UTC).isoformat(),
            "path": _relative(latest[1], root, resolved), "mtime_ns": latest[0]}


def _relative(path: Path, root: Path, resolved: Path) -> str:
    """``path`` below ``root``, tolerating symlinked temp dirs and Windows 8.3 short names."""
    for base in (resolved, root):
        for candidate in (path, path.resolve()):
            try:
                return candidate.relative_to(base).as_posix()
            except ValueError:
                pass
    return path.name


_digests: dict[tuple[str, int, int], str] = {}
_digest_lock = threading.Lock()


def _sha1(path: Path) -> tuple[str, os.stat_result] | None:
    """SHA-1 of a file, reused while its size and mtime are unchanged; ``None`` if it changed while read."""
    before = path.stat()
    key = (str(path), before.st_size, before.st_mtime_ns)
    with _digest_lock:
        if key in _digests:
            return _digests[key], before
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha1").hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    with _digest_lock:
        if len(_digests) > 64:
            _digests.clear()
        _digests[key] = digest
    return digest, after


def build_check(root: Path, version: str) -> dict[str, Any]:
    """Compare the main build output with the first entry of ``config/<VERSION>/build.sha1``.

    dtk manifests list ``<sha1> <path>`` per output; the first line is the main binary. Its path is
    used when it stays inside the project, else ``build/<VERSION>/main.dol``.
    """
    try:
        manifest = contained(root, f"config/{version}/build.sha1")
        with manifest.open(encoding="utf-8", errors="replace") as stream:
            first = stream.read(4096).split("\n", 1)[0].split()
        expected = first[0].lower()
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("invalid manifest")
        relative = first[1] if len(first) > 1 else f"build/{version}/main.dol"
        try:
            target = contained(root, relative)
        except ValueError:
            relative = f"build/{version}/main.dol"
            target = contained(root, relative)
        measured = _sha1(target)
        if measured is None:
            return {"status": "changing", "scope": Path(relative).name}
        actual, stat = measured
        return {"status": "exact" if actual == expected else "mismatch", "expected": expected, "actual": actual,
                "built_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(), "scope": Path(relative).name}
    except FileNotFoundError:
        return {"status": "not_built"}
    except (OSError, ValueError, IndexError):
        return {"status": "unknown"}


class HistoryStore:
    """Distinct exact-build report measurements, kept in the dashboard's own cache directory.

    The dashboard never writes into a project checkout, so this replaces the checkout-local
    progress-history the original dashboard maintained. A project's own
    ``build/decomp-support/<VERSION>/progress-history.json`` is still read and merged when present.
    """

    LIMIT = 2000

    def __init__(self, folder: Path | None):
        self.folder = None if folder is None else Path(folder)
        self.lock = threading.Lock()

    def path(self, project: str, version: str) -> Path | None:
        return None if self.folder is None else self.folder / "history" / f"{project}-{version}.json"

    def points(self, project: str, version: str) -> list[dict[str, Any]]:
        path = self.path(project, version)
        value = read_object(path) if path else {}
        points = value.get("points")
        return [row for row in points if isinstance(row, dict)] if isinstance(points, list) else []

    def record(self, project: str, version: str, source: dict[str, Any], values: dict[str, Any],
               build: dict[str, Any]) -> list[dict[str, Any]]:
        """Append a point when an exact build's report measures changed; return the stored points."""
        with self.lock:
            points = self.points(project, version)
            if (source.get("status") != "available" or build.get("status") != "exact"
                    or not values.get("total_code") or not values.get("total_functions")):
                return points
            point = {"at": source["report_at"], **values}
            if points and all(points[-1].get(key) == value for key, value in values.items()):
                return points
            if points and str(points[-1].get("at") or "") > point["at"]:
                return points  # never record an older report after a newer one
            points = [*points, point][-self.LIMIT:]
            path = self.path(project, version)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    temporary.write_text(json.dumps({"schema_version": 1, "points": points}), encoding="utf-8")
                    os.replace(temporary, path)
                except OSError:
                    pass
                finally:
                    temporary.unlink(missing_ok=True)
            return points


def project_history(root: Path, version: str) -> list[dict[str, Any]]:
    """Points from a project's own progress-history artifact, if a tool wrote one (read-only)."""
    value = read_object(root / "build" / "decomp-support" / version / "progress-history.json")
    points = value.get("points")
    return [row for row in points if isinstance(row, dict) and row.get("at")] if isinstance(points, list) else []


def merge_history(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = {}
    for source in sources:
        for row in source:
            at = row.get("at")
            if isinstance(at, str) and at:
                rows[at] = {**rows.get(at, {}), **{key: value for key, value in row.items()
                                                   if key == "at" or key in MEASURE_KEYS}}
    return [rows[key] for key in sorted(rows)]


def stats_delta(points: list[dict[str, Any]], current: dict[str, Any], at: str | None) -> dict[str, Any] | None:
    """Change of each headline measure since the previous distinct recorded measurement."""
    if not current:
        return None
    previous = None
    for row in reversed(points):
        if any(row.get(key) != value for key, value in current.items() if key in row):
            previous = row
            break
    if previous is None:
        return None
    delta = {key: value - previous[key] for key, value in current.items()
             if isinstance(previous.get(key), (int, float)) and not isinstance(previous.get(key), bool)}
    return {"from_at": previous.get("at"), "to_at": at, "measures": delta} if delta else None


# ── Permuter farm ───────────────────────────────────────────────────────────────────────────


def permuter_snapshot(path: Path, project: str | None = None) -> dict[str, Any]:
    """The permuter farm status file, read as-is; never contacts the farm.

    A poller's ``{"kind": "permuter_farm", ...}`` status is served unchanged. The vendored farm's own
    heartbeat (``state/status.json``: ``alive``, ``ts``, ``workers`` …) is wrapped into that shape.
    """
    status = read_json(Path(path))
    if not isinstance(status, dict):
        raise ValueError("permuter farm status must be an object")
    if status.get("kind") == "permuter_farm":
        return {**status, "project": status.get("project") or project}
    if "alive" not in status or "ts" not in status:
        raise ValueError("permuter farm status has an unexpected kind")
    try:
        polled = datetime.fromtimestamp(Path(path).stat().st_mtime, UTC).isoformat()
    except OSError:
        polled = None
    active = status.get("active") if isinstance(status.get("active"), dict) else {}
    units = [{"fn": name, "state": "active", "base_score": (row or {}).get("base"),
              "best_score": (row or {}).get("best"), "elapsed_seconds": (row or {}).get("elapsed")}
             for name, row in sorted(active.items())]
    farm = {key: status.get(key) for key in ("alive", "ts", "pid", "workers", "target_workers", "round",
                                              "units", "wins", "bad")}
    return {"schema_version": 1, "kind": "permuter_farm", "source": "farm_heartbeat", "project": project,
            "host": "local", "reachable": True, "polled_at": polled, "farm": farm,
            "counts": {"active": len(units)}, "units": units, "wins": [],
            "improved_units": sum(isinstance(unit["base_score"], (int, float))
                                  and isinstance(unit["best_score"], (int, float))
                                  and unit["best_score"] < unit["base_score"] for unit in units)}

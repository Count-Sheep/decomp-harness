"""Read-only filesystem timestamps for the live campaign dashboard.

File mtimes describe activity, not successful validation. In particular, a
source edit never changes the canonical objdiff measurements until report.json
is rebuilt. Keep that distinction visible to remote dashboard viewers.
"""

from __future__ import annotations

import os
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


_retail_cache: dict[str, Any] = {"key": None, "value": None}


def _manifest(root: Path, version: str) -> list[tuple[str, str]]:
    """(sha1, root-relative path) rows of the dtk ``config/<VERSION>/build.sha1`` manifest."""
    manifest = root / "config" / version / "build.sha1"
    if not version or not manifest.is_file():
        return []
    rows = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 40:
            continue
        rows.append((parts[0].lower(), parts[1].lstrip("* ").strip()))
    return rows


def _retail_hashes(root: Path, version: str) -> dict[str, Any] | None:
    """Check the build outputs declared in the version's SHA-1 manifest; cache by file metadata."""
    manifest = root / "config" / version / "build.sha1"
    expected = [(digest, root / relative) for digest, relative in _manifest(root, version)]
    if not expected:
        return None
    key = (str(root), manifest.stat().st_mtime_ns,
           tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else (str(path), 0, 0)
                 for _, path in expected))
    if _retail_cache["key"] == key:
        return _retail_cache["value"]
    files = []
    for digest, path in expected:
        if not path.is_file():
            files.append({"path": path.relative_to(root).as_posix(), "status": "missing"})
            continue
        with path.open("rb") as built:
            actual = hashlib.file_digest(built, "sha1").hexdigest()
        files.append({"path": path.relative_to(root).as_posix(), "status": "match" if actual == digest else "mismatch"})
    status = "missing" if any(row["status"] == "missing" for row in files) else (
        "match" if all(row["status"] == "match" for row in files) else "mismatch")
    value = {"status": status, "files": files}
    _retail_cache.update(key=key, value=value)
    return value


def _newest(root: Path, paths: Iterable[str], suffixes: set[str] | None = None) -> dict[str, Any] | None:
    latest: tuple[int, Path] | None = None
    for name in paths:
        path = root / name
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = (
                Path(folder) / filename
                for folder, _, files in os.walk(path)
                for filename in files
            )
        else:
            continue
        for candidate in candidates:
            if suffixes is not None and candidate.suffix.lower() not in suffixes:
                continue
            try:
                changed = candidate.stat().st_mtime_ns
            except OSError:  # a build or editor may replace a file mid-scan
                continue
            if latest is None or changed > latest[0]:
                latest = changed, candidate
    if latest is None:
        return None
    changed, path = latest
    return {
        "at": datetime.fromtimestamp(changed / 1_000_000_000, UTC).isoformat(timespec="seconds"),
        "path": path.relative_to(root).as_posix(),
        "mtime_ns": changed,
    }


def freshness(decomp_root: Path, version: str | None = None) -> dict[str, Any]:
    """Summarize observable edits and build outputs of the decomp without starting a build.

    ``version`` defaults to the selected project's (harness_paths.VERSION); the build outputs are the
    files its ``config/<VERSION>/build.sha1`` manifest declares."""
    if version is None:
        from harness_paths import VERSION as version
    decomp_source = _newest(decomp_root, ("src", "include", "config", "configure.py"))
    decomp_work = _newest(decomp_root, ("src", "include", "config", "docs", "configure.py"))
    decomp_report = _newest(decomp_root, (f"build/{version}/report.json",)) if version else None
    decomp_build = _newest(decomp_root, tuple(relative for _, relative in _manifest(decomp_root, version)))
    records = (("decomp", decomp_work), ("decomp", decomp_build))
    latest = max(((project, row) for project, row in records if row), key=lambda pair: pair[1]["mtime_ns"], default=None)
    latest_work = {"project": latest[0], **latest[1]} if latest else None
    if latest_work:
        latest_work.pop("mtime_ns", None)
    result = {
        "latest_work": latest_work,
        "decomp": {
            "source": decomp_source,
            "work": decomp_work,
            "report": decomp_report,
            "build": decomp_build,
            "retail_hashes": _retail_hashes(decomp_root, version) if version else None,
            "report_behind_source": bool(decomp_source and decomp_report and decomp_source["mtime_ns"] > decomp_report["mtime_ns"]),
        },
    }
    for row in result["decomp"].values():
        if isinstance(row, dict):
            row.pop("mtime_ns", None)
    return result

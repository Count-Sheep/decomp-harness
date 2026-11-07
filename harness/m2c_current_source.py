"""Measure a project's checked-in function as an independent m2c candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import m2c_object_evidence
import local_campaign_decompile_validate as validator


def _number(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return int(str(value), 16 if str(value).lower().startswith("0x") else 10)


class Index:
    """One immutable report/config view shared by a complete validation pass."""

    def __init__(self, root: Path, version: str):
        self.root = root.resolve()
        self.version = version
        self.report_path = self.root / "build" / version / "report.json"
        self.config_path = self.root / "objdiff.json"
        self.available = self.report_path.is_file() and self.config_path.is_file()
        self.report = json.loads(self.report_path.read_text(encoding="utf-8")) if self.available else {}
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.available else {}

    def locate(self, row: dict) -> dict | None:
        return _locate(self, row)


def _locate(index: Index, row: dict) -> dict | None:
    """Locate one source-backed report function and its configured owner object."""
    if not index.available:
        return None
    root, report, config = index.root, index.report, index.config
    expected_address = _number(row.get("address"))
    expected_size = _number(row.get("size"))
    matches = []
    for unit in report.get("units", []):
        metadata = unit.get("metadata") or {}
        source = metadata.get("source_path")
        if not source:
            continue
        for function in unit.get("functions") or []:
            if function.get("name") != row.get("symbol"):
                continue
            address = _number((function.get("metadata") or {}).get("virtual_address"))
            size = _number(function.get("size"))
            if expected_address is not None and address is not None and address != expected_address:
                continue
            if expected_size is not None and size is not None and size != expected_size:
                continue
            matches.append((unit, function, source))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"Ambiguous current source owner: {row.get('symbol')}: {len(matches)}")
    unit, function, source = matches[0]
    owners = [owner for owner in config.get("units", []) if owner.get("name") == unit.get("name")]
    if len(owners) != 1:
        raise ValueError(f"Missing or ambiguous objdiff owner: {unit.get('name')}")
    owner = owners[0]
    if (owner.get("metadata") or {}).get("source_path") != source:
        raise ValueError(f"Report/objdiff source mismatch: {unit.get('name')}")
    paths = {"target": (root / owner["target_path"]).resolve(),
             "base": (root / owner["base_path"]).resolve()}
    paths["source"] = (root / source).resolve()
    if any(not path.is_relative_to(root.resolve()) or not path.is_file() for path in paths.values()):
        raise ValueError(f"Current source owner has missing or external inputs: {unit.get('name')}")
    return {"unit": unit, "function": function, "owner": owner, "paths": paths,
            "report_path": index.report_path.resolve(), "config_path": index.config_path.resolve()}


def locate(root: Path, version: str, row: dict) -> dict | None:
    return Index(root, version).locate(row)


def input_paths(root: Path, version: str, rows: list[dict]) -> set[Path]:
    """Return files whose changes invalidate current-source candidate scores."""
    index = Index(root, version)
    paths = {index.report_path.resolve(), index.config_path.resolve()}
    for row in rows:
        found = index.locate(row)
        if found:
            paths.update(found["paths"].values())
    return paths


def measure(root: Path, version: str, row: dict, index: Index | None = None) -> dict | None:
    """Score the real built owner and retain the editable function source."""
    found = (index or Index(root, version)).locate(row)
    if found is None:
        return None
    source_path = found["paths"]["source"]
    source = source_path.relative_to(root.resolve()).as_posix()
    owner_source, files = validator.snapshot_owner(root, source, row["symbol"])
    text = files[owner_source]
    start, end = validator.campaign.function_span(text, row["symbol"])
    candidate = text[start:end].strip() + "\n"
    audit = m2c_object_evidence.compare(found["paths"]["target"], found["paths"]["base"], row["symbol"])
    if audit.get("status") != "checked":
        raise ValueError(f"Could not compare current source object: {audit.get('error', 'unknown error')}")
    pct = float(found["function"].get("fuzzy_match_percent") or 0)
    count = int((found["function"].get("size") or 0)) // 4
    exact = bool(audit.get("scratch_object_equal"))
    selected = (row.get("compiler_profile") or {}).get("selected") or {}
    profile = str(selected.get("mw_version") or selected.get("compiler") or "project_owner")
    return {"compiled": True, "profile": profile, "pct": pct, "error": "", "diff": "",
            "feedback": {"exact": exact, "target_count": count, "compiled_count": count,
                         **(audit.get("byte_feedback") or {})},
            "object_audit": audit, "candidate": candidate,
            "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
            "source_path": source, "owner_source": owner_source,
            "unit": found["unit"]["name"], "scoring_scope": "project_owner_compiled_object"}

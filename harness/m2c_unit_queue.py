"""Read unit evidence conservatively; changed inputs revoke displayed approval."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from m2c_capture_inputs import Inputs


def replace_file(temporary: Path, destination: Path, attempts: int = 50) -> None:
    """``os.replace`` that tolerates a reader briefly holding ``destination`` open on Windows."""
    for attempt in range(attempts):
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)


def publish(root: Path, version: str, record: Path) -> None:
    if not version.isalnum():
        raise ValueError("Invalid project version")
    record = record.absolute()
    folder = root / "build/decomp-support" / version / "unit-promotions"
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(record).encode()).hexdigest()
    destination = folder / (key + ".json")
    temporary = folder / (key + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"artifact": str(record), "sha256": digest(record)}) + "\n", encoding="utf-8")
    replace_file(temporary, destination)


def snapshot(root: Path, version: str) -> dict:
    rows = []
    for entry in sorted((root / "build/decomp-support" / version / "unit-promotions").glob("*.json")):
        try:
            pointer = json.loads(entry.read_bytes())
            path = Path(pointer["artifact"])
            if digest(path) != pointer["sha256"]:
                raise ValueError("Published evidence record changed")
            rows.append(inspect_record(path, root))
        except (OSError, ValueError, KeyError, TypeError) as error:
            rows.append({"artifact": str(entry), "state": "stale", "error": str(error),
                         "ready_to_merge": False, "link_verified": False, "object_verified": False})
    units = summarize_units(rows)
    return {"records": rows, "units": units, "unit_count": len(units),
            "link_verified": sum(row["link_verified"] for row in units),
            "ready_to_merge": sum(row["ready_to_merge"] for row in units),
            "record_counts": {"link_verified": sum(row["link_verified"] for row in rows),
                              "ready_to_merge": sum(row["ready_to_merge"] for row in rows)}}


def summarize_units(rows: list[dict]) -> list[dict]:
    """Keep history separate from one strongest current certificate per owner."""
    grouped = {}
    for row in rows:
        if row.get("unit"):
            grouped.setdefault(row["unit"], []).append(row)

    def rank(row):
        if row["state"] == "stale":
            return 0
        if row["state"] == "promoted":
            return 9
        if row.get("ready_to_merge"):
            return 8
        if row["state"] == "review_rejected":
            return 7
        if row.get("proposal_build_verified"):
            return 6
        if row["state"] == "proposal_review_required":
            return 5
        if row.get("link_verified"):
            return 4
        if row.get("object_verified"):
            return 3
        return 2 if row.get("compiled") else 1

    units = []
    for name, history in sorted(grouped.items()):
        selected = max(history, key=lambda row: (rank(row), row["artifact"]))
        verified = selected.get("object_verified") is True and selected["state"] != "stale"
        units.append({**selected, "unit": name, "history_count": len(history),
                      "evidence_records": [row["artifact"] for row in history],
                      "milestones": {"compiled": bool(selected.get("compiled") or verified),
                                     "function_exact": True if verified else None,
                                     "unit_complete": True if verified else None,
                                     "object_verified": verified,
                                     "link_verified": selected.get("link_verified") is True,
                                     "proposal_build_verified": selected.get("proposal_build_verified") is True,
                                     "ready_to_merge": selected.get("ready_to_merge") is True}})
    return units


def sweep_snapshot(report: dict, fallback_root: Path, version: str) -> dict:
    workspace = (report.get("latest") or {}).get("workspace")
    root = Path(workspace).resolve() if workspace else fallback_root.resolve()
    return {**snapshot(root, version), "workspace": str(root)}


def digest(path: Path, algorithm: str = "sha256") -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def inspect_record(path: Path, root: Path) -> dict:
    result = {"artifact": str(path), "state": "unverified", "ready_to_merge": False,
              "link_verified": False, "object_verified": False}
    try:
        record = json.loads(path.read_bytes())
        result["unit"] = record["unit"]
        result["record_sha256"] = digest(path)
        if record.get("kind") == "automatic_unit_promotion":
            from m2c_unit_auto_promote import inspect_promotion
            result.update(inspect_promotion(record, root))
            return result
        inputs = Inputs()
        inputs.files = record["inputs"]
        if not inputs.files:
            raise ValueError("Missing bound inputs")
        inputs.verify()
        if record.get("kind") == "unit_review":
            from m2c_unit_review import inspect_review
            result.update(inspect_review(record, path, root))
            return result
        if record.get("kind") == "unit_proposal":
            from m2c_unit_proposal import inspect_proposal
            result.update(inspect_proposal(record, root))
            return result
        if record.get("kind") == "unit_project_trial":
            from m2c_unit_project_trial import inspect_trial
            result.update(inspect_trial(record, root))
            return result
        if "plan_sha256" not in record:
            if Path(record["inventory"]["project_root"]).resolve() != root.resolve():
                raise ValueError("Different project")
            result.update(state=record["state"], compiled=record["compiled"],
                          object_verified=record["object_verified"] is True)
            return result
        if Path(record["project_root"]).resolve() != root.resolve():
            raise ValueError("Different project")
        plan_path = path.parent / "plan.json"
        if digest(plan_path) != record["plan_sha256"]:
            raise ValueError("Link plan changed")
        plan = json.loads(plan_path.read_bytes())
        for item in plan["files"]:
            if digest(root / item["source"]) != item["sha256"]:
                raise ValueError("Link input changed: " + item["source"])
        for name, key in (("build.ninja", "build_ninja_sha256"), ("objdiff.json", "objdiff_sha256"),
                          (plan["checksum_file"], "checksum_file_sha256")):
            if digest(root / name) != plan[key]:
                raise ValueError("Link configuration changed: " + name)
        result["state"] = record["state"]
        if record.get("link_verified") is True:
            for phase in ("baseline", "candidate"):
                evidence = record[phase]
                dol = path.parent / phase / "main.dol"
                if (evidence.get("passed") is not True or evidence["sha1"] != plan["expected_sha1"]
                        or digest(dol, "sha1") != plan["expected_sha1"]
                        or digest(dol) != evidence["dol_sha256"]):
                    raise ValueError("Link output no longer matches: " + phase)
            result.update(state="link_verified", link_verified=True, object_verified=True)
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result.update(state="stale", link_verified=False, object_verified=False, error=str(error))
        return result

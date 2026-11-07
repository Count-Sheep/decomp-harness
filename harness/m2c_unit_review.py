"""Record explicit unit reviews; readiness is recomputed from current evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from m2c_capture_inputs import Inputs


def current_path(root: Path, version: str, unit: str) -> Path:
    if not version.isalnum():
        raise ValueError("Invalid review version")
    key = hashlib.sha256(unit.encode()).hexdigest()
    return root / "build/decomp-support" / version / "unit-reviews" / (key + ".json")


def run(root: Path, trial: Path, output: Path, reviewer: str, decision: str, notes: str) -> dict:
    from m2c_unit_queue import inspect_record, publish, replace_file

    root, trial, output = root.resolve(), trial.absolute(), output.absolute()
    if output.exists():
        raise ValueError("Use a fresh review output directory")
    if decision not in ("approve", "reject") or not reviewer.strip() or not notes.strip():
        raise ValueError("Review requires a decision, reviewer and nonempty notes")
    inputs = Inputs()
    record = json.loads(inputs.read(trial))
    if record.get("kind") != "unit_project_trial":
        raise ValueError("Review requires a native project trial")
    evidence = inspect_record(trial, root)
    if not evidence.get("proposal_build_verified") or not evidence.get("link_verified"):
        raise ValueError("Review requires current native-build and link evidence")
    proposal_path = Path(record["proposal"])
    proposal = json.loads(inputs.read(proposal_path))
    linked = Path(proposal["link_record"])
    plan = json.loads(inputs.read(linked.parent / "plan.json"))
    version = Path(plan["elf"]).parts[1]
    pointer = current_path(root, version, record["unit"])
    result = {"kind": "unit_review", "unit": record["unit"], "version": version,
              "project_root": str(root), "trial": str(trial), "proposal": str(proposal_path),
              "reviewer": reviewer.strip(), "decision": decision, "notes": notes.strip(),
              "reviewed_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs.files,
              "state": "review_recorded", "ready_to_merge": False}
    inputs.verify()
    if not inspect_record(trial, root).get("proposal_build_verified"):
        raise ValueError("Trial changed during review capture")
    output.mkdir(parents=True)
    path = output / "result.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"artifact": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}) + "\n", encoding="utf-8")
    replace_file(temporary, pointer)
    publish(root, version, path)
    return inspect_record(path, root)


def inspect_review(record: dict, path: Path, root: Path) -> dict:
    from m2c_unit_queue import inspect_record

    if Path(record["project_root"]).resolve() != root.resolve():
        raise ValueError("Review belongs to a different project")
    if record["decision"] not in ("approve", "reject") or not record["reviewer"].strip() or not record["notes"].strip():
        raise ValueError("Incomplete review")
    pointer = json.loads(current_path(root, record["version"], record["unit"]).read_bytes())
    if (Path(pointer["artifact"]).absolute() != path.absolute()
            or pointer["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError("Review has been superseded")
    trial_path = Path(record["trial"])
    trial = json.loads(trial_path.read_bytes())
    if trial.get("kind") != "unit_project_trial" or trial["unit"] != record["unit"]:
        raise ValueError("Review references a different trial owner")
    evidence = inspect_record(trial_path, root)
    if not evidence.get("proposal_build_verified") or not evidence.get("link_verified"):
        raise ValueError("Reviewed native build is no longer current")
    approved = record["decision"] == "approve"
    return {"state": "ready_to_merge" if approved else "review_rejected", "ready_to_merge": approved,
            "object_verified": True, "link_verified": True, "proposal_build_verified": True,
            "reviewer": record["reviewer"], "reviewed_at": record["reviewed_at"], "notes": record["notes"],
            "proposal": record["proposal"], "trial": str(trial_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--decision", choices=("approve", "reject"), required=True)
    parser.add_argument("--notes", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.trial, args.output, args.reviewer, args.decision, args.notes)))


if __name__ == "__main__":
    main()

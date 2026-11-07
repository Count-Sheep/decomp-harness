"""Revalidate a whole-unit candidate, then link it in a captured baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import local_campaign_link_gate as gate
from m2c_capture_inputs import Inputs
from m2c_unit_evidence import compare_objects


def candidate_inputs(root: Path, folder: Path) -> tuple[dict, Inputs]:
    from permuter import owner_extract
    record_path = folder / "result.json"
    raw = record_path.read_bytes()
    record = json.loads(raw)
    if not all(record.get(key) is True for key in ("compiled", "object_verified", "inputs_unchanged")):
        raise ValueError("Unit requires successful unchanged whole-object evidence")
    owner = record["inventory"]
    if Path(owner["project_root"]).resolve() != root.resolve() or owner["unit"] != record["unit"]:
        raise ValueError("Unit certificate belongs to a different project or owner")
    inputs = Inputs()
    inputs.files = dict(record["inputs"])
    target = root / gate.relative_path(owner["target_path"])
    required = [target, root / "objdiff.json", folder / "unit.c", folder / "unit.o"]
    if any(str(path.absolute()) not in inputs.files for path in required):
        raise ValueError("Unit certificate omits required inputs")
    inputs.verify()
    if inputs.read(record_path) != raw:
        raise ValueError("Unit certificate changed during revalidation")
    evidence = compare_objects(owner_extract.read_elf(target), owner_extract.read_elf(folder / "unit.o"))
    if not evidence["object_verified"]:
        raise ValueError("Whole-object revalidation failed")
    inputs.verify()
    return record, inputs


def run(root: Path, compiled: Path, output: Path, target: str, checksum_file: str,
        mode: str = "retail-reference") -> dict:
    root, compiled, output = root.resolve(), compiled.absolute(), output.resolve()
    record, inputs = candidate_inputs(root, compiled)
    plan = gate.capture_plan(root, output, target, checksum_file, mode)
    workspace = output / "workspace"
    result = {"unit": record["unit"], "state": "object_verified", "link_verified": False,
              "ready_to_merge": False, "context": mode, "compiled": str(compiled),
              "project_root": str(root)}
    try:
        unit = plan["units"].get(record["unit"], {})
        if Path(unit.get("target_path") or "") != Path(record["inventory"]["target_path"]):
            raise ValueError("Link graph owner differs from compiled owner")
        slots = [slot for slot in plan["objects"] if slot in (unit.get("target_path"), unit.get("base_path"))]
        if len(slots) != 1:
            raise ValueError("Unit must own exactly one input in the link graph")
        inputs.verify()
        baseline = gate.link_once(workspace, plan, output / "baseline")
        result["baseline"] = baseline
        if not baseline["passed"]:
            result["state"] = "baseline_failed"
        else:
            candidate = compiled / "unit.o"
            destination = workspace / slots[0]
            original = destination.read_bytes()
            try:
                inputs.verify()
                shutil.copyfile(candidate, destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != inputs.files[str(candidate)]["sha256"]:
                    raise ValueError("Candidate changed during link injection")
                result["candidate"] = gate.link_once(workspace, plan, output / "candidate")
            finally:
                destination.write_bytes(original)
            result["link_verified"] = result["candidate"]["passed"]
            result["state"] = "link_verified" if result["link_verified"] else "link_failed"
        inputs.verify()
        for item in plan["files"]:
            for path in (root / item["source"], workspace / item["slot"]):
                if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Link baseline input changed: " + str(path))
        inputs.read(root / checksum_file)
        if hashlib.sha256((root / checksum_file).read_bytes()).hexdigest() != plan["checksum_file_sha256"]:
            raise ValueError("Expected checksum changed")
        if hashlib.sha256((root / "build.ninja").read_bytes()).hexdigest() != plan["build_ninja_sha256"]:
            raise ValueError("Build graph changed")
    except (OSError, ValueError, RuntimeError) as error:
        result.update(state="invalid", link_verified=False, error=str(error))
    result["inputs"] = inputs.files
    result["plan_sha256"] = hashlib.sha256((output / "plan.json").read_bytes()).hexdigest()
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    from m2c_unit_queue import publish
    publish(root, Path(plan["elf"]).parts[1], output / "result.json")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--compiled", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--checksum-file", required=True)
    parser.add_argument("--context", choices=("native", "retail-reference"), default="retail-reference")
    args = parser.parse_args()
    result = run(args.root, args.compiled, args.output, args.target, args.checksum_file, args.context)
    print(json.dumps({key: result[key] for key in ("unit", "state", "link_verified", "ready_to_merge")}))


if __name__ == "__main__":
    main()

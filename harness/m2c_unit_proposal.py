"""Package verified unit evidence as reviewable changes, without applying them."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import subprocess

from m2c_capture_inputs import Inputs
from m2c_unit_link import candidate_inputs


def head(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=10).strip()


def destination(root: Path, name: str) -> Path:
    relative = Path(name)
    if (relative.is_absolute() or not relative.parts or any(part in ("..", ".git") for part in relative.parts)
            or any(char in name for char in "\n\r\t")):
        raise ValueError("Invalid proposal destination")
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("Proposal destination escapes project or is a symlink")
    return path


def run(root: Path, linked: Path, output: Path, source_path: str | None = None,
        configuration: dict[str, Path] | None = None) -> dict:
    from m2c_unit_queue import inspect_record, publish

    root, linked, output = root.resolve(), linked.absolute(), output.absolute()
    if output.exists():
        raise ValueError("Use a fresh proposal output")
    link_record = json.loads(linked.read_bytes())
    if "plan_sha256" not in link_record or not inspect_record(linked, root)["link_verified"]:
        raise ValueError("Proposal requires current verified link evidence")
    compiled = Path(link_record["compiled"])
    compile_record, inputs = candidate_inputs(root, compiled)
    if compile_record["unit"] != link_record["unit"]:
        raise ValueError("Compile/link owner mismatch")
    inputs.read(linked)
    plan = json.loads(inputs.read(linked.parent / "plan.json"))
    source_path = source_path or compile_record["inventory"]["configuration"].get("metadata", {}).get("source_path")
    if not source_path:
        raise ValueError("Proposal requires an explicit project source destination")
    configuration = configuration or {}
    if source_path in configuration:
        raise ValueError("Source destination duplicated by configuration change")
    revision = head(root)
    changes, absent, patches = [], {}, []
    payloads = [("source", source_path, compiled / "unit.c")]
    payloads.extend(("configuration", name, path) for name, path in configuration.items())
    # Resolve all targets and capture their current contents before writing a bundle.
    captured = []
    targets = set()
    for kind, name, payload in payloads:
        target = destination(root, name)
        if target.resolve() in targets:
            raise ValueError("Duplicate proposal destination")
        targets.add(target.resolve())
        new = inputs.read(payload)
        if target.exists():
            old = inputs.read(target)
        else:
            old = b""
            absent[str(target)] = str(target.resolve())
        patch_lines = difflib.unified_diff(old.decode().splitlines(keepends=True),
                                          new.decode().splitlines(keepends=True),
                                          fromfile="a/" + name if target.exists() else "/dev/null",
                                          tofile="b/" + name)
        # Git patches need explicit no-newline markers for unterminated lines.
        patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                        for line in patch_lines)
        captured.append((kind, name, new, patch))
    inputs.verify()
    output.mkdir(parents=True)
    for index, (kind, name, raw, patch) in enumerate(captured):
        payload = output / f"change-{index}.txt"
        payload.write_bytes(raw)
        inputs.read(payload)
        changes.append({"kind": kind, "destination": name, "payload": str(payload), "changed": bool(patch)})
        patches.append(patch)
    patch_path = output / "changes.patch"
    patch_path.write_text("".join(patches), encoding="utf-8", newline="\n")
    inputs.read(patch_path)
    if head(root) != revision or any(Path(path).exists() or Path(path).is_symlink()
                                   or str(Path(path).resolve()) != resolved for path, resolved in absent.items()):
        raise ValueError("Project changed during proposal capture")
    inputs.verify()
    if not inspect_record(linked, root)["link_verified"]:
        raise ValueError("Link evidence changed during proposal capture")
    result = {"kind": "unit_proposal", "unit": link_record["unit"], "project_root": str(root),
              "base_commit": revision, "state": "proposal_review_required", "link_record": str(linked),
              "changes": changes, "patch": str(patch_path), "absent_paths": absent, "inputs": inputs.files,
              "ready_to_merge": False, "proposal_build_verified": False,
              "required_checks": ["Review source and configuration changes", "Build proposed project changes",
                                  "Require expected linked checksum on current branch"]}
    record = output / "result.json"
    record.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    publish(root, Path(plan["elf"]).parts[1], record)
    return result


def inspect_proposal(record: dict, root: Path) -> dict:
    from m2c_unit_queue import inspect_record

    if Path(record["project_root"]).resolve() != root.resolve() or head(root) != record["base_commit"]:
        raise ValueError("Proposal project or base commit changed")
    if not isinstance(record["absent_paths"], dict):
        raise ValueError("Proposal lacks resolved-path absence bindings")
    if any(Path(path).exists() or Path(path).is_symlink() or str(Path(path).resolve()) != resolved
           for path, resolved in record["absent_paths"].items()):
        raise ValueError("Previously absent proposal destination appeared")
    linked = Path(record["link_record"])
    parent = json.loads(linked.read_bytes())
    if "plan_sha256" not in parent or parent["unit"] != record["unit"]:
        raise ValueError("Proposal does not reference same-owner link evidence")
    evidence = inspect_record(linked, root)
    if not evidence["link_verified"]:
        raise ValueError("Proposal link evidence is stale")
    return {"state": "proposal_review_required", "link_verified": True, "object_verified": True,
            "ready_to_merge": False, "proposal_build_verified": False,
            "changes": record["changes"], "patch": record["patch"], "base_commit": record["base_commit"],
            "required_checks": record["required_checks"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--link-record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-path")
    parser.add_argument("--config-change", action="append", default=[], metavar="TARGET=FILE")
    args = parser.parse_args()
    changes = {}
    for value in args.config_change:
        name, separator, path = value.partition("=")
        if not separator or not path or name in changes:
            parser.error("Configuration changes require unique TARGET=FILE values")
        changes[name] = Path(path)
    result = run(args.root, args.link_record, args.output, args.source_path, changes)
    print(json.dumps({key: result[key] for key in ("unit", "state", "patch", "ready_to_merge")}))


if __name__ == "__main__":
    main()

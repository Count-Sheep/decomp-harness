"""Compile a self-contained unit hypothesis with captured owner settings."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import harness_paths
from m2c_draft_attempts import compiler_choices
from m2c_capture_inputs import Inputs, compiler_profile, unit_name
from m2c_unit_evidence import compare_objects
from m2c_unit_inventory import capture


def compiler_launcher(root: Path) -> list[str]:
    """The compiler wrapper for this host as absolute paths ([] where MWCC runs natively)."""
    resolved = []
    for part in harness_paths.compiler_wrapper(root):
        path = Path(part)
        if not path.is_absolute() and len(path.parts) == 1 and not (root / path).exists():
            found = shutil.which(part)
            path = Path(found) if found else root / path
        elif not path.is_absolute():
            path = root / path
        resolved.append(str(path))
    return resolved


def run(root: Path, unit: str, source: Path, output: Path, *, provenance: Inputs | None = None,
        native_context: Path | None = None) -> dict:
    from permuter import owner_extract
    root, source, output = root.resolve(), source.absolute(), output.absolute()
    if output.exists():
        raise ValueError("Unit compile output already exists")
    inputs = Inputs()
    if provenance is not None:
        inputs.files = dict(provenance.files)
        inputs.verify()
    objdiff = json.loads(inputs.read(root / "objdiff.json"))
    owner = capture(root, unit)
    inputs.read(root / "objdiff.json")
    target = root / owner["target_path"]
    if hashlib.sha256(inputs.read(target)).hexdigest() != owner["target_sha256"]:
        raise ValueError("Target changed during inventory capture")
    profile = compiler_profile(root, unit_name(unit), objdiff, inputs)
    choices = compiler_choices(root, {"symbol": unit, "compiler_profile": profile})
    if len(choices) != 1:
        raise ValueError("Unit compilation requires one recorded compiler")
    _, flags, selected = choices[0]
    # The isolated unit is self-contained; external include graphs need a separate capture.
    raw = inputs.read(source)
    import local_campaign_decompile as decompile
    import sys
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_unit_source import validate_directives
    captured_pragmas = set()
    if native_context is not None:
        record_path = native_context / "result.json"
        context_path = native_context / "source.c"
        if provenance is None or any(str(path.absolute()) not in provenance.files
                                     for path in (record_path, context_path)):
            raise ValueError("Native context must be captured in compile provenance")
        context_record = json.loads(inputs.read(record_path))
        if context_record.get("state") != "captured" or context_record.get("unit") != unit:
            raise ValueError("Invalid native preprocessing context")
        captured_pragmas = {line.strip() for line in
                            c_types.strip_comments(inputs.read(context_path).decode()).splitlines()
                            if line.lstrip().startswith("#pragma ")}
    validate_directives(c_types.strip_comments(raw.decode()), captured_pragmas)
    if any(flag.startswith("@") or flag in ("-o", "-c", "-include", "-prefix", "-precompile") for flag in flags):
        raise ValueError("Recorded flags contain uncaptured inputs or output controls")
    # The host's compiler wrapper (none on Windows) is bound like any other input.
    wrapper = compiler_launcher(root)
    for part in wrapper:
        inputs.read(Path(part))
    output.mkdir(parents=True)
    candidate_source = output / "unit.c"
    candidate_source.write_bytes(raw)
    inputs.read(candidate_source)
    candidate = output / "unit.o"
    command = [*wrapper, selected["binary"], *flags, "-c", str(candidate_source), "-o", str(candidate)]
    result = {"unit": unit, "state": "draft", "compiled": False,
              "object_verified": False, "link_verified": False,
              "compiler_profile": profile, "command": command, "inventory": owner}
    try:
        process = subprocess.run(command, cwd=output, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=90)
        (output / "compile.log").write_text(process.stdout + process.stderr, encoding="utf-8")
        result["returncode"] = process.returncode
        result["compiled"] = process.returncode == 0 and candidate.is_file()
        if not result["compiled"]:
            result["state"] = "compile_failed"
        if result["compiled"]:
            inputs.read(candidate)
            evidence = compare_objects(owner_extract.read_elf(target), owner_extract.read_elf(candidate))
            result["object_evidence"] = evidence
            result["object_verified"] = evidence["object_verified"]
            if result["object_verified"]:
                result["state"] = "object_verified"
        inputs.verify()
        result["inputs_unchanged"] = True
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        result.update(state="invalid", object_verified=False, inputs_unchanged=False, error=str(error))
    result["inputs"] = inputs.files
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    from m2c_unit_queue import publish
    publish(root, Path(owner["target_path"]).parts[1], output / "result.json")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.root, args.unit, args.source, args.output)
    print(json.dumps({key: result[key] for key in ("unit", "state", "compiled", "object_verified", "link_verified")}))


if __name__ == "__main__":
    main()

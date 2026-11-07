"""Inventory draft-covered owner units and run bounded isolated unit attempts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from m2c_capture_inputs import Inputs
from m2c_unit_inventory import inventory
from m2c_unit_pipeline import run as attempt
from m2c_unit_source import source_drafts


def index_drafts(batches: list[Path], inputs: Inputs) -> dict:
    indexed = {}
    for batch in batches:
        data = json.loads(inputs.read(batch))
        if data.get("summary", {}).get("finished") is not True:
            raise ValueError("Candidate selection requires a completed batch")
        for row in data["results"]:
            if row.get("status") != "ok" or not row.get("artifacts"):
                continue
            unit = row.get("unit", "")
            if row.get("module", "main") == "main" and not unit.startswith("main/"):
                unit = "main/" + unit
            path = Path(row["artifacts"]) / "draft.c"
            if not path.is_file():
                continue
            source = inputs.read(path)
            context = path.parent / "context.c"
            declarations = inputs.read(context) if context.is_file() else b""
            identity = (hashlib.sha256(source).hexdigest(), hashlib.sha256(declarations).hexdigest())
            indexed.setdefault(unit, {}).setdefault(row["symbol"], set()).add(identity)
    return indexed


def discover_source(root: Path, unit: dict, owner: dict, inputs: Inputs,
                    preprocess_output: Path | None = None) -> dict:
    recorded = unit.get("metadata", {}).get("source_path")
    if not recorded:
        return {}
    path = (root / recorded).absolute()
    if not path.resolve().is_relative_to(root.resolve()) or path.suffix != ".c":
        raise ValueError("Recorded unit source must be project-local C")
    source = inputs.read(path).decode()
    if preprocess_output is not None:
        from m2c_unit_preprocess import run as preprocess
        local_inputs = Inputs()
        try:
            source = preprocess(root, unit["name"], path, preprocess_output, local_inputs)
        finally:
            # Per-unit records must not absorb earlier units' evidence. The sweep
            # still binds their union and rejects inconsistent shared inputs.
            for name, evidence in local_inputs.files.items():
                if name in inputs.files and inputs.files[name] != evidence:
                    raise ValueError("Input changed between source captures: " + name)
                inputs.files[name] = evidence
    imported = source_drafts(source)
    names = {draft["symbol"] for draft in imported}
    required = {symbol["name"] for sec in owner["sections"] for symbol in sec["definitions"]
                if symbol["type"] == 2}
    helpers = {draft["symbol"] for draft in imported if draft["static_inline"] and draft["symbol"] not in required}
    if names - required - helpers:
        raise ValueError("Recorded source defines functions outside target owner: " + ", ".join(sorted(names - required - helpers)))
    return {"source_path": str(path), "source_functions": sorted(names & required),
            "source_inline_helpers": sorted(helpers), "source_preprocessed": preprocess_output is not None}


def assess(owner: dict, drafts: dict, source_functions: list[str] | None = None) -> dict:
    functions = [symbol for section in owner["sections"] for symbol in section["definitions"]
                 if symbol["type"] == 2]
    missing = [symbol["name"] for symbol in functions if not drafts.get(symbol["name"])]
    ambiguous = [symbol["name"] for symbol in functions if len(drafts.get(symbol["name"], [])) > 1]
    supplied = set(source_functions or [])
    missing_implementations = [name for name in missing if name not in supplied]
    ambiguous_implementations = [name for name in ambiguous if name not in supplied]
    return {"functions": len(functions), "missing_functions": missing, "ambiguous_functions": ambiguous,
            "draft_covered": bool(functions) and not missing and not ambiguous,
            "implementation_covered": bool(functions) and not missing_implementations and not ambiguous_implementations,
            "missing_implementations": missing_implementations,
            "ambiguous_implementations": ambiguous_implementations,
            "unit_complete": False, "code_bytes": sum(s["size"] for s in functions),
            "data_definitions": sum(s["type"] != 2 for sec in owner["sections"] for s in sec["definitions"]),
            "unnamed_ranges": sum(len(sec["unnamed_ranges"]) for sec in owner["sections"]),
            "relocations": sum(len(sec["relocations"]) for sec in owner["sections"])}


def run(root: Path, batches: list[Path], output: Path, attempts: int = 0, link: bool = False,
        regenerate: bool = False, existing_sources: bool = False, preprocess_sources: bool = False,
        propose: bool = False, verify_project: bool = False, link_context: str = "native") -> dict:
    from permuter import owner_extract
    root, output = root.resolve(), output.absolute()
    if output.exists() or attempts < 0:
        raise ValueError("Use a fresh output and a nonnegative attempt count")
    inputs = Inputs()
    config = json.loads(inputs.read(root / "objdiff.json"))
    indexed = index_drafts(batches, inputs)
    names = [unit["name"] for unit in config["units"]]
    if len(names) != len(set(names)):
        raise ValueError("Ambiguous owner names in objdiff configuration")
    output.mkdir(parents=True)
    rows = []
    for unit in config["units"]:
        if unit.get("metadata", {}).get("complete") is True:
            continue
        row = {"unit": unit["name"], "draft_covered": False, "implementation_covered": False, "unit_complete": False}
        try:
            target = (root / unit["target_path"]).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Owner escapes project root")
            raw = inputs.read(target)
            owner = inventory(owner_extract.read_elf(target))
            if owner["target_sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("Owner changed during inventory")
            if existing_sources or preprocess_sources:
                try:
                    key = hashlib.sha256(unit["name"].encode()).hexdigest()[:16]
                    row.update(discover_source(root, unit, owner, inputs,
                        output / "source-discovery" / key if preprocess_sources else None))
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    row["source_error"] = str(error)
            row.update(assess(owner, indexed.get(unit["name"], {}), row.get("source_functions")),
                       target_sha256=owner["target_sha256"])
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            row["error"] = str(error)
        rows.append(row)
    inputs.verify()
    eligible = sorted((row for row in rows if row["implementation_covered"] and not row.get("error")),
                      key=lambda row: (row["data_definitions"], row["code_bytes"], row["unit"]))
    result = {"scope": "implementation_coverage_not_unit_completeness", "project_root": str(root),
              "units": rows, "eligible": len(eligible), "attempts": [], "inputs": inputs.files}
    record = output / "selection.json"
    record.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for row in eligible[:attempts]:
        inputs.verify()
        key = hashlib.sha256(row["unit"].encode()).hexdigest()[:16]
        measured = attempt(root, row["unit"], batches, output / "units" / key, link=link, regenerate=regenerate,
                           unit_source=Path(row["source_path"]) if row.get("source_path") else None,
                           preprocess_source=row.get("source_preprocessed", False),
                           propose=propose, verify_project=verify_project, link_context=link_context)
        result["attempts"].append({"unit": row["unit"], "state": measured["state"],
                                   "compiled": measured["compiled"], "object_verified": measured["object_verified"],
                                   "error": measured.get("error"), "artifact": str(output / "units" / key)})
        record.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch-results", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=0)
    parser.add_argument("--link", action="store_true")
    parser.add_argument("--link-context", choices=("native", "retail-reference"), default="native")
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--existing-sources", action="store_true", help="include recorded self-contained C sources")
    parser.add_argument("--preprocess-sources", action="store_true", help="capture native preprocessing during source discovery")
    parser.add_argument("--propose", action="store_true", help="package proposals for successfully linked units")
    parser.add_argument("--verify-project", action="store_true", help="also run isolated native proposal builds")
    args = parser.parse_args()
    result = run(args.root, args.batch_results, args.output, args.attempts, args.link, args.regenerate,
                 args.existing_sources, args.preprocess_sources, args.propose, args.verify_project, args.link_context)
    print(json.dumps({"units": len(result["units"]), "draft_covered": sum(r["draft_covered"] for r in result["units"]),
                      "implementation_covered": result["eligible"],
                      "attempts": result["attempts"]}))


if __name__ == "__main__":
    main()

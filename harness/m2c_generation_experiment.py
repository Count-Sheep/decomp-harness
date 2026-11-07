"""Compare raw m2c generation, with no draft repair, on immutable scratch inputs."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import m2c_draft_attempts as attempts
from m2c_failure_audit import assembly_evidence, compiler_messages, digest, failure_categories, summarize
from m2c_repair_experiment import validate_owners
import m2c_readonly_data
import m2c_capture_inputs


def tool_fingerprint(root: Path) -> str:
    files = [root / "m2c.py", *sorted((root / "m2c").rglob("*.py")),
             *sorted((root / "m2c_pycparser").rglob("*.py"))]
    return digest(json.dumps([(path.relative_to(root).as_posix(), digest(path.read_bytes()))
                              for path in files]).encode())


def generate(tool: Path, row: dict, folder: Path, *, context_decls: bool,
             infer_unknowns: bool = False, deterministic_vars: bool = False,
             mwcc_fused_subtract: bool = False, reg_vars: tuple[str, ...] = (),
             ppc_stack_storage: bool = False, heuristic_strings: bool = False) -> dict:
    if any(not re.fullmatch(r"[rf](?:[0-9]|[12][0-9]|3[01])", reg) for reg in reg_vars):
        raise ValueError("Invalid PPC register variable")
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "target.s").write_text(row["assembly"], encoding="utf-8", newline="\n")
    (folder / "context.c").write_text(row["context"], encoding="utf-8", newline="\n")
    command = [sys.executable, str(tool / "m2c.py"), "-t", "ppc-mwcc-c", "--no-cache",
               "--valid-syntax", "--explicit-field-access",
               "--context", str(folder / "context.c"), "-f", row["symbol"], str(folder / "target.s")]
    if context_decls:
        command.extend(["--globals", "used-context"])
    if not infer_unknowns:
        command.append("--no-unk-inference")
    if deterministic_vars:
        command.append("--deterministic-vars")
    if mwcc_fused_subtract:
        command.append("--mwcc-fused-subtract")
    if ppc_stack_storage:
        command.append("--ppc-stack-storage")
    if heuristic_strings:
        command.append("--heuristic-strings")
    if reg_vars:
        command.extend(["--reg-vars", ",".join(reg_vars)])
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=45, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        generated = {"command": command, "returncode": result.returncode,
                     "draft": result.stdout, "error": result.stderr}
    except subprocess.TimeoutExpired:
        generated = {"command": command, "returncode": -1, "draft": "", "error": "generation timeout"}
    (folder / "draft.c").write_text(generated["draft"], encoding="utf-8", newline="\n")
    attempts.write_json(folder / "generation.json", {key: value for key, value in generated.items() if key != "draft"})
    return generated


def measure(root: Path, version: str, row: dict, generated: dict, folder: Path) -> dict:
    target = {**row, "assembly": str(folder / "target.s"), "artifacts": str(folder)}
    if generated["returncode"] == 0 and generated["draft"].strip():
        outcome = attempts.score(root, version, target, generated["draft"], folder / "score", apply_repair=False)
    else:
        outcome = {"compiled": False, "pct": 0.0, "error": generated["error"] or "generation failed"}
    profile = outcome.get("profile_artifact") or outcome.get("profile")
    disasm = folder / "score" / f"{profile}.s"
    obj = folder / "score" / f"{profile}.o"
    compiled = disasm.read_text(encoding="utf-8") if disasm.is_file() else None
    return {"symbol": row["symbol"], "size": row["size"], "outcome": outcome,
            "compiled_object_sha256": digest(obj.read_bytes()) if obj.is_file() else None,
            "compiler_messages": compiler_messages(outcome.get("error") or ""),
            "categories": failure_categories(generated["draft"], outcome,
                                             "ok" if generated["returncode"] == 0 else "decompile_error"),
            "generation_returncode": generated["returncode"],
            "evidence": assembly_evidence(row["assembly"], compiled, outcome)}


def run(snapshot: Path, baseline: Path, candidate: Path, output: Path,
        categories: list[str], limit: int, controls: int, *, infer_unknowns: bool = False,
        deterministic_vars: bool = False, baseline_context_declarations: bool = False,
        readonly_data: bool = False, baseline_readonly_data: bool = False,
        baseline_original_context: bool = False, candidate_mwcc_fused_subtract: bool = False,
        candidate_ppc_stack_storage: bool = False, candidate_heuristic_strings: bool = False,
        symbols: list[str] | None = None) -> dict:
    raw = snapshot.read_bytes()
    frozen = json.loads(raw)
    root = Path(frozen["root"])
    report_path = root / "build" / frozen["version"] / "report.json"
    report = report_path.read_bytes()
    if symbols and set(symbols) - {row["symbol"] for row in frozen["rows"]}:
        raise ValueError("Requested symbol is not present in frozen inputs")
    selected = [row for row in frozen["rows"] if not categories or set(categories) & set(row["categories"])]
    if symbols:
        selected = [row for row in selected if row["symbol"] in set(symbols)]
    selected = selected[:limit] if limit else selected
    names = {row["symbol"] for row in selected}
    selected += [row for row in frozen["rows"] if row["evidence"].get("instruction_exact") is True
                 and row["symbol"] not in names][:controls]
    captured = frozen.get("capture_kind") == "native_m2c_batch_inputs"
    if baseline_original_context and (not captured or any("original_context" not in row for row in selected)):
        raise ValueError("Original-context comparison requires captured original headers")
    if captured:
        m2c_capture_inputs.verify_snapshot(frozen)
        for row in selected:
            if not (row.get("compiler_profile") or {}).get("strict_selection_required"):
                raise ValueError("Captured inputs require strict compiler selection")
            attempts.compiler_choices(root, row)
    validate_owners(json.loads(report), selected)
    tools = {"baseline": tool_fingerprint(baseline), "candidate": tool_fingerprint(candidate)}

    def verify_inputs() -> None:
        if captured:
            m2c_capture_inputs.verify_snapshot(frozen)
        for path, expected in frozen["target_objects"].items():
            if digest(Path(path).read_bytes()) != expected:
                raise ValueError(f"Frozen target changed: {path}")
        if report_path.read_bytes() != report:
            raise ValueError("Target report changed")
        if tools != {"baseline": tool_fingerprint(baseline), "candidate": tool_fingerprint(candidate)}:
            raise ValueError("m2c source changed during experiment")

    verify_inputs()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"started_at": datetime.now(UTC).isoformat(), "snapshot_sha256": digest(raw),
                "report_sha256": digest(report), "tools": tools, "categories": categories,
                "symbols": [row["symbol"] for row in selected], "repair_enabled": False,
                "infer_unknowns": infer_unknowns, "deterministic_vars": deterministic_vars,
                "baseline_context_declarations": baseline_context_declarations,
                "readonly_data": readonly_data,
                "baseline_readonly_data": baseline_readonly_data,
                "baseline_original_context": baseline_original_context,
                "candidate_mwcc_fused_subtract": candidate_mwcc_fused_subtract,
                "candidate_ppc_stack_storage": candidate_ppc_stack_storage,
                "candidate_heuristic_strings": candidate_heuristic_strings,
                "capture_hashes_verified": captured,
                "strict_compiler_selection": captured,
                "readonly_capture_sha256": digest(Path(m2c_readonly_data.__file__).read_bytes()),
                "baseline_root": str(baseline), "candidate_root": str(candidate),
                "experiment_sha256": digest(Path(__file__).read_bytes()),
                "scorer_sha256": digest(Path(attempts.__file__).read_bytes())}
    attempts.write_json(output / "manifest.json", manifest)
    before, after, results = [], [], []
    summary = {}
    for index, row in enumerate(selected):
        if captured:
            verify_inputs()
        pair = {}
        for kind, tool in (("baseline", baseline), ("candidate", candidate)):
            folder = output / f"{index:05d}" / kind
            inputs, capture_info = row, None
            if baseline_original_context and kind == "baseline":
                inputs = {**row, "context": row["original_context"]}
            if (readonly_data and kind == "candidate") or (baseline_readonly_data and kind == "baseline"):
                assembly, capture_info = m2c_readonly_data.capture(
                    attempts.target_object(root, frozen["version"], row), row["symbol"], row["assembly"].encode())
                inputs = {**inputs, "assembly": assembly.decode()}
            generated = generate(tool, inputs, folder,
                                 context_decls=kind == "candidate" or baseline_context_declarations,
                                 infer_unknowns=infer_unknowns, deterministic_vars=deterministic_vars,
                                 mwcc_fused_subtract=kind == "candidate" and candidate_mwcc_fused_subtract,
                                 ppc_stack_storage=kind == "candidate" and candidate_ppc_stack_storage,
                                 heuristic_strings=kind == "candidate" and candidate_heuristic_strings)
            if capture_info is not None:
                attempts.write_json(folder / "readonly-data.json", capture_info)
            pair[kind] = measure(root, frozen["version"], row, generated, folder)
        before.append(pair["baseline"])
        after.append(pair["candidate"])
        results.append({"symbol": row["symbol"], **pair})
        summary = {"processed": len(results), "total": len(selected),
                   "baseline": summarize(before), "candidate": summarize(after),
                   "compile_gains": sum(not old["outcome"].get("compiled") and new["outcome"].get("compiled", False)
                                        for old, new in zip(before, after)),
                   "compile_losses": [old["symbol"] for old, new in zip(before, after)
                                      if old["outcome"].get("compiled") and not new["outcome"].get("compiled")],
                   "exact_losses": [old["symbol"] for old, new in zip(before, after)
                                    if old["evidence"].get("instruction_exact") is True
                                    and new["evidence"].get("instruction_exact") is not True],
                   "frozen_exact_not_reproduced": [old["symbol"] for old, new in zip(selected, after)
                                                  if old["evidence"].get("instruction_exact") is True
                                                  and new["evidence"].get("instruction_exact") is not True]}
        attempts.write_json(output / "results.json", {"summary": summary, "results": results})
        print(f"{index + 1}/{len(selected)} {row['symbol']}: "
              f"{pair['baseline']['outcome']['pct']:.3f} -> {pair['candidate']['outcome']['pct']:.3f}", flush=True)
    verify_inputs()
    manifest["finished_at"] = datetime.now(UTC).isoformat()
    attempts.write_json(output / "manifest.json", manifest)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--exact-controls", type=int, default=30)
    parser.add_argument("--infer-unknowns", action="store_true")
    parser.add_argument("--deterministic-vars", action="store_true")
    parser.add_argument("--baseline-context-declarations", action="store_true")
    parser.add_argument("--readonly-data", action="store_true", help="supply candidate m2c with target-owner readonly bytes")
    parser.add_argument("--baseline-readonly-data", action="store_true", help="also supply baseline m2c with target-owner readonly bytes")
    parser.add_argument("--baseline-original-context", action="store_true")
    parser.add_argument("--candidate-mwcc-fused-subtract", action="store_true")
    parser.add_argument("--candidate-ppc-stack-storage", action="store_true")
    parser.add_argument("--candidate-heuristic-strings", action="store_true")
    args = parser.parse_args()
    if args.limit < 0 or args.exact_controls < 0:
        parser.error("limits must be nonnegative")
    print(json.dumps(run(args.snapshot, args.baseline.resolve(), args.candidate.resolve(), args.output.resolve(),
                         args.category, args.limit, args.exact_controls, infer_unknowns=args.infer_unknowns,
                         deterministic_vars=args.deterministic_vars,
                         baseline_context_declarations=args.baseline_context_declarations,
                         readonly_data=args.readonly_data, baseline_readonly_data=args.baseline_readonly_data,
                         baseline_original_context=args.baseline_original_context,
                         candidate_mwcc_fused_subtract=args.candidate_mwcc_fused_subtract,
                         candidate_ppc_stack_storage=args.candidate_ppc_stack_storage,
                         candidate_heuristic_strings=args.candidate_heuristic_strings,
                         symbols=args.symbol), indent=2))


if __name__ == "__main__":
    main()

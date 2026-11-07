"""Serial MWCC/objdiff diagnostics for every m2c draft; never promotes source."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import local_campaign_decompile as decompile
import local_campaign as campaign
from local_campaign_decompile_batch import command_map
from harness_paths import DECOMP_ROOT, VERSION, build_tool
import local_campaign_m2c_report as m2c_report

# This is byte-offset access, not a guessed struct or a placeholder operation.
FIELD_MACRO = '#define M2C_FIELD(expr, type_ptr, offset) (*(type_ptr)((char *)(expr) + (offset)))\n'
FIELD_FALLBACK = '#ifndef M2C_FIELD\n' + FIELD_MACRO + '#endif\n'
IMPLEMENTATION = Path(__file__)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def environment_fingerprint(root: Path, commands: dict[str, str], version: str | None = None) -> str:
    paths = set()
    for directory in (root / "src", root / "include", root / "build" / (version or VERSION) / "include"):
        paths.update(path for path in directory.rglob("*") if path.is_file())
    for command in commands.values():
        for argument in decompile.split_command(command.split(" && ", 1)[0]):
            path = root / argument
            if path.is_file():
                paths.add(path)
    result = hashlib.sha256()
    for path in sorted(paths):
        result.update(str(path).encode())
        result.update(path.read_bytes())
    return result.hexdigest()


def compile_args(command: str, source: Path, directory: Path) -> tuple[list[str], Path]:
    args = [arg for arg in decompile.split_command(command.split(" && ", 1)[0]) if arg != "-MMD"]
    if "-c" not in args or "-o" not in args:
        raise ValueError("Unit command has no compile input/output")
    args[args.index("-c") + 1] = str(source)
    args[args.index("-o") + 1] = str(directory)
    # MWCC searches -i directories before a quoted include's source directory.
    index = args.index("-i") if "-i" in args else args.index("-c")
    args[index:index] = ["-i", str(source.parent)]
    return args, directory / source.with_suffix(".o").name


def run_compile(command: str, source: Path, directory: Path, include_dirs: list[Path],
                root: Path | None = None, sym_on: bool = False) -> tuple:
    base = Path(os.path.commonpath((source.parent, directory)))
    # MWCC (natively or through a loader) can crash on long artifact paths.
    with tempfile.TemporaryDirectory(prefix="m2c-mwcc-") as temporary:
        anchor = Path(temporary) / "tree"
        decompile.link_directory(anchor, base.resolve())
        try:
            short_source = anchor / source.relative_to(base)
            short_output = anchor / directory.relative_to(base)
            args, _ = compile_args(command, short_source, short_output)
            if sym_on:
                args[args.index("-c"):args.index("-c")] = ["-sym", "on"]
            for path in include_dirs:
                args[args.index("-c"):args.index("-c")] = ["-i", str(path)]
            obj = directory / source.with_suffix(".o").name
            obj.unlink(missing_ok=True)
            result = campaign.checked_command(args, timeout=120, cwd=root)
            return result, obj, args
        finally:
            decompile.unlink_directory(anchor)


def snapshot_owner(root: Path, source: str, symbol: str) -> tuple[str, dict[str, str]]:
    files, queue = {}, [source]
    while queue:
        name = queue.pop(0)
        if name in files:
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("Source wrapper escapes project root or is missing: " + name)
        text = path.read_text(encoding="utf-8", errors="replace")
        files[name] = text
        for include in re.findall(r'^\s*#\s*include\s+"([^"\n]+\.(?:c|inc))"', text, flags=re.M):
            candidates = [path.parent / include, root / include]
            child = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
            if child is None:
                continue
            if not child.is_relative_to(root.resolve()):
                raise ValueError("Include escapes project root: " + include)
            queue.append(child.relative_to(root.resolve()).as_posix())
    definitions = []
    for name, text in files.items():
        try:
            campaign.function_span(text, symbol)
            definitions.append(name)
        except ValueError as error:
            if not str(error).startswith("function definition not found:"):
                raise
    if len(definitions) != 1:
        raise ValueError(f"Cannot isolate one owner definition for {symbol}: {len(definitions)}")
    owner = definitions[0]
    return owner, files


def stage_owner(root: Path, source: str, owner: str, files: dict[str, str], symbol: str,
                draft: str | None, folder: Path) -> Path:
    import local_campaign as campaign
    prepared = dict(files)
    if draft is not None:
        start, end = campaign.function_span(files[owner], symbol)
        # Existing owner macros take precedence; untouched baselines get no helper.
        prepared[owner] = files[owner][:start] + FIELD_FALLBACK + draft.rstrip() + files[owner][end:]
    for name, text in prepared.items():
        if name != owner:
            for child in files:
                if child == name:
                    continue
                pattern = re.compile(rf'(?m)^(\s*#\s*include\s+)"{re.escape(child)}"')
                relative = Path(os.path.relpath(folder / child, (folder / name).parent)).as_posix()
                text = pattern.sub(lambda match: match.group(1) + '"' + relative + '"', text)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        decompile.write_text(path, text)
    return folder / source


def apply_owner_rewrites(files: dict[str, str], target_symbol: str,
                         rewrites: list[dict] | None) -> tuple[dict[str, str], list[dict]]:
    """Apply signature-preserving helper replacements to a captured owner snapshot."""
    prepared, evidence, seen = dict(files), [], set()
    for rewrite in rewrites or []:
        name, symbol, replacement = rewrite.get("path"), rewrite.get("symbol"), rewrite.get("source")
        if not isinstance(name, str) or name not in prepared:
            raise ValueError("Owner rewrite path is outside the captured scoring unit")
        if not isinstance(symbol, str) or not symbol or symbol == target_symbol:
            raise ValueError("Owner rewrites may only replace named helper functions")
        if (name, symbol) in seen:
            raise ValueError("Duplicate owner helper rewrite: " + symbol)
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError("Owner helper rewrite has no source: " + symbol)
        seen.add((name, symbol))
        original_start, original_end = campaign.function_span(prepared[name], symbol)
        replacement_start, replacement_end = campaign.function_span(replacement, symbol)
        original_signature = campaign.function_signature(prepared[name], original_start, original_end)
        replacement_signature = campaign.function_signature(replacement, replacement_start, replacement_end)
        if replacement_start != 0 or replacement[replacement_end:].strip():
            raise ValueError("Owner helper rewrite must define only its assigned function: " + symbol)
        if replacement_signature != original_signature:
            raise ValueError("Owner helper rewrite changed signature: " + symbol)
        normalized = replacement.rstrip()
        prepared[name] = prepared[name][:original_start] + normalized + prepared[name][original_end:]
        evidence.append({"path": name, "symbol": symbol,
                         "sha256": digest((normalized + "\n").encode())})
    return prepared, evidence


def score_object(root: Path, target: Path, candidate: Path, symbol: str, folder: Path) -> dict:
    import local_campaign as campaign
    output = folder / "objdiff.json"
    output.unlink(missing_ok=True)
    objdiff = next((root / "build" / "tools" / name
                    for name in ("objdiff-cli.exe", "objdiff-cli")
                    if (root / "build" / "tools" / name).is_file()), None)
    if objdiff is None:
        raise RuntimeError(f"objdiff-cli is missing from selected decomp root: {root}")
    command = [str(objdiff), "diff", "-1", str(target), "-2", str(candidate),
               "-o", str(output), "--format", "json", symbol]
    result = campaign.checked_command(command, timeout=120, cwd=root)
    (folder / "objdiff.log").write_text(result.stdout + result.stderr, encoding="utf-8", newline="\n")
    if result.returncode or not output.is_file():
        return {"status": "objdiff_error", "error": (result.stdout + result.stderr)[-3000:], "command": command}
    data = json.loads(output.read_text(encoding="utf-8"))
    left = next((value for value in data.get("left", {}).get("symbols", []) if value.get("name") == symbol), None)
    right = next((value for value in data.get("right", {}).get("symbols", []) if value.get("name") == symbol), None)
    if not left or not right:
        return {"status": "missing_function", "error": "Target or draft object has no assigned function", "command": command}
    pct = left.get("match_percent")
    if not isinstance(pct, (int, float)):
        return {"status": "objdiff_error", "error": "No numeric match percentage for assigned target", "command": command}
    rows = campaign.instruction_rows(left, right)
    mismatches = [{"index": index, "target": want, "candidate": have, "source_line": line,
                   "kind": campaign.diff_class(want, have)}
                  for index, (want, have, line) in enumerate(rows) if want != have]
    classes = Counter(row["kind"] for row in mismatches)
    return {"status": "scorer_exact" if pct == 100.0 else "non_exact", "pct": pct,
            "target_size": left.get("size"), "candidate_size": right.get("size"), "command": command,
            "mismatch_classification": {"counts": dict(classes), "total": len(mismatches),
                                        "register_only": bool(mismatches) and set(classes) <= {"register", "branch", "data"},
                                        "mismatches": mismatches[:32]}}


def compile_baseline(root: Path, source: str, owner: str, files: dict[str, str], symbol: str,
                     command: str, environment: str, baseline_root: Path, force: bool = False) -> dict:
    key = digest(json.dumps({"files": files, "command": command, "environment": environment,
                             "validator": digest(IMPLEMENTATION.read_bytes())}, sort_keys=True).encode())
    folder = baseline_root / key
    saved = folder / "result.json"
    expected_obj = folder / "objects" / Path(source).with_suffix(".o").name
    if saved.is_file() and not force:
        try:
            previous = json.loads(saved.read_text(encoding="utf-8"))
            hashes = previous.get("overlay_sha256", {})
            if (previous.get("input_sha256") == key and previous.get("object") == str(expected_obj)
                    and set(hashes) == set(files)
                    and all(digest((folder / "overlay" / name).read_bytes()) == sha for name, sha in hashes.items())
                    and digest((folder / "compile.log").read_bytes()) == previous.get("log_sha256")
                    and (digest(expected_obj.read_bytes()) if expected_obj.is_file() else None) == previous.get("object_sha256")
                    and (not previous.get("compile_ok") or expected_obj.is_file())):
                return {**previous, "artifacts": str(folder)}
        except (OSError, ValueError, TypeError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    staged = stage_owner(root, source, owner, files, symbol, None, folder / "overlay")
    objects = folder / "objects"
    objects.mkdir(exist_ok=True)
    include_dirs = [root, (root / source).parent]
    if owner != source:
        include_dirs.append((root / owner).parent)
    result, obj, args = run_compile(command, staged, objects, include_dirs, root)
    log = result.stdout + result.stderr
    baseline = {"input_sha256": key, "compile_ok": result.returncode == 0 and obj.is_file(), "returncode": result.returncode,
                "command": args, "source": str(staged), "object": str(obj),
                "object_sha256": digest(obj.read_bytes()) if obj.is_file() else None,
                "overlay_sha256": {name: digest((folder / "overlay" / name).read_bytes()) for name in files},
                "log_sha256": digest(log.encode()), "error": log[-6000:]}
    (folder / "compile.log").write_text(log, encoding="utf-8", newline="\n")
    decompile.write_json(saved, baseline)
    return {**baseline, "artifacts": str(folder)}


def validate_one(root: Path, row: dict, command: str, unit: dict, fallback: str,
                 folder: Path, force: bool = False, environment: str = "", baseline_root: Path | None = None,
                 source_lines: bool = False, owner_rewrites: list[dict] | None = None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    record = {"id": row["id"], "symbol": row["symbol"], "unit": row["unit"],
              "draft_status": row["status"], "artifacts": str(folder), "promoted": False,
              "compile_ok": False, "review_eligible": False, "environment_sha256": environment,
              "tested_at": datetime.now(UTC).isoformat()}
    artifact = row.get("artifacts")
    if not artifact or not (Path(artifact) / "draft.c").is_file():
        return {**record, "status": "missing_draft", "error": row.get("error", "No draft artifact")}
    draft_bytes = (Path(artifact) / "draft.c").read_bytes()
    draft = draft_bytes.decode()
    record["draft_sha256"] = digest(draft_bytes)
    record["unresolved_macros"] = sorted(set(re.findall(r'\b(?:M2C_\w+|saved_reg_\w+)\b', draft)) - {"M2C_FIELD"})
    source, owner, files, baseline_files = row.get("source"), None, {}, {}
    try:
        if source and command:
            owner, files = snapshot_owner(root, source, row["symbol"])
            baseline_files = dict(files)
            files, rewrite_evidence = apply_owner_rewrites(files, row["symbol"], owner_rewrites)
            record["owner_rewrites"] = rewrite_evidence
    except (OSError, ValueError) as exc:
        record["context_error"] = str(exc)
    try:
        if owner:
            record["scope"] = "full_owner_scoring_unit"
            start, end = campaign.function_span(files[owner], row["symbol"])
            record["baseline_signature"] = campaign.function_signature(files[owner], start, end)
            record["baseline_pragmas"] = campaign.pragmas_active_at(files[owner], start)
            staged = stage_owner(root, source, owner, files, row["symbol"], draft, folder / "overlay")
            prefix_end = campaign.function_span(files[owner], row["symbol"])[0]
            record["draft_source_line"] = (files[owner][:prefix_end] + FIELD_FALLBACK).count("\n") + 1
            record["draft_source_end_line"] = record["draft_source_line"] + draft.count("\n")
        else:
            record["scope"] = "standalone_unit_flags" if command else "standalone_fallback_flags"
            staged = folder / "standalone.c"
            staged.write_text('#include "dolphin/types.h"\n' + FIELD_FALLBACK + draft, encoding="utf-8", newline="\n")
            command = command or fallback
        if not command:
            return {**record, "status": "missing_compile_command", "error": "No MWCC compiler command available"}
        try:
            start, end = campaign.function_span(draft, row["symbol"])
            record["draft_signature"] = campaign.function_signature(draft, start, end)
        except ValueError as exc:
            record["draft_definition_error"] = str(exc)
        record["signature_preserved"] = bool(record.get("baseline_signature")) and \
                                         record.get("draft_signature") == record.get("baseline_signature")
        key = digest(json.dumps({"draft": record["draft_sha256"], "files": files, "command": command,
                                 "validator": digest(IMPLEMENTATION.read_bytes()),
                                 "environment": environment,
                                 "source_lines": source_lines,
                                 "target": digest((root / unit["target_path"]).read_bytes()) if unit.get("target_path") else None},
                                sort_keys=True).encode())
        saved = folder / "result.json"
        if saved.is_file() and not force:
            previous = json.loads(saved.read_text(encoding="utf-8"))
            if previous.get("input_sha256") == key:
                return previous
        record["input_sha256"] = key
        if owner:
            baseline = compile_baseline(root, source, owner, baseline_files, row["symbol"], command, environment,
                                        baseline_root if baseline_root is not None else folder.parent / "baselines", force)
            record["baseline"] = baseline
        out = folder / "objects"
        out.mkdir(exist_ok=True)
        # Copied owners/wrappers still resolve their original local header directory.
        include_dirs = [root]
        if source:
            include_dirs.append((root / source).parent)
        if owner and owner != source:
            include_dirs.append((root / owner).parent)
        result, obj, args = run_compile(command, staged, out, include_dirs, root)
        record["compile_command"] = args
        record.update(compile_input=str(staged), compile_output=str(obj))
        log = result.stdout + result.stderr
        (folder / "compile.log").write_text(log, encoding="utf-8", newline="\n")
        record.update(compile_ok=result.returncode == 0 and obj.is_file(), compile_returncode=result.returncode)
        if not record["compile_ok"]:
            status = "baseline_compile_error" if owner and not baseline["compile_ok"] else "compile_error"
            return {**record, "status": status, "error": log[-6000:]}
        if record.get("draft_definition_error"):
            return {**record, "status": "no_generated_function", "error": record["draft_definition_error"]}
        target = root / unit.get("target_path", "")
        if not target.is_file():
            return {**record, "status": "compiled_without_target", "error": "No retail object for objdiff"}
        measured = score_object(root, target, obj, row["symbol"], folder)
        actionable = bool((measured.get("mismatch_classification") or {}).get("register_only"))
        if source_lines and actionable:
            debug_out = folder / "debug-objects"
            debug_out.mkdir(exist_ok=True)
            debug_result, debug_obj, debug_args = run_compile(
                command, staged, debug_out, include_dirs, root, sym_on=True)
            debug_log = debug_result.stdout + debug_result.stderr
            (folder / "debug-compile.log").write_text(debug_log, encoding="utf-8", newline="\n")
            record.update(debug_compile_command=debug_args,
                          debug_compile_ok=debug_result.returncode == 0 and debug_obj.is_file())
            if record["debug_compile_ok"]:
                measured = score_object(root, target, debug_obj, row["symbol"], folder)
        elif source_lines:
            record["debug_compile_skipped"] = "mismatch_not_register_allocatable"
        line_start, line_end = record.get("draft_source_line"), record.get("draft_source_end_line")
        if line_start and line_end:
            for mismatch in (measured.get("mismatch_classification") or {}).get("mismatches") or []:
                line = mismatch.get("source_line")
                if isinstance(line, int):
                    mismatch["owner_source_line"] = line
                    mismatch["source_line"] = line - line_start + 1 if line_start <= line <= line_end else None
        record.update(measured)
        # Scratch scores alone never establish owner, relocation, link or policy fidelity.
        record["review_eligible"] = False
        return record
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return {**record, "status": "validation_error", "error": f"{type(exc).__name__}: {exc}"}


def run_validation(root: Path, batch: Path, limit: int = 0, force: bool = False) -> int:
    import local_campaign as campaign
    if root.resolve() != campaign.ROOT.resolve():
        raise ValueError("Validation root must match the configured campaign root")
    data = json.loads((batch / "results.json").read_text(encoding="utf-8"))
    if not data["summary"].get("finished"):
        raise ValueError("Draft batch must be complete before validation")
    work = sorted(data["results"], key=lambda row: (row["unit"], str(row["address"])))
    if limit:
        work = work[:limit]
    with campaign.COORDINATOR.build("Serial draft validation", "Read unit compiler commands"):
        commands = campaign.checked_command(["ninja", "-t", "commands", "all_source"], timeout=60)
        if commands.returncode:
            raise ValueError(commands.stderr or "Cannot read unit compiler commands")
        compiler_commands = command_map(commands.stdout)
        project = json.loads((root / "objdiff.json").read_text(encoding="utf-8"))
        environment = environment_fingerprint(root, compiler_commands)
    units = {unit["name"]: unit for unit in project.get("units", [])}
    fallback = next(iter(compiler_commands.values()), "")
    folder = REPO_ROOT / "build/decomp-support/validation" / (batch.name + (f"-limit{limit}" if limit else ""))
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    final_environment = None

    def checkpoint(finished: bool = False):
        summary = {"batch": str(batch.resolve()), "folder": str(folder), "jobs": 1,
                   "validator_version": m2c_report.VALIDATOR_VERSION,
                   "workspace": str(root.resolve()),
                   "environment_sha256": environment,
                   "final_environment_sha256": final_environment,
                   "environment_unchanged": final_environment == environment if finished else None,
                   "total": len(work), "processed": len(results), "finished": finished,
                   "updated_at": datetime.now(UTC).isoformat(),
                   "counts": dict(Counter(row["status"] for row in results)),
                   "compiler_succeeded": sum(row.get("compile_ok", False) for row in results),
                   "compiled_functions": sum(row.get("compile_ok", False) and row["status"] not in
                                             {"no_generated_function", "missing_function"} for row in results), "promoted": 0}
        decompile.write_json(folder / "manifest.json", summary)
        if finished:
            decompile.write_json(folder / "results.json", {"summary": summary, "results": results})
            m2c_report.refresh_report_safely(REPO_ROOT / "build/decomp-support", campaign.REPORT_FILE)
        print(json.dumps(summary), flush=True)

    checkpoint()
    for row in work:
        with campaign.COORDINATOR.build("Serial draft validation", f"MWCC/objdiff: {row['symbol']}"):
            if (campaign.STATE_DIR / "halt.json").exists():
                # Scratch testing never edits project source, but do not hide a fleet halt.
                halt = json.loads((campaign.STATE_DIR / "halt.json").read_text(encoding="utf-8"))
                if not results:
                    print(json.dumps({"fleet_halt": halt, "mode": "read-only scratch compile"}), flush=True)
            result = validate_one(root, row, compiler_commands.get(row.get("source"), ""),
                                  units.get(row["unit"], {}), fallback, folder / row["id"], force, environment)
        decompile.write_json(folder / row["id"] / "result.json", result)
        results.append(result)
        checkpoint()
    with campaign.COORDINATOR.build("Serial draft validation", "Audit source/compiler inputs after testing"):
        final_environment = environment_fingerprint(root, compiler_commands)
    checkpoint(finished=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path, help="completed m2c batch directory")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return run_validation(DECOMP_ROOT, args.batch, args.limit, args.force)


if __name__ == "__main__":
    raise SystemExit(main())

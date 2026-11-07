#!/usr/bin/env python3
"""Bank standalone exact candidates as visible source, then link certified leaves."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from harness_paths import DECOMP_ROOT, VERSION, build_tool, compiler_wrapper
from m2c_draft_attempts import PRELUDE
import m2c_compile_repair
from m2c_context import build_check


AUTO_TEXT = re.compile(r"(?:^|/)auto_[0-9A-Fa-f]{2}_([0-9A-Fa-f]{8})_text$")
CANDIDATE_TEXT = re.compile(r"(?:^|/)candidate_([0-9A-Fa-f]{8})$")
OBJECT_HEADER = re.compile(r"^(?!Sections:)(\S.*):$")
START = re.compile(r"\bstart:0x([0-9A-Fa-f]+)")
LINK_OBJECT = re.compile(r"\bin (candidate_[0-9A-Fa-f]{8})\.o\b")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)


def project_contract(root: Path, version: str) -> dict | None:
    path = root / ".m2c-candidate-bank.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported candidate-bank contract")
    configured = (data.get("versions") or {}).get(version)
    if configured is None:
        return None
    required = {"manifest", "source_dir", "split_file", "section"}
    if not required <= configured.keys():
        raise ValueError("Incomplete candidate-bank contract for " + version)
    return configured


def source_backed_symbols(report: dict, root: Path | None = None) -> set[str]:
    return {function["name"] for unit in report.get("units") or []
            if (source := (unit.get("metadata") or {}).get("source_path"))
            and (root is None or (root / source).is_file())
            for function in unit.get("functions") or []}


def function_index(report: dict) -> dict[str, dict]:
    indexed = {}
    for unit in report.get("units") or []:
        match = AUTO_TEXT.search(str(unit.get("name") or ""))
        candidate = CANDIDATE_TEXT.search(str(unit.get("name") or ""))
        if match and (unit.get("metadata") or {}).get("auto_generated"):
            base = int(match.group(1), 16)
        elif candidate:
            base = int(candidate.group(1), 16)
        else:
            continue
        sections = unit.get("sections") or []
        if len(sections) != 1 or sections[0].get("name") != ".text":
            continue
        for function in unit.get("functions") or []:
            indexed[function["name"]] = {
                "unit": unit["name"], "address": base + int(function["address"]),
                "size": int(function["size"]),
            }
    return indexed


def candidate_rows(validation_path: Path, report_path: Path, source_dir: str) -> list[dict]:
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    summary = validation.get("summary") or {}
    if not summary.get("finished") or summary.get("environment_unchanged") is not True:
        raise ValueError("Candidate banking requires a complete stable validation")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    linked = source_backed_symbols(report, report_path.resolve().parents[2])
    functions = function_index(report)
    evidence_sha256 = digest(validation_path)
    rows = []
    for result in validation.get("results") or []:
        symbol = result.get("symbol")
        if not result.get("instruction_exact") or not symbol or symbol in linked:
            continue
        owner = functions.get(symbol)
        winner = Path(result.get("winner_path") or "")
        flags = result.get("compiler_flags")
        profile = result.get("profile")
        if owner is None or not winner.is_file() or not isinstance(flags, list) or not flags or not profile:
            continue
        if digest(winner) != result.get("winner_sha256"):
            raise ValueError("Validation winner changed: " + str(winner))
        address, size = owner["address"], owner["size"]
        relative = f"{source_dir.removeprefix('src/').rstrip('/')}/candidate_{address:08X}.c"
        winner_text = winner.read_text(encoding="utf-8")
        repaired = m2c_compile_repair.repair_draft(winner_text, symbol, "")
        if not repaired["ready_for_compile"]:
            continue
        source = PRELUDE + repaired["code"].lstrip()
        rows.append({"symbol": symbol, "unit": owner["unit"], "address": address,
                     "end": address + size, "size": size, "path": relative,
                     "profile": str(profile), "compiler_flags": flags,
                     "source": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                     "winner_sha256": result["winner_sha256"],
                     "validation": str(validation_path.resolve()),
                     "validation_sha256": evidence_sha256, "completed": False})
    return sorted(rows, key=lambda row: (row["address"], row["symbol"]))


def split_blocks(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines) if OBJECT_HEADER.match(line)), len(lines))
    header = "\n".join(lines[:first]).rstrip()
    blocks, current = [], []
    for line in lines[first:]:
        if OBJECT_HEADER.match(line):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return header, blocks


def merge_splits(text: str, candidates: list[dict], section: str) -> str:
    header, blocks = split_blocks(text)
    names = {block.splitlines()[0][:-1] for block in blocks}
    intervals = []
    for block in blocks:
        starts = [int(value, 16) for value in START.findall(block)]
        end_match = re.search(r"\bend:0x([0-9A-Fa-f]+)", block)
        if starts and end_match:
            intervals.append((min(starts), int(end_match.group(1), 16), block.splitlines()[0][:-1]))
    for row in candidates:
        if row["path"] in names:
            continue
        overlap = [name for start, end, name in intervals
                   if row["address"] < end and row["end"] > start]
        if overlap:
            raise ValueError(f"{row['symbol']} overlaps configured split {overlap[0]}")
        blocks.append(f"{row['path']}:\n\t{section:<11} start:0x{row['address']:08X} "
                      f"end:0x{row['end']:08X}")
    def key(block: str) -> tuple[int, str]:
        match = START.search(block)
        if match is None:
            raise ValueError("Split block has no start address: " + block.splitlines()[0])
        return int(match.group(1), 16), block.splitlines()[0]
    return header + "\n\n" + "\n\n".join(sorted(blocks, key=key)) + "\n"


def materialize(root: Path, version: str, validation: Path) -> dict:
    contract = project_contract(root, version)
    if contract is None:
        return {"supported": False, "planned": 0, "added": 0, "candidates": []}
    manifest_path = root / contract["manifest"]
    split_path = root / contract["split_file"]
    rows = candidate_rows(validation, root / "build" / version / "report.json",
                          contract["source_dir"])
    if not rows:
        return {"supported": True, "planned": 0, "added": 0, "candidates": [],
                "manifest": str(manifest_path), "split_file": str(split_path)}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = {row["symbol"]: row for row in manifest.get("candidates") or []}
    added = []
    for row in rows:
        if row["symbol"] in existing:
            continue
        source_path = root / "src" / row["path"]
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(row.pop("source"), encoding="utf-8")
        existing[row["symbol"]] = row
        added.append(row)
    candidates = sorted(existing.values(), key=lambda row: (row["address"], row["symbol"]))
    write_json(manifest_path, {"schema_version": 1, "candidates": candidates})
    split_path.write_text(merge_splits(split_path.read_text(encoding="utf-8"), added,
                                       contract["section"]), encoding="utf-8")
    return {"supported": True, "planned": len(rows), "added": len(added),
            "candidates": candidates, "manifest": str(manifest_path),
            "split_file": str(split_path)}


def _project_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def configure_command(root: Path, version: str) -> list[str]:
    """Reconfigure a dtk-template project with its already-installed local tools."""
    command = [sys.executable, "configure.py", "--version", version, "--no-progress",
               "--dtk", _project_relative(root, build_tool(root, "dtk")),
               "--objdiff", _project_relative(root, build_tool(root, "objdiff-cli")),
               "--compilers", "build/compilers", "--binutils", "build/binutils"]
    wrapper = compiler_wrapper(root)
    if wrapper:
        command += ["--wrapper", _project_relative(root, Path(wrapper[0]))]
    return command + ["--sjiswrap", "build/tools/sjiswrap.exe"]


def execute(command: list[str], root: Path, log: Path) -> None:
    with log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(command, cwd=root, stdout=handle, stderr=subprocess.STDOUT, text=True)
    if completed.returncode:
        raise RuntimeError(f"Command failed ({completed.returncode}); see {log}")


def certified_paths(report_path: Path, paths: set[str]) -> set[str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    certified = set()
    for unit in report.get("units") or []:
        source = (unit.get("metadata") or {}).get("source_path")
        if source not in {"src/" + path for path in paths}:
            continue
        functions = unit.get("functions") or []
        sections = unit.get("sections") or []
        if (functions and all(float(row.get("fuzzy_match_percent") or 0) == 100 for row in functions)
                and all(float(row.get("fuzzy_match_percent") or 0) == 100 for row in sections)):
            certified.add(source.removeprefix("src/"))
    return certified


def referenced_candidate_paths(log: Path, candidates: list[dict]) -> set[str]:
    stems = set(LINK_OBJECT.findall(log.read_text(encoding="utf-8", errors="replace")))
    return {row["path"] for row in candidates if Path(row["path"]).stem in stems}


def status_paths(root: Path) -> set[str]:
    output = git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return {line[3:] for line in output.splitlines() if len(line) > 3}


def restore_snapshot(root: Path, contract: dict, output: Path, record: dict) -> None:
    git(root, "restore", "--staged", "--", ".", check=False)
    for key, backup_name in (("manifest", "manifest.backup"), ("split_file", "splits.backup")):
        backup = output / backup_name
        if backup.is_file():
            (root / contract[key]).write_bytes(backup.read_bytes())
    for relative in record.get("candidate_paths") or []:
        path = root / "src" / relative
        if path.is_file() and relative not in {
                row.get("path") for row in json.loads(
                    (root / contract["manifest"]).read_text(encoding="utf-8")).get("candidates") or []}:
            path.unlink()


def publish(root: Path, version: str, validation: Path, result: dict) -> None:
    support = root / "build/decomp-support" / version
    refinement_path = support / "refinement-runs" / validation.parent.name / "results.json"
    if refinement_path.is_file():
        refinement = json.loads(refinement_path.read_text(encoding="utf-8"))
        gate = refinement.setdefault("unit_gate", {})
        gate.update(candidate_banked=int(result.get("added") or 0),
                    candidate_linked=int(result.get("candidate_linked") or 0),
                    candidate_visible=int(result.get("candidate_only") or 0))
        refinement["updated_at"] = datetime.now(UTC).isoformat()
        write_json(refinement_path, refinement)
    import local_campaign_m2c_report
    local_campaign_m2c_report.refresh_report_safely(
        support, root / "build" / version / "report.json")


def run(root: Path, version: str, validation: Path, output: Path) -> dict:
    root, validation, output = root.resolve(), validation.resolve(), output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    record = output / "result.json"
    contract = project_contract(root, version)
    if contract is None:
        result = {"kind": "m2c_candidate_bank", "project_root": str(root), "version": version,
                  "validation": str(validation), "state": "unsupported", "promoted": False,
                  "finished": True, "started_at": datetime.now(UTC).isoformat(), "pid": os.getpid()}
        result.update(state="unsupported", finished=True)
        write_json(record, result)
        return result
    if git(root, "branch", "--show-current").stdout.strip() not in {"main", "master"}:
        raise ValueError("Candidate banking requires main or master")
    prior = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
    if prior.get("state") == "promoted" and prior.get("commit"):
        if git(root, "merge-base", "--is-ancestor", prior["commit"], "HEAD", check=False).returncode == 0:
            publish(root, version, validation, prior)
            return prior
        raise ValueError("Recorded candidate-bank commit is no longer on this branch")
    if prior.get("base_commit"):
        current = git(root, "rev-parse", "HEAD").stdout.strip()
        if current != prior["base_commit"]:
            parent = git(root, "rev-parse", "HEAD^").stdout.strip()
            subject = git(root, "log", "-1", "--pretty=%s").stdout.strip()
            if (parent == prior["base_commit"] and subject == prior.get("commit_message")
                    and build_check(root, version).get("status") == "exact"):
                prior.update(state="promoted", promoted=True, finished=True, commit=current,
                             recovered=True, phase="complete")
                write_json(record, prior)
                publish(root, version, validation, prior)
                return prior
            raise ValueError("Project HEAD changed during interrupted candidate banking")
        restore_snapshot(root, contract, output, prior)
    if status_paths(root):
        raise ValueError("Candidate banking requires a clean worktree")
    base = git(root, "rev-parse", "HEAD").stdout.strip()
    result = {"kind": "m2c_candidate_bank", "project_root": str(root), "version": version,
              "validation": str(validation), "state": "checking", "promoted": False,
              "started_at": datetime.now(UTC).isoformat(), "pid": os.getpid(),
              "base_commit": base}
    write_json(record, result)
    lock = Path(git(root, "rev-parse", "--git-dir").stdout.strip()) / "m2c-candidate-bank.lock"
    if not lock.is_absolute():
        lock = root / lock
    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(lock_fd, str(os.getpid()).encode())
    original = {root / contract[key]: (root / contract[key]).read_bytes()
                for key in ("manifest", "split_file")}
    (output / "manifest.backup").write_bytes(original[root / contract["manifest"]])
    (output / "splits.backup").write_bytes(original[root / contract["split_file"]])
    try:
        result["phase"] = "materialize"
        write_json(record, result)
        planned = candidate_rows(validation, root / "build" / version / "report.json",
                                 contract["source_dir"])
        result["candidate_paths"] = [row["path"] for row in planned]
        write_json(record, result)
        staged = materialize(root, version, validation)
        result.update(planned=staged["planned"], added=staged["added"], phase="candidate_report")
        write_json(record, result)
        if not staged["added"]:
            result.update(state="nothing_to_bank", finished=True)
            return result
        execute(configure_command(root, version), root, output / "configure-candidates.log")
        execute(["ninja", "progress"], root, output / "candidate-report.log")
        manifest_path = root / contract["manifest"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        added_symbols = {row["symbol"] for row in planned}
        added_paths = {row["path"] for row in manifest["candidates"] if row["symbol"] in added_symbols}
        certified = certified_paths(root / "build" / version / "report.json", added_paths)
        for row in manifest["candidates"]:
            if row["path"] in certified:
                row["completed"] = True
        write_json(manifest_path, manifest)
        result.update(certified=len(certified), phase="retail_build")
        write_json(record, result)
        linked = set(certified)
        build_attempt = 0
        while True:
            for row in manifest["candidates"]:
                if row["symbol"] in added_symbols:
                    row["completed"] = row["path"] in linked
            write_json(manifest_path, manifest)
            configure_log = output / f"configure-linked-{build_attempt}.log"
            build_log = output / f"retail-build-{build_attempt}.log"
            execute(configure_command(root, version), root, configure_log)
            try:
                execute(["ninja", f"build/{version}/ok"], root, build_log)
            except RuntimeError:
                blocked = referenced_candidate_paths(build_log, manifest["candidates"]) & linked
                if blocked:
                    linked -= blocked
                    result.setdefault("link_demotions", []).append({
                        "attempt": build_attempt, "paths": sorted(blocked),
                        "reason": "unresolved split-local dependency"})
                    result.update(candidate_linked=len(linked),
                                  candidate_only=len(added_paths - linked))
                    write_json(record, result)
                    build_attempt += 1
                    continue
                if linked:
                    result.setdefault("link_demotions", []).append({
                        "attempt": build_attempt, "paths": sorted(linked),
                        "reason": "unattributed retail link failure"})
                    linked.clear()
                    build_attempt += 1
                    continue
                raise
            checked = build_check(root, version)
            if checked.get("status") == "exact":
                break
            if not linked:
                raise ValueError("Non-linking candidate bank changed the retail SHA-1")
            result.setdefault("link_demotions", []).append({
                "attempt": build_attempt, "paths": sorted(linked),
                "reason": "retail SHA mismatch"})
            linked.clear()
            build_attempt += 1
        result.update(candidate_linked=len(linked), candidate_only=len(added_paths - linked),
                      link_attempts=build_attempt + 1)
        execute(["ninja", "progress"], root, output / "final-report.log")
        changed = status_paths(root)
        if not changed:
            raise ValueError("Candidate bank produced no tracked project changes")
        git(root, "add", "--", *sorted(changed))
        checked_diff = git(root, "diff", "--cached", "--check", check=False)
        if checked_diff.returncode:
            raise ValueError(checked_diff.stdout + checked_diff.stderr)
        message = f"Bank {staged['added']} m2c function candidates"
        result["commit_message"] = message
        write_json(record, result)
        committed = git(root, "commit", "-m", message)
        result.update(state="promoted", promoted=True, finished=True, base_commit=base,
                      commit=git(root, "rev-parse", "HEAD").stdout.strip(), changed=sorted(changed),
                      build=checked, commit_output=committed.stdout.strip(), phase="complete")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        restore_snapshot(root, contract, output, result)
        result.update(state="rejected", finished=True, error=f"{type(error).__name__}: {error}")
    finally:
        os.close(lock_fd)
        lock.unlink(missing_ok=True)
        write_json(record, result)
    if result.get("promoted"):
        publish(root, version, validation, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=VERSION,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    result = run(args.root, args.version, args.validation, args.output)
    print(json.dumps({key: result.get(key) for key in (
        "state", "promoted", "planned", "added", "certified", "candidate_linked",
        "candidate_only", "link_attempts", "commit", "error")}, indent=2))


if __name__ == "__main__":
    main()

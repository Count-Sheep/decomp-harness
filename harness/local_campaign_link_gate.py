"""Isolated DOL checksum gate for object-verified recovery candidates."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import local_campaign_decompile as decompile
import local_campaign_knowledge as knowledge
from harness_paths import VERSION, is_compiler_wrapper
from local_campaign_coordination import Coordinator


def relative_path(value: str) -> Path:
    path = Path(value)
    # anchor also catches Windows rooted ("\\x") and drive-relative ("C:x") paths.
    if path.anchor or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Link capsule requires root-relative paths: " + value)
    return path


def same_path(left: str | None, right: str | None) -> bool:
    """Root-relative path equality that ignores the host's separator style."""
    return bool(left) and bool(right) and Path(left) == Path(right)


def expected_checksum(text: str, output: str) -> str:
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{40})\s+\*?(.+)", line)
        if match and same_path(match[2], output):
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise ValueError("Expected exactly one SHA-1 entry for " + output)
    return matches[0]


def parse_commands(plain: list, expanded: list, target: str) -> dict:
    def one(rows, output):
        matches = [row for row in rows if same_path(row.get("output"), output)]
        if len(matches) != 1:
            raise ValueError("Expected exactly one Ninja command for " + output)
        return decompile.split_command(matches[0]["command"])

    relative_path(target)
    command = one(plain, target)
    wide = one(expanded, target)
    response = [i for i, token in enumerate(command) if token.startswith("@")]
    if len(response) != 1 or response[0] != len(command) - 1 or wide[:response[0]] != command[:response[0]]:
        raise ValueError("Unsupported linker response-file command")
    # The linker runs natively on Windows and through a loader (wibo/wine) elsewhere.
    # Attested flag forms: none, or hardware FP with default libraries disabled.
    # Other flags may introduce uncaptured inputs.
    wrapped = bool(command) and is_compiler_wrapper(command[0])
    linker = 1 if wrapped else 0
    prefix = command[linker + 1:-5]
    if (len(command) < linker + 6 or Path(command[linker]).name.lower() != "mwldeppc.exe"
            or prefix not in ([], ["-fp", "hardware", "-nodefaults"])
            or command[-5] != "-lcf" or command[-3] != "-o" or not same_path(command[-2], target)):
        raise ValueError("Unsupported MWCC link command; no project files changed")
    target = command[-2]  # the graph's own spelling; Windows Ninja files may use backslashes
    objects = wide[response[0]:]
    if (not objects or any(not token.endswith(".o") or token.startswith(("@", "-")) for token in objects)
            or len(set(objects)) != len(objects)):
        raise ValueError("Link inputs must be an ordered list of unique object paths")
    dol = target[:-len(Path(target).suffix)] + ".dol" if Path(target).suffix else target + ".dol"
    conversion = one(plain, dol)
    if (len(conversion) != 4 or Path(conversion[0]).name.lower() not in ("dtk", "dtk.exe")
            or conversion[1] != "elf2dol" or conversion[2:] != [target, dol]):
        raise ValueError("Unsupported DOL conversion command")
    tools = [*command[:linker + 1], conversion[0]]
    for value in [*tools, command[-4], command[-1][1:], *objects, dol]:
        relative_path(value)
    return {"link_command": command, "objects": objects, "elf": target, "dol": dol,
            "response_file": command[-1][1:], "conversion_command": conversion,
            "tools": tools, "linker_script": command[-4]}


def discover(root: Path, target: str, checksum_file: str, mode: str) -> dict:
    def compdb(expand=False):
        command = ["ninja", "-t", "compdb", *(["-x"] if expand else []), "link", "elf2dol"]
        return json.loads(subprocess.check_output(command, cwd=root, text=True, encoding="utf-8", timeout=60))

    plain, expanded = compdb(), compdb(True)
    plan = parse_commands(plain, expanded, target)
    units = json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]
    lookup = {}
    for unit in units:
        if unit.get("base_path") and unit.get("target_path"):
            key = Path(unit["base_path"])
            if key in lookup and lookup[key] != unit["target_path"]:
                raise ValueError("Ambiguous objdiff object mapping")
            lookup[key] = unit["target_path"]
    if mode not in {"native", "retail-reference"}:
        raise ValueError("Unknown link context")
    files = [{"slot": value, "source": lookup.get(Path(value), value) if mode == "retail-reference" else value}
             for value in plan["objects"]]
    files.extend({"slot": value, "source": value} for value in [*plan["tools"], plan["linker_script"]])
    for file in files:
        relative_path(file["source"])
        path = root / file["source"]
        if not path.is_file():
            raise ValueError("Missing link input: " + str(path))
    script = (root / plan["linker_script"]).read_text(encoding="utf-8")
    if re.search(r"(?im)^\s*(?:INCLUDE|SEARCHDIR|INPUT)\b", script):
        raise ValueError("External linker-script dependencies need an explicit capture adapter")
    checksum = (root / relative_path(checksum_file)).read_bytes().decode("utf-8")
    return {**plan, "context": mode, "files": files, "units": {u["name"]: u for u in units},
            "expected_sha1": expected_checksum(checksum, plan["dol"]),
            "checksum_file": checksum_file, "checksum_file_sha256": knowledge.digest(checksum),
            "build_ninja_sha256": knowledge.digest((root / "build.ninja").read_bytes()),
            "objdiff_sha256": knowledge.digest((root / "objdiff.json").read_bytes())}


def link_once(workspace: Path, plan: dict, artifacts: Path) -> dict:
    artifacts.mkdir()
    elf, dol = workspace / plan["elf"], workspace / plan["dol"]
    elf.parent.mkdir(parents=True, exist_ok=True)
    elf.unlink(missing_ok=True)
    dol.unlink(missing_ok=True)
    response = workspace / plan["response_file"]
    with tempfile.TemporaryDirectory(prefix="c-link-") as name:
        anchor = Path(name) / "w"
        decompile.link_directory(anchor, workspace.resolve())
        try:
            link_command = list(plan["link_command"])
            conversion_command = list(plan["conversion_command"])
            # A loader-wrapped linker gets short paths through the anchor; native runs use it as cwd.
            linker = 1 if link_command and is_compiler_wrapper(link_command[0]) else 0
            anchored = linker == 1 and len(link_command) >= 7
            if anchored:
                response.write_text("\n".join(
                    subprocess.list2cmdline(["w/" + value]) for value in plan["objects"]) + "\n",
                    encoding="utf-8", newline="\n")
                for index in (*range(linker + 1), -4, -2):
                    link_command[index] = "w/" + link_command[index]
                link_command[-1] = "@w/" + link_command[-1][1:]
                for index in (0, 2, 3):
                    conversion_command[index] = "w/" + conversion_command[index]
            else:
                response.write_text("\n".join(
                    subprocess.list2cmdline([value]) for value in plan["objects"]) + "\n",
                    encoding="utf-8", newline="\n")
                # Windows resolves a relative program against the parent's cwd, not the child's.
                for command in (link_command, conversion_command):
                    if command and Path(command[0]).parent != Path("."):
                        command[0] = str(anchor / command[0])
            for label, command in (("link", link_command), ("convert", conversion_command)):
                try:
                    result = subprocess.run(command, cwd=name if anchored else anchor,
                                            capture_output=True, text=True, encoding="utf-8",
                                            errors="replace", timeout=180)
                except (OSError, subprocess.TimeoutExpired) as error:
                    (artifacts / (label + ".log")).write_text(str(error), encoding="utf-8", newline="\n")
                    return {"passed": False, "status": label + "_error", "error": str(error)}
                (artifacts / (label + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8",
                                                          newline="\n")
                if result.returncode:
                    return {"passed": False, "status": label + "_error", "returncode": result.returncode,
                            "error": (result.stdout + result.stderr)[-4000:]}
        finally:
            decompile.unlink_directory(anchor)
    if not dol.is_file() or not elf.is_file():
        return {"passed": False, "status": "missing_link_output"}
    actual = hashlib.sha1(dol.read_bytes()).hexdigest()
    dol_hash = knowledge.digest(dol.read_bytes())
    cache = artifacts.parent / "dol-cache"
    cache.mkdir(exist_ok=True)
    blob = cache / (dol_hash + ".dol")
    if not blob.exists():
        shutil.copy2(dol, blob)
    if knowledge.digest(blob.read_bytes()) != dol_hash:
        return {"passed": False, "status": "output_artifact_changed"}
    try:
        (artifacts / "main.dol").hardlink_to(blob)
    except OSError:
        shutil.copy2(blob, artifacts / "main.dol")
    return {"passed": actual == plan["expected_sha1"], "status": "checksum_match" if actual == plan["expected_sha1"] else "checksum_mismatch",
            "sha1": actual, "expected_sha1": plan["expected_sha1"], "dol_sha256": dol_hash,
            "elf_sha256": knowledge.digest(elf.read_bytes()), "artifacts": str(artifacts.resolve())}


def capture_plan(root: Path, output: Path, target: str, checksum_file: str,
                 mode: str = "retail-reference") -> dict:
    """Capture one immutable link baseline for recovery or whole-unit trials."""
    if output.exists():
        raise ValueError("Use a new output directory to preserve checksum history")
    coordinator = Coordinator(root / "build/local_llm_campaign")
    output.mkdir(parents=True, mode=0o700)
    workspace = output / "workspace"
    with coordinator.build("Candidate checksum gate", "Capture isolated link inputs"):
        plan = discover(root, target, checksum_file, mode)
        original_plan = knowledge.encoded(plan)
        for item in plan["files"]:
            source, dest = root / item["source"], workspace / item["slot"]
            before = knowledge.digest(source.read_bytes())
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if knowledge.digest(dest.read_bytes()) != before or knowledge.digest(source.read_bytes()) != before:
                raise ValueError("Link input changed while capturing: " + item["source"])
            item.update(sha256=before, size=dest.stat().st_size)
        if knowledge.encoded(discover(root, target, checksum_file, mode)) != original_plan:
            raise ValueError("Build graph changed while capturing link inputs")
        if any(knowledge.digest((root / item['source']).read_bytes()) != item['sha256'] for item in plan['files']):
            raise ValueError("Link input changed after capture")
        decompile.write_json(output / "plan.json", plan)
    return plan


def run(root: Path, recovery: Path, output: Path, target: str, checksum_file: str,
        mode: str = "retail-reference") -> dict:
    if output.exists():
        raise ValueError("Use a new output directory to preserve checksum history")
    data = json.loads((recovery / "results.json").read_text(encoding="utf-8"))
    if not data["summary"].get("finished") or not data["summary"].get("environment_unchanged"):
        raise ValueError("Recovery must be finished with unchanged inputs")
    coordinator = Coordinator(root / "build/local_llm_campaign")
    plan = capture_plan(root, output, target, checksum_file, mode)
    workspace = output / "workspace"
    records = []

    def checkpoint(phase, baseline=None, current=None, finished=False):
        summary = {"schema_version": 1, "phase": phase, "finished": finished, "current": current,
                   "context": mode, "recovery": str(recovery.resolve()), "project": str(root.resolve()),
                   "expected_sha1": plan["expected_sha1"], "baseline": baseline, "processed": len(records),
                   "linked_checksum_passes": sum(r.get("linked_checksum") is True for r in records),
                   "ready": 0, "promoted": 0, "updated_at": datetime.now(UTC).isoformat(),
                   "producer_sha256": knowledge.digest(Path(__file__).read_bytes()),
                   "plan_sha256": knowledge.digest((output / "plan.json").read_bytes())}
        decompile.write_json(output / "manifest.json", summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": records})
        print(json.dumps({k: summary[k] for k in ('phase', 'processed', 'linked_checksum_passes', 'current')}), flush=True)
        return summary

    checkpoint("baseline_link")
    with coordinator.build("Candidate checksum gate", "Isolated baseline link"):
        baseline = link_once(workspace, plan, output / "baseline")
    if not baseline["passed"]:
        return checkpoint("baseline_failed", baseline, finished=True)
    for item in data["results"]:
        best = item.get("best", {})
        verification = best.get("verification", {})
        if not all(verification.get(axis) is True for axis in ('raw_candidate_bytes', 'relocations', 'owner_object', 'policy_screen_passed')):
            continue
        row = {"target_id": item["target_id"], "symbol": item["symbol"], "linked_checksum": None,
               "context": mode, "ready": False, "policy_approved": None, "human_review": None}
        records.append(row)
        unit = plan["units"].get(item["unit"], {})
        slots = [path for path in plan["objects"]
                 if same_path(path, unit.get("base_path")) or same_path(path, unit.get("target_path"))]
        if len(slots) != 1:
            row.update(status="not_unique_link_input", error="Scoring object is absent or ambiguous in this link graph")
            continue
        obj, source = Path(best["compile_output"]), Path(best["candidate_path"])
        if (knowledge.digest(source.read_bytes()) != best["candidate_sha256"] or
                knowledge.digest(obj.read_bytes()) != verification.get("objects", {}).get("candidate", {}).get("sha256")):
            row.update(status="candidate_changed", error="Source or object no longer matches its certificate")
            continue
        row.update(candidate_sha256=best["candidate_sha256"], object_sha256=knowledge.digest(obj.read_bytes()),
                   replacement_slot=slots[0], prior_object_evidence_sha256=knowledge.digest(knowledge.encoded(verification)))
        destination = workspace / slots[0]
        original = destination.read_bytes()
        try:
            shutil.copyfile(obj, destination)
            if knowledge.digest(destination.read_bytes()) != row['object_sha256']:
                raise ValueError("Candidate object changed during link injection")
            checkpoint("candidate_link", baseline, item["symbol"])
            with coordinator.build("Candidate checksum gate", "Isolated link: " + item["symbol"]):
                measured = link_once(workspace, plan, output / item["target_id"])
            row.update(measured, linked_checksum=measured["passed"])
            if knowledge.digest(source.read_bytes()) != row['candidate_sha256']:
                raise ValueError("Candidate source changed during checksum testing")
        finally:
            destination.write_bytes(original)
        checkpoint("candidate_complete", baseline)
    unchanged = all(knowledge.digest((workspace / item["slot"]).read_bytes()) == item["sha256"] for item in plan["files"])
    if not unchanged:
        raise ValueError("Isolated baseline inputs changed after candidate links")
    return checkpoint("complete", baseline, finished=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--recovery', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', help='linked ELF as Ninja names it (default: build/<VERSION>/main.elf)')
    parser.add_argument('--checksum-file', help='default: config/<VERSION>/build.sha1')
    parser.add_argument('--context', choices=('native', 'retail-reference'), default='retail-reference')
    args = parser.parse_args()
    if args.target is None or args.checksum_file is None:
        if not VERSION:
            parser.error('no project version configured; pass --target and --checksum-file or set DECOMP_VERSION')
        # Ninja graphs use the host's separator style.
        args.target = args.target or str(Path('build', VERSION, 'main.elf'))
        args.checksum_file = args.checksum_file or str(Path('config', VERSION, 'build.sha1'))
    existed = args.output.exists()
    try:
        summary = run(args.root.resolve(), args.recovery.resolve(), args.output.resolve(), args.target,
                      args.checksum_file, args.context)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if not existed and args.output.is_dir():
            previous = args.output / 'results.json'
            data = json.loads(previous.read_text(encoding="utf-8")) if previous.is_file() else {'summary': {}, 'results': []}
            summary = {**data['summary'], 'phase': 'failed', 'finished': True, 'ready': 0, 'promoted': 0,
                       'error': f'{type(error).__name__}: {error}', 'context': args.context,
                       'project': str(args.root.resolve()), 'recovery': str(args.recovery.resolve()),
                       'updated_at': datetime.now(UTC).isoformat()}
            decompile.write_json(args.output / 'manifest.json', summary)
            decompile.write_json(previous, {'summary': summary, 'results': data['results']})
        print(f'{type(error).__name__}: {error}')
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary['phase'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())

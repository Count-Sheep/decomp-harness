"""Build an unchanged project and a proposed unit change in a fresh workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import harness_paths
from m2c_capture_inputs import Inputs
from m2c_unit_proposal import destination, head
from m2c_unit_queue import inspect_record
import portable

GITLINK_MARKER = "\0gitlink:"


def configure_command(root: Path, version: str) -> list[str]:
    """``configure.py`` with the project's already-downloaded tools, so the build stays offline.

    Paths are relative to ``root`` (the command also runs in captured copies of it). Tools that
    are absent are left to the project's own defaults; the compiler wrapper is passed only where
    this host needs one.
    """
    root = root.resolve()
    command = [harness_paths.python_command(), "configure.py", "--version", version, "--no-progress"]

    def relative(path: Path) -> str:
        path = Path(path)
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return str(path)

    for flag, path in (("--dtk", harness_paths.build_tool(root, "dtk")),
                       ("--objdiff", harness_paths.build_tool(root, "objdiff-cli")),
                       ("--compilers", root / "build" / "compilers"),
                       ("--binutils", root / "build" / "binutils")):
        if path.exists():
            command += [flag, relative(path)]
    wrapper = harness_paths.compiler_wrapper(root)
    if len(wrapper) == 1:
        command += ["--wrapper", relative(Path(wrapper[0]))]
    sjiswrap = root / "build" / "tools" / "sjiswrap.exe"
    if sjiswrap.exists():
        command += ["--sjiswrap", relative(sjiswrap)]
    return command


def execute(command: list[str], workspace: Path, log: Path, timeout: int = 300) -> None:
    process = subprocess.Popen(command, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               **portable.popen_group_kwargs())
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        portable.kill_tree(process.pid)
        output, _ = process.communicate()
        log.write_text(output, encoding="utf-8")
        raise ValueError("Project build timed out: " + str(log))
    log.write_text(output, encoding="utf-8")
    if process.returncode:
        raise ValueError("Project command failed: " + str(log) + "\n" + output[-1500:])


def project_paths(root: Path) -> list[str]:
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    names = {value.decode() for value in files.split(b"\0") if value}
    expanded = set()
    for name in names:
        path = root / name
        if not path.is_dir():
            expanded.add(name)
            continue
        entry = subprocess.run(["git", "ls-files", "-s", "--", name], cwd=root,
                               capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        fields = entry.split(maxsplit=3)
        if len(fields) != 4 or fields[0] != "160000" or fields[3] != name:
            raise ValueError("Tracked directory is not a supported gitlink: " + name)
        expected = fields[1]
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path,
                                         text=True, timeout=10).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=path,
                                        text=True, timeout=10)
        if actual != expected or dirty:
            raise ValueError("Gitlink must be initialized, clean and at its recorded commit: " + name)
        children = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=path)
        children = [value.decode() for value in children.split(b"\0") if value]
        if not children:
            raise ValueError("Gitlink has no tracked files: " + name)
        # NUL cannot occur in a Git pathname, so this state marker cannot
        # collide with a real project file that capture would otherwise skip.
        expanded.add(f"{GITLINK_MARKER}{name}:{expected}")
        expanded.update(f"{name}/{child}" for child in children)
    return sorted(expanded)


def capture(root: Path, workspace: Path, version: str, inputs: Inputs) -> list[str]:
    import yaml
    root, workspace = root.resolve(), workspace.resolve()
    sources = project_paths(root)
    names = set(sources)
    config = yaml.safe_load(inputs.read(root / f"config/{version}/config.yml"))
    if (not isinstance(config, dict) or config.get("modules") or config.get("name", "main") != "main"
            or not isinstance(config.get("object"), str) or Path(config["object"]).suffix != ".dol"):
        raise ValueError("Native project trial requires a main-only retail input adapter")
    retail = Path(config["object_base"]) / config["object"]
    names.add(retail.as_posix())
    for folder in ("build/tools", "build/compilers", "build/binutils"):
        for path in (root / folder).rglob("*"):
            if path.is_file() or path.is_symlink():
                names.add(path.relative_to(root).as_posix())
    for name in sorted(names):
        if name.startswith(GITLINK_MARKER):
            continue
        source = destination(root, name)
        if not source.is_file():
            raise ValueError("Missing snapshot input: " + name)
        raw = inputs.read(source)
        target = destination(workspace, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.run(["/bin/cp", "-c", "-p", str(source), str(target)], check=True, capture_output=True)
        else:
            shutil.copy2(source, target)
        if target.read_bytes() != raw:
            raise ValueError("Snapshot copy changed: " + name)
    inputs.verify()
    if project_paths(root) != sources:
        raise ValueError("Project file set changed during snapshot")
    return sources


def run(root: Path, proposal: Path, output: Path) -> dict:
    import yaml
    import local_campaign_link_gate as gate
    from permuter import owner_extract
    from m2c_unit_evidence import compare_objects

    root, proposal, output = root.resolve(), proposal.absolute(), output.absolute()
    if output.exists() or output.is_relative_to(root):
        raise ValueError("Use a fresh trial directory outside the project")
    evidence = inspect_record(proposal, root)
    if evidence["state"] != "proposal_review_required" or not evidence["link_verified"]:
        raise ValueError("Project trial requires a current verified proposal")
    inputs = Inputs()
    record = json.loads(inputs.read(proposal))
    inputs.files.update(record["inputs"])
    inputs.verify()
    inputs.read(Path(record["link_record"]))
    plan = json.loads(inputs.read(Path(record["link_record"]).parent / "plan.json"))
    version = Path(plan["elf"]).parts[1]
    if not version.isalnum():
        raise ValueError("Invalid build version")
    workspace = output / "workspace"
    workspace.mkdir(parents=True)
    result = {"kind": "unit_project_trial", "unit": record["unit"], "project_root": str(root),
              "proposal": str(proposal), "base_commit": record["base_commit"], "state": "invalid",
              "proposal_build_verified": False, "ready_to_merge": False, "commands": []}
    configure = configure_command(root, version)

    def build(phase: str) -> dict:
        for label, command in (("configure", configure), ("build", ["ninja", f"build/{version}/ok"])):
            result["commands"].append({"phase": phase, "command": command})
            execute(command, workspace, output / f"{phase}-{label}.log")
        dol = workspace / plan["dol"]
        raw = dol.read_bytes()
        actual = hashlib.sha1(raw).hexdigest()
        if actual != plan["expected_sha1"]:
            raise ValueError("Native project checksum mismatch: " + phase)
        artifact = output / f"{phase}.dol"
        shutil.copy2(dol, artifact)
        inputs.read(artifact)
        return {"passed": True, "sha1": actual, "dol": str(artifact)}

    try:
        result["project_paths"] = capture(root, workspace, version, inputs)
        result["baseline"] = build("baseline")
        inputs.verify()
        if head(root) != record["base_commit"]:
            raise ValueError("Project base changed during baseline")
        execute(["git", "apply", "--check", record["patch"]], workspace, output / "patch-check.log")
        execute(["git", "apply", record["patch"]], workspace, output / "patch-apply.log")
        for change in record["changes"]:
            if destination(workspace, change["destination"]).read_bytes() != inputs.read(Path(change["payload"])):
                raise ValueError("Applied payload differs from proposal")
        for name in (plan["elf"], plan["dol"], f"build/{version}/ok"):
            (workspace / name).unlink(missing_ok=True)
        result["candidate"] = build("candidate")
        current = gate.discover(workspace, plan["elf"], plan["checksum_file"], "native")
        owner = current["units"][record["unit"]]
        if owner.get("base_path") not in current["objects"]:
            raise ValueError("Proposed source object is not linked by native project")
        obj = workspace / owner["base_path"]
        expected = root / owner["target_path"]
        inputs.read(expected)
        verified = compare_objects(owner_extract.read_elf(expected), owner_extract.read_elf(obj))
        result["object_evidence"] = verified
        if not verified["object_verified"]:
            raise ValueError("Native project object differs from original owner")
        shutil.copy2(obj, output / "candidate.o")
        inputs.read(output / "candidate.o")
        for name in ("objdiff.json", "build.ninja"):
            shutil.copy2(workspace / name, output / ("candidate-" + name))
            inputs.read(output / ("candidate-" + name))
        inputs.verify()
        if (head(root) != record["base_commit"] or project_paths(root) != result["project_paths"]
                or not inspect_record(proposal, root)["link_verified"]):
            raise ValueError("Proposal changed during native build")
        result.update(state="proposal_build_verified", proposal_build_verified=True)
    except (OSError, ValueError, RuntimeError, KeyError, yaml.YAMLError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    for path in output.glob("*.log"):
        inputs.read(path)
    result["inputs"] = inputs.files
    record_path = output / "result.json"
    record_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    from m2c_unit_queue import publish
    publish(root, version, record_path)
    return result


def inspect_trial(record: dict, root: Path) -> dict:
    if Path(record["project_root"]).resolve() != root.resolve() or head(root) != record["base_commit"]:
        raise ValueError("Native trial project or base commit changed")
    if record.get("proposal_build_verified") and project_paths(root) != record["project_paths"]:
        raise ValueError("Native trial project file set changed")
    proposal = Path(record["proposal"])
    proposal_record = json.loads(proposal.read_bytes())
    if proposal_record.get("kind") != "unit_proposal" or proposal_record["unit"] != record["unit"]:
        raise ValueError("Native trial references a different proposal owner")
    evidence = inspect_record(proposal, root)
    if evidence["state"] != "proposal_review_required" or not evidence["link_verified"]:
        raise ValueError("Native trial proposal is stale")
    if not record.get("proposal_build_verified"):
        return {"state": record["state"], "proposal_build_verified": False,
                "object_verified": False, "link_verified": False, "ready_to_merge": False,
                "error": record.get("error")}
    if not record["object_evidence"]["object_verified"]:
        raise ValueError("Native trial lacks whole-object evidence")
    plan = json.loads((Path(proposal_record["link_record"]).parent / "plan.json").read_bytes())
    sha1 = plan["expected_sha1"]
    for phase in ("baseline", "candidate"):
        result = record[phase]
        dol = Path(result["dol"])
        if result["passed"] is not True or hashlib.sha1(dol.read_bytes()).hexdigest() != result["sha1"]:
            raise ValueError("Native build output changed: " + phase)
        if result["sha1"] != sha1:
            raise ValueError("Baseline and proposed build differ")
    return {"state": "proposal_build_verified", "proposal_build_verified": True,
            "object_verified": True, "link_verified": True, "ready_to_merge": False,
            "proposal": str(proposal), "base_commit": record["base_commit"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.root, args.proposal, args.output)
    print(json.dumps({key: result.get(key) for key in ("unit", "state", "proposal_build_verified", "error")}))


if __name__ == "__main__":
    main()

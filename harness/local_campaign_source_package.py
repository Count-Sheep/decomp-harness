"""Export and reverify real owner-only C patches; never install or approve them."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import UTC, datetime
import difflib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import local_campaign as campaign
import local_campaign_decompile as decompile
import local_campaign_decompile_validate as validator
import local_campaign_knowledge as knowledge
import local_campaign_link_gate as link
from harness_paths import SELECTED, setting
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_recovery import object_evidence

OBJECT_AXES = ("raw_candidate_bytes", "relocations", "owner_object")


def read_source(path: Path) -> str:
    return path.read_bytes().decode()


def owner_path(value: str) -> Path:
    path = link.relative_path(value)
    if (not value.startswith("src/") or path.suffix != ".c"
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)):
        raise ValueError("Source packages require a root-relative C owner under src/")
    return path


def replace_function(original: str, candidate: str, symbol: str, signature: str) -> str:
    from permuter import owner_source

    start, end = campaign.function_span(candidate, symbol)
    owner_source.validate_definition_only(candidate, start, end)
    if campaign.function_signature(candidate, start, end) != signature:
        raise ValueError("Exported candidate changes the required signature")
    rejected, flags = campaign.policy("", candidate)
    if rejected or flags or re.search(r"\b(?:M2C_\w+|saved_reg_\w+|__builtin_\w+)\b", campaign.source_mask(candidate)):
        raise ValueError("Exported C contains a forbidden helper or failed source screening")
    first, last = campaign.function_span(original, symbol)
    if campaign.function_signature(original, first, last) != signature:
        raise ValueError("Owner signature no longer matches the frozen contract")
    newline = "\r\n" if "\r\n" in original else "\n"
    definition = candidate[start:end].replace("\r\n", "\n").replace("\n", newline)
    return original[:first] + definition + original[last:]


def source_diff(original: dict[str, str], modified: dict[str, str]) -> str:
    if set(original) != set(modified):
        raise ValueError("A source package cannot add or remove owners")
    lines = []
    for name in sorted(original):
        owner_path(name)
        if original[name] == modified[name]:
            continue
        lines.append(f"diff --git a/{name} b/{name}\n")
        for line in difflib.unified_diff(original[name].splitlines(keepends=True),
                                         modified[name].splitlines(keepends=True),
                                         fromfile="a/" + name, tofile="b/" + name):
            lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def apply_patch_private(original: dict[str, str], modified: dict[str, str], patch: Path, destination: Path) -> dict:
    destination.mkdir()
    for name, text in original.items():
        path = destination / owner_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    expected = source_diff(original, modified)
    if patch.read_bytes() != expected.encode():
        raise ValueError("Exported patch no longer matches its assigned owner-only changes")
    # A minimal environment; Windows processes also need their system locations.
    keys = ("PATH", "HOME", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE", "PATHEXT", "COMSPEC")
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    environment.update(LANG="C", LC_ALL="C", GIT_CEILING_DIRECTORIES=str(destination.parent.resolve()))
    commands = []
    if expected:
        for extra in (["--check"], []):
            command = ["git", "-c", "core.autocrlf=false", "-c", "core.whitespace=trailing-space,space-before-tab,cr-at-eol",
                       "apply", *extra, "--whitespace=error", str(patch.resolve())]
            result = subprocess.run(command, cwd=destination, env=environment, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
            commands.append({"command": command, "returncode": result.returncode, "diagnostics": result.stdout + result.stderr})
            if result.returncode:
                raise ValueError("Exported patch failed private git apply: " + result.stderr)
    actual = {path.relative_to(destination).as_posix(): read_source(path)
              for path in destination.rglob("*") if path.is_file()}
    if actual != modified:
        raise ValueError("Private patch application differs from the intended owner-only source")
    return {"passed": True, "no_change": not bool(expected), "commands": commands,
            "patch_sha256": knowledge.digest(patch.read_bytes()),
            "owners": {name: {"before_sha256": knowledge.digest(original[name]),
                              "after_sha256": knowledge.digest(actual[name])} for name in actual}}


def load_policy(path: Path):
    spec = importlib.util.spec_from_file_location("source_package_policy_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise ValueError("Project source policy cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_policy() -> dict:
    """The project's own source-quality hooks, from the ``source_policy`` setting.

    Set on the project entry (or top level) of the harness config, paths relative to the
    policy root (the project checkout)::

        "source_policy": {
            "quality_scan": ".github/scripts/quality_scan.py",
            "wrapper_scan": ".github/scripts/check_asm_wrappers.py",
            "attest": ["AGENTS.md", ".github/workflows/quality.yml"]
        }

    ``quality_scan`` must provide ``added_lines_from_diff(patch) -> {path: lines}`` and
    ``scan_source(path, text, lines) -> bool``; ``wrapper_scan`` must provide
    ``scan(path) -> [(line, text), ...]`` of offending lines. ``attest`` files are hashed into the
    run so a policy change invalidates its results. Every entry is optional.
    """
    value = SELECTED.settings.get("source_policy")
    if value is None:
        value = setting("source_policy", default={})
    if not isinstance(value, dict):
        raise ValueError("source_policy must be an object")
    return value


def policy_files(policy_root: Path) -> list[Path]:
    """Configured policy scripts and attested documents, all hashed into a package run."""
    policy = source_policy()
    names = [policy[key] for key in ("quality_scan", "wrapper_scan") if policy.get(key)]
    names.extend(policy.get("attest") or [])
    return [policy_root / link.relative_path(str(name)) for name in names]


def ci_quality(root: Path, policy_root: Path, modified: dict[str, str], applied: Path, patch: str) -> dict:
    policy = source_policy()
    log = io.StringIO()
    passed, offenders, count = True, [], 0
    with redirect_stdout(log):
        if policy.get("quality_scan"):
            quality = load_policy(policy_root / link.relative_path(policy["quality_scan"]))
            added = quality.added_lines_from_diff(patch)
            if set(added) != {name for name in modified if f"+++ b/{name}\n" in patch}:
                raise ValueError("Quality scan cannot map the exported diff")
            passed = all(quality.scan_source(name, modified[name], lines) for name, lines in added.items())
        if policy.get("wrapper_scan"):
            wrappers = load_policy(policy_root / link.relative_path(policy["wrapper_scan"]))
            for path in sorted((root / "src").rglob("*")):
                if path.is_file() and path.suffix in {".c", ".cpp"}:
                    name = path.relative_to(root).as_posix()
                    effective = applied / name if name in modified else path
                    count += 1
                    offenders.extend({"path": name, "line": line, "text": text} for line, text in wrappers.scan(effective))
    configured = bool(policy.get("quality_scan") or policy.get("wrapper_scan"))
    return {"passed": passed and not offenders, "diff_scan_passed": passed,
            "full_tree_wrapper_scan_count": count, "wrapper_offenders": offenders, "diagnostics": log.getvalue(),
            "policy_configured": configured,
            "scope": ("actual_project_diff_quality_and_full_tree_live_inc_wrapper_checks_not_human_review"
                      if configured else "no_project_source_policy_configured_not_human_review")}


def target_ast_errors(node: dict) -> list[str]:
    errors = []

    def visit(current):
        kind = current.get("kind")
        if kind == "CallExpr":
            callee = next(iter(current.get("inner", [])), {})
            spelling = callee.get("type", {}).get("qualType", "")
            if not spelling or re.search(r"\(\s*\)", spelling):
                errors.append("Call without a known prototype: " + spelling)
        if kind in {"CompoundLiteralExpr", "StmtExpr"}:
            errors.append("Non-C89 target construct: " + kind)
        if kind in {"GCCAsmStmt", "MSAsmStmt"}:
            errors.append("Assembly in the expanded target body is not recovered C: " + kind)
        if kind in {"VarDecl", "FieldDecl"} and re.search(r"\[0\]", current.get("type", {}).get("qualType", "")):
            errors.append("Zero-length target array is not portable C89")
        children = current.get("inner", [])
        if kind == "ForStmt" and children and children[0].get("kind") == "DeclStmt":
            errors.append("For-loop declaration is not C89")
        for child in current.get("inner", []):
            visit(child)

    visit(node)
    return sorted(set(errors))


def analyze_source(context: str, symbol: str, folder: Path) -> dict:
    from permuter import owner_source

    parser = shutil.which("clang")
    if not parser:
        raise ValueError("Clang source diagnostics unavailable")
    start, end = owner_source._definition_span(context, symbol)
    pruned = owner_source._pruned_ast_context(context, symbol, context[start:end])
    if not pruned.endswith("\n"):
        pruned += "\n"  # -Wall -Werror rejects a missing final newline on newer Clang
    source = folder / "analysis.c"
    source.write_text(pruned, encoding="utf-8", newline="\n")
    # C99 parses the project's existing inline/header extensions; the assigned
    # body is additionally checked for C89 declarations and AST-only constructs.
    command = [parser, "--target=powerpc-unknown-eabi", "-x", "c", "-std=c99", "-pedantic-errors",
               "-nostdinc", "-fno-builtin", "-Wall", "-Werror", "-Werror=declaration-after-statement",
               "-Wconditional-uninitialized", "-Wno-unknown-pragmas", "-Wno-typedef-redefinition",
               "-Wno-deprecated-non-prototype", "-Wno-strict-prototypes", "-Wno-zero-length-array",
               "-fsyntax-only", "-Xclang", "-ast-dump=json", str(source)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=30)
    (folder / "clang.log").write_text(result.stderr, encoding="utf-8", newline="\n")
    evidence = {"passed": False, "command": command, "returncode": result.returncode,
                "parser_sha256": knowledge.digest(Path(parser).resolve().read_bytes()),
                "analysis_sha256": knowledge.digest(pruned), "diagnostics": result.stderr[-6000:], "errors": []}
    if result.returncode:
        return evidence
    ast = json.loads(result.stdout)
    targets = [node for node in ast.get("inner", []) if node.get("kind") == "FunctionDecl"
               and node.get("name") == symbol and any(child.get("kind") == "CompoundStmt" for child in node.get("inner", []))]
    if len(targets) != 1:
        raise ValueError("Source diagnostics did not parse exactly one assigned definition")
    target = targets[0]
    decompile.write_json(folder / "target.ast.json", target)
    evidence.update(errors=target_ast_errors(target), target_ast_sha256=knowledge.digest(knowledge.encoded(target)))
    evidence["passed"] = not evidence["errors"]
    return evidence


def capture_capsule(prior: Path, destination: Path, project: Path) -> dict:
    summary = json.loads((prior / "manifest.json").read_text(encoding="utf-8"))
    plan_bytes = (prior / "plan.json").read_bytes()
    if (not summary.get("finished") or summary.get("phase") != "complete" or not summary.get("baseline", {}).get("passed")
            or knowledge.digest(plan_bytes) != summary.get("plan_sha256")):
        raise ValueError("Link capsule is incomplete or no longer matches its recorded plan")
    plan = json.loads(plan_bytes)
    if Path(summary["project"]).resolve() != project.resolve():
        raise ValueError("Explicit checksum/policy project differs from the recorded link project")
    command = plan["link_command"]
    prefix = command[:-1]
    checked = link.parse_commands(
        [{"output": plan["elf"], "command": " ".join(map(shlex_quote, command))},
         {"output": plan["dol"], "command": " ".join(map(shlex_quote, plan["conversion_command"]))}],
        [{"output": plan["elf"], "command": " ".join(map(shlex_quote, [*prefix, *plan["objects"]]))}], plan["elf"])
    if any(checked[key] != plan[key] for key in checked):
        raise ValueError("Link capsule command does not agree with its graph")
    checksum = project / link.relative_path(plan["checksum_file"])
    if (knowledge.digest(checksum.read_bytes()) != plan["checksum_file_sha256"]
            or link.expected_checksum(checksum.read_text(encoding="utf-8"), plan["dol"]) != plan["expected_sha1"]):
        raise ValueError("Link capsule target checksum does not agree with the frozen project")
    slots = [item["slot"] for item in plan["files"]]
    if len(set(slots)) != len(slots) or set(slots) != {*plan["objects"], *plan["tools"], plan["linker_script"]}:
        raise ValueError("Link capsule has incomplete or duplicate inputs")
    for item in plan["files"]:
        path = prior / "workspace" / link.relative_path(item["slot"])
        data = path.read_bytes()
        if knowledge.digest(data) != item["sha256"]:
            raise ValueError("Historical link capsule input changed: " + item["slot"])
        target = destination / item["slot"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if knowledge.digest(target.read_bytes()) != item["sha256"] or knowledge.digest(path.read_bytes()) != item["sha256"]:
            raise ValueError("Link capsule input changed during capture")
    return plan


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)


def candidates(runs: list[Path]) -> tuple[list[dict], list[dict]]:
    inputs, bindings = [], []
    for run in runs:
        raw = (run / "results.json").read_bytes()
        data = json.loads(raw)
        summary = data["summary"]
        if not summary.get("finished") or not summary.get("environment_unchanged"):
            raise ValueError("Source packages require terminal runs with unchanged inputs")
        bindings.append({"path": str(run.resolve()), "sha256": knowledge.digest(raw),
                         "workspace_sha256": summary["workspace_sha256"]})
        for item in data["results"]:
            measures = [(key, item[key]) for key in ("best", "measurement", "off", "on") if key in item]
            for origin, measured in measures:
                if not measured.get("compile_ok") or measured.get("pct") != 100:
                    continue
                path = Path(measured["candidate_path"]) if measured.get("candidate_path") else (
                    run / item["target_id"] / origin / "draft.c" if origin in {"off", "on"}
                    else run / item["target_id"] / "draft.c")
                candidate = read_source(path)
                if knowledge.digest(candidate) != measured.get("draft_sha256"):
                    raise ValueError("Prior measured candidate changed: " + str(path))
                inputs.append({**{key: item[key] for key in ("target_id", "symbol", "source", "unit", "signature")},
                               "origin": origin, "candidate": candidate, "run": str(run.resolve()),
                               "candidate_sha256": knowledge.digest(candidate),
                               "package_id": knowledge.digest(knowledge.encoded([knowledge.digest(raw), item["target_id"], origin]))})
    if not inputs:
        raise ValueError("No exact-scoring compiled source candidates")
    return inputs, bindings


def compile_files(root: Path, source: str, owner: str, files: dict[str, str], symbol: str,
                  command: str, folder: Path) -> dict:
    folder.mkdir(parents=True)
    staged = validator.stage_owner(root, source, owner, files, symbol, None, folder / "overlay")
    if read_source(folder / "overlay" / owner) != files[owner]:
        raise ValueError("Compiler overlay differs from the actual applied owner patch")
    objects = folder / "objects"
    objects.mkdir()
    includes = [(root / source).parent]
    if source != owner:
        includes.append((root / owner).parent)
    result, obj, arguments = validator.run_compile(command, staged, objects, includes)
    (folder / "compile.log").write_text(result.stdout + result.stderr, encoding="utf-8", newline="\n")
    return {"compile_ok": result.returncode == 0 and obj.is_file(), "returncode": result.returncode,
            "object": str(obj), "source": str(staged), "command": arguments,
            "error": (result.stdout + result.stderr)[-6000:]}


def source_files(root: Path, source: str, symbol: str) -> tuple[str, dict[str, str]]:
    owner, files = validator.snapshot_owner(root, source, symbol)
    owner_path(owner)
    return owner, {name: read_source(root / name) for name in files}


def run(root: Path, runs: list[Path], capsule: Path, policy_root: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Source package output already exists; preserve history with a new path")
    if root.resolve() != campaign.ROOT.resolve():
        raise ValueError("DECOMP_ROOT must point to the source-verification workspace")
    inputs, bindings = candidates(runs)
    workspace_hash = fingerprint(root)
    if any(binding["workspace_sha256"] != workspace_hash for binding in bindings):
        raise ValueError("Source candidates belong to a different or changed workspace")
    commands = subprocess.check_output(["ninja", "-t", "commands", "all_source"], cwd=root, text=True,
                                       encoding="utf-8", errors="replace", timeout=60)
    lookup = command_map(commands)
    units = {unit["name"]: unit for unit in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
    coordinator = Coordinator(policy_root / "build/local_llm_campaign")
    policy_paths = policy_files(policy_root)
    prior_plan = json.loads((capsule / "plan.json").read_text(encoding="utf-8"))
    policy_paths.append(policy_root / link.relative_path(prior_plan["checksum_file"]))
    from permuter import owner_source, owner_extract
    producers = [Path(__file__), Path(validator.__file__), Path(link.__file__), Path(owner_source.__file__),
                 Path(owner_extract.__file__), Path(campaign.__file__), Path(decompile.__file__)]
    attested = {str(path.resolve()): knowledge.digest(path.read_bytes()) for path in [*policy_paths, *producers]}
    configuration = {"schema_version": 1, "mode": "exported_source_patch_verification", "root": str(root.resolve()),
                     "policy_root": str(policy_root.resolve()), "workspace_sha256": workspace_hash,
                     "inputs": bindings, "attested_tools_and_policy": attested,
                     "link_capsule": str(capsule.resolve()),
                     "link_capsule_manifest_sha256": knowledge.digest((capsule / "manifest.json").read_bytes()),
                     "targets": [{key: value for key, value in item.items() if key != "candidate"} for item in inputs]}
    output.mkdir(parents=True, mode=0o700)
    decompile.write_json(output / "configuration.json", configuration)
    records, baselines = [], {}

    def checkpoint(phase, finished=False, **detail):
        summary = {**{key: configuration[key] for key in ("schema_version", "mode", "root", "workspace_sha256")},
                   "configuration_sha256": knowledge.digest(knowledge.encoded(configuration)), "phase": phase,
                   "finished": finished, "total": len(inputs), "processed": len(records),
                   "machine_verified_C": sum(row.get("machine_verified_C") is True for row in records),
                   "ready": 0, "promoted": 0, "updated_at": datetime.now(UTC).isoformat(), **detail}
        decompile.write_json(output / "manifest.json", summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": records})
        print(json.dumps({key: summary[key] for key in ("phase", "processed", "total", "machine_verified_C")}), flush=True)
        return summary

    try:
        checkpoint("capture_link_context")
        with coordinator.build("Source package", "Copy previously verified link inputs"):
            plan = capture_capsule(capsule, output / "link-workspace", policy_root)
            decompile.write_json(output / "link-plan.json", plan)
            baseline_link = link.link_once(output / "link-workspace", plan, output / "baseline-link")
        if not baseline_link["passed"]:
            raise ValueError("Independent captured baseline link failed its configured checksum")
        for item in inputs:
            checkpoint("source_package", current=item["symbol"])
            folder = output / "packages" / item["package_id"]
            folder.mkdir(parents=True)
            row = {key: value for key, value in item.items() if key != "candidate"}
            row.update(machine_verified_C=False, ready=False, policy_approved=None, human_review=None,
                       package=str(folder.resolve()), link_context=plan["context"], status="checking")
            records.append(row)
            try:
                candidate = item["candidate"]
                owner, files = source_files(root, item["source"], item["symbol"])
                modified_owner = replace_function(files[owner], candidate, item["symbol"], item["signature"])
                original = {owner: files[owner]}
                modified = {owner: modified_owner}
                row["owner"] = owner
                (folder / "function.c").write_bytes(candidate.encode())
                patch = source_diff(original, modified)
                (folder / "owner.patch").write_bytes(patch.encode())
                applied = folder / "applied"
                row["patch_application"] = apply_patch_private(original, modified, folder / "owner.patch", applied)
                row["ci_quality"] = ci_quality(root, policy_root, modified, applied, patch)
                if not row["ci_quality"]["passed"]:
                    raise ValueError("Project CI source-quality checks rejected the actual applied patch")
                applied_files = {**files, owner: read_source(applied / owner)}
                command = lookup[item["source"]]
                baseline_key = knowledge.digest(knowledge.encoded([files, command]))
                with coordinator.build("Source package", "Compile exported C: " + item["symbol"]):
                    if baseline_key not in baselines:
                        baselines[baseline_key] = compile_files(root, item["source"], owner, files, item["symbol"],
                                                              command, output / "baselines" / baseline_key)
                    baseline = baselines[baseline_key]
                    if not baseline["compile_ok"]:
                        raise ValueError("Unchanged full owner cannot compile: " + baseline["error"])
                    compiled = compile_files(root, item["source"], owner, applied_files, item["symbol"], command, folder / "compile")
                    if not compiled["compile_ok"]:
                        raise ValueError("Exported helper-free C cannot compile: " + compiled["error"])
                    unit = units[item["unit"]]
                    measured = validator.score_object(root, root / unit["target_path"], Path(compiled["object"]), item["symbol"], folder)
                    measured.update(compile_ok=True, compile_output=compiled["object"], draft_sha256=knowledge.digest(candidate),
                                    compile_input=compiled["source"], baseline=baseline, scope="full_owner_scoring_unit", artifacts=str(folder))
                    row["measurement"] = measured
                    evidence = object_evidence(root, item, candidate, "", measured, unit)
                    row["object_evidence"] = evidence
                    if not all(evidence.get(axis) is True for axis in (*OBJECT_AXES, "policy_screen_passed")):
                        raise ValueError("Actual exported C failed byte, relocation, sibling or source checks")
                    pp = decompile.preprocess(root, command, compiled["source"])
                    if pp["status"] != "ok":
                        raise ValueError("Actual exported C cannot be preprocessed for source diagnostics")
                    row["source_diagnostics"] = analyze_source(pp["context"], item["symbol"], folder)
                if not row["source_diagnostics"]["passed"]:
                    raise ValueError("Target source diagnostics failed: " + knowledge.encoded(row["source_diagnostics"]))
                plan_unit = plan["units"].get(item["unit"], {})
                if any(unit.get(key) != plan_unit.get(key) for key in ("base_path", "target_path")):
                    raise ValueError("Scoring unit disagrees with the captured link graph")
                slots = [slot for slot in plan["objects"]
                     if link.same_path(slot, unit.get("base_path")) or link.same_path(slot, unit.get("target_path"))]
                if len(slots) != 1:
                    raise ValueError("Assigned owner is not one unique link input")
                slot = output / "link-workspace" / slots[0]
                original_object = slot.read_bytes()
                object_bytes = Path(compiled["object"]).read_bytes()
                if knowledge.digest(object_bytes) != evidence["objects"]["candidate"]["sha256"]:
                    raise ValueError("Compiled candidate object changed before linking")
                try:
                    slot.write_bytes(object_bytes)
                    with coordinator.build("Source package", "Link actual exported C: " + item["symbol"]):
                        row["linked"] = link.link_once(output / "link-workspace", plan, output / ("link-" + item["package_id"]))
                finally:
                    slot.write_bytes(original_object)
                row["replacement_slot"] = slots[0]
                row["linked_checksum"] = row["linked"]["passed"]
                if not row["linked_checksum"]:
                    raise ValueError("Actual exported source object failed the configured linked checksum")
                row["machine_verified_C"] = True
                row["status"] = "machine_verified_review_required"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row.update(status="rejected", error=f"{type(error).__name__}: {error}")
            decompile.write_json(folder / "package.json", row)
            checkpoint("candidate_complete")
        # Seal the exported source and the actual applied/compiled owner, not
        # just a source hash copied from an earlier diagnostic run.
        for row in records:
            if not row.get("machine_verified_C"):
                continue
            folder = Path(row["package"])
            owner = row["owner"]
            applied_hash = row["patch_application"]["owners"][owner]["after_sha256"]
            artifacts = {"function.c": row["candidate_sha256"], "owner.patch": row["patch_application"]["patch_sha256"],
                         "applied/" + owner: applied_hash, "compile/overlay/" + owner: applied_hash,
                         "analysis.c": row["source_diagnostics"]["analysis_sha256"]}
            obj = Path(row["measurement"]["compile_output"])
            artifacts[obj.relative_to(folder).as_posix()] = row["object_evidence"]["objects"]["candidate"]["sha256"]
            if any(knowledge.digest((folder / name).read_bytes()) != sha for name, sha in artifacts.items()):
                raise ValueError("Exported/applied source or its compiled object changed before sealing")
            row["artifact_hashes"] = artifacts
            row["artifacts_sealed"] = True
            decompile.write_json(folder / "package.json", row)
        if (fingerprint(root) != workspace_hash
                or any(knowledge.digest(Path(path).read_bytes()) != sha for path, sha in attested.items())
                or any(knowledge.digest((Path(binding["path"]) / "results.json").read_bytes()) != binding["sha256"] for binding in bindings)
                or knowledge.digest((capsule / "manifest.json").read_bytes()) != configuration["link_capsule_manifest_sha256"]
                or any(knowledge.digest((output / "link-workspace" / f["slot"]).read_bytes()) != f["sha256"] for f in plan["files"])):
            raise ValueError("Source, verifier, history, policy or isolated link inputs changed during verification")
        return checkpoint("complete", finished=True, environment_unchanged=True,
                          baseline_link=baseline_link, link_context=plan["context"])
    except Exception as error:
        for row in records:
            row.update(machine_verified_C=False, status="run_invalidated", run_error=str(error))
            decompile.write_json(Path(row["package"]) / "package.json", row)
        checkpoint("failed", finished=True, environment_unchanged=False, error=f"{type(error).__name__}: {error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--policy-root", type=Path, required=True)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--link-capsule", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run(args.root.resolve(), args.run, args.link_capsule.resolve(), args.policy_root.resolve(), args.output.resolve())
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Import a frozen DTK workspace and existing m2c observations without building anything."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

from harness_paths import PROJECT_ID, REPO_ROOT, SELECTED, VERSION, tool_path, venv_python
from local_campaign_knowledge import DEFAULT_DATABASE, FEATURE_VERSION, KnowledgeStore, UNKNOWN_VERIFICATION, digest, encoded


def simindex_root(root: Path) -> Path:
    """The optional, project-provided ``simindex_lib`` package (``tools.simindex`` overrides it)."""
    return tool_path("simindex", root / "tools/decomp_work/simindex")


def analysis_assembly(root: Path, python: str, version: str | None = None) -> list[dict]:
    """Use the project's existing DTK parser/normalizer in its optional analysis environment."""
    adapter = r'''
import hashlib, json, pathlib, re, sys
root, library, version = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, library)
import simindex_lib as lib
header = re.compile(r"(?m)^\.fn\s+([^,\s]+),[^\n]*\n(.*?)^\.endfn\b", re.S)
octets = re.compile(r"^/\* [0-9A-F]{8} [0-9A-F]{8}\s+((?:[0-9A-F]{2} ){3}[0-9A-F]{2}) \*/")
rows = []
asm_root = root / "build" / version / "asm"
for path in sorted(asm_root.rglob("*.s")):
    original = path.read_text(errors="replace", encoding="utf-8")
    bodies = {m.group(1): m.group(2) for m in header.finditer(original)}
    for fn in lib.parse_dtk_asm(path):
        raw = b"".join(bytes.fromhex(m.group(1)) for line in bodies.get(fn["name"], "").splitlines()
                       if (m := octets.match(line)))
        fn.update(asm_path=path.relative_to(asm_root).as_posix(),
                  normalized=lib.normalize(fn["insns"], fn["labels"]),
                  bytes_hash=hashlib.sha256(raw).hexdigest() if len(raw) == fn["size"] and raw else None)
        rows.append(fn)
print(json.dumps(rows))
'''
    library = simindex_root(root)
    if not (library / "simindex_lib.py").is_file():
        raise ValueError(f"Assembly analysis needs the project's simindex_lib.py (set tools.simindex): {library}")
    result = subprocess.run([python, "-c", adapter, str(root), str(library), version or VERSION],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=180)
    return json.loads(result.stdout)


def safe_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Evidence path escapes frozen workspace: {name}")
    return path


def import_snapshot(store: KnowledgeStore, root: Path, python: str,
                    assembly: list[dict] | None = None, commands: dict | None = None,
                    check_fingerprint=None, version: str | None = None) -> dict:
    import local_campaign as campaign
    import local_campaign_decompile_snapshot as frozen
    from local_campaign_decompile import split_command
    from local_campaign_decompile_batch import command_map, targets
    from local_campaign_templates import core_shape

    root = root.resolve()
    manifest = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
    version = version or manifest.get("version") or VERSION
    if not version:
        raise ValueError("No project version: pass one or configure the project")
    check_fingerprint = check_fingerprint or (lambda path: frozen.fingerprint(path, version))
    if not manifest.get("stable") or check_fingerprint(root) != manifest.get("sha256"):
        raise ValueError("Import requires an unchanged, stable frozen workspace")
    report_bytes = (root / "build" / version / "report.json").read_bytes()
    report = json.loads(report_bytes)
    metadata = json.loads((root / "objdiff.json").read_text(encoding="utf-8"))
    units = {unit["name"]: unit for unit in report["units"]}
    if commands is None:
        result = subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                check=True, timeout=60)
        commands = command_map(result.stdout)
    assembly = analysis_assembly(root, python, version) if assembly is None else assembly
    indexed = {}
    for row in assembly:
        key = (row["asm_path"], row["name"], row["addr"])
        if key in indexed:
            raise ValueError("Ambiguous target assembly identity")
        indexed[key] = row
    normalizer = simindex_root(root) / "simindex_lib.py"
    # Generic PowerPC facts; the ABI label is a per-project setting.
    provenance = {"project": PROJECT_ID, "version": version, "architecture": "ppc750", "endianness": "big",
                  "abi": str(SELECTED.settings.get("abi") or "ppc-eabi"),
                  "workspace": str(root), "source_root": manifest.get("source_root"),
                  "source_commit": manifest.get("source_commit"), "snapshot_sha256": manifest["sha256"],
                  "report_sha256": digest(report_bytes), "feature_version": FEATURE_VERSION,
                  "normalizer_sha256": digest(normalizer.read_bytes()) if normalizer.is_file() else None,
                  "producer_sha256": digest(Path(__file__).read_bytes()),
                  "source_parser_sha256": digest(Path(campaign.__file__).read_bytes()),
                  "objdiff_config_sha256": digest(encoded(metadata)), "verification": UNKNOWN_VERIFICATION}
    counts, texts, owners = Counter(), {}, {}
    with store.connection:
        snapshot_id = store.add_snapshot(provenance)
        for target in targets(report, "all"):
            addr = target["address"]
            try:
                addr = int(str(addr), 16 if str(addr).startswith("0x") else 10)
            except ValueError:
                counts["invalid_address"] += 1
                continue
            asm_name = target["unit"].split("/", 1)[-1] + ".s"
            fn = indexed.get((asm_name, target["symbol"], addr))
            if fn is None:
                counts["missing_assembly"] += 1
                continue
            unit = units[target["unit"]]
            source, body, owner, reasons = target["source"], "", None, []
            if source and not target["auto_generated"]:
                try:
                    safe_path(root, source)
                    if source not in owners:
                        owners[source] = campaign.owner_source(source, root=root)
                    owner = owners[source]
                    path = safe_path(root, owner)
                    if owner not in texts:
                        texts[owner] = path.read_text(encoding="utf-8")
                    text = texts[owner]
                    start, end = campaign.function_span(text, target["symbol"])
                    body = text[start:end].strip()
                    rejected, flags = campaign.policy("", body)
                    reasons = rejected + flags
                    if campaign.pragmas_active_at(text, start):
                        reasons.append("active compiler shaping pragma")
                except (OSError, ValueError) as error:
                    reasons.append(str(error))
            else:
                reasons.append("no source-backed C definition")
            if target["pct"] != 100:
                reasons.append("target source is not scorer-exact")
            command = commands.get(source)
            if not command:
                reasons.append("missing observed compile command")
            # All sibling/header/compiler bytes are covered by the frozen input fingerprint.
            args = split_command(command.split(" && ", 1)[0]) if command else []
            for flag in ("-c", "-o"):
                if flag in args:
                    args[args.index(flag) + 1] = "<SOURCE>" if flag == "-c" else "<OUTPUT>"
            toolchain = {"ordered_command_template": args,
                         "workspace_inputs_sha256": manifest["sha256"],
                         "kind": "observed_build_configuration_not_original_source_proof"} if command else None
            insns = fn["insns"]
            strict = "\n".join(mnemonic + " " + operands for mnemonic, operands in insns)
            calls = [operands for mnemonic, operands in insns if mnemonic in {"bl", "bla"}]
            features = {"core": core_shape(fn["normalized"]), "calls": calls,
                        "opcode_counts": dict(Counter(mnemonic for mnemonic, _ in insns)),
                        "normalized": fn["normalized"], "cfg": None}
            store.add_function(snapshot_id, {
                "module": target["module"], "address": addr, "symbol": target["symbol"],
                "unit": target["unit"], "source_path": source, "owner": owner, "source_text": body,
                "assembly": strict, "bytes_hash": fn.get("bytes_hash"), "strict_hash": digest(strict),
                "shape_hash": digest(encoded(fn["normalized"])), "features": features,
                "insns": len(insns), "toolchain": toolchain, "eligible": bool(body) and not reasons,
                "eligibility": {"screen": "conservative_campaign_policy", "reasons": reasons,
                                "human_approval": None, "compile_command_sha256": digest(command) if command else None,
                                "owner_sha256": digest(texts[owner]) if owner in texts else None}, "pct": target["pct"],
                "reported_complete": bool(unit.get("metadata", {}).get("complete")),
            })
            counts["functions"] += 1
            counts["eligible" if body and not reasons else "excluded_from_retrieval"] += 1
        if check_fingerprint(root) != manifest["sha256"]:
            raise ValueError("Frozen evidence changed during import; transaction rolled back")
    return {"snapshot_id": snapshot_id, "counts": dict(counts), "stats": store.stats()}


def import_experiment(store: KnowledgeStore, snapshot_id: str, batch: Path, validation: Path) -> dict:
    snapshot = store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError("Unknown evidence snapshot")
    provenance = json.loads(snapshot[0])
    generated = json.loads((batch / "results.json").read_text(encoding="utf-8"))
    compiled = json.loads((validation / "results.json").read_text(encoding="utf-8"))
    summary, measured = generated["summary"], compiled["summary"]
    if not summary.get("finished") or not measured.get("finished"):
        raise ValueError("Only completed generation/validation runs can be imported")
    if (summary["report_sha256"] != provenance["report_sha256"]
            or summary.get("workspace_sha256") != provenance["snapshot_sha256"]
            or Path(summary["workspace"]).resolve() != Path(provenance["workspace"]).resolve()
            or Path(measured["workspace"]).resolve() != Path(provenance["workspace"]).resolve()
            or Path(measured["batch"]).resolve() != batch.resolve()):
        raise ValueError("Experiment belongs to a different frozen input set")
    for manifest in (generated, compiled):
        identifiers = [row["id"] for row in manifest["results"]]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Experiment contains duplicate task identities")
    indexed = {row["id"]: row for row in compiled["results"]}
    counts = Counter()
    with store.connection:
        for row in generated["results"]:
            result = indexed.get(row["id"])
            artifact = Path(row["artifacts"]) / "draft.c" if row.get("artifacts") else None
            if not result or artifact is None or not artifact.is_file():
                counts["missing_observation"] += 1
                continue
            raw = artifact.read_bytes()
            if digest(raw) != result.get("draft_sha256"):
                raise ValueError("Draft changed since the recorded compiler experiment")
            body = raw.decode()
            try:
                function_id = store.resolve(snapshot_id, row["symbol"], row["module"])
            except ValueError:
                counts["unresolved_target"] += 1
                continue
            target = store.function(function_id)
            for field in ("symbol", "module", "unit"):
                if result.get(field, target[field]) != target[field] or row.get(field, target[field]) != target[field]:
                    raise ValueError("Experiment target identity differs from the frozen function")
            if "address" in row:
                address = str(row["address"])
                if int(address, 16 if address.startswith("0x") else 10) != target["address"]:
                    raise ValueError("Experiment target address differs from the frozen function")
            variant = store.add_variant(function_id, body, "m2c", {
                "batch": str(batch.resolve()), "run_id": summary["run_id"], "tool": summary["tool"],
                "draft_status": row["status"], "context_mode": row.get("context_mode"),
            })
            evidence = {key: result.get(key) for key in (
                "pct", "input_sha256", "environment_sha256", "scope", "compile_ok",
                "signature_preserved", "target_size", "candidate_size", "unresolved_macros",
            )}
            evidence.update(verification=UNKNOWN_VERIFICATION, validator_version=measured.get("validator_version"),
                            environment_unchanged=measured.get("environment_unchanged"),
                            validation_manifest=str((validation / "results.json").resolve()),
                            measurement="m2c_validation_diagnostic")
            store.add_observation(variant, result["status"], evidence)
            counts[result["status"]] += 1
    return {"counts": dict(counts), "stats": store.stats()}


def import_paired_experiment(store: KnowledgeStore, snapshot_id: str, folder: Path) -> dict:
    """Keep paired model diagnostics quarantined from the original-source reference pool."""
    snapshot = store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError("Unknown evidence snapshot")
    provenance = json.loads(snapshot[0])
    data = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
    selection = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
    summary, configuration = data["summary"], inputs["configuration"]
    if not summary.get("finished") or not summary.get("environment_unchanged"):
        raise ValueError("Paired experiment must be complete with unchanged inputs")
    if (configuration.get("snapshot_id") != snapshot_id
            or configuration.get("workspace_sha256") != provenance["snapshot_sha256"]
            or summary.get("final_workspace_sha256") != provenance["snapshot_sha256"]
            or digest(encoded(configuration)) != summary.get("configuration_sha256")
            or any(summary.get(key) != value for key, value in configuration.items())
            or selection.get("configuration") != configuration):
        raise ValueError("Paired experiment configuration or frozen inputs changed")
    identifiers = [row["target_id"] for row in data["results"]]
    input_ids = [row["target_id"] for row in inputs["inputs"]]
    if (len(set(identifiers)) != len(identifiers) or identifiers != input_ids
            or input_ids != selection["targets"]):
        raise ValueError("Paired experiment target identities changed")
    excluded = set(selection["excluded"])
    counts = Counter()
    with store.connection:
        for row, captured in zip(data["results"], inputs["inputs"]):
            target = store.function(row["target_id"])
            if any(row.get(key) != target[key] for key in ("symbol", "unit", "module", "address")):
                raise ValueError("Paired target identity differs from the frozen function")
            if row.get("reference_ids", []) != captured.get("reference_ids", []) or excluded.intersection(row.get("reference_ids", [])):
                raise ValueError("Paired experiment reference identities changed or leaked")
            for arm in ("off", "on"):
                result = row.get(arm)
                if not result:
                    counts["missing_observation"] += 1
                    continue
                directory = safe_path(folder, row["target_id"] + "/" + arm)
                prompt = safe_path(folder, row["target_id"] + "/" + arm + ".prompt.txt").read_bytes()
                prompt_hash = captured["prompt_sha256"][arm]
                if digest(prompt) != prompt_hash or result.get("prompt_sha256") != prompt_hash:
                    raise ValueError("Captured paired prompt changed")
                artifact = directory / "draft.c"
                if artifact.is_file():
                    raw = artifact.read_bytes()
                    if result.get("draft_sha256") != digest(raw):
                        raise ValueError("Paired candidate changed since validation")
                    artifact_format = "candidate_C"
                else:
                    response = directory / "response.txt"
                    if not response.is_file() or not response.stat().st_size:
                        counts["missing_observation"] += 1
                        continue
                    raw = response.read_bytes()
                    if not result.get("response_sha256"):
                        counts["unbound_raw_response"] += 1
                        continue
                    if result["response_sha256"] != digest(raw):
                        raise ValueError("Paired model response changed")
                    artifact_format = "raw_model_response_not_validated_C"
                variant = store.add_variant(target["id"], raw.decode(), "local_model", {
                    "run": str(folder.resolve()), "arm": arm, "model": configuration["model"],
                    "configuration_sha256": summary["configuration_sha256"], "prompt_sha256": prompt_hash,
                    "reference_ids": row.get("reference_ids", []) if arm == "on" else [],
                    "seed": result.get("seed"), "artifact_format": artifact_format,
                    "prompt_artifact_sha256": store.artifact(prompt.decode()),
                })
                outcome = result["status"]
                if outcome == "inference_or_policy_error":
                    outcome = "validation_error"
                evidence = {key: result.get(key) for key in (
                    "pct", "input_sha256", "environment_sha256", "scope", "compile_ok", "signature_preserved",
                    "target_size", "candidate_size", "error", "elapsed_seconds")}
                evidence.update(verification=UNKNOWN_VERIFICATION, diagnostic_kind=result["status"],
                                measurement="paired_local_model_diagnostic", environment_unchanged=True)
                store.add_observation(variant, outcome, evidence)
                counts[outcome] += 1
    return {"counts": dict(counts), "stats": store.stats()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("snapshot")
    ingest.add_argument("workspace", type=Path)
    ingest.add_argument("--version", help="project build version (default: the snapshot's, else configured)")
    ingest.add_argument("--analysis-python", default=str(venv_python(REPO_ROOT / "build/tools/analysis-venv")))
    experiment = sub.add_parser("experiment")
    experiment.add_argument("snapshot_id")
    experiment.add_argument("batch", type=Path)
    experiment.add_argument("validation", type=Path)
    paired = sub.add_parser("paired")
    paired.add_argument("snapshot_id")
    paired.add_argument("folder", type=Path)
    args = parser.parse_args()
    with KnowledgeStore(args.database) as store:
        if args.command == "snapshot":
            result = import_snapshot(store, args.workspace, args.analysis_python, version=args.version)
        elif args.command == "paired":
            result = import_paired_experiment(store, args.snapshot_id, args.folder)
        else:
            result = import_experiment(store, args.snapshot_id, args.batch, args.validation)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

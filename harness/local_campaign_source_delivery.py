"""Combine verified C exports and recompile/relink the exact combined source patch."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

import local_campaign as campaign
import local_campaign_decompile as decompile
import local_campaign_decompile_validate as validator
import local_campaign_knowledge as knowledge
import local_campaign_link_gate as link
import local_campaign_source_package as package
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_recovery import object_evidence


def selected_packages(prior: Path) -> tuple[list[dict], dict, dict]:
    data = json.loads((prior / "results.json").read_text(encoding="utf-8"))
    summary = data["summary"]
    configuration = json.loads((prior / "configuration.json").read_text(encoding="utf-8"))
    if (not summary.get("finished") or summary.get("phase") != "complete" or not summary.get("environment_unchanged")
            or knowledge.digest(knowledge.encoded(configuration)) != summary["configuration_sha256"]):
        raise ValueError("Source package run is incomplete, changed or invalid")
    selected, seen = [], set()
    for row in data["results"]:
        if not row.get("machine_verified_C"):
            continue
        gates = [row.get("ci_quality", {}).get("passed"), row.get("source_diagnostics", {}).get("passed"),
                 row.get("linked_checksum"), row.get("patch_application", {}).get("passed"),
                 *[row.get("object_evidence", {}).get(axis) for axis in (*package.OBJECT_AXES, "policy_screen_passed")]]
        if not all(value is True for value in gates):
            raise ValueError("Source package claims machine verification without all actual gates")
        folder = prior / "packages" / row["package_id"]
        if knowledge.encoded(json.loads((folder / "package.json").read_text(encoding="utf-8"))) != knowledge.encoded(row):
            raise ValueError("Source package receipt differs from its run result")
        files = [(folder / "function.c", row["candidate_sha256"]),
                 (folder / "owner.patch", row["patch_application"]["patch_sha256"]),
                 (folder / "applied" / row["owner"], row["patch_application"]["owners"][row["owner"]]["after_sha256"]),
                 (Path(row["measurement"]["compile_output"]), row["object_evidence"]["objects"]["candidate"]["sha256"]),
                 (folder / "analysis.c", row["source_diagnostics"]["analysis_sha256"])]
        files.extend((folder / name, sha) for name, sha in row.get("artifact_hashes", {}).items())
        if any(knowledge.digest(path.read_bytes()) != sha for path, sha in files):
            raise ValueError("Source package source, patch, analysis or object changed")
        target_ast = json.loads((folder / "target.ast.json").read_text(encoding="utf-8"))
        if knowledge.digest(knowledge.encoded(target_ast)) != row["source_diagnostics"]["target_ast_sha256"]:
            raise ValueError("Source package target AST changed")
        key = (row["owner"], row["symbol"])
        if key not in seen:
            seen.add(key)
            selected.append({**row, "candidate": package.read_source(folder / "function.c")})
    if not selected:
        raise ValueError("No machine-verified C packages to deliver")
    return selected, configuration, summary


def combine_sources(root: Path, selected: list[dict]) -> tuple[dict, dict]:
    original, modified = {}, {}
    symbols = set()
    for row in selected:
        owner = row["owner"]
        package.owner_path(owner)
        if (owner, row["symbol"]) in symbols:
            raise ValueError("Combined source patch has an ambiguous duplicate target")
        symbols.add((owner, row["symbol"]))
        if owner not in original:
            original[owner] = package.read_source(root / owner)
            modified[owner] = original[owner]
        modified[owner] = package.replace_function(modified[owner], row["candidate"], row["symbol"], row["signature"])
    return original, modified


def run(prior: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("Delivery output exists; preserve history with a new path")
    selected, previous_config, previous_summary = selected_packages(prior)
    root = Path(previous_config["root"])
    project = Path(previous_config["policy_root"])
    if root.resolve() != campaign.ROOT.resolve() or fingerprint(root) != previous_summary["workspace_sha256"]:
        raise ValueError("DECOMP_ROOT must point to the unchanged source-package workspace")
    attested = {**previous_config["attested_tools_and_policy"], str(Path(__file__).resolve()): knowledge.digest(Path(__file__).read_bytes())}
    if any(knowledge.digest(Path(path).read_bytes()) != sha for path, sha in attested.items()):
        raise ValueError("Source policy or verifier changed after individual package verification")
    coordinator = Coordinator(project / "build/local_llm_campaign")
    inputs = {str(prior / name): knowledge.digest((prior / name).read_bytes())
              for name in ("results.json", "configuration.json", "link-plan.json")}
    inputs.update({str(prior / "packages" / row["package_id"] / "package.json"):
                   knowledge.digest((prior / "packages" / row["package_id"] / "package.json").read_bytes()) for row in selected})
    for row in selected:
        inputs.update({str(prior / "packages" / row["package_id"] / name): sha
                       for name, sha in row.get("artifact_hashes", {}).items()})
    configuration = {"schema_version": 1, "mode": "combined_exported_C_source_delivery", "source_packages": str(prior.resolve()),
                     "root": str(root.resolve()), "project": str(project.resolve()),
                     "workspace_sha256": previous_summary["workspace_sha256"], "inputs": inputs, "attested": attested,
                     "targets": [{key: row[key] for key in ("package_id", "target_id", "symbol", "owner", "source", "unit", "candidate_sha256")}
                                 for row in selected], "link_context": previous_summary["link_context"]}
    output.mkdir(parents=True, mode=0o700)
    delivery = output / "delivery"
    delivery.mkdir()
    decompile.write_json(output / "configuration.json", configuration)
    records = []

    def checkpoint(phase, finished=False, **detail):
        summary = {**{key: configuration[key] for key in ("schema_version", "mode", "workspace_sha256", "link_context")},
                   "configuration_sha256": knowledge.digest(knowledge.encoded(configuration)), "phase": phase,
                   "finished": finished, "total": len(selected), "processed": len(records),
                   "machine_verified_C": False, "ready": False, "promoted": False,
                   "human_review": None, "policy_approved": None, "updated_at": datetime.now(UTC).isoformat(), **detail}
        decompile.write_json(output / "manifest.json", summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": records})
        print(json.dumps({key: summary[key] for key in ("phase", "processed", "total", "machine_verified_C")}), flush=True)
        return summary

    try:
        checkpoint("applying_combined_patch")
        original, modified = combine_sources(root, selected)
        diff = package.source_diff(original, modified)
        patch = delivery / "owners.patch"
        patch.write_bytes(diff.encode())
        application = package.apply_patch_private(original, modified, patch, output / "applied")
        quality = package.ci_quality(root, project, modified, output / "applied", diff)
        if not quality["passed"]:
            raise ValueError("Combined patch failed project source-quality checks")
        # Reuse the actual captured baseline inputs, not any individual candidate link output.
        with coordinator.build("C delivery", "Capture independent combined link context"):
            plan = package.capture_capsule(Path(previous_config["link_capsule"]), output / "link-workspace", project)
            decompile.write_json(output / "link-plan.json", plan)
            baseline_link = link.link_once(output / "link-workspace", plan, output / "baseline-link")
        if not baseline_link["passed"]:
            raise ValueError("Combined delivery baseline checksum failed")
        import subprocess
        lookup = command_map(subprocess.check_output(["ninja", "-t", "commands", "all_source"], cwd=root, text=True,
                                                    encoding="utf-8", errors="replace", timeout=60))
        units = {unit["name"]: unit for unit in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
        compiled_units, slots = {}, {}
        for row in selected:
            symbol, source = row["symbol"], row["source"]
            checkpoint("compiling_combined_source", current=symbol)
            owner, files = package.source_files(root, source, symbol)
            if owner != row["owner"]:
                raise ValueError("Combined patch owner resolution changed")
            effective = {name: package.read_source(output / "applied" / name) if name in modified else text for name, text in files.items()}
            unit = units[row["unit"]]
            plan_unit = plan["units"].get(row["unit"], {})
            if any(unit.get(key) != plan_unit.get(key) for key in ("base_path", "target_path")):
                raise ValueError("Combined source unit disagrees with the link graph")
            folder = output / "targets" / row["target_id"]
            folder.mkdir(parents=True)
            with coordinator.build("C delivery", "Verify combined-source target: " + symbol):
                if row["unit"] not in compiled_units:
                    compilation = output / "units" / knowledge.digest(row["unit"])
                    baseline = package.compile_files(root, source, owner, files, symbol, lookup[source], compilation / "baseline")
                    candidate = package.compile_files(root, source, owner, effective, symbol, lookup[source], compilation / "candidate")
                    if not baseline["compile_ok"] or not candidate["compile_ok"]:
                        raise ValueError("Combined source or unchanged full-owner baseline failed MWCC compilation")
                    compiled_units[row["unit"]] = (baseline, candidate)
                baseline, candidate = compiled_units[row["unit"]]
                measured = validator.score_object(root, root / unit["target_path"], Path(candidate["object"]), symbol, folder)
                measured.update(compile_ok=True, compile_output=candidate["object"], baseline=baseline,
                                draft_sha256=row["candidate_sha256"], scope="full_owner_scoring_unit", artifacts=str(folder))
                evidence = object_evidence(root, row, row["candidate"], "", measured, unit)
                if not all(evidence.get(axis) is True for axis in (*package.OBJECT_AXES, "policy_screen_passed")):
                    raise ValueError("Combined source target failed raw byte/relocation/sibling checks: " + symbol)
                pp = decompile.preprocess(root, lookup[source], candidate["source"])
                if pp["status"] != "ok":
                    raise ValueError("Combined source cannot be preprocessed")
                diagnostics = package.analyze_source(pp["context"], symbol, folder)
                if not diagnostics["passed"]:
                    raise ValueError("Combined source failed assigned-target diagnostics: " + symbol)
            assigned = [slot for slot in plan["objects"]
                     if link.same_path(slot, unit.get("base_path")) or link.same_path(slot, unit.get("target_path"))]
            if len(assigned) != 1:
                raise ValueError("Combined source has no unique assigned link slot")
            obj = Path(candidate["object"])
            if assigned[0] in slots and slots[assigned[0]] != obj:
                raise ValueError("Combined source has conflicting objects for one link slot")
            slots[assigned[0]] = obj
            records.append({"target_id": row["target_id"], "symbol": symbol, "unit": row["unit"], "owner": owner,
                            "candidate_sha256": row["candidate_sha256"], "object_evidence": evidence, "source_diagnostics": diagnostics,
                            "replacement_slot": assigned[0], "compile_output": str(obj), "measurement": measured})
            (delivery / (symbol + ".c")).write_bytes(row["candidate"].encode())
        originals = {}
        try:
            for slot, obj in slots.items():
                destination = output / "link-workspace" / slot
                originals[slot] = destination.read_bytes()
                content = obj.read_bytes()
                certified = next(r["object_evidence"]["objects"]["candidate"]["sha256"] for r in records if r["replacement_slot"] == slot)
                if knowledge.digest(content) != certified:
                    raise ValueError("Combined source object changed before linking")
                destination.write_bytes(content)
            checkpoint("combined_link")
            with coordinator.build("C delivery", "Link all exported-source owners together"):
                linked = link.link_once(output / "link-workspace", plan, output / "combined-link")
        finally:
            for slot, content in originals.items():
                (output / "link-workspace" / slot).write_bytes(content)
        if not linked["passed"]:
            raise ValueError("Simultaneous combined-owner link failed the configured checksum")
        if (knowledge.digest(patch.read_bytes()) != application["patch_sha256"]
                or any(knowledge.digest((delivery / (row["symbol"] + ".c")).read_bytes()) != row["candidate_sha256"] for row in selected)
                or any(knowledge.digest((output / "applied" / name).read_bytes()) != binding["after_sha256"]
                       for name, binding in application["owners"].items())):
            raise ValueError("Actual source-only delivery or applied source changed after verification")
        for unit_name in compiled_units:
            row = next(item for item in selected if item["unit"] == unit_name)
            owner, files = package.source_files(root, row["source"], row["symbol"])
            effective = {name: package.read_source(output / "applied" / name) if name in modified else text for name, text in files.items()}
            compilation = output / "units" / knowledge.digest(unit_name)
            validator.stage_owner(root, row["source"], owner, effective, row["symbol"], None, compilation / "expected-overlay")
            if any(knowledge.digest((compilation / "candidate/overlay" / name).read_bytes()) !=
                   knowledge.digest((compilation / "expected-overlay" / name).read_bytes()) for name in files):
                raise ValueError("Actual compiled owner or wrapper changed after compilation")
        if (fingerprint(root) != configuration["workspace_sha256"]
                or any(knowledge.digest(Path(path).read_bytes()) != sha for path, sha in {**attested, **inputs}.items())
                or any(knowledge.digest((output / "link-workspace" / item["slot"]).read_bytes()) != item["sha256"] for item in plan["files"])):
            raise ValueError("Combined source inputs, prior certificates, policy or link inputs changed")
        source_hashes = {str(path.relative_to(delivery)): knowledge.digest(path.read_bytes()) for path in delivery.iterdir() if path.is_file()}
        receipt = {"schema_version": 1, "machine_verified_C": True, "ready": False, "human_review": None,
                   "policy_approved": None, "promoted": False, "source_only": True, "sources": source_hashes,
                   "owner_patch": application, "ci_quality": quality, "link": linked, "link_context": plan["context"],
                   "target_count": len(records), "owner_count": len(modified), "compiled_units": len(compiled_units),
                   "required_remaining_gates": ["native_project_build_and_version_checks", "maintainer_source_review"],
                   "configuration_sha256": knowledge.digest(knowledge.encoded(configuration))}
        decompile.write_json(delivery / "receipt.json", receipt)
        return checkpoint("complete", finished=True, machine_verified_C=True, environment_unchanged=True,
                          baseline_link=baseline_link, combined_link=linked, owners=len(modified), compiled_units=len(compiled_units),
                          delivery=str(delivery.resolve()), receipt_sha256=knowledge.digest((delivery / "receipt.json").read_bytes()))
    except Exception as error:
        decompile.write_json(delivery / "receipt.json", {"machine_verified_C": False, "ready": False, "promoted": False,
                                                       "error": f"{type(error).__name__}: {error}"})
        checkpoint("failed", finished=True, environment_unchanged=False, error=f"{type(error).__name__}: {error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source_packages.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

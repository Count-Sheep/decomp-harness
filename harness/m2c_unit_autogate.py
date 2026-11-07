"""Group exact function candidates by owner and automatically run unit link gates."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

import harness_paths
import m2c_unit_pipeline
import m2c_unit_auto_promote
import m2c_candidate_bank
import m2c_unit_evidence
import m2c_unit_source
import m2c_unit_preprocess
import m2c_unit_compile
import local_campaign_link_gate
from m2c_unit_queue import replace_file


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    replace_file(temporary, path)


def active_checkpoint(path: Path, output: Path, phase: str, processed: int, total: int,
                      unit: str | None = None, promotion: Path | None = None,
                      counts: dict[str, int] | None = None) -> None:
    write_json(path, {"pid": os.getpid(), "output": str(output), "phase": phase,
        "processed": processed, "total": total, "current_unit": unit,
        "promotion": str(promotion) if promotion else None,
        "counts": counts or {},
        "updated_at": datetime.now(UTC).isoformat()})


def trial_counts(rows: list[dict]) -> dict[str, int]:
    return {"object_verified": sum(bool(row.get("object_verified")) for row in rows),
            "link_verified": sum(bool(row.get("link_verified")) for row in rows),
            "promoted": sum(bool((row.get("promotion") or {}).get("promoted")) for row in rows)}


def bank_counts(record: dict | None) -> dict[str, int]:
    record = record or {}
    return {"candidate_banked": int(record.get("added") or 0),
            "candidate_linked": int(record.get("candidate_linked") or 0),
            "candidate_visible": int(record.get("candidate_only") or 0)}


def publish_summary(root: Path, version: str, validation: Path, result: dict) -> None:
    path = (root / "build/decomp-support" / version / "refinement-runs" /
            validation.parent.name / "results.json")
    if not path.is_file():
        return
    refinement = json.loads(path.read_text(encoding="utf-8"))
    gate = refinement.setdefault("unit_gate", {})
    gate.update({key: int(result.get(key) or 0) for key in (
        "ready_units", "owner_trials_run", "owner_objects_verified", "owner_links_verified",
        "owner_promoted", "gates_run", "links_verified", "promoted", "candidate_banked",
        "candidate_linked", "candidate_visible")})
    refinement["updated_at"] = datetime.now(UTC).isoformat()
    write_json(path, refinement)
    import local_campaign_m2c_report
    local_campaign_m2c_report.refresh_report_safely(
        root / "build/decomp-support" / version, root / "build" / version / "report.json")


def exact_refinements(paths: list[Path]) -> dict[str, dict]:
    found = {}
    for path in sorted(paths, key=lambda item: item.stat().st_mtime):
        data = json.loads(path.read_text(encoding="utf-8"))
        groups = data.get("results") if isinstance(data.get("results"), list) else [data]
        for group in groups:
            attempts = group.get("attempts") or []
            best = group.get("best_attempt")
            if not isinstance(best, int) or not 0 <= best < len(attempts):
                continue
            attempt = attempts[best]
            if not (attempt.get("object_audit") or {}).get("scratch_object_equal"):
                continue
            symbol = group.get("symbol") or data.get("symbol")
            artifact = Path(attempt.get("artifact") or "") / "draft.c"
            if artifact.is_file():
                source = artifact.read_bytes().decode("utf-8")
            else:
                source = attempt.get("candidate")
            if not symbol or not source:
                continue
            helper_rewrites = []
            for rewrite in attempt.get("owner_rewrite_artifacts") or []:
                helper = Path(rewrite.get("source_path") or "")
                if not helper.is_file() or digest(helper) != rewrite.get("sha256"):
                    raise ValueError("Exact refinement helper evidence changed: " + str(symbol))
                helper_rewrites.append({"symbol": rewrite.get("symbol"), "path": str(helper.resolve()),
                                        "sha256": rewrite.get("sha256"),
                                        "owner_path": rewrite.get("path")})
            found[symbol] = {"symbol": symbol, "source": source.rstrip() + "\n",
                             "source_sha256": hashlib.sha256((source.rstrip() + "\n").encode()).hexdigest(),
                             "evidence": str(path.resolve()), "evidence_sha256": digest(path),
                             "scope": "owner_context_function_bytes_and_relocations",
                             "helper_rewrites": helper_rewrites}
    return found


def exact_validation(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    if not summary.get("finished") or summary.get("environment_unchanged") is not True:
        raise ValueError("Unit gating requires a complete stable validation")
    evidence_sha256 = digest(path)
    found = {}
    for row in data.get("results") or []:
        winner = Path(row.get("winner_path") or "")
        if not row.get("instruction_exact") or not winner.is_file():
            continue
        if digest(winner) != row.get("winner_sha256"):
            raise ValueError("Validation winner changed: " + str(winner))
        # Bytes, not text mode: the hash-bound source must round-trip exactly on every platform.
        found[row["symbol"]] = {"symbol": row["symbol"], "source": winner.read_bytes().decode("utf-8"),
                                "source_sha256": row["winner_sha256"],
                                "evidence": str(path.resolve()), "evidence_sha256": evidence_sha256,
                                "scope": "instruction_exact_validation"}
    return found


def configured_source_paths(root: Path, version: str, report: dict) -> dict[str, str]:
    split_path = root / "config" / version / "splits.txt"
    if not split_path.is_file():
        return {}
    headers = []
    for line in split_path.read_text(encoding="utf-8").splitlines():
        if not line.endswith(":"):
            continue
        relative = line[:-1]
        if Path(relative).suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}:
            headers.append(relative)
    mapped = {}
    for unit in report.get("units") or []:
        name = str(unit.get("name") or "")
        suffix = name.split("/", 1)[-1]
        matches = [path for path in headers if Path(path).with_suffix("").as_posix() == suffix]
        if len(matches) == 1:
            mapped[name] = "src/" + matches[0]
    return mapped


def inventory(report: dict, exact: dict[str, dict], source_paths: dict[str, str] | None = None,
              root: Path | None = None) -> list[dict]:
    rows = []
    for unit in report.get("units") or []:
        source = ((unit.get("metadata") or {}).get("source_path")
                  or (source_paths or {}).get(unit.get("name")))
        functions = unit.get("functions") or []
        if not source or not functions:
            continue
        names = [function["name"] for function in functions]
        project_exact = {function["name"] for function in functions
                         if float(function.get("fuzzy_match_percent") or 0) == 100.0}
        overrides = [name for name in names if name not in project_exact and name in exact]
        missing = [name for name in names if name not in project_exact and name not in exact]
        rows.append({"unit": unit["name"], "source_path": source, "functions": names,
                     "source_exists": root is None or (root / source).is_file(),
                     "project_exact": sorted(project_exact), "candidate_exact": overrides,
                     "missing": missing, "ready": not missing,
                     "gate_required": not missing and bool(overrides)})
    return rows


def materialize_candidates(output: Path, row: dict, symbols: list[str],
                           exact: dict[str, dict]) -> tuple[dict[str, Path], dict[str, Path]]:
    overrides = {}
    helper_inputs = {}
    helper_hashes = {}
    candidate_dir = output / "candidates" / hashlib.sha256(
        (row["unit"] + "\0" + "\0".join(symbols)).encode()).hexdigest()[:16]
    candidate_dir.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        path = candidate_dir / (hashlib.sha256(symbol.encode()).hexdigest()[:16] + ".c")
        path.write_bytes(exact[symbol]["source"].encode("utf-8"))
        if digest(path) != exact[symbol]["source_sha256"]:
            raise ValueError("Materialized exact candidate changed: " + symbol)
        overrides[symbol] = path
        for helper in exact[symbol].get("helper_rewrites") or []:
            name, sha = helper.get("symbol"), helper.get("sha256")
            if not name or not sha:
                raise ValueError("Incomplete helper evidence for " + symbol)
            if name in helper_hashes and helper_hashes[name] != sha:
                raise ValueError("Conflicting exact candidates require different helper source: " + name)
            helper_hashes[name] = sha
            helper_inputs[name] = Path(helper["path"])
    return overrides, helper_inputs


def run(root: Path, version: str, validation: Path, refinements: list[Path], output_root: Path,
        *, auto_promote: bool = False, owner_trials: bool = True) -> dict:
    root, validation, output_root = root.resolve(), validation.resolve(), output_root.absolute()
    validation_exact = exact_validation(validation)
    refined_exact = exact_refinements(refinements)
    exact = {**validation_exact, **refined_exact}
    report_path = root / "build" / version / "report.json"
    config_path = root / "objdiff.json"
    inputs = {str(path.resolve()): digest(path) for path in [validation, report_path, config_path,
              Path(__file__), Path(m2c_unit_pipeline.__file__), Path(m2c_candidate_bank.__file__),
              Path(local_campaign_link_gate.__file__), Path(m2c_unit_auto_promote.__file__),
              Path(m2c_unit_evidence.__file__),
              Path(m2c_unit_source.__file__), Path(m2c_unit_preprocess.__file__),
              Path(m2c_unit_compile.__file__),
              *refinements]}
    key = hashlib.sha256(json.dumps({"inputs": inputs, "auto_promote": auto_promote,
                                    "owner_trials": owner_trials},
                                   sort_keys=True).encode()).hexdigest()[:16]
    output = output_root / key
    saved = output / "result.json"
    if saved.is_file():
        prior = json.loads(saved.read_text(encoding="utf-8"))
        if prior.get("inputs") == inputs and prior.get("finished"):
            publish_summary(root, version, validation, prior)
            return prior
        if prior.get("inputs") != inputs:
            raise ValueError("Existing unit gate output does not match its inputs")
        result = prior
    elif output.exists():
        raise ValueError("Unit gate output exists without a recovery record")
    else:
        output.mkdir(parents=True)
        result = {"schema_version": 1, "project_root": str(root), "version": version,
                  "validation": str(validation), "inputs": inputs, "exact_candidates": sorted(exact),
                  "units": [],
                  "auto_promote": auto_promote, "owner_trials_enabled": owner_trials,
                  "owner_trials": [], "attempts": [], "candidate_bank": None,
                  "finished": False}
        write_json(saved, result)
    result.setdefault("owner_trials", [])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    units = inventory(report, exact, configured_source_paths(root, version, report), root)
    result["units"] = units
    completed = {attempt["unit"] for attempt in result["attempts"]}
    gate_rows = [row for row in units if row["gate_required"]]
    active_path = output_root / "active.json"
    owners = {row["name"]: row for row in json.loads(config_path.read_text(encoding="utf-8")).get("units") or []}
    batch = Path(json.loads(validation.read_text(encoding="utf-8"))["summary"]["batch"]) / "results.json"
    trial_rows = ([(row, symbol) for row in units if not row["ready"] and row["source_exists"]
                   for symbol in row["candidate_exact"]] if owner_trials else [])
    completed_trials = {(row["unit"], row["symbol"]) for row in result["owner_trials"]}
    active_checkpoint(active_path, output, "owner_trial", len(completed_trials), len(trial_rows),
                      counts=trial_counts(result["owner_trials"]))
    for row, symbol in trial_rows:
        if (row["unit"], symbol) in completed_trials:
            continue
        owner = owners.get(row["unit"])
        attempt = {"unit": row["unit"], "symbol": symbol, "state": "not_run",
                   "object_verified": False, "link_verified": False}
        if (not row["source_exists"] or owner is None
                or (owner.get("metadata") or {}).get("source_path") != row["source_path"]):
            attempt["error"] = "Missing matching objdiff source owner"
        else:
            trial_output = output / "owner-trials" / hashlib.sha256(
                (row["unit"] + "\0" + symbol).encode()).hexdigest()[:16]
            if trial_output.exists():
                shutil.rmtree(trial_output)
            active_checkpoint(active_path, output, "owner_trial", len(result["owner_trials"]),
                              len(trial_rows), row["unit"] + ":" + symbol,
                              counts=trial_counts(result["owner_trials"]))
            try:
                overrides, helper_inputs = materialize_candidates(output, row, [symbol], exact)
                measured = m2c_unit_pipeline.run(root, row["unit"], [batch], trial_output,
                    link=True, unit_source=root / row["source_path"], preprocess_source=True,
                    function_sources=overrides, helper_sources=helper_inputs)
                attempt.update(state=measured.get("state"),
                    object_verified=measured.get("object_verified", False),
                    link_verified=measured.get("link_verified", False),
                    artifact=str(trial_output), error=measured.get("error"))
                if auto_promote and attempt["link_verified"]:
                    promotion_output = output / "owner-promotions" / hashlib.sha256(
                        (row["unit"] + "\0" + symbol).encode()).hexdigest()[:16]
                    active_checkpoint(active_path, output, "promotion", len(result["owner_trials"]),
                                      len(trial_rows), row["unit"] + ":" + symbol, promotion_output,
                                      counts=trial_counts(result["owner_trials"]))
                    promoted = m2c_unit_auto_promote.run(
                        root, version, trial_output / "link/result.json", promotion_output)
                    attempt["promotion"] = {key: promoted.get(key) for key in
                                            ("state", "promoted", "commit", "error")}
            except (OSError, ValueError, RuntimeError) as error:
                attempt["state"] = "owner_trial_failed"
                attempt["error"] = f"{type(error).__name__}: {error}"
        result["owner_trials"].append(attempt)
        write_json(saved, result)
        active_checkpoint(active_path, output, "owner_trial", len(result["owner_trials"]),
                          len(trial_rows), counts=trial_counts(result["owner_trials"]))
    # Absent source is normal for named decomp owners. Assemble the available
    # drafts once per owner, keeping exact overrides and all remaining functions.
    source_less = ([row for row in units if not row["source_exists"] and not row["ready"]
                    and row["candidate_exact"]] if owner_trials else [])
    for row in source_less:
        if (row["unit"], "*") in completed_trials:
            continue
        trial_output = output / "owner-trials" / hashlib.sha256(row["unit"].encode()).hexdigest()[:16]
        if trial_output.exists():
            shutil.rmtree(trial_output)
        attempt = {"unit": row["unit"], "symbol": "*", "symbols": row["candidate_exact"],
                   "source_mode": "retained_drafts", "object_verified": False, "link_verified": False}
        active_checkpoint(active_path, output, "owner_trial", len(result["owner_trials"]),
                          len(trial_rows) + len(source_less), row["unit"])
        try:
            if owners.get(row["unit"]) is None:
                raise ValueError("Missing target objdiff owner: " + row["unit"])
            overrides, helpers = materialize_candidates(output, row, row["candidate_exact"], exact)
            measured = m2c_unit_pipeline.run(root, row["unit"], [batch], trial_output,
                link=True, function_sources=overrides, helper_sources=helpers,
                proposal_source=row["source_path"])
            attempt.update(state=measured.get("state"), compiled=measured.get("compiled", False),
                           object_verified=measured.get("object_verified", False),
                           link_verified=measured.get("link_verified", False),
                           artifact=str(trial_output), error=measured.get("error"))
            if auto_promote and attempt["link_verified"]:
                promoted = m2c_unit_auto_promote.run(root, version, trial_output / "link/result.json",
                    output / "owner-promotions" / trial_output.name, row["source_path"])
                attempt["promotion"] = {key: promoted.get(key) for key in ("state", "promoted", "commit", "error")}
        except (OSError, ValueError, RuntimeError) as error:
            attempt.update(state="owner_trial_failed", error=f"{type(error).__name__}: {error}")
        result["owner_trials"].append(attempt)
        write_json(saved, result)
    active_checkpoint(active_path, output, "unit_gate", len(result["attempts"]), len(gate_rows))
    for row in units:
        if not row["gate_required"] or row["unit"] in completed:
            continue
        owner = owners.get(row["unit"])
        if owner is None or (row["source_exists"]
                and (owner.get("metadata") or {}).get("source_path") != row["source_path"]):
            row["gate_error"] = "Missing matching objdiff source owner"
            continue
        try:
            overrides, helper_inputs = materialize_candidates(
                output, row, row["candidate_exact"], exact)
        except ValueError as error:
            row["gate_error"] = str(error)
            continue
        unit_output = output / "units" / hashlib.sha256(row["unit"].encode()).hexdigest()[:16]
        if unit_output.exists():
            shutil.rmtree(unit_output)
        active_checkpoint(active_path, output, "unit_gate", len(result["attempts"]),
                          len(gate_rows), row["unit"])
        if row["source_exists"]:
            unit_source = root / row["source_path"]
            preprocess = True
            function_sources = overrides
        else:
            if row["project_exact"]:
                row["gate_error"] = "Source-less owner has exact functions without retained source"
                continue
            unit_source = None
            preprocess = False
            function_sources = overrides
        measured = m2c_unit_pipeline.run(root, row["unit"], [batch], unit_output,
            link=True, unit_source=unit_source, preprocess_source=preprocess,
            function_sources=function_sources, helper_sources=helper_inputs,
            proposal_source=row["source_path"])
        attempt = {"unit": row["unit"], "state": measured.get("state"),
                   "object_verified": measured.get("object_verified", False),
                   "link_verified": measured.get("link_verified", False),
                   "artifact": str(unit_output), "error": measured.get("error")}
        if auto_promote and attempt["link_verified"]:
            promotion_output = output / "promotions" / hashlib.sha256(row["unit"].encode()).hexdigest()[:16]
            active_checkpoint(active_path, output, "promotion", len(result["attempts"]),
                              len(gate_rows), row["unit"], promotion_output)
            promoted = m2c_unit_auto_promote.run(root, version, unit_output / "link/result.json",
                                                 promotion_output, row["source_path"])
            attempt["promotion"] = {key: promoted.get(key) for key in
                                    ("state", "promoted", "commit", "error")}
        result["attempts"].append(attempt)
        write_json(saved, result)
        active_checkpoint(active_path, output, "unit_gate", len(result["attempts"]), len(gate_rows))
    if auto_promote and result.get("candidate_bank") is None:
        bank_output = output / "candidate-bank"
        active_checkpoint(active_path, output, "candidate_bank", 0, len(exact),
                          counts={**trial_counts(result["owner_trials"]),
                                  **bank_counts(result.get("candidate_bank"))})
        banked = m2c_candidate_bank.run(root, version, validation, bank_output)
        result["candidate_bank"] = {key: banked.get(key) for key in
                                    ("state", "promoted", "added", "certified",
                                     "candidate_linked", "candidate_only", "commit", "error")}
        write_json(saved, result)
        active_checkpoint(active_path, output, "candidate_bank", len(exact), len(exact),
                          counts={**trial_counts(result["owner_trials"]),
                                  **bank_counts(result["candidate_bank"])})
    result.update(finished=True, ready_units=sum(row["ready"] for row in units),
                  owner_trials_run=len(result["owner_trials"]),
                  owner_objects_verified=sum(row["object_verified"] for row in result["owner_trials"]),
                  owner_links_verified=sum(row["link_verified"] for row in result["owner_trials"]),
                  owner_promoted=sum(bool((row.get("promotion") or {}).get("promoted"))
                                     for row in result["owner_trials"]),
                  gates_run=len(result["attempts"]),
                  links_verified=sum(row["link_verified"] for row in result["attempts"]),
                  promoted=(sum(bool((row.get("promotion") or {}).get("promoted"))
                                for row in result["owner_trials"])
                            + sum(bool((row.get("promotion") or {}).get("promoted"))
                                  for row in result["attempts"])),
                  **bank_counts(result.get("candidate_bank")))
    write_json(saved, result)
    publish_summary(root, version, validation, result)
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
        if active.get("pid") == os.getpid():
            active_path.unlink()
    except (OSError, ValueError):
        pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=harness_paths.DECOMP_ROOT,
                        help="project checkout (default: the configured project)")
    parser.add_argument("--version", default=harness_paths.VERSION,
                        help="build version (default: the configured project's)")
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--refinement", action="append", default=[], type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--auto-promote", action="store_true",
                        help="commit SHA-verified units and bank standalone candidates")
    parser.add_argument("--skip-owner-trials", action="store_true",
                        help="process complete units before optional incremental owner trials")
    args = parser.parse_args()
    if not args.version:
        parser.error("no build version: pass --version or configure the project (see harness.example.json)")
    result = run(args.root, args.version, args.validation, args.refinement, args.output_root,
                 auto_promote=args.auto_promote, owner_trials=not args.skip_owner_trials)
    print(json.dumps({key: result[key] for key in ("ready_units", "gates_run", "links_verified")}, indent=2))


if __name__ == "__main__":
    main()

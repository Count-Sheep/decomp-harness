"""Refine validation winners, then re-evaluate owner-unit link readiness."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import local_campaign_decompile as decompile
import m2c_capture_inputs
import m2c_native_search
import m2c_source_search
import m2c_unit_autogate


def selected_rows(validation: dict, limit: int, minimum_pct: float) -> list[dict]:
    if not 0 <= limit <= 32:
        raise ValueError("Refinement limit must be between zero and 32")
    rows = [row for row in validation.get("results") or []
            if row.get("status") == "non_exact" and float(row.get("pct") or 0) >= minimum_pct
            and row.get("winner_path") and Path(row["winner_path"]).is_file()]
    return sorted(rows, key=lambda row: (-float(row["pct"]), row["symbol"]))[:limit]


def validation_fingerprint(validation: dict) -> str:
    """Hash stable compiler evidence, excluding dashboard/refinement annotations."""
    summary = validation.get("summary") or {}
    stable = {"batch": summary.get("batch"),
              "environment_sha256": summary.get("environment_sha256"),
              "final_environment_sha256": summary.get("final_environment_sha256"),
              "rows": [{key: row.get(key) for key in ("id", "symbol", "unit", "status", "pct",
                       "candidate_origin", "winner_path", "winner_sha256", "instruction_exact")}
                       for row in validation.get("results") or []]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def native_supported_symbols(snapshot: dict) -> list[str]:
    """Keep unsupported compiler owners in source refinement without aborting native search."""
    return [row["symbol"] for row in snapshot.get("rows") or []
            if (row.get("compiler_profile") or {}).get("supported") is True
            and (row.get("compiler_profile") or {}).get("profiles")]


def run(root: Path, version: str, validation_path: Path, output: Path, *, limit: int = 12,
        minimum_pct: float = 98.0, source_variants: int = 48, native_variants: int = 3,
        auto_promote: bool = False) -> dict:
    root, validation_path, output = root.resolve(), validation_path.resolve(), output.absolute()
    validation_raw = validation_path.read_bytes()
    validation = json.loads(validation_raw)
    summary = validation.get("summary") or {}
    if not summary.get("finished") or summary.get("environment_unchanged") is not True:
        raise ValueError("Refinement requires a complete stable validation")
    rows = selected_rows(validation, limit, minimum_pct)
    expected = {"project_root": str(root), "version": version,
              "validation": str(validation_path),
              "validation_sha256": validation_fingerprint(validation),
              "selection": [{key: row.get(key) for key in ("symbol", "address", "size", "pct",
                             "candidate_origin", "winner_path", "winner_sha256")} for row in rows],
              "source_variants": source_variants, "native_variants": native_variants,
              "auto_promote": auto_promote}
    record = output / "results.json"
    if record.is_file():
        result = json.loads(record.read_text(encoding="utf-8"))
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing refinement output belongs to different inputs")
        if result.get("finished"):
            return result
    elif output.exists():
        raise ValueError("Refinement output exists without a recovery record")
    else:
        output.mkdir(parents=True)
        result = {"schema_version": 1, **expected,
                  "refinement_engine": "deterministic_compiler_guided", "model_inputs": [],
                  "attempts": [], "finished": False, "updated_at": datetime.now(UTC).isoformat()}
        decompile.write_json(record, result)
    native_rows = [row for row in rows if row.get("candidate_origin") == "m2c"]
    native_by_symbol = {}
    if native_rows:
        snapshot = output / "native-inputs.json"
        batch = Path(summary["batch"])
        if not snapshot.is_file():
            captured = m2c_capture_inputs.export(root, version, [batch],
                [row["symbol"] for row in native_rows], snapshot, fresh_context=True)
        else:
            captured = json.loads(snapshot.read_text(encoding="utf-8"))
        supported = native_supported_symbols(captured)
        result["native_search"] = {"eligible": supported,
            "skipped_unsupported": sorted(set(row["symbol"] for row in native_rows) - set(supported))}
        decompile.write_json(record, result)
        if supported:
            native_output = output / "native"
            m2c_native_search.run(snapshot, decompile.tool().parent, native_output,
                                  supported, native_variants)
            native_report = json.loads((native_output / "results.json").read_text(encoding="utf-8"))
            native_by_symbol = {row["symbol"]: row for row in native_report["results"]}
    completed = {row["symbol"] for row in result["attempts"]}
    for row in rows:
        if row["symbol"] in completed:
            continue
        winner = Path(row["winner_path"])
        if hashlib.sha256(winner.read_bytes()).hexdigest() != row["winner_sha256"]:
            raise ValueError("Validation winner changed: " + str(winner))
        seed, origin = winner, row["candidate_origin"]
        native = native_by_symbol.get(row["symbol"])
        if native:
            best = native["attempts"][native["best_attempt"]]
            candidate = Path(best.get("artifact") or "") / "draft.c"
            pct = float((best.get("measurement", {}).get("outcome") or {}).get("pct") or 0)
            exact = bool((best.get("object_audit") or {}).get("scratch_object_equal"))
            if candidate.is_file() and (exact or pct > float(row["pct"])):
                seed, origin = candidate, "native_m2c_variant"
        destination = root / "build/decomp-support" / version / "refinements" / (
            "auto-" + hashlib.sha256((result["validation_sha256"] + row["symbol"]
                                      + str(output)).encode()).hexdigest()[:20])
        seed_argument = None if origin == "current_source" and seed == winner else seed
        source_record = destination / "results.json"
        try:
            measured = json.loads(source_record.read_text(encoding="utf-8")) if source_record.is_file() else {}
        except (OSError, ValueError):
            measured = {}
        if not measured.get("finished"):
            if destination.exists():
                shutil.rmtree(destination)
            try:
                measured = m2c_source_search.run(root, version, row, destination, source_variants,
                                                  seed_path=seed_argument, seed_origin=origin)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result["attempts"].append({"symbol": row["symbol"], "origin": origin,
                    "artifact": str(destination), "best_pct": row.get("pct"),
                    "object_exact": False, "status": "skipped", "error": str(error)})
                result["updated_at"] = datetime.now(UTC).isoformat()
                decompile.write_json(record, result)
                continue
        best = measured["attempts"][measured["best_attempt"]]
        result["attempts"].append({"symbol": row["symbol"], "origin": origin,
            "artifact": str(destination), "best_pct": best.get("pct"),
            "object_exact": bool((best.get("object_audit") or {}).get("scratch_object_equal")),
            "status": "complete"})
        result["updated_at"] = datetime.now(UTC).isoformat()
        decompile.write_json(record, result)
    refinements = sorted((root / "build/decomp-support" / version / "refinements").glob("*/results.json"))
    result.update(phase="unit_gate", updated_at=datetime.now(UTC).isoformat())
    decompile.write_json(record, result)
    gated = m2c_unit_autogate.run(root, version, validation_path, refinements,
                                  root / "build/decomp-support" / version / "unit-gates",
                                  auto_promote=auto_promote)
    result.update(finished=True, phase="complete", updated_at=datetime.now(UTC).isoformat(),
                  exact=sum(row["object_exact"] for row in result["attempts"]),
                  unit_gate={key: gated.get(key, 0) for key in
                             ("ready_units", "owner_trials_run", "owner_objects_verified",
                              "owner_links_verified", "owner_promoted", "gates_run",
                              "links_verified", "promoted", "candidate_banked",
                              "candidate_linked", "candidate_visible")})
    decompile.write_json(record, result)
    return result

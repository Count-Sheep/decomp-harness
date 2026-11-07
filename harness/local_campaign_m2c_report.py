"""Build the compact, persistent dashboard report for project-wide m2c sweeps.

The batch and validation directories remain the authoritative artifacts. This
module turns them into a small index so the read-only dashboard never has to
parse every per-function result on its two-second refresh cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness_paths import DECOMP_ROOT, PROJECT_ID, VERSION, WINDOWS, project

SCHEMA_VERSION = 1
REPORT_NAME = "m2c-report.json"
VALIDATOR_VERSION = 2


def project_state_root() -> Path:
    """The checkout that keeps m2c state: the configured project's root.

    ``DECOMP_ROOT`` may select a frozen workspace copy for an experiment; sweeps run there still
    report into the configured project, so its dashboard shows them. Without a configured project
    it is ``DECOMP_ROOT`` itself.
    """
    configured = project(PROJECT_ID)
    return configured.root if configured else DECOMP_ROOT


def support_root(version: str | None = None) -> Path:
    """Batches, validations, draft caches and the dashboard report: ``build/decomp-support/<VERSION>``."""
    version = version or VERSION
    if not version:
        raise SystemExit("No project version is configured: set DECOMP_VERSION or configure a project "
                         "(see harness.example.json)")
    return project_state_root() / "build" / "decomp-support" / version


def _default_support_root() -> Path | None:
    try:
        return support_root()
    except SystemExit:
        return None


def _windows_argv(command: str) -> list[str]:
    """Split a Windows command line exactly as the C runtime does."""
    import ctypes
    from ctypes import wintypes

    if not command:
        return []
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    array = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not array:
        raise ValueError("Unparseable command line")
    try:
        return [array[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(array)


def _windows_process_snapshot() -> list[dict[str, Any]] | None:
    """Windows has no ``ps``: use psutil when installed, else one CIM query."""
    now = time.time()
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None
    if psutil is not None:
        rows = []
        for process in psutil.process_iter(["pid", "ppid", "create_time", "cmdline", "status"]):
            info = process.info
            if not info.get("cmdline"):
                continue
            rows.append({"pid": int(info["pid"]), "parent": int(info.get("ppid") or 0),
                         "elapsed_seconds": max(0, int(now - (info.get("create_time") or now))),
                         "state": "T" if info.get("status") == "stopped" else "R",
                         "argv": list(info["cmdline"])})
        return rows
    script = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | ForEach-Object { "
              "[pscustomobject]@{pid=$_.ProcessId; parent=$_.ParentProcessId; "
              "started=[DateTimeOffset]::new($_.CreationDate).ToUnixTimeSeconds(); command=$_.CommandLine} } "
              "| ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                check=True, timeout=10)
        value = json.loads(result.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    rows = []
    for row in value if isinstance(value, list) else [value]:
        try:
            rows.append({"pid": int(row["pid"]), "parent": int(row["parent"]),
                         "elapsed_seconds": max(0, int(now - int(row["started"]))), "state": "R",
                         "argv": _windows_argv(row.get("command") or "")})
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return rows


def process_snapshot() -> list[dict[str, Any]] | None:
    """Observe existing runners, including ones started before live reporting."""
    if WINDOWS:
        return _windows_process_snapshot()
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,stat=,command="],
                                capture_output=True, text=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in result.stdout.splitlines():
        try:
            pid, parent, elapsed, state, command = line.split(None, 4)
            parts = [int(part) for part in elapsed.replace("-", ":").split(":")]
            seconds = sum(value * weight for value, weight in zip(reversed(parts), (1, 60, 3600, 86400)))
            rows.append({"pid": int(pid), "parent": int(parent), "elapsed_seconds": seconds,
                         "state": state, "argv": shlex.split(command)})
        except ValueError:
            continue
    return rows


def _active_refinement_progress(support_root: Path, refinement_path: Path,
                                refinement: dict[str, Any]) -> dict[str, Any]:
    """Compact inner-loop state for a running deterministic source search."""
    completed = list(refinement.get("attempts") or [])
    selected = list(refinement.get("selection") or [])
    current = selected[len(completed)] if len(completed) < len(selected) else None
    recent = []
    baseline_by_symbol = {row.get("symbol"): row.get("pct") for row in selected}
    for row in completed[-5:]:
        validation_pct = baseline_by_symbol.get(row.get("symbol"))
        best = row.get("best_pct")
        completed_search = _read(Path(str(row.get("artifact") or "")) / "results.json")
        completed_attempts = completed_search.get("attempts") or []
        baseline = completed_attempts[0].get("pct") if completed_attempts else validation_pct
        delta = (float(best) - float(baseline)
                 if isinstance(best, (int, float)) and isinstance(baseline, (int, float)) else None)
        recent.append({"symbol": row.get("symbol"), "validation_pct": validation_pct,
                       "baseline_pct": baseline, "best_pct": best,
                       "delta_pp": delta, "object_exact": bool(row.get("object_exact")),
                       "origin": row.get("origin")})
    result: dict[str, Any] = {"current": None, "recent_completed": recent}
    if not current or not refinement.get("validation_sha256"):
        return result
    output = refinement_path.parent.resolve()
    key = hashlib.sha256((str(refinement["validation_sha256"]) + str(current.get("symbol"))
                          + str(output)).encode()).hexdigest()[:20]
    search_path = support_root / "refinements" / f"auto-{key}" / "results.json"
    search = _read(search_path)
    if not search:
        result["current"] = {"symbol": current.get("symbol"), "baseline_pct": current.get("pct"),
                             "variants_tested": 0, "variant_budget": refinement.get("source_variants")}
        return result
    attempts = list(search.get("attempts") or [])
    best_index = int(search.get("best_attempt") or 0)
    best = attempts[best_index] if 0 <= best_index < len(attempts) else {}
    latest = attempts[-1] if attempts else {}
    validation_pct = current.get("pct")
    baseline = attempts[0].get("pct") if attempts else validation_pct
    best_pct = best.get("pct")
    mismatch = best.get("mismatch_classification") or {}
    mismatch_rows = mismatch.get("mismatches") or []
    source_lines = sorted({int(row["source_line"]) for row in mismatch_rows
                           if isinstance(row.get("source_line"), int) and row["source_line"] > 0})[:8]
    try:
        updated_at = datetime.fromtimestamp(search_path.stat().st_mtime, UTC).isoformat()
    except OSError:
        updated_at = refinement.get("updated_at")
    result["current"] = {
        "symbol": current.get("symbol"), "validation_pct": validation_pct,
        "baseline_pct": baseline, "best_pct": best_pct,
        "delta_pp": (float(best_pct) - float(baseline)
                     if isinstance(best_pct, (int, float)) and isinstance(baseline, (int, float)) else None),
        "variants_tested": max(0, len(attempts) - 1),
        "variant_budget": search.get("max_variants", refinement.get("source_variants")),
        "compiled_attempts": sum(bool(row.get("compile_ok")) for row in attempts),
        "improved_attempts": sum(isinstance(row.get("pct"), (int, float))
                                 and isinstance(baseline, (int, float))
                                 and float(row["pct"]) > float(baseline) for row in attempts[1:]),
        "search_depth": latest.get("search_depth"), "max_depth": search.get("max_depth"),
        "latest_pct": latest.get("pct"), "latest_variant": latest.get("variant"),
        "best_variant": best.get("variant"), "mismatch_counts": mismatch.get("counts") or {},
        "mismatch_total": mismatch.get("total"), "source_lines": source_lines,
        "updated_at": updated_at,
    }
    return result


def live_runs(support_root: Path, processes: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    manifests = [(path, _read(path)) for kind in ("batches", "validation")
                 for path in (support_root / kind).glob("*/manifest.json")]
    batches = {path.parent.name: value for path, value in manifests if path.parent.parent.name == "batches"}
    # support_root is <project>/build/decomp-support/<VERSION>; runners resolve relative paths there.
    parents = support_root.resolve().parents
    project_root = parents[2] if len(parents) > 2 else support_root.resolve()
    campaign_root = _default_support_root()
    active = []
    for process in processes:
        argv = process["argv"]
        if not argv or not Path(argv[0]).name.lower().startswith("python") or process["state"].startswith("Z"):
            continue
        stage, runner, arguments = None, "campaign", []
        for offset, arg in enumerate(argv[1:], 1):
            if Path(arg).name == "local_campaign_decompile.py":
                stage, arguments = "generation", argv[offset + 1:]
                break
            if Path(arg).name == "local_campaign_decompile_validate.py":
                stage, arguments = "validation", argv[offset + 1:]
                break
            if Path(arg).name == "m2c_drafts.py":
                stage, runner, arguments = "generation", "project", argv[offset + 1:]
                break
            if Path(arg).name == "m2c_validate_drafts.py":
                stage, runner, arguments = "validation", "project", argv[offset + 1:]
                break
            if Path(arg).name == "m2c_pipeline_runner.py":
                stage, runner, arguments = "pipeline", "pipeline", argv[offset + 1:]
                break
            if Path(arg).name == "m2c_unit_autogate.py":
                stage, runner, arguments = "validation", "unit_gate", argv[offset + 1:]
                break
        if stage is None:
            continue
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        parser.add_argument("--limit", type=int, default=0)
        if runner in ("project", "pipeline", "unit_gate"):
            parser.add_argument("--root")
            parser.add_argument("--version")
        if runner == "unit_gate":
            parser.add_argument("--validation", required=True)
        if stage == "generation" and runner == "campaign":
            parser.add_argument("--replay")
            parser.add_argument("--all", action="store_true")
            parser.add_argument("--corpus", default="unmatched")
        elif stage == "validation" and runner == "campaign":
            parser.add_argument("batch", nargs="?")
            parser.add_argument("--force", action="store_true")
        else:
            parser.add_argument("--batch")
            parser.add_argument("--force", action="store_true")
        try:
            options, _ = parser.parse_known_args(arguments)
        except (argparse.ArgumentError, SystemExit):
            continue
        if runner in ("project", "pipeline", "unit_gate"):
            if options.root and Path(options.root).resolve() != project_root.resolve():
                continue
            if options.version and options.version != support_root.name:
                continue
        if runner == "unit_gate":
            validation = Path(options.validation)
            validation = validation if validation.is_absolute() else project_root / validation
            if validation.resolve() != (support_root / "validation" / validation.parent.name / "results.json").resolve():
                continue
            options.batch = (_read(validation).get("summary") or {}).get("batch")
            if not options.batch:
                continue
        if runner == "pipeline":
            pipeline = _read(support_root / "pipeline-state.json")
            if pipeline.get("status") != "active" or pipeline.get("stage") not in ("generation", "validation"):
                continue
            stage = pipeline["stage"]
            options.batch = pipeline.get("batch")
        if stage == "generation" and runner == "campaign" and not (options.all or options.replay):
            continue
        input_batch = options.replay if stage == "generation" and runner == "campaign" else options.batch
        if input_batch:
            candidate = Path(input_batch)
            candidate = candidate if candidate.is_absolute() else project_root / candidate
            if candidate.resolve() != (support_root / "batches" / candidate.name).resolve():
                continue
        started = now.timestamp() - process["elapsed_seconds"]
        choices = []
        for path, value in manifests:
            if path.parent.parent.name != ("batches" if stage == "generation" else "validation"):
                continue
            try:
                updated = path.stat().st_mtime
            except OSError:
                continue
            if runner == "unit_gate" and path.parent.resolve() != validation.parent.resolve():
                continue
            if runner != "unit_gate" and updated < started - 3:
                continue
            if stage == "generation" and runner == "campaign":
                if options.replay:
                    if Path(str(value.get("baseline", ""))).name != Path(options.replay).name:
                        continue
                elif value.get("baseline") != "live" or value.get("selection", "unmatched") != options.corpus:
                    continue
            elif stage == "generation":
                if value.get("selection", "unmatched") != "unmatched":
                    continue
            elif options.batch and Path(str(value.get("batch", ""))).name != Path(options.batch).name:
                continue
            choices.append((updated, path, value))
        selected = max(choices, key=lambda item: item[0]) if choices else None
        if not selected and (campaign_root is None or support_root.resolve() != campaign_root.resolve()):
            continue
        path, value = (selected[1], selected[2]) if selected else (None, {})
        run_id = path.parent.name if path else (Path(options.batch).name if stage == "validation" and options.batch else None)
        batch = batches.get(run_id, {})
        if stage == "generation" and runner == "campaign":
            if value:
                batch = value
            elif options.replay:
                batch = batches.get(Path(options.replay).name, {})
            else:
                batch = {"selection": options.corpus}
        elif stage == "generation":
            batch = value or {"selection": "unmatched"}
        current_function = None
        current_work = None
        waiting = False
        for child in processes:
            if child["parent"] != process["pid"]:
                continue
            args = child["argv"]
            if any(Path(arg).name == "m2c.py" for arg in args) and "-f" in args:
                position = args.index("-f") + 1
                current_function = args[position] if position < len(args) else None
        workspace = value.get("workspace") or batch.get("workspace")
        if workspace:
            coordination = _read(Path(workspace) / "build/local_llm_campaign/coordination.json")
            for build in (coordination.get("builds") or {}).values():
                if build.get("pid") != process["pid"]:
                    continue
                current_work = build.get("detail")
                waiting = build.get("status") == "waiting"
                if current_work and current_work.startswith("MWCC/objdiff: "):
                    current_function = current_work.removeprefix("MWCC/objdiff: ")
                break
        state = "running" if value else "starting"
        if value.get("finished"):
            state = "finishing"
        display_stage, display_value = stage, value
        refinement_progress = None
        if stage == "validation" and path is not None and value.get("finished"):
            refinement_path = support_root / "refinement-runs" / path.parent.name / "results.json"
            refinement = _read(refinement_path)
            if refinement and not refinement.get("finished"):
                completed = list(refinement.get("attempts") or [])
                selected_rows = list(refinement.get("selection") or [])
                refinement_phase = refinement.get("phase")
                display_stage = (refinement_phase if refinement_phase in ("owner_trial", "unit_gate", "promotion",
                                                                           "candidate_bank")
                                 else "refinement")
                display_value = {
                    "processed": len(completed), "total": len(selected_rows),
                    "counts": {"object_exact": sum(bool(row.get("object_exact")) for row in completed)},
                    "updated_at": refinement.get("updated_at") or datetime.fromtimestamp(
                        refinement_path.stat().st_mtime, UTC).isoformat(),
                }
                next_row = selected_rows[len(completed)] if len(completed) < len(selected_rows) else None
                current_function = (next_row or {}).get("symbol")
                current_work = ("Preparing exact candidates and unit ownership"
                                if display_stage in ("unit_gate", "owner_trial")
                                else "Compiler-guided source-shape search")
                if display_stage == "refinement":
                    refinement_progress = _active_refinement_progress(
                        support_root, refinement_path, refinement)
                    current_detail = refinement_progress.get("current") or {}
                    if current_detail.get("updated_at"):
                        display_value["updated_at"] = current_detail["updated_at"]
                state = "running"
            elif refinement:
                completed = list(refinement.get("attempts") or [])
                display_stage = "finalizing"
                display_value = {
                    "processed": len(completed),
                    "total": len(refinement.get("selection") or []),
                    "counts": {"object_exact": sum(bool(row.get("object_exact")) for row in completed),
                               "promoted": (refinement.get("unit_gate") or {}).get("promoted", 0)},
                    "updated_at": refinement.get("updated_at"),
                }
                current_function = None
                current_work = "Refreshing retained sweep and promotion reports"
                state = "running"
            gate_active = _read(support_root / "unit-gates" / "active.json")
            gate_output = Path(str(gate_active.get("output") or ""))
            expected_gate_root = (support_root / "unit-gates").resolve()
            try:
                gate_output.resolve().relative_to(expected_gate_root)
                trusted_gate = gate_active.get("pid") == process["pid"]
            except (OSError, ValueError):
                trusted_gate = False
            if trusted_gate:
                display_stage = gate_active.get("phase") or "unit_gate"
                display_value = {
                    "processed": gate_active.get("processed", 0),
                    "total": gate_active.get("total", 0),
                    "counts": gate_active.get("counts") or {},
                    "updated_at": gate_active.get("updated_at"),
                }
                current_function = gate_active.get("current_unit")
                current_work = "Full-unit object and link verification"
                if display_stage == "promotion":
                    promotion = _read(Path(str(gate_active.get("promotion") or "")) / "result.json")
                    phase = str(promotion.get("phase") or "full build and SHA verification").replace("_", " ")
                    current_work = f"Promotion: {phase}"
                elif display_stage == "candidate_bank":
                    current_work = "Banking exact functions and verifying linked leaves"
                refinement_progress = None
                state = "running"
        if waiting:
            state = "waiting_for_build_lock"
        if process["state"].startswith("T"):
            state = "paused"
        active.append({
            "run_id": run_id, "stage": display_stage, "pid": process["pid"],
            "state": state,
            "selection": batch.get("selection", "unmatched"), "elapsed_seconds": process["elapsed_seconds"],
            "updated_at": display_value.get("updated_at"), "processed": int(display_value.get("processed") or 0),
            "total": int(display_value.get("total") or 0), "counts": display_value.get("counts") or {},
            "compiler_succeeded": display_value.get("compiler_succeeded"), "compiled_functions": display_value.get("compiled_functions"),
            "pipeline": value.get("pipeline"), "dossier_functions": value.get("dossier_functions"),
            "current_function": current_function,
            "current_work": current_work,
            "refinement_progress": refinement_progress,
        })
    return sorted(active, key=lambda row: (row["stage"], row["pid"]))


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _matched_reference_pool(report_file: Path | None) -> dict[str, Any]:
    if report_file is None or not report_file.is_file():
        return {"available": 0, "report_sha256": None, "similarity_index_ready": False}
    import hashlib

    raw = report_file.read_bytes()
    report = json.loads(raw)
    names = set()
    for unit in report.get("units", []):
        meta = unit.get("metadata") or {}
        if not meta.get("source_path") or meta.get("auto_generated"):
            continue
        functions = list(unit.get("functions") or [])
        for section in unit.get("sections") or []:
            functions.extend(section.get("functions") or [])
        for function in functions:
            if float(function.get("fuzzy_match_percent") or 0) == 100:
                names.add(function.get("name"))
    index = report_file.parents[1] / "simindex/index.pkl"
    return {
        "available": len(names),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "similarity_index_ready": index.is_file(),
    }


def _batch_record(path: Path, support_root: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    data = _read(path)
    summary = data.get("summary") or {}
    if not summary.get("finished"):
        return None
    results = data.get("results") or []
    counts = Counter(row.get("status", "unknown") for row in results)
    diagnostics: Counter[str] = Counter()
    affected: Counter[str] = Counter()
    for row in results:
        for name, count in (row.get("diagnostics") or {}).items():
            if isinstance(count, (int, float)) and count:
                diagnostics[name] += int(count)
                affected[name] += 1
    record = {
        "run_id": summary.get("run_id") or path.parent.name,
        "selection": summary.get("selection", "unmatched"),
        "workspace": summary.get("workspace"),
        "updated_at": summary.get("updated_at"),
        "report_sha256": summary.get("report_sha256"),
        "tool_revision": (summary.get("tool") or {}).get("revision"),
        "tool_fingerprint": (summary.get("tool") or {}).get("fingerprint"),
        "total": int(summary.get("total") or len(results)),
        "processed": int(summary.get("processed") or len(results)),
        "jobs": int(summary.get("jobs") or 1),
        "counts": dict(counts or summary.get("counts") or {}),
        "context_modes": summary.get("context_modes") or {},
        "pipeline": summary.get("pipeline"),
        "dossier_functions": sum(row.get("investigation", {}).get("available") is True for row in results)
            if summary.get("pipeline") else None,
        "reference_candidate_functions": sum(bool(row.get("investigation", {}).get("dossier", {}).get("matched_reference_candidates"))
                                             for row in results) if summary.get("pipeline") else None,
        "reference_functions": sum(bool(row.get("reference") or row.get("references")) for row in results),
        "reference_examples": sum(len(row.get("references") or []) for row in results),
        "typed_context_functions": sum(row.get("context_mode") == "typed" for row in results),
        "inferred_profile_functions": sum(bool(row.get("compiler_profile")) for row in results),
        "high_confidence_profile_functions": sum(
            (row.get("compiler_profile") or {}).get("confidence") == "high" for row in results
        ),
        "exact_reuse_functions": sum(
            row.get("status") == "exact_reuse" or bool(row.get("exact_reuse")) for row in results
        ),
        "baseline": Path(str(summary.get("baseline"))).name if summary.get("baseline") not in (None, "", "live") else "live",
        "artifact": _relative(path.parent, support_root),
        "diagnostics": dict(diagnostics),
        "diagnostic_functions": dict(affected),
    }
    return record, {str(row.get("id")): row for row in results if row.get("id")}


def _validation_record(path: Path, support_root: Path, batch_rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    data = _read(path)
    summary = data.get("summary") or {}
    if not summary.get("finished"):
        return None
    rows = data.get("results") or []
    counts = Counter(row.get("status", "unknown") for row in rows)
    scored = []
    for row in rows:
        if not isinstance(row.get("pct"), (int, float)):
            continue
        original = batch_rows.get(str(row.get("id")), {}).get("pct")
        scored.append({
            "symbol": row.get("symbol"), "unit": row.get("unit"), "pct": round(float(row["pct"]), 6),
            "original_pct": round(float(original), 6) if isinstance(original, (int, float)) else None,
            "scope": row.get("scope"), "status": row.get("status"),
            "instruction_exact": row.get("instruction_exact"),
        })
    scored.sort(key=lambda row: (-row["pct"], row.get("symbol") or ""))
    version = int(summary.get("validator_version") or 0)
    return {
        "updated_at": summary.get("updated_at"),
        "total": int(summary.get("total") or len(rows)),
        "processed": int(summary.get("processed") or len(rows)),
        "counts": dict(counts or summary.get("counts") or {}),
        "compiler_succeeded": int(summary.get("compiler_succeeded") or 0),
        "compiled_functions": int(summary.get("compiled_functions") or 0),
        "instruction_exact": (sum(bool(row.get("instruction_exact")) for row in rows)
                              if any("instruction_exact" in row for row in rows) else None),
        "reused_functions": sum(bool(row.get("reused_from_validation")) for row in rows),
        "repaired_functions": sum(bool((row.get("repair") or {}).get("changed")) for row in rows),
        "repair_transformations": sum(
            len((row.get("repair") or {}).get("transformations") or []) for row in rows
        ),
        "environment_unchanged": summary.get("environment_unchanged"),
        "validator_version": version,
        "comparable": version >= VALIDATOR_VERSION and summary.get("environment_unchanged") is True,
        "artifact": _relative(path.parent, support_root),
        "top_candidates": scored[:30],
        "mean_scored_pct": round(sum(row["pct"] for row in scored) / len(scored), 6) if scored else None,
        "_measurements": {str(row["id"]): {
            "symbol": row.get("symbol"), "pct": row.get("pct"), "scope": row.get("scope"),
            "compile_ok": bool(row.get("compile_ok")), "status": row.get("status"),
            "draft_status": row.get("draft_status"),
        } for row in rows if row.get("id")},
        "environment_sha256": summary.get("environment_sha256"),
    }


def _refinement_record(path: Path, support_root: Path) -> dict[str, Any] | None:
    data = _read(path)
    if not data:
        return None
    selected = {str(row.get("symbol")): row for row in data.get("selection") or []}
    rows = []
    for attempt in data.get("attempts") or []:
        symbol = str(attempt.get("symbol") or "")
        baseline = selected.get(symbol, {}).get("pct")
        best = attempt.get("best_pct")
        delta = (round(float(best) - float(baseline), 6)
                 if isinstance(best, (int, float)) and isinstance(baseline, (int, float)) else None)
        rows.append({"symbol": symbol, "baseline_pct": baseline, "best_pct": best, "delta_pp": delta,
                     "object_exact": bool(attempt.get("object_exact")), "origin": attempt.get("origin")})
    updated = data.get("updated_at")
    if not updated:
        try:
            updated = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        except OSError:
            updated = None
    return {"finished": bool(data.get("finished")), "updated_at": updated,
            "selected": len(data.get("selection") or []), "processed": len(rows),
            "exact": sum(row["object_exact"] for row in rows),
            "improved": sum(isinstance(row["delta_pp"], (int, float)) and row["delta_pp"] > 0 for row in rows),
            "rows": rows, "unit_gate": data.get("unit_gate") or {},
            "artifact": _relative(path.parent, support_root)}


def compare_validation(current: dict[str, Any] | None, prior: dict[str, Any] | None,
                       allow_environment_change: bool = False) -> dict[str, Any] | None:
    if not current or not prior:
        return None
    before, after = prior.get("_measurements", {}), current.get("_measurements", {})
    environment_matches = (bool(current.get("environment_sha256"))
                           and current.get("environment_sha256") == prior.get("environment_sha256"))
    comparable = (current.get("comparable") and prior.get("comparable")
                  and (environment_matches or allow_environment_change)
                  and bool(before) and before.keys() == after.keys()
                  and prior.get("total") == len(before) and current.get("total") == len(after))
    if not comparable:
        return {"comparable": False}
    gained, lost, newly_scored = [], [], []
    exact_gained, exact_lost, compile_losses = [], [], []
    compiled_gained, compiled_lost = 0, 0
    for key, row in after.items():
        old = before[key]
        compiled_gained += row['compile_ok'] and not old['compile_ok']
        compiled_lost += old['compile_ok'] and not row['compile_ok']
        if old['compile_ok'] and not row['compile_ok']:
            compile_losses.append({'symbol': row['symbol'], 'draft_status': row.get('draft_status'),
                                   'status': row.get('status')})
        if old.get('scope') == row.get('scope'):
            if row.get('pct') == 100.0 and old.get('pct') != 100.0:
                exact_gained.append(row['symbol'])
            if old.get('pct') == 100.0 and row.get('pct') != 100.0:
                exact_lost.append(row['symbol'])
        if isinstance(row.get('pct'), (int, float)) and not isinstance(old.get('pct'), (int, float)):
            newly_scored.append({'symbol': row['symbol'], 'pct': row['pct'], 'scope': row.get('scope')})
        if (not isinstance(old.get('pct'), (int, float)) or not isinstance(row.get('pct'), (int, float))
                or old.get('scope') != row.get('scope')):
            continue
        delta = row['pct'] - old['pct']
        entry = {"symbol": row['symbol'], "before_pct": old['pct'], "after_pct": row['pct'],
                 "gain_pp": round(delta, 6)}
        if delta > 1e-6:
            gained.append(entry)
        elif delta < -1e-6:
            lost.append(entry)
    return {"comparable": True, "comparison_basis": ("frozen_target_replay" if allow_environment_change
                                                       and not environment_matches else "identical_environment"),
            "environment_changed": not environment_matches,
            "functions": len(after), "scores_improved": len(gained),
            "scores_regressed": len(lost), "new_compiles": compiled_gained, "lost_compiles": compiled_lost,
            "new_exact_scores": len(exact_gained), "lost_exact_scores": len(exact_lost),
            "new_exact_functions": sorted(exact_gained)[:30], "lost_exact_functions": sorted(exact_lost)[:30],
            "compile_losses": sorted(compile_losses, key=lambda row: row['symbol'])[:30],
            "new_scores": len(newly_scored),
            "newly_scored": sorted(newly_scored, key=lambda row: -row['pct'])[:30],
            "improvements": sorted(gained, key=lambda row: -row['gain_pp'])[:30],
            "regressions": sorted(lost, key=lambda row: row['gain_pp'])[:30]}


def build_report(support_root: Path, report_file: Path | None = None) -> dict[str, Any]:
    support_root = support_root.resolve()
    batches: dict[str, dict[str, Any]] = {}
    batch_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted((support_root / "batches").glob("*/results.json")):
        parsed = _batch_record(path, support_root)
        if parsed:
            record, rows = parsed
            batches[record["run_id"]] = record
            batch_rows[record["run_id"]] = rows

    validations: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((support_root / "validation").glob("*/results.json")):
        raw = _read(path)
        batch_id = Path(str((raw.get("summary") or {}).get("batch", ""))).name
        if not batch_id or batch_id not in batches:
            continue
        record = _validation_record(path, support_root, batch_rows.get(batch_id, {}))
        if record:
            record["canonical"] = path.parent.name == batch_id
            refinement = _refinement_record(
                support_root / "refinement-runs" / path.parent.name / "results.json", support_root)
            if refinement:
                record["refinement"] = refinement
            validations.setdefault(batch_id, []).append(record)

    runs = []
    for choices in validations.values():
        choices.sort(key=lambda row: (bool(row["canonical"]), str(row.get("updated_at") or "")), reverse=True)
    for run_id, batch in batches.items():
        choices = validations.get(run_id, [])
        choices.sort(key=lambda row: (bool(row["canonical"]), str(row.get("updated_at") or "")), reverse=True)
        validation = choices[0] if choices else None
        prior = batches.get(batch.get("baseline"))
        clean = int(batch["counts"].get("ok", 0))
        run = {**batch, "validation": validation, "archived_validations": choices[1:], "artifact_available": True}
        run["clean_delta"] = clean - int(prior["counts"].get("ok", 0)) if prior else None
        baseline_validations = validations.get(batch.get("baseline"), [])
        run["comparison"] = compare_validation(
            validation, baseline_validations[0] if baseline_validations else None,
            allow_environment_change=bool(prior and batch.get("baseline") not in (None, "", "live")),
        )
        runs.append(run)
    for choices in validations.values():
        for choice in choices:
            choice.pop('_measurements', None)
    runs.sort(key=lambda row: (str(row.get("updated_at") or ""), row["run_id"]))
    unmatched = [row for row in runs if row.get("selection", "unmatched") == "unmatched"]
    matched = [row for row in runs if row.get("selection") == "matched"]
    latest = unmatched[-1] if unmatched else (runs[-1] if runs else None)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "support_root": str(support_root),
        "history_count": len(runs),
        "runs": runs,
        "latest": latest,
        "matched_latest": matched[-1] if matched else None,
        "matched_reference_pool": _matched_reference_pool(report_file),
    }


def refresh_report(support_root: Path, report_file: Path | None = None) -> dict[str, Any]:
    previous = _read(support_root / REPORT_NAME)
    report = build_report(support_root, report_file)
    current = {row["run_id"]: row for row in report["runs"]}
    for row in previous.get("runs", []):
        if row.get("run_id") not in current:
            current[row["run_id"]] = {**row, "artifact_available": False}
    report["runs"] = sorted(current.values(), key=lambda row: (str(row.get("updated_at") or ""), row["run_id"]))
    report["history_count"] = len(report["runs"])
    unmatched = [row for row in report["runs"] if row.get("selection", "unmatched") == "unmatched"]
    matched = [row for row in report["runs"] if row.get("selection") == "matched"]
    report["latest"] = unmatched[-1] if unmatched else (report["runs"][-1] if report["runs"] else None)
    report["matched_latest"] = matched[-1] if matched else None
    support_root.mkdir(parents=True, exist_ok=True)
    destination = support_root / REPORT_NAME
    fd, temporary = tempfile.mkstemp(prefix=f".{REPORT_NAME}.", dir=support_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        # A Windows dashboard reading the previous report can briefly block the replacement.
        for attempt in range(50):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if not WINDOWS or attempt == 49:
                    raise
                time.sleep(0.02)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return report


def refresh_report_safely(support_root: Path, report_file: Path | None = None) -> None:
    """Reporting must never turn a completed sweep into a failed sweep."""
    try:
        refresh_report(support_root, report_file)
    except (OSError, ValueError, TypeError) as error:
        print(f"m2c dashboard report refresh failed: {type(error).__name__}: {error}", file=sys.stderr)


def load_report(support_root: Path) -> dict[str, Any]:
    report = _read(support_root / REPORT_NAME)
    if not report:
        report = {"schema_version": SCHEMA_VERSION, "generated_at": None, "history_count": 0, "runs": [],
                  "latest": None, "matched_reference_pool": {"available": 0, "similarity_index_ready": False}}
    now = datetime.now(UTC)
    processes = process_snapshot()
    report.update(active_runs=live_runs(support_root, processes, now) if processes is not None else [],
                  activity_available=processes is not None, activity_checked_at=now.isoformat())
    # Read compact checkpoints, never thousands of per-function results on a poll.
    active = {row["run_id"]: row for row in report["active_runs"] if row["stage"] == "validation"}
    progress = {}
    for path in (support_root / "validation").glob("*/manifest.json"):
        summary = _read(path)
        if not summary or summary.get("finished"):
            continue
        batch = Path(str(summary.get("batch", "")))
        if batch.resolve() != (support_root / "batches" / batch.name).resolve():
            continue
        record = {key: summary.get(key) for key in (
            "updated_at", "total", "processed", "counts", "compiler_succeeded",
            "compiled_functions", "instruction_exact", "environment_sha256")}
        record.update(artifact=_relative(path.parent, support_root), finished=False, comparable=False,
                      state=active[path.parent.name]["state"] if path.parent.name in active else
                      "interrupted" if processes is not None else "unknown")
        if str(record.get("updated_at") or "") > str(progress.get(batch.name, {}).get("updated_at") or ""):
            progress[batch.name] = record
    for run in [*report.get("runs", []), report.get("latest"), report.get("matched_latest")]:
        if not run:
            continue
        pending = progress.get(run["run_id"])
        completed_at = str((run.get("validation") or {}).get("updated_at") or "")
        if pending and str(pending.get("updated_at") or "") > completed_at:
            run["validation_progress"] = pending
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-root", type=Path,
                        help="default: <project>/build/decomp-support/<VERSION>")
    parser.add_argument("--report", type=Path, help="default: <DECOMP_ROOT>/build/<VERSION>/report.json")
    args = parser.parse_args()
    if args.support_root is None:
        args.support_root = support_root()
    if args.report is None:
        if not VERSION:
            parser.error("no project version configured; pass --report or set DECOMP_VERSION")
        args.report = DECOMP_ROOT / "build" / VERSION / "report.json"
    value = refresh_report(args.support_root, args.report)
    print(json.dumps({"report": str(args.support_root / REPORT_NAME), "runs": value["history_count"],
                      "latest": (value.get("latest") or {}).get("run_id")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

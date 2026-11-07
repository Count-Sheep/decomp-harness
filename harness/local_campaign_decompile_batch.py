"""Resumable project-wide m2c coverage and diagnostics, isolated from campaign state."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from pathlib import Path

import local_campaign_decompile as decompile
import local_campaign_asm_inputs as asm_inputs
import local_campaign_m2c_report as m2c_report
import local_campaign_investigation as investigation
import local_campaign_obligations as obligations
from harness_paths import VERSION


def targets(report: dict, selection: str = "unmatched") -> list[dict]:
    """Deduplicate overlapping report views without claiming exact assembly stubs as C."""
    if selection not in {"unmatched", "matched", "all"}:
        raise ValueError("Unknown corpus selection")
    selected = {}
    for unit in report.get("units", []):
        meta = unit.get("metadata") or {}
        functions = list(unit.get("functions") or [])
        for section in unit.get("sections") or []:
            functions.extend(section.get("functions") or [])
        for function in functions:
            symbol = function["name"]
            address = (function.get("metadata") or {}).get("virtual_address", symbol)
            key = (meta.get("module_id", meta.get("module_name", "main")), address)
            row = {"symbol": symbol, "unit": unit["name"], "source": meta.get("source_path"),
                   "module": meta.get("module_name", "main"), "address": address,
                   "pct": float(function.get("fuzzy_match_percent") or 0),
                   "auto_generated": bool(meta.get("auto_generated"))}
            row["id"] = hashlib.sha256(f"{key}:{unit['name']}:{symbol}".encode()).hexdigest()[:20]
            priority = lambda value: (bool(value["source"]), not value["auto_generated"], value["pct"])
            if key not in selected or priority(row) > priority(selected[key]):
                selected[key] = row
    return sorted((row for row in selected.values()
                   if selection == "all" or selection == "unmatched" and row["pct"] < 100.0
                   or selection == "matched" and row["pct"] == 100.0 and row["source"] and not row["auto_generated"]),
                  key=lambda row: (row["unit"], str(row["address"]), row["symbol"]))


def command_map(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if "mwcceppc" not in line:
            continue
        try:
            args = decompile.split_command(line.split(" && ", 1)[0])
            if "-c" in args:
                source = args[args.index("-c") + 1]
                result[source] = line
        except (ValueError, IndexError):
            continue
    return result


def target_evidence(root: Path, row: dict, index: dict, report_file: Path,
                    report_hash: str, assembly: tuple[str, bytes] | None) -> dict:
    """Capture the campaign dossier without letting annotations alter m2c inputs."""
    import local_campaign as campaign
    try:
        if assembly is None or not row.get("source"):
            raise ValueError("Target assembly or editable source is unavailable")
        if investigation.digest(report_file.read_bytes()) != report_hash:
            raise ValueError("Report changed after batch selection")
        owner = (root / campaign.owner_source(row["source"])).resolve()
        if not owner.is_relative_to(root.resolve() / "src"):
            raise ValueError("Owner is outside the project source tree")
        source = owner.read_text(encoding="utf-8")
        start, end = campaign.function_span(source, row["symbol"])
        address = row.get("address")
        address = int(str(address), 16 if str(address).startswith("0x") else 10) if address is not None else None
        session = investigation.Session(root, index, row["symbol"], source[start:end],
            campaign.function_signature(source, start, end), report_sha256=report_hash,
            expected_address=address, expected_unit=row["unit"])
        if session.content != assembly[1]:
            raise ValueError("Assembly changed after input capture")
        return {"available": True, "dossier": session.dossier(),
                "role": "Read-only annotation; not an m2c input or verification certificate"}
    except (OSError, ValueError, UnicodeError) as error:
        return {"available": False, "reason": str(error)[:300]}


def annotate_result(result: dict, evidence: dict) -> dict:
    result["investigation"] = evidence
    artifact = result.get("artifacts")
    draft = Path(artifact) / "draft.c" if artifact else None
    candidate_hash = investigation.digest(draft.read_bytes()) if draft and draft.is_file() else None
    result["draft_sha256"] = candidate_hash
    result["obligations"] = obligations.ledger({}, candidate_sha256=candidate_hash)
    return result


def translate_target(root: Path, row: dict, snapshot: tuple[str, bytes] | None, context: dict,
                     cache: Path, metadata: dict, timeout: int, force: bool, *, context_declarations: bool = False,
                     readonly_data: bool = False) -> dict:
    if snapshot is None:
        return {**row, "status": "missing_assembly", "error": "No target assembly for this report function"}
    try:
        label, asm = snapshot
        owner = decompile.m2c_readonly_data.owner_path(root, VERSION, row["unit"]) if readonly_data else None
        translated = decompile.translate(root, row["symbol"], asm, label, context, cache, metadata, timeout, force,
                                         context_declarations=context_declarations, readonly_owner=owner)
        typed_failure = None
        if translated["status"] == "decompile_error" and translated["context_mode"] == "typed":
            typed_failure = {key: translated[key] for key in ("status", "error", "artifacts")}
            translated = decompile.translate(root, row["symbol"], asm, label,
                                              {"status": "unavailable", "error": typed_failure["error"]},
                                              cache, metadata, timeout, force, context_declarations=context_declarations,
                                              readonly_owner=owner)
        result = {**row, **{key: value for key, value in translated.items()
                            if key not in {"draft", "command", "context_command"}},
                  "draft_chars": len(translated.get("draft", ""))}
        if typed_failure:
            result["typed_failure"] = typed_failure
        return result
    except Exception as exc:
        return {**row, "status": "batch_error", "error": f"{type(exc).__name__}: {exc}"}


def run_batch(root: Path, jobs: int = 1, limit: int = 0, timeout: int = 45, force: bool = False,
              baseline: Path | None = None, selection: str = "unmatched", *, context_declarations: bool = False,
              readonly_data: bool = False) -> int:
    if baseline and readonly_data:
        raise ValueError("Readonly enrichment requires a fresh batch; legacy replays lack frozen owner data")
    import local_campaign as campaign
    import local_campaign_corpus as corpus
    metadata = decompile.tool_metadata()
    support = m2c_report.support_root()
    cache = support / "drafts"
    frozen = {}
    with campaign.COORDINATOR.build("Batch C draft", "Snapshot report and target assembly index"):
        if baseline:
            prior = json.loads((baseline / "results.json").read_text(encoding="utf-8"))
            if not prior["summary"].get("finished"):
                raise ValueError("Only a completed batch can be replayed")
            report_hash = prior["summary"]["report_sha256"]
            selection = prior["summary"].get("selection", "unmatched")
            frozen = {row["id"]: row for row in prior["results"]}
            keys = ("id", "symbol", "unit", "source", "module", "address", "pct", "auto_generated")
            work = [{key: row[key] for key in keys} for row in frozen.values()]
            index, compile_commands = {}, {}
            commands = subprocess.CompletedProcess([], 0, "", "")
        else:
            report_bytes = campaign.REPORT_FILE.read_bytes()
            report_hash = hashlib.sha256(report_bytes).hexdigest()
            work = targets(json.loads(report_bytes), selection)
            index = corpus.build_index(root, campaign.REPORT_FILE)
            commands = subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      timeout=60)
            compile_commands = command_map(commands.stdout) if commands.returncode == 0 else {}
        if limit:
            work = work[:max(0, limit)]
        asm_inputs.jump_tables(root)
    origin = str(baseline.resolve()) if baseline else "live"
    pipeline = {"version": "m2c-dossiers-v1", "investigation_version": investigation.VERSION,
                "context_declarations": context_declarations,
                "readonly_data": readonly_data,
                "readonly_owners": (decompile.m2c_readonly_data.owner_fingerprints(
                    root, VERSION, [row["unit"] for row in work]) if readonly_data else {}),
                "readonly_capture_sha256": (investigation.digest(Path(decompile.m2c_readonly_data.__file__).read_bytes())
                                             if readonly_data else None),
                "producer_sha256": investigation.digest(Path(__file__).read_bytes()),
                "investigation_sha256": investigation.digest(Path(investigation.__file__).read_bytes()),
                "model_calls": 0, "role": "Dossier annotations do not change deterministic m2c inputs"}
    identity = f"batch-v3:{report_hash}:{metadata['fingerprint']}:{limit}:{origin}:{investigation.digest(investigation.encoded(pipeline))}"
    snapshot_manifest = root / "snapshot.json"
    snapshot_id = (json.loads(snapshot_manifest.read_text(encoding="utf-8")).get("sha256")
                   if snapshot_manifest.is_file() else None)
    if snapshot_id:
        identity += f":snapshot:{snapshot_id}"
    if selection != "unmatched":
        identity += f":{selection}"
    run_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
    folder = support / "batches" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    decompile.write_json(folder / "targets.json", {"report_sha256": report_hash, "targets": work})
    results = {}
    for row in work:
        path = folder / "results" / (row["id"] + ".json")
        if path.is_file() and not force:
            try:
                results[row["id"]] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    grouped = defaultdict(list)
    for row in work:
        if row["id"] not in results:
            grouped[(row["unit"], row["source"])].append(row)

    def checkpoint(finished: bool = False) -> None:
        if finished and readonly_data and pipeline["readonly_owners"] != decompile.m2c_readonly_data.owner_fingerprints(
                root, VERSION, [row["unit"] for row in work]):
            raise ValueError("Readonly owner inputs changed during generation; batch remains unfinished")
        summary = {"run_id": run_id, "report_sha256": report_hash, "tool": metadata,
                   "selection": selection,
                   "pipeline": pipeline,
                   "dossier_functions": sum(row.get("investigation", {}).get("available") is True for row in results.values()),
                   "reference_candidate_functions": sum(bool(row.get("investigation", {}).get("dossier", {}).get("matched_reference_candidates"))
                                                        for row in results.values()),
                   "workspace": str(root.resolve()),
                   "workspace_sha256": snapshot_id,
                   "updated_at": datetime.now(UTC).isoformat(), "total": len(work), "processed": len(results),
                   "finished": finished, "jobs": jobs, "counts": dict(Counter(row["status"] for row in results.values())),
                   "context_modes": dict(Counter(row.get("context_mode", "none") for row in results.values())),
                   "folder": str(folder), "baseline": origin,
                   "compile_commands_error": commands.stderr[-1000:] if commands.returncode else ""}
        decompile.write_json(folder / "manifest.json", summary)
        if finished:
            decompile.write_json(folder / "results.json", {"summary": summary, "results": list(results.values())})
            m2c_report.refresh_report_safely(support, campaign.REPORT_FILE)
        print(json.dumps({key: summary[key] for key in ("run_id", "total", "processed", "counts", "finished", "folder")}), flush=True)

    def process_unit(group: list[dict]) -> list[dict]:
        source = group[0]["source"]
        assemblies = {}
        references = {}
        evidence = {}
        # Only context capture needs the build lock; m2c runs on immutable snapshots.
        if baseline:
            context = {"status": "unavailable", "error": "Baseline had no parseable typed context"}
            for row in group:
                previous = frozen[row["id"]]
                evidence[row["id"]] = previous.get("investigation") or {
                    "available": False, "reason": "Legacy replay has no captured dossier; live evidence is not substituted"}
                if previous.get("reference"):
                    references[row["id"]] = previous["reference"]
                artifact = previous.get("typed_failure", {}).get("artifacts") or previous.get("artifacts")
                if not artifact:
                    continue
                path = Path(artifact)
                asm_path = path / "original_target.s"
                assemblies[row["id"]] = (previous.get("assembly", str(path / "target.s")),
                                          (asm_path if asm_path.exists() else path / "target.s").read_bytes())
                if previous.get("typed_failure") or previous.get("context_mode") == "typed":
                    context = {"status": "ok", "context": (path / "context.c").read_text(encoding="utf-8")}
        else:
            with campaign.COORDINATOR.build("Batch C draft", f"Snapshot types: {group[0]['unit']}"):
                context = decompile.preprocess(root, compile_commands.get(source, ""), source) if source else \
                          {"status": "unavailable", "error": "No editable C source in the report"}
                if selection == "matched" and source:
                    try:
                        owner = campaign.owner_source(source)
                        owner_text = (root / owner).read_text(encoding="utf-8")
                        for row in group:
                            try:
                                start, end = campaign.function_span(owner_text, row["symbol"])
                                reference = folder / "references" / (row["id"] + ".c")
                                reference.parent.mkdir(parents=True, exist_ok=True)
                                body = owner_text[start:end]
                                decompile.write_text(reference, body)
                                references[row["id"]] = {
                                    "source": owner, "function": str(reference),
                                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                                    "pragmas": campaign.pragmas_active_at(owner_text, start),
                                    "policy_flags": campaign.policy("", body)[1],
                                }
                            except ValueError:
                                pass
                    except (OSError, ValueError):
                        pass
                for row in group:
                    asm_path = index["functions"].get(row["symbol"], {}).get("asm_path")
                    if asm_path:
                        path = root / asm_path
                        try:
                            assemblies[row["id"]] = (str(path), path.read_bytes())
                        except OSError:
                            pass
                for row in group:
                    evidence[row["id"]] = target_evidence(root, row, index, campaign.REPORT_FILE,
                                                         report_hash, assemblies.get(row["id"]))
        completed = []
        for row in group:
            result = translate_target(root, row, assemblies.get(row["id"]), context, cache, metadata, timeout, force,
                                      context_declarations=context_declarations, readonly_data=readonly_data)
            annotate_result(result, evidence[row["id"]])
            decompile.write_json(folder / "evidence" / (row["id"] + ".json"), evidence[row["id"]])
            if row["id"] in references:
                result["reference"] = references[row["id"]]
            decompile.write_json(folder / "results" / (row["id"] + ".json"), result)
            completed.append(result)
        return completed

    checkpoint()
    def collect(group: list[dict], operation) -> None:
        try:
            completed = operation()
        except Exception as exc:
            completed = [{**row, "status": "batch_error", "error": f"{type(exc).__name__}: {exc}"} for row in group]
            for result in completed:
                decompile.write_json(folder / "results" / (result["id"] + ".json"), result)
        results.update({row["id"]: row for row in completed})
        checkpoint()

    if jobs == 1:
        for group in grouped.values():
            collect(group, lambda: process_unit(group))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(process_unit, group): group for group in grouped.values()}
            for future in as_completed(futures):
                collect(futures[future], future.result)
    checkpoint(finished=True)
    return 0

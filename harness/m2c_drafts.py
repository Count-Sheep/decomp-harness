#!/usr/bin/env python3
"""Create reproducible evidence-rich m2c drafts for the selected project's DOL."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import local_campaign_asm_inputs as asm_inputs
import local_campaign_decompile as decompile
import local_campaign_m2c_report as m2c_report
import m2c_context as m2c_context
import m2c_remote
from harness_paths import DECOMP_ROOT, VERSION, bind_project_tools
from m2c_context import build_check

PIPELINE_VERSION = "gc-evidence-v3"
FUNCTION = re.compile(
    r"(?ms)^\.fn\s+(?P<symbol>[A-Za-z_][A-Za-z_0-9]*),[^\n]*\n.*?^\.endfn\s+(?P=symbol)\b[^\n]*"
)
INSTRUCTION = re.compile(r"(?m)^/\* ([0-9A-F]{8}) [0-9A-F]{8}\s")
SYMBOL = re.compile(
    r"^(\S+) = \.text:0x([0-9A-Fa-f]+); // type:function size:0x([0-9A-Fa-f]+)\b"
)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def symbols(root: Path, version: str) -> dict[str, tuple[int, int]]:
    path = root / "config" / version / "symbols.txt"
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = SYMBOL.match(line)
        if match:
            result[match.group(1)] = (int(match.group(2), 16), int(match.group(3), 16))
    return result


def candidates(root: Path, version: str, minimum: int, maximum: int, *, matched: bool = False) -> list[dict]:
    """Size-verified functions that are not exact yet, or (``matched``) only the exact ones."""
    known = symbols(root, version)
    config = json.loads((root / "build" / version / "config.json").read_text(encoding="utf-8"))
    active = {
        Path(str(unit["name"])).with_suffix("").as_posix()
        for unit in config["units"] if int(unit.get("code_size") or 0) > 0
    }
    target_project = m2c_context.target_project_id(root, version)
    exact = {
        row["symbol"] for row in
        m2c_context._report_exact_rows(m2c_context.ProjectSpec(target_project, root, version))
    }
    found = {}
    asm_root = root / "build" / version / "asm"
    for path in sorted(asm_root.rglob("*.s")):
        unit_name = path.relative_to(asm_root).with_suffix("").as_posix()
        if unit_name not in active:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in FUNCTION.finditer(text):
            symbol = match.group("symbol")
            expected = known.get(symbol)
            if (symbol in exact) != matched or expected is None or not minimum <= expected[1] <= maximum:
                continue
            addresses = [int(value, 16) for value in INSTRUCTION.findall(match.group())]
            if not addresses or addresses[0] != expected[0] or len(addresses) * 4 != expected[1]:
                continue
            if addresses != list(range(expected[0], expected[0] + expected[1], 4)):
                continue
            assembly = (".text\n.balign 4\n" + match.group() + "\n").encode("utf-8")
            key = (expected[0], symbol)
            found[key] = {"symbol": symbol, "address": expected[0], "size": expected[1],
                          "unit": unit_name, "assembly": assembly, "assembly_sha256": sha256(assembly)}
    return [found[key] for key in sorted(found)]


def spread(rows: list[dict], limit: int) -> list[dict]:
    """Sample across the DOL instead of taking only the first code region."""
    if limit <= 0 or limit >= len(rows):
        return rows
    return [rows[(index * len(rows) + len(rows) // 2) // limit] for index in range(limit)]


def candidate_priority(row: dict, reusable: set[tuple[str, int]]) -> tuple[int, list[str]]:
    """Cheap first-pass ranking; full corpus retrieval runs only for selected rows."""
    features = m2c_context.parse_assembly_features(row["assembly"].decode(errors="replace"), row["symbol"])
    score, reasons = 0, []
    if (row["symbol"], row["address"]) in reusable:
        score += 1000
        reasons.append("byte-identical cross-project source available")
    if 24 <= row["size"] <= 160:
        score += 40
        reasons.append("preferred 24-160 byte size")
    elif row["size"] <= 256:
        score += 12
    branches = features.get("branches") or {}
    if not branches.get("indirect_calls"):
        score += 20
        reasons.append("no unresolved indirect calls")
    field_count = sum(len(item.get("fields") or []) for item in features.get("placeholder_structs") or [])
    score += max(0, 16 - min(field_count, 16))
    calls = len(features.get("calls") or [])
    score += max(0, 12 - 2 * calls)
    if branches.get("conditional", 0) <= 3:
        score += 8
        reasons.append("simple control flow")
    row["selection_features"] = features
    row["selection_priority"] = {"score": score, "reasons": reasons}
    return score, reasons


def prioritized(rows: list[dict], limit: int, catalog: dict) -> list[dict]:
    reusable = set()
    target_project = str(catalog.get("target_project") or m2c_context.harness_paths.PROJECT_ID)
    for match in (catalog.get("cross_project_exact") or {}).get("matches") or []:
        address = (match.get("addresses") or {}).get(target_project)
        external = {key: value for key, value in (match.get("linked_sources") or {}).items()
                    if key != target_project}
        if not external or address is None:
            continue
        try:
            reusable.add((str(match.get("name")), int(str(address), 0)))
        except ValueError:
            continue
    for row in rows:
        candidate_priority(row, reusable)
    ordered = sorted(rows, key=lambda row: (-row["selection_priority"]["score"], row["size"],
                                            row["address"], row["symbol"]))
    return ordered if limit <= 0 else ordered[:limit]


def replay_rows(root: Path, version: str, batch: Path) -> list[dict]:
    """Reload a frozen target set while rechecking retail symbol boundaries."""
    data = json.loads((batch / "targets.json").read_text(encoding="utf-8"))
    identity = data.get("identity") or {}
    if identity.get("version") != version:
        raise ValueError(f"replay batch targets {identity.get('version')}, not {version}")
    retail_sha1 = (root / "config" / version / "build.sha1").read_text(encoding="ascii").split()[0]
    if identity.get("dol_sha1") != retail_sha1:
        raise ValueError("replay batch belongs to another retail DOL")
    known = symbols(root, version)
    rows = []
    for frozen in data.get("targets") or []:
        symbol = frozen["symbol"]
        expected = known.get(symbol)
        if expected != (int(frozen["address"]), int(frozen["size"])):
            raise ValueError(f"replay symbol boundary changed: {symbol}")
        assembly = (batch / "input" / f"{frozen['id']}.s").read_bytes()
        digest = sha256(assembly)
        if digest != frozen["assembly_sha256"]:
            raise ValueError(f"replay assembly hash changed: {symbol}")
        rows.append({"symbol": symbol, "address": expected[0], "size": expected[1],
                     "unit": frozen["unit"], "assembly": assembly, "assembly_sha256": digest})
    return rows


def run(root: Path, version: str, limit: int, jobs: int, timeout: int,
        minimum: int, maximum: int, force: bool = False,
        start: int | None = None, end: int | None = None, replay: Path | None = None,
        *, context_declarations: bool = False, readonly_data: bool = False,
        executors: list[str] | None = None, holdout: int = 0, seed: int = 0) -> dict:
    """Draft unmatched functions, or with ``holdout`` a seeded sample of already matched ones.

    Holdout batches benchmark the pipeline against functions whose matching C is known. The
    target's own source is never used as evidence, so the result shows what drafting would have
    achieved before those functions were matched.
    """
    if holdout and replay:
        raise ValueError("--holdout cannot be combined with --replay")
    if replay and readonly_data:
        raise ValueError("Readonly enrichment requires a fresh batch; legacy replays lack frozen owner data")
    root = root.resolve()
    bind_project_tools(root, prefer=True)
    execution = m2c_remote.configure(executors)
    if execution:
        jobs = m2c_remote.slots()
    check = build_check(root, version)
    if check["status"] != "exact":
        raise ValueError(f"{version}: byte-exact DOL build is required before drafting ({check['status']})")
    catalog = m2c_context.build_catalog(root, version)
    if replay:
        replay = replay.resolve()
        rows = replay_rows(root, version, replay)
    else:
        rows = [row for row in candidates(root, version, minimum, maximum, matched=holdout > 0)
                if (start is None or row["address"] >= start)
                and (end is None or row["address"] + row["size"] <= end)]
        if holdout:
            rows = sorted(random.Random(seed).sample(rows, min(holdout, len(rows))),
                          key=lambda row: row["address"])
            for row in rows:
                candidate_priority(row, set())
        else:
            rows = prioritized(rows, limit, catalog)
    if not rows:
        raise ValueError(f"{version}: no size-verified .text functions found")
    metadata = decompile.tool_metadata()
    executor_health = m2c_remote.verify_executors(metadata["fingerprint"])
    support = root / "build" / "decomp-support" / version
    identity = {"pipeline": PIPELINE_VERSION, "version": version, "dol_sha1": check["expected"],
                "context_declarations": context_declarations,
                "readonly_data": readonly_data,
                "readonly_owners": (decompile.m2c_readonly_data.owner_fingerprints(
                    root, version, [row["unit"] for row in rows]) if readonly_data else {}),
                "readonly_capture_sha256": (sha256(Path(decompile.m2c_readonly_data.__file__).read_bytes())
                                             if readonly_data else None),
                "symbols_sha256": sha256((root / "config" / version / "symbols.txt").read_bytes()),
                "tool_fingerprint": metadata["fingerprint"], "producer_sha256": sha256(Path(__file__).read_bytes()),
                "context_producer_sha256": sha256(Path(m2c_context.__file__).read_bytes()),
                "reference_index_fingerprint": (catalog.get("reference_index") or {}).get("fingerprint"),
                "cross_project_generated_at": (catalog.get("cross_project_exact") or {}).get("generated_at"),
                "baseline": replay.name if replay else "live",
                "selection": f"holdout:{seed}" if holdout else "unmatched",
                "address_range": [start, end],
                "targets": [(row["address"], row["size"], row["assembly_sha256"]) for row in rows]}
    run_id = sha256(json.dumps(identity, sort_keys=True).encode())[:20]
    folder = support / "batches" / run_id
    input_dir, result_dir = folder / "input", folder / "results"
    input_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    target_rows = []
    for row in rows:
        row_id = sha256(f"{version}:{row['address']:08X}:{row['assembly_sha256']}".encode())[:20]
        row["id"] = row_id
        assembly_path = input_dir / f"{row_id}.s"
        if assembly_path.exists() and sha256(assembly_path.read_bytes()) != row["assembly_sha256"]:
            raise ValueError(f"Immutable input changed: {assembly_path}")
        if not assembly_path.exists():
            assembly_path.write_bytes(row["assembly"])
        target_rows.append({key: value for key, value in row.items() if key != "assembly"})
    decompile.write_json(folder / "targets.json", {"identity": identity, "targets": target_rows})
    results = {}
    for row in rows:
        path = result_dir / f"{row['id']}.json"
        if path.is_file() and not force:
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("assembly_sha256") == row["assembly_sha256"]:
                    results[row["id"]] = previous
            except (OSError, ValueError):
                pass
    asm_inputs.jump_tables(root, version)

    def checkpoint(finished: bool) -> dict:
        if finished and readonly_data and identity["readonly_owners"] != decompile.m2c_readonly_data.owner_fingerprints(
                root, version, [row["unit"] for row in rows]):
            raise ValueError("Readonly owner inputs changed during generation; batch remains unfinished")
        summary = {"run_id": run_id, "selection": "holdout" if holdout else "unmatched", "pipeline": {"version": PIPELINE_VERSION,
                   "model_calls": 0, "role": "Typed m2c hypotheses with matched references, exact reuse, and compiler evidence",
                   "reference_pool": len((catalog.get("reference_index") or {}).get("functions") or []),
                   "reference_projects": [row.get("name") for row in catalog.get("reference_projects") or []]},
                   "tool": metadata, "version": version, "workspace": str(root),
                   "report_sha256": identity["symbols_sha256"], "updated_at": datetime.now(UTC).isoformat(),
                   "total": len(rows), "processed": len(results), "jobs": jobs, "finished": finished,
                   "executors": execution or [{"executor": "local", "slots": jobs}],
                   "executor_health": executor_health,
                   "counts": dict(Counter(row["status"] for row in results.values())),
                   "context_modes": dict(Counter(row.get("context_mode", "none") for row in results.values())),
                   "folder": str(folder), "baseline": replay.name if replay else "live"}
        decompile.write_json(folder / "manifest.json", summary)
        if finished:
            ordered = [results[row["id"]] for row in rows]
            decompile.write_json(folder / "results.json", {"summary": summary, "results": ordered})
            m2c_report.refresh_report(support, None)
        return summary

    checkpoint(False)

    def translate(row: dict) -> dict:
        label = str(input_dir / f"{row['id']}.s")
        knowledge = m2c_context.build_knowledge(root, version, row, row["assembly"], catalog=catalog)
        common = {**{key: value for key, value in row.items() if key != "assembly"},
                  "features": knowledge["features"], "references": knowledge["references"],
                  "compiler_profile": knowledge["compiler_profile"],
                  "exact_reuse": knowledge["exact_reuse"], "bootstrap": knowledge["bootstrap"],
                  "priority": knowledge["priority"]}
        if knowledge["exact_reuse"]:
            reuse_key = sha256(json.dumps(knowledge["exact_reuse"], sort_keys=True).encode())
            reuse_folder = support / "exact-reuse" / reuse_key
            reuse_folder.mkdir(parents=True, exist_ok=True)
            snippet = knowledge["exact_reuse"].get("source_snippet") or ""
            (reuse_folder / "reuse.c").write_text(snippet + ("\n" if snippet else ""), encoding="utf-8")
            decompile.write_json(reuse_folder / "knowledge.json", knowledge)
            return {**common, "status": "exact_reuse", "context_mode": "exact_reuse",
                    "artifacts": str(reuse_folder), "draft_chars": 0,
                    "diagnostics": {}, "source": knowledge["exact_reuse"].get("source_path"),
                    "auto_generated": False, "pct": None,
                    "error": "m2c bypassed: retail-byte-identical source is available"}
        result = decompile.translate(root, row["symbol"], row["assembly"], label,
                                     knowledge["context"], support / "drafts", metadata, timeout, force,
                                     explicit_fields=True, infer_unknowns=True, deterministic_vars=True,
                                     version=version, context_declarations=context_declarations,
                                     readonly_owner=(decompile.m2c_readonly_data.owner_path(root, version, row["unit"])
                                                     if readonly_data else None))
        artifact_folder = Path(result["artifacts"])
        if artifact_folder.is_dir():
            decompile.write_json(artifact_folder / "knowledge.json", knowledge)
        return {**common,
                **{key: value for key, value in result.items() if key not in {"draft", "command", "context_command"}},
                "draft_chars": len(result.get("draft", "")), "source": None, "auto_generated": True,
                "pct": None}

    pending = [row for row in rows if row["id"] not in results]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(translate, row): row for row in pending}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {**{key: value for key, value in row.items() if key != "assembly"},
                          "status": "batch_error", "error": f"{type(error).__name__}: {error}"}
            results[row["id"]] = result
            decompile.write_json(result_dir / f"{row['id']}.json", result)
            summary = checkpoint(False)
            print(f"{summary['processed']}/{summary['total']} {row['symbol']}: {result['status']}", flush=True)
    return checkpoint(True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=VERSION,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--limit", type=int, default=48, help="priority-ranked function count; 0 drafts all eligible functions")
    parser.add_argument("--replay", type=Path, help="regenerate a frozen batch target set with the current pipeline")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--min-bytes", type=int, default=24)
    parser.add_argument("--max-bytes", type=int, default=256)
    parser.add_argument("--start", type=lambda value: int(value, 0), help="inclusive function address")
    parser.add_argument("--end", type=lambda value: int(value, 0), help="exclusive function end address")
    parser.add_argument("--force", action="store_true", help="regenerate this run's existing drafts")
    parser.add_argument("--holdout", type=int, default=0, metavar="N",
                        help="benchmark: draft a seeded sample of N already matched functions instead")
    parser.add_argument("--seed", type=int, default=0, help="sample seed for --holdout")
    parser.add_argument("--context-declarations", action="store_true", help="emit referenced context declarations (requires m2c patch 0015)")
    parser.add_argument("--readonly-data", action="store_true", help="capture referenced readonly bytes from the target owner")
    parser.add_argument("--executor", action="append", default=[], metavar="EXECUTOR=SLOTS",
                        help="weighted execution slots, for example local=2 or http://127.0.0.1:18766=8")
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    if (args.limit < 0 or args.jobs < 1 or args.timeout < 1 or args.min_bytes < 4
            or args.max_bytes < args.min_bytes or (args.start is not None and args.end is not None
                                                  and args.start >= args.end)):
        parser.error("invalid limit, jobs, timeout, or function size")
    if args.replay and (args.start is not None or args.end is not None):
        parser.error("--replay cannot be combined with an address range")
    summary = run(args.root, args.version, args.limit, args.jobs, args.timeout,
                  args.min_bytes, args.max_bytes, args.force, args.start, args.end, args.replay,
                  context_declarations=args.context_declarations, readonly_data=args.readonly_data,
                  executors=args.executor or None, holdout=args.holdout, seed=args.seed)
    print(json.dumps({key: summary[key] for key in ("run_id", "version", "total", "counts", "folder")}, indent=2))


if __name__ == "__main__":
    main()

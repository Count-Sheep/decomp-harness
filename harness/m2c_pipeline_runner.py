#!/usr/bin/env python3
"""Run a durable m2c generation/validation pipeline with a pinned stage journal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import m2c_drafts
import m2c_validate_drafts
import local_campaign_decompile as decompile
from harness_paths import DECOMP_ROOT, VERSION


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def run(root: Path, version: str, *, jobs: int = 1, timeout: int = 120,
        minimum: int = 4, maximum: int = 65536, context_declarations: bool = True,
        readonly_data: bool = True, executors: list[str] | None = None,
        validation_jobs: int = 3, refinement_limit: int = 24,
        source_variants: int = 64, native_variants: int = 3,
        auto_promote: bool = False) -> dict:
    root = root.resolve()
    support = root / "build" / "decomp-support" / version
    state_path = support / "pipeline-state.json"
    tool = decompile.tool_metadata()
    config = {"root": str(root), "version": version, "limit": 0, "jobs": jobs,
              "timeout": timeout, "minimum": minimum, "maximum": maximum,
              "context_declarations": context_declarations, "readonly_data": readonly_data,
              "executors": executors or [], "validation_jobs": validation_jobs,
              "refinement_limit": refinement_limit, "source_variants": source_variants,
              "native_variants": native_variants, "auto_promote": auto_promote,
              "tool_revision": tool["revision"], "tool_fingerprint": tool["fingerprint"]}
    config_sha256 = fingerprint(config)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if state.get("status") == "active" and state.get("config_sha256") != config_sha256:
        raise ValueError("Active pipeline state belongs to a different configuration")
    if state.get("status") != "active":
        state = {"schema_version": 1, "status": "active", "stage": "generation",
                 "config": config, "config_sha256": config_sha256,
                 "started_at": datetime.now(UTC).isoformat()}

    def checkpoint(**changes) -> None:
        state.update(changes, updated_at=datetime.now(UTC).isoformat())
        write_json(state_path, state)

    checkpoint(last_error=None)
    try:
        if state["stage"] == "generation":
            generated = m2c_drafts.run(root, version, 0, jobs, timeout, minimum, maximum,
                context_declarations=context_declarations, readonly_data=readonly_data,
                executors=executors)
            checkpoint(stage="validation", batch=generated["folder"],
                       generation_run_id=generated["run_id"])
        validated = m2c_validate_drafts.run(root, version, Path(state["batch"]),
            jobs=validation_jobs, refinement_limit=refinement_limit,
            source_variants=source_variants, native_variants=native_variants,
            auto_promote=auto_promote)
        checkpoint(status="complete", stage="complete", validation=validated,
                   finished_at=datetime.now(UTC).isoformat())
        return state
    except BaseException as error:
        checkpoint(last_error=f"{type(error).__name__}: {error}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=VERSION,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--min-bytes", type=int, default=4)
    parser.add_argument("--max-bytes", type=int, default=65536)
    parser.add_argument("--context-declarations", action="store_true")
    parser.add_argument("--readonly-data", action="store_true")
    parser.add_argument("--executor", action="append", default=[])
    parser.add_argument("--validation-jobs", type=int, default=3)
    parser.add_argument("--refinement-limit", type=int, default=24)
    parser.add_argument("--source-variants", type=int, default=64)
    parser.add_argument("--native-variants", type=int, default=3)
    parser.add_argument("--auto-promote", action="store_true")
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    result = run(args.root, args.version, jobs=args.jobs, timeout=args.timeout,
        minimum=args.min_bytes, maximum=args.max_bytes,
        context_declarations=args.context_declarations, readonly_data=args.readonly_data,
        executors=args.executor or None, validation_jobs=args.validation_jobs,
        refinement_limit=args.refinement_limit, source_variants=args.source_variants,
        native_variants=args.native_variants, auto_promote=args.auto_promote)
    print(json.dumps({key: result.get(key) for key in ("status", "stage", "batch", "finished_at")}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Use Claude Agent SDK to propose general m2c improvements in an isolated worktree.

The agent may edit m2c and its tests. Frozen corpus A/B results, not the agent,
decide whether the resulting patch is worth review.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable

import harness_paths
import m2c_generation_experiment as experiment


DEFAULT_MODEL = "claude-sonnet-5"
ENV_FILE = harness_paths.REPO_ROOT / ".env"
DEFAULT_BASE = harness_paths.REPO_ROOT / "build/tools/m2c"
DEFAULT_OUTPUT_ROOT = harness_paths.REPO_ROOT / "build/decomp-support/claude-m2c-engineer"
ALLOWED_PREFIXES = ("m2c/", "m2c_pycparser/", "tests/")
SYSTEM_PROMPT = """You are improving the general-purpose m2c decompiler, not solving one
project's function. Work only in the isolated m2c checkout supplied as your cwd. Diagnose the
failure cluster from the frozen examples, implement the smallest general correction, and add
focused regression tests. Do not add target symbols, project-specific addresses, expected-output
special cases, pragmas, inline assembly, or test bypasses. Do not modify generated corpus data.
Do not commit changes; the harness captures the worktree patch.
Make a concrete implementation and focused test early; do not spend the full session only exploring.
Use the supplied code-ownership hints first and edit within eight tool calls when a general fix exists.
The harness runs tests and compiler tools after you return; do not search for external compilers.
The external harness will independently A/B your patch on
unseen exact controls and rejects every compile, score, or instruction-exact regression."""


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def command(args: list[str], *, cwd: Path, timeout: int = 300, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
    return result


def load_api_key(path: Path = ENV_FILE) -> bool:
    """Load only ANTHROPIC_API_KEY from an owner-private ignored env file.

    POSIX permission bits are enforced where they exist; Windows relies on the file's ACL.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if not path.is_file():
        return False
    if not harness_paths.WINDOWS and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"refusing credential file with group/other permissions: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ANTHROPIC_API_KEY":
            value = value.strip().strip("\"'")
            if value:
                os.environ["ANTHROPIC_API_KEY"] = value
                return True
    return False


@dataclass
class AgentReply:
    text: str
    cost_usd: float = 0.0
    session_id: str | None = None
    subtype: str | None = None
    usage: dict[str, Any] | None = None


class AgentCallError(RuntimeError):
    def __init__(self, message: str, *, cost_usd: float = 0.0, session_id: str | None = None):
        super().__init__(message)
        self.cost_usd = cost_usd
        self.session_id = session_id


async def _query_sdk(prompt: str, root: Path, model: str, budget_usd: float) -> AgentReply:
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                      TextBlock, query)
    except ImportError as exc:
        raise AgentCallError(
            "claude-agent-sdk is not installed; install harness/requirements-agent-sdk.txt"
        ) from exc
    if not load_api_key():
        raise AgentCallError("ANTHROPIC_API_KEY is not set")
    options = ClaudeAgentOptions(
        model=model,
        cwd=root,
        tools=["Read", "Glob", "Grep", "Edit", "Write"],
        allowed_tools=["Read", "Glob", "Grep", "Edit", "Write"],
        disallowed_tools=["Bash", "WebSearch", "WebFetch", "Agent", "NotebookEdit"],
        permission_mode="acceptEdits",
        setting_sources=[],
        system_prompt=SYSTEM_PROMPT,
        max_turns=18,
        max_budget_usd=budget_usd,
        max_thinking_tokens=2048,
        effort="low",
        verbatim_prompts=True,
        env={"CLAUDE_AGENT_SDK_CLIENT_APP": "decomp-harness-m2c-engineer"},
    )
    chunks: list[str] = []
    result = None
    failure = None
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                chunks.extend(block.text for block in message.content if isinstance(block, TextBlock))
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        failure = exc
    if result is None:
        raise AgentCallError("Claude Agent SDK ended without a result message") from failure
    text = result.result if isinstance(result.result, str) and result.result.strip() else "\n".join(chunks)
    if result.is_error and result.subtype == "error_max_turns":
        return AgentReply(text or "SDK turn limit reached; evaluate the current worktree.",
                          float(result.total_cost_usd or 0), result.session_id,
                          result.subtype, result.usage if isinstance(result.usage, dict) else None)
    if result.is_error or not text.strip():
        raise AgentCallError(f"Claude Agent SDK failed ({result.subtype}): {text[-1000:]}",
                             cost_usd=float(result.total_cost_usd or 0),
                             session_id=result.session_id) from failure
    usage = result.usage if isinstance(result.usage, dict) else None
    return AgentReply(text, float(result.total_cost_usd or 0), result.session_id,
                      result.subtype, usage)


def query_sdk(prompt: str, root: Path, model: str, budget_usd: float) -> AgentReply:
    return asyncio.run(_query_sdk(prompt, root, model, budget_usd))


def bounded(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def select_rows(snapshot: dict[str, Any], categories: list[str], limit: int,
                offset: int = 0, excluded_categories: list[str] | None = None) -> list[dict[str, Any]]:
    excluded = set(excluded_categories or [])
    rows = [row for row in snapshot.get("rows", [])
            if (not categories or set(categories) & set(row.get("categories", [])))
            and not excluded & set(row.get("categories", []))]
    rows.sort(key=lambda row: (len(row.get("categories", [])), int(row.get("size") or 0),
                               row.get("symbol", "")))
    return rows[offset:offset + limit] if limit else rows[offset:]


def ownership_hints(base: Path, rows: list[dict[str, Any]]) -> str:
    tokens = set()
    for row in rows:
        tokens.update(match.group(1) for match in
                      re.finditer(r"\*/\s*([A-Za-z_][\w.]*)", row.get("assembly", "")))
        for message in row.get("compiler_messages", []):
            tokens.update(re.findall(r"'([A-Za-z_]\w*)'", message))
    # Common instructions and generated temporary names are poor ownership signals.
    tokens -= {"blr", "addi", "stwu", "sp8", "r0", "r1", "r3", "f0", "f1"}
    matches = []
    roots = [base / "m2c", base / "m2c_pycparser", base / "tests/unit"]
    for path in sorted(candidate for root in roots if root.is_dir() for candidate in root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        found = sorted(token for token in tokens if token in text)
        if not found:
            continue
        lines = text.splitlines()
        excerpts = []
        for number, line in enumerate(lines, 1):
            if any(token in line for token in found):
                excerpts.append(f"{number}: {line.strip()}")
            if len(excerpts) == 4:
                break
        matches.append(f"- {path.relative_to(base).as_posix()} ({', '.join(found)}): " + " | ".join(excerpts))
        if len(matches) == 12:
            break
    return "\n".join(matches) or "No direct mnemonic/diagnostic ownership matches were found."


def build_dossier(snapshot_path: Path, categories: list[str], limit: int,
                  base: Path | None = None, offset: int = 0,
                  excluded_categories: list[str] | None = None) -> tuple[str, list[str]]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = select_rows(snapshot, categories, limit, offset, excluded_categories)
    return dossier_from_rows(snapshot_path.name, rows, base)


def dossier_from_rows(snapshot_name: str, rows: list[dict[str, Any]],
                      base: Path | None = None) -> tuple[str, list[str]]:
    counts = Counter(category for row in rows for category in row.get("categories", []))
    parts = [
        "# Current-base failure cluster",
        f"Snapshot: {snapshot_name}",
        f"Selected rows: {len(rows)}",
        f"Categories: {json.dumps(dict(counts.most_common()), sort_keys=True)}",
    ]
    for row in rows:
        outcome = row.get("outcome") or {}
        parts.extend([
            f"\n## {row.get('symbol')} ({row.get('size')} bytes)",
            f"Failure categories: {', '.join(row.get('categories', [])) or 'none'}",
            f"Historical categories: {', '.join(row.get('historical_categories', [])) or 'none'}",
            f"Compiler messages: {bounded(row.get('compiler_messages', []), 1200)}",
            f"Current compiled/score: {bool(outcome.get('compiled'))} / {outcome.get('pct')}",
            f"Current instruction exact: {row.get('evidence', {}).get('instruction_exact')}",
            f"Current instruction comparison: {bounded(row.get('evidence', {}).get('comparison', {}), 2500)}",
            "### Current m2c output\n```c\n" + bounded(row.get("draft", ""), 5000) + "\n```",
            "### Context excerpts\n```c\n" + bounded(row.get("context", ""), 3500) + "\n```",
            "### Target assembly\n```asm\n" + bounded(row.get("assembly", ""), 5000) + "\n```",
        ])
    if base is not None:
        parts.extend(["\n# Likely code ownership", ownership_hints(base, rows)])
    return "\n".join(parts), [row.get("symbol", "") for row in rows]


def refresh_current_rows(snapshot: Path, base: Path, rows: list[dict[str, Any]],
                         output: Path) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Regenerate selected rows with the current base before spending model credits."""
    symbols = [row["symbol"] for row in rows]
    experiment.run(snapshot, base, base, output, [], 0, 0,
                   baseline_context_declarations=True, symbols=symbols)
    report = json.loads((output / "results.json").read_text(encoding="utf-8"))
    historical = {row["symbol"]: row for row in rows}
    refreshed = []
    for index, result in enumerate(report.get("results", [])):
        symbol = result["symbol"]
        old = historical[symbol]
        measured = result["baseline"]
        draft = (output / f"{index:05d}" / "baseline" / "draft.c").read_text(encoding="utf-8")
        refreshed.append({**old, "historical_categories": old.get("categories", []),
                          "draft": draft, "outcome": measured.get("outcome", {}),
                          "categories": measured.get("categories", []),
                          "compiler_messages": measured.get("compiler_messages", []),
                          "evidence": measured.get("evidence", {})})
    if {row["symbol"] for row in refreshed} != set(symbols):
        raise RuntimeError("current-base preflight did not return every selected symbol")
    skipped = [row["symbol"] for row in refreshed
               if row.get("evidence", {}).get("instruction_exact") is True]
    active = [row for row in refreshed if row["symbol"] not in set(skipped)]
    return active, skipped, report.get("summary", {})


def changed_paths(root: Path) -> list[str]:
    result = command(["git", "status", "--porcelain=v1", "-z"], cwd=root, check=True)
    paths = []
    fields = result.stdout.split("\0")
    index = 0
    while index < len(fields) and fields[index]:
        field = fields[index]
        status, path = field[:2], field[3:]
        paths.append(path)
        index += 1
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                paths.append(fields[index])
                index += 1
    return sorted(set(paths))


def disallowed_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if not path.startswith(ALLOWED_PREFIXES)]


def patch_text(root: Path, paths: list[str]) -> str:
    """Include untracked tests in the review patch without committing them."""
    if paths:
        command(["git", "add", "-N", "--", *paths], cwd=root, check=True)
    return command(["git", "diff", "--binary", "HEAD"], cwd=root, check=True).stdout


def run_unit_tests(root: Path) -> dict[str, Any]:
    started = timestamp()
    try:
        result = command([sys.executable, "-m", "unittest", "discover",
                          "-s", "tests/unit"], cwd=root, timeout=300)
        return {"started_at": started, "finished_at": timestamp(), "returncode": result.returncode,
                "output": (result.stdout + result.stderr)[-12000:]}
    except subprocess.TimeoutExpired as exc:
        return {"started_at": started, "finished_at": timestamp(), "returncode": -1,
                "output": f"unit tests timed out after {exc.timeout}s"}


def assess_ab(path: Path) -> dict[str, Any]:
    report = json.loads((path / "results.json").read_text(encoding="utf-8"))
    score_gains, score_losses, compile_gains, compile_losses = [], [], [], []
    exact_gains, exact_losses = [], []
    for row in report.get("results", []):
        old, new = row["baseline"], row["candidate"]
        old_compiled = bool(old.get("outcome", {}).get("compiled"))
        new_compiled = bool(new.get("outcome", {}).get("compiled"))
        old_pct = float(old.get("outcome", {}).get("pct") or 0)
        new_pct = float(new.get("outcome", {}).get("pct") or 0)
        old_exact = old.get("evidence", {}).get("instruction_exact") is True
        new_exact = new.get("evidence", {}).get("instruction_exact") is True
        symbol = row["symbol"]
        if not old_compiled and new_compiled:
            compile_gains.append(symbol)
        if old_compiled and not new_compiled:
            compile_losses.append(symbol)
        if new_pct > old_pct + 1e-6:
            score_gains.append({"symbol": symbol, "before": old_pct, "after": new_pct})
        if new_pct + 1e-6 < old_pct:
            score_losses.append({"symbol": symbol, "before": old_pct, "after": new_pct})
        if not old_exact and new_exact:
            exact_gains.append(symbol)
        if old_exact and not new_exact:
            exact_losses.append(symbol)
    losses = bool(compile_losses or score_losses or exact_losses)
    gains = bool(compile_gains or score_gains or exact_gains)
    return {"admitted": gains and not losses, "has_gain": gains, "has_loss": losses,
            "compile_gains": compile_gains, "compile_losses": compile_losses,
            "score_gains": score_gains, "score_losses": score_losses,
            "exact_gains": exact_gains, "exact_losses": exact_losses,
            "summary": report.get("summary", {})}


def initial_prompt(dossier: str) -> str:
    return f"""Inspect this failure cluster and the m2c implementation in your cwd. Find one
general root cause shared by these examples, implement a minimal correction, and add focused tests.
The harness will run them. The examples are evidence, never keys for special casing.

{dossier}
"""


def review_prompt(dossier: str, assessment: dict[str, Any], tests: dict[str, Any]) -> str:
    return f"""Your first m2c patch failed deterministic admission. Inspect the current worktree
and revise or revert it so it produces a general improvement with no regressions. Do not stack
special cases. Add or improve a focused regression test; the harness will run it.

## Unit-test result
{bounded(tests, 5000)}

## Frozen A/B result
{bounded(assessment, 7000)}

{dossier}
"""


def create_worktree(base: Path, target: Path) -> str:
    if command(["git", "status", "--porcelain"], cwd=base, check=True).stdout.strip():
        raise RuntimeError(f"m2c base must be clean: {base}")
    revision = command(["git", "rev-parse", "HEAD"], cwd=base, check=True).stdout.strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    command(["git", "worktree", "add", "--detach", str(target), revision], cwd=base, check=True)
    return revision


def remove_worktree(base: Path, target: Path) -> None:
    command(["git", "worktree", "remove", "--force", str(target)], cwd=base)
    if target.exists():
        shutil.rmtree(target)


def run(snapshot: Path, base: Path, output: Path, *, categories: list[str], limit: int,
        controls: int, model: str, budget_usd: float, review_budget_usd: float,
        keep_worktree: bool = False, offset: int = 0,
        excluded_categories: list[str] | None = None,
        call_agent: Callable[[str, Path, str, float], AgentReply] = query_sdk) -> dict[str, Any]:
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    frozen = json.loads(snapshot.read_text(encoding="utf-8"))
    selected = select_rows(frozen, categories, limit, offset, excluded_categories)
    if not selected:
        raise ValueError("no snapshot rows match the requested cluster")
    if command(["git", "status", "--porcelain"], cwd=base, check=True).stdout.strip():
        raise RuntimeError(f"m2c base must be clean: {base}")
    revision = command(["git", "rev-parse", "HEAD"], cwd=base, check=True).stdout.strip()
    output.mkdir(parents=True)
    worktree = output / "worktree"
    selected_symbols = [row["symbol"] for row in selected]
    record: dict[str, Any] = {"schema_version": 1, "started_at": timestamp(),
        "snapshot": str(snapshot), "snapshot_sha256": digest(snapshot.read_bytes()),
        "base": str(base), "base_revision": revision, "categories": categories,
        "excluded_categories": excluded_categories or [], "selected_symbols": selected_symbols,
        "symbols": selected_symbols,
        "offset": offset, "exact_controls": controls, "model": model,
        "attempts": [], "spent_usd": 0.0, "status": "running"}
    write_json(output / "result.json", record)
    worktree_created = False
    try:
        rows, skipped, preflight = refresh_current_rows(
            snapshot, base, selected, output / "preflight")
        symbols = [row["symbol"] for row in rows]
        record["symbols"] = symbols
        record["preflight"] = {"summary": preflight, "skipped_exact": skipped,
                               "eligible": symbols}
        write_json(output / "result.json", record)
        if not rows:
            record["status"] = "preflight_exact"
            return record
        dossier, _ = dossier_from_rows(snapshot.name, rows, base)
        (output / "dossier.md").write_text(dossier + "\n", encoding="utf-8")
        create_worktree(base, worktree)
        worktree_created = True
        prompts = [initial_prompt(dossier)]
        budgets = [budget_usd, review_budget_usd]
        for index, (prompt, call_budget) in enumerate(zip(prompts, budgets), 1):
            attempt_dir = output / f"attempt-{index:02d}"
            attempt_dir.mkdir()
            (attempt_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            try:
                reply = call_agent(prompt, worktree, model, call_budget)
                (attempt_dir / "response.md").write_text(reply.text + "\n", encoding="utf-8")
                record["spent_usd"] += reply.cost_usd
                agent_result = {"cost_usd": reply.cost_usd, "session_id": reply.session_id,
                                "subtype": reply.subtype, "usage": reply.usage}
            except AgentCallError as exc:
                record["spent_usd"] += exc.cost_usd
                record["attempts"].append({"index": index, "agent_error": str(exc),
                                           "cost_usd": exc.cost_usd, "session_id": exc.session_id})
                record["status"] = "agent_error"
                break
            paths = changed_paths(worktree)
            forbidden = disallowed_paths(paths)
            tests = run_unit_tests(worktree) if paths and not forbidden else {
                "returncode": -1, "output": "no patch" if not paths else f"disallowed paths: {forbidden}"}
            patch = patch_text(worktree, paths)
            (attempt_dir / "candidate.patch").write_text(patch, encoding="utf-8")
            assessment: dict[str, Any] = {"admitted": False, "reason": "patch policy or tests failed"}
            if paths and not forbidden and tests["returncode"] == 0:
                ab_dir = attempt_dir / "ab"
                try:
                    experiment.run(snapshot, base, worktree, ab_dir, categories, limit, controls,
                                   baseline_context_declarations=True, symbols=symbols)
                    assessment = assess_ab(ab_dir)
                except Exception as exc:
                    assessment = {"admitted": False,
                                  "evaluation_error": f"{type(exc).__name__}: {exc}"}
            attempt = {"index": index, "agent": agent_result, "changed_paths": paths,
                       "disallowed_paths": forbidden, "tests": tests, "assessment": assessment}
            record["attempts"].append(attempt)
            write_json(output / "result.json", record)
            if assessment.get("admitted"):
                (output / "accepted.patch").write_text(patch, encoding="utf-8")
                record["status"] = "accepted_for_review"
                break
            if not paths:
                record["status"] = "no_patch"
                break
            if assessment.get("evaluation_error"):
                record["status"] = "evaluation_error"
                break
            if index == 1 and review_budget_usd > 0:
                prompts.append(review_prompt(dossier, assessment, tests))
            else:
                record["status"] = "rejected"
        if record["status"] == "running":
            record["status"] = "rejected"
    except Exception as exc:
        record["status"] = "evaluation_error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if record["status"] == "running":
            record["status"] = "interrupted"
        record["finished_at"] = timestamp()
        write_json(output / "result.json", record)
        if worktree_created and not keep_worktree:
            remove_worktree(base, worktree)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, nargs="?")
    parser.add_argument("--base", type=Path, default=harness_paths.tool_path("m2c", DEFAULT_BASE),
                        help="clean m2c git checkout to branch worktrees from "
                             "(default: tools.m2c / DECOMP_M2C, else build/tools/m2c)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--exclude-category", action="append", default=[])
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=1,
                        help="run consecutive non-overlapping clusters")
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--exact-controls", type=int, default=12)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--review-budget-usd", type=float, default=3.0)
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument("--plan", action="store_true", help="write/print the dossier without spending")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if (args.limit < 1 or args.iterations < 1 or args.start_offset < 0 or
            args.exact_controls < 0 or args.budget_usd <= 0 or args.review_budget_usd < 0):
        parser.error("limits and budgets are outside their valid range")
    base = args.base.resolve()
    if args.check:
        try:
            import claude_agent_sdk  # noqa: F401
            sdk = True
        except ImportError:
            sdk = False
        print(json.dumps({"sdk": sdk, "api_key": load_api_key(), "base": str(base),
                          "base_clean": not bool(command(["git", "status", "--porcelain"], cwd=base).stdout.strip())},
                         indent=2))
        return
    if args.snapshot is None:
        parser.error("snapshot is required unless --check is used")
    snapshot = args.snapshot.resolve()
    if args.plan:
        dossier, symbols = build_dossier(snapshot, args.category, args.limit, base,
                                         args.start_offset, args.exclude_category)
        if args.output:
            args.output.write_text(dossier + "\n", encoding="utf-8")
        print(json.dumps({"symbols": symbols, "dossier_chars": len(dossier)}, indent=2))
        return
    output = (args.output or (DEFAULT_OUTPUT_ROOT /
                              datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))).resolve()
    if args.iterations == 1:
        result = run(snapshot, base, output, categories=args.category, limit=args.limit,
                     controls=args.exact_controls, model=args.model, budget_usd=args.budget_usd,
                     review_budget_usd=args.review_budget_usd, keep_worktree=args.keep_worktree,
                     offset=args.start_offset, excluded_categories=args.exclude_category)
        summary = {"status": result["status"], "spent_usd": result["spent_usd"],
                   "output": str(output)}
    else:
        output.mkdir(parents=True, exist_ok=False)
        campaign = {"schema_version": 1, "started_at": timestamp(), "snapshot": str(snapshot),
                    "category": args.category, "exclude_category": args.exclude_category,
                    "limit": args.limit,
                    "requested_iterations": args.iterations, "iterations": [], "spent_usd": 0.0}
        write_json(output / "campaign.json", campaign)
        for index in range(args.iterations):
            offset = args.start_offset + index * args.limit
            if not select_rows(json.loads(snapshot.read_text(encoding="utf-8")),
                               args.category, args.limit, offset, args.exclude_category):
                break
            result = run(snapshot, base, output / f"iteration-{index + 1:03d}",
                         categories=args.category, limit=args.limit, controls=args.exact_controls,
                         model=args.model, budget_usd=args.budget_usd,
                         review_budget_usd=args.review_budget_usd,
                         keep_worktree=args.keep_worktree, offset=offset,
                         excluded_categories=args.exclude_category)
            campaign["iterations"].append({"index": index + 1, "offset": offset,
                                           "status": result["status"],
                                           "spent_usd": result["spent_usd"],
                                           "symbols": result["symbols"]})
            campaign["spent_usd"] += result["spent_usd"]
            write_json(output / "campaign.json", campaign)
        campaign["finished_at"] = timestamp()
        write_json(output / "campaign.json", campaign)
        summary = {"status": "complete", "iterations": len(campaign["iterations"]),
                   "spent_usd": campaign["spent_usd"], "output": str(output)}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

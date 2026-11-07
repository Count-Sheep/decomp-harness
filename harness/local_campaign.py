#!/usr/bin/env python3
"""Local-LLM queue runner for strict, review-only decompilation proposals.

The runner asks a local model server (Ollama, or any OpenAI-compatible `/v1`
endpoint) for a single replacement function and retains it in ignored campaign
state. Verification temporarily substitutes source under the shared build lock
and restores it afterward. A 100% result remains a review candidate; source is
only committed when `run --promote` is given explicitly.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from local_campaign_coordination import ClaimConflict, Coordinator, replace_file
from local_campaign_freshness import freshness
from local_campaign_priority import rank, reference_graph
import harness_paths
import portable
import local_campaign_corpus as corpus
import local_campaign_decompile as decompile
import local_campaign_m2c_report as m2c_report
import local_campaign_templates as templates
import local_campaign_permute as permute
import local_campaign_rewrite as rw
import local_campaign_colouring as colouring
import local_campaign_investigation as investigation
import local_campaign_obligations as obligations
import local_campaign_evidence_cache as evidence_cache

SCRATCH_CACHE = evidence_cache.EvidenceCache()
HISTORY_READ_AHEAD = evidence_cache.ReadAhead()
COMMAND_SECONDS: dict[str, float] = {}

from harness_paths import DECOMP_ROOT as ROOT, REPO_ROOT, VERSION  # the decomp this harness drives (see harness_paths.py)
STATE_DIR = ROOT / "build" / "local_llm_campaign"
STATE_FILE = STATE_DIR / "state.json"
CANDIDATES_DIR: Path | None = None  # a bench run points this (and STATE_FILE) at its own directory
REPORT_FILE = ROOT / "build" / VERSION / "report.json"
# Optional project-provided helpers (configure `tools.brief` / `tools.verify`, or DECOMP_BRIEF / DECOMP_VERIFY).
# Without the brief tool the prompt's brief is built from the scratch objdiff measurement alone.
BRIEF_TOOL = harness_paths.tool_path("brief", ROOT / "tools" / "decomp_work" / "handoff" / "gen_brief.py")
VERIFY_TOOL = harness_paths.tool_path("verify", ROOT / "tools" / "decomp_work" / "handoff" / "verify.py")
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = harness_paths.env("MODEL", "local-model")  # DECOMP_MODEL
DEFAULT_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "4096"))
DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
COORDINATOR = Coordinator(STATE_DIR)
MODEL_WORKER = "Local LLM"
DEFAULT_WORKER = os.environ.get("LOCAL_CAMPAIGN_WORKER", MODEL_WORKER)
RUNNER_LOCKS: dict[str, Any] = {}
STOP_REQUESTED = False
NUM_CTX = DEFAULT_NUM_CTX
HOST_BACKOFF = (30, 600)
STALL_SECONDS = 600  # longest silence tolerated on an Ollama stream before the attempt is abandoned
THINK: bool | None = None  # None: let the model decide; thinking models reason unless told otherwise
DEFAULT_REASONING_TOKENS = 2048
# Sampling profile (e.g. a model card's recommended settings). FIXED_TEMPERATURE replaces the retry schedule,
# SAMPLING adds request fields (top_p, top_k, min_p, presence_penalty, repeat_penalty), and TEMPLATE_KWARGS adds
# chat-template options such as reasoning_effort. Empty/None keeps the harness defaults.
FIXED_TEMPERATURE: float | None = None
# False gives every task its full RETRIES budget: no stop after STALL_ROUNDS without a new best, and none on an
# unchanged register-only answer. The bench uses it so every model gets the same number of attempts.
EARLY_STOP = True
SEED_SALT = ""
# Set once a response streams reasoning: under --think auto a model that reasons by default then gets the
# reasoning allowance too, instead of spending its whole output cap thinking and never answering.
REASONING_SEEN = False  # varies the per-attempt seeds, so a repeated bench run is a fresh sample
SAMPLING: dict[str, Any] = {}
TEMPLATE_KWARGS: dict[str, Any] = {}
REASONING_TOKENS = DEFAULT_REASONING_TOKENS  # added to the answer's output cap under --think on; match the server's --reasoning-budget
RETRIES = 1  # correction attempts per task after the first, each fed the latest diff
# A time budget per task instead of RETRIES: correction rounds continue until TASK_SECONDS of the task's wall
# time is spent, up to BUDGET_ATTEMPT_CAP attempts. A fast model then gets more tries in the same time.
TASK_SECONDS: float | None = None
BUDGET_ATTEMPT_CAP = 64
# After an unchanged or repeated answer, sample hotter and name one kind of edit the next answer must make.
DIVERSIFY = False
# Write each task's best candidate into the owner source once its visit ends, after a report rebuild shows no
# function anywhere got worse, and commit it; the dashboard and agents then see the gain. Off for the bench.
PROMOTE = False
# After each task the worker takes a breather: it runs the jobs queued in BREATHER_FILE (state syncs, merges,
# refreshes), then pauses BREATHER_SECONDS holding no claim or build lock, so waiting builds get their turn.
DEFAULT_BREATHER_SECONDS = 15.0  # the `run` command's default
BREATHER_SECONDS = 0.0  # library callers (the bench, tests) pause only when set
# An optional machine-wide lock file (DECOMP_HEAVY_LOCK) shared with other heavy build users on this machine.
HEAVY_LOCK = harness_paths.env("HEAVY_LOCK", "")
RUN_OPTIONS: dict[str, Any] = {}  # run settings shown on the dashboard's worker card
STALL_ROUNDS = 4  # stop a task's correction rounds after this many rounds without a new best
PERMUTE = False  # run the declaration-order search on near-exact tasks after the model rounds
PERMUTE_CAP = 720
PERMUTE_SECONDS = 600
REWRITE = False  # run the policy-safe rewrite search on near-exact tasks after the model rounds
REWRITE_SECONDS = 600
REWRITE_SIDEWAYS = 6
RECYCLE = False  # when the queue runs dry, retry attempts made before the latest harness improvement
LOWEST_FIRST = False  # take the lowest-% functions first and the priority list last
INVESTIGATION = harness_paths.env("INVESTIGATION", "1") != "0"
CONTEXT_REQUESTS = harness_paths.env("CONTEXT_REQUESTS", "1") != "0"


def build_dir() -> Path:
    """The selected project's version build directory (``build/<VERSION>``) under ROOT."""
    return ROOT / "build" / VERSION


def build_relative(*parts: str | Path) -> Path:
    """A path under ``build/<VERSION>`` relative to ROOT, as ninja names its targets."""
    return Path("build", VERSION, *parts)


def ninja_target(path: Path) -> str:
    """A ROOT-relative ninja target name (ninja files use forward slashes on every host)."""
    return Path(path).as_posix()


def split_command(line: str) -> list[str]:
    """POSIX-split a ninja command line without eating backslashes in Windows paths.

    Ninja files use POSIX quoting; a lone backslash (a Windows path separator) is doubled first,
    while escapes of a quote or backslash keep their meaning."""
    return shlex.split(re.sub(r'\\(?![\\"])', r"\\\\", line))


class StaleSource(RuntimeError):
    pass


class HostUnavailable(RuntimeError):
    """The Ollama host could not be reached; the task was not attempted."""


class PromptTooLarge(RuntimeError):
    """The server rejected the prompt as longer than its context (llama-server exceed_context_size_error).
    A problem with this task's prompt, not with the host: never back off on it."""

    def __init__(self, n_prompt: int, n_ctx: int):
        super().__init__(f"prompt of {n_prompt} tokens exceeds the server context of {n_ctx}")
        self.n_prompt, self.n_ctx = n_prompt, n_ctx


class RepeatedResponse(RuntimeError):
    pass


class IncompleteResponse(RuntimeError):
    pass


class RestorationError(RuntimeError):
    pass


class TaskBlocked(RuntimeError):
    """No edit to the assigned C can reach the target; `status` says why. Sync keeps the status until the
    owner source changes, so the worker doesn't spend its rounds on it again."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


# Instructions MWCC never emits from C (only from asm or intrinsics): a target that has one was hand-written.
NON_C_OPCODES = {"sc", "rfi", "sync", "isync", "eieio", "dcbf", "dcbi", "dcbst", "icbi", "tlbie", "tlbsync",
                 "lwarx", "stwcx."}
# The special-register moves MWCC does emit (prologues, calls through a pointer, CR and FPSCR access).
C_SPR_MOVES = {"mflr", "mtlr", "mfctr", "mtctr", "mfcr", "mtcrf", "mffs", "mtfsf", "mtfsfi", "mtfsb0", "mtfsb1"}


def diff_columns(brief: str) -> tuple[list[str], list[str]]:
    """The TARGET and OURS instruction columns of a brief's instruction diff table."""
    table = re.search(r"## Instruction diff.*?```\n(.*?)```", brief, flags=re.S)
    if not table:
        return [], []
    lines = table.group(1).splitlines()
    header = next((line for line in lines if "TARGET" in line and "OURS" in line), None)
    if header is None:
        return [], []
    left, right = header.index("TARGET"), header.index("OURS")
    target, ours = [], []
    for line in lines:
        if line is header or not re.match(r"^(?:>>|  )\s*\d+\s", line):
            continue
        target.append(line[left:right].strip())
        ours.append(line[right:].strip())
    return [text for text in target if text], [text for text in ours if text]


def blocked_reason(brief: str) -> TaskBlocked | None:
    """Why the task can't be solved in C, read from its brief: the target is hand-written assembly, or the
    assigned definition is not compiled into the unit (an inactive #if region, or another owner's copy)."""
    if "target symbol not found in objdiff output" in brief:
        return TaskBlocked("blocked_not_compiled",
                           "objdiff has no such symbol in the compiled unit: the assigned definition is not built "
                           "(an inactive #if region or a definition owned elsewhere); fix the split before queueing it")
    target, ours = diff_columns(brief)
    if not target:
        return None
    opcodes = {text.split()[0] for text in target}
    foreign = sorted(op for op in opcodes if op in NON_C_OPCODES or op.startswith("tw")
                     or (op[:2] in ("mf", "mt") and op.rstrip(".") not in C_SPR_MOVES))
    if foreign:
        return TaskBlocked("blocked_asm_only",
                           f"the target uses {', '.join(foreign)}, which MWCC never emits from C: hand-written asm")
    if not ours:
        return TaskBlocked("blocked_not_compiled",
                           "the assigned definition compiles to nothing in this unit (an inactive #if region or "
                           "a definition owned elsewhere): fix the owner source's split before queueing it")
    return None


def is_placeholder(function: str, target_instructions: int) -> bool:
    """A stub standing in for a function nobody has written yet (`return 0;`, `(void)arg;`), against a
    target with real work in it: the attempt must write the body, not make the smallest change."""
    clean = source_mask(function)
    body = clean[clean.find("{") + 1:clean.rfind("}")]
    statements = [text.strip() for text in body.split(";")
                  if text.strip() and not re.fullmatch(r"\s*\(void\)\s*\w+\s*", text)]
    constant_return = r"return\s+(?:(?:\([^()]+\))\s*)?(?:NULL|[-+]?(?:0[xX][0-9A-Fa-f]+|[0-9]+)[uUlL]*)"
    return target_instructions >= 8 and (not statements or
                                        len(statements) == 1 and bool(re.fullmatch(constant_return, statements[0])))


PLACEHOLDER_NOTE = (
    "The assigned body appears to be a constant-return or no-op placeholder. Recover missing work evidenced\n"
    "by the target assembly; do not assume that its literal return value describes the target behavior.\n"
    "Write the complete body from the target instructions. Work through them in order: each `bl` is a call\n"
    "(its arguments are the r3-r10 and f1-f8 values set just before it), each load and store at an offset\n"
    "from a pointer is a field access, each compare and conditional branch is an if or a loop test, and\n"
    "r3 or f1 at the final `blr` is the return value. Keep the signature.")


def check_baseline(item: dict[str, Any]) -> None:
    path = ROOT / item.get("owner_source", item["source"])
    if digest(path.read_text(encoding="utf-8", errors="replace")) != item["source_sha256"]:
        raise StaleSource("source changed since this task was queued; run sync")


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def candidates_dir() -> Path:
    return CANDIDATES_DIR or STATE_DIR / "candidates"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    replace_file(temporary, path)


def blank_state(host: str, model: str, num_predict: int = DEFAULT_NUM_PREDICT) -> dict[str, Any]:
    return {
        "schema": 1,
        "created_at": timestamp(),
        "settings": {
            "ollama_host": host.rstrip("/"),
            "ollama_model": model,
            "ollama_num_predict": num_predict,
        },
        "workers": {},
        "items": {},
        "events": [],
        "snapshots": [],
    }


def load_state(host: str, model: str, num_predict: int = DEFAULT_NUM_PREDICT, *, update_settings: bool = True) -> dict[str, Any]:
    state = read_json(STATE_FILE, blank_state(host, model, num_predict))
    if state.get("schema") != 1:
        raise RuntimeError(f"unsupported state schema: {STATE_FILE}")
    state.setdefault("items", {})
    state.setdefault("events", [])
    state.setdefault("snapshots", [])
    state.setdefault("settings", {})
    state.setdefault("workers", {})
    defaults = {
        "ollama_host": host.rstrip("/"),
        "ollama_model": model,
        "ollama_num_predict": num_predict,
    }
    for key, value in defaults.items():
        state["settings"].setdefault(key, value)
    if update_settings:
        state["settings"].update(defaults)
    return state


def event_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


SNAPSHOT_LIMIT = 500
RECENT_SNAPSHOT_LIMIT = 350


def retain_snapshots(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the chart's full time span while retaining recent points at full detail."""
    ordered = sorted(rows, key=lambda row: str(row.get("at", "")))
    if len(ordered) <= SNAPSHOT_LIMIT:
        return ordered
    recent = ordered[-RECENT_SNAPSHOT_LIMIT:]
    historic = ordered[:-RECENT_SNAPSHOT_LIMIT]
    slots = SNAPSHOT_LIMIT - RECENT_SNAPSHOT_LIMIT
    indexes = [round(index * (len(historic) - 1) / (slots - 1)) for index in range(slots)]
    return [historic[index] for index in indexes] + recent


def item_stamp(item: dict[str, Any]) -> str:
    activity_row = item.get("activity") or {}
    return max(str(item.get("updated_at", "")), str(activity_row.get("updated_at", "")), str(item.get("last_attempt_at", "")))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = timestamp()
    with COORDINATOR.state_file():
        current = read_json(STATE_FILE, None)
        replace_items = bool(state.pop("_replace_items", False))
        if not isinstance(current, dict) or current.get("schema") != 1:
            merged = state
        else:
            merged = current
            merged.setdefault("settings", {}).update(state.get("settings", {}))
            for name, worker in state.get("workers", {}).items():
                old_worker = merged.setdefault("workers", {}).get(name, {})
                if worker.get("updated_at", "") >= old_worker.get("updated_at", ""):
                    merged["workers"][name] = worker
            if replace_items:
                merged["items"] = state.get("items", {})
                merged["priority_analysis"] = state.get("priority_analysis", {})
            else:
                merged.setdefault("items", {})
                for task, item in state.get("items", {}).items():
                    old = merged["items"].get(task)
                    if old is None or item_stamp(item) > item_stamp(old):
                        merged["items"][task] = item
            existing = {event_key(row) for row in merged.setdefault("events", [])}
            for row in state.get("events", []):
                key = event_key(row)
                if key not in existing:
                    merged["events"].append(row)
                    existing.add(key)
            current_snapshots = merged.setdefault("snapshots", [])
            existing_snapshots = {event_key(row) for row in current_snapshots}
            oldest_snapshot = min((str(row.get("at", "")) for row in current_snapshots), default="")
            for row in state.get("snapshots", []):
                key = event_key(row)
                # An older process may still hold points already removed by compaction. Do not let
                # that stale copy move the chart's baseline backward on its next save.
                if key not in existing_snapshots and (not oldest_snapshot or str(row.get("at", "")) >= oldest_snapshot):
                    merged["snapshots"].append(row)
                    existing_snapshots.add(key)
            merged["updated_at"] = state["updated_at"]
        merged["events"] = merged.get("events", [])[-500:]
        # Preserve the chart's original baseline and a representative long-term history. Recent
        # progress stays at full resolution; older points are evenly downsampled as the run grows.
        merged["snapshots"] = retain_snapshots(merged.get("snapshots", []))
        merged.pop("_replace_items", None)
        write_json(STATE_FILE, merged)


def event(state: dict[str, Any], kind: str, **details: Any) -> None:
    state["events"].append({"at": timestamp(), "kind": kind, **details})


def activity(state: dict[str, Any], item: dict[str, Any], phase: str, detail: str, **metrics: Any) -> None:
    """Persist the exact worker phase so the dashboard survives a refresh."""
    previous = item.get("activity") or {}
    item["activity"] = {
        **(previous if previous.get("phase") == phase else {}),
        "started_at": previous.get("started_at") or timestamp(),
        "updated_at": timestamp(),
        "phase": phase,
        "detail": detail,
        **metrics,
    }
    item["updated_at"] = item["activity"]["updated_at"]
    save_state(state)


def functions_in_report(report: dict[str, Any]):
    for unit in report.get("units", []):
        functions = list(unit.get("functions") or [])
        for section in unit.get("sections") or []:
            functions.extend(section.get("functions") or [])
        for function in functions:
            yield unit, function


def identifier(source: str, unit: str, symbol: str) -> str:
    hash_part = hashlib.sha256(f"{source}\0{unit}\0{symbol}".encode()).hexdigest()[:16]
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)[:48]
    return f"{clean}-{hash_part}"


def category(unit: dict[str, Any]) -> str:
    categories = (unit.get("metadata") or {}).get("progress_categories") or []
    # Units carry the binary ("dol"/"modules") first and the subsystem after; group by subsystem.
    specific = [name for name in categories if name not in ("dol", "modules")]
    return (specific or categories or ["other"])[0]


def owner_source(source: str, root: Path | None = None) -> str:
    """Resolve pure forwarding score shims without claiming a real C owner."""
    root = root or ROOT
    visited = set()
    while source not in visited:
        visited.add(source)
        path = root / source
        if not path.is_file():
            return source
        contents = path.read_text(encoding="utf-8", errors="replace")
        without_comments = re.sub(r"/\*.*?\*/|//[^\n]*", "", contents, flags=re.DOTALL)
        active_lines = [line.strip() for line in without_comments.splitlines() if line.strip()]
        # A real translation unit may include one sibling .c for a shared
        # candidate yet define its own functions. Following that include
        # reserves the wrong owner and leaves the real source unclaimed.
        if not active_lines or any(not line.startswith("#") for line in active_lines):
            return source
        includes = [match.group(1) for line in active_lines
                    if (match := re.fullmatch(r'#\s*include\s+"(src/[^"\n]+\.c)"', line))]
        if len(includes) != 1 or not (root / includes[0]).is_file():
            return source
        source = includes[0]
    raise ValueError(f"cyclic source-owner includes: {source}")


FORBIDDEN_PRAGMAS = ("optimization_level", "optimize_for_size", "scheduling", "peephole", "opt_propagation")
PRAGMA_LINE = re.compile(r"^[ \t]*#[ \t]*pragma[ \t]+(\w+)(?:[ \t]+([^\n/]*))?", re.M)


def pragmas_active_at(text: str, offset: int) -> dict[str, str]:
    """Forbidden compiler-control pragmas in effect at `offset`, honouring push/pop scoping."""
    state: dict[str, str] = {}
    stack: list[dict[str, str]] = []
    live: list[bool] = []  # conditional-compilation stack; `#if 0` regions don't count
    for line in text[:offset].splitlines():
        directive = re.match(r"[ \t]*#[ \t]*(\w+)[ \t]*(.*)", line)
        if not directive:
            continue
        word, rest = directive.group(1), directive.group(2).strip()
        if word in ("if", "ifdef", "ifndef"):
            live.append(not (word == "if" and rest.split("//")[0].strip() == "0"))
            continue
        if word in ("else", "elif"):
            if live:
                live[-1] = not live[-1] if word == "else" else True
            continue
        if word == "endif":
            if live:
                live.pop()
            continue
        if word != "pragma" or not all(live):
            continue
        match = PRAGMA_LINE.match(line)
        if not match:
            continue
        name, value = match.group(1), (match.group(2) or "").strip()
        if name == "push":
            stack.append(dict(state))
        elif name == "pop":
            state = stack.pop() if stack else {}
        elif name in FORBIDDEN_PRAGMAS:
            if value == "reset":
                state.pop(name, None)
            else:
                state[name] = value
    return state


def unit_pragma_defaults(cflags: str) -> dict[str, str]:
    """The pragma values a unit's own command-line flags imply, so one function can be
    compiled as if no local pragma applied without disturbing its neighbours."""
    level = re.search(r"-O(\d)(,([ps]))?", cflags)
    return {
        "optimization_level": level.group(1) if level else "4",
        "optimize_for_size": "on" if level and level.group(3) == "s" else "off",
        "peephole": "off" if "nopeephole" in cflags else "on",
        "scheduling": "off" if re.search(r"noschedule|-schedule off", cflags) else "on",
        "opt_propagation": "off" if "noprop" in cflags else "on",
    }


def neutralize(text: str, symbol: str, cflags: str) -> str:
    """Wrap one function so it compiles under the unit's flags instead of its local pragmas."""
    start, end = function_span(text, symbol)
    defaults = unit_pragma_defaults(cflags)
    head = "#pragma push\n" + "".join(f"#pragma {name} {defaults[name]}\n" for name in FORBIDDEN_PRAGMAS)
    return text[:start] + head + text[start:end] + "\n#pragma pop\n" + text[end:]


def unit_cflags(item: dict[str, Any]) -> str:
    compiled = build_relative(Path(item["source"]).with_suffix(".o"))
    out = subprocess.run(["ninja", "-t", "commands", ninja_target(compiled)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    lines = [line for line in out.stdout.splitlines() if "mwcceppc" in line]
    return lines[-1] if lines else ""


def sync(state: dict[str, Any], reset: bool = False) -> dict[str, int]:
    refreshed = subprocess.run(
        ["ninja", "-j2", "all_source", ninja_target(build_relative("report.json"))],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )
    if refreshed.returncode:
        raise RuntimeError("authoritative report rebuild failed: " + (refreshed.stderr or refreshed.stdout)[-1600:])
    if not REPORT_FILE.exists():
        raise RuntimeError("report target completed without creating report.json")
    report = read_json(REPORT_FILE, {})
    grouped = list(functions_in_report(report))
    sources, hashes, functions = {}, {}, {}
    for unit, function in grouped:
        metadata = unit.get("metadata") or {}
        source = metadata.get("source_path")
        owner = None
        if source and not metadata.get("auto_generated"):
            if source not in sources:
                sources[source] = owner_source(source)
            owner = sources[source]
            if owner not in hashes and (ROOT / owner).is_file():
                hashes[owner] = digest((ROOT / owner).read_text(encoding="utf-8", errors="replace"))
        functions[(unit["name"], function["name"])] = {
            "unit": unit["name"], "symbol": function["name"],
            "owner_source": owner if owner in hashes else None,
            "source_sha256": hashes.get(owner),
            "base_pct": float(function.get("fuzzy_match_percent") or 0),
            "size": int(function.get("size") or 0),
        }
    graph, covered, missing = reference_graph(ROOT, read_json(ROOT / "objdiff.json", {}).get("units", []), functions)
    priorities = rank(functions, graph, covered)
    state["priority_analysis"] = {
        "version": 1, "at": timestamp(), "report_sha256": digest(REPORT_FILE.read_text(encoding="utf-8")),
        "covered_functions": len(covered), "total_functions": len(functions),
        "reference_edges": sum(len(targets) for targets in graph.values()), "missing_units": missing,
    }
    unit_totals = Counter(unit["name"] for unit, _ in grouped)
    unit_exacts = Counter(
        unit["name"] for unit, function in grouped
        if float(function.get("fuzzy_match_percent") or 0) >= 100.0
    )
    unit_stats = {
        name: {"total": unit_totals[name], "exact": unit_exacts[name]}
        for name in unit_totals
    }
    residuals = Counter(
        unit["name"] for unit, function in grouped
        if float(function.get("fuzzy_match_percent") or 0) < 100.0
    )
    owner_residuals = Counter()
    for unit, function in grouped:
        if float(function.get("fuzzy_match_percent") or 0) >= 100.0:
            continue
        metadata = unit.get("metadata") or {}
        source = metadata.get("source_path")
        if source and not metadata.get("auto_generated"):
            owner_residuals[sources[source]] += 1
    claimed_sources = set((COORDINATOR.snapshot().get("claims") or {}).keys())
    refreshed: dict[str, dict[str, Any]] = {}
    owner_texts: dict[str, str] = {}
    for unit, function in grouped:
        pct = float(function.get("fuzzy_match_percent") or 0)
        cleanup = None
        if pct >= 100.0:
            # Exact only under a forbidden local pragma: the source policy can't accept it, so the
            # model gets a policy-cleanup task to find pragma-free C that still matches.
            metadata = unit.get("metadata") or {}
            source = metadata.get("source_path")
            if metadata.get("complete") or not source or metadata.get("auto_generated") or source not in sources:
                continue
            owner = sources[source]
            if owner not in owner_texts:
                path = ROOT / owner
                owner_texts[owner] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            try:
                start, _ = function_span(owner_texts[owner], function["name"])
            except ValueError:
                continue
            cleanup = pragmas_active_at(owner_texts[owner], start)
            if not cleanup:
                continue
        metadata = unit.get("metadata") or {}
        source = metadata.get("source_path")
        symbol = function["name"]
        task_source = source or "[no declared source]"
        task = identifier(task_source, unit["name"], symbol)
        if not source or metadata.get("auto_generated"):
            refreshed[task] = {
                "id": task, "source": task_source, "unit": unit["name"], "symbol": symbol,
                "category": category(unit), "size": int(function.get("size") or 0), "base_pct": pct,
                "residual_functions": residuals[unit["name"]], "status": "blocked_no_editable_source",
                "attempts": 0, "updated_at": timestamp(),
                "last_error": "no source-backed owner is declared in the current object map",
            }
            continue
        owner = sources[source]
        owner_path = ROOT / owner
        if not owner_path.is_file():
            refreshed[task] = {
                "id": task, "source": source, "unit": unit["name"], "symbol": symbol,
                "category": category(unit), "size": int(function.get("size") or 0), "base_pct": pct,
                "residual_functions": residuals[unit["name"]], "status": "blocked_no_editable_source",
                "attempts": 0, "updated_at": timestamp(),
                "last_error": "declared source file is absent from this checkout",
            }
            continue
        record = {
            "id": task,
            "source": source,
            "unit": unit["name"],
            "symbol": symbol,
            "category": category(unit),
            "owner_source": owner,
            "size": int(function.get("size") or 0),
            "base_pct": pct,
            "residual_functions": residuals[unit["name"]],
            "exact_siblings": unit_stats.get(unit["name"], {}).get("exact", 0),
            "owner_residual_functions": owner_residuals.get(owner, 0),
            "source_sha256": hashes[owner],
            "updated_at": timestamp(),
        }
        record.update(priorities.get((unit["name"], symbol), {}))
        if cleanup:
            record.update(kind="cleanup", pragmas=cleanup, base_pct=0.0)
        old = state["items"].get(task)
        if old and not reset:
            for key in ("status", "attempts", "last_attempt_at", "last_report", "last_error", "candidate", "worker", "activity",
                        "best", "promoted_pct", "promotion_rejected", "structural_hunks", "clean_base_pct", "tried"):
                if key in old:
                    record[key] = old[key]
            if record.get("kind") == "cleanup" and record.get("clean_base_pct") is not None:
                record["base_pct"] = record["clean_base_pct"]
            if record.get("status") == "running" and owner not in claimed_sources:
                record["status"] = "pending"
                record["last_error"] = "recovered after interrupted runner"
            if old.get("source_sha256") != record["source_sha256"] and record.get("status") not in {"review_exact", "running"}:
                # The owner changed (a merge or promotion): requeue against the new source. The old
                # best candidate was built on the old text, so it no longer applies.
                for key in ("best", "candidate", "last_report", "structural_hunks", "tried"):
                    record.pop(key, None)
                record.update(status="pending", last_error="requeued: owner source changed since the last attempt")
            elif record.get("status") == "stale_source":
                # Marked stale by a runner mid-flight; this sync has refreshed its baseline.
                record.update(status="pending", last_error="requeued after sync")
        else:
            record.update({"status": "pending", "attempts": 0})
        try:
            start, end = function_span(owner_path.read_text(encoding="utf-8", errors="replace"), symbol)
            record["function_chars"] = end - start
            if record["status"] == "blocked_source_context":
                record["status"] = "pending"
                record.pop("last_error", None)
            elif record.get("last_error") in {
                f"function definition not found: {symbol}",
                f"ambiguous C definitions: {symbol}",
            }:
                record.pop("last_error", None)
        except ValueError as exc:
            record.update(status="blocked_source_context", last_error=str(exc))
        refreshed[task] = record
    removed = len(set(state["items"]) - set(refreshed))
    state["items"] = refreshed
    state["_replace_items"] = True
    counts = Counter(item["status"] for item in refreshed.values())
    state["snapshots"].append(progress_snapshot(state, report, force=True))
    event(state, "queue_synced", pending=counts["pending"], removed=removed, report_rebuilt=True)
    save_state(state)
    return {"open": len(refreshed), "pending": counts["pending"], "removed": removed}


def source_mask(text: str) -> str:
    """Keep offsets while masking comments, literals, directives and dead #if 0.

    Unknown conditions retain both branches; ambiguous definitions fail closed.
    This is a source locator, not a replacement for the build preprocessor.
    """
    blank = lambda value: re.sub(r"[^\n]", " ", value)
    clean = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                   lambda match: blank(match.group()), text, flags=re.DOTALL)
    active, stack, lines = True, [], []
    continued_directive = False
    for line in clean.splitlines(keepends=True):
        if continued_directive:
            continued_directive = line.rstrip("\r\n").endswith("\\")
            lines.append(blank(line))
            continue
        directive = re.match(r"\s*#\s*(\w+)\b(.*)", line)
        if directive:
            continued_directive = line.rstrip("\r\n").endswith("\\")
            kind, expression = directive.groups()
            expression = expression.strip().strip("() ")
            value = expression == "1" if expression in {"0", "1"} else None
            if kind in {"if", "ifdef", "ifndef"}:
                stack.append((active, value))
                active = active and value is not False
            elif kind in {"else", "elif"} and stack:
                parent, taken = stack[-1]
                branch = (not taken) if kind == "else" and taken is not None else value
                active = parent and taken is not True and branch is not False
                stack[-1] = (parent, True if taken is True or branch is True else None if taken is None or branch is None else False)
            elif kind == "endif" and stack:
                active, _ = stack.pop()
            lines.append(blank(line))
        else:
            lines.append(line if active else blank(line))
    return "".join(lines)


def function_span(text: str, symbol: str) -> tuple[int, int]:
    """Find one C definition, including simple K&R parameter declarations.

    Never select a disabled reference-asm stub or infer a body from a mere
    prototype/call followed by another declaration.
    """
    clean = source_mask(text)
    definitions = []
    pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for match in pattern.finditer(clean):
        open_paren = clean.find("(", match.start())
        try:
            after_params = balanced_end(clean, open_paren, "(", ")")
        except ValueError:
            continue
        cursor = skip_space_and_comments(clean, after_params)
        if cursor >= len(clean):
            continue
        if clean[cursor] != "{":
            # Old-style definitions put typed parameter declarations between
            # the name's bare identifier list and the opening brace. Keep
            # this deliberately narrow: malformed or non-K&R context fails
            # closed instead of consuming a later function's body.
            params = [part.strip() for part in clean[open_paren + 1:after_params - 1].split(",")]
            if not params or not all(re.fullmatch(r"[A-Za-z_]\w*", part) for part in params):
                continue
            brace = clean.find("{", cursor)
            if brace < 0 or brace - cursor > 1024:
                continue
            declarations = clean[cursor:brace]
            if re.search(r"[{}()=#]", declarations) or not declarations.rstrip().endswith(";"):
                continue
            statements = [part.strip() for part in declarations.split(";")[:-1]]
            if not statements or any(not statement for statement in statements):
                continue
            found = set()
            valid_declarations = True
            for statement in statements:
                names = {name for name in params if re.search(rf"\b{re.escape(name)}\b", statement)}
                if not names:
                    valid_declarations = False
                    break
                found.update(names)
            if not valid_declarations or found != set(params):
                continue
            cursor = brace
        start = max(clean.rfind(delimiter, 0, match.start()) for delimiter in ";{}") + 1
        while start < match.start() and clean[start].isspace():
            start += 1
        prefix = clean[start:match.start()]
        if not prefix.strip() or re.search(r"\b(?:asm|__asm|__asm__|if|while|switch)\b", prefix):
            continue
        definitions.append((start, balanced_end(clean, cursor, "{", "}")))
    if len(definitions) == 1:
        return definitions[0]
    if definitions:
        raise ValueError(f"ambiguous C definitions: {symbol}")
    raise ValueError(f"function definition not found: {symbol}")


def skip_space_and_comments(text: str, cursor: int) -> int:
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
        elif text.startswith("/*", cursor):
            end = text.find("*/", cursor + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            cursor = end + 2
        elif text.startswith("//", cursor):
            end = text.find("\n", cursor + 2)
            cursor = len(text) if end < 0 else end + 1
        else:
            break
    return cursor


def balanced_end(text: str, start: int, opening: str, closing: str) -> int:
    depth, cursor, quote = 0, start, ""
    while cursor < len(text):
        char = text[cursor]
        if quote:
            if char == "\\":
                cursor += 2
                continue
            if char == quote:
                quote = ""
        elif text.startswith("/*", cursor):
            end = text.find("*/", cursor + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            cursor = end + 2
            continue
        elif text.startswith("//", cursor):
            end = text.find("\n", cursor + 2)
            cursor = len(text) if end < 0 else end + 1
            continue
        elif char in {'"', "'"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    raise ValueError(f"unbalanced {opening}{closing}")


def function_signature(text: str, start: int, end: int) -> str:
    return " ".join(text[start:text.index("{", start, end)].split())


def extract_function(response: str, symbol: str) -> str:
    blocks = re.findall(r"```(?:c|C)?\s*\n(.*?)```", response, flags=re.DOTALL)
    for block in blocks or [response]:
        try:
            start, end = function_span(block, symbol)
        except ValueError:
            continue
        candidate = block[start:end].strip() + "\n"
        if re.search(r"#\s*include\b|\b(?:__asm__|__asm|asm)\b|\.inc\b", candidate, flags=re.IGNORECASE):
            raise ValueError("candidate contains forbidden assembly or include")
        return candidate
    raise ValueError("model response contains no complete target function")


def splice(base: str, symbol: str, proposal: str) -> str:
    base_start, base_end = function_span(base, symbol)
    proposal_start, proposal_end = function_span(proposal, symbol)
    if function_signature(base, base_start, base_end) != function_signature(proposal, proposal_start, proposal_end):
        raise ValueError("candidate changed the target function signature")
    return base[:base_start] + proposal[proposal_start:proposal_end].strip() + "\n" + base[base_end:]


def policy(base_function: str, proposal: str) -> tuple[list[str], list[str]]:
    """Hard-reject the explicit project bans; flag ambiguity for human audit."""
    clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", proposal, flags=re.DOTALL)
    rejected, flags = [], []
    forbidden = [
        (r"#\s*(?:pragma|include|define|if|ifdef|ifndef|elif|else|endif)\b", "preprocessor directive"),
        (r"\b(?:__asm__|__asm|asm)\b", "inline assembly"),
        (r"\.inc\b", ".inc reference"),
        (r"\b_Pragma\s*\(", "compiler pragma"),
        (r"\?\?[=()/!'<>-]", "C trigraph"),
    ]
    for pattern, label in forbidden:
        if re.search(pattern, clean, flags=re.IGNORECASE):
            rejected.append(label)
    if re.search(r"\bvolatile\b", clean) and "volatile" not in base_function:
        flags.append("adds volatile; requires real MMIO/shared-state evidence")
    if re.search(r"\b([A-Za-z_]\w*)\s*=\s*\1\s*;", clean):
        flags.append("contains self-assignment")
    if re.search(r"\b(?:goto|typedef|union|struct)\b", clean):
        flags.append("control-flow or aggregate construct needs source-evidence audit")
    return rejected, flags


def build_prompt(item: dict[str, Any], source: str, worker: str = MODEL_WORKER) -> tuple[str, str]:
    with COORDINATOR.build(worker, f"Read objdiff: {item['symbol']}"):
        if (STATE_DIR / "halt.json").exists():
            raise RestorationError("fleet halted after baseline restoration failure; inspect halt.json")
        check_baseline(item)
        owner = ROOT / item.get("owner_source", item["source"])
        original = owner.read_bytes()
        try:
            owner.write_text(source, encoding="utf-8", newline="")  # measure exactly the version shown in the prompt
            if item.get("kind") == "cleanup" and item.get("clean_base_pct") is None:
                with tempfile.TemporaryDirectory() as temporary:
                    item["clean_base_pct"] = scratch_score(item, Path(temporary))
                if item["clean_base_pct"] is not None:
                    item["base_pct"] = item["clean_base_pct"]
            brief, prompt = build_prompt_unlocked(item, source)
        finally:
            owner.write_bytes(original)
        if item.get("kind") != "cleanup":
            return brief, prompt
        note = ("## Policy cleanup task\n\nThis function already matches, but only under forbidden local "
                f"compiler pragmas ({', '.join(f'{k} {v}' for k, v in (item.get('pragmas') or {}).items())}). "
                "The project's source policy does not accept pragma-shaped code. The diff below is the function compiled under "
                "its unit's normal flags. Write natural, pragma-free C that matches the target under those flags.\n\n")
        return brief, note + prompt


def _clean_body(symbol: str, source_path: str | None, budget: int) -> str:
    """A matched function's source body, only if it is policy-clean and within budget."""
    if not source_path:
        return ""
    try:
        owner = owner_source(source_path)
        text = (ROOT / owner).read_text(encoding="utf-8", errors="replace")
        start, end = function_span(text, symbol)
    except (OSError, ValueError):
        return ""
    body = text[start:end].strip()
    if pragmas_active_at(text, start):
        return ""
    if REPORT_FILE.exists() and (ROOT / owner).stat().st_mtime > REPORT_FILE.stat().st_mtime:
        return ""
    if re.search(r"#\s*pragma\b|\.inc\b|\b(?:asm|__asm|__asm__)\b", body) or len(body) > budget:
        return ""
    rejected, flags = policy("", body)
    return "" if rejected or flags else body


def corpus_context(item: dict[str, Any], owner_text: str) -> str:
    """Similar already-matched code and the declarations of every callee, from the corpus index."""
    try:
        index = corpus.load_index(ROOT, REPORT_FILE, STATE_DIR / "corpus_index.json")
    except (OSError, ValueError):
        return reference_context(item)
    from local_campaign_knowledge_context import prompt_context as knowledge_prompt_context
    knowledge_context = knowledge_prompt_context(ROOT, REPORT_FILE, item, _clean_body)
    owner = item.get("owner_source", item["source"])
    shaped = {row["symbol"]: row for row in templates.matched_templates(ROOT, item["symbol"])}
    names = list(dict.fromkeys([*shaped, *corpus.similar_matched(index, item["symbol"], owner, limit=5)]))
    parts, budget = [], 4000
    for name in ([] if knowledge_context else names):
        row = index["functions"].get(name, {})
        if row.get("pct", 0) < 100:
            continue
        body = _clean_body(name, row.get("source"), budget)
        if body:
            shape = shaped.get(name)
            why = f", normalized assembly similarity {shape['similarity']:.0%}" if shape else ", shared callees/data"
            assembly = corpus.assembly_excerpt(ROOT, row, name, budget=min(700, budget - len(body))) if shape else ""
            pair = f"\nTarget instructions for this reference (bounded prefix):\n```asm\n{assembly}\n```" if assembly else ""
            parts.append(f"### {name} ({row['source']}, report 100%{why})\n```c\n{body}\n```{pair}")
            budget -= len(body) + len(assembly)
        if len(parts) == 3 or budget < 400:
            break
    out = []
    if knowledge_context:
        out.append(knowledge_context)
    elif parts:
        out.append("## Matched code in this project (assembly-shape or shared-data references)\n"
                   "These are reported byte-exact project examples without active shaping pragmas.\n"
                   "Assembly similarity ignores register names and buckets immediates; it is NOT semantic equality.\n"
                   "Reuse supported idioms, not their constants, signatures or layouts without target evidence.\n\n"
                   + "\n\n".join(parts))
    else:
        fallback = reference_context(item)
        if fallback:
            out.append(fallback)
    decls = corpus.callee_declarations(index, item["symbol"], owner_text, _headers_text(), ROOT)
    if decls:
        out.append("## Declarations of the functions it calls (match these parameter and return types exactly)\n\n```c\n"
                   + "\n".join(decls) + "\n```")
    return "\n\n".join(out)


LIBRARY_DEF = re.compile(r"(?m)^[A-Za-z_][^\n;#{}()]*?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")
_LIBRARY_INDEX: dict[Path, dict[str, Path]] = {}


def library_sources() -> list[tuple[str, Path]]:
    """(name, source directory) of each configured reference project (see harness_paths.reference_projects).

    Decomps that share a library with this one (an SDK, a middleware or engine library) can show its
    original structure, types and field names under the same function names."""
    return [(project.name or project.id, project.root / "src") for project in harness_paths.reference_projects()
            if (project.root / "src").is_dir()]


def _library_index(source_root: Path) -> dict[str, Path]:
    index = _LIBRARY_INDEX.get(source_root)
    if index is None:
        index = {}
        for path in sorted(source_root.rglob("*.c")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in LIBRARY_DEF.finditer(text):
                index.setdefault(match.group(1), path)
        _LIBRARY_INDEX[source_root] = index
    return index


def library_reference(symbol: str, budget: int = 3500, sources: list[tuple[str, Path]] | None = None) -> str:
    """The same-named function from a configured reference decomp, when one defines it.

    A reference project's copy matches its own build, so it can show the original structure, types
    and field names of a shared library function; it is reference only and is filtered by the same
    policy as project examples. The first reference project defining the symbol cleanly is used.
    """
    for name, source_root in (library_sources() if sources is None else sources):
        if not source_root.is_dir():
            continue
        path = _library_index(source_root).get(symbol)
        if not path:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            start, end = function_span(text, symbol)
        except (OSError, ValueError):
            continue
        body = text[start:end].strip()
        if len(body) > budget or re.search(r"#\s*pragma\b|\.inc\b|\b(?:asm|__asm|__asm__)\b", body):
            continue
        rejected, _ = policy("", body)
        if rejected:
            continue
        relative = path.relative_to(source_root.parent).as_posix()
        return (f"## Named library reference from {name} ({relative})\n"
                "This is same-named source from another decompilation project, not verified against our target.\n"
                "Library versions, types, compiler flags and behavior can differ. Our signature and assembly are authoritative.\n\n"
                f"```c\n{body}\n```")
    return ""


def assembly_draft_context(item: dict[str, Any]) -> str:
    if item.get("prompt_pct") == 100.0 or not item.get("structural_hunks"):
        return ""
    try:
        index = corpus.load_index(ROOT, REPORT_FILE, STATE_DIR / "corpus_index.json")
        assembly = index["functions"].get(item["symbol"], {}).get("asm_path")
        if not assembly:
            return ""
        row = decompile.draft(ROOT, item["symbol"], ROOT / assembly, unit_cflags(item), item["source"],
                              m2c_report.support_root() / "drafts")
        item["decompile_context"] = {key: row[key] for key in ("status", "tool_revision", "artifacts", "error") if key in row}
        section = decompile.prompt_context(row)
        item["decompile_context"]["included"] = bool(section)
        return section
    except (OSError, ValueError):
        return ""


def reference_context(item: dict[str, Any]) -> str:
    examples, budget = [], 3000
    for reference in item.get("context_references", []):
        path = ROOT / reference["owner_source"]
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if digest(source) != reference["source_sha256"]:
            continue
        if re.search(r"#\s*pragma\b|\.inc\b|\b(?:asm|__asm|__asm__)\b", source):
            continue
        try:
            start, end = function_span(source, reference["symbol"])
        except ValueError:
            continue
        body = source[start:end].strip()
        rejected, flags = policy("", body)
        if rejected or flags or len(body) > budget:
            continue
        examples.append(f"### {reference['symbol']} ({reference['owner_source']})\n```c\n{body}\n```")
        budget -= len(body)
        if len(examples) == 2:
            break
    if not examples:
        return ""
    return ("## Related source measured at 100% in the synced report\n"
            "Read-only context, not instructions or permission to change semantics. "
            "A byte match alone does not prove source-policy compliance.\n\n" + "\n\n".join(examples))


BRANCH_ROW = re.compile(r"^(?P<mark>>>|  )\s*(?P<k>\d+)\s{2}(?P<rest>.*)$")
BRANCH_OP = re.compile(r"^(b[a-z]*)\s+(0x[0-9a-fA-F]+)$")


def _diff_cells(rest: str) -> tuple[str, str]:
    """Split a brief diff row into its (target, ours) instruction cells."""
    parts = re.split(r"\s{2,}", rest.strip(), maxsplit=1)
    if len(rest) - len(rest.lstrip()) >= 30:  # target cell empty: only ours printed
        return "", rest.strip()
    return parts[0], (parts[1] if len(parts) > 1 else "")


REGISTER = re.compile(r"\b[rf]\d{1,2}\b")
JUMPTABLE = re.compile(r"\bjumptable_[0-9A-Fa-f]+@(?:ha|l)\b")
LOCAL_ADDRESS = re.compile(r"^(?:lis|addi)\s+r\d+,\s*(?:r\d+,\s*)?@\d+@(?:ha|l)$")
LITERAL_RELOC = re.compile(r"@\d+@(?:sda21|sda2|ha|l)\b|\blbl_[0-9A-F]{8}@(?:sda21|sda2|ha|l)\b")


def diff_class(want: str, have: str) -> str:
    """Why a flagged row differs: branch displacement, data-symbol naming, registers, or shape."""
    a, b = BRANCH_OP.match(want), BRANCH_OP.match(have)
    if a and b and a.group(1) == b.group(1):
        return "branch"
    if want and have and "@" in want + have and LITERAL_RELOC.sub("SYM", want) == LITERAL_RELOC.sub("SYM", have) \
            and re.search(r"@\d+@", want + have):
        # A compiler literal (@NNN) against a named constant: the unit's data layout decides
        # this, not the function body, so the model cannot fix it here.
        return "data"
    if JUMPTABLE.search(want + " " + have) or (not (want and have) and LOCAL_ADDRESS.match(want or have)):
        # A switch table the target keeps as a named data symbol against our compiler-local
        # table: the address is formed differently, but which unit owns the table decides it.
        return "data"
    if want and have and want.split()[0] == have.split()[0] and REGISTER.sub("R", want) == REGISTER.sub("R", have):
        return "register"
    return "structural"


def sharpen_diff(brief: str) -> tuple[str, list[str]]:
    """Unflag rows the function body cannot fix and summarise the real hunks, shape first.

    Once code before a branch changes size, every later branch target shifts, so the raw
    diff marks most branches as mismatches even when the branch itself is identical; a
    compiler literal against a named constant is unit data layout. Both are noise that
    hides the few real differences. Hunks with a structural difference (an extra, missing
    or different instruction) come before register-only ones, which usually disappear
    once the structure matches.
    """
    lines, real, kinds = brief.splitlines(), [], {}
    for index, line in enumerate(lines):
        row = BRANCH_ROW.match(line)
        if not row or row["mark"] != ">>":
            continue
        want, have = _diff_cells(row["rest"])
        kind = diff_class(want, have)
        if kind in ("branch", "data"):
            lines[index] = "  " + line[2:]
        else:
            real.append(int(row["k"]))
            kinds[int(row["k"])] = kind
    rows = {int(m["k"]): m for m in (BRANCH_ROW.match(l) for l in lines) if m}
    hunks, seen = [], set()
    for k in real:
        if k in seen:
            continue
        span = [j for j in range(k - 2, k + 3) if j in rows]
        seen.update(span)
        label = "structural" if any(kinds.get(j) == "structural" for j in span) else "register-only"
        body = "\n".join(("-> " if j in real else "   ") + rows[j]["rest"].rstrip() for j in span)
        hunks.append((label, f"[{label}]\n{body}"))
    hunks.sort(key=lambda pair: pair[0] != "structural")
    return "\n".join(lines), [text for _, text in hunks]


def _scratch_symbols(item: dict[str, Any], directory: Path, sym_on: bool = True, use_cache: bool = False):
    """Compile the unit to a scratch object and objdiff the symbol: (target, ours) or None.

    MWCC's `-sym on` only adds debug sections (.text is byte-identical), and with them
    objdiff reports the source line behind each of our instructions. The scratch object
    never replaces the build's own, so it cannot disturb verification.
    """
    compiled = build_relative(Path(item["source"]).with_suffix(".o"))
    commands = subprocess.run(["ninja", "-t", "commands", ninja_target(compiled)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
    sources = {f" {item['source']} ", f" {Path(item['source'])} "}
    lines = [line for line in commands.stdout.splitlines()
             if "mwcceppc" in line and any(text in line + " " for text in sources)]
    project = read_json(ROOT / "objdiff.json", {})
    unit = next((u for u in project.get("units", []) if u.get("name") == item["unit"]), None)
    if commands.returncode or not lines or not unit or not unit.get("target_path"):
        return None
    compile_line = lines[-1].split(" && ", 1)[0]
    args = [arg for arg in split_command(compile_line) if arg != "-MMD"]
    if "-o" not in args:
        return None
    cache_key = None
    if use_cache:
        cache_key = evidence_cache.fingerprint(ROOT, args, ROOT / unit["target_path"], objdiff_cli(), item["symbol"])
        if cache_key:
            cache_key += f":sym={sym_on}"
            cached = SCRATCH_CACHE.get(cache_key)
            if cached is not None:
                item["prompt_evidence_cache"] = "hit"
                return tuple(cached)
        item["prompt_evidence_cache"] = "miss"
    args[args.index("-o") + 1] = str(directory)
    if sym_on:
        args[args.index("-o"):args.index("-o")] = ["-sym", "on"]
    built = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=300)
    scratch = directory / compiled.name
    if built.returncode or not scratch.exists():
        return None
    output = directory / "lines.json"
    diff = subprocess.run([str(objdiff_cli()), "diff", "-1", str(ROOT / unit["target_path"]), "-2", str(scratch),
                           "-o", str(output), "--format", "json", item["symbol"]],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    data = read_json(output, {}) if not diff.returncode else {}
    pick = lambda side: next((s for s in data.get(side, {}).get("symbols", []) if s.get("name") == item["symbol"]), None)
    left, right = pick("left"), pick("right")
    if left and right and cache_key:
        SCRATCH_CACHE.put(cache_key, (left, right))
    return (left, right) if left and right else None


def scratch_score(item: dict[str, Any], directory: Path) -> float | None:
    """The symbol's objdiff match for whatever the owner source currently holds."""
    pair = _scratch_symbols(item, directory, sym_on=False)
    return None if pair is None else pair[0].get("match_percent")


def instruction_rows(left: dict[str, Any], right: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    """Keep objdiff's aligned rows, including gaps and source locations."""
    rows = []
    for index in range(max(len(left.get("instructions") or []), len(right.get("instructions") or []))):
        cell = lambda symbol: ((symbol.get("instructions") or [])[index].get("instruction") or {}) \
            if index < len(symbol.get("instructions") or []) else {}
        want, have = cell(left), cell(right)
        rows.append((want.get("formatted", ""), have.get("formatted", ""), have.get("line_number")))
    return rows


def _objdiff_rows(item: dict[str, Any], directory: Path) -> list[tuple[str, str, int | None]] | None:
    """(target, ours, our source line) per aligned row, from a scratch `-sym on` compile."""
    pair = _scratch_symbols(item, directory)
    return instruction_rows(*pair) if pair is not None else None


def measured_brief(brief: str, pair: tuple[dict[str, Any], dict[str, Any]]) -> str:
    """Replace the built object's stale score/table with the current scratch measurement."""
    left, right = pair
    rows = instruction_rows(left, right)
    pct = left.get("match_percent")
    score = f"{pct:.6f}%" if pct is not None else "n/a"
    brief = re.sub(r"(?m)^- \*\*Current match\*\*:.*$", f"- **Current match**: {score}", brief)
    deltas = sum(want != have for want, have, _ in rows)
    brief = re.sub(r"(?m)^- \*\*Instruction deltas\*\*:.*$",
                   f"- **Instruction deltas**: {deltas} of {len(rows)}", brief)
    table = "   #  TARGET (want)                          OURS (have)\n" + "\n".join(
        f"{'>>' if want != have else '  '}{k:>3}  {want:<38} {have}".rstrip()
        for k, (want, have, _) in enumerate(rows))
    return re.sub(r"(## Instruction diff[^\n]*\n\s*```[^\n]*\n).*?(\n```)",
                  lambda match: match[1] + table + match[2], brief, count=1, flags=re.DOTALL)


def line_annotated_hunks(item: dict[str, Any], source_text: str, limit: int = 10,
                         rows: list[tuple[str, str, int | None]] | None = None) -> list[str] | None:
    """Real-difference hunks, shape first, each naming the C statements that produced it."""
    try:
        if rows is None:
            with tempfile.TemporaryDirectory() as temporary:
                rows = _objdiff_rows(item, Path(temporary))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not rows:
        return None
    source_lines = source_text.splitlines()
    kinds = {k: diff_class(want, have) for k, (want, have, _) in enumerate(rows) if want != have}
    real = [k for k, kind in kinds.items() if kind in ("structural", "register")]
    hunks, seen = [], set()
    for k in real:
        if k in seen:
            continue
        span = [j for j in range(k - 2, k + 3) if 0 <= j < len(rows)]
        seen.update(span)
        label = "structural" if any(kinds.get(j) == "structural" for j in span) else "register-only"
        # Label the columns in every hunk: large prompts omit the full table and its header.
        body = f"   {'TARGET (retail, want)':<38} OURS (this source, have)\n" + \
            "\n".join(f"{'-> ' if j in real else '   '}{rows[j][0]:<38} {rows[j][1]}" for j in span)
        # A target-only row has no line of ours; its neighbours' lines are the place to look.
        numbers = sorted({rows[j][2] for j in span if rows[j][2]})
        statements = "\n".join(f"    {source_lines[n - 1].strip()}" for n in numbers if 0 < n <= len(source_lines))
        text = f"[{label}]\n{body}"
        if statements:
            text += f"\nproduced by these lines of the assigned function:\n{statements}"
        hunks.append((label, text, numbers))
    hunks.sort(key=lambda pair: pair[0] != "structural")
    # Every structural hunk's lines, in order, so focused retries can move past the first one.
    item["focus_hunks"] = [numbers for label, _, numbers in hunks if label == "structural" and numbers]
    item["focus_hunk_texts"] = [text for label, text, numbers in hunks if label == "structural" and numbers]
    if item["focus_hunks"]:
        item["focus_lines"] = item["focus_hunks"][0]
    return [text for _, text, _ in hunks[:limit]]


TYPE_WORD = re.compile(r"\b[A-Za-z_]\w*\b")
_HEADER_CACHE: dict[str, str] = {}


def _headers_text() -> str:
    if "all" not in _HEADER_CACHE:
        parts = []
        for path in sorted((ROOT / "include").rglob("*.h")):
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        _HEADER_CACHE["all"] = "\n".join(parts)
    return _HEADER_CACHE["all"]


def _type_definition(name: str, text: str) -> str:
    """Return the typedef/struct/union/enum block that defines `name`, if present."""
    patterns = (rf"typedef\s+(?:struct|union|enum)\s*\w*\s*\{{", rf"(?:struct|union|enum)\s+{name}\s*\{{")
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            depth, index = 0, match.end() - 1
            while index < len(text):
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
                if depth == 0:
                    break
            tail = re.match(r"\s*(\w+)?\s*;", text[index:index + 80])
            block = text[match.start():index + (tail.end() if tail else 0)]
            if pattern.startswith("typedef") and not (tail and tail.group(1) == name):
                continue
            return block.strip()
    alias = re.search(rf"typedef\s+[^;{{}}]*\b{name}\s*;", text)
    return alias.group(0) if alias else ""


def type_context(body: str, owner_source: str, budget: int = 3500) -> str:
    """Referenced types and their dependencies, including lowercase SDK typedefs."""
    headers = _headers_text()
    definitions = source_mask(owner_source + "\n" + headers)
    declared = set(re.findall(r"\}\s*(\w+)\s*;|typedef\s+[^;{}]*?\b(\w+)\s*;|"
                              r"\b(?:struct|union|enum)\s+(\w+)\s*\{", definitions))
    names_available = {name for row in declared for name in row if name}
    names = list(dict.fromkeys(n for n in TYPE_WORD.findall(source_mask(body)) if n in names_available))
    found, seen, blocks = [], set(), set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        block = _type_definition(name, owner_source) or _type_definition(name, headers)
        if block and block not in blocks and len(block) <= budget:
            blocks.add(block)
            found.append(f"```c\n{block}\n```")
            budget -= len(block)
            names.extend(n for n in TYPE_WORD.findall(source_mask(block)) if n in names_available and n not in seen)
    return ("## Types used by this function (from the repo; use these real fields)\n\n" + "\n\n".join(found)) if found else ""


MWCC_GUIDE = """## How MWCC source shape maps to these differences

Structure (fix these first):
- An extra or missing `b` next to a conditional branch, or `beq` where the target has `bne`,
  means the if/else, loop or early-return structure differs: invert the test, swap the
  branches, or turn a loop into the target's shape (for/while/do-while, test at top vs bottom).
  Separate early returns and one `||` condition compile differently; so do a switch and an
  if-chain, and `if (a->next == NULL) x = y; else x = a->next;` vs `x = a->next; if (!x) x = y;`.
- An extra `clrlwi`/`extsb`/`extsh`/`rlwinm` (or a missing one), or `cmpwi` vs `cmplwi`,
  means an integer width or signedness differs: fix the variable, field, parameter or return
  type (s8/u8/s16/u16/s32/u32). A value printed with %d, or compared signed, is signed.
- A load the target repeats but we hoist (or the reverse) means the source re-reads memory:
  read the field again instead of caching it in a local, pass a `const T*` (a const pointer
  parameter makes MWCC re-read through it), or the reverse.
- An extra `mr` copy of a value into another register, or a copy the target has and we
  lack, usually marks the boundary of an inlined helper: the target may have expanded a small
  function (one that also exists, or is expanded at other sites, in the same file) there.
- A literal the target loads from a named constant while we emit `@NNN` (or the reverse) is
  data layout, not something the function body can fix.

Registers (fix only after the structure matches):
- MWCC assigns callee-saved registers r31 downwards. Locals of inlined helpers are coloured
  first: the first inlined expansion colours its locals in reverse declaration order, then
  its parameters; later expansions colour parameters first, then locals in order. After that,
  the function's own locals are coloured in declaration order (top level, then inner blocks).
  A value reuses the lowest already-used register that doesn't overlap it, otherwise it
  takes the next new one. So to move a variable to a higher register, declare it earlier.
- Only register numbers differ: change declaration order and variable lifetimes, and keep
  every instruction that already matches.
- Never use dummy locals, self-assignments, volatile, pragmas or asm to force registers.
"""


FILE_SCOPE_DECL = r"(?m)^(?:extern\s+|static\s+)?[A-Za-z_][^\n;(){{}}=]*\b{name}\b[^\n;(){{}}]*(?:=[^\n;]*)?;"


def mwcc_context(hunks: list[str]) -> str:
    """Show only compiler hypotheses relevant to the observed mismatches."""
    instructions = "\n".join(line[3:] for hunk in hunks if hunk.startswith("[structural]")
                             for line in hunk.splitlines() if line.startswith("-> "))
    opcodes = set(re.findall(r"\b(?:b[a-z]+|mr|lwz|lhz|lha|lbz|lfs|lfd|clrlwi|extsb|extsh|rlwinm|cmpwi|cmplwi)\b",
                             instructions))
    paragraphs = re.findall(r"(?m)^- .*?(?=^- |\n\n|\Z)", MWCC_GUIDE, flags=re.DOTALL)
    hints = []
    if any(op.startswith("b") and op not in {"bl", "blr", "bctrl"} for op in opcodes) or re.search(r"\bb\b", instructions):
        hints.append(paragraphs[0])
    if opcodes & {"clrlwi", "extsb", "extsh", "rlwinm", "cmpwi", "cmplwi"}:
        hints.append(paragraphs[1].replace("fix the variable, field, parameter or return\n  type",
                                        "check local variable widths and explicit conversions against the declared\n  field, parameter and return types"))
    if opcodes & {"lwz", "lhz", "lha", "lbz", "lfs", "lfd"}:
        hints.append("- Repeated versus cached loads can reflect aliasing and value lifetimes. Compare when the\n"
                     "  existing source reads fields and when calls may modify them; preserve memory access semantics.")
    if "mr" in opcodes:
        hints.append(paragraphs[3])
    if any(h.startswith("[register-only]") for h in hunks):
        hints.extend(paragraphs[5:7])
    if not hints:
        return ""
    return ("## How MWCC source shape maps to these differences\n\n"
            "These are hypotheses to test, not proof of the original source. Fix structural differences first;\n"
            "preserve the signature, declared types, side effects and evaluation dependencies.\n\n"
            + "\n".join(hints) + "\n")


def same_file_context(item: dict[str, Any], source: str, budget: int = 4500) -> str:
    """What the rest of the translation unit contributes to this function's code.

    MWCC inlines same-file definitions (deferred inlining even ones defined later) and
    pools the file's constants, so the callees defined in this file and the file-scope
    declarations of the data it touches often decide the instructions the diff shows.
    """
    try:
        index = corpus.load_index(ROOT, REPORT_FILE, STATE_DIR / "corpus_index.json")
    except (OSError, ValueError):
        return ""
    row = index["functions"].get(item["symbol"]) or {}
    parts, used = [], 0
    for callee in row.get("callees", []):
        try:
            start, end = function_span(source, callee)
        except ValueError:
            continue
        body = source[start:end].strip()
        if len(body) > 1800 or used + len(body) > budget:
            continue
        if re.search(r"#\s*pragma\b|\b(?:asm|__asm|__asm__)\b", body):
            continue
        parts.append(f"### {callee} (defined in this file; MWCC may inline it here)\n```c\n{body}\n```")
        used += len(body)
    decls = []
    for name in row.get("data", [])[:24]:
        match = re.search(FILE_SCOPE_DECL.format(name=re.escape(name)), source)
        if match and len(match.group(0)) < 200:
            decls.append(match.group(0).strip())
    if decls:
        parts.append("### File-scope declarations of the data this function uses\n```c\n" + "\n".join(decls) + "\n```")
    if not parts:
        return ""
    return "## Same-file context (this translation unit)\n\n" + "\n\n".join(parts)


DIFF_TABLE_BUDGET = 9000


def cap_diff_table(brief: str, budget: int = DIFF_TABLE_BUDGET) -> str:
    """Keep the full instruction table only when it fits; large functions get the hunks alone.

    Whole-corpus runs reach multi-kilobyte functions whose table alone would overflow the
    context window. The ranked, line-annotated hunks carry what the model needs to change.
    """
    start = brief.find("## Instruction diff")
    if start < 0:
        return brief
    fence = brief.find("```", start)
    close = brief.find("```", fence + 3) if fence >= 0 else -1
    if fence < 0 or close < 0 or close - fence <= budget:
        return brief
    rows = brief[fence + 3:close].count("\n")
    note = (f"(The full {rows}-row instruction table is omitted for length; the real differences "
            "below show a bounded selection of mismatched regions and their source lines.)\n")
    return brief[:fence] + note + brief[close + 3:]


def refinement_guidance(item: dict[str, Any], hunks: list[str]) -> str:
    structural = sum(hunk.startswith("[structural]") for hunk in hunks)
    registers = sum(hunk.startswith("[register-only]") for hunk in hunks)
    mode = ("Investigate the first structural mismatch and its source statements."
            if structural else
            "The displayed residue is register-only: investigate existing locals' declaration order, "
            "first use and last use, preserving control flow and memory accesses."
            if registers else
            "No actionable mismatch survived display filtering. Do not infer byte equality from this.")
    return ("## Refinement evidence and strategy\n\n"
            f"Fresh scratch score: {item.get('prompt_pct')}%. Displayed hunks: "
            f"{structural} structural, {registers} register-only. These counts describe the bounded display.\n"
            + mode + "\n"
            "Use the assigned source and its freshly measured diff as the starting point. "
            "Raw m2c output is a hypothesis; matched references are examples, not target definitions. "
            "Prefer target declarations, compiler flags and observed accesses over inferred types or names.\n"
            "For a near-exact function, test one local hypothesis per replacement. Preserve unaffected "
            "expressions, literal spelling, casts, calls, memory accesses and evaluation order. "
            "Declaration reordering and equivalent expression forms are candidates to measure, not "
            "guarantees about register allocation. Do not add dummy state to force registers.\n"
            "A fuzzy score of 100% is not a byte/relocation or linked-unit certificate. "
            "The harness compiles proposals, retains improvements and separately verifies exactness.\n")


def minimal_brief(item: dict[str, Any]) -> str:
    """The brief skeleton used without a project brief tool; measured_brief fills in its score and table."""
    return (f"# {item['symbol']} ({item['source']}, unit {item.get('unit', '?')})\n\n"
            "- **Current match**: n/a\n- **Instruction deltas**: n/a\n\n"
            "## Instruction diff (target vs ours)\n\n```\n\n```")


def project_brief(item: dict[str, Any]) -> str:
    """The project's own objdiff brief (BRIEF_TOOL) when it ships one, else a minimal skeleton."""
    if BRIEF_TOOL is None or not BRIEF_TOOL.is_file():
        return minimal_brief(item)
    result = subprocess.run(
        [harness_paths.python_command(), str(BRIEF_TOOL), "--source", item["source"], "--symbol", item["symbol"]],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-1600:])
    return result.stdout


def build_prompt_unlocked(item: dict[str, Any], source: str) -> tuple[str, str]:
    brief = project_brief(item).split("## Current C source", 1)[0].rstrip()
    # Legacy brief levers allow compiler shaping; the campaign contract does not.
    brief = re.sub(r"## Goal\b.*?(?=## Instruction diff)", "", brief, flags=re.DOTALL)
    brief = brief.split("## Levers", 1)[0].rstrip()
    with tempfile.TemporaryDirectory() as temporary:
        pair = _scratch_symbols(item, Path(temporary), use_cache=True)
    if pair is None:
        raise RuntimeError("Cannot compile the current prompt source to a scratch object; refusing to use a stale instruction diff")
    rows = instruction_rows(*pair)
    item["prompt_pct"] = pair[0].get("match_percent")
    brief = measured_brief(brief, pair)
    brief, hunks = sharpen_diff(brief)
    brief = cap_diff_table(brief)
    annotated = line_annotated_hunks(item, source, rows=rows)
    if annotated:
        hunks = annotated
    # Register-only residue is an allocation problem the model rarely reasons its way out of;
    # the runner uses this count to spend its attempts on shape differences first.
    item["structural_hunks"] = sum(hunk.startswith("[structural]") for hunk in hunks)
    replayed = register_replay_section(item, hunks)
    start, end = function_span(source, item["symbol"])
    real = ("## Real differences to fix (`->` marks the mismatched rows)\n\n"
            "Rows that differ only by branch displacement or by a compiler literal (`@NNN`) versus a named\n"
            "constant may be filtered for readability; relocation identity still requires verification. Fix the [structural] hunks first\n"
            "(an instruction the target has and we lack, or the reverse); [register-only] hunks usually\n"
            "resolve once the structure matches. Where shown, \"produced by\" lists the statements of the\n"
            "assigned function that compiled to that hunk: that is where to change the source.\n\n"
            + "\n\n".join(f"```\n{h}\n```" for h in hunks[:12])) if hunks else ""
    types = type_context(source[start:end], source)
    dossier_context = investigation_context(item, source) if INVESTIGATION else ""
    if item["prompt_pct"] == 100.0:
        instruction = ("This source already measures exactly 100% in the fresh scratch compile. Preserve it;\n"
                       "the harness will verify it through the normal candidate and review gates.")
    elif is_placeholder(source[start:end], len(diff_columns(brief)[0])):
        instruction = PLACEHOLDER_NOTE
    else:
        instruction = ("The code below does NOT yet compile to the target. Returning it unchanged is a failed attempt.\n"
                       "Make the smallest source changes that fix the real differences listed above.")
    prompt = f"""{refinement_guidance(item, hunks)}

{brief}

{real}

{replayed}

{mwcc_context(hunks)}
{types}

{dossier_context}

{assembly_draft_context(item)}

## Assigned function source

{instruction}

```c
{source[start:end].strip()}
```

{same_file_context(item, source)}

{corpus_context(item, source)}

{library_reference(item["symbol"])}

## Response contract

Return exactly one complete replacement definition for `{item['symbol']}` in a
single ```c fenced block and no other text. Preserve the function signature.
{source_policy_contract()}
"""
    return brief, prompt


def source_policy_contract() -> str:
    return """## Source constraints

Use natural, semantically faithful C89 only. Never add pragmas, preprocessor
directives, inline assembly, `.inc`, compiler intrinsics, fake volatility,
artificial aliases or layouts, dummy/self assignments, uninitialized reads, or
code whose only purpose is compiler shaping. Do not change headers, other
functions, or compiler flags. An exact result still undergoes human audit.
"""


def investigation_session(item: dict[str, Any], source: str) -> investigation.Session:
    index = corpus.load_index(ROOT, REPORT_FILE, STATE_DIR / "corpus_index.json")
    start, end = function_span(source, item["symbol"])
    address = item.get("address")
    if address is not None:
        address = int(str(address), 16 if str(address).startswith("0x") else 10)

    def declaration(name):
        return "\n".join(corpus.callee_declarations(
            {"functions": {**index["functions"], item["symbol"]: {"callees": [name]}}},
            item["symbol"], source, _headers_text(), ROOT, limit=1))

    return investigation.Session(ROOT, index, item["symbol"], source[start:end],
                                 function_signature(source, start, end),
                                 report_sha256=investigation.digest(REPORT_FILE.read_bytes()) if REPORT_FILE.exists() else None,
                                 declaration=declaration, reference_body=_clean_body,
                                 expected_address=address, expected_unit=item.get("unit"),
                                 report_file=REPORT_FILE if REPORT_FILE.exists() else None)


def investigation_context(item: dict[str, Any], source: str) -> str:
    try:
        dossier = investigation_session(item, source).dossier()
        item["investigation"] = {"available": True, "dossier": dossier}
        return ("## Target evidence dossier\n"
                "These are observed target facts, not inferred source types or proof of a candidate.\n"
                + investigation.encoded(dossier) + "\n")
    except (OSError, ValueError, UnicodeError) as error:
        item["investigation"] = {"available": False, "reason": str(error)[:300]}
        return "## Target evidence dossier\nUnavailable: " + str(error)[:300] + ". No target facts inferred.\n"


def generate_with_context(state: dict, item: dict, source: str, prompt: str, folder: Path,
                          attempt: int, host: str, model: str, timeout: int, num_predict: int,
                          temperature: float, seed: int) -> tuple[str, str]:
    captured = item.get("investigation", {})
    session = investigation_session(item, source) if INVESTIGATION and CONTEXT_REQUESTS and captured.get("available") else None
    if session and session.fingerprint != captured["dossier"]["fingerprint"]:
        raise StaleSource("Target dossier inputs changed before context generation")
    records = []
    calls = []

    def record(row):
        if row.get("model_call") and calls:
            row["timings"] = calls[-1]
        records.append(row)
        item["context_model_calls"] = sum(row.get("model_call") is True for row in records)
        item["context_lookup_rounds"] = sum(row.get("model_call") is False for row in records)
        write_json(folder / f"attempt-{attempt:03d}.investigation.json", {
            "version": investigation.VERSION, "dossier": captured, "records": records,
            "model_calls": item["context_model_calls"], "lookup_rounds": item["context_lookup_rounds"],
            "timeout_seconds": timeout, "max_lookup_rounds": investigation.MAX_ROUNDS if session else 0,
        })

    def send(current, round_number, remaining):
        path = folder / f"attempt-{attempt:03d}.context-{round_number:02d}.response.md"
        for shrink in range(3):
            (folder / f"attempt-{attempt:03d}.context-{round_number:02d}.prompt.md").write_text(current, encoding="utf-8", newline="")
            call_started = time.monotonic()
            LAST_TIMINGS.clear()
            try:
                response = ollama(host, model, current, remaining, num_predict,
                                  lambda **metrics: activity(state, item, "Generating candidate with local model",
                                      f"Streaming generation round {round_number + 1} from {host.rstrip('/')}.", **metrics),
                                  response_path=path, temperature=temperature, seed=seed + round_number)
                calls.append(dict(LAST_TIMINGS))
                return response, current
            except PromptTooLarge as error:
                remaining -= time.monotonic() - call_started
                if remaining <= 0:
                    raise TimeoutError("Context generation deadline exhausted") from error
                smaller = compact_prompt(current, error.n_prompt, error.n_ctx, num_predict) if shrink < 2 else None
                if smaller is None:
                    raise ValueError(f"{error}; the prompt could not be cut to fit") from error
                current = smaller
            finally:
                if path.exists():
                    (folder / f"attempt-{attempt:03d}.response.md").write_bytes(path.read_bytes())
        raise AssertionError("Unreachable prompt recovery")

    if session:
        prompt += "\n\n" + investigation.CONTRACT
        response, prompt = investigation.exchange(prompt, send, session.query, record, timeout=timeout)
    else:
        response, prompt = send(prompt, 0, timeout)
        record({"round": 0, "model_call": True, "prompt_sha256": digest(prompt),
                "response_sha256": digest(response), "response": response})
    (folder / f"attempt-{attempt:03d}.prompt.md").write_text(prompt, encoding="utf-8", newline="")
    if len(calls) > 1:
        for prefix in ("prompt", "predicted"):
            token_values = [row.get(prefix + "_n") for row in calls]
            rates = [row.get(prefix + "_per_second") for row in calls]
            if all(type(value) in (int, float) for value in token_values):
                LAST_TIMINGS[prefix + "_n"] = sum(token_values)
                if all(type(rate) in (int, float) and rate > 0 for rate in rates):
                    seconds = sum(value / rate for value, rate in zip(token_values, rates))
                    LAST_TIMINGS[prefix + "_per_second"] = sum(token_values) / seconds if seconds else None
                else:
                    LAST_TIMINGS[prefix + "_per_second"] = None
            else:
                LAST_TIMINGS[prefix + "_n"] = None
                LAST_TIMINGS[prefix + "_per_second"] = None
    return response, prompt


def repeated_tail(answer: str) -> bool:
    """Detect degenerate decoding without mistaking ordinary short C for a loop."""
    compact = re.sub(r"\s+", " ", answer[-4096:]).strip()
    if len(compact) < 768:
        return False
    for width in range(24, min(256, len(compact) // 4) + 1):
        tail = compact[-width:]
        if compact.endswith(tail * 4):
            return True
    return False


def context_window(prompt: str, num_predict: int) -> int:
    """Use the worker's context when the prompt fits; never let Ollama truncate a large prompt."""
    needed = len(prompt) // 3 + num_predict + 512
    return NUM_CTX if needed <= NUM_CTX else max(NUM_CTX, DEFAULT_NUM_CTX)


def ollama_payload(model: str, prompt: str, num_predict: int, temperature: float = 0.15,
                   seed: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model, "prompt": prompt, "stream": True, "keep_alive": "20m",
        "options": {"temperature": temperature, "num_ctx": context_window(prompt, num_predict), "num_predict": num_predict,
                    **SAMPLING, **({"seed": seed} if seed is not None else {})},
    }
    if THINK is not None:
        payload["think"] = THINK
    return payload


# The server's own measurements for the latest request (llama-server sends them with the final chunk).
LAST_TIMINGS: dict[str, Any] = {}


def openai_payload(model: str, prompt: str, num_predict: int, temperature: float = 0.15,
                   seed: int | None = None) -> dict[str, Any]:
    """An OpenAI-compatible chat request (llama.cpp's llama-server). The server fixes its context size
    at launch, so there is no per-request num_ctx; the chat template is applied server-side, and
    --think reaches it as the template's enable_thinking switch."""
    payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True,
                               "max_tokens": num_predict, "temperature": temperature, **SAMPLING}
    if seed is not None:
        payload["seed"] = seed
    kwargs = {**({"enable_thinking": THINK} if THINK is not None else {}), **TEMPLATE_KWARGS}
    if kwargs:
        payload["chat_template_kwargs"] = kwargs
    return payload


def openai_packet(line: bytes) -> dict[str, Any] | None:
    """One server-sent event from a streaming chat completion, in the shape of an Ollama packet."""
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    body = text[5:].strip()
    if body == "[DONE]":
        return {"done": True, "done_reason": "stop"}
    event = json.loads(body)
    if event.get("error"):
        return {"error": event["error"]}
    choice = (event.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")
    return {"response": delta.get("content") or "", "thinking": delta.get("reasoning_content"),
            "done": finish is not None, "done_reason": finish,
            "eval_count": (event.get("usage") or {}).get("completion_tokens"), "timings": event.get("timings")}


def ollama(host: str, model: str, prompt: str, timeout: int, num_predict: int, progress, response_path: Path | None = None,
           temperature: float = 0.15, seed: int | None = None, response_format: dict | None = None) -> str:
    """Stream one answer. A host ending in /v1 is an OpenAI-compatible server (llama-server);
    any other host is Ollama."""
    global REASONING_SEEN
    compatible = host.rstrip("/").endswith("/v1")
    if response_format is not None and not compatible:
        raise ValueError("Constrained response formats require the compatible /v1 endpoint")
    if response_format is not None and response_format.get("type") == "json_schema":
        wrapper = response_format.get("json_schema")
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("schema"), dict) or not wrapper["schema"]:
            raise ValueError("json_schema response format requires a nonempty nested json_schema.schema")
    payload = (openai_payload if compatible else ollama_payload)(model, prompt, num_predict, temperature, seed)
    if response_format is not None:
        payload["response_format"] = response_format
    request = urllib.request.Request(
        host.rstrip("/") + ("/chat/completions" if compatible else "/api/generate"),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    chunks, completed, started = [], False, time.monotonic()
    thinking_chars = 0
    LAST_TIMINGS.clear()
    try:
        # A silent stream must fail fast: cap each read, while the loop below enforces the total deadline.
        with urllib.request.urlopen(request, timeout=min(timeout, STALL_SECONDS)) as response:
            last_update = 0.0
            for line in response:
                if STOP_REQUESTED:
                    raise InterruptedError("worker stopped; partial response retained")
                if time.monotonic() - started > timeout:
                    raise TimeoutError("model exceeded total generation deadline")
                if not line.strip():
                    continue
                packet = openai_packet(line) if compatible else json.loads(line)
                if packet is None:
                    continue
                if packet.get("error"):
                    raise RuntimeError(f"model server error: {packet['error']}")
                if packet.get("timings"):
                    LAST_TIMINGS.update(packet["timings"])
                piece = packet.get("response")
                if isinstance(piece, str):
                    chunks.append(piece)
                    if repeated_tail("".join(chunks)):
                        raise RepeatedResponse("Ollama response entered a repeated-output loop")
                # Thinking models stream their reasoning separately; count it so a long
                # reasoning phase shows progress instead of looking stalled.
                if isinstance(packet.get("thinking"), str):
                    thinking_chars += len(packet["thinking"])
                    REASONING_SEEN = True
                moment = time.monotonic()
                if moment - last_update >= 1.0 or packet.get("done"):
                    answer_so_far = "".join(chunks)
                    progress(
                        thinking_chars=thinking_chars,
                        response_chars=len(answer_so_far),
                        response_chunks=len(chunks),
                        response_preview=answer_so_far[-8000:],
                        eval_count=packet.get("eval_count"),
                        eval_duration_ns=packet.get("eval_duration"),
                        done_reason=packet.get("done_reason"),
                    )
                    last_update = moment
                if packet.get("done"):
                    if packet.get("done_reason") == "length":
                        raise IncompleteResponse("model reached output cap; partial response retained")
                    completed = True
                    break
            answer = "".join(chunks)
    except urllib.error.URLError as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 400 and not chunks:
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace") or "{}").get("error") or {}
            except (OSError, ValueError, AttributeError):
                body = {}
            if body.get("type") == "exceed_context_size_error" and body.get("n_prompt_tokens") and body.get("n_ctx"):
                raise PromptTooLarge(int(body["n_prompt_tokens"]), int(body["n_ctx"])) from exc
        if not chunks:
            raise HostUnavailable(f"Ollama request failed: {exc}") from exc
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
    finally:
        if response_path is not None:
            response_path.write_text("".join(chunks), encoding="utf-8", newline="")
    if not completed:
        raise IncompleteResponse("model stream ended without completion; partial response retained")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Ollama returned no candidate")
    return answer


def verify(item: dict[str, Any], candidate: Path, worker: str = MODEL_WORKER, link: bool = False) -> dict[str, Any]:
    waiting = time.monotonic()
    with COORDINATOR.build(worker, f"Verify: {item['symbol']}"):
        COMMAND_SECONDS["build_lock_wait"] = COMMAND_SECONDS.get("build_lock_wait", 0) + time.monotonic() - waiting
        if (STATE_DIR / "halt.json").exists():
            raise RestorationError("fleet halted after baseline restoration failure; inspect halt.json")
        check_baseline(item)
        try:
            return verify_unlocked(item, candidate, link)
        except RestorationError as exc:
            write_json(STATE_DIR / "halt.json", {"at": timestamp(), "worker": worker, "error": str(exc)})
            raise


def verify_unlocked(item: dict[str, Any], candidate: Path, link: bool = False) -> dict[str, Any]:
    if link:
        raise RuntimeError("full-unit policy and relocation review must precede promotion/link testing")
    return verify_shim_owner(item, candidate)


def checked_command(command: list[str], timeout: int, cwd: Path | None = None):
    """Reap the entire tool process group before restoring shared source."""
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd or ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, **portable.popen_group_kwargs())
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        portable.kill_tree(process.pid)
        process.communicate()
        raise
    finally:
        stage = "diff" if "objdiff" in Path(command[0]).name else "build" if Path(command[0]).name == "ninja" else "other_tools"
        COMMAND_SECONDS[stage] = COMMAND_SECONDS.get(stage, 0) + time.monotonic() - started
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def objdiff_cli() -> Path:
    path = harness_paths.build_tool(ROOT, "objdiff-cli")
    if path.exists():
        return path
    raise RuntimeError("objdiff-cli is missing; run ninja once")


def verify_shim_owner(item: dict[str, Any], candidate: Path) -> dict[str, Any]:
    """Measure an ignored owner candidate through its declared shim unit."""
    owner = ROOT / item.get("owner_source", item["source"])
    compiled = ROOT / build_relative(Path(item["source"]).with_suffix(".o"))
    original = owner.read_bytes()
    output = ""
    try:
        owner.write_bytes(candidate.read_bytes())
        compiled.unlink(missing_ok=True)
        build = checked_command(["ninja", "-j2", ninja_target(compiled.relative_to(ROOT))], timeout=900)
        output = (build.stdout + "\n" + build.stderr).strip()
        if build.returncode or not compiled.exists():
            return {"returncode": 1, "pct": None, "deltas": None, "linked": False, "output_tail": output[-6000:]}
        with tempfile.TemporaryDirectory() as temporary:
            diff_path = Path(temporary) / "diff.json"
            diff = checked_command(
                [str(objdiff_cli()), "diff", "-p", str(ROOT), "-u", item["unit"], "-o", str(diff_path), "--format", "json", item["symbol"]],
                timeout=120,
            )
            output = (output + "\n" + diff.stdout + "\n" + diff.stderr).strip()
            if diff.returncode or not diff_path.exists():
                return {"returncode": 1, "pct": None, "deltas": None, "linked": False, "output_tail": output[-6000:]}
            data = read_json(diff_path, {})
            left = next((symbol for symbol in data.get("left", {}).get("symbols", []) if symbol.get("name") == item["symbol"]), None)
            right = next((symbol for symbol in data.get("right", {}).get("symbols", []) if symbol.get("name") == item["symbol"]), None)
            if not left or not right:
                return {"returncode": 1, "pct": None, "deltas": None, "linked": False, "output_tail": "target symbol not found in objdiff output"}
            left_rows, right_rows = left.get("instructions") or [], right.get("instructions") or []
            def formatted(rows: list[dict[str, Any]], index: int) -> str:
                return "" if index >= len(rows) else (rows[index].get("instruction") or {}).get("formatted", "")
            deltas = sum(formatted(left_rows, index) != formatted(right_rows, index) for index in range(max(len(left_rows), len(right_rows))))
            pct = left.get("match_percent")
            rows = []
            for index in range(max(len(left_rows), len(right_rows))):
                want, have = formatted(left_rows, index), formatted(right_rows, index)
                kind = diff_class(want, have) if want != have else "same"
                if kind in ("structural", "register"):
                    rows.append((kind != "structural", f"[{kind}] target: {want or '(none)'} | candidate: {have or '(none)'}"))
            # Shape differences first; branch-offset and literal-naming rows are noise here too.
            feedback = [text for _, text in sorted(rows, key=lambda row: row[0])]
            if pct != 100.0:
                # The candidate is still in place: name the statements behind each remaining hunk.
                annotated = line_annotated_hunks(item, owner.read_text(encoding="utf-8", errors="replace"), limit=6)
                if annotated:
                    feedback = annotated
            return {"returncode": 0 if pct == 100.0 else 1, "pct": pct, "deltas": deltas, "linked": False,
                    "diff_feedback": feedback[:24], "output_tail": output[-6000:]}
    finally:
        owner.write_bytes(original)
        compiled.unlink(missing_ok=True)
        try:
            restored = checked_command(["ninja", "-j2", ninja_target(compiled.relative_to(ROOT))], timeout=900)
            if restored.returncode or not compiled.exists():
                raise RestorationError("baseline rebuild failed: " + (restored.stdout + restored.stderr)[-1200:])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RestorationError(f"baseline rebuild failed: {exc}") from exc


RETRY_TEMPERATURES = (0.15, 0.45, 0.7, 0.9)
# Under DIVERSIFY, the floor for an attempt after unchanged or repeated answers, by how many came in a row.
HOT_TEMPERATURES = (1.0, 0.85, 1.15)
WASTED = {"no_change", "duplicate"}  # answers that changed nothing new: nothing was built
# Under DIVERSIFY, one kind of edit named per attempt after a wasted answer, in rotation, so the model
# stops returning the same text. Each is a change MWCC is known to respond to.
EDIT_STRATEGIES = (
    "reorder the local declarations (MWCC hands out saved registers largely by declaration order)",
    "change a loop's shape: for to while or do-while, count up instead of down, or index instead of pointer walk",
    "change the types or casts of the values involved (signed/unsigned, s32/s16/u8, an explicit cast)",
    "introduce a temporary for a repeated expression, or remove one that holds a single-use value",
    "reorder independent statements, or move a computation to just before its first use",
    "restructure a branch: invert the condition, swap if/else, or turn an if-chain into a switch or back",
    "change how a field or element is reached: through a local pointer, array indexing, or the base pointer",
)


def applicable_strategies(function: str) -> tuple[str, ...]:
    """The EDIT_STRATEGIES this function has something to apply to: no loop reshaping without a loop,
    no branch restructuring without a branch, no reordering in a one-statement body."""
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", function, flags=re.S)
    needs = {
        1: re.search(r"\b(?:for|while|do)\b", body),
        4: body.count(";") >= 2,
        5: re.search(r"\b(?:if|switch)\b|\?", body),
        6: re.search(r"->|\.\w|\[", body),
    }
    return tuple(text for index, text in enumerate(EDIT_STRATEGIES) if needs.get(index, True))


def strategy_hint(attempt: int, wasted: int, function: str = "") -> str:
    """The DIVERSIFY instruction for a whole-function attempt, or for any attempt that follows `wasted`
    unchanged or repeated answers. Without it, models tend to resend unchanged or repeated whole
    functions; with it, far more attempts produce new code worth building."""
    strategies = applicable_strategies(function) if function else EDIT_STRATEGIES
    strategy = strategies[attempt % len(strategies)]
    why = (f"Your last {wasted} answer{'s' if wasted > 1 else ''} changed nothing new, so nothing was built. "
           if wasted else "")
    return ("\n\n## Required change for this attempt\n"
            f"{why}Whatever else you do, {strategy}. An answer that returns the code unchanged or repeats "
            "an earlier answer is discarded without being compiled.\n")


def _unfinished(line: str) -> bool:
    """A code line that continues on the next line (a statement or call cut mid-way)."""
    code = re.sub(r"//.*$", "", line).rstrip()
    return bool(code) and not code.lstrip().startswith("#") \
        and not code.endswith((";", "{", "}", ":", "*/"))


def focus_region(source: str, symbol: str, lines: list[int], context: int = 1, cap: int = 14) -> tuple[int, int] | None:
    """Owner-file line range (inclusive) to rewrite: the statements behind one hunk, inside the function.

    The range never includes the function's opening brace, always covers whole statements
    (a call split over lines is taken whole), and is widened to close a block it opens
    when that stays within `cap + 6` lines. Cut statements and stray braces were the most
    common reason a focused answer failed to compile."""
    start, end = function_span(source, symbol)
    first = source.count("\n", 0, start) + 1
    last = source.count("\n", 0, end) + 1
    body = source.count("\n", 0, source.find("{", start)) + 2  # first line after the opening brace
    inside = [n for n in lines if first < n < last]
    if not inside:
        return None
    text = source.splitlines()
    low, high = max(body, min(inside) - context), min(last - 1, max(inside) + context)
    if low > high:
        return None

    def depth(opening: str, closing: str) -> int:
        chunk = "\n".join(re.sub(r"//.*$", "", line) for line in text[low - 1:high])
        return chunk.count(opening) - chunk.count(closing)

    def complete() -> None:
        nonlocal low, high
        while low > body and _unfinished(text[low - 2]):
            low -= 1
        while high < last - 1 and (_unfinished(text[high - 1]) or depth("(", ")") > 0):
            high += 1

    complete()
    if high - low >= cap:
        return None
    wide = (low, high)
    while depth("{", "}") > 0 and high < last - 1 and high - low < cap + 6:
        high += 1
        complete()
    while depth("{", "}") < 0 and low > body and high - low < cap + 6:
        low -= 1
        complete()
    if depth("{", "}") != 0 or high - low >= cap + 6:
        low, high = wide  # could not close the block within reach: keep the statement-complete range
    return low, high


CHARS_PER_TOKEN = 3.5


def exceeds_budget(function_text: str, num_predict: int) -> bool:
    """A whole-function answer would not fit the output cap (with room for the fence and slack)."""
    return len(function_text) / CHARS_PER_TOKEN > 0.75 * num_predict


FOCUS_REASONS = {
    "no_change": "Your previous answer left the function unchanged.",
    "incomplete_response": "Your previous answer ran out of output tokens before the function ended.",
    "too_large": "This function is too long to return whole within the output limit.",
    "plateau": "Whole-function rewrites have stopped raising the score.",
    "registers": "Only register numbers differ now: the instructions already match the target.",
    "near_exact": "The fresh baseline is near exact; test one local change and preserve the remaining source.",
}


DEFAULT_FOCUS_VARIANTS = 3  # alternatives asked for per focused edit: compiling one costs ~1 s, generating ~10 s
FOCUS_VARIANTS = DEFAULT_FOCUS_VARIANTS
FOCUS_TOKENS = 1536  # room for FOCUS_VARIANTS rewrites of a region of at most ~20 lines


def output_cap(configured: int, function_text: str, focused: bool, mode: str | None) -> int:
    """Tokens a correct answer needs, with slack, never above the worker's cap.

    An answer far past that is prose or a loop, not code: a short function can otherwise draw pages
    of register analysis that run for minutes into the cap.
    A retry after a cut-off answer keeps the full (already doubled) cap."""
    if mode == "incomplete_response":
        return configured
    if focused:
        return min(configured, FOCUS_TOKENS * max(FOCUS_VARIANTS, 3) // 3)
    return min(configured, max(768, int(len(function_text) / CHARS_PER_TOKEN * 2) + 384))


_SERVER_CTX: dict[str, int] = {}


def prompt_tokens(host: str, prompt: str) -> tuple[int, int] | None:
    """(prompt tokens, server context) from llama-server's /tokenize and /props, or None when the host is not
    llama-server or does not answer. Only used for long prompts, where CHARS_PER_TOKEN can be far off."""
    if not host.rstrip("/").endswith("/v1"):
        return None
    root = host.rstrip("/")[:-3]
    try:
        if root not in _SERVER_CTX:
            with urllib.request.urlopen(root + "/props", timeout=10) as reply:
                props = json.load(reply)
            _SERVER_CTX[root] = int((props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx") or 0)
        request = urllib.request.Request(root + "/tokenize", data=json.dumps({"content": prompt}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as reply:
            count = len(json.load(reply).get("tokens") or [])
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return (count + 64, _SERVER_CTX[root]) if count and _SERVER_CTX.get(root) else None  # +64: chat template


# Optional prompt sections, dropped in this order when a prompt is too long for the server's context.
PROMPT_OPTIONAL = ("## Matched code in this project", "## How MWCC source shape maps", "## Same-file context",
                   "## Named library reference", "## Assembly-derived C hypothesis",
                   "## Types used by this function", "## Declarations of the functions it calls",
                   "## Target evidence dossier")


def compact_prompt(prompt: str, n_prompt: int, n_ctx: int, num_predict: int) -> str | None:
    """A shorter prompt that should fit the server's context, calibrated on the server's own token count for
    this prompt (dense text such as Shift-JIS or assembly runs far below CHARS_PER_TOKEN). Optional sections go
    first, then the differences list is cut; None when it still cannot fit."""
    ratio = len(prompt) / max(1, n_prompt)
    target = int(max(1024, n_ctx - num_predict - 512) * ratio * 0.95)
    if len(prompt) <= target:
        target = int(len(prompt) * n_ctx / n_prompt * 0.85)
    parts = re.split(r"(?m)^(?=## )", prompt)
    for heading in PROMPT_OPTIONAL:
        if len("".join(parts)) <= target:
            break
        parts = [part for part in parts if not part.startswith(heading)]
    text = "".join(parts)
    if len(text) > target:
        for index, part in enumerate(parts):
            if part.startswith("## Real differences to fix"):
                keep = max(800, len(part) - (len(text) - target))
                parts[index] = part[:keep] + "\n... (further differences cut to fit the model's context)\n\n"
                text = "".join(parts)
                break
    return text if len(text) <= target else None


def focused_prompt(prompt: str, source: str, region: tuple[int, int], reason: str = "no_change",
                   current: str | None = None, hunk: str | None = None) -> str:
    """Ask for one hunk's lines only, in up to FOCUS_VARIANTS alternatives. `current` is the function
    being edited when it is not the assigned source (the best candidate so far), shown whole so the
    model sees what it keeps. `hunk` is the diff these lines produce, quoted beside them."""
    prompt = re.sub(r"(?ms)^## Response contract\n.*?(?=^## |\Z)", "", prompt)
    lines = source.splitlines()
    excerpt = "\n".join(lines[region[0] - 1:region[1]])
    where = "\"Assigned function source\""
    shown = ""
    if current is not None:
        where = "the current function below"
        shown = ("The function currently reads as follows. This is your best candidate so far, which the\n"
                 "harness builds on:\n\n```c\n" + current.strip() + "\n```\n\n")
    target = ("\nThese lines compile to this hunk. Change them so our column (right) emits the target's\n"
              "instructions (left):\n\n```\n" + hunk.split("\nproduced by")[0] + "\n```\n") if hunk else ""
    boundaries = focused_boundaries(source, region)
    if reason == "registers":
        what = ("are the function's local declarations. MWCC hands out saved registers largely in\n"
                "declaration order, so their order decides which register each value gets")
        ideas = ("different orders of these declarations (you may also split a combined declaration, or\n"
                 "move an initialiser into a plain assignment on its own line if it has no side effects)")
    else:
        what = "compile to one [structural] hunk"
        ideas = "different ideas (types and casts, statement order, temporaries, loop or branch shape)"
    return (prompt + "\n\n## Focused edit (this attempt)\n"
            + FOCUS_REASONS[reason] + " This time rewrite ONLY the lines below, which\n"
            f"{what}. Return exactly\n"
            f"{FOCUS_VARIANTS} alternative replacements, each in its own ```c block, and nothing else. Each must\n"
            "differ from the lines shown and from the others: try\n"
            f"{ideas}. The harness\n"
            "compiles every one and keeps the best. Each may have more or fewer lines. Do not return the rest of\n"
            "the function: the harness splices your lines back in place of these. Every other line of the function\n"
            f"stays exactly as shown in {where}, so keep any declaration or variable those\n"
            "other lines still use. If these lines open or close a block that continues outside them, keep\n"
            "that brace. Declare any new local at the top of a block, as C89 requires.\n\n"
            + shown + boundaries + "Lines to rewrite:\n\n```c\n" + excerpt + "\n```\n" + target
            + f"\nAnswer with exactly {FOCUS_VARIANTS} ```c blocks, each a different rewrite of these lines.\n")


LOCAL_DECL = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:const|volatile|unsigned|signed|struct|union|enum|register)\s+)*"
    r"(?P<type>[A-Za-z_]\w*)(?:\s+[A-Za-z_]\w*)*[\s\*]+(?P<name>[A-Za-z_]\w*)\s*(?P<array>\[[^\]]*\])?"
    r"\s*(?:=\s*(?P<init>[^;]+))?;[ \t]*(?://.*)?$"
)
NOT_A_TYPE = {"return", "goto", "case", "else", "do", "sizeof", "extern", "static", "typedef"}
MULTI_DECL = re.compile(
    r"^(?P<indent>[ \t]*)(?P<type>(?:(?:const|volatile|unsigned|signed|struct|union|enum|register)\s+)*[A-Za-z_]\w*)\s+"
    r"(?P<rest>[\*\s]*[A-Za-z_]\w*\s*(?:\[[^\]]*\])?\s*(?:=\s*[^,;()]+)?"
    r"(?:\s*,\s*[\*\s]*[A-Za-z_]\w*\s*(?:\[[^\]]*\])?\s*(?:=\s*[^,;()]+)?)+)\s*;[ \t]*(?://.*)?$"
)
DECLARATOR = re.compile(r"^(?P<ptr>[\*\s]*)(?P<name>[A-Za-z_]\w*)\s*(?P<array>\[[^\]]*\])?\s*(?:=\s*(?P<init>.+))?$")


def parse_declaration(line: str) -> tuple[str, str, list[re.Match]] | None:
    """(indent, base type, declarators) for a local declaration line, else None.

    Lists (`s32 i, j;`) are split only when no parentheses are present, so a call in an
    initializer is never cut at its argument commas."""
    match = MULTI_DECL.match(line) if "(" not in line else None
    if match and match["type"].split()[-1] not in NOT_A_TYPE:
        parts = [DECLARATOR.match(part.strip()) for part in match["rest"].split(",")]
        return (match["indent"], match["type"], parts) if all(parts) else None
    match = LOCAL_DECL.match(line)
    if match and match["type"] not in NOT_A_TYPE:
        base = line[:match.start("name")].strip().rstrip("*").strip()
        declarator = DECLARATOR.match(line[match.start("name"):].rstrip().rstrip(";").split("//")[0].strip())
        return (match["indent"], base, [declarator]) if declarator else None
    return None


def declaration_region(source: str, symbol: str) -> tuple[int, int] | None:
    """Owner-file line range (inclusive) of the local declarations that open a function's body, or
    None when there are fewer than two (nothing to reorder). Comments and blank lines inside the block
    are kept; the first statement ends it."""
    start, end = function_span(source, symbol)
    text = source.splitlines()
    body = source.count("\n", 0, source.find("{", start)) + 2  # first line after the opening brace
    last = source.count("\n", 0, end) + 1
    first = final = None
    names = 0
    for number in range(body, last):
        stripped = text[number - 1].strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            continue
        if first is None and stripped.startswith("extern "):
            continue  # block-scope prototypes ahead of the locals: not reorderable, not part of the block
        parsed = parse_declaration(text[number - 1])
        if parsed is None:
            break
        first = first or number
        final = number
        names += len(parsed[2])
    return (first, final) if first and names >= 2 else None


def local_declarations(lines: list[str]) -> dict[str, int]:
    """Local declarations, one name per declarator: name -> index of the declaring line."""
    found = {}
    for index, line in enumerate(lines):
        parsed = parse_declaration(line)
        for declarator in parsed[2] if parsed else []:
            found.setdefault(declarator["name"], index)
    return found


def parameter_names(function_lines: list[str]) -> set[str]:
    """Parameter names from the signature at the top of `function_lines` (empty when absent)."""
    head = "\n".join(function_lines).split("{", 1)[0]
    if "(" not in head:
        return set()
    inside, depth, parts, part = head[head.index("(") + 1:], 0, [], ""
    for char in inside:
        if char == ")" and depth == 0:
            break
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += char
    parts.append(part)
    names = set()
    for part in parts:
        pointer = re.search(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)", part)
        found = pointer or re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$", part.strip())
        if found and found.group(1) != "void" and len(part.split()) + part.count("*") > 1:
            names.add(found.group(1))
    return names


def repair_snippet(function_lines: list[str], first: int, last: int, snippet: list[str]) -> tuple[list[str], set[int]]:
    """Fix a focused snippet's local bookkeeping; the compiler and objdiff still judge the result.

    `function_lines[first:last]` is the region the snippet replaces. Returns the repaired
    snippet and the indices of function lines outside the region to delete.
    - A snippet declaration identical to one outside the region was moved into the region:
      the outside copy is deleted.
    - Any other snippet declaration of a name the rest of the function declares becomes an
      assignment, or is dropped when it has no initializer. An initializer with a call is left
      alone, because turning it into an assignment could move the call's side effects.
    - A region declaration the snippet dropped while other lines still use the name is
      restored at the snippet's top.
    A parameter counts as declared outside the region: redeclaring one is an MWCC error.
    """
    outside_index = list(range(first)) + list(range(last, len(function_lines)))
    outside = [function_lines[i] for i in outside_index]
    declared_outside = local_declarations(outside)
    parameters = parameter_names(function_lines)
    declared_inside = local_declarations(function_lines[first:last])
    repaired, remove = [], set()
    for line in snippet:
        parsed = parse_declaration(line)
        # A name the region itself declared lives in its own block scope: leave that declaration be.
        clash = [d for d in parsed[2] if (d["name"] in declared_outside or d["name"] in parameters)
                 and d["name"] not in declared_inside] if parsed else []
        if not clash:
            repaired.append(line)
            continue
        if len(parsed[2]) == 1 and clash[0]["name"] in declared_outside:
            origin = outside_index[declared_outside[clash[0]["name"]]]
            if " ".join(function_lines[origin].split()) == " ".join(line.split()):
                remove.add(origin)
                repaired.append(line)
                continue
        if any(d["init"] and "(" in d["init"] for d in clash):
            repaired.append(line)
            continue
        indent, base, declarators = parsed
        keep = [d for d in declarators if d not in clash]
        if keep:
            repaired.append(f"{indent}{base} " + ", ".join(d.group(0).strip() for d in keep) + ";")
        repaired.extend(f"{indent}{d['name']} = {d['init'].strip()};" for d in clash if d["init"] and not d["array"])
    kept = local_declarations(repaired)
    region = function_lines[first:last]
    remaining = [line for i, line in zip(outside_index, outside) if i not in remove]
    restored = [region[index] for name, index in sorted(local_declarations(region).items(), key=lambda pair: pair[1])
                if name not in kept and name not in declared_outside
                and any(re.search(rf"\b{re.escape(name)}\b", line) for line in remaining + repaired)]
    return restored + repaired, remove


JUMP = re.compile(r"^\s*(?:return\b[^;]*|goto\s+\w+|break|continue)\s*;\s*(?://.*)?$")


def adds_early_exit(region_lines: list[str], snippet: str) -> bool:
    """The snippet ends in a jump (return/goto/break/continue) at its own top level that the region
    did not end in, so every statement after the region would become unreachable.

    A typical case: every variant appends `return 1;` after a null check; each compiles, to a tiny
    fraction of the function."""
    def last_top_level(lines: list[str]) -> str:
        depth, last = 0, ""
        for line in lines:
            if depth == 0 and line.strip():
                last = line
            depth += line.count("{") - line.count("}")
        return last
    ending = last_top_level(snippet.splitlines())
    return bool(JUMP.match(ending)) and not JUMP.match(last_top_level(region_lines))


def brace_profile(text: str) -> tuple[int, int]:
    depth, minimum = 0, 0
    for char in source_mask(text):
        depth += (char == "{") - (char == "}")
        minimum = min(minimum, depth)
    return depth, minimum


def focused_boundaries(source: str, region: tuple[int, int]) -> str:
    """Name real C locals and scope obligations; assembly registers are not C variables."""
    lines = source.splitlines()
    first, last = region[0] - 1, region[1]
    # Locate the enclosing definition, excluding sibling locals from the inventory.
    clean = source_mask(source)
    offset = sum(len(line) + 1 for line in lines[:first])
    opening = []
    for match in re.finditer(r"[{}]", clean[:offset]):
        if match.group() == "{":
            opening.append(match.start())
        elif opening:
            opening.pop()
    if not opening:
        return ""
    function_open = opening[0]
    function_line = source.count("\n", 0, function_open)
    end = function_open + 1
    depth = 1
    while end < len(clean) and depth:
        depth += (clean[end] == "{") - (clean[end] == "}")
        end += 1
    last_line = source.count("\n", 0, end) + 1
    scopes, shown, position = [], [], 0
    for number, line in enumerate(lines[:last]):
        # A declaration in a completed sibling block is not available at this edit.
        in_scope = scopes == opening[:len(scopes)] and function_open in scopes
        if number >= function_line and parse_declaration(line) and (in_scope or number >= first):
            shown.append(line.strip())
        for match in re.finditer(r"[{}]", clean[position:position + len(line)]):
            if match.group() == "{":
                scopes.append(position + match.start())
            elif scopes:
                scopes.pop()
        position += len(line) + 1
    shown = list(dict.fromkeys(shown))[:40]
    outside = source_mask("\n".join(lines[function_line:first] + lines[last:last_line]))
    inside = local_declarations(lines[first:last])
    needed = sorted(name for name in inside if re.search(rf"\b{re.escape(name)}\b", outside))
    net, minimum = brace_profile("\n".join(lines[first:last]))
    return ("C variables are the assigned parameters and declarations below, NOT the PPC register names in the diff.\n"
            "Do not introduce r7/r10/etc unless already declared; reuse these locals.\n"
            + ("```c\n" + "\n".join(shown) + "\n```\n" if shown else "")
            + ("Region declarations needed by untouched lines: " + ", ".join(needed) + ". Preserve them.\n" if needed else "")
            + f"Brace boundary: net opens minus closes = {net}; minimum relative depth = {minimum}. Preserve both.\n"
            "Any new C89 local must be declared before statements in its enclosing block.\n\n")


def apply_focused(source: str, symbol: str, region: tuple[int, int], response: str) -> str:
    """The whole function with the region replaced by the model's snippet."""
    blocks = re.findall(r"```(?:c|C)?\s*\n(.*?)```", response, flags=re.DOTALL)
    if not blocks:
        raise ValueError("focused response has no code block")
    snippet = blocks[0].rstrip("\n")
    if re.search(r"#\s*include\b|\b(?:__asm__|__asm|asm)\b|\.inc\b", snippet, flags=re.IGNORECASE):
        raise ValueError("candidate contains forbidden assembly or include")
    if re.search(rf"\b{re.escape(symbol)}\s*\([^;]*\)\s*\{{", snippet):
        # The model returned the whole definition after all: use it as a whole-function answer.
        return extract_function(response, symbol)
    lines = source.splitlines()
    start, end = function_span(source, symbol)
    first_line, last_line = source.count("\n", 0, start), source.count("\n", 0, end) + 1
    outside_text = "\n".join(lines[:region[0] - 1] + lines[region[1]:])
    kept_lines = []
    for line in snippet.splitlines():
        declared = re.match(r"^\s*extern\b[^;(]*?\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;\s*$", line) \
            or re.match(r"^\s*extern\b[^;]*?\b([A-Za-z_]\w*)\s*\([^;]*\)\s*;\s*$", line)
        # An extern the file already declares elsewhere is a duplicate (often with a clashing type).
        if declared and re.search(rf"\b{re.escape(declared.group(1))}\b", outside_text):
            continue
        kept_lines.append(line)
    snippet = "\n".join(kept_lines)
    replaced = "\n".join(lines[region[0] - 1:region[1]])
    if brace_profile(snippet) != brace_profile(replaced):
        raise ValueError("focused edit changes the region's brace boundary; preserve blocks continued by untouched lines")
    function_lines = lines[first_line:last_line]
    fixed, remove = repair_snippet(function_lines, region[0] - 1 - first_line, region[1] - first_line, snippet.splitlines())
    # `remove` holds function-relative indices of declarations the snippet moved into the region.
    drop = {first_line + index for index in remove}
    before = [line for index, line in enumerate(lines[:region[0] - 1]) if index not in drop]
    after = [line for index, line in enumerate(lines[region[1]:], region[1]) if index not in drop]
    edited = "\n".join(before + fixed + after) + "\n"
    start, end = function_span(edited, symbol)
    return edited[start:end].strip() + "\n"


def continuation_source(item: dict[str, Any], source: str) -> str:
    """Carry forward only the target function from a measured improvement."""
    best = item.get("best") or {}
    if best.get("pct") is None or best["pct"] <= float(item.get("base_pct") or 0):
        return source
    current = candidate_function(item, best.get("candidate"))
    if not current:
        return source
    try:
        return splice(source, item["symbol"], current)
    except ValueError:
        return source


def process(state: dict[str, Any], item: dict[str, Any], host: str, model: str, timeout: int, worker: str, feedback: str = "") -> None:
    source_path = ROOT / item.get("owner_source", item["source"])
    if not source_path.is_file():
        # A merge reshaped the units since the last sync; never let one stale task stop the worker.
        item.update(status="stale_source", last_error="owner source no longer exists; run sync")
        item["updated_at"] = timestamp()
        save_state(state)
        return
    source = source_path.read_text(encoding="utf-8", errors="replace")
    if digest(source) != item["source_sha256"]:
        item.update(status="stale_source", last_error="source changed; run sync")
        item["updated_at"] = timestamp()
        save_state(state)
        return
    if item.get("kind") == "cleanup":
        try:
            source = neutralize(source, item["symbol"], unit_cflags(item))
        except ValueError as exc:
            item.update(status="blocked_source_context", last_error=str(exc), updated_at=timestamp())
            save_state(state)
            return
    item["status"] = "running"
    for key in ("last_report", "last_error", "candidate", "activity", "prompt_pct", "investigation",
                "context_model_calls", "context_lookup_rounds"):
        item.pop(key, None)
    item["attempts"] = int(item.get("attempts", 0)) + 1
    item["last_attempt_at"] = timestamp()
    activity(state, item, "Checking source baseline", "Confirming the queued owner source has not changed.")
    attempt = item["attempts"]
    folder = candidates_dir() / item["id"]
    folder.mkdir(parents=True, exist_ok=True)
    if "tried" not in item:
        item["tried"] = seed_tried(item, folder)
    attempt_started = time.monotonic()
    stage_seconds = {"preparation": 0.0, "verification": 0.0}
    COMMAND_SECONDS.clear()
    generation_started = generation_seconds = None
    fresh_exact = False
    LAST_TIMINGS.clear()  # never report the previous attempt's measurements
    try:
        activity(state, item, "Generating objdiff brief", "Reading the target diff and active compiler flags.")
        working = continuation_source(item, source)
        brief, prompt = build_prompt(item, working, worker)
        stage_seconds["preparation"] = time.monotonic() - attempt_started
        fresh_exact = item.get("prompt_pct") == 100.0
        if working != source:
            prompt = (f"## Continuation baseline\n\nThe assigned function source and ALL current assembly differences below\n"
                      f"describe the best retained candidate ({best_pct(item)}%), not the original source\n"
                      f"({item['base_pct']}%). Keep its improvements and change only unresolved differences.\n\n" + prompt)
        blocked = blocked_reason(brief)
        if blocked:
            raise blocked
        if feedback:
            prompt += "\n\n## Previous attempt feedback\n" + feedback
            if working != source:
                prompt += ("\nThe authoritative starting point is the best retained function in Assigned function source.\n"
                           "Historical or failed answers above are diagnostic context; preserve the best function's gains.\n")
        else:
            history = HISTORY_READ_AHEAD.take(history_key(item))
            prompt += history if history is not None else attempt_history(item)
        retry = int(item.pop("retry_round", 0) or 0)
        temperature = RETRY_TEMPERATURES[0] if retry == 0 else \
            RETRY_TEMPERATURES[1 + (retry - 1) % (len(RETRY_TEMPERATURES) - 1)]
        if FIXED_TEMPERATURE is not None:
            temperature = FIXED_TEMPERATURE
        wasted = int(item.get("wasted_streak", 0) or 0) if DIVERSIFY and feedback else 0
        if wasted:
            # The last answer changed nothing new: a sample as cool as that one would likely repeat it.
            temperature = max(temperature, HOT_TEMPERATURES[(wasted - 1) % len(HOT_TEMPERATURES)])
        # Same task and attempt, same sample: a run can be repeated exactly, while retries still differ.
        seed = int(digest(f"{item['id']}:{attempt}:{SEED_SALT}")[:8], 16)
        region = None
        mode = item.pop("retry_mode", None) if feedback else None
        hunk_lines = item.get("focus_hunks") or []
        hunk_texts = item.get("focus_hunk_texts") or []
        start, end = function_span(source, item["symbol"])
        ws, we = function_span(working, item["symbol"])
        current = working[ws:we] if working != source else None
        configured = int(state["settings"].get("ollama_num_predict", DEFAULT_NUM_PREDICT))
        if exceeds_budget(working[ws:we], configured):
            mode = "too_large"
        if fresh_exact:
            mode = None
        elif mode is None and float(item.get("prompt_pct") or 0) >= 99.0:
            mode = "registers" if register_only(item) else "near_exact"
        if current is not None and not hunk_lines:
            best = item.get("best") or {}
            hunk_lines, hunk_texts = best.get("focus_hunks") or [], best.get("focus_hunk_texts") or []
        if mode and mode != "registers" and hunk_lines:
            first = int(item.get("focus_round", 0))
            # Start at this round's hunk; skip hunks whose lines are too spread out to quote.
            for step in range(len(hunk_lines)):
                index = (first + step) % len(hunk_lines)
                region = focus_region(working, item["symbol"], hunk_lines[index])
                if region:
                    item["focus_round"] = first + step
                    hunk = hunk_texts[index] if len(hunk_texts) == len(hunk_lines) else None
                    prompt = focused_prompt(prompt, working, region, mode, current, hunk)
                    break
        if (not fresh_exact and (mode == "registers" or (DIVERSIFY and feedback)) and not region and mode not in ("too_large", "incomplete_response")
                and register_only(item)):
            # Only register numbers differ, so there is no structural hunk to focus on, and whole-function
            # retries came back unchanged or repeated (89% on the bench). Ask for several declaration
            # orders instead, on the best candidate so earlier gains are kept.
            region = declaration_region(working, item["symbol"])
            if region:
                prompt = focused_prompt(prompt, working, region, "registers", current)
        item["last_focused"] = bool(region)
        if not fresh_exact and DIVERSIFY and (wasted or not region):
            # Focused edits already demand several different rewrites; whole-function prompts need the push.
            prompt += strategy_hint(attempt, wasted, current or source[start:end])
        # Reasoning tokens count against the same cap as the answer, so thinking gets its own allowance.
        reasoning = THINK or (THINK is None and REASONING_SEEN)
        num_predict = output_cap(configured, working[ws:we], bool(region), mode) + (REASONING_TOKENS if reasoning else 0)
        (folder / f"attempt-{attempt:03d}.brief.md").write_text(brief, encoding="utf-8", newline="")
        (folder / f"attempt-{attempt:03d}.prompt.md").write_text(prompt, encoding="utf-8", newline="")
        activity(
            state, item, "Rechecking already exact source" if fresh_exact else "Generating candidate with local model",
            "Fresh scratch match is 100%; retaining this function for normal verification and review."
            if fresh_exact else f"Streaming the Ollama response from {host.rstrip('/')}.",
            prompt_chars=len(prompt), response_chars=0, response_chunks=0, num_predict=num_predict,
        )
        if not fresh_exact and len(prompt) > 16000:
            # Long prompts: measure them, and cut them before sending if the answer would not fit after them.
            measured = prompt_tokens(host, prompt)
            if measured and measured[0] + num_predict + 256 > measured[1]:
                smaller = compact_prompt(prompt, measured[0], measured[1], num_predict)
                if smaller:
                    prompt = smaller
                    (folder / f"attempt-{attempt:03d}.prompt.md").write_text(prompt, encoding="utf-8", newline="")
                    activity(state, item, "Prompt cut to fit the model's context", f"{measured[0]} prompt tokens + {num_predict} for the answer exceeded {measured[1]}")
        generation_started = time.monotonic()
        next_items = [other for other in state["items"].values()
                      if other.get("status") == "pending" and other.get("tried")]
        if next_items:
            next_item = max(next_items, key=best_pct)
            key = history_key(next_item)
            if key:
                HISTORY_READ_AHEAD.submit(key, prepare_history, copy.deepcopy(next_item), key)
        if fresh_exact:
            response = f"```c\n{working[ws:we].strip()}\n```"
        else:
            response, prompt = generate_with_context(state, item, working, prompt, folder, attempt,
                                                    host, model, timeout, num_predict, temperature, seed)
        generation_seconds = time.monotonic() - generation_started
        generation_started = None
        (folder / f"attempt-{attempt:03d}.response.md").write_text(response, encoding="utf-8", newline="")
        activity(state, item, "Extracting replacement source", "Rejecting any response that is not a complete definition for this symbol.", response_chars=len(response))
        exits = 0
        if region:
            blocks = re.findall(r"```(?:c|C)?\s*\n(.*?)```", response, flags=re.DOTALL)[:FOCUS_VARIANTS]
            lines = working.splitlines()
            ws, we = function_span(working, item["symbol"])
            after = lines[region[1]:working.count("\n", 0, we)]
            # Only statements after the region can be cut off; a return at the function's end is fine.
            guarded = any(line.strip() not in ("", "}") for line in after)
            proposals, broken = [], []
            for block in blocks:
                if guarded and adds_early_exit(lines[region[0] - 1:region[1]], block):
                    exits += 1
                    continue
                try:
                    proposals.append(apply_focused(working, item["symbol"], region, f"```c\n{block}```"))
                except ValueError as exc:
                    broken.append(str(exc))
            if not proposals and not exits:
                raise ValueError(broken[0] if broken else "focused response has no code block")
        else:
            proposals = [extract_function(response, item["symbol"])]
        start, end = function_span(source, item["symbol"])
        ws, we = function_span(working, item["symbol"])
        same = lambda text: " ".join(text.split())
        tried = item.setdefault("tried", {})
        # Filter before compiling. Whitespace-only edits compile to the same code, and an answer already
        # measured for this source measures the same again.
        fresh, echoes, repeats, refusals, seen = [], 0, [], [], set()
        for proposal in proposals:
            key = digest(same(proposal))
            if not fresh_exact and same(proposal) in (same(source[start:end]), same(working[ws:we])):
                echoes += 1
            elif not fresh_exact and key in tried:
                repeats.append(tried[key])
            elif key not in seen:
                rejected, flags = policy(source[start:end], proposal)
                if rejected:
                    refusals.append(rejected)
                else:
                    seen.add(key)
                    fresh.append((key, proposal, flags))
        if not fresh:
            if refusals:
                reasons = refusals[0]
                item.update(status="policy_rejected", last_error="; ".join(reasons), last_report={"policy_rejected": reasons, "review_flags": []})
                activity(state, item, "Rejected by source policy", "; ".join(reasons), completed_at=timestamp())
                event(state, "policy_rejected", task=item["id"], reasons=reasons)
            elif exits:
                item.update(status="invalid_edit", last_error=(
                    f"{exits} of {exits + echoes + len(repeats)} rewrites ended in a return, goto, break or continue "
                    "that the lines shown did not have, which would skip every statement after them"),
                    last_report={"pct": None, "early_exit": exits})
                activity(state, item, "Rewrite would skip the rest of the function", item["last_error"] + "; not built.",
                         completed_at=timestamp())
                event(state, "invalid_edit", task=item["id"], worker=worker)
            elif repeats:
                earlier = repeats[0]
                score = "did not compile" if earlier.get("pct") is None else f"scored {earlier['pct']}%"
                item.update(status="duplicate", last_error=f"identical to attempt {earlier['attempt']}, which {score}",
                            last_report={"pct": None, "duplicate_of": earlier["attempt"], "compile_error": earlier.get("error")})
                activity(state, item, "Repeated earlier answer", item["last_error"] + "; not rebuilt.", completed_at=timestamp())
                event(state, "duplicate", task=item["id"], worker=worker, attempt=earlier["attempt"])
            else:
                item.update(status="no_change", last_error="model repeated the existing function unchanged")
                activity(state, item, "Unchanged proposal", "No compilation needed; source is identical.", completed_at=timestamp())
                event(state, "no_change", task=item["id"], worker=worker)
            return
        legacy = re.search(r"#\s*pragma\s+(?:optimization_level|optimize_for_size|scheduling|peephole|opt_propagation)\b", source)
        outcomes = []
        for number, (key, proposal, flags) in enumerate(fresh, 1):
            suffix = "" if number == 1 else f"-v{number}"
            candidate = folder / f"attempt-{attempt:03d}{suffix}.c"
            candidate.write_text(splice(source, item["symbol"], proposal), encoding="utf-8", newline="")
            activity(
                state, item, "Compiling ignored candidate",
                f"Building variant {number} of {len(fresh)} for the assigned objdiff unit; tracked source will be restored afterward.",
                candidate=str(candidate.relative_to(ROOT)), review_flags=flags,
            )
            # verify() re-annotates the candidate in place: keep its hunk lines apart from the assigned source's.
            assigned = {name: item.pop(name, None) for name in ("focus_hunks", "focus_hunk_texts")}
            verification_started = time.monotonic()
            measured = verify(item, candidate, worker)
            stage_seconds["verification"] += time.monotonic() - verification_started
            candidate_hunks = item.pop("focus_hunks", None) or []
            candidate_texts = item.pop("focus_hunk_texts", None) or []
            item.update({name: value for name, value in assigned.items() if value is not None})
            if legacy:
                flags.append("owner contains legacy compiler controls; audit active target settings before promotion")
            measured.update(candidate=str(candidate.relative_to(ROOT)), review_flags=flags, variant=number, variants=len(fresh),
                            candidate_sha256=investigation.digest(candidate.read_bytes()))
            measured["obligations"] = obligations.ledger(measured, candidate_sha256=measured["candidate_sha256"])
            tried[key] = {"attempt": attempt, "variant": number, "pct": measured["pct"], "candidate": measured["candidate"]}
            if measured["pct"] is None:
                tried[key]["error"] = compile_error(measured.get("output_tail", ""))
            outcomes.append(measured)
            # Keep the best-scoring answer so later corrections build on it, not on a worse retry.
            if measured["pct"] is not None and measured["pct"] > float((item.get("best") or {}).get("pct") or item["base_pct"]):
                item["best"] = {"pct": measured["pct"], "attempt": attempt, "variant": number, "candidate": measured["candidate"],
                                "diff_feedback": measured.get("diff_feedback", []), "focus_hunks": candidate_hunks,
                                "focus_hunk_texts": candidate_texts, "at": timestamp()}
                item["focus_round"] = 0  # a new best has its own hunk list; start from its first hunk
            if measured["pct"] == 100.0:
                break
        # The attempt reports its best variant; the others stay in `tried` for the history.
        measured = max(outcomes, key=lambda row: -1.0 if row["pct"] is None else row["pct"])
        if len(outcomes) > 1:
            measured["variant_pcts"] = [row["pct"] for row in outcomes]
        item["candidate"] = measured["candidate"]
        item["last_report"] = measured
        if measured["pct"] == 100.0:
            measured["link_gate"] = {"skipped": "full-owner source policy, sibling/data and relocation audit required before link testing"}
            item["status"] = "review_exact"
            activity(state, item, "Exact text candidate awaiting review", "Raw objdiff is 100%; source policy, full-owner and link gates remain pending.", completed_at=timestamp())
            event(state, "review_exact", task=item["id"], symbol=item["symbol"], linked=measured["link_gate"].get("linked", False))
        elif measured["pct"] is None:
            item.update(status="verification_error", last_error=measured["output_tail"][-1200:])
            activity(state, item, "Verification failed", "The candidate could not be measured; inspect the retained report.", completed_at=timestamp())
            event(state, "verification_error", task=item["id"])
        else:
            item["status"] = "non_exact"
            measured["improvement_pp"] = measured["pct"] - item["base_pct"]
            activity(state, item, "Candidate measured non-exact", f"objdiff measured {measured['pct']:.5f}% with {measured['deltas']} instruction deltas.", completed_at=timestamp())
            event(state, "non_exact", task=item["id"], symbol=item["symbol"], pct=measured["pct"])
    except RestorationError as exc:
        item.update(status="restoration_error", last_error=str(exc))
        activity(state, item, "Worker halted", str(exc), completed_at=timestamp())
        raise
    except InterruptedError as exc:
        item.update(status="interrupted", last_error=str(exc))
        activity(state, item, "Worker stopped", str(exc), completed_at=timestamp())
    except IncompleteResponse as exc:
        item.update(status="incomplete_response", last_error=str(exc))
        activity(state, item, "Incomplete model output", str(exc), completed_at=timestamp())
        event(state, "incomplete_response", task=item["id"], worker=worker)
    except TaskBlocked as exc:
        # Nothing was generated: the attempt isn't counted.
        item.update(status=exc.status, last_error=str(exc), attempts=attempt - 1)
        activity(state, item, "Task cannot be solved in C", str(exc), completed_at=timestamp())
        event(state, exc.status, task=item["id"], symbol=item["symbol"])
    except StaleSource as exc:
        item.update(status="stale_source", last_error=str(exc))
        activity(state, item, "Source changed", str(exc), completed_at=timestamp())
        event(state, "stale_source", task=item["id"], symbol=item["symbol"])
    except RepeatedResponse as exc:
        item.update(status="model_loop", last_error=str(exc))
        activity(state, item, "Looping model response stopped", str(exc), completed_at=timestamp())
        event(state, "model_loop", task=item["id"], symbol=item["symbol"])
    except HostUnavailable as exc:
        item.update(status="pending", last_error=str(exc), attempts=attempt - 1)
        activity(state, item, "Model host unreachable", "Requeued without consuming an attempt.", completed_at=timestamp())
        event(state, "host_unavailable", task=item["id"], worker=worker, detail=str(exc))
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
        item.update(status="error", last_error=str(exc)[-1800:])
        activity(state, item, "Runner error", item["last_error"], completed_at=timestamp())
        event(state, "error", task=item["id"], detail=item["last_error"])
    finally:
        if generation_started is not None:
            # The stream ended in an error (cut off, loop, timeout): generation was all this attempt did.
            generation_seconds = time.monotonic() - generation_started
        write_json(folder / f"attempt-{attempt:03d}.report.json", {
            "task": item["id"], "symbol": item["symbol"], "worker": worker, "model": model,
            "source_sha256": item["source_sha256"], "status": item["status"],
            "base_pct": item["base_pct"], "at": timestamp(),
            "report": item.get("last_report"), "error": item.get("last_error"),
            "activity": item.get("activity"),
            "inference_skipped": fresh_exact,
            "investigation_version": investigation.VERSION if INVESTIGATION else None,
            "context_requests_enabled": CONTEXT_REQUESTS and INVESTIGATION,
            "model_calls": item.get("context_model_calls", 0),
            "context_lookup_rounds": item.get("context_lookup_rounds", 0),
            "prompt_evidence_cache": item.get("prompt_evidence_cache"),
            "generation_seconds": round(generation_seconds, 2) if generation_seconds is not None else None,
            "stage_seconds": {**{key: round(value, 3) for key, value in stage_seconds.items()},
                              "generation": round(generation_seconds or 0, 3),
                              "total": round(time.monotonic() - attempt_started, 3)},
            "verification_breakdown_seconds": {key: round(value, 3) for key, value in COMMAND_SECONDS.items()},
            "timings": {key: LAST_TIMINGS.get(key) for key in ("prompt_n", "prompt_per_second", "predicted_n", "predicted_per_second")}
                       if LAST_TIMINGS else None,
        })
        save_state(state)


def candidate_function(item: dict[str, Any], path: str | None) -> str:
    """The task's function as it reads in a retained candidate file, or "" when unavailable."""
    try:
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace") if path else ""
        start, end = function_span(text, item["symbol"])
        return text[start:end].strip()
    except (OSError, ValueError):
        return ""


def compile_error(output: str, limit: int = 500) -> str:
    """The MWCC diagnostic lines from a failed build's output, without the file banners."""
    lines = [line.lstrip("#").rstrip() for line in output.splitlines()
             if line.startswith("#") and not re.match(r"#\s+(?:In|From):|#\s*-+\s*$", line)]
    return "\n".join(line for line in lines if line.strip())[:limit]


def seed_tried(item: dict[str, Any], folder: Path) -> dict[str, dict[str, Any]]:
    """Measured answers retained from before `tried` was recorded, for the same source only."""
    tried = {}
    for path in sorted(folder.glob("attempt-*.report.json")):
        record = read_json(path, {})
        measured = record.get("report") or {}
        if record.get("source_sha256") != item.get("source_sha256") or not measured.get("candidate"):
            continue
        current = candidate_function(item, measured["candidate"])
        if current:
            tried[digest(" ".join(current.split()))] = {
                "attempt": int(path.name.split("-")[1].split(".")[0]), "pct": measured.get("pct"), "candidate": measured["candidate"]}
    return tried


def history_key(item: dict[str, Any]) -> str | None:
    try:
        paths = {ROOT / item.get("owner_source", item["source"])}
        paths.update(ROOT / row["candidate"] for row in (item.get("tried") or {}).values()
                     if row.get("candidate"))
        return digest(json.dumps([item["symbol"], item.get("base_pct"), item.get("best"),
                                  item.get("tried"), [(str(path), hashlib.sha256(path.read_bytes()).hexdigest())
                                                     for path in sorted(paths)]], sort_keys=True))
    except (OSError, KeyError):
        return None


def prepare_history(item: dict[str, Any], key: str) -> str | None:
    history = attempt_history(item)
    return history if history_key(item) == key else None


def attempt_history(item: dict[str, Any], limit: int = 6, max_lines: int = 10) -> str:
    """What each measured answer changed against the assigned source, and what it scored.

    Without it the model re-proposes changes that already failed: retries only ever saw the
    latest answer."""
    tried = sorted((item.get("tried") or {}).values(), key=lambda row: (row["attempt"], row.get("variant", 1)))[-limit:]
    if not tried:
        return ""
    assigned = ""
    try:
        text = (ROOT / item.get("owner_source", item["source"])).read_text(encoding="utf-8", errors="replace")
        start, end = function_span(text, item["symbol"])
        assigned = text[start:end].strip()
    except (OSError, ValueError, KeyError):
        pass
    rows = []
    for row in tried:
        score = "did not compile" if row.get("pct") is None else f"{row['pct']}%"
        change = ""
        current = candidate_function(item, row.get("candidate"))
        if assigned and current:
            lines = [line for line in difflib.unified_diff(assigned.splitlines(), current.splitlines(), lineterm="", n=0)
                     if line[:1] in "+-" and not line.startswith(("+++", "---")) and line[1:].strip()]
            change = "\n".join(lines[:max_lines]) + (f"\n... ({len(lines) - max_lines} more changed lines)" if len(lines) > max_lines else "")
            change = change or "(whitespace only)"
        label = f"Attempt {row['attempt']}" + (f" variant {row['variant']}" if row.get("variant", 1) > 1 else "")
        error = f"\ncompiler: {row['error'].splitlines()[-1].strip()}" if row.get("error") else ""
        rows.append(f"{label}: {score}{error}\n" + (change or "(no line diff available)"))
    return (f"\n## Attempts so far on this function (assigned source: {item['base_pct']}%; best: {best_pct(item)}%)\n"
            "Each entry shows the lines that answer changed against the original queued source "
            "(- removed, + added). Do not repeat a change that scored at or below the best; try a different one.\n\n"
            + "\n\n".join(rows)[:3500] + "\n")


def retry_feedback(item: dict[str, Any]) -> str:
    status = item.get("status")
    if status not in {"no_change", "non_exact", "verification_error", "incomplete_response", "duplicate", "invalid_edit",
                      "error", "policy_rejected", "model_loop"}:
        return ""
    report = item.get("last_report") or {}
    if not report and not item.get("last_error"):
        return ""
    history = attempt_history(item)
    if status in {"error", "policy_rejected"}:
        # An answer the harness could not use: say exactly why, instead of ending the task's visit.
        return (f"Your previous answer could not be used: {str(item.get('last_error', 'unknown error'))[-600:]}. "
                f"Return exactly one complete definition of `{item['symbol']}` in a single ```c block (or, for a focused "
                "edit, only the requested lines), keeping the signature exactly as in \"Assigned function source\": same "
                "return type, name, parameter types and names. No assembly, #include, pragmas or compiler shaping. "
                "Preserve semantics.\n" + history)
    if status == "model_loop":
        return ("Your previous answer started repeating itself and was stopped. Reply with only the ```c block the task "
                "asks for, with no explanation. Preserve semantics and the exact signature; no compiler shaping.\n" + history)
    if status == "no_change" and register_only(item):
        # No structural hunk to point at: every remaining difference is register allocation.
        return ("Your previous answer was the assigned function unchanged, so it cannot match. The instructions already "
                "match the target; only register numbers differ ([register-only] hunks). MWCC assigns saved registers "
                "largely by declaration order and by the order values are first used, so change the order of local "
                "declarations, or of independent statements, or which values are kept in locals, following the register "
                "replay section if it is present. Preserve semantics and the exact signature; no compiler shaping.\n" + history)
    if status == "no_change":
        # Echoing the unchanged function back only primes the model to copy it again.
        return ("Your previous answer was the assigned function unchanged, so it cannot match. "
                "This time change the source: take the first [structural] hunk in \"Real differences to fix\", "
                "find the C statement that produces those instructions, and rewrite that statement (its "
                "types, operand order, loop or branch shape, or which values live in locals) so the "
                "compiler emits the target's instructions. Keep everything else as it is. "
                "Preserve semantics and the exact signature; no compiler shaping.\n" + history)
    if status == "duplicate":
        error = report.get("compile_error")
        focus = ("register allocation using existing locals and independent statements"
                 if register_only(item) else "the first unresolved structural hunk")
        return (f"Your previous answer was {item.get('last_error', 'identical to an earlier answer')}. Repeating it "
                f"cannot change the result. Test a different hypothesis about {focus}. "
                "Preserve semantics and the exact signature; no compiler shaping.\n"
                + (f"That answer failed with:\n{error}\nUse only fields, types and names that the source above "
                   "already uses or declares.\n" if error else "") + history)
    if status == "invalid_edit":
        return (f"Your previous answer was not built: {item.get('last_error', '')}. The lines you rewrite sit in "
                "the middle of the function, and the statements after them must still run. Do not add a return, "
                "goto, break or continue after them. Preserve semantics and the exact signature; no compiler "
                "shaping.\n" + history)
    if status == "incomplete_response":
        # The cut-off text is usually prose reasoning: echoing it back invites more of the same.
        return ("Your previous answer ran out of output tokens before it finished. Reply with only the ```c "
                "block(s) the task asks for: no explanation, analysis or notes before or after the code. "
                "Preserve semantics and the exact signature; no compiler shaping.\n" + history)
    best = item.get("best") or {}
    score, last = best_pct(item), report.get("pct")
    if status == "non_exact" and last is not None and best.get("attempt") != item["attempts"]:
        # The answer set no new best, so the harness discards it and builds on the best version. Critiquing the
        # discarded answer's diff while editing another version gave the model two conflicting targets.
        verb = "lower than" if last < score - 1e-9 else "no better than"
        if best.get("pct") is not None:
            origin = f"your attempt {best['attempt']}"
            current = "" if best.get("focus_hunks") else candidate_function(item, best.get("candidate"))
            resume = ("The harness continues from that version. Its remaining differences (\"produced by\" quotes its lines):\n"
                      + "\n\n".join(best.get("diff_feedback", []))[:3000] + "\n"
                      + (f"That version of the function:\n```c\n{current}\n```\n" if current else ""))
        else:
            origin = "the assigned source"
            resume = ("The harness continues from \"Assigned function source\"; its remaining differences are the ones "
                      "in \"Real differences to fix\" above.\n")
        return (f"Your previous answer scored {last}%, {verb} the best so far ({score}%, from {origin}), so it was "
                "discarded. Preserve semantics and the exact signature; no compiler shaping.\n" + resume + history)
    # A new best or a compile failure: correct that answer itself, shown as the whole function it produced.
    response = candidates_dir() / item["id"] / f"attempt-{item['attempts']:03d}.response.md"
    previous = candidate_function(item, item.get("candidate"))
    if not previous:
        text = response.read_text(encoding="utf-8") if response.exists() else ""
        blocks = re.findall(r"```(?:c|C)?\s*\n(.*?)```", text, flags=re.DOTALL)
        previous = blocks[0] if blocks else text
    if status == "verification_error":
        error = compile_error(report.get("output_tail") or str(item.get("last_error", ""))) \
            or str(item.get("last_error", ""))[-1000:]
        detail = (f"It did not compile:\n{error}\nUse only fields, types and names that the source above already "
                  "uses or declares.\n")
    else:
        detail = ("Remaining differences of your previous answer (\"produced by\" quotes your own lines):\n"
                  + "\n\n".join(report.get("diff_feedback", []))[:3000] + "\n")
    return ("Correct your previous attempt. Preserve semantics and the exact signature. "
            "Do not repeat unchanged code or use compiler shaping.\n"
            f"Previous result: {status}; baseline: {item['base_pct']}%; candidate: {last}.\n"
            + detail + "Previous answer:\n```c\n" + previous[:5000].strip() + "\n```\n" + history)


def pause(seconds: float) -> None:
    """Sleep in short steps so SIGTERM still stops a backing-off worker promptly."""
    deadline = time.monotonic() + seconds
    while not STOP_REQUESTED and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def worker_slug(worker: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", worker.strip()).strip("-")
    return clean or "worker"


PRIORITY_FILE = STATE_DIR / "priority.json"


def best_pct(item: dict[str, Any]) -> float:
    return max(float(item.get("base_pct") or 0), float((item.get("best") or {}).get("pct") or 0))


def permute_eligible(item: dict[str, Any]) -> bool:
    """Near-exact tasks not yet searched for this exact source."""
    return (item.get("kind") != "cleanup" and item.get("status") != "review_exact" and best_pct(item) >= 97.0
            and (item.get("permuted") or {}).get("sha") != item.get("source_sha256"))


def permute_search(state: dict[str, Any], item: dict[str, Any], worker: str) -> float | None:
    """Try reorderings of the function's declaration block; keep the best as a candidate.

    Runs under the build lock because each try rewrites the owner source in place (always
    restored). Only declarations move, so semantics are unchanged; the result still goes
    through the normal promotion gates (policy, full report, SHA1).
    """
    owner = ROOT / item.get("owner_source", item["source"])
    record = {"sha": item.get("source_sha256"), "at": timestamp(), "tried": 0}
    with COORDINATOR.build(worker, f"Declaration-order search: {item['symbol']}"):
        original = owner.read_bytes()
        text = original.decode("utf-8", errors="replace")
        try:
            start, end = function_span(text, item["symbol"])
        except ValueError:
            return None
        function = text[start:end]
        block = permute.declaration_block(function)
        if digest(text) != item.get("source_sha256") or not block:
            item["permuted"] = {**record, "skipped": "no declaration block" if not block else "source changed"}
            save_state(state)
            return None
        begin, finish, lines = block
        activity(state, item, "Declaration-order search", f"Trying up to {PERMUTE_CAP} orders of {len(lines)} declarations.")
        best_text, deadline = None, time.monotonic() + PERMUTE_SECONDS
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base = scratch_score(item, Path(temporary))
                if base is None:
                    return None
                top = base
                for order in permute.orderings(lines, PERMUTE_CAP):
                    if time.monotonic() > deadline or STOP_REQUESTED:
                        break
                    candidate = text[:start] + function[:begin] + "".join(order) + function[finish:] + text[end:]
                    owner.write_text(candidate, encoding="utf-8", newline="")
                    pct = scratch_score(item, Path(temporary))
                    record["tried"] += 1
                    if pct is not None and pct > top + 1e-9:
                        top, best_text = pct, candidate
                        if pct >= 100.0:
                            break
        finally:
            owner.write_bytes(original)
    record.update(base=base, best=top)
    item["permuted"] = record
    if best_text is not None and top > best_pct(item) + 1e-9:
        folder = candidates_dir() / item["id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"permute-{record['tried']:04d}.c"
        path.write_text(best_text, encoding="utf-8", newline="")
        item["best"] = {"pct": top, "attempt": "declaration-order", "candidate": str(path.relative_to(ROOT)),
                        "diff_feedback": [], "at": timestamp()}
        if top >= 100.0:
            item["status"] = "review_exact"
        event(state, "permute_improved", task=item["id"], symbol=item["symbol"], worker=worker,
              base=base, pct=top, tried=record["tried"])
    item["updated_at"] = timestamp()
    save_state(state)
    return top


def rewrite_eligible(item: dict[str, Any]) -> bool:
    """Near-exact tasks whose current source has not had the rewrite search yet."""
    return (item.get("kind") != "cleanup" and item.get("status") != "review_exact" and best_pct(item) >= 97.0
            and (item.get("rewritten") or {}).get("sha") != item.get("source_sha256"))


def _score_text(item: dict[str, Any], owner: Path, original: bytes, text: str, directory: Path) -> float | None:
    """Scratch-score `text` as the owner source; the owner is restored before returning."""
    try:
        owner.write_text(text, encoding="utf-8", newline="")
        return scratch_score(item, directory)
    finally:
        owner.write_bytes(original)


def rewrite_search(item: dict[str, Any], worker: str, seconds: float, sideways: int = REWRITE_SIDEWAYS,
                   seed: int = 0) -> dict[str, Any]:
    """Hill-climb over policy-safe rewrites of one function (see local_campaign_rewrite).

    Each round scores every single rewrite of the current best form and moves to the best
    one that raises the score. When none does, it takes an equal-scoring rewrite (up to
    `sideways` in a row) so two-step fixes stay reachable. The build lock is held for one round
    at a time and the owner is compared before each round, so a concurrent change aborts
    the search instead of being overwritten.
    """
    import random

    owner = ROOT / item.get("owner_source", item["source"])
    original = owner.read_bytes()
    text = original.decode("utf-8", errors="replace")
    start, end = function_span(text, item["symbol"])
    base_function = current = best_function = text[start:end]
    rng, seen = random.Random(seed), {digest(current)}
    result: dict[str, Any] = {"symbol": item["symbol"], "tried": 0, "steps": [], "at": timestamp()}
    deadline, streak = time.monotonic() + seconds, 0
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        with COORDINATOR.build(worker, f"Rewrite search: {item['symbol']}"):
            base = _score_text(item, owner, original, text, directory)
        result["base"] = top = base
        if base is None:
            result["skipped"] = "the current source does not compile or score"
            return result
        steps: list[str] = []
        while time.monotonic() < deadline and not STOP_REQUESTED and top < 100.0:
            options = rw.rewrites(current)
            rng.shuffle(options)
            scored = []
            with COORDINATOR.build(worker, f"Rewrite search: {item['symbol']}"):
                if owner.read_bytes() != original:
                    result["aborted"] = "owner source changed during the search"
                    break
                for option in options:
                    if time.monotonic() > deadline or STOP_REQUESTED:
                        break
                    candidate = option.apply(current)
                    key = digest(candidate)
                    if key in seen:
                        continue
                    seen.add(key)
                    pct = _score_text(item, owner, original, text[:start] + candidate + text[end:], directory)
                    result["tried"] += 1
                    if pct is not None:
                        scored.append((pct, option.label, candidate))
                        if pct >= 100.0:
                            break
            if not scored:
                break
            pct, label, candidate = max(scored, key=lambda row: row[0])
            if pct > top + 1e-9:
                top, current, best_function, streak = pct, candidate, candidate, 0
                steps.append(label)
                result["steps"] = list(steps)
                continue
            level = [row for row in scored if abs(row[0] - top) < 1e-9]
            if not level or streak >= sideways:
                break
            _, label, current = rng.choice(level)
            steps.append(label + " (sideways)")
            streak += 1
    best_steps = [step.removesuffix(" (sideways)") for step in result["steps"]]
    if best_function != base_function and len(best_steps) > 1 and not result.get("aborted"):
        # Sideways steps can leave rewrites that add nothing. Drop whole steps (never parts of
        # one: a loop rewrite split in half changes meaning) by replaying the remaining steps
        # through the rewrite generator, so every candidate is still built only from safe rewrites.
        kept = list(best_steps)
        for step in list(best_steps):
            trial = [s for s in kept if s != step]
            candidate = replay_rewrites(base_function, trial)
            if candidate is None:
                continue
            with COORDINATOR.build(worker, f"Rewrite minimise: {item['symbol']}"):
                if owner.read_bytes() != original:
                    break
                with tempfile.TemporaryDirectory() as temporary:
                    pct = _score_text(item, owner, original, text[:start] + candidate + text[end:], Path(temporary))
            if pct is not None and pct >= top - 1e-9:
                kept, best_function = trial, candidate
        result["steps"] = kept
    result.update(best=top, improved=best_function != base_function and top > (base or 0) + 1e-9,
                  function=best_function, text=text[:start] + best_function + text[end:],
                  flags=policy(base_function, best_function)[1] if best_function != base_function else [])
    return result


def replay_rewrites(function_text: str, labels: list[str]) -> str | None:
    """Apply rewrites by label, in order, each found afresh in the current text; None if one is gone."""
    current = function_text
    for label in labels:
        match = next((option for option in rw.rewrites(current) if option.label == label), None)
        if match is None:
            return None
        current = match.apply(current)
    return current


def run_rewrite_search(state: dict[str, Any], item: dict[str, Any], worker: str) -> float | None:
    """Runner hook: search, then keep an improvement as the task's best candidate."""
    activity(state, item, "Rewrite search", f"Trying policy-safe rewrites for up to {REWRITE_SECONDS}s.")
    try:
        result = rewrite_search(item, worker, REWRITE_SECONDS)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        item["rewritten"] = {"sha": item.get("source_sha256"), "at": timestamp(), "error": str(exc)[-400:]}
        save_state(state)
        return None
    item["rewritten"] = {"sha": item.get("source_sha256"), "at": result["at"], "tried": result["tried"],
                         "base": result.get("base"), "best": result.get("best"), "steps": result["steps"],
                         **({"aborted": result["aborted"]} if "aborted" in result else {})}
    if result.get("improved") and result["best"] > best_pct(item) + 1e-9:
        folder = candidates_dir() / item["id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rewrite-{result['tried']:04d}.c"
        path.write_text(result["text"], encoding="utf-8", newline="")
        item["best"] = {"pct": result["best"], "attempt": "rewrite-search", "candidate": str(path.relative_to(ROOT)),
                        "diff_feedback": [], "steps": result["steps"], "review_flags": result["flags"], "at": timestamp()}
        if result["best"] >= 100.0:
            item["status"] = "review_exact"
        event(state, "rewrite_improved", task=item["id"], symbol=item["symbol"], worker=worker,
              base=result["base"], pct=result["best"], tried=result["tried"])
    item["updated_at"] = timestamp()
    save_state(state)
    return result.get("best")


def locate_symbol(symbol: str) -> dict[str, Any]:
    """A minimal task record (unit, source, owner) for any function in the report."""
    report = read_json(build_dir() / "report.json", {})
    for unit in report.get("units", []):
        if not any(fn.get("name") == symbol for fn in unit.get("functions", [])):
            continue
        source = (unit.get("metadata") or {}).get("source_path")
        if not source or not (ROOT / source).is_file():
            continue
        text = (ROOT / source).read_text(encoding="utf-8", errors="replace")
        owner = source
        try:
            function_span(text, symbol)
        except ValueError:
            forwarded = owner_source(source)
            if forwarded != source and _defines(ROOT / forwarded, symbol):
                return {"id": f"rewrite-{symbol}", "symbol": symbol, "unit": unit["name"],
                        "source": source, "owner_source": forwarded}
            names = re.findall(r'#\s*include\s*"([^"]+\.c)"', text)
            # Wrappers include either next to themselves or from the repository root.
            included = [base / name for name in names for base in (ROOT / Path(source).parent, ROOT)]
            owner = next((str(path.resolve().relative_to(ROOT)) for path in included if path.is_file()
                          and _defines(path, symbol)), None)
            if owner is None:
                continue
        return {"id": f"rewrite-{symbol}", "symbol": symbol, "unit": unit["name"], "source": source, "owner_source": owner}
    raise ValueError(f"{symbol}: no unit in report.json with a C definition of it")


def explain_registers(item: dict[str, Any], worker: str = MODEL_WORKER) -> str | None:
    """Replay the function's register allocation and say which values retail colours differently.

    None when the mwcc-debugger tools are not installed or the replay fails. The build lock is
    held because a worker's verification rewrites owner sources in place."""
    if not colouring.available():
        return None
    with COORDINATOR.build(worker, f"Register replay: {item['symbol']}"):
        return explain_registers_unlocked(item)


def explain_registers_unlocked(item: dict[str, Any]) -> str | None:
    """explain_registers for callers that already hold the build lock."""
    if not colouring.available():
        return None
    compiled = Path(item["source"]).with_suffix(".o").name
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as replay:
        rows = _objdiff_rows(item, Path(temporary))
        try:
            colouring.dump(item["source"], item["symbol"], colouring.REPLAY_VERSIONS[0], Path(replay))
            is_faithful = colouring.faithful(Path(replay), item["source"], item["symbol"], Path(temporary) / compiled)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        owner = (ROOT / item.get("owner_source", item["source"])).read_text(encoding="utf-8", errors="replace")
        if not rows or not colouring.assignments(Path(replay)):
            return None
        return colouring.explain(Path(replay), rows, owner, is_faithful)


def register_replay_section(item: dict[str, Any], hunks: list[str]) -> str:
    """Prompt section with the replayed allocation, for tasks whose remaining diff is mostly registers.

    Cached per source version: the replay takes about 20 seconds."""
    if item.get("kind") == "cleanup" or not colouring.available():
        return ""
    registers = sum(hunk.startswith("[register-only]") for hunk in hunks)
    if not registers or (item.get("structural_hunks", 0) > registers and best_pct(item) < 97.0):
        return ""
    cached = item.get("register_replay") or {}
    if cached.get("sha") != item.get("source_sha256"):
        cached = {"sha": item.get("source_sha256"), "text": explain_registers_unlocked(item) or ""}
        item["register_replay"] = cached
    if not cached["text"] or cached["text"].startswith("No register"):
        return ""
    return ("## Register allocation, replayed\n\n" + cached["text"] + "\n\nA register can only change through "
            "natural source: where a variable is declared, first set or last used; statement order; loop form. "
            "Never add a variable, copy or helper only to move a register.\n")


def _defines(path: Path, symbol: str) -> bool:
    try:
        function_span(path.read_text(encoding="utf-8", errors="replace"), symbol)
        return True
    except ValueError:
        return False


def register_only(item: dict[str, Any]) -> bool:
    """True once a prompt has shown that only register-allocation differences remain."""
    return item.get("structural_hunks") == 0


def recycle_attempted(state: dict[str, Any], max_function_bytes: int = 0, batch: int = 40, min_pct: float = 0) -> int:
    """Re-queue the oldest finished attempts that predate the latest harness improvement.

    Lets a worker keep cycling through the whole unmatched corpus instead of stopping once
    every task has had one pass. Rejected candidates, source-less tasks and claimed owners stay out.
    """
    marks = [row["at"] for row in state.get("events", []) if row.get("kind") in ("harness_improved", "model_tuned")]
    since = max(marks) if marks else ""
    claims = set(COORDINATOR.snapshot().get("claims", {}))
    retryable = {"no_change", "non_exact", "incomplete_response", "verification_error", "error", "interrupted", "model_loop", "duplicate", "invalid_edit"}
    candidates = sorted(
        (item for item in state["items"].values()
         if item.get("status") in retryable and not item.get("promotion_rejected")
         and (item.get("last_attempt_at") or "") < since
         and (not max_function_bytes or item["size"] <= max_function_bytes)
         and float(item.get("base_pct") or 0) >= min_pct
         and item.get("owner_source", item["source"]) not in claims),
        key=lambda item: item.get("last_attempt_at") or "")[:batch]
    for item in candidates:
        item.update(status="pending", updated_at=timestamp())
    if candidates:
        event(state, "recycled", tasks=len(candidates), detail=f"re-queued attempts older than {since[:19]}")
        save_state(state)
    return len(candidates)


def local_priority(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The explicit priority list with each target's live status, for the dashboard."""
    spec = read_json(PRIORITY_FILE, {})
    by_symbol = {item["symbol"]: item for item in items}
    rows = []
    for symbol in spec.get("symbols", []):
        item = by_symbol.get(symbol, {})
        best = (item.get("best") or {}).get("pct")
        rows.append({"symbol": symbol, "status": item.get("status", "not queued"), "base_pct": item.get("base_pct"),
                     "best_pct": best, "worker": item.get("worker"), "attempts": item.get("attempts", 0),
                     "promoted_pct": item.get("promoted_pct")})
    promoted = [item for item in items if item.get("promoted_pct")]
    return {"why": spec.get("why", ""), "at": spec.get("at"), "rows": rows,
            "promoted": sorted(({"symbol": item["symbol"], "pct": item["promoted_pct"]} for item in promoted),
                               key=lambda row: row["symbol"])}


def focus_rank() -> dict[str, int]:
    """The explicit priority list's order: symbol -> position."""
    manual = read_json(PRIORITY_FILE, {}).get("symbols", [])
    return {symbol: index for index, symbol in enumerate(manual)}


def runner_lock_file(worker: str) -> Path:
    return STATE_DIR / "runners" / f"{worker_slug(worker)}.lock"


def acquire_lock(worker: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = runner_lock_file(worker)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8", newline="")
    try:
        portable.flock(handle, portable.LOCK_EX | portable.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"runner already active for {worker}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    RUNNER_LOCKS[worker_slug(worker)] = handle


def release_lock(worker: str) -> None:
    handle = RUNNER_LOCKS.pop(worker_slug(worker), None)
    if handle:
        handle.close()


def register_worker(state: dict[str, Any], worker: str, host: str, model: str, num_predict: int) -> None:
    state.setdefault("workers", {})[worker] = {
        "worker": worker,
        "ollama_host": host.rstrip("/"),
        "ollama_model": model,
        "ollama_num_predict": num_predict,
        "ollama_num_ctx": NUM_CTX,
        "think": {None: "auto", True: "on", False: "off"}[THINK],
        **RUN_OPTIONS,
        "pid": os.getpid(),
        "updated_at": timestamp(),
    }
    save_state(state)


def note_waste(item: dict[str, Any]) -> None:
    """Count unchanged or repeated answers in a row; DIVERSIFY escalates on the count."""
    item["wasted_streak"] = int(item.get("wasted_streak", 0) or 0) + 1 if item.get("status") in WASTED else 0


def work_task(state: dict[str, Any], item: dict[str, Any], host: str, model: str, timeout: int, worker: str,
              num_predict: int) -> None:
    """One visit to a claimed task: the first attempt, correction rounds until RETRIES or STALL_ROUNDS
    without a new best, then the optional permute and rewrite searches. Shared by `run` and the bench.
    Under TASK_SECONDS the rounds run until that much of the task's wall time is spent instead."""
    started = time.monotonic()
    item["wasted_streak"] = 0
    process(state, item, host, model, timeout, worker)
    note_waste(item)
    best_seen, stalled = best_pct(item), 0
    # Whole-function retries that echo the input or leave the score flat rarely
    # recover, so once either happens the rest of the task uses focused edits.
    improved, sticky = True, False
    for retry in range(BUDGET_ATTEMPT_CAP - 1 if TASK_SECONDS else RETRIES):
        if TASK_SECONDS and time.monotonic() - started >= TASK_SECONDS:
            break
        if BREATHER_SECONDS > 0 and breather_overdue():
            # A long task shouldn't hold up queued merges and refreshes: run them between attempts.
            take_breather(worker, "", "", num_predict, mid_task=True)
        feedback = retry_feedback(item)
        if not feedback or STOP_REQUESTED:
            break
        if item.get("wasted_streak", 0) >= 2:
            event(state, "duplicate_budget_exhausted", task=item["id"], worker=worker,
                  detail="Two consecutive unchanged or repeated answers; moving to another target")
            save_state(state)
            break
        if EARLY_STOP and not DIVERSIFY and item["status"] == "no_change" and register_only(item):
            # Nothing structural to fix and the model saw nothing to change. Under DIVERSIFY register-only
            # tasks get declaration-order edits instead, so they keep going until the stall limit.
            break
        if item.pop("last_focused", False) and not improved:
            # That hunk did not give way: try the next one.
            item["focus_round"] = int(item.get("focus_round", 0)) + 1
        item["retry_round"] = retry + 1
        if item["status"] == "no_change":
            # A whole-function prompt produced a copy; ask for the hunk's lines only.
            item["retry_mode"], sticky = "no_change", True
        elif item["status"] == "incomplete_response":
            # Double the cap for the retry, never below the worker's own setting, and
            # ask for the hunk's lines only so the answer fits either way.
            state["settings"]["ollama_num_predict"] = max(num_predict, min(16384, num_predict * 2))
            item["retry_mode"] = "incomplete_response"
        elif sticky or (item["status"] in {"non_exact", "duplicate", "invalid_edit"} and not improved):
            item["retry_mode"], sticky = "plateau", True
        process(state, item, host, model, timeout, worker, feedback)
        note_waste(item)
        improved = best_pct(item) > best_seen + 1e-9
        if improved:
            best_seen, stalled = best_pct(item), 0
        else:
            stalled += 1
            if EARLY_STOP and stalled >= STALL_ROUNDS:
                break
    if PERMUTE and not STOP_REQUESTED and permute_eligible(item):
        permute_search(state, item, worker)
    if REWRITE and not STOP_REQUESTED and rewrite_eligible(item):
        run_rewrite_search(state, item, worker)
    if PROMOTE and not STOP_REQUESTED:
        promote_best(state, item, worker)


class _HeavyLock:
    """The optional machine-wide lock (DECOMP_HEAVY_LOCK); a no-op when unset."""
    def __enter__(self):
        self.handle = open(HEAVY_LOCK, "a+", encoding="utf-8", newline="") if HEAVY_LOCK else None
        if self.handle:
            portable.flock(self.handle, portable.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.handle:
            portable.flock(self.handle, portable.LOCK_UN)
            self.handle.close()


def report_scores(report: dict[str, Any]) -> dict[tuple[str, str], float]:
    return {(unit["name"], function["name"]): float(function.get("fuzzy_match_percent") or 0)
            for unit in report.get("units", []) for function in unit.get("functions", [])}


def scores_by_name(scores: dict[tuple[str, str], float]) -> dict[str, float]:
    """Each function's best score across units, so a function that moved to another unit is not a loss."""
    best: dict[str, float] = {}
    for (_, name), score in scores.items():
        best[name] = max(score, best.get(name, -1.0))
    return best


def rebuild_report() -> tuple[bool, str]:
    result = checked_command(["ninja", "all_source", ninja_target(build_relative("report.json"))], timeout=3600)
    return result.returncode == 0, (result.stdout + result.stderr)[-1500:]


def promote_best(state: dict[str, Any], item: dict[str, Any], worker: str) -> bool:
    """Write the task's best candidate into its owner source and commit it, if it beats the source.

    Guarded three ways: the owner must be unchanged since the candidate was measured; the rebuilt report must
    reproduce the candidate's score; and no function anywhere in the report may lose score (the candidate
    replaces the whole owner file, which other units may include). Anything else reverts the file. Linking a
    unit that becomes complete stays a manual step: it needs a configure change and the full SHA-1 build."""
    best = item.get("best") or {}
    pct, base = best.get("pct"), float(item.get("base_pct") or 0)
    if pct is None or pct <= base + 1e-9 or not best.get("candidate"):
        return False
    owner = ROOT / item.get("owner_source", item["source"])
    candidate = ROOT / best["candidate"]
    if not owner.is_file() or not candidate.is_file():
        return False
    relative = str(owner.relative_to(ROOT))
    activity(state, item, "Promoting the best candidate", f"{base:.4f}% -> {pct:.4f}%: rebuilding the report to check nothing regresses.")
    with _HeavyLock(), COORDINATOR.build(worker, f"Promote: {item['symbol']}"):
        original = owner.read_text(encoding="utf-8", errors="replace")
        old_digest = digest(original)
        if old_digest != item["source_sha256"]:
            event(state, "promotion_skipped", task=item["id"], symbol=item["symbol"], reason="owner source changed")
            return False
        # Measure the baseline from a fresh build: the report on disk can predate a landing that renamed or
        # moved units, and comparing against it blamed the candidate for those moves.
        fresh, output = rebuild_report()
        if not fresh:
            event(state, "promotion_skipped", task=item["id"], symbol=item["symbol"],
                  reason="baseline report build failed: " + output[-300:])
            return False
        before = scores_by_name(report_scores(read_json(REPORT_FILE, {})))
        replacement = candidate.read_text(encoding="utf-8", errors="replace")
        owner.write_text(replacement, encoding="utf-8", newline="")
        built, output = rebuild_report()
        after = report_scores(read_json(REPORT_FILE, {})) if built else {}
        target = after.get((item.get("unit"), item["symbol"]))
        after_by_name = scores_by_name(after)
        worse = [f"{name} {old:.4f}->{after_by_name.get(name, -1):.4f}" for name, old in before.items()
                 if after_by_name.get(name, -1.0) < old - 1e-6]
        reason = ("report build failed: " + output[-300:]) if not built else \
                 (f"{len(worse)} function(s) lost score: {'; '.join(worse[:5])}") if worse else \
                 (f"report gives {target} for the candidate's {pct}") if target is None or target < pct - 0.01 else ""
        committed = False
        if not reason:
            message = (f"Promote {item['symbol']} {base:.2f}% -> {target:.2f}% (local model, {worker})\n\n"
                       f"Campaign worker {worker}, attempt {best.get('attempt')} (candidate {best['candidate']}). "
                       "The rebuilt report shows no function anywhere lost score."
                       + (" Exact: link its unit after review (configure change and full SHA-1 build)." if target >= 100 else ""))
            added = subprocess.run(["git", "-C", str(ROOT), "add", "--", relative], capture_output=True, text=True)
            commit = subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", message, "--", relative],
                                    capture_output=True, text=True)
            committed = added.returncode == 0 and commit.returncode == 0
            if not committed:
                reason = "git commit failed: " + (commit.stderr or added.stderr)[-300:]
        if reason:
            owner.write_text(original, encoding="utf-8", newline="")
            if built:
                rebuild_report()
            item["promotion_failed"] = {"at": timestamp(), "pct": pct, "reason": reason}
            event(state, "promotion_failed", task=item["id"], symbol=item["symbol"], reason=reason)
            save_state(state)
            return False
    new_digest = digest(replacement)
    owner_key = item.get("owner_source", item["source"])
    for other in state["items"].values():
        # Every task in the same owner file now measures against the promoted text.
        if other.get("owner_source", other.get("source")) == owner_key and other.get("source_sha256") == old_digest:
            other["source_sha256"] = new_digest
            score = after.get((other.get("unit"), other["symbol"]))
            if score is not None:
                other["base_pct"] = score
    item.update(promoted_pct=target, base_pct=target, best={})
    for key in ("focus_hunks", "focus_hunk_texts", "focus_round"):
        item.pop(key, None)
    event(state, "promoted", task=item["id"], symbol=item["symbol"], pct=target, worker=worker)
    save_state(state)
    return True


def breather_file() -> Path:
    return STATE_DIR / "breather_queue.json"


class _BreatherLock:
    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = (STATE_DIR / "breather.lock").open("a+", encoding="utf-8", newline="")
        portable.flock(self.handle, portable.LOCK_EX)
        return self

    def __exit__(self, *exc):
        portable.flock(self.handle, portable.LOCK_UN)
        self.handle.close()


def breather_add(kind: str, command: list[str] | None = None, description: str = "", requested_by: str = "") -> dict[str, Any]:
    """Queue a job for the next worker breather. `sync` runs the queue sync inside the worker (a separate sync
    can't take the fleet lock while a runner holds it); `command` runs a shell command from the decomp root.
    An identical pending job is not queued twice."""
    with _BreatherLock():
        queue = read_json(breather_file(), {"jobs": []})
        for job in queue["jobs"]:
            if job["status"] == "pending" and job["kind"] == kind and job.get("command") == command:
                return job
        job = {"id": digest(f"{timestamp()}:{kind}:{command}:{len(queue['jobs'])}")[:10], "kind": kind,
               "command": command, "description": description or (" ".join(command) if command else kind),
               "requested_by": requested_by, "created_at": timestamp(), "status": "pending"}
        queue["jobs"].append(job)
        queue["jobs"] = [row for row in queue["jobs"] if row["status"] in {"pending", "running"}] + \
                        [row for row in queue["jobs"] if row["status"] not in {"pending", "running"}][-20:]
        write_json(breather_file(), queue)
        return job


def _breather_update(job_id: str, **changes: Any) -> None:
    with _BreatherLock():
        queue = read_json(breather_file(), {"jobs": []})
        for job in queue["jobs"]:
            if job["id"] == job_id:
                job.update(changes)
        write_json(breather_file(), queue)


BREATHER_MAX_WAIT = 600.0  # seconds a queued command job may wait before the worker runs it between attempts


def breather_overdue() -> bool:
    """A command job has waited longer than BREATHER_MAX_WAIT (a long task can run many attempts)."""
    queue = read_json(breather_file(), {"jobs": []})
    now = datetime.now(UTC)
    return any(job["status"] == "pending" and job["kind"] == "command"
               and (now - datetime.fromisoformat(job["created_at"])).total_seconds() > BREATHER_MAX_WAIT
               for job in queue.get("jobs", []))


def take_breather(worker: str, host: str, model: str, num_predict: int, *, mid_task: bool = False) -> None:
    """Run every queued job, then pause holding no claim or build lock. The run loop reloads state after this,
    so a sync here is picked up before the next task. mid_task: between two attempts of a long task, run only
    command jobs and don't pause; a sync would be overwritten by the task's in-memory state, so it waits."""
    ran = 0
    while not STOP_REQUESTED:
        with _BreatherLock():
            queue = read_json(breather_file(), {"jobs": []})
            job = next((row for row in queue["jobs"] if row["status"] == "pending"
                        and (not mid_task or row["kind"] == "command")), None)
            if job is None:
                break
            job.update(status="running", started_at=timestamp(), worker=worker)
            write_json(breather_file(), queue)
        try:
            if job["kind"] == "sync":
                state = load_state(host, model, num_predict, update_settings=False)
                with COORDINATOR.build(worker, "Queue sync (breather)"):
                    result = sync(state)
                _breather_update(job["id"], status="done", finished_at=timestamp(), output_tail=json.dumps(result))
            else:
                done = checked_command(job["command"], timeout=int(job.get("timeout") or 3600))
                _breather_update(job["id"], status="done" if done.returncode == 0 else "failed", finished_at=timestamp(),
                                 returncode=done.returncode, output_tail=(done.stdout + done.stderr)[-1500:])
        except Exception as error:  # a failed job never stops the worker
            _breather_update(job["id"], status="failed", finished_at=timestamp(), output_tail=f"{type(error).__name__}: {error}")
        ran += 1
    with _BreatherLock():
        queue = read_json(breather_file(), {"jobs": []})
        queue["last_breather"] = {"worker": worker, "at": timestamp(), "jobs": ran,
                                  "seconds": 0 if mid_task else BREATHER_SECONDS, "mid_task": mid_task}
        write_json(breather_file(), queue)
    if not mid_task:
        pause(BREATHER_SECONDS)


def run(state: dict[str, Any], host: str, model: str, limit: int, timeout: int, delay: float, worker: str, num_predict: int, max_function_bytes: int = 0, min_pct: float = 0) -> int:
    acquire_lock(worker)
    try:
        register_worker(state, worker, host, model, num_predict)
        recovered = 0
        live_claims = COORDINATOR.snapshot().get("claims", {})
        claimed_sources = set(live_claims)
        for item in state["items"].values():
            if item.get("status") == "running" and item.get("owner_source", item["source"]) not in claimed_sources:
                item.update(status="pending", last_error="recovered after interrupted runner")
                item["updated_at"] = timestamp()
                recovered += 1
        if recovered:
            event(state, "runner_recovered", tasks=recovered)
            save_state(state)
        processed, backoff, waiting = 0, 0, None
        while not STOP_REQUESTED and (not limit or processed < limit):
            if (STATE_DIR / "halt.json").exists():
                raise RestorationError("fleet halted; inspect halt.json and rebuild the baseline before resuming")
            state = load_state(host, model, num_predict, update_settings=False)
            register_worker(state, worker, host, model, num_predict)
            # The explicit priority list outranks a worker's size/closeness filters: those
            # targets were chosen by hand and are worth the longer runs.
            prioritised = set(read_json(PRIORITY_FILE, {}).get("symbols", []))
            pending = [item for item in state["items"].values() if item.get("status") == "pending"
                       and (item["symbol"] in prioritised
                            or ((not max_function_bytes or item["size"] <= max_function_bytes)
                                and float(item.get("base_pct") or 0) >= min_pct))]
            if not pending:
                if RECYCLE and recycle_attempted(state, max_function_bytes, min_pct=min_pct):
                    continue
                break
            focus = focus_rank()
            if LOWEST_FIRST:
                # Flipped order: the furthest-from-exact functions first, the priority list last.
                # A policy-cleanup task already matches; its 0% is a placeholder until measured, and the
                # byte-match-first policy leaves cleanup for the end, so those go last too.
                pending.sort(key=lambda item: (item["symbol"] in focus, item.get("kind") == "cleanup", float(item["base_pct"]),-int(item.get("value_score") or 0), item["residual_functions"], item["id"]))
            else:
                pending.sort(key=lambda item: (focus.get(item["symbol"], len(focus)), register_only(item), -int(item.get("value_score") or 0), -float(item["base_pct"]), item["residual_functions"], -item["size"], item["id"]))
            blocked = set()
            for item in pending:
                try:
                    claim = COORDINATOR.claim(
                        item.get("owner_source", item["source"]), worker,
                        item["symbol"], automatic=True, detail="Generating and verifying candidate",
                    )
                except ClaimConflict:
                    blocked.add(item.get("owner_source", item["source"]))
                    continue
                try:
                    state = load_state(host, model, num_predict, update_settings=False)
                    item = state["items"][item["id"]]
                    if item.get("status") != "pending":
                        continue
                    state["settings"]["ollama_num_predict"] = num_predict
                    item["worker"] = worker
                    try:
                        work_task(state, item, host, model, timeout, worker, num_predict)
                        processed += 1
                        backoff = 0
                    except HostUnavailable:
                        backoff = min(max(backoff * 2, HOST_BACKOFF[0]), HOST_BACKOFF[1])
                finally:
                    COORDINATOR.release(claim["source"], claim["token"])
                waiting = None
                break
            else:
                # Every pending owner is claimed elsewhere; say so once instead of idling silently.
                if blocked != waiting:
                    event(state, "waiting_on_claims", worker=worker, sources=sorted(blocked))
                    save_state(state)
                    waiting = blocked
                pause(15)
            if BREATHER_SECONDS > 0:
                # Only a worker started with --breather runs queued jobs; library callers and tests never do.
                take_breather(worker, host, model, num_predict)
            pause(backoff or delay)
        print(json.dumps({"processed": processed, "remaining": sum(item.get("status") == "pending" for item in state["items"].values())}, indent=2))
        return 0
    finally:
        release_lock(worker)


def latest_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """The newest chart point by time; the stored list is not guaranteed to end with it."""
    return max(state.get("snapshots") or [{}], key=lambda row: str(row.get("at", "")))


def progress_snapshot(state: dict[str, Any], report: dict[str, Any], force: bool = False) -> dict[str, Any] | None:
    """One point of the over-time chart: the report's measures and the queue's counts.

    Without `force`, returns None unless the report has changed since the newest point."""
    try:
        report_at = datetime.fromtimestamp(REPORT_FILE.stat().st_mtime, UTC).isoformat()
    except OSError:
        report_at = None
    last = latest_snapshot(state)
    measures = report.get("measures") or {}
    missing_code_point = measures.get("matched_code_percent") is not None and last.get("matched_code_percent") is None
    if not force and (report_at is None or (last.get("report_at") == report_at and not missing_code_point)):
        return None
    items = list(state.get("items", {}).values())
    return {
        "at": timestamp(), "report_at": report_at,
        "fuzzy_match_percent": measures.get("fuzzy_match_percent", 0),
        "matched_functions": measures.get("matched_functions", 0),
        "total_functions": measures.get("total_functions", 0),
        "matched_code": measures.get("matched_code"),
        "total_code": measures.get("total_code"),
        "matched_code_percent": measures.get("matched_code_percent"),
        "complete_code_percent": measures.get("complete_code_percent", 0),
        "worklist": len(items),
        "review_exact": sum(item.get("status") == "review_exact" for item in items),
        "attempted": sum(int(item.get("attempts", 0)) > 0 for item in items),
    }


def recent_responses(items: list[dict[str, Any]], workers: list[str], limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Each worker's latest model responses with the measured result, newest first.

    Read from the retained attempt files. `prior_best` is the best score before that attempt
    (the base or an earlier attempt), so the dashboard can say whether it moved the function."""
    recent: dict[str, list[dict[str, Any]]] = {}
    for worker in workers:
        tasks = sorted((item for item in items if item.get("worker") == worker and int(item.get("attempts") or 0) > 0),
                       key=lambda item: item.get("last_attempt_at") or "", reverse=True)[:limit]
        rows = []
        for item in tasks:
            folder = STATE_DIR / "candidates" / item["id"]
            reports = sorted(folder.glob("attempt-*.report.json"))
            best = float(item.get("base_pct") or 0)
            for path in reports:
                record = read_json(path, {})
                measured = record.get("report") or {}
                pct = measured.get("pct")
                if path in reports[-limit:] and record.get("worker") == worker:
                    stem = path.name.removesuffix(".report.json")
                    try:
                        response = (folder / f"{stem}.response.md").read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        response = ""
                    try:
                        focused = "## Focused edit (this attempt)" in (folder / f"{stem}.prompt.md").read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        focused = False
                    feedback = measured.get("diff_feedback") or []
                    rows.append({
                        "symbol": item.get("symbol"), "attempt": int(stem.split("-")[-1]), "at": record.get("at"),
                        "status": record.get("status"), "error": (record.get("error") or "")[-600:],
                        "base_pct": record.get("base_pct", item.get("base_pct")), "prior_best": best,
                        "pct": pct, "deltas": measured.get("deltas"), "focused": focused,
                        "variant_pcts": measured.get("variant_pcts"),
                        "response": response[:6000], "response_chars": len(response),
                        "first_hunk": (feedback[0] if isinstance(feedback, list) and feedback else "")[:1500],
                    })
                if pct is not None:
                    best = max(best, float(pct))
        rows.sort(key=lambda row: row.get("at") or "", reverse=True)
        recent[worker] = rows[:limit]
    return recent


def bench_activity(bench_dir: Path) -> dict[str, Any]:
    """What the bench is doing now, from the status file its runs and queue write at every step.
    A status whose process has gone reads as idle, or as stopped unexpectedly mid-step."""
    status = read_json(bench_dir / "status.json", {}) or {}
    if not status:
        return {"phase": "idle", "detail": "no benchmark has run yet", "alive": False}
    try:
        alive = portable.pid_alive(int(status.get("pid") or 0))
    except (OSError, ValueError):
        alive = False
    if not alive and status.get("phase") not in {"finished", "idle", "stopped", "failed"}:
        status = {**status, "phase": "dead", "detail": f"stopped unexpectedly while: {status.get('detail')}"}
    elif not alive:
        status = {**status, "phase": "idle", "detail": f"nothing running (last: {status.get('run') or 'queue'} "
                                                        f"{status.get('phase')}: {status.get('detail')})"}
    return {**status, "alive": alive}


def bench_status() -> dict[str, Any]:
    """The model benchmark (harness/local_campaign_bench.py): its task set, and each run's summary,
    per-task results and the task it is on now. Runs write results.json after every task."""
    import local_campaign_bench as bench_module  # imported here: the bench module imports this one
    bench = STATE_DIR / "bench"
    tasks = read_json(bench / "set.json", {}).get("tasks", [])
    runs = []
    for folder in sorted(path for path in (bench / "runs").glob("*") if path.is_dir()) if (bench / "runs").is_dir() else []:
        results = bench_module.restrict_to_set(read_json(folder / "results.json", {}), "main")
        run_state = read_json(folder / "state.json", {})
        summary = results.get("summary") or {}
        current = next((item for item in run_state.get("items", {}).values() if item.get("status") == "running"), None)
        activity_row = (current or {}).get("activity") or {}
        # A run killed mid-task leaves a "running" item behind: only a recent heartbeat counts as live.
        fresh = bool(activity_row.get("updated_at")) and \
            (datetime.now(UTC) - datetime.fromisoformat(activity_row["updated_at"])).total_seconds() < 900
        runs.append({
            "name": folder.name,
            "model": summary.get("model") or (run_state.get("settings") or {}).get("ollama_model"),
            "settings": bench_module.settings_label({**(read_json(folder / "meta.json", {}) or {}), **summary}),
            "summary": summary, "finished": bool(summary.get("finished_at")),
            "best_attempt": bench_module.best_attempt(results.get("tasks", [])),
            "started_at": summary.get("started_at") or run_state.get("updated_at"),
            "tasks": {row["id"]: {"outcome": row.get("outcome"), "base_pct": row.get("base_pct"), "best_pct": row.get("best_pct"),
                                  "seconds": row.get("seconds"), "attempts": len(row.get("attempt_rows") or [])}
                      for row in results.get("tasks", [])},
            "current": {"id": current["id"], "symbol": current["symbol"], "attempts": current.get("attempts"),
                        "phase": activity_row.get("phase")} if current and fresh else None,
        })
    runs.sort(key=lambda run: run.get("started_at") or "")
    prelim = prelim_status()
    activity = bench_activity(bench)
    if activity["phase"] in {"idle", "dead"}:
        # A run started before status reporting (or whose status lagged) still shows its task in progress.
        live = [(run["name"], run["current"]) for run in runs if run.get("current")] + \
               [(row["name"], row["current"]) for row in prelim.get("runs", []) if row.get("current")]
        if live:
            name, current = live[0]
            activity = {"phase": "running", "detail": f"{current['symbol']} (attempt {current.get('attempts') or 1})",
                        "run": name, "alive": True, "inferred": True}
    return {"tasks": tasks, "runs": runs, "prelim": prelim, "activity": activity}


def prelim_status() -> dict[str, Any]:
    """The preliminary round: each run's speed and answer health and its gate verdict. Main-set runs are
    included on the preliminary tasks, so models that skipped the round still appear."""
    import local_campaign_bench as bench  # imported here: the bench module imports this one
    try:
        runs = bench.load_runs("prelim")
        verdicts = bench.gate(runs)
    except (OSError, ValueError, KeyError):
        return {"tasks": [], "runs": []}
    rows = []
    for run in runs:
        summary = run.get("summary") or {}
        folder = bench.runs_dir("prelim") / str(summary.get("name"))
        current = next((item for item in read_json(folder / "state.json", {}).get("items", {}).values()
                        if item.get("status") == "running"), None) if run["source"] == "prelim" else None
        passed, reason = verdicts.get(summary.get("name"), (False, ""))
        rows.append({
            "name": summary.get("name"), "model": summary.get("model"), "source": run["source"],
            "settings": summary.get("settings"),
            "finished": bool(summary.get("finished_at")), "measured": summary.get("measured"), "tasks": summary.get("tasks"),
            "attempts_per_hour": summary.get("attempts_per_hour"), "seconds_per_attempt": summary.get("seconds_per_attempt"),
            "gen_tps": summary.get("gen_tps"), "prompt_tps": summary.get("prompt_tps"),
            "unusable_rate": summary.get("unusable_rate"), "no_compile": (summary.get("rates") or {}).get("verification_error"),
            "improved": summary.get("improved"), "best_attempt": bench.best_attempt(run.get("tasks", [])),
            "passed": passed, "reason": reason,
            "current": {"symbol": current["symbol"], "attempts": current.get("attempts")} if current else None,
        })
    return {"tasks": read_json(bench.set_file("prelim"), {}).get("tasks", []), "runs": rows}


def dashboard() -> dict[str, Any]:
    state = load_state(DEFAULT_HOST, DEFAULT_MODEL, update_settings=False)
    report = read_json(REPORT_FILE, {"measures": {}, "categories": [], "units": []})
    maps: dict[str, list[dict[str, Any]]] = {}
    for unit in report.get("units", []):
        metadata = unit.get("metadata") or {}
        if metadata.get("auto_generated") or not metadata.get("source_path"):
            continue
        measures = unit.get("measures") or {}
        maps.setdefault(category(unit), []).append({
            "name": unit["name"], "source": metadata["source_path"], "fuzzy": measures.get("fuzzy_match_percent", 0),
            "matched": measures.get("matched_functions", 0), "total": measures.get("total_functions", 0),
            "complete": bool(metadata.get("complete")), "code": int(measures.get("total_code") or 0),
        })
    for entries in maps.values():
        entries.sort(key=lambda entry: (-entry["code"], entry["name"]))
    items = list(state["items"].values())
    high_value = sorted(
        (item for item in items if item.get("status") == "pending"),
        key=lambda item: (-int(item.get("value_score") or 0), -float(item.get("base_pct") or 0), item.get("id", "")),
    )[:20]
    point = progress_snapshot(state, report)
    if point:
        # Keep the chart current: record a point whenever the report changed since the last one.
        state["snapshots"].append(point)
        save_state(state)
    import m2c_unit_queue
    # Every dashboard project reads the same project/version-scoped m2c layout.
    m2c = m2c_report.load_report(ROOT / "build" / "decomp-support" / VERSION)
    m2c["unit_promotions"] = m2c_unit_queue.sweep_snapshot(m2c, ROOT, VERSION)
    return {
        "generated_at": timestamp(), "report_measures": report.get("measures") or {}, "categories": report.get("categories") or [], "maps": maps,
        "project": {"id": harness_paths.PROJECT_ID, "name": harness_paths.SELECTED.name, "version": VERSION},
        "freshness": freshness(ROOT, VERSION),
        # Queue panels and the progress chart change only on sync; the page warns when the report is newer.
        "queue_synced_at": max((row.get("at", "") for row in state.get("events", []) if row.get("kind") == "queue_synced"), default=None),
        "queue": {
            "total": len(items), "status": Counter(item.get("status", "pending") for item in items),
            "active": [item for item in items if item.get("status") == "running"],
            "review": [{**item, "obligations": obligations.ledger(item.get("last_report") or {},
                        candidate_sha256=(item.get("last_report") or {}).get("candidate_sha256"))}
                       for item in sorted((item for item in items if item.get("status") == "review_exact"),
                                          key=lambda item: item.get("last_attempt_at", ""), reverse=True)[:100]],
            "high_value": high_value,
            "local_priority": local_priority(items),
        },
        "breather": read_json(breather_file(), {"jobs": []}),
        # Merged writers append out of order; show the newest events, not the last appended.
        "events": sorted(state.get("events", []), key=lambda row: row.get("at", ""), reverse=True)[:80], "snapshots": state.get("snapshots", []), "settings": state.get("settings", {}),
        "workers": state.get("workers", {}),
        "recent_responses": recent_responses(items, list(state.get("workers", {}))),
        "bench": bench_status(),
        "m2c": m2c,
        "worker_outcomes": {
            name: {
                "attempted": sum(item.get("worker") == name and int(item.get("attempts", 0)) > 0 for item in items),
                "improved": sum(item.get("worker") == name and (item.get("last_report") or {}).get("pct") is not None
                                and item["last_report"]["pct"] > item["base_pct"] for item in items),
                "status": Counter(item["status"] for item in items if item.get("worker") == name),
            } for name in state.get("workers", {})
        },
        "priority_analysis": state.get("priority_analysis", {}),
        "coordination": COORDINATOR.snapshot(),
    }



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", DEFAULT_HOST))
    parser.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument("--worker", default=DEFAULT_WORKER,
                        help=f"fleet worker name for claims, locks, and dashboard rows (default: {DEFAULT_WORKER})")
    parser.add_argument("--num-predict", type=int, default=DEFAULT_NUM_PREDICT,
                        help=f"maximum generated tokens per Ollama request (default: {DEFAULT_NUM_PREDICT})")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                        help="context window for prompts that fit; larger prompts fall back to "
                             f"{DEFAULT_NUM_CTX} rather than being truncated (default: {DEFAULT_NUM_CTX})")
    parser.add_argument("--think", choices=("auto", "on", "off"), default="auto",
                        help="reasoning for thinking models: auto sends nothing (the model decides), "
                             "on/off set Ollama's think flag (default: auto)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="one sampling temperature for every attempt instead of the retry schedule")
    parser.add_argument("--sampling", type=json.loads, default={},
                        help='extra sampling fields as JSON, e.g. \'{"top_p": 0.95, "top_k": 20, "min_p": 0}\'')
    parser.add_argument("--template-kwargs", type=json.loads, default={},
                        help='chat-template options as JSON, e.g. \'{"reasoning_effort": "medium"}\' (llama-server only)')
    parser.add_argument("--reasoning-tokens", type=int, default=DEFAULT_REASONING_TOKENS,
                        help=f"output allowance for thinking under --think on (default: {DEFAULT_REASONING_TOKENS})")
    commands =parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="queue every source-backed, non-exact report function")
    sync_parser.add_argument("--reset", action="store_true", help="restart existing tasks from pending")
    run_parser = commands.add_parser("run", help="ask the local model to process pending tasks")
    run_parser.add_argument("--limit", type=int, default=0, help="maximum tasks; 0 means all pending")
    run_parser.add_argument("--timeout", type=int, default=900)
    run_parser.add_argument("--recycle", action="store_true",
                            help="when no task is pending, re-queue attempts made before the latest harness improvement")
    run_parser.add_argument("--retries", type=int, default=1,
                            help="correction attempts per task after the first, each given the latest diff (default: 1)")
    run_parser.add_argument("--delay", type=float, default=0)
    run_parser.add_argument("--max-function-bytes", type=int, default=0,
                            help="fast-worker size ceiling; 0 includes the whole queue")
    run_parser.add_argument("--stall-rounds", type=int, default=4,
                            help="stop a task's correction rounds after this many without a new best (default: 4)")
    run_parser.add_argument("--permute", action="store_true",
                            help="after the model rounds, search declaration orders for near-exact tasks")
    run_parser.add_argument("--permute-cap", type=int, default=720, help="maximum orders tried per task (default: 720)")
    run_parser.add_argument("--permute-seconds", type=int, default=600, help="time budget per task (default: 600)")
    run_parser.add_argument("--min-pct", type=float, default=0,
                            help="only take functions already at least this close to exact (default: 0)")
    run_parser.add_argument("--lowest-first", action="store_true",
                            help="take the functions furthest from exact first, the priority list last")
    run_parser.add_argument("--rewrite", action="store_true",
                            help="after the model rounds, run the policy-safe rewrite search on near-exact tasks")
    run_parser.add_argument("--rewrite-seconds", type=int, default=600, help="rewrite budget per task (default: 600)")
    run_parser.add_argument("--task-seconds", type=float, default=None,
                            help="time budget per task: correction rounds continue until it is spent, replacing "
                                 f"--retries (at most {BUDGET_ATTEMPT_CAP} attempts)")
    run_parser.add_argument("--diversify", action="store_true",
                            help="after an unchanged or repeated answer, sample hotter and name one kind of edit to make")
    run_parser.add_argument("--breather", type=float, default=DEFAULT_BREATHER_SECONDS,
                            help="seconds to pause after each task, holding no claim or build lock, after running "
                                 "any queued breather jobs (default: %(default)s)")
    run_parser.add_argument("--promote", action="store_true",
                            help="write each task's best candidate into its owner source and commit it when it beats "
                                 "the source and a report rebuild shows nothing regressed")
    run_parser.add_argument("--focus-variants", type=int, default=DEFAULT_FOCUS_VARIANTS,
                            help="alternatives asked for per focused edit (default: %(default)s)")
    rewrite_parser = commands.add_parser("rewrite", help="run the policy-safe rewrite search on one function")
    rewrite_parser.add_argument("symbol")
    rewrite_parser.add_argument("--seconds", type=int, default=600)
    rewrite_parser.add_argument("--sideways", type=int, default=REWRITE_SIDEWAYS,
                                help="equal-score moves allowed in a row (default: %(default)s)")
    rewrite_parser.add_argument("--seed", type=int, default=0)
    rewrite_parser.add_argument("--worker", default="manual")
    explain_parser = commands.add_parser("explain", help="replay one function's register allocation against retail")
    explain_parser.add_argument("symbol")
    explain_parser.add_argument("--worker", default="manual")
    commands.add_parser("status", help="show campaign state")
    claim_parser = commands.add_parser("claim", help="reserve a function's entire source owner")
    claim_parser.add_argument("task", help="queue ID or unambiguous symbol")
    claim_parser.add_argument("--worker", default="manual")
    claim_parser.add_argument("--detail", default="Reviewing and matching source")
    release_parser = commands.add_parser("release", help="release a manual source claim")
    release_parser.add_argument("source", help="owner source path returned by claim")
    release_parser.add_argument("--token", required=True)
    build_parser = commands.add_parser("build", help="run a command under the shared build lock")
    build_parser.add_argument("--worker", default="manual")
    build_parser.add_argument("build_command", nargs=argparse.REMAINDER)
    commands.add_parser("claims", help="show source claims and waiting builds")
    breather_parser = commands.add_parser("breather", help="queue jobs for the running worker's next breather")
    breather_parser.add_argument("action", choices=("add", "list"))
    breather_parser.add_argument("--sync", action="store_true", help="queue a campaign queue sync (run inside the worker)")
    breather_parser.add_argument("--description", default="")
    breather_parser.add_argument("--by", default="", help="who queued it")
    breather_parser.add_argument("job_command", nargs=argparse.REMAINDER, help="-- COMMAND to run from the decomp root")
    args = parser.parse_args()
    if args.command == "run":
        def stop_worker(signum, frame):
            global STOP_REQUESTED
            STOP_REQUESTED = True
        signal.signal(signal.SIGTERM, stop_worker)
        signal.signal(signal.SIGINT, stop_worker)
    if args.num_predict < 1:
        parser.error("--num-predict must be at least 1")
    if args.num_ctx < 2048:
        parser.error("--num-ctx must be at least 2048")
    global NUM_CTX, THINK, REASONING_TOKENS, FIXED_TEMPERATURE, SAMPLING, TEMPLATE_KWARGS, RETRIES, RECYCLE, LOWEST_FIRST, STALL_ROUNDS, PERMUTE, PERMUTE_CAP, PERMUTE_SECONDS, REWRITE, REWRITE_SECONDS, TASK_SECONDS, DIVERSIFY, FOCUS_VARIANTS, PROMOTE, BREATHER_SECONDS
    NUM_CTX = args.num_ctx
    RETRIES = max(0, getattr(args, "retries", 1))
    if args.command == "run":
        RECYCLE, LOWEST_FIRST = args.recycle, args.lowest_first
        STALL_ROUNDS, PERMUTE = max(1, args.stall_rounds), args.permute
        PERMUTE_CAP, PERMUTE_SECONDS = max(1, args.permute_cap), max(10, args.permute_seconds)
        REWRITE, REWRITE_SECONDS = args.rewrite, max(10, args.rewrite_seconds)
        TASK_SECONDS, DIVERSIFY, FOCUS_VARIANTS = args.task_seconds, args.diversify, max(1, args.focus_variants)
        PROMOTE = args.promote
        BREATHER_SECONDS = max(0.0, args.breather)
        RUN_OPTIONS.update(stall_rounds=STALL_ROUNDS, permute=PERMUTE, rewrite=REWRITE, retries=RETRIES, timeout=args.timeout, max_function_bytes=args.max_function_bytes, min_pct=args.min_pct, recycle=RECYCLE, lowest_first=LOWEST_FIRST,
                           task_seconds=TASK_SECONDS, diversify=DIVERSIFY, focus_variants=FOCUS_VARIANTS, promote=PROMOTE, breather=BREATHER_SECONDS)
    if args.command in {"sync", "run", "rewrite", "explain"} and not VERSION:
        parser.error(f"no build version for the decomp at {ROOT}: configure a project in harness.local.json "
                     "or set DECOMP_ROOT/DECOMP_VERSION")
    THINK = {"auto": None, "on": True, "off": False}[args.think]
    REASONING_TOKENS = args.reasoning_tokens
    FIXED_TEMPERATURE, SAMPLING, TEMPLATE_KWARGS = args.temperature, args.sampling, args.template_kwargs
    if args.command in {"sync", "run"}:
        with COORDINATOR.writer(shared=args.command == "run"):
            state = load_state(args.ollama_host, args.model, args.num_predict)
            if args.command == "run" and not state["items"]:
                parser.error("queue is empty; run sync before starting workers")
            if args.command == "sync":
                with COORDINATOR.build("Queue sync", "Refresh authoritative report"):
                    result = sync(state, getattr(args, "reset", False))
                print(json.dumps(result, indent=2))
            if args.command == "run":
                return run(state, args.ollama_host, args.model, args.limit, args.timeout, args.delay, args.worker, args.num_predict, args.max_function_bytes, args.min_pct)
    elif args.command == "claim":
        state = load_state(args.ollama_host, args.model, args.num_predict, update_settings=False)
        matches = [item for item in state["items"].values() if args.task in (item["id"], item["symbol"])]
        if len(matches) != 1 or not matches[0].get("owner_source"):
            parser.error("task must identify one source-backed queue entry; use its full ID if ambiguous")
        item = matches[0]
        claim = COORDINATOR.claim(item["owner_source"], args.worker, item["symbol"], detail=args.detail)
        print(json.dumps(claim, indent=2))
    elif args.command == "release":
        COORDINATOR.release(args.source, args.token)
    elif args.command == "build":
        command = args.build_command
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            parser.error("build requires a command after --")
        with COORDINATOR.build(args.worker, " ".join(command)):
            return subprocess.run(command, cwd=ROOT).returncode
    elif args.command == "breather":
        if args.action == "list":
            print(json.dumps(read_json(breather_file(), {"jobs": []}), indent=2))
            return 0
        # argparse's REMAINDER also swallows options written after `add`: read them back from before the `--`.
        tokens, command = list(args.job_command), []
        if "--" in tokens:
            cut = tokens.index("--")
            tokens, command = tokens[:cut], tokens[cut + 1:]
        extra = argparse.ArgumentParser(prog="breather add")
        extra.add_argument("--sync", action="store_true")
        extra.add_argument("--description", default=None)
        extra.add_argument("--by", default=None)
        more, leftover = extra.parse_known_args(tokens)
        if leftover:
            parser.error(f"unexpected arguments before --: {' '.join(leftover)} (put the command after --)")
        args.sync = args.sync or more.sync
        args.description = more.description if more.description is not None else args.description
        args.by = more.by if more.by is not None else args.by
        if not args.sync and not command:
            parser.error("breather add needs --sync or -- COMMAND")
        job = breather_add("sync" if args.sync else "command", None if args.sync else command, args.description, args.by)
        print(json.dumps(job, indent=2))
        return 0
    elif args.command == "claims":
        print(json.dumps(COORDINATOR.snapshot(), indent=2))
    elif args.command == "rewrite":
        item = locate_symbol(args.symbol)
        claim = COORDINATOR.claim(item["owner_source"], args.worker, args.symbol, detail="Rewrite search")
        try:
            result = rewrite_search(item, args.worker, args.seconds, sideways=args.sideways, seed=args.seed)
        finally:
            COORDINATOR.release(claim["source"], claim["token"])
        folder = STATE_DIR / "rewrite"
        folder.mkdir(parents=True, exist_ok=True)
        if result.get("improved"):
            (folder / f"{args.symbol}.c").write_text(result["function"], encoding="utf-8", newline="")
        summary = {key: result.get(key) for key in ("symbol", "base", "best", "tried", "steps", "flags", "skipped", "aborted")}
        summary["candidate"] = str((folder / f"{args.symbol}.c").relative_to(ROOT)) if result.get("improved") else None
        print(json.dumps(summary, indent=2))
    elif args.command == "explain":
        text = explain_registers(locate_symbol(args.symbol), args.worker)
        print(text if text is not None else "register replay unavailable (tools missing, or the replay failed)")
    else:
        state = load_state(args.ollama_host, args.model, args.num_predict, update_settings=False)
        print(json.dumps({"state": str(STATE_FILE.relative_to(ROOT)), "counts": Counter(item.get("status", "pending") for item in state["items"].values()), "settings": state["settings"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

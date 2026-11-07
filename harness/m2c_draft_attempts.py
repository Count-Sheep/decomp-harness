#!/usr/bin/env python3
"""Resumable, review-only C attempts for the selected project's assembly-only m2c drafts."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from harness_paths import DECOMP_ROOT, VERSION, build_tool, compiler_wrapper, env
import m2c_compile_repair as compile_repair
from m2c_context import build_check

# A project can mix SDK, game, and runtime-library objects built by different compilers.
# Linked exact units establish each compiler and the profile-specific character/codegen flags.
PROFILES = ("1.2.5n", "1.3", "1.3.2", "2.0", "2.5", "2.6", "3.0a5.2")
# Additional compilers require recorded flags and a hash-bound binary, not a generic search.
STRICT_PROFILES = (*PROFILES, "2.0p1")
PROFILE_FLAGS = {
    "1.2.5n": ("-char", "unsigned"),
    "1.3": ("-char", "unsigned"),
    "1.3.2": ("-char", "unsigned"),
    "2.0": ("-char", "unsigned"),
    "2.5": ("-char", "signed", "-use_lmw_stmw", "on"),
    "2.6": ("-char", "signed", "-use_lmw_stmw", "on"),
    "3.0a5.2": ("-char", "signed", "-use_lmw_stmw", "on", "-enc", "SJIS"),
}
FLAGS = ("-nodefaults", "-proc", "gekko", "-align", "powerpc", "-enum", "int",
         "-fp", "hardware", "-Cpp_exceptions", "off", "-O4,p", "-inline", "auto",
         "-RTTI", "off", "-fp_contract", "off", "-str", "reuse", "-multibyte",
         "-sym", "on", "-requireprotos", "-lang=c")
PRELUDE = ("typedef signed char s8; typedef unsigned char u8;\n"
           "typedef short s16; typedef unsigned short u16;\n"
           "typedef int s32; typedef unsigned int u32;\n"
           "typedef long long s64; typedef unsigned long long u64;\n"
           "typedef float f32; typedef double f64;\n#define NULL ((void *)0)\n")


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def model_id(host: str, timeout: int) -> str:
    with urllib.request.urlopen(host.rstrip("/") + "/models", timeout=timeout) as response:
        data = json.load(response)
    return data["data"][0]["id"]


def model_reply(host: str, model: str, prompt: str, timeout: int) -> str:
    request = urllib.request.Request(host.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": [
            {"role": "system", "content": "Return only a self-contained C function and required extern declarations. No Markdown, asm, pragmas, includes, or commentary."},
            {"role": "user", "content": prompt}], "temperature": 0.6, "max_tokens": 1024,
            "stream": False}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["choices"][0]["message"]["content"] or ""


def clean_code(response: str, symbol: str) -> str:
    code = response.strip()
    fence = re.fullmatch(r"```(?:c|C)?\s*\n(.*?)\n```", code, re.S)
    if fence:
        code = fence.group(1).strip()
    if not re.search(r"\b" + re.escape(symbol) + r"\s*\(", code):
        raise ValueError("response does not define the assigned function")
    if re.search(r"(?m)^\s*#\s*(?:include|pragma)\b|\b(?:asm|__asm__)\b|M2C_", code):
        raise ValueError("response contains unsupported C or unresolved m2c markers")
    return code + "\n"


def draft_rows(batch: Path) -> list[dict]:
    data = read_json(batch / "results.json")
    if data.get("results") is not None:
        candidates = data["results"]
    else:
        candidates = [read_json(path) for path in sorted((batch / "results").glob("*.json"))]
    rows = [row for row in candidates if row.get("status") != "exact_reuse"
            and Path(row.get("artifacts", ""), "draft.c").is_file()]
    rows.sort(key=lambda row: (-int((row.get("priority") or {}).get("score") or 0),
                               sum((row.get("diagnostics") or {}).values()), row["size"], row["address"]))
    return rows


def target_object(root: Path, version: str, row: dict) -> Path:
    path = root / "build" / version / "obj" / (row["unit"] + ".o")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def instructions(text: str) -> list[str]:
    labels = {label: f".L{index}" for index, label in enumerate(
        re.findall(r"(?m)^(\.L_[0-9A-Fa-f]+):", text))}
    return [re.sub(r"\.L_[0-9A-Fa-f]+", lambda match: labels.get(match.group(), match.group()),
                   line.split("*/", 1)[1].strip())
            for line in text.splitlines() if "*/" in line]


def instruction_diff(row: dict, disasm: Path) -> str:
    target = instructions(Path(row["assembly"]).read_text(encoding="utf-8"))
    actual = instructions(disasm.read_text(encoding="utf-8"))
    return "\n".join(list(difflib.unified_diff(target, actual, fromfile="target", tofile="compiled",
                                                lineterm=""))[:45])[:2200]


def compiler_choices(root: Path, row: dict) -> list[tuple[str, tuple[str, ...], dict]]:
    context = row.get("compiler_profile") or {}
    inferred = context.get("profiles") or []
    if context.get("strict_selection_required"):
        selected = context.get("selected") or {}
        profile = str(selected.get("mw_version") or "").removeprefix("GC/")
        flags = selected.get("flags")
        if (not context.get("supported") or profile not in STRICT_PROFILES
                or inferred != [selected] or not isinstance(flags, list) or not flags
                or any(not isinstance(flag, str) or not flag for flag in flags)):
            raise ValueError(f"Strict compiler profile unavailable for {row.get('symbol')}")
        binary = root / "build/compilers/GC" / profile / "mwcceppc.exe"
        expected = selected.get("binary_sha256")
        if (not expected or not binary.is_file()
                or str(binary) != selected.get("binary")
                or hashlib.sha256(binary.read_bytes()).hexdigest() != expected):
            raise ValueError(f"Strict compiler binary changed for {row.get('symbol')}")
        return [(profile, tuple(flags), selected)]
    choices = []
    seen = set()
    for evidence in inferred:
        profile = str(evidence.get("mw_version") or "").removeprefix("GC/")
        flags = tuple(str(flag) for flag in evidence.get("flags") or [])
        key = (profile, flags)
        if profile not in PROFILES or not flags or key in seen:
            continue
        if not (root / "build/compilers/GC" / profile / "mwcceppc.exe").is_file():
            continue
        seen.add(key)
        choices.append((profile, flags, evidence))
    if choices:
        for profile in dict.fromkeys(choice[0] for choice in choices):
            flags = (*FLAGS, *PROFILE_FLAGS[profile])
            key = (profile, flags)
            if key not in seen:
                choices.append((profile, flags, {"confidence": "same_version_known_fallback",
                                                  "settings": {"fp_contract": "off"}}))
        return choices
    return [(profile, (*FLAGS, *PROFILE_FLAGS[profile]), {"confidence": "generic_fallback"})
            for profile in PROFILES]


def compile_context(row: dict) -> str:
    """Keep authoritative declarations without duplicating m2c-only scaffolding."""
    knowledge = read_json(Path(row.get("artifacts") or "") / "knowledge.json")
    header = (knowledge.get("context") or {}).get("context") or ""
    declarations = []
    for line in header.splitlines():
        stripped = line.strip()
        if not stripped.startswith("extern "):
            continue
        declarations.append(stripped)
    return "\n".join(declarations)


def score(root: Path, version: str, row: dict, code: str, folder: Path, prelude: str = PRELUDE,
          *, apply_repair: bool = True) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "candidate.c"
    repair = (compile_repair.repair_draft(code, row.get("symbol"), compile_context(row))
              if apply_repair else {"code": code, "changed": False, "ready_for_compile": True,
                                    "transformations": [], "unresolved": [], "disabled": True})
    source.write_text(prelude + repair["code"], encoding="utf-8")
    wrapper = compiler_wrapper(root)
    objdiff = build_tool(root, "objdiff-cli")
    repair_record = {key: value for key, value in repair.items() if key != "code"}
    outcome = {"pct": 0.0, "profile": None, "error": "", "compiled": False, "diff": "",
               "feedback": {}, "repair": repair_record, "profile_attempts": []}
    if not repair["ready_for_compile"]:
        outcome["error"] = "Deterministic repair blockers:\n" + "\n".join(repair["unresolved"])
        return outcome
    outcome["candidate"] = repair["code"]
    errors = []
    used_artifacts = set()
    for profile, flags, profile_evidence in compiler_choices(root, row):
        compiler = root / "build/compilers/GC" / profile / "mwcceppc.exe"
        artifact_profile = profile
        if artifact_profile in used_artifacts:
            artifact_profile = f"{profile}-{hashlib.sha256(chr(0).join(flags).encode()).hexdigest()[:8]}"
        used_artifacts.add(artifact_profile)
        obj = folder / (artifact_profile + ".o")
        compile_run = subprocess.run([*wrapper, str(compiler), *flags, "-c", str(source), "-o", str(obj)],
                                     cwd=root, capture_output=True, text=True, errors="replace", timeout=45)
        profile_attempt = {"profile": profile, "flags": list(flags),
                           "confidence": profile_evidence.get("confidence"),
                           "compiled": compile_run.returncode == 0}
        outcome["profile_attempts"].append(profile_attempt)
        if compile_run.returncode:
            errors.append(f"{profile}: {(compile_run.stdout + compile_run.stderr)[-1500:]}")
            continue
        outcome["compiled"] = True
        project = folder / artifact_profile
        project.mkdir(exist_ok=True)
        write_json(project / "objdiff.json", {"min_version": "3.0.0", "units": [{
            "name": "candidate", "target_path": str(target_object(root, version, row)), "base_path": str(obj)}]})
        report_path = project / "report.json"
        diff_run = subprocess.run([str(objdiff), "report", "generate", "-p", str(project),
                                   "-o", str(report_path), "-f", "json"],
                                  cwd=root, capture_output=True, text=True, errors="replace", timeout=45)
        if diff_run.returncode:
            errors.append(f"{profile} objdiff: {diff_run.stderr[-1500:]}")
            continue
        functions = [function for unit in read_json(report_path).get("units", [])
                     for function in unit.get("functions", []) if function.get("name") == row["symbol"]]
        if not functions:
            errors.append(f"{profile}: assigned symbol absent from objdiff report")
            continue
        pct = float(functions[0].get("fuzzy_match_percent") or 0)
        if outcome["profile"] is None or pct > outcome["pct"]:
            disasm = folder / (artifact_profile + ".s")
            disasm_run = subprocess.run([str(build_tool(root, "dtk")), "elf", "disasm", str(obj), str(disasm)],
                                        cwd=root, capture_output=True, text=True, errors="replace", timeout=20)
            if disasm_run.returncode:
                errors.append(f"{profile} disasm: {disasm_run.stderr[-1000:]}")
                diff = ""
                feedback = {}
            else:
                diff = instruction_diff(row, disasm)
                feedback = compile_repair.compare_assembly(
                    Path(row["assembly"]).read_text(encoding="utf-8"),
                    disasm.read_text(encoding="utf-8"),
                )
            outcome.update(pct=pct, profile=profile, profile_artifact=artifact_profile,
                           diff=diff, feedback=feedback,
                           compiler_flags=list(flags), compiler_evidence=profile_evidence)
    outcome["error"] = "\n".join(errors)[-2200:]
    return outcome


def prompt(row: dict, draft: str, prior: dict, best_code: str) -> str:
    assembly = Path(row["assembly"]).read_text(encoding="utf-8")
    profile = (row.get("compiler_profile") or {}).get("selected") or {}
    profile_text = (f"Likely compiler: {profile.get('mw_version', 'unknown')} "
                    f"({profile.get('confidence', 'unavailable')} confidence); settings: "
                    f"{json.dumps(profile.get('settings') or {}, sort_keys=True)}.\n")
    context_path = Path(row.get("artifacts") or "") / "knowledge.json"
    knowledge = read_json(context_path)
    typed_context = ((knowledge.get("context") or {}).get("context") or "")[:2600]
    bootstrap = row.get("bootstrap") or knowledge.get("bootstrap") or {}
    bootstrap_text = ""
    if bootstrap:
        bounded = {key: bootstrap.get(key) for key in
                   ("type_hints", "symbol_hints", "unit_regions", "call_clusters")
                   if bootstrap.get(key)}
        bootstrap_text = ("\nBootstrap evidence (hypotheses only; do not treat names, types, or unit "
                          "boundaries as proven):\n" + json.dumps(bounded, sort_keys=True)[:2600] + "\n")
    references = row.get("references") or knowledge.get("references") or []
    reference_text = ""
    for index, reference in enumerate(references[:5], 1):
        reference_text += (f"\nMatched reference {index}: {reference.get('project')}/"
                           f"{reference.get('symbol')} similarity={float(reference.get('similarity') or 0):.3f}; "
                           f"signature: {reference.get('signature') or 'unknown'}; compiler: "
                           f"{((reference.get('compiler') or {}).get('mw_version') or 'unknown')}\n"
                           f"{(reference.get('source_snippet') or '')[:1400]}\n")
    previous = (f"\nPrevious result: {prior.get('pct', 0):.3f}% with GC/{prior.get('profile') or '?'}; "
                f"compiler/objdiff feedback:\n{prior.get('error', '')[-1000:]}\n"
                f"Structured divergence:\n{compile_repair.format_feedback(prior.get('feedback') or {}) if prior.get('feedback') else 'unavailable'}\n"
                f"Deterministic repairs: {json.dumps((prior.get('repair') or {}).get('transformations') or [])[-1200:]}\n"
                f"Instruction diff (- target, + compiled):\n{prior.get('diff', '')[-2200:]}\n") if prior else ""
    return (f"Recover exactly one function named {row['symbol']} from a GameCube PowerPC target. "
            "This is an untyped m2c hypothesis; its types and names are not evidence. "
            "Use only standard C and the provided typedefs s8/u8/s16/u16/s32/u32/s64/u64/f32/f64. "
            "Declare any external symbols you need. Do not reproduce the typedefs. "
            "Preserve the target's behavior; compiler and objdiff, not appearance, judge the attempt.\n"
            f"{profile_text}\nTyped target context:\n{typed_context}\n"
            + bootstrap_text +
            f"Target assembly:\n{assembly[:5200]}\n\nm2c draft:\n{draft[:3500]}\n"
            + reference_text
            + (f"\nBest previous C:\n{best_code[:3500]}\n" if best_code else "") + previous)


def run(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    if build_check(root, args.version)["status"] != "exact":
        raise ValueError(f"{args.version} DOL build is not byte-exact")
    support = root / "build/decomp-support" / args.version
    state_path = support / "attempts" / "state.json"
    state = read_json(state_path)
    if args.batch:
        batch = args.batch
    elif state.get("batch"):
        batch = support / "batches" / state["batch"]
    else:
        batch = max((p for p in (support / "batches").iterdir()
                     if (p / "results.json").is_file() or (p / "manifest.json").is_file()),
                    key=lambda path: path.stat().st_mtime)
    if state and state.get("batch") != batch.name:
        raise ValueError("existing attempt state uses another batch; select its batch or archive state")
    if state and state.get("model") != args.model:
        state.setdefault("model_history", []).append({"model": state.get("model"),
                                                        "host": state.get("host"), "ended_at": now()})
        state["model"] = args.model
    if state:
        state["host"] = args.host
    state = state or {"version": args.version, "batch": batch.name, "model": args.model,
                      "host": args.host, "started_at": now(), "items": {}}
    write_json(state_path, state)
    while True:
        rows = draft_rows(batch)
        if args.limit:
            rows = rows[:args.limit]
        state["target_total"] = len(rows)
        state["draft_batch_finished"] = bool(read_json(batch / "manifest.json").get("finished"))
        state["updated_at"] = now()
        write_json(state_path, state)
        if not rows:
            if args.loop and not state["draft_batch_finished"]:
                time.sleep(max(args.pause, 10))
                continue
            raise ValueError("no completed m2c drafts in selected batch")
        work = False
        for row in rows:
            symbol = row["symbol"]
            item = state["items"].setdefault(symbol, {"symbol": symbol, "address": row["address"],
                "size": row["size"], "attempts": [], "best_pct": 0.0, "best_source": None})
            if item["best_pct"] >= 100 or len([a for a in item["attempts"] if a["index"] > 0]) >= args.max_attempts:
                continue
            work = True
            draft = Path(row["artifacts"], "draft.c").read_text(encoding="utf-8")
            number = len(item["attempts"])
            folder = support / "attempts" / symbol / f"{number:03d}"
            state["current"] = symbol
            state["updated_at"] = now()
            write_json(state_path, state)
            if number == 0:
                code = draft
                try:
                    outcome = score(root, args.version, row, code, folder)
                except (OSError, subprocess.TimeoutExpired) as error:
                    outcome = {"pct": 0.0, "profile": None, "error": str(error), "compiled": False}
            else:
                previous = item["attempts"][-1]
                if previous.get("compiled") and previous.get("profile") and "diff" not in previous:
                    prior_folder = support / "attempts" / symbol / f"{previous['index']:03d}"
                    artifact_profile = previous.get("profile_artifact") or previous["profile"]
                    disasm = prior_folder / (artifact_profile + ".s")
                    if not disasm.is_file():
                        subprocess.run([str(build_tool(root, "dtk")), "elf", "disasm",
                                        str(prior_folder / (artifact_profile + ".o")), str(disasm)],
                                       cwd=root, capture_output=True, timeout=20)
                    if disasm.is_file():
                        previous["diff"] = instruction_diff(row, disasm)
                best = (Path(item["best_source"]).read_text(encoding="utf-8").removeprefix(PRELUDE)
                        if item["best_source"] and item["best_pct"] > 0 else "")
                try:
                    while True:
                        try:
                            response = model_reply(args.host, args.model,
                                                   prompt(row, draft, previous, best), args.timeout)
                            state.pop("rpc_error", None)
                            break
                        except (urllib.error.URLError, TimeoutError) as error:
                            if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                                raise
                            state["rpc_error"] = str(error)
                            state["updated_at"] = now()
                            write_json(state_path, state)
                            print(f"RPC unavailable; retrying {symbol} in 30s: {error}", flush=True)
                            time.sleep(30)
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "response.txt").write_text(response, encoding="utf-8")
                    code = clean_code(response, symbol)
                    outcome = score(root, args.version, row, code, folder)
                except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                    outcome = {"pct": 0.0, "profile": None, "error": str(error), "compiled": False}
            before = float(item["best_pct"])
            attempt = {"index": number, "at": now(), "model": args.model, "host": args.host,
                       **outcome, "delta": round(outcome["pct"] - before, 5),
                       "source": str(folder / "candidate.c") if (folder / "candidate.c").is_file() else None}
            item["attempts"].append(attempt)
            if attempt["source"] and (not item["best_source"] or outcome["pct"] > before):
                item.update(best_pct=outcome["pct"], best_source=attempt["source"])
            state["updated_at"] = now()
            write_json(state_path, state)
            print(f"{symbol} #{number}: {outcome['pct']:.3f}% ({attempt['delta']:+.3f}) "
                  f"{outcome['profile'] or outcome['error'][:90]}", flush=True)
        if not args.loop or (not work and state["draft_batch_finished"]):
            break
        time.sleep(max(args.pause, 10) if not work else args.pause)
    state.pop("current", None)
    state["updated_at"] = now()
    write_json(state_path, state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=VERSION,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--host", default=env("MODEL_HOST", "http://127.0.0.1:18080/v1"),
                        help="OpenAI-compatible endpoint (default: DECOMP_MODEL_HOST or a local server)")
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int, default=0, help="0 means every completed draft")
    parser.add_argument("--max-attempts", type=int, default=8, help="per-function model attempt cap")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--pause", type=int, default=10)
    parser.add_argument("--loop", action="store_true", help="cycle until exact or max attempts")
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    if args.limit < 0 or args.max_attempts < 1 or args.timeout < 1 or args.pause < 0:
        parser.error("invalid attempt limits")
    args.model = args.model or model_id(args.host, min(args.timeout, 10))
    run(args)


if __name__ == "__main__":
    main()

"""Type-informed, source-body-held-out retrieval-off/on pilot on a frozen workspace."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import local_campaign_asm_inputs as asm_inputs
import local_campaign_corpus as corpus
import local_campaign_decompile as decompile
import local_campaign_knowledge as knowledge
import portable
from harness_paths import VERSION
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_knowledge_bench import validate_split


def declaration_context(text: str, required: set[str], inline_contracts: bool = False) -> str:
    """Use m2c's parser to select dependency-closed declarations, never C bodies."""
    from m2c import c_types
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator

    text = c_types.strip_comments(c_types.add_builtin_typedefs(text))
    text = c_types.remove_backslashes_at_eol(c_types.strip_macro_defs(c_types.process_ifdef(text)))
    ast, _ = c_types.parse_c(text, None)
    declarations, inline_names = [], set()
    for node in ast.ext:
        if isinstance(node, ca.FuncDef):
            node = node.decl
        if isinstance(node, ca.Decl):
            node.init = None
        if isinstance(node, (ca.Decl, ca.Typedef)):
            declarations.append(node)
            if isinstance(node, ca.Decl) and isinstance(node.type, ca.FuncDecl) and "inline" in node.funcspec:
                inline_names.add(node.name)

    class Names(ca.NodeVisitor):
        def __init__(self, providers=False):
            self.names = set()
            self.providers = providers

        def visit_IdentifierType(self, node):
            if not self.providers:
                self.names.update(node.names)

        def visit_ID(self, node):
            if not self.providers:
                self.names.add(node.name)

        def visit_Struct(self, node):
            if node.name and (not self.providers or node.decls is not None):
                self.names.add("struct:" + node.name)
            self.generic_visit(node)

        def visit_Union(self, node):
            if node.name and (not self.providers or node.decls is not None):
                self.names.add("union:" + node.name)
            self.generic_visit(node)

        def visit_Enum(self, node):
            if node.name and (not self.providers or node.values is not None):
                self.names.add("enum:" + node.name)
            self.generic_visit(node)

        def visit_Enumerator(self, node):
            if self.providers:
                self.names.add(node.name)
            self.generic_visit(node)

    providers = {}
    for index, node in enumerate(declarations):
        names = Names(providers=True)
        names.visit(node)
        if node.name:
            names.names.add(node.name)
        for name in names.names:
            providers[name] = index
    selected, pending = set(), set(required) | (inline_names if inline_contracts else set())
    while pending:
        name = pending.pop()
        index = providers.get(name)
        if index is None or index in selected:
            continue
        selected.add(index)
        dependencies = Names()
        dependencies.visit(declarations[index])
        pending.update(dependencies.names)
    generator = CGenerator()
    return "\n".join(generator.visit(declarations[index]) + ";" for index in sorted(selected))


def candidate_declarations(context: str, source: str, symbol: str) -> str:
    """Close real declaration dependencies introduced by a generated C body."""
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_pycparser.c_lexer import CLexer

    def fail(message, line, column):
        raise ValueError(f"Candidate lexing failed at {line}:{column}: {message}")

    lexer = CLexer(fail, lambda: None, lambda: None, lambda name: False)
    lexer.build()
    lexer.input(c_types.strip_comments(source))
    names = {symbol}
    while token := lexer.token():
        if token.type == "ID":
            names.add(token.value)
    return declaration_context(context, names)


def extract_assembly(root: Path, target: dict, version: str | None = None) -> str:
    version = version or VERSION
    relative = target["unit"].split("/", 1)[-1] + ".s"
    path = root / "build" / version / "asm" / relative
    text = path.read_text(encoding="utf-8")
    pattern = rf"(?ms)^\.fn\s+{re.escape(target['symbol'])},[^\n]*\n.*?^\.endfn[^\n]*"
    match = re.search(pattern, text)
    if match is None:
        raise ValueError("Frozen target assembly definition is missing")
    body = match.group() + "\n"
    instructions = [re.match(r"(\S+)\s*(.*)$", line.split("*/", 1)[1].lstrip())
                    for line in body.splitlines() if corpus.INSN.match(line)]
    strict = "\n".join(match[1] + " " + match[2] for match in instructions)
    if knowledge.digest(strict) != target["strict_hash"]:
        raise ValueError("Frozen target instruction identity changed")
    prepared, info = asm_inputs.prepare(root, target["symbol"], body.encode(), version)
    if info["missing_tables"]:
        raise ValueError("Frozen target has missing jump-table inputs")
    needed = set(corpus.DATA.findall(body))
    constants = [match.group() for match in asm_inputs.OBJECT.finditer(text)
                 if asm_inputs.table_name(match) in needed and asm_inputs.table_name(match) not in info["added_tables"]]
    return prepared.decode() + ("\n.data\n" + "\n".join(constants) if constants else "")


def prompt_pair(target: dict, signature: str, assembly: str, declarations: str, pack: dict,
                prompt_version: str = "minimal-v1", inline_contracts: bool = False,
                *, revision_context: bool = False) -> dict[str, str]:
    if target["id"] in {row["function_id"] for row in pack["references"]}:
        raise ValueError("Target source cannot be a reference")
    if prompt_version not in {"minimal-v1", "c89-symbolic-relocations-target-last-v2"}:
        raise ValueError("Unknown pilot prompt version")
    guidance = (
        "This is a C89 MWCC build: declare every local at the start of its block, before statements.\n"
        "Do not use compiler builtins or declare variables inside for-loop initializers.\n"
        "The @sda21/@ha/@l operands name linker-resolved symbols. Use the named global or its address;\n"
        "do not add the encoded relocation displacement, mask the address, or invent an address constant.\n"
        "Stack saves/restores of LR and preserved registers are compiler bookkeeping, not source operations.\n"
    ) if prompt_version != "minimal-v1" else ""
    if inline_contracts:
        guidance += ("Existing owner/header inline helpers are declared below, without their implementations.\n"
                     "A helper call may expand to target instructions without an assembly bl. Reuse it only\n"
                     "when its behavior is appropriate; its name or prototype is not proof of equivalence.\n")
    task = ("Revise the existing generated PowerPC C candidate using measured MWCC/objdiff feedback.\n"
            "Improve the measured differences with focused changes; do not start a new decompilation.\n"
            "The candidate and the sole response contract follow this frozen reference context.\n"
            if revision_context else
            "Reconstruct this PowerPC function as C for MWCC. Match the retail instruction bytes.\n"
            "Return only the complete function in a C code block, using the exact signature below.\n")
    provenance = ("The generated draft is supplied below; the assigned implementation is never supplied. "
                  "Types are project-provided contracts.\n\n" if revision_context else
                  "No assigned C implementation or previous attempt is supplied. Types are project-provided contracts.\n\n")
    base = (
        task +
        "Do not add includes, macros, pragmas, assembly, guessed struct fields or volatile qualifiers.\n"
        + guidance +
        "Use the supplied declarations and preserve evaluation order, signedness, constants and branch behavior.\n" +
        provenance +
        f"## Required signature\n```c\n{signature}\n```\n\n"
        f"## Type and symbol declarations (no function bodies or global initializers)\n```c\n{declarations}\n```\n\n"
    )
    references = []
    for row in pack["references"]:
        location = ("Local eight-instruction idiom only; assembly below is an excerpt, not the complete function.\n"
                    f"Match: {knowledge.encoded(row['idiom_match'])}\n"
                    f"Excerpt: {knowledge.encoded(row['assembly_excerpt'])}\n"
                    if row["reason"] == "COMPILER_IDIOM_REFERENCE" else "")
        references.append(
            f"### {row['symbol']} [{row['reason']}]\n"
            f"Source SHA-256: {row['source_sha256']}\n"
            + location +
            f"```c\n{row['source_text']}\n```\n"
            f"```asm\n{row['assembly']}\n```\n"
        )
    extra = ("\n## Optional compiler idiom references\n"
             "These are scorer-exact project observations, not verified target solutions.\n"
             "Shared callees or normalized shapes do not prove equivalent types, constants or semantics.\n"
             "Adapt only relevant idioms; do not copy reference behavior or change the required signature.\n"
             + "\n".join(references)) if references else ""
    heading = ("## Retail target assembly" if prompt_version == "minimal-v1" else
               "\n## Retail target assembly (the authoritative behavior to reconstruct)")
    if revision_context:
        heading = "\n## Retail target assembly (reference for the measured differences, not a new translation task)"
    target_assembly = f"{heading}\n```asm\n{assembly}\n```\n"
    return {"off": base + target_assembly,
            "on": base + target_assembly + extra if prompt_version == "minimal-v1" else base + extra + target_assembly}


def select_targets(store: knowledge.KnowledgeStore, split: dict, limit: int) -> list[str]:
    """Enrich a small pilot for covered targets, with up to three uncovered controls."""
    covered, controls = [], []
    excluded = set(split["excluded"])
    for key in split["targets"]:
        target = store.function(key)
        if not (target.get("source_path") or "").endswith(".c"):
            continue
        row = (target["insns"], key)
        pack = knowledge.retrieval_pack(store, key, excluded=excluded)
        (covered if pack["references"] else controls).append(row)
    covered.sort(); controls.sort()
    picks = covered[:max(1, limit - min(3, len(controls)))]
    remaining = limit - len(picks)
    picks += controls[:remaining]
    return [key for _, key in picks]


def select_coverage_delta(store: knowledge.KnowledgeStore, split: dict, baseline: dict, limit: int) -> list[str]:
    """Freeze newly covered C targets plus uncovered controls before inference."""
    rows = baseline.get("results", [])
    keys = [row["target_id"] for row in rows]
    if (baseline.get("mode") != "retrieval_audit" or baseline.get("source_leaks") != 0
            or baseline.get("snapshot_id") != split["snapshot_id"]
            or baseline.get("split_sha256") != split["split_sha256"]
            or len(keys) != len(set(keys)) or set(keys) != set(split["targets"])
            or any(set(row["reference_ids"]) & set(split["excluded"]) for row in rows)):
        raise ValueError("Coverage baseline must be a complete, leak-free audit of the same holdout")
    covered_before = {row["target_id"] for row in rows if row["reference_ids"]}
    gained, controls = [], []
    for key in split["targets"]:
        target = store.function(key)
        if not (target.get("source_path") or "").endswith(".c"):
            continue
        covered = bool(knowledge.retrieval_pack(store, key, excluded=set(split["excluded"]))["references"])
        if covered and key not in covered_before:
            gained.append((target["insns"], key))
        elif not covered:
            controls.append((target["insns"], key))
    gained.sort(); controls.sort()
    if not gained:
        raise ValueError("No newly covered C targets relative to the audit")
    if len(gained) >= limit or not controls:
        raise ValueError("Coverage-delta pilot must fit all gained C targets and at least one uncovered control")
    return [key for _, key in gained + controls[:min(3, limit - len(gained))]]


def paired_summary(results: list[dict]) -> dict:
    arms = {}
    for arm in ("off", "on"):
        rows = [row[arm] for row in results if arm in row]
        arms[arm] = {"attempted": len(rows), "outcomes": dict(Counter(r["status"] for r in rows)),
                     "compiled": sum(bool(r.get("compile_ok")) for r in rows),
                     "scorer_exact": sum(r["status"] == "scorer_exact" for r in rows)}
    scored = [row for row in results if all(isinstance(row.get(arm, {}).get("pct"), (int, float))
                                          for arm in ("off", "on"))]
    controls = [row for row in results if "off" in row and "on" in row and row.get("prompt_sha256", {}).get("off")
                and row["prompt_sha256"]["off"] == row["prompt_sha256"].get("on")]
    control_changes = sum(tuple(row["off"].get(key) for key in ("status", "pct", "compile_ok")) !=
                          tuple(row["on"].get(key) for key in ("status", "pct", "compile_ok")) for row in controls)
    return {"arms": arms, "paired_attempted": sum("off" in row and "on" in row for row in results),
            "preparation_errors": sum(bool(row.get("preparation_error")) for row in results),
            "paired_scored": len(scored),
            "score_wins": sum(row["on"]["pct"] > row["off"]["pct"] for row in scored),
            "score_losses": sum(row["on"]["pct"] < row["off"]["pct"] for row in scored),
            "score_ties": sum(row["on"]["pct"] == row["off"]["pct"] for row in scored),
            "new_compiles": sum(row.get("on", {}).get("compile_ok", False) and
                                not row.get("off", {}).get("compile_ok", False) for row in results),
            "lost_compiles": sum(row.get("off", {}).get("compile_ok", False) and
                                 not row.get("on", {}).get("compile_ok", False) for row in results),
            "identical_prompt_controls": len(controls), "control_outcome_changes": control_changes,
            "interpretation_warning": "Identical-prompt controls diverged; do not attribute all changes to retrieval."
                                      if control_changes else "Small coverage-enriched pilot, not independent project-wide validation.",
            "verified_gain": None}


def report_paired_run(folder: Path) -> dict:
    """Recompute summaries in a new report without rewriting historical run artifacts."""
    data = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    summary = data["summary"]
    results = data["results"]
    controls = [row["symbol"] for row in results if row.get("prompt_sha256", {}).get("off")
                and row["prompt_sha256"]["off"] == row["prompt_sha256"].get("on")]
    return {"run": str(folder.resolve()), "configuration_sha256": summary["configuration_sha256"],
            "snapshot_id": summary["snapshot_id"], "split_sha256": summary["split_sha256"],
            "prompt_version": summary.get("prompt_version", "minimal-v1"),
            "retrieval_version": summary.get("retrieval_version"),
            "declaration_context_version": summary.get("declaration_context_version", "assembly-symbol-dependencies-v1"),
            "coverage_baseline": summary.get("coverage_baseline"),
            "development_replay_of": summary.get("development_replay_of"),
            "finished": summary.get("finished"), "environment_unchanged": summary.get("environment_unchanged"),
            "target_count": len(results), "covered_targets": sum(bool(row.get("reference_ids")) for row in results),
            "controls": controls, **paired_summary(results),
            "pairs": [{"symbol": row["symbol"], "reference_count": len(row.get("reference_ids", [])),
                       **{arm: {key: row.get(arm, {}).get(key) for key in ("status", "pct", "compile_ok", "elapsed_seconds")}
                          for arm in ("off", "on")}} for row in results]}


@contextmanager
def model_lock(source_root: Path):
    state = source_root / "build/local_llm_campaign"
    for path in sorted((state / "runners").glob("*.lock")):
        with path.open("a+") as handle:
            try:
                portable.flock(handle, portable.LOCK_EX | portable.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(f"Campaign worker is using the model: {path.stem}") from error
            portable.flock(handle, portable.LOCK_UN)
    folder = state / "bench"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "run.lock").open("a+") as handle:
        portable.flock(handle, portable.LOCK_EX | portable.LOCK_NB)
        try:
            yield
        finally:
            portable.flock(handle, portable.LOCK_UN)


def run(store: knowledge.KnowledgeStore, split: dict, output: Path, host: str, model: str,
        limit: int = 12, tokens: int = 2048, timeout: int = 180, seed: int = 1234,
        selection_manifest: Path | None = None, prompt_version: str = "minimal-v1",
        coverage_baseline: Path | None = None, inline_contracts: bool = False) -> dict:
    import local_campaign as campaign
    import local_campaign_decompile_validate as validator

    validate_split(store, split)
    provenance = json.loads(store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?",
                                                    (split["snapshot_id"],)).fetchone()[0])
    root = Path(provenance["workspace"])
    version = provenance.get("version") or VERSION
    if root.resolve() != campaign.ROOT.resolve():
        raise ValueError("Configure DECOMP_ROOT to the frozen evidence workspace, not the live checkout")
    if fingerprint(root, version) != provenance["snapshot_sha256"]:
        raise ValueError("Frozen workspace changed before the paired pilot")
    if limit < 1 or tokens < 1 or timeout < 1:
        raise ValueError("Pilot limits must be positive")
    if selection_manifest and coverage_baseline:
        raise ValueError("Replay and coverage-delta selections are mutually exclusive")
    baseline_bytes = coverage_baseline.read_bytes() if coverage_baseline else None
    source_root = Path(provenance["source_root"])
    coordinator = Coordinator(source_root / "build/local_llm_campaign")
    manifest_file = output / "manifest.json"
    if manifest_file.exists():
        raise ValueError("Pilot output already exists; use a new directory to preserve history")
    output.mkdir(parents=True, exist_ok=True)
    configuration = {"schema_version": 1, "mode": "type_informed_body_holdout_paired_pilot",
                     "snapshot_id": split["snapshot_id"], "split_sha256": split["split_sha256"],
                     "workspace_sha256": provenance["snapshot_sha256"],
                     "model": model, "host": host, "seed": seed, "temperature": .15,
                     "max_tokens": tokens, "timeout": timeout, "attempts_per_arm": 1,
                     "selection": "coverage_enriched_C_targets_with_uncovered_controls_not_representative",
                     "coverage_baseline": {"path": str(coverage_baseline.resolve()),
                                           "sha256": knowledge.digest(baseline_bytes)} if coverage_baseline else None,
                     "producer_sha256": knowledge.digest(Path(__file__).read_bytes()),
                     "retriever_sha256": knowledge.digest(Path(knowledge.__file__).read_bytes()),
                     "retrieval_version": knowledge.RETRIEVAL_VERSION,
                     "prompt_version": prompt_version,
                     "declaration_context_version": "dependency-closed-inline-contracts-v2" if inline_contracts
                                                    else "assembly-symbol-dependencies-v1",
                     "development_replay_of": str(selection_manifest.resolve()) if selection_manifest else None,
                     "parser_tool": decompile.tool_metadata(),
                     "verification": knowledge.UNKNOWN_VERIFICATION.copy()}
    results, inputs = [], []
    config_hash = knowledge.digest(knowledge.encoded(configuration))

    def checkpoint(phase: str, finished: bool = False, **detail):
        summary = {**configuration, "configuration_sha256": config_hash,
                   "targets": [row["target_id"] for row in inputs], "total": len(inputs),
                   "processed": len(results), "finished": finished, "phase": phase,
                   "pid": os.getpid(), "updated_at": datetime.now(UTC).isoformat(),
                   **paired_summary(results), **detail}
        decompile.write_json(manifest_file, summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": results})
        print(json.dumps({key: summary[key] for key in ("phase", "total", "processed", "finished", "arms")}), flush=True)
        return summary

    with model_lock(source_root):
        checkpoint("preparing")
        commands = subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  check=True, timeout=60)
        command_lookup = command_map(commands.stdout)
        units = {unit["name"]: unit for unit in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
        environment = validator.environment_fingerprint(root, command_lookup, version)
        # Frozen target order and references are captured before any inference or score is observed.
        if selection_manifest:
            previous = json.loads(selection_manifest.read_text(encoding="utf-8"))
            if previous["split_sha256"] != split["split_sha256"]:
                raise ValueError("Replay selection belongs to a different holdout")
            targets = previous["targets"]
            if not targets or len(set(targets)) != len(targets) or not set(targets) <= set(split["targets"]):
                raise ValueError("Replay target selection is invalid")
        elif coverage_baseline:
            targets = select_coverage_delta(store, split, json.loads(baseline_bytes), limit)
            if coverage_baseline.read_bytes() != baseline_bytes:
                raise ValueError("Coverage baseline changed during selection")
        else:
            targets = select_targets(store, split, limit)
        if not targets:
            raise ValueError("No eligible C targets for the paired pilot")
        decompile.write_json(output / "selection.json", {"configuration": configuration, "targets": targets,
                             "excluded": split["excluded"], "split_sha256": split["split_sha256"]})
        sys.path.insert(0, str(decompile.tool().parent))
        for key in targets:
            target = store.function(key)
            folder = output / key
            folder.mkdir()
            item = {"target_id": key, "symbol": target["symbol"], "source": target["source_path"],
                    "unit": target["unit"], "address": target["address"], "module": target["module"]}
            try:
                assembly = extract_assembly(root, target, version)
                owner = (root / target["owner"]).read_text(encoding="utf-8")
                start, end = campaign.function_span(owner, target["symbol"])
                signature = campaign.function_signature(owner, start, end)
                command = command_lookup[target["source_path"]]
                context = decompile.preprocess(root, command, target["source_path"])
                if context["status"] != "ok":
                    raise ValueError("Cannot preprocess the frozen type context: " + context.get("error", "")[-500:])
                required = {target["symbol"], *corpus.CALL.findall(assembly), *corpus.DATA.findall(assembly)}
                declarations = declaration_context(context["context"], required, inline_contracts=inline_contracts)
                if len(declarations) > 24000:
                    raise ValueError("Dependency-closed type context exceeds the pilot budget")
                pack = knowledge.retrieval_pack(store, key, excluded=set(split["excluded"]))
                if set(split["excluded"]) & {ref["function_id"] for ref in pack["references"]}:
                    raise ValueError("Held-out reference source leaked into the paired pilot")
                prompts = prompt_pair(target, signature, assembly, declarations, pack, prompt_version, inline_contracts)
                for arm, prompt in prompts.items():
                    (folder / (arm + ".prompt.txt")).write_text(prompt, encoding="utf-8", newline="\n")
                (folder / "target.s").write_text(assembly, encoding="utf-8", newline="\n")
                (folder / "declarations.c").write_text(declarations, encoding="utf-8", newline="\n")
                item.update(signature=signature, prompt_sha256={arm: knowledge.digest(p) for arm, p in prompts.items()},
                            reference_ids=[ref["function_id"] for ref in pack["references"]],
                            reference_content_chars=pack["reference_content_chars"])
            except Exception as error:
                item["preparation_error"] = f"{type(error).__name__}: {error}"
            inputs.append(item)
        decompile.write_json(output / "inputs.json", {"configuration": configuration, "inputs": inputs})
        checkpoint("health_check")
        from local_campaign_bench import wait_for_model
        wait_for_model(host, model, 180)
        for index, item in enumerate(inputs):
            result = dict(item)
            if item.get("preparation_error"):
                results.append(result)
                checkpoint("preparation_error")
                continue
            # Alternate arm order to reduce cache/warmup and time-of-day bias.
            order = ("off", "on") if index % 2 == 0 else ("on", "off")
            pair_seed = seed + int(item["target_id"][:8], 16) % 100000
            for arm in order:
                folder = output / item["target_id"] / arm
                folder.mkdir()
                prompt = (folder.parent / (arm + ".prompt.txt")).read_text(encoding="utf-8")
                if knowledge.digest(prompt) != item["prompt_sha256"][arm]:
                    raise ValueError("Captured pilot prompt changed")
                checkpoint("inference", symbol=item["symbol"], arm=arm)
                started = time.monotonic()
                try:
                    response = campaign.ollama(host, model, prompt, timeout, tokens, lambda **kw: None,
                                               folder / "response.txt", temperature=.15, seed=pair_seed)
                    candidate = campaign.extract_function(response, item["symbol"])
                    first, last = campaign.function_span(candidate, item["symbol"])
                    if campaign.function_signature(candidate, first, last) != item["signature"]:
                        raise ValueError("Model changed the required function signature")
                    rejected, flags = campaign.policy("", candidate)
                    if rejected or flags:
                        raise ValueError("Source policy rejected candidate: " + ", ".join(rejected + flags))
                    (folder / "draft.c").write_text(candidate, encoding="utf-8", newline="\n")
                    checkpoint("compilation", symbol=item["symbol"], arm=arm)
                    row = {**item, "id": item["target_id"], "status": "ok", "artifacts": str(folder.resolve())}
                    with coordinator.build("Knowledge paired pilot", "MWCC/objdiff: " + item["symbol"]):
                        measured = validator.validate_one(root, row, command_lookup[item["source"]],
                                                          units[item["unit"]], "", folder / "validation",
                                                          environment=environment)
                    measured["model_timings"] = dict(campaign.LAST_TIMINGS)
                except Exception as error:
                    measured = {"status": "inference_or_policy_error", "compile_ok": False,
                                "error": f"{type(error).__name__}: {error}"}
                measured.update(elapsed_seconds=time.monotonic() - started, seed=pair_seed,
                                prompt_sha256=item["prompt_sha256"][arm])
                response_path = folder / "response.txt"
                measured["response_sha256"] = knowledge.digest(response_path.read_bytes()) if response_path.exists() else None
                decompile.write_json(folder / "result.json", measured)
                result[arm] = measured
                decompile.write_json(output / "current-pair.json", result)
            results.append(result)
            checkpoint("pair_complete")
        final_fingerprint = fingerprint(root, version)
        unchanged = (final_fingerprint == provenance["snapshot_sha256"]
                     and (not coverage_baseline or coverage_baseline.read_bytes() == baseline_bytes)
                     and knowledge.digest(Path(knowledge.__file__).read_bytes()) == configuration["retriever_sha256"]
                     and knowledge.digest(Path(__file__).read_bytes()) == configuration["producer_sha256"])
        summary = checkpoint("complete" if unchanged else "input_changed", finished=True,
                             environment_unchanged=unchanged, final_workspace_sha256=final_fingerprint)
        if not unchanged:
            raise ValueError("Frozen input set changed; paired results are not comparable")
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=knowledge.DEFAULT_DATABASE)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--selection-manifest", type=Path, help="replay frozen target IDs; results are development, not new validation")
    parser.add_argument("--coverage-baseline", type=Path, help="audit defining all newly covered C targets, plus uncovered controls")
    parser.add_argument("--inline-contracts", action="store_true", help="include existing inline helper prototypes, never their bodies")
    parser.add_argument("--prompt-version", choices=("minimal-v1", "c89-symbolic-relocations-target-last-v2"), default="minimal-v1")
    args = parser.parse_args()
    with knowledge.KnowledgeStore(args.database, readonly=True) as store:
        result = run(store, json.loads(args.split.read_text(encoding="utf-8")), args.output.resolve(), args.host, args.model,
                     args.limit, args.max_tokens, args.timeout, args.seed, args.selection_manifest, args.prompt_version,
                     args.coverage_baseline, args.inline_contracts)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

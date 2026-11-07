"""Recover frozen drafts using MWCC/objdiff feedback without exposing assigned C."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import secrets
import subprocess
import time

import local_campaign as campaign
import local_campaign_c89 as c89
import local_campaign_decompile as decompile
import local_campaign_decompile_validate as validator
import local_campaign_knowledge as knowledge
import local_campaign_knowledge_ablation as ablation
import local_campaign_permute as permute
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_knowledge_ablation import model_lock, prompt_pair


def native_inputs(prior: Path, data: dict, previous: dict, selection: dict,
                  inputs: list[dict]) -> dict:
    """Capture all native seeds before inference; never read the assigned C body."""
    config = data["configuration"]
    if not config.get("native_c"):
        raise ValueError("Native recovery requires an explicit native-C sweep")
    ids = [row["target_id"] for row in data["inputs"]]
    if (len(set(ids)) != len(ids) or ids != selection["targets"]
            or ids != previous["summary"]["targets"]
            or ids != [row["target_id"] for row in previous["results"]]
            or selection["configuration"] != config
            or selection["split_sha256"] != config["split_sha256"]):
        raise ValueError("Native sweep target selection or configuration changed")
    rows = {row["target_id"]: row for row in previous["results"]}
    captured = {}
    for item in inputs:
        key = item["target_id"]
        row = rows[key]
        if any(row.get(field) != item.get(field) for field in item):
            raise ValueError("Native result identity differs from frozen input")
        if item.get("preparation_error"):
            captured[key] = {}
            continue
        if not row.get("native_declarations_sha256") or not row.get("measurement", {}).get("draft_sha256"):
            if row.get("measurement", {}).get("status") != "generation_or_validation_error":
                raise ValueError("Native seed lacks captured declaration or draft hashes")
            captured[key] = {"seed_error": row["measurement"].get("error", "No native seed produced")}
            continue
        folder = prior / key

        def read(name, expected):
            content = (folder / name).read_text(encoding="utf-8")
            if not expected or knowledge.digest(content) != expected:
                raise ValueError(f"Native {name} changed for {item['symbol']}")
            return content

        assembly = read("target.s", item["assembly_sha256"])
        read("declarations.c", item["declarations_sha256"])
        declarations = read("native-declarations.c", row.get("native_declarations_sha256"))
        pack = json.loads((folder / "references.json").read_text(encoding="utf-8"))
        if (knowledge.digest(knowledge.encoded(pack)) != item["references_sha256"]
                or pack["target_id"] != key
                or [ref["function_id"] for ref in pack["references"]] != item["reference_ids"]
                or set(item["reference_ids"]) & set(selection["excluded"])):
            raise ValueError("Native matched references changed or contain an excluded target")
        for ref in pack["references"]:
            if knowledge.digest(ref["source_text"]) != ref["source_sha256"]:
                raise ValueError("Native reference source hash changed")
        measurement = row.get("measurement", {})
        seed = read("draft.c", measurement.get("draft_sha256"))
        verification = measurement.get("verification", {})
        if (verification.get("candidate_sha256") != knowledge.digest(seed)
                or verification.get("declarations_sha256") != knowledge.digest(declarations)):
            raise ValueError("Native seed/context differs from measured input")
        base = prompt_pair({"id": key}, item["signature"], assembly, declarations, pack,
                           prompt_version="c89-symbolic-relocations-target-last-v2", revision_context=True)["on"]
        captured[key] = {"declarations": declarations, "assembly": assembly,
                         "base_prompt": base, "references": pack, "seed": seed}
    return captured


def rank(result: dict) -> tuple:
    pct = result.get("pct")
    return (bool(result.get("compile_ok")), pct if isinstance(pct, (int, float)) else -1,
            result.get("verification", {}).get("policy_screen_passed") is True)


def object_verified(result: dict) -> bool:
    return bool(result.get("compile_ok")) and all(result.get("verification", {}).get(axis) is True for axis in
                                                 ("policy_screen_passed", "raw_candidate_bytes", "relocations", "owner_object"))


def declaration_candidates(candidate: str, block: tuple[int, int, list[str]], cap: int, seed: int):
    """Generate bounded recipes against one fixed base, not a changing best draft."""
    start, end, lines = block
    if candidate[start:end] != "".join(lines) or len(set(lines)) != len(lines):
        raise ValueError("Declaration block does not bind uniquely to the base candidate")
    for order in permute.orderings(lines, cap=cap, seed=seed):
        yield (candidate[:start] + "".join(order) + candidate[end:],
               {"version": "uninitialized-scalar-declaration-order-v2",
                "base_candidate_sha256": knowledge.digest(candidate), "block_start": start,
                "block_end": end, "original_block_sha256": knowledge.digest("".join(lines)),
                "order": [lines.index(line) for line in order], "random_seed": seed})


def compiler_feedback(result: dict, symbol: str, budget: int = 7000) -> str:
    """Only candidate diagnostics reach the model, never baseline owner source."""
    if result.get("status") == "baseline_compile_error":
        return "The unchanged owner baseline cannot compile. This is an environment problem, not candidate feedback."
    if not result.get("compile_ok"):
        return "MWCC/policy failure:\n" + (result.get("error") or "Candidate did not compile")[-budget:]
    parts = [f"MWCC compiled this candidate. Objdiff score: {result.get('pct')}%.\n"
             f"Target size: {result.get('target_size')}; candidate size: {result.get('candidate_size')}.\n"
             "Aligned instructions below are target | candidate. <gap> means no instruction.\n"]
    if result.get("verification", {}).get("policy_screen_passed") is False:
        parts.append("Source-screening failure: " + result["verification"]["errors"].get("policy", "") + "\n")
    relocations = result.get("verification", {}).get("target_records", {}).get("retail", {}).get("relocations", [])
    if relocations:
        parts.append("Retail ELF relocations (authoritative symbolic operands):\n")
        for _, offset, kind, addend, identity in relocations:
            line = f"instruction byte offset +0x{offset:x}: type {kind}, symbol identity {json.dumps(identity)}, addend {addend}\n"
            if sum(map(len, parts)) + len(line) > budget // 3:
                parts.append("[Remaining relocation records omitted.]\n")
                break
            parts.append(line)
        parts.append("Use these symbols as bases; encoded relocation displacements are not C pointer offsets.\n"
                     "Memory operands AFTER the resolved base still have real byte offsets.\n")
    path = Path(result["artifacts"]) / "objdiff.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        pair = [next((s for s in data.get(side, {}).get("symbols", []) if s.get("name") == symbol), {})
                for side in ("left", "right")]
        rows = campaign.instruction_rows(*pair)
        different = [i for i, (want, have, _) in enumerate(rows) if want != have]
        indices = sorted({j for i in different for j in range(max(0, i - 1), min(len(rows), i + 2))})
        used = sum(map(len, parts))
        for index in indices:
            want, have, _ = rows[index]
            line = f"{index:4}: {want or '<gap>'} | {have or '<gap>'}\n"
            if used + len(line) > budget:
                parts.append("[Additional aligned differences omitted; full target assembly is above.]\n")
                break
            parts.append(line)
            used += len(line)
    return "".join(parts)


def edit_schema(candidate: str, symbol: str) -> dict:
    """Only unique body anchors are decoding choices; validity still needs MWCC."""
    first, last = campaign.function_span(candidate, symbol)
    masked = campaign.source_mask(candidate)
    body, closing = masked.index("{", first, last) + 1, masked.rindex("}", first, last)
    lines = candidate[body:closing].splitlines(keepends=True)
    anchors = []
    for width in (1, 2, 3):
        for start in range(len(lines) - width + 1):
            anchor = "".join(lines[start:start + width])
            code = campaign.source_mask(anchor).strip()
            if (not code or code in ("{", "}") or len(anchor) > 512
                    or candidate.count(anchor) != 1 or anchor in anchors):
                continue
            anchors.append(anchor)
    if not anchors:
        raise ValueError("No unique bounded body anchors available for constrained edits")
    return {"type": "object", "properties": {"edits": {"type": "array", "minItems": 1, "maxItems": 1,
             "items": {"type": "object", "properties": {
                 "old": {"type": "string", "enum": anchors}, "new": {"type": "string", "maxLength": 512}},
                 "required": ["old", "new"], "additionalProperties": False}}},
            "required": ["edits"], "additionalProperties": False}


def schema_format(schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": "decomp_edit", "strict": True, "schema": schema}}


def schema_probe(host: str, model: str, folder: Path) -> dict:
    """Verify real constraint handling before spending target inference requests."""
    folder.mkdir()
    nonce = secrets.token_hex(16)
    expected = {"probe": nonce}
    response_format = schema_format({"type": "object", "properties": {"probe": {"const": nonce}},
                                    "required": ["probe"], "additionalProperties": False})
    prompt = "Return exactly the plain text NOT_JSON, without JSON or extra text."
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
    decompile.write_json(folder / "response-format.json", response_format)
    response = campaign.ollama(host, model, prompt, 60, 128, lambda **unused: None,
                              folder / "response.txt", temperature=0, seed=1234, response_format=response_format)
    try:
        passed = json.loads(response) == expected
    except json.JSONDecodeError:
        passed = False
    result = {"passed": passed, "scope": "single_nonce_constraint_probe_not_target_correctness",
              "prompt_sha256": knowledge.digest(prompt), "response_sha256": knowledge.digest(response),
              "response_format_sha256": knowledge.digest((folder / "response-format.json").read_bytes()),
              "model_timings": dict(campaign.LAST_TIMINGS)}
    decompile.write_json(folder / "result.json", result)
    if not passed:
        raise ValueError("Server did not enforce the nonce schema; target requests are disabled")
    return result


def apply_edits(candidate: str, response: str, symbol: str, *, schema: dict | None = None) -> str:
    """Apply bounded, unique, non-overlapping anchors against the same seed body."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON edit key")
            result[key] = value
        return result

    text = response.strip()
    if text.startswith("```"):
        fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
        if not fenced:
            raise ValueError("Expected one JSON edit object, not a function or commentary")
        text = fenced[1]
    data = json.loads(text, object_pairs_hook=unique_keys)
    if not isinstance(data, dict) or set(data) != {"edits"} or not isinstance(data["edits"], list):
        raise ValueError("Expected an edits array")
    if not 1 <= len(data["edits"]) <= 4:
        raise ValueError("Supply one to four focused edits")
    if schema is not None and len(data["edits"]) != 1:
        raise ValueError("Schema mode permits exactly one edit")
    first, last = campaign.function_span(candidate, symbol)
    masked = campaign.source_mask(candidate)
    body = masked.index("{", first, last) + 1
    closing = masked.rindex("}", first, last)
    replacements = []
    for edit in data["edits"]:
        if (not isinstance(edit, dict) or set(edit) != {"old", "new"}
                or not all(isinstance(edit[key], str) for key in ("old", "new"))):
            raise ValueError("Each edit requires exactly old and new strings")
        old, new = edit["old"], edit["new"]
        if schema is not None:
            properties = schema["properties"]["edits"]["items"]["properties"]
            if old not in properties["old"]["enum"] or len(new) > properties["new"]["maxLength"]:
                raise ValueError("Response violates the captured anchor schema")
        if not old or old == new or max(len(old), len(new)) > 2000:
            raise ValueError("Edit is empty, unchanged or exceeds the focused edit budget")
        if candidate.count(old) != 1:
            raise ValueError("Edit anchor must occur exactly once in the current candidate")
        start, end = candidate.index(old), candidate.index(old) + len(old)
        if not body <= start < end <= closing:
            raise ValueError("Edits must stay inside the function body, preserving its signature")
        replacements.append((start, end, new))
    replacements.sort()
    if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:])):
        raise ValueError("Edit anchors overlap")
    for start, end, new in reversed(replacements):
        candidate = candidate[:start] + new + candidate[end:]
    return candidate


def recovery_prompt(base: str, candidate: str, feedback: str, previous_failure: str = "",
                    revision_protocol: str = "function") -> str:
    prompt = (base + "\n## Revision constraints\n"
            "Use C89: declare locals at the start of each block. No for-loop declarations.\n"
            "Linker operands name symbols: do not add their encoded displacement to an address.\n"
            "Register/LR saves are compiler bookkeeping, not source operations. No compiler builtins.\n"
            "The draft below is a generated attempt, NOT the assigned implementation.\n"
            "Make a focused revision from the measured feedback; preserve the required signature and behavior.\n"
            "Do not invent types, fields or undefined identifiers to hide a compiler error.\n"
            f"\n## Current generated candidate\n```c\n{candidate}\n```\n"
            f"\n## Measured feedback\n```text\n{feedback}\n```\n"
            + ("\n## Last rejected or regressing revision\n"
               "The current candidate above was retained. Avoid repeating this unsuccessful revision:\n"
               + previous_failure + "\n" if previous_failure else ""))
    if revision_protocol in ("edits-v1", "schema-edits-v1"):
        limits = ("Supply exactly one focused edit, at most 512 characters per string.\n"
                  if revision_protocol == "schema-edits-v1" else
                  "Supply one to four focused edits, each at most 2000 characters per string.\n")
        prompt += ("\n## Focused edit response contract\n"
                   "Return only one JSON object, not a complete function:\n"
                   '{"edits": [{"old": "exact current code", "new": "replacement code"}]}\n'
                   + limits +
                   "Each old string must match exactly once in the current generated candidate, including whitespace.\n"
                   "Anchors must be non-overlapping and all refer to the current candidate, not a previous edit.\n"
                   "Edit only inside its body. Preserve the signature, outer braces and every unedited byte.\n"
                   "Do not return a whole function, copied reference, new declarations unrelated to the difference,\n"
                   "or hundreds of numbered temporaries. JSON strings must escape embedded newlines.\n")
        if revision_protocol == "schema-edits-v1":
            prompt += ("The server schema constrains this request to exactly ONE edit.\n"
                       "Copy one unique passage of one to three body lines (at most 512 characters) as old.\n"
                       "Keep new at most 512 characters. Change only the highest-value measured difference.\n")
    else:
        prompt += ("\n## Revision response contract\n"
                   "Return only the complete revised function in one C code block.\n"
                   "Keep unmodified code as it is; do not expand the assembly into new numbered temporaries.\n")
    return prompt


def object_evidence(root: Path, item: dict, candidate: str, declarations: str,
                    result: dict, unit: dict) -> dict:
    """Fail-closed evidence from the project's tested ELF and source-policy tools."""
    evidence = {**knowledge.UNKNOWN_VERIFICATION, "ready": False, "errors": {}}
    evidence["candidate_sha256"] = knowledge.digest(candidate)
    evidence["declarations_sha256"] = knowledge.digest(declarations)
    from permuter import owner_extract, owner_source

    evidence["verifiers"] = {module.__name__: {"path": module.__file__,
                            "sha256": knowledge.digest(Path(module.__file__).read_bytes())}
                            for module in (owner_extract, owner_source)}
    try:
        if re.search(r"\b(?:M2C_\w+|saved_reg_\w+)\b", candidate):
            raise ValueError("Unresolved m2c helpers are not ready-to-use project C")
        first, last = campaign.function_span(candidate, item["symbol"])
        owner_source.validate_definition_only(candidate, first, last)
        if campaign.function_signature(candidate, first, last) != item["signature"]:
            raise ValueError("Required signature changed")
        rejected, flags = campaign.policy("", candidate)
        if rejected or flags or re.search(r"\b__builtin_\w+", campaign.source_mask(candidate)):
            raise ValueError("Source screening rejected: " + ", ".join(rejected + flags))
        evidence["policy_screen_passed"] = True
        evidence["policy_scope"] = "campaign_screen_and_single_definition_not_project_review"
    except (OSError, ValueError, RuntimeError) as error:
        evidence["policy_screen_passed"] = False
        evidence["errors"]["policy"] = str(error)
    if not result.get("compile_ok"):
        return evidence
    if result.get("draft_sha256") != evidence["candidate_sha256"]:
        evidence["errors"]["input_binding"] = "Measured draft differs from the supplied candidate"
        return evidence
    try:
        path = Path(result["compile_output"])
        generated = owner_extract.read_elf(path)
        target = owner_extract.read_elf(root / unit["target_path"])
        ours = owner_extract.find_target(generated, item["symbol"])
        retail = owner_extract.find_target(target, item["symbol"])
        records = {"candidate": owner_extract.target_record(generated, ours),
                   "retail": owner_extract.target_record(target, retail)}
        evidence["target_records"] = records
        evidence["objects"] = {"candidate": {"path": str(path), "sha256": knowledge.digest(path.read_bytes())},
                               "retail": {"path": str(target.path), "sha256": knowledge.digest(target.path.read_bytes())}}
        evidence["raw_candidate_bytes"] = (records["candidate"]["size"] == records["retail"]["size"] and
                                            records["candidate"]["bytes_sha256"] == records["retail"]["bytes_sha256"])
        evidence["relocations"] = records["candidate"]["relocations"] == records["retail"]["relocations"]
    except (OSError, ValueError, RuntimeError) as error:
        evidence["errors"]["target_object"] = str(error)
    try:
        baseline = result.get("baseline", {})
        if not baseline.get("compile_ok") or result.get("scope") != "full_owner_scoring_unit":
            raise ValueError("No successful full-owner baseline for sibling audit")
        base = owner_extract.read_elf(Path(baseline["object"]))
        generated = owner_extract.read_elf(Path(result["compile_output"]))
        evidence["sibling_audit"] = owner_extract.audit_siblings(
            base, generated, owner_extract.find_target(base, item["symbol"]),
            owner_extract.find_target(generated, item["symbol"]))
        evidence["owner_object"] = True
    except (OSError, ValueError, RuntimeError) as error:
        evidence["owner_object"] = False
        evidence["errors"]["owner_object"] = str(error)
    # Link checksum and human review must come from their actual gates, never a scratch score.
    return evidence


def run(prior: Path, output: Path, host: str, model: str, attempts: int = 2,
        tokens: int = 2048, timeout: int = 180, symbols: set[str] | None = None,
        database: Path = knowledge.DEFAULT_DATABASE, revision_protocol: str = "function",
        declaration_orderings: int = 0) -> dict:
    data = json.loads((prior / "inputs.json").read_text(encoding="utf-8"))
    previous = json.loads((prior / "results.json").read_text(encoding="utf-8"))
    config = data["configuration"]
    root = campaign.ROOT
    native = config.get("mode") == "actual_unmatched_project_m2c_baseline"
    if not previous["summary"].get("finished") or not previous["summary"].get("environment_unchanged"):
        raise ValueError("Recovery requires a completed, unchanged prior run")
    if knowledge.digest(knowledge.encoded(config)) != previous["summary"]["configuration_sha256"]:
        raise ValueError("Original paired configuration changed")
    if attempts < 0 or tokens < 1 or timeout < 1:
        raise ValueError("Invalid recovery limits")
    if not 0 <= declaration_orderings <= 720:
        raise ValueError("Declaration-order budget must be between zero and 720 per target")
    if revision_protocol not in ("function", "edits-v1", "schema-edits-v1"):
        raise ValueError("Unsupported revision protocol")
    if revision_protocol == "schema-edits-v1" and not host.rstrip("/").endswith("/v1"):
        raise ValueError("Schema edits require the compatible /v1 endpoint; no unconstrained fallback")
    if output.exists():
        raise ValueError("Use a new recovery output directory; historical runs are immutable")
    if fingerprint(root) != config["workspace_sha256"]:
        raise ValueError("DECOMP_ROOT must be the unchanged frozen workspace")
    with knowledge.KnowledgeStore(database, readonly=True) as store:
        snapshot = store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?",
                                            (config["snapshot_id"],)).fetchone()
        if snapshot is None:
            raise ValueError("Unknown knowledge snapshot")
        provenance = json.loads(snapshot[0])
        if native:
            from local_campaign_knowledge_sweep import validate_selection
            cohort = json.loads((prior / "cohort.json").read_text(encoding="utf-8"))
            validate_selection(store, cohort)
            if config["split_sha256"] != cohort["split_sha256"]:
                raise ValueError("Native configuration belongs to a different cohort")
    if Path(provenance["workspace"]).resolve() != root.resolve():
        raise ValueError("Knowledge snapshot belongs to a different workspace")
    coordinator = Coordinator(Path(provenance["source_root"]) / "build/local_llm_campaign")
    selection = json.loads((prior / "selection.json").read_text(encoding="utf-8"))
    if [item["target_id"] for item in data["inputs"]] != selection["targets"]:
        raise ValueError("Frozen target selection changed")
    if native and selection["excluded"] != cohort["excluded"]:
        raise ValueError("Native reference exclusions differ from the frozen cohort")
    if symbols and not symbols <= {item["symbol"] for item in data["inputs"]}:
        raise ValueError("Unknown requested target symbol")
    inputs = [item for item in data["inputs"] if not symbols or item["symbol"] in symbols]
    captured = native_inputs(prior, data, previous, selection, inputs) if native else {}
    bound_paths = [prior / name for name in ("inputs.json", "results.json", "selection.json")]
    if native:
        bound_paths.append(prior / "cohort.json")
        for item in inputs:
            if captured[item["target_id"]].get("seed") is not None:
                bound_paths.extend(prior / item["target_id"] / name for name in
                                   ("target.s", "declarations.c", "native-declarations.c", "references.json", "draft.c"))
    prior_bindings = {str(path.resolve()): knowledge.digest(path.read_bytes()) for path in bound_paths}
    tool_bindings = {module.__file__: knowledge.digest(Path(module.__file__).read_bytes())
                     for module in (campaign, c89, decompile, validator, knowledge, ablation, permute)}
    tool_bindings[__file__] = knowledge.digest(Path(__file__).read_bytes())
    output.mkdir(parents=True)
    configuration = {"schema_version": 1, "mode": ("actual_unmatched_native_development_recovery" if native else
                                                     "held_out_development_recovery_not_independent_validation"),
                     "prior": str(prior.resolve()), "prior_configuration_sha256": previous["summary"]["configuration_sha256"],
                     "snapshot_id": config["snapshot_id"], "workspace_sha256": config["workspace_sha256"],
                     "model": model, "host": host, "max_model_attempts": attempts, "max_tokens": tokens,
                     "timeout": timeout, "targets": [item["target_id"] for item in inputs],
                     "revision_protocol": revision_protocol,
                     "declaration_orderings": declaration_orderings,
                     "declaration_order_version": "uninitialized-scalar-declaration-order-v2" if declaration_orderings else None,
                     "response_format_version": "nested-schema-envelope-v1" if revision_protocol == "schema-edits-v1" else None,
                     "prompt_version": "native-revision-context-single-contract-v3" if native else "paired-recovery-v2",
                     "prior_total": len(data["inputs"]), "native_c": native,
                     "prior_artifact_sha256": prior_bindings, "attested_tools": tool_bindings,
                     "notice": "Development experiment; selected targets are not a full-project recovery rate.",
                     "producer_sha256": knowledge.digest(Path(__file__).read_bytes()),
                     "repair_sha256": knowledge.digest(Path(c89.__file__).read_bytes()),
                     "m2c_tool": decompile.tool_metadata()}
    decompile.write_json(output / "selection.json", {"configuration": configuration,
                         "targets": configuration["targets"], "excluded": selection["excluded"]})
    # Freeze every selected input before the first model request, including uncovered controls.
    for item in inputs:
        folder = output / item["target_id"]
        folder.mkdir()
        if native and captured[item["target_id"]].get("seed") is not None:
            capture = captured[item["target_id"]]
            for name, field in (("base.prompt.txt", "base_prompt"), ("target.s", "assembly"),
                                ("declarations.c", "declarations"), ("seed.c", "seed")):
                (folder / name).write_text(capture[field], encoding="utf-8", newline="\n")
            decompile.write_json(folder / "references.json", capture["references"])
    decompile.write_json(output / "inputs.json", {"configuration": configuration, "inputs": inputs})
    results = []
    current = {}
    lookup, environment = {}, ""
    model_requests = 0
    probe = None
    probe_requests = 0

    def checkpoint(phase, finished=False):
        bests = [row.get("best", {}) for row in results]
        summary = {**configuration, "phase": phase, "finished": finished, "processed": len(results),
                   "total": len(inputs), "current": current, "updated_at": datetime.now(UTC).isoformat(),
                   "counts": dict(Counter(row.get("status", "missing") for row in bests)),
                   "compiled": sum(bool(row.get("compile_ok")) for row in bests),
                   "scorer_exact": sum(row.get("pct") == 100 for row in bests),
                   "object_verified": sum(all(row.get("verification", {}).get(axis) is True for axis in
                                              ("raw_candidate_bytes", "relocations", "owner_object"))
                                          for row in bests),
                   "covered_targets": sum(bool(row.get("reference_ids")) for row in results),
                   "model_requests": model_requests,
                   "declaration_attempts": sum(len(row.get("declaration_search", {}).get("attempts", [])) for row in results),
                   "schema_probe": probe, "schema_probe_requests": probe_requests,
                   "baseline_already_exact": sum(row.get("baseline_measurement", {}).get("pct") == 100 for row in results),
                   "actual_unmatched_scorer_gains": sum(native and
                       isinstance(row.get("baseline_measurement", {}).get("pct"), (int, float)) and
                       row["baseline_measurement"]["pct"] < 100 and row.get("best", {}).get("pct") == 100
                       for row in results),
                   "ready": 0, "promoted": 0}
        if finished:
            summary["environment_unchanged"] = (phase != "failed" and fingerprint(root) == config["workspace_sha256"] and
                validator.environment_fingerprint(root, lookup) == environment and
                all(Path(path).is_file() and knowledge.digest(Path(path).read_bytes()) == sha
                    for path, sha in {**prior_bindings, **tool_bindings}.items()) and
                decompile.tool_metadata() == configuration["m2c_tool"])
        decompile.write_json(output / "manifest.json", summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": results})
        print(json.dumps({key: summary[key] for key in ("phase", "processed", "total", "scorer_exact", "object_verified", "current")}), flush=True)
        return summary

    @contextmanager
    def terminal_checkpoint():
        try:
            yield
        except Exception as error:
            current.update(error=f"{type(error).__name__}: {error}")
            checkpoint("failed", finished=True)
            raise

    with terminal_checkpoint(), model_lock(Path(provenance["source_root"])):
        commands = subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  check=True, timeout=60)
        lookup = command_map(commands.stdout)
        units = {unit["name"]: unit for unit in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
        environment = validator.environment_fingerprint(root, lookup)
        if native and environment != config["environment_sha256"]:
            raise ValueError("Native compiler environment changed")
        checkpoint("preparing")
        if attempts:
            from local_campaign_bench import wait_for_model
            wait_for_model(host, model, 180)
            if revision_protocol == "schema-edits-v1":
                probe_requests += 1
                checkpoint("schema_probe")
                probe = schema_probe(host, model, output / "schema-probe")
        for item in inputs:
            current = {"symbol": item["symbol"]}
            folder = output / item["target_id"]
            original = prior / item["target_id"]
            records = []
            best = None
            best_candidate = ""
            result = {**item, "attempts": records, "model_attempts": []}
            seed_error = captured.get(item["target_id"], {}).get("seed_error")
            if item.get("preparation_error") or seed_error:
                result["best"] = {"status": "preparation_error", "error": item.get("preparation_error") or seed_error}
                results.append(result)
                decompile.write_json(folder / "result.json", result)
                checkpoint("target_complete")
                continue
            capture = captured.get(item["target_id"], {})
            declarations = capture["declarations"] if native else (original / "declarations.c").read_text(encoding="utf-8")
            assembly = capture["assembly"].encode() if native else (original / "target.s").read_bytes()
            base_prompt = capture["base_prompt"] if native else (original / "on.prompt.txt").read_text(encoding="utf-8")
            if not native and knowledge.digest(base_prompt) != item["prompt_sha256"]["on"]:
                raise ValueError("Original held-out prompt changed")
            if (f"```c\n{declarations}\n```" not in base_prompt or
                    f"```asm\n{assembly.decode()}\n```" not in base_prompt):
                raise ValueError("Frozen assembly or declarations differ from captured prompt")
            if set(item.get("reference_ids", [])) & set(selection["excluded"]):
                raise ValueError("Held-out reference leaked into recovery")
            (folder / "base.prompt.txt").write_text(base_prompt, encoding="utf-8", newline="\n")
            (folder / "target.s").write_bytes(assembly)
            (folder / "declarations.c").write_text(declarations, encoding="utf-8", newline="\n")

            def measure(candidate, origin, repair=True, transformation=None):
                nonlocal best, best_candidate
                attempt_folder = folder / f"attempt-{len(records):03}"
                attempt_folder.mkdir()
                original_candidate = candidate
                original_hash = knowledge.digest(candidate)
                repair_count, repair_error = 0, None
                signature_error = None
                try:
                    first, last = campaign.function_span(candidate, item["symbol"])
                    if campaign.function_signature(candidate, first, last) != item["signature"]:
                        candidate = c89.bind_signature(candidate, declarations, item["signature"], item["symbol"],
                                                       primitive_aliases=native)
                except Exception as error:
                    signature_error = f"{type(error).__name__}: {error}"
                if repair:
                    try:
                        candidate, repair_count = c89.repair_declarations(candidate, declarations, item["symbol"])
                    except Exception as error:
                        repair_error = f"{type(error).__name__}: {error}"
                (attempt_folder / "draft.c").write_text(candidate, encoding="utf-8", newline="\n")
                (attempt_folder / "unrepaired.c").write_text(original_candidate, encoding="utf-8", newline="\n")
                current.update(origin=origin, attempt=len(records))
                checkpoint("compilation")
                try:
                    first, last = campaign.function_span(candidate, item["symbol"])
                    if campaign.function_signature(candidate, first, last) != item["signature"]:
                        raise ValueError("Required signature changed")
                    rejected, flags = campaign.policy("", candidate)
                    if rejected or flags:
                        raise ValueError("Source policy: " + ", ".join(rejected + flags))
                    with coordinator.build("Frozen draft recovery", "MWCC/objdiff: " + item["symbol"]):
                        measured = validator.validate_one(root, {**item, "id": item["target_id"], "status": "ok",
                                                          "artifacts": str(attempt_folder)}, lookup[item["source"]],
                                                          units[item["unit"]], "", folder / "validation" / attempt_folder.name,
                                                          environment=environment,
                                                          baseline_root=Path(config["baseline_cache_root"])
                                                          if native and config.get("baseline_cache_root") else None)
                    measured["verification"] = object_evidence(root, item, candidate, declarations, measured, units[item["unit"]])
                except (OSError, ValueError, RuntimeError) as error:
                    measured = {"status": "policy_or_validation_error", "compile_ok": False, "error": str(error)}
                measured.update(origin=origin, original_candidate_sha256=original_hash,
                                candidate_sha256=knowledge.digest(candidate), c89_repairs=repair_count,
                                c89_repair_error=repair_error, signature_binding_error=signature_error,
                                candidate_path=str((attempt_folder / "draft.c").resolve()))
                if transformation is not None:
                    measured["transformation"] = transformation
                records.append(measured)
                if best is None or rank(measured) > rank(best):
                    best, best_candidate = measured, candidate
                decompile.write_json(attempt_folder / "result.json", measured)
                return measured

            if native:
                measure(capture["seed"], "frozen_native_m2c", repair=False)
            for arm in (() if native else ("off", "on")):
                draft_path = original / arm / "draft.c"
                prior_record = next(row for row in previous["results"] if row["target_id"] == item["target_id"]).get(arm, {})
                if draft_path.is_file() and knowledge.digest(draft_path.read_bytes()) == prior_record.get("draft_sha256"):
                    raw_candidate = draft_path.read_text(encoding="utf-8")
                    measure(raw_candidate, "frozen_model_" + arm)
            translated = decompile.translate(root, item["symbol"], assembly, str(folder / "target.s"),
                                              {"status": "ok", "context": declarations}, output / "m2c-cache",
                                              configuration["m2c_tool"], explicit_fields=native)
            result["m2c"] = {key: translated.get(key) for key in ("status", "diagnostics", "artifacts", "error")}
            hypothesis = decompile.prompt_context(translated, budget=6000)
            if native and best_candidate:
                hypothesis = ""
                result["m2c_hypothesis_omitted"] = "Measured native candidate already supplied; avoid duplicate speculative guidance"
            if re.search(r"\b(?:M2C_\w+|saved_reg_\w+)\b", translated.get("draft", "")):
                hypothesis = ""
                result["m2c_hypothesis_omitted"] = "Unresolved helpers/types; diagnostics retained, not model guidance"
            if translated["status"] == "ok":
                try:
                    raw_candidate = campaign.extract_function(translated["draft"], item["symbol"])
                    measure(raw_candidate, "m2c_declaration_only")
                except ValueError as error:
                    result["m2c_candidate_error"] = str(error)
            if declaration_orderings:
                search = {"budget": declaration_orderings, "attempts": [],
                          "base_candidate_sha256": knowledge.digest(best_candidate),
                          "base_pct": best.get("pct") if best else None}
                result["declaration_search"] = search
                if not best or not best.get("compile_ok") or not best.get("verification", {}).get("policy_screen_passed"):
                    search.update(status="skipped", reason="No compiling source-screened base candidate")
                elif best.get("verification", {}).get("owner_object") is not True:
                    search.update(status="skipped", reason="Base has not passed the owner/sibling audit")
                elif object_verified(best):
                    search.update(status="skipped", reason="Base already passes target object checks")
                else:
                    try:
                        base_candidate = best_candidate
                        block = c89.declaration_order_block(base_candidate, declarations, item["symbol"])
                        if block is None:
                            search.update(status="skipped", reason="Fewer than two leading supported declarations")
                        else:
                            search.update(status="searching", local_count=len(block[2]))
                            seed = int(item["target_id"][:8], 16)
                            for candidate, recipe in declaration_candidates(base_candidate, block, declaration_orderings, seed):
                                current.update(declaration_attempt=len(search["attempts"]) + 1,
                                               declaration_budget=declaration_orderings)
                                measured = measure(candidate, "scripted_declaration_order", repair=False, transformation=recipe)
                                search["attempts"].append({"candidate_path": measured["candidate_path"],
                                                           "candidate_sha256": measured["candidate_sha256"],
                                                           "status": measured["status"], "pct": measured.get("pct"),
                                                           "object_verified": object_verified(measured)})
                                if object_verified(measured):
                                    break
                            search.update(status="complete", best_pct=best.get("pct"),
                                          object_verified=object_verified(best))
                    except (OSError, ValueError, RuntimeError) as error:
                        search.update(status="error", error=f"{type(error).__name__}: {error}")
            previous_failure = ""
            for index in range(attempts):
                if best and object_verified(best):
                    break
                attempt_folder = folder / f"model-{index:03}"
                attempt_folder.mkdir()
                feedback = compiler_feedback(best, item["symbol"]) if best else "No usable candidate yet."
                protocol = revision_protocol if best_candidate else "function"
                revision_base = best_candidate
                prompt = recovery_prompt(base_prompt + hypothesis, best_candidate, feedback, previous_failure, protocol)
                (attempt_folder / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
                schema = edit_schema(revision_base, item["symbol"]) if protocol == "schema-edits-v1" else None
                response_format = schema_format(schema) if schema else None
                if response_format:
                    decompile.write_json(attempt_folder / "response-format.json", response_format)
                current.update(model_attempt=index)
                model_requests += 1
                checkpoint("inference")
                started = time.monotonic()

                def inference_progress(**values):
                    current.update(response_chars=values.get("response_chars"),
                                   response_chunks=values.get("response_chunks"))
                    if values.get("done") or time.monotonic() - inference_progress.updated >= 10:
                        checkpoint("inference")
                        inference_progress.updated = time.monotonic()

                inference_progress.updated = started
                try:
                    response = campaign.ollama(host, model, prompt, timeout, tokens, inference_progress,
                                               attempt_folder / "response.txt", temperature=.15,
                                               seed=1234 + index + int(item["target_id"][:8], 16) % 100000,
                                               **({"response_format": response_format} if response_format else {}))
                    raw_candidate = (apply_edits(revision_base, response, item["symbol"], schema=schema) if protocol != "function" else
                                     campaign.extract_function(response, item["symbol"]))
                    measured = measure(raw_candidate, "compiler_feedback_model")
                    measured.update(prompt_sha256=knowledge.digest(prompt), response_sha256=knowledge.digest(response),
                                    model_timings=dict(campaign.LAST_TIMINGS), elapsed_seconds=time.monotonic() - started)
                except Exception as error:
                    measured = {"status": "inference_error", "compile_ok": False, "error": str(error),
                                "prompt_sha256": knowledge.digest(prompt), "elapsed_seconds": time.monotonic() - started}
                response_path = attempt_folder / "response.txt"
                measured.update(revision_protocol=protocol, revision_base_sha256=knowledge.digest(revision_base),
                                response_format_sha256=knowledge.digest((attempt_folder / "response-format.json").read_bytes())
                                if response_format else None,
                                model_timings=dict(campaign.LAST_TIMINGS))
                measured["response_sha256"] = knowledge.digest(response_path.read_bytes()) if response_path.exists() else None
                decompile.write_json(attempt_folder / "result.json", measured)
                result["model_attempts"].append({"artifacts": str(attempt_folder.resolve()), **measured})
                if best is not measured:
                    revision = Path(measured["candidate_path"]).read_text(encoding="utf-8") if measured.get("candidate_path") else ""
                    previous_failure = (f"Outcome: {measured['status']}; score: {measured.get('pct')}.\n"
                                        + (measured.get("error") or "This revision did not improve the retained score.")[-2000:]
                                        + (f"\n```c\n{revision}\n```" if revision and len(revision) <= 2000 else ""))
            result["best"] = best or {"status": "no_candidate"}
            if best_candidate:
                (folder / "review.c").write_text(best_candidate, encoding="utf-8", newline="\n")
                decompile.write_json(folder / "review.json", {"candidate_sha256": knowledge.digest(best_candidate),
                                     "measurement": result["best"], "ready": False,
                                     "required_remaining_gates": [axis for axis in knowledge.UNKNOWN_VERIFICATION
                                                                  if result["best"].get("verification", {}).get(axis) is not True]})
            results.append(result)
            decompile.write_json(folder / "result.json", result)
            checkpoint("target_complete")
        current = {}
        summary = checkpoint("complete", finished=True)
        if not summary["environment_unchanged"]:
            raise ValueError("Frozen environment changed; recovery evidence is invalid")
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=knowledge.DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--symbol", action="append")
    parser.add_argument("--revision-protocol", choices=("function", "edits-v1", "schema-edits-v1"), default="function")
    parser.add_argument("--declaration-orderings", type=int, default=0,
                        help="Try up to this many AST-screened declaration orders per target (0 disables; maximum 720)")
    args = parser.parse_args()
    print(json.dumps(run(args.prior.resolve(), args.output.resolve(), args.host, args.model,
                         args.attempts, args.max_tokens, args.timeout, set(args.symbol or []), args.database,
                         args.revision_protocol, args.declaration_orderings), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

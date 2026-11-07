"""Build a unit hypothesis from retained drafts and advance only verified stages."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from m2c_capture_inputs import Inputs
import m2c_unit_compile
from m2c_unit_inventory import capture, track_drafts
import m2c_unit_link
from m2c_unit_queue import publish
from m2c_unit_source import assemble, NULL_DEFINITION, source_drafts, overlay_native_source


def advance_verified_unit(root: Path, version: str, output: Path, *, propose: bool = False,
                          verify_project: bool = False, proposal_source: str | None = None,
                          configuration: dict[str, Path] | None = None, link_context: str = "native") -> dict:
    if link_context not in ("native", "retail-reference"):
        raise ValueError("Unsupported unit link context")
    linked = m2c_unit_link.run(root, output / "compile", output / "link",
                               f"build/{version}/main.elf", f"config/{version}/build.sha1", mode=link_context)
    result = {"state": "link_trial_complete", "link_record": str(output / "link/result.json"),
              "link_verified": bool(linked.get("link_verified"))}
    if not linked.get("link_verified") or not (propose or verify_project):
        return result
    from m2c_unit_proposal import run as create_proposal
    create_proposal(root, output / "link/result.json", output / "proposal", proposal_source, configuration)
    result.update(state="proposal_created", proposal_record=str(output / "proposal/result.json"))
    if verify_project:
        from m2c_unit_project_trial import run as project_trial
        trial = project_trial(root, output / "proposal/result.json", output / "project-trial")
        result.update(state="project_trial_complete", project_trial_record=str(output / "project-trial/result.json"),
                      project_trial_passed=trial.get("proposal_build_verified") is True)
    return result


def regenerate_draft(symbol: str, path: Path, declarations: str, folder: Path,
                     inputs: Inputs) -> tuple[bytes, dict]:
    import local_campaign_decompile as decompile
    from m2c_generation_experiment import generate, tool_fingerprint

    tool = decompile.tool().parent
    fingerprint = tool_fingerprint(tool)
    for source in [tool / "m2c.py", *sorted((tool / "m2c").rglob("*.py")),
                   *sorted((tool / "m2c_pycparser").rglob("*.py"))]:
        inputs.read(source)
    assembly = inputs.read(path.parent / "target.s").decode()
    generated = generate(tool, {"symbol": symbol, "assembly": assembly, "context": declarations},
                         folder, context_decls=True, infer_unknowns=True, deterministic_vars=True)
    for name in ("target.s", "context.c", "generation.json"):
        inputs.read(folder / name)
    raw = inputs.read(folder / "draft.c")
    inputs.verify()
    if tool_fingerprint(tool) != fingerprint:
        raise ValueError("Native generator changed during regeneration")
    if generated["returncode"] != 0:
        raise ValueError("Native regeneration failed: " + generated["error"])
    if raw.decode() != generated["draft"]:
        raise ValueError("Regenerated draft differs from recorded output")
    return raw, {"symbol": symbol, "tool_fingerprint": fingerprint, "command": generated["command"],
                 "record": str(folder / "generation.json")}


def run(root: Path, unit: str, batches: list[Path], output: Path,
        shared: Path | None = None, link: bool = False, regenerate: bool = False,
        unit_source: Path | None = None, preprocess_source: bool = False, *,
        propose: bool = False, verify_project: bool = False, proposal_source: str | None = None,
        configuration: dict[str, Path] | None = None, link_context: str = "native",
        function_sources: dict[str, Path] | None = None,
        helper_sources: dict[str, Path] | None = None) -> dict:
    root, output = root.resolve(), output.absolute()
    if output.exists():
        raise ValueError("Use a fresh pipeline output directory")
    inputs = Inputs()
    inputs.read(root / "objdiff.json")
    owner = capture(root, unit)
    target = root / owner["target_path"]
    if hashlib.sha256(inputs.read(target)).hexdigest() != owner["target_sha256"]:
        raise ValueError("Owner changed during inventory")
    for batch in batches:
        inputs.read(batch)
    tracker = track_drafts(owner, batches)
    inputs.verify()
    version = Path(owner["target_path"]).parts[1]
    output.mkdir(parents=True)
    result = {"unit": unit, "inventory": owner, "tracker": tracker, "state": "draft",
              "compiled": False, "object_verified": False, "link_verified": False,
              "ready_to_merge": False}
    try:
        shared_text = inputs.read(shared).decode() if shared else ""
        source_text = inputs.read(unit_source).decode() if unit_source else ""
        if preprocess_source:
            if not unit_source:
                raise ValueError("Native preprocessing requires a unit source")
            from m2c_unit_preprocess import run as preprocess
            source_text = preprocess(root, unit, unit_source, output / "preprocess", inputs)
        if preprocess_source and function_sources and not helper_sources:
            required = {function["name"] for function in tracker["functions"]}
            candidates = {symbol: inputs.read(path).decode() for symbol, path in function_sources.items()}
            source = overlay_native_source(source_text, candidates, required,
                                            cpp=unit_source.suffix.lower() in (".cpp", ".cc", ".cxx"))
            source_path = output / "assembled.c"
            source_path.write_text(source, encoding="utf-8", newline="\n")
            inputs.read(source_path)
            measured = m2c_unit_compile.run(root, unit, source_path, output / "compile",
                                           provenance=inputs, native_context=output / "preprocess")
            result.update({key: measured[key] for key in ("compiled", "object_verified", "state")})
            result.update(compile_record=str(output / "compile/result.json"),
                          source_mode="native_context_overlay", function_overrides={
                              symbol: {"path": str(path.resolve()), "sha256": hashlib.sha256(
                                  candidates[symbol].encode()).hexdigest()}
                              for symbol, path in function_sources.items()})
            inputs.files.update(measured["inputs"])
            inputs.read(output / "compile/result.json")
            if (link or propose or verify_project) and measured["object_verified"]:
                result.update(advance_verified_unit(root, version, output, propose=propose,
                    verify_project=verify_project, proposal_source=proposal_source,
                    configuration=configuration, link_context=link_context))
                for key in ("link_record", "proposal_record", "project_trial_record"):
                    if key in result:
                        inputs.read(Path(result[key]))
            inputs.verify()
            result["inputs"] = inputs.files
            record_path = output / "result.json"
            record_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            publish(root, version, record_path)
            return result
        imported = source_drafts(source_text, shared_text) if unit_source else []
        required = {function["name"] for function in tracker["functions"]}
        helpers = [draft for draft in imported if draft["symbol"] not in required and draft["static_inline"]]
        helpers_by_name = {draft["symbol"]: draft for draft in helpers}
        helper_overrides = {}
        for symbol, path in (helper_sources or {}).items():
            if symbol not in helpers_by_name:
                raise ValueError("Helper override is outside the source-backed owner: " + symbol)
            raw = inputs.read(path).decode()
            parsed = source_drafts(raw, helpers_by_name[symbol]["context"])
            if len(parsed) != 1 or parsed[0]["symbol"] != symbol or not parsed[0]["static_inline"]:
                raise ValueError("Helper override must define only its assigned static inline function: " + symbol)
            helpers_by_name[symbol] = parsed[0]
            helper_overrides[symbol] = {"path": str(path.resolve()),
                                        "sha256": hashlib.sha256(raw.encode()).hexdigest()}
        helper_names = set(helpers_by_name)
        helpers = [helpers_by_name[draft["symbol"]] for draft in helpers]
        result["helper_overrides"] = helper_overrides
        imported_by_name = {draft["symbol"]: draft for draft in imported
                            if draft["symbol"] not in helper_names}
        if set(imported_by_name) - required:
            raise ValueError("Source defines functions outside target owner: " +
                             ", ".join(sorted(set(imported_by_name) - required)))
        overrides = {}
        for symbol, path in (function_sources or {}).items():
            if symbol not in required or (unit_source and symbol not in imported_by_name):
                raise ValueError("Function override is outside the source-backed owner: " + symbol)
            raw = inputs.read(path).decode()
            parsed = source_drafts(raw, imported_by_name.get(symbol, {}).get("context", ""))
            if len(parsed) != 1 or parsed[0]["symbol"] != symbol:
                raise ValueError("Function override must define only its assigned symbol: " + symbol)
            imported_by_name[symbol] = parsed[0]
            overrides[symbol] = {"path": str(path.resolve()),
                                 "sha256": hashlib.sha256(raw.encode()).hexdigest()}
        result["function_overrides"] = overrides
        missing = [name for name in tracker["missing_functions"] if name not in imported_by_name]
        result["source_functions"] = sorted(imported_by_name)
        result["source_inline_helpers"] = [draft["symbol"] for draft in helpers]
        result["missing_implementations"] = missing
        if missing:
            raise ValueError("Missing function drafts: " + ", ".join(missing))
        drafts = []
        order = []
        for section in owner["sections"]:
            functions = sorted((s for s in section["definitions"] if s["type"] == 2), key=lambda s: s["offset"])
            order.extend(s["name"] for s in functions)
        for function in tracker["functions"]:
            if function["name"] in imported_by_name:
                drafts.append(imported_by_name[function["name"]])
                continue
            candidates = function["drafts"]
            if len({(candidate["sha256"], candidate["context_sha256"]) for candidate in candidates}) != 1:
                raise ValueError("Ambiguous drafts for " + function["name"])
            candidate = candidates[0]
            path = Path(candidate["path"])
            raw = inputs.read(path)
            if hashlib.sha256(raw).hexdigest() != candidate["sha256"]:
                raise ValueError("Draft changed during selection")
            context = path.parent / "context.c"
            context_raw = inputs.read(context) if context.is_file() else b""
            if hashlib.sha256(context_raw).hexdigest() != candidate["context_sha256"]:
                raise ValueError("Draft context changed during selection")
            declarations = context_raw.decode()
            if regenerate:
                raw, evidence = regenerate_draft(function["name"], path, declarations,
                    output / "generation" / str(len(drafts)), inputs)
                result.setdefault("regeneration", []).append(evidence)
            drafts.append({"symbol": function["name"], "source": raw.decode(), "context": declarations})
        header = root / "include/dolphin/types.h"
        if header.is_file():
            definitions = [line for line in inputs.read(header).decode().splitlines()
                           if NULL_DEFINITION.fullmatch(line)]
            if len(definitions) == 1:
                shared_text = definitions[0] + "\n" + shared_text
        from permuter import owner_extract
        recovered_data = []
        source = assemble(drafts, order, shared_text, data_owner=owner_extract.read_elf(target),
                          recovered_data=recovered_data, helper_drafts=helpers)
        result["recovered_data"] = recovered_data
        source_path = output / "assembled.c"
        source_path.write_text(source, encoding="utf-8", newline="\n")
        inputs.read(source_path)
        inputs.verify()
        measured = m2c_unit_compile.run(root, unit, source_path, output / "compile", provenance=inputs)
        result.update({key: measured[key] for key in ("compiled", "object_verified", "state")})
        result["compile_record"] = str(output / "compile/result.json")
        # The compiler's record also binds compiler, launcher, generated source and object.
        inputs.files.update(measured["inputs"])
        inputs.read(output / "compile/result.json")
        if (link or propose or verify_project) and measured["object_verified"]:
            result.update(advance_verified_unit(root, version, output, propose=propose,
                verify_project=verify_project, proposal_source=proposal_source, configuration=configuration,
                link_context=link_context))
            for key in ("link_record", "proposal_record", "project_trial_record"):
                if key in result:
                    inputs.read(Path(result[key]))
            # Each stage publishes independently revalidated evidence; this summary never approves a merge.
        inputs.verify()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result.update(state="assembly_or_validation_failed", error=str(error), object_verified=False)
    result["inputs"] = inputs.files
    record_path = output / "result.json"
    record_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    publish(root, version, record_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--batch-results", type=Path, action="append", default=[])
    parser.add_argument("--shared-context", type=Path)
    parser.add_argument("--unit-source", type=Path, help="self-contained existing C; object verification still required")
    parser.add_argument("--preprocess-source", action="store_true", help="capture native preprocessing of recorded source")
    parser.add_argument("--function-source", action="append", default=[], metavar="SYMBOL=FILE",
                        help="replace one source-backed function before object/link verification")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--link", action="store_true")
    parser.add_argument("--link-context", choices=("native", "retail-reference"), default="native")
    parser.add_argument("--regenerate", action="store_true", help="regenerate native drafts from retained inputs")
    parser.add_argument("--propose", action="store_true", help="package a review proposal after successful linking")
    parser.add_argument("--verify-project", action="store_true", help="also validate the proposal in an isolated native build")
    parser.add_argument("--proposal-source", help="project-relative destination for proposed source")
    parser.add_argument("--config-change", action="append", default=[], metavar="TARGET=FILE")
    args = parser.parse_args()
    configuration = {}
    for value in args.config_change:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in configuration:
            parser.error("Configuration changes require unique TARGET=FILE values")
        configuration[name] = Path(path)
    function_sources = {}
    for value in args.function_source:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in function_sources:
            parser.error("Function sources require unique SYMBOL=FILE values")
        function_sources[name] = Path(path)
    if (configuration or args.proposal_source) and not (args.propose or args.verify_project):
        parser.error("Proposal changes require --propose or --verify-project")
    result = run(args.root, args.unit, args.batch_results, args.output, args.shared_context, args.link,
                 args.regenerate, args.unit_source, args.preprocess_source, propose=args.propose,
                 verify_project=args.verify_project, proposal_source=args.proposal_source, configuration=configuration,
                 link_context=args.link_context, function_sources=function_sources)
    print(json.dumps({key: result.get(key) for key in ("unit", "state", "compiled", "object_verified", "error")}))


if __name__ == "__main__":
    main()

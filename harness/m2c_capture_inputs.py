"""Capture selected completed-batch inputs without consulting the attempt ledger.

Example: --root PROJECT --version VERSION --batch BATCH --symbol NAME --output FILE
(--root and --version default to the selected configured project.)
Repeat --batch and --symbol for a union within one project/version. Ambiguous
names must be captured separately. Only main-module PPC functions are supported.
Owner objects are read with the harness's vendored ``permuter.owner_extract`` ELF
reader; no project executable is invoked.

This interim snapshot embeds assembly/context but references current owner and
tool files. Run --verify FILE before use. The generation runner enforces these
extra hashes and strict compiler selection. No compilation, preprocessing, or
generation is run during capture.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shlex

import harness_paths


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Inputs:
    """Track logical paths as well as contents, including symlink retargeting."""

    def __init__(self):
        self.files: dict[str, dict] = {}

    def read(self, path: Path) -> bytes:
        path = path.absolute()
        raw = path.read_bytes()
        record = {"sha256": digest(raw), "resolved_path": str(path.resolve())}
        previous = self.files.setdefault(str(path), record)
        if previous != record:
            raise ValueError(f"Input changed during capture: {path}")
        return raw

    def verify(self) -> None:
        for path in list(self.files):
            self.read(Path(path))


def number(value) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an address or size")
    return int(str(value), 16 if str(value).lower().startswith("0x") else 10)


def unit_name(value: str) -> str:
    value = value.removeprefix("main/")
    if not value or Path(value).is_absolute() or any(p in (".", "..") for p in value.split("/")):
        raise ValueError(f"Unsupported unit: {value}")
    return value


def instructions(text: str, symbol: str) -> list[tuple[int, bytes, str]]:
    label = r'(?:' + re.escape(symbol) + r'|"' + re.escape(symbol) + r'")'
    blocks = re.findall(r"(?ms)^\s*\.fn\s+" + label + r",[^\n]*\n(.*?)^\s*\.endfn\s+"
                        + label + r"\s*(?:\n|$)", text)
    if len(blocks) != 1:
        raise ValueError(f"Expected one bounded assembly function: {symbol}")
    rows = []
    pattern = re.compile(r"^\s*/\*\s*([0-9a-fA-F]{8})\s+[0-9a-fA-F]{8}\s+"
                         r"((?:[0-9a-fA-F]{2}\s+){3}[0-9a-fA-F]{2})\s*\*/\s*(\S.*)$")
    for line in blocks[0].splitlines():
        match = pattern.match(line)
        if match:
            rows.append((int(match[1], 16), bytes.fromhex(match[2]), match[3]))
        elif line.strip() and not line.lstrip().startswith(("#", ".")):
            raise ValueError(f"Unsupported instruction evidence: {symbol}: {line}")
    if not rows or [r[0] for r in rows] != list(range(rows[0][0], rows[0][0] + 4 * len(rows), 4)):
        raise ValueError(f"Missing or noncontiguous instruction bytes: {symbol}")
    return rows


def read_owner(path: Path, inputs: Inputs):
    try:
        from permuter.owner_extract import read_elf
    except ModuleNotFoundError as error:
        raise ValueError("The vendored permuter package (harness/permuter) is unavailable") from error
    raw = inputs.read(path)
    obj = read_elf(path)
    if obj.data != raw:
        raise ValueError(f"Owner changed while reading: {path}")
    return obj


def compare_owner(obj, symbol: str, rows: list, report_function: dict) -> dict:
    functions = [s for s in obj.symbols if s.name == symbol and s.type == 2 and s.shndx]
    if len(functions) != 1:
        raise ValueError(f"Ambiguous or missing ELF function: {symbol}")
    fn = functions[0]
    section = obj.sections[fn.shndx]
    size = len(rows) * 4
    if (fn.size != size or fn.value < 0 or fn.value + size > len(section.data)
            or section.type != 1 or section.flags & 6 != 6):
        raise ValueError(f"Unsupported ELF function extent: {symbol}")
    if "address" in report_function and number(report_function["address"]) != fn.value:
        raise ValueError(f"Report/ELF section offset mismatch: {symbol}")
    payload = section.data[fn.value:fn.value + size]
    masks = bytearray(size)
    relocation_evidence = []
    # Mask only ABI-defined relocation fields, retaining opcode/register bits.
    for rel in obj.relocations:
        if rel.target_section_index != fn.shndx or not fn.value <= rel.offset < fn.value + size:
            continue
        offset = rel.offset - fn.value
        if rel.type in (4, 5, 6):  # ADDR16_LO/HI/HA
            width, mask, alignment = 2, 0xffff, 2
            valid = offset % 4 == 2
        elif rel.type == 10:  # REL24
            width, mask, alignment = 4, 0x03fffffc, 4
            valid = offset % 4 == 0
        elif rel.type == 109:  # EMB_SDA21, includes the base register
            width, mask, alignment = 4, 0x001fffff, 4
            valid = offset % 4 == 0
        else:
            raise ValueError(f"Unsupported PPC relocation {rel.type}: {symbol}")
        if not valid or offset % alignment or offset + width > size:
            raise ValueError(f"Invalid relocation extent: {symbol}")
        referenced = obj.symbols[rel.symbol_index].name
        operand = rows[offset // 4][2]
        suffix = {4: "@l", 5: "@h", 6: "@ha", 10: "", 109: "@sda21"}[rel.type]
        spelling = r'(?:' + re.escape(referenced) + '|' + re.escape('"' + referenced + '"') + ')'
        if not referenced or not re.search(r'(?<![\w.$@\"])' + spelling + re.escape(suffix)
                                           + r"(?=\s*(?:[,()]|$))", operand):
            raise ValueError(f"Relocation target not evidenced in assembly: {symbol}: {referenced}")
        # Nonzero addends need expression evaluation, which this exporter cannot certify.
        if rel.addend != 0:
            raise ValueError(f"Unsupported relocation addend: {symbol}: {rel.addend}")
        field = mask.to_bytes(width, "big")
        if any(masks[offset:offset + width]):
            raise ValueError(f"Overlapping relocations: {symbol}")
        masks[offset:offset + width] = field
        relocation_evidence.append({"offset": offset, "type": rel.type, "symbol": referenced,
                                    "addend": rel.addend, "mask": field.hex()})
    observed = b"".join(row[1] for row in rows)
    if any((a ^ b) & (~mask & 255) for a, b, mask in zip(observed, payload, masks)):
        raise ValueError(f"Stale assembly instruction bytes: {symbol}")
    return {"status": "matched_unrelocated_bits", "payload_sha256": digest(payload),
            "observed_sha256": digest(observed), "relocations": relocation_evidence,
            "limitation": "Relocated values are masked, not linked-address equality evidence"}


def bind_owner(root: Path, version: str, report: dict, symbol: str, address: int,
               size: int, inputs: Inputs) -> tuple[str, dict, object]:
    matches = []
    for unit in report.get("units", []):
        module = (unit.get("metadata") or {}).get("module_name", "main")
        if module != "main":
            continue
        functions = list(unit.get("functions") or [])
        for section in unit.get("sections") or []:
            for fn in section.get("functions") or []:
                if fn not in functions:
                    functions.append(fn)
        for fn in functions:
            if fn["name"] != symbol or number(fn["size"]) != size:
                continue
            name = unit_name(unit["name"])
            virtual = (fn.get("metadata") or {}).get("virtual_address")
            if virtual is None:
                asm = inputs.read(root / "build" / version / "asm" / (name + ".s")).decode()
                current = instructions(asm, symbol)
                if len(current) * 4 != size:
                    raise ValueError(f"Current assembly/report size mismatch: {symbol}")
                virtual = current[0][0]
            if number(virtual) == address:
                matches.append((name, fn))
    if len(matches) != 1:
        raise ValueError(f"Expected unique module/symbol/address/size owner: {symbol}: {len(matches)}")
    name, fn = matches[0]
    obj = read_owner(root / "build" / version / "obj" / (name + ".o"), inputs)
    return name, fn, obj


def compiler_profile(root: Path, original_unit: str, objdiff: dict, inputs: Inputs) -> dict:
    from m2c_context import COMPILER_NAMES
    from m2c_draft_attempts import STRICT_PROFILES
    units = [u for u in objdiff.get("units", []) if unit_name(u["name"]) == original_unit]
    if len(units) > 1:
        raise ValueError(f"Ambiguous original compiler owner: {original_unit}")
    scratch = (units[0].get("scratch") or {}) if units else {}
    compiler = scratch.get("compiler")
    version = COMPILER_NAMES.get(compiler)
    flags = shlex.split(scratch.get("c_flags") or "")
    selected = {"compiler": compiler, "mw_version": version, "flags": flags,
                "confidence": "original_owner_configuration", "unit": original_unit}
    supported = bool(version and version.removeprefix("GC/") in STRICT_PROFILES and flags)
    binary = root / "build/compilers" / version / "mwcceppc.exe" if version else None
    available = bool(binary and binary.is_file())
    if available:
        selected["binary_sha256"] = digest(inputs.read(binary))
        selected["binary"] = str(binary)
    return {"method": "current_objdiff_original_owner", "selected": selected,
            "profiles": [selected] if supported and available else [],
            "supported": supported, "binary_available": available,
            "status": ("supported" if supported and available else
                       "missing_binary" if supported else "unsupported_or_unknown"),
            "strict_selection_required": True}


def current_exact_owner_profile(root: Path, current_unit: str, symbol: str,
                                objdiff: dict, inputs: Inputs, original: dict) -> dict:
    """Recover a missing historical profile only from a certified current owner."""
    if original.get("status") == "supported":
        return original
    units = [unit for unit in objdiff.get("units", []) if unit_name(unit["name"]) == current_unit]
    if len(units) != 1:
        raise ValueError(f"Ambiguous current compiler owner: {current_unit}")
    unit = units[0]
    if unit.get("metadata", {}).get("complete") is not True:
        return original
    profile = compiler_profile(root, current_unit, objdiff, inputs)
    if profile["status"] != "supported":
        return original
    paths = []
    for name in (unit.get("target_path"), unit.get("base_path"),
                 unit.get("metadata", {}).get("source_path")):
        if not name:
            return original
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            return original
        inputs.read(path)
        paths.append(path)
    import m2c_object_evidence
    inputs.read(Path(m2c_object_evidence.__file__))
    evidence = m2c_object_evidence.compare(paths[0], paths[1], symbol)
    if evidence.get("status") != "checked" or evidence.get("scratch_object_equal") is not True:
        return {**original, "current_owner_rejected": {"unit": current_unit, "evidence": evidence}}
    profile["method"] = "current_exact_owner_configuration"
    profile["selected"]["confidence"] = "current_exact_owner_configuration"
    profile["original_profile"] = original
    profile["current_owner_evidence"] = {"symbol": symbol, "comparison": evidence,
                                          "source": str(paths[2]),
                                          "source_sha256": digest(inputs.read(paths[2]))}
    return profile


def refresh_context(root: Path, version: str, symbol: str, assembly: str, inputs: Inputs) -> tuple[str, dict]:
    import m2c_context as context

    features = context.parse_assembly_features(assembly, symbol)
    wanted = set(features.get("calls") or []) | {symbol}
    declarations, evidence, conflicts = {}, [], set()
    for row in context._report_exact_rows(context.ProjectSpec("capture", root, version)):
        name = row["symbol"]
        if name not in wanted:
            continue
        path = root / row["source_path"]
        try:
            raw = inputs.read(path)
        except FileNotFoundError:
            continue
        signature, _ = context._source_function(raw.decode("utf-8"), name)
        if not signature:
            continue
        if name in declarations and declarations[name] != signature:
            conflicts.add(name)
        declarations[name] = signature
        evidence.append({"symbol": name, "source": str(path), "sha256": digest(raw),
                         "signature": signature, "unit": row["unit"]})
    for name in conflicts:
        declarations.pop(name, None)
    target = declarations.pop(symbol, None)
    unresolved_types = {name: sorted(context.prototype_type_names(signature)[1])
                        for name, signature in {**declarations, **({symbol: target} if target else {})}.items()
                        if context.prototype_type_names(signature)[1]}
    for name in unresolved_types:
        declarations.pop(name, None)
    if symbol in unresolved_types:
        target = None
    header = context.build_mini_header(features, declarations, target)
    return header, {"method": "current_source_backed_signatures", "sources": evidence,
                    "conflicts": sorted(conflicts), "known_callees": sorted(declarations),
                    "unresolved_by_value_types": unresolved_types,
                    "target_signature": target,
                    "unknown_callees": sorted(set(features.get("calls") or []) - declarations.keys())}


def capture(root: Path, version: str, batches: list[Path], symbols: list[str], *,
            fresh_context: bool = False) -> dict:
    root = root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", version):
        raise ValueError("Unsupported version")
    if not symbols or len(set(symbols)) != len(symbols) or not batches:
        raise ValueError("Select distinct explicit symbols and at least one completed batch")
    inputs = Inputs()
    report_path = root / "build" / version / "report.json"
    report_raw = inputs.read(report_path)
    report = json.loads(report_raw)
    objdiff_raw = inputs.read(root / "objdiff.json")
    objdiff = json.loads(objdiff_raw)
    config_hashes = {}
    for path in [root / "build" / version / "config.json",
                 *(root / "config" / version / name for name in ("config.yml", "symbols.txt", "splits.txt"))]:
        config_hashes[str(path)] = digest(inputs.read(path))
    selected = {symbol: [] for symbol in symbols}
    batch_records = []
    for batch in batches:
        batch = batch.resolve()
        raw = inputs.read(batch / "results.json")
        data = json.loads(raw)
        summary, rows = data["summary"], data["results"]
        if (summary.get("finished") is not True or summary.get("total") != len(rows)
                or summary.get("processed") != len(rows)):
            raise ValueError(f"Batch is incomplete: {batch}")
        # Campaign batches without a recorded version belong to the selected project's version.
        if (Path(summary["workspace"]).resolve() != root
                or summary.get("version", harness_paths.VERSION) != version):
            raise ValueError(f"Batch project/version mismatch: {batch}")
        batch_records.append({"path": str(batch), "sha256": digest(raw), "summary": summary})
        for row in rows:
            if row["symbol"] in selected:
                selected[row["symbol"]].append((batch, row))
    rows, objects = [], {}
    for symbol, candidates in selected.items():
        if len(candidates) != 1:
            raise ValueError(f"Missing or ambiguous selected symbol: {symbol}: {len(candidates)}")
        batch, historical = candidates[0]
        if historical.get("module", "main") != "main":
            raise ValueError(f"Unsupported module: {historical['module']}")
        artifact = Path(historical["artifacts"])
        if not artifact.is_absolute():
            raise ValueError(f"Artifact path must be absolute: {artifact}")
        assembly = inputs.read(artifact / "target.s").decode()
        original = inputs.read(artifact / "original_target.s").decode()
        original_context = inputs.read(artifact / "context.c").decode()
        context, context_evidence = original_context, {"method": "frozen_batch_artifact"}
        if fresh_context:
            context, context_evidence = refresh_context(root, version, symbol, assembly, inputs)
        prepared_insns, original_insns = instructions(assembly, symbol), instructions(original, symbol)
        address = number(historical["address"])
        size = number(historical.get("size", len(original_insns) * 4))
        for insns in (prepared_insns, original_insns):
            if insns[0][0] != address or len(insns) * 4 != size:
                raise ValueError(f"Historical assembly identity mismatch: {symbol}")
        if [r[:2] for r in prepared_insns] != [r[:2] for r in original_insns]:
            raise ValueError(f"Prepared/original instruction bytes differ: {symbol}")
        name, fn, obj = bind_owner(root, version, report, symbol, address, size, inputs)
        evidence = compare_owner(obj, symbol, original_insns, fn)
        compare_owner(obj, symbol, prepared_insns, fn)
        owners = [u for u in objdiff["units"] if unit_name(u["name"]) == name]
        if len(owners) != 1 or (root / owners[0]["target_path"]).resolve() != obj.path.resolve():
            raise ValueError(f"Objdiff/report owner mismatch: {symbol}")
        objects[str(obj.path)] = digest(obj.data)
        original_unit = unit_name(historical["unit"])
        profile = compiler_profile(root, original_unit, objdiff, inputs)
        if name != original_unit:
            profile = current_exact_owner_profile(root, name, symbol, objdiff, inputs, profile)
        rows.append({"symbol": symbol, "module": "main", "address": address, "size": size,
                     "unit": name, "original_unit": historical["unit"], "owner_rebound": name != original_unit,
                     "batch": str(batch), "artifacts": str(artifact),
                     "draft_status": historical["status"], "historical_status": historical["status"],
                     "historical_pct": historical.get("pct"),
                     "historical_compiler_profile": historical.get("compiler_profile"),
                     "assembly": assembly, "assembly_sha256": digest(assembly.encode()),
                     "original_assembly": original, "original_assembly_sha256": digest(original.encode()),
                     "context": context, "context_sha256": digest(context.encode()), "context_available": True,
                     "original_context": original_context,
                     "original_context_sha256": digest(original_context.encode()),
                     "context_evidence": context_evidence,
                     "context_mode": historical.get("context_mode"), "compiler_profile": profile,
                     "owner_evidence": evidence, "evidence": {"status": "unavailable", "instruction_exact": None},
                     "categories": [], "compiler_messages": [], "outcome": {}})
    tool_hashes = {}
    tool_paths = [*(Path(part) for part in harness_paths.compiler_wrapper(root)[:1]),
                  harness_paths.build_tool(root, "dtk"), harness_paths.build_tool(root, "objdiff-cli")]
    for path in tool_paths:
        tool_hashes[str(path)] = digest(inputs.read(path)) if path.is_file() else None
    inputs.verify()
    return {"schema_version": 1, "capture_kind": "native_m2c_batch_inputs", "root": str(root),
            "version": version, "captured_at": datetime.now(UTC).isoformat(), "batches": batch_records,
            "report_sha256": digest(report_raw), "objdiff_sha256": digest(objdiff_raw),
            "config_hashes": config_hashes, "target_objects": objects, "tool_hashes": tool_hashes,
            "input_files": inputs.files, "rows": rows,
            "fresh_context": fresh_context,
            "scoring_contract": {"durable_scoring_root": False, "strict_profile_enforcement": "required",
                                 "requires_runner_update": False,
                                 "limitations": ["Referenced project files must remain unchanged",
                                                 "Missing original profiles require a certified current exact owner",
                                                 "Masked relocations do not certify linked byte equality"]}}


def verify_snapshot(snapshot: dict) -> None:
    inputs = Inputs()
    inputs.files = snapshot["input_files"]
    inputs.verify()
    for row in snapshot["rows"]:
        for field in ("assembly", "original_assembly", "context"):
            if digest(row[field].encode()) != row[field + "_sha256"]:
                raise ValueError(f"Embedded input changed: {row['symbol']}: {field}")
        if "original_context" in row and digest(row["original_context"].encode()) != row["original_context_sha256"]:
            raise ValueError(f"Embedded original context changed: {row['symbol']}")
    for filename, expected in snapshot.get("tool_hashes", {}).items():
        if expected is None and Path(filename).exists():
            raise ValueError(f"Previously unavailable tool appeared: {filename}")


def export(root: Path, version: str, batches: list[Path], symbols: list[str], output: Path, *,
           fresh_context: bool = False) -> dict:
    if output.exists():
        raise FileExistsError(output)
    snapshot = capture(root, version, batches, symbols, fresh_context=fresh_context)
    verify_snapshot(snapshot)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write("\n")
    try:
        verify_snapshot(snapshot)
    except Exception:
        output.unlink()
        raise
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=harness_paths.DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=harness_paths.VERSION or None,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--batch", action="append", type=Path, default=[])
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--fresh-context", action="store_true",
                        help="Rebuild mini-headers from hash-bound, source-backed signatures")
    args = parser.parse_args()
    if args.verify:
        verify_snapshot(json.loads(args.verify.read_bytes()))
        print("Snapshot input hashes verified; generation requires an available strict compiler profile.")
    else:
        if not args.root or not args.version or not args.output:
            parser.error("--root, --version and --output are required for capture")
        result = export(args.root, args.version, args.batch, args.symbol, args.output,
                        fresh_context=args.fresh_context)
        print(json.dumps({"rows": len(result["rows"]), "output": str(args.output),
                          "scoring_contract": result["scoring_contract"]}, indent=2))


if __name__ == "__main__":
    main()

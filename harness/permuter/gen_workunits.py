#!/usr/bin/env python3
"""gen_workunits.py — build decomp-permuter work units for the permuter farm.

For each row of a queue TSV (tier, pct, size, name, addr, unit) this produces
<outdir>/<fn>/ containing:
  base.c           preprocessed whole-TU source reduced to just <fn>
                   (other function bodies stripped to declarations)
  target.o         single-function object assembled from the dtk asm .s
  compile.sh       POSIX compile wrapper with the unit's exact MWCC flags
  compile.bat      the same for Windows
  compile_cmd.json direct compiler argv (no shell per candidate)
  settings.toml    permuter settings (func_name, mwcc weights, objdump cmd)
  meta.json        provenance (fn, unit, addr, pct, mw_version, cflags, gate info)

The unit's exact compiler version + flags are read from build.ninja (ground
truth for the byte-matching build).  Preprocessing runs the unit's own
mwcceppc.exe -E (through the host's compiler wrapper where one is needed), so
the preprocessed source is exactly what the matching build compiles.

Each unit is gated locally before it is queued:
  gate 1: base.c re-parses with decomp-permuter's pycparser fork
  gate 2: base.c compiles with the unit's exact mwcc + flags
  gate 3: the compiled object contains exactly one function: <fn>
  gate 4: isolated <fn> codegen equals the real full-TU build object
  gate 5: permuter Scorer(base.o vs target.o) yields a finite base score > 0

The opt-in ``--full-owner`` lane is local benchmark tooling, not a farm
format.  It uses Clang only as a directive-preserving sanitizer, splices a
clean target-only seed into that immutable full translation unit, proves the
shaped owner still reproduces the live target, applies the reviewed
owner-context policy configured for that function (none by default), and
compiles both owners with exact MWCC.  It audits every sibling
byte/symbol/relocation across both transitions and extracts only the
clean-context target for the unchanged scorer interface.

Usage (from the harness checkout, against the configured project):
  python harness/permuter/gen_workunits.py \
      [--root <project>] [--version <VERSION>] \
      [--queue build/permuter_queue.tsv] [--outdir <farm root>/units] \
      [--only fn_80001234] [--limit N] [--permuter <path>] [--binutils <dir>] \
      [--mwcc-pragma "peephole off"]

Only writes under --outdir (default: the farm's units/ directory under the
project's build/, which is gitignored).
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if __package__:
    from . import farm_config
    from .owner_extract import (
        ExtractError,
        extract as extract_owner_target,
        standalone_text_record,
    )
    from .owner_source import (
        OwnerSourceError,
        owner_policy_record,
        validate_candidate_translation_unit,
        validate_definition_only,
        validate_owner_target,
    )
else:  # run as a script
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import farm_config  # noqa: E402
    from owner_extract import (  # noqa: E402
        ExtractError,
        extract as extract_owner_target,
        standalone_text_record,
    )
    from owner_source import (  # noqa: E402
        OwnerSourceError,
        owner_policy_record,
        validate_candidate_translation_unit,
        validate_definition_only,
        validate_owner_target,
    )

harness_paths = farm_config.harness_paths

# The project checkout and its objdiff-cli; main() rebinds both from --root.
REPO = farm_config.project_root()
OBJDIFF = REPO / "build" / "tools" / "objdiff-cli"

OWNER_FIDELITY = "full-owner-clean-context-extracted-v2"
# Reviewed {function: policy name} for --full-owner targets.  Empty unless
# configured (permuter.owner_context_policies) or given with
# --owner-context-policy; a target without a policy is rejected.
OWNER_CONTEXT_POLICIES: dict[str, str] = {}
OBJDUMP_FLAGS = "-dr -EB -mpowerpc -M broadway"


def split_flags(value: str) -> list[str]:
    """Split ninja flags with POSIX quoting while keeping Windows path separators."""
    return shlex.split(re.sub(r"\\(?=[A-Za-z0-9_.$@+\-])", "/", value))


def compiler_launcher(root: Path) -> list[str]:
    """The host's compiler wrapper as absolute paths ([] where MWCC runs natively)."""
    if harness_paths is None:
        raise SystemExit("gen_workunits.py must run from the harness checkout (harness_paths)")
    resolved = []
    for part in harness_paths.compiler_wrapper(root):
        path = Path(part)
        if not path.is_absolute() and len(path.parts) == 1 and not (root / path).exists():
            found = shutil.which(part)
            path = Path(found) if found else root / path
        elif not path.is_absolute():
            path = root / path
        resolved.append(str(path))
    return resolved


def command_word(path: Path | str) -> str:
    """A tool path for a whitespace-split command string; quoted only when it must be."""
    value = Path(path).as_posix()
    return f'"{value}"' if any(char.isspace() for char in value) else value

# ---------------------------------------------------------------------------
# build.ninja parsing
# ---------------------------------------------------------------------------

def parse_ninja(ninja_path: Path):
    """Return {obj_path: {"mw_version":…, "cflags":…}} for mwcc rules.

    Object and source paths are normalised to ``/`` separators (dtk-template
    writes native separators, so a Windows build.ninja uses ``\\``).
    """
    text = ninja_path.read_text(encoding="utf-8")
    # Un-wrap ninja line continuations ("$\n" + indent).
    text = re.sub(r"\$\n\s*", "", text)
    units = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^build (\S+?): (mwcc\S*) (\S+)", line)
        if m:
            obj = m.group(1).replace("\\", "/")
            cur = {"obj": obj, "rule": m.group(2), "src": m.group(3).replace("\\", "/")}
            units[obj] = cur
            continue
        if cur is not None:
            m = re.match(r"^\s+mw_version = (.+)$", line)
            if m:
                cur["mw_version"] = m.group(1).strip()
                continue
            m = re.match(r"^\s+cflags = (.+)$", line)
            if m:
                cur["cflags"] = m.group(1).strip()
                continue
            if not line.startswith(" "):
                cur = None
    return units


def unit_to_paths(unit: str, version: str):
    """Map a queue unit name (e.g. main/game/unit) to dtk-template project paths."""
    rel = unit.split("/", 1)[1]  # drop leading "main/"
    return {
        "src": REPO / "src" / (rel + ".c"),
        "obj": f"build/{version}/src/{rel}.o",
        "asm": REPO / "build" / version / "asm" / (rel + ".s"),
    }


def normalized_disasm(objdump: Path, obj: Path, fn: str):
    """Normalize one function enough to compare isolated and full-TU codegen."""
    r = subprocess.run(
        [str(objdump), "-dr", "-EB", "-mpowerpc", "-M", "broadway",
         f"--disassemble={fn}", str(obj)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if (not line or line.startswith("Disassembly") or "file format" in line
                or line.endswith(">:")):
            continue
        reloc = re.match(r"^[0-9a-f]+:\s+(R_PPC\S+)\s+(.*)$", line)
        if reloc:
            symbol = re.sub(r"@\d+", "@N", reloc.group(2).strip())
            out.append(f"RELOC {reloc.group(1)} {symbol}")
            continue
        insn = re.match(r"^[0-9a-f]+:\s+((?:[0-9a-f]{2} ){4})\s*(.*)$", line)
        if insn:
            text = re.sub(r"\b[0-9a-f]+ <[^>]*>", "<T>", insn.group(2))
            out.append(f"{insn.group(1).strip()} {text.strip()}")
            continue
        out.append(line)
    return "\n".join(out) or None


def objdiff_match_percent(target: Path, candidate: Path, function: str) -> float:
    result = subprocess.run(
        [
            str(OBJDIFF),
            "diff",
            "-1",
            str(target),
            "-2",
            str(candidate),
            "-o",
            "-",
            "--format",
            "json",
            function,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError((result.stderr or result.stdout).strip()[-1200:])
    try:
        report = json.loads(result.stdout)
        symbols = report["left"]["symbols"]
        record = next(symbol for symbol in symbols if symbol.get("name") == function)
        percent = float(record["match_percent"])
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("objdiff did not return a target match percentage") from exc
    if not 0.0 <= percent <= 100.0:
        raise ValueError("objdiff returned an out-of-range match percentage")
    return percent


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_path(path: Path) -> str:
    """Project-relative POSIX path when ``path`` is inside the project, else absolute."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def owner_preprocessor_args(cflags: list[str]) -> list[str]:
    """Translate only MWCC include/define flags into Clang preprocessing flags."""
    result = ["-I", "."]
    index = 0
    while index < len(cflags):
        value = cflags[index]
        if value == "-i":
            if index + 1 >= len(cflags):
                raise ValueError("MWCC -i flag has no path")
            result.extend(["-I", cflags[index + 1]])
            index += 2
            continue
        if value in ("-D", "-U"):
            if index + 1 >= len(cflags):
                raise ValueError(f"MWCC {value} flag has no value")
            result.extend([value, cflags[index + 1]])
            index += 2
            continue
        if value.startswith(("-D", "-U")) and len(value) > 2:
            result.append(value)
        index += 1
    return result


def parse_depfile(path: Path) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    logical = text.replace("\\\n", " ")
    # The target ends at the first colon followed by whitespace; a Windows drive
    # letter's colon is followed by a separator instead.
    parts = re.split(r":(?=\s|$)", logical, maxsplit=1)
    if len(parts) != 2:
        raise ValueError("sanitizer dependency file has no target")
    dependencies = parts[1]
    paths: list[Path] = []
    for value in split_flags(dependencies):
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (REPO / candidate).resolve()
        if not resolved.is_file():
            raise ValueError(f"sanitizer dependency is missing: {value}")
        if resolved not in paths:
            paths.append(resolved)
    if not paths:
        raise ValueError("sanitizer dependency file is empty")
    return sorted(paths, key=repo_path)


def function_definition_span(source: str, function: str) -> tuple[int, int]:
    """Return a conservative one-line-signature function span."""
    matches = [entry for entry in _scan_functions(source) if entry[0] == function]
    if len(matches) != 1:
        raise ValueError(f"expected one definition of {function}, got {len(matches)}")
    _, _header_start, body_open, body_close, _knr = matches[0]
    header = source[:body_open]
    names = list(re.finditer(rf"\b{re.escape(function)}\s*\(", header))
    if not names:
        raise ValueError(f"could not locate signature for {function}")
    name_start = names[-1].start()
    start = source.rfind("\n", 0, name_start) + 1
    prefix = source[start:name_start].strip()
    if not prefix or "#" in prefix or ";" in prefix or "{" in prefix or "}" in prefix:
        raise ValueError(f"{function} must have a one-line signature in owner mode")
    return start, body_close + 1


def splice_owner_target(
    owner: str,
    function: str,
    definition: str,
    *,
    parser: Path,
    intrinsic_allowlist: frozenset[str] | None = None,
) -> str:
    owner_start, owner_end = function_definition_span(owner, function)
    seed_start, seed_end = function_definition_span(definition, function)
    validate_definition_only(definition, seed_start, seed_end)
    clean = definition[seed_start:seed_end].strip()
    owner_signature = " ".join(owner[owner_start : owner.index("{", owner_start, owner_end)].split())
    seed_signature = " ".join(clean[: clean.index("{")].split())
    if owner_signature != seed_signature:
        raise ValueError(
            f"clean seed signature drifted: expected {owner_signature!r}, got {seed_signature!r}"
        )
    spliced = owner[:owner_start] + clean + owner[owner_end:]
    validate_owner_target(
        clean,
        function,
        parser=parser,
        context_source=spliced,
        intrinsic_allowlist=intrinsic_allowlist,
    )
    return spliced


def clean_owner_target_context(
    owner: str, function: str, policies: dict[str, str] | None = None
) -> tuple[str, dict]:
    """Apply the reviewed owner-context policy configured for ``function``.

    ``policies`` maps function names to policy names (default:
    ``OWNER_CONTEXT_POLICIES``, empty unless configured).  A target without a
    reviewed policy, or with an unknown one, is rejected: the owner context is
    never rewritten by default.
    """
    policy = (OWNER_CONTEXT_POLICIES if policies is None else policies).get(function)
    if policy is None:
        raise OwnerSourceError(
            f"full-owner target rejected: no reviewed clean-context policy for {function}"
        )
    transform = OWNER_CONTEXT_TRANSFORMS.get(policy)
    if transform is None:
        raise OwnerSourceError(
            f"full-owner target rejected: unknown clean-context policy {policy!r} for "
            f"{function} (known: {', '.join(sorted(OWNER_CONTEXT_TRANSFORMS))})"
        )
    return transform(owner, function, policy)


def pragma_clean_v1(owner: str, function: str, policy: str) -> tuple[str, dict]:
    """Put a mutable target under canonical, pragma-clean flags.

    For an owner that shapes the target with ``#pragma optimization_level 4``
    followed by ``#pragma peephole off`` immediately before the definition and
    restores ``#pragma peephole on`` immediately after it, replace exactly that
    prefix with ``peephole on`` and retain the existing post-definition
    restore byte-for-byte.  Anything else fails closed.
    """
    start, end = function_definition_span(owner, function)
    prefix = owner[:start]
    suffix = owner[end:]
    shaped_prefix = re.compile(
        r"(?m)(^[ \t]*#pragma[ \t]+optimization_level[ \t]+4[ \t]*\n"
        r"[ \t]*#pragma[ \t]+peephole[ \t]+off[ \t]*\n)$"
    )
    match = shaped_prefix.search(prefix)
    if match is None or match.end() != len(prefix):
        raise OwnerSourceError(
            "full-owner clean-context policy did not find the exact shaped target prefix"
        )
    if not re.match(
        r"^\n[ \t]*#pragma[ \t]+peephole[ \t]+on(?:[ \t]*\n|[ \t]*$)", suffix
    ):
        raise OwnerSourceError(
            "full-owner clean-context policy did not find the target state restore"
        )
    replacement = "#pragma peephole on\n"
    clean = prefix[: match.start()] + replacement + owner[start:]
    clean_start, clean_end = function_definition_span(clean, function)
    if clean[clean_start:clean_end] != owner[start:end]:
        raise OwnerSourceError("clean-context transform changed the mutable definition")
    clean_suffix = clean[clean_end:]
    if clean_suffix != suffix:
        raise OwnerSourceError("clean-context transform changed following owner source")
    return clean, {
        "schema": 1,
        "policy": policy,
        "function": function,
        "removed_pragmas": ["optimization_level 4", "peephole off"],
        "inserted_pragmas": ["peephole on"],
        "following_state_restore": "peephole on retained",
        "shaped_source_sha256": hashlib.sha256(owner.encode("utf-8")).hexdigest(),
        "clean_source_sha256": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        "following_source_sha256": hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
    }


# Policy names usable in OWNER_CONTEXT_POLICIES.
OWNER_CONTEXT_TRANSFORMS = {"pragma-clean-v1": pragma_clean_v1}


def tool_attestation(path: Path) -> dict[str, str]:
    return {"path": repo_path(path), "sha256": file_sha256(path)}

# ---------------------------------------------------------------------------
# C pruning: strip all function bodies except the target's
# ---------------------------------------------------------------------------

def _scan_functions(src: str):
    """Yield (name, header_start, body_open, body_close) for every top-level
    function definition.  Tokenizer-based: tracks strings/chars and nesting, so
    multi-line signatures are handled (unlike a line regex)."""
    i, n = 0, len(src)
    depth_brace = 0
    funcs = []
    last_semicolon = 0  # start of the current top-level "statement"
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    break
                i += 1
        elif c == "{":
            if depth_brace == 0:
                # Function body iff the last non-ws char before '{' is ')'
                # (ANSI), or ';' preceded only by K&R parameter declarations
                # that trace back to a ')' (K&R style:  f(a, b) int a; int b; { ).
                j = i - 1
                while j >= 0 and src[j] in " \t\r\n":
                    j -= 1
                is_knr = False
                if j >= 0 and src[j] == ";":
                    k = j
                    declset = set(
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        "0123456789_*,;[] \t\r\n"
                    )
                    while k >= 0 and src[k] in declset:
                        k -= 1
                    if k >= 0 and src[k] == ")":
                        is_knr = True
                        j = k  # continue as if ')' preceded the '{'
                if j >= 0 and src[j] == ")":
                    # find matching '(' for the parameter list
                    k, pd = j, 0
                    while k >= 0:
                        if src[k] == ")":
                            pd += 1
                        elif src[k] == "(":
                            pd -= 1
                            if pd == 0:
                                break
                        k -= 1
                    # identifier before '('
                    m = None
                    kk = k - 1
                    while kk >= 0 and src[kk] in " \t\r\n":
                        kk -= 1
                    end_id = kk + 1
                    while kk >= 0 and (src[kk].isalnum() or src[kk] == "_"):
                        kk -= 1
                    name = src[kk + 1 : end_id]
                    if name:
                        m = name
                    # brace-match the body
                    bd, ii = 0, i
                    while ii < n:
                        cc = src[ii]
                        if cc == '"' or cc == "'":
                            qq = cc
                            ii += 1
                            while ii < n:
                                if src[ii] == "\\":
                                    ii += 2
                                    continue
                                if src[ii] == qq:
                                    break
                                ii += 1
                        elif cc == "{":
                            bd += 1
                        elif cc == "}":
                            bd -= 1
                            if bd == 0:
                                break
                        ii += 1
                    if m:
                        # for K&R fns the declaration must end at the '(' —
                        # param names may not appear in a prototype.  The K&R
                        # param decls also contain top-level ';', which poisons
                        # last_semicolon, so recompute the header start by
                        # scanning back over the return-type tokens.
                        hs = last_semicolon
                        if is_knr:
                            t = kk
                            while t >= 0 and (src[t].isalnum() or src[t] in "_* \t\r\n"):
                                t -= 1
                            hs = t + 1
                        funcs.append((m, hs, i, ii, k if is_knr else None))
                    i = ii
                    last_semicolon = ii + 1
                else:
                    # top-level initializer block: skip it wholesale
                    bd, ii = 0, i
                    while ii < n:
                        cc = src[ii]
                        if cc == '"' or cc == "'":
                            qq = cc
                            ii += 1
                            while ii < n:
                                if src[ii] == "\\":
                                    ii += 2
                                    continue
                                if src[ii] == qq:
                                    break
                                ii += 1
                        elif cc == "{":
                            bd += 1
                        elif cc == "}":
                            bd -= 1
                            if bd == 0:
                                break
                        ii += 1
                    i = ii
                    last_semicolon = ii + 1
            else:
                depth_brace += 1
        elif c == "}":
            if depth_brace > 0:
                depth_brace -= 1
        elif c == ";" and depth_brace == 0:
            last_semicolon = i + 1
        i += 1
    return funcs


def prune_source(src: str, keep_fn: str):
    """Replace every top-level function body except keep_fn's with ';'.
    'static'/'asm' qualifiers on stripped definitions are rewritten so the
    result still compiles (a declared-but-undefined static is an error).

    `static inline` helpers keep their bodies: the target may expand them, and
    a declaration-only stub would change the isolated target's code.  This is
    fail-closed -- if such a helper is *not* inlined it emits its own symbol
    and the single-symbol gate rejects the unit."""
    funcs = _scan_functions(src)
    kept = [f for f in funcs if f[0] == keep_fn]
    if not kept:
        return None, funcs
    out = []
    pos = 0
    for name, hdr_start, body_open, body_close, knr_paren in funcs:
        if name == keep_fn:
            continue
        if knr_paren is None and re.search(
            r"(^|\s)inline(\s)", src[hdr_start:body_open]
        ):
            continue
        out.append(src[pos:hdr_start])
        if knr_paren is not None:
            # K&R: declaration is "<ret> <name>();"
            header = src[hdr_start:knr_paren].rstrip() + "()"
        else:
            header = src[hdr_start:body_open].rstrip()
        # drop 'asm' qualifier and turn 'static' into 'extern'
        header = re.sub(r"(^|\s)asm(\s)", r"\1\2", header)
        header = re.sub(r"(^|\s)static(\s)", r"\1extern\2", header)
        out.append(header + ";\n")
        pos = body_close + 1
    out.append(src[pos:])
    return "".join(out), funcs

# ---------------------------------------------------------------------------
# target.o extraction
# ---------------------------------------------------------------------------

def extract_fn_asm(asm_path: Path, fn: str):
    lines = asm_path.read_text(encoding="utf-8").splitlines()
    out = None
    for ln in lines:
        if re.match(rf"^\.fn {re.escape(fn)},", ln):
            out = [".text", f".globl {fn}", f"{fn}:"]
            continue
        if out is not None:
            if ln.startswith(f".endfn {fn}"):
                return "\n".join(out) + "\n"
            # GNU as (gc-wii-binutils) wants a bare GQR number in psq_l/psq_st
            # operands where dtk writes qr0..qr7.
            ln = re.sub(r",\s*qr(\d)\b", r", \1", ln)
            # ... and bare CR-bit numbers where dtk writes cr1eq etc.
            ln = re.sub(
                r"\bcr([0-7])(lt|gt|eq|so|un)\b",
                lambda m: str(int(m.group(1)) * 4
                              + {"lt": 0, "gt": 1, "eq": 2, "so": 3, "un": 3}[m.group(2)]),
                ln,
            )
            out.append(ln)
    return None

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def write_text(path: Path, text: str, newline: str = "\n") -> None:
    """Write generated text byte-identically on every host (UTF-8, explicit line endings)."""
    path.write_text(text, encoding="utf-8", newline=newline)


def main():
    global REPO, OBJDIFF
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--root", help="project checkout (default: the configured project)")
    ap.add_argument("--version", help="build version (default: the configured project's)")
    ap.add_argument("--farm-root", help="permuter farm directory "
                    "(default: permuter.farm_root, else <project>/build/permuter-farm)")
    ap.add_argument("--queue", default="build/permuter_queue.tsv",
                    help="queue TSV: tier, pct, size, name, addr, unit (project-relative)")
    ap.add_argument("--outdir", help="unit output directory (default: <farm root>/units)")
    ap.add_argument("--only", action="append", default=None, help="only these fn names")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--permuter", help="decomp-permuter checkout "
                    "(default: tools.permuter / DECOMP_PERMUTER, else <farm root>/decomp-permuter)")
    ap.add_argument("--binutils", help="PowerPC binutils directory "
                    "(default: tools.ppc_binutils / DECOMP_PPC_BINUTILS, else <project>/build/binutils)")
    ap.add_argument(
        "--mwcc-pragma",
        action="append",
        default=[],
        help=(
            "additional MWCC pragma active for the isolated target; repeat for "
            "multiple pragmas (the full-TU fidelity gate still must pass)"
        ),
    )
    ap.add_argument(
        "--full-owner",
        action="store_true",
        help=(
            "explicit local-only mode: retain and compile the sanitized full owner, "
            "then audit/extract only the target"
        ),
    )
    ap.add_argument(
        "--owner-target-source",
        help="target-only clean seed required by --full-owner",
    )
    ap.add_argument(
        "--owner-sanitizer",
        help="Clang executable used only for directive-preserving preprocessing "
        "(default: tools.clang / DECOMP_CLANG, else clang on PATH)",
    )
    ap.add_argument(
        "--owner-context-policy",
        action="append",
        default=[],
        metavar="FUNCTION=POLICY",
        help="reviewed --full-owner context policy for a function, added to "
        "permuter.owner_context_policies (known: "
        + ", ".join(sorted(OWNER_CONTEXT_TRANSFORMS)) + ")",
    )
    ap.add_argument(
        "--intrinsic",
        action="append",
        default=[],
        metavar="FUNCTION=NAME",
        help="reviewed __ intrinsic a --full-owner target may use, added to "
        "permuter.intrinsic_allowlists (default: none)",
    )
    ap.add_argument("--force", action="store_true", help="regenerate existing units")
    args = ap.parse_args()

    if harness_paths is None:
        ap.error("run from the harness checkout: harness_paths is not importable")
    REPO = farm_config.project_root(args.root)
    OBJDIFF = harness_paths.build_tool(REPO, "objdiff-cli")
    version = farm_config.project_version(args.version)
    if not version:
        ap.error("no build version: pass --version or configure the project "
                 "(see harness.example.json)")
    farm = farm_config.farm_root(args.farm_root, REPO)
    try:
        OWNER_CONTEXT_POLICIES.update(farm_config.owner_context_policies(args.owner_context_policy))
        intrinsic_allowlists = farm_config.intrinsic_allowlists(args.intrinsic)
    except ValueError as exc:
        ap.error(str(exc))

    if args.full_owner:
        if not args.owner_target_source:
            ap.error("--full-owner requires --owner-target-source")
        if not args.only or len(args.only) != 1:
            ap.error("--full-owner requires exactly one --only target")
        if args.mwcc_pragma:
            ap.error("--full-owner preserves source pragmas; --mwcc-pragma is forbidden")
    elif args.owner_target_source:
        ap.error("--owner-target-source requires --full-owner")

    permuter = farm_config.permuter_checkout(args.permuter, farm)
    binutils = farm_config.binutils_dir(args.binutils, REPO)
    ppc_as = farm_config.binutil(binutils, "as")
    ppc_objdump = farm_config.binutil(binutils, "objdump")
    ppc_objcopy = farm_config.binutil(binutils, "objcopy")
    ppc_readelf = farm_config.binutil(binutils, "readelf")
    owner_extractor = SCRIPT_DIR / "owner_extract.py"
    owner_guard = SCRIPT_DIR / "owner_source.py"
    sanitizer = farm_config.clang(args.owner_sanitizer)
    if args.full_owner and sanitizer is None:
        ap.error("--full-owner needs Clang: pass --owner-sanitizer or configure tools.clang")
    prerequisites = [permuter / "strip_other_fns.py", ppc_as, ppc_objdump]
    if args.full_owner:
        prerequisites.extend(
            [ppc_objcopy, ppc_readelf, owner_extractor, sanitizer, OBJDIFF]
        )
    for p in prerequisites:
        if not p.exists():
            sys.exit(f"missing prerequisite: {p}")
    sys.path.insert(0, str(permuter))

    # MWCC runs natively on Windows and through the host's wrapper elsewhere.
    wrapper = compiler_launcher(REPO)
    sjiswrap = REPO / "build" / "tools" / "sjiswrap.exe"

    owner_seed_path: Path | None = None
    owner_seed = ""
    sanitizer_version = ""
    if args.full_owner:
        owner_seed_path = Path(args.owner_target_source).expanduser()
        if not owner_seed_path.is_absolute():
            owner_seed_path = (REPO / owner_seed_path).resolve()
        if not owner_seed_path.is_file():
            sys.exit(f"missing owner target seed: {owner_seed_path}")
        owner_seed = owner_seed_path.read_text(encoding="utf-8")
        identified = subprocess.run(
            [str(sanitizer), "--version"], capture_output=True, text=True
        )
        if identified.returncode != 0 or not identified.stdout.strip():
            sys.exit(f"could not identify owner sanitizer: {sanitizer}")
        sanitizer_version = identified.stdout.strip()

    ninja_units = parse_ninja(REPO / "build.ninja")

    outdir = REPO / args.outdir if args.outdir else farm / "units"
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    with open(REPO / args.queue, encoding="utf-8") as f:
        for ln in f:
            if ln.startswith("#") or not ln.strip():
                continue
            tier, pct, size, name, addr, unit = ln.rstrip("\n").split("\t")
            rows.append(dict(tier=int(tier), pct=float(pct), size=int(size),
                             fn=name, addr=addr, unit=unit))
    if args.only:
        rows = [r for r in rows if r["fn"] in set(args.only)]
    if args.limit:
        rows = rows[: args.limit]

    # local scorer (gate 4)
    from src.scorer import Scorer  # decomp-permuter

    manifest = []
    n_ok = n_skip = 0
    for row in rows:
        fn, unit = row["fn"], row["unit"]
        udir = outdir / fn
        meta = dict(row)
        meta["status"] = "unknown"

        def fail(reason, detail=""):
            nonlocal n_skip
            meta["status"] = "skip"
            meta["reason"] = reason
            if detail:
                meta["detail"] = detail[-1500:]
            manifest.append(meta)
            n_skip += 1
            print(f"SKIP {fn} ({unit}): {reason}")

        if udir.exists() and (udir / "meta.json").exists() and not args.force:
            old = json.loads((udir / "meta.json").read_text(encoding="utf-8"))
            if old.get("status") == "ok":
                manifest.append(old)
                n_ok += 1
                print(f"KEEP {fn} (already generated)")
                continue
            if old.get("status") == "dequeued":
                # terminal: manually pulled from the farm (e.g. a unit whose
                # isolated baseline proved unusable) — never resurrect
                manifest.append(old)
                n_skip += 1
                print(f"KEEP {fn} (dequeued)")
                continue

        paths = unit_to_paths(unit, version)
        nu = ninja_units.get(paths["obj"])
        if nu is None:
            fail("unit not in build.ninja")
            continue
        if not paths["src"].exists() or not paths["asm"].exists():
            fail("missing src or asm", f"{paths['src']} {paths['asm']}")
            continue

        mwcc = REPO / "build" / "compilers" / nu["mw_version"] / "mwcceppc.exe"
        cflags = split_flags(nu["cflags"])
        allowlist = intrinsic_allowlists.get(fn, frozenset())
        for pragma in args.mwcc_pragma:
            cflags.extend(["-pragma", pragma])

        udir.mkdir(parents=True, exist_ok=True)

        # 1. Preprocess.  Normal work units deliberately retain the historical
        # exact-MWCC path.  Full-owner mode uses Clang only to resolve
        # directives while retaining active MWCC pragmas; exact MWCC remains
        # the sole code generator.
        pp = udir / "_pp.c"
        owner_dependencies: list[Path] = []
        sanitizer_argv: list[str] = []
        shaped_owner_source = ""
        context_transform: dict | None = None
        if args.full_owner:
            depfile = udir / "_owner.d"
            try:
                sanitizer_argv = [
                    str(sanitizer),
                    "-E",
                    "-P",
                    "-nostdinc",
                    "-Wno-unknown-pragmas",
                    *owner_preprocessor_args(cflags),
                    "-MMD",
                    "-MF",
                    str(depfile),
                    str(paths["src"]),
                    "-o",
                    str(pp),
                ]
            except ValueError as exc:
                fail("could not derive owner sanitizer flags", str(exc))
                continue
            r = subprocess.run(
                sanitizer_argv, cwd=REPO, capture_output=True, text=True
            )
        else:
            r = subprocess.run(
                [*wrapper, str(mwcc), *cflags, "-E", str(paths["src"]), "-o", str(pp)],
                cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0 or not pp.exists():
            fail(
                "owner sanitizer failed" if args.full_owner else "mwcc -E failed",
                r.stderr or r.stdout,
            )
            continue
        src_pp = pp.read_text(encoding="utf-8", errors="replace")
        if args.full_owner:
            try:
                owner_dependencies = parse_depfile(depfile)
            except (OSError, ValueError) as exc:
                fail("invalid owner sanitizer dependency record", str(exc))
                continue
            forbidden_context = []
            if re.search(r"(?m)^\s*#\s*include\b", src_pp):
                forbidden_context.append("#include")
            if re.search(r"\b(?:__asm__|__asm|asm)\b", src_pp):
                forbidden_context.append("assembly")
            if re.search(r"\.inc\b", src_pp, flags=re.IGNORECASE):
                forbidden_context.append(".inc")
            if forbidden_context:
                fail(
                    "sanitized owner retained forbidden source constructs",
                    ", ".join(forbidden_context),
                )
                continue
        else:
            src_pp = re.sub(r"^/\* #line .*\n?", "", src_pp, flags=re.M)

        # 2. Keep either an isolated target or an immutable full-owner context.
        funcs = _scan_functions(src_pp)
        if args.full_owner:
            try:
                shaped_owner_source = splice_owner_target(
                    src_pp,
                    fn,
                    owner_seed,
                    parser=sanitizer,
                    intrinsic_allowlist=allowlist,
                )
                pruned, context_transform = clean_owner_target_context(
                    shaped_owner_source, fn
                )
            except (ValueError, OwnerSourceError) as exc:
                fail("invalid clean owner seed", str(exc))
                continue
        else:
            pruned, funcs = prune_source(src_pp, fn)
            if pruned is None:
                fail("target fn not found in preprocessed TU (asm-only or name mismatch)")
                continue
        base_c = udir / "base.c"
        write_text(base_c, pruned)
        shaped_c = udir / "_shaped_owner.c"
        if args.full_owner:
            write_text(shaped_c, shaped_owner_source)
            try:
                validate_candidate_translation_unit(
                    pruned,
                    pruned,
                    fn,
                    parser=sanitizer,
                    intrinsic_allowlist=allowlist,
                )
            except OwnerSourceError as exc:
                fail("clean owner candidate policy failed", str(exc))
                continue

        # gate 1: pycparser parse (what the permuter itself will do)
        if not args.full_owner:
            gate = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]);"
                 "from perm_pycparser.c_parser import CParser;"
                 "CParser().parse(open(sys.argv[2], encoding='utf-8').read(), 'base.c')",
                 str(permuter), str(base_c)],
                capture_output=True, text=True)
            if gate.returncode != 0:
                fail("pycparser parse failed", gate.stderr)
                continue

        # 3. target.o from dtk asm
        fn_asm = extract_fn_asm(paths["asm"], fn)
        if fn_asm is None:
            fail("fn not found in dtk asm .s")
            continue
        tgt_s = udir / "_target.s"
        write_text(tgt_s, fn_asm)
        r = subprocess.run(
            [str(ppc_as), "-mgekko", "-mregnames", "-be",
             "-o", str(udir / "target.o"), str(tgt_s)],
            capture_output=True, text=True)
        if r.returncode != 0:
            fail("powerpc-eabi-as failed", r.stderr)
            continue
        retail_target_record: dict | None = None
        if args.full_owner:
            try:
                retail_target_record = standalone_text_record(udir / "target.o")
            except ExtractError as exc:
                fail("could not fingerprint retail target", str(exc))
                continue

        # 4. compile gate with exact flags (drop -i/include & -MMD-ish flags:
        # base.c is fully preprocessed).  Keep everything else identical.
        cf = []
        skip_next = False
        for a in cflags:
            if skip_next:
                skip_next = False
                continue
            if a == "-i":
                skip_next = True
                continue
            cf.append(a)
        base_o = udir / "_base.o"
        r = subprocess.run(
            [*wrapper, str(sjiswrap), str(mwcc), *cf, "-c",
             str(base_c), "-o", str(base_o)],
            cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0 or not base_o.exists():
            fail("base.c does not compile", r.stderr or r.stdout)
            continue

        # gate 3/4: establish the single-function scorer object and prove that
        # it reproduces the live full-TU target.  Owner mode additionally
        # audits every sibling before extracting the target.
        full_o = REPO / paths["obj"]
        if not full_o.is_file():
            fail("missing full-TU object for fidelity gate", str(full_o))
            continue
        score_o = base_o
        owner_report: dict | None = None
        shaped_report: dict | None = None
        shaped_o = udir / "_shaped_owner.o"
        shaped_extracted = udir / "_shaped_extracted.o"
        if args.full_owner:
            score_o = udir / "_base_extracted.o"
            r = subprocess.run(
                [
                    *wrapper,
                    str(sjiswrap),
                    str(mwcc),
                    *cf,
                    "-c",
                    str(shaped_c),
                    "-o",
                    str(shaped_o),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0 or not shaped_o.exists():
                fail("shaped owner does not compile", r.stderr or r.stdout)
                continue
            try:
                shaped_report = extract_owner_target(
                    baseline_path=full_o,
                    candidate_path=shaped_o,
                    function=fn,
                    output_path=shaped_extracted,
                    objcopy=ppc_objcopy,
                    readelf=ppc_readelf,
                )
                owner_report = extract_owner_target(
                    baseline_path=shaped_o,
                    candidate_path=base_o,
                    function=fn,
                    output_path=score_o,
                    objcopy=ppc_objcopy,
                    readelf=ppc_readelf,
                )
            except ExtractError as exc:
                fail("full-owner sibling audit/extraction failed", str(exc))
                continue
            if (
                shaped_report["baseline"]["fingerprint_sha256"]
                != shaped_report["candidate"]["fingerprint_sha256"]
            ):
                fail("shaped owner target differs from live full-TU target")
                continue
            if (
                owner_report["baseline"]["fingerprint_sha256"]
                == owner_report["candidate"]["fingerprint_sha256"]
            ):
                fail("pragma-clean target did not differ from shaped target")
                continue
            meta["mode"] = "full-owner"
            meta["fidelity"] = OWNER_FIDELITY
        else:
            r = subprocess.run([str(ppc_objdump), "-d", str(base_o)],
                               capture_output=True, text=True)
            symbols = re.findall(r"^[0-9a-f]+ <([^>]+)>:$", r.stdout, flags=re.M)
            if symbols != [fn]:
                fail("stripped object has wrong functions", ",".join(symbols))
                continue
            isolated_disasm = normalized_disasm(ppc_objdump, base_o, fn)
            full_disasm = normalized_disasm(ppc_objdump, full_o, fn)
            if not isolated_disasm or not full_disasm:
                fail("could not disassemble function for fidelity gate")
                continue
            if isolated_disasm != full_disasm:
                fail("isolated baseline differs from full-TU object")
                continue
            meta["fidelity"] = "isolated-equals-full-tu"

        # gate 5: finite base score
        try:
            scorer = Scorer(str(udir / "target.o"), stack_differences=True,
                            algorithm="difflib", debug_mode=False,
                            ign_branch_targets=False,
                            objdump_command=f"{command_word(ppc_objdump)} {OBJDUMP_FLAGS}")
            score, _ = scorer.score(str(score_o))
        except Exception as e:  # noqa
            fail("scorer failed", str(e))
            continue
        if score >= Scorer.PENALTY_INF:
            fail("base score infinite")
            continue
        meta["base_score"] = score
        if args.full_owner:
            try:
                meta["pct"] = objdiff_match_percent(
                    udir / "target.o", score_o, fn
                )
            except ValueError as exc:
                fail("objdiff baseline score failed", str(exc))
                continue
        if score == 0:
            fail("base score already 0 (function already matches?)")
            continue

        # 5. Farm-side files.  Every command names this host's own tools by
        # absolute path, so the farm runs the units where they were generated.
        mwver = nu["mw_version"]
        flags_str = subprocess.list2cmdline(cf)
        if args.full_owner:
            assert owner_report is not None
            compile_bat = (
                "@echo off\r\n"
                "echo full-owner work units are local benchmark-only 1^>^&2\r\n"
                "exit /b 2\r\n"
            )
        else:
            launcher = [*wrapper, str(sjiswrap), str(mwcc)]
            compile_bat = (
                "@echo off\r\n"
                + " ".join(f'"{part}"' for part in launcher)
                + f" {flags_str} -c %1 -o %3\r\n"
            )
        write_text(udir / "compile.bat", compile_bat, newline="")
        # compile.sh: what an unpatched decomp-permuter runs on POSIX hosts.
        if args.full_owner:
            baseline_fingerprint = owner_report["candidate"]["fingerprint_sha256"]
            python_path = Path(sys.executable).resolve()
            # Project tools are attested relative to the project root; the
            # harness guard/extractor and host tools by absolute path.
            wrapper_paths = {
                "sjiswrap": "build/tools/sjiswrap.exe",
                "compiler": f"build/compilers/{mwver}/mwcceppc.exe",
            }
            wrapper_external = {
                "source_guard": str(owner_guard),
                "extractor": str(owner_extractor),
                "python": str(python_path),
                "sanitizer": str(sanitizer),
                "objcopy": str(ppc_objcopy),
                "readelf": str(ppc_readelf),
            }
            launcher_names = [f"compiler_wrapper_{index}" for index in range(len(wrapper))]
            wrapper_external.update(zip(launcher_names, wrapper))
            wrapper_hashes = {
                name: file_sha256(
                    REPO / value if name in wrapper_paths else Path(value)
                )
                for name, value in {**wrapper_paths, **wrapper_external}.items()
            }
            compile_sh = f"""#!{python_path} -I
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FUNCTION = {fn!r}
FLAGS = {cf!r}
INTRINSICS = {sorted(allowlist)!r}
PROJECT_ROOT = {str(REPO)!r}
REPO_PATHS = {wrapper_paths!r}
EXTERNAL_PATHS = {wrapper_external!r}
LAUNCHER = {launcher_names!r}
EXPECTED_HASHES = {wrapper_hashes!r}
EXPECTED_BASE_SHA256 = {file_sha256(base_c)!r}
EXPECTED_BASELINE_FINGERPRINT = {baseline_fingerprint!r}
CLEAN_ENV = {{"LANG": "C", "LC_ALL": "C", "PATH": os.defpath}}
if os.name == "nt":
    for _name in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "WINDIR"):
        if os.environ.get(_name):
            CLEAN_ENV[_name] = os.environ[_name]

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def fail(message):
    print("full-owner compile error: " + message, file=sys.stderr)
    raise SystemExit(2)

if len(sys.argv) != 4 or sys.argv[2] != "-o":
    fail("expected candidate, -o, output arguments")
script_dir = Path(__file__).resolve().parent
repo_root = Path(PROJECT_ROOT)
if not (repo_root / "configure.py").is_file():
    fail("could not find the project root")
paths = {{name: repo_root / value for name, value in REPO_PATHS.items()}}
paths.update({{name: Path(value) for name, value in EXTERNAL_PATHS.items()}})
for name, path in paths.items():
    if not path.is_file() or digest(path) != EXPECTED_HASHES[name]:
        fail("attested tool drifted: " + name)
base = script_dir / "base.c"
base_bytes = base.read_bytes()
if hashlib.sha256(base_bytes).hexdigest() != EXPECTED_BASE_SHA256:
    fail("attested clean base drifted")
candidate = Path(sys.argv[1]).resolve()
candidate_bytes = candidate.read_bytes()
output = Path(sys.argv[3]).resolve()
output.unlink(missing_ok=True)

def run(argv):
    result = subprocess.run(
        [str(value) for value in argv],
        cwd=repo_root,
        env=CLEAN_ENV,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)

with tempfile.TemporaryDirectory(prefix="owner-build-", dir=script_dir) as temporary:
    scratch = Path(temporary)
    base_snapshot = scratch / "base.c"
    candidate_snapshot = scratch / "candidate.c"
    base_snapshot.write_bytes(base_bytes)
    candidate_snapshot.write_bytes(candidate_bytes)
    baseline_owner = scratch / "baseline-owner.o"
    candidate_owner = scratch / "candidate-owner.o"
    run([
        paths["python"], "-I", paths["source_guard"],
        "--base-tu", base_snapshot, "--candidate-tu", candidate_snapshot,
        "--function", FUNCTION, "--parser", paths["sanitizer"],
        *("--intrinsic=" + name for name in INTRINSICS),
    ])
    prefix = [*(paths[name] for name in LAUNCHER), paths["sjiswrap"], paths["compiler"], *FLAGS]
    run([*prefix, "-c", base_snapshot, "-o", baseline_owner])
    run([*prefix, "-c", candidate_snapshot, "-o", candidate_owner])
    run([
        paths["python"], "-I", paths["extractor"],
        "--baseline", baseline_owner, "--candidate", candidate_owner,
        "--function", FUNCTION, "--output", output,
        "--objcopy", paths["objcopy"], "--readelf", paths["readelf"],
        "--expected-baseline-fingerprint", EXPECTED_BASELINE_FINGERPRINT,
    ])
"""
        else:
            compile_sh = (
                "#!/bin/sh\n"
                "exec "
                + " ".join(shlex.quote(part) for part in [*wrapper, str(sjiswrap), str(mwcc), *cf])
                + ' -c "$1" -o "$3"\n'
            )
        write_text(udir / "compile.sh", compile_sh)
        os.chmod(udir / "compile.sh", 0o755)

        # Direct-argv compile command (avoids a shell per candidate; sjiswrap
        # only when the preprocessed source actually contains non-ASCII).
        if args.full_owner:
            compile_command = {
                "mode": "full-owner",
                "supported": False,
                "reason": "local compile.sh audit/extract pipeline required",
            }
        else:
            needs_sjis = any(b > 0x7F for b in base_c.read_bytes())
            argv = list(wrapper)
            if needs_sjis:
                argv.append(str(sjiswrap))
            argv.append(str(mwcc))
            argv += cf
            compile_command = {"argv": argv}
        write_text(udir / "compile_cmd.json", json.dumps(compile_command, indent=1))

        objdump_command = f"{command_word(ppc_objdump)} {OBJDUMP_FLAGS}"
        settings = (
            f'func_name = "{fn}"\n'
            'compiler_type = "mwcc"\n'
            f"objdump_command = {json.dumps(objdump_command)}\n"
            "\n"
            "[weight_overrides]\n"
            "perm_reorder_decls = 40\n"
            "perm_reorder_stmts = 40\n"
            "perm_dummy_comma_expr = 0\n"
            "perm_commutative = 20\n"
            "perm_pad_var_decl = 0\n"
            "perm_split_assignment = 20\n"
            "perm_duplicate_assignment = 0\n"
            "perm_add_self_assignment = 0\n"
            "perm_refer_to_var = 0\n"
        )
        write_text(udir / "settings.toml", settings)

        meta["status"] = "ok"
        meta["mw_version"] = mwver
        meta["cflags"] = flags_str
        meta["mwcc_pragmas"] = args.mwcc_pragma
        meta["n_fns_in_tu"] = len(funcs)
        if args.full_owner:
            assert owner_report is not None
            assert shaped_report is not None
            assert owner_seed_path is not None
            assert context_transform is not None
            assert retail_target_record is not None
            dependency_records = [
                {"path": repo_path(path), "sha256": file_sha256(path)}
                for path in owner_dependencies
            ]
            meta["owner"] = {
                "schema": 2,
                "source": repo_path(paths["src"]),
                "seed": {
                    "path": repo_path(owner_seed_path),
                    "sha256": file_sha256(owner_seed_path),
                },
                "clean_base_sha256": file_sha256(base_c),
                "candidate_policy": owner_policy_record(
                    fn, sanitizer, intrinsic_allowlist=allowlist
                ),
                "context_transform": context_transform,
                "sanitizer": {
                    **tool_attestation(sanitizer),
                    "version": sanitizer_version,
                    "version_sha256": hashlib.sha256(
                        sanitizer_version.encode("utf-8")
                    ).hexdigest(),
                    "argv": [repo_path(Path(value)) if value.startswith(str(REPO)) else value
                             for value in sanitizer_argv],
                    "inputs": dependency_records,
                },
                "generator": tool_attestation(Path(__file__).resolve()),
                "source_guard": tool_attestation(owner_guard),
                "extractor": tool_attestation(owner_extractor),
                "objcopy": tool_attestation(ppc_objcopy),
                "readelf": tool_attestation(ppc_readelf),
                "python": tool_attestation(Path(sys.executable)),
                "compiler": tool_attestation(mwcc),
                "compiler_wrapper": [tool_attestation(Path(part)) for part in wrapper],
                "sjiswrap": tool_attestation(sjiswrap),
                "live_owner": {
                    "path": repo_path(full_o),
                    "sha256": file_sha256(full_o),
                    "target": shaped_report["baseline"],
                },
                "shaped_owner": {
                    "source_sha256": hashlib.sha256(
                        shaped_owner_source.encode("utf-8")
                    ).hexdigest(),
                    "sha256": shaped_report["candidate_owner_sha256"],
                    "target": shaped_report["candidate"],
                    "sibling_audit": shaped_report["sibling_audit"],
                },
                "clean_owner": {
                    "source_sha256": file_sha256(base_c),
                    "sha256": owner_report["candidate_owner_sha256"],
                    "target": owner_report["candidate"],
                },
                "retail_target": retail_target_record,
                "extracted_baseline": owner_report["output"],
                "sibling_audit": owner_report["sibling_audit"],
            }
        write_text(udir / "meta.json", json.dumps(meta, indent=1))
        manifest.append(meta)
        n_ok += 1
        print(f"OK   {fn} ({unit}) base_score={score}")

        # tidy intermediates
        temporary_paths = [pp, tgt_s, base_o]
        if args.full_owner:
            temporary_paths.extend(
                [
                    udir / "_owner.d",
                    udir / "_base_extracted.o",
                    shaped_c,
                    shaped_o,
                    shaped_extracted,
                ]
            )
        for tmp in temporary_paths:
            try:
                tmp.unlink()
            except OSError:
                pass

    # The farm reloads manifest.json between assignments; replace it atomically.
    staged = outdir / "manifest.json.tmp"
    write_text(staged, json.dumps(manifest, indent=1))
    for attempt in range(50):
        try:
            os.replace(staged, outdir / "manifest.json")
            break
        except PermissionError:  # Windows: the farm is reading it right now
            if attempt == 49:
                raise
            time.sleep(0.05)
    print(f"\ndone: {n_ok} ok, {n_skip} skipped -> {outdir / 'manifest.json'}")


if __name__ == "__main__":
    main()

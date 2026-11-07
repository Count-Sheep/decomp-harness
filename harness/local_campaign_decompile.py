"""Cached, advisory PowerPC-to-C drafts from m2c. Never modifies project source."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harness_paths import DECOMP_ROOT, REPO_ROOT, VERSION, WINDOWS, env, python_command, tool_path
import local_campaign_asm_inputs as asm_inputs
import m2c_readonly_data
import m2c_remote


def m2c_root() -> Path:
    """The m2c checkout: ``tools.m2c`` / ``DECOMP_M2C``, else ``build/tools/m2c`` in the harness."""
    return tool_path("m2c", REPO_ROOT / "build/tools/m2c")


def tool() -> Path:
    return m2c_root() / "m2c.py"


def split_command(command: str) -> list[str]:
    """Split a Ninja command line with POSIX quoting.

    Ninja files use POSIX quoting on every host, but Windows checkouts write backslash paths,
    which POSIX escaping would silently collapse; there backslashes are kept literally.
    """
    if not WINDOWS:
        return shlex.split(command)
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return list(lexer)


def link_file(link: Path, target: Path) -> None:
    """Point ``link`` at ``target``: a symlink, else (Windows without symlink rights) a hard link or copy."""
    try:
        link.symlink_to(target)
        return
    except FileExistsError:
        raise
    except OSError:
        if not WINDOWS:
            raise
    try:
        os.link(target, link)
    except FileExistsError:
        raise
    except OSError:
        shutil.copyfile(target, link)


def link_directory(link: Path, target: Path) -> None:
    """A directory symlink, or a junction on Windows hosts without symlink rights."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if not WINDOWS:
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def unlink_directory(link: Path) -> None:
    """Remove a directory symlink or junction without touching what it points to."""
    try:
        if WINDOWS:
            os.rmdir(link)
        else:
            link.unlink()
    except FileNotFoundError:
        pass


def replace_file(source: Path, destination: Path, attempts: int = 50) -> None:
    """``os.replace`` that tolerates a Windows reader briefly holding ``destination`` open."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.02)


def context_flags(compile_command: str) -> list[str]:
    """Carry the unit's include search and defines into the context preprocessor."""
    args = split_command(compile_command.split(" && ", 1)[0])
    flags, index = [], 0
    while index < len(args):
        arg = args[index]
        if arg in {"-i", "-I", "-D", "-U"} and index + 1 < len(args):
            flags.extend(["-I" if arg == "-i" else arg, args[index + 1]])
            index += 2
        else:
            if arg.startswith(("-D", "-U", "-I")) and len(arg) > 2:
                flags.append(arg)
            index += 1
    return flags


def global_declaration_modes(script: Path) -> list[str]:
    """Read m2c's enum without importing an untrusted checkout into this process."""
    options = script.parent / "m2c" / "options.py"
    try:
        tree = ast.parse(options.read_text(encoding="utf-8"), filename=str(options))
    except (OSError, SyntaxError):
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GlobalDeclsEnum":
            return sorted({statement.value.value for statement in node.body
                           if isinstance(statement, ast.Assign)
                           and isinstance(statement.value, ast.Constant)
                           and isinstance(statement.value.value, str)})
    return []


def tool_metadata() -> dict:
    script = tool()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=script.parent,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    if not revision:
        raise ValueError("m2c checkout revision is unavailable")
    digest = hashlib.sha256(revision.encode())
    files = [script, *sorted((script.parent / "m2c").rglob("*.py")),
             *sorted((script.parent / "m2c_pycparser").glob("*.py"))]
    for path in files:
        digest.update(path.relative_to(script.parent).as_posix().encode())
        digest.update(path.read_bytes())
    return {"revision": revision, "fingerprint": digest.hexdigest(),
            "capabilities": {"global_declaration_modes": global_declaration_modes(script)}}


def preprocess(root: Path, compile_command: str, source: str) -> dict:
    clang = shutil.which("clang")
    if not clang or not compile_command:
        return {"status": "unavailable", "error": "clang or the unit compile command is missing"}
    # MWCC searches the working directory for root-relative scoring-wrapper includes.
    command = [clang, "--target=powerpc-unknown-none", "-E", "-P", "-nostdinc", "-I", str(root),
               *context_flags(compile_command), source]
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30)
        return {"status": "ok" if result.returncode == 0 else "context_error", "context": result.stdout,
                "command": command, "error": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "context_error", "error": str(exc), "command": command}


def write_text(path: Path, text: str) -> None:
    """UTF-8 with LF line endings on every host, so recorded hashes match the bytes on disk."""
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8",
                                     newline="\n") as temporary:
        json.dump(value, temporary, indent=2)
    replace_file(Path(temporary.name), path)


def translate(root: Path, symbol: str, asm: bytes, assembly_label: str, context: dict,
              cache: Path, metadata: dict, timeout: int = 45, force: bool = False,
              *, explicit_fields: bool = False, infer_unknowns: bool = False,
              deterministic_vars: bool = False, version: str | None = None,
              context_declarations: bool = False, readonly_owner: Path | None = None) -> dict:
    """Translate immutable input snapshots; supports explicitly untyped batch drafts."""
    version = version or VERSION
    typed = context.get("status") == "ok"
    text = context.get("context", "") if typed else ""
    original_asm = asm
    asm, assembly_inputs = asm_inputs.prepare(root, symbol, asm, version)
    readonly = None
    if readonly_owner is not None:
        asm, readonly = m2c_readonly_data.capture(readonly_owner, symbol, asm)
    supported_modes = (metadata.get("capabilities") or {}).get("global_declaration_modes")
    declaration_mode = "used-context" if context_declarations and (
        supported_modes is None or "used-context" in supported_modes) else None
    key = hashlib.sha256(b"draft-v4\0" + str(typed).encode() + symbol.encode() + asm + text.encode()
                         + metadata["fingerprint"].encode()
                         + (b"\0explicit-fields-v1" if explicit_fields else b"")
                         + (b"\0infer-unknowns-v1" if infer_unknowns else b"")
                         + (b"\0context-declarations-v2:" + str(declaration_mode).encode()
                            if context_declarations else b"")
                         + (b"\0readonly-owner-v1:" + readonly["owner_sha256"].encode() if readonly else b"")
                         + (b"\0deterministic-vars-v1" if deterministic_vars else b"")).hexdigest()
    folder = cache / key
    manifest = folder / "result.json"
    if manifest.is_file() and not force:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
        cached["assembly"] = str(folder / "original_target.s")
        return cached
    folder.mkdir(parents=True, exist_ok=True)
    # Shared immutable inputs avoid copying large units hundreds of times.
    for name, content, extension in (("target.s", asm, ".s"), ("original_target.s", original_asm, ".s"),
                                     ("context.c", text.encode(), ".c")):
        shared = cache / "inputs" / (hashlib.sha256(content).hexdigest() + extension)
        if not shared.is_file():
            shared.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=shared.parent, delete=False) as temporary:
                temporary.write(content)
            replace_file(Path(temporary.name), shared)
        link = folder / name
        if not link.exists():
            try:
                link_file(link, shared.resolve())
            except FileExistsError:
                pass
    command = [env("M2C_PYTHON") or python_command(), str(tool()), "-t", "ppc-mwcc-c",
               "--no-cache", "--valid-syntax"]
    if not infer_unknowns:
        command.append("--no-unk-inference")
    if explicit_fields:
        command.append("--explicit-field-access")
    if deterministic_vars:
        command.append("--deterministic-vars")
    if declaration_mode:
        command.extend(["--globals", declaration_mode])
    if typed:
        command.extend(["--context", str(folder / "context.c")])
    command.extend(["-f", symbol, str(folder / "target.s")])
    row = {"symbol": symbol, "tool_revision": metadata["revision"], "tool_fingerprint": metadata["fingerprint"],
           "assembly": str(folder / "original_target.s"), "assembly_source": assembly_label,
           "context_command": context.get("command"),
           "context_mode": "typed" if typed else "assembly_only",
           "context_error": str(context.get("error") or "")[-1600:],
           "assembly_inputs": assembly_inputs, "command": command, "artifacts": str(folder)}
    row["explicit_field_access"] = explicit_fields
    row["unknown_inference"] = infer_unknowns
    row["deterministic_vars"] = deterministic_vars
    row["context_declarations"] = context_declarations
    row["context_declarations_mode"] = declaration_mode
    row["readonly_data"] = readonly
    if context_declarations and not declaration_mode:
        row.update(status="unavailable", draft="",
                   error="m2c does not support --globals used-context; install harness patch 0015")
        row["diagnostics"] = {"translation_errors": 0, "unresolved_context_transfers": 0,
                              "unset_saved_registers": 0, "unknown_fields": 0, "unknown_types": 0}
        write_text(folder / "draft.c", "")
        write_json(manifest, row)
        return row
    payload = m2c_remote.job_payload(symbol, asm, text, {
        "typed": typed, "infer_unknowns": infer_unknowns, "explicit_fields": explicit_fields,
        "deterministic_vars": deterministic_vars, "declaration_mode": declaration_mode,
    }, metadata["fingerprint"], timeout)

    def execute_local() -> dict:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    try:
        executed = m2c_remote.execute(payload, execute_local)
        row["execution"] = executed.get("execution")
        status = ("timeout" if executed["returncode"] == 124 else
                  "ok" if executed["returncode"] == 0 and executed["stdout"].strip() else
                  "decompile_error")
        row.update(status=status, draft=executed["stdout"],
                   error=(executed["stderr"] or
                          (executed["stdout"] if executed["returncode"] else ""))[-1600:])
        write_text(folder / "stderr.log", executed["stderr"])
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        row.update(status="timeout", draft=partial, error=f"m2c exceeded {timeout} seconds")
    except (OSError, RuntimeError) as exc:
        row.update(status="error", draft="", error=str(exc))
    write_text(folder / "draft.c", row["draft"])
    if row["status"] == "ok" and ("M2C_ERROR" in row["draft"] or "saved_reg_" in row["draft"]
                                  or "M2C_UNRESOLVED_CONTEXT_TRANSFER" in row["draft"]):
        row["status"] = "unsupported"
    row["diagnostics"] = {"translation_errors": row["draft"].count("M2C_ERROR"),
                          "unresolved_context_transfers": row["draft"].count("M2C_UNRESOLVED_CONTEXT_TRANSFER"),
                          "unset_saved_registers": row["draft"].count("saved_reg_"),
                          "unknown_fields": row["draft"].count("M2C_FIELD"),
                          "unknown_types": row["draft"].count("M2C_UNK")}
    write_json(manifest, row)
    return row


def draft(root: Path, symbol: str, assembly: Path, compile_command: str,
          source: str, cache: Path, *, context_declarations: bool = False,
          readonly_owner: Path | None = None) -> dict:
    """Return a draft or diagnostic, keeping assembly, context and tool provenance.

    Clang is used only to expand includes/macros for m2c's type parser. MWCC and
    objdiff remain the only judges of candidate code. A failed context never
    silently falls back to guessed signatures.
    """
    script, clang = tool(), shutil.which("clang")
    if not script.is_file() or not clang:
        return {"status": "unavailable", "error": "m2c checkout or clang is missing"}
    if not compile_command or not assembly.is_file():
        return {"status": "unavailable", "error": "unit compile command or target assembly is missing"}
    try:
        context = preprocess(root, compile_command, source)
        if context["status"] != "ok":
            return {"status": context["status"], "error": context.get("error", "")[-1600:]}
        return translate(root, symbol, assembly.read_bytes(), str(assembly), context, cache, tool_metadata(),
                         context_declarations=context_declarations, readonly_owner=readonly_owner)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": str(exc)}


def prompt_context(row: dict, budget: int = 3500) -> str:
    body = row.get("draft", "").strip()
    if row.get("status") != "ok" or row.get("context_mode") == "assembly_only" or not body or len(body) > budget:
        return ""
    return ("## Assembly-derived C hypothesis (m2c, not a verified match)\n\n"
            "This draft comes from RETAIL target assembly, using this unit's preprocessed types.\n"
            "Use it to understand loads, offsets, branches and loops, not to replace the best source blindly.\n"
            "Its inferred temporaries, casts, signatures and unknown-type macros are NOT authoritative.\n"
            "Keep the assigned signature and real layouts; do not copy M2C_* macros or invent fields.\n"
            "Only MWCC compilation and exact objdiff verification establish a match.\n\n"
            f"```c\n{body}\n```")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true", help="draft every canonical report function below exactly 100%%")
    parser.add_argument("--corpus", choices=("unmatched", "matched", "all"), default="unmatched",
                        help="batch target set; matched functions test m2c against known C")
    parser.add_argument("--jobs", type=int, default=1, help="concurrent m2c processes for --all (default 1; serial)")
    parser.add_argument("--limit", type=int, default=0, help="limit batch targets for a pilot; 0 means all")
    parser.add_argument("--force", action="store_true", help="regenerate completed batch targets")
    parser.add_argument("--timeout", type=int, default=45, help="per-function m2c timeout in seconds")
    parser.add_argument("--replay", type=Path, help="rerun a completed batch's frozen assembly and type contexts")
    parser.add_argument("--context-declarations", action="store_true", help="emit referenced context declarations (requires m2c patch 0015)")
    parser.add_argument("--readonly-data", action="store_true", help="capture referenced readonly bytes from the target owner")
    args = parser.parse_args()
    if args.all or args.replay:
        if args.symbol:
            parser.error("choose a symbol or --all, not both")
        from local_campaign_decompile_batch import run_batch
        return run_batch(DECOMP_ROOT, max(1, min(args.jobs, 8)), args.limit, args.timeout, args.force, args.replay, args.corpus,
                         context_declarations=args.context_declarations, readonly_data=args.readonly_data)
    if not args.symbol:
        parser.error("provide a symbol or --all")
    import local_campaign as campaign
    import local_campaign_corpus as corpus
    from local_campaign_m2c_report import support_root
    # The model's verifier temporarily substitutes source: context reads share its lock.
    with campaign.COORDINATOR.build("C draft", f"m2c context: {args.symbol}"):
        item = campaign.locate_symbol(args.symbol)
        index = corpus.load_index(campaign.ROOT, campaign.REPORT_FILE, campaign.STATE_DIR / "corpus_index.json")
        assembly = index["functions"].get(args.symbol, {}).get("asm_path")
        row = draft(campaign.ROOT, args.symbol, campaign.ROOT / assembly if assembly else Path(),
                    campaign.unit_cflags(item), item["source"], support_root() / "drafts",
                    context_declarations=args.context_declarations,
                    readonly_owner=(m2c_readonly_data.owner_path(campaign.ROOT, VERSION, item["unit"])
                                    if args.readonly_data else None))
    if args.json:
        print(json.dumps(row, indent=2))
    elif row.get("status") == "ok":
        print(row["draft"], end="")
        print(f"Unverified draft; provenance and inputs: {row['artifacts']}", file=sys.stderr)
    else:
        print(f"{row.get('status')}: {row.get('error') or 'unsupported target assembly'}", file=sys.stderr)
    return 0 if row.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

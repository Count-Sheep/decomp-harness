"""Explain register walls by replaying MWCC's register allocator.

cadmic/mwcc-debugger (mwdbg.py) runs MWCC under the retrowin32 emulator's gdb stub and dumps
its internal passes, including the code just before and just after register allocation.
It only knows the GC/1.1 and GC/2.6 compilers, so this tool replays a function with one of
those and first checks the replay is faithful: the replay compiler's code for the function
must equal what the unit's real compiler produces. When it is, diffing the before/after
regalloc dumps maps every virtual register to its physical one, and the objdiff against
retail then says which values retail colours differently, and at which source lines.

Setup: put mwdbg.py, the gdb.py stand-in and a retrowin32 binary in the decomp's build/tools/mwdbg
(or configure `tools.mwdbg` / $DECOMP_MWDBG). Neither the binary nor the dumps belong in git.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import harness_paths
from harness_paths import DECOMP_ROOT as ROOT, VERSION  # the decomp this harness drives (see harness_paths.py)
MWDBG_DIR = harness_paths.tool_path("mwdbg", ROOT / "build" / "tools" / "mwdbg")
REPLAY_VERSIONS = ("GC/2.6", "GC/1.1")
BEFORE, AFTER = "before-regalloc", "after-regalloc"


def emulator() -> Path:
    return harness_paths.exe(MWDBG_DIR / "retrowin32")


def available() -> bool:
    return all((MWDBG_DIR / name).exists() for name in ("mwdbg.py", "gdb.py")) and emulator().exists()


def split_command(line: str) -> list[str]:
    """POSIX-split a ninja command line, keeping lone backslashes of Windows paths."""
    return shlex.split(re.sub(r'\\(?![\\"])', r"\\\\", line))


def compile_args(source: str) -> list[str]:
    """The unit's real mwcceppc arguments (without the wrappers, -MMD, -c and -o)."""
    compiled = Path("build", VERSION, Path(source).with_suffix(".o")).as_posix()
    commands = subprocess.run(["ninja", "-t", "commands", compiled], cwd=ROOT, capture_output=True, text=True, timeout=60)
    spellings = {f" {source} ", f" {Path(source)} "}
    lines = [line for line in commands.stdout.splitlines()
             if "mwcceppc" in line and any(text in line + " " for text in spellings)]
    if commands.returncode or not lines:
        raise RuntimeError(f"no compile command for {source}")
    args = split_command(lines[-1].split(" && ", 1)[0])
    # Drop any compiler wrapper (wibo/wine) in front of the compiler itself.
    args = args[next(i for i, arg in enumerate(args) if Path(arg).name.lower() in ("mwcceppc.exe", "mwcceppc")):]
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-o", "-MF"):
            skip = True
            continue
        if arg in ("-MMD", "-c") or arg == source or Path(arg) == Path(source):
            continue
        out.append(arg)
    return out


def replay_command(args: list[str], version: str, source: str, output: Path) -> list[str]:
    compiler = f"build/compilers/{version}/mwcceppc.exe"
    return [compiler] + args[1:] + ["-c", source, "-o", str(output)]


def _dump_main(argv: list[str]) -> int:
    """Subprocess entry: start the emulator's gdb stub and run mwdbg against it."""
    outdir, function, command = argv[0], argv[1], argv[argv.index("--") + 1:]
    os.makedirs(outdir, exist_ok=True)
    with open(Path(outdir) / "emu.log", "w", encoding="utf-8") as log:
        process = subprocess.Popen([str(emulator()), "--gdb-stub"] + command, cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=log)
    time.sleep(0.3)
    sys.path.insert(0, str(MWDBG_DIR))
    import faulthandler
    import gdb  # noqa: F401  (the stand-in; mwdbg imports it too)
    faulthandler.dump_traceback_later(900, exit=True)
    scope = {"__name__": "__main__", "FUNCTION_NAME": function, "OUTPUT_DIR": outdir}
    try:
        code = (MWDBG_DIR / "mwdbg.py").read_text(encoding="utf-8")
        exec(compile(code, str(MWDBG_DIR / "mwdbg.py"), "exec"), scope)
    except SystemExit:
        pass
    finally:
        process.kill()
    return 0


def dump(source: str, function: str, version: str, outdir: Path, timeout: int = 600) -> Path:
    command = replay_command(compile_args(source), version, source, outdir / "replay.o")
    subprocess.run([sys.executable, __file__, "_dump", str(outdir), function, "--"] + command, cwd=ROOT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    return outdir


def blocks(path: Path) -> list[tuple[str, list[str]]]:
    """(mnemonic, operand text) per instruction, in block order, from a backend dump."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s+\d+\s+(\S+)\s*(.*)$", line)
        if match and not line.lstrip().startswith(("B", ";")):
            out.append((match.group(1), match.group(2).strip()))
    return out


def stage(outdir: Path, name: str) -> Path | None:
    found = sorted(outdir.glob(f"backend-*-{name}*.txt"))
    return found[0] if found else None


REGISTER = re.compile(r"\b(?:r|f|cr)\d+\b|\b(?:vr|vf|v)\d+\b|%\w+")


def virtual_map(outdir: Path) -> dict[str, set[str]]:
    """virtual register -> physical registers it became, from the before/after regalloc dumps."""
    before, after = stage(outdir, BEFORE), stage(outdir, AFTER)
    if not before or not after:
        return {}
    pre, post = blocks(before), blocks(after)
    mapping: dict[str, set[str]] = {}
    # Allocation may insert spill code; align on mnemonic runs and skip what does not line up.
    i = j = 0
    while i < len(pre) and j < len(post):
        if pre[i][0] != post[j][0]:
            j += 1
            continue
        a, b = REGISTER.findall(pre[i][1]), REGISTER.findall(post[j][1])
        if len(a) == len(b):
            for virtual, physical in zip(a, b):
                if virtual != physical:
                    mapping.setdefault(virtual, set()).add(physical)
        i += 1
        j += 1
    return mapping


def faithful(outdir: Path, source: str, function: str, real_object: Path, version: str = REPLAY_VERSIONS[0]) -> bool:
    """The replay compiler's code for the function equals the real compiler's.

    The emulated run stops once the dump is taken, so the replay object is built separately
    through the host's compiler wrapper (none on Windows), as the unit's own build runs the real compiler."""
    replay = outdir / "replay.o"
    command = replay_command(compile_args(source), version, source, replay)
    subprocess.run(harness_paths.compiler_wrapper(ROOT) + command, cwd=ROOT, capture_output=True, timeout=300)
    if not replay.exists() or not real_object.exists():
        return False
    objdiff = harness_paths.build_tool(ROOT, "objdiff-cli")
    if not objdiff.exists():
        return False
    out = outdir / "fidelity.json"
    subprocess.run([str(objdiff), "diff", "-1", str(real_object), "-2", str(replay), "-o", str(out), "--format", "json",
                    function], cwd=ROOT, capture_output=True, timeout=120)
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    left = next((s for s in data.get("left", {}).get("symbols", []) if s.get("name") == function), None)
    return bool(left) and left.get("match_percent") == 100.0


def assignments(outdir: Path, kind: str = "gpr") -> list[dict]:
    """The allocator's decisions in the order it made them (last pass wins)."""
    passes = sorted(outdir.glob(f"regalloc-{kind}-pass-*-assigned.txt"),
                    key=lambda p: int(re.search(r"pass-(\d+)", p.name).group(1)))
    if not passes:
        return []
    out, current = [], None
    for line in passes[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        head = re.match(r"^(\S+) -> (\S+)(?: (.+))?$", line)
        if head:
            current = {"virtual": head.group(1), "physical": head.group(2), "name": (head.group(3) or "").strip(),
                       "order": len(out) + 1}
            out.append(current)
            continue
        field = re.match(r"^\s+(adjusted cost|cost|neighbors): (\S+)", line)
        if field and current is not None:
            key = {"adjusted cost": "adjusted", "cost": "cost", "neighbors": "degree"}[field.group(1)]
            current[key] = float(field.group(2)) if key == "adjusted" else int(field.group(2))
    return out


def virtual_lines(outdir: Path) -> dict[str, list[int]]:
    """Source lines at which each virtual register is written, from the before-regalloc dump."""
    path = stage(outdir, BEFORE)
    lines: dict[str, list[int]] = {}
    if not path:
        return lines
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s+(\d+)\s+\S+\s+(r\d+|f\d+)\b", line)
        if match:
            lines.setdefault(match.group(2), [])
            number = int(match.group(1))
            if number not in lines[match.group(2)]:
                lines[match.group(2)].append(number)
    return lines


def register_differences(rows: list[tuple[str, str, int | None]]) -> list[tuple[str, str, int | None]]:
    """(ours, retail, source line) for each register that differs on otherwise equal rows."""
    found, seen = [], set()
    for want, have, line in rows:
        if not want or not have or want == have or want.split()[0] != have.split()[0]:
            continue
        a, b = re.findall(r"\b[rf]\d+\b", want), re.findall(r"\b[rf]\d+\b", have)
        if len(a) != len(b):
            continue
        for retail, ours in zip(a, b):
            if retail != ours and (ours, retail, line) not in seen:
                seen.add((ours, retail, line))
                found.append((ours, retail, line))
    return found


def explain(outdir: Path, rows: list[tuple[str, str, int | None]], source_text: str, is_faithful: bool) -> str:
    """Plain-text account of which values retail colours differently, for a person or the model."""
    table = {row["virtual"]: row for row in assignments(outdir, "gpr") + assignments(outdir, "fpr")}
    written = virtual_lines(outdir)
    source_lines = source_text.splitlines()
    everything = register_differences(rows)
    if not everything:
        return "No register-only differences against retail."
    # A colouring difference swaps registers consistently: each of ours maps to one of retail's
    # and back. When one register of ours maps to several, the instructions are scheduled
    # differently and the rows only line up by accident; the replay cannot explain those.
    forward: dict[str, set[str]] = {}
    backward: dict[str, set[str]] = {}
    for ours, retail, _ in everything:
        forward.setdefault(ours, set()).add(retail)
        backward.setdefault(retail, set()).add(ours)
    differences = [d for d in everything if len(forward[d[0]]) == 1 and len(backward[d[1]]) == 1]
    scheduled = len(everything) - len(differences)
    if not differences:
        return (f"The {len(everything)} register differences do not pair up one-to-one: the instruction order "
                "differs (scheduling), not the colouring. Fix the order of the computations first.")
    header = ("Register replay (MWCC's own allocator, replayed with GC/2.6; its code for this function is identical "
              "to the unit's compiler, so these decisions are exact):" if is_faithful else
              "Register replay (GC/2.6 does NOT reproduce this unit's code exactly; treat as a guide):")
    out = [header]
    if scheduled:
        out.append(f"({scheduled} other register differences do not pair up one-to-one; they come from instruction "
                   "order, not colouring, and are left out.)")
    involved = set()
    by_pair: dict[tuple[str, str], list[int]] = {}
    for ours, retail, line in differences:
        by_pair.setdefault((ours, retail), [])
        if line and line not in by_pair[(ours, retail)]:
            by_pair[(ours, retail)].append(line)
    for (ours, retail), lines in sorted(by_pair.items()):
        # The values the replay put in `ours` that are written at or near the differing lines.
        holders = [row for row in table.values() if row["physical"] == ours]
        # Prefer values written on the differing lines; then compiler temporaries with no line of
        # their own (induction offsets and the like); only then everything in that register.
        near = [row for row in holders if set(written.get(row["virtual"], [])) & set(lines)] \
            or [row for row in holders if not written.get(row["virtual"])] or holders
        described = []
        for row in near[:2]:
            involved.add(row["virtual"])
            where = written.get(row["virtual"], [])
            what = f"`{row['name']}`" if row["name"] and not row["name"].startswith("@") else \
                f"compiler temporary {row['name'] or row['virtual']}"
            code = source_lines[where[0] - 1].strip() if where and 0 < where[0] <= len(source_lines) else ""
            described.append(f"{what} ({row['virtual']}, written at line {', '.join(map(str, where[:4])) or '?'}"
                             + (f" `{code}`" if code else "") + f", colouring step {row['order']}, cost {row.get('cost')},"
                             f" adjusted {row.get('adjusted', '?')}, {row.get('degree', '?')} neighbours)")
        out.append(f"- we put {ours} where retail has {retail} (lines {', '.join(map(str, lines[:6])) or '?'}): "
                   + ("; ".join(described) or "no value found"))
    saved = [row for row in sorted(table.values(), key=lambda r: r["order"])
             if re.fullmatch(r"[rf](1[4-9]|2\d|3[01])", row["physical"])]
    if saved:
        out.append("Colouring order of values in saved registers (each takes the highest saved register its "
                   "neighbours leave free, so an earlier step gets a higher register):")
        out.append("  " + "; ".join(f"{row['order']}. {row['name'] or row['virtual']}→{row['physical']}"
                                     f"{' *' if row['virtual'] in involved else ''}" for row in saved[:16]))
        out.append("To trade registers the starred values must be coloured in the other order, or stop "
                   "interfering. The order follows spill cost, so change it by moving where a value is first "
                   "set or last used, or how often it is used, not by adding code.")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if argv[:1] == ["_dump"]:
        return _dump_main(argv[1:])
    import argparse
    parser = argparse.ArgumentParser(description="replay a function's register allocation")
    parser.add_argument("source", help="unit source as built (e.g. src/module/file.c)")
    parser.add_argument("function")
    parser.add_argument("--version", choices=REPLAY_VERSIONS, default=REPLAY_VERSIONS[0])
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if not available():
        parser.error(f"mwdbg tools missing in {MWDBG_DIR}")
    outdir = Path(args.out or tempfile.mkdtemp(prefix="mwdbg-"))
    dump(args.source, args.function, args.version, outdir)
    print(json.dumps({"out": str(outdir), "files": sorted(p.name for p in outdir.iterdir()),
                      "virtual_map": {k: sorted(v) for k, v in sorted(virtual_map(outdir).items())}}, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

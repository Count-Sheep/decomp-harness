"""Capture native MWCC preprocessing without executing the project's build graph."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import re
import shlex
import shutil
import subprocess

import harness_paths
from m2c_draft_attempts import compiler_choices
from m2c_capture_inputs import Inputs, compiler_profile, unit_name

SJISWRAP = "sjiswrap.exe"
COMPILER = "mwcceppc.exe"


def split_command(line: str) -> list[str]:
    """Split one ninja command with POSIX quoting, keeping Windows path separators.

    A backslash followed by a path character is a Windows separator (as dtk-template writes on
    Windows), not a POSIX escape; it becomes ``/``. Other escapes keep their POSIX meaning.
    """
    return shlex.split(re.sub(r"\\(?=[A-Za-z0-9_.$@+\-])", "/", line))


def _tool_name(part: str) -> str:
    return Path(part.replace("\\", "/")).name.lower()


def dependency_paths(root: Path, text: str) -> list[Path]:
    lines = text.splitlines()
    if not lines or ": " not in lines[0]:
        raise ValueError("Unsupported MWCC dependency output")
    lines[0] = lines[0].split(": ", 1)[1]
    paths = []
    for line in lines:
        value = line.strip().removesuffix("\\").rstrip().replace("\\", "/")
        if not value:
            continue
        drive = PureWindowsPath(value).drive
        if harness_paths.WINDOWS and drive:
            # Native MWCC reports real absolute paths; containment is checked below.
            value = str(PureWindowsPath(value))
        elif drive.lower() == "z:":
            # Under wibo the host root is mapped to Z:.
            value = value[2:]
        elif ":" in value:
            raise ValueError("Unsupported dependency drive")
        path = (root / value).absolute()
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Dependency escapes project root")
        paths.append(path)
    if not paths:
        raise ValueError("Empty dependency graph")
    return sorted(set(paths))


def _known_wrapper(root: Path, part: str) -> bool:
    if harness_paths.is_compiler_wrapper(part):
        return True
    configured = {str(Path(value)) for value in harness_paths.compiler_wrapper(root)}
    return str(Path(part)) in configured


def _launcher_prefix(root: Path, prefix: list[str]) -> bool:
    """An empty prefix (native MWCC), or an optional host wrapper followed by optional sjiswrap."""
    rest = list(prefix)
    if rest and _tool_name(rest[-1]) == SJISWRAP:
        rest.pop()
    return not rest or (len(rest) == 1 and _known_wrapper(root, rest[0]))


def _launcher_path(root: Path, part: str) -> str:
    path = Path(part)
    if not path.is_absolute() and len(path.parts) == 1 and not (root / path).exists():
        found = shutil.which(part)
        if found:
            return found
    return str(root / path)


def native_command(root: Path, commands: str, source: Path, selected: dict, flags: list[str]) -> list[str]:
    matches = []
    for line in commands.splitlines():
        args = split_command(line.split(" && ", 1)[0])
        if "-c" not in args:
            continue
        pos = args.index("-c")
        if pos + 1 >= len(args) or (root / args[pos + 1]).resolve() != source.resolve():
            continue
        compilers = [i for i, arg in enumerate(args[:pos]) if _tool_name(arg) == COMPILER]
        if len(compilers) != 1:
            raise ValueError("Unrecognized compiler command")
        index = compilers[0]
        prefix = args[:index]
        if not _launcher_prefix(root, prefix):
            raise ValueError("Uncaptured compiler wrapper")
        if (root / args[index]).resolve() != Path(selected["binary"]).resolve():
            raise ValueError("Build/profile compiler mismatch")
        if args[pos + 2:pos + 3] != ["-o"] or len(args[pos + 2:]) != 2:
            raise ValueError("Unexpected build output controls")
        actual = args[index + 1:pos]
        if not actual or actual[-1] != "-MMD":
            raise ValueError("Expected recorded dependency flag")
        actual = actual[:-1]
        without_includes = []
        i = 0
        while i < len(actual):
            if actual[i] == "-i" and i + 1 < len(actual):
                if not (root / actual[i + 1]).resolve().is_relative_to(root.resolve()):
                    raise ValueError("Include directory escapes project root")
                i += 2
            else:
                without_includes.append(actual[i])
                i += 1
        if without_includes != list(flags) or "-nodefaults" not in flags or "-nosyspath" not in flags:
            raise ValueError("Build/profile flags mismatch or implicit include search")
        if any(arg.startswith("@") or arg in ("-include", "-prefix", "-precompile", "-o", "-c", "-E", "-EP", "-M")
               for arg in flags):
            raise ValueError("Uncaptured preprocessing controls")
        matches.append([_launcher_path(root, part) for part in args[:index + 1]] + actual)
    if len(matches) != 1:
        raise ValueError("Expected one recorded source compilation")
    return matches[0]


def run(root: Path, unit: str, source: Path, output: Path, inputs: Inputs) -> str:
    root, source, output = root.resolve(), source.absolute(), output.absolute()
    if output.exists():
        raise ValueError("Use a fresh preprocessing output")
    config = json.loads(inputs.read(root / "objdiff.json"))
    owners = [row for row in config["units"] if row["name"] == unit]
    if len(owners) != 1:
        raise ValueError("Expected one preprocessing owner")
    owner = owners[0]
    if (root / owner.get("metadata", {}).get("source_path", "")).resolve() != source.resolve():
        raise ValueError("Source is not the recorded owner source")
    inputs.read(root / "build.ninja")
    inputs.read(source)
    profile = compiler_profile(root, unit_name(unit), config, inputs)
    choices = compiler_choices(root, {"symbol": unit, "compiler_profile": profile})
    if len(choices) != 1:
        raise ValueError("Expected one recorded compiler")
    _, flags, selected = choices[0]
    listing = subprocess.run(["ninja", "-t", "commands", owner["base_path"]], cwd=root,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=30, check=True)
    command = native_command(root, listing.stdout, source, selected, flags)
    for part in command:
        if harness_paths.is_compiler_wrapper(part) or _tool_name(part) in (SJISWRAP, COMPILER) \
                or _known_wrapper(root, part):
            inputs.read(Path(part))
    output.mkdir(parents=True)
    record = {"unit": unit, "compiler_profile": profile, "commands": [], "state": "invalid"}

    def invoke(mode: str, name: str) -> str:
        args = [*command, mode, str(source.relative_to(root))]
        record["commands"].append(args)
        process = subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8",
                                 timeout=60)
        (output / (name + ".stdout")).write_text(process.stdout, encoding="utf-8", newline="\n")
        (output / (name + ".stderr")).write_text(process.stderr, encoding="utf-8", newline="\n")
        inputs.read(output / (name + ".stdout"))
        inputs.read(output / (name + ".stderr"))
        if process.returncode:
            raise ValueError("Native preprocessing failed: " + process.stderr[-1000:] + process.stdout[-1000:])
        return process.stdout

    try:
        before = dependency_paths(root, invoke("-M", "dependencies-before"))
        if source not in before:
            raise ValueError("Dependency graph omits source")
        for path in before:
            inputs.read(path)
        text = invoke("-E", "preprocessed")
        after = dependency_paths(root, invoke("-M", "dependencies-after"))
        if before != after:
            raise ValueError("Dependency graph changed during preprocessing")
        inputs.verify()
        record.update(state="captured", dependencies=[str(path) for path in before])
        (output / "source.c").write_text(text, encoding="utf-8", newline="\n")
        inputs.read(output / "source.c")
        return text
    finally:
        record["inputs"] = dict(inputs.files)
        (output / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        inputs.read(output / "result.json")

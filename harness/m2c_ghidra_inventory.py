#!/usr/bin/env python3
"""Create or refresh a hash-bound Ghidra inventory for a stripped binary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


HEADLESS_NAMES = ("analyzeHeadless.bat", "analyzeHeadless") if os.name == "nt" else ("analyzeHeadless",)


def executable(explicit: Path | None = None) -> Path:
    candidates = [explicit]
    if os.environ.get("GHIDRA_HOME"):
        candidates.extend(Path(os.environ["GHIDRA_HOME"]) / "support" / name for name in HEADLESS_NAMES)
    found = shutil.which("analyzeHeadless")
    candidates.append(Path(found) if found else None)
    # Homebrew's versioned install location on macOS.
    candidates.extend(sorted(Path("/opt/homebrew/Cellar/ghidra").glob("*/libexec/support/analyzeHeadless"),
                             reverse=True))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("analyzeHeadless not found; set GHIDRA_HOME or --analyze-headless")


def command(tool: Path, binary: Path, workspace: Path, project: str, output: Path,
            script_path: Path, log: Path, existing: bool) -> list[str]:
    action = ["-process", binary.name, "-readOnly"] if existing else ["-import", str(binary)]
    return [str(tool), str(workspace), project, *action,
            "-scriptPath", str(script_path), "-postScript", "GhidraExportInventory.java",
            str(output), "-log", str(log)]


def run(binary: Path, workspace: Path, project: str, output: Path, script_path: Path,
        log: Path, tool: Path | None = None) -> dict:
    binary = binary.resolve()
    script_path = script_path.resolve()
    output = output.resolve()
    log = log.resolve()
    workspace = workspace.resolve()
    if not binary.is_file():
        raise FileNotFoundError(binary)
    workspace.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    tool = executable(tool)
    existing = (workspace / f"{project}.gpr").is_file()
    invocation = command(tool, binary, workspace, project, output, script_path, log, existing)
    completed = subprocess.run(invocation, text=True, errors="replace", capture_output=True, timeout=3600)
    if completed.returncode:
        raise RuntimeError((completed.stdout + "\n" + completed.stderr)[-4000:])
    try:
        report = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Ghidra did not produce a valid inventory: {output}") from error
    expected = hashlib.sha256(binary.read_bytes()).hexdigest()
    if str(report.get("input_sha256") or "").lower() != expected:
        output.unlink(missing_ok=True)
        raise ValueError("Ghidra inventory input hash does not match the supplied binary")
    return {"status": "ok", "mode": "refresh" if existing else "import",
            "binary_sha256": expected, "functions": report.get("function_count"),
            "schema_version": report.get("schema_version"), "output": str(output),
            "log": str(log), "command": invocation}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--script-path", type=Path, default=Path(__file__).parent)
    parser.add_argument("--analyze-headless", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.binary, args.workspace, args.project, args.output,
                         args.script_path, args.log, args.analyze_headless), indent=2))


if __name__ == "__main__":
    main()

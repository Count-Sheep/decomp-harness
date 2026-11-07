#!/usr/bin/env python3
"""Run the harness and dashboard test suites on any platform.

    python scripts/run_tests.py            # everything
    python scripts/run_tests.py harness    # one suite: harness or dashboard

Node tests for the dashboard frontend run when ``node`` is on PATH and are skipped otherwise.
No decomp checkout, retail files or network access is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path) -> bool:
    print(f"\n$ {' '.join(command)}  (in {cwd.relative_to(REPO) if cwd != REPO else '.'})", flush=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # Tests never read a developer's real configuration.
    env.pop("DECOMP_HARNESS_CONFIG", None)
    for name in ("DECOMP_PROJECT", "DECOMP_ROOT", "DECOMP_VERSION"):
        env.pop(name, None)
    env["DECOMP_HARNESS_CONFIG"] = str(REPO / "harness.example.json")
    return subprocess.run(command, cwd=cwd, env=env).returncode == 0


def harness() -> bool:
    return run([sys.executable, "-m", "unittest", "discover", "-s", ".", "-t", ".", "-p", "test_*.py"],
               REPO / "harness")


def dashboard() -> bool:
    tests = REPO / "dashboard" / "tests"
    if not tests.is_dir():
        return True
    ok = run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
             REPO / "dashboard")
    node = shutil.which("node")
    scripts = sorted(str(path.relative_to(REPO / "dashboard")) for path in tests.glob("test_*.cjs"))
    if node and scripts:
        ok &= run([node, "--test", *scripts], REPO / "dashboard")
    elif scripts:
        print("node not found; skipping dashboard frontend tests")
    return ok


def main(argv: list[str]) -> int:
    suites = {"harness": harness, "dashboard": dashboard}
    chosen = argv or list(suites)
    unknown = [name for name in chosen if name not in suites]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}; choose from {', '.join(suites)}")
        return 2
    results = {name: suites[name]() for name in chosen}
    print("\n" + "  ".join(f"{name}: {'ok' if ok else 'FAILED'}" for name, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

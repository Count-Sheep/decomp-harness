"""Use the decomp's existing assembly-similarity engine for matched C retrieval.

The similarity engine (``simindex_lib``/``query``/``build_index.py``) is an optional project-provided
tool: ``tools.simindex`` in the harness config (or DECOMP_SIMINDEX), defaulting to the decomp's
``tools/decomp_work/simindex``. Its corpus name for this project is ``tools.simindex_corpus``
(or DECOMP_SIMINDEX_CORPUS), defaulting to the project id. Without the tool, retrieval returns nothing.

The optional numpy dependency stays in an isolated tool environment, not the
campaign worker. Query results are advisory and rechecked against the live report.
"""

from __future__ import annotations

import argparse
import json
import re
import difflib
import subprocess
import sys
from pathlib import Path

import harness_paths
from harness_paths import DECOMP_ROOT, REPO_ROOT, VERSION

_RESULTS: dict[tuple, dict] = {}


def core_shape(norm: list[str]) -> list[str]:
    """Exclude generic frame scaffolding so it cannot masquerade as a useful C template."""
    stack = next((match.group(1) for line in norm
                  if (match := re.match(r"stwu (G\d+),", line))), None)
    core = []
    for line in norm:
        opcode = line.split()[0]
        if opcode in {"stwu", "mflr", "mtlr", "blr", "stmw", "lmw"}:
            continue
        if stack and (line.endswith(f"({stack})") or line.startswith(f"addi {stack},{stack},")):
            continue
        core.append(line)
    return core


def tool_python() -> str:
    """The interpreter of the isolated analysis environment (DECOMP_ANALYSIS_PYTHON overrides it)."""
    return harness_paths.env("ANALYSIS_PYTHON") or str(harness_paths.venv_python(REPO_ROOT / "build/tools/analysis-venv"))


def simindex_library(root: Path) -> Path:
    """The project's similarity-index tool directory (configured, else the decomp's own copy)."""
    return harness_paths.tool_path("simindex") or root / "tools/decomp_work/simindex"


def corpus_name() -> str:
    """The similarity index's corpus name for the selected project."""
    return harness_paths.env("SIMINDEX_CORPUS") or str(
        harness_paths.setting("tools", "simindex_corpus", default=harness_paths.PROJECT_ID))


def asm_relative(version: str, asm_file: str) -> str:
    return (Path("build") / version / "asm" / asm_file).as_posix()


def assembly_current(root: Path, relative: str | None, index_mtime_ns: int) -> bool:
    if not relative:
        return False
    path = root / relative
    try:
        return path.is_file() and path.stat().st_mtime_ns <= index_mtime_ns
    except OSError:
        return False


def refresh_matches(index: dict, root: Path, index_mtime_ns: int, version: str | None = None,
                    corpus: str | None = None) -> None:
    """Refresh eligibility without recomputing unchanged assembly fingerprints."""
    from local_campaign_decompile_batch import targets

    version = version or VERSION
    corpus = corpus or corpus_name()
    report = json.loads((root / "build" / version / "report.json").read_text(encoding="utf-8"))
    matched = {}
    for row in targets(report, "matched"):
        try:
            address = int(str(row["address"]), 16 if str(row["address"]).startswith("0x") else 10)
        except ValueError:
            continue
        matched[(row["symbol"], address)] = row
    current = {}
    for fn in index["functions"]:
        if fn["corpus"] != corpus:
            continue
        relative = asm_relative(version, fn.get("asm_file") or "")
        if relative not in current:
            current[relative] = assembly_current(root, relative, index_mtime_ns)
        row = matched.get((fn["name"], fn["addr"])) if current[relative] else None
        fn.update(fuzzy=100.0 if row else 0.0,
                  src_path=row["source"] if row else None,
                  unit=row["unit"] if row else None)


def matched_templates(root: Path, symbol: str, version: str | None = None) -> list[dict]:
    version = version or VERSION
    index = root / "build/simindex/index.pkl"
    report = root / "build" / version / "report.json"
    try:
        if not Path(tool_python()).is_file() or not index.is_file() or not report.is_file():
            return []
        key = (str(root), symbol, index.stat().st_mtime_ns, report.stat().st_mtime_ns)
        cached = _RESULTS.get(key)
        if cached is None or not all(assembly_current(root, path, key[2]) for path in cached["assembly_files"]):
            result = subprocess.run([tool_python(), str(Path(__file__).resolve()), "query", symbol,
                                     "--root", str(root), "--version", version, "--metadata"],
                                    capture_output=True, text=True, timeout=30)
            if len(_RESULTS) >= 512:
                _RESULTS.clear()
            if result.returncode:
                return []
            _RESULTS[key] = json.loads(result.stdout)
        if index.stat().st_mtime_ns != key[2] or report.stat().st_mtime_ns != key[3]:
            return []
        if not all(assembly_current(root, path, key[2]) for path in _RESULTS[key]["assembly_files"]):
            return []
        return _RESULTS[key]["references"]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "query"))
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT)
    parser.add_argument("--version", default=VERSION, help="build version (default: the selected project's)")
    parser.add_argument("--metadata", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.version:
        parser.error("no build version: configure a project or pass --version")
    library = simindex_library(args.root)
    if not library.is_dir():
        parser.error(f"similarity index tool not found at {library} (configure tools.simindex)")
    if args.command == "build":
        return subprocess.run([sys.executable, str(library / "build_index.py")], cwd=args.root).returncode
    if not args.symbol:
        parser.error("query requires a symbol")
    # Only this subprocess imports the optional index dependencies and local pickle.
    sys.path.insert(0, str(library))
    import simindex_lib as lib
    from query import top_similar
    index_path = args.root / "build/simindex/index.pkl"
    index_mtime_ns = index_path.stat().st_mtime_ns
    index = lib.load_index(str(index_path))
    corpus = corpus_name()
    refresh_matches(index, args.root, index_mtime_ns, args.version, corpus)
    qid = lib.resolve(index, args.symbol, corpus=corpus)
    target_asm = (asm_relative(args.version, index["functions"][qid]["asm_file"])
                  if qid is not None else None)
    if not assembly_current(args.root, target_asm, index_mtime_ns):
        print(json.dumps({"references": [], "assembly_files": []} if args.metadata else []))
        return 0
    rows = []
    target_core = core_shape(index["functions"][qid]["norm"])
    for score, ratio, _, fn in top_similar(index, qid, k=12, corpus=corpus, matched_only=True):
        if ratio < 0.65 or score < 0.60 or fn["n_insns"] < 6:
            continue
        reference_core = core_shape(fn["norm"])
        body_ratio = difflib.SequenceMatcher(None, target_core, reference_core).ratio()
        if min(len(target_core), len(reference_core)) < 4 or body_ratio < 0.65:
            continue
        rows.append({"symbol": fn["name"], "source": fn["src_path"], "similarity": round(ratio, 4),
                     "body_similarity": round(body_ratio, 4),
                     "asm_path": asm_relative(args.version, fn["asm_file"])})
    print(json.dumps({"references": rows, "assembly_files": [target_asm, *(row["asm_path"] for row in rows)]}
                     if args.metadata else rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

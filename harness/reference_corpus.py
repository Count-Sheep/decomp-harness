"""Build reproducible reference corpora from compatible public decomps.

Decomp repositories intentionally do not ship retail binaries. This importer only activates
source that upstream marks matching (or that a corpus entry explicitly lists) and that compiles
locally into a PowerPC object. Generated assembly therefore comes from exact reconstructed
source, not from a guessed or unavailable disc image.

Nothing is built in: corpora come from the ``reference_corpora`` list of the harness config.
Each entry describes one source repository::

    {
      "name": "sdk",                         # checkout directory and default corpus name
      "url": "https://example.invalid/sdk.git",
      "license": "CC0-1.0",                  # recorded in the registry, never interpreted
      "role": "SDK source",                  # free text, recorded in the registry
      "configure": false,                    # run `python configure.py` when build.ninja is missing
      "corpus": "sdk",                       # corpus name (default: name); omit "jobs" to stage
      "jobs": {                              # source-only, without generating assembly
        "kind": "sources",                   # "sources": compile every matching file
        "sources": ["src/**/*.c"],           # globs relative to the checkout
        "compiler": "GC/1.2.5",              # build/compilers/<compiler>/mwcceppc.exe
        "flags": "-O4,p -I- -Iinclude",      # string (POSIX quoting) or list
        "object_dir": "build/reference-objects/sdk"
      }
    }

``"kind": "configure"`` instead reads a dtk-template ``configure.py`` without executing it and
takes every literal ``Object(Matching, "path")`` inside calls to the functions named by
``libraries`` (``{"FunctionName": "GC/1.2.5n", ...}``, the compiler for that call). ``source``
maps an object path to its source file (``{library}`` = first path component, ``{relative}`` =
the rest, ``{path}`` = the whole path; default ``src/{path}``) and ``object_flags`` adds flags
for individual object paths.

Compilers and dtk come from a dtk-template toolchain checkout: ``tools.reference_toolchain``
(``DECOMP_REFERENCE_TOOLCHAIN``), else the selected project root. MWCC runs through
``compiler_wrapper`` (nothing on Windows).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from harness_paths import DECOMP_ROOT, REPO_ROOT, build_tool, compiler_wrapper, python_command, setting, tool_path


REFERENCE_ROOT = REPO_ROOT / "build/decomp-support/reference-projects"
CORPUS_ROOT = REPO_ROOT / "build/decomp-support/reference-corpora"
REGISTRY = CORPUS_ROOT / "registry.json"
VERSION = "REFERENCE"

FUNCTION = re.compile(
    r"(?m)^# \.text:0x([0-9A-Fa-f]+) \| size: 0x([0-9A-Fa-f]+)\s*$"
    r"\n\.fn\s+([^,\s]+),"
)
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
# decomp.me / objdiff scratch compiler ids for common MWCC builds; entries may override.
MWCC_NAMES = {"1.2.5": "mwcc_233_163", "1.2.5n": "mwcc_233_163n"}
REQUIRED_FIELDS = ("name", "url", "license", "role")


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def command(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def revision(root: Path) -> str | None:
    result = command(["git", "rev-parse", "HEAD"], root, 10)
    return result.stdout.strip() if result.returncode == 0 else None


def repositories(configured: Any = None) -> list[dict[str, Any]]:
    """The configured corpus sources; an error when none are configured."""
    value = setting("reference_corpora", default=[]) if configured is None else configured
    if not value:
        raise SystemExit("No reference corpora are configured. Add entries to \"reference_corpora\" in the "
                         "harness config (see the reference_corpus module docstring for the fields).")
    if not isinstance(value, list):
        raise SystemExit("reference_corpora must be a list of objects")
    rows, names = [], set()
    for entry in value:
        if not isinstance(entry, dict) or any(not entry.get(field) for field in REQUIRED_FIELDS):
            raise SystemExit("Each reference corpus needs " + ", ".join(REQUIRED_FIELDS) + f": {entry!r}")
        name = str(entry["name"])
        if SAFE_NAME.sub("_", name) != name or name in (".", ".."):
            raise SystemExit(f"Reference corpus name must be a plain directory name: {name!r}")
        if name in names:
            raise SystemExit(f"Duplicate reference corpus name: {name!r}")
        names.add(name)
        rows.append(dict(entry))
    return rows


def sync(refresh: bool = False, items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Clone small source-only checkouts and optionally fast-forward them."""
    items = repositories() if items is None else items
    REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in items:
        root = REFERENCE_ROOT / item["name"]
        if not (root / ".git").is_dir():
            result = command(
                ["git", "clone", "--depth", "1", "--filter=blob:none", item["url"], str(root)],
                REFERENCE_ROOT,
                600,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"failed to clone {item['name']}")
        elif refresh:
            result = command(["git", "pull", "--ff-only"], root, 300)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"failed to update {item['name']}")
        if item.get("configure") and not (root / "build.ninja").is_file():
            result = command([python_command(), "configure.py"], root, 120)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"failed to configure {item['name']}")
        rows.append({**item, "root": str(root.resolve()), "revision": revision(root)})
    return rows


def toolchain_root() -> Path:
    """A dtk-template checkout providing ``build/tools`` and ``build/compilers``."""
    return tool_path("reference_toolchain", DECOMP_ROOT)


def executable(name: str) -> Path:
    path = build_tool(toolchain_root(), name)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.resolve()


def compiler(version: str) -> Path:
    path = toolchain_root() / "build/compilers" / version / "mwcceppc.exe"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.resolve()


def _flags(value: Any) -> list[str]:
    if value is None:
        return []
    return shlex.split(value) if isinstance(value, str) else [str(flag) for flag in value]


def source_jobs(name: str, root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Every source file matching the entry's globs, compiled with one flag set."""
    flags = _flags(spec.get("flags"))
    object_dir = root / spec.get("object_dir", f"build/reference-objects/{name}")
    sources = set()
    for pattern in spec.get("sources") or ["src/**/*.c"]:
        sources.update(path for path in root.glob(pattern) if path.is_file())
    jobs = []
    for source in sorted(sources):
        relative = source.relative_to(root)
        jobs.append({"project": name, "root": root, "source": source,
                     "object": object_dir / relative.with_suffix(".o"),
                     "compiler_version": spec["compiler"], "flags": flags})
    return jobs


def _matching_object(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name) or call.func.id != "Object" or len(call.args) < 2:
        return None
    status, path = call.args[:2]
    if not isinstance(status, ast.Name) or status.id != "Matching":
        return None
    return path.value if isinstance(path, ast.Constant) and isinstance(path.value, str) else None


def configure_objects(configure: Path, libraries: dict[str, str]) -> list[tuple[str, str]]:
    """Return (path, compiler) for literal matching objects inside the named library calls."""
    tree = ast.parse(configure.read_text(encoding="utf-8"), filename=str(configure))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in libraries or len(node.args) < 2:
            continue
        objects = node.args[1]
        if not isinstance(objects, (ast.List, ast.Tuple)):
            continue
        for element in objects.elts:
            if isinstance(element, ast.Call):
                path = _matching_object(element)
                if path and path.endswith((".c", ".cpp", ".cp")):
                    rows.append((path, libraries[node.func.id]))
    return sorted(set(rows))


def _configure_source(root: Path, template: str, object_path: str) -> Path:
    library, _, relative = object_path.partition("/")
    return root / template.format(path=object_path, library=library, relative=relative)


def configure_jobs(name: str, root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Matching objects declared in the corpus's own configure.py."""
    libraries = spec.get("libraries") or {}
    if not isinstance(libraries, dict) or not libraries:
        raise SystemExit(f"Reference corpus {name}: \"configure\" jobs need a libraries mapping")
    flags = _flags(spec.get("flags"))
    extra = {path: _flags(value) for path, value in (spec.get("object_flags") or {}).items()}
    object_dir = root / spec.get("object_dir", f"build/reference-objects/{name}")
    jobs = []
    for object_path, version in configure_objects(root / spec.get("configure_file", "configure.py"), libraries):
        source = _configure_source(root, spec.get("source", "src/{path}"), object_path)
        if not source.is_file():
            continue
        jobs.append({"project": name, "root": root, "source": source,
                     "object": object_dir / Path(object_path).with_suffix(".o"),
                     "compiler_version": version, "flags": [*flags, *extra.get(object_path, [])]})
    return jobs


def corpus_jobs(item: dict[str, Any]) -> list[dict[str, Any]]:
    spec = item.get("jobs") or {}
    root = Path(item["root"])
    name = str(item.get("corpus") or item["name"])
    kind = spec.get("kind", "sources")
    if kind == "configure":
        return configure_jobs(name, root, spec)
    if kind == "sources":
        if not spec.get("compiler"):
            raise SystemExit(f"Reference corpus {name}: \"sources\" jobs need a compiler")
        return source_jobs(name, root, spec)
    raise SystemExit(f"Reference corpus {name}: unknown jobs kind {kind!r}")


def compile_job(job: dict[str, Any]) -> dict[str, Any]:
    job["object"].parent.mkdir(parents=True, exist_ok=True)
    args = [*compiler_wrapper(toolchain_root()), str(compiler(job["compiler_version"])), *job["flags"],
            "-c", job["source"].relative_to(job["root"]).as_posix(), "-o",
            job["object"].relative_to(job["root"]).as_posix()]
    result = command(args, job["root"], 120)
    return {**job, "compile_command": args, "compiled": result.returncode == 0,
            "error": result.stderr[-2000:] if result.returncode else None}


def compile_jobs(jobs: Iterable[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    completed = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(compile_job, job) for job in jobs]
        for future in as_completed(futures):
            completed.append(future.result())
    return sorted(completed, key=lambda row: str(row["source"]))


def parse_disassembly(text: str) -> list[dict[str, Any]]:
    return [
        {"name": match.group(3), "virtual_address": int(match.group(1), 16),
         "size": int(match.group(2), 16), "fuzzy_match_percent": 100.0}
        for match in FUNCTION.finditer(text)
        if int(match.group(2), 16) > 0
    ]


def scratch_compiler(version: str, configured: dict[str, str] | None = None) -> str:
    """The objdiff scratch compiler id for an MWCC build directory such as ``GC/1.2.5n``."""
    names = {**MWCC_NAMES, **(configured or {})}
    return names.get(version) or names.get(Path(version).name) or Path(version).name


def import_corpus(name: str, rows: list[dict[str, Any]],
                  scratch_names: dict[str, str] | None = None) -> dict[str, Any]:
    root = CORPUS_ROOT / name
    asm_root = root / "build" / VERSION / "asm"
    asm_root.mkdir(parents=True, exist_ok=True)
    units, objdiff_units, failures = [], [], []
    for row in rows:
        if not row.get("compiled") or not row["object"].is_file():
            failures.append({"source": str(row["source"]), "error": row.get("error")})
            continue
        relative = row["source"].relative_to(row["root"])
        unit_name = SAFE_NAME.sub("_", relative.with_suffix("").as_posix())
        output = asm_root / f"{unit_name}.s"
        result = command([str(executable("dtk")), "elf", "disasm", str(row["object"]), str(output)],
                         row["root"], 60)
        if result.returncode:
            failures.append({"source": str(row["source"]), "error": result.stderr[-2000:]})
            continue
        functions = parse_disassembly(output.read_text(encoding="utf-8", errors="replace"))
        if not functions:
            output.unlink(missing_ok=True)
            continue
        source_path = str(row["source"].resolve())
        units.append({"name": unit_name, "metadata": {"source_path": source_path, "complete": True},
                      "functions": functions})
        objdiff_units.append({"name": unit_name, "metadata": {"source_path": source_path},
                              "scratch": {"compiler": scratch_compiler(row["compiler_version"], scratch_names),
                                          "c_flags": shlex.join(row["flags"])}})
    report = {"schema_version": 1, "generated_at": now(), "units": units,
              "measures": {"matched_functions": sum(len(unit["functions"]) for unit in units)}}
    write_json(root / "build" / VERSION / "report.json", report)
    write_json(root / "objdiff.json", {"units": objdiff_units})
    return {"name": name, "status": "active", "root": str(root.resolve()), "version": VERSION,
            "objects": len(units), "functions": report["measures"]["matched_functions"],
            "compile_failures": len(failures), "failures": failures[:20]}


def build(workers: int = 4) -> dict[str, Any]:
    sources = sync(False)
    active, staged = [], []
    for item in sources:
        if not item.get("jobs"):
            staged.append({**item, "status": "source_only",
                           "reason": item.get("reason") or "no compile jobs configured"})
            continue
        name = str(item.get("corpus") or item["name"])
        compiled = compile_jobs(corpus_jobs(item), workers)
        active.append(import_corpus(name, compiled, (item.get("jobs") or {}).get("scratch_compilers")))
    registry = {"schema_version": 1, "generated_at": now(), "projects": active,
                "staged_projects": staged, "source_repositories": sources}
    write_json(REGISTRY, registry)
    return registry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="action", required=True)
    sync_parser = subparsers.add_parser("sync", help="clone or fast-forward the configured source repos")
    sync_parser.add_argument("--refresh", action="store_true")
    build_parser = subparsers.add_parser("build", help="compile and index disc-independent exact corpora")
    build_parser.add_argument("--jobs", type=int, default=4)
    subparsers.add_parser("status", help="print the installed corpus registry")
    args = parser.parse_args()
    if args.action == "sync":
        value = {"source_repositories": sync(args.refresh)}
    elif args.action == "build":
        value = build(max(1, min(args.jobs, 8)))
    else:
        value = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.is_file() else {}
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

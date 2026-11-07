"""Shared test setup: an isolated, empty harness config and neutral TEST01 fixture projects.

Imported first by every test module so ``harness_paths`` never reads a developer's harness.local.json.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
_folder = Path(tempfile.mkdtemp(prefix="dashboard-tests-"))
atexit.register(shutil.rmtree, _folder, True)
(_folder / "harness.json").write_text('{"projects": []}', encoding="utf-8")
os.environ["DECOMP_HARNESS_CONFIG"] = str(_folder / "harness.json")
for _name in ("DECOMP_PROJECT", "DECOMP_ROOT", "DECOMP_VERSION"):
    os.environ.pop(_name, None)
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

import providers  # noqa: E402  (after the environment is isolated)

VERSION = "TEST01"
MEASURES = {"fuzzy_match_percent": 62.5, "total_code": 4096, "matched_code": 1024, "matched_code_percent": 25.0,
            "total_functions": 40, "matched_functions": 10, "matched_functions_percent": 25.0,
            "complete_code": 512, "complete_code_percent": 12.5}


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def now() -> str:
    return datetime.now(UTC).isoformat()


def make_project(root: Path, *, version: str = VERSION, exact: bool = True, measures=None, report=True) -> Path:
    """A dtk-shaped checkout: build manifest, main output, objdiff report and an m2c report."""
    output = root / "build" / version / "main.dol"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"\x00binary fixture\x01" * 64)
    expected = hashlib.sha1(output.read_bytes()).hexdigest() if exact else "0" * 40
    manifest = root / "config" / version / "build.sha1"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"{expected}  build/{version}/main.dol\n", encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "unit_a.c").write_text("int fn_a(void) { return 0; }\n", encoding="utf-8")
    if report:
        write_json(root / "build" / version / "report.json", {
            "measures": measures or MEASURES,
            "categories": [{"id": "core", "name": "Core", "measures": {"fuzzy_match_percent": 70.0, "total_code": 100}}],
            "units": []})
    support = root / "build" / "decomp-support" / version
    write_json(support / "m2c-report.json", {
        "schema_version": 1, "generated_at": now(), "history_count": 1,
        "runs": [{"run_id": "run-1", "selection": "unmatched", "updated_at": now(), "total": 10, "processed": 10,
                  "counts": {"ok": 8, "error": 2}, "validation": {"total": 8, "compiler_succeeded": 6, "comparable": True,
                                                                  "counts": {"non_exact": 4, "scorer_exact": 2}}}],
        "latest": {"run_id": "run-1", "selection": "unmatched", "updated_at": now(), "total": 10, "processed": 10,
                   "counts": {"ok": 8, "error": 2},
                   "validation": {"total": 8, "compiler_succeeded": 6, "comparable": True,
                                  "counts": {"non_exact": 4, "scorer_exact": 2},
                                  "refinement": {"unit_gate": {"promoted": 1, "visible_units": {"state": "nothing_to_promote"}}}}},
        "matched_reference_pool": {"available": 3, "similarity_index_ready": False}})
    return root


def tree_digest(root: Path) -> dict[str, str]:
    """Every file below ``root`` with its content hash, to prove a run wrote nothing."""
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def project(root: Path, project_id: str = "proj", versions=(VERSION,)) -> providers.Project:
    return providers.Project(id=project_id, name=f"Project {project_id}", root=Path(root).resolve(),
                             versions=tuple(versions), default_version=versions[0])

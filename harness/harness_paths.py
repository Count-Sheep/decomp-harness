"""Where the harness, its configuration and the plugged-in decomp projects live.

The harness ships with no project. Decomps are plugged in through a JSON config file:

1. ``DECOMP_HARNESS_CONFIG`` names the file explicitly; otherwise
2. ``harness.local.json`` at the repository root is used (untracked, per machine).

See ``harness.example.json`` for the schema. Relative paths in the config resolve against the
config file's directory. Environment overrides, applied after the config:

``DECOMP_PROJECT``  id of the configured project to select (default: ``default_project``, then the first)
``DECOMP_ROOT``     checkout root of the selected project (also enough on its own, without a config)
``DECOMP_VERSION``  build version of the selected project (default: the configured version, else the
                    single ``config/<VERSION>/build.sha1`` found in the root)

Projects are expected to follow the decomp-toolkit (dtk) template layout: ``config/<VERSION>/``,
``build/<VERSION>/report.json``, ``build/tools/`` and ``build/compilers/``. All harness state for a
project stays in that project's ignored ``build/`` directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parent
LOCAL_CONFIG = REPO_ROOT / "harness.local.json"
WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Project:
    id: str
    root: Path
    version: str
    name: str = ""
    versions: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    settings: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def build_dir(self) -> Path:
        return self.root / "build" / self.version

    @property
    def support_dir(self) -> Path:
        return self.root / "build" / "decomp-support" / self.version


def env(name: str, default: str | None = None) -> str | None:
    """A ``DECOMP_<NAME>`` environment override, or ``default``."""
    value = os.environ.get("DECOMP_" + name.upper())
    return value if value not in (None, "") else default


def _config_path() -> Path | None:
    configured = os.environ.get("DECOMP_HARNESS_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return LOCAL_CONFIG if LOCAL_CONFIG.is_file() else None


def _load_config() -> tuple[dict[str, Any], Path]:
    path = _config_path()
    if path is None:
        return {}, REPO_ROOT
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot read harness config {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"Harness config {path} must be a JSON object")
    return value, path.parent


CONFIG, CONFIG_DIR = _load_config()


def resolve(value: str | os.PathLike[str] | None, base: Path | None = None) -> Path | None:
    """Resolve a configured path: ``~`` expands, relative paths are against the config directory."""
    if value in (None, ""):
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (base or CONFIG_DIR) / path).resolve()


def detect_versions(root: Path) -> list[str]:
    """Versions with a dtk ``config/<VERSION>/build.sha1`` in ``root``, sorted."""
    try:
        return sorted(path.parent.name for path in (root / "config").glob("*/build.sha1"))
    except OSError:
        return []


def _project(entry: dict[str, Any]) -> Project:
    if not isinstance(entry, dict) or not entry.get("id") or not entry.get("root"):
        raise SystemExit("Each configured project needs at least an id and a root")
    root = resolve(entry["root"])
    versions = tuple(str(item) for item in entry.get("versions") or ())
    version = str(entry.get("version") or (versions[0] if versions else "")) or \
        next(iter(detect_versions(root)), "")
    return Project(id=str(entry["id"]), root=root, version=version, name=str(entry.get("name") or entry["id"]),
                   versions=versions or ((version,) if version else ()),
                   references=tuple(str(item) for item in entry.get("references") or ()),
                   settings={key: value for key, value in entry.items()
                             if key not in ("id", "root", "version", "versions", "name", "references")})


def projects() -> list[Project]:
    """Every configured project, in config order."""
    return [_project(entry) for entry in CONFIG.get("projects") or []]


def project(project_id: str) -> Project | None:
    return next((item for item in projects() if item.id == project_id), None)


def _selected() -> Project:
    configured = projects()
    wanted = env("PROJECT") or CONFIG.get("default_project")
    chosen = next((item for item in configured if item.id == wanted), None) if wanted else None
    if wanted and chosen is None and configured:
        raise SystemExit(f"DECOMP_PROJECT/default_project {wanted!r} is not a configured project")
    chosen = chosen or (configured[0] if configured else None)
    root = resolve(env("ROOT"), Path.cwd()) or (chosen.root if chosen else None)
    if root is None:
        # Nothing configured: run against the current directory when it is a dtk checkout.
        root = Path.cwd().resolve() if detect_versions(Path.cwd()) else (REPO_ROOT.parent / "decomp").resolve()
    version = env("VERSION") or (chosen.version if chosen and chosen.root == root else "") or \
        next(iter(detect_versions(root)), "")
    if chosen and chosen.root == root:
        return Project(chosen.id, root, version, chosen.name, chosen.versions or (version,),
                       chosen.references, chosen.settings)
    return Project(chosen.id if chosen else "default", root, version, chosen.name if chosen else root.name,
                   (version,) if version else ())


SELECTED = _selected()
DECOMP_ROOT = SELECTED.root
VERSION = SELECTED.version
PROJECT_ID = SELECTED.id


def reference_projects(selected: Project | None = None) -> list[Project]:
    """Other configured projects usable as cross-project evidence for ``selected``.

    A project's ``references`` list restricts and orders them; without one, every other
    configured project with a root on disk is used.
    """
    selected = selected or SELECTED
    others = [item for item in projects() if item.id != selected.id and item.root != selected.root]
    if selected.references:
        by_id = {item.id: item for item in others}
        others = [by_id[name] for name in selected.references if name in by_id]
    return [item for item in others if item.root.is_dir()]


def project_for_version(version: str) -> Project | None:
    """The configured project building ``version``; the selected project when it matches."""
    if version and version == SELECTED.version:
        return SELECTED
    return next((item for item in projects() if version in item.versions or version == item.version), None)


def project_id_for_version(version: str, default: str = "project") -> str:
    found = project_for_version(version)
    return found.id if found else default


def setting(*keys: str, default: Any = None) -> Any:
    """A nested top-level config value, e.g. ``setting("tools", "m2c")``."""
    value: Any = CONFIG
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def tool_path(name: str, default: str | os.PathLike[str] | None = None) -> Path | None:
    """A configured tool location (``tools.<name>``), ``DECOMP_<NAME>`` env, or ``default``."""
    configured = env(name) or setting("tools", name)
    if configured:
        return resolve(configured, CONFIG_DIR if not env(name) else Path.cwd())
    return Path(default).resolve() if default is not None else None


def exe(path: Path) -> Path:
    """``path`` with ``.exe`` appended on Windows when only that file exists."""
    if WINDOWS and path.suffix.lower() != ".exe" and not path.exists():
        candidate = path.with_name(path.name + ".exe")
        if candidate.exists():
            return candidate
    return path


def build_tool(root: Path, name: str) -> Path:
    """A dtk-template tool such as ``objdiff-cli`` or ``dtk`` under ``root/build/tools``."""
    return exe(root / "build" / "tools" / name)


def compiler_wrapper(root: Path) -> list[str]:
    """The prefix that runs Windows compiler executables on this host.

    Empty on Windows, where MWCC runs natively. Elsewhere it is ``build/tools/wibo`` when present,
    then ``wibo`` or ``wine`` on PATH. An explicit ``tools.compiler_wrapper`` config value (a string or
    list; an empty list disables wrapping) always wins.
    """
    configured = setting("tools", "compiler_wrapper")
    if configured is not None:
        return [configured] if isinstance(configured, str) else [str(item) for item in configured]
    if WINDOWS:
        return []
    local = root / "build" / "tools" / "wibo"
    if local.exists():
        return [str(local)]
    for name in ("wibo", "wine"):
        found = shutil.which(name)
        if found:
            return [found]
    return [str(local)]


def is_compiler_wrapper(part: str) -> bool:
    return Path(part).name.lower() in ("wibo", "wine", "wine64", "wibo.exe")


def venv_python(venv: Path) -> Path:
    """The interpreter inside a virtual environment on this platform."""
    return venv / ("Scripts/python.exe" if WINDOWS else "bin/python")


def python_command() -> str:
    """The interpreter to run harness or project Python scripts with."""
    return env("PYTHON") or sys.executable


def bind_project_tools(root: Path, *, prefer: bool = False) -> None:
    """Optionally expose a project's own ``tools/`` directory on ``sys.path``.

    Some projects ship helper packages there. Harness modules win unless ``prefer`` is set.
    Nothing calls this implicitly.
    """
    tools = root.resolve() / "tools"
    if not tools.is_dir():
        return
    value = str(tools)
    if value in sys.path:
        sys.path.remove(value)
    if prefer:
        sys.path.insert(0, value)
    else:
        sys.path.append(value)

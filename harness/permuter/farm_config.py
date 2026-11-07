"""Locations and policies for the permuter tooling, read from the harness config.

Every value has a CLI override in the scripts that use it. Without a harness config the
defaults are relative to the selected project checkout (``DECOMP_ROOT``, else the current
directory):

``permuter.farm_root``               farm directory (units/, state/, logs/, tmp/)
                                     default ``<project>/build/permuter-farm``
``permuter.results_dir``             where wins and near wins land
                                     default ``<project>/build/permuter_results``
``permuter.owner_context_policies``  ``{function: policy}`` for ``--full-owner`` units; empty
``permuter.intrinsic_allowlists``    ``{function: [intrinsic, ...]}`` for ``--full-owner``; empty
``tools.permuter``                   decomp-permuter checkout (env ``DECOMP_PERMUTER``)
                                     default ``<farm root>/decomp-permuter``
``tools.ppc_binutils``               PowerPC binutils directory (env ``DECOMP_PPC_BINUTILS``)
                                     default ``<project>/build/binutils``
``tools.clang``                      Clang for ``--full-owner`` (env ``DECOMP_CLANG``), else PATH

The module works when the package is imported as ``permuter`` from ``harness/`` and when a
script in this directory is run directly. ``harness_paths`` is optional so ``farm.py`` can
also run from a copy of this directory alone.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

PACKAGE_DIR = Path(__file__).resolve().parent
HARNESS_DIR = PACKAGE_DIR.parent

if str(HARNESS_DIR) not in sys.path and (HARNESS_DIR / "harness_paths.py").is_file():
    sys.path.append(str(HARNESS_DIR))

try:
    import harness_paths
except ImportError:  # running from a standalone copy of this directory
    harness_paths = None  # type: ignore[assignment]

WINDOWS = os.name == "nt"


def setting(*keys: str, default: Any = None) -> Any:
    """``permuter.<keys>`` from the harness config."""
    if harness_paths is None:
        return default
    return harness_paths.setting("permuter", *keys, default=default)


def _resolve(value: str | os.PathLike[str] | None) -> Path | None:
    if value in (None, ""):
        return None
    if harness_paths is not None:
        return harness_paths.resolve(value)
    return Path(value).expanduser().resolve()


def project_root(value: str | os.PathLike[str] | None = None) -> Path:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    if harness_paths is not None:
        return harness_paths.DECOMP_ROOT
    return Path.cwd().resolve()


def project_version(value: str | None = None) -> str:
    if value:
        return value
    return harness_paths.VERSION if harness_paths is not None else ""


def farm_root(value: str | os.PathLike[str] | None = None, root: Path | None = None) -> Path:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    return _resolve(setting("farm_root")) or project_root(root) / "build" / "permuter-farm"


def results_dir(value: str | os.PathLike[str] | None = None, root: Path | None = None) -> Path:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    return _resolve(setting("results_dir")) or project_root(root) / "build" / "permuter_results"


def _tool(name: str) -> Path | None:
    if harness_paths is not None:
        return harness_paths.tool_path(name)
    configured = os.environ.get("DECOMP_" + name.upper())
    return Path(configured).expanduser().resolve() if configured else None


def permuter_checkout(value: str | os.PathLike[str] | None = None,
                      farm: Path | None = None) -> Path:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    return _tool("permuter") or (farm or farm_root()) / "decomp-permuter"


def binutils_dir(value: str | os.PathLike[str] | None = None, root: Path | None = None) -> Path:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    return _tool("ppc_binutils") or project_root(root) / "build" / "binutils"


def executable(path: Path) -> Path:
    """``path``, or ``path.exe`` on Windows when only that exists."""
    if harness_paths is not None:
        return harness_paths.exe(path)
    if WINDOWS and path.suffix.lower() != ".exe" and not path.exists():
        candidate = path.with_name(path.name + ".exe")
        if candidate.exists():
            return candidate
    return path


def binutil(binutils: Path, name: str) -> Path:
    """One PowerPC binutils program, e.g. ``binutil(dir, "objdump")``."""
    return executable(binutils / f"powerpc-eabi-{name}")


def clang(value: str | os.PathLike[str] | None = None) -> Path | None:
    if value not in (None, ""):
        return Path(value).expanduser().resolve()
    configured = _tool("clang")
    if configured:
        return configured
    found = shutil.which("clang")
    return Path(found).resolve() if found else None


def _pairs(values: Iterable[str], label: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item:
            raise ValueError(f"{label} must be FUNCTION=VALUE, got {value!r}")
        pairs.append((name, item))
    return pairs


def owner_context_policies(cli: Iterable[str] = ()) -> dict[str, str]:
    """Reviewed ``{function: policy}`` mappings: config first, ``FUNCTION=POLICY`` CLI values win."""
    configured = setting("owner_context_policies", default={}) or {}
    if not isinstance(configured, dict):
        raise ValueError("permuter.owner_context_policies must be an object")
    policies = {str(name): str(policy) for name, policy in configured.items()}
    policies.update(_pairs(cli, "--owner-context-policy"))
    return policies


def intrinsic_allowlists(cli: Iterable[str] = ()) -> dict[str, frozenset[str]]:
    """Reviewed ``{function: intrinsics}``: config lists plus ``FUNCTION=__name`` CLI values."""
    configured = setting("intrinsic_allowlists", default={}) or {}
    if not isinstance(configured, dict):
        raise ValueError("permuter.intrinsic_allowlists must be an object")
    allowlists = {str(name): set(map(str, values or ())) for name, values in configured.items()}
    for name, intrinsic in _pairs(cli, "--intrinsic"):
        allowlists.setdefault(name, set()).add(intrinsic)
    return {name: frozenset(values) for name, values in allowlists.items()}

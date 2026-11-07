"""Transactionally promote a link-verified unit after a full retail-SHA build."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import m2c_unit_project_trial
import m2c_unit_proposal
from m2c_unit_queue import inspect_record, publish, replace_file
import portable


def build_check(root: Path, version: str) -> dict[str, Any]:
    """Compare ``build/<version>/main.dol`` with the first SHA-1 in ``config/<version>/build.sha1``."""
    manifest = root / "config" / version / "build.sha1"
    dol = root / "build" / version / "main.dol"
    try:
        expected = manifest.read_text(encoding="ascii").split()[0].lower()
        if len(expected) != 40 or not all(char in "0123456789abcdef" for char in expected):
            raise ValueError("invalid SHA-1 manifest")
        with dol.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha1").hexdigest()
        return {"status": "exact" if digest == expected else "mismatch", "expected": expected,
                "actual": digest,
                "built_at": datetime.fromtimestamp(dol.stat().st_mtime, timezone.utc).isoformat()}
    except (OSError, ValueError, IndexError):
        return {"status": "not_built"}


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          timeout=60, check=check)


def status_paths(root: Path) -> set[str]:
    raw = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root)
    paths = set()
    fields = raw.split(b"\0")
    index = 0
    while index < len(fields) and fields[index]:
        item = fields[index]
        if len(item) < 4 or item[2:3] != b" ":
            raise ValueError("Unsupported git status record")
        status = item[:2]
        paths.add(item[3:].decode())
        index += 1
        if b"R" in status or b"C" in status:
            if index >= len(fields) or not fields[index]:
                raise ValueError("Incomplete git rename status")
            paths.add(fields[index].decode())
            index += 1
    return paths


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    replace_file(temporary, path)


def process_alive(pid: object) -> bool:
    try:
        return portable.pid_alive(int(pid))
    except (OSError, TypeError, ValueError):
        return False


def recover_interrupted(root: Path, output: Path, saved: dict) -> dict | None:
    """Finish a committed transaction, or restore an uncommitted proposal for retry."""
    if process_alive(saved.get("pid")):
        raise ValueError("Automatic promotion is already running")
    base = saved.get("base_commit")
    current = git(root, "rev-parse", "HEAD").stdout.strip()
    if base and current != base:
        parent = git(root, "rev-parse", "HEAD^").stdout.strip()
        subject = git(root, "log", "-1", "--pretty=%s").stdout.strip()
        checked = build_check(root, saved["version"])
        if (parent == base and subject == f"Match {saved.get('unit')}"
                and checked.get("status") == "exact"
                and checked.get("actual") == (saved.get("build") or {}).get("actual")):
            saved.update(state="promoted", promoted=True, commit=current, recovered=True)
            write_json(output / "result.json", saved)
            return saved
        raise ValueError("Project HEAD changed during interrupted promotion")
    proposal = saved.get("proposal")
    changed = saved.get("changed") or []
    if proposal and saved.get("phase") in {"applying", "applied", "build_verified", "staged"}:
        if changed:
            git(root, "restore", "--staged", "--", *changed, check=False)
        patch = json.loads(Path(proposal).read_text(encoding="utf-8"))["patch"]
        reverse = git(root, "apply", "-R", "--check", patch, check=False)
        if reverse.returncode == 0:
            git(root, "apply", "-R", patch)
        elif status_paths(root):
            raise ValueError("Interrupted promotion could not be rolled back")
    lock = saved.get("lock")
    if lock:
        Path(lock).unlink(missing_ok=True)
    shutil.rmtree(output)
    return None


def build_project(root: Path, version: str, plan: dict, output: Path) -> dict:
    configure = m2c_unit_project_trial.configure_command(root, version)
    for name in (plan["elf"], plan["dol"], f"build/{version}/ok"):
        (root / name).unlink(missing_ok=True)
    m2c_unit_project_trial.execute(configure, root, output / "configure.log")
    m2c_unit_project_trial.execute(["ninja", f"build/{version}/ok"], root, output / "build.log")
    checked = build_check(root, version)
    if checked.get("status") != "exact" or checked.get("actual") != plan["expected_sha1"]:
        raise ValueError("Full project build did not reproduce the retail SHA-1")
    return checked


def run(root: Path, version: str, linked: Path, output: Path,
        source_path: str | None = None) -> dict:
    root, linked, output = root.resolve(), linked.resolve(), output.absolute()
    if output.exists():
        saved = output / "result.json"
        if saved.is_file():
            prior = json.loads(saved.read_text(encoding="utf-8"))
            if prior.get("state") in {"promoted", "rejected", "already_present"}:
                return prior
            recovered = recover_interrupted(root, output, prior)
            if recovered is not None:
                if recovered.get("unit"):
                    publish(root, version, saved)
                return recovered
        else:
            shutil.rmtree(output)
    output.mkdir(parents=True)
    result = {"kind": "automatic_unit_promotion", "project_root": str(root),
              "version": version, "link_record": str(linked), "promoted": False,
              "state": "checking", "phase": "starting", "pid": os.getpid()}
    record_path = output / "result.json"
    write_json(record_path, result)
    patch_applied = False
    staged = False
    lock_fd = None
    proposal = None
    try:
        git_dir = Path(git(root, "rev-parse", "--git-dir").stdout.strip())
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        lock = git_dir / "m2c-auto-promote.lock"
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(lock_fd, str(os.getpid()).encode())
        result.update(lock=str(lock), phase="preflight")
        write_json(record_path, result)
        branch = git(root, "branch", "--show-current").stdout.strip()
        if branch not in {"main", "master"}:
            raise ValueError("Automatic promotion requires main or master")
        if status_paths(root):
            raise ValueError("Automatic promotion requires a clean worktree")
        base = git(root, "rev-parse", "HEAD").stdout.strip()
        evidence = inspect_record(linked, root)
        if not evidence.get("link_verified"):
            raise ValueError("Automatic promotion requires current link verification")
        m2c_unit_proposal.run(root, linked, output / "proposal", source_path)
        proposal_path = output / "proposal/result.json"
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        if proposal["base_commit"] != base:
            raise ValueError("Project changed before promotion")
        changed = {row["destination"] for row in proposal["changes"] if row["changed"]}
        result.update(unit=proposal["unit"], base_commit=base, proposal=str(proposal_path),
                      changed=sorted(changed), phase="proposal")
        write_json(record_path, result)
        if not changed:
            result.update(state="already_present", unit=proposal["unit"], base_commit=base)
            return result
        patch = proposal["patch"]
        git(root, "apply", "--check", patch)
        result["phase"] = "applying"
        write_json(record_path, result)
        git(root, "apply", patch)
        patch_applied = True
        result["phase"] = "applied"
        write_json(record_path, result)
        for change in proposal["changes"]:
            if change["changed"]:
                destination = root / change["destination"]
                if destination.read_bytes() != Path(change["payload"]).read_bytes():
                    raise ValueError("Applied source differs from verified proposal")
        plan = json.loads((linked.parent / "plan.json").read_text(encoding="utf-8"))
        result["build"] = build_project(root, version, plan, output)
        result["phase"] = "build_verified"
        write_json(record_path, result)
        observed = status_paths(root)
        if observed != changed:
            raise ValueError("Full build changed files outside the unit proposal: " +
                             ", ".join(sorted(observed - changed)))
        git(root, "add", "--", *sorted(changed))
        staged = True
        result["phase"] = "staged"
        write_json(record_path, result)
        checked = git(root, "diff", "--cached", "--check", check=False)
        if checked.returncode:
            raise ValueError("Promoted source failed git diff validation: " + checked.stdout + checked.stderr)
        committed = git(root, "commit", "-m", f"Match {proposal['unit']}")
        commit = git(root, "rev-parse", "HEAD").stdout.strip()
        staged = False
        patch_applied = False
        result.update(state="promoted", promoted=True, unit=proposal["unit"], branch=branch,
                      base_commit=base, commit=commit, changed=sorted(changed),
                      commit_output=committed.stdout.strip(), phase="complete")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result.update(state="rejected", error=f"{type(error).__name__}: {error}")
        if staged and proposal:
            git(root, "restore", "--staged", "--", *sorted(
                row["destination"] for row in proposal["changes"] if row["changed"]), check=False)
        if patch_applied and proposal:
            reverse = git(root, "apply", "-R", "--check", proposal["patch"], check=False)
            if reverse.returncode == 0:
                git(root, "apply", "-R", proposal["patch"], check=False)
            else:
                result["rollback_error"] = reverse.stderr.strip() or reverse.stdout.strip()
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock.unlink()
            except OSError:
                pass
        write_json(record_path, result)
        if result.get("unit"):
            try:
                publish(root, version, record_path)
            except (OSError, ValueError):
                pass
    return result


def inspect_promotion(record: dict, root: Path) -> dict:
    if Path(record["project_root"]).resolve() != root.resolve():
        raise ValueError("Automatic promotion belongs to a different project")
    if not record.get("promoted"):
        return {"state": "promotion_rejected", "object_verified": False,
                "link_verified": False, "ready_to_merge": False,
                "error": record.get("error")}
    commit = record["commit"]
    contained = git(root, "merge-base", "--is-ancestor", commit, "HEAD", check=False)
    if contained.returncode != 0:
        raise ValueError("Promoted commit is no longer on the current branch")
    checked = build_check(root, record["version"])
    if checked.get("status") != "exact" or checked.get("actual") != record["build"]["actual"]:
        raise ValueError("Promoted project no longer has the verified retail checksum")
    return {"state": "promoted", "promoted": True, "commit": commit,
            "object_verified": True, "link_verified": True,
            "proposal_build_verified": True, "ready_to_merge": False}

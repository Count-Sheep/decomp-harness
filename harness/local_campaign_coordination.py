"""Shared source claims and serialized builds for the local campaign."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import portable


class ClaimConflict(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def alive(pid: int) -> bool:
    return portable.pid_alive(pid)


def replace_file(temporary: Path, path: Path, attempts: int = 50) -> None:
    """``temporary.replace(path)``, retried briefly while a reader holds ``path`` open (Windows)."""
    for attempt in range(attempts):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)


def agent_exit_detail(lane: dict) -> tuple[str, str]:
    """Classify a completed launcher process without interpreting model output."""
    message = Path(lane.get("last_message", ""))
    if message.is_file() and message.stat().st_size:
        return "review_ready", "Agent finished; inspect its branch and final report."
    log = Path(lane.get("log", ""))
    if log.is_file():
        tail = log.read_text(encoding="utf-8", errors="replace")[-1200:]
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        if "error:" in tail.lower() or "usage:" in tail.lower():
            return "failed", f"Agent did not start: {last[:220]}"
    return "finished", "Agent process finished; inspect its branch and log before review."


def agent_live_detail(lane: dict) -> str:
    """Return the newest useful agent event without persisting a log-derived state."""
    log = Path(lane.get("log", ""))
    if not log.is_file():
        return "Starting agent."
    try:
        tail = log.read_text(encoding="utf-8", errors="replace")[-16_384:]
    except OSError:
        return lane.get("detail", "Working.")
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        command = item.get("command")
        if command and event.get("type") == "item.started":
            return f"Running: {command[:220]}"
        text = item.get("text") or event.get("message")
        if text:
            compact = " ".join(str(text).split())
            if compact:
                return compact[:260]
    return lane.get("detail", "Working.")


class Coordinator:
    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "coordination.lock").open("a+", encoding="utf-8") as lock:
            portable.flock(lock, portable.LOCK_EX)
            path = self.directory / "coordination.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            state.setdefault("claims", {})
            state.setdefault("builds", {})
            state.setdefault("agents", {})
            original = json.dumps(state, sort_keys=True)
            # Manual claims survive CLI exits; automatic claims belong to a live process.
            for collection in (state["claims"], state["builds"]):
                for key, row in list(collection.items()):
                    if row.get("pid") and not alive(row["pid"]):
                        del collection[key]
            for lane in state["agents"].values():
                if lane.get("status") == "running" and lane.get("pid") and not alive(lane["pid"]):
                    status, detail = agent_exit_detail(lane)
                    lane.update(status=status, finished_at=now(), detail=detail)
            try:
                yield state
            finally:
                if not path.exists() or json.dumps(state, sort_keys=True) != original:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as output:
                        json.dump(state, output, indent=2, sort_keys=True)
                        temporary = Path(output.name)
                    replace_file(temporary, path)

    def snapshot(self) -> dict:
        with self.transaction() as state:
            snapshot = json.loads(json.dumps(state))
        for lane in snapshot["agents"].values():
            if lane.get("status") == "running":
                lane["live_detail"] = agent_live_detail(lane)
        return snapshot

    def claim(self, source: str, worker: str, symbol: str, *, automatic: bool = False,
              detail: str = "Working on source") -> dict:
        with self.transaction() as state:
            existing = state["claims"].get(source)
            if existing:
                raise ClaimConflict(f"{source} is claimed by {existing['worker']} ({existing['symbol']})")
            claim = {
                "token": uuid.uuid4().hex, "worker": worker, "source": source,
                "symbol": symbol, "pid": os.getpid() if automatic else None,
                "started_at": now(), "updated_at": now(), "detail": detail,
            }
            state["claims"][source] = claim
            return dict(claim)

    def release(self, source: str, token: str) -> None:
        with self.transaction() as state:
            existing = state["claims"].get(source)
            if existing is None or existing["token"] != token:
                raise ClaimConflict("claim changed or no longer exists")
            del state["claims"][source]

    def register_agent(self, lane: dict) -> dict:
        with self.transaction() as state:
            lane_id = lane["id"]
            existing = state["agents"].get(lane_id)
            if existing and existing.get("status") == "running":
                raise ClaimConflict(f"agent lane already exists: {lane_id}")
            replacement = dict(lane)
            replacement["attempt"] = int((existing or {}).get("attempt", 0)) + 1
            state["agents"][lane_id] = replacement
            return dict(replacement)

    def update_agent(self, lane_id: str, **changes) -> None:
        with self.transaction() as state:
            if lane_id not in state["agents"]:
                raise ClaimConflict(f"unknown agent lane: {lane_id}")
            state["agents"][lane_id].update(changes, updated_at=now())

    @contextmanager
    def build(self, worker: str, detail: str):
        ticket = uuid.uuid4().hex
        with self.transaction() as state:
            state["builds"][ticket] = {
                "worker": worker, "detail": detail, "pid": os.getpid(),
                "status": "waiting", "started_at": now(),
            }
        try:
            with (self.directory / "build.lock").open("a+", encoding="utf-8") as lock:
                portable.flock(lock, portable.LOCK_EX)
                with self.transaction() as state:
                    state["builds"][ticket].update(status="building", acquired_at=now())
                try:
                    yield
                finally:
                    portable.flock(lock, portable.LOCK_UN)
        finally:
            with self.transaction() as state:
                state["builds"].pop(ticket, None)

    @contextmanager
    def writer(self, shared=False):
        """Allow concurrent runners, but exclude report sync from the fleet."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "fleet.lock").open("a+", encoding="utf-8") as lock:
            try:
                portable.flock(lock, (portable.LOCK_SH if shared else portable.LOCK_EX) | portable.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("campaign state writer is active; stop the runner before sync") from exc
            yield

    @contextmanager
    def state_file(self):
        """Serialize short state-file updates across multiple model workers."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "state-writer.lock").open("a+", encoding="utf-8") as lock:
            portable.flock(lock, portable.LOCK_EX)
            yield

#!/usr/bin/env python3
"""farm.py — decomp-permuter farm supervisor (Windows, Linux and macOS).

Runs N independent SINGLE-THREADED decomp-permuter processes, one per work
unit.  This deliberately avoids decomp-permuter's -j flag: its multiprocessing
is fork-only and does not work on native Windows.  One process per unit needs
no fork and scales to the machine's cores on every platform.

Layout under the farm root (--farm-root; default ``permuter.farm_root`` from
the harness config, else ``<project>/build/permuter-farm``):
  decomp-permuter/     permuter checkout (override: --permuter / tools.permuter)
  units/<fn>/          work units from gen_workunits.py
  units/manifest.json  generation manifest (queue order preserved)
  state/state.json     progress, survives restarts
  state/status.json    heartbeat and dashboard snapshot (kind "permuter_farm")
  state/farm.pid       single-instance marker
  logs/<fn>.log        per-unit permuter output
  tmp/<fn>/            per-worker temporary directory

Results (--results-dir; default ``permuter.results_dir``, else
``<project>/build/permuter_results``):
  <fn>/                WINS: <fn>.c + summary.json
  nearwins/            near-miss candidates worth human review

Wins are detected two ways: the permuter exits after --stop-on-zero writing
output-0-*/, or a nonzero personal best lands below --nearwin-threshold.
Nothing is ever applied to the project; wins wait for human validation.

The unit manifest is reloaded between worker assignments, so units can be
added (gen_workunits.py replaces manifest.json atomically) without stopping
active permuters.

On Windows the supervisor holds the machine awake via SetThreadExecutionState
while at least one worker is running (no admin needed); sleep resumes when it
stops.

Usage:  python harness/permuter/farm.py [--workers 12] [--budget 5400] [--rounds 99]
        python harness/permuter/farm.py --status        (print status.json)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from . import farm_config
except ImportError:  # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import farm_config  # noqa: E402

try:
    import portable  # harness/portable.py (on sys.path via farm_config)
except ImportError:  # standalone copy of this directory
    portable = None  # type: ignore[assignment]

WINDOWS = os.name == "nt"
STATUS_SCHEMA_VERSION = 1

# Bound by configure(); the defaults only matter to code that never calls it.
FARM_ROOT = farm_config.farm_root()
PERMUTER = FARM_ROOT / "decomp-permuter" / "permuter.py"
UNITS = FARM_ROOT / "units"
STATE_DIR = FARM_ROOT / "state"
OUTBOX = farm_config.results_dir()
LOGS = FARM_ROOT / "logs"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
CREATE_NO_WINDOW = 0x08000000

RE_BASE_SCORE = re.compile(r"base score = (\d+)")
RE_NEW_BEST = re.compile(r"found new best score! \((\d+) vs (\d+)\)")


def configure(farm_root: Path, results: Path, permuter: Path) -> None:
    """Bind the farm's directories (see the module docstring for the layout)."""
    global FARM_ROOT, PERMUTER, UNITS, STATE_DIR, OUTBOX, LOGS
    FARM_ROOT = farm_root
    PERMUTER = permuter / "permuter.py"
    UNITS = farm_root / "units"
    STATE_DIR = farm_root / "state"
    OUTBOX = results
    LOGS = farm_root / "logs"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def keep_awake(active: bool) -> None:
    """Hold off system sleep while workers run (Windows only; a no-op elsewhere)."""
    if not WINDOWS:
        return
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader (the dashboard) has it open
            if attempt == 49:
                raise
            time.sleep(0.05)


def pid_alive(pid: int) -> bool:
    if portable is not None:
        return portable.pid_alive(pid)
    if WINDOWS:
        out = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_manifest_queue(units: Path):
    manifest = load_json(units / "manifest.json", [])
    metas = {m["fn"]: m for m in manifest if m.get("status") == "ok"}
    queue_order = [
        m["fn"] for m in manifest
        if m.get("status") == "ok"
        and (units / m["fn"] / "base.c").is_file()
    ]
    return metas, queue_order


def prune_stale_results(state: dict, queue_order: list[str]) -> list[str]:
    """Drop persisted results for work units no longer in the manifest."""
    admitted = set(queue_order)
    stale = sorted(set(state["done"]) - admitted)
    for fn in stale:
        del state["done"][fn]
    return stale


class Worker:
    def __init__(self, fn: str, unit_dir: Path, budget: float):
        self.fn = fn
        self.unit_dir = unit_dir
        self.budget = budget
        self.started = time.time()
        self.base_score = None
        self.best_score = None
        self.log_path = LOGS / f"{fn}.log"
        self.log_start = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self.log.write(f"\n===== farm start {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                       f"budget={budget}s =====\n")
        self.log.flush()
        # Its own process group/session, so stop() can end the whole tree
        # (the permuter plus any compiler it is running).
        kwargs = portable.popen_group_kwargs() if portable is not None else (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
            else {"start_new_session": True})
        if WINDOWS:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        # Keep every candidate .c/.o inside the farm tree: the permuter writes
        # them through tempfile, and a per-worker directory is easy to exclude
        # from real-time scanning and to clean up.
        env = dict(os.environ)
        tmp = FARM_ROOT / "tmp" / fn
        tmp.mkdir(parents=True, exist_ok=True)
        for var in ("TEMP", "TMP", "TMPDIR"):
            env[var] = str(tmp)
        self.tmp_dir = tmp
        self.proc = subprocess.Popen(
            [sys.executable, str(PERMUTER), str(unit_dir),
             "--best-only", "--stop-on-zero", "--stack-diffs"],
            stdout=self.log, stderr=subprocess.STDOUT,
            cwd=str(unit_dir), env=env, **kwargs)

    def poll(self):
        return self.proc.poll()

    def expired(self) -> bool:
        return time.time() - self.started > self.budget

    def stop(self) -> None:
        if self.proc.poll() is None:
            if portable is not None:
                portable.terminate_tree(self.proc.pid, grace=15)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if portable is not None:
                    portable.kill_tree(self.proc.pid)
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        # clear leftover candidate temp files
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def read_scores(self) -> None:
        try:
            with self.log_path.open("rb") as stream:
                stream.seek(self.log_start)
                text = stream.read().decode("utf-8", errors="replace")
        except OSError:
            return
        m = RE_BASE_SCORE.search(text)
        if m:
            self.base_score = int(m.group(1))
            if self.best_score is None:
                self.best_score = self.base_score
        for m in RE_NEW_BEST.finditer(text):
            s = int(m.group(1))
            if self.best_score is None or s < self.best_score:
                self.best_score = s


def harvest(w: Worker, meta: dict, nearwin_threshold: int):
    """Collect output-*/source.c candidates. Returns (won, best_score)."""
    w.read_scores()
    outputs = []
    for d in sorted(w.unit_dir.glob("output-*")):
        m = re.match(r"output-(\d+)-\d+", d.name)
        if m and (d / "source.c").is_file():
            outputs.append((int(m.group(1)), d))
    outputs.sort(key=lambda t: t[0])
    won = False
    best = w.best_score
    if outputs:
        score, best_dir = outputs[0]
        best = min(score, best if best is not None else score)
        dest = OUTBOX / w.fn if score == 0 else OUTBOX / "nearwins" / f"{w.fn}_score{score}"
        if score == 0 or score <= nearwin_threshold:
            dest.mkdir(parents=True, exist_ok=True)
            src_c = (best_dir / "source.c").read_text(encoding="utf-8", errors="replace")
            (dest / f"{w.fn}.c").write_text(src_c, encoding="utf-8", newline="\n")
            summary = {
                "fn": w.fn,
                "unit": meta.get("unit"),
                "addr": meta.get("addr"),
                "pct": meta.get("pct"),
                "size": meta.get("size"),
                "mw_version": meta.get("mw_version"),
                "cflags": meta.get("cflags"),
                "base_score": w.base_score,
                "score": score,
                "diff": f"permuter difflib score {score} (0 = asm-exact vs dtk target.o, "
                        f"--stack-diffs strict)",
                "won": score == 0,
                "ts": utc_now(),
                "machine": "permuter-farm",
            }
            (dest / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        won = score == 0
    return won, best


def collect_wins(results: Path) -> tuple[list[dict], list[str]]:
    """Wins (``<results>/<fn>/summary.json``) and near-win names under ``results``."""
    wins = []
    if results.is_dir():
        for summary_path in sorted(results.glob("*/summary.json")):
            if summary_path.parent.name == "nearwins":
                continue
            summary = load_json(summary_path, None)
            if not isinstance(summary, dict) or not summary.get("won"):
                continue
            wins.append({key: summary.get(key) for key in
                         ("fn", "ts", "addr", "size", "pct", "base_score", "unit", "mw_version")}
                        | {"verify": summary.get("verify"),
                           "local_source": str(summary_path.parent / f"{summary.get('fn')}.c")})
    nearwins_dir = results / "nearwins"
    nearwins = sorted(path.name for path in nearwins_dir.iterdir()) if nearwins_dir.is_dir() else []
    return wins, nearwins


def build_status(*, state: dict, workers: dict, queue_order: list[str], metas: dict,
                 target_workers: int, round_no: int, alive: bool = True) -> dict:
    """The status.json document: farm heartbeat plus a per-unit dashboard snapshot.

    The schema is documented in README.md ("status.json").
    """
    now = utc_now()
    done = state["done"]
    wins, nearwins = collect_wins(OUTBOX)
    units = []
    for fn in queue_order:
        record = done.get(fn, {})
        worker = workers.get(fn)
        meta = metas.get(fn, {})
        if record.get("won"):
            unit_state = "won"
        elif worker is not None:
            unit_state = "active"
        elif record.get("bad"):
            unit_state = "bad"
        elif record.get("last_round") == round_no:
            unit_state = "done"
        else:
            unit_state = "queued"
        base = worker.base_score if worker is not None and worker.base_score is not None \
            else record.get("base", meta.get("base_score"))
        best = worker.best_score if worker is not None and worker.best_score is not None \
            else record.get("best")
        units.append({
            "fn": fn, "state": unit_state, "base_score": base, "best_score": best,
            "rounds": record.get("rounds", 0),
            "elapsed_seconds": int(time.time() - worker.started) if worker is not None else None,
            "unit": meta.get("unit"), "addr": meta.get("addr"), "size": meta.get("size"),
            "pct": meta.get("pct"), "mw_version": meta.get("mw_version"),
            "bad": record.get("bad"),
        })
    counts = {name: sum(unit["state"] == name for unit in units)
              for name in ("active", "queued", "won", "done", "bad")}
    farm = {
        "alive": alive,
        "ts": now,
        "pid": os.getpid(),
        "workers": len(workers),
        "target_workers": target_workers,
        "round": round_no,
        "units": len(queue_order),
        "wins": sum(1 for v in done.values() if v.get("won")),
        "bad": sum(1 for v in done.values() if v.get("bad")),
        "active": {fn: {"base": w.base_score, "best": w.best_score,
                        "elapsed": int(time.time() - w.started)}
                   for fn, w in workers.items()},
    }
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "kind": "permuter_farm",
        "polled_at": now,
        "reachable": True,
        "host": "local",
        "runtime": f"{platform.system()} · Python {platform.python_version()}",
        "farm_root": str(FARM_ROOT),
        "results_dir": str(OUTBOX),
        "farm": farm,
        "counts": counts,
        "improved_units": sum(1 for unit in units if unit["best_score"] is not None
                              and unit["base_score"] is not None
                              and unit["best_score"] < unit["base_score"]),
        "units": units,
        "wins": wins,
        "nearwins": nearwins,
    }


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--farm-root", help="farm directory (default: permuter.farm_root, "
                    "else <project>/build/permuter-farm)")
    ap.add_argument("--results-dir", help="where wins and near wins land (default: "
                    "permuter.results_dir, else <project>/build/permuter_results)")
    ap.add_argument("--permuter", help="decomp-permuter checkout (default: tools.permuter / "
                    "DECOMP_PERMUTER, else <farm root>/decomp-permuter)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--budget", type=float, default=5400,
                    help="seconds per unit per round")
    ap.add_argument("--rounds", type=int, default=99,
                    help="passes over the unfinished queue")
    ap.add_argument("--nearwin-threshold", type=int, default=25)
    ap.add_argument("--poll-interval", type=float, default=20,
                    help="seconds between supervision passes")
    ap.add_argument("--status", action="store_true", help="print status.json and exit")
    args = ap.parse_args(argv)

    farm_root = farm_config.farm_root(args.farm_root)
    configure(farm_root, farm_config.results_dir(args.results_dir),
              farm_config.permuter_checkout(args.permuter, farm_root))

    status_path = STATE_DIR / "status.json"
    state_path = STATE_DIR / "state.json"

    if args.status:
        print(json.dumps(load_json(status_path, {}), indent=1))
        return

    if not PERMUTER.is_file():
        sys.exit(f"decomp-permuter not found: {PERMUTER} (pass --permuter or set tools.permuter)")
    for folder in (STATE_DIR, OUTBOX, OUTBOX / "nearwins", LOGS, UNITS):
        folder.mkdir(parents=True, exist_ok=True)

    # single-instance lock (best effort)
    lock = STATE_DIR / "farm.pid"
    if lock.exists():
        try:
            oldpid = int(lock.read_text(encoding="utf-8"))
            if oldpid != os.getpid() and pid_alive(oldpid):
                print(f"farm already running (pid {oldpid}); exiting")
                return
        except (ValueError, OSError):
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")

    metas, queue_order = load_manifest_queue(UNITS)

    state = load_json(state_path, {"done": {}, "round": 0})
    pruned = prune_stale_results(state, queue_order)
    if pruned:
        atomic_write(state_path, json.dumps(state, indent=1))
        print(f"startup: pruned {len(pruned)} stale results")

    def runnable(rnd):
        for fn in queue_order:
            d = state["done"].get(fn, {})
            if d.get("won"):
                continue
            if d.get("bad"):
                continue
            # `rounds` predates live manifest injection and only works when
            # every unit starts in round zero.  Track the actual global round
            # as new units finish so a late addition runs once, rather than
            # repeatedly occupying a worker until its counter catches up.
            if d.get("last_round") == rnd:
                continue
            if "last_round" not in d and d.get("rounds", 0) > rnd:
                continue
            yield fn

    workers = {}
    round_no = state.get("round", 0)
    print(f"farm: {len(queue_order)} units, {args.workers} workers, "
          f"budget {args.budget}s, starting round {round_no}")

    try:
        while round_no < args.rounds:
            # gen_workunits.py atomically replaces manifest.json after writing
            # complete unit directories. Preserve metadata for active workers,
            # then adopt the latest queue order before selecting free work.
            loaded_metas, loaded_order = load_manifest_queue(UNITS)
            metas.update(loaded_metas)
            if loaded_order != queue_order:
                queue_order = loaded_order
                pruned = prune_stale_results(state, queue_order)
                if pruned:
                    atomic_write(state_path, json.dumps(state, indent=1))
                print(f"manifest reload: {len(queue_order)} units, "
                      f"pruned {len(pruned)} stale results")

            pending = [fn for fn in runnable(round_no) if fn not in workers]
            if not pending and not workers:
                round_no += 1
                state["round"] = round_no
                remaining = [fn for fn in runnable(round_no)]
                if not remaining:
                    print("queue exhausted — all units won or bad")
                    break
                continue

            # top-up workers
            while pending and len(workers) < args.workers:
                fn = pending.pop(0)
                try:
                    workers[fn] = Worker(fn, UNITS / fn, args.budget)
                    print(f"start {fn}")
                except OSError as e:
                    state["done"].setdefault(fn, {})["bad"] = f"spawn: {e}"

            time.sleep(args.poll_interval)
            keep_awake(bool(workers))

            for fn in list(workers):
                w = workers[fn]
                rc = w.poll()
                if rc is None and not w.expired():
                    continue
                w.stop()
                won, best = harvest(w, metas.get(fn, {}), args.nearwin_threshold)
                d = state["done"].setdefault(fn, {})
                d["rounds"] = d.get("rounds", 0) + 1
                d["last_round"] = round_no
                d["best"] = best if best is not None else d.get("best")
                d["base"] = w.base_score
                if won:
                    d["won"] = True
                    print(f"WIN  {fn}")
                elif rc not in (None, 0) and w.base_score is None:
                    d["bad"] = f"permuter exited rc={rc} before base score"
                    print(f"BAD  {fn} rc={rc}")
                else:
                    print(f"stop {fn} best={best}")
                del workers[fn]
                atomic_write(state_path, json.dumps(state, indent=1))

            for w in workers.values():
                w.read_scores()
            status = build_status(state=state, workers=workers, queue_order=queue_order,
                                  metas=metas, target_workers=args.workers, round_no=round_no)
            atomic_write(status_path, json.dumps(status, indent=1))
    finally:
        for w in workers.values():
            w.stop()
        keep_awake(False)
        status = build_status(state=state, workers={}, queue_order=queue_order, metas=metas,
                              target_workers=args.workers, round_no=round_no, alive=False)
        atomic_write(status_path, json.dumps(status, indent=1))
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()

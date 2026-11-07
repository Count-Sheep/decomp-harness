# Dashboard

A small, read-only web dashboard for the harness's m2c / permuter stack and its draft workers.
Python 3.11+ standard library only; runs on Windows, macOS and Linux (including WSL).

```sh
python dashboard/server.py [--host 127.0.0.1] [--port 8765] [--open] [--cache-dir DIR]
```

## Configuration

The dashboard reuses the harness configuration: `DECOMP_HARNESS_CONFIG`, or `harness.local.json` at the
repository root, which `harness/harness_paths.py` loads. Its `projects` appear in the project selector
(each with its `version`/`versions`), and `default_project` / `DECOMP_PROJECT` picks the first page. With no
config, a `DECOMP_ROOT` checkout is shown on its own. The optional `dashboard` section sets the server:

```json
"dashboard": {
  "host": "127.0.0.1",
  "port": 8765,
  "cache_seconds": 2,
  "telemetry_seconds": 1,
  "permuter_status": "path/to/status.json",
  "permuter_project": "example",
  "m2c_executors": ["http://127.0.0.1:18766=4"],
  "probe_remote_model_hosts": false
}
```

- `host`/`port`: bind address; the default `127.0.0.1` keeps it local. No route needs authentication, so
  bind only to an interface you trust. `--host`/`--port` override.
- `permuter_status`: a permuter farm status file. Either a poller's `{"kind": "permuter_farm", ...}`
  status, or the vendored farm's own heartbeat (`<farm_root>/state/status.json`). Without this setting,
  `permuter.farm_root` is used when that file exists. `permuter_project` limits the farm's detail section to
  one project's page; the farm card is always shown in the worker strip.
- `m2c_executors`: remote m2c workers (`URL` or `URL=SLOTS`, as passed to the harness) to check through their
  `/v1/health` endpoint.
- `probe_remote_model_hosts`: the draft-attempt model RPC (`<host>/models`) is probed only on loopback unless
  this is set.

Paths are relative to the config file. The configuration is read once at startup.

## What it shows

For each project and version:

- **Progress**: the measures in `build/<VERSION>/report.json` (fuzzy match, exact functions, matched and
  linked code, categories), whether the report is older than the newest source edit, and a SHA-1 check of
  the main build output against the first line of `config/<VERSION>/build.sha1`.
- **Progress over time**: each new report from a byte-exact build becomes a point, stored in the
  dashboard's own `.cache/history/`. A project's `build/decomp-support/<VERSION>/progress-history.json`
  is merged in when one exists.
- **m2c sweeps**: the harness's `local_campaign_m2c_report.load_report` on
  `build/decomp-support/<VERSION>/`, covering active runs with stage, progress and stall warnings,
  unit-gate / promotion-loop totals, visible-units progress, refinement search, history, yield chart,
  diagnostics and closest drafts.
- **Unit promotions**: `m2c_unit_queue.snapshot`, which re-verifies every published evidence record.
- **Draft attempts**: the `m2c_draft_attempts.py` ledger (`attempts/state.json`) with its model RPC status.
- **Worker strip** (all projects): running harness scripts with the project they work on, the last four
  attempts, one m2c card per project/version, the health of remote m2c executors, and the permuter farm.

Process observation is portable: `psutil` if it is installed, otherwise `/proc` on Linux, the Win32 API on
Windows, and `ps` on macOS. The harness's own `ps`-based scan is swapped for this one while the dashboard
reads m2c reports.

## Read-only guarantees

GET and HEAD only; other methods return 405. Snapshot readers never write into a project checkout. The
only files the dashboard writes are its own history, under `dashboard/.cache/` (`--cache-dir`, git-ignored).
Static files come only from `web/`; traversal, hidden paths and unknown API routes are refused.

## HTTP

| Route | Content |
| --- | --- |
| `/`, `/?project=ID[&version=V]` | the page (`web/index.html`, with `window.DashboardConfig` injected) |
| `/api/config` | project registry (ids, names, versions; no local paths) |
| `/api/projects/ID[?version=V]` | project snapshot: `versions.V.{measures, categories, build, report_source, progress_history, stats_delta, m2c, unit_promotions, draft_attempts}` |
| `/api/telemetry` | `{active, recent, m2c_projects, executors, processes_available}` |
| `/api/permuter` | the farm status (404 when not configured) |

Snapshots refresh in the background, at most one refresh per key at a time. A failed refresh keeps
serving the last good value. Each response carries `_cache: {status, refreshing, updated_at, age_seconds, error}`
and an ETag that changes only when the content does. The page polls with `If-None-Match` (a 304 costs
nothing), pauses while its tab is hidden, and backs off after errors. It follows the OS light/dark setting;
the header toggle stores an explicit choice.

## Tests

```sh
python -m unittest discover -s dashboard/tests
node --test dashboard/tests/test_app.cjs
```

The Python tests build neutral `TEST01` fixture checkouts in temporary directories with an isolated, empty
harness config. They use only loopback servers.

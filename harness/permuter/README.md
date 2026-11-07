> **Notice:** this package is adapted from MIT-licensed upstream decomp tooling; the original
> copyright and license are kept in [`LICENSE`](LICENSE).

# Permuter farm

Tooling that turns near-matching functions of a dtk-template decomp into
[decomp-permuter](https://github.com/simonlindholm/decomp-permuter) work units and keeps a pool of
permuter processes busy on them. It runs on Windows, Linux and macOS.

decomp-permuter's own `-j` uses fork-only multiprocessing, which does not work on native Windows.
The farm sidesteps `-j` entirely: it runs N independent single-threaded permuter processes, one
per work unit, which needs no fork and scales with the machine's cores on every platform.

| file | what it does |
|---|---|
| `gen_workunits.py` | builds self-contained work units from the project: preprocesses each queued translation unit with its **exact** per-unit MWCC + flags (from `build.ninja`), strips all other function bodies, assembles a single-function `target.o` from the dtk asm, and gates everything (parse / compile / single-symbol / full-TU fidelity / finite base score) before queueing it |
| `farm.py` | supervisor: keeps N permuter processes running through the queue with per-unit time budgets, writes wins to the results directory, persists state across restarts, and writes `status.json` for the dashboard |
| `owner_extract.py` | small ELF32/PowerPC reader/writer for `--full-owner`: audits a whole-owner object and emits one relocation-preserving function |
| `owner_source.py` | fail-closed source policy (lexical + Clang AST + dataflow) for `--full-owner` targets |
| `farm_config.py` | resolves the locations and policies below from the harness config |

Nothing here ever edits the project's sources: wins wait in the results directory for a human to
validate them against the full build.

## Configuration

Everything has a command-line override. Defaults come from the harness config
(`harness.local.json`, see `harness.example.json`) and the selected project:

| setting | CLI | default |
|---|---|---|
| `permuter.farm_root` | `--farm-root` | `<project>/build/permuter-farm` |
| `permuter.results_dir` | `--results-dir` (farm) | `<project>/build/permuter_results` |
| `permuter.owner_context_policies` | `--owner-context-policy FUNCTION=POLICY` | `{}` (none) |
| `permuter.intrinsic_allowlists` | `--intrinsic FUNCTION=__name` | `{}` (none) |
| `tools.permuter` / `DECOMP_PERMUTER` | `--permuter` | `<farm root>/decomp-permuter` |
| `tools.ppc_binutils` / `DECOMP_PPC_BINUTILS` | `--binutils` | `<project>/build/binutils` |
| `tools.clang` / `DECOMP_CLANG` | `--owner-sanitizer` | `clang` on `PATH` |
| project root / version | `--root`, `--version` | the configured project (`DECOMP_ROOT`, `DECOMP_VERSION`) |

MWCC runs natively on Windows and through the host's compiler wrapper elsewhere (`build/tools/wibo`
or `tools.compiler_wrapper`, as for the rest of the harness).

Farm layout:

```
<farm root>/
  decomp-permuter/     permuter checkout (unless tools.permuter points elsewhere)
  units/<fn>/          work units; units/manifest.json lists them in queue order
  state/state.json     progress, survives restarts
  state/status.json    heartbeat + dashboard snapshot (see below)
  state/farm.pid       single-instance marker
  logs/<fn>.log        per-unit permuter output
  tmp/<fn>/            per-worker temporary directory (candidate .c/.o files)
<results dir>/
  <fn>/<fn>.c, <fn>/summary.json         wins
  nearwins/<fn>_score<N>/                 near misses (score <= --nearwin-threshold)
```

Keeping every candidate file inside the farm tree (the farm points `TEMP`/`TMP`/`TMPDIR` at
`tmp/<fn>/`) makes it easy to exclude from real-time antivirus scanning, which otherwise taxes
every compile on Windows.

## Generating work units

The queue is a tab-separated file (default `build/permuter_queue.tsv` in the project; `#` starts a
comment):

```
tier	pct	size	name	addr	unit
1	97.5	120	fn_80001234	0x80001234	main/game/unit
```

```sh
python harness/permuter/gen_workunits.py [--queue build/permuter_queue.tsv] [--only fn_80001234] [--limit N]
```

Each unit lands in `<farm root>/units/<fn>/` (or `--outdir`) with `base.c`, `target.o`,
`compile.sh`, `compile.bat`, `compile_cmd.json`, `settings.toml` and `meta.json`, and
`manifest.json` is replaced atomically, so a running farm picks new units up between assignments.
A unit is only admitted when:

1. `base.c` re-parses with decomp-permuter's pycparser fork;
2. `base.c` compiles with the unit's exact MWCC and flags;
3. the compiled object contains exactly one function, the target;
4. the isolated target's code equals the real full-TU build object;
5. decomp-permuter's scorer gives a finite, nonzero base score.

`--mwcc-pragma` adds pragmas for the isolated target (the fidelity gate still applies). Existing
units are kept unless `--force`; a unit whose `meta.json` says `"status": "dequeued"` is never
regenerated.

The generated commands name this host's own tools by absolute path, so units are run by the farm
on the machine that generated them. Tool paths containing whitespace are quoted in the objdump
command; keep such paths free of spaces if your decomp-permuter splits that command on whitespace.

### decomp-permuter on Windows

Upstream decomp-permuter runs `compile.sh` through bash and preprocesses `base.c` with `cpp`.
The small patch series in [`decomp_permuter_patches/`](decomp_permuter_patches/) (MIT, like
upstream) makes it run each unit's `compile_cmd.json` argv directly, with no shell or bash per
candidate, and read the already-preprocessed `base.c` verbatim when no `cpp` is installed.
Apply it to the farm's checkout:

```sh
git clone --config core.autocrlf=false https://github.com/simonlindholm/decomp-permuter <farm root>/decomp-permuter
git -C <farm root>/decomp-permuter checkout 8556c81
python harness/permuter/apply_permuter_patches.py --root <farm root>/decomp-permuter
python -m pip install toml          # decomp-permuter's only third-party dependency
```

The patches help on every platform (one process per candidate instead of a shell and the
compiler) and are required on Windows hosts without bash or `cpp`. Unpatched checkouts still work
wherever bash and `cpp` exist, using `compile.sh`.

### Full-owner mode (local benchmark only)

Some functions do not reproduce when their owner is reduced to one function.
`gen_workunits.py --full-owner --only FN --owner-target-source SEED.c` keeps the whole translation
unit instead. It is fail-closed and local-only: `compile.bat` deliberately exits with an error,
and `compile.sh` is an isolated Python wrapper that verifies every tool hash, snapshots the base
and candidate once, guards exact context equality and compiles only those snapshots.

* Clang (attested by path, version and hash) is used only to resolve preprocessor directives
  while keeping active MWCC pragmas; MWCC remains the only code generator.
* The seed must contain exactly one definition with the incumbent signature. It is spliced into
  the owner, and the shaped owner must still reproduce the live target.
* A reviewed **owner-context policy** must be configured for the function; the mapping is empty by
  default, so nothing is rewritten unless a reviewer opts a function in. The available policy is
  `pragma-clean-v1`: when `#pragma optimization_level 4` and `#pragma peephole off` immediately
  precede the target and `#pragma peephole on` immediately follows it, the prefix is replaced by
  `#pragma peephole on` and the restore is kept byte-for-byte. The clean-context target must then
  differ from the shaped one.
* `owner_source.py` admits only plain C: no directives/`_Pragma`, assembly, `.inc`,
  `goto`/labels, layout qualifiers or attributes, function calls, compound assignment, pointer
  alias tricks, save/write/restore sequences, dead locals/stores or reads before definition, and
  no `__*` compiler intrinsic unless it is in the function's reviewed **intrinsic allowlist**
  (empty by default). Unsupported AST shapes are rejected, never accepted lexically.
* `owner_extract.py` rejects any change to sibling text, named/static/data symbols, allocatable
  non-text sections or sibling relocations across both transitions. Compiler-generated `@N` local
  names may renumber; they are compared by section/value/type identity. Only target-owned
  relocations are kept in the emitted single-function object.

`meta.json` binds the retail, shaped and clean targets, the sanitizer and its inputs, the seed,
context transform, candidate policy, generator, guard, extractor, compiler and wrappers, and both
sibling audits. The fidelity label is `full-owner-clean-context-extracted-v2`.

## Running the farm

```sh
python harness/permuter/farm.py [--workers 12] [--budget 5400] [--rounds 99] [--nearwin-threshold 25]
python harness/permuter/farm.py --status
```

The farm takes the unfinished units in manifest order, runs each for `--budget` seconds per round
and makes up to `--rounds` passes. Workers run in their own process group and are stopped with
their whole process tree. On Windows the farm also keeps the machine from sleeping while workers
are running. Start it from a service manager (Task Scheduler, systemd, launchd) if it should
survive logouts and reboots; it resumes from `state/state.json`.

A **win** is permuter score 0 with `--stack-diffs` (strict stack offsets): the candidate compiles
with the unit's exact compiler and flags and its disassembly matches the dtk-split target modulo
standard normalisations (sda21 base-register rewriting, anonymous local symbol names). Wins must
still be validated against the full build before they are applied by hand.

## `status.json`

`state/status.json` is rewritten atomically after every supervision pass and once more when the
farm exits. Point the dashboard's `permuter_farm.path` at it. Fields:

| field | meaning |
|---|---|
| `schema_version` | `1` |
| `kind` | always `"permuter_farm"` (the dashboard rejects anything else) |
| `polled_at` | UTC time this snapshot was written (ISO 8601) |
| `reachable` | always `true` when written by the farm itself |
| `host`, `runtime` | `"local"` and the OS / Python version |
| `farm_root`, `results_dir` | the directories in use |
| `farm.alive` | `false` once the farm has exited |
| `farm.ts` | heartbeat time (UTC) |
| `farm.pid` | supervisor process id |
| `farm.workers`, `farm.target_workers` | running and requested worker counts |
| `farm.round` | current pass over the queue |
| `farm.units`, `farm.wins`, `farm.bad` | admitted units, won units, units that failed to start |
| `farm.active` | `{fn: {base, best, elapsed}}` for running workers |
| `counts` | units per state: `active`, `queued`, `won`, `done` (ran this round), `bad` |
| `improved_units` | units whose best score is below their base score |
| `units[]` | per unit: `fn`, `state`, `base_score`, `best_score`, `rounds`, `elapsed_seconds`, `unit`, `addr`, `size`, `pct`, `mw_version`, `bad` |
| `wins[]` | from `<results>/<fn>/summary.json`: `fn`, `ts`, `addr`, `size`, `pct`, `base_score`, `unit`, `mw_version`, `verify`, `local_source` |
| `nearwins[]` | directory names under `<results>/nearwins/` |

`farm.py --status` prints the file.

## Tests

From `harness/`:

```sh
python -m unittest permuter.test_farm permuter.test_gen_workunits permuter.test_honest_generator_weights \
    permuter.test_owner_source permuter.test_owner_extract
```

`test_owner_source` needs Clang and `test_owner_extract` needs PowerPC binutils (`as`, `objcopy`,
`readelf`); both skip cleanly when the tools are absent.

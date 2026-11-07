# decomp-harness

A portable harness for matching decompilation projects that use the
[decomp-toolkit](https://github.com/encounter/decomp-toolkit) (dtk) template and
Metrowerks CodeWarrior (MWCC). It runs a review-only pipeline from assembly to
verified C:

- **m2c draft pipeline**: project-wide [m2c](https://github.com/matt-kempster/m2c)
  drafts with type and reference context, scratch MWCC/objdiff validation, bounded
  refinement searches, and owner-unit link gates that check the full build's SHA-1.
- **Permuter farm**: turns near-matching functions into
  [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) work units
  and keeps a pool of permuter processes busy on them, natively on Windows too.
- **Draft harness**: a local-LLM campaign that asks any OpenAI-compatible model
  server for replacement functions and verifies each one with objdiff. It comes
  with a model benchmark and an evidence store.
- **m2c engineering lane**: an optional Claude Agent SDK runner that proposes
  general m2c fixes, admitted only through a deterministic A/B check.
- **Dashboard**: a small, read-only web view of the m2c, permuter and draft work.

The harness contains no project. You plug decomps in through a local config file.
Results are candidates for review by default. Source only changes when you opt in
to one of two things: SHA-1-gated unit promotion (`--auto-promote`), or the draft
campaign's `run --promote`, which commits improvements after a rebuilt report
shows that no function lost score.

## Requirements

- Python 3.11 or later, and Git. No third-party Python packages are needed for
  the core.
- Windows, Linux or macOS. On Windows MWCC runs natively. Elsewhere the harness
  runs it through [wibo](https://github.com/decompals/wibo), from the project's
  `build/tools/wibo` or from `PATH`, or through Wine.
- For each plugged-in project: a dtk-template checkout that builds with
  `python configure.py && ninja`, so that it has `build/<VERSION>/report.json`,
  `build/tools/objdiff-cli` and `build/compilers/`.
- An m2c checkout at the base revision named in
  [`harness/m2c_patches/NOTICE.txt`](harness/m2c_patches/NOTICE.txt), with the
  harness's patches applied (see below).
- Optional:
  - [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) with
    the harness's small patch series, plus PowerPC binutils, for the permuter
    farm. Clang is needed only for full-owner mode. See
    [harness/permuter/README.md](harness/permuter/README.md).
  - A local model server (llama.cpp, Ollama, vLLM and so on), for the draft
    campaign.
  - `claude-agent-sdk` (`harness/requirements-agent-sdk.txt`), for the
    engineering lane.
  - `psutil`, for richer process info in the dashboard.

## Quick start

```sh
git clone <this repository> decomp-harness
cd decomp-harness

# 1. Plug in a project: copy the example and edit the root/version.
cp harness.example.json harness.local.json         # Windows: copy harness.example.json harness.local.json

# 2. Install m2c and apply the harness patches. autocrlf must be off so the
#    patches apply byte-exact (Git for Windows turns it on by default).
git clone --config core.autocrlf=false https://github.com/matt-kempster/m2c build/tools/m2c
git -C build/tools/m2c checkout 708d2d2cb2698f091a92492b328f73b24209f72d
python harness/local_campaign_m2c_patch.py

# 3. Build the project once (in its own checkout) so the report and tools exist,
#    then generate and validate drafts for every eligible function.
python harness/m2c_pipeline_runner.py --jobs 4 --validation-jobs 4 --context-declarations --readonly-data

# 4. Watch it.
python dashboard/server.py --open
```

Use `python` on Windows and `python3` where that is the interpreter's name.
Every command defaults to the selected project and its version.
`--root`/`--version` or the environment variables below override that.

## Configuration

`harness.local.json` sits at the repository root and is git-ignored. You can
instead point `DECOMP_HARNESS_CONFIG` at a config file anywhere. Relative paths
resolve against the config file's folder. See
[`harness.example.json`](harness.example.json).

| Key | Meaning |
|---|---|
| `projects[]` | `id`, `name`, `root` (checkout path), `version` or `versions`, and optional `references` (ids of other projects to use as cross-project evidence) |
| `default_project` | id selected when `DECOMP_PROJECT` is unset (default: the first project) |
| `tools.m2c` | m2c checkout (default `build/tools/m2c` in this repository) |
| `tools.compiler_wrapper` | override the MWCC wrapper (`"wibo"`, `["wine"]`, or `[]` for none) |
| `tools.brief`, `tools.simindex`, `tools.simindex_corpus`, `tools.mwdbg` | optional project-provided helper tools used by the draft campaign (function briefs, assembly-similarity index and its corpus name, MWCC register-allocator replay) |
| `tools.reference_toolchain` | compiler toolchain used to build reference corpora |
| `source_policy` | optional project policy checks run when packaging verified source (see [docs/LOCAL_LLM_CAMPAIGN.md](docs/LOCAL_LLM_CAMPAIGN.md)) |
| `tools.permuter`, `tools.ppc_binutils`, `tools.clang` | permuter farm tools (see [`harness/permuter/README.md`](harness/permuter/README.md)) |
| `permuter.*` | farm root, results directory, per-function policies |
| `reference_corpora[]` | optional external reference decomps used as matched-C evidence (`name`, `url`, `license`, `role`) |
| `dashboard.*` | bind address, port, permuter status file, m2c executors (see [`dashboard/README.md`](dashboard/README.md)) |

These environment variables override the config:

| Variable | Effect |
|---|---|
| `DECOMP_HARNESS_CONFIG` | config file to load |
| `DECOMP_PROJECT` | select a configured project by id |
| `DECOMP_ROOT` | project checkout; enough by itself when there is no config |
| `DECOMP_VERSION` | build version; otherwise the configured one, then the only `config/<VERSION>/build.sha1` in the root |
| `DECOMP_M2C`, `DECOMP_PERMUTER`, `DECOMP_CLANG`, … | any `tools.<name>` entry |
| `DECOMP_MODEL_HOST` | default OpenAI-compatible endpoint for draft attempts |
| `DECOMP_PYTHON` | interpreter used for project scripts (default: the current one) |

Each project's state goes in that project's ignored `build/` directory:
`build/decomp-support/<VERSION>/` for m2c batches, validation, refinement and
unit gates, and `build/local_llm_campaign/` for the draft campaign. State shared
across projects goes in this repository's ignored `build/`: the m2c checkout,
reference corpora and the reference registry.

## Layout

| Path | Contents |
|---|---|
| `harness/harness_paths.py` | configuration and project selection |
| `harness/portable.py` | cross-platform file locks and process-tree control |
| `harness/m2c_drafts.py`, `m2c_context.py` | m2c draft generation with type, readonly-data and cross-project reference context |
| `harness/m2c_validate_drafts.py`, `m2c_draft_attempts.py`, `m2c_compile_repair.py` | scratch MWCC/objdiff validation, conservative compile repair, optional model attempts |
| `harness/m2c_pipeline_runner.py`, `m2c_refinement_pipeline.py` | durable generation → validation → refinement pipeline with a stage journal |
| `harness/m2c_source_search.py`, `m2c_native_search.py`, `m2c_deterministic_transforms.py` | bounded, model-free refinement searches |
| `harness/m2c_unit_*.py` | owner-unit assembly, compile, link gate, proposals, review and (opt-in) SHA-1-gated promotion |
| `harness/m2c_remote.py`, `m2c_remote_worker.py` | hash-verified local/remote m2c execution pool |
| `harness/local_campaign_m2c_patch.py`, `m2c_patches/` | the harness's m2c patch series (GPL-3.0) and its applier |
| `harness/local_campaign_m2c_report.py`, `m2c_artifact_retention.py` | the compact m2c report the dashboard reads, and artifact pruning |
| `harness/permuter/` | permuter work-unit generator and farm supervisor |
| `harness/local_campaign.py`, `local_campaign_*.py` | local-LLM draft campaign: queue, workers, claims, shared build lock, rewrite and permute searches, register-colouring replay, evidence store, recovery and source packaging |
| `harness/local_campaign_bench.py` | model benchmark: fixed task sets, speed/health gate, paired comparisons, queue runner |
| `harness/claude_m2c_engineer.py` | Agent SDK m2c engineering lane |
| `harness/m2c_bootstrap_evidence.py`, `m2c_ghidra_inventory.py` | bootstrap evidence for stripped binaries (optional Ghidra) |
| `harness/test_*.py` | unit tests |
| `dashboard/` | read-only dashboard (server, web UI, tests) |
| `bench/queue.example.json` | example model benchmark queue |
| `docs/` | detailed documentation |
| `scripts/run_tests.py` | cross-platform test runner |

## Common commands

Run these from the repository root:

```sh
# m2c pipeline
python harness/m2c_pipeline_runner.py --jobs 4 --validation-jobs 4 --context-declarations --readonly-data [--auto-promote]
python harness/m2c_drafts.py --limit 200 --executor local=4 --executor http://127.0.0.1:8766=8
python harness/m2c_validate_drafts.py [--batch DIR]
python harness/m2c_remote_worker.py --tool build/tools/m2c --cache build/m2c-worker-cache   # on a worker machine

# permuter farm
python harness/permuter/gen_workunits.py --help
python harness/permuter/farm.py --help

# local-LLM draft campaign
python harness/local_campaign.py sync
python harness/local_campaign.py --worker w1 --ollama-host http://127.0.0.1:11434 --model MODEL run --retries 12 --recycle
python harness/local_campaign_bench.py queue bench/queue.local.json --stage prelim

# dashboard
python dashboard/server.py --open
```

Remote m2c workers bind to loopback by default. Reach them through an SSH
tunnel or a network you trust, then pass `--executor URL=SLOTS`. A worker
refuses any job whose m2c fingerprint doesn't match its own exact patched tree.

## Documentation

- [docs/M2C_PIPELINE.md](docs/M2C_PIPELINE.md): pipeline stages, artifacts and gates
- [docs/LOCAL_LLM_CAMPAIGN.md](docs/LOCAL_LLM_CAMPAIGN.md): draft campaign, benchmark and evidence store
- [docs/CLAUDE_M2C_ENGINEERING.md](docs/CLAUDE_M2C_ENGINEERING.md): Agent SDK m2c engineering lane
- [harness/permuter/README.md](harness/permuter/README.md): permuter farm
- [dashboard/README.md](dashboard/README.md): dashboard configuration and API

## Tested setup

Besides the unit tests, the whole stack has been run end to end, natively on
Windows 11, against a real in-progress dtk-template GameCube decomp built from
a retail disc image (RVZ):

- **Build:** the project built byte-exact (every `build.sha1` entry), with MWCC
  running natively.
- **Evidence:** reference corpora were compiled from two public doldecomp
  repositories, plus a sibling decomp's sources as a reference project.
- **Pipeline:** `m2c_pipeline_runner.py` drafted every unmatched function with
  typed cross-project context, validated each one with scratch MWCC/objdiff, and
  ran refinement. It left the workspace unchanged.
- **Permuter:** `gen_workunits.py` built and fidelity-gated permuter units, and
  `farm.py` ran them through the patched decomp-permuter with no bash and no
  `cpp`.
- **Dashboard:** it showed the project's progress, the m2c run and the live
  farm.

### Windows notes for project maintainers

Two problems in a project's own `configure.py` can stop it building on Windows,
even though the harness itself is fine:

- **Chained commands:** ninja starts Windows commands without a shell. A rule
  that chains commands (`mwcceppc ... && python tools/...`) needs `cmd /c` in
  front on Windows, or MWCC receives `&&` and everything after it as arguments.
- **Response files:** GNU ld treats backslashes in response files as escape
  characters. Partial-link rules that pass `@$out.rsp` built from `$in_newline`
  should pass `$in` directly on Windows instead.

## Tests

```sh
python scripts/run_tests.py            # harness + dashboard; node tests run when node is installed
python scripts/run_tests.py harness
```

No decomp checkout, retail files or network access is needed. Tests that need
m2c, Clang or binutils skip when those are missing. CI runs on Windows, Linux
and macOS.

## Safety and privacy

- Nothing about your machines lives in the repository: project paths, hosts and
  tool locations go in the untracked `harness.local.json`.
- The dashboard and remote m2c workers bind to `127.0.0.1` unless you say
  otherwise, and neither has authentication.
- Keep API keys in the environment or an ignored `.env`; never commit them or
  put them on a command line.
- You supply retail files, compilers and game data yourself. This repository
  contains none.

## License

MIT; see [LICENSE](LICENSE). `harness/m2c_patches/` is GPL-3.0 because it
modifies m2c, and `harness/permuter/` keeps its upstream MIT notice. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

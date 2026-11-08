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

## Dependencies

### Required

| Dependency | Why | How to get it |
|---|---|---|
| Python 3.11+ | runs the harness, dashboard and projects' `configure.py` | [python.org](https://www.python.org/downloads/), or your package manager. On Windows tick "Add python.exe to PATH". |
| Git | clones m2c and corpora, applies patches, runs gates | [git-scm.com](https://git-scm.com/downloads); Git for Windows on Windows |
| Ninja | builds the plugged-in project | `winget install Ninja-build.Ninja`, `brew install ninja`, `apt install ninja-build`, or `pip install ninja` |
| A dtk-template decomp | the project the harness works on (see step 2) | the project's own repository, plus your own disc image |
| m2c | generates drafts; the harness applies its own patch series (step 4) | cloned from [matt-kempster/m2c](https://github.com/matt-kempster/m2c) |

The core harness and dashboard use only the Python standard library. The
project's build downloads its own compilers, `dtk`, `objdiff-cli`, `sjiswrap`
and PowerPC binutils into its `build/` directory.

### Platform notes

- **Windows**: MWCC runs natively, so nothing else is needed.
- **Linux / macOS**: MWCC is a Windows program. dtk-template projects download
  [wibo](https://github.com/decompals/wibo) into `build/tools/wibo`
  automatically on x86 Linux. Otherwise install wibo or Wine and put it on
  `PATH`, or set `tools.compiler_wrapper`.
- **WSL** works like Linux. Keep checkouts on the Linux filesystem for speed.

### Optional, per feature

| Feature | Needs |
|---|---|
| Permuter farm | [decomp-permuter](https://github.com/simonlindholm/decomp-permuter) with the harness patch series (step 8), `pip install toml`, and PowerPC binutils (downloaded by the project build) |
| Permuter full-owner mode, source-package gate | Clang on `PATH` |
| Draft campaign / model benchmark | any OpenAI-compatible model server: [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server`, [Ollama](https://ollama.com), vLLM, … |
| Claude m2c engineering lane | `pip install -r harness/requirements-agent-sdk.txt` and an Anthropic API key |
| Project trial / auto-promote extras | `pip install -r harness/requirements-project-trial.txt` |
| Ghidra bootstrap evidence | [Ghidra](https://ghidra-sre.org) (`analyzeHeadless`) |
| Dashboard process details | `pip install psutil` (it falls back to built-in process listing without it) |
| Frontend tests | Node.js 18+ |

## Setup

Run the commands from the harness repository root. Use `python` on Windows and
`python3` where that is the interpreter's name.

### 1. Get the harness

```sh
git clone https://github.com/Count-Sheep/decomp-harness.git
cd decomp-harness
python scripts/run_tests.py          # optional: confirms the install (no project needed)
```

### 2. Build the decomp you want to work on

The harness drives an existing [dtk-template](https://github.com/encounter/dtk-template)
project. In that project's checkout, place your legally obtained disc image (ISO,
RVZ, …) or its extracted files under `orig/<GAMEID>/`, as the project's README
describes. Then build it once:

```sh
cd ../your-decomp
python configure.py
ninja                                # builds, checks build.sha1, writes build/<VERSION>/report.json
```

The build must finish byte-exact (`ninja` prints `N files OK`) before the
harness will draft anything. If a project fails to build on Windows, see
[Windows notes for project maintainers](#windows-notes-for-project-maintainers).

### 3. Plug the project in

```sh
cd ../decomp-harness
cp harness.example.json harness.local.json        # Windows: copy harness.example.json harness.local.json
```

Edit `harness.local.json` and set the project's `id`, `name`, `root` (the
checkout path, relative to this file) and `version` (the `config/<VERSION>`
directory name):

```json
{
  "default_project": "mygame",
  "projects": [
    {"id": "mygame", "name": "My game", "root": "../your-decomp", "version": "GAME01", "references": ["sibling"]},
    {"id": "sibling", "name": "Sibling decomp (evidence only)", "root": "../sibling-decomp", "version": "GAME02"}
  ]
}
```

Check the selection:

```sh
python -c "import sys; sys.path.insert(0, 'harness'); import harness_paths as h; print(h.PROJECT_ID, h.DECOMP_ROOT, h.VERSION)"
```

### 4. Install m2c with the harness patches

`core.autocrlf` must stay off so the patches apply byte-exact. Git for Windows
turns it on by default, and `--config` keeps it off for this clone.

```sh
git clone --config core.autocrlf=false https://github.com/matt-kempster/m2c build/tools/m2c
git -C build/tools/m2c checkout 708d2d2cb2698f091a92492b328f73b24209f72d
python harness/local_campaign_m2c_patch.py        # prints "applied" (or "already_applied")
```

### 5. Add cross-project evidence (recommended)

See [Cross-project evidence](#cross-project-evidence) for why this matters.
Add sibling decomps to `projects[]` and list them in your project's
`references`, add public corpora to `reference_corpora[]`, then build the
corpora:

```sh
python harness/reference_corpus.py sync           # shallow-clones each configured corpus
python harness/reference_corpus.py build --jobs 8 # compiles matching source with the right MWCC and indexes it
python harness/reference_corpus.py status
```

### 6. Run the m2c pipeline

```sh
python harness/m2c_pipeline_runner.py --jobs 8 --validation-jobs 8 --context-declarations --readonly-data
```

This drafts every unmatched function, validates each draft in a scratch
workspace with the project's own compiler flags, and runs bounded refinement and
owner-unit gates. Results go in `<project>/build/decomp-support/<VERSION>/`.
Nothing in the project changes unless you add `--auto-promote`, which commits a
unit only after the full build still matches every SHA-1.

### 7. Watch it

```sh
python dashboard/server.py --open                 # http://127.0.0.1:8765
```

### 8. Permuter farm (optional)

```sh
# a permuter checkout with the harness patches, in the project's default farm location
git clone --config core.autocrlf=false https://github.com/simonlindholm/decomp-permuter ../your-decomp/build/permuter-farm/decomp-permuter
git -C ../your-decomp/build/permuter-farm/decomp-permuter checkout 8556c81
python harness/permuter/apply_permuter_patches.py --root ../your-decomp/build/permuter-farm/decomp-permuter
python -m pip install toml

# queue near-matches as a TSV: tier, pct, size, name, addr, unit (see harness/permuter/README.md)
python harness/permuter/gen_workunits.py --queue build/permuter_queue.tsv --limit 20
python harness/permuter/farm.py --workers 8 --budget 600
```

Wins land in `<project>/build/permuter_results/` for review. The dashboard picks
up the farm automatically.

### 9. Local-LLM draft campaign (optional)

Start any OpenAI-compatible server, for example `llama-server -m MODEL.gguf --port 8080`
or `ollama serve`. Then:

```sh
python harness/local_campaign.py sync
python harness/local_campaign.py --worker w1 --ollama-host http://127.0.0.1:11434 --model MODEL run --retries 12
```

See [docs/LOCAL_LLM_CAMPAIGN.md](docs/LOCAL_LLM_CAMPAIGN.md) for workers, claims
and the model benchmark.

Every command defaults to the selected project and its version.
`--root`/`--version` or the environment variables below override that.

## Cross-project evidence

GameCube and Wii games share a lot of code that was compiled from the same
source: the Dolphin/Revolution SDK, the MSL C runtime, MetroTRK, middleware
such as JSystem, and engine code a studio reused across its games. Once one
decomp has matched a function like that, others can learn from it. The harness
uses two kinds of evidence.

- **Reference projects**: other decomp checkouts listed in `projects[]` and
  named in your project's `references`. Only their exact, source-backed functions
  are used. If a reference project is also built (its own `report.json`), its
  retail bytes can be compared directly.
- **Reference corpora**: public decomps listed in `reference_corpora[]`. These
  don't need that game's disc. `reference_corpus.py` compiles only the source
  upstream marks as matching, with the compiler version upstream uses, then
  indexes the resulting assembly next to its C.

Where the evidence is used:

1. **Reference examples for model-backed drafting.** Every target is scored
   against the reference pool by opcode mix, size, control flow, calls and stack
   frame. The closest matched functions, with their real C, are attached to each
   draft and passed to the models in `m2c_draft_attempts.py` and the local-LLM
   campaign. This is where similar code helps most: a model that sees how a
   structurally similar function was written produces matching C more often.
2. **Code-verified reference candidates.** When a reference's compiled bytes
   match the target, validation scores that reference's C as a candidate
   alongside m2c's output.
3. **Exact reuse.** When an exact-function inventory
   (`build/decomp-support/cross-project-exact.json`) lists a byte-identical
   function in another project, the draft is marked for reuse instead of
   decompiled, and it gains priority.
4. **Prioritization and templates.** Reference similarity raises a function's
   draft priority. The template index and the campaign's library-reference
   lookup search the same pool by instruction shape and symbol name.

Evidence does **not** change the C that m2c itself emits. m2c works from the
target's assembly and the project's own headers. A holdout benchmark (below)
with a 64,000-function corpus produced byte-identical m2c drafts with and
without the corpus. So measure corpora with the model-backed stages, not with
raw m2c drafts.

Evidence helps most when it shares the target's SDK version, compiler version
or engine. A run records what it used: the m2c report lists `reference_pool`,
`reference_projects` and `reference_examples`, and the dashboard shows them.

Most public GameCube and Wii decomps use the dtk template, and for those one line
per repository is enough. `"kind": "dtk"` runs the corpus's own `configure.py`
with its build step intercepted, so no disc image is needed, and compiles every
`Matching` object with the MWCC version, flags and Shift-JIS setting that project
uses. Sources that need disc-generated headers just fail and are reported.
Repositories that fail to clone or configure are skipped.

```json
"reference_corpora": [
  {"name": "sibling-a", "url": "https://github.com/OWNER/sibling-a-decomp.git", "license": "see upstream",
   "role": "same engine as the target", "jobs": {"kind": "dtk"}},
  {"name": "sibling-b", "url": "https://github.com/OWNER/sibling-b-decomp.git", "license": "see upstream",
   "role": "SDK and runtime of the same era", "jobs": {"kind": "dtk"}}
]
```

Repositories that aren't dtk-template projects use `"kind": "sources"` with an
explicit compiler and flags (the format is documented in
[`harness/reference_corpus.py`](harness/reference_corpus.py)):

```json
"reference_corpora": [
  {
    "name": "dolsdk2001",
    "url": "https://github.com/doldecomp/dolsdk2001.git",
    "license": "see upstream",
    "role": "Dolphin SDK source",
    "jobs": {
      "kind": "sources",
      "sources": ["src/**/*.c"],
      "compiler": "GC/1.2.5",
      "flags": "-char unsigned -nodefaults -proc gekko -fp hard -Cpp_exceptions off -enum int -O4,p -inline auto -I- -Iinclude -Iinclude/libc -ir src -DRELEASE",
      "object_dir": "build/release"
    }
  }
]
```

`"kind": "ninja"` reads an already built dtk-template project's `build.ninja` and
`objdiff.json`. `"kind": "configure"` statically parses the `Object(Matching, ...)`
entries of the library functions you name.

To measure what your evidence actually buys, draft a seeded sample of functions
the project has already matched. The project's own code is never used as
evidence, so the scores show what drafting would have achieved before those
functions were matched:

```sh
python harness/m2c_drafts.py --holdout 300 --seed 1 --min-bytes 4 --max-bytes 65536 --context-declarations --readonly-data
python harness/m2c_validate_drafts.py --batch <printed folder> --refinement-limit 0 --keep-superseded
```

Run it with different `reference_corpora` and compare the results.
Corpus checkouts and indexes go in this repository's ignored `build/`, and
compilers come from `tools.reference_toolchain` (any built dtk-template checkout;
default: the selected project).

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

## More commands

```sh
python harness/m2c_drafts.py --limit 200 --executor local=4 --executor http://127.0.0.1:8766=8   # drafts only, with a remote worker
python harness/m2c_validate_drafts.py [--batch DIR]                                               # validate an existing batch
python harness/m2c_remote_worker.py --tool build/tools/m2c --cache build/m2c-worker-cache          # on a worker machine
python harness/local_campaign_bench.py queue bench/queue.local.json --stage prelim                # model benchmark
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

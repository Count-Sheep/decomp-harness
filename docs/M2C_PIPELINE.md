# m2c draft pipeline

The m2c pipeline turns every eligible unmatched function of the selected project
into an [m2c](https://github.com/matt-kempster/m2c) draft, scores each one with
the project's real MWCC and objdiff in scratch directories, refines the closest
results with bounded model-free searches, and then groups exact functions by
owner unit and checks each unit against the full link's SHA-1. Nothing is applied
to the project unless you pass `--auto-promote`.

Every command below runs from the repository root and defaults to the selected
project and version (see the README's configuration section). `--root` and
`--version` override them where a script accepts them.

## Verification principles

- **A scorer match is not verification.** objdiff's 100% is recorded as
  `scorer_exact`. `instruction_exact` additionally needs a compiled object, an
  empty diff and nonempty, equal target/compiled instruction lists. Neither proves
  relocations, data, sibling functions or the linked image.
- **The SHA-1 link gate is the bar.** A unit is `link_verified` only when its
  compiled object, substituted into a captured link, reproduces the first SHA-1 in
  `config/<VERSION>/build.sha1`. The captured baseline has to reproduce it first.
- **Review-only by default.** Validation results carry `review_eligible: false`
  and `promoted: false`. Proposals, project trials and reviews are packaged
  evidence, not applied changes. Only `--auto-promote` writes to the project,
  and only under the conditions in [Promotion](#opt-in-promotion).
- **Inputs are hash-bound.** Batches, validations, refinements and unit gates
  record the hashes of their inputs and producers. A changed input makes a
  stage refuse to resume ("belongs to different inputs") instead of mixing
  evidence. Drafting and validation both refuse to start unless
  `build/<VERSION>/main.dol` already matches `build.sha1`.

## Prerequisites

1. A built dtk-template checkout (`report.json`, `objdiff.json`, `build/tools/`,
   `build/compilers/`), as in the README.
2. An m2c checkout at the pinned base revision with the patch series applied:

   ```sh
   git clone --config core.autocrlf=false https://github.com/matt-kempster/m2c build/tools/m2c
   git -C build/tools/m2c checkout 708d2d2cb2698f091a92492b328f73b24209f72d
   python harness/local_campaign_m2c_patch.py --check   # prints ready / already_applied
   python harness/local_campaign_m2c_patch.py           # prints applied / already_applied
   ```

   `--config` stores `core.autocrlf=false` in the clone's own config, so the
   checkout that follows also keeps LF line endings. `git -c core.autocrlf=false
   clone` affects only the clone command; on Git for Windows the following
   checkout would then rewrite files as CRLF and the patches would not apply.

The applier (`local_campaign_m2c_patch.py`, `--root` to point elsewhere; default
`tools.m2c` / `DECOMP_M2C`, else `build/tools/m2c`) refuses any other base
revision and leaves the checkout unchanged on conflict. It applies every
`harness/m2c_patches/NNNN-*.patch` in order, skips patches that are already
installed, and preflights overlapping patches on copies before applying one
combined diff. Patches stored with CRLF (a converted checkout of this repository)
are restored to LF first. `harness/m2c_patches/NOTICE.txt` lists what each patch
changes. Some flags depend on specific patches: `--context-declarations` needs
0015, for example.

## Running it

```sh
python harness/m2c_pipeline_runner.py --jobs 4 --validation-jobs 4 \
    --context-declarations --readonly-data [--executor local=4 --executor URL=SLOTS] [--auto-promote]
```

`m2c_pipeline_runner.py` drafts **every** eligible function (limit 0, 4 to 65536
bytes by default) and then validates that batch. Validation in turn runs
refinement and the unit gates. It keeps a journal in
`build/decomp-support/<VERSION>/pipeline-state.json`:

| Field | Meaning |
|---|---|
| `status` | `active` while running or interrupted, `complete` when done |
| `stage` | `generation`, then `validation`, then `complete` |
| `config`, `config_sha256` | every option plus the m2c revision and tool fingerprint |
| `batch`, `generation_run_id` | the batch the generation stage produced |
| `last_error` | the exception that stopped the last run, if any |

Rerun the same command to resume. An active journal whose configuration hash
differs is refused, so finish or remove it before you change options. A completed
journal is replaced by the next run.

| Runner option | Default | |
|---|---|---|
| `--jobs`, `--timeout` | 1, 120 s | m2c processes and per-function timeout |
| `--min-bytes`, `--max-bytes` | 4, 65536 | function size window |
| `--context-declarations` | off | emit referenced context declarations |
| `--readonly-data` | off | give m2c the target owner's relocation-free readonly bytes |
| `--executor` | local | weighted execution slots (see below) |
| `--validation-jobs` | 3 | concurrent scratch validations (capped at 8) |
| `--refinement-limit` | 24 | validation winners refined (0 to 32) |
| `--source-variants`, `--native-variants` | 64, 3 | refinement search budgets |
| `--auto-promote` | off | opt-in promotion |

The stages can also be run on their own, as described below.

## Stages

### 1. Generation: `m2c_drafts.py` and `m2c_context.py`

```sh
python harness/m2c_drafts.py --limit 200 [--min-bytes 24 --max-bytes 256] [--start 0x80001000 --end 0x80002000]
python harness/m2c_drafts.py --replay build/decomp-support/VERSION/batches/RUN_ID   # same targets, current pipeline
```

Candidates are `.text` functions of active units from `config/<VERSION>/symbols.txt`
whose dtk assembly covers exactly the symbol's address range and that the report
does not already list as exact. Without `--limit 0`, the top functions by a cheap
priority are taken: byte-identical cross-project source, 24 to 160 bytes, no
indirect calls, few placeholder fields, few calls, simple control flow.

For each target `m2c_context.build_knowledge` assembles deterministic context:
parsed assembly features, the inferred compiler profile, referenced types,
matched references from other projects, and bootstrap hints. The catalog is
built once per batch from:

- the configured reference projects (`references` on the project entry, or every
  other configured project with a checkout; see `harness_paths.reference_projects`);
- active installed reference corpora (`reference_corpus.py`, below);
- `build/decomp-support/cross-project-exact.json` in this repository, when present;
- `<project>/build/decomp-support/<VERSION>/bootstrap-evidence.json`, used only
  while every input it names still has its recorded hash.

Its reference index is cached in `reference-context-v1.json`. When a target has
byte-identical, source-backed code in another project, m2c is skipped and the
row becomes `exact_reuse`, with the snippet under `exact-reuse/`.

**Remote execution.** `--executor local=N` and `--executor http(s)://HOST:PORT=N`
build one weighted slot pool. The total slot count replaces `--jobs`. Before a
batch starts, each remote worker's `/v1/health` must report the same m2c tool
fingerprint (revision plus a hash of `m2c.py`, `m2c/` and `m2c_pycparser/`). Each
job is SHA-256-bound, and replies for another job or tool tree are rejected. A
job never silently moves to another executor. On the worker machine:

```sh
python harness/m2c_remote_worker.py --tool build/tools/m2c --cache build/m2c-worker-cache [--host 127.0.0.1 --port 8766]
```

The worker binds loopback by default and has no authentication. Reach it through
an SSH tunnel or a network you trust.

### 2. Validation: `m2c_validate_drafts.py` and `m2c_compile_repair.py`

```sh
python harness/m2c_validate_drafts.py [--batch DIR] [--jobs 4] [--force] [--seed-validation DIR] [--keep-superseded]
```

Without `--batch`, the newest finished batch is used. Each row is compared in
isolation, with the inferred (or fallback) compiler profile, against up to three
kinds of candidate:

- the m2c draft, after `m2c_compile_repair` (it fixes only mechanically provable
  C problems such as missing externs, unknown placeholder types and `void *`
  arithmetic, and records an audit trail and blockers for the rest);
- code-verified references whose bytes match exactly, renamed to the target;
- the project's checked-in function, measured by `m2c_current_source.py`.

The best one becomes `winner.c` and the row gets one status: `scorer_exact`,
`non_exact`, `compile_error`, `objdiff_error`, `validation_error`,
`missing_draft`, `linked_elsewhere` (now source-backed) or
`exact_reuse_available`. The environment fingerprint covers the batch, compilers,
objdiff-cli, target objects and the harness modules involved. It is recomputed at
the end, and `environment_unchanged` must be true for later stages to accept the
validation. `--seed-validation` reuses completed rows of an interrupted pass for
the same batch and workspace, but only when the draft hashes match.

### 3. Optional model attempts: `m2c_draft_attempts.py`

```sh
python harness/m2c_draft_attempts.py --host http://127.0.0.1:18080/v1 [--model MODEL] [--max-attempts 8] [--loop]
```

This is a resumable, review-only loop over a completed batch's drafts. Attempt
`000` scores the raw draft, and later attempts ask an OpenAI-compatible endpoint
(`--host`, default `DECOMP_MODEL_HOST`) for a corrected function, with the
previous instruction diff as feedback. The ledger is `attempts/state.json`, and
each attempt keeps its files in `attempts/<symbol>/<NNN>/`. The dashboard shows
it. It is not part of `m2c_pipeline_runner.py`.

### 4. Refinement: `m2c_refinement_pipeline.py`

Validation calls this automatically when the project has `objdiff.json` and a
report. Up to `--refinement-limit` `non_exact` rows at 98% or better are refined,
closest first:

1. **Native search** (`m2c_capture_inputs.py`, `m2c_native_search.py`): for
   m2c-origin winners whose compiler profile is supported, the inputs are
   captured with fresh context and m2c is regenerated with option variants chosen
   from the compiled feedback, for example stack-storage declarations for
   undefined `spNN` identifiers or a collapsed frame, or string heuristics for
   invalid symbol names. A variant replaces the seed only if it is object-equal
   or scores higher.
2. **Source search** (`m2c_source_search.py`, `m2c_deterministic_transforms.py`):
   a beam search (width 3, depth 3, at most 64 variants) over
   semantics-preserving rewrites of the seed inside the real owner file,
   compiled with the owner's own Ninja command. The variant categories are
   declaration order and scope, initialization shape, the `register` storage
   class, statement order, expression shape and control flow. When the remaining
   mismatch is register allocation only, rewrites at the mismatching source lines
   are tried first, and static-inline helpers of the owner may be varied too.

Each search uses no model (`model_inputs: []`). The run record is in
`refinement-runs/<validation>/results.json` and each search's record in
`refinements/auto-<hash>/results.json`. Exactness here means
`object_audit.scratch_object_equal` (`m2c_object_evidence.py`: function bytes and
relocations), not the scorer.

### 5. Owner-unit gates: `m2c_unit_autogate.py` and `m2c_unit_*.py`

```sh
python harness/m2c_unit_autogate.py --validation build/decomp-support/VERSION/validation/NAME/results.json \
    --refinement build/decomp-support/VERSION/refinements/auto-HASH/results.json \
    --output-root build/decomp-support/VERSION/unit-gates [--skip-owner-trials] [--auto-promote]
```

Exact candidates from validation and refinement are grouped by owner unit.
`m2c_unit_pipeline.py` then advances a unit only through verified stages:

| Stage | Module | Evidence |
|---|---|---|
| inventory | `m2c_unit_inventory.py` | complete owner object; claims nothing |
| source | `m2c_unit_source.py`, `m2c_unit_data.py`, `m2c_unit_preprocess.py` | parsed drafts assembled without guessing between conflicting declarations; typed data and pointer tables; native MWCC preprocessing captured without running the build graph |
| compile | `m2c_unit_compile.py`, `m2c_unit_evidence.py` | the unit compiled with the captured owner settings and compared conservatively against the target object |
| link | `m2c_unit_link.py`, `local_campaign_link_gate.py` | `link_verified`: the captured link reproduces `build.sha1` |
| proposal | `m2c_unit_proposal.py` | reviewable patch and config changes, never applied |
| project trial | `m2c_unit_project_trial.py` | the unchanged project and the proposal both built in a fresh workspace |
| review | `m2c_unit_review.py` | explicit approve/reject with reviewer and notes |

The autogate first runs owner trials (one exact function swapped into an
existing source owner, or retained drafts for owners without source), then a full
gate for every unit that needs one. `unit-gates/active.json` reports the current
phase for the dashboard. `m2c_unit_queue.py` publishes records to
`unit-promotions/` and recomputes readiness on every read, so a changed input
revokes a displayed approval.

**Link gate.** `local_campaign_link_gate.py` captures an isolated link capsule
from Ninja's compilation database: response-file input order, the MWCC linker,
the wrapper, dtk, the linker script and every object hash. Unsupported link
flags, external linker-script dependencies and ambiguous object slots are refused
rather than ignored. Each candidate object replaces exactly one slot and is
linked separately. Two contexts exist: `native` links the objects named by the
current build graph, and `retail-reference` (the CLI default) maps each slot to
its retail counterpart through objdiff, which verifies the candidate in that
recorded context, not the native C build. It can also gate recovery runs
directly (see [LOCAL_LLM_CAMPAIGN.md](LOCAL_LLM_CAMPAIGN.md)):

```sh
python harness/local_campaign_link_gate.py --root PROJECT --recovery RUN_DIR --output NEW_DIR \
    [--context native|retail-reference] [--target build/VERSION/main.elf] [--checksum-file config/VERSION/build.sha1]
```

Output directories are never reused, so checksum history is kept.

### Opt-in promotion

With `--auto-promote` (runner, validator or autogate), `m2c_unit_auto_promote.py`
promotes a link-verified unit only if all of these hold:

- the project is on `main` or `master`, with a clean worktree;
- the link evidence is still current and the proposal's base commit is `HEAD`;
- `configure.py` plus `ninja build/<VERSION>/ok` reproduces the retail SHA-1;
- the build changed no files outside the proposal.

It then commits `Match <unit>`. A lock in the project's git directory
(`m2c-auto-promote.lock`) prevents concurrent promotion, and an interrupted
promotion is completed or rolled back on the next run.

**Candidate bank** (`m2c_candidate_bank.py`, also gated by `--auto-promote`)
banks standalone exact candidates as visible source and links certified leaves.
It runs only when the project opts in with a `.m2c-candidate-bank.json` contract
at its root (`schema_version: 1`, and per version `manifest`, `source_dir`,
`split_file`, `section`); otherwise it reports `unsupported`. It needs the same
clean `main`/`master` worktree and an unchanged retail SHA-1, and restores its
snapshot on any failure.

## Artifact layout

All state for a project version lives in `<project>/build/decomp-support/<VERSION>/`:

| Path | Written by | Contents |
|---|---|---|
| `batches/<run_id>/` | drafts | `targets.json` (identity + frozen targets), `input/<id>.s`, `results/<id>.json`, `manifest.json`, `results.json` when finished |
| `drafts/` | drafts | content-addressed m2c artifacts (`draft.c`, `knowledge.json`, inputs) |
| `exact-reuse/<hash>/` | drafts | `reuse.c` and evidence for byte-identical cross-project source |
| `validation/<batch>-<env12>/` | validation | `<ADDRESS>/result.json` + `winner.c`, `manifest.json`, `results.json` |
| `attempts/` | model attempts | `state.json` ledger, `<symbol>/<NNN>/` |
| `refinement-runs/<validation>/` | refinement | `results.json`, `native-inputs.json`, `native/` |
| `refinements/auto-<hash>/` | source search | `results.json` and the variants |
| `unit-gates/<key>/` | autogate | `result.json`, `owner-trials/`, `units/`, `candidates/`, `promotions/`, `owner-promotions/`, `candidate-bank/` |
| `unit-gates/active.json` | autogate | live phase while a gate runs |
| `unit-promotions/`, `unit-reviews/` | unit queue, review | hash pointers to published evidence; current reviews |
| `reference-context-v1.json` | context | cached reference index |
| `bootstrap-evidence.json` | you | optional output of `m2c_bootstrap_evidence.py` |
| `pipeline-state.json` | runner | stage journal |
| `m2c-report.json` | report | compact dashboard index |

`local_campaign_m2c_report.py` rebuilds `m2c-report.json` after each stage
(`python harness/local_campaign_m2c_report.py [--support-root DIR]`). The batch
and validation directories stay authoritative.

## Reference projects and corpora

- **Configured projects.** Any other project in `projects[]` can serve as
  evidence. List ids in a project's `references` to restrict and order them.
  Only their exact, source-backed functions are used.
- **External corpora.** `reference_corpora[]` entries need `name`, `url`,
  `license` and `role`, and optionally `configure`, `corpus` and `jobs` (see the
  `reference_corpus.py` docstring for `sources` and `configure` job kinds). Only
  source that upstream marks matching, or that an entry lists explicitly, and
  that compiles locally to a PowerPC object is indexed:

  ```sh
  python harness/reference_corpus.py sync [--refresh]   # shallow clones into build/decomp-support/reference-projects/
  python harness/reference_corpus.py build [--jobs 4]   # compiles, indexes, writes reference-corpora/registry.json
  python harness/reference_corpus.py status
  ```

  Compilers and dtk come from `tools.reference_toolchain`
  (`DECOMP_REFERENCE_TOOLCHAIN`), else the selected project. These directories
  sit under this repository's ignored `build/`, not the project's.

## Artifact retention

Validation prunes superseded artifacts unless you pass `--keep-superseded`. To run
it by hand (a dry run unless `--apply` is given):

```sh
python harness/m2c_artifact_retention.py <project>/build/decomp-support/VERSION [--keep-recent 2] [--apply]
```

It keeps the newest active batch, the `--keep-recent` newest finished batches,
the newest and best batch per symbol, replay baselines, and anything referenced
from unit or review records. It removes the other batches with their validations,
stale unfinished validations, orphaned draft caches and inputs, and compacts the
attempt ledger.

## Experiment tools

These write only to the output you name, send no model requests and never touch
project source. Use them to test m2c or harness changes on frozen inputs:

| Tool | Purpose |
|---|---|
| `m2c_failure_audit.py --root R --version V --output SNAP.json` | freeze drafts, attempt-0 outcomes and compiler diagnostics into a snapshot with overlapping failure categories (needs `attempts/state.json`) |
| `m2c_repair_experiment.py SNAP.json --output DIR [--category C] [--exact-controls 30] [--templates IDX]` | replay repair passes or template transfers on a snapshot, one compile at a time, with exact controls |
| `m2c_generation_experiment.py SNAP.json --baseline M2C_A --candidate M2C_B --output DIR` | raw A/B of two m2c trees on identical inputs, with no harness repair; option flags select candidate behaviours |
| `m2c_capture_inputs.py --batch B --symbol S --output FILE`, `--verify FILE` | capture completed-batch inputs for selected symbols; verify before use |
| `m2c_template_index.py SNAP.json --output IDX [--drafts-only]` | index exact drafts and source-backed references by PowerPC structure |
| `m2c_native_search.py`, `m2c_object_evidence.py` | standalone native variant search and object audit on a capture |
| `m2c_worker_review.py --output FILE` | export retained model-attempt gains as review-only m2c evidence |
| `m2c_bootstrap_evidence.py --ghidra INV --matches M --output FILE` | reviewable naming and owner hints for a stripped binary; hints only until compiled and verified |
| `m2c_ghidra_inventory.py --binary B --workspace W --project P --output O --log L` | hash-bound Ghidra inventory (`analyzeHeadless` from `GHIDRA_HOME`, `PATH` or `--analyze-headless`) |

The [Claude m2c engineering lane](CLAUDE_M2C_ENGINEERING.md) consumes such
snapshots for automated, A/B-gated m2c fixes.

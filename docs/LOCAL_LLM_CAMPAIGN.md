# Local-LLM draft campaign

`harness/local_campaign.py` asks a local model server for one replacement C
function at a time and measures every answer with the project's own MWCC and
objdiff. It reads the canonical `build/<VERSION>/report.json`, queues every
function below 100% that has an editable source owner, and keeps every prompt,
reply and candidate in ignored state under `<project>/build/local_llm_campaign/`.

Verification temporarily writes a candidate into the owner source under a shared
build lock, measures it and restores the original bytes. A 100% result is a
review candidate (`review_exact`), not accepted source. Only the opt-in
`run --promote` writes to the project (see [Promotion](#promotion)).

All commands run from the repository root against the selected project
(`DECOMP_PROJECT`, `DECOMP_ROOT`, `DECOMP_VERSION`; see the README).

## Model server

Any Ollama or OpenAI-compatible server works (llama.cpp's `llama-server`,
Ollama, vLLM and so on).

- A host ending in `/v1` is spoken to as an OpenAI-compatible streaming chat API
  (`/chat/completions`). `--think` becomes the chat template's thinking switch.
- Any other host is Ollama (`/api/generate`). The default host is
  `http://127.0.0.1:11434`.
- On llama-server, long prompts are measured with its own tokenizer and compacted
  to fit, instead of being truncated.

## Quick start

```sh
python harness/local_campaign.py sync          # rebuild the report, then queue the work
python harness/local_campaign.py --worker w1 --ollama-host http://127.0.0.1:11434 --model MODEL run --limit 1   # smoke test
python harness/local_campaign.py --worker w1 --ollama-host http://127.0.0.1:8080/v1 --model MODEL \
    run --retries 12 --recycle
python harness/local_campaign.py status
```

Global options go **before** the subcommand:

| Option | Default | Meaning |
|---|---|---|
| `--ollama-host` | `OLLAMA_HOST`, else `http://127.0.0.1:11434` | model server |
| `--model` | `OLLAMA_MODEL`, else a built-in tag | model id the server serves |
| `--worker` | `LOCAL_CAMPAIGN_WORKER`, else `Local LLM` | worker name for claims, locks and the dashboard |
| `--num-predict` | `OLLAMA_NUM_PREDICT`, else 4096 | output token cap per request |
| `--num-ctx` | `OLLAMA_NUM_CTX`, else 32768 | context for prompts that fit (Ollama only; at least 2048) |
| `--think auto\|on\|off` | `auto` | `auto` sends nothing and lets the model decide |
| `--reasoning-tokens` | 2048 | extra output allowance under `--think on`; match the server's reasoning budget |
| `--temperature` | retry schedule | one fixed temperature for every attempt |
| `--sampling JSON` | `{}` | extra sampling fields, e.g. `'{"top_p": 0.95, "top_k": 20, "min_p": 0}'` |
| `--template-kwargs JSON` | `{}` | chat-template options (llama-server only) |

## Subcommands

| Command | Purpose |
|---|---|
| `sync [--reset]` | run `ninja all_source build/<VERSION>/report.json` and refresh the queue; `--reset` restarts existing tasks from pending |
| `run [options]` | process pending tasks (below) |
| `status` | queue counts by status and current settings |
| `claim TASK [--worker W] [--detail D]` | reserve a function's whole owner source; prints the source path and a token |
| `release SOURCE --token T` | release a manual claim |
| `claims` | current claims, waiting builds and their holders |
| `build [--worker W] -- COMMAND...` | run a command from the project root under the shared build lock; keeps its exit code |
| `rewrite SYMBOL [--seconds 600] [--sideways 6] [--seed 0] [--worker W]` | run the rewrite search on any report function |
| `explain SYMBOL [--worker W]` | replay the function's register allocation against retail |
| `breather add [--sync \| -- COMMAND] [--description D] [--by WHO]`, `breather list` | queue a job for a running worker's next pause |

`sync` also queues **policy-cleanup** tasks: functions that only reach 100% under
a forbidden local pragma (`optimization_level`, `optimize_for_size`,
`scheduling`, `peephole`, `opt_propagation`). For these, the function is
compiled under its unit's normal flags, and the model is asked for pragma-free C.
Functions without an editable owner are kept as `blocked_no_editable_source` or
`blocked_source_context`, and hand-written assembly or inactive definitions as
`blocked_asm_only` / `blocked_not_compiled`. The runner never invents an owner or
a signature.

## Run options

| Option | Default | Meaning |
|---|---|---|
| `--limit N` | 0 (all) | tasks to take |
| `--timeout S` | 900 | per-request timeout |
| `--retries N` | 1 | correction rounds after the first attempt |
| `--stall-rounds N` | 4 | stop a task after N rounds without a new best |
| `--task-seconds S` | off | spend a wall-time budget per task instead of `--retries` (at most 64 attempts) |
| `--min-pct P` | 0 | only functions already at least P% matched |
| `--max-function-bytes N` | 0 (none) | size ceiling for a fast worker |
| `--lowest-first` | off | take the furthest functions first, the priority list last |
| `--recycle` | off | when nothing is pending, re-queue attempts older than the newest `harness_improved` / `model_tuned` event in `state.json`. No command records these events, so with none nothing is re-queued |
| `--diversify` | off | after an unchanged or repeated answer, sample hotter and name one kind of edit to try |
| `--focus-variants N` | 3 | alternatives requested per focused edit |
| `--permute`, `--permute-cap`, `--permute-seconds` | off, 720, 600 | declaration-order search after the model rounds |
| `--rewrite`, `--rewrite-seconds` | off, 600 | rewrite search after the model rounds |
| `--breather S` | 15 | pause after each task, holding no claim or lock |
| `--delay S` | 0 | sleep between tasks |
| `--promote` | off | commit improvements (see [Promotion](#promotion)) |

Tasks are ordered by `value_score`, then closeness. Symbols listed in
`build/local_llm_campaign/priority.json` (`{"symbols": [...]}`) come first and
bypass `--min-pct` and `--max-function-bytes`. `value_score` (from
`local_campaign_priority.py`) is a scheduling hint only: 6 x learning (references
from other unresolved functions, from retail relocations) + 2 x ease (closeness
and size) + unit completion (the last few residuals of a scoring unit).

## Workers, claims and the build lock

- **Workers.** Each running worker needs its own `--worker` name. A name is
  single-instance (`runners/<name>.lock`), and different names run in parallel on
  different owners. Generation runs concurrently. Verification, report reads and
  scratch compiles take the exclusive build lock.
- **Claims.** A claim reserves an entire owner file, including its other
  functions. Workers skip claimed owners. Automatic claims belong to a live
  process and are dropped when it dies. Manual `claim` reservations last until
  `release`. Claim before you edit by hand, and wrap your own builds in
  `local_campaign.py build -- ninja ...`. The claims and the lock are advisory:
  a raw `ninja` or an edit to an unclaimed file bypasses them.
- **Sync vs. run.** `sync` needs the fleet lock exclusively and is refused while
  any runner holds it. To refresh a running fleet, queue
  `breather add --sync`: the worker runs the sync between tasks.
- **Machine-wide lock.** `DECOMP_HEAVY_LOCK=PATH` names an extra lock file that
  promotion rebuilds share with other heavy build users on the machine.
- **Stopping.** SIGTERM/SIGINT finish the current verification and restore the
  owner first. If a restore ever fails, `halt.json` stops every worker. Repair
  and rebuild the baseline under the build lock, then delete it.
- **Stale source.** The owner's hash is checked again under the lock before
  verifying. An edit since `sync` marks the attempt `stale_source`, and the next
  `sync` re-queues it against the new source.

Keep one checkout and one state directory per fleet. Remote machines should only
serve the model, or reach the same checkout, so claims and candidates do not
diverge.

## The attempt loop

Every task visit (`work_task`, shared with the benchmark) runs as follows:

1. **Prompt.** The prompt holds a fresh scratch compile of the owner with
   `-sym on`, so objdiff reports the source line behind each instruction. Hunks
   are ranked structural first, then register-only, and each quotes the C that
   produced it. Rows that differ only by branch displacement or by a compiler
   literal against a named constant are dropped. The prompt adds referenced
   types, same-file callees and data, mismatch-specific MWCC hints and matched
   examples (see [Context sources](#context-sources)). A function that already
   measures 100% is verified without a model request.
2. **Answer checks.** A reply that repeats itself is cut off as `model_loop`. An
   unchanged answer is `no_change` and is not built. A repeat of an earlier answer
   is `duplicate`. Preprocessor directives, inline asm, `.inc` references,
   `_Pragma` and trigraphs are `policy_rejected`. Added `volatile`,
   self-assignment, `goto`, `typedef`, `struct` and `union` are flagged for audit.
3. **Retries.** Temperatures rise across retries (0.15, 0.45, 0.7, 0.9) unless
   `--temperature` is set. Feedback is anchored on the best candidate so far,
   lists up to six earlier answers with their scores, and quotes MWCC's
   diagnostics instead of the build log. Output caps follow the function's size.
4. **Focused edits.** After an unchanged answer, a cut-off answer, a whole-function
   plateau, a register-only residue, or for a function too long to return whole,
   the model rewrites only the lines behind one hunk, in `--focus-variants`
   alternatives. Regions never start at the opening brace and always cover whole
   statements. Snippet declarations are repaired (moved, re-declared or dropped
   locals and parameters) before compiling. A snippet that adds an early
   `return`/`goto`/`break`/`continue` is refused as `invalid_edit`.
5. **Searches** (tasks at 97% or better, once per source hash):
   - `--permute` (`local_campaign_permute.py`) tries orderings of the leading
     declaration block, since MWCC hands out saved registers largely by
     declaration order.
   - `--rewrite` (`local_campaign_rewrite.py`) hill-climbs over
     meaning-preserving rewrites: operand swaps and mirrored comparisons where
     precedence and side effects allow, `!x` vs `x == 0`, increment forms,
     compound assignment, index vs pointer, `for` vs `while`, swapped if/else,
     and safe declaration and statement moves. `&&`/`||` are never swapped and
     comparisons are never negated. Up to `--sideways` equal-score steps keep
     two-step fixes reachable.

   Both edit the owner in place under the build lock and always restore it. The
   best result becomes the task's candidate.

Candidates and attempt files (`attempt-NNN.prompt.md`, `.response.md`, `.c`,
`.report.json`) live in `build/local_llm_campaign/candidates/<task>/`. The
standalone `rewrite` command writes an improvement to
`build/local_llm_campaign/rewrite/<symbol>.c`.

### Register-colouring replay

`explain SYMBOL` (and, cached per source version, prompts whose remaining diff is
mostly registers) uses `local_campaign_colouring.py`. It runs MWCC under
[mwcc-debugger](https://github.com/cadmic/mwcc-debugger) and the retrowin32 gdb
stub, then pairs the allocator's record with the objdiff against retail. That
names the value behind each differing register, the line that writes it and its
colouring step. The debugger supports only some compiler versions, so the tool
first checks that the replay compiler emits the same code as the unit's real
compiler and says whether the replay is faithful. Install `mwdbg.py`, its
`gdb.py` stand-in and a `retrowin32` binary in `<project>/build/tools/mwdbg`
(or `tools.mwdbg` / `DECOMP_MWDBG`). None of them belong in git.

## Context sources

| Source | Where it comes from | Notes |
|---|---|---|
| Brief | `tools.brief` (`DECOMP_BRIEF`), default `<project>/tools/decomp_work/handoff/gen_brief.py` | optional project objdiff brief; otherwise a minimal skeleton filled from the scratch measurement |
| Corpus index | `build/local_llm_campaign/corpus_index.json` (`local_campaign_corpus.py`) | matched code with the same callees and data, and exact callee prototypes |
| Similarity templates | `tools.simindex` (`DECOMP_SIMINDEX`), corpus `tools.simindex_corpus` (`DECOMP_SIMINDEX_CORPUS`) | optional project assembly-similarity engine, run in an isolated environment (`DECOMP_ANALYSIS_PYTHON`, default `build/tools/analysis-venv` in this repository; on Windows `build\tools\analysis-venv\Scripts\python.exe`) |
| Library references | `src/` of each configured reference project (`references` in the config) | same-named functions from decomps that share a library; reference only, filtered by the same policy |
| Target dossier | `local_campaign_investigation.py` | callees, data, callers, widths and branch sites from dtk assembly; the model may answer with bounded read-only `context_requests` instead of C |
| Evidence store | `DECOMP_KNOWLEDGE_DB` | opt-in; only used when the variable is set (below) |

`DECOMP_INVESTIGATION=0` turns off the dossier and request protocol.
`DECOMP_CONTEXT_REQUESTS=0` keeps the dossier but turns off the extra exchanges.
`tools.verify` (`DECOMP_VERIFY`) is resolved, but the current runner does not
call it.

## Promotion

`run --promote` writes a task's best candidate into its owner and commits it
(`git commit` of that one file) only when all of these hold:

- the candidate beats the queued score;
- the owner is unchanged since the candidate was measured;
- a fresh report rebuild reproduces the candidate's score;
- no function anywhere in the report loses score.

Anything else reverts the file. This gate uses objdiff scores, not the SHA-1.
Linking a unit that becomes complete stays manual (a configure change and a full
SHA-1 build). Policy-cleanup tasks are never auto-promoted. Without `--promote`,
results are review candidates only.

## Model benchmark

`harness/local_campaign_bench.py` compares models on frozen task sets with the
campaign's own attempt loop, so its results describe this harness rather than a
general benchmark. It reads the campaign queue (run `sync` first) and keeps its
state in `build/local_llm_campaign/bench/`, never in the campaign state.

```sh
python harness/local_campaign_bench.py select [--name main] [--max-bytes 2048] [--force]   # freeze the main set
python harness/local_campaign_bench.py select-prelim [--count 5]                           # spread-by-size subset
python harness/local_campaign_bench.py run NAME --host URL --model MODEL [--set prelim] [--server-cmd CMD]
python harness/local_campaign_bench.py report [--set prelim] [--json]
python harness/local_campaign_bench.py compare RUN_A RUN_B          # paired per-function wins/losses and a sign test
python harness/local_campaign_bench.py queue bench/queue.local.json --stage prelim
python harness/local_campaign_bench.py queue bench/queue.local.json --stage main --only-passing
```

- `select` takes tasks across match bands, one per owner. A set named other than
  `main` avoids the main set's owners, so it tests generalisation.
- Every run starts each task from its assigned source with no history and gives
  it the full budget (`--retries 7`, i.e. 8 attempts). `--early-stop` restores the
  campaign's stall stops. `--seed-salt` makes a repeated run a fresh sample. A
  lead only counts when replicate runs agree.
- The **prelim** round is a speed and health gate (attempts per hour against the
  fastest complete run, unusable answers, answers that do not compile). It
  cannot rank quality. Quality is decided per function on the main set.
- One run at a time: a lock serialises runs (`--wait` queues behind another), and
  a run refuses to start while a campaign worker holds a runner lock, because
  they would share the model server (`--allow-busy` overrides this). Verification still takes the build lock and
  owner claims.

**Queue file.** Copy `bench/queue.example.json` to `bench/queue.local.json`
(git-ignored):

| Key | Meaning |
|---|---|
| `host` | model endpoint for every entry |
| `server_cmd` | optional shell command that (re)starts the server; `{model}` and `{extra}` are substituted. Leave it empty when the server is already running |
| `pre_cmd` | optional command run before each entry, e.g. to open a tunnel |
| `server_wait` | seconds to wait until the model is served |
| `force` | discard and rerun entries that already have a run directory |
| `configs[]` | `name`, `model`, optional `server_extra` (fills `{extra}`), `args` (extra `run` options such as `--think`, `--temperature`, `--sampling`), optional `skip_stages` |

## Evidence store

`local_campaign_knowledge.py` is an immutable SQLite store of frozen-workspace
snapshots, function observations, source and draft variants, observed compiler
profiles and diagnostics. Text artifacts are compressed and SHA-256-addressed.
It lives at `DECOMP_KNOWLEDGE_DB`, else
`<project>/build/decomp-support/knowledge/evidence.sqlite3`. Every CLI accepts
`--database PATH` before its subcommand.

Evidence boundaries: `scorer_exact` is an observation, not a certificate. Raw
bytes, relocations, owner object, linked checksum, policy and human review stay
unknown in the store. Only original project bodies that are 100% in the report,
have an observed compile command and pass policy screening become references.
Similarity is a retrieval reason, never proof of types or semantics.

```sh
python harness/local_campaign_decompile_snapshot.py                     # freeze a workspace (default <support>/workspaces/)
python harness/local_campaign_knowledge_import.py snapshot WORKSPACE      # needs the project's simindex_lib (tools.simindex)
python harness/local_campaign_knowledge_import.py experiment SNAPSHOT_ID BATCH_DIR VALIDATION_DIR
python harness/local_campaign_knowledge_bench.py index SNAPSHOT_ID       # rebuild derived retrieval terms
python harness/local_campaign_knowledge_bench.py stats
python harness/local_campaign_knowledge_bench.py query SNAPSHOT_ID func_80001234
python harness/local_campaign_knowledge_bench.py audit SNAPSHOT_ID --output AUDIT.json [--split SPLIT.json]
```

Import requires a workspace whose fingerprint is unchanged before and after.
`audit` freezes a holdout split that excludes whole owner and clone groups. It
measures retrieval coverage only. `local_campaign_knowledge_ablation.py --split
SPLIT --output DIR --host URL --model MODEL` then runs a paired retrieval-off/on
model and compiler pilot on the frozen workspace. To give normal campaign
prompts up to three matching references from a snapshot of the same report, set
`DECOMP_KNOWLEDGE_DB`.

## Recovery, reference drafts and source delivery

These tools run against a frozen workspace. Point `DECOMP_ROOT` at it
(`DECOMP_ROOT=WORKSPACE python ...`, or `$env:DECOMP_ROOT = "WORKSPACE"` in
PowerShell). Each needs a new output directory, rechecks its frozen inputs, and
leaves `ready` and `promoted` false.

| Step | Command | What it does |
|---|---|---|
| Baselines | `local_campaign_knowledge_sweep.py --unmatched-snapshot ID --output DIR [--native-c] [--baseline-cache DIR]` | measures untouched owners, then declaration-only m2c drafts for every unmatched function (`--split` instead takes a frozen holdout) |
| Reference drafts | `local_campaign_reference_draft.py --baseline DIR --output DIR [--full-references]` | adapts whole matched functions with compatible contracts as deterministic seeds; similarity is never a certificate |
| Recovery | `local_campaign_recovery.py --prior DIR --output DIR --host URL --model MODEL [--attempts 2] [--symbol S] [--declaration-orderings N] [--revision-protocol function\|edits-v1\|schema-edits-v1]` | recompiles prior drafts, then bounded MWCC/objdiff feedback retries; `--attempts 0` runs only the deterministic candidates. The assigned C is never shown to the model |
| Link gate | `local_campaign_link_gate.py --root PROJECT --recovery DIR --output DIR [--context ...]` | SHA-1 link capsule; see [M2C_PIPELINE.md](M2C_PIPELINE.md#5-owner-unit-gates-m2c_unit_autogatepy-and-m2c_unit_py) |
| Source package | `local_campaign_source_package.py --root WORKSPACE --policy-root PROJECT --run DIR... --link-capsule DIR --output DIR` | exports the assigned definition as an owner-only patch, applies it privately, recompiles, re-audits bytes, relocations and siblings, runs Clang C89 diagnostics and the project's policy hooks, and relinks |
| Delivery | `local_campaign_source_delivery.py --source-packages DIR --output DIR` | combines the passing packages into one `delivery/owners.patch`, recompiles every affected unit and links all changed owners together |

Project policy hooks for packaging come from a `source_policy` object on the
project entry (or at top level): `quality_scan`, `wrapper_scan` and `attest`
paths relative to the policy root. See the `local_campaign_source_package.py`
docstring for the expected functions. Every hook is optional.

## Environment variables

| Variable | Effect |
|---|---|
| `OLLAMA_HOST`, `OLLAMA_MODEL` | default `--ollama-host` / `--model` |
| `OLLAMA_NUM_PREDICT`, `OLLAMA_NUM_CTX` | default `--num-predict` / `--num-ctx` |
| `LOCAL_CAMPAIGN_WORKER` | default `--worker` for `run` |
| `DECOMP_HEAVY_LOCK` | optional machine-wide lock file for heavy rebuilds |
| `DECOMP_INVESTIGATION`, `DECOMP_CONTEXT_REQUESTS` | `0` disables the dossier / the context-request exchanges |
| `DECOMP_KNOWLEDGE_DB` | evidence store; also turns on store context in prompts |
| `DECOMP_BRIEF`, `DECOMP_VERIFY`, `DECOMP_SIMINDEX`, `DECOMP_MWDBG` | project tool overrides (`tools.*`) |
| `DECOMP_SIMINDEX_CORPUS`, `DECOMP_ANALYSIS_PYTHON` | similarity corpus name and its interpreter |
| `DECOMP_MODEL_HOST` | endpoint for `m2c_draft_attempts.py` (not the campaign) |

## State layout

`<project>/build/local_llm_campaign/`:

| Path | Contents |
|---|---|
| `state.json` | queue, per-task history, events, settings, chart snapshots |
| `candidates/<task>/` | prompts, replies, candidate `.c` files and per-attempt reports |
| `coordination.json` (+ `*.lock`) | claims, waiting builds; `build.lock`, `fleet.lock`, `state-writer.lock` |
| `runners/<worker>.lock` | single-instance lock per worker name |
| `priority.json` | optional `{"symbols": [...]}` taken first |
| `breather_queue.json` | jobs for the next breather |
| `halt.json` | present only after a failed restore; stops the fleet |
| `corpus_index.json` | corpus index cache |
| `rewrite/` | improvements from the `rewrite` command |
| `bench/` | benchmark sets, runs and status |

The dashboard reads this directory together with the m2c report and shows
workers, recent replies, claims, waiting builds and the benchmark.

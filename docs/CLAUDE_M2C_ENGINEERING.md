# Claude m2c engineering lane

`harness/claude_m2c_engineer.py` uses the Claude Agent SDK as a codebase
engineer for m2c. It does not ask Claude to solve individual decompilation
functions and does not modify a decomp project.

The runner selects a small failure cluster from an immutable m2c snapshot,
regenerates and scores that cluster with the current accepted m2c base, skips
currently instruction-exact rows, then creates a detached worktree. The prompt
uses the fresh draft, score, diagnostics, and categories rather than historical
failure evidence. Claude may inspect
and edit only m2c implementation and test paths. It has no shell access; the
harness alone runs tests, compiler tools, and A/B scoring. It is asked for a general
generator correction plus focused regression tests. The dossier maps uncommon
target mnemonics and diagnostics to likely implementation and test files so paid
turns are not consumed rediscovering basic code ownership.

Admission is deterministic:

1. Reject edits outside `m2c/`, `m2c_pycparser/`, and `tests/`.
2. Run m2c's unit tests.
3. Generate both baseline and candidate drafts from the same frozen inputs.
4. Compile each draft with the project's real compiler profile and score it.
5. Reject any compile, score, or instruction-exact loss, including sampled
   exact controls.
6. Require at least one compile, score, or instruction-exact gain.
7. If the first patch fails, provide the measured result to one bounded review
   call and evaluate the revision again.

An admitted patch is still only `accepted_for_review`. The canonical m2c tree
is not changed automatically. Prompts, responses, tests, A/B artifacts, costs,
and the binary patch are retained under the ignored output directory. The
disposable worktree is removed unless `--keep-worktree` is supplied.

## Commands

Create a virtual environment for the Agent SDK (the commands are shown for macOS/Linux; on Windows
use `build\agent-sdk-venv\Scripts\python.exe` and set `$env:PYTHONPATH = "harness"` in PowerShell):

```bash
python -m venv build/agent-sdk-venv
build/agent-sdk-venv/bin/python -m pip install -r harness/requirements-agent-sdk.txt

PYTHONPATH=harness build/agent-sdk-venv/bin/python harness/claude_m2c_engineer.py --check
```

Plan, then run, one cluster from a frozen m2c baseline snapshot (`BASELINE.json` is a snapshot
produced by the m2c improvement experiments for your project):

```bash
PYTHONPATH=harness build/agent-sdk-venv/bin/python harness/claude_m2c_engineer.py   build/decomp-support/m2c-improvements/BASELINE.json   --category missing_declaration --limit 4 --exact-controls 12 --plan

PYTHONPATH=harness build/agent-sdk-venv/bin/python harness/claude_m2c_engineer.py   build/decomp-support/m2c-improvements/BASELINE.json   --category missing_declaration --limit 4 --exact-controls 12   --budget-usd 5 --review-budget-usd 3
```

Run several non-overlapping clusters unattended. Budgets are per iteration;
`campaign.json` records aggregate cost and status as each iteration finishes:

```bash
PYTHONPATH=harness build/agent-sdk-venv/bin/python harness/claude_m2c_engineer.py   build/decomp-support/m2c-improvements/BASELINE.json   --category syntax --limit 4 --iterations 10 --exact-controls 12   --budget-usd 3 --review-budget-usd 1   --output build/decomp-support/claude-m2c-engineer/syntax-10
```

Repeat `--category` to form a combined corpus. Use `--exclude-category` to keep
parallel campaigns disjoint; rows carrying an excluded category are omitted
even when they also carry an included category.

Keep the Anthropic key in the process environment or the ignored owner-private
`.env` described by `--check`. Never put credentials in a command line, log,
prompt, or tracked file.

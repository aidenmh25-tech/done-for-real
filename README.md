# done-for-real

A Claude Code skill (and hook) that catches **silent tool failures** — the cases
where a command exits `0` while it actually failed — by verifying the command's
real output instead of trusting its exit code.

## The problem

Exit code `0` is a *claim*, not evidence. Plenty of tools return `0` while
having failed: pytest under `-n`/xdist, linters in report mode, a background job
that crashes a millisecond after launch, a pipe that masks the real exit status,
a config file "written" but still holding an unfilled `<FROM_KEYCHAIN>` secret.
When that happens, an agent reads exit `0`, reports "tests pass ✅", and moves
on — shipping the very failure it was supposed to catch.

## Before / after

A test suite with three real failures, piped so the shell exit status is `0`:

```
$ pytest -n 4 | tail
FAILED tests/test_orders.py::test_fill
3 failed, 3 passed in 6.9s
$ echo $?
0
```

**Without done-for-real:** exit is `0`, so the run looks green and gets reported
as passing.

**With done-for-real** the engine reads the summary line rather than the exit
code and blocks:

```
VERIFY FAIL [pytest]: summary says "3 failed, 0 error(s)".
Evidence: 3 failed, 3 passed in 6.9s
Do NOT report success. Diagnose the ROOT CAUSE, fix it, then re-run the exact
same command. This is retry 1/3.
```

It stays completely silent when a command genuinely succeeded, so the happy path
costs nothing.

## How it works

- **`skills/done-for-real/SKILL.md`** — the model-facing skill: the core rule,
  the tiered checks, the bounded fix-loop, and the known limits.
- **`skills/done-for-real/scripts/verify.py`** — the verification engine. Runs
  off-context; only its one-line verdict is ever seen. Silent on success
  (`exit 0`), blocks on failure (`exit 2`).
- **`skills/done-for-real/references/lying-tools.md`** — catalog of tools that
  lie (or false-alarm), parsed by the engine.
- **`hooks/hooks.json`** — a `PostToolUse` (Bash) + `Stop` hook for automatic
  coverage on every command.

In Claude Code, **only exit code `2` blocks and feeds `stderr` back to the model**
— so the engine uses `exit 2` on failure. `PostToolUse` fires only after a
command that exited `0` and carries no exit code, which is exactly the niche: a
command that *failed* exits nonzero and Claude already sees `Exit code N`; this
skill exists to catch the ones that lie about exiting `0`.

## Install

Requires **Python 3** (standard library only — no `pip install`).

### As a plugin (recommended — wires the skill *and* the hook)

Clone into your personal skills directory. Because the repo carries a
`.claude-plugin/plugin.json`, Claude Code loads it in place as the plugin
`done-for-real@skills-dir` on the next session — no install step, hook included:

```
git clone https://github.com/aidenmh25-tech/done-for-real ~/.claude/skills/done-for-real
```

Restart Claude Code. The skill is available as `/done-for-real:done-for-real`
and the hook runs automatically.

> **Windows / non-standard Python:** the hook command uses `python3`. If only
> `python` is on your PATH, either alias it or use the `install-hook.py` route
> below (it auto-detects your interpreter).

### Skill only (no hook), or hook via the installer

Put the skill anywhere Claude Code scans and, if you want the hook, run the
installer (it detects your interpreter, writes an absolute path, and runs a
self-proving smoke test):

```
python3 ~/.claude/skills/done-for-real/skills/done-for-real/scripts/install-hook.py            # global
python3 ~/.claude/skills/done-for-real/skills/done-for-real/scripts/install-hook.py --project  # this project only
```

## Test results

Observed by running the suites in this repo (not summarized from code):

```
python3 tests/run_tests.py       # -> ALL 36 SCENARIOS PASSED
python3 tests/corpus_eval.py     # -> 0/53 false positives, 11/11 lies caught
```

The nine core acceptance scenarios (`tests/run_tests.py`, cases 1–9), with the
exact results witnessed on this machine:

| # | Scenario | Expected | Observed |
|---|----------|----------|----------|
| 1 | `pytest -n 4` with 3 failing tests, exit 0 | catch | ✅ blocked (exit 2) |
| 2 | `grep` no match, exit 1 | pass (suppress false alarm) | ✅ passed (exit 0) |
| 3 | `tsc` prints 2 type errors, exit 0 | catch | ✅ blocked (exit 2) |
| 4 | `eslint` report mode, 5 errors, exit 0 | catch | ✅ blocked (exit 2) |
| 5 | server started with `&` that crashes on launch | catch | ✅ blocked (exit 2) |
| 6 | file written containing `<FROM_KEYCHAIN>` placeholder | catch | ✅ blocked (exit 2) |
| 7 | a genuinely passing test suite, exit 0 | pass, silent | ✅ passed (exit 0) |
| 8 | read-only `ls` / `cat` / search | skipped by gate | ✅ passed (exit 0) |
| 9 | same failing command run 3× | loop stops, escalates | ✅ escalates ("retry budget EXHAUSTED", exit 2) |

**9/9 core scenarios passed.** The full `run_tests.py` suite (36 scenarios,
including regression cases for compound commands, redirect look-alikes, and
`\|\| true` opt-outs) reported `ALL 36 SCENARIOS PASSED`, and the corpus eval
measured **0% false positives across 53 benign commands** (git, npm, docker,
build tools, GSD/node hooks) with **100% of 11 lying-exit-0 commands caught**.

## What this doesn't catch

Be clear-eyed about the limits:

- **Non-catalog tools that lie at exit 0.** Only tools listed in
  `references/lying-tools.md` get tool-specific detection. A custom runner that
  prints failures but exits `0` will pass unless you add a catalog entry. This is
  a deliberate trade for a ~0% false-positive rate.
- **Failures behind a nonzero exit** aren't handled by the hook — by design.
  `PostToolUse` only fires on success, and a nonzero exit is already visible to
  Claude as `Exit code N`, so there is nothing to rescue.
- **Explicit opt-outs are respected.** A command masked with `|| true`, `|| :`,
  or `; true` is never blocked — that is an intentional "non-fatal" step.
- **A tool invoked inside a quoted `-c` string** (e.g. `bash -c "pytest ..."`) is
  not identified, because quoted arguments are stripped to avoid false positives
  from tool names that appear in messages.
- **It reads output; it does not re-run your tests** or inspect program state
  beyond the file/process/endpoint real-effect checks.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Aiden.

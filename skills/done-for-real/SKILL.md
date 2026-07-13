---
name: done-for-real
description: Verifies that a command actually succeeded instead of trusting its exit code, catching silent failures where a tool exits 0 while it really failed (pytest under xdist, linters in report mode, a background job that crashes on launch, a file written with an unfilled placeholder). Invoke after running tests, builds, linters, type-checks, migrations, or deployments, and whenever a command's success output looks empty, truncated, or otherwise unconvincing, before telling the user a task is done or fixed.
---

# done-for-real

Prove work happened before you say it's done. An exit code is a claim, not
evidence.

## The core rule

**Never report success from an exit code alone.** Confirm the real effect — the
tests actually passed, the file actually holds real content, the process is
actually running, the migration actually applied. Exit `0` from a tool that lies
means nothing.

## What it checks

Run the engine over the command you just executed; it returns a one-line verdict
and never enters your context beyond that verdict:

```
python3 scripts/verify.py "<the exact command>" --exit <code> \
        --stdout "<captured stdout>" --stderr "<captured stderr>"
```

- Exit `0` from the script → verified (or nothing to verify). Proceed.
- Exit `2` → **FAIL**; its stderr is your verdict. Do not report success; enter
  the fix-loop below.

It applies three tiers, cheapest first:

- **Tier 0 — output signatures.** Scans output for failure markers (`N failed`,
  `Traceback`, `npm ERR!`, `panic:`) — but only when there is reason to distrust
  the result, so benign commands that merely *print* those words are not flagged.
- **Tier 1 — tool-specific truth.** For known tools it parses the real success
  condition: the pytest summary line (trusted over the exit code under `-n` /
  xdist), any `error TS` line from `tsc`, `npm ERR!` / `npm error`, lint error
  counts, `go test` / `cargo` failures. The catalog lives in
  `references/lying-tools.md` — read it only to add a tool or when the script
  reports an unknown one.
- **Tier 2 — real-effect checks** for high-stakes actions: a written file must
  exist and contain no placeholder token (`<FROM_KEYCHAIN…>`, `PLACEHOLDER`); a
  backgrounded process must actually be alive; a migration/deploy must have taken
  effect. When it can't run the check itself, it emits the exact check for you to
  run.

## How to read the output

Silence (exit 0) means verified — say so plainly. A `VERIFY FAIL` block (exit 2)
names the tool, the evidence (which test, which line, which real error), and the
retry count. Treat that evidence as ground truth over any success message the
tool printed.

## The fix-loop (on a FAIL)

1. **Do not report success.** Suppress all "done / fixed / passing" language.
2. **Capture the specific evidence** from the verdict.
3. **Diagnose the root cause before touching code** — one hypothesis aimed at the
   cause, not the symptom.
4. **Fix, then re-run the exact same command** and re-verify.
5. **Bounded retries: max 3 per command.** After the 3rd, stop looping and
   surface to the human with the accumulated evidence and everything you tried.
6. The counter **resets on PASS**.

## Known limits

- **Exit-0 only, in hook mode.** As a `PostToolUse` hook it only sees commands
  that exited 0 (Claude Code fires `PostToolUse` after success and carries no
  exit code). That is the intended niche: a genuinely failed command exits
  nonzero and Claude already sees `Exit code N`. The hook exists to catch the
  *lying* exit-0 case.
- **Catalog-bound.** Only tools listed in `references/lying-tools.md` get
  tool-specific (Tier 1) detection. A custom runner that lies at exit 0 needs a
  catalog entry.
- **Explicit opt-out respected.** A command the author masked with `|| true`,
  `|| :`, or `; true` is never blocked — that is an intentional "non-fatal" step.
- **Text-only.** It reads output; it does not re-execute your tests or inspect
  program state beyond the real-effect checks above.

## Automatic coverage

To run on every Bash action without having to remember, install the hook:

```
python3 scripts/install-hook.py            # user-global (~/.claude/settings.json)
python3 scripts/install-hook.py --project  # this project only
```

It merges without clobbering existing hooks and runs a self-proving smoke test.
Blocks with **exit 2** on failure (the only exit code Claude Code feeds back);
silent otherwise.

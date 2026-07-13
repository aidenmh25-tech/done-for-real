# Lying-tools catalog

Tools whose exit codes lie (return 0 on failure) or false-alarm (return nonzero
on success). `verify.py` parses this file at load time to build its detection
table. Claude reads this file only when **extending** the catalog or when the
script reports an **unknown tool**.

## Entry format

Each entry is a `##` heading followed by fields:

- `detect:` a Python regex matched against the command string.
- `tier:` `0` | `1` | `2` (highest check tier this tool participates in).
- `rule:` human-readable success condition (Claude reads this when extending).
- `false_alarm:` (optional) nonzero-exit condition that is actually NORMAL.
- `high_stakes:` `true` | `false` (enables Tier 2 semantic checks).

Only `detect`, `tier`, and `high_stakes` are consumed by the script's logic;
`rule`/`false_alarm` are documentation for humans and for Claude.

---

## pytest
detect: \bpytest\b
tier: 1
rule: Parse the final summary line. PASS only if it contains "0 failed" and no
      "error(s)". If invoked with -n / xdist, the exit code is unreliable —
      always trust the summary line, never the exit code.
false_alarm: none
high_stakes: false

## grep
detect: \bgrep\b
tier: 0
rule: Exit 1 means "no matches found" — this is NORMAL, not a failure. Never
      flag grep exit 1 as a failure on its own.
false_alarm: exit 1 = no matches
high_stakes: false

## diff
detect: \bdiff\b
tier: 0
rule: Exit 1 means "files differ" — expected, not a failure.
false_alarm: exit 1 = files differ
high_stakes: false

## npm
detect: \bnpm\b
tier: 1
rule: npm can swallow child exit codes. Presence of "npm ERR!" (classic) or
      "npm error" (npm 9+) in output is a FAIL regardless of exit code. Note
      that a failing `npm test` script may print NEITHER and only exit nonzero —
      that case is caught by the recognized-catalog-tool nonzero rule.
false_alarm: none
high_stakes: false

## tsc
detect: \btsc\b|\btypescript\b
tier: 1
rule: Any output line matching "error TS" is a type error. Exit code varies —
      trust the output, not the code.
false_alarm: none
high_stakes: false

## eslint
detect: \beslint\b
tier: 1
rule: In report mode eslint prints errors and may exit 0. Parse the summary
      ("N problems", "N errors"); nonzero error count = FAIL.
false_alarm: none
high_stakes: false

## ruff
detect: \bruff\b
tier: 1
rule: Report mode prints findings and may exit 0. "Found N error" with N>0 = FAIL.
false_alarm: none
high_stakes: false

## flake8
detect: \bflake8\b
tier: 1
rule: Any output line matching a lint code (e.g. E501, F401) is a finding. Exit
      code unreliable in some CI wrappers — trust output.
false_alarm: none
high_stakes: false

## background process
detect: \s&\s*$|\bnohup\b|launchctl load|systemctl start|systemctl restart
tier: 2
rule: A successful LAUNCH is not a successful RUN. After start, confirm the
      process is actually alive (pgrep / ps) and, if it serves a port, that the
      port responds. PID=0 or immediate exit = FAIL even if launch returned 0.
false_alarm: none
high_stakes: true

## masked exit code
detect: \|\|\s*true\b|;\s*echo\b|;\s*true\b
tier: 0
rule: The exit code is masked by "|| true", "; echo done", etc. Distrust the
      exit code entirely; rely on Tier 0 output signatures only.
false_alarm: none
high_stakes: false

## jest/vitest
detect: \b(jest|vitest)\b
tier: 0
rule: Treated as an ordinary test runner: a nonzero exit is a failure, exit 0 is
      trusted. (The old bare `-u`/`--ci` detect matched unrelated flags like
      `curl -u` / `sort -u` and is intentionally removed.)
false_alarm: none
high_stakes: false

## migration
detect: \balembic\b|\bmigrate\b|\bmigration\b|db:migrate|prisma migrate
tier: 2
rule: A migration command returning 0 does not prove the schema advanced.
      Assert the schema/revision version actually moved forward.
false_alarm: none
high_stakes: true

## deploy
detect: \bdeploy\b|kubectl apply|terraform apply
tier: 2
rule: Deploy returning 0 does not prove the service is live. Assert the endpoint
      responds / rollout completed.
false_alarm: none
high_stakes: true

## go test
detect: \bgo test\b
tier: 1
rule: Look for "FAIL" lines and "--- FAIL:". "ok" per package is required.
false_alarm: none
high_stakes: false

## cargo
detect: \bcargo (test|build)\b
tier: 1
rule: "error[" or "test result: FAILED" indicates failure. Warnings are not
      failures on their own.
false_alarm: none
high_stakes: false

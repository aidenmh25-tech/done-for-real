#!/usr/bin/env python3
"""Acceptance harness for done-for-real (§7 of the build plan).

Runs verify.py in direct mode against 9 scenarios and checks the exit code.
  exit 2 == FAIL (blocking)   exit 0 == PASS/gated/unsure (silent)
"""
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "..", "skills", "done-for-real", "scripts", "verify.py")


def run(command, exit_code=0, stdout="", stderr=""):
    p = subprocess.run(
        [sys.executable, VERIFY, command, "--exit", str(exit_code),
         "--stdout", stdout, "--stderr", stderr],
        capture_output=True, text=True,
    )
    return p.returncode, (p.stderr or "").strip()


CASES = [
    # (label, kwargs, expected_exit)
    ("1 pytest -n xdist, 3 failing, exit 0",
     dict(command="pytest -n 4 tests/",
          exit_code=0,
          stdout="tests/test_orders.py::test_fill FAILED\n"
                 "===== 3 failed, 120 passed in 4.2s ====="),
     2),
    ("2 grep no match, exit 1",
     dict(command="grep foo missing.txt", exit_code=1, stdout=""),
     0),
    ("3 tsc 2 type errors, exit 0",
     dict(command="tsc --noEmit", exit_code=0,
          stdout="src/a.ts(3,5): error TS2322: Type 'x'.\n"
                 "src/b.ts(9,1): error TS2554: bad."),
     2),
    ("4 eslint report mode, 5 errors, exit 0",
     dict(command="eslint . --format json", exit_code=0,
          stdout="✖ 5 problems (5 errors, 0 warnings)"),
     2),
    ("5 server via & that crashes",
     dict(command="node server.js &", exit_code=1,
          stdout="", stderr="Error: listen EADDRINUSE"),
     2),
    ("6 file write with placeholder", None, 2),  # special-cased below
    ("7 genuinely passing suite, exit 0",
     dict(command="pytest tests/", exit_code=0,
          stdout="===== 214 passed in 8.1s ====="),
     0),
    ("8 read-only ls gated",
     dict(command="ls -la /etc", exit_code=0, stdout="drwxr-xr-x ..."),
     0),
    ("9 loop stops at retry 3", None, None),  # special-cased below

    # --- interference / seamlessness regression cases (must NOT block) ---
    ("10 shell `test -f` control flow, exit 1",
     dict(command="test -f /nonexistent", exit_code=1), 0),
    ("11 `[ -d x ]` conditional, exit 1",
     dict(command="[ -d /nope ] && echo hi", exit_code=1), 0),
    ("12 pgrep miss, exit 1",
     dict(command="pgrep myproc", exit_code=1), 0),
    ("13 curl health probe refused, exit 7",
     dict(command="curl -sf http://localhost:1/health", exit_code=7), 0),
    ("14 log line mentions FAILED, action tool, exit 0",
     dict(command="make deploy-check", exit_code=0,
          stdout="retrying step (previous attempt FAILED), now OK\nDone."), 0),
    ("15 passing suite prints '0 failed', exit 0",
     dict(command="pytest tests/", exit_code=0,
          stdout="===== 0 failed, 300 passed in 9s ====="), 0),
    ("16 clean background launch, exit 0 (caution, non-blocking)",
     dict(command="node watcher.js &", exit_code=0, stdout=""), 0),
    ("17 npm install success with WARN noise, exit 0",
     dict(command="npm install", exit_code=0,
          stdout="npm WARN deprecated foo@1.0.0\nadded 200 packages"), 0),
    ("18 real pip failure: ERROR + nonzero, exit 1",
     dict(command="pip install badpkg", exit_code=1,
          stdout="ERROR: Could not find a version that satisfies badpkg"), 2),
    # Regression: an unknown command that exits 0 but PRINTS a failure word in
    # its own success output must NOT block. (This is the installer-smoke-output
    # false positive that fired the live hook on its own install command.)
    ("19 installer prints '3 failed' in success output, exit 0",
     dict(command="python install-hook.py --project", exit_code=0,
          stdout="[ok] lying pytest (exit0 + '3 failed') -> BLOCKED\nInstalled."), 0),
    ("20 echo a report line with Traceback, non-tool, exit 0",
     dict(command="echo summary", exit_code=0,
          stdout="last night's job hit a Traceback (most recent call last)"), 0),
    # Regression: `>` that is NOT a redirect must not be read as a write target.
    ("21 arrow '->' inside quotes is not a redirect, exit 0",
     dict(command="python -c \"print('  -> returned rc=0 (not blocked)')\"",
          exit_code=0, stdout="  -> returned rc=0 (not blocked)"), 0),
    ("22 fd dup '2>&1' is not a file write, exit 0",
     dict(command="make build target 2>&1", exit_code=0, stdout="ok"), 0),
    ("23 comparison '=>' / '>=' is not a redirect, exit 0",
     dict(command="awk '$1 => 5' data && echo n>=1", exit_code=0, stdout="ok"), 0),
    ("24 redirect to /dev/null must not block, exit 0",
     dict(command="make build > /dev/null 2>&1", exit_code=0, stdout=""), 0),
    ("25 diff of logs containing 'failed', exit 1 (differ)",
     dict(command="diff old.log new.log", exit_code=1,
          stdout="< 3 failed yesterday\n> 0 failed today"), 0),
    ("26 truncate/create empty lock file, exit 0",
     dict(command=": > app.lock", exit_code=0, stdout=""), 0),
    # Regression: a COMPOUND command with '; echo' that prints failure words at
    # exit 0 must not block (this is what fired the live hook).
    ("27 compound '; echo' printing '3 failed' + Traceback, exit 0",
     dict(command="python -c 'print(x)'; echo done",
          exit_code=0,
          stdout="build step: 3 failed, hit a Traceback (most recent call last)"), 0),
    ("28 curl -u (not a snapshot flag) failing, exit 22",
     dict(command="curl -u user:pass https://api.example.com", exit_code=22,
          stdout=""), 0),
    ("29 sort -u success, exit 0",
     dict(command="sort -u data.txt", exit_code=0, stdout="a\nb\nc"), 0),
    # Regression: compound cmd that INVOKES pytest (output elsewhere) AND prints
    # a stray "3 failed" from another sub-command must NOT be misattributed.
    ("30 compound w/ pytest + stray '3 failed' text, exit 0",
     dict(command="pytest -q > /dev/null 2>&1; echo done",
          exit_code=0,
          stdout="deploy step: 3 failed earlier, now recovered\n< 3 failed\ndone"), 0),
    # Flagship preserved: a REAL pytest summary (even with exit masked to 0 by a
    # pipe) is still caught because the summary line structure matches.
    ("31 real pytest summary, exit masked to 0 (piped)",
     dict(command="pytest -n 4 | tail", exit_code=0,
          stdout="FAILED orders/tests/test_pricing.py::test_x\n"
                 "3 failed, 3 passed in 6.97s"), 2),
    ("32 real pytest all-pass summary, exit 0",
     dict(command="pytest -n 4 -q", exit_code=0,
          stdout="......   [100%]\n6 passed in 5.87s"), 0),
    # Regression: explicit `|| true` opt-out must not block even with findings.
    ("33 flake8 with '|| true' opt-out, exit 0",
     dict(command="flake8 src || true", exit_code=0,
          stdout="src/x.py:1:1: E501 line too long"), 0),
    ("34 pytest failing but '|| true' opt-out, exit 0",
     dict(command="pytest -q || true", exit_code=0,
          stdout="2 failed, 8 passed in 1.2s"), 0),
    ("35 classic 'npm ERR!' still caught, exit 0",
     dict(command="npm run build", exit_code=0,
          stdout="npm ERR! code ELIFECYCLE\nnpm ERR! Failed at build"), 2),
    ("36 tool name only in quoted arg not misidentified, exit 0",
     dict(command="python -c \"print('npm ERR! in docs, 3 failed in 2s')\"",
          exit_code=0, stdout="npm ERR! in docs, 3 failed in 2s"), 0),
]


def main():
    failures = []

    for label, kwargs, expected in CASES:
        if label.startswith("6"):
            # write a file containing a placeholder token, then verify a write claim
            import tempfile
            fd = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
            fd.write("API_KEY=<FROM_KEYCHAIN:token>\n")
            fd.close()
            cmd = f"echo secret > {fd.name}"
            rc, msg = run(cmd, exit_code=0, stdout="")
            ok = rc == 2
            os.unlink(fd.name)
            _report(label, ok, rc, msg, failures)
            continue

        if label.startswith("9"):
            # same failing command 4 times; 4th must announce exhausted budget
            cmd = "pytest -n 4 retry_case/"
            out = "===== 1 failed, 1 passed in 0.5s ====="
            last_msg = ""
            for _ in range(4):
                rc, last_msg = run(cmd, exit_code=0, stdout=out)
            ok = rc == 2 and "EXHAUSTED" in last_msg
            _report(label, ok, rc, last_msg, failures)
            continue

        rc, msg = run(**kwargs)
        ok = rc == expected
        _report(label, ok, rc, msg, failures)

    print()
    if failures:
        print(f"FAILED {len(failures)}/{len(CASES)}: {failures}")
        sys.exit(1)
    print(f"ALL {len(CASES)} SCENARIOS PASSED")


def _report(label, ok, rc, msg, failures):
    status = "ok  " if ok else "FAIL"
    print(f"[{status}] {label}  (rc={rc})")
    if msg and not ok:
        print("        verdict:", msg.replace("\n", " | "))
    if not ok:
        failures.append(label.split()[0])


if __name__ == "__main__":
    main()

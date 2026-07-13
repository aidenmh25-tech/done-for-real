#!/usr/bin/env python3
"""Production-faithful corpus eval for done-for-real.

Feeds verify.py the EXACT hook payload shape observed in Claude Code
(tool_response = {stdout, stderr, ...} with NO exit code, i.e. exit-0 only),
then measures:

  * False-positive rate  — benign commands from other skills/systems/tools that
    must pass silently (exit 0). ANY block here is interference.
  * True-positive rate   — genuinely-lying exit-0 commands that must block
    (exit 2). A miss here means the skill isn't doing its job.

Run:  python tests/corpus_eval.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "..", "skills", "done-for-real", "scripts", "verify.py")


def run_hook(command, stdout="", stderr=""):
    """Invoke verify.py exactly as the PostToolUse hook does: JSON on stdin,
    tool_response carrying stdout/stderr and NO exit code."""
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout, "stderr": stderr,
            "interrupted": False, "isImage": False, "noOutputExpected": False,
        },
    })
    p = subprocess.run([sys.executable, VERIFY], input=payload,
                       capture_output=True, text=True)
    return p.returncode, (p.stderr or "").strip()


# ----------------------------------------------------------------------------
# BENIGN corpus — real, successful (exit-0) commands from the kinds of skills,
# hooks, and tools that share the session. NONE of these may block.
# ----------------------------------------------------------------------------
BENIGN = [
    # --- git (a lot of other skills shell out to git) ---
    ("git commit", 'git commit -m "fix: orders"', "[main a1b2c3d] fix: orders\n 3 files changed, 40 insertions(+), 12 deletions(-)"),
    ("git push", "git push origin main", "To github.com:me/repo.git\n   a1b2c3d..e4f5g6h  main -> main"),
    ("git status", "git status", "On branch main\nnothing to commit, working tree clean"),
    ("git merge conflict resolved note", "git merge feature", "Merge made by the 'ort' strategy.\n 2 files changed"),
    ("git log grep", "git log --oneline -5", "e4f5g6h fix failing test\na1b2c3d add error handling"),
    ("git diff stat", "git diff --stat", " orders/pricing.py | 4 ++--\n 1 file changed"),
    # --- GSD / node hooks (present in this user's settings) ---
    ("gsd context monitor", 'node "C:/Users/x/.claude/hooks/gsd-context-monitor.js"', "context ok"),
    ("gsd check update", 'node "C:/x/.claude/hooks/gsd-check-update.js"', ""),
    ("write planning file", 'echo "phase 3 complete" > .planning/STATE.md', ""),
    ("gsd echo progress", 'echo "Phase 2 FAILED validation last run, now passing"', "Phase 2 FAILED validation last run, now passing"),
    # --- package managers (npm is a catalog tool — must not misfire on success) ---
    ("npm install success + warn", "npm install", "npm warn deprecated foo@1.0.0\nadded 220 packages in 4s"),
    ("npm run build success", "npm run build", "> build\n> vite build\n\n✓ built in 2.3s"),
    ("npm audit mentions vulnerabilities", "npm audit", "found 0 vulnerabilities"),
    ("yarn install", "yarn install", "success Saved lockfile.\nDone in 3.1s"),
    ("pnpm install", "pnpm install", "Packages: +180\nProgress: done"),
    ("npx tsc build ok", "npx tsc --noEmit", ""),
    # --- python / pip ---
    ("pip install ok", "pip install requests", "Successfully installed requests-2.31.0"),
    ("pip list", "pip list", "pytest 9.1.1\nrequests 2.31.0"),
    ("python script prints error word", "python scripts/report.py", "Rows with ERROR status: 0\nDone."),
    ("python migrate success", "python manage.py migrate", "Running migrations:\n  Applying orders.0003... OK"),
    # --- build tools ---
    ("make success", "make build", "gcc -c main.c\nBuild complete."),
    ("cargo build warnings only", "cargo build", "warning: unused variable: x\n    Finished dev [unoptimized] target(s) in 3.2s"),
    ("go build ok", "go build ./...", ""),
    ("docker build success", "docker build -t app .", "Step 8/8 : CMD node index.js\nSuccessfully built a1b2c3d\nSuccessfully tagged app:latest"),
    ("docker ps", "docker ps", "CONTAINER ID   IMAGE   STATUS\nabc123   app   Up 2 minutes"),
    # --- passing test runs (must be recognized as PASS) ---
    ("pytest all pass", "pytest -q", "......\n6 passed in 5.9s"),
    ("pytest pass with skips", "pytest", "==== 120 passed, 3 skipped in 12.4s ===="),
    ("jest pass", "npm test", "Tests:       42 passed, 42 total\nSnapshots:   0 total"),
    ("go test ok", "go test ./...", "ok  \texample.com/orders\t0.312s"),
    ("cargo test pass", "cargo test", "test result: ok. 18 passed; 0 failed; 0 ignored"),
    # --- file ops / redirects / control-flow that exit 0 ---
    ("redirect to /dev/null", "make check > /dev/null 2>&1", ""),
    ("truncate lock file", ": > app.lock", ""),
    ("tee a log", "echo started | tee run.log", "started"),
    ("cat a log mentioning failures", "cat build.log", "line 1\n3 failed earlier\nline 3"),
    ("grep found matches", 'grep -n "error" app.log', "12:handled error path\n40:error recovery ok"),
    ("head of a report", "head -3 pytest_report.txt", "=== 3 failed, 10 passed in 2s ===\ntest_a\ntest_b"),
    # --- compound / chained commands (the misattribution risk zone) ---
    ("compound pytest to devnull + echo failword", 'pytest -q > /dev/null 2>&1; echo "deploy 3 failed earlier, recovered"', "deploy 3 failed earlier, recovered"),
    ("compound build && test", "npm run build && npm test", "✓ built\nTests: 10 passed, 10 total"),
    ("masked with || true", "flake8 src || true", "src/x.py:1:1: E501 line too long"),
    ("arrow in output", 'python -c "print(\'a -> b -> c done\')"', "a -> b -> c done"),
    ("2>&1 fd redirect", "npm run lint 2>&1", "lint passed"),
    # --- deploy / migration mentions that SUCCEEDED (high-stakes, exit 0) ---
    ("deploy succeeded", "npm run deploy", "Deploying...\nDeployment succeeded: https://app.example.com"),
    ("terraform apply ok", "terraform apply -auto-approve", "Apply complete! Resources: 3 added, 0 changed, 0 destroyed."),
    ("alembic upgrade ok", "alembic upgrade head", "INFO  Running upgrade abc -> def, add index"),
    # --- misc tool output that merely mentions scary words ---
    ("eslint zero problems", "eslint .", "✔ 0 problems (0 errors, 0 warnings)"),
    ("ruff clean", "ruff check .", "All checks passed!"),
    ("curl health ok", "curl -sf http://localhost:3000/health", '{"status":"ok"}'),
    ("systemctl status active", "systemctl status app", "Active: active (running)"),
    ("docker logs mentioning error handling", "docker logs app", "server up. error-handling middleware loaded."),
    # --- tool NAMES appearing only inside quoted string args must not count as
    #     invocations of that tool (identify() strips quotes) ---
    ("npm name only in a string arg", "python -c \"print('npm ERR! see the guide')\"", "npm ERR! see the guide"),
    ("git commit msg mentions npm error", 'git commit -m "fix npm error handling"', "[main abc] fix npm error handling\n 1 file changed"),
    ("pytest word in a python string", "python -c \"print('pytest reported 3 failed in 2s')\"", "pytest reported 3 failed in 2s"),
    ("tsc mentioned in a commit message", 'git commit -m "add tsc error TS2322 note"', " 1 file changed, 2 insertions(+)"),
]

# ----------------------------------------------------------------------------
# TRUE-POSITIVE corpus — commands that exited 0 but ACTUALLY failed (the lying
# case). Each must block (exit 2). Placeholder-file case is handled specially.
# ----------------------------------------------------------------------------
LYING = [
    ("pytest failed, piped (exit masked)", "pytest -n 4 | tail",
     "FAILED tests/test_orders.py::test_fill\n3 failed, 3 passed in 6.9s", ""),
    ("pytest errors", "pytest",
     "==== 2 failed, 1 error, 8 passed in 3.1s ====", ""),
    ("tsc type errors, exit 0", "tsc --noEmit",
     "src/a.ts(3,5): error TS2322: Type 'x' is not assignable.", ""),
    ("eslint report mode errors", "eslint . -f json",
     "✖ 5 problems (5 errors, 0 warnings)", ""),
    ("ruff report errors", "ruff check --output-format concise .",
     "Found 4 errors.", ""),
    ("npm modern error prefix", "npm run build",
     "npm error code ELIFECYCLE\nnpm error Failed at build script", ""),
    ("go test FAIL", "go test ./...",
     "--- FAIL: TestFill (0.00s)\nFAIL\texample.com/orders\t0.2s", ""),
    ("cargo compile error", "cargo build",
     "error[E0308]: mismatched types\n --> src/main.rs:4:5", ""),
    ("flake8 findings", "flake8 src",
     "src/x.py:10:1: F401 'os' imported but unused", ""),
    ("npm classic ERR! prefix", "npm run build",
     "npm ERR! code ELIFECYCLE\nnpm ERR! Failed at the build script", ""),
]


def main():
    fp = []   # benign that wrongly blocked
    fn = []   # lying that wrongly passed
    tp = 0

    print("=== BENIGN (must PASS / exit 0) ===")
    for label, cmd, *rest in BENIGN:
        out = rest[0] if rest else ""
        err = rest[1] if len(rest) > 1 else ""
        rc, msg = run_hook(cmd, out, err)
        if rc != 0:
            fp.append(label)
            print(f"  [BLOCK!] {label}\n           {msg.splitlines()[0] if msg else ''}")
    print(f"  {len(BENIGN) - len(fp)}/{len(BENIGN)} passed silently")

    print("\n=== LYING exit-0 (must BLOCK / exit 2) ===")
    for label, cmd, out, err in LYING:
        rc, _ = run_hook(cmd, out, err)
        if rc == 2:
            tp += 1
        else:
            fn.append(label)
            print(f"  [MISS]  {label}")

    # placeholder file-write true positive (needs a real file on disk)
    fd = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    fd.write("API_KEY=<FROM_KEYCHAIN:tok>\n")
    fd.close()
    rc, _ = run_hook(f"echo secret > {fd.name}", "")
    lying_total = len(LYING) + 1
    if rc == 2:
        tp += 1
    else:
        fn.append("placeholder file write")
        print("  [MISS]  placeholder file write")
    os.unlink(fd.name)
    print(f"  {tp}/{lying_total} caught")

    # ---- report ----
    n_benign = len(BENIGN)
    fp_rate = len(fp) / n_benign * 100
    tp_rate = tp / lying_total * 100
    print("\n================= RESULTS =================")
    print(f"False-positive rate : {len(fp)}/{n_benign}  = {fp_rate:.1f}%   (target 0%)")
    print(f"True-positive  rate : {tp}/{lying_total}  = {tp_rate:.1f}%   (target 100%)")
    if fp:
        print(f"\nINTERFERENCE (benign blocked): {fp}")
    if fn:
        print(f"MISSED (lying passed): {fn}")
    ok = not fp and tp == lying_total
    print("\n" + ("PASS — no interference, all lies caught." if ok else "REVIEW NEEDED (see above)."))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

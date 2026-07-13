#!/usr/bin/env python3
"""Wire the done-for-real hooks into a Claude Code settings.json WITHOUT
clobbering any existing hooks — then PROVE the wiring works.

Usage:
    python install-hook.py            # global  (~/.claude/settings.json)
    python install-hook.py --project  # project (./.claude/settings.json)
    python install-hook.py --path /some/settings.json
    python install-hook.py --dry-run  # print the merged result, write nothing
    python install-hook.py --no-smoke # skip the post-install smoke test

Design choices that make this caveat-free:
  * The hook command uses an interpreter that is ACTUALLY on PATH (detected
    here), not a hard-coded `python3` that may not exist on Windows.
  * It points at an ABSOLUTE, forward-slash path to verify.py resolved from this
    script's location — so it works wherever the skill folder lives, not only
    under ~/.claude/skills.
  * After writing, it runs the exact command string it just installed against a
    known-failing and a known-passing payload and verifies exit 2 / exit 0. If
    the wiring is broken, you find out now, not silently at runtime.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY_PY = (HERE / "verify.py").resolve()


def detect_interpreter():
    """Return a python interpreter name/path that is on PATH, or None.

    Prefer bare names (matches the proven pattern of node/python hooks that rely
    on PATH); fall back to the absolute sys.executable, which always exists.
    """
    for name in ("python3", "python", "py"):
        if shutil.which(name):
            return name
    # Last resort: the very interpreter running this installer. Guaranteed valid.
    return _fs(sys.executable)


def _fs(p):
    """Forward-slash a path and quote it — the form Claude Code hooks accept on
    every platform (mirrors the existing `node "C:/..."` hook style)."""
    return str(p).replace("\\", "/")


def build_commands():
    interp = detect_interpreter()
    verify = f'"{_fs(VERIFY_PY)}"'
    base = f'{interp} {verify}'
    return interp, base, base + " --stop-recheck"


def load_json(path):
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not valid JSON ({e}). Fix it before installing.")


def has_our_hook(entry):
    return any("verify.py" in h.get("command", "") for h in entry.get("hooks", []))


def merge(settings, post_cmd, stop_cmd):
    hooks = settings.setdefault("hooks", {})

    post = hooks.setdefault("PostToolUse", [])
    if not any(has_our_hook(e) for e in post if e.get("matcher") == "Bash"):
        post.append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": post_cmd}],
        })

    stop = hooks.setdefault("Stop", [])
    if not any(has_our_hook(e) for e in stop):
        stop.append({"hooks": [{"type": "command", "command": stop_cmd}]})

    return settings


# --------------------------------------------------------------------------- #
# Post-install smoke test — runs the EXACT installed command string.
# --------------------------------------------------------------------------- #

FAIL_PAYLOAD = json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -n 4 tests/"},
    "tool_response": {"stdout": "3 failed, 120 passed in 4s", "exit_code": 0},
})
PASS_PAYLOAD = json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": "ls -la"},
    "tool_response": {"stdout": "total 8", "exit_code": 0},
})


def _run_installed(command_str, payload):
    """Execute the installed command string via the shell, exactly as the hook
    runner would, feeding the payload on stdin. Returns the exit code."""
    p = subprocess.run(command_str, input=payload, shell=True,
                       capture_output=True, text=True)
    return p.returncode, (p.stderr or "").strip()


def smoke_test(post_cmd):
    print("\nSmoke test (running the installed command string as the hook would):")
    ok = True

    rc, msg = _run_installed(post_cmd, FAIL_PAYLOAD)
    if rc == 2:
        print("  [ok]   lying pytest (exit0 + '3 failed') -> BLOCKED (exit 2)")
    else:
        ok = False
        print(f"  [FAIL] expected exit 2 on a failing payload, got exit {rc}.")
        print("         The hook will NOT catch failures. Check the interpreter/path.")
        if msg:
            print("         stderr:", msg.splitlines()[0])

    rc, _ = _run_installed(post_cmd, PASS_PAYLOAD)
    if rc == 0:
        print("  [ok]   read-only `ls` -> passed silently (exit 0)")
    else:
        ok = False
        print(f"  [FAIL] expected exit 0 on a benign payload, got exit {rc}.")

    # Retry counters must not leak from the smoke test.
    _clear_counter("pytest -n 4 tests/")
    return ok


def _clear_counter(command):
    import hashlib
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    h = hashlib.sha1(command.encode("utf-8")).hexdigest()[:16]
    try:
        (Path(base) / "done-for-real" / h).unlink()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", action="store_true",
                    help="install into ./.claude/settings.json")
    ap.add_argument("--path", help="explicit path to a settings.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    args = ap.parse_args()

    if not VERIFY_PY.exists():
        sys.exit(f"error: cannot find verify.py at {VERIFY_PY}")

    interp, post_cmd, stop_cmd = build_commands()
    if interp is None:
        sys.exit("error: no python interpreter found on PATH. Install Python "
                 "first, or edit the hook command in settings.json by hand.")

    if args.path:
        target = Path(args.path)
    elif args.project:
        target = Path(".claude") / "settings.json"
    else:
        target = Path(os.path.expanduser("~")) / ".claude" / "settings.json"

    settings = load_json(target)
    merged = merge(settings, post_cmd, stop_cmd)
    output = json.dumps(merged, indent=2)

    if args.dry_run:
        print(f"# interpreter: {interp}")
        print(f"# would write to {target}:\n")
        print(output)
        # smoke test still runs — it doesn't need the file written.
        if not args.no_smoke:
            ok = smoke_test(post_cmd)
            sys.exit(0 if ok else 1)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(output + "\n", encoding="utf-8")
    print(f"Installed done-for-real hooks into {target}")
    print(f"  interpreter : {interp}")
    print(f"  PostToolUse : {post_cmd}")
    print(f"  Stop        : {stop_cmd}")

    if not args.no_smoke:
        ok = smoke_test(post_cmd)
        if not ok:
            sys.exit("\nWiring is broken — see FAIL above. The settings were "
                     "written but the hook won't work until fixed.")
        print("\nWiring verified. The hook is live for new tool calls in this scope.")


if __name__ == "__main__":
    main()

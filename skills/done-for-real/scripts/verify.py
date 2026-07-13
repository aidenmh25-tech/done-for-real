#!/usr/bin/env python3
"""done-for-real verification engine.

Runs OFF-CONTEXT. Its code never enters Claude's context window; only its short
verdict does. Silent on success (exit 0), loud only on failure (exit 2).

Invocation modes
----------------
1. PostToolUse / Stop hook: reads the hook JSON payload on stdin.
2. Direct (from Claude): `verify.py "<command>" --exit N` with stdout/stderr
   either piped on stdin's "output" field or passed via --stdout/--stderr.

Exit codes
----------
  0  PASS or gated-out or UNSURE  -> silent (or a one-line caution on stderr)
  2  FAIL -> stderr carries the compact verdict; THIS is what blocks & feeds
     the message back to Claude. Only exit 2 blocks in Claude Code hooks.

Observed Claude Code hook behaviour (verified empirically, 2026)
----------------------------------------------------------------
The PostToolUse Bash hook fires ONLY after a command that exited 0, and the
payload's tool_response carries {stdout, stderr, interrupted, ...} but NO exit
code. Two consequences shape this engine:

  * A genuinely FAILED command (nonzero exit) never reaches the hook at all —
    and it doesn't need to: Claude already sees "Exit code N" in the tool result
    and won't mistake it for success. The hook's real and only niche is the
    LYING case: a command that exited 0 while actually having failed (pytest
    piped/xdist, a linter in report mode, a masked `cmd; echo done`). That is
    exactly what this skill exists to catch.
  * Because no exit code is provided, in-hook detection is entirely TEXT-based:
    Tier 1 tool-specific rules on the tool's own output, plus the placeholder
    file-write check. The exit-code-driven branches below still exist and are
    exercised by the direct-invocation mode (`--exit N`) and the test suite, and
    would activate automatically if a future Claude Code version does pass exit
    codes / fire on failures — so the engine is correct either way.
"""

import sys
import os
import re
import json
import argparse
import hashlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# Catalog loading (parsed from references/lying-tools.md at import time)
# --------------------------------------------------------------------------- #

CATALOG_PATH = Path(__file__).resolve().parent.parent / "references" / "lying-tools.md"


def load_catalog():
    """Parse the markdown catalog into a list of {name, detect, tier, high_stakes}."""
    entries = []
    if not CATALOG_PATH.exists():
        return entries
    text = CATALOG_PATH.read_text(encoding="utf-8")
    # Split on level-2 headings; skip the doc preamble sections.
    blocks = re.split(r"^##\s+", text, flags=re.MULTILINE)
    skip = {"Entry format"}
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        name = lines[0].strip()
        if not name or name in skip or name.startswith("#"):
            continue
        fields = {}
        for line in lines[1:]:
            m = re.match(r"^(detect|tier|high_stakes):\s*(.*)$", line.strip())
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if "detect" not in fields:
            continue
        try:
            pattern = re.compile(fields["detect"])
        except re.error:
            continue
        entries.append({
            "name": name,
            "detect": pattern,
            "tier": int(fields.get("tier", "0")),
            "high_stakes": fields.get("high_stakes", "false").lower() == "true",
        })
    return entries


CATALOG = load_catalog()

# --------------------------------------------------------------------------- #
# Gate: is this action worth verifying at all?
# --------------------------------------------------------------------------- #

# Commands with a side effect or an implicit completion claim.
ACTION_RE = re.compile(
    r"\b(pytest|test|build|lint|tsc|typecheck|type-check|mypy|eslint|ruff|flake8|"
    r"migrate|migration|alembic|deploy|kubectl|terraform|install|compile|"
    r"cargo|go test|npm|yarn|pnpm|make|serve|start|nohup|launchctl|systemctl)\b"
    r"|\s&\s*$|\|\|\s*true\b",
    re.IGNORECASE,
)

# Purely read-only / no-side-effect commands -> never verify.
READONLY_RE = re.compile(
    r"^\s*(ls|cat|less|more|head|tail|pwd|echo|which|type|find|grep|rg|"
    r"view|stat|file|wc|tree|env|printenv|whoami|date|git (status|log|diff|show|branch))\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Tier 0 signatures
# --------------------------------------------------------------------------- #

# STRONG signatures are unambiguous failure evidence — they always block,
# regardless of exit code or whether the tool is in the catalog. Note the
# leading [1-9]: "0 failed" / "0 errors" is a PASS, not a match.
STRONG_SIGNATURES = [
    r"\b[1-9]\d*\s+failed\b",
    r"\b[1-9]\d*\s+errors?\b",
    r"\bpanic:",
    r"npm ERR!",
    r"Traceback \(most recent call last\)",
    r"\berror TS\d+",
    r"error\[",
    r"test result:\s*FAILED",
    r"---\s*FAIL:",
]

# WEAK signatures are suggestive but appear in benign output too (a log line
# that mentions "FAILED", a progress marker). They only block when corroborated
# by a nonzero exit code OR a recognized catalog tool — never on their own.
WEAK_SIGNATURES = [
    r"\bFAILED\b",
    r"\bERROR\b",
    r"✗",
]

# Placeholder tokens that must never appear in a "written" file.
PLACEHOLDER_RE = re.compile(
    r"<FROM_KEYCHAIN|PLACEHOLDER|<YOUR_|XXXXX|TODO_?REPLACE|CHANGEME|\bFIXME_SECRET\b"
)

TAIL_CHARS = 4000  # only scan the tail of output — cheap and sufficient.


def tail(s):
    s = s or ""
    return s[-TAIL_CHARS:]


# --------------------------------------------------------------------------- #
# Retry counter (bounds the fix loop at 3)
# --------------------------------------------------------------------------- #

def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    d = Path(base) / "done-for-real"
    d.mkdir(parents=True, exist_ok=True)
    return d


def counter_path(command):
    h = hashlib.sha1((command or "").encode("utf-8")).hexdigest()[:16]
    return cache_dir() / h


def bump_retry(command):
    p = counter_path(command)
    n = 0
    try:
        n = int(p.read_text())
    except Exception:
        n = 0
    n += 1
    try:
        p.write_text(str(n))
    except Exception:
        pass
    return n


def reset_retry(command):
    try:
        counter_path(command).unlink()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Verdict emission
# --------------------------------------------------------------------------- #

MAX_RETRIES = 3


def fail(tool, summary, evidence, command):
    n = bump_retry(command)
    tag = f"[{tool}]" if tool else ""
    if n > MAX_RETRIES:
        msg = (
            f"VERIFY FAIL {tag}: {summary}\n"
            f"Evidence: {evidence}\n"
            f"Retry budget EXHAUSTED ({n-1}/{MAX_RETRIES} attempts already made). "
            f"STOP looping. Surface to the human: what failed, the evidence above, "
            f"and every fix you tried."
        )
    else:
        msg = (
            f"VERIFY FAIL {tag}: {summary}\n"
            f"Evidence: {evidence}\n"
            f"Do NOT report success. Diagnose the ROOT CAUSE, fix it, then re-run "
            f"the exact same command. This is retry {n}/{MAX_RETRIES}."
        )
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def unsure(note):
    sys.stderr.write(f"VERIFY UNSURE: {note}\n")
    sys.exit(0)


def passed(command):
    reset_retry(command)
    sys.exit(0)


# --------------------------------------------------------------------------- #
# Tool identification
# --------------------------------------------------------------------------- #

def identify(command):
    # Match tool names only as real command tokens, not inside quoted string
    # arguments. Otherwise `python -c "...npm ERR!..."` or
    # `git commit -m "fix npm error"` would be misread as an npm invocation.
    scanned = _strip_quoted(command or "")
    for entry in CATALOG:
        if entry["detect"].search(scanned):
            return entry
    return None


# --------------------------------------------------------------------------- #
# Tier 1 tool-specific rules
# --------------------------------------------------------------------------- #

def _pytest_summary_line(out):
    """Return pytest's summary line, or None. The summary always ends with a
    "in <time>s" clause and reports at least one test-count category, e.g.
    "= 3 failed, 3 passed in 6.97s =" or "1 failed in 0.12s". This structure is
    what distinguishes real pytest output from an arbitrary line mentioning
    "failed", so stray text in a compound command is never misread as a result."""
    for line in out.splitlines():
        if re.search(r"\bin\s+\d+(\.\d+)?\s*s\b", line) and \
           re.search(r"\b\d+\s+(failed|passed|error|errors|skipped|deselected"
                     r"|xfailed|xpassed|warnings?)\b", line):
            return line
    return None


def tier1(name, command, out, exit_code):
    """Return (is_fail, summary, evidence) or None if this tier is inconclusive."""
    low = out
    if name == "pytest":
        # Anchor to pytest's ACTUAL summary line, not a loose "N failed"
        # substring — otherwise a compound command that merely prints "3 failed"
        # (an echo, a diff of logs) would be misattributed to pytest. The real
        # summary always pairs a count with a "in <time>s" tail, e.g.
        # "3 failed, 3 passed in 6.97s". If we can't find that line, pytest's
        # output isn't in the captured text (piped elsewhere) -> inconclusive.
        summary = _pytest_summary_line(low)
        if summary is None:
            return None
        mf = re.search(r"(\d+)\s+failed\b", summary)
        me = re.search(r"(\d+)\s+error", summary)
        nf = int(mf.group(1)) if mf else 0
        ne = int(me.group(1)) if me else 0
        if nf > 0 or ne > 0:
            ev = re.search(r"(\S+::\S+ FAILED[^\n]*)", low)
            return (True, f'summary says "{nf} failed, {ne} error(s)"',
                    ev.group(1) if ev else summary.strip()[:140])
        return (False, "", "")  # a real summary line with 0 failed/errors -> PASS
    if name == "npm":
        # Classic npm uses "npm ERR!"; npm 9+ switched to "npm error". Match both.
        # Note: no \b after "ERR!" — "!" is already a non-word char, and a
        # trailing \b there would (wrongly) never match. Use \b only on the word
        # form so "npm error" matches but "npm errorhandler" does not.
        m = re.search(r"(npm (?:ERR!|error\b)[^\n]*)", low)
        if m:
            return (True, "npm reported an error", m.group(1)[:120])
        return None
    if name == "tsc":
        errs = re.findall(r"error TS\d+[^\n]*", low)
        if errs:
            return (True, f"{len(errs)} TypeScript error(s)", errs[0][:140])
        return None
    if name in ("eslint", "ruff", "flake8"):
        m = re.search(r"(\d+)\s+error", low)
        if m and int(m.group(1)) > 0:
            return (True, f"{m.group(1)} lint error(s)", _first_lint_line(low))
        # flake8 style: bare code lines
        codes = re.findall(r":\d+:\d+:\s+[EWF]\d{3}\b[^\n]*", low)
        if codes:
            return (True, f"{len(codes)} lint finding(s)", codes[0].strip()[:140])
        return None
    if name == "go test":
        if re.search(r"---\s*FAIL:|^FAIL\b", low, re.MULTILINE):
            ev = re.search(r"---\s*FAIL:[^\n]*", low)
            return (True, "go test reports FAIL", ev.group(0) if ev else "FAIL")
        return None
    if name == "cargo":
        if re.search(r"test result:\s*FAILED|error\[", low):
            ev = re.search(r"(error\[[^\n]*|test result:\s*FAILED[^\n]*)", low)
            return (True, "cargo build/test failed", ev.group(1) if ev else "FAILED")
        return None
    return None


def _first_lint_line(out):
    for line in out.splitlines():
        if re.search(r"error|warning", line, re.IGNORECASE):
            return line.strip()[:140]
    return "see lint output"


# --------------------------------------------------------------------------- #
# Tier 2 semantic checks (high-stakes only)
# --------------------------------------------------------------------------- #

def tier2(entry, command, out, exit_code):
    """High-stakes real-effect checks. Emits an instruction if it can't self-check."""
    name = entry["name"]
    if name == "background process":
        # We can't reliably know the PID from here; instruct Claude to confirm.
        if exit_code not in (0, None):
            return (True, "background process launch returned nonzero",
                    f"exit={exit_code}; process almost certainly not running")
        return (True, "launch succeeded but RUN not confirmed",
                "confirm the process is alive (pgrep/ps) and, if it serves a "
                "port, that the port responds. A launch that exits immediately "
                "is a FAIL. This check must be run — do not assume success.")
    if name in ("migration",):
        return (True, "migration returned without proof the schema advanced",
                "assert the revision/schema version actually moved forward "
                "(e.g. `alembic current`, check migrations table).")
    if name in ("deploy",):
        return (True, "deploy returned without proof the service is live",
                "curl the endpoint / check rollout status to confirm the new "
                "version is actually serving.")
    return None


def _strip_quoted(s):
    """Remove single/double-quoted spans so operators INSIDE strings (a "->"
    arrow, a ">" in a message) are never mistaken for shell syntax."""
    s = re.sub(r'"(?:[^"\\]|\\.)*"', " ", s)
    s = re.sub(r"'(?:[^'\\]|\\.)*'", " ", s)
    return s


# A genuine redirect: optional single fd digit + > or >>, anchored to a
# whitespace/line-start boundary so it does NOT match `->`, `=>`, `>=`, `<=`.
_REDIRECT_RE = re.compile(r"(?:(?<=\s)|^)\d?>{1,2}\s*([^\s;|&<>]+)")
_TEE_RE = re.compile(r"\btee\b\s+(?:-a\s+)?([^\s;|&<>]+)")


def find_write_target(command):
    """Return the file path a command redirects into, or None. Quote-aware and
    operator-precise so non-redirect `>` usage never produces a phantom target."""
    stripped = _strip_quoted(command or "")
    m = _TEE_RE.search(stripped)
    if m:
        return m.group(1)
    m = _REDIRECT_RE.search(stripped)
    if m:
        target = m.group(1)
        if target.startswith("&"):  # `2>&1` and friends are fd dups, not files
            return None
        return target
    return None


# Null sinks / special devices that are legitimately "written" to constantly.
_SINK_RE = re.compile(r"^(/dev/(null|stdout|stderr|tty|zero)|nul)$", re.IGNORECASE)


def check_file_write(target):
    """Flag ONLY the high-confidence failure: a written file that still contains
    a placeholder token (a secret that never got substituted, a TODO stub).

    We deliberately do NOT block on "file missing" or "file empty": redirecting
    to /dev/null, truncating with `: > file`, and creating empty lock files are
    all legitimate, and a genuinely failed write already surfaces via a nonzero
    exit. Blocking those would interrupt routine commands for no real defect.
    """
    if _SINK_RE.match(target):
        return None
    path = Path(os.path.expanduser(target))
    try:
        if not path.is_file():
            return None
        content = path.read_text(errors="ignore")
    except Exception:
        return None
    ph = PLACEHOLDER_RE.search(content)
    if ph:
        return (True, f"{path} contains placeholder token", ph.group(0))
    return None


# --------------------------------------------------------------------------- #
# Core verification
# --------------------------------------------------------------------------- #

def verify(command, exit_code, stdout, stderr):
    out = tail(stdout) + "\n" + tail(stderr)
    command = command or ""

    entry = identify(command)
    name = entry["name"] if entry else None
    masked = bool(re.search(r"\|\|\s*true\b|;\s*(echo|true)\b", command))
    # `|| true`, `|| :`, `; true` are the developer EXPLICITLY opting out of this
    # command's failure — a ubiquitous idiom for "make this step non-fatal"
    # (lint/test in Makefiles, CI, other skills). Respect it: never hard-block a
    # result its author deliberately discarded.
    ignored = bool(re.search(r"\|\|\s*(true|:)\b|;\s*(true|:)\s*(#.*)?$", command))

    # 1. file-write semantic check — runs BEFORE the gate, because a redirect
    #    into a file (even via `echo`) is a side effect worth checking. Uses a
    #    quote-aware, operator-precise extractor so `->`/`=>`/`2>&1`/quoted `>`
    #    never yield a phantom target.
    write_target = find_write_target(command)
    if write_target:
        fw = check_file_write(write_target)
        if fw:
            fail(name or "file-write", fw[1], fw[2], command)

    # 2. GATE
    if READONLY_RE.search(command):
        passed(command)
    if not ACTION_RE.search(command):
        passed(command)

    # 3. TIER 1 (catalog tools) — most authoritative, run before trusting exit code
    if entry and entry["tier"] >= 1:
        r = tier1(name, command, out, exit_code)
        if r is not None:
            is_fail, summary, evidence = r
            if is_fail:
                if ignored:
                    unsure(f"[{name}] {summary} — result explicitly ignored "
                           f"via '|| true'/'; true'; not blocking.")
                fail(name, summary, evidence, command)
            else:
                passed(command)  # tool-specific PASS overrides exit-code noise

    # 4. TIER 0 — output signature scan.
    nonzero = exit_code not in (0, None)

    # Search/compare tools emit arbitrary DATA as output (a grepped log line, a
    # diff of files that mention "failed"); their output is never a self-report,
    # so we never scan it and never block on their nonzero exits.
    safe_nonzero = bool(entry and entry["name"] in ("grep", "diff"))
    if safe_nonzero:
        passed(command)
    #
    # CRITICAL: we only scan output on a NONZERO exit. We must NOT scan the
    # output of a command that exited 0, because countless benign commands
    # legitimately *print or quote* failure words: an installer echoing
    # "3 failed", a log viewer, `echo`, a `; echo done` chain, CI output pasted
    # to stdout. Scanning those would block them. The "lying exit 0" case that
    # motivates output inspection is ALWAYS a known tool (pytest/tsc/eslint/...),
    # and those are fully handled by their Tier 1 rule at step 3 above — so
    # nothing is lost by not scanning arbitrary exit-0 output here.
    if nonzero:
        # STRONG signatures: unambiguous failure evidence.
        for pat in STRONG_SIGNATURES:
            m = re.search(pat, out)
            if m:
                fail(name or "tier0", f'exit={exit_code} and output shows "{m.group(0)}"',
                     _evidence_line(out, m.group(0)), command)

        # WEAK signatures ("FAILED"/"ERROR"/"✗"): corroborated by the nonzero exit.
        for pat in WEAK_SIGNATURES:
            m = re.search(pat, out)
            if m:
                fail(name or "tier0",
                     f'exit={exit_code} with failure marker "{m.group(0)}"',
                     _evidence_line(out, m.group(0)), command)

    # Bare nonzero exit (no signature): only block for RECOGNIZED catalog tools.
    # For everything else, nonzero is frequently normal control flow — `test`,
    # `[ ]`, `pgrep`, `curl` probes — so we do NOT block, to stay seamless with
    # other skills and shell scripts. (grep/diff already returned above.)
    if nonzero and entry is not None and not masked:
        fail(name, f"nonzero exit={exit_code} from {name} with no suppression rule",
             _last_nonempty(out), command)

    # 5. TIER 2 — high-stakes real-effect checks.
    #    On a nonzero exit we have concrete failure evidence -> hard block.
    #    On a clean (exit 0) launch we CANNOT self-verify from the hook's
    #    vantage, so we emit a non-blocking caution instead of interrupting.
    if entry and entry["high_stakes"]:
        r = tier2(entry, command, out, exit_code)
        if r is not None:
            _, summary, evidence = r
            if nonzero:
                fail(name, summary, evidence, command)
            else:
                unsure(f"[{name}] {summary}: {evidence}")

    # 6. masked exit code with clean output -> caution, non-blocking
    if masked and not sig:
        unsure(f"exit code is masked in `{command[:60]}` — verified output only.")

    passed(command)


def _evidence_line(out, sig):
    for line in out.splitlines():
        if sig in line:
            return line.strip()[:160]
    return sig


def _last_nonempty(out):
    for line in reversed(out.splitlines()):
        if line.strip():
            return line.strip()[:160]
    return "(no output)"


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #

def from_hook_payload(payload):
    """Extract fields from a Claude Code PostToolUse hook payload.

    The Bash tool_response shape has varied across Claude Code versions (dict
    with stdout/stderr, dict with a single output/content field, or a bare
    string). Be liberal: pull the command from tool_input, and gather every
    plausible text field into stdout/stderr so the signature scan has something
    to read. Missing exit_code -> None (we then rely on signatures, never on a
    bare exit code).
    """
    tool_input = payload.get("tool_input", {}) or {}
    tool_resp = payload.get("tool_response", payload.get("tool_result", {})) or {}
    command = (tool_input.get("command")
               or payload.get("command")
               or (tool_input.get("cmd") if isinstance(tool_input, dict) else "")
               or "")

    if isinstance(tool_resp, dict):
        parts = [str(tool_resp.get(k, "")) for k in
                 ("stdout", "output", "content", "stdoutText", "result")]
        stdout = "\n".join(p for p in parts if p)
        stderr = str(tool_resp.get("stderr", "") or tool_resp.get("stderrText", ""))
        exit_code = tool_resp.get("exit_code",
                                  tool_resp.get("exitCode",
                                                tool_resp.get("returncode")))
        if exit_code is None and (tool_resp.get("is_error")
                                  or tool_resp.get("isError")):
            exit_code = 1
    else:
        stdout, stderr, exit_code = str(tool_resp), "", None
    return command, exit_code, stdout, stderr


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("command", nargs="?", default=None)
    ap.add_argument("--exit", dest="exit_code", type=int, default=None)
    ap.add_argument("--stdout", default="")
    ap.add_argument("--stderr", default="")
    ap.add_argument("--stop-recheck", action="store_true")
    args = ap.parse_args()

    # Stop-hook mode: no payload of its own; nothing to re-verify deterministically
    # here (per-command state already handled during PostToolUse). Stay silent.
    if args.stop_recheck:
        sys.exit(0)

    if args.command is not None:
        verify(args.command, args.exit_code, args.stdout, args.stderr)
        return

    # Hook mode: read JSON on stdin.
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)  # not our payload; stay silent
    command, exit_code, stdout, stderr = from_hook_payload(payload)
    verify(command, exit_code, stdout, stderr)


if __name__ == "__main__":
    # FAIL OPEN. A verification tool must never become the thing that breaks the
    # user's workflow. The intentional blocking signal is sys.exit(2), which
    # raises SystemExit (a BaseException) and passes through this guard untouched.
    # Any real bug (a bad regex, an unexpected payload) is swallowed and we exit
    # 0 — silently proceeding rather than wedging or crash-spamming the session.
    try:
        main()
    except Exception:
        sys.exit(0)

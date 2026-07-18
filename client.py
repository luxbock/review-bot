#!@PYTHON@
"""review-bot-review — thin, credential-free CLIENT for the review-bot service.

Same argv surface as the old in-process CLI (poll.py and every other caller
migrate by doing nothing). Instead of running the engines in-process — which
required the caller to hold the forge token and live LLM credentials
(CLAUDE_CONFIG_DIR/CODEX_HOME) — it serializes the flags into a one-line JSON
request, sends it to `review-bot-serve` over the Unix socket at
$REVIEW_BOT_SOCKET (default /run/review-bot/review.sock), streams the NDJSON
response, and mirrors the old CLI's stdout/exit-code contract:

  --print-only  -> the review markdown on stdout;
  otherwise     -> the posted comment URL on stdout;
  exit 0 on success, 1 on failure (error detail on stderr).

Progress ({"type":"log"}) events are relayed to stderr. The service holds all
credentials; this process never sees them.

`--repo-dir` cannot cross the service boundary (the service must not read
arbitrary caller paths) — for that, run `review-bot-review-local` directly,
which needs local credentials.
"""

import argparse
import json
import os
import socket
import sys
import time

SOCKET_PATH = os.environ.get("REVIEW_BOT_SOCKET", "/run/review-bot/review.sock")
FOCUS_CAP = 2000  # keep in sync with serve.py (server truncates too)
SERVICE_SOCKET_UNIT = "review-bot-review.socket"
CONNECTION_LOST = (
    "the review-bot service connection was lost — the review outcome is unknown "
    "and may already have posted; inspect the target or service journal before retrying"
)
# Truthful signal that the socket unit refused the request past MaxConnections=N:
# review-bot-serve never ran, so no side-effects to worry about. Distinct from
# CONNECTION_LOST (outcome unknown). Exit 75 == sysexits.h EX_TEMPFAIL.
SERVICE_BUSY = (
    "review-bot service busy — a review is already in flight; retry later"
)
EX_TEMPFAIL = 75
# Bounded exponential backoff for the busy-retry loop, all env-overridable
# (only the count needs to be tunable in practice — the others are here for
# tests and unusual deployments).
BUSY_BACKOFF_BASE = float(os.environ.get("REVIEW_BOT_BUSY_BACKOFF_BASE", "1"))
BUSY_BACKOFF_FACTOR = float(os.environ.get("REVIEW_BOT_BUSY_BACKOFF_FACTOR", "2"))
BUSY_BACKOFF_CAP = float(os.environ.get("REVIEW_BOT_BUSY_BACKOFF_CAP", "30"))


def die(msg, code=1):
    print(f"review-bot-review: error: {msg}", file=sys.stderr)
    sys.exit(code)


def build_request(args):
    """argv -> the whitelisted request object serve.py accepts. Mirrors the mode /
    harness resolution the in-process main() does, so errors surface client-side."""
    if args.repo_dir:
        die(
            "--repo-dir is not supported across the service boundary — the review-bot "
            "service only reviews its own cache clones and must not read arbitrary "
            "caller paths. For a local-checkout run use `review-bot-review-local` "
            "directly (requires local forge + LLM credentials)."
        )

    mode = args.mode
    if args.scope == "repo":
        if mode and mode != "repo":
            die("--scope repo conflicts with --mode " + mode)
        mode = "repo"
    if not mode:
        mode = "issue" if (args.issue is not None and args.pr is None) else "pr"
    if mode == "pr" and args.pr is None:
        die("mode=pr requires --pr N")
    if mode == "issue" and args.issue is None:
        die("mode=issue requires --issue N")
    if mode == "repo" and (args.pr is not None or args.issue is not None):
        die("mode=repo takes no --pr/--issue number (it audits the whole repo)")
    # mode=repo is numberless — the whole-repo audit needs no PR/issue target.
    number = None
    if mode != "repo":
        number = args.pr if mode == "pr" else args.issue
        if number <= 0:
            die(f"--{'pr' if mode == 'pr' else 'issue'} must be a positive integer")

    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    for h in harnesses:
        if h not in ("claude", "codex"):
            die(f"unknown harness '{h}' (supported: claude, codex)")
    if not harnesses:
        die("no harness given")

    request = {
        "mode": mode,
        "owner": args.owner,
        "repo": args.repo,
        "harness": ",".join(harnesses),
        "depth": args.depth,
        "confidence_bar": args.confidence_bar,
        "focus": args.focus[:FOCUS_CAP],
        "print_only": args.print_only,
        "dry_run": args.dry_run,
    }
    # Omit 'number' entirely for repo mode (serve.py makes it optional only for repo).
    if number is not None:
        request["number"] = number
    return request


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except FileNotFoundError:
        sock.close()
        die(
            f"review-bot service socket not found at {path} — check that "
            f"{SERVICE_SOCKET_UNIT} is running, or set REVIEW_BOT_SOCKET to the correct path"
        )
    except ConnectionRefusedError:
        sock.close()
        die(
            f"review-bot service refused the connection at {path} — check "
            f"{SERVICE_SOCKET_UNIT} and REVIEW_BOT_SOCKET"
        )
    except PermissionError:
        sock.close()
        die(
            f"permission denied connecting to the review-bot service at {path} — check "
            f"{SERVICE_SOCKET_UNIT} and the review-bot-client supplementary group; after "
            f"group changes, restart the login/session or any long-running user manager"
        )
    except OSError as e:
        sock.close()
        die(
            f"cannot connect to the review-bot service at {path} ({e}) — check "
            f"{SERVICE_SOCKET_UNIT} and REVIEW_BOT_SOCKET"
        )
    return sock


def _one_attempt(request):
    """Send `request`, read the NDJSON response, return (result | None, saw_any_event).

    `saw_any_event` is True as soon as ANY byte-derived line reaches us — even a
    malformed / unknown-type event — because that proves the serve process
    accepted the connection and started emitting. A stream that closes with
    `saw_any_event = False` is a *busy-drop* (systemd refused past
    MaxConnections; serve never ran); with `saw_any_event = True` it's a
    genuine mid-review loss.
    """
    sock = connect(SOCKET_PATH)
    result = None
    saw_any_event = False
    try:
        # A peer can finish and send its result while our send/shutdown observes its
        # close or reset. Keep reading in that case: a complete result is authoritative.
        try:
            sock.sendall((json.dumps(request) + "\n").encode())
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            with sock.makefile("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    saw_any_event = True
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        print(f"review-bot-review: unparseable event: {line[:200]}", file=sys.stderr)
                        continue
                    if not isinstance(event, dict):
                        continue
                    etype = event.get("type")
                    if etype == "log":
                        print(f"review-bot: {event.get('message', '')}", file=sys.stderr)
                    elif etype == "result":
                        result = event
                        break
                    # unknown event types are ignored (forward compatibility)
        except OSError:
            pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return result, saw_any_event


def main():
    # Argv surface kept identical to the old in-process CLI (review.py main()).
    ap = argparse.ArgumentParser(description="Run review-bot on a Forgejo PR or issue (via the review-bot service).")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mode", default="", choices=["", "pr", "issue", "repo"], help="pr (default) | issue | repo")
    ap.add_argument("--scope", default="", choices=["", "repo"], help="alias: --scope repo maps to --mode repo")
    ap.add_argument("--pr", type=int, help="PR number (mode=pr)")
    ap.add_argument("--issue", type=int, help="issue number (mode=issue)")
    ap.add_argument("--harness", default="claude", help="claude | codex | claude,codex")
    ap.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
    ap.add_argument("--focus", default="", help="advisory, untrusted focus directive")
    ap.add_argument("--confidence-bar", default="", choices=["", "low", "medium", "high"])
    ap.add_argument("--repo-dir", default="", help="unsupported here — see review-bot-review-local")
    ap.add_argument("--dry-run", action="store_true", help="service prints prompt(s) to its journal, posts nothing")
    ap.add_argument("--print-only", action="store_true", help="run engines but print markdown, don't POST")
    args = ap.parse_args()

    request = build_request(args)

    try:
        retries = int(os.environ.get("REVIEW_BOT_BUSY_RETRIES", "6"))
    except ValueError:
        retries = 6
    retries = max(0, retries)

    # `retries + 1` = initial attempt + up to `retries` reconnects on busy-drop.
    # A ≥1-event stream close aborts the loop with CONNECTION_LOST (exit 1) —
    # only zero-event drops are retried.
    delay = BUSY_BACKOFF_BASE
    attempts_left = retries + 1
    result = None
    while attempts_left > 0:
        attempts_left -= 1
        result, saw_any_event = _one_attempt(request)
        if result is not None:
            break
        if saw_any_event:
            die(CONNECTION_LOST)
        if attempts_left > 0:
            time.sleep(delay)
            delay = min(delay * BUSY_BACKOFF_FACTOR, BUSY_BACKOFF_CAP)

    if result is None:
        die(SERVICE_BUSY, code=EX_TEMPFAIL)
    if not result.get("ok"):
        die(result.get("error") or "review failed (service reported no error detail)")
    # Mirror the old CLI's stdout: markdown when --print-only, else the posted URL.
    if args.print_only:
        print(result.get("markdown") or "")
    elif result.get("url"):
        print(result["url"])
    sys.exit(0)


if __name__ == "__main__":
    main()

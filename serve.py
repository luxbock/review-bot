#!@PYTHON@
"""review-bot-serve — inetd-style, credential-isolating front door for review-bot.

Runs under a systemd socket unit (Accept=yes, MaxConnections=1): one connection
= one process instance, with stdin/stdout wired to the connection socket and
stderr going to the journal. It reads exactly ONE request from stdin — a single
JSON object on one line, at most 64 KiB, within ~30 s — then runs the EXISTING
review/triage pipeline (imported from review.py; the service user owns the LLM
and forge credentials, callers never do) and streams NDJSON events on stdout.

Request fields (whitelist — any unknown field is a hard error):
  mode            "pr" (default) | "issue"
  owner, repo     required, [A-Za-z0-9_.-]+
  number          required, positive int (the PR / issue number)
  harness         "claude" | "codex" | "claude,codex"          (default claude)
  depth           quick | standard | deep                      (default standard)
  confidence_bar  "" | low | medium | high                     (default "")
  focus           free text, capped at 2000 chars
  print_only      bool — don't POST the comment, return the markdown only
  dry_run         bool — print prompts to the journal, run no engines

Deliberately NOT accepted: repo_dir (would let a caller point the service at an
arbitrary path readable by the service user) and any engine-command override —
REVIEW_BOT_CLAUDE_CMD / REVIEW_BOT_CODEX_CMD are honoured from the SERVICE's
own environment (trusted, set by the unit), never from the request.

Response events (NDJSON, one object per line on stdout):
  {"type":"log","message":<string>}                               (progress)
  {"type":"result","ok":<bool>,"markdown":<string|null>,
   "url":<string|null>,"error":<string|null>}                     (final line)

An invalid request still gets a result event (ok=false) and a nonzero exit.
"""

import argparse
import contextlib
import importlib.util
import json
import os
import re
import selectors
import socket
import struct
import sys
import time

# ── Build-time substituted constants (see default.nix) ─────────────────────────
REVIEW_IMPL = "@REVIEW_IMPL@"  # the installed (substituted) review.py module

MAX_REQUEST_BYTES = 64 * 1024
# Env-overridable (from the trusted unit environment) mainly so tests can shrink it.
READ_TIMEOUT = float(os.environ.get("REVIEW_BOT_READ_TIMEOUT", "30"))

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
HARNESSES = ("claude", "codex")
BARS = ("", "low", "medium", "high")
FOCUS_CAP = 2000

ALLOWED_FIELDS = {
    "mode",
    "owner",
    "repo",
    "number",
    "harness",
    "depth",
    "confidence_bar",
    "focus",
    "print_only",
    "dry_run",
}


class RequestError(Exception):
    """The request is malformed — reported to the caller, never executed."""


class ReviewFailure(Exception):
    """The pipeline aborted (review.py would have sys.exit'd) — reported as ok=false."""


def log(msg):
    print(f"review-bot-serve: {msg}", file=sys.stderr)


def emit(proto, event):
    proto.write(json.dumps(event) + "\n")
    proto.flush()


def load_review_module():
    spec = importlib.util.spec_from_file_location("review_bot_review", REVIEW_IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log_peercred():
    """Best-effort audit line: who connected. stdin is the connection socket under
    Accept=yes; in tests it's a pipe and SO_PEERCRED is simply unavailable."""
    try:
        s = socket.socket(fileno=os.dup(sys.stdin.fileno()))
    except OSError:
        return  # not a socket (pipe/tty) — nothing to audit
    try:
        data = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", data)
        log(f"connection from uid={uid} gid={gid} pid={pid}")
    except OSError:
        pass
    finally:
        s.close()


def read_request_line():
    """Read one line (<= 64 KiB) from stdin with a deadline. Works on both a socket
    (service) and a pipe (tests); anything after the first newline is ignored."""
    fd = sys.stdin.fileno()
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    deadline = time.monotonic() + READ_TIMEOUT
    buf = b""
    try:
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not sel.select(remaining):
                raise RequestError(f"timed out after {READ_TIMEOUT:g}s waiting for the request line")
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF — treat what we have as the request
                break
            buf += chunk
            if len(buf) > MAX_REQUEST_BYTES:
                raise RequestError(f"request too large (limit {MAX_REQUEST_BYTES} bytes)")
    finally:
        sel.close()
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        raise RequestError("empty request")
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        raise RequestError("request is not valid UTF-8")


def _req_name(req, key):
    v = req.get(key)
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise RequestError(f"'{key}' is required and must match [A-Za-z0-9_.-]+")
    return v


def _req_enum(req, key, allowed, default):
    v = req.get(key, default)
    if not isinstance(v, str) or v not in allowed:
        shown = ", ".join(repr(a) for a in allowed)
        raise RequestError(f"'{key}' must be one of {shown}")
    return v


def _req_bool(req, key):
    v = req.get(key, False)
    if not isinstance(v, bool):
        raise RequestError(f"'{key}' must be a boolean")
    return v


def parse_request(line, review):
    """Validate the whitelisted request against the same enums review.py's argparse
    enforces; return an argparse-shaped namespace ready for do_pr_review/do_issue_triage."""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as e:
        raise RequestError(f"request is not valid JSON: {e}")
    if not isinstance(req, dict):
        raise RequestError("request must be a single JSON object")

    unknown = sorted(set(req) - ALLOWED_FIELDS)
    if unknown:
        raise RequestError(f"unknown request field(s): {', '.join(unknown)}")

    mode = _req_enum(req, "mode", ("pr", "issue"), "pr")
    owner = _req_name(req, "owner")
    repo = _req_name(req, "repo")

    number = req.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise RequestError("'number' is required and must be a positive integer")

    harness = req.get("harness", "claude")
    if not isinstance(harness, str):
        raise RequestError("'harness' must be a string")
    harnesses = [h.strip() for h in harness.split(",") if h.strip()]
    if not harnesses or any(h not in HARNESSES for h in harnesses):
        raise RequestError(f"'harness' must be a comma-list of {', '.join(HARNESSES)}")

    depth = _req_enum(req, "depth", tuple(review.BAR_BY_DEPTH), "standard")
    confidence_bar = _req_enum(req, "confidence_bar", BARS, "")

    focus = req.get("focus", "")
    if not isinstance(focus, str):
        raise RequestError("'focus' must be a string")
    focus = focus[:FOCUS_CAP]

    args = argparse.Namespace(
        mode=mode,
        owner=owner,
        repo=repo,
        pr=number if mode == "pr" else None,
        issue=number if mode == "issue" else None,
        harness=",".join(harnesses),
        depth=depth,
        focus=focus,
        confidence_bar=confidence_bar,
        repo_dir="",  # deliberately not caller-settable
        dry_run=_req_bool(req, "dry_run"),
        print_only=_req_bool(req, "print_only"),
    )
    bar = confidence_bar or review.BAR_BY_DEPTH[depth]
    focus = focus.strip() or "(none provided)"
    return args, harnesses, bar, focus


def _die_to_exception(msg, code=1):
    raise ReviewFailure(str(msg))


def main():
    proto = sys.stdout  # the protocol channel — nothing else may write here
    log_peercred()

    review = load_review_module()
    # Route review.py's fatal-error path (die -> sys.exit) into an exception we can
    # report as a result event instead of dropping the connection with no answer.
    review.die = _die_to_exception

    try:
        line = read_request_line()
        args, harnesses, bar, focus = parse_request(line, review)
    except RequestError as e:
        emit(proto, {"type": "result", "ok": False, "markdown": None, "url": None, "error": str(e)})
        log(f"rejected request: {e}")
        sys.exit(1)

    num = args.pr if args.mode == "pr" else args.issue
    emit(
        proto,
        {
            "type": "log",
            "message": f"{args.mode} {args.owner}/{args.repo}#{num} "
            f"(harness={args.harness} depth={args.depth} bar={bar})",
        },
    )

    ok, markdown, url, error = False, None, None, None
    auth = None
    try:
        token = review.load_token()
        auth = review.GitAuth(token)
        # The pipeline's own prints (print_only markdown / posted URL) belong to the
        # direct CLI; here stdout is the protocol channel, so shunt them to the journal.
        with contextlib.redirect_stdout(sys.stderr):
            if args.mode == "issue":
                res = review.do_issue_triage(args, harnesses, bar, focus, token, auth)
            else:
                res = review.do_pr_review(args, harnesses, bar, focus, token, auth)
        if res is not None:  # None ⇔ dry_run (nothing generated)
            markdown, url = res
        ok = True
    except ReviewFailure as e:
        error = str(e)
        log(f"review failed: {error}")
    except Exception as e:  # never drop the connection without a result line
        error = f"internal error: {e.__class__.__name__}: {e}"
        log(error)
    finally:
        if auth is not None:
            auth.cleanup()

    emit(proto, {"type": "result", "ok": ok, "markdown": markdown, "url": url, "error": error})
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

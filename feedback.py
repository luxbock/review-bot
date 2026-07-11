#!@PYTHON@
"""review-bot-feedback — fetch review-bot's already-posted feedback for a PR/issue (issue #2).

A standalone READ command: after review-bot comments on a PR (or triages an issue),
this pulls that feedback back programmatically via the Forgejo REST API and prints it.

A pure read needs only a forge READ token — NOT the LLM credentials and NOT the engine
socket. So it speaks REST directly (reusing the same ~30-line api/api_paged plumbing that
poll.py duplicates) and ships as its OWN bin. It deliberately does NOT route through
review-bot-serve: that would make a cheap read block behind the service's MaxConnections=1
engine slot. It is READ-ONLY — it never posts, labels, or closes anything.

CLI:
  review-bot-feedback --owner O --repo R (--pr N | --issue N)
                      [--json|--markdown] [--all] [--kind review,triage,parked]

PRs and issues share the SAME comments endpoint (repos/{owner}/{repo}/issues/{n}/comments),
so one code path serves both. A comment is review-bot's iff its author login is in the
handle set (default review-bot, review_bot; env REVIEW_BOT_HANDLES, matching poll.py).
Each matched comment is classified by footer marker into a `kind`.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import NoReturn

FORGE_URL = os.environ.get("FORGEJO_URL", "http://10.0.150.1:3000").rstrip("/")
TOKEN_FILE_ENV = os.environ.get("REVIEW_BOT_TOKEN_FILE", "")
# Standard candidates, mirroring review.py:63-67.
TOKEN_FILE_CANDIDATES = [
    TOKEN_FILE_ENV,
    "/home/agent/.config/review-bot/token",
    os.path.expanduser("~/.config/review-bot/token"),
]

# review-bot's own handle spellings — same source/default as poll.py's HANDLE_ALIASES.
HANDLE_ALIASES = [h for h in re.split(r"[\s,]+", os.environ.get("REVIEW_BOT_HANDLES", "review-bot review_bot")) if h]

# Footer markers, matching review.py's render footers and poll.py's markers. A matched
# comment is classified by which (if any) marker its body contains.
REVIEW_MARKER = "Automated review by **review-bot**"
TRIAGE_MARKER = "Automated triage by **review-bot**"
PARK_MARKER = "review-bot — parked"

KINDS = ("review", "triage", "parked", "other")


def die(msg, code=1) -> NoReturn:
    print(f"review-bot-feedback: error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── Forgejo REST (READ only; token in the Authorization header, never fj) ──────
# Duplicated from review.py/poll.py deliberately: the repo is stdlib-only and
# low-coupling, and poll.py already carries its own copy rather than sharing a module.
def api(method, path, token, data=None):
    url = f"{FORGE_URL}/api/v1/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        hint = ""
        if e.code in (401, 403):
            hint = (
                " — the token may lack repo read access, or its scope is wrong "
                "(any token that can READ the repo works). "
            )
        die(f"{method} {path} -> HTTP {e.code}{hint}\n{detail}")
    except urllib.error.URLError as e:
        die(f"{method} {path} -> {e.reason} (is {FORGE_URL} reachable from here?)")


def api_paged(path, token):
    """GET every page of a list endpoint (Forgejo caps a single page, so a long thread
    would otherwise be silently truncated to page 1)."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        chunk = api("GET", f"{path}{sep}page={page}&limit=50", token)
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 50:
            break
        page += 1
    return out


def load_token():
    """FORGEJO_TOKEN env -> REVIEW_BOT_TOKEN_FILE / standard candidates -> error."""
    env_tok = os.environ.get("FORGEJO_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for cand in TOKEN_FILE_CANDIDATES:
        if cand and os.path.isfile(cand):
            with open(cand) as f:
                tok = f.read().strip()
            if tok:
                return tok
    die(
        "no forge token found. Set FORGEJO_TOKEN, or point REVIEW_BOT_TOKEN_FILE at a "
        "token file (also tried: "
        + ", ".join(c for c in TOKEN_FILE_CANDIDATES[1:] if c)
        + "). Any token that can READ the repo works."
    )


def classify(body):
    """Footer marker -> kind. Order matters: a parked notice has no review/triage marker."""
    if REVIEW_MARKER in body:
        return "review"
    if TRIAGE_MARKER in body:
        return "triage"
    if PARK_MARKER in body:
        return "parked"
    return "other"


def comment_sort_key(c):
    """Newest-first ordering key: created_at primary, id as a tiebreak/fallback."""
    return (c.get("created_at") or "", c.get("id") or 0)


def to_envelope_entry(c):
    return {
        "id": c.get("id"),
        "html_url": c.get("html_url"),
        "created_at": c.get("created_at"),
        "author": c.get("user", {}).get("login", ""),
        "kind": classify(c.get("body") or ""),
        "body_markdown": c.get("body") or "",
    }


def main():
    ap = argparse.ArgumentParser(
        description="Fetch review-bot's already-posted feedback for a Forgejo PR or issue (read-only).",
    )
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, help="PR number")
    ap.add_argument("--issue", type=int, help="issue number")
    out_fmt = ap.add_mutually_exclusive_group()
    out_fmt.add_argument("--json", dest="as_json", action="store_true", help="emit the JSON envelope (default)")
    out_fmt.add_argument("--markdown", action="store_true", help="print just the latest matched comment's markdown body")
    ap.add_argument("--all", dest="show_all", action="store_true", help="include every matched comment, newest-first")
    ap.add_argument("--kind", default="", help="comma-list filter over the classification (default: all kinds)")
    args = ap.parse_args()

    # Target resolution mirrors review.py main(): --pr/--issue, or infer from which is given.
    # PR and issue comments share the SAME endpoint, so one path serves both.
    if args.pr is not None and args.issue is not None:
        die("give only one of --pr / --issue")
    if args.pr is not None:
        target, number = "pr", args.pr
    elif args.issue is not None:
        target, number = "issue", args.issue
    else:
        die("a target is required: --pr N or --issue N")
    if number <= 0:
        die(f"--{target} must be a positive integer")

    # --kind filter over the classification.
    if args.kind.strip():
        want_kinds = {k.strip() for k in args.kind.split(",") if k.strip()}
        bad = want_kinds - set(KINDS)
        if bad:
            die(f"unknown --kind value(s): {', '.join(sorted(bad))} (valid: {', '.join(KINDS)})")
    else:
        want_kinds = set(KINDS)

    handles = {h.lower() for h in HANDLE_ALIASES if h}
    if not handles:
        die("no handles configured (REVIEW_BOT_HANDLES) — cannot identify review-bot's comments")

    token = load_token()
    comments = api_paged(f"repos/{args.owner}/{args.repo}/issues/{number}/comments", token)

    # review-bot's comments iff the author login is in the handle set, then filter by kind.
    matched = []
    for c in comments:
        author = (c.get("user", {}).get("login", "") or "").lower()
        if author not in handles:
            continue
        if classify(c.get("body") or "") not in want_kinds:
            continue
        matched.append(c)

    if not matched:
        kind_note = "" if want_kinds == set(KINDS) else f" matching --kind {args.kind}"
        die(
            f"no review-bot feedback found on {args.owner}/{args.repo} {target} #{number}"
            f"{kind_note}. (review-bot may not have commented yet.)",
            code=2,
        )

    # Newest-first; "latest" is the most recent matched comment.
    matched.sort(key=comment_sort_key, reverse=True)
    latest = matched[0]

    if args.markdown:
        print(latest.get("body") or "")
        return

    envelope = {
        "repo": f"{args.owner}/{args.repo}",
        "number": number,
        "target": target,
        "latest": to_envelope_entry(latest),
    }
    if args.show_all:
        envelope["all"] = [to_envelope_entry(c) for c in matched]
    print(json.dumps(envelope, indent=2))


if __name__ == "__main__":
    main()

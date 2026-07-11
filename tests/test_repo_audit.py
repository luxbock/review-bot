#!/usr/bin/env python3
"""Acceptance tests for `--mode repo` — the whole-repo maintainability audit that files
ONE prioritized issue. Stdlib only; NO live engine, NO live forge.

The engine is stubbed via REVIEW_BOT_CLAUDE_CMD pointing at a tiny script that echoes a
canned audit JSON (the code shlex-splits that env var). The forge is stubbed either by
monkeypatching review.api (to capture the create-issue POST) or not touched at all
(dry-run / print-only). serve.py is loaded through the same import mechanism it uses at
runtime and its parse_request is exercised directly.

Run:  python3 tests/test_repo_audit.py
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── module loading (mirrors serve.load_review_module) ──────────────────────────
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wire_review(review):
    """review.py ships with build-time @...@ placeholders. Point them at real values so the
    module is runnable in-tree without a nix build."""
    review.GIT = shutil.which("git")
    review.REVIEW_PROMPT_FILE = os.path.join(REPO_ROOT, "review-prompt.md")
    review.VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "verify-prompt.md")
    review.SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "synthesis-prompt.md")
    review.TRIAGE_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-prompt.md")
    review.TRIAGE_VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-verify-prompt.md")
    review.TRIAGE_SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-synthesis-prompt.md")
    review.AUDIT_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-prompt.md")
    review.AUDIT_VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-verify-prompt.md")
    review.AUDIT_SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-synthesis-prompt.md")
    return review


# Canned audit JSON with several ranked findings, wrapped in the `claude -p --output-format
# json` envelope (result is a string containing the audit JSON) so the real parser path runs.
CANNED_AUDIT = {
    "summary": "Two modules duplicate the request-parsing logic and one helper is dead.",
    "findings": [
        {
            "file": "client.py", "line_start": 40, "line_end": 79,
            "severity": "major", "confidence": "high",
            "title": "duplication: request validation copied from serve.py",
            "rationale": "client.build_request and serve.parse_request re-implement the same mode/harness checks.",
            "suggestion": "Extract a shared validate_request() helper.",
        },
        {
            "file": "review.py", "line_start": 358, "line_end": 391,
            "severity": "minor", "confidence": "medium",
            "title": "dead code: find_json_object fallback branch never reached",
            "rationale": "grep shows the fenced-block path always matches first in practice.",
            "suggestion": "",
        },
        {
            "file": "poll.py", "line_start": 1, "line_end": 1,
            "severity": "blocker", "confidence": "high",
            "title": "layering drift: poller imports server internals",
            "rationale": "poll.py reaches into serve.py private helpers, crossing the client boundary.",
            "suggestion": "Route through the client protocol instead.",
        },
    ],
}


def make_stub_engine(tmpdir, payload):
    """Write a tiny executable that echoes `payload` as the claude-json envelope on stdout."""
    envelope = json.dumps({"result": json.dumps(payload)})
    path = os.path.join(tmpdir, "stub-engine.py")
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n")
        f.write("import sys\n")
        f.write("sys.stdin.read()\n")  # consume the prompt like a real engine
        f.write("sys.stdout.write(%r)\n" % envelope)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return path


class _Args:
    """argparse.Namespace-alike for do_repo_audit."""

    def __init__(self, **kw):
        self.owner = "acme"
        self.repo = "widget"
        self.mode = "repo"
        self.pr = None
        self.issue = None
        self.depth = "quick"  # quick => no verify pass, single engine call
        self.dry_run = False
        self.print_only = False
        self.repo_dir = ""
        self.__dict__.update(kw)


def fresh_review():
    """Load a FRESH review module (so REVIEW_BOT_CLAUDE_CMD, set by the caller before this,
    is re-read) and wire its placeholders."""
    review = load_module("review_audit_test", os.path.join(REPO_ROOT, "review.py"))
    return wire_review(review)


# A checked-out repo tree the audit "explores". We stub prepare_head_checkout so no git
# fetch happens, but the engine cwd must exist.
@contextlib.contextmanager
def fake_checkout(review, cdir, head="abc123def456"):
    orig = review.prepare_head_checkout
    review.prepare_head_checkout = lambda owner, repo, db, auth, repo_dir=None: (cdir, head)
    try:
        yield
    finally:
        review.prepare_head_checkout = orig


# ── tests ──────────────────────────────────────────────────────────────────────
def test_dry_run_emits_prompt_runs_nothing():
    """1. --dry-run emits the filled audit prompt to stderr and runs no engine / posts nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        # Deliberately point the engine at a command that would FAIL if invoked, proving
        # dry-run never runs it.
        os.environ["REVIEW_BOT_CLAUDE_CMD"] = os.path.join(tmp, "does-not-exist")
        review = fresh_review()
        posted = []
        review.api = lambda *a, **k: posted.append((a, k)) or {"default_branch": "main"}

        args = _Args(dry_run=True)
        cdir = os.path.join(tmp, "checkout")
        os.makedirs(cdir)
        with fake_checkout(review, cdir):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = review.do_repo_audit(args, ["claude"], "medium", "(none provided)", "tok", auth=None)
        text = err.getvalue()
        assert res is None, "dry-run should return None"
        assert "DRY RUN" in text, "dry-run should print the engine invocation"
        # The filled audit prompt must appear (placeholders substituted, not literal).
        assert "whole-repository maintainability audit" in text, "audit prompt body missing"
        assert "acme/widget" in text, "REPO placeholder not filled"
        assert "{{REPO}}" not in text and "{{DEFAULT_BRANCH}}" not in text, "unfilled placeholder leaked"
        # Only the GET repo-meta api call is allowed; no POST.
        assert all(a[0][0] == "GET" for a in posted), f"dry-run must not POST: {posted}"
    print("ok  1. dry-run emits filled prompt, runs no engine, posts nothing")


def test_print_only_renders_no_post():
    """2. --print-only with the stubbed engine: render_audit_markdown produces the expected
    title + severity-ordered body, and NO POST occurs."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_BOT_CLAUDE_CMD"] = sys.executable + " " + make_stub_engine(tmp, CANNED_AUDIT)
        review = fresh_review()
        calls = []

        def fake_api(method, path, token, data=None):
            calls.append((method, path))
            if method == "GET" and path == "repos/acme/widget":
                return {"default_branch": "main"}
            raise AssertionError(f"unexpected api call in print-only: {method} {path}")

        review.api = fake_api

        args = _Args(print_only=True)
        cdir = os.path.join(tmp, "checkout")
        os.makedirs(cdir)
        out = io.StringIO()
        with fake_checkout(review, cdir):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                markdown, url = review.do_repo_audit(args, ["claude"], "medium", "(none provided)", "tok", auth=None)

        assert url is None, "print-only must not return a posted url"
        # No POST happened (only the GET repo-meta).
        assert all(m == "GET" for m, _ in calls), f"print-only must not POST: {calls}"
        # Title convention.
        assert markdown.startswith("## 🤖 review-bot audit — acme/widget maintainability findings"), markdown[:120]
        # Severity ordering: blocker before major before minor in the rendered body.
        b = markdown.index("layering drift")   # blocker finding
        j = markdown.index("request validation")  # major finding
        n = markdown.index("find_json_object")  # minor finding
        assert b < j < n, f"findings not severity-ordered (blocker<major<minor): {b},{j},{n}"
        assert "Automated audit by **review-bot**" in markdown, "audit footer marker missing"
        # stdout got the markdown (print-only contract).
        assert "review-bot audit" in out.getvalue()
    print("ok  2. print-only renders severity-ordered body, no POST")


def test_create_issue_path():
    """3. Create-issue path: verify the tool POSTs to .../issues (create issue) with the
    title/labels convention and rendered body — NOT to .../issues/{n}/comments."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_BOT_CLAUDE_CMD"] = sys.executable + " " + make_stub_engine(tmp, CANNED_AUDIT)
        review = fresh_review()
        calls = []

        def fake_api(method, path, token, data=None):
            calls.append((method, path, data))
            if method == "GET" and path == "repos/acme/widget":
                return {"default_branch": "main"}
            if method == "GET" and path.startswith("repos/acme/widget/issues?"):
                return []  # no existing audit issue
            if method == "POST" and path == "repos/acme/widget/issues":
                return {"html_url": "http://forge/acme/widget/issues/42", "number": 42}
            raise AssertionError(f"unexpected api call: {method} {path}")

        review.api = fake_api

        args = _Args(print_only=False)
        cdir = os.path.join(tmp, "checkout")
        os.makedirs(cdir)
        with fake_checkout(review, cdir):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                markdown, url = review.do_repo_audit(args, ["claude"], "medium", "(none provided)", "tok", auth=None)

        assert url == "http://forge/acme/widget/issues/42", url
        posts = [c for c in calls if c[0] == "POST"]
        assert len(posts) == 1, f"expected exactly one POST, got {posts}"
        method, path, data = posts[0]
        assert path == "repos/acme/widget/issues", f"must POST to create-issue, not: {path}"
        assert not path.endswith("/comments"), "must NOT post to the comments endpoint"
        assert data["title"] == "review-bot audit: acme/widget maintainability findings", data["title"]
        assert data["labels"] == ["audit", "review-bot"], data.get("labels")
        assert data["body"] == markdown, "posted body must be the rendered markdown"
        assert data["body"].startswith("## 🤖 review-bot audit"), data["body"][:80]
    print("ok  3. create-issue POSTs to /issues with title+labels+body, not /comments")


def test_create_issue_label_fallback_and_supersede():
    """3b. If create-with-labels fails, retry label-free; and a prior audit issue is linked."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_BOT_CLAUDE_CMD"] = sys.executable + " " + make_stub_engine(tmp, CANNED_AUDIT)
        review = fresh_review()
        calls = []

        def fake_api(method, path, token, data=None):
            calls.append((method, path, data))
            if method == "GET" and path == "repos/acme/widget":
                return {"default_branch": "main"}
            if method == "GET" and path.startswith("repos/acme/widget/issues?"):
                # one existing open audit issue (matched by title prefix)
                return [{"number": 7, "title": "review-bot audit: acme/widget maintainability findings", "labels": []}]
            if method == "POST" and path == "repos/acme/widget/issues":
                if data.get("labels"):
                    review.die("label 'audit' does not exist")  # simulate forge rejecting labels
                return {"html_url": "http://forge/acme/widget/issues/43", "number": 43}
            raise AssertionError(f"unexpected api call: {method} {path}")

        review.api = fake_api
        args = _Args(print_only=False)
        cdir = os.path.join(tmp, "checkout")
        os.makedirs(cdir)
        with fake_checkout(review, cdir):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                markdown, url = review.do_repo_audit(args, ["claude"], "medium", "(none provided)", "tok", auth=None)

        assert url == "http://forge/acme/widget/issues/43", url
        posts = [c for c in calls if c[0] == "POST"]
        assert len(posts) == 2, f"expected a labeled POST then a label-free retry, got {posts}"
        assert posts[0][2].get("labels") == ["audit", "review-bot"]
        assert "labels" not in posts[1][2], "retry must drop labels"
        assert "Supersedes #7" in markdown, "prior audit issue must be linked"
    print("ok  3b. label failure falls back to label-free; prior audit issue linked (Supersedes)")


def test_serve_parse_request_repo_numberless():
    """4. serve.parse_request ACCEPTS {"mode":"repo",owner,repo} with NO number; still rejects
    an unknown field; still rejects a number on a repo request; still accepts pr with number."""
    review = fresh_review()
    serve = load_module("serve_audit_test", os.path.join(REPO_ROOT, "serve.py"))

    # repo mode, no number -> accepted, pr/issue both None.
    line = json.dumps({"mode": "repo", "owner": "acme", "repo": "widget"})
    args, harnesses, bar, focus = serve.parse_request(line, review)
    assert args.mode == "repo" and args.pr is None and args.issue is None, vars(args)
    assert harnesses == ["claude"], harnesses

    # unknown field is still a hard error.
    try:
        serve.parse_request(json.dumps({"mode": "repo", "owner": "a", "repo": "b", "bogus": 1}), review)
        raise AssertionError("unknown field should be rejected")
    except serve.RequestError as e:
        assert "bogus" in str(e), e

    # a number on a repo request is rejected (numberless discipline).
    try:
        serve.parse_request(json.dumps({"mode": "repo", "owner": "a", "repo": "b", "number": 3}), review)
        raise AssertionError("number on repo mode should be rejected")
    except serve.RequestError as e:
        assert "number" in str(e), e

    # pr mode still REQUIRES a positive number (unchanged discipline).
    try:
        serve.parse_request(json.dumps({"mode": "pr", "owner": "a", "repo": "b"}), review)
        raise AssertionError("pr mode without number should be rejected")
    except serve.RequestError:
        pass
    args2, _, _, _ = serve.parse_request(json.dumps({"mode": "pr", "owner": "a", "repo": "b", "number": 5}), review)
    assert args2.pr == 5 and args2.issue is None, vars(args2)
    print("ok  4. serve.parse_request: repo numberless accepted; unknown field & stray number rejected")


def main():
    tests = [
        test_dry_run_emits_prompt_runs_nothing,
        test_print_only_renders_no_post,
        test_create_issue_path,
        test_create_issue_label_fallback_and_supersede,
        test_serve_parse_request_repo_numberless,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

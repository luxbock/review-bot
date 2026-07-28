#!/usr/bin/env python3
"""In-use acceptance tests for empty finder drafts and rendered provenance.

Stdlib only; no live engine and no live Forgejo. Each case drives the real review or
audit pipeline over a real Git worktree. REVIEW_BOT_CLAUDE_CMD points to a subprocess
stub that returns a configured sequence of engine results and records every invocation.

Run:  python3 tests/test_empty_draft.py
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMPTY_FINDER_DISCLOSURE = (
    "⚠️ The finder stage returned no findings, so nothing was verified — "
    "this reports an empty finder, not a verified-clean diff."
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wire_review(review):
    """Replace build-time placeholders so review.py runs directly from the checkout."""
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


@contextlib.contextmanager
def scratch_dir():
    """Use WORKER_SCRATCH when provided, otherwise a normal temporary directory."""
    root = os.environ.get("WORKER_SCRATCH") or None
    if root:
        os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="review-bot-empty-draft-", dir=root)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_stub_engine(tmpdir, responses):
    """Return an executable engine stub plus its invocation-count file."""
    responses_path = os.path.join(tmpdir, "responses.json")
    count_path = os.path.join(tmpdir, "invocations.log")
    with open(responses_path, "w") as f:
        json.dump(responses, f)

    path = os.path.join(tmpdir, "stub-engine.py")
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n")
        f.write("import json, os, sys\n")
        f.write("prompt = sys.stdin.read()\n")
        f.write("with open(os.environ['REVIEW_BOT_TEST_RESPONSES']) as src:\n")
        f.write("    responses = json.load(src)\n")
        f.write("count_path = os.environ['REVIEW_BOT_TEST_COUNT']\n")
        f.write("try:\n")
        f.write("    with open(count_path) as src:\n")
        f.write("        index = sum(1 for _ in src)\n")
        f.write("except FileNotFoundError:\n")
        f.write("    index = 0\n")
        f.write("with open(count_path, 'a') as dst:\n")
        f.write("    dst.write(str(index + 1) + '\\n')\n")
        f.write("if index >= len(responses):\n")
        f.write("    raise SystemExit('unexpected extra engine invocation')\n")
        f.write("sys.stdout.write(json.dumps({'result': json.dumps(responses[index])}))\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)

    os.environ["REVIEW_BOT_TEST_RESPONSES"] = responses_path
    os.environ["REVIEW_BOT_TEST_COUNT"] = count_path
    os.environ["REVIEW_BOT_CLAUDE_CMD"] = sys.executable + " " + path
    return count_path


def invocation_count(path):
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def fresh_review():
    review = load_module("review_empty_draft_test", os.path.join(REPO_ROOT, "review.py"))
    return wire_review(review)


def make_git_tree(parent):
    """Create a real two-commit PR-shaped Git worktree; return tree/base/head."""
    wt = os.path.join(parent, "private-worktree")
    os.makedirs(wt)
    git = shutil.which("git")
    subprocess.run([git, "init", "-q", wt], check=True)
    with open(os.path.join(wt, "README.md"), "w") as f:
        f.write("# Widget\n\nKeep request handling explicit and tested.\n")
    src = os.path.join(wt, "src")
    os.makedirs(src)
    widget = os.path.join(src, "widget.py")
    with open(widget, "w") as f:
        f.write("def widget(value):\n    return value\n")
    subprocess.run([git, "-C", wt, "add", "."], check=True)
    subprocess.run(
        [git, "-C", wt, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "base"],
        check=True,
    )
    base = subprocess.check_output([git, "-C", wt, "rev-parse", "HEAD"], text=True).strip()
    with open(widget, "w") as f:
        f.write("def widget(value):\n    if value is None:\n        return 0\n    return value\n")
    subprocess.run([git, "-C", wt, "add", "."], check=True)
    subprocess.run(
        [git, "-C", wt, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "change widget"],
        check=True,
    )
    head = subprocess.check_output([git, "-C", wt, "rev-parse", "HEAD"], text=True).strip()
    return wt, base, head


class RecordingCheckout:
    def __init__(self, wt):
        self.wt = wt
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class _Auth:
    def env(self):
        return dict(os.environ)


class _Args:
    def __init__(self, mode="pr", **kw):
        self.owner = "acme"
        self.repo = "widget"
        self.mode = mode
        self.pr = 7 if mode == "pr" else None
        self.issue = None
        self.depth = "standard"
        self.dry_run = False
        self.print_only = True
        self.repo_dir = ""
        self.__dict__.update(kw)


def run_pr_case(tmpdir, responses):
    count_path = make_stub_engine(tmpdir, responses)
    review = fresh_review()
    wt, base, head = make_git_tree(tmpdir)
    checkout = RecordingCheckout(wt)
    review.api = lambda method, path, token, data=None: {
        "merged": False,
        "base": {"ref": "main"},
        "head": {"sha": head},
    }

    def prepare(owner, repo, pr, base_ref, auth, repo_dir=None, expected_head=None):
        assert expected_head == head
        return checkout, base

    review.prepare_checkout = prepare
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        markdown, url = review.do_pr_review(
            _Args(), ["claude"], "medium", "(none provided)", "tok", auth=_Auth()
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, invocation_count(count_path)


def run_repo_case(tmpdir, responses):
    count_path = make_stub_engine(tmpdir, responses)
    review = fresh_review()
    wt, _base, head = make_git_tree(tmpdir)
    checkout = RecordingCheckout(wt)
    review.api = lambda method, path, token, data=None: {"default_branch": "main"}
    review.prepare_head_checkout = (
        lambda owner, repo, default_branch, auth, repo_dir=None: (checkout, head)
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        markdown, url = review.do_repo_audit(
            _Args(mode="repo"), ["claude"], "medium", "(none provided)", "tok", auth=None
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, invocation_count(count_path)


EMPTY_REVIEW = {"verdict": "approve", "summary": "", "findings": []}
REALISTIC_DRAFT = {
    "verdict": "request_changes",
    "summary": "The null fallback masks invalid caller input and the new branch lacks a regression test.",
    "findings": [
        {
            "file": "src/widget.py", "line_start": 2, "line_end": 3,
            "severity": "major", "confidence": "high",
            "title": "None input is silently converted to a valid value",
            "rationale": "Returning zero erases the distinction between missing input and an actual zero.",
            "suggestion": "Reject None explicitly or preserve it for the caller.",
        },
        {
            "file": "src/widget.py", "line_start": 1, "line_end": 4,
            "severity": "minor", "confidence": "medium",
            "title": "The new fallback has no focused test",
            "rationale": "No test captures the intended behavior for None or zero.",
            "suggestion": "Add table-driven cases for None, zero, and a nonzero value.",
        },
    ],
}
DEGENERATE_DRAFT = {
    "verdict": "comment",
    "findings": [
        {
            "file": "src/widget.py", "severity": "minor", "confidence": "medium",
            "title": "Optional location and suggestion are absent",
            "rationale": "The renderer must tolerate omitted optional fields.",
        },
        {
            "file": "", "line_start": None, "line_end": None,
            "severity": "question", "confidence": "low", "title": "",
            "rationale": "", "suggestion": "",
        },
    ],
}
VERIFIED_ONE = {
    "verdict": "approve",
    "summary": "One lower-confidence concern remains advisory.",
    "findings": [REALISTIC_DRAFT["findings"][1]],
}
EMPTY_AUDIT = {"summary": "", "findings": []}


def test_empty_pr_skips_verify_and_discloses():
    with scratch_dir() as tmp:
        markdown, calls = run_pr_case(tmp, [EMPTY_REVIEW])
    assert calls == 1, calls
    expected = (
        "No blocking issues found at or above the **medium** confidence bar.\n\n"
        + EMPTY_FINDER_DISCLOSURE
    )
    assert expected in markdown, markdown
    assert "findings `claude 0→0`" in markdown
    feedback = load_module("feedback_empty_draft_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert feedback.classify(markdown) == "review"
    print("ok  1. empty PR draft: one call, explicit disclosure, footer, classification")


def test_two_findings_verified_to_zero():
    with scratch_dir() as tmp:
        markdown, calls = run_pr_case(tmp, [REALISTIC_DRAFT, EMPTY_REVIEW])
    assert calls == 2, calls
    assert "All 2 draft finding(s) were checked and dropped by the verification stage." in markdown
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "findings `claude 2→0`" in markdown
    print("ok  2. realistic two-finding draft: two calls, verified-drop disclosure")


def test_degenerate_two_findings_verified_to_one():
    with scratch_dir() as tmp:
        markdown, calls = run_pr_case(tmp, [DEGENERATE_DRAFT, VERIFIED_ONE])
    assert calls == 2, calls
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "draft finding(s) were checked and dropped" not in markdown
    assert "findings `claude 2→1`" in markdown
    assert "### Findings (1)" in markdown
    print("ok  3. degenerate optional fields render; two calls and one survivor")


def test_empty_repo_audit_skips_verify_and_has_footer():
    with scratch_dir() as tmp:
        markdown, calls = run_repo_case(tmp, [EMPTY_AUDIT])
    assert calls == 1, calls
    assert markdown.startswith("## 🤖 review-bot audit — acme/widget maintainability findings")
    assert "No maintainability findings at or above the **medium** confidence bar." in markdown
    assert "findings `claude 0→0`" in markdown
    print("ok  4. empty repo audit: one call and audit provenance footer")


def test_green_nonempty_verdict_keeps_body_and_footer():
    with scratch_dir() as tmp:
        markdown, calls = run_pr_case(tmp, [VERIFIED_ONE, VERIFIED_ONE])
    assert calls == 2, calls
    assert markdown.startswith("## 🤖 review-bot — ✅ no blocking issues")
    assert "### Findings (1)" in markdown
    assert "The new fallback has no focused test" in markdown
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "draft finding(s) were checked and dropped" not in markdown
    assert "findings `claude 1→1`" in markdown
    print("ok  5. green verdict with a finding retains body and gains footer only")


def main():
    tests = [
        test_empty_pr_skips_verify_and_discloses,
        test_two_findings_verified_to_zero,
        test_degenerate_two_findings_verified_to_one,
        test_empty_repo_audit_skips_verify_and_has_footer,
        test_green_nonempty_verdict_keeps_body_and_footer,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

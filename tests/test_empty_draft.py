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
        # A response carrying __raw_result__ is emitted verbatim as the claude envelope's
        # `result` string, which is how a prose (non-JSON) reply actually reaches review.py.
        f.write("resp = responses[index]\n")
        f.write("if isinstance(resp, dict) and '__raw_result__' in resp:\n")
        f.write("    sys.stdout.write(json.dumps({'result': resp['__raw_result__']}))\n")
        f.write("else:\n")
        f.write("    sys.stdout.write(json.dumps({'result': json.dumps(resp)}))\n")
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

    def prepare(
        owner, repo, pr, base_ref, auth, repo_dir=None,
        expected_head=None, recorded_merge_base=None,
    ):
        assert expected_head == head
        return checkout, base

    review.prepare_checkout = prepare
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        markdown, url = review.do_pr_review(
            _Args(), ["claude"], "medium", "(none provided)", "tok", auth=_Auth()
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, invocation_count(count_path), err.getvalue()


def run_issue_case(tmpdir, responses):
    count_path = make_stub_engine(tmpdir, responses)
    review = fresh_review()
    wt, _base, head = make_git_tree(tmpdir)
    checkout = RecordingCheckout(wt)
    ISSUE = {
        "number": 11, "title": "widget returns 0 for None",
        "body": "Passing None yields 0 instead of an error.",
        "user": {"login": "olli"}, "labels": [], "state": "open",
    }

    def fake_api(method, path, token, data=None):
        if path.endswith("/issues/11"):
            return ISSUE
        if path == "repos/acme/widget":
            return {"default_branch": "main"}
        raise AssertionError(f"unexpected api path: {path}")

    review.api = fake_api
    review.api_paged = lambda path, token: []
    review.prepare_head_checkout = (
        lambda owner, repo, default_branch, auth, repo_dir=None: (checkout, head)
    )
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        markdown, url = review.do_issue_triage(
            _Args(mode="issue", issue=11, pr=None), ["claude"], "medium",
            "(none provided)", "tok", auth=None,
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, invocation_count(count_path), err.getvalue()


def run_repo_case(tmpdir, responses):
    count_path = make_stub_engine(tmpdir, responses)
    review = fresh_review()
    wt, _base, head = make_git_tree(tmpdir)
    checkout = RecordingCheckout(wt)
    review.api = lambda method, path, token, data=None: {"default_branch": "main"}
    review.prepare_head_checkout = (
        lambda owner, repo, default_branch, auth, repo_dir=None: (checkout, head)
    )
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        markdown, url = review.do_repo_audit(
            _Args(mode="repo"), ["claude"], "medium", "(none provided)", "tok", auth=None
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, invocation_count(count_path), err.getvalue()


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
# A prose reply whose only balanced {...} is an unrelated fragment — here the schema the
# engine was quoting back. find_json_object scrapes it, normalize() defaults the absent
# verdict and findings, and the run renders as a clean review nobody performed.
DEGENERATE_PROSE = {
    "__raw_result__": (
        "I looked at the change. The schema I was asked to follow is:\n\n"
        '{"file": "<path>", "line_start": 1, "severity": "minor"}\n\n'
        "Overall the widget change reads fine to me."
    )
}


def empty_finder_diag(stderr):
    """The JSON review.py journals for an empty finder, or None when it did not fire."""
    prefix = "empty-finder diagnostic: "
    for line in stderr.splitlines():
        if prefix in line:
            return json.loads(line.split(prefix, 1)[1])
    return None


def test_empty_pr_skips_verify_and_discloses():
    with scratch_dir() as tmp:
        markdown, calls, _err = run_pr_case(tmp, [EMPTY_REVIEW])
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
        markdown, calls, _err = run_pr_case(tmp, [REALISTIC_DRAFT, EMPTY_REVIEW])
    assert calls == 2, calls
    assert "All 2 draft finding(s) were checked and dropped by the verification stage." in markdown
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "findings `claude 2→0`" in markdown
    print("ok  2. realistic two-finding draft: two calls, verified-drop disclosure")


def test_degenerate_two_findings_verified_to_one():
    with scratch_dir() as tmp:
        markdown, calls, _err = run_pr_case(tmp, [DEGENERATE_DRAFT, VERIFIED_ONE])
    assert calls == 2, calls
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "draft finding(s) were checked and dropped" not in markdown
    assert "findings `claude 2→1`" in markdown
    assert "### Findings (1)" in markdown
    print("ok  3. degenerate optional fields render; two calls and one survivor")


def test_empty_repo_audit_skips_verify_and_has_footer():
    with scratch_dir() as tmp:
        markdown, calls, err = run_repo_case(tmp, [EMPTY_AUDIT])
    assert calls == 1, calls
    assert markdown.startswith("## 🤖 review-bot audit — acme/widget maintainability findings")
    assert "No maintainability findings at or above the **medium** confidence bar." in markdown
    assert "findings `claude 0→0`" in markdown
    # The audit pipeline shares run_pipeline, so it gets the same diagnostic — but the
    # audit schema carries NO verdict (normalize_audit / AUDIT_SCHEMA_HINT), so a
    # verdict-keyed discriminator would call every clean audit a parse pathology and send
    # an operator after a bug that is not there.
    diag = empty_finder_diag(err)
    assert diag is not None and diag["mode"] == "repo", diag
    assert diag["parse"]["findings_kind"] == "list", diag
    assert diag["parse"]["verdict_present"] is False, diag
    assert "DEFAULTED" not in err and "ABSENT" not in err, err
    assert "genuine empty result" in err and "verdict None (n/a, the audit schema" in err, err
    print("ok  4. empty repo audit: diagnostic reads the audit schema, not the PR schema")


def test_green_nonempty_verdict_keeps_body_and_footer():
    with scratch_dir() as tmp:
        markdown, calls, err = run_pr_case(tmp, [VERIFIED_ONE, VERIFIED_ONE])
    assert calls == 2, calls
    assert markdown.startswith("## 🤖 review-bot — ✅ no blocking issues")
    assert "### Findings (1)" in markdown
    assert "The new fallback has no focused test" in markdown
    assert EMPTY_FINDER_DISCLOSURE not in markdown
    assert "draft finding(s) were checked and dropped" not in markdown
    assert "findings `claude 1→1`" in markdown
    # A non-empty finder journals nothing: the diagnostic marks a rare event, so it must
    # stay rare enough to be worth grepping for.
    assert empty_finder_diag(err) is None, err
    print("ok  5. green verdict with a finding retains body and gains footer only")


def test_genuine_empty_verdict_is_recorded_as_genuine():
    with scratch_dir() as tmp:
        _markdown, calls, err = run_pr_case(tmp, [EMPTY_REVIEW])
    assert calls == 1, calls
    diag = empty_finder_diag(err)
    assert diag is not None, err
    assert diag["harness"] == "claude" and diag["repair_retried"] is False, diag
    parse = diag["parse"]
    assert diag["mode"] == "pr", diag
    assert parse["path"] == "envelope-result", parse
    assert parse["verdict_present"] is True and parse["verdict_raw"] == "approve", parse
    assert "genuine empty result" in err and "DEFAULTED" not in err, err
    assert parse["findings_kind"] == "list" and parse["findings_len"] == 0, parse
    assert parse["keys"] == ["findings", "summary", "verdict"], parse
    # The excerpt is the untouched engine stdout — the claude envelope, escaping and all.
    assert diag["raw_excerpt"].startswith('{"result":') and "approve" in diag["raw_excerpt"], diag
    assert diag["raw_chars"] == len(diag["raw_excerpt"]), diag
    print("ok  6. genuine empty verdict: diagnostic records an explicit approve")


def test_degenerate_parse_renders_clean_but_is_recorded_as_defaulted():
    with scratch_dir() as tmp:
        markdown, calls, err = run_pr_case(tmp, [DEGENERATE_PROSE])
    assert calls == 1, calls  # the fragment parsed, so no repair retry fired
    # Indistinguishable from test 6 in the posted comment — same disclosure, same footer.
    assert EMPTY_FINDER_DISCLOSURE in markdown, markdown
    assert "findings `claude 0→0`" in markdown
    diag = empty_finder_diag(err)
    assert diag is not None, err
    assert diag["repair_retried"] is False, diag
    parse = diag["parse"]
    # …and fully distinguishable in the journal: neither review key was ever present.
    assert parse["verdict_present"] is False and parse["verdict_raw"] is None, parse
    assert parse["findings_kind"] == "missing" and parse["findings_len"] is None, parse
    assert parse["keys"] == ["file", "line_start", "severity"], parse
    assert "Overall the widget change reads fine" in diag["raw_excerpt"], diag
    assert "DEFAULTED — not a real result object" in err, err


GOOD_TRIAGE = {
    "disposition": "genuine-bug", "confidence": "high",
    "summary": "None is silently coerced to a valid value.",
    "assessment": "The new branch erases the caller's distinction between missing and zero.",
    "grounding": "src/widget.py:2-3", "recommended_action": "Reject None explicitly.",
}
# The triage analogue of DEGENERATE_PROSE: prose whose only balanced {...} is a fragment.
# normalize_triage posts a confident `needs-info` built from nothing.
DEGENERATE_TRIAGE_PROSE = {
    "__raw_result__": (
        "Looking at the issue, the relevant shape is:\n\n"
        '{"file": "src/widget.py", "line_start": 2}\n\n'
        "I think this is a real defect worth fixing."
    )
}


def defaulted_triage_diag(stderr):
    prefix = "defaulted-triage diagnostic: "
    for line in stderr.splitlines():
        if prefix in line:
            return json.loads(line.split(prefix, 1)[1])
    return None


def test_good_triage_journals_nothing():
    with scratch_dir() as tmp:
        markdown, calls, err = run_issue_case(tmp, [GOOD_TRIAGE, GOOD_TRIAGE])
    assert calls == 2, calls
    assert "🐛 genuine bug" in markdown, markdown
    assert defaulted_triage_diag(err) is None, err
    print("ok 10. a triage the engine actually produced journals nothing")


def test_defaulted_triage_disposition_is_journalled():
    """normalize_triage substitutes `needs-info` for an absent disposition, so a scraped
    fragment is posted as a confident triage nobody produced. Triage has no finder stage,
    so the empty-finder trigger cannot see it."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_issue_case(tmp, [DEGENERATE_TRIAGE_PROSE, GOOD_TRIAGE])
    # The posted comment is unchanged — this is the failure, and it is still invisible there.
    assert "review-bot triage" in markdown, markdown
    diag = defaulted_triage_diag(err)
    assert diag is not None, err
    assert diag["mode"] == "issue", diag
    assert diag["parse"]["disposition_present"] is False, diag
    assert diag["parse"]["keys"] == ["file", "line_start"], diag
    assert "I think this is a real defect" in diag["raw_excerpt"], diag
    assert "DEFAULTED TRIAGE" in err and "supplied none" in err, err
    print(f"ok 11. an absent triage disposition is journalled ({calls} engine calls)")


def test_unrecognised_triage_disposition_counts_as_defaulted():
    """Present-but-unrecognised is coerced to `needs-info` too, and posts just as confidently."""
    with scratch_dir() as tmp:
        bogus = dict(GOOD_TRIAGE, disposition="probably-fine")
        _markdown, _calls, err = run_issue_case(tmp, [bogus, bogus])
    diag = defaulted_triage_diag(err)
    assert diag is not None, err
    assert diag["parse"]["disposition_present"] is True, diag
    assert diag["parse"]["disposition_raw"] == "probably-fine", diag
    assert "supplied 'probably-fine'" in err, err
    print("ok 12. an unrecognised triage disposition is journalled, not silently coerced")


def test_genuine_check_is_mode_aware():
    """The one signal this diagnostic exists to give must not invert between modes."""
    review = fresh_review()
    audit_clean = {"findings_kind": "list", "verdict_present": False}
    pr_clean = {"findings_kind": "list", "verdict_present": True}
    scraped = {"findings_kind": "missing", "verdict_present": False}
    # An audit reply legitimately carries no verdict; a PR reply that lacks one was
    # defaulted into existence by normalize().
    assert review.empty_finder_is_genuine(audit_clean, "repo") is True
    assert review.empty_finder_is_genuine(audit_clean, "pr") is False
    assert review.empty_finder_is_genuine(pr_clean, "pr") is True
    # A missing findings list is never genuine, in either mode.
    assert review.empty_finder_is_genuine(scraped, "repo") is False
    assert review.empty_finder_is_genuine(scraped, "pr") is False
    assert review.empty_finder_is_genuine({}, "pr") is False
    print("ok  9. the genuine/defaulted check follows the schema of the mode that ran")
    print("ok  7. degenerate parse renders identically but is recorded as defaulted")


def test_raw_excerpt_clips_head_and_tail():
    review = fresh_review()
    long_text = "A" * 3000 + "MIDDLE" + "Z" * 3000
    clipped = review._clip(long_text, limit=100)
    assert clipped.startswith("A" * 50) and clipped.endswith("Z" * 50), clipped
    assert "MIDDLE" not in clipped and "5906 chars elided" in clipped, clipped
    assert review._clip("short", limit=100) == "short"
    print("ok  8. raw excerpt keeps both ends and states how much it dropped")


def main():
    tests = [
        test_empty_pr_skips_verify_and_discloses,
        test_two_findings_verified_to_zero,
        test_degenerate_two_findings_verified_to_one,
        test_empty_repo_audit_skips_verify_and_has_footer,
        test_green_nonempty_verdict_keeps_body_and_footer,
        test_genuine_empty_verdict_is_recorded_as_genuine,
        test_degenerate_parse_renders_clean_but_is_recorded_as_defaulted,
        test_raw_excerpt_clips_head_and_tail,
        test_good_triage_journals_nothing,
        test_defaulted_triage_disposition_is_journalled,
        test_unrecognised_triage_disposition_counts_as_defaulted,
        test_genuine_check_is_mode_aware,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

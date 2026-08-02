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
# The PR path always measures the diff, so a real review renders one of the two
# calibrated tiers; the flat line survives only for renders with no measurement.
SMALL_DISCLOSURE = (
    "0 findings on a small change (1 file, +2/-0) — an empty result is typical "
    "and consistent with a clean PR. Verification skipped: nothing to verify."
)
EMPTY_DISCLOSURE_TELLS = (
    "0 findings on a small change",
    "0 findings on a substantial change",
    "The finder stage returned no findings",
)


def assert_no_empty_disclosure(markdown):
    for tell in EMPTY_DISCLOSURE_TELLS:
        assert tell not in markdown, markdown


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
        + SMALL_DISCLOSURE
    )
    assert expected in markdown, markdown
    assert "findings `claude 0→0`" in markdown
    feedback = load_module("feedback_empty_draft_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert feedback.classify(markdown) == "review"
    print("ok  1. empty PR draft: one call, calibrated disclosure, footer, classification")


def test_two_findings_verified_to_zero():
    with scratch_dir() as tmp:
        markdown, calls, _err = run_pr_case(tmp, [REALISTIC_DRAFT, EMPTY_REVIEW])
    assert calls == 2, calls
    assert "All 2 draft finding(s) were checked and dropped by the verification stage." in markdown
    assert_no_empty_disclosure(markdown)
    assert "findings `claude 2→0`" in markdown
    print("ok  2. realistic two-finding draft: two calls, verified-drop disclosure")


def test_degenerate_two_findings_verified_to_one():
    with scratch_dir() as tmp:
        markdown, calls, _err = run_pr_case(tmp, [DEGENERATE_DRAFT, VERIFIED_ONE])
    assert calls == 2, calls
    assert_no_empty_disclosure(markdown)
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


def test_discarded_findings_entries_are_not_reported_as_genuine():
    """normalize* silently drops non-dict entries, so an engine CAN send a non-empty
    findings list and still reach zero drafts. That is normalize manufacturing the empty
    result — case 2 — and must not be reported as the engine answering clean."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_repo_case(
            tmp, [{"summary": "One concern.", "findings": ["unchecked index in parse_args"]}]
        )
    assert calls == 1, calls
    assert "findings `claude 0\u21920`" in markdown, markdown
    diag = empty_finder_diag(err)
    assert diag["parse"]["findings_kind"] == "list", diag
    assert diag["parse"]["findings_len"] == 1, diag
    assert "DEFAULTED — not a real result object" in err, err
    assert "genuine empty result" not in err, err
    # The length is the whole tell, so the human line must not hide it behind a bare `list`.
    assert "findings list of 1 — every entry discarded by normalize" in err, err
    print("ok  5. entries discarded by normalize are reported as defaulted, with the count")


def test_null_findings_counts_as_the_engine_answering_clean():
    """normalize*'s `obj.get("findings") or []` makes `null` indistinguishable from `[]`,
    and it is the one non-list shape an engine plausibly means as "no findings". Flagging
    it would put a false DEFAULTED on every clean audit that spells it that way."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_repo_case(
            tmp, [{"summary": "nothing found", "findings": None}]
        )
    assert calls == 1, calls
    assert "findings `claude 0\u21920`" in markdown, markdown
    diag = empty_finder_diag(err)
    assert diag["parse"]["findings_kind"] == "null", diag
    assert "genuine empty result" in err and "DEFAULTED" not in err, err
    print("ok  6. a null findings value reads as the engine answering, not as a pathology")


def test_green_nonempty_verdict_keeps_body_and_footer():
    with scratch_dir() as tmp:
        markdown, calls, err = run_pr_case(tmp, [VERIFIED_ONE, VERIFIED_ONE])
    assert calls == 2, calls
    assert markdown.startswith("## 🤖 review-bot — ✅ no blocking issues")
    assert "### Findings (1)" in markdown
    assert "The new fallback has no focused test" in markdown
    assert_no_empty_disclosure(markdown)
    assert "draft finding(s) were checked and dropped" not in markdown
    assert "findings `claude 1→1`" in markdown
    # A non-empty finder journals nothing: the diagnostic marks a rare event, so it must
    # stay rare enough to be worth grepping for.
    assert empty_finder_diag(err) is None, err
    print("ok  7. green verdict with a finding retains body and gains footer only")


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
    print("ok  8. genuine empty verdict: diagnostic records an explicit approve")


def test_verdict_only_approve_is_genuine_not_a_pathology():
    """{"verdict":"approve","summary":…} with the empty array left out is a real approve.
    Flagging it would send a reader after a parse bug that is not there."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_pr_case(
            tmp, [{"verdict": "approve", "summary": "Nothing to flag."}]
        )
    assert calls == 1, calls
    assert "findings `claude 0\u21920`" in markdown, markdown
    diag = empty_finder_diag(err)
    assert diag["parse"]["findings_kind"] == "missing", diag
    assert diag["parse"]["verdict_raw"] == "approve", diag
    assert "genuine empty result" in err and "DEFAULTED" not in err, err
    print("ok  9. a verdict-only approve counts as genuine, not as a parse pathology")


def test_scraped_fragment_triggers_repair_instead_of_a_clean_review():
    """The behaviour change. A fragment used to be normalized into a confident green
    review; it is now refused, and the repair retry recovers the engine's real answer."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_pr_case(
            tmp, [DEGENERATE_PROSE, REALISTIC_DRAFT, VERIFIED_ONE]
        )
    assert calls == 3, calls  # generate (refused) -> repair -> verify
    # The finder was never empty, so there is no green review to mistake for one.
    assert_no_empty_disclosure(markdown)
    assert "findings `claude 2→1`" in markdown, markdown
    assert "The new fallback has no focused test" in markdown, markdown
    assert "carrying none of verdict/findings" in err, err
    assert "refusing to normalize a fragment into a result" in err, err
    print("ok 10. a scraped fragment is refused and the repair retry recovers the answer")


def test_scraped_fragment_twice_aborts_rather_than_posting():
    """When the repair also fails there is no answer to post. Aborting is the honest
    outcome; poll.py turns the give-up into a visible notice, which silence never was."""
    with scratch_dir() as tmp:
        try:
            markdown, _calls, _err = run_pr_case(tmp, [DEGENERATE_PROSE, DEGENERATE_PROSE])
        except SystemExit as e:
            assert e.code == 1, e.code
        else:
            raise AssertionError(f"expected the review to abort, got: {markdown[:200]}")
    print("ok 11. two unusable replies abort the review instead of inventing a verdict")


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
    print("ok 13. a triage the engine actually produced journals nothing")


def test_triage_fragment_is_refused_not_posted_as_needs_info():
    """The triage half of the behaviour change. A fragment carrying no `disposition` used
    to be posted as a confident `needs-info`; it is now refused and repaired."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_issue_case(
            tmp, [DEGENERATE_TRIAGE_PROSE, GOOD_TRIAGE, GOOD_TRIAGE]
        )
    assert calls == 3, calls  # generate (refused) -> repair -> verify
    assert "\U0001f41b genuine bug" in markdown, markdown
    assert "needs-info" not in markdown, markdown
    assert "carrying none of disposition" in err, err
    # Nothing was defaulted in the end, so the triage diagnostic stays quiet.
    assert defaulted_triage_diag(err) is None, err
    print("ok 14. a triage fragment is refused rather than posted as a fabricated verdict")


def test_triage_fragment_at_the_verify_stage_is_refused_too():
    """Triage never skips verification and the verify result REPLACES the draft, so the
    fragment guard has to hold on that call as well — it is the one that gets rendered."""
    with scratch_dir() as tmp:
        markdown, calls, err = run_issue_case(
            tmp, [GOOD_TRIAGE, DEGENERATE_TRIAGE_PROSE, GOOD_TRIAGE]
        )
    assert calls == 3, calls  # generate -> verify (refused) -> repair
    assert "\U0001f41b genuine bug" in markdown, markdown
    assert "needs more info" not in markdown, markdown
    assert defaulted_triage_diag(err) is None, err
    print("ok 15. a fragment returned by the verify stage is refused too")


def test_disposition_defaulted_at_the_verify_stage_is_journalled():
    """An unrecognised disposition still carries the key, so the fragment guard passes it
    and normalize_triage coerces it to `needs-info`. The diagnostic must name the stage
    that produced the posted object — claiming the drafting call would be a false claim of
    exactly the kind this whole diagnostic exists to prevent."""
    with scratch_dir() as tmp:
        bogus = dict(GOOD_TRIAGE, disposition="probably-fine")
        markdown, calls, err = run_issue_case(tmp, [GOOD_TRIAGE, bogus])
    assert calls == 2, calls
    assert "\u2753 needs more info" in markdown, markdown
    diag = defaulted_triage_diag(err)
    assert diag is not None, err
    assert diag["mode"] == "issue", diag
    assert diag["stage"] == "verify" and diag["posted"] is True, diag
    assert diag["parse"]["disposition_present"] is True, diag
    assert diag["parse"]["disposition_raw"] == "probably-fine", diag
    assert "DEFAULTED TRIAGE (claude, verify stage)" in err, err
    assert "supplied 'probably-fine'" in err, err
    assert "this is the disposition being posted" in err, err
    print("ok 16. a disposition defaulted at the verify stage is journalled with its stage")


def test_genuine_check_is_mode_aware():
    """The one signal this diagnostic exists to give must not invert between modes."""
    review = fresh_review()
    empty_list = {"findings_kind": "list", "findings_len": 0,
                  "verdict_present": False, "verdict_raw": None}
    discarded = {"findings_kind": "list", "findings_len": 2,
                 "verdict_present": False, "verdict_raw": None}
    approve_with_discarded = {"findings_kind": "list", "findings_len": 1,
                              "verdict_present": True, "verdict_raw": "approve"}
    # The common shorthand: a real approve with the empty array left out.
    verdict_only = {"findings_kind": "missing", "findings_len": None,
                    "verdict_present": True, "verdict_raw": "approve"}
    # A quoted schema is present but is not an answer.
    schema_echo = {"findings_kind": "missing", "verdict_present": True,
                   "verdict_raw": "approve|comment|request_changes"}
    scraped = {"findings_kind": "missing", "verdict_present": False, "verdict_raw": None}
    # An explicit empty list is the universal tell — the audit schema has nothing else.
    assert review.empty_finder_is_genuine(empty_list, "repo") is True
    assert review.empty_finder_is_genuine(empty_list, "pr") is True
    # A recognised verdict stands alone in PR mode, but the audit schema carries none,
    # so it can never be the evidence there.
    assert review.empty_finder_is_genuine(verdict_only, "pr") is True
    assert review.empty_finder_is_genuine(verdict_only, "repo") is False
    # Present but not a value: checking presence alone would pass this.
    assert review.empty_finder_is_genuine(schema_echo, "pr") is False
    # Neither tell, in either mode.
    assert review.empty_finder_is_genuine(scraped, "repo") is False
    assert review.empty_finder_is_genuine(scraped, "pr") is False
    assert review.empty_finder_is_genuine({}, "pr") is False
    # A list that arrived non-empty is manufacturing, in either mode — and a real verdict
    # alongside discarded entries must not paper over them.
    assert review.empty_finder_is_genuine(discarded, "repo") is False
    assert review.empty_finder_is_genuine(discarded, "pr") is False
    assert review.empty_finder_is_genuine(approve_with_discarded, "pr") is False
    # `null` is an answer in either mode; the other falsy shapes normalize the same way
    # but are malformed, so they stay pathologies.
    null_findings = {"findings_kind": "null", "findings_len": None,
                     "verdict_present": False, "verdict_raw": None}
    assert review.empty_finder_is_genuine(null_findings, "repo") is True
    assert review.empty_finder_is_genuine(null_findings, "pr") is True
    for junk in ("dict", "str", "int"):
        assert review.empty_finder_is_genuine(
            {"findings_kind": junk, "verdict_present": False, "verdict_raw": None}, "repo"
        ) is False, junk
    print("ok 17. the genuine/defaulted check follows the schema of the mode that ran")


def test_disclosure_tiers_follow_diff_size():
    """The empty-verdict calibration: the tier boundary is inclusive on both knobs, the
    raw numbers always render, and a render with no measurement keeps the flat warning.
    The tiers only reword the disclosure — a tier change can never add findings."""
    review = fresh_review()
    prov = {"stages": [{"harness": "claude", "draft_count": 0, "surviving_count": 0}]}

    def render(stats):
        return review.render_markdown(
            {"verdict": "approve", "summary": "", "findings": []},
            ["claude"], "standard", "medium", "f" * 40,
            provenance={"stages": list(prov["stages"])}, diff_stats=stats,
        )

    at_boundary = render({"files": 5, "insertions": 150, "deletions": 50})
    assert "0 findings on a small change (5 files, +150/-50)" in at_boundary, at_boundary
    assert "⚠️" not in at_boundary, at_boundary

    over_files = render({"files": 6, "insertions": 10, "deletions": 0})
    assert "⚠️ 0 findings on a substantial change (6 files, +10/-0)" in over_files, over_files
    assert "not fully reviewed" in over_files, over_files

    over_lines = render({"files": 1, "insertions": 200, "deletions": 1})
    assert "substantial change (1 file, +200/-1)" in over_lines, over_lines

    unmeasured = render(None)
    assert "The finder stage returned no findings" in unmeasured, unmeasured
    print("ok 18. disclosure tier tracks the measured diff size; boundary is inclusive")


def test_raw_excerpt_clips_head_and_tail():
    review = fresh_review()
    long_text = "A" * 3000 + "MIDDLE" + "Z" * 3000
    clipped = review._clip(long_text, limit=100)
    assert clipped.startswith("A" * 50) and clipped.endswith("Z" * 50), clipped
    assert "MIDDLE" not in clipped and "5906 chars elided" in clipped, clipped
    assert review._clip("short", limit=100) == "short"
    print("ok 12. raw excerpt keeps both ends and states how much it dropped")


def test_normalize_report_stays_coupled_to_normalized_results():
    """Every default and discard reported by normalize* must agree with its output."""
    review = fresh_review()

    class FindingLike:
        """Non-dict with the methods normalization would use if its guard drifted."""

        def get(self, _key, default=None):
            return default

    rows = [
        ("review empty", review.normalize, "pr", {"verdict": "approve", "findings": []}),
        ("review null", review.normalize, "pr", {"verdict": "approve", "findings": None}),
        ("review verdict only", review.normalize, "pr", {"verdict": "approve"}),
        ("review schema echo", review.normalize, "pr", {
            "verdict": "approve|comment|request_changes",
        }),
        ("review two findings", review.normalize, "pr", {"findings": [{}, {}]}),
        ("review mapping-like non-dict", review.normalize, "pr", {
            "findings": [FindingLike()],
        }),
        ("review discarded", review.normalize, "pr", {"findings": [1, "bad"]}),
        ("review mixed", review.normalize, "pr", {"findings": [{}, None]}),
        ("review dict findings", review.normalize, "pr", {"findings": {}}),
        ("review string findings", review.normalize, "pr", {"findings": ""}),
        ("review integer findings", review.normalize, "pr", {"findings": 0}),
        ("audit empty", review.normalize_audit, "repo", {"findings": []}),
        ("audit mixed", review.normalize_audit, "repo", {"findings": [{}, False]}),
        ("triage engine", review.normalize_triage, "issue", {
            "disposition": "genuine-bug",
        }),
        ("triage absent", review.normalize_triage, "issue", {"summary": "unclear"}),
        ("triage unrecognised", review.normalize_triage, "issue", {
            "disposition": "probably-fine",
        }),
    ]
    report_fields = {
        "schema", "schema_has_verdict", "findings_source", "findings_received",
        "findings_discarded", "verdict_source", "disposition_source",
    }

    def findings_source(raw):
        if "findings" not in raw:
            return "absent"
        value = raw["findings"]
        if value is None:
            return "null"
        if isinstance(value, list):
            return "list"
        return f"non-list:{type(value).__name__}"

    for name, norm, mode, raw in rows:
        report = {}
        result = norm(raw, report)
        assert set(report) == report_fields, (name, report)
        expected_source = "n/a" if mode == "issue" else findings_source(raw)
        assert report["findings_source"] == expected_source, (name, report)
        if report["findings_source"] == "list":
            assert report["findings_received"] == len(raw["findings"]), (name, report)
            assert report["findings_discarded"] == (
                report["findings_received"] - len(result["findings"])
            ), (name, report, result)
        else:
            assert report["findings_received"] is None, (name, report)

        if mode == "pr":
            engine_verdict_survived = (
                "verdict" in raw and result["verdict"] == raw["verdict"]
            )
            assert (report["verdict_source"] == "engine") == engine_verdict_survived, (
                name, report, result,
            )
        else:
            assert report["verdict_source"] == "n/a", (name, report)

        if mode == "issue":
            engine_disposition_survived = (
                "disposition" in raw and result["disposition"] == raw["disposition"]
            )
            assert (
                report["disposition_source"] == "engine"
            ) == engine_disposition_survived, (name, report, result)
        else:
            assert report["disposition_source"] == "n/a", (name, report)

        legacy_parse = review._describe_parsed(raw, "coupling-table")
        report_parse = dict(legacy_parse, normalize_report=report)
        assert review.empty_finder_is_genuine(report_parse, mode) == (
            review.empty_finder_is_genuine(legacy_parse, mode)
        ), (name, legacy_parse, report)
    print("ok 19. normalize reports stay coupled to every normalized result")


def test_journal_never_calls_a_printed_verdict_absent():
    """The note distinguishes all three verdict states, from the report and from a
    pre-report journal alike. Folding `unrecognised` into the absent note made the line
    say `verdict 'approve|comment|request_changes' (ABSENT — defaulted)` — asserting
    absence about a value printed in the same breath, which is the class of unsupported
    claim this whole diagnostic exists to prevent."""
    review = fresh_review()

    def note_for(raw_verdict, verdict_present, with_report):
        parse = {"path": "envelope-result", "keys": ["verdict"],
                 "verdict_present": verdict_present, "verdict_raw": raw_verdict,
                 "findings_kind": "missing", "findings_len": None}
        if with_report:
            report = {}
            obj = {"verdict": raw_verdict} if verdict_present else {}
            review.normalize(obj, report)
            parse["normalize_report"] = report
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            review.log_empty_finder_diagnostic("claude", {"mode": "pr", "parse": parse})
        line = next(l for l in err.getvalue().splitlines() if "EMPTY FINDER" in l)
        return line[line.index("verdict "):line.index(", findings")]

    for with_report in (True, False):
        where = "report" if with_report else "legacy"
        recognised = note_for("approve", True, with_report)
        assert recognised == "verdict 'approve' (present)", (where, recognised)
        # The quoted schema an engine echoes back: present, not an answer, and the note
        # must not claim it was absent while quoting it.
        echoed = note_for("approve|comment|request_changes", True, with_report)
        assert echoed == (
            "verdict 'approve|comment|request_changes' "
            "(present but unrecognised — defaulted)"
        ), (where, echoed)
        assert "ABSENT" not in echoed, (where, echoed)
        missing = note_for(None, False, with_report)
        assert missing == "verdict None (ABSENT — defaulted)", (where, missing)


def main():
    tests = [
        test_empty_pr_skips_verify_and_discloses,
        test_two_findings_verified_to_zero,
        test_degenerate_two_findings_verified_to_one,
        test_empty_repo_audit_skips_verify_and_has_footer,
        test_discarded_findings_entries_are_not_reported_as_genuine,
        test_null_findings_counts_as_the_engine_answering_clean,
        test_green_nonempty_verdict_keeps_body_and_footer,
        test_genuine_empty_verdict_is_recorded_as_genuine,
        test_verdict_only_approve_is_genuine_not_a_pathology,
        test_scraped_fragment_triggers_repair_instead_of_a_clean_review,
        test_scraped_fragment_twice_aborts_rather_than_posting,
        test_raw_excerpt_clips_head_and_tail,
        test_good_triage_journals_nothing,
        test_triage_fragment_is_refused_not_posted_as_needs_info,
        test_triage_fragment_at_the_verify_stage_is_refused_too,
        test_disposition_defaulted_at_the_verify_stage_is_journalled,
        test_genuine_check_is_mode_aware,
        test_disclosure_tiers_follow_diff_size,
        test_normalize_report_stays_coupled_to_normalized_results,
        test_journal_never_calls_a_printed_verdict_absent,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

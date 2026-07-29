#!/usr/bin/env python3
"""Acceptance tests for the diff input-mode instrumentation (issue #21).

Covers, with NO live engine and NO live forge:
  * the inline/file-list cap boundary, tested EXACTLY (a diff of precisely
    DIFF_INLINE_CAP chars inlines; one char more does not);
  * the journal line `diff <N> chars vs cap <C> — <inlined|file-list only>`;
  * the rendered footer segment `diff `inlined|file-list`` and its position
    (after `findings`, before `merge-base`);
  * feedback.classify() still returning "review" for a rendered review body;
  * tools/finder_ab.py driven against a stub binary — every argv carries
    --print-only, the forced caps reach the runs, aborted runs are recorded,
    and the summary table counts empty drafts.

Run:  python3 tests/test_diff_mode.py
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def fresh_review():
    return wire_review(load_module("review_diff_mode_test", os.path.join(REPO_ROOT, "review.py")))


@contextlib.contextmanager
def scratch_dir():
    root = os.environ.get("WORKER_SCRATCH") or None
    if root:
        os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="review-bot-diff-mode-", dir=root)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _Auth:
    def env(self):
        e = dict(os.environ)
        e["GIT_CONFIG_NOSYSTEM"] = "1"
        return e


class _Args:
    def __init__(self, **kw):
        self.owner = "acme"
        self.repo = "widget"
        self.mode = "pr"
        self.pr = 7
        self.issue = None
        self.depth = "standard"
        self.dry_run = False
        self.print_only = True
        self.repo_dir = ""
        self.__dict__.update(kw)


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


# ── git fixture: a two-commit tree whose diff length we can dial exactly ───────
GIT = shutil.which("git")
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def _git(wt, *args):
    return subprocess.run(
        [GIT, "-C", wt, *args], check=True, capture_output=True, text=True
    ).stdout


def make_git_tree(parent, payload=1000):
    """Two commits touching one single-line file; return (worktree, base_sha)."""
    wt = os.path.join(parent, "private-worktree")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True)
    with open(os.path.join(wt, "payload.txt"), "w") as f:
        f.write("x\n")
    _git(wt, "add", ".")
    _git(wt, *GIT_ID, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD").strip()
    set_payload(wt, payload, amend=False)
    return wt, base


def set_payload(wt, n, amend=True):
    """Rewrite the head commit so the single changed line carries n 'x' characters."""
    with open(os.path.join(wt, "payload.txt"), "w") as f:
        f.write("x" * n + "\n")
    _git(wt, "add", ".")
    args = [*GIT_ID, "commit", "-q"]
    if amend:
        args.append("--amend")
    _git(wt, *args, "-m", "change payload")


def diff_len(wt, base):
    return len(_git(wt, "diff", f"{base}..HEAD"))


def tune_diff_to(wt, base, target):
    """Dial the payload until the diff is EXACTLY `target` chars long.

    The diff's fixed overhead (headers, hunk marker, the abbreviated blob hashes)
    is constant for a given tree, but the abbreviation length is git's business —
    so we converge on it instead of assuming it.
    """
    n = target // 2
    for _ in range(8):
        set_payload(wt, n)
        got = diff_len(wt, base)
        if got == target:
            return n
        n += target - got
        assert n > 0, "target is smaller than the diff's fixed overhead"
    raise AssertionError(f"could not dial the diff to exactly {target} chars")


# ── stub engine (same idiom as tests/test_empty_draft.py) ─────────────────────
def make_stub_engine(tmpdir, responses):
    responses_path = os.path.join(tmpdir, "responses.json")
    count_path = os.path.join(tmpdir, "invocations.log")
    with open(responses_path, "w") as f:
        json.dump(responses, f)
    path = os.path.join(tmpdir, "stub-engine.py")
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n")
        f.write("import json, os, sys\n")
        f.write("sys.stdin.read()\n")
        f.write("with open(os.environ['REVIEW_BOT_TEST_RESPONSES']) as src:\n")
        f.write("    responses = json.load(src)\n")
        f.write("count_path = os.environ['REVIEW_BOT_TEST_COUNT']\n")
        f.write("try:\n")
        f.write("    with open(count_path) as src:\n")
        f.write("        index = sum(1 for _ in src)\n")
        f.write("except FileNotFoundError:\n")
        f.write("    index = 0\n")
        f.write("with open(count_path, 'a') as dst:\n")
        f.write("    dst.write('1\\n')\n")
        f.write("if index >= len(responses):\n")
        f.write("    raise SystemExit('unexpected extra engine invocation')\n")
        f.write("sys.stdout.write(json.dumps({'result': json.dumps(responses[index])}))\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)
    os.environ["REVIEW_BOT_TEST_RESPONSES"] = responses_path
    os.environ["REVIEW_BOT_TEST_COUNT"] = count_path
    os.environ["REVIEW_BOT_CLAUDE_CMD"] = sys.executable + " " + path
    return path


DRAFT_TWO = {
    "verdict": "request_changes",
    "summary": "The payload line grew without a bound.",
    "findings": [
        {
            "file": "payload.txt", "line_start": 1, "line_end": 1,
            "severity": "major", "confidence": "high",
            "title": "Unbounded payload line",
            "rationale": "Nothing caps the line length.",
            "suggestion": "Cap it.",
        },
        {
            "file": "payload.txt", "line_start": 1, "line_end": 1,
            "severity": "minor", "confidence": "medium",
            "title": "No regression test",
            "rationale": "The change has no test.",
            "suggestion": "Add one.",
        },
    ],
}
VERIFIED_ONE = {
    "verdict": "comment",
    "summary": "One finding survived verification.",
    "findings": [DRAFT_TWO["findings"][0]],
}


def render_pr_review(tmpdir, cap=None):
    """Drive the real do_pr_review over the real git fixture; return the markdown."""
    make_stub_engine(tmpdir, [DRAFT_TWO, VERIFIED_ONE])
    review = fresh_review()
    if cap is not None:
        review.DIFF_INLINE_CAP = cap
    wt, base = make_git_tree(tmpdir, payload=200)
    head = _git(wt, "rev-parse", "HEAD").strip()
    checkout = RecordingCheckout(wt)
    review.api = lambda method, path, token, data=None: {
        "merged": False, "base": {"ref": "main"}, "head": {"sha": head},
    }
    review.prepare_checkout = (
        lambda owner, repo, pr, base_ref, auth, repo_dir=None, expected_head=None,
        recorded_merge_base=None: (checkout, base)
    )
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        markdown, url = review.do_pr_review(
            _Args(), ["claude"], "medium", "(none provided)", "tok", auth=_Auth()
        )
    assert url is None and checkout.entered and checkout.exited
    return markdown, err.getvalue()


# ── 1. the cap boundary, exactly ──────────────────────────────────────────────
def test_cap_boundary_is_exact():
    cap = 4000
    with scratch_dir() as tmp:
        review = fresh_review()
        review.DIFF_INLINE_CAP = cap
        wt, base = make_git_tree(tmp)
        n = tune_diff_to(wt, base, cap)
        assert diff_len(wt, base) == cap

        with contextlib.redirect_stderr(io.StringIO()):
            block, inlined = review.changed_files_block(wt, base, _Auth())
        assert inlined is True, "a diff of exactly DIFF_INLINE_CAP chars must inline"
        assert "```diff" in block and "diff is large" not in block

        set_payload(wt, n + 1)
        assert diff_len(wt, base) == cap + 1
        with contextlib.redirect_stderr(io.StringIO()):
            block, inlined = review.changed_files_block(wt, base, _Auth())
        assert inlined is False, "one char over the cap must fall back to the file list"
        assert "```diff" not in block and "diff is large" in block
    print("ok  1. cap boundary: exactly DIFF_INLINE_CAP inlines, +1 char elides")


# ── 2. the journal line ───────────────────────────────────────────────────────
def test_log_line_shape():
    cap = 3000
    with scratch_dir() as tmp:
        review = fresh_review()
        review.DIFF_INLINE_CAP = cap
        wt, base = make_git_tree(tmp)
        tune_diff_to(wt, base, cap)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            review.changed_files_block(wt, base, _Auth())
        assert f"review-bot-review: diff {cap} chars vs cap {cap} — inlined\n" in err.getvalue(), (
            err.getvalue()
        )

        tune_diff_to(wt, base, cap + 500)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            review.changed_files_block(wt, base, _Auth())
        expected = f"review-bot-review: diff {cap + 500} chars vs cap {cap} — file-list only\n"
        assert expected in err.getvalue(), err.getvalue()
    print("ok  2. journal line: 'diff <N> chars vs cap <C> — <inlined|file-list only>'")


# ── 3. the footer segment, its ordering, and classification ───────────────────
def test_footer_segment_order_and_classification():
    with scratch_dir() as tmp:
        markdown, stderr = render_pr_review(tmp)
    assert "· findings `claude 2→1` · diff `inlined` · merge-base `" in markdown, markdown
    footer = markdown.rsplit("---", 1)[1]
    assert footer.index("findings `") < footer.index("diff `") < footer.index("merge-base `")
    assert "Automated review by **review-bot**" in markdown
    assert re.search(r"review-bot-review: diff \d+ chars vs cap \d+ — inlined", stderr), stderr

    feedback = load_module("feedback_diff_mode_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert feedback.classify(markdown) == "review"
    print("ok  3. footer: findings → diff → merge-base, and classify() == 'review'")


def test_footer_reports_file_list_when_elided():
    with scratch_dir() as tmp:
        markdown, stderr = render_pr_review(tmp, cap=1)
    assert "· findings `claude 2→1` · diff `file-list` · merge-base `" in markdown, markdown
    assert "diff `inlined`" not in markdown
    assert re.search(r"review-bot-review: diff \d+ chars vs cap 1 — file-list only", stderr), stderr
    feedback = load_module("feedback_diff_mode_elide_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert feedback.classify(markdown) == "review"
    print("ok  4. elided input renders `diff `file-list`` and still classifies as a review")


# ── 5-7. tools/finder_ab.py against a stub binary ─────────────────────────────
STUB_BINARY = r'''
import json, os, sys
log = os.environ["FINDER_AB_TEST_LOG"]
try:
    with open(log) as src:
        index = sum(1 for _ in src)
except FileNotFoundError:
    index = 0
cap = os.environ.get("REVIEW_BOT_DIFF_CAP", "")
with open(log, "a") as dst:
    dst.write(json.dumps({"argv": sys.argv, "cap": cap}) + "\n")
exit_code = int(os.environ.get("FINDER_AB_TEST_EXIT", "0"))
if exit_code:
    sys.stderr.write("stub: simulated abort\n")
    raise SystemExit(exit_code)
counts = "claude 0→0" if index % 2 == 0 else "claude 2→1"
verdict = "✅ no blocking issues" if index % 2 == 0 else "\U0001f4ac comments"
# review.py journals this for every empty finder. The stub alternates the two shapes it
# can carry — an explicit approve, and an object normalize() defaulted into a review —
# because telling those apart is the whole reason the harness stores it.
diag_shape = os.environ.get("FINDER_AB_TEST_EMPTY_DIAG", "")
if counts.endswith("0→0") and diag_shape:
    if index % 4 == 0:
        parse = {"path": "envelope-result", "keys": ["findings", "summary", "verdict"],
                 "verdict_raw": "approve", "verdict_present": True,
                 "findings_kind": "list", "findings_len": 0}
    else:
        parse = {"path": "envelope-result", "keys": ["file", "line_start"],
                 "verdict_raw": None, "verdict_present": False,
                 "findings_kind": "missing", "findings_len": None}
    sys.stderr.write(
        "review-bot-review: empty-finder diagnostic: "
        + json.dumps({"harness": "claude", "mode": "pr", "raw_chars": 42,
                      "raw_excerpt": "…", "repair_retried": False, "parse": parse})
        + "\n"
    )
mode = "file-list" if cap == "1" else "inlined"
# Mimic review.py's journal line so the harness can record the MEASURED diff size.
chars = os.environ.get("FINDER_AB_TEST_DIFF_CHARS", "4096")
if chars == "0":
    mode = "inlined"  # 0 <= any cap — the masquerade the harness must refuse
# Reports a cap the harness did NOT force, standing in for the socket client, which
# drops REVIEW_BOT_DIFF_CAP and runs at the service default while still relaying a
# well-formed journal line.
reported_cap = os.environ.get("FINDER_AB_TEST_REPORT_CAP", cap or "60000")
sys.stderr.write(
    "review-bot-review: diff " + chars + " chars vs cap " + reported_cap
    + " — " + ("inlined" if mode == "inlined" else "file-list only") + "\n"
)
print("## \U0001f916 review-bot — " + verdict)
print()
print("---")
print(
    "*Automated review by **review-bot** · harness `claude` · depth `standard` · "
    "bar `medium` · findings `" + counts + "` · diff `" + mode + "` · "
    "merge-base `abcdef012345`. Advisory only — olli merges.*"
)
'''


def make_stub_binary(tmpdir, name="stub-review-local.py"):
    path = os.path.join(tmpdir, name)
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n")
        f.write(STUB_BINARY)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)
    log = os.path.join(tmpdir, "binary-invocations.jsonl")
    os.environ["FINDER_AB_TEST_LOG"] = log
    return path, log


def fresh_finder_ab(head="deadbeefcafe1234"):
    mod = load_module("finder_ab_test", os.path.join(REPO_ROOT, "tools", "finder_ab.py"))
    mod.load_token = lambda: "test-token"
    heads = head if isinstance(head, list) else None
    if heads is None:
        mod.pr_head_sha = lambda owner, repo, pr, token: head
    else:
        seq = iter(heads)
        mod.pr_head_sha = lambda owner, repo, pr, token: next(seq)
    return mod


def run_finder_ab(mod, stub, out, runs=2, extra=()):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        rc = mod.main([
            "--owner", "acme", "--repo", "widget", "--pr", "7",
            "--runs", str(runs), "--binary", stub, "--out", out, *extra,
        ])
    return rc, stdout.getvalue()


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def summary_rows(table_text):
    """Parse the printed summary into {(harness, input mode): [cells]}."""
    rows = {}
    for line in table_text.splitlines():
        parts = line.split()
        if len(parts) == 7 and parts[0] in ("claude", "codex") and parts[1].startswith("forced-"):
            rows[(parts[0], parts[1])] = parts[2:]
    return rows


def test_finder_ab_factorial_print_only_and_caps():
    runs = 2
    with scratch_dir() as tmp:
        stub, log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        out = os.path.join(tmp, "finder-ab.jsonl")
        rc, table = run_finder_ab(fresh_finder_ab(), stub, out, runs=runs)
        assert rc == 0
        invocations = read_jsonl(log)
        records = read_jsonl(out)

    assert len(records) == 2 * 2 * runs == len(invocations), (len(records), len(invocations))
    for inv in invocations:
        assert "--print-only" in inv["argv"], inv["argv"]
        assert "--mode" in inv["argv"] and "pr" in inv["argv"]
    for rec, inv in zip(records, invocations):
        if rec["input_mode"] == "forced-elide":
            assert inv["cap"] == "1" and rec["diff_cap"] == 1, (inv, rec)
            assert rec["diff_mode"] == "file-list"
        else:
            assert int(inv["cap"]) >= 1000000 and rec["diff_cap"] >= 1000000, (inv, rec)
            assert rec["diff_mode"] == "inlined"
        assert rec["status"] == 0 and rec["aborted"] is False
        assert rec["head"] == "deadbeefcafe1234"
        assert rec["depth"] == "standard" and rec["confidence_bar"] == "medium"
        assert rec["verdict"]

    cells = {(r["harness"], r["input_mode"]) for r in records}
    assert cells == {(h, m) for h in ("claude", "codex") for m in ("forced-inline", "forced-elide")}

    rows = summary_rows(table)
    assert len(rows) == 4, table
    for key, cols in rows.items():
        runs_col, aborted, empty, mean_draft, mean_surv = cols
        assert runs_col == str(runs) and aborted == "0", (key, cols)
        assert empty == "1", (key, cols, table)  # one 0→0 draft per two-run cell
        assert mean_draft == "1.00" and mean_surv == "0.50", (key, cols)
    print("ok  5. finder_ab: 2×2×runs runs, every argv --print-only, caps and cells correct")


def test_finder_ab_records_aborted_runs():
    runs = 1
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ["FINDER_AB_TEST_EXIT"] = "3"
        out = os.path.join(tmp, "finder-ab.jsonl")
        try:
            rc, table = run_finder_ab(fresh_finder_ab(), stub, out, runs=runs)
        finally:
            os.environ.pop("FINDER_AB_TEST_EXIT", None)
        records = read_jsonl(out)
    assert rc == 0
    assert len(records) == 2 * 2 * runs, records
    for rec in records:
        assert rec["status"] == 3 and rec["aborted"] is True
        assert rec["draft_findings"] is None and rec["verdict"] is None
        assert "simulated abort" in rec["stderr_tail"]
    for cols in summary_rows(table).values():
        assert cols[:2] == [str(runs), str(runs)]  # runs, aborted
        assert cols[3] == "-" and cols[4] == "-"   # no comparable means
    print("ok  6. finder_ab: aborted runs are recorded with their status, never dropped")


def test_finder_ab_aborts_when_head_moves():
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        out = os.path.join(tmp, "finder-ab.jsonl")
        # pin, first run, then the head moves before run 2
        mod = fresh_finder_ab(head=["aaaa000011112222", "aaaa000011112222", "bbbb333344445555"])
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                mod.main([
                    "--owner", "acme", "--repo", "widget", "--pr", "7",
                    "--runs", "2", "--binary", stub, "--out", out,
                ])
        except SystemExit as e:
            code = e.code
        else:
            raise AssertionError("a moving PR head must abort the experiment")
        records = read_jsonl(out)
    assert code == 1
    assert "head moved" in err.getvalue(), err.getvalue()
    assert len(records) == 1, records  # the completed run is kept, the cell is not faked
    print("ok  7. finder_ab: a moving PR head aborts loudly instead of mixing cells")


def test_finder_ab_refuses_a_vacuous_zero_char_diff():
    """The failure this harness shipped with: a 0-char diff satisfies `0 <= cap`, so the
    footer says `inlined` even in the forced-elide cells, every cell reports zero findings,
    and the run LOOKS like a clean result. Originally observed live against a merged PR,
    back when the merge base was always computed live and collapsed to the head; #26 fixed
    that cause, but the guard still matters for a genuinely empty PR or a live fallback.
    It must abort, not tabulate."""
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        os.environ["FINDER_AB_TEST_DIFF_CHARS"] = "0"
        out = os.path.join(tmp, "finder-ab.jsonl")
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                fresh_finder_ab().main([
                    "--owner", "acme", "--repo", "widget", "--pr", "7",
                    "--runs", "2", "--binary", stub, "--out", out,
                ])
        except SystemExit as e:
            code = e.code
        else:
            raise AssertionError("a 0-char diff must abort rather than produce a table")
        finally:
            os.environ.pop("FINDER_AB_TEST_DIFF_CHARS", None)
        records = read_jsonl(out)
    assert code == 1
    msg = err.getvalue()
    assert "0-char diff" in msg and "MERGED PR" in msg, msg
    assert len(records) == 1, records          # the observation is kept, the cells are not
    assert records[0]["diff_chars"] == 0, records[0]
    assert records[0]["diff_mode"] == "inlined"  # the masquerade, recorded for the record
    print("ok  8. finder_ab: a vacuous 0-char diff aborts instead of tabulating")


def test_finder_ab_records_measured_diff_size():
    """diff_mode alone cannot separate 'the cap did not take' from 'the diff was empty';
    the measured size can."""
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        os.environ["FINDER_AB_TEST_DIFF_CHARS"] = "65525"
        out = os.path.join(tmp, "finder-ab.jsonl")
        try:
            rc, _table = run_finder_ab(fresh_finder_ab(), stub, out, runs=1)
        finally:
            os.environ.pop("FINDER_AB_TEST_DIFF_CHARS", None)
        records = read_jsonl(out)
    assert rc == 0
    assert len(records) == 4, records
    for rec in records:
        assert rec["diff_chars"] == 65525, rec
        expected_cap = 1 if rec["input_mode"] == "forced-elide" else 100000000
        assert rec["diff_cap_observed"] == expected_cap, rec
    print("ok  9. finder_ab: the measured diff size and the cap actually applied are recorded")


def test_finder_ab_refuses_a_cap_that_did_not_take():
    """The symmetric masquerade to the 0-char one: the reviewer applied a cap the harness
    did not force, so every cell shares one input mode and the matching means read as the
    experiment's conclusion instead of as a broken instrument. The classic trigger is
    --binary pointed at review-bot-review, the socket CLIENT, which accepts the identical
    argv but drops REVIEW_BOT_DIFF_CAP (serve.py honours only its own env) while still
    relaying a journal line that parses. It must abort, not tabulate."""
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        os.environ["FINDER_AB_TEST_REPORT_CAP"] = "60000"  # the service default, not ours
        out = os.path.join(tmp, "finder-ab.jsonl")
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                fresh_finder_ab().main([
                    "--owner", "acme", "--repo", "widget", "--pr", "7",
                    "--runs", "2", "--binary", stub, "--out", out,
                ])
        except SystemExit as e:
            code = e.code
        else:
            raise AssertionError("a cap that did not take must abort rather than tabulate")
        finally:
            os.environ.pop("FINDER_AB_TEST_REPORT_CAP", None)
        records = read_jsonl(out)
    assert code == 1
    msg = err.getvalue()
    assert "did not reach the binary" in msg, msg
    assert "review-bot-review-local" in msg, msg
    # The run that exposed it is still recorded — aborting must not discard evidence.
    assert len(records) == 1, records
    assert records[0]["diff_cap_observed"] == 60000, records[0]
    print("ok 10. finder_ab: a forced cap that never reached the reviewer aborts")


def test_finder_ab_stores_the_empty_finder_diagnostic():
    """An empty cell is the experiment's own subject matter. The record must carry WHY the
    finder came back empty, and the two shapes must not collapse into one another."""
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        os.environ["FINDER_AB_TEST_EMPTY_DIAG"] = "1"
        out = os.path.join(tmp, "finder-ab.jsonl")
        try:
            rc, table = run_finder_ab(fresh_finder_ab(), stub, out, runs=2)
        finally:
            os.environ.pop("FINDER_AB_TEST_EMPTY_DIAG", None)
        records = read_jsonl(out)
        mod = fresh_finder_ab()
    assert rc == 0
    empties = [r for r in records if r["draft_findings"] == 0]
    assert len(empties) == 4, records
    assert all(r.get("empty_finder_diag") for r in empties), empties
    # Non-empty runs stay clean: the field marks the exceptional case only.
    assert all("empty_finder_diag" not in r for r in records if r["draft_findings"]), records
    described = [mod.describe_empty_diag(r) for r in empties]
    assert sum("genuine empty result" in d for d in described) == 2, described
    assert sum("DEFAULTED" in d for d in described) == 2, described
    # …and the operator sees it without reaching for jq.
    assert "empty finders (4)" in table, table
    assert "DEFAULTED — not a real result object" in table, table
    # The classifier follows the schema of the mode that ran. finder_ab only ever drives
    # --mode pr, but a verdict-keyed check would call every clean AUDIT a parse pathology.
    audit_clean = {"empty_finder_diag": {"mode": "repo", "parse": {
        "findings_kind": "list", "verdict_present": False, "verdict_raw": None,
        "path": "envelope-result", "keys": ["findings", "summary"]}}}
    described_audit = mod.describe_empty_diag(audit_clean)
    assert described_audit.startswith("genuine empty result"), described_audit
    assert "verdict n/a (audit schema)" in described_audit, described_audit
    print("ok 11. finder_ab: empty runs keep the reviewer's diagnostic and are classified")


def test_finder_ab_marks_a_missing_diagnostic_rather_than_faking_one():
    """An older binary emits no diagnostic. That must read as 'unknown', never as 'genuine'."""
    with scratch_dir() as tmp:
        stub, _log = make_stub_binary(tmp)
        os.environ.pop("FINDER_AB_TEST_EXIT", None)
        os.environ.pop("FINDER_AB_TEST_EMPTY_DIAG", None)
        out = os.path.join(tmp, "finder-ab.jsonl")
        rc, table = run_finder_ab(fresh_finder_ab(), stub, out, runs=1)
        records = read_jsonl(out)
        mod = fresh_finder_ab()
    assert rc == 0
    empties = [r for r in records if r["draft_findings"] == 0]
    assert empties and all(r["empty_finder_diag"] is None for r in empties), empties
    # The stderr tail is kept instead, so the run is still worth something.
    assert all(r.get("stderr_tail") for r in empties), empties
    assert all("no diagnostic recorded" in mod.describe_empty_diag(r) for r in empties), empties
    assert "no diagnostic recorded" in table, table
    print("ok 12. finder_ab: a missing diagnostic is reported as unknown, not as genuine")


def main():
    tests = [
        test_cap_boundary_is_exact,
        test_log_line_shape,
        test_footer_segment_order_and_classification,
        test_footer_reports_file_list_when_elided,
        test_finder_ab_factorial_print_only_and_caps,
        test_finder_ab_records_aborted_runs,
        test_finder_ab_aborts_when_head_moves,
        test_finder_ab_refuses_a_vacuous_zero_char_diff,
        test_finder_ab_records_measured_diff_size,
        test_finder_ab_refuses_a_cap_that_did_not_take,
        test_finder_ab_stores_the_empty_finder_diagnostic,
        test_finder_ab_marks_a_missing_diagnostic_rather_than_faking_one,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

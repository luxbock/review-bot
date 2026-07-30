#!/usr/bin/env python3
"""Acceptance tests for cheap-agent inline selection (issue #35).

Over the cap, a cheap engine ranks the changed files so the inline budget goes to
the hunks most worth reading. It decides ORDER, never scope. Covered here, with NO
live engine and NO live forge:

  * an under-cap diff runs no selection at all and renders a byte-identical footer;
  * an over-cap diff runs it, and the ranked files are the ones that get inlined;
  * every failure mode — disabled, missing binary, non-zero exit, unparseable
    output, empty list, a ranking naming files that are not in the diff — degrades
    to source-order packing and still produces a review;
  * selection can never remove a file from the block: unranked files are still
    listed with the read-it-from-the-checkout instruction;
  * the footer and journal report the same counts from the one measurement, and
    the selection's own prose reaches the JOURNAL only — no path exists by which
    a "reason" string becomes a finding.

Run:  python3 tests/test_diff_selection.py
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
GIT = shutil.which("git")
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_review():
    review = load_module("review_diff_selection_test", os.path.join(REPO_ROOT, "review.py"))
    review.GIT = GIT
    review.REVIEW_PROMPT_FILE = os.path.join(REPO_ROOT, "review-prompt.md")
    review.VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "verify-prompt.md")
    review.SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "synthesis-prompt.md")
    review.TRIAGE_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-prompt.md")
    review.TRIAGE_VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-verify-prompt.md")
    review.TRIAGE_SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "triage-synthesis-prompt.md")
    review.AUDIT_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-prompt.md")
    review.AUDIT_VERIFY_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-verify-prompt.md")
    review.AUDIT_SYNTHESIS_PROMPT_FILE = os.path.join(REPO_ROOT, "audit-synthesis-prompt.md")
    review.SELECT_PROMPT_FILE = os.path.join(REPO_ROOT, "select-prompt.md")
    return review


@contextlib.contextmanager
def scratch_dir():
    root = os.environ.get("WORKER_SCRATCH") or None
    if root:
        os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="review-bot-selection-", dir=root)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _Auth:
    def env(self):
        e = dict(os.environ)
        e["GIT_CONFIG_NOSYSTEM"] = "1"
        return e


def _git(wt, *args):
    return subprocess.run(
        [GIT, "-C", wt, *args], check=True, capture_output=True, text=True
    ).stdout


def make_tree(parent, payloads):
    """Two commits touching len(payloads) files; file i's changed line carries
    payloads[i] 'x' chars. Returns (worktree, base_sha)."""
    wt = os.path.join(parent, "wt")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True)
    for i in range(len(payloads)):
        with open(os.path.join(wt, f"file{i}.txt"), "w") as f:
            f.write("x\n")
    _git(wt, "add", ".")
    _git(wt, *GIT_ID, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD").strip()
    for i, n in enumerate(payloads):
        with open(os.path.join(wt, f"file{i}.txt"), "w") as f:
            f.write("x" * n + "\n")
    _git(wt, "add", ".")
    _git(wt, *GIT_ID, "commit", "-qm", "change")
    return wt, base


def make_select_stub(tmpdir, *, stdout="", exit_code=0, name="select-stub.py"):
    """A fake selection engine. Records the prompt it was given so the test can assert
    what the stage sends (metadata only — never hunk bodies)."""
    path = os.path.join(tmpdir, name)
    prompt_log = os.path.join(tmpdir, name + ".prompt")
    with open(path, "w") as f:
        f.write("#!" + sys.executable + "\n")
        f.write("import os, sys\n")
        f.write("data = sys.stdin.read()\n")
        f.write("open(os.environ['SELECT_STUB_PROMPT'], 'w').write(data)\n")
        f.write(f"sys.stdout.write({stdout!r})\n")
        f.write(f"raise SystemExit({exit_code})\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)
    os.environ["SELECT_STUB_PROMPT"] = prompt_log
    return sys.executable + " " + path, prompt_log


def envelope(files):
    """The shape `claude -p --output-format json` returns: the model's text nested in
    a result envelope."""
    return json.dumps({"result": json.dumps({"files": files})})


# ── 1. under the cap, the stage does not run at all ──────────────────────────
def test_under_cap_never_selects():
    with scratch_dir() as tmp:
        review = fresh_review()
        cmd, prompt_log = make_select_stub(tmp, stdout=envelope([{"path": "file0.txt"}]))
        review.SELECT_CMD = cmd.split()
        wt, base = make_tree(tmp, [200, 200])
        review.DIFF_INLINE_CAP = 10_000_000

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        assert mode.kind == "inlined" and mode.selection is None, mode
        assert mode.footer_word == "inlined", mode.footer_word
        assert not os.path.exists(prompt_log), "the cheap engine must not be invoked"
        assert "selecting inline order" not in err.getvalue(), err.getvalue()
        assert "```diff" in block
    print("ok  1. an under-cap review runs no selection and its footer is unchanged")


# ── 2. over the cap, the ranking decides which files get the budget ──────────
def test_ranking_decides_what_is_inlined():
    with scratch_dir() as tmp:
        review = fresh_review()
        # file2 is last in source order, so source-order packing would drop it first.
        cmd, prompt_log = make_select_stub(tmp, stdout=envelope([
            {"path": "file2.txt", "reason": "touches the persistence path"},
            {"path": "file0.txt", "reason": "control flow"},
        ]))
        review.SELECT_CMD = cmd.split()
        wt, base = make_tree(tmp, [500, 500, 500])
        chunks = review.split_diff_by_file(_git(wt, "diff", f"{base}..HEAD"))
        sizes = {p: len(t) for p, t in chunks}
        review.DIFF_INLINE_CAP = sizes["file0.txt"] + sizes["file2.txt"]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            block, mode, _stats = review.changed_files_block(
                wt, base, _Auth(), conv_str="README.md"
            )

        assert (mode.kind, mode.inlined_files, mode.total_files) == ("partial", 2, 3), mode
        assert mode.selected and mode.selection.status == "ok", vars(mode.selection)
        assert mode.footer_word == "partial 2/3 files, selected", mode.footer_word
        # The ranked files are inlined; the unranked one is listed, not dropped.
        body = block.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
        assert "file2.txt" in body and "file0.txt" in body, body[:300]
        assert "file1.txt" not in body, "an unranked file took a ranked file's budget"
        assert "- `file1.txt`" in block, block[-300:]
        assert "read from the checkout before judging" in block
        # Ranked order wins over source order inside the inlined section.
        assert body.index("file2.txt") < body.index("file0.txt"), "ranking must set order"

        # The stage is fed metadata only — hunk headers, never hunk bodies.
        sent = open(prompt_log).read()
        assert "file0.txt" in sent and "@@" in sent, sent[:400]
        assert "x" * 500 not in sent, "the cheap stage must not be sent hunk bodies"
        assert "README.md" in sent, "convention files belong in the selection prompt"
    print("ok  2. the ranking sets the packing order; unranked files are listed, not lost")


# ── 3. every failure mode degrades to source order, never to a dead review ───
def test_failures_degrade_to_source_order():
    cases = [
        ("disabled", lambda tmp: ([], None)),
        ("failed", lambda tmp: (["/nonexistent/select-binary"], None)),
        ("failed", lambda tmp: (make_select_stub(tmp, stdout="", exit_code=3,
                                                 name="boom.py")[0].split(), None)),
        ("unparsed", lambda tmp: (make_select_stub(tmp, stdout="I am not JSON",
                                                   name="prose.py")[0].split(), None)),
        ("unparsed", lambda tmp: (make_select_stub(tmp, stdout='{"files": "nope"}',
                                                   name="wrongtype.py")[0].split(), None)),
        ("empty", lambda tmp: (make_select_stub(tmp, stdout=envelope([]),
                                                name="empty.py")[0].split(), None)),
        ("empty", lambda tmp: (make_select_stub(tmp, stdout=envelope(
            [{"path": "not-in-this-diff.txt", "reason": "invented"}]),
            name="hallucinated.py")[0].split(), None)),
    ]
    for expected_status, build in cases:
        with scratch_dir() as tmp:
            review = fresh_review()
            cmd, _ = build(tmp)
            review.SELECT_CMD = cmd
            wt, base = make_tree(tmp, [500, 500, 500])
            chunks = review.split_diff_by_file(_git(wt, "diff", f"{base}..HEAD"))
            sizes = [len(t) for _, t in chunks]
            review.DIFF_INLINE_CAP = sizes[0] + sizes[1]

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                block, mode, _stats = review.changed_files_block(wt, base, _Auth())

            assert mode.selection.status == expected_status, (
                f"{expected_status} case reported {mode.selection.status}"
            )
            assert not mode.selected, "a failed ranking must not claim to have selected"
            assert mode.footer_word == "partial 2/3 files", mode.footer_word
            # Source order: the first two files, exactly as without a selection stage.
            body = block.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
            assert "file0.txt" in body and "file1.txt" in body and "file2.txt" not in body
            assert "- `file2.txt`" in block
            assert f"selection {expected_status}" in err.getvalue(), err.getvalue()
    print(f"ok  3. all {len(cases)} failure modes fall back to source order and say so")


# ── 4. one measurement: footer and journal cannot disagree ───────────────────
def test_footer_and_journal_agree():
    with scratch_dir() as tmp:
        review = fresh_review()
        cmd, _ = make_select_stub(tmp, stdout=envelope([
            {"path": "file2.txt", "reason": "persistence"},
        ]))
        review.SELECT_CMD = cmd.split()
        wt, base = make_tree(tmp, [400, 400, 400])
        chunks = review.split_diff_by_file(_git(wt, "diff", f"{base}..HEAD"))
        sizes = {p: len(t) for p, t in chunks}
        review.DIFF_INLINE_CAP = sizes["file2.txt"] + sizes["file0.txt"]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        journal = err.getvalue()

        assert f"{mode.inlined_files} of {mode.total_files} files inlined" in journal
        assert f"partial {mode.inlined_files}/{mode.total_files} files" in mode.footer_word
        # The reasons are journalled for the operator — and ONLY journalled.
        assert "selection ranked 1: file2.txt — persistence" in journal, journal
        rendered = review.render_markdown(
            {"verdict": "approve", "summary": "", "findings": []},
            ["claude"], "standard", "medium", "f" * 40,
            provenance={"stages": [{"harness": "claude", "draft_count": 0,
                                    "surviving_count": 0}]},
            diff_stats={"files": 3, "insertions": 3, "deletions": 3}, diff_mode=mode,
        )
        assert "persistence" not in rendered, "a selection reason must never be rendered"
        assert f"diff `{mode.footer_word}`" in rendered, rendered
        # And an empty finder on a partially-seen diff still warns (#34 constraint 5).
        assert "⚠️ 0 findings on a change the finder did not fully see" in rendered
    print("ok  4. footer, journal and calibration agree; reasons stay in the journal")


# ── 5. the parser itself, against hostile input ──────────────────────────────
def test_parser_rejects_hostile_input():
    review = fresh_review()
    known = {"a.py", "b.py"}

    ranked, reasons = review.parse_selection(
        json.dumps({"files": [
            {"path": "b.py", "reason": "  multi\nline   reason  "},
            {"path": "b.py", "reason": "duplicate"},
            {"path": "../../etc/passwd", "reason": "path escape"},
            {"path": 42, "reason": "wrong type"},
            "a.py",
            {"no_path_key": True},
        ]}), known)
    assert ranked == ["b.py", "a.py"], ranked
    assert reasons["b.py"] == "multi line reason", repr(reasons["b.py"])
    assert len(reasons["b.py"]) <= review.SELECT_REASON_MAX

    # A reply that is not the contract at all yields "no ranking", not a crash.
    assert review.parse_selection("", known) == (None, {})
    assert review.parse_selection("no json here", known) == (None, {})
    assert review.parse_selection(json.dumps({"verdict": "approve"}), known) == (None, {})
    # A findings-shaped reply is not a finding — it is discarded like anything else.
    assert review.parse_selection(
        json.dumps({"findings": [{"file": "a.py", "severity": "blocker"}]}), known
    ) == (None, {})
    print("ok  5. the parser keeps only real paths, flattens reasons, mints nothing")


# ── 6. reordering is a permutation — never adds or drops a file ──────────────
def test_apply_selection_is_a_permutation():
    review = fresh_review()
    chunks = [("a.py", "A"), ("b.py", "B"), ("c.py", "C")]
    assert review.apply_selection(chunks, []) == chunks
    assert review.apply_selection(chunks, ["c.py"]) == [
        ("c.py", "C"), ("a.py", "A"), ("b.py", "B")]
    assert review.apply_selection(chunks, ["c.py", "a.py", "b.py"]) == [
        ("c.py", "C"), ("a.py", "A"), ("b.py", "B")]
    # An unknown path in the ranking is inert.
    assert sorted(review.apply_selection(chunks, ["zz.py", "b.py"])) == sorted(chunks)
    for ranked in ([], ["a.py"], ["c.py", "b.py"], ["zz.py"]):
        assert sorted(review.apply_selection(chunks, ranked)) == sorted(chunks), ranked
    print("ok  6. apply_selection permutes the chunk list and nothing more")


TESTS = [
    test_under_cap_never_selects,
    test_ranking_decides_what_is_inlined,
    test_failures_degrade_to_source_order,
    test_footer_and_journal_agree,
    test_parser_rejects_hostile_input,
    test_apply_selection_is_a_permutation,
]


def main():
    if not GIT:
        print("git not found on PATH", file=sys.stderr)
        return 1
    for test in TESTS:
        test()
    print(f"\nall {len(TESTS)} selection tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

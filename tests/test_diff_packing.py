#!/usr/bin/env python3
"""Acceptance tests for greedy whole-file diff packing (issue #34).

Before #34 the cap was all-or-nothing: a diff one char over `REVIEW_BOT_DIFF_CAP`
lost every hunk. These tests pin the replacement, with NO live engine and NO live
forge:

  * the packing itself — whole files only, source order, first-fit with skip-over,
    and the unchanged boundary (a diff of exactly the cap is still fully inlined);
  * constraint 4: a single file bigger than the whole cap still degrades to the
    file-list-only block, never to half a file;
  * one measurement, three consumers — the journal line, the footer segment and the
    empty-verdict calibration all report the SAME mode for the same run;
  * constraint 5: an empty finder result on a non-fully-inlined diff takes the ⚠️
    "not fully reviewed" branch whatever the diff's size;
  * the #34 evidence case replayed: 3 files / 61,313 chars at cap 60,000 inlines
    files instead of none.

Run:  python3 tests/test_diff_packing.py
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
GIT = shutil.which("git")
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_review():
    """review.py wired past its build-time @PLACEHOLDER@s so it runs from the checkout."""
    review = load_module("review_diff_packing_test", os.path.join(REPO_ROOT, "review.py"))
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
    # Packing is this file's subject; the #35 selection stage that can REORDER the
    # chunks is disabled so these assertions are about source-order packing alone.
    # tests/test_diff_selection.py owns the ranked-order case.
    review.SELECT_CMD = []
    return review


@contextlib.contextmanager
def scratch_dir():
    root = os.environ.get("WORKER_SCRATCH") or None
    if root:
        os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(prefix="review-bot-diff-packing-", dir=root)
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


# ── git fixture: N files whose per-file diff sizes we dial independently ──────
def _git(wt, *args):
    return subprocess.run(
        [GIT, "-C", wt, *args], check=True, capture_output=True, text=True,
        errors="replace",
    ).stdout


def make_multi_file_tree(parent, payloads):
    """Two commits touching len(payloads) files; file i's changed line carries
    payloads[i] 'x' characters. Returns (worktree, base_sha)."""
    wt = os.path.join(parent, "private-worktree")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True)
    for i in range(len(payloads)):
        with open(os.path.join(wt, f"file{i}.txt"), "w") as f:
            f.write("x\n")
    _git(wt, "add", ".")
    _git(wt, *GIT_ID, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD").strip()
    set_payloads(wt, payloads, amend=False)
    return wt, base


def set_payloads(wt, payloads, amend=True):
    for i, n in enumerate(payloads):
        with open(os.path.join(wt, f"file{i}.txt"), "w") as f:
            f.write("x" * n + "\n")
    _git(wt, "add", ".")
    args = [*GIT_ID, "commit", "-q"]
    if amend:
        args.append("--amend")
    _git(wt, *args, "-m", "change payloads")


def diff_text(wt, base):
    return _git(wt, "diff", f"{base}..HEAD")


def chunk_sizes(review, wt, base):
    return [len(text) for _path, text in review.split_diff_by_file(diff_text(wt, base))]


def tune_total_to(wt, base, payloads, target):
    """Dial the LAST file's payload until the whole diff is exactly `target` chars.

    The per-file overhead (headers, hunk marker, git's chosen blob-hash abbreviation)
    is git's business, so converge on it rather than assume it.
    """
    payloads = list(payloads)
    for _ in range(10):
        set_payloads(wt, payloads)
        got = len(diff_text(wt, base))
        if got == target:
            return payloads
        payloads[-1] += target - got
        assert payloads[-1] > 0, "target is smaller than the diff's fixed overhead"
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


EMPTY_REVIEW = {"verdict": "approve", "summary": "", "findings": []}


def render_pr_review(tmpdir, payloads, cap, responses):
    """Drive the real do_pr_review over a real multi-file tree; return (markdown, stderr)."""
    make_stub_engine(tmpdir, responses)
    review = fresh_review()
    review.DIFF_INLINE_CAP = cap
    wt, base = make_multi_file_tree(tmpdir, payloads)
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


# ── 1. splitting is lossless and per file ─────────────────────────────────────
def test_split_is_lossless_and_per_file():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_multi_file_tree(tmp, [300, 400, 500])
        diff = diff_text(wt, base)
        chunks = review.split_diff_by_file(diff)

        assert [p for p, _ in chunks] == ["file0.txt", "file1.txt", "file2.txt"], chunks
        assert "".join(t for _, t in chunks) == diff, "splitting must round-trip exactly"
        for path, text in chunks:
            assert text.startswith("diff --git "), text[:80]
            assert text.count("diff --git ") == 1, f"{path} chunk swallowed another file"
    print("ok  1. split_diff_by_file: one whole chunk per file, concatenates back exactly")


# ── 2. the cap boundary is unchanged, and cap-1 packs instead of collapsing ───
def test_boundary_unchanged_and_over_cap_packs():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_multi_file_tree(tmp, [800, 300, 800, 300])
        total = len(diff_text(wt, base))
        sizes = chunk_sizes(review, wt, base)

        # Exactly the cap: fully inlined, as before #34.
        review.DIFF_INLINE_CAP = total
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.kind == "inlined" and mode.fully_inlined, mode
        assert mode.inlined_files == mode.total_files == 4, mode
        assert block.endswith("```") and "Not inlined" not in block

        # One char under the total: every file but the last one fits.
        review.DIFF_INLINE_CAP = total - 1
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.kind == "partial" and not mode.fully_inlined, mode
        assert (mode.inlined_files, mode.total_files) == (3, 4), mode
        assert mode.inlined_chars == sum(sizes[:3]) and mode.total_chars == total, mode
        assert "```diff" in block, block[:400]
        assert "+" + "x" * 800 in block, "the first file's hunk must be inlined in full"
        assert "Not inlined (1 file)" in block and "- `file3.txt`" in block, block[-400:]
        assert "the other 1 file is listed after them" in block, block[:500]
        assert "- `file0.txt`" not in block, "an inlined file must not also be listed"
    print("ok  2. exactly-cap still inlines everything; over-cap keeps the files that fit")


# ── 3. whole files only, first-fit skips over an oversized file ──────────────
def test_oversized_file_is_skipped_not_truncated():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_multi_file_tree(tmp, [200, 5000, 200])
        sizes = chunk_sizes(review, wt, base)
        # Budget for the two small files but not the middle one: first-fit must skip it
        # and keep scanning rather than stop at the first file that does not fit.
        review.DIFF_INLINE_CAP = sizes[0] + sizes[2]
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        assert (mode.kind, mode.inlined_files, mode.total_files) == ("partial", 2, 3), mode
        assert "Not inlined (1 file)" in block, block[-300:]
        assert "- `file1.txt`" in block and "x" * 5000 not in block, "no partial file"
        assert "file0.txt" in block and "file2.txt" in block
        # Nothing may be truncated mid-file: every inlined chunk appears verbatim.
        inlined_body = block.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
        chunks = dict(review.split_diff_by_file(diff_text(wt, base)))
        assert inlined_body == chunks["file0.txt"] + chunks["file2.txt"], inlined_body[:400]
    print("ok  3. a file that does not fit is skipped whole; later files still pack")


# ── 4. constraint 4: nothing fits → the pre-#34 file-list block ──────────────
def test_single_oversized_file_degrades_to_file_list():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_multi_file_tree(tmp, [4000])
        review.DIFF_INLINE_CAP = 100
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        assert mode.kind == "file-list" and mode.inlined_files == 0, mode
        assert "```diff" not in block, "a file too big for the cap must not be truncated"
        assert "only the file list is inlined" in block
        assert "— file-list only" in err.getvalue(), err.getvalue()
    print("ok  4. no file fits → file-list only, the pre-#34 wording, no partial file")


# ── 5. one measurement, three consumers ──────────────────────────────────────
def test_journal_footer_and_calibration_agree():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_multi_file_tree(tmp, [800, 300, 800, 300])
        total = len(diff_text(wt, base))
        sizes = chunk_sizes(review, wt, base)
        review.DIFF_INLINE_CAP = total - 1
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        expected = (
            f"review-bot-review: diff {total} chars vs cap {total - 1} — "
            f"3 of 4 files inlined, {sum(sizes[:3])} of {total} chars "
            f"(selection disabled)\n"
        )
        assert expected in err.getvalue(), err.getvalue()
        assert mode.footer_word == "partial 3/4 files", mode.footer_word
        # tools/finder_ab.py keys off this prefix; the wording after it may grow.
        assert re.search(r"diff (\d+) chars vs cap (\d+) — ", err.getvalue())
    print("ok  5. journal line and footer word are the same measurement, three states")


def test_footer_states_end_to_end():
    review = fresh_review()
    findings_draft = {
        "verdict": "request_changes", "summary": "s",
        "findings": [{
            "file": "file0.txt", "line_start": 1, "line_end": 1,
            "severity": "major", "confidence": "high", "title": "t",
            "rationale": "r", "suggestion": "",
        }],
    }
    with scratch_dir() as tmp:
        wt, base = make_multi_file_tree(tmp, [800, 300, 800, 300])
        total = len(diff_text(wt, base))
        shutil.rmtree(wt)
        markdown, err = render_pr_review(
            tmp, [800, 300, 800, 300], total - 1, [findings_draft, findings_draft]
        )
    assert " · diff `partial 3/4 files` · merge-base `" in markdown, markdown
    footer = markdown.rsplit("---", 1)[1]
    assert footer.index("findings `") < footer.index("diff `") < footer.index("merge-base `")
    assert re.search(r"— 3 of 4 files inlined, \d+ of \d+ chars", err), err

    feedback = load_module("feedback_diff_packing_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert feedback.classify(markdown) == "review"
    print("ok  6. the footer discloses `partial k/n files` and still classifies as a review")


# ── 7. constraint 5: an empty finder on a partial diff is never reassuring ───
def test_empty_result_on_partial_input_takes_the_warning_branch():
    review = fresh_review()
    prov = {"stages": [{"harness": "claude", "draft_count": 0, "surviving_count": 0}]}

    def render(stats, mode):
        return review.render_markdown(
            {"verdict": "approve", "summary": "", "findings": []},
            ["claude"], "standard", "medium", "f" * 40,
            provenance={"stages": list(prov["stages"])}, diff_stats=stats, diff_mode=mode,
        )

    small = {"files": 3, "insertions": 40, "deletions": 7}
    inlined = review.DiffMode("inlined", 3, 3, 900, 900)
    partial = review.DiffMode("partial", 1, 3, 400, 900)
    file_list = review.DiffMode("file-list", 0, 3, 0, 900)

    clean = render(small, inlined)
    assert "0 findings on a small change (3 files, +40/-7)" in clean, clean
    assert "⚠️" not in clean, clean

    for mode in (partial, file_list):
        warned = render(small, mode)
        assert "⚠️ 0 findings on a change the finder did not fully see" in warned, warned
        assert f"diff `{mode.footer_word}`" in warned, warned
        assert "not fully reviewed" in warned, warned
        assert "typical and consistent with a clean PR" not in warned, warned

    # No mode information (a direct render outside the PR path) keeps the size tiers.
    assert "0 findings on a small change" in render(small, None)
    print("ok  7. an empty result on partial/file-list input warns at ANY size")


def test_empty_result_on_partial_input_end_to_end():
    with scratch_dir() as tmp:
        wt, base = make_multi_file_tree(tmp, [800, 300, 800, 300])
        total = len(diff_text(wt, base))
        shutil.rmtree(wt)
        markdown, _err = render_pr_review(tmp, [800, 300, 800, 300], total - 1, [EMPTY_REVIEW])
    # 4 files / +4-4 lines is "small" by both size knobs — only the input mode makes
    # this a warning, which is exactly the org-gtd-cli#52 case #34 was filed over.
    assert "· findings `claude 0→0` · diff `partial 3/4 files`" in markdown, markdown
    assert "⚠️ 0 findings on a change the finder did not fully see" in markdown, markdown
    assert "small change" not in markdown, markdown
    print("ok  8. end-to-end: a 0→0 on a partially-inlined small diff renders the warning")


# ── 9. the #34 evidence case, replayed ───────────────────────────────────────
def test_replays_the_org_gtd_cli_52_case():
    """org-gtd-cli#52's second pass: 3 files, 61,313 chars, cap 60,000 — 2% over, and
    every hunk was dropped. Packing must inline files instead of none."""
    with scratch_dir() as tmp:
        review = fresh_review()
        review.DIFF_INLINE_CAP = 60000
        wt, base = make_multi_file_tree(tmp, [20000, 20000, 20000])
        tune_total_to(wt, base, [20000, 20000, 20000], 61313)
        assert len(diff_text(wt, base)) == 61313

        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.kind == "partial", mode
        assert mode.inlined_files >= 1 and mode.inlined_files < mode.total_files, mode
        assert mode.inlined_chars <= 60000, mode
        assert "```diff" in block and "Not inlined" in block
    print("ok  9. 3 files / 61,313 chars at cap 60,000 now inlines hunks, not nothing")


# ── 10. the shapes git emits that are not a plain content change ─────────────
def make_edge_case_tree(parent):
    """A tree exercising every per-file diff shape that is not a plain edit."""
    wt = os.path.join(parent, "edge-worktree")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True)
    with open(os.path.join(wt, "renamed-from.txt"), "w") as f:
        f.write("a\n" * 20)
    with open(os.path.join(wt, "deleted.txt"), "w") as f:
        f.write("gone\n")
    with open(os.path.join(wt, "mode.sh"), "w") as f:
        f.write("#!/bin/sh\n")
    with open(os.path.join(wt, "file with space.txt"), "w") as f:
        f.write("s\n")
    with open(os.path.join(wt, "tricky.txt"), "w") as f:
        f.write("z\n")
    with open(os.path.join(wt, "pic.bin"), "wb") as f:
        f.write(bytes(range(256)) * 4)
    _git(wt, "add", "-A")
    _git(wt, *GIT_ID, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD").strip()

    os.rename(os.path.join(wt, "renamed-from.txt"), os.path.join(wt, "renamed-to.txt"))
    os.remove(os.path.join(wt, "deleted.txt"))
    os.chmod(os.path.join(wt, "mode.sh"), 0o755)
    with open(os.path.join(wt, "file with space.txt"), "a") as f:
        f.write("more\n")
    # Content that MIMICS diff headers: git prefixes every hunk line with ' '/'+'/'-',
    # so neither line can be mistaken for a real header — pin that rather than assume it.
    with open(os.path.join(wt, "tricky.txt"), "a") as f:
        f.write("++ b/not-a-real-path.txt\n")
        f.write("diff --git a/fake b/fake\n")
    with open(os.path.join(wt, "new-file.txt"), "w") as f:
        f.write("new\n")
    with open(os.path.join(wt, "pic.bin"), "wb") as f:
        f.write(bytes(range(255, -1, -1)) * 4)
    _git(wt, "add", "-A")
    _git(wt, *GIT_ID, "commit", "-qm", "change")
    return wt, base


def test_handles_renames_deletions_modes_binaries_and_odd_names():
    with scratch_dir() as tmp:
        review = fresh_review()
        wt, base = make_edge_case_tree(tmp)
        diff = diff_text(wt, base)
        chunks = review.split_diff_by_file(diff)
        numstat = _git(wt, "diff", "--numstat", f"{base}..HEAD").splitlines()

        assert "".join(t for _, t in chunks) == diff, "splitting must round-trip exactly"
        assert len(chunks) == len(numstat), (
            f"{len(chunks)} chunks vs git's own {len(numstat)} files — a DiffMode "
            "file count that disagrees with git would make the footer lie"
        )
        # A rename reports its NEW name; a deletion its surviving old name; a binary and
        # a mode-only change have no ---/+++ pair at all and fall back to the header.
        assert [p for p, _ in chunks] == [
            "deleted.txt", "file with space.txt", "mode.sh", "new-file.txt",
            "pic.bin", "renamed-to.txt", "tricky.txt",
        ], [p for p, _ in chunks]

        # Packing over this tree still lists exactly the files it left out.
        sizes = {p: len(t) for p, t in chunks}
        review.DIFF_INLINE_CAP = sum(sizes.values()) - sizes["tricky.txt"]
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.kind == "partial" and mode.total_files == len(numstat), mode
        assert "- `tricky.txt`" in block, block[-300:]
    print("ok 10. renames, deletions, mode changes, binaries and odd names all parse")


# ── 11. git text output containing bytes outside UTF-8 ───────────────────────
def test_git_replaces_undecodable_diff_bytes():
    with scratch_dir() as tmp:
        wt = os.path.join(tmp, "undecodable-worktree")
        os.makedirs(wt)
        subprocess.run([GIT, "init", "-q", wt], check=True)
        _git(wt, *GIT_ID, "commit", "--allow-empty", "-qm", "base")
        base = _git(wt, "rev-parse", "HEAD").strip()
        with open(os.path.join(wt, "examples.db"), "wb") as f:
            f.write(b"caf\xe9 latin-1 line\n" * 40)
        _git(wt, "add", "examples.db")
        _git(wt, *GIT_ID, "commit", "-qm", "add NUL-free non-UTF-8 file")

        raw_diff = subprocess.run(
            [GIT, "-C", wt, "diff", f"{base}..HEAD"],
            check=True, capture_output=True,
        ).stdout
        assert b"Binary files" not in raw_diff, "fixture must exercise git's text diff path"
        assert b"\xe9" in raw_diff, "fixture must carry the undecodable byte into git output"

        review = fresh_review()
        proc = review.git(["diff", f"{base}..HEAD"], cwd=wt, auth=_Auth())
        assert "�" in proc.stdout, proc.stdout
    print("ok 11. git text output replaces undecodable bytes instead of raising")


def test_engines_replace_undecodable_output_bytes():
    with scratch_dir() as tmp:
        engine = os.path.join(tmp, "invalid-output.py")
        with open(engine, "w") as f:
            f.write("#!" + sys.executable + "\n")
            f.write("import sys\n")
            f.write("sys.stdin.read()\n")
            f.write("sys.stdout.buffer.write(b'answer: \\xff\\n')\n")
        os.chmod(engine, os.stat(engine).st_mode | stat.S_IEXEC | stat.S_IRWXU)

        review = fresh_review()
        review.CLAUDE_CMD = [engine]
        assert review.run_engine("claude", "prompt", tmp) == "answer: �\n"

        review.SELECT_CMD = [engine]
        selection = review.select_files_to_inline(
            [("file.py", "diff --git a/file.py b/file.py\n")],
            "file.py | 1 +\n", "(none found)", tmp,
        )
        assert selection.status == "unparsed", selection.status
        assert "�" in selection.detail, selection.detail
    print("ok 12. finder and selection engines replace undecodable output bytes")


TESTS = [
    test_split_is_lossless_and_per_file,
    test_boundary_unchanged_and_over_cap_packs,
    test_oversized_file_is_skipped_not_truncated,
    test_single_oversized_file_degrades_to_file_list,
    test_journal_footer_and_calibration_agree,
    test_footer_states_end_to_end,
    test_empty_result_on_partial_input_takes_the_warning_branch,
    test_empty_result_on_partial_input_end_to_end,
    test_replays_the_org_gtd_cli_52_case,
    test_handles_renames_deletions_modes_binaries_and_odd_names,
    test_git_replaces_undecodable_diff_bytes,
    test_engines_replace_undecodable_output_bytes,
]


def main():
    if not GIT:
        print("git not found on PATH", file=sys.stderr)
        return 1
    for test in TESTS:
        test()
    print(f"\nall {len(TESTS)} diff-packing tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

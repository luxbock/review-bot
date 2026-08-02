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


def fresh_review(path=None, name="review_diff_packing_test"):
    """review.py wired past its build-time @PLACEHOLDER@s so it runs from the checkout."""
    review = load_module(name, path or os.path.join(REPO_ROOT, "review.py"))
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


# Every git this file runs must be insulated from the developer's own config, not just
# from the system one. The golden fixture records raw `git diff` bytes, and a global
# `core.abbrev` silently rewrites the `index a..b` hashes — so without this the suite
# passes in CI and under `nix flake check` (where default.nix pins HOME and
# GIT_CONFIG_GLOBAL) while failing from a checkout, and the failure message would talk
# the developer into committing a config-tainted golden. The other suites
# (test_merge_base, test_head_sync, test_checkout_isolation) already pin both.
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git_env():
    return {**os.environ, **GIT_ENV}


class _Auth:
    def env(self):
        return git_env()


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
        errors="replace", env=git_env(),
    ).stdout


def make_multi_file_tree(parent, payloads):
    """Two commits touching len(payloads) files; file i's changed line carries
    payloads[i] 'x' characters. Returns (worktree, base_sha)."""
    wt = os.path.join(parent, "private-worktree")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True, env=git_env())
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
    subprocess.run([GIT, "init", "-q", wt], check=True, env=git_env())
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


# ── 11-14. git text output and compatibility across undecodable handling ─────
def make_undecodable_tree(parent, include_source=True):
    wt = os.path.join(parent, "undecodable-worktree")
    os.makedirs(wt)
    subprocess.run([GIT, "init", "-q", wt], check=True, env=git_env())
    _git(wt, *GIT_ID, "commit", "--allow-empty", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD").strip()
    if include_source:
        with open(os.path.join(wt, "ordinary.py"), "w") as f:
            f.write("print('readable')\n")
    with open(os.path.join(wt, "examples.db"), "wb") as f:
        f.write(b"caf\xe9 latin-1 line\n" * 40)
    _git(wt, "add", ".")
    _git(wt, *GIT_ID, "commit", "-qm", "add NUL-free non-UTF-8 file")
    raw_diff = subprocess.run(
        [GIT, "-C", wt, "diff", f"{base}..HEAD"], check=True, capture_output=True,
    ).stdout
    assert b"Binary files" not in raw_diff, "fixture must exercise git's text diff path"
    assert b"\xe9" in raw_diff, "fixture must carry the undecodable byte into git output"
    return wt, base


def test_git_replaces_undecodable_diff_bytes():
    with scratch_dir() as tmp:
        wt, base = make_undecodable_tree(tmp, include_source=False)

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


def test_undecodable_chunks_are_disclosed_but_never_inlined():
    with scratch_dir() as tmp:
        wt, base = make_undecodable_tree(tmp)
        trap = os.path.join(tmp, "selection-must-not-run.py")
        marker = os.path.join(tmp, "selection-ran")
        with open(trap, "w") as f:
            f.write("#!" + sys.executable + "\n")
            f.write("from pathlib import Path\n")
            f.write(f"Path({marker!r}).write_text('called')\n")
        os.chmod(trap, os.stat(trap).st_mode | stat.S_IEXEC | stat.S_IRWXU)

        review = fresh_review()
        review.SELECT_CMD = [trap]
        review.DIFF_INLINE_CAP = 100000
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        assert not os.path.exists(marker), "an under-cap diff must not invoke selection"
        assert mode.kind == "partial" and mode.undecodable_files == 1, mode
        assert (mode.inlined_files, mode.total_files) == (1, 2), mode
        assert mode.footer_word == "partial 1/2 files, 1 undecodable", mode.footer_word
        expected_journal = (
            f"1 of 2 files inlined, {mode.inlined_chars} of {mode.total_chars} chars, "
            "1 not valid UTF-8 (selection disabled: under cap)"
        )
        assert mode.journal_phrase == expected_journal, mode.journal_phrase
        assert "ordinary.py" in block and "print('readable')" in block, block
        assert "�" not in block, "replacement-character diff content must not be inlined"
        assert (
            "- `examples.db` (not valid UTF-8 — not readable as text)" in block
        ), block
        assert "some files are not valid UTF-8" in block, block
        assert expected_journal in err.getvalue(), err.getvalue()

        selected = review.DiffMode(
            "partial", 1, 2, mode.inlined_chars, mode.total_chars,
            selection=review.Selection("ok", ranked=["ordinary.py"]),
            undecodable_files=1,
        )
        assert selected.footer_word == "partial 1/2 files, selected, 1 undecodable"
        assert selected.journal_phrase.endswith(
            ", 1 not valid UTF-8 (selection ranked 1: ordinary.py)"
        ), selected.journal_phrase

    with scratch_dir() as tmp:
        wt, base = make_undecodable_tree(tmp, include_source=False)
        review = fresh_review()
        review.DIFF_INLINE_CAP = 100000
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.kind == "file-list" and mode.inlined_files == 0, mode
        assert mode.footer_word == "file-list, 1 undecodable", mode.footer_word
        assert mode.journal_phrase == "file-list only, 1 not valid UTF-8"
        assert "```diff" not in block and "�" not in block, block
        assert "diff is not valid UTF-8" in block, block
        assert "- `examples.db` (not valid UTF-8 — not readable as text)" in block
        assert "file-list only, 1 not valid UTF-8" in err.getvalue(), err.getvalue()
    print("ok 13. undecodable chunks are marked and excluded, including all-undecodable input")


GOLDEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "diff_packing_modes.json")


def _redact_merge_base(text, base):
    """Blank the one token that legitimately varies between runs.

    The file-list and partial hints embed the merge base ("run `git diff <sha>..HEAD`"),
    and the fixture worktree is built fresh each run, so its commit sha is new every
    time. Blob hashes in the `index a..b` lines are NOT redacted: those are derived
    from file content, so they are stable and worth pinning. Only the commit sha moves.
    """
    for n in (len(base), 12, 8, 7):
        text = text.replace(base[:n], "<merge-base>")
    return text


def zero_undecodable_outputs(tmp):
    """Every emitted byte of the four zero-undecodable modes, keyed by mode label.

    The fixture the caller compares against is deliberately a snapshot of the OUTPUT,
    not of an old review.py: an implementation baseline can only be read out of git
    history (so it cannot run in a sandbox, a shallow clone or an export) and cannot be
    updated when a wording change is intended — you would have to repoint it at a newer
    commit, silently redefining what is being asserted. A golden regenerates by intent
    (`python3 tests/test_diff_packing.py --update-goldens`) and shows exactly what moved.
    """
    review = fresh_review(name="review_diff_packing_golden")
    wt, base = make_multi_file_tree(tmp, [800, 300, 800, 300])
    total = len(diff_text(wt, base))
    selector = os.path.join(tmp, "selector.py")
    with open(selector, "w") as f:
        f.write("import json, sys\n")
        f.write("sys.stdin.read()\n")
        f.write("print(json.dumps({'files': ['file3.txt', 'file2.txt', "
                "'file1.txt', 'file0.txt']}))\n")

    cases = [
        ("inlined", total, [], False),
        ("partial", total - 1, [], False),
        ("partial selected", total - 1, [sys.executable, selector], True),
        ("file-list", 1, [], False),
    ]
    observed = {}
    for label, cap, select_cmd, expect_selected in cases:
        review.DIFF_INLINE_CAP = cap
        review.SELECT_CMD = list(select_cmd)
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())
        assert mode.selected is expect_selected, (label, mode)
        observed[label] = {
            "block": _redact_merge_base(block, base),
            "footer_word": mode.footer_word,
            "journal_phrase": _redact_merge_base(mode.journal_phrase, base),
        }
    # A redaction that silently stops matching would turn every golden vacuous, so
    # assert the token really was present where the renderer is supposed to emit it.
    assert "<merge-base>" in observed["file-list"]["block"], observed["file-list"]
    return observed


def update_goldens():
    with scratch_dir() as tmp:
        observed = zero_undecodable_outputs(tmp)
    os.makedirs(os.path.dirname(GOLDEN_FILE), exist_ok=True)
    with open(GOLDEN_FILE, "w") as f:
        json.dump(observed, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {GOLDEN_FILE} ({len(observed)} modes)")


def test_zero_undecodable_outputs_match_the_goldens():
    with open(GOLDEN_FILE) as f:
        expected = json.load(f)
    with scratch_dir() as tmp:
        observed = zero_undecodable_outputs(tmp)
    # A missing mode must fail as loudly as a changed one — comparing only the
    # shared keys would let a dropped mode pass as agreement.
    assert set(observed) == set(expected), (sorted(observed), sorted(expected))
    for label in sorted(expected):
        assert observed[label] == expected[label], (
            f"{label} output changed. If the change is intended, regenerate with\n"
            f"  python3 tests/test_diff_packing.py --update-goldens\n"
            f"and review the fixture diff.\n"
            f"golden={expected[label]!r}\nobserved={observed[label]!r}"
        )
    print("ok 14. zero-undecodable prompt/footer/journal bytes match the goldens "
          "in all four modes")


def test_literal_replacement_char_is_not_undecodable():
    """A valid UTF-8 file containing U+FFFD must stay inlinable.

    A U+FFFD sentinel cannot tell that file apart from one carrying undecodable bytes,
    so it would drop it from the finder's prompt under a marker asserting — falsely —
    that it is not readable as text. review.py is exactly such a file, so the sentinel
    version of this feature blinded the finder to its own implementation.
    """
    with scratch_dir() as tmp:
        wt = os.path.join(tmp, "mixed-worktree")
        os.makedirs(wt)
        subprocess.run([GIT, "init", "-q", wt], check=True, env=git_env())
        _git(wt, *GIT_ID, "commit", "--allow-empty", "-qm", "base")
        base = _git(wt, "rev-parse", "HEAD").strip()
        # Valid UTF-8 that happens to contain the replacement character, the way any
        # code testing for it does.
        with open(os.path.join(wt, "sentinel.py"), "w", encoding="utf-8") as f:
            f.write('MARKER = "�"  # a literal replacement character\n')
        with open(os.path.join(wt, "examples.db"), "wb") as f:
            f.write(b"caf\xe9 latin-1 line\n" * 40)
        _git(wt, "add", ".")
        _git(wt, *GIT_ID, "commit", "-qm", "sentinel plus genuinely undecodable file")

        review = fresh_review()
        review.SELECT_CMD = []
        review.DIFF_INLINE_CAP = 100000
        with contextlib.redirect_stderr(io.StringIO()):
            block, mode, _stats = review.changed_files_block(wt, base, _Auth())

        assert mode.undecodable_files == 1, mode
        assert (mode.inlined_files, mode.total_files) == (1, 2), mode
        assert "MARKER" in block, "a valid UTF-8 file must be inlined, U+FFFD or not"
        assert "- `sentinel.py`" not in block, block
        assert "- `examples.db` (not valid UTF-8 — not readable as text)" in block, block
    print("ok 15. a literal U+FFFD in valid UTF-8 is not mistaken for undecodable bytes")


def test_undecodability_is_decided_by_bytes_not_by_a_sentinel_character():
    """The classifier keys on lone surrogates, which valid UTF-8 can never produce.

    Also a standing guard on this repo's own sources: any tracked file containing a
    literal U+FFFD must stay inlinable, or the finder goes blind to it — silently, on
    every future PR that touches it.
    """
    review = fresh_review()
    assert not review.has_undecodable_bytes("plain ascii\n")
    # The whole point: a literal U+FFFD is ordinary valid text, not a decode failure.
    assert not review.has_undecodable_bytes('MARKER = "�"\n')
    assert review.has_undecodable_bytes(b"caf\xe9\n".decode("utf-8", "surrogateescape"))

    checked = 0
    for rel in ("review.py", "client.py", "serve.py", "tests/test_diff_packing.py"):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
            source = f.read()
        if "�" not in source:
            continue
        checked += 1
        assert not review.has_undecodable_bytes(source), (
            f"{rel} classified as undecodable — the finder would never see this file"
        )
    assert checked, "guard is vacuous: no tracked source carries a literal U+FFFD"
    print("ok 16. undecodability keys on bytes, so a literal U+FFFD stays inlinable")


DEPLOYED_DIFF_CAP = 250000
"""What nixos-config's hosts/convox/review-bot-service.nix sets REVIEW_BOT_DIFF_CAP to.

Not importable from here — it lives in another repo — so it is restated, and the test
below is what makes the restatement load-bearing rather than a stale comment.
"""


def test_default_cap_matches_the_deployed_service_and_the_readme():
    """The unforced default must equal what production runs, and the README must say so.

    These drifted apart silently: the default stayed at the nixos-config import's 60000
    while the deployed unit was tuned to 250000, so `review-bot-review-local` — the
    binary tools/finder_ab.py drives — showed the finder a quarter of production's diff.
    An A/B measurement taken that way describes an instrument nobody runs, and nothing
    failed for months. A restated constant with no test is exactly the drift class this
    repo keeps getting bitten by, so pin all three faces of it here.
    """
    env = dict(os.environ)
    os.environ.pop("REVIEW_BOT_DIFF_CAP", None)
    try:
        review = fresh_review(name="review_default_cap_test")
    finally:
        os.environ.clear()
        os.environ.update(env)
    assert review.DIFF_INLINE_CAP == DEPLOYED_DIFF_CAP, review.DIFF_INLINE_CAP

    readme = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    claim = f"default {DEPLOYED_DIFF_CAP} chars"
    assert claim in readme, f"README no longer states {claim!r}"
    # The journal example must stay coherent with the cap it prints: an over-cap diff is
    # what produces a `partial` line, so an example whose size fits under the cap would
    # illustrate a mode it could not actually reach.
    example = re.search(r"diff (\d+) chars vs cap (\d+) — (\d+) of (\d+) files inlined",
                        readme)
    assert example, "README's partial journal example is gone or reshaped"
    total, cap, inlined_files, all_files = (int(g) for g in example.groups())
    assert cap == DEPLOYED_DIFF_CAP, cap
    assert total > cap, (total, cap)
    assert inlined_files < all_files, (inlined_files, all_files)
    print(f"ok 17. the default cap, the deployed unit and the README all say "
          f"{DEPLOYED_DIFF_CAP}")


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
    test_undecodable_chunks_are_disclosed_but_never_inlined,
    test_zero_undecodable_outputs_match_the_goldens,
    test_literal_replacement_char_is_not_undecodable,
    test_undecodability_is_decided_by_bytes_not_by_a_sentinel_character,
    test_default_cap_matches_the_deployed_service_and_the_readme,
]


def main():
    if not GIT:
        print("git not found on PATH", file=sys.stderr)
        return 1
    if "--update-goldens" in sys.argv[1:]:
        update_goldens()
        return 0
    for test in TESTS:
        test()
    print(f"\nall {len(TESTS)} diff-packing tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

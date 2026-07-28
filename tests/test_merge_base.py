#!/usr/bin/env python3
"""Acceptance tests for forge-recorded merge-base selection and empty-diff refusal.

Stdlib only: the suite imports the real review.py and drives real git repositories.
The bare origin models an already-merged PR by fast-forwarding its branch into main
while leaving refs/pull/1/head at the PR tip. A later main-only commit keeps that
topology realistic and supplies a locally-known commit that is not a PR-head ancestor.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
NO_RECORDED_BASE = object()


def load_review():
    spec = importlib.util.spec_from_file_location(
        "review_merge_base", os.path.join(REPO_ROOT, "review.py")
    )
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    review.GIT = shutil.which("git")
    assert review.GIT, "git not on PATH"
    return review


def run_git(args, cwd):
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.path.join(cwd, ".throwaway-gitconfig"),
        }
    )
    return subprocess.run(
        [shutil.which("git"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


class MergeBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-merge-base-")
        self.review = load_review()
        self.review.CACHE_ROOT = os.path.join(self.tmp, "cache")

        self.owner, self.repo = "acme", "widget"
        self.forge_dir = os.path.join(self.tmp, "forge")
        self.bare = os.path.join(self.forge_dir, self.owner, f"{self.repo}.git")
        os.makedirs(self.bare)
        run_git(["init", "--quiet", "--bare", self.bare], cwd=self.forge_dir)

        self.work = os.path.join(self.tmp, "work")
        run_git(["clone", "--quiet", self.bare, self.work], cwd=self.tmp)
        run_git(["checkout", "-q", "-b", "main"], cwd=self.work)
        with open(os.path.join(self.work, "base.txt"), "w") as f:
            f.write("base\n")
        run_git(["add", "."], cwd=self.work)
        run_git(["commit", "-q", "-m", "base"], cwd=self.work)
        run_git(["push", "-q", "origin", "main"], cwd=self.work)
        self.base_sha = run_git(["rev-parse", "HEAD"], cwd=self.work).stdout.strip()

        run_git(["checkout", "-q", "-b", "pr1"], cwd=self.work)
        with open(os.path.join(self.work, "pr-change.txt"), "w") as f:
            f.write("content changed by the PR\n")
        run_git(["add", "."], cwd=self.work)
        run_git(["commit", "-q", "-m", "pr: add changed file"], cwd=self.work)
        self.pr_sha = run_git(["rev-parse", "HEAD"], cwd=self.work).stdout.strip()
        run_git(
            ["push", "-q", "origin", "pr1:refs/pull/1/head"], cwd=self.work
        )

        # Merge the PR by fast-forwarding main, then advance main once more. The pull
        # ref deliberately stays at the PR tip, so live merge-base(main, pull-head)
        # collapses to the pull head exactly as it does for an already-merged PR.
        run_git(["checkout", "-q", "main"], cwd=self.work)
        run_git(["merge", "-q", "--ff-only", "pr1"], cwd=self.work)
        run_git(["push", "-q", "origin", "main"], cwd=self.work)
        with open(os.path.join(self.work, "main-after-merge.txt"), "w") as f:
            f.write("main advanced after the merge\n")
        run_git(["add", "."], cwd=self.work)
        run_git(["commit", "-q", "-m", "main: advance after merge"], cwd=self.work)
        self.main_after_sha = run_git(
            ["rev-parse", "HEAD"], cwd=self.work
        ).stdout.strip()
        run_git(["push", "-q", "origin", "main"], cwd=self.work)

        published_head = run_git(
            ["rev-parse", "refs/pull/1/head"], cwd=self.bare
        ).stdout.strip()
        self.assertEqual(published_head, self.pr_sha)

        self.review.FORGE_URL = "file://" + self.forge_dir
        self.auth = self.review.GitAuth("dummy-token")

    def tearDown(self):
        self.auth.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self, recorded_merge_base=NO_RECORDED_BASE, repo_dir=None):
        kwargs = {"expected_head": self.pr_sha}
        if recorded_merge_base is not NO_RECORDED_BASE:
            kwargs["recorded_merge_base"] = recorded_merge_base
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            checkout, merge_base = self.review.prepare_checkout(
                self.owner,
                self.repo,
                1,
                "main",
                self.auth,
                repo_dir,
                **kwargs,
            )
        return checkout, merge_base, stderr.getvalue()

    def assert_single_merge_base_line(self, stderr, expected):
        lines = [line for line in stderr.splitlines() if "merge base " in line]
        self.assertEqual(lines, [f"review-bot-review: {expected}"])

    def test_usable_recorded_base_preserves_merged_pr_diff(self):
        checkout, merge_base, journal = self._prepare(self.base_sha)
        with checkout:
            # changed_files_block returns (block, inlined) since issue #21's
            # instrumentation landed — unpack, or `assertIn` would test tuple
            # membership rather than the substring it looks like it tests.
            diff_block, inlined = self.review.changed_files_block(
                checkout.wt, merge_base, self.auth
            )

        self.assertIn("pr-change.txt", diff_block)
        self.assertIn("+content changed by the PR", diff_block)
        self.assertTrue(inlined)
        self.assert_single_merge_base_line(
            journal, f"merge base {self.base_sha[:12]} (forge-recorded)"
        )

    def test_absent_recorded_base_documents_live_collapse(self):
        checkout, merge_base, journal = self._prepare()
        with checkout:
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()
            diff = self.review.git(
                ["diff", f"{merge_base}..HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout

        self.assertEqual(merge_base, self.pr_sha)
        self.assertEqual(head, self.pr_sha)
        self.assertEqual(diff, "")
        self.assert_single_merge_base_line(
            journal,
            f"merge base {self.pr_sha[:12]} "
            "(computed live — forge merge_base absent)",
        )

    def test_non_ancestor_recorded_base_falls_back_live(self):
        checkout, merge_base, journal = self._prepare(self.main_after_sha)
        with checkout:
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()

        self.assertEqual(merge_base, self.pr_sha)
        self.assertEqual(head, self.pr_sha)
        self.assert_single_merge_base_line(
            journal,
            f"merge base {self.pr_sha[:12]} (computed live — forge merge_base "
            f"{self.main_after_sha[:12]} not an ancestor of head {self.pr_sha[:12]})",
        )

    def test_empty_recorded_base_falls_back_on_repo_dir_path(self):
        checkout, merge_base, journal = self._prepare("", repo_dir=self.work)
        with checkout:
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()

        self.assertEqual(merge_base, self.pr_sha)
        self.assertEqual(head, self.pr_sha)
        self.assert_single_merge_base_line(
            journal,
            f"merge base {self.pr_sha[:12]} "
            "(computed live — forge merge_base absent)",
        )

    def test_empty_diff_raises_instead_of_rendering_pass(self):
        checkout, merge_base, _journal = self._prepare()
        with checkout:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    self.review.changed_files_block(
                        checkout.wt, merge_base, self.auth
                    )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn(
            f"empty diff at merge base {merge_base[:12]} — nothing to review",
            stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

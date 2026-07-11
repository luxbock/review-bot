#!/usr/bin/env python3
"""Acceptance test for issue #1: concurrent runs against the SAME repo must each get a
private, isolated worktree so a review is never generated against the wrong PR's diff.

Stdlib only. No engine, no live forge. We stand up a local bare "origin" with two PR
refs touching DIFFERENT files, then drive review.prepare_checkout CONCURRENTLY for PR 1
and PR 2 and assert each returned worktree's diff shows EXACTLY its own PR's file.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)


def load_review():
    """Import review.py as a module and wire up the build-time placeholders the checkout
    path needs (GIT + CACHE_ROOT). The prompt-file placeholders are irrelevant here."""
    spec = importlib.util.spec_from_file_location("review", os.path.join(REPO_ROOT, "review.py"))
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    review.GIT = shutil.which("git")
    assert review.GIT, "git not on PATH"
    return review


def run_git(args, cwd, env=None):
    e = dict(os.environ)
    # deterministic, side-effect-free identity so commits work in a bare CI sandbox
    e.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.path.join(cwd, ".throwaway-gitconfig"),
        }
    )
    if env:
        e.update(env)
    return subprocess.run(
        [shutil.which("git"), *args], cwd=cwd, env=e, capture_output=True, text=True, check=True
    )


class CheckoutIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-iso-test-")
        self.review = load_review()
        self.review.CACHE_ROOT = os.path.join(self.tmp, "cache")

        # ── build a bare "origin" with two PR refs touching DIFFERENT files ─────
        self.owner, self.repo = "acme", "widget"
        forge_dir = os.path.join(self.tmp, "forge")
        bare = os.path.join(forge_dir, self.owner, f"{self.repo}.git")
        os.makedirs(bare)
        run_git(["init", "--quiet", "--bare", bare], cwd=forge_dir)

        # a work clone to author the base + two PR branches
        work = os.path.join(self.tmp, "work")
        run_git(["clone", "--quiet", bare, work], cwd=self.tmp)
        run_git(["checkout", "-q", "-b", "main"], cwd=work)
        with open(os.path.join(work, "base.txt"), "w") as f:
            f.write("base\n")
        run_git(["add", "."], cwd=work)
        run_git(["commit", "-q", "-m", "base"], cwd=work)
        run_git(["push", "-q", "origin", "main"], cwd=work)
        base_sha = run_git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

        # PR 1 touches pr1.txt; PR 2 touches pr2.txt — both descend from base.
        shas = {}
        for pr, fname in ((1, "pr1.txt"), (2, "pr2.txt")):
            run_git(["checkout", "-q", "-B", f"pr{pr}", base_sha], cwd=work)
            with open(os.path.join(work, fname), "w") as f:
                f.write(f"content for {fname}\n")
            run_git(["add", "."], cwd=work)
            run_git(["commit", "-q", "-m", f"pr{pr}: add {fname}"], cwd=work)
            shas[pr] = run_git(["rev-parse", "HEAD"], cwd=work).stdout.strip()
            # push the branch so the objects land in the bare origin, then publish it as
            # a Forgejo-style refs/pull/N/head ref pointing at that object.
            run_git(["push", "-q", "origin", f"pr{pr}:refs/pull/{pr}/head"], cwd=work)

        self.expected_file = {1: "pr1.txt", 2: "pr2.txt"}

        # Point the checkout at the local bare repo: ensure_clone builds
        # f"{FORGE_URL}/{owner}/{repo}.git", so FORGE_URL = file://<forge_dir>.
        self.review.FORGE_URL = "file://" + forge_dir

        # A real GitAuth just writes a throwaway gitconfig — fine against a local path.
        self.auth = self.review.GitAuth("dummy-token")

    def tearDown(self):
        self.auth.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_prs_are_isolated(self):
        # Warm the shared clone first. The shared-store lock deliberately covers only the
        # fetch + worktree-add ref updates (not the one-time initial clone), matching the
        # real deployment where the repo cache is already present before concurrent runs.
        self.review.ensure_clone(self.owner, self.repo, self.auth, None)

        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def worker(pr):
            try:
                barrier.wait()  # maximise overlap on the shared store
                checkout, merge_base = self.review.prepare_checkout(
                    self.owner, self.repo, pr, "main", self.auth, None
                )
                # Read the private tree's diff BEFORE cleanup, exactly like the engine
                # would explore checkout.wt for the whole run.
                diff = self.review.git(
                    ["diff", "--name-only", f"{merge_base}..HEAD"], cwd=checkout.wt, auth=self.auth
                ).stdout.strip()
                results[pr] = {"wt": checkout.wt, "diff": diff, "merge_base": merge_base}
                # hold the tree a beat so both runs coexist, then clean up
                import time

                time.sleep(0.2)
                checkout.__exit__(None, None, None)
            except BaseException as e:  # noqa: BLE001 — record, assert on main thread
                errors[pr] = e

        threads = [threading.Thread(target=worker, args=(pr,)) for pr in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, {}, f"worker(s) raised: {errors}")

        # Each worktree's diff shows EXACTLY its own PR's file — no cross-contamination.
        for pr in (1, 2):
            self.assertIn(pr, results)
            files = [ln for ln in results[pr]["diff"].splitlines() if ln]
            self.assertEqual(
                files,
                [self.expected_file[pr]],
                f"PR {pr} worktree diff was {files}, expected [{self.expected_file[pr]}]",
            )

        # The two worktrees were distinct dirs.
        self.assertNotEqual(results[1]["wt"], results[2]["wt"])

        # After cleanup: both worktrees are gone and .wt/ has not accumulated.
        cdir = os.path.join(self.review.CACHE_ROOT, f"{self.owner}__{self.repo}")
        for pr in (1, 2):
            self.assertFalse(os.path.exists(results[pr]["wt"]), f"PR {pr} worktree not removed")
        wt_root = os.path.join(cdir, ".wt")
        leftover = os.listdir(wt_root) if os.path.isdir(wt_root) else []
        self.assertEqual(leftover, [], f".wt/ accumulated leftovers: {leftover}")

        # git's own worktree list should be back to just the main clone.
        listing = self.review.git(["worktree", "list", "--porcelain"], cwd=cdir, auth=self.auth).stdout
        self.assertEqual(listing.count("worktree "), 1, f"stray worktrees registered:\n{listing}")

    def test_issue_head_checkout_isolated_and_cleaned(self):
        checkout, head = self.review.prepare_head_checkout(
            self.owner, self.repo, "main", self.auth, None
        )
        self.assertTrue(os.path.isdir(checkout.wt))
        self.assertTrue(os.path.exists(os.path.join(checkout.wt, "base.txt")))
        wt = checkout.wt
        checkout.__exit__(None, None, None)
        self.assertFalse(os.path.exists(wt), "issue-mode worktree not removed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

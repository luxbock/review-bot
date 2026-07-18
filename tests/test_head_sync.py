#!/usr/bin/env python3
"""Acceptance tests for issue #16: the push→review race in review-bot-review.

Forgejo populates ``refs/pull/N/head`` asynchronously after a branch push, so a
review fired seconds after a push can fetch the *pre-push* commit and post a
finding citing code the pushed head already removed. ``prepare_checkout`` now
compares the checked-out head SHA against the API-reported ``meta["head"]["sha"]``
and re-fetches with bounded backoff until it converges — or aborts with a
distinct error rather than review a stale tree.

Stdlib only. No engine, no live forge. We stand up the same bare
``file://``-origin harness ``test_checkout_isolation.py`` uses (which already
publishes ``refs/pull/N/head`` refs) and drive the four documented scenarios.
"""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)


def load_review():
    """Import review.py as a module and wire up the build-time placeholders the checkout
    path needs (GIT + CACHE_ROOT). The prompt-file placeholders are irrelevant here."""
    spec = importlib.util.spec_from_file_location("review_head_sync", os.path.join(REPO_ROOT, "review.py"))
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    review.GIT = shutil.which("git")
    assert review.GIT, "git not on PATH"
    return review


def run_git(args, cwd, env=None):
    e = dict(os.environ)
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


class HeadSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rb-head-sync-")
        self.review = load_review()
        self.review.CACHE_ROOT = os.path.join(self.tmp, "cache")
        # Fast retries so mismatch tests don't sleep real seconds.
        self.review.HEAD_SYNC_BASE_SECS = 0.01

        # ── build a bare "origin" with base + two commits on branch pr1 ─────────
        self.owner, self.repo = "acme", "widget"
        self.forge_dir = os.path.join(self.tmp, "forge")
        self.bare = os.path.join(self.forge_dir, self.owner, f"{self.repo}.git")
        os.makedirs(self.bare)
        run_git(["init", "--quiet", "--bare", self.bare], cwd=self.forge_dir)

        work = os.path.join(self.tmp, "work")
        run_git(["clone", "--quiet", self.bare, work], cwd=self.tmp)
        run_git(["checkout", "-q", "-b", "main"], cwd=work)
        with open(os.path.join(work, "base.txt"), "w") as f:
            f.write("base\n")
        run_git(["add", "."], cwd=work)
        run_git(["commit", "-q", "-m", "base"], cwd=work)
        run_git(["push", "-q", "origin", "main"], cwd=work)
        self.base_sha = run_git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

        # OLD PR head: the pre-push commit that a stale refs/pull/1/head still points at.
        run_git(["checkout", "-q", "-B", "pr1", self.base_sha], cwd=work)
        with open(os.path.join(work, "old.txt"), "w") as f:
            f.write("old-pr-content\n")
        run_git(["add", "."], cwd=work)
        run_git(["commit", "-q", "-m", "pr1: old (pre-push)"], cwd=work)
        self.old_sha = run_git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

        # NEW PR head: what meta["head"]["sha"] reports after the push. Same branch,
        # one commit forward, touching a different file so the diff is unambiguous.
        with open(os.path.join(work, "new.txt"), "w") as f:
            f.write("new-pr-content\n")
        run_git(["add", "."], cwd=work)
        run_git(["commit", "-q", "-m", "pr1: new (the pushed head)"], cwd=work)
        self.new_sha = run_git(["rev-parse", "HEAD"], cwd=work).stdout.strip()

        # Push both commits' objects into the bare so either SHA is fetchable.
        run_git(["push", "-q", "origin", "pr1:refs/heads/pr1"], cwd=work)
        self.work = work

        self.review.FORGE_URL = "file://" + self.forge_dir
        self.auth = self.review.GitAuth("dummy-token")

    def tearDown(self):
        self.auth.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish_pull_ref(self, sha):
        """Set the bare origin's refs/pull/1/head to ``sha`` — models what Forgejo
        does asynchronously after a push."""
        run_git(["update-ref", "refs/pull/1/head", sha], cwd=self.bare)

    # ── 1. Stale ref, no convergence → abort. ──────────────────────────────────
    def test_stale_ref_no_convergence_aborts(self):
        self._publish_pull_ref(self.old_sha)
        self.review.HEAD_SYNC_RETRIES = 0  # single-shot: fetch once, mismatch, die
        with self.assertRaises(SystemExit):
            self.review.prepare_checkout(
                self.owner, self.repo, 1, "main", self.auth, None,
                expected_head=self.new_sha,
            )
        # No worktree should have been carved.
        cdir = os.path.join(self.review.CACHE_ROOT, f"{self.owner}__{self.repo}")
        wt_root = os.path.join(cdir, ".wt")
        leftover = os.listdir(wt_root) if os.path.isdir(wt_root) else []
        self.assertEqual(leftover, [], f".wt/ leaked a worktree after aborting: {leftover}")

    # ── 2. Stale then converges → succeeds at the right head. ──────────────────
    def test_stale_then_converges_lands_on_new_head(self):
        # Start stale; on the second pull-ref fetch, advance the origin's pull ref
        # to the expected SHA (models Forgejo finishing propagation between attempts).
        self._publish_pull_ref(self.old_sha)
        self.review.HEAD_SYNC_RETRIES = 3  # budget=4 attempts total

        real_git = self.review.git
        fetch_count = {"n": 0}

        def advancing_git(args, *a, **kw):
            if args and args[0] == "fetch" and any("refs/pull/1/head" in x for x in args):
                fetch_count["n"] += 1
                # Advance on the second attempt: the first fetch still lands on OLD,
                # then between attempt-0's rev-parse (mismatch → sleep) and attempt-1's
                # fetch, Forgejo has "caught up" and republished pointing at NEW.
                if fetch_count["n"] == 2:
                    self._publish_pull_ref(self.new_sha)
            return real_git(args, *a, **kw)

        self.review.git = advancing_git
        try:
            checkout, merge_base = self.review.prepare_checkout(
                self.owner, self.repo, 1, "main", self.auth, None,
                expected_head=self.new_sha,
            )
        finally:
            self.review.git = real_git

        try:
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()
            self.assertEqual(head, self.new_sha, "worktree HEAD did not converge on the expected SHA")

            # The merge_base..HEAD diff must reflect the NEW commit (touches new.txt),
            # not the OLD one — proving we didn't review the stale tree.
            diff = self.review.git(
                ["diff", "--name-only", f"{merge_base}..HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip().splitlines()
            self.assertIn("new.txt", diff, f"expected new.txt in diff, got {diff}")

            # And we actually did >1 fetch — otherwise the retry didn't fire.
            self.assertGreater(fetch_count["n"], 1, "expected at least one re-fetch")
        finally:
            checkout.__exit__(None, None, None)

    # ── 3. Matching head → unchanged. ──────────────────────────────────────────
    def test_matching_head_takes_no_extra_fetches(self):
        # If the pull ref already matches expected_head, we must NOT re-fetch.
        # Guards against a regression that always retries/raises.
        self._publish_pull_ref(self.new_sha)
        self.review.HEAD_SYNC_RETRIES = 3

        real_git = self.review.git
        pull_fetches = {"n": 0}

        def counting_git(args, *a, **kw):
            if args and args[0] == "fetch" and any("refs/pull/1/head" in x for x in args):
                pull_fetches["n"] += 1
            return real_git(args, *a, **kw)

        self.review.git = counting_git
        try:
            checkout, merge_base = self.review.prepare_checkout(
                self.owner, self.repo, 1, "main", self.auth, None,
                expected_head=self.new_sha,
            )
        finally:
            self.review.git = real_git

        try:
            self.assertEqual(pull_fetches["n"], 1, f"expected exactly one pull-ref fetch, got {pull_fetches['n']}")
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()
            self.assertEqual(head, self.new_sha)
            diff = self.review.git(
                ["diff", "--name-only", f"{merge_base}..HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip().splitlines()
            self.assertIn("new.txt", diff)
        finally:
            checkout.__exit__(None, None, None)

    # ── 4. expected_head=None → check skipped. ─────────────────────────────────
    def test_expected_head_none_skips_check(self):
        # The --repo-dir path (and any other caller that omits expected_head) must
        # see the legacy behaviour: fetch once, no comparison, no abort — even if
        # the pull ref is stale.
        self._publish_pull_ref(self.old_sha)
        self.review.HEAD_SYNC_RETRIES = 0  # would abort immediately if the check ran

        checkout, merge_base = self.review.prepare_checkout(
            self.owner, self.repo, 1, "main", self.auth, None,
            expected_head=None,
        )
        try:
            head = self.review.git(
                ["rev-parse", "HEAD"], cwd=checkout.wt, auth=self.auth
            ).stdout.strip()
            self.assertEqual(head, self.old_sha, "with expected_head=None we should land on whatever the pull ref pointed at")
        finally:
            checkout.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)

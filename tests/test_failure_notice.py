#!/usr/bin/env python3
"""Acceptance tests for the in-band failure notice — the visible half of refusing a
fragment.

Before this existed a run that could not produce a usable result posted NOTHING, and
callers waiting on a reply — agents poll their own PRs for one — blocked on a comment
that was never coming. The notice is posted by review.py itself at the moment it aborts
(single attempt, no retry), armed only when the poller passes --post-failure-notice on
a trigger's final automatic attempt. Stdlib only; no live forge.

Run:  python3 tests/test_failure_notice.py
"""

import contextlib
import importlib.util
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_review():
    return load_module("review_failure_notice_test", os.path.join(REPO_ROOT, "review.py"))


def fresh_poll():
    return load_module("poll_failure_notice_test", os.path.join(REPO_ROOT, "poll.py"))


class RecordingApi:
    """Stands in for review.api, capturing every POST body."""

    def __init__(self, explode=False):
        self.posts = []
        self.explode = explode

    def __call__(self, method, path, token, data=None):
        if self.explode:
            # The real api() reacts to an HTTP error by calling die(), which raises
            # SystemExit — reproduce that exact failure shape.
            raise SystemExit(1)
        self.posts.append((method, path, token, data))
        return {"html_url": "http://forge.example/c/1"}


def expect_exit(fn, code=1):
    try:
        fn()
    except SystemExit as e:
        assert e.code == code, e.code
        return
    raise AssertionError("expected SystemExit")


def test_armed_die_posts_one_notice_headline_only():
    review = fresh_review()
    rec = RecordingApi()
    review.api = rec
    review.arm_failure_notice("acme", "widget", 7, "pr", 3, "tok")
    with contextlib.redirect_stderr(io.StringIO()):
        expect_exit(lambda: review.die("engine returned garbage\nRAW ENGINE OUTPUT LINE"))
    assert len(rec.posts) == 1, rec.posts
    method, path, token, data = rec.posts[0]
    assert (method, path, token) == ("POST", "repos/acme/widget/issues/7/comments", "tok")
    body = data["body"]
    assert review.FAIL_MARKER in body, body
    assert "@olli" in body and "3 time(s)" in body, body
    assert "nothing here was reviewed" in body, body
    assert "Reason: `engine returned garbage`" in body, body
    # die() appends engine output after the first line; it must stay out of the comment.
    assert "RAW ENGINE OUTPUT LINE" not in body, body
    assert "--pr 7" in body, body
    # The notice must never be countable as a review round.
    assert "Automated review by **review-bot**" not in body, body
    print("ok  1. armed die() posts one notice: marker, ping, attempts, headline only")


def test_notice_matches_feedback_classification():
    review = fresh_review()
    feedback = load_module("feedback_failure_notice_test", os.path.join(REPO_ROOT, "feedback.py"))
    assert review.FAIL_MARKER == feedback.FAIL_MARKER
    rec = RecordingApi()
    review.api = rec
    review.arm_failure_notice("acme", "widget", 11, "issue", 2, "tok")
    with contextlib.redirect_stderr(io.StringIO()):
        expect_exit(lambda: review.die("boom"))
    body = rec.posts[0][3]["body"]
    assert feedback.classify(body) == "failed", body
    # Triage phrasing follows the mode, and the retry hint targets the issue.
    assert "nothing here was triaged" in body and "--issue 11" in body, body
    print("ok  2. the posted notice classifies as `failed` and speaks the issue dialect")


def test_unarmed_die_posts_nothing():
    review = fresh_review()
    rec = RecordingApi()
    review.api = rec
    with contextlib.redirect_stderr(io.StringIO()):
        expect_exit(lambda: review.die("plain CLI failure"), code=1)
    assert rec.posts == [], rec.posts
    print("ok  3. an unarmed die() (direct CLI use) posts nothing")


def test_notice_fires_once_and_swallows_its_own_delivery_failure():
    review = fresh_review()
    rec = RecordingApi(explode=True)
    review.api = rec
    review.arm_failure_notice("acme", "widget", 7, "pr", 3, "tok")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        # The POST raises SystemExit (api()'s die() path); the original abort code must
        # win and the notice must not retry or recurse.
        expect_exit(lambda: review.die("original failure", code=7), code=7)
    assert review.FAILURE_NOTICE is None
    assert "could not post the failure notice" in err.getvalue(), err.getvalue()
    # A later die() in the same process (cleanup) must not re-attempt.
    rec.explode = False
    with contextlib.redirect_stderr(io.StringIO()):
        expect_exit(lambda: review.die("cleanup failure"))
    assert rec.posts == [], rec.posts
    print("ok  4. delivery failure is swallowed once; the original exit code survives")


def test_successful_post_disarms():
    review = fresh_review()
    rec = RecordingApi()
    review.api = rec
    review.arm_failure_notice("acme", "widget", 7, "pr", 3, "tok")

    class _Args:
        mode, owner, repo, pr, issue, print_only = "pr", "acme", "widget", 7, None, False

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        review.post_or_print(_Args(), "tok", "## verdict body", "review")
        # The verdict reached the forge; a die() during cleanup must stay silent.
        expect_exit(lambda: review.die("worktree cleanup failed"))
    bodies = [d["body"] for _m, _p, _t, d in rec.posts]
    assert bodies == ["## verdict body"], bodies
    print("ok  5. a posted verdict disarms the notice — cleanup failures stay silent")


def test_main_arms_only_for_poller_shaped_invocations():
    review = fresh_review()
    review.load_token = lambda: "tok"
    armed = []
    review.arm_failure_notice = lambda *a: armed.append(a)

    class _NoAuth:
        def __init__(self, token):
            pass

        def cleanup(self):
            pass

    review.GitAuth = _NoAuth
    review.do_pr_review = lambda *a, **k: None

    base = ["review-bot-review", "--owner", "acme", "--repo", "widget", "--pr", "7"]
    for argv, expect in [
        (base + ["--post-failure-notice", "3"], [("acme", "widget", 7, "pr", 3, "tok")]),
        (base + ["--post-failure-notice", "3", "--print-only"], []),
        (base + ["--post-failure-notice", "3", "--dry-run"], []),
        (base, []),
    ]:
        armed.clear()
        old = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                review.main()
        finally:
            sys.argv = old
        assert armed == expect, (argv, armed)
    print("ok  6. main() arms for the poller only — consults and dry runs stay silent")


def test_poll_passes_flag_on_final_attempt_only():
    poll = fresh_poll()
    assert poll.MAX_FAILS == 3  # the arithmetic below assumes the default
    assert poll.notice_attempts_for(0) == 0
    assert poll.notice_attempts_for(1) == 0
    assert poll.notice_attempts_for(2) == 3
    assert poll.notice_attempts_for(7) == 8  # over-tally still discloses honestly

    calls = []

    class _Proc:
        returncode = 0
        stdout = "url"
        stderr = ""

    poll.subprocess.run = lambda cmd, capture_output, text: calls.append(cmd) or _Proc()
    with contextlib.redirect_stderr(io.StringIO()):
        poll.run_review("acme", "widget", 7, "claude", "standard", "", "")
        poll.run_review("acme", "widget", 7, "claude", "standard", "", "", notice_attempts=3)
    assert "--post-failure-notice" not in calls[0], calls[0]
    assert calls[1][calls[1].index("--post-failure-notice") + 1] == "3", calls[1]
    print("ok  7. poll passes --post-failure-notice on the final attempt only")


def main():
    tests = [
        test_armed_die_posts_one_notice_headline_only,
        test_notice_matches_feedback_classification,
        test_unarmed_die_posts_nothing,
        test_notice_fires_once_and_swallows_its_own_delivery_failure,
        test_successful_post_disarms,
        test_main_arms_only_for_poller_shaped_invocations,
        test_poll_passes_flag_on_final_attempt_only,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

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
import json
import os
import runpy
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE_PATH = os.path.join(REPO_ROOT, "serve.py")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_review():
    return load_module("review_failure_notice_test", os.path.join(REPO_ROOT, "review.py"))


def fresh_poll():
    return load_module("poll_failure_notice_test", os.path.join(REPO_ROOT, "poll.py"))


def fresh_client():
    return load_module("client_failure_notice_test", os.path.join(REPO_ROOT, "client.py"))


class RecordingApi:
    """Stands in for review.api, capturing every POST body. `explode` is the exception
    instance a delivery attempt raises: SystemExit(1) mimics the CLI path (api() ->
    die() -> sys.exit), a plain Exception mimics the serve path (die() rebound to raise
    ReviewFailure)."""

    def __init__(self, explode=None):
        self.posts = []
        self.explode = explode

    def __call__(self, method, path, token, data=None):
        if self.explode is not None:
            raise self.explode
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
    rec = RecordingApi(explode=SystemExit(1))
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
    rec.explode = None
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


def test_poll_argv_is_accepted_by_the_shipped_client():
    """poll dispatches through review-bot-review, which is client.py, NOT review.py —
    the flag must survive the whole shipped path: poll argv -> client argparse ->
    request field. An unrecognized flag would SystemExit(2) out of client.main() here."""
    poll = fresh_poll()
    client = fresh_client()
    calls = []

    class _Proc:
        returncode = 0
        stdout = "url"
        stderr = ""

    poll.subprocess.run = lambda cmd, capture_output, text: calls.append(cmd) or _Proc()
    with contextlib.redirect_stderr(io.StringIO()):
        poll.run_review("acme", "widget", 7, "claude", "standard", "", "", notice_attempts=3)
        poll.run_review("acme", "widget", 7, "claude", "standard", "", "")

    real_build = client.build_request
    captured = []

    class _Stop(Exception):
        pass

    def fake_build(args):
        captured.append(args)
        raise _Stop()

    client.build_request = fake_build
    for argv in calls:
        old = sys.argv
        sys.argv = ["review-bot-review"] + argv[1:]
        try:
            try:
                client.main()
            except _Stop:
                pass
        finally:
            sys.argv = old

    with_flag = real_build(captured[0])
    without = real_build(captured[1])
    assert with_flag["post_failure_notice"] == 3, with_flag
    # Omitted when unset: the common request stays byte-identical to the old protocol.
    assert "post_failure_notice" not in without, without
    print("ok  8. poll's exact argv crosses the shipped client into the request field")


def test_serve_validates_and_threads_the_request_field():
    serve = runpy.run_path(SERVE_PATH, run_name="serve_failure_notice_test")
    review = fresh_review()
    base = {"owner": "acme", "repo": "widget", "number": 7}

    args, _h, _b, _f = serve["parse_request"](
        json.dumps(base | {"post_failure_notice": 3}), review
    )
    assert args.post_failure_notice == 3
    args, _h, _b, _f = serve["parse_request"](json.dumps(base), review)
    assert args.post_failure_notice == 0

    bad_requests = [
        base | {"post_failure_notice": -1},
        base | {"post_failure_notice": True},
        base | {"post_failure_notice": "3"},
        {"owner": "acme", "repo": "widget", "mode": "repo", "post_failure_notice": 1},
    ]
    for req in bad_requests:
        try:
            serve["parse_request"](json.dumps(req), review)
        except serve["RequestError"]:
            continue
        raise AssertionError(f"accepted: {req}")

    # The serve failure contract: die() is rebound to raise ReviewFailure (an
    # Exception), and the handler calls _post_failure_notice(error) — a delivery
    # failure raising through the rebound die() must be swallowed, and the call is a
    # no-op once disarmed.
    rec = RecordingApi(explode=RuntimeError("rebound die"))
    review.api = rec
    review.arm_failure_notice("acme", "widget", 7, "pr", 3, "tok")
    with contextlib.redirect_stderr(io.StringIO()):
        review._post_failure_notice("engine died")  # swallowed, disarms
        review._post_failure_notice("engine died")  # no-op
    assert rec.posts == [] and review.FAILURE_NOTICE is None
    rec.explode = None
    review.arm_failure_notice("acme", "widget", 7, "pr", 3, "tok")
    with contextlib.redirect_stderr(io.StringIO()):
        review._post_failure_notice("engine died")
    assert len(rec.posts) == 1, rec.posts
    print("ok  9. serve validates the field and its failure handler delivers the notice")


def main():
    tests = [
        test_armed_die_posts_one_notice_headline_only,
        test_notice_matches_feedback_classification,
        test_unarmed_die_posts_nothing,
        test_notice_fires_once_and_swallows_its_own_delivery_failure,
        test_successful_post_disarms,
        test_main_arms_only_for_poller_shaped_invocations,
        test_poll_passes_flag_on_final_attempt_only,
        test_poll_argv_is_accepted_by_the_shipped_client,
        test_serve_validates_and_threads_the_request_field,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

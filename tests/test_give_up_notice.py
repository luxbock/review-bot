#!/usr/bin/env python3
"""Acceptance tests for the give-up notice — the visible half of refusing a fragment.

Before this existed, a review that could not produce a usable result posted NOTHING and
stopped after MAX_FAILS. Callers waiting on a reply — agents poll their own PRs for one —
blocked on a comment that was never coming. Stdlib only; no live forge.

Run:  python3 tests/test_give_up_notice.py
"""

import importlib.util
import os
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh_poll():
    spec = importlib.util.spec_from_file_location(
        "poll_give_up_test", os.path.join(REPO_ROOT, "poll.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RecordingApi:
    """Stands in for poll.api, capturing every POST body."""

    def __init__(self, fail_times=0, code=502):
        self.posts = []
        self.attempts = 0
        self.fail_times = fail_times
        self.code = code

    def __call__(self, method, path, token, data=None):
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise urllib.error.HTTPError(path, self.code, "boom", {}, None)
        self.posts.append((path, (data or {}).get("body", "")))
        return {}


def given_up(poll, reason="could not parse a JSON result from claude output"):
    """The state record poll.py writes when a trigger exhausts MAX_FAILS."""
    rec = {"status": "given-up", "fails": poll.MAX_FAILS}
    poll.record_give_up(rec, "acme", "widget", 7, "pr", reason)
    return {"m:acme/widget#7:c1": rec}


def test_failure_reason_takes_the_headline_not_the_engine_dump():
    poll = fresh_poll()
    stderr = (
        "review-bot-review: running claude …\n"
        "review-bot-review: error: could not parse a JSON result from claude output "
        "(even after a reformat retry):\n"
        "Here is a long prose reply the engine produced instead of JSON, which must "
        "not be relayed into a public comment.\n"
    )
    reason = poll.failure_reason(stderr)
    assert reason.startswith("could not parse a JSON result"), reason
    assert "long prose reply" not in reason, reason
    assert len(reason) <= 300, len(reason)
    # No recognisable headline: fall back to the last non-empty line rather than nothing.
    assert poll.failure_reason("some other tool blew up\n") == "some other tool blew up"
    assert poll.failure_reason("") == "(no error detail)"
    print("ok  1. the reported reason is the error headline, not the engine output")


def test_give_up_posts_exactly_one_notice():
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    answered = given_up(poll)
    poll.flush_give_up_notices(answered, "tok")
    poll.flush_give_up_notices(answered, "tok")  # a later tick must not repeat it
    assert len(api.posts) == 1, api.posts
    path, body = api.posts[0]
    assert path == "repos/acme/widget/issues/7/comments", path
    assert answered["m:acme/widget#7:c1"]["reported"] is True
    print("ok  2. a give-up posts exactly one notice, however often the flush runs")


def test_the_notice_says_nothing_was_reviewed_and_is_not_a_review():
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    poll.flush_give_up_notices(given_up(poll), "tok")
    _path, body = api.posts[0]
    assert poll.FAIL_MARKER in body, body
    # Must not read as an approval — that inference is the whole reason it exists.
    assert "nothing here was reviewed" in body, body
    assert "not an approval" in body, body
    assert "could not parse a JSON result" in body, body
    # Must not be counted as a review round, nor mistaken for one by the round counter.
    assert poll.REVIEW_MARKER not in body, body
    assert poll.PARK_MARKER not in body, body
    print("ok  3. the notice states that nothing was reviewed and is not a review round")


def test_a_failed_post_is_retried_on_the_next_tick():
    """A give-up is terminal for its trigger, so a dropped notice has no other chance."""
    poll = fresh_poll()
    api = RecordingApi(fail_times=1)
    poll.api = api
    answered = given_up(poll)
    poll.flush_give_up_notices(answered, "tok")
    assert api.posts == [], api.posts
    assert "reported" not in answered["m:acme/widget#7:c1"]
    poll.flush_give_up_notices(answered, "tok")
    assert len(api.posts) == 1, api.posts
    assert answered["m:acme/widget#7:c1"]["reported"] is True
    print("ok  4. a notice the forge rejected is retried instead of being swallowed")


def test_a_permanently_rejected_notice_stops_being_retried():
    """Nothing prunes the state map and the flush walks all of it every tick, so a
    rejection that cannot fix itself — 404 (issue deleted), 403 (repo archived, token
    lost write access) — would otherwise re-POST and re-log forever."""
    for code in (404, 403, 410):
        poll = fresh_poll()
        api = RecordingApi(fail_times=99, code=code)
        poll.api = api
        answered = given_up(poll)
        for _tick in range(5):
            poll.flush_give_up_notices(answered, "tok")
        rec = answered["m:acme/widget#7:c1"]
        assert api.attempts == 1, (code, api.attempts)
        assert rec["reported"] is True and rec["undeliverable"] == code, (code, rec)
    # 429 and 5xx stay retryable: those are the transient ones the retry exists for.
    # 401 belongs with the transient codes: the token can be rotated, and load_token()
    # re-reads it every tick. A dead token is exactly when this path fires.
    for code in (401, 429, 500, 503):
        poll = fresh_poll()
        api = RecordingApi(fail_times=99, code=code)
        poll.api = api
        answered = given_up(poll)
        for _tick in range(3):
            poll.flush_give_up_notices(answered, "tok")
        assert api.attempts == 3, (code, api.attempts)
        assert "reported" not in answered["m:acme/widget#7:c1"], code
    print("ok  5. a permanent rejection stops; 401/429/5xx keep retrying")


def test_even_a_retryable_rejection_is_eventually_abandoned():
    """Nothing prunes the state map, so a token that is never rotated must not re-POST
    forever either — the retryable set needs a terminating condition of its own."""
    poll = fresh_poll()
    api = RecordingApi(fail_times=999, code=401)
    poll.api = api
    answered = given_up(poll)
    for _tick in range(poll.MAX_NOTICE_ATTEMPTS + 5):
        poll.flush_give_up_notices(answered, "tok")
    rec = answered["m:acme/widget#7:c1"]
    assert api.attempts == poll.MAX_NOTICE_ATTEMPTS, api.attempts
    assert rec["reported"] is True and rec["undeliverable"] == 401, rec
    assert rec["notice_attempts"] == poll.MAX_NOTICE_ATTEMPTS, rec
    print("ok  6. a retryable rejection is abandoned after MAX_NOTICE_ATTEMPTS")


def test_dry_run_posts_nothing_and_leaves_it_pending():
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    answered = given_up(poll)
    poll.flush_give_up_notices(answered, "tok", dry=True)
    assert api.posts == [], api.posts
    assert "reported" not in answered["m:acme/widget#7:c1"]
    print("ok  7. --dry-run reports the intent without posting or marking it delivered")


def test_only_unreported_give_ups_are_flushed():
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    answered = {
        "done": {"status": "done"},
        "failing": {"status": "failing", "fails": 1},
        "parked": {"status": "parked"},
        "reported": {"status": "given-up", "reported": True,
                     "notice": {"owner": "a", "repo": "b", "num": 1, "mode": "pr",
                                "fails": 3, "reason": "x"}},
        "legacy": {"status": "given-up"},  # pre-upgrade record, no notice to send
    }
    poll.flush_give_up_notices(answered, "tok")
    assert api.posts == [], api.posts
    print("ok  8. done/failing/parked/already-reported/legacy records are left alone")


def test_issue_triage_give_up_names_the_issue():
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    rec = {"status": "given-up", "fails": 3}
    poll.record_give_up(rec, "acme", "widget", 11, "issue", "engine timed out")
    poll.flush_give_up_notices({"mi:acme/widget#11:c9": rec}, "tok")
    path, body = api.posts[0]
    assert path == "repos/acme/widget/issues/11/comments", path
    assert "nothing here was triaged" in body, body
    assert "I tried to triage this" in body and "--issue 11" in body, body
    print("ok  9. an issue triage give-up is reported on the issue, in triage wording")


def test_feedback_classifies_the_notice_as_failed():
    """review-bot-feedback is what a caller polls for a verdict, so the notice must arrive
    there as its own kind — `other` would hide the one thing it needs to say."""
    poll = fresh_poll()
    api = RecordingApi()
    poll.api = api
    poll.flush_give_up_notices(given_up(poll), "tok")
    _path, body = api.posts[0]
    spec = importlib.util.spec_from_file_location(
        "feedback_give_up_test", os.path.join(REPO_ROOT, "feedback.py")
    )
    feedback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feedback)
    assert feedback.FAIL_MARKER == poll.FAIL_MARKER, (feedback.FAIL_MARKER, poll.FAIL_MARKER)
    assert feedback.classify(body) == "failed", feedback.classify(body)
    assert "failed" in feedback.KINDS, feedback.KINDS
    print("ok 10. review-bot-feedback classifies the notice as `failed`, not `other`")


def main():
    tests = [
        test_failure_reason_takes_the_headline_not_the_engine_dump,
        test_give_up_posts_exactly_one_notice,
        test_the_notice_says_nothing_was_reviewed_and_is_not_a_review,
        test_a_failed_post_is_retried_on_the_next_tick,
        test_a_permanently_rejected_notice_stops_being_retried,
        test_even_a_retryable_rejection_is_eventually_abandoned,
        test_dry_run_posts_nothing_and_leaves_it_pending,
        test_only_unreported_give_ups_are_flushed,
        test_issue_triage_give_up_names_the_issue,
        test_feedback_classifies_the_notice_as_failed,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

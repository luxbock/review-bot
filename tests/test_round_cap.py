#!/usr/bin/env python3
"""Acceptance tests for the per-revision round cap (issue #29) — stdlib only,
NO live forge, NO engines.

Covers:
  * the review footer's `` head `<sha12>` `` stamp (present with head_sha, absent
    without; after `merge-base`; classify() still "review"; triage/audit unchanged);
  * count_review_rounds over canned comment dicts: per-head attribution, legacy
    reviews, manual passes, non-self authors, park / give-up / triage notices
    counting toward neither counter, and the falsy-head_sha conservative fallback;
  * the #27 regression sequence (head A capped, a push to B re-arms review);
  * the monotone lifetime budget (MAX_TOTAL) that no push can un-trip;
  * post_parked's two wordings under the byte-identical PARK_MARKER heading.

Run:  python3 tests/test_round_cap.py
"""

import contextlib
import importlib.util
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# The constants under test assume the shipped defaults (MAX_ROUNDS=3, MAX_TOTAL=12,
# @olli ping) — scrub any deployment overrides before the modules read them.
for _var in ("REVIEW_BOT_MAX_ROUNDS", "REVIEW_BOT_MAX_TOTAL", "REVIEW_BOT_HANDLES",
             "REVIEW_BOT_OWNER_HANDLE"):
    os.environ.pop(_var, None)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_poll():
    return load_module("poll_round_cap_test", os.path.join(REPO_ROOT, "poll.py"))


def fresh_review():
    return load_module("review_round_cap_test", os.path.join(REPO_ROOT, "review.py"))


def fresh_feedback():
    return load_module("feedback_round_cap_test", os.path.join(REPO_ROOT, "feedback.py"))


SELF = {"review-bot", "review_bot"}
HEAD_A = "1111aaaa2222bbbb3333cccc4444dddd5555eeee"
HEAD_B = "9999ffff8888eeee7777dddd6666cccc5555bbbb"


def _c(login, body, created_at="2026-07-27T16:00:00Z"):
    return {"user": {"login": login}, "body": body, "created_at": created_at}


def review_body(head=None):
    """A canned review comment matching render_markdown's footer shape. head=None is
    a LEGACY review — posted before the head stamp existed."""
    seg = f" · head `{head[:12]}`" if head else ""
    return (
        "## 🤖 review-bot — ✅ no blocking issues\n\n---\n"
        "*Automated review by **review-bot** · harness `claude` · depth `standard` · "
        f"bar `medium` · merge-base `abcdef012345`{seg}. "
        "Advisory only — olli merges. Re-run with `@review-bot <args>`.*"
    )


def parked_bodies(poll):
    """Capture BOTH real parked-notice bodies off a stubbed api()."""
    posted = []
    poll.api = lambda method, path, token, data=None: posted.append((method, path, data))
    with contextlib.redirect_stderr(io.StringIO()):
        poll.post_parked("acme", "widget", 7, 3, "tok", head_sha=HEAD_A)
        poll.post_parked("acme", "widget", 7, 12, "tok", budget=True)
    return posted


FAIL_BODY = (
    "## 🤖 review-bot — could not complete\n\n"
    "@olli I tried to review this 3 time(s) and could not produce a usable result, "
    "so **nothing here was reviewed**."
)
TRIAGE_BODY = (
    "Works as designed.\n\n---\n*Automated triage by **review-bot** · harness `claude` · "
    "depth `standard` · confidence `high` · bar `medium` · repo tip `abcdef012345`.*"
)


# ── 1-2. the footer head stamp (criteria 1, 2) ────────────────────────────────
def test_footer_head_segment_and_classification():
    review = fresh_review()
    clean = {"verdict": "approve", "findings": [], "summary": "Looks good."}
    markdown = review.render_markdown(clean, ["claude"], "standard", "medium",
                                      "abcdef0123456789", head_sha=HEAD_A)
    assert f"· merge-base `abcdef012345` · head `{HEAD_A[:12]}`. " in markdown, markdown
    footer = markdown.rsplit("---", 1)[1]
    assert footer.index("merge-base `") < footer.index("head `")
    feedback = fresh_feedback()
    assert feedback.classify(markdown) == "review"
    # Triage and audit footers are unchanged — no head segment.
    triage = {"disposition": "works-as-designed", "summary": "s", "assessment": "",
              "grounding": "", "recommended_action": "", "confidence": "high"}
    tri_md = review.render_triage_markdown(triage, ["claude"], "standard", "medium", HEAD_A)
    assert " · head `" not in tri_md and f"repo tip `{HEAD_A[:12]}`" in tri_md, tri_md
    audit = {"findings": [], "summary": "s"}
    aud_md = review.render_audit_markdown(audit, "acme/widget", ["claude"], "standard",
                                          "medium", HEAD_A)
    assert " · head `" not in aud_md and f"repo tip `{HEAD_A[:12]}`" in aud_md, aud_md
    print("ok  1. review footer stamps `head `<sha12>`` after merge-base; triage/audit unchanged")


def test_footer_without_head_sha_unchanged():
    review = fresh_review()
    clean = {"verdict": "approve", "findings": [], "summary": "Looks good."}
    markdown = review.render_markdown(clean, ["claude"], "standard", "medium",
                                      "abcdef0123456789")
    assert " · head `" not in markdown, markdown
    assert "· merge-base `abcdef012345`. " in markdown, markdown
    feedback = fresh_feedback()
    assert feedback.classify(markdown) == "review"
    print("ok  2. head_sha=None omits the segment — direct renders keep today's footer")


# ── 3. marker strings byte-identical (criterion 2 / ruling 5) ─────────────────
def test_marker_strings_byte_identical():
    poll, feedback, review = fresh_poll(), fresh_feedback(), fresh_review()
    assert poll.REVIEW_MARKER == "Automated review by **review-bot**"
    assert poll.PARK_MARKER == "review-bot — parked"
    assert feedback.REVIEW_MARKER == poll.REVIEW_MARKER
    assert feedback.PARK_MARKER == poll.PARK_MARKER
    assert feedback.FAIL_MARKER == review.FAIL_MARKER == "review-bot — could not complete"
    print("ok  3. REVIEW_MARKER / PARK_MARKER / FAIL_MARKER are byte-identical to main")


# ── 4. per-head attribution: a push re-arms review (criteria 3, 6) ────────────
def test_push_resets_effective_rounds():
    poll = fresh_poll()
    park_a, park_budget = (d["body"] for _, _, d in parked_bodies(poll))
    comments = [
        _c("review-bot", review_body(HEAD_A)),
        _c("review-bot", review_body(HEAD_A)),
        _c("review_bot", review_body(HEAD_A)),        # alias handle is still us
        _c("review-bot", review_body()),              # legacy: budget only, no head
        _c("aatos", review_body(HEAD_A)),             # not ours — a human quoting a footer
        _c("review-bot", park_a),                     # PARK_MARKER: neither counter
        _c("review-bot", park_budget),                # PARK_MARKER: neither counter
        _c("review-bot", FAIL_BODY),                  # FAIL_MARKER: neither counter
        _c("review-bot", TRIAGE_BODY),                # triage footer: neither counter
    ]
    assert poll.count_review_rounds(comments, SELF, HEAD_A) == (3, 4)
    # Head A is at the cap; a push to head B re-arms — A-reviews and the legacy
    # review contribute nothing to B's count.
    head_b, total_b = poll.count_review_rounds(comments, SELF, HEAD_B)
    assert (head_b, total_b) == (0, 4)
    assert poll.count_review_rounds(comments, SELF, HEAD_A)[0] >= poll.MAX_ROUNDS
    assert head_b < poll.MAX_ROUNDS and total_b < poll.MAX_TOTAL
    print("ok  4. reviews attribute per head — a push resets the effective round count")


# ── 5. manual passes count toward the per-revision cap (criterion 4) ──────────
def test_manual_pass_counts_toward_the_cap():
    poll = fresh_poll()
    assert poll.MAX_ROUNDS == 3  # the arithmetic below assumes the default
    comments = [
        _c("review-bot", review_body(HEAD_A)),  # poller round 1
        _c("review-bot", review_body(HEAD_A)),  # poller round 2
        # A manual/direct pass renders the same footer — invisible to poller state,
        # visible to the thread scan, and it must count.
        _c("review-bot", review_body(HEAD_A)),
    ]
    head_rounds, total_rounds = poll.count_review_rounds(comments, SELF, HEAD_A)
    assert (head_rounds, total_rounds) == (3, 3)
    assert head_rounds >= poll.MAX_ROUNDS  # the 4th attempt on head A parks
    print("ok  5. 2 poller + 1 manual review of one head park the 4th attempt")


# ── 6. falsy head_sha is conservative (criterion 7) ───────────────────────────
def test_falsy_head_sha_degrades_to_the_lifetime_count():
    poll = fresh_poll()
    comments = [
        _c("review-bot", review_body(HEAD_A)),
        _c("review-bot", review_body(HEAD_B)),
        _c("review-bot", review_body()),
    ]
    for falsy in ("", None):
        head_rounds, total_rounds = poll.count_review_rounds(comments, SELF, falsy)
        assert head_rounds == total_rounds == 3, (falsy, head_rounds, total_rounds)
    print("ok  6. falsy head_sha => head_rounds == total_rounds (never looser)")


# ── 7. the #27 regression sequence (criterion 8) ──────────────────────────────
def test_issue27_regression_sequence():
    poll = fresh_poll()
    comments = [
        _c("review-bot", review_body(HEAD_A), "2026-07-27T15:59:00Z"),
        _c("review-bot", review_body(HEAD_A), "2026-07-27T16:09:00Z"),
        _c("review-bot", review_body(HEAD_A), "2026-07-27T16:23:00Z"),
        _c("review-bot", review_body(HEAD_A), "2026-07-27T16:31:00Z"),  # manual pass
    ]
    head_rounds, total_rounds = poll.count_review_rounds(comments, SELF, HEAD_A)
    assert head_rounds == 4 >= poll.MAX_ROUNDS  # A parks (once — the park key is per head)
    # Push to head B: reviewable again, nothing carried over to B's count.
    head_rounds, total_rounds = poll.count_review_rounds(comments, SELF, HEAD_B)
    assert head_rounds == 0 and total_rounds == 4
    assert head_rounds < poll.MAX_ROUNDS and total_rounds < poll.MAX_TOTAL
    print("ok  7. the #27 sequence: head A parked, a push to B is reviewable again")


# ── 8. the lifetime budget is monotone (criterion 5) ──────────────────────────
def test_budget_backstop_is_monotone_across_pushes():
    poll = fresh_poll()
    assert poll.MAX_TOTAL == 12  # the arithmetic below assumes the default
    comments = (
        [_c("review-bot", review_body(HEAD_A)) for _ in range(5)]
        + [_c("review-bot", review_body(HEAD_B)) for _ in range(4)]
        + [_c("review-bot", review_body()) for _ in range(3)]  # legacy reviews count here
    )
    for head in (HEAD_A, HEAD_B, "cccc0000dddd1111eeee2222ffff3333aaaa4444"):
        total = poll.count_review_rounds(comments, SELF, head)[1]
        assert total == 12 >= poll.MAX_TOTAL, head  # no push un-trips the budget
    print("ok  8. MAX_TOTAL counts every review ever; a push never lowers it")


# ── 9-10. the two parked wordings (criteria 3, 5) ─────────────────────────────
def test_parked_wordings():
    poll = fresh_poll()
    feedback = fresh_feedback()
    posted = parked_bodies(poll)
    assert [(m, p) for m, p, _ in posted] == [("POST", "repos/acme/widget/issues/7/comments")] * 2
    revision, budget = (d["body"] for _, _, d in posted)
    for body in (revision, budget):
        assert body.startswith(f"## 🤖 {poll.PARK_MARKER}\n\n@olli ")
        assert feedback.classify(body) == "parked"
        assert poll.REVIEW_MARKER not in body  # a park must never count as a round
    assert (
        f"I've posted 3 automated reviews of `{HEAD_A[:12]}` — parking further "
        "automatic reviews of this revision to avoid a review↔fix loop. Push new "
        "commits, or ask me directly (`review-bot-review … --pr 7`) for another pass."
    ) in revision, revision
    assert (
        "I've posted 12 automated reviews on this PR — parking automatic reviews "
        "here; the total-review budget (12) is spent. Ask me directly "
        "(`review-bot-review … --pr 7`) if more are needed."
    ) in budget, budget
    print("ok  9. per-revision and budget parked wordings, same PARK_MARKER heading")


def test_rendered_review_is_what_the_counter_counts():
    """End-to-end contract: the footer render_markdown emits is the exact segment
    count_review_rounds matches on — the two sides cannot drift apart silently."""
    poll, review = fresh_poll(), fresh_review()
    clean = {"verdict": "approve", "findings": [], "summary": "Looks good."}
    rendered = review.render_markdown(clean, ["claude"], "standard", "medium",
                                      "abcdef0123456789", head_sha=HEAD_A)
    comments = [_c("review-bot", rendered)]
    assert poll.count_review_rounds(comments, SELF, HEAD_A) == (1, 1)
    assert poll.count_review_rounds(comments, SELF, HEAD_B) == (0, 1)
    print("ok 10. a really-rendered review counts for its head and no other")


def main():
    tests = [
        test_footer_head_segment_and_classification,
        test_footer_without_head_sha_unchanged,
        test_marker_strings_byte_identical,
        test_push_resets_effective_rounds,
        test_manual_pass_counts_toward_the_cap,
        test_falsy_head_sha_degrades_to_the_lifetime_count,
        test_issue27_regression_sequence,
        test_budget_backstop_is_monotone_across_pushes,
        test_parked_wordings,
        test_rendered_review_is_what_the_counter_counts,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

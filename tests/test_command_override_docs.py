#!/usr/bin/env python3
"""Doc-coupling test for the engine-command override variables — stdlib only, NO live
forge, NO engines, NO live model.

There are THREE engine-command overrides: REVIEW_BOT_CLAUDE_CMD (finder, verify and
synthesis), REVIEW_BOT_SELECT_CMD (the over-cap file-selection stage, issue #35) and
REVIEW_BOT_CODEX_CMD. Several prose passages enumerate that set, and an enumeration is
exactly the kind of prose that rots silently: SELECT_CMD was added by #35/#38 and the
lists written before it simply never grew a third entry. The README was corrected by
hand during nixos-config#493; `serve.py`'s module docstring, `review.py`'s own tuning
comment and the SKILL's gotcha carried the stale two-variable list for longer, each
telling a reader that overriding two commands covers the engines when it covers two of
three — and the third is a claude command line carrying the #493 security flags.

So the property pinned here is not the flags (that is test_engine_command_hardening.py)
but COMPLETENESS: any passage that enumerates the override set must enumerate all of it.

How it works:
  * the variable set is DISCOVERED from `review.py` rather than hardcoded, so a fourth
    engine command is covered the day it is added, with a guard so discovery cannot go
    silently empty or shrink below the three known today;
  * each documented passage is located by a stable anchor phrase and delimited by its
    own syntax (comment block / markdown bullet / prose paragraph), so the check reads
    the passage a human reads and cannot pass on a mention somewhere else in the file —
    `review.py` names all three in CODE, which is precisely why whole-file matching
    would be vacuous here;
  * a missing anchor is a FAILURE, not a skip, so deleting or rewording a passage
    surfaces instead of quietly disabling its check;
  * a control test feeds the checker a passage with a known omission and a missing
    anchor, because a check that cannot produce a negative verifies nothing.

Run:  python3 tests/test_command_override_docs.py
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Discovery: every REVIEW_BOT_*_CMD the module actually reads from the environment.
OVERRIDE_RE = re.compile(r'os\.environ\.get\(\s*"(REVIEW_BOT_[A-Z_]+_CMD)"')
# Known today; discovery must not fall below this, or it has gone vacuous.
KNOWN_OVERRIDES = {"REVIEW_BOT_CLAUDE_CMD", "REVIEW_BOT_SELECT_CMD", "REVIEW_BOT_CODEX_CMD"}

# (path relative to the repo root, anchor phrase). The anchor must be unique in the file
# and must sit on the passage's own first line.
DOCUMENTED_PASSAGES = [
    ("README.md", "Deliberately **not** accepted:"),
    ("serve.py", "Deliberately NOT accepted:"),
    ("review.py", "The harness commands are env-overridable"),
    ("skills/review-bot/SKILL.md", "**Engine flags may need tuning"),
]


def read(relpath):
    with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as fh:
        return fh.read()


def discover_overrides():
    found = set(OVERRIDE_RE.findall(read("review.py")))
    missing = sorted(KNOWN_OVERRIDES - found)
    assert not missing, (
        f"discovery no longer finds {missing} in review.py — either an override was "
        f"renamed (update KNOWN_OVERRIDES) or OVERRIDE_RE has gone stale and every "
        f"assertion below is passing vacuously; found: {sorted(found)}"
    )
    return sorted(found)


def passage(text, anchor):
    """The passage a human reads around `anchor`, delimited by its own syntax.

    A comment block runs over consecutive `#` lines; a markdown bullet runs to the next
    bullet; anything else is the paragraph of consecutive non-blank lines. Raises if the
    anchor is absent or ambiguous — a passage that has been deleted or duplicated must
    fail loudly rather than silently checking nothing.
    """
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if anchor in line]
    if len(hits) != 1:
        raise LookupError(f"anchor {anchor!r} matched {len(hits)} lines, expected exactly 1")
    index = hits[0]
    stripped = lines[index].lstrip()
    if stripped.startswith("#"):
        keep = lambda line: line.lstrip().startswith("#")  # noqa: E731
    elif stripped.startswith("- "):
        keep = lambda line: bool(line.strip()) and not line.lstrip().startswith("- ")  # noqa: E731
    else:
        keep = lambda line: bool(line.strip())  # noqa: E731

    start = index
    while start > 0 and keep(lines[start - 1]):
        start -= 1
    end = index + 1
    while end < len(lines) and keep(lines[end]):
        end += 1
    return "\n".join(lines[start:end])


def missing_from(text, names):
    return [name for name in names if name not in text]


def test_discovery_is_not_vacuous():
    """Every assertion below is over a set read out of review.py; if that read breaks,
    the whole file passes for free."""
    overrides = discover_overrides()
    assert len(overrides) >= len(KNOWN_OVERRIDES), overrides
    print(f"ok  1. discovery finds {len(overrides)} override variables: {', '.join(overrides)}")


def test_documented_passages_enumerate_every_override():
    """The stale two-variable list, in every place it was written down."""
    overrides = discover_overrides()
    for relpath, anchor in DOCUMENTED_PASSAGES:
        text = passage(read(relpath), anchor)
        gaps = missing_from(text, overrides)
        assert not gaps, (
            f"{relpath}: the passage anchored at {anchor!r} enumerates the engine-command "
            f"overrides but omits {gaps}. An incomplete list reads as a complete one — "
            f"either name them all or stop enumerating.\n--- passage ---\n{text}"
        )
    print(f"ok  2. all {len(DOCUMENTED_PASSAGES)} documented passages name every override")


def test_the_check_can_fail():
    """A verification method that cannot produce a negative is not a verification."""
    overrides = discover_overrides()
    stale = "the REVIEW_BOT_CLAUDE_CMD / REVIEW_BOT_CODEX_CMD tuning knobs"
    assert missing_from(stale, overrides) == ["REVIEW_BOT_SELECT_CMD"], (
        "the checker no longer flags the exact stale list this test file exists to catch"
    )

    for text, anchor in (("no such phrase here", "Deliberately NOT accepted:"),
                         ("twice: anchor\nagain: anchor", "anchor")):
        try:
            passage(text, anchor)
        except LookupError:
            continue
        raise AssertionError(f"passage() accepted {anchor!r} against text it does not uniquely match")

    # And the extractor must really stop at the passage boundary: review.py names all
    # three overrides in code, so a comment block that leaked into it would pass anyway.
    block = passage(read("review.py"), "The harness commands are env-overridable")
    assert all(line.lstrip().startswith("#") for line in block.splitlines()), (
        f"the review.py comment block leaked into code, making its check vacuous:\n{block}"
    )
    print("ok  3. the check fails on a stale list, a missing anchor and a leaked boundary")


def main():
    tests = [
        test_discovery_is_not_vacuous,
        test_documented_passages_enumerate_every_override,
        test_the_check_can_fail,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

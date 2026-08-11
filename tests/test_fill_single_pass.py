#!/usr/bin/env python3
"""Acceptance tests for single-pass prompt filling (issue #49) — stdlib only,
NO live forge, NO engines, NO model.

Background: `fill()` used to apply one `str.replace` per mapping key over an
accumulating string, so a value substituted early was rescanned by every later key.
`DIFF_OR_FILE_LIST` carries the UNTRUSTED diff and precedes `CONVENTION_FILES` at
the call site, so a `{{CONVENTION_FILES}}` token occurring INSIDE the diff was
expanded as if it were part of the prompt template — the engine then reviewed source
that exists nowhere. Observed for real on #48, whose diff adds
`CONVENTION_PLACEHOLDER = "{{CONVENTION_FILES}}"` to tests/test_prompt_scope.py.

Reordering the mapping is NOT the fix and these tests deliberately refuse to accept
one: every value may contain every other key's token, and the diff is untrusted by
definition, so no ordering is safe. The defect is the RESCAN, so the property tested
here is order-independence — the same output whichever key comes first.

Covers:
  * a value containing another key's token survives verbatim, in BOTH mapping orders
    (order-independence is the property; a reordering patch cannot satisfy it);
  * unknown tokens pass through unchanged — never emptied, never raising — whether
    they sit in the template or arrive inside a value;
  * backslash sequences in a value (`\\1`, `\\g<0>`, a trailing backslash) reach the
    output byte-identical, i.e. the replacement is a function and not a string that
    `re` would read escapes out of;
  * every real prompt template still gets every known placeholder filled, across the
    pr / issue / repo modes plus the verify, synthesis and selection fills;
  * the assembly-level #48 case: a diff block carrying the literal text
    `{{CONVENTION_FILES}}` reaches the emitted prompt byte-identical while the
    template's OWN placeholder is still filled with the convention-file names.

Run:  python3 tests/test_fill_single_pass.py
"""

import importlib.util
import os
import re
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The placeholder vocabulary as the call sites use it, per fill. Keyed by the prompt
# file so the coverage test can assert BOTH directions: every placeholder a template
# contains is supplied, and every key a call site supplies is actually used. Mirrors
# review.py:939 (select), :2139/:2151/:2155 (pr), :2196/:2209/:2213 (issue),
# :2246/:2258/:2262 (repo).
CALL_SITE_KEYS = {
    "review-prompt.md": {"MERGE_BASE", "DIFF_OR_FILE_LIST", "CONVENTION_FILES", "FOCUS", "CONFIDENCE_BAR"},
    "verify-prompt.md": {"MERGE_BASE", "REVIEW_JSON", "CONFIDENCE_BAR"},
    "synthesis-prompt.md": {"N", "REVIEW_JSON_LIST"},
    "triage-prompt.md": {"DEFAULT_BRANCH", "REPO", "ISSUE_BLOCK", "CONVENTION_FILES", "FOCUS", "CONFIDENCE_BAR"},
    "triage-verify-prompt.md": {"DEFAULT_BRANCH", "REVIEW_JSON", "CONFIDENCE_BAR"},
    "triage-synthesis-prompt.md": {"N", "REVIEW_JSON_LIST"},
    "audit-prompt.md": {"DEFAULT_BRANCH", "REPO", "CONVENTION_FILES", "FOCUS", "CONFIDENCE_BAR"},
    "audit-verify-prompt.md": {"DEFAULT_BRANCH", "REVIEW_JSON", "CONFIDENCE_BAR"},
    "audit-synthesis-prompt.md": {"N", "REVIEW_JSON_LIST"},
    "select-prompt.md": {"STAT", "FILE_HEADERS", "CONVENTION_FILES"},
}

TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")

# The #48 evidence, reproduced as a diff hunk: a PR that touches the placeholder
# vocabulary carries the token in its own diff. This is untrusted input — it must
# reach the engine exactly as the forge served it.
POISONED_DIFF = '''diff --git a/tests/test_prompt_scope.py b/tests/test_prompt_scope.py
--- a/tests/test_prompt_scope.py
+++ b/tests/test_prompt_scope.py
@@ -39,6 +39,8 @@
+# The placeholder that interpolates the convention-file list into a prompt.
+CONVENTION_PLACEHOLDER = "{{CONVENTION_FILES}}"
+FOCUS_PLACEHOLDER = "{{FOCUS}}"
+UNKNOWN_PLACEHOLDER = "{{NOT_A_KEY}}"
+PATTERN = re.compile(r"\\g<0>\\1\\\\")
'''


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REVIEW = load_module("review_fill_single_pass_test", os.path.join(REPO_ROOT, "review.py"))


def fill_text(template_text, mapping):
    """fill() takes a PATH (prompts are files on disk), so stage the text as one."""
    with tempfile.TemporaryDirectory(prefix="review-bot-fill-") as tmp:
        path = os.path.join(tmp, "template.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(template_text)
        return REVIEW.fill(path, mapping)


def prompt_path(name):
    return os.path.join(REPO_ROOT, name)


def test_substituted_values_are_never_rescanned():
    """The core property. A diff containing `{{CONVENTION_FILES}}` must land in the
    prompt with that token intact — the engine has to see the source as the forge
    served it, not a rewritten copy of it."""
    template = "diff: {{DIFF_OR_FILE_LIST}}\nconventions: {{CONVENTION_FILES}}\n"
    diff = 'CONVENTION_PLACEHOLDER = "{{CONVENTION_FILES}}"'
    assert "{{CONVENTION_FILES}}" in diff, "fixture lost its token — the test would pass vacuously"

    out = fill_text(template, {"DIFF_OR_FILE_LIST": diff, "CONVENTION_FILES": "AGENTS.md, README.md"})
    assert out == f"diff: {diff}\nconventions: AGENTS.md, README.md\n", (
        f"the diff's own token was expanded as template syntax — issue #49:\n{out!r}"
    )
    print("ok  1. a token inside a substituted value survives verbatim")


def test_the_property_is_order_independent():
    """Order-independence is what separates the real fix from a reordered mapping:
    ANY value may carry ANY key's token, so the result must not depend on which key
    the call site happens to list first. Both directions are exercised, plus the
    mutual case where each value carries the other's token — unsalvageable by
    ordering in principle, since no order can be first for both."""
    template = "A={{ALPHA}} B={{BETA}}\n"
    alpha, beta = "alpha-value {{BETA}}", "beta-value {{ALPHA}}"
    expected = f"A={alpha} B={beta}\n"

    forward = fill_text(template, {"ALPHA": alpha, "BETA": beta})
    reverse = fill_text(template, {"BETA": beta, "ALPHA": alpha})
    assert forward == reverse, f"output depends on mapping order:\n{forward!r}\nvs\n{reverse!r}"
    assert forward == expected, f"cross-substitution leaked between values:\n{forward!r}"
    print("ok  2. identical output in both mapping orders, incl. mutually-referring values")


def test_unknown_tokens_pass_through_unchanged():
    """An unmapped `{{TOKEN}}` is not ours to interpret: it must survive byte-identical
    rather than becoming the empty string (silent data loss) or raising (a review that
    dies on a PR that merely mentions a placeholder)."""
    template = "known={{FOCUS}} unknown={{NOT_A_KEY}} lower={{not_a_key}} empty={{}}\n"
    out = fill_text(template, {"FOCUS": "the {{ALSO_UNKNOWN}} focus"})
    assert out == "known=the {{ALSO_UNKNOWN}} focus unknown={{NOT_A_KEY}} lower={{not_a_key}} empty={{}}\n", (
        f"unknown tokens were not passed through intact:\n{out!r}"
    )
    assert "{{NOT_A_KEY}}" in out and "{{ALSO_UNKNOWN}}" in out
    print("ok  3. unknown tokens pass through unchanged — in the template and inside a value")


def test_backslashes_in_values_are_literal():
    """The replacement must be a FUNCTION, not a replacement string: `re` reads `\\1`
    and `\\g<0>` out of a replacement STRING, and the value here is a diff, which is
    full of backslashes. A trailing backslash would additionally raise."""
    hostile = "re.compile(r'\\g<0>') \\1 \\\\ tail\\"
    out = fill_text("value: {{DIFF_OR_FILE_LIST}}!\n", {"DIFF_OR_FILE_LIST": hostile})
    assert out == f"value: {hostile}!\n", f"backslash escapes in the value were interpreted:\n{out!r}"
    print("ok  4. backslash sequences in a value reach the output byte-identical")


def test_every_prompt_still_gets_every_placeholder_filled():
    """This is a fix, not a behaviour change: well-formed input must fill exactly as
    before, in all three modes plus the verify/synthesis/selection fills. Asserted
    both ways so neither side can drift silently — no template placeholder goes
    unsupplied, and no call-site key goes unused."""
    for name, keys in sorted(CALL_SITE_KEYS.items()):
        path = prompt_path(name)
        assert os.path.exists(path), f"{name} is listed at a call site but is not in the repo"
        text = open(path, encoding="utf-8").read()
        used = set(TOKEN_RE.findall(text))
        assert used, f"{name} contains no placeholders at all — the vocabulary probably changed"
        assert used == keys, (
            f"{name}: template placeholders {sorted(used)} do not match the call-site "
            f"mapping {sorted(keys)} — update CALL_SITE_KEYS deliberately"
        )
        mapping = {k: f"<<{k}>>" for k in keys}
        out = REVIEW.fill(path, mapping)
        assert not TOKEN_RE.search(out), f"{name}: unfilled placeholders remain: {TOKEN_RE.findall(out)}"
        for k in keys:
            assert f"<<{k}>>" in out, f"{name}: {k} was not substituted"
    print(f"ok  5. all {len(CALL_SITE_KEYS)} prompt templates fill correctly across pr/issue/repo")


def test_assembly_level_poisoned_diff_reaches_the_prompt_intact():
    """The end of the real path, on the real review-prompt.md: the emitted prompt must
    contain the diff block byte-identical — token, backslashes and all — while the
    template's own {{CONVENTION_FILES}} is still filled with the file names."""
    conv_str = "AGENTS.md, README.md"
    prompt = REVIEW.fill(
        prompt_path("review-prompt.md"),
        {
            "MERGE_BASE": "abc123def456",
            "DIFF_OR_FILE_LIST": POISONED_DIFF,
            "CONVENTION_FILES": conv_str,
            "FOCUS": "(none)",
            "CONFIDENCE_BAR": "high",
        },
    )
    assert POISONED_DIFF in prompt, (
        "the diff block was rewritten on its way into the prompt — the engine would "
        "review source that exists in no tree (issue #49)"
    )
    # …and the template's own placeholder is still genuinely filled, not merely spared.
    assert conv_str in prompt, "the template's own {{CONVENTION_FILES}} was not filled"
    assert prompt.count("{{CONVENTION_FILES}}") == 1, (
        "expected exactly one surviving token — the one inside the diff; got "
        f"{prompt.count('{{CONVENTION_FILES}}')}"
    )
    assert prompt.count("{{FOCUS}}") == 1 and "{{NOT_A_KEY}}" in prompt
    assert "abc123def456" in prompt and "(none)" in prompt
    print("ok  6. assembly level: poisoned diff intact, template placeholders still filled")


def main():
    tests = [
        test_substituted_values_are_never_rescanned,
        test_the_property_is_order_independent,
        test_unknown_tokens_pass_through_unchanged,
        test_backslashes_in_values_are_literal,
        test_every_prompt_still_gets_every_placeholder_filled,
        test_assembly_level_poisoned_diff_reaches_the_prompt_intact,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Acceptance tests for the claude engine-command hardening flags (issue #493) —
stdlib only, NO live forge, NO engines, NO live model.

Background: every claude invocation runs with cwd inside the per-run worktree, checked
out at the PR HEAD. So a `.claude/settings.json` carrying hooks, and a repo-root
`.mcp.json` declaring a stdio server, are code the REVIEWED repository controls. Both
were verified against the exact store binary to execute as the review-bot user with no
model involvement, no prompt, no trust dialog and no permission prompt. Neither is
reachable by prompt hardening — a hook fires before the model sees the prompt and an
MCP server launches at session start — and neither is gated by `--allowedTools`,
because hooks and MCP servers are session-level. `--settings '{"disableAllHooks":
true}'` and `--strict-mcp-config` on the command line are the lever that closes them.

That the CLI *accepts* the flags is checked out of band (`claude --help`); reaching a
live model from the suite is exactly what this suite must not do. What is checkable
here is the property that actually rots: the flags surviving in the defaults.

Covers:
  * both claude command defaults carry both flags — asserted per command, so
    dropping either flag from either one is caught independently;
  * the flags are discovered on EVERY default that invokes claude, not just the two
    known today, with a non-vacuity guard so discovery cannot silently empty;
  * the `shlex.split` boundary: `--settings` and its JSON must come out as two argv
    entries and the JSON must round-trip through `json.loads` (unquoted JSON in the
    default string would shatter into several argv entries instead);
  * they are a DEFAULT, not a floor — an env override still replaces the whole
    command, and an empty REVIEW_BOT_SELECT_CMD still disables the stage;
  * CODEX_CMD deliberately does NOT carry them (repo-level codex hook discovery was
    tested and does not exist; these are claude flags codex would reject);
  * the README quotes the select default verbatim, so the prose cannot drift from it.

Run:  python3 tests/test_engine_command_hardening.py
"""

import importlib.util
import json
import os
import shlex

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
REVIEW_PY = os.path.join(REPO_ROOT, "review.py")

SETTINGS_FLAG = "--settings"
STRICT_MCP_FLAG = "--strict-mcp-config"
# The settings payload must PARSE to this — the assertion is on meaning, not on the
# byte string, so reformatting the JSON is fine and weakening it is not.
REQUIRED_SETTINGS = {"disableAllHooks": True}

# Every env var that can replace an engine command default. Cleared before each load so
# the module under test yields its DEFAULTS and not an inherited environment (a
# developer shell or a test runner that exports one would otherwise pass vacuously).
COMMAND_ENV_VARS = ("REVIEW_BOT_CLAUDE_CMD", "REVIEW_BOT_SELECT_CMD", "REVIEW_BOT_CODEX_CMD")

# The claude command defaults known when this test was written. Discovery below finds
# these dynamically so a future third claude command line is covered automatically;
# this set keeps discovery from going silently vacuous if one is renamed or if its
# default stops invoking claude.
KNOWN_CLAUDE_COMMANDS = ("CLAUDE_CMD", "SELECT_CMD")


def load_review(env=None, name="review_engine_hardening_test"):
    """Load a FRESH review.py with the command env vars cleared, plus `env` if given."""
    saved = {k: os.environ[k] for k in COMMAND_ENV_VARS if k in os.environ}
    for key in COMMAND_ENV_VARS:
        os.environ.pop(key, None)
    os.environ.update(env or {})
    try:
        spec = importlib.util.spec_from_file_location(name, REVIEW_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for key in COMMAND_ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update(saved)


def claude_command_defaults(review):
    """Every module-level `*_CMD` default whose argv[0] is the claude CLI."""
    found = {}
    for attr in sorted(dir(review)):
        if not attr.endswith("_CMD"):
            continue
        value = getattr(review, attr)
        if isinstance(value, list) and value and os.path.basename(value[0]) == "claude":
            found[attr] = value
    return found


def commands_under_test(review):
    """The discovered claude commands, plus the known ones by name.

    The union matters: discovery alone would stop covering a command whose default was
    repointed at some other binary, and the named pair alone would not cover a new one.
    """
    found = claude_command_defaults(review)
    for name in KNOWN_CLAUDE_COMMANDS:
        assert hasattr(review, name), f"review.py no longer defines {name}"
        found.setdefault(name, getattr(review, name))
    return found


def settings_value(name, argv):
    """The argv entry following `--settings`, asserting the flag is present at all."""
    assert SETTINGS_FLAG in argv, (
        f"{name} default lost {SETTINGS_FLAG}: {shlex.join(argv)!r}. This is a SECURITY "
        "BOUNDARY (issue #493), not tuning — repo-controlled .claude/settings.json "
        "hooks execute without it."
    )
    index = argv.index(SETTINGS_FLAG)
    assert index + 1 < len(argv), f"{name}: {SETTINGS_FLAG} is the last argv entry, it has no value"
    return argv[index + 1]


def test_discovery_is_not_vacuous():
    review = load_review()
    found = claude_command_defaults(review)
    assert found, (
        "no *_CMD default invokes claude — the constants were probably renamed or "
        "repointed, which would make every assertion below pass vacuously"
    )
    missing = [n for n in KNOWN_CLAUDE_COMMANDS if n not in found]
    assert not missing, (
        f"these defaults used to invoke claude and no longer appear to: {missing}. "
        "If intended, update KNOWN_CLAUDE_COMMANDS deliberately."
    )
    for name, argv in sorted(found.items()):
        assert argv, f"{name} default is empty"
    print(f"ok  1. {len(found)} claude command defaults discovered: {sorted(found)}")


def test_every_claude_command_disables_hooks():
    """Path 1: a `.claude/settings.json` in the reviewed repo runs its hooks as the
    review-bot user, before the model sees anything. Asserted per command."""
    review = load_review()
    for name, argv in sorted(commands_under_test(review).items()):
        raw = settings_value(name, argv)
        parsed = json.loads(raw)
        for key, want in REQUIRED_SETTINGS.items():
            assert parsed.get(key) == want, (
                f"{name}: {SETTINGS_FLAG} value {raw!r} does not set {key}={want!r} — "
                "hooks declared by the REVIEWED repo would execute"
            )
    print(f"ok  2. every claude command passes {SETTINGS_FLAG} {json.dumps(REQUIRED_SETTINGS)}")


def test_every_claude_command_pins_mcp_config():
    """Path 2: a repo-root `.mcp.json` launches its stdio server at session start,
    independently of hooks and not closed by disabling them. Asserted per command."""
    review = load_review()
    for name, argv in sorted(commands_under_test(review).items()):
        assert STRICT_MCP_FLAG in argv, (
            f"{name} default lost {STRICT_MCP_FLAG}: {shlex.join(argv)!r}. This is a "
            "SECURITY BOUNDARY (issue #493), not tuning — a repo-root .mcp.json server "
            "is executed without it, and disabling hooks does not close that path."
        )
    print(f"ok  3. every claude command passes {STRICT_MCP_FLAG}")


def test_settings_json_survives_shlex_split():
    """The defaults are strings run through `shlex.split`, so the inline JSON has to be
    quoted. Unquoted, `{"disableAllHooks": true}` splits on its own space into two argv
    entries and claude is handed `{"disableAllHooks":` as a settings value."""
    review = load_review()
    for name, argv in sorted(commands_under_test(review).items()):
        assert argv.count(SETTINGS_FLAG) == 1, f"{name}: {SETTINGS_FLAG} appears more than once"
        raw = settings_value(name, argv)
        assert raw.startswith("{") and raw.endswith("}"), (
            f"{name}: {SETTINGS_FLAG} value {raw!r} is not one whole JSON object — the "
            "quoting in the default string is wrong"
        )
        json.loads(raw)  # round-trips; the shape is asserted in test 2
        smeared = [a for a in argv if a != raw and "disableAllHooks" in a]
        assert not smeared, f"{name}: the settings JSON leaked into other argv entries: {smeared}"
    print("ok  4. --settings and its JSON survive shlex.split as two argv entries")


def test_the_flags_are_a_default_not_a_floor():
    """Acceptance criterion 3: an override still replaces the WHOLE command. Hardening
    the default must not quietly become a mandatory prefix — the unit, `--dry-run`
    tuning and the stub engines the rest of this suite runs all depend on that."""
    review = load_review(
        {"REVIEW_BOT_CLAUDE_CMD": "stub-engine --flag", "REVIEW_BOT_SELECT_CMD": "other-stub"}
    )
    assert review.CLAUDE_CMD == ["stub-engine", "--flag"], review.CLAUDE_CMD
    assert review.SELECT_CMD == ["other-stub"], review.SELECT_CMD
    disabled = load_review({"REVIEW_BOT_SELECT_CMD": ""})
    assert disabled.SELECT_CMD == [], disabled.SELECT_CMD
    print("ok  5. env overrides still replace the whole command; empty still disables select")


def test_codex_command_is_deliberately_untouched():
    """The asymmetry is a finding, not an oversight: repo-level codex hook discovery was
    tested and does not exist, and these are claude flags codex would reject."""
    review = load_review()
    for flag in (SETTINGS_FLAG, STRICT_MCP_FLAG):
        assert flag not in review.CODEX_CMD, (
            f"CODEX_CMD gained {flag}, which is a claude flag: {shlex.join(review.CODEX_CMD)!r}"
        )
    print("ok  6. CODEX_CMD carries neither claude flag, as measured")


def test_readme_quotes_the_select_default_verbatim():
    """The README prints the select default as a command line, and nothing but this
    stops it drifting the moment the default changes (it already had to be corrected by
    hand for this change)."""
    review = load_review()
    readme = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    quoted = shlex.join(review.SELECT_CMD)
    assert quoted in readme, (
        f"README no longer quotes the REVIEW_BOT_SELECT_CMD default verbatim; expected "
        f"a line reading {quoted!r}"
    )
    assert STRICT_MCP_FLAG in readme, "README no longer explains the hardening flags"
    print("ok  7. README quotes the select default verbatim and names the flags")


def main():
    tests = [
        test_discovery_is_not_vacuous,
        test_every_claude_command_disables_hooks,
        test_every_claude_command_pins_mcp_config,
        test_settings_json_survives_shlex_split,
        test_the_flags_are_a_default_not_a_floor,
        test_codex_command_is_deliberately_untouched,
        test_readme_quotes_the_select_default_verbatim,
    ]
    for test in tests:
        test()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()

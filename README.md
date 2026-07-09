# review-bot

Automated Forgejo PR reviewer + issue-triage routine. A stdlib-only Python
program that runs a portable review prompt on a selectable engine (`claude` /
`codex`), then posts **one** Markdown comment to a Forgejo PR (or issue) as a
read-only `review-bot` identity via REST — it never pushes, never merges, never
uses `fj`.

- `review.py` → `review-bot-review` — the reviewer/triager (engine-agnostic).
- `poll.py`   → `review-bot-poll`   — scans readable Forgejo repos for
  `@review-bot` mention comments and dispatches the reviewer.
- `*-prompt.md` — the portable review / verify / synthesis / triage prompts.
- `default.nix` — `callPackage`-able derivation (deps: `python3`, `git`;
  `claude`/`codex` resolved from PATH at runtime).

## Status

Personal tool, developed for a single deployment (convox). Configuration is
env-overridable (`FORGEJO_URL`, `REVIEW_BOT_TOKEN_FILE`, `REVIEW_BOT_*`) but the
defaults assume that host. Shared in case it's useful — **unsupported**; issues
and PRs may go unanswered.

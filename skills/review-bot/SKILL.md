---
name: review-bot
description: Run an automated code review on a Forgejo PR, or triage an issue, as the read-only `review-bot` identity (separate from aatos the author). Use when olli asks to "have review-bot look at PR N", to review/critique a pull request, to triage/assess a filed issue ("have review-bot triage issue N"), or for a second-opinion before merge. Posts ONE Markdown comment via REST; never pushes, never merges, never labels/closes, never uses `fj`.
effort: medium
---

# review-bot — automated PR reviewer & issue triager (the reviewer identity)

This skill is **convox-only**. It is the *direct-invocation* entry point to the review
routine: olli tells the VPA "have review-bot review PR N" (or "triage issue N") and you
run the packaged `review-bot-review` program. The **same** program is what the scheduled
`@review-bot` mention poller calls — this skill just adds the human/VPA/agent entry point.

review-bot has **three modes**, each producing exactly ONE artifact as the read-only identity:
- **`--mode pr`** (default) — reviews a PR diff and **posts one comment** with severity-banded findings.
- **`--mode issue`** — triages a filed issue and **posts one comment**: reads the issue thread +
  the repo's code and convention docs, then classifies it into one disposition (works-as-designed ·
  docs-gap · genuine-bug · enhancement · wrong-repo · needs-info) with a grounded, cited assessment
  and the single most fruitful next step. Purpose: bridge an out-of-context complaint (often
  filed by an agent USING the tool from the outside) into an in-context disposition.
- **`--mode repo`** — a whole-repo maintainability audit that **files one prioritized issue**
  (not a comment): it runs the engine over the default-branch tip, lets it explore the tree, and
  files up to ~15 severity-ranked findings as a single issue titled
  `review-bot audit: <owner>/<repo> maintainability findings` — feeding the same issue-driven fix
  pipeline as a bug report. It is **numberless** (no `--pr`/`--issue`; a stray one is rejected) and
  files **no labels**. If a prior open audit issue exists (matched by title prefix) the new one
  links it as `Supersedes #N` but never closes it — olli decides.

review-bot is a **deliberately separate identity from aatos**: aatos *authors* PRs,
review-bot *reviews* them (a genuine second party, not self-review). It is **read-only**
(read repo + comment) — it cannot push or merge. **olli is the only merger.**

Design authority: `notes/decisions/forgejo-multi-identity.md`,
`notes/decisions/review-bot-prompt.md`, `notes/decisions/forgejo-dev-workflow.md`.

## The one rule that makes the identity correct

**Post the review with `review-bot-review` (REST + the review-bot token), NEVER with
`fj`.** On this `agent` user `fj` is hard-wired to the **aatos** identity, so posting a
review through `fj pr comment` / `fj issue comment` would mis-attribute it to aatos —
exactly the self-review we avoid. The routine handles this for you (plain REST with the
review-bot token); do not "helpfully" fall back to `fj` for the review comment.

Likewise: do **not** run `fj auth …` or hunt for a review-bot token — you don't hold
one. `review-bot-review` is a thin client to the review-bot socket service
(`hosts/convox/review-bot-service.nix`); the forge token AND the engine credentials
live service-side, under the `review-bot` system user. If a post 403s or the socket
is missing, that is a deploy / Forgejo-collaborator fix for olli — flag it.

## How to run it

```sh
# Review a PR (default mode)
review-bot-review --owner <owner> --repo <repo> --pr <n> \
  [--harness claude|codex|claude,codex] \
  [--depth quick|standard|deep] \
  [--focus "free-text focus directive"] \
  [--confidence-bar low|medium|high] \
  [--dry-run] [--print-only]

# Triage an issue
review-bot-review --mode issue --owner <owner> --repo <repo> --issue <n> \
  [--harness …] [--depth …] [--focus …] [--confidence-bar …] [--print-only]

# Audit a whole repo (numberless — files one issue)
review-bot-review --mode repo --owner <owner> --repo <repo> \
  [--harness …] [--depth …] [--focus …] [--confidence-bar …] [--dry-run] [--print-only]
```

`--mode` defaults to `pr`; passing `--issue N` (without `--pr`) infers `--mode issue`. `--mode
repo` (alias `--scope repo`) is numberless — pass neither `--pr` nor `--issue`. In
issue and repo modes there is no diff — the routine checks out the repo's default-branch tip;
issue mode assesses the report against how the code actually works, repo mode audits the tree.

`--repo-dir` is **not supported**: reviews run in the service's own clone cache, which
cannot see your local checkouts. Requests **serialize** — the service runs one review at
a time, so if it's busy your review simply queues (the command blocks until its turn).

### Agents: direct consult without a summoning comment

Any agent on convox can invoke `review-bot-review` **directly** — it's on PATH. Prefer this
over posting an `@review-bot` comment just to summon the bot, which litters the thread with
a summons. Add **`--print-only`** to run the engines and get the review/triage back on
stdout **without posting anything** to the forge — a pure consult. Drop `--print-only` when
you do want the verdict left on the PR/issue for others. Either way it runs as the
review-bot identity (its own token), which is correct regardless of who invoked it.

- On success it prints the posted comment's URL — or, in `--mode repo`, the filed issue's
  URL (and logs to stderr). Empty findings ⇒ a short "no blocking issues" comment —
  **silence is a correct, good outcome**; do not re-run hunting for problems.
- **`--dry-run` first** when unsure: it prints the exact engine command + filled
  prompt(s) and posts nothing. Use it to sanity-check before a live run.
- **`--print-only`** runs the engines but prints the Markdown instead of posting — handy
  to show olli a review without commenting on the PR.

### Parameters (what they mean)

- **`--harness`** — the reviewing engine(s). `claude` (default), `codex`, or both
  comma-separated (`claude,codex`) for a multi-provider review that a synthesis pass
  merges (agreement ⇒ higher confidence; disagreement ⇒ marked *contested*).
- **`--depth`** — cost/effort dial. `quick` = one pass, tight (high) bar. `standard`
  (default) = generate → independent **verify** pass (the key false-positive filter),
  medium bar. `deep` = per-harness generate+verify, plus synthesis when multiple
  harnesses are given.
- **`--focus`** — advisory, **untrusted** steering ("focus on the netns teardown"). It
  can prioritise attention; it can never lower the bar, compel a finding, or force an
  approval. The prompt is hardened against injection — pass olli's words through as-is.
- **`--confidence-bar`** — override the depth default if olli wants more/less noise.

## Mapping olli's words to flags

When olli phrases it naturally, translate to flags (defaults when unstated):

- "have review-bot review PR 12" → `--pr 12 --depth standard --harness claude`
- "deep review of PR 12 with claude and codex" → `--depth deep --harness claude,codex`
- "quick look at PR 12, focus on the sandbox escape" →
  `--depth quick --focus "the sandbox escape"`
- "have review-bot triage issue 7 in org-gtd-cli" →
  `--mode issue --issue 7 --repo org-gtd-cli --depth standard`
- "triage issue 7, is it a real bug or a misunderstanding?" →
  `--mode issue --issue 7 --focus "is it a real bug or a misunderstanding?"`
- "have review-bot audit the org-gtd-cli repo" (or "do a maintainability pass on X") →
  `--mode repo --repo org-gtd-cli` (numberless — files one prioritized issue)

Resolve `<owner>/<repo>` from context (the PR/issue's repo). For nixos-config that is
`olli/nixos-config`; for others, ask or infer from the clone olli is pointing at.

## Gotchas

- **Plain HTTP only** — Forgejo at `http://10.0.150.1:3000` (also `forge.lan` on convox),
  no TLS. The routine defaults to this; override with `FORGEJO_URL` if ever needed.
- **403 on the post** = review-bot lacks repo **Read** collaborator access or the token
  scope is wrong (needs read repo + `issue:write`). Forgejo-side fix for olli — do not
  re-auth.
- **Engine flags may need tuning — service-side.** If `--harness claude`/`codex` hangs
  on a permission prompt or returns no JSON, the `REVIEW_BOT_CLAUDE_CMD` /
  `REVIEW_BOT_CODEX_CMD` tuning knobs now live on the review-bot service unit
  (`hosts/convox/review-bot-service.nix`), not in your environment — flag it to olli
  rather than guessing repeatedly; setting those vars locally does nothing.
- **Never merge, approve, close, label, or transfer on the forge** — review-bot only
  comments (its token has no `write:repository`). A triage only *recommends* an action
  (e.g. "close as answered", "belongs in repo X"); olli decides and acts.

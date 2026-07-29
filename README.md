# review-bot

Automated Forgejo PR reviewer + issue-triage + whole-repo maintainability
auditor. A stdlib-only Python program that runs a portable prompt on a
selectable engine (`claude` / `codex`), then posts **one** Markdown comment to a
Forgejo PR (or issue) — or, in audit mode, **files one prioritized issue** — as a
read-only `review-bot` identity via REST. It never pushes, never merges, never
uses `fj`.

Three modes, sharing all the identity/git/engine/post plumbing:

- `--mode pr` (default) — reviews a PR diff and posts one review comment.
- `--mode issue` — triages a filed issue and posts one triage comment.
- `--mode repo` — runs a whole-repository maintainability pass (categories:
  duplication, dead code, layering drift, test-coverage-gap hotspots) and
  **creates one prioritized issue** (title `review-bot audit: <owner>/<repo>
  maintainability findings`) whose body is a ranked finding list. Takes **no**
  `--pr`/`--issue` number (`--scope repo` is an alias). It POSTs `{title, body}`
  to the create-issue endpoint (not comments), feeding the same issue-driven fix
  pipeline. Consistent with review-bot's read-only charter it applies **no
  labels** (it never touches the labels API); if a prior open audit issue exists
  it links it (`Supersedes #N`) rather than closing it.

- `review.py` → `review-bot-review-local` — the in-process reviewer/triager
  (engine-agnostic); also the module `review-bot-serve` imports. When reviewing
  a PR it fetches `refs/pull/N/head` and verifies the checked-out SHA equals
  the API's `meta["head"]["sha"]` — Forgejo populates the pull ref
  **asynchronously** after a push, so a review fired seconds later can
  otherwise land on the pre-push commit. Bounded exponential backoff
  (`REVIEW_BOT_HEAD_SYNC_RETRIES`, `REVIEW_BOT_HEAD_SYNC_BASE_SECS`) absorbs
  the propagation lag; a persistent mismatch aborts with a distinct error
  rather than review a stale tree (issue #16). For the diff boundary it prefers
  the forge-recorded `meta["merge_base"]` when that commit is available locally
  and remains an ancestor of the converged head. This preserves the original PR
  diff after the PR has been merged, when a live merge-base calculation would
  collapse to the PR head. An absent, unknown, or non-ancestor recorded value
  falls back to a live calculation with the reason logged. Regardless of the
  source, a zero-length diff aborts rather than being reviewed or posted as a
  vacuous pass.
- `serve.py`  → `review-bot-serve`  — inetd-style service entry point (see
  *Serve / client mode* below).
- `client.py` → `review-bot-review` — the credential-free client callers use.
- `poll.py`   → `review-bot-poll`   — scans readable Forgejo repos for
  `@review-bot` mention comments and dispatches the reviewer (via the client).
- `feedback.py` → `review-bot-feedback` — read-only fetch of review-bot's
  already-posted feedback for a PR/issue (see *Reading feedback back* below).
- `*-prompt.md` — the portable review / verify / synthesis / triage prompts.
- `default.nix` — `callPackage`-able derivation (deps: `python3`, `git`;
  `claude`/`codex` resolved from PATH at runtime).
- `tools/finder_ab.py` — operator instrument for the finder-stage A/B
  experiment (see *Diff input mode* below). Deliberately **not** packaged.

## Review output

At `standard` and `deep` depth, each usable finder draft normally goes through
verification. A zero-finding PR or repo draft skips that no-op engine call and
stays green, but the PR review makes the stage provenance explicit immediately
after its confidence-bar sentence:

> ⚠️ The finder stage returned no findings, so nothing was verified — this reports an empty finder, not a verified-clean diff.

If verification instead checks a non-empty draft and removes every finding, the
review says `All N draft finding(s) were checked and dropped by the verification stage.`

### The empty-finder diagnostic

The disclosure above says *that* the finder was empty; it cannot say **why**, and
there are two causes needing opposite fixes:

1. the engine genuinely answered `{"verdict":"approve","findings":[]}`, or
2. `normalize()` manufactured that answer. It defaults an absent `verdict` to
   `comment` and an absent or non-list `findings` to `[]`, so any balanced
   `{...}` scraped out of a prose reply — a schema fragment the engine was
   quoting back, say — parses as a clean, finding-free review with no other trace.

So every empty finder journals what the engine actually emitted (**stderr only** —
the posted comment is unchanged):

```
review-bot-review: EMPTY FINDER (claude): zero drafts, verify stage skipped. parse-path
  envelope-result, verdict 'approve' (present), findings list, top-level keys findings,summary,verdict
review-bot-review: empty-finder diagnostic: {"harness": "claude", "parse": {…}, "raw_excerpt": "…", …}
```

The second line is one JSON object: the harness, the raw output size and a
head+tail excerpt of it (4000 chars, with the elided count stated), whether the
JSON-repair retry fired, and — the discriminator — the *pre-normalization* facts
`verdict_present`, `verdict_raw`, `findings_kind`, `findings_len`, `keys` and the
`path` by which the JSON was reached, plus the `mode` that ran. Case 1 shows
`verdict_present: true` with `findings_kind: "list"`; case 2 shows the keys of whatever
was actually scraped.

The discriminator follows the schema of the mode that ran. An explicit `findings` list is
the universal tell; a `verdict` is required evidence in **PR mode only**, because the
audit schema carries none by design (`AUDIT_SCHEMA_HINT`, `normalize_audit`). Demanding
one everywhere would report every clean repo audit as a parse pathology — a false alarm in
the single signal this diagnostic exists to give.
Under `review-bot-serve` these land in the service journal; `poll.py` discards a
successful review's stderr, so read the service unit, not the poller.

**Triage has the same hole with a different key.** `normalize_triage` substitutes
`needs-info` whenever the engine supplies no `disposition` *or* an unrecognised one, so a
scraped fragment is posted as a confident triage nobody produced. Triage has no finder
stage and no draft count, so the empty-finder trigger cannot see it; it gets its own
trigger and its own line:

```
review-bot-review: DEFAULTED TRIAGE (claude): the engine supplied none; normalize_triage
  posted `needs-info` instead. parse-path envelope-result, top-level keys file,line_start
review-bot-review: defaulted-triage diagnostic: {"harness": "claude", "mode": "issue", …}
```
Review and audit footers also include a `findings` segment directly after the `bar`
segment, with each generator harness's draft and surviving counts in pipeline
order—for example, ``findings `claude 3→1, codex 2→0, synthesized` ``. The
`synthesized` suffix appears when the multi-harness synthesis stage ran.

## Diff input mode

A PR review feeds the finder either the **full diff** or, when the diff exceeds
`REVIEW_BOT_DIFF_CAP` (default 60000 chars), only the **file list** plus the
instruction to read the checked-out tree. Which one the finder got changes what
it can find, so both are now disclosed rather than inferred:

- a journal line, emitted before any engine runs —
  `review-bot-review: diff 65525 chars vs cap 60000 — file-list only`
  (the alternative wording is `— inlined`);
- a review footer segment directly after `findings` and before `merge-base`:
  ``diff `inlined` `` or ``diff `file-list` ``.

Both come from the single `len(diff) <= REVIEW_BOT_DIFF_CAP` comparison
`changed_files_block` already made — a diff of exactly the cap is inlined — so
the disclosure can never drift from the input the engine actually saw. PR mode
only: triage and audit footers are unchanged.

### The A/B harness

`tools/finder_ab.py` measures whether the input mode changes finder yield. It
runs a full factorial over one PR — harness ∈ {`claude`, `codex`} × input mode ∈
{forced-inline, forced-elide} × `--runs` (default 5) — at a fixed `--depth`
(default `standard`) and `--confidence-bar` (default `medium`):

```
python3 tools/finder_ab.py --owner O --repo R --pr N [--runs 5] [--out finder-ab.jsonl]
```

The input mode is forced through the shipped knob and nothing else:
`REVIEW_BOT_DIFF_CAP=100000000` (nothing elides) versus `=1` (everything does).
It **never posts** — every invocation carries `--print-only` — and it drives
`review-bot-review-local`, not the `review-bot-review` socket client, because
the client cannot carry the cap (`review-bot-serve` whitelists request fields
and honours engine/env settings only from its own trusted environment). Running
local therefore needs the forge token plus live engine credentials. By default
it `nix-build`s the package and uses `$out/bin/review-bot-review-local`;
`--binary PATH` overrides that.

Every run appends one JSON object to `--out` (harness, forced cap, run index,
exit status, the draft→surviving counts and diff mode parsed from the rendered
footer, the **measured diff size and the cap actually applied** read off the
reviewer's journal line, and the verdict); a run whose review aborts is recorded
**with** its status rather than dropped. Cells are comparable by construction:
the PR head is re-read before every run and a moved head aborts the experiment
instead of mixing two diffs into one cell. The summary table has one row per
cell with the run count, aborted count, number of empty drafts, and the mean
draft and surviving finding counts.

A run whose finder came back empty additionally carries `empty_finder_diag` — the
reviewer's own diagnostic (above), lifted off stderr — so a rare empty cell is
explainable from the record instead of only reproducible by re-running. When the
line is absent (a binary predating it) the field is `null` and the stderr tail is
kept in its place; it is never inferred. After the summary table the harness
prints one line per empty run classifying it as a *genuine empty verdict* or as
`DEFAULTED — not a real review object`.

**Target a PR whose diff against its merge base is non-empty.** Since #26
`review.py` prefers the forge-recorded merge base, so a *merged* PR normally
still yields its original diff — merged PRs are in fact the better subjects,
since their heads are frozen and the runs are reproducible. A 0-char diff now
means either the PR really changes nothing, or the recorded base was unusable
and the live fallback collapsed to the head; `review.py`'s
`merge base … (computed live — <reason>)` journal line says which.

An empty diff satisfies `0 <= cap`, which makes the footer report `inlined` even
in the forced-elide cells and every cell report zero findings — a null
experiment that *looks* like a clean result. The harness therefore refuses a
0-char diff outright rather than tabulating it, and the recorded diff size is
what distinguishes "the cap did not take" from "there was nothing to review".

The harness refuses the symmetric masquerade too: if the cap the reviewer
actually applied differs from the one forced, the run aborts. Otherwise every
cell would share one input mode and the matching means would read as the
experiment's conclusion rather than as a broken instrument — the usual cause
being `--binary` pointed at `review-bot-review`, the socket client, which
silently drops `REVIEW_BOT_DIFF_CAP`.

## Serve / client mode

Running the pipeline in-process means the calling user must hold the forge
token **and** live LLM OAuth credentials (`CLAUDE_CONFIG_DIR` /
`CODEX_HOME`) — the engine subprocess inherits the caller's environment. The
serve/client split inverts that:

- `review-bot-serve` runs as a systemd **socket-activated service**
  (`Accept=yes`, inetd-style: one connection = one unit instance with
  stdin/stdout wired to the socket) under a dedicated user that owns the
  credentials. The socket unit's `MaxConnections=N` (default `N=4`) bounds
  how many client connections systemd accepts in parallel — each spawning a
  fresh `review-bot-serve` instance. Reviews themselves are still **serialized
  one at a time** by a process-wide exclusive `fcntl.flock` on
  `$REVIEW_BOT_LOCK_FILE` (default `~/review-serve.lock`) that every serve
  instance takes around the review pipeline; the second-and-later instance
  emits a `{"type":"log","message":"queued: …"}` progress line and blocks
  until it is its turn (issue #17). This lock is distinct from review.py's
  per-repo `{owner}__{repo}.lock` (issue #1). Serialization is required
  because both engines share the service's OAuth refresh-token store and
  concurrent runs would invalidate each other's credentials.
- `review-bot-review` (the binary on caller PATHs) is now a thin client: same
  argv as before, so `poll.py` and every other caller migrate by doing
  nothing. It serializes the flags to a one-line JSON request, sends it over
  the Unix socket at `$REVIEW_BOT_SOCKET` (default
  `/run/review-bot/review.sock`), streams the response, and prints the
  markdown (`--print-only`) or posted comment URL exactly as before,
  exiting 0/1. Callers never see a credential.

  A request **beyond** `MaxConnections=N` is refused by systemd — zero
  NDJSON events reach the client. The client distinguishes this *busy-drop*
  from a genuine mid-review connection loss by tracking whether any event
  was received: **zero events** ⇒ busy, retried automatically with bounded
  exponential backoff (base 1s, factor 2, cap 30s, count
  `REVIEW_BOT_BUSY_RETRIES`, default `6`); if still busy after the budget
  the client exits **75** (`EX_TEMPFAIL`) with a truthful busy message,
  never the misleading `connection was lost` text. **≥1 event** ⇒ genuine
  loss: exits 1 with `connection was lost` and the outcome-unknown
  guidance, exactly as before.
- `review-bot-review-local` is the old direct-execution path — it requires
  local credentials and is what the service itself runs (as an import).

### Protocol

Request: a **single JSON object on one line**, max 64 KiB, read timeout ~30 s.
Fields are whitelisted (an unknown field is a hard error):

| field            | type | notes                                          |
|------------------|------|------------------------------------------------|
| `mode`           | str  | `"pr"` (default) \| `"issue"` \| `"repo"`      |
| `owner`, `repo`  | str  | required; `[A-Za-z0-9_.-]+`                    |
| `number`         | int  | positive PR/issue number; required for pr/issue, **omitted for `repo`** (numberless audit) |
| `harness`        | str  | `claude` \| `codex` \| `claude,codex`          |
| `depth`          | str  | `quick` \| `standard` \| `deep`                |
| `confidence_bar` | str  | `""` \| `low` \| `medium` \| `high`            |
| `focus`          | str  | free text, capped at 2000 chars                |
| `print_only`     | bool | return markdown instead of posting             |
| `dry_run`        | bool | print prompts to the journal, run no engines   |

Deliberately **not** accepted: `repo_dir` (the service must not read arbitrary
caller paths) and engine-command overrides (`REVIEW_BOT_CLAUDE_CMD` /
`REVIEW_BOT_CODEX_CMD` are honored only from the service's own trusted unit
environment, never from the request).

Response: NDJSON events on the socket — optional
`{"type":"log","message":…}` progress lines, then exactly one final

```json
{"type":"result","ok":bool,"markdown":string|null,"url":string|null,"error":string|null}
```

Invalid requests still get a `result` event (`ok:false`) and a nonzero exit.
When stdin is the connection socket, the peer's uid/pid (`SO_PEERCRED`) is
logged to the journal for audit.

## Reading feedback back

After review-bot comments on a PR (or triages an issue), `review-bot-feedback`
pulls that feedback back **programmatically** (issue #2). It is a pure READ:

```
review-bot-feedback --owner O --repo R (--pr N | --issue N) \
                    [--json|--markdown] [--all] [--kind review,triage,parked]
```

Unlike `review-bot-review`, it needs **only a forge READ token** — no LLM
credentials and no engine socket — so it speaks REST directly and ships as its
own bin. It is **never** routed through `review-bot-serve` (that would make a
cheap read block behind the service's `MaxConnections=1` engine slot). It is
strictly read-only: it never posts, labels, or closes.

- `--pr N` / `--issue N` — the target; PR and issue comments share the same
  endpoint, so one path serves both. Give exactly one.
- A comment counts as review-bot's iff its author login is in the handle set
  (default `review-bot`, `review_bot`; override via `REVIEW_BOT_HANDLES`, same
  as the poller). Each matched comment is classified by its footer marker into
  a `kind`: `review`, `triage`, `parked`, or `other`.
- `--json` (default) emits an envelope:

  ```json
  {
    "repo": "owner/repo", "number": 70, "target": "pr",
    "latest": {"id":123, "html_url":"…", "created_at":"…",
               "author":"review-bot", "kind":"review", "body_markdown":"…"},
    "all": [ … ]
  }
  ```

  `"all"` (newest-first) is present only with `--all`.
- `--markdown` prints just the latest matched comment's markdown body.
- `--kind review,triage` filters to those classifications (default: all kinds).
- "Latest" is the most recent matched comment by `created_at` (id as a
  tiebreak). If review-bot has never commented on the thread (after any
  `--kind` filter), it prints a message to stderr and **exits non-zero**.

Token source, in order: `FORGEJO_TOKEN` env → `REVIEW_BOT_TOKEN_FILE` / the
standard token-file candidates → else an error with guidance. Any token that
can read the repo works.

## Status

Personal tool, developed for a single deployment (convox). Configuration is
env-overridable (`FORGEJO_URL`, `REVIEW_BOT_TOKEN_FILE`, `REVIEW_BOT_*`) but the
defaults assume that host. Shared in case it's useful — **unsupported**; issues
and PRs may go unanswered.

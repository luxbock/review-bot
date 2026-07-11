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
  maintainability findings`, labels `audit,review-bot`) whose body is a ranked
  finding list. Takes **no** `--pr`/`--issue` number (`--scope repo` is an
  alias). It POSTs to the create-issue endpoint (not comments), feeding the same
  issue-driven fix pipeline. If label creation fails (labels absent on the repo)
  it retries label-free; if a prior open audit issue exists it links it
  (`Supersedes #N`) rather than closing it.

- `review.py` → `review-bot-review-local` — the in-process reviewer/triager
  (engine-agnostic); also the module `review-bot-serve` imports.
- `serve.py`  → `review-bot-serve`  — inetd-style service entry point (see
  *Serve / client mode* below).
- `client.py` → `review-bot-review` — the credential-free client callers use.
- `poll.py`   → `review-bot-poll`   — scans readable Forgejo repos for
  `@review-bot` mention comments and dispatches the reviewer (via the client).
- `*-prompt.md` — the portable review / verify / synthesis / triage prompts.
- `default.nix` — `callPackage`-able derivation (deps: `python3`, `git`;
  `claude`/`codex` resolved from PATH at runtime).

## Serve / client mode

Running the pipeline in-process means the calling user must hold the forge
token **and** live LLM OAuth credentials (`CLAUDE_CONFIG_DIR` /
`CODEX_HOME`) — the engine subprocess inherits the caller's environment. The
serve/client split inverts that:

- `review-bot-serve` runs as a systemd **socket-activated service**
  (`Accept=yes`, inetd-style: one connection = one unit instance with
  stdin/stdout wired to the socket) under a dedicated user that owns the
  credentials. `MaxConnections=1` serializes requests in the listen backlog,
  which also prevents concurrent runs from fighting over the shared cache
  clone (issue #1).
- `review-bot-review` (the binary on caller PATHs) is now a thin client: same
  argv as before, so `poll.py` and every other caller migrate by doing
  nothing. It serializes the flags to a one-line JSON request, sends it over
  the Unix socket at `$REVIEW_BOT_SOCKET` (default
  `/run/review-bot/review.sock`), streams the response, and prints the
  markdown (`--print-only`) or posted comment URL exactly as before,
  exiting 0/1. Callers never see a credential.
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

## Status

Personal tool, developed for a single deployment (convox). Configuration is
env-overridable (`FORGEJO_URL`, `REVIEW_BOT_TOKEN_FILE`, `REVIEW_BOT_*`) but the
defaults assume that host. Shared in case it's useful — **unsupported**; issues
and PRs may go unanswered.

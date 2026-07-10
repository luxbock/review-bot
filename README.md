# review-bot

Automated Forgejo PR reviewer + issue-triage routine. A stdlib-only Python
program that runs a portable review prompt on a selectable engine (`claude` /
`codex`), then posts **one** Markdown comment to a Forgejo PR (or issue) as a
read-only `review-bot` identity via REST — it never pushes, never merges, never
uses `fj`.

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
| `mode`           | str  | `"pr"` (default) \| `"issue"`                  |
| `owner`, `repo`  | str  | required; `[A-Za-z0-9_.-]+`                    |
| `number`         | int  | required; positive PR/issue number             |
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

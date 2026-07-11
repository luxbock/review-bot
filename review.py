#!@PYTHON@
"""review-bot-review-local — the reusable PR-review / issue-triage routine (subtask #3 core).

This is the IN-PROCESS implementation: running it directly requires local
credentials (the forge token plus CLAUDE_CONFIG_DIR/CODEX_HOME for the engine
subprocesses). It is installed two ways:
  - `review-bot-review-local` — direct CLI, for the service user / debugging;
  - imported as a module by `review-bot-serve` (the socket-activated service).
Ordinary callers use `review-bot-review`, which is now a thin CLIENT (client.py)
speaking to the service over a Unix socket and holding no credentials.

Two modes, sharing all the identity/git/engine/post plumbing:

`--mode pr` (default) — reviews a PR diff:
  1. fetches the PR branch into a cache clone, at the merge base;
  2. resolves the repo's own convention files (CLAUDE.md / AGENTS.md / …);
  3. fills the portable review prompt and runs it on the selected engine(s);
  4. (depth>quick) runs an independent verify pass; (multi-harness) synthesises;
  5. renders the JSON verdict+findings into ONE Markdown comment and POSTs it to
     the PR via the issues/comments REST endpoint, as the review-bot identity.

`--mode issue` — triages a filed issue: checks out the repo's default-branch tip (no
diff), feeds the issue thread + convention files to the triage prompt, and renders a
disposition (works-as-designed / docs-gap / genuine-bug / enhancement / wrong-repo /
needs-info) with a grounded assessment + recommended next step. Same verify/synthesis
dial, same one-comment POST (PRs and issues share the issues/comments endpoint).

review-bot is READ-ONLY (read repo + issue:write). It posts via plain REST with the
review-bot token in the Authorization header — NEVER via `fj` (on the agent user `fj`
is hard-wired to aatos, so it would mis-attribute the review). It never pushes/merges;
olli is the only merger. Design: notes/decisions/forgejo-multi-identity.md,
notes/decisions/review-bot-prompt.md.

Both invocation paths (the scheduled @review-bot poller, and direct VPA invocation)
call THIS program — it is the single reusable unit.
"""

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import NoReturn

# ── Build-time substituted constants (see default.nix) ─────────────────────────
GIT = "@GIT@"
REVIEW_PROMPT_FILE = "@REVIEW_PROMPT@"
VERIFY_PROMPT_FILE = "@VERIFY_PROMPT@"
SYNTHESIS_PROMPT_FILE = "@SYNTHESIS_PROMPT@"
TRIAGE_PROMPT_FILE = "@TRIAGE_PROMPT@"
TRIAGE_VERIFY_PROMPT_FILE = "@TRIAGE_VERIFY_PROMPT@"
TRIAGE_SYNTHESIS_PROMPT_FILE = "@TRIAGE_SYNTHESIS_PROMPT@"
AUDIT_PROMPT_FILE = "@AUDIT_PROMPT@"
AUDIT_VERIFY_PROMPT_FILE = "@AUDIT_VERIFY_PROMPT@"
AUDIT_SYNTHESIS_PROMPT_FILE = "@AUDIT_SYNTHESIS_PROMPT@"

# ── Runtime config (env-overridable so olli can tune without a rebuild) ────────
FORGE_URL = os.environ.get("FORGEJO_URL", "http://10.0.150.1:3000").rstrip("/")
TOKEN_FILE_ENV = os.environ.get("REVIEW_BOT_TOKEN_FILE", "")
TOKEN_FILE_CANDIDATES = [
    TOKEN_FILE_ENV,
    "/home/agent/.config/review-bot/token",
    os.path.expanduser("~/.config/review-bot/token"),
]
CACHE_ROOT = os.environ.get(
    "REVIEW_BOT_CACHE",
    os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "review-bot"),
)
ENGINE_TIMEOUT = int(os.environ.get("REVIEW_BOT_ENGINE_TIMEOUT", "1800"))
DIFF_INLINE_CAP = int(os.environ.get("REVIEW_BOT_DIFF_CAP", "60000"))

# The harness commands are env-overridable because the exact CLI flags for headless
# review (esp. tool-permission flags) may need tuning against the live engines —
# validate with --dry-run, then adjust REVIEW_BOT_CLAUDE_CMD / _CODEX_CMD if needed.
CLAUDE_CMD = shlex.split(
    os.environ.get(
        "REVIEW_BOT_CLAUDE_CMD",
        "claude -p --output-format json --allowedTools Read,Grep,Glob,Bash",
    )
)
CODEX_CMD = shlex.split(os.environ.get("REVIEW_BOT_CODEX_CMD", "codex exec --skip-git-repo-check -"))

SEVERITY_ORDER = ["blocker", "major", "minor", "nit", "question"]
SEVERITY_EMOJI = {
    "blocker": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "nit": "⚪",
    "question": "🔵",
}
VERDICT_LABEL = {
    "approve": "✅ no blocking issues",
    "comment": "💬 comments",
    "request_changes": "🛑 changes requested",
}
# Confidence-bar default per depth (the depth dial's first knob). --confidence-bar wins.
BAR_BY_DEPTH = {"quick": "high", "standard": "medium", "deep": "medium"}

# ── issue-triage vocabulary (mode=issue) ───────────────────────────────────────
# The six triage buckets (see triage-prompt.md). needs-info is the safe default the
# routine falls back to when the engine emits something outside the enum.
DISPOSITIONS = [
    "genuine-bug",
    "enhancement",
    "docs-gap",
    "wrong-repo",
    "works-as-designed",
    "needs-info",
]
DISPOSITION_LABEL = {
    "genuine-bug": "🐛 genuine bug",
    "enhancement": "✨ enhancement / unmet need",
    "docs-gap": "📄 documentation gap",
    "wrong-repo": "↪️ out of scope / wrong repo",
    "works-as-designed": "✅ works as designed",
    "needs-info": "❓ needs more info",
}


def die(msg, code=1) -> NoReturn:
    print(f"review-bot-review: error: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg):
    print(f"review-bot-review: {msg}", file=sys.stderr)


# ── Forgejo REST (token in the Authorization header; never fj) ─────────────────
def api(method, path, token, data=None):
    url = f"{FORGE_URL}/api/v1/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        hint = ""
        if e.code in (401, 403):
            hint = (
                " — review-bot may lack repo read access (add it as a Read collaborator) "
                "or the token scope is wrong (needs read repo + issue:write). "
                "This is a Forgejo-side fix for olli; do NOT re-auth/regenerate blindly."
            )
        die(f"{method} {path} -> HTTP {e.code}{hint}\n{detail}")
    except urllib.error.URLError as e:
        die(f"{method} {path} -> {e.reason} (is {FORGE_URL} reachable from here?)")


def api_paged(path, token):
    """GET every page of a list endpoint (Forgejo caps a single page, so a long issue
    thread would otherwise be silently truncated to page 1)."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        chunk = api("GET", f"{path}{sep}page={page}&limit=50", token)
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 50:
            break
        page += 1
    return out


# ── git (auth via a throwaway gitconfig — keeps the token out of argv and out of
#    the agent's global config, so review-bot's git NEVER picks up aatos creds) ──
class GitAuth:
    def __init__(self, token):
        self._dir = tempfile.TemporaryDirectory(prefix="review-bot-git-")
        self.config = os.path.join(self._dir.name, "gitconfig")
        # Forgejo's git smart-HTTP endpoints authenticate via Basic auth (NOT the
        # `Authorization: token` scheme that the REST API uses). Forgejo accepts an
        # access token AS the Basic-auth username, so we send base64("<token>:") —
        # this needs no real username, sidestepping the review-bot/review_bot handle
        # ambiguity. (The REST calls in api() still use the `token` scheme.)
        basic = base64.b64encode(f"{token}:".encode()).decode()
        with open(self.config, "w") as f:
            f.write("[http]\n")
            f.write(f"\textraHeader = Authorization: Basic {basic}\n")
        os.chmod(self.config, 0o600)

    def env(self):
        e = dict(os.environ)
        e["GIT_CONFIG_GLOBAL"] = self.config
        e["GIT_CONFIG_NOSYSTEM"] = "1"
        e["GIT_TERMINAL_PROMPT"] = "0"
        return e

    def cleanup(self):
        self._dir.cleanup()


def git(args, cwd, auth, check=True, capture=True):
    proc = subprocess.run(
        [GIT, *args],
        cwd=cwd,
        env=auth.env(),
        capture_output=capture,
        text=True,
    )
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed (rc={proc.returncode}):\n{proc.stderr}")
    return proc


def ensure_clone(owner, repo, auth, repo_dir=None):
    """Return a git working dir for owner/repo — an existing --repo-dir or a cache clone."""
    if repo_dir:
        cdir = os.path.abspath(repo_dir)
        if not os.path.isdir(os.path.join(cdir, ".git")):
            die(f"--repo-dir {cdir} is not a git repository")
        return cdir
    os.makedirs(CACHE_ROOT, exist_ok=True)
    cdir = os.path.join(CACHE_ROOT, f"{owner}__{repo}")
    if not os.path.isdir(os.path.join(cdir, ".git")):
        log(f"cloning {owner}/{repo} into cache {cdir}")
        git(["clone", "--quiet", f"{FORGE_URL}/{owner}/{repo}.git", cdir], cwd=CACHE_ROOT, auth=auth)
    return cdir


def prepare_checkout(owner, repo, pr, base_ref, auth, repo_dir=None):
    """Clone/fetch into a cache dir, fetch the PR head + base, return (dir, merge_base)."""
    cdir = ensure_clone(owner, repo, auth, repo_dir)
    log(f"fetching base {base_ref} + PR #{pr} head")
    git(["fetch", "--quiet", "origin", f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"], cwd=cdir, auth=auth)
    git(["fetch", "--quiet", "origin", f"+refs/pull/{pr}/head:refs/review-bot/pr-{pr}"], cwd=cdir, auth=auth)

    mb = git(["merge-base", f"refs/remotes/origin/{base_ref}", f"refs/review-bot/pr-{pr}"], cwd=cdir, auth=auth)
    merge_base = mb.stdout.strip()
    git(["checkout", "--quiet", "--detach", f"refs/review-bot/pr-{pr}"], cwd=cdir, auth=auth)
    return cdir, merge_base


def changed_files_block(cdir, merge_base, auth):
    # The cache clone is checked out detached at the PR head, so HEAD is the head.
    stat = git(["diff", "--stat", f"{merge_base}..HEAD"], cwd=cdir, auth=auth).stdout
    diff = git(["diff", f"{merge_base}..HEAD"], cwd=cdir, auth=auth).stdout
    if len(diff) <= DIFF_INLINE_CAP:
        return f"{stat}\n```diff\n{diff}\n```"
    return (
        f"{stat}\n\n(diff is large — only the file list is inlined. The repo is checked "
        f"out at the PR head; run `git diff {merge_base[:12]}..HEAD -- <file>` to inspect "
        f"specific hunks.)"
    )


# ── issue-triage input (mode=issue) ────────────────────────────────────────────
def prepare_head_checkout(owner, repo, default_branch, auth, repo_dir=None):
    """Check out the tip of the default branch (issue triage reads code, not a diff)."""
    cdir = ensure_clone(owner, repo, auth, repo_dir)
    log(f"fetching {default_branch} tip for triage")
    git(
        ["fetch", "--quiet", "origin", f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}"],
        cwd=cdir,
        auth=auth,
    )
    git(["checkout", "--quiet", "--detach", f"refs/remotes/origin/{default_branch}"], cwd=cdir, auth=auth)
    head = git(["rev-parse", "HEAD"], cwd=cdir, auth=auth).stdout.strip()
    return cdir, head


def issue_context_block(issue, comments):
    """Render the issue + thread as a single untrusted-data block for the prompt."""
    labels = ", ".join(lb.get("name", "") for lb in (issue.get("labels") or [])) or "(none)"
    parts = [
        f"Title: {issue.get('title', '') or '(no title)'}",
        f"State: {issue.get('state', '') or '?'}",
        f"Reporter: @{issue.get('user', {}).get('login', '') or '?'}",
        f"Labels: {labels}",
        "",
        "--- issue body ---",
        (issue.get("body") or "(empty body)").strip(),
    ]
    for c in comments:
        author = c.get("user", {}).get("login", "") or "?"
        parts += ["", f"--- comment by @{author} ---", (c.get("body") or "").strip()]
    text = "\n".join(parts)
    if len(text) > DIFF_INLINE_CAP:
        text = text[:DIFF_INLINE_CAP] + "\n\n(issue thread truncated — too long to inline in full)"
    return text


# ── convention-file discovery (repo-agnostic) ──────────────────────────────────
def convention_files(cdir):
    import glob as _glob

    found = []
    exact = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", "README.md", "README.rst", "README",
             "notes/INDEX.md", "notes/README.md", ".cursorrules",
             # Behavioral spec (factory projects): the same-PR contract for what the
             # code DOES — see notes/decisions/spec-maintenance-policy.md. Only picked
             # up when present, so this stays repo-agnostic.
             "docs/design.md", "docs/DESIGN.md", "DESIGN.md"]
    for name in exact:
        if os.path.exists(os.path.join(cdir, name)):
            found.append(name)
    for pat in ["CONTRIBUTING*", "docs/CONTRIBUTING*"]:
        for p in _glob.glob(os.path.join(cdir, pat)):
            rel = os.path.relpath(p, cdir)
            if rel not in found:
                found.append(rel)
    # de-dupe a README family down to the first that exists, keep the rest as-is.
    return found


# ── prompt filling ─────────────────────────────────────────────────────────────
def fill(template_path, mapping):
    with open(template_path) as f:
        text = f.read()
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ── engine invocation ──────────────────────────────────────────────────────────
def run_engine(harness, prompt, cwd, dry_run=False):
    cmd = CLAUDE_CMD if harness == "claude" else CODEX_CMD
    if not cmd:
        die(f"empty command for harness {harness}")
    if dry_run:
        print(f"\n===== DRY RUN: {harness} =====", file=sys.stderr)
        print(f"$ (cwd={cwd}) {' '.join(shlex.quote(c) for c in cmd)} <<'PROMPT'", file=sys.stderr)
        print(prompt, file=sys.stderr)
        print("PROMPT", file=sys.stderr)
        return None
    log(f"running {harness} ({cmd[0]}) in {cwd} …")
    try:
        proc = subprocess.run(
            cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=ENGINE_TIMEOUT
        )
    except FileNotFoundError:
        die(f"harness binary not found: {cmd[0]} (is {harness} on PATH?)")
    except subprocess.TimeoutExpired:
        die(f"{harness} timed out after {ENGINE_TIMEOUT}s")
    if proc.returncode != 0:
        # Surface SOMETHING on failure. Some harnesses (notably
        # `claude -p --output-format json`) exit non-zero with EMPTY stderr and
        # put their error on stdout — reporting only stderr made the claude
        # harness fail silently ("exited 1" with a blank tail). Fall back to
        # stdout, then to an explicit marker, so a failure is never invisible.
        detail = (proc.stderr or "").strip()
        if not detail:
            detail = (proc.stdout or "").strip() or "(no output on stderr or stdout)"
        die(f"{harness} exited {proc.returncode}:\n{detail[-2000:]}")
    return proc.stdout


def find_json_object(text):
    """Extract the first balanced {...} JSON object from arbitrary text."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


def normalize(obj):
    if not isinstance(obj, dict):
        die("engine returned non-object JSON")
    verdict = obj.get("verdict", "comment")
    if verdict not in VERDICT_LABEL:
        verdict = "comment"
    findings = obj.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    clean = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity", "question")
        if sev not in SEVERITY_ORDER:
            sev = "question"
        clean.append(
            {
                "file": str(f.get("file", "") or ""),
                "line_start": f.get("line_start"),
                "line_end": f.get("line_end"),
                "severity": sev,
                "confidence": f.get("confidence", "medium"),
                "title": str(f.get("title", "") or "(untitled finding)"),
                "rationale": str(f.get("rationale", "") or ""),
                "suggestion": str(f.get("suggestion", "") or ""),
            }
        )
    return {"verdict": verdict, "summary": str(obj.get("summary", "") or ""), "findings": clean}


def normalize_triage(obj):
    if not isinstance(obj, dict):
        die("engine returned non-object JSON")
    disp = obj.get("disposition", "needs-info")
    if disp not in DISPOSITIONS:
        disp = "needs-info"
    conf = obj.get("confidence", "medium")
    if conf not in ("high", "medium", "low"):
        conf = "medium"
    return {
        "disposition": disp,
        "confidence": conf,
        "summary": str(obj.get("summary", "") or ""),
        "assessment": str(obj.get("assessment", "") or ""),
        "grounding": str(obj.get("grounding", "") or ""),
        "recommended_action": str(obj.get("recommended_action", "") or ""),
    }


def normalize_audit(obj):
    """Normalize the audit schema: a ranked finding list with NO verdict. Reuses the same
    finding shape and severity sanitising as normalize(); preserves the engine's ordering
    (findings are returned most-severe-first, so we do NOT re-sort here)."""
    if not isinstance(obj, dict):
        die("engine returned non-object JSON")
    findings = obj.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    clean = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity", "question")
        if sev not in SEVERITY_ORDER:
            sev = "question"
        clean.append(
            {
                "file": str(f.get("file", "") or ""),
                "line_start": f.get("line_start"),
                "line_end": f.get("line_end"),
                "severity": sev,
                "confidence": f.get("confidence", "medium"),
                "title": str(f.get("title", "") or "(untitled finding)"),
                "rationale": str(f.get("rationale", "") or ""),
                "suggestion": str(f.get("suggestion", "") or ""),
            }
        )
    return {"summary": str(obj.get("summary", "") or ""), "findings": clean}


# Compact schema reminders for the reformat retry (the full schemas live in the prompt
# files; this is just enough for the engine to re-emit its own conclusions as JSON).
REVIEW_SCHEMA_HINT = (
    '{"verdict":"approve|comment|request_changes","summary":"...",'
    '"findings":[{"file":"...","line_start":N,"line_end":N,'
    '"severity":"blocker|major|minor|nit|question","confidence":"high|medium|low",'
    '"title":"...","rationale":"...","suggestion":"..."}]}'
)
TRIAGE_SCHEMA_HINT = (
    '{"summary":"...","assessment":"...","grounding":"...","recommended_action":"...",'
    '"confidence":"high|medium|low",'
    '"disposition":"works-as-designed|docs-gap|genuine-bug|enhancement|wrong-repo|needs-info"}'
)
AUDIT_SCHEMA_HINT = (
    '{"summary":"...","findings":[{"file":"...","line_start":N,"line_end":N,'
    '"severity":"blocker|major|minor|nit|question","confidence":"high|medium|low",'
    '"title":"...","rationale":"...","suggestion":"..."}]}  // NO verdict; findings ranked most-severe-first'
)
REFORMAT_INSTRUCTION = (
    "Your previous response was NOT valid JSON. Re-express EXACTLY the same conclusions "
    "as a single JSON object and nothing else — no prose, no markdown fences, no text "
    "before or after. Do not add, drop, upgrade, or soften anything; only change the "
    "format. If your previous response reported no findings, emit the empty/approve form. "
    "Required schema:\n{schema}\n\nYour previous response was:\n{prior}"
)


def _parse_engine_output(raw, harness, key, norm):
    """Raw engine stdout -> (normalized_obj_or_None, inner_text_for_a_repair_retry)."""
    text = raw
    if harness == "claude":
        # `claude -p --output-format json` wraps the answer in an envelope; the real
        # answer is the `result` string (which should itself be the review JSON).
        try:
            env = json.loads(raw)
            if isinstance(env, dict) and isinstance(env.get("result"), str):
                text = env["result"]
            elif isinstance(env, dict) and key in env:
                return norm(env), text
        except json.JSONDecodeError:
            text = raw
    obj = find_json_object(text)
    return (norm(obj) if obj is not None else None), text


def review_via(harness, prompt, cwd, dry_run, mode="pr"):
    raw = run_engine(harness, prompt, cwd, dry_run=dry_run)
    if dry_run or raw is None:
        return None
    if mode == "issue":
        norm, key = normalize_triage, "disposition"
    elif mode == "repo":
        norm, key = normalize_audit, "findings"
    else:
        norm, key = normalize, "verdict"

    result, text = _parse_engine_output(raw, harness, key, norm)
    if result is not None:
        return result

    # One JSON-repair retry. Engines occasionally answer in prose despite the prompt's
    # "output ONLY JSON" (observed deterministically with `claude -p` on single-finding
    # reviews) — which otherwise fails the whole review even though the analysis was
    # sound. Ask the engine to reformat its own prior output as strict JSON before giving
    # up: cheaper and more faithful than discarding the review or re-generating it.
    log(f"{harness} did not return parseable JSON; attempting one reformat retry")
    schema_hint = {"issue": TRIAGE_SCHEMA_HINT, "repo": AUDIT_SCHEMA_HINT}.get(mode, REVIEW_SCHEMA_HINT)
    repaired = run_engine(
        harness, REFORMAT_INSTRUCTION.format(schema=schema_hint, prior=text[-6000:]), cwd
    )
    if repaired is not None:
        result, _ = _parse_engine_output(repaired, harness, key, norm)
        if result is not None:
            return result
    die(f"could not parse a JSON result from {harness} output (even after a reformat retry):\n{text[-2000:]}")


# ── markdown rendering ─────────────────────────────────────────────────────────
def fmt_loc(f):
    path = f["file"]
    ls, le = f.get("line_start"), f.get("line_end")
    if not path:
        return ""
    if isinstance(ls, int) and ls > 0:
        if isinstance(le, int) and le > ls:
            return f"`{path}:L{ls}-L{le}`"
        return f"`{path}:L{ls}`"
    return f"`{path}`"


def render_markdown(review, harnesses, depth, bar, merge_base):
    verdict = review["verdict"]
    findings = review["findings"]
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    out = [f"## 🤖 review-bot — {VERDICT_LABEL[verdict]}", ""]
    if review["summary"]:
        out += [review["summary"], ""]
    if not findings:
        out += [f"No blocking issues found at or above the **{bar}** confidence bar.", ""]
    else:
        out += [f"### Findings ({len(findings)})", ""]
        for f in findings:
            emoji = SEVERITY_EMOJI[f["severity"]]
            loc = fmt_loc(f)
            head = f"#### {emoji} {f['severity']} · {f['confidence']}"
            if loc:
                head += f" · {loc}"
            out += [head, f"**{f['title']}**", ""]
            if f["rationale"]:
                out += [f["rationale"], ""]
            if f["suggestion"]:
                out += ["> **suggestion:** " + f["suggestion"].replace("\n", "\n> "), ""]
    hlabel = ",".join(harnesses)
    out += [
        "---",
        f"*Automated review by **review-bot** · harness `{hlabel}` · depth `{depth}` · "
        f"bar `{bar}` · merge-base `{merge_base[:12]}`. Advisory only — olli merges. "
        f"Re-run with `@review-bot <args>` (e.g. `@review-bot deep with claude,codex`).*",
    ]
    return "\n".join(out)


def render_triage_markdown(triage, harnesses, depth, bar, head_sha):
    disp = triage["disposition"]
    out = [f"## 🤖 review-bot triage — {DISPOSITION_LABEL[disp]}", ""]
    if triage["summary"]:
        out += [triage["summary"], ""]
    if triage["assessment"]:
        out += ["### Assessment", "", triage["assessment"], ""]
    if triage["grounding"]:
        out += [f"**Grounding:** {triage['grounding']}", ""]
    if triage["recommended_action"]:
        out += ["### Recommended next step", "", triage["recommended_action"], ""]
    hlabel = ",".join(harnesses)
    out += [
        "---",
        f"*Automated triage by **review-bot** · harness `{hlabel}` · depth `{depth}` · "
        f"confidence `{triage['confidence']}` · bar `{bar}` · repo tip `{head_sha[:12]}`. "
        f"Advisory only — olli decides. Re-run with `@review-bot <args>`.*",
    ]
    return "\n".join(out)


def render_audit_markdown(audit, repo, harnesses, depth, bar, head_sha, supersedes=None):
    """Render the ranked audit findings as the BODY of a create-issue POST. Findings arrive
    most-severe-first; we preserve that order but group under severity-band headers using the
    existing SEVERITY_ORDER / SEVERITY_EMOJI vocabulary."""
    findings = audit["findings"]
    # Stable-sort by band so ordering within a band is preserved (list.sort is stable).
    findings = sorted(findings, key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    out = [f"## 🤖 review-bot audit — {repo} maintainability findings", ""]
    if audit["summary"]:
        out += [audit["summary"], ""]
    if supersedes:
        out += [f"Supersedes #{supersedes}.", ""]
    if not findings:
        out += [f"No maintainability findings at or above the **{bar}** confidence bar.", ""]
    else:
        out += [f"### Findings ({len(findings)})", ""]
        current_band = None
        for f in findings:
            sev = f["severity"]
            if sev != current_band:
                current_band = sev
                out += [f"### {SEVERITY_EMOJI[sev]} {sev}", ""]
            loc = fmt_loc(f)
            head = f"#### {SEVERITY_EMOJI[sev]} {sev} · {f['confidence']}"
            if loc:
                head += f" · {loc}"
            out += [head, f"**{f['title']}**", ""]
            if f["rationale"]:
                out += [f["rationale"], ""]
            if f["suggestion"]:
                out += ["> **suggestion:** " + f["suggestion"].replace("\n", "\n> "), ""]
    hlabel = ",".join(harnesses)
    out += [
        "---",
        f"*Automated audit by **review-bot** · harness `{hlabel}` · depth `{depth}` · "
        f"bar `{bar}` · repo tip `{head_sha[:12]}`. Advisory only — olli decides which "
        f"findings become fixes. Re-run with `@review-bot audit`.*",
    ]
    return "\n".join(out)


# ── main ───────────────────────────────────────────────────────────────────────
def load_token():
    for cand in TOKEN_FILE_CANDIDATES:
        if cand and os.path.isfile(cand):
            with open(cand) as f:
                tok = f.read().strip()
            if tok:
                return tok
    die(
        "review-bot token not found. Looked in: "
        + ", ".join(c for c in TOKEN_FILE_CANDIDATES if c)
        + ". (Rendered by hosts/convox/forgejo-agent.nix on deploy.)"
    )


# ── shared generate → verify → synthesise pipeline (both modes) ────────────────
def run_pipeline(harnesses, gen_prompt, verify_fill, synth_fill, cdir, depth, mode):
    """Generate per harness → (depth>quick) verify → (multi-harness) synthesise.

    verify_fill(result)->prompt and synth_fill(results)->prompt are mode-specific
    template-fillers; verify_fill is None at depth=quick. Returns the final normalized obj.
    """
    results = []
    for h in harnesses:
        r = review_via(h, gen_prompt, cdir, False, mode)
        if depth != "quick" and r is not None and verify_fill is not None:
            v = review_via(h, verify_fill(r), cdir, False, mode)
            if v is not None:
                r = v
        results.append(r)
    results = [r for r in results if r is not None]
    if not results:
        die("no engine produced a usable result")
    if len(results) == 1:
        return results[0]
    synth = review_via(harnesses[0], synth_fill(results), cdir, False, mode)
    return synth if synth is not None else results[0]


def post_or_print(args, token, markdown, kind):
    """Post (or just print) the final markdown. Returns (markdown, url-or-None) so the
    serve wrapper (serve.py) can relay both over the protocol; the prints keep the
    direct CLI behaviour unchanged."""
    if args.print_only:
        print(markdown)
        return markdown, None
    num = args.pr if args.mode == "pr" else args.issue
    created = api("POST", f"repos/{args.owner}/{args.repo}/issues/{num}/comments", token, data={"body": markdown})
    url = created.get("html_url") or None
    log(f"posted {kind} comment: {url or '(no html_url returned)'}")
    print(url or "(posted; no html_url returned)")
    return markdown, url


AUDIT_TITLE_PREFIX = "review-bot audit:"
AUDIT_LABELS = ["audit", "review-bot"]


def find_existing_audit_issue(owner, repo, token):
    """GET open issues; return the number of a prior audit issue (matched by the title
    prefix or the audit label) so the new body can link it, else None. Best-effort — never
    fails the audit."""
    try:
        issues = api_paged(f"repos/{owner}/{repo}/issues?state=open&type=issues", token)
    except SystemExit:
        raise
    except Exception:
        return None
    for it in issues:
        if it.get("pull_request"):
            continue
        title = (it.get("title") or "").strip()
        labels = {lb.get("name", "") for lb in (it.get("labels") or [])}
        if title.startswith(AUDIT_TITLE_PREFIX) or (set(AUDIT_LABELS) & labels):
            num = it.get("number")
            if isinstance(num, int):
                return num
    return None


def post_or_create_issue(args, token, title, markdown, kind):
    """CREATE an issue (NOT a comment) with the rendered audit body. Returns (markdown, url).
    Honours --print-only (render, don't POST). Tries with the audit labels first; if label
    creation/attachment fails (labels may not exist on the repo), retries WITHOUT labels
    rather than failing the whole audit."""
    if args.print_only:
        print(markdown)
        return markdown, None
    path = f"repos/{args.owner}/{args.repo}/issues"
    try:
        created = api("POST", path, token, data={"title": title, "body": markdown, "labels": AUDIT_LABELS})
    except (SystemExit, Exception):
        # api() dies on HTTP error (e.g. labels don't exist on the repo). die() is
        # sys.exit under the CLI but monkeypatched to raise under serve.py, so catch both
        # — retry label-free rather than failing the whole audit.
        log("create-issue with labels failed; retrying without labels")
        created = api("POST", path, token, data={"title": title, "body": markdown})
    url = created.get("html_url") or None
    log(f"created {kind} issue: {url or '(no html_url returned)'}")
    print(url or "(created; no html_url returned)")
    return markdown, url


def do_pr_review(args, harnesses, bar, focus, token, auth):
    meta = api("GET", f"repos/{args.owner}/{args.repo}/pulls/{args.pr}", token)
    if meta.get("merged"):
        log("note: PR is already merged — reviewing anyway")
    base_ref = meta["base"]["ref"]
    cdir, merge_base = prepare_checkout(args.owner, args.repo, args.pr, base_ref, auth, args.repo_dir or None)

    diff_block = changed_files_block(cdir, merge_base, auth)
    conv = convention_files(cdir)
    conv_str = ", ".join(conv) if conv else "(none found — infer conventions from the surrounding code)"

    gen_prompt = fill(
        REVIEW_PROMPT_FILE,
        {
            "MERGE_BASE": merge_base[:12],
            "DIFF_OR_FILE_LIST": diff_block,
            "CONVENTION_FILES": conv_str,
            "FOCUS": focus,
            "CONFIDENCE_BAR": bar,
        },
    )
    verify_fill = None
    if args.depth != "quick":
        verify_fill = lambda r: fill(  # noqa: E731
            VERIFY_PROMPT_FILE,
            {"MERGE_BASE": merge_base[:12], "REVIEW_JSON": json.dumps(r, indent=2), "CONFIDENCE_BAR": bar},
        )
    synth_fill = lambda rs: fill(  # noqa: E731
        SYNTHESIS_PROMPT_FILE, {"N": str(len(rs)), "REVIEW_JSON_LIST": json.dumps(rs, indent=2)}
    )

    if args.dry_run:
        for h in harnesses:
            run_engine(h, gen_prompt, cdir, dry_run=True)
        if verify_fill:
            run_engine(harnesses[0], verify_fill({"<the>": "<generated review JSON>"}), cdir, dry_run=True)
        if len(harnesses) > 1:
            run_engine(harnesses[0], synth_fill(["<per-harness review JSONs>"]), cdir, dry_run=True)
        log("dry run complete — no engines executed, nothing posted")
        return

    final = run_pipeline(harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "pr")
    markdown = render_markdown(final, harnesses, args.depth, bar, merge_base)
    return post_or_print(args, token, markdown, "review")


def do_issue_triage(args, harnesses, bar, focus, token, auth):
    issue = api("GET", f"repos/{args.owner}/{args.repo}/issues/{args.issue}", token)
    if issue.get("pull_request"):
        die(f"#{args.issue} is a pull request, not an issue — use --mode pr --pr {args.issue}")
    repo_meta = api("GET", f"repos/{args.owner}/{args.repo}", token)
    default_branch = repo_meta.get("default_branch") or "master"
    # Page the whole thread — a single GET caps at page 1, so on a long issue the
    # triggering @mention and later comments would silently never reach the prompt.
    comments = api_paged(f"repos/{args.owner}/{args.repo}/issues/{args.issue}/comments", token)

    cdir, head_sha = prepare_head_checkout(args.owner, args.repo, default_branch, auth, args.repo_dir or None)
    conv = convention_files(cdir)
    conv_str = ", ".join(conv) if conv else "(none found — infer conventions from the surrounding code)"
    issue_block = issue_context_block(issue, comments)

    gen_prompt = fill(
        TRIAGE_PROMPT_FILE,
        {
            "DEFAULT_BRANCH": default_branch,
            "REPO": f"{args.owner}/{args.repo}",
            "ISSUE_BLOCK": issue_block,
            "CONVENTION_FILES": conv_str,
            "FOCUS": focus,
            "CONFIDENCE_BAR": bar,
        },
    )
    verify_fill = None
    if args.depth != "quick":
        verify_fill = lambda r: fill(  # noqa: E731
            TRIAGE_VERIFY_PROMPT_FILE,
            {"DEFAULT_BRANCH": default_branch, "REVIEW_JSON": json.dumps(r, indent=2), "CONFIDENCE_BAR": bar},
        )
    synth_fill = lambda rs: fill(  # noqa: E731
        TRIAGE_SYNTHESIS_PROMPT_FILE, {"N": str(len(rs)), "REVIEW_JSON_LIST": json.dumps(rs, indent=2)}
    )

    if args.dry_run:
        for h in harnesses:
            run_engine(h, gen_prompt, cdir, dry_run=True)
        if verify_fill:
            run_engine(harnesses[0], verify_fill({"<the>": "<generated triage JSON>"}), cdir, dry_run=True)
        if len(harnesses) > 1:
            run_engine(harnesses[0], synth_fill(["<per-harness triage JSONs>"]), cdir, dry_run=True)
        log("dry run complete — no engines executed, nothing posted")
        return

    final = run_pipeline(harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "issue")
    markdown = render_triage_markdown(final, harnesses, args.depth, bar, head_sha)
    return post_or_print(args, token, markdown, "triage")


def do_repo_audit(args, harnesses, bar, focus, token, auth):
    """mode=repo: check out the default-branch tip, run the audit prompt (the engine explores
    the tree itself), and file ONE prioritized issue via create-issue (not a PR comment)."""
    repo_meta = api("GET", f"repos/{args.owner}/{args.repo}", token)
    default_branch = repo_meta.get("default_branch") or "master"

    cdir, head_sha = prepare_head_checkout(args.owner, args.repo, default_branch, auth, args.repo_dir or None)
    conv = convention_files(cdir)
    conv_str = ", ".join(conv) if conv else "(none found — infer conventions from the surrounding code)"

    gen_prompt = fill(
        AUDIT_PROMPT_FILE,
        {
            "DEFAULT_BRANCH": default_branch,
            "REPO": f"{args.owner}/{args.repo}",
            "CONVENTION_FILES": conv_str,
            "FOCUS": focus,
            "CONFIDENCE_BAR": bar,
        },
    )
    verify_fill = None
    if args.depth != "quick":
        verify_fill = lambda r: fill(  # noqa: E731
            AUDIT_VERIFY_PROMPT_FILE,
            {"DEFAULT_BRANCH": default_branch, "REVIEW_JSON": json.dumps(r, indent=2), "CONFIDENCE_BAR": bar},
        )
    synth_fill = lambda rs: fill(  # noqa: E731
        AUDIT_SYNTHESIS_PROMPT_FILE, {"N": str(len(rs)), "REVIEW_JSON_LIST": json.dumps(rs, indent=2)}
    )

    if args.dry_run:
        for h in harnesses:
            run_engine(h, gen_prompt, cdir, dry_run=True)
        if verify_fill:
            run_engine(harnesses[0], verify_fill({"<the>": "<generated audit JSON>"}), cdir, dry_run=True)
        if len(harnesses) > 1:
            run_engine(harnesses[0], synth_fill(["<per-harness audit JSONs>"]), cdir, dry_run=True)
        log("dry run complete — no engines executed, nothing posted")
        return

    final = run_pipeline(harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "repo")
    # Dedup: link (not close) a prior audit issue if one is open. Skipped under --print-only.
    supersedes = None
    if not args.print_only:
        supersedes = find_existing_audit_issue(args.owner, args.repo, token)
    repo_slug = f"{args.owner}/{args.repo}"
    markdown = render_audit_markdown(final, repo_slug, harnesses, args.depth, bar, head_sha, supersedes)
    title = f"{AUDIT_TITLE_PREFIX} {repo_slug} maintainability findings"
    return post_or_create_issue(args, token, title, markdown, "audit")


def main():
    ap = argparse.ArgumentParser(description="Run review-bot on a Forgejo PR or issue.")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mode", default="", choices=["", "pr", "issue", "repo"], help="pr (default) | issue | repo")
    ap.add_argument("--scope", default="", choices=["", "repo"], help="alias: --scope repo maps to --mode repo")
    ap.add_argument("--pr", type=int, help="PR number (mode=pr)")
    ap.add_argument("--issue", type=int, help="issue number (mode=issue)")
    ap.add_argument("--harness", default="claude", help="claude | codex | claude,codex")
    ap.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
    ap.add_argument("--focus", default="", help="advisory, untrusted focus directive")
    ap.add_argument("--confidence-bar", default="", choices=["", "low", "medium", "high"])
    ap.add_argument("--repo-dir", default="", help="use an existing clone instead of the cache")
    ap.add_argument("--dry-run", action="store_true", help="print prompt(s) + command, post nothing")
    ap.add_argument("--print-only", action="store_true", help="run engines but print markdown, don't POST")
    args = ap.parse_args()

    # Resolve mode: --scope repo is an alias for --mode repo; explicit --mode wins; else
    # infer from which target number was given. mode=repo takes NO --pr/--issue number.
    mode = args.mode
    if args.scope == "repo":
        if mode and mode != "repo":
            die("--scope repo conflicts with --mode " + mode)
        mode = "repo"
    if not mode:
        mode = "issue" if (args.issue is not None and args.pr is None) else "pr"
    if mode == "pr" and args.pr is None:
        die("mode=pr requires --pr N")
    if mode == "issue" and args.issue is None:
        die("mode=issue requires --issue N")
    if mode == "repo" and (args.pr is not None or args.issue is not None):
        die("mode=repo takes no --pr/--issue number (it audits the whole repo)")
    args.mode = mode

    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    for h in harnesses:
        if h not in ("claude", "codex"):
            die(f"unknown harness '{h}' (supported: claude, codex)")
    bar = args.confidence_bar or BAR_BY_DEPTH[args.depth]
    focus = args.focus.strip() or "(none provided)"

    token = load_token()
    auth = GitAuth(token)
    try:
        if mode == "issue":
            do_issue_triage(args, harnesses, bar, focus, token, auth)
        elif mode == "repo":
            do_repo_audit(args, harnesses, bar, focus, token, auth)
        else:
            do_pr_review(args, harnesses, bar, focus, token, auth)
    finally:
        auth.cleanup()


if __name__ == "__main__":
    main()

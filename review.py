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
import contextlib
import fcntl
import json
import uuid
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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
# Age past which a leftover per-run worktree is considered stale even if a live process
# happens to share its (possibly reused) owner pid. Default 6h — comfortably above any
# real multi-engine run (ENGINE_TIMEOUT is 30min per engine).
WT_STALE_SECS = int(os.environ.get("REVIEW_BOT_WT_STALE_SECS", "21600"))
DIFF_INLINE_CAP = int(os.environ.get("REVIEW_BOT_DIFF_CAP", "60000"))
# Empty-verdict calibration: a zero-finding result is read against the size of the diff
# it was asked about. On a small change an empty finder is the expected answer for a
# clean PR; issue #21 measured the finder returning 2, 2, 0 findings on byte-identical
# input, so on a substantial change a single empty sample is weak evidence. The tiers
# only reword the disclosure — they can never add findings (a remedy may downgrade a
# green verdict to "unknown", never upgrade it to a finding).
SMALL_DIFF_MAX_FILES = int(os.environ.get("REVIEW_BOT_SMALL_DIFF_MAX_FILES", "5"))
SMALL_DIFF_MAX_LINES = int(os.environ.get("REVIEW_BOT_SMALL_DIFF_MAX_LINES", "200"))
# Pinged in the in-band failure notice, same knob as poll.py's.
OWNER_HANDLE = os.environ.get("REVIEW_BOT_OWNER_HANDLE", "olli")
# Forgejo populates refs/pull/N/head asynchronously after a branch push, so a review
# fired seconds later can fetch the pre-push commit. prepare_checkout re-fetches the
# pull ref with bounded exponential backoff until it matches meta["head"]["sha"];
# default 4 retries at 1s base ≈ 1+2+4+8 ≈ 15s total, comfortably above the observed
# sub-second-to-seconds propagation window. Both are env-overridable so a test can
# set retries to 0 for a deterministic single-shot failure.
HEAD_SYNC_RETRIES = int(os.environ.get("REVIEW_BOT_HEAD_SYNC_RETRIES", "4"))
HEAD_SYNC_BASE_SECS = float(os.environ.get("REVIEW_BOT_HEAD_SYNC_BASE_SECS", "1.0"))

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
    _post_failure_notice(str(msg))
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


# ── in-band failure notice ─────────────────────────────────────────────────────
# Marker for the give-up comment. Must stay in sync with feedback.py's FAIL_MARKER
# (classified "failed") and deliberately shares nothing with render_markdown's
# "Automated review by **review-bot**" footer, so poll.py never counts a notice as a
# review round.
FAIL_MARKER = "review-bot — could not complete"

# Armed by main() when the poller passes --post-failure-notice: everything one comment
# needs to say "this run produced nothing". A module global rather than a parameter
# because die() is called from every layer of the pipeline and the point is to fire on
# ANY abort, not just the ones that remembered to thread a context through. The serve
# path rebinds die(), so this module's die-hook never fires there; serve arms this
# explicitly and delivers it from its own failure handlers instead (see serve.py main()).
FAILURE_NOTICE = None


def arm_failure_notice(owner, repo, num, mode, attempts, token):
    global FAILURE_NOTICE
    FAILURE_NOTICE = {"owner": owner, "repo": repo, "num": num, "mode": mode,
                      "attempts": attempts, "token": token}


def _disarm_failure_notice():
    global FAILURE_NOTICE
    FAILURE_NOTICE = None


def _post_failure_notice(reason):
    """Post the give-up comment, once, best-effort. Until this existed a run that could
    not produce a result posted NOTHING, and callers waiting on a reply — agents poll
    their own PRs for one — blocked on a comment that was never coming. Single attempt,
    no retry (olli's explicit ruling): if this POST fails the notice is lost and the log
    line is the only trace; the alternative was a persistent delivery state machine in
    poll.py. Disarms BEFORE posting so the api() -> die() error path cannot recurse."""
    global FAILURE_NOTICE
    fn, FAILURE_NOTICE = FAILURE_NOTICE, None
    if not fn:
        return
    what, past = ("review", "reviewed") if fn["mode"] == "pr" else ("triage", "triaged")
    # die() appends the offending engine output after the first line; that belongs in
    # the journal, not in a public comment, so the notice carries the headline only.
    lines = [ln for ln in (reason or "").strip().splitlines() if ln.strip()]
    headline = lines[0].strip()[:300] if lines else "(no error detail)"
    body = (
        f"## 🤖 {FAIL_MARKER}\n\n"
        f"@{OWNER_HANDLE} I tried to {what} this {fn['attempts']} time(s) and could not "
        f"produce a usable result, so **nothing here was {past}**. This is not an "
        f"approval and not a clean bill of health — no analysis was performed.\n\n"
        f"Reason: `{headline}`\n\n"
        f"Automatic attempts have stopped. Ask me directly "
        f"(`review-bot-review … --{'pr' if fn['mode'] == 'pr' else 'issue'} {fn['num']}`) to retry."
    )
    try:
        api("POST", f"repos/{fn['owner']}/{fn['repo']}/issues/{fn['num']}/comments",
            fn["token"], data={"body": body})
        log(f"posted failure notice on {fn['owner']}/{fn['repo']}#{fn['num']}")
    except (Exception, SystemExit):
        # api()'s error paths call die(), which raises SystemExit — swallow it too, so
        # the caller exits with the ORIGINAL failure, not the notice's delivery problem.
        log(f"could not post the failure notice on {fn['owner']}/{fn['repo']}#{fn['num']} "
            "— accepted loss (single attempt, no retry)")


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
    """Return a git working dir for owner/repo — an existing --repo-dir or a cache clone.

    The returned cache clone is the SHARED object store + fetch target for a repo; it is
    never itself checked out at a PR head anymore. Per-run isolated worktrees (see
    add_worktree) are carved out of it. When --repo-dir is given we return the caller's
    dir untouched and unmanaged: concurrent --repo-dir runs on the SAME dir race each
    other's working tree, and keeping them safe is the caller's responsibility (this
    escape hatch is for a human debugging against a checkout they control).
    """
    if repo_dir:
        cdir = os.path.abspath(repo_dir)
        if not os.path.isdir(os.path.join(cdir, ".git")):
            die(f"--repo-dir {cdir} is not a git repository")
        return cdir
    os.makedirs(CACHE_ROOT, exist_ok=True)
    cdir = os.path.join(CACHE_ROOT, f"{owner}__{repo}")
    if os.path.isdir(os.path.join(cdir, ".git")):
        return cdir
    # Clone-if-missing under the sibling repo lock, with double-checked locking so two
    # concurrent first-ever runs don't collide on `git clone` into the same dir: the
    # loser re-checks inside the lock and skips. The lock is a SIBLING file (not inside
    # cdir) so it can be held while cdir is still empty — git clone needs an empty target.
    with _shared_store_lock(_repo_lock_path(owner, repo)):
        if not os.path.isdir(os.path.join(cdir, ".git")):
            log(f"cloning {owner}/{repo} into cache {cdir}")
            git(["clone", "--quiet", f"{FORGE_URL}/{owner}/{repo}.git", cdir], cwd=CACHE_ROOT, auth=auth)
    return cdir


# ── per-run worktree isolation (issue #1) ──────────────────────────────────────
# Every run gets its OWN detached worktree carved out of the shared cache clone, so
# two concurrent runs against the same repo never race a single shared working tree.
# The engine explores that private tree (cwd=worktree) for the whole run, unlocked.
def _new_runid():
    """A per-invocation id that is unique even for two runs on the SAME PR: pid plus a
    random uuid suffix. We only use it to name a dir/ref, not as the dir itself."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _repo_lock_path(owner, repo):
    """Sibling lock file for a repo's shared store. It lives BESIDE the clone dir (not
    inside it), because the clone-if-missing guard needs to hold the lock while the
    target dir is still empty — a lock file inside cdir would block `git clone`, which
    requires an empty destination."""
    return os.path.join(CACHE_ROOT, f"{owner}__{repo}.lock")


def _wt_root(cdir):
    return os.path.join(cdir, ".wt")


def _pid_is_alive(pid):
    """True if `pid` names a live process. os.kill(pid, 0) is the portable liveness probe:
    ProcessLookupError => dead; PermissionError => alive but not ours (treat as alive)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # be conservative — an ambiguous error must not delete a live tree


def _worktree_is_stale(name, path, now):
    """A leftover .wt/<name> is stale (safe to reap) only if its OWNER run is gone.

    runid is "{pid}-{uuid}", so the leading int is the owning process's pid:
      - owner pid still alive  -> NOT stale (a concurrent run is using this tree); UNLESS
      - the dir is older than WT_STALE_SECS  -> stale anyway (belt-and-suspenders against
        pid reuse — a truly ancient tree can't be protected forever by a recycled pid).
    An unparseable name has no owner we can check, so it's treated as stale."""
    try:
        age = now - os.path.getmtime(path)
    except OSError:
        age = 0.0
    if age > WT_STALE_SECS:
        return True
    pid_str = name.split("-", 1)[0]
    try:
        pid = int(pid_str)
    except ValueError:
        return True  # can't identify an owner -> assume orphaned
    return not _pid_is_alive(pid)


def _prune_run_refs(cdir, runid, auth):
    """Delete any per-run namespaced refs (refs/review-bot/wt-<runid>/*) belonging to a
    reaped run. On a graceful exit Checkout.__exit__ deletes the run's ref; on a hard kill
    (SIGKILL/OOM/power loss) __exit__ never runs, so without this the sweep would reap the
    dead worktree DIR but leak its ref, pinning the fetched objects against GC forever."""
    prefix = f"refs/review-bot/wt-{runid}/"
    proc = git(["for-each-ref", "--format=%(refname)", prefix], cwd=cdir, auth=auth, check=False)
    for ref in (proc.stdout or "").split():
        with contextlib.suppress(Exception):
            git(["update-ref", "-d", ref], cwd=cdir, auth=auth, check=False)


def sweep_stale_worktrees(cdir, auth):
    """Best-effort crash cleanup: drop ONLY leftover .wt/* dirs whose owning run is gone
    (dead owner pid, or older than WT_STALE_SECS), plus their orphaned per-run refs, then
    let git forget them. Worktrees owned by a still-live concurrent run are LEFT ALONE —
    force-removing them would destroy that run's checkout mid-review. Never fatal — hygiene."""
    import time as _time

    root = _wt_root(cdir)
    if os.path.isdir(root):
        now = _time.time()
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not _worktree_is_stale(name, path, now):
                continue
            try:
                git(["worktree", "remove", "--force", path], cwd=cdir, auth=auth, check=False)
                if os.path.exists(path):
                    shutil.rmtree(path, ignore_errors=True)
                # The dir's name IS the runid; reap its per-run refs too (crash asymmetry).
                _prune_run_refs(cdir, name, auth)
            except Exception:
                pass
    with contextlib.suppress(Exception):
        git(["worktree", "prune"], cwd=cdir, auth=auth, check=False)


@contextlib.contextmanager
def _shared_store_lock(lock_path):
    """Hold an exclusive flock on `lock_path` around ALL writes to a repo's shared store —
    the clone-if-missing guard AND the fetch + worktree-add critical section — so
    concurrent runs serialise on ONE per-repo sibling lock. Released as soon as the
    private worktree exists; the (minutes-long) engine run is NOT held."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def add_worktree(cdir, runid, ref, auth):
    """Create a private detached worktree at cdir/.wt/<runid> checked out at `ref`."""
    root = _wt_root(cdir)
    os.makedirs(root, exist_ok=True)
    wt = os.path.join(root, runid)
    git(["worktree", "add", "--quiet", "--detach", wt, ref], cwd=cdir, auth=auth)
    return wt


def remove_worktree(cdir, wt, auth):
    """Tear down a private worktree (safe to call even if it's already gone)."""
    if not wt:
        return
    with contextlib.suppress(Exception):
        git(["worktree", "remove", "--force", wt], cwd=cdir, auth=auth, check=False)
    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)
    with contextlib.suppress(Exception):
        git(["worktree", "prune"], cwd=cdir, auth=auth, check=False)


class Checkout:
    """A private per-run worktree plus the data the caller needs. Used as a context
    manager so the worktree is ALWAYS removed — on normal return, on die() (sys.exit in
    the CLI path) and on the ReviewFailure exception the serve path rebinds die() to."""

    def __init__(self, cdir, wt, ref):
        self.cdir = cdir  # shared cache clone (object store)
        self.wt = wt  # private worktree — the engine's cwd
        self.ref = ref  # the per-run namespaced ref we fetched into (or None)
        self.auth = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # cdir is None for the unmanaged --repo-dir path: nothing to remove.
        if self.wt and self.cdir and self.wt != self.cdir:
            remove_worktree(self.cdir, self.wt, self.auth)
        if self.ref and self.cdir:
            with contextlib.suppress(Exception):
                git(["update-ref", "-d", self.ref], cwd=self.cdir, auth=self.auth, check=False)
        return False


def _select_merge_base(cdir, base_ref, head_ref, auth, recorded_merge_base=None):
    """Prefer a usable forge-recorded merge base, otherwise compute one live."""
    recorded = (recorded_merge_base or "").strip()
    if not recorded:
        fallback_reason = "forge merge_base absent"
    elif git(
        ["cat-file", "-e", f"{recorded}^{{commit}}"],
        cwd=cdir,
        auth=auth,
        check=False,
    ).returncode != 0:
        fallback_reason = f"forge merge_base {recorded[:12]} unknown locally"
    elif git(
        ["merge-base", "--is-ancestor", recorded, head_ref],
        cwd=cdir,
        auth=auth,
        check=False,
    ).returncode != 0:
        head = git(["rev-parse", head_ref], cwd=cdir, auth=auth).stdout.strip()
        fallback_reason = (
            f"forge merge_base {recorded[:12]} not an ancestor of head {head[:12]}"
        )
    else:
        merge_base = git(
            ["rev-parse", f"{recorded}^{{commit}}"], cwd=cdir, auth=auth
        ).stdout.strip()
        log(f"merge base {merge_base[:12]} (forge-recorded)")
        return merge_base

    merge_base = git(
        ["merge-base", f"refs/remotes/origin/{base_ref}", head_ref],
        cwd=cdir,
        auth=auth,
    ).stdout.strip()
    log(f"merge base {merge_base[:12]} (computed live — {fallback_reason})")
    return merge_base


def prepare_checkout(
    owner,
    repo,
    pr,
    base_ref,
    auth,
    repo_dir=None,
    expected_head=None,
    *,
    recorded_merge_base=None,
):
    """Fetch the PR head + base into the shared cache, carve a PRIVATE detached worktree
    at the PR head, and return (Checkout, merge_base). The returned Checkout MUST be used
    as a context manager (or have __exit__ called) so its worktree is cleaned up.

    When ``expected_head`` is set (the serve/PR path passes ``meta["head"]["sha"]``),
    the fetched ``refs/pull/{pr}/head`` is verified against it: Forgejo populates the
    pull ref asynchronously after a push, so a fetch fired seconds later can land on
    the pre-push commit. On mismatch, re-fetch with bounded exponential backoff; if
    the pull ref never converges, ``die()`` with a distinct message rather than review
    a stale tree. When ``expected_head`` is ``None`` (the ``--repo-dir`` path and any
    other caller — e.g. issue-triage never passes it), the check is skipped entirely.
    """
    cdir = ensure_clone(owner, repo, auth, repo_dir)
    if repo_dir:
        # --repo-dir is the caller's own checkout: keep the legacy in-place behaviour
        # (no private worktree, no cleanup). Concurrency here is the caller's problem.
        git(["fetch", "--quiet", "origin", f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"], cwd=cdir, auth=auth)
        head_ref = f"refs/review-bot/pr-{pr}"
        git(["fetch", "--quiet", "origin", f"+refs/pull/{pr}/head:{head_ref}"], cwd=cdir, auth=auth)
        merge_base = _select_merge_base(
            cdir, base_ref, head_ref, auth, recorded_merge_base
        )
        git(["checkout", "--quiet", "--detach", head_ref], cwd=cdir, auth=auth)
        co = Checkout(None, cdir, None)
        co.auth = auth
        return co, merge_base

    runid = _new_runid()
    pr_ref = f"refs/review-bot/wt-{runid}/pr-{pr}"
    log(f"fetching base {base_ref} + PR #{pr} head (run {runid})")
    sweep_stale_worktrees(cdir, auth)
    with _shared_store_lock(_repo_lock_path(owner, repo)):
        git(["fetch", "--quiet", "origin", f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"], cwd=cdir, auth=auth)
        # Fetch the pull ref, then verify it against the API-reported head. Retries
        # are budget-bounded and *internal*: a persistent mismatch surfaces via die()
        # → ok=false result event, distinct from the #17 client-side busy/exit-75
        # channel. The retry budget covers Forgejo's ref-propagation lag; we do NOT
        # rebuild the merge_base until the head has converged.
        got = None
        for attempt in range(HEAD_SYNC_RETRIES + 1):
            git(["fetch", "--quiet", "origin", f"+refs/pull/{pr}/head:{pr_ref}"], cwd=cdir, auth=auth)
            got = git(["rev-parse", pr_ref], cwd=cdir, auth=auth).stdout.strip()
            if not expected_head or got == expected_head:
                break
            if attempt < HEAD_SYNC_RETRIES:
                backoff = HEAD_SYNC_BASE_SECS * (2 ** attempt)
                log(
                    f"PR #{pr} pull ref at {got[:12]} != forge head {expected_head[:12]} "
                    f"(attempt {attempt + 1}/{HEAD_SYNC_RETRIES + 1}); "
                    f"sleeping {backoff:.1f}s for propagation"
                )
                time.sleep(backoff)
        if expected_head and got != expected_head:
            # die() fires before add_worktree/Checkout, so Checkout.__exit__'s ref
            # cleanup (see 418-420) never runs. Delete the per-run pull ref here to
            # match that path — otherwise each abort leaks a ref that pins its
            # fetched objects against GC, defeating _prune_run_refs.
            with contextlib.suppress(Exception):
                git(["update-ref", "-d", pr_ref], cwd=cdir, auth=auth, check=False)
            die(
                f"PR #{pr} head {got[:12]} != forge head {expected_head[:12]} after "
                f"{HEAD_SYNC_RETRIES + 1} refetches — pull ref lagging the push; retry"
            )
        merge_base = _select_merge_base(
            cdir, base_ref, pr_ref, auth, recorded_merge_base
        )
        wt = add_worktree(cdir, runid, pr_ref, auth)
    co = Checkout(cdir, wt, pr_ref)
    co.auth = auth
    return co, merge_base


class DiffMode:
    """What the finder was actually shown of the diff — measured once, in
    `changed_files_block`, and carried to every consumer (issue #21's invariant).

    Three states, because since issue #34 the cap is packed per file rather than
    applied all-or-nothing:

      `inlined`    — every file's hunks are in the prompt;
      `partial`    — some whole files are in the prompt, the rest are named only;
      `file-list`  — no hunks at all, only `git diff --stat` and an instruction.

    Never re-derive any of these from a diff length downstream: the journal line, the
    footer segment and the empty-verdict calibration must all be the same measurement,
    or the disclosure can drift from the input the engine really got.
    """

    def __init__(self, kind, inlined_files, total_files, inlined_chars, total_chars):
        self.kind = kind
        self.inlined_files = inlined_files
        self.total_files = total_files
        self.inlined_chars = inlined_chars
        self.total_chars = total_chars

    @property
    def fully_inlined(self):
        return self.kind == "inlined"

    @property
    def footer_word(self):
        """The footer's ``diff `…` `` segment. Byte-identical to the pre-#34 wording in
        the two states that existed then, so old and new footers stay comparable."""
        if self.kind == "partial":
            return f"partial {self.inlined_files}/{self.total_files} files"
        return self.kind

    @property
    def journal_phrase(self):
        """The tail of the journal line. tools/finder_ab.py keys off the leading
        `diff <N> chars vs cap <C> — ` prefix only, so this wording is free to grow."""
        if self.kind == "partial":
            return (
                f"{self.inlined_files} of {self.total_files} files inlined, "
                f"{self.inlined_chars} of {self.total_chars} chars"
            )
        if self.kind == "inlined":
            return "inlined"
        return "file-list only"

    def __repr__(self):
        return (
            f"DiffMode({self.kind!r}, {self.inlined_files}/{self.total_files} files, "
            f"{self.inlined_chars}/{self.total_chars} chars)"
        )


def split_diff_by_file(diff):
    """Split a unified diff into whole per-file chunks, in source order.

    Returns [(path, text), …] where the texts concatenate back to `diff`. A file header
    is the only thing that can start a line with `diff --git ` at column 0 — every line
    inside a hunk carries a ` `/`+`/`-` prefix — so splitting there cannot cut a hunk in
    half. Anything before the first header (there is normally nothing) is kept as a
    leading chunk so the round-trip holds.
    """
    chunks = []
    current = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))
    return [(diff_chunk_path(c), c) for c in chunks]


def diff_chunk_path(chunk):
    """Best-effort path for one file chunk, for the not-inlined list.

    Advisory only: `git diff --stat` above the block is the authoritative file list, so a
    path this cannot parse degrades to a `?` entry rather than to a wrong claim.
    """
    for line in chunk.splitlines():
        # `+++ b/path` is absent for a deletion (`+++ /dev/null`), where `--- a/path` is
        # the surviving name; both are absent for a pure mode/rename change.
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/") and "+++ /dev/null" in chunk:
            return line[6:].strip()
        if line.startswith("rename to "):
            return line[len("rename to "):].strip()
    head = chunk.splitlines()[0] if chunk else ""
    if head.startswith("diff --git a/") and " b/" in head:
        return head[len("diff --git a/"):].rsplit(" b/", 1)[0].strip()
    return "?"


def pack_diff_chunks(chunks, cap):
    """Greedy first-fit in source order: return (kept, elided).

    Source order rather than largest- or smallest-first so the inlined hunks read in the
    same order as `--stat` above them, and so the packing is a pure function of the diff
    (no size-dependent reshuffling between two reviews of the same PR). A file that does
    not fit is skipped, not a stop condition — the files after it still get their chance
    at the remaining budget.

    The cap governs diff CONTENT only, exactly as the pre-#34 `len(diff) <= cap`
    comparison did; the `--stat` header and the surrounding prose are not counted.
    """
    kept, elided, budget = [], [], cap
    for path, text in chunks:
        if len(text) <= budget:
            kept.append((path, text))
            budget -= len(text)
        else:
            elided.append((path, text))
    return kept, elided


def changed_files_block(cdir, merge_base, auth):
    """Return (block, mode, stats) — the review prompt's diff input, the `DiffMode`
    saying how much of the diff that block actually carries, and the diff's size
    ({"files": N, "insertions": A, "deletions": D}) for the empty-verdict calibration.

    The mode and the stats are returned rather than re-derived by the caller on purpose
    (issue #21): each measurement exists in exactly one place, so the journal line, the
    review footer and the disclosure can never disagree with the input the engine
    actually saw.

    Over-cap diffs are packed per whole file (issue #34) instead of collapsing to the
    file list entirely: a diff 2% over the cap used to lose 100% of its hunks.
    """
    # The cache clone is checked out detached at the PR head, so HEAD is the head.
    stat = git(["diff", "--stat", f"{merge_base}..HEAD"], cwd=cdir, auth=auth).stdout
    diff = git(["diff", f"{merge_base}..HEAD"], cwd=cdir, auth=auth).stdout
    numstat = git(["diff", "--numstat", f"{merge_base}..HEAD"], cwd=cdir, auth=auth).stdout
    files = insertions = deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # Binary files report "-" for both counts: a changed file with no countable lines.
        insertions += int(parts[0]) if parts[0].isdigit() else 0
        deletions += int(parts[1]) if parts[1].isdigit() else 0
    stats = {"files": files, "insertions": insertions, "deletions": deletions}
    chunks = split_diff_by_file(diff)
    if len(diff) <= DIFF_INLINE_CAP:
        kept, elided = chunks, []
        kind = "inlined"
    else:
        kept, elided = pack_diff_chunks(chunks, DIFF_INLINE_CAP)
        # Nothing fit — a single file bigger than the whole cap. Degrade to the pre-#34
        # file-list-only block rather than emit half of one file's hunks: a truncated
        # diff is worse input than an honest omission, and the engine cannot tell the
        # two apart unless it is told.
        kind = "partial" if kept else "file-list"
    mode = DiffMode(
        kind,
        inlined_files=len(kept),
        total_files=len(chunks),
        inlined_chars=sum(len(t) for _, t in kept),
        total_chars=len(diff),
    )
    # The measurement is logged BEFORE the empty-diff refusal below, not after: a 0-char
    # diff is exactly the case tools/finder_ab.py needs the number for, since `0 <= cap`
    # holds under ANY cap and the inlined/file-list word alone cannot tell "the cap did
    # not take" from "there was nothing to review". Dying first would leave the harness
    # with no measurement at all. Nothing is rendered and no engine runs either way.
    log(f"diff {len(diff)} chars vs cap {DIFF_INLINE_CAP} — {mode.journal_phrase}")
    if not diff:
        die(
            f"empty diff at merge base {merge_base[:12]} — nothing to review; "
            "refusing to render a vacuous pass"
        )
    if mode.fully_inlined:
        return f"{stat}\n```diff\n{diff}\n```", mode, stats
    if kind == "file-list":
        return (
            f"{stat}\n\n(diff is large — only the file list is inlined. The repo is "
            f"checked out at the PR head; run `git diff {merge_base[:12]}..HEAD -- "
            f"<file>` to inspect specific hunks.)",
            mode,
            stats,
        )
    listed = "\n".join(f"- `{path}`" for path, _ in elided)
    return (
        f"{stat}\n\n(diff is large — {mode.inlined_files} of {mode.total_files} files "
        f"are inlined in full below; the other {len(elided)} are listed after them, with "
        f"no hunks. The repo is checked out at the PR head; run "
        f"`git diff {merge_base[:12]}..HEAD -- <file>` to read any of those.)\n"
        f"\n```diff\n{''.join(t for _, t in kept)}\n```\n"
        f"\nNot inlined ({len(elided)} file(s)) — read these from the checkout before "
        f"judging them:\n{listed}",
        mode,
        stats,
    )


# ── issue-triage input (mode=issue) ────────────────────────────────────────────
def prepare_head_checkout(owner, repo, default_branch, auth, repo_dir=None):
    """Check out the tip of the default branch (issue triage reads code, not a diff) into
    a PRIVATE per-run worktree. Returns (Checkout, head_sha); use the Checkout as a
    context manager so its worktree is cleaned up."""
    cdir = ensure_clone(owner, repo, auth, repo_dir)
    if repo_dir:
        git(
            ["fetch", "--quiet", "origin", f"+refs/heads/{default_branch}:refs/remotes/origin/{default_branch}"],
            cwd=cdir,
            auth=auth,
        )
        git(["checkout", "--quiet", "--detach", f"refs/remotes/origin/{default_branch}"], cwd=cdir, auth=auth)
        head = git(["rev-parse", "HEAD"], cwd=cdir, auth=auth).stdout.strip()
        co = Checkout(None, cdir, None)
        co.auth = auth
        return co, head

    runid = _new_runid()
    branch_ref = f"refs/review-bot/wt-{runid}/{default_branch}"
    log(f"fetching {default_branch} tip for triage (run {runid})")
    sweep_stale_worktrees(cdir, auth)
    with _shared_store_lock(_repo_lock_path(owner, repo)):
        git(
            ["fetch", "--quiet", "origin", f"+refs/heads/{default_branch}:{branch_ref}"],
            cwd=cdir,
            auth=auth,
        )
        wt = add_worktree(cdir, runid, branch_ref, auth)
    head = git(["rev-parse", "HEAD"], cwd=wt, auth=auth).stdout.strip()
    co = Checkout(cdir, wt, branch_ref)
    co.auth = auth
    return co, head


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


# ── empty-finder diagnostics (issue #21) ──────────────────────────────────────
# A finder that returns zero drafts skips verification entirely (run_pipeline), so the
# review renders as clean off a stage that never produced anything. The rendered footer's
# `0→0` cannot say WHICH of two very different things happened:
#   1. the engine genuinely answered {"verdict":"approve","findings":[]}, or
#   2. normalize() manufactured that answer from something else — it defaults a missing
#      `verdict` to "comment" and a missing/non-list `findings` to [], so an unrelated
#      JSON fragment scraped out of a prose reply parses as a clean review.
# Those need opposite fixes, so record the evidence when it happens rather than re-running
# the pipeline blind afterwards. Journal only: the posted comment is unchanged.
# Triage has the same hole with a different key: normalize_triage defaults an absent or
# unrecognised `disposition` to "needs-info", so a scraped fragment becomes a confident,
# posted disposition. It has no finder stage, so it needs its own trigger and prefix.
EMPTY_FINDER_DIAG_PREFIX = "empty-finder diagnostic: "
TRIAGE_DIAG_PREFIX = "defaulted-triage diagnostic: "
EMPTY_FINDER_RAW_LIMIT = 4000
_MISSING = object()


def _clip(text, limit=EMPTY_FINDER_RAW_LIMIT):
    """Head+tail excerpt: a degenerate reply can hide its tell at either end."""
    if text is None or len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…[{len(text) - limit} chars elided]…\n{text[-half:]}"


def _describe_parsed(obj, path):
    """What the PRE-normalization object carried — the part normalize() erases."""
    if not isinstance(obj, dict):
        return {"path": path, "type": type(obj).__name__}
    findings = obj.get("findings", _MISSING)
    if findings is _MISSING:
        kind, length = "missing", None
    elif findings is None:
        # Distinguished from the other non-list shapes on purpose: `null` is the one an
        # engine plausibly means as "no findings", and normalize*'s `obj.get("findings")
        # or []` already collapses it to exactly the empty-list case.
        kind, length = "null", None
    elif isinstance(findings, list):
        kind, length = "list", len(findings)
    else:
        kind, length = type(findings).__name__, None
    verdict = obj.get("verdict", _MISSING)
    disposition = obj.get("disposition", _MISSING)
    return {
        "path": path,
        "keys": sorted(str(k) for k in obj),
        "verdict_raw": None if verdict is _MISSING else verdict,
        "verdict_present": verdict is not _MISSING,
        "disposition_raw": None if disposition is _MISSING else disposition,
        "disposition_present": disposition is not _MISSING,
        "findings_kind": kind,
        "findings_len": length,
    }


def _fill_diag(diag, **fields):
    if isinstance(diag, dict):
        diag.update(fields)


def _describe_findings(parse):
    """`findings` as the engine sent it. The length matters and is easy to miss: this line
    is only ever printed when zero drafts survived, so a non-zero length means normalize*
    discarded every entry — say so instead of printing a bare `list`."""
    kind = parse.get("findings_kind", "(none)")
    length = parse.get("findings_len")
    if kind != "list" or not length:
        return kind
    return f"list of {length} — every entry discarded by normalize"


def empty_finder_is_genuine(parse, mode):
    """Did the engine really answer with an empty result, or did normalize() default its
    way there? Either tell is sufficient, and each mode has its own:

    - an explicit empty `findings` list — the universal one; the audit schema has nothing
      else, since it carries no `verdict` by design (AUDIT_SCHEMA_HINT, normalize_audit),
      so demanding one there would report every clean audit as a parse pathology;
    - `"findings": null`, which normalize* collapses to [] identically and which an engine
      plausibly means as "no findings". The other falsy shapes ({} / "" / 0) collapse the
      same way but are malformed rather than answers, so they stay pathologies;
    - a RECOGNISED `verdict` with NO `findings` key at all, in PR mode. Requiring a list as
      well would flag the common shorthand {"verdict":"approve","summary":…} — a real
      approve with the empty array left out — as a pathology, sending a reader after a bug
      that is not there. Checking the verdict's value rather than mere presence is what
      keeps a quoted schema ("approve|comment|request_changes") from passing as an answer.
      `missing` rather than "any non-list" is what holds those malformed falsy shapes out
      of this escape hatch; discarded entries are already caught by the list branch."""
    if not parse:
        return False
    kind = parse.get("findings_kind")
    if kind == "null":
        return True
    if kind == "list":
        # An EXPLICITLY empty list. A list that was non-empty and still reached zero
        # drafts means normalize* discarded every entry (both silently `continue` past a
        # non-dict), which is manufacturing, not answering — the case this exists to catch.
        return parse.get("findings_len") == 0
    return mode != "repo" and kind == "missing" and parse.get("verdict_raw") in VERDICT_LABEL


def log_empty_finder_diagnostic(harness, diag):
    """Journal what the engine actually emitted, human line first then one JSON line."""
    diag = diag or {}
    parse = diag.get("parse") or {}
    mode = diag.get("mode", "pr")
    call = (
        "genuine empty result"
        if empty_finder_is_genuine(parse, mode)
        else "DEFAULTED — not a real result object"
    )
    if mode == "repo":
        verdict_note = "n/a, the audit schema carries none"
    else:
        verdict_note = "present" if parse.get("verdict_present") else "ABSENT — defaulted"
    retry = " after a JSON-repair retry;" if diag.get("repair_retried") else ";"
    log(
        f"EMPTY FINDER ({harness}, {mode} mode): {call}. Zero drafts{retry} "
        f"verify stage skipped. parse-path {parse.get('path', '(unparsed)')}, "
        f"verdict {parse.get('verdict_raw')!r} ({verdict_note}), "
        f"findings {_describe_findings(parse)}, "
        f"top-level keys {','.join(parse.get('keys') or []) or '(none)'}"
    )
    log(EMPTY_FINDER_DIAG_PREFIX + json.dumps(diag, sort_keys=True, default=str))


def triage_disposition_was_defaulted(parse):
    """Did the POSTED disposition come from the engine, or from us? normalize_triage
    substitutes `needs-info` both when `disposition` is absent and when it is present but
    unrecognised, so either case posts a confident disposition nobody produced — the
    triage analogue of an empty finder rendering as a confident green."""
    if not parse:
        return False
    if not parse.get("disposition_present"):
        return True
    return parse.get("disposition_raw") not in DISPOSITIONS


def log_defaulted_triage_diagnostic(harness, diag, stage, posted):
    """Triage has no finder stage and no draft count, so the empty-finder trigger cannot see
    this. Same evidence, same journal-only sink, different trigger.

    `stage` names the call whose output defaulted and `posted` says whether that output is
    what the reader will see — a triage always verifies, and in a multi-harness run
    synthesis has the last word, so claiming "this was posted" from any single call would
    be a false claim of exactly the kind this whole diagnostic exists to prevent."""
    diag = diag or {}
    parse = diag.get("parse") or {}
    raw = parse.get("disposition_raw")
    cause = "supplied none" if not parse.get("disposition_present") else f"supplied {raw!r}"
    retry = " after a JSON-repair retry;" if diag.get("repair_retried") else ";"
    where = (
        "this is the disposition being posted"
        if posted
        else "this harness's contribution to the synthesis stage"
    )
    log(
        f"DEFAULTED TRIAGE ({harness}, {stage} stage): the engine {cause}{retry} "
        f"normalize_triage substituted `needs-info` — {where}. "
        f"parse-path {parse.get('path', '(unparsed)')}, "
        f"top-level keys {','.join(parse.get('keys') or []) or '(none)'}"
    )
    log(TRIAGE_DIAG_PREFIX + json.dumps(dict(diag, stage=stage, posted=posted),
                                        sort_keys=True, default=str))


# A scraped object must carry at least one key its mode's schema defines. Every normalize*
# defaults the fields it does not find — verdict -> "comment", findings -> [], disposition
# -> "needs-info" — so an object with NONE of them is not a partial result, it is a
# different object that happened to be the first balanced {...} in the reply. Accepting it
# manufactures a confident answer out of a fragment. The test is presence-only and never
# judges content: a real result always carries one of these, so no genuine review, audit or
# triage can be rejected by it — including a legitimately empty one.
RESULT_KEYS = {
    "pr": ("verdict", "findings"),
    "repo": ("findings",),
    "issue": ("disposition",),
}


def _parse_engine_output(raw, harness, key, norm, accept=()):
    """Raw engine stdout -> (normalized_obj_or_None, inner_text_for_a_repair_retry,
    parse_facts_or_None). The third element records how the JSON was reached and what it
    carried, for log_empty_finder_diagnostic. A scraped object carrying none of `accept`
    is refused (`rejected: True`) and reported as unparsed, so the caller's repair retry
    handles it exactly like output that never parsed at all."""
    text, path = raw, "raw"
    if harness == "claude":
        # `claude -p --output-format json` wraps the answer in an envelope; the real
        # answer is the `result` string (which should itself be the review JSON).
        try:
            env = json.loads(raw)
            if isinstance(env, dict) and isinstance(env.get("result"), str):
                text, path = env["result"], "envelope-result"
            elif isinstance(env, dict) and key in env:
                return norm(env), text, _describe_parsed(env, "envelope-direct")
        except json.JSONDecodeError:
            text, path = raw, "raw"
    obj = find_json_object(text)
    if obj is None:
        return None, text, None
    parse = _describe_parsed(obj, path)
    if accept and isinstance(obj, dict) and not any(k in obj for k in accept):
        parse["rejected"] = True
        return None, text, parse
    return norm(obj), text, parse


def review_via(harness, prompt, cwd, dry_run, mode="pr", diag=None):
    """`diag`, when a dict, is filled with the evidence log_empty_finder_diagnostic
    needs. It is always filled and only sometimes read: whether this call was the finder,
    and whether it came back empty, is known to the caller and not here."""
    raw = run_engine(harness, prompt, cwd, dry_run=dry_run)
    if dry_run or raw is None:
        return None
    if mode == "issue":
        norm, key = normalize_triage, "disposition"
    elif mode == "repo":
        norm, key = normalize_audit, "findings"
    else:
        norm, key = normalize, "verdict"

    accept = RESULT_KEYS.get(mode, RESULT_KEYS["pr"])
    _fill_diag(diag, harness=harness, mode=mode, raw_chars=len(raw), raw_excerpt=_clip(raw))
    result, text, parse = _parse_engine_output(raw, harness, key, norm, accept)
    if result is not None:
        _fill_diag(diag, repair_retried=False, parse=parse)
        return result

    # One JSON-repair retry. Engines occasionally answer in prose despite the prompt's
    # "output ONLY JSON" (observed deterministically with `claude -p` on single-finding
    # reviews) — which otherwise fails the whole review even though the analysis was
    # sound. Ask the engine to reformat its own prior output as strict JSON before giving
    # up: cheaper and more faithful than discarding the review or re-generating it.
    if parse is not None and parse.get("rejected"):
        # Distinguish the two ways we get here: nothing parsed at all, versus something
        # parsed and was refused. The second used to be posted as a confident result.
        log(
            f"{harness} returned JSON carrying none of {'/'.join(accept)} "
            f"(keys {','.join(parse.get('keys') or []) or '(none)'}) — refusing to "
            f"normalize a fragment into a result; attempting one reformat retry"
        )
        _fill_diag(diag, rejected_parse=parse)
    else:
        log(f"{harness} did not return parseable JSON; attempting one reformat retry")
    _fill_diag(diag, repair_retried=True)
    schema_hint = {"issue": TRIAGE_SCHEMA_HINT, "repo": AUDIT_SCHEMA_HINT}.get(mode, REVIEW_SCHEMA_HINT)
    repaired = run_engine(
        harness, REFORMAT_INSTRUCTION.format(schema=schema_hint, prior=text[-6000:]), cwd
    )
    if repaired is not None:
        _fill_diag(diag, repair_chars=len(repaired), repair_excerpt=_clip(repaired))
        result, _text, parse = _parse_engine_output(repaired, harness, key, norm, accept)
        if result is not None:
            _fill_diag(diag, parse=parse)
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


def provenance_counts(provenance):
    stages = provenance.get("stages", []) if provenance else []
    counts = ", ".join(
        f"{stage['harness']} {stage['draft_count']}→{stage['surviving_count']}"
        for stage in stages
    )
    if counts and provenance.get("synthesized"):
        counts += ", synthesized"
    return counts


def append_clean_review_provenance(out, provenance, diff_stats=None, diff_mode=None):
    stages = provenance.get("stages", []) if provenance else []
    if not stages:
        return
    total_draft = sum(stage["draft_count"] for stage in stages)
    if total_draft == 0:
        if diff_stats:
            n = diff_stats["files"]
            plus, minus = diff_stats["insertions"], diff_stats["deletions"]
            size = f"{n} file{'s' if n != 1 else ''}, +{plus}/-{minus}"
            # Size is only half the question; the other half is how much of that size the
            # finder was shown (issue #34, constraint 5). An empty result on hunks that
            # were never in the prompt is not evidence about them at ANY size, so the
            # input mode is checked before the smallness tiers — a 3-file change reviewed
            # from a file list must not read as "typical and consistent with a clean PR".
            if diff_mode is not None and not diff_mode.fully_inlined:
                out += [
                    f"⚠️ 0 findings on a change the finder did not fully see ({size}, "
                    f"diff `{diff_mode.footer_word}`) — hunks that were never shown "
                    "cannot be evidence of clean code, whatever the size. Treat as not "
                    "fully reviewed; a second pass can be requested with `@review-bot` "
                    "or `review-bot-review`.",
                    "",
                ]
            elif n <= SMALL_DIFF_MAX_FILES and plus + minus <= SMALL_DIFF_MAX_LINES:
                out += [
                    f"0 findings on a small change ({size}) — an empty result is "
                    "typical and consistent with a clean PR. Verification skipped: "
                    "nothing to verify.",
                    "",
                ]
            else:
                out += [
                    f"⚠️ 0 findings on a substantial change ({size}) — empty results "
                    "are weaker evidence at this size. Treat as not fully reviewed; a "
                    "second pass can be requested with `@review-bot` or "
                    "`review-bot-review`.",
                    "",
                ]
        else:
            # No diff measurement available (a direct render outside the PR path).
            out += [
                "⚠️ The finder stage returned no findings, so nothing was verified — "
                "this reports an empty finder, not a verified-clean diff.",
                "",
            ]
    elif sum(stage["surviving_count"] for stage in stages) == 0:
        out += [
            f"All {total_draft} draft finding(s) were checked and dropped by the "
            "verification stage.",
            "",
        ]


def render_markdown(review, harnesses, depth, bar, merge_base, provenance=None, diff_mode=None,
                    diff_stats=None, head_sha=None):
    verdict = review["verdict"]
    findings = review["findings"]
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    out = [f"## 🤖 review-bot — {VERDICT_LABEL[verdict]}", ""]
    if review["summary"]:
        out += [review["summary"], ""]
    if not findings:
        out += [f"No blocking issues found at or above the **{bar}** confidence bar.", ""]
        append_clean_review_provenance(out, provenance, diff_stats, diff_mode)
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
    counts = provenance_counts(provenance)
    findings_segment = f" · findings `{counts}`" if counts else ""
    # diff_mode comes straight from changed_files_block's own packing (issues #21, #34),
    # so the disclosed input mode is the one the finder actually got — three states now,
    # never collapsed to two. None (no caller information, e.g. a direct render in a
    # test) simply omits the segment.
    diff_segment = ""
    if diff_mode is not None:
        diff_segment = f" · diff `{diff_mode.footer_word}`"
    # head_sha stamps the reviewed head into the footer so poll.py can attribute this
    # review to a revision (per-revision round cap, issue #29). None (no caller
    # information, e.g. a direct render in a test) simply omits the segment.
    head_segment = f" · head `{head_sha[:12]}`" if head_sha else ""
    out += [
        "---",
        f"*Automated review by **review-bot** · harness `{hlabel}` · depth `{depth}` · "
        f"bar `{bar}`{findings_segment}{diff_segment} · merge-base `{merge_base[:12]}`{head_segment}. "
        f"Advisory only — olli merges. "
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


def render_audit_markdown(
    audit, repo, harnesses, depth, bar, head_sha, supersedes=None, provenance=None
):
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
    counts = provenance_counts(provenance)
    findings_segment = f" · findings `{counts}`" if counts else ""
    out += [
        "---",
        f"*Automated audit by **review-bot** · harness `{hlabel}` · depth `{depth}` · "
        f"bar `{bar}`{findings_segment} · repo tip `{head_sha[:12]}`. "
        f"Advisory only — olli decides which "
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
    template-fillers; verify_fill is None at depth=quick. Returns the final normalized
    object alongside generator-stage provenance for the review and audit renderers.
    """
    results = []
    stages = []
    for h in harnesses:
        diag = {}
        r = review_via(h, gen_prompt, cdir, False, mode, diag=diag)
        draft_count = None
        stage = "draft"
        if r is not None and mode in ("pr", "repo"):
            draft_count = len(r["findings"])
            if draft_count == 0:
                # Verification is skipped just below for a zero-draft pr/repo finder, so
                # this drafting call is unambiguously the object that gets rendered.
                log_empty_finder_diagnostic(h, diag)
        should_verify = (
            depth != "quick"
            and r is not None
            and verify_fill is not None
            and not (mode in ("pr", "repo") and draft_count == 0)
        )
        if should_verify:
            vdiag = {}
            v = review_via(h, verify_fill(r), cdir, False, mode, diag=vdiag)
            if v is not None:
                # The verify result REPLACES the draft, so from here on its parse is the
                # one that describes this harness's contribution. Triage never skips
                # verification, so watching only the draft would both miss a disposition
                # defaulted here and misreport a draft anomaly that verification repaired.
                r, diag, stage = v, vdiag, "verify"
        if r is not None and mode == "issue" and triage_disposition_was_defaulted(diag.get("parse")):
            log_defaulted_triage_diagnostic(h, diag, stage, posted=len(harnesses) == 1)
        if r is not None and draft_count is not None:
            stages.append(
                {
                    "harness": h,
                    "draft_count": draft_count,
                    "surviving_count": len(r["findings"]),
                }
            )
        results.append(r)
    results = [r for r in results if r is not None]
    if not results:
        die("no engine produced a usable result")
    provenance = {"stages": stages, "synthesized": len(results) > 1}
    if len(results) == 1:
        return results[0], provenance
    synth_diag = {}
    synth = review_via(harnesses[0], synth_fill(results), cdir, False, mode, diag=synth_diag)
    if synth is not None and mode == "issue" and triage_disposition_was_defaulted(synth_diag.get("parse")):
        # Synthesis is the last word in a multi-harness run — this one really is posted.
        log_defaulted_triage_diagnostic(harnesses[0], synth_diag, "synthesis", posted=True)
    return (synth if synth is not None else results[0]), provenance


def post_or_print(args, token, markdown, kind):
    """Post (or just print) the final markdown. Returns (markdown, url-or-None) so the
    serve wrapper (serve.py) can relay both over the protocol; the prints keep the
    direct CLI behaviour unchanged."""
    if args.print_only:
        print(markdown)
        return markdown, None
    num = args.pr if args.mode == "pr" else args.issue
    created = api("POST", f"repos/{args.owner}/{args.repo}/issues/{num}/comments", token, data={"body": markdown})
    # The verdict is on the forge — a die() after this point (worktree cleanup, cache
    # maintenance) must not post a "nothing here was reviewed" notice under it.
    _disarm_failure_notice()
    url = created.get("html_url") or None
    log(f"posted {kind} comment: {url or '(no html_url returned)'}")
    print(url or "(posted; no html_url returned)")
    return markdown, url


AUDIT_TITLE_PREFIX = "review-bot audit:"


def find_existing_audit_issue(owner, repo, token):
    """GET open issues; return the number of a prior audit issue (matched by the title
    prefix) so the new body can link it, else None. review-bot never applies labels, so
    matching is by title only."""
    issues = api_paged(f"repos/{owner}/{repo}/issues?state=open&type=issues", token)
    for it in issues:
        if it.get("pull_request"):
            continue
        title = (it.get("title") or "").strip()
        if title.startswith(AUDIT_TITLE_PREFIX):
            num = it.get("number")
            if isinstance(num, int):
                return num
    return None


def post_or_create_issue(args, token, title, markdown, kind):
    """CREATE an issue (NOT a comment) with the rendered audit body. Returns (markdown, url).
    Honours --print-only (render, don't POST). POSTs {title, body} only — review-bot is
    READ-ONLY and never touches the labels API (and Forgejo's labels field takes label IDs,
    not names, so name-based labels can't work anyway). A genuine POST failure surfaces via
    api()'s die(), like everywhere else."""
    if args.print_only:
        print(markdown)
        return markdown, None
    path = f"repos/{args.owner}/{args.repo}/issues"
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
    # Guard against a malformed API response: a missing/empty head.sha degrades to
    # today's no-check behaviour rather than hard-failing every review.
    expected_head = (meta.get("head") or {}).get("sha") or None
    checkout, merge_base = prepare_checkout(
        args.owner, args.repo, args.pr, base_ref, auth, args.repo_dir or None,
        expected_head=expected_head,
        recorded_merge_base=meta.get("merge_base"),
    )
    with checkout:
        cdir = checkout.wt  # the private per-run worktree — the engine's cwd
        diff_block, diff_mode, diff_stats = changed_files_block(cdir, merge_base, auth)
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

        final, provenance = run_pipeline(
            harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "pr"
        )
        markdown = render_markdown(
            final, harnesses, args.depth, bar, merge_base, provenance=provenance,
            diff_mode=diff_mode, diff_stats=diff_stats, head_sha=expected_head,
        )
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

    checkout, head_sha = prepare_head_checkout(args.owner, args.repo, default_branch, auth, args.repo_dir or None)
    with checkout:
        cdir = checkout.wt  # the private per-run worktree — the engine's cwd
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

        final, _provenance = run_pipeline(
            harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "issue"
        )
        markdown = render_triage_markdown(final, harnesses, args.depth, bar, head_sha)
        return post_or_print(args, token, markdown, "triage")


def do_repo_audit(args, harnesses, bar, focus, token, auth):
    """mode=repo: check out the default-branch tip, run the audit prompt (the engine explores
    the tree itself), and file ONE prioritized issue via create-issue (not a PR comment)."""
    repo_meta = api("GET", f"repos/{args.owner}/{args.repo}", token)
    default_branch = repo_meta.get("default_branch") or "master"

    checkout, head_sha = prepare_head_checkout(args.owner, args.repo, default_branch, auth, args.repo_dir or None)
    with checkout:
        cdir = checkout.wt  # the private per-run worktree — the engine's cwd
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

        final, provenance = run_pipeline(
            harnesses, gen_prompt, verify_fill, synth_fill, cdir, args.depth, "repo"
        )
        # Dedup: link (not close) a prior audit issue if one is open. Skipped under --print-only.
        supersedes = None
        if not args.print_only:
            supersedes = find_existing_audit_issue(args.owner, args.repo, token)
        repo_slug = f"{args.owner}/{args.repo}"
        markdown = render_audit_markdown(
            final,
            repo_slug,
            harnesses,
            args.depth,
            bar,
            head_sha,
            supersedes,
            provenance=provenance,
        )
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
    ap.add_argument(
        "--post-failure-notice", type=int, default=0, metavar="ATTEMPTS",
        help="if this run aborts, post one in-band give-up comment on the target "
             "(ATTEMPTS = total automatic attempts including this one, shown in the "
             "notice). Passed by review-bot-poll on a trigger's final attempt; direct "
             "CLI use, --print-only and --dry-run stay silent.",
    )
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
    # Arm only for a poller-invoked run that will actually POST: a --print-only or
    # --dry-run consult is private, and mode=repo has no thread to notify (the poller
    # never triggers audits). Everything before this point dies silently — those are
    # argument errors, and there may not even be a valid target to post to.
    if args.post_failure_notice > 0 and mode in ("pr", "issue") \
            and not (args.print_only or args.dry_run):
        arm_failure_notice(args.owner, args.repo,
                           args.pr if mode == "pr" else args.issue,
                           mode, args.post_failure_notice, token)
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

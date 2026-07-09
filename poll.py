#!@PYTHON@
"""review-bot-poll — the scheduled @review-bot mention trigger (subtask #3, path 1).

Runs on a timer (systemd user timer for `agent`). For each watched repo it scans
open PRs AND open issues for trigger conditions the bot hasn't answered yet, then calls
the reusable `review-bot-review` routine:

  - an `@review-bot <natural-language args>` COMMENT (the primary trigger — on PRs it runs
    a review, on issues a triage `--mode issue`), and
  - optionally a `needs-review` / `deep-review` LABEL (PRs only).

Issues carry no head-SHA / push and no review↔fix loop, so an issue triage fires ONLY on a
new, undeduped @mention (dedup by comment id) — no label triggers, no round cap.

It parses the NL args into {harness, depth, bar, focus} deterministically (so focus
text can't smuggle harness/depth/bar overrides), dedups so it never loops on the same mention
or head SHA, caps review↔fix rounds (then parks + pings olli), and never reviews on a
plain push. review-bot stays read-only — it only ever comments; olli merges.

Design: notes/decisions/forgejo-dev-workflow.md (review-bot), forgejo-multi-identity.md.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

FORGE_URL = os.environ.get("FORGEJO_URL", "http://10.0.150.1:3000").rstrip("/")
REVIEW_BIN = os.environ.get("REVIEW_BOT_REVIEW_BIN", "review-bot-review")
TOKEN_FILE_CANDIDATES = [
    os.environ.get("REVIEW_BOT_TOKEN_FILE", ""),
    "/home/agent/.config/review-bot/token",
    os.path.expanduser("~/.config/review-bot/token"),
]
STATE_DIR = os.environ.get(
    "REVIEW_BOT_STATE",
    os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "review-bot"),
)
STATE_FILE = os.path.join(STATE_DIR, "answered.json")
LOCK_FILE = os.path.join(STATE_DIR, "poll.lock")

# Watched repos. Default: auto-discover EVERY repo the review-bot token can read
# (all public repos + any private repo it's a collaborator on) — no allowlist, so a
# new public repo is covered with no rebuild. REVIEW_BOT_REPOS (space/comma list) or
# --repo override the discovery with a fixed set, for testing/scoping.
REPOS = [r for r in re.split(r"[\s,]+", os.environ.get("REVIEW_BOT_REPOS", "")) if r]

# Extra handle spellings to honour beyond the bot's real login (olli sometimes writes
# the underscore form). The bot's actual login is auto-resolved from the token.
HANDLE_ALIASES = [h for h in re.split(r"[\s,]+", os.environ.get("REVIEW_BOT_HANDLES", "review-bot review_bot")) if h]
OWNER_HANDLE = os.environ.get("REVIEW_BOT_OWNER_HANDLE", "olli")

DEFAULT_DEPTH = os.environ.get("REVIEW_BOT_DEFAULT_DEPTH", "standard")
DEFAULT_HARNESS = os.environ.get("REVIEW_BOT_DEFAULT_HARNESS", "claude")
MAX_ROUNDS = int(os.environ.get("REVIEW_BOT_MAX_ROUNDS", "3"))
MAX_PER_RUN = int(os.environ.get("REVIEW_BOT_MAX_PER_RUN", "3"))
MAX_FAILS = int(os.environ.get("REVIEW_BOT_MAX_FAILS", "3"))

# Footer substring present in EVERY real review (not in a parked notice) — used to count
# review rounds and to recognise our own reviews. Must match render_markdown in review.py.
REVIEW_MARKER = "Automated review by **review-bot**"
PARK_MARKER = "review-bot — parked"


def log(msg):
    print(f"review-bot-poll: {msg}", file=sys.stderr)


def load_token():
    for c in TOKEN_FILE_CANDIDATES:
        if c and os.path.isfile(c):
            t = open(c).read().strip()
            if t:
                return t
    log("error: review-bot token not found (rendered by forgejo-agent.nix on deploy)")
    sys.exit(1)


def api(method, path, token, data=None):
    url = f"{FORGE_URL}/api/v1/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def api_paged(path, token):
    """GET all pages of a list endpoint."""
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


def discover_repos(token):
    """Every repo the token can read (public + collaborator), minus archived ones.

    Uses /repos/search, whose paginated response is {"ok":..,"data":[...]} (NOT a bare
    array), so it needs its own pager. Skips archived repos (no point reviewing them).
    """
    repos, page = [], 1
    while page <= 40:  # guard: 40*50 = 2000 repos max
        try:
            resp = api("GET", f"repos/search?limit=50&page={page}", token)
        except urllib.error.HTTPError as e:
            log(f"repo discovery failed (HTTP {e.code}); set REVIEW_BOT_REPOS to scope manually")
            break
        except urllib.error.URLError as e:
            log(f"cannot reach {FORGE_URL} ({e.reason}) — skipping this tick")
            break
        data = resp.get("data") or []
        if not data:
            break
        for r in data:
            if r.get("archived"):
                continue
            full = r.get("full_name") or ""
            if full:
                repos.append(full)
        if len(data) < 50:
            break
        page += 1
    return sorted(set(repos))


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"answered": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# Confidence-bar phrasings: "high bar", "low confidence bar", "high-bar",
# "bar high", "bar = medium". Compiled once; reused to parse AND to scrub the phrase
# out of the focus steer. Empty match ⇒ review-bot-review applies its per-depth default.
BAR_RE = re.compile(
    r"\b(low|medium|high)[\s-]+(?:confidence[\s-]+)?bar\b"
    r"|\bbar\b[\s:=-]*\b(low|medium|high)\b",
    re.IGNORECASE,
)


# ── NL parse: mention text -> {harness, depth, bar, focus} (deterministic) ───────
def parse_args_text(arg):
    """arg = the text following the @mention. Keyword-scan; never lets focus override."""
    low = arg.lower()
    m = re.search(r"\b(quick|standard|deep)\b", low)
    depth = m.group(1) if m else DEFAULT_DEPTH
    engines = []
    if re.search(r"\b(both|all)\b", low) or ("claude" in low and "codex" in low):
        engines = ["claude", "codex"]
    elif "codex" in low:
        engines = ["codex"]
    elif "claude" in low:
        engines = ["claude"]
    harness = ",".join(engines) if engines else DEFAULT_HARNESS
    # Confidence bar — scanned deterministically like depth/harness, so a focus steer
    # can never move the bar. Empty ⇒ review-bot-review uses its per-depth default.
    bm = BAR_RE.search(arg)
    bar = (bm.group(1) or bm.group(2)) if bm else ""
    # Focus = the natural-language steer. Two cases:
    #   - explicit "focus …" clause wins (e.g. "deep claude, focus on the netns
    #     teardown" -> "focus on the netns teardown");
    #   - otherwise the whole arg, minus a LEADING run of pure command words, is the
    #     steer — so "does X look OK to you?" passes through intact while a bare
    #     "deep review with claude" yields no focus.
    # harness/depth/bar are already extracted above, so focus can never override them.
    fm = re.search(r"\bfocus\b.*", arg, re.IGNORECASE | re.DOTALL)
    if fm:
        raw_focus = fm.group(0)
    else:
        # Scrub a recognised bar phrase first so it doesn't linger in the steer, then
        # trim a LEADING run of pure command words.
        raw_focus = re.sub(
            r"^(?:[\s,:;.\-]|\b(?:quick|standard|deep|claude|codex|both|all|review|reviews|reviewing|reviewed|using|use|with|and|please|pls|look)\b)+",
            "",
            BAR_RE.sub(" ", arg),
            flags=re.IGNORECASE,
        )
    focus = re.sub(r"\s+", " ", raw_focus).strip()[:500]
    return harness, depth, bar, focus


def mention_arg(body, handles):
    """Return the text after the first @handle mention, or None if no mention."""
    pat = re.compile(r"(?<![\w/@])@(" + "|".join(re.escape(h) for h in handles) + r")\b", re.IGNORECASE)
    m = pat.search(body)
    if not m:
        return None
    rest = body[m.end():]
    # Stop at the end of the mention's line — keeps a multi-paragraph comment from
    # dragging unrelated prose into the focus directive.
    return rest.splitlines()[0].strip() if rest.strip() else ""


def run_review(owner, repo, num, harness, depth, bar, focus, mode="pr"):
    target = "--pr" if mode == "pr" else "--issue"
    verb = "reviewing" if mode == "pr" else "triaging"
    cmd = [REVIEW_BIN, "--owner", owner, "--repo", repo, "--mode", mode, target, str(num),
           "--harness", harness, "--depth", depth]
    if bar:
        cmd += ["--confidence-bar", bar]
    if focus:
        cmd += ["--focus", focus]
    log(f"{verb} {owner}/{repo}#{num} (harness={harness} depth={depth} bar={bar or 'default'} focus={focus!r})")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"review-bot-review failed (rc={proc.returncode}): {proc.stderr.strip()[-500:]}")
        return False
    log(f"{mode} done {owner}/{repo}#{num}: {proc.stdout.strip()}")
    return True


def post_parked(owner, repo, pr, rounds, token):
    body = (
        f"## 🤖 {PARK_MARKER}\n\n"
        f"@{OWNER_HANDLE} I've posted {rounds} automated reviews on this PR — parking "
        f"further automatic reviews to avoid a review↔fix loop. Merge when ready, or ask "
        f"me directly (`review-bot-review … --pr {pr}`) for another pass."
    )
    api("POST", f"repos/{owner}/{repo}/issues/{pr}/comments", token, data={"body": body})
    log(f"parked {owner}/{repo}#{pr} after {rounds} rounds (pinged @{OWNER_HANDLE})")


def main():
    ap = argparse.ArgumentParser(description="Poll Forgejo PRs for @review-bot triggers.")
    ap.add_argument("--repo", action="append", default=[], help="owner/repo (repeatable); overrides REVIEW_BOT_REPOS")
    ap.add_argument("--dry-run", action="store_true", help="scan + parse + print intended actions; review/post/state-write nothing")
    args = ap.parse_args()
    dry = args.dry_run
    explicit_repos = args.repo or REPOS  # fixed list overrides auto-discovery

    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("a previous poll run is still active — skipping this tick")
        return

    token = load_token()
    # We deliberately do NOT call GET /user: the review-bot token is minimal
    # (issue + repository:read, NO read:user), so /user 403s. The bot's own login is
    # one of the configured handles, so HANDLE_ALIASES doubles as the mention-match
    # set AND the self-author skip set — no user-scope needed.
    handles = list(dict.fromkeys(h for h in HANDLE_ALIASES if h))
    if not handles:
        log("no handles configured (REVIEW_BOT_HANDLES) — nothing to match")
        return
    self_authors = {h.lower() for h in handles}

    repos = explicit_repos if explicit_repos else discover_repos(token)
    if not repos:
        log("no repos to scan (discovery returned nothing and none configured) — done")
        return
    src = "configured" if explicit_repos else "discovered"
    log(f"polling (handles {handles}); {len(repos)} repo(s) {src}: {', '.join(repos)}")

    state = load_state()
    answered = state["answered"]

    def is_done(key):
        st = answered.get(key, {}).get("status")
        return st in ("done", "given-up", "parked")

    reviews_done = 0
    for slug in repos:
        if "/" not in slug:
            log(f"skip malformed repo '{slug}'")
            continue
        owner, repo = slug.split("/", 1)
        try:
            pulls = api_paged(f"repos/{owner}/{repo}/pulls?state=open", token)
        except urllib.error.HTTPError as e:
            # Only skip the PR scan — the issue scan below is independent (e.g. a repo
            # with the Pull Requests unit disabled still has triageable issues).
            log(f"skip {slug} PRs: cannot list (HTTP {e.code}) — check review-bot has Read access")
            pulls = []

        for pr in pulls:
            num = pr["number"]
            head_sha = pr.get("head", {}).get("sha", "")
            labels = [lb.get("name", "") for lb in pr.get("labels", [])]

            # Build the candidate triggers for this PR, highest priority first.
            triggers = []  # (key, harness, depth, bar, focus)
            try:
                comments = api_paged(f"repos/{owner}/{repo}/issues/{num}/comments", token)
            except urllib.error.HTTPError as e:
                log(f"skip {slug}#{num}: cannot read comments (HTTP {e.code})")
                continue
            review_rounds = 0
            for c in comments:
                author = c.get("user", {}).get("login", "")
                body = c.get("body", "") or ""
                if author.lower() in self_authors:
                    # Our own comment — never react to it (the review footer literally
                    # contains "@review-bot …" examples). Count real reviews for the cap.
                    if REVIEW_MARKER in body:
                        review_rounds += 1
                    continue
                arg = mention_arg(body, handles)
                if arg is None:
                    continue
                key = f"m:{slug}#{num}:c{c['id']}"
                if is_done(key):
                    continue
                harness, depth, bar, focus = parse_args_text(arg)
                triggers.append((key, harness, depth, bar, focus))
            # Label triggers (deduped per head SHA, so only a new push re-fires).
            for lbl, depth in (("deep-review", "deep"), ("needs-review", DEFAULT_DEPTH)):
                if lbl in labels:
                    key = f"l:{slug}#{num}:{lbl}:{head_sha}"
                    if not is_done(key):
                        triggers.append((key, DEFAULT_HARNESS, depth, "", ""))

            if not triggers:
                continue

            # Cap: park (once per head SHA) instead of reviewing past MAX_ROUNDS.
            if review_rounds >= MAX_ROUNDS:
                pkey = f"p:{slug}#{num}:{head_sha}"
                if not is_done(pkey):
                    if dry:
                        log(f"[dry-run] would park {slug}#{num} ({review_rounds} rounds) + ping @{OWNER_HANDLE}")
                    else:
                        try:
                            post_parked(owner, repo, num, review_rounds, token)
                            answered[pkey] = {"status": "parked", "rounds": review_rounds}
                            save_state(state)
                        except urllib.error.HTTPError as e:
                            log(f"could not post parked notice on {slug}#{num} (HTTP {e.code})")
                continue

            if reviews_done >= MAX_PER_RUN:
                log(f"hit MAX_PER_RUN={MAX_PER_RUN}; deferring {len(triggers)} trigger(s) on {slug}#{num} to next tick")
                continue

            # One review per PR per tick: take the first (highest-priority) trigger.
            key, harness, depth, bar, focus = triggers[0]
            if dry:
                log(f"[dry-run] would review {slug}#{num}: harness={harness} depth={depth} bar={bar or 'default'} focus={focus!r} (trigger {key})")
                reviews_done += 1
                continue
            ok = run_review(owner, repo, num, harness, depth, bar, focus)
            if ok:
                answered[key] = {"status": "done", "sha": head_sha}
                reviews_done += 1
            else:
                rec = answered.get(key, {"status": "failing", "fails": 0})
                rec["fails"] = rec.get("fails", 0) + 1
                rec["status"] = "given-up" if rec["fails"] >= MAX_FAILS else "failing"
                answered[key] = rec
                if rec["status"] == "given-up":
                    log(f"giving up on {key} after {rec['fails']} failures")
            save_state(state)

        # ── open ISSUES: @review-bot mentions → triage (mode=issue) ────────────
        # Unlike PRs, issues have no head-SHA / push and no review↔fix loop, so a
        # triage fires ONLY on a new, undeduped @mention (dedup by comment id): no
        # label triggers, no round cap. review-bot never auto-replies to feedback on
        # its own triage — a re-triage needs a fresh @mention.
        try:
            issues = api_paged(f"repos/{owner}/{repo}/issues?state=open&type=issues", token)
        except urllib.error.HTTPError as e:
            log(f"skip {slug} issues: cannot list (HTTP {e.code})")
            issues = []
        for iss in issues:
            if iss.get("pull_request"):
                continue  # defensive: &type=issues should already exclude PRs
            num = iss["number"]
            try:
                icomments = api_paged(f"repos/{owner}/{repo}/issues/{num}/comments", token)
            except urllib.error.HTTPError as e:
                log(f"skip {slug}#{num} (issue): cannot read comments (HTTP {e.code})")
                continue
            # First un-answered @mention on this issue wins (one triage per issue/tick).
            trigger = None
            for c in icomments:
                author = c.get("user", {}).get("login", "")
                if author.lower() in self_authors:
                    continue  # never react to our own triage (its footer shows @review-bot)
                arg = mention_arg(c.get("body", "") or "", handles)
                if arg is None:
                    continue
                key = f"mi:{slug}#{num}:c{c['id']}"
                if is_done(key):
                    continue
                harness, depth, bar, focus = parse_args_text(arg)
                trigger = (key, harness, depth, bar, focus)
                break
            if trigger is None:
                continue
            if reviews_done >= MAX_PER_RUN:
                log(f"hit MAX_PER_RUN={MAX_PER_RUN}; deferring issue triage on {slug}#{num} to next tick")
                continue
            key, harness, depth, bar, focus = trigger
            if dry:
                log(f"[dry-run] would triage {slug}#{num}: harness={harness} depth={depth} bar={bar or 'default'} focus={focus!r} (trigger {key})")
                reviews_done += 1
                continue
            ok = run_review(owner, repo, num, harness, depth, bar, focus, mode="issue")
            if ok:
                answered[key] = {"status": "done"}
                reviews_done += 1
            else:
                rec = answered.get(key, {"status": "failing", "fails": 0})
                rec["fails"] = rec.get("fails", 0) + 1
                rec["status"] = "given-up" if rec["fails"] >= MAX_FAILS else "failing"
                answered[key] = rec
                if rec["status"] == "given-up":
                    log(f"giving up on {key} after {rec['fails']} failures")
            save_state(state)

    log(f"done — {reviews_done} review(s) this tick")


if __name__ == "__main__":
    main()

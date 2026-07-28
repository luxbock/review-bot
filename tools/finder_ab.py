#!/usr/bin/env python3
"""finder_ab.py — operator instrument for the finder-stage A/B experiment (issue #21).

Question it answers: does inlining the full diff into the finder prompt produce more
(or better-grounded) draft findings than handing the engine only the file list and
letting it read the tree itself?

It runs a full factorial over one PR:

    harness ∈ {claude, codex} × input mode ∈ {forced-inline, forced-elide} × --runs

at a FIXED --depth and --confidence-bar, forcing the input mode purely through
`REVIEW_BOT_DIFF_CAP` — the same knob production uses — rather than a private code
path, so each cell exercises the shipped pipeline:

    forced-inline  REVIEW_BOT_DIFF_CAP=100000000   (no real diff reaches this)
    forced-elide   REVIEW_BOT_DIFF_CAP=1           (every real diff is elided)

Two deliberate properties:

  * It NEVER posts. Every invocation carries `--print-only`; the rendered markdown is
    read off stdout and parsed, never sent to the forge.
  * It invokes `review-bot-review-local` (the in-process implementation), NOT the
    `review-bot-review` socket client. The client cannot carry `REVIEW_BOT_DIFF_CAP`:
    serve.py whitelists request fields and honours engine/env settings only from the
    SERVICE's own environment, so a cap sent by a client is either rejected or ignored.
    Running local means the caller needs the forge token and live engine credentials —
    that is the price of steering the cap.

This is an operator tool, deliberately NOT packaged by default.nix. Run it from a
checkout:

    python3 tools/finder_ab.py --owner olli --repo review-bot --pr 23 --runs 5

By default it builds the package with nix-build and uses
`$out/bin/review-bot-review-local`; `--binary PATH` overrides that (this is also how
the tests drive it against a stub).

Output: one JSON object per run appended to --out (default finder-ab.jsonl), plus a
summary table on stdout with one row per (harness × input mode) cell. A run whose
review aborts is recorded WITH its exit status and counted as aborted — never dropped,
since silently discarding the failures is exactly how an A/B result gets flattered.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import NoReturn

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORGE_URL = os.environ.get("FORGEJO_URL", "http://10.0.150.1:3000").rstrip("/")
TOKEN_FILE_CANDIDATES = [
    os.environ.get("REVIEW_BOT_TOKEN_FILE", ""),
    "/home/agent/.config/review-bot/token",
    os.path.expanduser("~/.config/review-bot/token"),
]

HARNESSES = ("claude", "codex")
# The two forced input modes, as (label, REVIEW_BOT_DIFF_CAP value).
INPUT_MODES = (("forced-inline", 100000000), ("forced-elide", 1))

NIX_EXPR = "with import <nixpkgs> {}; callPackage ./default.nix {}"

# Footer segments rendered by review.py's render_markdown.
VERDICT_RE = re.compile(r"^## 🤖 review-bot — (.+)$", re.MULTILINE)
FINDINGS_RE = re.compile(r"· findings `([^`]*)`")
DIFF_MODE_RE = re.compile(r"· diff `([^`]*)`")
STAGE_RE = re.compile(r"^(\S+) (\d+)→(\d+)$")


def die(msg, code=1) -> NoReturn:
    print(f"finder-ab: error: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg):
    print(f"finder-ab: {msg}", file=sys.stderr)


# ── forge READ (head-SHA pinning only; never a write) ──────────────────────────
def load_token():
    """FORGEJO_TOKEN env -> REVIEW_BOT_TOKEN_FILE / standard candidates -> error."""
    env_tok = os.environ.get("FORGEJO_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for cand in TOKEN_FILE_CANDIDATES:
        if cand and os.path.isfile(cand):
            with open(cand) as f:
                tok = f.read().strip()
            if tok:
                return tok
    die(
        "no forge token found. Set FORGEJO_TOKEN, or point REVIEW_BOT_TOKEN_FILE at a "
        "token file. Any token that can READ the repo works."
    )


def pr_head_sha(owner, repo, pr, token):
    """GET the PR's current head SHA. Read-only; used solely to pin the experiment."""
    url = f"{FORGE_URL}/api/v1/repos/{owner}/{repo}/pulls/{pr}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"token {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            meta = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        die(f"GET pulls/{pr} -> HTTP {e.code}\n{e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        die(f"GET pulls/{pr} -> {e.reason} (is {FORGE_URL} reachable from here?)")
    sha = (meta.get("head") or {}).get("sha") or ""
    if not sha:
        die(f"PR #{pr} returned no head.sha — cannot pin the experiment to one head")
    return sha


# ── the binary under test ──────────────────────────────────────────────────────
def build_binary():
    """nix-build the package and return $out/bin/review-bot-review-local."""
    log("building the package (nix-build) …")
    proc = subprocess.run(
        ["nix-build", "--no-out-link", "--expr", NIX_EXPR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        die(f"nix-build failed (rc={proc.returncode}):\n{proc.stderr}")
    out = proc.stdout.strip().splitlines()[-1].strip()
    binary = os.path.join(out, "bin", "review-bot-review-local")
    if not os.path.exists(binary):
        die(f"built {out} but {binary} is missing")
    log(f"using {binary}")
    return binary


def build_argv(binary, owner, repo, pr, harness, depth, bar):
    """The argv for one run. --print-only is non-negotiable: this tool never posts."""
    return [
        binary,
        "--owner", owner,
        "--repo", repo,
        "--mode", "pr",
        "--pr", str(pr),
        "--harness", harness,
        "--depth", depth,
        "--confidence-bar", bar,
        "--print-only",
    ]


# ── parsing the rendered review ────────────────────────────────────────────────
def parse_stage_counts(counts):
    """'claude 3→1, codex 2→0, synthesized' -> ([{harness,draft,surviving}], synthesized)."""
    stages, synthesized = [], False
    for part in (p.strip() for p in counts.split(",")):
        if part == "synthesized":
            synthesized = True
            continue
        m = STAGE_RE.match(part)
        if m:
            stages.append(
                {"harness": m.group(1), "draft": int(m.group(2)), "surviving": int(m.group(3))}
            )
    return stages, synthesized


def parse_markdown(markdown):
    """Pull the machine-readable bits out of a rendered PR review."""
    verdict = None
    m = VERDICT_RE.search(markdown or "")
    if m:
        verdict = m.group(1).strip().rstrip(".")
    diff_mode = None
    m = DIFF_MODE_RE.search(markdown or "")
    if m:
        diff_mode = m.group(1)
    stages, synthesized, draft, surviving = [], False, None, None
    m = FINDINGS_RE.search(markdown or "")
    if m:
        stages, synthesized = parse_stage_counts(m.group(1))
        if stages:
            draft = sum(s["draft"] for s in stages)
            surviving = sum(s["surviving"] for s in stages)
    return {
        "verdict": verdict,
        "diff_mode": diff_mode,
        "stages": stages,
        "synthesized": synthesized,
        "draft_findings": draft,
        "surviving_findings": surviving,
    }


# ── one run ────────────────────────────────────────────────────────────────────
def run_once(binary, args, harness, mode_label, cap, run_index, head):
    argv = build_argv(binary, args.owner, args.repo, args.pr, harness, args.depth, args.confidence_bar)
    assert "--print-only" in argv, "refusing to run without --print-only"
    env = dict(os.environ)
    env["REVIEW_BOT_DIFF_CAP"] = str(cap)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    parsed = parse_markdown(proc.stdout)
    record = {
        "harness": harness,
        "input_mode": mode_label,
        "diff_cap": cap,
        "run": run_index,
        "status": proc.returncode,
        "head": head,
        "depth": args.depth,
        "confidence_bar": args.confidence_bar,
        "draft_findings": parsed["draft_findings"],
        "surviving_findings": parsed["surviving_findings"],
        "stages": parsed["stages"],
        "synthesized": parsed["synthesized"],
        "diff_mode": parsed["diff_mode"],
        "verdict": parsed["verdict"],
    }
    # An aborted run keeps its status AND its stderr tail, so a cell that looks thin in
    # the summary can be explained rather than guessed at.
    if proc.returncode != 0 or parsed["draft_findings"] is None:
        record["aborted"] = True
        record["stderr_tail"] = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
    else:
        record["aborted"] = False
    return record


def cell_ok(record):
    """A run that produced comparable numbers."""
    return not record["aborted"] and record["draft_findings"] is not None


# ── summary ────────────────────────────────────────────────────────────────────
def summarize(records):
    """Return the summary table (one row per harness × input-mode cell) as text."""
    header = ("harness", "input mode", "runs", "aborted", "empty drafts", "mean draft", "mean surviving")
    rows = []
    for harness in HARNESSES:
        for mode_label, _cap in INPUT_MODES:
            cell = [r for r in records if r["harness"] == harness and r["input_mode"] == mode_label]
            good = [r for r in cell if cell_ok(r)]
            empty = sum(1 for r in good if r["draft_findings"] == 0)
            if good:
                mean_draft = f"{sum(r['draft_findings'] for r in good) / len(good):.2f}"
                mean_surv = f"{sum(r['surviving_findings'] for r in good) / len(good):.2f}"
            else:
                mean_draft = mean_surv = "-"
            rows.append(
                (harness, mode_label, str(len(cell)), str(len(cell) - len(good)),
                 str(empty), mean_draft, mean_surv)
            )
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip(),
           "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return "\n".join(out)


# ── driver ─────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="A/B the finder stage on inlined vs elided diff input (issue #21)."
    )
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--runs", type=int, default=5, help="repetitions per cell (default 5)")
    ap.add_argument("--depth", default="standard", choices=["quick", "standard", "deep"])
    ap.add_argument("--confidence-bar", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--out", default="finder-ab.jsonl", help="JSONL result file (appended)")
    ap.add_argument(
        "--binary",
        default="",
        help="path to review-bot-review-local (default: nix-build the package)",
    )
    args = ap.parse_args(argv)
    if args.runs < 1:
        die("--runs must be at least 1")

    binary = args.binary or build_binary()

    # Pin the experiment to ONE PR head: cells are only comparable if every run read the
    # same code at the same depth and bar. The head is re-read before every run and a
    # change aborts loudly rather than silently mixing two diffs into one cell.
    token = load_token()
    head = pr_head_sha(args.owner, args.repo, args.pr, token)
    log(f"pinned to {args.owner}/{args.repo}#{args.pr} head {head[:12]} "
        f"(depth {args.depth}, bar {args.confidence_bar}, {args.runs} run(s) per cell)")

    records = []
    with open(args.out, "a") as sink:
        for harness in HARNESSES:
            for mode_label, cap in INPUT_MODES:
                for run_index in range(1, args.runs + 1):
                    now = pr_head_sha(args.owner, args.repo, args.pr, token)
                    if now != head:
                        die(
                            f"PR #{args.pr} head moved {head[:12]} -> {now[:12]} mid-experiment; "
                            f"the cells are no longer comparable. Re-run from scratch against "
                            f"the new head ({len(records)} run(s) already written to {args.out})."
                        )
                    log(f"{harness} / {mode_label} / run {run_index}/{args.runs} …")
                    record = run_once(binary, args, harness, mode_label, cap, run_index, head)
                    records.append(record)
                    sink.write(json.dumps(record) + "\n")
                    sink.flush()
                    if record["aborted"]:
                        log(f"  aborted (status {record['status']}) — recorded, not dropped")
                    else:
                        log(f"  {record['draft_findings']}→{record['surviving_findings']} "
                            f"({record['diff_mode']})")

    print()
    print(f"{args.owner}/{args.repo}#{args.pr} @ {head[:12]} — depth {args.depth}, "
          f"bar {args.confidence_bar}, {args.runs} run(s) per cell")
    print(summarize(records))
    print()
    print(f"{len(records)} run(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

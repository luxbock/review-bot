#!/usr/bin/env python3
"""Acceptance test for review-bot-feedback (feedback.py) — stdlib only, NO live forge.

Spins up a tiny http.server in a thread that serves canned
GET /api/v1/repos/{owner}/{repo}/issues/{n}/comments JSON, respecting the
page/limit query params so pagination is exercised (a full page of 50 then a
partial page, matching api_paged's paging). feedback.py is pointed at it via
FORGEJO_URL + a dummy FORGEJO_TOKEN and invoked as a subprocess.

Run:  python3 tests/test_feedback.py
"""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_PY = os.path.join(os.path.dirname(HERE), "feedback.py")

# Footer markers (must match feedback.py / review.py render footers).
REVIEW_FOOTER = "\n\n---\n*Automated review by **review-bot** · harness `claude` · depth `standard`.*"
TRIAGE_FOOTER = "\n\n---\n*Automated triage by **review-bot** · harness `claude`.*"
PARK_FOOTER = "## 🤖 review-bot — parked\n\n@olli parking further reviews."


def _c(cid, login, body, created_at):
    return {
        "id": cid,
        "user": {"login": login},
        "body": body,
        "created_at": created_at,
        "html_url": f"http://forge.example/comment/{cid}",
    }


# The PR/issue #70 thread. A deliberate mix of authors + a review, a triage, and a
# parked notice at different created_at timestamps. To exercise pagination the endpoint
# returns a FULL page of 50 review-bot-authored filler review comments first, then a
# partial page holding the interesting ones — so api_paged must fetch page 2 to see them.
_FILLER = [
    _c(1000 + i, "review-bot", f"filler review {i}" + REVIEW_FOOTER, f"2026-01-01T00:{i:02d}:00Z")
    for i in range(50)
]
_PARTIAL = [
    _c(1, "aatos", "PR author opening comment", "2026-02-01T10:00:00Z"),
    _c(2, "review-bot", "Old review body" + REVIEW_FOOTER, "2026-02-02T10:00:00Z"),
    _c(3, "some-human", "Looks reasonable to me @review-bot", "2026-02-03T10:00:00Z"),
    _c(4, "review_bot", "Triage: works as designed" + TRIAGE_FOOTER, "2026-02-04T10:00:00Z"),
    _c(5, "review-bot", PARK_FOOTER, "2026-02-05T10:00:00Z"),
    # NEWEST review-bot comment overall — a review, underscore-handle irrelevant here.
    _c(6, "review-bot", "Newest review — please fix X" + REVIEW_FOOTER, "2026-02-06T10:00:00Z"),
]
THREADS = {70: _FILLER + _PARTIAL, 71: [_c(9, "some-human", "no bot here", "2026-03-01T00:00:00Z")]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        page = int(qs.get("page", ["1"])[0])
        limit = int(qs.get("limit", ["50"])[0])
        parts = parsed.path.strip("/").split("/")
        # /api/v1/repos/{owner}/{repo}/issues/{n}/comments -> 8 segments
        try:
            assert parts[:3] == ["api", "v1", "repos"]
            assert parts[5] == "issues" and parts[7] == "comments"
            num = int(parts[6])
        except (IndexError, ValueError, AssertionError):
            self._send(404, {"message": "not found"})
            return
        thread = THREADS.get(num, [])
        start = (page - 1) * limit
        chunk = thread[start : start + limit]
        self._send(200, chunk)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FeedbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.host, cls.port = cls.server.server_address
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://{cls.host}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_tool(self, *args):
        env = dict(os.environ)
        env["FORGEJO_URL"] = self.base
        env["FORGEJO_TOKEN"] = "dummy-read-token"
        env.pop("REVIEW_BOT_HANDLES", None)  # exercise the default handle set
        proc = subprocess.run(
            [sys.executable, FEEDBACK_PY, *args],
            capture_output=True,
            text=True,
            env=env,
        )
        return proc

    def test_selects_newest_review_bot_comment(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "70")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = json.loads(proc.stdout)
        self.assertEqual(env["repo"], "o/r")
        self.assertEqual(env["number"], 70)
        self.assertEqual(env["target"], "pr")
        # Comment id 6 is the newest review-bot-authored comment (pagination-crossing).
        self.assertEqual(env["latest"]["id"], 6)
        self.assertEqual(env["latest"]["author"], "review-bot")
        self.assertEqual(env["latest"]["kind"], "review")
        self.assertIn("Newest review", env["latest"]["body_markdown"])
        # Envelope shape: no "all" without --all.
        self.assertNotIn("all", env)
        self.assertEqual(
            set(env["latest"].keys()),
            {"id", "html_url", "created_at", "author", "kind", "body_markdown"},
        )

    def test_kind_tagging(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "70", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = json.loads(proc.stdout)
        by_id = {e["id"]: e["kind"] for e in env["all"]}
        self.assertEqual(by_id[6], "review")
        self.assertEqual(by_id[5], "parked")
        self.assertEqual(by_id[4], "triage")
        self.assertEqual(by_id[2], "review")

    def test_all_newest_first(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "70", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = json.loads(proc.stdout)
        ts = [e["created_at"] for e in env["all"]]
        self.assertEqual(ts, sorted(ts, reverse=True))
        self.assertEqual(env["all"][0]["id"], 6)  # newest first
        # Includes all matched review-bot comments across both pages (50 filler + 4 real).
        self.assertEqual(len(env["all"]), 54)
        # No human comments leaked in.
        for e in env["all"]:
            self.assertIn(e["author"].lower(), {"review-bot", "review_bot"})

    def test_kind_filter_excludes_parked(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "70", "--all", "--kind", "review,triage")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = json.loads(proc.stdout)
        kinds = {e["kind"] for e in env["all"]}
        self.assertNotIn("parked", kinds)
        self.assertTrue(kinds <= {"review", "triage"})
        # The parked comment (id 5) is gone; latest is still the newest review (id 6).
        self.assertNotIn(5, [e["id"] for e in env["all"]])
        self.assertEqual(env["latest"]["id"], 6)

    def test_markdown_output(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "70", "--markdown")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Newest review", proc.stdout)
        self.assertIn(REVIEW_FOOTER.strip(), proc.stdout)
        # markdown mode prints the body, not JSON.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(proc.stdout)

    def test_issue_target_same_endpoint(self):
        # --issue uses the same comments endpoint; #70 works either way.
        proc = self.run_tool("--owner", "o", "--repo", "r", "--issue", "70")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = json.loads(proc.stdout)
        self.assertEqual(env["target"], "issue")
        self.assertEqual(env["latest"]["id"], 6)

    def test_no_bot_comment_exits_nonzero(self):
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "71")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("no review-bot feedback", proc.stderr.lower())

    def test_kind_filter_with_no_match_exits_nonzero(self):
        # #71 has no bot comments at all; filtering to triage also yields nothing.
        proc = self.run_tool("--owner", "o", "--repo", "r", "--pr", "71", "--kind", "triage")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Deterministic client/service protocol tests (stdlib only, no live services)."""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(REPO_ROOT, "client.py")
SERVE = os.path.join(REPO_ROOT, "serve.py")
CLIENT_ARGS = ["--owner", "acme", "--repo", "widget", "--pr", "7"]
LOST = (
    "review-bot-review: error: the review-bot service connection was lost — "
    "the review outcome is unknown and may already have posted; inspect the target "
    "or service journal before retrying\n"
)


def read_request(conn):
    """Consume through the client's SHUT_WR so peer close/reset timing is stable."""
    data = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    request = json.loads(data.decode())
    if request["owner"] != "acme" or request["repo"] != "widget":
        raise AssertionError(f"unexpected request: {request!r}")


def send_event(conn, event):
    conn.sendall((json.dumps(event) + "\n").encode())


def reset_peer(conn):
    """Abort rather than gracefully shutting down the accepted Unix connection."""
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()


class FakeUnixPeer:
    def __init__(self, handler):
        self.tmp = tempfile.mkdtemp(prefix="rb-client-test-")
        self.path = os.path.join(self.tmp, "review.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.handler = handler
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn = None
        try:
            conn, _ = self.listener.accept()
            self.handler(conn)
        except BaseException as e:
            self.error = e
        finally:
            if conn is not None and conn.fileno() >= 0:
                conn.close()
            self.listener.close()

    def finish(self):
        self.thread.join(5)
        try:
            if self.thread.is_alive():
                raise AssertionError("fake Unix-socket peer did not finish")
            if self.error is not None:
                raise self.error
        finally:
            shutil.rmtree(self.tmp)


class ClientTest(unittest.TestCase):
    maxDiff = None

    def run_client(self, handler, *extra_args):
        peer = FakeUnixPeer(handler)
        env = dict(os.environ, REVIEW_BOT_SOCKET=peer.path)
        try:
            proc = subprocess.run(
                [sys.executable, CLIENT, *CLIENT_ARGS, *extra_args],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
        finally:
            peer.finish()
        return proc

    def run_connect_error(self, exception_name, path):
        # A real subprocess runs client.main; only socket.socket is replaced so each
        # otherwise host-dependent connect errno is deterministic.
        code = f"""
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("tested_client", {CLIENT!r})
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
class FailingSocket:
    def connect(self, path):
        raise {exception_name}(13, "deterministic test error")
    def close(self):
        pass
client.socket.socket = lambda *args: FailingSocket()
client.SOCKET_PATH = {path!r}
sys.argv = ["review-bot-review", *{CLIENT_ARGS!r}]
client.main()
"""
        return subprocess.run(
            [sys.executable, "-c", code], text=True, capture_output=True, timeout=5
        )

    def assert_process(self, proc, code, stdout, stderr):
        self.assertEqual(proc.returncode, code)
        self.assertEqual(proc.stdout, stdout)
        self.assertEqual(proc.stderr, stderr)

    def test_file_not_found_diagnostic(self):
        path = "/deterministic/missing/review.sock"
        proc = self.run_connect_error("FileNotFoundError", path)
        self.assert_process(
            proc,
            1,
            "",
            "review-bot-review: error: review-bot service socket not found at "
            f"{path} — check that review-bot-review.socket is running, or set "
            "REVIEW_BOT_SOCKET to the correct path\n",
        )

    def test_connection_refused_diagnostic(self):
        path = "/deterministic/refused/review.sock"
        proc = self.run_connect_error("ConnectionRefusedError", path)
        self.assert_process(
            proc,
            1,
            "",
            "review-bot-review: error: review-bot service refused the connection at "
            f"{path} — check review-bot-review.socket and REVIEW_BOT_SOCKET\n",
        )

    def test_permission_error_diagnostic(self):
        path = "/deterministic/forbidden/review.sock"
        proc = self.run_connect_error("PermissionError", path)
        self.assert_process(
            proc,
            1,
            "",
            "review-bot-review: error: permission denied connecting to the review-bot "
            f"service at {path} — check review-bot-review.socket and the "
            "review-bot-client supplementary group; after group changes, restart the "
            "login/session or any long-running user manager\n",
        )

    def test_reset_before_response_is_unknown(self):
        def handler(conn):
            reset_peer(conn)

        self.assert_process(self.run_client(handler), 1, "", LOST)

    def test_log_then_reset_preserves_log_and_reports_unknown(self):
        def handler(conn):
            read_request(conn)
            send_event(conn, {"type": "log", "message": "review started"})
            reset_peer(conn)

        self.assert_process(
            self.run_client(handler), 1, "", "review-bot: review started\n" + LOST
        )

    def test_complete_success_then_reset_is_authoritative(self):
        def handler(conn):
            read_request(conn)
            send_event(
                conn,
                {"type": "result", "ok": True, "markdown": "done", "url": "https://forge/review/7"},
            )
            reset_peer(conn)

        self.assert_process(self.run_client(handler), 0, "https://forge/review/7\n", "")

    def test_explicit_failure_is_authoritative(self):
        def handler(conn):
            read_request(conn)
            send_event(
                conn,
                {"type": "result", "ok": False, "markdown": None, "url": None, "error": "engine failed"},
            )

        self.assert_process(
            self.run_client(handler),
            1,
            "",
            "review-bot-review: error: engine failed\n",
        )

    def test_clean_eof_without_result_is_unknown(self):
        def handler(conn):
            read_request(conn)

        self.assert_process(self.run_client(handler), 1, "", LOST)

    def test_clean_success_prints_markdown(self):
        def handler(conn):
            read_request(conn)
            send_event(
                conn,
                {"type": "result", "ok": True, "markdown": "complete review", "url": None},
            )

        self.assert_process(
            self.run_client(handler, "--print-only"), 0, "complete review\n", ""
        )

    def test_malformed_and_unknown_events_do_not_hide_result(self):
        def handler(conn):
            read_request(conn)
            conn.sendall(b"not-json\n[]\n")
            send_event(conn, {"type": "future-event", "value": 1})
            send_event(
                conn,
                {"type": "result", "ok": True, "markdown": None, "url": "https://forge/review/7"},
            )

        self.assert_process(
            self.run_client(handler),
            0,
            "https://forge/review/7\n",
            "review-bot-review: unparseable event: not-json\n",
        )


class ServicePathTest(unittest.TestCase):
    def test_service_emits_complete_success_and_exits_zero(self):
        with tempfile.TemporaryDirectory(prefix="rb-service-test-") as tmp:
            review_impl = os.path.join(tmp, "fake_review.py")
            with open(review_impl, "w", encoding="utf-8") as f:
                f.write(
                    "BAR_BY_DEPTH = {'quick': 'low', 'standard': 'medium', 'deep': 'high'}\n"
                    "def load_token(): return 'token'\n"
                    "class GitAuth:\n"
                    "    def __init__(self, token): pass\n"
                    "    def cleanup(self): pass\n"
                    "def do_pr_review(*args): return ('complete markdown', 'https://forge/review/7')\n"
                    "def do_issue_triage(*args): return ('complete markdown', 'https://forge/review/7')\n"
                    "def do_repo_audit(*args): return ('complete markdown', 'https://forge/review/7')\n"
                )

            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            code = f"""
import runpy
service = runpy.run_path({SERVE!r}, run_name="tested_service")
service["main"].__globals__["REVIEW_IMPL"] = {review_impl!r}
service["main"]()
"""
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdin=child,
                stdout=child,
                stderr=subprocess.PIPE,
            )
            child.close()
            try:
                request = {
                    "mode": "pr",
                    "owner": "acme",
                    "repo": "widget",
                    "number": 7,
                    "harness": "claude",
                    "depth": "standard",
                    "confidence_bar": "",
                    "focus": "",
                    "print_only": False,
                    "dry_run": False,
                }
                parent.sendall((json.dumps(request) + "\n").encode())
                parent.shutdown(socket.SHUT_WR)
                parent.settimeout(5)
                response = b""
                while True:
                    chunk = parent.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                returncode = proc.wait(timeout=5)
                stderr = proc.stderr.read().decode()
                proc.stderr.close()
            finally:
                parent.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

            events = [json.loads(line) for line in response.decode().splitlines()]
            self.assertEqual(returncode, 0, stderr)
            self.assertEqual(
                events[-1],
                {
                    "type": "result",
                    "ok": True,
                    "markdown": "complete markdown",
                    "url": "https://forge/review/7",
                    "error": None,
                },
            )


if __name__ == "__main__":
    unittest.main()

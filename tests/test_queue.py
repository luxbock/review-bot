#!/usr/bin/env python3
"""Deterministic queue-on-busy tests (issue #17): serve-level flock, client
busy-drop retry with exit 75, and mid-review loss unchanged. Stdlib only,
no live services, no engines, no network."""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(REPO_ROOT, "client.py")
SERVE = os.path.join(REPO_ROOT, "serve.py")
CLIENT_ARGS = ["--owner", "acme", "--repo", "widget", "--pr", "7"]


BUSY_MSG = (
    "review-bot-review: error: review-bot service busy — a review is already "
    "in flight; retry later\n"
)
LOST_MSG = (
    "review-bot-review: error: the review-bot service connection was lost — "
    "the review outcome is unknown and may already have posted; inspect the target "
    "or service journal before retrying\n"
)


def _read_request(conn):
    """Drain until the client's SHUT_WR; validate the request looks like ours."""
    data = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    request = json.loads(data.decode())
    if request["owner"] != "acme" or request["repo"] != "widget":
        raise AssertionError(f"unexpected request: {request!r}")


def _send_event(conn, event):
    conn.sendall((json.dumps(event) + "\n").encode())


def _reset_peer(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()


class FakeUnixPeer:
    def __init__(self, handler):
        self.tmp = tempfile.mkdtemp(prefix="rb-queue-test-")
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


def _run_client(handler, env_overrides=None, timeout=10, extra_args=()):
    peer = FakeUnixPeer(handler)
    env = dict(os.environ, REVIEW_BOT_SOCKET=peer.path)
    env.setdefault("REVIEW_BOT_BUSY_RETRIES", "0")
    if env_overrides:
        env.update(env_overrides)
    try:
        proc = subprocess.run(
            [sys.executable, CLIENT, *CLIENT_ARGS, *extra_args],
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        peer.finish()
    return proc


class QueuedPathTest(unittest.TestCase):
    """1) Peer emits a `queued` log then a delayed result → client waits and succeeds
    (no busy, no lost, no drop)."""

    def test_queued_log_then_delayed_result_succeeds(self):
        def handler(conn):
            _read_request(conn)
            # Mirror serve.py's initial accept-log, then the queued log a real
            # serve process emits when it can't grab the flock immediately.
            _send_event(conn, {"type": "log", "message": "pr acme/widget#7 (harness=claude depth=standard bar=medium)"})
            _send_event(conn, {"type": "log", "message": "queued: waiting for the in-flight review to finish"})
            # Non-trivial delay proves the client sits on the read instead of
            # bailing out; short enough to keep the suite fast.
            time.sleep(0.2)
            _send_event(
                conn,
                {"type": "result", "ok": True, "markdown": "done",
                 "url": "https://forge/review/7"},
            )

        proc = _run_client(handler)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "https://forge/review/7\n")
        # Both log lines were relayed to stderr; the busy/lost errors were not.
        self.assertIn("review-bot: queued: waiting for the in-flight review to finish", proc.stderr)
        self.assertNotIn("busy", proc.stderr.lower().split("queued:")[0])
        self.assertNotIn("connection was lost", proc.stderr)


class BusyDropTest(unittest.TestCase):
    """2) Peer accepts then closes with zero bytes; with REVIEW_BOT_BUSY_RETRIES=0,
    client exits 75 with the busy message, NOT the `connection was lost` text."""

    def test_zero_bytes_drop_exits_75_with_busy_message(self):
        def handler(conn):
            # Read the request (mirrors systemd having accepted the socket)
            # then close with no bytes emitted. The client must treat this as
            # a truthful busy signal, not an outcome-unknown lost error.
            _read_request(conn)

        proc = _run_client(handler, env_overrides={"REVIEW_BOT_BUSY_RETRIES": "0"})
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, BUSY_MSG)
        self.assertNotIn("connection was lost", proc.stderr)

    def test_reset_before_events_also_exits_75(self):
        def handler(conn):
            _reset_peer(conn)

        proc = _run_client(handler, env_overrides={"REVIEW_BOT_BUSY_RETRIES": "0"})
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertEqual(proc.stderr, BUSY_MSG)


class MidReviewLossTest(unittest.TestCase):
    """3) Peer sends one log event then resets → client still exits 1 with
    the existing `CONNECTION_LOST` text (mid-review outcome unknown, unchanged)."""

    def test_log_then_reset_still_reports_connection_lost(self):
        def handler(conn):
            _read_request(conn)
            _send_event(conn, {"type": "log", "message": "review started"})
            _reset_peer(conn)

        # Default retries=0 in _run_client is irrelevant — this is a ≥1-event
        # path so the busy-retry loop is never entered.
        proc = _run_client(handler)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "review-bot: review started\n" + LOST_MSG)


class ServeFlockContentionTest(unittest.TestCase):
    """4) Two real serve.py processes contending on one REVIEW_BOT_LOCK_FILE
    never hold the lock simultaneously; the second emits the queued log.

    This is the in-VM stand-in for the deferred runtime e2e (real systemd
    socket + ≥3 concurrent requests) — actual processes, real flock, not a
    mocked lock object."""

    REQUEST = {
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

    @staticmethod
    def _read_all_events(sock):
        sock.settimeout(15)
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                raise AssertionError(f"serve process did not close socket in time; got {data!r}")
            if not chunk:
                break
            data += chunk
        return [json.loads(l) for l in data.decode().splitlines() if l.strip()]

    def test_two_processes_serialize_and_second_emits_queued(self):
        with tempfile.TemporaryDirectory(prefix="rb-flock-test-") as tmp:
            review_impl = os.path.join(tmp, "fake_review.py")
            lock_file = os.path.join(tmp, "serve.lock")
            marker = os.path.join(tmp, "marker.txt")

            # Fake review.py: writes start-/end-<pid> around a 0.3s sleep, all
            # inside the pipeline (i.e. under serve.py's flock). If the flock
            # holds, the marker sequence is well-nested; if it doesn't, we see
            # interleaved start-B before end-A.
            with open(review_impl, "w", encoding="utf-8") as f:
                f.write(
                    "import os, time\n"
                    "BAR_BY_DEPTH = {'quick': 'low', 'standard': 'medium', 'deep': 'high'}\n"
                    "def load_token(): return 'token'\n"
                    "class GitAuth:\n"
                    "    def __init__(self, token): pass\n"
                    "    def cleanup(self): pass\n"
                    f"MARKER = {marker!r}\n"
                    "def _work():\n"
                    "    pid = os.getpid()\n"
                    "    with open(MARKER, 'a', encoding='utf-8') as mf:\n"
                    "        mf.write(f'start-{pid}\\n')\n"
                    "        mf.flush()\n"
                    "    time.sleep(0.3)\n"
                    "    with open(MARKER, 'a', encoding='utf-8') as mf:\n"
                    "        mf.write(f'end-{pid}\\n')\n"
                    "        mf.flush()\n"
                    "    return ('markdown', 'https://forge/review/7')\n"
                    "def do_pr_review(*a): return _work()\n"
                    "def do_issue_triage(*a): return _work()\n"
                    "def do_repo_audit(*a): return _work()\n"
                )

            env = dict(os.environ, REVIEW_BOT_LOCK_FILE=lock_file)
            request_line = (json.dumps(self.REQUEST) + "\n").encode()

            def spawn():
                parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
                code = (
                    "import runpy\n"
                    f"service = runpy.run_path({SERVE!r}, run_name='tested_service')\n"
                    f"service['main'].__globals__['REVIEW_IMPL'] = {review_impl!r}\n"
                    "service['main']()\n"
                )
                proc = subprocess.Popen(
                    [sys.executable, "-c", code],
                    stdin=child,
                    stdout=child,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                child.close()
                parent.sendall(request_line)
                parent.shutdown(socket.SHUT_WR)
                return proc, parent

            def wait_for_marker_start(deadline):
                while time.monotonic() < deadline:
                    if os.path.exists(marker):
                        with open(marker, "r", encoding="utf-8") as mf:
                            if any(l.startswith("start-") for l in mf):
                                return True
                    time.sleep(0.01)
                return False

            proc_a, sock_a = spawn()
            # Wait until A has actually entered the pipeline (grabbed the lock
            # + written its start marker). Without this, B could race in and
            # win the flock, and the queued path wouldn't fire.
            self.assertTrue(
                wait_for_marker_start(time.monotonic() + 5),
                "A never reached the pipeline start marker",
            )
            proc_b, sock_b = spawn()

            try:
                events_a = self._read_all_events(sock_a)
                events_b = self._read_all_events(sock_b)
                rc_a = proc_a.wait(timeout=15)
                rc_b = proc_b.wait(timeout=15)
                stderr_a = proc_a.stderr.read().decode()
                stderr_b = proc_b.stderr.read().decode()
            finally:
                proc_a.stderr.close()
                proc_b.stderr.close()
                sock_a.close()
                sock_b.close()
                for p in (proc_a, proc_b):
                    if p.poll() is None:
                        p.kill()
                        p.wait()

            # Both serve processes finished successfully with a result event.
            self.assertEqual(rc_a, 0, stderr_a)
            self.assertEqual(rc_b, 0, stderr_b)
            self.assertTrue(
                any(e.get("type") == "result" and e.get("ok") for e in events_a),
                f"A missing ok result: {events_a!r}",
            )
            self.assertTrue(
                any(e.get("type") == "result" and e.get("ok") for e in events_b),
                f"B missing ok result: {events_b!r}",
            )

            # B (the waiter) must emit the queued log; A (the lock holder) must not.
            def has_queued(events):
                return any(
                    e.get("type") == "log" and "queued" in e.get("message", "")
                    for e in events
                )
            self.assertTrue(has_queued(events_b),
                            f"B did not emit the queued log; got {events_b!r}")
            self.assertFalse(has_queued(events_a),
                             f"A should not have emitted queued; got {events_a!r}")

            # Marker file must be well-nested: start-A, end-A, start-B, end-B.
            # Any interleaving would prove the flock did not serialize.
            with open(marker, "r", encoding="utf-8") as mf:
                entries = [l.strip() for l in mf if l.strip()]
            self.assertEqual(len(entries), 4, f"unexpected marker entries: {entries!r}")
            starts = [i for i, e in enumerate(entries) if e.startswith("start-")]
            ends = [i for i, e in enumerate(entries) if e.startswith("end-")]
            self.assertEqual(starts, [0, 2],
                             f"pipelines overlapped: {entries!r}")
            self.assertEqual(ends, [1, 3],
                             f"pipelines overlapped: {entries!r}")
            # The pids for the first start/end must match, and same for second.
            first_pid = entries[0].split("-", 1)[1]
            self.assertEqual(entries[1], f"end-{first_pid}",
                             f"pipelines overlapped: {entries!r}")
            second_pid = entries[2].split("-", 1)[1]
            self.assertEqual(entries[3], f"end-{second_pid}",
                             f"pipelines overlapped: {entries!r}")
            self.assertNotEqual(first_pid, second_pid, entries)


if __name__ == "__main__":
    unittest.main()

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

from campusnet.config import Config
from campusnet.daemon import Daemon
from campusnet.detector import NetStatus
from campusnet.providers import LoginResult
from campusnet.providers.ruijie import RuijieProvider
from campusnet.runner import Runner, RunResult
from campusnet.session import Response, Session
from campusnet.singleton import ProcessLock
from campusnet.wifi import WifiResult
from campusnet.procflags import no_window_kwargs


class RedirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/landing?nasip=fresh")
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(b"landing")
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:{}/".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_redirect_is_preserved_unless_requested(self):
        session = Session(retries=0)
        for follow in (False, True, False):
            response = session.get(self.url, allow_redirects=follow)
            self.assertEqual(response.status, 200 if follow else 302)
            if follow:
                self.assertTrue(response.url.endswith("/landing?nasip=fresh"))
            else:
                self.assertEqual(response.location, "/landing?nasip=fresh")

    def test_fresh_portal_query_overrides_stored_token(self):
        session = Mock()
        session.post.return_value = Response(200, {}, b'{"result":"success"}', self.url)
        provider = RuijieProvider(session, {"query_string": "nasip=stale"})
        provider.login(self.url + "?wlanuserip=ip&nasip=fresh", "test", "test", "10.0.0.1")
        self.assertIn("nasip=fresh", session.post.call_args.kwargs["data"]["queryString"])
        session.get.assert_not_called()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(username="test", password="test", wifi_ssid="tjus_wifi",
                          provider="ruijie", portal_ip="http://192.0.2.1",
                          options={"verify_delay": 0})
        self.runner = Runner(self.cfg)
        self.lock_name = "campusnet-test-" + uuid.uuid4().hex
        lock_patch = patch("campusnet.runner.ProcessLock", side_effect=lambda _: ProcessLock(self.lock_name))
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    def test_other_wifi_and_unknown_never_probe_or_authenticate(self):
        for ssid in ("hotspot", "", "TJUS_WIFI"):
            with patch("campusnet.runner.current_ssid", return_value=ssid), \
                 patch.object(self.runner, "status") as status:
                result = self.runner.ensure_online(force=True)
                self.assertTrue(result.skipped)
                self.assertFalse(result.ok)
                status.assert_not_called()

    def test_online_does_not_authenticate(self):
        with patch("campusnet.runner.current_ssid", return_value="tjus_wifi"), \
             patch.object(self.runner, "status", return_value=NetStatus(online=True)), \
             patch("campusnet.runner.get_provider") as provider:
            result = self.runner.ensure_online()
            self.assertTrue(result.ok)
            provider.assert_not_called()

    def test_online_claim_requires_connectivity_and_recovers_later(self):
        provider = Mock()
        provider.login.return_value = LoginResult(True, "ruijie", already_online=True)
        with patch("campusnet.runner.current_ssid", return_value="tjus_wifi"), \
             patch.object(self.runner, "status", side_effect=[NetStatus(), NetStatus(),
                                                               NetStatus(), NetStatus(online=True)]), \
             patch("campusnet.runner.discover_portal_url", return_value=self.cfg.portal_ip), \
             patch("campusnet.runner.get_provider", return_value=lambda *a: provider):
            self.assertFalse(self.runner.ensure_online().ok)
            self.assertTrue(self.runner.ensure_online().ok)
            self.assertEqual(provider.login.call_count, 2)

    def test_network_changed_during_probe_never_sends_password(self):
        with patch("campusnet.runner.current_ssid", side_effect=["tjus_wifi", "hotspot"]), \
             patch.object(self.runner, "status", return_value=NetStatus()), \
             patch("campusnet.runner.discover_portal_url", return_value=self.cfg.portal_ip), \
             patch("campusnet.runner.get_provider") as provider:
            self.assertTrue(self.runner.ensure_online().skipped)
            provider.assert_not_called()

    def test_exceptions_release_login_lock(self):
        with patch("campusnet.runner.current_ssid", return_value="tjus_wifi"), \
             patch.object(self.runner, "status", side_effect=RuntimeError("probe failure")):
            with self.assertRaises(RuntimeError):
                self.runner.ensure_online()
        lock = ProcessLock(self.lock_name)
        self.assertTrue(lock.acquire())
        lock.release()

    def test_backoff_resets_after_recovery(self):
        class Clock:
            now = 0
            done = False
            def is_set(self): return self.done
            def wait(self, seconds): self.now += seconds
        clock = Clock()
        attempts = []
        def attempt():
            attempts.append(clock.now)
            if len(attempts) == 8:
                clock.done = True
            return RunResult(ok=len(attempts) == 6, message="simulated")
        with patch.object(self.runner, "ensure_wifi", return_value=WifiResult(ok=True)), \
             patch.object(self.runner, "ensure_online", side_effect=attempt), \
             patch("campusnet.daemon.time.monotonic", side_effect=lambda: clock.now):
            Daemon(self.runner).watch(stop_event=clock)
        self.assertEqual(attempts, [0, 5, 20, 50, 110, 170, 185, 190])

    def test_reconnect_interrupts_previous_backoff(self):
        class Clock:
            now = 0
            done = False
            def is_set(self): return self.done
            def wait(self, seconds): self.now += seconds
        clock = Clock()
        attempts = []
        def wifi(): return WifiResult(ok=clock.now != 65)
        def attempt():
            attempts.append(clock.now)
            if clock.now >= 80: clock.done = True
            return RunResult(message="offline")
        with patch.object(self.runner, "ensure_wifi", side_effect=wifi), \
             patch.object(self.runner, "ensure_online", side_effect=attempt), \
             patch("campusnet.daemon.time.monotonic", side_effect=lambda: clock.now):
            Daemon(self.runner).watch(stop_event=clock)
        self.assertEqual(attempts, [0, 5, 20, 50, 80])


class LockTests(unittest.TestCase):
    def test_process_collision_then_release(self):
        name = "campusnet-test-" + uuid.uuid4().hex
        lock = ProcessLock(name)
        self.assertTrue(lock.acquire())
        code = "from campusnet.singleton import ProcessLock; import sys; lock=ProcessLock(sys.argv[1]); sys.exit(0 if lock.acquire() else 9)"
        def child():
            return subprocess.run([sys.executable, "-c", code, name], timeout=10,
                                  capture_output=True, **no_window_kwargs()).returncode
        try:
            self.assertEqual(child(), 9)
        finally:
            lock.release()
        self.assertEqual(child(), 0)


if __name__ == "__main__":
    unittest.main()

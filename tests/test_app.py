import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from campusnet.cli import main
from campusnet.config import Config
from campusnet.daemon import Daemon
from campusnet.runner import Runner, RunResult
from campusnet.runtime import load_config, write_json
from campusnet.wifi import WifiResult


class AppTests(unittest.TestCase):
    def test_modes_are_exclusive_and_force_is_only_once(self):
        for args in (["--once", "--self-test"], ["--force"], ["--report", "x.json"]):
            with self.assertRaises(ValueError):
                main(args)

    def test_invalid_config_never_constructs_network_runner(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("campusnet.cli.state_dir", return_value=Path(folder)), \
             patch("campusnet.cli.make_logger"), \
             patch("campusnet.cli.Runner") as runner:
            self.assertEqual(main(["--check-config", "--config", str(Path(folder) / "missing.json")]), 2)
            runner.assert_not_called()

    def test_manual_and_background_launch_route_to_desktop_without_valid_credentials(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("campusnet.cli.state_dir", return_value=Path(folder)), \
             patch("campusnet.cli.make_logger"), \
             patch("campusnet.cli.start_desktop", return_value=0) as desktop, \
             patch("campusnet.cli.load_config") as config:
            self.assertEqual(main([]), 0)
            self.assertFalse(desktop.call_args.args[2])
            self.assertEqual(main(["--background"]), 0)
            self.assertTrue(desktop.call_args.args[2])
            config.assert_not_called()

    def test_config_accepts_bom_rejects_invalid_types_and_bounds(self):
        valid = dict(username="test", password="test", wifi_ssid="tjus_wifi",
                     provider="ruijie", portal_ip="http://192.0.2.1")
        with tempfile.TemporaryDirectory() as folder, \
             patch("campusnet.config._keyring_get", return_value=""):
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(valid), encoding="utf-8-sig")
            self.assertEqual(load_config(path).wifi_ssid, "tjus_wifi")
            for invalid in ([], {**valid, "options": []}, {**valid, "timeout": True},
                            {**valid, "wifi_ssid": 12}, {**valid, "provider": "missing"},
                            {**valid, "portal_ip": "file:///tmp"},
                            {**valid, "options": {"verify_delay": -1}},
                            {**valid, "options": {"online_check_seconds": 0}}):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_check_config_does_not_probe_network(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("campusnet.cli.state_dir", return_value=Path(folder)), \
             patch("campusnet.cli.make_logger"), \
             patch("campusnet.cli.load_config", return_value=Config()), \
             patch("campusnet.cli.Runner") as runner:
            self.assertEqual(main(["--check-config"]), 0)
            runner.assert_not_called()

    def test_atomic_state_is_valid_during_concurrent_writes(self):
        errors = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "status.json"
            def write(number):
                try:
                    for index in range(10):
                        write_json(path, {"pid": number, "sequence": index})
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(path.read_text())["sequence"], 9)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])


class DaemonTests(unittest.TestCase):
    def test_duplicate_never_probes_or_overwrites_status(self):
        runner, publish = Mock(), Mock()
        with patch("campusnet.daemon.ProcessLock") as factory:
            factory.return_value.acquire.return_value = False
            self.assertEqual(Daemon(runner, publish).run(), 0)
        runner.ensure_online.assert_not_called()
        publish.assert_not_called()

    def test_failed_round_retries_then_recovers_and_releases_lock(self):
        class Clock:
            now = 0
            done = False
            def is_set(self): return self.done
            def wait(self, seconds): self.now += seconds
        clock = Clock()
        runner = Runner(Config(wifi_ssid="tjus_wifi"))
        states, attempts = [], []
        def attempt():
            attempts.append(clock.now)
            if len(attempts) == 1:
                raise OSError("temporary failure")
            clock.done = True
            return RunResult(ok=True, message="recovered")
        with patch.object(runner, "ensure_wifi", return_value=WifiResult(ok=True, ssid="tjus_wifi")), \
             patch.object(runner, "ensure_online", side_effect=attempt), \
             patch("campusnet.daemon.time.monotonic", side_effect=lambda: clock.now), \
             patch("campusnet.daemon.ProcessLock") as factory:
            self.assertEqual(Daemon(runner, states.append).run(clock), 0)
            factory.return_value.release.assert_called_once()
        self.assertEqual(attempts, [0, 5])
        self.assertEqual([s["state"] for s in states],
                         ["starting", "checking", "retrying", "checking", "online", "stopped"])
        self.assertEqual(states[-2]["retries"], 0)
        self.assertTrue(all(s["heartbeat"] for s in states))


if __name__ == "__main__":
    unittest.main()

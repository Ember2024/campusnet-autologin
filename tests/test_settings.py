import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from campusnet.config import Config
from campusnet.daemon import Daemon
from campusnet.runner import RunResult
from campusnet.settings import read_settings, save_credentials
from campusnet.wifi import WifiResult


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'config.json'

    def test_reads_file_credentials_without_environment_overrides(self):
        self.path.write_text(json.dumps({'username': 'file-user', 'password': 'file-password'}),
                             encoding='utf-8-sig')
        with patch.dict(os.environ, {'CAMPUSNET_USERNAME': 'env-user',
                                     'CAMPUSNET_PASSWORD': 'env-password'}):
            self.assertEqual(read_settings(self.path), ('file-user', 'file-password'))

    def test_save_preserves_unknown_fields_and_all_existing_options(self):
        original = {'username': 'old', 'password': 'old-password',
                    'provider': 'custom-provider', 'portal_ip': 'http://192.0.2.1',
                    'options': {'service': 'selected', 'custom': {'nested': [1, 2]}},
                    'future_option': ['keep', 3]}
        self.path.write_text(json.dumps(original), encoding='utf-8')
        save_credentials(self.path, ' new-user ', ' new-password ')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),
                         {**original, 'username': 'new-user', 'password': ' new-password '})
        self.assertEqual(read_settings(self.path), ('new-user', ' new-password '))

    def test_missing_config_initializes_tjus_defaults(self):
        self.assertEqual(read_settings(self.path), ('', ''))
        save_credentials(self.path, 'new-user', 'new-password')
        document = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(document['wifi_ssid'], 'tjus_wifi')
        self.assertEqual(document['provider'], 'ruijie')
        self.assertEqual(document['portal_ip'], 'http://192.168.24.65')
        self.assertEqual(document['options']['online_check_seconds'], 15)
        self.assertFalse(document['use_proxy'])

    def test_corrupt_or_nonobject_config_is_not_overwritten(self):
        for content in (b'{broken', b'[]', b'null', b'false', b'\xff\xfe'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_settings(self.path)
                with self.assertRaises(ValueError):
                    save_credentials(self.path, 'new-user', 'new-password')
                self.assertEqual(self.path.read_bytes(), content)

    def test_empty_credentials_never_create_config(self):
        for username, password in (('', 'valid'), ('   ', 'valid'), ('valid', ''),
                                   ('valid', ' \t '), (None, 'valid'), ('valid', 123)):
            with self.subTest(username=username, password=password):
                with self.assertRaises(ValueError):
                    save_credentials(self.path, username, password)
                self.assertFalse(self.path.exists())

    def test_read_rejects_credentials_with_invalid_types(self):
        for document in ({'username': []}, {'password': None}):
            self.path.write_text(json.dumps(document), encoding='utf-8')
            with self.assertRaises(ValueError):
                read_settings(self.path)

    def test_failed_replace_preserves_old_config_and_removes_temporary_file(self):
        original = b'{"username":"old","password":"old-password"}'
        self.path.write_bytes(original)
        with patch('campusnet.runtime.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                save_credentials(self.path, 'new-user', 'new-password')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


class ReloadTests(unittest.TestCase):
    def runner(self, result=None):
        runner = Mock()
        runner.cfg = Config(wifi_ssid='tjus_wifi', options={'online_check_seconds': 3600})
        runner.ensure_wifi.return_value = WifiResult(ok=True, ssid='tjus_wifi')
        runner.ensure_online.return_value = result or RunResult(ok=True, message='simulated online')
        return runner

    def start(self, daemon):
        stop, errors = threading.Event(), []
        def watch():
            try:
                daemon.watch(stop)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        def cleanup():
            stop.set()
            daemon.request_reload()
            thread.join(2)
            self.assertFalse(thread.is_alive(), 'worker did not stop')
            self.assertEqual(errors, [])
        self.addCleanup(cleanup)
        return thread

    def test_missing_runner_publishes_configuration_heartbeat(self):
        class Clock:
            now = 0
            done = False
            def is_set(self): return self.done
            def wait(self, seconds): self.now += seconds
        clock, states = Clock(), []
        def publish(state):
            states.append((clock.now, state))
            if len(states) == 2:
                clock.done = True
        Daemon(None, publish).watch(clock)
        self.assertEqual([instant for instant, _ in states], [0, 15])
        self.assertEqual([state['state'] for _, state in states], ['needs_config', 'needs_config'])
        self.assertTrue(all(state['retries'] == 0 and state['ssid'] == '' for _, state in states))

    def test_failed_initial_reload_waits_for_settings_then_recovers(self):
        needs_config, online = threading.Event(), threading.Event()
        runner, logger = self.runner(), Mock()
        loader = Mock(side_effect=[ValueError('sensitive-password'), runner])
        def publish(state):
            if state['state'] == 'needs_config': needs_config.set()
            if state['state'] == 'online': online.set()
        daemon = Daemon(None, publish, reload_runner=loader, logger=logger)
        self.start(daemon)
        self.assertTrue(needs_config.wait(2))
        self.assertIsNone(daemon.runner)
        runner.ensure_online.assert_not_called()
        daemon.request_reload()
        self.assertTrue(online.wait(2))
        self.assertEqual(loader.call_count, 2)
        self.assertNotIn('sensitive-password', str(logger.call_args_list))

    def test_reload_interrupts_backoff_and_runs_only_in_worker(self):
        retrying, online = threading.Event(), threading.Event()
        old = self.runner(RunResult(message='simulated failure'))
        new = self.runner()
        pending, callback_threads, auth_threads = [old, new], [], []
        def load():
            callback_threads.append(threading.get_ident())
            return pending.pop(0)
        def authenticate():
            auth_threads.append(threading.get_ident())
            return RunResult(ok=True, message='new credentials')
        new.ensure_online.side_effect = authenticate
        def publish(state):
            if state['state'] == 'retrying': retrying.set()
            if state['state'] == 'online': online.set()
        daemon = Daemon(None, publish, reload_runner=load)
        worker = self.start(daemon)
        self.assertTrue(retrying.wait(2))
        daemon.request_reload()
        self.assertTrue(online.wait(2), 'reload did not interrupt the five-second backoff')
        self.assertEqual(callback_threads, [worker.ident, worker.ident])
        self.assertEqual(auth_threads, [worker.ident])
        old.ensure_online.assert_called_once()
        self.assertEqual(daemon.state['retries'], 0)

    def test_reload_during_authentication_waits_until_round_finishes(self):
        entered, release, reloaded, online = (threading.Event() for _ in range(4))
        old, new = self.runner(), self.runner()
        pending = [old, new]
        def load():
            runner = pending.pop(0)
            if runner is new: reloaded.set()
            return runner
        def authenticate():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release authentication')
            return RunResult(message='old round finished')
        old.ensure_online.side_effect = authenticate
        def publish(state):
            if state['state'] == 'online': online.set()
        daemon = Daemon(None, publish, reload_runner=load)
        self.start(daemon)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        daemon.request_reload()
        self.assertFalse(reloaded.wait(0.05))
        self.assertIs(daemon.runner, old)
        new.ensure_online.assert_not_called()
        release.set()
        self.assertTrue(online.wait(2))
        self.assertIs(daemon.runner, new)
        self.assertTrue(reloaded.is_set())
        old.ensure_online.assert_called_once()
        new.ensure_online.assert_called_once()

    def test_invalid_reload_discards_stale_runner_then_recovers(self):
        first_online, needs_config, second_online = (threading.Event() for _ in range(3))
        old, new = self.runner(), self.runner()
        loader = Mock(side_effect=[old, ValueError('invalid config'), new])
        def publish(state):
            if state['state'] == 'needs_config': needs_config.set()
            if state['state'] == 'online':
                (second_online if first_online.is_set() else first_online).set()
        daemon = Daemon(None, publish, reload_runner=loader)
        self.start(daemon)
        self.assertTrue(first_online.wait(2))
        daemon.request_reload()
        self.assertTrue(needs_config.wait(2))
        self.assertIsNone(daemon.runner)
        old.ensure_online.assert_called_once()
        daemon.request_reload()
        self.assertTrue(second_online.wait(2))
        self.assertIs(daemon.runner, new)
        self.assertEqual(loader.call_count, 3)


if __name__ == '__main__':
    unittest.main()

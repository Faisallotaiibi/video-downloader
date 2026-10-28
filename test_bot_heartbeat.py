import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bot_heartbeat import BotHeartbeat


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.delivery = Mock()
        self.delivery.ready = True
        self.delivery.configured = True
        self.delivery.url = 'https://example.test/telegram/webhook'
        self.delivery.secret = 'never-log-this'
        self.delivery.public_url = 'https://example.test'
        self.delivery.stopping = threading.Event()
        self.delivery.store.busy_since = None
        self.delivery.overdue.return_value = False
        self.delivery.workers = [Mock(), Mock()]
        self.info = SimpleNamespace(url=self.delivery.url, pending_update_count=0, last_error_date=0)
        self.delivery.bot.get_webhook_info.return_value = self.info
        self.heartbeat = BotHeartbeat(self.delivery, state_path=str(Path(self.folder.name, 'heartbeat.json')))
        env = patch.dict(os.environ, {'BOT_PUBLIC_HEARTBEAT': 'false'})
        env.start()
        self.addCleanup(env.stop)

    def test_repair_missing_webhook_without_discarding_pending_updates(self):
        self.delivery.bot.get_webhook_info.side_effect = [
            SimpleNamespace(url='', pending_update_count=5, last_error_date=0), self.info]
        self.heartbeat.check_once()
        self.assertEqual(self.heartbeat.failures, 0)
        settings = self.delivery.bot.set_webhook.call_args.kwargs
        self.assertFalse(settings['drop_pending_updates'])
        self.assertEqual(settings['secret_token'], self.delivery.secret)
        self.assertEqual(settings['url'], self.delivery.url)

    def test_recent_telegram_delivery_error_repairs_secret_once(self):
        self.info.pending_update_count = 2
        self.info.last_error_date = time.time()
        self.heartbeat.check_once()
        self.heartbeat.check_once()
        self.delivery.bot.set_webhook.assert_called_once()

    def test_healthy_hook_is_not_reset_and_state_file_contains_no_secret(self):
        self.heartbeat.check_once()
        self.delivery.bot.set_webhook.assert_not_called()
        self.delivery.check_local_delivery.assert_called_once()
        path = Path(self.heartbeat.state_path)
        data = json.loads(path.read_text())
        self.assertEqual(data['interval_seconds'], 300)
        self.assertEqual(data['cycles'], 1)
        self.assertIsNone(data['last_error'])
        self.assertNotIn(self.delivery.secret, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_transient_failure_recovers_next_cycle_without_stopping_workers(self):
        self.delivery.check_local_delivery.side_effect = [TimeoutError('secret-value'), None]
        self.heartbeat.check_once()
        self.assertEqual(self.heartbeat.failures, 1)
        self.assertEqual(self.heartbeat.last_error, 'TimeoutError')
        self.assertIsNone(self.heartbeat.failure_reason())
        self.heartbeat.check_once()
        self.assertEqual(self.heartbeat.failures, 0)
        self.delivery.terminate.assert_not_called()

    def test_supervisor_detects_hung_check_and_stale_success(self):
        self.heartbeat.cycle_started = time.monotonic() - 151
        self.assertEqual(self.heartbeat.failure_reason(), 'heartbeat_stalled')
        self.heartbeat.cycle_started = None
        self.heartbeat.last_success = time.monotonic() - 661
        self.assertEqual(self.heartbeat.failure_reason(), 'delivery_checks_stale')
        with patch.object(self.delivery.stopping, 'wait', return_value=False):
            self.heartbeat.supervise()
        self.delivery.terminate.assert_called_once_with(1)

    def test_repeated_timer_runs_and_shutdown_stops_future_checks(self):
        self.heartbeat.interval = .04
        self.heartbeat.start()
        deadline = time.monotonic() + 2
        while self.heartbeat.cycles < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.delivery.stopping.set()
        self.heartbeat.thread.join(1)
        self.heartbeat.watchdog.join(1)
        self.assertGreaterEqual(self.heartbeat.cycles, 3)
        self.assertFalse(self.heartbeat.thread.is_alive())
        self.assertFalse(self.heartbeat.watchdog.is_alive())

    def test_dead_worker_or_stalled_database_requires_recovery(self):
        self.delivery.workers[0].is_alive.return_value = False
        self.assertEqual(self.heartbeat.failure_reason(), 'worker_stopped')
        self.delivery.workers[0].is_alive.return_value = True
        self.delivery.store.busy_since = time.monotonic() - 61
        self.assertEqual(self.heartbeat.failure_reason(), 'database_call_stalled')

    def test_failed_repair_is_not_reported_as_success(self):
        self.info.url = ''
        self.delivery.bot.set_webhook.return_value = False
        self.heartbeat.check_once()
        self.assertIsNone(self.heartbeat.last_success)
        self.assertEqual(self.heartbeat.failures, 1)
        self.delivery.check_local_delivery.assert_not_called()


if __name__ == '__main__':
    unittest.main()

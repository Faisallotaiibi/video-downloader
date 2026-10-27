import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask
import telebot

from reliable_delivery import InboxStore, WebhookDelivery, WEBHOOK_PATH, webhook_secret


TOKEN = '123456789:unit_test_only'


def update(update_id=1, text='https://vt.tiktok.com/example/'):
    return {'update_id': update_id, 'message': {'message_id': update_id, 'date': 1,
            'chat': {'id': 17, 'type': 'private'},
            'from': {'id': 17, 'is_bot': False, 'first_name': 'Test'}, 'text': text}}


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for expected state')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name, 'inbox.db'))
        self.store = InboxStore(self.path)
        self.store.schema()
        self.bot = telebot.TeleBot(TOKEN, threaded=False)
        self.bot.set_webhook = Mock(return_value=True)
        self.bot.get_webhook_info = Mock(return_value=SimpleNamespace(
            url='https://example.test' + WEBHOOK_PATH, pending_update_count=2))
        self.delivery = WebhookDelivery(self.bot, self.store, TOKEN, 'https://example.test')
        self.app = Flask(__name__)
        self.delivery.register(self.app)
        self.client = self.app.test_client()
        self.delivery.ready = True

    def tearDown(self):
        self.delivery.stop(timeout=2)
        self.folder.cleanup()

    def post(self, body=None, secret=None):
        return self.client.post(WEBHOOK_PATH, json=body or update(), headers={
            'X-Telegram-Bot-Api-Secret-Token': secret or webhook_secret(TOKEN)})

    def rows(self):
        return self.store.execute('SELECT * FROM telegram_inbox ORDER BY update_id')

    def test_only_authenticated_telegram_requests_are_accepted(self):
        self.assertEqual(self.post(secret='bad').status_code, 403)
        self.assertEqual(self.client.post(WEBHOOK_PATH, json=update()).status_code, 403)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.post().status_code, 200)

    def test_ack_follows_commit_and_duplicate_delivery_is_not_added_twice(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(InboxStore(self.path).claim('restarted')['update_id'], 1)

    def test_storage_failure_returns_retryable_response_without_ack(self):
        with patch.object(self.store, 'accept', side_effect=TimeoutError):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.post().status_code, 200)

    def test_queue_overload_keeps_update_at_telegram_for_retry(self):
        for number in range(200):
            self.store.accept(update(number))
        self.assertEqual(self.post(update(201)).status_code, 503)
        self.assertEqual(self.post(update(1)).status_code, 200)
        self.assertEqual(len(self.rows()), 200)

    def test_invalid_payload_and_large_payload_are_rejected(self):
        for body in [{'update_id': True}, {'update_id': '1'}, ['bad']]:
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.post({'update_id': 1, 'padding': 'x' * (1024 * 1024)}).status_code, 413)

    def test_pending_and_expired_lease_survive_process_restart(self):
        self.store.accept(update(1))
        self.store.accept(update(2))
        self.assertEqual(self.store.claim('old')['update_id'], 1)
        reopened = InboxStore(self.path)
        self.assertEqual(reopened.claim('new')['update_id'], 2)
        self.assertIsNone(reopened.claim('new'))
        reopened.execute('UPDATE telegram_inbox SET lease_until=0 WHERE update_id=1')
        self.assertEqual(reopened.claim('new')['update_id'], 1)

    def test_atomic_claim_across_instances_and_completion_is_owner_fenced(self):
        self.store.accept(update())
        barrier = threading.Barrier(3)
        claims = []
        def claim(owner):
            barrier.wait()
            claims.append((owner, InboxStore(self.path).claim(owner)))
        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ['a', 'b']]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        winner = next(owner for owner, job in claims if job)
        self.assertEqual(sum(job is not None for _, job in claims), 1)
        self.store.finish(1, 'wrong')
        self.assertEqual(self.rows()[0]['state'], 'running')
        self.store.finish(1, winner)
        self.assertEqual(self.rows()[0]['state'], 'done')
        self.assertEqual(self.rows()[0]['payload'], '{}')

    def test_real_telegram_dispatch_stays_responsive_when_one_download_blocks(self):
        release, entered, quick = threading.Event(), threading.Event(), threading.Event()
        @self.bot.message_handler(func=lambda message: True)
        def handler(message):
            if message.text == 'slow':
                entered.set()
                release.wait(2)
            else:
                quick.set()
        try:
            self.post(update(1, 'slow'))
            self.delivery.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.post(update(2, '/start')).status_code, 200)
            self.assertTrue(quick.wait(1))
        finally:
            release.set()
        until(lambda: all(row['state'] == 'done' for row in self.rows()))

    def test_handler_error_does_not_kill_worker_or_replay_finished_update(self):
        calls = []
        @self.bot.message_handler(func=lambda message: True)
        def handler(message):
            calls.append(message.message_id)
            if message.message_id == 1:
                raise RuntimeError('simulated failure')
        self.post(update(1))
        self.post(update(2))
        self.delivery.start()
        until(lambda: len(calls) == 2)
        until(lambda: self.rows()[0]['state'] == 'failed')
        self.post(update(2))
        time.sleep(.1)
        self.assertEqual(sorted(calls), [1, 2])

    def test_webhook_registration_never_drops_pending_updates_and_shutdown_keeps_hook(self):
        self.delivery.start()
        settings = self.bot.set_webhook.call_args.kwargs
        self.assertIs(settings['drop_pending_updates'], False)
        self.assertEqual(settings['allowed_updates'], ['message', 'callback_query'])
        self.assertEqual(settings['secret_token'], webhook_secret(TOKEN))
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.delivery.stop()
        self.assertEqual(self.client.get('/healthz').status_code, 503)
        self.assertEqual(self.post().status_code, 503)

    def test_finishing_write_failure_retries_storage_without_replaying_handler(self):
        calls = []
        @self.bot.message_handler(func=lambda message: True)
        def handler(message):
            calls.append(message.message_id)
        self.post()
        finish = self.store.finish
        failures = [True]
        def flaky(*args):
            if failures:
                failures.pop()
                raise TimeoutError
            finish(*args)
        with patch.object(self.store, 'finish', side_effect=flaky):
            self.delivery.start()
            until(lambda: self.rows()[0]['state'] == 'done', timeout=4)
        self.assertEqual(calls, [1])

    def test_monitor_detects_stalled_jobs_and_database_without_waiting_minutes(self):
        terminate = Mock()
        self.delivery.terminate = terminate
        self.delivery.active[1] = time.monotonic() - 721
        with patch.object(self.delivery.stopping, 'wait', return_value=False):
            self.delivery._monitor()
        terminate.assert_called_once_with(1)
        self.delivery.active.clear()
        terminate.reset_mock()
        self.store.busy_since = time.monotonic() - 61
        with patch.object(self.delivery.stopping, 'wait', return_value=False):
            self.delivery._monitor()
        terminate.assert_called_once_with(1)

    def test_render_requires_persistent_storage(self):
        with self.assertRaises(RuntimeError):
            InboxStore(self.path, require_remote=True)

    def test_remote_inbox_failure_never_acknowledges_ephemeral_local_fallback(self):
        ephemeral = str(Path(self.folder.name, 'must-not-exist.db'))
        remote = Mock()
        remote.execute.side_effect = ConnectionError('temporary database outage')
        store = InboxStore(ephemeral, remote_factory=lambda: remote, require_remote=True)
        try:
            with self.assertRaises(ConnectionError):
                store.schema()
            self.assertFalse(os.path.exists(ephemeral))
            self.assertIsNone(store.remote)
            remote.close.assert_called_once()
        finally:
            store.executor.shutdown()

    def test_remote_sql_path_commits_and_reopens_without_reprocessing_completed_update(self):
        # Exercise the exact remote ResultSet/Row interface using a real SQLite
        # file; the inbox must not rely on local-only transaction semantics.
        class Row:
            def __init__(self, row):
                self.row = row
            def asdict(self):
                return dict(self.row)
        class Remote:
            def execute(_, sql, params):
                with sqlite3.connect(self.path, timeout=3) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = [Row(row) for row in connection.execute(sql, params).fetchall()]
                return SimpleNamespace(rows=rows)
        stores = [InboxStore('unused', remote_factory=Remote, require_remote=True) for _ in range(2)]
        try:
            stores[0].schema()
            self.assertTrue(stores[0].accept(update()))
            self.assertFalse(stores[1].accept(update()))
            self.assertEqual(stores[1].claim('new')['update_id'], 1)
            self.assertIsNone(stores[0].claim('old'))
            stores[1].finish(1, 'new')
            self.assertIsNone(stores[0].claim('old'))
        finally:
            for store in stores:
                store.executor.shutdown()

    def test_completed_payload_is_removed_and_old_dedup_records_are_pruned(self):
        self.store.accept(update())
        self.store.claim('owner')
        self.store.finish(1, 'owner')
        self.store.execute('UPDATE telegram_inbox SET finished=?', (time.time() - 86401,))
        self.store.prune()
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()

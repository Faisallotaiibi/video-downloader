import importlib
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import telebot
from telebot import types

from public_users import PublicUsers, UNAVAILABLE, COUNT_CALLBACK
from reliable_delivery import InboxStore


TOKEN = '123456789:unit_test_only'


def user(user_id=17, **fields):
    return SimpleNamespace(id=user_id, is_bot=False, **fields)


def update(text='/users', user_id=17, callback=None, photo=False):
    sender = {'id': user_id, 'is_bot': False, 'first_name': 'Test'}
    body = {'message_id': 1, 'date': 1, 'chat': {'id': 123, 'type': 'private'}, 'from': sender}
    if photo:
        body['photo'] = [{'file_id': 'p', 'file_unique_id': 'p', 'width': 10, 'height': 10}]
    else:
        body['text'] = text
    if callback:
        return types.Update.de_json({'update_id': 1, 'callback_query': {
            'id': 'click', 'from': sender, 'chat_instance': 'test', 'data': callback,
            'message': {**body, 'from': {'id': 999, 'is_bot': True, 'first_name': 'Bot'}}}})
    return types.Update.de_json({'update_id': 1, 'message': body})


class PublicUsersTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = InboxStore(str(Path(self.folder.name, 'usage.db')))
        self.counter = PublicUsers(self.store.execute)
        self.bot = telebot.TeleBot(TOKEN, threaded=False)
        self.bot.reply_to = Mock()
        self.bot.answer_callback_query = Mock()
        self.counter.install(self.bot)
        self.worker = None

    def tearDown(self):
        self.counter.stopped.set()
        if self.worker:
            self.worker.join(2)
        self.folder.cleanup()

    def activate(self):
        self.worker = threading.Thread(target=self.counter.activate, daemon=True)
        self.worker.start()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Counter did not finish')
            time.sleep(.01)

    def test_same_id_deduplicates_across_names_and_restarts(self):
        self.assertIn('1 👥', self.counter.text(user(username='old')))
        self.assertIn('1 👥', self.counter.text(user(username='new')))
        reopened = PublicUsers(self.store.execute)
        self.assertIn('1 👥', reopened.text(user()))
        self.assertIn('2 👥', reopened.text(user(18)))

    def test_backfill_only_distinct_positive_telegram_ids(self):
        self.store.execute('CREATE TABLE usage_log(source TEXT,telegram_id INTEGER,requester TEXT)')
        for source, user_id, name in [('telegram', 5, 'old'), ('telegram', 5, 'new'),
                                      ('telegram', 6, 'same'), ('web', 7, 'web'),
                                      ('telegram', None, 'unknown'), ('telegram', -1, 'chat'),
                                      ('telegram', 0, 'invalid')]:
            self.store.execute('INSERT INTO usage_log VALUES (?,?,?)', (source, user_id, name))
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 2 👥')
        PublicUsers(self.store.execute).schema()
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 2 👥')

    def test_legacy_schema_without_ids_is_not_counted_by_name(self):
        self.store.execute('CREATE TABLE usage_log(requester TEXT)')
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 0 👥')

    def test_bots_anonymous_and_chat_ids_are_excluded(self):
        self.counter.text(SimpleNamespace(id=999, is_bot=True))
        for value in [None, -1, 0, True, '17']:
            self.counter.observe(user(value))
        self.counter.observe(None)
        self.assertTrue(self.counter.pending.empty())
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 0 👥')

    def test_two_instances_count_same_person_once(self):
        self.counter.schema()
        other = PublicUsers(self.store.execute)
        other.schema()
        threads = [threading.Thread(target=counter.record, args=(17,))
                   for counter in [self.counter, other] for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 1 👥')

    def test_failures_never_publish_zero_or_stop_download_handler(self):
        self.counter.execute = Mock(side_effect=TimeoutError)
        self.assertEqual(self.counter.text(user()), UNAVAILABLE)
        download = Mock()
        self.bot.register_message_handler(download, content_types=['text'], func=lambda m: True)
        self.bot.process_new_updates([update('https://example.test/video')])
        download.assert_called_once()

    def test_blocked_background_count_does_not_block_downloads(self):
        entered, release = threading.Event(), threading.Event()
        real_execute = self.counter.execute
        def execute(sql, params=()):
            if sql.startswith('INSERT OR IGNORE INTO telegram_users'):
                entered.set()
                release.wait(2)
            return real_execute(sql, params)
        self.counter.execute = execute
        self.activate()
        self.counter.observe(user())
        try:
            self.assertTrue(entered.wait(1))
            download = Mock()
            self.bot.register_message_handler(download, content_types=['text'], func=lambda m: True)
            started = time.monotonic()
            self.bot.process_new_updates([update('https://example.test/video', user_id=18)])
            self.assertLess(time.monotonic() - started, .2)
            download.assert_called_once()
        finally:
            release.set()

    def test_observer_preserves_media_signature_and_audio_handlers(self):
        self.activate()
        upload, audio, signature = Mock(), Mock(), Mock()
        self.bot.register_message_handler(upload, content_types=['photo'])
        self.bot.register_callback_query_handler(audio, func=lambda c: c.data.startswith('audio:'))
        self.bot.register_callback_query_handler(signature, func=lambda c: c.data.startswith('sig:'))
        self.bot.process_new_updates([update(photo=True)])
        self.bot.process_new_updates([update(callback='audio:test', user_id=18)])
        self.bot.process_new_updates([update(callback='sig:test', user_id=19)])
        upload.assert_called_once()
        audio.assert_called_once()
        signature.assert_called_once()
        self.wait_for(lambda: self.counter.pending.unfinished_tasks == 0)
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 3 👥')

    def test_command_arabic_alias_and_button_show_current_total(self):
        self.bot.process_new_updates([update()])
        self.assertEqual(self.bot.reply_to.call_args.args[1], 'عدد مستخدمي البوت 1 👥')
        self.bot.process_new_updates([update('عدد المستخدمين', user_id=18)])
        self.assertEqual(self.bot.reply_to.call_args.args[1], 'عدد مستخدمي البوت 2 👥')
        self.bot.process_new_updates([update(callback=COUNT_CALLBACK, user_id=19)])
        self.bot.answer_callback_query.assert_called_once_with(
            'click', 'عدد مستخدمي البوت 3 👥', show_alert=True)
        self.assertEqual(self.counter.text(), 'عدد مستخدمي البوت 3 👥')

    def test_real_start_dispatch_includes_count_and_button(self):
        # Import production handlers with test credentials; no network or real
        # Telegram messages, and route all counter SQL to the temporary DB.
        with patch.dict(os.environ, {'BOT_TOKEN': TOKEN, 'BOT_TRANSPORT': 'polling',
                                    'TURSO_DATABASE_URL': '', 'TURSO_AUTH_TOKEN': '',
                                    'RENDER': '', 'RENDER_EXTERNAL_URL': ''}):
            main = importlib.import_module('main')
        with patch.object(main, 'public_users', self.counter), \
             patch.object(main.bot, 'threaded', False), \
             patch.object(main.bot, 'reply_to') as reply:
            main.bot.process_new_updates([update('/start')])
        self.assertIn(main.WELCOME_MESSAGE, reply.call_args.args[1])
        self.assertIn('عدد مستخدمي البوت 1 👥', reply.call_args.args[1])
        keyboard = reply.call_args.kwargs['reply_markup'].to_dict()
        self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'], COUNT_CALLBACK)

    def test_background_recovers_after_temporary_storage_failure(self):
        self.counter.schema()
        real_record = self.counter.record
        self.counter.record = Mock(side_effect=[TimeoutError, None])
        # Exercise retries without sleeping five seconds or stopping the worker.
        real_wait = self.counter.stopped.wait
        def short_wait(seconds):
            return real_wait(.01 if seconds == 5 else seconds)
        with patch.object(self.counter.stopped, 'wait', side_effect=short_wait):
            self.activate()
            self.counter.observe(user())
            self.wait_for(lambda: self.counter.record.call_count == 2)
            self.wait_for(lambda: self.counter.pending.unfinished_tasks == 0)
        self.counter.record = real_record


if __name__ == '__main__':
    unittest.main()

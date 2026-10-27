"""Public unique Telegram-user count, isolated from download/inbox storage."""
import logging
import queue
import threading
import time

from telebot import types
from telebot.handler_backends import ContinueHandling

logger = logging.getLogger(__name__)
COUNT_CALLBACK = 'public_users:count'
UNAVAILABLE = 'عدد مستخدمي البوت غير متاح مؤقتًا'


def human_id(user):
    value = getattr(user, 'id', None)
    if type(value) is int and value > 0 and not getattr(user, 'is_bot', True):
        return value
    return None


class PublicUsers:
    def __init__(self, execute):
        # Use a dedicated bounded connection, never the inbox's executor or
        # usage logging's ephemeral fallback. Only IDs are stored/publicly
        # aggregated; names, chats and download counts are not identities.
        self.execute = execute
        self.ready = False
        self.schema_lock = threading.Lock()
        self.pending = queue.Queue(maxsize=1000)
        self.queued = set()
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def schema(self):
        if self.ready:
            return
        if not self.schema_lock.acquire(timeout=.1):
            raise TimeoutError('User count initializing')
        try:
            if self.ready:
                return
            self.execute('CREATE TABLE IF NOT EXISTS telegram_users ('
                         'user_id INTEGER PRIMARY KEY, first_seen REAL NOT NULL)')
            columns = {row['name'] for row in self.execute('PRAGMA table_info(usage_log)')}
            if {'telegram_id', 'source'} <= columns:
                self.execute(
                    'INSERT OR IGNORE INTO telegram_users(user_id,first_seen) '
                    'SELECT DISTINCT telegram_id,? FROM usage_log '
                    "WHERE source='telegram' AND typeof(telegram_id)='integer' AND telegram_id>0",
                    (time.time(),))
            self.ready = True
        finally:
            self.schema_lock.release()

    def record(self, user_id):
        self.schema()
        self.execute('INSERT OR IGNORE INTO telegram_users(user_id,first_seen) VALUES (?,?)',
                     (user_id, time.time()))

    def observe(self, user):
        """No network/SQL on the normal download or signature handler path."""
        user_id = human_id(user)
        if user_id is None:
            return
        with self.lock:
            if user_id in self.queued:
                return
            try:
                self.pending.put_nowait(user_id)
            except queue.Full:
                logger.warning('Public user count queue full; downloads remain available')
                return
            self.queued.add(user_id)

    def activate(self):
        while not self.stopped.is_set():
            try:
                self.schema()
                count = self.execute('SELECT COUNT(*) AS count FROM telegram_users')[0]['count']
                logger.info('PUBLIC_USERS_READY count=%s', count)
                break
            except Exception as error:
                logger.warning('Public user count initialization: %s', type(error).__name__)
                self.stopped.wait(5)
        while not self.stopped.is_set():
            try:
                user_id = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                # Retry this ID without blocking bot handlers or losing it on
                # a temporary database outage. The primary key deduplicates
                # retries and simultaneous instances.
                while not self.stopped.is_set():
                    try:
                        self.record(user_id)
                        break
                    except Exception as error:
                        logger.warning('Public user count write: %s', type(error).__name__)
                        self.stopped.wait(5)
            finally:
                with self.lock:
                    self.queued.discard(user_id)
                self.pending.task_done()

    def text(self, user=None):
        try:
            user_id = human_id(user)
            if user_id is not None:
                self.record(user_id)
            else:
                self.schema()
            count = self.execute('SELECT COUNT(*) AS count FROM telegram_users')[0]['count']
            return f'عدد مستخدمي البوت {count:,} 👥'
        except Exception as error:
            # A failed read never publishes an invented zero or stale count.
            logger.warning('Public user count unavailable: %s', type(error).__name__)
            return UNAVAILABLE

    @staticmethod
    def keyboard():
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton('عدد مستخدمي البوت 👥', callback_data=COUNT_CALLBACK))
        return keyboard

    def install(self, bot):
        # Passthrough observers preserve the bot's existing dispatch and
        # exception semantics. Register before all media/signature handlers.
        def message_observer(message):
            self.observe(message.from_user)
            return ContinueHandling()

        def callback_observer(call):
            self.observe(call.from_user)
            return ContinueHandling()

        bot.register_message_handler(message_observer, func=lambda message: True,
                                     content_types=['text', 'photo', 'document', 'animation',
                                                    'video', 'audio', 'voice', 'sticker',
                                                    'video_note', 'contact', 'location', 'poll', 'dice'])
        bot.register_callback_query_handler(callback_observer, func=lambda call: True)

        def show_count(message):
            bot.reply_to(message, self.text(message.from_user), reply_markup=self.keyboard())

        def count_callback(call):
            bot.answer_callback_query(call.id, self.text(call.from_user), show_alert=True)

        bot.register_message_handler(show_count, commands=['users'])
        bot.register_message_handler(show_count, func=lambda message: (message.text or '').strip() == 'عدد المستخدمين')
        bot.register_callback_query_handler(count_callback, func=lambda call: call.data == COUNT_CALLBACK)

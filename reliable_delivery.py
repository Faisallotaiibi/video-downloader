"""Durable, authenticated Telegram delivery that also wakes free web hosting.

Telegram retries non-2xx responses. Only acknowledge after the update is stored;
never silently fall back from a persistent inbox to ephemeral Render storage.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlparse

import requests
from types import SimpleNamespace

from flask import jsonify, request
from telebot import types

logger = logging.getLogger(__name__)
WEBHOOK_PATH = '/telegram/webhook'
MAX_BODY = 1024 * 1024


def webhook_secret(token):
    return hmac.new(token.encode(), b'the966bot-webhook-v1', hashlib.sha256).hexdigest()



class _HTTPRow(dict):
    def asdict(self):
        return dict(self)


class TursoHTTP:
    """Stateless bounded SQL requests; no stale async keep-alive sessions."""
    def __init__(self, url, token):
        self.url = url.replace('libsql://', 'https://', 1).replace('turso://', 'https://', 1).rstrip('/') + '/v2/pipeline'
        if not self.url.startswith('https://'):
            raise ValueError('Persistent inbox requires HTTPS')
        self.token = token

    @staticmethod
    def value(value):
        if value is None:
            return {'type': 'null'}
        if isinstance(value, (int, bool)):
            return {'type': 'integer', 'value': str(int(value))}
        if isinstance(value, float):
            return {'type': 'float', 'value': value}
        if isinstance(value, str):
            return {'type': 'text', 'value': value}
        raise TypeError('Unsupported SQL parameter')

    def execute(self, sql, params=()):
        payload = {'requests': [
            {'type': 'execute', 'stmt': {'sql': sql, 'args': [self.value(v) for v in params], 'want_rows': True}},
            {'type': 'close'}]}
        # No automatic write retries: callers resolve uncertain commits by
        # update ID. Fresh requests avoid the idle-connection reset observed in
        # production, and the explicit close releases the database stream.
        with requests.post(self.url, json=payload, headers={
                'Authorization': 'Bearer ' + self.token, 'Connection': 'close'},
                timeout=(2, 3)) as response:
            response.raise_for_status()
            results = response.json()['results']
        result = results[0]
        if result.get('type') != 'ok':
            code = result.get('error', {}).get('code', 'UNKNOWN')
            raise RuntimeError('Inbox SQL failed: ' + str(code))
        table = result['response']['result']
        columns = [col['name'] for col in table.get('cols', [])]
        def decode(cell):
            kind = cell['type']
            if kind == 'null':
                return None
            if kind == 'integer':
                return int(cell['value'])
            if kind == 'float':
                return float(cell['value'])
            return cell.get('value')
        rows = [_HTTPRow(zip(columns, [decode(cell) for cell in row]))
                for row in table.get('rows', [])]
        return SimpleNamespace(rows=rows)

    def close(self):
        pass


class InboxStore:
    def __init__(self, path, remote_factory=None, require_remote=False):
        if require_remote and remote_factory is None:
            raise RuntimeError('Persistent inbox storage is required on Render')
        self.path, self.remote_factory = path, remote_factory
        self.remote = None
        self.busy_since = None
        self.slot = threading.BoundedSemaphore(1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='inbox-db') if remote_factory else None

    def _remote_execute(self, sql, params):
        if self.remote is None:
            self.remote = self.remote_factory()
        try:
            return [row.asdict() for row in self.remote.execute(sql, params).rows]
        except Exception:
            stale = self.remote
            self.remote = None
            # Sync libsql clients own an event-loop thread and HTTP session.
            # Retire both before reconnecting, instead of leaking them after
            # every transient upstream disconnect. The call remains bounded by
            # execute() and the stalled-database watchdog.
            try:
                stale.close()
            except Exception:
                pass
            raise

    def execute(self, sql, params=()):
        if self.executor:
            if not self.slot.acquire(timeout=2):
                raise TimeoutError('Inbox storage is busy')
            self.busy_since = time.monotonic()
            try:
                future = self.executor.submit(self._remote_execute, sql, params)
            except BaseException:
                self.slot.release()
                self.busy_since = None
                raise
            def finished(_):
                self.busy_since = None
                self.slot.release()
            future.add_done_callback(finished)
            # No fallback: the sender must retry if durability cannot be confirmed.
            return future.result(timeout=6)
        with sqlite3.connect(self.path, timeout=3) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def schema(self):
        self.execute('CREATE TABLE IF NOT EXISTS telegram_inbox ('
                     'update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, '
                     "state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, "
                     'lease_until REAL NOT NULL DEFAULT 0, owner TEXT, finished REAL)')
        self.execute('CREATE INDEX IF NOT EXISTS inbox_pending ON telegram_inbox(state,lease_until,update_id)')

    def accept(self, payload):
        update_id = payload['update_id']
        rows = self.execute(
            'INSERT OR IGNORE INTO telegram_inbox(update_id,payload,created) '
            'SELECT ?,?,? WHERE (SELECT COUNT(*) FROM telegram_inbox '
            "WHERE state IN ('pending','running'))<200 RETURNING update_id",
            (update_id, json.dumps(payload, ensure_ascii=False), time.time()))
        if not rows and not self.execute('SELECT update_id FROM telegram_inbox WHERE update_id=?', (update_id,)):
            raise TimeoutError('Inbox is full')
        return bool(rows)

    def claim(self, owner, lease_seconds=900):
        now = time.time()
        rows = self.execute(
            "UPDATE telegram_inbox SET state='running',owner=?,lease_until=? "
            'WHERE update_id=(SELECT update_id FROM telegram_inbox '
            "WHERE state='pending' OR (state='running' AND lease_until<?) "
            'ORDER BY update_id LIMIT 1) RETURNING update_id,payload',
            (owner, now + lease_seconds, now))
        return rows[0] if rows else None

    def finish(self, update_id, owner, failed=False):
        self.execute("UPDATE telegram_inbox SET state=?,payload='{}',finished=?,lease_until=0 "
                     'WHERE update_id=? AND owner=?',
                     ('failed' if failed else 'done', time.time(), update_id, owner))

    def prune(self):
        self.execute('DELETE FROM telegram_inbox WHERE finished<?', (time.time() - 86400,))


class WebhookDelivery:
    def __init__(self, bot, store, token, public_url, *, terminate=None):
        if not public_url.startswith('https://'):
            raise ValueError('Webhook URL must use HTTPS')
        if bot.threaded:
            raise ValueError('Durable delivery requires synchronous handler dispatch')
        self.bot, self.store = bot, store
        self.url = public_url.rstrip('/') + WEBHOOK_PATH
        self.secret = webhook_secret(token)
        self.owner = uuid.uuid4().hex
        self.ready = False
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.workers = []
        self.active = {}
        self.lock = threading.Lock()
        self.last_maintenance = 0
        self.monitor = None
        self.terminate = terminate or os._exit

    def register(self, app):
        app.add_url_rule(WEBHOOK_PATH, 'telegram_webhook', self.receive, methods=['POST'])
        app.add_url_rule('/healthz', 'bot_health', self.health)

    def receive(self):
        supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(supplied, self.secret):
            return jsonify(ok=False), 403
        if request.content_length is not None and request.content_length > MAX_BODY:
            return jsonify(ok=False), 413
        if not self.ready or self.stopping.is_set():
            return jsonify(ok=False), 503
        raw = request.stream.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return jsonify(ok=False), 413
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or type(payload.get('update_id')) is not int:
                return jsonify(ok=False), 400
            types.Update.de_json(payload)
        except (ValueError, TypeError, KeyError, AttributeError):
            return jsonify(ok=False), 400
        try:
            created = self.store.accept(payload)
        except Exception as error:
            logger.warning('Webhook not acknowledged: storage %s', type(error).__name__)
            return jsonify(ok=False), 503
        if created:
            logger.info('Telegram update persisted update_id=%s', payload['update_id'])
        self.wake.set()
        return jsonify(ok=True)

    def health(self):
        healthy = self.ready and not self.stopping.is_set() and all(w.is_alive() for w in self.workers)
        return jsonify(ok=healthy, transport='webhook', persistent_inbox=bool(self.store.remote_factory)), 200 if healthy else 503

    def start(self):
        if self.monitor is None:
            self.monitor = threading.Thread(target=self._monitor, name='telegram-monitor', daemon=True)
            self.monitor.start()
        self.store.schema()
        self.store.prune()
        for index in range(2):
            worker = threading.Thread(target=self._worker, name=f'telegram-job-{index}', daemon=True)
            self.workers.append(worker)
            worker.start()
        # Start accepting durable updates before registration; Telegram may POST
        # immediately, and an existing webhook also survives a sleeping instance.
        self.ready = True
        self._configure()

    def _configure(self):
        delay = 1
        while not self.stopping.is_set():
            try:
                before = self.bot.get_webhook_info(timeout=15)
                self._audit(before, 'before-registration')
                identity = self.bot.get_me()
                logger.info('Telegram bot identity username=%s expected_webhook_host=%s',
                            identity.username, urlparse(self.url).netloc)
                if not self.bot.set_webhook(url=self.url, secret_token=self.secret,
                        max_connections=2, allowed_updates=['message', 'callback_query'],
                        drop_pending_updates=False, timeout=15):
                    raise RuntimeError('Webhook registration was rejected')
                info = self.bot.get_webhook_info(timeout=15)
                self._audit(info, 'after-registration')
                if info.url != self.url:
                    raise RuntimeError('Webhook verification failed')
                logger.info('Telegram webhook verified persistent_inbox=%s pending=%s',
                            bool(self.store.remote_factory), info.pending_update_count)
                return
            except Exception as error:
                logger.warning('Webhook registration retry: %s', type(error).__name__)
                self.stopping.wait(delay)
                delay = min(delay * 2, 60)

    def _audit(self, info, stage):
        error = str(getattr(info, 'last_error_message', '') or '')
        error = error.replace(str(getattr(self.bot, 'token', 'not-a-token')), '[redacted]')
        logger.info('Telegram delivery audit stage=%s host=%s path_matches=%s pending=%s last_error_date=%s last_error=%s',
                    stage, urlparse(info.url).netloc, info.url == self.url,
                    info.pending_update_count, getattr(info, 'last_error_date', None), error[:300])

    def _worker(self):
        while not self.stopping.is_set():
            try:
                job = self.store.claim(self.owner)
                if job is None:
                    self.wake.wait(60)
                    self.wake.clear()
                    continue
                update_id = job['update_id']
                with self.lock:
                    self.active[update_id] = time.monotonic()
                failed = False
                try:
                    self.bot.process_new_updates([types.Update.de_json(json.loads(job['payload']))])
                except Exception as error:
                    # Never re-send a possibly delivered video just because a
                    # later handler step failed; preserve the worker for new work.
                    failed = True
                    logger.warning('Telegram handler failed update_id=%s type=%s', update_id, type(error).__name__)
                # Do not replay a completed handler if a finish write transiently
                # fails. Retry the write while retaining the job's lease.
                while True:
                    try:
                        self.store.finish(update_id, self.owner, failed)
                        break
                    except Exception as error:
                        logger.warning('Inbox completion retry: %s', type(error).__name__)
                        if self.stopping.wait(2):
                            return
                logger.info('Telegram update completed update_id=%s failed=%s', update_id, failed)
                with self.lock:
                    self.active.pop(update_id, None)
            except Exception as error:
                logger.warning('Inbox worker retry: %s', type(error).__name__)
                self.stopping.wait(2)

    def overdue(self):
        with self.lock:
            return any(time.monotonic() - started > 720 for started in self.active.values())

    def _monitor(self):
        while not self.stopping.wait(15):
            db_stalled = self.store.busy_since is not None and time.monotonic() - self.store.busy_since > 60
            if db_stalled or self.overdue() or any(not worker.is_alive() for worker in self.workers):
                logger.critical('Telegram worker stalled; exiting for automatic host recovery')
                self.terminate(1)
                return
            if time.monotonic() - self.last_maintenance >= 3600:
                try:
                    self.store.prune()
                    self.last_maintenance = time.monotonic()
                except Exception as error:
                    logger.warning('Inbox maintenance retry: %s', type(error).__name__)

    def stop(self, timeout=20):
        self.ready = False
        self.stopping.set()
        self.wake.set()
        deadline = time.monotonic() + timeout
        for worker in self.workers:
            worker.join(max(0, deadline - time.monotonic()))
        # Keep the webhook registered: it is what wakes the next instance.

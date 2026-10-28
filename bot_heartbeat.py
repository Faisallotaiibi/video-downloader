"""Five-minute delivery checks and independent supervision.

This can repair a running process. It cannot execute after the hosting provider
suspends that process; always-on hosting is required to remove idle shutdowns.
"""
import json
import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)
INTERVAL = 300
STALE_AFTER = 660


class BotHeartbeat:
    def __init__(self, delivery, *, interval=INTERVAL, state_path=None):
        self.delivery = delivery
        self.interval = interval
        self.state_path = state_path or '/tmp/the966bot-heartbeat.json'
        self.started = time.monotonic()
        self.last_success = None
        self.last_finished = None
        self.cycle_started = None
        self.failures = 0
        self.cycles = 0
        self.last_error = None
        self.repaired_error_date = 0
        self.lock = threading.Lock()
        self.thread = None
        self.watchdog = None

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.run, name='bot-heartbeat', daemon=True)
        self.watchdog = threading.Thread(target=self.supervise, name='bot-supervisor', daemon=True)
        self.thread.start()
        self.watchdog.start()

    def status(self):
        with self.lock:
            return {
                'interval_seconds': self.interval,
                'cycles': self.cycles,
                'consecutive_failures': self.failures,
                'last_error': self.last_error,
                'last_success_age_seconds': (round(time.monotonic() - self.last_success, 1)
                                             if self.last_success is not None else None),
            }

    def healthy(self):
        with self.lock:
            reference = self.last_success if self.last_success is not None else self.started
            return time.monotonic() - reference < STALE_AFTER

    def refresh_webhook(self):
        d = self.delivery
        info = d.bot.get_webhook_info(timeout=15)
        error_date = getattr(info, 'last_error_date', 0) or 0
        recent_error = (info.pending_update_count > 0 and error_date > self.repaired_error_date
                        and time.time() - error_date < 600)
        if info.url != d.url or recent_error:
            if not d.bot.set_webhook(url=d.url, secret_token=d.secret,
                                    max_connections=2, allowed_updates=['message', 'callback_query'],
                                    drop_pending_updates=False, timeout=15):
                raise RuntimeError('Webhook repair rejected')
            info = d.bot.get_webhook_info(timeout=15)
            if info.url != d.url:
                raise RuntimeError('Webhook repair not confirmed')
            self.repaired_error_date = error_date
            logger.warning('HEARTBEAT webhook repaired; pending updates preserved')
        return info.pending_update_count

    def check_once(self):
        d = self.delivery
        self.cycle_started = time.monotonic()
        try:
            pending = self.refresh_webhook()
            # Use this instance's HTTP server. Public routing may still target
            # the retiring instance during a rolling deploy.
            d.check_local_delivery()
            d.store.prune()
            # Best-effort public traffic reduces free-tier idle shutdowns. It
            # is NOT the recovery mechanism and cannot wake a suspended process.
            if os.environ.get('BOT_PUBLIC_HEARTBEAT', 'true').lower() == 'true':
                try:
                    with requests.get(d.public_url + '/healthz?probe=heartbeat', timeout=(5, 15)) as response:
                        response.raise_for_status()
                except Exception as error:
                    logger.warning('HEARTBEAT public path unavailable type=%s', type(error).__name__)
            with self.lock:
                self.last_success = time.monotonic()
                self.failures = 0
                self.last_error = None
            logger.info('HEARTBEAT OK interval=%ss pending=%s local_receipt=ok inbox=ok dispatch=ok',
                        self.interval, pending)
        except Exception as error:
            with self.lock:
                self.failures += 1
                # Exception messages can contain API URLs or credentials.
                self.last_error = type(error).__name__
            logger.warning('HEARTBEAT FAILED type=%s consecutive=%s', self.last_error, self.failures)
        finally:
            with self.lock:
                self.cycles += 1
                self.last_finished = time.monotonic()
            self.cycle_started = None
            self.write_state()

    def write_state(self):
        state = self.status()
        state['checked_at_unix'] = time.time()
        state['pid'] = os.getpid()
        # Private atomic file: no partial JSON and no tokens or user content.
        temporary = self.state_path + '.' + str(os.getpid()) + '.tmp'
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump(state, target)
            os.replace(temporary, self.state_path)
        except OSError as error:
            logger.warning('HEARTBEAT state write failed type=%s', type(error).__name__)

    def run(self):
        stop = self.delivery.stopping
        while not stop.is_set() and not self.delivery.configured:
            stop.wait(1)
        next_check = time.monotonic()
        while not stop.is_set():
            self.check_once()
            next_check += self.interval
            # Never accumulate overlapping probes after an outage.
            if next_check < time.monotonic():
                next_check = time.monotonic() + self.interval
            stop.wait(max(0, next_check - time.monotonic()))

    def failure_reason(self):
        d = self.delivery
        now = time.monotonic()
        if d.store.busy_since is not None and now - d.store.busy_since > 60:
            return 'database_call_stalled'
        if d.overdue():
            return 'handler_stalled'
        if d.ready and (len(d.workers) != 2 or any(not worker.is_alive() for worker in d.workers)):
            return 'worker_stopped'
        if self.thread is not None and not self.thread.is_alive():
            return 'heartbeat_stopped'
        if self.cycle_started is not None and now - self.cycle_started > 150:
            return 'heartbeat_stalled'
        if not self.healthy():
            return 'delivery_checks_stale'
        return None

    def supervise(self):
        while not self.delivery.stopping.wait(15):
            reason = self.failure_reason()
            if reason:
                logger.critical('HEARTBEAT RECOVERY reason=%s; exiting for host restart', reason)
                self.delivery.terminate(1)
                return

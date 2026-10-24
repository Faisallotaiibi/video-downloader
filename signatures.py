"""Per-user signatures, backed by existing storage and local FFmpeg only."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from PIL import Image, ImageOps
from telebot import types

logger = logging.getLogger(__name__)
SIGNATURE_LIMIT = 2 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024
POSITIONS = {'br': 'أسفل يمين', 'bl': 'أسفل يسار', 'tr': 'أعلى يمين',
             'tl': 'أعلى يسار', 'cc': 'الوسط'}
SIZES = {'s': ('صغير', .12), 'm': ('متوسط', .18), 'l': ('كبير', .25)}


class SignatureStorage:
    """Optional-feature failures never change the usage logger's DB state."""
    def __init__(self, path, remote=None):
        self.path, self.remote = path, remote
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='signature-db') if remote else None

    def remote_execute(self, sql, params):
        future = self.executor.submit(self.remote.execute, sql, params)
        try:
            return future.result(timeout=8)
        except TimeoutError:
            future.cancel()
            raise

    def execute(self, sql, params=()):
        if self.remote:
            return self.remote_execute(sql, params)
        with sqlite3.connect(self.path, timeout=3) as connection:
            connection.execute(sql, params)

    def query(self, sql, params=()):
        if self.remote:
            return [row.asdict() for row in self.remote_execute(sql, params).rows]
        with sqlite3.connect(self.path, timeout=3) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params).fetchall()]


def process(command, timeout=180):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('المقطع احتاج معالجة أطول من المتاح جرّب مقطع أقصر') from None
    if result.returncode:
        # Never include Telegram URLs, file contents, or executable output in chat.
        logger.warning('Signature FFmpeg failed: %s', result.stderr[-600:].decode(errors='replace'))
        raise ValueError('تعذرت معالجة هذا الملف والأصل متاح بدون توقيع')
    return result


def normalize_signature(source, target, ffmpeg):
    """Decode actual content; small normalized RGBA assets bound encoder memory."""
    if not 0 < os.path.getsize(source) <= SIGNATURE_LIMIT:
        raise ValueError('حجم التوقيع لازم يكون أقل من ٢ ميغابايت')
    try:
        with Image.open(source) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError('أبعاد التوقيع كبيرة ارفعه بحجم أصغر')
            if not getattr(image, 'is_animated', False):
                image = ImageOps.exif_transpose(image).convert('RGBA')
                image.thumbnail((512, 512))
                image.save(target + '.png')
                return target + '.png'
            if image.n_frames > 60:
                raise ValueError('ارفع توقيع GIF خفيف وقصير حتى ٦ ثواني')
            frames, durations = [], []
            elapsed = 0
            for index in range(image.n_frames):
                image.seek(index)
                frame = image.convert('RGBA')
                frame.thumbnail((256, 256))
                duration = max(20, image.info.get('duration', 100))
                frames.append(frame)
                durations.append(duration)
                elapsed += duration
                if elapsed > 6000:
                    raise ValueError('ارفع توقيع GIF قصير حتى ٦ ثواني')
            frames[0].save(target + '.gif', save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, disposal=2)
            return target + '.gif'
    except (OSError, SyntaxError):
        # Telegram converts GIF animations to MP4. Accept that representation
        # only if FFmpeg can decode it, keeping the same size/duration bounds.
        if Path(source).read_bytes()[:12][4:8] != b'ftyp':
            raise ValueError('ارفع صورة PNG أو توقيع GIF صالح') from None
        output = target + '.gif'
        process([ffmpeg, '-nostdin', '-v', 'error', '-y', '-threads', '1',
                 '-protocol_whitelist', 'file,pipe', '-i', source,
                 '-t', '7', '-vf', 'fps=10,scale=256:256:force_original_aspect_ratio=decrease',
                 '-threads', '1', output], timeout=30)
        if not os.path.isfile(output) or not os.path.getsize(output):
            raise ValueError('ارفع صورة PNG أو توقيع GIF صالح')
        with Image.open(output) as animation:
            if animation.n_frames > 60:
                raise ValueError('ارفع توقيع GIF قصير حتى ٦ ثواني')
        return output


def render_signature(source, signature, output, ffmpeg, width, height,
                     position='br', size='m', moving=True):
    if position not in POSITIONS or size not in SIZES:
        raise ValueError('إعداد التوقيع غير صالح')
    if not width or not height or min(width, height) <= 0:
        raise ValueError('تعذر قراءة أبعاد المقطع')
    # Bound long edge, frame rate, encoder threads, wall time and output size.
    ratio = min(1, 1280 / max(width, height))
    out_width = max(2, int(width * ratio) // 2 * 2)
    out_height = max(2, int(height * ratio) // 2 * 2)
    box_width = max(8, round(out_width * SIZES[size][1]))
    box_height = max(8, round(out_height * .20))
    margin = max(8, round(min(out_width, out_height) * .025))
    drift = max(4, round(out_width * .035)) if moving else 0
    if position == 'cc':
        x = f'(W-w)/2+{drift}*sin(t*0.9)'
        y = '(H-h)/2'
    else:
        x = (f'W-w-{margin}-{drift}*(1+sin(t*0.9))/2' if position.endswith('r')
             else f'{margin}+{drift}*(1+sin(t*0.9))/2')
        y = f'H-h-{margin}' if position.startswith('b') else str(margin)
    loop = ['-stream_loop', '-1'] if signature.endswith('.gif') else ['-loop', '1']
    graph = (
        f'[0:v]scale={out_width}:{out_height},setsar=1,fps=30[base];'
        f'[1:v]scale={box_width}:{box_height}:force_original_aspect_ratio=decrease,'
        f'format=rgba[mark];[base][mark]overlay=x=\'{x}\':y=\'{y}\':'
        'shortest=1:eval=frame:format=auto,format=yuv420p[out]'
    )
    process([ffmpeg, '-nostdin', '-v', 'error', '-y', '-threads', '1', '-i', source,
             *loop, '-threads', '1', '-i', signature, '-filter_complex_threads', '1',
             '-filter_complex', graph, '-map', '[out]', '-map', '0:a?',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '25', '-threads', '1',
             '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
             '-fs', str(VIDEO_LIMIT), output])
    if not os.path.isfile(output) or not os.path.getsize(output):
        raise ValueError('تعذر تجهيز النسخة الموقعة والأصل متاح')
    if os.path.getsize(output) >= VIDEO_LIMIT - 65536:
        raise ValueError('النسخة الموقعة تجاوزت الحجم المسموح والأصل متاح')
    return out_width, out_height


class SignatureService:
    def __init__(self, bot, execute, query, ffmpeg, fetch_video):
        self.bot, self.execute, self.query = bot, execute, query
        self.ffmpeg, self.fetch_video = ffmpeg, fetch_video
        self.enabled = False
        self.lock = threading.Lock()
        self.pending = {}
        self.active_users = set()
        self.slots = threading.BoundedSemaphore(2)
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='signature')
        self.controls = ThreadPoolExecutor(max_workers=1, thread_name_prefix='signature-controls')
        self.control_slots = threading.BoundedSemaphore(4)

    def schema(self):
        self.execute('CREATE TABLE IF NOT EXISTS user_signatures ('
                     'user_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, '
                     "position TEXT NOT NULL DEFAULT 'br', size TEXT NOT NULL DEFAULT 'm', "
                     'moving INTEGER NOT NULL DEFAULT 1)')
        self.execute('CREATE TABLE IF NOT EXISTS signature_requests ('
                     'token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, '
                     'payload TEXT NOT NULL, expires REAL NOT NULL)')
        self.execute('CREATE INDEX IF NOT EXISTS signature_expiry ON signature_requests(expires)')

    def settings(self, user_id):
        rows = self.query('SELECT * FROM user_signatures WHERE user_id=?', (user_id,))
        return rows[0] if rows else None

    def menu_markup(self, user_id, settings=None):
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        def button(text, action):
            return types.InlineKeyboardButton(text, callback_data=f'sig:{user_id}:{action}')
        def selected(text, current):
            return ('✓ ' if current else '') + text
        keyboard.row(button('رفع أو تغيير توقيعي', 'upload'))
        for left, right in [('br', 'bl'), ('tr', 'tl')]:
            keyboard.row(*(button(selected(POSITIONS[key], bool(settings and settings['position'] == key)),
                                  'pos-' + key) for key in (left, right)))
        keyboard.row(button(selected('الوسط', bool(settings and settings['position'] == 'cc')), 'pos-cc'))
        keyboard.row(*(button(selected(SIZES[key][0], bool(settings and settings['size'] == key)),
                             'size-' + key) for key in SIZES))
        movement = 'الحركة مفعلة' if settings and settings['moving'] else 'الحركة متوقفة'
        keyboard.row(button(movement, 'motion'), button('حذف توقيعي', 'delete'))
        keyboard.row(button('تم', 'done'))
        return keyboard

    def menu_text(self, settings):
        if not settings:
            return 'ارفع توقيعك PNG أو GIF كملف بحجم أقل من ٢ ميغابايت\nتوقيع GIF يكون قصير حتى ٦ ثواني'
        return (f"توقيعك محفوظ\nالمكان {POSITIONS.get(settings['position'], 'أسفل يمين')}"
                f"\nالحجم {SIZES.get(settings['size'], SIZES['m'])[0]}"
                f"\nالحركة {'مفعلة' if settings['moving'] else 'ثابتة'}\n\nاضغط تم بعد اختيار إعداداتك")

    def edit_panel(self, call, text, markup):
        try:
            self.bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                       reply_markup=markup)
        except Exception as error:
            # Selecting an already-selected position/size is a successful no-op.
            if getattr(error, 'error_code', None) == 400 and 'message is not modified' in getattr(error, 'description', '').lower():
                return
            raise

    def menu(self, message):
        if not self.enabled:
            self.bot.reply_to(message, 'ميزة التوقيع تجهز الآن والتحميل المعتاد شغال')
            return
        try:
            settings = self.settings(message.from_user.id)
            if not settings:
                with self.lock:
                    self.pending[(message.chat.id, message.from_user.id)] = time.monotonic() + 600
            self.bot.reply_to(message, self.menu_text(settings),
                              reply_markup=self.menu_markup(message.from_user.id, settings))
        except Exception as error:
            logger.warning('Signature menu unavailable: %s', type(error).__name__)
            self.bot.reply_to(message, 'تعذر فتح إعدادات التوقيع حاليًا والتحميل المعتاد شغال')

    def download_telegram(self, file_id, target, limit):
        remote = self.bot.get_file(file_id)
        if remote.file_size and remote.file_size > limit:
            raise ValueError('حجم الملف أكبر من المسموح')
        data = self.bot.download_file(remote.file_path)
        if not 0 < len(data) <= limit:
            raise ValueError('حجم الملف أكبر من المسموح')
        Path(target).write_bytes(data)
        return target

    def upload(self, message):
        key = (message.chat.id, message.from_user.id)
        with self.lock:
            pending = self.pending.get(key, 0) > time.monotonic()
        if not self.enabled or not pending:
            self.bot.reply_to(message, 'اكتب توقيعي أول عشان ترفع أو تغير توقيعك')
            return
        try:
            media = message.document or message.animation or (message.photo[-1] if message.photo else None)
            if not media or (media.file_size and media.file_size > SIGNATURE_LIMIT):
                raise ValueError('ارفع توقيع PNG أو GIF بحجم أقل من ٢ ميغابايت')
            with tempfile.TemporaryDirectory(prefix='signature-upload-') as folder:
                source = self.download_telegram(media.file_id, os.path.join(folder, 'input'), SIGNATURE_LIMIT)
                normalize_signature(source, os.path.join(folder, 'normalized'), self.ffmpeg)
            # Keep only Telegram's reusable file ID in the existing database.
            self.execute('INSERT INTO user_signatures(user_id,file_id) VALUES (?,?) '
                         'ON CONFLICT(user_id) DO UPDATE SET file_id=excluded.file_id',
                         (message.from_user.id, media.file_id))
            settings = self.settings(message.from_user.id)
            if not settings:
                raise ValueError('تعذر حفظ التوقيع حاول مرة ثانية')
            with self.lock:
                self.pending.pop(key, None)
            self.bot.reply_to(message, 'حفظت توقيعك الحين أرسل رابط مقطع واضغط تحميل بتوقيعي',
                              reply_markup=self.menu_markup(message.from_user.id, settings))
        except Exception as error:
            logger.warning('Signature upload failed: %s', type(error).__name__)
            self.bot.reply_to(message, str(error) if isinstance(error, ValueError)
                              else 'تعذر حفظ التوقيع ارفعه كملف PNG أو GIF')

    def configure(self, call):
        if not self.enabled or not call.message:
            self.bot.answer_callback_query(call.id, 'ميزة التوقيع تجهز الآن')
            return
        parts = call.data.split(':', 2)
        if len(parts) != 3 or parts[1] != str(call.from_user.id):
            self.bot.answer_callback_query(call.id, 'هذه إعدادات مستخدم آخر')
            return
        action, user_id = parts[2], call.from_user.id
        self.bot.answer_callback_query(call.id)
        try:
            if action == 'done':
                settings = self.settings(user_id)
                with self.lock:
                    self.pending.pop((call.message.chat.id, user_id), None)
                text = ('تم حفظ إعدادات توقيعك\nارجع للمقطع واضغط تحميل بتوقيعي' if settings
                        else 'لرفع توقيعك اكتب توقيعي')
                self.edit_panel(call, text, types.InlineKeyboardMarkup())
                return
            if action == 'upload':
                with self.lock:
                    # Bound abandoned upload prompts without retaining messages.
                    now = time.monotonic()
                    self.pending = {k: v for k, v in self.pending.items() if v > now}
                    self.pending[(call.message.chat.id, user_id)] = now + 600
                text = 'ارفع توقيعك الآن PNG أو GIF كملف أقل من ٢ ميغابايت\nتوقيع GIF يكون قصير حتى ٦ ثواني'
            elif action == 'delete':
                self.execute('DELETE FROM user_signatures WHERE user_id=?', (user_id,))
                text = 'حذفت توقيعك'
            else:
                settings = self.settings(user_id)
                if not settings:
                    raise ValueError('ارفع توقيعك أول')
                if action.startswith('pos-') and action[4:] in POSITIONS:
                    if settings['position'] != action[4:]:
                        self.execute('UPDATE user_signatures SET position=? WHERE user_id=?', (action[4:], user_id))
                    text = 'مكان توقيعك ' + POSITIONS[action[4:]]
                elif action.startswith('size-') and action[5:] in SIZES:
                    if settings['size'] != action[5:]:
                        self.execute('UPDATE user_signatures SET size=? WHERE user_id=?', (action[5:], user_id))
                    text = 'حجم توقيعك ' + SIZES[action[5:]][0]
                elif action == 'motion':
                    self.execute('UPDATE user_signatures SET moving=? WHERE user_id=?', (int(not settings['moving']), user_id))
                    text = 'حركة التوقيع ' + ('مفعلة' if not settings['moving'] else 'ثابتة')
                else:
                    raise ValueError('خيار غير صالح')
            settings = self.settings(user_id)
            panel_text = text if action in ('upload', 'delete') else self.menu_text(settings)
            self.edit_panel(call, panel_text, self.menu_markup(user_id, settings))
        except Exception as error:
            logger.warning('Signature settings failed: %s', type(error).__name__)
            try:
                self.bot.answer_callback_query(call.id, str(error) if isinstance(error, ValueError)
                                               else 'تعذر تحديث الإعدادات حاول بعد شوي', show_alert=True)
            except Exception:
                pass

    def attach(self, sent, url, user_id, original_markup):
        if not self.enabled or not sent or not sent.video:
            return
        try:
            token = uuid.uuid4().hex
            payload = dict(file_id=sent.video.file_id, file_size=sent.video.file_size,
                           duration=sent.video.duration, width=sent.video.width,
                           height=sent.video.height, url=url)
            self.execute('DELETE FROM signature_requests WHERE expires<?', (time.time(),))
            self.execute('INSERT INTO signature_requests VALUES (?,?,?,?,?)',
                         (token, user_id, sent.chat.id, json.dumps(payload), time.time() + 21600))
            original_markup.row(
                types.InlineKeyboardButton('تحميل بتوقيعي', callback_data='mark:yes:' + token),
                types.InlineKeyboardButton('تحميل بدون توقيع', callback_data='mark:no:' + token))
            self.bot.edit_message_reply_markup(sent.chat.id, sent.message_id, reply_markup=original_markup)
        except Exception as error:
            # A storage or button-edit failure must never fail a delivered video.
            logger.warning('Signature buttons unavailable: %s', type(error).__name__)

    def schedule_attach(self, sent, url, user_id, original_markup):
        # Database timeouts or a slow Telegram edit must not hold the bot's
        # ordinary download threads. Skip optional controls if this queue fills.
        if not self.enabled or not self.control_slots.acquire(blocking=False):
            return
        def attach_controls():
            try:
                self.attach(sent, url, user_id, original_markup)
            finally:
                self.control_slots.release()
        try:
            self.controls.submit(attach_controls)
        except Exception:
            self.control_slots.release()

    def request(self, call):
        if not self.enabled or not call.message:
            self.bot.answer_callback_query(call.id, 'ميزة التوقيع تجهز الآن')
            return
        parts = call.data.split(':', 2)
        if len(parts) != 3 or parts[1] not in ('yes', 'no'):
            return
        try:
            rows = self.query('SELECT * FROM signature_requests WHERE token=? AND user_id=? '
                              'AND chat_id=? AND expires>?',
                              (parts[2], call.from_user.id, call.message.chat.id, time.time()))
            if not rows:
                raise ValueError('انتهت صلاحية الزر أو الطلب لمستخدم آخر أرسل الرابط من جديد')
            payload = json.loads(rows[0]['payload'])
            if parts[1] == 'no':
                self.bot.answer_callback_query(call.id)
                self.bot.send_video(call.message.chat.id, payload['file_id'],
                                    supports_streaming=True, caption='تفضل المقطع بدون توقيع')
                return
            settings = self.settings(call.from_user.id)
            if not settings:
                self.bot.answer_callback_query(call.id, 'اكتب توقيعي وارفع توقيعك أول')
                return
            with self.lock:
                if call.from_user.id in self.active_users:
                    self.bot.answer_callback_query(call.id, 'توقيع مقطعك قيد التجهيز')
                    return
                if not self.slots.acquire(blocking=False):
                    self.bot.answer_callback_query(call.id, 'التوقيع مشغول بطلبين جرّب بعد شوي')
                    return
                self.active_users.add(call.from_user.id)
            try:
                self.bot.answer_callback_query(call.id, 'جاري تجهيز نسختك الموقعة')
                self.worker.submit(self.deliver, call.message.chat.id, call.from_user.id, payload, dict(settings))
            except Exception:
                self.release(call.from_user.id)
                raise
        except Exception as error:
            logger.warning('Signature action failed: %s', type(error).__name__)
            self.bot.answer_callback_query(call.id, str(error) if isinstance(error, ValueError)
                                          else 'تعذر تجهيز الطلب والأصل متاح')

    def release(self, user_id):
        with self.lock:
            self.active_users.discard(user_id)
        self.slots.release()

    def deliver(self, chat_id, user_id, payload, settings):
        try:
            self.bot.send_message(chat_id, 'جاري إضافة توقيعك على نسخة من المقطع')
            with tempfile.TemporaryDirectory(prefix='signed-video-') as folder:
                signature_source = self.download_telegram(settings['file_id'], os.path.join(folder, 'signature.input'), SIGNATURE_LIMIT)
                signature = normalize_signature(signature_source, os.path.join(folder, 'mark'), self.ffmpeg)
                source = None
                # getFile supports downloads up to 20 MB. Larger originals are
                # fetched through the existing downloader, never a paid API.
                if (payload.get('file_size') or VIDEO_LIMIT) <= 20 * 1024 * 1024:
                    try:
                        source = self.download_telegram(payload['file_id'], os.path.join(folder, 'source.mp4'), VIDEO_LIMIT)
                    except Exception:
                        pass
                if not source:
                    source = self.fetch_video(payload['url'], folder)
                output = os.path.join(folder, 'signed.mp4')
                width, height = render_signature(source, signature, output, self.ffmpeg,
                                                 payload['width'], payload['height'],
                                                 settings['position'], settings['size'], bool(settings['moving']))
                with open(output, 'rb') as video:
                    self.bot.send_video(chat_id, video, caption='تفضل المقطع بتوقيعك',
                                        duration=payload.get('duration'), width=width, height=height,
                                        supports_streaming=True, timeout=120)
            logger.info('Signed video delivered successfully')
        except Exception as error:
            logger.warning('Signed video failed: %s', type(error).__name__)
            try:
                self.bot.send_message(chat_id, str(error) if isinstance(error, ValueError)
                                      else 'تعذرت إضافة التوقيع والمقطع الأصلي متاح بدون توقيع')
            except Exception:
                pass
        finally:
            self.release(user_id)

    def register(self):
        self.bot.register_message_handler(self.menu, commands=['signature'])
        self.bot.register_message_handler(self.menu, func=lambda m: (m.text or '').strip() == 'توقيعي', content_types=['text'])
        self.bot.register_message_handler(self.upload, content_types=['photo', 'document', 'animation'])
        self.bot.register_callback_query_handler(self.configure, func=lambda c: bool(c.data and c.data.startswith('sig:')))
        self.bot.register_callback_query_handler(self.request, func=lambda c: bool(c.data and c.data.startswith('mark:')))

    def activate(self):
        try:
            self.schema()
            self.query('SELECT user_id FROM user_signatures LIMIT 0')
            self.query('SELECT token FROM signature_requests LIMIT 0')
            result = encoder_self_test(self.ffmpeg)
            self.enabled = True
            logger.info('SIGNATURE_CHECK SUCCESS %s', result)
        except Exception as error:
            logger.error('SIGNATURE_CHECK FAILED %s; ordinary downloads remain available', type(error).__name__)


def encoder_self_test(ffmpeg):
    """Actual PNG movement/GIF encode check before enabling the feature."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='signature-self-test-') as folder:
        source = os.path.join(folder, 'source.mp4')
        process([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=720x1280:r=30:d=2',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', '-c:v', 'libx264',
                 '-preset', 'ultrafast', '-threads', '1', '-c:a', 'aac', source], timeout=30)
        png = os.path.join(folder, 'logo.png')
        Image.new('RGBA', (120, 60), (255, 0, 0, 160)).save(png)
        gif = os.path.join(folder, 'logo.gif')
        Image.new('RGB', (120, 60), 'red').save(gif, save_all=True,
            append_images=[Image.new('RGB', (120, 60), 'green')], duration=[300, 300], loop=0)
        for logo in (png, gif):
            output = os.path.join(folder, 'output.mp4')
            render_signature(source, logo, output, ffmpeg, 720, 1280)
            process([ffmpeg, '-v', 'error', '-i', output, '-map', '0:v:0', '-map', '0:a:0',
                     '-f', 'null', '-'], timeout=30)
        return f'PNG+GIF audio+video OK seconds={time.monotonic() - started:.1f}'

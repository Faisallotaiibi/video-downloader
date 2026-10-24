import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

import imageio_ffmpeg
from PIL import Image, ImageChops, ImageDraw
from telebot import types

import signatures as sig

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


class Storage:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        self.db.execute(sql, params)
        self.db.commit()

    def query(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]


def make_video(path, width=320, height=240, audio=True, duration=1.6):
    command = [FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i',
               f'color=c=blue:s={width}x{height}:r=30:d={duration}']
    if audio:
        command += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}']
    command += ['-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1']
    if audio:
        command += ['-c:a', 'aac']
    sig.process(command + [str(path)], timeout=30)


def frame(path, second):
    result = sig.process([FFMPEG, '-v', 'error', '-ss', str(second), '-i', str(path),
                          '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
    return Image.open(io.BytesIO(result.stdout)).convert('RGB')


def red_box(image):
    r, g, b = image.split()
    mask = ImageChops.multiply(r.point(lambda v: 255 if v > 120 else 0),
                              g.point(lambda v: 255 if v < 90 else 0))
    return ImageChops.multiply(mask, b.point(lambda v: 255 if v < 150 else 0)).getbbox()


class EncoderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.source = self.root / 'source.mp4'
        make_video(self.source)
        self.logo = self.root / 'logo.png'
        image = Image.new('RGBA', (120, 60), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((10, 10, 110, 50), fill=(255, 0, 0, 192))
        image.save(self.logo)

    def tearDown(self):
        self.folder.cleanup()

    def render(self, **options):
        output = self.root / 'signed.mp4'
        sig.render_signature(str(self.source), str(self.logo), str(output), FFMPEG, 320, 240, **options)
        return output

    def test_png_visible_transparent_and_moving_without_changing_original(self):
        original_hash = hashlib.sha256(self.source.read_bytes()).digest()
        output = self.render()
        first, second = frame(output, .1), frame(output, 1.3)
        first_box, second_box = red_box(first), red_box(second)
        self.assertIsNotNone(first_box)
        self.assertIsNotNone(second_box)
        self.assertGreater(abs(first_box[0] - second_box[0]), 1)
        self.assertGreater(first.getpixel((10, 10))[2], 220)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), original_hash)
        sig.process([FFMPEG, '-v', 'error', '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])

    def test_gif_animation_loops(self):
        self.logo = self.root / 'logo.gif'
        Image.new('RGB', (120, 60), 'red').save(self.logo, save_all=True,
            append_images=[Image.new('RGB', (120, 60), 'green')], duration=[300, 300], loop=0)
        output = self.render(moving=False)
        self.assertIsNotNone(red_box(frame(output, .1)))
        self.assertIsNone(red_box(frame(output, .4)))
        self.assertIsNotNone(red_box(frame(output, .8)))

    def test_muted_video_supported(self):
        make_video(self.source, audio=False)
        output = self.render(moving=False)
        self.assertIsNotNone(red_box(frame(output, .4)))

    def test_positions_and_sizes_stay_inside_video(self):
        for position in sig.POSITIONS:
            with self.subTest(position=position):
                output = self.render(position=position, size='l', moving=False)
                box = red_box(frame(output, .2))
                self.assertIsNotNone(box)
                self.assertGreaterEqual(box[0], 0)
                self.assertLessEqual(box[2], 320)
                self.assertGreaterEqual(box[1], 0)
                self.assertLessEqual(box[3], 240)
                if position.startswith('b'):
                    self.assertGreater(box[1], 120)
                if position.startswith('t'):
                    self.assertLess(box[3], 120)

    def test_portrait_and_landscape_hd_are_bounded(self):
        for width, height, expected in [(1080, 1920, (720, 1280)), (1920, 1080, (1280, 720))]:
            with self.subTest(width=width):
                make_video(self.source, width, height, duration=.4)
                output = self.root / 'hd.mp4'
                dimensions = sig.render_signature(str(self.source), str(self.logo), str(output),
                                                  FFMPEG, width, height)
                self.assertEqual(dimensions, expected)
                self.assertEqual(frame(output, .1).size, expected)

    def test_invalid_settings_rejected(self):
        with self.assertRaises(ValueError):
            self.render(position='anything;movie=http://example.test')

    def test_timeout_keeps_original(self):
        with patch.object(sig.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 180)):
            with self.assertRaisesRegex(ValueError, 'معالجة'):
                self.render()
        self.assertTrue(self.source.exists())

    def test_png_gif_and_telegram_animation_normalization(self):
        normalized = sig.normalize_signature(str(self.logo), str(self.root / 'normalized'), FFMPEG)
        with Image.open(normalized) as image:
            self.assertEqual(image.mode, 'RGBA')
        animation = self.root / 'animation.mp4'
        make_video(animation, audio=False, duration=.4)
        converted = sig.normalize_signature(str(animation), str(self.root / 'animation'), FFMPEG)
        self.assertTrue(converted.endswith('.gif'))
        with Image.open(converted) as image:
            self.assertLessEqual(max(image.size), 256)

    def test_bad_and_oversized_uploads_rejected(self):
        source = self.root / 'bad.input'
        source.write_text('#EXTM3U\nhttp://localhost/private')
        with self.assertRaises(ValueError), patch.object(sig, 'process') as run:
            sig.normalize_signature(str(source), str(self.root / 'bad'), FFMPEG)
        run.assert_not_called()
        source.write_bytes(b'x' * (sig.SIGNATURE_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, 'حجم'):
            sig.normalize_signature(str(source), str(self.root / 'big'), FFMPEG)

    def test_long_gif_is_rejected_instead_of_silently_truncated(self):
        logo = self.root / 'long.gif'
        Image.new('RGB', (50, 50), 'red').save(logo, save_all=True,
            append_images=[Image.new('RGB', (50, 50), 'green')], duration=[4000, 4000], loop=0)
        with self.assertRaisesRegex(ValueError, '٦ ثواني'):
            sig.normalize_signature(str(logo), str(self.root / 'long-normalized'), FFMPEG)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.bot = MagicMock()
        self.fetch = MagicMock()
        self.service = sig.SignatureService(self.bot, self.storage.execute, self.storage.query, FFMPEG, self.fetch)
        self.service.schema()
        self.service.enabled = True
        self.storage.execute('INSERT INTO user_signatures(user_id,file_id) VALUES (?,?)', (1, 'signature-one'))
        self.storage.execute('INSERT INTO user_signatures(user_id,file_id) VALUES (?,?)', (2, 'signature-two'))

    def tearDown(self):
        self.service.worker.shutdown(wait=True)
        self.service.controls.shutdown(wait=True)
        self.storage.db.close()

    def token(self, user=1, chat=5):
        sent = NS(video=NS(file_id='original-video', file_size=10, duration=1, width=320, height=240),
                  chat=NS(id=chat), message_id=10)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton('تحميل الصوت', callback_data='audio:test'))
        self.service.attach(sent, 'https://example.test/video', user, keyboard)
        self.assertEqual(keyboard.keyboard[0][0].callback_data, 'audio:test')
        return keyboard.keyboard[1][0].callback_data.split(':')[2]

    def call(self, token, user=1, chat=5, action='yes'):
        return NS(id='callback', data=f'mark:{action}:{token}', from_user=NS(id=user), message=NS(chat=NS(id=chat)))

    def test_settings_persist_across_service_restart_and_are_isolated(self):
        self.storage.execute("UPDATE user_signatures SET position='tl',size='l',moving=0 WHERE user_id=1")
        restarted = sig.SignatureService(self.bot, self.storage.execute, self.storage.query, FFMPEG, self.fetch)
        self.assertEqual(restarted.settings(1)['position'], 'tl')
        self.assertEqual(restarted.settings(2)['position'], 'br')
        self.assertNotEqual(restarted.settings(1)['file_id'], restarted.settings(2)['file_id'])
        restarted.worker.shutdown(wait=True)
        restarted.controls.shutdown(wait=True)

    def test_upload_replaces_only_requesting_users_signature_and_keeps_settings(self):
        image = io.BytesIO()
        Image.new('RGBA', (80, 40), (255, 0, 0, 100)).save(image, 'PNG')
        self.bot.get_file.return_value = NS(file_size=len(image.getvalue()), file_path='new-signature')
        self.bot.download_file.return_value = image.getvalue()
        self.storage.execute("UPDATE user_signatures SET position='tl' WHERE user_id=1")
        self.service.pending[(5, 1)] = time.monotonic() + 600
        message = NS(chat=NS(id=5), from_user=NS(id=1),
                     document=NS(file_id='new-signature', file_size=len(image.getvalue())),
                     animation=None, photo=None)
        self.service.upload(message)
        self.assertEqual(self.service.settings(1)['file_id'], 'new-signature')
        self.assertEqual(self.service.settings(1)['position'], 'tl')
        self.assertEqual(self.service.settings(2)['file_id'], 'signature-two')
        self.assertNotIn((5, 1), self.service.pending)

    def test_upload_requires_explicit_prompt(self):
        message = NS(chat=NS(id=5), from_user=NS(id=1))
        self.service.upload(message)
        self.bot.get_file.assert_not_called()
        self.assertEqual(self.service.settings(1)['file_id'], 'signature-one')

    def test_optional_controls_have_bounded_separate_queue(self):
        with patch.object(self.service.controls, 'submit') as submit:
            for _ in range(10):
                self.service.schedule_attach(None, 'url', 1, None)
        self.assertEqual(submit.call_count, 4)

    def test_remote_storage_failure_does_not_switch_to_local(self):
        remote = MagicMock()
        remote.execute.side_effect = RuntimeError('temporary network failure')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'local.db')
            storage = sig.SignatureStorage(str(path), remote=remote)
            with self.assertRaises(RuntimeError):
                storage.execute('CREATE TABLE optional_feature(id INTEGER)')
            self.assertIs(storage.remote, remote)
            self.assertFalse(path.exists())
            storage.executor.shutdown(wait=True)

    def test_local_storage_survives_new_storage_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder, 'signatures.db'))
            storage = sig.SignatureStorage(path)
            storage.execute('CREATE TABLE sample (user_id INTEGER, file_id TEXT)')
            storage.execute('INSERT INTO sample VALUES (?,?)', (1, 'file-one'))
            restarted = sig.SignatureStorage(path)
            self.assertEqual(restarted.query('SELECT * FROM sample')[0]['file_id'], 'file-one')

    def test_original_download_uses_telegram_file_without_processing(self):
        token = self.token()
        with patch.object(sig, 'render_signature') as encode:
            self.service.request(self.call(token, action='no'))
        self.bot.send_video.assert_called_once()
        self.assertEqual(self.bot.send_video.call_args.args[1], 'original-video')
        encode.assert_not_called()
        self.fetch.assert_not_called()

    def test_request_rejects_other_user_chat_and_expired_token(self):
        token = self.token()
        for user, chat in [(2, 5), (1, 6)]:
            self.service.request(self.call(token, user, chat))
        self.storage.execute('UPDATE signature_requests SET expires=0')
        self.service.request(self.call(token))
        self.bot.send_video.assert_not_called()
        self.assertEqual(self.bot.answer_callback_query.call_count, 3)

    def test_settings_callback_cannot_modify_other_user(self):
        call = NS(id='c', data='sig:1:pos-tl', from_user=NS(id=2), message=NS(chat=NS(id=5)))
        self.service.configure(call)
        self.assertEqual(self.service.settings(1)['position'], 'br')

    def test_duplicate_user_and_global_queue_are_bounded(self):
        with patch.object(self.service.worker, 'submit') as submit:
            one, two, three = self.token(1), self.token(2), self.token(3)
            self.storage.execute('INSERT INTO user_signatures(user_id,file_id) VALUES (3,?)', ('three',))
            self.service.request(self.call(one))
            self.service.request(self.call(one))
            self.service.request(self.call(two, user=2))
            self.service.request(self.call(three, user=3))
            self.assertEqual(submit.call_count, 2)
        self.service.release(1)
        self.service.release(2)

    def test_database_failure_cannot_fail_delivered_original(self):
        self.service.execute = MagicMock(side_effect=RuntimeError('db unavailable'))
        self.service.attach(NS(video=NS(file_id='v', file_size=10, duration=1, width=2, height=2),
                               chat=NS(id=5)), 'url', 1, types.InlineKeyboardMarkup())
        self.bot.edit_message_reply_markup.assert_not_called()

    def test_signed_delivery_cleans_files_and_preserves_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source.mp4'
            make_video(source)
            logo = root / 'signature.png'
            Image.new('RGBA', (120, 60), (255, 0, 0, 180)).save(logo)
            self.bot.get_file.side_effect = lambda key: NS(file_size=100, file_path=key)
            self.bot.download_file.side_effect = lambda key: logo.read_bytes() if key == 'signature-one' else source.read_bytes()
            captured = []
            def send(chat, video, **kwargs):
                captured.append(video.name)
                copied = root / 'result.mp4'
                copied.write_bytes(video.read())
                sig.process([FFMPEG, '-v', 'error', '-i', str(copied), '-map', '0:a:0', '-f', 'null', '-'])
            self.bot.send_video.side_effect = send
            self.service.slots.acquire()
            self.service.active_users.add(1)
            self.service.deliver(5, 1, dict(file_id='original',file_size=100,width=320,height=240,duration=1,url='url'),
                                 self.service.settings(1))
            self.assertEqual(len(captured), 1)
            self.assertFalse(os.path.exists(captured[0]))
            self.assertNotIn(1, self.service.active_users)
            self.fetch.assert_not_called()

    def test_failure_releases_queue_and_cleans_temp_files(self):
        files = []
        def download(key, target, limit):
            files.append(target)
            Path(target).write_bytes(b'invalid')
            return target
        with patch.object(self.service, 'download_telegram', side_effect=download):
            self.service.slots.acquire()
            self.service.active_users.add(1)
            self.service.deliver(5, 1, {'url': 'url'}, self.service.settings(1))
        self.assertNotIn(1, self.service.active_users)
        self.assertTrue(all(not os.path.exists(path) for path in files))
        self.assertTrue(self.service.slots.acquire(blocking=False))

    def test_encoder_failure_disables_signature_only(self):
        self.service.enabled = False
        with patch.object(sig, 'encoder_self_test', side_effect=RuntimeError('encoder failed')):
            self.service.activate()
        self.assertFalse(self.service.enabled)


if __name__ == '__main__':
    unittest.main()

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import media_support as media


class MediaTests(unittest.TestCase):
    def test_cdn_connection_reset_is_retried_and_partial_file_removed(self):
        import requests
        with TemporaryDirectory() as folder:
            target = str(Path(folder, 'photo.jpg'))
            def fail(url, filename, limit):
                Path(filename).write_bytes(b'partial')
                raise requests.ConnectionError('reset')
            def succeed(url, filename, limit):
                self.assertFalse(os.path.exists(filename))
                Path(filename).write_bytes(b'complete')
                return filename
            with patch.object(media, '_download_asset_once', side_effect=[fail, succeed]) as fetch:
                # Callable side effects need explicit dispatch through a sequence.
                fetch.side_effect = lambda *args: (fail if fetch.call_count == 1 else succeed)(*args)
                with patch.object(media.time, 'sleep'):
                    media.download_asset('https://p16.tiktokcdn.com/photo', target, 100)
            self.assertEqual(Path(target).read_bytes(), b'complete')
            self.assertEqual(fetch.call_count, 2)

    def test_permanent_cdn_error_is_not_retried(self):
        import requests
        response = requests.Response()
        response.status_code = 403
        with TemporaryDirectory() as folder, \
             patch.object(media, '_download_asset_once', side_effect=requests.HTTPError(response=response)) as fetch:
            with self.assertRaises(requests.HTTPError):
                media.download_asset('https://p16.tiktokcdn.com/photo', str(Path(folder, 'p')), 100)
        self.assertEqual(fetch.call_count, 1)

    def test_album_resolver_falls_back_from_blocked_get_to_browser_post(self):
        blocked = MagicMock(status_code=403)
        blocked.raise_for_status.side_effect = RuntimeError('blocked')
        success = MagicMock(status_code=200)
        success.json.return_value = {'code': 0, 'data': {
            'images': ['https://p16.tiktokcdn.com/one.jpg'],
            'music': 'https://v16.tiktokcdn.com/audio.mp3'}}
        with patch.object(media.browser_requests, 'get', side_effect=RuntimeError('embed blocked')), \
             patch.object(media.browser_requests, 'request', side_effect=[blocked, success]) as fetch:
            album = media.tiktok_album('https://www.tiktok.com/@test/photo/123')
        self.assertEqual(len(album['images']), 1)
        self.assertEqual(fetch.call_args_list[0].args[0], 'GET')
        self.assertEqual(fetch.call_args_list[1].args[0], 'POST')
        self.assertEqual(fetch.call_args.kwargs['impersonate'], 'chrome')
        self.assertNotIn('cookies', fetch.call_args.kwargs)

    def test_embed_extracts_all_photos_and_sound_for_exact_post(self):
        import json
        payload = {'source': {'data': {'/embed/v2/123': {'videoData': {
            'itemInfos': {'id': '123'},
            'imagePostInfo': {'displayImages': [
                {'urlList': ['https://p16.tiktokcdn.com/one.jpeg']},
                {'urlList': ['https://p16.tiktokcdn.com/two.jpeg']} ]},
            'musicInfos': {'playUrl': ['https://v16.tiktokcdn.com/audio.mp3']},
        }}}}}
        html = '<script id="__FRONTITY_CONNECT_STATE__" type="application/json">' + json.dumps(payload) + '</script>'
        album = media._web_album(html, expected_id='123')
        self.assertEqual(len(album['images']), 2)
        self.assertTrue(album['music'].endswith('audio.mp3'))
        with self.assertRaises(ValueError):
            media._web_album(html, expected_id='999')

    def test_embed_success_does_not_use_external_resolver(self):
        response = MagicMock(status_code=200, text='html')
        with patch.object(media.browser_requests, 'get', return_value=response), \
             patch.object(media, '_web_album', return_value={'images': ['photo'], 'music': None}), \
             patch.object(media.browser_requests, 'request') as fallback:
            media.tiktok_album('https://www.tiktok.com/@test/photo/123')
        fallback.assert_not_called()

    def test_production_check_disabled_when_unconfigured_or_expired(self):
        with patch.dict(os.environ, {'MEDIA_CHECK_URL': 'https://vt.tiktok.com/test/',
                                     'MEDIA_CHECK_UNTIL': '0'}), \
             patch.object(media, 'tiktok_album') as album:
            media.run_configured_media_check('ffmpeg')
        album.assert_not_called()

    def test_photo_detection_and_host_validation(self):
        url = 'https://vt.tiktok.com/example/'
        resolved = 'https://www.tiktok.com/@example/photo/123456'
        self.assertEqual(media.photo_post_url(url, 'Unsupported URL: ' + resolved), resolved)
        self.assertEqual(media.photo_post_url(resolved), resolved)
        self.assertIsNone(media.photo_post_url('https://tiktok.com.attacker.test/photo/123'))
        self.assertIsNone(media.photo_post_url(url, 'Connection reset'))
        self.assertIsNone(media._media_url('https://localhost/file.jpg'))
        self.assertIsNone(media._media_url('https://tiktokcdn.com.attacker.test/file.jpg'))
        self.assertIsNone(media._media_url('file:///etc/passwd'))

    def test_album_batches_preserve_order_and_cleanup(self):
        bot = MagicMock()
        received = []
        paths = []
        def prepare(url, filename):
            Path(filename).write_bytes(url.encode())
            paths.append(filename)
            return filename, False
        def group(chat, items, **kwargs):
            self.assertLessEqual(len(items), 10)
            received.extend(item.media.read().decode() for item in items)
        def photo(chat, source, **kwargs):
            received.append(source.read().decode())
        bot.send_media_group.side_effect = group
        bot.send_photo.side_effect = photo
        images = [str(i) for i in range(21)]
        with patch.object(media, 'prepare_photo', side_effect=prepare):
            media.send_album(bot, 1, {'images': images}, 'caption')
        self.assertEqual(received, images)
        self.assertEqual(bot.send_media_group.call_count, 2)
        self.assertEqual(bot.send_photo.call_count, 1)
        self.assertTrue(all(not os.path.exists(p) for p in paths))

    def test_album_cleanup_on_upload_failure(self):
        bot = MagicMock()
        bot.send_photo.side_effect = RuntimeError('upload failed')
        paths = []
        def prepare(url, filename):
            Path(filename).write_bytes(b'photo')
            paths.append(filename)
            return filename, False
        with patch.object(media, 'prepare_photo', side_effect=prepare):
            with self.assertRaises(RuntimeError):
                media.send_album(bot, 1, {'images': ['one']}, 'caption')
        self.assertTrue(all(not os.path.exists(p) for p in paths))

    def test_photo_normalization(self):
        with TemporaryDirectory() as folder:
            source = Path(folder, 'webp.image')
            Image.new('RGBA', (100, 100), (255, 0, 0, 0)).save(source, 'WEBP')
            with patch.object(media, 'download_asset'):
                result, document = media.prepare_photo('unused', str(source))
            self.assertFalse(document)
            with Image.open(result) as image:
                self.assertEqual(image.format, 'JPEG')
                self.assertEqual(image.mode, 'RGB')

    def test_buttons_bind_owner_block_duplicates_and_expire(self):
        buttons = media.AudioButtons()
        markup = buttons.keyboard('https://example.test/video', 1, 2)
        key = markup.keyboard[0][0].callback_data.split(':')[1]
        self.assertIsNone(buttons.claim(key, 1, 3))
        self.assertEqual(buttons.claim(key, 1, 2), 'https://example.test/video')
        self.assertIs(buttons.claim(key, 1, 2), False)
        buttons.release(key)
        self.assertEqual(buttons.claim(key, 1, 2), 'https://example.test/video')
        with patch.object(media.time, 'monotonic', return_value=10**12):
            self.assertIsNone(buttons.claim(key, 1, 2))

    def test_actual_bytes_limit_even_without_content_length(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.is_redirect = False
        response.headers = {}
        response.iter_content.return_value = [b'12', b'345']
        with TemporaryDirectory() as folder, patch.object(media.requests, 'get', return_value=response):
            with self.assertRaisesRegex(ValueError, 'حجم'):
                media.download_asset('https://p16.tiktokcdn.com/photo', str(Path(folder, 'p')), 4)

    def test_audio_only_selector_and_local_mp3_conversion(self):
        import imageio_ffmpeg
        import subprocess
        with TemporaryDirectory() as folder:
            source = str(Path(folder, 'source.wav'))
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=0.2', source],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            def extract(opts, url, download):
                self.assertEqual(opts['format'], 'bestaudio/best')
                ydl = MagicMock()
                ydl.prepare_filename.return_value = source
                return ydl, {'ext': 'wav'}
            output = media.download_audio('https://example.test/video', folder,
                                          imageio_ffmpeg.get_ffmpeg_exe(), extract, {})
            self.assertGreater(os.path.getsize(output), 0)
            self.assertTrue(output.endswith('.mp3'))

    def test_photo_audio_uses_album_sound(self):
        with TemporaryDirectory() as folder, \
             patch.object(media, 'tiktok_album', return_value={'music': 'https://p16.tiktokcdn.com/audio'}), \
             patch.object(media, 'download_asset') as asset, \
             patch.object(media, 'make_mp3', return_value='audio.mp3'):
            source = str(Path(folder, 'source.audio'))
            Path(source).write_bytes(b'audio')
            asset.return_value = source
            extractor = MagicMock()
            media.download_audio('https://www.tiktok.com/@test/photo/123', folder, 'ffmpeg', extractor, {})
            extractor.assert_not_called()
            asset.assert_called_once()


class HandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {'BOT_TOKEN': '123456:unit-test-token',
                                     'TURSO_DATABASE_URL': '', 'TURSO_AUTH_TOKEN': ''}):
            import main
        cls.main = main

    def setUp(self):
        self.message = SimpleNamespace(
            text='https://vt.tiktok.com/test/',
            chat=SimpleNamespace(id=1, type='private'),
            from_user=SimpleNamespace(id=2, username='test', first_name='Test',
                                      last_name=None, language_code='ar'))

    def test_short_photo_post_routes_to_album(self):
        main = self.main
        error = RuntimeError('Unsupported URL: https://www.tiktok.com/@test/photo/123')
        with patch.object(main, 'bot'), patch.object(main, 'log_usage') as log, \
             patch.object(main, 'extract_info_with_retry', side_effect=error), \
             patch.object(main, 'send_photos') as photos:
            main.download(self.message)
        photos.assert_called_once_with(self.message, 'https://www.tiktok.com/@test/photo/123')
        self.assertEqual(log.call_args.args[3], 'success')

    def test_normal_video_path_and_audio_button(self):
        main = self.main
        with TemporaryDirectory() as folder:
            source = str(Path(folder, 'video.mp4'))
            Path(source).write_bytes(b'video')
            with patch.object(main, 'bot') as bot, patch.object(main, 'log_usage'), \
                 patch.object(main, 'extract_info_with_retry', return_value=(MagicMock(), {})), \
                 patch.object(main, 'select_downloaded_file', return_value=source), \
                 patch.object(main, 'probe_video_metadata', return_value=(1, 100, 100, 'h264', 'yuv420p', 'aac')), \
                 patch.object(main, 'ensure_playable', return_value=source), \
                 patch.object(main, 'send_photos') as photos:
                main.download(self.message)
                photos.assert_not_called()
                bot.send_video.assert_called_once()
                self.assertIsNotNone(bot.send_video.call_args.kwargs['reply_markup'])
            self.assertFalse(os.path.exists(source))

    def test_arabic_audio_request(self):
        self.message.text = 'صوت https://vt.tiktok.com/test/'
        with patch.object(self.main, 'send_audio') as audio:
            self.main.download(self.message)
        audio.assert_called_once_with(self.message, 'https://vt.tiktok.com/test/')

    def test_callback_logs_clicking_user_and_cleans_up(self):
        main = self.main
        paths = []
        def audio(url, folder, *args):
            source = str(Path(folder, 'audio.mp3'))
            Path(source).write_bytes(b'mp3')
            paths.append(source)
            return source
        bot_message = SimpleNamespace(chat=self.message.chat,
                                      from_user=SimpleNamespace(id=999, username='bot'))
        with patch.object(main, 'bot') as bot, patch.object(main, 'log_usage') as log, \
             patch.object(main, 'download_audio', side_effect=audio):
            main.send_audio(bot_message, 'https://example.test/video', self.message.from_user)
        bot.send_audio.assert_called_once()
        self.assertEqual(log.call_args.kwargs['telegram_id'], 2)
        self.assertTrue(all(not os.path.exists(p) for p in paths))


if __name__ == '__main__':
    unittest.main()

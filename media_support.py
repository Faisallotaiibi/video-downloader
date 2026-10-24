"""Photo albums and optional MP3 downloads, separate from the video path."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid

from PIL import Image, ImageOps
from curl_cffi import requests as browser_requests
import requests
from telebot import types

logger = logging.getLogger(__name__)
PHOTO_LIMIT = 10 * 1024 * 1024
FILE_LIMIT = 50 * 1024 * 1024
MEDIA_HOSTS = ('tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokv.com',
               'byteoversea.com', 'ibytedtos.com', 'muscdn.com', 'tikwm.com')
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.tiktok.com/'}


def is_tiktok(url):
    host = (urlparse(url).hostname or '').lower()
    return host == 'tiktok.com' or host.endswith('.tiktok.com')


def photo_post_url(url, error=''):
    """Recognize yt-dlp's resolved photo URL without slowing normal videos."""
    if not is_tiktok(url):
        return None
    if re.search(r'/photo/\d+', urlparse(url).path):
        return url
    match = re.search(r'https?://[^\s\x1b<>\"\']+/photo/\d+', error)
    return match.group(0) if match and is_tiktok(match.group(0)) else None


def _media_url(value):
    if isinstance(value, dict):
        value = value.get('urlList') or value.get('url_list') or []
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, str)), None)
    if not isinstance(value, str):
        return None
    if value.startswith('/'):
        value = 'https://www.tikwm.com' + value
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme == 'https' and not parsed.username and any(
        host == domain or host.endswith('.' + domain) for domain in MEDIA_HOSTS
    ):
        return value
    return None


def _web_album(html, expected_id=None):
    """Support TikTok's two public hydration formats; never use thumbnails."""
    for match in re.finditer(r'<script\b[^>]*\bid=[\"\'](?:__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE|__FRONTITY_CONNECT_STATE__)[\"\'][^>]*>(.*?)</script>', html, re.S):
        try:
            state = json.loads(match.group(1))
        except ValueError:
            continue
        # TikTok's embed page includes the complete photo post even when
        # the ordinary /photo/ page omits its hydration payload for servers.
        for entry in state.get('source', {}).get('data', {}).values():
            video = entry.get('videoData') or {}
            if expected_id and str(video.get('itemInfos', {}).get('id')) != expected_id:
                continue
            photos = video.get('imagePostInfo', {}).get('displayImages', [])
            if photos:
                images = [_media_url(p.get('urlList')) for p in photos]
                if not all(images):
                    raise ValueError('تعذر العثور على جميع صور المنشور')
                return {'images': images, 'music': _media_url(video.get('musicInfos', {}).get('playUrl'))}
        scope = state.get('__DEFAULT_SCOPE__', {})
        item = scope.get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct', {})
        items = [item] if item else list(state.get('ItemModule', {}).values())
        for item in items:
            photos = item.get('imagePost', {}).get('images', [])
            if photos:
                images = [_media_url(p.get('imageURL', {})) for p in photos]
                if not all(images):
                    raise ValueError('تعذر العثور على جميع صور المنشور')
                return {'images': images, 'music': _media_url(item.get('music', {}).get('playUrl'))}
    raise ValueError('تعذر الوصول إلى صور المنشور حاول مرة ثانية')


def tiktok_album(url):
    if not is_tiktok(url):
        raise ValueError('رابط تيك توك غير صالح')
    post_id = re.search(r'/(?:photo|video)/(\d+)', urlparse(url).path)
    if post_id:
        try:
            response = browser_requests.get(
                'https://www.tiktok.com/embed/v2/' + post_id.group(1),
                impersonate='chrome', timeout=20,
            )
            response.raise_for_status()
            album = _web_album(response.text, expected_id=post_id.group(1))
            logger.info('TikTok album resolved directly from embed: %s images', len(album['images']))
            return album
        except Exception as error:
            logger.warning('TikTok embed extraction failed: %s', type(error).__name__)
    # yt-dlp does not support TikTok photo posts. This public, keyless
    # resolver is used only for albums; no cookies or credentials are sent.
    # Render's datacenter traffic can receive an HTML anti-bot response to
    # ordinary requests. Use the already-installed browser TLS transport,
    # and try a POST if the GET is rejected. Never forward session cookies.
    for method, endpoint in [('GET', 'https://www.tikwm.com/api/'),
                             ('POST', 'https://www.tikwm.com/api/'),
                             ('GET', 'https://tikwm.com/api/')]:
        status = None
        try:
            options = {'params' if method == 'GET' else 'data': {'url': url}}
            response = browser_requests.request(
                method, endpoint, impersonate='chrome', timeout=20,
                headers={'Accept': 'application/json', 'Referer': 'https://www.tikwm.com/'},
                **options,
            )
            status = response.status_code
            response.raise_for_status()
            payload = response.json()
            data = payload.get('data') or {}
            photos = data.get('images') or []
            if payload.get('code') == 0 and photos:
                images = [_media_url(p) for p in photos]
                if not all(images):
                    raise ValueError('تعذر العثور على جميع صور المنشور')
                logger.info('TikTok album resolved via %s %s: %s images', method,
                            urlparse(endpoint).hostname, len(images))
                return {'images': images, 'music': _media_url(data.get('music'))}
            logger.warning('TikTok album resolver %s returned HTTP %s code=%s images=%s',
                           method, status, payload.get('code'), len(photos))
        except Exception as error:
            # Status/class are enough to diagnose blocking without logging
            # response bodies, signed CDN URLs, or any credentials.
            logger.warning('TikTok album resolver %s %s failed: HTTP %s %s',
                           method, urlparse(endpoint).hostname, status, type(error).__name__)
    response = browser_requests.get(url, impersonate='chrome', timeout=25)
    response.raise_for_status()
    return _web_album(response.text)


def download_asset(url, filename, limit):
    """Retry transient CDN resets without ever handing off partial files."""
    for attempt in range(3):
        try:
            return _download_asset_once(url, filename, limit)
        except requests.RequestException as error:
            response = getattr(error, 'response', None)
            status = response.status_code if response is not None else None
            retryable = status in (408, 429) or (status is not None and status >= 500)
            retryable = retryable or isinstance(error, (
                requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ))
            if os.path.exists(filename):
                os.remove(filename)
            if not retryable or attempt == 2:
                raise
            logger.warning('Transient media CDN failure %s HTTP %s; retrying attempt %s/3',
                           type(error).__name__, status, attempt + 2)
            time.sleep(.5 * (attempt + 1))


def _download_asset_once(url, filename, limit):
    """Bound both advertised and actual size and validate CDN redirects."""
    if not _media_url(url):
        raise ValueError('رابط الوسائط غير صالح')
    for _ in range(5):
        with requests.get(url, headers=HEADERS, timeout=(10, 30), stream=True,
                          allow_redirects=False) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get('Location', ''))
                if not _media_url(url):
                    raise ValueError('رابط الوسائط غير صالح')
                continue
            response.raise_for_status()
            if int(response.headers.get('Content-Length') or 0) > limit:
                raise ValueError('حجم الملف أكبر من الحد المسموح')
            total = 0
            with open(filename, 'wb') as output:
                for chunk in response.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError('حجم الملف أكبر من الحد المسموح')
                    output.write(chunk)
            if not total:
                raise ValueError('ملف الوسائط فارغ')
            return filename
    raise ValueError('تعذر الوصول إلى ملف الوسائط')


def prepare_photo(url, filename):
    download_asset(url, filename, PHOTO_LIMIT)
    with Image.open(filename) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((4000, 4000))
        # Telegram rejects extremely long panoramas; send those as files.
        as_document = max(image.size) / min(image.size) > 20
        image.save(filename, 'JPEG', quality=95)
    if os.path.getsize(filename) > PHOTO_LIMIT:
        raise ValueError('حجم الصورة أكبر من الحد المسموح')
    return filename, as_document


def send_album(bot, chat_id, album, caption):
    """At most ten files on disk/in an album, ordered as in the source post."""
    images = album['images']
    if not images or len(images) > 100:
        raise ValueError('عدد صور المنشور غير مدعوم')
    for offset in range(0, len(images), 10):
        with tempfile.TemporaryDirectory(prefix='bot-photos-') as folder:
            jobs = [(url, os.path.join(folder, f'{i + offset + 1:03}.jpg'))
                    for i, url in enumerate(images[offset:offset + 10])]
            with ThreadPoolExecutor(max_workers=4) as pool:
                files = list(pool.map(lambda job: prepare_photo(*job), jobs))
            # Separate incompatible aspect ratios without reordering images.
            with ExitStack() as stack:
                pending = []
                def flush():
                    if len(pending) == 1:
                        item = pending[0]
                        bot.send_photo(chat_id, item.media, caption=item.caption, timeout=120)
                    elif pending:
                        bot.send_media_group(chat_id, pending, timeout=120)
                    pending.clear()
                for i, (filename, as_document) in enumerate(files):
                    handle = stack.enter_context(open(filename, 'rb'))
                    text = caption if offset == 0 and i == 0 else None
                    if as_document:
                        flush()
                        bot.send_document(chat_id, handle, caption=text, timeout=120)
                    else:
                        pending.append(types.InputMediaPhoto(handle, caption=text))
                flush()


def make_mp3(source, output, ffmpeg):
    result = subprocess.run(
        [ffmpeg, '-nostdin', '-y', '-i', source, '-map', '0:a:0', '-vn',
         '-c:a', 'libmp3lame', '-b:a', '192k', '-fs', str(FILE_LIMIT), output],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120,
    )
    if result.returncode or not os.path.exists(output) or not os.path.getsize(output):
        raise ValueError('ما لقيت مسار صوت صالح في هذا الرابط')
    if os.path.getsize(output) >= FILE_LIMIT - 65536:
        raise ValueError('الملف أكبر من 50MB')
    return output


def download_audio(url, folder, ffmpeg, extract_info, cookie_opts):
    source = os.path.join(folder, 'source.%(ext)s')
    options = {
        'format': 'bestaudio/best', 'noplaylist': True, 'outtmpl': source,
        'max_filesize': FILE_LIMIT, 'ffmpeg_location': ffmpeg,
        'socket_timeout': 30, 'retries': 3, 'fragment_retries': 3,
        **cookie_opts,
    }
    if photo_post_url(url):
        album = tiktok_album(url)
        if not album.get('music'):
            raise ValueError('منشور الصور هذا ما فيه صوت متاح')
        source = download_asset(album['music'], os.path.join(folder, 'source.audio'), FILE_LIMIT)
    else:
        try:
            ydl, info = extract_info(options, url, download=True)
            if info.get('entries') is not None:
                raise ValueError('أرسل رابط مقطع واحد لتحميل صوته')
            source = ydl.prepare_filename(info)
        except Exception as error:
            photo_url = photo_post_url(url, str(error))
            if not photo_url:
                raise
            album = tiktok_album(photo_url)
            if not album.get('music'):
                raise ValueError('منشور الصور هذا ما فيه صوت متاح')
            source = download_asset(album['music'], os.path.join(folder, 'source.audio'), FILE_LIMIT)
    if not os.path.isfile(source):
        raise ValueError('تعذر تنزيل ملف الصوت')
    return make_mp3(source, os.path.join(folder, 'audio.mp3'), ffmpeg)


def run_configured_media_check(ffmpeg):
    """Opt-in, time-limited production network check; never sends messages."""
    url = os.environ.get('MEDIA_CHECK_URL', '')
    try:
        until = float(os.environ.get('MEDIA_CHECK_UNTIL', '0'))
    except ValueError:
        return
    if not is_tiktok(url) or not 0 < until - time.time() <= 3600:
        return
    logger.info('MEDIA_CHECK started')
    try:
        album = tiktok_album(url)
        count = len(album['images'])
        with tempfile.TemporaryDirectory(prefix='bot-media-check-') as folder:
            for index, image in enumerate(album['images']):
                filename, _ = prepare_photo(image, os.path.join(folder, 'photo.jpg'))
                os.remove(filename)
                logger.info('MEDIA_CHECK photo %s/%s OK', index + 1, count)
            if album.get('music'):
                source = download_asset(album['music'], os.path.join(folder, 'source.audio'), FILE_LIMIT)
                output = make_mp3(source, os.path.join(folder, 'audio.mp3'), ffmpeg)
                logger.info('MEDIA_CHECK MP3 OK bytes=%s', os.path.getsize(output))
        logger.info('MEDIA_CHECK SUCCESS photos=%s audio=%s', count, bool(album.get('music')))
    except Exception as error:
        logger.error('MEDIA_CHECK FAILED %s: %s', type(error).__name__, str(error)[:200])


class AudioButtons:
    """Bounded, expiring actions; stale buttons can be replaced by resending."""
    def __init__(self):
        self.actions = OrderedDict()
        self.active = set()
        self.lock = threading.Lock()

    def keyboard(self, url, chat_id, user_id):
        key = uuid.uuid4().hex
        with self.lock:
            self.actions[key] = (url, chat_id, user_id, time.monotonic())
            while len(self.actions) > 2000:
                self.actions.popitem(last=False)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('تحميل الصوت 🎵', callback_data='audio:' + key))
        return markup

    def claim(self, key, chat_id, user_id):
        with self.lock:
            action = self.actions.get(key)
            if not action or action[1:3] != (chat_id, user_id) or time.monotonic() - action[3] > 86400:
                return None
            if key in self.active:
                return False
            self.active.add(key)
            return action[0]

    def release(self, key):
        with self.lock:
            self.active.discard(key)

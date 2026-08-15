import concurrent.futures
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

import imageio_ffmpeg
import requests
import telebot
import yt_dlp
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", "https://video-downloader-0ea4.onrender.com")
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot API upload limit

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

# Render's free tier wipes local disk on every deploy, so usage history is
# stored in Turso (SQLite-compatible, persists independently of Render)
# whenever it's configured; otherwise it falls back to the local file, which
# is fine for local development but won't survive a redeploy.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)

# A hung Turso connection (network issue, bad credentials, etc.) must never
# block the bot itself - it only ever gets TURSO_TIMEOUT seconds on a
# background thread before we give up and fall back to local storage.
TURSO_TIMEOUT = 10
_turso_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4) if USE_TURSO else None


def _run_with_timeout(func, *args, **kwargs):
    return _turso_executor.submit(func, *args, **kwargs).result(timeout=TURSO_TIMEOUT)


if USE_TURSO:
    try:
        import libsql_client
        # The libsql:// scheme defaults to a WebSocket connection, which
        # fails its handshake from Render's network (confirmed via logs:
        # WSServerHandshakeError 400 on wss://...turso.io). The https://
        # scheme talks to the exact same database over plain HTTP instead.
        turso_http_url = TURSO_DATABASE_URL.replace("libsql://", "https://", 1)
        _turso_client = _run_with_timeout(
            libsql_client.create_client_sync, url=turso_http_url, auth_token=TURSO_AUTH_TOKEN
        )
    except Exception:
        logger.exception(
            "Failed to connect to Turso within %ss - falling back to local (non-persistent) storage",
            TURSO_TIMEOUT,
        )
        USE_TURSO = False

if not USE_TURSO:
    logger.warning(
        "Usage history is stored locally and will be lost on the next deploy. "
        "Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN for persistent storage."
    )


def _disable_turso(action):
    global USE_TURSO
    logger.exception(
        "Turso %s failed/timed out - switching to local storage for the rest of this run", action
    )
    USE_TURSO = False


def db_execute(sql, params=()):
    """Run an INSERT/CREATE TABLE statement against whichever backend is active.

    Any Turso failure (timeout, network error, bad query, etc.) permanently
    falls back to local storage for the rest of the process instead of
    raising - a flaky remote database must never crash the bot.
    """
    if USE_TURSO:
        try:
            _run_with_timeout(_turso_client.execute, sql, params)
            return
        except Exception:
            _disable_turso("write")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def db_query(sql, params=()):
    """Run a SELECT and return a list of dict rows, regardless of backend.

    Same fallback behavior as db_execute: a broken Turso connection
    degrades to local storage instead of raising.
    """
    if USE_TURSO:
        try:
            result = _run_with_timeout(_turso_client.execute, sql, params)
            return [row.asdict() for row in result.rows]
        except Exception:
            _disable_turso("read")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# YouTube's web player extraction occasionally breaks upstream ("Failed to
# extract any player response"); falling back to the android/ios clients
# works around most of these outages until yt-dlp ships a fix.
YOUTUBE_EXTRACTOR_ARGS = {'youtube': {'player_client': ['android', 'ios', 'web']}}

# Render's server IP is a recognized datacenter address, and YouTube/
# Instagram/X increasingly rate-limit or outright block requests from
# those (confirmed in logs: YouTube's "sign in to confirm you're not a
# bot", Instagram "empty media response", Twitter "Video is unavailable"
# - all on content that downloads instantly from residential IPs). A
# cookies.txt (Netscape format) from a real logged-in session makes
# outbound requests look like a real browser instead. Uploaded via
# Render's "Secret Files" (not a regular env var - the file can be
# several KB and env vars aren't meant for that), which mounts it at
# /etc/secrets/<filename> and persists across deploys. Entirely
# optional: every request just runs cookie-less, as before, if it's
# not there.
SECRET_COOKIES_FILE = "/etc/secrets/cookies.txt"
# yt-dlp writes updated cookies back to the file on every run (sites
# rotate session tokens), but Secret Files are mounted read-only
# (confirmed in logs: "OSError: [Errno 30] Read-only file system:
# '/etc/secrets/cookies.txt'", which killed the request entirely even
# though extraction itself had already succeeded). Work from a writable
# copy in /tmp instead - yt-dlp can update that copy freely.
COOKIES_FILE = "/tmp/cookies.txt"
if os.path.exists(SECRET_COOKIES_FILE):
    shutil.copyfile(SECRET_COOKIES_FILE, COOKIES_FILE)
    logger.info("Using cookies from %s (writable copy of %s)", COOKIES_FILE, SECRET_COOKIES_FILE)
else:
    logger.info(
        "No cookies file at %s - requests are unauthenticated and more likely "
        "to be rate-limited by YouTube/Instagram/X", SECRET_COOKIES_FILE,
    )


def cookie_opts_for(url):
    """cookiefile kwarg for yt-dlp, withheld for YouTube.

    Confirmed live (Render logs, 2026-08-15): once a cookiefile is passed,
    yt-dlp skips the android/ios clients outright ("does not support
    cookies" - those clients don't send cookies at all, by yt-dlp's own
    design) and falls through to the web client alone. The web client needs
    an n-challenge/nsig JS solver we don't have installed, so every request
    then failed with "n challenge solving failed" -> "Only images are
    available for download". android/ios already worked reliably for
    YouTube without cookies (see YOUTUBE_EXTRACTOR_ARGS above), so cookies
    would only ever narrow YouTube down to the one client that's broken
    here. Twitter/X and Instagram have no such client-selection fallout, so
    they keep using the cookie file.
    """
    if detect_platform(url) == "YouTube":
        return {}
    return {'cookiefile': COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}

# Many sites (Reddit, etc.) serve video and audio as separate streams that
# need muxing; the OS ffmpeg isn't installed on Render, so use the static
# binary bundled by imageio-ffmpeg instead.
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
_DIMENSIONS_RE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')
_VCODEC_RE = re.compile(r'Video:\s*(\w+)')
_PIXFMT_RE = re.compile(r'Video:[^,]*,\s*([a-z0-9]+)')
_ACODEC_RE = re.compile(r'Audio:\s*(\w+)')

# 8-bit 4:2:0 variants that phone hardware decoders handle natively.
# yuvj420p is the full-range flavor of yuv420p - identical compatibility,
# and treating it as incompatible sent perfectly fine h264 files through
# the minutes-long re-encode path for nothing.
COMPATIBLE_PIX_FMTS = ('yuv420p', 'yuvj420p')


def is_stream_compatible(vcodec, pix_fmt):
    return vcodec in ('h264', 'hevc') and pix_fmt in COMPATIBLE_PIX_FMTS


def probe_video_metadata(filepath):
    """Read duration/width/height/codec/pixel-format from the file via ffmpeg.

    Some sites (e.g. Instagram) don't always give yt-dlp a duration in the
    extracted info, which makes Telegram's client show a broken 0:00/frozen
    player. ffmpeg -i prints this straight from the file's own container
    metadata regardless of what the source site reported. The pixel format
    matters as much as the codec: phone hardware decoders only handle 8-bit
    4:2:0 (yuv420p) - an h264 stream in yuv444p or 10-bit still shows up as
    frozen-picture-with-audio on iOS.
    """
    try:
        result = subprocess.run(
            [FFMPEG_PATH, '-i', filepath],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        )
        output = result.stderr
        duration = None
        m = _DURATION_RE.search(output)
        if m:
            hours, minutes, seconds = m.groups()
            duration = int(float(hours) * 3600 + float(minutes) * 60 + float(seconds))
        width = height = None
        m2 = _DIMENSIONS_RE.search(output)
        if m2:
            width, height = int(m2.group(1)), int(m2.group(2))
        vcodec = None
        m3 = _VCODEC_RE.search(output)
        if m3:
            vcodec = m3.group(1).lower()
        pix_fmt = None
        m4 = _PIXFMT_RE.search(output)
        if m4:
            pix_fmt = m4.group(1).lower()
        acodec = None
        m5 = _ACODEC_RE.search(output)
        if m5:
            acodec = m5.group(1).lower()
        return duration, width, height, vcodec, pix_fmt, acodec
    except Exception:
        logger.exception("Failed to probe video metadata for %s", filepath)
        return None, None, None, None, None, None


def remux_clean(filepath):
    """Losslessly rewrite the container: stream-copy remux, ~0.02s measured.

    Fixes the delivery problems that don't need a re-encode: a non-zero
    start timestamp / edit list (freezes strict players on the first frame
    while audio runs), the moov index sitting at the end of the file
    (breaks streaming playback - Telegram streams, it doesn't download
    first), and stray data/subtitle tracks. Keeps only the first video and
    audio stream, zeroes timestamps, and puts the index up front.
    """
    clean_path = f'{filepath}.clean.mp4'
    try:
        result = subprocess.run(
            [
                FFMPEG_PATH, '-y', '-i', filepath,
                '-map', '0:v:0', '-map', '0:a:0?',
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                clean_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        if result.returncode != 0 or not os.path.exists(clean_path):
            logger.error(
                "Remux failed for %s (exit %s): %s",
                filepath, result.returncode, result.stderr.decode(errors='replace')[-1000:],
            )
            return filepath
    except Exception:
        logger.exception("Remux raised an exception for %s", filepath)
        return filepath

    os.remove(filepath)
    return clean_path


def ensure_playable(filepath, vcodec=None, pix_fmt=None, acodec=None):
    """Guarantee an iOS/Telegram-playable file, as cheaply as possible.

    Phone players only hardware-decode H.264/HEVC in 8-bit 4:2:0. When the
    stream already is that (the overwhelmingly common case now that the
    format selector prefers avc1), a lossless 0.02s remux normalizes the
    container quirks that still freeze strict players (non-zero start
    timestamps, moov index at the end of the file). Only when the codec or
    pixel format is genuinely incompatible (VP9/AV1, yuv444p, 10-bit) does
    the expensive re-encode run - memory-light (threads=1, ~112MB peak RSS
    measured vs ~250MB default) because Render's free tier OOM-killed the
    whole process on ffmpeg's defaults, confirmed via crash+restart logs.
    The slow path keeps its output at 960px / 30fps and stream-copies
    already-AAC audio: on the free tier's tiny CPU allowance every saved
    cycle is minutes of user-visible wait, and half the output size also
    halves the Telegram upload.
    """
    if vcodec is None or pix_fmt is None:
        _, _, _, vcodec, pix_fmt, acodec = probe_video_metadata(filepath)
    if is_stream_compatible(vcodec, pix_fmt):
        return remux_clean(filepath)

    logger.info(
        "Incompatible stream (vcodec=%r pix_fmt=%r) in %s - re-encoding",
        vcodec, pix_fmt, filepath,
    )
    audio_args = ['-c:a', 'copy'] if acodec == 'aac' else ['-c:a', 'aac', '-b:a', '128k']
    fixed_path = f'{filepath}.fixed.mp4'
    try:
        result = subprocess.run(
            [
                FFMPEG_PATH, '-y', '-i', filepath,
                '-map', '0:v:0', '-map', '0:a:0?',
                '-vf', "scale='min(960,iw)':'min(960,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30",
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-threads', '1',
                '-pix_fmt', 'yuv420p',
                *audio_args,
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                fixed_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if result.returncode != 0 or not os.path.exists(fixed_path):
            logger.error(
                "Re-encode failed for %s (exit %s): %s",
                filepath, result.returncode, result.stderr.decode(errors='replace')[-2000:],
            )
            return filepath
    except Exception:
        logger.exception("Re-encode raised an exception for %s", filepath)
        return filepath

    os.remove(filepath)
    return fixed_path

# Prefer the best h264 (avc1) stream: it's what Telegram's mobile players
# and iOS's camera roll can actually decode, and nearly every platform
# serves one at full quality - a bare 'bestvideo' would sometimes pick a
# VP9 stream instead, which plays as frozen-picture-with-audio on iOS
# Telegram and can't be saved to the camera roll (confirmed via logs:
# vcodec=vp09 on the affected Instagram reels). Explicit bestvideo+bestaudio
# fallbacks keep separate-stream sites (Reddit) merging when no h264 exists.
FORMAT_SELECTOR = (
    'bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/'
    'bestvideo[vcodec^=avc1]+bestaudio/'
    'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
    'bestvideo+bestaudio/best'
)

# Instagram (reels and posts share one extractor) exposes two format kinds:
# ready-made progressive mp4s (video_versions - single h264 file, direct
# from the CDN, no merging) and DASH fragment streams (separate video/audio
# needing an ffmpeg merge; also where the VP9 variants live). The general
# selector's bestvideo+bestaudio preference always chose the DASH pair
# (confirmed in logs: "Downloading 1 format(s): dash-...v+dash-...a"),
# paying merge time and container-quirk risk for nothing. Preferring any
# combined (video+audio) format grabs the progressive file first - what
# other download bots send, which is why they deliver faster - and falls
# back to the normal chain when no progressive format exists.
INSTAGRAM_FORMAT_SELECTOR = 'best[vcodec!=none][acodec!=none]/' + FORMAT_SELECTOR


def format_selector_for(url):
    host = urlparse(url).netloc.lower()
    if 'instagram' in host:
        return INSTAGRAM_FORMAT_SELECTOR
    return FORMAT_SELECTOR

WELCOME_MESSAGE = (
    "أهلين 👋 أنا مساعدك الخاص لتحميل أي فيديو تحبه\n\n"
    "بس الصق الرابط وأنا أسوي الباقي – بدون علامة مائية طبعًا 😉\n\n"
    "منصاتي المفضلة: 🎵📷🐦👻🔴"
)

NOT_A_URL_MESSAGE = "هذا مو رابط 🤔\n\nأرسل لي رابط الفيديو مباشرة وأنا أتكفل الباقي 🎬"

DOWNLOADING_MESSAGES = [
    "🎬 جاري تجهيز فيديوك...",
    "✨ ثانية وتلقاه...",
    "🚀 شوي وأجهزه لك...",
    "⏳ خلني أشتغل عليه...",
]

SUCCESS_CAPTION = "✅ تفضل! جرّب @The966bot مع أصحابك 😉"

SNAPCHAT_SHORT_LINK_RE = re.compile(r"snapchat\.com/t/", re.IGNORECASE)

SNAPCHAT_SHORT_LINK_MESSAGE = (
    "👻 رابط سناب شات هذا مختصر وما أقدر أفتحه\n\n"
    "افتحه بتطبيق سناب شات، انسخ الرابط الجديد اللي يطلع، وارسله لي 🙌"
)

MAX_FILESIZE_MESSAGE = "📀 واااو! الفيديو هذا ضخم زيادة لتيليجرام 😅\nجرّب واحد أقصر"

NO_VIDEO_FOUND_MESSAGE = "🤔 ما لقيت فيديو بهذا الرابط\nمتأكد انه فيديو مو منشور نص/صورة؟"

YOUTUBE_BLOCKED_MESSAGE = "😅 يوتيوب ما موافق علي الوقت\n🎵 جرّب تيك توك أو 👻 سناب شات بدله"

GENERIC_ERROR_MESSAGE = "😅 صار شي غريب من جهتي! جرّب مرة ثانية ولا جرّب رابط ثاني 🙌"

SLOW_CONVERSION_MESSAGE = "⚙️ هذا المقطع بصيغة خاصة ويحتاج معالجة إضافية — ممكن ياخذ كم دقيقة، اصبر عليّ 🙏"


def friendly_error_message(url, error_text):
    text = error_text.lower()
    if "sign in to confirm" in text or ("youtube" in url.lower() and "player response" in text):
        return YOUTUBE_BLOCKED_MESSAGE
    if "max_filesize" in text or "50mb" in text or "أكبر من 50" in error_text:
        return MAX_FILESIZE_MESSAGE
    if "requested format is not available" in text or "unsupported url" in text:
        return NO_VIDEO_FOUND_MESSAGE
    return GENERIC_ERROR_MESSAGE


bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder=".")


def init_db():
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            requester TEXT,
            url TEXT NOT NULL,
            platform TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )


def detect_platform(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if "tiktok" in host:
        return "TikTok"
    if "instagram" in host:
        return "Instagram"
    if "twitter" in host or host == "x.com":
        return "Twitter/X"
    if "youtube" in host or "youtu.be" in host:
        return "YouTube"
    return host or "غير معروف"


def log_usage(source, requester, url, status, error=None):
    try:
        db_execute(
            "INSERT INTO usage_log (timestamp, source, requester, url, platform, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source,
                requester,
                url,
                detect_platform(url),
                status,
                error,
            ),
        )
    except Exception:
        logger.exception("Failed to log usage")


# Instagram throttles anonymous/burst traffic with errors that clear up on
# their own a few seconds later (confirmed in yt-dlp's own instagram.py:
# "You have exceeded the rate-limit for accessing posts anonymously" and
# "Instagram sent an empty media response") - unlike a real 404 or a
# too-large file, retrying the exact same request shortly after tends to
# just work. Scoped to Instagram only via detect_platform(); every other
# site still gets exactly one attempt, unchanged.
INSTAGRAM_TRANSIENT_ERROR_RE = re.compile(r'rate-limit|empty media response', re.IGNORECASE)
INSTAGRAM_RETRY_ATTEMPTS = 3
INSTAGRAM_RETRY_DELAY = 3


def extract_info_with_retry(ydl_opts, url, download):
    attempts = INSTAGRAM_RETRY_ATTEMPTS if detect_platform(url) == "Instagram" else 1
    delay = INSTAGRAM_RETRY_DELAY
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=download)
                return ydl, info
        except yt_dlp.utils.DownloadError as e:
            if attempt == attempts or not INSTAGRAM_TRANSIENT_ERROR_RE.search(str(e)):
                raise
            logger.info(
                "Instagram request looked rate-limited (attempt %s/%s) for %s - retrying in %ss",
                attempt, attempts, url, delay,
            )
            time.sleep(delay)
            delay *= 2


def keep_alive():
    while True:
        try:
            requests.get(SELF_URL, timeout=10)
        except requests.RequestException:
            logger.warning("keep_alive ping failed", exc_info=True)
        time.sleep(840)


@bot.message_handler(commands=['start'])
def start(message):
    logger.info("Received /start from chat_id=%s", message.chat.id)
    bot.reply_to(message, WELCOME_MESSAGE)


@bot.message_handler(content_types=['text'])
def download(message):
    logger.info("Received message from chat_id=%s: %r", message.chat.id, message.text)
    url = message.text.strip()
    requester = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or str(message.chat.id)
    )

    if not url.lower().startswith(("http://", "https://")):
        bot.reply_to(message, NOT_A_URL_MESSAGE)
        return

    if SNAPCHAT_SHORT_LINK_RE.search(url):
        bot.reply_to(message, SNAPCHAT_SHORT_LINK_MESSAGE)
        return

    bot.reply_to(message, random.choice(DOWNLOADING_MESSAGES))
    filename = None
    try:
        ydl_opts = {
            'format': format_selector_for(url),
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'max_filesize': MAX_TELEGRAM_FILE_SIZE,
            'extractor_args': YOUTUBE_EXTRACTOR_ARGS,
            'ffmpeg_location': FFMPEG_PATH,
            # yt-dlp sets no socket timeout by default, so a stalled CDN
            # connection can hang a worker thread forever; overwrites=True
            # stops it from silently reusing a leftover file from an
            # earlier hung/interrupted attempt at the same path. yt-dlp's
            # default retry counts (10) combined with a 30s socket timeout
            # could still take up to 5 minutes to finally give up on a
            # truly dead connection, so cap retries tighter too.
            'socket_timeout': 30,
            'overwrites': True,
            'retries': 3,
            'fragment_retries': 3,
            **cookie_opts_for(url),
        }
        ydl, info = extract_info_with_retry(ydl_opts, url, download=True)
        filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            raise FileNotFoundError("الملف أكبر من 50MB")

        duration, width, height, vcodec, pix_fmt, acodec = probe_video_metadata(filename)
        if not is_stream_compatible(vcodec, pix_fmt):
            bot.reply_to(message, SLOW_CONVERSION_MESSAGE)
        processed = ensure_playable(filename, vcodec, pix_fmt, acodec)
        if processed != filename:
            filename = processed
            # dimensions may have changed (960px cap) - re-read from the new file
            duration, width, height, vcodec, pix_fmt, acodec = probe_video_metadata(filename)

        if os.path.getsize(filename) > MAX_TELEGRAM_FILE_SIZE:
            raise FileNotFoundError("الملف أكبر من 50MB")

        duration = duration or info.get('duration')
        width = width or info.get('width')
        height = height or info.get('height')
        logger.info(
            "Downloaded %s: duration=%s width=%s height=%s vcodec=%s acodec=%s",
            url, duration, width, height, vcodec, acodec or info.get('acodec'),
        )

        with open(filename, 'rb') as f:
            # Passing duration/width/height explicitly avoids Telegram clients
            # showing a broken 0:00 / static-thumbnail player when they can't
            # cleanly probe the remuxed file's own metadata.
            bot.send_video(
                message.chat.id,
                f,
                caption=SUCCESS_CAPTION,
                duration=int(duration) if duration else None,
                width=width,
                height=height,
                supports_streaming=True,
            )
        log_usage("telegram", requester, url, "success")
    except Exception as e:
        logger.exception("Download failed for url=%s", url)
        bot.reply_to(message, friendly_error_message(url, str(e)))
        log_usage("telegram", requester, url, "failed", str(e))
    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requester = request.remote_addr or "web"

    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "رابط غير صالح"}), 400

    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
            'extractor_args': YOUTUBE_EXTRACTOR_ARGS,
            'ffmpeg_location': FFMPEG_PATH,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            **cookie_opts_for(url),
        }
        _, info = extract_info_with_retry(ydl_opts, url, download=False)

        direct_url = info.get("url")
        if not direct_url and info.get("formats"):
            direct_url = info["formats"][-1].get("url")

        if not direct_url:
            log_usage("web", requester, url, "failed", "no direct url found")
            return jsonify({"error": "تعذر العثور على رابط التحميل"}), 502

        log_usage("web", requester, url, "success")
        return jsonify({"download_url": direct_url})
    except Exception as e:
        logger.exception("API download failed for url=%s", url)
        log_usage("web", requester, url, "failed", str(e))
        return jsonify({"error": str(e)}), 500


def check_dashboard_auth(auth):
    return bool(
        DASHBOARD_PASSWORD
        and auth
        and auth.username == DASHBOARD_USERNAME
        and auth.password == DASHBOARD_PASSWORD
    )


def require_dashboard_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not check_dashboard_auth(request.authorization):
            return Response(
                "الرجاء تسجيل الدخول لعرض لوحة التحكم",
                401,
                {"WWW-Authenticate": 'Basic realm="Dashboard"'},
            )
        return f(*args, **kwargs)

    return wrapper


def dashboard_data():
    total = db_query("SELECT COUNT(*) AS c FROM usage_log")[0]["c"]
    success = db_query("SELECT COUNT(*) AS c FROM usage_log WHERE status = 'success'")[0]["c"]
    by_platform = db_query(
        "SELECT platform, COUNT(*) AS c FROM usage_log GROUP BY platform ORDER BY c DESC"
    )
    daily = db_query(
        "SELECT strftime('%Y-%m-%d', timestamp) AS day, COUNT(*) AS c FROM usage_log "
        "WHERE timestamp >= date('now', '-13 days') GROUP BY day ORDER BY day"
    )
    recent = db_query("SELECT * FROM usage_log ORDER BY id DESC LIMIT 200")
    users = db_query(
        "SELECT requester, COUNT(*) AS c FROM usage_log "
        "WHERE requester IS NOT NULL AND requester != '' "
        "GROUP BY requester ORDER BY c DESC LIMIT 100"
    )

    daily_by_day = {row["day"]: row["c"] for row in daily}
    daily_series = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_series.append({"day": day, "count": daily_by_day.get(day, 0)})

    return {
        "total": total,
        "success": success,
        "failed": total - success,
        "by_platform": [{"platform": r["platform"], "count": r["c"]} for r in by_platform],
        "daily": daily_series,
        "recent": recent,
        "users": [{"requester": r["requester"], "count": r["c"]} for r in users],
        "persistent_storage": USE_TURSO,
    }


@app.route("/api/dashboard-data")
@require_dashboard_auth
def api_dashboard_data():
    return jsonify(dashboard_data())


@app.route("/api/dashboard-data/user-requests")
@require_dashboard_auth
def api_user_requests():
    requester = (request.args.get("name") or "").strip()
    if not requester:
        return jsonify({"error": "missing name"}), 400
    rows = db_query(
        "SELECT * FROM usage_log WHERE requester = ? ORDER BY id DESC LIMIT 500",
        (requester,),
    )
    return jsonify({"requester": requester, "requests": rows})


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>لوحة التحكم</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Almarai:wght@400;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #020617;
  --surface: #0F172A;
  --surface-2: #1E293B;
  --border: #334155;
  --text-primary: #F8FAFC;
  --text-secondary: #94A3B8;
  --text-muted: #8494AB;
  --success: #22C55E;
  --danger: #EF4444;
  --accent: #60A5FA;
  --donut-1: #F472B6;
  --donut-2: #22D3EE;
  --donut-3: #A78BFA;
  --donut-4: #FBBF24;
  --donut-other: #94A3B8;
  --radius: 16px;
  --radius-sm: 10px;
  --safe-top: env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);
}
* { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { overflow-x: hidden; max-width: 100%; }
body {
  font-family: 'Almarai', 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text-primary);
  min-height: 100vh;
  min-height: 100dvh;
  padding-bottom: calc(28px + var(--safe-bottom));
  font-size: 16px;
  line-height: 1.5;
}
.wrap { max-width: 640px; margin: 0 auto; padding: 0 16px; }
a { color: inherit; }
button { font-family: inherit; }

/* ---------- Header ---------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: calc(14px + var(--safe-top)) 16px 14px;
  background: rgba(2, 6, 23, 0.85);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand-icon {
  flex-shrink: 0;
  width: 38px; height: 38px;
  border-radius: 11px;
  background: linear-gradient(160deg, var(--surface-2), var(--surface));
  border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  color: var(--accent);
}
.brand-text { min-width: 0; }
.brand-text h1 { font-size: 15px; font-weight: 800; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.brand-sub { display: block; font-size: 11px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.avatar-menu { position: relative; flex-shrink: 0; }
.avatar-btn {
  width: 44px; height: 44px;
  border-radius: 50%;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text-primary);
  font-weight: 800;
  font-size: 15px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: filter 0.15s;
}
.avatar-btn:hover, .avatar-btn:focus-visible { filter: brightness(1.25); outline: 2px solid var(--accent); outline-offset: 2px; }
.avatar-dropdown {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  min-width: 190px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: 0 16px 40px rgba(0,0,0,0.5);
  padding: 6px;
  z-index: 40;
}
.avatar-dropdown[hidden] { display: none; }
.avatar-dropdown-user {
  padding: 10px 12px 8px;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border);
  margin-bottom: 4px;
}
.avatar-dropdown-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 12px;
  min-height: 44px;
  background: none;
  border: none;
  border-radius: 8px;
  color: var(--danger);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  text-align: right;
}
.avatar-dropdown-item:hover { background: rgba(239, 68, 68, 0.12); }
.avatar-dropdown-item:focus-visible { background: rgba(239, 68, 68, 0.12); outline: 2px solid var(--danger); outline-offset: -2px; }

/* ---------- Layout / cards ---------- */
main { padding-top: 18px; }
.card {
  background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0) 45%), var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.35);
  margin-bottom: 14px;
}
.card h2 { font-size: 13px; font-weight: 700; color: var(--text-secondary); margin-bottom: 14px; }
#loading { color: var(--text-muted); font-size: 13px; padding: 40px 0; text-align: center; }
#content { transition: opacity 0.2s; }
#content.stale { opacity: 0.55; }

@media (prefers-reduced-motion: no-preference) {
  #content.reveal .card {
    animation: cardIn 0.45s cubic-bezier(.2,.8,.2,1) both;
    animation-delay: calc(var(--i, 0) * 0.06s);
  }
  @keyframes cardIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .trend.pulse { animation: trendPop 0.4s ease; }
  @keyframes trendPop {
    0% { transform: scale(1); }
    35% { transform: scale(1.12); }
    100% { transform: scale(1); }
  }
  .line-path { animation: drawLine 0.9s ease forwards; }
  @keyframes drawLine {
    from { stroke-dashoffset: var(--len); }
    to { stroke-dashoffset: 0; }
  }
}

.refresh-note { display: block; font-size: 11px; color: var(--text-muted); margin: -4px 0 14px; }

/* ---------- Stats: hero + horizontal scroll ---------- */
.stats { margin-bottom: 4px; }
.hero-card {
  display: flex;
  align-items: center;
  gap: 14px;
}
.hero-icon {
  flex-shrink: 0;
  width: 52px; height: 52px;
  border-radius: 14px;
  background: rgba(34, 197, 94, 0.14);
  color: var(--success);
  display: flex; align-items: center; justify-content: center;
}
.hero-body { min-width: 0; flex: 1; }
.hero-label { font-size: 13px; color: var(--text-secondary); font-weight: 700; }
.hero-num { font-size: 38px; font-weight: 800; font-variant-numeric: tabular-nums; line-height: 1.15; color: var(--success); }
.trend { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; margin-top: 4px; font-weight: 700; }
.trend.up { color: var(--success); }
.trend.down { color: var(--danger); }
.trend.flat { color: var(--text-muted); }

.stats-scroll {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding: 2px 2px 12px;
  margin: 0 -2px 2px;
  scroll-snap-type: x proximity;
}
.mini-card {
  flex: 0 0 auto;
  scroll-snap-align: start;
  min-width: 130px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mini-icon {
  width: 34px; height: 34px;
  border-radius: 10px;
  background: var(--surface-2);
  color: var(--text-secondary);
  display: flex; align-items: center; justify-content: center;
}
.mini-card.good .mini-icon { background: rgba(34, 197, 94, 0.14); color: var(--success); }
.mini-card.critical .mini-icon { background: rgba(239, 68, 68, 0.14); color: var(--danger); }
.mini-num { font-size: 22px; font-weight: 800; font-variant-numeric: tabular-nums; }
.mini-card.good .mini-num { color: var(--success); }
.mini-card.critical .mini-num { color: var(--danger); }
.mini-label { font-size: 11px; color: var(--text-secondary); font-weight: 700; }

/* ---------- Donut + legend ---------- */
.donut-wrap { display: flex; justify-content: center; margin-bottom: 16px; }
.donut { width: 172px; height: 172px; }
.donut-seg { transition: opacity 0.15s; }
.donut-total { fill: var(--text-primary); font-size: 22px; font-weight: 800; font-family: 'Almarai', sans-serif; }
.donut-total-label { fill: var(--text-muted); font-size: 10px; font-family: 'Almarai', sans-serif; }
.legend { display: flex; flex-direction: column; gap: 2px; }
.legend-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 4px;
  min-height: 44px;
  border-bottom: 1px solid var(--border);
}
.legend-item:last-child { border-bottom: none; }
.legend-dot { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }
.legend-name { flex: 1; min-width: 0; font-size: 13px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend-count { font-size: 12px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.legend-pct { font-size: 13px; font-weight: 800; color: var(--text-secondary); min-width: 42px; text-align: left; direction: ltr; }

/* ---------- Line chart ---------- */
.line-svg { width: 100%; height: 140px; display: block; overflow: visible; }
.line-axis-label { fill: var(--text-muted); font-size: 9px; font-family: 'Almarai', sans-serif; }
.line-point { cursor: pointer; }
.line-point:hover, .line-point:focus { outline: none; }
.line-point:focus circle:last-child { stroke-width: 3; r: 4.5; }
.line-point circle.hit { fill: transparent; }
.tooltip {
  position: fixed;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  pointer-events: none;
  z-index: 50;
  display: none;
  box-shadow: 0 8px 20px rgba(0,0,0,0.4);
}
.tooltip .v { font-weight: 800; color: var(--text-primary); }
.tooltip .d { color: var(--text-secondary); }

/* ---------- Toolbar / filters ---------- */
.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
.toolbar input[type="text"] {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 11px 14px;
  color: var(--text-primary);
  font-size: 14px;
  min-width: 200px;
  flex: 1;
  min-height: 44px;
}
.toolbar input[type="text"]::placeholder { color: var(--text-muted); }
.toolbar select {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0 12px;
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
  min-height: 44px;
  min-width: 44px;
}
.chip-row { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 9px 16px;
  min-height: 44px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
  display: inline-flex;
  align-items: center;
}
.chip.active { background: var(--text-primary); color: #020617; border-color: var(--text-primary); }

/* ---------- Request cards (replaces table) ---------- */
.request-list { display: flex; flex-direction: column; gap: 10px; }
.request-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 13px 14px;
}
.request-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
.platform-badge { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 800; min-width: 0; }
.platform-badge-icon {
  flex-shrink: 0;
  width: 26px; height: 26px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff;
}
.platform-badge-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-pill {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 800;
}
.status-pill.success { background: rgba(34, 197, 94, 0.14); color: var(--success); }
.status-pill.failed { background: rgba(239, 68, 68, 0.14); color: var(--danger); }
.request-url {
  display: block;
  font-size: 12.5px;
  color: var(--accent);
  text-decoration: none;
  direction: ltr;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-bottom: 1px dotted transparent;
  margin-bottom: 8px;
}
.request-url:hover, .request-url:focus-visible { border-bottom-color: var(--accent); outline: none; }
.request-meta { font-size: 11px; color: var(--text-muted); direction: ltr; text-align: right; unicode-bidi: plaintext; }
.empty-state { text-align: center; color: var(--text-muted); font-size: 13px; padding: 30px 10px; }

/* ---------- Users list ---------- */
.users-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  list-style: none;
  min-height: 44px;
}
.users-summary::-webkit-details-marker { display: none; }
.users-summary::marker { content: ''; }
.users-summary h2 { margin-bottom: 0; }
.users-summary-icon { flex-shrink: 0; color: var(--text-secondary); display: flex; transition: transform 0.2s; }
.users-card[open] .users-summary-icon { transform: rotate(180deg); }
.users-card .users-list { margin-top: 14px; }
.users-list { display: flex; flex-direction: column; gap: 2px; }
.user-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 48px;
  padding: 9px 6px;
  background: none;
  border: none;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary);
  font-family: inherit;
  cursor: pointer;
  text-align: right;
}
.user-row:last-child { border-bottom: none; }
.user-row:hover, .user-row:focus-visible { background: var(--surface-2); outline: none; }
.user-avatar {
  flex-shrink: 0;
  width: 32px; height: 32px;
  border-radius: 50%;
  background: var(--surface-2);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 800; color: var(--text-secondary);
}
.user-name { flex: 1; min-width: 0; font-size: 13px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: ltr; text-align: right; unicode-bidi: plaintext; }
.user-count { font-size: 12px; font-weight: 800; color: var(--text-secondary); background: var(--surface-2); padding: 3px 9px; border-radius: 20px; flex-shrink: 0; }
.user-chevron { flex-shrink: 0; color: var(--text-muted); display: flex; }

/* ---------- User sheet (drill-down) ---------- */
.sheet-overlay {
  position: fixed; inset: 0;
  background: rgba(2, 6, 23, 0.7);
  z-index: 60;
  display: flex;
  align-items: flex-end;
}
.sheet-overlay[hidden] { display: none; }
.sheet {
  width: 100%;
  max-height: 82vh;
  background: var(--surface);
  border: 1px solid var(--border);
  border-top-left-radius: 20px;
  border-top-right-radius: 20px;
  display: flex;
  flex-direction: column;
  padding-bottom: var(--safe-bottom);
}
.sheet-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.sheet-title { min-width: 0; }
.sheet-title .name { font-size: 15px; font-weight: 800; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: ltr; text-align: right; unicode-bidi: plaintext; }
.sheet-title .count { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.sheet-close {
  flex-shrink: 0;
  width: 44px; height: 44px;
  border-radius: 50%;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text-primary);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
}
.sheet-close:hover, .sheet-close:focus-visible { filter: brightness(1.25); outline: 2px solid var(--accent); outline-offset: 2px; }
.sheet-body { overflow-y: auto; padding: 14px 16px; }
.sheet-body .request-list { gap: 10px; }

@media (min-width: 480px) {
  .stats-scroll { flex-wrap: wrap; }
  .mini-card { flex: 1 1 0; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <div class="brand">
      <span class="brand-icon" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      </span>
      <div class="brand-text">
        <h1>لوحة التحكم</h1>
        <span class="brand-sub">إحصائيات بوت التيليجرام</span>
      </div>
    </div>
    <div class="avatar-menu">
      <button type="button" class="avatar-btn" id="avatarBtn" aria-haspopup="true" aria-expanded="false" aria-label="قائمة الحساب">{{ (username or '?')[0] | upper }}</button>
      <div class="avatar-dropdown" id="avatarDropdown" role="menu" hidden>
        <div class="avatar-dropdown-user">{{ username }}</div>
        <button type="button" class="avatar-dropdown-item" id="logoutBtn" role="menuitem">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
          تسجيل الخروج
        </button>
      </div>
    </div>
  </header>

  <main>
    <div id="loading">جاري التحميل...</div>
    <div id="content" style="display:none">

      <section class="stats">
        <div class="card hero-card">
          <span class="hero-icon" aria-hidden="true">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
          </span>
          <div class="hero-body">
            <div class="hero-label">معدل النجاح</div>
            <div class="hero-num" id="statRate">0%</div>
            <div class="trend" id="statTrend"></div>
          </div>
        </div>

        <div class="stats-scroll">
          <div class="card mini-card">
            <span class="mini-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
            </span>
            <div class="mini-num" id="statTotal">0</div>
            <div class="mini-label">إجمالي الاستخدامات</div>
          </div>
          <div class="card mini-card good">
            <span class="mini-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </span>
            <div class="mini-num" id="statSuccess">0</div>
            <div class="mini-label">ناجحة</div>
          </div>
          <div class="card mini-card critical">
            <span class="mini-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </span>
            <div class="mini-num" id="statFailed">0</div>
            <div class="mini-label">فاشلة</div>
          </div>
        </div>
        <span class="refresh-note" id="refreshNote"><span id="storageNote"></span> · يحدّث تلقائيًا كل 30 ثانية</span>
      </section>

      <div class="card">
        <h2>توزيع المنصات</h2>
        <div class="donut-wrap">
          <svg viewBox="0 0 160 160" class="donut" id="donutChart" role="img" aria-label="توزيع الاستخدام حسب المنصة"></svg>
        </div>
        <div class="legend" id="platformLegend"></div>
      </div>

      <details class="card users-card">
        <summary class="users-summary">
          <h2>المستخدمون</h2>
          <span class="users-summary-icon" aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </span>
        </summary>
        <div class="users-list" id="usersList"></div>
      </details>

      <div class="card">
        <h2>اتجاهات الاستخدام (آخر 14 يوم)</h2>
        <svg class="line-svg" id="lineChart" viewBox="0 0 480 140" preserveAspectRatio="none" role="img" aria-label="اتجاه الاستخدام اليومي آخر 14 يوم"></svg>
      </div>

      <div class="card">
        <h2>أحدث طلبات التحميل</h2>
        <div class="toolbar">
          <input type="text" id="search" placeholder="ابحث بالمستخدم أو الرابط أو المنصة...">
          <select id="sortSelect" aria-label="ترتيب حسب">
            <option value="timestamp_desc">الأحدث أولاً</option>
            <option value="timestamp_asc">الأقدم أولاً</option>
            <option value="status">حسب الحالة</option>
            <option value="platform">حسب المنصة</option>
          </select>
        </div>
        <div class="toolbar">
          <div class="chip-row">
            <span class="chip active" data-filter="status" data-value="all">الكل</span>
            <span class="chip" data-filter="status" data-value="success">ناجحة</span>
            <span class="chip" data-filter="status" data-value="failed">فاشلة</span>
          </div>
        </div>
        <div class="toolbar">
          <div class="chip-row">
            <span class="chip active" data-filter="source" data-value="all">كل المصادر</span>
            <span class="chip" data-filter="source" data-value="telegram">تيليجرام</span>
            <span class="chip" data-filter="source" data-value="web">الموقع</span>
          </div>
        </div>
        <div class="request-list" id="requestList"></div>
      </div>

    </div>
  </main>
</div>

<div class="tooltip" id="tooltip"></div>

<div class="sheet-overlay" id="sheetOverlay" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetUserName">
    <div class="sheet-header">
      <div class="sheet-title">
        <div class="name" id="sheetUserName"></div>
        <div class="count" id="sheetUserCount"></div>
      </div>
      <button type="button" class="sheet-close" id="sheetClose" aria-label="إغلاق">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="sheet-body">
      <div class="request-list" id="sheetRequestList"></div>
    </div>
  </div>
</div>

<script>
let allRows = [];
let sortValue = 'timestamp_desc';
let statusFilter = 'all';
let sourceFilter = 'all';
let lastTrendText = null;

const PLATFORM_ICONS = {
  'YouTube': { bg: '#EF4444', svg: '<svg width="15" height="15" viewBox="0 0 24 24" fill="#fff"><path d="M8 6v12l10-6z"/></svg>' },
  'TikTok': { bg: '#111827', svg: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l7 2v9a3 3 0 1 1-3-3"/></svg>' },
  'Instagram': { bg: 'linear-gradient(135deg,#F472B6,#A78BFA)', svg: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1"/></svg>' },
  'Twitter/X': { bg: '#111827', svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"><line x1="4" y1="4" x2="20" y2="20"/><line x1="20" y1="4" x2="4" y2="20"/></svg>' },
};
const DEFAULT_PLATFORM_ICON = { bg: '#334155', svg: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>' };
const ICON_CHECK = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const ICON_X = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function platformIcon(name) {
  return PLATFORM_ICONS[name] || DEFAULT_PLATFORM_ICON;
}

/* ---------- Stats ---------- */
function renderStats(data) {
  const rate = data.total ? Math.round((data.success / data.total) * 1000) / 10 : 0;
  document.getElementById('statRate').textContent = rate + '%';
  document.getElementById('statTotal').textContent = data.total;
  document.getElementById('statSuccess').textContent = data.success;
  document.getElementById('statFailed').textContent = data.failed;

  const storageNote = document.getElementById('storageNote');
  if (storageNote) {
    storageNote.textContent = data.persistent_storage
      ? '🟢 تخزين دائم'
      : '🟡 تخزين مؤقت (يُمسح مع كل نشر)';
  }

  const trend = document.getElementById('statTrend');
  if (trend && data.daily && data.daily.length >= 2) {
    const today = data.daily[data.daily.length - 1].count;
    const yesterday = data.daily[data.daily.length - 2].count;
    const diff = today - yesterday;
    let cls, text;
    if (diff > 0) { cls = 'trend up'; text = `↑ +${diff} اليوم`; }
    else if (diff < 0) { cls = 'trend down'; text = `↓ ${diff} اليوم`; }
    else { cls = 'trend flat'; text = '— بدون تغيير اليوم'; }
    trend.textContent = text;
    if (text !== lastTrendText) {
      trend.className = cls;
      void trend.offsetWidth;
      trend.className = cls + ' pulse';
      lastTrendText = text;
    }
  }
}

/* ---------- Donut + legend ---------- */
const DONUT_COLORS = ['var(--donut-1)', 'var(--donut-2)', 'var(--donut-3)', 'var(--donut-4)'];
const MAX_DONUT_SLICES = 5;

function renderDonut(byPlatform) {
  const svg = document.getElementById('donutChart');
  const legend = document.getElementById('platformLegend');
  const total = byPlatform.reduce((s, p) => s + p.count, 0);

  svg.innerHTML = '';
  legend.innerHTML = '';

  if (total === 0) {
    legend.innerHTML = '<div class="empty-state">لا توجد بيانات بعد</div>';
    const track = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    track.setAttribute('cx', '80'); track.setAttribute('cy', '80'); track.setAttribute('r', '60');
    track.setAttribute('fill', 'none'); track.setAttribute('stroke', 'var(--surface-2)'); track.setAttribute('stroke-width', '20');
    svg.appendChild(track);
    return;
  }

  let slices = byPlatform.slice();
  if (slices.length > MAX_DONUT_SLICES) {
    const head = slices.slice(0, MAX_DONUT_SLICES - 1);
    const restCount = slices.slice(MAX_DONUT_SLICES - 1).reduce((s, p) => s + p.count, 0);
    slices = head.concat([{ platform: 'أخرى', count: restCount }]);
  }

  const r = 60, cx = 80, cy = 80, circumference = 2 * Math.PI * r, strokeW = 20;

  const track = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  track.setAttribute('cx', cx); track.setAttribute('cy', cy); track.setAttribute('r', r);
  track.setAttribute('fill', 'none'); track.setAttribute('stroke', 'var(--surface-2)'); track.setAttribute('stroke-width', strokeW);
  svg.appendChild(track);

  const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  group.setAttribute('transform', `rotate(-90 ${cx} ${cy})`);
  svg.appendChild(group);

  let offset = 0;
  slices.forEach((p, i) => {
    const pct = p.count / total;
    const dash = pct * circumference;
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('class', 'donut-seg');
    circle.setAttribute('cx', cx); circle.setAttribute('cy', cy); circle.setAttribute('r', r);
    circle.setAttribute('fill', 'none');
    circle.setAttribute('stroke', p.platform === 'أخرى' ? 'var(--donut-other)' : DONUT_COLORS[i % DONUT_COLORS.length]);
    circle.setAttribute('stroke-width', strokeW);
    circle.setAttribute('stroke-dasharray', `${dash} ${circumference - dash}`);
    circle.setAttribute('stroke-dashoffset', -offset);
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = `${p.platform}: ${p.count} (${Math.round(pct * 100)}%)`;
    circle.appendChild(title);
    group.appendChild(circle);
    offset += dash;
  });

  const totalText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  totalText.setAttribute('x', cx); totalText.setAttribute('y', cy - 6);
  totalText.setAttribute('text-anchor', 'middle'); totalText.setAttribute('class', 'donut-total');
  totalText.textContent = total >= 1000 ? (total / 1000).toFixed(1) + 'k' : total;
  svg.appendChild(totalText);

  const totalLabel = document.createElementNS('http://www.w3.org/2000/svg', 'text');
  totalLabel.setAttribute('x', cx); totalLabel.setAttribute('y', cy + 14);
  totalLabel.setAttribute('text-anchor', 'middle'); totalLabel.setAttribute('class', 'donut-total-label');
  totalLabel.textContent = 'تحميل';
  svg.appendChild(totalLabel);

  slices.forEach((p, i) => {
    const pct = Math.round((p.count / total) * 100);
    const color = p.platform === 'أخرى' ? 'var(--donut-other)' : DONUT_COLORS[i % DONUT_COLORS.length];
    const row = document.createElement('div');
    row.className = 'legend-item';
    row.innerHTML = `
      <span class="legend-dot" style="background:${color}"></span>
      <span class="legend-name">${escapeHtml(p.platform)}</span>
      <span class="legend-count">${p.count}</span>
      <span class="legend-pct">${pct}%</span>
    `;
    legend.appendChild(row);
  });
}

/* ---------- Line chart ---------- */
function renderLineChart(daily) {
  const svg = document.getElementById('lineChart');
  const tooltip = document.getElementById('tooltip');
  svg.innerHTML = '';
  if (!daily.length) return;

  const W = 480, H = 140, padX = 8, padTop = 14, padBottom = 26;
  const max = Math.max(1, ...daily.map(d => d.count));
  const step = (W - padX * 2) / Math.max(1, daily.length - 1);
  const points = daily.map((d, i) => {
    const x = padX + i * step;
    const y = padTop + (1 - d.count / max) * (H - padTop - padBottom);
    return { x, y, d };
  });

  const areaPath = `M ${points[0].x} ${H - padBottom} ` +
    points.map(p => `L ${p.x} ${p.y}`).join(' ') +
    ` L ${points[points.length - 1].x} ${H - padBottom} Z`;
  const area = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  area.setAttribute('d', areaPath);
  area.setAttribute('fill', 'var(--accent)');
  area.setAttribute('opacity', '0.14');
  svg.appendChild(area);

  const linePath = points.map((p, i) => (i === 0 ? 'M' : 'L') + ` ${p.x} ${p.y}`).join(' ');
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  line.setAttribute('d', linePath);
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--accent)');
  line.setAttribute('stroke-width', '2.5');
  line.setAttribute('stroke-linecap', 'round');
  line.setAttribute('stroke-linejoin', 'round');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduceMotion) {
    const approxLen = points.reduce((sum, p, i) => i === 0 ? 0 : sum + Math.hypot(p.x - points[i - 1].x, p.y - points[i - 1].y), 0);
    line.style.setProperty('--len', approxLen);
    line.style.strokeDasharray = String(approxLen);
    line.classList.add('line-path');
  }
  svg.appendChild(line);

  const labelEvery = daily.length > 10 ? 2 : 1;
  points.forEach((p, i) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', 'line-point');
    g.setAttribute('tabindex', '0');

    const hit = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    hit.setAttribute('class', 'hit');
    hit.setAttribute('cx', p.x); hit.setAttribute('cy', p.y); hit.setAttribute('r', '14');
    g.appendChild(hit);

    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); dot.setAttribute('r', '3');
    dot.setAttribute('fill', 'var(--bg)');
    dot.setAttribute('stroke', 'var(--accent)');
    dot.setAttribute('stroke-width', '2');
    g.appendChild(dot);

    const show = e => {
      tooltip.innerHTML = `<span class="v">${p.d.count}</span> <span class="d">— ${p.d.day}</span>`;
      tooltip.style.display = 'block';
      const rect = (e.currentTarget.querySelector('.hit')).getBoundingClientRect();
      tooltip.style.left = (rect.left + rect.width / 2 - tooltip.offsetWidth / 2) + 'px';
      tooltip.style.top = (rect.top - 40) + 'px';
    };
    const hide = () => { tooltip.style.display = 'none'; };
    g.addEventListener('pointerdown', show);
    g.addEventListener('pointerup', () => setTimeout(hide, 1200));
    g.addEventListener('mouseenter', show);
    g.addEventListener('mouseleave', hide);
    g.addEventListener('focus', show);
    g.addEventListener('blur', hide);
    svg.appendChild(g);

    if (i % labelEvery === 0) {
      const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      lbl.setAttribute('x', p.x); lbl.setAttribute('y', H - 6);
      lbl.setAttribute('text-anchor', 'middle');
      lbl.setAttribute('class', 'line-axis-label');
      lbl.textContent = p.d.day.slice(5).replace('-', '/');
      svg.appendChild(lbl);
    }
  });
}

/* ---------- Requests: filter/sort/render ---------- */
function applyFiltersAndSort() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  let rows = allRows.filter(r => {
    if (statusFilter !== 'all' && r.status !== statusFilter) return false;
    if (sourceFilter !== 'all' && r.source !== sourceFilter) return false;
    if (q) {
      const hay = `${r.requester || ''} ${r.url || ''} ${r.platform || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const [key, dir] = sortValue === 'timestamp_desc' ? ['timestamp', -1]
    : sortValue === 'timestamp_asc' ? ['timestamp', 1]
    : sortValue === 'status' ? ['status', 1]
    : ['platform', 1];
  rows.sort((a, b) => {
    const av = (a[key] || '').toString();
    const bv = (b[key] || '').toString();
    return av > bv ? dir : av < bv ? -dir : 0;
  });

  renderRequestList(rows);
}

const SOURCE_LABEL = { telegram: 'تيليجرام', web: 'الموقع' };

function requestCardHTML(r) {
  const icon = platformIcon(r.platform);
  const isSuccess = r.status === 'success';
  return `
    <div class="request-card">
      <div class="request-top">
        <span class="platform-badge">
          <span class="platform-badge-icon" style="background:${icon.bg}">${icon.svg}</span>
          <span class="platform-badge-name">${escapeHtml(r.platform || 'غير معروف')}</span>
        </span>
        <span class="status-pill ${isSuccess ? 'success' : 'failed'}">
          ${isSuccess ? ICON_CHECK : ICON_X} ${isSuccess ? 'نجح' : 'فشل'}
        </span>
      </div>
      <a class="request-url" href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(r.url)}">${escapeHtml(r.url)}</a>
      <div class="request-meta">${escapeHtml(r.requester || '')} · #${r.id} · ${escapeHtml(r.timestamp)} · ${SOURCE_LABEL[r.source] || escapeHtml(r.source)}</div>
    </div>
  `;
}

function renderRequestList(rows) {
  const list = document.getElementById('requestList');
  if (rows.length === 0) {
    list.innerHTML = '<div class="empty-state">لا توجد نتائج</div>';
    return;
  }
  list.innerHTML = rows.map(requestCardHTML).join('');
}

/* ---------- Users + drill-down sheet ---------- */
function renderUsers(users) {
  const list = document.getElementById('usersList');
  if (!users || users.length === 0) {
    list.innerHTML = '<div class="empty-state">لا يوجد مستخدمون بعد</div>';
    return;
  }
  list.innerHTML = users.map(u => `
    <button type="button" class="user-row" data-user="${escapeHtml(u.requester)}" data-count="${u.count}">
      <span class="user-avatar" aria-hidden="true">${escapeHtml((u.requester || '?').replace('@', '')[0] || '?').toUpperCase()}</span>
      <span class="user-name">${escapeHtml(u.requester)}</span>
      <span class="user-count">${u.count}</span>
      <span class="user-chevron" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg></span>
    </button>
  `).join('');
  list.querySelectorAll('.user-row').forEach(btn => {
    btn.addEventListener('click', () => openUserSheet(btn.dataset.user, btn.dataset.count));
  });
}

const sheetOverlay = document.getElementById('sheetOverlay');
let lastFocusedBeforeSheet = null;

async function openUserSheet(requester, count) {
  lastFocusedBeforeSheet = document.activeElement;
  document.getElementById('sheetUserName').textContent = requester;
  document.getElementById('sheetUserCount').textContent = `${count} طلب تحميل`;
  const body = document.getElementById('sheetRequestList');
  body.innerHTML = '<div class="empty-state">جاري التحميل...</div>';
  sheetOverlay.hidden = false;
  document.getElementById('sheetClose').focus();

  try {
    const res = await fetch(`/api/dashboard-data/user-requests?name=${encodeURIComponent(requester)}`);
    if (!res.ok) throw new Error('request failed');
    const data = await res.json();
    body.innerHTML = data.requests.length
      ? data.requests.map(requestCardHTML).join('')
      : '<div class="empty-state">لا توجد طلبات</div>';
  } catch (e) {
    body.innerHTML = '<div class="empty-state">تعذر تحميل طلبات هذا المستخدم</div>';
  }
}

function closeUserSheet() {
  sheetOverlay.hidden = true;
  if (lastFocusedBeforeSheet) lastFocusedBeforeSheet.focus();
}

document.getElementById('sheetClose').addEventListener('click', closeUserSheet);
sheetOverlay.addEventListener('click', e => { if (e.target === sheetOverlay) closeUserSheet(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !sheetOverlay.hidden) closeUserSheet(); });

/* ---------- Avatar dropdown ---------- */
const avatarBtn = document.getElementById('avatarBtn');
const avatarDropdown = document.getElementById('avatarDropdown');
function closeAvatarMenu() {
  avatarDropdown.hidden = true;
  avatarBtn.setAttribute('aria-expanded', 'false');
}
avatarBtn.addEventListener('click', e => {
  e.stopPropagation();
  const open = avatarDropdown.hidden;
  avatarDropdown.hidden = !open;
  avatarBtn.setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', e => {
  if (!avatarDropdown.hidden && !avatarDropdown.contains(e.target) && e.target !== avatarBtn) closeAvatarMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAvatarMenu(); });

document.getElementById('logoutBtn').addEventListener('click', async () => {
  try {
    await fetch(window.location.pathname, { headers: { Authorization: 'Basic ' + btoa('logout:logout') } });
  } catch (e) { /* best-effort: some browsers keep Basic Auth cached regardless */ }
  window.location.href = '/';
});

/* ---------- Load / refresh ---------- */
async function loadData() {
  const content = document.getElementById('content');
  const hasData = allRows.length > 0;
  const isFirstLoad = !hasData;
  if (hasData) content.classList.add('stale');
  try {
    const res = await fetch('/api/dashboard-data');
    if (!res.ok) throw new Error('request failed');
    const data = await res.json();
    allRows = data.recent;
    renderStats(data);
    renderDonut(data.by_platform);
    renderUsers(data.users);
    renderLineChart(data.daily);
    applyFiltersAndSort();
    document.getElementById('loading').style.display = 'none';
    content.style.display = 'block';
    if (isFirstLoad) {
      content.querySelectorAll('.card').forEach((card, i) => card.style.setProperty('--i', i));
      content.classList.add('reveal');
    }
  } catch (e) {
    document.getElementById('refreshNote').textContent = 'تعذر تحديث البيانات';
  } finally {
    content.classList.remove('stale');
  }
}

document.getElementById('search').addEventListener('input', applyFiltersAndSort);
document.getElementById('sortSelect').addEventListener('change', e => { sortValue = e.target.value; applyFiltersAndSort(); });
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const group = chip.dataset.filter;
    document.querySelectorAll(`.chip[data-filter="${group}"]`).forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    if (group === 'status') statusFilter = chip.dataset.value;
    if (group === 'source') sourceFilter = chip.dataset.value;
    applyFiltersAndSort();
  });
});

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""


@app.route("/dashboard")
@require_dashboard_auth
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE, username=DASHBOARD_USERNAME)


def run_server():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Starting Telegram bot polling as @%s", bot.get_me().username)
    bot.infinity_polling(logger_level=logging.INFO)

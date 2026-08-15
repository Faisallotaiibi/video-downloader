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
        "persistent_storage": USE_TURSO,
    }


@app.route("/api/dashboard-data")
@require_dashboard_auth
def api_dashboard_data():
    return jsonify(dashboard_data())


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>لوحة التحكم</title>
<style>
:root {
  --bg: #0a0a0a;
  --surface: #141414;
  --surface-2: #1c1c1c;
  --border: #2a2a2a;
  --text-primary: #f2f2f2;
  --text-secondary: #9a9a9a;
  --text-muted: #6b6b6b;
  --bar-dim: #3a3a3a;
  --bar-bright: #f2f2f2;
  --status-good: #0ca30c;
  --status-critical: #d03b3b;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { overflow-x: hidden; max-width: 100%; }
body {
  font-family: 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text-primary);
  min-height: 100vh;
  padding: 30px 15px 60px;
}
.wrap { max-width: 1100px; margin: 0 auto; }
header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 22px; flex-wrap: wrap; gap: 8px; }
h1 { font-size: 20px; font-weight: 600; }
.refresh-note { font-size: 12px; color: var(--text-muted); }
.stats { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
.card {
  background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0) 45%), var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.35);
  margin-bottom: 16px;
}
.stat-card { flex: 1; min-width: 140px; }
.stat-card .num { font-size: 32px; font-weight: 700; font-variant-numeric: proportional-nums; }
.stat-card .label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.stat-card.good .num { color: var(--status-good); }
.stat-card.critical .num { color: var(--status-critical); }
.trend { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; margin-top: 8px; font-weight: 600; }
.trend.up { color: var(--status-good); }
.trend.down { color: var(--status-critical); }
.trend.flat { color: var(--text-muted); }
.card h2 { font-size: 14px; font-weight: 600; color: var(--text-secondary); margin-bottom: 16px; }
#loading { color: var(--text-muted); font-size: 13px; padding: 20px 0; text-align: center; }
#content { transition: opacity 0.2s; }

@media (prefers-reduced-motion: no-preference) {
  #content.reveal .card {
    animation: cardIn 0.45s cubic-bezier(.2,.8,.2,1) both;
    animation-delay: calc(var(--i, 0) * 0.06s);
  }
  @keyframes cardIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .bar { transition: height 0.5s cubic-bezier(.2,.8,.2,1), filter 0.15s; }
  .trend.pulse { animation: trendPop 0.4s ease; }
  @keyframes trendPop {
    0% { transform: scale(1); }
    35% { transform: scale(1.12); }
    100% { transform: scale(1); }
  }
}
#content.stale { opacity: 0.55; }

.chart-scroll { overflow-x: auto; }
.chart-inner { min-width: 480px; }
.bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 120px; position: relative; }
.bar-col { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar {
  width: 100%;
  max-width: 22px;
  border-radius: 4px 4px 0 0;
  background: var(--bar-dim);
  min-height: 3px;
  cursor: pointer;
  transition: filter 0.15s;
}
.bar:hover, .bar:focus { filter: brightness(1.3); outline: none; }
.bar-labels { display: flex; gap: 4px; margin-top: 8px; }
.bar-labels span { flex: 1; min-width: 0; text-align: center; font-size: 10px; color: var(--text-muted); }
.tooltip {
  position: fixed;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  pointer-events: none;
  z-index: 10;
  display: none;
  box-shadow: 0 8px 20px rgba(0,0,0,0.4);
}
.tooltip .v { font-weight: 700; color: var(--text-primary); }
.tooltip .d { color: var(--text-secondary); }

.platform-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 13px; }
.platform-name { width: 100px; flex-shrink: 0; color: var(--text-secondary); }
.platform-track { flex: 1; background: var(--surface-2); border-radius: 6px; height: 10px; overflow: hidden; }
.platform-fill { display: block; height: 100%; background: linear-gradient(90deg, #6b6b6b, var(--bar-bright)); border-radius: 6px; }
.platform-count { width: 90px; text-align: left; direction: ltr; color: var(--text-muted); font-size: 12px; }

.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
.toolbar input[type="text"] {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  color: var(--text-primary);
  font-size: 13px;
  min-width: 200px;
  flex: 1;
}
.toolbar input[type="text"]::placeholder { color: var(--text-muted); }
.chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 6px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.chip.active { background: var(--text-primary); color: #000; border-color: var(--text-primary); font-weight: 600; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; cursor: pointer; user-select: none; }
th:hover { color: var(--text-secondary); }
td { color: var(--text-secondary); }
.table-wrap { overflow-x: auto; }
td.url { max-width: 280px; overflow: hidden; text-overflow: ellipsis; direction: ltr; text-align: left; }
td.url a { color: var(--text-primary); text-decoration: none; border-bottom: 1px dotted var(--text-muted); }
td.url a:hover { color: #fff; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.status-dot.success { background: var(--status-good); }
.status-dot.failed { background: var(--status-critical); }
.empty-row td { text-align: center; color: var(--text-muted); padding: 24px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📊 لوحة تحكم البوت</h1>
    <span class="refresh-note" id="refreshNote">
      <span id="storageNote"></span> · يحدّث تلقائيًا كل 30 ثانية
    </span>
  </header>

  <div id="loading">جاري التحميل...</div>
  <div id="content" style="display:none">
    <div class="stats">
      <div class="card stat-card"><div class="num" id="statTotal">0</div><div class="label">إجمالي الاستخدامات</div><div class="trend" id="statTrend"></div></div>
      <div class="card stat-card good"><div class="num" id="statSuccess">0</div><div class="label">ناجحة</div></div>
      <div class="card stat-card critical"><div class="num" id="statFailed">0</div><div class="label">فاشلة</div></div>
    </div>

    <div class="card">
      <h2>الاستخدام آخر 14 يوم</h2>
      <div class="chart-scroll">
        <div class="chart-inner">
          <div class="bar-chart" id="barChart"></div>
          <div class="bar-labels" id="barLabels"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>حسب المنصة</h2>
      <div id="platformList"></div>
    </div>

    <div class="card">
      <h2>آخر الطلبات</h2>
      <div class="toolbar">
        <input type="text" id="search" placeholder="ابحث بالمستخدم أو الرابط أو المنصة...">
        <span class="chip active" data-filter="status" data-value="all">الكل</span>
        <span class="chip" data-filter="status" data-value="success">ناجحة</span>
        <span class="chip" data-filter="status" data-value="failed">فاشلة</span>
        <span class="chip active" data-filter="source" data-value="all">كل المصادر</span>
        <span class="chip" data-filter="source" data-value="telegram">تيليجرام</span>
        <span class="chip" data-filter="source" data-value="web">الموقع</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-key="timestamp">الوقت (UTC)</th>
              <th data-key="source">المصدر</th>
              <th data-key="requester">المستخدم</th>
              <th data-key="platform">المنصة</th>
              <th>الرابط</th>
              <th data-key="status">الحالة</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
let allRows = [];
let sortKey = 'timestamp';
let sortDir = -1;
let statusFilter = 'all';
let sourceFilter = 'all';
let lastTrendText = null;

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderStats(data) {
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
    if (diff > 0) {
      cls = 'trend up';
      text = `↑ +${diff} اليوم`;
    } else if (diff < 0) {
      cls = 'trend down';
      text = `↓ ${diff} اليوم`;
    } else {
      cls = 'trend flat';
      text = '— بدون تغيير اليوم';
    }
    trend.textContent = text;
    if (text !== lastTrendText) {
      trend.className = cls;
      void trend.offsetWidth; // restart the pulse animation on change
      trend.className = cls + ' pulse';
      lastTrendText = text;
    }
  }
}

function renderBarChart(daily) {
  const chart = document.getElementById('barChart');
  const labels = document.getElementById('barLabels');
  const tooltip = document.getElementById('tooltip');
  chart.innerHTML = '';
  labels.innerHTML = '';
  const max = Math.max(1, ...daily.map(d => d.count));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const grownBars = [];

  daily.forEach(d => {
    const col = document.createElement('div');
    col.className = 'bar-col';
    const bar = document.createElement('div');
    bar.className = 'bar';
    const pct = d.count / max;
    const targetHeight = Math.max(3, pct * 100) + '%';
    bar.style.height = reduceMotion ? targetHeight : '0%';
    const lightness = 35 + pct * 55;
    bar.style.background = `hsl(0, 0%, ${lightness}%)`;
    bar.tabIndex = 0;
    bar.addEventListener('mouseenter', e => showTooltip(e, d));
    bar.addEventListener('focus', e => showTooltip(e, d));
    bar.addEventListener('mousemove', e => positionTooltip(e));
    bar.addEventListener('mouseleave', hideTooltip);
    bar.addEventListener('blur', hideTooltip);
    col.appendChild(bar);
    chart.appendChild(col);
    if (!reduceMotion) grownBars.push({ el: bar, targetHeight });

    const lbl = document.createElement('span');
    const shortDay = d.day.slice(5).replace('-', '/');
    lbl.textContent = shortDay;
    labels.appendChild(lbl);
  });

  if (grownBars.length) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      grownBars.forEach(({ el, targetHeight }) => { el.style.height = targetHeight; });
    }));
  }

  function showTooltip(e, d) {
    tooltip.innerHTML = `<span class="v">${d.count}</span> <span class="d">— ${d.day}</span>`;
    tooltip.style.display = 'block';
    positionTooltip(e);
  }
  function positionTooltip(e) {
    const rect = e.target.getBoundingClientRect();
    tooltip.style.left = (rect.left + rect.width / 2 - tooltip.offsetWidth / 2) + 'px';
    tooltip.style.top = (rect.top - 40) + 'px';
  }
  function hideTooltip() { tooltip.style.display = 'none'; }
}

function renderPlatforms(byPlatform) {
  const container = document.getElementById('platformList');
  container.innerHTML = '';
  const max = Math.max(1, ...byPlatform.map(p => p.count));
  if (byPlatform.length === 0) {
    container.innerHTML = '<div class="empty-row" style="color:var(--text-muted);font-size:13px;">لا توجد بيانات بعد</div>';
    return;
  }
  byPlatform.forEach(p => {
    const row = document.createElement('div');
    row.className = 'platform-row';
    const pct = (p.count / max) * 100;
    row.innerHTML = `
      <span class="platform-name">${escapeHtml(p.platform)}</span>
      <span class="platform-track"><span class="platform-fill" style="width:${pct}%"></span></span>
      <span class="platform-count">${p.count}</span>
    `;
    container.appendChild(row);
  });
}

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
  rows.sort((a, b) => {
    const av = (a[sortKey] || '').toString();
    const bv = (b[sortKey] || '').toString();
    return av > bv ? sortDir : av < bv ? -sortDir : 0;
  });
  renderTable(rows);
}

function renderTable(rows) {
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = '';
  if (rows.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">لا توجد نتائج</td></tr>';
    return;
  }
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const statusLabel = r.status === 'success' ? 'نجح' : 'فشل';
    tr.innerHTML = `
      <td>${escapeHtml(r.timestamp)}</td>
      <td>${escapeHtml(r.source)}</td>
      <td>${escapeHtml(r.requester || '')}</td>
      <td>${escapeHtml(r.platform || '')}</td>
      <td class="url"></td>
      <td><span class="status"><span class="status-dot ${r.status}"></span>${statusLabel}</span></td>
    `;
    const urlCell = tr.querySelector('.url');
    const a = document.createElement('a');
    a.href = r.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = r.url;
    a.textContent = r.url;
    urlCell.appendChild(a);
    tbody.appendChild(tr);
  });
}

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
    renderBarChart(data.daily);
    renderPlatforms(data.by_platform);
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
document.querySelectorAll('th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    if (sortKey === key) { sortDir *= -1; } else { sortKey = key; sortDir = -1; }
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
    return render_template_string(DASHBOARD_TEMPLATE)


def run_server():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Starting Telegram bot polling as @%s", bot.get_me().username)
    bot.infinity_polling(logger_level=logging.INFO)

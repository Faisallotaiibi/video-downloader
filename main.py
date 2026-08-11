import concurrent.futures
import logging
import os
import random
import re
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

# Many sites (Reddit, etc.) serve video and audio as separate streams that
# need muxing; the OS ffmpeg isn't installed on Render, so use the static
# binary bundled by imageio-ffmpeg instead.
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
_DIMENSIONS_RE = re.compile(r'Video:.*?(\d{2,5})x(\d{2,5})')


def probe_video_metadata(filepath):
    """Read duration/width/height straight from the file via ffmpeg.

    Some sites (e.g. Instagram) don't always give yt-dlp a duration in the
    extracted info, which makes Telegram's client show a broken 0:00/frozen
    player. ffmpeg -i prints this straight from the file's own container
    metadata regardless of what the source site reported.
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
        return duration, width, height
    except Exception:
        logger.exception("Failed to probe video metadata for %s", filepath)
        return None, None, None


def reencode_for_compatibility(filepath):
    """Re-encode to a clean, standard H.264/AAC mp4.

    yt-dlp's merger uses `-c copy` (no re-encode) to stay fast, which just
    repackages whatever bitstream the source site served. Some sites
    (Instagram's DASH-fragmented clips especially) hand over a technically
    valid but slightly irregular stream - frame references or timestamps
    that a lenient decoder (a seek/preview or desktop player) tolerates,
    but that a strict one (Telegram's iOS player, or iOS's own camera roll
    import) chokes on: playback freezes on the first frame while audio
    keeps going, and saving the file fails outright. A real re-encode
    produces a clean stream that plays and saves normally everywhere, at
    the cost of some CPU time.
    """
    fixed_path = f'{filepath}.fixed.mp4'
    try:
        result = subprocess.run(
            [
                FFMPEG_PATH, '-y', '-i', filepath,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart',
                fixed_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
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

# 'best' alone never triggers muxing, even with ffmpeg available - it only
# picks an already-combined format. Ask for bestvideo+bestaudio explicitly
# so separate-stream sites (Reddit, etc.) actually get merged. No ext filter
# here on purpose: restricting to mp4/m4a can silently cap quality below
# what's actually available (e.g. a higher-res webm/vp9 stream) since
# merge_output_format already remuxes the result to mp4 regardless of the
# source container.
FORMAT_SELECTOR = 'bestvideo+bestaudio/best'

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
            'format': FORMAT_SELECTOR,
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'max_filesize': MAX_TELEGRAM_FILE_SIZE,
            'extractor_args': YOUTUBE_EXTRACTOR_ARGS,
            'ffmpeg_location': FFMPEG_PATH,
            # yt-dlp sets no socket timeout by default, so a stalled CDN
            # connection can hang a worker thread forever; overwrites=True
            # stops it from silently reusing a leftover file from an
            # earlier hung/interrupted attempt at the same path.
            'socket_timeout': 30,
            'overwrites': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            raise FileNotFoundError("الملف أكبر من 50MB")

        filename = reencode_for_compatibility(filename)

        if os.path.getsize(filename) > MAX_TELEGRAM_FILE_SIZE:
            raise FileNotFoundError("الملف أكبر من 50MB")

        duration, width, height = probe_video_metadata(filename)
        duration = duration or info.get('duration')
        width = width or info.get('width')
        height = height or info.get('height')
        logger.info(
            "Downloaded %s: duration=%s width=%s height=%s vcodec=%s acodec=%s",
            url, duration, width, height, info.get('vcodec'), info.get('acodec'),
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
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

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

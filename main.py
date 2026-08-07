import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

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

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder=".")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
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
    conn.commit()
    conn.close()


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
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
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
        conn.commit()
        conn.close()
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
    bot.reply_to(message, "أهلاً! أرسل لي رابط الفيديو وأنا أحمله لك بدون علامة مائية 🎬")


@bot.message_handler(func=lambda m: True)
def download(message):
    url = message.text.strip()
    requester = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or str(message.chat.id)
    )

    if not url.lower().startswith(("http://", "https://")):
        bot.reply_to(message, "من فضلك أرسل رابط فيديو صالح.")
        return

    bot.reply_to(message, "⏳ جاري التحميل...")
    filename = None
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'max_filesize': MAX_TELEGRAM_FILE_SIZE,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            raise FileNotFoundError("تعذر تنزيل الملف (قد يكون حجمه أكبر من 50MB)")

        with open(filename, 'rb') as f:
            bot.send_video(message.chat.id, f)
        log_usage("telegram", requester, url, "success")
    except Exception as e:
        logger.exception("Download failed for url=%s", url)
        bot.reply_to(message, f"❌ فشل التحميل: {e}")
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


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>لوحة التحكم</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Segoe UI', sans-serif;
  background: linear-gradient(135deg, #667eea, #764ba2);
  min-height: 100vh;
  padding: 30px 15px;
}
.wrap { max-width: 1000px; margin: 0 auto; }
h1 { color: white; margin-bottom: 20px; }
.stats { display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 25px; }
.stat-card {
  background: white;
  border-radius: 14px;
  padding: 20px 25px;
  flex: 1;
  min-width: 140px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.2);
  text-align: center;
}
.stat-card .num { font-size: 30px; font-weight: bold; color: #333; }
.stat-card .label { font-size: 13px; color: #888; margin-top: 4px; }
.card {
  background: white;
  border-radius: 14px;
  padding: 20px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.2);
  margin-bottom: 20px;
  overflow-x: auto;
}
.card h2 { font-size: 16px; color: #333; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid #eee; white-space: nowrap; }
th { color: #888; font-weight: 600; }
td.url { max-width: 300px; overflow: hidden; text-overflow: ellipsis; direction: ltr; text-align: left; }
.badge { padding: 3px 10px; border-radius: 20px; font-size: 12px; }
.badge.success { background: #d4edda; color: #28a745; }
.badge.failed { background: #f8d7da; color: #e74c3c; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📊 لوحة تحكم البوت</h1>
  <div class="stats">
    <div class="stat-card"><div class="num">{{ total }}</div><div class="label">إجمالي الاستخدامات</div></div>
    <div class="stat-card"><div class="num">{{ success }}</div><div class="label">ناجحة</div></div>
    <div class="stat-card"><div class="num">{{ failed }}</div><div class="label">فاشلة</div></div>
  </div>

  <div class="card">
    <h2>حسب المنصة</h2>
    <table>
      <tr><th>المنصة</th><th>عدد الطلبات</th></tr>
      {% for row in by_platform %}
      <tr><td>{{ row.platform }}</td><td>{{ row.c }}</td></tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>آخر الطلبات</h2>
    <table>
      <tr><th>الوقت (UTC)</th><th>المصدر</th><th>المستخدم</th><th>المنصة</th><th>الرابط</th><th>الحالة</th></tr>
      {% for row in recent %}
      <tr>
        <td>{{ row.timestamp }}</td>
        <td>{{ row.source }}</td>
        <td>{{ row.requester }}</td>
        <td>{{ row.platform }}</td>
        <td class="url">{{ row.url }}</td>
        <td><span class="badge {{ row.status }}">{{ 'نجح' if row.status == 'success' else 'فشل' }}</span></td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
</body>
</html>
"""


@app.route("/dashboard")
@require_dashboard_auth
def dashboard():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) AS c FROM usage_log").fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) AS c FROM usage_log WHERE status = 'success'"
    ).fetchone()["c"]
    by_platform = conn.execute(
        "SELECT platform, COUNT(*) AS c FROM usage_log GROUP BY platform ORDER BY c DESC"
    ).fetchall()
    recent = conn.execute(
        "SELECT * FROM usage_log ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()

    return render_template_string(
        DASHBOARD_TEMPLATE,
        total=total,
        success=success,
        failed=total - success,
        by_platform=by_platform,
        recent=recent,
    )


def run_server():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    bot.infinity_polling()

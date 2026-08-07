import logging
import os
import threading
import time

import requests
import telebot
import yt_dlp
from flask import Flask, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", "https://video-downloader-0ea4.onrender.com")
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot API upload limit

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder=".")


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
    bot.reply_to(message, "أهلاً! أرسل لي رابط الفيديو وأنا أحمله لك بدون علامة مائية 🎬")


@bot.message_handler(content_types=['text'])
def download(message):
    logger.info("Received message from chat_id=%s: %r", message.chat.id, message.text)
    url = message.text.strip()
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
    except Exception as e:
        logger.exception("Download failed for url=%s", url)
        bot.reply_to(message, f"❌ فشل التحميل: {e}")
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
            return jsonify({"error": "تعذر العثور على رابط التحميل"}), 502

        return jsonify({"download_url": direct_url})
    except Exception as e:
        logger.exception("API download failed for url=%s", url)
        return jsonify({"error": str(e)}), 500


def run_server():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Starting Telegram bot polling as @%s", bot.get_me().username)
    bot.infinity_polling(logger_level=logging.INFO)

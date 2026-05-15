import os
import telebot
import yt_dlp
import threading
import http.server

BOT_TOKEN = "8899902646:AAGSVzBQ-c6HFqpI_AdO0u_0s7bcCGAtcEo"

bot = telebot.TeleBot(BOT_TOKEN, num_threads=4)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "أهلاً! أرسل لي رابط الفيديو وأنا أحمله لك بدون علامة مائية 🎬\n\nيدعم: TikTok, Instagram, Twitter, YouTube")

@bot.message_handler(func=lambda m: True)
def download(message):
    url = message.text.strip()
    msg = bot.reply_to(message, "⏳ جاري التحميل...")
    try:
        ydl_opts = {
            'format': 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[ext=mp4]/worst',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'max_filesize': 45000000,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        bot.edit_message_text("📤 جاري الإرسال...", message.chat.id, msg.message_id)

        with open(filename, 'rb') as f:
            bot.send_video(message.chat.id, f, supports_streaming=True)
        os.remove(filename)

    except Exception as e:
        bot.edit_message_text(f"❌ فشل التحميل: {str(e)}", message.chat.id, msg.message_id)

def run_server():
    server = http.server.HTTPServer(('0.0.0.0', 10000), http.server.BaseHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()
bot.infinity_polling()

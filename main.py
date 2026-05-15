import os
import telebot
import yt_dlp

BOT_TOKEN = "8899902646:AAGSVzBQ-c6HFqpI_AdO0u_0s7bcCGAtcEo"

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "أهلاً! أرسل لي رابط الفيديو وأنا أحمله لك بدون علامة مائية 🎬")

@bot.message_handler(func=lambda m: True)
def download(message):
    url = message.text.strip()
    bot.reply_to(message, "⏳ جاري التحميل...")
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        
        with open(filename, 'rb') as f:
            bot.send_video(message.chat.id, f)
        os.remove(filename)
    except Exception as e:
        bot.reply_to(message, f"❌ فشل التحميل: {str(e)}")

bot.polling()

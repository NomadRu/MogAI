import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 从 Render 环境变量读取，绝不要写死
TOKEN = os.environ["TELEGRAM_TOKEN"]

app = Flask(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot 已启动")

def run_bot():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.run_polling()

@app.route("/")
def index():
    return "Bot is running"

@app.route("/health")
def health():
    return "OK"

if __name__ == "__main__":
    # 在后台线程启动 Telegram 机器人
    threading.Thread(target=run_bot, daemon=True).start()
    # Flask 监听 Render 分配的端口
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает (Webhook)!")

if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()  # ← не добавляйте updater(None)
    application.add_handler(CommandHandler("start", start))

    port = int(os.environ.get("PORT", 10000))

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{URL}/{TOKEN}"
    )

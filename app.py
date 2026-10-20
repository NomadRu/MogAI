import os
import logging
from flask import Flask, request, Response, abort
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Настройка логирования
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Чтение переменных окружения (не записывайте токены в код!)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
# ВАЖНО: замените на ваш фактический домен Render, например https://my-bot.onrender.com
# Не добавляйте косую черту в конце
URL = os.environ.get("RENDER_EXTERNAL_URL") 

if not TOKEN:
    raise ValueError("Отсутствует переменная окружения TELEGRAM_TOKEN")

# Инициализация Flask
flask_app = Flask(__name__)

# Создание приложения Telegram (updater=None, так как webhook не требует активного опроса)
telegram_app = Application.builder().token(TOKEN).updater(None).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    await update.message.reply_text("Привет! Бот успешно запущен и работает через Webhook.")

# Регистрация обработчиков
telegram_app.add_handler(CommandHandler("start", start))

@flask_app.route("/telegram", methods=["POST"])
async def telegram_webhook():
    """Приём обновлений, отправленных из Telegram"""
    if request.headers.get("content-type") != "application/json":
        abort(403)
    
    # Помещение обновления в очередь обработки
    await telegram_app.update_queue.put(
        Update.de_json(data=request.get_json(force=True), bot=telegram_app.bot)
    )
    return Response(status=200)

@flask_app.route("/")
def index():
    return "Bot is running (Webhook mode)"

@flask_app.route("/health")
def health():
    return "OK"

async def main():
    """Запуск приложения и установка Webhook"""
    await telegram_app.initialize()
    await telegram_app.start()
    
    # Установка Webhook, указывающего на ваш сервис Render
    # Обратите внимание: Telegram требует HTTPS
    webhook_url = f"{URL}/telegram"
    await telegram_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook установлен на: {webhook_url}")

    # Запуск Flask с использованием асинхронного сервера (рекомендуется ASGI, здесь используется WSGI-обёртка для упрощения)
    # На практике Render предоставляет PORT, необходимо убедиться, что сервис слушает этот порт
    port = int(os.environ.get("PORT", 10000))
    # Примечание: для простоты здесь используется встроенный сервер Flask, в производственной среде рекомендуется Gunicorn+Uvicorn
    # Но поскольку это простой бот, обычно достаточно и этого
    from werkzeug.serving import run_simple
    # Запуск в отдельном потоке или с использованием асинхронного решения... 
    # Для минимальной рабочей версии проще всего использовать run_webhook из PTB, но это требует изменения требований
    # Здесь продолжается использование Flask, но необходимо убедиться, что асинхронные задачи выполняются
    # Более надёжный способ — использовать run_webhook напрямую, см. примечание ниже

if __name__ == "__main__":
    # Для простоты и надёжности на Render рекомендуется использовать встроенный run_webhook из python-telegram-bot
    # Приведённый выше Flask-код подходит для пользователей, знакомых с ASGI
    # Самый простой надёжный способ:
    port = int(os.environ.get("PORT", 10000))
    webhook_url = f"{URL}/telegram" # Здесь нужен полный URL
    
    # Использование встроенного метода (упрощает многие вопросы обработки)
    # Примечание: этот метод заблокирует процесс, но именно это и нужно Render
    telegram_app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN, # Использование TOKEN в качестве пути для безопасности
        webhook_url=f"{URL}/{TOKEN}"
    )

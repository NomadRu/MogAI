import os
import base64
import requests
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)

# Настройка логирования
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем переменные окружения
TOKEN = os.environ.get("TELEGRAM_TOKEN")
URL = os.environ.get("RENDER_EXTERNAL_URL")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TOKEN or not OPENROUTER_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или OPENROUTER_API_KEY в переменных окружения!")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_main_keyboard():
    """Создает inline-клавиатуру с кнопками"""
    keyboard = [
        [InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def ask_ai(messages: list) -> str:
    """Отправляет текстовый запрос в OpenRouter"""
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4o-mini", # Хорошая и дешевая модель
                "messages": messages,
            },
            timeout=60
        )
        data = response.json()
        if "choices" not in data:
            return f"⚠️ Ошибка ИИ: {data.get('error', {}).get('message', 'Неизвестная ошибка')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Ошибка сети: {str(e)}"

def ask_ai_with_image(image_b64: str, user_text: str) -> str:
    """Отправляет изображение и текст в OpenRouter (Vision)"""
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "google/gemini-2.5-flash", # Дешевая модель, понимающая картинки
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]
                }],
            },
            timeout=60
        )
        data = response.json()
        if "choices" not in data:
            return f"⚠️ Ошибка ИИ: {data.get('error', {}).get('message', 'Неизвестная ошибка')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Ошибка сети: {str(e)}"

# --- ОБРАБОТЧИКИ КОМАНД ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сбрасываем историю при команде /start
    context.user_data["history"] = []
    await update.message.reply_text(
        "Привет! Я MogAI. Отправь мне текст, фото или текстовый файл.\n"
        "Используй кнопки ниже для управления.",
        reply_markup=get_main_keyboard()
    )

# --- ОБРАБОТЧИКИ КНОПОК ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Обязательно отвечаем на callback
    
    if query.data == "new_chat":
        context.user_data["history"] = []
        await query.edit_message_text(
            "🧹 История очищена. Новый чат начат! Напиши что-нибудь.",
            reply_markup=get_main_keyboard()
        )
    elif query.data == "help":
        text = (
            "🤖 **Что я умею:**\n"
            "• Отвечать на текстовые сообщения (помню контекст)\n"
            "• Анализировать фотографии\n"
            "• Читать текстовые файлы (.txt, .py, .json и т.д.)\n\n"
            "Кнопка «Новый чат» сбрасывает память диалога."
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    # Получаем историю (если нет - создаем)
    history = context.user_data.get("history", [])
    
    # Добавляем сообщение пользователя
    history.append({"role": "user", "content": user_text})
    
    # Ограничиваем историю (чтобы не превысить лимит токенов)
    if len(history) > 12:
        history = history[-12:]
    
    await update.message.chat.send_action(action="typing")
    ai_response = ask_ai(history)
    
    # Добавляем ответ ИИ в историю
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    
    await update.message.reply_text(ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="typing")
    
    # Берем фото наивысшего качества
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    
    # Кодируем в base64
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    
    # Берем подпись к фото или дефолтный вопрос
    caption = update.message.caption or "Что изображено на этом фото? Опиши подробно."
    
    ai_response = ask_ai_with_image(image_b64, caption)
    await update.message.reply_text(ai_response)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    
    # Лимит 5 МБ
    if doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("⚠️ Файл слишком большой (макс. 5 МБ).")
        return
    
    await update.message.chat.send_action(action="typing")
    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()
    
    try:
        text_content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        await update.message.reply_text("⚠️ Не могу прочитать этот файл как текст (возможно, бинарный формат).")
        return
    
    # Обрезаем текст
    if len(text_content) > 12000:
        text_content = text_content[:12000] + "\n\n...[файл обрезан, так как слишком длинный]"
    
    prompt = f"Пользователь прислал файл `{doc.file_name}`. Вот его содержимое:\n\n{text_content}\n\nПроанализируй этот файл или ответь на вопросы, если они есть внутри."
    
    # Для файлов используем текстовую модель без сохранения в историю (чтобы не забивать память)
    history = [{"role": "user", "content": prompt}]
    ai_response = ask_ai(history)
    
    await update.message.reply_text(ai_response)

# --- ЗАПУСК ---

if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()
    
    # Регистрация команд и кнопок
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Регистрация сообщений (важен порядок: команды, фото, файлы, текст)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.TEXT, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Запуск на порту {port}...")

    # Запуск через webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{URL}/{TOKEN}"
        )

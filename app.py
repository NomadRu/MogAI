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

# Переменные окружения
TOKEN = os.environ.get("TELEGRAM_TOKEN")
URL = os.environ.get("RENDER_EXTERNAL_URL")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if not TOKEN or not GEMINI_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY в переменных окружения!")

# Список моделей: от самой стабильной к более мощным
GEMINI_MODELS = [
    "gemini-3.1-flash-lite",   # 1-я попытка (самая легкая и стабильная)
    "gemini-3.5-flash",        # 2-я попытка (если первая упала)
    "gemini-3.8-flash"         # 3-я попытка
]

# --- КЛАВИАТУРЫ ---

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat")],
        [InlineKeyboardButton("🔄 Сбросить (Reset)", callback_data="reset_confirm")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_reset_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Да, начать заново", callback_data="reset_yes")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="reset_no")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- ФУНКЦИИ ЗАПРОСОВ К ИИ ---

def ask_ai(messages: list) -> str:
    """Отправляет текстовый запрос в Gemini с перебором моделей"""
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(
                url=url,
                headers={"Content-Type": "application/json"},
                json={"contents": contents},
                timeout=30
            )
            data = response.json()

            if "error" in data:
                code = data["error"].get("code")
                # Если перегрузка (503) или лимит (429) — пробуем следующую модель
                if code in (503, 429):
                    logger.warning(f"Модель {model} перегружена. Пробуем следующую...")
                    continue 
                return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"

            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]

        except Exception as e:
            logger.error(f"Сбой на модели {model}: {e}")
            continue

    return "⚠️ Все доступные модели сейчас перегружены. Попробуйте позже."

def ask_ai_with_image(image_b64: str, user_text: str) -> str:
    """Отправляет изображение и текст в Gemini с перебором моделей"""
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(
                url=url,
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{
                        "parts": [
                            {"text": user_text},
                            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
                        ]
                    }]
                },
                timeout=30
            )
            data = response.json()

            if "error" in data:
                code = data["error"].get("code")
                if code in (503, 429):
                    logger.warning(f"Модель {model} перегружена (фото). Пробуем следующую...")
                    continue
                return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"

            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]

        except Exception as e:
            logger.error(f"Сбой на модели {model} (фото): {e}")
            continue

    return "⚠️ Не удалось обработать фото: все модели перегружены."

# --- КОМАНДЫ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка /start"""
    context.user_data["history"] = []
    
    welcome_text = (
        "👋 **Привет! Я MogAI — твой умный помощник.**\n\n"
        "Я умею:\n"
        "💬 Отвечать на любые вопросы (помню контекст беседы)\n"
        "🖼️ Анализировать фото (отправь мне картинку)\n"
        "📄 Читать текстовые файлы (.txt, .py, .json)\n\n"
        "**Задай мне любой вопрос прямо сейчас**, или используй кнопки ниже для управления!"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /reset (запрос подтверждения)"""
    await update.message.reply_text(
        "⚠️ **Вы уверены, что хотите начать новый чат?**\n"
        "История текущего диалога будет безвозвратно удалена.",
        reply_markup=get_reset_keyboard(),
        parse_mode="Markdown"
    )

# --- ОБРАБОТЧИКИ КНОПОК ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "reset_confirm":
        await query.edit_message_text(
            "⚠️ **Вы уверены, что хотите начать новый чат?**\n"
            "История текущего диалога будет удалена.",
            reply_markup=get_reset_keyboard(),
            parse_mode="Markdown"
        )
        
    elif query.data == "reset_yes":
        context.user_data["history"] = []
        await query.edit_message_text(
            "🧹 История очищена. Новый чат начат!\nЗадай свой первый вопрос.",
            reply_markup=get_main_keyboard()
        )
        
    elif query.data == "reset_no":
        await query.edit_message_text(
            "👍 Отмена. Ваш текущий чат сохранен.",
            reply_markup=get_main_keyboard()
        )
        
    elif query.data == "new_chat":
        context.user_data["history"] = []
        await query.edit_message_text(
            "🧹 Новый чат начат! Напиши что-нибудь.",
            reply_markup=get_main_keyboard()
        )
        
    elif query.data == "help":
        text = (
            "🤖 **Что я умею:**\n"
            "• Отвечать на текстовые сообщения (помню контекст)\n"
            "• Анализировать фотографии\n"
            "• Читать текстовые файлы\n\n"
            "**Команды:**\n"
            "/start — перезапустить бота\n"
            "/reset — очистить историю с подтверждением"
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    history = context.user_data.get("history", [])
    
    history.append({"role": "user", "content": user_text})
    if len(history) > 12:
        history = history[-12:]
    
    await update.message.chat.send_action(action="typing")
    ai_response = ask_ai(history)
    
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    
    await update.message.reply_text(ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="typing")
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    
    caption = update.message.caption or "Что изображено на этом фото? Опиши подробно."
    ai_response = ask_ai_with_image(image_b64, caption)
    await update.message.reply_text(ai_response)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("⚠️ Файл слишком большой (макс. 5 МБ).")
        return
    
    await update.message.chat.send_action(action="typing")
    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()
    
    try:
        text_content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        await update.message.reply_text("⚠️ Не могу прочитать этот файл как текст.")
        return
    
    if len(text_content) > 12000:
        text_content = text_content[:12000] + "\n\n...[файл обрезан]"
    
    prompt = f"Пользователь прислал файл `{doc.file_name}`. Содержимое:\n\n{text_content}\n\nПроанализируй этот файл."
    ai_response = ask_ai([{"role": "user", "content": prompt}])
    await update.message.reply_text(ai_response)

# --- ЗАПУСК ---

if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.TEXT, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Запуск на порту {port}...")

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{URL}/{TOKEN}"
)

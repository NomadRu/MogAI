import os
import base64
import requests
import logging
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
URL = os.environ.get("RENDER_EXTERNAL_URL")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if not TOKEN or not GEMINI_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY!")

# Модели для ТЕКСТА (быстрые)
GEMINI_TEXT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
# Модели для ФОТО (мощные, видят картинки)
GEMINI_VISION_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash"]

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

# --- ЗАПРОСЫ К GEMINI ---
def ask_ai(messages: list) -> str:
    contents = []
    system_instruction = (
        "Ты — MogAI. Отвечай текстом. Используй HTML-теги для форматирования (например, <b>жирный</b>, <i>курсив</i>). "
        "Если нужно создать таблицу, используй форматирование Markdown (символ | для колонок и - для разделителей). "
        "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выводить JSON-код."
    )
    contents.append({"role": "user", "parts": [{"text": system_instruction}]})
    
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    for model in GEMINI_TEXT_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(url=url, headers={"Content-Type": "application/json"}, json={"contents": contents}, timeout=30)
            data = response.json()
            if "error" in data:
                if data["error"].get("code") in (503, 429): continue 
                return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "⚠️ Все модели перегружены. Попробуйте позже."

def ask_ai_with_image(image_b64: str, user_text: str) -> str:
    for model in GEMINI_VISION_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(
                url=url, headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": user_text}, {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}]}]},
                timeout=90 
            )
            data = response.json()
            if "error" in data:
                if data["error"].get("code") in (503, 429, 500): continue
                return f"⚠️ Ошибка Gemini (фото): {data['error'].get('message')}"
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.error(f"Сбой на модели {model} (фото): {e}")
            continue
    return "⚠️ Не удалось обработать фото. Сфотографируйте ближе или меньше задач."

# --- УМНАЯ ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
    # Если текст огромный, сохраняем в файл (чтобы не сломать Telegram)
    if len(text) > 3500:
        file_stream = io.BytesIO(text.encode('utf-8'))
        file_stream.name = "Reshenie.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Ответ длинный, сохранил в файл!")
        return

    try:
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Ошибка HTML: {e}. Отправляем как обычный текст.")
        await update.message.reply_text(text)

# --- КОМАНДЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    welcome_text = (
        "👋 <b>Привет! Я MogAI.</b>\n\n"
        "Я умею:\n"
        "💬 Отвечать на вопросы\n"
        "📊 <b>Создавать таблицы</b> (напиши «сделай таблицу...»)\n"
        "📄 <b>Создавать .txt файлы</b>\n"
        "🧮 <b>Решать задачи по фото</b> (пришли фото и напиши «реши»)\n\n"
        "<b>Задай вопрос или используй кнопки!</b>"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ <b>Вы уверены, что хотите начать новый чат?</b>",
        reply_markup=get_reset_keyboard(), parse_mode="HTML"
    )

# --- КНОПКИ ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "reset_confirm":
        await query.edit_message_text("⚠️ Вы уверены?", reply_markup=get_reset_keyboard(), parse_mode="HTML")
    elif query.data == "reset_yes":
        context.user_data["history"] = []
        await query.edit_message_text("🧹 История очищена. Новый чат!", reply_markup=get_main_keyboard())
    elif query.data == "reset_no":
        await query.edit_message_text("👍 Отмена. Чат сохранен.", reply_markup=get_main_keyboard())
    elif query.data == "new_chat":
        context.user_data["history"] = []
        await query.edit_message_text("🧹 Новый чат начат!", reply_markup=get_main_keyboard())
    elif query.data == "help":
        text = (
            "🤖 <b>Что я умею:</b>\n"
            "• /reset — сброс памяти\n"
            "• <b>Таблицы:</b> «сделай таблицу расходов на месяц»\n"
            "• <b>Файлы:</b> «создай файл: Привет!»\n"
            "• <b>Фото:</b> пришли фото задачи и напиши «реши»."
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- ОБРАБОТЧИКИ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    
    # 1. СОЗДАНИЕ ФАЙЛА ВРУЧНУЮ
    if "создай файл" in user_text or "сделай файл" in user_text:
        await update.message.chat.send_action(action="upload_document")
        content = user_text.split("файл", 1)[1].replace(":", "").strip()
        if not content: content = "Пустой файл."
        file_stream = io.BytesIO(content.encode('utf-8'))
        file_stream.name = "MogAI_File.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Файл готов!")
        return

    # 2. ОБЫЧНЫЙ ЧАТ (включая таблицы)
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 12: history = history[-12:]
    
    await update.message.chat.send_action(action="typing")
    ai_response = ask_ai(history)
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    
    await send_safe_message(update, ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="upload_photo")
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    
    caption = update.message.caption or "Реши задачи на фото. Если ответ длинный, оформи его пошагово."
    
    ai_response = ask_ai_with_image(image_b64, caption)
    await send_safe_message(update, ai_response)

# --- ЗАПУСК ---
if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{URL}/{TOKEN}")

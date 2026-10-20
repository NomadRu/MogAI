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
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TOKEN or not GEMINI_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY!")

GEMINI_TEXT_MODEL = "gemini-3.1-flash-lite"
OPENROUTER_VISION_MODEL = "qwen/qwen-2-vl-72b-instruct:free"

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

# --- ЗАПРОСЫ ---
def ask_gemini_text(messages: list) -> str:
    contents = []
    system_instruction = "Ты — MogAI. Отвечай кратко. Используй HTML-теги (<b>жирный</b>)."
    contents.append({"role": "user", "parts": [{"text": system_instruction}]})
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        response = requests.post(url=url, headers={"Content-Type": "application/json"}, 
            json={"contents": contents, "generationConfig": {"maxOutputTokens": 600}}, timeout=20)
        data = response.json()
        if "error" in data:
            return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"
        if "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"⚠️ Ошибка: {e}"
    return "⚠️ Не удалось получить ответ."

def ask_openrouter_vision(image_b64: str, user_text: str, history: list) -> str:
    if not OPENROUTER_KEY:
        return "⚠️ Для фото нужен OPENROUTER_API_KEY."
    messages = []
    for msg in history[-4:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": [
        {"type": "text", "text": user_text or "Что на фото?"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
    ]})
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": OPENROUTER_VISION_MODEL, "messages": messages, "max_tokens": 800}, timeout=40)
        data = response.json()
        if "error" in data:
            return f"⚠️ Ошибка Vision: {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Сбой: {str(e)}"

# --- ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
    if len(text) > 3500:
        file_stream = io.BytesIO(text.encode('utf-8'))
        file_stream.name = "Reshenie.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Ответ в файле.")
        return
    try:
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(text)

# --- ХЕНДЛЕРЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("👋 <b>Привет! Я MogAI.</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Вы уверены?</b>", reply_markup=get_reset_keyboard(), parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "reset_confirm":
        await query.edit_message_text("⚠️ Вы уверены?", reply_markup=get_reset_keyboard(), parse_mode="HTML")
    elif query.data == "reset_yes":
        context.user_data["history"] = []
        await query.edit_message_text("🧹 История очищена.", reply_markup=get_main_keyboard())
    elif query.data == "reset_no":
        await query.edit_message_text("👍 Отмена.", reply_markup=get_main_keyboard())
    elif query.data == "new_chat":
        context.user_data["history"] = []
        await query.edit_message_text("🧹 Новый чат!", reply_markup=get_main_keyboard())
    elif query.data == "help":
        await query.edit_message_text("🤖 Просто напиши вопрос или пришли фото.", reply_markup=get_main_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    if "создай файл" in user_text:
        await update.message.chat.send_action(action="upload_document")
        content = user_text.split("файл", 1)[1].replace(":", "").strip()
        file_stream = io.BytesIO(content.encode('utf-8'))
        file_stream.name = "MogAI_File.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Файл готов!")
        return

    await update.message.chat.send_action(action="typing")
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 8: history = history[-8:]
    ai_response = ask_gemini_text(history)
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    await send_safe_message(update, ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="typing")
    try:
        photo_file = await update.message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        if len(file_bytes) > 4 * 1024 * 1024:
            await update.message.reply_text("⚠️ Фото больше 4 МБ.")
            return
        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
        caption = update.message.caption or "Что на фото?"
        history = context.user_data.get("history", [])
        ai_response = ask_openrouter_vision(image_b64, caption, history)
        history.append({"role": "user", "content": f"[Фото] {caption}"})
        history.append({"role": "assistant", "content": ai_response})
        context.user_data["history"] = history[-8:]
        await send_safe_message(update, ai_response)
    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("⚠️ Не удалось скачать фото.")

# --- ЗАПУСК (WEBHOOK) ---
if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    
    # Запускаем через встроенный вебхук-сервер (он сам обрабатывает запросы и держит порт)
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{URL}/{TOKEN}",
        drop_pending_updates=True
                       )

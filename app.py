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

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
URL = os.environ.get("RENDER_EXTERNAL_URL")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TOKEN or not GEMINI_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY!")

# Модели для текста (Gemini)
GEMINI_TEXT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]

# Модели для фото (OpenRouter Vision) - перебираются по очереди
OPENROUTER_VISION_MODELS = [
    "qwen/qwen-2-vl-72b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "qwen/qwen-2.5-vl-7b-instruct:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free" # Добавил на всякий случай
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

# --- ЗАПРОСЫ К GEMINI (ТЕКСТ) ---
def ask_gemini_text(messages: list) -> str:
    contents = []
    system_instruction = "Ты — MogAI. Отвечай текстом. Используй HTML-теги (<b>жирный</b>)."
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
    return "⚠️ Текстовые модели перегружены. Попробуйте позже."

# --- ЗАПРОСЫ К OPENROUTER (ФОТО) ---
def ask_openrouter_vision(image_b64: str, user_text: str, history: list) -> str:
    if not OPENROUTER_KEY:
        return "⚠️ Для чтения фото нужен OPENROUTER_API_KEY в настройках Render."

    messages = []
    # Берем последние сообщения для контекста
    for msg in history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
        
    # Добавляем текущее фото и текст
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": user_text or "Что на фото? Опиши или реши задачу."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]
    })

    # Перебираем модели
    for model in OPENROUTER_VISION_MODELS:
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 1500
                },
                timeout=60
            )
            data = response.json()
            
            if "error" in data:
                err_code = data["error"].get("code")
                err_msg = data["error"].get("message", "")
                logger.error(f"Ошибка модели {model}: {err_msg}")
                # Если модель недоступна (404) или перегружена (429/503) — пробуем следующую
                if err_code in (404, 429, 503) or "endpoints" in err_msg:
                    continue
                return f"⚠️ Ошибка OpenRouter: {err_msg}"
                
            return data["choices"][0]["message"]["content"]
            
        except Exception as e:
            logger.error(f"Сбой сети на модели {model}: {e}")
            continue

    return "⚠️ Все бесплатные Vision-модели сейчас недоступны. Попробуйте позже."

# --- УМНАЯ ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
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
        "💬 Отвечать на вопросы (Gemini)\n"
        "📊 <b>Создавать таблицы</b>\n"
        "📄 <b>Создавать .txt файлы</b>\n"
        "🧮 <b>Решать задачи по фото</b> (OpenRouter Vision)\n\n"
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
            "• <b>Таблицы:</b> «сделай таблицу расходов»\n"
            "• <b>Файлы:</b> «создай файл: Привет!»\n"
            "• <b>Фото:</b> пришли фото задачи и напиши «реши»."
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- ОБРАБОТЧИКИ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    
    if "создай файл" in user_text or "сделай файл" in user_text:
        await update.message.chat.send_action(action="upload_document")
        content = user_text.split("файл", 1)[1].replace(":", "").strip()
        if not content: content = "Пустой файл."
        file_stream = io.BytesIO(content.encode('utf-8'))
        file_stream.name = "MogAI_File.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Файл готов!")
        return

    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 12: history = history[-12:]
    
    await update.message.chat.send_action(action="typing")
    
    ai_response = ask_gemini_text(history)
    
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    
    await send_safe_message(update, ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action="upload_photo")
    
    try:
        # Берем фото с максимальным качеством
        photo_file = await update.message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        
        if len(file_bytes) > 4 * 1024 * 1024:
            await update.message.reply_text("⚠️ Фото слишком большое (больше 4 МБ).")
            return

        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
        caption = update.message.caption or "Что на фото? Опиши или реши."
        
        history = context.user_data.get("history", [])
        
        # Идем в OpenRouter
        ai_response = ask_openrouter_vision(image_b64, caption, history)
        
        # Сохраняем в историю
        history.append({"role": "user", "content": f"[Пользователь отправил фото] {caption}"})
        history.append({"role": "assistant", "content": ai_response})
        if len(history) > 12: history = history[-12:]
        context.user_data["history"] = history
        
        await send_safe_message(update, ai_response)
        
    except Exception as e:
        logger.error(f"Критическая ошибка при скачивании фото: {e}")
        await update.message.reply_text("⚠️ Не удалось скачать фото из Telegram.")

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

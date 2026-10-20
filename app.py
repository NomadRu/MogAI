import os
import base64
import requests
import logging
import urllib.parse
import io
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
# Для изменения фото (получите бесплатный токен на huggingface.co/settings/tokens)
HF_TOKEN = os.environ.get("HF_TOKEN") 

if not TOKEN or not GEMINI_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY в переменных окружения!")

GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash"
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

# --- ГЕНЕРАЦИЯ И ИЗМЕНЕНИЕ КАРТИНОК ---

def generate_image(prompt: str) -> str:
    """Генерирует картинку с нуля через бесплатный Pollinations.ai"""
    # Добавляем теги для качества
    full_prompt = f"{prompt}, masterpiece, high quality, detailed"
    encoded_prompt = urllib.parse.quote(full_prompt)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&seed={os.urandom(4).hex()}"

def modify_image_hf(image_b64: str, prompt: str) -> bytes:
    """Изменяет присланное фото через Hugging Face (InstructPix2Pix)"""
    API_URL = "https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    
    # Декодируем base64 обратно в байты
    image_bytes = base64.b64decode(image_b64)
    
    # Отправляем фото и текстовый промпт
    files = {"image": image_bytes}
    data = {"prompt": prompt}
    
    response = requests.post(API_URL, headers=headers, files=files, data=data, timeout=60)
    
    if response.status_code == 200:
        return response.content # Возвращаем готовые байты картинки
    else:
        raise Exception(f"HF API Error: {response.text}")

# --- ЗАПРОСЫ К GEMINI ---

def ask_ai(messages: list) -> str:
    contents = []
    system_instruction = (
        "Ты — MogAI. Отвечай простым текстом. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выводить JSON-код "
        "или писать команды вроде 'dalle.text2im'. Если просят нарисовать — скажи, что сейчас нарисуешь, "
        "но сам код не выводи."
    )
    contents.append({"role": "user", "parts": [{"text": system_instruction}]})
    
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(
                url=url, headers={"Content-Type": "application/json"},
                json={"contents": contents}, timeout=30
            )
            data = response.json()
            if "error" in data:
                if data["error"].get("code") in (503, 429):
                    continue 
                return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "⚠️ Все модели перегружены. Попробуйте позже."

def ask_ai_with_image(image_b64: str, user_text: str) -> str:
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        try:
            response = requests.post(
                url=url, headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": user_text}, {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}]}]},
                timeout=30
            )
            data = response.json()
            if "error" in data:
                if data["error"].get("code") in (503, 429): continue
                return f"⚠️ Ошибка Gemini: {data['error'].get('message')}"
            if "candidates" in data:
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "⚠️ Не удалось обработать фото."

# --- КОМАНДЫ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    welcome_text = (
        "👋 **Привет! Я MogAI.**\n\n"
        "Я умею:\n"
        "💬 Отвечать на вопросы\n"
        "🎨 **Рисовать картинки** (напиши «нарисуй кота»)\n"
        "🖌️ **Изменять фото** (пришли фото с подписью «сделай его синим»)\n"
        "📄 **Создавать .txt файлы** (напиши «создай файл: [твой текст]»)\n\n"
        "**Задай вопрос или используй кнопки!**"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ **Вы уверены, что хотите начать новый чат?**",
        reply_markup=get_reset_keyboard(), parse_mode="Markdown"
    )

# --- ОБРАБОТЧИКИ КНОПОК ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "reset_confirm":
        await query.edit_message_text("⚠️ Вы уверены?", reply_markup=get_reset_keyboard(), parse_mode="Markdown")
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
            "🤖 **Команды и фишки:**\n"
            "• `/reset` — сброс памяти\n"
            "• **Генерация:** «нарисуй закат над морем»\n"
            "• **Изменение фото:** пришли фото и напиши в подписи, что изменить\n"
            "• **Создание файла:** «создай файл: Привет, мир!»"
        )
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    
    # 1. ГЕНЕРАЦИЯ КАРТИНОК
    if "нарисуй" in user_text or "сгенерируй" in user_text:
        await update.message.chat.send_action(action="upload_photo")
        # Извлекаем промпт (убираем команду)
        prompt = user_text.replace("нарисуй", "").replace("сгенерируй", "").strip()
        if not prompt: prompt = "beautiful landscape"
        
        image_url = generate_image(prompt)
        await update.message.reply_photo(photo=image_url, caption=f"🎨 Вот твоя картинка: *{prompt}*", parse_mode="Markdown")
        return

    # 2. СОЗДАНИЕ TXT ФАЙЛА
    if "создай файл" in user_text or "сделай файл" in user_text:
        await update.message.chat.send_action(action="upload_document")
        # Извлекаем текст для файла
        content = user_text.split("файл", 1)[1].replace(":", "").strip()
        if not content: content = "Пустой файл."
        
        # Создаем файл в памяти
        file_stream = io.BytesIO(content.encode('utf-8'))
        file_stream.name = "MogAI_File.txt"
        
        await update.message.reply_document(document=file_stream, caption="📄 Твой файл готов!")
        return

    # 3. ОБЫЧНЫЙ ЧАТ
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})
    if len(history) > 12: history = history[-12:]
    
    await update.message.chat.send_action(action="typing")
    ai_response = ask_ai(history)
    history.append({"role": "assistant", "content": ai_response})
    context.user_data["history"] = history
    await update.message.reply_text(ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото: анализ или изменение"""
    photo_file = await update.message.photo[-1].get_file()
    file_bytes = await photo_file.download_as_bytearray()
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")
    
    caption = update.message.caption or ""
    
    # Если в подписи есть слово "измени" или "сделай", то используем Hugging Face
    if "измени" in caption.lower() or "сделай" in caption.lower():
        if not HF_TOKEN:
            await update.message.reply_text("⚠️ Для изменения фото нужен бесплатный токен Hugging Face. Добавьте HF_TOKEN в Render.")
            return
        
        await update.message.chat.send_action(action="upload_photo")
        try:
            # Отправляем в HF
            new_image_bytes = modify_image_hf(image_b64, caption)
            await update.message.reply_photo(photo=new_image_bytes, caption="🖌️ Фото изменено!")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка изменения: {e}")
    else:
        # Обычный анализ через Gemini
        await update.message.chat.send_action(action="typing")
        if not caption: caption = "Что на фото? Опиши."
        ai_response = ask_ai_with_image(image_b64, caption)
        await update.message.reply_text(ai_response)

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

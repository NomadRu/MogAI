import os
import base64
import requests
import logging
import io
import asyncio
import re
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
GEMINI_PROXY = "https://gemini-proxy.bold-violet-3c8d.workers.dev"
OPENROUTER_VISION_MODEL = "qwen/qwen2.5-vl-3b-instruct:free"
OPENROUTER_TEXT_MODEL = "openrouter/free"

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

# --- УМНАЯ КОНВЕРТАЦИЯ ТАБЛИЦЫ В CSV ---
def markdown_to_csv(text: str) -> str:
    """Находит Markdown-таблицу в тексте и превращает в нормальный CSV"""
    lines = text.split('\n')
    csv_lines = []
    in_table = False
    
    for line in lines:
        # Если строка похожа на часть таблицы
        if "|" in line and not line.strip().startswith('```'):
            in_table = True
            # Очищаем от крайних палок и разбиваем по палкам
            parts = [part.strip() for part in line.strip().strip('|').split('|')]
            
            # Пропускаем строки-разделители (типа |---|---|)
            if all(re.match(r'^:?-+:?$', p) for p in parts):
                continue
                
            # Оборачиваем в кавычки на случай, если внутри есть запятые
            csv_line = ",".join([f'"{p}"' for p in parts])
            csv_lines.append(csv_line)
        elif in_table and line.strip() == "":
            in_table = False
            
    if csv_lines:
        return "\n".join(csv_lines)
    return ""

# --- ЧИСТИЛЬЩИК ТЕКСТА ---
def format_text_for_telegram(text: str) -> str:
    # Сначала вырезаем код, чтобы его не испортить
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"__CODE_PLACEHOLDER_{len(code_blocks)-1}__"
    
    text = re.sub(r'```[\s\S]*?```', save_code_block, text)
    
    # Убираем Markdown-таблицы из текста (они уйдут в файл)
    text = re.sub(r'(\|.*\|)', '', text)
    text = re.sub(r'(-{3,}.*)', '', text)
    
    # Жирный и курсив
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
    
    # Заголовки
    text = re.sub(r'^#{1,6}\s+(.*)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    
    # Возвращаем код обратно
    for i, block in enumerate(code_blocks):
        clean_code = block.replace('```', '').strip()
        lines = clean_code.split('\n')
        if lines and lines[0].strip() in ['python', 'html', 'javascript', 'css', 'json', 'sql', 'cpp', 'java', 'php', 'bash']:
            lines = lines[1:]
        
        # ВАЖНО: Экранируем код, чтобы теги внутри не сломали HTML
        safe_code = '\n'.join(lines).replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')
        html_code = f"<pre><code>{safe_code}</code></pre>"
        text = text.replace(f"__CODE_PLACEHOLDER_{i}__", html_code)
        
    # Экранируем опасные символы в основном тексте
    text = text.replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')
    
    # Возвращаем наши HTML-теги
    for tag in ['b', 'i', 's', 'code', 'pre']:
        text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>').replace(f'&lt;/{tag}&gt;', f'</{tag}>')
    
    return text

# --- АНИМАЦИЯ ---
async def animate_thinking(update: Update, stop_event: asyncio.Event):
    try:
        msg = await update.message.reply_text("⏳ <b>MogAI думает...</b>", parse_mode="HTML")
        seconds = 0
        while not stop_event.is_set():
            await asyncio.sleep(2)
            seconds += 2
            if stop_event.is_set():
                break
            try:
                await msg.edit_text(f"⏳ <b>MogAI думает...</b> ({seconds} сек)", parse_mode="HTML")
            except Exception:
                pass
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception:
        pass

# --- ЗАПРОСЫ ---
def ask_openrouter_text(messages: list) -> str:
    if not OPENROUTER_KEY:
        return "⚠️ Для резервной модели нужен OPENROUTER_API_KEY."
    
    or_messages = [{"role": "system", "content": "Ты — MogAI. Отвечай текстом."}]
    for msg in messages[-6:]:
        or_messages.append({"role": msg["role"], "content": msg["content"]})
    
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": OPENROUTER_TEXT_MODEL, "messages": or_messages, "max_tokens": 3000},
            timeout=30
        )
        data = response.json()
        if "error" in data:
            return f"⚠️ OpenRouter: {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Сбой: {e}"

def ask_openrouter_vision(image_b64: str, user_text: str, history: list) -> str:
    if not OPENROUTER_KEY:
        return "⚠️ Для фото нужен OPENROUTER_API_KEY."

    messages = []
    for msg in history[-4:]: 
        messages.append({"role": msg["role"], "content": msg["content"]})
        
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": user_text or "Что на фото?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
        ]
    })

    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": OPENROUTER_VISION_MODEL, "messages": messages, "max_tokens": 3000},
            timeout=60
        )
        data = response.json()
        if "error" in data:
            return f"⚠️ Ошибка Vision: {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ Сбой: {str(e)}"

def ask_text_smart(messages: list) -> str:
    contents = []
    system_instruction = (
        "Ты — MogAI. Если нужно создать таблицу, используй Markdown (| и -).\n"
        "Если пишешь код, используй ```язык\nкод\n```."
    )
    contents.append({"role": "user", "parts": [{"text": system_instruction}]})
    
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    url = f"{GEMINI_PROXY}/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={GEMINI_KEY}"
    
    try:
        response = requests.post(url=url, headers={"Content-Type": "application/json"}, 
            json={"contents": contents, "generationConfig": {"maxOutputTokens": 3000}}, timeout=25)
        data = response.json()
        
        if "error" not in data and "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"]
            
        if data.get("error", {}).get("code") == 503:
            return ask_openrouter_text(messages)
            
        return f"⚠️ Ошибка Gemini: {data.get('error', {}).get('message')}"
        
    except Exception as e:
        return ask_openrouter_text(messages)

# --- ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
    # 1. Проверяем, есть ли таблица
    csv_content = markdown_to_csv(text)
    if csv_content:
        # Отправляем красивую таблицу в CSV
        file_stream = io.BytesIO(csv_content.encode('utf-8'))
        file_stream.name = "MogAI_Table.csv"
        await update.message.reply_document(
            document=file_stream, 
            caption="📊 Вот твоя таблица! Открой файл в Excel или Google Таблицах."
        )
        # Если после таблицы есть еще текст, отправим его тоже
        text_without_table = re.sub(r'(\|.*\|)', '', text)
        text_without_table = text_without_table.strip()
        if text_without_table:
            text = text_without_table
        else:
            return

    # 2. Если очень длинный
    if len(text) > 3500:
        file_stream = io.BytesIO(text.encode('utf-8'))
        file_stream.name = "MogAI_Otvet.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Длинный ответ в файле.")
        return

    # 3. Форматируем и отправляем
    formatted_text = format_text_for_telegram(text)
    if not formatted_text.strip():
        return # Если после чистки ничего не осталось

    try:
        await update.message.reply_text(formatted_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"HTML сломался: {e}. Отправляем как текст.")
        await update.message.reply_text(text)

# --- КОМАНДЫ И КНОПКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    welcome_text = (
        "👋 <b>Привет! Я MogAI.</b>\n\n"
        "Я умею:\n"
        "💬 Отвечать на вопросы\n"
        "📊 <b>Создавать таблицы</b> (отправлю файлом CSV)\n"
        "📄 <b>Создавать .txt файлы</b>\n"
        "🧮 <b>Решать задачи по фото</b>\n\n"
        "<b>Задай вопрос!</b>"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

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
        text = "🤖 <b>Команды:</b>\n• /reset — сброс\n• «сделай таблицу»\n• Пришли фото задачи."
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    if "создай файл" in user_text:
        await update.message.chat.send_action(action="upload_document")
        content = user_text.split("файл", 1)[1].replace(":", "").strip()
        file_stream = io.BytesIO(content.encode('utf-8'))
        file_stream.name = "MogAI_File.txt"
        await update.message.reply_document(document=file_stream, caption="📄 Файл готов!")
        return

    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(animate_thinking(update, stop_event))

    try:
        history = context.user_data.get("history", [])
        history.append({"role": "user", "content": user_text})
        if len(history) > 8: history = history[-8:]
        ai_response = await asyncio.to_thread(ask_text_smart, history)
        history.append({"role": "assistant", "content": ai_response})
        context.user_data["history"] = history
    finally:
        stop_event.set()
        await animation_task

    await send_safe_message(update, ai_response)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(animate_thinking(update, stop_event))

    try:
        photo_file = await update.message.photo[-1].get_file()
        file_bytes = await photo_file.download_as_bytearray()
        if len(file_bytes) > 4 * 1024 * 1024:
            stop_event.set()
            await animation_task
            await update.message.reply_text("⚠️ Фото больше 4 МБ.")
            return

        image_b64 = base64.b64encode(file_bytes).decode("utf-8")
        caption = update.message.caption or "Что на фото?"
        history = context.user_data.get("history", [])
        ai_response = await asyncio.to_thread(ask_openrouter_vision, image_b64, caption, history)
        history.append({"role": "user", "content": f"[Фото] {caption}"})
        history.append({"role": "assistant", "content": ai_response})
        context.user_data["history"] = history[-8:]
    finally:
        stop_event.set()
        await animation_task

    await send_safe_message(update, ai_response)

if __name__ == "__main__":
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    application.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{URL}/{TOKEN}", drop_pending_updates=True)

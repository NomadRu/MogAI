import os
import base64
import requests
import logging
import io
import asyncio
import re
import time
import traceback
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
URL = os.environ.get("RENDER_EXTERNAL_URL")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TOKEN:
    raise ValueError("Не задан TELEGRAM_TOKEN!")

# Собираем все ключи Google (от 1 до 5)
def get_gemini_keys():
    keys = []
    for i in range(1, 6):
        key = os.environ.get(f"GEMINI_KEY_{i}")
        if key:
            keys.append(key)
    return keys

GEMINI_TEXT_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
GEMINI_VISION_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
GEMINI_PROXY = "https://gemini-proxy.bold-violet-3c8d.workers.dev"

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat")],
        [InlineKeyboardButton("🔄 Сбросить (Reset)", callback_data="reset_confirm")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ])

def get_reset_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, начать заново", callback_data="reset_yes")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="reset_no")]
    ])

def get_stop_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Остановить", callback_data="stop_thinking")]])

# --- ПАРСЕР ТАБЛИЦ В PNG ---
def markdown_to_png(text: str) -> io.BytesIO:
    if not HAS_MATPLOTLIB: return None
    try:
        lines = text.split('\n')
        table_data = []
        for line in lines:
            clean_line = line.strip()
            if "|" in clean_line and not clean_line.startswith('```'):
                parts = [part.strip() for part in clean_line.strip('|').split('|')]
                if all(re.match(r'^:?-+:?$', p) for p in parts if p): continue
                if any(p for p in parts): table_data.append(parts)
        if len(table_data) < 2: return None
        max_cols = max(len(row) for row in table_data)
        for row in table_data:
            while len(row) < max_cols: row.append("")
        fig, ax = plt.subplots(figsize=(10, max(2, len(table_data) * 0.5)))
        ax.axis('tight'); ax.axis('off')
        table = ax.table(cellText=table_data[1:], colLabels=table_data[0], cellLoc='center', loc='center')
        table.auto_set_font_size(False); table.set_fontsize(10); table.scale(1.2, 1.5)
        for (row, col), cell in table.get_celld().items():
            if row == 0:
                cell.set_text_props(weight='bold', color='white'); cell.set_facecolor('#4a4a4a')
            else: cell.set_facecolor('#f0f0f0' if row % 2 == 0 else 'white')
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=200)
        buf.seek(0); plt.close(fig)
        return buf
    except Exception: return None

# --- ЧИСТИЛЬЩИК ТЕКСТА ---
def format_text_for_telegram(text: str) -> str:
    try:
        code_blocks = []
        def save_code(match):
            code = match.group(2).strip(); lang = match.group(1).strip() if match.group(1) else ""
            code_blocks.append((lang, code)); return f"%%%CODE_{len(code_blocks)-1}%%%"
        text = re.sub(r'```([a-zA-Z0-9]*)\n?([\s\S]*?)```', save_code, text)
        text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
        text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
        text = re.sub(r'_(.*?)_', r'<i>\1</i>', text)
        text = re.sub(r'^#{1,6}\s+(.*)$', r'<b>\1</b>', text, flags=re.MULTILINE)
        text = text.replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')
        for tag in ['b', 'i', 's', 'code', 'pre']:
            text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>').replace(f'&lt;/{tag}&gt;', f'</{tag}>')
        for i, (lang, code) in enumerate(code_blocks):
            safe_code = code.replace('&', '&amp;').replace('>', '&gt;').replace('<', '&lt;')
            lang_class = f' class="language-{lang}"' if lang else ''
            html_code = f"<pre><code{lang_class}>{safe_code}</code></pre>"
            text = text.replace(f"%%%CODE_{i}%%%", html_code)
        return text.strip()
    except Exception: return text

# --- АНИМАЦИЯ ---
async def animate_thinking(update: Update, stop_event: asyncio.Event):
    try:
        msg = await update.message.reply_text("⏳ <b>MogAI думает...</b>", parse_mode="HTML", reply_markup=get_stop_keyboard())
        seconds = 0
        while not stop_event.is_set():
            await asyncio.sleep(2); seconds += 2
            if stop_event.is_set(): break
            try: await msg.edit_text(f"⏳ <b>MogAI думает...</b> ({seconds} сек)", parse_mode="HTML", reply_markup=get_stop_keyboard())
            except Exception: pass
    except Exception: pass

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отменяю...")
    if "stop_event" in context.user_data: context.user_data["stop_event"].set()
    try: await query.edit_message_text("⏹ <b>Запрос отменён.</b>", parse_mode="HTML")
    except Exception: pass

# --- УМНЫЕ ЗАПРОСЫ С ПЕРЕБОРОМ КЛЮЧЕЙ ---
def ask_gemini_text(messages: list) -> str:
    contents = []
    system_instruction = "Ты — MogAI. Отвечай обычным текстом. Используй таблицы ТОЛЬКО если попросили."
    contents.append({"role": "user", "parts": [{"text": system_instruction}]})
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    keys = get_gemini_keys()
    if not keys:
        return ask_openrouter_text(messages)

    for key_index, key in enumerate(keys):
        for model in GEMINI_TEXT_MODELS:
            url = f"{GEMINI_PROXY}/v1beta/models/{model}:generateContent?key={key}"
            try:
                response = requests.post(url=url, headers={"Content-Type": "application/json"}, 
                    json={"contents": contents, "generationConfig": {"maxOutputTokens": 3000}}, timeout=20)
                data = response.json()
                
                if "error" not in data and "candidates" in data:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                
                if data.get("error", {}).get("code") == 429:
                    logger.warning(f"Ключ №{key_index + 1} исчерпан. Переключаюсь...")
                    break # Переходим к следующему ключу
            except Exception: continue

    logger.warning("Все ключи Google исчерпаны. Иду в OpenRouter.")
    return ask_openrouter_text(messages)

def ask_gemini_vision(image_b64: str, user_text: str) -> str:
    keys = get_gemini_keys()
    if not keys: return "⚠️ Нет ключей Google."

    for key_index, key in enumerate(keys):
        for model in GEMINI_VISION_MODELS:
            url = f"{GEMINI_PROXY}/v1beta/models/{model}:generateContent?key={key}"
            try:
                response = requests.post(url=url, headers={"Content-Type": "application/json"}, 
                    json={"contents": [{"parts": [{"text": user_text}, {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}]}]}, timeout=40)
                data = response.json()
                if "error" not in data and "candidates" in data:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                if data.get("error", {}).get("code") == 429:
                    break # Следующий ключ
            except Exception: continue
    return "⚠️ Все ключи Google для фото исчерпаны."

def ask_openrouter_text(messages: list) -> str:
    if not OPENROUTER_KEY: return "⚠️ Все ключи Google исчерпаны, а OpenRouter не настроен."
    or_messages = [{"role": "system", "content": "Ты — MogAI. Отвечай текстом."}]
    for msg in messages[-6:]: or_messages.append({"role": msg["role"], "content": msg["content"]})
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={"model": "openrouter/free", "messages": or_messages, "max_tokens": 3000}, timeout=30)
        data = response.json()
        if "error" in data: return f"⚠️ OpenRouter: {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except Exception as e: return f"⚠️ Сбой: {e}"

# --- СОЗДАНИЕ РЕФЕРАТА (С ПЕРЕБОРОМ КЛЮЧЕЙ) ---
def create_gost_document(topic: str) -> io.BytesIO:
    prompt = (
        f"Напиши текст для реферата на тему: '{topic}'. "
        "Структура: Введение, Основная часть, Заключение. Объем: 1000-1500 слов. "
        "Пиши чистым текстом без Markdown. Заголовки пиши ЗАГЛАВНЫМИ буквами."
    )
    
    raw_text = ""
    keys = get_gemini_keys()

    # Ищем текст в Google
    for key_index, key in enumerate(keys):
        for model in GEMINI_TEXT_MODELS:
            url = f"{GEMINI_PROXY}/v1beta/models/{model}:generateContent?key={key}"
            try:
                response = requests.post(url=url, headers={"Content-Type": "application/json"}, 
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 4000}}, timeout=60)
                data = response.json()
                if "error" not in data and "candidates" in data:
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    break
                if data.get("error", {}).get("code") == 429:
                    break 
            except Exception: continue
        if raw_text: break

    # Если Google не смог, идем в OpenRouter
    if not raw_text and OPENROUTER_KEY:
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
                json={"model": "openrouter/free", "messages": [{"role": "user", "content": prompt}], "max_tokens": 4000},
                timeout=90
            )
            data = response.json()
            if "error" not in data and "choices" in data:
                raw_text = data["choices"][0]["message"]["content"]
        except Exception: pass

    if not raw_text: return None

    # Очистка и сборка Word
    raw_text = re.sub(r'[*#_`~]', '', raw_text)
    raw_text = "".join(c for c in raw_text if c.isprintable() or c in '\n\r\t')

    try:
        doc = Document()
        style = doc.styles['Normal']
        style.font.name = 'Times New Roman'
        style.font.size = Pt(14)
        
        for section in doc.sections:
            section.left_margin = Cm(3)
            section.right_margin = Cm(1.5)
            section.top_margin = Cm(2)
            section.bottom_margin = Cm(2)
            
        def add_gost_paragraph(text, is_heading=False):
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            p.paragraph_format.first_line_indent = Cm(1.25)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            if is_heading:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Cm(0)
                run = p.add_run(text.upper())
                run.bold = True
            else:
                p.add_run(text)

        doc.add_paragraph().add_run("РЕФЕРАТ").bold = True
        doc.add_paragraph(f"на тему: «{topic}»")
        doc.add_paragraph("\n\n\n\n\n\n\n\n\n\n")
        
        for line in raw_text.split('\n'):
            line = line.strip()
            if not line: continue
            if len(line) < 60 and (line.isupper() or line.endswith(':')):
                add_gost_paragraph(line, is_heading=True)
                doc.add_paragraph()
            else:
                add_gost_paragraph(line)
                
        buf = io.BytesIO()
        doc.save(buf); buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка сборки DOCX: {e}")
        return None

# --- ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
    try:
        if "|" in text and "-" in text:
            png_buffer = markdown_to_png(text)
            if png_buffer:
                await update.message.reply_photo(photo=png_buffer, caption="📊 Вот твоя таблица!")
                text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE).strip()
                if not text: return
        if len(text) > 3500:
            file_stream = io.BytesIO(text.encode('utf-8'))
            file_stream.name = "MogAI_Otvet.txt"
            await update.message.reply_document(document=file_stream, caption="📄 Длинный ответ в файле.")
            return
        formatted_text = format_text_for_telegram(text)
        if not formatted_text.strip(): return
        try: await update.message.reply_text(formatted_text, parse_mode="HTML")
        except Exception: await update.message.reply_text(text)
    except Exception as e: logger.error(f"Ошибка отправки: {e}")

# --- ХЕНДЛЕРЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    text = (
        "👋 <b>Привет! Я MogAI.</b>\n\n"
        "Я умею:\n"
        "💬 Отвечать на вопросы\n"
        "📊 <b>Создавать таблицы</b> (PNG)\n"
        "📝 <b>Писать рефераты по ГОСТу</b> (.docx)\n"
        "🧮 <b>Решать задачи по фото</b>\n\n"
        "Напиши: <i>«напиши реферат на тему [тема]»</i>"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Вы уверены?</b>", reply_markup=get_reset_keyboard(), parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "stop_thinking":
        await stop_callback(update, context); return
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
        text = "🤖 <b>Команды:</b>\n• /reset — сброс\n• «напиши реферат про...»\n• Пришли фото задачи."
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = update.message.text.lower()
        
        if "реферат" in user_text or "доклад" in user_text:
            match = re.search(r'(?:на тему|про)\s+(.+)', user_text)
            topic = match.group(1) if match else "Интересные факты"
            
            stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
            anim = asyncio.create_task(animate_thinking(update, stop_event))
            try:
                await update.message.chat.send_action(action="upload_document")
                docx_buffer = await asyncio.to_thread(create_gost_document, topic)
                if stop_event.is_set(): return
                if docx_buffer:
                    await update.message.reply_document(
                        document=docx_buffer, filename=f"Referat_{topic[:10]}.docx", 
                        caption=f"📝 Реферат по ГОСТу: <b>{topic}</b>", parse_mode="HTML"
                    )
                else: 
                    await update.message.reply_text("⚠️ Не удалось создать реферат. Лимиты исчерпаны.")
            finally:
                stop_event.set(); await anim
            return

        stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
        anim = asyncio.create_task(animate_thinking(update, stop_event))
        try:
            history = context.user_data.get("history", [])
            history.append({"role": "user", "content": user_text})
            if len(history) > 8: history = history[-8:]
            ai_response = await asyncio.to_thread(ask_gemini_text, history)
            if stop_event.is_set(): return
            history.append({"role": "assistant", "content": ai_response})
            context.user_data["history"] = history
        finally:
            stop_event.set(); await anim
        await send_safe_message(update, ai_response)
    except Exception as e:
        logger.error(f"ОШИБКА: {e}\n{traceback.format_exc()}")
        try: await update.message.reply_text("⚠️ Ошибка, но бот жив.")
        except: pass

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
        anim = asyncio.create_task(animate_thinking(update, stop_event))
        try:
            photo_file = await update.message.photo[-1].get_file()
            file_bytes = await photo_file.download_as_bytearray()
            if len(file_bytes) > 4 * 1024 * 1024:
                stop_event.set(); await anim; await update.message.reply_text("⚠️ Фото больше 4 МБ."); return
            image_b64 = base64.b64encode(file_bytes).decode("utf-8")
            caption = update.message.caption or "Что на фото?"
            history = context.user_data.get("history", [])
            ai_response = await asyncio.to_thread(ask_gemini_vision, image_b64, caption)
            if stop_event.is_set(): return
            history.append({"role": "user", "content": f"[Фото] {caption}"})
            history.append({"role": "assistant", "content": ai_response})
            context.user_data["history"] = history[-8:]
        finally:
            stop_event.set(); await anim
        await send_safe_message(update, ai_response)
    except Exception as e:
        logger.error(f"ОШИБКА ФОТО: {e}")
        try: await update.message.reply_text("⚠️ Ошибка при обработке фото.")
        except: pass

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{URL}/{TOKEN}", drop_pending_updates=True)

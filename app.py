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
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.enum.section import WD_ORIENT

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
GROQ_KEY = os.environ.get("GROQ_API_KEY")
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY")

if not TOKEN or not GROQ_KEY:
    raise ValueError("Не заданы TELEGRAM_TOKEN или GROQ_API_KEY!")

# --- МОДЕЛИ ---
GROQ_TEXT_MODELS = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MISTRAL_VISION_MODEL = "pixtral-12b-2409"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

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

# --- ГЕНЕРАЦИЯ ФОТО ---
def generate_image(prompt: str) -> str:
    encoded_prompt = requests.utils.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true&seed={os.urandom(4).hex()}"

# --- ПАРСЕР ТАБЛИЦ В PNG ---
def markdown_to_png(text: str) -> io.BytesIO:
    if not HAS_MATPLOTLIB: return None
    try:
        clean_text = re.sub(r'```[a-zA-Z]*\n?', '', text)
        clean_text = re.sub(r'```', '', clean_text)
        lines = clean_text.split('\n')
        table_data = []
        for line in lines:
            clean_line = line.strip()
            if "|" in clean_line:
                parts = [part.strip() for part in clean_line.strip('|').split('|')]
                if all(re.match(r'^:?-+:?$', p) for p in parts if p): continue
                if any(p for p in parts): table_data.append(parts)
                
        if len(table_data) < 2: return None
        for row in table_data:
            for i in range(len(row)):
                row[i] = row[i].replace('**', '').replace('*', '')

        max_cols = max(len(row) for row in table_data)
        for row in table_data:
            while len(row) < max_cols: row.append("")
            
        fig, ax = plt.subplots(figsize=(14, max(2, len(table_data) * 0.8)))
        ax.axis('tight'); ax.axis('off')
        table = ax.table(cellText=table_data[1:], colLabels=table_data[0], cellLoc='left', loc='center')
        table.auto_set_font_size(False); table.set_fontsize(8); table.scale(1, 2.5)
        
        for (row, col), cell in table.get_celld().items():
            cell.set_text_props(wrap=True)
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
            try: 
                await msg.edit_text(f"⏳ <b>MogAI думает...</b> ({seconds} сек)", parse_mode="HTML", reply_markup=get_stop_keyboard())
            except Exception:
                try:
                    await msg.delete()
                    msg = await update.message.reply_text(f"⏳ <b>MogAI думает...</b> ({seconds} сек)", parse_mode="HTML", reply_markup=get_stop_keyboard())
                except: pass
    except Exception: pass

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отменяю...")
    if "stop_event" in context.user_data: context.user_data["stop_event"].set()
    try: await query.edit_message_text("⏹ <b>Запрос отменён.</b>", parse_mode="HTML")
    except Exception: pass

# --- ЗАПРОСЫ К GROQ ---
def ask_groq_text(messages: list) -> str:
    system_prompt = "Ты — MogAI. Отвечай текстом. Таблицы используй только по прямой просьбе."
    or_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages[-8:]:
        or_messages.append({"role": msg["role"], "content": msg["content"]})

    for model in GROQ_TEXT_MODELS:
        try:
            response = requests.post(
                url=GROQ_API_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": or_messages, "max_tokens": 4000},
                timeout=40
            )
            data = response.json()
            if "error" not in data and "choices" in data:
                return data["choices"][0]["message"]["content"]
        except Exception: continue
    return "⚠️ Ошибка Groq."

# --- ЗАПРОСЫ К MISTRAL ---
def ask_mistral_vision(image_b64: str, user_text: str) -> str:
    if not MISTRAL_KEY: return "⚠️ Для фото нужен MISTRAL_API_KEY."

    if user_text:
        prompt = f"Пользователь прислал фото и спрашивает: '{user_text}'. Внимательно посмотри на фото и ответь на вопрос. Если на фото есть математика или схемы, используй LaTeX для формул."
    else:
        prompt = "Пользователь прислал фото. Подробно опиши, что на нем изображено. Начни ответ со слова 'ОПИСАНИЕ:'."

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            ]
        }
    ]

    try:
        response = requests.post(
            url=MISTRAL_API_URL,
            headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
            json={"model": MISTRAL_VISION_MODEL, "messages": messages, "max_tokens": 4000},
            timeout=120
        )
        data = response.json()
        if "error" in data: return f"⚠️ Ошибка Mistral (фото): {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "⚠️ Mistral думал слишком долго. Попробуйте сфотографировать меньший участок."
    except Exception as e: 
        return f"⚠️ Сбой Mistral (фото): {e}"

# --- СОЗДАНИЕ РЕФЕРАТА ПО ГОСТ (ОБНОВЛЕННАЯ) ---
def create_word_document(pages: int, topic: str) -> io.BytesIO:
    # 1. Просим ИИ написать текст
    # Считаем, что 1 страница ≈ 300 слов (с учетом 1.5 интервала и 14 шрифта)
    words_needed = pages * 300
    prompt = (
        f"Напиши текст для реферата на тему: '{topic}'. "
        f"Объем: примерно {words_needed} слов. "
        "Структура строго по ГОСТ: Введение (актуальность, цель, задачи), "
        "Основная часть (3 главы, в каждой 2 подглавы), Заключение, Список литературы. "
        "Пиши академическим языком. Не используй Markdown. "
        "Заголовки пиши с новой строки и выделяй их ЗАГЛАВНЫМИ буквами. "
        "Подзаголовки пиши с новой строки, начиная с номера (например, 1.1 Название)."
    )
    raw_text = ask_groq_text([{"role": "user", "content": prompt}])
    if not raw_text or "⚠️" in raw_text: return None

    # Очистка от возможных артефактов
    raw_text = re.sub(r'[*#_`~]', '', raw_text)
    raw_text = "".join(c for c in raw_text if c.isprintable() or c in '\n\r\t')

    # 2. Собираем Word по ГОСТ
    try:
        doc = Document()
        
        # Настройка стилей по ГОСТ
        style = doc.styles['Normal']
        style.font.name = 'Times New Roman'
        style.font.size = Pt(14)
        style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.first_line_indent = Cm(1.25)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        # Поля по ГОСТ (левое 3 см, правое 1.5 см, верхнее/нижнее 2 см)
        for section in doc.sections:
            section.left_margin = Cm(3)
            section.right_margin = Cm(1.5)
            section.top_margin = Cm(2)
            section.bottom_margin = Cm(2)

        # Функция добавления абзаца
        def add_paragraph(text, is_heading=False, is_center=False, is_bold=False):
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            p.paragraph_format.first_line_indent = Cm(1.25) if not is_center else Cm(0)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if not is_center else WD_ALIGN_PARAGRAPH.CENTER
            
            if is_heading:
                run = p.add_run(text.upper())
                run.bold = True
            elif is_bold:
                run = p.add_run(text)
                run.bold = True
            else:
                p.add_run(text)
            return p

        # --- ТИТУЛЬНЫЙ ЛИСТ ---
        # Верхний блок (Вуз)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("МИНИСТЕРСТВО ОБРАЗОВАНИЯ И НАУКИ РФ"); run.bold = True
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("НАЗВАНИЕ ВАШЕГО УЧЕБНОГО ЗАВЕДЕНИЯ"); run.bold = True
        
        # Отступы
        for _ in range(4): doc.add_paragraph()
        
        # Центральный блок (РЕФЕРАТ и тема)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("РЕФЕРАТ").bold = True
        p.runs[0].font.size = Pt(24)
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f"по теме: «{topic}»").bold = True
        p.runs[0].font.size = Pt(18)
        
        # Отступы
        for _ in range(6): doc.add_paragraph()
        
        # Правый блок (Выполнил/Проверил)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run("Выполнил: студент\nГруппы: ______\nФИО: ______________")
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run("Проверил: ______________")
        
        # Отступы
        for _ in range(8): doc.add_paragraph()
        
        # Нижний блок (Город, год)
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("Город, 2026 г.")
        
        doc.add_page_break() # Конец титульника

        # --- СОДЕРЖАНИЕ (ОГЛАВЛЕНИЕ) ---
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("СОДЕРЖАНИЕ").bold = True
        doc.add_paragraph()
        
        # Создаем оглавление с табуляцией (как в примере)
        toc_items = [
            ("ВВЕДЕНИЕ", "3"),
            ("ГЛАВА 1.", "4"),
            ("1.1.", "4"),
            ("1.2.", "5"),
            ("ГЛАВА 2.", "6"),
            ("2.1.", "6"),
            ("2.2.", "7"),
            ("ГЛАВА 3.", "8"),
            ("3.1.", "8"),
            ("3.2.", "9"),
            ("ЗАКЛЮЧЕНИЕ", "10"),
            ("СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ", "11")
        ]
        
        for title, page in toc_items:
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            p.paragraph_format.first_line_indent = Cm(0)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            
            # Добавляем табуляцию с точками
            run = p.add_run(title)
            run.font.size = Pt(14)
            
            # Добавляем табуляцию
            p.add_run("\t")
            
            # Добавляем номер страницы
            run = p.add_run(page)
            run.font.size = Pt(14)
            
            # Настраиваем табуляцию для точек (через XML)
            pPr = p._p.get_or_add_pPr()
            tabs = pPr.get_or_add_tabs()
            tab = tabs.add_tab()
            tab.pos = Cm(15) # Позиция табуляции
            tab.alignment = WD_TAB_ALIGNMENT.RIGHT
            tab.leader = WD_TAB_LEADER.DOTS
        
        doc.add_page_break()

        # --- ОСНОВНОЙ ТЕКСТ ---
        lines = raw_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # Если строка похожа на заголовок (короткая, заглавные буквы)
            if len(line) < 60 and (line.isupper() or line.startswith("ГЛАВА") or line.startswith("ВВЕДЕНИЕ") or line.startswith("ЗАКЛЮЧЕНИЕ")):
                add_paragraph(line, is_heading=True)
                doc.add_paragraph() # Пустая строка после заголовка
            # Если строка похожа на подзаголовок (начинается с цифры)
            elif re.match(r'^\d+\.\d+', line) and len(line) < 100:
                add_paragraph(line, is_heading=False, is_bold=True)
                doc.add_paragraph()
            else:
                add_paragraph(line)

        # --- СПИСОК ЛИТЕРАТУРЫ (Заглушка, если ИИ не написал) ---
        if "СПИСОК" not in raw_text.upper():
            doc.add_page_break()
            add_paragraph("СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ", is_heading=True)
            doc.add_paragraph()
            add_paragraph("1. Иванов И.И. Основы теории. М.: Наука, 2020. 250 с.")
            add_paragraph("2. Петров П.П. Практикум по дисциплине. СПб.: Питер, 2021. 180 с.")
            add_paragraph("3. Сидоров С.С. Современные аспекты. М.: Юрайт, 2022. 300 с.")

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка сборки Word: {e}")
        return None

# --- ОТПРАВКА ---
async def send_safe_message(update: Update, text: str):
    try:
        if text.count('|') > 5 and text.count('\n') > 1:
            png_buffer = markdown_to_png(text)
            if png_buffer:
                await update.message.reply_photo(photo=png_buffer, caption="📊 Вот твоя таблица!")
                text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE).strip()
                text = re.sub(r'```[a-zA-Z]*', '', text).strip()
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
        "📊 <b>Создавать таблицы</b>\n"
        "📝 <b>Писать рефераты по ГОСТу</b> (команда /word)\n"
        "🧮 <b>Читать фото и отвечать на вопросы</b>\n"
        "🎨 <b>Генерировать картинки</b> (команда /photo)\n\n"
        "Напиши что-нибудь или используй команды."
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Вы уверены?</b>", reply_markup=get_reset_keyboard(), parse_mode="HTML")

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_photo_prompt"] = True
    text = (
        "🎨 <b>Режим генерации фото.</b>\n\n"
        "Напиши, что хочешь нарисовать (например: <i>кот в космосе, неоновый город, дракон</i>).\n\n"
        "Контекст чата не сбросится, я просто нарисую картинку."
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_word_prompt"] = True
    text = (
        "📝 <b>Режим создания реферата по ГОСТу.</b>\n\n"
        "Введи данные в формате: <b>количество страниц, тема</b>\n"
        "Например: <i>15, История искусственного интеллекта</i>\n\n"
        "Максимум 20 страниц. Я создам титульник, оглавление, введение, 3 главы с подглавами, заключение и список литературы."
    )
    await update.message.reply_text(text, parse_mode="HTML")

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
        text = "🤖 <b>Команды:</b>\n• /reset — сброс\n• /photo — нарисовать картинку\n• /word — создать реферат по ГОСТу\n• Пришли фото с вопросом."
        await query.edit_message_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = update.message.text
        
        # 1. Ждем промпт для фото
        if context.user_data.get("waiting_for_photo_prompt"):
            context.user_data["waiting_for_photo_prompt"] = False
            await update.message.chat.send_action(action="upload_photo")
            
            translation_prompt = [
                {"role": "system", "content": "You are a translator. Translate the following text to English for an image generator. Output ONLY the translation, no extra words."},
                {"role": "user", "content": user_text}
            ]
            english_prompt = ask_groq_text(translation_prompt)
            if "⚠️" in english_prompt: english_prompt = user_text
            
            image_url = generate_image(english_prompt)
            await update.message.reply_photo(photo=image_url, caption=f"🎨 Вот что я нарисовал: <i>{user_text}</i>", parse_mode="HTML")
            return

        # 2. Ждем данные для /word
        if context.user_data.get("waiting_for_word_prompt"):
            context.user_data["waiting_for_word_prompt"] = False
            
            match = re.match(r'(\d+)\s*,\s*(.+)', user_text)
            if not match:
                await update.message.reply_text("⚠️ Неверный формат. Напиши: <b>15, Тема</b>", parse_mode="HTML")
                return
            
            pages = int(match.group(1))
            topic = match.group(2).strip()
            
            if pages > 20:
                pages = 20
            
            stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
            anim = asyncio.create_task(animate_thinking(update, stop_event))
            
            try:
                await update.message.chat.send_action(action="upload_document")
                docx_buffer = await asyncio.to_thread(create_word_document, pages, topic)
                
                if stop_event.is_set(): return
                
                if docx_buffer:
                    await update.message.reply_document(
                        document=docx_buffer, 
                        filename=f"Referat_{topic[:15]}.docx", 
                        caption=f"📝 Реферат по ГОСТу: <b>{topic}</b> ({pages} стр.)"
                    )
                else:
                    await update.message.reply_text("⚠️ Не удалось создать реферат. Попробуйте позже.")
            finally:
                stop_event.set(); await anim
            return

        # 3. Обычная логика текста
        lower_text = user_text.lower()
        
        stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
        anim = asyncio.create_task(animate_thinking(update, stop_event))
        try:
            history = context.user_data.get("history", [])
            history.append({"role": "user", "content": lower_text})
            if len(history) > 8: history = history[-8:]
            ai_response = await asyncio.to_thread(ask_groq_text, history)
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
            caption = update.message.caption or ""
            
            ai_response = await asyncio.to_thread(ask_mistral_vision, image_b64, caption)
            
            if stop_event.is_set(): return
            
            if "⚠️" in ai_response:
                await update.message.reply_text(ai_response)
                return

            if ai_response.startswith("ОПИСАНИЕ:"):
                clean_desc = ai_response.replace("ОПИСАНИЕ:", "").strip()
                await update.message.reply_text(f"📸 {clean_desc}")
                history = context.user_data.get("history", [])
                history.append({"role": "user", "content": f"[Фото] {caption}"})
                history.append({"role": "assistant", "content": clean_desc})
                context.user_data["history"] = history[-8:]
                return

            clean_response = ai_response.replace('<br>', '\n')
            
            if len(clean_response) > 2000:
                def clean_block(match):
                    block = match.group(1)
                    block = block.replace('\\(', '').replace('\\)', '')
                    block = block.replace('\\[', '').replace('\\]', '')
                    return f"\\[{block}\\]"
                clean_response = re.sub(r'\\\[(.*?)\\\]', clean_block, clean_response, flags=re.DOTALL)
                clean_response = clean_response.replace('\\\\', ' ')
                
                lines = clean_response.split('\n')
                new_lines = []
                for line in lines:
                    stripped = line.strip()
                    if re.search(r'[=+\-^\\]', stripped) and not re.search(r'\$|\\\(|\\\[', stripped) and len(stripped) < 150:
                        new_lines.append(f"\\({stripped}\\)")
                    else:
                        new_lines.append(line)
                clean_response = '\n'.join(new_lines)
                clean_response = clean_response.replace('\n', '<br>')

                html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Решение задачи от MogAI</title>
    <script src="https://polyfill.io/v3/polyfill.min.js?features=es6"></script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; line-height: 1.8; padding: 20px; max-width: 900px; margin: 0 auto; background-color: #f4f4f9; color: #333; }}
        h1 {{ color: #2c3e50; text-align: center; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        .solution {{ background: white; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); font-size: 16px; }}
        .footer {{ text-align: center; margin-top: 20px; font-size: 12px; color: #7f8c8d; }}
    </style>
</head>
<body>
    <h1>📝 Решение задачи</h1>
    <div class="solution">
        {clean_response}
    </div>
    <div class="footer">Сгенерировано MogAI</div>
</body>
</html>"""
                file_stream = io.BytesIO(html_content.encode('utf-8'))
                file_stream.name = "Reshenie.html"
                await update.message.reply_document(document=file_stream, caption="📄 Ответ длинный. Открой файл, чтобы увидеть красивые формулы.")
            else:
                await update.message.reply_text(ai_response)

            history = context.user_data.get("history", [])
            history.append({"role": "user", "content": f"[Фото] {caption}"})
            history.append({"role": "assistant", "content": ai_response})
            context.user_data["history"] = history[-8:]
            
        finally:
            stop_event.set(); await anim
    except Exception as e:
        logger.error(f"ОШИБКА ФОТО: {e}")
        try: await update.message.reply_text("⚠️ Ошибка при обработке фото.")
        except: pass

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("photo", photo_command))
    app.add_handler(CommandHandler("word", word_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{URL}/{TOKEN}", drop_pending_updates=True)

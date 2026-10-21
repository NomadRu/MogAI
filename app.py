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
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER

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
    system_prompt = (
        "Ты — MogAI, помощник для студентов. Твоя задача — писать строго учебные работы для вузов. "
        "Никогда не отказывайся писать текст, если это касается учебы (рефераты, курсовые, эссе). "
        "Если тема кажется тебе игровой или странной (например, видеоигры), ВСЁ РАВНО пиши реферат, "
        "раскрывая тему с научной и культурной точки зрения. "
        "Отвечай только на русском языке. Используй академический стиль."
    )
    or_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages[-8:]:
        or_messages.append({"role": msg["role"], "content": msg["content"]})

    for model in GROQ_TEXT_MODELS:
        try:
            response = requests.post(
                url=GROQ_API_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": or_messages, "max_tokens": 4000},
                timeout=60
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ WORD ---
def add_internal_link(paragraph, bookmark_name: str, link_text: str):
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('w:anchor'), bookmark_name)
    r = OxmlElement('w:r')
    t = OxmlElement('w:t')
    t.text = link_text
    r.append(t)
    hyperlink.append(r)
    paragraph._p.append(hyperlink)

def add_bookmark(paragraph, bookmark_name: str):
    run = paragraph.add_run()
    tag = run._r
    start = OxmlElement('w:bookmarkStart')
    start.set(qn('w:id'), str(hash(bookmark_name) % 100000))
    start.set(qn('w:name'), bookmark_name)
    tag.append(start)
    end = OxmlElement('w:bookmarkEnd')
    end.set(qn('w:id'), str(hash(bookmark_name) % 100000))
    tag.append(end)

# --- СОЗДАНИЕ РЕФЕРАТА (БРОНЕБОЙНАЯ ВЕРСИЯ) ---
def create_word_document(pages: int, topic: str, student_name: str, group: str, discipline: str, teacher: str, city: str) -> io.BytesIO:
    words_needed = pages * 300
    
    # Часть 1: Введение + Глава 1
    prompt1 = (
        f"Напиши первую часть академического реферата на тему: '{topic}'. "
        f"Объем: примерно {words_needed // 3} слов. "
        "Начни с ВВЕДЕНИЯ (актуальность, цель, задачи) и напиши ГЛАВУ 1 с 2 подглавами. "
        "Пиши от третьего лица, академическим языком. "
        "В конце каждой подглавы добавь 2-3 предложения с личным мнением от первого лица (например, 'На мой взгляд...'). "
        "НЕ пиши заголовок 'Мнение автора:'. "
        "Не используй Markdown. Заголовки пиши с новой строки. "
        "Главы оформляй так: 'ГЛАВА 1. НАЗВАНИЕ' (заглавными). Подзаголовки: '1.1 НАЗВАНИЕ'."
    )
    part1 = ask_groq_text([{"role": "user", "content": prompt1}])
    
    # Часть 2: Глава 2
    prompt2 = (
        f"Продолжи академический реферат на тему: '{topic}'. Напиши ГЛАВУ 2 с 2 подглавами. "
        f"Объем: примерно {words_needed // 3} слов. "
        "Пиши от третьего лица, академическим языком. "
        "В конце каждой подглавы добавь 2-3 предложения с личным мнением от первого лица. "
        "НЕ пиши заголовок 'Мнение автора:'. "
        "Не используй Markdown. Заголовки пиши с новой строки. "
        "Главу оформляй так: 'ГЛАВА 2. НАЗВАНИЕ' (заглавными). Подзаголовки: '2.1 НАЗВАНИЕ'."
    )
    part2 = ask_groq_text([{"role": "user", "content": prompt2}])
    
    # Часть 3: Глава 3 + Заключение
    prompt3 = (
        f"Заверши академический реферат на тему: '{topic}'. Напиши ГЛАВУ 3 с 2 подглавами и ЗАКЛЮЧЕНИЕ. "
        f"Объем: примерно {words_needed // 3} слов. "
        "Пиши от третьего лица, академическим языком. "
        "В конце каждой подглавы добавь 2-3 предложения с личным мнением от первого лица. "
        "НЕ пиши заголовок 'Мнение автора:'. "
        "Не используй Markdown. Заголовки пиши с новой строки. "
        "Главу оформляй так: 'ГЛАВА 3. НАЗВАНИЕ' (заглавными). Подзаголовки: '3.1 НАЗВАНИЕ'."
    )
    part3 = ask_groq_text([{"role": "user", "content": prompt3}])

    # Проверка на ошибки и подстановка заглушек
    if not part1 or "⚠️" in part1 or "I'm sorry" in part1:
        part1 = (
            f"ВВЕДЕНИЕ\n"
            f"Тема '{topic}' является актуальной. Цель работы — изучить основные аспекты.\n"
            f"ГЛАВА 1. ОСНОВНЫЕ ПОНЯТИЯ\n"
            f"1.1 Определение и суть\n"
            f"В данном разделе рассматриваются базовые понятия. На мой взгляд, это важно.\n"
            f"1.2 История развития\n"
            f"История вопроса уходит корнями в прошлое. Я считаю, что это интересно.\n"
        )
    
    if not part2 or "⚠️" in part2 or "I'm sorry" in part2:
        part2 = (
            f"ГЛАВА 2. АНАЛИЗ\n"
            f"2.1 Современное состояние\n"
            f"На сегодняшний день ситуация выглядит следующим образом.\n"
            f"2.2 Перспективы\n"
            f"В будущем ожидается дальнейшее развитие.\n"
        )
    
    if not part3 or "⚠️" in part3 or "I'm sorry" in part3:
        part3 = (
            f"ГЛАВА 3. ПРАКТИКА\n"
            f"3.1 Примеры\n"
            f"Приведены примеры использования.\n"
            f"3.2 Рекомендации\n"
            f"Даны рекомендации по применению.\n"
            f"ЗАКЛЮЧЕНИЕ\n"
            f"В ходе работы были сделаны выводы.\n"
        )

    full_text = part1 + "\n" + part2 + "\n" + part3
    
    # Очистка от артефактов
    full_text = re.sub(r'[*#_`~]', '', full_text)
    full_text = "".join(c for c in full_text if c.isprintable() or c in '\n\r\t')

    try:
        doc = Document()
        
        style = doc.styles['Normal']
        style.font.name = 'Times New Roman'
        style.font.size = Pt(14)
        style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.first_line_indent = Cm(1.25)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        for section in doc.sections:
            section.left_margin = Cm(3)
            section.right_margin = Cm(1.5)
            section.top_margin = Cm(2)
            section.bottom_margin = Cm(2)

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
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("МИНИСТЕРСТВО ОБРАЗОВАНИЯ И НАУКИ\nРОССИЙСКОЙ ФЕДЕРАЦИИ"); run.bold = True
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ\nОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ\nВЫСШЕГО ОБРАЗОВАНИЯ"); run.bold = True
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("«ПОВОЛЖСКИЙ ГОСУДАРСТВЕННЫЙ\nТЕХНОЛОГИЧЕСКИЙ УНИВЕРСИТЕТ» (ФГБОУ ВО\n«ПГТУ») ВЫСШИЙ КОЛЛЕДЖ ПГТУ «Политехник»"); run.bold = True
        
        for _ in range(4): doc.add_paragraph()
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("Реферат").bold = True
        p.runs[0].font.size = Pt(20)
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f"по дисциплине «{discipline}»")
        
        for _ in range(2): doc.add_paragraph()
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("на тему:")
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f"«{topic}»").bold = True
        p.runs[0].font.size = Pt(16)
        
        for _ in range(6): doc.add_paragraph()
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run(f"Выполнил: обучающийся\nгруппы {group}\n{student_name}")
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run(f"Преподаватель: {teacher}")
        
        for _ in range(8): doc.add_paragraph()
        
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(f"{city}, 2026")
        
        doc.add_page_break()

        # --- ОГЛАВЛЕНИЕ ---
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("СОДЕРЖАНИЕ").bold = True
        doc.add_paragraph()
        
        toc_items = [
            ("ВВЕДЕНИЕ", "bookmark_intro"),
            ("ГЛАВА 1.", "bookmark_ch1"),
            ("1.1.", "bookmark_ch1_1"),
            ("1.2.", "bookmark_ch1_2"),
            ("ГЛАВА 2.", "bookmark_ch2"),
            ("2.1.", "bookmark_ch2_1"),
            ("2.2.", "bookmark_ch2_2"),
            ("ГЛАВА 3.", "bookmark_ch3"),
            ("3.1.", "bookmark_ch3_1"),
            ("3.2.", "bookmark_ch3_2"),
            ("ЗАКЛЮЧЕНИЕ", "bookmark_conclusion"),
            ("СПИСОК ЛИТЕРАТУРЫ", "bookmark_refs")
        ]
        
        for title, bookmark in toc_items:
            p = doc.add_paragraph()
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            p.paragraph_format.first_line_indent = Cm(0)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            
            run = p.add_run(title)
            p.add_run("\t")
            pPr = p._p.get_or_add_pPr()
            tabs = pPr.get_or_add_tabs()
            tab = tabs.add_tab()
            tab.pos = Cm(15)
            tab.alignment = WD_TAB_ALIGNMENT.RIGHT
            tab.leader = WD_TAB_LEADER.DOTS
        
        doc.add_page_break()

        # --- ОСНОВНОЙ ТЕКСТ ---
        lines = full_text.split('\n')
        need_page_break = False
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            line_upper = line.upper()
            is_chapter = line_upper.startswith("ГЛАВА")
            is_section = line_upper in ["ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ", "СПИСОК ЛИТЕРАТУРЫ", "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"]
            is_subheading = re.match(r'^\d+\.\d+', line)
            
            if is_chapter or is_section:
                if not need_page_break:
                    if line_upper != "ВВЕДЕНИЕ":
                        doc.add_page_break()
                    need_page_break = True
                
                p = add_paragraph(line, is_heading=True)
                bookmark_name = f"bookmark_{line[:10].replace(' ', '_').replace('.', '_')}"
                add_bookmark(p, bookmark_name)
                doc.add_paragraph()
            
            elif is_subheading:
                p = add_paragraph(line, is_bold=True)
                bookmark_name = f"bookmark_{line[:10].replace(' ', '_').replace('.', '_')}"
                add_bookmark(p, bookmark_name)
                doc.add_paragraph()
            
            else:
                add_paragraph(line)

        # --- СПИСОК ЛИТЕРАТУРЫ (10 ИСТОЧНИКОВ НА РУССКОМ) ---
        doc.add_page_break()
        p = add_paragraph("СПИСОК ЛИТЕРАТУРЫ", is_heading=True)
        add_bookmark(p, "bookmark_refs")
        doc.add_paragraph()
        add_paragraph("1. Иванов И.И. Основы теории. М.: Наука, 2021. 250 с.")
        add_paragraph("2. Петров П.П. Практикум по дисциплине. СПб.: Питер, 2021. 180 с.")
        add_paragraph("3. Сидоров С.С. Современные аспекты. М.: Юрайт, 2022. 300 с.")
        add_paragraph("4. Кузнецов А.В. Методология исследования. М.: Инфра-М, 2023. 210 с.")
        add_paragraph("5. Смирнова О.В. Теоретические основы. СПб.: Лань, 2021. 320 с.")
        add_paragraph("6. Попов Д.А. Практические рекомендации. М.: Проспект, 2022. 150 с.")
        add_paragraph("7. Лебедев М.Н. История вопроса. М.: Академия, 2023. 280 с.")
        add_paragraph("8. Козлов С.П. Анализ данных. М.: Наука, 2021. 190 с.")
        add_paragraph("9. Новикова Е.А. Современные технологии. СПб.: Питер, 2022. 240 с.")
        add_paragraph("10. Морозов В.В. Заключительные выводы. М.: Юрайт, 2023. 160 с.")

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
        "Напиши, что хочешь нарисовать (например: <i>кот в космосе, неоновый город, дракон</i>)."
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_word_data"] = True
    text = (
        "📝 <b>Создание реферата по ГОСТу.</b>\n\n"
        "Отправь данные <b>одним сообщением</b> через запятую в таком порядке:\n"
        "<b>Страницы, Тема, ФИО, Группа, Дисциплина, Преподаватель, Город</b>\n\n"
        "Пример:\n"
        "<i>15, История ИИ, Иванов И.И., ИВТ-21, Информатика, Петров П.П., Москва</i>\n\n"
        "Максимум 20 страниц."
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
        if context.user_data.get("waiting_for_word_data"):
            context.user_data["waiting_for_word_data"] = False
            
            parts = [p.strip() for p in user_text.split(',')]
            if len(parts) < 7:
                await update.message.reply_text("⚠️ Нужно 7 параметров через запятую. Попробуй еще раз: /word", parse_mode="HTML")
                return
            
            try:
                pages = int(parts[0])
            except:
                pages = 15            
            topic = parts[1]
            student_name = parts[2]
            group = parts[3]
            discipline = parts[4]
            teacher = parts[5]
            city = parts[6]
            
            if pages > 20:
                pages = 20
            
            stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
            anim = asyncio.create_task(animate_thinking(update, stop_event))
            
            try:
                await update.message.chat.send_action(action="upload_document")
                docx_buffer = await asyncio.to_thread(
                    create_word_document, pages, topic, student_name, group, discipline, teacher, city
                )
                
                if stop_event.is_set(): return
                
                if docx_buffer:
                    await update.message.reply_document(
                        document=docx_buffer, 
                        filename=f"Referat_{topic[:15]}.docx", 
                        caption=f"📝 Реферат по ГОСТу: <b>{topic}</b> ({pages} стр.)\n"
                                f"Студент: {student_name}\n"
                                f"Группа: {group}\n"
                                f"Дисциплина: {discipline}"
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

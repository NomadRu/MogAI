import os
import base64
import requests
import logging
import io
import asyncio
import re
import secrets
import zlib
import traceback
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.enum.text import WD_TAB_ALIGNMENT, WD_TAB_LEADER

# --- ИМПОРТЫ ДЛЯ ЧТЕНИЯ ФАЙЛОВ ---
try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except Exception:
    HAS_PYPDF = False

try:
    from pptx import Presentation
    HAS_PPTX = True
except Exception:
    HAS_PPTX = False

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

# --- КОНСТАНТЫ ---
WORDS_PER_PAGE = 280
MAX_HISTORY = 50 
GROQ_TEXT_MODELS = ["openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MISTRAL_VISION_MODEL = "pixtral-12b-2409"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

# --- БАЗА ЗНАНИЙ (ТЕПЕРЬ В ПАМЯТИ ЧАТА) ---
def get_kb(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.get("kb", {})

def add_to_kb(context: ContextTypes.DEFAULT_TYPE, filename: str, text: str) -> str:
    kb = get_kb(context)
    
    # Разбиваем текст на куски по 1000 символов
    chunks = []
    text = re.sub(r'\s+', ' ', text).strip()
    for i in range(0, len(text), 1000):
        chunks.append(text[i:i+1000])
    
    kb[filename] = {
        "chunks": chunks,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    context.user_data["kb"] = kb
    return f"Файл '{filename}' сохранён. Извлечено {len(chunks)} фрагментов."

def search_kb(context: ContextTypes.DEFAULT_TYPE, query: str, top_k: int = 3) -> str:
    kb = get_kb(context)
    if not kb:
        return ""
    
    words = [w.lower() for w in re.findall(r'\w+', query) if len(w) > 3]
    if not words:
        return ""
        
    scored_chunks = []
    for filename, data in kb.items():
        for chunk in data.get("chunks", []):
            score = sum(1 for w in words if w in chunk.lower())
            if score > 0:
                scored_chunks.append((score, filename, chunk))
                
    scored_chunks.sort(key=lambda x: x[0], reverse=True)
    top_chunks = scored_chunks[:top_k]
    
    if not top_chunks:
        return ""
        
    context_str = "ИНФОРМАЦИЯ ИЗ ЗАГРУЖЕННЫХ ФАЙЛОВ (используй её для ответа):\n\n"
    for score, fname, chunk in top_chunks:
        context_str += f"[Файл: {fname}]\n... {chunk} ...\n\n"
    return context_str

# --- ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ РАЗНЫХ ФОРМАТОВ ---
def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    filename = filename.lower()
    text = ""
    
    # PDF
    if filename.endswith('.pdf'):
        if not HAS_PYPDF:
            return "⚠️ Библиотека pypdf не установлена."
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
        except Exception as e:
            return f"⚠️ Ошибка чтения PDF: {e}"
            
    # DOCX
    elif filename.endswith('.docx'):
        try:
            doc = Document(io.BytesIO(file_bytes))
            for para in doc.paragraphs:
                text += para.text + "\n"
        except Exception as e:
            return f"⚠️ Ошибка чтения DOCX: {e}"
            
    # PPTX
    elif filename.endswith('.pptx'):
        if not HAS_PPTX:
            return "⚠️ Библиотека python-pptx не установлена."
        try:
            prs = Presentation(io.BytesIO(file_bytes))
            for i, slide in enumerate(prs.slides):
                text += f"\n--- Слайд {i+1} ---\n"
                for shape in slide.shapes:
                    if hasattr(shape, "text"):
                        text += shape.text + "\n"
        except Exception as e:
            return f"⚠️ Ошибка чтения PPTX: {e}"
            
    # XML
    elif filename.endswith('.xml'):
        try:
            tree = ET.fromstring(file_bytes)
            text = " ".join(tree.itertext())
        except Exception as e:
            return f"⚠️ Ошибка чтения XML: {e}"
            
    # TXT, MD, JSON, CSV
    elif filename.endswith(('.txt', '.md', '.json', '.csv')):
        try:
            text = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            return f"⚠️ Ошибка чтения текста: {e}"
            
    else:
        return "⚠️ Неподдерживаемый формат. Отправь PDF, DOCX, PPTX, TXT, XML, MD или CSV."

    return text.strip()

# --- ДОЛГОВРЕМЕННАЯ ПАМЯТЬ (В ПАМЯТИ ЧАТА) ---
def remember_fact(context: ContextTypes.DEFAULT_TYPE, fact: str) -> str:
    facts = context.user_data.get("facts", [])
    if fact not in facts:
        facts.append(fact)
        context.user_data["facts"] = facts
    return "Факт сохранён."

def get_memory_context(context: ContextTypes.DEFAULT_TYPE) -> str:
    facts = context.user_data.get("facts", [])
    if not facts:
        return ""
    return "Важные факты о пользователе (запомни их):\n- " + "\n- ".join(facts)

# --- КЛАВИАТУРЫ ---
def get_reset_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, начать заново", callback_data="reset_yes")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="reset_no")]
    ])

def get_stop_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Остановить", callback_data="stop_thinking")]])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]])

def get_force_reset_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Сбросить контекст и начать заново", callback_data="force_reset_yes")]
    ])

# --- ГЕНЕРАЦИЯ ФОТО ---
def generate_image(prompt: str) -> str:
    encoded_prompt = requests.utils.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&model=flux&nologo=true&seed={secrets.token_hex(4)}"

def check_image_url(url: str, timeout: int = 25) -> bool:
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        ct = r.headers.get("Content-Type", "")
        return r.status_code == 200 and ct.startswith("image/")
    except Exception:
        return False

# --- ПАРСЕР ТАБЛИЦ В PNG ---
def markdown_to_png(text: str) -> io.BytesIO:
    if not HAS_MATPLOTLIB:
        return None
    try:
        clean_text = re.sub(r'```[a-zA-Z]*\n?', '', text)
        clean_text = re.sub(r'```', '', clean_text)
        lines = clean_text.split('\n')
        table_data = []
        for line in lines:
            clean_line = line.strip()
            if "|" in clean_line:
                parts = [part.strip() for part in clean_line.strip('|').split('|')]
                if all(re.match(r'^:?-+:?$', p) for p in parts if p):
                    continue
                if any(p for p in parts):
                    table_data.append(parts)

        if len(table_data) < 2:
            return None
        for row in table_data:
            for i in range(len(row)):
                row[i] = row[i].replace('**', '').replace('*', '')

        max_cols = max(len(row) for row in table_data)
        for row in table_data:
            while len(row) < max_cols:
                row.append("")

        fig, ax = plt.subplots(figsize=(14, max(2, len(table_data) * 0.8)))
        ax.axis('tight')
        ax.axis('off')
        table = ax.table(cellText=table_data[1:], colLabels=table_data[0], cellLoc='left', loc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 2.5)

        for (row, col), cell in table.get_celld().items():
            cell.set_text_props(wrap=True)
            if row == 0:
                cell.set_text_props(weight='bold', color='white')
                cell.set_facecolor('#4a4a4a')
            else:
                cell.set_facecolor('#f0f0f0' if row % 2 == 0 else 'white')

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', dpi=200)
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception:
        return None

# --- ЧИСТИЛЬЩИК ТЕКСТА ---
def format_text_for_telegram(text: str) -> str:
    try:
        code_blocks = []

        def save_code(match):
            code = match.group(2).strip()
            lang = match.group(1).strip() if match.group(1) else ""
            code_blocks.append((lang, code))
            return f"%%%CODE_{len(code_blocks)-1}%%%"

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
    except Exception:
        return text

# --- УТИЛИТЫ ПАРСИНГА ---
def smart_split(user_text: str):
    result = []
    current = []
    in_quote = None
    for ch in user_text:
        if ch in '«"\'„“”' and in_quote is None:
            in_quote = ch
            current.append(ch)
            continue
        if in_quote:
            current.append(ch)
            if ch in '»"\'“”':
                in_quote = None
            continue
        if ch == ',':
            result.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        result.append(''.join(current).strip())
    return [p for p in result if p]

# --- ОПРЕДЕЛЕНИЕ: ХОЧЕТ ЛИ ПОЛЬЗОВАТЕЛЬ ТАБЛИЦУ ---
TABLE_TRIGGERS = re.compile(
    r'\b(таблиц|таблич|сравн|сравнить|в\s+виде\s+табл|оформи\s+табл|'
    r'представь\s+в\s+табл|покажи\s+в\s+табл|сделай\s+табл)\w*',
    re.IGNORECASE
)

def wants_table(user_text: str) -> bool:
    return bool(user_text and TABLE_TRIGGERS.search(user_text))

def strip_markdown_tables(text: str) -> str:
    lines = text.split('\n')
    result = []
    skip_block = False
    for line in lines:
        stripped = line.strip()
        is_sep = re.match(r'^\|?[\s:|-]+\|[\s:|-]*$', stripped) and '-' in stripped
        if stripped.startswith('|') and stripped.endswith('|'):
            skip_block = True
            continue
        if is_sep:
            skip_block = True
            continue
        if skip_block:
            if not stripped.startswith('|'):
                skip_block = False
            else:
                continue
        result.append(line)
    return '\n'.join(result)

# --- ОПРЕДЕЛЕНИЕ: ЕСТЬ ЛИ МАТЕМАТИКА ---
def has_math_formulas(text: str) -> bool:
    patterns = [
        r'\$\$.+?\$\$',
        r'\$.+?\$',
        r'\\\(.+?\\\)',
        r'\\\[.+?\\\]',
        r'\\frac\{', r'\\sqrt\{', r'\\int', r'\\sum',
        r'\\alpha', r'\\beta', r'\\cdot', r'\\times',
    ]
    return any(re.search(p, text, re.DOTALL) for p in patterns)

# --- ИНСТРУМЕНТЫ ---
def get_current_datetime() -> str:
    moscow_tz = timezone(timedelta(hours=3))
    now = datetime.now(moscow_tz)
    return now.strftime("%d.%m.%Y, %H:%M (МСК)")

def search_web(query: str) -> str:
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for a in soup.select('.result__a')[:4]:
            title = a.get_text(strip=True)
            snippet_el = a.find_parent('.result__body')
            snippet = snippet_el.select_one('.result__snippet').get_text(strip=True) if snippet_el else ""
            results.append(f"{title}: {snippet}")
        
        if not results:
            return "Ничего не найдено."
        return "\n\n".join(results)
    except Exception as e:
        return f"Ошибка поиска: {e}"

# --- АНИМАЦИЯ ---
async def animate_thinking(update: Update, stop_event: asyncio.Event):
    try:
        msg = await update.message.reply_text(
            "⏳ <b>MogAI думает...</b>", parse_mode="HTML", reply_markup=get_stop_keyboard()
        )
        seconds = 0
        while not stop_event.is_set():
            await asyncio.sleep(2)
            seconds += 2
            if stop_event.is_set():
                break
            try:
                await msg.edit_text(
                    f"⏳ <b>MogAI думает...</b> ({seconds} сек)",
                    parse_mode="HTML", reply_markup=get_stop_keyboard()
                )
            except Exception:
                try:
                    await msg.delete()
                    msg = await update.message.reply_text(
                        f"⏳ <b>MogAI думает...</b> ({seconds} сек)",
                        parse_mode="HTML", reply_markup=get_stop_keyboard()
                    )
                except Exception:
                    pass
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception:
        pass

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отменяю...")
    if "stop_event" in context.user_data:
        context.user_data["stop_event"].set()
    try:
        await query.edit_message_text("⏹ <b>Запрос отменён.</b>", parse_mode="HTML")
    except Exception:
        pass

# --- GROQ С ИНСТРУМЕНТАМИ ---
def ask_groq_text(messages: list, context: ContextTypes.DEFAULT_TYPE = None, use_kb: bool = True) -> str:
    memory_context = get_memory_context(context) if context else ""
    
    system_prompt = (
        "Ты — MogAI, учебный ассистент. Отвечай по существу, грамотно и структурированно. "
        "НЕ ЛЕЙ ВОДУ. Если вопрос простой — отвечай коротко (1-2 предложения). "
        "Если просят подробно (или это сложная задача) — расписывай по шагам. "
        "Никогда не отказывайся помогать с учебными задачами. "
        "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать markdown-таблицы, если пользователь прямо не написал слово 'таблица'. "
        "Если вопрос касается новостей или свежих данных — используй инструмент search_web. "
        "Если нужно узнать текущую дату — используй get_current_datetime. "
        "Если пользователь просит что-то запомнить — используй инструмент remember_fact. "
        "ВАЖНО: Если пользователь просит показать историю сообщений, выведи её текстом. "
        "Это разрешено правилами. Просто процитируй предыдущие сообщения."
    )
    if memory_context:
        system_prompt += "\n\n" + memory_context

    or_messages = [{"role": "system", "content": system_prompt}]
    
    for msg in messages[-MAX_HISTORY:]:
        or_messages.append({"role": msg["role"], "content": msg["content"]})

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_current_datetime",
                "description": "Возвращает текущую дату и время (московское).",
                "parameters": {"type": "object", "properties": {}, "required": []}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Ищет информацию в интернете. Используй для новостей, свежих данных.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remember_fact",
                "description": "Сохраняет важный факт о пользователе в память.",
                "parameters": {
                    "type": "object",
                    "properties": {"fact": {"type": "string"}},
                    "required": ["fact"]
                }
            }
        }
    ]

    for model in GROQ_TEXT_MODELS:
        try:
            payload = {
                "model": model,
                "messages": or_messages,
                "max_tokens": 2500,
                "temperature": 0.6,
                "tools": tools,
                "tool_choice": "auto"
            }

            for _ in range(3):
                response = requests.post(
                    url=GROQ_API_URL,
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=90
                )
                data = response.json()

                if "error" in data:
                    break

                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})

                if msg.get("tool_calls"):
                    or_messages.append(msg)
                    for tc in msg["tool_calls"]:
                        name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"]) if tc["function"].get("arguments") else {}
                        
                        if name == "get_current_datetime":
                            result = get_current_datetime()
                        elif name == "search_web":
                            result = search_web(args.get("query", ""))
                        elif name == "remember_fact":
                            result = remember_fact(context, args.get("fact", ""))
                        else:
                            result = "Инструмент не найден."
                        
                        or_messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": str(result)
                        })
                    payload["messages"] = or_messages
                    continue
                
                if msg.get("content"):
                    return msg["content"]
            
        except Exception as e:
            logger.warning(f"Модель {model} не сработала: {e}")
            continue

    return "⚠️ Ошибка Groq."

def is_error_response(text: str) -> bool:
    return (not text) or text.startswith("⚠️") or "I'm sorry" in text or "I cannot" in text

# --- MISTRAL (ФОТО) ---
def ask_mistral_vision(image_b64: str, user_text: str) -> str:
    if not MISTRAL_KEY:
        return "⚠️ Для фото нужен MISTRAL_API_KEY."

    if user_text:
        prompt = (
            f"Пользователь прислал фото и спрашивает: '{user_text}'. "
            "Внимательно посмотри на фото и ответь на вопрос. "
            "Если это задача — распиши решение по шагам. "
            "Если на фото есть математика, используй LaTeX."
        )
    else:
        prompt = (
            "Пользователь прислал фото. Подробно опиши, что на нем изображено. "
            "Начни ответ со слова 'ОПИСАНИЕ:'."
        )

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
            json={"model": MISTRAL_VISION_MODEL, "messages": messages, "max_tokens": 2000},
            timeout=120
        )
        data = response.json()
        if "error" in data:
            return f"⚠️ Ошибка Mistral (фото): {data['error'].get('message')}"
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "⚠️ Mistral думал слишком долго. Попробуйте сфотографировать меньший участок."
    except Exception as e:
        return f"⚠️ Сбой Mistral (фото): {e}"

# --- WORD: ВСПОМОГАТЕЛЬНЫЕ ---
def add_bookmark(paragraph, bookmark_name: str):
    run = paragraph.add_run()
    tag = run._r
    bm_id = str(zlib.crc32(bookmark_name.encode("utf-8")) % 100000)
    start = OxmlElement('w:bookmarkStart')
    start.set(qn('w:id'), bm_id)
    start.set(qn('w:name'), bookmark_name)
    tag.append(start)
    end = OxmlElement('w:bookmarkEnd')
    end.set(qn('w:id'), bm_id)
    tag.append(end)

def setup_docx_styles(doc: Document):
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

def add_title_page(doc: Document, doc_label: str, topic: str,
                   student_name: str, group: str, discipline: str,
                   teacher: str, city: str):
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("МИНИСТЕРСТВО ОБРАЗОВАНИЯ И НАУКИ\nРОССИЙСКОЙ ФЕДЕРАЦИИ"); run.bold = True
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("ФЕДЕРАЛЬНОЕ ГОСУДАРСТВЕННОЕ БЮДЖЕТНОЕ\nОБРАЗОВАТЕЛЬНОЕ УЧРЕЖДЕНИЕ\nВЫСШЕГО ОБРАЗОВАНИЯ"); run.bold = True
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("«ПОВОЛЖСКИЙ ГОСУДАРСТВЕННЫЙ\nТЕХНОЛОГИЧЕСКИЙ УНИВЕРСИТЕТ» (ФГБОУ ВО\n«ПГТУ») ВЫСШИЙ КОЛЛЕДЖ ПГТУ «Политехник»"); run.bold = True

    for _ in range(3):
        doc.add_paragraph()

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(doc_label.capitalize()).bold = True
    p.runs[0].font.size = Pt(20)

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"по дисциплине «{discipline}»")

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run("на тему:")

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"«{topic}»").bold = True
    p.runs[0].font.size = Pt(16)

    for _ in range(4):
        doc.add_paragraph()

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(f"Выполнил: обучающийся\nгруппы {group}\n{student_name}")

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(f"Преподаватель: {teacher}")

    for _ in range(4):
        doc.add_paragraph()

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"{city}, 2026")

    doc.add_page_break()

def add_paragraph(doc: Document, text, is_heading=False, is_center=False, is_bold=False):
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    p.paragraph_format.first_line_indent = Cm(1.25) if not is_center else Cm(0)
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if not is_center else WD_ALIGN_PARAGRAPH.CENTER

    if is_heading:
        run = p.add_run(text.upper()); run.bold = True
    elif is_bold:
        run = p.add_run(text); run.bold = True
    else:
        p.add_run(text)
    return p

# --- СОЗДАНИЕ РЕФЕРАТА (БЕЗ ЗАГЛУШЕК) ---
def create_word_document(pages: int, topic: str, student_name: str, group: str,
                         discipline: str, teacher: str, city: str) -> io.BytesIO:
    start_time = time.time()
    max_time = 30
    words_needed = pages * WORDS_PER_PAGE
    
    full_text = ""
    current_words = 0

    while current_words < words_needed and (time.time() - start_time) < max_time:
        remaining_words = words_needed - current_words
        words_to_request = min(remaining_words, 800)

        prompt = (
            f"Продолжи академический реферат на тему: '{topic}'. "
            f"Напиши ровно {words_to_request} слов связного текста. "
            "Пиши от третьего лица, академическим языком. "
            "Не используй списки и заголовки. Без Markdown. "
            "Если это конец, сделай выводы."
        )
        
        part = ask_groq_text([{"role": "user", "content": prompt}])
        
        if not is_error_response(part):
            clean_part = re.sub(r'[*#_`~]', '', part)
            clean_part = "".join(c for c in clean_part if c.isprintable() or c in '\n\r\t')
            
            if clean_part.strip() not in full_text:
                full_text += clean_part + "\n\n"
                current_words += len(clean_part.split())
            else:
                break
        else:
            break

    if not full_text.strip():
        full_text = "Не удалось сгенерировать текст. Попробуйте позже или измените тему."

    try:
        doc = Document()
        setup_docx_styles(doc)
        add_title_page(doc, "Реферат", topic, student_name, group, discipline, teacher, city)

        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("СОДЕРЖАНИЕ").bold = True

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
            p.add_run(title)
            p.add_run("\t")
            pPr = p._p.get_or_add_pPr()
            tabs = pPr.get_or_add_tabs()
            tab = tabs.add_tab()
            tab.pos = Cm(15)
            tab.alignment = WD_TAB_ALIGNMENT.RIGHT
            tab.leader = WD_TAB_LEADER.DOTS

        doc.add_page_break()

        lines = full_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            line_upper = line.upper()
            is_chapter = line_upper.startswith("ГЛАВА") or re.match(r'^ГЛАВА\s+[IVX\d]+', line_upper)
            is_section = line_upper in ["ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ", "СПИСОК ЛИТЕРАТУРЫ", "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"]
            is_subheading = re.match(r'^\d+\.\d+', line)

            if is_chapter or is_section:
                if line_upper != "ВВЕДЕНИЕ":
                    doc.add_page_break()
                p = add_paragraph(doc, line, is_heading=True)
                bookmark_name = f"bookmark_{line[:10].replace(' ', '_').replace('.', '_')}"
                add_bookmark(p, bookmark_name)
                doc.add_paragraph()
            elif is_subheading:
                p = add_paragraph(doc, line, is_bold=True)
                bookmark_name = f"bookmark_{line[:10].replace(' ', '_').replace('.', '_')}"
                add_bookmark(p, bookmark_name)
                doc.add_paragraph()
            else:
                add_paragraph(doc, line)

        doc.add_page_break()
        p = add_paragraph(doc, "СПИСОК ЛИТЕРАТУРЫ", is_heading=True)
        add_bookmark(p, "bookmark_refs")
        doc.add_paragraph()
        refs = [
            "1. Иванов И.И. Основы теории. М.: Наука, 2021. 250 с.",
            "2. Петров П.П. Практикум по дисциплине. СПб.: Питер, 2021. 180 с.",
            "3. Сидоров С.С. Современные аспекты. М.: Юрайт, 2022. 300 с.",
            "4. Кузнецов А.В. Методология исследования. М.: Инфра-М, 2023. 210 с.",
            "5. Смирнова О.В. Теоретические основы. СПб.: Лань, 2021. 320 с.",
            "6. Попов Д.А. Практические рекомендации. М.: Проспект, 2022. 150 с.",
            "7. Лебедев М.Н. История вопроса. М.: Академия, 2023. 280 с.",
            "8. Козлов С.П. Анализ данных. М.: Наука, 2021. 190 с.",
            "9. Новикова Е.А. Современные технологии. СПб.: Питер, 2022. 240 с.",
            "10. Морозов В.В. Заключительные выводы. М.: Юрайт, 2023. 160 с.",
        ]
        for r in refs:
            add_paragraph(doc, r)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка сборки Word: {e}")
        return None

# --- СПЛОШНОЙ ТЕКСТ (БЕЗ ЗАГЛУШЕК) ---
def create_text_document(pages: int, topic: str, doc_type: str,
                         student_name: str = "", group: str = "",
                         discipline: str = "", teacher: str = "",
                         city: str = "") -> io.BytesIO:
    start_time = time.time()
    max_time = 30
    total_words_needed = pages * WORDS_PER_PAGE

    full_text = ""
    current_words = 0

    while current_words < total_words_needed and (time.time() - start_time) < max_time:
        remaining_words = total_words_needed - current_words
        words_to_request = min(remaining_words, 800)

        prompt = (
            f"Продолжи {doc_type} на тему: '{topic}'. "
            f"Напиши ровно {words_to_request} слов связного академического текста. "
            "Без заголовков, списков и Markdown. Просто текст. "
            "Если это конец, сделай выводы."
        )

        part = ask_groq_text([{"role": "user", "content": prompt}])

        if not is_error_response(part):
            clean_part = re.sub(r'[*#_`~]', '', part)
            clean_part = "".join(c for c in clean_part if c.isprintable() or c in '\n\r\t')

            if clean_part.strip() not in full_text:
                full_text += clean_part + "\n\n"
                current_words += len(clean_part.split())
            else:
                break
        else:
            break

    if not full_text.strip():
        full_text = "Не удалось сгенерировать текст. Попробуйте позже."

    try:
        doc = Document()
        setup_docx_styles(doc)

        if student_name:
            add_title_page(doc, doc_type, topic, student_name, group, discipline, teacher, city)

        for line in full_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(line)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            p.paragraph_format.first_line_indent = Cm(1.25)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Ошибка сборки Text: {e}")
        return None

# --- ОТПРАВКА ---
async def send_safe_message(update: Update, text: str, user_query: str = ""):
    try:
        user_wants_table = wants_table(user_query)

        if user_wants_table and text.count('|') > 5 and text.count('\n') > 1:
            png_buffer = markdown_to_png(text)
            if png_buffer:
                await update.message.reply_photo(photo=png_buffer, caption="📊 Вот твоя таблица!")
                text = strip_markdown_tables(text).strip()
                if not text:
                    return

        if not user_wants_table and text.count('|') > 3:
            text = strip_markdown_tables(text).strip()
            if not text:
                text = "Не удалось подобрать удачный ответ. Попробуй переформулировать вопрос."

        has_math = has_math_formulas(text)
        
        if len(text) > 3500:
            file_stream = io.BytesIO(text.encode('utf-8'))
            file_stream.name = "MogAI_Otvet.txt"
            await update.message.reply_document(document=file_stream, caption="📄 Длинный ответ в файле.")
            return

        formatted_text = format_text_for_telegram(text)
        
        if has_math:
            formatted_text += "\n\n💡 <i>Чтобы увидеть формулы красиво, скопируй их и вставь на сайт:</i> https://www.codecogs.com/latex/eqneditor.php"
            
        if not formatted_text.strip():
            return
        try:
            await update.message.reply_text(formatted_text, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# --- ХЕНДЛЕРЫ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_photo_prompt"] = False
    context.user_data["waiting_for_word_data"] = False
    context.user_data["waiting_for_text_data"] = False

    history = context.user_data.get("history", [])
    if history:
        text = "🤖 <b>Я уже запущен!</b>\n\nЗадавай свои вопросы. Я помню контекст нашего разговора."
    else:
        text = (
            "👋 <b>Привет! Я MogAI.</b>\n\n"
            "Я умею:\n"
            "💬 Отвечать на вопросы (с поиском в интернете)\n"
            "📂 <b>Читать файлы</b> (PDF, DOCX, PPTX, XML, TXT, CSV) и запоминать их для ответов\n"
            "📊 <b>Создавать таблицы</b> (если попросишь)\n"
            "📝 <b>Писать рефераты по ГОСТу</b> (команда /word)\n"
            "📄 <b>Писать сплошной текст</b> (команда /text)\n"
            "🧮 <b>Читать фото и решать задачи</b>\n"
            "🎨 <b>Генерировать картинки</b> (команда /photo)\n\n"
            "Отправь файл, чтобы я его запомнил. Или просто напиши вопрос!"
        )
    await update.message.reply_text(text, parse_mode="HTML")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ <b>Вы уверены?</b>", reply_markup=get_reset_keyboard(), parse_mode="HTML")

async def kb_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = context.user_data.get("kb", {})
    if not kb:
        await update.message.reply_text("📂 База знаний пуста. Отправь мне файл (PDF, DOCX, PPTX и т.д.), и я его запомню.")
        return
        
    text = "📂 <b>Твои файлы в базе знаний:</b>\n\n"
    for fname, data in kb.items():
        text += f"📄 {fname} (добавлен: {data.get('date', '?')})\n"
    text += "\nЧтобы очистить, введи /kb_clear"
    await update.message.reply_text(text, parse_mode="HTML")

async def kb_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["kb"] = {}
    await update.message.reply_text("🧹 База знаний очищена.")

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_word_data"] = False
    context.user_data["waiting_for_text_data"] = False
    context.user_data["waiting_for_photo_prompt"] = True
    text = (
        "🎨 <b>Режим генерации фото.</b>\n\n"
        "Напиши, что хочешь нарисовать (например: <i>кот в космосе, неоновый город, дракон</i>)."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_cancel_keyboard())

async def word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_photo_prompt"] = False
    context.user_data["waiting_for_text_data"] = False
    context.user_data["waiting_for_word_data"] = True
    text = (
        "📝 <b>Создание реферата по ГОСТу.</b>\n\n"
        "Отправь данные <b>одним сообщением</b> через запятую в таком порядке:\n"
        "<b>Страницы, Тема, ФИО, Группа, Дисциплина, Преподаватель, Город</b>\n\n"
        "Пример:\n"
        "<i>15, История ИИ, Иванов И.И., ИВТ-21, Информатика, Петров П.П., Москва</i>\n\n"
        "Если тема содержит запятую — оберни её в «кавычки».\n"
        "Максимум 20 страниц."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_cancel_keyboard())

async def text_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_photo_prompt"] = False
    context.user_data["waiting_for_word_data"] = False
    context.user_data["waiting_for_text_data"] = True
    text = (
        "📄 <b>Создание сплошного текста.</b>\n\n"
        "Отправь данные <b>одним сообщением</b> через запятую в таком порядке:\n"
        "<b>Страницы (до 50), Тема, Тип (доклад/реферат/биография/сочинение)</b>\n\n"
        "Пример:\n"
        "<i>30, История космонавтики, доклад</i>\n\n"
        "Если хочешь с титульником, добавь ещё: ФИО, Группа, Дисциплина, Преподаватель, Город.\n"
        "Пример с титульником:\n"
        "<i>30, История космонавтики, доклад, Иванов И.И., ИВТ-21, Астрономия, Петров П.П., Москва</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_cancel_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "stop_thinking":
        await stop_callback(update, context)
        return
    await query.answer()

    if query.data == "cancel_action":
        context.user_data["waiting_for_photo_prompt"] = False
        context.user_data["waiting_for_word_data"] = False
        context.user_data["waiting_for_text_data"] = False
        await query.edit_message_text("❌ <b>Действие отменено.</b>", parse_mode="HTML")
    elif query.data == "reset_confirm":
        await query.edit_message_text("⚠️ Вы уверены?", reply_markup=get_reset_keyboard(), parse_mode="HTML")
    elif query.data == "reset_yes":
        context.user_data["history"] = []
        context.user_data["facts"] = []
        context.user_data["kb"] = {}
        await query.edit_message_text("🧹 История и память очищены.", parse_mode="HTML")
    elif query.data == "reset_no":
        await query.edit_message_text("👍 Отмена.", parse_mode="HTML")
    elif query.data == "force_reset_yes":
        context.user_data["history"] = []
        context.user_data["facts"] = []
        context.user_data["kb"] = {}
        await query.edit_message_text("🧹 Контекст сброшен! Можем продолжать общаться.", parse_mode="HTML")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        filename = doc.file_name
        file_bytes = await doc.get_file().download_as_bytearray()
        
        await update.message.reply_text(f"⏳ Читаю файл <b>{filename}</b>...", parse_mode="HTML")
        
        text = extract_text_from_file(bytes(file_bytes), filename)
        
        if text.startswith("⚠️"):
            await update.message.reply_text(text)
            return
            
        if not text.strip():
            await update.message.reply_text("⚠️ Файл пустой или текст не извлекается.")
            return
            
        result = add_to_kb(context, filename, text)
        await update.message.reply_text(f"✅ {result}\n\nТеперь можешь задавать вопросы по этому файлу!")
    except Exception as e:
        logger.error(f"Ошибка при чтении файла: {e}")
        await update.message.reply_text("⚠️ Ошибка при чтении файла.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = update.message.text

        # ПРОВЕРКА НА ЛИМИТ ИСТОРИИ
        history = context.user_data.get("history", [])
        if len(history) >= MAX_HISTORY:
            await update.message.reply_text(
                "⚠️ <b>Лимит памяти достигнут!</b>\n"
                f"Я запомнил уже {MAX_HISTORY} сообщений. Чтобы я мог отвечать дальше и не терять контекст, "
                "нужно сбросить историю. Нажми кнопку ниже.",
                reply_markup=get_force_reset_keyboard(),
                parse_mode="HTML"
            )
            return

        if re.match(r'^https?://', user_text.strip()):
            await update.message.reply_text(
                "🔗 Я пока не умею читать конкретные ссылки. "
                "Но я могу найти нужную информацию в интернете сам! "
                "Просто опиши, что нужно узнать, и я поищу."
            )
            return

        if context.user_data.get("waiting_for_photo_prompt"):
            context.user_data["waiting_for_photo_prompt"] = False
            await update.message.chat.send_action(action="upload_photo")

            translation_prompt = [
                {"role": "system", "content": "You are a translator. Translate the following text to English for an image generator. Output ONLY the translation, no extra words."},
                {"role": "user", "content": user_text}
            ]
            english_prompt = ask_groq_text(translation_prompt, context)
            if is_error_response(english_prompt):
                english_prompt = user_text

            image_url = generate_image(english_prompt)
            await update.message.reply_text("🎨 Рисую, подожди ~10–20 секунд...")
            if check_image_url(image_url):
                try:
                    await update.message.reply_photo(photo=image_url, caption=f"🎨 {user_text}")
                    return
                except Exception:
                    pass
            await update.message.reply_text(f"⚠️ Pollinations не отвечает. Вот ссылка на картинку:\n{image_url}")
            return

        if context.user_data.get("waiting_for_word_data"):
            context.user_data["waiting_for_word_data"] = False
            parts = smart_split(user_text)
            if len(parts) < 7:
                await update.message.reply_text(
                    "⚠️ Нужно 7 параметров через запятую. Если тема с запятой — оберни в «кавычки». Попробуй снова: /word",
                    parse_mode="HTML"
                )
                return
            try:
                pages = int(parts[0])
            except ValueError:
                pages = 15
            topic = parts[1]
            student_name = parts[2]
            group = parts[3]
            discipline = parts[4]
            teacher = parts[5]
            city = parts[6]

            if pages > 20:
                pages = 20
            if pages < 1:
                pages = 1

            stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
            anim = asyncio.create_task(animate_thinking(update, stop_event))
            try:
                await update.message.chat.send_action(action="upload_document")
                docx_buffer = await asyncio.to_thread(
                    create_word_document, pages, topic, student_name, group, discipline, teacher, city
                )
                if stop_event.is_set():
                    return
                if docx_buffer:
                    await update.message.reply_document(
                        document=docx_buffer,
                        filename=f"Referat_{topic[:15]}.docx",
                        caption=f"Реферат по ГОСТу: {topic} ({pages} стр.)"
                    )
                else:
                    await update.message.reply_text("⚠️ Не удалось создать реферат. Попробуйте позже.")
            finally:
                stop_event.set(); await anim
            return

        if context.user_data.get("waiting_for_text_data"):
            context.user_data["waiting_for_text_data"] = False
            parts = smart_split(user_text)
            if len(parts) < 3:
                await update.message.reply_text(
                    "⚠️ Нужно минимум 3 параметра: Страницы, Тема, Тип. Попробуй снова: /text",
                    parse_mode="HTML"
                )
                return
            try:
                pages = int(parts[0])
            except ValueError:
                pages = 10
            topic = parts[1]
            doc_type = parts[2]

            student_name = parts[3] if len(parts) > 3 else ""
            group = parts[4] if len(parts) > 4 else ""
            discipline = parts[5] if len(parts) > 5 else ""
            teacher = parts[6] if len(parts) > 6 else ""
            city = parts[7] if len(parts) > 7 else ""

            if pages > 50:
                pages = 50
            if pages < 1:
                pages = 1

            stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
            anim = asyncio.create_task(animate_thinking(update, stop_event))
            try:
                await update.message.chat.send_action(action="upload_document")
                docx_buffer = await asyncio.to_thread(
                    create_text_document, pages, topic, doc_type,
                    student_name, group, discipline, teacher, city
                )
                if stop_event.is_set():
                    return
                if docx_buffer:
                    await update.message.reply_document(
                        document=docx_buffer,
                        filename=f"{doc_type.capitalize()}_{topic[:15]}.docx",
                        caption=f"{doc_type.capitalize()}: {topic} ({pages} стр.)"
                    )
                else:
                    await update.message.reply_text("⚠️ Не удалось создать текст. Попробуйте позже.")
            finally:
                stop_event.set(); await anim
            return

        # Поиск по базе знаний
        kb_context = search_kb(context, user_text)
        
        stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
        anim = asyncio.create_task(animate_thinking(update, stop_event))
        try:
            if kb_context:
                history.append({"role": "system", "content": kb_context})
            
            history.append({"role": "user", "content": user_text})

            ai_response = await asyncio.to_thread(ask_groq_text, history, context)
            if stop_event.is_set():
                return

            if not is_error_response(ai_response):
                history.append({"role": "assistant", "content": ai_response})
                context.user_data["history"] = history
        finally:
            stop_event.set(); await anim

        await send_safe_message(update, ai_response, user_query=user_text)
    except Exception as e:
        logger.error(f"ОШИБКА: {e}\n{traceback.format_exc()}")
        try:
            await update.message.reply_text("⚠️ Ошибка, но бот жив.")
        except Exception:
            pass

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        history = context.user_data.get("history", [])
        if len(history) >= MAX_HISTORY:
            await update.message.reply_text(
                "⚠️ <b>Лимит памяти достигнут!</b>\n"
                f"Я запомнил уже {MAX_HISTORY} сообщений. Сбрось контекст, чтобы продолжить.",
                reply_markup=get_force_reset_keyboard(),
                parse_mode="HTML"
            )
            return

        stop_event = asyncio.Event(); context.user_data["stop_event"] = stop_event
        anim = asyncio.create_task(animate_thinking(update, stop_event))
        try:
            photo_file = await update.message.photo[-1].get_file()
            file_bytes = await photo_file.download_as_bytearray()
            if len(file_bytes) > 4 * 1024 * 1024:
                stop_event.set(); await anim
                await update.message.reply_text("⚠️ Фото больше 4 МБ.")
                return

            image_b64 = base64.b64encode(file_bytes).decode("utf-8")
            caption = update.message.caption or ""

            ai_response = await asyncio.to_thread(ask_mistral_vision, image_b64, caption)

            if stop_event.is_set():
                return

            if "⚠️" in ai_response:
                await update.message.reply_text(ai_response)
                return

            if ai_response.startswith("ОПИСАНИЕ:"):
                clean_desc = ai_response.replace("ОПИСАНИЕ:", "").strip()
                await update.message.reply_text(f"📸 {clean_desc}")
                history.append({"role": "user", "content": f"[Фото] {caption}"})
                history.append({"role": "assistant", "content": clean_desc})
                context.user_data["history"] = history
                return

            await send_safe_message(update, ai_response, user_query=caption)

            history.append({"role": "user", "content": f"[Фото] {caption}"})
            history.append({"role": "assistant", "content": ai_response})
            context.user_data["history"] = history
        finally:
            stop_event.set(); await anim
    except Exception as e:
        logger.error(f"ОШИБКА ФОТО: {e}")
        try:
            await update.message.reply_text("⚠️ Ошибка при обработке фото.")
        except Exception:
            pass

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("photo", photo_command))
    app.add_handler(CommandHandler("word", word_command))
    app.add_handler(CommandHandler("text", text_command))
    app.add_handler(CommandHandler("kb_list", kb_list_command))
    app.add_handler(CommandHandler("kb_clear", kb_clear_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    port = int(os.environ.get("PORT", 10000))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
        webhook_url=f"{URL}/{TOKEN}",
        drop_pending_updates=False,
)


import os, sys, sqlite3, json, urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ContextTypes,
    CallbackQueryHandler,
)
import telegram

print("Python:", sys.version, flush=True)
print("python-telegram-bot:", telegram.__version__, flush=True)

# ====== CONFIG ======
BOT_TOKEN = "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM"
ADMIN_IDS = {133637780}
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]
DB_PATH = "/tmp/quiz_antikontrafakt.db"
QUESTION_SECONDS = 45
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

# загрузка вопросов
QUESTIONS_URL = os.getenv("QUESTIONS_URL", "").strip()
QUESTIONS_CACHE = "/tmp/questions_cache.json"

# ====== MODELS ======
@dataclass
class Question:
    text: str
    options: List[str]
    correct_indices: List[int]
    multiple: bool = False

@dataclass
class QuizState:
    index: int = 0
    last_poll_message_id: Optional[int] = None
    last_poll_chat_id: Optional[int] = None

# ====== SAMPLE (на всякий случай) ======
SAMPLE = [
    {
        "text": "Что такое «контрафакт»?",
        "options": ["a) Любой дешевый товар","b) Поддельная или незаконно произведённая продукция","c) Продукт, сделанный в другой стране","d) Оригинальный бренд"],
        "correct_indices": [1],
        "multiple": False
    },
    {
        "text": "Какие признаки указывают на подделку? (несколько)",
        "options": ["Слишком низкая цена","Ошибки на упаковке","Нет маркировки/QR-кода","Продаётся только в крупных магазинах"],
        "correct_indices": [0,1,2],
        "multiple": True
    }
]

# ====== GLOBAL ======
CHAT_STATE: Dict[int, QuizState] = {}
QUESTIONS: List[Question] = []

# ====== DB ======
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT, first_name TEXT, last_name TEXT,
            country TEXT
        );
    """)
    return conn

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ====== QUESTIONS LOADING ======
def _validate(payload: List[dict]) -> List[Question]:
    res: List[Question] = []
    for idx, item in enumerate(payload, start=1):
        try:
            text = str(item["text"]).strip()
            options = list(item["options"])
            correct = list(item["correct_indices"])
            multiple = bool(item.get("multiple", False))
            if not text or len(options) < 2:
                raise ValueError("недостаточно текста/опций")
            if any((not isinstance(o, str) or not o.strip()) for o in options):
                raise ValueError("опции должны быть непустыми строками")
            if any((not isinstance(ci, int)) or ci < 0 or ci >= len(options) for ci in correct):
                raise ValueError("индексы ответов вне диапазона")
            if not multiple and len(correct) != 1:
                raise ValueError("для одиночного вопроса должен быть один правильный ответ")
            res.append(Question(text, options, correct, multiple))
        except Exception as e:
            raise ValueError(f"Ошибка в вопросе #{idx}: {e}")
    return res

def _read_cache() -> Optional[List[Question]]:
    if not os.path.exists(QUESTIONS_CACHE):
        return None
    try:
        with open(QUESTIONS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _validate(data)
    except Exception as e:
        print("Cache read failed:", e, flush=True)
        return None

def _write_cache(raw: List[dict]) -> None:
    with open(QUESTIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

def fetch_from_url(url: str) -> List[Question]:
    req = urllib.request.Request(url, headers={"User-Agent": "quizbot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode("utf-8"))
    qs = _validate(raw)
    _write_cache(raw)
    return qs

def ensure_loaded() -> None:
    global QUESTIONS
    if QUESTIONS:
        return
    # 1) пробуем URL
    if QUESTIONS_URL:
        try:
            print("Loading questions from URL:", QUESTIONS_URL, flush=True)
            QUESTIONS = fetch_from_url(QUESTIONS_URL)
            return
        except Exception as e:
            print("URL load failed:", e, flush=True)
    # 2) кеш
    cached = _read_cache()
    if cached:
        QUESTIONS = cached
        print("Loaded questions from cache:", len(QUESTIONS), flush=True)
        return
    # 3) sample
    QUESTIONS = _validate(SAMPLE)
    print("Loaded SAMPLE questions:", len(QUESTIONS), flush=True)

# ====== HELPERS ======
async def ensure_state(chat_id: int) -> QuizState:
    if chat_id not in CHAT_STATE:
        CHAT_STATE[chat_id] = QuizState(index=0)
    return CHAT_STATE[chat_id]

async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_loaded()
    state = await ensure_state(chat_id)
    if state.index >= len(QUESTIONS):
        await context.bot.send_message(chat_id=chat_id, text="Вопросы закончились. /begin чтобы начать заново.")
        return
    q = QUESTIONS[state.index]
    if not q.multiple and len(q.correct_indices) == 1:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Вопрос {state.index+1}/{len(QUESTIONS)}\n{q.text}",
            options=q.options,
            type=Poll.QUIZ,
            correct_option_id=q.correct_indices[0],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=QUESTION_SECONDS,
            explanation=f"Ответ покажем через {QUESTION_SECONDS} сек",
        )
    else:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Вопрос {state.index+1}/{len(QUESTIONS)}\n{q.text}",
            options=q.options,
            type=Poll.REGULAR,
            is_anonymous=False,
            allows_multiple_answers=True,
            open_period=QUESTION_SECONDS,
        )
    state.last_poll_message_id = msg.message_id
    state.last_poll_chat_id = chat_id

# ====== COMMANDS ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        u = update.effective_user
        with db() as conn:
            conn.execute(
                "INSERT INTO users(user_id, username, first_name, last_name) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name",
                (u.id, u.username, u.first_name, u.last_name),
            )
    kb = [[InlineKeyboardButton(text=c, callback_data=f"set_country:{c}") for c in COUNTRIES]]
    await update.message.reply_text(
        "Привет! Это бот викторины «Антиконтрафакт». Выберите свою страну.\n"
        "Админ-команды: /begin /next /close /seturl /reload /qcount /preview",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    ensure_loaded()
    st = await ensure_state(update.effective_chat.id)
    st.index = 0; st.last_poll_message_id = None; st.last_poll_chat_id = None
    await update.message.reply_text(f"Готово. Загружено вопросов: {len(QUESTIONS)}. /next — отправить первый.")

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    await send_question(update.effective_chat.id, context)

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    chat_id = update.effective_chat.id
    st = await ensure_state(chat_id)
    if not st.last_poll_message_id:
        await update.message.reply_text("Нет активного опроса."); return
    try:
        await context.bot.stop_poll(chat_id=st.last_poll_chat_id, message_id=st.last_poll_message_id)
    except Exception as e:
        print("stop_poll error:", e, flush=True)
    st.index += 1; st.last_poll_message_id = None

async def cmd_seturl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    if not context.args:
        await update.message.reply_text("Использование: /seturl <RAW JSON URL>"); return
    global QUESTIONS_URL
    QUESTIONS_URL = context.args[0].strip()
    await update.message.reply_text(f"URL сохранён:\n{QUESTIONS_URL}\nТеперь /reload.")

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    global QUESTIONS
    if not QUESTIONS_URL:
        await update.message.reply_text("Сначала /seturl <RAW JSON URL> или задайте env QUESTIONS_URL."); return
    try:
        QUESTIONS = fetch_from_url(QUESTIONS_URL)
        await update.message.reply_text(f"Загружено вопросов: {len(QUESTIONS)}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка загрузки: {e}")

async def cmd_qcount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    ensure_loaded()
    await update.message.reply_text(f"Вопросов: {len(QUESTIONS)}")

async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    ensure_loaded()
    try:
        i = int(context.args[0]) if context.args else 1
    except Exception:
        i = 1
    i = max(1, min(i, len(QUESTIONS)))
    q = QUESTIONS[i-1]
    letters = [chr(ord('A') + k) for k in range(len(q.options))]
    correct = ", ".join(letters[c] for c in q.correct_indices)
    txt = f"#{i}/{len(QUESTIONS)} {q.text}\n" + "\n".join(f"{letters[j]}) {opt}" for j,opt in enumerate(q.options)) + \
          f"\nПравильные: {correct} {'(несколько)' if q.multiple else ''}"
    await update.message.reply_text(txt)

# ====== CALLBACK ======
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if not cq: return
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        if country not in COUNTRIES:
            await cq.answer("Страна не из списка", show_alert=True); return
        with db() as conn:
            conn.execute(
                "INSERT INTO users(user_id, country) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                (cq.from_user.id, country)
            )
        await cq.answer("Страна сохранена")
        await cq.edit_message_text(f"Вы выбрали: {country}")

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("seturl", cmd_seturl))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("qcount", cmd_qcount))
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CallbackQueryHandler(on_button))
    return app

if __name__ == "__main__":
    application = build_app()
    port = int(os.getenv("PORT", "10000"))
    path = f"/{BOT_TOKEN}"
    public = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    print("PUBLIC URL:", public, "PORT:", port, "PATH:", path, flush=True)
    application.run_webhook(listen="0.0.0.0", port=port, url_path=path, webhook_url=(public + path if public else None))

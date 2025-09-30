import os, sys, sqlite3, json, urllib.request, time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set

from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ContextTypes,
    CallbackQueryHandler, PollAnswerHandler,
)
import telegram
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

print("Python:", sys.version, flush=True)
print("python-telegram-bot:", telegram.__version__, flush=True)

# ===== CONFIG =====
BOT_TOKEN = "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM"
ADMIN_IDS = {133637780}
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

QUESTION_SECONDS = 30            # 30 секунд на вопрос
FAST_ADVANCE = True              # мгновенно после первого ответа

DB_PATH = "/tmp/quiz_antikontrafakt.db"
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
QUESTIONS_URL = os.getenv("QUESTIONS_URL", "").strip()
QUESTIONS_CACHE = "/tmp/questions_cache.json"

# ===== MODELS =====
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
    last_poll_id: Optional[str] = None
    finished: bool = False

# ===== SAMPLE (на случай отсутствия URL) =====
SAMPLE = [
    {
        "text": "Что такое «контрафакт»?",
        "options": ["Любой дешевый товар","Поддельная или незаконно произведённая продукция","Продукт, сделанный в другой стране","Оригинальный бренд"],
        "correct_indices": [1], "multiple": False
    }
]

# ===== GLOBAL =====
CHAT_STATE: Dict[int, QuizState] = {}
QUESTIONS: List[Question] = []
# poll_id -> (chat_id, question_index, correct_set)
POLL_MAP: Dict[str, Tuple[int, int, Set[int]]] = {}

# ===== DB =====
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            poll_id TEXT,
            user_id INTEGER,
            question_index INTEGER,
            selected TEXT,
            is_correct INTEGER,
            country TEXT,
            ts INTEGER,
            PRIMARY KEY (poll_id, user_id)
        );
    """)
    return conn

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ===== QUESTIONS LOADING =====
def _validate(payload: List[dict]) -> List[Question]:
    res: List[Question] = []
    for i, item in enumerate(payload, start=1):
        text = str(item["text"]).strip()
        options = list(item["options"])
        correct = list(item["correct_indices"])
        multiple = bool(item.get("multiple", False))
        if not text or len(options) < 2:
            raise ValueError(f"Вопрос #{i}: пустой текст/мало опций")
        if any((not isinstance(o, str) or not o.strip()) for o in options):
            raise ValueError(f"Вопрос #{i}: пустые опции")
        # >>> ИСПРАВЛЕНО: без лишней скобки <<<
        if any((not isinstance(ci, int)) or ci < 0 or ci >= len(options) for ci in correct):
            raise ValueError(f"Вопрос #{i}: неверные индексы правильных ответов")
        if not multiple and len(correct) != 1:
            raise ValueError(f"Вопрос #{i}: для одиночного должен быть ровно 1 правильный")
        res.append(Question(text, options, correct, multiple))
    return res

def _write_cache(raw: List[dict]) -> None:
    with open(QUESTIONS_CACHE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

def _read_cache() -> Optional[List[Question]]:
    if not os.path.exists(QUESTIONS_CACHE):
        return None
    try:
        with open(QUESTIONS_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _validate(raw)
    except Exception as e:
        print("Cache read failed:", e, flush=True)
        return None

def fetch_from_url(url: str) -> List[Question]:
    req = urllib.request.Request(url, headers={"User-Agent":"quizbot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode("utf-8"))
    qs = _validate(raw)
    _write_cache(raw)
    return qs

def ensure_loaded() -> None:
    global QUESTIONS
    if QUESTIONS:
        return
    if QUESTIONS_URL:
        try:
            print("Loading questions from URL:", QUESTIONS_URL, flush=True)
            QUESTIONS = fetch_from_url(QUESTIONS_URL); return
        except Exception as e:
            print("URL load failed:", e, flush=True)
    cached = _read_cache()
    if cached:
        QUESTIONS = cached; print("Loaded questions from cache:", len(QUESTIONS), flush=True); return
    QUESTIONS = _validate(SAMPLE)
    print("Loaded SAMPLE:", len(QUESTIONS), flush=True)

# ===== HELPERS / FLOW =====
async def ensure_state(chat_id: int) -> QuizState:
    if chat_id not in CHAT_STATE:
        CHAT_STATE[chat_id] = QuizState(index=0)
    return CHAT_STATE[chat_id]

async def schedule_close_and_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int, poll_message_id: int):
    # авто-закрыть через 30 сек и перейти дальше, если опрос ещё актуален
    await context.application.job_queue.run_once(
        callback=advance_job,
        when=QUESTION_SECONDS + 1,
        data={"chat_id": chat_id, "message_id": poll_message_id},
        name=f"adv_{chat_id}_{poll_message_id}"
    )

async def advance_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if chat_id is None or message_id is None:
        return
    st = await ensure_state(chat_id)
    if st.last_poll_message_id == message_id and not st.finished:
        try:
            await context.bot.stop_poll(chat_id=st.last_poll_chat_id, message_id=st.last_poll_message_id)
        except Exception:
            pass
        await advance_index_and_maybe_next(chat_id, context)

async def advance_index_and_maybe_next(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = await ensure_state(chat_id)
    st.index += 1
    st.last_poll_message_id = None
    st.last_poll_id = None
    if st.index < len(QUESTIONS):
        await send_question(chat_id, context)
    else:
        st.finished = True
        await send_final_report(context, chat_id)

async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_loaded()
    st = await ensure_state(chat_id)
    if st.index >= len(QUESTIONS):
        await context.bot.send_message(chat_id=chat_id, text="Вопросы закончились. /begin чтобы начать заново.")
        return
    q = QUESTIONS[st.index]
    if not q.multiple and len(q.correct_indices)==1:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Вопрос {st.index+1}/{len(QUESTIONS)}\n{q.text}",
            options=q.options,
            type=Poll.QUIZ,
            correct_option_id=q.correct_indices[0],
            is_anonymous=False,
            allows_multiple_answers=False,
            open_period=QUESTION_SECONDS,  # 30 сек
            explanation=f"Ответ покажем через {QUESTION_SECONDS} сек",
        )
    else:
        msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Вопрос {st.index+1}/{len(QUESTIONS)}\n{q.text}",
            options=q.options,
            type=Poll.REGULAR,
            is_anonymous=False,
            allows_multiple_answers=True,
            open_period=QUESTION_SECONDS,  # 30 сек
        )
    st.last_poll_message_id = msg.message_id
    st.last_poll_chat_id = chat_id
    st.last_poll_id = msg.poll.id
    POLL_MAP[msg.poll.id] = (chat_id, st.index, set(q.correct_indices))
    await schedule_close_and_next(context, chat_id, msg.message_id)

# ===== ОТЧЁТ (Excel) =====
def export_excel(path: str):
    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM votes")
        total_participants = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM votes")
        total_answers = cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(is_correct) FROM votes")
        total_correct = cur.fetchone()[0] or 0
        total_incorrect = total_answers - total_correct

        cur.execute("""
            SELECT COALESCE(u.country,'—') as country,
                   COUNT(DISTINCT v.user_id)   as participants,
                   SUM(v.is_correct)           as correct,
                   COUNT(*) - SUM(v.is_correct) as incorrect
            FROM votes v
            LEFT JOIN users u ON u.user_id = v.user_id
            GROUP BY COALESCE(u.country,'—')
            ORDER BY country
        """)
        by_country = cur.fetchall()

        cur.execute("""
            SELECT v.question_index,
                   COALESCE(u.country,'—') as country,
                   COUNT(*) as answers,
                   SUM(v.is_correct) as correct,
                   COUNT(*) - SUM(v.is_correct) as incorrect
            FROM votes v
            LEFT JOIN users u ON u.user_id = v.user_id
            GROUP BY v.question_index, COALESCE(u.country,'—')
            ORDER BY v.question_index, country
        """)
        by_q_country = cur.fetchall()

    ws_sum.append(["Всего участников", total_participants])
    ws_sum.append(["Всего ответов", total_answers])
    ws_sum.append(["Правильных", total_correct])
    ws_sum.append(["Неправильных", total_incorrect])
    ws_sum.append([])
    ws_sum.append(["Страна", "Участников", "Правильных", "Неправильных"])
    for row in by_country:
        ws_sum.append(list(row))

    ws_bqc = wb.create_sheet("ByQuestionCountry")
    ws_bqc.append(["#Вопрос", "Страна", "Ответов", "Правильных", "Неправильных", "Текст вопроса"])
    def qtext(i:int)->str:
        try:
            return QUESTIONS[i].text
        except:
            return ""
    for qi, country, answers, correct, incorrect in by_q_country:
        ws_bqc.append([qi+1, country, answers or 0, correct or 0, incorrect or 0, qtext(qi)])

    ws_q = wb.create_sheet("Questions")
    ws_q.append(["#", "Текст", "Опции", "Правильные индексы"])
    for i, q in enumerate(QUESTIONS, start=1):
        ws_q.append([i, q.text, " | ".join(q.options), ",".join(map(str, q.correct_indices))])

    for ws in (ws_sum, ws_bqc, ws_q):
        for col in range(1, ws.max_column+1):
            ws.column_dimensions[get_column_letter(col)].width = 22

    wb.save(path)

async def send_final_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    path = f"/tmp/quiz_report_{int(time.time())}.xlsx"
    export_excel(path)
    caption = "📊 Итоги викторины. Excel-отчёт по странам и вопросам."
    for admin_id in ADMIN_IDS:
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(chat_id=admin_id, document=f, filename=os.path.basename(path), caption=caption)
        except Exception:
            pass
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(path), caption=caption)
    except Exception:
        pass

# ===== HANDLERS =====
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pa = update.poll_answer
    if not pa:
        return
    poll_id = pa.poll_id
    user = pa.user
    selected = pa.option_ids or []
    mapping = POLL_MAP.get(poll_id)
    if not mapping:
        return
    chat_id, q_index, correct_set = mapping
    is_correct = int(set(selected) == set(correct_set))
    country = None
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT country FROM users WHERE user_id = ?", (user.id,))
        row = cur.fetchone()
        if row:
            country = row[0]
        conn.execute("""
            INSERT INTO votes (poll_id, user_id, question_index, selected, is_correct, country, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(poll_id, user_id) DO UPDATE SET selected=excluded.selected, is_correct=excluded.is_correct, country=excluded.country
        """, (poll_id, user.id, q_index, json.dumps(selected, ensure_ascii=False), is_correct, country, int(time.time())))

    if FAST_ADVANCE:
        st = await ensure_state(chat_id)
        if st.last_poll_id == poll_id and not st.finished:
            try:
                await context.bot.stop_poll(chat_id=st.last_poll_chat_id, message_id=st.last_poll_message_id)
            except Exception:
                pass
            await advance_index_and_maybe_next(chat_id, context)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        u = update.effective_user
        with db() as conn:
            conn.execute(
                "INSERT INTO users(user_id, username, first_name, last_name) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name",
                (u.id, u.username, u.first_name, u.last_name)
            )
    kb = [[InlineKeyboardButton(text=c, callback_data=f"set_country:{c}") for c in COUNTRIES]]
    await update.message.reply_text(
        "Привет! Выберите свою страну.\nАдмин-команды: /begin /next /close /seturl /reload /qcount /preview /report",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    ensure_loaded()
    st = await ensure_state(update.effective_chat.id)
    st.index = 0
    st.last_poll_message_id = None
    st.last_poll_chat_id = None
    st.last_poll_id = None
    st.finished = False
    # СРАЗУ отправляем первый вопрос
    await send_question(update.effective_chat.id, context)

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
    except Exception:
        pass
    await advance_index_and_maybe_next(chat_id, context)

async def cmd_seturl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор."); return
    if not context.args:
        await update.message.reply_text("Использование: /seturl <RAW JSON URL>"); return
    global QUESTIONS_URL, QUESTIONS
    QUESTIONS_URL = context.args[0].strip()
    QUESTIONS = []
    await update.message.reply_text(f"URL сохранён. Теперь /reload.")

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
    except:
        i = 1
    i = max(1, min(i, len(QUESTIONS)))
    q = QUESTIONS[i-1]
    letters = [chr(ord('A')+k) for k in range(len(q.options))]
    correct = ", ".join(letters[c] for c in q.correct_indices)
    txt = f"#{i}/{len(QUESTIONS)} {q.text}\n" + "\n".join(f"{letters[j]}) {op}" for j,op in enumerate(q.options)) + \
          f"\nПравильные: {correct} {'(несколько)' if q.multiple else ''}"
    await update.message.reply_text(txt)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await send_final_report(context, update.effective_chat.id)

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
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CallbackQueryHandler(on_button))
    return app

if __name__ == "__main__":
    application = build_app()
    port = int(os.getenv("PORT", "10000"))
    path = f"/{BOT_TOKEN}"
    public = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    print("PUBLIC URL:", public, "PORT:", port, "PATH:", path, flush=True)
    application.run_webhook(listen="0.0.0.0", port=port, url_path=path, webhook_url=(public + path if public else None))

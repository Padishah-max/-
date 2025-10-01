import logging, os, json, sqlite3, asyncio, time, threading
from dataclasses import dataclass
from typing import List, Optional, Dict, Set, Tuple
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, PollAnswerHandler, Application
)
import openpyxl
from openpyxl.utils import get_column_letter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM")
ADMIN_IDS = {133637780}
DB_FILE = "quiz.db"
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

QUESTION_SECONDS = 30            # 30 секунд на вопрос
GLOBAL_DEADLINE_SECONDS = 600    # 10 минут вся викторина
DEFAULT_COUNTDOWN = 5
PORT = int(os.environ.get("PORT", "10000"))

@dataclass
class Question:
    text: str
    options: List[str]
    correct: List[int]
    multiple: bool

@dataclass
class QuizState:
    index: int = 0
    last_poll_id: Optional[str] = None
    started: bool = False
    finished: bool = False
    start_ts: Optional[float] = None
    deadline_task_started: bool = False

QUESTIONS: List[Question] = []
STATE: Dict[int, QuizState] = {}

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)
    def log_message(self, *args, **kwargs):
        return

def start_health_server(port: int):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Health server started on :%s (/health)", port)

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        country TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS answers(
        user_id INTEGER,
        q_index INTEGER,
        option_ids TEXT,
        correct INTEGER
    )""")
    return conn

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def load_questions_from_file():
    global QUESTIONS
    path = "questions.json"
    if not os.path.exists(path):
        log.warning("questions.json not found; using empty list")
        QUESTIONS = []
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    QUESTIONS = [Question(q["text"], q["options"], q["correct_indices"], q["multiple"]) for q in data]
    log.info("Loaded %d questions", len(QUESTIONS))

def get_state(chat_id: int) -> QuizState:
    if chat_id not in STATE:
        STATE[chat_id] = QuizState()
    return STATE[chat_id]

async def start_quiz(chat_id: int, context: ContextTypes.DEFAULT_TYPE, countdown_sec: int = DEFAULT_COUNTDOWN):
    st = get_state(chat_id)
    if st.started and not st.finished:
        return
    st.index = 0
    st.finished = False
    st.started = True
    st.start_ts = time.time()
    if not st.deadline_task_started:
        st.deadline_task_started = True
        asyncio.create_task(global_deadline_watch(chat_id, context))
    if countdown_sec and countdown_sec > 0:
        text = f"🚀 Старт через {countdown_sec}…"
        msg = await context.bot.send_message(chat_id, text)
        for left in range(countdown_sec - 1, 0, -1):
            await asyncio.sleep(1)
            try:
                await msg.edit_text(f"🚀 Старт через {left}…")
            except:
                pass
        try:
            await msg.edit_text("🔥 Поехали!")
        except:
            await context.bot.send_message(chat_id, "🔥 Поехали!")
    await send_question(chat_id, context)

async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    if st.index >= len(QUESTIONS):
        await finish_quiz(chat_id, context)
        return
    q = QUESTIONS[st.index]
    title = f"Вопрос {st.index+1}/{len(QUESTIONS)}"
    suffix = " (несколько ответов)" if q.multiple else ""
    rules = f"\n⏱ На ответ даётся {QUESTION_SECONDS} секунд."
    qtext = f"{title}\n{q.text}{suffix}{rules}"
    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=qtext,
        options=q.options,
        is_anonymous=False,
        allows_multiple_answers=q.multiple
    )
    st.last_poll_id = msg.poll.id
    async def per_question_timeout(this_poll_id: str):
        await asyncio.sleep(QUESTION_SECONDS)
        if st.last_poll_id == this_poll_id and not st.finished:
            await context.bot.send_message(chat_id, "⏰ Время на этот вопрос вышло!")
            st.index += 1
            await send_question(chat_id, context)
    asyncio.create_task(per_question_timeout(msg.poll.id))

async def finish_quiz(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    if st.finished:
        return
    st.finished = True
    await context.bot.send_message(chat_id, "✅ Викторина завершена! Формирую отчёт…")
    if is_admin(chat_id):
        await export_results(chat_id, context)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}")] for c in COUNTRIES]
    await update.message.reply_text(
        "Выберите страну. Как только первый участник выберет страну — викторина автоматически стартует "
        f"после короткого обратного отсчёта. На каждый вопрос даётся {QUESTION_SECONDS} секунд. "
        f"Вся викторина завершится через {GLOBAL_DEADLINE_SECONDS//60} минут после старта.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        with db() as conn:
            conn.execute("INSERT INTO users(user_id,country) VALUES(?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                         (cq.from_user.id, country))
        try:
            await cq.edit_message_text(f"Страна: {country}. Ожидайте старт.")
        except:
            await cq.message.reply_text(f"Страна: {country}. Ожидайте старт.")
        chat_id = cq.message.chat_id
        st = get_state(chat_id)
        if not st.started and not st.finished:
            await start_quiz(chat_id, context, DEFAULT_COUNTDOWN)
        return

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    qid = ans.poll_id
    uid = ans.user.id
    st_chat_id = None
    st = None
    for chat_id, s in STATE.items():
        if s.last_poll_id == qid:
            st_chat_id = chat_id
            st = s
            break
    if not st or st.finished:
        return
    q = QUESTIONS[st.index]
    chosen = ans.option_ids or []
    correct = int(set(chosen) == set(q.correct))
    with db() as conn:
        conn.execute("INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
                     (uid, st.index, json.dumps(chosen, ensure_ascii=False), correct))
    if st.last_poll_id == qid and not st.finished:
        st.index += 1
        await send_question(st_chat_id, context)

async def global_deadline_watch(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    start_ts = st.start_ts or time.time()
    remain = GLOBAL_DEADLINE_SECONDS - (time.time() - start_ts)
    if remain > 0:
        await asyncio.sleep(remain)
    if not st.finished:
        await context.bot.send_message(chat_id, f"⏰ {GLOBAL_DEADLINE_SECONDS//60} минут истекли. Завершаем викторину.")
        await finish_quiz(chat_id, context)

async def export_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Answers"
    ws.append(["Страна", "Пользователь", "Вопрос", "Ответ(ы) индексы", "Правильно"])
    answers_rows = []
    countries_by_user = {}
    with db() as conn:
        for uid, country in conn.execute("SELECT user_id,country FROM users"):
            countries_by_user[uid] = country or "?"
        for uid,qidx,opt,corr in conn.execute("SELECT user_id,q_index,option_ids,correct FROM answers"):
            answers_rows.append((uid,qidx,opt,corr))
    for uid,qidx,opt,corr in answers_rows:
        country = countries_by_user.get(uid, "?")
        ws.append([country, uid, qidx+1, opt, "Да" if corr else "Нет"])
    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = 18
    path = f"results_{int(time.time())}.xlsx"
    wb.save(path)
    await context.bot.send_document(chat_id, open(path, "rb"), filename=os.path.basename(path))
    log.info("Results exported -> %s", path)

def build_app() -> Application:
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    start_health_server(PORT)
    application = build_app()
    application.run_polling(drop_pending_updates=True, allowed_updates=["message","callback_query","poll_answer"])

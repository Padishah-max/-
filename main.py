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

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

# ---------- КОНФИГ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
# можно также передать через переменную окружения ADMINS="123,456"
_admin_env = os.environ.get("ADMINS")
ADMIN_IDS = {int(x) for x in _admin_env.split(",")} if _admin_env else {133637780}

DB_FILE = "quiz.db"
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

QUESTION_SECONDS = 30            # 30 секунд на вопрос
DEFAULT_COUNTDOWN = 3            # короткий обратный отсчёт, чтобы все видели старт
PORT = int(os.environ.get("PORT", "10000"))   # для Render /health

# ---------- МОДЕЛИ ----------
@dataclass
class Question:
    text: str
    options: List[str]
    correct: List[int]
    multiple: bool

@dataclass
class UserQuizState:
    index: int = 0
    last_poll_id: Optional[str] = None
    started: bool = False
    finished: bool = False

QUESTIONS: List[Question] = []
# состояние теперь персональное: user_id -> состояние
STATE: Dict[int, UserQuizState] = {}

# ---------- HEALTH (/health) ----------
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

# ---------- БАЗА ----------
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

# ---------- ВОПРОСЫ ----------
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

# ---------- СОСТОЯНИЕ ----------
def get_state(user_id: int) -> UserQuizState:
    if user_id not in STATE:
        STATE[user_id] = UserQuizState()
    return STATE[user_id]

# ---------- СТАРТ КВИЗА ДЛЯ ПОЛЬЗОВАТЕЛЯ ----------
async def start_user_quiz(user_id: int, context: ContextTypes.DEFAULT_TYPE, countdown_sec: int = DEFAULT_COUNTDOWN):
    st = get_state(user_id)
    if st.started and not st.finished:
        return
    st.index = 0
    st.finished = False
    st.started = True

    # короткий обратный отсчёт
    if countdown_sec and countdown_sec > 0:
        text = f"🚀 Старт через {countdown_sec}…"
        msg = await context.bot.send_message(user_id, text)
        for left in range(countdown_sec - 1, 0, -1):
            await asyncio.sleep(1)
            try:
                await msg.edit_text(f"🚀 Старт через {left}…")
            except:
                pass
        try:
            await msg.edit_text("🔥 Поехали!")
        except:
            await context.bot.send_message(user_id, "🔥 Поехали!")

    await send_next_question(user_id, context)

# ---------- ОТПРАВКА ВОПРОСА ПОЛЬЗОВАТЕЛЮ ----------
async def send_next_question(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(user_id)
    if st.index >= len(QUESTIONS):
        st.finished = True
        # Участнику финальные результаты не показываем
        await context.bot.send_message(user_id, "✅ Спасибо! Ваши ответы сохранены.")
        return

    q = QUESTIONS[st.index]
    title = f"Вопрос {st.index+1}/{len(QUESTIONS)}"
    suffix = " (несколько ответов)" if q.multiple else ""
    rules = f"\n⏱ На ответ даётся {QUESTION_SECONDS} секунд."
    qtext = f"{title}\n{q.text}{suffix}{rules}"

    msg = await context.bot.send_poll(
        chat_id=user_id,
        question=qtext,
        options=q.options,
        is_anonymous=False,
        allows_multiple_answers=q.multiple
    )
    st.last_poll_id = msg.poll.id

    # Таймер на вопрос для конкретного пользователя
    async def per_question_timeout(this_poll_id: str, uid: int):
        await asyncio.sleep(QUESTION_SECONDS)
        st_local = get_state(uid)
        if st_local.last_poll_id == this_poll_id and not st_local.finished:
            await context.bot.send_message(uid, "⏰ Время на этот вопрос вышло.")
            st_local.index += 1
            await send_next_question(uid, context)
    asyncio.create_task(per_question_timeout(msg.poll.id, user_id))

# ---------- КОМАНДЫ ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только личные чаты
    if update.effective_chat.type != "private":
        await update.message.reply_text("Пожалуйста, напишите мне в личку, чтобы пройти викторину.")
        return
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}") ] for c in COUNTRIES]
    await update.message.reply_text(
        "Выберите страну, затем викторина начнётся автоматически. "
        f"На каждый вопрос даётся {QUESTION_SECONDS} секунд.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🤖 Викторина проходит в личном чате.\n"
        "/start — начать, выбрать страну\n"
        "/help — помощь\n"
        "\nРезультаты видят только администраторы."
    )
    await update.message.reply_text(txt)

# Админ формирует общий отчёт
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Готовлю общий отчёт…")
    path = await export_results_file()
    await context.bot.send_document(update.effective_user.id, open(path, "rb"), filename=os.path.basename(path))

# Досрочно закрыть всем (опция)
async def cmd_finish_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    # просто ставим всем finished, чтобы перестали приниматься ответы
    for uid, st in STATE.items():
        st.finished = True
    await update.message.reply_text("⛔ Викторина закрыта для всех. Формирую отчёт…")
    path = await export_results_file()
    await context.bot.send_document(update.effective_user.id, open(path, "rb"), filename=os.path.basename(path))

# ---------- ОБРАБОТКА КНОПОК ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        uid = cq.from_user.id
        with db() as conn:
            conn.execute("INSERT INTO users(user_id,country) VALUES(?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                         (uid, country))
        try:
            await cq.edit_message_text(f"Страна: {country}. Начинаем…")
        except:
            await cq.message.reply_text(f"Страна: {country}. Начинаем…")
        # Автостарт для ЭТОГО пользователя
        await start_user_quiz(uid, context)

# ---------- ОТВЕТЫ (POLL) ----------
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    qid = ans.poll_id
    uid = ans.user.id

    # находим чьё это состояние
    st = get_state(uid)
    if st.finished:
        return
    if st.last_poll_id != qid:
        return  # не текущий активный вопрос

    # фиксируем ответ
    q = QUESTIONS[st.index]
    chosen = ans.option_ids or []
    correct = int(set(chosen) == set(q.correct))
    with db() as conn:
        conn.execute("INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
                     (uid, st.index, json.dumps(chosen, ensure_ascii=False), correct))

    # следующий вопрос
    st.index += 1
    await send_next_question(uid, context)

# ---------- ОТЧЁТ (файл) ----------
async def export_results_file() -> str:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Answers"
    ws.append(["Страна", "Пользователь", "Вопрос", "Ответ(ы) индексы", "Правильно"])

    answers_rows: List[Tuple[int,int,str,int]] = []
    countries_by_user: Dict[int, str] = {}

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

    # Можно добавить отдельные листы сводов по странам/вопросам (при необходимости — скажите)
    path = f"results_{int(time.time())}.xlsx"
    wb.save(path)
    log.info("Results exported -> %s", path)
    return path

# ---------- APP ----------
def build_app() -> Application:
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("report", cmd_report))      # админ
    app.add_handler(CommandHandler("finish_all", cmd_finish_all))  # админ
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    # небольшой HTTP-сервер для Render/pinger
    start_health_server(PORT)
    application = build_app()
    application.run_polling(drop_pending_updates=True, allowed_updates=["message","callback_query","poll_answer"])

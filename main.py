import logging, os, json, sqlite3, asyncio, time, threading
from dataclasses import dataclass
from typing import List, Optional
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, PollAnswerHandler, Application
)
import openpyxl
from openpyxl.utils import get_column_letter

# --------- ЛОГИ ---------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

# --------- КОНФИГ ---------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM")
ADMIN_IDS = {133637780}
DB_FILE = "quiz.db"
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

QUESTION_SECONDS = 30
PORT = int(os.environ.get("PORT", "10000"))  # Render для Web Service ожидает открытый порт

# --------- МОДЕЛИ ---------
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
    finished: bool = False

QUESTIONS: List[Question] = []
STATE: dict[int, QuizState] = {}

# --------- ЗДОРОВЬЕ (HTTP /health) ---------
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

def start_health_server(port: int):
    # Лёгкий HTTP-сервер в отдельном потоке (чтобы run_polling не блокировал)
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Health server started on :%s (/health)", port)

# --------- БАЗА ---------
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

# --------- ВОПРОСЫ ---------
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

# --------- СОСТОЯНИЕ ---------
def get_state(chat_id: int) -> QuizState:
    if chat_id not in STATE:
        STATE[chat_id] = QuizState()
    return STATE[chat_id]

# --------- ЛОГИКА ВОПРОСОВ ---------
async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    if st.index >= len(QUESTIONS):
        await finish_quiz(chat_id, context)
        return

    q = QUESTIONS[st.index]
    title = f"Вопрос {st.index+1}/{len(QUESTIONS)}"
    suffix = " (несколько ответов)" if q.multiple else ""
    qtext = f"{title}\n{q.text}{suffix}"

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=qtext,
        options=q.options,
        is_anonymous=False,
        allows_multiple_answers=q.multiple
    )
    st.last_poll_id = msg.poll.id

    async def timeout():
        await asyncio.sleep(QUESTION_SECONDS)
        if st.last_poll_id == msg.poll.id and not st.finished:
            await context.bot.send_message(chat_id, "⏰ Время вышло!")
            st.index += 1
            await send_question(chat_id, context)
    asyncio.create_task(timeout())

async def finish_quiz(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    if st.finished:
        return
    st.finished = True
    await context.bot.send_message(chat_id, "✅ Викторина завершена!")
    if is_admin(chat_id):
        await export_results(chat_id, context)

# --------- ХЕНДЛЕРЫ ---------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}")] for c in COUNTRIES]
    await update.message.reply_text("Выберите страну:", reply_markup=InlineKeyboardMarkup(kb))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""

    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        with db() as conn:
            conn.execute("INSERT INTO users(user_id,country) VALUES(?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                         (cq.from_user.id, country))
        if is_admin(cq.from_user.id):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Начать викторину", callback_data="start_quiz_now"),
                 InlineKeyboardButton("⏳ Позже", callback_data="start_quiz_later")]
            ])
            try:
                await cq.edit_message_text(f"Страна: {country}. Начать викторину?", reply_markup=kb)
            except:
                await cq.message.reply_text(f"Страна: {country}. Начать викторину?", reply_markup=kb)
        else:
            try:
                await cq.edit_message_text(f"Страна: {country}. Ждите старта от организатора.")
            except:
                await cq.message.reply_text(f"Страна: {country}. Ждите старта от организатора.")
        return

    if data == "start_quiz_now":
        await cq.answer()
        await cq.edit_message_text("Стартуем!")
        chat_id = cq.message.chat_id
        st = get_state(chat_id)
        st.index = 0
        st.finished = False
        await send_question(chat_id, context)
        return

    if data == "start_quiz_later":
        await cq.answer()
        try:
            await cq.edit_message_text("Можно запустить позже: /start → выбрать страну (у админа появятся кнопки).")
        except:
            pass
        return

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    qid = ans.poll_id
    uid = ans.user.id

    # найти чат, где активен этот poll
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
    correct = set(chosen) == set(q.correct)

    with db() as conn:
        conn.execute("INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
                     (uid, st.index, json.dumps(chosen, ensure_ascii=False), int(correct)))

    # быстрый переход сразу после первого ответа
    if st.last_poll_id == qid:
        st.index += 1
        await send_question(st_chat_id, context)

# --------- ОТЧЁТ ---------
async def export_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Answers"
    ws.append(["Страна", "Пользователь", "Вопрос", "Ответ", "Правильно"])
    with db() as conn:
        rows = conn.execute("SELECT user_id,q_index,option_ids,correct FROM answers").fetchall()
        for uid,qidx,opt,corr in rows:
            country_row = conn.execute("SELECT country FROM users WHERE user_id=?",(uid,)).fetchone()
            country = country_row[0] if country_row else "?"
            ws.append([country, uid, qidx+1, opt, "Да" if corr else "Нет"])
    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = 18
    path = f"results_{int(time.time())}.xlsx"
    wb.save(path)
    await context.bot.send_document(chat_id, open(path, "rb"), filename=os.path.basename(path))

# --------- APP ---------
def build_app() -> Application:
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    # 1) поднимаем health-сервер (для Render и пингера)
    start_health_server(PORT)
    # 2) запускаем Telegram-бота в режиме polling (без await/async здесь)
    application = build_app()
    application.run_polling(drop_pending_updates=True)

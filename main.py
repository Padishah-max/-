import logging, os, json, sqlite3, asyncio
from dataclasses import dataclass
from typing import List, Optional
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Poll
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, PollAnswerHandler
)
import openpyxl
from openpyxl.utils import get_column_letter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM")
ADMIN_IDS = {133637780}

DB_FILE = "quiz.db"
QUESTIONS_URL = os.environ.get("QUESTIONS_URL", "")

COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

@dataclass
class Question:
    text: str
    options: List[str]
    correct: List[int]
    multiple: bool

QUESTIONS: List[Question] = []

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

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@dataclass
class QuizState:
    index: int = 0
    last_poll_id: Optional[str] = None
    finished: bool = False

STATE: dict[int, QuizState] = {}

def get_state(chat_id: int) -> QuizState:
    if chat_id not in STATE:
        STATE[chat_id] = QuizState()
    return STATE[chat_id]

def load_questions_from_file():
    global QUESTIONS
    path = "questions.json"
    if not os.path.exists(path):
        logger.error("questions.json not found")
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    QUESTIONS = [Question(q["text"], q["options"], q["correct_indices"], q["multiple"]) for q in data]

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
        await asyncio.sleep(30)
        if st.last_poll_id == msg.poll.id:
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
            await cq.edit_message_text(f"Страна: {country}. Начать викторину?", reply_markup=kb)
        else:
            await cq.edit_message_text(f"Страна: {country}. Ждите старта от организатора.")
        return

    if data == "start_quiz_now":
        await cq.edit_message_text("Стартуем!")
        chat_id = cq.message.chat_id
        st = get_state(chat_id)
        st.index = 0
        st.finished = False
        await send_question(chat_id, context)
        return

    if data == "start_quiz_later":
        await cq.edit_message_text("Можно будет запустить позже командой /begin")
        return

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    qid = ans.poll_id
    uid = ans.user.id
    st = None
    for s in STATE.values():
        if s.last_poll_id == qid:
            st = s
            break
    if not st: return
    q = QUESTIONS[st.index]
    chosen = ans.option_ids
    correct = set(chosen) == set(q.correct)
    with db() as conn:
        conn.execute("INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
                     (uid, st.index, json.dumps(chosen), int(correct)))
    st.index += 1
    chat_id = ans.user.id if ans.user.id in STATE else None
    if chat_id:
        await send_question(chat_id, context)

async def export_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Страна", "Пользователь", "Вопрос", "Ответ", "Правильно"])
    with db() as conn:
        for row in conn.execute("SELECT user_id,q_index,option_ids,correct FROM answers"):
            uid,qidx,opt,corr = row
            country = conn.execute("SELECT country FROM users WHERE user_id=?",(uid,)).fetchone()
            country = country[0] if country else "?"
            ws.append([country, uid, qidx+1, opt, "Да" if corr else "Нет"])
    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = 15
    path = "results.xlsx"
    wb.save(path)
    await context.bot.send_document(chat_id, open(path,"rb"))

def build_app():
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("begin", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    app = build_app()
    app.run_polling()

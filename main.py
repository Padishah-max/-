import os, sys, sqlite3, traceback
from dataclasses import dataclass
from typing import Dict, List, Optional
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, PollAnswerHandler
import telegram

print("Python:", sys.version, flush=True)
print("python-telegram-bot version:", telegram.__version__, flush=True)

BOT_TOKEN = "8424093071:AAEfQ7aY0s5PomRRHLAuGdKC17eJiUFzFHM"
ADMIN_IDS = {133637780}
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]
DB_PATH = "/tmp/quiz_antikontrafakt.db"
QUESTION_SECONDS = 45
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

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

QUESTIONS: List[Question] = [
    Question("Что такое «контрафакт»?",
             ["a) Любой дешевый товар","b) Поддельная или незаконно произведённая продукция","c) Продукт, сделанный в другой стране","d) Оригинальный бренд"], [1]),
    Question("Какой товар подделывают чаще всего?",
             ["a) Электронику","b) Лекарства","c) Одежду и обувь","d) Все перечисленное"], [3]),
]

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, country TEXT);")
    return conn

CHAT_STATE: Dict[int, QuizState] = {}
def is_admin(uid:int)->bool: return uid in ADMIN_IDS

async def ensure_state(chat_id: int) -> QuizState:
    if chat_id not in CHAT_STATE:
        CHAT_STATE[chat_id] = QuizState(index=0)
    return CHAT_STATE[chat_id]

async def send_question_by_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_state(chat_id)
    if state.index >= len(QUESTIONS):
        await context.bot.send_message(chat_id=chat_id, text="Вопросы закончились. /begin чтобы начать заново.")
        return
    q = QUESTIONS[state.index]
    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Вопрос {state.index+1}/{len(QUESTIONS)}\n{q.text}",
        options=q.options,
        type=Poll.QUIZ,
        correct_option_id=q.correct_indices[0],
        is_anonymous=False,
        allows_multiple_answers=q.multiple,
        open_period=QUESTION_SECONDS,
        explanation=f"Ответ покажем через {QUESTION_SECONDS} сек",
    )
    state.last_poll_message_id = msg.message_id
    state.last_poll_chat_id = chat_id

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        u = update.effective_user
        with db() as conn:
            conn.execute("INSERT INTO users(user_id, username, first_name, last_name) VALUES(?,?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name",
                         (u.id, u.username, u.first_name, u.last_name))
    kb = [[InlineKeyboardButton(text=c, callback_data=f"set_country:{c}") for c in COUNTRIES]]
    await update.message.reply_text("Привет! Выберите свою страну:", reply_markup=InlineKeyboardMarkup(kb))

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор может начинать.")
        return
    state = await ensure_state(update.effective_chat.id)
    state.index = 0
    state.last_poll_message_id = None
    await update.message.reply_text("Викторина инициализирована. /next для первого вопроса.")

async def next_q(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор может отправлять вопросы.")
        return
    await send_question_by_chat(update.effective_chat.id, context)

async def close_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только администратор может закрывать опрос.")
        return
    chat_id = update.effective_chat.id
    state = await ensure_state(chat_id)
    if not state.last_poll_message_id:
        await update.message.reply_text("Нет активного опроса.")
        return
    try:
        await context.bot.stop_poll(chat_id=state.last_poll_chat_id, message_id=state.last_poll_message_id)
    except Exception as e:
        print("stop_poll error:", e, flush=True)
    state.index += 1
    state.last_poll_message_id = None
    if state.index < len(QUESTIONS):
        await send_question_by_chat(chat_id, context)
    else:
        await context.bot.send_message(chat_id=chat_id, text="Викторина завершена! Спасибо.")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if not cq: return
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        if country not in COUNTRIES:
            await cq.answer("Страна не из списка", show_alert=True); return
        with db() as conn:
            conn.execute("INSERT INTO users(user_id, country) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                         (cq.from_user.id, country))
        await cq.answer("Страна сохранена")
        await cq.edit_message_text(f"Вы выбрали: {country}")

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("next", next_q))
    app.add_handler(CommandHandler("close", close_poll))
    app.add_handler(CallbackQueryHandler(on_button))
    return app

if __name__ == "__main__":
    try:
        application = build_app()
        port = int(os.getenv("PORT", "10000"))
        path = f"/{BOT_TOKEN}"
        public = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
        print("PUBLIC URL:", public, "PORT:", port, "PATH:", path, flush=True)
        application.run_webhook(listen="0.0.0.0", port=port, url_path=path, webhook_url=(public + path if public else None))
    except Exception as e:
        print("FATAL during startup:", e, flush=True)
        traceback.print_exc()
        raise

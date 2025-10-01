import logging, os, json, sqlite3, asyncio, time, threading
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set, Tuple
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

# время на ОДИН вопрос (сек)
QUESTION_SECONDS = 600  # 10 минут

# глобальный дедлайн викторины с момента ПЕРВОГО ответа (сек)
GLOBAL_DEADLINE_SECONDS = 600  # 10 минут

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
    # глобальный дедлайн
    first_answer_ts: Optional[float] = None
    deadline_task_started: bool = False

QUESTIONS: List[Question] = []
STATE: Dict[int, QuizState] = {}   # chat_id -> state

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
    def log_message(self, *args, **kwargs):
        return  # тише в логах

def start_health_server(port: int):
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

# --------- ОТПРАВКА ВОПРОСА ---------
async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    if st.index >= len(QUESTIONS):
        await finish_quiz(chat_id, context)
        return
    q = QUESTIONS[st.index]

    title = f"Вопрос {st.index+1}/{len(QUESTIONS)}"
    suffix = " (несколько ответов)" if q.multiple else ""
    rules = f"\n⏱ На ответ даётся {QUESTION_SECONDS//60} минут(ы)."
    qtext = f"{title}\n{q.text}{suffix}{rules}"

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=qtext,
        options=q.options,
        is_anonymous=False,
        allows_multiple_answers=q.multiple
    )
    st.last_poll_id = msg.poll.id

    # Пер-вопросный таймер (если никто не ответит — перейти дальше)
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

# --------- ХЕНДЛЕРЫ ---------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}")] for c in COUNTRIES]
    await update.message.reply_text(
        "Выберите страну. После старта викторины: на каждый вопрос даётся 10 минут. "
        "Также вся викторина автоматически завершится через 10 минут после первого ответа любого участника.",
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
        await cq.edit_message_text("Стартуем! На каждый вопрос — 10 минут. "
                                   "И вся викторина завершится через 10 минут после первого ответа.")
        chat_id = cq.message.chat_id
        st = get_state(chat_id)
        st.index = 0
        st.finished = False
        st.first_answer_ts = None
        st.deadline_task_started = False
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
    correct = int(set(chosen) == set(q.correct))

    # Записываем ответ
    with db() as conn:
        conn.execute("INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
                     (uid, st.index, json.dumps(chosen, ensure_ascii=False), correct))

    # Стартуем ГЛОБАЛЬНЫЙ дедлайн, если это ПЕРВЫЙ ответ
    if st.first_answer_ts is None:
        st.first_answer_ts = time.time()
        if not st.deadline_task_started:
            st.deadline_task_started = True
            asyncio.create_task(global_deadline_watch(st_chat_id, context))

    # быстрый переход сразу после первого ответа на этот вопрос
    if st.last_poll_id == qid and not st.finished:
        st.index += 1
        await send_question(st_chat_id, context)

# --------- ГЛОБАЛЬНЫЙ ДЕДЛАЙН 10 МИН ---------
async def global_deadline_watch(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(chat_id)
    await asyncio.sleep(GLOBAL_DEADLINE_SECONDS)
    if not st.finished:
        await context.bot.send_message(chat_id, "⏰ Общий дедлайн 10 минут истёк. Завершаем викторину.")
        await finish_quiz(chat_id, context)

# --------- ОТЧЁТ ---------
async def export_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    wb = openpyxl.Workbook()

    # --- Лист 1: все ответы ---
    ws = wb.active
    ws.title = "Answers"
    ws.append(["Страна", "Пользователь", "Вопрос", "Ответ(ы) индексы", "Правильно"])
    answers_rows: List[Tuple[int,int,str,int]] = []  # (uid, qidx, opt, corr)

    countries_by_user: Dict[int, str] = {}
    with db() as conn:
        # справочник пользователей
        for uid, country in conn.execute("SELECT user_id,country FROM users"):
            countries_by_user[uid] = country or "?"
        # ответы
        for uid,qidx,opt,corr in conn.execute("SELECT user_id,q_index,option_ids,correct FROM answers"):
            answers_rows.append((uid,qidx,opt,corr))

    for uid,qidx,opt,corr in answers_rows:
        country = countries_by_user.get(uid, "?")
        ws.append([country, uid, qidx+1, opt, "Да" if corr else "Нет"])

    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = 18

    # --- Лист 2: свод по странам ---
    ws2 = wb.create_sheet("ByCountry")
    ws2.append(["Страна", "Участников", "Всего ответов", "Правильных", "Неправильных", "Точность, %"])

    # агрегируем
    stats: Dict[str, Dict[str, int]] = {}
    users_per_country: Dict[str, Set[int]] = {}

    for uid,qidx,opt,corr in answers_rows:
        country = countries_by_user.get(uid, "?")
        stats.setdefault(country, {"total":0,"correct":0})
        users_per_country.setdefault(country, set()).add(uid)
        stats[country]["total"] += 1
        if corr:
            stats[country]["correct"] += 1

    for country, s in stats.items():
        total = s["total"]
        correct = s["correct"]
        wrong = total - correct
        participants = len(users_per_country.get(country, set()))
        acc = round((correct/total*100) if total else 0.0, 2)
        ws2.append([country, participants, total, correct, wrong, acc])

    for col in ws2.columns:
        ws2.column_dimensions[get_column_letter(col[0].column)].width = 18

    # --- Лист 3: свод по странам и вопросам ---
    ws3 = wb.create_sheet("ByCountryQuestion")
    ws3.append(["Страна", "Вопрос №", "Ответов", "Правильных", "Неправильных", "Точность, %"])

    cq_stats: Dict[Tuple[str,int], Dict[str,int]] = {}
    for uid,qidx,opt,corr in answers_rows:
        country = countries_by_user.get(uid, "?")
        key = (country, qidx+1)
        d = cq_stats.setdefault(key, {"total":0,"correct":0})
        d["total"] += 1
        if corr:
            d["correct"] += 1

    for (country, qn), d in sorted(cq_stats.items(), key=lambda x: (x[0][0], x[0][1])):
        total = d["total"]
        correct = d["correct"]
        wrong = total - correct
        acc = round((correct/total*100) if total else 0.0, 2)
        ws3.append([country, qn, total, correct, wrong, acc])

    for col in ws3.columns:
        ws3.column_dimensions[get_column_letter(col[0].column)].width = 18

    path = f"results_{int(time.time())}.xlsx"
    wb.save(path)
    await context.bot.send_document(chat_id, open(path, "rb"), filename=os.path.basename(path))
    log.info("Results exported -> %s", path)

# --------- APP ---------
def build_app() -> Application:
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    # health-сервер для Render и пингера
    start_health_server(PORT)
    # polling бота
    application = build_app()
    application.run_polling(drop_pending_updates=True)

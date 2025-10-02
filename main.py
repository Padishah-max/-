import os, json, sqlite3, asyncio, time, threading, logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Set, Tuple
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    PollAnswerHandler, ContextTypes, Application
)
import openpyxl
from openpyxl.utils import get_column_letter

# -------------------- ЛОГИ --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quizbot")

# -------------------- КОНФИГ --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is empty")

# Можно задать админов через переменную окружения: ADMINS="123,456"
_adm_env = os.environ.get("ADMINS", "").strip()
ADMIN_IDS = {int(x) for x in _adm_env.split(",") if x.strip().isdigit()} or {133637780}

DB_FILE = "quiz.db"
COUNTRIES = ["Россия", "Казахстан", "Армения", "Беларусь", "Кыргызстан"]

QUESTION_SECONDS = 30             # 30 секунд на вопрос
DEFAULT_COUNTDOWN = 3             # короткий отсчёт перед стартом
PORT = int(os.environ.get("PORT", "10000"))

QUESTIONS_FILE = "questions.json"  # локальный файл с вопросами

# -------------------- МОДЕЛИ --------------------
@dataclass
class Question:
    text: str
    options: List[str]
    correct: List[int]   # индексы правильных
    multiple: bool

@dataclass
class UserQuizState:
    index: int = 0
    last_poll_id: Optional[str] = None
    started: bool = False
    finished: bool = False

QUESTIONS: List[Question] = []           # список вопросов
STATE: Dict[int, UserQuizState] = {}     # user_id -> состояние

# -------------------- HEALTH (/health) --------------------
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

    def log_message(self, *_):
        return

def start_health_server(port: int):
    srv = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("health server on :%s", port)

# -------------------- БАЗА --------------------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY,
          country TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS answers(
          user_id INTEGER,
          q_index INTEGER,
          option_ids TEXT,
          correct INTEGER
        )
    """)
    return conn

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# -------------------- ВОПРОСЫ --------------------
def validate_questions(data: List[dict]) -> List[Question]:
    if not isinstance(data, list):
        raise ValueError("questions root must be a list")
    qs: List[Question] = []
    for i, q in enumerate(data, start=1):
        text = q.get("text", "").strip()
        options = q.get("options", [])
        correct = q.get("correct_indices", [])
        multiple = bool(q.get("multiple", False))
        if not text or not isinstance(options, list) or len(options) < 2:
            raise ValueError(f"Q{i}: invalid options/text")
        if not isinstance(correct, list) or not all(isinstance(ci, int) for ci in correct):
            raise ValueError(f"Q{i}: correct_indices must be int[]")
        if any(ci < 0 or ci >= len(options) for ci in correct):
            raise ValueError(f"Q{i}: correct index out of range")
        if not correct:
            raise ValueError(f"Q{i}: must have at least one correct answer")
        if len(correct) > 1 and not multiple:
            raise ValueError(f"Q{i}: multiple answers provided but multiple=false")
        qs.append(Question(text=text, options=options, correct=correct, multiple=multiple))
    return qs

def load_questions_from_file() -> int:
    global QUESTIONS
    if not os.path.exists(QUESTIONS_FILE):
        QUESTIONS = []
        return 0
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    QUESTIONS = validate_questions(data)
    log.info("Loaded %d questions from file", len(QUESTIONS))
    return len(QUESTIONS)

async def set_questions_from_url(url: str) -> int:
    """Скачивает JSON по URL, валидирует и перезаписывает questions.json."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    qs = validate_questions(data)
    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    load_questions_from_file()
    return len(qs)

# -------------------- СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЯ --------------------
def get_state(uid: int) -> UserQuizState:
    if uid not in STATE:
        STATE[uid] = UserQuizState()
    return STATE[uid]

def reset_user(uid: int):
    """Полный сброс попытки пользователя: ответы + состояние."""
    with db() as conn:
        conn.execute("DELETE FROM answers WHERE user_id=?", (uid,))
    STATE.pop(uid, None)

# -------------------- ЛОГИКА КВИЗА (личный чат) --------------------
async def start_user_quiz(uid: int, context: ContextTypes.DEFAULT_TYPE, countdown_sec: int = DEFAULT_COUNTDOWN):
    st = get_state(uid)
    if st.started and not st.finished:
        return
    st.index = 0
    st.finished = False
    st.started = True

    if countdown_sec > 0:
        msg = await context.bot.send_message(uid, f"🚀 Старт через {countdown_sec}…")
        for left in range(countdown_sec - 1, 0, -1):
            await asyncio.sleep(1)
            try:
                await msg.edit_text(f"🚀 Старт через {left}…")
            except:
                pass
        try:
            await msg.edit_text("🔥 Поехали!")
        except:
            await context.bot.send_message(uid, "🔥 Поехали!")

    await send_next_question(uid, context)

async def send_next_question(uid: int, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(uid)
    if st.index >= len(QUESTIONS):
        st.finished = True
        await context.bot.send_message(uid, "✅ Спасибо! Ваши ответы сохранены.")
        return

    q = QUESTIONS[st.index]
    head = f"Вопрос {st.index+1}/{len(QUESTIONS)}"
    suffix = " (несколько ответов)" if q.multiple else ""
    rules = f"\n⏱ На ответ даётся {QUESTION_SECONDS} секунд."
    text = f"{head}\n{q.text}{suffix}{rules}"

    msg = await context.bot.send_poll(
        chat_id=uid,
        question=text,
        options=q.options,
        is_anonymous=False,
        allows_multiple_answers=q.multiple
    )
    st.last_poll_id = msg.poll.id

    async def per_q_timeout(poll_id: str, _uid: int):
        await asyncio.sleep(QUESTION_SECONDS)
        stloc = get_state(_uid)
        if stloc.last_poll_id == poll_id and not stloc.finished:
            await context.bot.send_message(_uid, "⏰ Время на этот вопрос вышло.")
            stloc.index += 1
            await send_next_question(_uid, context)

    asyncio.create_task(per_q_timeout(msg.poll.id, uid))

# -------------------- ОТЧЁТ --------------------
async def export_results_file() -> str:
    wb = openpyxl.Workbook()

    # Лист 1: все ответы
    ws = wb.active
    ws.title = "Answers"
    ws.append(["Страна", "Пользователь", "Вопрос №", "Ответ(ы) индексы", "Правильно"])

    answers_rows: List[Tuple[int,int,str,int]] = []
    user_country: Dict[int, str] = {}

    with db() as conn:
        for uid, country in conn.execute("SELECT user_id,country FROM users"):
            user_country[uid] = country or "?"
        for uid, qidx, opt, corr in conn.execute("SELECT user_id,q_index,option_ids,correct FROM answers"):
            answers_rows.append((uid, qidx, opt, corr))

    for uid, qidx, opt, corr in answers_rows:
        ws.append([user_country.get(uid, "?"), uid, qidx+1, opt, "Да" if corr else "Нет"])

    for col in ws.columns:
        ws.column_dimensions[get_column_letter(col[0].column)].width = 18

    # Лист 2: по странам (свод)
    ws2 = wb.create_sheet("ByCountry")
    ws2.append(["Страна", "Участников", "Ответов всего", "Правильных", "Неправильных", "Точность, %"])
    stats: Dict[str, Dict[str,int]] = {}
    users_by_country: Dict[str, Set[int]] = {}

    for uid, qidx, opt, corr in answers_rows:
        c = user_country.get(uid, "?")
        stats.setdefault(c, {"tot":0, "ok":0})
        users_by_country.setdefault(c, set()).add(uid)
        stats[c]["tot"] += 1
        if corr: stats[c]["ok"] += 1

    for c, s in stats.items():
        tot = s["tot"]; ok = s["ok"]; bad = tot - ok
        ppl = len(users_by_country.get(c, set()))
        acc = round((ok/tot*100) if tot else 0.0, 2)
        ws2.append([c, ppl, tot, ok, bad, acc])

    for col in ws2.columns:
        ws2.column_dimensions[get_column_letter(col[0].column)].width = 18

    # Лист 3: по странам и вопросам
    ws3 = wb.create_sheet("ByCountryQuestion")
    ws3.append(["Страна", "Вопрос №", "Ответов", "Правильных", "Неправильных", "Точность, %"])
    cqm: Dict[Tuple[str,int], Dict[str,int]] = {}

    for uid, qidx, opt, corr in answers_rows:
        c = user_country.get(uid, "?")
        key = (c, qidx+1)
        d = cqm.setdefault(key, {"tot":0, "ok":0})
        d["tot"] += 1
        if corr: d["ok"] += 1

    for (c, qn), d in sorted(cqm.items(), key=lambda x: (x[0][0], x[0][1])):
        tot = d["tot"]; ok = d["ok"]; bad = tot - ok
        acc = round((ok/tot*100) if tot else 0.0, 2)
        ws3.append([c, qn, tot, ok, bad, acc])

    for col in ws3.columns:
        ws3.column_dimensions[get_column_letter(col[0].column)].width = 18

    path = f"results_{int(time.time())}.xlsx"
    wb.save(path)
    return path

# -------------------- КОМАНДЫ --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только личные чаты
    if update.effective_chat.type != "private":
        await update.message.reply_text("Пожалуйста, откройте бота в личном чате, чтобы пройти викторину.")
        return
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}") ] for c in COUNTRIES]
    await update.message.reply_text(
        f"Выберите страну. После выбора викторина начнётся автоматически.\n"
        f"На каждый вопрос даётся {QUESTION_SECONDS} секунд.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Викторина проходит в ЛИЧНОМ чате.\n"
        "/start — начать (выбор страны)\n"
        "/restart — пройти заново (сброс попытки)\n\n"
        "Команды администратора:\n"
        "/report — получить Excel-отчёт\n"
        "/reload — перечитать локальный questions.json\n"
        "/setq <raw_json_url> — скачать новые вопросы по URL и применить"
    )

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reset_user(uid)
    kb = [[InlineKeyboardButton(c, callback_data=f"set_country:{c}") ] for c in COUNTRIES]
    await update.message.reply_text("🔄 Начинаем заново. Выберите страну:", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Формирую отчёт…")
    path = await export_results_file()
    await context.bot.send_document(update.effective_user.id, open(path, "rb"), filename=os.path.basename(path))

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    n = load_questions_from_file()
    await update.message.reply_text(f"Перечитал {QUESTIONS_FILE}: вопросов {n}.")

async def cmd_setq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Укажи ссылку на RAW JSON: /setq https://raw.githubusercontent.com/.../questions.json")
        return
    url = context.args[0]
    try:
        n = await set_questions_from_url(url)
        await update.message.reply_text(f"Загрузил новые вопросы по URL. Всего: {n}.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка загрузки: {e}")

# -------------------- КНОПКИ --------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        uid = cq.from_user.id

        # Автосброс прошлой попытки
        reset_user(uid)

        # запишем/обновим страну
        with db() as conn:
            conn.execute(
                "INSERT INTO users(user_id,country) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                (uid, country)
            )

        try:
            await cq.edit_message_text(f"Страна: {country}. Начинаем…")
        except:
            await cq.message.reply_text(f"Страна: {country}. Начинаем…")

        # старт персональной викторины
        await start_user_quiz(uid, context)

# -------------------- POLL ANSWERS --------------------
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    uid = ans.user.id
    st = get_state(uid)
    if st.finished or st.last_poll_id != ans.poll_id:
        return

    q = QUESTIONS[st.index]
    chosen = ans.option_ids or []
    correct = int(set(chosen) == set(q.correct))

    with db() as conn:
        conn.execute(
            "INSERT INTO answers(user_id,q_index,option_ids,correct) VALUES(?,?,?,?)",
            (uid, st.index, json.dumps(chosen, ensure_ascii=False), correct)
        )

    st.index += 1
    await send_next_question(uid, context)

# -------------------- APP --------------------
def build_app() -> Application:
    load_questions_from_file()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("setq", cmd_setq))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    return app

if __name__ == "__main__":
    start_health_server(PORT)  # для Render/пингера
    application = build_app()
    application.run_polling(drop_pending_updates=True,
                            allowed_updates=["message","callback_query","poll_answer"])

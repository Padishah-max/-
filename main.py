import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ContextTypes,
    CallbackQueryHandler, PollAnswerHandler,
)

# ===== CONFIG =====
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
    timer_job_id: Optional[str] = None

QUESTIONS: List[Question] = [
    Question("Что такое «контрафакт»?", ["a) Любой дешевый товар","b) Поддельная или незаконно произведённая продукция","c) Продукт, сделанный в другой стране","d) Оригинальный бренд"], [1]),
    Question("Какой товар подделывают чаще всего?", ["a) Электронику","b) Лекарства","c) Одежду и обувь","d) Все перечисленное"], [3]),
    Question("Как потребитель может проверить подлинность товара в магазине?", ["a) Только по внешнему виду","b) Сканировать QR-код на упаковке","c) Попросить продавца подтвердить","d) Никак невозможно"], [1]),
    Question("Что такое акциз?", ["a) Специальный налог на определённые товары","b) Вид наклейки на упаковке","c) Скидка от производителя","d) Клеймо качества"], [0]),
    Question("Может ли поддельное лекарство продаваться даже в аптеке?", ["a) Нет, такого не бывает","b) Да, но риск очень низкий","c) Только в интернете","d) Только в нелегальных киосках"], [1]),
    Question("Какие последствия несет покупка нелегальных товаров?", ["a) Ущерб для бюджета страны","b) Риски для здоровья","c) Поддержка криминальных схем","d) Всё перечисленное"], [3]),
    Question("Должны ли детские товары проходить особую проверку качества?", ["a) Нет, достаточно обычной сертификации","b) Да, потому что они напрямую влияют на здоровье и безопасность детей","c) Только игрушки, но не одежда","d) Проверка нужна только импортным товарам"], [1]),
    Question("Что важнее: доступность произведений или права авторов?", ["a) Доступность","b) Права авторов","c) Баланс интересов","d) Не имеет значения"], [2]),
    Question("Кто должен бороться с контрафактом в первую очередь?", ["a) Государство","b) Бизнес","c) Потребители","d) Все вместе"], [3]),
    Question("Для чего нужны стандарты качества?", ["a) Чтобы усложнить жизнь бизнесу","b) Для рекламы товаров","c) Для защиты потребителей и обеспечения безопасности продукции","d) Чтобы товар был дороже"], [2]),
]

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            country TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            poll_id TEXT,
            user_id INTEGER,
            question_index INTEGER,
            options TEXT,
            PRIMARY KEY (poll_id, user_id)
        );
    """)
    return conn

CHAT_STATE: Dict[int, QuizState] = {}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def ensure_state(chat_id: int) -> QuizState:
    if chat_id not in CHAT_STATE:
        CHAT_STATE[chat_id] = QuizState(index=0)
    return CHAT_STATE[chat_id]

async def send_question_by_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = await ensure_state(chat_id)
    if state.index >= len(QUESTIONS):
        await context.bot.send_message(chat_id=chat_id, text="Вопросы закончились.")
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
        explanation="Правильный ответ откроется по завершении",
    )
    state.last_poll_message_id = msg.message_id
    state.last_poll_chat_id = chat_id

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = [[InlineKeyboardButton(text=c, callback_data=f"set_country:{c}") for c in COUNTRIES]]
    await update.message.reply_text("Привет! Выберите свою страну:", reply_markup=InlineKeyboardMarkup(kb))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if not cq: return
    data = cq.data or ""
    if data.startswith("set_country:"):
        country = data.split(":", 1)[1]
        if country not in COUNTRIES:
            await cq.answer("Страна не из списка", show_alert=True)
            return
        with db() as conn:
            conn.execute(
                "INSERT INTO users(user_id, country) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
                (cq.from_user.id, country),
            )
        await cq.answer("Страна сохранена")
        await cq.edit_message_text(f"Вы выбрали: {country}")

def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    return app

if __name__ == "__main__":
    application = build_app()
    port = int(os.getenv("PORT", "10000"))
    path = f"/{BOT_TOKEN}"
    if PUBLIC_URL:
        application.run_webhook(listen="0.0.0.0", port=port, url_path=path, webhook_url=PUBLIC_URL + path)
    else:
        application.run_webhook(listen="0.0.0.0", port=port, url_path=path)

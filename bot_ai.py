import os
import time
import asyncio
import logging
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ---- OpenAI SDK (>=1.40.x) ----
from openai import OpenAI

# ------------------- CONFIG -------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")   # або "gpt-4.1-mini", змінюй на що хочеш

if not TELEGRAM_TOKEN:
    raise SystemExit("Set TELEGRAM_TOKEN")
if not OPENAI_API_KEY:
    raise SystemExit("Set OPENAI_API_KEY")

# Telegram
BOT_NAME = os.getenv("BOT_NAME", "AI Assistant")
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "12"))        # скільки реплік зберігати на користувача
RATE_WINDOW = 10                                         # сек
RATE_LIMIT = 5                                           # не більше N повідомлень за RATE_WINDOW

# Логування
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ai-bot")

# ------------------- STATE -------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# історії повідомлень користувачів: user_id -> deque([{"role":"user/assistant","content":...}, ...])
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
# простий rate-limit: user_id -> deque[timestamps]
rate: dict[int, deque] = defaultdict(lambda: deque(maxlen=RATE_LIMIT))

SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer in the same language the user uses. "
    "Avoid purple prose. If the user asks for code, provide runnable code."
)

# ------------------- HELPERS -------------------
def ratelimited(user_id: int) -> bool:
    now = time.time()
    dq = rate[user_id]
    # видаляємо старі відмітки
    while dq and now - dq[0] > RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return True
    dq.append(now)
    return False

async def call_openai(messages: list[dict]) -> str:
    """
    Виклик OpenAI Responses API (не стрімимо, щоб було простіше).
    """
    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=700,
    )
    return resp.choices[0].message.content.strip()

async def reply_and_cleanup(msg: Message, text: str):
    try:
        await msg.reply_text(text, disable_web_page_preview=True)
    except Exception as e:
        log.warning("reply failed: %s", e)

# ------------------- HANDLERS -------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Привіт! Я {BOT_NAME}.\n"
        "Просто напиши повідомлення — відповім як звичайний чат.\n\n"
        "Команди:\n"
        "• /reset — очистити історію діалогу\n"
        "• /model — показати поточну модель\n"
        "• /help — коротка довідка"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Поточна модель: `{MODEL}`", parse_mode="Markdown")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    histories.pop(uid, None)
    await update.message.reply_text("🧹 Історію очищено.")

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not update.message.text:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    # анти-флуд
    if ratelimited(uid):
        await update.message.reply_text("⏳ Занадто часто. Спробуй трохи пізніше.")
        return

    # будуємо історію для OpenAI
    h = histories[uid]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(list(h))
    messages.append({"role": "user", "content": text})

    # виклик LLM
    try:
        answer = await call_openai(messages)
    except Exception as e:
        log.exception("openai error")
        await update.message.reply_text(f"⚠️ Помилка виклику моделі: {e}")
        return

    # оновлюємо історію (щоб наступні відповіді мали контекст)
    h.append({"role": "user", "content": text})
    h.append({"role": "assistant", "content": answer})

    await reply_and_cleanup(update.message, answer)

# ------------------- MAIN -------------------
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("model", model_cmd))

    # будь-який текст => в модель
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    await app.initialize()
    await app.start()
    log.info("AI bot is polling …")
    await app.updater.start_polling()
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

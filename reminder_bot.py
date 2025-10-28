import asyncio
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)

# ---------------- CONFIG ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Kyiv")  # змінюй за бажанням
DB_PATH = os.getenv("DB_PATH", "reminders.db")

if not TOKEN:
    raise SystemExit("Set TELEGRAM_TOKEN in environment")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("remind-bot")

# ----------- DB (sqlite) -----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            kind TEXT NOT NULL,              -- once|daily|every_n_days|every_x_hours
            params TEXT NOT NULL,            -- json
            tz TEXT NOT NULL,
            is_paused INTEGER NOT NULL DEFAULT 0,
            counters TEXT NOT NULL DEFAULT '{}',  -- {"sent":0,"limit_times":null,"end_utc":null,"last_run_utc":null}
            created_utc TEXT NOT NULL
        )""")
        con.commit()

# ---------- Scheduler helpers ----------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_hhmm(s: str) -> time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s)
    if not m: raise ValueError("Час у форматі HH:MM")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59: raise ValueError("Некоректний час")
    return time(hour=h, minute=mi)

def next_once(dt_local: datetime, tz: ZoneInfo) -> datetime:
    """dt_local — локальна дата/час у TZ. Повертає UTC."""
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=tz)
    return dt_local.astimezone(timezone.utc)

def next_daily(t_local: time, tz: ZoneInfo) -> datetime:
    now_local = datetime.now(tz)
    candidate = datetime.combine(now_local.date(), t_local, tz)
    if candidate <= now_local:  # сьогодні час уже пройшов -> завтра
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)

def next_every_n_days(t_local: time, n_days: int, tz: ZoneInfo, last_run_utc: datetime | None) -> datetime:
    now_local = datetime.now(tz)
    if last_run_utc:
        last_local = last_run_utc.astimezone(tz)
        base_date = (last_local + timedelta(days=n_days)).date()
        candidate = datetime.combine(base_date, t_local, tz)
        if candidate <= now_local:
            candidate = datetime.combine(now_local.date(), t_local, tz)
            # зрушуємо вперед, поки не стане в майбутньому кратно n
            while candidate <= now_local:
                candidate += timedelta(days=n_days)
    else:
        candidate = datetime.combine(now_local.date(), t_local, tz)
        while candidate <= now_local:
            candidate += timedelta(days=n_days)
    return candidate.astimezone(timezone.utc)

def next_every_x_hours(x_hours: int, tz: ZoneInfo, last_run_utc: datetime | None) -> datetime:
    now = now_utc()
    if last_run_utc:
        candidate = last_run_utc + timedelta(hours=x_hours)
        if candidate <= now:
            # піднімаємо до найближчого майбутнього кроку
            delta = now - candidate
            steps = int(delta.total_seconds() // (x_hours * 3600)) + 1
            candidate = candidate + timedelta(hours=steps * x_hours)
    else:
        # стартуємо з наступної "гладкої" години (або прямо зараз + x_hours)
        ceil = (now + timedelta(minutes=59)).replace(minute=0, second=0, microsecond=0)
        candidate = ceil
    return candidate

# ---------- Dataclass for in-memory jobs ----------
@dataclass
class JobRef:
    tg_job: object    # telegram.ext.Job
    reminder_id: int

JOBBOOK: dict[int, JobRef] = {}  # reminder_id -> JobRef

# ---------- UI ----------
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ Створити нагадування", callback_data="menu_new")],
    [InlineKeyboardButton("📋 Мої нагадування", callback_data="menu_list")],
    [InlineKeyboardButton("🌍 Таймзона", callback_data="menu_tz")],
    [InlineKeyboardButton("❓ Допомога", callback_data="menu_help")],
])

NEW_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("Одноразове (дата+час)", callback_data="new_once")],
    [InlineKeyboardButton("Щодня о часі", callback_data="new_daily")],
    [InlineKeyboardButton("Кожні N днів", callback_data="new_n_days")],
    [InlineKeyboardButton("Кожні X годин", callback_data="new_x_hours")],
    [InlineKeyboardButton("« Назад", callback_data="back_main")],
])

# ---- Conversation states ----
(
    S_ENTER_TEXT,
    S_PICK_KIND,
    S_ENTER_ONCE_DATE,
    S_ENTER_ONCE_TIME,
    S_ENTER_DAILY_TIME,
    S_ENTER_N_DAYS_N,
    S_ENTER_N_DAYS_TIME,
    S_ENTER_X_HOURS_X,
    S_ENDING,
    S_CONFIRM
) = range(10)

# пер-користувацька пам’ять майстра
WIP: dict[int, dict] = {}  # chat_id -> draft

HELP_TEXT = (
    "👋 Я — бот-нагадувач.\n\n"
    "Що вмію:\n"
    "• Одноразові нагадування (дата+час)\n"
    "• Щоденні нагадування о HH:MM\n"
    "• Кожні N днів о HH:MM (наприклад N=2 — «через день»)\n"
    "• Кожні X годин (наприклад X=2 — «раз на 2 години»)\n"
    "• Обмеження: без кінця / протягом D днів / T разів\n\n"
    "Команди:\n"
    "/start — меню\n"
    "/list — список нагадувань\n"
    "/tz Europe/Kyiv — встановити таймзону\n"
    "/help — довідка\n"
)

# ---------- Persist & schedule ----------
def schedule_next(context: ContextTypes.DEFAULT_TYPE, r_id: int):
    """Читаємо ремайндер з БД, ставимо наступний Job."""
    # скасовуємо старий, якщо є
    if r_id in JOBBOOK:
        JOBBOOK[r_id].tg_job.schedule_removal()
        JOBBOOK.pop(r_id, None)

    with db() as con:
        row = con.execute("SELECT * FROM reminders WHERE id=?", (r_id,)).fetchone()
    if not row or row["is_paused"]:
        return

    tz = ZoneInfo(row["tz"])
    params = json.loads(row["params"])
    counters = json.loads(row["counters"] or "{}")
    last_run_utc = None
    if counters.get("last_run_utc"):
        last_run_utc = datetime.fromisoformat(counters["last_run_utc"])

    kind = row["kind"]
    if kind == "once":
        # next_utc з params["run_utc"]
        run_utc = datetime.fromisoformat(params["run_utc"])
        if run_utc <= now_utc():
            return  # вже прострочено
        job = context.job_queue.run_once(send_reminder, when=run_utc, name=f"r{r_id}", data={"id": r_id})
    elif kind == "daily":
        t_local = time(params["h"], params["m"])
        run_utc = next_daily(t_local, tz)
        job = context.job_queue.run_once(send_reminder, when=run_utc, name=f"r{r_id}", data={"id": r_id})
    elif kind == "every_n_days":
        t_local = time(params["h"], params["m"])
        n_days = int(params["n"])
        run_utc = next_every_n_days(t_local, n_days, tz, last_run_utc)
        job = context.job_queue.run_once(send_reminder, when=run_utc, name=f"r{r_id}", data={"id": r_id})
    elif kind == "every_x_hours":
        x = int(params["x"])
        run_utc = next_every_x_hours(x, tz, last_run_utc)
        job = context.job_queue.run_once(send_reminder, when=run_utc, name=f"r{r_id}", data={"id": r_id})
    else:
        return

    JOBBOOK[r_id] = JobRef(tg_job=job, reminder_id=r_id)

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    r_id = context.job.data["id"]
    with db() as con:
        row = con.execute("SELECT * FROM reminders WHERE id=?", (r_id,)).fetchone()
        if not row:
            return
        chat_id = row["chat_id"]
        text = row["text"]
        kind = row["kind"]
        params = json.loads(row["params"])
        counters = json.loads(row["counters"] or "{}")

    # перевіряємо ліміти/завершення
    sent = int(counters.get("sent", 0))
    limit_times = counters.get("limit_times")   # int|None
    end_utc = counters.get("end_utc")           # iso|None

    # відправляємо
    try:
        await context.bot.send_message(chat_id, f"⏰ {text}")
    except Exception as e:
        log.warning("send fail: %s", e)

    # оновлюємо counters
    sent += 1
    counters["sent"] = sent
    counters["last_run_utc"] = now_utc().isoformat()

    # перевіряємо завершення
    finished = False
    if limit_times is not None and sent >= int(limit_times):
        finished = True
    if end_utc is not None and now_utc() >= datetime.fromisoformat(end_utc):
        finished = True

    with db() as con:
        if finished and kind == "once":
            con.execute("DELETE FROM reminders WHERE id=?", (r_id,))
        elif finished and kind in {"daily", "every_n_days", "every_x_hours"}:
            con.execute("UPDATE reminders SET is_paused=1, counters=? WHERE id=?",
                        (json.dumps(counters), r_id))
        else:
            con.execute("UPDATE reminders SET counters=? WHERE id=?",
                        (json.dumps(counters), r_id))
        con.commit()

    # ставимо наступний запуск (якщо не finished)
    if not finished:
        schedule_next(context, r_id)

# ---------- Commands ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню:", reply_markup=MAIN_KB)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, disable_web_page_preview=True)

async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /tz Europe/Kyiv
    if not context.args:
        tz = get_user_tz(update.effective_chat.id)
        return await update.message.reply_text(
            f"Поточна таймзона: {tz}\n"
            "Щоб змінити: `/tz Europe/Kyiv`",
            parse_mode="Markdown"
        )
    tzname = context.args[0]
    try:
        ZoneInfo(tzname)
    except Exception:
        return await update.message.reply_text("Невірна таймзона. Приклад: `Europe/Kyiv`", parse_mode="Markdown")
    set_user_tz(update.effective_chat.id, tzname)
    await update.message.reply_text(f"✅ Таймзона встановлена: {tzname}")

def get_user_tz(chat_id: int) -> str:
    # зберігаємо в окремій таблиці user_settings
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS user_settings
                       (chat_id INTEGER PRIMARY KEY, tz TEXT)""")
        row = con.execute("SELECT tz FROM user_settings WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row["tz"]:
            return row["tz"]
        return DEFAULT_TZ

def set_user_tz(chat_id: int, tz: str):
    with db() as con:
        con.execute("""INSERT INTO user_settings(chat_id,tz)
                       VALUES(?,?)
                       ON CONFLICT(chat_id) DO UPDATE SET tz=excluded.tz""", (chat_id, tz))
        con.commit()

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as con:
        rows = con.execute("SELECT * FROM reminders WHERE chat_id=? ORDER BY id DESC", (chat_id,)).fetchall()
    if not rows:
        return await update.message.reply_text("Нема активних нагадувань.")

    parts = []
    for r in rows:
        params = json.loads(r["params"])
        counters = json.loads(r["counters"] or "{}")
        status = "⏸" if r["is_paused"] else "▶️"
        parts.append(
            f"ID {r['id']} {status} • {r['kind']}\n"
            f"   «{r['text']}»\n"
            f"   TZ: {r['tz']} | sent: {counters.get('sent',0)}"
        )
    kb = [
        [InlineKeyboardButton(f"⏸ Пауза ID {r['id']}", callback_data=f"pause:{r['id']}"),
         InlineKeyboardButton(f"▶️ Резюм ID {r['id']}", callback_data=f"resume:{r['id']}") ]
        for r in rows
    ] + [
        [InlineKeyboardButton(f"❌ Видалити ID {r['id']}", callback_data=f"del:{r['id']}")]
        for r in rows
    ] + [[InlineKeyboardButton("« Назад", callback_data="back_main")]]

    await update.message.reply_text("\n\n".join(parts), reply_markup=InlineKeyboardMarkup(kb))

# ---------- Callbacks (menu & actions) ----------
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu_new":
        WIP[q.message.chat_id] = {"text": None, "kind": None, "params": {}, "end": {}}
        await q.message.edit_text("Введи текст нагадування (що нагадати):")
        return S_ENTER_TEXT

    if data == "menu_list":
        fake_update = Update(update.update_id, message=q.message)  # трохи хак для повторного використання
        await list_cmd(fake_update, context)
        return ConversationHandler.END

    if data == "menu_tz":
        tz = get_user_tz(q.message.chat_id)
        await q.message.edit_text(
            f"Поточна таймзона: {tz}\n"
            "Щоб змінити, надішли команду, напр.: `/tz Europe/Kyiv`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="back_main")]])
        )
        return ConversationHandler.END

    if data == "menu_help":
        await q.message.edit_text(HELP_TEXT, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="back_main")]]), disable_web_page_preview=True)
        return ConversationHandler.END

    if data == "back_main":
        await q.message.edit_text("Меню:", reply_markup=MAIN_KB)
        return ConversationHandler.END

    # actions on reminders
    if data.startswith("pause:"):
        r_id = int(data.split(":")[1])
        with db() as con:
            con.execute("UPDATE reminders SET is_paused=1 WHERE id=?", (r_id,))
            con.commit()
        if r_id in JOBBOOK:
            JOBBOOK[r_id].tg_job.schedule_removal()
            JOBBOOK.pop(r_id, None)
        await q.message.reply_text(f"⏸ Поставлено на паузу ID {r_id}")
        return ConversationHandler.END

    if data.startswith("resume:"):
        r_id = int(data.split(":")[1])
        with db() as con:
            con.execute("UPDATE reminders SET is_paused=0 WHERE id=?", (r_id,))
            con.commit()
        schedule_next(context, r_id)
        await q.message.reply_text(f"▶️ Відновлено ID {r_id}")
        return ConversationHandler.END

    if data.startswith("del:"):
        r_id = int(data.split(":")[1])
        with db() as con:
            con.execute("DELETE FROM reminders WHERE id=?", (r_id,))
            con.commit()
        if r_id in JOBBOOK:
            JOBBOOK[r_id].tg_job.schedule_removal()
            JOBBOOK.pop(r_id, None)
        await q.message.reply_text(f"❌ Видалено ID {r_id}")
        return ConversationHandler.END

# ---- Wizard steps ----
async def w_enter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    WIP[chat_id]["text"] = update.message.text.strip()
    await update.message.reply_text(
        "Оберіть тип повторення:", reply_markup=NEW_KB
    )
    return S_PICK_KIND

async def w_pick_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat_id

    if data == "new_once":
        WIP[chat_id]["kind"] = "once"
        await q.message.edit_text("Вкажи дату у форматі YYYY-MM-DD (наприклад 2025-10-28):")
        return S_ENTER_ONCE_DATE

    if data == "new_daily":
        WIP[chat_id]["kind"] = "daily"
        await q.message.edit_text("Вкажи час у форматі HH:MM (наприклад 09:30):")
        return S_ENTER_DAILY_TIME

    if data == "new_n_days":
        WIP[chat_id]["kind"] = "every_n_days"
        await q.message.edit_text("Вкажи N — кожні скільки днів? (наприклад 2):")
        return S_ENTER_N_DAYS_N

    if data == "new_x_hours":
        WIP[chat_id]["kind"] = "every_x_hours"
        await q.message.edit_text("Вкажи X — кожні скільки годин? (наприклад 2):")
        return S_ENTER_X_HOURS_X

    if data == "back_main":
        await q.message.edit_text("Меню:", reply_markup=MAIN_KB)
        return ConversationHandler.END

async def w_once_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        y, m, d = map(int, update.message.text.strip().split("-"))
        context.user_data["once_date"] = (y, m, d)
    except Exception:
        return await update.message.reply_text("Формат дати: YYYY-MM-DD")

    await update.message.reply_text("Вкажи час HH:MM (наприклад 19:05):")
    return S_ENTER_ONCE_TIME

async def w_once_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tzname = get_user_tz(chat_id)
    tz = ZoneInfo(tzname)
    try:
        t = parse_hhmm(update.message.text)
    except Exception as e:
        return await update.message.reply_text(str(e))

    y, m, d = context.user_data["once_date"]
    dt_local = datetime(y, m, d, t.hour, t.minute, tzinfo=tz)
    run_utc = next_once(dt_local, tz)

    draft = WIP[chat_id]
    draft["params"] = {"run_utc": run_utc.isoformat()}
    return await w_ending_options(update, context)

async def w_daily_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        t = parse_hhmm(update.message.text)
    except Exception as e:
        return await update.message.reply_text(str(e))
    draft = WIP[chat_id]
    draft["params"] = {"h": t.hour, "m": t.minute}
    return await w_ending_options(update, context)

async def w_n_days_n(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        n = int(update.message.text.strip())
        if n <= 0: raise ValueError
    except Exception:
        return await update.message.reply_text("Введи додатнє ціле N")
    context.user_data["n_days"] = n
    await update.message.reply_text("Вкажи час HH:MM (наприклад 10:00):")
    return S_ENTER_N_DAYS_TIME

async def w_n_days_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        t = parse_hhmm(update.message.text)
    except Exception as e:
        return await update.message.reply_text(str(e))
    n = context.user_data["n_days"]
    draft = WIP[chat_id]
    draft["params"] = {"n": n, "h": t.hour, "m": t.minute}
    return await w_ending_options(update, context)

async def w_x_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        x = int(update.message.text.strip())
        if x <= 0: raise ValueError
    except Exception:
        return await update.message.reply_text("Введи додатнє ціле X (години)")
    draft = WIP[chat_id]
    draft["params"] = {"x": x}
    return await w_ending_options(update, context)

async def w_ending_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Без завершення", callback_data="end_none")],
        [InlineKeyboardButton("Протягом D днів", callback_data="end_days")],
        [InlineKeyboardButton("За T разів", callback_data="end_times")],
    ])
    await update.message.reply_text("⬇️ Як завершувати нагадування?", reply_markup=kb)
    return S_ENDING

async def w_ending_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat_id

    if data == "end_none":
        WIP[chat_id]["end"] = {}
        return await confirm_draft(q, context)

    if data == "end_days":
        await q.message.edit_text("Скільки D днів працювати? (наприклад 7):")
        context.user_data["end_kind"] = "days"
        return S_ENDING

    if data == "end_times":
        await q.message.edit_text("Скільки T разів надіслати? (наприклад 10):")
        context.user_data["end_kind"] = "times"
        return S_ENDING

async def w_ending_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kind = context.user_data.get("end_kind")
    try:
        v = int(update.message.text.strip())
        if v <= 0: raise ValueError
    except Exception:
        return await update.message.reply_text("Введи додатнє ціле")

    end = {}
    if kind == "days":
        end["days"] = v
    elif kind == "times":
        end["times"] = v
    WIP[chat_id]["end"] = end
    return await confirm_draft(update, context)

async def confirm_draft(upd_or_q, context: ContextTypes.DEFAULT_TYPE):
    # upd_or_q може бути Update.message або CallbackQuery
    if isinstance(upd_or_q, Update):
        msg = upd_or_q.message
        chat_id = msg.chat_id
        edit = False
    else:
        q = upd_or_q
        msg = q.message
        chat_id = msg.chat_id
        edit = True

    draft = WIP[chat_id]
    tzname = get_user_tz(chat_id)
    kind = draft["kind"]
    p = draft["params"]
    end = draft["end"]

    human = f"Текст: «{draft['text']}»\nТип: {kind}\nTZ: {tzname}\n"
    if kind == "once":
        human += f"Коли: {p['run_utc']} (UTC)\n"
    elif kind == "daily":
        human += f"Час щодня: {p['h']:02d}:{p['m']:02d}\n"
    elif kind == "every_n_days":
        human += f"Кожні N днів: N={p['n']} о {p['h']:02d}:{p['m']:02d}\n"
    elif kind == "every_x_hours":
        human += f"Кожні X годин: X={p['x']}\n"
    if end:
        human += f"Завершення: {end}\n"
    else:
        human += "Завершення: без\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Зберегти", callback_data="confirm_save")],
        [InlineKeyboardButton("« Скасувати", callback_data="back_main")]
    ])
    if edit:
        await msg.edit_text(human, reply_markup=kb)
    else:
        await msg.reply_text(human, reply_markup=kb)
    return S_CONFIRM

async def do_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    draft = WIP.get(chat_id)
    if not draft:
        return await q.message.reply_text("Нема чернетки. /start")

    tzname = get_user_tz(chat_id)
    tz = ZoneInfo(tzname)
    text = draft["text"]
    kind = draft["kind"]
    params = draft["params"]

    # ліміти завершення
    counters = {"sent": 0, "limit_times": None, "end_utc": None, "last_run_utc": None}
    if draft["end"].get("times"):
        counters["limit_times"] = int(draft["end"]["times"])
    if draft["end"].get("days"):
        counters["end_utc"] = (now_utc() + timedelta(days=int(draft["end"]["days"]))).isoformat()

    # якщо once — перевіримо, що не в минулому
    if kind == "once":
        run_utc = datetime.fromisoformat(params["run_utc"])
        if run_utc <= now_utc():
            return await q.message.reply_text("⛔ Дата/час у минулому.")

    with db() as con:
        cur = con.execute("""INSERT INTO reminders
            (chat_id,text,kind,params,tz,is_paused,counters,created_utc)
            VALUES (?,?,?,?,?,0,?,?)""",
            (chat_id, text, kind, json.dumps(params), tzname,
             json.dumps(counters), now_utc().isoformat()))
        r_id = cur.lastrowid
        con.commit()

    schedule_next(context, r_id)
    await q.message.edit_text(f"✅ Збережено! (ID {r_id})", reply_markup=MAIN_KB)
    WIP.pop(chat_id, None)
    return ConversationHandler.END

# ---------- App startup ----------
def reschedule_all(app):
    with db() as con:
        rows = con.execute("SELECT id FROM reminders WHERE is_paused=0").fetchall()
    for r in rows:
        schedule_next(app, r["id"])

# ---------- Wire up ----------
def build_application():
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_cb, pattern="^menu_new$")],
        states={
            S_ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_enter_text)],
            S_PICK_KIND: [CallbackQueryHandler(w_pick_kind)],
            S_ENTER_ONCE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_once_date)],
            S_ENTER_ONCE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_once_time)],
            S_ENTER_DAILY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_daily_time)],
            S_ENTER_N_DAYS_N: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_n_days_n)],
            S_ENTER_N_DAYS_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_n_days_time)],
            S_ENTER_X_HOURS_X: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_x_hours)],
            S_ENDING: [
                CallbackQueryHandler(w_ending_choice, pattern="^end_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, w_ending_value),
            ],
            S_CONFIRM: [CallbackQueryHandler(do_save, pattern="^confirm_save$"),
                        CallbackQueryHandler(menu_cb, pattern="^back_main$")],
        },
        fallbacks=[CallbackQueryHandler(menu_cb, pattern="^back_main$")],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^(menu_|back_main|pause:|resume:|del:)"))
    app.add_handler(conv)

    return app

async def main():
    init_db()
    app = build_application()
    await app.initialize()
    await app.start()
    # показати меню при першому /start
    log.info("Reminder bot is running (polling)…")
    reschedule_all(app)  # відновлюємо джоби
    await app.updater.start_polling()
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

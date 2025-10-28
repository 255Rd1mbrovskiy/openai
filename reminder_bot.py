import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Optional
import secrets

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackContext,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, JobQueue, Job
)

# ---------------------- CONFIG ----------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")  # обов'язково додай у Render -> Environment
DEFAULT_TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("reminder-bot")

# ---------------------- STORAGE ----------------------
# Пам'ять у процесі. На рестарті зникне. Якщо потрібно збереження — додамо БД/файл.
@dataclass
class Ending:
    kind: str = "none"         # "none" | "days" | "times"
    days: int = 0
    times: int = 0

@dataclass
class Rem:
    id: str
    chat_id: int
    text: str
    kind: str                 # "once" | "daily" | "n_days" | "x_hours"
    tz: ZoneInfo
    next_dt: datetime
    n: int = 0               # для n_days або x_hours (у годинах)
    hhmm: Optional[str] = None  # "HH:MM" для daily/n_days
    ending: Ending = field(default_factory=Ending)
    left_times: Optional[int] = None
    end_date: Optional[datetime] = None
    paused: bool = False
    job: Optional[Job] = None

@dataclass
class ChatState:
    tz: ZoneInfo = DEFAULT_TZ
    items: Dict[str, Rem] = field(default_factory=dict)

STORE: Dict[int, ChatState] = {}

def get_state(chat_id: int) -> ChatState:
    if chat_id not in STORE:
        STORE[chat_id] = ChatState()
    return STORE[chat_id]

# ---------------------- CONVERSATION STATES ----------------------
(
    S_ENTER_TEXT,
    S_PICK_KIND,
    S_ENTER_ONCE_DT,
    S_ENTER_DAILY_TIME,
    S_ENTER_N_DAYS,
    S_ENTER_X_HOURS,
    S_ENDING,
    S_CONFIRM
) = range(8)

# ---------------------- UTIL ----------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def parse_hhmm(s: str) -> Optional[tuple]:
    try:
        hh, mm = s.strip().split(":")
        h = int(hh); m = int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return None

def pretty_dt(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Створити нагадування", callback_data="menu_new")],
        [InlineKeyboardButton("📄 Список нагадувань", callback_data="menu_list")],
        [InlineKeyboardButton("🕒 Встановити таймзону", callback_data="menu_tz")],
        [InlineKeyboardButton("ℹ️ Довідка", callback_data="menu_help")],
    ])

def kb_kinds():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Одноразове (дата+час)", callback_data="k_once"),
        ],
        [
            InlineKeyboardButton("Щодня у HH:MM", callback_data="k_daily"),
            InlineKeyboardButton("Кожні N днів у HH:MM", callback_data="k_ndays"),
        ],
        [
            InlineKeyboardButton("Кожні X годин", callback_data="k_xhours"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ])

def kb_ending():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Без кінця", callback_data="end_none"),
            InlineKeyboardButton("Протягом D днів", callback_data="end_days"),
            InlineKeyboardButton("T разів", callback_data="end_times"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ])

def kb_confirm():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Зберегти", callback_data="confirm_save"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main"),
        ]
    ])

def kb_item(rem: Rem):
    if rem.paused:
        pr_btn = InlineKeyboardButton("▶️ Відновити", callback_data=f"resume:{rem.id}")
    else:
        pr_btn = InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{rem.id}")
    return InlineKeyboardMarkup([
        [pr_btn, InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{rem.id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
    ])

# ---------------------- SCHEDULING ----------------------
def schedule_rem(app: Application, rem: Rem):
    """Поставити/перепоставити job на next_dt."""
    if rem.job:
        try:
            rem.job.schedule_removal()
        except Exception:
            pass
        rem.job = None

    delay = (rem.next_dt - utcnow()).total_seconds()
    if delay < 1:
        delay = 1

    rem.job = app.job_queue.run_once(job_fire, when=delay, data={"id": rem.id}, name=rem.id)
    log.info("scheduled %s at %s", rem.id, rem.next_dt)

def compute_next(rem: Rem):
    tz = rem.tz
    now_local = utcnow().astimezone(tz)
    if rem.kind == "once":
        return  # уже є точна дата
    if rem.kind == "daily":
        h, m = parse_hhmm(rem.hhmm)
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        rem.next_dt = candidate.astimezone(timezone.utc)
        return
    if rem.kind == "n_days":
        h, m = parse_hhmm(rem.hhmm)
        if rem.next_dt and rem.next_dt > utcnow():
            return  # уже розписано наперед
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=rem.n)
        rem.next_dt = candidate.astimezone(timezone.utc)
        return
    if rem.kind == "x_hours":
        if rem.next_dt and rem.next_dt > utcnow():
            return
        rem.next_dt = (now_local + timedelta(hours=rem.n)).astimezone(timezone.utc)
        return

def reschedule_after_fire(rem: Rem):
    tz = rem.tz
    now_local = utcnow().astimezone(tz)

    if rem.kind == "once":
        rem.next_dt = None
        return

    if rem.kind == "daily":
        h, m = parse_hhmm(rem.hhmm)
        nxt = now_local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
        rem.next_dt = nxt.astimezone(timezone.utc)

    elif rem.kind == "n_days":
        h, m = parse_hhmm(rem.hhmm)
        base = rem.next_dt.astimezone(tz) if rem.next_dt else now_local
        nxt = base.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=rem.n)
        rem.next_dt = nxt.astimezone(timezone.utc)

    elif rem.kind == "x_hours":
        base = rem.next_dt.astimezone(tz) if rem.next_dt else now_local
        rem.next_dt = (base + timedelta(hours=rem.n)).astimezone(timezone.utc)

# ---------------------- JOB CALLBACK ----------------------
async def job_fire(ctx: CallbackContext):
    rem_id = ctx.job.data["id"]
    # знайти reminder
    rem: Optional[Rem] = None
    for ch_id, st in STORE.items():
        if rem_id in st.items:
            rem = st.items[rem_id]
            break
    if not rem:
        return

    if rem.paused:
        return

    # відправка
    try:
        await ctx.bot.send_message(
            chat_id=rem.chat_id,
            text=f"⏰ *Нагадування*: {rem.text}",
            parse_mode="Markdown",
            reply_markup=kb_item(rem),
        )
    except Exception as e:
        log.exception("send failed: %s", e)

    # логіка завершення
    if rem.ending.kind == "times":
        if rem.left_times is None:
            rem.left_times = rem.ending.times
        rem.left_times -= 1
        if rem.left_times <= 0:
            # видаляємо
            try:
                rem.job.schedule_removal()
            except Exception:
                pass
            st = get_state(rem.chat_id)
            st.items.pop(rem.id, None)
            return

    if rem.ending.kind == "days" and rem.end_date:
        if utcnow().astimezone(rem.tz) >= rem.end_date:
            try:
                rem.job.schedule_removal()
            except Exception:
                pass
            st = get_state(rem.chat_id)
            st.items.pop(rem.id, None)
            return

    # підрахувати наступний раз і поставити ще раз
    reschedule_after_fire(rem)
    if rem.next_dt:
        schedule_rem(ctx.application, rem)

# ---------------------- COMMANDS ----------------------
async def start_cmd(upd: Update, ctx: CallbackContext):
    st = get_state(upd.effective_chat.id)
    await upd.effective_message.reply_text(
        "👋 Привіт! Я — бот-нагадувач.\n"
        "Просто натисни кнопку нижче, щоб створити нагадування.",
        reply_markup=kb_main(),
    )

async def help_cmd(upd: Update, ctx: CallbackContext):
    await upd.effective_message.reply_text(
        "Я вмію:\n"
        "• Одноразові нагадування (дата+час)\n"
        "• Щоденні в HH:MM\n"
        "• Кожні N днів у HH:MM\n"
        "• Кожні X годин\n"
        "• Обмеження: без кінця / протягом D днів / T разів\n\n"
        "Команди:\n"
        "/start — меню\n"
        "/list — список нагадувань\n"
        "/tz <Region/City> — встановити таймзону (напр. /tz Europe/Kyiv)\n"
        "/help — довідка"
    )

async def tz_cmd(upd: Update, ctx: CallbackContext):
    chat_id = upd.effective_chat.id
    st = get_state(chat_id)
    if ctx.args:
        try:
            tz = ZoneInfo(ctx.args[0])
            st.tz = tz
            await upd.effective_message.reply_text(f"✅ Таймзона встановлена: {ctx.args[0]}")
        except Exception:
            await upd.effective_message.reply_text("❌ Невірна таймзона. Приклад: /tz Europe/Kyiv")
    else:
        await upd.effective_message.reply_text(f"Поточна таймзона: {st.tz.key if hasattr(st.tz,'key') else st.tz}")

async def list_cmd(upd: Update, ctx: CallbackContext):
    chat_id = upd.effective_chat.id
    st = get_state(chat_id)
    if not st.items:
        await upd.effective_message.reply_text("Список порожній.", reply_markup=kb_main())
        return
    parts = []
    for r in st.items.values():
        status = "⏸" if r.paused else "▶️"
        nxt = pretty_dt(r.next_dt, r.tz) if r.next_dt else "—"
        parts.append(f"{status} *{r.text}*\n   id:`{r.id}`  наступне: {nxt}")
    await upd.effective_message.reply_text(
        "\n\n".join(parts), parse_mode="Markdown", reply_markup=kb_main()
    )

# ---------------------- MENU CALLBACKS ----------------------
async def menu_cb(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    st = get_state(chat_id)

    data = q.data
    if data == "menu_new":
        await q.message.reply_text(
            "Введи текст нагадування (що нагадати):",
        )
        return S_ENTER_TEXT

    if data == "menu_list":
        return await list_cmd(upd, ctx)

    if data == "menu_tz":
        await q.message.reply_text(
            "Надішли команду виду: /tz Europe/Kyiv",
        )
        return ConversationHandler.END

    if data == "menu_help":
        return await help_cmd(upd, ctx)

    # кнопки керування елементом
    if data.startswith("pause:"):
        rid = data.split(":", 1)[1]
        r = st.items.get(rid)
        if r:
            r.paused = True
            if r.job:
                try:
                    r.job.schedule_removal()
                except Exception:
                    pass
                r.job = None
            await q.edit_message_reply_markup(reply_markup=kb_item(r))
        return

    if data.startswith("resume:"):
        rid = data.split(":", 1)[1]
        r = st.items.get(rid)
        if r:
            if not r.next_dt:
                compute_next(r)
            r.paused = False
            schedule_rem(ctx.application, r)
            await q.edit_message_reply_markup(reply_markup=kb_item(r))
        return

    if data.startswith("del:"):
        rid = data.split(":", 1)[1]
        r = st.items.pop(rid, None)
        if r and r.job:
            try:
                r.job.schedule_removal()
            except Exception:
                pass
        await q.edit_message_text("🗑 Видалено.", reply_markup=kb_main())
        return

    if data == "back_main":
        await q.message.reply_text("Головне меню:", reply_markup=kb_main())
        return ConversationHandler.END

# ---------------------- CONVERSATION FLOW ----------------------
async def w_enter_text(upd: Update, ctx: CallbackContext):
    txt = upd.effective_message.text.strip()
    ctx.user_data["tmp_text"] = txt
    await upd.effective_message.reply_text(
        "Обери тип нагадування:", reply_markup=kb_kinds()
    )
    return S_PICK_KIND

async def w_pick_kind(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    data = q.data
    if data == "k_once":
        await q.message.reply_text(
            "Введи *дату і час* у форматі `YYYY-MM-DD HH:MM` (твоя таймзона).",
            parse_mode="Markdown",
        )
        return S_ENTER_ONCE_DT

    if data == "k_daily":
        await q.message.reply_text("Введи час HH:MM (щодня).")
        return S_ENTER_DAILY_TIME

    if data == "k_ndays":
        await q.message.reply_text("Введи N і час: наприклад `2 09:00` (раз на 2 дні о 09:00).")
        return S_ENTER_N_DAYS

    if data == "k_xhours":
        await q.message.reply_text("Введи X — кожні X годин (наприклад `3`).")
        return S_ENTER_X_HOURS

async def w_once_dt(upd: Update, ctx: CallbackContext):
    chat_id = upd.effective_chat.id
    st = get_state(chat_id)
    s = upd.effective_message.text.strip()
    try:
        dt_local = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=st.tz)
        if dt_local < datetime.now(st.tz):
            await upd.effective_message.reply_text("Дата/час у минулому. Спробуй ще раз.")
            return S_ENTER_ONCE_DT
        ctx.user_data["tmp_kind"] = "once"
        ctx.user_data["tmp_next"] = dt_local.astimezone(timezone.utc)
        await upd.effective_message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING
    except Exception:
        await upd.effective_message.reply_text("Невірний формат. Спробуй ще раз у вигляді YYYY-MM-DD HH:MM.")
        return S_ENTER_ONCE_DT

async def w_daily_time(upd: Update, ctx: CallbackContext):
    s = upd.effective_message.text.strip()
    ok = parse_hhmm(s)
    if not ok:
        await upd.effective_message.reply_text("Невірний формат. Введи HH:MM.")
        return S_ENTER_DAILY_TIME
    ctx.user_data["tmp_kind"] = "daily"
    ctx.user_data["tmp_hhmm"] = s
    await upd.effective_message.reply_text("Обери обмеження:", reply_markup=kb_ending())
    return S_ENDING

async def w_n_days(upd: Update, ctx: CallbackContext):
    s = upd.effective_message.text.strip()
    try:
        n_s, hhmm = s.split()
        n = int(n_s)
        if n <= 0:
            raise ValueError()
        if not parse_hhmm(hhmm):
            raise ValueError()
        ctx.user_data["tmp_kind"] = "n_days"
        ctx.user_data["tmp_n"] = n
        ctx.user_data["tmp_hhmm"] = hhmm
        await upd.effective_message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING
    except Exception:
        await upd.effective_message.reply_text("Приклад: `2 09:00`", parse_mode="Markdown")
        return S_ENTER_N_DAYS

async def w_x_hours(upd: Update, ctx: CallbackContext):
    s = upd.effective_message.text.strip()
    try:
        n = int(s)
        if n <= 0:
            raise ValueError()
        ctx.user_data["tmp_kind"] = "x_hours"
        ctx.user_data["tmp_n"] = n
        await upd.effective_message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING
    except Exception:
        await upd.effective_message.reply_text("Введи додатне ціле число, напр. `3`.")
        return S_ENTER_X_HOURS

async def w_ending_choice(upd: Update, ctx: CallbackContext):
    # ловимо кнопки end_...
    pass

async def w_ending(upd: Update, ctx: CallbackContext):
    pass

async def w_ending_choice(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    kind = q.data  # end_none / end_days / end_times
    ctx.user_data["end_choice"] = kind
    if kind == "end_none":
        ctx.user_data["ending"] = Ending("none")
        return await show_confirm(q, ctx)
    if kind == "end_days":
        await q.message.reply_text("Введи D — кількість днів тривалості (наприклад `7`).")
        return S_ENDING
    if kind == "end_times":
        await q.message.reply_text("Введи T — скільки разів надіслати (наприклад `10`).")
        return S_ENDING

async def w_ending_value(upd: Update, ctx: CallbackContext):
    choice = ctx.user_data.get("end_choice")
    s = upd.effective_message.text.strip()
    if choice == "end_days":
        try:
            d = int(s)
            if d <= 0: raise ValueError()
            ctx.user_data["ending"] = Ending("days", days=d)
            return await show_confirm(upd, ctx)
        except Exception:
            await upd.effective_message.reply_text("Введи додатне ціле число днів, напр. `7`.")
            return S_ENDING
    if choice == "end_times":
        try:
            t = int(s)
            if t <= 0: raise ValueError()
            ctx.user_data["ending"] = Ending("times", times=t)
            return await show_confirm(upd, ctx)
        except Exception:
            await upd.effective_message.reply_text("Введи додатне ціле число разів, напр. `10`.")
            return S_ENDING
    # safety
    ctx.user_data["ending"] = Ending("none")
    return await show_confirm(upd, ctx)

async def show_confirm(src, ctx: CallbackContext):
    # src може бути Update або CallbackQuery.msg container
    if hasattr(src, "effective_message"):
        msg = src.effective_message
        chat_id = src.effective_chat.id
    else:
        msg = src.message
        chat_id = src.message.chat.id

    st = get_state(chat_id)
    tz = st.tz

    text = ctx.user_data["tmp_text"]
    kind = ctx.user_data["tmp_kind"]
    ending: Ending = ctx.user_data.get("ending", Ending("none"))

    # Зібрати попередній next_dt
    if kind == "once":
        next_dt = ctx.user_data["tmp_next"]
        preview = pretty_dt(next_dt, tz)
    elif kind == "daily":
        hhmm = ctx.user_data["tmp_hhmm"]
        h, m = parse_hhmm(hhmm)
        now_local = datetime.now(tz)
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        next_dt = candidate.astimezone(timezone.utc)
        preview = pretty_dt(next_dt, tz) + f" (щодня {hhmm})"
    elif kind == "n_days":
        n = ctx.user_data["tmp_n"]; hhmm = ctx.user_data["tmp_hhmm"]
        h, m = parse_hhmm(hhmm)
        now_local = datetime.now(tz)
        candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=n)
        next_dt = candidate.astimezone(timezone.utc)
        preview = pretty_dt(next_dt, tz) + f" (кожні {n} дн. о {hhmm})"
    else:  # x_hours
        n = ctx.user_data["tmp_n"]
        next_dt = (datetime.now(tz) + timedelta(hours=n)).astimezone(timezone.utc)
        preview = pretty_dt(next_dt, tz) + f" (кожні {n} год.)"

    ctx.user_data["computed_next"] = next_dt

    if ending.kind == "none":
        end_txt = "без обмежень"
    elif ending.kind == "days":
        end_txt = f"протягом {ending.days} днів"
    else:
        end_txt = f"{ending.times} разів"

    await msg.reply_text(
        f"Перевір:\n"
        f"• Текст: *{text}*\n"
        f"• Перше спрацювання: *{preview}*\n"
        f"• Обмеження: *{end_txt}*",
        parse_mode="Markdown",
        reply_markup=kb_confirm(),
    )
    return S_CONFIRM

async def do_save(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    st = get_state(chat_id)
    tz = st.tz

    rid = secrets.token_hex(3)
    text = ctx.user_data["tmp_text"]
    kind = ctx.user_data["tmp_kind"]
    ending: Ending = ctx.user_data.get("ending", Ending("none"))
    next_dt = ctx.user_data["computed_next"]

    rem = Rem(
        id=rid, chat_id=chat_id, text=text, kind=kind, tz=tz,
        next_dt=next_dt,
    )
    if kind == "daily":
        rem.hhmm = ctx.user_data["tmp_hhmm"]
    if kind == "n_days":
        rem.n = ctx.user_data["tmp_n"]
        rem.hhmm = ctx.user_data["tmp_hhmm"]
    if kind == "x_hours":
        rem.n = ctx.user_data["tmp_n"]
    if kind == "once":
        # nothing extra
        pass

    rem.ending = ending
    if ending.kind == "times":
        rem.left_times = ending.times
    if ending.kind == "days":
        rem.end_date = datetime.now(tz) + timedelta(days=ending.days)

    st.items[rem.id] = rem
    schedule_rem(ctx.application, rem)

    await q.edit_message_text(
        f"✅ Збережено! id:`{rem.id}`\nПерше спрацювання: *{pretty_dt(rem.next_dt, tz)}*",
        parse_mode="Markdown",
        reply_markup=kb_item(rem),
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ---------------------- BUILD APP (ВАЖЛИВО: порядок!) ----------------------
def build_application() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_cb, pattern="^menu_new$")],
        states={
            S_ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_enter_text)],
            S_PICK_KIND: [CallbackQueryHandler(w_pick_kind)],
            S_ENTER_ONCE_DT: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_once_dt)],
            S_ENTER_DAILY_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_daily_time)],
            S_ENTER_N_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_n_days)],
            S_ENTER_X_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_x_hours)],
            S_ENDING: [
                CallbackQueryHandler(w_ending_choice, pattern="^end_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, w_ending_value),
            ],
            S_CONFIRM: [
                CallbackQueryHandler(do_save, pattern="^confirm_save$"),
                CallbackQueryHandler(menu_cb, pattern="^back_main$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(menu_cb, pattern="^back_main$")],
        allow_reentry=True,
    )

    # Команди
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("list", list_cmd))

    # 1) Додаємо розмову раніше
    app.add_handler(conv)

    # 2) Потім — загальний callback-меню/керування
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^(menu_|back_main|pause:|resume:|del:)"))

    log.info("bot ready")
    return app

# ---------------------- MAIN ----------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No TELEGRAM_TOKEN provided")
    application = build_application()
    application.run_polling(close_loop=False)

import os
import logging
import calendar as pycal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from typing import Dict, Optional
import secrets

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackContext,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, Job, JobQueue
)

# ---------------------- CONFIG ----------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
DEFAULT_TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("reminder-bot")

# ---------------------- STORAGE ----------------------
@dataclass
class Ending:
    kind: str = "none"        # "none" | "days" | "times"
    days: int = 0
    times: int = 0

@dataclass
class Rem:
    id: str
    chat_id: int
    text: str
    kind: str                  # "once" | "daily" | "n_days" | "x_hours"
    tz: ZoneInfo
    next_dt: datetime
    n: int = 0
    hhmm: Optional[str] = None
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
    S_ENTER_ONCE_DT,      # (не використовується тепер, але лишаю як посв.)
    S_ENTER_DAILY_TIME,   # теж не юзаємо напряму
    S_ENTER_N_DAYS,
    S_ENTER_X_HOURS,
    S_ENDING,
    S_CONFIRM,
    S_PICK_DATE,          # новий: вибір дати для once
    S_PICK_TIME,          # новий: вибір часу для once/daily/n_days
    S_ENTER_N_ONLY        # новий: вводимо N (кожні N днів)
) = range(11)

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
        [InlineKeyboardButton("Одноразове (дата+час)", callback_data="k_once")],
        [
            InlineKeyboardButton("Щодня у HH:MM", callback_data="k_daily"),
            InlineKeyboardButton("Кожні N днів у HH:MM", callback_data="k_ndays"),
        ],
        [InlineKeyboardButton("Кожні X годин", callback_data="k_xhours")],
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

# ---------------------- CALENDAR / TIME PICKER ----------------------
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    """Inline календар для обраного місяця."""
    cal = pycal.Calendar(firstweekday=0)  # Monday
    rows = []

    # Заголовок місяця
    rows.append([
        InlineKeyboardButton("‹", callback_data=f"calp:{year}-{month:02d}"),
        InlineKeyboardButton(f"{year}-{month:02d}", callback_data="noop"),
        InlineKeyboardButton("›", callback_data=f"caln:{year}-{month:02d}"),
    ])

    # Шапка днів тижня
    rows.append([InlineKeyboardButton(w, callback_data="noop") for w in WEEKDAYS])

    # Тіло місяця
    for week in cal.monthdayscalendar(year, month):
        btns = []
        for d in week:
            if d == 0:
                btns.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                btns.append(InlineKeyboardButton(str(d), callback_data=f"cald:{year}-{month:02d}-{d:02d}"))
        rows.append(btns)

    # Низ
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_kind")])
    return InlineKeyboardMarkup(rows)

def build_time_picker(h: int, m: int) -> InlineKeyboardMarkup:
    """Inline пікер часу."""
    disp = f"{h:02d}:{m:02d}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("− год", callback_data="tp:h-"),
         InlineKeyboardButton(f"🕐 {disp}", callback_data="noop"),
         InlineKeyboardButton("+ год", callback_data="tp:h+")],
        [InlineKeyboardButton("− хв", callback_data="tp:m-"),
         InlineKeyboardButton("00", callback_data="tp:q:00"),
         InlineKeyboardButton("15", callback_data="tp:q:15"),
         InlineKeyboardButton("30", callback_data="tp:q:30"),
         InlineKeyboardButton("45", callback_data="tp:q:45"),
         InlineKeyboardButton("+ хв", callback_data="tp:m+")],
        [InlineKeyboardButton("✅ ОК", callback_data="tp:ok"),
         InlineKeyboardButton("⬅️ Назад", callback_data="back_kind")]
    ])

def round_future_30(now_local: datetime) -> tuple:
    """Найближчі майбутні 00/30 хв."""
    h = now_local.hour
    m = 30 if now_local.minute < 30 else 0
    if m == 0:
        # якщо було >=30, переносимо на наступну годину
        h = (h + 1) % 24
    return h, m

# ---------------------- SCHEDULING ----------------------
def schedule_rem(app: Application, rem: Rem):
    if rem.job:
        try:
            rem.job.schedule_removal()
        except Exception:
            pass
        rem.job = None

    delay = max(1, int((rem.next_dt - utcnow()).total_seconds()))
    rem.job = app.job_queue.run_once(job_fire, when=delay, data={"id": rem.id}, name=rem.id)
    log.info("scheduled %s at %s", rem.id, rem.next_dt)

def reschedule_after_fire(rem: Rem):
    tz = rem.tz
    now_local = utcnow().astimezone(tz)
    if rem.kind == "once":
        rem.next_dt = None
        return
    if rem.kind == "daily":
        h, m = parse_hhmm(rem.hhmm)
        rem.next_dt = (now_local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    elif rem.kind == "n_days":
        h, m = parse_hhmm(rem.hhmm)
        base = rem.next_dt.astimezone(tz) if rem.next_dt else now_local
        rem.next_dt = (base.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=rem.n)).astimezone(timezone.utc)
    elif rem.kind == "x_hours":
        base = rem.next_dt.astimezone(tz) if rem.next_dt else now_local
        rem.next_dt = (base + timedelta(hours=rem.n)).astimezone(timezone.utc)

# ---------------------- JOB CALLBACK ----------------------
async def job_fire(ctx: CallbackContext):
    rem_id = ctx.job.data["id"]
    rem: Optional[Rem] = None
    for ch_id, st in STORE.items():
        if rem_id in st.items:
            rem = st.items[rem_id]
            break
    if not rem or rem.paused:
        return

    try:
        await ctx.bot.send_message(rem.chat_id, f"⏰ *Нагадування*: {rem.text}", parse_mode="Markdown", reply_markup=kb_item(rem))
    except Exception as e:
        log.exception("send failed: %s", e)

    # обмеження
    if rem.ending.kind == "times":
        rem.left_times = (rem.left_times or rem.ending.times) - 1
        if rem.left_times <= 0:
            try:
                rem.job.schedule_removal()
            except Exception:
                pass
            get_state(rem.chat_id).items.pop(rem.id, None)
            return
    if rem.ending.kind == "days" and rem.end_date:
        if utcnow().astimezone(rem.tz) >= rem.end_date:
            try:
                rem.job.schedule_removal()
            except Exception:
                pass
            get_state(rem.chat_id).items.pop(rem.id, None)
            return

    reschedule_after_fire(rem)
    if rem.next_dt:
        schedule_rem(ctx.application, rem)

# ---------------------- COMMANDS ----------------------
async def start_cmd(upd: Update, ctx: CallbackContext):
    await upd.effective_message.reply_text(
        "👋 Привіт! Я — бот-нагадувач.\nНатисни кнопку, щоб створити нагадування.",
        reply_markup=kb_main(),
    )

async def help_cmd(upd: Update, ctx: CallbackContext):
    await upd.effective_message.reply_text(
        "Можливості:\n"
        "• Одноразові (календар + час)\n"
        "• Щодня у HH:MM (пікер часу)\n"
        "• Кожні N днів у HH:MM (спершу N, потім пікер часу)\n"
        "• Кожні X годин (число)\n"
        "• Обмеження: без кінця / D днів / T разів\n\n"
        "Команди: /start /list /tz Europe/Kyiv /help"
    )

async def tz_cmd(upd: Update, ctx: CallbackContext):
    st = get_state(upd.effective_chat.id)
    if ctx.args:
        try:
            st.tz = ZoneInfo(ctx.args[0])
            await upd.effective_message.reply_text(f"✅ Таймзона встановлена: {ctx.args[0]}")
        except Exception:
            await upd.effective_message.reply_text("❌ Невірна таймзона. Приклад: /tz Europe/Kyiv")
    else:
        await upd.effective_message.reply_text(f"Поточна таймзона: {getattr(st.tz,'key',str(st.tz))}")

async def list_cmd(upd: Update, ctx: CallbackContext):
    st = get_state(upd.effective_chat.id)
    if not st.items:
        await upd.effective_message.reply_text("Список порожній.", reply_markup=kb_main())
        return
    parts = []
    for r in st.items.values():
        mark = "⏸" if r.paused else "▶️"
        nxt = pretty_dt(r.next_dt, r.tz) if r.next_dt else "—"
        parts.append(f"{mark} *{r.text}*\n   id:`{r.id}` наступне: {nxt}")
    await upd.effective_message.reply_text("\n\n".join(parts), parse_mode="Markdown", reply_markup=kb_main())

# ---------------------- MENU CALLBACKS ----------------------
async def menu_cb(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    data = q.data
    chat_id = q.message.chat.id
    st = get_state(chat_id)

    if data == "menu_new":
        await q.message.reply_text("Введи текст нагадування (що нагадати):")
        return S_ENTER_TEXT

    if data == "menu_list":
        return await list_cmd(upd, ctx)

    if data == "menu_tz":
        await q.message.reply_text("Надішли команду: /tz Europe/Kyiv")
        return ConversationHandler.END

    if data == "menu_help":
        return await help_cmd(upd, ctx)

    # керування елементом
    if data.startswith("pause:"):
        rid = data.split(":",1)[1]; r = st.items.get(rid)
        if r:
            r.paused = True
            if r.job:
                try: r.job.schedule_removal()
                except: pass
                r.job = None
            await q.edit_message_reply_markup(reply_markup=kb_item(r))
        return
    if data.startswith("resume:"):
        rid = data.split(":",1)[1]; r = st.items.get(rid)
        if r:
            r.paused = False
            if not r.next_dt:
                # підстрахуємо
                if r.kind == "daily":
                    h,m = parse_hhmm(r.hhmm)
                    now = datetime.now(r.tz)
                    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if cand <= now: cand += timedelta(days=1)
                    r.next_dt = cand.astimezone(timezone.utc)
                elif r.kind == "n_days":
                    h,m = parse_hhmm(r.hhmm)
                    now = datetime.now(r.tz)
                    cand = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=r.n)
                    r.next_dt = cand.astimezone(timezone.utc)
                elif r.kind == "x_hours":
                    r.next_dt = (datetime.now(r.tz)+timedelta(hours=r.n)).astimezone(timezone.utc)
            schedule_rem(ctx.application, r)
            await q.edit_message_reply_markup(reply_markup=kb_item(r))
        return
    if data.startswith("del:"):
        rid = data.split(":",1)[1]; r = st.items.pop(rid, None)
        if r and r.job:
            try: r.job.schedule_removal()
            except: pass
        await q.edit_message_text("🗑 Видалено.", reply_markup=kb_main())
        return

    if data == "back_main":
        await q.message.reply_text("Головне меню:", reply_markup=kb_main())
        return ConversationHandler.END

# ---------------------- CONVERSATION FLOW ----------------------
async def w_enter_text(upd: Update, ctx: CallbackContext):
    ctx.user_data["tmp_text"] = upd.effective_message.text.strip()
    await upd.effective_message.reply_text("Обери тип нагадування:", reply_markup=kb_kinds())
    return S_PICK_KIND

async def w_pick_kind(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    st = get_state(q.message.chat.id)
    data = q.data

    if data == "k_once":
        ctx.user_data["tmp_kind"] = "once"
        now = datetime.now(st.tz)
        ctx.user_data["pick_year"] = now.year
        ctx.user_data["pick_month"] = now.month
        await q.message.reply_text("Вибери дату:", reply_markup=build_calendar(now.year, now.month))
        return S_PICK_DATE

    if data == "k_daily":
        ctx.user_data["tmp_kind"] = "daily"
        now = datetime.now(st.tz)
        h,m = round_future_30(now)
        ctx.user_data["tp_h"] = h; ctx.user_data["tp_m"] = m
        await q.message.reply_text("Вибери час:", reply_markup=build_time_picker(h,m))
        return S_PICK_TIME

    if data == "k_ndays":
        ctx.user_data["tmp_kind"] = "n_days"
        await q.message.reply_text("Введи N — кожні N днів (напр. `2`).")
        return S_ENTER_N_ONLY

    if data == "k_xhours":
        ctx.user_data["tmp_kind"] = "x_hours"
        await q.message.reply_text("Введи X — кожні X годин (напр. `3`).")
        return S_ENTER_X_HOURS

async def w_enter_n_only(upd: Update, ctx: CallbackContext):
    s = upd.effective_message.text.strip()
    try:
        n = int(s)
        if n <= 0: raise ValueError()
        ctx.user_data["tmp_n"] = n
        st = get_state(upd.effective_chat.id)
        now = datetime.now(st.tz)
        h,m = round_future_30(now)
        ctx.user_data["tp_h"] = h; ctx.user_data["tp_m"] = m
        await upd.effective_message.reply_text(f"N = {n}. Тепер вибери час:", reply_markup=build_time_picker(h,m))
        return S_PICK_TIME
    except Exception:
        await upd.effective_message.reply_text("Введи додатне ціле число, напр. `2`.")
        return S_ENTER_N_ONLY

# ----- calendar handlers -----
async def calendar_cb(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    st = get_state(q.message.chat.id)
    data = q.data

    year = ctx.user_data.get("pick_year", datetime.now(st.tz).year)
    month = ctx.user_data.get("pick_month", datetime.now(st.tz).month)

    if data.startswith("calp:"):  # prev month
        y,m = map(int, data.split(":")[1].split("-"))
        month = m - 1
        year = y
        if month == 0:
            month = 12; year -= 1
        ctx.user_data["pick_year"] = year; ctx.user_data["pick_month"] = month
        await q.edit_message_reply_markup(reply_markup=build_calendar(year, month))
        return S_PICK_DATE

    if data.startswith("caln:"):  # next month
        y,m = map(int, data.split(":")[1].split("-"))
        month = m + 1
        year = y
        if month == 13:
            month = 1; year += 1
        ctx.user_data["pick_year"] = year; ctx.user_data["pick_month"] = month
        await q.edit_message_reply_markup(reply_markup=build_calendar(year, month))
        return S_PICK_DATE

    if data.startswith("cald:"):
        y, m, d = map(int, data.split(":")[1].split("-"))
        ctx.user_data["picked_date"] = date(y,m,d)
        # далі час
        now = datetime.now(st.tz)
        h, mm = round_future_30(now)
        ctx.user_data["tp_h"] = h; ctx.user_data["tp_m"] = mm
        await q.message.reply_text(f"Обрана дата: {y}-{m:02d}-{d:02d}. Вибери час:", reply_markup=build_time_picker(h,mm))
        return S_PICK_TIME

    if data == "back_kind":
        await q.message.reply_text("Обери тип:", reply_markup=kb_kinds())
        return S_PICK_KIND

# ----- time picker handlers -----
def clamp_time(h: int, m: int) -> tuple:
    h %= 24
    m %= 60
    return h, m

async def time_cb(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    data = q.data

    h = ctx.user_data.get("tp_h", 9)
    m = ctx.user_data.get("tp_m", 0)

    if data == "tp:h-":
        h -= 1
    elif data == "tp:h+":
        h += 1
    elif data == "tp:m-":
        m -= 5
    elif data == "tp:m+":
        m += 5
    elif data.startswith("tp:q:"):
        m = int(data.split(":")[2])
    elif data == "tp:ok":
        # завершили вибір часу
        ctx.user_data["tp_h"] = h; ctx.user_data["tp_m"] = m
        return await after_time_picked(q, ctx)

    h, m = clamp_time(h, m)
    ctx.user_data["tp_h"] = h; ctx.user_data["tp_m"] = m
    await q.edit_message_reply_markup(reply_markup=build_time_picker(h,m))
    return S_PICK_TIME

async def after_time_picked(q, ctx: CallbackContext):
    chat_id = q.message.chat.id
    st = get_state(chat_id)
    kind = ctx.user_data["tmp_kind"]
    h = ctx.user_data["tp_h"]; m = ctx.user_data["tp_m"]

    if kind == "once":
        d: date = ctx.user_data["picked_date"]
        dt_local = datetime(d.year, d.month, d.day, h, m, tzinfo=st.tz)
        if dt_local <= datetime.now(st.tz):
            await q.message.reply_text("⛔ Дата/час у минулому. Обери знову:", reply_markup=build_calendar(d.year, d.month))
            return S_PICK_DATE
        ctx.user_data["tmp_next"] = dt_local.astimezone(timezone.utc)
        await q.message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING

    if kind == "daily":
        ctx.user_data["tmp_hhmm"] = f"{h:02d}:{m:02d}"
        await q.message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING

    if kind == "n_days":
        ctx.user_data["tmp_hhmm"] = f"{h:02d}:{m:02d}"
        await q.message.reply_text("Обери обмеження:", reply_markup=kb_ending())
        return S_ENDING

# ----- ending -----
async def ending_choice_cb(upd: Update, ctx: CallbackContext):
    q = upd.callback_query
    await q.answer()
    data = q.data
    ctx.user_data["end_choice"] = data
    if data == "end_none":
        ctx.user_data["ending"] = Ending("none")
        return await show_confirm(q, ctx)
    if data == "end_days":
        await q.message.reply_text("Введи D — кількість днів (напр. `7`).")
        return S_ENDING
    if data == "end_times":
        await q.message.reply_text("Введи T — кількість разів (напр. `10`).")
        return S_ENDING

async def ending_value_msg(upd: Update, ctx: CallbackContext):
    choice = ctx.user_data.get("end_choice")
    s = upd.effective_message.text.strip()
    if choice == "end_days":
        try:
            d = int(s); assert d > 0
            ctx.user_data["ending"] = Ending("days", days=d)
            return await show_confirm(upd, ctx)
        except Exception:
            await upd.effective_message.reply_text("Введи додатне ціле число, напр. `7`.")
            return S_ENDING
    if choice == "end_times":
        try:
            t = int(s); assert t > 0
            ctx.user_data["ending"] = Ending("times", times=t)
            return await show_confirm(upd, ctx)
        except Exception:
            await upd.effective_message.reply_text("Введи додатне ціле число, напр. `10`.")
            return S_ENDING
    # дефолт
    ctx.user_data["ending"] = Ending("none")
    return await show_confirm(upd, ctx)

# ----- confirm -----
async def show_confirm(src, ctx: CallbackContext):
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

    if kind == "once":
        next_dt = ctx.user_data["tmp_next"]
        preview = pretty_dt(next_dt, tz)
    elif kind == "daily":
        hhmm = ctx.user_data["tmp_hhmm"]
        h,m = parse_hhmm(hhmm)
        now = datetime.now(tz)
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now: cand += timedelta(days=1)
        next_dt = cand.astimezone(timezone.utc)
        preview = pretty_dt(next_dt, tz) + f" (щодня {hhmm})"
    elif kind == "n_days":
        n = ctx.user_data["tmp_n"]; hhmm = ctx.user_data["tmp_hhmm"]
        h,m = parse_hhmm(hhmm)
        now = datetime.now(tz)
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now: cand += timedelta(days=n)
        next_dt = cand.astimezone(timezone.utc)
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
        f"Перевір:\n• Текст: *{text}*\n• Перше спрацювання: *{preview}*\n• Обмеження: *{end_txt}*",
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

    rem = Rem(id=rid, chat_id=chat_id, text=text, kind=kind, tz=tz, next_dt=next_dt)

    if kind == "daily":
        rem.hhmm = ctx.user_data["tmp_hhmm"]
    if kind == "n_days":
        rem.n = ctx.user_data["tmp_n"]
        rem.hhmm = ctx.user_data["tmp_hhmm"]
    if kind == "x_hours":
        rem.n = ctx.user_data["tmp_n"]
    rem.ending = ending
    if ending.kind == "times":
        rem.left_times = ending.times
    if ending.kind == "days":
        rem.end_date = datetime.now(tz) + timedelta(days=ending.days)

    st.items[rem.id] = rem
    schedule_rem(upd.application, rem)

    await q.edit_message_text(
        f"✅ Збережено! id:`{rem.id}`\nПерше спрацювання: *{pretty_dt(rem.next_dt, tz)}*",
        parse_mode="Markdown",
        reply_markup=kb_item(rem),
    )
    ctx.user_data.clear()
    return ConversationHandler.END

# ---------------------- BUILD APP ----------------------
def build_application() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_cb, pattern="^menu_new$")],
        states={
            S_ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_enter_text)],
            S_PICK_KIND: [CallbackQueryHandler(w_pick_kind, pattern="^(k_once|k_daily|k_ndays|k_xhours)$")],
            S_ENTER_N_ONLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, w_enter_n_only)],
            S_PICK_DATE: [CallbackQueryHandler(calendar_cb, pattern="^(cal[dpn]:|cald:|back_kind)$")],
            S_PICK_TIME: [CallbackQueryHandler(time_cb, pattern="^(tp:|back_kind)$")],
            S_ENDING: [
                CallbackQueryHandler(ending_choice_cb, pattern="^end_(none|days|times)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ending_value_msg),
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

    # Спочатку розмова
    app.add_handler(conv)
    # Потім загальні меню/керування
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^(menu_|back_main|pause:|resume:|del:)"))

    log.info("bot ready")
    return app

# ---------------------- MAIN ----------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No TELEGRAM_TOKEN provided")
    application = build_application()
    application.run_polling(close_loop=False)

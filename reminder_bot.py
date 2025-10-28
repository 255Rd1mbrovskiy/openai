
import json
import os
import uuid
import calendar
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, time, timezone, date
try:
    from zoneinfo import ZoneInfo
except Exception:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)

TOKEN = os.getenv("TELEGRAM_TOKEN")

STORE_FILE = "reminders.json"
DEFAULT_TZ = "Europe/Kyiv"

(
    AWAIT_TEXT,
    AWAIT_TYPE,
    AWAIT_N_DAYS,
    AWAIT_X_HOURS,
    CAL_PICK,
    PICK_HOUR,
    PICK_MINUTE,
) = range(7)

TYPE_ONE = "one"
TYPE_DAILY = "daily"
TYPE_EVERY_N_DAYS = "n_days"
TYPE_EVERY_X_HOURS = "x_hours"


def load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_FILE):
        return {}
    with open(STORE_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def save_store(data: Dict[str, Any]) -> None:
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_bucket(chat_id: int) -> Dict[str, Any]:
    store = load_store()
    bucket = store.get(str(chat_id))
    if not bucket:
        bucket = {"tz": DEFAULT_TZ, "reminders": {}, "completed": []}
        store[str(chat_id)] = bucket
        save_store(store)
    return bucket


def set_user_bucket(chat_id: int, bucket: Dict[str, Any]) -> None:
    store = load_store()
    store[str(chat_id)] = bucket
    save_store(store)


def get_tz(chat_id: int) -> ZoneInfo:
    bucket = get_user_bucket(chat_id)
    tzname = bucket.get("tz", DEFAULT_TZ)
    try:
        return ZoneInfo(tzname)
    except Exception:
        return ZoneInfo("UTC")


def fmt_dt(dt_: datetime) -> str:
    return dt_.strftime("%Y-%m-%d %H:%M")


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("➕ Створити нагадування", callback_data="menu:new")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="menu:list")],
        [InlineKeyboardButton("🌍 Встановити таймзону", callback_data="menu:tz")],
        [InlineKeyboardButton("ℹ️ Довідка", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(kb)


def type_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Одноразове (дата+час)", callback_data=f"type:{TYPE_ONE}")],
        [
            InlineKeyboardButton("Щодня у HH:MM", callback_data=f"type:{TYPE_DAILY}"),
            InlineKeyboardButton("Кожні N днів у HH:MM", callback_data=f"type:{TYPE_EVERY_N_DAYS}"),
        ],
        [InlineKeyboardButton("Кожні X годин", callback_data=f"type:{TYPE_EVERY_X_HOURS}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:menu")],
    ]
    return InlineKeyboardMarkup(kb)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад у меню", callback_data="back:menu")]])


def list_kb(chat_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(chat_id)
    kb: List[List[InlineKeyboardButton]] = []
    reminders = bucket.get("reminders", {})
    if not reminders:
        return back_to_menu_kb()
    for rid, r in reminders.items():
        title = r.get("text", "—")[:28]
        kb.append([
            InlineKeyboardButton(f"❌ Скасувати: {title}", callback_data=f"cancel:{rid}")
        ])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:menu")])
    return InlineKeyboardMarkup(kb)


def completed_kb(chat_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(chat_id)
    compl = bucket.get("completed", [])
    kb: List[List[InlineKeyboardButton]] = []
    if not compl:
        return back_to_menu_kb()
    for item in compl[-20:][::-1]:
        title = item.get("text", "—")[:28]
        kb.append([InlineKeyboardButton(f"🗂 {title}", callback_data="noop")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:menu")])
    return InlineKeyboardMarkup(kb)


def calendar_kb(y: int, m: int) -> InlineKeyboardMarkup:
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(y, m)
    header = InlineKeyboardButton(f"{calendar.month_name[m]} {y}", callback_data="noop")
    prev_m = m - 1 or 12
    prev_y = y - 1 if m == 1 else y
    next_m = (m % 12) + 1
    next_y = y + 1 if m == 12 else y
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("‹", callback_data=f"cal:prev:{prev_y}:{prev_m}"), header,
         InlineKeyboardButton("›", callback_data=f"cal:next:{next_y}:{next_m}")],
        [InlineKeyboardButton(d, callback_data="noop") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]]
    ]
    for w in weeks:
        row = []
        for day in w:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal:pick:{y}:{m}:{day}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:type")])
    return InlineKeyboardMarkup(rows)


def hours_kb() -> InlineKeyboardMarkup:
    hours = [f"{h:02d}" for h in range(0, 24)]
    rows = []
    for i in range(0, 24, 6):
        rows.append([InlineKeyboardButton(h, callback_data=f"hour:{h}") for h in hours[i:i+6]])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:calendar")])
    return InlineKeyboardMarkup(rows)


def minutes_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("00", callback_data="min:00"),
         InlineKeyboardButton("15", callback_data="min:15"),
         InlineKeyboardButton("30", callback_data="min:30"),
         InlineKeyboardButton("45", callback_data="min:45")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back:hour")]
    ]
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_tz(chat_id)
    await update.effective_message.reply_text(
        f"👋 Привіт! Я — бот-нагадувач.\n"
        f"Поточна таймзона: {tz.key}\n\n"
        f"Просто натисни кнопку нижче, щоб створити нагадування.",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Команди:\n"
        "/start — меню\n"
        "/list — список активних нагадувань\n"
        "/completed — виконані\n"
        "/tz <Region/City> — встановити таймзону (напр. /tz Europe/Kyiv)\n"
    )


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        tz = get_tz(chat_id)
        await update.effective_message.reply_text(f"Поточна таймзона: {tz.key}")
        return
    tzname = " ".join(args)
    try:
        _ = ZoneInfo(tzname)
    except Exception:
        await update.effective_message.reply_text("⚠️ Невірна таймзона. Приклад: /tz Europe/Kyiv")
        return
    bucket = get_user_bucket(chat_id)
    bucket["tz"] = tzname
    set_user_bucket(chat_id, bucket)
    await update.effective_message.reply_text(f"✅ Таймзона встановлена: {tzname}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bucket = get_user_bucket(chat_id)
    reminders = bucket.get("reminders", {})
    if not reminders:
        await update.effective_message.reply_text("Активних нагадувань немає.", reply_markup=back_to_menu_kb())
        return
    lines = ["🗒 Активні нагадування:"]
    for r in reminders.values():
        lines.append(f"• {r.get('text')} — {r.get('human')}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=list_kb(chat_id))


async def completed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bucket = get_user_bucket(chat_id)
    compl = bucket.get("completed", [])
    if not compl:
        await update.effective_message.reply_text("Поки немає виконаних.", reply_markup=back_to_menu_kb())
        return
    lines = ["🗂 Останні виконані:"]
    for item in compl[-10:][::-1]:
        lines.append(f"• {item.get('text')} — {item.get('human')}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=completed_kb(chat_id))


async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("menu:"):
        action = data.split(":")[1]
        if action == "new":
            context.user_data["new"] = {"text": None, "type": None, "dt": None, "hour": None, "minute": None,
                                        "n_days": None, "x_hours": None}
            await query.message.reply_text("Введи текст нагадування (що нагадати):")
            return AWAIT_TEXT
        elif action == "list":
            await list_cmd(update, context)
        elif action == "tz":
            await query.message.reply_text("Вкажи таймзону у форматі Region/City, напр.: /tz Europe/Kyiv")
        elif action == "help":
            await help_cmd(update, context)
        return ConversationHandler.END

    if data == "back:menu":
        await query.message.reply_text("Головне меню:", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if data == "back:type":
        await query.message.reply_text("Обери тип:", reply_markup=type_kb())
        return AWAIT_TYPE

    if data == "back:calendar":
        # return to calendar of stored month
        sel = context.user_data.get("cal_month")
        y, m = sel if sel else (date.today().year, date.today().month)
        await query.message.reply_text("Оберіть дату:", reply_markup=calendar_kb(y, m))
        return CAL_PICK

    if data == "back:hour":
        await query.message.reply_text("Оберіть годину:", reply_markup=hours_kb())
        return PICK_HOUR

    if data.startswith("type:"):
        tp = data.split(":")[1]
        context.user_data["new"]["type"] = tp
        if tp == TYPE_ONE:
            today = date.today()
            context.user_data["cal_month"] = (today.year, today.month)
            await query.message.reply_text("Оберіть дату:", reply_markup=calendar_kb(today.year, today.month))
            return CAL_PICK
        elif tp == TYPE_DAILY:
            await query.message.reply_text("Оберіть годину:", reply_markup=hours_kb())
            return PICK_HOUR
        elif tp == TYPE_EVERY_N_DAYS:
            kb = [
                [InlineKeyboardButton(str(x), callback_data=f"ndays:{x}") for x in [1,2,3,5,7]],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back:type")]
            ]
            await query.message.reply_text("Оберіть кожні N днів:", reply_markup=InlineKeyboardMarkup(kb))
            return AWAIT_N_DAYS
        elif tp == TYPE_EVERY_X_HOURS:
            kb = [
                [InlineKeyboardButton(h, callback_data=f"xhrs:{h}") for h in ["1","2","3","4"]],
                [InlineKeyboardButton(h, callback_data=f"xhrs:{h}") for h in ["6","8","12","24"]],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back:type")]
            ]
            await query.message.reply_text("Оберіть інтервал (години):", reply_markup=InlineKeyboardMarkup(kb))
            return AWAIT_X_HOURS

    if data.startswith("cal:"):
        _, act, y, m = data.split(":")[0:4]
        y = int(y); m = int(m)
        if act in ("prev", "next"):
            context.user_data["cal_month"] = (y, m)
            await query.message.reply_text("Оберіть дату:", reply_markup=calendar_kb(y, m))
            return CAL_PICK
        if act == "pick":
            d = int(data.split(":")[4])
            context.user_data["new"]["dt"] = date(y, m, d)
            await query.message.reply_text("Оберіть годину:", reply_markup=hours_kb())
            return PICK_HOUR

    if data.startswith("hour:"):
        hr = int(data.split(":")[1])
        context.user_data["new"]["hour"] = hr
        await query.message.reply_text("Оберіть хвилини:", reply_markup=minutes_kb())
        return PICK_MINUTE

    if data.startswith("min:"):
        minute = int(data.split(":")[1])
        context.user_data["new"]["minute"] = minute
        await finalize_creation(update, context)
        return ConversationHandler.END

    if data.startswith("ndays:"):
        n = int(data.split(":")[1])
        context.user_data["new"]["n_days"] = n
        await query.message.reply_text("Оберіть годину:", reply_markup=hours_kb())
        return PICK_HOUR

    if data.startswith("xhrs:"):
        x = int(data.split(":")[1])
        context.user_data["new"]["x_hours"] = x
        await finalize_creation(update, context)
        return ConversationHandler.END

    if data.startswith("cancel:"):
        rid = data.split(":")[1]
        await cancel_reminder(update, context, rid, user_initiated=True)
        return ConversationHandler.END

    if data.startswith("done:"):
        rid = data.split(":")[1]
        await mark_done(update, context, rid)
        return ConversationHandler.END

    return ConversationHandler.END


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept reminder text
    if "new" not in context.user_data:
        await update.effective_message.reply_text("Скористайся меню /start")
        return ConversationHandler.END
    context.user_data["new"]["text"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("Обери тип нагадування:", reply_markup=type_kb())
    return AWAIT_TYPE


async def finalize_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_tz(chat_id)
    new = context.user_data.get("new", {})
    text = new.get("text")
    tp = new.get("type")
    hour = new.get("hour")
    minute = new.get("minute", 0)

    rid = str(uuid.uuid4())

    if tp == TYPE_ONE:
        d: date = new.get("dt")
        dt_local = datetime(d.year, d.month, d.day, hour or 0, minute or 0, tzinfo=tz)
        when_utc = dt_local.astimezone(timezone.utc)
        human = f"одноразово — {fmt_dt(dt_local)}"
        schedule_once(context, chat_id, rid, text, human, when_utc)
    elif tp == TYPE_DAILY:
        t_local = time(hour or 0, minute or 0, tzinfo=tz)
        human = f"щодня — {t_local.strftime('%H:%M')}"
        schedule_daily(context, chat_id, rid, text, human, t_local)
    elif tp == TYPE_EVERY_N_DAYS:
        n = int(new.get("n_days") or 1)
        t_local = time(hour or 0, minute or 0, tzinfo=tz)
        human = f"кожні {n} дн. — {t_local.strftime('%H:%M')}"
        schedule_every_n_days(context, chat_id, rid, text, human, t_local, n, tz)
    elif tp == TYPE_EVERY_X_HOURS:
        x = int(new.get("x_hours") or 1)
        human = f"кожні {x} год."
        schedule_every_x_hours(context, chat_id, rid, text, human, x)

    # store
    bucket = get_user_bucket(chat_id)
    bucket["reminders"][rid] = {
        "id": rid,
        "text": text,
        "type": tp,
        "hour": hour,
        "minute": minute,
        "tz": tz.key,
        "human": human,
        "dt": new.get("dt").isoformat() if new.get("dt") else None,
        "n_days": new.get("n_days"),
        "x_hours": new.get("x_hours"),
    }
    set_user_bucket(chat_id, bucket)
    context.user_data.pop("new", None)

    await update.effective_message.reply_text(f"✅ Створено нагадування: “{text}”\n{human}",
                                              reply_markup=main_menu_kb())


def reminder_kb(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Виконано", callback_data=f"done:{rid}"),
        InlineKeyboardButton("✖️ Скасувати", callback_data=f"cancel:{rid}")
    ]])


async def fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    payload = job.data
    chat_id = payload["chat_id"]
    rid = payload["rid"]
    text = payload["text"]
    human = payload["human"]
    await context.bot.send_message(chat_id, f"⏰ *Нагадування:* {text}\n_{human}_",
                                   parse_mode="Markdown",
                                   reply_markup=reminder_kb(rid))


async def cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str, user_initiated: bool):
    chat_id = update.effective_chat.id
    # cancel job
    for j in context.job_queue.get_jobs_by_name(rid):
        j.schedule_removal()
    # drop from store
    bucket = get_user_bucket(chat_id)
    rem = bucket["reminders"].pop(rid, None)
    set_user_bucket(chat_id, bucket)
    if user_initiated:
        await update.callback_query.edit_message_text("❌ Нагадування скасовано.")
    else:
        await context.bot.send_message(chat_id, "❌ Нагадування скасовано.")


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, rid: str):
    chat_id = update.effective_chat.id
    # cancel job(s)
    for j in context.job_queue.get_jobs_by_name(rid):
        j.schedule_removal()
    # move to completed
    bucket = get_user_bucket(chat_id)
    rem = bucket["reminders"].pop(rid, None)
    if rem:
        bucket["completed"].append(rem)
    set_user_bucket(chat_id, bucket)
    await update.callback_query.edit_message_text("✅ Відмічено як виконано. Перенесено в ‘виконані’.")


def schedule_once(context: ContextTypes.DEFAULT_TYPE, chat_id: int, rid: str, text: str, human: str, when_utc):
    context.job_queue.run_once(
        fire_reminder,
        when=when_utc,
        name=rid,
        data={"chat_id": chat_id, "rid": rid, "text": text, "human": human},
    )


def schedule_daily(context: ContextTypes.DEFAULT_TYPE, chat_id: int, rid: str, text: str, human: str, t_local):
    context.job_queue.run_daily(
        fire_reminder,
        time=t_local,
        days=(0,1,2,3,4,5,6),
        name=rid,
        data={"chat_id": chat_id, "rid": rid, "text": text, "human": human},
    )


def schedule_every_n_days(context: ContextTypes.DEFAULT_TYPE, chat_id: int, rid: str, text: str, human: str,
                          t_local, n_days: int, tz: ZoneInfo):
    now = datetime.now(tz)
    first = datetime.combine(now.date(), time(t_local.hour, t_local.minute, tzinfo=tz))
    if first <= now:
        first += timedelta(days=1)
    # Align the first run to the next slot that matches the N-day cadence starting tomorrow
    context.job_queue.run_repeating(
        fire_reminder,
        interval=timedelta(days=n_days),
        first=first.astimezone(timezone.utc),
        name=rid,
        data={"chat_id": chat_id, "rid": rid, "text": text, "human": human},
    )


def schedule_every_x_hours(context: ContextTypes.DEFAULT_TYPE, chat_id: int, rid: str, text: str, human: str, x_hours: int):
    context.job_queue.run_repeating(
        fire_reminder,
        interval=timedelta(hours=x_hours),
        first=timedelta(seconds=5),
        name=rid,
        data={"chat_id": chat_id, "rid": rid, "text": text, "human": human},
    )


async def restore_jobs(app):
    store = load_store()
    for chat_id_str, bucket in store.items():
        chat_id = int(chat_id_str)
        tzname = bucket.get("tz", DEFAULT_TZ)
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz = ZoneInfo("UTC")
        for rid, r in bucket.get("reminders", {}).items():
            text = r.get("text")
            tp = r.get("type")
            human = r.get("human", "")
            hour = r.get("hour")
            minute = r.get("minute")
            if tp == TYPE_ONE and r.get("dt"):
                d = date.fromisoformat(r["dt"])
                dt_local = datetime(d.year, d.month, d.day, hour or 0, minute or 0, tzinfo=tz)
                if dt_local > datetime.now(tz):
                    when_utc = dt_local.astimezone(timezone.utc)
                    schedule_once(app, chat_id, rid, text, human, when_utc)
            elif tp == TYPE_DAILY:
                t_local = time(hour or 0, minute or 0, tzinfo=tz)
                schedule_daily(app, chat_id, rid, text, human, t_local)
            elif tp == TYPE_EVERY_N_DAYS:
                n = int(r.get("n_days") or 1)
                t_local = time(hour or 0, minute or 0, tzinfo=tz)
                schedule_every_n_days(app, chat_id, rid, text, human, t_local, n, tz)
            elif tp == TYPE_EVERY_X_HOURS:
                x = int(r.get("x_hours") or 1)
                schedule_every_x_hours(app, chat_id, rid, text, human, x)


async def post_init(app):
    await restore_jobs(app.job_queue)


def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(restore_jobs).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_router)],
        states={
            AWAIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)],
            AWAIT_TYPE: [CallbackQueryHandler(cb_router)],
            AWAIT_N_DAYS: [CallbackQueryHandler(cb_router)],
            AWAIT_X_HOURS: [CallbackQueryHandler(cb_router)],
            CAL_PICK: [CallbackQueryHandler(cb_router)],
            PICK_HOUR: [CallbackQueryHandler(cb_router)],
            PICK_MINUTE: [CallbackQueryHandler(cb_router)],
        },
        fallbacks=[CallbackQueryHandler(cb_router)],
        allow_reentry=True,
        name="reminder_flow",
        persistent=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("completed", completed_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_router))

    return app


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TELEGRAM_TOKEN env var is not set")
    app = build_app()
    app.run_polling(close_loop=False)

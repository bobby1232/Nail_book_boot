from __future__ import annotations

from datetime import datetime, timedelta, time, date
from typing import List, Optional

import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

# ✅ ЕДИНЫЙ стиль импортов (пакет app). Убрал дубли и конфликты.
from app.db import DB
from app.config import Defaults
from app import texts, keyboards

# states
SVC, DAY, TIME, COMMENT, PHONE, FINAL = range(6)

RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _service_label(service: dict) -> str:
    name = str(service.get("name") or "").strip()
    return name or "Услуга"


def _slots_rows(slots: List[datetime]) -> List[List[InlineKeyboardButton]]:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for st in slots:
        row.append(InlineKeyboardButton(st.strftime("%H:%M"), callback_data=f"time:{st.isoformat()}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _tz(context: ContextTypes.DEFAULT_TYPE) -> pytz.BaseTzInfo:
    # ✅ Не падаем KeyError, если tz не задан
    tz_name = context.bot_data.get("tz", "Europe/Moscow")
    return pytz.timezone(tz_name)


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # ✅ Всегда bool, без None
    u = update.effective_user
    admin_id = context.bot_data.get("admin_id")
    return bool(u and admin_id and u.id == admin_id)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: DB = context.bot_data["db"]
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    await db.upsert_user(user.id, user.username, user.full_name)

    services = await db.get_services()
    if not services:
        # ✅ effective_message безопаснее, чем update.message
        await update.effective_message.reply_text("Услуги не настроены. Обратитесь к мастеру.")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(
            f"{_service_label(s)} — {s['duration_min']} мин — {s['price']} ₽",
            callback_data=f"svc:{s['id']}"
        )]
        for s in services
    ]
    kb = InlineKeyboardMarkup(buttons + [[InlineKeyboardButton("↩️ В меню", callback_data="svc:cancel")]])
    await update.effective_message.reply_text("Выберите услугу:", reply_markup=kb)
    return SVC


async def pick_service_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    await query.answer()
    data = query.data or ""

    if data == "svc:cancel":
        await query.edit_message_text(texts.MAIN_MENU)
        await query.message.reply_text(
            texts.ABOUT,
            reply_markup=keyboards.main_menu(_is_admin(update, context))
        )
        return ConversationHandler.END

    # ✅ защита от мусорных callback
    if ":" not in data:
        await query.edit_message_text("Ошибка выбора услуги. Попробуйте ещё раз.")
        return ConversationHandler.END

    _, sid = data.split(":", 1)
    try:
        context.user_data["service_id"] = int(sid)
    except ValueError:
        await query.edit_message_text("Ошибка выбора услуги. Попробуйте ещё раз.")
        return ConversationHandler.END

    tz = _tz(context)
    today = datetime.now(tz).date()
    horizon = int(getattr(Defaults, "BOOKING_HORIZON_DAYS", 14))

    rows = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(f"{RU_WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}", callback_data=f"day:{d.isoformat()}")])

    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="day:back")])
    await query.edit_message_text("Выберите дату:", reply_markup=InlineKeyboardMarkup(rows))
    return DAY


async def _compute_free_slots(db: DB, tz, chosen_date: date, service_id: int) -> List[datetime]:
    service = await db.get_service(service_id)
    if not service:
        return []

    slot_step = await db.get_setting_int("slot_step_min", Defaults.SLOT_STEP_MIN)
    min_lead = await db.get_setting_int("min_lead_time_min", Defaults.MIN_LEAD_TIME_MIN)
    work_start = await db.get_setting_str("work_start", Defaults.WORK_START)
    work_end = await db.get_setting_str("work_end", Defaults.WORK_END)
    work_days = await db.get_setting_str("work_days", ",".join(map(str, Defaults.WORK_DAYS)))
    work_days_set = {int(x) for x in work_days.split(",") if x.strip() != ""}

    if chosen_date.weekday() not in work_days_set:
        return []

    start_t = _parse_hhmm(work_start)
    end_t = _parse_hhmm(work_end)

    day_start = tz.localize(datetime.combine(chosen_date, start_t))
    day_end = tz.localize(datetime.combine(chosen_date, end_t))

    now = datetime.now(tz)
    earliest = now + timedelta(minutes=min_lead)

    duration = int(service["duration_min"])
    buffer_min = int(service.get("buffer_min", 0))
    total = duration + buffer_min

    blocked = await db.list_blocked(day_start, day_end)
    busy = await db.list_active_appointments(day_start, day_end)

    def overlaps(st: datetime, en: datetime) -> bool:
        for r in blocked:
            if st < r["end_dt"] and en > r["start_dt"]:
                return True
        for r in busy:
            if st < r["end_dt"] and en > r["start_dt"]:
                return True
        return False

    slots: List[datetime] = []
    cursor = day_start
    step = timedelta(minutes=slot_step)

    while cursor + timedelta(minutes=duration) <= day_end:
        st = cursor
        en = cursor + timedelta(minutes=total)
        if st >= earliest and not overlaps(st, en):
            slots.append(st)
        cursor += step

    return slots


async def pick_day_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    await query.answer()
    data = query.data or ""

    if data == "day:back":
        db: DB = context.bot_data["db"]
        services = await db.get_services()
        buttons = [
            [InlineKeyboardButton(
                f"{_service_label(s)} — {s['duration_min']} мин — {s['price']} ₽",
                callback_data=f"svc:{s['id']}"
            )]
            for s in services
        ]
        buttons.append([InlineKeyboardButton("↩️ В меню", callback_data="svc:cancel")])
        await query.edit_message_text("Выберите услугу:", reply_markup=InlineKeyboardMarkup(buttons))
        return SVC

    if ":" not in data:
        await query.edit_message_text("Ошибка выбора даты. Попробуйте ещё раз.")
        return DAY

    _, iso = data.split(":", 1)
    try:
        chosen_date = date.fromisoformat(iso)
    except ValueError:
        await query.edit_message_text("Ошибка выбора даты. Попробуйте ещё раз.")
        return DAY

    context.user_data["date"] = chosen_date.isoformat()

    db: DB = context.bot_data["db"]
    tz = _tz(context)
    service_id = context.user_data.get("service_id")
    if service_id is None:
        await query.edit_message_text("Сессия устарела. Начните запись заново.")
        return ConversationHandler.END

    slots = await _compute_free_slots(db, tz, chosen_date, int(service_id))
    if not slots:
        await query.edit_message_text("На эту дату свободных слотов нет. Выберите другую дату.")
        return DAY

    rows = _slots_rows(slots[:40])
    rows.append([InlineKeyboardButton("↩️ Назад к дате", callback_data="time:back")])

    await query.edit_message_text("Выберите время:", reply_markup=InlineKeyboardMarkup(rows))
    return TIME


async def pick_time_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    await query.answer()
    data = query.data or ""

    # ✅ Реально работающая кнопка "Назад к дате"
    if data == "time:back":
        date_iso = context.user_data.get("date")
        if not date_iso:
            await query.edit_message_text("Сессия устарела. Начните запись заново.")
            return ConversationHandler.END

        chosen_date = date.fromisoformat(date_iso)

        db: DB = context.bot_data["db"]
        tz = _tz(context)
        service_id = context.user_data.get("service_id")
        if service_id is None:
            await query.edit_message_text("Сессия устарела. Начните запись заново.")
            return ConversationHandler.END

        slots = await _compute_free_slots(db, tz, chosen_date, int(service_id))
        if not slots:
            await query.edit_message_text("На эту дату свободных слотов нет. Выберите другую дату.")
            return DAY

        rows = _slots_rows(slots[:40])
        rows.append([InlineKeyboardButton("↩️ Назад к дате", callback_data="time:back")])

        await query.edit_message_text("Выберите время:", reply_markup=InlineKeyboardMarkup(rows))
        return TIME

    # ✅ выбор конкретного времени: ожидаем time:<iso>
    if not data.startswith("time:"):
        await query.edit_message_text("Ошибка выбора времени. Попробуйте ещё раз.")
        return TIME

    _, iso = data.split(":", 1)
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        await query.edit_message_text("Ошибка выбора времени. Попробуйте ещё раз.")
        return TIME

    # dt может быть naive/aware — нормализуем в tz
    tz = _tz(context)
    if dt.tzinfo is None:
        dt = tz.localize(dt)
    else:
        dt = dt.astimezone(tz)

    context.user_data["start_dt"] = dt.isoformat()

    # ✅ Дальше по воронке: комментарий/телефон и т.д.
    # Здесь ты продолжишь свои состояния COMMENT/PHONE/FINAL.
    await query.edit_message_text("Напишите комментарий (необязательно) или отправьте '-' чтобы пропустить:")
    return COMMENT

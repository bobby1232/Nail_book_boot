from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal

from telegram.ext import ContextTypes
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.models import Appointment, AppointmentStatus, User, Service
from app.logic import get_settings
from app.keyboards import reminder_kb, admin_visit_confirm_kb
from app.utils import format_price, appointment_services_label
from texts import get_aftercare_recommendations_parts, get_reminder_template



WEEKDAY_RU_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

def weekday_ru_full(dt: datetime) -> str:
    return WEEKDAY_RU_FULL[dt.weekday()]

REMINDER_48H_TEMPLATE = get_reminder_template(48)
REMINDER_2H_TEMPLATE = get_reminder_template(2)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_date(dt: datetime, tz_name: str) -> tuple[str, str]:
    # dt в БД timezone-aware; переводим в tz бота (чтобы клиент видел локальное время)
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        local = dt.astimezone(tz)
    except Exception:
        local = dt
    return f"{weekday_ru_full(local)}, {local.strftime('%d.%m.%Y')}", local.strftime('%H:%M')


def _localize(dt: datetime, tz) -> datetime:
    if hasattr(tz, "localize"):
        return tz.localize(dt)
    return dt.replace(tzinfo=tz)


def _format_hours(total_hours: float) -> str:
    formatted = f"{total_hours:.2f}".rstrip("0").rstrip(".")
    return formatted or "0"

def _admin_ids(cfg) -> tuple[int, ...]:
    if cfg is None:
        return tuple()
    ids = getattr(cfg, "admin_telegram_ids", None)
    if ids:
        return tuple(ids)
    admin_id = getattr(cfg, "admin_telegram_id", None)
    if admin_id:
        return (int(admin_id),)
    return tuple()


async def _send_earnings_report(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    start_utc: datetime,
    end_utc: datetime,
    title: str,
    label: str,
) -> None:
    app = context.application
    session_factory = app.bot_data.get("session_factory")
    cfg = app.bot_data.get("cfg")
    if session_factory is None or cfg is None:
        return

    admin_ids = getattr(cfg, "admin_telegram_ids", None)
    if not admin_ids:
        return

    async with session_factory() as session:
        q = (
            select(Appointment)
            .options(selectinload(Appointment.service))
            .where(Appointment.visit_confirmed.is_(True))
            .where(Appointment.start_dt >= start_utc)
            .where(Appointment.start_dt < end_utc)
            .order_by(Appointment.start_dt.asc())
        )
        res = await session.execute(q)
        appts = list(res.scalars().all())

    if not appts:
        text = f"{title}\nПодтверждённых записей нет."
    else:
        total_earnings = Decimal("0")
        total_seconds = 0.0
        for appt in appts:
            price = appt.price_override if appt.price_override is not None else appt.service.price
            total_earnings += Decimal(str(price))
            total_seconds += (appt.end_dt - appt.start_dt).total_seconds()
        total_hours = total_seconds / 3600.0
        text = (
            f"{title}\n"
            f"Подтверждённых записей: {len(appts)}\n"
            f"Заработок {label}: {format_price(total_earnings)}\n"
            f"Общее время работы (по записям): {_format_hours(total_hours)} ч."
        )

    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            continue


async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Запускается JobQueue раз в минуту.
    Шлём:
      - за 48 часов (флаг reminder_24h_sent используем как "первое напоминание")
      - за 2 часа   (флаг reminder_2h_sent используем как "второе напоминание")
    Только для AppointmentStatus.Booked.
    """
    app = context.application
    session_factory = app.bot_data.get("session_factory")
    if session_factory is None:
        # если у тебя session_factory хранится иначе — скажи, поменяю
        return
    cfg = app.bot_data.get("cfg")

    tz_name = app.bot_data.get("tz", "Europe/Moscow")
    now = _utcnow()

    # Окна под отправку (чтобы не ловить погрешности по минутам)
    # 48 часов: попадаем в окно [48h, 48h+2min)
    # 2 часа:   попадаем в окно [2h, 2h+2min)
    win = timedelta(minutes=2)

    target_48_from = now + timedelta(hours=48)
    target_48_to = target_48_from + win

    target_2_from = now + timedelta(hours=2)
    target_2_to = target_2_from + win

    async with session_factory() as session:
        settings = await get_settings(session, tz_name)
        # --- 48h reminders ---
        q48 = (
            select(Appointment)
            .options(selectinload(Appointment.client), selectinload(Appointment.service))
            .where(Appointment.status == AppointmentStatus.Booked)
            .where(Appointment.reminder_24h_sent.is_(False))   # используем как "48h не отправляли"
            .where(Appointment.start_dt >= target_48_from)
            .where(Appointment.start_dt < target_48_to)
        )
        res48 = await session.execute(q48)
        appts48 = list(res48.scalars().all())

        for appt in appts48:
            if not appt.client or not appt.client.tg_id:
                continue

            d, t = _fmt_date(appt.start_dt, tz_name)
            allow_reschedule = now <= (appt.start_dt - timedelta(hours=settings.cancel_limit_hours))
            text = REMINDER_48H_TEMPLATE.format(
                service=appointment_services_label(appt),
                date=d,
                time=t,
            )

            try:
                await context.bot.send_message(
                    chat_id=appt.client.tg_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=reminder_kb(appt.id, allow_reschedule=allow_reschedule),
                )
                # помечаем как отправленное
                await session.execute(
                    update(Appointment)
                    .where(Appointment.id == appt.id)
                    .values(reminder_24h_sent=True, updated_at=_utcnow())
                )
            except Exception:
                # не валим весь джоб из-за 1 ошибки
                continue

        # --- 2h reminders ---
        q2 = (
            select(Appointment)
            .options(selectinload(Appointment.client), selectinload(Appointment.service))
            .where(Appointment.status == AppointmentStatus.Booked)
            .where(Appointment.reminder_2h_sent.is_(False))   # используем как "2h не отправляли"
            .where(Appointment.start_dt >= target_2_from)
            .where(Appointment.start_dt < target_2_to)
        )
        res2 = await session.execute(q2)
        appts2 = list(res2.scalars().all())

        for appt in appts2:
            if not appt.client or not appt.client.tg_id:
                continue

            d, t = _fmt_date(appt.start_dt, tz_name)
            allow_reschedule = now <= (appt.start_dt - timedelta(hours=settings.cancel_limit_hours))
            text = REMINDER_2H_TEMPLATE.format(
                service=appointment_services_label(appt),
                time=t,
            )

            try:
                await context.bot.send_message(
                    chat_id=appt.client.tg_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=reminder_kb(appt.id, allow_reschedule=allow_reschedule),
                )
                await session.execute(
                    update(Appointment)
                    .where(Appointment.id == appt.id)
                    .values(reminder_2h_sent=True, updated_at=_utcnow())
                )
            except Exception:
                continue

        await session.commit()

    # После commit можно отправить пост-уходовые рекомендации
    async with session_factory() as session:
        q_aftercare = (
            select(Appointment)
            .options(selectinload(Appointment.client), selectinload(Appointment.service))
            .where(Appointment.status == AppointmentStatus.Booked)
            .where(Appointment.end_dt <= now)
        )
        res_aftercare = await session.execute(q_aftercare)
        appts_aftercare = list(res_aftercare.scalars().all())

        for appt in appts_aftercare:
            admin_ids = _admin_ids(cfg)
            if admin_ids:
                date_label, time_label = _fmt_date(appt.start_dt, tz_name)
                price_label = format_price(
                    appt.price_override if appt.price_override is not None else appt.service.price
                )
                client_label = appt.client.full_name or (
                    f"@{appt.client.username}" if appt.client.username else str(appt.client.tg_id)
                )
                service_label = appointment_services_label(appt)
                text = (
                    "✅ Визит завершён.\n"
                    "Подтверди финальную стоимость для учёта:\n"
                    f"{date_label} {time_label}\n"
                    f"Услуга: {service_label}\n"
                    f"Клиент: {client_label}\n"
                    f"Цена: {price_label}"
                )
                for admin_id in admin_ids:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=text,
                            reply_markup=admin_visit_confirm_kb(appt.id),
                        )
                    except Exception:
                        continue

            if appt.client and appt.client.tg_id:
                try:
                    for part in get_aftercare_recommendations_parts(appt.service.category, cfg.name):
                        await context.bot.send_message(
                            chat_id=appt.client.tg_id,
                            text=part,
                        )
                except Exception:
                    pass

            await session.execute(
                update(Appointment)
                .where(Appointment.id == appt.id)
                .values(status=AppointmentStatus.Completed, updated_at=_utcnow())
            )

        await session.commit()


async def send_daily_admin_schedule(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневная сводка записей на сегодня для мастера (админа).
    """
    app = context.application
    session_factory = app.bot_data.get("session_factory")
    cfg = app.bot_data.get("cfg")
    if session_factory is None or cfg is None:
        return

    admin_ids = getattr(cfg, "admin_telegram_ids", None)
    if not admin_ids:
        return

    tz_name = app.bot_data.get("tz", "Europe/Moscow")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz=tz)
    day = now_local.date()
    start_local = datetime.combine(day, dt_time.min)
    if hasattr(tz, "localize"):
        start_local = tz.localize(start_local)
    else:
        start_local = start_local.replace(tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    async with session_factory() as session:
        q = (
            select(Appointment)
            .options(selectinload(Appointment.client), selectinload(Appointment.service))
            .where(Appointment.status == AppointmentStatus.Booked)
            .where(Appointment.start_dt >= start_utc)
            .where(Appointment.start_dt < end_utc)
            .order_by(Appointment.start_dt.asc())
        )
        res = await session.execute(q)
        appts = list(res.scalars().all())

    if not appts:
        text = "На сегодня записей нет."
    else:
        day_label = f"{day.strftime('%d.%m.%Y')} ({weekday_ru_full(now_local)})"
        lines = [f"📅 Записи на сегодня: {day_label}"]
        for appt in appts:
            start_t = appt.start_dt.astimezone(tz).strftime("%H:%M")
            end_t = appt.end_dt.astimezone(tz).strftime("%H:%M")
            client = appt.client.full_name or (
                f"@{appt.client.username}" if appt.client.username else str(appt.client.tg_id)
            )
            phone = appt.client.phone or "—"
            price = format_price(
                appt.price_override if appt.price_override is not None else appt.service.price
            )
            service_label = appointment_services_label(appt)
            lines.append(
                f"• {start_t}–{end_t} | {service_label} | {price} | {client} | {phone}"
            )
        text = "\n".join(lines)

    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            continue


async def send_daily_admin_earnings_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневный отчёт мастеру о заработке за сегодня (подтверждённые записи).
    """
    app = context.application
    tz_name = app.bot_data.get("tz", "Europe/Moscow")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz=tz)
    day = now_local.date()
    start_local = _localize(datetime.combine(day, dt_time.min), tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    day_label = day.strftime('%d.%m.%Y')
    title = f"💰 Отчёт за сегодня ({day_label})"
    await _send_earnings_report(
        context,
        start_utc=start_utc,
        end_utc=end_utc,
        title=title,
        label="за сегодня",
    )


async def send_weekly_admin_earnings_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Еженедельный отчёт мастеру о заработке (подтверждённые записи).
    Отправляется в конце недели (воскресенье).
    """
    app = context.application
    tz_name = app.bot_data.get("tz", "Europe/Moscow")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz=tz)
    if now_local.weekday() != 6:
        return

    day = now_local.date()
    week_start = day - timedelta(days=day.weekday())
    start_local = _localize(datetime.combine(week_start, dt_time.min), tz)
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    week_label = f"{week_start.strftime('%d.%m.%Y')}–{day.strftime('%d.%m.%Y')}"
    title = f"💰 Отчёт за неделю ({week_label})"
    await _send_earnings_report(
        context,
        start_utc=start_utc,
        end_utc=end_utc,
        title=title,
        label="за неделю",
    )


async def send_monthly_admin_earnings_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежемесячный отчёт мастеру о заработке (подтверждённые записи).
    Отправляется в последний день месяца.
    """
    app = context.application
    tz_name = app.bot_data.get("tz", "Europe/Moscow")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = timezone.utc

    now_local = datetime.now(tz=tz)
    day = now_local.date()
    next_day = day + timedelta(days=1)
    if next_day.month == day.month:
        return

    month_start = day.replace(day=1)
    start_local = _localize(datetime.combine(month_start, dt_time.min), tz)
    if day.month == 12:
        next_month = datetime(day.year + 1, 1, 1).date()
    else:
        next_month = datetime(day.year, day.month + 1, 1).date()
    end_local = _localize(datetime.combine(next_month, dt_time.min), tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    month_label = month_start.strftime('%m.%Y')
    title = f"💰 Отчёт за месяц ({month_label})"
    await _send_earnings_report(
        context,
        start_utc=start_utc,
        end_utc=end_utc,
        title=title,
        label="за месяц",
    )

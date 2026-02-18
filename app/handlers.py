from __future__ import annotations
from datetime import datetime, date, timedelta, time
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote
import asyncio
import logging
import os
import pytz

from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import ContextTypes

from app.config import Config
from app.logic import (
    get_settings, upsert_user, set_user_phone, list_active_services_filtered, list_active_services_by_category_filtered, list_available_dates,
    list_available_slots_for_service, list_available_slots_for_duration,
    create_hold_appointment, create_hold_appointment_with_duration, get_user_appointments,
    get_user_appointments_history, get_appointment, admin_confirm, admin_reject,
    cancel_by_client, request_reschedule, confirm_reschedule, reject_reschedule,
    admin_list_appointments_for_day, admin_list_holds, create_admin_appointment,
    create_admin_appointment_with_duration, check_slot_available,
    check_slot_available_for_duration, compute_slot_end, compute_slot_end_for_duration,
    admin_cancel_appointment, list_available_break_slots, create_blocked_interval,
    admin_reschedule_appointment, admin_list_appointments_range,
    list_future_breaks, delete_blocked_interval, SettingsView,
    create_break_rule, generate_breaks_from_rules
)
from app.keyboards import (
    main_menu_kb, phone_request_kb, booking_categories_kb, services_multi_kb, dates_kb, slots_kb, confirm_request_kb,
    admin_request_kb, my_appts_kb, my_appt_actions_kb, admin_menu_kb,
    reschedule_dates_kb, reschedule_slots_kb, reschedule_confirm_kb, admin_reschedule_kb,
    admin_services_kb, admin_dates_kb, admin_slots_kb, admin_manage_appt_kb,
    admin_reschedule_dates_kb, admin_reschedule_slots_kb, admin_reschedule_confirm_kb,
    break_dates_kb, break_slots_kb, break_repeat_kb, status_ru, RU_WEEKDAYS, cancel_breaks_kb,
    contacts_kb, admin_visit_confirm_kb, category_selector_kb,
)
from app.models import AppointmentStatus, BlockedInterval, Service
from app.schedule_style import DAY_TIMELINE_STYLE, WEEK_SCHEDULE_STYLE
from app.utils import (
    format_price,
    appointment_services_label,
    service_category_title,
    service_label_with_category,
    services_label_with_category,
)
from texts import (
    CONTACTS,
    ADDRESS_LINE,
    PRECARE_RECOMMENDATIONS,
    AFTERCARE_RECOMMENDATIONS,
    get_precare_recommendations_parts,
)

logger = logging.getLogger(__name__)

K_SVC = "svc_id"
K_SVCS = "svc_ids"
K_DATE = "date"
K_SLOT = "slot_iso"
K_COMMENT = "comment"
K_PHONE = "phone"
K_RESCHED_APPT = "resched_appt_id"
K_RESCHED_SVC = "resched_svc_id"
K_RESCHED_DATE = "resched_date"
K_RESCHED_SLOT = "resched_slot_iso"
K_ADMIN_SVC = "admin_svc_id"
K_ADMIN_DATE = "admin_date"
K_ADMIN_TIME = "admin_time_iso"
K_ADMIN_DURATION = "admin_duration_min"
K_ADMIN_CLIENT_NAME = "admin_client_name"
K_ADMIN_CLIENT_PHONE = "admin_client_phone"
K_ADMIN_CLIENT_TGID = "admin_client_tg_id"
K_ADMIN_PRICE = "admin_price_override"
K_ADMIN_CONFIRM_APPT = "admin_confirm_appt_id"
K_ADMIN_VISIT_APPT = "admin_visit_appt_id"
K_ADMIN_TIME_ERRORS = "admin_time_errors"
K_ADMIN_RESCHED_APPT = "admin_resched_appt_id"
K_ADMIN_RESCHED_SVC = "admin_resched_svc_id"
K_ADMIN_RESCHED_DATE = "admin_resched_date"
K_ADMIN_RESCHED_SLOT = "admin_resched_slot_iso"
K_BREAK_DATE = "break_date"
K_BREAK_DURATION = "break_duration_min"
K_BREAK_TIME_ERRORS = "break_time_errors"
K_BREAK_REASON = "break_reason"
K_BREAK_REPEAT = "break_repeat"
K_BREAK_CANCEL_IDS = "break_cancel_ids"
K_BOOKING_CATEGORY = "booking_category"

def _hidden_service_categories(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, ...]:
    cfg: Config = context.bot_data.get("cfg")
    if not cfg:
        return tuple()
    return tuple(getattr(cfg, "hidden_service_categories", tuple()) or tuple())


def _available_service_categories(services: list[Service]) -> list[str]:
    categories: list[str] = []
    for service in services:
        category = (service.category or "").strip()
        if not category or category in categories:
            continue
        categories.append(category)
    return categories


def _selected_service_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    raw = context.user_data.get(K_SVCS) or []
    return [int(x) for x in raw if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]

def _collect_selected_services(services: list, selected_ids: list[int]) -> list:
    if not selected_ids:
        return []
    selected_set = set(selected_ids)
    return [s for s in services if s.id in selected_set]

def _selected_break_cancel_ids(context: ContextTypes.DEFAULT_TYPE) -> list[int]:
    raw = context.user_data.get(K_BREAK_CANCEL_IDS) or []
    return [int(x) for x in raw if isinstance(x, int) or (isinstance(x, str) and x.isdigit())]

async def _load_break_cancel_items(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[SettingsView, list[tuple[int, datetime, datetime]]]:
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        now_local = datetime.now(tz=settings.tz)
        end_local = now_local + timedelta(days=30)
        blocks = await list_future_breaks(
            s,
            now_local.astimezone(pytz.UTC),
            end_local.astimezone(pytz.UTC),
        )
    items = [
        (b.id, b.start_dt.astimezone(settings.tz), b.end_dt.astimezone(settings.tz))
        for b in blocks
    ]
    return settings, items

def _slot_duration_for_services(services: list, base_service) -> int:
    duration_sum = sum(int(s.duration_min) for s in services)
    buffer_sum = sum(int(s.buffer_min) for s in services)
    return duration_sum + buffer_sum - int(base_service.buffer_min)

def _display_duration_for_services(services: list) -> int:
    duration_sum = sum(int(s.duration_min) for s in services)
    buffer_sum = sum(int(s.buffer_min) for s in services)
    return duration_sum + buffer_sum

def admin_ids(cfg: Config) -> tuple[int, ...]:
    ids = getattr(cfg, "admin_telegram_ids", None)
    if ids:
        return tuple(ids)
    admin_id = getattr(cfg, "admin_telegram_id", None)
    if admin_id:
        return (int(admin_id),)
    return tuple()

def is_admin(cfg: Config, user_id: int) -> bool:
    return user_id in admin_ids(cfg)

async def notify_admins(
    context: ContextTypes.DEFAULT_TYPE,
    cfg: Config,
    text: str,
    reply_markup=None,
) -> None:
    for admin_id in admin_ids(cfg):
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)

def main_menu_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config | None = context.bot_data.get("cfg")
    if cfg and update.effective_user:
        return main_menu_kb(is_admin(cfg, update.effective_user.id))
    return main_menu_kb()

def _clear_admin_booking(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        K_ADMIN_SVC,
        K_ADMIN_DATE,
        K_ADMIN_TIME,
        K_ADMIN_DURATION,
        K_ADMIN_CLIENT_NAME,
        K_ADMIN_CLIENT_PHONE,
        K_ADMIN_CLIENT_TGID,
        K_ADMIN_PRICE,
        K_ADMIN_TIME_ERRORS,
    ):
        context.user_data.pop(key, None)
    for flag in (
        "awaiting_admin_time",
        "awaiting_admin_duration",
        "awaiting_admin_client_name",
        "awaiting_admin_client_phone",
        "awaiting_admin_client_tg",
        "awaiting_admin_price",
    ):
        context.user_data.pop(flag, None)

def _clear_admin_reschedule(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        K_ADMIN_RESCHED_APPT,
        K_ADMIN_RESCHED_SVC,
        K_ADMIN_RESCHED_DATE,
        K_ADMIN_RESCHED_SLOT,
    ):
        context.user_data.pop(key, None)

def _clear_admin_confirm(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(K_ADMIN_CONFIRM_APPT, None)
    context.user_data.pop("awaiting_admin_confirm_price", None)

def _clear_admin_visit(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(K_ADMIN_VISIT_APPT, None)
    context.user_data.pop("awaiting_admin_visit_price", None)

def _clear_break(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (K_BREAK_DATE, K_BREAK_DURATION, K_BREAK_TIME_ERRORS, K_BREAK_REASON, K_BREAK_REPEAT):
        context.user_data.pop(key, None)
    for flag in (
        "awaiting_break_duration",
        "awaiting_break_reason",
        "awaiting_break_repeat",
        "awaiting_break_time",
    ):
        context.user_data.pop(flag, None)

def _normalize_phone(value: str) -> str:
    phone = (value or "").strip()
    for ch in [" ", "-", "(", ")", "\u00A0"]:
        phone = phone.replace(ch, "")
    return phone

def _generate_offline_tg_id() -> int:
    return -int(datetime.now(tz=pytz.UTC).timestamp() * 1_000_000)

def _increment_admin_time_errors(context: ContextTypes.DEFAULT_TYPE) -> int:
    errors = int(context.user_data.get(K_ADMIN_TIME_ERRORS, 0)) + 1
    context.user_data[K_ADMIN_TIME_ERRORS] = errors
    return errors

async def _sync_break_rules(session, settings: SettingsView) -> None:
    await generate_breaks_from_rules(
        session,
        settings,
        horizon_days=settings.booking_horizon_days,
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            await upsert_user(s, update.effective_user.id, update.effective_user.username, update.effective_user.full_name)
    await update.message.reply_text(
        "Привет! \n\n"
        f"👋 Я — бот {cfg.name} по записи на депиляцию и эпиляцию ✨\n\n"
        "С моей помощью ты можешь: \n"
        "• быстро записаться на процедуру в удобное время \n"
        "• узнать стоимость услуг и адрес студии \n"
        "• посмотреть историю своих записей \n"
        "• получать напоминания, чтобы ничего не забыть 🗓️\n"
        "Я здесь, чтобы сделать процесс записи простым и комфортным \n\n"
        "💛 Если понадобится помощь — я рядом.\n"
        "Приятного пользования и до встречи на процедуре 🤗",
        reply_markup=main_menu_for(update, context)
    )
    if is_admin(cfg, update.effective_user.id):
        await update.message.reply_text("Админ-панель 👇", reply_markup=admin_menu_kb())

async def unified_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_break_duration"):
        return await handle_break_duration(update, context)
    if context.user_data.get("awaiting_break_reason"):
        return await handle_break_reason(update, context)
    if context.user_data.get("awaiting_break_repeat"):
        return await handle_break_repeat_text(update, context)
    if context.user_data.get("awaiting_break_time"):
        return await handle_break_time(update, context)
    if context.user_data.get("awaiting_admin_duration"):
        return await handle_admin_duration(update, context)
    if context.user_data.get("awaiting_admin_time"):
        return await handle_admin_time(update, context)
    if context.user_data.get("awaiting_admin_client_name"):
        return await handle_admin_client_name(update, context)
    if context.user_data.get("awaiting_admin_client_phone"):
        return await handle_admin_client_phone(update, context)
    if context.user_data.get("awaiting_admin_client_tg"):
        return await handle_admin_client_tg(update, context)
    if context.user_data.get("awaiting_admin_price"):
        return await handle_admin_price(update, context)
    if context.user_data.get("awaiting_admin_confirm_price"):
        return await handle_admin_confirm_price(update, context)
    if context.user_data.get("awaiting_admin_visit_price"):
        return await handle_admin_visit_price(update, context)
    if context.user_data.get("awaiting_question"):
        return await handle_question(update, context)
    if context.user_data.get("awaiting_comment"):
        return await handle_comment(update, context)
    if context.user_data.get("awaiting_phone"):
        return await handle_contact(update, context)
    return await text_router(update, context)

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "Записаться":
        return await flow_services(update, context)
    if txt == "Цены и услуги":
        return await show_prices(update, context)
    if txt == "Адрес / Контакты":
        return await show_contacts(update, context)
    if txt == "Мои записи":
        return await show_my_appointments(update, context)
    if txt == "История":
        return await show_my_history(update, context)
    if txt == "Подготовка к процедуре":
        return await show_precare(update, context)
    if txt == "Уход после процедуры":
        return await show_aftercare(update, context)
    if txt == "Задать вопрос":
        return await ask_question(update, context)

    # Admin menu (только для ADMIN_TELEGRAM_ID)
    cfg: Config = context.bot_data.get("cfg")
    if cfg and is_admin(cfg, update.effective_user.id):
        if txt == "📅 Записи сегодня":
            return await admin_day_view(update, context, offset_days=0)
        if txt == "📅 Записи завтра":
            return await admin_day_view(update, context, offset_days=1)
        if txt == "📆 Записи неделя":
            return await admin_week_view(update, context)
        if txt == "🧾 Все заявки (Ожидание)":
            return await admin_holds_view(update, context)
        if txt == "🗓 Все заявки":
            return await admin_booked_month_view(update, context)
        if txt == "📝 Записать клиента":
            return await admin_start_booking(update, context)
        if txt == "⏸ Перерыв":
            return await admin_start_break(update, context)
        if txt == "🗑 Отменить перерыв":
            return await admin_cancel_break_view(update, context)
        if txt == "⬅️ В главное меню":
            await update.message.reply_text("Главное меню 👇", reply_markup=main_menu_for(update, context))
            return
        if txt == "Админ-меню":
            await update.message.reply_text("Админ-панель 👇", reply_markup=admin_menu_kb())
            return

    await update.message.reply_text("Используй кнопки меню 👇", reply_markup=main_menu_for(update, context))

async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("price_category", None)
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
    categories = _available_service_categories(services)
    if not categories:
        await update.message.reply_text("Пока нет услуг. Напиши мастеру.", reply_markup=main_menu_for(update, context))
        return
    await update.message.reply_text(
        "Выбери категорию для просмотра прайс-листа:",
        reply_markup=category_selector_kb(
            categories,
            callback_prefix="pricecat",
            back_callback="back:main",
        ),
    )

async def show_prices_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str):
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        all_services = await list_active_services_filtered(s, _hidden_service_categories(context))
        services = await list_active_services_by_category_filtered(s, category, _hidden_service_categories(context))
    if not services:
        return await update.callback_query.message.edit_text("Для этой категории пока нет услуг.")

    lines = [f"Прайс-лист • {service_category_title(category)}:"]
    for service in services:
        lines.append(f"• {service_label_with_category(service)} — {format_price(service.price)} — {int(service.duration_min)} мин")

    context.user_data["price_category"] = category
    await update.callback_query.message.edit_text(
        "\n".join(lines),
        reply_markup=category_selector_kb(
            _available_service_categories(all_services),
            callback_prefix="pricecat",
            back_callback="back:prices",
        ),
    )

async def show_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address_query = quote(ADDRESS_LINE)
    yandex_maps_url = f"https://yandex.ru/maps/?text={address_query}"
    await update.message.reply_text(
        CONTACTS,
        reply_markup=contacts_kb(yandex_maps_url=yandex_maps_url),
    )

async def send_address_copy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.reply_text(
        f"Адрес для копирования:\n{ADDRESS_LINE}",
        reply_markup=main_menu_for(update, context),
    )

async def show_precare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        PRECARE_RECOMMENDATIONS,
        reply_markup=main_menu_for(update, context),
    )

async def show_aftercare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        AFTERCARE_RECOMMENDATIONS,
        reply_markup=main_menu_for(update, context),
    )

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши вопрос одним сообщением — я перешлю мастеру.", reply_markup=main_menu_for(update, context))
    context.user_data["awaiting_question"] = True

async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not context.user_data.get("awaiting_question"):
        return
    context.user_data["awaiting_question"] = False
    q = update.message.text.strip()
    user = update.effective_user
    await notify_admins(
        context,
        cfg,
        text=(
            "❓ Вопрос от клиента:\n"
            f"Имя: {user.full_name}\n@{user.username}\nTG ID: {user.id}\n\n{q}"
        ),
    )
    await update.message.reply_text("Отправлено ✅ Мастер ответит вам в Telegram.", reply_markup=main_menu_for(update, context))

async def flow_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(K_SVC, None)
    context.user_data.pop(K_SVCS, None)
    context.user_data.pop(K_BOOKING_CATEGORY, None)
    await update.message.reply_text(
        "Выбери категорию услуг:",
        reply_markup=booking_categories_kb(_hidden_service_categories(context)),
    )

async def admin_start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        if update.message:
            return await update.message.reply_text("Нет доступа.")
        if update.callback_query:
            return await update.callback_query.message.edit_text("Нет доступа.")
        return
    _clear_admin_booking(context)
    context.user_data.pop("admin_booking_category", None)
    return await admin_show_booking_categories(update, context)


async def admin_show_booking_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
    categories = _available_service_categories(services)
    if not categories:
        if update.message:
            await update.message.reply_text("Услуги пока не настроены.", reply_markup=admin_menu_kb())
        elif update.callback_query:
            await update.callback_query.message.edit_text("Услуги пока не настроены.")
        return

    text = "Выбери категорию услуг для записи:"
    reply_markup = category_selector_kb(
        categories,
        callback_prefix="admbookcat",
        back_callback="back:main",
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup)


async def admin_show_services_by_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str):
    context.user_data["admin_booking_category"] = category
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        services = await list_active_services_by_category_filtered(s, category, _hidden_service_categories(context))
    if not services:
        return await update.callback_query.message.edit_text("Для этой категории пока нет услуг.")
    await update.callback_query.message.edit_text(
        f"{service_category_title(category)}\n\nВыбери услугу для записи:",
        reply_markup=admin_services_kb(services, back_callback="admback:categories"),
    )

async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("bookcat:"):
        category = data.split(":", 1)[1]
        context.user_data[K_BOOKING_CATEGORY] = category
        context.user_data.pop(K_SVCS, None)
        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            services = await list_active_services_by_category_filtered(s, category, _hidden_service_categories(context))
        if not services:
            return await query.message.edit_text("Для этой категории пока нет услуг.")
        await query.message.edit_text(
            f"{service_category_title(category)}\n\nВыбери одну или несколько услуг, затем нажми «Далее»:",
            reply_markup=services_multi_kb(services, set()),
        )
        return

    if data.startswith("pricecat:"):
        category = data.split(":", 1)[1]
        return await show_prices_category(update, context, category)

    if data.startswith("admbookcat:"):
        category = data.split(":", 1)[1]
        return await admin_show_services_by_category(update, context, category)

    if data.startswith("svcsel:"):
        svc_id = int(data.split(":")[1])
        selected = _selected_service_ids(context)
        if svc_id in selected:
            selected = [x for x in selected if x != svc_id]
        else:
            selected.append(svc_id)
        context.user_data[K_SVCS] = selected

        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            services = await list_active_services_by_category_filtered(s, context.user_data.get(K_BOOKING_CATEGORY), _hidden_service_categories(context))
        await query.message.edit_text(
            "Выбери одну или несколько услуг, затем нажми «Далее»:",
            reply_markup=services_multi_kb(services, set(selected)),
        )
        return

    if data == "svcclear":
        context.user_data.pop(K_SVCS, None)
        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            services = await list_active_services_by_category_filtered(s, context.user_data.get(K_BOOKING_CATEGORY), _hidden_service_categories(context))
        await query.message.edit_text(
            "Выбери одну или несколько услуг, затем нажми «Далее»:",
            reply_markup=services_multi_kb(services, set()),
        )
        return

    if data == "svcnext":
        selected = _selected_service_ids(context)
        if not selected:
            await query.message.edit_text("Выбери хотя бы одну услугу.")
            return
        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            services = await list_active_services_by_category_filtered(s, context.user_data.get(K_BOOKING_CATEGORY), _hidden_service_categories(context))
        selected_services = _collect_selected_services(services, selected)
        if not selected_services:
            await query.message.edit_text("Выбери хотя бы одну услугу.")
            return
        context.user_data[K_SVC] = selected_services[0].id
        return await flow_dates(update, context)

    if data.startswith("svc:"):
        context.user_data[K_SVC] = int(data.split(":")[1])
        context.user_data[K_SVCS] = [context.user_data[K_SVC]]
        return await flow_dates(update, context)

    if data.startswith("admsvc:"):
        context.user_data[K_ADMIN_SVC] = int(data.split(":")[1])
        return await admin_flow_dates(update, context)

    if data.startswith("date:"):
        context.user_data[K_DATE] = data.split(":")[1]
        if context.user_data.get(K_RESCHED_APPT):
            return await flow_reschedule_slots(update, context)
        return await flow_slots(update, context)

    if data.startswith("admdate:"):
        context.user_data[K_ADMIN_DATE] = data.split(":")[1]
        return await admin_prompt_duration(update, context)

    if data.startswith("breakdate:"):
        context.user_data[K_BREAK_DATE] = data.split(":")[1]
        return await admin_break_prompt_duration(update, context)

    if data.startswith("slot:"):
        context.user_data[K_SLOT] = data.split("slot:")[1]
        if context.user_data.get(K_RESCHED_APPT):
            context.user_data[K_RESCHED_SLOT] = context.user_data[K_SLOT]
            return await confirm_reschedule_request(update, context)
        return await flow_comment(update, context)

    if data == "req:send":
        return await finalize_request(update, context)

    if data.startswith("myresched:"):
        appt_id = int(data.split(":")[1])
        return await start_reschedule(update, context, appt_id)

    if data.startswith("adm:confirm:"):
        appt_id = int(data.split(":")[2])
        return await admin_action_confirm(update, context, appt_id)

    if data.startswith("adm:reject:"):
        appt_id = int(data.split(":")[2])
        return await admin_action_reject(update, context, appt_id)

    if data.startswith("adm:msg:"):
        appt_id = int(data.split(":")[2])
        return await admin_action_msg(update, context, appt_id)

    if data.startswith("adm:cancel:"):
        appt_id = int(data.split(":")[2])
        return await admin_cancel(update, context, appt_id)

    if data.startswith("adm:visit:confirm:"):
        appt_id = int(data.split(":")[3])
        return await admin_visit_confirm(update, context, appt_id)

    if data.startswith("adm:visit:price:"):
        appt_id = int(data.split(":")[3])
        return await admin_visit_price(update, context, appt_id)

    if data.startswith("admresched:start:"):
        appt_id = int(data.split(":")[2])
        return await admin_start_reschedule(update, context, appt_id)

    if data.startswith("admtime:"):
        slot_iso = data.split(":", 1)[1]
        return await admin_pick_time_from_slots(update, context, slot_iso)

    if data.startswith("breaktime:"):
        slot_iso = data.split(":", 1)[1]
        return await admin_pick_break_time(update, context, slot_iso)

    if data.startswith("breakrepeat:"):
        repeat = data.split(":", 1)[1]
        if repeat not in {"none", "daily", "weekly"}:
            await query.message.reply_text("Не удалось распознать регулярность. Попробуй ещё раз.")
            return
        context.user_data[K_BREAK_REPEAT] = repeat
        context.user_data["awaiting_break_repeat"] = False
        return await _send_break_time_prompt(update, context)

    if data.startswith("breakcsel:"):
        block_id = int(data.split(":", 1)[1])
        selected = set(_selected_break_cancel_ids(context))
        if block_id in selected:
            selected.remove(block_id)
        else:
            selected.add(block_id)
        context.user_data[K_BREAK_CANCEL_IDS] = list(selected)
        _, items = await _load_break_cancel_items(context)
        valid_ids = {block_id for block_id, _, _ in items}
        selected = selected & valid_ids
        context.user_data[K_BREAK_CANCEL_IDS] = list(selected)
        if not items:
            return await query.message.edit_text("Перерывы не найдены.")
        selected_label = f"Выбрано: {len(selected)}"
        return await query.message.edit_text(
            f"Выберите перерывы для отмены.\n{selected_label}",
            reply_markup=cancel_breaks_kb(items, selected),
        )

    if data == "breakcclear":
        context.user_data[K_BREAK_CANCEL_IDS] = []
        _, items = await _load_break_cancel_items(context)
        if not items:
            return await query.message.edit_text("Перерывы не найдены.")
        return await query.message.edit_text(
            "Выберите перерывы для отмены.\nВыбрано: 0",
            reply_markup=cancel_breaks_kb(items, set()),
        )

    if data == "breakcconfirm":
        selected = set(_selected_break_cancel_ids(context))
        if not selected:
            _, items = await _load_break_cancel_items(context)
            if not items:
                return await query.message.edit_text("Перерывы не найдены.")
            return await query.message.edit_text(
                "Выберите хотя бы один перерыв.",
                reply_markup=cancel_breaks_kb(items, set()),
            )
        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            async with s.begin():
                deleted = 0
                for block_id in selected:
                    if await delete_blocked_interval(s, block_id):
                        deleted += 1
        context.user_data[K_BREAK_CANCEL_IDS] = []
        if deleted == 0:
            return await query.message.edit_text("Перерывы уже отменены или не найдены.")
        await query.message.edit_text(f"Отменено перерывов: {deleted} ✅")
        await query.message.reply_text("Админ-панель 👇", reply_markup=admin_menu_kb())
        return

    if data.startswith("breakcancel:"):
        block_id = int(data.split(":", 1)[1])
        return await admin_cancel_break(update, context, block_id)

    if data == "back:main":
        await query.message.reply_text("Главное меню 👇", reply_markup=main_menu_for(update, context))
        return

    if data == "back:services":
        return await flow_services_from_callback(update, context)

    if data == "back:dates":
        return await flow_dates(update, context)

    if data == "back:phone":
        context.user_data.pop(K_PHONE, None)
        return await prompt_phone(update, context)

    if data == "admback:services":
        return await admin_start_booking(update, context)

    if data == "admback:categories":
        return await admin_show_booking_categories(update, context)

    if data == "back:prices":
        session_factory = context.bot_data["session_factory"]
        async with session_factory() as s:
            services = await list_active_services_filtered(s, _hidden_service_categories(context))
        categories = _available_service_categories(services)
        if not categories:
            return await query.message.edit_text("Пока нет услуг. Напиши мастеру.")
        return await query.message.edit_text(
            "Выбери категорию для просмотра прайс-листа:",
            reply_markup=category_selector_kb(
                categories,
                callback_prefix="pricecat",
                back_callback="back:main",
            ),
        )

    if data == "admback:dates":
        return await admin_flow_dates(update, context)

    if data == "breakback:dates":
        return await admin_start_break(update, context)

    if data == "myback:list":
        return await show_my_appointments_from_cb(update, context)

    if data.startswith("my:"):
        appt_id = int(data.split(":")[1])
        return await show_my_appointment_detail(update, context, appt_id)

    if data.startswith("mycancel:"):
        appt_id = int(data.split(":")[1])
        return await client_cancel(update, context, appt_id)

    if data.startswith("r:confirm:"):
        appt_id = int(data.split(":")[2])
        return await reminder_confirm(update, context, appt_id)

    if data.startswith("r:cancel:"):
        appt_id = int(data.split(":")[2])
        return await reminder_cancel(update, context, appt_id)

    if data.startswith("r:resched:"):
        appt_id = int(data.split(":")[2])
        return await start_reschedule(update, context, appt_id)

    if data.startswith("rdate:"):
        context.user_data[K_RESCHED_DATE] = data.split(":")[1]
        return await flow_reschedule_slots(update, context)

    if data.startswith("rslot:"):
        context.user_data[K_RESCHED_SLOT] = data.split(":")[1]
        return await confirm_reschedule_request(update, context)

    if data == "resched:send":
        return await finalize_reschedule_request(update, context)

    if data == "rback:dates":
        return await flow_reschedule_dates(update, context)

    if data.startswith("admresched:date:"):
        context.user_data[K_ADMIN_RESCHED_DATE] = data.split(":")[2]
        return await admin_flow_reschedule_slots(update, context)

    if data.startswith("admresched:slot:"):
        context.user_data[K_ADMIN_RESCHED_SLOT] = data.split(":")[2]
        return await admin_confirm_reschedule(update, context)

    if data == "admresched:send":
        return await admin_finalize_reschedule(update, context)

    if data == "admresched:back:dates":
        return await admin_flow_reschedule_dates(update, context)

    if data.startswith("adm:resched:confirm:"):
        appt_id = int(data.split(":")[3])
        return await admin_reschedule_confirm(update, context, appt_id)

    if data.startswith("adm:resched:reject:"):
        appt_id = int(data.split(":")[3])
        return await admin_reschedule_reject(update, context, appt_id)

    if data == "contact:copy":
        return await send_address_copy(update, context)

async def flow_services_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.callback_query.message
    category = context.user_data.get(K_BOOKING_CATEGORY)
    if not category:
        await msg.edit_text("Выбери категорию услуг:", reply_markup=booking_categories_kb(_hidden_service_categories(context)))
        return
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        services = await list_active_services_by_category_filtered(s, category, _hidden_service_categories(context))
    selected = set(_selected_service_ids(context))
    await msg.edit_text(
        f"{service_category_title(category)}\n\nВыбери одну или несколько услуг, затем нажми «Далее»:",
        reply_markup=services_multi_kb(services, selected),
    )

async def flow_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            dates = await list_available_dates(s, settings)
    await update.callback_query.message.edit_text("Выбери дату:", reply_markup=dates_kb(dates))

async def admin_flow_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            dates = await list_available_dates(s, settings)
    await update.callback_query.message.edit_text("Выбери дату для записи:", reply_markup=admin_dates_kb(dates))

async def admin_start_break(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.effective_message.reply_text("Нет доступа.")
    _clear_break(context)
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        dates = await list_available_dates(s, settings)
    await update.effective_message.reply_text("Выбери день перерыва:", reply_markup=break_dates_kb(dates))

async def admin_break_prompt_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_break_duration"] = True
    await update.callback_query.message.edit_text(
        "Укажи длительность перерыва в минутах (например, 30)."
    )

async def admin_prompt_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_admin_duration"] = True
    await update.callback_query.message.edit_text(
        "Введи длительность услуги в минутах (например, 45).\n"
        "Можно отправить «-», чтобы взять стандартную длительность услуги."
    )

async def _admin_send_time_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    svc_id = context.user_data.get(K_ADMIN_SVC)
    day_iso = context.user_data.get(K_ADMIN_DATE)
    if not svc_id or not day_iso:
        _clear_admin_booking(context)
        await update.effective_message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())
        return

    day = date.fromisoformat(day_iso)
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            services = await list_active_services_filtered(s, _hidden_service_categories(context))
            service = next((x for x in services if x.id == int(svc_id)), None)
            if not service:
                _clear_admin_booking(context)
                await update.effective_message.reply_text("Услуга недоступна.", reply_markup=admin_menu_kb())
                return
            duration_min = int(context.user_data.get(K_ADMIN_DURATION) or service.duration_min)
            slots = await list_available_slots_for_duration(s, settings, service, day, duration_min)

    context.user_data["awaiting_admin_time"] = True
    slots_hint = "Свободных слотов нет."
    if slots:
        slots_hint = "Свободные слоты: " + ", ".join(st.strftime("%H:%M") for st in slots[:12])
        if len(slots) > 12:
            slots_hint += " и ещё…"

    await update.effective_message.reply_text(
        "Введи время визита в формате HH:MM (например, 14:30).\n"
        f"Длительность: {duration_min} мин.\n"
        f"{slots_hint}\n"
        "Можно выбрать время кнопкой ниже.",
        reply_markup=admin_slots_kb(slots),
    )

async def _send_break_time_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["cfg"]
    day_iso = context.user_data.get(K_BREAK_DATE)
    duration_min = context.user_data.get(K_BREAK_DURATION)
    if not day_iso or not duration_min:
        _clear_break(context)
        await update.effective_message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())
        return

    day = date.fromisoformat(day_iso)
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        slots = await list_available_break_slots(s, settings, day, int(duration_min))

    context.user_data["awaiting_break_time"] = True
    slots_hint = "Свободных слотов нет."
    if slots:
        slots_hint = "Свободные слоты: " + ", ".join(st.strftime("%H:%M") for st in slots[:12])
        if len(slots) > 12:
            slots_hint += " и ещё…"

    await update.effective_message.reply_text(
        "Выбери время начала перерыва в формате HH:MM (например, 14:30).\n"
        f"Длительность: {int(duration_min)} мин.\n"
        f"{slots_hint}\n"
        "Можно выбрать время кнопкой ниже.",
        reply_markup=break_slots_kb(slots),
    )

async def flow_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    svc_id = context.user_data.get(K_SVC)
    day_iso = context.user_data.get(K_DATE)
    if not svc_id or not day_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Нажми «Записаться» заново.")
    day = date.fromisoformat(day_iso)

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            services = await list_active_services_filtered(s, _hidden_service_categories(context))
            service = next((x for x in services if x.id == svc_id), None)
            if not service:
                return await update.callback_query.message.edit_text("Услуга недоступна.")
            selected_services = _collect_selected_services(services, _selected_service_ids(context))
            if len(selected_services) > 1:
                duration_min = _slot_duration_for_services(selected_services, service)
                slots = await list_available_slots_for_duration(s, settings, service, day, duration_min)
            else:
                slots = await list_available_slots_for_service(s, settings, service, day)

    if not slots:
        return await update.callback_query.message.edit_text("На эту дату нет свободных слотов. Выбери другую дату.")

    await update.callback_query.message.edit_text("Выбери время:", reply_markup=slots_kb(slots))

async def flow_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.edit_text(
        "Комментарий (при желании). Отправь «-», если без комментария."
    )
    context.user_data["awaiting_comment"] = True

async def prompt_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_phone"] = True
    await update.effective_message.reply_text(
        "Теперь отправь телефон кнопкой 👇\n"
        "Если кнопки нет — нажми /start и снова «Записаться».",
        reply_markup=phone_request_kb(),
    )

async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_comment"):
        return

    context.user_data["awaiting_comment"] = False
    c = (update.message.text or "").strip()
    context.user_data[K_COMMENT] = None if c == "-" else c

    await prompt_phone(update, context)
    return


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Получает телефон (через contact или текстом), сохраняет его и показывает подтверждение заявки.

    ВАЖНО: берём выбранную услугу/слот из тех же ключей user_data, которые заполняются
    на шагах выбора услуги/даты/времени: K_SVC ("svc_id") и K_SLOT ("slot_iso").
    """
    if not context.user_data.get("awaiting_phone"):
        return

    msg = update.message
    if not msg:
        return

    # 1) достаём телефон: контакт или текст (или пропуск)
    phone = None
    if msg.contact and msg.contact.phone_number:
        phone = msg.contact.phone_number
    else:
        txt = (msg.text or "").strip()
        ok = all(ch.isdigit() or ch in "+-() " for ch in txt) and any(ch.isdigit() for ch in txt)
        if ok:
            phone = txt

    if not phone:
        await msg.reply_text(
            "Не вижу номер телефона. Нажми кнопку «Отправить телефон» 👇"
        )
        return

    # нормализация
    if phone:
        phone = (phone or "").strip()
        for ch in [" ", "-", "(", ")", "\u00A0"]:
            phone = phone.replace(ch, "")

    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]

    # 2) читаем данные флоу (услуга/слот/коммент)
    svc_id = context.user_data.get(K_SVC)
    slot_iso = context.user_data.get(K_SLOT)
    context.user_data[K_PHONE] = phone

    # 3) сохраняем телефон (если есть) + собираем сводку
    async with session_factory() as s:
        await upsert_user(
            s,
            tg_id=update.effective_user.id,
            username=update.effective_user.username,
            full_name=update.effective_user.full_name,
        )
        if phone:
            await set_user_phone(s, update.effective_user.id, phone)

        settings = await get_settings(s, cfg.timezone)

        # валидация: обязательно должны быть услуга и слот
        if not svc_id or not slot_iso:
            context.user_data["awaiting_phone"] = False
            await s.commit()
            prefix = "Телефон сохранён ✅\n"
            await msg.reply_text(
                f"{prefix}Но я не вижу выбранную услугу/время. Начни запись заново: /start → «Записаться».",
                reply_markup=main_menu_for(update, context),
            )
            return

        start_local = datetime.fromisoformat(slot_iso)

        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == int(svc_id)), None)
        if not service:
            context.user_data["awaiting_phone"] = False
            await s.commit()
            prefix = "Телефон сохранён ✅\n"
            await msg.reply_text(
                f"{prefix}Выбранная услуга недоступна. Начни запись заново: /start → «Записаться».",
                reply_markup=main_menu_for(update, context),
            )
            return
        await s.commit()

    context.user_data["awaiting_phone"] = False
    selected_services = _collect_selected_services(services, _selected_service_ids(context))
    if not selected_services:
        selected_services = [service]
    total_price = sum(Decimal(str(s.price)) for s in selected_services)
    duration_min = _display_duration_for_services(selected_services)
    price_label = format_price(total_price)
    local_dt = start_local.astimezone(settings.tz) if start_local.tzinfo else settings.tz.localize(start_local)
    await msg.reply_text(
        "Проверь, всё ли верно перед отправкой заявки:\n"
        f"Услуги: {services_label_with_category(selected_services)}\n"
        f"Дата/время: {local_dt.strftime('%d.%m %H:%M')}\n"
        f"Длительность: {int(duration_min)} мин (+буфер)\n"
        f"Цена: {price_label}",
        reply_markup=confirm_request_kb(),
    )

async def handle_admin_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_duration"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_booking(context)
        return await update.message.reply_text("Нет доступа.")

    txt = (update.message.text or "").strip().lower()
    if txt in {"отмена", "cancel", "/cancel"}:
        _clear_admin_booking(context)
        return await update.message.reply_text("Запись отменена.", reply_markup=admin_menu_kb())

    svc_id = context.user_data.get(K_ADMIN_SVC)
    day_iso = context.user_data.get(K_ADMIN_DATE)
    if not svc_id or not day_iso:
        _clear_admin_booking(context)
        return await update.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == int(svc_id)), None)
        if not service:
            _clear_admin_booking(context)
            return await update.message.reply_text("Услуга недоступна.", reply_markup=admin_menu_kb())

    if txt in {"-", "стандарт", "стандартная"}:
        duration_min = int(service.duration_min)
    else:
        try:
            duration_min = int(txt)
        except ValueError:
            return await update.message.reply_text("Длительность должна быть числом. Введи количество минут.")
        if duration_min <= 0:
            return await update.message.reply_text("Длительность должна быть больше нуля. Введи количество минут.")

    context.user_data[K_ADMIN_DURATION] = duration_min
    context.user_data["awaiting_admin_duration"] = False
    await _admin_send_time_prompt(update, context)

async def handle_break_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_break_duration"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_break(context)
        return await update.message.reply_text("Нет доступа.")

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return await update.message.reply_text("Нужно число минут, например 30.")

    duration = int(text)
    if duration <= 0:
        return await update.message.reply_text("Длительность должна быть больше 0.")

    context.user_data[K_BREAK_DURATION] = duration
    context.user_data["awaiting_break_duration"] = False
    context.user_data["awaiting_break_reason"] = True
    await update.message.reply_text(
        "Напиши название перерыва (например, «Обед»).\n"
        "Можно отправить «-», чтобы оставить стандартное название."
    )

async def handle_break_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_break_reason"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_break(context)
        return await update.message.reply_text("Нет доступа.")

    text = (update.message.text or "").strip()
    if not text or text in {"-", "пропустить", "пропуск"}:
        reason = "Перерыв"
    else:
        reason = text[:200]

    context.user_data[K_BREAK_REASON] = reason
    context.user_data["awaiting_break_reason"] = False
    context.user_data["awaiting_break_repeat"] = True
    await update.message.reply_text(
        "Нужно ли повторять этот перерыв?",
        reply_markup=break_repeat_kb(),
    )

async def handle_break_repeat_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_break_repeat"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_break(context)
        return await update.message.reply_text("Нет доступа.")

    text = (update.message.text or "").strip().lower()
    mapping = {
        "без": "none",
        "без повторов": "none",
        "нет": "none",
        "не повторять": "none",
        "каждый день": "daily",
        "ежедневно": "daily",
        "каждую неделю": "weekly",
        "еженедельно": "weekly",
    }
    repeat = mapping.get(text)
    if repeat is None:
        return await update.message.reply_text(
            "Выбери вариант регулярности кнопкой ниже.",
            reply_markup=break_repeat_kb(),
        )

    context.user_data[K_BREAK_REPEAT] = repeat
    context.user_data["awaiting_break_repeat"] = False
    await _send_break_time_prompt(update, context)

async def admin_pick_time_from_slots(update: Update, context: ContextTypes.DEFAULT_TYPE, slot_iso: str):
    query = update.callback_query
    if not query:
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_booking(context)
        return await query.message.reply_text("Нет доступа.")

    try:
        start_local = datetime.fromisoformat(slot_iso)
    except ValueError:
        return await query.message.reply_text("Не удалось распознать время. Попробуй ещё раз.")

    svc_id = context.user_data.get(K_ADMIN_SVC)
    day_iso = context.user_data.get(K_ADMIN_DATE)
    if not svc_id or not day_iso:
        _clear_admin_booking(context)
        return await query.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == int(svc_id)), None)
        if not service:
            _clear_admin_booking(context)
            return await query.message.reply_text("Услуга недоступна.", reply_markup=admin_menu_kb())

        if start_local.tzinfo is None:
            start_local = settings.tz.localize(start_local)
        duration_min = int(context.user_data.get(K_ADMIN_DURATION) or service.duration_min)
        end_local = compute_slot_end_for_duration(start_local, duration_min, service, settings)
        work_start_local = settings.tz.localize(datetime.combine(start_local.date(), settings.work_start))
        work_end_local = settings.tz.localize(datetime.combine(start_local.date(), settings.work_end))
        if start_local < work_start_local or end_local > work_end_local:
            return await query.message.reply_text(
                f"Время вне рабочего диапазона ({settings.work_start.strftime('%H:%M')}–{settings.work_end.strftime('%H:%M')})."
            )
        try:
            await check_slot_available_for_duration(s, settings, service, start_local, duration_min)
        except ValueError as e:
            code = str(e)
            if code == "SLOT_TAKEN":
                return await query.message.reply_text("Этот слот уже занят. Выбери другое время.")
            if code == "SLOT_BLOCKED":
                return await query.message.reply_text("Это время заблокировано. Выбери другое время.")
            raise

    context.user_data["awaiting_admin_time"] = False
    context.user_data[K_ADMIN_TIME] = start_local.isoformat()
    context.user_data.pop(K_ADMIN_TIME_ERRORS, None)
    context.user_data["awaiting_admin_client_name"] = True
    await query.message.reply_text("Введи имя клиента.")

async def handle_admin_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_time"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_booking(context)
        return await update.message.reply_text("Нет доступа.")

    txt = (update.message.text or "").strip().lower()
    if txt in {"отмена", "cancel", "/cancel"}:
        _clear_admin_booking(context)
        return await update.message.reply_text("Запись отменена.", reply_markup=admin_menu_kb())

    svc_id = context.user_data.get(K_ADMIN_SVC)
    day_iso = context.user_data.get(K_ADMIN_DATE)
    if not svc_id or not day_iso:
        _clear_admin_booking(context)
        return await update.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())

    async def _maybe_abort_after_errors() -> bool:
        if _increment_admin_time_errors(context) >= 3:
            _clear_admin_booking(context)
            await update.message.reply_text(
                "Слишком много ошибок. Процесс записи сброшен.",
                reply_markup=main_menu_for(update, context),
            )
            return True
        return False

    try:
        hh, mm = txt.split(":")
        hh_i = int(hh)
        mm_i = int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            raise ValueError
    except ValueError:
        if await _maybe_abort_after_errors():
            return
        return await update.message.reply_text("Неверный формат времени. Введи HH:MM, например 14:30.")

    day = date.fromisoformat(day_iso)
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == int(svc_id)), None)
        if not service:
            _clear_admin_booking(context)
            return await update.message.reply_text("Услуга недоступна.", reply_markup=admin_menu_kb())

        start_local = settings.tz.localize(datetime.combine(day, time(hh_i, mm_i)))
        now_local = datetime.now(tz=settings.tz)
        if start_local < now_local:
            if await _maybe_abort_after_errors():
                return
            return await update.message.reply_text("Нельзя выбрать время в прошлом. Введи другое время.")

        work_start_local = settings.tz.localize(datetime.combine(day, settings.work_start))
        work_end_local = settings.tz.localize(datetime.combine(day, settings.work_end))
        duration_min = int(context.user_data.get(K_ADMIN_DURATION) or service.duration_min)
        end_local = compute_slot_end_for_duration(start_local, duration_min, service, settings)
        if start_local < work_start_local or end_local > work_end_local:
            if await _maybe_abort_after_errors():
                return
            return await update.message.reply_text(
                f"Время вне рабочего диапазона ({settings.work_start.strftime('%H:%M')}–{settings.work_end.strftime('%H:%M')})."
            )

        try:
            await check_slot_available_for_duration(s, settings, service, start_local, duration_min)
        except ValueError as e:
            code = str(e)
            if code == "SLOT_TAKEN":
                if await _maybe_abort_after_errors():
                    return
                return await update.message.reply_text("Этот слот уже занят. Введи другое время.")
            if code == "SLOT_BLOCKED":
                if await _maybe_abort_after_errors():
                    return
                return await update.message.reply_text("Это время заблокировано. Введи другое время.")
            raise

    context.user_data["awaiting_admin_time"] = False
    context.user_data[K_ADMIN_TIME] = start_local.isoformat()
    context.user_data.pop(K_ADMIN_TIME_ERRORS, None)
    context.user_data["awaiting_admin_client_name"] = True
    await update.message.reply_text("Введи имя клиента.")

async def admin_pick_break_time(update: Update, context: ContextTypes.DEFAULT_TYPE, slot_iso: str):
    query = update.callback_query
    if not query:
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_break(context)
        return await query.message.reply_text("Нет доступа.")

    try:
        start_local = datetime.fromisoformat(slot_iso)
    except ValueError:
        return await query.message.reply_text("Не удалось распознать время. Попробуй ещё раз.")

    await _finalize_break(query.message, context, start_local)

async def handle_break_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_break_time"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_break(context)
        return await update.message.reply_text("Нет доступа.")

    text = (update.message.text or "").strip()
    try:
        hh, mm = text.split(":")
        t = time(int(hh), int(mm))
    except ValueError:
        return await update.message.reply_text("Нужно время в формате HH:MM, например 14:30.")

    day_iso = context.user_data.get(K_BREAK_DATE)
    if not day_iso:
        _clear_break(context)
        return await update.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        day = date.fromisoformat(day_iso)
        start_local = settings.tz.localize(datetime.combine(day, t))
        duration_min = int(context.user_data.get(K_BREAK_DURATION, 0))
        if duration_min <= 0:
            _clear_break(context)
            return await update.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())
        slots = await list_available_break_slots(s, settings, day, duration_min)

    if start_local not in slots:
        errors = int(context.user_data.get(K_BREAK_TIME_ERRORS, 0)) + 1
        context.user_data[K_BREAK_TIME_ERRORS] = errors
        if errors >= 3:
            _clear_break(context)
            return await update.message.reply_text(
                "Слишком много ошибок. Начни заново.", reply_markup=admin_menu_kb()
            )
        return await update.message.reply_text("Этот слот недоступен. Выбери другое время.")

    await _finalize_break(update.message, context, start_local)

def _break_repeat_starts(start_local: datetime, repeat: str) -> list[datetime]:
    if repeat == "daily":
        return [start_local + timedelta(days=offset) for offset in range(28)]
    if repeat == "weekly":
        return [start_local + timedelta(days=7 * offset) for offset in range(4)]
    return [start_local]

def _break_repeat_label(repeat: str) -> str:
    if repeat == "daily":
        return "каждый день (4 недели)"
    if repeat == "weekly":
        return "каждую неделю (4 недели)"
    return "без повторов"

async def _finalize_break(message, context: ContextTypes.DEFAULT_TYPE, start_local: datetime) -> None:
    cfg: Config = context.bot_data["cfg"]
    day_iso = context.user_data.get(K_BREAK_DATE)
    duration_min = int(context.user_data.get(K_BREAK_DURATION, 0))
    reason = (context.user_data.get(K_BREAK_REASON) or "Перерыв").strip() or "Перерыв"
    repeat = (context.user_data.get(K_BREAK_REPEAT) or "none").strip().lower()
    if not day_iso or duration_min <= 0:
        _clear_break(context)
        await message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())
        return

    session_factory = context.bot_data["session_factory"]
    created = []
    skipped = []
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            for candidate_start in _break_repeat_starts(start_local, repeat):
                try:
                    await create_blocked_interval(
                        s,
                        settings,
                        candidate_start,
                        duration_min,
                        created_by_admin=message.from_user.id if message.from_user else admin_ids(cfg)[0],
                        reason=reason,
                    )
                    created.append(candidate_start)
                except ValueError as e:
                    code = str(e)
                    if code in {"SLOT_TAKEN", "SLOT_BLOCKED"}:
                        skipped.append(candidate_start)
                        continue
                    raise
            if repeat in {"daily", "weekly"}:
                last_generated_date = None
                if created:
                    last_generated_date = max(dt.date() for dt in created)
                await create_break_rule(
                    s,
                    repeat=repeat,
                    start_local=start_local,
                    duration_min=duration_min,
                    reason=reason,
                    created_by_admin=message.from_user.id if message.from_user else admin_ids(cfg)[0],
                    last_generated_date=last_generated_date,
                )

    _clear_break(context)
    if not created:
        await message.reply_text(
            "Не удалось добавить перерыв: выбранные слоты заняты или заблокированы.",
            reply_markup=admin_menu_kb(),
        )
        return

    end_local = created[0] + timedelta(minutes=duration_min)
    summary_lines = [
        "Перерыв добавлен ✅",
        f"Название: {reason}",
        f"Дата: {created[0].strftime('%d.%m')}",
        f"Время: {created[0].strftime('%H:%M')}–{end_local.strftime('%H:%M')}",
        f"Повтор: {_break_repeat_label(repeat)}",
        f"Создано: {len(created)}",
    ]
    if skipped:
        skipped_dates = ", ".join(dt.strftime("%d.%m") for dt in skipped[:8])
        if len(skipped) > 8:
            skipped_dates += "…"
        summary_lines.append(f"Пропущено (занято/блок): {skipped_dates}")

    await message.reply_text(
        "\n".join(summary_lines),
        reply_markup=admin_menu_kb(),
    )

async def handle_admin_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_client_name"):
        return
    name = (update.message.text or "").strip()
    if not name:
        return await update.message.reply_text("Имя не может быть пустым. Введи имя клиента.")
    context.user_data["awaiting_admin_client_name"] = False
    context.user_data[K_ADMIN_CLIENT_NAME] = name
    context.user_data["awaiting_admin_client_phone"] = True
    await update.message.reply_text("Введи телефон клиента или «-», если без телефона.")

async def handle_admin_client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_client_phone"):
        return
    txt = (update.message.text or "").strip()
    phone = None
    if txt not in {"-", "без телефона"}:
        cleaned = _normalize_phone(txt)
        if not cleaned or not any(ch.isdigit() for ch in cleaned):
            return await update.message.reply_text("Не вижу телефон. Введи номер или «-» для пропуска.")
        phone = cleaned
    context.user_data["awaiting_admin_client_phone"] = False
    context.user_data[K_ADMIN_CLIENT_PHONE] = phone
    context.user_data["awaiting_admin_client_tg"] = True
    await update.message.reply_text("Введи Telegram ID клиента или «-», если запись без Telegram.")

async def handle_admin_client_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_client_tg"):
        return
    txt = (update.message.text or "").strip()
    tg_id = None
    if txt not in {"-", "нет", "без", "без telegram", "без телеграм"}:
        try:
            tg_id = int(txt)
        except ValueError:
            return await update.message.reply_text("Telegram ID должен быть числом. Введи число или «-».")
    if tg_id is None:
        tg_id = _generate_offline_tg_id()
    context.user_data["awaiting_admin_client_tg"] = False
    context.user_data[K_ADMIN_CLIENT_TGID] = tg_id
    context.user_data["awaiting_admin_price"] = True
    await update.message.reply_text("Введи цену услуги или «-», чтобы оставить стандартную.")

async def handle_admin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_price"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_booking(context)
        return await update.message.reply_text("Нет доступа.")
    txt = (update.message.text or "").strip()
    price_override = None
    if txt not in {"-", "стандарт", "стандартная"}:
        try:
            price_override = float(txt.replace(",", "."))
        except ValueError:
            return await update.message.reply_text("Цена должна быть числом. Введи цену или «-».")
        if price_override < 0:
            return await update.message.reply_text("Цена не может быть отрицательной. Введи цену или «-».")

    svc_id = context.user_data.get(K_ADMIN_SVC)
    day_iso = context.user_data.get(K_ADMIN_DATE)
    time_iso = context.user_data.get(K_ADMIN_TIME)
    duration_min = context.user_data.get(K_ADMIN_DURATION)
    client_name = context.user_data.get(K_ADMIN_CLIENT_NAME)
    client_phone = context.user_data.get(K_ADMIN_CLIENT_PHONE)
    client_tg_id = context.user_data.get(K_ADMIN_CLIENT_TGID)

    if not all([svc_id, day_iso, time_iso, client_name, client_tg_id]):
        _clear_admin_booking(context)
        return await update.message.reply_text("Сессия сброшена. Начни заново.", reply_markup=admin_menu_kb())

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            services = await list_active_services_filtered(s, _hidden_service_categories(context))
            service = next((x for x in services if x.id == int(svc_id)), None)
            if not service:
                _clear_admin_booking(context)
                return await update.message.reply_text("Услуга недоступна.", reply_markup=admin_menu_kb())

            client = await upsert_user(s, client_tg_id, None, client_name)
            if client_phone:
                await set_user_phone(s, client_tg_id, client_phone)

            start_local = datetime.fromisoformat(time_iso)
            try:
                appt = await create_admin_appointment_with_duration(
                    s,
                    settings=settings,
                    client=client,
                    service=service,
                    start_local=start_local,
                    duration_min=int(duration_min or service.duration_min),
                    price_override=price_override,
                    admin_comment="Создано мастером",
                )
            except ValueError as e:
                code = str(e)
                if code == "SLOT_TAKEN":
                    return await update.message.reply_text("Этот слот уже занят. Начни запись заново.", reply_markup=admin_menu_kb())
                if code == "SLOT_BLOCKED":
                    return await update.message.reply_text("Этот слот заблокирован. Начни запись заново.", reply_markup=admin_menu_kb())
                raise
            except Exception as exc:
                logger.exception("Failed to create admin appointment: %s", exc)
                _clear_admin_booking(context)
                return await update.message.reply_text(
                    "Не удалось создать запись. Проверьте базу и попробуйте ещё раз.",
                    reply_markup=admin_menu_kb(),
                )

    _clear_admin_booking(context)
    price_label = format_price(price_override if price_override is not None else service.price)
    local_dt = appt.start_dt.astimezone(settings.tz)
    await update.message.reply_text(
        "Запись создана ✅\n"
        f"Клиент: {client_name}\n"
        f"Услуга: {service_label_with_category(service)}\n"
        f"Дата/время: {local_dt.strftime('%d.%m %H:%M')}\n"
        f"Цена: {price_label}",
        reply_markup=admin_manage_appt_kb(appt.id),
    )

    if client_tg_id > 0:
        try:
            await context.bot.send_message(
                chat_id=client_tg_id,
                text=(
                    "✅ Мастер записал вас на услугу.\n"
                    f"{local_dt.strftime('%d.%m %H:%M')}\n"
                    f"Услуга: {service_label_with_category(service)}\n"
                    f"Цена: {price_label}"
                )
            )
        except Exception:
            pass
    await update.message.reply_text("Админ-панель 👇", reply_markup=admin_menu_kb())

async def handle_admin_confirm_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_confirm_price"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_confirm(context)
        return await update.message.reply_text("Нет доступа.")

    txt = (update.message.text or "").strip()
    if txt.lower() in {"отмена", "cancel", "/cancel"}:
        _clear_admin_confirm(context)
        return await update.message.reply_text("Подтверждение отменено.", reply_markup=admin_menu_kb())

    appt_id = context.user_data.get(K_ADMIN_CONFIRM_APPT)
    if not appt_id:
        _clear_admin_confirm(context)
        return await update.message.reply_text("Сессия сброшена. Повтори подтверждение.", reply_markup=admin_menu_kb())

    price_override = None
    if txt not in {"-", "стандарт", "стандартная"}:
        try:
            price_override = float(txt.replace(",", "."))
        except ValueError:
            return await update.message.reply_text("Цена должна быть числом. Введи цену или «-».")
        if price_override < 0:
            return await update.message.reply_text("Цена не может быть отрицательной. Введи цену или «-».")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            if appt.status != AppointmentStatus.Hold:
                _clear_admin_confirm(context)
                return await update.message.reply_text("Заявка уже обработана.", reply_markup=admin_menu_kb())
            if price_override is not None:
                appt.price_override = price_override
            await admin_confirm(s, appt)

            price_label = format_price(appt.price_override if appt.price_override is not None else appt.service.price)
            await context.bot.send_message(
                chat_id=appt.client.tg_id,
                text=(
                    f"✅ Запись подтверждена!\n"
                    f"{appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')}\n"
                    f"Услуга: {appointment_services_label(appt)}\n"
                    f"Цена: {price_label}\n"
                    f"{cfg.name} ждет Вас!\n\n"
                ),
            )
            await asyncio.sleep(5)
            for part in get_precare_recommendations_parts(appt.service.category, cfg.name):
                await context.bot.send_message(
                    chat_id=appt.client.tg_id,
                    text=part,
                )

    _clear_admin_confirm(context)
    await update.message.reply_text("Подтверждено ✅", reply_markup=admin_menu_kb())

async def handle_admin_visit_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_admin_visit_price"):
        return
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        _clear_admin_visit(context)
        return await update.message.reply_text("Нет доступа.")

    txt = (update.message.text or "").strip()
    if txt.lower() in {"отмена", "cancel", "/cancel"}:
        _clear_admin_visit(context)
        return await update.message.reply_text("Подтверждение отменено.", reply_markup=admin_menu_kb())

    appt_id = context.user_data.get(K_ADMIN_VISIT_APPT)
    if not appt_id:
        _clear_admin_visit(context)
        return await update.message.reply_text("Сессия сброшена. Повтори подтверждение.", reply_markup=admin_menu_kb())

    price_override = None
    if txt not in {"-", "стандарт", "стандартная"}:
        try:
            price_override = float(txt.replace(",", "."))
        except ValueError:
            return await update.message.reply_text("Цена должна быть числом. Введи цену или «-».")
        if price_override < 0:
            return await update.message.reply_text("Цена не может быть отрицательной. Введи цену или «-».")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            if price_override is not None:
                appt.price_override = price_override
            appt.visit_confirmed = True
            if appt.status == AppointmentStatus.Booked and appt.end_dt <= datetime.now(tz=pytz.UTC):
                appt.status = AppointmentStatus.Completed
            appt.updated_at = datetime.now(tz=pytz.UTC)
            price_label = format_price(appt.price_override if appt.price_override is not None else appt.service.price)

    _clear_admin_visit(context)
    await update.message.reply_text(
        f"Готово ✅\nФинальная цена: {price_label}",
        reply_markup=admin_menu_kb(),
    )

async def finalize_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]

    svc_id = context.user_data.get(K_SVC)
    slot_iso = context.user_data.get(K_SLOT)
    phone = context.user_data.get(K_PHONE)
    if not svc_id or not slot_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Нажми «Записаться» заново.")

    start_local = datetime.fromisoformat(slot_iso)

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            client = await upsert_user(s, update.effective_user.id, update.effective_user.username, update.effective_user.full_name)
            if phone:
                await set_user_phone(s, update.effective_user.id, phone)
            services = await list_active_services_filtered(s, _hidden_service_categories(context))
            service = next((x for x in services if x.id == svc_id), None)
            if not service:
                return await update.callback_query.message.edit_text("Услуга недоступна.")
            try:
                selected_services = _collect_selected_services(services, _selected_service_ids(context))
                if not selected_services:
                    selected_services = [service]
                if len(selected_services) > 1:
                    duration_min = _slot_duration_for_services(selected_services, service)
                    total_price = sum(Decimal(str(s.price)) for s in selected_services)
                    comment = context.user_data.get(K_COMMENT)
                    admin_comment = f"Услуги: {services_label_with_category(selected_services)}"
                    appt = await create_hold_appointment_with_duration(
                        s,
                        settings,
                        client,
                        service,
                        start_local,
                        comment=comment,
                        duration_min=duration_min,
                        price_override=total_price,
                        admin_comment=admin_comment,
                    )
                else:
                    appt = await create_hold_appointment(
                        s,
                        settings,
                        client,
                        service,
                        start_local,
                        context.user_data.get(K_COMMENT),
                    )
            except ValueError as e:
                code = str(e)
                if code == "SLOT_TAKEN":
                    return await update.callback_query.message.edit_text("Этот слот уже занят. Выбери другое время.")
                if code == "SLOT_BLOCKED":
                    return await update.callback_query.message.edit_text("Этот слот заблокирован. Выбери другое время.")
                raise

            selected_services = _collect_selected_services(services, _selected_service_ids(context))
            if not selected_services:
                selected_services = [service]
            duration_label = _display_duration_for_services(selected_services)
            total_price = sum(Decimal(str(s.price)) for s in selected_services)
            await notify_admins(
                context,
                cfg,
                text=(
                    f"🆕 Новая заявка (HOLD #{appt.id})\n"
                    f"Услуги: {services_label_with_category(selected_services)}\n"
                    f"Дата/время: {appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')}\n"
                    f"Длительность: {int(duration_label)} мин (+буфер)\n"
                    f"Цена: {format_price(total_price)}\n\n"
                    f"Клиент: {update.effective_user.full_name} (@{update.effective_user.username})\n"
                    f"Телефон: {client.phone or '—'}\n"
                    f"Комментарий: {context.user_data.get(K_COMMENT) or '—'}\n\n"
                    f"Hold истекает: {appt.hold_expires_at.astimezone(settings.tz).strftime('%H:%M')}"
                ),
                reply_markup=admin_request_kb(appt.id),
            )

    for k in (K_SVC, K_SVCS, K_DATE, K_SLOT, K_COMMENT, K_PHONE):
        context.user_data.pop(k, None)

    await update.callback_query.message.edit_text(
        "Заявка создана ✅\nСтатус: Ожидает подтверждения.\nЯ сообщу, когда мастер подтвердит запись."
    )

async def show_my_appointments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appts = await get_user_appointments(s, update.effective_user.id, limit=10)
    if not appts:
        await update.message.reply_text("У вас пока нет записей.", reply_markup=main_menu_for(update, context))
        return
    await update.message.reply_text("Ваши записи:", reply_markup=my_appts_kb(appts, settings.tz))

async def show_my_appointments_from_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appts = await get_user_appointments(s, update.effective_user.id, limit=10)
    if not appts:
        return await update.callback_query.message.edit_text("У вас пока нет записей.")
    await update.callback_query.message.edit_text("Ваши записи:", reply_markup=my_appts_kb(appts, settings.tz))


async def show_my_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appts = await get_user_appointments_history(s, update.effective_user.id, limit=10)
    if not appts:
        await update.message.reply_text("История пустая.", reply_markup=main_menu_for(update, context))
        return
    await update.message.reply_text("История:", reply_markup=my_appts_kb(appts, settings.tz))

async def show_my_history_from_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appts = await get_user_appointments_history(s, update.effective_user.id, limit=10)
    if not appts:
        return await update.callback_query.message.edit_text("История пустая.")
    await update.callback_query.message.edit_text("История:", reply_markup=my_appts_kb(appts, settings.tz))

async def show_my_appointment_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appt = await get_appointment(s, appt_id)

    proposed = ""
    if appt.proposed_alt_start_dt:
        proposed_dt = appt.proposed_alt_start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
        proposed = f"\nЗапрос на перенос: {proposed_dt} (ожидает подтверждения)"

    price = format_price(appt.price_override if appt.price_override is not None else appt.service.price)
    txt = (
        "Запись\n"
        f"Статус: {status_ru(appt.status.value)}\n"
        f"Дата/время: {appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')}\n"
        f"Услуга: {appointment_services_label(appt)}\n"
        f"Цена: {price}\n"
        f"Комментарий: {appt.client_comment or '—'}"
        f"{proposed}"
    )
    kb = my_appt_actions_kb(appt.id) if appt.status == AppointmentStatus.Booked else None
    await update.callback_query.message.edit_text(txt, reply_markup=kb)

async def client_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            ok = await cancel_by_client(s, settings, appt)
            if not ok:
                return await update.callback_query.message.edit_text(
                    f"Отмена недоступна менее чем за {settings.cancel_limit_hours} часов. Напишите мастеру напрямую."
                )
            await notify_admins(
                context,
                cfg,
                text=(
                    "🚫 Клиент отменил запись "
                    f"#{appt.id} на {appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')}"
                ),
            )
    await update.callback_query.message.edit_text("Запись отменена ✅")

async def start_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            if appt.client.tg_id != update.effective_user.id:
                return await update.callback_query.message.edit_text("Нет доступа.")
            if appt.status != AppointmentStatus.Booked:
                return await update.callback_query.message.edit_text("Перенос доступен только для подтверждённых записей.")
            now_utc = datetime.now(tz=pytz.UTC)
            if now_utc > (appt.start_dt - timedelta(hours=settings.cancel_limit_hours)):
                return await update.callback_query.message.edit_text("До визита осталось слишком мало времени. Для переноса свяжитесь напрямую.")

    context.user_data[K_RESCHED_APPT] = appt_id
    context.user_data[K_RESCHED_SVC] = appt.service_id
    return await flow_reschedule_dates(update, context)

async def flow_reschedule_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        dates = await list_available_dates(s, settings)
    await update.callback_query.message.edit_text("Выбери новую дату для переноса:", reply_markup=reschedule_dates_kb(dates))

async def flow_reschedule_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    appt_id = context.user_data.get(K_RESCHED_APPT)
    svc_id = context.user_data.get(K_RESCHED_SVC)
    day_iso = context.user_data.get(K_RESCHED_DATE) or context.user_data.get(K_DATE)
    if not svc_id or not day_iso or not appt_id:
        return await update.callback_query.message.edit_text("Сессия сброшена. Нажми «Мои записи» и начни перенос заново.")
    day = date.fromisoformat(day_iso)

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == svc_id), None)
        if not service:
            return await update.callback_query.message.edit_text("Услуга недоступна.")
        appt = await get_appointment(s, appt_id)
        duration_total = int((appt.end_dt - appt.start_dt).total_seconds() / 60)
        base_duration = max(1, duration_total - int(service.buffer_min) - int(settings.buffer_min))
        slots = await list_available_slots_for_duration(s, settings, service, day, base_duration)

    if not slots:
        return await update.callback_query.message.edit_text("На эту дату нет свободных слотов. Выбери другую дату.")

    await update.callback_query.message.edit_text("Выбери новое время:", reply_markup=reschedule_slots_kb(slots))

async def confirm_reschedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    appt_id = context.user_data.get(K_RESCHED_APPT)
    slot_iso = context.user_data.get(K_RESCHED_SLOT)
    if not appt_id or not slot_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Нажми «Мои записи» и начни перенос заново.")

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appt = await get_appointment(s, appt_id)

    new_start = datetime.fromisoformat(slot_iso)
    if new_start.tzinfo:
        new_local = new_start.astimezone(settings.tz)
    else:
        new_local = settings.tz.localize(new_start)
    new_dt = new_local.strftime('%d.%m %H:%M')
    old_dt = appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
    await update.callback_query.message.edit_text(
        f"Запросить перенос записи?\nТекущее время: {old_dt}\nНовое время: {new_dt}",
        reply_markup=reschedule_confirm_kb()
    )

async def finalize_reschedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    session_factory = context.bot_data["session_factory"]
    appt_id = context.user_data.get(K_RESCHED_APPT)
    slot_iso = context.user_data.get(K_RESCHED_SLOT)
    if not appt_id or not slot_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Нажми «Мои записи» и начни перенос заново.")

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            if appt.client.tg_id != update.effective_user.id:
                return await update.callback_query.message.edit_text("Нет доступа.")
            try:
                await request_reschedule(s, settings, appt, datetime.fromisoformat(slot_iso))
            except ValueError as e:
                code = str(e)
                if code == "SLOT_TAKEN":
                    return await update.callback_query.message.edit_text("Этот слот уже занят. Выбери другое время.")
                if code == "SLOT_BLOCKED":
                    return await update.callback_query.message.edit_text("Это время заблокировано. Выбери другое.")
                return await update.callback_query.message.edit_text("Не удалось отправить запрос на перенос. Попробуй ещё раз.")

            new_local = appt.proposed_alt_start_dt.astimezone(settings.tz)
            old_local = appt.start_dt.astimezone(settings.tz)

            await notify_admins(
                context,
                cfg,
                text=(
                    "🔄 Запрос на перенос записи\n"
                    f"#{appt.id}\n"
                    f"Услуга: {appointment_services_label(appt)}\n"
                    f"Текущее время: {old_local.strftime('%d.%m %H:%M')}\n"
                    f"Новое время: {new_local.strftime('%d.%m %H:%M')}\n"
                    f"Клиент: {appt.client.full_name or appt.client.tg_id}\n"
                    f"Телефон: {appt.client.phone or '—'}"
                ),
                reply_markup=admin_reschedule_kb(appt.id),
            )

    for k in (K_RESCHED_APPT, K_RESCHED_SVC, K_RESCHED_DATE, K_RESCHED_SLOT, K_DATE, K_SLOT):
        context.user_data.pop(k, None)

    await update.callback_query.message.edit_text(
        "Запрос на перенос отправлен ✅\nОжидай подтверждения мастера."
    )

async def admin_action_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            if appt.status != AppointmentStatus.Hold:
                return await update.callback_query.message.edit_text("Заявка уже обработана.")
            price_label = format_price(appt.price_override if appt.price_override is not None else appt.service.price)

    _clear_admin_confirm(context)
    context.user_data[K_ADMIN_CONFIRM_APPT] = appt_id
    context.user_data["awaiting_admin_confirm_price"] = True
    await update.callback_query.message.edit_text(
        "Введи новую цену или «-», чтобы оставить текущую.\n"
        f"Текущая цена: {price_label}\n"
        "Для отмены отправь /cancel."
    )

async def admin_action_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            await admin_reject(s, appt, reason="Отклонено мастером")

            await context.bot.send_message(
                chat_id=appt.client.tg_id,
                text=(
                    f"❌ Запись отклонена.\n"
                    f"Слот: {appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')}\n"
                    f"Попробуйте выбрать другое время."
                )
            )
    await update.callback_query.message.edit_text("Отклонено ❌")

def _is_admin_created(appt) -> bool:
    return (appt.admin_comment or "").strip().lower() == "создано мастером"

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            ok = await admin_cancel_appointment(s, appt)
            if not ok:
                return await update.callback_query.message.edit_text("Отменить можно только подтверждённую запись.")
            appt_local = appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
            if appt.client.tg_id > 0:
                try:
                    await context.bot.send_message(
                        chat_id=appt.client.tg_id,
                        text=(
                            "🚫 Мастер отменил вашу запись.\n"
                            f"Дата/время: {appt_local}\n"
                            f"Услуга: {appointment_services_label(appt)}"
                        )
                    )
                except Exception:
                    pass
    await update.callback_query.message.edit_text("Запись отменена ✅")

async def admin_visit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            appt.visit_confirmed = True
            if appt.status == AppointmentStatus.Booked and appt.end_dt <= datetime.now(tz=pytz.UTC):
                appt.status = AppointmentStatus.Completed
            appt.updated_at = datetime.now(tz=pytz.UTC)

    _clear_admin_visit(context)
    await update.callback_query.message.edit_text("Визит подтверждён ✅")

async def admin_visit_price(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        appt = await get_appointment(s, appt_id)
        price_label = format_price(appt.price_override if appt.price_override is not None else appt.service.price)

    _clear_admin_visit(context)
    context.user_data[K_ADMIN_VISIT_APPT] = appt_id
    context.user_data["awaiting_admin_visit_price"] = True
    await update.callback_query.message.edit_text(
        "Введи финальную цену или «-», чтобы оставить текущую.\n"
        f"Текущая цена: {price_label}\n"
        "Для отмены отправь /cancel."
    )

async def admin_start_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            if not _is_admin_created(appt):
                return await update.callback_query.message.edit_text(
                    "Перенос доступен только для записей, созданных мастером."
                )
            if appt.status != AppointmentStatus.Booked:
                return await update.callback_query.message.edit_text("Перенос доступен только для подтверждённых записей.")

    _clear_admin_reschedule(context)
    context.user_data[K_ADMIN_RESCHED_APPT] = appt_id
    context.user_data[K_ADMIN_RESCHED_SVC] = appt.service_id
    return await admin_flow_reschedule_dates(update, context)

async def admin_flow_reschedule_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        dates = await list_available_dates(s, settings)
    await update.callback_query.message.edit_text(
        "Выбери новую дату для переноса:",
        reply_markup=admin_reschedule_dates_kb(dates),
    )

async def admin_flow_reschedule_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    appt_id = context.user_data.get(K_ADMIN_RESCHED_APPT)
    svc_id = context.user_data.get(K_ADMIN_RESCHED_SVC)
    day_iso = context.user_data.get(K_ADMIN_RESCHED_DATE)
    if not svc_id or not day_iso or not appt_id:
        return await update.callback_query.message.edit_text("Сессия сброшена. Начни перенос заново.")
    day = date.fromisoformat(day_iso)

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        services = await list_active_services_filtered(s, _hidden_service_categories(context))
        service = next((x for x in services if x.id == svc_id), None)
        if not service:
            return await update.callback_query.message.edit_text("Услуга недоступна.")
        appt = await get_appointment(s, appt_id)
        duration_total = int((appt.end_dt - appt.start_dt).total_seconds() / 60)
        base_duration = max(1, duration_total - int(service.buffer_min) - int(settings.buffer_min))
        slots = await list_available_slots_for_duration(s, settings, service, day, base_duration)

    if not slots:
        return await update.callback_query.message.edit_text("На эту дату нет свободных слотов. Выбери другую дату.")

    await update.callback_query.message.edit_text(
        "Выбери новое время:",
        reply_markup=admin_reschedule_slots_kb(slots),
    )

async def admin_confirm_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_factory = context.bot_data["session_factory"]
    cfg: Config = context.bot_data["cfg"]
    appt_id = context.user_data.get(K_ADMIN_RESCHED_APPT)
    slot_iso = context.user_data.get(K_ADMIN_RESCHED_SLOT)
    if not appt_id or not slot_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Начни перенос заново.")

    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        appt = await get_appointment(s, appt_id)

    new_start = datetime.fromisoformat(slot_iso)
    if new_start.tzinfo:
        new_local = new_start.astimezone(settings.tz)
    else:
        new_local = settings.tz.localize(new_start)
    new_dt = new_local.strftime('%d.%m %H:%M')
    old_dt = appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
    await update.callback_query.message.edit_text(
        f"Перенести запись?\nТекущее время: {old_dt}\nНовое время: {new_dt}",
        reply_markup=admin_reschedule_confirm_kb(),
    )

async def admin_finalize_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]
    appt_id = context.user_data.get(K_ADMIN_RESCHED_APPT)
    slot_iso = context.user_data.get(K_ADMIN_RESCHED_SLOT)
    if not appt_id or not slot_iso:
        return await update.callback_query.message.edit_text("Сессия сброшена. Начни перенос заново.")

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            if not _is_admin_created(appt):
                return await update.callback_query.message.edit_text(
                    "Перенос доступен только для записей, созданных мастером."
                )
            if appt.status != AppointmentStatus.Booked:
                return await update.callback_query.message.edit_text("Перенос доступен только для подтверждённых записей.")
            new_start = datetime.fromisoformat(slot_iso)
            now_local = datetime.now(tz=settings.tz)
            if new_start < now_local:
                return await update.callback_query.message.edit_text("Нельзя перенести запись на время в прошлом.")
            try:
                await admin_reschedule_appointment(s, settings, appt, new_start)
            except ValueError as e:
                code = str(e)
                if code == "SLOT_TAKEN":
                    return await update.callback_query.message.edit_text("Слот уже занят. Выбери другое время.")
                if code == "SLOT_BLOCKED":
                    return await update.callback_query.message.edit_text("Слот заблокирован. Выбери другое время.")
                return await update.callback_query.message.edit_text("Не удалось перенести запись.")

            new_local = appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
            if appt.client.tg_id > 0:
                try:
                    await context.bot.send_message(
                        chat_id=appt.client.tg_id,
                        text=(
                            "🔄 Мастер перенёс вашу запись.\n"
                            f"Новая дата/время: {new_local}\n"
                            f"Услуга: {appointment_services_label(appt)}"
                        )
                    )
                except Exception:
                    pass

    _clear_admin_reschedule(context)
    await update.callback_query.message.edit_text("Запись перенесена ✅")

async def admin_reschedule_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            appt = await get_appointment(s, appt_id)
            if not appt.proposed_alt_start_dt:
                return await update.callback_query.message.edit_text("Запрос на перенос не найден.")
            try:
                await confirm_reschedule(s, settings, appt)
            except ValueError as e:
                code = str(e)
                if code == "SLOT_TAKEN":
                    return await update.callback_query.message.edit_text("Слот уже занят. Запрос не подтверждён.")
                if code == "SLOT_BLOCKED":
                    return await update.callback_query.message.edit_text("Слот заблокирован. Запрос не подтверждён.")
                return await update.callback_query.message.edit_text("Не удалось подтвердить перенос.")

            new_local = appt.start_dt.astimezone(settings.tz).strftime('%d.%m %H:%M')
            await context.bot.send_message(
                chat_id=appt.client.tg_id,
                text=(
                    "✅ Перенос подтверждён!\n"
                    f"Новая дата/время: {new_local}\n"
                    f"Услуга: {appointment_services_label(appt)}"
                )
            )
    await update.callback_query.message.edit_text("Перенос подтверждён ✅")

async def admin_reschedule_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]

    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            if not appt.proposed_alt_start_dt:
                return await update.callback_query.message.edit_text("Запрос на перенос не найден.")
            await reject_reschedule(s, appt)
            await context.bot.send_message(
                chat_id=appt.client.tg_id,
                text=(
                    "❌ Перенос отклонён мастером.\n"
                    "Запись остаётся в исходное время."
                )
            )
    await update.callback_query.message.edit_text("Перенос отклонён ❌")

async def admin_action_msg(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        appt = await get_appointment(s, appt_id)
    await update.callback_query.message.edit_text(
        f"TG ID клиента: {appt.client.tg_id}\n@{appt.client.username or '—'}",
        reply_markup=admin_request_kb(appt_id)
    )

async def reminder_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            appt = await get_appointment(s, appt_id)
            appt.visit_confirmed = True
            appt.updated_at = datetime.now(tz=pytz.UTC)
    await update.callback_query.message.edit_text("Отлично, визит подтверждён ✅")

async def reminder_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    return await client_cancel(update, context, appt_id)

def _slot_status_for_time(
    slot_start_local: datetime,
    spans: list[tuple[datetime, datetime, AppointmentStatus]],
    break_spans: list[tuple[datetime, datetime]] | None = None,
) -> AppointmentStatus | str | None:
    if break_spans:
        for start_local, end_local in break_spans:
            if start_local <= slot_start_local < end_local:
                return "break"
    has_hold = False
    for start_local, end_local, status in spans:
        if start_local <= slot_start_local < end_local:
            if status == AppointmentStatus.Booked:
                return AppointmentStatus.Booked
            if status == AppointmentStatus.Hold:
                has_hold = True
    return AppointmentStatus.Hold if has_hold else None

def _build_day_timeline(
    day: date,
    settings: SettingsView,
    appts: list,
    breaks: list[BlockedInterval] | None = None,
    slots_per_line: int = 4,
) -> str:
    work_start_local = settings.tz.localize(datetime.combine(day, settings.work_start))
    work_end_local = settings.tz.localize(datetime.combine(day, settings.work_end))
    step = timedelta(minutes=settings.slot_step_min)
    spans = [
        (a.start_dt.astimezone(settings.tz), a.end_dt.astimezone(settings.tz), a.status)
        for a in appts
    ]
    break_spans = []
    if breaks:
        break_spans = [
            (b.start_dt.astimezone(settings.tz), b.end_dt.astimezone(settings.tz))
            for b in breaks
        ]

    def slot_symbol(status: AppointmentStatus | str | None) -> str:
        if status == AppointmentStatus.Booked:
            return "🟥"
        if status == AppointmentStatus.Hold:
            return "🟨"
        if status == "break":
            return "🟡"
        return "🟩"

    slots: list[str] = []
    cursor = work_start_local
    while cursor < work_end_local:
        status = _slot_status_for_time(cursor, spans, break_spans)
        slots.append(f"{cursor.strftime('%H:%M')}")
        cursor += step

    status_symbols = []
    cursor = work_start_local
    while cursor < work_end_local:
        status = _slot_status_for_time(cursor, spans, break_spans)
        status_symbols.append(slot_symbol(status))
        cursor += step

    entries = [f"{time_label} {symbol}" for time_label, symbol in zip(slots, status_symbols)]
    col_width = max((len(entry) for entry in entries), default=0) + 2
    lines = ["🧭 График слотов:"]
    for idx in range(0, len(entries), slots_per_line):
        row = entries[idx:idx + slots_per_line]
        lines.append("".join(entry.ljust(col_width) for entry in row).rstrip())
    lines.append("Легенда: 🟩 свободно • 🟥 подтверждено • 🟨 ожидает подтверждения • 🟡 перерыв")
    if breaks:
        lines.append("Перерывы:")
        for b in breaks:
            start_t = b.start_dt.astimezone(settings.tz).strftime("%H:%M")
            end_t = b.end_dt.astimezone(settings.tz).strftime("%H:%M")
            reason = b.reason or "Перерыв"
            lines.append(f"• {start_t}–{end_t} | {reason}")
    return "\n".join(lines)

def _pick_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    pil_font = os.path.join(os.path.dirname(ImageFont.__file__), "fonts", "DejaVuSans.ttf")
    candidates = [
        pil_font,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, font_size)
    return ImageFont.load_default()

def _build_day_timeline_image(
    day: date,
    settings: SettingsView,
    appts: list,
    breaks: list[BlockedInterval] | None = None,
    slots_per_line: int = 4,
) -> BytesIO:
    style = DAY_TIMELINE_STYLE
    work_start_local = settings.tz.localize(datetime.combine(day, settings.work_start))
    work_end_local = settings.tz.localize(datetime.combine(day, settings.work_end))
    step = timedelta(minutes=settings.slot_step_min)
    spans = [
        (a.start_dt.astimezone(settings.tz), a.end_dt.astimezone(settings.tz), a.status)
        for a in appts
    ]
    break_entries: list[tuple[datetime, datetime, str]] = []
    if breaks:
        break_entries = [
            (
                b.start_dt.astimezone(settings.tz),
                b.end_dt.astimezone(settings.tz),
                (b.reason or "Перерыв").strip() or "Перерыв",
            )
            for b in breaks
        ]
    break_spans = [(start, end) for start, end, _ in break_entries]

    def slot_color(status: AppointmentStatus | str | None) -> tuple[int, int, int]:
        if status == AppointmentStatus.Booked:
            return style["slot_colors"]["booked"]
        if status == AppointmentStatus.Hold:
            return style["slot_colors"]["hold"]
        if status == "break":
            return style["slot_colors"]["break"]
        return style["slot_colors"]["free"]

    slots: list[tuple[str, AppointmentStatus | str | None]] = []
    cursor = work_start_local
    while cursor < work_end_local:
        status = _slot_status_for_time(cursor, spans, break_spans)
        slots.append((cursor.strftime("%H:%M"), status))
        cursor += step

    title_font = _pick_font(style["font_sizes"]["title"])
    time_font = _pick_font(style["font_sizes"]["time"])
    legend_font = _pick_font(style["font_sizes"]["legend"])

    padding = style["padding"]
    col_gap = style["col_gap"]
    row_gap = style["row_gap"]
    square_size = style["square_size"]

    dummy_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy_img)
    time_width = max((draw.textbbox((0, 0), label, font=time_font)[2] for label, _ in slots), default=0)
    time_height = max((draw.textbbox((0, 0), label, font=time_font)[3] for label, _ in slots), default=0)

    cell_width = time_width + 10 + square_size
    cell_height = max(time_height, square_size)
    rows = (len(slots) + slots_per_line - 1) // slots_per_line
    grid_width = slots_per_line * cell_width + max(slots_per_line - 1, 0) * col_gap
    grid_height = rows * cell_height + max(rows - 1, 0) * row_gap

    title_text = f"График слотов • {day.strftime('%d.%m')}"
    title_height = draw.textbbox((0, 0), title_text, font=title_font)[3]

    legend_labels = [
        ("Свободно", slot_color(None)),
        ("Подтверждено", slot_color(AppointmentStatus.Booked)),
        ("Ожидает подтверждения", slot_color(AppointmentStatus.Hold)),
        ("Перерыв", slot_color("break")),
    ]
    legend_text_height = max(
        (draw.textbbox((0, 0), label, font=legend_font)[3] for label, _ in legend_labels),
        default=0,
    )
    legend_height = legend_text_height + 8

    break_lines: list[str] = []
    if break_entries:
        for start_local, end_local, reason in break_entries:
            if start_local.date() == end_local.date():
                time_label = f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
            else:
                time_label = f"{start_local.strftime('%d.%m %H:%M')}–{end_local.strftime('%d.%m %H:%M')}"
            break_lines.append(f"{time_label} • {reason}")

    break_text_width = max(
        (draw.textbbox((0, 0), line, font=legend_font)[2] for line in break_lines),
        default=0,
    )
    break_line_height = legend_text_height + 6
    break_section_height = (len(break_lines) * break_line_height + 8) if break_lines else 0

    width = max(grid_width, 360, break_text_width) + padding * 2
    height = padding + title_height + 20 + grid_height + 24 + legend_height + break_section_height + padding
    img = Image.new("RGB", (width, height), style["background_color"])
    draw = ImageDraw.Draw(img)

    title_x = padding
    title_y = padding
    draw.text((title_x, title_y), title_text, font=title_font, fill=style["title_color"])

    grid_start_y = title_y + title_height + 20
    for idx, (time_label, status) in enumerate(slots):
        row = idx // slots_per_line
        col = idx % slots_per_line
        x = padding + col * (cell_width + col_gap)
        y = grid_start_y + row * (cell_height + row_gap)
        draw.text((x, y), time_label, font=time_font, fill=style["time_color"])
        square_x = x + time_width + 10
        square_y = y + (cell_height - square_size) // 2
        draw.rounded_rectangle(
            (square_x, square_y, square_x + square_size, square_y + square_size),
            radius=style["legend_square_radius"],
            fill=slot_color(status),
        )

    legend_y = grid_start_y + grid_height + 24
    legend_x = padding
    for label, color in legend_labels:
        draw.rounded_rectangle(
            (legend_x, legend_y + 2, legend_x + square_size, legend_y + square_size + 2),
            radius=style["legend_square_radius"],
            fill=color,
        )
        draw.text(
            (legend_x + square_size + 8, legend_y),
            label,
            font=legend_font,
            fill=style["legend_text_color"],
        )
        legend_x += square_size + 8 + draw.textbbox((0, 0), label, font=legend_font)[2] + 20

    if break_lines:
        break_y = legend_y + legend_height + 10
        draw.text(
            (padding, break_y),
            "Перерывы:",
            font=legend_font,
            fill=style["legend_text_color"],
        )
        break_y += break_line_height
        for line in break_lines:
            draw.text(
                (padding, break_y),
                line,
                font=legend_font,
                fill=style["legend_text_color"],
            )
            break_y += break_line_height

    buffer = BytesIO()
    buffer.name = "timeline.png"
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

def _wrap_text_lines(
    text: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if width <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def _build_week_schedule_image(
    start_day: date,
    settings: SettingsView,
    appts: list,
    breaks: list[BlockedInterval] | None = None,
) -> BytesIO:
    style = WEEK_SCHEDULE_STYLE
    days = [start_day + timedelta(days=offset) for offset in range(7)]
    work_start_minutes = settings.work_start.hour * 60 + settings.work_start.minute
    work_end_minutes = settings.work_end.hour * 60 + settings.work_end.minute
    total_minutes = max(work_end_minutes - work_start_minutes, 60)

    title_font = _pick_font(style["font_sizes"]["title"])
    header_font = _pick_font(style["font_sizes"]["header"])
    time_font = _pick_font(style["font_sizes"]["time"])
    appt_font = _pick_font(style["font_sizes"]["appointment"])

    padding = style["padding"]
    header_height = style["header_height"]
    hour_height = style["hour_height"]
    minute_height = hour_height / 60

    dummy_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy_img)
    time_col_width = draw.textbbox((0, 0), "00:00", font=time_font)[2] + 10

    day_labels = [f"{RU_WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}" for d in days]
    header_widths = [draw.textbbox((0, 0), label, font=header_font)[2] for label in day_labels]
    day_col_width = max(140, max(header_widths, default=120) + 16)

    grid_left = padding + time_col_width + 12
    grid_top = padding + header_height
    grid_width = day_col_width * 7
    grid_height = int(total_minutes * minute_height)

    title_text = f"Записи на неделю • {start_day.strftime('%d.%m')}–{days[-1].strftime('%d.%m')}"
    title_height = draw.textbbox((0, 0), title_text, font=title_font)[3]

    width = grid_left + grid_width + padding
    height = grid_top + grid_height + padding + title_height
    img = Image.new("RGB", (width, height), style["background_color"])
    draw = ImageDraw.Draw(img)

    title_y = padding
    draw.text((padding, title_y), title_text, font=title_font, fill=style["title_color"])

    header_y = title_y + title_height + 12
    for idx, label in enumerate(day_labels):
        x = grid_left + idx * day_col_width + day_col_width / 2
        label_width = draw.textbbox((0, 0), label, font=header_font)[2]
        draw.text(
            (x - label_width / 2, header_y),
            label,
            font=header_font,
            fill=style["header_text_color"],
        )

    grid_top = header_y + header_height - 6

    for day_idx in range(8):
        x = grid_left + day_idx * day_col_width
        draw.line((x, grid_top, x, grid_top + grid_height), fill=style["grid_line_color"], width=1)

    for minute_offset in range(0, total_minutes + 1, 60):
        y = grid_top + minute_offset * minute_height
        draw.line((grid_left, y, grid_left + grid_width, y), fill=style["hour_line_color"], width=1)
        time_minutes = work_start_minutes + minute_offset
        hour = time_minutes // 60
        minute = time_minutes % 60
        label = f"{hour:02d}:{minute:02d}"
        label_width = draw.textbbox((0, 0), label, font=time_font)[2]
        draw.text(
            (grid_left - 12 - label_width, y - 10),
            label,
            font=time_font,
            fill=style["time_text_color"],
        )

    def block_colors(kind: str, status: AppointmentStatus | None) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        if kind == "break":
            colors = style["appointment_colors"]["break"]
            return colors["fill"], colors["outline"]
        if status == AppointmentStatus.Booked:
            colors = style["appointment_colors"]["booked"]
            return colors["fill"], colors["outline"]
        colors = style["appointment_colors"]["hold"]
        return colors["fill"], colors["outline"]

    line_height = draw.textbbox((0, 0), "Ag", font=appt_font)[3] + 2

    break_items = breaks or []
    for kind, item in (
        [("appointment", appt) for appt in appts]
        + [("break", br) for br in break_items]
    ):
        if kind == "break":
            local_start = item.start_dt.astimezone(settings.tz)
            local_end = item.end_dt.astimezone(settings.tz)
            label_lines = [item.reason or "Перерыв"]
            status = None
        else:
            local_start = item.start_dt.astimezone(settings.tz)
            local_end = item.end_dt.astimezone(settings.tz)
            client_label = item.client.full_name or (f"@{item.client.username}" if item.client.username else str(item.client.tg_id))
            service_label = appointment_services_label(item)
            label_lines = [client_label]
            if service_label:
                label_lines.append(service_label)
            status = item.status

        day_offset = (local_start.date() - start_day).days
        if day_offset < 0 or day_offset >= 7:
            continue
        start_min = local_start.hour * 60 + local_start.minute - work_start_minutes
        end_min = local_end.hour * 60 + local_end.minute - work_start_minutes
        if end_min <= 0 or start_min >= total_minutes:
            continue
        start_min = max(start_min, 0)
        end_min = min(end_min, total_minutes)

        x0 = grid_left + day_offset * day_col_width + 6
        x1 = x0 + day_col_width - 12
        y0 = grid_top + start_min * minute_height + 2
        y1 = grid_top + end_min * minute_height - 2
        if y1 - y0 < style["appointment_min_height"]:
            y1 = y0 + style["appointment_min_height"]

        fill, outline = block_colors(kind, status)
        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=style["appointment_corner_radius"],
            fill=fill,
            outline=outline,
            width=style["appointment_outline_width"],
        )

        max_text_width = int(x1 - x0 - style["appointment_text_padding_x"] * 2)
        text_lines: list[str] = []
        for label in label_lines:
            if label:
                text_lines += _wrap_text_lines(label, draw, appt_font, max_text_width)
        max_lines = max(
            int((y1 - y0 - style["appointment_text_padding_y"] * 2) / line_height),
            0,
        )
        if max_lines:
            text_lines = text_lines[:max_lines]
            text_y = y0 + style["appointment_text_padding_y"]
            for line in text_lines:
                draw.text(
                    (x0 + style["appointment_text_padding_x"], text_y),
                    line,
                    font=appt_font,
                    fill=style["appointment_text_color"],
                )
                text_y += line_height

    buffer = BytesIO()
    buffer.name = "week_schedule.png"
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

def _build_single_day_schedule_image(
    day: date,
    settings: SettingsView,
    appts: list,
    breaks: list[BlockedInterval] | None = None,
) -> BytesIO:
    style = WEEK_SCHEDULE_STYLE
    days = [day]
    work_start_minutes = settings.work_start.hour * 60 + settings.work_start.minute
    work_end_minutes = settings.work_end.hour * 60 + settings.work_end.minute
    total_minutes = max(work_end_minutes - work_start_minutes, 60)

    title_font = _pick_font(style["font_sizes"]["title"])
    header_font = _pick_font(style["font_sizes"]["header"])
    time_font = _pick_font(style["font_sizes"]["time"])
    appt_font = _pick_font(style["font_sizes"]["appointment"])

    padding = style["padding"]
    header_height = style["header_height"]
    hour_height = style["hour_height"]
    minute_height = hour_height / 60

    dummy_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy_img)
    time_col_width = draw.textbbox((0, 0), "00:00", font=time_font)[2] + 10

    day_labels = [f"{RU_WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')}"]
    header_widths = [draw.textbbox((0, 0), label, font=header_font)[2] for label in day_labels]
    day_col_width = max(220, max(header_widths, default=120) + 24)

    grid_left = padding + time_col_width + 12
    grid_top = padding + header_height
    grid_width = day_col_width * len(days)
    grid_height = int(total_minutes * minute_height)

    title_text = f"Записи на {day.strftime('%d.%m')} ({RU_WEEKDAYS[day.weekday()]})"
    title_height = draw.textbbox((0, 0), title_text, font=title_font)[3]

    width = grid_left + grid_width + padding
    height = grid_top + grid_height + padding + title_height
    img = Image.new("RGB", (width, height), style["background_color"])
    draw = ImageDraw.Draw(img)

    title_y = padding
    draw.text((padding, title_y), title_text, font=title_font, fill=style["title_color"])

    header_y = title_y + title_height + 12
    for idx, label in enumerate(day_labels):
        x = grid_left + idx * day_col_width + day_col_width / 2
        label_width = draw.textbbox((0, 0), label, font=header_font)[2]
        draw.text(
            (x - label_width / 2, header_y),
            label,
            font=header_font,
            fill=style["header_text_color"],
        )

    grid_top = header_y + header_height - 6

    for day_idx in range(len(days) + 1):
        x = grid_left + day_idx * day_col_width
        draw.line((x, grid_top, x, grid_top + grid_height), fill=style["grid_line_color"], width=1)

    for minute_offset in range(0, total_minutes + 1, 60):
        y = grid_top + minute_offset * minute_height
        draw.line((grid_left, y, grid_left + grid_width, y), fill=style["hour_line_color"], width=1)
        time_minutes = work_start_minutes + minute_offset
        hour = time_minutes // 60
        minute = time_minutes % 60
        label = f"{hour:02d}:{minute:02d}"
        label_width = draw.textbbox((0, 0), label, font=time_font)[2]
        draw.text(
            (grid_left - 12 - label_width, y - 10),
            label,
            font=time_font,
            fill=style["time_text_color"],
        )

    def block_colors(kind: str, status: AppointmentStatus | None) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        if kind == "break":
            colors = style["appointment_colors"]["break"]
            return colors["fill"], colors["outline"]
        if status == AppointmentStatus.Booked:
            colors = style["appointment_colors"]["booked"]
            return colors["fill"], colors["outline"]
        colors = style["appointment_colors"]["hold"]
        return colors["fill"], colors["outline"]

    line_height = draw.textbbox((0, 0), "Ag", font=appt_font)[3] + 2

    break_items = breaks or []
    for kind, item in (
        [("appointment", appt) for appt in appts]
        + [("break", br) for br in break_items]
    ):
        if kind == "break":
            local_start = item.start_dt.astimezone(settings.tz)
            local_end = item.end_dt.astimezone(settings.tz)
            label_lines = [item.reason or "Перерыв"]
            status = None
        else:
            local_start = item.start_dt.astimezone(settings.tz)
            local_end = item.end_dt.astimezone(settings.tz)
            client_label = item.client.full_name or (f"@{item.client.username}" if item.client.username else str(item.client.tg_id))
            service_label = appointment_services_label(item)
            label_lines = [client_label]
            if service_label:
                label_lines.append(service_label)
            status = item.status

        day_offset = (local_start.date() - day).days
        if day_offset != 0:
            continue
        start_min = local_start.hour * 60 + local_start.minute - work_start_minutes
        end_min = local_end.hour * 60 + local_end.minute - work_start_minutes
        if end_min <= 0 or start_min >= total_minutes:
            continue
        start_min = max(start_min, 0)
        end_min = min(end_min, total_minutes)

        x0 = grid_left + day_offset * day_col_width + 6
        x1 = x0 + day_col_width - 12
        y0 = grid_top + start_min * minute_height + 2
        y1 = grid_top + end_min * minute_height - 2
        if y1 - y0 < style["appointment_min_height"]:
            y1 = y0 + style["appointment_min_height"]

        fill, outline = block_colors(kind, status)
        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=style["appointment_corner_radius"],
            fill=fill,
            outline=outline,
            width=style["appointment_outline_width"],
        )

        max_text_width = int(x1 - x0 - style["appointment_text_padding_x"] * 2)
        text_lines: list[str] = []
        for label in label_lines:
            if label:
                text_lines += _wrap_text_lines(label, draw, appt_font, max_text_width)
        max_lines = max(
            int((y1 - y0 - style["appointment_text_padding_y"] * 2) / line_height),
            0,
        )
        if max_lines:
            text_lines = text_lines[:max_lines]
            text_y = y0 + style["appointment_text_padding_y"]
            for line in text_lines:
                draw.text(
                    (x0 + style["appointment_text_padding_x"], text_y),
                    line,
                    font=appt_font,
                    fill=style["appointment_text_color"],
                )
                text_y += line_height

    buffer = BytesIO()
    buffer.name = "day_schedule.png"
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

async def admin_day_view(update: Update, context: ContextTypes.DEFAULT_TYPE, offset_days: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            day = (datetime.now(tz=settings.tz) + timedelta(days=offset_days)).date()
            appts = await admin_list_appointments_for_day(s, settings.tz, day)
            start_local = settings.tz.localize(datetime.combine(day, datetime.min.time()))
            end_local = start_local + timedelta(days=1)
            breaks = await list_future_breaks(
                s,
                start_local.astimezone(pytz.UTC),
                end_local.astimezone(pytz.UTC),
            )

    lines = [f"📅 Записи на {day.strftime('%d.%m')} ({RU_WEEKDAYS[day.weekday()]}):"]
    if not appts:
        lines.append("• Записей нет.")
    else:
        for a in appts:
            start_t = a.start_dt.astimezone(settings.tz).strftime("%H:%M")
            end_t = a.end_dt.astimezone(settings.tz).strftime("%H:%M")
            client = a.client.full_name or (f"@{a.client.username}" if a.client.username else str(a.client.tg_id))
            phone = a.client.phone or "—"
            price = format_price(a.price_override if a.price_override is not None else a.service.price)
            service_label = appointment_services_label(a)
            lines.append(
                f"• {start_t}–{end_t} | {status_ru(a.status.value)} | {service_label} | {price} | {client} | {phone}"
            )

    if breaks:
        lines.append("• Перерывы:")
        for b in breaks:
            start_t = b.start_dt.astimezone(settings.tz).strftime("%H:%M")
            end_t = b.end_dt.astimezone(settings.tz).strftime("%H:%M")
            reason = b.reason or "Перерыв"
            lines.append(f"  - {start_t}–{end_t} | {reason}")

    await update.message.reply_text("\n".join(lines), reply_markup=admin_menu_kb())
    if getattr(cfg, "schedule_visualization", 1) == 2:
        timeline_image = _build_single_day_schedule_image(day, settings, appts, breaks)
    else:
        timeline_image = _build_day_timeline_image(day, settings, appts, breaks)
    await update.message.reply_photo(
        photo=timeline_image,
        caption="🧭 График слотов",
        reply_markup=admin_menu_kb(),
    )
    for a in appts:
        if a.status == AppointmentStatus.Booked:
            start_t = a.start_dt.astimezone(settings.tz).strftime("%H:%M")
            await update.message.reply_text(
                f"Запись • {start_t} • {appointment_services_label(a)}",
                reply_markup=admin_manage_appt_kb(a.id, allow_reschedule=_is_admin_created(a)),
            )

async def admin_week_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            start_day = datetime.now(tz=settings.tz).date()
            start_local = settings.tz.localize(datetime.combine(start_day, datetime.min.time()))
            end_local = start_local + timedelta(days=7)
            appts = await admin_list_appointments_range(
                s,
                start_local.astimezone(pytz.UTC),
                end_local.astimezone(pytz.UTC),
            )
            breaks = await list_future_breaks(
                s,
                start_local.astimezone(pytz.UTC),
                end_local.astimezone(pytz.UTC),
            )

    week_image = _build_week_schedule_image(start_day, settings, appts, breaks)
    await update.message.reply_photo(
        photo=week_image,
        caption="📆 Записи на неделю",
        reply_markup=admin_menu_kb(),
    )

async def admin_booked_month_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            settings = await get_settings(s, cfg.timezone)
            await _sync_break_rules(s, settings)
            now_local = datetime.now(tz=settings.tz)
            end_local = now_local + timedelta(days=30)
            appts = await admin_list_appointments_range(
                s,
                now_local.astimezone(pytz.UTC),
                end_local.astimezone(pytz.UTC),
            )

    lines = ["🗓 Все записи на месяц вперёд:"]
    if not appts:
        lines.append("• Записей нет.")
    else:
        for a in appts:
            local_dt = a.start_dt.astimezone(settings.tz)
            end_dt = a.end_dt.astimezone(settings.tz)
            day_label = f"{local_dt.strftime('%d.%m')} ({RU_WEEKDAYS[local_dt.weekday()]})"
            client = a.client.full_name or (f"@{a.client.username}" if a.client.username else str(a.client.tg_id))
            phone = a.client.phone or "—"
            price = format_price(a.price_override if a.price_override is not None else a.service.price)
            service_label = appointment_services_label(a)
            status_label = status_ru(a.status.value)
            lines.append(
                f"• {day_label} {local_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')} | "
                f"{status_label} | {service_label} | {price} | {client} | {phone}"
            )

    await update.message.reply_text("\n".join(lines), reply_markup=admin_menu_kb())

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        start_day = datetime.now(tz=settings.tz).date()
        for week_index in range(4):
            week_start = start_day + timedelta(days=7 * week_index)
            week_start_local = settings.tz.localize(datetime.combine(week_start, datetime.min.time()))
            week_end_local = week_start_local + timedelta(days=7)
            appts = await admin_list_appointments_range(
                s,
                week_start_local.astimezone(pytz.UTC),
                week_end_local.astimezone(pytz.UTC),
            )
            breaks = await list_future_breaks(
                s,
                week_start_local.astimezone(pytz.UTC),
                week_end_local.astimezone(pytz.UTC),
            )
            week_image = _build_week_schedule_image(week_start, settings, appts, breaks)
            week_end = week_start + timedelta(days=6)
            caption = f"📆 Неделя {week_index + 1} • {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')}"
            await update.message.reply_photo(
                photo=week_image,
                caption=caption,
                reply_markup=admin_menu_kb(),
            )

async def admin_cancel_break_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    context.user_data[K_BREAK_CANCEL_IDS] = []
    _, items = await _load_break_cancel_items(context)

    if not items:
        return await update.message.reply_text("Перерывы не найдены.", reply_markup=admin_menu_kb())

    await update.message.reply_text(
        "Выберите перерывы для отмены.\nВыбрано: 0",
        reply_markup=cancel_breaks_kb(items, set()),
    )

async def admin_cancel_break(update: Update, context: ContextTypes.DEFAULT_TYPE, block_id: int):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.callback_query.message.edit_text("Нет доступа.")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        async with s.begin():
            ok = await delete_blocked_interval(s, block_id)

    if not ok:
        return await update.callback_query.message.edit_text("Перерыв уже отменён или не найден.")

    await update.callback_query.message.edit_text("Перерыв отменён ✅")
    await update.callback_query.message.reply_text("Админ-панель 👇", reply_markup=admin_menu_kb())


async def admin_holds_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg: Config = context.bot_data["cfg"]
    if not is_admin(cfg, update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    session_factory = context.bot_data["session_factory"]
    async with session_factory() as s:
        settings = await get_settings(s, cfg.timezone)
        holds = await admin_list_holds(s)

    if not holds:
        return await update.message.reply_text("HOLD-заявок нет.", reply_markup=admin_menu_kb())

    lines = ["🧾 HOLD-заявки:"]
    for a in holds:
        t = a.start_dt.astimezone(settings.tz).strftime("%d.%m %H:%M")
        exp = a.hold_expires_at.astimezone(settings.tz).strftime("%H:%M") if a.hold_expires_at else "—"
        client = a.client.full_name or (f"@{a.client.username}" if a.client.username else str(a.client.tg_id))
        lines.append(f"• {t} | #{a.id} | {appointment_services_label(a)} | {client} | hold до {exp}")

    await update.message.reply_text("\n".join(lines), reply_markup=admin_menu_kb())

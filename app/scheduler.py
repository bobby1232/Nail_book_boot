from datetime import datetime
import pytz

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Appointment, AppointmentStatus
from app.utils import appointment_services_label


async def tick(application):
    """
    Автоматически сжигает истёкшие HOLD-заявки
    и уведомляет клиента ОДИН РАЗ.
    """

    session_factory = application.bot_data["session_factory"]
    now_utc = datetime.now(tz=pytz.UTC)

    # сюда собираем уведомления ПОКА сессия жива
    notifications: list[tuple[int, str]] = []

    async with session_factory() as s:  # type: AsyncSession
        # 1️⃣ Берём ТОЛЬКО истёкшие HOLD
        res = await s.execute(
            select(Appointment)
            .options(
                selectinload(Appointment.client),
                selectinload(Appointment.service),
            )
            .where(
                and_(
                    Appointment.status == AppointmentStatus.Hold,
                    Appointment.hold_expires_at.is_not(None),
                    Appointment.hold_expires_at <= now_utc,
                )
            )
        )

        expired = res.scalars().all()
        if not expired:
            return  # Нечего сжигать → не шлём сообщений

        # 2️⃣ Обновляем статус + готовим сообщения
        for appt in expired:
            appt.status = AppointmentStatus.Rejected
            appt.updated_at = now_utc

            chat_id = appt.client.tg_id
            service_name = appointment_services_label(appt)
            dt_txt = appt.start_dt.astimezone(pytz.UTC).strftime("%d.%m %H:%M")

            notifications.append(
                (
                    chat_id,
                    (
                        "⏳ Заявка не была подтверждена мастером и автоматически отменена.\n\n"
                        f"Услуга: {service_name}\n"
                        f"Дата/время: {dt_txt}\n\n"
                        "Вы можете выбрать другое время в меню «Записаться»."
                    ),
                )
            )

        # 3️⃣ Фиксируем изменения В БАЗЕ
        await s.commit()

    # 4️⃣ Отправляем уведомления ПОСЛЕ commit (важно!)
    for chat_id, text in notifications:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            # логируем, но не валим тик
            print("TICK NOTIFY ERROR:", e)

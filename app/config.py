from dataclasses import dataclass
import os

from texts import get_brand_name

@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_telegram_id: int
    admin_telegram_ids: tuple[int, ...]
    database_url: str
    timezone: str
    name: str

    webhook_url: str | None
    port: int
    schedule_visualization: int
    hidden_service_categories: tuple[str, ...]

    # Seed defaults (only used on first DB init)
    slot_step_min: int
    buffer_min: int
    min_lead_time_min: int
    booking_horizon_days: int
    hold_ttl_min: int
    cancel_limit_hours: int
    work_start: str
    work_end: str
    work_days: str

def _get_int(name: str, default: int) -> int:
    v = os.getenv(name, str(default)).strip()
    return int(v)

def _parse_admin_ids(raw_value: str) -> tuple[int, ...]:
    raw = (raw_value or "").strip()
    if not raw:
        return tuple()
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    ids: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError("ADMIN_TELEGRAM_ID must contain only numeric IDs") from exc
        if value:
            ids.append(value)
    return tuple(ids)


def _parse_categories(raw_value: str) -> tuple[str, ...]:
    raw = (raw_value or "").strip()
    if not raw:
        return tuple()
    categories: list[str] = []
    for part in raw.replace(";", ",").split(","):
        value = part.strip().lower()
        if value and value not in categories:
            categories.append(value)
    return tuple(categories)

def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_raw = os.getenv("ADMIN_TELEGRAM_IDS", "").strip() or os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    db_url = os.getenv("DATABASE_URL", "").strip()
    admin_ids = _parse_admin_ids(admin_raw)
    if not bot_token or not admin_ids or not db_url:
        raise RuntimeError("Missing BOT_TOKEN / ADMIN_TELEGRAM_ID / DATABASE_URL")

    webhook_url = os.getenv("WEBHOOK_URL", "").strip() or None
    port = _get_int("PORT", 8080)
    schedule_visualization = _get_int("SCHEDULE_VISUALIZATION", 1)
    hidden_service_categories = _parse_categories(os.getenv("HIDDEN_SERVICE_CATEGORIES", ""))

    return Config(
        bot_token=bot_token,
        admin_telegram_id=admin_ids[0],
        admin_telegram_ids=admin_ids,
        database_url=db_url,
        timezone=os.getenv("TIMEZONE", "Europe/Amsterdam").strip(),
        name=os.getenv("NAME", "").strip() or get_brand_name("Найра"),

        webhook_url=webhook_url,
        port=port,
        schedule_visualization=schedule_visualization,
        hidden_service_categories=hidden_service_categories,

        slot_step_min=_get_int("SLOT_STEP_MIN", 30),
        buffer_min=_get_int("BUFFER_MIN", 10),
        min_lead_time_min=_get_int("MIN_LEAD_TIME_MIN", 0),
        booking_horizon_days=_get_int("BOOKING_HORIZON_DAYS", 30),
        hold_ttl_min=_get_int("HOLD_TTL_MIN", 720),
        cancel_limit_hours=_get_int("CANCEL_LIMIT_HOURS", 2),
        work_start=os.getenv("WORK_START", "10:00").strip(),
        work_end=os.getenv("WORK_END", "19:00").strip(),
        work_days=os.getenv("WORK_DAYS", "0,1,2,3,4,5,6").strip(),  # 0=Mon
    )

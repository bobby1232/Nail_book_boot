import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Defaults:
    # Baseline config (зафиксированные дефолты)
    WORK_DAYS = [0, 1, 2, 3, 4, 5, 6]   # Mon-Sun
    WORK_START = "10:00"
    WORK_END = "19:00"

    SLOT_STEP_MIN = 30
    BUFFER_MIN = 10

    MIN_LEAD_TIME_MIN = 120
    BOOKING_HORIZON_DAYS = 30

    HOLD_TTL_MIN = 720
    CANCEL_LIMIT_HOURS = 12

    REMINDER_24H = True
    REMINDER_2H = True


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_tg_id: int
    database_url: str
    timezone: str

    mode: str
    webhook_url: str | None
    port: int


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    admin_tg_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    database_url = os.getenv("DATABASE_URL", "").strip()
    timezone = os.getenv("TIMEZONE", "Europe/Amsterdam").strip()

    mode = os.getenv("MODE", "polling").strip().lower()
    webhook_url = os.getenv("WEBHOOK_URL", "").strip() or None
    port = int(os.getenv("PORT", "8080"))

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if not admin_tg_id:
        raise RuntimeError("ADMIN_TELEGRAM_ID is required")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    if mode not in ("polling", "webhook"):
        raise RuntimeError("MODE must be polling|webhook")

    if mode == "webhook" and not webhook_url:
        raise RuntimeError("WEBHOOK_URL is required for webhook mode")

    return Config(
        bot_token=bot_token,
        admin_tg_id=admin_tg_id,
        database_url=database_url,
        timezone=timezone,
        mode=mode,
        webhook_url=webhook_url,
        port=port,
    )

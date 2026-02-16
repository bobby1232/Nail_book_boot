from __future__ import annotations

from decimal import Decimal, InvalidOperation


CATEGORY_TITLES = {
    "sugar": "Шугаринг",
    "laser": "Лазер",
    "sugar_m": "Шугаринг(муж)",
    "laser_m": "Лазерная эпиляция(муж)",
    "wax": "Полимерный воск(жен)",
    "wax_premium": "Спа-Премиум по маслу(жен)",
    "cosmeology": "Косметология(эстетическая)",
    "massage": "Массаж",
    "nails": "Ногтевой сервис",
    "brows": "Брови/Ресницы",
    "hair": "Парикмахерские услуги(жен)",
    "hair_m": "Парикмахерские услуги(муж)",
}


CATEGORY_ORDER = (
    "sugar",
    "laser",
    "sugar_m",
    "laser_m",
    "wax",
    "wax_premium",
    "cosmeology",
    "massage",
    "nails",
    "brows",
    "hair",
    "hair_m",
)


_SERVICE_NAME_PREFIXES = (
    "Шугаринг:",
    "Лазерная эпиляция:",
    "Лазер:",
    "Шугаринг(муж):",
    "Лазерная эпиляция(муж):",
    "Полимерный воск(жен):",
    "Спа-Премиум по маслу(жен):",
    "Косметология(эстетическая):",
    "Массаж:",
    "Ногтевой сервис:",
    "Брови/Ресницы:",
    "Парикмахерские услуги(жен):",
    "Парикмахерские услуги(муж):",
)


def format_price(value: object) -> str:
    if value is None:
        return ""
    try:
        normalized = f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return str(value)
    return normalized.rstrip("0").rstrip(".")


def appointment_services_label(appt) -> str:
    comment = (getattr(appt, "admin_comment", None) or "").strip()
    if comment.lower().startswith("услуги:"):
        label = comment.split(":", 1)[1].strip()
        if label:
            return label
    service = getattr(appt, "service", None)
    if service and getattr(service, "name", None):
        return service_label_with_category(service)
    return "Услуга"


def service_category_title(category: str | None) -> str:
    if not category:
        return "Услуги"
    return CATEGORY_TITLES.get(category, category)


def service_label_with_category(service) -> str:
    name = (getattr(service, "name", None) or "").strip()
    for prefix in _SERVICE_NAME_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            break
    if name:
        return name
    category = service_category_title(getattr(service, "category", None))
    return category if category else "Услуга"


def services_label_with_category(services: list) -> str:
    return ", ".join(service_label_with_category(s) for s in services)

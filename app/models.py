import enum
from datetime import datetime, date, time
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, Time,
    UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class AppointmentStatus(str, enum.Enum):
    Hold = "Hold"
    Booked = "Booked"
    Rejected = "Rejected"
    Canceled = "Canceled"
    Completed = "Completed"

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="sugar")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)

class BlockedInterval(Base):
    __tablename__ = "blocked_intervals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_admin: Mapped[int] = mapped_column(BigInteger, nullable=False)

class BreakRule(Base):
    __tablename__ = "break_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repeat: Mapped[str] = mapped_column(String(16), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_generated_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_admin: Mapped[int] = mapped_column(BigInteger, nullable=False)

class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_status_start", "status", "start_dt"),
        UniqueConstraint("id", name="uq_appointments_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)

    start_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus), nullable=False)

    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_override: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)

    proposed_alt_start_dt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visit_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    client: Mapped["User"] = relationship("User")
    service: Mapped["Service"] = relationship("Service")

import asyncpg
from datetime import datetime, timedelta
from config import Defaults

APPT_HOLD = "Hold"
APPT_BOOKED = "Booked"
APPT_REJECTED = "Rejected"
APPT_CANCELED = "Canceled"
APPT_COMPLETED = "Completed"

class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def init_schema(self):
        assert self.pool
        async with self.pool.acquire() as con:
            # extensions for exclusion constraints
            await con.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")

            # Core tables
            await con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS services (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                price INT NOT NULL,
                duration_min INT NOT NULL,
                buffer_min INT NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INT NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS working_hours (
                id BIGSERIAL PRIMARY KEY,
                weekday INT NOT NULL, -- 0=Mon
                start_time TIME NOT NULL,
                end_time TIME NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );

            CREATE TABLE IF NOT EXISTS blocked_intervals (
                id BIGSERIAL PRIMARY KEY,
                start_dt TIMESTAMPTZ NOT NULL,
                end_dt TIMESTAMPTZ NOT NULL,
                reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            -- Appointments
            CREATE TABLE IF NOT EXISTS appointments (
                id BIGSERIAL PRIMARY KEY,
                client_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                service_id BIGINT NOT NULL REFERENCES services(id),
                start_dt TIMESTAMPTZ NOT NULL,
                end_dt TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                hold_expires_at TIMESTAMPTZ,
                client_comment TEXT,
                admin_comment TEXT,
                proposed_alt_start_dt TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                canceled_at TIMESTAMPTZ,
                cancel_reason TEXT,
                reminder_24_sent BOOLEAN NOT NULL DEFAULT FALSE,
                reminder_2_sent BOOLEAN NOT NULL DEFAULT FALSE,
                visit_confirmed BOOLEAN NOT NULL DEFAULT FALSE
            );

            -- Generated range for exclusion constraint
            ALTER TABLE appointments
            ADD COLUMN IF NOT EXISTS period tstzrange
            GENERATED ALWAYS AS (tstzrange(start_dt, end_dt, '[)')) STORED;

            ALTER TABLE blocked_intervals
            ADD COLUMN IF NOT EXISTS period tstzrange
            GENERATED ALWAYS AS (tstzrange(start_dt, end_dt, '[)')) STORED;

            -- Prevent overlapping active appointments (Hold/Booked)
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'appointments_no_overlap_active'
              ) THEN
                ALTER TABLE appointments
                ADD CONSTRAINT appointments_no_overlap_active
                EXCLUDE USING gist (period WITH &&)
                WHERE (status IN ('Hold','Booked'));
              END IF;
            END$$;

            -- Prevent overlapping blocked intervals
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'blocked_no_overlap'
              ) THEN
                ALTER TABLE blocked_intervals
                ADD CONSTRAINT blocked_no_overlap
                EXCLUDE USING gist (period WITH &&);
              END IF;
            END$$;

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """)

            await self._seed_defaults(con)

    async def _seed_defaults(self, con: asyncpg.Connection):
        # settings (upsert)
        defaults = {
            "slot_step_min": str(Defaults.SLOT_STEP_MIN),
            "buffer_min": str(Defaults.BUFFER_MIN),
            "min_lead_time_min": str(Defaults.MIN_LEAD_TIME_MIN),
            "booking_horizon_days": str(Defaults.BOOKING_HORIZON_DAYS),
            "hold_ttl_min": str(Defaults.HOLD_TTL_MIN),
            "cancel_limit_hours": str(Defaults.CANCEL_LIMIT_HOURS),
            "work_start": Defaults.WORK_START,
            "work_end": Defaults.WORK_END,
            "work_days": ",".join(map(str, Defaults.WORK_DAYS)),
        }
        for k, v in defaults.items():
            await con.execute("""
                INSERT INTO settings(key, value) VALUES($1, $2)
                ON CONFLICT (key) DO NOTHING;
            """, k, v)

        # working_hours (seed if empty)
        count = await con.fetchval("SELECT COUNT(*) FROM working_hours;")
        if count == 0:
            for wd in Defaults.WORK_DAYS:
                await con.execute("""
                    INSERT INTO working_hours(weekday, start_time, end_time, is_active)
                    VALUES($1, $2::time, $3::time, TRUE);
                """, wd, Defaults.WORK_START, Defaults.WORK_END)

        # services seed (минимальный набор, чтобы бот работал “из коробки”)
        svc_count = await con.fetchval("SELECT COUNT(*) FROM services;")
        if svc_count == 0:
            seed = [
                ("Подмышки", 1500, 30),
                ("Бикини классика", 2500, 45),
                ("Глубокое бикини", 3500, 60),
                ("Голени", 2500, 45),
                ("Ноги полностью", 4500, 75),
            ]
            for i, (name, price, dur) in enumerate(seed, start=1):
                await con.execute("""
                    INSERT INTO services(name, price, duration_min, buffer_min, is_active, sort_order)
                    VALUES($1, $2, $3, $4, TRUE, $5);
                """, name, price, dur, Defaults.BUFFER_MIN, i)

    # ---------- helpers ----------
    async def upsert_user(self, tg_id: int, username: str | None, full_name: str | None) -> int:
        assert self.pool
        async with self.pool.acquire() as con:
            row = await con.fetchrow("""
                INSERT INTO users(tg_id, username, full_name, last_seen_at)
                VALUES($1, $2, $3, now())
                ON CONFLICT(tg_id) DO UPDATE
                SET username=EXCLUDED.username,
                    full_name=EXCLUDED.full_name,
                    last_seen_at=now()
                RETURNING id;
            """, tg_id, username, full_name)
            return int(row["id"])

    async def set_user_phone(self, tg_id: int, phone: str):
        assert self.pool
        async with self.pool.acquire() as con:
            await con.execute("UPDATE users SET phone=$1 WHERE tg_id=$2;", phone, tg_id)

    async def get_services(self):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetch("""
                SELECT id, name, price, duration_min, buffer_min
                FROM services
                WHERE is_active=TRUE
                ORDER BY sort_order, id;
            """)

    async def get_service(self, service_id: int):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetchrow("""
                SELECT id, name, price, duration_min, buffer_min
                FROM services
                WHERE id=$1 AND is_active=TRUE;
            """, service_id)

    async def get_setting_int(self, key: str, default: int) -> int:
        assert self.pool
        async with self.pool.acquire() as con:
            v = await con.fetchval("SELECT value FROM settings WHERE key=$1;", key)
            return int(v) if v is not None else default

    async def get_setting_str(self, key: str, default: str) -> str:
        assert self.pool
        async with self.pool.acquire() as con:
            v = await con.fetchval("SELECT value FROM settings WHERE key=$1;", key)
            return str(v) if v is not None else default

    async def list_blocked(self, start_dt: datetime, end_dt: datetime):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetch("""
                SELECT start_dt, end_dt
                FROM blocked_intervals
                WHERE period && tstzrange($1, $2, '[)');
            """, start_dt, end_dt)

    async def list_active_appointments(self, start_dt: datetime, end_dt: datetime):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetch("""
                SELECT start_dt, end_dt
                FROM appointments
                WHERE status IN ('Hold','Booked')
                  AND period && tstzrange($1, $2, '[)');
            """, start_dt, end_dt)

    async def create_hold(self, client_user_id: int, service_id: int, start_dt: datetime, end_dt: datetime,
                          hold_ttl_min: int, comment: str | None) -> int:
        """
        Атомарно создаём Hold.
        Защита от двойного бронирования: EXCLUDE constraint на period&& для Hold/Booked.
        """
        assert self.pool
        hold_expires_at = datetime.now(tz=start_dt.tzinfo) + timedelta(minutes=hold_ttl_min)
        async with self.pool.acquire() as con:
            async with con.transaction():
                try:
                    row = await con.fetchrow("""
                        INSERT INTO appointments(client_user_id, service_id, start_dt, end_dt, status, hold_expires_at, client_comment)
                        VALUES($1, $2, $3, $4, 'Hold', $5, $6)
                        RETURNING id;
                    """, client_user_id, service_id, start_dt, end_dt, hold_expires_at, comment)
                    return int(row["id"])
                except asyncpg.exceptions.ExclusionViolationError:
                    raise ValueError("SLOT_TAKEN")

    async def get_appointment(self, appt_id: int):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetchrow("""
                SELECT a.*, u.tg_id AS client_tg_id, u.username, u.full_name, u.phone,
                       s.name AS service_name, s.price, s.duration_min
                FROM appointments a
                JOIN users u ON u.id=a.client_user_id
                JOIN services s ON s.id=a.service_id
                WHERE a.id=$1;
            """, appt_id)

    async def admin_confirm(self, appt_id: int) -> bool:
        assert self.pool
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow("""
                    UPDATE appointments
                    SET status='Booked', updated_at=now()
                    WHERE id=$1 AND status='Hold'
                    RETURNING id;
                """, appt_id)
                return row is not None

    async def admin_reject(self, appt_id: int, reason: str | None = None) -> bool:
        assert self.pool
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow("""
                    UPDATE appointments
                    SET status='Rejected', updated_at=now(), admin_comment=$2
                    WHERE id=$1 AND status='Hold'
                    RETURNING id;
                """, appt_id, reason)
                return row is not None

    async def expire_holds(self) -> list[int]:
        """
        Возвращает список appt_id, которые автопереведены в Rejected.
        """
        assert self.pool
        async with self.pool.acquire() as con:
            rows = await con.fetch("""
                UPDATE appointments
                SET status='Rejected', updated_at=now(), admin_comment='Auto-expired'
                WHERE status='Hold' AND hold_expires_at IS NOT NULL AND hold_expires_at < now()
                RETURNING id;
            """)
            return [int(r["id"]) for r in rows]

    async def list_user_appointments(self, tg_id: int):
        assert self.pool
        async with self.pool.acquire() as con:
            return await con.fetch("""
                SELECT a.id, a.start_dt, a.end_dt, a.status, s.name AS service_name, s.price
                FROM appointments a
                JOIN users u ON u.id=a.client_user_id
                JOIN services s ON s.id=a.service_id
                WHERE u.tg_id=$1 AND a.status IN ('Hold','Booked')
                ORDER BY a.start_dt ASC;
            """, tg_id)

    async def cancel_by_user(self, tg_id: int, appt_id: int) -> str:
        """
        Возврат:
          OK | TOO_LATE | NOT_FOUND | BAD_STATUS
        """
        assert self.pool
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow("""
                    SELECT a.id, a.status, a.start_dt
                    FROM appointments a
                    JOIN users u ON u.id=a.client_user_id
                    WHERE a.id=$1 AND u.tg_id=$2;
                """, appt_id, tg_id)
                if not row:
                    return "NOT_FOUND"
                if row["status"] not in (APPT_HOLD, APPT_BOOKED):
                    return "BAD_STATUS"
                # Cancel rule handled outside (in app) because timezone / config
                await con.execute("""
                    UPDATE appointments
                    SET status='Canceled', updated_at=now(), canceled_at=now(), cancel_reason='Canceled by client'
                    WHERE id=$1 AND status IN ('Hold','Booked');
                """, appt_id)
                return "OK"

    async def mark_reminder_sent(self, appt_id: int, kind: str):
        assert self.pool
        col = "reminder_24_sent" if kind == "24" else "reminder_2_sent"
        async with self.pool.acquire() as con:
            await con.execute(f"""
                UPDATE appointments SET {col}=TRUE, updated_at=now()
                WHERE id=$1;
            """, appt_id)

    async def mark_visit_confirmed(self, appt_id: int) -> bool:
        assert self.pool
        async with self.pool.acquire() as con:
            row = await con.fetchrow("""
                UPDATE appointments
                SET visit_confirmed=TRUE, updated_at=now()
                WHERE id=$1 AND status='Booked'
                RETURNING id;
            """, appt_id)
            return row is not None

    async def list_due_reminders(self):
        """
        Возвращает appointments для отправки напоминаний (24ч и 2ч),
        не отправленные ранее.
        """
        assert self.pool
        async with self.pool.acquire() as con:
            rows = await con.fetch("""
                SELECT id, start_dt, client_user_id, reminder_24_sent, reminder_2_sent
                FROM appointments
                WHERE status='Booked'
                  AND start_dt > now()
                  AND start_dt < now() + interval '2 days';
            """)
            return rows

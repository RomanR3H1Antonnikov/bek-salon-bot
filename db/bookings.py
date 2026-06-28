import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .schema import get_conn, get_conn_immediate, SERVICES_SEED, SERVICES_BY_SLUG, MASTER_SLUG, MASTERS_SEED, sum_duration


class SlotTaken(ValueError):
    """Запрошенное время пересекается с существующей бронью."""

MSK = ZoneInfo("Europe/Moscow")


# ─── helpers ───────────────────────────────────────────────────────────────────

def _get_master_id(c: sqlite3.Cursor, slug: str) -> int:
    c.execute("SELECT id FROM masters WHERE slug = ?", (slug,))
    row = c.fetchone()
    if not row:
        raise ValueError(f"Мастер '{slug}' не найден в БД — запусти seed_db()")
    return row[0]


def _get_day_hours(
    c: sqlite3.Cursor, master_id: int, date: str
) -> tuple[str, str] | None:
    """Возвращает (open_time, close_time) МСК или None если выходной."""
    c.execute(
        "SELECT is_working, open_time, close_time FROM schedule_overrides "
        "WHERE master_id = ? AND date = ?",
        (master_id, date),
    )
    override = c.fetchone()
    if override is not None:
        if override[0] == 0:
            return None
        return override[1], override[2]

    weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
    c.execute(
        "SELECT open_time, close_time FROM working_hours "
        "WHERE master_id = ? AND weekday = ?",
        (master_id, weekday),
    )
    row = c.fetchone()
    return (row[0], row[1]) if row else None


def get_or_create_client(
    conn: sqlite3.Connection,
    *,
    telegram_id: int | None = None,
    name: str | None = None,
    phone: str | None = None,  # нормализован вызывающей стороной
) -> int:
    c = conn.cursor()

    if telegram_id:
        c.execute("SELECT id FROM clients WHERE telegram_id = ?", (telegram_id,))
        row = c.fetchone()
        if row:
            if name:
                c.execute("UPDATE clients SET name = ? WHERE id = ?", (name, row[0]))
            return row[0]

    if phone:
        c.execute("SELECT id FROM clients WHERE phone = ?", (phone,))
        row = c.fetchone()
        if row:
            if telegram_id:
                c.execute(
                    "UPDATE clients SET telegram_id = ?, name = COALESCE(?, name) WHERE id = ?",
                    (telegram_id, name, row[0]),
                )
            return row[0]

    c.execute(
        "INSERT INTO clients (telegram_id, name, phone) VALUES (?, ?, ?)",
        (telegram_id, name, phone),
    )
    return c.lastrowid


# ─── public API ────────────────────────────────────────────────────────────────

def create_booking(
    *,
    master_slug: str = MASTER_SLUG,
    date: str,           # YYYY-MM-DD МСК
    time_start: str,     # HH:MM МСК
    services: list[str], # список slug'ов услуг; длительность суммируется
    telegram_id: int | None = None,
    client_name: str | None = None,
    phone: str | None = None,  # уже нормализован вызывающей стороной (FastAPI)
    source: str = "bot",       # "bot" | "site"
    note: str | None = None,
) -> dict:
    """
    Единая функция брони для юзербота и FastAPI.
    Все проверки (выходной, рабочие часы, пересечения) внутри одной BEGIN IMMEDIATE транзакции.
    Raises SlotTaken при конфликте времени, ValueError при остальных ошибках валидации.
    """
    total_duration = sum_duration(services)  # ValueError если пусто или неизвестный slug

    start_dt  = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
    end_dt    = start_dt + timedelta(minutes=total_duration)
    start_iso = start_dt.strftime("%Y-%m-%d %H:%M")
    end_iso   = end_dt.strftime("%Y-%m-%d %H:%M")

    # BEGIN IMMEDIATE: эксклюзивная блокировка до SELECT — исключает двойные брони.
    conn = get_conn_immediate()
    try:
        c = conn.cursor()
        master_id = _get_master_id(c, master_slug)

        # 1. День не выходной + весь визит укладывается в рабочие часы
        hours = _get_day_hours(c, master_id, date)
        if hours is None:
            raise ValueError(f"{date} — выходной день")

        open_dt  = datetime.strptime(f"{date} {hours[0]}", "%Y-%m-%d %H:%M")
        close_dt = datetime.strptime(f"{date} {hours[1]}", "%Y-%m-%d %H:%M")

        if start_dt < open_dt or end_dt > close_dt:
            raise ValueError(
                f"Время {time_start}–{end_dt.strftime('%H:%M')} выходит за рамки "
                f"рабочего дня {hours[0]}–{hours[1]}"
            )

        # 2. Anti-race: пересечение с существующими бронями
        c.execute(
            """
            SELECT id FROM bookings
            WHERE master_id = ? AND status = 'booked'
              AND start_time < ? AND end_time > ?
            """,
            (master_id, end_iso, start_iso),
        )
        conflict = c.fetchone()
        if conflict:
            raise SlotTaken(
                f"Время {time_start} уже занято (пересечение с бронью #{conflict[0]})"
            )

        # 3. Клиент (дедуп telegram_id → phone → новый)
        client_id = get_or_create_client(
            conn, telegram_id=telegram_id, name=client_name, phone=phone
        )

        # 4. Вставка брони
        c.execute(
            """
            INSERT INTO bookings
                (master_id, client_id, start_time, end_time, status, source, note, master_notified)
            VALUES (?, ?, ?, ?, 'booked', ?, ?, ?)
            """,
            (master_id, client_id, start_iso, end_iso, source, note,
             1 if source == "bot" else 0),
        )
        booking_id = c.lastrowid

        # 5. Услуги: снимок цены на момент брони (M:N)
        total_price = 0
        for slug in services:
            c.execute(
                "SELECT id, price FROM services WHERE slug = ? AND active = 1", (slug,)
            )
            svc = c.fetchone()
            if not svc:
                raise ValueError(f"Услуга {slug!r} не найдена или отключена в БД")
            svc_id, price = svc
            c.execute(
                "INSERT INTO booking_services (booking_id, service_id, price) VALUES (?, ?, ?)",
                (booking_id, svc_id, price),
            )
            total_price += price

        conn.commit()
        return {
            "booking_id":   booking_id,
            "date":         date,
            "time_start":   time_start,
            "time_end":     end_dt.strftime("%H:%M"),
            "services":     services,
            "total_price":  total_price,
            "duration_min": total_duration,
            "source":       source,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_slot_available(
    master_slug: str, date: str, time_start: str, duration_min: int
) -> bool:
    """Проверяет доступность конкретного слота без записи."""
    try:
        start_dt = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=duration_min)
        start_iso = start_dt.strftime("%Y-%m-%d %H:%M")
        end_iso = end_dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return False

    conn = get_conn()
    try:
        c = conn.cursor()
        master_id = _get_master_id(c, master_slug)

        hours = _get_day_hours(c, master_id, date)
        if hours is None:
            return False

        open_dt  = datetime.strptime(f"{date} {hours[0]}", "%Y-%m-%d %H:%M")
        close_dt = datetime.strptime(f"{date} {hours[1]}", "%Y-%m-%d %H:%M")
        if start_dt < open_dt or end_dt > close_dt:
            return False

        c.execute(
            """
            SELECT 1 FROM bookings
            WHERE master_id = ? AND status = 'booked'
              AND start_time < ? AND end_time > ?
            LIMIT 1
            """,
            (master_id, end_iso, start_iso),
        )
        return c.fetchone() is None
    finally:
        conn.close()


def get_free_slots(
    master_slug: str, date: str, duration_min: int = 30, limit: int = 8
) -> list[str]:
    """Возвращает свободные стартовые времена HH:MM для заданной длительности."""
    conn = get_conn()
    try:
        c = conn.cursor()
        master_id = _get_master_id(c, master_slug)

        hours = _get_day_hours(c, master_id, date)
        if hours is None:
            return []

        open_dt  = datetime.strptime(f"{date} {hours[0]}", "%Y-%m-%d %H:%M")
        close_dt = datetime.strptime(f"{date} {hours[1]}", "%Y-%m-%d %H:%M")

        c.execute(
            """
            SELECT start_time, end_time FROM bookings
            WHERE master_id = ? AND status = 'booked'
              AND start_time >= ? AND start_time < ?
            ORDER BY start_time
            """,
            (master_id, f"{date} 00:00", f"{date} 23:59"),
        )
        booked = [(r[0], r[1]) for r in c.fetchall()]

        free: list[str] = []
        slot = open_dt
        while slot + timedelta(minutes=duration_min) <= close_dt:
            s_iso = slot.strftime("%Y-%m-%d %H:%M")
            e_iso = (slot + timedelta(minutes=duration_min)).strftime("%Y-%m-%d %H:%M")
            if not any(bs < e_iso and be > s_iso for bs, be in booked):
                free.append(slot.strftime("%H:%M"))
                if limit > 0 and len(free) >= limit:
                    break
            slot += timedelta(minutes=30)

        return free
    finally:
        conn.close()


def get_upcoming_reminders() -> list[dict]:
    """Записи, до которых < 1 ч и напоминание клиенту ещё не отправлено."""
    now  = datetime.now(MSK).replace(tzinfo=None)  # наивное МСК для сравнения с start_time
    soon = now + timedelta(hours=1)

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT b.id, b.start_time, c.telegram_id, c.name, s.name
            FROM bookings b
            JOIN clients          c  ON c.id  = b.client_id
            JOIN booking_services bs ON bs.booking_id = b.id
            JOIN services         s  ON s.id  = bs.service_id
            WHERE b.status = 'booked' AND b.reminded = 0
              AND datetime(b.start_time) >  ?
              AND datetime(b.start_time) <= ?
            """,
            (now.strftime("%Y-%m-%d %H:%M"), soon.strftime("%Y-%m-%d %H:%M")),
        )
        return [
            {"booking_id": r[0], "start_time": r[1],
             "telegram_id": r[2], "client_name": r[3], "service": r[4]}
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def mark_reminded(booking_id: int) -> None:
    conn = get_conn()
    try:
        with conn:
            conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
    finally:
        conn.close()


def get_pending_master_notifications() -> list[dict]:
    """Записи с сайта, о которых Бек ещё не уведомлён."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT b.id, b.start_time, b.end_time,
                   c.name, c.phone, c.telegram_id,
                   s.name as service, bs.price,
                   m.name as master_name, m.slug as master_slug
            FROM bookings b
            JOIN clients          c  ON c.id  = b.client_id
            JOIN booking_services bs ON bs.booking_id = b.id
            JOIN services         s  ON s.id  = bs.service_id
            JOIN masters          m  ON m.id  = b.master_id
            WHERE b.source = 'site'
              AND b.status = 'booked'
              AND b.master_notified = 0
            ORDER BY b.start_time
            """
        )
        return [
            {
                "booking_id":   r[0],
                "start_time":   r[1],
                "end_time":     r[2],
                "client_name":  r[3],
                "phone":        r[4],
                "telegram_id":  r[5],
                "service":      r[6],
                "price":        r[7],
                "master_name":  r[8],
                "master_slug":  r[9],
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def list_masters() -> list[dict]:
    """Список мастеров из БД (slug, name, role)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT slug, name, role FROM masters ORDER BY role DESC, name")
        return [{"slug": r[0], "name": r[1], "role": r[2]} for r in c.fetchall()]
    finally:
        conn.close()


def mark_master_notified(booking_id: int) -> None:
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                "UPDATE bookings SET master_notified = 1 WHERE id = ?", (booking_id,)
            )
    finally:
        conn.close()

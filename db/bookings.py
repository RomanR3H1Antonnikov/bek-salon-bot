import secrets
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .schema import get_conn, get_conn_immediate, SERVICES_SEED, SERVICES_BY_SLUG, MASTER_SLUG, MASTERS_SEED, sum_duration


class SlotTaken(ValueError):
    """Запрошенное время пересекается с существующей бронью."""
    code = "slot_taken"


class AlreadyCancelled(ValueError):
    """Бронь уже отменена."""
    code = "already_cancelled"


class TooLate(ValueError):
    """Действие невозможно из-за правила 15 минут."""
    code = "too_late"

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


def _check_permission(
    c: sqlite3.Cursor,
    *,
    booking_master_id: int,
    client_tg: int | None,
    client_phone: str | None,
    booking_manage_token: str | None,
    by_telegram_id: int | None,
    by_phone: str | None,
    by_master_slug: str | None,
    by_manage_token: str | None,
    is_owner: bool,
    action: str,
) -> None:
    """Raises ValueError если запрашивающий не имеет права на действие с бронью."""
    if is_owner:
        return
    if by_manage_token is not None:
        if booking_manage_token and booking_manage_token == by_manage_token:
            return
        raise ValueError("Неверный токен управления записью")
    if by_master_slug is not None:
        c.execute("SELECT id FROM masters WHERE slug = ?", (by_master_slug,))
        row = c.fetchone()
        if not row or row[0] != booking_master_id:
            raise ValueError(f"Нет прав {action} эту запись: принадлежит другому мастеру")
        return
    if by_telegram_id is not None:
        if client_tg != by_telegram_id:
            raise ValueError(f"Нет прав {action} эту запись")
        return
    if by_phone is not None:
        if client_phone != by_phone:
            raise ValueError(f"Нет прав {action} эту запись")
        return
    raise ValueError("Не указан идентификатор запрашивающего (requester)")


def _insert_booking(
    conn: sqlite3.Connection,
    c: sqlite3.Cursor,
    *,
    master_slug: str,
    date: str,
    time_start: str,
    services: list[str],
    telegram_id: int | None,
    client_name: str | None,
    phone: str | None,
    source: str,
    note: str | None,
) -> dict:
    """Вставляет бронь внутри УЖЕ ОТКРЫТОЙ BEGIN IMMEDIATE транзакции.
    Raises SlotTaken / ValueError. Не делает commit/rollback — это задача вызывающего."""
    total_duration = sum_duration(services)
    start_dt  = datetime.strptime(f"{date} {time_start}", "%Y-%m-%d %H:%M")
    end_dt    = start_dt + timedelta(minutes=total_duration)
    start_iso = start_dt.strftime("%Y-%m-%d %H:%M")
    end_iso   = end_dt.strftime("%Y-%m-%d %H:%M")

    master_id = _get_master_id(c, master_slug)

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

    client_id = get_or_create_client(
        conn, telegram_id=telegram_id, name=client_name, phone=phone
    )

    manage_token = secrets.token_urlsafe(16)

    # Считаем total_price до INSERT, чтобы персистировать снимок вместе с бронью
    total_price = 0
    svc_rows: list[tuple[int, int]] = []
    for slug in services:
        c.execute(
            "SELECT id, price FROM services WHERE slug = ? AND active = 1", (slug,)
        )
        svc = c.fetchone()
        if not svc:
            raise ValueError(f"Услуга {slug!r} не найдена или отключена в БД")
        svc_rows.append(svc)
        total_price += svc[1]

    c.execute(
        """
        INSERT INTO bookings
            (master_id, client_id, start_time, end_time, status, source, note,
             master_notified, manage_token, total_price)
        VALUES (?, ?, ?, ?, 'booked', ?, ?, ?, ?, ?)
        """,
        (master_id, client_id, start_iso, end_iso, source, note,
         1 if source == "bot" else 0, manage_token, total_price),
    )
    booking_id = c.lastrowid

    for svc_id, price in svc_rows:
        c.execute(
            "INSERT INTO booking_services (booking_id, service_id, price) VALUES (?, ?, ?)",
            (booking_id, svc_id, price),
        )

    return {
        "booking_id":    booking_id,
        "date":          date,
        "time_start":    time_start,
        "time_end":      end_dt.strftime("%H:%M"),
        "services":      services,
        "total_price":   total_price,
        "duration_min":  total_duration,
        "source":        source,
        "manage_token":  manage_token,
    }


# ─── public API ────────────────────────────────────────────────────────────────

def create_booking(
    *,
    master_slug: str = MASTER_SLUG,
    date: str,           # YYYY-MM-DD МСК
    time_start: str,     # HH:MM МСК
    services: list[str], # список slug'ов; длительность суммируется
    telegram_id: int | None = None,
    client_name: str | None = None,
    phone: str | None = None,  # уже нормализован вызывающей стороной (FastAPI)
    source: str = "bot",       # "bot" | "site"
    note: str | None = None,
) -> dict:
    """
    Единая функция брони для юзербота и FastAPI.
    Возвращает manage_token — клиент получает его для управления записью.
    Raises SlotTaken при конфликте, ValueError при остальных ошибках валидации.
    """
    sum_duration(services)  # ранняя валидация до захвата блокировки БД
    conn = get_conn_immediate()
    try:
        c = conn.cursor()
        result = _insert_booking(
            conn, c,
            master_slug=master_slug, date=date, time_start=time_start,
            services=services, telegram_id=telegram_id, client_name=client_name,
            phone=phone, source=source, note=note,
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_booking(
    booking_id: int,
    *,
    by_telegram_id: int | None = None,   # клиент-бот: идентифицирован по TG ID
    by_phone: str | None = None,          # клиент-сайт: идентифицирован по телефону
    by_manage_token: str | None = None,   # клиент: предъявил токен управления
    by_master_slug: str | None = None,    # персонал-мастер: только свои записи
    is_owner: bool = False,               # владелец (Бек): отменяет любую
) -> dict:
    """
    Помечает запись как 'cancelled'. История сохраняется (никакого DELETE).
    Слот освобождается автоматически (все запросы занятости фильтруют status='booked').

    Raises ValueError:
      - запись не найдена / уже отменена / статус не 'booked'
      - now(МСК) >= start_time - 15 мин
      - у запрашивающего нет прав
    """
    now_msk = datetime.now(MSK).replace(tzinfo=None)

    conn = get_conn_immediate()
    try:
        c = conn.cursor()

        c.execute(
            """
            SELECT b.start_time, b.status, b.master_id,
                   cl.telegram_id, cl.phone, b.manage_token
            FROM bookings b
            JOIN clients cl ON cl.id = b.client_id
            WHERE b.id = ?
            """,
            (booking_id,),
        )
        row = c.fetchone()
        if not row:
            raise ValueError(f"Запись #{booking_id} не найдена")
        start_iso, status, master_id, client_tg, client_phone, booking_token = row

        if status == "cancelled":
            raise AlreadyCancelled(f"Запись #{booking_id} уже отменена")
        if status != "booked":
            raise ValueError(f"Запись #{booking_id} имеет статус {status!r}, отмена невозможна")

        start_dt = datetime.strptime(start_iso, "%Y-%m-%d %H:%M")
        if now_msk >= start_dt - timedelta(minutes=15):
            raise TooLate(
                f"Отменить можно не позднее чем за 15 минут до записи "
                f"(запись в {start_dt.strftime('%H:%M')}, сейчас {now_msk.strftime('%H:%M')})"
            )

        _check_permission(
            c,
            booking_master_id=master_id,
            client_tg=client_tg,
            client_phone=client_phone,
            booking_manage_token=booking_token,
            by_telegram_id=by_telegram_id,
            by_phone=by_phone,
            by_manage_token=by_manage_token,
            by_master_slug=by_master_slug,
            is_owner=is_owner,
            action="отменить",
        )

        c.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        conn.commit()
        return get_booking_by_token(booking_token)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reschedule_booking(
    booking_id: int,
    *,
    new_master_slug: str,
    new_date: str,                        # YYYY-MM-DD МСК
    new_time: str,                        # HH:MM МСК
    new_services: list[str] | None = None,  # None → наследуем из старой брони
    by_telegram_id: int | None = None,
    by_phone: str | None = None,
    by_manage_token: str | None = None,
    by_master_slug: str | None = None,
    is_owner: bool = False,
) -> dict:
    """
    In-place перенос: UPDATE существующей строки, manage_token НЕ меняется.

    Anti-race: проверка занятости нового слота и UPDATE — внутри одного
    BEGIN IMMEDIATE. При SlotTaken или ValueError весь блок откатывается,
    старая запись остаётся нетронутой.

    Raises SlotTaken, AlreadyCancelled, TooLate, ValueError.
    Returns полный объект брони (как GET /booking/<token>).
    """
    now_msk = datetime.now(MSK).replace(tzinfo=None)

    conn = get_conn_immediate()
    try:
        c = conn.cursor()

        # ── 1. Загрузить старую бронь ─────────────────────────────────────────
        c.execute(
            """
            SELECT b.start_time, b.status, b.master_id,
                   cl.telegram_id, cl.phone, b.source, b.manage_token
            FROM bookings b
            JOIN clients cl ON cl.id = b.client_id
            WHERE b.id = ?
            """,
            (booking_id,),
        )
        row = c.fetchone()
        if not row:
            raise ValueError(f"Запись #{booking_id} не найдена")
        (start_iso, status, old_master_id,
         client_tg, client_phone, old_source, booking_token) = row

        if status == "cancelled":
            raise AlreadyCancelled(f"Запись #{booking_id} уже отменена, перенос невозможен")
        if status != "booked":
            raise ValueError(f"Запись #{booking_id} имеет статус {status!r}")

        # ── 2. Правило 15 минут (по старому времени) ──────────────────────────
        start_dt = datetime.strptime(start_iso, "%Y-%m-%d %H:%M")
        if now_msk >= start_dt - timedelta(minutes=15):
            raise TooLate(
                f"Перенести можно не позднее чем за 15 минут до записи "
                f"(запись в {start_dt.strftime('%H:%M')}, сейчас {now_msk.strftime('%H:%M')})"
            )

        # ── 3. Проверка прав (по старой записи) ──────────────────────────────
        _check_permission(
            c,
            booking_master_id=old_master_id,
            client_tg=client_tg,
            client_phone=client_phone,
            booking_manage_token=booking_token,
            by_telegram_id=by_telegram_id,
            by_phone=by_phone,
            by_manage_token=by_manage_token,
            by_master_slug=by_master_slug,
            is_owner=is_owner,
            action="перенести",
        )

        # ── 4. Услуги: наследуем из старой брони если не переданы явно ───────
        if new_services is None:
            c.execute(
                """
                SELECT s.slug FROM booking_services bs
                JOIN services s ON s.id = bs.service_id
                WHERE bs.booking_id = ?
                """,
                (booking_id,),
            )
            new_services = [r[0] for r in c.fetchall()]
        if not new_services:
            raise ValueError(f"Не удалось получить список услуг для переноса #{booking_id}")

        # ── 5. Вычислить новые времена ────────────────────────────────────────
        total_duration = sum_duration(new_services)
        new_start_dt  = datetime.strptime(f"{new_date} {new_time}", "%Y-%m-%d %H:%M")
        new_end_dt    = new_start_dt + timedelta(minutes=total_duration)
        new_start_iso = new_start_dt.strftime("%Y-%m-%d %H:%M")
        new_end_iso   = new_end_dt.strftime("%Y-%m-%d %H:%M")

        new_master_id = _get_master_id(c, new_master_slug)

        # ── 6. Проверить рабочие часы нового мастера ─────────────────────────
        hours = _get_day_hours(c, new_master_id, new_date)
        if hours is None:
            raise ValueError(f"{new_date} — выходной день")
        open_dt  = datetime.strptime(f"{new_date} {hours[0]}", "%Y-%m-%d %H:%M")
        close_dt = datetime.strptime(f"{new_date} {hours[1]}", "%Y-%m-%d %H:%M")
        if new_start_dt < open_dt or new_end_dt > close_dt:
            raise ValueError(
                f"Время {new_time}–{new_end_dt.strftime('%H:%M')} выходит за рамки "
                f"рабочего дня {hours[0]}–{hours[1]}"
            )

        # ── 7. Anti-race: занятость нового слота (текущую бронь исключаем) ───
        c.execute(
            """
            SELECT id FROM bookings
            WHERE  master_id = ? AND status = 'booked'
              AND  start_time < ? AND end_time > ?
              AND  id != ?
            """,
            (new_master_id, new_end_iso, new_start_iso, booking_id),
        )
        conflict = c.fetchone()
        if conflict:
            raise SlotTaken(
                f"Время {new_time} уже занято (пересечение с бронью #{conflict[0]})"
            )

        # ── 8. Пересчитать total_price и собрать svc_rows ────────────────────
        total_price = 0
        svc_rows: list[tuple[int, int]] = []
        for slug in new_services:
            c.execute(
                "SELECT id, price FROM services WHERE slug = ? AND active = 1", (slug,)
            )
            svc = c.fetchone()
            if not svc:
                raise ValueError(f"Услуга {slug!r} не найдена или отключена")
            svc_rows.append(svc)
            total_price += svc[1]

        # ── 9. In-place UPDATE — manage_token не трогаем ─────────────────────
        c.execute(
            """
            UPDATE bookings
               SET master_id   = ?,
                   start_time  = ?,
                   end_time    = ?,
                   total_price = ?,
                   note        = ?
             WHERE id = ?
            """,
            (new_master_id, new_start_iso, new_end_iso, total_price,
             f"Перенесено с {start_iso[:10]}", booking_id),
        )

        # ── 10. Обновить booking_services ─────────────────────────────────────
        c.execute("DELETE FROM booking_services WHERE booking_id = ?", (booking_id,))
        for svc_id, price in svc_rows:
            c.execute(
                "INSERT INTO booking_services (booking_id, service_id, price) VALUES (?, ?, ?)",
                (booking_id, svc_id, price),
            )

        conn.commit()
        return get_booking_by_token(booking_token)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_booking_by_token(token: str) -> dict | None:
    """Возвращает полную информацию о брони по manage_token, или None если не найдена."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT b.id, b.start_time, b.end_time, b.status,
                   m.slug, m.name,
                   cl.name, cl.phone
            FROM bookings b
            JOIN masters m  ON m.id  = b.master_id
            JOIN clients cl ON cl.id = b.client_id
            WHERE b.manage_token = ?
            """,
            (token,),
        )
        row = c.fetchone()
        if not row:
            return None
        booking_id, start_iso, end_iso, status, master_slug, master_name, client_name, client_phone = row

        c.execute(
            """
            SELECT s.slug, s.name, s.duration_min, bs.price
            FROM booking_services bs
            JOIN services s ON s.id = bs.service_id
            WHERE bs.booking_id = ?
            """,
            (booking_id,),
        )
        svcs = c.fetchall()

        now_msk = datetime.now(MSK).replace(tzinfo=None)
        start_dt = datetime.strptime(start_iso, "%Y-%m-%d %H:%M")
        end_dt   = datetime.strptime(end_iso,   "%Y-%m-%d %H:%M")

        # Граничные случаи:
        # - past:      end_dt <= now  (не >=, чтобы не перекрываться с active)
        # - active:    status='booked' AND end_dt > now
        # - cancelled: status='cancelled' (независимо от времени)
        # can_cancel:  строго < (start - 15мин), не <=, чтобы совпадало с cancel_booking
        if status == "cancelled":
            computed_status = "cancelled"
            can_cancel = False
        elif end_dt <= now_msk:
            computed_status = "past"
            can_cancel = False
        else:
            computed_status = "active"
            can_cancel = now_msk < start_dt - timedelta(minutes=15)

        return {
            "booking_id":  booking_id,
            "master":      {"slug": master_slug, "name": master_name},
            "name":        client_name,
            "phone":       client_phone or "",
            "services":    [
                {"slug": r[0], "name": r[1], "duration_min": r[2], "price": r[3]}
                for r in svcs
            ],
            "date":        start_dt.strftime("%Y-%m-%d"),
            "time_start":  start_dt.strftime("%H:%M"),
            "time_end":    end_dt.strftime("%H:%M"),
            "total_price": sum(r[3] for r in svcs),
            "status":      computed_status,
            "can_cancel":  can_cancel,
        }
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
    now  = datetime.now(MSK).replace(tzinfo=None)
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


def mark_master_notified(booking_id: int) -> None:
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                "UPDATE bookings SET master_notified = 1 WHERE id = ?", (booking_id,)
            )
    finally:
        conn.close()


def _check_payment_permission(
    c: sqlite3.Cursor,
    *,
    booking_master_id: int,
    by_master_slug: str | None,
    is_owner: bool,
) -> None:
    """Только персонал может отмечать оплату. Raises ValueError если нет прав."""
    if is_owner:
        return
    if by_master_slug is not None:
        c.execute("SELECT id FROM masters WHERE slug = ?", (by_master_slug,))
        row = c.fetchone()
        if not row or row[0] != booking_master_id:
            raise ValueError(
                "Нет прав отметить оплату для этой записи: принадлежит другому мастеру"
            )
        return
    raise ValueError("Не указан идентификатор персонала для отметки оплаты")


def mark_paid(
    booking_id: int,
    *,
    by_master_slug: str | None = None,
    is_owner: bool = False,
) -> dict:
    """
    Отмечает запись как оплаченную. Идемпотентно: повторный вызов не трогает paid_at.
    Raises ValueError если status != 'booked' или нет прав.
    """
    conn = get_conn_immediate()
    try:
        c = conn.cursor()
        c.execute("SELECT status, master_id FROM bookings WHERE id = ?", (booking_id,))
        row = c.fetchone()
        if not row:
            raise ValueError(f"Запись #{booking_id} не найдена")
        status, master_id = row

        if status != "booked":
            raise ValueError(
                f"Нельзя отметить оплату: запись #{booking_id} имеет статус {status!r}"
            )

        _check_payment_permission(
            c, booking_master_id=master_id, by_master_slug=by_master_slug, is_owner=is_owner
        )

        paid_at_new = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
        # COALESCE сохраняет оригинальную метку времени при повторном вызове
        c.execute(
            "UPDATE bookings SET paid = 1, paid_at = COALESCE(paid_at, ?) WHERE id = ?",
            (paid_at_new, booking_id),
        )
        c.execute("SELECT paid_at FROM bookings WHERE id = ?", (booking_id,))
        paid_at = c.fetchone()[0]

        conn.commit()
        return {"booking_id": booking_id, "paid": True, "paid_at": paid_at}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_unpaid(
    booking_id: int,
    *,
    by_master_slug: str | None = None,
    is_owner: bool = False,
) -> dict:
    """
    Откатывает отметку оплаты (исправление ошибки мастера). Идемпотентно.
    Raises ValueError если status != 'booked' или нет прав.
    """
    conn = get_conn_immediate()
    try:
        c = conn.cursor()
        c.execute("SELECT status, master_id FROM bookings WHERE id = ?", (booking_id,))
        row = c.fetchone()
        if not row:
            raise ValueError(f"Запись #{booking_id} не найдена")
        status, master_id = row

        if status != "booked":
            raise ValueError(
                f"Нельзя снять оплату: запись #{booking_id} имеет статус {status!r}"
            )

        _check_payment_permission(
            c, booking_master_id=master_id, by_master_slug=by_master_slug, is_owner=is_owner
        )

        c.execute(
            "UPDATE bookings SET paid = 0, paid_at = NULL WHERE id = ?",
            (booking_id,),
        )
        conn.commit()
        return {"booking_id": booking_id, "paid": False, "paid_at": None}
    except Exception:
        conn.rollback()
        raise
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


def revenue_by_period(
    date_from: str,               # YYYY-MM-DD включительно
    date_to: str,                 # YYYY-MM-DD включительно
    master_slug: str | None = None,
) -> dict:
    """
    Выручка за период [date_from, date_to] включительно.
    Считается по start_time (дата визита), НЕ по paid_at.
    Фильтр: paid=1 AND status='booked'.

    Допущение: одна бронь = один мастер = одна сумма;
    разделённых броней между мастерами нет.

    Returns:
        {
            "date_from": str, "date_to": str,
            "total": int, "count": int,
            "by_master": [{"master_slug", "master_name", "total", "count"}]
        }
    """
    # Верхняя граница: < следующего дня 00:00, а не <= 23:59.
    # Страховка: если когда-нибудь добавятся слоты 23:00+ с секундами — корректно.
    dt_from   = date_from + " 00:00"
    dt_to_ex  = (
        datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d 00:00")

    conn = get_conn()
    try:
        c = conn.cursor()

        params: list = [dt_from, dt_to_ex]
        master_filter = ""
        if master_slug is not None:
            master_filter = "AND m.slug = ?"
            params.append(master_slug)

        c.execute(
            f"""
            SELECT m.slug,
                   m.name,
                   SUM(b.total_price) AS total,
                   COUNT(b.id)        AS cnt
            FROM bookings b
            JOIN masters m ON m.id = b.master_id
            WHERE b.paid = 1
              AND b.status = 'booked'
              AND b.start_time >= ?
              AND b.start_time <  ?
              {master_filter}
            GROUP BY m.id, m.slug, m.name
            ORDER BY m.name
            """,
            params,
        )
        rows = c.fetchall()

        by_master = [
            {
                "master_slug": r[0],
                "master_name": r[1],
                "total":       r[2] or 0,
                "count":       r[3],
            }
            for r in rows
        ]

        return {
            "date_from": date_from,
            "date_to":   date_to,
            "total":     sum(m["total"] for m in by_master),
            "count":     sum(m["count"] for m in by_master),
            "by_master": by_master,
        }
    finally:
        conn.close()


def get_today_bookings(date: str, master_slug: str | None = None) -> list[dict]:
    """
    Записи на конкретную дату (YYYY-MM-DD, МСК).
    master_slug=None → все мастера (для owner).
    Возвращает только status='booked', сортировка по start_time.
    """
    master_filter = "AND m.slug = ?" if master_slug else ""
    params: list = [date + " %"]
    if master_slug:
        params.append(master_slug)

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            f"""
            SELECT b.start_time, b.end_time,
                   cl.name, cl.phone,
                   m.name,
                   b.paid, b.total_price,
                   GROUP_CONCAT(s.name, ', ') AS services
            FROM   bookings b
            JOIN   clients cl ON cl.id = b.client_id
            JOIN   masters m  ON m.id  = b.master_id
            LEFT JOIN booking_services bs ON bs.booking_id = b.id
            LEFT JOIN services s          ON s.id          = bs.service_id
            WHERE  b.status = 'booked'
              AND  b.start_time LIKE ?
              {master_filter}
            GROUP  BY b.id
            ORDER  BY b.start_time
            """,
            params,
        )
        return [
            {
                "start":        r[0],
                "end":          r[1],
                "client_name":  r[2] or "—",
                "client_phone": r[3] or "",
                "master_name":  r[4],
                "paid":         bool(r[5]),
                "total_price":  r[6],
                "services":     r[7] or "",
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()

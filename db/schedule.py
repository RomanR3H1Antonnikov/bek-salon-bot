"""
Управление графиком мастеров: working_hours и schedule_overrides.
Все write-функции — только для role=owner; enforce на уровне бота, не здесь.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .schema import get_conn

MSK = ZoneInfo("Europe/Moscow")

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class ScheduleConflict(Exception):
    """На дату есть booked-записи — изменение заблокировано."""
    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} запись(ей) на дату")


def _master_id(c, slug: str) -> int:
    c.execute("SELECT id FROM masters WHERE slug = ?", (slug,))
    row = c.fetchone()
    if not row:
        raise ValueError(f"Мастер '{slug}' не найден")
    return row[0]


def get_schedule_conflicts(master_slug: str, date: str) -> list[dict]:
    """
    Booked-записи мастера на конкретную дату (МСК).
    Пустой список → конфликтов нет, можно менять или закрывать день.

    Используется перед set_day_off / set_hours_override.
    date: YYYY-MM-DD

    Почему LIKE с пробелом, а не BETWEEN:
      start_time хранится как «YYYY-MM-DD HH:MM» — LIKE '2026-07-05 %'
      точно изолирует день без риска захватить другие даты.
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        c.execute(
            """
            SELECT b.id, b.start_time, b.end_time, cl.name, cl.phone
            FROM   bookings b
            JOIN   clients cl ON cl.id = b.client_id
            WHERE  b.master_id  = ?
              AND  b.status     = 'booked'
              AND  b.start_time LIKE ?
            ORDER  BY b.start_time
            """,
            (mid, date + " %"),
        )
        return [
            {
                "booking_id":   r[0],
                "time":         r[1][11:16] + "–" + r[2][11:16],  # "10:00–10:30"
                "client_name":  r[3] or "—",
                "client_phone": r[4] or "—",
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def set_day_off(master_slug: str, date: str) -> None:
    """
    Выходной на конкретную дату (is_working=0).
    Raises ScheduleConflict если на дату есть booked-записи.
    Идемпотентен: повторный вызов перезаписывает прежний override.
    """
    conflicts = get_schedule_conflicts(master_slug, date)
    if conflicts:
        raise ScheduleConflict(conflicts)

    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        with conn:
            conn.execute(
                """
                INSERT INTO schedule_overrides
                    (master_id, date, is_working, open_time, close_time)
                VALUES (?, ?, 0, NULL, NULL)
                ON CONFLICT(master_id, date) DO UPDATE SET
                    is_working=0, open_time=NULL, close_time=NULL
                """,
                (mid, date),
            )
    finally:
        conn.close()


def set_hours_override(master_slug: str, date: str, open_time: str, close_time: str) -> None:
    """
    Нестандартные часы на конкретную дату (is_working=1).
    Raises ScheduleConflict если на дату есть booked-записи.
    Идемпотентен.
    """
    conflicts = get_schedule_conflicts(master_slug, date)
    if conflicts:
        raise ScheduleConflict(conflicts)

    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        with conn:
            conn.execute(
                """
                INSERT INTO schedule_overrides
                    (master_id, date, is_working, open_time, close_time)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(master_id, date) DO UPDATE SET
                    is_working=1,
                    open_time=excluded.open_time,
                    close_time=excluded.close_time
                """,
                (mid, date, open_time, close_time),
            )
    finally:
        conn.close()


def remove_override(master_slug: str, date: str) -> bool:
    """
    Удаляет исключение для мастера на дату.
    True если строка была, False если нечего удалять.
    Не проверяет конфликты — снятие выходного не может навредить.
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        with conn:
            cur = conn.execute(
                "DELETE FROM schedule_overrides WHERE master_id = ? AND date = ?",
                (mid, date),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def get_overrides(master_slug: str, from_date: str | None = None) -> list[dict]:
    """
    Исключения мастера начиная с from_date (по умолчанию — сегодня МСК).
    Сортировка по дате ASC.
    """
    if from_date is None:
        from_date = datetime.now(MSK).strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        c.execute(
            """
            SELECT date, is_working, open_time, close_time
            FROM   schedule_overrides
            WHERE  master_id = ? AND date >= ?
            ORDER  BY date
            """,
            (mid, from_date),
        )
        return [
            {
                "date":       r[0],
                "type":       "hours" if r[1] else "dayoff",
                "open_time":  r[2],
                "close_time": r[3],
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def get_working_hours(master_slug: str) -> list[dict]:
    """Регулярный график по всем дням недели (0=Пн … 6=Вс)."""
    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        c.execute(
            """
            SELECT weekday, open_time, close_time
            FROM   working_hours
            WHERE  master_id = ?
            ORDER  BY weekday
            """,
            (mid,),
        )
        return [
            {
                "weekday": r[0],
                "name":    WEEKDAY_NAMES[r[0]],
                "open":    r[1],
                "close":   r[2],
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def get_weekday_conflicts(
    master_slug: str,
    weekday: int,
    horizon_days: int = 90,
) -> list[dict]:
    """
    Booked-записи мастера на все будущие даты заданного дня недели
    в горизонте horizon_days дней от сегодня (МСК).

    weekday: 0=Пн … 6=Вс (Python-конвенция).

    Конвертация в SQLite %w: Sun=0, Mon=1 … Sat=6
    → sqlite_wd = (py_wd + 1) % 7
    """
    today       = datetime.now(MSK).strftime("%Y-%m-%d")
    horizon_end = (datetime.now(MSK) + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
    sqlite_wd   = str((weekday + 1) % 7)

    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        c.execute(
            """
            SELECT b.id, b.start_time, b.end_time, cl.name, cl.phone
            FROM   bookings b
            JOIN   clients cl ON cl.id = b.client_id
            WHERE  b.master_id  = ?
              AND  b.status     = 'booked'
              AND  b.start_time >= ?
              AND  b.start_time <  ?
              AND  strftime('%w', b.start_time) = ?
            ORDER  BY b.start_time
            """,
            (mid, today + " 00:00", horizon_end + " 00:00", sqlite_wd),
        )
        return [
            {
                "booking_id":   r[0],
                "date":         r[1][:10],
                "time":         r[1][11:16] + "–" + r[2][11:16],
                "client_name":  r[3] or "—",
                "client_phone": r[4] or "—",
            }
            for r in c.fetchall()
        ]
    finally:
        conn.close()


def set_weekday_hours(
    master_slug: str, weekday: int, open_time: str, close_time: str
) -> None:
    """
    Обновляет регулярные часы мастера на конкретный день недели.
    weekday: 0=Пн … 6=Вс.

    Raises ScheduleConflict если в горизонте 90 дней есть booked-записи
    на этот день недели — изменение затронуло бы их все разом.
    """
    conflicts = get_weekday_conflicts(master_slug, weekday)
    if conflicts:
        raise ScheduleConflict(conflicts)

    conn = get_conn()
    try:
        c = conn.cursor()
        mid = _master_id(c, master_slug)
        with conn:
            conn.execute(
                """
                INSERT INTO working_hours (master_id, weekday, open_time, close_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(master_id, weekday) DO UPDATE SET
                    open_time=excluded.open_time,
                    close_time=excluded.close_time
                """,
                (mid, weekday, open_time, close_time),
            )
    finally:
        conn.close()

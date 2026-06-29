import sqlite3
from pathlib import Path

# Абсолютный путь к БД — корень проекта (родитель папки db/).
# Нужен, чтобы systemd не создавал файл в своём CWD (/root или /tmp).
DB_FILE = str(Path(__file__).parent.parent / "salon_bookings.db")
MASTER_SLUG = "bek"   # владелец — дефолт для бота и backward-compat

MASTERS_SEED: dict[str, dict] = {
    "bek": {"name": "Бек", "role": "owner"},
    "ali": {"name": "Али", "role": "master"},
}

# Ключ — русское название (для GPT-промпта бота и дисплея).
# slug — идентификатор для API и фронта. Финальный каталог: 12 позиций (сверен с сайтом).
SERVICES_SEED: dict[str, dict] = {
    "мужская стрижка":              {"slug": "mens-haircut",      "price": 1500, "duration": 30},
    "детская стрижка (до 11 лет)":  {"slug": "kids-haircut",      "price": 1000, "duration": 30},
    "социальная стрижка":           {"slug": "social-haircut",    "price":  600, "duration": 30},
    "стрижка машинкой (1 насадка)": {"slug": "machine-haircut-1", "price":  400, "duration": 15},
    "стрижка машинкой (2 насадки)": {"slug": "machine-haircut-2", "price":  600, "duration": 30},
    "химическая завивка":           {"slug": "perm-haircut",      "price": 4000, "duration": 180},
    "тонировка головы":             {"slug": "hair-tinting",      "price": 1200, "duration": 30},
    "моделирование бороды":         {"slug": "beard-modeling",    "price": 1000, "duration": 30},
    "тонировка бороды":             {"slug": "beard-tinting",     "price":  600, "duration": 30},
    "чистка лица (комплекс)":       {"slug": "face-cleansing",    "price": 2000, "duration": 60},
    "скраб + чёрная маска":         {"slug": "face-scrub-mask",   "price":  500, "duration": 30},
    "папа + сын":                   {"slug": "father-and-son",    "price": 2500, "duration": 60},
}
# Деактивированы: haircut-beard, haircut-beard-wax (комбо заменены мультивыбором),
# clipper-1/2, face-care, scrub-mask, beard-tint, head-tint, dad-son (slug'и переименованы),
# eye-patches, full-complex, wax-*, eyebrow-wax (удалены из каталога).

# Обратный индекс slug → {name, price, duration} для быстрой валидации.
SERVICES_BY_SLUG: dict[str, dict] = {
    info["slug"]: {"name": name, "price": info["price"], "duration": info["duration"]}
    for name, info in SERVICES_SEED.items()
}


def sum_duration(slugs: list[str]) -> int:
    """Валидирует список slug'ов и возвращает суммарную длительность в минутах.
    Raises ValueError при пустом списке или неизвестном slug."""
    if not slugs:
        raise ValueError("Список услуг не может быть пустым")
    for slug in slugs:
        if slug not in SERVICES_BY_SLUG:
            raise ValueError(f"Неизвестная услуга: {slug!r}")
    return sum(SERVICES_BY_SLUG[s]["duration"] for s in slugs)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn_immediate() -> sqlite3.Connection:
    """Открывает соединение и сразу берёт эксклюзивную блокировку (BEGIN IMMEDIATE).
    Используй только для write-транзакций (create_booking).
    Вызывающий код обязан вызвать conn.commit() или conn.rollback() и conn.close()."""
    conn = sqlite3.connect(DB_FILE, timeout=5, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    return conn


def init_db() -> None:
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS masters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            role        TEXT    NOT NULL DEFAULT 'master',  -- 'owner' | 'master'
            telegram_id INTEGER,
            created_at  TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )
    """)
    # Миграция: добавляем role к существующим БД без этой колонки
    c.execute("PRAGMA table_info(masters)")
    if "role" not in {row[1] for row in c.fetchall()}:
        c.execute("ALTER TABLE masters ADD COLUMN role TEXT NOT NULL DEFAULT 'master'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            slug         TEXT    NOT NULL UNIQUE,
            name         TEXT    NOT NULL UNIQUE,
            price        INTEGER NOT NULL,
            duration_min INTEGER NOT NULL,
            active       INTEGER DEFAULT 1
        )
    """)
    # Миграция: добавляем slug к существующим БД без этой колонки
    c.execute("PRAGMA table_info(services)")
    if "slug" not in {row[1] for row in c.fetchall()}:
        c.execute("ALTER TABLE services ADD COLUMN slug TEXT")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_services_slug "
            "ON services(slug) WHERE slug IS NOT NULL"
        )

    c.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            name        TEXT,
            phone       TEXT,
            created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )
    """)
    # Partial unique indexes — NULL не участвует в конфликтах
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_phone
        ON clients(phone) WHERE phone IS NOT NULL
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_tg
        ON clients(telegram_id) WHERE telegram_id IS NOT NULL
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id        INTEGER NOT NULL REFERENCES masters(id),
            client_id        INTEGER NOT NULL REFERENCES clients(id),
            start_time       TEXT    NOT NULL,   -- московское время YYYY-MM-DD HH:MM
            end_time         TEXT    NOT NULL,   -- московское время YYYY-MM-DD HH:MM
            status           TEXT    DEFAULT 'booked',
            source           TEXT    DEFAULT 'bot',
            note             TEXT,
            reminded         INTEGER DEFAULT 0,
            master_notified  INTEGER DEFAULT 0,
            manage_token     TEXT    UNIQUE,     -- secrets.token_urlsafe(16), выдаётся клиенту
            -- created_at хранится в UTC (SQLite datetime('now')), start_time — МСК.
            -- Не сравнивай их напрямую — расхождение в 3ч.
            created_at       TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )
    """)
    # Миграция: добавляем manage_token к существующим БД
    c.execute("PRAGMA table_info(bookings)")
    if "manage_token" not in {row[1] for row in c.fetchall()}:
        c.execute("ALTER TABLE bookings ADD COLUMN manage_token TEXT")
    # Partial unique index: NULL не индексируется, только реальные токены уникальны
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_manage_token "
        "ON bookings(manage_token) WHERE manage_token IS NOT NULL"
    )
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_bookings_master_start
        ON bookings(master_id, start_time)
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS booking_services (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            service_id INTEGER NOT NULL REFERENCES services(id),
            price      INTEGER NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS working_hours (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id   INTEGER NOT NULL REFERENCES masters(id),
            weekday     INTEGER NOT NULL,  -- 0=пн, 6=вс
            open_time   TEXT    NOT NULL,  -- HH:MM МСК
            close_time  TEXT    NOT NULL,  -- HH:MM МСК
            UNIQUE(master_id, weekday)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule_overrides (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id   INTEGER NOT NULL REFERENCES masters(id),
            date        TEXT    NOT NULL,          -- YYYY-MM-DD МСК
            is_working  INTEGER DEFAULT 0,         -- 0=выходной, 1=особые часы
            open_time   TEXT,
            close_time  TEXT,
            UNIQUE(master_id, date)
        )
    """)

    conn.commit()
    conn.close()
    print("[INIT] Схема БД актуальна (CREATE TABLE IF NOT EXISTS, данные не тронуты)")


def seed_db(master_telegram_id: int | None = None) -> int:
    """Идемпотентный сид: мастера, услуги, рабочие часы.
    master_telegram_id — Telegram ID владельца (bek), задаётся при старте юзербота.
    Возвращает id мастера-владельца (bek)."""
    conn = get_conn()
    c = conn.cursor()

    # Мастера — все из MASTERS_SEED
    for slug, info in MASTERS_SEED.items():
        c.execute(
            "INSERT OR IGNORE INTO masters (slug, name, role) VALUES (?, ?, ?)",
            (slug, info["name"], info["role"]),
        )
        c.execute(
            "UPDATE masters SET name = ?, role = ? WHERE slug = ?",
            (info["name"], info["role"], slug),
        )
    # Telegram ID владельца (bek) — проставляем только для него
    if master_telegram_id:
        c.execute(
            "UPDATE masters SET telegram_id = ? WHERE slug = ?",
            (master_telegram_id, MASTER_SLUG),
        )
    c.execute("SELECT id FROM masters WHERE slug = ?", (MASTER_SLUG,))
    owner_id: int = c.fetchone()[0]

    # Услуги: три шага, чтобы корректно обрабатывать переименование slug'ов.
    # Проблема: UNIQUE(slug) и UNIQUE(name) — если slug меняется при том же name,
    # INSERT ON CONFLICT(slug) упадёт на name-constraint до срабатывания DO UPDATE.
    # Решение: сначала обновить по slug, затем по name, потом вставить новую строку.
    for name, info in SERVICES_SEED.items():
        slug, price, dur = info["slug"], info["price"], info["duration"]

        # 1. Обычный идемпотентный случай: slug уже есть — обновить поля.
        c.execute(
            "UPDATE services SET name=?, price=?, duration_min=?, active=1 WHERE slug=?",
            (name, price, dur, slug),
        )
        if c.rowcount:
            continue

        # 2. Slug сменился, но название прежнее (переименование slug).
        c.execute(
            "UPDATE services SET slug=?, price=?, duration_min=?, active=1 WHERE name=?",
            (slug, price, dur, name),
        )
        if c.rowcount:
            continue

        # 3. Совершенно новая позиция.
        c.execute(
            "INSERT INTO services (slug, name, price, duration_min, active) VALUES (?, ?, ?, ?, 1)",
            (slug, name, price, dur),
        )

    # Деактивируем всё, чего нет в финальном seed (сохраняем историю броней).
    active_slugs = [info["slug"] for info in SERVICES_SEED.values()]
    c.execute(
        f"UPDATE services SET active=0 WHERE slug NOT IN ({','.join('?'*len(active_slugs))})",
        active_slugs,
    )

    # Рабочие часы: ежедневно 10:00–22:00 для ВСЕХ мастеров из MASTERS_SEED
    c.execute("SELECT id FROM masters WHERE slug IN ({})".format(
        ",".join("?" * len(MASTERS_SEED))
    ), list(MASTERS_SEED.keys()))
    all_master_ids = [row[0] for row in c.fetchall()]
    for mid in all_master_ids:
        for weekday in range(7):
            c.execute(
                """
                INSERT OR IGNORE INTO working_hours (master_id, weekday, open_time, close_time)
                VALUES (?, ?, '10:00', '22:00')
                """,
                (mid, weekday),
            )

    conn.commit()
    conn.close()
    print(
        f"[SEED] Мастеров: {len(MASTERS_SEED)} "
        f"({', '.join(MASTERS_SEED.keys())}), "
        f"услуги ({len(SERVICES_SEED)} шт.), часы 10:00–22:00 ежедневно"
    )
    return owner_id

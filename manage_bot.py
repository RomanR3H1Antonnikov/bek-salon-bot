"""
Управляющий бот (BEK_NOTIFY_BOT) — расширение, НЕ новый бот.
Один токен, два режима:
  1. Пассивный: принимает уведомления о записях, которые шлёт юзербот.
  2. Активный: CRM-команды через reply-keyboard.

Роли:
  owner (Бек)  — полный доступ: выручка, график, настройки.
  master (Али) — только своё: записи, выручка, график (read-only).

Запуск: python manage_bot.py
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

from db import init_db, get_conn, revenue_by_period
from db.schema import MASTERS_SEED, MASTER_SLUG
from db.schedule import (
    ScheduleConflict,
    WEEKDAY_NAMES,
    get_schedule_conflicts,
    set_day_off,
    set_hours_override,
    remove_override,
    get_overrides,
    get_working_hours,
    set_weekday_hours,
)

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")

BOT_TOKEN    = os.environ["BEK_NOTIFY_BOT_TOKEN"]
BEK_OWNER_ID: int | None = (
    int(os.environ["BEK_OWNER_ID"]) if os.environ.get("BEK_OWNER_ID") else None
)
_BEK_CHAT_ID_FILE = _HERE / "bek_chat_id.txt"
_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MSK = ZoneInfo("Europe/Moscow")

# Conversation state per chat (in-memory; clears on restart — acceptable for mgmt bot)
_STATE: dict[int, str] = {}


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_master_by_tg(telegram_id: int) -> dict | None:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT slug, name, role FROM masters WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = c.fetchone()
        return {"slug": row[0], "name": row[1], "role": row[2]} if row else None
    finally:
        conn.close()


def _link_master(slug: str, telegram_id: int) -> bool:
    """Привязывает telegram_id к мастеру по slug — только если поле ещё NULL."""
    conn = get_conn()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE masters SET telegram_id = ? WHERE slug = ? AND telegram_id IS NULL",
                (telegram_id, slug),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


# ── Bot API helpers ─────────────────────────────────────────────────────────────

async def _send(
    session: aiohttp.ClientSession,
    chat_id: int,
    text: str,
    keyboard: dict | None = None,
) -> None:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    try:
        async with session.post(
            f"{_API}/sendMessage", json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                print(f"[MANAGE] sendMessage {r.status}: {(await r.text())[:200]}")
    except Exception as e:
        print(f"[MANAGE] sendMessage error: {e}")


# ── Keyboards ──────────────────────────────────────────────────────────────────

_OWNER_KB = {
    "keyboard": [
        [{"text": "📅 Записи на сегодня"}, {"text": "💰 Выручка"}],
        [{"text": "👨‍💼 Мастера"},           {"text": "⚙️ Настройки"}],
    ],
    "resize_keyboard": True,
}

_MASTER_KB = {
    "keyboard": [
        [{"text": "📅 Мои записи на сегодня"}],
        [{"text": "💰 Моя выручка"}, {"text": "💳 Мои оплаты"}],
        [{"text": "📋 Мой график"}],
    ],
    "resize_keyboard": True,
}

_REVENUE_KB = {
    "keyboard": [
        [{"text": "💰 Сегодня"},    {"text": "💰 7 дней"}],
        [{"text": "💰 Этот месяц"}, {"text": "🔙 В меню"}],
    ],
    "resize_keyboard": True,
}

# Динамический: одна кнопка на каждого мастера из MASTERS_SEED
_MASTERS_KB = {
    "keyboard": [
        [{"text": f"📋 График: {info['name']}"} for info in MASTERS_SEED.values()],
        [{"text": "🔙 В меню"}],
    ],
    "resize_keyboard": True,
}

_SCHED_OWNER_KB = {
    "keyboard": [
        [{"text": "📅 Выходной на дату"}, {"text": "🕐 Часы на дату"}],
        [{"text": "↩️ Снять исключение"}, {"text": "📋 Регулярный график"}],
        [{"text": "🔙 К мастерам"}],
    ],
    "resize_keyboard": True,
}

_SCHED_MASTER_VIEW_KB = {
    "keyboard": [[{"text": "🔙 В меню"}]],
    "resize_keyboard": True,
}

_OWNER_STUBS: dict[str, str] = {
    "📅 Записи на сегодня": "📅 <b>Записи на сегодня</b>\n\n⏳ Скоро.",
    "⚙️ Настройки":        "⚙️ <b>Настройки</b>\n\n⏳ Скоро.",
}

_MASTER_STUBS: dict[str, str] = {
    "📅 Мои записи на сегодня": "📅 <b>Мои записи</b>\n\n⏳ Скоро.",
    "💳 Мои оплаты":            "💳 <b>Мои оплаты</b>\n\n⏳ Скоро.",
}


# ── Revenue helpers ─────────────────────────────────────────────────────────────

def _revenue_period(btn: str) -> tuple[str, str]:
    today = datetime.now(MSK).date()
    if btn == "💰 Сегодня":
        return str(today), str(today)
    if btn == "💰 7 дней":
        return str(today - timedelta(days=6)), str(today)
    if btn == "💰 Этот месяц":
        return str(today.replace(day=1)), str(today)
    return str(today), str(today)


def _fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def _fmt_date_ru(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y")


def _fmt_revenue(data: dict, role: str) -> str:
    df, dt = data["date_from"], data["date_to"]
    period = _fmt_date_ru(df) if df == dt else f"{_fmt_date_ru(df)} — {_fmt_date_ru(dt)}"

    if role == "owner":
        if data["count"] == 0:
            return f"💰 <b>Выручка {period}</b>\n\nНет оплаченных записей."
        lines = [
            f"💰 <b>Выручка {period}</b>",
            "",
            f"Итого:  <b>{_fmt_money(data['total'])} ₽</b>  ({data['count']} зап.)",
        ]
        if len(data["by_master"]) > 1:
            lines.append("")
            for m in data["by_master"]:
                lines.append(
                    f"  👤 {m['master_name']}:  {_fmt_money(m['total'])} ₽"
                    f"  ({m['count']} зап.)"
                )
        return "\n".join(lines)
    else:
        if data["count"] == 0:
            return f"💰 <b>Ваша выручка {period}</b>\n\nНет оплаченных записей."
        return (
            f"💰 <b>Ваша выручка {period}</b>\n\n"
            f"<b>{_fmt_money(data['total'])} ₽</b>  ({data['count']} записей)"
        )


# ── Schedule input parsers ──────────────────────────────────────────────────────

def _parse_date(text: str) -> str | None:
    """YYYY-MM-DD или ДД.ММ.ГГГГ → YYYY-MM-DD, иначе None."""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_time_str(t: str) -> str | None:
    """«10» или «10:00» → «10:00», иначе None."""
    t = t.strip()
    if re.match(r"^\d{1,2}$", t):
        try:
            return datetime.strptime(t, "%H").strftime("%H:%M")
        except ValueError:
            return None
    try:
        return datetime.strptime(t, "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def _parse_time_range(text: str) -> tuple[str, str] | None:
    """«12:00-20:00» или «12-20» → («12:00», «20:00»), иначе None."""
    text = text.strip().replace("–", "-").replace("—", "-")
    m = re.match(r"^(\d{1,2}(?::\d{2})?)\s*-\s*(\d{1,2}(?::\d{2})?)$", text)
    if not m:
        return None
    open_t  = _parse_time_str(m.group(1))
    close_t = _parse_time_str(m.group(2))
    if not open_t or not close_t:
        return None
    return open_t, close_t


# ── Schedule display helpers ────────────────────────────────────────────────────

async def _show_sched_menu(
    session: aiohttp.ClientSession,
    chat_id: int,
    slug: str,
    prefix: str = "",
) -> None:
    name = MASTERS_SEED[slug]["name"]
    msg  = (prefix + "\n\n" if prefix else "") + f"📋 <b>График: {name}</b>"
    _STATE[chat_id] = f"sched:{slug}"
    await _send(session, chat_id, msg, keyboard=_SCHED_OWNER_KB)


async def _show_schedule_readonly(
    session: aiohttp.ClientSession, chat_id: int, slug: str
) -> None:
    hours     = get_working_hours(slug)
    overrides = get_overrides(slug)

    lines = ["📋 <b>Мой регулярный график</b>"]
    for h in hours:
        lines.append(f"{h['name']}: {h['open']}–{h['close']}")

    if overrides:
        lines.append("")
        lines.append("📅 <b>Ближайшие изменения:</b>")
        for o in overrides[:10]:
            if o["type"] == "dayoff":
                lines.append(f"• {_fmt_date_ru(o['date'])} — выходной")
            else:
                lines.append(f"• {_fmt_date_ru(o['date'])} — {o['open_time']}–{o['close_time']}")

    await _send(session, chat_id, "\n".join(lines), keyboard=_SCHED_MASTER_VIEW_KB)


async def _show_regular_schedule(
    session: aiohttp.ClientSession, chat_id: int, slug: str, prefix: str = ""
) -> None:
    hours = get_working_hours(slug)
    name  = MASTERS_SEED[slug]["name"]

    lines = []
    if prefix:
        lines.append(prefix)
        lines.append("")
    lines.append(f"📋 <b>Регулярный график: {name}</b>")
    for h in hours:
        lines.append(f"{h['name']}: {h['open']}–{h['close']}")
    lines.append("\nНажмите день для изменения часов:")

    wd_kb = {
        "keyboard": [
            [{"text": d} for d in WEEKDAY_NAMES[:4]],
            [{"text": d} for d in WEEKDAY_NAMES[4:]],
            [{"text": "🔙 К мастерам"}],
        ],
        "resize_keyboard": True,
    }
    _STATE[chat_id] = f"regular:{slug}"
    await _send(session, chat_id, "\n".join(lines), keyboard=wd_kb)


async def _show_remove_overrides(
    session: aiohttp.ClientSession, chat_id: int, slug: str
) -> None:
    overrides = get_overrides(slug)
    name = MASTERS_SEED[slug]["name"]

    if not overrides:
        await _send(session, chat_id,
            f"У <b>{name}</b> нет запланированных исключений.",
            keyboard=_SCHED_OWNER_KB,
        )
        return

    lines   = [f"Исключения для <b>{name}</b> — нажмите чтобы удалить:"]
    buttons: list[dict] = []
    for o in overrides[:20]:
        if o["type"] == "dayoff":
            label = f"🚫 {o['date']}"
            lines.append(f"• {_fmt_date_ru(o['date'])} — выходной")
        else:
            label = f"🕐 {o['date']}"
            lines.append(f"• {_fmt_date_ru(o['date'])} — {o['open_time']}–{o['close_time']}")
        buttons.append({"text": label})

    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([{"text": "🔙 К мастерам"}])
    kb = {"keyboard": rows, "resize_keyboard": True}

    _STATE[chat_id] = f"remove:{slug}"
    await _send(session, chat_id, "\n".join(lines), keyboard=kb)


# ── Schedule action helpers ─────────────────────────────────────────────────────

async def _apply_day_off(
    session: aiohttp.ClientSession, chat_id: int, slug: str, date: str
) -> None:
    name = MASTERS_SEED[slug]["name"]
    try:
        set_day_off(slug, date)
        await _show_sched_menu(session, chat_id, slug,
            prefix=f"✅ Выходной для <b>{name}</b> на <b>{_fmt_date_ru(date)}</b> поставлен.")
    except ScheduleConflict as e:
        lines = [
            f"⚠️ Нельзя поставить выходной — у <b>{name}</b> на "
            f"<b>{_fmt_date_ru(date)}</b> {len(e.conflicts)} запись(ей):"
        ]
        for c in e.conflicts:
            lines.append(f"  • {c['time']} — {c['client_name']}")
        lines.append("\nПеренесите или отмените записи, затем попробуйте снова.")
        # State остаётся «dayoff:slug» — пользователь может ввести другую дату
        await _send(session, chat_id, "\n".join(lines))


async def _apply_hours_override(
    session: aiohttp.ClientSession,
    chat_id: int,
    slug: str,
    date: str,
    open_t: str,
    close_t: str,
) -> None:
    name = MASTERS_SEED[slug]["name"]
    try:
        set_hours_override(slug, date, open_t, close_t)
        await _show_sched_menu(session, chat_id, slug,
            prefix=f"✅ Часы для <b>{name}</b> на <b>{_fmt_date_ru(date)}</b>: {open_t}–{close_t}")
    except ScheduleConflict as e:
        lines = [
            f"⚠️ Нельзя изменить часы — у <b>{name}</b> на "
            f"<b>{_fmt_date_ru(date)}</b> {len(e.conflicts)} запись(ей):"
        ]
        for c in e.conflicts:
            lines.append(f"  • {c['time']} — {c['client_name']}")
        lines.append("\nПеренесите или отмените записи, затем попробуйте снова.")
        await _send(session, chat_id, "\n".join(lines))


# ── Handlers ───────────────────────────────────────────────────────────────────

async def _handle_start(
    session: aiohttp.ClientSession,
    chat_id: int,
    tg_id: int,
    first_name: str,
    param: str,
) -> None:
    master = _get_master_by_tg(tg_id)

    if master is None and BEK_OWNER_ID and tg_id == BEK_OWNER_ID:
        master = {"slug": MASTER_SLUG, "name": "Бек", "role": "owner"}

    if master is None and param and param in MASTERS_SEED and param != MASTER_SLUG:
        if _link_master(param, tg_id):
            info   = MASTERS_SEED[param]
            master = {"slug": param, "name": info["name"], "role": info["role"]}
            print(f"[MANAGE] linked: {param} → tg_id={tg_id}")
        else:
            await _send(session, chat_id,
                f"⚠️ Мастер <b>{MASTERS_SEED[param]['name']}</b> уже привязан к другому аккаунту.\n"
                "Обратитесь к владельцу."
            )
            return

    if master is None:
        slugs_for_masters = [s for s in MASTERS_SEED if s != MASTER_SLUG]
        hint = "\n".join(f"<code>/start {s}</code>" for s in slugs_for_masters)
        await _send(session, chat_id,
            "❌ Ваш Telegram не привязан к салону.\n\n"
            f"Если вы мастер, введите:\n{hint}"
        )
        return

    _STATE.pop(chat_id, None)

    if master["role"] == "owner":
        _BEK_CHAT_ID_FILE.write_text(str(chat_id))
        print(f"[MANAGE] owner /start: chat_id={chat_id}")
        await _send(session, chat_id,
            f"👋 Привет, {first_name}!\n"
            f"Вы вошли как <b>владелец</b>.\n\n"
            "Управляющий бот готов к работе.",
            keyboard=_OWNER_KB,
        )
    else:
        await _send(session, chat_id,
            f"👋 Привет, {first_name}!\n"
            f"Вы вошли как <b>мастер {master['name']}</b>.",
            keyboard=_MASTER_KB,
        )


async def _handle_text(
    session: aiohttp.ClientSession,
    chat_id: int,
    tg_id: int,
    text: str,
) -> None:
    master = _get_master_by_tg(tg_id)
    if master is None and BEK_OWNER_ID and tg_id == BEK_OWNER_ID:
        master = {"slug": MASTER_SLUG, "name": "Бек", "role": "owner"}
    if master is None:
        return

    role    = master["role"]
    main_kb = _OWNER_KB if role == "owner" else _MASTER_KB
    state   = _STATE.get(chat_id, "")

    # ── Глобальная навигация (работает из любого состояния) ─────────────────
    if text == "🔙 В меню":
        _STATE.pop(chat_id, None)
        await _send(session, chat_id, "Главное меню:", keyboard=main_kb)
        return

    if text == "🔙 К мастерам" and role == "owner":
        _STATE[chat_id] = "masters"
        await _send(session, chat_id, "Выберите мастера:", keyboard=_MASTERS_KB)
        return

    # ── Выручка ──────────────────────────────────────────────────────────────
    if text in ("💰 Выручка", "💰 Моя выручка"):
        _STATE.pop(chat_id, None)
        await _send(session, chat_id, "Выберите период:", keyboard=_REVENUE_KB)
        return

    if text in ("💰 Сегодня", "💰 7 дней", "💰 Этот месяц"):
        master_filter = None if role == "owner" else master["slug"]
        date_from, date_to = _revenue_period(text)
        try:
            data = revenue_by_period(date_from, date_to, master_slug=master_filter)
            msg  = _fmt_revenue(data, role)
        except Exception as e:
            print(f"[MANAGE] revenue error: {e}")
            msg = "⚠️ Ошибка при расчёте выручки."
        await _send(session, chat_id, msg, keyboard=_REVENUE_KB)
        return

    # ── Вход в раздел Мастера (owner) / Мой график (master) ─────────────────
    if text == "👨‍💼 Мастера" and role == "owner":
        _STATE[chat_id] = "masters"
        await _send(session, chat_id, "Выберите мастера:", keyboard=_MASTERS_KB)
        return

    if text == "📋 Мой график" and role == "master":
        await _show_schedule_readonly(session, chat_id, master["slug"])
        return

    # ── Выбор мастера (state: masters) ──────────────────────────────────────
    if state == "masters" and role == "owner":
        for slug, info in MASTERS_SEED.items():
            if text == f"📋 График: {info['name']}":
                await _show_sched_menu(session, chat_id, slug)
                return

    # ── Меню графика выбранного мастера (state: sched:<slug>) ───────────────
    if state.startswith("sched:") and role == "owner":
        slug = state.split(":", 1)[1]
        name = MASTERS_SEED[slug]["name"]

        if text == "📅 Выходной на дату":
            _STATE[chat_id] = f"dayoff:{slug}"
            await _send(session, chat_id,
                f"Введите дату выходного для <b>{name}</b>:\n"
                "Формат: ГГГГ-ММ-ДД  или  ДД.ММ.ГГГГ")
            return

        if text == "🕐 Часы на дату":
            _STATE[chat_id] = f"hours_date:{slug}"
            await _send(session, chat_id,
                f"Введите дату для нестандартных часов (<b>{name}</b>):\n"
                "Формат: ГГГГ-ММ-ДД  или  ДД.ММ.ГГГГ")
            return

        if text == "↩️ Снять исключение":
            await _show_remove_overrides(session, chat_id, slug)
            return

        if text == "📋 Регулярный график":
            await _show_regular_schedule(session, chat_id, slug)
            return

    # ── Кнопки меню графика работают из любого sched-подсостояния ─────────────
    _SCHED_INPUT_PREFIXES = (
        "dayoff:", "hours_date:", "hours_time:", "remove:", "regular:", "regular_time:"
    )
    if role == "owner" and any(state.startswith(p) for p in _SCHED_INPUT_PREFIXES):
        slug = state.split(":")[1]
        if text == "📅 Выходной на дату":
            _STATE[chat_id] = f"dayoff:{slug}"
            await _send(session, chat_id,
                f"Введите дату выходного для <b>{MASTERS_SEED[slug]['name']}</b>:\n"
                "Формат: ГГГГ-ММ-ДД  или  ДД.ММ.ГГГГ")
            return
        if text == "🕐 Часы на дату":
            _STATE[chat_id] = f"hours_date:{slug}"
            await _send(session, chat_id,
                f"Введите дату для нестандартных часов (<b>{MASTERS_SEED[slug]['name']}</b>):\n"
                "Формат: ГГГГ-ММ-ДД  или  ДД.ММ.ГГГГ")
            return
        if text == "↩️ Снять исключение":
            await _show_remove_overrides(session, chat_id, slug)
            return
        if text == "📋 Регулярный график":
            await _show_regular_schedule(session, chat_id, slug)
            return

    # ── Ввод даты выходного (state: dayoff:<slug>) ───────────────────────────
    if state.startswith("dayoff:") and role == "owner":
        slug = state.split(":", 1)[1]
        date = _parse_date(text)
        if not date:
            await _send(session, chat_id,
                "❌ Не распознал дату.\nПример: 2026-07-15  или  15.07.2026")
            return
        await _apply_day_off(session, chat_id, slug, date)
        return

    # ── Ввод даты нестандартных часов (state: hours_date:<slug>) ────────────
    if state.startswith("hours_date:") and role == "owner":
        slug = state.split(":", 1)[1]
        date = _parse_date(text)
        if not date:
            await _send(session, chat_id,
                "❌ Не распознал дату.\nПример: 2026-07-15  или  15.07.2026")
            return
        _STATE[chat_id] = f"hours_time:{slug}:{date}"
        await _send(session, chat_id,
            f"Дата: <b>{_fmt_date_ru(date)}</b>\n"
            "Введите рабочие часы (пример: 12:00-20:00):")
        return

    # ── Ввод часов (state: hours_time:<slug>:<YYYY-MM-DD>) ──────────────────
    if state.startswith("hours_time:") and role == "owner":
        # state = "hours_time:bek:2026-07-05"  → split limit 2 → 3 parts
        _, slug, date = state.split(":", 2)
        times = _parse_time_range(text)
        if not times:
            await _send(session, chat_id,
                "❌ Не распознал время.\nПример: 12:00-20:00")
            return
        open_t, close_t = times
        await _apply_hours_override(session, chat_id, slug, date, open_t, close_t)
        return

    # ── Удаление исключения: нажатие на дату (state: remove:<slug>) ──────────
    if state.startswith("remove:") and role == "owner":
        slug = state.split(":", 1)[1]
        # Кнопки вида «🚫 2026-07-05» или «🕐 2026-07-05» — вытаскиваем дату
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            date = m.group(1)
            ok   = remove_override(slug, date)
            prefix = (
                f"✅ Исключение на {_fmt_date_ru(date)} для {MASTERS_SEED[slug]['name']} удалено."
                if ok else
                f"❌ Исключение на {_fmt_date_ru(date)} не найдено."
            )
            await _show_sched_menu(session, chat_id, slug, prefix=prefix)
            return

    # ── Регулярный график: нажатие на день (state: regular:<slug>) ───────────
    if state.startswith("regular:") and role == "owner":
        slug = state.split(":", 1)[1]
        if text in WEEKDAY_NAMES:
            weekday = WEEKDAY_NAMES.index(text)
            _STATE[chat_id] = f"regular_time:{slug}:{weekday}"
            await _send(session, chat_id,
                f"Изменение <b>{text}</b> для {MASTERS_SEED[slug]['name']}.\n"
                "Введите новые часы (пример: 10:00-22:00):")
            return

    # ── Регулярный график: ввод часов (state: regular_time:<slug>:<wd>) ──────
    if state.startswith("regular_time:") and role == "owner":
        _, slug, wd_str = state.split(":", 2)
        weekday = int(wd_str)
        times   = _parse_time_range(text)
        if not times:
            await _send(session, chat_id,
                "❌ Не распознал время.\nПример: 10:00-22:00")
            return
        open_t, close_t = times
        try:
            set_weekday_hours(slug, weekday, open_t, close_t)
            prefix = (
                f"✅ {MASTERS_SEED[slug]['name']} / {WEEKDAY_NAMES[weekday]}: "
                f"{open_t}–{close_t}"
            )
            await _show_regular_schedule(session, chat_id, slug, prefix=prefix)
        except ScheduleConflict as e:
            name = MASTERS_SEED[slug]["name"]
            day  = WEEKDAY_NAMES[weekday]
            shown = e.conflicts[:10]
            lines = [
                f"⚠️ Нельзя изменить <b>{day}</b> для {name} — "
                f"в ближайшие 90 дней {len(e.conflicts)} запись(ей):"
            ]
            for bk in shown:
                lines.append(
                    f"  • {_fmt_date_ru(bk['date'])} {bk['time']} — {bk['client_name']}"
                )
            if len(e.conflicts) > 10:
                lines.append(f"  … и ещё {len(e.conflicts) - 10}")
            lines.append(
                "\nПеренесите или отмените эти записи, "
                "затем повторите изменение.\n"
                "Или поставьте разовый override через «📅 Выходной на дату» / «🕐 Часы на дату»."
            )
            # State остаётся regular_time:slug:wd — можно ввести другое время или выйти
            await _send(session, chat_id, "\n".join(lines))
        except Exception as e:
            prefix = f"⚠️ Ошибка: {e}"
            await _show_regular_schedule(session, chat_id, slug, prefix=prefix)
        return

    # ── Stubs для нереализованных пунктов ────────────────────────────────────
    stubs = _OWNER_STUBS if role == "owner" else _MASTER_STUBS
    reply = stubs.get(text)
    if reply:
        await _send(session, chat_id, reply)
    else:
        await _send(session, chat_id, "Используйте кнопки меню.", keyboard=main_kb)


# ── Dispatch ───────────────────────────────────────────────────────────────────

async def _dispatch(session: aiohttp.ClientSession, update: dict) -> None:
    msg = update.get("message")
    if not msg:
        return

    chat_id    = msg["chat"]["id"]
    tg_id      = msg["from"]["id"]
    text       = (msg.get("text") or "").strip()
    first_name = msg["from"].get("first_name", "")

    if not text:
        return

    if text.startswith("/start"):
        param = text[len("/start"):].strip().lstrip("_")
        await _handle_start(session, chat_id, tg_id, first_name, param)
    else:
        await _handle_text(session, chat_id, tg_id, text)


# ── Polling loop ───────────────────────────────────────────────────────────────

async def main() -> None:
    init_db()
    print("[MANAGE] Управляющий бот запущен (long-polling)")

    offset = 0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{_API}/getUpdates",
                    params={
                        "offset":          offset,
                        "timeout":         30,
                        "limit":           100,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json()
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        try:
                            await _dispatch(session, upd)
                        except Exception as e:
                            print(f"[MANAGE] dispatch error: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[MANAGE] polling error: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

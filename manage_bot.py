"""
Управляющий бот (BEK_NOTIFY_BOT) — расширение, НЕ новый бот.
Один токен, два режима:
  1. Пассивный: принимает уведомления о записях, которые шлёт юзербот.
  2. Активный: CRM-команды через reply-keyboard (каркас; реализация — следующие шаги).

Роли:
  owner (Бек)  — видит всё: записи, выручку, мастеров, настройки.
  master (Али) — видит только своё: записи, оплаты.

Запуск: python manage_bot.py
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

from db import init_db, get_conn, revenue_by_period
from db.schema import MASTERS_SEED, MASTER_SLUG

MSK = ZoneInfo("Europe/Moscow")

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")

BOT_TOKEN    = os.environ["BEK_NOTIFY_BOT_TOKEN"]
# BEK_OWNER_ID — fallback для /start до первого seed_db с me.id в юзерботе
BEK_OWNER_ID: int | None = (
    int(os.environ["BEK_OWNER_ID"]) if os.environ.get("BEK_OWNER_ID") else None
)
_BEK_CHAT_ID_FILE = _HERE / "bek_chat_id.txt"
_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ── DB helpers (только для manage_bot) ────────────────────────────────────────

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


# ── Bot API helpers ────────────────────────────────────────────────────────────

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

_OWNER_STUBS: dict[str, str] = {
    "📅 Записи на сегодня": "📅 <b>Записи на сегодня</b>\n\n⏳ Скоро.",
    "👨‍💼 Мастера":          "👨‍💼 <b>Мастера</b>\n\n⏳ Скоро.",
    "⚙️ Настройки":        "⚙️ <b>Настройки</b>\n\n⏳ Скоро.",
}

_MASTER_STUBS: dict[str, str] = {
    "📅 Мои записи на сегодня": "📅 <b>Мои записи</b>\n\n⏳ Скоро.",
    "💳 Мои оплаты":            "💳 <b>Мои оплаты</b>\n\n⏳ Скоро.",
}


# ── Revenue helpers ────────────────────────────────────────────────────────────

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
    return f"{amount:,}".replace(",", " ")  # неразрывный пробел как разделитель тысяч


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


# ── Handlers ───────────────────────────────────────────────────────────────────

async def _handle_start(
    session: aiohttp.ClientSession,
    chat_id: int,
    tg_id: int,
    first_name: str,
    param: str,
) -> None:
    master = _get_master_by_tg(tg_id)

    # Fallback: BEK_OWNER_ID из .env (работает до первого seed_db с me.id из юзербота)
    if master is None and BEK_OWNER_ID and tg_id == BEK_OWNER_ID:
        master = {"slug": MASTER_SLUG, "name": "Бек", "role": "owner"}

    # Первая привязка мастера: /start <slug>  (например /start ali)
    if master is None and param and param in MASTERS_SEED and param != MASTER_SLUG:
        if _link_master(param, tg_id):
            info = MASTERS_SEED[param]
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

    if master["role"] == "owner":
        _BEK_CHAT_ID_FILE.write_text(str(chat_id))
        print(f"[MANAGE] owner /start: chat_id={chat_id}")
        await _send(session, chat_id,
            f"👋 Привет, {first_name}!\n"
            f"Вы вошли как <b>владелец</b>.\n\n"
            f"Управляющий бот готов к работе.",
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

    role = master["role"]
    main_kb = _OWNER_KB if role == "owner" else _MASTER_KB

    # ── Кнопка "Назад" из любого подменю ─────────────────────────────────────
    if text == "🔙 В меню":
        await _send(session, chat_id, "Главное меню:", keyboard=main_kb)
        return

    # ── Открыть подменю выручки ───────────────────────────────────────────────
    if text in ("💰 Выручка", "💰 Моя выручка"):
        await _send(session, chat_id, "Выберите период:", keyboard=_REVENUE_KB)
        return

    # ── Период выручки ────────────────────────────────────────────────────────
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

    # ── Stubs для остальных пунктов ───────────────────────────────────────────
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
        param = text[len("/start"):].strip().lstrip("_")  # /start ali или /start
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

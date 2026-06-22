import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from openai import AsyncOpenAI

from db import (
    init_db, seed_db, SERVICES_SEED, SERVICES_BY_SLUG, MASTER_SLUG,
    create_booking, is_slot_available, get_free_slots,
    get_upcoming_reminders, mark_reminded,
    get_pending_master_notifications, mark_master_notified,
)

_HERE = Path(__file__).parent
load_dotenv(_HERE / ".env")

# ================== КОНФИГ ==================
API_ID   = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE    = os.environ["TG_PHONE"]
OPENAI_CLIENT = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
SESSION_NAME = str(_HERE / "bek_salon_tdlib")  # абсолютный путь — нужен для systemd

# Отдельный бот для уведомлений Беку о записях с сайта.
# Не принимает клиентов, не связан с юзерботом — только «система → Бек».
BEK_NOTIFY_BOT_TOKEN = os.environ.get("BEK_NOTIFY_BOT_TOKEN", "")
BEK_CHAT_ID: int | None = None  # задать вручную или обнаружится из /start Бека

MSK = ZoneInfo("Europe/Moscow")
_BEK_CHAT_ID_FILE = _HERE / "bek_chat_id.txt"

init_db()


# ================== Утилиты ==================

SERVICE_NAMES = ", ".join(sorted(SERVICES_SEED.keys()))

INFO_TRIGGERS = {
    'услуги', 'услуг', 'прайс', 'цены', 'цен', 'стоимость',
    'что умеешь', 'что делаешь', 'информация', 'инфо', 'info', 'список',
}


def services_menu() -> str:
    lines = ["Услуги барбершопа «Стрижём и Бреем»:\n"]
    for name, info in SERVICES_SEED.items():
        lines.append(f"• {name.capitalize()} — {info['price']} ₽  ({info['duration']} мин)")
    lines.append("\nНапишите услугу и удобное время, например:\n«стрижка завтра в 17:00»")
    return "\n".join(lines)


def looks_like_info_request(text: str) -> bool:
    t = text.lower()
    return any(tr in t for tr in INFO_TRIGGERS)


RECORD_TRIGGERS = {
    'хочу', 'записаться', 'запись', 'запиши', 'запишите',
    'стрижк', 'бород', 'моделирован', 'уход', 'маска', 'скраб', 'патч',
    'сегодня', 'завтра', 'послезавтра', 'понедельник', 'вторник', 'среду',
    'четверг', 'пятниц', 'суббот', 'воскресен',
    'утром', 'днём', 'вечером', 'после', 'до', 'в', 'на', 'время',
    'час', 'пол', ' :',
}


def looks_like_booking_intent(text: str) -> bool:
    t = text.lower()
    trigger_count = sum(1 for tr in RECORD_TRIGGERS if tr in t)
    has_time = bool(re.search(r'\d{1,2}[:.]?\d{2}', t))
    has_day  = any(w in t for w in ['сегодня', 'завтра', 'послезавтра'])
    return (
        trigger_count >= 2 or
        (trigger_count >= 1 and (has_time or has_day)) or
        (has_time and 'стриж' in t)
    )


def normalize_time(time_str: str) -> str:
    """Округляет до ближайшего 30-минутного слота."""
    try:
        if ':' not in time_str:
            time_str = time_str.replace('.', ':')
        h, m = map(int, time_str.split(':'))
        m = ((m + 15) // 30) * 30
        if m == 60:
            h += 1
            m = 0
        return f"{h:02d}:{m:02d}"
    except Exception:
        return time_str


def human_date(db_date: str) -> str:
    """2026-03-18 → 18.03.2026"""
    try:
        return datetime.strptime(db_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return db_date


async def analyze_message(text: str) -> dict:
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    prompt = f"""Ты администратор барбершопа "Стрижём и Бреем", адрес Филёвский бульвар 39, Москва.
Сегодня: {today}

График: каждый день 10:00–22:00 (без выходных!)

Услуги (выбирай только из этого списка, если ничего похожего — ставь null):
{SERVICE_NAMES}

Если клиент НЕ назвал конкретную услугу — по умолчанию подразумевай "мужская стрижка".

Сообщение: {text}

Верни ТОЛЬКО валидный JSON без ```json, без лишнего текста и без комментариев:

{{
  "intent": "record" | "cancel" | "info" | "other",
  "service": "точное название из списка или null",
  "date": "YYYY-MM-DD" или null,
  "time": "HH:MM" (ближайшее 30-минутное) или null,
  "name": имя клиента или null
}}

Не пиши ничего кроме JSON!
"""
    try:
        resp = await OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
            max_tokens=300,
        )
        parsed = json.loads(resp.choices[0].message.content.strip())
        return parsed if isinstance(parsed, dict) else {"intent": "other"}
    except Exception as e:
        print(f"[GPT] Ошибка анализа: {e}")
        return {"intent": "other"}


# ================== Уведомления Беку (Вариант B) ==================
# Отдельный бот пишет ТОЛЬКО Беку. Юзербот (Telethon) — канал клиентов.
# BEK_NOTIFY_BOT_TOKEN и _bek_chat_id полностью изолированы от клиентского потока.

_bek_chat_id: int | None = BEK_CHAT_ID


def _load_cached_chat_id() -> int | None:
    if _BEK_CHAT_ID_FILE.exists():
        try:
            return int(_BEK_CHAT_ID_FILE.read_text().strip())
        except ValueError:
            return None
    return None


async def notify_bek_via_bot(html_text: str) -> bool:
    """Отправляет HTML-сообщение Беку через бот уведомлений (Bot API)."""
    if not BEK_NOTIFY_BOT_TOKEN or not _bek_chat_id:
        return False
    url = f"https://api.telegram.org/bot{BEK_NOTIFY_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"chat_id": _bek_chat_id, "text": html_text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[NOTIFY] Bot API {resp.status}: {body[:200]}")
                return resp.status == 200
    except Exception as e:
        print(f"[NOTIFY] Ошибка Bot API: {e}")
        return False


async def _discover_bek_chat_id_loop() -> None:
    """Поллит бот уведомлений, ждёт /start от Бека, сохраняет chat_id в файл."""
    global _bek_chat_id
    if not BEK_NOTIFY_BOT_TOKEN:
        print("[NOTIFY] BEK_NOTIFY_BOT_TOKEN не задан — уведомления Беку отключены")
        return

    print("[NOTIFY] Жду /start от Бека в боте уведомлений...")
    url    = f"https://api.telegram.org/bot{BEK_NOTIFY_BOT_TOKEN}/getUpdates"
    offset = 0

    async with aiohttp.ClientSession() as session:
        while _bek_chat_id is None:
            try:
                async with session.get(
                    url,
                    params={"offset": offset, "timeout": 0, "limit": 20},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message", {})
                        if msg.get("text", "").startswith("/start"):
                            _bek_chat_id = msg["chat"]["id"]
                            _BEK_CHAT_ID_FILE.write_text(str(_bek_chat_id))
                            print(f"[NOTIFY] BEK_CHAT_ID = {_bek_chat_id} (сохранён в {_BEK_CHAT_ID_FILE})")
                            return
            except Exception as e:
                print(f"[NOTIFY] getUpdates error: {e}")
            await asyncio.sleep(30)


# ================== Telegram-клиент (юзербот) ==================
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

conversations: dict[int, dict] = {}  # user_id → {service, date, time, expires}


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    if not event.is_private:
        return

    user_id = None
    try:
        sender  = await event.get_sender()
        user_id = sender.id
        text    = (event.text or "").strip()

        if not text:
            return

        print(f"[{datetime.now(MSK):%H:%M:%S}] от {user_id}: {text[:80]!r}")
        await asyncio.sleep(random.uniform(1.3, 3.8))

        # ─── Ожидаем подтверждение ────────────────────────────────────────────
        if user_id in conversations:
            if conversations[user_id].get('expires', datetime.min) < datetime.now(MSK).replace(tzinfo=None):
                del conversations[user_id]
            else:
                lower = text.lower().strip()
                positive = {'да', 'yes', 'подтверждаю', 'ок', 'окей', 'записывай', 'запись', 'давай'}
                negative = {'нет', 'no', 'отмена', 'не надо', 'не хочу', 'отменить'}

                if lower in positive or lower.startswith('да'):
                    rec = conversations[user_id]
                    try:
                        result = create_booking(
                            date=rec['date'],
                            time_start=rec['time'],
                            services=[rec['service_slug']],
                            telegram_id=user_id,
                            client_name=sender.first_name or None,
                            source="bot",
                        )
                        await event.reply(
                            f"✅ Записал вас на {result['time_start']} {human_date(result['date'])} "
                            f"на «{rec['service_name']}»\n"
                            "Напоминание придёт за час до визита."
                        )
                    except ValueError as e:
                        await event.reply(f"Не удалось записать: {e}\nПопробуйте другое время.")
                    finally:
                        del conversations[user_id]
                    return

                elif lower in negative or lower.startswith('нет'):
                    await event.reply("Запись отменена. Если передумаете — напишите снова.")
                    del conversations[user_id]
                    return

                else:
                    await event.reply("Пожалуйста, ответьте **да** или **нет**")
                    return

        # ─── Инфо-запрос (услуги, цены) — приоритет перед фильтром брони ────────
        if looks_like_info_request(text):
            await event.reply(services_menu())
            return

        # ─── Фильтр намерения ─────────────────────────────────────────────────
        if len(text) < 10 or not looks_like_booking_intent(text):
            await event.reply(
                "Привет! Я ассистент барбершопа «Стрижём и Бреем».\n\n"
                "Напишите, **в какой день и на какое время** хотите записаться.\n"
                "Примеры:\n"
                "• завтра 17:30\n"
                "• в пятницу после обеда\n"
                "• 23 марта 13:00 стрижка\n\n"
                "Работаем **ежедневно** 10:00–22:00"
            )
            return

        # ─── Разбор через GPT ─────────────────────────────────────────────────
        analysis = await analyze_message(text)

        if analysis.get("intent") == "info":
            await event.reply(services_menu())
            return

        if analysis.get("intent") not in ("record", None):
            await event.reply(
                "Пока я помогаю только с записью на услуги.\n"
                "Напишите желаемую дату и время, например:\n"
                "завтра 15:30\nпятница 18:00\n20.03 в 13:00"
            )
            return

        service_name = analysis.get("service")
        if not service_name or service_name not in SERVICES_SEED:
            service_name = "мужская стрижка"
        service_slug = SERVICES_SEED[service_name]["slug"]

        date_str = analysis.get("date")
        time_raw = analysis.get("time")

        if not date_str or not time_raw:
            await event.reply(
                "Не удалось понять дату и время.\n"
                "Попробуйте написать например:\n"
                "завтра в 16:30\n"
                "пятница 17:00\n"
                "23 марта 14:00"
            )
            return

        time_str = normalize_time(time_raw)

        if not ("10:00" <= time_str <= "22:00"):
            await event.reply(
                "Салон работает ежедневно с 10:00 до 22:00.\n"
                "Выберите пожалуйста время в этом диапазоне."
            )
            return

        try:
            req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            now_msk  = datetime.now(MSK)
            today    = now_msk.date()
            if req_date < today:
                await event.reply("Нельзя записаться в прошлое. Выберите дату от сегодня и позже.")
                return
            if req_date == today and time_str <= now_msk.strftime("%H:%M"):
                await event.reply("На сегодня это время уже прошло. Выберите другое или завтра.")
                return
        except ValueError:
            await event.reply("Не понял дату. Попробуйте: завтра, пятница, 23.03")
            return

        # ─── Проверка доступности ─────────────────────────────────────────────
        duration = SERVICES_SEED[service_name]["duration"]
        price    = SERVICES_SEED[service_name]["price"]

        if is_slot_available(MASTER_SLUG, date_str, time_str, duration):
            conversations[user_id] = {
                'service_name': service_name,
                'service_slug': service_slug,
                'date':         date_str,
                'time':         time_str,
                'expires':      datetime.now(MSK).replace(tzinfo=None) + timedelta(minutes=7),
            }
            await event.reply(
                f"Свободно:\n"
                f"📅 {human_date(date_str)}  {time_str}\n"
                f"✂️ {service_name}\n"
                f"⏱ {duration} мин   💰 {price} ₽\n\n"
                "**Подтверждаете** запись?  (да / нет)"
            )
        else:
            alts = get_free_slots(MASTER_SLUG, date_str, duration, limit=6)
            if alts:
                alt_text = ', '.join(alts[:4]) + (' и др.' if len(alts) > 4 else '')
                await event.reply(
                    f"Это время занято.\n"
                    f"Свободные окна на {human_date(date_str)}:\n"
                    f"{alt_text}\n\n"
                    "Напишите желаемое время."
                )
            else:
                await event.reply(
                    f"На {human_date(date_str)} свободных окон нет.\n"
                    "Попробуйте другой день."
                )

    except Exception as e:
        print(f"[ERROR] handler: {e}")
        if user_id is not None and user_id in conversations:
            del conversations[user_id]
            await event.reply("Произошла ошибка. Давайте начнём заново — напишите день и время.")


# ================== Фоновые задачи ==================

async def reminder_loop():
    """Отправляет клиентам напоминание за час до записи (через юзербот)."""
    while True:
        await asyncio.sleep(60)
        try:
            for booking in get_upcoming_reminders():
                tg_id = booking["telegram_id"]
                if not tg_id:
                    continue
                dt = datetime.strptime(booking["start_time"], "%Y-%m-%d %H:%M")
                await client.send_message(
                    tg_id,
                    f"Напоминание: ваша запись на {dt.strftime('%H:%M')} "
                    f"{dt.strftime('%d.%m.%Y')}"
                )
                mark_reminded(booking["booking_id"])
                print(f"[REMINDER] Клиент {tg_id}, запись #{booking['booking_id']}")
        except Exception as e:
            print(f"[ERROR] reminder_loop: {e}")
            await asyncio.sleep(10)


async def master_notification_loop():
    """Уведомляет Бека о новых записях с сайта через отдельный бот (Вариант B).
    Не использует юзербот и не пишет клиентам."""
    while True:
        await asyncio.sleep(30)
        try:
            for n in get_pending_master_notifications():
                dt   = datetime.strptime(n["start_time"], "%Y-%m-%d %H:%M")
                dt_e = datetime.strptime(n["end_time"],   "%Y-%m-%d %H:%M")
                dur  = SERVICES_SEED.get(n["service"], {}).get("duration", "?")
                name  = n["client_name"] or "—"
                phone = n["phone"] or "нет"

                text = (
                    f"📲 <b>Запись с сайта</b>\n\n"
                    f"👤 {name}\n"
                    f"📞 {phone}\n"
                    f"📅 {dt.strftime('%d.%m.%Y')}  "
                    f"<b>{dt.strftime('%H:%M')}–{dt_e.strftime('%H:%M')}</b>\n"
                    f"✂️ {n['service']}\n"
                    f"⏱ {dur} мин  💰 {n['price']} ₽"
                )

                sent = await notify_bek_via_bot(text)
                if sent:
                    mark_master_notified(n["booking_id"])
                    print(f"[NOTIFY] Бек уведомлён о записи #{n['booking_id']}")
                else:
                    print(f"[NOTIFY] Не удалось уведомить Бека о #{n['booking_id']} — повтор через 30с")
        except Exception as e:
            print(f"[ERROR] master_notification_loop: {e}")
            await asyncio.sleep(10)


# ================== Запуск ==================

async def main():
    global _bek_chat_id
    print("Запуск клиента...")

    await client.connect()
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("Введите код из Telegram: ")
        try:
            await client.sign_in(PHONE, code)
        except Exception as e:
            if "password" in str(e).lower():
                pw = input("Введите пароль 2FA: ")
                await client.sign_in(password=pw)

    me = await client.get_me()
    print(f"\n✅ Клиент запущен как: {me.first_name} ({me.phone})")

    seed_db(master_telegram_id=me.id)

    # Разрешение BEK_CHAT_ID: конфиг → файл → авто-обнаружение
    if _bek_chat_id is None:
        _bek_chat_id = _load_cached_chat_id()
    if _bek_chat_id:
        print(f"[NOTIFY] BEK_CHAT_ID = {_bek_chat_id}")
    else:
        asyncio.create_task(_discover_bek_chat_id_loop())

    asyncio.create_task(reminder_loop())
    asyncio.create_task(master_notification_loop())

    print("Бот запущен и слушает личные сообщения...")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())

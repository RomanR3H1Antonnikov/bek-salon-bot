import asyncio
import json
import os
import random
import re
import time
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
    list_masters,
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

# ================== Защита от петель и флуд-бана ==================

# Служебные Telegram ID — никогда не отвечаем.
# Добавь ID «Кошелька» когда выяснишь (написать /id боту или найти в логах).
IGNORED_IDS: frozenset[int] = frozenset({
    777000,   # Telegram Service (системные уведомления)
})

_RATE_LIMIT_WINDOW: int = 60   # секунд
_RATE_LIMIT_MAX:    int = 3    # ответов одному sender за окно
_rate_history: dict[int, list[float]] = {}

_CB_WINDOW:   int = 60    # секунд скользящего окна
_CB_MAX:      int = 20    # максимум исходящих за окно
_CB_COOLDOWN: int = 300   # секунд паузы после срабатывания

_outgoing_times: list[float] = []
EMERGENCY_STOP: bool = False

# ─── Рубильник автоответов ─────────────────────────────────────────────
# BEK_OWNER_ID — Telegram user_id владельца (тот, кто пишет /answersoff).
# Если не задан, команда недоступна (лучше задать в .env сразу).
BEK_OWNER_ID: int | None = (
    int(os.environ["BEK_OWNER_ID"]) if os.environ.get("BEK_OWNER_ID") else None
)
_AUTO_ANSWER_FILE = _HERE / "auto_answer.txt"

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
    'тонировк', 'завивк', 'чистк',
    'сегодня', 'завтра', 'послезавтра', 'понедельник', 'вторник', 'среду',
    'четверг', 'пятниц', 'суббот', 'воскресен',
    'утром', 'днём', 'вечером', 'после обеда', 'время',
    'час', 'пол', ' :',
}
# Убраны 'в', 'на', 'до' — слишком короткие, дают ложные срабатывания внутри других слов.


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
  "intent": "record" | "cancel" | "info" | "availability" | "other",
  "service": "точное название из списка или null",
  "date": "YYYY-MM-DD" или null,
  "time": "HH:MM" (ближайшее 30-минутное) или null,
  "name": имя клиента или null
}}

intent — правила:
- "record": клиент хочет записаться (есть дата/время/услуга).
- "availability": клиент спрашивает, какое время свободно на конкретную дату.
- "info": клиент спрашивает про услуги или цены.
- "cancel": клиент хочет отменить запись.
- "other": всё остальное.
Если время вне рабочих часов — всё равно ставь "record" и указывай то время, что назвал клиент.

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


# ================== Защита: rate-limit, circuit-breaker, safe_reply ==================

def _check_rate_limit(sender_id: int) -> bool:
    """False если sender прислал ≥ _RATE_LIMIT_MAX сообщений за последнюю минуту."""
    now = time.monotonic()
    times = [t for t in _rate_history.get(sender_id, []) if now - t < _RATE_LIMIT_WINDOW]
    if len(times) >= _RATE_LIMIT_MAX:
        _rate_history[sender_id] = times
        return False
    times.append(now)
    _rate_history[sender_id] = times
    return True


async def _emergency_reset() -> None:
    await asyncio.sleep(_CB_COOLDOWN)
    global EMERGENCY_STOP
    EMERGENCY_STOP = False
    _outgoing_times.clear()
    print("[CB] EMERGENCY_STOP сброшен, бот возобновляет работу")


async def safe_reply(event, text: str) -> None:
    """Отправляет ответ клиенту и считает исходящие в circuit-breaker.
    Если EMERGENCY_STOP или лимит превышен — молча прерывает."""
    global EMERGENCY_STOP
    if EMERGENCY_STOP:
        return
    now = time.monotonic()
    while _outgoing_times and now - _outgoing_times[0] > _CB_WINDOW:
        _outgoing_times.pop(0)
    _outgoing_times.append(now)
    if len(_outgoing_times) > _CB_MAX:
        EMERGENCY_STOP = True
        asyncio.create_task(_emergency_reset())
        asyncio.create_task(notify_bek_via_bot(
            f"⚠️ <b>Circuit breaker</b>: юзербот превысил лимит "
            f"({_CB_MAX}+ исходящих за {_CB_WINDOW}с). "
            f"Пауза {_CB_COOLDOWN // 60} мин."
        ))
        print(f"[CB] EMERGENCY_STOP=True — {len(_outgoing_times)} исходящих за {_CB_WINDOW}с")
        return
    _send = event.reply
    await _send(text)


def _load_auto_answer() -> bool:
    if _AUTO_ANSWER_FILE.exists():
        return _AUTO_ANSWER_FILE.read_text().strip() != "0"
    return True


def _save_auto_answer(state: bool) -> None:
    _AUTO_ANSWER_FILE.write_text("1" if state else "0")


AUTO_ANSWER_ENABLED: bool = _load_auto_answer()


# ================== Telegram-клиент (юзербот) ==================
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

conversations: dict[int, dict] = {}  # user_id → {service, date, time, expires}


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    if not event.is_private:
        return
    if event.out:          # собственные исходящие (incoming=True уже фильтрует, но defence-in-depth)
        return
    if EMERGENCY_STOP:     # circuit-breaker сработал — ждём авто-сброса
        return

    user_id = None
    try:
        sender  = await event.get_sender()
        user_id = sender.id
        text    = (event.text or "").strip()

        if not text:
            return

        # ─── Фильтры отправителя (до любой логики и GPT) ─────────────────────
        if user_id in IGNORED_IDS:
            return
        if getattr(sender, 'bot', False):   # корень инцидента с «Кошельком»
            return

        # ─── Команды владельца (до rate-limit, чтобы не расходовать квоту) ──────
        if BEK_OWNER_ID and user_id == BEK_OWNER_ID:
            global AUTO_ANSWER_ENABLED
            if text.lower() == "/answersoff":
                AUTO_ANSWER_ENABLED = False
                _save_auto_answer(False)
                await safe_reply(event, "Автоответы выключены. Приём записей и система работают.")
                print("[SWITCH] AUTO_ANSWER_ENABLED = False")
                return
            if text.lower() == "/answerson":
                AUTO_ANSWER_ENABLED = True
                _save_auto_answer(True)
                await safe_reply(event, "Автоответы включены.")
                print("[SWITCH] AUTO_ANSWER_ENABLED = True")
                return

        if not _check_rate_limit(user_id):
            print(f"[RATE] {user_id} превысил лимит — игнор")
            return

        print(f"[{datetime.now(MSK):%H:%M:%S}] от {user_id}: {text[:80]!r}")

        # ─── Рубильник автоответов ────────────────────────────────────────────
        # Логируем и обнаруживаем намерение записи — но клиенту не отвечаем.
        if not AUTO_ANSWER_ENABLED:
            if looks_like_booking_intent(text):
                name = getattr(sender, 'first_name', None) or f"id{user_id}"
                link = (f" (@{sender.username})" if getattr(sender, 'username', None) else "")
                asyncio.create_task(notify_bek_via_bot(
                    f"🔕 <b>Автоответы выключены</b>\n"
                    f"Клиент {name}{link} написал про запись — посмотри личку."
                ))
            return

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
                        manage_url = (
                            f"https://barber.rehy.store/b/{result['manage_token']}"
                        )
                        await safe_reply(event,
                            f"✅ Записал вас на {result['time_start']} {human_date(result['date'])} "
                            f"на «{rec['service_name']}»\n"
                            "Напоминание придёт за час до визита.\n\n"
                            f"🔗 Управление записью:\n{manage_url}"
                        )
                        asyncio.create_task(notify_bek_via_bot(
                            f"📲 <b>Новая запись (бот)</b>\n"
                            f"👤 {sender.first_name or '—'}"
                            + (f" @{sender.username}" if getattr(sender, 'username', None) else "") + "\n"
                            f"✂️ {rec['service_name']}\n"
                            f"📅 {human_date(result['date'])}  {result['time_start']}–{result['time_end']}\n"
                            f"💰 {result['total_price']} ₽"
                        ))
                    except ValueError as e:
                        await safe_reply(event,f"Не удалось записать: {e}\nПопробуйте другое время.")
                    finally:
                        del conversations[user_id]
                    return

                elif lower in negative or lower.startswith('нет'):
                    await safe_reply(event,"Запись отменена. Если передумаете — напишите снова.")
                    del conversations[user_id]
                    return

                else:
                    await safe_reply(event,"Пожалуйста, ответьте **да** или **нет**")
                    return

        # ─── Инфо-запрос (услуги, цены) — приоритет перед фильтром брони ────────
        if looks_like_info_request(text):
            await safe_reply(event,services_menu())
            return

        # ─── Фильтр намерения ─────────────────────────────────────────────────
        if len(text) < 10 or not looks_like_booking_intent(text):
            await safe_reply(event,
                "Привет! Я ассистент барбершопа «Стрижём и Бреем».\n\n"
                "Напишите, **в какой день и на какое время** хотите записаться.\n"
                "Примеры:\n"
                "• завтра 17:30\n"
                "• в пятницу после обеда\n"
                "• 23 марта 13:00 стрижка\n\n"
                "Работаем **ежедневно** 10:00–22:00"
            )
            return

        # ─── Пред-проверка времени (до GPT, без лишнего вызова API) ──────────
        _hour_m = re.search(r'\b(\d{1,2})[:.]\d{2}\b', text)
        if _hour_m:
            _h = int(_hour_m.group(1))
            # Проверяем только валидные часы (0-23). 25, 67 и т.п. — это не время, а дата.
            if 0 <= _h <= 23 and (_h >= 22 or _h < 10):
                await safe_reply(event,"Салон работает с 10:00 до 22:00. Выберите другое время!")
                return

        # ─── Разбор через GPT ─────────────────────────────────────────────────
        analysis = await analyze_message(text)

        if analysis.get("intent") == "info":
            await safe_reply(event,services_menu())
            return

        if analysis.get("intent") == "availability":
            avail_date = analysis.get("date")
            if not avail_date:
                await safe_reply(event,
                    "На какой день смотреть свободные окна?\n"
                    "Напишите, например: завтра, пятница, 25 июня"
                )
                return
            free = get_free_slots(MASTER_SLUG, avail_date, duration_min=30, limit=0)
            # Фильтр по времени суток, если клиент уточнил
            tl = text.lower()
            if 'вечер' in tl:
                free = [s for s in free if s >= "18:00"]
            elif 'утр' in tl:
                free = [s for s in free if s < "13:00"]
            elif 'обед' in tl or 'днём' in tl or 'дн ' in tl:
                free = [s for s in free if "13:00" <= s < "18:00"]
            if free:
                shown = free[:12]
                tail  = " и др." if len(free) > 12 else ""
                await safe_reply(event,
                    f"Свободные окна на {human_date(avail_date)}:\n"
                    f"{', '.join(shown)}{tail}\n\n"
                    "Напишите удобное время — и запишу."
                )
            else:
                await safe_reply(event,
                    f"На {human_date(avail_date)} свободных окон нет.\n"
                    "Попробуйте другой день."
                )
            return

        if analysis.get("intent") not in ("record", None):
            await safe_reply(event,
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
            await safe_reply(event,
                "Не удалось понять дату и время.\n"
                "Попробуйте написать например:\n"
                "завтра в 16:30\n"
                "пятница 17:00\n"
                "23 марта 14:00"
            )
            return

        time_str = normalize_time(time_raw)
        duration = SERVICES_SEED[service_name]["duration"]
        price    = SERVICES_SEED[service_name]["price"]

        # Проверяем и старт, и конец визита относительно рабочего дня.
        _end_check = (
            datetime.strptime(f"2000-01-01 {time_str}", "%Y-%m-%d %H:%M")
            + timedelta(minutes=duration)
        )
        if time_str < "10:00" or _end_check.strftime("%H:%M") > "22:00":
            _last = (datetime(2000, 1, 1, 22, 0) - timedelta(minutes=duration)).strftime("%H:%M")
            await safe_reply(event,
                f"Салон работает с 10:00 до 22:00.\n"
                f"Для «{service_name}» ({duration} мин) последний старт — {_last}.\n"
                f"Выберите другое время."
            )
            return

        try:
            req_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            now_msk  = datetime.now(MSK)
            today    = now_msk.date()
            if req_date < today:
                await safe_reply(event,"Нельзя записаться в прошлое. Выберите дату от сегодня и позже.")
                return
            if req_date == today and time_str <= now_msk.strftime("%H:%M"):
                await safe_reply(event,"На сегодня это время уже прошло. Выберите другое или завтра.")
                return
        except ValueError:
            await safe_reply(event,"Не понял дату. Попробуйте: завтра, пятница, 23.03")
            return

        # ─── Проверка доступности ─────────────────────────────────────────────

        if is_slot_available(MASTER_SLUG, date_str, time_str, duration):
            conversations[user_id] = {
                'service_name': service_name,
                'service_slug': service_slug,
                'date':         date_str,
                'time':         time_str,
                'expires':      datetime.now(MSK).replace(tzinfo=None) + timedelta(minutes=7),
            }
            await safe_reply(event,
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
                await safe_reply(event,
                    f"Это время занято.\n"
                    f"Свободные окна на {human_date(date_str)}:\n"
                    f"{alt_text}\n\n"
                    "Напишите желаемое время."
                )
            else:
                await safe_reply(event,
                    f"На {human_date(date_str)} свободных окон нет.\n"
                    "Попробуйте другой день."
                )

    except Exception as e:
        print(f"[ERROR] handler: {e}")
        if user_id is not None and user_id in conversations:
            del conversations[user_id]
            await safe_reply(event,"Произошла ошибка. Давайте начнём заново — напишите день и время.")


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

                master_line = (
                    f"\n👨‍💼 {n['master_name']}"
                    if n.get("master_name") else ""
                )
                text = (
                    f"📲 <b>Запись с сайта</b>\n\n"
                    f"👤 {name}\n"
                    f"📞 {phone}\n"
                    f"📅 {dt.strftime('%d.%m.%Y')}  "
                    f"<b>{dt.strftime('%H:%M')}–{dt_e.strftime('%H:%M')}</b>\n"
                    f"✂️ {n['service']}"
                    f"{master_line}\n"
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

    # BEK_CHAT_ID — из файла (записывается manage_bot.py при /start от Бека)
    if _bek_chat_id is None:
        _bek_chat_id = _load_cached_chat_id()
    if _bek_chat_id:
        print(f"[NOTIFY] BEK_CHAT_ID = {_bek_chat_id}")
    else:
        print("[NOTIFY] bek_chat_id.txt не найден — запусти manage_bot.py и отправь /start")

    asyncio.create_task(reminder_loop())
    asyncio.create_task(master_notification_loop())

    print("Бот запущен и слушает личные сообщения...")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())

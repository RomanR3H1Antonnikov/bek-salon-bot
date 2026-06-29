"""
FastAPI-слой для записи с сайта.
Слушает 127.0.0.1:8000, наружу через nginx.
Два эндпоинта: GET /slots, POST /book.
Все запросы требуют заголовка X-Internal-Token.
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from db import (
    MASTER_SLUG, MASTERS_SEED,
    SlotTaken,
    create_booking, cancel_booking, reschedule_booking, get_booking_by_token,
    get_free_slots, list_masters,
    init_db, sum_duration,
)

# ================== КОНФИГ ==================
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
PORT           = 8000
HOST           = "127.0.0.1"
MSK            = ZoneInfo("Europe/Moscow")

init_db()

# ================== AUTH ==================

async def verify_token(x_internal_token: Annotated[str, Header()]) -> None:
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=500, detail="INTERNAL_TOKEN не задан на сервере")
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# ================== Нормализация телефона ==================
# Только здесь, на входе FastAPI. Бот и create_booking принимают уже нормализованный.

_PHONE_RE = re.compile(r"\D")

def normalize_phone(raw: str) -> str:
    """7-495-123-45-67 / 89161234567 / +79161234567 → +79161234567"""
    digits = _PHONE_RE.sub("", raw)
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7{digits[1:]}"
    raise ValueError(f"Неверный формат телефона: {raw!r} (ожидается 10 или 11 цифр)")


# ================== Эндпоинты ==================

app = FastAPI(title="Salon Booking API", docs_url=None, redoc_url=None)


def _check_master(master_id: str) -> None:
    if master_id not in MASTERS_SEED:
        raise HTTPException(
            status_code=400,
            detail=f"Неизвестный мастер: {master_id!r}. Доступны: {list(MASTERS_SEED.keys())}",
        )


@app.get("/masters")
async def get_masters(_: None = Depends(verify_token)):
    """Список мастеров салона."""
    return {"masters": list_masters()}


@app.get("/slots")
async def get_slots(
    date:      str,
    master_id: str       = MASTER_SLUG,
    services:  list[str] = Query(default=[]),
    _: None = Depends(verify_token),
):
    """
    Свободные стартовые времена HH:MM для заданного дня.
    services=mens-haircut&services=beard-modeling → длительность суммируется.
    Без services → дефолтные 30 мин.
    """
    _check_master(master_id)

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат date (YYYY-MM-DD)")

    if services:
        try:
            duration_min = sum_duration(services)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        duration_min = 30

    slots = get_free_slots(master_slug=master_id, date=date, duration_min=duration_min, limit=0)
    return {"date": date, "duration_min": duration_min, "master_id": master_id, "slots": slots}


class BookRequest(BaseModel):
    date:      str        # YYYY-MM-DD
    time:      str        # HH:MM
    services:  list[str]  # список slug'ов; длительность суммируется
    name:      str        # обязателен: Бек должен знать клиента
    phone:     str        # обязателен: нормализуется здесь перед create_booking
    master_id: str = MASTER_SLUG


@app.post("/book")
async def book(
    req: BookRequest,
    _:   None = Depends(verify_token),
):
    """
    Создаёт запись с сайта. Вызывает create_booking(source='site').

    Ответы:
      200 — запись создана
      400 — невалидные данные (формат, неизвестный slug, нерабочее время)
      403 — неверный токен
      409 — slot_taken (время занято)
      500 — внутренняя ошибка
    """
    _check_master(req.master_id)

    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name не может быть пустым")

    try:
        datetime.strptime(req.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат date (YYYY-MM-DD)")

    try:
        datetime.strptime(req.time, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат time (HH:MM)")

    if not req.services:
        raise HTTPException(status_code=400, detail="services не может быть пустым")

    try:
        phone_norm = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = create_booking(
            master_slug=req.master_id,
            date=req.date,
            time_start=req.time,
            services=req.services,
            client_name=req.name.strip(),
            phone=phone_norm,
            source="site",
        )
        return result

    except SlotTaken as e:
        raise HTTPException(status_code=409, detail={"code": "slot_taken", "message": str(e)})

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        print(f"[API] Ошибка /book: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


# ── Управление записью по токену ──────────────────────────────────────────────


@app.get("/booking/{token}")
async def get_booking(token: str, _: None = Depends(verify_token)):
    """
    Информация о брони по manage_token.
    status: "active" | "cancelled" | "past"
    can_cancel: сервер считает правило 15 мин — фронт не дублирует логику.
    404 если токен не найден (никогда не возвращаем «не найден, потому что отменена»).
    """
    info = get_booking_by_token(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return info


class CancelRequest(BaseModel):
    token: str


@app.post("/cancel")
async def cancel(req: CancelRequest, _: None = Depends(verify_token)):
    """
    Отмена брони по manage_token.
    409 — уже отменена / правило 15 мин.
    """
    info = get_booking_by_token(req.token)
    if info is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    try:
        result = cancel_booking(info["booking_id"], by_manage_token=req.token)
        return result
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


class RescheduleRequest(BaseModel):
    token:     str
    master_id: str       = MASTER_SLUG
    date:      str        # YYYY-MM-DD
    time:      str        # HH:MM
    services:  list[str] | None = None  # None → наследуем из старой брони


@app.post("/reschedule")
async def reschedule(req: RescheduleRequest, _: None = Depends(verify_token)):
    """
    Атомарный перенос брони по manage_token.
    Новая запись получает новый manage_token (старый перестаёт работать).
    409 — занятый слот (откат) / правило 15 мин / уже отменена.
    """
    _check_master(req.master_id)

    try:
        datetime.strptime(req.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат date (YYYY-MM-DD)")
    try:
        datetime.strptime(req.time, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат time (HH:MM)")

    info = get_booking_by_token(req.token)
    if info is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    try:
        result = reschedule_booking(
            info["booking_id"],
            new_master_slug=req.master_id,
            new_date=req.date,
            new_time=req.time,
            new_services=req.services,
            by_manage_token=req.token,
        )
        return result
    except SlotTaken as e:
        raise HTTPException(status_code=409, detail={"code": "slot_taken", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        print(f"[API] Ошибка /reschedule: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")


# ================== Запуск ==================

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

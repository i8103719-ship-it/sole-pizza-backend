import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
DATABASE_PATH = os.getenv("DATABASE_PATH", "orders.db")
ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "*")

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except ValueError:
    raise RuntimeError("ADMIN_ID должен быть числом")

ALLOWED_ORIGINS = (
    ["*"] if ALLOWED_ORIGINS_RAW.strip() == "*"
    else [origin.strip() for origin in ALLOWED_ORIGINS_RAW.split(",") if origin.strip()]
)

bot: Optional[Bot] = None
polling_task: Optional[asyncio.Task] = None

router = Router()
dp = Dispatcher()
dp.include_router(router)


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    quantity: int = Field(ge=1, le=100)
    price: float = Field(ge=0)


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=40)
    address: str = Field(min_length=3, max_length=500)
    user_id: int = Field(gt=0)
    items: list[OrderItem] = Field(min_length=1, max_length=50)
    total: float = Field(ge=0)


STATUS_LABELS = {
    "new": "Новый",
    "cooking": "Готовится",
    "courier": "У курьера",
    "delivered": "Доставлен",
}

STATUS_MESSAGES = {
    "cooking": "Ваша пицца готовится",
    "courier": "Ваша пицца в пути",
    "delivered": "Доставлено",
}

NEXT_STATUS = {
    "new": "cooking",
    "cooking": "courier",
    "courier": "delivered",
}

BUTTON_LABELS = {
    "cooking": "Начать готовить",
    "courier": "Передать курьеру",
    "delivered": "Доставлено",
}


async def init_db() -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                total REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()


def order_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup:
    next_status = NEXT_STATUS.get(status)
    if not next_status:
        return InlineKeyboardMarkup(inline_keyboard=[])

    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=BUTTON_LABELS[next_status],
                callback_data=f"order:{order_id}:{next_status}",
            )
        ]]
    )


async def get_order(order_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Бот доставки пиццы.\n"
        "Чтобы получать уведомления о заказе, оформляйте заказ через наше приложение."
    )


@router.callback_query(lambda callback: callback.data and callback.data.startswith("order:"))
async def order_status_callback(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("У вас нет доступа к управлению заказами.", show_alert=True)
        return

    try:
        _, order_id_raw, requested_status = callback.data.split(":")
        order_id = int(order_id_raw)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    order = await get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    expected_status = NEXT_STATUS.get(order["status"])
    if requested_status != expected_status:
        await callback.answer("Этот статус уже изменён или действие недоступно.", show_alert=True)
        return

    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "UPDATE orders SET status = ? WHERE id = ? AND status = ?",
            (requested_status, order_id, order["status"]),
        )
        await db.commit()
        changed = cursor.rowcount
        await cursor.close()

    if not changed:
        await callback.answer("Статус уже был изменён.", show_alert=True)
        return

    updated_order = await get_order(order_id)
    updated_text = (
        f"Заказ №{order_id}\n"
        f"Статус: {STATUS_LABELS[requested_status]}\n\n"
        f"Имя: {order['name']}\n"
        f"Телефон: {order['phone']}\n"
        f"Адрес: {order['address']}\n"
        f"Сумма: {order['total']:.2f}"
    )

    try:
        if requested_status in STATUS_MESSAGES:
            await bot.send_message(
                chat_id=order["user_id"],
                text=STATUS_MESSAGES[requested_status],
            )
    except Exception:
        logger.exception("Не удалось отправить уведомление клиенту заказа %s", order_id)

    try:
        await callback.message.edit_text(
            updated_text,
            reply_markup=order_keyboard(order_id, requested_status),
        )
    except Exception:
        logger.exception("Не удалось обновить сообщение администратора")

    await callback.answer("Статус заказа обновлён.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, polling_task

    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    if ADMIN_ID is None:
        raise RuntimeError("Не задана переменная окружения ADMIN_ID")

    await init_db()
    bot = Bot(token=BOT_TOKEN)

    # Бот получает обновления через long polling в том же процессе, что и API.
    polling_task = asyncio.create_task(dp.start_polling(bot))

    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Ошибка при остановке polling")
        if bot:
            await bot.session.close()
            bot = None


app = FastAPI(
    title="Pizza Delivery API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOWED_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"service": "Pizza Delivery API", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/order", status_code=201)
async def create_order(payload: CreateOrderRequest):
    if bot is None or ADMIN_ID is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not initialized")

    import json

    items_data = [item.model_dump() for item in payload.items]
    items_json = json.dumps(items_data, ensure_ascii=False)
    created_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO orders
                (name, phone, address, user_id, items_json, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                payload.name,
                payload.phone,
                payload.address,
                payload.user_id,
                items_json,
                payload.total,
                created_at,
            ),
        )
        await db.commit()
        order_id = cursor.lastrowid
        await cursor.close()

    item_lines = [
        f"• {item.name} × {item.quantity} — {item.price * item.quantity:.2f}"
        for item in payload.items
    ]
    admin_text = (
        f"НОВЫЙ ЗАКАЗ №{order_id}\n\n"
        f"Имя: {payload.name}\n"
        f"Телефон: {payload.phone}\n"
        f"Адрес: {payload.address}\n"
        f"Telegram ID клиента: {payload.user_id}\n\n"
        f"Состав заказа:\n" + "\n".join(item_lines) +
        f"\n\nИтого: {payload.total:.2f}\n"
        f"Статус: {STATUS_LABELS['new']}"
    )

    admin_notified = True
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            reply_markup=order_keyboard(order_id, "new"),
        )
    except Exception:
        admin_notified = False
        logger.exception("Не удалось отправить заказ №%s администратору", order_id)

    return {
        "success": True,
        "order_id": order_id,
        "status": "new",
        "admin_notified": admin_notified,
    }


@app.get("/api/order/{order_id}")
async def get_order_endpoint(order_id: int):
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    import json

    order["items"] = json.loads(order.pop("items_json"))
    return order

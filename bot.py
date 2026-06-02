import os
import asyncio
import logging
from datetime import datetime, timedelta
import httpx
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
WB_TOKEN = os.environ.get("WB_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))

WB_ADV = "https://advert-api.wildberries.ru"
WB_STAT = "https://statistics-api.wildberries.ru"

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

pending = {}

def h():
    return {"Authorization": WB_TOKEN, "Content-Type": "application/json"}

async def wget(path, base=WB_ADV):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(base + path, headers=h())
        r.raise_for_status()
        return r.json()

async def wpost(path, body, base=WB_ADV):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(base + path, headers=h(), json=body)
        r.raise_for_status()
        return r.json()

def ok(uid):
    return ALLOWED_USER == 0 or uid == ALLOWED_USER

@router.message(Command("start"))
async def start(m: Message):
    if not ok(m.from_user.id): return
    await m.answer(
        "Привет! Я твой WB менеджер 🤖\n\n"
        "/report — P&L отчёт\n"
        "/ads — анализ рекламы\n"
        "/stock — склады\n"
        "/top — стратегия топа\n"
        "/help — все команды"
    )

@router.message(Command("report"))
async def report(m: Message):
    if not ok(m.from_user.id): return
    msg = await m.answer("Загружаю данные...")
    try:
        df = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        orders = await wget(f"/api/v1/supplier/orders?dateFrom={df}", WB_STAT)
        sales = await wget(f"/api/v1/supplier/sales?dateFrom={df}", WB_STAT)
        rev = sum(o.get("totalPrice", 0) for o in (sales or []))
        cnt = len(orders) if orders else 0
        comm = round(rev * 0.15)
        log = round(rev * 0.07)
        camps = await wget("/adv/v1/promotion/count")
        ids = []
        for g in (camps.get("adverts") or []):
            for a in g.get("advert_list", []):
                ids.append(a["advertId"])
        adv = 0
        if ids:
            today = datetime.now().strftime("%Y-%m-%d")
            st = await wpost("/adv/v1/fullstats", [{"id": i, "dates": [today]} for i in ids[:10]])
            adv = sum(s.get("sum", 0) for s in (st or []))
        profit = rev - comm - log - adv
        drr = round(adv / rev * 100, 1) if rev else 0
        await msg.edit_text(
            f"📊 P&L — {datetime.now().strftime('%d.%m.%Y')}\n\n"
            f"💰 Выручка: {rev:,.0f}₽\n"
            f"📦 Заказов: {cnt} шт\n"
            f"📢 Реклама: {adv:,.0f}₽\n"
            f"🚚 Логистика: {log:,.0f}₽\n"
            f"🏪 Комиссия: {comm:,.0f}₽\n"
            f"📈 ДРР: {drr}%\n\n"
            f"{'🟢' if profit > 0 else '🔴'} Прибыль: {profit:,.0f}₽"
        )
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")

@router.message(Command("ads"))
async def ads(m: Message):
    if not ok(m.from_user.id): return
    msg = await m.answer("Анализирую кампании...")
    try:
        camps = await wget("/adv/v1/promotion/count")
        ids = []
        for g in (camps.get("adverts") or []):
            for a in g.get("advert_list", []):
                ids.append(a["advertId"])
        if not ids:
            await msg.edit_text("Активных кампаний не найдено.")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        stats = await wpost("/adv/v1/fullstats", [{"id": i, "dates": [today]} for i in ids[:10]])
        actions = []
        text = "📢 Анализ кампаний\n\n"
        for s in (stats or []):
            name = s.get("name", f"#{s.get('advertId','')}")
            bid = s.get("cpm", 0)
            views = s.get("views", 0)
            clicks = s.get("clicks", 0)
            ctr = round(clicks / views * 100, 2) if views else 0
            pos = s.get("position", 99)
            if pos > 10:
                new_bid = round(bid * 1.15)
                reason = f"Позиция #{pos} — поднять"
                act = "up"
            elif ctr < 0.5:
                new_bid = round(bid * 0.9)
                reason = f"CTR {ctr}% — снизить"
                act = "down"
            else:
                new_bid = bid
                reason = "В норме"
                act = "keep"
            icon = "🟢" if act == "keep" else ("🔼" if act == "up" else "🔽")
            text += f"{icon} {name}\n   {bid}₽"
            if act != "keep":
                text += f" → {new_bid}₽"
            text += f" | {reason}\n\n"
            if act != "keep":
                actions.append({"id": s.get("advertId"), "name": name, "bid": bid, "new_bid": new_bid})
        if actions:
            pending[m.from_user.id] = actions
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Применить", callback_data="apply"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
            ]])
            await msg.edit_text(text, reply_markup=kb)
        else:
            await msg.edit_text(text + "Все в норме.")
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")

@router.message(Command("stock"))
async def stock(m: Message):
    if not ok(m.from_user.id): return
    msg = await m.answer("Загружаю склады...")
    try:
        data = await wget("/api/v3/stocks/warehouses")
        items = data if isinstance(data, list) else data.get("stocks", [])
        if not items:
            await msg.edit_text("Нет данных. Нужен токен с правом Контент.")
            return
        text = "📦 Остатки\n\n"
        for s in items[:15]:
            qty = s.get("amount", s.get("quantity", 0))
            name = s.get("supplierArticle", s.get("nmId", "Товар"))
            wh = s.get("warehouseName", "—")
            icon = "🔴" if qty < 10 else ("🟡" if qty < 30 else "🟢")
            text += f"{icon} {name}: {qty} шт — {wh}\n"
            if qty < 10:
                text += f"   ⚠️ Срочная поставка!\n"
        await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")

@router.message(Command("top"))
async def top(m: Message):
    if not ok(m.from_user.id): return
    await m.answer(
        "🎯 Стратегия вывода в топ\n\n"
        "1. Реклама — ставки выше конкурентов\n"
        "2. Конверсия — CTR и % выкупа\n"
        "3. Склады — товар близко к покупателю\n"
        "4. Отзывы — рейтинг 4.5+\n"
        "5. SEO — ключи в названии\n\n"
        "Запусти /ads для плана."
    )

@router.message(Command("help"))
async def help_cmd(m: Message):
    if not ok(m.from_user.id): return
    await m.answer(
        "/report — P&L отчёт\n"
        "/ads — реклама\n"
        "/stock — склады\n"
        "/top — стратегия\n"
        "/help — справка"
    )

@router.callback_query(F.data == "apply")
async def apply(cb: CallbackQuery):
    acts = pending.get(cb.from_user.id, [])
    if not acts:
        await cb.answer("Нет действий")
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    msg = await cb.message.answer(f"Применяю {len(acts)} изменений...")
    done = 0
    for a in acts:
        try:
            await wpost(
                f"/adv/v0/cpm?advertId={a['id']}&type=8&cpm={a['new_bid']}",
                {"advertId": a["id"], "type": 8, "cpm": a["new_bid"], "param": 0}
            )
            done += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"Ошибка {a['id']}: {e}")
    await msg.edit_text(f"✅ Готово! Изменено {done} из {len(acts)}.")
    pending.pop(cb.from_user.id, None)
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cancel(cb: CallbackQuery):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Отменено.")
    await cb.answer()

@router.message()
async def echo(m: Message):
    if not ok(m.from_user.id): return
    t = m.text.lower()
    if any(w in t for w in ["отчёт", "отчет", "прибыль"]):
        await report(m)
    elif any(w in t for w in ["реклама", "ставки"]):
        await ads(m)
    elif any(w in t for w in ["склад", "остатки"]):
        await stock(m)
    else:
        await m.answer("Не понял. Попробуй /help")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

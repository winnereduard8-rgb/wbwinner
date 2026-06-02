import os
import asyncio
import logging
from datetime import datetime, timedelta
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
WB_TOKEN = os.environ.get("WB_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))

WB_ADV = "https://advert-api.wildberries.ru"
WB_STAT = "https://statistics-api.wildberries.ru"

pending = {}

def headers():
    return {"Authorization": WB_TOKEN, "Content-Type": "application/json"}

async def get(path, base=WB_ADV):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(base + path, headers=headers())
        r.raise_for_status()
        return r.json()

async def post(path, body, base=WB_ADV):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(base + path, headers=headers(), json=body)
        r.raise_for_status()
        return r.json()

def ok(update):
    return ALLOWED_USER == 0 or update.effective_user.id == ALLOWED_USER

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    await update.message.reply_text(
        "Привет! Я твой WB менеджер 🤖\n\n"
        "/report — отчёт P&L за сегодня\n"
        "/ads — анализ рекламы и ставок\n"
        "/stock — остатки на складах\n"
        "/top — стратегия вывода в топ\n"
        "/help — все команды"
    )

async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    m = await update.message.reply_text("Загружаю данные...")
    try:
        df = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        orders = await get(f"/api/v1/supplier/orders?dateFrom={df}", WB_STAT)
        sales = await get(f"/api/v1/supplier/sales?dateFrom={df}", WB_STAT)
        rev = sum(o.get("totalPrice", 0) for o in (sales or []))
        cnt = len(orders) if orders else 0
        comm = round(rev * 0.15)
        log = round(rev * 0.07)
        camps = await get("/adv/v1/promotion/count")
        ids = []
        for g in (camps.get("adverts") or []):
            for a in g.get("advert_list", []):
                ids.append(a["advertId"])
        adv = 0
        if ids:
            today = datetime.now().strftime("%Y-%m-%d")
            st = await post("/adv/v1/fullstats", [{"id": i, "dates": [today]} for i in ids[:10]])
            adv = sum(s.get("sum", 0) for s in (st or []))
        profit = rev - comm - log - adv
        drr = round(adv / rev * 100, 1) if rev else 0
        await m.edit_text(
            f"📊 P&L отчёт — {datetime.now().strftime('%d.%m.%Y')}\n\n"
            f"💰 Выручка: {rev:,.0f}₽\n"
            f"📦 Заказов: {cnt} шт\n"
            f"📢 Реклама: {adv:,.0f}₽\n"
            f"🚚 Логистика: {log:,.0f}₽\n"
            f"🏪 Комиссия WB: {comm:,.0f}₽\n"
            f"📈 ДРР: {drr}%\n\n"
            f"{'🟢' if profit > 0 else '🔴'} Чистая прибыль: {profit:,.0f}₽"
        )
    except Exception as e:
        await m.edit_text(f"Ошибка: {e}\nПроверь WB_TOKEN в Environment на Render.")

async def ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    m = await update.message.reply_text("Анализирую кампании...")
    try:
        camps = await get("/adv/v1/promotion/count")
        ids = []
        for g in (camps.get("adverts") or []):
            for a in g.get("advert_list", []):
                ids.append(a["advertId"])
        if not ids:
            await m.edit_text("Активных кампаний не найдено.")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        stats = await post("/adv/v1/fullstats", [{"id": i, "dates": [today]} for i in ids[:10]])
        actions = []
        text = "📢 Анализ рекламных кампаний\n\n"
        for s in (stats or []):
            name = s.get("name", f"Кампания #{s.get('advertId','')}")
            bid = s.get("cpm", 0)
            views = s.get("views", 0)
            clicks = s.get("clicks", 0)
            ctr = round(clicks / views * 100, 2) if views else 0
            pos = s.get("position", 99)
            if pos > 10:
                new_bid = round(bid * 1.15)
                reason = f"Позиция #{pos} — поднять для топ-10"
                act = "up"
            elif ctr < 0.5:
                new_bid = round(bid * 0.9)
                reason = f"CTR {ctr}% слабый — снизить ставку"
                act = "down"
            else:
                new_bid = bid
                reason = "В норме"
                act = "keep"
            icon = "🟢" if act == "keep" else ("🔼" if act == "up" else "🔽")
            text += f"{icon} {name}\n"
            text += f"   Ставка: {bid}₽"
            if act != "keep":
                text += f" → {new_bid}₽"
            text += f" | CTR: {ctr}%\n"
            text += f"   {reason}\n\n"
            if act != "keep":
                actions.append({"id": s.get("advertId"), "name": name, "bid": bid, "new_bid": new_bid})
        if actions:
            pending[update.effective_user.id] = actions
            kb = [[
                InlineKeyboardButton("✅ Применить", callback_data="apply_ads"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]]
            await m.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await m.edit_text(text + "Все кампании в норме.")
    except Exception as e:
        await m.edit_text(f"Ошибка: {e}")

async def stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    m = await update.message.reply_text("Загружаю склады...")
    try:
        data = await get("/api/v3/stocks/warehouses")
        items = data if isinstance(data, list) else data.get("stocks", [])
        if not items:
            await m.edit_text("Нет данных. Нужен токен с правом Контент.")
            return
        text = "📦 Остатки на складах\n\n"
        for s in items[:15]:
            qty = s.get("amount", s.get("quantity", 0))
            name = s.get("supplierArticle", s.get("nmId", "Товар"))
            wh = s.get("warehouseName", "—")
            icon = "🔴" if qty < 10 else ("🟡" if qty < 30 else "🟢")
            text += f"{icon} {name}: {qty} шт — {wh}\n"
            if qty < 10:
                text += f"   ⚠️ Срочная поставка!\n"
        await m.edit_text(text)
    except Exception as e:
        await m.edit_text(f"Ошибка: {e}")

async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    await update.message.reply_text(
        "🎯 Стратегия вывода в топ\n\n"
        "WB ранжирует по:\n"
        "1. Реклама — ставки выше конкурентов\n"
        "2. Конверсия — CTR и % выкупа\n"
        "3. Склады — товар близко к покупателю\n"
        "4. Отзывы — рейтинг 4.5+\n"
        "5. SEO — ключи в названии\n\n"
        "Запусти /ads чтобы я составил план."
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    await update.message.reply_text(
        "Команды:\n\n"
        "/report — P&L отчёт\n"
        "/ads — анализ рекламы\n"
        "/stock — склады\n"
        "/top — стратегия топа\n"
        "/help — справка"
    )

async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "cancel":
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Отменено.")
    elif q.data == "apply_ads":
        acts = pending.get(uid, [])
        if not acts:
            await q.message.reply_text("Нет действий.")
            return
        await q.edit_message_reply_markup(None)
        m = await q.message.reply_text(f"Применяю {len(acts)} изменений...")
        done = 0
        for a in acts:
            try:
                await post(
                    f"/adv/v0/cpm?advertId={a['id']}&type=8&cpm={a['new_bid']}",
                    {"advertId": a["id"], "type": 8, "cpm": a["new_bid"], "param": 0}
                )
                done += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Ошибка {a['id']}: {e}")
        await m.edit_text(f"✅ Готово! Изменено {done} из {len(acts)} кампаний.")
        pending.pop(uid, None)

async def message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ok(update): return
    t = update.message.text.lower()
    if any(w in t for w in ["отчёт", "отчет", "прибыль", "выручка"]):
        await report(update, ctx)
    elif any(w in t for w in ["реклама", "ставки", "кампани"]):
        await ads(update, ctx)
    elif any(w in t for w in ["склад", "остатки"]):
        await stock(update, ctx)
    else:
        await update.message.reply_text("Не понял. Попробуй /help")

def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("ads", ads))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

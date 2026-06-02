import os, asyncio, logging, json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
WB_TOKEN = os.environ.get("WB_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))

WB_ADV = "https://advert-api.wildberries.ru"
WB_STAT = "https://statistics-api.wildberries.ru"

pending_actions = {}

def wb_headers():
    return {"Authorization": WB_TOKEN, "Content-Type": "application/json"}

async def wb_get(path: str, base: str = WB_ADV):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(base + path, headers=wb_headers())
        r.raise_for_status()
        return r.json()

async def wb_post(path: str, body: dict, base: str = WB_ADV):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(base + path, headers=wb_headers(), json=body)
        r.raise_for_status()
        return r.json()

def check_user(update: Update) -> bool:
    return ALLOWED_USER == 0 or update.effective_user.id == ALLOWED_USER

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    await update.message.reply_text(
        "Привет! Я твой WB менеджер.\n\n"
        "Команды:\n"
        "/report — P&L отчёт за сегодня\n"
        "/ads — анализ рекламы\n"
        "/stock — остатки на складах\n"
        "/top — стратегия вывода в топ\n"
        "/help — все команды"
    )

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    msg = await update.message.reply_text("Загружаю данные...")
    try:
        date_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        orders = await wb_get(f"/api/v1/supplier/orders?dateFrom={date_from}", WB_STAT)
        sales = await wb_get(f"/api/v1/supplier/sales?dateFrom={date_from}", WB_STAT)

        revenue = sum(o.get("totalPrice", 0) for o in (sales if sales else []))
        orders_count = len(orders) if orders else 0
        commission = round(revenue * 0.15)
        logistics = round(revenue * 0.07)

        camps = await wb_get("/adv/v1/promotion/count")
        adv_ids = []
        if camps.get("adverts"):
            for g in camps["adverts"]:
                for a in g.get("advert_list", []):
                    adv_ids.append(a["advertId"])

        adv_cost = 0
        if adv_ids:
            today = datetime.now().strftime("%Y-%m-%d")
            stats = await wb_post("/adv/v1/fullstats",
                [{"id": i, "dates": [today]} for i in adv_ids[:10]])
            adv_cost = sum(s.get("sum", 0) for s in (stats or []))

        profit = revenue - commission - logistics - adv_cost
        drr = round(adv_cost / revenue * 100, 1) if revenue else 0

        text = (
            f"📊 *P&L отчёт — {datetime.now().strftime('%d.%m.%Y')}*\n\n"
            f"💰 Выручка: *{revenue:,.0f}₽*\n"
            f"📦 Заказов: *{orders_count} шт*\n"
            f"📢 Реклама: *{adv_cost:,.0f}₽*\n"
            f"🚚 Логистика: *{logistics:,.0f}₽*\n"
            f"🏪 Комиссия WB: *{commission:,.0f}₽*\n"
            f"📈 ДРР: *{drr}%*\n\n"
            f"{'🟢' if profit > 0 else '🔴'} Чистая прибыль: *{profit:,.0f}₽*"
        )
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка загрузки данных: {e}\nПроверь WB_TOKEN в настройках Render.")

async def cmd_ads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    msg = await update.message.reply_text("Анализирую кампании...")
    try:
        camps = await wb_get("/adv/v1/promotion/count")
        adv_ids = []
        if camps.get("adverts"):
            for g in camps["adverts"]:
                for a in g.get("advert_list", []):
                    adv_ids.append(a["advertId"])

        if not adv_ids:
            await msg.edit_text("Активных кампаний не найдено.")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        stats = await wb_post("/adv/v1/fullstats",
            [{"id": i, "dates": [today]} for i in adv_ids[:10]])

        actions = []
        text = "📢 *Анализ рекламных кампаний*\n\n"
        for s in (stats or []):
            name = s.get("name", f"Кампания #{s.get('advertId','')}")
            bid = s.get("cpm", 0)
            ctr = round(s.get("clicks", 0) / s.get("views", 1) * 100, 2) if s.get("views") else 0
            drr = 0
            pos = s.get("position", 99)

            if pos > 10:
                new_bid = round(bid * 1.15)
                reason = f"Позиция #{pos} — поднять для топ-10"
                action = "up"
            elif drr > 30:
                new_bid = round(bid * 0.85)
                reason = f"ДРР высокий — снизить"
                action = "down"
            else:
                new_bid = bid
                reason = "В норме"
                action = "keep"

            icon = "🟢" if action == "keep" else ("🔼" if action == "up" else "🔽")
            text += f"{icon} *{name}*\n"
            text += f"   Ставка: {bid}₽"
            if action != "keep":
                text += f" → {new_bid}₽"
            text += f" | CTR: {ctr}%\n"
            text += f"   {reason}\n\n"

            if action != "keep":
                actions.append({"id": s.get("advertId"), "name": name, "bid": bid, "new_bid": new_bid, "reason": reason})

        if actions:
            pending_actions[update.effective_user.id] = {"type": "ads", "actions": actions}
            keyboard = [[
                InlineKeyboardButton("✅ Применить всё", callback_data="confirm_ads"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]]
            await msg.edit_text(text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await msg.edit_text(text + "Все кампании в норме.", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")

async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    msg = await update.message.reply_text("Загружаю склады...")
    try:
        stocks = await wb_get("/api/v3/stocks/warehouses")
        text = "📦 *Остатки на складах*\n\n"
        items = stocks if isinstance(stocks, list) else stocks.get("stocks", [])
        if not items:
            await msg.edit_text("Нет данных по остаткам. Проверь права токена (нужен доступ к Контенту).")
            return
        for s in items[:15]:
            qty = s.get("amount", s.get("quantity", 0))
            name = s.get("supplierArticle", s.get("nmId", "Товар"))
            wh = s.get("warehouseName", "—")
            icon = "🔴" if qty < 10 else ("🟡" if qty < 30 else "🟢")
            text += f"{icon} *{name}*: {qty} шт — {wh}\n"
            if qty < 10:
                text += f"   ⚠️ Критично мало — срочная поставка!\n"
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка загрузки складов: {e}")

async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    await update.message.reply_text(
        "🎯 *Стратегия вывода в топ*\n\n"
        "Для выхода в топ WB учитывает:\n\n"
        "1️⃣ *Реклама* — ставки выше конкурентов по ключевым запросам\n"
        "2️⃣ *Конверсия* — CTR карточки и % выкупа\n"
        "3️⃣ *Остатки* — товар должен быть на складах рядом с покупателем\n"
        "4️⃣ *Отзывы* — рейтинг 4.5+ критичен\n"
        "5️⃣ *SEO* — ключи в названии и описании\n\n"
        "Запусти /ads чтобы я проанализировал ставки и составил план.",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    await update.message.reply_text(
        "*Все команды:*\n\n"
        "/report — ежедневный P&L отчёт\n"
        "/ads — анализ и оптимизация ставок\n"
        "/stock — остатки на складах\n"
        "/top — стратегия вывода в топ\n"
        "/help — эта справка\n\n"
        "Бот также отвечает на вопросы в свободной форме.",
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "cancel":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("Отменено.")
        return

    if query.data == "confirm_ads":
        actions = pending_actions.get(uid, {}).get("actions", [])
        if not actions:
            await query.message.reply_text("Нет действий для выполнения.")
            return
        await query.edit_message_reply_markup(None)
        msg = await query.message.reply_text(f"Применяю {len(actions)} изменений...")
        done = 0
        for a in actions:
            try:
                await wb_post(
                    f"/adv/v0/cpm?advertId={a['id']}&type=8&cpm={a['new_bid']}",
                    {"advertId": a["id"], "type": 8, "cpm": a["new_bid"], "param": 0}
                )
                done += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Ошибка ставки {a['id']}: {e}")
        await msg.edit_text(f"✅ Готово! Изменено {done} из {len(actions)} кампаний.")
        pending_actions.pop(uid, None)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not check_user(update): return
    text = update.message.text.lower()
    if any(w in text for w in ["отчёт", "отчет", "прибыль", "выручка"]):
        await cmd_report(update, ctx)
    elif any(w in text for w in ["реклама", "ставки", "кампани"]):
        await cmd_ads(update, ctx)
    elif any(w in text for w in ["склад", "остатки", "товар"]):
        await cmd_stock(update, ctx)
    else:
        await update.message.reply_text(
            "Не понял команду. Попробуй:\n/report /ads /stock /top /help"
        )

async def daily_report(ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER:
        update_mock = type('U', (), {'effective_user': type('U2', (), {'id': ALLOWED_USER})(), 'message': None})()
        chat = await ctx.bot.get_chat(ALLOWED_USER)
        # Send via bot directly
        try:
            date_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
            sales = await wb_get(f"/api/v1/supplier/sales?dateFrom={date_from}", WB_STAT)
            revenue = sum(o.get("totalPrice", 0) for o in (sales or []))
            commission = round(revenue * 0.15)
            logistics = round(revenue * 0.07)
            profit = revenue - commission - logistics
            await ctx.bot.send_message(
                ALLOWED_USER,
                f"🌅 *Доброе утро! Отчёт за вчера*\n\n"
                f"💰 Выручка: *{revenue:,.0f}₽*\n"
                f"🟢 Прибыль: *{profit:,.0f}₽*\n\n"
                f"Отправь /ads чтобы я проверил рекламу",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Daily report error: {e}")

def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("ads", cmd_ads))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily report at 9:00
    app.job_queue.run_daily(daily_report, time=datetime.strptime("09:00", "%H:%M").time())

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

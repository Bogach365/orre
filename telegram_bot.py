"""
Telegram-бот моніторингу OREE collector.

Дві функції:
  1. Відповідає на команди (/status, /last, /gaps, /today) — опитує БД.
  2. Push-сповіщення викликаються з run_daily.sh через функцію send_alert
     (або просто curl, див. run_daily.sh).

Запуск як постійний сервіс (systemd) — див. RUNBOOK.md, Частина J.

Залежності: python-telegram-bot, asyncpg
    pip install 'python-telegram-bot>=21' asyncpg

Змінні середовища:
    TG_TOKEN   — токен від @BotFather
    TG_CHAT    — ваш chat_id (бот пише тільки сюди; захист від чужих)
    OREE_DSN   — рядок підключення до PostgreSQL
"""

from __future__ import annotations

import os
import logging
from datetime import date, timedelta

import asyncpg
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("oree.bot")

TG_TOKEN = os.environ["TG_TOKEN"]
ALLOWED_CHAT = str(os.environ.get("TG_CHAT", ""))
OREE_DSN = os.environ.get("OREE_DSN", "postgresql://oree:CHANGE_ME@localhost:5432/oree")

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=OREE_DSN, min_size=1, max_size=2)
    return _pool


def _authorized(update: Update) -> bool:
    """Бот відповідає тільки своєму власнику."""
    if not ALLOWED_CHAT:
        return True  # якщо не налаштовано — відповідати всім (не рекомендовано)
    return str(update.effective_chat.id) == ALLOWED_CHAT


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "OREE collector monitor\n\n"
        "/status — загальний стан збору\n"
        "/last — деталі останнього збору\n"
        "/today — чи зібрано за сьогодні\n"
        "/gaps — пропущені дні за 90 днів\n"
        "/help — ця довідка"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(DISTINCT delivery_date) AS days,
                MIN(delivery_date) AS first_day,
                MAX(delivery_date) AS last_day
            FROM dam_clearing
        """)
        curve_count = await conn.fetchval("SELECT COUNT(*) FROM dam_curves")
        fails = await conn.fetchval("""
            SELECT COUNT(*) FROM ingestion_log
            WHERE status = 'fail' AND finished_at > now() - interval '7 days'
        """)
    if not row or row["days"] == 0:
        await update.message.reply_text("⚠ База порожня — збір ще не виконувався.")
        return
    msg = (
        f"📊 OREE collector\n\n"
        f"Днів зібрано: {row['days']}\n"
        f"Період: {row['first_day']} → {row['last_day']}\n"
        f"Точок кривих: {curve_count:,}\n"
        f"Помилок за 7 днів: {fails}"
    )
    await update.message.reply_text(msg)


async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT delivery_date, status, points_count, error, finished_at
            FROM ingestion_log
            ORDER BY finished_at DESC
            LIMIT 1
        """)
    if not row:
        await update.message.reply_text("Журнал збору порожній.")
        return
    icon = {"ok": "✅", "empty": "➖", "fail": "❌"}.get(row["status"], "❓")
    msg = (
        f"{icon} Останній збір\n\n"
        f"Дата постачання: {row['delivery_date']}\n"
        f"Статус: {row['status']}\n"
        f"Точок: {row['points_count'] or 0}\n"
        f"Час: {row['finished_at']:%Y-%m-%d %H:%M}"
    )
    if row["error"]:
        msg += f"\nПомилка: {row['error']}"
    await update.message.reply_text(msg)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    yesterday = date.today() - timedelta(days=1)
    pool = await get_pool()
    async with pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM dam_clearing WHERE delivery_date = $1",
            yesterday,
        )
    if cnt and cnt > 0:
        await update.message.reply_text(f"✅ Дані за {yesterday} є ({cnt} годин).")
    else:
        await update.message.reply_text(f"⚠ Даних за {yesterday} ще немає.")


async def cmd_gaps(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH expected AS (
                SELECT generate_series(
                    CURRENT_DATE - 90, CURRENT_DATE - 1, '1 day'
                )::date AS d
            )
            SELECT e.d
            FROM expected e
            LEFT JOIN (SELECT DISTINCT delivery_date FROM dam_clearing) c
                ON c.delivery_date = e.d
            WHERE c.delivery_date IS NULL
            ORDER BY e.d
        """)
    if not rows:
        await update.message.reply_text("✅ За останні 90 днів пропусків немає.")
        return
    gaps = ", ".join(str(r["d"]) for r in rows[:30])
    extra = f"\n… і ще {len(rows) - 30}" if len(rows) > 30 else ""
    await update.message.reply_text(f"⚠ Пропущено {len(rows)} днів:\n{gaps}{extra}")


def main() -> None:
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("gaps", cmd_gaps))
    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

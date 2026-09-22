"""
Voice-based personal finance tracker bot.

Design notes (see README for the full spec this implements):
- Runs only while your machine is on. No missed transactions: Telegram's
  own update queue holds messages while the bot is offline, and we resume
  from the last processed update on startup.
- The update offset is persisted to SQLite and advanced only after each
  update has been fully handled (saved / flagged / command executed), so a
  crash mid-batch re-delivers that one update rather than losing it.
- Speech-to-text (Moonshine) and categorization (local Ollama model) both
  run fully offline/locally.
- A message that can't be parsed gets flagged back to you as a reply asking
  you to resend; the rest of the batch keeps processing. Your resend must be
  a reply to that specific prompt so it's matched to the right slot.
- Deletion is by transaction ID (text command), not inline buttons, since
  the bot may not be online when you'd want to press one.
"""
import asyncio
import calendar
import csv
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Bot, Update, InputFile, ReplyKeyboardMarkup, KeyboardButton, BotCommand
from telegram.error import TelegramError

import config
import db
import llm
import stt
from charts import make_category_pie, format_summary_caption, format_amount, format_balance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Weekly Expenses"), KeyboardButton("📅 Monthly Expenses")],
        [KeyboardButton("💰 Balance"), KeyboardButton("📁 Transaction History (CSV)")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

HELP_TEXT = (
    "💸 <b>Voice & Text Finance Tracker</b>\n\n"
    "Log your finances by <b>voice note</b> or by <b>typing text</b>!\n\n"
    "📝 <b>Examples:</b>\n"
    "  • <i>\"i spent 10 k to lunch\"</i>\n"
    "  • <i>\"bought coffee 15k\"</i>\n"
    "  • <i>\"salary 3 million\"</i>\n"
    "  • <i>\"taxi 20,000\"</i>\n\n"
    "🔘 <b>Menu Buttons:</b>\n"
    "  📊 <b>Weekly Expenses</b> — Last 7 days breakdown & pie chart\n"
    "  📅 <b>Monthly Expenses</b> — Month-to-date breakdown & pie chart\n"
    "  💰 <b>Balance</b> — Overall income, expenses & net savings\n"
    "  📁 <b>Transaction History (CSV)</b> — Export all transactions to CSV\n\n"
    "⌨️ <b>Commands:</b>\n"
    "  /weekly, /monthly, /balance, /export, /history\n"
    "  /delete &lt;id&gt; — e.g. <code>/delete 5</code>\n"
)

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_DELETE_PATTERNS = [
    re.compile(r"^/delete\s+#?[a-zA-Z]*0*(\d+)\s*$", re.IGNORECASE),
    re.compile(r"(?:delete|remove)\s+(?:transaction|txn|entry)?\s*#?[a-zA-Z]*0*(\d+)", re.IGNORECASE),
    re.compile(r"(?:delete|remove)\s+(?:transaction|txn|entry)?\s*#?\s*(one|two|three|four|five|six|seven|eight|nine|ten)\b", re.IGNORECASE),
]


def try_parse_delete_command(text: str) -> int | None:
    text = text.strip()
    for pattern in _DELETE_PATTERNS:
        m = pattern.search(text)
        if m:
            val = m.group(1).lower()
            if val in _WORD_TO_NUM:
                return _WORD_TO_NUM[val]
            return int(val)
    return None


def format_saved_message(tx_id: int, parsed: dict) -> str:
    icon = "📥" if parsed["type"] == "income" else "📤"
    type_label = parsed["type"].capitalize()
    return (
        f"{icon} <b>Saved #{tx_id} ({type_label})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Amount:</b>      <code>{format_amount(parsed['amount'])}</code>\n"
        f"📂 <b>Category:</b>    {parsed['category']}\n"
        f"📝 <b>Description:</b> {parsed['description']}"
    )


def format_history(rows) -> str:
    if not rows:
        return "No transactions yet."
    lines = ["🧾 Recent transactions:"]
    for row in rows:
        icon = "📥" if row["type"] == "income" else "📤"
        lines.append(
            f"{icon} #{row['id']} | {format_amount(row['amount'])} | "
            f"{row['category']} | {row['description'] or ''}"
        )
    lines.append("\n💡 Tap '📁 Transaction History (CSV)' or send /export for full CSV file.")
    return "\n".join(lines)


def generate_transactions_csv(rows: list, timezone_str: str = "Asia/Tashkent") -> bytes:
    """
    Exports SQLite transaction rows to CSV bytes (with UTF-8 BOM for Excel).
    """
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID",
        "Date & Time",
        "Type",
        f"Amount ({config.CURRENCY_LABEL})",
        "Category",
        "Description",
        "Original Voice/Text",
    ])

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = timezone.utc

    for row in rows:
        created_at_raw = row["created_at"]
        try:
            dt = datetime.fromisoformat(created_at_raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_dt = dt.astimezone(tz)
            date_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            date_str = str(created_at_raw)

        writer.writerow([
            row["id"],
            date_str,
            row["type"].capitalize(),
            f"{row['amount']:.2f}",
            row["category"],
            row["description"] or "",
            row["raw_text"] or "",
        ])

    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _period_bounds_month(now: datetime) -> tuple[str, str]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), now.isoformat()


def _period_bounds_week(now: datetime) -> tuple[str, str]:
    start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), now.isoformat()


async def send_period_summary(
    bot: Bot, label: str, start_iso: str, end_iso: str, chat_id: int | None = None
):
    target_chat = chat_id if chat_id is not None else config.OWNER_CHAT_ID
    totals = db.get_totals(start_iso=start_iso, end_iso=end_iso)
    category_totals = db.get_category_totals("expense", start_iso=start_iso, end_iso=end_iso)
    caption = format_summary_caption(label, totals, category_totals)

    png = make_category_pie(category_totals, f"{label} spending by category")
    if png:
        await bot.send_photo(
            target_chat,
            photo=InputFile(png, filename="summary.png"),
            caption=caption,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await bot.send_message(
            target_chat,
            caption + "\n\n(No expenses to chart.)",
            reply_markup=MAIN_KEYBOARD,
        )


# --------------------------------------------------------------- core logic

async def handle_delete(bot: Bot, chat_id: int, tx_id: int):
    tx = db.get_transaction(tx_id)
    if tx is None:
        await bot.send_message(chat_id, f"⚠️ No active transaction with ID #{tx_id}.", reply_markup=MAIN_KEYBOARD)
        return
    db.soft_delete_transaction(tx_id)
    await bot.send_message(
        chat_id,
        f"🗑️ Deleted #{tx_id} ({tx['type']}, {format_amount(tx['amount'])}, {tx['category']}).",
        reply_markup=MAIN_KEYBOARD,
    )


async def send_history(bot: Bot, chat_id: int):
    rows = db.get_history(limit=20)
    await bot.send_message(chat_id, format_history(rows), reply_markup=MAIN_KEYBOARD)


async def send_balance(bot: Bot, chat_id: int):
    totals = db.get_totals()
    await bot.send_message(chat_id, format_balance(totals), reply_markup=MAIN_KEYBOARD)


async def send_transactions_csv(bot: Bot, chat_id: int):
    rows = db.get_all_transactions()
    if not rows:
        await bot.send_message(
            chat_id,
            "ℹ️ No transactions recorded yet.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    csv_bytes = generate_transactions_csv(rows, timezone_str=config.TIMEZONE)
    tz = ZoneInfo(config.TIMEZONE)
    now_dt = datetime.now(tz)
    filename = f"transactions_{now_dt.strftime('%Y%m%d_%H%M%S')}.csv"

    totals = db.get_totals()
    caption = (
        f"📁 <b>Transaction History Export</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Total Records: <b>{len(rows)}</b>\n"
        f"💰 Net Balance: <b>{format_amount(totals['balance'])}</b>\n"
        f"📅 Generated: {now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Format: CSV (Excel Compatible)"
    )

    await bot.send_document(
        chat_id,
        document=InputFile(csv_bytes, filename=filename),
        caption=caption,
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def send_weekly_summary(bot: Bot, chat_id: int):
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    week_start, week_end = _period_bounds_week(now)
    await send_period_summary(bot, "Weekly", week_start, week_end, chat_id=chat_id)


async def send_monthly_summary(bot: Bot, chat_id: int):
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    month_start, month_end = _period_bounds_month(now)
    await send_period_summary(bot, "Monthly", month_start, month_end, chat_id=chat_id)


async def handle_unclear(bot: Bot, chat_id: int, message_id: int, existing_prompt_id: int | None):
    """Flag a message as unparseable and ask the user to resend it."""
    if existing_prompt_id is not None:
        db.remove_pending_resend(existing_prompt_id)

    sent = await bot.send_message(
        chat_id,
        "⚠️ I couldn't understand that as a transaction. "
        "Please reply to THIS message if you want to retry it.",
        reply_to_message_id=message_id,
        reply_markup=MAIN_KEYBOARD,
    )
    db.add_pending_resend(sent.message_id, chat_id, message_id)


async def process_transaction_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    pending_prompt_id: int | None,
) -> str:
    """
    Runs STT-text -> LLM parse -> save. Returns "saved" or "failed".
    """
    parsed = llm.parse_transaction_with_retry(text, attempts=config.MAX_PARSE_ATTEMPTS)

    if parsed is None:
        await handle_unclear(bot, chat_id, message_id, pending_prompt_id)
        return "failed"

    tx_id = db.insert_transaction(
        telegram_message_id=message_id,
        type_=parsed["type"],
        amount=parsed["amount"],
        category=parsed["category"],
        description=parsed["description"],
        raw_text=text,
    )
    if pending_prompt_id is not None:
        db.remove_pending_resend(pending_prompt_id)

    await bot.send_message(
        chat_id,
        format_saved_message(tx_id, parsed),
        reply_to_message_id=message_id,
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )
    return "saved"


async def handle_update(bot: Bot, update: Update) -> str | None:
    """
    Returns "saved", "failed", or None (command / ignored / non-owner).
    """
    message = update.message
    if message is None:
        return None

    chat_id = message.chat_id
    if chat_id != config.OWNER_CHAT_ID:
        log.info("Ignoring message from non-owner chat_id=%s", chat_id)
        return None

    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    pending_prompt_id = None
    if message.reply_to_message is not None:
        pending = db.get_pending_resend(message.reply_to_message.message_id)
        if pending is not None:
            pending_prompt_id = pending["prompt_message_id"]

    # --- Voice message ---
    if message.voice is not None:
        try:
            tg_file = await bot.get_file(message.voice.file_id)
            audio_bytes = bytes(await tg_file.download_as_bytearray())
            text = stt.transcribe_voice(audio_bytes)
        except Exception:
            log.exception("STT failed for message_id=%s", message.message_id)
            await handle_unclear(bot, chat_id, message.message_id, pending_prompt_id)
            return "failed"

        if not text:
            await handle_unclear(bot, chat_id, message.message_id, pending_prompt_id)
            return "failed"

        lowered_voice = text.lower().strip()
        if pending_prompt_id is None:
            delete_id = try_parse_delete_command(lowered_voice)
            if delete_id is not None:
                await handle_delete(bot, chat_id, delete_id)
                return None

        return await process_transaction_text(
            bot, chat_id, message.message_id, text, pending_prompt_id
        )

    # --- Text message ---
    if message.text is not None:
        text = message.text.strip()
        lowered = text.lower()

        # Commands only apply when this isn't a resend-in-progress
        if pending_prompt_id is None:
            if lowered in ("/start", "/help", "help"):
                await bot.send_message(chat_id, HELP_TEXT, reply_markup=MAIN_KEYBOARD)
                return None
            if lowered in (
                "/weekly", "weekly", "weekly expenses", "📊 weekly expenses",
                "weekly expense", "📊 weekly",
            ):
                await send_weekly_summary(bot, chat_id)
                return None
            if lowered in (
                "/monthly", "monthly", "monthly expenses", "📅 monthly expenses",
                "monthly expense", "📅 monthly",
            ):
                await send_monthly_summary(bot, chat_id)
                return None
            if lowered in ("/balance", "balance", "💰 balance", "my balance", "show balance"):
                await send_balance(bot, chat_id)
                return None
            if lowered in (
                "/export", "/csv", "/history_csv",
                "📁 transaction history (csv)", "transaction history (csv)",
                "transaction history", "history csv", "export csv", "csv",
            ):
                await send_transactions_csv(bot, chat_id)
                return None
            if lowered in ("/history", "history", "show history", "recent history", "🧾 recent history"):
                await send_history(bot, chat_id)
                return None
            delete_id = try_parse_delete_command(lowered)
            if delete_id is not None:
                await handle_delete(bot, chat_id, delete_id)
                return None

        # Otherwise: treat the text itself as a transaction description
        # (also used when the user types a resend instead of recording voice)
        return await process_transaction_text(
            bot, chat_id, message.message_id, text, pending_prompt_id
        )

    # Anything else (photo, sticker, etc.) is ignored.
    return None


# ----------------------------------------------------------- summary jobs

async def maybe_send_periodic_summaries(bot: Bot):
    """
    Both the weekly and monthly summary fire together, on the last calendar
    day of the month (per spec). Tracked in bot_state so a restart on the
    same day doesn't resend it.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]

    if now.day != last_day_of_month:
        return

    month_key = now.strftime("%Y-%m")
    if db.get_state("last_monthly_summary") == month_key:
        return

    month_start, month_end = _period_bounds_month(now)
    week_start, week_end = _period_bounds_week(now)

    try:
        await send_period_summary(bot, "Monthly", month_start, month_end)
        await send_period_summary(bot, "Weekly", week_start, week_end)
        db.set_state("last_monthly_summary", month_key)
    except Exception:
        log.exception("Failed to send periodic summaries; will retry next check")


# --------------------------------------------------------------- main loop

async def main():
    db.init_db()
    bot = Bot(token=config.BOT_TOKEN)

    async with bot:
        try:
            await bot.set_my_commands([
                BotCommand("weekly", "Weekly expenses summary & chart"),
                BotCommand("monthly", "Monthly expenses summary & chart"),
                BotCommand("balance", "Check balance & financial overview"),
                BotCommand("export", "Export all transactions to CSV"),
                BotCommand("history", "Show recent transactions"),
                BotCommand("help", "Help & instructions"),
            ])
        except TelegramError:
            log.warning("Could not set bot commands")

        try:
            await bot.send_message(
                config.OWNER_CHAT_ID,
                "🟢 Back online. Processing any queued messages...",
                reply_markup=MAIN_KEYBOARD,
            )
        except TelegramError:
            log.exception("Could not send startup message (check OWNER_CHAT_ID)")

        offset = db.get_offset()
        last_summary_check = 0.0

        log.info("Starting long-poll loop from offset=%s", offset)

        while True:
            try:
                updates = await bot.get_updates(offset=offset, timeout=config.POLL_TIMEOUT)
            except TelegramError:
                log.exception("get_updates failed; backing off 5s")
                await asyncio.sleep(5)
                continue

            if updates:
                saved = 0
                failed = 0
                for update in updates:
                    try:
                        outcome = await handle_update(bot, update)
                        if outcome == "saved":
                            saved += 1
                        elif outcome == "failed":
                            failed += 1
                    except Exception:
                        log.exception("Unhandled error processing update_id=%s", update.update_id)
                        failed += 1
                    finally:
                        # Advance and persist the offset only after this
                        # update has been fully handled (or has definitively
                        # errored), one at a time, so a crash mid-batch
                        # resumes at exactly this update instead of skipping
                        # or endlessly repeating an earlier one.
                        offset = update.update_id + 1
                        db.set_offset(offset)

                if saved or failed:
                    summary = f"✅ Batch done: {saved} saved, {failed} flagged."
                    try:
                        await bot.send_message(config.OWNER_CHAT_ID, summary)
                    except TelegramError:
                        log.exception("Could not send batch summary")

            now_ts = time.time()
            if now_ts - last_summary_check > config.SUMMARY_CHECK_INTERVAL:
                await maybe_send_periodic_summaries(bot)
                last_summary_check = now_ts


if __name__ == "__main__":
    asyncio.run(main())

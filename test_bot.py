"""
Audit test suite. Run with: python3 test_bot.py
Uses a throwaway sqlite file (config.DB_PATH points at test_finance_bot.db)
and mocks Ollama / Moonshine / Telegram so no external services are needed.
"""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402

# Make sure we're pointed at the disposable test DB, not a real one.
assert "test_" in config.DB_PATH, "Refusing to run tests against a non-test DB_PATH"

import db  # noqa: E402
import llm  # noqa: E402
import charts  # noqa: E402
import bot as botmod  # noqa: E402


def fresh_db():
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    db.init_db()


class TestDB(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_insert_and_get(self):
        tx_id = db.insert_transaction(101, "expense", 10000, "Coffee", "morning coffee", "raw")
        self.assertIsNotNone(tx_id)
        row = db.get_transaction(tx_id)
        self.assertEqual(row["amount"], 10000)
        self.assertEqual(row["category"], "Coffee")

    def test_duplicate_message_id_is_idempotent(self):
        first = db.insert_transaction(101, "expense", 10000, "Coffee", "d", "raw")
        second = db.insert_transaction(101, "expense", 99999, "Other", "d2", "raw2")
        # Should return the SAME id, not create a duplicate row.
        self.assertEqual(first, second)
        rows = db.get_history(limit=10)
        self.assertEqual(len(rows), 1)

    def test_soft_delete_excludes_from_history_and_totals(self):
        tx_id = db.insert_transaction(1, "expense", 50000, "Food", "lunch", "raw")
        db.insert_transaction(2, "income", 2000000, "Salary", "salary", "raw")
        ok = db.soft_delete_transaction(tx_id)
        self.assertTrue(ok)
        # deleting again should report no-op
        self.assertFalse(db.soft_delete_transaction(tx_id))
        self.assertIsNone(db.get_transaction(tx_id))
        totals = db.get_totals()
        self.assertEqual(totals["expense"], 0)
        self.assertEqual(totals["income"], 2000000)
        self.assertEqual(totals["count"], 1)

    def test_category_totals_grouping(self):
        db.insert_transaction(1, "expense", 10000, "Coffee", "d", "raw")
        db.insert_transaction(2, "expense", 20000, "Coffee", "d", "raw")
        db.insert_transaction(3, "expense", 5000, "Transport", "d", "raw")
        totals = db.get_category_totals("expense")
        self.assertEqual(totals["Coffee"], 30000)
        self.assertEqual(totals["Transport"], 5000)

    def test_offset_persists(self):
        self.assertEqual(db.get_offset(), 0)
        db.set_offset(42)
        self.assertEqual(db.get_offset(), 42)

    def test_pending_resend_lifecycle(self):
        db.add_pending_resend(555, 111, 222)
        row = db.get_pending_resend(555)
        self.assertIsNotNone(row)
        self.assertEqual(row["chat_id"], 111)
        self.assertEqual(db.count_pending_resends(), 1)
        attempts = db.bump_pending_resend_attempts(555)
        self.assertEqual(attempts, 1)
        db.remove_pending_resend(555)
        self.assertIsNone(db.get_pending_resend(555))
        self.assertEqual(db.count_pending_resends(), 0)

    def test_history_ordering_and_limit(self):
        for i in range(5):
            db.insert_transaction(i, "expense", 1000 * (i + 1), "X", "d", "raw")
        rows = db.get_history(limit=3)
        self.assertEqual(len(rows), 3)
        # newest first
        self.assertTrue(rows[0]["id"] > rows[1]["id"] > rows[2]["id"])

    def test_amount_must_be_positive(self):
        with self.assertRaises(Exception):
            db.insert_transaction(1, "expense", -10, "X", "d", "raw")

    def test_type_must_be_valid(self):
        with self.assertRaises(Exception):
            db.insert_transaction(1, "not_a_type", 10, "X", "d", "raw")

    def test_get_all_transactions(self):
        db.insert_transaction(1, "expense", 1000, "Food", "lunch", "r1")
        db.insert_transaction(2, "income", 5000, "Salary", "pay", "r2")
        rows = db.get_all_transactions(order="ASC")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[1]["id"], 2)


class TestLLMParsing(unittest.TestCase):
    def _mock_response(self, content: str):
        return {"message": {"content": content}}

    def test_valid_expense(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response(
                '{"type": "expense", "amount": 10000, "category": "coffee", "description": "morning coffee"}'
            )
            result = llm.parse_transaction("I spent 10k on coffee")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "expense")
        self.assertEqual(result["amount"], 10000)
        self.assertEqual(result["category"], "Coffee")

    def test_unclear_error_returns_none(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response('{"error": "unclear"}')
            result = llm.parse_transaction("uhh what")
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response("not json at all")
            result = llm.parse_transaction("garbled")
        self.assertIsNone(result)

    def test_missing_fields_returns_none(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response('{"type": "expense"}')
            result = llm.parse_transaction("incomplete")
        self.assertIsNone(result)

    def test_zero_or_negative_amount_rejected(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response(
                '{"type": "expense", "amount": 0, "category": "x", "description": "d"}'
            )
            result = llm.parse_transaction("free coffee")
        self.assertIsNone(result)

    def test_invalid_type_rejected(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response(
                '{"type": "sideways", "amount": 10, "category": "x", "description": "d"}'
            )
            result = llm.parse_transaction("weird")
        self.assertIsNone(result)

    def test_ollama_exception_returns_none(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.side_effect = RuntimeError("connection refused")
            result = llm.parse_transaction("anything")
        self.assertIsNone(result)

    def test_retry_succeeds_on_second_attempt(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.side_effect = [
                self._mock_response('{"error": "unclear"}'),
                self._mock_response(
                    '{"type": "income", "amount": 500000, "category": "gift", "description": "birthday gift"}'
                ),
            ]
            result = llm.parse_transaction_with_retry("mumble mumble", attempts=2)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "income")
        self.assertEqual(instance.chat.call_count, 2)

    def test_retry_exhausts_and_returns_none(self):
        with patch("llm.ollama.Client") as MockClient:
            instance = MockClient.return_value
            instance.chat.return_value = self._mock_response('{"error": "unclear"}')
            result = llm.parse_transaction_with_retry("static", attempts=2)
        self.assertIsNone(result)
        self.assertEqual(instance.chat.call_count, 2)

    def test_empty_text_short_circuits_without_calling_ollama(self):
        with patch("llm.ollama.Client") as MockClient:
            result = llm.parse_transaction("   ")
        self.assertIsNone(result)
        MockClient.assert_not_called()


class TestCharts(unittest.TestCase):
    def test_pie_chart_bytes_for_data(self):
        png = charts.make_category_pie({"Food": 100, "Transport": 50}, "Test")
        self.assertIsNotNone(png)
        self.assertGreater(len(png), 100)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")  # PNG magic bytes

    def test_none_for_empty_data(self):
        self.assertIsNone(charts.make_category_pie({}, "Test"))

    def test_none_for_all_zero_data(self):
        self.assertIsNone(charts.make_category_pie({"Food": 0}, "Test"))

    def test_format_amount(self):
        self.assertEqual(charts.format_amount(1000000), "1,000,000 UZS")

    def test_summary_caption_contains_all_fields(self):
        caption = charts.format_summary_caption(
            "Weekly", {"income": 100, "expense": 40, "balance": 60, "count": 3}
        )
        self.assertIn("Weekly", caption)
        self.assertIn("100", caption)
        self.assertIn("40", caption)
        self.assertIn("60", caption)
        self.assertIn("3", caption)

    def test_format_balance(self):
        text = charts.format_balance(
            {"income": 500000, "expense": 100000, "balance": 400000, "count": 2}
        )
        self.assertIn("500,000", text)
        self.assertIn("100,000", text)
        self.assertIn("400,000", text)
        self.assertIn("2", text)


class TestDeleteCommandParsing(unittest.TestCase):
    def test_slash_delete(self):
        self.assertEqual(botmod.try_parse_delete_command("/delete 23"), 23)

    def test_delete_transaction_phrase(self):
        self.assertEqual(botmod.try_parse_delete_command("delete transaction 23"), 23)

    def test_delete_with_letter_prefix(self):
        self.assertEqual(botmod.try_parse_delete_command("delete transaction O23"), 23)

    def test_delete_with_hash(self):
        self.assertEqual(botmod.try_parse_delete_command("delete #23"), 23)

    def test_remove_phrase(self):
        self.assertEqual(botmod.try_parse_delete_command("remove entry 7"), 7)

    def test_leading_zeros(self):
        self.assertEqual(botmod.try_parse_delete_command("delete transaction O023"), 23)

    def test_non_delete_text_returns_none(self):
        self.assertIsNone(botmod.try_parse_delete_command("I spent 10k on coffee"))

    def test_case_insensitive(self):
        self.assertEqual(botmod.try_parse_delete_command("DELETE TRANSACTION 5"), 5)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeVoice:
    def __init__(self, file_id):
        self.file_id = file_id


class FakeMessage:
    def __init__(self, message_id, chat_id, text=None, voice=None, reply_to_message=None):
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat = FakeChat(chat_id)
        self.text = text
        self.voice = voice
        self.reply_to_message = reply_to_message


class FakeUpdate:
    def __init__(self, update_id, message):
        self.update_id = update_id
        self.message = message


class TestHandleUpdateFlow(unittest.IsolatedAsyncioTestCase):
    """
    Exercises bot.handle_update's control flow end-to-end with STT/LLM/Telegram
    mocked out, against the real (test) sqlite DB.
    """

    def setUp(self):
        fresh_db()
        self.sent_messages = []  # (chat_id, text, kwargs)
        self.bot = MagicMock()

        async def fake_send_message(chat_id, text, **kwargs):
            self.sent_messages.append((chat_id, text, kwargs))
            msg = MagicMock()
            msg.message_id = 9000 + len(self.sent_messages)
            return msg

        self.bot.send_message = AsyncMock(side_effect=fake_send_message)

    async def test_non_owner_message_is_ignored(self):
        msg = FakeMessage(1, chat_id=999999, text="I spent 10k on coffee")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(1, msg))
        self.assertIsNone(outcome)
        self.bot.send_message.assert_not_called()

    async def test_history_command(self):
        db.insert_transaction(1, "expense", 5000, "Food", "lunch", "raw")
        msg = FakeMessage(2, chat_id=config.OWNER_CHAT_ID, text="history")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(2, msg))
        self.assertIsNone(outcome)
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("Food", self.sent_messages[0][1])

    async def test_balance_command(self):
        db.insert_transaction(1, "income", 100000, "Salary", "pay", "raw")
        msg = FakeMessage(3, chat_id=config.OWNER_CHAT_ID, text="balance")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(3, msg))
        self.assertIsNone(outcome)
        self.assertIn("100,000", self.sent_messages[0][1])

    async def test_delete_command_via_text(self):
        tx_id = db.insert_transaction(1, "expense", 5000, "Food", "lunch", "raw")
        msg = FakeMessage(4, chat_id=config.OWNER_CHAT_ID, text=f"delete transaction {tx_id}")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(4, msg))
        self.assertIsNone(outcome)
        self.assertIsNone(db.get_transaction(tx_id))
        self.assertIn("Deleted", self.sent_messages[0][1])

    async def test_text_transaction_saved(self):
        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = {
                "type": "expense", "amount": 10000, "category": "Coffee", "description": "coffee"
            }
            msg = FakeMessage(5, chat_id=config.OWNER_CHAT_ID, text="I spent 10k on coffee")
            outcome = await botmod.handle_update(self.bot, FakeUpdate(5, msg))
        self.assertEqual(outcome, "saved")
        rows = db.get_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 10000)

    async def test_text_transaction_unclear_creates_pending(self):
        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = None
            msg = FakeMessage(6, chat_id=config.OWNER_CHAT_ID, text="mumble mumble")
            outcome = await botmod.handle_update(self.bot, FakeUpdate(6, msg))
        self.assertEqual(outcome, "failed")
        self.assertEqual(db.count_pending_resends(), 1)
        self.assertIn("couldn't understand", self.sent_messages[0][1])

    async def test_resend_reply_resolves_pending(self):
        # First: an unclear message creates a pending prompt.
        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = None
            original_msg = FakeMessage(7, chat_id=config.OWNER_CHAT_ID, text="mumble")
            await botmod.handle_update(self.bot, FakeUpdate(7, original_msg))

        self.assertEqual(db.count_pending_resends(), 1)
        prompt_msg_id = list(self.sent_messages)[-1][2].get("reply_to_message_id")
        # The prompt's own message_id was faked as 9000+n in fake_send_message;
        # fetch it directly from the pending table instead of guessing.
        pending_row = None
        with_conn = db._conn()
        # (use the public API instead of touching internals)
        # Just re-derive from db: there should be exactly one pending row.
        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        pending_row = conn.execute("SELECT * FROM pending_resend").fetchone()
        conn.close()
        self.assertIsNotNone(pending_row)

        class FakeReplyTarget:
            def __init__(self, message_id):
                self.message_id = message_id

        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = {
                "type": "expense", "amount": 20000, "category": "Food", "description": "lunch retry"
            }
            resend_msg = FakeMessage(
                8, chat_id=config.OWNER_CHAT_ID, text="I spent 20k on lunch",
                reply_to_message=FakeReplyTarget(pending_row["prompt_message_id"]),
            )
            outcome = await botmod.handle_update(self.bot, FakeUpdate(8, resend_msg))

        self.assertEqual(outcome, "saved")
        self.assertEqual(db.count_pending_resends(), 0)
        rows = db.get_history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 20000)

    async def test_reply_to_pending_skips_command_parsing(self):
        """
        A reply to a pending-resend prompt should be treated as transaction
        text even if it happens to look like a command (edge case check).
        """
        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = None
            original_msg = FakeMessage(9, chat_id=config.OWNER_CHAT_ID, text="mumble")
            await botmod.handle_update(self.bot, FakeUpdate(9, original_msg))

        import sqlite3
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        pending_row = conn.execute("SELECT * FROM pending_resend").fetchone()
        conn.close()

        class FakeReplyTarget:
            def __init__(self, message_id):
                self.message_id = message_id

        with patch("bot.llm.parse_transaction_with_retry") as mock_parse:
            mock_parse.return_value = {
                "type": "expense", "amount": 1000, "category": "Other", "description": "history"
            }
            resend_msg = FakeMessage(
                10, chat_id=config.OWNER_CHAT_ID, text="history",
                reply_to_message=FakeReplyTarget(pending_row["prompt_message_id"]),
            )
            outcome = await botmod.handle_update(self.bot, FakeUpdate(10, resend_msg))

        # Should have been parsed as a transaction, NOT treated as the
        # "history" command, because it's a reply to a pending resend.
        self.assertEqual(outcome, "saved")

    async def test_offset_advances_across_batch(self):
        """Simulates the main-loop offset bookkeeping for a small batch."""
        fresh_db()
        updates = [
            FakeUpdate(100, FakeMessage(1, chat_id=999999)),  # ignored (non-owner)
            FakeUpdate(101, FakeMessage(2, chat_id=config.OWNER_CHAT_ID, text="balance")),
        ]
        offset = db.get_offset()
        for update in updates:
            await botmod.handle_update(self.bot, update)
            offset = update.update_id + 1
            db.set_offset(offset)
        self.assertEqual(db.get_offset(), 102)

    async def test_button_weekly_expenses(self):
        self.bot.send_photo = AsyncMock()
        db.insert_transaction(1, "expense", 15000, "Food", "lunch", "r")
        msg = FakeMessage(20, chat_id=config.OWNER_CHAT_ID, text="📊 Weekly Expenses")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(20, msg))
        self.assertIsNone(outcome)
        self.assertTrue(self.bot.send_photo.called or self.bot.send_message.called)

    async def test_button_monthly_expenses(self):
        self.bot.send_photo = AsyncMock()
        db.insert_transaction(1, "expense", 15000, "Food", "lunch", "r")
        msg = FakeMessage(21, chat_id=config.OWNER_CHAT_ID, text="📅 Monthly Expenses")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(21, msg))
        self.assertIsNone(outcome)
        self.assertTrue(self.bot.send_photo.called or self.bot.send_message.called)

    async def test_button_balance(self):
        db.insert_transaction(1, "income", 50000, "Salary", "pay", "r")
        msg = FakeMessage(22, chat_id=config.OWNER_CHAT_ID, text="💰 Balance")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(22, msg))
        self.assertIsNone(outcome)
        self.assertIn("50,000", self.sent_messages[-1][1])

    async def test_button_transaction_history_csv(self):
        self.bot.send_document = AsyncMock()
        db.insert_transaction(1, "expense", 10000, "Coffee", "morning", "r")
        msg = FakeMessage(23, chat_id=config.OWNER_CHAT_ID, text="📁 Transaction History (CSV)")
        outcome = await botmod.handle_update(self.bot, FakeUpdate(23, msg))
        self.assertIsNone(outcome)
        self.bot.send_document.assert_called_once()
        # Verify CSV content
        call_kwargs = self.bot.send_document.call_args[1]
        doc = call_kwargs["document"]
        csv_text = doc.input_file_content.decode("utf-8-sig")
        self.assertIn("Coffee", csv_text)
        self.assertIn("10000.00", csv_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

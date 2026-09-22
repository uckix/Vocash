"""
All SQLite access lives here. One file, three concerns:

- bot_state       -> arbitrary key/value state (last Telegram update offset,
                     last time weekly/monthly summaries were sent)
- transactions    -> the actual income/expense ledger
- pending_resend  -> "I couldn't understand this voice message" tracker,
                     keyed by the message_id of the bot's own "please resend"
                     prompt, so a later reply can be matched back to it.

Every call opens and closes its own short-lived connection. Throughput here
is "a handful of voice messages a day", so simplicity wins over pooling.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id  INTEGER,
    created_at           TEXT NOT NULL,   -- ISO 8601 UTC
    type                 TEXT NOT NULL CHECK(type IN ('income', 'expense')),
    amount               REAL NOT NULL CHECK(amount > 0),
    category             TEXT NOT NULL,
    description          TEXT,
    raw_text             TEXT,
    deleted              INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_msgid
    ON transactions(telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_transactions_created_at
    ON transactions(created_at);

CREATE TABLE IF NOT EXISTS pending_resend (
    prompt_message_id    INTEGER PRIMARY KEY,
    chat_id              INTEGER NOT NULL,
    original_message_id  INTEGER,
    attempts              INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL
);
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- bot_state

def get_state(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_state(key: str, value):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bot_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_offset() -> int:
    value = get_state("last_update_offset")
    return int(value) if value is not None else 0


def set_offset(offset: int):
    set_state("last_update_offset", offset)


# ------------------------------------------------------------- transactions

def insert_transaction(
    telegram_message_id: int,
    type_: str,
    amount: float,
    category: str,
    description: str,
    raw_text: str,
) -> int | None:
    """
    Returns the new transaction's id, or None if a transaction for this
    telegram_message_id already exists (crash-restart replay guard).
    """
    with _conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO transactions "
                "(telegram_message_id, created_at, type, amount, category, description, raw_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (telegram_message_id, _now_iso(), type_, amount, category, description, raw_text),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            # Only treat this as "already processed" if it's actually the
            # unique-message-id constraint. A CHECK-constraint failure (bad
            # amount/type) is a real bug upstream and must not be swallowed.
            if "idx_transactions_msgid" not in str(exc) and "UNIQUE" not in str(exc):
                raise
            row = conn.execute(
                "SELECT id FROM transactions WHERE telegram_message_id = ?",
                (telegram_message_id,),
            ).fetchone()
            return row["id"] if row else None


def get_transaction(tx_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND deleted = 0", (tx_id,)
        ).fetchone()


def soft_delete_transaction(tx_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET deleted = 1 WHERE id = ? AND deleted = 0", (tx_id,)
        )
        return cur.rowcount > 0


def get_history(limit: int = 20) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM transactions WHERE deleted = 0 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_all_transactions(order: str = "ASC") -> list[sqlite3.Row]:
    order_clause = "DESC" if order.upper() == "DESC" else "ASC"
    with _conn() as conn:
        return conn.execute(
            f"SELECT * FROM transactions WHERE deleted = 0 ORDER BY id {order_clause}"
        ).fetchall()


def get_totals(start_iso: str | None = None, end_iso: str | None = None) -> dict:
    """Overall income/expense/balance/count, optionally restricted to a date range."""
    query = (
        "SELECT type, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n "
        "FROM transactions WHERE deleted = 0"
    )
    params: list = []
    if start_iso:
        query += " AND created_at >= ?"
        params.append(start_iso)
    if end_iso:
        query += " AND created_at <= ?"
        params.append(end_iso)
    query += " GROUP BY type"

    income, expense, count = 0.0, 0.0, 0
    with _conn() as conn:
        for row in conn.execute(query, params):
            count += row["n"]
            if row["type"] == "income":
                income = row["total"]
            elif row["type"] == "expense":
                expense = row["total"]

    return {
        "income": income,
        "expense": expense,
        "balance": income - expense,
        "count": count,
    }


def get_category_totals(
    type_: str = "expense", start_iso: str | None = None, end_iso: str | None = None
) -> dict[str, float]:
    query = (
        "SELECT category, COALESCE(SUM(amount), 0) AS total "
        "FROM transactions WHERE deleted = 0 AND type = ?"
    )
    params: list = [type_]
    if start_iso:
        query += " AND created_at >= ?"
        params.append(start_iso)
    if end_iso:
        query += " AND created_at <= ?"
        params.append(end_iso)
    query += " GROUP BY category ORDER BY total DESC"

    with _conn() as conn:
        return {row["category"]: row["total"] for row in conn.execute(query, params)}


# ----------------------------------------------------------- pending_resend

def add_pending_resend(prompt_message_id: int, chat_id: int, original_message_id: int):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_resend "
            "(prompt_message_id, chat_id, original_message_id, attempts, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (prompt_message_id, chat_id, original_message_id, _now_iso()),
        )


def get_pending_resend(prompt_message_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM pending_resend WHERE prompt_message_id = ?",
            (prompt_message_id,),
        ).fetchone()


def bump_pending_resend_attempts(prompt_message_id: int) -> int:
    with _conn() as conn:
        conn.execute(
            "UPDATE pending_resend SET attempts = attempts + 1 WHERE prompt_message_id = ?",
            (prompt_message_id,),
        )
        row = conn.execute(
            "SELECT attempts FROM pending_resend WHERE prompt_message_id = ?",
            (prompt_message_id,),
        ).fetchone()
        return row["attempts"] if row else 0


def remove_pending_resend(prompt_message_id: int):
    with _conn() as conn:
        conn.execute(
            "DELETE FROM pending_resend WHERE prompt_message_id = ?", (prompt_message_id,)
        )


def count_pending_resends() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM pending_resend").fetchone()["n"]

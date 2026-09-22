"""
Configuration for the finance tracker bot.
All values are loaded from environment variables (see .env.example).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# --- Required ---
BOT_TOKEN = _require("BOT_TOKEN")
OWNER_CHAT_ID = int(_require("OWNER_CHAT_ID"))

# --- Optional, with sensible defaults ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")

# moonshine/tiny (fastest, smallest) or moonshine/base (a bit more accurate)
MOONSHINE_MODEL = os.environ.get("MOONSHINE_MODEL", "moonshine/base")

DB_PATH = os.environ.get("DB_PATH", "finance_bot.db")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tashkent")

# How long (seconds) each long-poll request waits for new updates.
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))

# How often (seconds) to check whether it's time to send the weekly/monthly
# summary. 300s = check every 5 minutes; cheap, no need to be exact to the second.
SUMMARY_CHECK_INTERVAL = int(os.environ.get("SUMMARY_CHECK_INTERVAL", "300"))

# How many parse attempts (STT text -> structured transaction) before a
# message is flagged back to the user as "unclear, please resend".
MAX_PARSE_ATTEMPTS = int(os.environ.get("MAX_PARSE_ATTEMPTS", "2"))

CURRENCY_LABEL = os.environ.get("CURRENCY_LABEL", "UZS")

"""
Turns a transcribed sentence into a structured transaction using a local
Ollama model (default: gemma3:4b). No categories are hard-coded — the model
invents whatever category fits the sentence.
"""
import json
import logging

import ollama

from config import OLLAMA_HOST, OLLAMA_MODEL

log = logging.getLogger("llm")

SYSTEM_PROMPT = """You are a strict, robust parser for a personal finance tracker.
The user enters ONE financial transaction either by typing text or by speaking (voice transcription).
Default currency is Uzbekistani som (UZS).
Amounts can be written or spoken in various shorthand forms:
- "10k", "10 k" = 10000; "50k", "50 k" = 50000; "100k", "100 k" = 100000
- "half a million" = 500000; "1.5m", "1.5 million" = 1500000; "2m", "2 million" = 2000000
- Plain numbers: "10000", "15 000", "50,000"

From the sentence, extract:
- "type": either "income" (money received, salary, gift, bonus, refund) or "expense" (money spent, bought, paid, lunch, coffee, etc.)
- "amount": the plain numeric amount in UZS (positive number, no commas, no currency symbol)
- "category": a short 1-2 word category inferred freely from context (e.g. Food, Coffee, Transport, Groceries, Salary, Shopping, Entertainment, Rent, Utilities, Health, Gift, Other). Always capitalize like a title.
- "description": a brief 2-6 word natural description of what happened

If the sentence does NOT clearly describe a financial transaction with an amount, respond with exactly:
{"error": "unclear"}

Otherwise respond with ONLY valid JSON, nothing else, in exactly this shape:
{"type": "income" or "expense", "amount": <number>, "category": "<text>", "description": "<text>"}

Examples:
Input: "i spent 10 k to lunch"
Output: {"type": "expense", "amount": 10000, "category": "Food", "description": "lunch expense"}

Input: "bought coffee 15k"
Output: {"type": "expense", "amount": 15000, "category": "Coffee", "description": "coffee"}

Input: "I received my salary it was two million"
Output: {"type": "income", "amount": 2000000, "category": "Salary", "description": "monthly salary received"}

Input: "taxi 25 k"
Output: {"type": "expense", "amount": 25000, "category": "Transport", "description": "taxi ride"}
"""


def _call_ollama(text: str) -> dict | None:
    client = ollama.Client(host=OLLAMA_HOST)
    try:
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            format="json",
            options={"temperature": 0.1},
        )
    except Exception:
        log.exception("Ollama call failed")
        return None

    raw = response.get("message", {}).get("content", "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Model returned non-JSON output: %r", raw)
        return None

    return data


def _validate(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    if "error" in data:
        return None

    try:
        type_ = str(data["type"]).strip().lower()
        amount = float(data["amount"])
        category = str(data["category"]).strip().title()[:50]
        description = str(data.get("description", "")).strip()[:200]
    except (KeyError, TypeError, ValueError):
        return None

    if type_ not in ("income", "expense"):
        return None
    if amount <= 0:
        return None
    if not category:
        category = "Other"
    if not description:
        description = category

    return {
        "type": type_,
        "amount": amount,
        "category": category,
        "description": description,
    }


def parse_transaction(text: str) -> dict | None:
    """Single attempt. Returns a validated dict, or None if unclear/invalid."""
    if not text or not text.strip():
        return None
    data = _call_ollama(text)
    if data is None:
        return None
    return _validate(data)


def parse_transaction_with_retry(text: str, attempts: int = 2) -> dict | None:
    """Try up to `attempts` times before giving up and returning None."""
    for i in range(max(1, attempts)):
        result = parse_transaction(text)
        if result is not None:
            return result
        log.info("Parse attempt %d/%d failed for text: %r", i + 1, attempts, text)
    return None

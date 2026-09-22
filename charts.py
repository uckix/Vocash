"""
Renders the category-breakdown pie chart sent for weekly/monthly summaries.
Headless (Agg backend) since this never runs near a display.
"""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import CURRENCY_LABEL


def make_category_pie(category_totals: dict[str, float], title: str) -> bytes | None:
    """
    category_totals: e.g. {"Food": 250000, "Transport": 80000}
    Returns PNG bytes, or None if there's nothing to plot.
    """
    filtered = {k: v for k, v in category_totals.items() if v > 0}
    if not filtered:
        return None

    labels = list(filtered.keys())
    values = list(filtered.values())

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.set_title(title)
    ax.axis("equal")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def format_amount(amount: float) -> str:
    return f"{amount:,.0f} {CURRENCY_LABEL}"


def format_summary_caption(
    period_label: str, totals: dict, category_totals: dict[str, float] | None = None
) -> str:
    lines = [
        f"📊 {period_label} summary",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"Income: {format_amount(totals['income'])}",
        f"Expense: {format_amount(totals['expense'])}",
        f"Net: {format_amount(totals['balance'])}",
        f"Transactions: {totals['count']}",
    ]
    if category_totals:
        filtered = {k: v for k, v in category_totals.items() if v > 0}
        if filtered:
            lines.append("\n📂 Expenses by Category:")
            total_exp = totals.get("expense", 0.0) or sum(filtered.values())
            for cat, amt in list(filtered.items())[:8]:
                pct = (amt / total_exp * 100) if total_exp > 0 else 0
                lines.append(f"  • {cat}: {format_amount(amt)} ({pct:.1f}%)")
    return "\n".join(lines)


def format_balance(totals: dict) -> str:
    income = totals.get("income", 0.0)
    expense = totals.get("expense", 0.0)
    balance = totals.get("balance", 0.0)
    count = totals.get("count", 0)

    icon = "🟢" if balance >= 0 else "🔴"
    sign = "+" if balance > 0 else ""

    lines = [
        "💳 Financial Balance Overview",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"📥 Total Income:   +{format_amount(income)}",
        f"📤 Total Expenses: -{format_amount(expense)}",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"{icon} Net Balance:    {sign}{format_amount(balance)}",
        f"🔢 Transactions:   {count}",
    ]
    if income > 0:
        savings_rate = max(0.0, (balance / income) * 100)
        lines.append(f"📈 Savings Rate:   {savings_rate:.1f}%")

    return "\n".join(lines)

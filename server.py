"""
Finance MCP — Abrar's personal spending tracker for ChatGPT (and Claude Code).

ChatGPT acts as the spending manager: it records every expense against a card,
checks monthly card limits and per-category budgets, and reports where the
money went. It deliberately CANNOT move real money and never sees full card
numbers — cards are tracked by nickname + last4 only.

Data lives in ~/.config/finance-mcp/finance.db (SQLite). Amounts are QAR
riyals (stored as integer dirhams). Dates are ISO YYYY-MM-DD.

Transports:
    python server.py                  -> stdio (Claude Code, Antigravity)
    python server.py --http [--port]  -> streamable HTTP at /mcp (ChatGPT
                                         connectors via Tailscale Funnel)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

CONFIG_DIR = Path.home() / ".config" / "finance-mcp"
DB_PATH = Path(os.environ.get("FINANCE_DB", CONFIG_DIR / "finance.db"))
CURRENCY = os.environ.get("FINANCE_CURRENCY", "QAR")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname      TEXT NOT NULL UNIQUE,
    last4         TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'debit',
    monthly_limit INTEGER NOT NULL DEFAULT 0,     -- paisa, 0 = no limit
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    amount      INTEGER NOT NULL,                 -- paisa
    category    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    card_id     INTEGER REFERENCES cards(id),
    spent_on    TEXT NOT NULL,                    -- YYYY-MM-DD
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    monthly  INTEGER NOT NULL                     -- dirhams
);
CREATE TABLE IF NOT EXISTS guardrails (
    key   TEXT PRIMARY KEY,                       -- single_purchase_limit | daily_limit
    value INTEGER NOT NULL                        -- dirhams
);
"""


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


with db() as _c:
    _c.executescript(SCHEMA)


mcp = FastMCP("finance", instructions=(
    "Abrar's spending manager with guardrails (currency QAR). Log every "
    "expense he mentions with log_expense; run check_purchase BEFORE he buys "
    "anything and tell him clearly if it fails. Every logged expense returns "
    "'flags' — suspicious signals (over single-purchase/daily limit, possible "
    "duplicate charge, unusually large vs his typical spending). If flags are "
    "non-empty, point them out immediately and ask whether the charge is "
    "really his; suggest freezing the card in his bank app if it looks like "
    "fraud. Run suspicious_activity when he asks to review a month. Cards are "
    "identified by nickname; never ask for full card numbers or CVVs — only "
    "a nickname and the last 4 digits. Changing a card limit, budget, or "
    "guardrail requires Abrar's explicit confirmation in the conversation "
    "first. This tool records and audits spending; it cannot and must not "
    "move real money."
))


class FinanceError(Exception):
    pass


def _paisa(amount: float) -> int:
    p = round(amount * 100)
    if p <= 0:
        raise FinanceError("amount must be positive")
    return int(p)


def _month(month: str | None) -> str:
    m = month or date.today().strftime("%Y-%m")
    if len(m) != 7 or m[4] != "-":
        raise FinanceError("month must be YYYY-MM")
    return m


def _card(conn, nickname: str):
    row = conn.execute("SELECT * FROM cards WHERE nickname=?", (nickname.strip().lower(),)).fetchone()
    if not row:
        known = [r["nickname"] for r in conn.execute("SELECT nickname FROM cards")]
        raise FinanceError(f"no card named {nickname!r} (known: {', '.join(known) or 'none — add_card first'})")
    return row


def _spent(conn, month: str, card_id: int | None = None, category: str | None = None) -> int:
    q = "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE spent_on LIKE ?"
    args: list[Any] = [month + "%"]
    if card_id is not None:
        q += " AND card_id=?"
        args.append(card_id)
    if category is not None:
        q += " AND category=?"
        args.append(category)
    return conn.execute(q, args).fetchone()["s"]


def _guardrails(conn) -> dict[str, int]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM guardrails")}


def _typical(conn) -> tuple[int, int]:
    """(count, average amount in dirhams) over the last 90 days of expenses."""
    since = date.fromordinal(date.today().toordinal() - 90).isoformat()
    r = conn.execute("SELECT COUNT(*) n, COALESCE(AVG(amount),0) a FROM expenses WHERE spent_on >= ?",
                     (since,)).fetchone()
    return r["n"], int(r["a"])


def _suspicion_flags(conn, amount_p: int, category: str, spent_on: str,
                     exclude_id: int | None = None) -> list[str]:
    """Guardrail checks shared by check_purchase (before buying) and log_expense (after)."""
    flags = []
    rails = _guardrails(conn)
    spl = rails.get("single_purchase_limit")
    if spl and amount_p > spl:
        flags.append(f"over the single-purchase limit of {spl / 100:.2f} {CURRENCY}")
    daily = rails.get("daily_limit")
    if daily:
        q = "SELECT COALESCE(SUM(amount),0) s FROM expenses WHERE spent_on=?"
        args: list[Any] = [spent_on]
        if exclude_id is not None:
            q += " AND id != ?"
            args.append(exclude_id)
        day_total = conn.execute(q, args).fetchone()["s"] + amount_p
        if day_total > daily:
            flags.append(f"day total {day_total / 100:.2f} exceeds the daily limit of {daily / 100:.2f} {CURRENCY}")
    q = "SELECT COUNT(*) n FROM expenses WHERE amount=? AND category=? AND spent_on=?"
    args = [amount_p, category, spent_on]
    if exclude_id is not None:
        q += " AND id != ?"
        args.append(exclude_id)
    if conn.execute(q, args).fetchone()["n"]:
        flags.append(f"possible DUPLICATE charge: same amount and category already recorded on {spent_on}")
    n, avg = _typical(conn)
    if n >= 5 and avg and amount_p > 3 * avg:
        flags.append(f"unusually large: {amount_p / 100:.2f} is over 3x his typical expense (~{avg / 100:.2f} {CURRENCY})")
    return flags


# --------------------------------------------------------------------------- cards
@mcp.tool()
def add_card(nickname: str, last4: str, kind: str = "debit", monthly_limit: float = 0) -> dict[str, Any]:
    """Track a card by nickname + last 4 digits (NEVER the full number).

    monthly_limit: max QAR this card should spend per month (0 = no limit)."""
    nickname = nickname.strip().lower()
    last4 = last4.strip()[-4:]
    if not (nickname and last4.isdigit() and len(last4) == 4):
        raise FinanceError("need a nickname and exactly 4 digits for last4")
    with db() as conn:
        try:
            conn.execute("INSERT INTO cards(nickname,last4,kind,monthly_limit,created_at) VALUES (?,?,?,?,?)",
                         (nickname, last4, kind, _paisa(monthly_limit) if monthly_limit else 0, int(time.time())))
        except sqlite3.IntegrityError:
            raise FinanceError(f"card nickname {nickname!r} already exists")
    return {"nickname": nickname, "last4": last4, "kind": kind, "monthly_limit": monthly_limit}


@mcp.tool()
def list_cards() -> list[dict[str, Any]]:
    """All tracked cards with this month's spend vs monthly limit (QAR)."""
    month = _month(None)
    with db() as conn:
        out = []
        for r in conn.execute("SELECT * FROM cards ORDER BY id"):
            spent = _spent(conn, month, card_id=r["id"])
            out.append({
                "nickname": r["nickname"], "last4": r["last4"], "kind": r["kind"],
                "monthly_limit": r["monthly_limit"] / 100 or None,
                "spent_this_month": spent / 100,
                "remaining_this_month": (r["monthly_limit"] - spent) / 100 if r["monthly_limit"] else None,
            })
        return out


@mcp.tool()
def set_card_limit(nickname: str, monthly_limit: float) -> dict[str, Any]:
    """Change a card's monthly spending limit (QAR, 0 = remove the limit).

    ONLY call this when Abrar has explicitly asked for or confirmed the new
    limit in this conversation — never change limits on your own initiative."""
    with db() as conn:
        c = _card(conn, nickname)
        conn.execute("UPDATE cards SET monthly_limit=? WHERE id=?",
                     (_paisa(monthly_limit) if monthly_limit else 0, c["id"]))
    return {"nickname": c["nickname"], "monthly_limit": monthly_limit or None}


@mcp.tool()
def set_guardrail(name: str, amount: float) -> dict[str, Any]:
    """Set a suspicion guardrail (QAR, 0 = remove). name: single_purchase_limit | daily_limit.

    ONLY call this when Abrar explicitly asked for or confirmed the value."""
    name = name.strip().lower()
    if name not in ("single_purchase_limit", "daily_limit"):
        raise FinanceError("name must be single_purchase_limit or daily_limit")
    with db() as conn:
        if amount <= 0:
            conn.execute("DELETE FROM guardrails WHERE key=?", (name,))
            return {"guardrail": name, "amount": None, "removed": True}
        conn.execute("INSERT INTO guardrails(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (name, _paisa(amount)))
    return {"guardrail": name, "amount": amount}


@mcp.tool()
def suspicious_activity(month: str | None = None) -> dict[str, Any]:
    """Audit a month (default: current) for suspicious spending: duplicate charges,
    unusually large expenses (>3x typical), and days over the daily limit."""
    month = _month(month)
    with db() as conn:
        findings = []
        for r in conn.execute(
                "SELECT amount, category, spent_on, COUNT(*) n, GROUP_CONCAT(id) ids FROM expenses "
                "WHERE spent_on LIKE ? GROUP BY amount, category, spent_on HAVING n > 1",
                (month + "%",)):
            findings.append({"type": "duplicate_charge", "spent_on": r["spent_on"],
                             "category": r["category"], "amount": r["amount"] / 100,
                             "times": r["n"], "expense_ids": r["ids"]})
        n, avg = _typical(conn)
        if n >= 5 and avg:
            for r in conn.execute(
                    "SELECT id, amount, category, description, spent_on FROM expenses "
                    "WHERE spent_on LIKE ? AND amount > ? ORDER BY amount DESC", (month + "%", 3 * avg)):
                findings.append({"type": "unusually_large", "expense_id": r["id"],
                                 "spent_on": r["spent_on"], "category": r["category"],
                                 "description": r["description"], "amount": r["amount"] / 100,
                                 "typical_expense": avg / 100})
        daily = _guardrails(conn).get("daily_limit")
        if daily:
            for r in conn.execute(
                    "SELECT spent_on, SUM(amount) s FROM expenses WHERE spent_on LIKE ? "
                    "GROUP BY spent_on HAVING s > ?", (month + "%", daily)):
                findings.append({"type": "over_daily_limit", "spent_on": r["spent_on"],
                                 "day_total": r["s"] / 100, "daily_limit": daily / 100})
        return {"month": month, "currency": CURRENCY, "findings": findings,
                "note": "If a flagged charge is not Abrar's, tell him to freeze the card "
                        "in his bank app — this tool cannot block real transactions."}


# --------------------------------------------------------------------------- expenses
@mcp.tool()
def log_expense(amount: float, category: str, description: str = "", card: str | None = None,
                spent_on: str | None = None) -> dict[str, Any]:
    """Record an expense (QAR). category e.g. food, transport, subscriptions, school.

    card: nickname of the card used (optional = cash). spent_on: YYYY-MM-DD, default today.
    The response includes limit/budget warnings — relay them to Abrar."""
    amount_p = _paisa(amount)
    category = category.strip().lower()
    spent_on = spent_on or date.today().isoformat()
    date.fromisoformat(spent_on)  # validate
    month = spent_on[:7]
    with db() as conn:
        card_row = _card(conn, card) if card else None
        cur = conn.execute(
            "INSERT INTO expenses(amount,category,description,card_id,spent_on,created_at) VALUES (?,?,?,?,?,?)",
            (amount_p, category, description, card_row["id"] if card_row else None, spent_on, int(time.time())))
        result: dict[str, Any] = {
            "id": cur.lastrowid, "amount": amount, "currency": CURRENCY, "category": category,
            "description": description, "card": card_row["nickname"] if card_row else "cash",
            "spent_on": spent_on, "warnings": [],
            "flags": _suspicion_flags(conn, amount_p, category, spent_on, exclude_id=cur.lastrowid),
        }
        if card_row and card_row["monthly_limit"]:
            spent = _spent(conn, month, card_id=card_row["id"])
            result["card_remaining_this_month"] = (card_row["monthly_limit"] - spent) / 100
            if spent > card_row["monthly_limit"]:
                result["warnings"].append(
                    f"card '{card_row['nickname']}' is OVER its monthly limit by {(spent - card_row['monthly_limit']) / 100:.2f} {CURRENCY}")
        budget = conn.execute("SELECT monthly FROM budgets WHERE category=?", (category,)).fetchone()
        if budget:
            cat_spent = _spent(conn, month, category=category)
            result["category_budget_remaining"] = (budget["monthly"] - cat_spent) / 100
            if cat_spent > budget["monthly"]:
                result["warnings"].append(
                    f"category '{category}' is OVER budget by {(cat_spent - budget['monthly']) / 100:.2f} {CURRENCY}")
        return result


@mcp.tool()
def check_purchase(amount: float, category: str, card: str | None = None) -> dict[str, Any]:
    """Before Abrar buys something: would this purchase break a card limit or category budget?

    Returns ok true/false with reasons. Does NOT record anything — call log_expense after he buys."""
    amount_p = _paisa(amount)
    category = category.strip().lower()
    month = _month(None)
    reasons = []
    with db() as conn:
        if card:
            c = _card(conn, card)
            if c["monthly_limit"]:
                spent = _spent(conn, month, card_id=c["id"])
                if spent + amount_p > c["monthly_limit"]:
                    reasons.append(f"card '{c['nickname']}' would go over its monthly limit "
                                   f"(spent {spent / 100:.2f}, limit {c['monthly_limit'] / 100:.2f})")
        budget = conn.execute("SELECT monthly FROM budgets WHERE category=?", (category,)).fetchone()
        if budget:
            cat_spent = _spent(conn, month, category=category)
            if cat_spent + amount_p > budget["monthly"]:
                reasons.append(f"category '{category}' would go over budget "
                               f"(spent {cat_spent / 100:.2f}, budget {budget['monthly'] / 100:.2f})")
        reasons += _suspicion_flags(conn, amount_p, category, date.today().isoformat())
    return {"ok": not reasons, "amount": amount, "currency": CURRENCY, "reasons": reasons}


@mcp.tool()
def list_expenses(month: str | None = None, category: str | None = None, card: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    """Expenses newest first, filterable by month (YYYY-MM), category, card nickname."""
    with db() as conn:
        q = ("SELECT e.id, e.amount, e.category, e.description, e.spent_on, c.nickname AS card "
             "FROM expenses e LEFT JOIN cards c ON c.id = e.card_id WHERE 1=1")
        args: list[Any] = []
        if month:
            q += " AND e.spent_on LIKE ?"
            args.append(_month(month) + "%")
        if category:
            q += " AND e.category=?"
            args.append(category.strip().lower())
        if card:
            q += " AND e.card_id=?"
            args.append(_card(conn, card)["id"])
        q += " ORDER BY e.spent_on DESC, e.id DESC LIMIT ?"
        args.append(limit)
        return [{**dict(r), "amount": r["amount"] / 100, "card": r["card"] or "cash"}
                for r in conn.execute(q, args)]


@mcp.tool()
def delete_expense(expense_id: int) -> dict[str, Any]:
    """Delete a wrongly logged expense by its id."""
    with db() as conn:
        n = conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,)).rowcount
        if not n:
            raise FinanceError(f"no expense with id {expense_id}")
    return {"deleted": expense_id}


# --------------------------------------------------------------------------- budgets & reports
@mcp.tool()
def set_budget(category: str, monthly_amount: float) -> dict[str, Any]:
    """Set (or update) a monthly budget for a category, in QAR. 0 removes it."""
    category = category.strip().lower()
    with db() as conn:
        if monthly_amount <= 0:
            conn.execute("DELETE FROM budgets WHERE category=?", (category,))
            return {"category": category, "monthly": None, "removed": True}
        conn.execute("INSERT INTO budgets(category,monthly) VALUES (?,?) "
                     "ON CONFLICT(category) DO UPDATE SET monthly=excluded.monthly",
                     (category, _paisa(monthly_amount)))
    return {"category": category, "monthly": monthly_amount}


@mcp.tool()
def summary(month: str | None = None) -> dict[str, Any]:
    """Monthly report: total spend, per-category spend vs budget, per-card spend vs limit."""
    month = _month(month)
    with db() as conn:
        total = _spent(conn, month)
        budgets = {r["category"]: r["monthly"] for r in conn.execute("SELECT * FROM budgets")}
        by_category = []
        for r in conn.execute(
                "SELECT category, SUM(amount) s, COUNT(*) n FROM expenses WHERE spent_on LIKE ? "
                "GROUP BY category ORDER BY s DESC", (month + "%",)):
            entry = {"category": r["category"], "spent": r["s"] / 100, "count": r["n"]}
            if r["category"] in budgets:
                entry["budget"] = budgets[r["category"]] / 100
                entry["remaining"] = (budgets[r["category"]] - r["s"]) / 100
            by_category.append(entry)
        for cat, monthly in budgets.items():  # budgets with no spend yet
            if not any(e["category"] == cat for e in by_category):
                by_category.append({"category": cat, "spent": 0, "count": 0,
                                    "budget": monthly / 100, "remaining": monthly / 100})
        by_card = []
        for r in conn.execute("SELECT * FROM cards ORDER BY id"):
            spent = _spent(conn, month, card_id=r["id"])
            by_card.append({"card": r["nickname"], "last4": r["last4"], "spent": spent / 100,
                            "monthly_limit": r["monthly_limit"] / 100 or None,
                            "remaining": (r["monthly_limit"] - spent) / 100 if r["monthly_limit"] else None})
        cash = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM expenses "
                            "WHERE card_id IS NULL AND spent_on LIKE ?", (month + "%",)).fetchone()["s"]
        return {"month": month, "currency": CURRENCY, "total_spent": total / 100,
                "by_category": by_category, "by_card": by_card, "cash_spent": cash / 100}


# --------------------------------------------------------------------------- entrypoint
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on /mcp (for ChatGPT)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8975)
    args = ap.parse_args()
    if not args.http:
        mcp.run()
        return

    token = os.environ.get("MCP_TOKEN")
    # MCP_NOAUTH=1: skip bearer check (ChatGPT connectors cannot send custom headers).
    noauth = os.environ.get("MCP_NOAUTH") == "1"
    if not token and not noauth:
        raise SystemExit("MCP_TOKEN env var required in --http mode (or MCP_NOAUTH=1)")
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class Bearer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not noauth and request.headers.get("authorization") != f"Bearer {token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    from mcp.server.transport_security import TransportSecuritySettings
    # Public hostname comes through Tailscale Funnel.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp.settings.host, mcp.settings.port = args.host, args.port
    mcp.settings.streamable_http_path = "/mcp"
    app = mcp.streamable_http_app()
    app.add_middleware(Bearer)

    # ---- local-only /guardrail routes for the Chromium extension ----------
    # The Funnel only proxies /finance-mcp -> /mcp, so these root-level routes
    # are reachable from this machine only (read-only, no auth needed).
    async def guardrail_status(request):
        with db() as conn:
            rails = {k: v / 100 for k, v in _guardrails(conn).items()}
        return JSONResponse({"currency": CURRENCY, "cards": list_cards(), "guardrails": rails})

    async def guardrail_check(request):
        try:
            amount = float(request.query_params["amount"])
            category = request.query_params.get("category", "online")
            card = request.query_params.get("card")
            if not card:
                cards = list_cards()
                card = cards[0]["nickname"] if cards else None
            return JSONResponse({**check_purchase(amount, category, card), "card": card})
        except (KeyError, ValueError):
            return JSONResponse({"error": "amount query param (number) required"}, status_code=400)
        except FinanceError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    app.add_route("/guardrail/status", guardrail_status, methods=["GET"])
    app.add_route("/guardrail/check", guardrail_check, methods=["GET"])

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

# finance-mcp

A personal spending-manager MCP server by Syed Abrar Ali. It turns any Model Context Protocol client (a chat assistant, a CLI agent) into a bookkeeper that records expenses against cards, enforces monthly card limits and per-category budgets, runs guardrail checks before a purchase, and audits a month for suspicious charges. It deliberately cannot move money and never sees full card numbers: cards are tracked by a nickname and their last four digits only. Everything is stored in a local SQLite file. A small companion browser extension reuses the same guardrails to show a red/green banner on checkout pages before the card is typed in.

## Where it is used

It runs on the owner's laptop as a user service. Local MCP clients talk to it over stdio; a remote chat assistant reaches the HTTP transport through a tunnel or reverse proxy that maps a path to the local port. The assistant is told, via the server instructions, to log every expense it hears about, to call `check_purchase` before the user buys something, and to relay any flags immediately. The Chromium extension in `extension/` runs in the owner's browser and asks the same server whether an order total fits the limits.

## How it works

### Server (`server.py`)

Built on `mcp.server.fastmcp.FastMCP`, with Starlette/uvicorn used only in HTTP mode.

Storage: SQLite at `~/.config/finance-mcp/finance.db` (WAL mode, foreign keys on), schema created on import.

| Table | Columns |
| --- | --- |
| `cards` | nickname (unique, lower-cased), last4, kind (debit/credit), monthly_limit, created_at |
| `expenses` | amount, category, description, card_id (nullable = cash), spent_on (YYYY-MM-DD), created_at |
| `budgets` | category (PK), monthly |
| `guardrails` | key (`single_purchase_limit` or `daily_limit`), value |

Amounts are stored as integers in hundredths of the currency unit (`_paisa()` rounds and rejects non-positive values); the currency label defaults to `QAR` and is only cosmetic.

Core decision logic lives in `_suspicion_flags(conn, amount, category, date)`, shared by `check_purchase` (before buying) and `log_expense` (after). It returns human-readable flags when:

1. the amount exceeds `single_purchase_limit`;
2. the day's total including this amount exceeds `daily_limit`;
3. an expense with the same amount, category and date already exists (possible duplicate charge);
4. there are at least five expenses in the last 90 days and this amount is more than three times their average (unusually large).

Tools:

| Tool | Purpose |
| --- | --- |
| `add_card`, `list_cards`, `set_card_limit` | Track a card by nickname + last4 with an optional monthly limit; list cards with this month's spend and remaining allowance |
| `set_guardrail(name, amount)` | Set or remove `single_purchase_limit` / `daily_limit` |
| `set_budget(category, monthly_amount)` | Set or remove a per-category monthly budget |
| `log_expense(amount, category, description, card, spent_on)` | Insert an expense; returns `flags` plus `warnings` when the card is over its monthly limit or the category is over budget, and the remaining amounts |
| `check_purchase(amount, category, card)` | Dry run: would this purchase break the card limit or category budget, and does it trip any guardrail? Returns `ok` and `reasons`; records nothing |
| `list_expenses(month, category, card, limit)` | Newest first, filterable |
| `delete_expense(expense_id)` | Remove a mistaken entry |
| `summary(month)` | Total spend, per-category spend vs budget (including budgets with no spend), per-card spend vs limit, cash spend |
| `suspicious_activity(month)` | Month audit: duplicate charges, unusually large expenses, days over the daily limit |

The server instructions require the client to get explicit confirmation in the conversation before changing a limit, budget or guardrail.

HTTP mode adds a Bearer-token middleware (`MCP_TOKEN`) unless `MCP_NOAUTH=1`, disables DNS-rebinding protection (the public hostname is terminated elsewhere), and mounts two local-only JSON routes for the extension: `GET /guardrail/status` (cards and guardrails) and `GET /guardrail/check?amount=&category=&card=` (same result as `check_purchase`, defaulting to the first card).

### Browser extension (`extension/`)

Manifest V3. `content.js` watches every page for card-number inputs (autocomplete `cc-*`, names/ids containing "cardnumber", checkout iframes). When one appears it injects a shadow-DOM banner, scrapes a likely order total from the page text ("Total: 45.00" and similar), and asks `background.js` to call the local `/guardrail/check` endpoint (content scripts cannot fetch localhost from HTTPS pages, so the service worker does it). The banner turns green ("fits your limits"), red ("DO NOT PAY" plus reasons) or amber (server unreachable), and offers a manual amount box when no total could be read. `test-page.html` is a fake checkout for trying it.

## Setup

Requirements: Python 3.12+, `uv` (or pip), a Chromium-based browser for the optional extension.

```bash
git clone https://github.com/USER/finance-mcp.git
cd finance-mcp
uv sync                      # installs mcp[cli], starlette, uvicorn from pyproject.toml
```

Environment variables (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `FINANCE_DB` | `~/.config/finance-mcp/finance.db` | SQLite path |
| `FINANCE_CURRENCY` | `QAR` | Currency label in results |
| `MCP_TOKEN` | — | Bearer token required in `--http` mode |
| `MCP_NOAUTH` | — | Set to `1` to skip the Bearer check (for connectors that cannot send headers; protect the port some other way) |

Extension: open `chrome://extensions`, enable Developer mode, "Load unpacked", choose `extension/`. It talks to `http://127.0.0.1:8975/guardrail`, so the server must run in HTTP mode on port 8975 (change `BASE` in `background.js` and `host_permissions` in `manifest.json` otherwise).

## Usage

```bash
uv run python server.py                           # stdio
MCP_TOKEN=CHANGE_ME uv run python server.py --http --port 8975   # HTTP at /mcp
```

Example conversation with a connected assistant:

1. "Track my debit card ending 1234 as 'main', limit 600 a month" → `add_card("main", "1234", monthly_limit=600)`
2. "Food budget 400, single purchase limit 250" → `set_budget("food", 400)`, `set_guardrail("single_purchase_limit", 250)` (after you confirm)
3. "Can I buy a 300 pair of shoes on main?" → `check_purchase(300, "clothes", "main")` → `ok: false`, reason "over the single-purchase limit"
4. "I paid 45 for lunch on main" → `log_expense(45, "food", "lunch", "main")` → flags `[]`, `category_budget_remaining: 355`
5. "How was this month?" → `summary()`; "Anything odd?" → `suspicious_activity()`

Extension check without a shop: open `extension/test-page.html` in the browser; the banner reads the "Order total" line and shows the verdict.

## Project structure

```
finance-mcp/
├── server.py              # FastMCP server: schema, guardrail logic, 12 tools, HTTP mode + /guardrail routes
├── pyproject.toml         # mcp[cli], starlette, uvicorn; Python >= 3.12
├── uv.lock
└── extension/
    ├── manifest.json      # MV3, content script on all URLs, host permission for the local server
    ├── background.js      # service worker: proxies check/status requests to 127.0.0.1:8975
    ├── content.js         # payment-form detection, total scraping, banner UI
    └── test-page.html     # fake checkout for manual testing
```

## Limitations and notes

- It is a ledger, not a bank connection: nothing is imported from statements and nothing can block a real transaction. If a flag looks like fraud, the advice returned is to freeze the card in the issuing bank's app.
- Duplicate detection is exact (same amount, category and date); two legitimate identical purchases on one day will be flagged.
- The "unusually large" rule needs at least five expenses in the trailing 90 days before it activates.
- Months are matched by string prefix on `spent_on`; dates must be ISO `YYYY-MM-DD`.
- `MCP_NOAUTH=1` removes the only authentication in HTTP mode; use it only behind a proxy that adds its own secret.
- The `/guardrail/*` routes are unauthenticated by design and meant to be reachable only from the same machine.
- The extension's total detection is a regular expression over page text and can pick the wrong number; the manual amount field is the fallback.
- Server instructions address the owner by first name; adjust if deployed for someone else.

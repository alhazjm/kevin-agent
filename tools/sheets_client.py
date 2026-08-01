import hashlib
import os
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

TRANSACTIONS_SHEET = "Transactions"
BUDGET_SHEET = "Budget"
MERCHANT_MAP_SHEET = "MerchantMap"
INSIGHTS_SHEET = "Insights"
JOURNAL_SHEET = "Journal"
WEBHOOK_LOG_SHEET = "WebhookLog"

# Placeholder category used by log_expense_pending. Rows with this category
# are excluded from spending aggregates (get_spending_summary,
# generate_spending_report, detect_subscription_creep) so they don't pollute
# totals until the user picks a real category.
PENDING_CATEGORY = "UNCATEGORIZED"


def _is_pending(row: dict) -> bool:
    """True if a transaction row is still awaiting user categorisation."""
    return str(row.get("Category", "")).strip().upper() == PENDING_CATEGORY


def _is_backfill(row: dict) -> bool:
    """True for statement-import rows (M14: excluded from all agent-side
    aggregates — counting them corrupts every comparison)."""
    return str(row.get("Source", "")).strip().lower() == "backfill"


def _is_pot_internal(row: dict) -> bool:
    """YouTrip-card SPENDS (Shortcut-sourced; Payment Method carries
    "YouTrip") are pot-internal: the top-up already counted as the outflow
    when it left the bank. Twin of the PWA's isYtSpend — found desynced
    while reconciling the Jul 2026 recap, which quietly counted a $6.95
    YouTrip spend the dashboard excluded. Top-ups are unaffected (their
    Payment Method is the funding bank card). Shared here because the
    semantics are identical on both backends (the _is_pending rule)."""
    return "youtrip" in str(row.get("Payment Method", "")).lower()


def _counts_in_totals(row: dict) -> bool:
    """M14 plus the pot rule in one predicate: agent-side monthly
    aggregates skip pending rows, backfill statement imports, and
    pot-internal YouTrip spends. The PWA hero deliberately COUNTS pending
    and backfill (honest cash-out, Hadi 2026-07-31) — the recap explains
    the difference via `excluded_from_totals` instead of matching it."""
    return not (_is_pending(row) or _is_backfill(row) or _is_pot_internal(row))


# Reader twin of travel_mode's [trip:] writer (M13: writer and reader
# regexes change together — the PWA's TRIP_TAG_RE is the third copy).
_TRIP_TAG_RE = re.compile(r"\[trip:([^\]]+)\]")
_TRANSFER_CATEGORY = "youtrip top-up"


def _rebucket_category(row: dict, trip_categories: dict[str, str]) -> str:
    """PWA spendByCat twin: a [trip:X]-tagged YouTrip top-up is outflow
    FOR that trip, so its per-category bucket is the TRIP's category, not
    "YouTrip Top-up". This is the compensation that makes excluding
    pot-internal spends safe: the trip's budgets row consumes the FUNDED
    amount, keeping budget-manager warnings and get_remaining_budget armed
    (without it, an active trip reads $0 spent of its budget all month —
    caught by adversarial review before it shipped, 2026-08-01)."""
    cat = str(row.get("Category", "Miscellaneous"))
    if trip_categories and cat.strip().lower() == _TRANSFER_CATEGORY:
        m = _TRIP_TAG_RE.search(str(row.get("Notes", "")))
        if m and m.group(1) in trip_categories:
            return trip_categories[m.group(1)]
    return cat


MONTH_COLUMNS = {
    1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7,
    7: 8, 8: 9, 9: 10, 10: 11, 11: 12, 12: 13,
}

# Canonical Transactions tab schema (1-indexed). Used for both fixed-column
# fallback writes and as the authoritative ordering when appending new rows.
# The runtime layout is detected from the live header row via
# `_get_column_index` so legacy 8-column sheets keep working until the user
# applies the schema migration documented in sheets-template/README.md.
TRANSACTION_COLUMNS = {
    "Date": 1,
    "Merchant": 2,
    "Amount": 3,
    "Currency": 4,
    "Category": 5,
    "Source": 6,
    "Payment Method": 7,
    "Notes": 8,
    "txn_id": 9,
    "telegram_message_id": 10,
    "idempotency_key": 11,
}

TXN_ID_PATTERN = re.compile(r"^txn_(\d{8})_(\d{3,})$")


@lru_cache(maxsize=1)
def get_client():
    sa_value = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    try:
        info = json.loads(sa_value)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except (json.JSONDecodeError, ValueError):
        creds = Credentials.from_service_account_file(sa_value, scopes=SCOPES)
    return gspread.authorize(creds)


_TRANSIENT_GSPREAD_STATUSES = {429, 500, 502, 503, 504}


def _is_transient_gspread_error(exc: Exception) -> bool:
    """Return True if an exception looks like a transient Google Sheets
    outage worth retrying. gspread raises `APIError` with a `response`
    attribute on HTTP errors; we retry 429 and 5xx, fail fast on 4xx."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in _TRANSIENT_GSPREAD_STATUSES


def get_spreadsheet():
    """Open the expense sheet. Retries transient 429 / 5xx errors with
    exponential backoff (0.5s, 1s, 2s) before surfacing the failure.

    Historical 503s from Google's fetch_sheet_metadata caused the agent
    to crash mid-cron even when the outage was measured in seconds; the
    retry absorbs those so the next tool call doesn't see the blip."""
    sheet_id = os.environ["GSPREAD_SPREADSHEET_ID"]
    last_exc = None
    for attempt in range(3):
        try:
            return get_client().open_by_key(sheet_id)
        except gspread.exceptions.APIError as exc:
            last_exc = exc
            if not _is_transient_gspread_error(exc) or attempt == 2:
                raise
            time.sleep(0.5 * (2 ** attempt))
    # Defensive — loop always either returns or raises above.
    raise last_exc  # type: ignore[misc]


def _get_column_index(ws, column_name: str) -> int | None:
    """Return the 1-indexed column position of `column_name` in the worksheet's
    header row, or None if the header doesn't contain that column. Lets the
    code tolerate sheets that haven't been migrated to the new schema yet."""
    header = ws.row_values(1)
    try:
        return header.index(column_name) + 1
    except ValueError:
        return None


def _generate_txn_id(ws, date_str: str) -> str:
    """Build the next `txn_<YYYYMMDD>_<NNN>` ID for the given date by scanning
    the existing txn_id column for that date prefix and incrementing the max
    sequence. Falls back to `txn_<YYYYMMDD>_001` if no prior IDs exist or if
    the txn_id column is absent."""
    compact_date = date_str.replace("-", "")
    prefix = f"txn_{compact_date}_"

    col_idx = _get_column_index(ws, "txn_id")
    if col_idx is None:
        return f"{prefix}001"

    existing_ids = ws.col_values(col_idx)[1:]  # skip header
    max_seq = 0
    for value in existing_ids:
        match = TXN_ID_PATTERN.match(value or "")
        if match and match.group(1) == compact_date:
            seq = int(match.group(2))
            if seq > max_seq:
                max_seq = seq

    return f"{prefix}{max_seq + 1:03d}"


def _compute_idempotency_key(date: str, merchant: str, amount: float,
                             payment_method: str, txn_time: str = "") -> str:
    """Deterministic hash of a transaction's natural key fields.

    Used to prevent duplicate inserts when the same webhook fires twice
    or when network retries cause double-writes. The hash is truncated
    to 16 hex chars (64 bits) — collision-safe for a personal ledger.

    `txn_time` (added 2026-07: the time-in-key fix) disambiguates two REAL
    purchases at the same merchant for the same amount on the same day —
    previously indistinguishable from a double-fired webhook and falsely
    rejected (the KOPITIAM $7.80 incident, 2026-07-25). The time comes from
    the bank email (DBS prints it; UOB uses the email's arrival time), so
    a re-processed SAME email still produces the same key. When empty
    (manual /log, backfill, legacy rows) the raw string is byte-identical
    to the pre-change 4-field format, so every existing stored key stays
    valid. Mirrored in Code.gs::computeIdempotencyKey — change BOTH sides
    together and regenerate the parity pins (M17).
    """
    raw = f"{date}|{merchant.strip().upper()}|{amount:.2f}|{payment_method.strip()}"
    if txn_time:
        raw += f"|{txn_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _find_by_idempotency_key(ws, key: str) -> tuple[int, dict] | None:
    """Check if a row with the given idempotency_key already exists.

    Returns (row_number, row_dict) or None. Returns None gracefully if the
    idempotency_key column doesn't exist yet (pre-migration sheets)."""
    col_idx = _get_column_index(ws, "idempotency_key")
    if col_idx is None:
        return None

    cell = ws.find(key, in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def append_transaction(date: str, merchant: str, amount: float, currency: str,
                       category: str, source: str = "email",
                       payment_method: str = "", notes: str = ""):
    """Append a transaction to the Transactions tab.

    Generates a `txn_id` of the form `txn_<YYYYMMDD>_<NNN>` and returns it in
    the result so the caller can link a Telegram message_id afterwards via
    `link_telegram_message`.

    The `telegram_message_id` column is left empty here — it gets filled
    automatically by `handle_log_expense` after the confirmation bubble is
    sent via the Telegram Bot API.

    **Idempotency:** a deterministic hash of (date, merchant, amount,
    payment_method) is written to the `idempotency_key` column. If a row
    with the same key already exists, the function returns the existing
    `txn_id` with `status: "duplicate"` instead of double-inserting. This
    prevents duplicates when the same webhook fires twice or network retries
    cause re-sends.

    The row layout matches `TRANSACTION_COLUMNS`. If the live sheet still has
    the legacy 8-column layout (no txn_id / telegram_message_id /
    idempotency_key columns), those trailing values are simply written into
    nonexistent cells, which gspread silently extends — so the migration is
    forward-compatible.
    """
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)

    # Idempotency check: reject duplicate inserts
    idem_key = _compute_idempotency_key(date, merchant, amount, payment_method)
    existing = _find_by_idempotency_key(ws, idem_key)
    if existing:
        _, row_dict = existing
        return {
            "status": "duplicate",
            "txn_id": row_dict.get("txn_id", ""),
            "idempotency_key": idem_key,
            "message": "Duplicate transaction — already logged.",
        }

    txn_id = _generate_txn_id(ws, date)
    row = [
        date,
        merchant,
        amount,
        currency,
        category,
        source,
        payment_method,
        notes,
        txn_id,
        "",          # telegram_message_id — filled by link_telegram_message
        idem_key,    # idempotency_key — dedup on re-send
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return {"status": "ok", "row": row, "txn_id": txn_id, "idempotency_key": idem_key}


SNAPSHOT_HEADER = ["Date", "Merchant", "Amount", "Currency", "Category",
                   "Source", "Payment Method", "Notes", "txn_id",
                   "telegram_message_id", "idempotency_key", "Time"]


def write_sheet_snapshot(transaction_rows: list[list],
                         budget_grid: list[list]) -> dict:
    """Rebuild the Transactions and Budget tabs from prepared grids — the
    nightly export that replaced the dual-write mirror (migration PR 4).

    The Sheet is a read-only VIEW from this point: a human-browsable grid
    and the free-tier backup copy. Hand edits are overwritten nightly by
    design. Each tab is a clear + one batched update (2 API calls per tab)
    at the 3 AM cron hour — quota-trivial, no per-row appends.

    `transaction_rows` are 12-element lists in SNAPSHOT_HEADER order;
    `budget_grid` includes its own header row (Category | Jan..Dec).
    """
    ss = get_spreadsheet()

    ws = ss.worksheet(TRANSACTIONS_SHEET)
    ws.clear()
    ws.update(values=[SNAPSHOT_HEADER] + transaction_rows, range_name="A1",
              value_input_option="USER_ENTERED")

    bs = ss.worksheet(BUDGET_SHEET)
    bs.clear()
    bs.update(values=budget_grid, range_name="A1",
              value_input_option="USER_ENTERED")

    return {
        "status": "ok",
        "transactions_exported": len(transaction_rows),
        "budget_rows_exported": max(0, len(budget_grid) - 1),
    }


def write_year_archive(year: int, transaction_rows: list[list]) -> dict:
    """Freeze one calendar year's transactions into an `Archive-<year>` tab
    in the backup Sheet — yearly cold storage, written by the Jan-1 cron.

    Archives are WRITE-ONCE by design. Unlike write_sheet_snapshot (which
    clears and rebuilds nightly), an existing `Archive-<year>` tab is
    treated as the immutable record of that year: if the tab is already
    there this function returns `status: "exists"` WITHOUT writing a single
    cell. Re-running the cron (or asking again mid-conversation) must never
    silently rewrite history — delete the tab by hand first if a re-archive
    is genuinely intended.

    `transaction_rows` are 12-element lists in SNAPSHOT_HEADER order (the
    handler assembles them from Supabase, pre-filtered to `year`). The tab
    is created exactly sized and filled with ONE batched update — no
    per-row appends.
    """
    ss = get_spreadsheet()
    tab = f"Archive-{year}"
    try:
        ss.worksheet(tab)
        return {
            "status": "exists",
            "tab": tab,
            "year": year,
            "message": f"{tab} already exists — archives are write-once, nothing was overwritten.",
        }
    except gspread.WorksheetNotFound:
        pass

    ws = ss.add_worksheet(title=tab, rows=len(transaction_rows) + 1,
                          cols=len(SNAPSHOT_HEADER))
    ws.update(values=[SNAPSHOT_HEADER] + transaction_rows, range_name="A1",
              value_input_option="USER_ENTERED")
    return {
        "status": "ok",
        "tab": tab,
        "year": year,
        "transactions_archived": len(transaction_rows),
    }


def read_transactions(month: str | None = None) -> list[dict]:
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    if not month:
        month = datetime.now().strftime("%Y-%m")

    filtered = []
    for r in records:
        try:
            row_date = str(r.get("Date", ""))
            if row_date.startswith(month):
                filtered.append(r)
        except (ValueError, TypeError):
            continue
    return filtered


def read_budgets(month_num: int | None = None) -> list[dict]:
    """Read budget limits. Supports per-month columns (Jan=col2 ... Dec=col13)
    or a simple 2-column layout (Category | Monthly Limit)."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()

    if not all_values:
        return []

    header = all_values[0]

    if month_num is None:
        month_num = datetime.now().month

    # Detect layout: if header has month names or 12+ columns, it's per-month
    is_per_month = len(header) >= 13 or any(
        m in header[1].lower() for m in ["jan", "feb", "mar", "1", "2"]
    )

    budgets = []
    if is_per_month:
        col_idx = MONTH_COLUMNS.get(month_num, 2)
        for row in all_values[1:]:
            if not row[0].strip():
                continue
            try:
                limit = float(row[col_idx - 1]) if len(row) >= col_idx and row[col_idx - 1] else 0
            except (ValueError, IndexError):
                limit = 0
            budgets.append({"Category": row[0].strip(), "Monthly Limit": limit})
    else:
        for row in all_values[1:]:
            if not row[0].strip():
                continue
            try:
                limit = float(row[1]) if len(row) > 1 and row[1] else 0
            except ValueError:
                limit = 0
            budgets.append({"Category": row[0].strip(), "Monthly Limit": limit})

    return budgets


def update_budget_row(category: str, monthly_limit: float, month_num: int | None = None):
    """Update budget limit for a category. If per-month layout, updates the specific month column."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()
    header = all_values[0] if all_values else []

    if month_num is None:
        month_num = datetime.now().month

    cell = ws.find(category, in_column=1)
    if cell is None:
        raise ValueError(f"Category '{category}' not found in Budget sheet")

    is_per_month = len(header) >= 13
    if is_per_month:
        col_idx = MONTH_COLUMNS.get(month_num, 2)
    else:
        col_idx = 2

    ws.update_cell(cell.row, col_idx, monthly_limit)
    return {"status": "ok", "category": category, "month": month_num, "new_limit": monthly_limit}


def find_transaction_by_id(txn_id: str) -> tuple[int, dict] | None:
    """Find a transaction row by its canonical `txn_id`. This is the preferred
    lookup path for edit/delete since it's unambiguous. Returns (row_number,
    row_dict) or None. Returns None gracefully if the txn_id column doesn't
    exist on the live sheet (legacy layout) — callers should fall back to
    `find_transaction_row` in that case."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "txn_id")
    if col_idx is None:
        return None

    cell = ws.find(txn_id, in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    # Pad row_values to header length so dict zip is complete
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def find_transaction_by_message_id(telegram_message_id: str) -> tuple[int, dict] | None:
    """Find a transaction row by the Telegram message_id of the bot's
    confirmation bubble. Used to resolve reply-to-message edits: when the user
    replies "that should be Groceries" to a confirmation, the inbound payload
    carries `reply_to_message_id`, which this function maps back to the
    original transaction row.

    Returns (row_number, row_dict) or None. Returns None if the column doesn't
    exist on the live sheet (legacy layout) or if no row matches."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "telegram_message_id")
    if col_idx is None:
        return None

    cell = ws.find(str(telegram_message_id), in_column=col_idx)
    if cell is None:
        return None

    header = ws.row_values(1)
    row_values = ws.row_values(cell.row)
    row_values += [""] * (len(header) - len(row_values))
    row_dict = dict(zip(header, row_values))
    return (cell.row, row_dict)


def find_transaction_row(merchant: str, amount: float, date: str | None = None) -> tuple[int, dict] | None:
    """Find a transaction row by merchant + amount (+ optional date).
    Returns (row_number, row_dict) or None. Row number is 1-indexed (Sheet rows).

    Legacy fallback used when the caller doesn't have a `txn_id` (e.g. for
    rows that pre-date the schema migration). Prefer `find_transaction_by_id`
    or `find_transaction_by_message_id` whenever possible."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    # Search from bottom (most recent) to top
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        row_merchant = str(r.get("Merchant", "")).strip().lower()
        row_amount = float(r.get("Amount", 0))

        search = merchant.strip().lower()
        merchant_match = (
            len(search) >= 3 and (search in row_merchant or row_merchant in search)
        )
        if merchant_match and abs(row_amount - amount) < 0.01:
            if date and str(r.get("Date", "")) != date:
                continue
            # +2 because: row 1 is header, records are 0-indexed
            return (i + 2, r)
    return None


def edit_transaction(merchant: str, amount: float, date: str | None = None,
                     updates: dict | None = None,
                     txn_id: str | None = None) -> dict:
    """Edit fields of an existing transaction. `updates` is a dict of
    column_name → new_value.

    Lookup priority:
      1. `txn_id` (canonical, unambiguous)
      2. merchant + amount (+ optional date) — legacy fallback for rows that
         pre-date the schema migration

    Updates use live header lookup (`_get_column_index`) rather than the
    static `TRANSACTION_COLUMNS` map, so the function works against both the
    legacy 8-column layout and the new 10-column layout."""
    if not updates:
        return {"status": "error", "message": "No updates provided"}

    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_num, existing = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)

    changed = {}
    for field, value in updates.items():
        col_idx = _get_column_index(ws, field)
        if col_idx is None:
            # Fall back to canonical schema for known columns
            col_idx = TRANSACTION_COLUMNS.get(field)
        if col_idx:
            ws.update_cell(row_num, col_idx, value)
            changed[field] = {"from": existing.get(field, ""), "to": value}

    return {
        "status": "ok",
        "row": row_num,
        "txn_id": existing.get("txn_id", ""),
        "changes": changed,
    }


def delete_transaction(merchant: str, amount: float, date: str | None = None,
                       txn_id: str | None = None) -> dict:
    """Delete a transaction row.

    Lookup priority matches `edit_transaction`:
      1. `txn_id` (canonical, unambiguous)
      2. merchant + amount (+ optional date) — legacy fallback
    """
    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_num, existing = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    ws.delete_rows(row_num)
    return {"status": "ok", "deleted_row": row_num, "transaction": existing}


def get_last_transaction() -> tuple[int, dict] | None:
    """Return the most recent transaction row (bottom of the sheet).

    Returns (row_number, row_dict) or None if the sheet is empty.
    Used by undo_last_expense to delete the last logged transaction."""
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()
    if not records:
        return None
    last = records[-1]
    row_num = len(records) + 1  # +1 for header row
    return (row_num, last)


def link_telegram_message(txn_id: str, telegram_message_id: str) -> dict:
    """Attach a Telegram message_id to an already-logged transaction.

    Called by the expense-tracker skill after it sends the confirmation
    bubble: `log_expense` returns a `txn_id`, the bot sends the bubble and
    receives a `message_id` back from Telegram, then this function records
    the link so future reply-to-message edits resolve correctly."""
    result = find_transaction_by_id(txn_id)
    if result is None:
        return {"status": "error", "message": f"Transaction not found: txn_id={txn_id}"}

    row_num, _ = result
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    col_idx = _get_column_index(ws, "telegram_message_id")
    if col_idx is None:
        return {
            "status": "error",
            "message": (
                "Sheet has no `telegram_message_id` column — "
                "apply the schema migration in sheets-template/README.md"
            ),
        }

    ws.update_cell(row_num, col_idx, str(telegram_message_id))
    return {
        "status": "ok",
        "txn_id": txn_id,
        "telegram_message_id": str(telegram_message_id),
        "row": row_num,
    }


def ensure_category_exists(category: str) -> dict:
    """Ensure a budget category row exists. Creates it with $0 limit if missing.
    Returns {"status": "exists"|"created", "category": str}."""
    ws = get_spreadsheet().worksheet(BUDGET_SHEET)
    all_values = ws.get_all_values()

    # Check if category already exists (case-insensitive)
    for row in all_values[1:]:
        if row and row[0].strip().lower() == category.strip().lower():
            return {"status": "exists", "category": category}

    # Determine layout: per-month (13+ cols) or simple (2 cols)
    header = all_values[0] if all_values else []
    is_per_month = len(header) >= 13

    if is_per_month:
        new_row = [category] + [0] * 12
    else:
        new_row = [category, 0]

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    return {"status": "created", "category": category}


def get_spending_summary(month: str | None = None,
                         trip_categories: dict[str, str] | None = None) -> dict:
    # `if not month` (not `is None`) so empty strings from LLM-constructed
    # tool calls also fall back to the current month. Previously crashed
    # with IndexError on `"".split("-")[1]`.
    # `trip_categories` (label → trip category) drives top-up re-bucketing;
    # this twin takes it as a parameter — it can't reach the travel_mode
    # table (same pattern as the sub-detector's exclude_keys).
    if not month:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    spending = {}
    pending_count = 0
    for t in transactions:
        if _is_pending(t):
            pending_count += 1
            continue
        if not _counts_in_totals(t):
            continue
        cat = _rebucket_category(t, trip_categories or {})
        amt = float(t.get("Amount", 0))
        spending[cat] = spending.get(cat, 0) + amt

    summary = {}
    for cat, limit in budgets.items():
        spent = spending.get(cat, 0)
        summary[cat] = {
            "limit": limit,
            "spent": round(spent, 2),
            "remaining": round(limit - spent, 2),
            "percent_used": round((spent / limit) * 100, 1) if limit > 0 else 0,
        }

    unbudgeted = set(spending.keys()) - set(budgets.keys())
    for cat in unbudgeted:
        summary[cat] = {
            "limit": 0,
            "spent": round(spending[cat], 2),
            "remaining": 0,
            "percent_used": 100,
            "note": "No budget set for this category",
        }

    if pending_count:
        summary["_pending_review"] = {
            "count": pending_count,
            "note": (
                f"{pending_count} transaction(s) still UNCATEGORIZED — "
                "awaiting user pick via reply-to-ask-prompt. Not included "
                "in category totals."
            ),
        }

    return summary


def generate_spending_report(month: str | None = None,
                             trip_categories: dict[str, str] | None = None) -> dict:
    """Build a detailed spending report for a given month.

    Returns category breakdown with budget comparisons, top merchants by
    spend, daily spending totals, and month-over-month comparison vs the
    prior month. Designed to give the LLM everything it needs to present
    a rich summary with citations (txn_ids, date ranges).
    `trip_categories` (label → trip category) drives top-up re-bucketing —
    a parameter here because this twin can't reach the travel_mode table.
    """
    if not month:
        month = datetime.now().strftime("%Y-%m")
    trip_categories = trip_categories or {}

    month_num = int(month.split("-")[1])
    year = int(month.split("-")[0])

    # Current month data
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    # Prior month data for comparison
    prev_month_num = month_num - 1 if month_num > 1 else 12
    prev_year = year if month_num > 1 else year - 1
    prev_month_str = f"{prev_year}-{prev_month_num:02d}"
    prev_transactions = read_transactions(prev_month_str)

    # Category breakdown. Pending rows are collected separately so they
    # don't pollute totals but stay surfaced to the LLM for a review nudge.
    category_spend: dict[str, float] = defaultdict(float)
    category_txns: dict[str, list[str]] = defaultdict(list)
    pending_rows = []
    backfill_count, backfill_total = 0, 0.0
    yt_count, yt_total = 0, 0.0
    for t in transactions:
        if _is_pending(t):
            pending_rows.append({
                "txn_id": t.get("txn_id", ""),
                "merchant": t.get("Merchant", ""),
                "amount": float(t.get("Amount", 0)),
                "date": str(t.get("Date", "")),
            })
            continue
        if _is_backfill(t):
            backfill_count += 1
            backfill_total += float(t.get("Amount", 0))
            continue
        if _is_pot_internal(t):
            yt_count += 1
            yt_total += float(t.get("Amount", 0))
            continue
        cat = _rebucket_category(t, trip_categories)
        amt = float(t.get("Amount", 0))
        category_spend[cat] += amt
        txn_id = t.get("txn_id", "")
        if txn_id:
            category_txns[cat].append(txn_id)

    categories = []
    all_cats = set(budgets.keys()) | set(category_spend.keys())
    for cat in sorted(all_cats):
        limit = budgets.get(cat, 0)
        spent = round(category_spend.get(cat, 0), 2)
        categories.append({
            "category": cat,
            "limit": limit,
            "spent": spent,
            "remaining": round(limit - spent, 2),
            "percent_used": round((spent / limit) * 100, 1) if limit > 0 else 0,
            "txn_ids": category_txns.get(cat, []),
        })
    categories.sort(key=lambda c: c["spent"], reverse=True)

    # Top merchants by total spend (same exclusions as the category
    # breakdown above).
    merchant_spend: dict[str, float] = defaultdict(float)
    merchant_count: dict[str, int] = defaultdict(int)
    for t in transactions:
        if not _counts_in_totals(t):
            continue
        m = t.get("Merchant", "Unknown")
        merchant_spend[m] += float(t.get("Amount", 0))
        merchant_count[m] += 1

    top_merchants = sorted(
        [{"merchant": m, "total": round(s, 2), "count": merchant_count[m]}
         for m, s in merchant_spend.items()],
        key=lambda x: x["total"],
        reverse=True,
    )[:10]

    # Daily spending (same exclusions — a "spent per day" chart shouldn't
    # carry rows the monthly total doesn't).
    daily_spend: dict[str, float] = defaultdict(float)
    for t in transactions:
        if not _counts_in_totals(t):
            continue
        d = str(t.get("Date", ""))
        daily_spend[d] += float(t.get("Amount", 0))
    daily = [{"date": d, "total": round(s, 2)}
             for d, s in sorted(daily_spend.items())]

    # Month-over-month comparison — both sides use the same predicate, or
    # the comparison manufactures phantom swings.
    prev_total = sum(
        float(t.get("Amount", 0)) for t in prev_transactions if _counts_in_totals(t)
    )
    curr_total = sum(
        float(t.get("Amount", 0)) for t in transactions if _counts_in_totals(t)
    )

    prev_cat_spend: dict[str, float] = defaultdict(float)
    for t in prev_transactions:
        if not _counts_in_totals(t):
            continue
        cat = _rebucket_category(t, trip_categories)
        prev_cat_spend[cat] += float(t.get("Amount", 0))

    mom_changes = []
    for cat in sorted(all_cats):
        prev = round(prev_cat_spend.get(cat, 0), 2)
        curr = round(category_spend.get(cat, 0), 2)
        if prev > 0 or curr > 0:
            change_pct = round(((curr - prev) / prev) * 100, 1) if prev > 0 else 0
            mom_changes.append({
                "category": cat,
                "previous": prev,
                "current": curr,
                "change_pct": change_pct,
            })

    total_budget = sum(budgets.values())

    return {
        "month": month,
        "total_spent": round(curr_total, 2),
        "total_budget": round(total_budget, 2),
        "transaction_count": len(transactions),
        "categories": categories,
        "top_merchants": top_merchants,
        "daily_spending": daily,
        "month_over_month": {
            "previous_month": prev_month_str,
            "previous_total": round(prev_total, 2),
            "current_total": round(curr_total, 2),
            "change_pct": round(((curr_total - prev_total) / prev_total) * 100, 1) if prev_total > 0 else 0,
            "category_changes": mom_changes,
        },
        "pending_review": {
            "count": len(pending_rows),
            "total": round(sum(r["amount"] for r in pending_rows), 2),
            "rows": pending_rows,
        },
        # The recap's honesty line: the PWA hero COUNTS pending+backfill
        # (honest cash-out), this report does not (M14) — stating what was
        # excluded is what lets the two totals reconcile at a glance
        # (Jul 2026: a silent $667 gap across 26 pending rows).
        "excluded_from_totals": {
            "pending_count": len(pending_rows),
            "pending_total": round(sum(r["amount"] for r in pending_rows), 2),
            "backfill_count": backfill_count,
            "backfill_total": round(backfill_total, 2),
            "youtrip_spend_count": yt_count,
            "youtrip_spend_total": round(yt_total, 2),
        },
    }


# --- Insights store ---
#
# Tier-4 semantic memory: derived facts persisted to an Insights tab in the
# Google Sheet. Weekly/monthly summaries write short facts here so future
# reports can build on prior conclusions instead of recomputing from scratch.

def write_insight(insight: str, category: str = "general",
                  month: str | None = None) -> dict:
    """Persist a derived insight to the Insights tab.

    Each insight is a short fact (e.g. "You overspent Dining by 22% in
    week 14", "Grab rides trend up on rainy weeks"). The LLM writes these
    after generating summaries; future report runs read them first.
    """
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(INSIGHTS_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=INSIGHTS_SHEET, rows=200, cols=4)
        ws.append_row(
            ["date", "category", "month", "insight"],
            value_input_option="USER_ENTERED",
        )

    if not month:
        month = datetime.now().strftime("%Y-%m")

    today = datetime.now().strftime("%Y-%m-%d")
    ws.append_row(
        [today, category, month, insight],
        value_input_option="USER_ENTERED",
    )
    return {"status": "ok", "insight": insight, "category": category, "month": month}


def get_insights(month: str | None = None, category: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Read insights from the Insights tab, optionally filtered by month
    and/or category. Returns most recent first, up to `limit` entries."""
    try:
        ws = get_spreadsheet().worksheet(INSIGHTS_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()

    filtered = []
    for r in records:
        if month and str(r.get("month", "")) != month:
            continue
        if category and str(r.get("category", "")).lower() != category.lower():
            continue
        filtered.append({
            "date": str(r.get("date", "")),
            "category": str(r.get("category", "")),
            "month": str(r.get("month", "")),
            "insight": str(r.get("insight", "")),
        })

    # Most recent first
    filtered.reverse()
    return filtered[:limit]


# Money movements, not billing: the PWA's TRANSFER_CATS ("youtrip top-up")
# and LENDING_CATS ("lending") — a monthly top-up or IOU repayment is
# perfectly billing-shaped but is never a subscription. Compared against
# the trimmed-lowercased category name; keep in sync with the PWA.
_SUB_EXCLUDED_CATEGORIES = frozenset({"youtrip top-up", "lending"})


def detect_subscription_creep(months_back: int = 3,
                              exclude_keys: set[str] | None = None) -> dict:
    """Scan transactions for recurring BILLING patterns (subscriptions),
    grouped by CATEGORY. Two live incidents shaped this detector:

    Incident 1 — rhythm ≠ billing (the first tightening). The original
    net (≥2 months, ≤2 charges/month, amount spread ≤30% of avg) matched
    anything with a monthly rhythm — 2026-08-01: the deployed view
    flagged Atome instalments, two Prudential insurance premiums, a
    barber (clippers.com.sg), Shaw Theatres, a $7 kopi (TARIK) and Polar
    Puffs as "subscriptions". The five billing-behaviour thresholds below
    date from that incident.

    Incident 2 — merchant-string wobble; MerchantMap already solved
    identity (the category re-key). After the tightening deployed, the
    section STILL showed Atome/Prudential×2/TARIK and MISSED every real
    subscription: Spotify's merchant string carries a unique payment ref
    EVERY month ("Spotify P424C25C39", "Spotify P435EF3A4E", …), so
    merchant grouping fragmented real subs into single-month groups that
    could never reach the 2-month threshold. But those txns are reliably
    MerchantMap'd into dedicated categories (Spotify, OneDrive,
    GoogleOne, Anthropic, Gym, …) — the CATEGORY is the stable key.
    Category grouping also auto-kills the false positives: two Prudential
    policies share one insurance category → two charges/month → rejected
    by rule 2; TARIK lands in a food category with many charges; Atome
    instalments fragment across three categories.

    The billing-behaviour thresholds (unchanged by the re-key):

      1. Category seen in ≥2 distinct months — minimum evidence of
         recurrence.
      2. EXACTLY 1 charge per month in every month seen — subscriptions
         bill once; two same-month Prudential premiums are two policies,
         not a subscription.
      3. Day-of-month spread ≤ 4 across all charges (max − min) — billers
         land on the same date every month (a 29th–31st billing date
         clamping to Feb 28 stays within 4); kopi and haircuts drift
         (the clippers.com.sg case).
      4. Every charge ≥ $0.50 — a micro-charge can no longer hide under
         a passing average.
      5. Consecutive monthly amounts (months sorted ascending, one amount
         each) step by ≤ max(10% of the previous amount, $0.05) — billing
         is near-exact month to month; a 5–10% step still registers as a
         price change downstream.

    `exclude_keys`: trimmed-UPPERCASED CATEGORY keys to skip — the PWA
    `sub_overrides` table's 'exclude' verdicts (user pressed "not a
    subscription"). The table's `merchant_key` column name is historical:
    since the category re-key the PWA writes normalized category keys
    into it; pre-existing merchant-key rows are stale but harmless (they
    match no category). ASYMMETRY: this Sheets twin has no access to that
    Supabase table, so `None` here simply means "no overrides"; the
    supabase_client twin defaults `None` by fetching `sub_overrides`
    itself. The filter is applied identically inside both math blocks
    (parity).

    Status flags:
      - **active** — recurring charge still appearing this month
      - **new** — earliest charge within the lookback window falls in the
        current or previous month, i.e. the pattern just crossed the
        2-month recurrence threshold. Precedence: `price_change` and
        `possibly_cancelled` beat `new`; `new` beats plain `active`. "New"
        subscriptions still count toward `total_monthly_subscriptions`
        (they are active charges). Before 2026-07 nothing ever assigned
        "new", so `new_this_month` was always empty — the regression tests
        pin the fix. Byte-parallel with supabase_client — change together.
      - **price_change** — same category, amount changed >5% vs prior month
      - **possibly_cancelled** — appeared in prior months but not this month

    Each subscription entry's identity is its `category`; `last_merchant`
    (the merchant string of the most recent charge) rides along for
    display/debugging only.

    Excludes `backfill` rows (imported statements would otherwise trigger
    false "new subscription" alerts), pending (UNCATEGORIZED) rows, and
    the transfer/IOU categories in `_SUB_EXCLUDED_CATEGORIES`.

    Returns a structured report the LLM can summarise for the user.
    """
    ws = get_spreadsheet().worksheet(TRANSACTIONS_SHEET)
    records = ws.get_all_records()

    now = datetime.now()
    current_month = now.strftime("%Y-%m")

    # Build list of month strings in the analysis window
    months = []
    for i in range(months_back):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        months.append(f"{y}-{m:02d}")
    months_set = set(months)

    # Filter to relevant months, exclude backfill and pending rows.
    # Pending (UNCATEGORIZED) rows have no real category yet so they can't
    # be a confirmed recurring charge; skipping them avoids spurious
    # "possibly_cancelled" flags while the user hasn't replied to the
    # ask-prompt. Transfer/IOU categories (_SUB_EXCLUDED_CATEGORIES) are
    # money movements, not billing — mirror of the PWA's TRANSFER_CATS /
    # LENDING_CATS exclusions.
    relevant = []
    for r in records:
        source = str(r.get("Source", "")).strip().lower()
        if source == "backfill":
            continue
        if _is_pending(r):
            continue
        if str(r.get("Category", "")).strip().lower() in _SUB_EXCLUDED_CATEGORIES:
            continue
        date_str = str(r.get("Date", ""))
        month = date_str[:7]
        if month in months_set:
            relevant.append(r)

    # Group by CATEGORY (case-insensitive) per month — NOT by merchant:
    # MerchantMap keeps a subscription's category stable even when its
    # merchant string wobbles (Spotify appends a unique payment ref every
    # month — see docstring, incident 2). Day-of-month per charge feeds
    # the billing-date drift check (threshold 3).
    cat_months: dict[str, dict[str, list[float]]] = {}
    cat_days: dict[str, list[int]] = {}
    cat_original: dict[str, str] = {}
    cat_last_merchant: dict[str, str] = {}
    cat_last_date: dict[str, str] = {}

    for r in relevant:
        category = str(r.get("Category", "")).strip()
        cat_key = category.lower()
        amount = float(r.get("Amount", 0))
        date_str = str(r.get("Date", ""))
        month = date_str[:7]
        merchant = str(r.get("Merchant", "")).strip()

        if cat_key not in cat_months:
            cat_months[cat_key] = {}
            cat_days[cat_key] = []
            cat_original[cat_key] = category
            cat_last_date[cat_key] = ""
        cat_months[cat_key].setdefault(month, []).append(amount)
        try:
            cat_days[cat_key].append(int(date_str[8:10]))
        except ValueError:
            pass  # malformed day → excluded from the drift check only
        # Most recent charge's merchant string, for display/debugging only
        # (date ties → last row wins, matching the PWA's latest-charge
        # reduce).
        if date_str >= cat_last_date[cat_key]:
            cat_last_date[cat_key] = date_str
            cat_last_merchant[cat_key] = merchant

    # Identify subscriptions — the billing-behaviour net (see docstring;
    # each threshold exists because the 2026-08-01 incident showed the old
    # rhythm-based net flagging kopi, haircuts and instalments).
    subscriptions = []
    prev_month = months[1] if len(months) > 1 else None

    # sub_overrides keys are trimmed + uppercased; normalise defensively so
    # a caller passing raw category names still matches.
    excluded_keys = {str(k).strip().upper()
                     for k in (exclude_keys or set())}

    for cat_key, month_data in cat_months.items():
        # User dismissed this category ("not a subscription") — skip it
        # regardless of how subscription-like the pattern looks.
        if cat_original[cat_key].strip().upper() in excluded_keys:
            continue

        # 1. Need at least 2 distinct months to call something recurring.
        if len(month_data) < 2:
            continue

        # 2. EXACTLY 1 charge per month in every month seen — subscriptions
        # bill once (two same-month Prudential premiums sharing one
        # insurance category = two policies).
        if any(len(amounts) != 1 for amounts in month_data.values()):
            continue

        # 3. Billing-date drift: every charge lands within a 4-day window
        # (a 29th–31st billing date clamping to Feb 28 stays within 4);
        # kopi and haircuts drift (the clippers.com.sg case).
        days = cat_days[cat_key]
        if days and max(days) - min(days) > 4:
            continue

        # 4. Every charge ≥ $0.50 — no micro-charge hiding under the avg.
        all_amounts = [a for amounts in month_data.values() for a in amounts]
        if any(a < 0.50 for a in all_amounts):
            continue
        avg_amount = sum(all_amounts) / len(all_amounts)

        # 5. Near-exact billing: consecutive monthly amounts step by
        # ≤ max(10% of the previous amount, $0.05). A 5–10% step passes
        # here and still registers as a price change downstream.
        months_seen = sorted(month_data.keys())
        step_ok = True
        for earlier, later in zip(months_seen, months_seen[1:]):
            prev_amt = month_data[earlier][0]
            curr_amt = month_data[later][0]
            if abs(curr_amt - prev_amt) > max(0.10 * prev_amt, 0.05):
                step_ok = False
                break
        if not step_ok:
            continue

        latest_month = months_seen[-1]
        latest_amount = month_data[latest_month][-1]

        # Determine status
        status = "active"
        price_change_pct = 0.0

        if current_month not in month_data:
            status = "possibly_cancelled"
        elif prev_month and prev_month in month_data:
            prev_avg = sum(month_data[prev_month]) / len(month_data[prev_month])
            if prev_avg > 0:
                price_change_pct = ((latest_amount - prev_avg) / prev_avg) * 100
                if abs(price_change_pct) > 5:
                    status = "price_change"

        # "new": earliest charge in the window is this month or last —
        # the pattern just crossed the 2-month recurrence threshold.
        # Checked LAST and only against plain "active" so price_change /
        # possibly_cancelled keep priority.
        if status == "active" and months_seen[0] in (current_month, prev_month):
            status = "new"

        subscriptions.append({
            "category": cat_original[cat_key],
            "last_merchant": cat_last_merchant[cat_key],
            "avg_amount": round(avg_amount, 2),
            "months_seen": months_seen,
            "latest_amount": round(latest_amount, 2),
            "status": status,
            "price_change_pct": round(price_change_pct, 1),
        })

    subscriptions.sort(key=lambda s: s["avg_amount"], reverse=True)

    # "new" subs are active charges — they count toward the monthly total.
    active = [s for s in subscriptions
              if s["status"] in ("active", "price_change", "new")]
    total = sum(s["latest_amount"] for s in active)

    return {
        "subscriptions": subscriptions,
        "total_monthly_subscriptions": round(total, 2),
        "new_this_month": [s for s in subscriptions if s["status"] == "new"],
        "price_changes": [s for s in subscriptions if s["status"] == "price_change"],
        "possibly_cancelled": [s for s in subscriptions if s["status"] == "possibly_cancelled"],
        "months_analyzed": sorted(months),
    }


# --- Journal: reply=journal narrative store ---
#
# Free-text replies the user sends to cron summary messages (9 PM daily
# review etc.) land here, keyed by date. The schema is deliberately thin:
# the point is to accumulate narrative content as a join key for later
# FTS5 indexing — not to impose structure up-front. Columns:
#   date | reply_text | txn_ids_referenced | tags
# `txn_ids_referenced` is comma-separated; `tags` is optional free-text.

def write_journal_entry(reply_text: str, date: str | None = None,
                        txn_ids_referenced: str = "",
                        tags: str = "") -> dict:
    """Append a journal entry. Creates the Journal tab on first call."""
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(JOURNAL_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=JOURNAL_SHEET, rows=500, cols=4)
        ws.append_row(
            ["date", "reply_text", "txn_ids_referenced", "tags"],
            value_input_option="USER_ENTERED",
        )

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    ws.append_row(
        [date, reply_text, txn_ids_referenced, tags],
        value_input_option="USER_ENTERED",
    )
    return {
        "status": "ok",
        "date": date,
        "reply_text": reply_text,
        "txn_ids_referenced": txn_ids_referenced,
        "tags": tags,
    }


def read_journal_entries(date: str | None = None, month: str | None = None,
                         limit: int = 20) -> list[dict]:
    """Read journal entries, most recent first. Returns empty list if the
    Journal tab doesn't exist yet (so callers don't special-case pre-
    experiment state)."""
    try:
        ws = get_spreadsheet().worksheet(JOURNAL_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()
    filtered = []
    for r in records:
        row_date = str(r.get("date", ""))
        if date and row_date != date:
            continue
        if month and not row_date.startswith(month):
            continue
        filtered.append({
            "date": row_date,
            "reply_text": str(r.get("reply_text", "")),
            "txn_ids_referenced": str(r.get("txn_ids_referenced", "")),
            "tags": str(r.get("tags", "")),
        })

    filtered.reverse()
    return filtered[:limit]


# --- MerchantMap: learned merchant → category mappings ---
#
# The MerchantMap tab lets the agent persist categorisation decisions across
# sessions. When the user corrects "Shopee → Miso Litter," writing a row here
# means the next Shopee transaction can be auto-categorised without re-asking.
#
# Schema: merchant_pattern | category | created_at
# Lookup is case-insensitive substring match against the incoming merchant
# string, with longest-pattern-wins precedence so more specific patterns
# (e.g. "GRABFOOD") beat shorter ones (e.g. "GRAB").

def read_merchant_mappings() -> list[dict]:
    """Read all rows from the MerchantMap tab. Returns an empty list if the
    tab doesn't exist yet (so callers don't have to special-case the
    pre-migration state)."""
    try:
        ws = get_spreadsheet().worksheet(MERCHANT_MAP_SHEET)
    except gspread.WorksheetNotFound:
        return []

    records = ws.get_all_records()
    mappings = []
    for r in records:
        pattern = str(r.get("merchant_pattern", "")).strip()
        category = str(r.get("category", "")).strip()
        if pattern and category:
            mappings.append({
                "merchant_pattern": pattern,
                "category": category,
                "created_at": str(r.get("created_at", "")),
            })
    return mappings


def lookup_merchant_category(merchant: str) -> dict | None:
    """Return the best MerchantMap match for a merchant string, or None.

    Match rule: case-insensitive substring; longest matching pattern wins.
    A return of None means the agent should fall back to its own judgement
    (and ask the user if uncertain)."""
    if not merchant:
        return None

    merchant_lower = merchant.lower()
    best_match = None
    best_length = 0

    for mapping in read_merchant_mappings():
        pattern_lower = mapping["merchant_pattern"].lower()
        if pattern_lower and pattern_lower in merchant_lower:
            if len(pattern_lower) > best_length:
                best_match = mapping
                best_length = len(pattern_lower)

    return best_match


def add_merchant_mapping(merchant_pattern: str, category: str) -> dict:
    """Append (or update) a row in the MerchantMap tab. If a row with the
    same `merchant_pattern` (case-insensitive) already exists, its category
    is updated in place rather than duplicated.

    Creates the MerchantMap tab on first call if it doesn't exist."""
    ss = get_spreadsheet()
    try:
        ws = ss.worksheet(MERCHANT_MAP_SHEET)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=MERCHANT_MAP_SHEET, rows=100, cols=3)
        ws.append_row(
            ["merchant_pattern", "category", "created_at"],
            value_input_option="USER_ENTERED",
        )

    pattern_clean = merchant_pattern.strip()
    today = datetime.now().strftime("%Y-%m-%d")

    # Check for existing row with same pattern (case-insensitive)
    all_values = ws.get_all_values()
    for idx, row in enumerate(all_values[1:], start=2):
        if row and row[0].strip().lower() == pattern_clean.lower():
            ws.update_cell(idx, 2, category)
            return {
                "status": "updated",
                "merchant_pattern": pattern_clean,
                "category": category,
                "row": idx,
            }

    ws.append_row(
        [pattern_clean, category, today],
        value_input_option="USER_ENTERED",
    )
    return {
        "status": "created",
        "merchant_pattern": pattern_clean,
        "category": category,
        "created_at": today,
    }


# --- WebhookLog sweep: find bank emails that never made it to Transactions ---
#
# The Apps Script writes every parsed bank email to the WebhookLog tab BEFORE
# firing the webhook, so even if the LLM call returns 529 / times out, the
# parsed payload is durable. This function diffs WebhookLog against the
# Transactions ledger by idempotency_key and returns anything that's missing
# so the user can review and log it.
#
# Idempotency key parity (JS side ↔ Python side) is critical here; mismatch
# = false positives in the missed list. The JS implementation is in
# apps-script/Code.gs::computeIdempotencyKey, verified by the testIdempotency
# KeyParity() Apps Script function against the pins in the Python unit test.

def sweep_missed_transactions(days_back: int = 7) -> dict:
    """Compare WebhookLog against Transactions to surface parsed bank emails
    that never made it into the ledger (e.g. because the LLM returned 529).

    Returns a dict with:
      - status: "ok" | "error"
      - total_webhook_logs: count of audit rows within the window
      - missed_count: count of audit rows with no matching Transactions row
      - missed: list of the unmatched audit rows (each with date, merchant,
        amount, currency, payment_method, idempotency_key, webhook_status)

    Rows already marked `matched == "yes"` in WebhookLog are excluded (the
    user has already resolved them in a prior sweep). Rows with `date` older
    than `days_back` are excluded to keep the response small.

    Returns {"status": "error"} if the WebhookLog tab doesn't exist — that's
    the signal the Apps Script audit log hasn't been deployed yet.
    """
    ss = get_spreadsheet()

    try:
        wl = ss.worksheet(WEBHOOK_LOG_SHEET)
    except gspread.WorksheetNotFound:
        return {
            "status": "error",
            "message": (
                "WebhookLog tab not found. Deploy the updated Apps Script "
                "first (apps-script/Code.gs) so bank emails get audited "
                "before the webhook fires."
            ),
        }

    ws = ss.worksheet(TRANSACTIONS_SHEET)

    # Collect idempotency keys already present in Transactions. Header
    # lookup rather than a fixed column index so this works against both
    # the legacy 8-column layout (no idem key → empty set, everything
    # looks missed) and the v4 11-column layout.
    idem_col_idx = _get_column_index(ws, "idempotency_key")
    txn_keys: set[str] = set()
    if idem_col_idx is not None:
        for value in ws.col_values(idem_col_idx)[1:]:  # skip header
            value = (value or "").strip()
            if value:
                txn_keys.add(value)

    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    log_records = wl.get_all_records()

    missed: list[dict] = []
    in_window = 0
    for row in log_records:
        row_date = str(row.get("date", ""))
        if row_date < cutoff:
            continue
        in_window += 1

        # Skip rows the user already reconciled in a previous sweep.
        if str(row.get("matched", "")).strip().lower() == "yes":
            continue

        key = str(row.get("idempotency_key", "")).strip()
        if not key:
            continue
        if key in txn_keys:
            continue

        missed.append({
            "date": row_date,
            "bank": str(row.get("bank", "")),
            "type": str(row.get("type", "")),
            "merchant": str(row.get("merchant", "")),
            "amount": row.get("amount", ""),
            "currency": str(row.get("currency", "")),
            "payment_method": str(row.get("payment_method", "")),
            "idempotency_key": key,
            "webhook_status": str(row.get("webhook_status", "")),
            "timestamp": str(row.get("timestamp", "")),
        })

    return {
        "status": "ok",
        "days_back": days_back,
        "total_webhook_logs": in_window,
        "missed_count": len(missed),
        "missed": missed,
    }

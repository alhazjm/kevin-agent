"""Supabase (PostgREST) mirror of `sheets_client` — the migration seam.

This module implements the SAME public function surface, signatures, and
return shapes as `tools/sheets_client.py`, backed by the Supabase Postgres
schema in `supabase/migrations/0001_init.sql` instead of the Google Sheet.
The flip (migration PR 3) swaps the backend by aliasing the import in
`tools/expense_sheets_tool.py`; nothing downstream — handlers, skills,
return-shape contracts, silence contract — changes.

Contract rules this module lives by:

- **Read paths return Sheet-shaped rows.** Every transaction row dict uses
  the Sheet's header keys (`Date`, `Merchant`, `Amount`, `Category`,
  `Payment Method`, `Notes`, `txn_id`, ...) so `card_optimiser`,
  `travel_mode`, and the analytics functions consume them unchanged. The
  DB's `txn_time` surfaces as `Time` (the column added for the
  time-in-key fix).
- **"Row numbers" are DB ids.** Sheets functions return `(row_number,
  row_dict)`; here the first element is the `transactions.id` primary key.
  Callers treat it as opaque, which they already do.
- **The idempotency formula is imported, never copied** (M17): the single
  Python source of truth stays `sheets_client._compute_idempotency_key`,
  parity-pinned against Code.gs.
- **Zero new dependencies.** Plain stdlib urllib against PostgREST, the
  same idiom as `_send_telegram_bubble`. The service key bypasses RLS;
  this module must only ever run server-side (Render), never in the PWA.
- **Atomic dedup.** `append_transaction` inserts with
  `on_conflict=idempotency_key` + `Prefer: resolution=ignore-duplicates`,
  so the duplicate check happens inside Postgres — the read-then-write
  race the Sheet path has cannot occur here.

Aggregation functions (`get_spending_summary`, `generate_spending_report`,
`detect_subscription_creep`) are verbatim copies of the sheets_client
bodies operating on this module's own readers — deliberate duplication so
behavior is pinned by the same expectations, not re-derived (drift risk
beats DRY here; see M11 for the house position on lookalike helpers).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

# Single source of truth for the dedup formula + pending/backfill/pot
# semantics (M17; the aggregate predicates are shared because they are
# byte-identical on both backends).
from tools.sheets_client import (
    PENDING_CATEGORY,
    TXN_ID_PATTERN,
    _compute_idempotency_key,
    _counts_in_totals,
    _is_backfill,
    _is_pending,
    _is_pot_internal,
    _rebucket_category,
)

__all_the_same_surface__ = True  # marker: keep function list in sync with sheets_client

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

# Sheet header key ↔ transactions column mapping. Read paths translate
# DB → Sheet keys; edit_transaction translates Sheet keys → DB columns.
_DB_TO_SHEET = {
    "date": "Date",
    "merchant": "Merchant",
    "amount": "Amount",
    "currency": "Currency",
    "category": "Category",
    "source": "Source",
    "payment_method": "Payment Method",
    "notes": "Notes",
    "txn_id": "txn_id",
    "telegram_message_id": "telegram_message_id",
    "idempotency_key": "idempotency_key",
    "txn_time": "Time",
}
_SHEET_TO_DB = {v: k for k, v in _DB_TO_SHEET.items()}


def _configured() -> bool:
    """True when both Supabase env vars are present. The tool layer's
    check_fn gates on this the same way `_sheets_configured` gates on the
    Google env vars."""
    return bool(os.environ.get("SUPABASE_URL")) and bool(
        os.environ.get("SUPABASE_SERVICE_KEY")
    )


def _request(method: str, path: str, params: dict | None = None,
             body: dict | list | None = None,
             prefer: str | None = None) -> tuple[int, object]:
    """One PostgREST round trip. Returns (status_code, parsed_json_or_None).

    Retries transient 429/5xx and network errors with the same 0.5/1/2s
    backoff shape as `sheets_client.get_spreadsheet` (copied idiom, not a
    new invention). Raises RuntimeError on persistent failure or non-2xx —
    callers are the same handlers that already catch sheets exceptions and
    convert them to `{"status": "error"}` dicts.
    """
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    url = f"{base}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw) if raw.strip() else None
                return (resp.status, parsed)
        except urllib.error.HTTPError as exc:
            status = exc.code
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            if status in _TRANSIENT_STATUSES and attempt < 2:
                last_err = exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(
                f"supabase {method} {path}: HTTP {status}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                last_err = exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"supabase {method} {path}: {exc}") from exc
    raise RuntimeError(f"supabase {method} {path}: {last_err}")  # defensive


def _to_sheet_row(rec: dict) -> dict:
    """Map a transactions DB record to the Sheet-header dict shape every
    downstream consumer expects. Amount stays float; None → ''."""
    row = {}
    for db_key, sheet_key in _DB_TO_SHEET.items():
        val = rec.get(db_key)
        if val is None:
            val = ""
        row[sheet_key] = val
    if isinstance(row.get("Amount"), str) and row["Amount"] != "":
        try:
            row["Amount"] = float(row["Amount"])
        except ValueError:
            pass
    return row


def _fetch_transactions(params: dict) -> list[dict]:
    """GET /transactions with the given PostgREST params → raw DB records."""
    _, rows = _request("GET", "transactions", params=params)
    return rows or []


# --- ledger -----------------------------------------------------------------


def append_transaction(date: str, merchant: str, amount: float, currency: str,
                       category: str, source: str = "email",
                       payment_method: str = "", notes: str = "",
                       txn_time: str = "", create_category: bool = False):
    """Insert a transaction. Mirrors `sheets_client.append_transaction`
    including the duplicate contract, with one improvement invisible to
    callers: dedup is enforced by the UNIQUE(idempotency_key) index inside
    Postgres, so two racing inserts can never both land.

    `txn_time` participates in the idempotency key (the time-in-key fix):
    two real same-day same-amount purchases carry different times and both
    log; a re-fired webhook carries the same time and dedupes. Empty time
    (manual/backfill) falls back to the legacy 4-field key format.

    The category must resolve to an existing Budget category (see
    `resolve_category`); unknown names return `status="unknown_category"`
    with nothing written unless `create_category=True` — creation is a
    user decision, never the LLM's (the 2026-07-30 phantom-category
    incident). Runs before the txn-id RPC so refusals consume nothing.
    """
    resolved = resolve_category(category)
    if resolved["match"] is not None:
        category = resolved["match"]
        created_category = False
    elif create_category:
        ensure_category_exists(category)
        created_category = True
    else:
        return _unknown_category_result(category, resolved)

    idem_key = _compute_idempotency_key(date, merchant, amount, payment_method,
                                        txn_time or "")
    _, txn_id = _request("POST", "rpc/next_txn_id", body={"d": date})

    record = {
        "txn_id": txn_id,
        "date": date,
        "txn_time": txn_time or "",
        "merchant": merchant,
        "amount": amount,
        "currency": currency,
        "category": category,
        "source": source,
        "payment_method": payment_method,
        "notes": notes,
        "telegram_message_id": None,
        "idempotency_key": idem_key,
    }
    _, inserted = _request(
        "POST", "transactions",
        params={"on_conflict": "idempotency_key"},
        body=[record],
        prefer="resolution=ignore-duplicates,return=representation",
    )

    if not inserted:
        # Conflict: the key already exists. Fetch the existing row so the
        # return shape matches the Sheet path's duplicate contract.
        existing = _fetch_transactions(
            {"idempotency_key": f"eq.{idem_key}", "limit": 1}
        )
        existing_txn_id = existing[0].get("txn_id", "") if existing else ""
        return {
            "status": "duplicate",
            "txn_id": existing_txn_id,
            "idempotency_key": idem_key,
            "message": "Duplicate transaction — already logged.",
        }

    # Informational 11-element list, same order as the Sheet row write.
    row = [date, merchant, amount, currency, category, source,
           payment_method, notes, txn_id, "", idem_key]
    result = {"status": "ok", "row": row, "txn_id": txn_id,
              "idempotency_key": idem_key}
    if created_category:
        # Same flag the handler's old post-log ensure step used to set —
        # the skill's "new category created with $0 limit" follow-up keys
        # on it.
        result["new_category_created"] = True
        result["new_category"] = category
    return result


def read_all_transaction_rows() -> list[dict]:
    """Every transaction, Sheet-shaped, in ledger order — the nightly
    export's source. Distinct from read_transactions (month-filtered)."""
    records = _fetch_transactions({"order": "date.asc,id.asc"})
    return [_to_sheet_row(r) for r in records]


def read_all_budget_rows() -> list[dict]:
    """Every (category, month, limit) budget row — the nightly export
    assembles the Sheet's per-month grid from these."""
    _, rows = _request("GET", "budgets",
                       params={"order": "category.asc,month.asc"})
    return [
        {
            "category": str(r.get("category", "")).strip(),
            "month": str(r.get("month", "")),
            "limit_amount": float(r.get("limit_amount", 0) or 0),
        }
        for r in rows or []
    ]


def read_transactions(month: str | None = None) -> list[dict]:
    if not month:
        month = datetime.now().strftime("%Y-%m")

    year, mnum = int(month.split("-")[0]), int(month.split("-")[1])
    ny, nm = (year + 1, 1) if mnum == 12 else (year, mnum + 1)
    records = _fetch_transactions({
        "date": f"gte.{month}-01",
        "and": f"(date.lt.{ny}-{nm:02d}-01)",
        "order": "date.asc,id.asc",
    })
    return [_to_sheet_row(r) for r in records]


def find_transaction_by_id(txn_id: str) -> tuple[int, dict] | None:
    rows = _fetch_transactions({"txn_id": f"eq.{txn_id}", "limit": 1})
    if not rows:
        return None
    return (rows[0]["id"], _to_sheet_row(rows[0]))


def find_transaction_by_message_id(telegram_message_id: str) -> tuple[int, dict] | None:
    rows = _fetch_transactions({
        "telegram_message_id": f"eq.{telegram_message_id}", "limit": 1,
        "order": "id.desc",
    })
    if not rows:
        return None
    return (rows[0]["id"], _to_sheet_row(rows[0]))


def find_transaction_row(merchant: str, amount: float,
                         date: str | None = None) -> tuple[int, dict] | None:
    """Fuzzy merchant+amount lookup, newest first — same matching rules as
    the Sheet version (bidirectional substring, amount within $0.01,
    optional exact date). Scans the most recent 500 rows, which comfortably
    covers the lookback any edit/delete realistically targets."""
    records = _fetch_transactions({"order": "id.desc", "limit": 500})

    search = merchant.strip().lower()
    for r in records:
        row_merchant = str(r.get("merchant", "")).strip().lower()
        try:
            row_amount = float(r.get("amount", 0))
        except (TypeError, ValueError):
            continue
        merchant_match = (
            len(search) >= 3 and (search in row_merchant or row_merchant in search)
        )
        if merchant_match and abs(row_amount - amount) < 0.01:
            if date and str(r.get("date", "")) != date:
                continue
            return (r["id"], _to_sheet_row(r))
    return None


def edit_transaction(merchant: str, amount: float, date: str | None = None,
                     updates: dict | None = None,
                     txn_id: str | None = None,
                     create_category: bool = False) -> dict:
    if not updates:
        return {"status": "error", "message": "No updates provided"}

    created_category = False
    if "Category" in updates:
        # Same gate as append_transaction — this is the exact path the
        # phantom category came through (ask-prompt reply → edit_expense
        # with the emoji-decorated option string).
        proposed = str(updates["Category"])
        resolved = resolve_category(proposed)
        if resolved["match"] is not None:
            updates = {**updates, "Category": resolved["match"]}
        elif create_category:
            ensure_category_exists(proposed)
            created_category = True
        else:
            return _unknown_category_result(proposed, resolved)

    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_id, existing = result

    patch = {}
    changed = {}
    for field, value in updates.items():
        db_col = _SHEET_TO_DB.get(field)
        if db_col is None:
            continue  # unknown column: skipped, same tolerance as the Sheet path
        patch[db_col] = value
        changed[field] = {"from": existing.get(field, ""), "to": value}

    if patch:
        _request("PATCH", "transactions", params={"id": f"eq.{row_id}"},
                 body=patch, prefer="return=minimal")

    result = {
        "status": "ok",
        "row": row_id,
        "txn_id": existing.get("txn_id", ""),
        "changes": changed,
    }
    if created_category:
        result["new_category_created"] = True
        result["new_category"] = updates.get("Category", "")
    return result


def delete_transaction(merchant: str, amount: float, date: str | None = None,
                       txn_id: str | None = None) -> dict:
    if txn_id:
        result = find_transaction_by_id(txn_id)
    else:
        result = find_transaction_row(merchant, amount, date)

    if result is None:
        identifier = f"txn_id={txn_id}" if txn_id else f"{merchant} ${amount}"
        return {"status": "error", "message": f"Transaction not found: {identifier}"}

    row_id, existing = result
    _request("DELETE", "transactions", params={"id": f"eq.{row_id}"},
             prefer="return=minimal")
    return {"status": "ok", "deleted_row": row_id, "transaction": existing}


def get_last_transaction() -> tuple[int, dict] | None:
    rows = _fetch_transactions({"order": "id.desc", "limit": 1})
    if not rows:
        return None
    return (rows[0]["id"], _to_sheet_row(rows[0]))


def link_telegram_message(txn_id: str, telegram_message_id: str) -> dict:
    result = find_transaction_by_id(txn_id)
    if result is None:
        return {"status": "error", "message": f"Transaction not found: txn_id={txn_id}"}

    row_id, _ = result
    _request("PATCH", "transactions", params={"id": f"eq.{row_id}"},
             body={"telegram_message_id": str(telegram_message_id)},
             prefer="return=minimal")
    return {
        "status": "ok",
        "txn_id": txn_id,
        "telegram_message_id": str(telegram_message_id),
        "row": row_id,
    }


# --- budgets ----------------------------------------------------------------
#
# The budgets table is (category, month 'YYYY-MM', limit_amount) — the
# Sheet's per-month-columns vs simple dual layout collapses into one shape.
# month_num maps into the CURRENT year, faithfully mirroring the Sheet's
# year-less Jan..Dec columns (including their December-of-last-year
# blind spot — fidelity beats correction here; the report's MoM comparison
# tolerates it the same way it always has).


def _month_str(month_num: int | None) -> str:
    if month_num is None:
        month_num = datetime.now().month
    return f"{datetime.now().year}-{month_num:02d}"


def read_budgets(month_num: int | None = None) -> list[dict]:
    month = _month_str(month_num)
    _, rows = _request("GET", "budgets",
                       params={"month": f"eq.{month}", "order": "category.asc"})
    budgets = []
    for r in rows or []:
        try:
            limit = float(r.get("limit_amount", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        budgets.append({"Category": str(r.get("category", "")).strip(),
                        "Monthly Limit": limit})
    return budgets


def update_budget_row(category: str, monthly_limit: float,
                      month_num: int | None = None):
    month = _month_str(month_num)
    # Snap to the canonical existing name (decoration/case-blind) and refuse
    # unknowns as a status dict rather than the old ValueError — the handler
    # json-dumps whatever comes back, and a raise here used to surface as a
    # raw tool error. Creation still goes through ensure_category_exists.
    resolved = resolve_category(category)
    if resolved["match"] is None:
        return _unknown_category_result(category, resolved)
    category = resolved["match"]

    _request("POST", "budgets",
             params={"on_conflict": "category,month"},
             body=[{"category": category, "month": month,
                    "limit_amount": monthly_limit}],
             prefer="resolution=merge-duplicates,return=minimal")
    if month_num is None:
        month_num = datetime.now().month
    return {"status": "ok", "category": category, "month": month_num,
            "new_limit": monthly_limit}


def _normalize_category(name: str) -> str:
    """Comparison key for category matching: decoration-blind, case-blind,
    whitespace-collapsed.

    Strips leading/trailing non-word runs — on 2026-07-30 the LLM decorated
    an ask-prompt option with an emoji and then logged the decorated string
    verbatim, minting a phantom "🍜 Personal - Food & Drinks" category next
    to the real one. Unicode letters count as word characters, so non-Latin
    category names survive; only symbol/emoji trim happens at the edges.
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    s = re.sub(r"^[\W_]+|[\W_]+$", "", s)
    return s.casefold()


def resolve_category(category: str) -> dict:
    """Snap a proposed category onto the existing Budget categories.

    Returns ``{"match": <canonical name>}`` when the proposal —
    emoji-stripped, case-insensitive — equals an existing category, else
    ``{"match": None, "existing_categories": [...], "closest": [...]}``.
    ``UNCATEGORIZED`` (the reserved pending value) always matches itself so
    the pending flow never trips the guard. This is the gate that keeps the
    LLM from minting categories: writers call it and refuse unknown names
    unless the caller explicitly opted into creation.
    """
    proposed = _normalize_category(category)
    if proposed == _normalize_category(PENDING_CATEGORY):
        return {"match": PENDING_CATEGORY}

    _, rows = _request("GET", "budgets", params={"select": "category"})
    canon: dict[str, str] = {}
    for r in rows or []:
        name = str(r.get("category", "")).strip()
        key = _normalize_category(name)
        if name and key and key not in canon:
            canon[key] = name

    if proposed and proposed in canon:
        return {"match": canon[proposed]}

    closest_keys = difflib.get_close_matches(
        proposed, list(canon.keys()), n=3, cutoff=0.6)
    return {
        "match": None,
        "existing_categories": sorted(set(canon.values())),
        "closest": [canon[k] for k in closest_keys],
    }


def _unknown_category_result(category: str, resolved: dict) -> dict:
    """The shared refusal shape for writers hitting an unknown category."""
    return {
        "status": "unknown_category",
        "category": category,
        "closest": resolved["closest"],
        "existing_categories": resolved["existing_categories"],
        "message": (
            f"Category '{category}' does not exist in the Budget sheet — "
            "nothing was written. Pick one of `closest` / "
            "`existing_categories` and retry, or ask the user. Only retry "
            "with create_category=true when the user themselves asked for "
            "a brand-new category by that exact name."
        ),
    }


def ensure_category_exists(category: str) -> dict:
    _, rows = _request("GET", "budgets", params={"select": "category"})
    existing = {str(r.get("category", "")).strip().lower() for r in rows or []}
    if category.strip().lower() in existing:
        return {"status": "exists", "category": category}

    _request("POST", "budgets",
             params={"on_conflict": "category,month"},
             body=[{"category": category, "month": _month_str(None),
                    "limit_amount": 0}],
             prefer="resolution=ignore-duplicates,return=minimal")
    return {"status": "created", "category": category}


# --- aggregation (verbatim sheets_client bodies over this module's readers) --


def _trip_category_map() -> dict[str, str]:
    """label → trip_category from travel_mode, {} when unavailable (the
    guarded-read pattern: an absent table or a transient read failure must
    never break a report). Feeds _rebucket_category so a [trip:]-tagged
    top-up consumes the TRIP's budgets row — the PWA does the same, and
    without it an all-YouTrip trip reads $0 spent of its budget."""
    try:
        return {str(r.get("label", "")).strip(): str(r.get("trip_category", "")).strip()
                for r in read_travel_mode_records()
                if str(r.get("label", "")).strip()
                and str(r.get("trip_category", "")).strip()}
    except Exception:
        return {}


def get_spending_summary(month: str | None = None) -> dict:
    if not month:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}
    trips = _trip_category_map()

    spending = {}
    pending_count = 0
    for t in transactions:
        if _is_pending(t):
            pending_count += 1
            continue
        if not _counts_in_totals(t):
            continue
        cat = _rebucket_category(t, trips)
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


def generate_spending_report(month: str | None = None) -> dict:
    if not month:
        month = datetime.now().strftime("%Y-%m")

    month_num = int(month.split("-")[1])
    year = int(month.split("-")[0])

    transactions = read_transactions(month)
    budgets = {b["Category"]: b["Monthly Limit"] for b in read_budgets(month_num)}

    prev_month_num = month_num - 1 if month_num > 1 else 12
    prev_year = year if month_num > 1 else year - 1
    prev_month_str = f"{prev_year}-{prev_month_num:02d}"
    prev_transactions = read_transactions(prev_month_str)
    trips = _trip_category_map()

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
        cat = _rebucket_category(t, trips)
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

    daily_spend: dict[str, float] = defaultdict(float)
    for t in transactions:
        if not _counts_in_totals(t):
            continue
        d = str(t.get("Date", ""))
        daily_spend[d] += float(t.get("Amount", 0))
    daily = [{"date": d, "total": round(s, 2)}
             for d, s in sorted(daily_spend.items())]

    # Both sides of the MoM comparison use the same predicate — comparing a
    # filtered current month against an unfiltered previous month would
    # manufacture phantom swings.
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
        cat = _rebucket_category(t, trips)
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


def read_sub_overrides() -> list[dict]:
    """All `sub_overrides` rows (migration 0006, shipped with the PWA
    dismissal UI): `merchant_key` = the dismissed key trimmed + UPPERCASED,
    `verdict` = 'exclude'. The COLUMN NAME is historical: since the
    category re-key of the subscription detector the PWA writes normalized
    CATEGORY keys into `merchant_key`; pre-existing merchant-key rows are
    stale but harmless (they match no category). None values become '' to
    match the rest of the data layer. Raises (via `_request`) when the
    table doesn't exist yet — `detect_subscription_creep` guards its call
    so pre-migration deploys degrade to "no overrides"."""
    _, rows = _request("GET", "sub_overrides",
                       params={"order": "merchant_key.asc"})
    out = []
    for r in rows or []:
        out.append({k: ("" if v is None else v) for k, v in r.items()})
    return out


# Money movements, not billing: the PWA's TRANSFER_CATS ("youtrip top-up")
# and LENDING_CATS ("lending") — a monthly top-up or IOU repayment is
# perfectly billing-shaped but is never a subscription. Compared against
# the trimmed-lowercased category name; keep in sync with the PWA.
_SUB_EXCLUDED_CATEGORIES = frozenset({"youtrip top-up", "lending"})


def detect_subscription_creep(months_back: int = 3,
                              exclude_keys: set[str] | None = None) -> dict:
    """Verbatim sheets_client logic; two deliberate differences only:

    - the row fetch (whole ledger via PostgREST instead of
      `ws.get_all_records()`);
    - ASYMMETRY: when `exclude_keys` is None, this side defaults it by
      reading the `sub_overrides` table (migration 0006 — PWA dismissals:
      merchant_key = trimmed-UPPERCASED key, verdict 'exclude'; the
      column NAME is historical — since the category re-key the PWA
      writes normalized CATEGORY keys into it, and pre-existing
      merchant-key rows are stale but harmless). The sheets_client twin
      has no access to that table, so its None means "no overrides". The
      filter itself is applied identically inside both math blocks
      (parity). The fetch is guarded: a missing table (pre-migration)
      degrades to "no overrides" and never raises out of this function.

    Groups by CATEGORY, not merchant — two live incidents shaped this
    (the sheets_client twin's docstring tells the full story):

    Incident 1 — rhythm ≠ billing (2026-08-01): the original net matched
    anything with a monthly rhythm (Atome instalments, two Prudential
    premiums, a barber, Shaw Theatres, a $7 kopi, Polar Puffs) → the
    five billing-behaviour thresholds below.

    Incident 2 — merchant-string wobble; MerchantMap already solved
    identity: Spotify's merchant string carries a unique payment ref
    EVERY month ("Spotify P424C25C39", "Spotify P435EF3A4E", …), so
    merchant grouping fragmented real subs into single-month groups that
    never reached the 2-month threshold — while MerchantMap reliably
    lands them in dedicated categories. The CATEGORY is the stable key,
    and it auto-kills the incident-1 false positives (two policies in
    one insurance category = 2 charges/month → rejected; instalments
    fragment across categories).

    Billing-behaviour thresholds (unchanged by the re-key):
      1. Category seen in ≥2 distinct months.
      2. EXACTLY 1 charge per month in every month seen —
         subscriptions bill once.
      3. Day-of-month spread ≤ 4 across all charges (max − min; a
         29th–31st billing date clamping to Feb 28 stays within 4) —
         billers land on the same date; kopi and haircuts drift.
      4. Every charge ≥ $0.50.
      5. Consecutive monthly amounts step ≤ max(10% of the previous
         amount, $0.05) — near-exact billing; a 5–10% step still
         registers as a price change downstream.

    Status semantics (byte-parallel with sheets_client — change together):
    a subscription is **new** when its earliest charge within the lookback
    window falls in the current or previous month, i.e. the pattern just
    crossed the 2-month recurrence threshold. Precedence: `price_change`
    and `possibly_cancelled` beat `new`; `new` beats plain `active`. "New"
    subscriptions still count toward `total_monthly_subscriptions` (they
    are active charges). Before 2026-07 nothing ever assigned "new", so
    `new_this_month` was always empty — the regression tests pin the fix.

    Each subscription entry's identity is its `category`; `last_merchant`
    (the merchant string of the most recent charge) rides along for
    display/debugging only.
    """
    _, raw = _request("GET", "transactions",
                      params={"order": "date.asc,id.asc"})
    records = [_to_sheet_row(r) for r in raw or []]

    if exclude_keys is None:
        # Default from the sub_overrides table. Guarded: pre-migration the
        # table doesn't exist and _request raises RuntimeError on the
        # PostgREST error — degrade to "no overrides", never break the
        # subscription report over a dismissal nicety.
        try:
            exclude_keys = {
                str(r.get("merchant_key", "")).strip().upper()
                for r in read_sub_overrides()
                if str(r.get("verdict", "")).strip().lower() == "exclude"
            }
        except Exception:
            exclude_keys = set()

    now = datetime.now()
    current_month = now.strftime("%Y-%m")

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


# --- insights ---------------------------------------------------------------


def write_insight(insight: str, category: str = "general",
                  month: str | None = None) -> dict:
    if not month:
        month = datetime.now().strftime("%Y-%m")
    today = datetime.now().strftime("%Y-%m-%d")
    _request("POST", "insights",
             body=[{"date": today, "category": category, "month": month,
                    "insight": insight}],
             prefer="return=minimal")
    return {"status": "ok", "insight": insight, "category": category,
            "month": month}


def get_insights(month: str | None = None, category: str | None = None,
                 limit: int = 20) -> list[dict]:
    params = {"order": "id.desc"}
    if month:
        params["month"] = f"eq.{month}"
    _, rows = _request("GET", "insights", params=params)

    filtered = []
    for r in rows or []:
        if category and str(r.get("category", "")).lower() != category.lower():
            continue
        filtered.append({
            "date": str(r.get("date", "")),
            "category": str(r.get("category", "")),
            "month": str(r.get("month", "")),
            "insight": str(r.get("insight", "")),
        })
    return filtered[:limit]


def _insightable(t: dict) -> bool:
    """Rows that count toward insight aggregates — same predicate as the
    report totals: excludes UNCATEGORIZED, backfill imports (M14), and
    pot-internal YouTrip spends."""
    return _counts_in_totals(t)


def generate_daily_insight(month: str | None = None) -> dict:
    """Compute ONE deterministic spending insight and persist it via
    write_insight (category keys prefixed ``auto:``).

    Exists because the daily-review cron's discretionary "call write_insight
    if something stands out" step was silently dropped by the small prod
    model almost every night (one row written in months of runs) — a tool
    call the prompt REQUIRES is reliable; model judgment is not. Rules are
    ranked (surge > quiet win > new-merchant habit > big day > budget pace);
    a 3-day per-rule recency window keeps consecutive nights from repeating
    themselves, and at most one auto row is written per day.

    Returns status ok (written), exists (today's auto row already exists),
    no_match (every candidate was a recent repeat), or error. Never raises
    (runs inside a cron flow) and never sends Telegram.
    """
    try:
        import calendar
        now = datetime.now()
        if not month:
            month = now.strftime("%Y-%m")
        today_str = now.strftime("%Y-%m-%d")
        day = now.day
        year, month_num = int(month.split("-")[0]), int(month.split("-")[1])
        days_in_month = calendar.monthrange(year, month_num)[1]

        recent = get_insights(limit=12)
        for r in recent:
            if r["date"] == today_str and r["category"].startswith("auto:"):
                return {"status": "exists", "insight": r["insight"],
                        "reason": "auto insight already written today"}

        def _recently_used(key: str, days: int = 3) -> bool:
            for r in recent:
                if r["category"] != key:
                    continue
                try:
                    age = (now - datetime.strptime(r["date"], "%Y-%m-%d")).days
                except (ValueError, TypeError):
                    continue
                if age <= days:
                    return True
            return False

        cur = [t for t in read_transactions(month) if _insightable(t)]
        prev_month_num = month_num - 1 if month_num > 1 else 12
        prev_year = year if month_num > 1 else year - 1
        prev = [t for t in read_transactions(f"{prev_year}-{prev_month_num:02d}")
                if _insightable(t)]
        # month-over-month pace compares like-for-like: last month clipped
        # to the same day-of-month we're at now
        prev_to_day = [t for t in prev
                       if str(t.get("Date", ""))[8:10].isdigit()
                       and int(str(t.get("Date", ""))[8:10]) <= day]

        def _by_cat(rows: list[dict]) -> dict[str, float]:
            agg: dict[str, float] = defaultdict(float)
            for t in rows:
                agg[t.get("Category", "Miscellaneous")] += float(t.get("Amount", 0))
            return agg

        cur_cat = _by_cat(cur)
        prev_cat = _by_cat(prev_to_day)
        budgets = {b["Category"]: float(b.get("Monthly Limit", 0) or 0)
                   for b in read_budgets(month_num)}
        budget_total = sum(budgets.values())
        spent_total = sum(cur_cat.values())

        candidates: list[tuple[str, str]] = []   # (rule key, insight text)

        # 1. surge — the category running furthest ahead of last month's pace
        surge = None
        for cat, amt in cur_cat.items():
            base = prev_cat.get(cat, 0.0)
            if amt >= 80 and base > 0 and amt >= 1.6 * base and amt - base >= 60:
                if surge is None or amt - base > surge[1]:
                    surge = (cat, amt - base, amt, base)
        if surge:
            cat, _, amt, base = surge
            candidates.append((f"auto:surge:{cat}",
                f"{cat} is running hot — ${amt:,.0f} by day {day} vs "
                f"${base:,.0f} at this point last month."))

        # 2. quiet win — a normally-heavy category at half pace or better
        quiet = None
        for cat, base in prev_cat.items():
            amt = cur_cat.get(cat, 0.0)
            if base >= 100 and amt <= 0.5 * base:
                if quiet is None or base > quiet[1]:
                    quiet = (cat, base, amt)
        if quiet:
            cat, base, amt = quiet
            candidates.append((f"auto:quiet:{cat}",
                f"{cat} is way down — ${amt:,.0f} so far vs ${base:,.0f} "
                f"by day {day} last month."))

        # 3. habit — a merchant unseen last month, already charged 3+ times
        prev_merchants = {str(t.get("Merchant", "")).strip().upper() for t in prev}
        m_count: dict[str, int] = defaultdict(int)
        m_total: dict[str, float] = defaultdict(float)
        for t in cur:
            m = str(t.get("Merchant", "")).strip()
            m_count[m] += 1
            m_total[m] += float(t.get("Amount", 0))
        habit = None
        for m, n in m_count.items():
            if n >= 3 and m and m.upper() not in prev_merchants:
                if habit is None or n > m_count[habit]:
                    habit = m
        if habit:
            candidates.append((f"auto:habit:{habit[:40]}",
                f"New habit forming: {m_count[habit]} charges at {habit} "
                f"this month (${m_total[habit]:,.2f})."))

        # 4. big day — the month's largest spending day just happened
        day_total: dict[str, float] = defaultdict(float)
        day_count: dict[str, int] = defaultdict(int)
        for t in cur:
            d = str(t.get("Date", ""))
            day_total[d] += float(t.get("Amount", 0))
            day_count[d] += 1
        if day_total:
            top_day, top_amt = max(day_total.items(), key=lambda kv: kv[1])
            try:
                top_dt = datetime.strptime(top_day, "%Y-%m-%d")
                if (now - top_dt).days <= 2 and top_amt >= 120:
                    candidates.append(("auto:bigday",
                        f"{top_dt.strftime('%A')} was the month's biggest day — "
                        f"${top_amt:,.2f} across {day_count[top_day]} transactions."))
            except ValueError:
                pass

        # 5. fallback — plain budget pace, so there is always a candidate
        if budget_total > 0:
            delta = budget_total * day / days_in_month - spent_total
            candidates.append(("auto:pace",
                f"Day {day} of {days_in_month}: ${spent_total:,.0f} of "
                f"${budget_total:,.0f} used — ${abs(delta):,.0f} "
                f"{'under' if delta >= 0 else 'over'} pace."))

        for key, text in candidates:
            if _recently_used(key):
                continue
            write_insight(insight=text, category=key, month=month)
            return {"status": "ok", "insight": text, "rule": key}

        return {"status": "no_match",
                "reason": "every candidate insight was written in the last few days"}
    except Exception as exc:  # cron path — a raise here would kill the review
        return {"status": "error",
                "message": f"generate_daily_insight failed: {exc}"}


# --- journal ----------------------------------------------------------------


def write_journal_entry(reply_text: str, date: str | None = None,
                        txn_ids_referenced: str = "",
                        tags: str = "") -> dict:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    _request("POST", "journal",
             body=[{"date": date, "reply_text": reply_text,
                    "txn_ids_referenced": txn_ids_referenced, "tags": tags}],
             prefer="return=minimal")
    return {
        "status": "ok",
        "date": date,
        "reply_text": reply_text,
        "txn_ids_referenced": txn_ids_referenced,
        "tags": tags,
    }


def read_journal_entries(date: str | None = None, month: str | None = None,
                         limit: int = 20) -> list[dict]:
    params = {"order": "id.desc"}
    if date:
        params["date"] = f"eq.{date}"
    _, rows = _request("GET", "journal", params=params)

    filtered = []
    for r in rows or []:
        row_date = str(r.get("date", ""))
        if month and not row_date.startswith(month):
            continue
        filtered.append({
            "date": row_date,
            "reply_text": str(r.get("reply_text", "")),
            "txn_ids_referenced": str(r.get("txn_ids_referenced", "")),
            "tags": str(r.get("tags", "")),
        })
    return filtered[:limit]


# --- merchant map -----------------------------------------------------------


def read_merchant_mappings() -> list[dict]:
    _, rows = _request("GET", "merchant_map", params={"order": "pattern.asc"})
    mappings = []
    for r in rows or []:
        pattern = str(r.get("pattern", "")).strip()
        category = str(r.get("category", "")).strip()
        if pattern and category:
            mappings.append({
                "merchant_pattern": pattern,
                "category": category,
                "created_at": str(r.get("created_at", "")),
            })
    return mappings


def lookup_merchant_category(merchant: str) -> dict | None:
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
    pattern_clean = merchant_pattern.strip()
    today = datetime.now().strftime("%Y-%m-%d")

    # Learned mappings feed every future auto-categorisation, so a phantom
    # here would propagate forever. Snap to the canonical Budget name and
    # refuse unknowns outright — learning never creates categories.
    resolved = resolve_category(category)
    if resolved["match"] is None:
        return _unknown_category_result(category, resolved)
    category = resolved["match"]

    # Case-insensitive existence check mirrors the Sheet path; the DB also
    # enforces it via the unique lower(pattern) index.
    for mapping in read_merchant_mappings():
        if mapping["merchant_pattern"].lower() == pattern_clean.lower():
            _request("PATCH", "merchant_map",
                     params={"pattern": f"eq.{mapping['merchant_pattern']}"},
                     body={"category": category}, prefer="return=minimal")
            return {
                "status": "updated",
                "merchant_pattern": pattern_clean,
                "category": category,
                "row": mapping["merchant_pattern"],
            }

    _request("POST", "merchant_map",
             body=[{"pattern": pattern_clean, "category": category,
                    "created_at": today}],
             prefer="return=minimal")
    return {
        "status": "created",
        "merchant_pattern": pattern_clean,
        "category": category,
        "created_at": today,
    }


# --- webhook-log sweep ------------------------------------------------------


def sweep_missed_transactions(days_back: int = 7) -> dict:
    """Diff webhook_log against transactions by idempotency_key — same
    contract as the Sheet version. Until migration PR 4 repoints the Apps
    Script audit write, the webhook_log table only holds backfilled rows;
    the Sheet's WebhookLog tab stays authoritative for the sweep until
    then (the flip PR keeps the sweep on sheets_client until PR 4 lands)."""
    _, log_rows = _request("GET", "webhook_log", params={"order": "id.asc"})
    log_rows = log_rows or []

    _, key_rows = _request("GET", "transactions",
                           params={"select": "idempotency_key"})
    txn_keys = {
        str(r.get("idempotency_key") or "").strip()
        for r in key_rows or []
        if str(r.get("idempotency_key") or "").strip()
    }

    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    missed: list[dict] = []
    in_window = 0
    for row in log_rows:
        row_date = str(row.get("txn_date", ""))
        if row_date < cutoff:
            continue
        in_window += 1

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
            "timestamp": str(row.get("ts", "")),
        })

    return {
        "status": "ok",
        "days_back": days_back,
        "total_webhook_logs": in_window,
        "missed_count": len(missed),
        "missed": missed,
    }


# --- card optimiser / travel mode data layer (migration 3b) -----------------
# These mirror the Sheet tabs' get_all_records() shapes exactly: the
# supabase column names ARE the Sheet header names for these tabs (the
# 0001 migration was designed that way), so the consuming modules keep
# their record-dict code unchanged. None values become "" to match
# gspread's empty-cell behavior.


def _records(path: str, order: str) -> list[dict]:
    _, rows = _request("GET", path, params={"order": order})
    out = []
    for r in rows or []:
        out.append({k: ("" if v is None else v) for k, v in r.items()})
    return out


def read_cards_records() -> list[dict]:
    return _records("cards", "card_id.asc")


def read_card_strategy_records() -> list[dict]:
    return _records("card_strategy", "category.asc")


def upsert_card_strategy(record: dict) -> None:
    """Insert-or-replace one CardStrategy row (PK: category). Used by
    set_category_primary and the lazy promo reversion — whole-row upsert
    replaces the Sheet's per-cell update_cell dance."""
    _request("POST", "card_strategy",
             params={"on_conflict": "category"},
             body=[record],
             prefer="resolution=merge-duplicates,return=minimal")


def read_card_nudge_log() -> list[dict]:
    return _records("card_nudge_log", "id.asc")


def append_card_nudge(cycle_window: str, card_id: str, category: str,
                      threshold: int, triggering_txn_id: str = "",
                      fallback_card_id: str = "") -> bool:
    """Record a cap nudge. Returns False if the dedup tuple
    (cycle_window, card_id, category, threshold) already exists — the
    UNIQUE constraint makes dedup atomic, closing the read-then-write race
    the Sheet version had between concurrent webhook deliveries."""
    _, rows = _request(
        "POST", "card_nudge_log",
        params={"on_conflict": "cycle_window,card_id,category,threshold"},
        body=[{"cycle_window": cycle_window, "card_id": card_id,
               "category": category, "threshold": threshold,
               "triggering_txn_id": triggering_txn_id,
               "fallback_card_id": fallback_card_id}],
        prefer="resolution=ignore-duplicates,return=representation")
    return bool(rows)


def read_travel_mode_records() -> list[dict]:
    return _records("travel_mode", "id.asc")


def insert_travel_mode_row(label: str, start_date: str, end_date: str,
                           trip_category: str, budget_map: str = "",
                           total_budget: float = 0.0,
                           notes: str = "") -> dict:
    """Insert one travel_mode trip row and return the created record.

    Duplicate-label refusal lives in `travel_mode.create_trip` (labels are
    matched case-insensitively there); this is the raw insert. total_budget
    is the PLANNED envelope — the funded pot is derived by summing
    `[trip:<label>]`-tagged top-ups, never written back here.
    """
    _, rows = _request(
        "POST", "travel_mode",
        body=[{"label": label, "start_date": start_date,
               "end_date": end_date, "trip_category": trip_category,
               "budget_map": budget_map, "total_budget": total_budget,
               "notes": notes}],
        prefer="return=representation")
    rec = (rows or [{}])[0]
    return {k: ("" if v is None else v) for k, v in rec.items()}


def read_trip_nudge_log() -> list[dict]:
    return _records("trip_nudge_log", "id.asc")


def append_trip_nudge(trip_label: str, bucket: str, threshold: int,
                      budget_at_nudge: float,
                      triggering_txn_id: str = "") -> bool:
    """Record a trip-bucket nudge. Returns False when the dedup tuple
    (trip_label, bucket, threshold, budget_at_nudge) already exists;
    budget_at_nudge in the tuple is what re-arms alerts after a mid-trip
    reallocation."""
    _, rows = _request(
        "POST", "trip_nudge_log",
        params={"on_conflict": "trip_label,bucket,threshold,budget_at_nudge"},
        body=[{"trip_label": trip_label, "bucket": bucket,
               "threshold": threshold, "budget_at_nudge": budget_at_nudge,
               "triggering_txn_id": triggering_txn_id}],
        prefer="resolution=ignore-duplicates,return=representation")
    return bool(rows)


# --- loans (IOU ledger) ------------------------------------------------------
# The `loans` table (migration 0005) tracks money lent until it comes back.
# Loan/repayment LOGIC (oldest-open matching, the offsetting negative ledger
# txn) lives in tools/loans.py — this is the raw row I/O only, same split as
# the card/travel data layer above.


def insert_loan(person: str, amount: float, lent_date: str,
                channel: str = "paylah", txn_id: str = "",
                notes: str = "") -> dict:
    """Insert one loans row (status defaults to 'open' in the DB) and
    return the created record. Validation (person non-empty, amount > 0,
    date shape) lives in `loans.create_loan`."""
    _, rows = _request(
        "POST", "loans",
        body=[{"person": person, "amount": amount, "lent_date": lent_date,
               "channel": channel, "txn_id": txn_id, "notes": notes}],
        prefer="return=representation")
    rec = (rows or [{}])[0]
    return {k: ("" if v is None else v) for k, v in rec.items()}


def read_loans(status: str | None = None) -> list[dict]:
    """All loans rows, oldest first (lent_date then id — the order
    `mark_loan_repaid`'s person-matching relies on). Optional exact-match
    status filter ('open' / 'repaid'). None values become '' to match the
    rest of the data layer."""
    params = {"order": "lent_date.asc,id.asc"}
    if status:
        params["status"] = f"eq.{status}"
    _, rows = _request("GET", "loans", params=params)
    out = []
    for r in rows or []:
        out.append({k: ("" if v is None else v) for k, v in r.items()})
    return out


def update_loan(loan_id: int, patch: dict) -> dict:
    """PATCH one loans row by primary key and return the updated record."""
    _, rows = _request(
        "PATCH", "loans",
        params={"id": f"eq.{loan_id}"},
        body=patch,
        prefer="return=representation")
    rec = (rows or [{}])[0]
    return {k: ("" if v is None else v) for k, v in rec.items()}

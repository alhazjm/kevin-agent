#!/usr/bin/env python3
"""One-time backfill: copy the Google Sheet into Supabase (migration PR 2).

Run from the repo root with all four env vars set:

    GOOGLE_SERVICE_ACCOUNT_JSON=... GSPREAD_SPREADSHEET_ID=... \
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \
    python scripts/backfill_supabase.py

Re-runnable by construction:
  - transactions upsert on txn_id (always present, unlike idempotency_key
    which is empty on legacy rows) with ignore-duplicates
  - budgets / merchant_map / cards / card_strategy upsert on their keys
  - nudge logs upsert on their dedup tuples
  - travel_mode / webhook_log skip rows already present (no natural key)
  - insights / journal only fill an EMPTY table (append-only tabs with no
    natural key — refusing to re-import beats duplicating)

Also seeds txn_id_counters from the per-day txn_id maxima so next_txn_id()
continues each day's sequence instead of colliding with backfilled ids.
This is an operator script: printing progress is the point (the no-prints
rule is for tools/, which run inside the agent).
"""

import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import gspread  # noqa: E402

from tools import sheets_client, supabase_client  # noqa: E402
from tools.sheets_client import TXN_ID_PATTERN, get_spreadsheet  # noqa: E402

CHUNK = 200


def _dedupe(rows: list[dict], key_fn) -> list[dict]:
    """Collapse rows sharing a conflict key to the LAST occurrence. Postgres
    rejects a single INSERT ... ON CONFLICT batch that touches the same key
    twice (error 21000), and sheet tabs can carry duplicate rows (that's how
    this bit us: a category listed twice in Budget). Last-wins matches how
    the Sheet readers behave (dict comprehensions keep the last row)."""
    seen: dict = {}
    for r in rows:
        seen[key_fn(r)] = r
    return list(seen.values())


def _post(table: str, rows: list[dict], on_conflict: str | None = None,
          merge: bool = False) -> int:
    """Batch-insert rows; returns how many were sent. ignore-duplicates by
    default so re-runs are safe; merge-duplicates where updates are wanted."""
    if not rows:
        return 0
    resolution = "merge-duplicates" if merge else "ignore-duplicates"
    params = {"on_conflict": on_conflict} if on_conflict else None
    for i in range(0, len(rows), CHUNK):
        supabase_client._request(
            "POST", table, params=params, body=rows[i:i + CHUNK],
            prefer=f"resolution={resolution},return=minimal",
        )
    return len(rows)


def _records(tab_name: str) -> list[dict]:
    """get_all_records for a tab, [] if the tab doesn't exist."""
    try:
        return get_spreadsheet().worksheet(tab_name).get_all_records()
    except gspread.WorksheetNotFound:
        return []


def _existing(table: str, select: str) -> set[str]:
    _, rows = supabase_client._request("GET", table, params={"select": select})
    return {str(r.get(select, "")).strip() for r in rows or []}


def _count(table: str) -> int:
    _, rows = supabase_client._request("GET", table, params={"select": "id",
                                                             "limit": 1})
    return len(rows or [])


def backfill_transactions() -> None:
    records = _records(sheets_client.TRANSACTIONS_SHEET)
    rows = []
    legacy_seq = 0
    for r in records:
        merchant = str(r.get("Merchant", "")).strip()
        date = str(r.get("Date", "")).strip()
        if not merchant or not date:
            continue
        txn_id = str(r.get("txn_id", "")).strip()
        if not txn_id:
            legacy_seq += 1
            txn_id = f"txn_legacy_bf{legacy_seq:03d}"
        idem = str(r.get("idempotency_key", "")).strip() or None
        try:
            amount = float(r.get("Amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        rows.append({
            "txn_id": txn_id,
            "date": date,
            "txn_time": str(r.get("Time", "")).strip(),
            "merchant": merchant,
            "amount": amount,
            "currency": str(r.get("Currency", "")).strip() or "SGD",
            "category": str(r.get("Category", "")).strip() or "Miscellaneous",
            "source": (str(r.get("Source", "")).strip().lower()
                       if str(r.get("Source", "")).strip().lower()
                       in ("email", "manual", "backfill") else "manual"),
            "payment_method": str(r.get("Payment Method", "")).strip(),
            "notes": str(r.get("Notes", "")),
            "telegram_message_id": str(r.get("telegram_message_id", "")).strip() or None,
            "idempotency_key": idem,
        })
    rows = _dedupe(rows, lambda r: r["txn_id"])
    n = _post("transactions", rows, on_conflict="txn_id")
    print(f"transactions: {n} rows sent")

    # Seed the per-day counters from the ids we just imported.
    day_max: dict[str, int] = defaultdict(int)
    for row in rows:
        m = TXN_ID_PATTERN.match(row["txn_id"])
        if m:
            day_max[m.group(1)] = max(day_max[m.group(1)], int(m.group(2)))
    counters = [{"day": d, "seq": s} for d, s in sorted(day_max.items())]
    _post("txn_id_counters", counters, on_conflict="day", merge=True)
    print(f"txn_id_counters: {len(counters)} days seeded")


def backfill_budgets() -> None:
    year = datetime.now().year
    rows = []
    for month_num in range(1, 13):
        for b in sheets_client.read_budgets(month_num):
            rows.append({
                "category": b["Category"],
                "month": f"{year}-{month_num:02d}",
                "limit_amount": b["Monthly Limit"],
            })
    rows = _dedupe(rows, lambda r: (r["category"], r["month"]))
    n = _post("budgets", rows, on_conflict="category,month", merge=True)
    print(f"budgets: {n} (category, month) rows sent")


def backfill_merchant_map() -> None:
    rows = []
    for m in sheets_client.read_merchant_mappings():
        row = {"pattern": m["merchant_pattern"], "category": m["category"]}
        if m.get("created_at"):
            row["created_at"] = m["created_at"]
        rows.append(row)
    rows = _dedupe(rows, lambda r: r["pattern"].lower())
    n = _post("merchant_map", rows, on_conflict="pattern", merge=True)
    print(f"merchant_map: {n} rows sent")


def backfill_cards() -> None:
    rows = []
    for r in _records("Cards"):
        card_id = str(r.get("card_id", "")).strip()
        if not card_id:
            continue
        rows.append({
            "card_id": card_id,
            "display_name": str(r.get("display_name", "")).strip(),
            "payment_method_pattern": str(r.get("payment_method_pattern", "")).strip(),
            "cycle_start_day": max(1, min(31, int(float(r.get("cycle_start_day") or 1)))),
            "min_spend_bonus": float(r.get("min_spend_bonus") or 0),
            "notes": str(r.get("notes", "")),
        })
    rows = _dedupe(rows, lambda r: r["card_id"])
    n = _post("cards", rows, on_conflict="card_id", merge=True)
    print(f"cards: {n} rows sent")


def backfill_card_strategy() -> None:
    rows = []
    for r in _records("CardStrategy"):
        category = str(r.get("category", "")).strip()
        if not category:
            continue
        promo = str(r.get("promo_active_until", "")).strip()
        rows.append({
            "category": category,
            "primary_card_id": str(r.get("primary_card_id", "")).strip(),
            "primary_cap": float(r.get("primary_cap") or 0),
            "primary_earn_rate": float(r.get("primary_earn_rate") or 0),
            "fallback_card_id": str(r.get("fallback_card_id", "")).strip(),
            "fallback_earn_rate": float(r.get("fallback_earn_rate") or 0),
            "promo_active_until": promo or None,
            "notes": str(r.get("notes", "")),
        })
    rows = _dedupe(rows, lambda r: r["category"])
    n = _post("card_strategy", rows, on_conflict="category", merge=True)
    print(f"card_strategy: {n} rows sent")


def backfill_travel_mode() -> None:
    existing = _existing("travel_mode", "label")
    rows = []
    for r in _records("TravelMode"):
        label = str(r.get("label", "")).strip()
        if not label or label in existing:
            continue
        rows.append({
            "start_date": str(r.get("start_date", "")).strip(),
            "end_date": str(r.get("end_date", "")).strip(),
            "label": label,
            "trip_category": str(r.get("trip_category", "")).strip(),
            "budget_map": str(r.get("budget_map", "")),
            "total_budget": float(r.get("total_budget") or 0),
            "notes": str(r.get("notes", "")),
        })
    n = _post("travel_mode", rows)
    print(f"travel_mode: {n} new trips sent")


def backfill_nudge_logs() -> None:
    rows = []
    for r in _records("CardNudgeLog"):
        if not str(r.get("cycle_window", "")).strip():
            continue
        rows.append({
            "ts": str(r.get("timestamp", "")).strip() or None,
            "cycle_window": str(r.get("cycle_window", "")).strip(),
            "card_id": str(r.get("card_id", "")).strip(),
            "category": str(r.get("category", "")).strip(),
            "threshold": int(float(r.get("threshold") or 0)),
            "triggering_txn_id": str(r.get("triggering_txn_id", "")).strip(),
            "fallback_card_id": str(r.get("fallback_card_id", "")).strip(),
        })
    rows = [{k: v for k, v in row.items() if v is not None} for row in rows]
    rows = _dedupe(rows, lambda r: (r["cycle_window"], r["card_id"], r["category"], r["threshold"]))
    n = _post("card_nudge_log", rows,
              on_conflict="cycle_window,card_id,category,threshold")
    print(f"card_nudge_log: {n} rows sent")

    rows = []
    for r in _records("TripNudgeLog"):
        if not str(r.get("trip_label", "")).strip():
            continue
        rows.append({
            "ts": str(r.get("timestamp", "")).strip() or None,
            "trip_label": str(r.get("trip_label", "")).strip(),
            "bucket": str(r.get("bucket", "")).strip(),
            "threshold": int(float(r.get("threshold") or 0)),
            "budget_at_nudge": float(r.get("budget_at_nudge") or 0),
            "triggering_txn_id": str(r.get("triggering_txn_id", "")).strip(),
        })
    rows = [{k: v for k, v in row.items() if v is not None} for row in rows]
    rows = _dedupe(rows, lambda r: (r["trip_label"], r["bucket"], r["threshold"], r["budget_at_nudge"]))
    n = _post("trip_nudge_log", rows,
              on_conflict="trip_label,bucket,threshold,budget_at_nudge")
    print(f"trip_nudge_log: {n} rows sent")


def backfill_webhook_log(days_back: int = 30) -> None:
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    existing = _existing("webhook_log", "idempotency_key")
    rows = []
    for r in _records("WebhookLog"):
        key = str(r.get("idempotency_key", "")).strip()
        row_date = str(r.get("date", "")).strip()
        if not key or key in existing or row_date < cutoff:
            continue
        try:
            amount = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            amount = None
        row = {
            "bank": str(r.get("bank", "")).strip(),
            "type": str(r.get("type", "")).strip(),
            "amount": amount,
            "currency": str(r.get("currency", "")).strip(),
            "merchant": str(r.get("merchant", "")).strip(),
            "txn_date": row_date,
            "payment_method": str(r.get("payment_method", "")).strip(),
            "idempotency_key": key,
            "webhook_status": str(r.get("webhook_status", "")).strip(),
            "matched": str(r.get("matched", "")).strip(),
        }
        ts = str(r.get("timestamp", "")).strip()
        if ts:
            row["ts"] = ts
        rows.append(row)
    n = _post("webhook_log", rows)
    print(f"webhook_log: {n} rows sent (last {days_back} days)")


def backfill_append_only(tab: str, table: str, mapper) -> None:
    """insights / journal: only fill an empty table."""
    if _count(table):
        print(f"{table}: table not empty — skipped (append-only, no natural key)")
        return
    rows = [mapper(r) for r in _records(tab)]
    # Drop empty mappings and None values (absent key → column default;
    # explicit null would violate NOT NULL).
    rows = [{k: v for k, v in r.items() if v is not None} for r in rows if r]
    n = _post(table, rows)
    print(f"{table}: {n} rows sent")


def main() -> None:
    if not supabase_client._configured():
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — aborting")
        sys.exit(1)

    backfill_transactions()
    backfill_budgets()
    backfill_merchant_map()
    backfill_cards()
    backfill_card_strategy()
    backfill_travel_mode()
    backfill_nudge_logs()
    backfill_webhook_log()
    backfill_append_only(
        "Insights", "insights",
        lambda r: {
            "date": str(r.get("date", "")).strip() or None,
            "category": str(r.get("category", "")),
            "month": str(r.get("month", "")),
            "insight": str(r.get("insight", "")),
        } if str(r.get("insight", "")).strip() else None,
    )
    backfill_append_only(
        "Journal", "journal",
        lambda r: {
            "date": str(r.get("date", "")).strip() or None,
            "reply_text": str(r.get("reply_text", "")),
            "txn_ids_referenced": str(r.get("txn_ids_referenced", "")),
            "tags": str(r.get("tags", "")),
        } if str(r.get("reply_text", "")).strip() else None,
    )
    print("backfill complete — spot-check row counts in the Supabase Table Editor")


if __name__ == "__main__":
    main()

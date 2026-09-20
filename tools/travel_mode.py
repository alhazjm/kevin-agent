"""Travel-mode routing and per-bucket trip-budget tracking.

Ported in migration 3b, reads are live: all data access goes through the
`supabase_client` data layer (Postgres tables `travel_mode`,
`trip_nudge_log`, `transactions`) instead of the Google Sheet tabs.

Architecture (see PR description for full design):

- The budget layer gets one category row per trip, e.g.
  `Travel - ID 2026-04 = $2000`. All trip transactions land there so the
  existing monthly-budget machinery (warnings, summaries, charts) keeps
  working unchanged.

- The `travel_mode` table carries one row per trip with the date range, the
  trip's main budget category, and the per-bucket allocation
  (`food=450; transport=300; flight=800; misc=450`). The user maintains this
  by hand — there's no `set_travel_mode` write tool in v1.

- Each trip transaction's bucket is stamped into the `Notes` column as
  `[bucket:X]` (e.g. `[bucket:food] warung kopi`). This lets us compute
  per-bucket spend without adding a column to the transactions ledger.

- Routing rule (DETERMINISTIC, decided by `route_for_trip` inside
  `log_expense` — the model no longer routes; 2026-08-17): when a
  travel_mode row covers the txn date, a spend is routed into the trip's
  budget category on one of two signals — payment_method mentions YouTrip
  (Shortcut taps), or notes carry an `orig:` FX trace (bank card used
  abroad, or a manual foreign amount the tool converted first). The
  bucket is derived from the model's PROPOSED home category via a keyword
  map and stamped as `[bucket:X]`. MerchantMap decides the BUCKET, never
  the category — the old "a learned mapping beats travel mode" rule sent
  Gojek-in-KL to the home Personal - Travel budget (912%). SGD spends
  with no signal (Shopee for home, PayLah to a friend) are NOT routed —
  currency/pot is the guard against "everything during the trip is
  travel", not the date. YouTrip top-ups and rows already in the trip
  category are never routed.

- Auto-nudge: after a trip txn is logged, `maybe_send_trip_bucket_nudge`
  checks whether it crossed an 80%/100% bucket threshold and fires a
  Telegram bubble. Dedup via the `trip_nudge_log` table, keyed on
  (trip_label, bucket, threshold, budget_at_nudge) so mid-trip budget
  reallocations let fresh nudges fire when the new budget is crossed.

- Trip funding (approved 2026-08-01): YouTrip top-ups get a
  `[trip:<label>]` Notes tag (`link_topup_to_trip`) linking them to a
  travel_mode row; the trip's funded pot is DERIVED as the sum of tagged
  top-ups. `total_budget` stays the PLANNED number — never mutated by
  funding. `create_trip` inserts new trip rows (the skill previews and
  confirms first).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from tools import supabase_client
from tools.sheets_client import _is_pending

DEFAULT_BUCKET = "misc"

# `[bucket:foo]` anywhere in the Notes string. Anchored at start of group so
# we can pull just the bucket name. Matches the writer in `_apply_bucket_tag`.
_BUCKET_TAG_PATTERN = re.compile(r"\[bucket:([^\]]+)\]", re.IGNORECASE)

# `[trip:<label>]` anywhere in the Notes string — links a YouTrip top-up to
# a travel_mode row so the trip's funded pot can be DERIVED as the sum of
# tagged top-ups (total_budget stays the PLANNED number, never mutated).
# Labels are stored verbatim (may contain spaces/case); matching against
# travel_mode labels is case-insensitive at lookup time. Writer:
# `_apply_trip_tag` — reader and writer change together (M13).
_TRIP_TAG_PATTERN = re.compile(r"\[trip:([^\]]+)\]", re.IGNORECASE)


# --- Value helpers ----------------------------------------------------------


def _as_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_kv_map(raw: str) -> dict[str, str]:
    """Parse a `key=value; key=value` string into a dict. Whitespace around
    keys/values is stripped; case-preserved. Empty input returns {}."""
    out: dict[str, str] = {}
    if not raw:
        return out
    for pair in str(raw).split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _parse_budget_map(raw: str) -> dict[str, float]:
    """Same shape as `_parse_kv_map` but coerces values to floats. Bad
    values are skipped silently — schema validation lives at the user-input
    layer (the user types this into a sheet cell, so 'best effort' parse)."""
    out: dict[str, float] = {}
    for k, v in _parse_kv_map(raw).items():
        try:
            out[k.lower()] = float(v)
        except (ValueError, TypeError):
            continue
    return out


# --- Travel mode lookup -----------------------------------------------------


def read_travel_mode_rows() -> list[dict]:
    """Read all rows from the `travel_mode` table. Returns `[]` when the
    table is empty (the table always exists in Postgres — the old
    missing-tab case collapses into "no rows")."""
    records = supabase_client.read_travel_mode_records()
    rows = []
    for r in records:
        label = str(r.get("label", "")).strip()
        start = _parse_date(r.get("start_date"))
        end = _parse_date(r.get("end_date"))
        if not label or start is None or end is None:
            continue
        rows.append({
            "start_date": start,
            "end_date": end,
            "label": label,
            "trip_category": str(r.get("trip_category", "")).strip(),
            "budget_map_raw": str(r.get("budget_map", "")),
            "budget_map": _parse_budget_map(str(r.get("budget_map", ""))),
            "total_budget": _as_float(r.get("total_budget")),
            "notes": str(r.get("notes", "")),
        })
    return rows


def _find_active_trip(rows: list[dict], as_of: date) -> dict | None:
    """Return the trip row whose date range contains `as_of`. If multiple
    rows match (overlapping/back-to-back trips), the one ADDED MOST
    RECENTLY wins — we treat the bottom-most matching row as authoritative
    since users typically append new trips."""
    matches = [r for r in rows if r["start_date"] <= as_of <= r["end_date"]]
    if not matches:
        return None
    # `read_travel_mode_rows` preserves sheet order, so the last match is
    # the most recently appended row.
    return matches[-1]


def get_active_travel_mode(as_of: str | None = None) -> dict:
    """Tool-facing: return the active trip row for today's date (or `as_of`),
    plus a summary of buckets and overlap warnings. Empty dict-with-`active=False`
    if no trip is active."""
    rows = read_travel_mode_rows()
    as_of_date = _parse_date(as_of) or date.today()

    # Surface overlap warning if 2+ rows match — the trip we actually use
    # is the most-recently-added, but the user should know.
    matches = [r for r in rows if r["start_date"] <= as_of_date <= r["end_date"]]
    overlap_warning = None
    if len(matches) > 1:
        overlap_warning = (
            "Multiple TravelMode rows match today's date "
            + f"({', '.join(r['label'] for r in matches)}). Using the most "
            + "recently-added row. Consider pruning."
        )

    active = _find_active_trip(rows, as_of_date)
    if active is None:
        return {
            "active": False,
            "as_of": as_of_date.isoformat(),
            "all_rows_count": len(rows),
        }

    return {
        "active": True,
        "as_of": as_of_date.isoformat(),
        "label": active["label"],
        "start_date": active["start_date"].isoformat(),
        "end_date": active["end_date"].isoformat(),
        "trip_category": active["trip_category"],
        "budget_map": active["budget_map"],
        "total_budget": active["total_budget"],
        "notes": active["notes"],
        "overlap_warning": overlap_warning,
    }


# --- Notes parsing / writing ------------------------------------------------


def _parse_bucket_from_notes(notes: str) -> str | None:
    """Return the bucket name from `[bucket:X]` in notes, or None."""
    if not notes:
        return None
    m = _BUCKET_TAG_PATTERN.search(notes)
    if m:
        return m.group(1).strip().lower() or None
    return None


def _apply_bucket_tag(notes: str, bucket: str) -> str:
    """Set/replace the `[bucket:X]` prefix in notes, leaving freeform
    content otherwise intact. Always normalises bucket to lowercase. The
    prefix lands at the START of notes so it's easy to spot when scrolling
    the sheet."""
    bucket = (bucket or "").strip().lower()
    if not bucket:
        return notes
    cleaned = _BUCKET_TAG_PATTERN.sub("", notes or "").strip()
    if cleaned:
        return f"[bucket:{bucket}] {cleaned}"
    return f"[bucket:{bucket}]"


def _parse_trip_from_notes(notes: str) -> str | None:
    """Return the trip label from `[trip:<label>]` in notes, or None.
    Label comes back verbatim (whitespace-trimmed) — matching against
    travel_mode labels is the caller's job and is case-insensitive."""
    if not notes:
        return None
    m = _TRIP_TAG_PATTERN.search(notes)
    if m:
        return m.group(1).strip() or None
    return None


def _apply_trip_tag(notes: str, label: str) -> str:
    """Set/replace the `[trip:<label>]` tag in notes — the ONLY writer for
    this encoding (mirror of `_apply_bucket_tag`'s discipline; never edit
    trip tags via free-form Notes writes).

    Unlike the bucket writer, an existing tag is replaced IN PLACE (its
    position is preserved) so surrounding `[bucket:x]` tags and `orig:` FX
    traces are never reordered; with no existing tag, the label is
    prepended before the current content. The label is stored verbatim —
    it may contain spaces and mixed case."""
    label = (label or "").strip()
    if not label:
        return notes
    notes = notes or ""
    if _TRIP_TAG_PATTERN.search(notes):
        # Lambda replacement: labels are user text — keep re.sub from
        # interpreting backslashes/group refs in them.
        return _TRIP_TAG_PATTERN.sub(lambda _m: f"[trip:{label}]", notes,
                                     count=1)
    if notes:
        return f"[trip:{label}] {notes}"
    return f"[trip:{label}]"


# --- Deterministic trip routing (called from handle_log_expense) ------------

# The transfer category is never routed into a trip: a YouTrip top-up is
# the counted outflow (it gets a `[trip:<label>]` tag via
# `link_topup_to_trip` instead). Matched case-insensitively on the
# canonical name.
TRANSFER_CATEGORY = "YouTrip Top-up"

# Keyword map from the model's PROPOSED home category to a trip bucket.
# Order matters: the first group with a hit wins ("Personal - Travel" →
# transport, "Personal - Food & Drinks" → food). Anything unmatched falls
# to DEFAULT_BUCKET ("misc"). Keep in sync with the bucket names users
# put in `travel_mode.budget_map`.
_BUCKET_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("food", ("food", "drink", "dining", "grocer")),
    ("transport", ("travel", "transport", "taxi", "grab", "bus")),
    ("shopping", ("cloth", "shop", "misc", "beauty", "gift")),
    ("health", ("health", "medical", "dental", "pharm")),
    ("lodging", ("hotel", "lodg", "accom", "stay")),
]


def _bucket_for_category(proposed: str) -> str:
    """Derive a trip bucket from a proposed HOME category name (keyword
    match, case-insensitive). Unknown → DEFAULT_BUCKET."""
    name = (proposed or "").strip().lower()
    if not name:
        return DEFAULT_BUCKET
    for bucket, keywords in _BUCKET_KEYWORDS:
        if any(k in name for k in keywords):
            return bucket
    return DEFAULT_BUCKET


def route_for_trip(txn_date: str, proposed_category: str,
                   payment_method: str = "", notes: str = "",
                   currency: str = "SGD") -> dict:
    """Decide (deterministically) whether a spend belongs to the active
    trip and, if so, what category + notes it should land with.

    Called by `handle_log_expense` BEFORE `append_transaction`, with the
    already-FX-converted values (a manual foreign amount is converted to
    SGD in the tool first, which stamps `orig:` and thereby raises the
    signal). Pure logic over `get_active_travel_mode(as_of=txn_date)`;
    NEVER raises — every path returns a dict:

      {"applied": bool, "reason": str, "category": <final>,
       "notes": <final, [bucket:X] via _apply_bucket_tag>,
       "trip_label": str, "bucket": str | None,
       "signal": "youtrip" | "orig" | None, "from_category": str}

    Signals (either is sufficient once a trip covers `txn_date`):
      - "youtrip": payment_method mentions YouTrip (Shortcut taps; the
        spend is pot-internal — excluded from monthly totals as before).
      - "orig": notes carry an `orig:` FX trace (bank card abroad, or a
        tool-converted manual amount). A non-SGD `currency` that somehow
        reached here unconverted counts as the same signal.
    Guards (never routed): no active trip; proposed category is the
    transfer category (top-ups get `[trip:]` tags instead); proposed
    already equals the trip category ("already_trip"); no signal (SGD,
    no orig, not YouTrip — a Shopee order for home mid-trip stays home).

    Bucket = an existing `[bucket:X]` in notes (respected, notes left
    untouched) else `_bucket_for_category(proposed_category)` — so a
    MerchantMap hit now decides the BUCKET, never the category."""
    proposed = (proposed_category or "").strip()
    base = {
        "applied": False,
        "reason": "",
        "category": proposed,
        "notes": notes or "",
        "trip_label": "",
        "bucket": None,
        "signal": None,
        "from_category": proposed,
    }
    try:
        trip = get_active_travel_mode(as_of=txn_date)
        if not trip.get("active"):
            return dict(base, reason="no_active_trip")
        trip_category = str(trip.get("trip_category", "")).strip()
        label = str(trip.get("label", ""))
        base["trip_label"] = label
        if not trip_category:
            return dict(base, reason="trip_has_no_category")
        if proposed.lower() == TRANSFER_CATEGORY.lower():
            return dict(base, reason="topup_guard")
        if proposed.lower() == trip_category.lower():
            return dict(base, reason="already_trip")

        pm = (payment_method or "").lower()
        notes_l = (notes or "").lower()
        cur = (currency or "SGD").strip().upper()
        if "youtrip" in pm:
            signal = "youtrip"
        elif "orig:" in notes_l or (cur and cur != "SGD"):
            signal = "orig"
        else:
            return dict(base, reason="no_signal")

        # Fixed monthly bills are never trip spend, even when billed in a
        # foreign currency mid-trip (Anthropic/ChatGPT/iCloud arrive in USD
        # with an orig: trace on the DBS card): the proposed category's
        # `kind` from category_meta (0008) is the opt-out. Adversarial
        # review catch, 2026-08-17 — without it a correction could never
        # stick, because the router re-routed the same merchant next trip.
        try:
            kinds = supabase_client.read_category_kinds()
        except Exception:
            kinds = {}
        if kinds.get(supabase_client._normalize_category(proposed)) == "fixed":
            return dict(base, reason="fixed_bill")

        # The trip category must be a real budgets row, or every routed
        # spend would be refused with unknown_category and NOTHING logged
        # (the pending UNCATEGORIZED fallback is bypassed by routing). Fall
        # back to the home category rather than lose the transaction;
        # create_trip ensures the row exists for new trips.
        try:
            resolved = supabase_client.resolve_category(trip_category)
        except Exception:
            resolved = {"match": None}
        if not resolved.get("match"):
            return dict(base, reason="trip_category_unknown")
        trip_category = resolved["match"]

        existing = _parse_bucket_from_notes(notes or "")
        if existing:
            bucket = existing
            new_notes = notes or ""
        else:
            bucket = _bucket_for_category(proposed)
            new_notes = _apply_bucket_tag(notes or "", bucket)

        return dict(
            base,
            applied=True,
            reason="routed",
            category=trip_category,
            notes=new_notes,
            bucket=bucket,
            signal=signal,
        )
    except Exception as exc:
        return dict(base, reason="exception", error=str(exc))


# --- Trip-budget status -----------------------------------------------------


def _trip_transactions(trip: dict) -> list[dict]:
    """Read all transactions matching the trip: same trip_category and date
    in the trip's window. Excludes pending and backfill rows.

    Reads ALL records (not just current month) because trip date windows
    can straddle month boundaries (e.g. 27 Apr–3 May)."""
    records = supabase_client.read_all_transaction_rows()
    start = trip["start_date"]
    end = trip["end_date"]
    trip_cat = trip["trip_category"]
    out = []
    for r in records:
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        cat = str(r.get("Category", "")).strip()
        if trip_cat and cat != trip_cat:
            continue
        d = _parse_date(r.get("Date"))
        if d is None or not (start <= d <= end):
            continue
        out.append(r)
    return out


def get_trip_budget_status(trip_label: str | None = None,
                           as_of: str | None = None) -> dict:
    """Return per-bucket spent/budget for a trip. Defaults to the active
    trip. Looks up by `trip_label` if provided (so the user can ask about
    last month's trip).

    Status vocabulary is unchanged from the Sheet era — the skill prompts
    key on it. `no_travel_mode_tab` can no longer mean a literally missing
    tab (Postgres tables always exist); it now means the `travel_mode`
    table has no usable rows, which produces the same user-visible outcome."""
    rows = read_travel_mode_rows()
    if not rows:
        return {
            "status": "no_travel_mode_tab",
            "message": (
                "No trips defined — the `travel_mode` table is empty. Add a "
                "trip row before asking for trip status — see "
                "`supabase/migrations/0001_init.sql`."
            ),
        }

    as_of_date = _parse_date(as_of) or date.today()

    if trip_label:
        match = next(
            (r for r in rows if r["label"].lower() == trip_label.lower()),
            None,
        )
        if match is None:
            return {
                "status": "not_found",
                "message": f"No TravelMode row with label '{trip_label}'.",
                "available_labels": [r["label"] for r in rows],
            }
        trip = match
    else:
        trip = _find_active_trip(rows, as_of_date)
        if trip is None:
            return {
                "status": "no_active_trip",
                "as_of": as_of_date.isoformat(),
                "message": (
                    "No active trip for today. Pass `trip_label` to query a "
                    "specific past trip."
                ),
            }

    txns = _trip_transactions(trip)
    spend_by_bucket: dict[str, float] = defaultdict(float)
    untagged_total = 0.0
    untagged_txns: list[str] = []
    for r in txns:
        amount = _as_float(r.get("Amount"))
        bucket = _parse_bucket_from_notes(str(r.get("Notes", "")))
        if bucket is None:
            untagged_total += amount
            txn_id = str(r.get("txn_id", ""))
            if txn_id:
                untagged_txns.append(txn_id)
        else:
            spend_by_bucket[bucket] += amount

    buckets = []
    budget_map = trip["budget_map"]
    # Iterate over the union of (configured buckets, observed buckets) so
    # both unallocated spend and unfilled allocations are surfaced.
    for name in sorted(set(budget_map) | set(spend_by_bucket)):
        budget = budget_map.get(name, 0.0)
        spent = round(spend_by_bucket.get(name, 0.0), 2)
        pct = round(spent / budget * 100, 1) if budget > 0 else None
        if budget <= 0:
            status = "no_budget"
        elif pct < 80:
            status = "ok"
        elif pct < 100:
            status = "warning"
        else:
            status = "capped"
        buckets.append({
            "bucket": name,
            "budget": budget,
            "spent": spent,
            "remaining": round(budget - spent, 2) if budget > 0 else None,
            "percent_used": pct,
            "status": status,
        })

    total_spent = round(sum(_as_float(r.get("Amount")) for r in txns), 2)
    total_budget = trip["total_budget"]
    total_pct = round(total_spent / total_budget * 100, 1) if total_budget > 0 else None

    return {
        "status": "ok",
        "label": trip["label"],
        "trip_category": trip["trip_category"],
        "start_date": trip["start_date"].isoformat(),
        "end_date": trip["end_date"].isoformat(),
        "as_of": as_of_date.isoformat(),
        "total_budget": total_budget,
        "total_spent": total_spent,
        "total_percent_used": total_pct,
        "buckets": buckets,
        "untagged_spend": round(untagged_total, 2),
        "untagged_txn_ids": untagged_txns,
        "transaction_count": len(txns),
    }


# --- Bucket tag editing -----------------------------------------------------


def set_trip_bucket(txn_id: str, bucket: str) -> dict:
    """Update or insert the `[bucket:X]` prefix in a transaction's Notes.
    Used when the user replies to a trip bubble correcting the bucket
    (e.g. 'that was activities, not food').

    Leaves the Category column alone — only the Notes prefix changes.
    Returns {status, txn_id, bucket, notes_after} on success."""
    if not txn_id:
        return {"status": "error", "message": "txn_id is required"}
    if not bucket or not bucket.strip():
        return {"status": "error", "message": "bucket is required"}

    found = supabase_client.find_transaction_by_id(txn_id)
    if found is None:
        return {
            "status": "error",
            "message": f"Transaction not found: txn_id={txn_id}",
        }
    row_num, existing = found

    new_notes = _apply_bucket_tag(str(existing.get("Notes", "")), bucket.strip())
    updates = {"Notes": new_notes}

    # Reuse supabase_client.edit_transaction so the ledger mutation goes
    # through the shared data layer (same header-keyed updates dict the
    # Sheet path used) — no raw writes from this module.
    result = supabase_client.edit_transaction(
        merchant="", amount=0.0, txn_id=txn_id, updates=updates
    )
    if result.get("status") != "ok":
        return result
    return {
        "status": "ok",
        "txn_id": txn_id,
        "bucket": bucket.strip().lower(),
        "notes_before": str(existing.get("Notes", "")),
        "notes_after": new_notes,
        "row": row_num,
    }


# --- Trip creation & top-up linkage -----------------------------------------
# YouTrip top-ups fund trips: a `[trip:<label>]` Notes tag links a top-up
# txn to a travel_mode row, and the funded pot is DERIVED as the sum of
# tagged top-ups. `total_budget` stays the PLANNED envelope — nothing here
# ever mutates it (approved design, 2026-08-01).


def create_trip(label: str, start_date: str, end_date: str,
                trip_category: str, total_budget: float = 0.0,
                budget_map: str = "", notes: str = "") -> dict:
    """Insert a new travel_mode trip row. The skill previews the trip and
    confirms with the user BEFORE calling (money-state mutation).

    Validates dates (YYYY-MM-DD, start <= end) and refuses a duplicate
    label (case-insensitive — labels are the trip's lookup key everywhere)
    with `status="exists"`, writing nothing. `total_budget` is the PLANNED
    number; funding is derived from `[trip:<label>]`-tagged top-ups."""
    label = (label or "").strip()
    if not label:
        return {"status": "error", "message": "label is required"}
    trip_category = (trip_category or "").strip()
    if not trip_category:
        return {"status": "error", "message": "trip_category is required"}

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start is None or end is None:
        return {
            "status": "error",
            "message": "start_date and end_date must be YYYY-MM-DD dates",
        }
    if start > end:
        return {
            "status": "error",
            "message": f"start_date {start.isoformat()} is after "
                       f"end_date {end.isoformat()}",
        }

    existing = read_travel_mode_rows()
    match = next(
        (r for r in existing if r["label"].lower() == label.lower()), None)
    if match is not None:
        return {
            "status": "exists",
            "message": (
                f"A trip labelled '{match['label']}' already exists "
                f"({match['start_date'].isoformat()} to "
                f"{match['end_date'].isoformat()}). Nothing was written."
            ),
        }

    supabase_client.insert_travel_mode_row(
        label=label,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        trip_category=trip_category,
        budget_map=str(budget_map or ""),
        total_budget=_as_float(total_budget),
        notes=str(notes or ""),
    )
    # One budgets row per trip is the design ("so existing budget
    # machinery works unchanged") — and route_for_trip refuses to route
    # into a category that doesn't resolve. Ensure it at $0 here, exactly
    # like the reserved Lending row; the user sets a limit conversationally.
    try:
        supabase_client.ensure_category_exists(trip_category)
    except Exception:
        pass   # a later log_expense simply won't route until the row exists
    return {
        "status": "created",
        "trip": {
            "label": label,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "trip_category": trip_category,
            "total_budget": _as_float(total_budget),
            "budget_map": str(budget_map or ""),
            "notes": str(notes or ""),
        },
    }


def link_topup_to_trip(txn_id: str, trip_label: str) -> dict:
    """Stamp `[trip:<label>]` into a top-up transaction's Notes, linking it
    to a travel_mode row. The funded pot is derived by summing tagged
    top-ups — this never touches the trip's total_budget.

    Label matching is case-insensitive; the CANONICAL label from the
    travel_mode row is what gets stamped (and returned), so derived sums
    never fracture on casing. An existing `[trip:...]` tag is replaced;
    `[bucket:x]` tags and `orig:` FX traces are left untouched. The write
    goes through supabase_client.edit_transaction — no raw writes."""
    if not txn_id:
        return {"status": "error", "message": "txn_id is required"}
    if not trip_label or not trip_label.strip():
        return {"status": "error", "message": "trip_label is required"}

    rows = read_travel_mode_rows()
    trip = next(
        (r for r in rows if r["label"].lower() == trip_label.strip().lower()),
        None,
    )
    if trip is None:
        return {
            "status": "not_found",
            "message": f"No trip with label '{trip_label}'.",
            "available_labels": [r["label"] for r in rows],
        }

    found = supabase_client.find_transaction_by_id(txn_id)
    if found is None:
        return {
            "status": "not_found",
            "message": f"Transaction not found: txn_id={txn_id}",
        }
    row_num, existing = found

    notes_before = str(existing.get("Notes", ""))
    new_notes = _apply_trip_tag(notes_before, trip["label"])

    result = supabase_client.edit_transaction(
        merchant="", amount=0.0, txn_id=txn_id, updates={"Notes": new_notes}
    )
    if result.get("status") != "ok":
        return result
    return {
        "status": "updated",
        "txn_id": txn_id,
        "trip_label": trip["label"],
        "trip_category": trip["trip_category"],
        "notes_before": notes_before,
        "notes_after": new_notes,
        "row": row_num,
    }


# --- trip_nudge_log ---------------------------------------------------------
# The table is created by the Supabase migration, so the old
# ensure-the-tab-exists helper is gone — reads on an empty table just
# return [].


def _read_nudge_log() -> list[dict]:
    return supabase_client.read_trip_nudge_log()


def _already_nudged(trip_label: str, bucket: str, threshold: int,
                    budget_at_nudge: float) -> bool:
    """Dedup key includes `budget_at_nudge` so a mid-trip budget revision
    invalidates the prior nudge — the next time the (revised) threshold is
    crossed, a fresh nudge fires."""
    for r in _read_nudge_log():
        try:
            row_budget = float(r.get("budget_at_nudge", 0) or 0)
        except (ValueError, TypeError):
            continue
        if (
            str(r.get("trip_label", "")).strip() == trip_label
            and str(r.get("bucket", "")).strip().lower() == bucket.lower()
            and str(r.get("threshold", "")).strip() == str(threshold)
            and abs(row_budget - budget_at_nudge) < 0.005
        ):
            return True
    return False


def _record_nudge(trip_label: str, bucket: str, threshold: int,
                  budget_at_nudge: float, triggering_txn_id: str) -> None:
    # The DB stamps the timestamp; append_trip_nudge also enforces the
    # dedup tuple at insert time (returns False on conflict — harmless
    # here because _already_nudged already gated the send).
    supabase_client.append_trip_nudge(
        trip_label,
        bucket,
        threshold,
        budget_at_nudge,
        triggering_txn_id or "",
    )


# --- Auto-nudge hook (called from handle_log_expense) -----------------------


def maybe_send_trip_bucket_nudge(category: str, amount: float,
                                 txn_date: str, txn_id: str = "",
                                 notes: str = "") -> dict:
    """Fire a Telegram nudge when a trip txn pushes a bucket past 80% or
    100% of its allocation. At most one nudge per
    (trip_label, bucket, threshold, budget_at_nudge) — a budget revision
    invalidates the dedup so the next crossing fires fresh. Dedup state
    lives in the `trip_nudge_log` table; the `no_travel_mode_tab` reason
    string is kept for the empty-table case (the table itself always
    exists in Postgres).

    MUST NOT raise — wrapped in try/except, returns
    `{sent: bool, reason: str}` on every path."""
    try:
        rows = read_travel_mode_rows()
        if not rows:
            return {"sent": False, "reason": "no_travel_mode_tab"}

        as_of_date = _parse_date(txn_date) or date.today()
        trip = _find_active_trip(rows, as_of_date)
        if trip is None:
            return {"sent": False, "reason": "no_active_trip"}
        if (trip["trip_category"] or "").strip() != (category or "").strip():
            return {"sent": False, "reason": "category_mismatch"}

        bucket = _parse_bucket_from_notes(notes)
        if bucket is None:
            return {"sent": False, "reason": "untagged"}

        budget = trip["budget_map"].get(bucket, 0.0)
        if budget <= 0:
            return {"sent": False, "reason": "no_bucket_budget"}

        # Compute spent BEFORE this txn by re-reading the trip txns and
        # subtracting the current txn (find by txn_id if known, else by
        # amount fallback). We re-read instead of trusting just `amount`
        # so concurrent writes don't desync the threshold check.
        txns = _trip_transactions(trip)
        spent_after = 0.0
        for r in txns:
            r_bucket = _parse_bucket_from_notes(str(r.get("Notes", "")))
            if r_bucket == bucket:
                spent_after += _as_float(r.get("Amount"))
        spent_before = max(0.0, spent_after - amount)
        pct_before = spent_before / budget
        pct_after = spent_after / budget

        threshold = None
        if pct_before < 1.00 <= pct_after:
            threshold = 100
        elif pct_before < 0.80 <= pct_after:
            threshold = 80

        if threshold is None:
            return {"sent": False, "reason": "no_threshold_crossed"}

        if _already_nudged(trip["label"], bucket, threshold, budget):
            return {"sent": False, "reason": "already_nudged"}

        verb = "just hit" if threshold == 100 else f"is at {int(pct_after * 100)}% of"
        text = (
            f"🧳 {trip['label']} — {bucket} {verb} its SGD {budget:.0f} "
            f"allocation. Spent SGD {spent_after:.2f} so far."
        )
        if threshold == 80:
            remaining = budget - spent_after
            text += f" SGD {remaining:.2f} left in this bucket."

        from tools.expense_sheets_tool import _send_telegram_bubble

        send_result = _send_telegram_bubble(text)
        if not send_result.get("ok"):
            return {
                "sent": False,
                "reason": "telegram_send_failed",
                "error": send_result.get("error", ""),
            }

        _record_nudge(trip["label"], bucket, threshold, budget, txn_id)
        return {
            "sent": True,
            "trip_label": trip["label"],
            "bucket": bucket,
            "threshold": threshold,
            "budget": budget,
            "spent_after": round(spent_after, 2),
            "telegram_message_id": send_result.get("message_id", ""),
        }
    except Exception as exc:
        return {"sent": False, "reason": "exception", "error": str(exc)}

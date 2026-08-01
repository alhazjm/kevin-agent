"""Card optimiser — category-keyed earn-rate strategy + event-driven nudges.

See `docs/CARD-OPTIMISER-ARCHITECTURE.md` for the full design. Everything in
this module is gated behind `_check_setup()` — tools return
`{"status": "setup_required", ...}` until the user populates the `cards` and
`card_strategy` Supabase tables (with at least one row each and a `_default`
sentinel row in `card_strategy`).

Ported to Supabase in migration 3b — reads are live (no longer the
nightly-export copy).

The one cross-skill touchpoint is `maybe_send_post_cap_nudge`, called from
`handle_log_expense` after a transaction is logged. It MUST NOT raise — the
caller wraps it in try/except as a defensive second layer, but this module
should swallow its own errors and return a reason string.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

from tools import supabase_client
from tools.supabase_client import _is_pending, _is_pot_internal

DEFAULT_STRATEGY_KEY = "_default"

_STRATEGY_HEADERS = [
    "category",
    "primary_card_id",
    "primary_cap",
    "primary_earn_rate",
    "fallback_card_id",
    "fallback_earn_rate",
    "promo_active_until",
    "notes",
]


# --- Data-layer helpers -----------------------------------------------------


def _setup_error(message: str) -> dict:
    return {"status": "setup_required", "message": message}


def _as_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _as_int(val, default: int = 0) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def read_cards() -> list[dict]:
    """Read the `cards` table. Returns `[]` when it has no rows."""
    records = supabase_client.read_cards_records()
    cards = []
    for r in records:
        card_id = str(r.get("card_id", "")).strip()
        if not card_id:
            continue
        cycle_day = _as_int(r.get("cycle_start_day"), default=1)
        cards.append({
            "card_id": card_id,
            "display_name": str(r.get("display_name", "")).strip() or card_id,
            "payment_method_pattern": str(r.get("payment_method_pattern", "")).strip(),
            "cycle_start_day": max(1, min(31, cycle_day)),
            "min_spend_bonus": _as_float(r.get("min_spend_bonus")),
            # bonus_cap was added to the live table by hand around PR #40
            # (0003 migration makes it reproducible) — calendar-month bonus
            # spend cap in S$; 0 = none/uncapped
            "bonus_cap": _as_float(r.get("bonus_cap")),
            # base_mpd (0007): the card's flat everything-else earn rate.
            # review_card_efficiency uses it for cards outside a category's
            # strategy row — Vantage's uncapped 1.5 was previously scored
            # as 0, which alone overstated Jul 2026's "miles left on the
            # table". Absent column → 0.0, the exact pre-0007 behavior.
            "base_mpd": _as_float(r.get("base_mpd")),
            "notes": str(r.get("notes", "")),
        })
    return cards


def read_card_strategy() -> list[dict]:
    """Read the `card_strategy` table. Returns `[]` when it has no rows."""
    records = supabase_client.read_card_strategy_records()
    rows = []
    for r in records:
        category = str(r.get("category", "")).strip()
        if not category:
            continue
        rows.append({
            "category": category,
            "primary_card_id": str(r.get("primary_card_id", "")).strip(),
            "primary_cap": _as_float(r.get("primary_cap")),
            "primary_earn_rate": _as_float(r.get("primary_earn_rate")),
            "fallback_card_id": str(r.get("fallback_card_id", "")).strip(),
            "fallback_earn_rate": _as_float(r.get("fallback_earn_rate")),
            "promo_active_until": str(r.get("promo_active_until", "")).strip(),
            "notes": str(r.get("notes", "")),
        })
    return rows


def _check_setup() -> dict | None:
    """None if ready; setup_required dict otherwise.

    Tables always exist in Postgres, so (unlike the Sheet-tab era) only the
    empty-table / missing-sentinel cases can trigger the gate. The message
    text is unchanged — the skill layer surfaces it verbatim.
    """
    cards = read_cards()
    strategies = read_card_strategy()
    missing = []
    if not cards:
        missing.append("`Cards` tab is empty or missing")
    if not strategies:
        missing.append("`CardStrategy` tab is empty or missing")
    elif not any(s["category"] == DEFAULT_STRATEGY_KEY for s in strategies):
        missing.append(
            f"`CardStrategy` needs a sentinel row with category=`{DEFAULT_STRATEGY_KEY}`"
        )
    if missing:
        return _setup_error(
            "Card optimiser not ready: "
            + "; ".join(missing)
            + ". See sheets-template/README.md (Tab 7: Cards / Tab 8: CardStrategy)."
        )
    return None


# --- Resolution helpers -----------------------------------------------------


def _find_card_by_payment_method(payment_method: str,
                                 cards: list[dict]) -> dict | None:
    """Longest `payment_method_pattern` substring match wins, case-insensitive."""
    if not payment_method:
        return None
    pm_lower = payment_method.lower()
    best = None
    best_len = 0
    for c in cards:
        pat = (c.get("payment_method_pattern") or "").lower().strip()
        if not pat:
            continue
        if pat in pm_lower and len(pat) > best_len:
            best = c
            best_len = len(pat)
    return best


def _find_strategy(category: str, strategies: list[dict]) -> dict | None:
    """Exact category match first, else the `_default` sentinel row, else None."""
    for s in strategies:
        if s["category"] == category:
            return s
    for s in strategies:
        if s["category"] == DEFAULT_STRATEGY_KEY:
            return s
    return None


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


# --- Cycle math -------------------------------------------------------------


def _cycle_window(cycle_start_day: int, as_of: date) -> tuple[date, date]:
    """Return the cycle window (start, end inclusive) containing `as_of`.

    `cycle_start_day` is clamped to the month length (e.g. day 31 in Feb
    clamps to 28). End date is (start of next cycle) − 1 day.
    """
    csd = max(1, min(31, int(cycle_start_day)))
    y, m = as_of.year, as_of.month
    cur_day = min(csd, monthrange(y, m)[1])
    cur_month_start = date(y, m, cur_day)

    if as_of >= cur_month_start:
        start = cur_month_start
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        next_day = min(csd, monthrange(ny, nm)[1])
        next_start = date(ny, nm, next_day)
    else:
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        prev_day = min(csd, monthrange(py, pm)[1])
        start = date(py, pm, prev_day)
        next_start = cur_month_start
    return (start, next_start - timedelta(days=1))


# --- Spend aggregation ------------------------------------------------------


def _txn_rows_in_cycle(card: dict, category: str | None,
                       as_of: date) -> list[dict]:
    """Read transactions for `card` within the cycle window containing
    `as_of`. Optional category filter (exact match). Always excludes
    UNCATEGORIZED and backfill rows."""
    records = supabase_client.read_all_transaction_rows()
    start, end = _cycle_window(card["cycle_start_day"], as_of)
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    rows = []
    for r in records:
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        row_date = _parse_date(r.get("Date", ""))
        if row_date is None or not (start <= row_date <= end):
            continue
        row_pm = str(r.get("Payment Method", "")).lower()
        if pat and pat not in row_pm:
            continue
        if category is not None:
            if str(r.get("Category", "")).strip() != category:
                continue
        rows.append(r)
    return rows


def _spend_in_cycle(card: dict, category: str | None, as_of: date) -> float:
    return round(
        sum(_as_float(r.get("Amount")) for r in _txn_rows_in_cycle(card, category, as_of)),
        2,
    )


def _cap_status(spent: float, cap: float) -> str:
    """`ok` <80%, `warning` 80–99%, `capped` ≥100%. Unlimited cap (≤0) is `ok`."""
    if cap <= 0:
        return "ok"
    pct = spent / cap
    if pct < 0.80:
        return "ok"
    if pct < 1.00:
        return "warning"
    return "capped"


def _percent(spent: float, cap: float) -> float:
    return round(spent / cap * 100, 1) if cap > 0 else 0.0


def _has_cap_room(spent_before: float, cap: float) -> bool:
    return cap <= 0 or spent_before < cap


# --- Promo expiry reversion -------------------------------------------------


def _lazy_revert_expired_promos() -> list[dict]:
    """Scan `card_strategy` for rows where `promo_active_until` is in the
    past, and restore the prior config from the `PREV:` snapshot embedded in
    the `notes` column. Returns one dict per reverted row.

    Each reversion is a whole-row upsert keyed on category (replacing the
    Sheet era's per-cell writes); the `||PREV:` parsing itself is unchanged
    (M13 — writer and reader must stay in lockstep with
    `set_category_primary`)."""
    records = supabase_client.read_card_strategy_records()
    today = date.today()
    reverted = []
    for r in records:
        until_raw = str(r.get("promo_active_until", "") or "").strip()
        if not until_raw:
            continue
        expiry = _parse_date(until_raw)
        if expiry is None or expiry >= today:
            continue

        category = str(r.get("category", ""))
        row = {h: r.get(h, "") for h in _STRATEGY_HEADERS}
        notes = str(row.get("notes", "") or "")
        if " ||PREV:" in notes:
            clean_notes, prev_blob = notes.split(" ||PREV:", 1)
            restored = {}
            for pair in prev_blob.strip().split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    restored[k.strip()] = v.strip()
            for h, val in restored.items():
                if h in _STRATEGY_HEADERS:
                    row[h] = val
            row["notes"] = clean_notes.strip()
            reverted.append({
                "category": category,
                "restored_from": restored,
            })
        else:
            reverted.append({
                "category": category,
                "restored_from": "expiry_only",
            })
        row["promo_active_until"] = ""
        supabase_client.upsert_card_strategy(row)
    return reverted


# --- CardNudgeLog -----------------------------------------------------------


def _already_nudged(cycle_window: str, card_id: str, category: str,
                    threshold: int) -> bool:
    for r in supabase_client.read_card_nudge_log():
        if (str(r.get("cycle_window", "")) == cycle_window
                and str(r.get("card_id", "")) == card_id
                and str(r.get("category", "")) == category
                and str(r.get("threshold", "")) == str(threshold)):
            return True
    return False


def _record_nudge(cycle_window: str, card_id: str, category: str,
                  threshold: int, txn_id: str, fallback_card_id: str) -> None:
    # The returned dedup bool is ignored: the check-then-send flow already
    # ran _already_nudged, and the table's UNIQUE constraint makes a lost
    # race harmless (the nudge was sent either way).
    supabase_client.append_card_nudge(
        cycle_window,
        card_id,
        category,
        threshold,
        triggering_txn_id=txn_id,
        fallback_card_id=fallback_card_id or "",
    )


def _last_n_in_category_on_card(category: str, card_id: str,
                                cards: list[dict], n: int = 2) -> bool:
    """True if the last `n` non-pending, non-backfill transactions in
    `category` (across all time) landed on `card_id` by payment-method
    substring match. Used for silent-when-already-switched."""
    cards_by_id = {c["card_id"]: c for c in cards}
    card = cards_by_id.get(card_id)
    if card is None:
        return False
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    if not pat:
        return False

    try:
        records = supabase_client.read_all_transaction_rows()
    except Exception:
        return False

    matched = []
    for r in reversed(records):
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        if str(r.get("Category", "")).strip() != category:
            continue
        matched.append(r)
        if len(matched) >= n:
            break
    if len(matched) < n:
        return False
    return all(pat in str(r.get("Payment Method", "")).lower() for r in matched)


def _find_triggering_txn_id(card: dict, category: str, amount: float,
                            txn_date: date) -> str:
    """Best-effort lookup of the txn_id that just triggered the nudge."""
    try:
        records = supabase_client.read_all_transaction_rows()
    except Exception:
        return ""
    pat = (card.get("payment_method_pattern") or "").lower().strip()
    target = txn_date.isoformat()
    for r in reversed(records):
        if str(r.get("Date", ""))[:10] != target:
            continue
        if str(r.get("Category", "")).strip() != category:
            continue
        if abs(_as_float(r.get("Amount")) - amount) > 0.01:
            continue
        if pat and pat not in str(r.get("Payment Method", "")).lower():
            continue
        return str(r.get("txn_id", ""))
    return ""


# --- Post-cap nudge (called from handle_log_expense) ------------------------


def maybe_send_post_cap_nudge(payment_method: str, category: str,
                              amount: float, txn_date: str) -> dict:
    """Fire a Telegram nudge when a transaction pushes a card past 80% or
    100% of its monthly category cap — at most once per (cycle, card,
    category, threshold). Suppresses the 100% nudge if the user already
    switched to the fallback card after the 80% nudge.

    MUST NOT raise — callers wrap in try/except too, but this function is
    designed to always return a dict with `sent: bool` + a `reason`.
    """
    try:
        gate = _check_setup()
        if gate is not None:
            return {"sent": False, "reason": "setup"}

        cards = read_cards()
        strategies = read_card_strategy()
        card = _find_card_by_payment_method(payment_method, cards)
        if card is None:
            return {"sent": False, "reason": "unmapped_payment_method"}

        strat = _find_strategy(category, strategies)
        if strat is None:
            return {"sent": False, "reason": "no_strategy"}
        if strat["primary_card_id"] != card["card_id"]:
            return {"sent": False, "reason": "not_primary_for_category"}

        cap = strat["primary_cap"]
        if cap <= 0:
            return {"sent": False, "reason": "no_cap"}

        as_of = _parse_date(txn_date) or date.today()
        spent_after = _spend_in_cycle(card, category, as_of)
        spent_before = max(0.0, spent_after - amount)
        pct_before = spent_before / cap
        pct_after = spent_after / cap

        threshold = None
        if pct_before < 1.00 <= pct_after:
            threshold = 100
        elif pct_before < 0.80 <= pct_after:
            threshold = 80

        if threshold is None:
            return {"sent": False, "reason": "no_threshold_crossed"}

        start, end = _cycle_window(card["cycle_start_day"], as_of)
        cycle_key = f"{start.isoformat()}_{end.isoformat()}"

        if _already_nudged(cycle_key, card["card_id"], category, threshold):
            return {"sent": False, "reason": "already_nudged_this_cycle"}

        fallback_id = strat.get("fallback_card_id", "")
        fallback_rate = strat.get("fallback_earn_rate", 0.0)
        cards_by_id = {c["card_id"]: c for c in cards}
        fallback = cards_by_id.get(fallback_id)

        # Silent-when-already-switched: suppress the 100% nudge if the 80%
        # already fired AND the user has been using the fallback on the
        # last N transactions in this category.
        if (threshold == 100
                and fallback_id
                and _already_nudged(cycle_key, card["card_id"], category, 80)
                and _last_n_in_category_on_card(category, fallback_id, cards, n=2)):
            return {"sent": False, "reason": "silent_already_switched"}

        if threshold == 100:
            lead = f"⚠️ {card['display_name']} just hit its {category} cap."
        else:
            pct_display = int(pct_after * 100)
            lead = (
                f"⚠️ {card['display_name']} is at {pct_display}% of "
                f"its {category} cap."
            )
        if fallback is not None and fallback_rate > 0:
            body = (
                f"Switch to {fallback['display_name']} — {fallback_rate} mpd "
                f"instead of post-cap rate."
            )
        else:
            body = f"Consider another card for {category} spend until the cycle resets."
        text = f"{lead}\n{body}"

        # Lazy import to avoid a circular import at module load: the
        # expense-tracker tool module imports this module's post-cap hook.
        from tools.expense_sheets_tool import _send_telegram_bubble

        send_result = _send_telegram_bubble(text)
        if not send_result.get("ok"):
            return {
                "sent": False,
                "reason": "telegram_send_failed",
                "error": send_result.get("error", ""),
            }

        triggering_txn_id = _find_triggering_txn_id(card, category, amount, as_of)
        _record_nudge(
            cycle_key, card["card_id"], category, threshold,
            triggering_txn_id, fallback_id,
        )
        return {
            "sent": True,
            "threshold": threshold,
            "card_id": card["card_id"],
            "category": category,
            "fallback_card_id": fallback_id,
            "telegram_message_id": send_result.get("message_id", ""),
        }
    except Exception as exc:
        return {"sent": False, "reason": "exception", "error": str(exc)}


# --- Tool-facing public API -------------------------------------------------


def get_card_cap_status(card_id: str | None = None,
                        category: str | None = None,
                        as_of: str | None = None) -> dict:
    """Return per-(card, category) cycle spend + cap status.

    If `card_id` passed, only that card. If `category` passed, only that
    category within the reported cards. Otherwise reports every card and
    every strategy row whose `primary_card_id` matches the card.
    """
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    strategies = read_card_strategy()
    as_of_date = _parse_date(as_of) or date.today()
    strategy_by_category = {s["category"]: s for s in strategies}
    cards_by_id = {c["card_id"]: c for c in cards}

    if card_id:
        cards_to_report = [cards_by_id[card_id]] if card_id in cards_by_id else []
    else:
        cards_to_report = cards

    result_cards = []
    for c in cards_to_report:
        start, end = _cycle_window(c["cycle_start_day"], as_of_date)
        if category:
            cats = [category]
        else:
            cats = [
                s["category"]
                for s in strategies
                if s["primary_card_id"] == c["card_id"]
                and s["category"] != DEFAULT_STRATEGY_KEY
            ]

        cat_rows = []
        for cat in cats:
            strat = (
                strategy_by_category.get(cat)
                or strategy_by_category.get(DEFAULT_STRATEGY_KEY)
            )
            if strat is None:
                continue
            spent = _spend_in_cycle(c, cat, as_of_date)
            cap = strat["primary_cap"]
            status = _cap_status(spent, cap)
            earn_current = (
                strat["primary_earn_rate"]
                if status != "capped"
                else strat.get("fallback_earn_rate", 0.0)
            )
            cat_rows.append({
                "category": cat,
                "spent_in_cycle": spent,
                "cap": cap,
                "percent_used": _percent(spent, cap),
                "status": status,
                "earn_rate_current": earn_current,
                "fallback_card_id": strat.get("fallback_card_id", ""),
                "fallback_earn_rate": strat.get("fallback_earn_rate", 0.0),
            })

        result_cards.append({
            "card_id": c["card_id"],
            "display_name": c["display_name"],
            "cycle_start_day": c["cycle_start_day"],
            "cycle_window": [start.isoformat(), end.isoformat()],
            "categories": cat_rows,
        })

    return {
        "status": "ok",
        "as_of": as_of_date.isoformat(),
        "cards": result_cards,
    }


def recommend_card_for(category: str, amount: float | None = None,
                       as_of: str | None = None) -> dict:
    """Recommend the best card for a one-off purchase: primary if the cap
    still has room, fallback otherwise."""
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    strategies = read_card_strategy()
    as_of_date = _parse_date(as_of) or date.today()

    strat = _find_strategy(category, strategies)
    if strat is None:
        return {
            "status": "error",
            "message": (
                f"No CardStrategy row for '{category}' and no `{DEFAULT_STRATEGY_KEY}` "
                f"row either. Add one to the CardStrategy tab."
            ),
        }

    cards_by_id = {c["card_id"]: c for c in cards}
    primary = cards_by_id.get(strat["primary_card_id"])
    fallback = cards_by_id.get(strat.get("fallback_card_id", ""))

    if primary is None:
        return {
            "status": "error",
            "message": (
                f"CardStrategy points at unknown card_id "
                f"'{strat['primary_card_id']}' — add it to the Cards tab."
            ),
        }

    primary_spent = _spend_in_cycle(primary, category, as_of_date)
    primary_status = _cap_status(primary_spent, strat["primary_cap"])

    if primary_status == "capped" and fallback is not None and strat.get("fallback_earn_rate", 0) > 0:
        rec_card = fallback
        earn_rate = strat["fallback_earn_rate"]
        reason = (
            f"{primary['display_name']} {category} cap already hit this cycle "
            f"— {fallback['display_name']} earns {earn_rate} mpd instead of "
            f"post-cap rate."
        )
    else:
        rec_card = primary
        earn_rate = strat["primary_earn_rate"]
        if primary_status == "warning":
            reason = (
                f"{primary['display_name']} is at "
                f"{_percent(primary_spent, strat['primary_cap'])}% of the "
                f"{category} cap — still the right card until you cross it."
            )
        else:
            reason = (
                f"{primary['display_name']} at {earn_rate} mpd on {category}."
            )

    return {
        "status": "ok",
        "category": category,
        "amount": amount,
        "recommendation": {
            "card_id": rec_card["card_id"],
            "display_name": rec_card["display_name"],
            "earn_rate": earn_rate,
            "reason": reason,
        },
        "primary_status": primary_status,
        "primary_spent_in_cycle": primary_spent,
        "primary_cap": strat["primary_cap"],
    }


def plan_month(month: str | None = None) -> dict:
    """Return the current CardStrategy plan plus MTD cycle status. Also
    lazily reverts any expired promo overrides on first call after expiry."""
    gate = _check_setup()
    if gate:
        return gate

    reverted = _lazy_revert_expired_promos()
    strategies = read_card_strategy()
    cards = read_cards()
    cards_by_id = {c["card_id"]: c for c in cards}
    as_of = date.today()

    plan = []
    for s in strategies:
        primary = cards_by_id.get(s["primary_card_id"])
        if primary is None:
            plan.append({
                "category": s["category"],
                "error": f"unknown primary_card_id '{s['primary_card_id']}'",
            })
            continue
        cat_filter = None if s["category"] == DEFAULT_STRATEGY_KEY else s["category"]
        spent = _spend_in_cycle(primary, cat_filter, as_of)
        plan.append({
            "category": s["category"],
            "primary_card": {
                "card_id": primary["card_id"],
                "display_name": primary["display_name"],
                "earn_rate": s["primary_earn_rate"],
                "cap": s["primary_cap"],
            },
            "fallback_card_id": s.get("fallback_card_id", ""),
            "fallback_earn_rate": s.get("fallback_earn_rate", 0.0),
            "spent_in_cycle": spent,
            "percent_used": _percent(spent, s["primary_cap"]),
            "status": _cap_status(spent, s["primary_cap"]),
            "promo_active_until": s.get("promo_active_until", ""),
        })

    # Preformatted, deduped rendering: category rows identical to the
    # _default sentinel are noise (the Jul 2026 recap printed 14
    # near-identical lines). The skill presents plan_lines verbatim.
    default_row = next(
        (p for p in plan
         if p.get("category") == DEFAULT_STRATEGY_KEY and "error" not in p),
        None)

    def _cap_text(cap: float) -> str:
        return "uncapped" if not cap else f"cap ${cap:g}"

    def _same_as_default(p: dict) -> bool:
        if default_row is None or "error" in p:
            return False
        a, d = p["primary_card"], default_row["primary_card"]
        return (a["card_id"] == d["card_id"]
                and a["earn_rate"] == d["earn_rate"]
                and a["cap"] == d["cap"])

    plan_lines = []
    for p in plan:
        if "error" in p or p.get("category") == DEFAULT_STRATEGY_KEY:
            continue
        if _same_as_default(p):
            continue
        c = p["primary_card"]
        promo = (f" (promo until {p['promo_active_until']})"
                 if p.get("promo_active_until") else "")
        plan_lines.append(
            f"- {p['category']} → {c['display_name']} @{c['earn_rate']:g} "
            f"mpd, {_cap_text(c['cap'])}{promo}")
    if default_row is not None:
        d = default_row["primary_card"]
        plan_lines.append(
            f"- Everything else → {d['display_name']} @{d['earn_rate']:g} "
            f"mpd, {_cap_text(d['cap'])}")

    return {
        "status": "ok",
        "month": month or as_of.strftime("%Y-%m"),
        "plan": plan,
        "plan_lines": plan_lines,
        "reverted_promos": reverted,
    }


def review_card_efficiency(month: str | None = None) -> dict:
    """Walk every transaction in `month` and compare the card actually used
    vs. the card that would have been optimal given the cap state at the
    moment of the transaction. Returns miles earned, miles optimal, and a
    list of suboptimal transactions."""
    gate = _check_setup()
    if gate:
        return gate

    if not month:
        month = datetime.now().strftime("%Y-%m")

    cards = read_cards()
    strategies = read_card_strategy()
    cards_by_id = {c["card_id"]: c for c in cards}

    txns = supabase_client.read_transactions(month)
    txns = sorted(txns, key=lambda r: str(r.get("Date", "")))

    miles_actual = 0.0
    miles_optimal = 0.0
    suboptimal = []
    unmapped_count = 0
    spent_running: dict[tuple[str, str], float] = defaultdict(float)
    # Pattern-steered optimal spend per card — bounds the override by the
    # card's calendar-month bonus_cap, the same pool the live nudge gates
    # on (_steer_target_has_headroom). Approximation: organic spend on the
    # steer target isn't counted against the pool here.
    steer_spent: dict[str, float] = defaultdict(float)
    # Running calendar-month spend per ACTUAL card, for the min-spend
    # endorsement mirror (approximates _calendar_month_spend from the rows
    # this loop already walks — same month, same exclusions).
    cal_spent: dict[str, float] = defaultdict(float)

    for r in txns:
        if _is_pending(r):
            continue
        if str(r.get("Source", "")).strip().lower() == "backfill":
            continue
        if _is_pot_internal(r):
            # YouTrip-card spends can't earn miles and were counting as
            # "unmapped" noise in the scorecard.
            continue
        category = str(r.get("Category", "")).strip()
        amount = _as_float(r.get("Amount"))
        payment_method = str(r.get("Payment Method", ""))
        merchant = str(r.get("Merchant", ""))
        m_up = merchant.upper()
        txn_id = str(r.get("txn_id", ""))
        date_str = str(r.get("Date", ""))

        strat = _find_strategy(category, strategies)
        if strat is None:
            continue

        actual_card = _find_card_by_payment_method(payment_method, cards)
        primary_id = strat["primary_card_id"]
        cap = strat["primary_cap"]
        key_primary = (primary_id, category)
        primary_spent_before = spent_running[key_primary]

        if _has_cap_room(primary_spent_before, cap):
            optimal_id = primary_id
            optimal_rate = strat["primary_earn_rate"]
        else:
            optimal_id = strat.get("fallback_card_id") or primary_id
            optimal_rate = strat.get("fallback_earn_rate", 0.0)

        # Merchant patterns override the category row: category strategies
        # are too coarse for merchants the nudge already knows better about
        # (Grab in a yuu-primary category, WATSONS in the not-partner
        # list). Exception: an override BACK to a capped-out primary is
        # ignored — a pattern can't un-blow a cap.
        override_id = _pattern_optimal(merchant, cards_by_id)
        if override_id and override_id not in (optimal_id, primary_id):
            override_card = cards_by_id.get(override_id, {})
            override_cap = _as_float(override_card.get("bonus_cap"))
            if override_id == strat.get("fallback_card_id"):
                optimal_rate = strat.get("fallback_earn_rate", 0.0)
            elif override_cap > 0 and steer_spent[override_id] >= override_cap:
                # The steer target's modeled bonus pool is spent — the
                # live nudge gates the same steer on headroom
                # (steer_target_capped). Past the cap the card still
                # earns its base rate; no unlimited phantom miles.
                optimal_rate = _as_float(override_card.get("base_mpd"))
            else:
                optimal_rate = _PATTERN_STEER_RATES.get(override_id, 0.0)
                steer_spent[override_id] += amount
            optimal_id = override_id

        if actual_card is None:
            unmapped_count += 1
            actual_rate = 0.0
            actual_card_id = "unknown"
        else:
            actual_card_id = actual_card["card_id"]
            if actual_card_id == primary_id:
                spent_running[key_primary] += amount
                actual_rate = (
                    strat["primary_earn_rate"]
                    if _has_cap_room(primary_spent_before, cap)
                    else 0.0
                )
            elif actual_card_id == strat.get("fallback_card_id"):
                actual_rate = strat.get("fallback_earn_rate", 0.0)
            else:
                # Off-strategy card: its flat base rate (cards.base_mpd,
                # 0007), not zero — Vantage's uncapped 1.5 is real miles.
                actual_rate = _as_float(actual_card.get("base_mpd"))

        # dbs-yuu earns its bonus only on PARTNER merchants — for anything
        # else the strategy row's rate is phantom miles (the 0.25% trap,
        # "the single most expensive habit"). Same knowledge as the
        # nudge's not-partner branch, applied to the ACTUAL side so the
        # optimal floor below can't lift optimal back onto miles yuu never
        # paid (adversarial-review catch: Grab paid ON the yuu card scored
        # zero loss). For merchants no pattern knows, the credit line
        # right after restores the strategy's benefit of the doubt.
        if (actual_card is not None and actual_card_id == "dbs-yuu"
                and not any(p in m_up for p in _YUU_PARTNER_PATTERNS)):
            actual_rate = min(actual_rate, _as_float(actual_card.get("base_mpd")))

        # Using the card the model itself recommends earns the modeled
        # rate — without this, a pattern-steered card sitting outside the
        # category's strategy row (WATSONS tapped on uob-pref) would be
        # scored at base rate against its own recommendation.
        if actual_card is not None and actual_card_id == optimal_id:
            actual_rate = optimal_rate

        # The used card can out-earn the modeled optimal (uncapped base
        # rate vs a capped-out strategy row). Optimal is a floor — never
        # below actual — so per-txn "miles lost" can't go negative.
        if actual_rate > optimal_rate:
            optimal_id = actual_card_id
            optimal_rate = actual_rate

        # Min-spend priority, mirrored from the steer nudge: with ≤5 days
        # of the month left and the used card still short of its
        # calendar-month min spend, the routing was ENDORSED at runtime
        # ("min_spend_priority" — the money is going where it's needed).
        # The month-end scorecard must not scold what the live layer
        # deliberately blessed.
        if (actual_card is not None and optimal_rate > actual_rate
                and _as_float(actual_card.get("min_spend_bonus")) > 0):
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_left = monthrange(d.year, d.month)[1] - d.day
                if (days_left <= 5
                        and cal_spent[actual_card_id]
                        < _as_float(actual_card.get("min_spend_bonus"))):
                    optimal_id = actual_card_id
                    optimal_rate = actual_rate
            except ValueError:
                pass
        if actual_card is not None:
            cal_spent[actual_card_id] += amount

        miles_for_txn_actual = amount * actual_rate
        miles_for_txn_optimal = amount * optimal_rate
        miles_actual += miles_for_txn_actual
        miles_optimal += miles_for_txn_optimal

        if miles_for_txn_optimal - miles_for_txn_actual > 0.5:
            suboptimal.append({
                "txn_id": txn_id,
                "date": date_str,
                "merchant": merchant,
                "amount": amount,
                "category": category,
                "actual_card": actual_card_id,
                "actual_earn_rate": actual_rate,
                "optimal_card": optimal_id,
                "optimal_earn_rate": optimal_rate,
                "miles_lost": round(miles_for_txn_optimal - miles_for_txn_actual, 1),
            })

    # Preformatted lines for the recap: the Jul 2026 message kept the
    # tool's numbers but swapped a card NAME while paraphrasing — verbatim
    # strings leave the model nothing to garble.
    suboptimal.sort(key=lambda s: (-s["miles_lost"], s["date"]))

    def _cname(cid: str) -> str:
        return cards_by_id.get(cid, {}).get("display_name", cid)

    top_missed_lines = [
        (f"• {s['merchant']} (${s['amount']:.2f}) — used "
         f"{_cname(s['actual_card'])}, should have been "
         f"{_cname(s['optimal_card'])} (–{s['miles_lost']:.0f})")
        for s in suboptimal[:5]
    ]
    summary_line = (
        f"You earned {round(miles_actual):,} miles · optimal would have "
        f"been {round(miles_optimal):,} · left on the table: "
        f"{round(miles_optimal - miles_actual):,}"
    )

    return {
        "status": "ok",
        "month": month,
        "miles_earned_actual": round(miles_actual, 1),
        "miles_earned_optimal": round(miles_optimal, 1),
        "miles_left_on_table": round(miles_optimal - miles_actual, 1),
        "summary_line": summary_line,
        "top_missed_lines": top_missed_lines,
        "transactions_suboptimal": suboptimal,
        "unmapped_payment_methods": unmapped_count,
    }


def set_category_primary(category: str, card_id: str,
                         until_date: str | None = None,
                         earn_rate: float | None = None,
                         cap: float | None = None,
                         fallback_card_id: str | None = None,
                         fallback_earn_rate: float | None = None) -> dict:
    """Manual promo override: write or update a `card_strategy` row. When
    `until_date` is set, the row is tagged with `promo_active_until` and a
    snapshot of the prior values is written into `notes` (prefixed `||PREV:`)
    so `_lazy_revert_expired_promos` can restore it later.

    The write is a whole-row upsert keyed on category (replacing the Sheet
    era's per-cell update_cell dance); the `||PREV:` snapshot format is
    unchanged (M13 — the reversion parser depends on it)."""
    gate = _check_setup()
    if gate:
        return gate

    cards = read_cards()
    cards_by_id = {c["card_id"]: c for c in cards}
    if card_id not in cards_by_id:
        return {
            "status": "error",
            "message": (
                f"Unknown card_id '{card_id}' — add it to the Cards tab first."
            ),
        }
    if fallback_card_id and fallback_card_id not in cards_by_id:
        return {
            "status": "error",
            "message": (
                f"Unknown fallback_card_id '{fallback_card_id}' — add it to "
                f"the Cards tab first."
            ),
        }

    prev_row: dict = {}
    for r in supabase_client.read_card_strategy_records():
        if str(r.get("category", "")).strip() == category:
            prev_row = {h: r.get(h, "") for h in _STRATEGY_HEADERS}
            break

    # Build snapshot before overwriting so the reversion path has the data.
    snapshot_notes = ""
    prev_notes_raw = str(prev_row.get("notes", "") or "") if prev_row else ""
    prev_notes_clean = prev_notes_raw.split(" ||PREV:", 1)[0].strip()
    if until_date and prev_row:
        snapshot_parts = []
        for h in _STRATEGY_HEADERS:
            if h in ("notes", "promo_active_until"):
                continue
            snapshot_parts.append(f"{h}={prev_row.get(h, '')}")
        snapshot_notes = (prev_notes_clean + " ||PREV: " + ",".join(snapshot_parts)).strip()

    def _pick(new_val, old_val, default):
        if new_val is not None:
            return new_val
        if old_val not in (None, ""):
            return old_val
        return default

    new_primary_cap = _pick(cap, prev_row.get("primary_cap"), 0)
    new_primary_earn = _pick(earn_rate, prev_row.get("primary_earn_rate"), 0)
    new_fallback_id = _pick(fallback_card_id, prev_row.get("fallback_card_id"), "")
    new_fallback_earn = _pick(fallback_earn_rate, prev_row.get("fallback_earn_rate"), 0)

    new_row_values = {
        "category": category,
        "primary_card_id": card_id,
        "primary_cap": new_primary_cap,
        "primary_earn_rate": new_primary_earn,
        "fallback_card_id": new_fallback_id,
        "fallback_earn_rate": new_fallback_earn,
        "promo_active_until": until_date or "",
        "notes": snapshot_notes if until_date else prev_notes_clean,
    }

    supabase_client.upsert_card_strategy(new_row_values)
    action = "updated" if prev_row else "created"

    return {
        "status": "ok",
        "action": action,
        "category": category,
        "primary_card_id": card_id,
        "until_date": until_date or "",
    }


# --- min-spend + steering nudge hooks (strategy layer, 2026-07-28) ----------
# Merchant patterns hand-curated from the issuers' published bonus-category
# T&Cs (verified 2026-07-28; re-check when the quarterly re-verification
# runs — see .claude/skills/card-tnc-review). Substring match against the
# uppercased merchant string. These are one deployment's Singapore cards;
# replace them wholesale with your own issuers' bonus lists.

_YUU_PARTNER_PATTERNS = (
    "GOJEK", "GOPAY", "FOODPANDA", "FP*FOOD", "GUARDIAN", "7-ELEVEN",
    "COLD STORAGE", "CS FRESH", "JASONS", "GIANT", "BUS/MRT", "SIMPLYGO",
    "CHAGEE", "CHARGE+", "SINGTEL",
)
_ONLINE_4MPD_PATTERNS = (
    "SHOPEE", "LAZADA", "AMAZON", "GRAB", "KLOOK", "BOOKING", "AGODA",
    "FLYSCOOT", "SCOOT", "TRIP.COM", "EXPEDIA", "NETFLIX", "SPOTIFY",
    "MICROSOFT", "LINKEDIN",
)
# In-person merchants that are NOT yuu partners: catching them on the yuu
# card (0.25%) is the single most expensive habit the ledger showed.
_NOT_PARTNER_TAP_PATTERNS = ("SHENG SIONG", "FAIRPRICE", "NTUC", "WATSONS")

# Above this, a charge on Vantage is treated as deliberate big-one-off
# routing (uncapped 1.5 mpd beats overflowing a bonus cap) — no steer.
_STEER_BIG_ONEOFF_FLOOR = 500.0

# Earn rates for pattern-steered cards, mirroring the steer-nudge copy
# ("4 mpd", yuu partner up to 18% ≈ 10 mpd when the month qualifies).
# Kept next to the pattern tuples so the two stay in sync.
_PATTERN_STEER_RATES = {"dbs-yuu": 10.0, "uob-pref": 4.0, "hsbc-revo": 4.0}


def _pattern_optimal(merchant: str, cards_by_id: dict) -> str | None:
    """The card the MERCHANT patterns dictate for the scorecard's optimal,
    or None when the category strategy should stand. This is the same
    knowledge the steer nudge uses, applied to review_card_efficiency so
    the two can never contradict each other — Jul 2026: the nudge steered
    a Grab ride to hsbc-revo online (4 mpd) while the scorecard claimed
    dbs-yuu @10 for the same txn (Grab is not a yuu partner; on yuu it
    earns base 0.25%). Precedence mirrors the nudge: yuu partner first,
    known non-partner tap second, online-4mpd last."""
    m_up = merchant.upper()
    if any(p in m_up for p in _YUU_PARTNER_PATTERNS):
        return "dbs-yuu" if "dbs-yuu" in cards_by_id else None
    if any(p in m_up for p in _NOT_PARTNER_TAP_PATTERNS):
        return "uob-pref" if "uob-pref" in cards_by_id else None
    if any(p in m_up for p in _ONLINE_4MPD_PATTERNS):
        return "hsbc-revo" if "hsbc-revo" in cards_by_id else None
    return None


def _calendar_month_spend(card: dict, month: str) -> float:
    """Card spend in a CALENDAR month (by transaction date), excluding
    pending and backfill rows (M14). yuu's min-spend gate runs on calendar
    months per its T&Cs — deliberately NOT the statement cycle, which is
    why this doesn't reuse _spend_in_cycle."""
    pattern = str(card.get("payment_method_pattern", "")).lower().strip()
    if not pattern:
        return 0.0
    total = 0.0
    for row in supabase_client.read_all_transaction_rows():
        if _is_pending(row):
            continue
        if str(row.get("Source", "")).strip().lower() == "backfill":
            continue
        if not str(row.get("Date", "")).startswith(month):
            continue
        if pattern not in str(row.get("Payment Method", "")).lower():
            continue
        total += _as_float(row.get("Amount"))
    return round(total, 2)


def maybe_send_min_spend_nudge(payment_method: str, amount: float,
                               txn_date: str) -> dict:
    """Min-spend tracker nudge, calendar-month based.

    Anti-spam contract (agreed with Hadi 2026-07-28): at most (a) ONE
    "minimum met ✓" per card per calendar month, fired by the transaction
    that crosses the line, and (b) ONE at-risk warning per card per month,
    only when ≤5 days remain and the card is still short. Deduped through
    card_nudge_log with sentinel category `_min_spend` (threshold 100 =
    met, 80 = at-risk); cycle_window carries the calendar month string —
    the UNIQUE tuple makes the once-per-month guarantee DB-enforced.

    Only needs the cards table (no CardStrategy gate: min spend is a card
    property). MUST NOT raise — same contract as maybe_send_post_cap_nudge.
    """
    try:
        cards = read_cards()
        if not cards:
            return {"sent": False, "reason": "setup"}
        card = _find_card_by_payment_method(payment_method, cards)
        if card is None:
            return {"sent": False, "reason": "unmapped_payment_method"}
        min_spend = _as_float(card.get("min_spend_bonus"))
        if min_spend <= 0:
            return {"sent": False, "reason": "no_min_spend"}

        as_of = _parse_date(txn_date) or date.today()
        month = as_of.strftime("%Y-%m")
        month_name = as_of.strftime("%B")
        days_left = monthrange(as_of.year, as_of.month)[1] - as_of.day
        spent_after = _calendar_month_spend(card, month)
        spent_before = max(0.0, spent_after - amount)

        threshold = None
        if spent_before < min_spend <= spent_after:
            threshold = 100
        elif spent_after < min_spend and days_left <= 5:
            threshold = 80
        if threshold is None:
            return {"sent": False, "reason": "no_threshold"}
        if _already_nudged(month, card["card_id"], "_min_spend", threshold):
            return {"sent": False, "reason": "already_nudged_this_month"}

        if threshold == 100:
            text = (f"✅ {card['display_name']}: ${min_spend:,.0f} min spend "
                    f"met for {month_name} — bonus rate unlocked.")
        else:
            short = min_spend - spent_after
            plural = "s" if days_left != 1 else ""
            text = (f"⏳ {card['display_name']}: ${short:,.2f} short of the "
                    f"${min_spend:,.0f} min spend with {days_left} "
                    f"day{plural} left in {month_name}.")

        from tools.expense_sheets_tool import _send_telegram_bubble

        send_result = _send_telegram_bubble(text)
        if not send_result.get("ok"):
            return {"sent": False, "reason": "telegram_send_failed",
                    "error": send_result.get("error", "")}
        _record_nudge(month, card["card_id"], "_min_spend", threshold, "", "")
        return {"sent": True, "reason": f"min_spend_{threshold}",
                "card_id": card["card_id"], "text": text}
    except Exception as exc:
        return {"sent": False, "reason": "exception", "error": str(exc)}


def _steer_target_has_headroom(steer_to: str, merchant_upper: str,
                               month: str) -> bool:
    """True when `steer_to`'s relevant calendar-month bonus pool is not
    `capped` — the gate that stops a steer nudge pointing at a card whose
    bonus rate is already exhausted (2026-07-30 incident: "tap UOB
    Preferred next time (4 mpd)" with the tap pool at 119%).

    FAIL-OPEN by design: `get_bonus_pool_status` never raises, and any
    non-`ok` status degrades to True (today's behavior) — a pool-read
    error must never suppress a steer. A target absent from the result
    has no bonus_cap → unlimited headroom (covers dbs-yuu). For uob-pref
    the pool is picked by merchant class — online-whitelist merchants →
    `online`, everything else → `contactless` — mirroring
    get_bonus_pool_status's own attribution so the two never disagree.
    """
    pool_result = get_bonus_pool_status(month=month)
    if pool_result.get("status") != "ok":
        return True
    entry = next((c for c in pool_result.get("cards", [])
                  if c.get("card_id") == steer_to), None)
    if entry is None:
        return True
    if steer_to == "uob-pref":
        wanted = ("online"
                  if any(p in merchant_upper for p in _ONLINE_4MPD_PATTERNS)
                  else "contactless")
    else:
        wanted = "bonus"
    pool = next((p for p in entry.get("pools", [])
                 if p.get("pool") == wanted), None)
    if pool is None:
        return True
    return pool.get("status") != "capped"


def maybe_send_steer_nudge(merchant: str, payment_method: str,
                           amount: float, txn_date: str) -> dict:
    """Wrong-card steering: when a transaction lands on a card the strategy
    wouldn't pick for that merchant, send ONE gentle after-the-fact line —
    at most once per (merchant pattern, steer target) per calendar month,
    deduped via card_nudge_log sentinel `_steer:<pattern>` (threshold 0).

    Hadi explicitly wants steering-back, not point-of-sale advice ("very
    unlikely I will ask 'which card' before paying"). Rules, in priority
    order: yuu partner not on yuu → yuu; non-partner in-person spend ON
    yuu (the 0.25% trap) → Preferred tap; online-whitelist merchant not on
    Revo → Revo, EXCEPT deliberate big one-offs on Vantage (≥ the floor).

    Steering v2 (2026-07-30 incident, 11:48): for one FAIRPRICE txn on
    dbs-yuu the min-spend nudge said "$485.89 short of the $800 min spend
    with 1 day left" while THIS hook steered the same txn away to a
    Preferred tap pool already at 119%. Two deterministic checks now run
    after the rule cascade, before dedup:

    1. Min-spend priority — a card still short of its calendar-month min
       spend with ≤5 days left (the exact at-risk rule
       maybe_send_min_spend_nudge uses) outranks rate-chasing. Txn already
       ON the at-risk card → silent (`min_spend_priority`: the money is
       going where it's needed). At-risk card different from both used and
       target → the steer is overridden to the at-risk card with a
       shortfall message.
    2. Headroom — the FINAL target's bonus pool must not be `capped`
       (see _steer_target_has_headroom; fail-open on pool-read errors).
       A capped target falls back to the branch's next candidate; no
       candidate with headroom → silent (`steer_target_capped`).

    Dedup keys on the FINAL steer target. MUST NOT raise — same contract
    as the other hooks.
    """
    try:
        cards = read_cards()
        if not cards:
            return {"sent": False, "reason": "setup"}
        card = _find_card_by_payment_method(payment_method, cards)
        if card is None:
            return {"sent": False, "reason": "unmapped_payment_method"}
        used = card["card_id"]
        m_up = str(merchant or "").strip().upper()
        if not m_up:
            return {"sent": False, "reason": "no_merchant"}

        cards_by_id = {c["card_id"]: c for c in cards}

        def _name(cid: str) -> str:
            return cards_by_id.get(cid, {}).get("display_name", cid)

        steer_to = None
        pattern = None
        text = None
        # Ordered headroom-fallback candidates for the branch that fired
        # (the primary target first) — hardcoded like the cascade itself.
        fallbacks: tuple[str, ...] = ()
        if any(p in m_up for p in _YUU_PARTNER_PATTERNS):
            if used != "dbs-yuu" and "dbs-yuu" in cards_by_id:
                pattern = next(p for p in _YUU_PARTNER_PATTERNS if p in m_up)
                steer_to = "dbs-yuu"
                if pattern == "CHAGEE":
                    text = (f"💳 {merchant}: next time order via the CHAGEE "
                            f"app on {_name('dbs-yuu')} — yuu partner, up to "
                            f"18%. This one went on {_name(used)}.")
                else:
                    text = (f"💳 {merchant}: next time use {_name('dbs-yuu')} "
                            f"— yuu partner (up to 18% when the month "
                            f"qualifies). This one went on {_name(used)}.")
        elif used == "dbs-yuu" and any(
                p in m_up for p in _NOT_PARTNER_TAP_PATTERNS):
            pattern = next(p for p in _NOT_PARTNER_TAP_PATTERNS if p in m_up)
            if "uob-pref" in cards_by_id:
                steer_to = "uob-pref"
                fallbacks = ("uob-pref", "hsbc-revo")
                text = (f"💳 {merchant}: not a yuu partner (0.25% there) — "
                        f"tap {_name('uob-pref')} next time (4 mpd).")
        elif any(p in m_up for p in _ONLINE_4MPD_PATTERNS):
            pattern = next(p for p in _ONLINE_4MPD_PATTERNS if p in m_up)
            deliberate_oneoff = (used == "dbs-vantage"
                                 and amount >= _STEER_BIG_ONEOFF_FLOOR)
            if (used != "hsbc-revo" and not deliberate_oneoff
                    and "hsbc-revo" in cards_by_id):
                steer_to = "hsbc-revo"
                fallbacks = ("hsbc-revo",)
                text = (f"💳 {merchant}: next time use {_name('hsbc-revo')} "
                        f"online — 4 mpd. This one went on {_name(used)}.")

        if steer_to is None or text is None:
            return {"sent": False, "reason": "no_steer_rule"}

        as_of = _parse_date(txn_date) or date.today()
        month = as_of.strftime("%Y-%m")

        # (1) Min-spend priority override — same at-risk rule as
        # maybe_send_min_spend_nudge (short of min spend, ≤5 days left).
        days_left = monthrange(as_of.year, as_of.month)[1] - as_of.day
        if days_left <= 5:
            at_risk = []
            for c in cards:
                c_min = _as_float(c.get("min_spend_bonus"))
                if c_min <= 0:
                    continue
                c_spent = _calendar_month_spend(c, month)
                if c_spent < c_min:
                    at_risk.append((c, c_min, c_spent))
            if any(c["card_id"] == used for c, _, _ in at_risk):
                # The money is already going where it's needed — steering
                # it away would sabotage the min spend (the incident).
                return {"sent": False, "reason": "min_spend_priority"}
            if at_risk and all(c["card_id"] != steer_to
                               for c, _, _ in at_risk):
                c, c_min, c_spent = at_risk[0]
                short = c_min - c_spent
                plural = "s" if days_left != 1 else ""
                steer_to = c["card_id"]
                text = (f"💳 {merchant}: put this on {_name(steer_to)} "
                        f"instead — ${short:,.2f} short of the "
                        f"${c_min:,.0f} min spend with {days_left} "
                        f"day{plural} left.")
                # The shortfall message is specific to this card — never
                # swap another name into it via the headroom fallback.
                fallbacks = ()

        # (2) Headroom check on the FINAL target (fail-open helper).
        if not _steer_target_has_headroom(steer_to, m_up, month):
            new_target = None
            for cand in fallbacks:
                if cand == steer_to or cand not in cards_by_id:
                    continue
                if _steer_target_has_headroom(cand, m_up, month):
                    new_target = cand
                    break
            if new_target is None:
                return {"sent": False, "reason": "steer_target_capped"}
            text = text.replace(_name(steer_to), _name(new_target))
            steer_to = new_target

        dedup_cat = f"_steer:{pattern}"
        if _already_nudged(month, steer_to, dedup_cat, 0):
            return {"sent": False, "reason": "already_nudged_this_month"}

        from tools.expense_sheets_tool import _send_telegram_bubble

        send_result = _send_telegram_bubble(text)
        if not send_result.get("ok"):
            return {"sent": False, "reason": "telegram_send_failed",
                    "error": send_result.get("error", "")}
        _record_nudge(month, steer_to, dedup_cat, 0, "", used)
        return {"sent": True, "reason": "steer", "steer_to": steer_to,
                "pattern": pattern, "text": text}
    except Exception as exc:
        return {"sent": False, "reason": "exception", "error": str(exc)}


def get_bonus_pool_status(month: str | None = None) -> dict:
    """Calendar-month bonus-pool tracker for cards carrying a `bonus_cap`.

    UOB Preferred's 4 mpd runs as TWO separate S$600 calendar-month pools
    (mobile contactless / whitelist online — split 2025-10-01, per the
    card's published T&Cs). Bank alerts never reveal the payment channel,
    so pool attribution is by merchant class: online-whitelist merchants
    (_ONLINE_4MPD_PATTERNS) → online pool, everything else → contactless
    — under this deployment's standing assumptions (every in-person
    charge is mobile contactless; online charges are non-recurring). Any
    other card with a bonus_cap (hsbc-revo, S$1,000) reports a single
    pool. Both the split and the assumptions are card-specific: check
    yours before trusting the attribution.
    Cards without a bonus_cap are omitted entirely.

    Caps run on calendar months by POSTING date at the banks; we only
    have transaction dates, so end-of-month attribution can drift by a
    day or two — treat near-cap numbers as approximate.
    """
    try:
        cards = read_cards()
        if not cards:
            return {"status": "setup_required",
                    "message": "No cards configured — populate the cards "
                               "table first."}
        if not month:
            month = date.today().strftime("%Y-%m")

        rows = [
            r for r in supabase_client.read_all_transaction_rows()
            if not _is_pending(r)
            and str(r.get("Source", "")).strip().lower() != "backfill"
            and str(r.get("Date", "")).startswith(month)
        ]

        out_cards = []
        for card in cards:
            cap = _as_float(card.get("bonus_cap"))
            pattern = str(card.get("payment_method_pattern", "")).lower().strip()
            if cap <= 0 or not pattern:
                continue
            online = 0.0
            in_person = 0.0
            for row in rows:
                if pattern not in str(row.get("Payment Method", "")).lower():
                    continue
                m_up = str(row.get("Merchant", "")).strip().upper()
                amt = _as_float(row.get("Amount"))
                if any(p in m_up for p in _ONLINE_4MPD_PATTERNS):
                    online += amt
                else:
                    in_person += amt
            if card["card_id"] == "uob-pref":
                pools = [
                    {"pool": "contactless", "spent": round(in_person, 2),
                     "cap": cap, "status": _cap_status(in_person, cap)},
                    {"pool": "online", "spent": round(online, 2),
                     "cap": cap, "status": _cap_status(online, cap)},
                ]
            else:
                total = round(online + in_person, 2)
                pools = [{"pool": "bonus", "spent": total, "cap": cap,
                          "status": _cap_status(total, cap)}]
            out_cards.append({
                "card_id": card["card_id"],
                "display_name": card["display_name"],
                "pools": pools,
            })

        return {"status": "ok", "month": month, "cards": out_cards}
    except Exception as exc:
        return {"status": "error",
                "message": f"get_bonus_pool_status failed: {exc}"}

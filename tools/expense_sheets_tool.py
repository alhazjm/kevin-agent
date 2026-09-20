"""
Custom Hermes tools for personal expense tracking.

Backend (since migration PR 3/4): Supabase is the ledger of record — every
read and write goes through `tools.supabase_client`. The Google Sheet is a
read-only nightly EXPORT (the `export_sheet_backup` tool, invoked by the
3 AM cron): human-readable grid + the free-tier backup story. Hand edits
to the Sheet are overwritten nightly by design. As of migration 3b the
card/travel modules read Supabase live too — nothing reads the Sheet at
runtime anymore.

Registers tools with the Hermes tool registry:
  - log_expense:                 Record a new transaction (returns txn_id)
  - log_expense_pending:         Log as UNCATEGORIZED and send an ask-prompt
  - update_budget:               Change a category's monthly limit
  - get_remaining_budget:        Check spending vs budget
  - edit_expense:                Edit fields on an existing transaction
  - delete_expense:              Remove a transaction
  - link_telegram_message:       Attach a Telegram message_id to a txn
  - get_transaction_by_message_id: Resolve a reply-to-message back to a txn
  - lookup_merchant_category:    Check learned MerchantMap before asking
  - learn_merchant_mapping:      Persist a merchant → category mapping
  - detect_subscription_creep:   Scan for recurring charges and flag creep
  - undo_last_expense:           Delete the most recently logged transaction
  - generate_spending_report:    Detailed monthly spending report with citations
  - write_insight:               Persist a derived insight for future reports
  - get_insights:                Read stored insights (tier-4 memory)
  - generate_daily_insight:      Deterministic daily insight writer (cron; auto:* keys)
  - render_budget_chart:         Render a budget chart and deliver to Telegram
  - append_journal_entry:        Save user's freeform reply to the journal table
  - get_journal_entries:         Read journal table entries (most recent first)
  - sweep_missed_transactions:   Diff webhook_log vs transactions to find unlogged emails
  - export_sheet_backup:         Nightly rebuild of the Sheet tabs from Supabase
  - archive_year_snapshot:       Write-once Archive-<year> cold-storage tab (Jan-1 cron)
  - get_card_cap_status:         Per-card per-category MTD cycle spend vs cap
  - get_bonus_pool_status:       Calendar-month bonus-cap pools (Preferred: 2 pools)
  - recommend_card_for:          Which card to use for a category purchase
  - plan_month:                  Current CardStrategy plan + cycle status
  - review_card_efficiency:      Month-end scorecard: miles earned vs optimal
  - set_category_primary:        Manual promo override — write a CardStrategy row
  - get_active_travel_mode:      Active TravelMode row for today (or null)
  - get_trip_budget_status:      Per-bucket spent/budget for a trip
  - set_trip_bucket:             Retag a trip txn's bucket via Notes prefix
  - create_trip:                 Insert a travel_mode trip row (preview-confirmed)
  - link_topup_to_trip:          Stamp [trip:<label>] into a top-up txn's Notes
  - create_loan:                 Record an IOU in the loans table
  - mark_loan_repaid:            Flip a loan to repaid + log the negative offset txn
  - list_open_loans:             Open IOUs, oldest first, with total outstanding
  - sweep_loan_offsets:          Nightly: log missing offsets for PWA-flipped repaid loans
"""

import os
import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime

from tools.registry import registry
from tools import sheets_client    # mirror target + sweep source (dual-write window)
from tools import supabase_client  # ledger of record since migration PR 3


TOOLSET = "expense_tracker"


def _sheets_configured() -> bool:
    """Gate for every tool. Supabase is the ledger of record, so it is the
    only hard requirement.

    This used to demand the Google Sheets pair as well, which made sense
    during the migration window when the Sheet still powered the mutation
    mirror, the WebhookLog sweep and the card/travel hooks. All three of
    those moved to Postgres, and the Sheet became a write-only nightly
    backup — but the gate was never relaxed, so a new install had to stand
    up a Google Cloud project, a service account and a spreadsheet before
    the agent could log a single expense. Nothing read the Sheet by then.

    Sheets config is now checked only by the two tools that actually write
    to it (see `_sheet_export_configured`). The function keeps its old name
    because it is the `check_fn` on all 37 registrations.
    """
    return supabase_client._configured()


_SHEET_EXPORT_SETUP_MSG = (
    "Google Sheet backup is not configured — set GSPREAD_SPREADSHEET_ID and "
    "GOOGLE_SERVICE_ACCOUNT_JSON to enable it. This is optional; the ledger "
    "lives in Supabase and is unaffected."
)

def _sheet_export_configured() -> bool:
    """Extra gate for the two tools that write to the Google Sheet.

    `export_sheet_backup` (nightly) and `archive_year_snapshot` (Jan 1).
    Both are optional conveniences on top of the ledger, so an unconfigured
    Sheet is a `setup_required` no-op rather than an error — same contract
    the card optimiser uses before its tables exist.
    """
    return bool(
        os.environ.get("GSPREAD_SPREADSHEET_ID")
        and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    )



# Category-keyword → emoji table for the confirmation bubble prefix. First
# substring hit against the lowercased category wins, so put specific
# keywords (e.g. "miso litter") ABOVE general ones (e.g. "miso"). Pure
# visual scannability — no behaviour depends on the emoji.
_CATEGORY_EMOJI_KEYWORDS = [
    ("miso food",       "🍖"),
    ("miso litter",     "🐾"),
    ("miso grooming",   "✂️"),
    ("miso dental",     "🦷"),
    ("miso",            "🐱"),
    ("sam food",       "🥘"),
    ("sam travel",     "✈️"),
    ("sam phone",      "📱"),
    ("sam clothes",    "👗"),
    ("sam dental",     "🦷"),
    ("sam hair",       "💇"),
    ("sam self",       "📚"),
    ("sam",            "💕"),
    ("personal - food", "🍜"),
    ("food",            "🍜"),
    ("drink",           "🍜"),
    ("groc",            "🛒"),
    ("travel",          "🚇"),
    ("transport",       "🚇"),
    ("bus/mrt",         "🚇"),
    ("fuel",            "⛽"),
    ("car rental",      "🚗"),
    ("bill",            "💡"),
    ("utilit",          "💡"),
    ("electric",        "💡"),
    ("water",           "💧"),
    ("insurance",       "🛡️"),
    ("gift",            "🎁"),
    ("donation",        "🎁"),
    ("clean",           "🧹"),
    ("laundry",         "🧺"),
    ("dental",          "🦷"),
    ("haircut",         "✂️"),
    ("foot massage",    "💆"),
    ("massage",         "💆"),
    ("dinner",          "🍽️"),
    ("date",            "🍽️"),
    ("parent",          "👨‍👩‍👧"),
    ("phone",           "📱"),
    ("internet",        "🌐"),
    ("gym",             "🏋️"),
    ("spotify",         "🎵"),
    ("google",          "☁️"),
    ("icloud",          "☁️"),
    ("onedrive",        "☁️"),
    ("obsidian",        "📝"),
    ("anthropic",       "🤖"),
    ("openai",          "🤖"),
    ("self improv",     "📚"),
    ("savings",         "💰"),
    ("to claim",        "🧾"),
    ("claim",           "🧾"),
    ("misc",            "📦"),
]
_DEFAULT_BUBBLE_EMOJI = "💳"


def _pick_category_emoji(category: str) -> str:
    lower = (category or "").lower()
    for keyword, emoji in _CATEGORY_EMOJI_KEYWORDS:
        if keyword in lower:
            return emoji
    return _DEFAULT_BUBBLE_EMOJI


def _send_telegram_bubble(text: str, chat_id: str = "") -> dict:
    """Send a message via the Telegram Bot API and return the message_id.

    Uses urllib (stdlib) to avoid depending on python-telegram-bot internals.
    Falls back to TELEGRAM_ALLOWED_USERS env var if chat_id is not provided
    (works for this single-user bot).

    Returns {"ok": True, "message_id": "..."} on success or
    {"ok": False, "error": "..."} on failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        if "," in chat_id:
            chat_id = chat_id.split(",")[0].strip()

    if not chat_id:
        return {"ok": False, "error": "No chat_id and TELEGRAM_ALLOWED_USERS not set"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return {"ok": True, "message_id": str(data["result"]["message_id"])}
            return {"ok": False, "error": data.get("description", "Telegram API error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _send_telegram_photo(photo_url: str, caption: str, chat_id: str = "") -> dict:
    """Send a photo via the Telegram Bot API using a URL that Telegram fetches
    server-side. Same auth / chat_id resolution as _send_telegram_bubble.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        if "," in chat_id:
            chat_id = chat_id.split(",")[0].strip()

    if not chat_id:
        return {"ok": False, "error": "No chat_id and TELEGRAM_ALLOWED_USERS not set"}

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = json.dumps({
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return {"ok": True, "message_id": str(data["result"]["message_id"])}
            return {"ok": False, "error": data.get("description", "Telegram API error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


# --- log_expense ---

LOG_EXPENSE_SCHEMA = {
    "name": "log_expense",
    "description": (
        "Log a new expense transaction to the ledger (Supabase Postgres). Automatically "
        "sends a Telegram confirmation bubble and links the message_id to "
        "the transaction row (enabling reply-to-message edits). "
        "Do NOT send the confirmation yourself — the tool handles it. "
        "When this tool returns bubble_sent=true, you MUST produce an "
        "EMPTY assistant reply — the user has already seen the confirmation "
        "via the bubble and any extra text is a duplicate message. "
        "A non-SGD currency is converted to SGD IN THE TOOL (frankfurter; "
        "an `orig:` trace is stamped into notes) — pass the foreign amount "
        "and currency as given, never convert yourself. During an active "
        "trip the tool routes YouTrip / FX-converted spends into the trip "
        "category itself — you still propose the HOME category as usual "
        "(it becomes the trip bucket)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "Name of the merchant or store",
            },
            "amount": {
                "type": "number",
                "description": (
                    "Transaction amount in the given currency (the ORIGINAL "
                    "foreign amount when currency is not SGD — the tool "
                    "converts)."
                ),
            },
            "currency": {
                "type": "string",
                "description": (
                    "ISO currency code as the user/bank gave it (default: "
                    "SGD). Non-SGD is converted to SGD in the tool."
                ),
                "default": "SGD",
            },
            "category": {
                "type": "string",
                "description": (
                    "Budget category — the PLAIN category name copied exactly "
                    "from the Budget sheet, never decorated with emojis or "
                    "prefixes. Unknown names are refused with "
                    "status=unknown_category (plus `closest` suggestions) and "
                    "nothing is logged. Use your best judgment to pick an "
                    "EXISTING category based on the merchant name. During an "
                    "active trip, propose the HOME category exactly as always "
                    "— the tool re-routes trip spends itself and reports it "
                    "under `trip_routed`."
                ),
            },
            "create_category": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true ONLY when the user themselves asked for a "
                    "brand-new budget category by that exact name. Never set "
                    "it on your own judgment — with the default false, an "
                    "unknown category returns status=unknown_category "
                    "instead of being silently created."
                ),
            },
            "date": {
                "type": "string",
                "description": "Transaction date in YYYY-MM-DD format. Defaults to today.",
            },
            "payment_method": {
                "type": "string",
                "description": "Payment source (e.g. 'DBS/POSB card ending 1234', 'PayLah! Wallet')",
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes about the transaction",
                "default": "",
            },
            "source": {
                "type": "string",
                "description": (
                    "How this transaction reached the agent. Allowed values: "
                    "`email` (came from the Gmail Apps Script webhook), "
                    "`manual` (user typed it directly via Telegram), "
                    "`backfill` (imported from a statement, e.g. Sam's "
                    "supplementary card). Defaults to `manual`. The "
                    "subscription-creep detector ignores `backfill` rows."
                ),
                "enum": ["email", "manual", "backfill"],
                "default": "manual",
            },
            "time": {
                "type": "string",
                "description": (
                    "Transaction time, 24h local time, e.g. '19:47' or '19:47:03'. "
                    "ALWAYS pass this through when the webhook payload "
                    "carries a Time field — it disambiguates two real "
                    "purchases at the same merchant for the same amount on "
                    "the same day. Leave empty for manual logs without a "
                    "known time."
                ),
                "default": "",
            },
            "route_to_trip": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Set false ONLY when the user explicitly says this "
                    "purchase is NOT a trip cost (e.g. a home order placed "
                    "abroad). Default true: during an active trip the tool "
                    "routes YouTrip taps and FX-converted charges into the "
                    "trip category itself. Fixed monthly bills are never "
                    "routed regardless of this flag."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "The 16-hex dedup key from the webhook payload's "
                    "'Idempotency key' line, copied EXACTLY. Never invent, "
                    "derive, or modify one — omit this argument entirely "
                    "for manual logs or when the payload has no key; the "
                    "ledger computes it then. Passing the payload's key "
                    "verbatim is what makes a retried webhook dedupe even "
                    "when other arguments drift."
                ),
                "default": "",
            },
        },
        "required": ["merchant", "amount", "category"],
    },
}


def _format_bubble(merchant: str, amount: float, currency: str,
                   category: str, payment_method: str, txn_id: str) -> str:
    """Build a short, scannable confirmation bubble from transaction data.

    Prefixes a category emoji (keyword-matched, deterministic) so the
    message scans faster in a busy Telegram chat. Pure cosmetic — the
    LLM never sees or reasons about this.
    """
    emoji = _pick_category_emoji(category)
    line1 = f"{emoji} Logged {currency} {amount:.2f} at {merchant} \u2192 {category}"
    parts = [line1]
    if payment_method:
        parts.append(payment_method)
    parts.append(f"({txn_id})")
    return "\n".join(parts)


def _retry_link(txn_id: str, message_id: str, max_attempts: int = 3) -> dict:
    """Retry supabase_client.link_telegram_message with exponential backoff.

    Kept from the Sheets era (where 60 req/min bursts made this fire
    regularly): Supabase has no comparable quota, but network blips during
    the log→link handoff would still orphan the reply-to-edit link, and
    the retry is cheap insurance.
    """
    for attempt in range(max_attempts):
        try:
            result = supabase_client.link_telegram_message(txn_id, message_id)
            return result
        except Exception as exc:
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
            else:
                return {
                    "status": "error",
                    "message": f"Failed after {max_attempts} attempts: {exc}",
                }


# Frankfurter (ECB reference rates) — the same free, keyless source the
# Apps Script uses in `convertToSGD`, so a manual "rm33" and a DBS-in-KL
# email stamp byte-identical `orig:` traces.
_FX_API_BASE = "https://api.frankfurter.dev/v1"


def _fx_to_sgd(amount: float, currency: str, date: str = "") -> dict:
    """Convert `amount` of `currency` to SGD via frankfurter (stdlib urllib,
    no SDK). Tries the DATED endpoint first (`/v1/{date}?base=CUR&symbols=SGD`
    — the rate the bank would have used) and falls back to `/v1/latest`
    when the dated call fails; up to 3 attempts per URL on network/5xx
    errors (0.5s/1s backoff), a 4xx skips straight to the next URL.

    Returns `{"ok": True, "amount": <2dp SGD>, "rate", "fx_date",
    "note": "orig: CUR amt @ rate6dp (frankfurter YYYY-MM-DD)"}` — the note
    mirrors Code.gs's `origNote` format exactly (the `orig:` prefix is the
    travel-routing signal, M13) — or `{"ok": False, "error"}`. SGD/empty
    currency is a passthrough (`rate` 1.0, empty note). Never raises."""
    cur = (currency or "").strip().upper()
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"non-numeric amount: {amount!r}"}
    if not cur or cur == "SGD":
        return {"ok": True, "amount": round(amt, 2), "rate": 1.0,
                "fx_date": "", "note": ""}

    candidates = []
    if date:
        candidates.append(f"{_FX_API_BASE}/{date}?base={cur}&symbols=SGD")
    candidates.append(f"{_FX_API_BASE}/latest?base={cur}&symbols=SGD")

    last_err = ""
    for url in candidates:
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "kevin-agent/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                rate = (data or {}).get("rates", {}).get("SGD")
                if not isinstance(rate, (int, float)) or isinstance(rate, bool):
                    raise ValueError("unexpected response shape (no rates.SGD)")
                fx_date = str(data.get("date") or "")
                stamp = f"frankfurter {fx_date}" if fx_date else "frankfurter"
                return {
                    "ok": True,
                    "amount": round(amt * float(rate), 2),
                    "rate": float(rate),
                    "fx_date": fx_date,
                    "note": f"orig: {cur} {amt:.2f} @ {float(rate):.6f} ({stamp})",
                }
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code} for {cur}"
                if exc.code < 500:
                    break  # unknown currency / bad date — try the next URL
            except Exception as exc:
                last_err = str(exc) or exc.__class__.__name__
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s
    return {"ok": False, "error": last_err or "fx lookup failed"}


def _fx_failed_result(currency: str, amount: float, error: str) -> str:
    """The shared fx_failed refusal (log_expense + log_expense_pending):
    nothing written, no bubble — the model asks the user for the SGD
    amount and logs that instead."""
    return json.dumps({
        "status": "error",
        "reason": "fx_failed",
        "message": (
            f"Couldn't convert {(currency or '').strip().upper()} "
            f"{amount:.2f} to SGD ({error}) — nothing logged. Ask the user "
            "for the SGD amount and log that instead."
        ),
    })


def handle_log_expense(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    amount = float(args.get("amount", 0))
    category = args.get("category", "Miscellaneous")
    currency = args.get("currency", "SGD")
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    payment_method = args.get("payment_method", "")
    notes = args.get("notes", "")
    source = args.get("source", "manual")
    txn_time = args.get("time", "")
    create_category = bool(args.get("create_category", False))
    idempotency_key = str(args.get("idempotency_key", "") or "")

    # Step 0a: FX normalisation IN THE TOOL (2026-08-17). A manual foreign
    # amount ("rm33") is converted to SGD here — the model no longer
    # converts — and the `orig:` trace is stamped into notes exactly like
    # Code.gs does for bank emails, so downstream (travel routing, PWA,
    # recon) sees one format. Email payloads arrive pre-converted with
    # `orig:` already in notes and are left alone. On FX failure NOTHING
    # is written and no bubble is sent — the model asks for the SGD amount.
    if (currency or "").strip().upper() != "SGD" and "orig:" not in (notes or "").lower():
        fx = _fx_to_sgd(amount, currency, date)
        if not fx.get("ok"):
            return _fx_failed_result(currency, amount, fx.get("error", ""))
        amount = fx["amount"]
        currency = "SGD"
        notes = f"{notes}; {fx['note']}" if notes else fx["note"]

    # Step 0b: deterministic trip routing (the tool decides, not the
    # model). With a trip active for `date`, a YouTrip payment or an
    # `orig:` FX trace sends the spend into the trip category with a
    # `[bucket:X]` derived from the PROPOSED home category; SGD spends with
    # no signal stay home. Lazy import (M10); the router never raises.
    trip_routed = None
    route_to_trip = args.get("route_to_trip", True)
    if isinstance(route_to_trip, str):
        route_to_trip = route_to_trip.strip().lower() not in ("false", "0", "no")
    try:
        from tools import travel_mode
        routing = (
            travel_mode.route_for_trip(
                txn_date=date,
                proposed_category=category,
                payment_method=payment_method,
                notes=notes,
                currency=currency,
            )
            if route_to_trip
            else {"applied": False, "reason": "opted_out"}
        )
        if routing.get("applied"):
            trip_routed = {
                "trip_label": routing.get("trip_label", ""),
                "from_category": routing.get("from_category", category),
                "to_category": routing.get("category", category),
                "bucket": routing.get("bucket"),
                "signal": routing.get("signal"),
            }
            category = routing["category"]
            notes = routing["notes"]
    except Exception:
        trip_routed = None

    # Step 1: log the transaction to Supabase (dedup is atomic there via
    # the UNIQUE(idempotency_key) index; txn_time participates in the key
    # so two real same-day same-amount purchases both log). A valid
    # payload-provided idempotency_key wins over recomputation — see
    # append_transaction's docstring (the 2026-08-02 Gojek double-log).
    # The category is snapped to an existing Budget category inside
    # append_transaction; unknown names come back as
    # status=unknown_category with nothing written unless
    # create_category=True (a user decision, never the LLM's).
    result = supabase_client.append_transaction(
        date=date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        category=category,
        source=source,
        payment_method=payment_method,
        notes=notes,
        txn_time=txn_time,
        create_category=create_category,
        idempotency_key=idempotency_key,
    )

    # Unknown category: nothing was logged, no bubble to send — return the
    # refusal (with `closest` suggestions) so the assistant can re-ask.
    if result.get("status") == "unknown_category":
        return json.dumps(result)

    # If duplicate detected, skip bubble and return early
    if result.get("status") == "duplicate":
        result["note"] = (
            "Duplicate transaction detected — this exact transaction was "
            "already logged. Returning existing entry."
        )
        return json.dumps(result)

    # append_transaction may have snapped a decorated/mis-cased name onto
    # the canonical one — use the canonical spelling for the bubble and
    # the nudge hooks below.
    if result.get("status") == "ok" and result.get("row"):
        category = result["row"][4]
    if trip_routed and result.get("status") == "ok":
        result["trip_routed"] = trip_routed

    txn_id = result.get("txn_id", "")

    # Near-duplicate advisory (the 2026-08-02 Gojek pair): banks
    # double-alert one purchase with drifted timestamps, which time-in-key
    # dedup CORRECTLY treats as distinct — so this is a flag, not a block.
    # The warning rides INSIDE the bubble (silence contract — never a
    # second message), after the "(txn_id)" line so the reply-to-edit
    # fallback still parses the bubble's own id first. Never raises.
    near_dup = None
    if result.get("status") == "ok" and txn_id:
        try:
            near_dup = supabase_client.find_near_duplicate(
                date=date, merchant=merchant, amount=amount,
                payment_method=payment_method, txn_time=txn_time,
                exclude_txn_id=txn_id,
            )
        except Exception:
            near_dup = None
        if near_dup:
            result["possible_duplicate_of"] = str(near_dup.get("txn_id", ""))

    # Step 2: automatically send confirmation bubble and link message_id.
    # This is fully deterministic — no LLM cooperation required.
    if txn_id and os.environ.get("TELEGRAM_BOT_TOKEN"):
        bubble = _format_bubble(merchant, amount, currency, category,
                                payment_method, txn_id)
        if near_dup:
            bubble += (
                f"\n⚠️ looks like {near_dup.get('txn_id', '?')} "
                f"minutes earlier — same amount, same card. If the bank "
                f"double-alerted, reply: delete {txn_id}"
            )
        send_result = _send_telegram_bubble(bubble)
        if send_result.get("ok"):
            message_id = send_result["message_id"]
            link_result = _retry_link(txn_id, message_id)
            result["telegram_message_id"] = message_id
            result["bubble_sent"] = True
            result["linked"] = link_result.get("status") == "ok"
            result["assistant_reply_required"] = False
            result["assistant_reply_policy"] = (
                "STOP. The confirmation bubble has already been delivered "
                "to the user via the Telegram Bot API. You MUST NOT emit "
                "any assistant text after this tool call. Produce an empty "
                "response. Do not write 'Logged.', 'Done.', or any "
                "acknowledgment — the user has already seen the bubble and "
                "any extra text is a duplicate message. THIS TURN ONLY: "
                "in later turns, direct user questions must always get a "
                "text answer — never carry this silence forward."
            )
            result["note"] = (
                "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply."
            )
        else:
            result["bubble_sent"] = False
            result["bubble_error"] = send_result.get("error", "")

    # Step 3: card-optimiser post-cap nudge. Gated inside the helper
    # behind _check_setup — returns {"sent": false, "reason": "setup"} when
    # the Cards / CardStrategy tabs aren't populated, so this is a no-op
    # until the user activates the card optimiser. Wrapped defensively:
    # card-optimiser failures must NEVER break log_expense.
    try:
        from tools import card_optimiser
        nudge = card_optimiser.maybe_send_post_cap_nudge(
            payment_method=payment_method,
            category=category,
            amount=amount,
            txn_date=date,
        )
        if nudge.get("sent"):
            result["card_nudge_sent"] = True
            result["card_nudge_detail"] = nudge
    except Exception as exc:
        result["card_nudge_error"] = str(exc)

    # Step 4: travel-mode per-bucket nudge. Only fires when an active
    # TravelMode row exists, this txn matches the trip's category, and the
    # Notes carry a `[bucket:X]` tag. Same defensive wrapper — never
    # breaks log_expense. Reads the Notes from the row we just appended,
    # since the LLM stamps the bucket via the `notes` arg.
    try:
        from tools import travel_mode
        trip_nudge = travel_mode.maybe_send_trip_bucket_nudge(
            category=category,
            amount=amount,
            txn_date=date,
            txn_id=txn_id,
            notes=notes,
        )
        if trip_nudge.get("sent"):
            result["trip_nudge_sent"] = True
            result["trip_nudge_detail"] = trip_nudge
    except Exception as exc:
        result["trip_nudge_error"] = str(exc)

    # Step 4b: min-spend tracker nudge (calendar-month, once-per-month
    # dedup) + wrong-card steering nudge (once per merchant-pattern per
    # month). Same defensive wrappers — strategy-layer failures must
    # NEVER break log_expense.
    try:
        from tools import card_optimiser
        ms_nudge = card_optimiser.maybe_send_min_spend_nudge(
            payment_method=payment_method,
            amount=amount,
            txn_date=date,
        )
        if ms_nudge.get("sent"):
            result["min_spend_nudge_sent"] = True
            result["min_spend_nudge_detail"] = ms_nudge
    except Exception as exc:
        result["min_spend_nudge_error"] = str(exc)

    try:
        from tools import card_optimiser
        steer_nudge = card_optimiser.maybe_send_steer_nudge(
            merchant=merchant,
            payment_method=payment_method,
            amount=amount,
            txn_date=date,
        )
        if steer_nudge.get("sent"):
            result["steer_nudge_sent"] = True
            result["steer_nudge_detail"] = steer_nudge
    except Exception as exc:
        result["steer_nudge_error"] = str(exc)

    # Step 5 (the old post-log ensure_category_exists) is gone: category
    # existence is enforced BEFORE the row lands, inside append_transaction.
    # When create_category=True minted a new one, the result already carries
    # new_category_created for the skill's "$0 limit — set a budget"
    # follow-up.

    return json.dumps(result)


registry.register(
    name="log_expense",
    toolset=TOOLSET,
    schema=LOG_EXPENSE_SCHEMA,
    handler=handle_log_expense,
    check_fn=_sheets_configured,
)


# --- log_expense_pending ---
#
# Paired with log_expense. Use when a transaction arrives but categorisation
# is ambiguous (always-ask merchant, LLM confidence <90%, PayLah! transfer to
# an individual). Unlike log_expense, this:
#   - forces category="UNCATEGORIZED" (the row gets logged anyway so the
#     ledger reflects that the transaction actually happened)
#   - sends a Telegram "ask-prompt" bubble with numbered options instead of
#     the normal confirmation bubble
#   - links the ask-prompt's message_id to the row
#
# When the user replies to the ask-prompt, the existing reply-to-message
# flow resolves the txn_id via either the message_id link OR the reply-to
# text fallback (which scans for "txn_YYYYMMDD_NNN" in the quoted text —
# the txn_id is always embedded in the ask-prompt for this fallback). The
# skill then calls edit_expense + learn_merchant_mapping as normal.

_PENDING_CATEGORY = "UNCATEGORIZED"


LOG_EXPENSE_PENDING_SCHEMA = {
    "name": "log_expense_pending",
    "description": (
        "Log a transaction with category='UNCATEGORIZED' and send a Telegram "
        "ask-prompt asking the user which category to use. The ask-prompt "
        "includes the txn_id so the user's reply resolves back to the row. "
        "Use this instead of log_expense when categorisation is genuinely ambiguous (NOT for supermarkets / marketplaces / convenience stores — those default to their category via log_expense and the user corrects by reply) — "
        ""
        "PayLah!/PayNow transfers to an individual, or when your confidence "
        "is below ~90%. You provide the numbered category options as a list. "
        "When the user replies with a choice, call edit_expense(txn_id=..., "
        "new_category=<picked>) then learn_merchant_mapping. "
        "When this tool returns bubble_sent=true, you MUST produce an EMPTY "
        "assistant reply — the user has already received the ask-prompt. "
        "A non-SGD currency is converted to SGD in the tool (never convert "
        "yourself); during an active trip a YouTrip / FX-converted spend is "
        "logged straight into the trip category (no ask-prompt) — offer the "
        "HOME category options as usual."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "Name of the merchant or store",
            },
            "amount": {
                "type": "number",
                "description": "Transaction amount in the given currency",
            },
            "currency": {
                "type": "string",
                "description": "Currency code (default: SGD)",
                "default": "SGD",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Numbered category options to offer the user — PLAIN "
                    "category names copied exactly from the Budget sheet, "
                    "e.g. ['Personal - Food & Drinks', 'Groceries']. NEVER "
                    "add emojis or any decoration (a decorated option once "
                    "got logged verbatim and minted a phantom category). "
                    "Keep to 2-4 plausible options; the user can also "
                    "reply with a freeform category name."
                ),
            },
            "date": {
                "type": "string",
                "description": "Transaction date in YYYY-MM-DD format. Defaults to today.",
            },
            "payment_method": {
                "type": "string",
                "description": "Payment source (e.g. 'DBS/POSB card ending 1234', 'PayLah! Wallet')",
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes about the transaction",
                "default": "",
            },
            "source": {
                "type": "string",
                "description": (
                    "How this transaction reached the agent. Same enum as "
                    "log_expense: `email`, `manual`, `backfill`."
                ),
                "enum": ["email", "manual", "backfill"],
                "default": "manual",
            },
            "time": {
                "type": "string",
                "description": (
                    "Transaction time, 24h local time. Same contract as "
                    "log_expense: pass the webhook payload's Time field "
                    "through when present."
                ),
                "default": "",
            },
        },
        "required": ["merchant", "amount", "options"],
    },
}


def _format_pending_prompt(merchant: str, amount: float, currency: str,
                           options: list[str], txn_id: str) -> str:
    """Build the ask-prompt bubble text. The trailing `(txn_id)` is critical —
    the reply-to fallback in SKILL.md scans the replied-to message for the
    txn_YYYYMMDD_NNN pattern, so if the link_telegram_message step fails
    (rate limit, race) the txn_id is still recoverable from the message text.
    """
    lines = [f"🤔 {currency} {amount:.2f} at {merchant} — which category?"]
    for i, opt in enumerate(options, start=1):
        # Defensive strip of leading emoji/decoration: options must render
        # as the plain Budget-tab names even if the model regresses on the
        # no-emoji rule (a decorated option once got logged verbatim and
        # minted a phantom category). Leading strip only — trailing text
        # like "(outside tracker)" is part of a real name.
        plain = re.sub(r"^[\W_]+", "", str(opt)).strip() or str(opt)
        lines.append(f"{i}. {plain}")
    lines.append(f"(or reply with another category name) ({txn_id})")
    return "\n".join(lines)


def handle_log_expense_pending(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    amount = float(args.get("amount", 0))
    currency = args.get("currency", "SGD")
    options = args.get("options", []) or []
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    payment_method = args.get("payment_method", "")
    notes = args.get("notes", "")
    source = args.get("source", "manual")

    if not options:
        return json.dumps({
            "status": "error",
            "message": (
                "log_expense_pending requires `options` (at least one "
                "numbered category to offer the user). If you don't need "
                "to ask, call log_expense directly."
            ),
        })

    # FX + trip routing FIRST (same rules as log_expense). During an active
    # trip a YouTrip / FX-converted spend is a pot spend — its home
    # category matters little, so we skip the ask-prompt and log it
    # straight into the trip category with `[bucket:misc]` via the normal
    # log_expense path (normal bubble). Nothing is written on FX failure.
    if (currency or "").strip().upper() != "SGD" and "orig:" not in (notes or "").lower():
        fx = _fx_to_sgd(amount, currency, date)
        if not fx.get("ok"):
            return _fx_failed_result(currency, amount, fx.get("error", ""))
        amount = fx["amount"]
        currency = "SGD"
        notes = f"{notes}; {fx['note']}" if notes else fx["note"]

    try:
        from tools import travel_mode
        routing = travel_mode.route_for_trip(
            txn_date=date,
            proposed_category=_PENDING_CATEGORY,
            payment_method=payment_method,
            notes=notes,
            currency=currency,
        )
    except Exception:
        routing = {"applied": False, "reason": "exception"}
    if routing.get("applied"):
        delegated = dict(args)
        delegated.pop("options", None)
        delegated.update({
            "amount": amount,
            "currency": currency,
            "date": date,
            "category": routing["category"],
            "notes": routing["notes"],
        })
        parsed = json.loads(handle_log_expense(delegated))
        if parsed.get("status") == "ok":
            parsed["trip_routed"] = {
                "trip_label": routing.get("trip_label", ""),
                "from_category": _PENDING_CATEGORY,
                "to_category": routing["category"],
                "bucket": routing.get("bucket"),
                "signal": routing.get("signal"),
            }
            parsed["pending_skipped"] = True
        return json.dumps(parsed)

    # Ensure the placeholder category exists so reports/budget calcs don't
    # fail on a missing-row lookup. Idempotent on both backends; the Sheet
    # side is best-effort (pre-result, so failures are silently cosmetic —
    # the mirror append below carries the observable state).
    supabase_client.ensure_category_exists(_PENDING_CATEGORY)

    result = supabase_client.append_transaction(
        date=date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        category=_PENDING_CATEGORY,
        source=source,
        payment_method=payment_method,
        notes=notes,
        txn_time=args.get("time", ""),
    )

    if result.get("status") == "duplicate":
        result["note"] = (
            "Duplicate transaction — already logged. Not sending another "
            "ask-prompt. If the existing row needs recategorising, "
            "edit_expense directly."
        )
        return json.dumps(result)

    txn_id = result.get("txn_id", "")

    if txn_id and os.environ.get("TELEGRAM_BOT_TOKEN"):
        prompt = _format_pending_prompt(merchant, amount, currency, options, txn_id)
        send_result = _send_telegram_bubble(prompt)
        if send_result.get("ok"):
            message_id = send_result["message_id"]
            link_result = _retry_link(txn_id, message_id)
            result["telegram_message_id"] = message_id
            result["bubble_sent"] = True
            result["linked"] = link_result.get("status") == "ok"
            result["pending"] = True
            result["assistant_reply_required"] = False
            result["assistant_reply_policy"] = (
                "STOP. The ask-prompt has already been delivered to the "
                "user via the Telegram Bot API. You MUST NOT emit any "
                "assistant text after this tool call — the user will reply "
                "to the prompt and a later turn will handle the category "
                "pick. Produce an empty response. THIS TURN ONLY: in later "
                "turns, direct user questions must always get a text "
                "answer — never carry this silence forward."
            )
            result["note"] = (
                "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply."
            )
        else:
            result["bubble_sent"] = False
            result["bubble_error"] = send_result.get("error", "")

    return json.dumps(result)


registry.register(
    name="log_expense_pending",
    toolset=TOOLSET,
    schema=LOG_EXPENSE_PENDING_SCHEMA,
    handler=handle_log_expense_pending,
    check_fn=_sheets_configured,
)


# --- update_budget ---

UPDATE_BUDGET_SCHEMA = {
    "name": "update_budget",
    "description": (
        "Update the monthly spending limit for a budget category. "
        "Use when the user wants to change how much they allocate to a category. "
        "Optionally specify a month (1-12) to update a specific month's budget."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "The budget category to update",
            },
            "monthly_limit": {
                "type": "number",
                "description": "New monthly spending limit in SGD",
            },
            "month": {
                "type": "integer",
                "description": "Month number (1-12). Defaults to current month. Use when user says 'set budget for June' etc.",
                "minimum": 1,
                "maximum": 12,
            },
        },
        "required": ["category", "monthly_limit"],
    },
}


def handle_update_budget(args: dict, **kwargs) -> str:
    category = args.get("category", "")
    monthly_limit = float(args.get("monthly_limit", 0))
    month = args.get("month")
    if month is not None:
        month = int(month)
    result = supabase_client.update_budget_row(category, monthly_limit, month_num=month)
    return json.dumps(result)


registry.register(
    name="update_budget",
    toolset=TOOLSET,
    schema=UPDATE_BUDGET_SCHEMA,
    handler=handle_update_budget,
    check_fn=_sheets_configured,
)


# --- get_remaining_budget ---

GET_BUDGET_SCHEMA = {
    "name": "get_remaining_budget",
    "description": (
        "Get the remaining budget for one or all categories for a given month. "
        "Shows limit, spent, remaining, percent used, and `kind` "
        "('fixed' monthly bills vs 'variable'). The `_attention.lines` "
        "entry is the precomputed warning list — variable categories at "
        "80%+ and fixed bills that came in OVER their usual amount. When "
        "asked to warn about budgets, print `_attention.lines` VERBATIM; "
        "never list a fixed bill just for being at 100% (that is its "
        "normal state) and never re-derive warnings from the rows yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Specific category to check. Omit for all categories.",
            },
            "month": {
                "type": "string",
                "description": "Month in YYYY-MM format. Defaults to current month.",
            },
        },
        "required": [],
    },
}


def handle_get_remaining_budget(args: dict, **kwargs) -> str:
    category = args.get("category")
    month = args.get("month")
    summary = supabase_client.get_spending_summary(month)
    if category:
        if category in summary:
            return json.dumps({category: summary[category]})
        return json.dumps({"error": f"Category '{category}' not found"})
    return json.dumps(summary)


registry.register(
    name="get_remaining_budget",
    toolset=TOOLSET,
    schema=GET_BUDGET_SCHEMA,
    handler=handle_get_remaining_budget,
    check_fn=_sheets_configured,
)


# --- edit_expense ---

EDIT_EXPENSE_SCHEMA = {
    "name": "edit_expense",
    "description": (
        "Edit an existing logged expense. Prefer locating the row by `txn_id` "
        "(unambiguous). If `txn_id` is not known, falls back to merchant + amount "
        "(+ optional date) — used for rows that pre-date the schema migration. "
        "Use when the user wants to correct a miscategorized transaction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": (
                    "Canonical transaction ID (format `txn_<YYYYMMDD>_<NNN>`). "
                    "Returned by `log_expense` and by `get_transaction_by_message_id`. "
                    "Always prefer this over merchant+amount."
                ),
            },
            "merchant": {
                "type": "string",
                "description": (
                    "Merchant name of the transaction to edit. "
                    "Only used when `txn_id` is not provided."
                ),
            },
            "amount": {
                "type": "number",
                "description": (
                    "Amount of the transaction to edit. "
                    "Only used when `txn_id` is not provided."
                ),
            },
            "date": {
                "type": "string",
                "description": "Date of the transaction (YYYY-MM-DD) to narrow the merchant+amount search. Optional.",
            },
            "new_category": {
                "type": "string",
                "description": (
                    "New category to assign — the PLAIN category name copied "
                    "exactly from the Budget sheet, never decorated with "
                    "emojis. Unknown names are refused with "
                    "status=unknown_category (plus `closest` suggestions) "
                    "and nothing is edited."
                ),
            },
            "create_category": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true ONLY when the user themselves asked for a "
                    "brand-new budget category by that exact name. Never set "
                    "it on your own judgment — with ONE exception: the "
                    "reserved `Lending` category (loan flows) may always be "
                    "created this way; it is system-reserved, not "
                    "user-invented."
                ),
            },
            "new_notes": {
                "type": "string",
                "description": "New notes for the transaction",
            },
            "new_merchant": {
                "type": "string",
                "description": "New merchant name to replace the existing one (e.g. rename 'SHOPEE SINGAPORE MP' to 'Miso Litter - Shopee')",
            },
        },
        "required": [],
    },
}


def handle_edit_expense(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id") or None
    merchant = args.get("merchant", "")
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else 0.0
    date = args.get("date")

    if not txn_id and not merchant:
        return json.dumps({
            "status": "error",
            "message": "Provide either `txn_id` or `merchant` + `amount`",
        })

    updates = {}
    if "new_category" in args:
        updates["Category"] = args["new_category"]
    if "new_notes" in args:
        updates["Notes"] = args["new_notes"]
    if "new_merchant" in args:
        updates["Merchant"] = args["new_merchant"]

    result = supabase_client.edit_transaction(
        merchant, amount, date, updates, txn_id=txn_id,
        create_category=bool(args.get("create_category", False)),
    )
    return json.dumps(result)


registry.register(
    name="edit_expense",
    toolset=TOOLSET,
    schema=EDIT_EXPENSE_SCHEMA,
    handler=handle_edit_expense,
    check_fn=_sheets_configured,
)


# --- delete_expense ---

DELETE_EXPENSE_SCHEMA = {
    "name": "delete_expense",
    "description": (
        "Delete an existing logged expense from the Transactions sheet. "
        "Prefer locating the row by `txn_id`. Falls back to merchant + amount "
        "(+ optional date) for legacy rows. "
        "Use when the user wants to remove a duplicate or incorrect entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "Canonical transaction ID (preferred)",
            },
            "merchant": {
                "type": "string",
                "description": "Merchant name of the transaction to delete (used when txn_id is absent)",
            },
            "amount": {
                "type": "number",
                "description": "Amount of the transaction to delete (used when txn_id is absent)",
            },
            "date": {
                "type": "string",
                "description": "Date of the transaction (YYYY-MM-DD) to narrow the search. Optional.",
            },
        },
        "required": [],
    },
}


def handle_delete_expense(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id") or None
    merchant = args.get("merchant", "")
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else 0.0
    date = args.get("date")

    if not txn_id and not merchant:
        return json.dumps({
            "status": "error",
            "message": "Provide either `txn_id` or `merchant` + `amount`",
        })

    result = supabase_client.delete_transaction(
        merchant, amount, date, txn_id=txn_id
    )
    return json.dumps(result)


registry.register(
    name="delete_expense",
    toolset=TOOLSET,
    schema=DELETE_EXPENSE_SCHEMA,
    handler=handle_delete_expense,
    check_fn=_sheets_configured,
)


# --- link_telegram_message ---

LINK_TELEGRAM_MESSAGE_SCHEMA = {
    "name": "link_telegram_message",
    "description": (
        "Attach a Telegram message_id to an already-logged transaction. "
        "Normally unnecessary — log_expense links the bubble itself. Use only to repair a row whose link failed for a freshly "
        "logged expense — pass the `txn_id` returned by `log_expense` and the "
        "`message_id` of the bubble Telegram just sent. This is what makes "
        "reply-to-message edits possible later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "The txn_id returned by log_expense",
            },
            "telegram_message_id": {
                "type": "string",
                "description": "The Telegram message_id of the confirmation bubble",
            },
        },
        "required": ["txn_id", "telegram_message_id"],
    },
}


def handle_link_telegram_message(args: dict, **kwargs) -> str:
    txn_id = args.get("txn_id", "")
    telegram_message_id = str(args.get("telegram_message_id", ""))
    result = supabase_client.link_telegram_message(txn_id, telegram_message_id)
    return json.dumps(result)


registry.register(
    name="link_telegram_message",
    toolset=TOOLSET,
    schema=LINK_TELEGRAM_MESSAGE_SCHEMA,
    handler=handle_link_telegram_message,
    check_fn=_sheets_configured,
)


# --- get_transaction_by_message_id ---

GET_TXN_BY_MESSAGE_ID_SCHEMA = {
    "name": "get_transaction_by_message_id",
    "description": (
        "Look up a transaction by the Telegram message_id of the confirmation "
        "bubble that the bot originally sent for it. Use this when the user "
        "REPLIES to a logged transaction's confirmation message — the inbound "
        "payload's `reply_to_message_id` is what you pass here. Returns the "
        "transaction (including its `txn_id`) so you can pass that `txn_id` "
        "to `edit_expense` or `delete_expense`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "telegram_message_id": {
                "type": "string",
                "description": "The Telegram message_id from the user's reply_to_message_id field",
            },
        },
        "required": ["telegram_message_id"],
    },
}


def handle_get_transaction_by_message_id(args: dict, **kwargs) -> str:
    telegram_message_id = str(args.get("telegram_message_id", ""))
    result = supabase_client.find_transaction_by_message_id(telegram_message_id)
    if result is None:
        return json.dumps({
            "status": "not_found",
            "telegram_message_id": telegram_message_id,
        })
    row_num, row_dict = result
    return json.dumps({
        "status": "ok",
        "row": row_num,
        "transaction": row_dict,
    })


registry.register(
    name="get_transaction_by_message_id",
    toolset=TOOLSET,
    schema=GET_TXN_BY_MESSAGE_ID_SCHEMA,
    handler=handle_get_transaction_by_message_id,
    check_fn=_sheets_configured,
)


# --- lookup_merchant_category ---

LOOKUP_MERCHANT_CATEGORY_SCHEMA = {
    "name": "lookup_merchant_category",
    "description": (
        "Check the merchant_map table for a learned merchant → category mapping. "
        "Call this BEFORE asking the user to disambiguate a category — if a "
        "mapping exists, use it directly instead of asking. Returns the matching "
        "mapping (with `merchant_pattern` and `category`) or null."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": "string",
                "description": "The merchant string from the incoming transaction",
            },
        },
        "required": ["merchant"],
    },
}


def handle_lookup_merchant_category(args: dict, **kwargs) -> str:
    merchant = args.get("merchant", "")
    result = supabase_client.lookup_merchant_category(merchant)
    if result is None:
        return json.dumps({"status": "no_match", "merchant": merchant})
    return json.dumps({"status": "match", "merchant": merchant, "mapping": result})


registry.register(
    name="lookup_merchant_category",
    toolset=TOOLSET,
    schema=LOOKUP_MERCHANT_CATEGORY_SCHEMA,
    handler=handle_lookup_merchant_category,
    check_fn=_sheets_configured,
)


# --- learn_merchant_mapping ---

LEARN_MERCHANT_MAPPING_SCHEMA = {
    "name": "learn_merchant_mapping",
    "description": (
        "Persist a merchant → category mapping to the merchant_map table so future "
        "transactions matching this pattern get categorised automatically. Call "
        "this after the user corrects a categorisation (e.g. moves a Shopee "
        "transaction to Miso Litter) so you don't have to ask again next time. "
        "If a mapping with the same `merchant_pattern` already exists, its "
        "category is updated in place — no duplicates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "merchant_pattern": {
                "type": "string",
                "description": (
                    "Substring to match against future merchant strings, "
                    "case-insensitive. Be specific enough to avoid collisions: "
                    "prefer 'GRABFOOD' over 'GRAB' if you want to distinguish "
                    "ride-hailing from food delivery."
                ),
            },
            "category": {
                "type": "string",
                "description": "The category to assign to matching merchants",
            },
        },
        "required": ["merchant_pattern", "category"],
    },
}


def handle_learn_merchant_mapping(args: dict, **kwargs) -> str:
    merchant_pattern = args.get("merchant_pattern", "")
    category = args.get("category", "")
    result = supabase_client.add_merchant_mapping(merchant_pattern, category)
    return json.dumps(result)


registry.register(
    name="learn_merchant_mapping",
    toolset=TOOLSET,
    schema=LEARN_MERCHANT_MAPPING_SCHEMA,
    handler=handle_learn_merchant_mapping,
    check_fn=_sheets_configured,
)


# --- detect_subscription_creep ---

DETECT_SUBSCRIPTION_CREEP_SCHEMA = {
    "name": "detect_subscription_creep",
    "description": (
        "Scan recent transactions for recurring subscription patterns, "
        "grouped by CATEGORY (MerchantMap keeps a subscription's category "
        "stable even when its merchant string carries a per-month payment "
        "ref). Identifies categories billed once a month with consistent "
        "amounts, flags new subscriptions, price changes, and possibly "
        "cancelled ones. Each entry carries 'category' (the identity) and "
        "'last_merchant' (most recent charge's merchant string, for "
        "display). Excludes backfill rows. Use when the user asks about "
        "subscriptions, recurring charges, or monthly fixed costs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "months_back": {
                "type": "integer",
                "description": (
                    "How many months of history to analyse (default 3). "
                    "Needs at least 2 months of data to detect patterns."
                ),
                "default": 3,
                "minimum": 2,
                "maximum": 12,
            },
        },
        "required": [],
    },
}


def handle_detect_subscription_creep(args: dict, **kwargs) -> str:
    months_back = int(args.get("months_back", 3))
    result = supabase_client.detect_subscription_creep(months_back)
    return json.dumps(result)


registry.register(
    name="detect_subscription_creep",
    toolset=TOOLSET,
    schema=DETECT_SUBSCRIPTION_CREEP_SCHEMA,
    handler=handle_detect_subscription_creep,
    check_fn=_sheets_configured,
)


# --- undo_last_expense ---

UNDO_LAST_EXPENSE_SCHEMA = {
    "name": "undo_last_expense",
    "description": (
        "Delete the most recently logged transaction. Use when the user says "
        "'undo', 'oops', 'remove that last one', or '/undo'. Shows the "
        "transaction details before deleting so the user can confirm."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "confirm": {
                "type": "boolean",
                "description": (
                    "Set to true to actually delete. When false (or omitted), "
                    "returns the last transaction for preview without deleting."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}


def handle_undo_last_expense(args: dict, **kwargs) -> str:
    confirm = args.get("confirm", False)

    last = supabase_client.get_last_transaction()
    if last is None:
        return json.dumps({
            "status": "error",
            "message": "No transactions found.",
        })

    row_num, txn = last
    txn_id = txn.get("txn_id", "")

    if not confirm:
        return json.dumps({
            "status": "preview",
            "message": "This is the last transaction. Confirm to delete.",
            "transaction": txn,
            "row": row_num,
        })

    # Delete by txn_id if available, otherwise by merchant+amount lookup
    if txn_id:
        result = supabase_client.delete_transaction("", 0.0, txn_id=txn_id)
    else:
        merchant = txn.get("Merchant", "")
        amount = float(txn.get("Amount", 0))
        result = supabase_client.delete_transaction(merchant, amount)

    return json.dumps(result)


registry.register(
    name="undo_last_expense",
    toolset=TOOLSET,
    schema=UNDO_LAST_EXPENSE_SCHEMA,
    handler=handle_undo_last_expense,
    check_fn=_sheets_configured,
)


# --- generate_spending_report ---

GENERATE_SPENDING_REPORT_SCHEMA = {
    "name": "generate_spending_report",
    "description": (
        "Generate a detailed monthly spending report with category breakdown, "
        "top merchants, daily spending, and month-over-month comparison. "
        "Includes txn_ids for citation. Use when the user asks for a summary, "
        "report, or overview of their spending."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": (
                    "Month in YYYY-MM format. Defaults to current month."
                ),
            },
        },
        "required": [],
    },
}


def handle_generate_spending_report(args: dict, **kwargs) -> str:
    month = args.get("month")
    result = supabase_client.generate_spending_report(month)
    return json.dumps(result)


registry.register(
    name="generate_spending_report",
    toolset=TOOLSET,
    schema=GENERATE_SPENDING_REPORT_SCHEMA,
    handler=handle_generate_spending_report,
    check_fn=_sheets_configured,
)


# --- write_insight ---

WRITE_INSIGHT_SCHEMA = {
    "name": "write_insight",
    "description": (
        "Persist a derived insight to the insights table for future reference. "
        "Call this after generating a spending report or summary to save key "
        "findings (e.g. 'Dining overspent by 22% in week 14'). Future "
        "report runs read stored insights first, so summaries build on prior "
        "conclusions instead of recomputing from scratch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "insight": {
                "type": "string",
                "description": "Short factual statement about spending patterns",
            },
            "category": {
                "type": "string",
                "description": (
                    "Insight category: 'spending', 'budget', 'subscription', "
                    "'trend', or 'general'. Defaults to 'general'."
                ),
                "default": "general",
            },
            "month": {
                "type": "string",
                "description": "Month the insight relates to (YYYY-MM). Defaults to current month.",
            },
        },
        "required": ["insight"],
    },
}


def handle_write_insight(args: dict, **kwargs) -> str:
    insight = args.get("insight", "")
    category = args.get("category", "general")
    month = args.get("month")
    result = supabase_client.write_insight(insight, category, month)
    return json.dumps(result)


registry.register(
    name="write_insight",
    toolset=TOOLSET,
    schema=WRITE_INSIGHT_SCHEMA,
    handler=handle_write_insight,
    check_fn=_sheets_configured,
)


# --- get_insights ---

GET_INSIGHTS_SCHEMA = {
    "name": "get_insights",
    "description": (
        "Read stored insights from the insights table. Call this BEFORE "
        "generating a new spending report to build on prior conclusions. "
        "Filter by month and/or category. Returns most recent first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Filter by month (YYYY-MM). Omit for all months.",
            },
            "category": {
                "type": "string",
                "description": "Filter by insight category. Omit for all categories.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of insights to return (default 20).",
                "default": 20,
            },
        },
        "required": [],
    },
}


def handle_get_insights(args: dict, **kwargs) -> str:
    month = args.get("month")
    category = args.get("category")
    limit = int(args.get("limit", 20))
    result = supabase_client.get_insights(month, category, limit)
    return json.dumps(result)


registry.register(
    name="get_insights",
    toolset=TOOLSET,
    schema=GET_INSIGHTS_SCHEMA,
    handler=handle_get_insights,
    check_fn=_sheets_configured,
)


# --- generate_daily_insight ---

GENERATE_DAILY_INSIGHT_SCHEMA = {
    "name": "generate_daily_insight",
    "description": (
        "Cron-maintenance tool: deterministically computes ONE noteworthy "
        "spending insight for the day (month-over-month surge or quiet win, "
        "new-merchant habit, biggest day, or budget pace) and writes the "
        "Insights row ITSELF — do not follow it with write_insight. Call it "
        "at most once per run; it returns status 'exists' if today's auto "
        "insight was already written and 'no_match' if everything noteworthy "
        "was already said recently. It sends no Telegram message — if it "
        "returns status 'ok' you may cite its insight text in the summary "
        "you were asked to produce; on any other status just move on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month to analyse (YYYY-MM). Defaults to the current month.",
            },
        },
        "required": [],
    },
}


def handle_generate_daily_insight(args: dict, **kwargs) -> str:
    month = args.get("month") or None   # ""→None; LLMs send empty strings
    result = supabase_client.generate_daily_insight(month=month)
    return json.dumps(result)


registry.register(
    name="generate_daily_insight",
    toolset=TOOLSET,
    schema=GENERATE_DAILY_INSIGHT_SCHEMA,
    handler=handle_generate_daily_insight,
    check_fn=_sheets_configured,
)


# --- render_budget_chart ---

def _create_quickchart_url(config: dict, width: int = 700, height: int = 420) -> dict:
    """POST chart config to QuickChart.io and get back a short image URL.

    Using POST avoids URL-length concerns and returns a permanent short
    URL that Telegram can fetch server-side via sendPhoto. On any
    network/HTTP failure, returns {"ok": False, "error": ...} and the
    caller decides whether to surface or retry.
    """
    url = "https://quickchart.io/chart/create"
    payload = json.dumps({
        "chart": config,
        "width": width,
        "height": height,
        "format": "png",
        "backgroundColor": "white",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("success") and data.get("url"):
                return {"ok": True, "url": data["url"]}
            return {"ok": False, "error": data.get("message", "QuickChart error")}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _build_budget_chart_config(summary: dict, chart_type: str, month_label: str) -> dict:
    """Build a QuickChart config from get_spending_summary output.

    summary is {category: {"limit": float, "spent": float, "remaining": float,
    "percent_used": float}} with possibly "error" string entries for
    categories that failed. Filters to rows with a real limit or real
    spend, sorts by spent desc, takes top 10.
    """
    budgeted = []
    for cat, data in summary.items():
        if isinstance(data, dict):
            limit = float(data.get("limit", 0) or 0)
            spent = float(data.get("spent", 0) or 0)
            if limit > 0 or spent > 0:
                budgeted.append((cat, limit, spent))
    budgeted.sort(key=lambda row: row[2], reverse=True)
    top = budgeted[:10]
    labels = [row[0] for row in top]
    spent_vals = [row[2] for row in top]
    limit_vals = [row[1] for row in top]

    if chart_type == "donut":
        return {
            "type": "doughnut",
            "data": {
                "labels": labels,
                "datasets": [{
                    "data": spent_vals,
                    "backgroundColor": [
                        "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16",
                        "#22c55e", "#14b8a6", "#06b6d4", "#3b82f6", "#8b5cf6",
                    ],
                }],
            },
            "options": {
                "plugins": {
                    "title": {
                        "display": True,
                        "text": f"Spending by Category \u2014 {month_label}",
                        "font": {"size": 16},
                    },
                    "legend": {"position": "right"},
                },
            },
        }
    # Default: horizontal bars, Spent vs Budget side-by-side
    return {
        "type": "horizontalBar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": "Spent",  "data": spent_vals, "backgroundColor": "#ef4444"},
                {"label": "Budget", "data": limit_vals, "backgroundColor": "#3b82f6"},
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": f"Budget vs Spent \u2014 {month_label}",
                "fontSize": 16,
            },
            "scales": {
                "xAxes": [{"ticks": {"beginAtZero": True}}],
            },
            "legend": {"position": "bottom"},
        },
    }


RENDER_BUDGET_CHART_SCHEMA = {
    "name": "render_budget_chart",
    "description": (
        "Render a budget chart (bars or donut) for the given month and "
        "deliver it as a photo to the user's Telegram. Use when the user "
        "asks for a visual snapshot, chart, or 'show me' their spending "
        "distribution or budget vs actual. The tool sends the photo "
        "automatically via the Bot API \u2014 do NOT send any message yourself after. "
        "When bubble_sent=true, emit an EMPTY assistant reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month in YYYY-MM format. Defaults to current month.",
            },
            "chart_type": {
                "type": "string",
                "enum": ["bars", "donut"],
                "description": (
                    "'bars' = horizontal bars with Spent vs Budget side-by-"
                    "side (use for budget vs actual comparisons). 'donut' = "
                    "share of spend by category (use for 'where did my money "
                    "go?'). Defaults to bars."
                ),
                "default": "bars",
            },
        },
        "required": [],
    },
}


def handle_render_budget_chart(args: dict, **kwargs) -> str:
    month = args.get("month")
    chart_type = args.get("chart_type", "bars")

    try:
        summary = supabase_client.get_spending_summary(month)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Could not read summary: {exc}",
        })

    month_label = month or datetime.now().strftime("%B %Y")
    config = _build_budget_chart_config(summary, chart_type, month_label)

    create_result = _create_quickchart_url(config)
    if not create_result.get("ok"):
        return json.dumps({
            "status": "error",
            "message": f"Chart rendering failed: {create_result.get('error')}",
        })
    chart_url = create_result["url"]

    # Compact caption — the chart has the detail, the caption has the totals
    total_spent = 0.0
    total_limit = 0.0
    for data in summary.values():
        if isinstance(data, dict):
            total_spent += float(data.get("spent", 0) or 0)
            total_limit += float(data.get("limit", 0) or 0)
    pct = (total_spent / total_limit * 100) if total_limit > 0 else 0.0
    caption = (
        f"\U0001f4ca Budget snapshot \u2014 {month_label}\n"
        f"Spent: SGD {total_spent:.2f}"
        + (f" / {total_limit:.2f} ({pct:.0f}%)" if total_limit > 0 else "")
    )

    send_result = _send_telegram_photo(chart_url, caption)
    if send_result.get("ok"):
        return json.dumps({
            "status": "ok",
            "bubble_sent": True,
            "chart_url": chart_url,
            "telegram_message_id": send_result["message_id"],
            "caption": caption,
            "assistant_reply_required": False,
            "assistant_reply_policy": (
                "STOP. The chart photo has been delivered to Telegram via "
                "sendPhoto. You MUST NOT emit any assistant text after this "
                "tool call unless the user asked a separate follow-up "
                "question alongside the chart request. Produce an empty "
                "response. THIS TURN ONLY: in later turns, direct user "
                "questions must always get a text answer — never carry "
                "this silence forward."
            ),
            "note": "DUPLICATE MESSAGE WARNING: bubble_sent=true. Emit EMPTY reply.",
        })
    return json.dumps({
        "status": "error",
        "chart_url": chart_url,
        "error": send_result.get("error"),
        "note": "sendPhoto failed; chart_url is still valid. Surface the error to the user.",
    })


registry.register(
    name="render_budget_chart",
    toolset=TOOLSET,
    schema=RENDER_BUDGET_CHART_SCHEMA,
    handler=handle_render_budget_chart,
    check_fn=_sheets_configured,
)


# --- append_journal_entry ---
#
# reply=journal experiment: when the user replies to the 9 PM cron summary
# (or any bot-initiated summary/report) with free-text that isn't an edit or
# delete command, capture that reply as a narrative journal entry keyed by
# date. Populates the `Journal` tab for later FTS5 indexing. Expected to be
# a light-touch experiment for 2 weeks — if the habit sticks, we wire up an
# Obsidian mirror later.

APPEND_JOURNAL_ENTRY_SCHEMA = {
    "name": "append_journal_entry",
    "description": (
        "Save a freeform user reply as a journal entry in the journal table. "
        "Call this ONLY when the user replies to the '💭 Journal:' prompt "
        "of a bot summary, or explicitly says to journal/note something "
        "('journal this', 'note for the diary'). A journal entry is a "
        "diary line — narrative, feelings, context ('was stressful today', "
        "'bought the $45 meal because a friend was visiting'). NEVER call it for: "
        "questions to the agent, budget/category/edit/delete instructions, "
        "category picks after log_expense_pending, forwarded bank alerts, "
        "or any message the user expects an ACTION from — those go through "
        "their own tools, and a wrong journal row pollutes long-term "
        "memory. When unsure, skip it; do not journal as a fallback. If "
        "the reply references specific txn_ids, pass them in "
        "`txn_ids_referenced` so the entry can be joined to transactions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply_text": {
                "type": "string",
                "description": "The user's reply verbatim (or lightly cleaned).",
            },
            "date": {
                "type": "string",
                "description": (
                    "Date of the journal entry in YYYY-MM-DD. Defaults to "
                    "today. Use the date the reply was written, not the "
                    "date any referenced transaction occurred."
                ),
            },
            "txn_ids_referenced": {
                "type": "string",
                "description": (
                    "Comma-separated txn_ids mentioned in or implied by "
                    "the reply (e.g. 'txn_20260422_003,txn_20260422_005'). "
                    "Optional. Leave empty if no transactions are "
                    "specifically referenced."
                ),
                "default": "",
            },
            "tags": {
                "type": "string",
                "description": (
                    "Optional short tags extracted from the reply, comma-"
                    "separated (e.g. 'sam,promo,regret'). Keep to 1-3. "
                    "Leave empty if nothing obvious stands out."
                ),
                "default": "",
            },
        },
        "required": ["reply_text"],
    },
}


def handle_append_journal_entry(args: dict, **kwargs) -> str:
    reply_text = args.get("reply_text", "").strip()
    if not reply_text:
        return json.dumps({
            "status": "error",
            "message": "reply_text is required and must be non-empty",
        })
    date = args.get("date") or datetime.now().strftime("%Y-%m-%d")
    txn_ids_referenced = args.get("txn_ids_referenced", "")
    tags = args.get("tags", "")
    result = supabase_client.write_journal_entry(
        reply_text=reply_text,
        date=date,
        txn_ids_referenced=txn_ids_referenced,
        tags=tags,
    )
    return json.dumps(result)


registry.register(
    name="append_journal_entry",
    toolset=TOOLSET,
    schema=APPEND_JOURNAL_ENTRY_SCHEMA,
    handler=handle_append_journal_entry,
    check_fn=_sheets_configured,
)


# --- get_journal_entries ---

GET_JOURNAL_ENTRIES_SCHEMA = {
    "name": "get_journal_entries",
    "description": (
        "Read journal entries from the journal table, most recent first. "
        "Filter by exact date (YYYY-MM-DD) or month (YYYY-MM). Use when "
        "generating summaries/reports that should reference the user's "
        "own narrative notes, or when the user asks 'what did I write on "
        "<date>?'. Returns an empty list if the journal table doesn't exist "
        "yet (pre-experiment state)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Exact date (YYYY-MM-DD). Omit for any date.",
            },
            "month": {
                "type": "string",
                "description": "Month (YYYY-MM). Omit for any month.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries to return (default 20).",
                "default": 20,
            },
        },
        "required": [],
    },
}


def handle_get_journal_entries(args: dict, **kwargs) -> str:
    date = args.get("date")
    month = args.get("month")
    limit = int(args.get("limit", 20))
    result = supabase_client.read_journal_entries(date=date, month=month, limit=limit)
    return json.dumps(result)


registry.register(
    name="get_journal_entries",
    toolset=TOOLSET,
    schema=GET_JOURNAL_ENTRIES_SCHEMA,
    handler=handle_get_journal_entries,
    check_fn=_sheets_configured,
)


# --- sweep_missed_transactions ---
#
# Recovery path for when the LLM API returns 529 / times out and the parsed
# bank email never reaches Transactions. The Apps Script audits every parsed
# email into the webhook_log table before firing the webhook, so this tool can
# diff WebhookLog against the ledger by idempotency_key and surface anything
# that was dropped.
#
# Design: docs/SWEEP-ARCHITECTURE.md. The weekly cron calls this with
# days_back=7 and stays silent if missed_count == 0.

SWEEP_MISSED_TRANSACTIONS_SCHEMA = {
    "name": "sweep_missed_transactions",
    "description": (
        "Compare the webhook_log table (every parsed bank email, written by "
        "the Apps Script before firing the webhook) against the Transactions "
        "tab to find entries that were parsed but never logged — typically "
        "because the LLM returned 529, timed out, or hit a rate limit. "
        "Returns the list of missed transactions for user review; the user "
        "then confirms which to log with log_expense (source='email'). "
        "Use when the user says 'check for missed transactions', 'sweep', "
        "'did any transactions get dropped?', or from the weekly sweep "
        "cron. Returns status='error' with a setup hint if the WebhookLog "
        "tab doesn't exist yet (the Apps Script audit log hasn't been "
        "deployed)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days_back": {
                "type": "integer",
                "description": (
                    "How many days of WebhookLog to scan (default 7). "
                    "Matches the cadence of the weekly sweep cron."
                ),
                "default": 7,
                "minimum": 1,
                "maximum": 90,
            },
        },
        "required": [],
    },
}


def handle_sweep_missed_transactions(args: dict, **kwargs) -> str:
    days_back = int(args.get("days_back", 7))
    # Flipped in PR 4 together with the Apps Script audit write: both sides
    # of the diff (webhook_log and transactions) now live in Supabase.
    result = supabase_client.sweep_missed_transactions(days_back=days_back)
    return json.dumps(result)


registry.register(
    name="sweep_missed_transactions",
    toolset=TOOLSET,
    schema=SWEEP_MISSED_TRANSACTIONS_SCHEMA,
    handler=handle_sweep_missed_transactions,
    check_fn=_sheets_configured,
)


# --- export_sheet_backup ---
#
# The nightly 3 AM cron calls this to rebuild the Sheet's Transactions and
# Budget tabs from Supabase (the ledger of record). This replaced the
# dual-write mirror: the Sheet is a read-only human view + the free-tier
# backup copy. Grid assembly happens here (the handler knows both backends'
# shapes); sheets_client.write_sheet_snapshot does the raw I/O.

EXPORT_SHEET_BACKUP_SCHEMA = {
    "name": "export_sheet_backup",
    "description": (
        "Rebuild the Google Sheet's Transactions and Budget tabs from the "
        "Supabase ledger — the nightly backup export. Invoked by the 3 AM "
        "cron; can also be run on demand if the user asks to 'refresh the "
        "sheet' or 'export to sheets'. Overwrites both tabs completely: "
        "hand edits to the Sheet do not survive, by design."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def _assemble_budget_grid(budget_rows: list[dict]) -> list[list]:
    """(category, 'YYYY-MM', limit) rows → the Sheet's per-month grid
    (Category | Jan..Dec) for the current year, categories sorted."""
    year_prefix = datetime.now().strftime("%Y-")
    by_cat: dict[str, dict[int, float]] = {}
    for r in budget_rows:
        month = r.get("month", "")
        if not month.startswith(year_prefix):
            continue
        try:
            month_num = int(month.split("-")[1])
        except (IndexError, ValueError):
            continue
        by_cat.setdefault(r["category"], {})[month_num] = r["limit_amount"]

    grid = [["Category", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]]
    for cat in sorted(by_cat):
        grid.append([cat] + [by_cat[cat].get(m, 0) for m in range(1, 13)])
    return grid


def handle_export_sheet_backup(args: dict, **kwargs) -> str:
    if not _sheet_export_configured():
        return json.dumps({
            "status": "setup_required",
            "message": _SHEET_EXPORT_SETUP_MSG,
        })
    try:
        txns = supabase_client.read_all_transaction_rows()
        budget_rows = supabase_client.read_all_budget_rows()
        transaction_rows = [
            [t.get("Date", ""), t.get("Merchant", ""), t.get("Amount", ""),
             t.get("Currency", ""), t.get("Category", ""), t.get("Source", ""),
             t.get("Payment Method", ""), t.get("Notes", ""),
             t.get("txn_id", ""), t.get("telegram_message_id", ""),
             t.get("idempotency_key", ""), t.get("Time", "")]
            for t in txns
        ]
        budget_grid = _assemble_budget_grid(budget_rows)
        result = sheets_client.write_sheet_snapshot(transaction_rows, budget_grid)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Sheet export failed: {exc}",
        })
    return json.dumps(result)


registry.register(
    name="export_sheet_backup",
    toolset=TOOLSET,
    schema=EXPORT_SHEET_BACKUP_SCHEMA,
    handler=handle_export_sheet_backup,
    check_fn=_sheets_configured,
)


# --- archive_year_snapshot ---
#
# Yearly cold storage: the Jan-1 cron freezes the year that just ended into
# an immutable `Archive-<year>` tab in the backup Sheet. Unlike the nightly
# export (which clears and rebuilds), archives are write-once —
# sheets_client.write_year_archive refuses to touch an existing tab and
# returns status="exists" instead. Row assembly mirrors
# handle_export_sheet_backup (SNAPSHOT_HEADER order, 12 columns incl. Time).

ARCHIVE_YEAR_SNAPSHOT_SCHEMA = {
    "name": "archive_year_snapshot",
    "description": (
        "Freeze one calendar year's transactions into an immutable "
        "'Archive-<year>' tab in the backup Google Sheet — yearly cold "
        "storage. Invoked by the Jan-1 cron for the year that just ended; "
        "call it mid-conversation only if the user explicitly asks to "
        "archive a year. Archives are write-once: if the tab already "
        "exists the tool returns status='exists' and changes NOTHING — "
        "never retry with a different year to force a write."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "year": {
                "type": "integer",
                "description": (
                    "Calendar year to archive, e.g. 2026. Omit to default "
                    "to the previous calendar year (the Jan-1 cron's "
                    "meaning: the year that just ended)."
                ),
            },
        },
        "required": [],
    },
}


def handle_archive_year_snapshot(args: dict, **kwargs) -> str:
    if not _sheet_export_configured():
        return json.dumps({
            "status": "setup_required",
            "message": _SHEET_EXPORT_SETUP_MSG,
        })
    try:
        # `or 0` first: the LLM sends "" / null for omitted params, and
        # int("") raises. 0 is falsy too, so both fall through to the
        # previous-calendar-year default (Jan-1 cron semantics).
        year = int(args.get("year") or 0) or (datetime.now().year - 1)
        txns = supabase_client.read_all_transaction_rows()
        transaction_rows = [
            [t.get("Date", ""), t.get("Merchant", ""), t.get("Amount", ""),
             t.get("Currency", ""), t.get("Category", ""), t.get("Source", ""),
             t.get("Payment Method", ""), t.get("Notes", ""),
             t.get("txn_id", ""), t.get("telegram_message_id", ""),
             t.get("idempotency_key", ""), t.get("Time", "")]
            for t in txns
            if str(t.get("Date", "")).startswith(str(year))
        ]
        result = sheets_client.write_year_archive(year, transaction_rows)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Year archive failed: {exc}",
        })
    return json.dumps(result)


registry.register(
    name="archive_year_snapshot",
    toolset=TOOLSET,
    schema=ARCHIVE_YEAR_SNAPSHOT_SCHEMA,
    handler=handle_archive_year_snapshot,
    check_fn=_sheets_configured,
)


# --- Card optimiser tools (5) ---
#
# All handlers delegate to tools.card_optimiser, which gates every call
# behind a Cards + CardStrategy setup check. When the tabs aren't populated,
# the tool returns {"status": "setup_required", "message": "..."} and the
# skill is expected to surface that message verbatim rather than fabricate
# card data. See skills/card-optimiser/SKILL.md.


GET_CARD_CAP_STATUS_SCHEMA = {
    "name": "get_card_cap_status",
    "description": (
        "Report the current cycle's spend vs cap for each (card, category) "
        "pair defined in the Cards + CardStrategy tabs. Use when the user "
        "asks 'how close am I to the DBS dining cap?' or 'where are my "
        "cards this cycle?'. Optional `card_id` to narrow to one card; "
        "optional `category` to narrow to one category. If the tool returns "
        "`status: \"setup_required\"`, surface the message in ONE short "
        "line and stop — the user needs to populate the Cards / "
        "CardStrategy tabs. Do NOT invent card data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card_id": {
                "type": "string",
                "description": "Limit the report to one card_id (e.g. 'dbs-altitude').",
            },
            "category": {
                "type": "string",
                "description": "Limit to one category (matches Budget tab name).",
            },
            "as_of": {
                "type": "string",
                "description": "Override the 'today' anchor (YYYY-MM-DD). Defaults to today.",
            },
        },
        "required": [],
    },
}


def handle_get_card_cap_status(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.get_card_cap_status(
        card_id=args.get("card_id"),
        category=args.get("category"),
        as_of=args.get("as_of"),
    ))


registry.register(
    name="get_card_cap_status",
    toolset=TOOLSET,
    schema=GET_CARD_CAP_STATUS_SCHEMA,
    handler=handle_get_card_cap_status,
    check_fn=_sheets_configured,
)


# --- recommend_card_for ---

RECOMMEND_CARD_FOR_SCHEMA = {
    "name": "recommend_card_for",
    "description": (
        "Recommend which card to use for a purchase in a given category. "
        "Primary if its cap still has room; fallback if already capped. "
        "Use when the user asks 'which card should I use for dinner?' or "
        "'what's the best card for $80 at Cold Storage?'. If the tool "
        "returns `status: \"setup_required\"`, surface the message and "
        "stop — do not guess."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Budget-tab category for the planned purchase.",
            },
            "amount": {
                "type": "number",
                "description": "Optional amount of the planned purchase (SGD).",
            },
            "as_of": {
                "type": "string",
                "description": "Override the 'today' anchor (YYYY-MM-DD).",
            },
        },
        "required": ["category"],
    },
}


def handle_recommend_card_for(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    amount_raw = args.get("amount")
    amount = float(amount_raw) if amount_raw is not None else None
    return json.dumps(card_optimiser.recommend_card_for(
        category=args.get("category", ""),
        amount=amount,
        as_of=args.get("as_of"),
    ))


registry.register(
    name="recommend_card_for",
    toolset=TOOLSET,
    schema=RECOMMEND_CARD_FOR_SCHEMA,
    handler=handle_recommend_card_for,
    check_fn=_sheets_configured,
)


# --- plan_month ---

PLAN_MONTH_SCHEMA = {
    "name": "plan_month",
    "description": (
        "Return the current CardStrategy plan (one row per category) plus "
        "cycle-to-date spend and status for each primary card. Called by "
        "the 1st-of-month briefing cron, and on demand when the user asks "
        "'what's my card plan?'. Also lazily reverts any expired promo "
        "overrides. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month label (YYYY-MM). Defaults to current.",
            },
        },
        "required": [],
    },
}


def handle_plan_month(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.plan_month(month=args.get("month")))


registry.register(
    name="plan_month",
    toolset=TOOLSET,
    schema=PLAN_MONTH_SCHEMA,
    handler=handle_plan_month,
    check_fn=_sheets_configured,
)


# --- review_card_efficiency ---

REVIEW_CARD_EFFICIENCY_SCHEMA = {
    "name": "review_card_efficiency",
    "description": (
        "Month-end scorecard: walk every transaction in `month`, compute "
        "the optimal card at the moment of the transaction (given cap state "
        "then), and diff against the card actually used. Returns total "
        "miles earned, total miles optimal, miles left on table, and a list "
        "of suboptimal transactions. Called by the 1st-of-month cron on "
        "the previous month, and on demand when the user asks for a "
        "scorecard. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Month to review (YYYY-MM). Defaults to current month.",
            },
        },
        "required": [],
    },
}


def handle_review_card_efficiency(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    return json.dumps(card_optimiser.review_card_efficiency(month=args.get("month")))


registry.register(
    name="review_card_efficiency",
    toolset=TOOLSET,
    schema=REVIEW_CARD_EFFICIENCY_SCHEMA,
    handler=handle_review_card_efficiency,
    check_fn=_sheets_configured,
)


# --- set_category_primary ---

SET_CATEGORY_PRIMARY_SCHEMA = {
    "name": "set_category_primary",
    "description": (
        "Write (or update) a CardStrategy row for `category`, pointing at "
        "`card_id` as primary. Use for manual promo overrides, e.g. the "
        "user says 'DBS is doing 10 mpd on dining until 2026-04-30, switch "
        "my dining primary to DBS Altitude until then'. If `until_date` is "
        "set, the prior row values are snapshotted into `notes` (prefixed "
        "with '||PREV:') and restored on the first `plan_month` call after "
        "the expiry date. If the tool returns `status: \"setup_required\"`, "
        "surface the message and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Budget-tab category (or '_default' for the catch-all row).",
            },
            "card_id": {
                "type": "string",
                "description": "New primary card_id (must exist in the Cards tab).",
            },
            "until_date": {
                "type": "string",
                "description": (
                    "YYYY-MM-DD expiry. Promo overrides auto-revert to the "
                    "prior config after this date. Omit for a permanent change."
                ),
            },
            "earn_rate": {
                "type": "number",
                "description": "Override primary_earn_rate. Omit to keep prior value.",
            },
            "cap": {
                "type": "number",
                "description": "Override primary_cap. Omit to keep prior value.",
            },
            "fallback_card_id": {
                "type": "string",
                "description": "Override fallback_card_id. Omit to keep prior value.",
            },
            "fallback_earn_rate": {
                "type": "number",
                "description": "Override fallback_earn_rate. Omit to keep prior value.",
            },
        },
        "required": ["category", "card_id"],
    },
}


def handle_set_category_primary(args: dict, **kwargs) -> str:
    from tools import card_optimiser

    def _opt_float(key):
        val = args.get(key)
        if val is None or val == "":
            return None
        return float(val)

    return json.dumps(card_optimiser.set_category_primary(
        category=args.get("category", ""),
        card_id=args.get("card_id", ""),
        until_date=args.get("until_date") or None,
        earn_rate=_opt_float("earn_rate"),
        cap=_opt_float("cap"),
        fallback_card_id=args.get("fallback_card_id") or None,
        fallback_earn_rate=_opt_float("fallback_earn_rate"),
    ))


registry.register(
    name="set_category_primary",
    toolset=TOOLSET,
    schema=SET_CATEGORY_PRIMARY_SCHEMA,
    handler=handle_set_category_primary,
    check_fn=_sheets_configured,
)


# --- get_bonus_pool_status ---

GET_BONUS_POOL_STATUS_SCHEMA = {
    "name": "get_bonus_pool_status",
    "description": (
        "Calendar-month bonus-cap pool tracker for cards with a bonus_cap. "
        "UOB Preferred reports TWO pools (contactless / online, S$600 "
        "each, attributed by merchant class); other capped cards (e.g. "
        "HSBC Revolution S$1,000) report one. Call when the user asks how "
        "much bonus-cap headroom a card has this month ('how's my "
        "contactless cap?', 'how much left on Revo?'). Statuses per pool: "
        "ok <80%, warning 80-99%, capped >=100%. Numbers are approximate "
        "near month-end (banks cap by posting date, we track transaction "
        "date)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Calendar month YYYY-MM. Defaults to the current month.",
            },
        },
        "required": [],
    },
}


def handle_get_bonus_pool_status(args: dict, **kwargs) -> str:
    from tools import card_optimiser   # lazy on purpose — circular import (M10)
    month = args.get("month") or None  # ""→None; LLMs send empty strings
    return json.dumps(card_optimiser.get_bonus_pool_status(month=month))


registry.register(
    name="get_bonus_pool_status",
    toolset=TOOLSET,
    schema=GET_BONUS_POOL_STATUS_SCHEMA,
    handler=handle_get_bonus_pool_status,
    check_fn=_sheets_configured,
)


# --- Travel-mode tools (3) ---
#
# Routing during trips: the user maintains a `TravelMode` tab with a row per
# trip (date range, the trip's main Budget category, per-bucket allocations).
# All three tools below delegate to tools.travel_mode. The skill checks
# `get_active_travel_mode` before categorising; replies route to
# `set_trip_bucket` for in-trip corrections; `get_trip_budget_status` shows
# per-bucket spend vs allocation.


GET_ACTIVE_TRAVEL_MODE_SCHEMA = {
    "name": "get_active_travel_mode",
    "description": (
        "Return the active travel_mode row (the trip whose date range "
        "contains today). Trip ROUTING happens INSIDE log_expense / "
        "log_expense_pending — do NOT call this before logging a spend, "
        "and do NOT pick the trip category or a [bucket:X] tag yourself. "
        "Use it only after a YouTrip top-up is logged (to decide which "
        "trip to link with link_topup_to_trip) or when the user asks about "
        "the active trip. Returns `active=false` when no trip covers the "
        "date. Optional `as_of` (YYYY-MM-DD) to query a specific date "
        "instead of today."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "as_of": {
                "type": "string",
                "description": "Override 'today' (YYYY-MM-DD). Omit for today.",
            },
        },
        "required": [],
    },
}


def handle_get_active_travel_mode(args: dict, **kwargs) -> str:
    from tools import travel_mode
    return json.dumps(travel_mode.get_active_travel_mode(as_of=args.get("as_of")))


registry.register(
    name="get_active_travel_mode",
    toolset=TOOLSET,
    schema=GET_ACTIVE_TRAVEL_MODE_SCHEMA,
    handler=handle_get_active_travel_mode,
    check_fn=_sheets_configured,
)


# --- get_trip_budget_status ---

GET_TRIP_BUDGET_STATUS_SCHEMA = {
    "name": "get_trip_budget_status",
    "description": (
        "Return per-bucket spent/budget for a trip plus the trip-envelope "
        "total. Defaults to the active trip; pass `trip_label` to query a "
        "specific past trip (use the `label` value from the TravelMode "
        "tab). Use when the user asks 'how am I tracking on this trip?', "
        "'where am I on the food budget?', or in the daily 9pm cron during "
        "active trip days. If the tool returns "
        "`status: \"no_travel_mode_tab\"` or `\"no_active_trip\"`, surface "
        "the message in one short line and stop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trip_label": {
                "type": "string",
                "description": (
                    "Specific trip label (e.g. 'ID Apr 2026'). Omit for "
                    "today's active trip."
                ),
            },
            "as_of": {
                "type": "string",
                "description": "Override 'today' (YYYY-MM-DD). Omit for today.",
            },
        },
        "required": [],
    },
}


def handle_get_trip_budget_status(args: dict, **kwargs) -> str:
    from tools import travel_mode
    return json.dumps(travel_mode.get_trip_budget_status(
        trip_label=args.get("trip_label"),
        as_of=args.get("as_of"),
    ))


registry.register(
    name="get_trip_budget_status",
    toolset=TOOLSET,
    schema=GET_TRIP_BUDGET_STATUS_SCHEMA,
    handler=handle_get_trip_budget_status,
    check_fn=_sheets_configured,
)


# --- set_trip_bucket ---

SET_TRIP_BUCKET_SCHEMA = {
    "name": "set_trip_bucket",
    "description": (
        "Update or insert the `[bucket:X]` prefix in a logged transaction's "
        "Notes column. Use ONLY when the user replies to a trip "
        "confirmation bubble correcting the bucket — e.g. 'that was "
        "activities, not food', or '#3 should be flight'. Leaves the "
        "Category column alone (the trip-level Budget category stays "
        "the same); only Notes is rewritten. After this call, the next "
        "trip-budget query reflects the new bucket."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "Canonical txn_id of the row to retag.",
            },
            "bucket": {
                "type": "string",
                "description": (
                    "New bucket name (food/transport/flight/activities/"
                    "misc/etc.). Lowercased and stamped as `[bucket:X]` "
                    "at the start of Notes."
                ),
            },
        },
        "required": ["txn_id", "bucket"],
    },
}


def handle_set_trip_bucket(args: dict, **kwargs) -> str:
    from tools import travel_mode
    return json.dumps(travel_mode.set_trip_bucket(
        txn_id=args.get("txn_id", ""),
        bucket=args.get("bucket", ""),
    ))


registry.register(
    name="set_trip_bucket",
    toolset=TOOLSET,
    schema=SET_TRIP_BUCKET_SCHEMA,
    handler=handle_set_trip_bucket,
    check_fn=_sheets_configured,
)


# --- create_trip ---

CREATE_TRIP_SCHEMA = {
    "name": "create_trip",
    "description": (
        "Insert a new trip row into the travel_mode table (label, date "
        "range, trip budget category, planned total_budget, optional "
        "per-bucket allocation). Use when the user confirms a new trip — "
        "typically from the YouTrip top-up flow ('new trip') or when they "
        "announce upcoming travel. ALWAYS preview the trip (label, dates, "
        "category, planned budget, buckets) and get an explicit user "
        "confirmation BEFORE calling — this writes money state. "
        "total_budget is the PLANNED envelope; actual funding is derived "
        "from [trip:<label>]-tagged YouTrip top-ups via "
        "link_topup_to_trip, never by editing total_budget. Returns "
        "status=exists (nothing written) when a trip with that label "
        "already exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": (
                    "Short unique trip label, e.g. 'ID Apr 2026'. This is "
                    "the key used by link_topup_to_trip and "
                    "get_trip_budget_status."
                ),
            },
            "start_date": {
                "type": "string",
                "description": "Trip start date, YYYY-MM-DD.",
            },
            "end_date": {
                "type": "string",
                "description": "Trip end date, YYYY-MM-DD (>= start_date).",
            },
            "trip_category": {
                "type": "string",
                "description": (
                    "The trip's Budget category, e.g. 'Travel - ID "
                    "2026-04'. All trip transactions land in this category."
                ),
            },
            "total_budget": {
                "type": "number",
                "description": (
                    "PLANNED trip budget in SGD. Defaults to 0. Funding "
                    "is tracked separately via tagged top-ups."
                ),
                "default": 0,
            },
            "budget_map": {
                "type": "string",
                "description": (
                    "Optional per-bucket allocation string, same format "
                    "as existing rows: 'food=450; transport=300; "
                    "flight=800; misc=450'."
                ),
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional freeform notes for the trip row.",
                "default": "",
            },
        },
        "required": ["label", "start_date", "end_date", "trip_category"],
    },
}


def handle_create_trip(args: dict, **kwargs) -> str:
    from tools import travel_mode   # lazy on purpose — circular import (M10)
    return json.dumps(travel_mode.create_trip(
        label=args.get("label", ""),
        start_date=args.get("start_date", ""),
        end_date=args.get("end_date", ""),
        trip_category=args.get("trip_category", ""),
        total_budget=float(args.get("total_budget") or 0),
        budget_map=args.get("budget_map", ""),
        notes=args.get("notes", ""),
    ))


registry.register(
    name="create_trip",
    toolset=TOOLSET,
    schema=CREATE_TRIP_SCHEMA,
    handler=handle_create_trip,
    check_fn=_sheets_configured,
)


# --- link_topup_to_trip ---

LINK_TOPUP_TO_TRIP_SCHEMA = {
    "name": "link_topup_to_trip",
    "description": (
        "Stamp a [trip:<label>] tag into a logged transaction's Notes, "
        "linking a YouTrip top-up to a travel_mode trip. The trip's "
        "funded pot is DERIVED as the sum of its tagged top-ups — this "
        "never edits the trip's total_budget. Use after logging a YouTrip "
        "top-up: when exactly one trip is active, link silently; "
        "otherwise ask the user which trip first (see the skill flow). "
        "An existing [trip:...] tag is replaced; [bucket:x] tags and "
        "orig: FX traces are preserved. Returns status=not_found with "
        "available_labels when the label doesn't match a trip — re-ask "
        "with those labels, never invent one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "txn_id": {
                "type": "string",
                "description": "Canonical txn_id of the top-up row to tag.",
            },
            "trip_label": {
                "type": "string",
                "description": (
                    "Label of the travel_mode trip to link (matched "
                    "case-insensitively; the row's canonical label is "
                    "what gets stamped)."
                ),
            },
        },
        "required": ["txn_id", "trip_label"],
    },
}


def handle_link_topup_to_trip(args: dict, **kwargs) -> str:
    from tools import travel_mode   # lazy on purpose — circular import (M10)
    return json.dumps(travel_mode.link_topup_to_trip(
        txn_id=args.get("txn_id", ""),
        trip_label=args.get("trip_label", ""),
    ))


registry.register(
    name="link_topup_to_trip",
    toolset=TOOLSET,
    schema=LINK_TOPUP_TO_TRIP_SCHEMA,
    handler=handle_link_topup_to_trip,
    check_fn=_sheets_configured,
)


# --- Loans (IOU) tools (4) ---
#
# Lending is NOT a budget: the `loans` table tracks IOUs until repaid, and
# repayment logs an offsetting NEGATIVE ledger txn (category "Lending") so
# monthly totals self-correct. Logic lives in tools/loans.py; handlers
# lazy-import it (M10 pattern, same as card/travel).


CREATE_LOAN_SCHEMA = {
    "name": "create_loan",
    "description": (
        "Record an IOU in the loans table. Use when the user says a "
        "transfer or payment was a loan ('that $50 PayLah to Sarah was a "
        "loan'). Pass txn_id when the outflow was logged as a ledger "
        "transaction — the tool then recategorises that txn to 'Lending' "
        "ITSELF; do NOT call edit_expense for it. Check `recategorised` "
        "in the result and be honest when it is false. Lending is NOT a "
        "budget — the loan sits open until the user reports repayment "
        "(mark_loan_repaid)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Who owes the money, e.g. 'Sarah'.",
            },
            "amount": {
                "type": "number",
                "description": "Amount lent in SGD. Must be > 0.",
            },
            "lent_date": {
                "type": "string",
                "description": "Date lent, YYYY-MM-DD. Defaults to today.",
            },
            "channel": {
                "type": "string",
                "description": (
                    "How the money went out (e.g. 'paylah', 'paynow', "
                    "'cash'). Defaults to 'paylah'. Used as the "
                    "payment_method on the repayment offset txn."
                ),
                "default": "paylah",
            },
            "txn_id": {
                "type": "string",
                "description": (
                    "Optional txn_id of the outflow ledger row this loan "
                    "corresponds to. Empty for cash loans with no logged "
                    "transaction."
                ),
                "default": "",
            },
            "notes": {
                "type": "string",
                "description": "Optional freeform notes.",
                "default": "",
            },
        },
        "required": ["person", "amount"],
    },
}


def handle_create_loan(args: dict, **kwargs) -> str:
    from tools import loans   # lazy on purpose — mirrors card/travel (M10)
    return json.dumps(loans.create_loan(
        person=args.get("person", ""),
        amount=float(args.get("amount", 0) or 0),
        lent_date=args.get("lent_date") or None,   # ""→None; LLMs send empty strings
        channel=args.get("channel") or "paylah",
        txn_id=args.get("txn_id", ""),
        notes=args.get("notes", ""),
    ))


registry.register(
    name="create_loan",
    toolset=TOOLSET,
    schema=CREATE_LOAN_SCHEMA,
    handler=handle_create_loan,
    check_fn=_sheets_configured,
)


# --- mark_loan_repaid ---

MARK_LOAN_REPAID_SCHEMA = {
    "name": "mark_loan_repaid",
    "description": (
        "Mark an open loan repaid and (by default) log an offsetting "
        "NEGATIVE ledger transaction (amount = -loan.amount, category "
        "'Lending', merchant 'Repayment — <person>') whose txn_id is "
        "stored on the loan. Identify the loan by loan_id, or by person — "
        "person matches the OLDEST open loan for that name, "
        "case-insensitively. This mutates money state: ALWAYS preview the "
        "repayment (loan + the offset txn it will log) and get user "
        "confirmation BEFORE calling. Returns status=not_found with the "
        "current open_loans list when nothing matches — show that list "
        "and ask which one. Set log_offset=false ONLY if the user "
        "explicitly says not to touch the ledger."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loan_id": {
                "type": "integer",
                "description": (
                    "The loans row id (from list_open_loans). Omit to "
                    "match by person instead."
                ),
            },
            "person": {
                "type": "string",
                "description": (
                    "Person name — matches their OLDEST open loan, "
                    "case-insensitively. Ignored when loan_id is given."
                ),
            },
            "repaid_date": {
                "type": "string",
                "description": "Repayment date, YYYY-MM-DD. Defaults to today.",
            },
            "log_offset": {
                "type": "boolean",
                "description": (
                    "Log the offsetting negative ledger txn (default "
                    "true). Only pass false when the user explicitly "
                    "asks to skip the ledger entry."
                ),
                "default": True,
            },
        },
        "required": [],
    },
}


def handle_mark_loan_repaid(args: dict, **kwargs) -> str:
    from tools import loans   # lazy on purpose — mirrors card/travel (M10)

    raw_id = args.get("loan_id")
    loan_id = int(raw_id) if raw_id not in (None, "") else None
    return json.dumps(loans.mark_loan_repaid(
        loan_id=loan_id,
        person=args.get("person") or None,          # ""→None
        repaid_date=args.get("repaid_date") or None,  # ""→None
        log_offset=bool(args.get("log_offset", True)),
    ))


registry.register(
    name="mark_loan_repaid",
    toolset=TOOLSET,
    schema=MARK_LOAN_REPAID_SCHEMA,
    handler=handle_mark_loan_repaid,
    check_fn=_sheets_configured,
)


# --- list_open_loans ---

LIST_OPEN_LOANS_SCHEMA = {
    "name": "list_open_loans",
    "description": (
        "List all open (unrepaid) loans, oldest first, with the total "
        "outstanding. Use when the user asks 'who owes me money?', "
        "before previewing mark_loan_repaid, and whenever a repayment "
        "reference is ambiguous. An empty list is status=ok with "
        "count=0 — nobody owes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def handle_list_open_loans(args: dict, **kwargs) -> str:
    from tools import loans   # lazy on purpose — mirrors card/travel (M10)
    return json.dumps(loans.list_open_loans())


registry.register(
    name="list_open_loans",
    toolset=TOOLSET,
    schema=LIST_OPEN_LOANS_SCHEMA,
    handler=handle_list_open_loans,
    check_fn=_sheets_configured,
)


# --- sweep_loan_offsets ---

SWEEP_LOAN_OFFSETS_SCHEMA = {
    "name": "sweep_loan_offsets",
    "description": (
        "Complete the ledger for loans marked repaid via the PWA's "
        "one-tap 'Paid back' button (the PWA flips the loan but cannot "
        "write ledger rows). Finds every repaid loan with an empty "
        "repay_txn_id, logs the missing negative offset txn (same shape "
        "as mark_loan_repaid's), and stamps the txn_id onto the loan. "
        "Idempotent — re-runs dedupe on the ledger's idempotency key. "
        "Called by the nightly cron; count=0 means nothing needed doing "
        "and the cron must say NOTHING about loans. Failed offsets land "
        "in `failed` and retry automatically the next night."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


def handle_sweep_loan_offsets(args: dict, **kwargs) -> str:
    from tools import loans   # lazy on purpose — mirrors card/travel (M10)
    return json.dumps(loans.sweep_loan_offsets())


registry.register(
    name="sweep_loan_offsets",
    toolset=TOOLSET,
    schema=SWEEP_LOAN_OFFSETS_SCHEMA,
    handler=handle_sweep_loan_offsets,
    check_fn=_sheets_configured,
)

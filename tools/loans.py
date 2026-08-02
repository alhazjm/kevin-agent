"""Loans (IOU) tracking — lending is money that comes back, not a budget.

Approved design: a `loans` Supabase table (migration 0005) tracks each IOU
until repaid. Repayment logs an offsetting NEGATIVE ledger transaction
(category "Lending") so monthly totals self-correct — Hadi chose the
offset-txn design explicitly over excluding Lending from reports. The
"Lending" budgets row auto-creates at $0 on first use; the PWA hides it
from budget bars by design.

Same layering as `card_optimiser` / `travel_mode`: pure logic here, raw row
I/O in `supabase_client` (`insert_loan`, `read_loans`, `update_loan`), tool
registration in `expense_sheets_tool.py` only (handlers lazy-import this
module — M10). Ledger mutations route through
`supabase_client.append_transaction` — never raw writes.

Ordering note in `mark_loan_repaid`: the offset txn is logged BEFORE the
loan row is patched. If the patch then fails and the user retries, the
ledger's idempotency key dedupes the second offset — the reverse order
would strand a repaid loan with no offset and a retry that finds no open
loan to match.

Two paths write the offset: `mark_loan_repaid` (user tells the agent) and
`sweep_loan_offsets` (nightly cron completes loans the PWA's one-tap
"Paid back" button flipped — the PWA cannot write ledger rows, its RLS is
read+update only by design). Both go through `_log_repayment_offset` so
the offset shape can never drift between them.
"""

from __future__ import annotations

from datetime import datetime

from tools import supabase_client

# The reserved category for loan outflows and repayment offsets. Auto-created
# at $0 on the first offset (create_category=True); the PWA hides it from
# budget bars by design.
LENDING_CATEGORY = "Lending"


def _as_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(value) -> str | None:
    """Normalise a YYYY-MM-DD-ish string to YYYY-MM-DD, or None if it
    doesn't parse. Loans only ever carry dates as strings."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime(
            "%Y-%m-%d")
    except ValueError:
        return None


def _loan_shape(rec: dict) -> dict:
    """Normalise a loans DB record into the tool-facing shape (floats for
    money, '' never None — supabase_client already maps None to '')."""
    return {
        "id": rec.get("id"),
        "person": str(rec.get("person", "")),
        "amount": _as_float(rec.get("amount")),
        "lent_date": str(rec.get("lent_date", "")),
        "channel": str(rec.get("channel", "")),
        "txn_id": str(rec.get("txn_id", "")),
        "status": str(rec.get("status", "")),
        "repaid_date": str(rec.get("repaid_date", "")),
        "repay_txn_id": str(rec.get("repay_txn_id", "")),
        "notes": str(rec.get("notes", "")),
    }


def _log_repayment_offset(loan: dict, offset_date: str) -> dict:
    """Log the offsetting NEGATIVE ledger txn for a repaid loan and return
    the raw `append_transaction` result. The single source of the offset
    shape — `mark_loan_repaid` and `sweep_loan_offsets` both call this, so
    the two paths can never drift (amount, merchant, notes and the derived
    idempotency key must stay byte-identical for the ledger's dedup to
    recognise a retry as a retry).

    Callers interpret the result: "ok"/"duplicate" carry the txn_id to
    stamp into `repay_txn_id` ("duplicate" = the offset already landed on
    an earlier attempt — reuse its txn_id, never fail); anything else is a
    refusal (e.g. "unknown_category") and the loan's ledger stays
    incomplete."""
    return supabase_client.append_transaction(
        date=offset_date,
        merchant=f"Repayment — {loan['person']}",
        amount=-loan["amount"],
        currency="SGD",
        category=LENDING_CATEGORY,
        source="manual",
        payment_method=loan["channel"],
        notes=f"repayment of loan #{loan['id']}",
        create_category=True,
    )


def create_loan(person: str, amount: float, lent_date: str | None = None,
                channel: str = "paylah", txn_id: str = "",
                notes: str = "") -> dict:
    """Record a new IOU. `txn_id` optionally links the outflow ledger row
    (the transfer that left the account); '' for cash loans with no bank
    alert. Returns {"status": "created", "loan": {...}, "recategorised":
    bool}.

    When a txn is linked, this ALSO recategorises it to "Lending" —
    atomically with loan creation, not as a separate skill step. The skill
    used to instruct a follow-up edit_expense call and the agent skipped
    it (2026-08-01: the Shopee-for-Dad loan stayed in Groceries, dinging
    the Groceries budget with money that comes back, while claiming
    "(Lending)" in the reply). Deterministic beats prompt discipline."""
    person = (person or "").strip()
    if not person:
        return {"status": "error", "message": "person is required"}

    amount = _as_float(amount)
    if amount <= 0:
        return {"status": "error",
                "message": "amount must be a positive number"}

    if lent_date:
        parsed = _parse_date(lent_date)
        if parsed is None:
            return {"status": "error",
                    "message": f"lent_date must be YYYY-MM-DD, got "
                               f"'{lent_date}'"}
        lent_date = parsed
    else:
        lent_date = datetime.now().strftime("%Y-%m-%d")

    rec = supabase_client.insert_loan(
        person=person,
        amount=amount,
        lent_date=lent_date,
        channel=str(channel or "paylah"),
        txn_id=str(txn_id or ""),
        notes=str(notes or ""),
    )

    # Move the linked outflow to the reserved Lending category.
    # create_category=True mirrors the repayment-offset path: "Lending"
    # auto-creates at $0 on very first use (the guard otherwise refuses —
    # exactly how the live edit_expense attempt failed pre-first-repayment).
    # Failure is NON-FATAL: the loan row already exists, and the skill
    # surfaces `recategorised: false` so the agent can retry.
    recategorised = False
    recat_error = ""
    if txn_id:
        try:
            edit = supabase_client.edit_transaction(
                merchant="", amount=0, txn_id=str(txn_id),
                updates={"Category": LENDING_CATEGORY},
                create_category=True)
            recategorised = edit.get("status") == "ok"
            if not recategorised:
                recat_error = str(edit.get("message") or edit.get("status", ""))
        except Exception as exc:   # the loan must survive a ledger hiccup
            recat_error = f"{exc}"

    result = {"status": "created", "loan": _loan_shape(rec),
              "recategorised": recategorised}
    if recat_error:
        result["recategorise_error"] = recat_error
    return result


def list_open_loans() -> dict:
    """All open loans, oldest first, with the outstanding total. An empty
    list is still status ok with count 0 — the skill says 'nobody owes you
    anything' rather than treating it as an error."""
    loans = [_loan_shape(r) for r in supabase_client.read_loans(status="open")]
    return {
        "status": "ok",
        "loans": loans,
        "count": len(loans),
        "total_outstanding": round(sum(l["amount"] for l in loans), 2),
    }


def mark_loan_repaid(loan_id: int | None = None, person: str | None = None,
                     repaid_date: str | None = None,
                     log_offset: bool = True) -> dict:
    """Flip a loan to repaid and (by default) log the offsetting negative
    ledger txn. Match by `loan_id`, or by `person` — case-insensitively,
    OLDEST open loan first (read_loans orders by lent_date then id).

    The offset txn: amount = -loan.amount, category "Lending"
    (create_category=True — auto-creates at $0 on first use), source
    "manual", merchant "Repayment — <person>", payment_method =
    loan.channel. Its txn_id lands in the loan's repay_txn_id. This is a
    money mutation — the skill previews and confirms BEFORE calling.

    No matching open loan → {"status": "not_found", "open_loans": [...]}.
    """
    if loan_id in (None, "") and not (person or "").strip():
        return {"status": "error",
                "message": "Provide loan_id or person to identify the loan"}

    open_loans = [_loan_shape(r)
                  for r in supabase_client.read_loans(status="open")]

    loan = None
    if loan_id not in (None, ""):
        loan = next((l for l in open_loans if l["id"] == loan_id), None)
    else:
        person_key = person.strip().lower()
        # read_loans returns oldest first — the first hit IS the oldest.
        loan = next((l for l in open_loans
                     if l["person"].strip().lower() == person_key), None)

    if loan is None:
        identifier = (f"loan_id={loan_id}" if loan_id not in (None, "")
                      else f"person='{person}'")
        return {
            "status": "not_found",
            "message": f"No open loan matching {identifier}.",
            "open_loans": open_loans,
        }

    if repaid_date:
        parsed = _parse_date(repaid_date)
        if parsed is None:
            return {"status": "error",
                    "message": f"repaid_date must be YYYY-MM-DD, got "
                               f"'{repaid_date}'"}
        repaid_date = parsed
    else:
        repaid_date = datetime.now().strftime("%Y-%m-%d")

    # Offset FIRST, patch second (see module docstring): a retry after a
    # failed patch dedupes on the ledger's idempotency key instead of
    # stranding a repaid loan without its offset.
    offset_txn_id = None
    if log_offset:
        offset = _log_repayment_offset(loan, repaid_date)
        # "duplicate" = the offset already landed on an earlier attempt —
        # reuse its txn_id rather than failing the repayment.
        if offset.get("status") in ("ok", "duplicate"):
            offset_txn_id = offset.get("txn_id") or None
        else:
            return {
                "status": "error",
                "message": (
                    "Offset transaction was not logged "
                    f"({offset.get('status')}): "
                    f"{offset.get('message', '')} — loan left open."
                ),
                "offset_result": offset,
            }

    updated = supabase_client.update_loan(loan["id"], {
        "status": "repaid",
        "repaid_date": repaid_date,
        "repay_txn_id": offset_txn_id or "",
    })
    return {
        "status": "updated",
        "loan": _loan_shape(updated),
        "offset_txn_id": offset_txn_id,
    }


def sweep_loan_offsets() -> dict:
    """Complete the ledger for loans repaid via the PWA's one-tap button.

    The PWA's "Paid back" button flips a loan to status="repaid" but
    cannot write ledger rows (RLS is read+update only by design — money
    writes stay agent-side), so a button-flipped loan sits with an empty
    repay_txn_id and no offsetting negative txn. The nightly cron calls
    this to finish the job: for every repaid loan missing its
    repay_txn_id, log the offset exactly the way mark_loan_repaid does
    (via _log_repayment_offset; date = the loan's repaid_date, or today
    when the PWA didn't stamp one) and store the resulting txn_id on the
    loan.

    Safe to re-run: the ledger's idempotency key dedupes the offset (a
    "duplicate" result reuses the existing txn_id), and loans whose
    repay_txn_id is already set are skipped entirely. A refused or
    failed offset (e.g. "unknown_category", a transient PostgREST error)
    skips that loan and leaves repay_txn_id empty so the NEXT night
    retries — it lands in `failed` for the cron to surface. Per-loan
    try/except keeps one bad loan from blocking the rest, and the outer
    try/except keeps a raise from ever reaching the cron flow (same
    two-layer shape as the nudge hooks).

    Returns {"status": "ok", "count": N, "completed": [{loan_id, person,
    amount, offset_txn_id}, ...], "failed": [{loan_id, person, amount,
    reason}, ...]} — count is len(completed) and 0 is still status ok
    (the cron says nothing about loans when there's nothing to say).
    """
    try:
        repaid = [_loan_shape(r)
                  for r in supabase_client.read_loans(status="repaid")]

        completed: list[dict] = []
        failed: list[dict] = []
        for loan in repaid:
            if loan["repay_txn_id"]:
                continue  # ledger already complete — nothing to do
            try:
                offset_date = (_parse_date(loan["repaid_date"])
                               or datetime.now().strftime("%Y-%m-%d"))
                offset = _log_repayment_offset(loan, offset_date)
                if offset.get("status") in ("ok", "duplicate"):
                    txn_id = offset.get("txn_id") or ""
                    supabase_client.update_loan(
                        loan["id"], {"repay_txn_id": txn_id})
                    completed.append({
                        "loan_id": loan["id"],
                        "person": loan["person"],
                        "amount": loan["amount"],
                        "offset_txn_id": txn_id,
                    })
                else:
                    failed.append({
                        "loan_id": loan["id"],
                        "person": loan["person"],
                        "amount": loan["amount"],
                        "reason": (f"{offset.get('status')}: "
                                   f"{offset.get('message', '')}"),
                    })
            except Exception as exc:  # one bad loan must not block the rest
                failed.append({
                    "loan_id": loan["id"],
                    "person": loan["person"],
                    "amount": loan["amount"],
                    "reason": f"exception: {exc}",
                })

        return {"status": "ok", "count": len(completed),
                "completed": completed, "failed": failed}
    except Exception as exc:  # cron path — a raise here would kill the review
        return {"status": "error",
                "message": f"sweep_loan_offsets failed: {exc}"}

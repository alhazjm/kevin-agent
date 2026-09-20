#!/usr/bin/env python3
"""Statement ↔ ledger reconciliation runner (repo-side, monthly ritual).

Usage (from the repo root, statements dropped into samples/ — untracked):

    python recon/recon.py samples/DBS-estatement.pdf samples/UOB-estatement.pdf

Parses each PDF with recon/statement_parsers.py (checksum-gated: a
statement that cannot prove its printed totals aborts the run), fetches
the ledger window from Supabase via PostgREST (stdlib urllib, the same
idiom as tools/supabase_client._request), and diffs:

  * matched          — amount within $0.01, date within ±3 days (post-date
                       lag), bidirectional uppercase merchant-substring
  * amount_mismatch  — same merchant + date window, different amount
  * statement_only   — on the statement, missing from the ledger (each gets
                       a ready-to-review SQL INSERT with source='backfill',
                       PRINTED, never executed)
  * ledger_only      — in the ledger, not on these statements (cash /
                       other-rail rows land here naturally — triage, don't
                       panic)

Backfill rows are INCLUDED in the ledger fetch — reconciliation is the
M14 exception: statement imports are exactly what we are checking against.

Credentials come from the repo-root .env (SUPABASE_URL /
SUPABASE_SERVICE_KEY) and are never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from recon.statement_parsers import (  # noqa: E402
    Statement,
    StatementRow,
    detect_bank,
    parse_dbs,
    parse_uob,
    pdf_to_text,
)

DATE_WINDOW_DAYS = 3  # statement post date lags the ledger txn date

_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


# --- env + PostgREST (mirrors tools/supabase_client._request) ---------------


def _load_env(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments, optional quotes.
    Existing environment always wins. Values are never printed."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _request(method: str, path: str, params: dict | None = None) -> object:
    """One PostgREST round trip with the house 0.5/1/2s transient-retry
    shape (copied idiom from tools/supabase_client, not a new invention).
    Raises RuntimeError on persistent failure — secrets never appear in
    the error text."""
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
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_STATUSES and attempt < 2:
                last_err = exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(
                f"supabase {method} {path}: HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                last_err = exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError(f"supabase {method} {path}: {exc}") from exc
    raise RuntimeError(f"supabase {method} {path}: {last_err}")  # defensive


def fetch_ledger(start_date: str, end_date: str) -> list[dict]:
    """All ledger transactions in [start_date, end_date], every source
    INCLUDING backfill (the M14 exception — recon checks statement imports
    too). Returns raw DB records (date/merchant/amount/source/...)."""
    rows = _request("GET", "transactions", params={
        "date": f"gte.{start_date}",
        "and": f"(date.lte.{end_date})",
        "order": "date.asc,id.asc",
    })
    return rows or []


# --- diff -------------------------------------------------------------------


def _norm_merchant(s: str) -> str:
    """Matching key: uppercase, © artifact and all punctuation collapsed to
    single spaces — so ledger '7-Eleven' meets statement '7 ELEVEN-TUAS
    LINK MRT' and the DBS mojibake never blocks a match."""
    return re.sub(r"[^A-Z0-9]+", " ", str(s).upper().replace("©", " ")).strip()


def _merchant_match(a: str, b: str) -> bool:
    """Bidirectional substring on normalised names; sub-3-char names must
    match exactly (same guard as sheets_client.find_transaction_row)."""
    na, nb = _norm_merchant(a), _norm_merchant(b)
    if not na or not nb:
        return False
    if len(na) < 3 or len(nb) < 3:
        return na == nb
    return na in nb or nb in na


def _days_apart(a: str, b: str) -> int:
    da = datetime.strptime(str(a)[:10], "%Y-%m-%d")
    db = datetime.strptime(str(b)[:10], "%Y-%m-%d")
    return abs((da - db).days)


def diff_rows(statement_rows: list[StatementRow], ledger_rows: list[dict],
              date_window_days: int = DATE_WINDOW_DAYS) -> dict:
    """Classify statement debits against ledger records.

    Two passes over unmatched rows, each ledger row consumed at most once:
      1. matched: merchant match + |date delta| ≤ window + amount within
         $0.01 (closest date wins, then smallest amount delta)
      2. amount_mismatch: merchant match + date window only
    Leftovers become statement_only / ledger_only.

    ``ledger_rows`` are DB-shaped dicts (date/merchant/amount/...);
    ``statement_rows`` are StatementRow instances (credits should already
    be filtered out by the caller — payments are not expenses).
    """
    remaining = list(range(len(ledger_rows)))
    matched: list[dict] = []
    mismatched: list[dict] = []
    statement_only: list[StatementRow] = []
    pending_second_pass: list[StatementRow] = []

    def _ledger_amount(rec: dict) -> float:
        try:
            return float(rec.get("amount", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _best(srow: StatementRow, require_amount: bool) -> int | None:
        best_idx, best_key = None, None
        for idx in remaining:
            rec = ledger_rows[idx]
            if not _merchant_match(srow.merchant, rec.get("merchant", "")):
                continue
            days = _days_apart(srow.date, rec.get("date", "1970-01-01"))
            if days > date_window_days:
                continue
            amt_delta = abs(_ledger_amount(rec) - srow.amount)
            if require_amount and amt_delta >= 0.01:
                continue
            key = (days, amt_delta)
            if best_key is None or key < best_key:
                best_idx, best_key = idx, key
        return best_idx

    for srow in statement_rows:
        idx = _best(srow, require_amount=True)
        if idx is None:
            pending_second_pass.append(srow)
            continue
        remaining.remove(idx)
        matched.append({"statement": srow, "ledger": ledger_rows[idx]})

    for srow in pending_second_pass:
        idx = _best(srow, require_amount=False)
        if idx is None:
            statement_only.append(srow)
            continue
        remaining.remove(idx)
        mismatched.append({"statement": srow, "ledger": ledger_rows[idx]})

    return {
        "matched": matched,
        "amount_mismatch": mismatched,
        "statement_only": statement_only,
        "ledger_only": [ledger_rows[i] for i in remaining],
    }


# --- report -----------------------------------------------------------------


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def sql_suggestion(row: StatementRow) -> str:
    """A ready-to-review INSERT for a statement_only row. source='backfill'
    (statement import — excluded from analytics per M14), category left as
    Miscellaneous for the reviewer to correct, txn_id minted by the DB's
    next_txn_id() so the frozen format is preserved. PRINTED ONLY — this
    script never executes writes."""
    pm = _sql_escape(f"{row.card_name} ****{row.card_last4}".strip())
    holder = f" ({row.cardholder})" if row.cardholder else ""
    notes = _sql_escape(f"recon: {row.bank} statement ****{row.card_last4}{holder}")
    return (
        f"-- {row.bank} ****{row.card_last4}{holder}: {row.date} "
        f"{row.merchant} {row.amount:.2f} — TODO review category\n"
        f"INSERT INTO transactions (txn_id, date, merchant, amount, currency,"
        f" category, source, payment_method, notes)\n"
        f"  VALUES (next_txn_id('{row.date}'), '{row.date}', "
        f"'{_sql_escape(row.merchant)}', {row.amount:.2f}, 'SGD', "
        f"'Miscellaneous', 'backfill', '{pm}', '{notes}');"
    )


def _fmt_srow(r: StatementRow) -> str:
    fx = f" ({r.foreign_currency} {r.foreign_amount})" if r.foreign_currency else ""
    return f"    {r.date}  {r.amount:>9.2f}  {r.merchant}{fx}"


def _fmt_lrow(rec: dict) -> str:
    try:
        amt = float(rec.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amt = 0.0
    return (f"    {rec.get('date', '?')}  {amt:>9.2f}  "
            f"{rec.get('merchant', '?')}  "
            f"[{rec.get('txn_id', '?')}, {rec.get('source', '?')}]")


def render_report(statements: list[Statement], diff: dict) -> str:
    """Human-readable report: per card section the matched count and the
    full statement_only / amount_mismatch lists, then the global
    ledger_only list, then the SQL suggestions block."""
    out: list[str] = []
    for stmt in statements:
        for section in stmt.sections:
            key = (section.bank, section.card_last4)

            def _mine(srow: StatementRow) -> bool:
                return (srow.bank, srow.card_last4) == key

            sec_matched = [p for p in diff["matched"] if _mine(p["statement"])]
            sec_mismatch = [p for p in diff["amount_mismatch"]
                            if _mine(p["statement"])]
            sec_only = [r for r in diff["statement_only"] if _mine(r)]
            debits = [r for r in section.rows if not r.is_credit]

            holder = f" — {section.cardholder}" if section.cardholder else ""
            out.append("")
            out.append(f"== {section.bank} {section.card_name} "
                       f"****{section.card_last4}{holder} ==")
            out.append(f"  statement debits: {len(debits)}  |  matched: "
                       f"{len(sec_matched)}  |  amount mismatches: "
                       f"{len(sec_mismatch)}  |  statement-only: "
                       f"{len(sec_only)}")
            out.append(f"  checksum OK against printed total "
                       f"{section.printed_total:.2f}")
            if sec_mismatch:
                out.append("  AMOUNT MISMATCH (same merchant + date window, "
                           "different amount):")
                for pair in sec_mismatch:
                    out.append(_fmt_srow(pair["statement"]) + "   <-> ledger:")
                    out.append(_fmt_lrow(pair["ledger"]))
            if sec_only:
                out.append("  STATEMENT ONLY (missing from ledger):")
                for r in sec_only:
                    out.append(_fmt_srow(r))

    out.append("")
    out.append(f"== LEDGER ONLY ({len(diff['ledger_only'])}) — in the ledger, "
               f"not on these statements ==")
    out.append("  (cash / other-rail / not-yet-posted rows land here "
               "naturally — triage, don't panic)")
    for rec in diff["ledger_only"]:
        out.append(_fmt_lrow(rec))

    if diff["statement_only"]:
        out.append("")
        out.append("== SQL SUGGESTIONS for statement-only rows "
                   "(review, then run by hand in Supabase Studio — "
                   "NEVER executed by this script) ==")
        for r in diff["statement_only"]:
            out.append("")
            out.append(sql_suggestion(r))
    return "\n".join(out)


# --- entrypoint -------------------------------------------------------------


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python recon/recon.py <statement.pdf> "
              "[<statement.pdf> ...]")
        return 2

    _load_env(REPO_ROOT / ".env")
    if not (os.environ.get("SUPABASE_URL")
            and os.environ.get("SUPABASE_SERVICE_KEY")):
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set (repo-root .env "
              "or environment) — aborting")
        return 1

    statements: list[Statement] = []
    for path in argv:
        text = pdf_to_text(path)
        bank = detect_bank(text)
        stmt = parse_dbs(text) if bank == "DBS" else parse_uob(text)
        n_rows = sum(len(s.rows) for s in stmt.sections)
        print(f"{path}: {bank} statement {stmt.statement_date}, "
              f"{len(stmt.sections)} card section(s), {n_rows} rows, "
              f"all checksums OK")
        statements.append(stmt)

    debits = [r for stmt in statements for s in stmt.sections
              for r in s.rows if not r.is_credit]
    if not debits:
        print("no debit rows parsed — nothing to reconcile")
        return 0

    dates = sorted(r.date for r in debits)
    pad = timedelta(days=DATE_WINDOW_DAYS)
    start = (datetime.strptime(dates[0], "%Y-%m-%d") - pad).strftime("%Y-%m-%d")
    end = (datetime.strptime(dates[-1], "%Y-%m-%d") + pad).strftime("%Y-%m-%d")

    ledger = fetch_ledger(start, end)
    print(f"ledger window {start}..{end}: {len(ledger)} transactions "
          f"(backfill included — recon is the M14 exception)")

    diff = diff_rows(debits, ledger)
    print(render_report(statements, diff))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

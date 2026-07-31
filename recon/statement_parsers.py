"""Deterministic DBS / UOB credit-card e-statement parsers.

Repo-side tooling for the monthly statement reconciliation ritual (see
docs/STATEMENT-RECON.md). Takes the TEXT layer of a bank PDF e-statement
(pypdf ``extract_text()`` per page, joined with newlines) and returns
structured card sections + transaction rows. PDF→text is a thin separate
function (``pdf_to_text``) so tests feed plain text and pypdf is only
imported when a real PDF is on the table.

THE CHECKSUM GATE IS NON-NEGOTIABLE. Every card section must reconcile:

    previous balance − payments/credits + new debits == printed total

(DBS prints ``TOTAL:`` per card; UOB prints ``SUB TOTAL`` per section and
``TOTAL BALANCE FOR <CARD>`` per card name; DBS additionally prints a
``GRAND TOTAL FOR ALL CARD ACCOUNTS``.) On any mismatch the parser raises
``ChecksumError`` naming the section and the delta — a parser that cannot
prove its sums outputs nothing. This is what catches a silently skipped or
misread row: the arithmetic fails loudly instead of the diff lying quietly.

Layout knowledge distilled from real July-2026 statements (all six card
sections' checksums verified against the printed totals):

DBS ("Statement of Account", one table for all cards):
  * No year on rows — inferred from the statement date, with Dec→Jan
    rollback (row month > statement month ⇒ previous year).
  * Three row shapes: one-liners ending in an amount; 3-line FX rows
    (dateline with NO amount, then ``<CURRENCY NAME> <foreign amt>``, then
    the SGD amount alone); 3-line payment blocks (dateline, ``REF NO:``,
    then ``<amt> CR``). The ONLY discriminator between a one-liner and the
    first line of a multi-line block is "does the dateline end in an
    amount" — the same merchant appears both ways in one statement.
  * FX marker is the full currency NAME ("MALAYSIAN RINGGIT",
    "U. S. DOLLAR" — dots and spaces), not an ISO code.
  * Instalment billing rows are digit soup ("001IL (36M) 2.68%PA+1%PF 36
    (23) 300.11") — the LAST decimal token is the amount, which the
    end-anchored regex delivers for free.
  * The text layer decodes apostrophes as ``©`` ("DBS Woman©s Platinum");
    merchant names are repaired here so downstream matching never sees it.
  * Page furniture (hotline/footer/points-summary/payment-coupon lines)
    interleaves mid-table and would false-match naive card-number regexes.
    Safe strategy: only parse between a ``CARD NO.:`` header and its
    ``TOTAL:``, and hard-stop at ``GRAND TOTAL FOR ALL CARD ACCOUNTS``.

UOB ("Credit Card(s) Statement", one section per card per cardholder):
  * Rows are 3 physical lines (POST+TRANS dateline, ``Ref No. :``, amount)
    or 4 for FX (extra ``<ISO code> <foreign amt>`` line). Payments/credits
    are a SINGLE line with the amount inline and a trailing `` CR``.
  * Rows are ordered by TRANS date; we use it (not the post date) since
    that is the day money moved and what the ledger carries.
  * Description and location are concatenated with NO separating space
    ("...NOODLESingapore", "...(CENTURYSG") — the tail is kept whole, never
    split. Diff-side matching is substring-based so the glued city is
    harmless.
  * Full-page reheaders (card name + ``(continued)`` + 6 column-header
    lines) repeat mid-table on every page and must be skipped.
  * Supplementary cards get their OWN section under the same card name;
    each row is tagged with its section's cardholder + last4 so the diff
    can attribute (e.g. ****-9753 → Sam → source=backfill downstream).
  * The PERSONAL LOAN section is formatted exactly like a card section; it
    is checksummed (parser health) but EXCLUDED from the returned sections.
  * ``SUB TOTAL <amt>`` has NO colon (DBS's per-card line is
    ``SUB-TOTAL:``) — the two regexes are deliberately different.
  * Hard stop at the ``End of Transaction Details`` divider; everything
    after (loan summary, rewards, payment advice) is ignored.

Zero third-party dependencies beyond pypdf, and pypdf only for real PDFs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Public data shapes -----------------------------------------------------


@dataclass
class StatementRow:
    """One statement transaction, normalised across banks.

    ``date`` is ISO YYYY-MM-DD (DBS txn date; UOB TRANS date). ``amount``
    is SGD. Credits (payments/refunds, trailing " CR" in print) carry
    ``is_credit=True`` and are checksum inputs, not expenses — the diff
    runner filters them out.
    """

    date: str
    merchant: str
    amount: float
    is_credit: bool = False
    bank: str = ""
    card_name: str = ""
    card_last4: str = ""
    cardholder: str = ""
    foreign_currency: str | None = None  # DBS: full name; UOB: ISO code
    foreign_amount: float | None = None
    ref_no: str | None = None  # UOB per-txn 23-digit ref; DBS has none


@dataclass
class CardSection:
    bank: str
    card_name: str
    card_last4: str
    cardholder: str = ""
    previous_balance: float = 0.0
    printed_total: float = 0.0  # DBS "TOTAL:"; UOB "SUB TOTAL"
    rows: list[StatementRow] = field(default_factory=list)


@dataclass
class Statement:
    bank: str
    statement_date: str  # ISO YYYY-MM-DD
    sections: list[CardSection] = field(default_factory=list)


class StatementParseError(ValueError):
    """The text does not look like the statement layout we know."""


class ChecksumError(StatementParseError):
    """Parsed rows do not reconcile against a printed total. Always names
    the section and the delta — this error is the product, not a bug."""


# --- Shared helpers ---------------------------------------------------------

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_AMOUNT = r"\d{1,3}(?:,\d{3})*\.\d{2}"


def _cents(amount_str: str) -> int:
    """Parse a printed amount (thousands commas allowed) to integer cents.
    All checksum arithmetic runs in cents so float drift can never turn a
    clean reconciliation into a phantom mismatch."""
    return int(round(float(amount_str.replace(",", "")) * 100))


def _infer_date(day: str, mon: str, stmt_year: int, stmt_month: int) -> str:
    """Rows carry no year. A statement spans ~one cycle, so: row month
    after the statement month ⇒ it belongs to the PREVIOUS year (the
    Dec-rows-on-a-Jan-statement rollback)."""
    m = _MONTHS[mon]
    year = stmt_year - 1 if m > stmt_month else stmt_year
    return f"{year:04d}-{m:02d}-{int(day):02d}"


def _clean_merchant(raw: str) -> str:
    """Collapse the fixed-width padding runs and repair the © mojibake
    (the DBS text layer decodes apostrophes as ©)."""
    return re.sub(r"\s+", " ", raw.replace("©", "'")).strip()


def pdf_to_text(path: str) -> str:
    """Extract the text layer of a PDF, pages joined with newlines.

    Kept deliberately thin so the parsers take plain text and tests never
    touch pypdf. Imported lazily: this module stays importable on machines
    without pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pypdf is not installed — install it with: "
            "py -V:3.11 -m pip install pypdf"
        ) from exc
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# --- DBS --------------------------------------------------------------------

_DBS_STMT_DATE = re.compile(
    r"^(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) \$",
    re.M,
)
_DBS_CARD_HEADER = re.compile(
    r"^(?P<name>.+?) CARD NO\.: (?P<num>[\d*]{4} [\d*]{4} [\d*]{4} [\d*]{4})$"
)
_DBS_ONELINE = re.compile(
    rf"^(\d{{2}}) ([A-Z]{{3}}) (.+?) ({_AMOUNT})( CR)?$"
)
_DBS_DATELINE = re.compile(r"^(\d{2}) ([A-Z]{3}) (.+)$")
_DBS_REF = re.compile(r"^REF NO: ?\S+$")
# Full currency NAME + foreign amount, e.g. "MALAYSIAN RINGGIT 14.18",
# "U. S. DOLLAR 21.80" (dots/spaces inside the name are real).
_DBS_CCY_LINE = re.compile(rf"^([A-Z][A-Z .]*[A-Z.]) ({_AMOUNT})$")
_AMOUNT_ONLY = re.compile(rf"^({_AMOUNT})( CR)?$")
_DBS_PREV = re.compile(rf"^PREVIOUS BALANCE ({_AMOUNT})$")
_DBS_SUBTOTAL = re.compile(rf"^SUB-TOTAL: ({_AMOUNT})$")
_DBS_TOTAL = re.compile(rf"^TOTAL: ({_AMOUNT})$")
_DBS_GRAND = re.compile(rf"^GRAND TOTAL FOR ALL CARD ACCOUNTS: ({_AMOUNT})$")
_DBS_NEWTXN = re.compile(r"^NEW TRANSACTIONS (.+)$")

# Page furniture that interleaves mid-table (page 1 footer + pages 2+
# header/footer). Anything matching these is dropped wherever it appears.
_DBS_FURNITURE = [
    re.compile(r"^Credit Cards$"),
    re.compile(r"^\s*Statement of Account$"),
    re.compile(r"^\d+ of \d+$"),
    re.compile(r"^PDS_"),
    re.compile(r"^DBS Cards P\.O\. Box"),
    re.compile(r"^Hotline:"),
    re.compile(r"^===== PAGE "),  # tolerate page markers from text dumps
]


def _is_dbs_furniture(line: str) -> bool:
    return any(rx.match(line) for rx in _DBS_FURNITURE)


def _close_section(section: CardSection, printed_cents: int) -> None:
    """The checksum gate: previous balance − credits + debits must equal
    the printed section total, to the cent. Raises ChecksumError naming
    the section and the delta otherwise."""
    debits = sum(int(round(r.amount * 100)) for r in section.rows
                 if not r.is_credit)
    credits = sum(int(round(r.amount * 100)) for r in section.rows
                  if r.is_credit)
    prev = int(round(section.previous_balance * 100))
    expected = prev - credits + debits
    if expected != printed_cents:
        delta = (expected - printed_cents) / 100
        raise ChecksumError(
            f"{section.bank} section '{section.card_name}' "
            f"(****{section.card_last4}): parsed rows sum to "
            f"{expected / 100:.2f} but the statement prints "
            f"{printed_cents / 100:.2f} — delta {delta:+.2f}. "
            f"A row was misread or skipped; refusing to output anything."
        )
    section.printed_total = printed_cents / 100


def parse_dbs(text: str) -> Statement:
    """Parse extracted DBS e-statement text. See module docstring for the
    layout contract. Raises StatementParseError / ChecksumError — never
    returns partial data."""
    m = _DBS_STMT_DATE.search(text)
    if not m:
        raise StatementParseError(
            "DBS: statement date not found (expected a 'DD Mon YYYY $...' "
            "summary line)"
        )
    stmt_year = int(m.group(3))
    stmt_month = _MONTHS[m.group(2).upper()]
    statement_date = f"{stmt_year:04d}-{stmt_month:02d}-{int(m.group(1)):02d}"

    stmt = Statement(bank="DBS", statement_date=statement_date)
    section: CardSection | None = None
    # pending multi-line block: {"date","merchant","state","fx_ccy","fx_amt"}
    pending: dict | None = None
    grand_cents: int | None = None
    section_total_cents = 0

    def _emit(date: str, merchant: str, amount_str: str, is_credit: bool,
              fx_ccy: str | None = None, fx_amt: float | None = None) -> None:
        section.rows.append(StatementRow(
            date=date,
            merchant=_clean_merchant(merchant),
            amount=_cents(amount_str) / 100,
            is_credit=is_credit,
            bank="DBS",
            card_name=section.card_name,
            card_last4=section.card_last4,
            cardholder=section.cardholder,
            foreign_currency=fx_ccy,
            foreign_amount=fx_amt,
        ))

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _is_dbs_furniture(line):
            continue

        g = _DBS_GRAND.match(line)
        if g:
            if section is not None or pending is not None:
                raise StatementParseError(
                    "DBS: GRAND TOTAL reached with an unclosed card section"
                )
            grand_cents = _cents(g.group(1))
            break  # hard stop — points summary / payment coupon follow

        header = _DBS_CARD_HEADER.match(line)
        if header:
            if section is not None:
                raise StatementParseError(
                    f"DBS: new card header '{header.group('name')}' before "
                    f"'{section.card_name}' printed its TOTAL:"
                )
            section = CardSection(
                bank="DBS",
                card_name=header.group("name").strip(),
                card_last4=header.group("num").split()[-1],
            )
            continue

        if section is None:
            continue  # address block, instalment summary table, etc.

        if pending is not None:
            state = pending["state"]
            if state == "after_date":
                if _DBS_REF.match(line):
                    pending["state"] = "after_ref"
                    continue
                ccy = _DBS_CCY_LINE.match(line)
                if ccy:
                    pending["fx_ccy"] = ccy.group(1)
                    pending["fx_amt"] = _cents(ccy.group(2)) / 100
                    pending["state"] = "after_ccy"
                    continue
                amt = _AMOUNT_ONLY.match(line)
                if amt:  # degenerate 2-line block: dateline then amount
                    _emit(pending["date"], pending["merchant"], amt.group(1),
                          bool(amt.group(2)))
                    pending = None
                    continue
                raise StatementParseError(
                    f"DBS: expected REF NO / currency / amount line after "
                    f"dateline for '{pending['merchant']}', got: {line!r}"
                )
            # after_ref (payment block) and after_ccy (FX block) both end
            # on an amount-only line.
            amt = _AMOUNT_ONLY.match(line)
            if not amt:
                raise StatementParseError(
                    f"DBS: expected the SGD amount line to close the block "
                    f"for '{pending['merchant']}', got: {line!r}"
                )
            _emit(pending["date"], pending["merchant"], amt.group(1),
                  bool(amt.group(2)),
                  pending.get("fx_ccy"), pending.get("fx_amt"))
            pending = None
            continue

        prev = _DBS_PREV.match(line)
        if prev:
            section.previous_balance = _cents(prev.group(1)) / 100
            continue
        newtxn = _DBS_NEWTXN.match(line)
        if newtxn:
            section.cardholder = newtxn.group(1).strip()
            continue
        if _DBS_SUBTOTAL.match(line):
            continue  # equal to TOTAL: on every observed statement
        total = _DBS_TOTAL.match(line)
        if total:
            printed = _cents(total.group(1))
            _close_section(section, printed)
            section_total_cents += printed
            stmt.sections.append(section)
            section = None
            continue

        one = _DBS_ONELINE.match(line)
        if one and one.group(2) in _MONTHS:
            _emit(_infer_date(one.group(1), one.group(2), stmt_year,
                              stmt_month),
                  one.group(3), one.group(4), bool(one.group(5)))
            continue
        dateline = _DBS_DATELINE.match(line)
        if dateline and dateline.group(2) in _MONTHS:
            pending = {
                "date": _infer_date(dateline.group(1), dateline.group(2),
                                    stmt_year, stmt_month),
                "merchant": dateline.group(3),
                "state": "after_date",
            }
            continue
        # Anything else inside a section is unknown furniture. Skipping is
        # safe: if it was actually a row, the section checksum fails loud.

    if not stmt.sections:
        raise StatementParseError("DBS: no card sections found")
    if grand_cents is None:
        raise StatementParseError(
            "DBS: 'GRAND TOTAL FOR ALL CARD ACCOUNTS' line not found"
        )
    if section_total_cents != grand_cents:
        raise ChecksumError(
            f"DBS grand total: card sections sum to "
            f"{section_total_cents / 100:.2f} but the statement prints "
            f"{grand_cents / 100:.2f} — delta "
            f"{(section_total_cents - grand_cents) / 100:+.2f}."
        )
    return stmt


# --- UOB --------------------------------------------------------------------

_UOB_STMT_DATE = re.compile(
    r"Statement Date\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{4})"
)
# Card-number + NAME ON CARD. Holder charset excludes digits on purpose so
# the page-1 summary / payment-advice lines (which trail amounts) can never
# false-match a section header.
_UOB_SECTION = re.compile(
    r"^(?P<num>[\d*]{4}-[\d*]{4}-[\d*]{4}-[\d*]{4}) "
    r"(?P<holder>[A-Z][A-Z .'\-]*[A-Z.])(?P<cont> \(continued\))?$"
)
# POST date, TRANS date, description(+glued location), optional inline
# amount (present ⇒ single-line payment/credit row).
_UOB_TXN_LINE = re.compile(
    rf"^(\d{{2}}) ([A-Z]{{3}}) (\d{{2}}) ([A-Z]{{3}}) (.+?)"
    rf"( ({_AMOUNT})( CR)?)?$"
)
_UOB_REF = re.compile(r"^Ref No\. : (\d+)$")
_UOB_FX_LINE = re.compile(rf"^([A-Z]{{3}}) ({_AMOUNT})$")
_UOB_PREV = re.compile(rf"^PREVIOUS BALANCE ({_AMOUNT})$")
_UOB_SUBTOTAL = re.compile(rf"^SUB TOTAL ({_AMOUNT})$")  # NO colon — real
_UOB_CARD_TOTAL = re.compile(rf"^TOTAL BALANCE FOR (.+?) ({_AMOUNT})$")
_UOB_END = re.compile(r"^\s*-+ End of Transaction Details -+\s*$")

# Furniture repeats on EVERY page, mid-table: page number, 3-line legal
# paragraph, 2 mojibake lines (caught by the non-ASCII test below), the
# bank address line, and the 6 column-header lines of each reheader.
_UOB_FURNITURE = [
    re.compile(r"^Page \d+ of \d+$"),
    re.compile(r"^Please note that you are bound by a duty"),
    re.compile(r"^omissions or unauthorised debits"),
    re.compile(r"^claim against the bank in relation thereto"),
    re.compile(r"^United Overseas Bank Limited"),
    re.compile(r"^Post$"),
    re.compile(r"^Date$"),
    re.compile(r"^Trans$"),
    re.compile(r"^Description of Transaction Transaction Amount$"),
    re.compile(r"^SGD$"),
    re.compile(r"^===== PAGE "),
]

_UOB_EXCLUDED_CARD_NAMES = {"PERSONAL LOAN"}


def _is_uob_furniture(line: str) -> bool:
    if any(rx.match(line) for rx in _UOB_FURNITURE):
        return True
    # The garbled Chinese-translation lines decode as non-ASCII soup; a
    # 30%-non-ASCII line is never a transaction row.
    if line and sum(ord(c) > 127 for c in line) > 0.3 * len(line):
        return True
    return False


def parse_uob(text: str) -> Statement:
    """Parse extracted UOB e-statement text. The PERSONAL LOAN section is
    checksummed but excluded from the returned sections. Raises
    StatementParseError / ChecksumError — never returns partial data."""
    m = _UOB_STMT_DATE.search(text)
    if not m:
        raise StatementParseError(
            "UOB: 'Statement Date DD MON YYYY' line not found"
        )
    stmt_year = int(m.group(3))
    stmt_month = _MONTHS[m.group(2)]
    statement_date = f"{stmt_year:04d}-{stmt_month:02d}-{int(m.group(1)):02d}"

    stmt = Statement(bank="UOB", statement_date=statement_date)
    section: CardSection | None = None
    pending: dict | None = None
    candidate_name = ""  # the bare card-name line preceding a section header
    # Per card name: sum of section SUB TOTALs, verified against the printed
    # "TOTAL BALANCE FOR <NAME>" line (covers principal + supplementary).
    name_subtotals: dict[str, int] = {}
    saw_end = False

    def _emit(date: str, merchant: str, amount_str: str, is_credit: bool,
              ref_no: str | None = None, fx_ccy: str | None = None,
              fx_amt: float | None = None) -> None:
        section.rows.append(StatementRow(
            date=date,
            merchant=_clean_merchant(merchant),
            amount=_cents(amount_str) / 100,
            is_credit=is_credit,
            bank="UOB",
            card_name=section.card_name,
            card_last4=section.card_last4,
            cardholder=section.cardholder,
            foreign_currency=fx_ccy,
            foreign_amount=fx_amt,
            ref_no=ref_no,
        ))

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or _is_uob_furniture(line):
            continue

        if _UOB_END.match(line):
            if section is not None or pending is not None:
                raise StatementParseError(
                    "UOB: 'End of Transaction Details' reached with an "
                    "unclosed section"
                )
            saw_end = True
            break  # loan summary / rewards / payment advice follow — ignore

        sec = _UOB_SECTION.match(line)
        if sec:
            if sec.group("cont"):
                # Mid-table reheader: the same section resumes.
                if section is None or pending is not None:
                    raise StatementParseError(
                        f"UOB: '(continued)' reheader in an unexpected "
                        f"place: {line!r}"
                    )
                continue
            if section is not None:
                raise StatementParseError(
                    f"UOB: new section header before "
                    f"'{section.card_name}' printed its SUB TOTAL: {line!r}"
                )
            section = CardSection(
                bank="UOB",
                card_name=candidate_name,
                card_last4=sec.group("num").split("-")[-1],
                cardholder=sec.group("holder").strip(),
            )
            continue

        if section is None:
            card_total = _UOB_CARD_TOTAL.match(line)
            if card_total:
                name = card_total.group(1).strip()
                printed = _cents(card_total.group(2))
                summed = name_subtotals.get(name)
                if summed is not None and summed != printed:
                    raise ChecksumError(
                        f"UOB card '{name}': section SUB TOTALs sum to "
                        f"{summed / 100:.2f} but 'TOTAL BALANCE FOR' prints "
                        f"{printed / 100:.2f} — delta "
                        f"{(summed - printed) / 100:+.2f}."
                    )
                continue
            # Anything else outside a section is the page-1 summary or the
            # bare card-name line that precedes a section header.
            candidate_name = line
            continue

        # --- inside a section ---
        if pending is not None:
            state = pending["state"]
            # A reheader's bare card-name line can interleave mid-block on
            # a page break; the (continued) line is caught by _UOB_SECTION
            # above only when pending is None, so guard both here.
            if line == section.card_name:
                continue
            cont = _UOB_SECTION.match(line)
            if cont and cont.group("cont"):
                continue
            if state == "after_ref":
                fx = _UOB_FX_LINE.match(line)
                if fx:
                    pending["fx_ccy"] = fx.group(1)
                    pending["fx_amt"] = _cents(fx.group(2)) / 100
                    pending["state"] = "after_fx"
                    continue
            if state == "after_date":
                ref = _UOB_REF.match(line)
                if ref:
                    pending["ref_no"] = ref.group(1)
                    pending["state"] = "after_ref"
                    continue
                raise StatementParseError(
                    f"UOB: expected 'Ref No. :' after dateline for "
                    f"'{pending['merchant']}', got: {line!r}"
                )
            amt = _AMOUNT_ONLY.match(line)
            if not amt:
                raise StatementParseError(
                    f"UOB: expected the SGD amount line to close the block "
                    f"for '{pending['merchant']}', got: {line!r}"
                )
            _emit(pending["date"], pending["merchant"], amt.group(1),
                  bool(amt.group(2)), pending.get("ref_no"),
                  pending.get("fx_ccy"), pending.get("fx_amt"))
            pending = None
            continue

        prev = _UOB_PREV.match(line)
        if prev:
            section.previous_balance = _cents(prev.group(1)) / 100
            continue
        sub = _UOB_SUBTOTAL.match(line)
        if sub:
            printed = _cents(sub.group(1))
            _close_section(section, printed)
            name_subtotals[section.card_name] = (
                name_subtotals.get(section.card_name, 0) + printed
            )
            if section.card_name not in _UOB_EXCLUDED_CARD_NAMES:
                stmt.sections.append(section)
            section = None
            continue

        txn = _UOB_TXN_LINE.match(line)
        if txn and txn.group(2) in _MONTHS and txn.group(4) in _MONTHS:
            # Use the TRANS date (groups 3+4), not the post date.
            date = _infer_date(txn.group(3), txn.group(4), stmt_year,
                               stmt_month)
            if txn.group(7):  # inline amount ⇒ single-line payment/credit
                _emit(date, txn.group(5), txn.group(7), bool(txn.group(8)))
            else:
                pending = {"date": date, "merchant": txn.group(5),
                           "state": "after_date"}
            continue
        # Unknown line inside a section (reheader card-name line, new
        # furniture variant): skip — a real missed row trips the checksum.

    if not stmt.sections:
        raise StatementParseError("UOB: no card sections found")
    if not saw_end:
        raise StatementParseError(
            "UOB: 'End of Transaction Details' divider not found"
        )
    return stmt


# --- Bank sniffing ----------------------------------------------------------


def detect_bank(text: str) -> str:
    """'DBS' or 'UOB', decided on layout anchors unique to each bank."""
    if "GRAND TOTAL FOR ALL CARD ACCOUNTS" in text or "CARD NO.:" in text:
        return "DBS"
    if _UOB_STMT_DATE.search(text) or "United Overseas Bank" in text:
        return "UOB"
    raise StatementParseError(
        "Could not identify the statement as DBS or UOB from its text layer"
    )


def parse_statement(text: str) -> Statement:
    """Sniff the bank and dispatch to the right parser."""
    bank = detect_bank(text)
    return parse_dbs(text) if bank == "DBS" else parse_uob(text)

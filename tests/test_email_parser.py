"""
Tests for DBS/UOB/HSBC/YouTrip-Shortcut email parsing logic.
Python port of the Apps Script regex patterns for local testability.
"""

import re
import pytest


def parse_dbs(body: str) -> dict | None:
    """Parse DBS PayLah! and Card transaction emails.

    Currency code is captured dynamically (any 3-letter ISO code), so
    foreign-currency cards (`Amount: IDR2334082.00`, `Amount: USD45.00`)
    parse cleanly. Conversion to SGD happens in the JS production code
    (`convertToSGD`); this Python mirror just exposes the original
    currency for regex parity testing.
    """
    amount_match = re.search(r"Amount:\s*([A-Z]{3})\s?([\d,]+\.?\d*)", body, re.IGNORECASE)
    merchant_match = re.search(r"To:\s*(.+)", body, re.IGNORECASE | re.MULTILINE)
    date_match = re.search(r"Date\s*&\s*Time:\s*(\d{1,2}\s+\w+)\s+", body, re.IGNORECASE)
    time_match = re.search(r"Date\s*&\s*Time:\s*\d{1,2}\s+\w+\s+(\d{1,2}:\d{2})", body, re.IGNORECASE)
    from_match = re.search(r"From:\s*(.+)", body, re.IGNORECASE | re.MULTILINE)

    if not amount_match or not merchant_match:
        return None

    from_text = from_match.group(1).strip() if from_match else ""
    is_paylah = "paylah" in from_text.lower()
    card_match = re.search(r"ending\s+(\d{4})", from_text, re.IGNORECASE)

    return {
        "bank": "DBS",
        "type": "paylah" if is_paylah else "card",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper(),
        "merchant": merchant_match.group(1).strip(),
        "date": date_match.group(1).strip() if date_match else None,
        "time": (("0" + time_match.group(1)) if time_match.group(1)[1] == ":"
                 else time_match.group(1)) if time_match else "",
        "card_last_four": card_match.group(1) if card_match else None,
        "payment_method": from_text,
    }


def parse_uob(body: str) -> dict | None:
    """Parse UOB credit card transaction emails.

    Same dynamic-currency-code approach as parse_dbs.
    """
    amount_match = re.search(
        r"transaction\s+of\s+([A-Z]{3})\s+([\d,]+\.?\d*)\s+was\s+made",
        body,
        re.IGNORECASE,
    )
    card_match = re.search(r"Card\s+ending\s+(\d{4})", body, re.IGNORECASE)
    date_match = re.search(r"on\s+(\d{2}/\d{2}/\d{2,4})", body, re.IGNORECASE)
    merchant_match = re.search(r"at\s+(.+?)(?:\.\s*If|\s*$)", body, re.IGNORECASE | re.MULTILINE)

    if not amount_match:
        return None

    merchant = merchant_match.group(1).strip() if merchant_match else "Unknown"
    merchant = re.sub(r"[.\s]+$", "", merchant)

    return {
        "bank": "UOB",
        "type": "card",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper(),
        "merchant": merchant,
        "date": date_match.group(1) if date_match else None,
        "card_last_four": card_match.group(1) if card_match else None,
    }


def parse_hsbc(body: str) -> dict | None:
    """Parse HSBC credit card transaction alert emails.

    Mirror of Code.gs::parseHSBC. The alert is an HTML table of label/value
    rows; getPlainBody() may flatten a row onto one line or split label and
    value across two, so every regex bridges the gap with \\s* (which
    matches newlines). Like parse_dbs, the date is exposed RAW for regex
    parity; the JS layer converts it via formatHSBCDate (see
    format_hsbc_date below).
    """
    amount_match = re.search(
        r"Transaction\s*Amount\s*:?\s*([A-Z]{3})\s*([\d,]+\.?\d*)", body, re.IGNORECASE)
    card_match = re.search(r"Card\s*Number\s*:?\s*[X*\-]+(\d{4})", body, re.IGNORECASE)
    date_match = re.search(
        r"Transaction\s*Date\s*:?\s*(\d{1,2}/[A-Za-z]{3}/\d{4})", body, re.IGNORECASE)
    time_match = re.search(
        r"Transaction\s*Time\s*:?\s*(\d{1,2}:\d{2}(?::\d{2})?)", body, re.IGNORECASE)
    merchant_match = re.search(r"Description\s*:?\s*(.+)", body, re.IGNORECASE | re.MULTILINE)

    if not amount_match or not merchant_match:
        return None

    merchant = re.sub(r"[-.\s]+$", "", merchant_match.group(1).strip())

    time_str = ""
    if time_match:
        time_str = time_match.group(1)
        if time_str[1] == ":":
            time_str = "0" + time_str

    return {
        "bank": "HSBC",
        "type": "card",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper(),
        "merchant": merchant,
        "date": date_match.group(1) if date_match else None,
        "time": time_str,
        "card_last_four": card_match.group(1) if card_match else None,
        "payment_method": "HSBC card" + (
            " ending " + card_match.group(1) if card_match else ""),
    }


_HSBC_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05",
    "JUN": "06", "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10",
    "NOV": "11", "DEC": "12",
}


def format_hsbc_date(date_str: str) -> str | None:
    """Mirror of Code.gs::formatHSBCDate: "29/JUL/2026" → "2026-07-29".

    Pure string arithmetic — no datetime round-trips (M5). The JS version
    falls back to today's date on malformed input; this mirror returns None
    there instead so tests stay deterministic.
    """
    parts = str(date_str).strip().split("/")
    if len(parts) != 3:
        return None
    day = "0" + parts[0] if len(parts[0]) == 1 else parts[0]
    month = _HSBC_MONTH_MAP.get(parts[1].upper())
    if not month or not re.fullmatch(r"\d{4}", parts[2]):
        return None
    return parts[2] + "-" + month + "-" + day


def parse_youtrip(body: str) -> dict | None:
    """Mirror of Code.gs::parseYouTrip — the self-sent iPhone-Shortcut
    source for YouTrip Apple Pay taps (YouTrip sends no emails itself).

    Pinned to the first live sample (2026-07-31): Shortcuts wraps the
    template's variable chips in braces, the Source value carries literal
    quotes, and Apple Pay truncates long merchant names. A bare "$"
    amount is SGD; an optional 3-letter code (expected overseas shape,
    unverified until the first foreign tap) is captured dynamically. An
    empty Merchant chip is a manual Shortcut test-run → None. The ts is
    sliced as strings (M5 — no datetime round-trips); like the other
    mirrors, the date falls back to None here (JS uses today) so tests
    stay deterministic.
    """
    merchant_match = re.search(r"Merchant:\s*\{([^}]*)\}", body, re.IGNORECASE)
    amount_match = re.search(
        r"Amount:\s*\{\s*(?:([A-Z]{3})\s*)?\$?\s*([\d,]+\.?\d*)\s*\}", body, re.IGNORECASE)
    ts_match = re.search(r"ts:\s*\{([^}]*)\}", body, re.IGNORECASE)

    if not amount_match or not merchant_match:
        return None

    merchant = merchant_match.group(1).strip()
    if not merchant:
        return None

    date_str = ""
    time_str = ""
    if ts_match:
        ts = ts_match.group(1).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts):
            date_str = ts[:10]
            time_str = ts[11:19]

    return {
        "bank": "YouTrip",
        "type": "applepay",
        "amount": float(amount_match.group(2).replace(",", "")),
        "currency": amount_match.group(1).upper() if amount_match.group(1) else "SGD",
        "merchant": merchant,
        "date": date_str or None,
        "time": time_str,
        "card_last_four": None,
        "payment_method": "YouTrip Card",
    }


class TestDBSPayLah:
    BODY = (
        "Transaction Ref: IPS10000000000000001\n"
        "Dear Sir / Madam,\n"
        "We refer to your PayLah! Scan & Pay Transfer dated 10 Apr.\n"
        "Date & Time: 10 Apr 13:10 (SGT)\n"
        "Amount: SGD25.00\n"
        "From: PayLah! Wallet (Mobile ending 9876)\n"
        "To: GREENFIELD COMMUNITY FUND\n"
    )

    def test_parses_amount(self):
        result = parse_dbs(self.BODY)
        assert result["amount"] == 25.00

    def test_parses_merchant(self):
        result = parse_dbs(self.BODY)
        assert result["merchant"] == "GREENFIELD COMMUNITY FUND"

    def test_identifies_paylah(self):
        result = parse_dbs(self.BODY)
        assert result["type"] == "paylah"

    def test_parses_date(self):
        result = parse_dbs(self.BODY)
        assert result["date"] == "10 Apr"

    def test_parses_mobile_ending(self):
        result = parse_dbs(self.BODY)
        assert result["card_last_four"] == "9876"


class TestDBSCard:
    BODY = (
        "Transaction Ref: SP100000000000000000001\n"
        "Dear Sir / Madam,\n"
        "We refer to your card transaction request dated 14/04/26.\n"
        "Date & Time: 14 APR 06:44 (SGT)\n"
        "Amount: SGD3.08\n"
        "From: DBS/POSB card ending 1234\n"
        "To: BUS/MRT\n"
    )

    def test_parses_amount(self):
        result = parse_dbs(self.BODY)
        assert result["amount"] == 3.08

    def test_parses_merchant(self):
        result = parse_dbs(self.BODY)
        assert result["merchant"] == "BUS/MRT"

    def test_identifies_card(self):
        result = parse_dbs(self.BODY)
        assert result["type"] == "card"

    def test_parses_card_ending(self):
        result = parse_dbs(self.BODY)
        assert result["card_last_four"] == "1234"

    def test_parses_payment_method(self):
        result = parse_dbs(self.BODY)
        assert "DBS/POSB card ending 1234" in result["payment_method"]


class TestUOB:
    BODY = (
        "A transaction of SGD 55.00 was made with your UOB Card ending 5678 "
        "on 12/04/26 at UrbanCompany. If unauthorised, call 24/7 Fraud Hotline now"
    )

    def test_parses_amount(self):
        result = parse_uob(self.BODY)
        assert result["amount"] == 55.00

    def test_parses_merchant(self):
        result = parse_uob(self.BODY)
        assert result["merchant"] == "UrbanCompany"

    def test_parses_date_two_digit_year(self):
        result = parse_uob(self.BODY)
        assert result["date"] == "12/04/26"

    def test_parses_card_ending(self):
        result = parse_uob(self.BODY)
        assert result["card_last_four"] == "5678"

    def test_no_match_returns_none(self):
        body = "Your UOB statement is ready for viewing"
        assert parse_uob(body) is None


class TestForeignCurrency:
    """Regression tests: pre-fix, these would all return None because the
    regexes hardcoded `SGD`. Post-fix, the currency code is captured and
    exposed as `currency` for the JS layer's `convertToSGD` to normalise."""

    def test_dbs_card_idr(self):
        # Real failure case from 2026-04-26: DBS card txn at Flyscoot.com
        # in IDR was silently dropped because the SGD-only regex didn't match.
        body = (
            "Transaction Ref: SP1300678880000000235123\n"
            "Dear Sir / Madam,\n"
            "We refer to your card transaction request dated 26/04/26.\n"
            "Date & Time: 26 APR 23:51 (SGT)\n"
            "Amount: IDR2334082.00\n"
            "From: DBS/POSB card ending 4321\n"
            "To: Flyscoot.com IDR\n"
        )
        result = parse_dbs(body)
        assert result is not None
        assert result["currency"] == "IDR"
        assert result["amount"] == 2334082.00
        assert result["merchant"] == "Flyscoot.com IDR"
        assert result["card_last_four"] == "4321"

    def test_dbs_card_usd(self):
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: USD45.00\n"
            "From: DBS/POSB card ending 1234\n"
            "To: AMAZON.COM\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "USD"
        assert result["amount"] == 45.00

    def test_dbs_card_eur_with_space(self):
        # Some bank emails put a space between the code and the amount.
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: EUR 18.50\n"
            "From: DBS/POSB card ending 1234\n"
            "To: SOMEPLACE PARIS\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "EUR"
        assert result["amount"] == 18.50

    def test_dbs_sgd_still_parses(self):
        # Regression guard for the common case.
        body = (
            "Date & Time: 14 APR 06:44 (SGT)\n"
            "Amount: SGD3.08\n"
            "From: DBS/POSB card ending 1234\n"
            "To: BUS/MRT\n"
        )
        result = parse_dbs(body)
        assert result["currency"] == "SGD"
        assert result["amount"] == 3.08

    def test_uob_eur(self):
        body = (
            "A transaction of EUR 18.50 was made with your UOB Card "
            "ending 5678 on 12/04/26 at PARIS HOTEL. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result is not None
        assert result["currency"] == "EUR"
        assert result["amount"] == 18.50
        assert result["merchant"] == "PARIS HOTEL"

    def test_uob_idr(self):
        body = (
            "A transaction of IDR 2334082.00 was made with your UOB Card "
            "ending 5678 on 26/04/26 at JAKARTA HOTEL. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result["currency"] == "IDR"
        assert result["amount"] == 2334082.00

    def test_uob_sgd_still_parses(self):
        body = (
            "A transaction of SGD 55.00 was made with your UOB Card "
            "ending 5678 on 12/04/26 at UrbanCompany. If unauthorised, "
            "call 24/7 Fraud Hotline now"
        )
        result = parse_uob(body)
        assert result["currency"] == "SGD"
        assert result["amount"] == 55.00


class TestHSBC:
    """First live sample 2026-07-29: SHENG SIONG on the Revolution (1357).
    The BODY mimics getPlainBody() flattening the alert's HTML table —
    each label and value on its own line."""

    BODY = (
        "Dear Customer\n"
        "Please note there was a transaction made on your HSBC credit card.\n"
        "Card Number\n"
        "XXXX-XXXX-XXXX-1357\n"
        "Transaction Date\n"
        "29/JUL/2026\n"
        "Transaction Time\n"
        "18:26:09\n"
        "Transaction Amount\n"
        "SGD8.50\n"
        "Description\n"
        "SHENG SIONG SUPERMARKET -\n"
        "You can also log on to the HSBC Singapore app to view your recent transactions.\n"
    )

    def test_parses_amount(self):
        result = parse_hsbc(self.BODY)
        assert result["amount"] == 8.50
        assert result["currency"] == "SGD"

    def test_parses_merchant_strips_trailing_dash(self):
        result = parse_hsbc(self.BODY)
        assert result["merchant"] == "SHENG SIONG SUPERMARKET"

    def test_parses_raw_date(self):
        result = parse_hsbc(self.BODY)
        assert result["date"] == "29/JUL/2026"

    def test_parses_time_with_seconds(self):
        result = parse_hsbc(self.BODY)
        assert result["time"] == "18:26:09"

    def test_parses_card_ending(self):
        result = parse_hsbc(self.BODY)
        assert result["card_last_four"] == "1357"

    def test_payment_method(self):
        result = parse_hsbc(self.BODY)
        assert result["payment_method"] == "HSBC card ending 1357"

    def test_bank_and_type(self):
        result = parse_hsbc(self.BODY)
        assert result["bank"] == "HSBC"
        assert result["type"] == "card"

    def test_inline_label_value_layout(self):
        # Same alert if getPlainBody() keeps each table row on one line.
        body = (
            "Card Number: XXXX-XXXX-XXXX-1357\n"
            "Transaction Date: 29/JUL/2026\n"
            "Transaction Time: 18:26:09\n"
            "Transaction Amount: SGD8.50\n"
            "Description: SHENG SIONG SUPERMARKET -\n"
        )
        result = parse_hsbc(body)
        assert result["amount"] == 8.50
        assert result["merchant"] == "SHENG SIONG SUPERMARKET"
        assert result["card_last_four"] == "1357"

    def test_foreign_currency(self):
        body = self.BODY.replace("SGD8.50", "USD12.99")
        result = parse_hsbc(body)
        assert result["currency"] == "USD"
        assert result["amount"] == 12.99

    def test_time_without_seconds_still_parses(self):
        body = self.BODY.replace("18:26:09", "18:26")
        assert parse_hsbc(body)["time"] == "18:26"

    def test_single_digit_hour_zero_padded(self):
        body = self.BODY.replace("18:26:09", "9:05:01")
        assert parse_hsbc(body)["time"] == "09:05:01"

    def test_internal_hyphen_preserved(self):
        body = self.BODY.replace("SHENG SIONG SUPERMARKET -", "7-ELEVEN -")
        assert parse_hsbc(body)["merchant"] == "7-ELEVEN"

    def test_no_match_returns_none(self):
        assert parse_hsbc("Your HSBC e-statement is ready for viewing") is None


class TestHSBCFormatDate:
    """Mirror of Code.gs::formatHSBCDate — string arithmetic only (M5)."""

    def test_converts_slash_month_name(self):
        assert format_hsbc_date("29/JUL/2026") == "2026-07-29"

    def test_pads_single_digit_day(self):
        assert format_hsbc_date("9/JUL/2026") == "2026-07-09"

    def test_all_months(self):
        months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        for i, mon in enumerate(months, start=1):
            assert format_hsbc_date(f"15/{mon}/2026") == f"2026-{i:02d}-15"

    def test_lowercase_month_accepted(self):
        assert format_hsbc_date("29/jul/2026") == "2026-07-29"

    def test_malformed_returns_none(self):
        # (JS falls back to today's date here; the mirror returns None.)
        assert format_hsbc_date("29-JUL-2026") is None
        assert format_hsbc_date("29/JULY/2026") is None
        assert format_hsbc_date("29/JUL/26") is None


class TestDBSTimeCapture:
    """Mirror of the Code.gs time regex (time-in-key fix, migration PR 4)."""

    def test_captures_zero_padded_time(self):
        body = ("Date & Time: 14 APR 06:44 (SGT)" + chr(10) +
                "Amount: SGD3.08" + chr(10) +
                "From: DBS/POSB card ending 1234" + chr(10) + "To: BUS/MRT")
        assert parse_dbs(body)["time"] == "06:44"

    def test_pads_single_digit_hour(self):
        body = ("Date & Time: 10 Apr 9:05 (SGT)" + chr(10) +
                "Amount: SGD25.00" + chr(10) +
                "From: PayLah! Wallet (Mobile ending 9876)" + chr(10) +
                "To: KOPI STALL")
        assert parse_dbs(body)["time"] == "09:05"

    def test_missing_time_is_empty(self):
        body = ("Amount: SGD25.00" + chr(10) +
                "From: PayLah! Wallet (Mobile ending 9876)" + chr(10) +
                "To: KOPI STALL")
        assert parse_dbs(body)["time"] == ""


class TestParseYouTrip:
    # The first live sample, byte-true: braces around every chip, quoted
    # Source value, Apple-Pay-truncated merchant, iPhone signature footer.
    BODY = (
        'Source: "applepay"\n'
        "Merchant: {Sheng Siong Supermarke}\n"
        "Amount: {$6.95}\n"
        "ts: {2026-07-31T13:52:52+08:00}\n"
        "\n"
        "Sent from my iPhone\n"
    )

    def test_real_sample_amount_sgd(self):
        result = parse_youtrip(self.BODY)
        assert result["amount"] == 6.95
        assert result["currency"] == "SGD"

    def test_truncated_merchant_kept_verbatim(self):
        # MerchantMap's substring matching absorbs the truncation.
        assert parse_youtrip(self.BODY)["merchant"] == "Sheng Siong Supermarke"

    def test_ts_sliced_to_date_and_time(self):
        result = parse_youtrip(self.BODY)
        assert result["date"] == "2026-07-31"
        assert result["time"] == "13:52:52"

    def test_bank_type_and_no_last_four(self):
        result = parse_youtrip(self.BODY)
        assert result["bank"] == "YouTrip"
        assert result["type"] == "applepay"
        assert result["card_last_four"] is None

    def test_payment_method_satisfies_pot_contract(self):
        # The PWA excludes pot spends by payment_method containing
        # "youtrip" (isYtSpend) — this string is that contract.
        result = parse_youtrip(self.BODY)
        assert result["payment_method"] == "YouTrip Card"
        assert "youtrip" in result["payment_method"].lower()

    def test_foreign_code_amount(self):
        # Expected overseas shape (unverified against a live foreign tap):
        # a 3-letter code inside the braces feeds the FX + travel path.
        body = self.BODY.replace("{$6.95}", "{MYR 45.00}")
        result = parse_youtrip(body)
        assert result["currency"] == "MYR"
        assert result["amount"] == 45.00

    def test_code_and_dollar_sign_amount(self):
        body = self.BODY.replace("{$6.95}", "{SGD $1,234.50}")
        result = parse_youtrip(body)
        assert result["currency"] == "SGD"
        assert result["amount"] == 1234.50

    def test_empty_merchant_chip_is_test_run(self):
        # Manual Shortcut runs produce empty variables — never ingest them.
        body = self.BODY.replace("{Sheng Siong Supermarke}", "{}")
        assert parse_youtrip(body) is None

    def test_missing_amount_returns_none(self):
        body = self.BODY.replace("Amount: {$6.95}\n", "")
        assert parse_youtrip(body) is None

    def test_malformed_ts_falls_back(self):
        # (JS falls back to today's date; the mirror exposes None.)
        body = self.BODY.replace("{2026-07-31T13:52:52+08:00}", "{tomorrow}")
        result = parse_youtrip(body)
        assert result["date"] is None
        assert result["time"] == ""

    def test_unrelated_self_mail_never_parses(self):
        assert parse_youtrip("Reminder: renew the car insurance") is None

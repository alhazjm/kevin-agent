"""Tests for recon/statement_parsers.py and recon/recon.py's diff logic.

All fixtures are SYNTHETIC — invented merchants and numbers arranged in the
same physical layout as the real statements (see the layout contract in
recon/statement_parsers.py). Real statement rows must never be copied into
this file. pypdf is imported nowhere here: the parsers take extracted text
by design, and pdf_to_text is the only pypdf touchpoint.
"""

import pytest

# --- Synthetic DBS statement -------------------------------------------------
# Statement date 23 Jan 2026 → Dec rows must roll back to 2025.
# Card ALPHA: prev 100.00, one 100.00 CR payment block, debits
#   12.50 + 6.50 (FX) + 10.00 + 50.00 (instalment) = 79.00 = TOTAL ✓
# Card BETA: prev 0.00, one padded-city one-liner 21.00 = TOTAL ✓
# GRAND = 79.00 + 21.00 = 100.00 ✓

DBS_TEXT = """\
TEST PERSON EXAMPLE
BLK 1 SYNTHETIC STREET
SINGAPORE 000000
STATEMENT DATE CREDIT LIMIT MINIMUM PAYMENT PAYMENT DUE DATE
23 Jan 2026 $10,000.00 $50.00 17 Feb 2026
This Statement serves as a TAX INVOICE if GST is charged.
DATE DESCRIPTION AMOUNT (S$)
DBS ALPHA VISA CARD NO.: **** **** **** 1111
PREVIOUS BALANCE 100.00
20 JAN BILL PAYMENT - DBS INTERNET/WIRELESS
REF NO: 11111111111111111111
100.00 CR
NEW TRANSACTIONS TEST PERSON EXAMPLE
28 DEC NOODLE PALACE 12.50
30 DEC FERRY POINT        NUSAJAYA   MY
MALAYSIAN RINGGIT 20.00
6.50
DBS Cards P.O. Box 360 S(912312)
Hotline: 1800 111 1111
Credit Cards
  Statement of Account
1 of 4
PDS_SYNTHETIC_0000
Credit Cards
  Statement of Account
02 JAN BOB©S BAKERY 10.00
23 JAN 001IL (36M) 2.68%PA+1%PF 36 (23) 50.00
SUB-TOTAL: 79.00
TOTAL: 79.00
DBS BETA MASTERCARD CARD NO.: **** **** **** 2222
PREVIOUS BALANCE 0.00
NEW TRANSACTIONS TEST PERSON EXAMPLE
05 JAN COFFEE ORBIT           SPACEPORT     CA 21.00
SUB-TOTAL: 21.00
TOTAL: 21.00
INSTALMENT PLANS SUMMARY
PLAN PRINCIPAL AMT INSTALMENT MTHS REMAINING INSTALMENT OUTSTANDING AMT
001IL (36M) 2.68%PA+1%PF    36 $1,000.00 36 13 $500.00
GRAND TOTAL FOR ALL CARD ACCOUNTS: 100.00
**** **** **** 1111 0 0 0 0 0
 DBS ALPHA VISA $ 79.00 $ 50.00. **** **** **** 1111
1 4000000000001111 0000010000 0000005000 1
"""

# --- Synthetic UOB statement -------------------------------------------------
# Statement date 12 JAN 2026 → Dec TRANS dates roll back to 2025.
# OMEGA VISA (principal, TEST PERSON): prev 40.00, 40.00 CR payment,
#   9.00 + 6.40 (FX) = SUB TOTAL 15.40 ✓
# OMEGA VISA (supplementary, SYNTH PARTNER ****4444): 4.60 = SUB TOTAL ✓
# TOTAL BALANCE FOR OMEGA VISA 20.00 = 15.40 + 4.60 ✓
# PERSONAL LOAN: checksummed but excluded from output.

UOB_TEXT = """\
Contact Us
Statement Summary
Statement Date 12  JAN  2026
Total Credit Limit SGD 10,000
Payment Summary
Amount to Pay SGD 120.00
Minimum Payment SGD 60.00
Due Date 31  JAN  2026
Please note that you are bound by a duty under the rules governing the operation of this account, to check the entries in the above statement. If you do not notify us in writing of any errors,
omissions or unauthorised debits within fourteen (14) days of this statement, the entries above shall be deemed valid, correct, accurate and conclusively binding upon you, and you shall have no
claim against the bank in relation thereto.
United Overseas Bank Limited   •   80 Raffles Place UOB Plaza Singapore 048624
Credit Card(s) Statement
Summary
OMEGA VISA 4000-0000-0000-3333 TEST PERSON 20.00 50.00
PERSONAL LOAN 9000-0000-0000-5555 TEST PERSON 100.00 100.00
120.00 150.00
OMEGA VISA
4000-0000-0000-3333 TEST PERSON
Post
Date
Trans
Date
Description of Transaction Transaction Amount
SGD
PREVIOUS BALANCE 40.00
24 DEC 24 DEC PAYMT THRU E-BANK/HOMEB/CYBERB (EP99) 40.00 CR
29 DEC 28 DEC NOODLE ORBIT LZ BEEF SOUPSingapore
Ref No. : 74000000000000000000001
9.00
Page 2 of 5
Please note that you are bound by a duty under the rules governing the operation of this account, to check the entries in the above statement. If you do not notify us in writing of any errors,
omissions or unauthorised debits within fourteen (14) days of this statement, the entries above shall be deemed valid, correct, accurate and conclusively binding upon you, and you shall have no
claim against the bank in relation thereto.
÷lŁaÿW(kdb7Sªv{¡tga˜Nÿ‘¤_¯{h8[økd~ÓSUb@Ryvîÿ^vW(SAVÛÿ 1 4ÿ
United Overseas Bank Limited   •   80 Raffles Place UOB Plaza Singapore 048624
OMEGA VISA
4000-0000-0000-3333 TEST PERSON (continued)
Post
Date
Trans
Date
Description of Transaction Transaction Amount
SGD
02 JAN 01 JAN FERRY POINT NUSAJAYA
Ref No. : 74000000000000000000002
MYR 20.00
6.40
SUB TOTAL 15.40
OMEGA VISA
4000-0000-0000-4444 SYNTH PARTNER
Post
Date
Trans
Date
Description of Transaction Transaction Amount
SGD
PREVIOUS BALANCE 0.00
03 JAN 02 JAN TEA NEBULA (CENTURYSG
Ref No. : 24000000000000000000003
4.60
SUB TOTAL 4.60
TOTAL BALANCE FOR OMEGA VISA 20.00
PERSONAL LOAN
9000-0000-0000-5555 TEST PERSON
Post
Date
Trans
Date
Description of Transaction Transaction Amount
SGD
PREVIOUS BALANCE 100.00
24 DEC 24 DEC PAYMT THRU E-BANK/HOMEB/CYBERB (EP98) 100.00 CR
11 JAN 11 JAN UOB PERSONAL LOAN INSTALM 10/24
Ref No. : 09000000000000000000004
100.00
SUB TOTAL 100.00
TOTAL BALANCE FOR PERSONAL LOAN 100.00
 -------------------------------------------------- End of Transaction Details -----------------------------------------------------
Outstanding Personal Loan Summary
4000-0000-0000-3333 20.00 50.00
TOTAL 120.00 150.00
"""


# --- DBS parser --------------------------------------------------------------


class TestParseDBS:
    def _parse(self):
        from recon.statement_parsers import parse_dbs
        return parse_dbs(DBS_TEXT)

    def test_happy_path_sections_and_totals(self):
        stmt = self._parse()
        assert stmt.bank == "DBS"
        assert stmt.statement_date == "2026-01-23"
        assert [s.card_name for s in stmt.sections] == [
            "DBS ALPHA VISA", "DBS BETA MASTERCARD"]
        assert [s.card_last4 for s in stmt.sections] == ["1111", "2222"]
        assert stmt.sections[0].printed_total == 79.00
        assert stmt.sections[1].printed_total == 21.00
        assert stmt.sections[0].cardholder == "TEST PERSON EXAMPLE"

    def test_year_rollback_dec_rows_on_jan_statement(self):
        stmt = self._parse()
        rows = stmt.sections[0].rows
        noodle = next(r for r in rows if "NOODLE" in r.merchant)
        assert noodle.date == "2025-12-28"
        bakery = next(r for r in rows if "BAKERY" in r.merchant)
        assert bakery.date == "2026-01-02"

    def test_fx_row_three_lines(self):
        stmt = self._parse()
        fx = next(r for r in stmt.sections[0].rows if r.foreign_currency)
        assert fx.foreign_currency == "MALAYSIAN RINGGIT"
        assert fx.foreign_amount == 20.00
        assert fx.amount == 6.50
        assert fx.date == "2025-12-30"
        assert not fx.is_credit

    def test_credit_block_flagged_and_not_a_debit(self):
        stmt = self._parse()
        credits = [r for r in stmt.sections[0].rows if r.is_credit]
        assert len(credits) == 1
        assert credits[0].amount == 100.00
        assert "BILL PAYMENT" in credits[0].merchant

    def test_instalment_row_takes_last_decimal_token(self):
        stmt = self._parse()
        inst = next(r for r in stmt.sections[0].rows if "001IL" in r.merchant)
        assert inst.amount == 50.00

    def test_mojibake_apostrophe_repaired(self):
        stmt = self._parse()
        assert any(r.merchant == "BOB'S BAKERY"
                   for r in stmt.sections[0].rows)

    def test_page_furniture_skipped(self):
        stmt = self._parse()
        merchants = [r.merchant for s in stmt.sections for r in s.rows]
        for junk in ("Hotline", "Statement of Account", "PDS_", "DBS Cards"):
            assert not any(junk in m for m in merchants)
        # exactly the fixture's rows, nothing phantom
        assert len(stmt.sections[0].rows) == 5  # 1 credit + 4 debits
        assert len(stmt.sections[1].rows) == 1

    def test_padded_oneliner_keeps_whole_tail_collapsed(self):
        stmt = self._parse()
        row = stmt.sections[1].rows[0]
        assert row.merchant == "COFFEE ORBIT SPACEPORT CA"
        assert row.amount == 21.00

    def test_checksum_failure_is_loud_and_names_section(self):
        from recon.statement_parsers import ChecksumError, parse_dbs
        broken = DBS_TEXT.replace("TOTAL: 79.00", "TOTAL: 80.00").replace(
            "SUB-TOTAL: 79.00", "SUB-TOTAL: 80.00")
        with pytest.raises(ChecksumError) as exc:
            parse_dbs(broken)
        msg = str(exc.value)
        assert "DBS ALPHA VISA" in msg
        assert "-1.00" in msg  # the delta, signed

    def test_grand_total_mismatch_is_loud(self):
        from recon.statement_parsers import ChecksumError, parse_dbs
        broken = DBS_TEXT.replace(
            "GRAND TOTAL FOR ALL CARD ACCOUNTS: 100.00",
            "GRAND TOTAL FOR ALL CARD ACCOUNTS: 999.00")
        with pytest.raises(ChecksumError) as exc:
            parse_dbs(broken)
        assert "grand total" in str(exc.value)

    def test_missing_statement_date_raises(self):
        from recon.statement_parsers import StatementParseError, parse_dbs
        with pytest.raises(StatementParseError):
            parse_dbs("DATE DESCRIPTION AMOUNT (S$)\nTOTAL: 1.00\n")


# --- UOB parser --------------------------------------------------------------


class TestParseUOB:
    def _parse(self):
        from recon.statement_parsers import parse_uob
        return parse_uob(UOB_TEXT)

    def test_happy_path_sections_and_totals(self):
        stmt = self._parse()
        assert stmt.bank == "UOB"
        assert stmt.statement_date == "2026-01-12"
        # PERSONAL LOAN excluded → two OMEGA VISA sections only
        assert [(s.card_name, s.card_last4) for s in stmt.sections] == [
            ("OMEGA VISA", "3333"), ("OMEGA VISA", "4444")]
        assert stmt.sections[0].printed_total == 15.40
        assert stmt.sections[1].printed_total == 4.60

    def test_personal_loan_section_excluded_entirely(self):
        stmt = self._parse()
        merchants = [r.merchant for s in stmt.sections for r in s.rows]
        assert not any("PERSONAL LOAN" in m for m in merchants)
        assert all(s.card_name != "PERSONAL LOAN" for s in stmt.sections)

    def test_uses_trans_date_not_post_date(self):
        stmt = self._parse()
        noodle = next(r for r in stmt.sections[0].rows
                      if "NOODLE" in r.merchant)
        # post 29 DEC, trans 28 DEC → trans date wins, with year rollback
        assert noodle.date == "2025-12-28"

    def test_fx_row_four_lines_iso_code(self):
        stmt = self._parse()
        fx = next(r for r in stmt.sections[0].rows if r.foreign_currency)
        assert fx.foreign_currency == "MYR"
        assert fx.foreign_amount == 20.00
        assert fx.amount == 6.40
        assert fx.date == "2026-01-01"
        assert fx.ref_no == "74000000000000000000002"

    def test_single_line_credit_row(self):
        stmt = self._parse()
        credits = [r for r in stmt.sections[0].rows if r.is_credit]
        assert len(credits) == 1
        assert credits[0].amount == 40.00
        assert "PAYMT THRU" in credits[0].merchant
        assert credits[0].ref_no is None

    def test_supplementary_section_tagged_with_cardholder(self):
        stmt = self._parse()
        supp = stmt.sections[1]
        assert supp.cardholder == "SYNTH PARTNER"
        assert supp.rows[0].cardholder == "SYNTH PARTNER"
        assert supp.rows[0].card_last4 == "4444"
        # glued location tail kept whole, never split
        assert supp.rows[0].merchant == "TEA NEBULA (CENTURYSG"

    def test_full_page_reheader_and_furniture_skipped(self):
        stmt = self._parse()
        merchants = [r.merchant for s in stmt.sections for r in s.rows]
        assert len(stmt.sections[0].rows) == 3  # 1 credit + 2 debits
        for junk in ("Page 2", "Post", "SGD", "Description of Transaction",
                     "United Overseas"):
            assert not any(junk in m for m in merchants)

    def test_checksum_failure_is_loud_and_names_section(self):
        from recon.statement_parsers import ChecksumError, parse_uob
        broken = UOB_TEXT.replace("SUB TOTAL 4.60", "SUB TOTAL 9.99")
        with pytest.raises(ChecksumError) as exc:
            parse_uob(broken)
        msg = str(exc.value)
        assert "OMEGA VISA" in msg
        assert "4444" in msg

    def test_card_total_balance_mismatch_is_loud(self):
        from recon.statement_parsers import ChecksumError, parse_uob
        broken = UOB_TEXT.replace("TOTAL BALANCE FOR OMEGA VISA 20.00",
                                  "TOTAL BALANCE FOR OMEGA VISA 25.00")
        with pytest.raises(ChecksumError) as exc:
            parse_uob(broken)
        assert "OMEGA VISA" in str(exc.value)


# --- bank sniffing -----------------------------------------------------------


class TestDetectBank:
    def test_detects_each_bank(self):
        from recon.statement_parsers import detect_bank
        assert detect_bank(DBS_TEXT) == "DBS"
        assert detect_bank(UOB_TEXT) == "UOB"

    def test_unknown_text_raises(self):
        from recon.statement_parsers import StatementParseError, detect_bank
        with pytest.raises(StatementParseError):
            detect_bank("hello world")


# --- diff classification -----------------------------------------------------


def _srow(date, merchant, amount, **kw):
    from recon.statement_parsers import StatementRow
    defaults = dict(bank="UOB", card_name="OMEGA VISA", card_last4="3333",
                    cardholder="TEST PERSON")
    defaults.update(kw)
    return StatementRow(date=date, merchant=merchant, amount=amount,
                        **defaults)


def _lrow(date, merchant, amount, **kw):
    rec = {"date": date, "merchant": merchant, "amount": amount,
           "txn_id": kw.pop("txn_id", "txn_20260101_001"),
           "source": kw.pop("source", "email")}
    rec.update(kw)
    return rec


class TestDiffRows:
    def test_exact_match(self):
        from recon.recon import diff_rows
        diff = diff_rows([_srow("2026-01-05", "NOODLE PALACE", 12.50)],
                         [_lrow("2026-01-05", "Noodle Palace", 12.50)])
        assert len(diff["matched"]) == 1
        assert diff["statement_only"] == []
        assert diff["ledger_only"] == []
        assert diff["amount_mismatch"] == []

    def test_date_within_three_days_matches(self):
        from recon.recon import diff_rows
        diff = diff_rows([_srow("2026-01-08", "NOODLE PALACE", 12.50)],
                         [_lrow("2026-01-05", "NOODLE PALACE", 12.50)])
        assert len(diff["matched"]) == 1

    def test_date_beyond_three_days_does_not_match(self):
        from recon.recon import diff_rows
        diff = diff_rows([_srow("2026-01-09", "NOODLE PALACE", 12.50)],
                         [_lrow("2026-01-05", "NOODLE PALACE", 12.50)])
        assert diff["matched"] == []
        assert len(diff["statement_only"]) == 1
        assert len(diff["ledger_only"]) == 1

    def test_bidirectional_substring_and_punctuation_blind(self):
        from recon.recon import diff_rows
        # statement carries the glued city tail; ledger has the short name
        diff = diff_rows(
            [_srow("2026-01-05", "7 ELEVEN-TUAS LINK MRT Singapore", 5.05)],
            [_lrow("2026-01-05", "7-Eleven", 5.05)])
        assert len(diff["matched"]) == 1

    def test_amount_mismatch_classification(self):
        from recon.recon import diff_rows
        diff = diff_rows([_srow("2026-01-05", "GYM MEMBERSHIP", 98.00)],
                         [_lrow("2026-01-05", "GYM MEMBERSHIP", 89.00)])
        assert diff["matched"] == []
        assert len(diff["amount_mismatch"]) == 1
        pair = diff["amount_mismatch"][0]
        assert pair["statement"].amount == 98.00
        assert pair["ledger"]["amount"] == 89.00

    def test_each_ledger_row_consumed_once(self):
        from recon.recon import diff_rows
        diff = diff_rows(
            [_srow("2026-01-05", "COFFEE ORBIT", 4.50),
             _srow("2026-01-05", "COFFEE ORBIT", 4.50)],
            [_lrow("2026-01-05", "COFFEE ORBIT", 4.50)])
        assert len(diff["matched"]) == 1
        assert len(diff["statement_only"]) == 1

    def test_backfill_ledger_rows_participate(self):
        # Recon is the M14 exception: backfill rows must match, not be
        # filtered out.
        from recon.recon import diff_rows
        diff = diff_rows(
            [_srow("2026-01-05", "TEA NEBULA", 4.60, card_last4="4444",
                   cardholder="SYNTH PARTNER")],
            [_lrow("2026-01-05", "TEA NEBULA", 4.60, source="backfill")])
        assert len(diff["matched"]) == 1
        assert diff["matched"][0]["ledger"]["source"] == "backfill"

    def test_closest_date_wins(self):
        from recon.recon import diff_rows
        far = _lrow("2026-01-02", "NOODLE PALACE", 12.50, txn_id="txn_far")
        near = _lrow("2026-01-05", "NOODLE PALACE", 12.50, txn_id="txn_near")
        diff = diff_rows([_srow("2026-01-05", "NOODLE PALACE", 12.50)],
                         [far, near])
        assert diff["matched"][0]["ledger"]["txn_id"] == "txn_near"
        assert diff["ledger_only"][0]["txn_id"] == "txn_far"


# --- SQL suggestions ---------------------------------------------------------


class TestSqlSuggestion:
    def test_backfill_insert_shape(self):
        from recon.recon import sql_suggestion
        sql = sql_suggestion(_srow("2026-01-05", "TEA NEBULA", 4.60,
                                   card_last4="4444",
                                   cardholder="SYNTH PARTNER"))
        assert "INSERT INTO transactions" in sql
        assert "next_txn_id('2026-01-05')" in sql
        assert "'backfill'" in sql
        assert "4.60" in sql
        assert "4444" in sql

    def test_single_quotes_escaped(self):
        from recon.recon import sql_suggestion
        sql = sql_suggestion(_srow("2026-01-05", "BOB'S BAKERY", 10.00))
        assert "BOB''S BAKERY" in sql


# --- report ------------------------------------------------------------------


class TestRenderReport:
    def test_report_groups_by_section_and_lists_classes(self):
        from recon.recon import diff_rows, render_report
        from recon.statement_parsers import parse_uob
        stmt = parse_uob(UOB_TEXT)
        debits = [r for s in stmt.sections for r in s.rows if not r.is_credit]
        ledger = [
            _lrow("2025-12-28", "NOODLE ORBIT", 9.00),
            _lrow("2026-01-01", "FERRY POINT", 6.40),
            # TEA NEBULA missing → statement_only
            _lrow("2026-01-03", "CASH HAWKER LUNCH", 7.00, source="manual"),
        ]
        diff = diff_rows(debits, ledger)
        report = render_report([stmt], diff)
        assert "OMEGA VISA ****3333" in report
        assert "OMEGA VISA ****4444" in report
        assert "STATEMENT ONLY" in report
        assert "TEA NEBULA" in report
        assert "LEDGER ONLY" in report
        assert "CASH HAWKER LUNCH" in report
        assert "SQL SUGGESTIONS" in report
        assert "never executed by this script".upper() \
            in report.upper()

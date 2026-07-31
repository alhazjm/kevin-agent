"""Tests for tools/loans.py + the 4 loan handlers in expense_sheets_tool.py.

Data access is mocked at the supabase_client seam (same convention as
test_travel_mode.py): tests patch the module's own `supabase_client`
attribute and configure `insert_loan` / `read_loans` / `update_loan` /
`append_transaction` return values.

Covers:
  - create_loan (validation, defaults, insert kwargs)
  - list_open_loans (totals, empty-is-ok)
  - mark_loan_repaid (loan_id and oldest-open-person matching, offset-txn
    wiring with log_offset true/false, not_found listing, retry-duplicate
    tolerance)
  - sweep_loan_offsets (PWA-flipped loan completion: offset kwargs parity
    with mark_loan_repaid, repay_txn_id stamping, count-0 silence,
    duplicate reuse, refusal → failed[] + retry-next-night, per-loan and
    outer never-raise layers)
  - the 4 handler wrappers (arg pass-through incl. ""→None coercions +
    JSON encoding)
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(os.environ, {
        "GSPREAD_SPREADSHEET_ID": "test-sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "/tmp/fake-sa.json",
    }):
        yield


def _loan_rec(**over):
    """A loans DB record as the supabase_client data layer returns it
    (None already mapped to '')."""
    rec = {
        "id": 7,
        "person": "Sarah",
        "amount": 50.0,
        "lent_date": "2026-07-02",
        "channel": "paylah",
        "txn_id": "txn_20260702_003",
        "status": "open",
        "repaid_date": "",
        "repay_txn_id": "",
        "notes": "",
    }
    rec.update(over)
    return rec


class TestCreateLoan:
    def test_creates_with_all_fields(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.insert_loan.return_value = _loan_rec()
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.create_loan(
                person="Sarah", amount=50, lent_date="2026-07-02",
                channel="paylah", txn_id="txn_20260702_003", notes="dinner",
            )
        assert result["status"] == "created"
        assert result["loan"]["person"] == "Sarah"
        assert result["loan"]["amount"] == 50.0
        mock_db.insert_loan.assert_called_once_with(
            person="Sarah", amount=50.0, lent_date="2026-07-02",
            channel="paylah", txn_id="txn_20260702_003", notes="dinner",
        )

    def test_defaults_lent_date_to_today_and_channel_paylah(self):
        from datetime import datetime
        from tools import loans
        mock_db = MagicMock()
        mock_db.insert_loan.return_value = _loan_rec()
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.create_loan(person="Sarah", amount=50)
        assert result["status"] == "created"
        kwargs = mock_db.insert_loan.call_args.kwargs
        assert kwargs["lent_date"] == datetime.now().strftime("%Y-%m-%d")
        assert kwargs["channel"] == "paylah"
        assert kwargs["txn_id"] == ""
        assert kwargs["notes"] == ""

    def test_rejects_empty_person(self):
        from tools import loans
        assert loans.create_loan(person="  ", amount=50)["status"] == "error"

    def test_rejects_non_positive_amount(self):
        from tools import loans
        assert loans.create_loan(person="Sarah", amount=0)["status"] == "error"
        assert loans.create_loan(person="Sarah", amount=-5)["status"] == "error"

    def test_rejects_unparseable_lent_date(self):
        from tools import loans
        result = loans.create_loan(person="Sarah", amount=50,
                                   lent_date="last tuesday")
        assert result["status"] == "error"
        assert "YYYY-MM-DD" in result["message"]


class TestListOpenLoans:
    def test_lists_with_total(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(id=7, amount=50.0),
            _loan_rec(id=9, person="Marc", amount=120.5,
                      lent_date="2026-07-10"),
        ]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.list_open_loans()
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["total_outstanding"] == 170.5
        mock_db.read_loans.assert_called_once_with(status="open")

    def test_empty_is_ok_with_count_zero(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = []
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.list_open_loans()
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["loans"] == []
        assert result["total_outstanding"] == 0


class TestMarkLoanRepaid:
    def test_by_loan_id_logs_offset_and_patches(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        mock_db.append_transaction.return_value = {
            "status": "ok", "txn_id": "txn_20260731_004",
            "idempotency_key": "k", "row": []}
        mock_db.update_loan.return_value = _loan_rec(
            status="repaid", repaid_date="2026-07-31",
            repay_txn_id="txn_20260731_004")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=7,
                                            repaid_date="2026-07-31")
        assert result["status"] == "updated"
        assert result["offset_txn_id"] == "txn_20260731_004"
        assert result["loan"]["status"] == "repaid"
        # Offset txn: negative amount, Lending, manual, channel as the
        # payment method, category auto-create enabled.
        mock_db.append_transaction.assert_called_once_with(
            date="2026-07-31",
            merchant="Repayment — Sarah",
            amount=-50.0,
            currency="SGD",
            category="Lending",
            source="manual",
            payment_method="paylah",
            notes="repayment of loan #7",
            create_category=True,
        )
        mock_db.update_loan.assert_called_once_with(7, {
            "status": "repaid",
            "repaid_date": "2026-07-31",
            "repay_txn_id": "txn_20260731_004",
        })

    def test_log_offset_false_skips_ledger(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        mock_db.update_loan.return_value = _loan_rec(
            status="repaid", repaid_date="2026-07-31")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=7,
                                            repaid_date="2026-07-31",
                                            log_offset=False)
        assert result["status"] == "updated"
        assert result["offset_txn_id"] is None
        mock_db.append_transaction.assert_not_called()
        mock_db.update_loan.assert_called_once_with(7, {
            "status": "repaid",
            "repaid_date": "2026-07-31",
            "repay_txn_id": "",
        })

    def test_person_matches_oldest_open_case_insensitively(self):
        from tools import loans
        mock_db = MagicMock()
        # read_loans returns oldest first (lent_date asc, id asc) — the
        # data layer's ordering IS the contract this matching relies on.
        mock_db.read_loans.return_value = [
            _loan_rec(id=3, person="Sarah", amount=20.0,
                      lent_date="2026-06-01"),
            _loan_rec(id=7, person="sarah", amount=50.0,
                      lent_date="2026-07-02"),
            _loan_rec(id=8, person="Marc", amount=99.0,
                      lent_date="2026-05-01"),
        ]
        mock_db.append_transaction.return_value = {
            "status": "ok", "txn_id": "txn_20260731_005"}
        mock_db.update_loan.return_value = _loan_rec(id=3, status="repaid")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(person="SARAH",
                                            repaid_date="2026-07-31")
        assert result["status"] == "updated"
        # Oldest Sarah loan (id=3, $20) — not the newer $50 one.
        assert mock_db.update_loan.call_args.args[0] == 3
        assert mock_db.append_transaction.call_args.kwargs["amount"] == -20.0

    def test_no_match_lists_open_loans(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(person="Sam")
        assert result["status"] == "not_found"
        assert len(result["open_loans"]) == 1
        assert result["open_loans"][0]["person"] == "Sarah"
        mock_db.update_loan.assert_not_called()
        mock_db.append_transaction.assert_not_called()

    def test_unknown_loan_id_lists_open_loans(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=999)
        assert result["status"] == "not_found"
        assert "999" in result["message"]

    def test_requires_loan_id_or_person(self):
        from tools import loans
        result = loans.mark_loan_repaid()
        assert result["status"] == "error"

    def test_duplicate_offset_reuses_existing_txn_id(self):
        # Retry after a failed loan patch: the offset already landed, the
        # ledger dedupes it, and the repayment still completes.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        mock_db.append_transaction.return_value = {
            "status": "duplicate", "txn_id": "txn_20260731_004",
            "idempotency_key": "k", "message": "Duplicate transaction"}
        mock_db.update_loan.return_value = _loan_rec(
            status="repaid", repay_txn_id="txn_20260731_004")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=7,
                                            repaid_date="2026-07-31")
        assert result["status"] == "updated"
        assert result["offset_txn_id"] == "txn_20260731_004"

    def test_offset_refusal_leaves_loan_open(self):
        # e.g. unknown_category refusal — the loan must NOT flip to repaid
        # when the offset write was refused.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        mock_db.append_transaction.return_value = {
            "status": "unknown_category", "category": "Lending",
            "message": "refused"}
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=7)
        assert result["status"] == "error"
        assert "loan left open" in result["message"]
        mock_db.update_loan.assert_not_called()

    def test_rejects_unparseable_repaid_date(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [_loan_rec()]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.mark_loan_repaid(loan_id=7, repaid_date="soon")
        assert result["status"] == "error"
        assert "YYYY-MM-DD" in result["message"]


class TestSweepLoanOffsets:
    """The nightly completion pass for loans the PWA's one-tap "Paid back"
    button flipped (status="repaid") without being able to write the
    offsetting ledger txn (its RLS is read+update only)."""

    def test_completes_pending_loan_and_stamps_repay_txn_id(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(status="repaid", repaid_date="2026-07-30"),
        ]
        mock_db.append_transaction.return_value = {
            "status": "ok", "txn_id": "txn_20260731_006",
            "idempotency_key": "k", "row": []}
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["failed"] == []
        assert result["completed"] == [{
            "loan_id": 7,
            "person": "Sarah",
            "amount": 50.0,
            "offset_txn_id": "txn_20260731_006",
        }]
        mock_db.read_loans.assert_called_once_with(status="repaid")
        # The offset must be byte-identical to mark_loan_repaid's (shared
        # _log_repayment_offset helper): negative amount, Lending, manual,
        # channel as payment method, category auto-create, and the loan's
        # repaid_date as the txn date.
        mock_db.append_transaction.assert_called_once_with(
            date="2026-07-30",
            merchant="Repayment — Sarah",
            amount=-50.0,
            currency="SGD",
            category="Lending",
            source="manual",
            payment_method="paylah",
            notes="repayment of loan #7",
            create_category=True,
        )
        # Only repay_txn_id is patched — the PWA already set status and
        # repaid_date when it flipped the loan.
        mock_db.update_loan.assert_called_once_with(
            7, {"repay_txn_id": "txn_20260731_006"})

    def test_count_zero_when_no_candidates(self):
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = []
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        # count 0 is still status ok — the cron stays silent about loans.
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["completed"] == []
        assert result["failed"] == []
        mock_db.append_transaction.assert_not_called()
        mock_db.update_loan.assert_not_called()

    def test_already_completed_loans_untouched(self):
        # A loan mark_loan_repaid (or a previous sweep) already finished:
        # repay_txn_id is set, so the sweep must not touch it.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(status="repaid", repaid_date="2026-07-30",
                      repay_txn_id="txn_20260730_002"),
        ]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["completed"] == []
        assert result["failed"] == []
        mock_db.append_transaction.assert_not_called()
        mock_db.update_loan.assert_not_called()

    def test_empty_repaid_date_defaults_to_today(self):
        from datetime import datetime
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(status="repaid", repaid_date=""),
        ]
        mock_db.append_transaction.return_value = {
            "status": "ok", "txn_id": "txn_20260731_006"}
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["count"] == 1
        kwargs = mock_db.append_transaction.call_args.kwargs
        assert kwargs["date"] == datetime.now().strftime("%Y-%m-%d")

    def test_duplicate_offset_reuses_existing_txn_id(self):
        # Re-run after a crash between offset write and loan patch: the
        # ledger dedupes on the idempotency key and hands back the
        # existing txn_id, which still gets stamped onto the loan.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(status="repaid", repaid_date="2026-07-30"),
        ]
        mock_db.append_transaction.return_value = {
            "status": "duplicate", "txn_id": "txn_20260730_009",
            "idempotency_key": "k", "message": "Duplicate transaction"}
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["completed"][0]["offset_txn_id"] == "txn_20260730_009"
        assert result["failed"] == []
        mock_db.update_loan.assert_called_once_with(
            7, {"repay_txn_id": "txn_20260730_009"})

    def test_refused_offset_lands_in_failed_and_rest_still_complete(self):
        # A refusal (e.g. unknown_category) skips that loan and leaves its
        # repay_txn_id empty so the NEXT night retries — and must not
        # block the other pending loans in the same sweep.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(id=7, person="Sarah", status="repaid",
                      repaid_date="2026-07-30"),
            _loan_rec(id=9, person="Marc", amount=120.5, channel="paynow",
                      status="repaid", repaid_date="2026-07-31"),
        ]
        mock_db.append_transaction.side_effect = [
            # consumer 1: Sarah's offset — refused (unknown_category)
            {"status": "unknown_category", "category": "Lending",
             "message": "refused"},
            # consumer 2: Marc's offset — succeeds
            {"status": "ok", "txn_id": "txn_20260731_007"},
        ]
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["completed"] == [{
            "loan_id": 9, "person": "Marc", "amount": 120.5,
            "offset_txn_id": "txn_20260731_007",
        }]
        assert result["failed"] == [{
            "loan_id": 7, "person": "Sarah", "amount": 50.0,
            "reason": "unknown_category: refused",
        }]
        # Only Marc's loan is patched — Sarah's repay_txn_id stays empty.
        mock_db.update_loan.assert_called_once_with(
            9, {"repay_txn_id": "txn_20260731_007"})

    def test_per_loan_exception_lands_in_failed_without_raising(self):
        # A transient PostgREST failure raises RuntimeError out of
        # append_transaction — the per-loan try/except converts it to a
        # failed[] entry and the sweep still returns ok.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.return_value = [
            _loan_rec(status="repaid", repaid_date="2026-07-30"),
        ]
        mock_db.append_transaction.side_effect = RuntimeError("supabase 503")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert len(result["failed"]) == 1
        assert result["failed"][0]["loan_id"] == 7
        assert "exception" in result["failed"][0]["reason"]
        mock_db.update_loan.assert_not_called()

    def test_read_failure_returns_error_dict_never_raises(self):
        # Outer layer: even the candidate read blowing up must come back
        # as a status dict — this runs inside the nightly cron flow.
        from tools import loans
        mock_db = MagicMock()
        mock_db.read_loans.side_effect = RuntimeError("supabase down")
        with patch.object(loans, "supabase_client", mock_db):
            result = loans.sweep_loan_offsets()
        assert result["status"] == "error"
        assert "sweep_loan_offsets failed" in result["message"]


class TestLoanHandlers:
    """The 4 handlers in expense_sheets_tool.py delegate to tools.loans.
    Pins: exact downstream kwargs (incl. ""→None coercions) + JSON
    round-trip."""

    def test_create_loan_handler_passes_kwargs(self):
        from tools.expense_sheets_tool import handle_create_loan
        from tools import loans
        with patch.object(loans, "create_loan",
                          return_value={"status": "created",
                                        "loan": {"id": 7}}) as mock_fn:
            result = json.loads(handle_create_loan({
                "person": "Sarah", "amount": 50,
                "lent_date": "2026-07-02", "channel": "paynow",
                "txn_id": "txn_20260702_003", "notes": "dinner",
            }))
        mock_fn.assert_called_once_with(
            person="Sarah", amount=50.0, lent_date="2026-07-02",
            channel="paynow", txn_id="txn_20260702_003", notes="dinner",
        )
        assert result["status"] == "created"

    def test_create_loan_handler_coerces_empty_strings(self):
        from tools.expense_sheets_tool import handle_create_loan
        from tools import loans
        with patch.object(loans, "create_loan",
                          return_value={"status": "created",
                                        "loan": {}}) as mock_fn:
            handle_create_loan({"person": "Sarah", "amount": 50,
                                "lent_date": "", "channel": ""})
        mock_fn.assert_called_once_with(
            person="Sarah", amount=50.0, lent_date=None,
            channel="paylah", txn_id="", notes="",
        )

    def test_mark_loan_repaid_handler_passes_kwargs(self):
        from tools.expense_sheets_tool import handle_mark_loan_repaid
        from tools import loans
        with patch.object(loans, "mark_loan_repaid",
                          return_value={"status": "updated", "loan": {},
                                        "offset_txn_id": "txn_x"}) as mock_fn:
            result = json.loads(handle_mark_loan_repaid({
                "loan_id": 7, "repaid_date": "2026-07-31",
                "log_offset": False,
            }))
        mock_fn.assert_called_once_with(
            loan_id=7, person=None, repaid_date="2026-07-31",
            log_offset=False,
        )
        assert result["offset_txn_id"] == "txn_x"

    def test_mark_loan_repaid_handler_coercions_and_defaults(self):
        from tools.expense_sheets_tool import handle_mark_loan_repaid
        from tools import loans
        with patch.object(loans, "mark_loan_repaid",
                          return_value={"status": "updated", "loan": {},
                                        "offset_txn_id": None}) as mock_fn:
            # loan_id as string (LLMs do this), person/date empty strings,
            # log_offset omitted → defaults to True.
            handle_mark_loan_repaid({"loan_id": "7", "person": "",
                                     "repaid_date": ""})
        mock_fn.assert_called_once_with(
            loan_id=7, person=None, repaid_date=None, log_offset=True,
        )

    def test_mark_loan_repaid_handler_person_only(self):
        from tools.expense_sheets_tool import handle_mark_loan_repaid
        from tools import loans
        with patch.object(loans, "mark_loan_repaid",
                          return_value={"status": "not_found",
                                        "open_loans": []}) as mock_fn:
            result = json.loads(handle_mark_loan_repaid({"person": "Sarah"}))
        mock_fn.assert_called_once_with(
            loan_id=None, person="Sarah", repaid_date=None, log_offset=True,
        )
        assert result["status"] == "not_found"

    def test_list_open_loans_handler_round_trip(self):
        from tools.expense_sheets_tool import handle_list_open_loans
        from tools import loans
        payload = {"status": "ok", "loans": [{"id": 7}], "count": 1,
                   "total_outstanding": 50.0}
        with patch.object(loans, "list_open_loans",
                          return_value=payload) as mock_fn:
            result = json.loads(handle_list_open_loans({}))
        mock_fn.assert_called_once_with()
        assert result == payload

    def test_sweep_loan_offsets_handler_round_trip(self):
        from tools.expense_sheets_tool import handle_sweep_loan_offsets
        from tools import loans
        payload = {"status": "ok", "count": 1,
                   "completed": [{"loan_id": 7, "person": "Sarah",
                                  "amount": 50.0,
                                  "offset_txn_id": "txn_20260731_006"}],
                   "failed": []}
        with patch.object(loans, "sweep_loan_offsets",
                          return_value=payload) as mock_fn:
            result = json.loads(handle_sweep_loan_offsets({}))
        mock_fn.assert_called_once_with()
        assert result == payload

    def test_sweep_loan_offsets_handler_ignores_stray_args(self):
        # The tool takes no args; an LLM inventing some must not break it
        # or leak them downstream.
        from tools.expense_sheets_tool import handle_sweep_loan_offsets
        from tools import loans
        with patch.object(loans, "sweep_loan_offsets",
                          return_value={"status": "ok", "count": 0,
                                        "completed": [],
                                        "failed": []}) as mock_fn:
            result = json.loads(handle_sweep_loan_offsets(
                {"days_back": 7, "person": "Sarah"}))
        mock_fn.assert_called_once_with()
        assert result["count"] == 0

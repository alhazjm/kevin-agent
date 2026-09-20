"""Tests for the card-optimiser module and its 5 tool handlers.

Also covers the two bug-fix regressions from commit 9926a5a that ride along
on this branch:
  - get_spending_summary(month="") no longer raises IndexError
  - _is_transient_gspread_error recognises 429 / 5xx as transient
"""

import json
import os
from datetime import date
from unittest.mock import MagicMock, patch

import gspread
import pytest


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(os.environ, {
        "GSPREAD_SPREADSHEET_ID": "test-sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "/tmp/fake-sa.json",
    }):
        yield


SAMPLE_CARDS = [
    {
        "card_id": "dbs-altitude",
        "display_name": "DBS Altitude Visa",
        "payment_method_pattern": "DBS/POSB card ending 1234",
        "cycle_start_day": 15,
        "min_spend_bonus": 0.0,
        "notes": "",
    },
    {
        "card_id": "uob-prvi",
        "display_name": "UOB PRVI Miles",
        "payment_method_pattern": "UOB Card ending 5678",
        "cycle_start_day": 1,
        "min_spend_bonus": 0.0,
        "notes": "",
    },
]


SAMPLE_STRATEGIES = [
    {
        "category": "_default",
        "primary_card_id": "uob-prvi",
        "primary_cap": 0.0,
        "primary_earn_rate": 1.4,
        "fallback_card_id": "",
        "fallback_earn_rate": 0.0,
        "promo_active_until": "",
        "notes": "catch-all",
    },
    {
        "category": "Personal - Food & Drinks",
        "primary_card_id": "dbs-altitude",
        "primary_cap": 1000.0,
        "primary_earn_rate": 4.0,
        "fallback_card_id": "uob-prvi",
        "fallback_earn_rate": 2.4,
        "promo_active_until": "",
        "notes": "DBS 4mpd dining",
    },
]


class TestSetupGate:
    def test_none_when_ready(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES):
            assert card_optimiser._check_setup() is None

    def test_missing_cards(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=[]), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES):
            gate = card_optimiser._check_setup()
        assert gate is not None
        assert gate["status"] == "setup_required"
        assert "`cards` table" in gate["message"]

    def test_missing_strategy(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=[]):
            gate = card_optimiser._check_setup()
        assert gate["status"] == "setup_required"
        assert "`card_strategy` table" in gate["message"]

    def test_missing_default_sentinel(self):
        from tools import card_optimiser
        no_default = [s for s in SAMPLE_STRATEGIES if s["category"] != "_default"]
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=no_default):
            gate = card_optimiser._check_setup()
        assert gate["status"] == "setup_required"
        assert "_default" in gate["message"]


class TestCardResolution:
    def test_longest_match_wins(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [
            {"card_id": "a", "payment_method_pattern": "DBS"},
            {"card_id": "b", "payment_method_pattern": "DBS/POSB card ending 1234"},
            {"card_id": "c", "payment_method_pattern": "UOB"},
        ]
        match = _find_card_by_payment_method("DBS/POSB card ending 1234 at Cold Storage", cards)
        assert match["card_id"] == "b"

    def test_no_match_returns_none(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [{"card_id": "a", "payment_method_pattern": "DBS"}]
        assert _find_card_by_payment_method("Citibank MC 1234", cards) is None

    def test_empty_payment_method(self):
        from tools.card_optimiser import _find_card_by_payment_method
        assert _find_card_by_payment_method("", SAMPLE_CARDS) is None

    def test_case_insensitive(self):
        from tools.card_optimiser import _find_card_by_payment_method
        cards = [{"card_id": "a", "payment_method_pattern": "DBS/POSB"}]
        match = _find_card_by_payment_method("dbs/posb card ending 1234", cards)
        assert match["card_id"] == "a"


class TestStrategyResolution:
    def test_exact_match_wins(self):
        from tools.card_optimiser import _find_strategy
        s = _find_strategy("Personal - Food & Drinks", SAMPLE_STRATEGIES)
        assert s["primary_card_id"] == "dbs-altitude"

    def test_default_fallback(self):
        from tools.card_optimiser import _find_strategy
        s = _find_strategy("Groceries", SAMPLE_STRATEGIES)
        assert s["primary_card_id"] == "uob-prvi"
        assert s["category"] == "_default"

    def test_no_default_returns_none(self):
        from tools.card_optimiser import _find_strategy
        no_default = [s for s in SAMPLE_STRATEGIES if s["category"] != "_default"]
        assert _find_strategy("Groceries", no_default) is None


class TestCycleWindow:
    def test_mid_cycle(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2026, 4, 22))
        assert start == date(2026, 4, 15)
        assert end == date(2026, 5, 14)

    def test_before_cycle_boundary(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2026, 4, 10))
        assert start == date(2026, 3, 15)
        assert end == date(2026, 4, 14)

    def test_cycle_day_31_feb_clamp(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(31, date(2026, 2, 15))
        # Jan 31 cycle runs until Feb 27 (Feb 28 clamps to day 28, so the
        # current cycle for Feb 15 started on Jan 31 and the next cycle
        # starts Feb 28 → current cycle ends Feb 27).
        assert start == date(2026, 1, 31)
        assert end == date(2026, 2, 27)

    def test_cycle_day_1_simple(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(1, date(2026, 4, 10))
        assert start == date(2026, 4, 1)
        assert end == date(2026, 4, 30)

    def test_cycle_across_year_boundary(self):
        from tools.card_optimiser import _cycle_window
        start, end = _cycle_window(15, date(2027, 1, 5))
        assert start == date(2026, 12, 15)
        assert end == date(2027, 1, 14)


class TestCapStatus:
    def test_ok_under_80(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(500, 1000) == "ok"
        assert _cap_status(799, 1000) == "ok"

    def test_warning_at_80(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(800, 1000) == "warning"
        assert _cap_status(999, 1000) == "warning"

    def test_capped_at_100(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(1000, 1000) == "capped"
        assert _cap_status(1500, 1000) == "capped"

    def test_zero_cap_is_unlimited(self):
        from tools.card_optimiser import _cap_status
        assert _cap_status(99999, 0) == "ok"


class TestSpendInCycle:
    def test_filters_by_cycle_window(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            # In cycle (2026-04-15 to 2026-05-14)
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Before cycle — excluded
            {"Date": "2026-04-10", "Category": "Personal - Food & Drinks",
             "Amount": 100, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # After cycle — excluded
            {"Date": "2026-05-20", "Category": "Personal - Food & Drinks",
             "Amount": 200, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
        ]
        with patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_all_transaction_rows.return_value = records
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0

    def test_excludes_backfill_and_pending(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Backfill — skipped
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 1000, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "backfill"},
            # Pending — skipped
            {"Date": "2026-04-20", "Category": "UNCATEGORIZED",
             "Amount": 25, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
        ]
        with patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_all_transaction_rows.return_value = records
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0

    def test_filters_by_payment_method(self):
        from tools import card_optimiser
        card = {
            "cycle_start_day": 15,
            "payment_method_pattern": "DBS/POSB card ending 1234",
        }
        records = [
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 50, "Payment Method": "DBS/POSB card ending 1234",
             "Source": "email"},
            # Different card — skipped
            {"Date": "2026-04-20", "Category": "Personal - Food & Drinks",
             "Amount": 500, "Payment Method": "UOB Card ending 5678",
             "Source": "email"},
        ]
        with patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_all_transaction_rows.return_value = records
            spent = card_optimiser._spend_in_cycle(
                card, "Personal - Food & Drinks", date(2026, 4, 22)
            )
        assert spent == 50.0


class TestMaybeSendPostCapNudge:
    @pytest.fixture
    def patched_helpers(self):
        """Patch out all data-layer reads so only the decision logic runs."""
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=SAMPLE_CARDS), \
             patch.object(card_optimiser, "read_card_strategy", return_value=SAMPLE_STRATEGIES), \
             patch.object(card_optimiser, "_record_nudge"), \
             patch.object(card_optimiser, "_find_triggering_txn_id", return_value="txn_20260420_001"):
            yield card_optimiser

    def test_setup_required_returns_no_send(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards", return_value=[]), \
             patch.object(card_optimiser, "read_card_strategy", return_value=[]):
            result = card_optimiser.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "setup"

    def test_unmapped_payment_method(self, patched_helpers):
        result = patched_helpers.maybe_send_post_cap_nudge(
            payment_method="Random Bank Card 9999",
            category="Personal - Food & Drinks",
            amount=100.0,
            txn_date="2026-04-20",
        )
        assert result["sent"] is False
        assert result["reason"] == "unmapped_payment_method"

    def test_not_primary_for_category(self, patched_helpers):
        result = patched_helpers.maybe_send_post_cap_nudge(
            payment_method="UOB Card ending 5678",
            category="Personal - Food & Drinks",
            amount=100.0,
            txn_date="2026-04-20",
        )
        assert result["sent"] is False
        assert result["reason"] == "not_primary_for_category"

    def test_fires_at_80_percent(self, patched_helpers):
        # spent_after=810 crosses 80% (cap=1000). amount=100 → spent_before=710 (71%).
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=810.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=False), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "1234"}):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is True
        assert result["threshold"] == 80
        assert result["card_id"] == "dbs-altitude"

    def test_no_threshold_crossed(self, patched_helpers):
        # spent_after=500 (50%), spent_before=400 (40%). No crossing.
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=500.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=False):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "no_threshold_crossed"

    def test_already_nudged_dedup(self, patched_helpers):
        # 80% threshold crossed, but already nudged this cycle.
        with patch.object(patched_helpers, "_spend_in_cycle", return_value=810.0), \
             patch.object(patched_helpers, "_already_nudged", return_value=True):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "already_nudged_this_cycle"

    def test_silent_when_already_switched(self, patched_helpers):
        # spent_after=1050 crosses 100%. amount=50 → spent_before=1000 (still
        # 100%, but pct_before=1.0 not < 1.0). Use amount=100 → spent_before=950
        # (95%), spent_after=1050 (105%) → crosses 100%. Good.
        def already(cycle_window, card_id, category, threshold):
            return threshold == 80  # 80% already fired, 100% has not

        with patch.object(patched_helpers, "_spend_in_cycle", return_value=1050.0), \
             patch.object(patched_helpers, "_already_nudged", side_effect=already), \
             patch.object(patched_helpers, "_last_n_in_category_on_card", return_value=True), \
             patch("tools.expense_sheets_tool._send_telegram_bubble") as mock_send:
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-25",
            )
        assert result["sent"] is False
        assert result["reason"] == "silent_already_switched"
        mock_send.assert_not_called()

    def test_100_fires_when_not_already_switched(self, patched_helpers):
        def already(cycle_window, card_id, category, threshold):
            return threshold == 80

        with patch.object(patched_helpers, "_spend_in_cycle", return_value=1050.0), \
             patch.object(patched_helpers, "_already_nudged", side_effect=already), \
             patch.object(patched_helpers, "_last_n_in_category_on_card", return_value=False), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "5678"}):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-25",
            )
        assert result["sent"] is True
        assert result["threshold"] == 100

    def test_swallows_exceptions(self, patched_helpers):
        with patch.object(patched_helpers, "_spend_in_cycle",
                          side_effect=RuntimeError("boom")):
            result = patched_helpers.maybe_send_post_cap_nudge(
                payment_method="DBS/POSB card ending 1234",
                category="Personal - Food & Drinks",
                amount=100.0,
                txn_date="2026-04-20",
            )
        assert result["sent"] is False
        assert result["reason"] == "exception"
        assert "boom" in result["error"]


class TestHandlers:
    """The 5 handlers in expense_sheets_tool.py delegate to card_optimiser.
    These tests verify arg pass-through and JSON encoding."""

    def test_get_card_cap_status_handler(self):
        from tools.expense_sheets_tool import handle_get_card_cap_status
        from tools import card_optimiser
        with patch.object(card_optimiser, "get_card_cap_status",
                          return_value={"status": "ok", "cards": []}) as mock_fn:
            result = json.loads(handle_get_card_cap_status({
                "card_id": "dbs-altitude",
                "category": "Personal - Food & Drinks",
            }))
        mock_fn.assert_called_once_with(
            card_id="dbs-altitude",
            category="Personal - Food & Drinks",
            as_of=None,
        )
        assert result["status"] == "ok"

    def test_recommend_card_for_handler(self):
        from tools.expense_sheets_tool import handle_recommend_card_for
        from tools import card_optimiser
        with patch.object(card_optimiser, "recommend_card_for",
                          return_value={"status": "ok"}) as mock_fn:
            handle_recommend_card_for({
                "category": "Personal - Food & Drinks",
                "amount": 42.50,
            })
        mock_fn.assert_called_once_with(
            category="Personal - Food & Drinks",
            amount=42.5,
            as_of=None,
        )

    def test_plan_month_handler(self):
        from tools.expense_sheets_tool import handle_plan_month
        from tools import card_optimiser
        with patch.object(card_optimiser, "plan_month",
                          return_value={"status": "ok", "plan": []}) as mock_fn:
            result = json.loads(handle_plan_month({"month": "2026-04"}))
        mock_fn.assert_called_once_with(month="2026-04")
        assert result["status"] == "ok"

    def test_review_card_efficiency_handler(self):
        from tools.expense_sheets_tool import handle_review_card_efficiency
        from tools import card_optimiser
        with patch.object(card_optimiser, "review_card_efficiency",
                          return_value={"status": "ok"}) as mock_fn:
            handle_review_card_efficiency({"month": "2026-03"})
        mock_fn.assert_called_once_with(month="2026-03")

    def test_set_category_primary_handler_passes_overrides(self):
        from tools.expense_sheets_tool import handle_set_category_primary
        from tools import card_optimiser
        with patch.object(card_optimiser, "set_category_primary",
                          return_value={"status": "ok"}) as mock_fn:
            handle_set_category_primary({
                "category": "Personal - Food & Drinks",
                "card_id": "dbs-altitude",
                "until_date": "2026-04-30",
                "earn_rate": 10,
            })
        mock_fn.assert_called_once_with(
            category="Personal - Food & Drinks",
            card_id="dbs-altitude",
            until_date="2026-04-30",
            earn_rate=10.0,
            cap=None,
            fallback_card_id=None,
            fallback_earn_rate=None,
        )

    def test_set_category_primary_handler_omits_empties(self):
        from tools.expense_sheets_tool import handle_set_category_primary
        from tools import card_optimiser
        with patch.object(card_optimiser, "set_category_primary",
                          return_value={"status": "ok"}) as mock_fn:
            handle_set_category_primary({
                "category": "Groceries",
                "card_id": "uob-prvi",
                "until_date": "",
            })
        call = mock_fn.call_args
        assert call.kwargs["until_date"] is None
        assert call.kwargs["earn_rate"] is None


# --- Bug-fix regressions from commit 9926a5a (already on this branch) -------


class TestEmptyMonthRegression:
    """get_spending_summary / generate_spending_report / read_transactions /
    write_insight must not crash when called with month=\"\"."""

    def test_get_spending_summary_empty_string(self):
        from tools import sheets_client
        with patch.object(sheets_client, "read_transactions", return_value=[]), \
             patch.object(sheets_client, "read_budgets", return_value=[]):
            result = sheets_client.get_spending_summary("")
        assert isinstance(result, dict)

    def test_generate_spending_report_empty_string(self):
        from tools import sheets_client
        with patch.object(sheets_client, "read_transactions", return_value=[]), \
             patch.object(sheets_client, "read_budgets", return_value=[]):
            result = sheets_client.generate_spending_report("")
        assert isinstance(result, dict)
        assert "month" in result


class TestTransientGspreadDetection:
    """_is_transient_gspread_error gates the retry wrapper in
    get_spreadsheet(). It should return True for 429/5xx, False otherwise."""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_status_codes(self, status):
        from tools.sheets_client import _is_transient_gspread_error
        exc = MagicMock()
        exc.response.status_code = status
        assert _is_transient_gspread_error(exc) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_transient_status_codes(self, status):
        from tools.sheets_client import _is_transient_gspread_error
        exc = MagicMock()
        exc.response.status_code = status
        assert _is_transient_gspread_error(exc) is False

    def test_no_response_attribute(self):
        from tools.sheets_client import _is_transient_gspread_error
        assert _is_transient_gspread_error(RuntimeError("unrelated")) is False


class TestGetSpreadsheetRetry:
    """get_spreadsheet() retries on transient gspread errors."""

    def _make_apierror(self, status_code):
        """Build a gspread APIError without calling its init (which needs a
        real Response). The retry code only reads `.response.status_code`."""
        err = gspread.exceptions.APIError.__new__(gspread.exceptions.APIError)
        resp = MagicMock()
        resp.status_code = status_code
        err.response = resp
        return err

    def test_succeeds_after_retry(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = [
            self._make_apierror(503),
            self._make_apierror(503),
            "ss-ok",
        ]
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            result = sheets_client.get_spreadsheet()
        assert result == "ss-ok"
        assert client.open_by_key.call_count == 3

    def test_fails_fast_on_403(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = self._make_apierror(403)
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            with pytest.raises(gspread.exceptions.APIError):
                sheets_client.get_spreadsheet()
        assert client.open_by_key.call_count == 1

    def test_gives_up_after_3_attempts(self):
        from tools import sheets_client
        client = MagicMock()
        client.open_by_key.side_effect = [
            self._make_apierror(503),
            self._make_apierror(503),
            self._make_apierror(503),
        ]
        with patch.object(sheets_client, "get_client", return_value=client), \
             patch.object(sheets_client.time, "sleep"):
            with pytest.raises(gspread.exceptions.APIError):
                sheets_client.get_spreadsheet()
        assert client.open_by_key.call_count == 3


# Real card lineup for the strategy-layer hooks (min-spend + steering) —
# the steer rules key on the real card_ids, unlike the fictional
# SAMPLE_CARDS above.
STRAT_CARDS = [
    {"card_id": "uob-pref", "display_name": "UOB Preferred Visa",
     "payment_method_pattern": "UOB Card ending 5678",
     "cycle_start_day": 13, "min_spend_bonus": 0.0, "bonus_cap": 600.0,
     "notes": ""},
    {"card_id": "dbs-yuu", "display_name": "DBS Yuu Visa",
     "payment_method_pattern": "DBS/POSB card ending 1234",
     "cycle_start_day": 24, "min_spend_bonus": 800.0, "bonus_cap": 0.0,
     "notes": ""},
    {"card_id": "hsbc-revo", "display_name": "HSBC Revolution Visa",
     "payment_method_pattern": "HSBC card ending 1357",
     "cycle_start_day": 21, "min_spend_bonus": 0.0, "bonus_cap": 1000.0,
     "notes": ""},
    {"card_id": "dbs-vantage", "display_name": "DBS Vantage Visa",
     "payment_method_pattern": "DBS/POSB card ending 4321",
     "cycle_start_day": 24, "min_spend_bonus": 0.0, "bonus_cap": 0.0,
     "notes": ""},
]


class TestGetBonusPoolStatus:
    """Calendar-month bonus pools: Preferred splits contactless/online by
    merchant class; other capped cards report one pool; capless cards are
    omitted."""

    def _txn(self, date_s, merchant, amount, pm, source="email"):
        return {"Date": date_s, "Merchant": merchant, "Amount": amount,
                "Category": "Whatever", "Source": source,
                "Payment Method": pm}

    def _run(self, rows, cards=None, month="2026-07"):
        from tools import card_optimiser
        mock_db = MagicMock()
        mock_db.read_all_transaction_rows.return_value = rows
        if cards is None:
            cards = STRAT_CARDS
        with patch.object(card_optimiser, "read_cards",
                          return_value=cards), \
             patch.object(card_optimiser, "supabase_client", mock_db):
            return card_optimiser.get_bonus_pool_status(month=month)

    def test_preferred_splits_two_pools_by_merchant_class(self):
        rows = [
            self._txn("2026-07-05", "KOPITIAM @ RAFFLES", 100.0,
                      "UOB Card ending 5678"),
            self._txn("2026-07-08", "SHENG SIONG SUPERMARKET", 50.0,
                      "UOB Card ending 5678"),
            self._txn("2026-07-10", "SHOPEE SINGAPORE MP", 80.0,
                      "UOB Card ending 5678"),
        ]
        result = self._run(rows)
        assert result["status"] == "ok"
        pref = next(c for c in result["cards"] if c["card_id"] == "uob-pref")
        pools = {p["pool"]: p for p in pref["pools"]}
        assert pools["contactless"]["spent"] == 150.0
        assert pools["contactless"]["cap"] == 600.0
        assert pools["contactless"]["status"] == "ok"
        assert pools["online"]["spent"] == 80.0

    def test_revo_reports_single_pool_with_bands(self):
        rows = [self._txn("2026-07-03", "GRAB* A-9XYZ", 850.0,
                          "HSBC card ending 1357")]
        result = self._run(rows)
        revo = next(c for c in result["cards"] if c["card_id"] == "hsbc-revo")
        assert len(revo["pools"]) == 1
        assert revo["pools"][0]["pool"] == "bonus"
        assert revo["pools"][0]["spent"] == 850.0
        assert revo["pools"][0]["status"] == "warning"

    def test_cards_with_nothing_to_track_omitted(self):
        # yuu has no pool but a min-spend gate → included (with min_spend
        # block, no pools); Vantage has neither → omitted, so the Friday
        # summary never prints "$0 / $0" for it again (2026-08-14).
        result = self._run([])
        ids = {c["card_id"] for c in result["cards"]}
        assert ids == {"uob-pref", "hsbc-revo", "dbs-yuu"}
        yuu = next(c for c in result["cards"] if c["card_id"] == "dbs-yuu")
        assert yuu["pools"] == []
        assert yuu["min_spend"]["threshold"] == 800.0
        assert yuu["min_spend"]["met"] is False

    def test_min_spend_progress_and_verbatim_lines(self):
        rows = [
            self._txn("2026-07-05", "KOPITIAM @ RAFFLES", 100.0,
                      "UOB Card ending 5678"),
            self._txn("2026-07-10", "SHOPEE SINGAPORE MP", 80.0,
                      "UOB Card ending 5678"),
            self._txn("2026-07-03", "GRAB* A-9XYZ", 850.0,
                      "HSBC card ending 1357"),
            self._txn("2026-07-06", "GOJEK", 312.0,
                      "DBS/POSB card ending 1234"),
        ]
        result = self._run(rows)
        yuu = next(c for c in result["cards"] if c["card_id"] == "dbs-yuu")
        assert yuu["min_spend"] == {"spent": 312.0, "threshold": 800.0,
                                    "met": False, "short_by": 488.0}
        # One honest number per pool, never a summed cap; Vantage absent.
        assert result["lines"] == [
            "💳 UOB Preferred Visa: tap $100 / $600 (17%) · online $80 / $600 (13%)",
            "💳 DBS Yuu Visa: $312 / $800 min spend — $488 to go",
            "💳 HSBC Revolution Visa: $850 / $1,000 bonus pool (85%)",
        ]
        assert not any("Vantage" in ln for ln in result["lines"])

    def test_min_spend_met_line(self):
        rows = [self._txn("2026-07-06", "GOJEK", 900.0,
                          "DBS/POSB card ending 1234")]
        result = self._run(rows)
        yuu_line = next(ln for ln in result["lines"] if "Yuu" in ln)
        assert yuu_line == "💳 DBS Yuu Visa: $900 / $800 min spend ✓ met"

    def test_month_pending_and_backfill_filtered(self):
        rows = [
            self._txn("2026-06-28", "KOPITIAM @ RAFFLES", 999.0,
                      "UOB Card ending 5678"),                 # wrong month
            self._txn("2026-07-02", "STMT IMPORT", 999.0,
                      "UOB Card ending 5678", source="backfill"),
            {"Date": "2026-07-03", "Merchant": "MYSTERY", "Amount": 999.0,
             "Category": "UNCATEGORIZED", "Source": "email",
             "Payment Method": "UOB Card ending 5678"},        # pending
            self._txn("2026-07-04", "KOPITIAM @ RAFFLES", 25.0,
                      "UOB Card ending 5678"),
        ]
        result = self._run(rows)
        pref = next(c for c in result["cards"] if c["card_id"] == "uob-pref")
        pools = {p["pool"]: p for p in pref["pools"]}
        assert pools["contactless"]["spent"] == 25.0
        assert pools["online"]["spent"] == 0.0

    def test_setup_required_when_no_cards(self):
        result = self._run([], cards=[])
        assert result["status"] == "setup_required"

    def test_never_raises(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          side_effect=RuntimeError("db down")):
            result = card_optimiser.get_bonus_pool_status()
        assert result["status"] == "error"
        assert "db down" in result["message"]


class TestGetBonusPoolStatusHandler:
    """Handler: kwargs pass-through + JSON round-trip."""

    def test_defaults_and_roundtrip(self):
        from tools.expense_sheets_tool import handle_get_bonus_pool_status
        with patch("tools.card_optimiser.get_bonus_pool_status",
                   return_value={"status": "ok", "month": "2026-07",
                                 "cards": []}) as mock_fn:
            result = json.loads(handle_get_bonus_pool_status({}))
        assert result["status"] == "ok"
        mock_fn.assert_called_once_with(month=None)

    def test_empty_month_coerced_and_passthrough(self):
        from tools.expense_sheets_tool import handle_get_bonus_pool_status
        with patch("tools.card_optimiser.get_bonus_pool_status",
                   return_value={"status": "ok", "month": "2026-06",
                                 "cards": []}) as mock_fn:
            json.loads(handle_get_bonus_pool_status({"month": ""}))
            json.loads(handle_get_bonus_pool_status({"month": "2026-06"}))
        assert mock_fn.call_args_list[0].kwargs == {"month": None}
        assert mock_fn.call_args_list[1].kwargs == {"month": "2026-06"}


class TestMaybeSendMinSpendNudge:
    """Calendar-month min-spend nudge: one 'met' + one at-risk per card
    per calendar month, deduped via the _min_spend sentinel category."""

    def _run(self, spent_after, amount, txn_date, already=False, cards=None):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          return_value=cards or STRAT_CARDS), \
             patch.object(card_optimiser, "_calendar_month_spend",
                          return_value=spent_after), \
             patch.object(card_optimiser, "_already_nudged",
                          return_value=already) as mock_already, \
             patch.object(card_optimiser, "_record_nudge") as mock_record, \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "77"}) as mock_send:
            result = card_optimiser.maybe_send_min_spend_nudge(
                payment_method="DBS/POSB card ending 1234",
                amount=amount,
                txn_date=txn_date,
            )
        return result, mock_send, mock_record, mock_already

    def test_fires_when_crossing_min(self):
        # 780 before + 50 = 830 crosses the $800 line
        result, mock_send, mock_record, _ = self._run(830.0, 50.0, "2026-07-10")
        assert result["sent"] is True
        assert result["reason"] == "min_spend_100"
        assert "min spend" in mock_send.call_args[0][0]
        mock_record.assert_called_once_with(
            "2026-07", "dbs-yuu", "_min_spend", 100, "", "")

    def test_silent_when_already_met_before(self):
        # 900 before + 50 = 950: the line was crossed earlier, not now
        result, mock_send, _, _ = self._run(950.0, 50.0, "2026-07-10")
        assert result["sent"] is False
        assert result["reason"] == "no_threshold"
        mock_send.assert_not_called()

    def test_at_risk_fires_only_in_last_five_days(self):
        # short of $800 on the 27th (July has 31 days → 4 left) → fires
        result, mock_send, mock_record, _ = self._run(300.0, 20.0, "2026-07-27")
        assert result["sent"] is True
        assert result["reason"] == "min_spend_80"
        assert "short" in mock_send.call_args[0][0]
        mock_record.assert_called_once_with(
            "2026-07", "dbs-yuu", "_min_spend", 80, "", "")

    def test_at_risk_silent_mid_month(self):
        result, mock_send, _, _ = self._run(300.0, 20.0, "2026-07-10")
        assert result["sent"] is False
        assert result["reason"] == "no_threshold"
        mock_send.assert_not_called()

    def test_deduped_once_per_month(self):
        result, mock_send, _, mock_already = self._run(
            830.0, 50.0, "2026-07-10", already=True)
        assert result["sent"] is False
        assert result["reason"] == "already_nudged_this_month"
        mock_already.assert_called_once_with(
            "2026-07", "dbs-yuu", "_min_spend", 100)
        mock_send.assert_not_called()

    def test_no_min_spend_card_is_silent(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          return_value=STRAT_CARDS):
            result = card_optimiser.maybe_send_min_spend_nudge(
                payment_method="UOB Card ending 5678",
                amount=100.0,
                txn_date="2026-07-10",
            )
        assert result["sent"] is False
        assert result["reason"] == "no_min_spend"

    def test_unmapped_payment_method(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          return_value=STRAT_CARDS):
            result = card_optimiser.maybe_send_min_spend_nudge(
                payment_method="PayLah! Wallet (Mobile ending 9876)",
                amount=10.0,
                txn_date="2026-07-10",
            )
        assert result["sent"] is False
        assert result["reason"] == "unmapped_payment_method"

    def test_never_raises(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          side_effect=RuntimeError("supabase down")):
            result = card_optimiser.maybe_send_min_spend_nudge(
                payment_method="DBS/POSB card ending 1234",
                amount=10.0,
                txn_date="2026-07-10",
            )
        assert result["sent"] is False
        assert result["reason"] == "exception"
        assert "supabase down" in result["error"]


class TestMaybeSendSteerNudge:
    """Wrong-card steering: partner→yuu, non-partner-on-yuu→Preferred,
    online→Revo (with the Vantage big-one-off exemption), deduped once
    per merchant pattern per calendar month. Steering v2 adds the
    min-spend-priority override and the pool-headroom check — the
    permissive defaults below (pool status ok everywhere, calendar spend
    well above yuu's $800 min) keep the v1 tests on the original paths."""

    def _run(self, merchant, payment_method, amount=20.0, already=False,
             txn_date="2026-07-10", pool_status=None, calendar_spend=2000.0):
        from tools import card_optimiser
        if pool_status is None:
            pool_status = {"status": "ok", "month": "2026-07", "cards": []}
        with patch.object(card_optimiser, "read_cards",
                          return_value=STRAT_CARDS), \
             patch.object(card_optimiser, "get_bonus_pool_status",
                          return_value=pool_status), \
             patch.object(card_optimiser, "_calendar_month_spend",
                          return_value=calendar_spend), \
             patch.object(card_optimiser, "_already_nudged",
                          return_value=already) as mock_already, \
             patch.object(card_optimiser, "_record_nudge") as mock_record, \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "88"}) as mock_send:
            result = card_optimiser.maybe_send_steer_nudge(
                merchant=merchant,
                payment_method=payment_method,
                amount=amount,
                txn_date=txn_date,
            )
        return result, mock_send, mock_record, mock_already

    def test_partner_merchant_on_wrong_card_steers_to_yuu(self):
        result, mock_send, mock_record, _ = self._run(
            "GOPAY-GOJEK", "UOB Card ending 5678")
        assert result["sent"] is True
        assert result["steer_to"] == "dbs-yuu"
        assert "DBS Yuu Visa" in mock_send.call_args[0][0]
        mock_record.assert_called_once_with(
            "2026-07", "dbs-yuu", "_steer:GOJEK", 0, "", "uob-pref")

    def test_partner_merchant_on_yuu_is_silent(self):
        result, mock_send, _, _ = self._run(
            "GOPAY-GOJEK", "DBS/POSB card ending 1234")
        assert result["sent"] is False
        assert result["reason"] == "no_steer_rule"
        mock_send.assert_not_called()

    def test_non_partner_on_yuu_steers_to_preferred(self):
        # calendar_spend default (2000 > the $800 min) keeps yuu NOT
        # at-risk — otherwise steering v2 would return min_spend_priority.
        result, mock_send, _, _ = self._run(
            "SHENG SIONG SUPERMARKET", "DBS/POSB card ending 1234")
        assert result["sent"] is True
        assert result["steer_to"] == "uob-pref"
        assert "not a yuu partner" in mock_send.call_args[0][0]

    def test_non_partner_on_preferred_is_silent(self):
        result, _, _, _ = self._run(
            "SHENG SIONG SUPERMARKET", "UOB Card ending 5678")
        assert result["sent"] is False
        assert result["reason"] == "no_steer_rule"

    def test_online_merchant_steers_to_revo(self):
        result, mock_send, _, _ = self._run(
            "SHOPEE SINGAPORE MP", "DBS/POSB card ending 4321", amount=42.0)
        assert result["sent"] is True
        assert result["steer_to"] == "hsbc-revo"

    def test_big_oneoff_on_vantage_is_exempt(self):
        result, mock_send, _, _ = self._run(
            "SHOPEE SINGAPORE MP", "DBS/POSB card ending 4321", amount=900.0)
        assert result["sent"] is False
        assert result["reason"] == "no_steer_rule"
        mock_send.assert_not_called()

    def test_chagee_message_mentions_the_app(self):
        result, mock_send, _, _ = self._run(
            "CHAGEE", "UOB Card ending 5678")
        assert result["sent"] is True
        assert "CHAGEE app" in mock_send.call_args[0][0]

    def test_deduped_once_per_pattern_per_month(self):
        result, mock_send, _, mock_already = self._run(
            "GOPAY-GOJEK", "UOB Card ending 5678", already=True)
        assert result["sent"] is False
        assert result["reason"] == "already_nudged_this_month"
        mock_already.assert_called_once_with(
            "2026-07", "dbs-yuu", "_steer:GOJEK", 0)
        mock_send.assert_not_called()

    def test_unmapped_payment_method_is_silent(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          return_value=STRAT_CARDS):
            result = card_optimiser.maybe_send_steer_nudge(
                merchant="GOPAY-GOJEK",
                payment_method="PayLah! Wallet (Mobile ending 9876)",
                amount=10.0,
                txn_date="2026-07-10",
            )
        assert result["sent"] is False
        assert result["reason"] == "unmapped_payment_method"

    def test_never_raises(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "read_cards",
                          side_effect=RuntimeError("boom")):
            result = card_optimiser.maybe_send_steer_nudge(
                merchant="GOPAY-GOJEK",
                payment_method="UOB Card ending 5678",
                amount=10.0,
                txn_date="2026-07-10",
            )
        assert result["sent"] is False
        assert result["reason"] == "exception"

    # --- Steering v2: min-spend priority + headroom (2026-07-30 incident) ---

    @staticmethod
    def _pools(pref_tap="ok", pref_online="ok", revo="ok"):
        return {
            "status": "ok", "month": "2026-07",
            "cards": [
                {"card_id": "uob-pref", "display_name": "UOB Preferred Visa",
                 "pools": [
                     {"pool": "contactless", "spent": 0.0, "cap": 600.0,
                      "status": pref_tap},
                     {"pool": "online", "spent": 0.0, "cap": 600.0,
                      "status": pref_online},
                 ]},
                {"card_id": "hsbc-revo",
                 "display_name": "HSBC Revolution Visa",
                 "pools": [{"pool": "bonus", "spent": 0.0, "cap": 1000.0,
                            "status": revo}]},
            ],
        }

    def test_incident_non_partner_on_at_risk_yuu_is_silent(self):
        # The 2026-07-30 11:48 incident pair: FAIRPRICE tap ON dbs-yuu
        # while yuu is $485.89 short of the $800 min with 1 day left.
        # The money is going exactly where it's needed — no steer bubble.
        result, mock_send, mock_record, _ = self._run(
            "FAIRPRICE FINEST", "DBS/POSB card ending 1234",
            txn_date="2026-07-30", calendar_spend=314.11)
        assert result["sent"] is False
        assert result["reason"] == "min_spend_priority"
        mock_send.assert_not_called()
        mock_record.assert_not_called()

    def test_at_risk_card_overrides_steer_target(self):
        # SHOPEE on Preferred would steer to Revo, but yuu is $300 short
        # with 3 days left → override to yuu, message names the shortfall,
        # dedup keys on the FINAL target.
        result, mock_send, mock_record, _ = self._run(
            "SHOPEE SINGAPORE MP", "UOB Card ending 5678",
            txn_date="2026-07-28", calendar_spend=500.0)
        assert result["sent"] is True
        assert result["steer_to"] == "dbs-yuu"
        text = mock_send.call_args[0][0]
        assert "put this on DBS Yuu Visa instead" in text
        assert "$300.00 short" in text
        assert "3 days left" in text
        mock_record.assert_called_once_with(
            "2026-07", "dbs-yuu", "_steer:SHOPEE", 0, "", "uob-pref")

    def test_capped_target_falls_back_to_next_candidate(self):
        # Preferred's tap pool capped → the tap steer swaps in Revo.
        result, mock_send, mock_record, _ = self._run(
            "FAIRPRICE FINEST", "DBS/POSB card ending 1234",
            pool_status=self._pools(pref_tap="capped"))
        assert result["sent"] is True
        assert result["steer_to"] == "hsbc-revo"
        text = mock_send.call_args[0][0]
        assert "HSBC Revolution Visa" in text
        assert "UOB Preferred Visa" not in text
        assert "not a yuu partner" in text
        mock_record.assert_called_once_with(
            "2026-07", "hsbc-revo", "_steer:FAIRPRICE", 0, "", "dbs-yuu")

    def test_all_candidates_capped_is_silent(self):
        result, mock_send, mock_record, _ = self._run(
            "FAIRPRICE FINEST", "DBS/POSB card ending 1234",
            pool_status=self._pools(pref_tap="capped", revo="capped"))
        assert result["sent"] is False
        assert result["reason"] == "steer_target_capped"
        mock_send.assert_not_called()
        mock_record.assert_not_called()

    def test_pool_status_error_fails_open(self):
        # A pool-read error must degrade to v1 behavior, never suppress.
        result, mock_send, _, _ = self._run(
            "FAIRPRICE FINEST", "DBS/POSB card ending 1234",
            pool_status={"status": "error", "message": "supabase down"})
        assert result["sent"] is True
        assert result["steer_to"] == "uob-pref"
        assert "tap UOB Preferred Visa" in mock_send.call_args[0][0]


# --- review_card_efficiency (the month-end scorecard) ---

def _scorecard_cards():
    return [
        {"card_id": "dbs-yuu", "display_name": "DBS Yuu Visa",
         "payment_method_pattern": "dbs/posb card ending 1234",
         "cycle_start_day": 24, "min_spend_bonus": 0.0, "bonus_cap": 0.0,
         "base_mpd": 0.1, "notes": ""},
        {"card_id": "uob-pref", "display_name": "UOB Preferred Visa",
         "payment_method_pattern": "uob card ending 5678",
         "cycle_start_day": 13, "min_spend_bonus": 0.0, "bonus_cap": 600.0,
         "base_mpd": 0.4, "notes": ""},
        {"card_id": "hsbc-revo", "display_name": "HSBC Revolution Visa",
         "payment_method_pattern": "hsbc card ending 1357",
         "cycle_start_day": 20, "min_spend_bonus": 0.0, "bonus_cap": 1000.0,
         "base_mpd": 0.4, "notes": ""},
        {"card_id": "dbs-vantage", "display_name": "DBS Vantage Visa",
         "payment_method_pattern": "vantage",
         "cycle_start_day": 23, "min_spend_bonus": 0.0, "bonus_cap": 0.0,
         "base_mpd": 1.5, "notes": ""},
    ]


def _scorecard_strats():
    return [
        {"category": "Personal - Travel", "primary_card_id": "dbs-yuu",
         "primary_earn_rate": 10.0, "primary_cap": 823.0,
         "fallback_card_id": "", "fallback_earn_rate": 0.0,
         "promo_active_until": "", "notes": ""},
        {"category": "Insurance", "primary_card_id": "hsbc-revo",
         "primary_earn_rate": 1.0, "primary_cap": 1000.0,
         "fallback_card_id": "", "fallback_earn_rate": 0.0,
         "promo_active_until": "", "notes": ""},
        {"category": "_default", "primary_card_id": "uob-pref",
         "primary_earn_rate": 4.0, "primary_cap": 600.0,
         "fallback_card_id": "", "fallback_earn_rate": 0.0,
         "promo_active_until": "", "notes": ""},
    ]


def _sc_txn(**over):
    txn = {"Date": "2026-07-10", "Merchant": "SOMEWHERE", "Amount": 10.0,
           "Category": "Personal - Travel", "Source": "email",
           "Payment Method": "DBS Vantage Visa", "txn_id": "txn_20260710_001"}
    txn.update(over)
    return txn


class TestReviewCardEfficiency:
    """The Jul 2026 scorecard audit: category strategies are too coarse
    (Grab scored as dbs-yuu @10 when it isn't a yuu partner) and
    off-strategy cards scored 0 mpd (Vantage's flat 1.5 is real miles) —
    both inflated 'miles left on the table'."""

    def _run(self, txns, cards=None):
        from tools import card_optimiser
        with patch.object(card_optimiser, "_check_setup", return_value=None), \
             patch.object(card_optimiser, "read_cards",
                          return_value=cards or _scorecard_cards()), \
             patch.object(card_optimiser, "read_card_strategy",
                          return_value=_scorecard_strats()), \
             patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_transactions.return_value = txns
            return card_optimiser.review_card_efficiency("2026-07")

    def test_grab_overridden_to_online_card_not_yuu(self):
        # Grab matches _ONLINE_4MPD_PATTERNS, NOT the yuu partners — the
        # steer nudge already knew this; the scorecard now agrees.
        result = self._run([_sc_txn(Merchant="Grab* A-9LHWK6", Amount=31.70)])
        (sub,) = result["transactions_suboptimal"]
        assert sub["optimal_card"] == "hsbc-revo"
        assert sub["optimal_earn_rate"] == 4.0
        assert sub["actual_earn_rate"] == 1.5      # Vantage base, not 0
        # 31.70 × (4 − 1.5) ≈ 79.25 — tolerance, not equality: the code
        # computes a*4 − a*1.5, which is not bit-identical to a*2.5
        assert abs(sub["miles_lost"] - 79.25) < 0.06

    def test_yuu_partner_keeps_category_primary(self):
        result = self._run([_sc_txn(Merchant="Gopay-Gojek", Amount=33.50)])
        (sub,) = result["transactions_suboptimal"]
        assert sub["optimal_card"] == "dbs-yuu"
        assert sub["optimal_earn_rate"] == 10.0
        assert abs(sub["miles_lost"] - 284.75) < 0.06

    def test_not_partner_tap_on_recommended_card_loses_nothing(self):
        # WATSONS in a yuu-primary category, tapped on uob-pref — exactly
        # what the steer nudge recommends. Zero miles lost.
        result = self._run([_sc_txn(Merchant="WATSONS - THE STAR VISTA",
                                    Amount=40.65,
                                    **{"Payment Method": "UOB Card ending 5678"})])
        assert result["transactions_suboptimal"] == []
        assert result["miles_left_on_table"] == 0.0

    def test_optimal_is_floored_at_actual(self):
        # Vantage base 1.5 beats the Insurance row's 1.0 — the model must
        # not report negative loss (nor punish the better card).
        result = self._run([_sc_txn(Merchant="INCOME INSURANCE",
                                    Category="Insurance", Amount=141.32)])
        assert result["transactions_suboptimal"] == []
        assert result["miles_left_on_table"] == 0.0
        assert result["miles_earned_actual"] == round(141.32 * 1.5, 1)

    def test_pot_internal_rows_skipped_not_unmapped(self):
        result = self._run([_sc_txn(Merchant="SHENG SIONG", Amount=6.95,
                                    **{"Payment Method": "YouTrip Card"})])
        assert result["unmapped_payment_methods"] == 0
        assert result["miles_earned_optimal"] == 0.0

    def test_verbatim_lines_use_display_names(self):
        result = self._run([_sc_txn(Merchant="Grab* A-9LHWK6", Amount=31.70)])
        (line,) = result["top_missed_lines"]
        assert "used DBS Vantage Visa" in line
        assert "should have been HSBC Revolution Visa" in line
        assert "Grab* A-9LHWK6 ($31.70)" in line
        assert "miles" in result["summary_line"]

    def test_suboptimal_sorted_by_miles_lost_desc(self):
        result = self._run([
            _sc_txn(Merchant="Grab* SMALL", Amount=10.0,
                    txn_id="txn_20260710_001"),
            _sc_txn(Merchant="Grab* BIG", Amount=100.0,
                    txn_id="txn_20260710_002"),
        ])
        lost = [s["miles_lost"] for s in result["transactions_suboptimal"]]
        assert lost == sorted(lost, reverse=True)

    def test_nonpartner_on_yuu_card_reports_the_trap(self):
        # Adversarial-review catch (high): Grab paid ON the yuu card was
        # credited yuu @10 by the strategy row, and the floor then lifted
        # optimal back onto those phantom miles — zero loss reported for
        # the exact 0.25%-trap the scorecard exists to catch.
        result = self._run([_sc_txn(
            Merchant="Grab* A-9LHWK6", Amount=31.70,
            **{"Payment Method": "DBS/POSB card ending 1234"})])
        (sub,) = result["transactions_suboptimal"]
        assert sub["actual_card"] == "dbs-yuu"
        assert sub["actual_earn_rate"] == 0.1      # yuu base, not @10
        assert sub["optimal_card"] == "hsbc-revo"
        assert abs(sub["miles_lost"] - 123.63) < 0.1

    def test_unknown_merchant_on_yuu_primary_gets_benefit_of_doubt(self):
        # No pattern knows this merchant — the strategy row stands and
        # yuu-on-yuu reports no loss (pre-existing behavior preserved).
        result = self._run([_sc_txn(
            Merchant="SOME KOPITIAM", Amount=20.0,
            **{"Payment Method": "DBS/POSB card ending 1234"})])
        assert result["transactions_suboptimal"] == []

    def test_steered_optimal_bounded_by_bonus_cap(self):
        # Adversarial-review catch: the pattern-steered optimal awarded
        # unlimited 4 mpd volume on a $600 bonus pool. Third $400 WATSONS
        # txn exceeds uob-pref's modeled pool -> base rate -> floored by
        # Vantage's 1.5 -> no loss entry.
        txns = [
            _sc_txn(Merchant="WATSONS ONE", Amount=400.0,
                    txn_id="txn_20260710_001"),
            _sc_txn(Merchant="WATSONS TWO", Amount=400.0, Date="2026-07-11",
                    txn_id="txn_20260711_001"),
            _sc_txn(Merchant="WATSONS TRE", Amount=400.0, Date="2026-07-12",
                    txn_id="txn_20260712_001"),
        ]
        result = self._run(txns)
        subs = result["transactions_suboptimal"]
        assert len(subs) == 2
        assert all(s["optimal_earn_rate"] == 4.0 for s in subs)
        assert {s["txn_id"] for s in subs} == {
            "txn_20260710_001", "txn_20260711_001"}

    def test_min_spend_endorsement_suppresses_loss(self):
        # Adversarial-review catch: the nudge deliberately blesses routing
        # onto a card still short of its min spend with <=5 days left
        # ("min_spend_priority") — the scorecard must not scold it.
        cards = _scorecard_cards()
        for c in cards:
            if c["card_id"] == "dbs-yuu":
                c["min_spend_bonus"] = 800.0
        txn = _sc_txn(Merchant="FAIRPRICE FINEST", Amount=50.0,
                      Category="Groceries", Date="2026-07-28",
                      **{"Payment Method": "DBS/POSB card ending 1234"})
        result = self._run([txn], cards=cards)
        assert result["transactions_suboptimal"] == []
        assert result["miles_left_on_table"] == 0.0

    def test_min_spend_endorsement_needs_month_end_window(self):
        # Same txn mid-month: no endorsement — the loss is real.
        cards = _scorecard_cards()
        for c in cards:
            if c["card_id"] == "dbs-yuu":
                c["min_spend_bonus"] = 800.0
        txn = _sc_txn(Merchant="FAIRPRICE FINEST", Amount=50.0,
                      Category="Groceries", Date="2026-07-10",
                      **{"Payment Method": "DBS/POSB card ending 1234"})
        result = self._run([txn], cards=cards)
        (sub,) = result["transactions_suboptimal"]
        assert sub["optimal_card"] == "uob-pref"
        assert sub["actual_earn_rate"] == 0.1


class TestPlanMonthLines:
    """plan_lines collapse rows identical to _default — the Jul 2026 recap
    printed 14 near-identical UOB Preferred lines."""

    def _run(self, strats):
        from tools import card_optimiser
        with patch.object(card_optimiser, "_check_setup", return_value=None), \
             patch.object(card_optimiser, "_lazy_revert_expired_promos",
                          return_value=[]), \
             patch.object(card_optimiser, "read_cards",
                          return_value=_scorecard_cards()), \
             patch.object(card_optimiser, "read_card_strategy",
                          return_value=strats), \
             patch.object(card_optimiser, "_spend_in_cycle",
                          return_value=0.0):
            return card_optimiser.plan_month()

    def test_default_matching_rows_collapse(self):
        strats = [
            {"category": "Groceries", "primary_card_id": "uob-pref",
             "primary_earn_rate": 4.0, "primary_cap": 600.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": ""},
            {"category": "Personal - Travel", "primary_card_id": "dbs-yuu",
             "primary_earn_rate": 10.0, "primary_cap": 823.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": ""},
            {"category": "_default", "primary_card_id": "uob-pref",
             "primary_earn_rate": 4.0, "primary_cap": 600.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": ""},
        ]
        result = self._run(strats)
        assert result["plan_lines"] == [
            "- Personal - Travel → DBS Yuu Visa @10 mpd, cap $823",
            "- Everything else → UOB Preferred Visa @4 mpd, cap $600",
        ]

    def test_promo_suffix_and_uncapped(self):
        strats = [
            {"category": "Dining", "primary_card_id": "dbs-vantage",
             "primary_earn_rate": 1.5, "primary_cap": 0.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": "2026-08-31"},
            {"category": "_default", "primary_card_id": "uob-pref",
             "primary_earn_rate": 4.0, "primary_cap": 600.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": ""},
        ]
        result = self._run(strats)
        assert result["plan_lines"][0] == (
            "- Dining → DBS Vantage Visa @1.5 mpd, uncapped "
            "(promo until 2026-08-31)")

    def test_full_plan_still_returned(self):
        strats = [
            {"category": "_default", "primary_card_id": "uob-pref",
             "primary_earn_rate": 4.0, "primary_cap": 600.0,
             "fallback_card_id": "", "fallback_earn_rate": 0.0,
             "promo_active_until": ""},
        ]
        result = self._run(strats)
        assert len(result["plan"]) == 1
        assert result["plan_lines"] == [
            "- Everything else → UOB Preferred Visa @4 mpd, cap $600"]


class TestReadCardsBaseMpd:
    def test_base_mpd_defaults_to_zero_pre_migration(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_cards_records.return_value = [
                {"card_id": "dbs-vantage", "display_name": "DBS Vantage",
                 "payment_method_pattern": "vantage", "cycle_start_day": 23}]
            cards = card_optimiser.read_cards()
        assert cards[0]["base_mpd"] == 0.0

    def test_base_mpd_read_when_present(self):
        from tools import card_optimiser
        with patch.object(card_optimiser, "supabase_client") as mock_db:
            mock_db.read_cards_records.return_value = [
                {"card_id": "dbs-vantage", "display_name": "DBS Vantage",
                 "payment_method_pattern": "vantage", "cycle_start_day": 23,
                 "base_mpd": "1.5"}]
            cards = card_optimiser.read_cards()
        assert cards[0]["base_mpd"] == 1.5

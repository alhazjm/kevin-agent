"""Google Sheets is optional; Supabase is not.

The universal `check_fn` used to require the Sheets env pair as well, which
meant a new install had to stand up a Google Cloud project, a service
account and a spreadsheet before the agent could log a single expense — for
a backup copy of data it did not have yet. Nothing reads the Sheet at
runtime; only two tools write to it.

These tests pin the split so a future refactor cannot quietly put Google
Cloud back on the critical path.
"""

import json
from unittest.mock import patch

import pytest


_VARS = ("GSPREAD_SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON",
         "SUPABASE_URL", "SUPABASE_SERVICE_KEY")


@pytest.fixture(autouse=True)
def mock_env():
    """Start every test with none of the four set, and RESTORE afterwards.

    Restoring matters: these tests deliberately unset the Supabase pair, and
    the suite shares one process. Leaking that state would fail whichever
    module happened to run next, as an ordering-dependent flake.
    """
    import os
    saved = {v: os.environ.get(v) for v in _VARS}
    for v in _VARS:
        os.environ.pop(v, None)
    try:
        yield
    finally:
        for v, original in saved.items():
            if original is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = original


def _set(**kwargs):
    import os
    for k, v in kwargs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestToolGate:
    def test_supabase_alone_is_enough(self):
        """The minimum usable state: Supabase only, no Google anything."""
        from tools.expense_sheets_tool import _sheets_configured

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
        assert _sheets_configured() is True

    def test_sheets_alone_is_not_enough(self):
        """Supabase is the ledger of record — there is no Sheets-only mode."""
        from tools.expense_sheets_tool import _sheets_configured

        _set(GSPREAD_SPREADSHEET_ID="sheet", GOOGLE_SERVICE_ACCOUNT_JSON="{}")
        assert _sheets_configured() is False

    def test_nothing_configured_is_not_enough(self):
        from tools.expense_sheets_tool import _sheets_configured

        assert _sheets_configured() is False

    def test_both_configured_still_works(self):
        """The existing deployment sets all four — behaviour is unchanged."""
        from tools.expense_sheets_tool import _sheets_configured

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k",
             GSPREAD_SPREADSHEET_ID="sheet", GOOGLE_SERVICE_ACCOUNT_JSON="{}")
        assert _sheets_configured() is True


class TestSheetExportGate:
    def test_export_gate_needs_both_sheets_vars(self):
        from tools.expense_sheets_tool import _sheet_export_configured

        assert _sheet_export_configured() is False
        _set(GSPREAD_SPREADSHEET_ID="sheet")
        assert _sheet_export_configured() is False
        _set(GOOGLE_SERVICE_ACCOUNT_JSON="{}")
        assert _sheet_export_configured() is True

    def test_export_backup_is_a_quiet_no_op_when_unconfigured(self):
        """setup_required, not error — an unconfigured Sheet is supported."""
        from tools.expense_sheets_tool import handle_export_sheet_backup

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
        result = json.loads(handle_export_sheet_backup({}))
        assert result["status"] == "setup_required"
        assert "optional" in result["message"].lower()

    def test_year_archive_is_a_quiet_no_op_when_unconfigured(self):
        from tools.expense_sheets_tool import handle_archive_year_snapshot

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
        result = json.loads(handle_archive_year_snapshot({}))
        assert result["status"] == "setup_required"

    def test_export_does_not_touch_supabase_when_unconfigured(self):
        """The gate short-circuits before any read — no wasted API calls."""
        from tools import expense_sheets_tool

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k")
        with patch.object(expense_sheets_tool.supabase_client,
                          "read_all_transaction_rows") as reader:
            expense_sheets_tool.handle_export_sheet_backup({})
            reader.assert_not_called()

    def test_export_runs_normally_when_configured(self):
        """With Sheets configured the handler behaves exactly as before."""
        from tools import expense_sheets_tool

        _set(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_KEY="k",
             GSPREAD_SPREADSHEET_ID="sheet", GOOGLE_SERVICE_ACCOUNT_JSON="{}")
        with patch.object(expense_sheets_tool.supabase_client,
                          "read_all_transaction_rows", return_value=[]), \
             patch.object(expense_sheets_tool.supabase_client,
                          "read_all_budget_rows", return_value=[]), \
             patch.object(expense_sheets_tool.sheets_client,
                          "write_sheet_snapshot",
                          return_value={"status": "ok"}) as writer:
            result = json.loads(expense_sheets_tool.handle_export_sheet_backup({}))
            writer.assert_called_once()
            assert result["status"] == "ok"

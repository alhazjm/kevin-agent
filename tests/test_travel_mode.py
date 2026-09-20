"""Tests for tools/travel_mode.py + the 3 handlers in expense_sheets_tool.py.

Data access is mocked at the supabase_client seam (migration 3b): tests
patch the module's own `supabase_client` attribute and configure
`read_travel_mode_records` / `read_all_transaction_rows` /
`read_trip_nudge_log` / `append_trip_nudge` / `find_transaction_by_id` /
`edit_transaction` return values.

Covers:
  - kv-pair / budget-map parsing
  - bucket tag parse + write (round-trip, idempotent overwrite)
  - active-trip lookup with overlap warning
  - trip budget status (per-bucket math, untagged surfacing)
  - set_trip_bucket (Notes-only update)
  - maybe_send_trip_bucket_nudge (fire/dedup/budget-revision/silent
    paths, exception swallowing)
  - the 3 handler wrappers (arg pass-through + JSON encoding)
"""

import json
import os
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_env():
    with patch.dict(os.environ, {
        "GSPREAD_SPREADSHEET_ID": "test-sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "/tmp/fake-sa.json",
    }):
        yield


SAMPLE_TRIP = {
    "start_date": date(2026, 4, 27),
    "end_date": date(2026, 5, 3),
    "label": "ID Apr 2026",
    "trip_category": "Travel - ID 2026-04",
    "budget_map_raw": "food=450; transport=300; flight=800; misc=450",
    "budget_map": {"food": 450.0, "transport": 300.0, "flight": 800.0, "misc": 450.0},
    "total_budget": 2000.0,
    "notes": "",
}


class TestParseKvMap:
    def test_basic(self):
        from tools.travel_mode import _parse_kv_map
        assert _parse_kv_map("a=1; b=2") == {"a": "1", "b": "2"}

    def test_strips_whitespace(self):
        from tools.travel_mode import _parse_kv_map
        assert _parse_kv_map("  food = 450 ; transport=300  ") == {
            "food": "450",
            "transport": "300",
        }

    def test_skips_malformed(self):
        from tools.travel_mode import _parse_kv_map
        # No `=`, empty value still allowed.
        assert _parse_kv_map("food=450; bare; transport=") == {
            "food": "450",
            "transport": "",
        }

    def test_empty(self):
        from tools.travel_mode import _parse_kv_map
        assert _parse_kv_map("") == {}
        assert _parse_kv_map(None) == {}


class TestParseBudgetMap:
    def test_basic(self):
        from tools.travel_mode import _parse_budget_map
        assert _parse_budget_map("food=450; transport=300") == {
            "food": 450.0,
            "transport": 300.0,
        }

    def test_skips_non_numeric(self):
        from tools.travel_mode import _parse_budget_map
        assert _parse_budget_map("food=450; junk=abc; transport=300") == {
            "food": 450.0,
            "transport": 300.0,
        }

    def test_lowercases_keys(self):
        from tools.travel_mode import _parse_budget_map
        assert _parse_budget_map("FOOD=450; Transport=300") == {
            "food": 450.0,
            "transport": 300.0,
        }


class TestBucketTag:
    def test_parse_present(self):
        from tools.travel_mode import _parse_bucket_from_notes
        assert _parse_bucket_from_notes("[bucket:food] warung") == "food"

    def test_parse_anywhere(self):
        from tools.travel_mode import _parse_bucket_from_notes
        # Spec writes prefix at start, but parse matches anywhere defensively.
        assert _parse_bucket_from_notes("orig: IDR 100 [bucket:transport]") == "transport"

    def test_parse_absent(self):
        from tools.travel_mode import _parse_bucket_from_notes
        assert _parse_bucket_from_notes("plain notes") is None
        assert _parse_bucket_from_notes("") is None

    def test_parse_lowercase(self):
        from tools.travel_mode import _parse_bucket_from_notes
        assert _parse_bucket_from_notes("[bucket:FOOD]") == "food"

    def test_apply_to_empty(self):
        from tools.travel_mode import _apply_bucket_tag
        assert _apply_bucket_tag("", "food") == "[bucket:food]"

    def test_apply_prepends(self):
        from tools.travel_mode import _apply_bucket_tag
        assert _apply_bucket_tag("warung kopi", "food") == "[bucket:food] warung kopi"

    def test_apply_replaces_existing(self):
        from tools.travel_mode import _apply_bucket_tag
        # Replace the tag, keep the rest.
        result = _apply_bucket_tag(
            "[bucket:food] orig: IDR 184000 (frankfurter)", "activities"
        )
        assert result == "[bucket:activities] orig: IDR 184000 (frankfurter)"

    def test_apply_idempotent(self):
        from tools.travel_mode import _apply_bucket_tag
        once = _apply_bucket_tag("warung", "food")
        twice = _apply_bucket_tag(once, "food")
        assert twice == once

    def test_apply_lowercases(self):
        from tools.travel_mode import _apply_bucket_tag
        assert _apply_bucket_tag("", "FOOD") == "[bucket:food]"


class TestTripTag:
    """Reader/writer for the `[trip:<label>]` Notes encoding (writer:
    `_apply_trip_tag`; they change together). Unlike bucket tags, labels
    are stored VERBATIM (spaces, case) and an existing tag is replaced in
    place so [bucket:x] / orig: content is never reordered."""

    def test_parse_present(self):
        from tools.travel_mode import _parse_trip_from_notes
        assert _parse_trip_from_notes("[trip:ID Apr 2026] topup") == "ID Apr 2026"

    def test_parse_absent(self):
        from tools.travel_mode import _parse_trip_from_notes
        assert _parse_trip_from_notes("plain notes") is None
        assert _parse_trip_from_notes("") is None

    def test_parse_label_verbatim(self):
        from tools.travel_mode import _parse_trip_from_notes
        # No lowercasing — labels keep their case (matching is the
        # caller's job, case-insensitive against travel_mode rows).
        assert _parse_trip_from_notes("[trip:JP Trip]") == "JP Trip"

    def test_apply_to_empty(self):
        from tools.travel_mode import _apply_trip_tag
        assert _apply_trip_tag("", "ID Apr 2026") == "[trip:ID Apr 2026]"

    def test_apply_prepends_before_existing_content(self):
        from tools.travel_mode import _apply_trip_tag
        assert _apply_trip_tag("orig: MYR 300 @ 0.32", "ID Apr 2026") == \
            "[trip:ID Apr 2026] orig: MYR 300 @ 0.32"

    def test_apply_replaces_existing_tag(self):
        from tools.travel_mode import _apply_trip_tag
        assert _apply_trip_tag("[trip:old label] topup", "New Label") == \
            "[trip:New Label] topup"

    def test_apply_replaces_in_place_preserving_position(self):
        from tools.travel_mode import _apply_trip_tag
        # Tag mid-string keeps its position — surrounding content is
        # never reordered.
        notes = "[bucket:misc] [trip:old] orig: IDR 500000 @ 0.000086"
        assert _apply_trip_tag(notes, "ID Apr 2026") == \
            "[bucket:misc] [trip:ID Apr 2026] orig: IDR 500000 @ 0.000086"

    def test_apply_leaves_bucket_and_orig_untouched(self):
        from tools.travel_mode import _apply_trip_tag
        notes = "[bucket:food] orig: THB 1200 @ 0.037"
        assert _apply_trip_tag(notes, "TH Sep 2026") == \
            "[trip:TH Sep 2026] [bucket:food] orig: THB 1200 @ 0.037"

    def test_apply_idempotent(self):
        from tools.travel_mode import _apply_trip_tag
        once = _apply_trip_tag("topup", "ID Apr 2026")
        assert _apply_trip_tag(once, "ID Apr 2026") == once

    def test_apply_empty_label_is_noop(self):
        from tools.travel_mode import _apply_trip_tag
        assert _apply_trip_tag("keep me", "") == "keep me"

    def test_round_trip(self):
        from tools.travel_mode import _apply_trip_tag, _parse_trip_from_notes
        assert _parse_trip_from_notes(
            _apply_trip_tag("orig: MYR 300 @ 0.32", "ID Apr 2026")
        ) == "ID Apr 2026"


class TestReadTravelModeRows:
    """read_travel_mode_rows parses supabase travel_mode records — ISO date
    strings and budget_map text — into typed trip rows."""

    def test_parses_supabase_records(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.read_travel_mode_records.return_value = [{
            "id": 1,
            "start_date": "2026-04-27",
            "end_date": "2026-05-03",
            "label": "ID Apr 2026",
            "trip_category": "Travel - ID 2026-04",
            "budget_map": "food=450; transport=300",
            "total_budget": 2000,
            "notes": "",
        }]
        with patch.object(travel_mode, "supabase_client", mock_db):
            rows = travel_mode.read_travel_mode_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["start_date"] == date(2026, 4, 27)
        assert row["end_date"] == date(2026, 5, 3)
        assert row["label"] == "ID Apr 2026"
        assert row["budget_map"] == {"food": 450.0, "transport": 300.0}
        assert row["total_budget"] == 2000.0

    def test_skips_rows_missing_label_or_dates(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.read_travel_mode_records.return_value = [
            {"id": 1, "start_date": "2026-04-27", "end_date": "2026-05-03",
             "label": "", "trip_category": "X", "budget_map": "",
             "total_budget": 0, "notes": ""},          # no label
            {"id": 2, "start_date": "", "end_date": "2026-05-03",
             "label": "no-start", "trip_category": "X", "budget_map": "",
             "total_budget": 0, "notes": ""},          # unparseable start
            {"id": 3, "start_date": "2026-04-27", "end_date": "2026-05-03",
             "label": "keeper", "trip_category": "X", "budget_map": "",
             "total_budget": 0, "notes": ""},
        ]
        with patch.object(travel_mode, "supabase_client", mock_db):
            rows = travel_mode.read_travel_mode_rows()
        assert [r["label"] for r in rows] == ["keeper"]

    def test_empty_table_returns_empty_list(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.read_travel_mode_records.return_value = []
        with patch.object(travel_mode, "supabase_client", mock_db):
            assert travel_mode.read_travel_mode_rows() == []


class TestActiveTrip:
    def test_in_range(self):
        from tools.travel_mode import _find_active_trip
        match = _find_active_trip([SAMPLE_TRIP], date(2026, 4, 30))
        assert match is not None
        assert match["label"] == "ID Apr 2026"

    def test_at_start(self):
        from tools.travel_mode import _find_active_trip
        assert _find_active_trip([SAMPLE_TRIP], date(2026, 4, 27)) is not None

    def test_at_end(self):
        from tools.travel_mode import _find_active_trip
        assert _find_active_trip([SAMPLE_TRIP], date(2026, 5, 3)) is not None

    def test_out_of_range(self):
        from tools.travel_mode import _find_active_trip
        assert _find_active_trip([SAMPLE_TRIP], date(2026, 4, 26)) is None
        assert _find_active_trip([SAMPLE_TRIP], date(2026, 5, 4)) is None

    def test_overlap_picks_most_recent(self):
        from tools.travel_mode import _find_active_trip
        # Two overlapping rows; the second (most recently added) wins.
        early = dict(SAMPLE_TRIP, label="early", start_date=date(2026, 4, 25),
                     end_date=date(2026, 5, 1))
        late = dict(SAMPLE_TRIP, label="late", start_date=date(2026, 4, 30),
                    end_date=date(2026, 5, 5))
        match = _find_active_trip([early, late], date(2026, 4, 30))
        assert match["label"] == "late"


class TestGetActiveTravelMode:
    def test_active(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.get_active_travel_mode(as_of="2026-04-30")
        assert result["active"] is True
        assert result["label"] == "ID Apr 2026"
        assert result["trip_category"] == "Travel - ID 2026-04"
        assert result["overlap_warning"] is None

    def test_inactive(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.get_active_travel_mode(as_of="2026-04-26")
        assert result["active"] is False
        assert result["all_rows_count"] == 1

    def test_no_rows(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows", return_value=[]):
            result = travel_mode.get_active_travel_mode(as_of="2026-04-30")
        assert result["active"] is False
        assert result["all_rows_count"] == 0

    def test_overlap_warning_surfaced(self):
        from tools import travel_mode
        early = dict(SAMPLE_TRIP, label="early")
        late = dict(SAMPLE_TRIP, label="late")
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[early, late]):
            result = travel_mode.get_active_travel_mode(as_of="2026-04-30")
        assert result["active"] is True
        assert result["label"] == "late"
        assert result["overlap_warning"] is not None
        assert "early" in result["overlap_warning"]
        assert "late" in result["overlap_warning"]


class TestTripBudgetStatus:
    @staticmethod
    def _mock_db(records):
        """MagicMock standing in for travel_mode's supabase_client with the
        given transaction rows (already Sheet-shaped, per the data layer)."""
        db = MagicMock()
        db.read_all_transaction_rows.return_value = records
        return db

    def test_empty_travel_mode_table(self):
        # Postgres tables always exist; an EMPTY travel_mode table keeps
        # producing the legacy `no_travel_mode_tab` status the skill keys on.
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows", return_value=[]):
            result = travel_mode.get_trip_budget_status()
        assert result["status"] == "no_travel_mode_tab"

    def test_no_active_trip(self):
        from tools import travel_mode
        # Trip ended yesterday relative to as_of.
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.get_trip_budget_status(as_of="2026-05-10")
        assert result["status"] == "no_active_trip"

    def test_lookup_by_label(self):
        from tools import travel_mode
        records = []
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(trip_label="ID Apr 2026")
        assert result["status"] == "ok"
        assert result["label"] == "ID Apr 2026"

    def test_lookup_unknown_label(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.get_trip_budget_status(trip_label="ZZZ")
        assert result["status"] == "not_found"
        assert "ID Apr 2026" in result["available_labels"]

    def test_buckets_tagged(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 100, "Notes": "[bucket:food] warung", "Source": "email",
             "txn_id": "txn_1"},
            {"Date": "2026-04-29", "Category": "Travel - ID 2026-04",
             "Amount": 50, "Notes": "[bucket:food] gado-gado", "Source": "email",
             "txn_id": "txn_2"},
            {"Date": "2026-04-29", "Category": "Travel - ID 2026-04",
             "Amount": 30, "Notes": "[bucket:transport] grab", "Source": "email",
             "txn_id": "txn_3"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(as_of="2026-04-30")
        assert result["status"] == "ok"
        food = next(b for b in result["buckets"] if b["bucket"] == "food")
        assert food["spent"] == 150.0
        assert food["budget"] == 450.0
        transport = next(b for b in result["buckets"] if b["bucket"] == "transport")
        assert transport["spent"] == 30.0
        assert result["total_spent"] == 180.0
        assert result["untagged_spend"] == 0.0

    def test_excludes_outside_window(self):
        from tools import travel_mode
        records = [
            # In window
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 100, "Notes": "[bucket:food]", "Source": "email"},
            # Before window
            {"Date": "2026-04-26", "Category": "Travel - ID 2026-04",
             "Amount": 999, "Notes": "[bucket:food]", "Source": "email"},
            # After window
            {"Date": "2026-05-04", "Category": "Travel - ID 2026-04",
             "Amount": 999, "Notes": "[bucket:food]", "Source": "email"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(as_of="2026-04-30")
        assert result["total_spent"] == 100.0

    def test_excludes_pending_and_backfill(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 100, "Notes": "[bucket:food]", "Source": "email"},
            # pending — UNCATEGORIZED
            {"Date": "2026-04-28", "Category": "UNCATEGORIZED",
             "Amount": 999, "Notes": "[bucket:food]", "Source": "email"},
            # backfill
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 999, "Notes": "[bucket:food]", "Source": "backfill"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(as_of="2026-04-30")
        assert result["total_spent"] == 100.0

    def test_untagged_surfaced(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 50, "Notes": "[bucket:food]", "Source": "email",
             "txn_id": "txn_food"},
            # No bucket tag — should land in untagged
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 75, "Notes": "no tag here", "Source": "email",
             "txn_id": "txn_orphan"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(as_of="2026-04-30")
        assert result["untagged_spend"] == 75.0
        assert "txn_orphan" in result["untagged_txn_ids"]

    def test_bucket_status_thresholds(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 360, "Notes": "[bucket:food]", "Source": "email"},  # 80%
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 800, "Notes": "[bucket:flight]", "Source": "email"},  # 100%
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 50, "Notes": "[bucket:transport]", "Source": "email"},  # ~17%
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)):
            result = travel_mode.get_trip_budget_status(as_of="2026-04-30")
        statuses = {b["bucket"]: b["status"] for b in result["buckets"]}
        assert statuses["food"] == "warning"
        assert statuses["flight"] == "capped"
        assert statuses["transport"] == "ok"


class TestSetTripBucket:
    def test_updates_notes(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            5, {"Notes": "[bucket:food] warung", "txn_id": "txn_1"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 5, "txn_id": "txn_1", "changes": {}}
        with patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.set_trip_bucket("txn_1", "activities")
        assert result["status"] == "ok"
        assert result["bucket"] == "activities"
        assert result["notes_after"] == "[bucket:activities] warung"
        # Ensure only Notes was sent to edit_transaction
        kwargs = mock_db.edit_transaction.call_args[1]
        assert kwargs["updates"] == {"Notes": "[bucket:activities] warung"}
        assert kwargs["txn_id"] == "txn_1"

    def test_inserts_when_missing_tag(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            5, {"Notes": "warung kopi", "txn_id": "txn_1"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 5, "txn_id": "txn_1", "changes": {}}
        with patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.set_trip_bucket("txn_1", "food")
        assert result["notes_after"] == "[bucket:food] warung kopi"

    def test_missing_txn_id(self):
        from tools import travel_mode
        result = travel_mode.set_trip_bucket("", "food")
        assert result["status"] == "error"

    def test_missing_bucket(self):
        from tools import travel_mode
        result = travel_mode.set_trip_bucket("txn_1", "")
        assert result["status"] == "error"

    def test_txn_not_found(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = None
        with patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.set_trip_bucket("txn_zzz", "food")
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()


class TestCreateTrip:
    def test_creates_trip(self):
        from tools import travel_mode
        mock_db = MagicMock()
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.create_trip(
                label="ID Sep 2026",
                start_date="2026-09-04",
                end_date="2026-09-11",
                trip_category="Travel - ID 2026-09",
                total_budget=2000,
                budget_map="food=450; transport=300",
                notes="bali",
            )
        assert result["status"] == "created"
        assert result["trip"]["label"] == "ID Sep 2026"
        assert result["trip"]["total_budget"] == 2000.0
        mock_db.insert_travel_mode_row.assert_called_once_with(
            label="ID Sep 2026",
            start_date="2026-09-04",
            end_date="2026-09-11",
            trip_category="Travel - ID 2026-09",
            budget_map="food=450; transport=300",
            total_budget=2000.0,
            notes="bali",
        )

    def test_defaults_optional_fields(self):
        from tools import travel_mode
        mock_db = MagicMock()
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.create_trip(
                label="JP Trip", start_date="2026-10-01",
                end_date="2026-10-05", trip_category="Travel - JP 2026-10",
            )
        assert result["status"] == "created"
        kwargs = mock_db.insert_travel_mode_row.call_args.kwargs
        assert kwargs["total_budget"] == 0.0
        assert kwargs["budget_map"] == ""
        assert kwargs["notes"] == ""

    def test_exists_case_insensitive_writes_nothing(self):
        from tools import travel_mode
        mock_db = MagicMock()
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.create_trip(
                label="id apr 2026", start_date="2026-04-27",
                end_date="2026-05-03", trip_category="Travel - ID 2026-04",
            )
        assert result["status"] == "exists"
        assert "ID Apr 2026" in result["message"]
        mock_db.insert_travel_mode_row.assert_not_called()

    def test_rejects_unparseable_dates(self):
        from tools import travel_mode
        result = travel_mode.create_trip(
            label="X", start_date="not-a-date", end_date="2026-10-05",
            trip_category="Travel - X",
        )
        assert result["status"] == "error"
        assert "YYYY-MM-DD" in result["message"]

    def test_rejects_start_after_end(self):
        from tools import travel_mode
        result = travel_mode.create_trip(
            label="X", start_date="2026-10-06", end_date="2026-10-05",
            trip_category="Travel - X",
        )
        assert result["status"] == "error"
        assert "after" in result["message"]

    def test_rejects_empty_label(self):
        from tools import travel_mode
        result = travel_mode.create_trip(
            label="  ", start_date="2026-10-01", end_date="2026-10-05",
            trip_category="Travel - X",
        )
        assert result["status"] == "error"

    def test_rejects_empty_trip_category(self):
        from tools import travel_mode
        result = travel_mode.create_trip(
            label="X", start_date="2026-10-01", end_date="2026-10-05",
            trip_category="",
        )
        assert result["status"] == "error"


class TestLinkTopupToTrip:
    def test_links_and_prepends_tag(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            9, {"Notes": "", "txn_id": "txn_topup"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 9, "txn_id": "txn_topup", "changes": {}}
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.link_topup_to_trip("txn_topup", "ID Apr 2026")
        assert result["status"] == "updated"
        assert result["txn_id"] == "txn_topup"
        assert result["trip_label"] == "ID Apr 2026"
        assert result["trip_category"] == "Travel - ID 2026-04"
        assert result["notes_after"] == "[trip:ID Apr 2026]"
        # The write goes through edit_transaction, Notes only.
        kwargs = mock_db.edit_transaction.call_args[1]
        assert kwargs["updates"] == {"Notes": "[trip:ID Apr 2026]"}
        assert kwargs["txn_id"] == "txn_topup"

    def test_label_matched_case_insensitively_stamps_canonical(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            9, {"Notes": "topup", "txn_id": "txn_topup"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 9, "txn_id": "txn_topup", "changes": {}}
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.link_topup_to_trip("txn_topup", "id apr 2026")
        # Canonical row label is stamped, not the user's casing.
        assert result["trip_label"] == "ID Apr 2026"
        assert result["notes_after"] == "[trip:ID Apr 2026] topup"

    def test_replaces_existing_trip_tag(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            9, {"Notes": "[trip:Old Trip] topup", "txn_id": "txn_topup"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 9, "txn_id": "txn_topup", "changes": {}}
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.link_topup_to_trip("txn_topup", "ID Apr 2026")
        assert result["notes_after"] == "[trip:ID Apr 2026] topup"

    def test_preserves_bucket_tag_and_orig_trace(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = (
            9, {"Notes": "[bucket:misc] orig: MYR 300 @ 0.32",
                "txn_id": "txn_topup"})
        mock_db.edit_transaction.return_value = {
            "status": "ok", "row": 9, "txn_id": "txn_topup", "changes": {}}
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.link_topup_to_trip("txn_topup", "ID Apr 2026")
        assert result["notes_after"] == \
            "[trip:ID Apr 2026] [bucket:misc] orig: MYR 300 @ 0.32"

    def test_unknown_label_lists_available(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.link_topup_to_trip("txn_topup", "ZZZ")
        assert result["status"] == "not_found"
        assert result["available_labels"] == ["ID Apr 2026"]

    def test_txn_not_found(self):
        from tools import travel_mode
        mock_db = MagicMock()
        mock_db.find_transaction_by_id.return_value = None
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client", mock_db):
            result = travel_mode.link_topup_to_trip("txn_zzz", "ID Apr 2026")
        assert result["status"] == "not_found"
        assert "txn_zzz" in result["message"]

    def test_missing_args(self):
        from tools import travel_mode
        assert travel_mode.link_topup_to_trip("", "ID Apr 2026")["status"] == "error"
        assert travel_mode.link_topup_to_trip("txn_1", " ")["status"] == "error"


class TestMaybeSendTripBucketNudge:
    @staticmethod
    def _mock_db(records):
        db = MagicMock()
        db.read_all_transaction_rows.return_value = records
        return db

    def test_empty_travel_mode_table(self):
        # Empty travel_mode table → same `no_travel_mode_tab` reason string
        # the missing tab used to produce.
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows", return_value=[]):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=100,
                txn_date="2026-04-28", notes="[bucket:food]",
            )
        assert result["sent"] is False
        assert result["reason"] == "no_travel_mode_tab"

    def test_no_active_trip(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=100,
                txn_date="2026-04-26", notes="[bucket:food]",
            )
        assert result["reason"] == "no_active_trip"

    def test_category_mismatch(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Personal - Food & Drinks", amount=100,
                txn_date="2026-04-28", notes="[bucket:food]",
            )
        assert result["reason"] == "category_mismatch"

    def test_untagged(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=100,
                txn_date="2026-04-28", notes="no tag",
            )
        assert result["reason"] == "untagged"

    def test_no_bucket_budget(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=100,
                txn_date="2026-04-28", notes="[bucket:UNALLOCATED]",
            )
        assert result["reason"] == "no_bucket_budget"

    def test_fires_at_80_percent(self):
        from tools import travel_mode
        # food budget = 450. spent_after needs to push past 360 (80%).
        # With amount=70, spent_before=290 (64%), spent_after=360 (80%).
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 360, "Notes": "[bucket:food]", "Source": "email"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)), \
             patch.object(travel_mode, "_already_nudged", return_value=False), \
             patch.object(travel_mode, "_record_nudge"), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "1234"}):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=70,
                txn_date="2026-04-28", txn_id="txn_x",
                notes="[bucket:food] satay",
            )
        assert result["sent"] is True
        assert result["threshold"] == 80
        assert result["bucket"] == "food"

    def test_fires_at_100_percent(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 460, "Notes": "[bucket:food]", "Source": "email"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)), \
             patch.object(travel_mode, "_already_nudged", return_value=False), \
             patch.object(travel_mode, "_record_nudge"), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "9999"}):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=50,
                txn_date="2026-04-28", txn_id="txn_y",
                notes="[bucket:food] gado",
            )
        assert result["sent"] is True
        assert result["threshold"] == 100

    def test_no_threshold_crossed(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 100, "Notes": "[bucket:food]", "Source": "email"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)), \
             patch.object(travel_mode, "_already_nudged", return_value=False):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=20,
                txn_date="2026-04-28", txn_id="txn_x",
                notes="[bucket:food] satay",
            )
        assert result["sent"] is False
        assert result["reason"] == "no_threshold_crossed"

    def test_dedup(self):
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 360, "Notes": "[bucket:food]", "Source": "email"},
        ]
        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)), \
             patch.object(travel_mode, "_already_nudged", return_value=True):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=70,
                txn_date="2026-04-28", txn_id="txn_x",
                notes="[bucket:food] satay",
            )
        assert result["sent"] is False
        assert result["reason"] == "already_nudged"

    def test_budget_revision_unblocks_nudge(self):
        """Mid-trip budget reallocation: dedup keys on budget_at_nudge,
        so a budget bump invalidates the prior 80% nudge and lets the
        next crossing fire fresh."""
        from tools import travel_mode

        # Old budget was 450 — 80% nudge fired at $360.
        # User bumped food to 600; the old nudge row has budget_at_nudge=450.
        # New txn pushes food to $480 (80% of 600). Should fire — budget
        # changed, dedup key differs.
        revised_trip = dict(SAMPLE_TRIP, budget_map={"food": 600.0})
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 480, "Notes": "[bucket:food]", "Source": "email"},
        ]

        # Old nudge at budget=450 doesn't match new budget=600 → not deduped.
        def fake_already(label, bucket, threshold, budget_at_nudge):
            return False

        with patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[revised_trip]), \
             patch.object(travel_mode, "supabase_client",
                          self._mock_db(records)), \
             patch.object(travel_mode, "_already_nudged", side_effect=fake_already), \
             patch.object(travel_mode, "_record_nudge") as mock_record, \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "8765"}):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=120,
                txn_date="2026-04-29", txn_id="txn_z",
                notes="[bucket:food] dinner",
            )
        assert result["sent"] is True
        assert result["budget"] == 600
        # The recorded nudge captures the NEW budget for future dedup.
        recorded_budget = mock_record.call_args[0][3]
        assert recorded_budget == 600

    def test_dedup_keys_on_budget(self):
        """Verify _already_nudged dedup logic: same trip+bucket+threshold
        with a DIFFERENT budget_at_nudge value should NOT match."""
        from tools import travel_mode
        log = [{
            "trip_label": "ID Apr 2026",
            "bucket": "food",
            "threshold": 80,
            "budget_at_nudge": 450,
        }]
        mock_db = MagicMock()
        mock_db.read_trip_nudge_log.return_value = log
        with patch.object(travel_mode, "supabase_client", mock_db):
            # Same budget → matches, deduped
            assert travel_mode._already_nudged("ID Apr 2026", "food", 80, 450) is True
            # Different budget (post-revision) → no match, fresh nudge fires
            assert travel_mode._already_nudged("ID Apr 2026", "food", 80, 600) is False

    def test_record_nudge_appends_dedup_tuple(self):
        """_record_nudge writes via append_trip_nudge with the exact dedup
        tuple values (trip_label, bucket, threshold, budget_at_nudge)."""
        from tools import travel_mode
        mock_db = MagicMock()
        with patch.object(travel_mode, "supabase_client", mock_db):
            travel_mode._record_nudge("ID Apr 2026", "food", 80, 450.0, "txn_x")
        mock_db.append_trip_nudge.assert_called_once_with(
            "ID Apr 2026", "food", 80, 450.0, "txn_x"
        )

    def test_fired_nudge_logged_through_append_trip_nudge(self):
        """End-to-end: a real threshold crossing lands one append_trip_nudge
        row carrying the NEW budget (the future dedup key)."""
        from tools import travel_mode
        records = [
            {"Date": "2026-04-28", "Category": "Travel - ID 2026-04",
             "Amount": 360, "Notes": "[bucket:food]", "Source": "email"},
        ]
        mock_db = self._mock_db(records)
        mock_db.read_trip_nudge_log.return_value = []  # nothing nudged yet
        mock_db.append_trip_nudge.return_value = True
        with patch.object(travel_mode, "supabase_client", mock_db), \
             patch.object(travel_mode, "read_travel_mode_rows",
                          return_value=[SAMPLE_TRIP]), \
             patch("tools.expense_sheets_tool._send_telegram_bubble",
                   return_value={"ok": True, "message_id": "4321"}):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=70,
                txn_date="2026-04-28", txn_id="txn_x",
                notes="[bucket:food] satay",
            )
        assert result["sent"] is True
        mock_db.append_trip_nudge.assert_called_once_with(
            "ID Apr 2026", "food", 80, 450.0, "txn_x"
        )

    def test_swallows_exceptions(self):
        from tools import travel_mode
        with patch.object(travel_mode, "read_travel_mode_rows",
                          side_effect=RuntimeError("boom")):
            result = travel_mode.maybe_send_trip_bucket_nudge(
                category="Travel - ID 2026-04", amount=100,
                txn_date="2026-04-28", notes="[bucket:food]",
            )
        assert result["sent"] is False
        assert result["reason"] == "exception"
        assert "boom" in result["error"]


class TestRouteForTrip:
    """The deterministic trip router (2026-08-17): the TOOL decides trip
    routing inside log_expense — YouTrip payment or an `orig:` FX trace
    during an active trip → trip category + [bucket:X] derived from the
    PROPOSED home category. SGD/no-signal spends stay home; top-ups and
    already-trip rows are never routed; never raises."""

    ACTIVE = {
        "active": True, "as_of": "2026-04-30", "label": "ID Apr 2026",
        "trip_category": "Travel - ID 2026-04",
        "budget_map": {"food": 450.0}, "total_budget": 2000.0,
        "notes": "", "overlap_warning": None,
    }

    @pytest.fixture(autouse=True)
    def _travel_db(self):
        # route_for_trip consults category_meta (fixed-bill guard) and
        # resolve_category (the trip category must be a real budgets row).
        # Default: no fixed bills, every category resolves to itself.
        from tools import travel_mode
        db = MagicMock()
        db.read_category_kinds.return_value = {}
        db.resolve_category.side_effect = lambda c: {"match": c}
        db._normalize_category.side_effect = lambda s: str(s or "").strip().casefold()
        with patch.object(travel_mode, "supabase_client", db):
            self.travel_db = db
            yield db

    def _route(self, **kw):
        from tools import travel_mode
        with patch.object(travel_mode, "get_active_travel_mode",
                          return_value=kw.pop("_trip", self.ACTIVE)):
            return travel_mode.route_for_trip(**kw)

    def test_fixed_bill_never_routed_even_with_orig(self):
        # Adversarial-review catch: Anthropic bills in USD mid-trip carried
        # an orig: trace and would have been swept into the trip's misc
        # bucket, and a user correction could never stick.
        self.travel_db.read_category_kinds.return_value = {"anthropic": "fixed"}
        r = self._route(txn_date="2026-04-30", proposed_category="Anthropic",
                        payment_method="DBS/POSB card ending 1234",
                        notes="orig: USD 20.00 @ 1.290000 (frankfurter 2026-04-30)",
                        currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "fixed_bill"
        assert r["category"] == "Anthropic"

    def test_unknown_trip_category_falls_back_to_home(self):
        # A conversationally-created trip with no budgets row must not turn
        # every spend into unknown_category refusals — the home category
        # lands instead.
        self.travel_db.resolve_category.side_effect = lambda c: {"match": None}
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="YouTrip card", notes="", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "trip_category_unknown"
        assert r["category"] == "Personal - Food & Drinks"

    def test_trip_category_snaps_to_canonical_spelling(self):
        self.travel_db.resolve_category.side_effect = lambda c: {"match": "Travel - ID 2026-04"}
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="YouTrip card", notes="", currency="SGD")
        assert r["applied"] is True
        assert r["category"] == "Travel - ID 2026-04"

    def test_inactive_not_applied(self):
        r = self._route(_trip={"active": False, "as_of": "2026-04-20",
                               "all_rows_count": 1},
                        txn_date="2026-04-20",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="YouTrip", notes="", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "no_active_trip"
        assert r["category"] == "Personal - Food & Drinks"
        assert r["notes"] == ""

    def test_youtrip_signal_routes_with_bucket_from_home_category(self):
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="YouTrip card",
                        notes="warung kopi", currency="SGD")
        assert r["applied"] is True
        assert r["signal"] == "youtrip"
        assert r["category"] == "Travel - ID 2026-04"
        assert r["from_category"] == "Personal - Food & Drinks"
        assert r["bucket"] == "food"
        assert r["notes"] == "[bucket:food] warung kopi"
        assert r["trip_label"] == "ID Apr 2026"

    def test_orig_signal_routes_merchantmap_category_into_bucket(self):
        # The Gojek-in-KL fix: a MerchantMap hit ("Personal - Travel") no
        # longer beats travel mode — it decides the BUCKET (transport).
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Travel",
                        payment_method="DBS/POSB card ending 1234",
                        notes="orig: MYR 33.00 @ 0.313220 (frankfurter 2026-04-30)",
                        currency="SGD")
        assert r["applied"] is True
        assert r["signal"] == "orig"
        assert r["category"] == "Travel - ID 2026-04"
        assert r["bucket"] == "transport"
        assert r["notes"].startswith("[bucket:transport] orig: MYR 33.00")

    def test_sgd_no_signal_stays_home(self):
        # Shopee for home / PayLah to a friend mid-trip: currency is the
        # guard, not the date.
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Others",
                        payment_method="PayLah! Wallet", notes="", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "no_signal"
        assert r["category"] == "Personal - Others"
        assert r["notes"] == ""

    def test_topup_guard(self):
        r = self._route(txn_date="2026-04-30",
                        proposed_category="youtrip top-up",
                        payment_method="DBS/POSB card ending 1234",
                        notes="orig: MYR 1.00 @ 0.3", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "topup_guard"

    def test_already_trip_guard(self):
        r = self._route(txn_date="2026-04-30",
                        proposed_category="travel - id 2026-04",
                        payment_method="YouTrip", notes="", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "already_trip"

    def test_existing_bucket_tag_respected(self):
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="YouTrip",
                        notes="[bucket:lodging] hostel", currency="SGD")
        assert r["applied"] is True
        assert r["bucket"] == "lodging"
        assert r["notes"] == "[bucket:lodging] hostel"

    def test_unconverted_foreign_currency_counts_as_orig_signal(self):
        r = self._route(txn_date="2026-04-30",
                        proposed_category="Personal - Food & Drinks",
                        payment_method="", notes="", currency="MYR")
        assert r["applied"] is True
        assert r["signal"] == "orig"

    def test_exception_not_applied(self):
        from tools import travel_mode
        with patch.object(travel_mode, "get_active_travel_mode",
                          side_effect=RuntimeError("db down")):
            r = travel_mode.route_for_trip(
                txn_date="2026-04-30", proposed_category="Personal - Food & Drinks",
                payment_method="YouTrip", notes="x", currency="SGD")
        assert r["applied"] is False
        assert r["reason"] == "exception"
        assert "db down" in r["error"]
        assert r["category"] == "Personal - Food & Drinks"
        assert r["notes"] == "x"


class TestBucketForCategory:
    def test_keyword_table(self):
        from tools.travel_mode import _bucket_for_category
        table = {
            "Personal - Food & Drinks": "food",
            "Groceries": "food",
            "Dining Out": "food",
            "Personal - Travel": "transport",
            "Transport": "transport",
            "Grab / Taxi": "transport",
            "Clothing": "shopping",
            "Shopping": "shopping",
            "Miscellaneous": "shopping",
            "Beauty": "shopping",
            "Gifts": "shopping",
            "Health & Medical": "health",
            "Dental": "health",
            "Pharmacy": "health",
            "Hotel": "lodging",
            "Accommodation": "lodging",
            "Insurance": "misc",
            "UNCATEGORIZED": "misc",
            "": "misc",
        }
        for name, expected in table.items():
            assert _bucket_for_category(name) == expected, name

    def test_case_insensitive(self):
        from tools.travel_mode import _bucket_for_category
        assert _bucket_for_category("PERSONAL - FOOD") == "food"


class TestHandlers:
    """The 3 handlers in expense_sheets_tool.py delegate to travel_mode.
    These tests verify arg pass-through + JSON encoding."""

    def test_get_active_travel_mode_handler(self):
        from tools.expense_sheets_tool import handle_get_active_travel_mode
        from tools import travel_mode
        with patch.object(travel_mode, "get_active_travel_mode",
                          return_value={"active": False}) as mock_fn:
            result = json.loads(handle_get_active_travel_mode({"as_of": "2026-04-28"}))
        mock_fn.assert_called_once_with(as_of="2026-04-28")
        assert result["active"] is False

    def test_get_trip_budget_status_handler(self):
        from tools.expense_sheets_tool import handle_get_trip_budget_status
        from tools import travel_mode
        with patch.object(travel_mode, "get_trip_budget_status",
                          return_value={"status": "ok"}) as mock_fn:
            handle_get_trip_budget_status({
                "trip_label": "ID Apr 2026",
                "as_of": "2026-04-28",
            })
        mock_fn.assert_called_once_with(
            trip_label="ID Apr 2026", as_of="2026-04-28"
        )

    def test_set_trip_bucket_handler(self):
        from tools.expense_sheets_tool import handle_set_trip_bucket
        from tools import travel_mode
        with patch.object(travel_mode, "set_trip_bucket",
                          return_value={"status": "ok"}) as mock_fn:
            handle_set_trip_bucket({
                "txn_id": "txn_1",
                "bucket": "activities",
            })
        mock_fn.assert_called_once_with(txn_id="txn_1", bucket="activities")

    def test_create_trip_handler_passes_kwargs(self):
        from tools.expense_sheets_tool import handle_create_trip
        from tools import travel_mode
        with patch.object(travel_mode, "create_trip",
                          return_value={"status": "created",
                                        "trip": {"label": "X"}}) as mock_fn:
            result = json.loads(handle_create_trip({
                "label": "ID Sep 2026",
                "start_date": "2026-09-04",
                "end_date": "2026-09-11",
                "trip_category": "Travel - ID 2026-09",
                "total_budget": 2000,
                "budget_map": "food=450",
                "notes": "bali",
            }))
        mock_fn.assert_called_once_with(
            label="ID Sep 2026", start_date="2026-09-04",
            end_date="2026-09-11", trip_category="Travel - ID 2026-09",
            total_budget=2000.0, budget_map="food=450", notes="bali",
        )
        assert result["status"] == "created"

    def test_create_trip_handler_defaults(self):
        from tools.expense_sheets_tool import handle_create_trip
        from tools import travel_mode
        with patch.object(travel_mode, "create_trip",
                          return_value={"status": "created",
                                        "trip": {}}) as mock_fn:
            # Empty-string total_budget (LLMs send those) → 0.0.
            handle_create_trip({
                "label": "X", "start_date": "2026-09-04",
                "end_date": "2026-09-11", "trip_category": "T",
                "total_budget": "",
            })
        mock_fn.assert_called_once_with(
            label="X", start_date="2026-09-04", end_date="2026-09-11",
            trip_category="T", total_budget=0.0, budget_map="", notes="",
        )

    def test_link_topup_to_trip_handler_passes_kwargs(self):
        from tools.expense_sheets_tool import handle_link_topup_to_trip
        from tools import travel_mode
        with patch.object(travel_mode, "link_topup_to_trip",
                          return_value={"status": "updated",
                                        "txn_id": "txn_1",
                                        "trip_label": "ID Apr 2026",
                                        "trip_category": "Travel - ID 2026-04"}) \
                as mock_fn:
            result = json.loads(handle_link_topup_to_trip({
                "txn_id": "txn_1",
                "trip_label": "ID Apr 2026",
            }))
        mock_fn.assert_called_once_with(txn_id="txn_1",
                                        trip_label="ID Apr 2026")
        assert result["status"] == "updated"
        assert result["trip_category"] == "Travel - ID 2026-04"

    def test_link_topup_to_trip_handler_json_round_trip(self):
        from tools.expense_sheets_tool import handle_link_topup_to_trip
        from tools import travel_mode
        with patch.object(travel_mode, "link_topup_to_trip",
                          return_value={"status": "not_found",
                                        "available_labels": ["A", "B"]}):
            result = json.loads(handle_link_topup_to_trip({}))
        assert result["status"] == "not_found"
        assert result["available_labels"] == ["A", "B"]


class TestCreateTripEnsuresBudgetRow:
    def test_budgets_row_ensured_after_insert(self):
        from tools import travel_mode
        db = MagicMock()
        db.read_travel_mode_records.return_value = []
        with patch.object(travel_mode, "supabase_client", db), \
             patch.object(travel_mode, "read_travel_mode_rows", return_value=[]):
            r = travel_mode.create_trip(label="JP Aug", start_date="2026-08-20",
                                        end_date="2026-08-25",
                                        trip_category="Travel - JP 2026-08",
                                        total_budget=1500)
        assert r["status"] == "created", r
        db.ensure_category_exists.assert_called_once_with("Travel - JP 2026-08")

    def test_ensure_failure_does_not_break_creation(self):
        from tools import travel_mode
        db = MagicMock()
        db.ensure_category_exists.side_effect = RuntimeError("503")
        with patch.object(travel_mode, "supabase_client", db), \
             patch.object(travel_mode, "read_travel_mode_rows", return_value=[]):
            r = travel_mode.create_trip(label="JP Aug", start_date="2026-08-20",
                                        end_date="2026-08-25",
                                        trip_category="Travel - JP 2026-08",
                                        total_budget=1500)
        assert r["status"] == "created", r


"""Tests for tools/supabase_client.py — the PostgREST mirror of sheets_client.

Every test patches `tools.supabase_client._request`, the module's single
I/O seam. Ordered `side_effect` lists are annotated with the consumer of
each entry, matching the suite's convention for multi-read functions.
"""

from unittest.mock import patch

import pytest


def _txn_rec(**over):
    """A transactions DB record as PostgREST returns it."""
    rec = {
        "id": 41,
        "txn_id": "txn_20260728_001",
        "date": "2026-07-28",
        "txn_time": "12:30",
        "merchant": "KOPITIAM @ RAFFLES",
        "amount": 7.80,
        "currency": "SGD",
        "category": "Personal - Food & Drinks",
        "source": "email",
        "payment_method": "UOB Card ending 5678",
        "notes": "",
        "telegram_message_id": None,
        "idempotency_key": "abc123def4567890",
        "created_at": "2026-07-28T12:31:00+00:00",
    }
    rec.update(over)
    return rec


class TestConfigured:
    def test_true_when_both_env_vars_set(self):
        from tools import supabase_client
        with patch.dict("os.environ", {"SUPABASE_URL": "https://x.supabase.co",
                                       "SUPABASE_SERVICE_KEY": "sk"}):
            assert supabase_client._configured() is True

    def test_false_when_missing(self):
        from tools import supabase_client
        with patch.dict("os.environ", {"SUPABASE_URL": "",
                                       "SUPABASE_SERVICE_KEY": ""}):
            assert supabase_client._configured() is False


class TestToSheetRow:
    def test_maps_db_columns_to_sheet_headers(self):
        from tools import supabase_client
        row = supabase_client._to_sheet_row(_txn_rec())
        assert row["Date"] == "2026-07-28"
        assert row["Merchant"] == "KOPITIAM @ RAFFLES"
        assert row["Amount"] == 7.80
        assert row["Payment Method"] == "UOB Card ending 5678"
        assert row["Time"] == "12:30"
        assert row["txn_id"] == "txn_20260728_001"

    def test_none_becomes_empty_string(self):
        from tools import supabase_client
        row = supabase_client._to_sheet_row(_txn_rec(telegram_message_id=None,
                                                     notes=None))
        assert row["telegram_message_id"] == ""
        assert row["Notes"] == ""

    def test_string_amount_coerced_to_float(self):
        from tools import supabase_client
        row = supabase_client._to_sheet_row(_txn_rec(amount="7.80"))
        assert row["Amount"] == pytest.approx(7.80)


class TestAppendTransaction:
    def test_ok_path_rpc_then_atomic_insert(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Personal - Food & Drinks"}]),  # resolve_category
                (200, "txn_20260728_004"),      # rpc/next_txn_id
                (201, [_txn_rec()]),            # insert w/ representation
            ]
            result = supabase_client.append_transaction(
                "2026-07-28", "KOPITIAM @ RAFFLES", 7.80, "SGD",
                "Personal - Food & Drinks", source="email",
                payment_method="UOB Card ending 5678", txn_time="12:30",
            )

        assert result["status"] == "ok"
        assert result["txn_id"] == "txn_20260728_004"
        assert len(result["idempotency_key"]) == 16
        assert len(result["row"]) == 11  # informational, same order as Sheet

        rpc_call = req.call_args_list[1]
        assert rpc_call.args[1] == "rpc/next_txn_id"
        assert rpc_call.kwargs["body"] == {"d": "2026-07-28"}

        ins_call = req.call_args_list[2]
        assert ins_call.args[1] == "transactions"
        assert ins_call.kwargs["params"] == {"on_conflict": "idempotency_key"}
        assert "ignore-duplicates" in ins_call.kwargs["prefer"]
        body = ins_call.kwargs["body"][0]
        assert body["txn_id"] == "txn_20260728_004"
        assert body["txn_time"] == "12:30"
        assert body["telegram_message_id"] is None

    def test_duplicate_returns_existing_txn_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Personal - Food & Drinks"}]),  # resolve_category
                (200, "txn_20260728_005"),                          # rpc
                (201, []),                                          # insert ignored
                (200, [_txn_rec(txn_id="txn_20260728_001")]),       # existing lookup
            ]
            result = supabase_client.append_transaction(
                "2026-07-28", "KOPITIAM @ RAFFLES", 7.80, "SGD",
                "Personal - Food & Drinks",
                payment_method="UOB Card ending 5678",
            )

        assert result["status"] == "duplicate"
        assert result["txn_id"] == "txn_20260728_001"
        assert "Duplicate" in result["message"]

    def test_key_formula_matches_sheets_client(self):
        from tools import sheets_client, supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Coffee"}]),  # resolve_category
                (200, "txn_20260728_001"),        # rpc/next_txn_id
                (201, [_txn_rec()]),              # insert
            ]
            result = supabase_client.append_transaction(
                "2026-04-16", "Starbucks", 5.50, "SGD", "Coffee",
                payment_method="DBS card ending 1234",
            )
        expected = sheets_client._compute_idempotency_key(
            "2026-04-16", "Starbucks", 5.50, "DBS card ending 1234")
        assert result["idempotency_key"] == expected


class TestReadTransactions:
    def test_month_window_params(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [_txn_rec()])
            rows = supabase_client.read_transactions("2026-07")
        params = req.call_args.kwargs["params"]
        assert params["date"] == "gte.2026-07-01"
        assert params["and"] == "(date.lt.2026-08-01)"
        assert rows[0]["Merchant"] == "KOPITIAM @ RAFFLES"

    def test_december_rolls_into_next_year(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            supabase_client.read_transactions("2025-12")
        params = req.call_args.kwargs["params"]
        assert params["and"] == "(date.lt.2026-01-01)"


class TestFinders:
    def test_find_by_id_found_and_missing(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [_txn_rec()])
            found = supabase_client.find_transaction_by_id("txn_20260728_001")
        assert found is not None
        row_id, row = found
        assert row_id == 41
        assert row["txn_id"] == "txn_20260728_001"

        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            assert supabase_client.find_transaction_by_id("txn_x") is None

    def test_find_by_message_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [_txn_rec(telegram_message_id="777")])
            found = supabase_client.find_transaction_by_message_id("777")
        assert found is not None
        assert found[1]["telegram_message_id"] == "777"
        assert req.call_args.kwargs["params"]["telegram_message_id"] == "eq.777"

    def test_fuzzy_substring_and_amount_tolerance(self):
        from tools import supabase_client
        recs = [
            _txn_rec(id=50, merchant="GRAB* A-9K5HPMKG", amount=17.90),
            _txn_rec(id=49, merchant="NTUC FairPrice", amount=61.30),
        ]
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, recs)
            found = supabase_client.find_transaction_row("grab", 17.905)
        assert found is not None
        assert found[0] == 50

    def test_fuzzy_rejects_short_search_and_wrong_date(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [_txn_rec()])
            assert supabase_client.find_transaction_row("ko", 7.80) is None
            assert supabase_client.find_transaction_row(
                "KOPITIAM", 7.80, date="2026-07-01") is None


class TestEditTransaction:
    def test_requires_updates(self):
        from tools import supabase_client
        result = supabase_client.edit_transaction("x", 1.0, updates=None)
        assert result["status"] == "error"

    def test_maps_sheet_fields_to_db_columns(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Groceries"}]),  # resolve_category
                (200, [_txn_rec()]),   # find_transaction_by_id
                (204, None),           # PATCH
            ]
            result = supabase_client.edit_transaction(
                "", 0, txn_id="txn_20260728_001",
                updates={"Category": "Groceries", "Notes": "fixed",
                         "Bogus Column": "ignored"},
            )

        assert result["status"] == "ok"
        assert result["row"] == 41
        assert result["changes"]["Category"]["to"] == "Groceries"
        assert "Bogus Column" not in result["changes"]

        patch_call = req.call_args_list[2]
        assert patch_call.args[0] == "PATCH"
        assert patch_call.kwargs["params"] == {"id": "eq.41"}
        assert patch_call.kwargs["body"] == {"category": "Groceries",
                                             "notes": "fixed"}

    def test_not_found(self):
        # Notes-only update so the category resolver stays out of the way —
        # the pin here is the not-found contract, not the guard.
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            result = supabase_client.edit_transaction(
                "", 0, txn_id="txn_gone", updates={"Notes": "x"})
        assert result["status"] == "error"
        assert "txn_gone" in result["message"]


class TestDeleteTransaction:
    def test_deletes_by_txn_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [_txn_rec()]),   # find_transaction_by_id
                (204, None),           # DELETE
            ]
            result = supabase_client.delete_transaction(
                "", 0, txn_id="txn_20260728_001")
        assert result["status"] == "ok"
        assert result["deleted_row"] == 41
        assert result["transaction"]["Merchant"] == "KOPITIAM @ RAFFLES"
        del_call = req.call_args_list[1]
        assert del_call.args[0] == "DELETE"
        assert del_call.kwargs["params"] == {"id": "eq.41"}


class TestGetLastTransaction:
    def test_returns_newest_by_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [_txn_rec(id=99)])
            found = supabase_client.get_last_transaction()
        assert found[0] == 99
        assert req.call_args.kwargs["params"]["order"] == "id.desc"

    def test_empty_ledger_returns_none(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            assert supabase_client.get_last_transaction() is None


class TestLinkTelegramMessage:
    def test_patches_message_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [_txn_rec()]),   # find_transaction_by_id
                (204, None),           # PATCH
            ]
            result = supabase_client.link_telegram_message(
                "txn_20260728_001", 555)
        assert result["status"] == "ok"
        assert result["telegram_message_id"] == "555"
        patch_call = req.call_args_list[1]
        assert patch_call.kwargs["body"] == {"telegram_message_id": "555"}

    def test_not_found(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            result = supabase_client.link_telegram_message("txn_x", 555)
        assert result["status"] == "error"


class TestBudgets:
    def test_read_budgets_shape(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [
                {"category": "Groceries", "month": "2026-07",
                 "limit_amount": "500"},
            ])
            budgets = supabase_client.read_budgets(7)
        assert budgets == [{"Category": "Groceries", "Monthly Limit": 500.0}]
        assert req.call_args.kwargs["params"]["month"].startswith("eq.")

    def test_update_budget_row_unknown_category_status(self):
        # Was a raw ValueError before the category guard; now the same
        # refusal shape every writer uses, so the handler json-dumps it.
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [])
            result = supabase_client.update_budget_row("Nope", 100)
        assert result["status"] == "unknown_category"

    def test_update_budget_row_upserts(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Groceries"}]),   # existence check
                (201, None),                          # upsert
            ]
            result = supabase_client.update_budget_row("Groceries", 550, 8)
        assert result == {"status": "ok", "category": "Groceries",
                          "month": 8, "new_limit": 550}
        up = req.call_args_list[1]
        assert up.kwargs["params"] == {"on_conflict": "category,month"}
        assert up.kwargs["body"][0]["limit_amount"] == 550
        assert up.kwargs["body"][0]["month"].endswith("-08")

    def test_ensure_category_exists_case_insensitive(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [{"category": "groceries"}])
            result = supabase_client.ensure_category_exists("Groceries")
        assert result == {"status": "exists", "category": "Groceries"}

    def test_ensure_category_creates_with_zero_limit(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, []),      # existence check
                (201, None),    # insert
            ]
            result = supabase_client.ensure_category_exists("New Cat")
        assert result["status"] == "created"
        ins = req.call_args_list[1]
        assert ins.kwargs["body"][0]["limit_amount"] == 0


class TestSpendingSummary:
    def test_pending_rows_excluded_and_counted(self):
        from tools import supabase_client
        txns = [
            _txn_rec(id=1, category="Groceries", amount=30.0),
            _txn_rec(id=2, category="UNCATEGORIZED", amount=99.0),
        ]
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, txns),                                        # read_transactions
                (200, [{"category": "Groceries", "month": "2026-07",
                        "limit_amount": 500}]),                     # read_budgets
            ]
            summary = supabase_client.get_spending_summary("2026-07")
        assert summary["Groceries"]["spent"] == 30.0
        assert summary["_pending_review"]["count"] == 1

    def test_unbudgeted_category_flagged(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [_txn_rec(category="Mystery", amount=12.0)]),  # read_transactions
                (200, []),                                           # read_budgets
            ]
            summary = supabase_client.get_spending_summary("2026-07")
        assert summary["Mystery"]["note"] == "No budget set for this category"

    def test_backfill_and_youtrip_spends_excluded(self):
        from tools import supabase_client
        txns = [
            _txn_rec(id=1, category="Groceries", amount=30.0),
            _txn_rec(id=2, category="Groceries", amount=500.0,
                     source="backfill"),
            _txn_rec(id=3, category="Travel - JB", amount=6.95,
                     payment_method="YouTrip Card"),
        ]
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, txns),                                        # read_transactions
                (200, [{"category": "Groceries", "month": "2026-07",
                        "limit_amount": 500}]),                     # read_budgets
            ]
            summary = supabase_client.get_spending_summary("2026-07")
        assert summary["Groceries"]["spent"] == 30.0
        assert "Travel - JB" not in summary


class TestReportExclusions:
    """generate_spending_report skips backfill imports and pot-internal
    YouTrip spends (M14 + the pot rule) and SAYS what it skipped via
    excluded_from_totals — the Jul 2026 recap silently ignored $667 of
    pending rows while silently counting a $6.95 YouTrip spend the
    dashboard excluded."""

    def _run(self, txns, prev=None, trips=None):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, txns),                                        # read_transactions (month)
                (200, [{"category": "Groceries", "month": "2026-07",
                        "limit_amount": 500}]),                     # read_budgets
                (200, prev or []),                                  # read_transactions (prev month)
                (200, trips or []),                                 # read_travel_mode_records
            ]
            return supabase_client.generate_spending_report("2026-07")

    def _july(self):
        return [
            _txn_rec(id=1, category="Groceries", amount=30.0,
                     merchant="NTUC"),
            _txn_rec(id=2, category="UNCATEGORIZED", amount=99.0,
                     merchant="MYSTERY SHOP"),
            _txn_rec(id=3, category="Groceries", amount=500.0,
                     source="backfill", merchant="STATEMENT IMPORT"),
            _txn_rec(id=4, category="Travel - JB", amount=6.95,
                     payment_method="YouTrip Card", merchant="SHENG SIONG"),
        ]

    def test_total_counts_only_clean_rows(self):
        report = self._run(self._july())
        assert report["total_spent"] == 30.0

    def test_excluded_from_totals_telemetry(self):
        report = self._run(self._july())
        excl = report["excluded_from_totals"]
        assert excl["pending_count"] == 1
        assert excl["pending_total"] == 99.0
        assert excl["backfill_count"] == 1
        assert excl["backfill_total"] == 500.0
        assert excl["youtrip_spend_count"] == 1
        assert excl["youtrip_spend_total"] == 6.95
        assert report["pending_review"]["total"] == 99.0

    def test_merchants_and_daily_share_the_filter(self):
        report = self._run(self._july())
        merchants = {m["merchant"] for m in report["top_merchants"]}
        assert merchants == {"NTUC"}
        assert report["daily_spending"] == [
            {"date": "2026-07-28", "total": 30.0}]

    def test_mom_filters_both_sides(self):
        prev = [
            _txn_rec(id=10, category="Groceries", amount=100.0,
                     date="2026-06-15"),
            _txn_rec(id=11, category="Groceries", amount=1000.0,
                     date="2026-06-16", source="backfill"),
        ]
        report = self._run(self._july(), prev=prev)
        assert report["month_over_month"]["previous_total"] == 100.0
        assert report["month_over_month"]["current_total"] == 30.0

    def test_tagged_topup_rebuckets_into_trip_category(self):
        # Adversarial-review catch: excluding pot-internal YouTrip spends
        # zeroed the trip's budgets row. PWA parity: the [trip:]-tagged
        # top-up consumes the TRIP category (funded amount), keeping
        # budget-manager warnings armed while pot spends stay excluded.
        txns = [
            _txn_rec(id=1, category="YouTrip Top-up", amount=300.0,
                     merchant="YOU TECHNOLOGIES",
                     notes="[trip:JB 2026-08] orig: SGD 300"),
            _txn_rec(id=2, category="Travel - JB", amount=280.0,
                     payment_method="YouTrip Card",
                     merchant="SHENG SIONG JB"),
        ]
        trips = [{"label": "JB 2026-08", "trip_category": "Travel - JB"}]
        report = self._run(txns, trips=trips)
        cats = {c["category"]: c for c in report["categories"]}
        assert cats["Travel - JB"]["spent"] == 300.0     # the funded pot
        assert "YouTrip Top-up" not in cats
        assert report["excluded_from_totals"]["youtrip_spend_total"] == 280.0

    def test_untagged_topup_stays_in_its_own_category(self):
        txns = [_txn_rec(id=1, category="YouTrip Top-up", amount=150.0,
                         merchant="YOU TECHNOLOGIES", notes="")]
        report = self._run(
            txns, trips=[{"label": "JB", "trip_category": "Travel - JB"}])
        cats = {c["category"]: c for c in report["categories"]}
        assert cats["YouTrip Top-up"]["spent"] == 150.0


class TestMerchantMap:
    def test_longest_pattern_wins(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [
                {"pattern": "GRAB", "category": "Personal - Travel",
                 "created_at": "2026-01-01"},
                {"pattern": "GRABFOOD", "category": "Personal - Food & Drinks",
                 "created_at": "2026-01-01"},
            ])
            match = supabase_client.lookup_merchant_category("GRABFOOD SG")
        assert match["category"] == "Personal - Food & Drinks"

    def test_add_mapping_created_and_updated(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "YouTrip Top-up"}]),  # resolve_category
                (200, []),      # read_merchant_mappings (no match)
                (201, None),    # insert
            ]
            result = supabase_client.add_merchant_mapping("YOU TECHNOLOGIES",
                                                          "YouTrip Top-up")
        assert result["status"] == "created"

        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "YouTrip Top-up"}]),  # resolve_category
                (200, [{"pattern": "you technologies", "category": "Old",
                        "created_at": "2026-01-01"}]),   # read_merchant_mappings
                (204, None),                             # PATCH
            ]
            result = supabase_client.add_merchant_mapping("YOU TECHNOLOGIES",
                                                          "YouTrip Top-up")
        assert result["status"] == "updated"


class TestInsightsAndJournal:
    def test_write_insight_round_trip(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (201, None)
            result = supabase_client.write_insight("Dining trending up",
                                                   category="dining")
        assert result["status"] == "ok"
        body = req.call_args.kwargs["body"][0]
        assert body["insight"] == "Dining trending up"

    def test_get_insights_filters_category(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [
                {"date": "2026-07-01", "category": "dining",
                 "month": "2026-07", "insight": "a"},
                {"date": "2026-07-02", "category": "general",
                 "month": "2026-07", "insight": "b"},
            ])
            rows = supabase_client.get_insights(category="dining")
        assert len(rows) == 1
        assert rows[0]["insight"] == "a"

    def test_journal_round_trip(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (201, None)
            result = supabase_client.write_journal_entry("long day",
                                                         date="2026-07-28")
        assert result["status"] == "ok"
        assert result["date"] == "2026-07-28"

        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [
                {"date": "2026-07-28", "reply_text": "long day",
                 "txn_ids_referenced": "", "tags": ""},
                {"date": "2026-06-01", "reply_text": "old",
                 "txn_ids_referenced": "", "tags": ""},
            ])
            rows = supabase_client.read_journal_entries(month="2026-07")
        assert len(rows) == 1


class TestSweep:
    def test_missed_matched_and_window(self):
        from tools import supabase_client
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        log_rows = [
            {"txn_date": today, "idempotency_key": "key_missed",
             "merchant": "LOST CAFE", "matched": "", "bank": "UOB",
             "type": "card", "amount": 9.9, "currency": "SGD",
             "payment_method": "UOB Card ending 5678",
             "webhook_status": "failed:529", "ts": "2026-07-28T01:00:00Z"},
            {"txn_date": today, "idempotency_key": "key_present",
             "merchant": "FOUND", "matched": "", "bank": "UOB",
             "type": "card", "amount": 1.0, "currency": "SGD",
             "payment_method": "", "webhook_status": "sent", "ts": ""},
            {"txn_date": today, "idempotency_key": "key_resolved",
             "merchant": "RESOLVED", "matched": "yes", "bank": "UOB",
             "type": "card", "amount": 2.0, "currency": "SGD",
             "payment_method": "", "webhook_status": "sent", "ts": ""},
            {"txn_date": "2020-01-01", "idempotency_key": "key_old",
             "merchant": "ANCIENT", "matched": "", "bank": "DBS",
             "type": "card", "amount": 3.0, "currency": "SGD",
             "payment_method": "", "webhook_status": "sent", "ts": ""},
        ]
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, log_rows),                              # webhook_log fetch
                (200, [{"idempotency_key": "key_present"}]),  # txn keys fetch
            ]
            result = supabase_client.sweep_missed_transactions(days_back=7)

        assert result["status"] == "ok"
        assert result["missed_count"] == 1
        assert result["missed"][0]["merchant"] == "LOST CAFE"
        assert result["total_webhook_logs"] == 3  # ancient row outside window


class TestDetectSubscriptionCreepNew:
    """The 'new' status — regression for the byte-parallel supabase port
    of the sheets_client fix (pre-fix code never assigned 'new', so
    new_this_month was always empty)."""

    @staticmethod
    def _recs(entries):
        """(date, merchant, amount) shorthand → transactions DB records."""
        return [
            _txn_rec(id=i, date=d, merchant=m, amount=a,
                     category="Entertainment", source="email")
            for i, (d, m, a) in enumerate(entries, start=1)
        ]

    def test_flags_new_subscription_crossing_two_month_threshold(self):
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            req.side_effect = [
                (200, self._recs([                    # ledger fetch
                    ("2026-03-05", "CURSOR AI", 20.00),
                    ("2026-04-05", "CURSOR AI", 20.00),
                ])),
                (200, []),                            # sub_overrides fetch
            ]
            result = supabase_client.detect_subscription_creep(months_back=3)

        assert result["subscriptions"][0]["status"] == "new"
        assert len(result["new_this_month"]) == 1
        assert result["new_this_month"][0]["category"] == "Entertainment"
        assert result["new_this_month"][0]["last_merchant"] == "CURSOR AI"
        assert result["total_monthly_subscriptions"] == 20.00

    def test_established_subscription_not_flagged_new(self):
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            req.side_effect = [
                (200, self._recs([                    # ledger fetch
                    ("2026-02-15", "APPLE.COM/BILL", 3.98),
                    ("2026-03-15", "APPLE.COM/BILL", 3.98),
                    ("2026-04-15", "APPLE.COM/BILL", 3.98),
                ])),
                (200, []),                            # sub_overrides fetch
            ]
            result = supabase_client.detect_subscription_creep(months_back=3)

        assert result["subscriptions"][0]["status"] == "active"
        assert result["new_this_month"] == []

    def test_price_change_beats_new(self):
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # +6.3% step: inside the ≤10% billing band, above the 5%
            # price-change threshold (the old 18.98 jump now trips the net).
            req.side_effect = [
                (200, self._recs([                    # ledger fetch
                    ("2026-03-01", "NETFLIX", 15.98),
                    ("2026-04-01", "NETFLIX", 16.98),
                ])),
                (200, []),                            # sub_overrides fetch
            ]
            result = supabase_client.detect_subscription_creep(months_back=3)

        assert result["subscriptions"][0]["status"] == "price_change"
        assert result["new_this_month"] == []


class TestSubOverrides:
    """sub_overrides dismissals (migration 0006, PWA companion PR).
    Supabase-only default fetch — the sheets twin takes exclude_keys
    explicitly; the filter inside the math block is parity. Since the
    category re-key the `merchant_key` column (name is historical)
    carries normalized CATEGORY keys."""

    @staticmethod
    def _recs(entries):
        """(date, merchant, amount) shorthand → transactions DB records."""
        return [
            _txn_rec(id=i, date=d, merchant=m, amount=a,
                     category="Entertainment", source="email")
            for i, (d, m, a) in enumerate(entries, start=1)
        ]

    def test_read_sub_overrides_fetches_table(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [
                {"merchant_key": "SPOTIFY", "verdict": "exclude"},
            ])
            rows = supabase_client.read_sub_overrides()
        call = req.call_args
        assert call.args[0] == "GET"
        assert call.args[1] == "sub_overrides"
        assert rows[0]["merchant_key"] == "SPOTIFY"
        assert rows[0]["verdict"] == "exclude"

    def test_exclude_verdict_skips_category(self):
        """A category dismissed via sub_overrides (the merchant_key column
        carries category keys since the re-key) never surfaces even when
        its pattern is perfectly subscription-shaped."""
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            req.side_effect = [
                (200, self._recs([                    # ledger fetch
                    ("2026-02-15", "APPLE.COM/BILL", 3.98),
                    ("2026-03-15", "APPLE.COM/BILL", 3.98),
                    ("2026-04-15", "APPLE.COM/BILL", 3.98),
                ])),
                (200, [{"merchant_key": "ENTERTAINMENT",   # sub_overrides fetch
                        "verdict": "exclude"}]),
            ]
            result = supabase_client.detect_subscription_creep(months_back=3)

        assert result["subscriptions"] == []
        assert result["total_monthly_subscriptions"] == 0

    def test_missing_sub_overrides_table_degrades_to_no_overrides(self):
        """Pre-migration deploys: the sub_overrides fetch raises (PostgREST
        404 → RuntimeError) and the detector degrades to 'no overrides'
        instead of breaking the subscription report."""
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            req.side_effect = [
                (200, self._recs([                    # ledger fetch
                    ("2026-02-15", "APPLE.COM/BILL", 3.98),
                    ("2026-03-15", "APPLE.COM/BILL", 3.98),
                    ("2026-04-15", "APPLE.COM/BILL", 3.98),
                ])),
                RuntimeError(                         # sub_overrides fetch (table absent)
                    "supabase GET sub_overrides: HTTP 404: relation "
                    "\"public.sub_overrides\" does not exist"),
            ]
            result = supabase_client.detect_subscription_creep(months_back=3)

        assert len(result["subscriptions"]) == 1
        assert result["subscriptions"][0]["category"] == "Entertainment"
        assert result["subscriptions"][0]["last_merchant"] == "APPLE.COM/BILL"

    def test_caller_supplied_set_skips_table_fetch(self):
        """An explicit exclude_keys set is used as-is (normalised
        trim+upper, same as the sheets twin) and sub_overrides is NOT
        fetched — only the ledger request fires."""
        from datetime import datetime
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req, \
             patch.object(supabase_client, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 16)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            req.side_effect = [
                (200, self._recs([                    # ledger fetch (only call)
                    ("2026-02-15", "APPLE.COM/BILL", 3.98),
                    ("2026-03-15", "APPLE.COM/BILL", 3.98),
                    ("2026-04-15", "APPLE.COM/BILL", 3.98),
                ])),
            ]
            result = supabase_client.detect_subscription_creep(
                months_back=3, exclude_keys={" entertainment "})

        assert result["subscriptions"] == []
        assert req.call_count == 1


class TestLoansDataLayer:
    def test_insert_loan_posts_row(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (201, [{
                "id": 7, "person": "Sarah", "amount": 50.0,
                "lent_date": "2026-07-02", "channel": "paylah",
                "txn_id": "", "status": "open", "repaid_date": None,
                "repay_txn_id": "", "notes": "",
            }])
            rec = supabase_client.insert_loan(
                person="Sarah", amount=50.0, lent_date="2026-07-02")
        assert rec["id"] == 7
        assert rec["repaid_date"] == ""   # None → '' like the rest of the layer
        call = req.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == "loans"
        body = call.kwargs["body"][0]
        assert body["person"] == "Sarah"
        assert body["channel"] == "paylah"

    def test_read_loans_filters_status_and_orders_oldest_first(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [{"id": 7, "repaid_date": None}])
            rows = supabase_client.read_loans(status="open")
        params = req.call_args.kwargs["params"]
        assert params["status"] == "eq.open"
        assert params["order"] == "lent_date.asc,id.asc"
        assert rows[0]["repaid_date"] == ""

    def test_update_loan_patches_by_id(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (200, [{"id": 7, "status": "repaid"}])
            rec = supabase_client.update_loan(7, {"status": "repaid"})
        assert rec["status"] == "repaid"
        call = req.call_args
        assert call.args[0] == "PATCH"
        assert call.kwargs["params"] == {"id": "eq.7"}
        assert call.kwargs["body"] == {"status": "repaid"}


class TestInsertTravelModeRow:
    def test_posts_trip_row(self):
        from tools import supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.return_value = (201, [{
                "id": 3, "label": "ID Sep 2026", "start_date": "2026-09-04",
                "end_date": "2026-09-11", "trip_category": "Travel - ID 2026-09",
                "budget_map": "", "total_budget": 2000, "notes": None,
            }])
            rec = supabase_client.insert_travel_mode_row(
                label="ID Sep 2026", start_date="2026-09-04",
                end_date="2026-09-11", trip_category="Travel - ID 2026-09",
                total_budget=2000.0)
        assert rec["label"] == "ID Sep 2026"
        assert rec["notes"] == ""   # None → ''
        call = req.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == "travel_mode"
        body = call.kwargs["body"][0]
        assert body["total_budget"] == 2000.0
        assert body["budget_map"] == ""


class TestRequestRetries:
    def test_transient_5xx_retried_then_succeeds(self):
        import urllib.error
        from tools import supabase_client

        calls = {"n": 0}

        class _Resp:
            status = 200
            def read(self):
                return b'[{"id": 1}]'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://x", 503, "unavailable", None, None)
            return _Resp()

        env = {"SUPABASE_URL": "https://x.supabase.co",
               "SUPABASE_SERVICE_KEY": "sk"}
        with patch.dict("os.environ", env), \
             patch.object(supabase_client.urllib.request, "urlopen",
                          side_effect=fake_urlopen), \
             patch.object(supabase_client.time, "sleep"):
            status, rows = supabase_client._request("GET", "transactions")

        assert status == 200
        assert rows == [{"id": 1}]
        assert calls["n"] == 2

    def test_hard_4xx_raises_runtime_error(self):
        import urllib.error
        from tools import supabase_client

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError("http://x", 400, "bad", None, None)

        env = {"SUPABASE_URL": "https://x.supabase.co",
               "SUPABASE_SERVICE_KEY": "sk"}
        with patch.dict("os.environ", env), \
             patch.object(supabase_client.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            with pytest.raises(RuntimeError):
                supabase_client._request("GET", "transactions")


class TestTimeInKey:
    def test_txn_time_changes_the_key_and_matches_formula(self):
        from tools import sheets_client, supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Personal - Food & Drinks"}]),  # resolve_category
                (200, "txn_20260725_002"),  # rpc/next_txn_id
                (201, [_txn_rec()]),        # insert
            ]
            result = supabase_client.append_transaction(
                "2026-07-25", "KOPITIAM @ RAFFLES", 7.80, "SGD",
                "Personal - Food & Drinks",
                payment_method="UOB Card ending 5678", txn_time="16:18",
            )
        assert result["idempotency_key"] == sheets_client._compute_idempotency_key(
            "2026-07-25", "KOPITIAM @ RAFFLES", 7.80, "UOB Card ending 5678", "16:18")
        assert result["idempotency_key"] == "5fa19a03294bdad9"  # pinned

    def test_empty_time_keeps_legacy_key(self):
        from tools import sheets_client, supabase_client
        with patch.object(supabase_client, "_request") as req:
            req.side_effect = [
                (200, [{"category": "Coffee"}]),  # resolve_category
                (200, "txn_20260416_001"),        # rpc/next_txn_id
                (201, [_txn_rec()]),              # insert
            ]
            result = supabase_client.append_transaction(
                "2026-04-16", "Starbucks", 5.50, "SGD", "Coffee",
                payment_method="DBS card ending 1234",
            )
        assert result["idempotency_key"] == "cb89d3a1d11dd274"  # legacy pin

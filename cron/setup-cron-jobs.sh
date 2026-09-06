#!/usr/bin/env bash
set -euo pipefail

echo "=== Setting up Hermes cron jobs for expense tracker ==="

# CLI shape (unchanged at HERMES_AGENT_SHA=29112bef / v0.21.0):
#   hermes cron create <schedule> [prompt] --skill X --skill Y --deliver target
# `schedule` and `prompt` are POSITIONAL; `--skill` is repeatable (NOT `--skills`).
# There is still NO per-job toolset flag, which is why the cron tool allowlist
# has to live in hermes-config/cli-config.yaml under `platform_toolsets.cron`.
#
# SILENCE (changed at the 0.20.6 bump): jobs that have nothing to report must
# reply with exactly `[SILENT]`, not an empty response. Two reasons. (1) The
# `send_message` tool no longer exists upstream — outbound messaging is
# gateway-side now — so telling the model to "skip send_message" points at
# nothing. (2) An EMPTY cron response does suppress delivery, but it is also
# booked as a soft-fail, which increments a persisted failure streak that
# later gets appended to the first GENUINE failure message as "this job has
# failed N runs in a row". Quiet nights were quietly poisoning that counter.
# `[SILENT]` suppresses delivery AND counts as success.

# Daily 9 PM — spending review that builds on prior insights
hermes cron create \
    "0 21 * * *" \
    "Daily 9 PM review — short, decision-grade, no filler. (1) Call get_remaining_budget. Print its _attention.lines VERBATIM as the warnings block (variable categories at 80%+ and fixed bills that came in OVER their usual amount) — do NOT list any other category, do NOT mention on-track categories, do NOT list a fixed bill (subscription, insurance, utilities) merely for being at 100%. If _attention.lines is empty, write one line: 'Budgets: nothing to flag.' (2) Call generate_daily_insight() exactly once. If it returns status='ok', print its insight text as ONE 💡 line and add a single concrete next step after it (e.g. 'Reply: cap <category> at \$X' or 'Reply: that was a trip cost'). If it returns 'exists' or 'no_match', print no insight at all — never pad with old insights; do NOT call get_insights or write_insight in this flow. (3) Call sweep_loan_offsets() exactly once; if count > 0 add one short line per completed repayment (e.g. 'Logged Adam's \$50 repayment ✓'); if count is 0 say NOTHING about loans. (4) End with: 💭 Journal: reply with one sentence if anything interesting happened today — I'll save it. Telegram formatting only: short lines, no markdown tables or headers. The whole message must fit on one phone screen." \
    --skill expense-tracker \
    --skill budget-manager \
    --deliver telegram
echo "Created: Daily 9 PM spending check"

# Friday 6 PM — weekly summary (+ cards on pace)
hermes cron create \
    "0 18 * * 5" \
    "Generate my weekly expense summary: total spent, top 3 categories, top merchants. Keep it fun. For budget pace, call get_remaining_budget and print ONLY its _attention.lines verbatim (skip the section if empty) — never list fixed bills (subscriptions, insurance, utilities) as watch/over, and never list on-track categories. Then append a 'Cards this month' section: call get_bonus_pool_status() and print its `lines` VERBATIM, one bullet each — do NOT call get_card_cap_status for this, do NOT add cards it omits, and never sum caps across categories into a per-card total. If get_bonus_pool_status returns status='setup_required', skip the cards section entirely — no message, no warning. Use Telegram-friendly formatting: short bullet lines, no markdown tables or headers." \
    --skill weekly-summary \
    --skill budget-manager \
    --skill card-optimiser \
    --deliver telegram
echo "Created: Friday 6 PM weekly summary"

# 1st of month 9 AM — monthly report (+ card plan + last-month card scorecard)
hermes cron create \
    "0 9 1 * *" \
    "Generate last month's final expense report first. Show total spent vs budget (red when over — say 'over by \$X', never a rounded-down 100%), the excluded_from_totals honesty line when anything was excluded, category breakdown, top merchants, and one suggestion for this month. Use Telegram-friendly formatting: short bullet lines, no markdown tables or headers. Then: (1) call plan_month() and present its plan_lines VERBATIM. Mention any active promos and their expiry dates. (2) Call review_card_efficiency(month=<last month in YYYY-MM>) and present its summary_line and top_missed_lines VERBATIM — never rebuild or paraphrase them. If either card tool returns status='setup_required', skip its section — don't warn the user." \
    --skill weekly-summary \
    --skill budget-manager \
    --skill card-optimiser \
    --deliver telegram
echo "Created: 1st of month monthly report"

# (The daily-noon "over 80%" job was removed 2026-08-17: with the 21:00
# review carrying the deterministic _attention block, a second daily
# budget ping was noise — one evening touchpoint was the call.
# Live disks still hold it until `hermes cron remove <id>`.)

# Sunday 10 PM — sweep for missed transactions (LLM/API failure recovery).
# Silent unless something is actually missed. days_back=7 matches cadence.
hermes cron create \
    "0 22 * * 0" \
    "Silently call sweep_missed_transactions(days_back=7). If missed_count is 0, reply with exactly [SILENT] and nothing else — do not message me. If missed_count > 0, send ONE Telegram message listing each missed transaction on its own short line (date, merchant, amount, payment_method), then ask which to log. Use Telegram-friendly formatting: short bullet lines, no markdown tables or headers. If the tool returns status='error', send a one-line heads-up: 'Sweep setup incomplete — the webhook_log table is unreadable.'" \
    --skill expense-tracker \
    --deliver telegram
echo "Created: Sunday 10 PM weekly sweep for missed transactions"

# 3 AM daily - rebuild the Google Sheet from Supabase (the ledger of
# record). The Sheet is a read-only human view + the free-tier backup copy;
# this replaced the dual-write mirror (migration PR 4). Silent on success.
hermes cron create     "0 3 * * *"     "Silently call export_sheet_backup. If status is ok OR status is setup_required (no Sheet configured), reply with exactly [SILENT] and nothing else - do not message me. If it returns status='error', send exactly one short line: 'Nightly sheet export failed: <message>'."     --skill expense-tracker     --deliver telegram
echo "Created: 3 AM nightly Sheet export from Supabase"

# 07:00 every Jan 1 - freeze the year that just ended into an immutable
# Archive-<year> tab in the backup Sheet (yearly cold storage). Write-once:
# an existing tab is never overwritten; the tool returns status='exists'.
hermes cron create     "0 7 1 1 *"     "Call archive_year_snapshot exactly once, with NO arguments - it defaults to the year that just ended. Then send exactly ONE short Telegram line and nothing else: if status='ok', 'Archived <transactions_archived> transactions for <year> into <tab>.'; if status='exists', '<tab> already exists - year already archived.'; if status='setup_required', reply with exactly [SILENT] (no Sheet is configured); if status='error', 'Year archive failed: <message>'. Do NOT retry the tool."     --skill expense-tracker     --deliver telegram
echo "Created: Jan 1 yearly cold-storage archive"

echo ""
echo "=== All cron jobs created ==="
echo "Verify with: hermes cron list"

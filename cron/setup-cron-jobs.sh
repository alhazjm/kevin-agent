#!/usr/bin/env bash
set -euo pipefail

echo "=== Setting up Hermes cron jobs for expense tracker ==="

# CLI shape per hermes_cli/main.py at HERMES_AGENT_SHA=73d0b083:
#   hermes cron create <schedule> [prompt] --skill X --skill Y --deliver target
# `schedule` and `prompt` are POSITIONAL; `--skill` is repeatable (NOT `--skills`).

# Daily 9 PM — spending review that builds on prior insights
hermes cron create \
    "0 21 * * *" \
    "Daily spending review. (1) First call get_insights for this month to see prior observations. (2) Then call get_remaining_budget to check today's numbers. (3) Send a brief summary in Telegram that CITES any relevant prior insights by date. Warn me if any category is over 80% used. Use Telegram-friendly formatting: short bullet lines or plain 'Category: spent / limit' rows. DO NOT use markdown tables (pipe syntax) or headers — Telegram does not render them. (4) Call generate_daily_insight() exactly once — it deterministically picks today's ONE noteworthy fact and writes the Insights row itself. If it returns status='ok', you may cite its insight text in your summary; on 'exists' or 'no_match', move on silently. Do NOT call write_insight yourself in this flow. (5) Call sweep_loan_offsets() exactly once — it completes the ledger for loans marked repaid via the PWA button. If count > 0, add one short line per completed repayment to the summary (e.g. 'Logged Adam's \$50 repayment ✓'). If count is 0, say NOTHING about loans — no mention, no 'no repayments today'. (6) End the message with: 💭 Journal: reply to this with one sentence if anything interesting happened today (decision, regret, good meal, stress) — I'll save it." \
    --skill expense-tracker \
    --skill budget-manager \
    --deliver telegram
echo "Created: Daily 9 PM spending check"

# Friday 6 PM — weekly summary (+ cards on pace)
hermes cron create \
    "0 18 * * 5" \
    "Generate my weekly expense summary with top categories, top merchants, and budget pace. Keep it fun. Then append a 'Cards on pace' section: call get_card_cap_status() and show one short bullet per card (traffic light 🟢/🟡/🔴, cycle spend / cap, percent used). If get_card_cap_status returns status='setup_required', skip the cards section entirely — no message, no warning. Use Telegram-friendly formatting: short bullet lines, no markdown tables or headers." \
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

# Daily noon — budget warning check
hermes cron create \
    "0 12 * * *" \
    "Silently check if any budget category is over 80% used. Only message me if there's a warning. When warning, use Telegram-friendly formatting: short bullet lines, no markdown tables or headers." \
    --skill budget-manager \
    --deliver telegram
echo "Created: Daily noon budget warning"

# Sunday 10 PM — sweep for missed transactions (LLM/API failure recovery).
# Silent unless something is actually missed. days_back=7 matches cadence.
hermes cron create \
    "0 22 * * 0" \
    "Silently call sweep_missed_transactions(days_back=7). If missed_count is 0, produce an EMPTY response and do NOT message me — skip send_message entirely. If missed_count > 0, send ONE Telegram message listing each missed transaction on its own short line (date, merchant, amount, payment_method), then ask which to log. Use Telegram-friendly formatting: short bullet lines, no markdown tables or headers. If the tool returns status='error' (WebhookLog tab missing), send a one-line heads-up: 'Sweep setup incomplete — WebhookLog tab not found.'" \
    --skill expense-tracker \
    --deliver telegram
echo "Created: Sunday 10 PM weekly sweep for missed transactions"

# 3 AM daily - rebuild the Google Sheet from Supabase (the ledger of
# record). The Sheet is a read-only human view + the free-tier backup copy;
# this replaced the dual-write mirror (migration PR 4). Silent on success.
hermes cron create     "0 3 * * *"     "Silently call export_sheet_backup. If status is ok, produce an EMPTY response and do NOT message me - skip send_message entirely. If status is 'setup_required' the Sheet backup is simply not configured, which is a supported setup - also produce an EMPTY response and say NOTHING. If it returns status='error', send exactly one short line: 'Nightly sheet export failed: <message>'."     --skill expense-tracker     --deliver telegram
echo "Created: 3 AM nightly Sheet export from Supabase"

# 07:00 every Jan 1 - freeze the year that just ended into an immutable
# Archive-<year> tab in the backup Sheet (yearly cold storage). Write-once:
# an existing tab is never overwritten; the tool returns status='exists'.
hermes cron create     "0 7 1 1 *"     "Call archive_year_snapshot exactly once, with NO arguments - it defaults to the year that just ended. Then send exactly ONE short Telegram line and nothing else: if status='ok', 'Archived <transactions_archived> transactions for <year> into <tab>.'; if status='exists', '<tab> already exists - year already archived.'; if status='error', 'Year archive failed: <message>'. Do NOT retry the tool."     --skill expense-tracker     --deliver telegram
echo "Created: Jan 1 yearly cold-storage archive"

echo ""
echo "=== All cron jobs created ==="
echo "Verify with: hermes cron list"

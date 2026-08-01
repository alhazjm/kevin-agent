---
name: card-optimiser
description: Recommends the best credit card per category, tracks monthly cap progress, and scores miles earned vs optimal
version: 1.4.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, cards, miles, optimisation]
---

# Card Optimiser

## When to Use
- The user asks which card to use for a purchase ("best card for dinner?", "which card for $80 at Cold Storage?")
- The user asks about cap progress ("am I close to the DBS dining cap?", "where are my cards this cycle?")
- The user says "plan my month" / "show me the card plan"
- The user wants a month-end scorecard ("how did I do on miles last month?")
- The user declares a promo override ("DBS is doing 10 mpd on dining until Apr 30, switch my dining primary")
- The 1st-of-month or Friday cron asks for a card briefing / scorecard

You share the expense-tracker toolset. Do NOT redefine categories, MerchantMap
rules, or the log_expense flow — they live in `skills/expense-tracker/SKILL.md`.

## HARD RULES

1. **Setup-gate behaviour.** Every card tool returns
   `{"status": "setup_required", "message": "..."}` when the `Cards` and
   `CardStrategy` tabs aren't populated (or the `_default` sentinel row is
   missing). When you see that status, surface the message as ONE short line
   and stop. Do NOT invent card data, card_ids, or earn rates. Example:
   ```
   🃏 Card optimiser not ready yet — populate the Cards and CardStrategy
   tabs first (see sheets-template/README.md).
   ```

2. **ALL nudges are automatic — you do NOT send any of them.** Three
   hooks fire from `log_expense` itself and send their own Telegram
   bubbles; you never call them, never replicate their messages, and
   never "double up" with a chat reply about them:
   - **Post-cap** (`maybe_send_post_cap_nudge`): card crosses 80%/100% of
     its category cap — once per (cycle, card, category, threshold).
   - **Min-spend** (`maybe_send_min_spend_nudge`): calendar-month based —
     at most ONE "minimum met ✓" per card per month (fired by the
     crossing transaction) plus ONE at-risk warning when ≤5 days remain
     and the card is still short. Never nag daily.
   - **Steering** (`maybe_send_steer_nudge`): a merchant landed on a card
     the strategy wouldn't pick (e.g. a yuu partner charged to UOB) —
     one gentle after-the-fact line, at most once per merchant pattern
     per calendar month. Big one-offs on Vantage are deliberately exempt.
     Steering priorities (v2): a card still short of its min spend with
     ≤5 days left beats rate-chasing — if the txn is already on that
     card the steer stays silent; otherwise it points there instead.
     Steer targets whose bonus pool is already capped are skipped (next
     candidate with headroom, or silence).

3. **Telegram-friendly formatting only.** Short bullet lines, no markdown
   tables, no headers. Same rule as expense-tracker.

## Tools

| Tool | When to call |
|---|---|
| `get_card_cap_status(card_id?, category?)` | User asks about cap progress, Friday summary briefing |
| `get_bonus_pool_status(month?)` | User asks about a card's monthly bonus-cap headroom ("how's my contactless cap?", "how much left on Revo?") |
| `recommend_card_for(category, amount?)` | User asks which card to use for a purchase |
| `plan_month(month?)` | 1st-of-month briefing, user asks "what's my card plan?" |
| `review_card_efficiency(month?)` | Month-end scorecard (1st-of-month cron on last month) |
| `set_category_primary(category, card_id, until_date?, ...)` | Manual promo override |

`get_bonus_pool_status` tracks CALENDAR-month bonus caps (distinct from
the cycle-window category caps above). UOB Preferred returns TWO pools —
contactless and online, S$600 each — attributed by merchant class since
bank alerts never reveal the channel. Present one short line per pool:

```
💳 Preferred this month: tap $413 / $600 (69%) · online $88 / $600 (15%)
💳 Revolution: $640 / $1,000 (64%)
```

If it returns `status="setup_required"`, say so in one line and stop.

## Flow: "which card should I use?"

1. Run the categorisation flow from expense-tracker (MerchantMap →
   always-ask list → own judgment) to pick a category for the purchase.
2. Call `recommend_card_for(category, amount)`.
3. Present the recommendation in one short line with the reason:
   ```
   💳 DBS Altitude for dinner — 4 mpd, dining cap at 62% this cycle.
   ```
4. If `primary_status == "warning"`, mention the cap is close.
5. If the tool returns `status: "setup_required"`, apply HARD RULES #1.

## Flow: cap progress / weekly briefing

When the Friday cron asks for a "cards on pace" section, call
`get_card_cap_status()` and produce one line per card:

```
💳 Cards this cycle:
  🟢 DBS Altitude: $412 / $1000 dining (41%)
  🟡 UOB PRVI: $820 / $1000 general (82%) — watch
  🔴 Citi PM: $1040 / $1000 grabs (100%) — on fallback
```

Traffic lights: 🟢 ok (<80%), 🟡 warning (80–99%), 🔴 capped (≥100%).

## Flow: 1st-of-month briefing

The 1st-of-month cron passes both `expense-tracker` and `card-optimiser`
skills. After the expense report is presented, extend it with:

1. Call `plan_month()` and present its `plan_lines` VERBATIM — one
   bullet each, under a `💳 This month's plan` header line. The tool has
   already collapsed every category that just follows the default row
   into a single "Everything else →" line; do NOT re-expand the full
   `plan` list into one line per category, and do NOT re-derive lines
   from `plan` yourself.
2. Mention any active promo overrides (`promo_active_until` non-empty)
   and when they expire.
3. Mention any `reverted_promos` from the result — these are promos that
   just lazily reverted, so the user knows the plan changed back.
4. Call `review_card_efficiency(month=<last month>)` and present the
   scorecard using the tool's preformatted strings VERBATIM —
   `summary_line` first, then `top_missed_lines` as-is:
   ```
   📊 March scorecard:
   You earned 4,320 miles · optimal would have been 4,642 · left on the
   table: 322
     • NTUC ($85.00) — used DBS Altitude, should have been UOB PRVI (–170)
     • Warung Gembira ($42.00) — used Citi PM, should have been DBS (–118)
   ```
   Copy card names EXACTLY as the lines carry them — a Jul 2026 recap
   paraphrased a line and swapped in the wrong card. Never rebuild these
   lines from `transactions_suboptimal`.
5. When `unmapped_payment_methods` > 0, add one honest line:
   `(<N> txns had no matching card and scored 0 in this model.)`

## Flow: promo override

User says something like:

> "DBS is running 10 mpd on dining until April 30, switch my dining primary to
> DBS Altitude until then."

1. Confirm the category matches a Budget-tab category exactly (re-fetch
   via `get_remaining_budget` if unsure).
2. Call:
   ```
   set_category_primary(
     category="Personal - Food & Drinks",
     card_id="dbs-altitude",
     until_date="2026-04-30",
     earn_rate=10,
   )
   ```
3. Confirm briefly:
   ```
   ✅ Dining → DBS Altitude @ 10 mpd until 2026-04-30, reverts after.
   ```
4. On the first `plan_month` call after the expiry date, the prior row
   is automatically restored from the `||PREV:` snapshot in `notes`.
   You don't need to clean up manually.

## Cycle Math — Non-Obvious Bits

- Each card has its own `cycle_start_day` (1–31). When day > days-in-month
  (e.g. 31 in Feb), it clamps to the last day of the month.
- "Cycle" ≠ calendar month. Always use `cycle_window` from the tool
  payload — don't assume 1st-to-last-of-month.
- Backfill and `UNCATEGORIZED` transactions are excluded from cycle spend
  totals (same rule as the rest of expense-tracker).

## Important Notes

- Card data (card_id, display_name, cycle_start_day, etc.) lives in the
  `Cards` tab. Strategy (primary, cap, earn rate, fallback) lives in
  `CardStrategy`. Both have a schema doc in `sheets-template/README.md`.
- Nudge dedup lives in the `CardNudgeLog` tab (auto-created by the helper).
  At most one nudge per (cycle, card, category, threshold).
- The post-cap nudge is suppressed at 100% if the 80% nudge already fired
  AND the last 2 transactions in that category used the fallback card —
  "silent when already switched" (user's already being disciplined).
- Payment-method match is case-insensitive substring, longest pattern wins
  (same rule as MerchantMap).
- All tool results use Telegram-friendly formatting hints. No markdown
  tables. No headers.

# Card Optimiser — Architecture

Status: **designed, not yet built**. Sibling skill to `expense-tracker`.
One-profile multi-skill (see "Profiles vs multi-skill" below for why not
a separate Hermes Profile).

## Problem

Every transaction logged to the Transactions tab has a `Payment Method`
column (e.g. `"DBS/POSB card ending 1234"`, `"UOB Card ending 5678"`,
`"PayLah! Wallet (Mobile ending 9876)"`). Each card has:

- A **headline earn rate** per spend category (e.g. 4 mpd on dining, 1.4
  mpd on everything else), and
- A **monthly cap** on that headline rate; once crossed, the earn drops
  to a fallback rate (commonly 0.4 mpd) for the rest of the cycle.

"Optimal" spending means using the **right card per category** and
**switching to a fallback** once the primary's cap is hit. In practice
this means the user forgets which card they planned to use, blows past
caps without noticing, and leaves miles on the table.

**Out of scope**: per-transaction real-time "tap Card X now"
recommendations driven by promo scraping. Too much maintenance for too
little gain (promos rotate monthly, per-bank MCC quirks, user friction
at point of sale). See "Non-goals" below.

## Goals

1. A **static monthly strategy** — which card to use for which category
   — that the user memorises and occasionally queries.
2. **Event-driven nudges** when a cap is crossed ("DBS dining cap hit —
   switch to UOB for the rest of the month").
3. **Post-mortem scorecard** at month end: "you left 320 miles on the
   table by using Citi for dining twice" — the enforcement loop that
   keeps the system honest.
4. **Manual promo override**: when a bank runs a short-term promo
   (10 mpd on dining for April), the user tells the bot in one message
   and it auto-reverts at an end date. No scraping.

## Non-goals

- No per-transaction POS recommendations. User does the spending.
- No MCC-level modelling. Earn rates are category-keyed.
- No promo scraping from bank emails. Promos are manual overrides.
- No separate Hermes Profile / separate bot token. See below.

## Profiles vs multi-skill — decision recorded

Hermes Profiles (upstream NousResearch feature, shipped in v0.6.0) do
exist in the fork. A Profile is a **fully isolated Hermes instance**:
its own `HERMES_HOME`, config, bot token, skills, memories, sessions,
gateway process. Wrong primitive for card-optimiser because:

- Card-optimiser needs to **hook into the `log_expense` flow** to fire
  the post-cap nudge. Cross-profile tool invocation in the same turn
  isn't supported — you'd need shared state over the sheet and a
  second agent turn, which adds latency and failure modes.
- The MerchantMap (which card-optimiser needs for category lookup)
  already lives in a Google Sheet — both skills point at the same sheet
  via `GSPREAD_SPREADSHEET_ID`. No Profile needed to "share" it.
- A separate Profile means a second Telegram bot in the user's app and
  a second Render service (or a dual-gateway rewrite). Both cost real
  money + cognitive load for zero gain.

**Decision**: card-optimiser is a **second skill in the same Hermes
profile** as expense-tracker. Both share the 15+ tools already
registered; card-optimiser adds ~5 new tools on top. Profiles will
become relevant only if a second tenant exists (long-term roadmap
item #10).

## Sheet schema

Two new tabs, zero changes to existing tabs.

### `Cards` tab (per-card metadata)

| Col | Header | Type | Notes |
|---|---|---|---|
| 1 | `card_id` | Text | Stable key, e.g. `dbs-altitude`, `uob-prvi`, `citi-pm` |
| 2 | `display_name` | Text | Human-readable name, e.g. `DBS Altitude Visa` |
| 3 | `payment_method_pattern` | Text | Substring match against Transactions.`Payment Method`, case-insensitive. E.g. `DBS/POSB card ending 1234` or just `UOB Card ending 5678`. Longest-match-wins if multiple cards share a bank. |
| 4 | `cycle_start_day` | Integer | Day of month the statement cycle closes + resets (1–31). |
| 5 | `min_spend_bonus` | Number | Optional: minimum spend to unlock earn rate (some cards). `0` = no minimum. |
| 6 | `notes` | Text | Free-text for quirks ("floor not ceiling", "promo until Jun") |

### `CardStrategy` tab (category → primary+fallback mapping)

| Col | Header | Type | Notes |
|---|---|---|---|
| 1 | `category` | Text | Budget category or `_default` catch-all |
| 2 | `primary_card_id` | Text | `card_id` from Cards tab |
| 3 | `primary_cap` | Number | Monthly cap amount at headline rate; `0` or blank = unlimited |
| 4 | `primary_earn_rate` | Number | mpd at headline rate |
| 5 | `fallback_card_id` | Text | `card_id` to switch to after cap. Optional. |
| 6 | `fallback_earn_rate` | Number | mpd on fallback |
| 7 | `promo_active_until` | Date (YYYY-MM-DD) | Optional: when this row was set via `set_category_primary` override, auto-revert after this date |
| 8 | `notes` | Text | Free-text, e.g. "DBS 4mpd dining" |

**Sentinel row**: one row with `category = "_default"` provides the
catch-all card for uncategorised spend. Always present.

### No change to `Transactions` or `MerchantMap`

Card identification happens via `payment_method_pattern` match against
the existing `Payment Method` column. Category resolution already
happens via MerchantMap in expense-tracker. Zero schema churn.

## Tool surface

All new tools go into `tools/expense_sheets_tool.py`, follow the same
register pattern, and need to be added to the Dockerfile sed injection
(CLAUDE.md §Dockerfile gotchas).

### 1. `get_card_cap_status(card_id?, category?)`

Read-only. Returns MTD spend per (card, category) vs. cap. Honours
per-card `cycle_start_day` — MTD means "since the last cycle reset",
not calendar month.

```json
{
  "as_of": "2026-04-22",
  "cards": [
    {
      "card_id": "dbs-altitude",
      "display_name": "DBS Altitude Visa",
      "cycle_start_day": 15,
      "cycle_window": ["2026-04-15", "2026-05-14"],
      "categories": [
        {
          "category": "Personal - Food & Drinks",
          "spent_in_cycle": 847.20,
          "cap": 1000.00,
          "percent_used": 84.7,
          "status": "warning",
          "earn_rate_current": 4.0,
          "fallback_card_id": "uob-prvi",
          "fallback_earn_rate": 2.4
        }
      ]
    }
  ]
}
```

Status enum: `ok` (<80%), `warning` (80–99%), `capped` (≥100%).

### 2. `recommend_card_for(category, amount?)`

One-off query: "which card should I use for $80 at X?". Uses
MerchantMap → category → CardStrategy → honours cap status. Returns
primary if within cap, fallback otherwise.

```json
{
  "category": "Personal - Food & Drinks",
  "recommendation": {
    "card_id": "uob-prvi",
    "display_name": "UOB PRVI Miles",
    "reason": "DBS Altitude dining cap is at 85% — switching to UOB keeps you at 2.4 mpd instead of dropping to 0.4."
  }
}
```

### 3. `plan_month(month?)`

Returns current CardStrategy + MTD cap status. Called by the 1st-of-
month cron to produce the monthly briefing, and can be queried ad-hoc.
Deterministic formatting hints for Telegram-friendly output in the
result payload.

### 4. `review_card_efficiency(month?)`

The enforcement loop. Walks every Transaction in the month, for each
one computes "what was the optimal card at the time of the transaction
given its category + the cap state then?", diffs against the actual
card used, and reports miles left on the table. Also surfaces
per-transaction detail for explanation.

```json
{
  "month": "2026-04",
  "miles_earned_actual": 4320,
  "miles_earned_optimal": 4642,
  "miles_left_on_table": 322,
  "transactions_suboptimal": [
    {
      "txn_id": "txn_20260404_002",
      "merchant": "Warung Gembira",
      "amount": 42.00,
      "actual_card": "citi-pm",
      "actual_earn_rate": 1.2,
      "optimal_card": "dbs-altitude",
      "optimal_earn_rate": 4.0,
      "miles_lost": 118
    }
  ]
}
```

### 5. `set_category_primary(category, card_id, until_date?, earn_rate?, cap?)`

Manual promo override. Writes/updates a `CardStrategy` row. If
`until_date` is set, the row is tagged with `promo_active_until`; a
nightly cron (or the `plan_month` tool on first call after the date)
reverts the row to the prior config.

Stored-reversion pattern: the tool writes the previous config as a
comma-separated snapshot into the `notes` column, keyed with a
`PREV:` prefix. Reversion reads `PREV:` out of `notes` and restores.
Not elegant but avoids a second sheet tab for promo history.

## Event-driven post-cap nudge

This is the one place card-optimiser reaches into expense-tracker's
flow. Implementation: a **helper function** called from
`handle_log_expense` after the confirmation bubble is sent:

```python
# In expense_sheets_tool.py, inside handle_log_expense (bottom of file,
# after bubble_sent and linked are set):

card_nudge = card_optimiser.maybe_send_post_cap_nudge(
    payment_method=payment_method,
    category=category,
    amount=amount,
    date=date,
)
if card_nudge.get("sent"):
    result["card_nudge_sent"] = True
```

The helper:

1. Resolves `payment_method` → `card_id` via Cards.`payment_method_pattern`
2. Looks up CardStrategy for `category`
3. If the card just became `capped` with this transaction (or crosses
   80% for the first time this cycle) **and** a fallback exists with a
   better earn rate **and** the nudge hasn't already fired this cycle
   for this (card, category) pair → sends a second Telegram bubble:

   ```
   ⚠️ DBS Altitude just hit its dining cap.
   Switch to UOB PRVI for dining — 2.4 mpd instead of 0.4.
   ```

4. Writes a row to a new `CardNudgeLog` internal tab (or reuses
   `Insights`) to prevent re-firing within the same cycle.

**Silent-when-already-switched logic**: the 100% nudge is suppressed
if the 80% nudge fired AND the next N (≥2) transactions in that
category used the fallback card. Tracks discipline without nagging.

## Cron integration

No new daily card cron. Piggyback on existing schedules.

### 1st of month 9 AM — monthly report (extend existing)

Add to the existing monthly-report cron prompt:

> After the expense report, call `plan_month()` for the current month
> and present the card strategy: "Use X for Y, caps at Z, fallback to
> W." Then call `review_card_efficiency(month=<last_month>)` and
> surface the scorecard: "You earned N miles last month, optimal was
> M — you left (M-N) on the table on K transactions."

### Friday 6 PM — weekly summary (extend existing)

Add to the existing weekly-summary prompt:

> Include a "cards on pace" section: for each card in CardStrategy,
> show `spent_in_cycle / cap` and flag any in `warning` state with the
> expected switch date at current pace.

### No daily card cron

Daily card status is noise — most days nothing meaningful changes.
Event-driven post-cap nudges handle urgency; weekly/monthly crons
handle summary.

## Data flow

```
Incoming webhook (DBS/UOB email)
  → hermes webhook handler
    → log_expense
      → append_transaction (Transactions tab)
      → send confirmation bubble
      → card_optimiser.maybe_send_post_cap_nudge()
        → if cap crossed + fallback better + not already nudged
          → send second Telegram bubble
          → log to CardNudgeLog
```

Everything non-hot-path stays in the sheet — no in-memory cache, no
Redis, no schema migrations on the existing tabs.

## Tools + sheet tabs checklist (implementation)

- [ ] Add `Cards` tab to sheet (one-time setup, hand-populated)
- [ ] Add `CardStrategy` tab to sheet with `_default` row
- [ ] Add `card_optimiser` module (separate file:
      `tools/card_optimiser.py`) or extend `sheets_client.py` with the
      card functions. Keep handler registrations in
      `tools/expense_sheets_tool.py` next to the rest.
- [ ] Register 5 tools: `get_card_cap_status`, `recommend_card_for`,
      `plan_month`, `review_card_efficiency`, `set_category_primary`.
- [ ] Add all 5 to Dockerfile sed injection (CLAUDE.md §2).
- [ ] Add `skills/card-optimiser/SKILL.md` with tool-use rules and
      the post-cap nudge integration contract.
- [ ] Update cron `setup-cron-jobs.sh`: 1st of month + Friday 6 PM
      prompts (extensions, not new jobs).
- [ ] Wire `maybe_send_post_cap_nudge` into `handle_log_expense`.
- [ ] Tests: `tests/test_card_optimiser.py` covering cap crossing,
      cycle-day math, fallback-already-used silencing, MerchantMap →
      category → card resolution.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `payment_method_pattern` mismatch (card last-four typo) | Medium | Card reads as "unknown" → no nudge | `get_card_cap_status` surfaces unmapped payment methods in its result; user can edit Cards row. |
| Cycle-day math edge case (31st of month on a 30-day month) | Low | Cycle window wrong for 1-2 days | Clamp cycle_start_day to `min(cycle_start_day, days_in_month)`. |
| Nudge spam on rapid-fire transactions | Medium | User annoyed, ignores nudges | Silent-when-already-switched + per-cycle dedup (CardNudgeLog). |
| Manual promo override gets forgotten | Medium | User keeps earning at base rate thinking promo is still active | `plan_month` on 1st + Friday summary explicitly list active overrides and their expiry. |
| `review_card_efficiency` contradicts user's actual decision | Low | Report says "left miles on table" when user had a good reason | Scorecard is descriptive, not prescriptive. Pair with Journal entries (reply=journal) for context. |
| CardStrategy row drift between user expectation and sheet reality | Medium | Bot and user disagree about primary card | `plan_month` on the 1st restates the plan; any ad-hoc edit routes through `set_category_primary` which updates the sheet. |

## Open decisions

1. **Separate skill file vs. folded into expense-tracker?** Separate.
   `skills/card-optimiser/SKILL.md` keeps the instruction surface
   focused and lets the LLM reason about card-optimiser concerns
   (caps, cycles, overrides) without bloating the expense-tracker
   prompt. Cron jobs invoke `--skill card-optimiser --skill
   expense-tracker` when the task spans both domains.

2. **CardNudgeLog tab vs. reuse Insights?** CardNudgeLog. Insights is
   narrative; nudge-log is event state. Mixing them pollutes
   `get_insights` results. Small new tab.

3. **Handle MCC-level merchant promos?** Defer. Route them as
   MerchantMap entries with a special category like
   `Food_Promo_Tier1`, with a corresponding CardStrategy row. Only
   build if the user hits a real need.

## Exit criteria for v1

- [ ] 1st of month briefing fires and shows current strategy + last
      month's scorecard, using Telegram-friendly formatting (no
      pipe-tables).
- [ ] A test transaction that crosses an 80% cap triggers the nudge
      bubble within 2 seconds of `log_expense`.
- [ ] A follow-up transaction at 101% does NOT double-nudge (silent-
      when-already-switched logic holds).
- [ ] `review_card_efficiency` returns numbers that match a manual
      spreadsheet calculation on the same month's data.
- [ ] `set_category_primary(until_date=...)` reverts correctly on the
      day after the expiry.

Once v1 ships, defer all expansion (multi-card-chaining, MCC promos,
category splits) until real usage reveals which matters.

---
name: budget-manager
description: Handles budget queries, warnings, reallocation, and the guilt-free calculator
version: 3.1.0
author: alhazjm
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, budget, management]
---

# Budget Manager

## When to Use
- The user asks about their budget or spending
- The user sends `/budget` or `/budget [category]`
- A cron job triggers a budget check
- The user wants to reallocate budget between categories
- The user asks "Can I afford X?"
- The user wants to change a budget limit (including for a specific month)

## Fixed vs variable (read this first)

Every row from `get_remaining_budget` carries `kind`: `fixed` (subscriptions,
insurance, utilities, phone — bills that land at ~100% every month by
design) or `variable` (everything else). The two are treated differently
EVERYWHERE in this skill:

- A fixed bill sitting at 100% is its normal state — it is NEVER a warning,
  never 🔴, never listed under "watch/over". Do not mention it.
- A fixed bill matters only when it comes in OVER its usual amount (price
  increase, double charge). The tool already detects that: it appears in
  `_attention.fixed_over` and as a 🧾 line in `_attention.lines`.
- Warnings are precomputed: `_attention.lines` = variable categories at
  80%+ plus fixed bills over their usual amount. Print those lines
  VERBATIM. Never re-derive warnings from the per-category rows, never add
  categories the tool didn't flag.

## Budget Queries

When the user asks "How much do I have left?" or sends `/budget`:

1. Call `get_remaining_budget` with the relevant category (or all)
2. Format clearly — VARIABLE categories sorted by percent used (highest
   first), skip categories at 0% unless showing all. Fixed bills collapse
   to a single line: `Fixed bills: N charged so far` (list them only if the
   user asks about a specific one).
3. Use traffic-light circles for status (variable rows only):
   - 🟢 Under 50% used
   - 🟡 50–80% used
   - 🔴 Over 80% used (add ⚠️)

```
💰 Budget Status — April

  🔴 Food & Drinks   $245 left of $400 ⚠️ (80% used)
  🟡 Transport       $42 left of $75 (44% used)
  🟢 Groceries       $310 left of $500 (38% used)

Fixed bills: 6 charged so far, all at their usual amounts

Total: $2,600 remaining of $3,300
```

Keep numbers clean — round to whole dollars unless cents matter.

## Budget Warnings (cron and on request)

Print `_attention.lines` verbatim — nothing else qualifies as a warning.
When the list is empty, say so in one line ("Budgets: nothing to flag.").
When it isn't, add days remaining in the month for context and, for a
variable category, one concrete next step (a surplus to shift from, or a
cap to set):

```
🔴 Food & Drinks: $340 / $400 (85%) — 17 days left
💡 Entertainment only at 30% — could shift $50 if needed
🧾 Spotify came in $0.98 over its usual $12.00 — price change or double charge?
```

## Budget Reallocation

When the user says "Move $50 from Dining to Transport":

1. Call `get_remaining_budget` to see current state
2. Call `update_budget` for each affected category
3. Confirm concisely:
   ```
   ✅ Done: Food $400→$350 🍜, Transport $75→$125 🚌
   ```

## Setting a Specific Month's Budget

When the user says "set Food budget to $500 for June" or "change my June groceries to $400":
1. Identify the month number (June = 6)
2. Call `update_budget` with the `month` parameter (1-12)
3. Confirm: `✅ June budget updated: Groceries → $400 🛒`

## Guilt-Free Calculator

When the user asks "Can I afford a $150 jacket?":

1. Call `get_remaining_budget` for the relevant category
2. Calculate: remaining after purchase, days left, daily rate
3. Give a straight answer:
   ```
   🛍️ Shopping: $280 left, 17 days to go
   After jacket: $130 left ($7.65/day)
   👍 You can swing it — just watch the rest of the month
   ```
   Use 👍 if comfortable, 😬 if tight, ❌ if over budget.

## Response Style
- Be concise — no walls of text
- Use traffic-light circles (🟢🟡🔴) for at-a-glance status
- Round to whole dollars
- Be honest but not guilt-tripping
- End with something light when things look good

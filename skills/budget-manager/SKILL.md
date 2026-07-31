---
name: budget-manager
description: Handles budget queries, warnings, reallocation, and the guilt-free calculator
version: 3.0.0
author: Hadi
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

## Budget Queries

When the user asks "How much do I have left?" or sends `/budget`:

1. Call `get_remaining_budget` with the relevant category (or all)
2. Format clearly — sort by percent used (highest first), skip categories at 0% unless showing all
3. Use traffic-light circles for status:
   - 🟢 Under 50% used
   - 🟡 50–80% used
   - 🔴 Over 80% used (add ⚠️)

```
💰 Budget Status — April

Variable:
  🔴 Food & Drinks   $245 left of $400 ⚠️ (80% used)
  🟡 Transport       $42 left of $75 (44% used)
  🟢 Groceries       $310 left of $500 (38% used)

Fixed: all on track ✅

Total: $2,600 remaining of $3,300
```

Keep numbers clean — round to whole dollars unless cents matter.

## Budget Warnings

When any category exceeds 80% usage:
- Flag it clearly with ⚠️ and 🔴
- Show days remaining in the month for context
- Suggest which categories have surplus

```
⚠️ Food & Drinks at 85% ($340/$400) — 17 days left
💡 Entertainment only at 30% — could shift $50 if needed
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

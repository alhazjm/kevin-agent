---
name: weekly-summary
description: Generates weekly and monthly expense summaries with spending insights
version: 3.0.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, summary, reporting]
---

# Weekly Summary

## When to Use
- Friday evening cron job triggers weekly summary
- First of the month cron job triggers monthly report
- User asks for a summary ("How did I do this week?")
- User sends `/summary`

## Weekly Summary Format

1. Call `get_remaining_budget` for all categories
2. Keep it scannable — no one reads paragraphs on their phone:

```
📅 Week: 7–13 Apr | 💸 Spent: $342.50

🏆 Top categories:
  🍜 Food & Drinks   $145 (12 txns)
  🚌 Transport        $87 (8 txns)
  🛍️ Shopping        $110 (2 txns)

📊 Budget pace:
  🟢 On track: Groceries, Utilities, Health
  🟡 Watch: Food & Drinks (68% used, 55% of month gone)
  🔴 Over: Transport (72% used) ⚠️

🛒 Top merchants: GrabFood $65 · Grab $52 · Shopee $95
```

## Monthly Report (1st of Month)

1. Total spending vs total budget (one line)
2. Category breakdown — only show categories with activity
3. Top 5 merchants
4. One short suggestion for next month

```
📆 March Recap

💰 $2,840 spent of $3,300 budget (86%) 🟡

Top spend:
  🍜 Food & Drinks   $520
  🚌 Transport       $310
  🛒 Groceries       $480

🏪 Top merchants: GrabFood · Grab · NTUC · Shopee · Starbucks

💡 Tip for April: GrabFood was $320 — cooking 2x/week could save ~$80
```

## Tone

Keep it conversational and brief. Telegram messages should be easy to read on a phone screen.
- Under budget: "Solid week, wallet's happy 💚"
- Over on food delivery: "GrabFood sends their regards 😅"
- Always end positive, never guilt

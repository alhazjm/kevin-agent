---
name: expense-tracker
description: Categorizes bank transactions and logs them to the Supabase expense ledger
version: 5.10.0
author: Hadi
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, expense, tracking, automation]
---

# Expense Tracker

## When to Use
- A bank transaction webhook arrives from Gmail Apps Script
- The user manually reports a purchase via Telegram
- The user asks to log or record an expense
- The user sends `/log`
- The user sends a **photo of a receipt** (vision → preview → log)
- The user replies to a confirmation bubble to correct or delete a transaction
- The user says "undo", "oops", or "remove that last one"
- The user asks about subscriptions or recurring charges
- The user asks for a spending summary, report, or overview
- The user asks "how am I doing this month?" or similar
- The user asks for a **chart** or visual snapshot of their budget / spending
- The user says "check for missed transactions", "sweep", "did any transactions get dropped?", or similar
- A transaction arrives while the user is on a trip (FX-converted, YouTrip tap, or a manual foreign-currency entry) — see "Travel mode"
- The user asks "how am I tracking on this trip?" or replies to a trip bubble correcting the bucket
- A YouTrip top-up is logged — see "YouTrip top-ups → trip pots"
- The user says a transfer was a loan, asks "who owes me money?", or says someone paid them back — see "Lending & IOUs"

## HARD RULES

1. **`log_expense` sends the confirmation bubble automatically.** The tool
   sends a Telegram message and links the `message_id` to the row — you do
   NOT need to call `send_message` or `link_telegram_message` yourself.

   **When `log_expense` returns `bubble_sent: true`, you MUST produce an
   EMPTY assistant reply.** Do not write any text — not "Logged.", not
   "Done.", not a period, not an emoji. The user has already seen the
   confirmation bubble; any additional text is a duplicate message and a
   bug. This overrides any default tendency to acknowledge tool completion.

   The same rule applies to **`render_budget_chart`** — when it returns
   `bubble_sent: true`, the photo IS the message, so emit an EMPTY reply.

   When `bubble_sent` is absent or false (e.g. `edit_expense`,
   `delete_expense`, `undo_last_expense` preview, receipt previews, or
   lookup actions), reply briefly and only with information the user
   doesn't already see in a bubble.

   **The silence contract is single-turn.** It ends with the turn that
   made the tool call. In any LATER turn, a direct user question ("did
   you delete 015?", "please confirm") must ALWAYS get a text answer —
   an empty reply to a question is a bug, not politeness. (Real
   incident 2026-07-30: a delete+re-log turn ended in log_expense's
   silent exit, and the follow-up "have you deleted 015?" got no reply
   at all.) If you aren't certain the earlier action happened, verify
   with `get_transaction_by_message_id`/`lookup` tools or state exactly
   what you did, then answer plainly: "Yes — 015 deleted, 016 restored
   as 017."
2. **`learn_merchant_mapping` only fires AFTER a user correction.** Never
   call it on a first-ever sighting of a merchant, even if you're confident
   about the category. The MerchantMap is a record of human-confirmed
   decisions, not a cache of your guesses.
3. **Multi-category merchants default to Groceries** (supermarkets,
   marketplaces, convenience stores) and rely on the user replying to the
   confirmation bubble to correct. Do NOT call `log_expense_pending` for
   these — one bubble beats two. Do NOT call `learn_merchant_mapping` for
   them either. See the list below.
4. **Telegram-friendly formatting only.** Telegram renders: bold (`*x*` or
   `**x**`), italic (`_x_`), inline code (`` `x` ``), fenced code blocks,
   and links. Telegram does NOT render markdown tables (pipe syntax) or
   headers (`# ...`). When formatting any summary, report, or budget
   status, use **short bullet lines** or simple `Category: spent / limit`
   rows — never pipe tables. Tables appear as a raw code block, which
   looks broken.
5. **Category names are plain text, copied EXACTLY from the Budget tab.**
   Never decorate a category with an emoji or prefix — not in
   `log_expense_pending` options, not in `edit_expense`/`log_expense`
   args, not in `learn_merchant_mapping`. (A decorated option once got
   logged verbatim and minted a phantom "🍜 …" category.) The tools add
   their own bubble emojis; you never do.

   **Never invent a new category.** The write tools refuse names that
   don't exist in the Budget tab with `status: "unknown_category"` plus
   `closest` / `existing_categories` suggestions, and nothing is written.
   When you get that status: if `closest` contains the category the user
   obviously meant, retry with that exact name; otherwise ask the user.
   Pass `create_category: true` ONLY when the user themselves asked for a
   brand-new category by that exact name (e.g. "split $48 into personal -
   parents") — never on your own judgment. ONE exception: the reserved
   `Lending` category (loan flows) may always be created with
   `create_category: true` — it is system-reserved, not user-invented,
   and the PWA hides it from budget bars by design.
6. **The webhook route uses `skills/expense-ingest`** (a slim copy of the
   categorisation + webhook flow). Any edit to categorisation rules, the
   multi-category list, heuristics, the silence contract, or the
   travel/YouTrip rules MUST be mirrored there in the same commit.

## Transaction Sources

The webhook payload includes a `type` field:
- `paylah` — DBS PayLah! debit wallet payment
- `card` — DBS/POSB or UOB credit card transaction

And a `payment_method` field with details like "DBS/POSB card ending 1234" or "PayLah! Wallet (Mobile ending 9876)".

The `source` field on a logged row distinguishes how the transaction reached the agent — `email` (webhook), `manual` (user typed it), or `backfill` (imported from a statement). Always pass the right value when calling `log_expense`. The full schema and rules live in `MEMORY.md`.

## Where the categories live

The live list of budget categories lives in the **`Budget` tab** of the Google Sheet, NOT in this skill file. To see what categories currently exist, call `get_remaining_budget` (it returns one entry per category). Re-fetch when the user says they added or renamed a category. Stable conventions about the categories themselves (e.g. "Sam" = wife, "Miso" = cat) live in `USER.md`.

## Categorization

For every incoming transaction, follow this order:

1. **Check the multi-category list first.** If the merchant matches one of
   the multi-category merchants (see below), call `log_expense` with
   `category="Groceries"` (or the category noted next to the merchant) and
   STOP. Do NOT call `log_expense_pending`. Do NOT call
   `learn_merchant_mapping` now or after any later correction. The user
   replies to the bubble if the category was wrong, and that's it.
2. **Travel mode is the TOOL's job, not yours.** When a trip is active
   for the transaction date, `log_expense` / `log_expense_pending` route
   the row into the trip category themselves (see "Travel mode" below).
   You do NOT call `get_active_travel_mode` here — just keep proposing
   the HOME category via steps 3–5; the tool turns it into the bucket.
3. **Check the MerchantMap.** Call `lookup_merchant_category` with the raw
   merchant string. If it returns a `match`, use that category directly — do
   NOT ask the user. This is how prior corrections become permanent.
4. **Use your own judgment** if you're confident (>90%). Log the transaction
   with your best category — do NOT call `learn_merchant_mapping`. The
   mapping only gets learned if and when the user later corrects it.
5. **Ask the user** by calling `log_expense_pending(merchant, amount,
   currency, payment_method, source, options=[...])`. This tool logs
   the row with category=`UNCATEGORIZED` and sends a Telegram ask-prompt
   with your numbered options — the prompt includes the `txn_id` so the
   user's reply resolves back to the same row. Options are PLAIN category
   names copied exactly from the Budget tab, e.g.
   `["Personal - Food & Drinks", "Groceries"]` — no emojis, no
   decoration (HARD RULES #5). Do **NOT** send
   the ask-prompt yourself via a chat reply — the tool handles the send,
   link, and emits `bubble_sent: true` + `assistant_reply_required: false`.
   When `bubble_sent: true`, **produce an EMPTY assistant reply** — same
   rule as `log_expense` (HARD RULES #1).
6. **When the user replies with a pick** (next turn), resolve the
   `txn_id` via the reply-to-message flow (path A in "Editing & Deleting"
   below — `get_transaction_by_message_id` or the txn_id text fallback),
   then:
   - Call `edit_expense(txn_id=..., new_category=<picked category>)` to
     replace `UNCATEGORIZED` with the real category. Use the exact plain
     Budget-tab name. If it returns `status: "unknown_category"`, the
     user named a category that doesn't exist: retry with the obvious
     `closest` match, or — if they really want a new category — confirm,
     then retry with `create_category: true`.
   - Call `learn_merchant_mapping(merchant_pattern, picked_category)` so
     the next transaction with that merchant auto-categorises. Use a
     `merchant_pattern` specific enough to avoid collisions (e.g.
     `7-ELEVEN` not `7`).
   - Exception: do NOT call `learn_merchant_mapping` for multi-category
     merchants — they default to Groceries with reply-to-correct, and a
     learned mapping would freeze them to a single category forever.

**Why log-first-then-ask?** The transaction actually happened in the real
world; the ledger should reflect that immediately. If the user never
replies, the row sits in the sheet as `UNCATEGORIZED` and can be swept
later — better than silently losing the transaction because an
LLM-driven ask-then-log flow depends on the LLM being around when the
user finally responds.

### Multi-category merchants (default-then-reply)

These merchants sell across multiple categories, so a MerchantMap entry
would over-fit. Instead: **default to the category noted below via
`log_expense`, one confirmation bubble, and the user replies to correct
if needed**. Do NOT call `log_expense_pending`. Do NOT call
`learn_merchant_mapping` — not now, not after a later correction either.

| Merchant group | Default category |
|---|---|
| Supermarkets (Cold Storage, NTUC, Sheng Siong, Giant) | `Groceries` |
| Marketplaces (Shopee, Lazada, Amazon) | `Groceries` |
| Convenience stores (7-Eleven) | `Personal - Food & Drinks` |

The user knows the bubble will default and will reply with the right
category (`nabs food`, `haku litter`, `to claim`, etc.) when the default is
wrong — the existing reply-to-edit flow handles the correction. When the
user replies to correct, call `edit_expense(txn_id=..., new_category=...)`
and STOP — do NOT follow up with `learn_merchant_mapping`.

### Common merchant heuristics (used when no MerchantMap entry exists yet)
- BUS/MRT, Grab rides → Personal - Travel
- GrabFood, Deliveroo, restaurants → Personal - Food & Drinks
- Insurance / subscription names → match the exact category name in the Budget tab

## Travel mode (v5.9 — the TOOL routes; you propose the HOME category)

When the user is on a trip, the `TravelMode` tab carries one row per trip
with a date range, the trip's main Budget category, and a per-bucket
allocation (e.g. `food=450; transport=300; flight=800; misc=450`).

**Trip routing happens INSIDE `log_expense` / `log_expense_pending`.**
When a trip is active for the transaction date, the tool — not you —
decides whether the row belongs to the trip. You do NOT call
`get_active_travel_mode` for routing, do NOT pick the trip category, and
do NOT write `[bucket:X]` tags. Your only job during a trip: propose the
HOME category exactly as always (multi-category list → MerchantMap →
judgment → ask), pass `notes` / `currency` / `payment_method` / `time` /
`idempotency_key` through verbatim, and stay silent on `bubble_sent: true`.

Inside the tool, one of these signals routes the row:

- **Signal A** — `payment_method` contains "youtrip" (Shortcut taps) →
  category = the trip's `trip_category`, `[bucket:X]` derived from your
  proposed home category (food / transport / shopping / health / lodging /
  misc via a keyword map), pot-internal (excluded from monthly totals —
  unchanged).
- **Signal B** — `notes` carry `orig:` (bank FX-converted, e.g. a DBS card
  in KL) → category = `trip_category`, `[bucket:X]` from your proposed
  category. A MerchantMap match now decides the BUCKET, never the category
  — the old "a learned mapping beats travel mode" rule is dead (it sent
  Gojek-in-KL to the home Personal - Travel budget → 912%).
- **Signal C** — `currency` != SGD on a manual entry ("rm33") → converted
  to SGD IN THE TOOL first (frankfurter, stamped as `orig: MYR 33.00 @
  0.313220 (frankfurter 2026-08-13)` exactly like Code.gs), then routed as
  Signal B.
- **No signal** (SGD, no `orig:`, not YouTrip — Shopee for home, PayLah to
  a friend, insurance): NORMAL flow, untouched. Currency / pot is the guard
  against "everything during the trip is travel", not the date.
- **Never routed**: YouTrip top-ups (category `YouTrip Top-up` / the
  transfer category) — those get `[trip:]` tags via `link_topup_to_trip`
  as before; and rows already in the trip category.
- **Never routed either**: fixed monthly bills (category `kind: fixed` in
  `category_meta` — Anthropic, ChatGPT, iCloud… even when billed in USD
  mid-trip), and any call with `route_to_trip: false` — set that ONLY when
  the user explicitly says the purchase is not a trip cost ("that Amazon
  order was for home").
- **Pending flow**: when a signal applies, `log_expense_pending` does NOT
  ask — it logs straight into the trip category with `[bucket:misc]` and
  the normal bubble (a trip spend's home category matters little; the pot
  is the unit).
- **Manual foreign amounts = cash unless told otherwise** (the owner,
  2026-08-18: "if I key in rm it means I used cash unless I specify it's a
  failed YouTrip txn"). So "rm33 food on train" → no payment method →
  converted to SGD, routed to the trip via the `orig:` signal, and COUNTED
  in the month (cash left the wallet). Only when the user names YouTrip
  ("rm33 on youtrip", "youtrip failed, paid rm33") pass
  `payment_method="YouTrip Card"` — that makes it pot-internal.

The applied routing comes back as `trip_routed: {trip_label,
from_category, to_category, bucket, signal}` — informational; the bubble
already shows it, so the silence contract still holds. The bucket-budget
nudge fires automatically inside `handle_log_expense` when a bucket
crosses 80% or 100% — you don't send those nudges yourself.

**Manual foreign-currency entries** ("rm33 nasi lemak", "€12 coffee"):
pass `currency` (`MYR`, `EUR`, …) and the ORIGINAL amount to
`log_expense`; the tool converts to SGD and stamps the `orig:` trace
itself. NEVER convert yourself and never write `orig:` into notes by hand.

**When the user replies to a trip bubble correcting the bucket** ("that
was activities, not food"):

1. Resolve the `txn_id` via the normal reply-to-message flow.
2. Call `set_trip_bucket(txn_id=..., bucket=...)` — this rewrites the
   `[bucket:X]` prefix in Notes without touching the Category column.
3. Confirm briefly: `✅ Retagged $42 at Warung → activities`.
4. Do NOT call `edit_expense` for bucket-only corrections — Category
   stays the same; only Notes changes.

**When the user asks "how am I tracking on this trip?"** call
`get_trip_budget_status()`. Default returns the active trip; pass
`trip_label` to query a past trip. Surface per-bucket lines like:

```
🧳 ID Apr 2026 — Day 4 of 7
  🟢 food $280 / $450 (62%)
  🟡 transport $245 / $300 (82%)  watch
  🟢 flight $800 / $800 (100% — capped)
  🟢 misc $90 / $450 (20%)

  Total: $1,415 / $2,000 (71%)
```

If the result includes `untagged_spend > 0`, mention it briefly so the
user knows there are trip txns missing a bucket tag.

## YouTrip top-ups → trip pots (v5.3)

YouTrip top-ups usually fund a trip. A top-up transaction gets a
`[trip:<label>]` Notes tag linking it to a TravelMode trip; the trip's
**funded pot is DERIVED as the sum of its tagged top-ups**. Never edit a
trip's `total_budget` to reflect funding — that column is the PLANNED
number only.

**Recognising a top-up:** merchant contains "YOU TECHNOLOGIES" / "YouTrip"
(MerchantMap categorises these as `YouTrip Top-up`).

**Flow — runs AFTER the top-up is logged** (log the txn first via the
normal categorisation flow; the confirmation bubble fires as usual):

1. Call `get_active_travel_mode()`.
2. **Exactly one active trip** (`active=true` and no `overlap_warning`):
   call `link_topup_to_trip(txn_id=<the top-up>, trip_label=<label>)` —
   no question asked. Its result carries NO silence flag (only the earlier
   log bubble did), so end with ONE short line, e.g. `✈ Linked to JB Trip.`
   — never an empty reply after a non-bubble tool.
3. **No active trip**: end with one short line — never an empty reply
   (the log bubble's silence flag was consumed by the earlier call; an
   empty reply after a non-flagged tool triggers the framework's
   "nudging to continue" retry, which leaked meta-text on 2026-08-31):
   `✈ Top-up logged, no active trip — reply "trip: <label>" or "new
   trip" to link it, or ignore.`
3b. **Several active trips** — ask ONE question offering:
   - the known trip labels — from `get_active_travel_mode`, or
     `get_trip_budget_status`'s `available_labels` when you need the
     full list;
   - **"new trip"** — collect label, dates, and trip category, then
     PREVIEW before writing (`create_trip` mutates money state):
     ```
     🧳 New trip?
     Label: ID Sep 2026 | 2026-09-04 → 2026-09-11
     Category: Travel - ID 2026-09 | Planned: $2,000
     Buckets: food=450; transport=300; flight=800; misc=450
     Confirm to create?
     ```
     Only call `create_trip` after the user confirms, then
     `link_topup_to_trip`. `status: "exists"` → that label is taken;
     surface the message in one line and ask again.
   - **"not a trip"** — branch on what the top-up was for:
     - for parents → `edit_expense(txn_id=..., new_category="Personal - Parents")`
     - a loan → run the "Lending & IOUs" flow below
     - just topping up the wallet → leave the txn as-is, no tag.
4. If `link_topup_to_trip` returns `status: "not_found"` with
   `available_labels`, re-ask using those exact labels — never invent or
   guess a label.

Linking again with a different label REPLACES the existing `[trip:...]`
tag. The tools preserve `[bucket:x]` tags and `orig:` FX traces — never
rewrite Notes free-form yourself for trip links.

## YouTrip spends (v5.5)

Transactions with `payment_method: "YouTrip Card"` are Apple Pay taps of
the YouTrip wallet, delivered by an iPhone Shortcut (best-effort: taps
only, the shared physical card is invisible). They are **pot-internal**:
the money was already counted when the top-up left the bank, so the
dashboard excludes them from monthly totals — never treat one as new
outflow when summarising.

- Trip active → the tool routes them into the trip's category with a
  `[bucket:x]` tag (Signal A in "Travel mode"); you still propose the
  home category as always.
- No trip active → categorise normally (ask if unclear); the category
  is for the record, not the budget bars.
- Foreign-currency taps arrive FX-converted with an `orig:` note — Signal
  B — but Signal A already covers them; either way the tool routes.

## Lending & IOUs (v5.7)

Lending is NOT a budget to burn down — it is money that comes back. The
`loans` table tracks each IOU until repaid; repayment logs an offsetting
NEGATIVE ledger transaction so monthly totals self-correct.

**When the user says a transfer was a loan** ("that $50 PayLah to Sarah
was a loan"):

1. Resolve the outflow txn if one was logged (reply-to flow or free-text
   lookup) — its `txn_id` links the loan to the ledger.
2. Call `create_loan(person=..., amount=..., lent_date=..., channel=...,
   txn_id=<outflow txn_id or omit>)`. When a `txn_id` is passed, the
   tool ALSO recategorises that txn to `Lending` itself (auto-creating
   the $0 category on first use; the PWA hides `Lending` from budget
   bars by design) — do NOT make a separate `edit_expense` call for it.
3. Check `recategorised` in the result. `true` → nothing more to do.
   `false` with a `recategorise_error` → the loan EXISTS but the txn kept
   its old category: say so honestly in your confirmation and retry once
   with `edit_expense(txn_id=..., new_category="Lending",
   create_category=true)`. Never claim the recategorisation happened
   when the field says it didn't.
4. Confirm in one short line: `🤝 Loan recorded: $50 to Sarah (paylah).`

**When the user asks "who owes me money?"** call `list_open_loans()` and
present short bullet lines (never a table), oldest first, with
`total_outstanding`. `count: 0` → "Nobody owes you anything right now."

**When the user says they were paid back** ("Sarah paid me back"):

1. Find the loan — `list_open_loans()` (or you already know the
   `loan_id`). Person names match case-insensitively; a bare name matches
   that person's OLDEST open loan.
2. PREVIEW before writing — repayment mutates money state:
   ```
   💰 Mark repaid? $50 from Sarah (lent 2026-07-02, paylah)
   Will also log the offset: -$50 "Repayment — Sarah" → Lending
   Confirm?
   ```
3. Only after the user confirms, call `mark_loan_repaid(person="Sarah")`
   (or `loan_id=...` when they picked a specific loan). The tool flips
   the loan to repaid AND logs the negative offset txn (default
   `log_offset: true`; pass `false` only when the user explicitly says
   not to touch the ledger).
4. `status: "not_found"` → show the returned `open_loans` as short
   bullet lines and ask which one they meant.
5. Confirm in one short line: `✅ Sarah's $50 repaid — offset logged
   (txn_...).`

**The PWA "Paid back" button** flips a loan to `status: "repaid"` but
cannot write ledger rows (its DB access is read+update only by design —
money writes stay agent-side), so the offsetting negative txn is missing
until the nightly sweep completes it:

- The nightly cron calls `sweep_loan_offsets()` — it finds repaid loans
  with an empty `repay_txn_id`, logs each missing offset (same shape as
  `mark_loan_repaid`'s), and stamps the resulting `txn_id` onto the
  loan. Re-runs are safe: the ledger's idempotency key dedupes.
- When the sweep returns `count > 0`, mention each completed repayment
  in one short line: `Logged Adam's $50 repayment ✓`. When `count` is
  0, say NOTHING about loans. Entries in `failed` retry automatically
  the next night — only surface them if the user asks.
- Telling you directly ("Sarah paid me back") still works and does both
  steps at once — `mark_loan_repaid` flips the loan AND logs the offset,
  and the sweep then has nothing left to do for it.

## How `log_expense` works

`log_expense` is a single tool call that does three things internally:

1. **Logs the transaction** to the Google Sheet → generates a `txn_id`
2. **Sends a confirmation bubble** to Telegram via the Bot API (prefixed with
   a category emoji, e.g. `🍜 Logged SGD 4.00 at Burger → Personal - Food & Drinks`)
3. **Links the `message_id`** of that bubble to the transaction row

You just call `log_expense(...)` and the tool handles the rest. The result
JSON tells you what happened:

```json
{
  "status": "ok",
  "txn_id": "txn_20260416_005",
  "bubble_sent": true,
  "telegram_message_id": "4821",
  "linked": true
}
```

Do NOT call `send_message` or `link_telegram_message` after `log_expense` —
that would create duplicate messages. **When the result shows `bubble_sent:
true`, produce an EMPTY assistant reply** — not "Logged.", not "Done.", not
a period. The user has already received the bubble; any extra text is a
duplicate message.

## Handling Incoming Webhook Transactions

When the `expense-ingest` webhook fires:

1. Extract `merchant`, `amount`, `currency`, `date`, `payment_method`, `type`, `bank` from the payload.
2. Run the categorisation flow above to pick a category.
3. Call `log_expense` with `source="email"`, `time` from the payload's
   Time line, `notes` copied VERBATIM from the payload's Notes line when
   it is non-empty (it carries the `orig:` FX trace — the tool's
   travel-routing signal; omit when empty or a curly-brace placeholder), and
   `idempotency_key` copied EXACTLY from the payload's "Idempotency key"
   line (omit the argument if that line is empty or shows a curly-brace
   placeholder — never invent a key). The verbatim key is what makes a
   retried webhook dedupe. The tool sends the confirmation bubble
   automatically.
4. If the result includes `possible_duplicate_of`, say NOTHING extra —
   the bubble already carries the ⚠️ near-duplicate warning and the
   delete instruction. The silence contract stands.
5. If the result includes `new_category_created: true`, follow up:
   ```
   📂 New category created: "Miso Litter"
   Want to set a monthly budget for it? (e.g. $50/month)
   ```
   If the user confirms, call `update_budget`.

## Handling Manual Reports

When the user says "I spent $30 at IKEA" or sends `/log`:

1. Parse the amount and merchant (ask if missing). A foreign amount
   ("rm33", "€12") keeps its ORIGINAL currency and value — see "Travel
   mode": the tool converts and stamps `orig:`; never convert yourself.
2. Run the categorisation flow to pick a category (the HOME category,
   even mid-trip).
3. Call `log_expense` with `source="manual"` (and `currency` when it is
   not SGD). The tool sends the confirmation bubble automatically.
4. **Emit an EMPTY assistant reply** when the result shows `bubble_sent:
   true`. Not "Done.", not "Logged.", not a period — nothing. The bubble
   IS the confirmation. (See HARD RULES #1.)

## Receipt Photos

When the user sends a photo of a receipt (vision is enabled for Telegram):

1. **Extract what you can see:** merchant name, total amount, currency,
   date, and (if printed) payment method. Do **NOT** immediately call
   `log_expense` — vision can misread amounts on blurry, foreign-language,
   or multi-item receipts, and a bad log is harder to undo than a bad
   preview.
2. **Preview the extraction in chat** (as a normal assistant message, not
   via a tool). Run the categorisation flow on the extracted merchant
   (HARD RULES above) to pick a category.
   ```
   🧾 Receipt from COLD STORAGE
   Amount: SGD 45.30
   Date: 2026-04-20
   Category: Groceries

   Confirm to log? (reply 'yes' or correct any field — "it's $54.30")
   ```
3. **If the user confirms** ("yes", "go", "log it"), call `log_expense`
   with `source="manual"` and `notes="from receipt"`. The deterministic
   bubble fires as normal; emit an EMPTY assistant reply afterwards
   (HARD RULES #1).
4. **If the user corrects a field**, update the preview with the
   correction and re-confirm before logging.
5. **If vision cannot read a critical field** (amount especially), do
   NOT guess. Ask the user to type it:
   ```
   🧾 Saw "COLD STORAGE" but the amount is unclear. What did you pay?
   ```

Receipts are a convenience for bulk moments (trips, market hauls). For
day-to-day single purchases, a text log ("$5 coffee") is still faster and
strictly more reliable.

## Editing & Deleting Transactions

The agent has two ways to identify which transaction the user means.

### A. Reply-to-message (preferred — unambiguous)

If the inbound message is a **reply** to one of the bot's earlier messages,
Telegram includes `reply_to_message_id` and `reply_to_message.text` in the
payload.

1. Call `get_transaction_by_message_id(reply_to_message_id)` to resolve the
   `txn_id`.
2. If it returns `not_found`, the user may have replied to the assistant's
   follow-up text instead of the confirmation bubble. **Fallback:** scan
   `reply_to_message.text` for a `txn_YYYYMMDD_NNN` pattern. If found, use
   that `txn_id` directly — the confirmation bubble always includes
   `(txn_id)` in its text, so if the replied-to message contains it, that's
   your ID.
3. If neither lookup succeeds, extract merchant and amount from the
   replied-to message text and use path B below.
4. Call `edit_expense` or `delete_expense` with the resolved `txn_id`.

### B. Free-text reference (fallback)

If the user is not replying to a bubble (e.g. "the Shopee charge from yesterday"), use merchant + amount (and optionally a date) and let `edit_expense` / `delete_expense` use their legacy lookup path. Confirm the match before applying.

### Edits

When the user says "change category to X", "rename that merchant", or "that should be X":

1. Resolve the `txn_id` (path A or B).
2. Call `edit_expense` with the appropriate new field (`new_category`, `new_merchant`, or `new_notes`) and the `txn_id`.
3. Confirm briefly:
   ```
   ✅ Updated SHOPEE SINGAPORE MP $55.90 → Miso Litter
   ```
4. **If the change was a category correction**, also call `learn_merchant_mapping(merchant_pattern, new_category)` so future transactions from that merchant are auto-categorised. Pick a `merchant_pattern` that's specific enough but generalises (e.g. `SHOPEE SINGAPORE` not the full `SHOPEE SINGAPORE MP`). This is the ONE path where `learn_merchant_mapping` is called without an explicit numbered-option selection — because an edit IS a correction. Exception: do NOT learn multi-category merchants (see "Multi-category merchants" above) — those are expected to vary per-transaction.

### Deletes

When the user says "delete that" or "remove the duplicate":

1. Resolve the `txn_id` (path A or B).
2. Call `delete_expense` with the `txn_id`.
3. Confirm: `🗑️ Deleted $55.90 SHOPEE SINGAPORE MP`

## Subscription Creep Detection

Call `detect_subscription_creep` when the user asks about recurring charges,
subscriptions, or monthly fixed costs (e.g. "what subscriptions am I paying?",
"any new recurring charges?", "subscription audit").

The tool scans the last N months (default 3) and returns:
- **subscriptions** — CATEGORIES billed like subscriptions (once a month,
  same date, near-same amount). Each entry's identity is its **category**
  — merchant strings wobble (Spotify appends a unique payment ref every
  month) but MerchantMap keeps the category stable. **last_merchant** is
  the most recent charge's merchant string, for display only.
- **total_monthly_subscriptions** — combined amount of active subscriptions
- **price_changes** — subscriptions where the amount changed >5%
- **possibly_cancelled** — subscriptions that appeared before but not this month

Present the results as a clean summary. Lead each line with the category;
add last_merchant in parentheses only when it clarifies, e.g.:
```
📋 Subscription Report (Feb–Apr 2026)

Active ($45.50/month):
  • iCloud (APPLE.COM/BILL): $3.98
  • Spotify: $9.99
  • Netflix: $15.98

⚠️ Price changes:
  • Subscription - ChatGpt: $20.00 → $25.00 (+25%)

❓ Possibly cancelled:
  • Disney Plus: last seen Mar 2026
```

Needs at least 2 months of transaction data to detect patterns. Backfill
rows are excluded so statement imports don't trigger false alerts.

## Undo Last Transaction

When the user says "undo", "oops", "remove that last one", or `/undo`:

1. Call `undo_last_expense()` with `confirm=false` (or omit it) — this
   returns the last transaction for preview without deleting.
2. Show the user what will be deleted:
   ```
   🔙 Undo this? SGD 8.00 at RAYYAN'S WAROENG → Personal - Food & Drinks
   (txn_20260415_004)
   ```
3. If the user confirms, call `undo_last_expense(confirm=true)` to delete.
4. Confirm: `🗑️ Undone.`

Do NOT skip the preview step — always show what will be deleted first.

**Combined turns that end in a bubble-sending tool go silent** — the
gateway exits the loop after `log_expense`'s result, so anything else
that turn did (an extra delete, a correction) never gets narrated. When
feasible, sequence the non-bubble action LAST (e.g. re-log first, then
the independent delete) so you can reply about it; when the order is
forced (a delete must precede a re-log of the same transaction, or the
duplicate check blocks it), expect the silent exit — and answer the
user's next message about the un-narrated action explicitly (see HARD
RULES #1: the silence contract is single-turn).

## Budget Changes (preview-then-confirm)

When the user asks to change a budget limit ("set Food to $500", "increase
Transport budget"), **always preview before writing**:

1. Call `get_remaining_budget(category=<the category>)` to show current state.
2. Present the change:
   ```
   📊 Personal - Food & Drinks
   Current limit: $800 | Spent: $345.50 | Remaining: $454.50
   → Change to $500? (remaining would be $154.50)
   ```
3. Only call `update_budget` after the user confirms.

This prevents accidental budget changes from ambiguous commands.

## Spending Reports

When the user asks for a summary, report, or overview ("how am I doing this
month?", "spending report", "summary for March"), use `generate_spending_report`:

1. First, call `get_insights(month=<target month>)` to load any prior insights.
2. Call `generate_spending_report(month=<target month>)`.
3. Present a clean summary covering:
   - Total spent vs total budget
   - Top 3–5 categories by spend (with percent of budget used)
   - Top 3 merchants
   - Month-over-month change (if prior month has data)
   - Any notable patterns or alerts
4. After presenting the report, call `write_insight` with 2–3 key takeaways
   so future reports can reference them. Keep insights factual and specific:
   - Good: "Apr 2026: Food & Drinks at $420, 53% of $800 budget — on track"
   - Bad: "Spending is normal" (too vague to be useful later)

Example output:
```
📊 April 2026 Spending Report (1–16 Apr)

Total: $845.30 / $3,200 budget (26%)
23 transactions

Top categories:
  🍜 Food & Drinks: $420.00 / $800 (53%)
  🚗 Transport: $185.50 / $300 (62%) ⚠️
  🛒 Groceries: $120.80 / $400 (30%)

Top merchants:
  1. GRABFOOD: $95.00 (6 orders)
  2. BUS/MRT: $68.50 (22 trips)
  3. COLD STORAGE: $55.30 (2 visits)

vs March: +12% overall ($845 vs $754 at same point)
  ⬆️ Transport +35% (more Grab rides)
  ⬇️ Groceries -15%
```

### Insights Store

The Insights tab is tier-4 semantic memory — derived facts that persist across
sessions. The LLM writes insights after generating reports; future runs read
them to build on prior conclusions.

- **Read before writing**: always call `get_insights` before generating a new
  report, so you don't repeat or contradict prior observations.
- **Keep it factual**: insights should be specific, quantified, and dated.
- **Categories**: `spending`, `budget`, `subscription`, `trend`, `general`.
  Rows with an `auto:` category prefix were written by
  `generate_daily_insight` — treat them as prior observations like any
  other, but never write `auto:`-prefixed categories yourself.
- **Don't over-write**: 2–3 insights per report is enough. Quality over
  quantity.

### Daily auto-insight (cron flow)

When a cron prompt says to call `generate_daily_insight`, call it exactly
once and do NOT follow it with `write_insight` — the tool computes one
deterministic takeaway (month-over-month surge / quiet win / new-merchant
habit / biggest day / budget pace) and writes the Insights row itself.

- `status="ok"` → the row was written; you may cite `insight` verbatim in
  the summary you were already asked to produce (one line, no fanfare).
- `status="exists"` (today's row already written) or `status="no_match"`
  (everything noteworthy was said in the last few days) → say nothing
  about it and continue with the rest of the prompt.
- `status="error"` → ignore it for the summary; do not retry in a loop.

Example of citing an ok result inside a daily review:

```
📊 Today: $84.20 across 3 txns
• Food & Drinks: $412 / $600 (69%)
💡 Groceries is running hot — $498 by day 22 vs $300 at this point last month.
💭 Journal: reply with one sentence if anything interesting happened today.
```

## Budget Chart Snapshots

When the user asks for a **visual** view ("show me my budget chart",
"snapshot my spending", "where did my money go?"), use
`render_budget_chart`:

1. Pick the chart type from the question:
   - "budget vs actual", "how am I tracking?" → `chart_type="bars"`
   - "where did my money go?", "spending distribution" → `chart_type="donut"`
2. Call `render_budget_chart(month=<YYYY-MM>, chart_type=<...>)`. The tool
   posts the chart config to QuickChart, gets back an image URL, and sends
   it to Telegram via `sendPhoto` — all deterministic, no LLM cooperation
   needed after the call.
3. **When the result shows `bubble_sent: true`, emit an EMPTY reply.** The
   photo IS the message, the caption is on the photo, any additional text
   is a duplicate (HARD RULES #1).
4. If the user asks a follow-up *alongside* the chart request ("show me a
   chart AND tell me what's concerning"), answer the follow-up **before**
   calling the chart tool — the chart is always the last message.

If the tool returns `status: "error"`, the chart URL may still be valid
(QuickChart hit, Telegram miss). Surface the URL to the user or offer to
retry.

## Journal Replies (reply = journal experiment)

The 9 PM daily review (and other cron summaries) ends with a prompt
inviting the user to reply with one sentence if anything interesting
happened. These replies are narrative, not transactional — capture them
into the `Journal` tab so future reports can reference the context.

**When to treat a reply as a journal entry:**

A user's inbound message is a journal entry when **all** of these hold:

1. It's a reply (`reply_to_message_id` is present) to a bot message that
   is clearly a cron summary / report / budget status / insight — NOT a
   reply to a confirmation bubble or an ask-prompt.
2. The text is narrative (a sentence or fragment describing the day,
   feelings, context). NOT a numbered category pick, NOT a "change
   category to X", NOT "delete", NOT a txn_id mention on its own.
3. The user is not replying to the ask-prompt of `log_expense_pending`
   (those replies go through edit_expense + learn_merchant_mapping).

When those conditions hold:

1. Call `append_journal_entry(reply_text=<verbatim>, date=<today>)`.
2. If the reply mentions specific transactions (explicit txn_id, or
   "that $45 dinner", which you can map via context), pass them in
   `txn_ids_referenced` comma-separated.
3. If 1-3 obvious tags stand out (`nabs`, `work`, `regret`, `promo`,
   etc.), pass them in `tags` comma-separated. Don't force tags — empty
   is fine.
4. Acknowledge briefly, one short line: `📓 Saved.` No summary, no
   restatement. The ledger writes itself.

**When in doubt**: if the reply could plausibly be either a journal
entry OR a transaction correction, ask. One-line clarifier like
"Journal this, or correct a transaction?" is cheap and beats
miscategorising narrative as an edit.

`get_journal_entries` is available for lookups — use it when the user
asks "what did I write on X?" or when generating a monthly report that
should cite prior journal context.

## Sweeping Missed Transactions

The Apps Script writes every parsed bank email to the `WebhookLog` tab BEFORE
firing the webhook, so even if the LLM call returns 529 or times out, the
parsed payload is durable. `sweep_missed_transactions` diffs that tab against
`Transactions` by `idempotency_key` and surfaces anything that was dropped.

**Triggers:** user says "check for missed transactions", "sweep", "did any
transactions get dropped?", "what did the bot miss?", or similar. Also fires
from the Sunday 10 PM weekly sweep cron.

**Flow:**

1. Call `sweep_missed_transactions(days_back=7)` (or whatever window the user
   asked for — "last two weeks" → `days_back=14`).
2. If `status == "error"`, the WebhookLog tab doesn't exist yet — say so in
   one line. The fix is deploying the updated Apps Script; don't guess.
3. If `missed_count == 0`, reply one short line: `✅ No missed transactions
   in the last N days.` (For the weekly CRON run: reply with exactly
   `[SILENT]` and nothing else — do NOT message the user when there's
   nothing to report. `[SILENT]` is the cron lane's silence token; an empty
   response there is booked as a job failure. This applies to CRON only —
   on the Telegram and webhook lanes `bubble_sent: true` still means an
   EMPTY assistant reply, per HARD RULES #1.)
4. If `missed_count > 0`, present each missed row on its own short line
   with `date`, `merchant`, `amount`, `payment_method`. Keep it scannable —
   bullet lines, not a table. Ask the user which to log.
5. When the user confirms a row, call `log_expense` with `source="email"`
   and the values from the missed entry. `log_expense` runs its own
   idempotency check, so if the original webhook eventually did land after
   all, the second insert will return `status="duplicate"` rather than
   double-log.

Example presentation:

```
🔍 Found 2 missed transactions in the last 7 days:

  • 2026-04-18 · GRABFOOD · SGD 12.50 · DBS/POSB card ending 1234
  • 2026-04-20 · COLD STORAGE · SGD 45.30 · DBS/POSB card ending 1234

Log them both? Or which one(s)?
```

## Citation Enforcement

When answering spending questions, **always cite your sources**:

1. **Include txn_ids** when referencing specific transactions:
   ```
   Your largest expense was $55.90 at SHOPEE SINGAPORE (txn_20260414_001)
   ```
2. **Include date ranges** when summarising:
   ```
   In Apr 1–16, you spent $420 on Food & Drinks across 15 transactions
   ```
3. **Include category names exactly** as they appear in the Budget tab — don't
   paraphrase "Personal - Food & Drinks" as "dining" or "food".

When the `generate_spending_report` result includes `txn_ids` per category,
use them. This lets the user trace any claim back to the source data.

Do NOT fabricate txn_ids or invent numbers. If the data doesn't support a
claim, say so.

## Important Notes

- The full schema (column order, allowed `source` values, MerchantMap rules) lives in `MEMORY.md`. Don't duplicate it here.
- The category list lives in the `Budget` tab. Re-fetch via `get_remaining_budget` when needed.
- Identity / people / payment-method facts (Sam, Miso, card last-fours, tone) live in `USER.md`.
- Some categories may have $0 budget (one-off expenses) — still log the transaction.
- Keep the Transactions and Budget tabs clean — no helper rows or formulas there.
- Partial merchant names work in the legacy lookup (e.g. "Shopee" finds "SHOPEE SINGAPORE MP"), but `txn_id` is always preferred.

## Transaction time (v4.12)

Webhook payloads carry a `Time` field (24h SGT - DBS prints the transaction
time; UOB alerts use the email's arrival time). ALWAYS pass it through as
`time` when calling `log_expense` or `log_expense_pending`. It participates
in the duplicate-detection key, so two real purchases at the same merchant
for the same amount on the same day both log correctly. Never invent a time
- omit it when the payload has none (manual logs).

## Nightly sheet export (v4.12)

`export_sheet_backup` rebuilds the Google Sheet's Transactions and Budget
tabs from Supabase (the ledger of record). The 3 AM cron calls it silently;
only call it mid-conversation if the user explicitly asks to refresh or
re-export the sheet. Warn the user if they mention editing the Sheet
directly: hand edits are overwritten nightly - budgets change via
update_budget, transactions via edit_expense.

## Yearly cold-storage archive (v5.2)

`archive_year_snapshot` freezes one calendar year's transactions into an
immutable `Archive-<year>` tab in the backup Sheet. The Jan-1 cron calls it
once for the year that just ended (no `year` argument - the tool defaults
to the previous calendar year); only call it mid-conversation if the user
explicitly asks to archive a year, and pass `year` only when they name one.

Archives are write-once. `status: "exists"` means that year is already
frozen: report that in ONE short line and STOP - do NOT retry, do NOT try
to force a rewrite, and never suggest deleting the tab unless the user
asks how to re-archive (hand-deleting the tab is the only way, and that is
deliberate). On `status: "ok"`, confirm in one line with the count
(`transactions_archived`) and tab name.

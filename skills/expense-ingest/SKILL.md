---
name: expense-ingest
description: Slim webhook-only subset of expense-tracker — categorises one Gmail bank/YouTrip transaction and logs it to the Supabase ledger
version: 1.0.1
author: alhazjm
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [finance, expense, tracking, automation, webhook]
---

# Expense Ingest (webhook subset of expense-tracker)

This skill is a faithful SUBSET of `expense-tracker`, loaded ONLY by the
`expense-ingest` webhook route (the whole skill body rides in every API
call of every ingest, so it carries just what one webhook turn needs).
The rules below are copied from expense-tracker; when they change there,
they change here in the same commit.

## When to Use
- A bank transaction webhook arrives from Gmail Apps Script (DBS / UOB /
  HSBC card or PayLah alert, or a self-sent YouTrip Shortcut alert)

Nothing else. Manual `/log`, undo/edit/delete, receipts, reports, charts,
journal, sweeps, subscriptions, budgets and lending live in
`expense-tracker` and are NOT part of a webhook turn.

## HARD RULES

1. **`log_expense` sends the confirmation bubble automatically.** The tool
   sends a Telegram message and links the `message_id` to the row — you do
   NOT need to send anything yourself or call `link_telegram_message`.

   **When `log_expense` (or `log_expense_pending`) returns `bubble_sent:
   true`, you MUST produce an EMPTY assistant reply.** Do not write any
   text — not "Logged.", not "Done.", not a period, not an emoji. The user
   has already seen the confirmation bubble; any additional text is a
   duplicate message and a bug. This overrides any default tendency to
   acknowledge tool completion. The result also carries
   `assistant_reply_required: false` — honour it.

   **The silence contract is single-turn.** It ends with the turn that
   made the tool call. A webhook turn is exactly one such turn.

   **Empty is ONLY for the silence flag.** End the turn with an empty
   reply ONLY when the LAST tool result of the turn carries
   `assistant_reply_required: false`. After any other tool
   (`get_active_travel_mode`, `link_topup_to_trip`, a lookup), an empty
   reply triggers the framework's "nudging to continue" retry and the
   retried model tends to leak meta-text into the chat (live incident
   2026-08-31: "All good — the webhook log completed successfully…" after
   a top-up). Either stay empty on the flag, or say the ONE short line
   the flow prescribes — never nothing after a non-flagged tool.
2. **`learn_merchant_mapping` only fires AFTER a user correction.** Never
   call it on a first-ever sighting of a merchant, even if you're confident
   about the category. The MerchantMap is a record of human-confirmed
   decisions, not a cache of your guesses. A webhook turn contains no user
   correction, so a webhook turn NEVER calls `learn_merchant_mapping`.
3. **Multi-category merchants default to Groceries** (supermarkets,
   marketplaces, convenience stores) and rely on the user replying to the
   confirmation bubble to correct. Do NOT call `log_expense_pending` for
   these — one bubble beats two. Do NOT call `learn_merchant_mapping` for
   them either. See the list below.
4. **Telegram-friendly formatting only.** Telegram renders bold, italic,
   inline code, fenced code blocks and links. It does NOT render markdown
   tables (pipe syntax) or headers (`# ...`). Use **short bullet lines** —
   never pipe tables. (A webhook turn normally sends no text at all.)
5. **Category names are plain text, copied EXACTLY from the Budget tab.**
   Never decorate a category with an emoji or prefix — not in
   `log_expense_pending` options, not in `log_expense` args. (A decorated
   option once got logged verbatim and minted a phantom "🍜 …" category.)
   The tools add their own bubble emojis; you never do.

   **Never invent a new category.** The write tools refuse names that
   don't exist in the Budget tab with `status: "unknown_category"` plus
   `closest` / `existing_categories` suggestions, and nothing is written.
   When you get that status: if `closest` contains the category obviously
   meant, retry with that exact name; otherwise fall back to
   `log_expense_pending`. Pass `create_category: true` ONLY when the user
   themselves asked for a brand-new category by that exact name — never on
   your own judgment, and never in a webhook turn.

## Where the categories live

The live list of budget categories lives in the **`Budget` tab**, NOT in
this skill file. To see what categories currently exist, call
`get_remaining_budget` (one entry per category).

## Categorization

For every incoming transaction, follow this order:

1. **Check the multi-category list first.** If the merchant matches one of
   the multi-category merchants (see below), call `log_expense` with
   `category="Groceries"` (or the category noted next to the merchant) and
   STOP. Do NOT call `log_expense_pending`. Do NOT call
   `learn_merchant_mapping`.
2. **Check the MerchantMap.** Call `lookup_merchant_category` with the raw
   merchant string. If it returns a `match`, use that category directly — do
   NOT ask the user. This is how prior corrections become permanent.
3. **Use your own judgment** if you're confident (>90%). Log the transaction
   with your best category — do NOT call `learn_merchant_mapping`.
4. **Ask the user** by calling `log_expense_pending(merchant, amount,
   currency, payment_method, source, options=[...])`. This tool logs
   the row with category=`UNCATEGORIZED` and sends a Telegram ask-prompt
   with your numbered options — the prompt includes the `txn_id` so the
   user's reply resolves back to the same row. Options are PLAIN category
   names copied exactly from the Budget tab, e.g.
   `["Personal - Food & Drinks", "Groceries"]` — no emojis, no
   decoration (HARD RULES #5). Do **NOT** send the ask-prompt yourself via
   a chat reply — the tool handles the send, link, and emits
   `bubble_sent: true` + `assistant_reply_required: false`. When
   `bubble_sent: true`, **produce an EMPTY assistant reply** — same rule as
   `log_expense` (HARD RULES #1). Pass `time`, `notes` and
   `idempotency_key` through exactly as you would for `log_expense`.

The user's later reply to a bubble is handled by `expense-tracker` in a
later turn — not by you now.

**Why log-first-then-ask?** The transaction actually happened in the real
world; the ledger should reflect that immediately. If the user never
replies, the row sits as `UNCATEGORIZED` and can be swept later.

### Multi-category merchants (default-then-reply)

These merchants sell across multiple categories, so a MerchantMap entry
would over-fit. Instead: **default to the category noted below via
`log_expense`, one confirmation bubble, and the user replies to correct
if needed**. Do NOT call `log_expense_pending`. Do NOT call
`learn_merchant_mapping`.

- Supermarkets (Cold Storage, NTUC, Sheng Siong, Giant) → `Groceries`
- Marketplaces (Shopee, Lazada, Amazon) → `Groceries`
- Convenience stores (7-Eleven) → `Personal - Food & Drinks`
   (Edit this list and the category names to your own supermarkets and categories.)

### Common merchant heuristics (used when no MerchantMap entry exists yet)
- BUS/MRT, Grab rides → Personal - Travel
- GrabFood, Deliveroo, restaurants → Personal - Food & Drinks
- Insurance / subscription names → match the exact category name in the Budget tab

## Handling Incoming Webhook Transactions

When the `expense-ingest` webhook fires:

1. Extract `merchant`, `amount`, `currency`, `date`, `payment_method`,
   `type`, `bank` from the payload. `type` is `paylah` (DBS PayLah! wallet)
   or `card`; `payment_method` reads like "DBS/POSB card ending 1234",
   "PayLah! Wallet (Mobile ending 9876)" or "YouTrip Card".
2. Run the categorisation flow above to pick a category. During a trip
   this is still your job — propose the HOME category exactly as always
   (see "Travel mode" — the tool turns it into the bucket).
3. Call `log_expense` with `source="email"`, `currency` from the payload,
   `payment_method` from the payload, `time` from the payload's Time line
   (it participates in the duplicate-detection key; omit when the line is
   empty — never invent a time), `notes` copied VERBATIM from the payload's
   Notes line when it is non-empty (it carries the `orig:` FX trace — the
   travel-routing signal; omit when empty or a curly-brace placeholder),
   and `idempotency_key` copied EXACTLY from the payload's "Idempotency
   key" line (omit the argument if that line is empty or shows a
   curly-brace placeholder — never invent a key). The verbatim key is what
   makes a retried webhook dedupe. The tool sends the confirmation bubble
   automatically. `status: "duplicate"` → the row already existed and NO
   bubble was sent, so the silence contract does NOT apply: reply with ONE
   short line, e.g. `Already logged as txn_20260802_003.` (an empty reply
   here would trip the framework's empty-response retries).
4. If the result includes `possible_duplicate_of`, say NOTHING extra —
   the bubble already carries the ⚠️ near-duplicate warning and the
   delete instruction. The silence contract stands.
5. If the result includes `new_category_created: true`, follow up:
   ```
   📂 New category created: "Miso Litter"
   Want to set a monthly budget for it? (e.g. $50/month)
   ```
   (This can only happen when a category was created at the user's
   request — never from a webhook categorisation.)

## Travel mode (the TOOL routes; you propose the HOME category)

When a trip is active for the transaction date, `log_expense` /
`log_expense_pending` decide trip routing THEMSELVES. You do NOT call
`get_active_travel_mode`, do NOT pick the trip category, and do NOT write
`[bucket:X]` tags. Your only job during a trip: propose the HOME category
exactly as always (multi-category list → MerchantMap → judgment → ask),
pass `notes` / `currency` / `payment_method` / `time` / `idempotency_key`
through verbatim, and stay silent on `bubble_sent: true`.

Inside the tool, one of these signals routes the row:

- **Signal A** — `payment_method` contains "youtrip" (Shortcut taps) →
  category = the trip's `trip_category`, `[bucket:X]` derived from your
  proposed home category (food / transport / shopping / health / lodging /
  misc via a keyword map), pot-internal (excluded from monthly totals).
- **Signal B** — `notes` carry `orig:` (bank FX-converted, e.g. a DBS card
  in KL) → category = `trip_category`, `[bucket:X]` from your proposed
  category. A MerchantMap match now decides the BUCKET, never the category
  — a learned mapping no longer beats travel mode.
- **Signal C** — `currency` != SGD on a manual entry → converted to SGD in
  the tool first (stamped `orig: MYR 33.00 @ 0.313220 (frankfurter
  2026-08-13)`), then routed as Signal B. Webhook payloads arrive already
  converted, so this is the manual-path twin of Signal B.
- **No signal** (SGD, no `orig:`, not YouTrip — Shopee for home, PayLah to
  a friend, insurance): NORMAL flow, untouched. Currency / pot is the guard
  against "everything during the trip is travel", not the date.
- **Never routed**: YouTrip top-ups (category `YouTrip Top-up` / the transfer category) — those
  get `[trip:]` tags via `link_topup_to_trip` (below); and rows already in
  the trip category.
- Manual "rm"-style entries never reach this route (they are Telegram
  turns handled by expense-tracker); the shared rule there: cash unless
  the user names YouTrip.
- **Never routed either**: fixed monthly bills (category `kind: fixed` in
  `category_meta` — Anthropic, ChatGPT, iCloud… even when billed in USD
  mid-trip), and any call with `route_to_trip: false` — set that ONLY when
  the user explicitly says the purchase is not a trip cost.
- **Pending flow**: when a signal applies, `log_expense_pending` does NOT
  ask — it logs straight into the trip category with `[bucket:misc]` and
  the normal bubble (the pot is the unit; the home category matters little).

The applied routing comes back as `trip_routed: {trip_label, from_category,
to_category, bucket, signal}` — informational; the bubble already shows it,
so the silence contract still holds. Bucket-budget nudges (80% / 100%) fire
automatically inside the tool — you never send them.

## YouTrip top-ups → trip pots

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
3. **No active trip**: you MUST still end with one short line — never
   an empty reply (the log bubble's flag was consumed by the earlier
   call; see HARD RULES #1). Use exactly this shape:
   `✈ Top-up logged, no active trip — reply "trip: <label>" or "new
   trip" to link it, or ignore.`
4. **Several active trips**: ask ONE question offering the known trip
   labels (from `get_active_travel_mode`, or `get_trip_budget_status`'s
   `available_labels`), "new trip", or "not a trip". The user's answer is
   handled by `expense-tracker` next turn.
5. If `link_topup_to_trip` returns `status: "not_found"` with
   `available_labels`, re-ask using those exact labels — never invent or
   guess a label.

Linking again with a different label REPLACES the existing `[trip:...]`
tag. The tools preserve `[bucket:x]` tags and `orig:` FX traces — never
rewrite Notes free-form yourself for trip links.

## YouTrip spends

Transactions with `payment_method: "YouTrip Card"` are Apple Pay taps of
the YouTrip wallet, delivered by an iPhone Shortcut (best-effort: taps
only, the shared physical card is invisible). They are **pot-internal**:
the money was already counted when the top-up left the bank, so the
dashboard excludes them from monthly totals.

- Trip active → the tool routes them into the trip's category with a
  `[bucket:x]` tag (Signal A above); you still propose the home category.
- No trip active → categorise normally (ask if unclear); the category
  is for the record, not the budget bars.

## Important Notes

- The full schema (column order, allowed `source` values, MerchantMap
  rules) lives in `MEMORY.md`. Don't duplicate it here.
- `source` is always `email` on a webhook turn.
- Some categories may have $0 budget (one-off expenses) — still log.
- Anything beyond this file — corrections, undo, reports, loans, sweeps —
  is `expense-tracker`'s job in a later user turn.

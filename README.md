# Personal Expense Hermes (PEH)

Telegram bot + Gmail webhook → tool-calling agent → Google Sheets ledger. A personal-finance LLM agent built around the Singapore context, deployed to a $7/mo Render box.

Note: This is the cleaned, public core of the agent I run in production daily. Production build: 27 tools · 250+ tests; this repo carries the core loop with its own test suite.

---

## What it does

I forward bank-alert emails (DBS/UOB) and message my Telegram bot. The agent categorises each transaction with an LLM, learns merchant-to-category mappings over time, writes everything to a Google Sheet (the source of truth), and answers natural-language questions about my budget.

| Message in Telegram | What happens |
|---|---|
| *"How much have I spent on food?"* | Queries the sheet, returns total |
| *"Remind me daily to stop ordering Grab"* | Creates a cron job |
| *"Can I afford a $150 jacket?"* | Checks discretionary budget, gives an assessment |
| *"Reallocate $50 from Dining to Transport"* | Updates budget limits in the sheet |

---

## Why I built it

To see how an agentic personal-finance system would actually work in a Singapore context — bank-email shapes (DBS/UOB), SGD as the base currency, the local card-rewards landscape — and then extend outward into the use cases that get more interesting once the ledger is reliable:

- **Tracking overseas spend.** Non-SGD transactions during travel, bucketed against a per-trip allocation rather than the regular monthly budget.
- **Optimising miles earned on credit-card spend.** Surfacing which card to use at which merchant given monthly caps, promo windows, and category multipliers.

Both are domain-specific in ways no off-the-shelf finance app handles, but cleanly expressible as agent skills + tools.

---

## Why not just an LLM call on a cron?

The "ChatGPT-summarises-my-CSV-once-a-week" version is the obvious lighter take. It's not what I want, for a few reasons:

- **Bank emails are event-driven.** Webhook in seconds, not "we'll see at the next cron tick."
- **No persistent identity.** A cron call starts from zero every run — no learned merchant→category mappings, no "you've already nudged me about Grab this week."
- **No writes, no tools.** Pure summarisation can't log a manual expense, edit yesterday's row, or update the budget.
- **No interactive surface.** You can't ask *"can I afford a $150 jacket?"* against a one-shot summary.
- **Prompt changes are unversioned.** A cron prompt drifts silently; a versioned skill markdown changes deliberately.

The agent shape exists because the surface needs writes, memory, event-driven triggers, and on-demand interaction. Cron is one of *several* invocation paths, not the whole product.

---

## Architecture

```
                    Inputs
          ┌───────────┴────────────┐
          │                        │
    Gmail (HMAC                 Telegram
     webhook)                    bot
          │                        │
          └───────────┬────────────┘
                      ▼
       ┌────────────────────────────────┐
       │       System prompt            │
       │ ┌────────────────────────────┐ │
       │ │ SOUL.md   — agent persona  │ │  ← every turn
       │ │ USER.md   — identity, prefs│ │
       │ │ MEMORY.md — system facts   │ │
       │ └────────────────────────────┘ │
       │              +                 │
       │ ┌────────────────────────────┐ │
       │ │  Active skill markdown     │ │  ← one of four,
       │ │  (versioned, ~100–500 LOC) │ │     selected by
       │ └────────────────────────────┘ │     route / cron
       │              +                 │
       │ ┌────────────────────────────┐ │
       │ │  24 registered tools       │ │
       │ │  (ledger, budget, cards,   │ │
       │ │   reports, recall)         │ │
       │ └────────────────────────────┘ │
       └────────────────┬───────────────┘
                        ▼
                  MiniMax-M2.7
                        │
            ┌───────────┼───────────┐
            ▼           ▼           ▼
      Google Sheets  Telegram   Cron jobs
      (ledger,       (replies,  (daily /
       budget,        confirms)  Friday /
       merchants,                monthly)
       cards)
```

### The three tier-2 files

Loaded into the system prompt every turn. Small on purpose.

| File | Contents | Why it's separate |
|---|---|---|
| [`SOUL.md`](hermes-config/SOUL.md.example) (~50 lines) | Voice, principles, failure modes | Anchors the persona so the agent feels like *one thing* across skills |
| [`USER.md`](hermes-config/USER.md.example) | People, payment methods, communication rules | Things the model needs to know about *me* that don't change week to week |
| [`MEMORY.md`](hermes-config/MEMORY.md.example) | Sheet schema, webhook payload shape, enums, edge-case quirks | Machine-ish reference — answers "how does this system work" questions |

Each file carries an explicit *"what does NOT belong here"* section. Transaction data, derived insights, budget numbers, category lists all live in the sheet — never in the prompt. That's how tier-2 stays small as the system grows.

### Skills, tools, storage

- **Skills** are versioned markdown files in [`skills/`](skills/) — `expense-tracker v4.11.0`, `card-optimiser v1.0.0`, `budget-manager v3.0.0`, `weekly-summary v3.0.0`. The active skill defines the playbook; tier-2 stays constant across them.
- **Tools** are Python functions registered with `@register_tool` in [`tools/`](tools/). The tool list is hard-coded into hermes-agent's `toolsets.py` via a `sed` injection in the [`Dockerfile`](Dockerfile), with a `grep` verification step that fails the build loud if the injection silently no-ops.
- **Storage** is Google Sheets via `gspread`. The append-only `Transactions` tab is the source of truth. No Postgres until a second user appears.

---

## Skills

Four skills, each versioned in its frontmatter. All four share the same 24-tool surface; the skill markdown is the playbook variant.

### `expense-tracker` (v4.11.0) — the conversational core

Owns bank-email parses, manual `/log` messages, and edits via reply-to-message.

| Trigger | Behaviour |
|---|---|
| Webhook delivers a parsed bank email | Looks up merchant in `MerchantMap`, categorises (LLM falls back if unknown), logs to ledger, replies with confirmation |
| User: *"$8 lunch at Toast Box"* | Same flow, manually triggered |
| User replies to a confirmation: *"that's transport not food"* | Resolves the original `txn_id` via `telegram_message_id`, edits the sheet row, learns the new merchant→category mapping |
| User: *"undo"* | Calls `undo_last_expense` |

### `card-optimiser` (v1.0.0) — miles & rewards

Cap tracking, card recommendations, promo overrides, post-cap nudges. Gated behind sheet setup — all five card tools return `setup_required` until `Cards` / `CardStrategy` / `CardNudgeLog` tabs exist.

| Trigger | Behaviour |
|---|---|
| User: *"which card should I use at Cold Storage?"* | `recommend_card_for("Cold Storage")` — returns the highest-multiplier card whose monthly cap isn't yet hit |
| Cron (Friday + 1st of month) | `review_card_efficiency` surfaces caps approaching ceiling, dormant cards, missed promos |
| Post-transaction (after a cap is hit) | Side-channels a one-time nudge: *"Cap hit on Card X — switch to Card Y for the rest of the month"* |

### `budget-manager` (v3.0.0) — burn-down warnings

Cron-invoked. Shares the expense-tracker tool surface; only the prompt changes.

| Trigger | Behaviour |
|---|---|
| Daily 21:00 cron | Compares today's spend per category against monthly limits, nudges if pacing >100% |
| Weekly Sunday cron | Per-category weekly burn-down |
| Monthly 1st cron | Final tally + reset |

### `weekly-summary` (v3.0.0) — reports

| Trigger | Behaviour |
|---|---|
| Friday 18:00 cron | Top categories, biggest single expense, anomalies (creep detection skips `backfill` rows), *"did you mean to..."* prompts |
| Monthly 1st cron | Full month-end report — categories, cards, budgets-vs-actuals |

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent framework | [hermes-agent](https://github.com/alhazjm/hermes-agent) (custom, pinned by SHA) |
| LLM | MiniMax-M2.7 (provider-agnostic — swapping to Claude or GPT is config, not refactor) |
| Storage | Google Sheets via `gspread` |
| Ingress | Telegram bot + Google Apps Script (HMAC webhook) |
| Egress | Telegram + scheduled cron jobs |
| Deploy | Docker on Render, Singapore region |
| Tests | pytest, ~200 tests with a stubbed registry for fast unit runs |

---

## Setting up the Google Sheet

The agent treats one Google Sheet as its database. Full schema and migration steps live in [sheets-template/README.md](sheets-template/README.md); the minimal path is:

1. **Create a new Google Sheet** with a `Transactions` tab. Header row exactly:
   ```
   Date | Merchant | Amount | Currency | Category | Source | Payment Method | Notes | txn_id | telegram_message_id | idempotency_key
   ```
   `MerchantMap`, `Insights`, `Journal`, `WebhookLog`, and `CardNudgeLog` tabs are auto-created on first write — you don't need to make them yourself.

2. **Add a `Budget` tab.** Two layouts work: per-month (`Category | Jan | Feb | … | Dec`) or simple (`Category | Monthly Limit`). [`sheets-template/budget_categories.json`](sheets-template/budget_categories.json) is a generic seed of 9 starter categories. The agent auto-creates new `Budget` rows at $0 limit when a transaction lands in an unseen category, so the seed is just a starting shape — not a hard schema.

3. **Share the sheet with a service account.** Create a Google Cloud service account, download the JSON key, share the sheet with the service account email as **Editor**, and set two env vars before running the agent:
   - `GSPREAD_SPREADSHEET_ID` — from the sheet URL
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — path to (or inline contents of) the key file

4. **Optional: enable card-optimiser.** Populate the `Cards` and `CardStrategy` tabs (schema in [sheets-template/README.md](sheets-template/README.md)) with at least one card row and a `_default` row in `CardStrategy`. Until both tabs exist with data, every card-optimiser tool returns `{"status": "setup_required"}` and the rest of the agent works untouched.

The header strings must match exactly — [`tools/sheets_client.py`](tools/sheets_client.py) does header-name column lookups (`_get_column_index`) instead of fixed indices, which is what lets older 8-column sheets keep working alongside the v4 11-column layout.

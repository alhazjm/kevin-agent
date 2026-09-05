# Kevin — a personal-finance agent

[![CI](https://github.com/alhazjm/kevin-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/alhazjm/kevin-agent/actions/workflows/ci.yml)
[![Upstream anchor check](https://github.com/alhazjm/kevin-agent/actions/workflows/anchor-check.yml/badge.svg)](https://github.com/alhazjm/kevin-agent/actions/workflows/anchor-check.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Bank emails → Gmail webhook → tool-calling LLM agent → Postgres ledger → Telegram replies + a receipt-styled PWA dashboard. A personal-finance agent built for the Singapore context (DBS/UOB/HSBC email shapes, SGD, the local card-rewards game), deployed on a $7/mo Render box.

This is production code, scrubbed of personal data: **37 tools · 5 versioned skills · 648 tests · 8 migrations · 2 build-time patches** on [hermes-agent](https://github.com/NousResearch/hermes-agent) 0.21.0. Sample identifiers (card numbers, merchants, emails) are fictional; the personal memory files ship as `.example` templates. Clone it, configure it to your banks, and it's yours — [**docs/SETUP.md**](docs/SETUP.md) is the runbook, and [**docs/UPGRADING-HERMES.md**](docs/UPGRADING-HERMES.md) is how you keep the framework current without anyone's help.

---

## What it does

Bank-alert emails are parsed within ~5 minutes of a purchase and land in the ledger already categorised, with a Telegram confirmation you can reply to ("that's transport, not food") to correct and teach it. Everything else is a conversation:

| Message in Telegram | What happens |
|---|---|
| *"How much have I spent on food?"* | Queries the ledger, returns total |
| *"$8 lunch at Toast Box"* | Logs it through the same categorisation flow as emails |
| *"Which card should I use at Cold Storage?"* | Best card given category multipliers and how full each cap is |
| *"Can I afford a $150 jacket?"* | Checks discretionary budget, gives an assessment |
| *"Reallocate $50 from Dining to Transport"* | Previews, confirms, updates budget limits |
| *"Alex owes me $20 for lunch"* | Opens an IOU; one tap in the PWA marks it repaid and the ledger self-corrects |
| *"rm33 lunch"* during a trip | Converts at today's rate, routes to the trip's budget and bucket, and counts it |

And **Kevin**, a single-file PWA dashboard, shows the read-side at a glance: left-to-spend pace, budget meters, per-card statement-cycle and bonus-cap meters, trip pots, open IOUs, detected subscriptions, and a receipt-styled transaction tape. Open [`pwa/index.html`](pwa/index.html) straight from disk to see it with sample data — no setup needed.

### How one purchase becomes a row

Roughly five minutes, most of it Gmail's polling interval. The ordering matters more than the boxes: the audit row is written *before* the webhook fires, the dedup can stop the flow dead, and the confirmation is sent by the tool rather than the model.

```mermaid
sequenceDiagram
    autonumber
    participant B as Bank
    participant G as Gmail + Apps Script
    participant K as Kevin
    participant DB as Supabase
    participant T as Telegram

    B->>G: transaction alert email
    Note over G: regex parse per bank<br/>FX-convert to SGD if foreign<br/>compute the idempotency key
    G->>DB: audit row into webhook_log
    Note over G,DB: written BEFORE the send — emails are<br/>marked read and there is no retry
    G->>K: HMAC-signed POST /webhooks/expense-ingest
    K->>DB: merchant_map lookup · active trip?
    Note over K: learned mapping → LLM judgment;<br/>trip routing is deterministic, in the tool
    K->>DB: insert, on_conflict=idempotency_key
    DB-->>K: txn_id — or "duplicate", and the flow stops here
    K->>T: confirmation bubble
    Note over K,T: the TOOL sends this, then returns<br/>assistant_reply_required:false so the agent<br/>loop exits — you get exactly one message
    T->>K: "that's transport, not food"
    K->>DB: edit the row, learn the mapping
```

A weekly sweep diffs `webhook_log` against the ledger, so anything that fell out between steps 3 and 8 gets reported rather than lost.

---

## The build, in five eras

The repo reads best as a story. Each era ended because something broke in a way the next era had to fix.

### 1 · Make it log

Gmail Apps Script regex-parses bank alert emails, HMAC-signs a payload, POSTs to the agent's webhook; the LLM picks a category; a row is appended; Telegram confirms. It worked on day one — and immediately double-replied to every transaction, because the confirmation is sent by the *tool* and the model couldn't reliably be prompted into silence. The eventual fix wasn't a better prompt: it's a build-time patch to the agent loop ([`deploy/patches/suppress_reply_on_silent_tools.py`](deploy/patches/suppress_reply_on_silent_tools.py)) that exits when a tool result carries `assistant_reply_required: false`. Knowing when to stop talking to the model and start writing code around it became the theme of the whole project.

### 2 · Make it not lose money

An agent that touches a ledger must fail loud, never silent. This era added:

- **Idempotency keys** — 16-hex SHA-256 of `date|merchant|amount|payment_method`, computed byte-identically in Apps Script (JS) and Python, with pinned parity tables on both sides so drift fails tests instead of silently double-logging real money. When two genuinely identical purchases in one day collided (a $7.80 food-court double-charge — the second one was eaten as a "duplicate"), the transaction *time* joined the key — appended only when present, so every previously stored key stayed valid. The key now travels end to end: computed once in the Apps Script, carried in the payload, deduped atomically on insert.
- **Audit-before-send** — every parsed email is written to a durable webhook log *before* the webhook fires; a weekly sweep diffs that log against the ledger and reports anything the LLM or API dropped.
- **Fail-loud deploys** — upstream hermes-agent is pinned by SHA; the build-time patches abort the build if the anchor they key on has moved.

### 3 · Make it know my life

Domain intelligence no off-the-shelf app has:

- **Card optimiser** — Singapore cards run on *two different clocks*: statement cycles cut mid-month (per card), while issuers' bonus caps run on *calendar* months. The card tools track both, recommend the right card per merchant, and nudge at log time — seconds after the purchase that crossed 80%/100% of a cap, when switching cards still matters.
- **Travel mode and trip pots** — a transaction that carries a foreign-currency signal during an active trip is routed to the trip's budget *by the tool*, deterministically, and tagged `[bucket:X]`; per-bucket alerts fire at 80%/100%. A trip's *funded* pot is derived from `[trip:]`-tagged top-ups, never from the planned envelope. Manual foreign entries ("rm33") are converted in the tool and counted; the model never routes.
- **Lending** — IOUs are real ledger rows, so monthly totals self-correct when someone pays you back instead of quietly overstating what you spent.
- **Merchant learning** — corrections (never first sights) become mappings; longest match wins; multi-category merchants like supermarkets stay default-then-reply.
- **Fixed vs variable bills** — subscriptions, insurance and utilities sit at 100% of budget by design. They are excluded from "over 80%" warnings and surface only when they come in *over their usual amount*.

### 4 · Give it a real database and a face

The Google Sheet had been the database from day one — the right call until it wasn't. This era moved the ledger to **Supabase Postgres** (atomic dedup via `ON CONFLICT DO NOTHING`, row-level security, the Sheet demoted to an optional nightly read-only export for humans), and shipped **Kevin**: one HTML file, no framework, no build step. Email-OTP auth, RLS-scoped reads, client-side stat calcs, and an in-app budget editor.

Having a real database made derived features cheap. Subscription detection moved from "merchant repeats" to *billing behaviour* — same category, once a month, tight day-of-month spread, near-flat amount — because bank strings carry per-charge reference codes that make merchant matching useless.

The face also turned out to be the attack surface: the self-sent iPhone-Shortcut ingest path was anchored to `from:me` (without it, anyone who knew the inbox address could mail a crafted body and have it signed and logged as a real transaction), a text sink in the insights renderer was escaped, and `supabase-js` was vendored at a pinned version instead of loaded from a floating CDN tag on a page that holds an authenticated session.

### 5 · Make it cheap, and make it current

Two concurrent bank emails once tripped a fresh OpenAI account's tokens-per-minute ceiling. Measuring where the tokens went (a per-call usage log, [`deploy/patches/log_llm_usage.py`](deploy/patches/log_llm_usage.py)) found that ~40% of all spend was a memory-flush agent replaying every webhook session at 4 AM for nothing, and that every ingest carried the full 10k-token skill plus 5k tokens of tool schemas it never used. The diet: a slim webhook-only skill, a webhook toolset of one, pacing in the Apps Script (two posts per tick, 20 s apart — a queue that costs no tokens), and a fix for the flush. Tokens per webhook call ~27k → ~14k; the 4 AM bucket gone; prompt-cache hit rate 74%; total tokens per day −51%.

Then the framework moved: hermes-agent 0.10 → 0.21 in one bump, ~19,000 upstream commits. Four of five patches stopped applying, the tool-injection trick had been silently doing nothing for months, cron started allow-listing tools per platform (every scheduled job would have lost every tool, with no warning), and "say nothing" changed meaning (an empty reply became a warning bubble). Three patches were deleted because upstream now did their job. The whole thing is written up in [docs/HERMES-0.20-MIGRATION-NOTES.md](docs/HERMES-0.20-MIGRATION-NOTES.md), and the lesson became infrastructure: a weekly [anchor-check workflow](.github/workflows/anchor-check.yml) tests the newest upstream tag against every assumption this repo makes and opens an issue saying "safe to bump" or "this drifted". Upgrading is now a [documented, self-service procedure](docs/UPGRADING-HERMES.md).

---

## Why an agent and not a cron + LLM call?

The "ChatGPT-summarises-my-CSV-once-a-week" version is the obvious lighter take. It's not what I want:

- **Bank emails are event-driven.** Webhook in seconds, not "we'll see at the next cron tick."
- **No persistent identity.** A cron call starts from zero every run — no learned merchant→category mappings, no "you've already nudged me about Grab this week."
- **No writes, no tools.** Pure summarisation can't log a manual expense, edit yesterday's row, or update the budget.
- **No interactive surface.** You can't ask *"can I afford a $150 jacket?"* against a one-shot summary.
- **Prompt changes are unversioned.** A cron prompt drifts silently; a versioned skill markdown changes deliberately.

Cron is one of *several* invocation paths here (six scheduled jobs), not the whole product.

---

## Architecture

```
                     Inputs
          ┌────────────┴─────────────┐
          │                          │
   Gmail Apps Script             Telegram
   (HMAC webhook,                 bot
    audit-first, paced)             │
          └────────────┬────────────┘
                       ▼
       ┌────────────────────────────────┐
       │        System prompt           │
       │  SOUL.md (+ memory files)      │  ← tier-2
       │              +                 │
       │  Active skill markdown         │  ← one of five, versioned;
       │              +                 │    the webhook lane gets a slim one
       │  37 registered tools           │  ← per-platform allow-list
       └────────────────┬───────────────┘
                        ▼
            LLM (any OpenAI-compatible
             endpoint — swap via config)
                        │
        ┌───────────┬───┴────────┬─────────────┐
        ▼           ▼            ▼             ▼
   Supabase      Telegram     Cron jobs    Google Sheet
   Postgres      (replies,    (6 SGT       (optional nightly
   (ledger,       nudges)      schedules)   export, human view)
    15 tables,
    RLS)
        │
        ▼
   Kevin PWA (email-OTP auth, RLS reads,
   sample-data mode when opened from disk)
```

### The three tier-2 files

Small on purpose. Each carries an explicit *"what does NOT belong here"* section: transaction data, derived insights, budget numbers and category lists live in the database, never in the prompt.

| File | Contents | Why it's separate |
|---|---|---|
| [`SOUL.md`](hermes-config/SOUL.md.example) (~50 lines) | Voice, principles, failure modes | Anchors the persona so the agent feels like *one thing* across skills |
| [`USER.md`](hermes-config/USER.md.example) | People, payment methods, communication rules | Things the model needs to know about *you* that don't change week to week |
| [`MEMORY.md`](hermes-config/MEMORY.md.example) | Schema, webhook payload shape, enums, edge-case quirks | Machine-ish reference — answers "how does this system work" questions |

They ship as `.example` templates and are gitignored under their real names, so a filled-in `USER.md` can't be committed by accident. One honest caveat, discovered during the 0.21 upgrade and documented rather than hidden: hermes reads `USER.md` and `MEMORY.md` from the *persistent disk*, not from where the Dockerfile copies them — so only `SOUL.md` loads from the image. The templates say how to make the other two live.

### Skills, tools, storage

- **Skills** are versioned markdown files in [`skills/`](skills/): `expense-tracker` (categorisation, travel routing, trips/pots, lending, the silence contract), `expense-ingest` (a slim webhook-only subset, the only skill the ingest route loads — a token diet, mirror-locked to its parent), `card-optimiser`, `budget-manager`, `weekly-summary`. The active skill defines the playbook; tier-2 stays constant across them. Slash commands (`/log`, `/undo`, `/budget`, `/summary`) are registered by four small bundle files in `hermes-config/skill-bundles/` — plain text works without them.
- **Tools** are Python functions registered imperatively in [`tools/expense_sheets_tool.py`](tools/expense_sheets_tool.py) (no decorator — the registry is injected by hermes-agent at runtime and stubbed in tests). They reach the model through the per-platform allow-lists in [`cli-config.yaml`](hermes-config/cli-config.yaml): a platform whose list omits `expense_tracker` sees none of them and says nothing about it — one of the named mistakes in [`AGENTS.md`](AGENTS.md).
- **Storage** is Supabase Postgres behind [`tools/supabase_client.py`](tools/supabase_client.py) (stdlib urllib, no SDK); [`tools/sheets_client.py`](tools/sheets_client.py) holds the optional export path and the idempotency formula. Schema in [`supabase/migrations/`](supabase/migrations/) — eight numbered files, append-only — consolidated into [`supabase/schema.sql`](supabase/schema.sql) so a fresh install is one paste.

---

## Skills

### `expense-tracker` — the conversational core

Owns bank-email parses, manual logging, receipt photos, reply-to-message edits, travel-mode routing, trip pots, and lending.

| Trigger | Behaviour |
|---|---|
| Webhook delivers a parsed bank email | MerchantMap lookup → LLM judgment → log → Telegram bubble (trip routing happens inside the tool) |
| User: *"$8 lunch at Toast Box"* | Same flow, manually triggered |
| User replies to a confirmation: *"that's transport not food"* | Resolves the `txn_id` via `telegram_message_id`, edits the row, learns the mapping |
| Foreign-currency txn during an active trip | Auto-routes to the trip budget with a `[bucket:X]` tag; 80%/100% bucket alerts |
| User: *"Alex owes me $20"* | Preview-then-confirm IOU; the nightly sweep completes the ledger when it's repaid |
| User: *"undo"* | Preview-then-confirm removal of the last transaction |

### `expense-ingest` — the same flow, on a diet

The webhook route loads this and only this: the categorisation order, the multi-category merchant list, the silence contract, the travel rules — nothing else. Every edit to those sections of `expense-tracker` is mirrored here in the same commit; the repo's operating manual treats that mirror as a contract.

### `card-optimiser` — miles & rewards

Cap tracking on the statement-cycle clock, bonus caps on the calendar clock, two-pool tracking for cards that split their cap, recommendations, min-spend and wrong-card steering nudges, promo overrides with lazy expiry, post-cap nudges fired from inside `log_expense`. Gated: all card tools return `setup_required` until card data exists, and the rest of the agent works untouched.

### `budget-manager` — burn-down warnings

Cron-invoked prompt variant over the same toolset: the evening review's attention block (variable categories over 80%, fixed bills over their usual), natural-language reallocation, the *"can I afford X?"* calculator.

### `weekly-summary` — reports

Friday summary with a "cards this month" section; 1st-of-month final report + miles scorecard; subscription-creep detection (excludes statement-import rows).

### Repo-side skills (Claude Code, never shipped)

A different species: workflows in [`.claude/skills/`](.claude/skills/) run by a human against the checkout, not by the deployed agent. Each is a markdown checklist, readable without Claude Code.

| Skill | What it does |
|---|---|
| `/statement-recon` | Runs the [`recon/`](recon/) CLI — checksum-gated PDF parsers that diff monthly e-statements against the ledger — and helps triage the diff |
| `/card-tnc-review` | Quarterly re-verification of card T&Cs against official issuer sources; hash-skips unchanged sources |
| `/new-alert-source` | The eight-step checklist for onboarding a new alert email source: parser, byte-for-byte Python mirror, parity pins, deploy note |

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent framework | [hermes-agent](https://github.com/NousResearch/hermes-agent) 0.21.0, pinned by SHA, two build-time patches, weekly upstream check |
| LLM | Any OpenAI-compatible endpoint (a nano-class model handles it — categorisation is not a frontier problem) |
| Ledger | Supabase Postgres (free tier), RLS-locked, 15 tables |
| Human view / backup | Google Sheet, rebuilt nightly by the agent — **optional** |
| Dashboard | Single-file PWA — no framework, no build step, dependencies vendored |
| Ingress | Telegram bot + Gmail Apps Script (HMAC-signed webhook, audit-before-send, paced) |
| Egress | Telegram + 6 cron schedules (wall-clock in the container's timezone) |
| Deploy | Docker on Render (Singapore), ~$7/mo all-in |
| Tests | pytest — 648 tests, ~2 s, no network; CI on every push |

---

## Setting it up for yourself

**→ [`docs/SETUP.md`](docs/SETUP.md)** is the real guide: what to sign up for, every credential, what to change because it's hardcoded, a verification ladder, and a troubleshooting table. Budget an afternoon.

The one architectural fact that makes self-hosting sane: **every credential stays in accounts you own** — your Gmail runs the Apps Script (your own script on your own mailbox needs no OAuth app verification), your Supabase holds the data, your Render runs the agent, your bot talks to you. There is no service in the middle, and nothing here phones home.

You need four accounts: **Supabase, Telegram, OpenAI, Render.** That's it — Google Cloud and a spreadsheet are an optional backup, not a dependency.

1. **Supabase** — free project; put your email in `is_owner()`, then paste [`supabase/schema.sql`](supabase/schema.sql) into the SQL editor once. One paste, not eight files.
2. **Render** — deploy the Dockerfile as a web service with a 1 GB disk and nine env vars (seven required).
3. **The PWA** — paste your project URL + publishable key into two files, publish `pwa/` as a static site.
4. **Apps Script** — paste [`Code.gs`](apps-script/Code.gs) into a script on *your* Google account, set Script Properties, run `setupTrigger()`. This needs no Google Cloud project. Skip it entirely and log by chat instead.
5. **Your banks** — the parsers are regexes for specific alert layouts. A different bank means a new parser *and* its Python mirror in [`tests/test_email_parser.py`](tests/test_email_parser.py), changed together.
6. **Optional: card optimiser** — insert your cards' earn rates, caps, and cycle start days. Until then every card tool politely returns `setup_required`.
7. **Optional: the Sheet backup** — a Google Cloud service account and a spreadsheet buy you a human-browsable copy rebuilt nightly.

Swapping the defaults: the **model** is a `base_url` edit away from any OpenAI-compatible provider, the **host** is any Docker runner with a persistent disk, and the **chat platform** is the one genuinely wired-in piece — see [Swapping the pieces](docs/SETUP.md#swapping-the-pieces) for the honest cost of each.

### Keeping it current

hermes-agent releases every week or two. [`docs/UPGRADING-HERMES.md`](docs/UPGRADING-HERMES.md) is the procedure, and the [anchor-check workflow](.github/workflows/anchor-check.yml) runs it for you every Monday — report-only, into an issue. No step needs the original author.

### What isn't in this repo

The deployed instance also carries card-strategy research — issuer T&C extracts, a rendered strategy page, a source-hash ledger, and `card_strategy` seed rows. That's one person's actual card holdings, so it isn't published, and it would be the wrong answer for you anyway. `/card-tnc-review` builds the equivalents for whatever cards you carry.

---

## Running the tests

```bash
pip install pytest gspread google-auth cffi
pytest tests/ -q        # 648 tests, ~2s, no network
```

`cffi` is a hidden hard dependency — without it pytest dies at collection with `pyo3_runtime.PanicException`. `conftest.py` stubs the framework-injected tool registry, so the suite runs without hermes-agent installed.

[`AGENTS.md`](AGENTS.md) is the operating manual for coding agents working in this repo — a catalogue of the mistakes that actually cost debugging time here, and the rule that prevents each. It's worth reading before your first change even if you're human. (`CLAUDE.md` is a short pointer at it, because Claude Code looks for that filename.)

[`CHANGELOG.md`](CHANGELOG.md) is dated and human-readable. MIT licensed — see [`LICENSE`](LICENSE).

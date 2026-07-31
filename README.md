# Kevin — a personal-finance agent (PEH)

Bank emails → Gmail webhook → tool-calling LLM agent → Postgres ledger → Telegram replies + a receipt-styled PWA dashboard. A personal-finance agent built for the Singapore context (DBS/UOB/HSBC email shapes, SGD, the local card-rewards game), deployed on a $7/mo Render box.

This is the production code I run daily, scrubbed of personal data: **37 tools · 4 versioned skills · 537 tests**. Sample identifiers (card numbers, merchants, emails) are fictional; the personal memory files ship as `.example` templates. Clone it, configure it to your banks, and it's yours — [**docs/SETUP.md**](docs/SETUP.md) is the runbook.

---

## What it does

Bank-alert emails are parsed within ~5 minutes of a purchase and land in the ledger already categorised, with a Telegram confirmation I can reply to ("that's transport, not food") to correct and teach it. Everything else is a conversation:

| Message in Telegram | What happens |
|---|---|
| *"How much have I spent on food?"* | Queries the ledger, returns total |
| *"$8 lunch at Toast Box"* | Logs it through the same categorisation flow as emails |
| *"Which card should I use at Cold Storage?"* | Best card given category multipliers and how full each cap is |
| *"Can I afford a $150 jacket?"* | Checks discretionary budget, gives an assessment |
| *"Reallocate $50 from Dining to Transport"* | Previews, confirms, updates budget limits |
| *"Alex owes me $20 for lunch"* | Opens an IOU; one tap in the PWA marks it repaid and the ledger self-corrects |

And **Kevin**, a single-file PWA dashboard, shows the read-side at a glance: left-to-spend pace, budget meters, per-card statement-cycle and bonus-cap meters, trip pots, open IOUs, detected subscriptions, and a receipt-styled transaction tape. Open [`pwa/index.html`](pwa/index.html) straight from disk to see it with sample data — no setup needed.

---

## The build, in four eras

The repo reads best as a story in four acts. Each era ended because something broke in a way the next era had to fix.

### 1 · Make it log

Gmail Apps Script regex-parses bank alert emails, HMAC-signs a payload, POSTs to the agent's webhook; the LLM picks a category; a row is appended; Telegram confirms. It worked on day one — and immediately double-replied to every transaction, because the confirmation is sent by the *tool* and the model couldn't reliably be prompted into silence. The eventual fix wasn't a better prompt: it's a build-time patch to the agent loop ([`deploy/patches/suppress_reply_on_silent_tools.py`](deploy/patches/suppress_reply_on_silent_tools.py)) that exits when a tool result carries `assistant_reply_required: false`. Knowing when to stop talking to the model and start writing code around it became the theme of the whole project.

### 2 · Make it not lose money

An agent that touches a ledger must fail loud, never silent. This era added:

- **Idempotency keys** — 16-hex SHA-256 of `date|merchant|amount|payment_method`, computed byte-identically in Apps Script (JS) and Python, with pinned parity tables on both sides so drift fails tests instead of silently double-logging real money. When two genuinely identical purchases in one day collided (a $7.80 food-court double-charge — the second one was eaten as a "duplicate"), the transaction *time* joined the key — appended only when present, so every previously stored key stayed valid.
- **Audit-before-send** — every parsed email is written to a durable webhook log *before* the webhook fires; a weekly sweep diffs that log against the ledger and reports anything the LLM or API dropped.
- **Fail-loud deploys** — upstream [hermes-agent](https://github.com/alhazjm/hermes-agent) is pinned by SHA; the Dockerfile injects tool names and patches via anchors that abort the build if upstream shifts underneath them.

### 3 · Make it know my life

Domain intelligence no off-the-shelf app has:

- **Card optimiser** — Singapore cards run on *two different clocks*: statement cycles cut mid-month (per card), while issuers' bonus caps run on *calendar* months. The card tools track both, recommend the right card per merchant, and nudge at log time — seconds after the purchase that crossed 80%/100% of a cap, when switching cards still matters.
- **Travel mode and trip pots** — FX-converted transactions during an active trip auto-route to the trip's budget and get a `[bucket:X]` tag in Notes; per-bucket alerts fire at 80%/100%. A trip's *funded* pot is derived from `[trip:]`-tagged top-ups, never from the planned envelope.
- **Lending** — IOUs are real ledger rows, so monthly totals self-correct when someone pays you back instead of quietly overstating what you spent.
- **Merchant learning** — corrections (never first sights) become mappings; longest match wins; multi-category merchants like supermarkets stay default-then-reply.

### 4 · Give it a real database and a face

The Google Sheet had been the database from day one — the right call until it wasn't. This era moved the ledger to **Supabase Postgres** (atomic dedup via `ON CONFLICT DO NOTHING`, row-level security, the Sheet demoted to a nightly read-only export for humans), and shipped **Kevin**: one HTML file, no framework, no build step. Email-OTP auth, RLS-scoped reads, client-side stat calcs, and an in-app budget editor (full-year spreadsheet grid on desktop, single-month on phone).

Having a real database made derived features cheap. Subscription detection moved from "merchant repeats" to *billing behaviour* — same category, once a month, tight day-of-month spread, near-flat amount — because bank strings carry per-charge reference codes that make merchant matching useless. That detector's thresholds are duplicated in three places on purpose, and the repo says so out loud.

The face also turned out to be the attack surface. Recent hardening: the self-sent iPhone-Shortcut ingest path was anchored to `from:me` (without it, anyone who knew the inbox address could mail a crafted body and have it signed and logged as a real transaction), a text sink in the insights renderer was escaped, and `supabase-js` was vendored at a pinned version instead of loaded from a floating CDN tag on a page that holds an authenticated session.

---

## Why an agent and not a cron + LLM call?

The "ChatGPT-summarises-my-CSV-once-a-week" version is the obvious lighter take. It's not what I want:

- **Bank emails are event-driven.** Webhook in seconds, not "we'll see at the next cron tick."
- **No persistent identity.** A cron call starts from zero every run — no learned merchant→category mappings, no "you've already nudged me about Grab this week."
- **No writes, no tools.** Pure summarisation can't log a manual expense, edit yesterday's row, or update the budget.
- **No interactive surface.** You can't ask *"can I afford a $150 jacket?"* against a one-shot summary.
- **Prompt changes are unversioned.** A cron prompt drifts silently; a versioned skill markdown changes deliberately.

Cron is one of *several* invocation paths here (seven SGT-scheduled jobs), not the whole product.

---

## Architecture

```
                     Inputs
          ┌────────────┴─────────────┐
          │                          │
   Gmail Apps Script             Telegram
   (HMAC webhook,                 bot
    audit-first)                    │
          └────────────┬────────────┘
                       ▼
       ┌────────────────────────────────┐
       │        System prompt           │
       │  SOUL.md / USER.md / MEMORY.md │  ← tier-2, every turn
       │              +                 │
       │  Active skill markdown         │  ← one of four, versioned
       │              +                 │
       │  37 registered tools           │
       └────────────────┬───────────────┘
                        ▼
            LLM (any OpenAI-compatible
             endpoint — swap via config)
                        │
        ┌───────────┬───┴────────┬─────────────┐
        ▼           ▼            ▼             ▼
   Supabase      Telegram     Cron jobs    Google Sheet
   Postgres      (replies,    (7 SGT       (nightly export,
   (ledger,       nudges)      schedules)   human view)
    14 tables,
    RLS)
        │
        ▼
   Kevin PWA (email-OTP auth, RLS reads,
   sample-data mode when opened from disk)
```

### The three tier-2 files

Loaded into the system prompt every turn. Small on purpose.

| File | Contents | Why it's separate |
|---|---|---|
| [`SOUL.md`](hermes-config/SOUL.md.example) (~50 lines) | Voice, principles, failure modes | Anchors the persona so the agent feels like *one thing* across skills |
| [`USER.md`](hermes-config/USER.md.example) | People, payment methods, communication rules | Things the model needs to know about *you* that don't change week to week |
| [`MEMORY.md`](hermes-config/MEMORY.md.example) | Schema, webhook payload shape, enums, edge-case quirks | Machine-ish reference — answers "how does this system work" questions |

Each file carries an explicit *"what does NOT belong here"* section. Transaction data, derived insights, budget numbers, category lists all live in the database — never in the prompt. That's how tier-2 stays small as the system grows.

They ship as `.example` templates and are gitignored under their real names, so a filled-in `USER.md` can't be committed by accident. Copy them before your first build — see [SETUP](docs/SETUP.md#0-prerequisites).

### Skills, tools, storage

- **Skills** are versioned markdown files in [`skills/`](skills/) — `expense-tracker v5.6.0` (categorisation, travel routing, trips/pots, lending, the silence contract), `card-optimiser v1.3.0`, `budget-manager v3.0.0`, `weekly-summary v3.0.0`. The active skill defines the playbook; tier-2 stays constant across them.
- **Tools** are Python functions registered imperatively in [`tools/expense_sheets_tool.py`](tools/expense_sheets_tool.py) (there is no decorator — the registry is injected by hermes-agent at runtime and stubbed in tests). The tool list is injected into hermes-agent's `toolsets.py` via a `sed` line in the [`Dockerfile`](Dockerfile), with a `grep` verification that fails the build loud if the injection no-ops. Registering a tool without adding it to that `sed` line loads it fine and makes it permanently uncallable — one of the named mistakes in [`CLAUDE.md`](CLAUDE.md).
- **Storage** is Supabase Postgres behind [`tools/supabase_client.py`](tools/supabase_client.py) (stdlib urllib, no SDK); [`tools/sheets_client.py`](tools/sheets_client.py) holds the export path and the idempotency formula. Schema in [`supabase/migrations/`](supabase/migrations/) — six numbered files, run by hand, append-only.

---

## Skills

### `expense-tracker` (v5.6.0) — the conversational core

Owns bank-email parses, manual `/log` messages, receipt photos, reply-to-message edits, travel-mode routing, trip pots, and lending.

| Trigger | Behaviour |
|---|---|
| Webhook delivers a parsed bank email | MerchantMap lookup → travel mode → LLM judgment → log → Telegram bubble |
| User: *"$8 lunch at Toast Box"* | Same flow, manually triggered |
| User replies to a confirmation: *"that's transport not food"* | Resolves the `txn_id` via `telegram_message_id`, edits the row, learns the mapping |
| Foreign-currency txn during an active trip | Auto-routes to the trip budget with a `[bucket:X]` tag; 80%/100% bucket alerts |
| User: *"Alex owes me $20"* | Preview-then-confirm IOU; the nightly sweep completes the ledger when it's repaid |
| User: *"undo"* | Preview-then-confirm removal of the last transaction |

### `card-optimiser` (v1.3.0) — miles & rewards

Cap tracking on the statement-cycle clock, bonus caps on the calendar clock, two-pool tracking for cards that split their cap, recommendations, min-spend and wrong-card steering nudges, promo overrides with lazy expiry, post-cap nudges fired from inside `log_expense`. Gated: all card tools return `setup_required` until card data exists, and the rest of the agent works untouched.

### `budget-manager` (v3.0.0) — burn-down warnings

Cron-invoked prompt variant over the same toolset: daily silent budget check (speaks only when a category crosses 80%), natural-language reallocation, the *"can I afford X?"* calculator.

### `weekly-summary` (v3.0.0) — reports

Friday summary with a "cards on pace" section; 1st-of-month final report + miles scorecard; subscription-creep detection (excludes statement-import rows).

### Repo-side skills (Claude Code, never shipped)

A different species: workflows in [`.claude/skills/`](.claude/skills/) run by a human against the checkout, not by the deployed agent.

| Skill | What it does |
|---|---|
| `/statement-recon` | Runs the [`recon/`](recon/) CLI — checksum-gated PDF parsers that diff monthly e-statements against the ledger — and helps triage the diff. Statements stay in a gitignored `samples/` |
| `/card-tnc-review` | Quarterly re-verification of card T&Cs against official issuer sources; hash-skips unchanged sources so a quiet quarter is nearly free |
| `/new-alert-source` | The eight-step checklist for onboarding a new alert email source: parser, byte-for-byte Python mirror, parity pins, deploy note |

---

## Tech stack

| Layer | Choice |
|---|---|
| Agent framework | [hermes-agent](https://github.com/alhazjm/hermes-agent) (pinned by SHA, patched at build time) |
| LLM | Any OpenAI-compatible endpoint (a nano-class model handles it — categorisation is not a frontier problem) |
| Ledger | Supabase Postgres (free tier), RLS-locked, 14 tables |
| Human view / backup | Google Sheet, rebuilt nightly by the agent |
| Dashboard | Single-file PWA — no framework, no build step, dependencies vendored |
| Ingress | Telegram bot + Gmail Apps Script (HMAC-signed webhook, audit-before-send) |
| Egress | Telegram + 7 cron schedules (SGT wall-clock) |
| Deploy | Docker on Render (Singapore), ~$7/mo all-in |
| Tests | pytest — 537 tests, ~1s, no network |

---

## Setting it up for yourself

**→ [`docs/SETUP.md`](docs/SETUP.md)** is the real guide: every account, every credential, what to change because it's hardcoded, a verification ladder, and a troubleshooting table. Budget a couple of hours.

The one architectural fact that makes self-hosting sane: **every credential stays in accounts you own** — your Gmail runs the Apps Script (your own script on your own mailbox needs no OAuth app verification), your Supabase holds the data, your Render runs the agent, your bot talks to you. There is no service in the middle, and nothing here phones home.

The shape of it, so you know what you're signing up for:

1. **Supabase** — free project; put your email in `is_owner()`, run the six migrations by hand.
2. **Render** — deploy the Dockerfile as a web service with a 1 GB disk; nine env vars.
3. **The PWA** — paste your project URL + publishable key into two files, publish `pwa/` as a static site.
4. **Apps Script** — paste [`Code.gs`](apps-script/Code.gs) into a script on *your* Google account, set Script Properties, run `setupTrigger()`.
5. **Your banks** — the parsers are regexes for DBS/UOB/HSBC layouts. A different bank means a new parser *and* its Python mirror in [`tests/test_email_parser.py`](tests/test_email_parser.py), changed together — the test file is the executable spec, and `testIdempotencyKeyParity()` verifies the dedup key still matches Python before you trust it with real money.
6. **Optional: card optimiser** — insert your cards' earn rates, caps, and cycle start days. Until then every card tool politely returns `setup_required`.

### What isn't in this repo

The deployed instance also carries card-strategy research — issuer T&C extracts, a rendered strategy page, a source-hash ledger, and `card_strategy` seed rows. That's one person's actual card holdings, so it isn't published, and it would be the wrong answer for you anyway. `/card-tnc-review` builds the equivalents for whatever cards you carry.

---

## Running the tests

```bash
pip install pytest gspread google-auth cffi
pytest tests/ -q        # 537 tests, ~1s, no network
```

`cffi` is a hidden hard dependency — without it pytest dies at collection with `pyo3_runtime.PanicException`. `conftest.py` stubs the framework-injected tool registry, so the suite runs without hermes-agent installed.

[`CLAUDE.md`](CLAUDE.md) (mirrored as [`AGENTS.md`](AGENTS.md)) is the operating manual for coding agents working in this repo — a catalogue of the mistakes that actually cost debugging time here, and the rule that prevents each. It's worth reading before your first change even if you're human.

# SETUP — running your own instance

First-run guide for someone who has cloned this repo and wants their own
copy running. Written against the code as it stands (Supabase ledger, four
email parsers, the PWA, loans and trip pots), and re-verified against the
tree — where a line number appears below, it was read out of the file, not
remembered.

## Read this first

This is one person's deployment, not a product. There is no multi-tenancy:
one Supabase project, one Google Sheet, one Telegram chat, one Render
service, and row-level security pinned to a specific email address. Running
your own copy means provisioning your own of each and editing the places
where the owner's identity is hardcoded — see
[What is hardcoded](#what-you-will-need-to-change-because-it-is-hardcoded),
which is the section that will actually cost you time if you skip it.

Budget a couple of hours. Expect to need a paid Render tier: the gateway is
a long-lived process that polls Telegram, serves the webhook, and runs cron
in-process, so a free tier that sleeps will not work.

Two things this guide will not fabricate: exact dashboard button labels for
Supabase and Render (they move), and any step I could not verify from the
code in this repo. Where a source in the repo disagrees with another, the
disagreement is called out rather than silently resolved.

### The five surfaces

Nothing you edit is live until it crosses its surface's boundary, and each
one deploys differently.

| Surface | What lives there | How it deploys |
|---|---|---|
| Render web service | The agent (Docker container built from `Dockerfile`) | Push to `main` → auto-build |
| Supabase project | The ledger of record — every table in `supabase/migrations/` | SQL you run by hand in the SQL editor |
| Render static site | The PWA (`pwa/`) | Separate service; same repo, publish directory `pwa/` |
| Google Apps Script | Gmail → webhook ingest (`apps-script/Code.gs`) | `clasp push` + Script Properties set by hand |
| Google Sheet | Nightly read-only export of the ledger | Created by hand; rebuilt by the 3 AM cron |

The Google Sheet used to be the database. Since the Supabase migration it is
a backup view: `tools/expense_sheets_tool.py` reads and writes Supabase for
everything, and the only tools that touch the Sheet are `export_sheet_backup`
(the 3 AM cron), which **overwrites** the `Transactions` and `Budget` tabs
from Supabase, and `archive_year_snapshot` (the Jan 1 cron), which writes an
`Archive-<year>` tab once. Hand edits to the Sheet do not survive the night,
by design.

You still need both. Every tool is gated on
`_sheets_configured()` (`tools/expense_sheets_tool.py:68`), which requires
`GSPREAD_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` **and** both Supabase
variables. Miss any one of them and the tools load but are never exposed to
the model — the bot chats happily and cannot log a thing.

---

## 0. Prerequisites

| Account / tool | What for | Cost |
|---|---|---|
| GitHub | Render builds from a repo you control | Free |
| Google Cloud project + service account | Sheets API access for the nightly export | Free |
| Google account with Gmail | Bank alert emails + Apps Script | Free |
| Supabase project | The ledger, and PWA auth | Free tier is enough to start |
| Telegram account | Bot + your own chat | Free |
| OpenAI API key | The model (`gpt-5.4-nano`) and speech-to-text (`gpt-4o-mini-transcribe`), both in `hermes-config/cli-config.yaml` | Usage-based |
| Render account | Web service + static site | ~$7/mo for the always-on service |
| Node.js + `clasp` | Deploying the Apps Script | Free |

Local tooling, only if you want to run the tests, the recon CLI, or the
backfill script: Python 3.11, and `pip install pytest gspread google-auth
cffi` (`cffi` is a hidden hard dependency — without it pytest dies at
collection with `pyo3_runtime.PanicException`). The recon CLI additionally
wants `pypdf`.

Fork or push this repo to your own GitHub account first. Render needs a repo
it can read, and you will be committing edits (hardcoded emails, URLs, card
ids) that you do not want to send upstream.

**Do this before your first Docker build.** The three tier-2 memory files
ship as templates, because the real ones carry a name, a household, card
last-4s and goals. The Dockerfile COPYs them under their real names, so a
fresh clone will fail the build until they exist:

```bash
cd hermes-config/
cp USER.md.example   USER.md
cp MEMORY.md.example MEMORY.md
cp SOUL.md.example   SOUL.md
```

Then fill in `USER.md` (it is all placeholders), skim `MEMORY.md` (mostly
system contract — keep it verbatim if you keep the same tools), and rewrite
`SOUL.md` if you want a different voice. All three are gitignored under
their real names, so your filled-in copies can never be pushed here by
accident; commit them to your own fork deliberately if you want Render to
build from them.

**Verify:** `pytest tests/ -q` → **578 passed**, in about a second, with no
network. On Windows: `.venv-test/Scripts/python.exe -m pytest tests/ -q`.

---

## 1. Google Cloud service account

The agent authenticates to Sheets as a service account, not as you.

1. Create a Google Cloud project.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Create a **service account** (no project role needed — access is granted
   by sharing the Sheet with it directly).
4. Create a **JSON key** for it and download the file.

`scripts/setup-google-oauth.sh` walks the same steps and automates them if
you have `gcloud` installed. It writes the key to `~/.hermes/service-account.json`.

Open the JSON and note the `client_email` value
(`something@project-id.iam.gserviceaccount.com`) — step 2 needs it.

**Verify:** you have a `.json` file containing `"type": "service_account"`
and a `client_email`.

---

## 2. The Google Sheet

Create a new spreadsheet. Post-migration you only strictly need two tabs,
because the nightly export rebuilds exactly those two
(`sheets_client.write_sheet_snapshot`, `tools/sheets_client.py:250`):

**Tab `Transactions`** — header row exactly (12 columns; the 12th, `Time`,
was added with the time-in-key fix and is written by the export). This is
`SNAPSHOT_HEADER` at `tools/sheets_client.py:245`:

```
Date | Merchant | Amount | Currency | Category | Source | Payment Method | Notes | txn_id | telegram_message_id | idempotency_key | Time
```

**Tab `Budget`** — header row exactly:

```
Category | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec
```

Note the export always writes the per-month grid, so use that layout.

The other tabs documented in `sheets-template/README.md` (`MerchantMap`,
`Insights`, `Journal`, `WebhookLog`, `Cards`, `CardStrategy`, `CardNudgeLog`,
`TravelMode`, `TripNudgeLog`) are **no longer read at runtime** — their data
lives in Supabase now. Two exceptions worth knowing:

- `WebhookLog` is still auto-created by the Apps Script as a *fallback*
  audit target when the Supabase POST fails (`Code.gs::logToAuditSheet`).
  Leave room for it; you do not need to create it.
- `Archive-<year>` tabs are created by the Jan 1 cron
  (`archive_year_snapshot`). Write-once — an existing tab is never
  overwritten.
- `sheets-template/README.md` still opens with "The Sheet is the source of
  truth". That sentence is stale — read the tab schemas there, ignore the
  framing.

Then:

1. Copy the spreadsheet ID out of the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
2. **Share** the sheet with the service account's `client_email`, giving it
   **Editor** access.

**Verify:** the share dialog lists the `...iam.gserviceaccount.com` address
as an Editor. A missing share is the single most common Sheets failure and
it surfaces as a 403 from gspread, not as anything friendlier.

---

## 3. Supabase — schema, and the RLS emails you MUST change

### 3.1 Create the project

Create a new Supabase project. From its API settings collect three things:

| Value | Looks like | Used by |
|---|---|---|
| Project URL | `https://<ref>.supabase.co` | Render (`SUPABASE_URL`), Apps Script, the PWA |
| Publishable / anon key | `sb_publishable_...` (older projects: a long `anon` JWT) | The PWA only — public by design |
| Secret / service_role key | `sb_secret_...` (older projects: a `service_role` JWT) | Render (`SUPABASE_SERVICE_KEY`) and Apps Script — **server side only** |

The secret key bypasses row-level security completely. It must never appear
in `pwa/*.html`, and those files carry a comment saying so
(a comment just above the two constants in `pwa/index.html`).

### 3.2 Change the owner email BEFORE running the migration

`supabase/migrations/0001_init.sql` lines 176–179:

```sql
create or replace function is_owner() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() ->> 'email', '') = 'you@example.com'
$$;
```

Replace that literal with the email address you will sign into the PWA
with. Every RLS policy in `0001`–`0006` calls `is_owner()`, so this one
line decides whether the PWA can read anything at all.

If you forget: the PWA will let you sign in and then render an empty
dashboard with no error, because RLS returns zero rows rather than a
permission failure. Fix by re-running just the `create or replace function`
block with your address — the policies pick it up immediately, no policy
rewrite needed.

> **If you later widen it**, land the change as a new numbered migration
> too. A live `is_owner()` that no migration describes is drift, and the
> next person to run `0001` on a fresh project silently gets different
> access rules.

### 3.3 Run the SQL

**The short version: open the Supabase SQL editor, paste all of
[`supabase/schema.sql`](../supabase/schema.sql), run it once. Done.**

That file is every migration concatenated in order — one paste instead of
seven, and if any statement fails the whole thing rolls back instead of
leaving you with a half-built database. Change the email in `is_owner()`
first (3.2); the file says so at the top.

It is generated, never hand-written: `python supabase/build_schema.py`
rebuilds it, and `tests/test_schema_consolidation.py` fails the suite if it
ever drifts from the migrations. So it cannot quietly go stale.

The numbered files under `supabase/migrations/` remain the source of truth,
and you want them if you are upgrading an existing database rather than
building a new one — run only the ones you have not run yet, in order:

1. `supabase/migrations/0001_init.sql` — the core schema, `next_txn_id()`,
   RLS enabled on every table with a SELECT-only `owner_read` policy.
2. `supabase/migrations/0002_budget_write_policies.sql` — insert/update/delete
   policies on `budgets` only, so the PWA's budget editor can write.
3. `supabase/migrations/0003_cards_bonus_cap.sql` — adds `cards.bonus_cap`
   and asserts two values. Harmless on a fresh DB (it only touches
   `card_id`s you will not have yet), but those `update` lines carry the
   original deployment's card ids, not yours.
4. `supabase/migrations/0004_transactions_update_policy.sql` — lets the PWA
   change a transaction's category inline.
5. `supabase/migrations/0005_loans.sql` — the `loans` table (IOUs) plus the
   narrow status-flip write policy the PWA's one-tap "repaid" button needs.
6. `supabase/migrations/0006_sub_overrides.sql` — `sub_overrides`, the
   subscription-detector verdict table (keys are normalized **category**
   names, not merchants).
7. `supabase/migrations/0007_cards_base_mpd.sql` — adds `cards.base_mpd`, a
   card's flat "everything else" earn rate. The month-end scorecard uses it
   to value spend that fell outside a category's strategy row. Same caveat
   as 0003: the `update` lines carry the original deployment's card ids, so
   adjust or skip them. Skipping the whole file is silent-but-wrong — the
   column reads as 0 and the scorecard over-reports "miles left on the
   table".
There is deliberately no seed file for `cards` / `card_strategy` — those
rows describe whichever cards you actually carry. Write your own (see 3.4).

All of them are written to be re-runnable (`create table if not exists`,
policies via the idempotent `do $$ … duplicate_object` pattern), so running
`schema.sql` over a database that already has some of them is safe.

Migrations are append-only: never edit one that has already run, and never
edit `schema.sql` by hand. A new schema change is a new numbered file plus a
regenerate, in the same commit.

That is 14 tables when you are done — `transactions`, `budgets`,
`merchant_map`, `txn_id_counters`, `insights`, `journal`, `webhook_log`,
`cards`, `card_strategy`, `card_nudge_log`, `trip_nudge_log`, `travel_mode`,
`loans`, `sub_overrides`.

Everything degrades gracefully if you stop early: the loans and
`sub_overrides` reads are guarded, so an absent table means the feature is
quiet rather than broken.

### 3.4 Seed the tables that are hand-maintained

There is no seed file for `cards` — insert your own rows. The card optimiser
stays inert (`status: "setup_required"`) until both `cards` and
`card_strategy` have rows, including a `_default` sentinel in
`card_strategy`.

```sql
insert into cards (card_id, display_name, payment_method_pattern,
                   cycle_start_day, min_spend_bonus, bonus_cap)
values ('my-card', 'My Bank Visa', 'MYBANK Card ending 1234', 20, 0, 0);

insert into card_strategy (category, primary_card_id, primary_cap,
                           primary_earn_rate, fallback_card_id,
                           fallback_earn_rate, notes)
values ('_default', 'my-card', 0, 1.4, '', 0, 'general-spend catch-all');
```

`payment_method_pattern` is matched case-insensitively as a substring of the
`Payment Method` string your bank emails actually produce — longest match
wins, so check a real transaction before guessing. `cycle_start_day` is the
statement cut, not the calendar month; confirm it off a real statement.
Column meanings are in `sheets-template/README.md` tabs 7 and 8; the
semantics carried over unchanged into the Supabase tables.

Budgets: the PWA's `budget.html` is the intended editor once you are signed
in. To bootstrap before then, insert directly:

```sql
insert into budgets (category, month, limit_amount)
values ('Groceries', '2026-08', 400);
```

`sheets-template/budget_categories.json` is a generic starter category list.
A `$0` limit is a real budget (any spend is over); "no budget set" means no
row at all.

### 3.5 Auth for the PWA

The PWA signs in with Supabase Auth **email OTP** — `signInWithOtp` sends the
mail, and the page accepts either the 6-digit code (typed into the app) or
the magic link (desktop browsers). In the Supabase dashboard you need email
auth enabled, your static-site URL added to the allowed redirect URLs, and
— realistically — your own SMTP configured, because the built-in sender is
heavily rate-limited. The PWA's own error copy points at the same place:
"couldn't send the email — check Supabase Auth Logs (usually the SMTP
password)".

**Verify:** in the SQL editor, `select * from card_strategy where category =
'_default';` returns a row, and `select * from sub_overrides;` succeeds
(empty is fine — it proves `0006` ran). `select is_owner();` returns nothing
useful, because you are running as a superuser rather than through the API —
the real verification of `is_owner()` happens in step 6.

---

## 4. Telegram bot

1. Message **@BotFather** → `/newbot` → pick a name and a username.
2. Copy the token (`123456:ABC-DEF...`). That is `TELEGRAM_BOT_TOKEN`.
3. Message your new bot once, so it is allowed to reply to you.
4. Get your own numeric user id: message **@userinfobot**, which replies with
   it. That is `TELEGRAM_ALLOWED_USERS`.

That variable does double duty. `deploy/start.sh:22` also exports it as
`TELEGRAM_HOME_CHANNEL`, which is where the seven cron jobs deliver their
messages. So it must be your numeric id, not a username.

**Verify:** `https://api.telegram.org/bot<TOKEN>/getMe` returns your bot's
JSON.

---

## 5. Render — the web service

### 5.1 Create it

Blueprint route: point Render at your fork and it reads `render.yaml` —
Docker runtime, Singapore region, Starter plan, and a 1 GB disk named
`hermes-data` mounted at `/data`.

Manual route: New → Web Service → Docker runtime → add a disk with mount
path `/data`, size 1 GB. The disk is not optional: it holds the service
account file, sessions, memories, cron jobs, LLM usage logs, and the cron
seed marker.

Region and timezone: the image sets `TZ=Asia/Singapore` and every cron
expression is SGT wall-clock. If you want your own timezone, change that
`ENV TZ` line — do **not** convert the cron expressions to UTC, and do not
add timezone-aware datetimes to the Python (all datetime code is
deliberately naive).

### 5.2 Environment variables

Set these in the dashboard. Everything except `PEHD_LLM_USAGE_DIR` is
`sync: false` in `render.yaml`, meaning Render will not take a value from the
file — you type them in by hand.

| Variable | What it is | Where to get it |
|---|---|---|
| `OPENAI_API_KEY` | Model + STT credentials; `cli-config.yaml` points at `api.openai.com/v1` | OpenAI dashboard |
| `PEHD_LLM_USAGE_DIR` | Where per-call LLM usage JSONL is written | Literal `/data/llm_usage` (the one var with a value in `render.yaml`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of the service-account JSON | Step 1's file — paste the whole blob. `start.sh` writes it to `/data/service-account.json` and re-points the variable at that path |
| `GSPREAD_SPREADSHEET_ID` | The Sheet id | Step 2 |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | Step 3.1 |
| `SUPABASE_SERVICE_KEY` | The **secret / service_role** key | Step 3.1 |
| `WEBHOOK_HMAC_SECRET` | Shared secret between Apps Script and the gateway. **Not optional** — see the warning below | Make one: `openssl rand -hex 32` |
| `TELEGRAM_BOT_TOKEN` | Bot token | Step 4 |
| `TELEGRAM_ALLOWED_USERS` | Your numeric user id; also becomes `TELEGRAM_HOME_CHANNEL` | Step 4 |

> **Do not skip `WEBHOOK_HMAC_SECRET`.** `start.sh` only substitutes it into
> `config.yaml` when it is set; otherwise the literal
> `__WEBHOOK_SECRET_PLACEHOLDER__` stays there and becomes your live signing
> key. That string is published in this repo, so an unconfigured instance will
> accept any request signed with a value anyone can read — and every accepted
> webhook is a write to your ledger. The container prints four `WARNING:` lines
> at boot if you get this wrong.

All nine are declared in `render.yaml`. The Supabase pair used to be missing
from it — a Blueprint deploy then came up as a bot that could chat and could
not call a single tool, because `_sheets_configured()` gates every tool on
those two. That is fixed; the comment above them in `render.yaml` records
why they are there.

`deploy/README.md`'s env table still lags the real list — treat
`render.yaml` as authoritative.

### 5.3 Deploy

Push to `main` (auto-deploy) or hit Manual Deploy. The build clones
`alhazjm/hermes-agent` at the SHA pinned in `Dockerfile` (`ARG
HERMES_AGENT_SHA`), copies **six** `tools/*.py` files in (`sheets_client`,
`expense_sheets_tool`, `card_optimiser`, `travel_mode`, `supabase_client`,
`loans`), injects the 37 tool names into the upstream telegram toolset with
a `sed` anchored on `"send_message",`, and applies four patches — each of
which fails the build loudly if its upstream anchor moved. A build that dies
in one of those `RUN python3 /app/patches/...` steps means upstream drifted;
do not loosen the anchor to get green.

**Verify — watch the deploy log for these lines from `start.sh`:**

```
Service account JSON written to /data/service-account.json
Environment written to /root/.hermes/.env
sessions created on persistent disk
Webhook secret injected into config
Seeding cron jobs (first run on this disk)...
Cron jobs seeded successfully
Starting Hermes gateway (foreground)...
TELEGRAM_BOT_TOKEN is set: yes
```

`TELEGRAM_BOT_TOKEN is set: NO` means the variable did not reach the
container. "Webhook secret injected into config" missing means
`WEBHOOK_HMAC_SECRET` is unset and every signed webhook will be rejected —
the secret is substituted into `__WEBHOOK_SECRET_PLACEHOLDER__` in
`config.yaml` at start (`start.sh:72`), because hermes YAML cannot expand env
vars. A second placeholder, `__OPENAI_KEY_PLACEHOLDER__` (the STT key), is
substituted the same way at `start.sh:81`.

---

## 6. The PWA

`pwa/` is a static, bundler-free app: three HTML files, a `vendor/`
directory, an icon, a manifest. It is **not** in the container — the
Dockerfile never copies it. It is served as a **separate Render static site**
from the same repo (see the rationale comment in `pwa/vendor/sync-cards.sh`).

Everything in `vendor/` is committed on purpose:

- `supabase-2.111.0.js` — pinned. It used to load `@supabase/supabase-js@2`
  from jsDelivr, which meant executing whatever the CDN served that day, on
  a page that holds an authenticated session. Vendoring it also makes the
  app work offline after first load.
- `cards.css` / `cards.global.js` — the Muted Card System card art, synced
  from its own repo by `sync-cards.sh`.

### 6.1 Point it at your Supabase

Two files, two constants each:

- `pwa/index.html` — `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY`
- `pwa/budget.html` — the same two constants

```js
const SUPABASE_URL = "https://<your-ref>.supabase.co";
const SUPABASE_PUBLISHABLE_KEY = "sb_publishable_...";
```

Use the **publishable / anon** key. It is public by design: it ships in
plain text to every visitor, and what protects the data is RLS plus auth —
which is why step 3.2 matters. The secret key must never appear here.

Both pages have a graceful setup fallback: opened over `file://`, or with a
key still starting `__`, `index.html` renders sample data and prints
"setup: paste the publishable key into pwa/index.html". That makes a
useful offline preview before you have Supabase wired.

### 6.2 Host it

Create a Render **Static Site** from the same repo with publish directory
`pwa` and no build command. Then add the resulting URL to Supabase Auth's
allowed redirect URLs (the OTP flow passes
`emailRedirectTo: location.origin + location.pathname`).

There is no config file for this in the repo — `render.yaml` describes only
the web service — so it is a dashboard-only setup.

### 6.3 What to expect on the pages

- `index.html` — the dashboard. Reads `transactions`, `budgets`, `cards`,
  `travel_mode`, `insights`, `loans`, `sub_overrides`. Sections: monthly
  hero and chart, budget meters, per-card cycle/bonus meters, trip pot,
  IOUs, subscriptions, and the receipt-styled transaction tape.
- `budget.html` — the budget grid editor. Needs the `0002` write policies;
  without the DELETE policy a row delete "succeeds" touching 0 rows, and the
  page reports exactly that.
Two deliberate divergences worth knowing before you "fix" them: the monthly
hero and chart **count** `backfill` and pending rows (honest cash-out
totals), while every agent-side aggregate excludes them. And YouTrip
*spends* are excluded from monthly totals everywhere, because the top-up was
already the counted outflow.

**Verify:** sign in with the email you put in `is_owner()`. If the dashboard
loads but every number is zero and the tape is empty, `is_owner()` does not
match your login address — go back to 3.2.

---

## 7. Apps Script — Gmail ingest

This is the only surface that merging a PR does not deploy. It ships with
`clasp push`.

### 7.1 Push the code

```bash
npm install -g @google/clasp
clasp login
cd apps-script/
clasp create --type standalone --title "Expense Tracker"
clasp push
```

### 7.2 Edit the two hardcoded constants

In `apps-script/Code.gs`:

- **Line 33** — `WEBHOOK_URL`. Change the host to your Render service:
  `https://<your-service>.onrender.com/webhooks/expense-ingest`.
  Note the path is `/webhooks/` (plural), matching the `webhook` platform
  config in `hermes-config/cli-config.yaml`. `deploy/README.md` describes the
  same URL; `Code.gs` is the source of truth for it.
- **Line 794** — `RENDER_SERVICE_ID` (`srv-...`), only used by the optional
  nightly restart. Replace with your own service id, or delete
  `setupRestartTrigger` / `restartRenderService` if you do not want it.

### 7.3 The two Gmail searches

There are deliberately **two** queries, not one boolean query:

- **Line 64** — `GMAIL_QUERY`: bank senders (DBS, PayLah, UOB, HSBC) plus a
  subject filter.
- **Line 65** — `SHORTCUT_QUERY`: `from:me subject:"YouTrip Transaction"`,
  the self-sent iPhone-Shortcut source.

Do not merge them. Gmail's parser is loose about `OR` precedence and a
combined query silently ANDed the `from:` clause over everything, so the
self-sent email never matched (live failure, 2026-08-01).

`from:me` in `SHORTCUT_QUERY` is also a **forgery guard**, not a
convenience. The Shortcut body is plain text with no signature; without
`from:me`, anyone who knows the inbox address could mail a crafted body and
have it parsed, HMAC-signed by your own script, and written to the ledger as
a real transaction. If you add a Shortcut-style source of your own, keep the
sender constraint.

There are four parsers, each tied to one email layout:
`parseDBS` (line 192), `parseUOB` (242), `parseHSBC` (281), and
`parseYouTrip` (340) — the last dispatched on the subject **prefix**.
A different bank means a new query clause and a new parser. If you write
one, mirror the regexes into `tests/test_email_parser.py` (a deliberate
byte-for-byte Python port) in the same commit. `.claude/skills/new-alert-source`
is the checklist for exactly this — see section 10.

### 7.4 Script Properties

Project Settings → Script Properties. All read at module load in `Code.gs`:

| Property | Why | Required? |
|---|---|---|
| `WEBHOOK_HMAC_SECRET` | Signs `X-Webhook-Signature`; must equal Render's value exactly | Yes |
| `SUPABASE_URL` | Audit rows go to the `webhook_log` table | Yes (the sweep reads that table) |
| `SUPABASE_SERVICE_KEY` | Same; the secret key | Yes |
| `SPREADSHEET_ID` | Fallback audit target (`WebhookLog` tab) when Supabase POST fails | Recommended |
| `TELEGRAM_BOT_TOKEN` | FX-failure and degraded-audit alerts | Recommended |
| `TELEGRAM_CHAT_ID` | Where those alerts go — your numeric id | Recommended |
| `RENDER_API_KEY` | Nightly restart (Render → Account Settings → API Keys, `rnd_...`) | Optional |

### 7.5 Triggers

In the Apps Script editor, run these functions once each:

- `setupTrigger()` — checks Gmail every 5 minutes. Required.
- `setupRestartTrigger()` — restarts the Render service at ~04:00 daily.
  Optional; it exists because the gateway's RSS ratchets toward the 512 MB
  Starter cap over days (OOM on 2026-07-22).

Then enable transaction alert emails in your bank's app, or the pipeline has
no input.

**Verify:** run `testIdempotencyKeyParity()` from the editor — it checks the
JS key against values pinned in the Python suite. Then check the Apps Script
execution log after a real transaction, and look for a new row in Supabase
`webhook_log`. Emails are marked read after processing and there is **no
retry**, which is why the audit row is written *before* the webhook fires.

---

## 8. Cron jobs

`cron/setup-cron-jobs.sh` creates seven jobs via the `hermes cron` CLI. It
runs automatically on the first container start on a given disk, gated by the
marker `/data/cron/.seeded` (`deploy/start.sh:91`). Seeding failures are
non-fatal — the gateway starts anyway and the next restart retries.

| Schedule (SGT) | Job |
|---|---|
| `0 21 * * *` | Daily spending review + `generate_daily_insight` + `sweep_loan_offsets` + journal prompt |
| `0 18 * * 5` | Friday weekly summary + cards-on-pace |
| `0 9 1 * *` | 1st-of-month report + card plan + last month's efficiency scorecard |
| `0 12 * * *` | Silent budget check (messages only when a category is over 80%) |
| `0 22 * * 0` | Sunday sweep for missed transactions (silent when nothing missed) |
| `0 3 * * *` | Nightly Sheet export from Supabase (silent on success) |
| `0 7 1 1 *` | Jan 1 yearly cold-storage archive into an `Archive-<year>` tab |

To change the roster: edit the script, redeploy, then delete
`/data/cron/.seeded` from the Render shell and restart. The seeding script
only **creates** jobs, so re-seeding on a disk that already has them
produces duplicates — remove the existing jobs first with `hermes cron list`
/ `hermes cron remove <id>`, then the marker, then restart.

The full subcommand set at the pinned upstream SHA is `list`, `create`,
`edit`, `pause`, `resume`, `run`, `remove`, `status`, `tick`. There is no
`delete`.

Note `cron/README.md` still lists only four jobs and mentions WhatsApp; it
predates the sweep, the export, the archive, and the move to Telegram.

**Verify:** in the Render shell, `hermes cron list` shows seven jobs.

---

## 9. Verify the whole thing works

Work through these in order; each one isolates a different link in the chain.

1. **Bot is alive.** Telegram → "hi". You get a reply. (Model + token +
   allowed-users all good.)
2. **Tools are visible.** "what's my remaining budget?" — it should call a
   tool and answer with categories. If it says it cannot access your budget,
   `_sheets_configured()` is returning False: one of the four Sheets/Supabase
   variables is missing.
3. **Manual logging works.** `/log $4.20 Test Merchant`. Expect a
   confirmation bubble containing a `txn_YYYYMMDD_NNN` id, and exactly **one**
   message — a second, chattier message after the bubble means the silence
   patch is not doing its job.
4. **It landed.** Supabase → `select * from transactions order by id desc
   limit 5;` — your test row, with `source = 'manual'`.
5. **The PWA sees it.** Reload the dashboard; the transaction appears in the
   tape.
6. **Email ingest works.** Make a small real card transaction, wait for the
   bank alert, and within ~5 minutes expect: a row in `webhook_log`
   (`webhook_status = 'sent'`), a categorisation bubble in Telegram, and a
   `transactions` row with `source = 'email'`.
7. **The sweep works.** "run sweep_missed_transactions for the last 7 days" —
   it diffs `webhook_log` against `transactions`. `missed_count: 0` is the
   healthy answer; a `status: "error"` means the audit table is unreachable.
8. **The export works.** "export to sheets" → the Sheet's `Transactions` and
   `Budget` tabs are rebuilt. This is the same call the 3 AM cron makes.
9. **Loans work** (needs `0005`). "Adam owes me $20 for lunch" → a preview,
   then confirm. `list_open_loans` shows it; the PWA's IOU card shows a
   one-tap repaid button.
10. **The card optimiser is either wired or politely quiet.** "which card for
    Cold Storage?" — a recommendation once `cards` + `card_strategy` have
    rows, or one `setup_required` line until then. Both are correct answers.

Then delete the test transaction ("undo that" / `undo_last_expense`) and the
test loan.

---

## 10. Optional local tooling (never ships)

Two things live in this repo that are run by a human from a checkout and
never reach the container. Neither is required to run the agent.

### 10.1 The statement-recon CLI

`recon/` diffs DBS and UOB credit-card e-statement PDFs against the Supabase
ledger once a month — the bank's ground truth against yours. It exists
because the webhook path can miss transactions (parser gaps, FX failures,
new banks), and because a supplementary card that never emails you enters
the ledger only through this ritual, as `source='backfill'`.

```bash
py -V:3.11 -m pip install pypdf      # first run only
py -V:3.11 recon/recon.py samples/DBS-estatement.pdf samples/UOB-estatement.pdf
```

`samples/` is **gitignored on purpose** — real e-statements carry full card
numbers and must never be committed. The parsers are checksum-gated: if a
statement's own totals do not reconcile, the parser refuses rather than
reporting a confident wrong answer. Full ritual in
[`STATEMENT-RECON.md`](STATEMENT-RECON.md).

### 10.2 The repo-side Claude Code skills

`.claude/skills/` holds three workflows for whoever maintains this repo.
They are a **different species** from `skills/` — those ship to
`/root/.hermes/skills/` and are read by the deployed agent; these run in
Claude Code, against the checkout, and are invoked as slash commands.

| Skill | Invoke | What it does |
|---|---|---|
| `statement-recon` | `/statement-recon` | Runs the CLI above and helps triage the diff. Relays the report; never writes to the ledger itself |
| `card-tnc-review` | `/card-tnc-review` | Quarterly re-verification of card T&Cs against official issuer sources; hash-skips unchanged sources, sweeps for promos, and keeps the nudge thresholds in `card_optimiser.py`, the PWA meters, and your `card_strategy` rows honest. First run builds `docs/CARD-TNC-LEDGER.md` for your cards |
| `new-alert-source` | `/new-alert-source` | The eight-step checklist for onboarding a new transaction-alert email source (new bank, Shortcut card, supplementary card) end-to-end: parser, Python mirror, parity pins, deploy note. One hard precondition — a real live sample email |

If you cloned this and do not use Claude Code, delete the directory; nothing
references it at runtime.

---

## What you will need to change because it is hardcoded

Grouped by consequence. The first group breaks or leaks if you skip it.

### Must change

| Where | What | If you don't |
|---|---|---|
| `supabase/migrations/0001_init.sql:178` | `is_owner()` compares `auth.jwt() ->> 'email'` to `you@example.com` | The PWA signs in and shows nothing — RLS returns 0 rows, no error |
| `pwa/index.html` — `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` | Both ship as `__PLACEHOLDER__` strings | Until you fill them in the page renders its sample-data preview and never talks to a database |
| `pwa/budget.html` — the same two constants | Same values again | Budget editor points at the wrong project |
| `apps-script/Code.gs:33` | `WEBHOOK_URL` = `https://YOUR-RENDER-SERVICE.onrender.com/webhooks/expense-ingest` | Your bank emails are POSTed at someone else's service |
| `apps-script/Code.gs:794` | `RENDER_SERVICE_ID = "srv-REPLACE-WITH-YOUR-SERVICE-ID"` | The nightly restart targets a service you do not own (it will 401 without his API key, but change it anyway) |
| `apps-script/Code.gs:64` | `GMAIL_QUERY` — DBS/PayLah/UOB/HSBC sender addresses | No emails match; nothing ever ingests |
| `apps-script/Code.gs:192,242,281,340` | `parseDBS` / `parseUOB` / `parseHSBC` / `parseYouTrip` regexes, tied to those exact layouts | Other banks parse to `null` and are silently skipped |
| `hermes-config/USER.md` | Ships only as `USER.md.example`. Fill it in and drop the suffix — the Dockerfile COPYs `USER.md` | The build fails on a missing COPY source; and an unfilled template means the agent knows nothing about you |

### Should change

| Where | What |
|---|---|
| `supabase/migrations/0003_cards_bonus_cap.sql:10-11` | `update ... uob-pref = 600`, `hsbc-revo = 1000` — his card ids |
| `tools/card_optimiser.py:1046,1117-1143` | Steering nudges hardcode the card ids `dbs-yuu`, `uob-pref`, `hsbc-revo`, `dbs-vantage` |
| `tools/card_optimiser.py:1266` | The two-pool bonus tracker special-cases `card_id == "uob-pref"` (UOB Preferred's split S$600 caps) |
| `pwa/index.html:665` | `LIVERY_ID` map: the original deployment's card ids → Muted Card System catalogue ids |
| `pwa/index.html:1428,1443` | The same `uob-pref` two-pool split, and the `dbs-yuu` partner-spend meter, on the card tiles |
| `pwa/index.html` — the sample-data block | Sample cards and transactions, shown in the no-key preview |
| `recon/statement_parsers.py` | DBS/UOB statement layouts, and the supplementary-card attribution rule |
| `hermes-config/MEMORY.md` | Ships as `MEMORY.md.example`; its payment-method examples use the fictional card last-4s |
| `hermes-config/SOUL.md` | Ships as `SOUL.md.example`; agent persona and tone |
| `pwa/manifest.webmanifest`, `pwa/kevin-icon.png` | The app is named "Kevin" |
| `Dockerfile` | `ENV TZ=Asia/Singapore`, and `ARG HERMES_AGENT_SHA` pinning `alhazjm/hermes-agent` — his fork, not upstream NousResearch |
| `render.yaml` | Service name and `region: singapore` |
| `cron/setup-cron-jobs.sh` | SGT schedules and the prompts' tone |
| `skills/*/SKILL.md` | Worked examples use fictional people and categories (Sam, Miso, Shopee). Harmless, but they are prompts the model reads |

Currency is another quiet assumption: SGD is the default in the schema
(`transactions.currency default 'SGD'`), the FX conversion in `Code.gs`
converts *to* SGD, and the PWA formats with `en-SG`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot replies but says it cannot see your budget / never calls a tool | `_sheets_configured()` is False — one of `GSPREAD_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` is unset | Set all four in the Render dashboard; redeploy |
| Webhook accepts transactions you did not make | `WEBHOOK_HMAC_SECRET` was never set, so the public placeholder is your signing key | Set it, redeploy, and check the boot log for "Webhook secret injected into config" |
| Webhook returns 401/403; nothing logs from email | HMAC mismatch — Apps Script's `WEBHOOK_HMAC_SECRET` Script Property differs from Render's env var, or the deploy log never printed "Webhook secret injected into config" | Make both sides byte-identical; redeploy so the placeholder in `config.yaml` is substituted |
| Webhook 404s | Wrong path — `/webhook/` instead of `/webhooks/expense-ingest` | Use the plural form from `Code.gs:33` |
| Bank emails ingest, YouTrip Shortcut emails do not | The two Gmail searches were merged into one query — `from:me` then ANDs over everything | Keep `GMAIL_QUERY` and `SHORTCUT_QUERY` separate |
| Sheets calls fail with 403 | Sheet not shared with the service account | Share it with `client_email` from the JSON, Editor access |
| Nightly export errors with a worksheet-not-found | The `Transactions` or `Budget` tab is missing or renamed | Create both tabs with the exact headers in step 2 |
| PWA signs in, dashboard is empty, no error | `is_owner()` still holds the original email | Re-run the `create or replace function is_owner()` block with your address |
| PWA cannot send the login email | Supabase SMTP not configured, or built-in sender rate-limited | Configure SMTP; check Supabase Auth Logs |
| Budget editor's delete "works" but the row stays | `0002_budget_write_policies.sql` never ran (no DELETE policy) | Run it; the page already reports the 0-rows case |
| PWA's IOU repaid button does nothing | `0005_loans.sql` never ran, or only partly — the status-flip policy is in it | Run `0005` |
| Subscriptions section shows nothing / dismissals do not stick | `0006_sub_overrides.sql` never ran | Run `0006`; the read is guarded, so an absent table is silent by design |
| Cron jobs never fire | Never seeded, or seeded before your edits | `hermes cron list` in the Render shell; if empty, delete `/data/cron/.seeded` and restart |
| Duplicate cron jobs after a re-seed | The seeding script only creates; the marker was deleted without removing the old jobs | `hermes cron remove <id>` the duplicates, then re-seed |
| Cron fires at the wrong hour | Someone converted expressions to UTC, or `TZ` was changed | Expressions are SGT wall-clock against `ENV TZ`; keep the two consistent |
| Docker build fails inside a `RUN python3 /app/patches/...` step | Upstream `hermes-agent` moved and a patch anchor no longer matches | Do not loosen the anchor — pin `HERMES_AGENT_SHA` back, or update the patch's `ANCHOR`/`INJECTION` deliberately |
| Every reply arrives twice (bubble + a chatty summary) | The silence contract broke — the patch or the tool's `assistant_reply_required: false` flags | Check the build log for the v2 patch marker grep |
| A tool loads in tests but the model never calls it | It is missing from the Dockerfile `sed` list — registration alone does not expose it | Add the name to the `sed -i` line anchored on `"send_message",` |
| `pyo3_runtime.PanicException` when running tests | `cffi` not installed | `pip install cffi` |

---

## Migrating an existing sheet (only if you have one)

`scripts/backfill_supabase.py` is a one-time importer: it copies an existing
Google Sheet into Supabase, upserting on natural keys so it is re-runnable,
and seeds `txn_id_counters` so `next_txn_id()` continues each day's sequence
instead of colliding.

```bash
GOOGLE_SERVICE_ACCOUNT_JSON=... GSPREAD_SPREADSHEET_ID=... \
SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \
python scripts/backfill_supabase.py
```

A fresh install does not need it.

---

## Repo docs that are stale (do not follow them blindly)

Listed so you can read them with the right suspicion, not so you fix them
now. `CLAUDE.md` / `AGENTS.md` were rewritten against current reality and are
no longer on this list — they are the best description of the deployment
surfaces and the mistakes that cost real debugging time.

- **`deploy/README.md`** — accurate on Render and Telegram, but its env
  table omits the Supabase pair.
- **`sheets-template/README.md`** — the tab schemas are still the best
  column reference, but "the Sheet is the source of truth" is no longer
  true, and most tabs are not read at runtime any more.
- **`cron/README.md`** — four jobs, not seven; mentions WhatsApp.
- **`ROADMAP.md`** (root) — strategy and the "Things NOT to do" list are
  current; its storage/tool-count asides are not.
- **`README.md`** (root) — reads as an overview, not a runbook. This file is
  the runbook.

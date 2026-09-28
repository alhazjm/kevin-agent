# SETUP — running your own instance

First-run guide for someone who has cloned this repo and wants their own
copy running. Written against the code as it stands — hermes-agent 0.21.0,
Supabase ledger, four email parsers, the PWA, loans, trip pots, six cron
jobs — and re-verified against the tree. Where this guide names a constant
or a function, it was read out of the file; where it would have needed a
line number, it names the symbol instead, so the reference survives edits.

## Read this first

This is one person's deployment, not a product. There is no multi-tenancy:
one Supabase project, one Telegram chat, one Render service, and row-level
security pinned to a specific email address. Running your own copy means
provisioning your own of each and editing the places where the owner's
identity is hardcoded — see
[What is hardcoded](#what-you-will-need-to-change-because-it-is-hardcoded),
which is the section that will actually cost you time if you skip it.

### What you will sign up for

| Account | Sign up at | What it's for | Cost |
|---|---|---|---|
| **GitHub** | github.com | Render builds from a repo you control; CI runs your tests | Free |
| **Supabase** | supabase.com → New project | The ledger (Postgres) and the dashboard's sign-in | Free tier is plenty |
| **Telegram** | The app, then message **@BotFather** | The bot you talk to | Free |
| **OpenAI** | platform.openai.com → API keys | The model (`gpt-5.4-nano`) and voice-note transcription | Usage-based; a few dollars a month at personal volume. **A brand-new account is on Tier 1** (200k tokens/min) — enough for one person, see [Rate limits](#rate-limits) |
| **Render** | render.com | Runs the agent (Docker web service + 1 GB disk) and hosts the dashboard (static site) | **~$7/mo** for the always-on service; the static site is free |

Required only if you want bank emails ingested automatically (skip it and
log by chat instead):

| Account | Sign up at | What it's for | Cost |
|---|---|---|---|
| **Google account with Gmail** | Already have one | Receives the bank alerts and runs the Apps Script that reads them | Free |
| **Node.js + `clasp`** | nodejs.org, then `npm i -g @google/clasp` | Pushes the script from the command line (or paste it into the editor and skip this) | Free |

Optional, only for the Google Sheet backup (section 8):

| Account | Sign up at | What it's for | Cost |
|---|---|---|---|
| **Google Cloud project + service account** | console.cloud.google.com | Lets the agent write to a spreadsheet | Free |

Local tooling, only if you want to run the tests or the recon CLI: Python
3.11 and `pip install pytest gspread google-auth cffi` (`cffi` is a hidden
hard dependency — without it pytest dies at collection with
`pyo3_runtime.PanicException`). The recon CLI additionally wants `pypdf`.

### The two setups

| | You provision | You get |
|---|---|---|
| **Base** (sections 1–7) | Supabase · Telegram · OpenAI · Render | The whole thing: agent, all 37 tools, the PWA dashboard, email ingest, cron |
| **+ Backup** (section 8) | …plus Google Cloud + a Google Sheet | A human-browsable grid of every transaction, rebuilt nightly, on a second provider's free tier |

Start with Base. Nothing in section 8 is required, and you can add it later
without touching anything you already set up.

There is no Sheets-only mode. The Sheet was the database once; the ledger
moved to Postgres for atomic dedup (`on_conflict=idempotency_key` — a unique
constraint, versus the read-then-write race that let a double-fired webhook
double-log real money), atomic id minting via the `next_txn_id()` RPC, and
row-level security for the dashboard's sign-in. Nothing reads the Sheet now.

Budget an afternoon. Expect to need a paid Render tier: the gateway is a
long-lived process that polls Telegram, serves the webhook, and runs cron
in-process, so a free tier that sleeps will not work.

**Two different Google things, easily confused.** *Gmail ingest* runs as an
Apps Script on your own account using `GmailApp` — no Cloud project, no
service account, just the unverified-app consent for your own script reading
your own mail. The *Google Cloud service account* in section 8 exists only
to authenticate to the Sheets API. Email ingest needs no Google Cloud at
all.

Two things this guide will not fabricate: exact dashboard button labels for
Supabase and Render (they move), and any step that could not be verified
from the code in this repo.

### The five surfaces

Nothing you edit is live until it crosses its surface's boundary, and each
one deploys differently.

| Surface | What lives there | How it deploys |
|---|---|---|
| Render web service | The agent (Docker container built from `Dockerfile`) | Push to `main` → auto-build |
| Supabase project | The ledger of record — every table in `supabase/migrations/` | SQL you run by hand in the SQL editor |
| Render static site | The PWA (`pwa/`) | Separate service; same repo, publish directory `pwa/` |
| Google Apps Script | Gmail → webhook ingest (`apps-script/Code.gs`) | `clasp push` + Script Properties set by hand |
| Google Sheet *(optional)* | Nightly read-only export of the ledger | Created by hand; rebuilt by the 3 AM cron |

Every tool is gated on `_sheets_configured()` in
`tools/expense_sheets_tool.py`, which requires only the Supabase pair. Miss
those two and the tools load but are never exposed to the model — the bot
chats happily and cannot log a thing. Miss the Sheets pair and you simply
have no backup export; nothing else changes.

---

## 0. Before your first build

**Fork or push this repo to your own GitHub account.** Render needs a repo
it can read, and you will be committing edits (hardcoded emails, URLs, card
ids) that you do not want to send upstream.

**Create the three memory files.** They ship as templates, because the real
ones carry a name, a household, card last-4s and goals. The Dockerfile COPYs
them under their real names, so a fresh clone fails the build until they
exist:

```bash
cd hermes-config/
cp USER.md.example   USER.md
cp MEMORY.md.example MEMORY.md
cp SOUL.md.example   SOUL.md
```

Fill in `USER.md` (it is all placeholders), skim `MEMORY.md` (mostly system
contract — keep it verbatim if you keep the same tools), and rewrite
`SOUL.md` if you want a different voice and name.

**Then commit them to your fork with `-f`.** All three are gitignored under
their real names (so they can never be pushed to the upstream repo by
accident), which also means a plain `git add .` silently skips them and
`git status` never lists them. Render builds from your fork, and the
Dockerfile COPYs the real filenames, so without this the build fails at
`COPY hermes-config/USER.md`:

```bash
git add -f hermes-config/USER.md hermes-config/MEMORY.md hermes-config/SOUL.md
git commit -m "Add my memory files"
```

If you would rather not carry personal details in a fork's history, the
alternative is to delete the three `hermes-config/*.md` lines from
`.gitignore` in your fork — simpler mental model, but you lose the guard.

> One honest caveat, discovered during the 0.21 upgrade: hermes reads
> `USER.md` and `MEMORY.md` from `HERMES_HOME/memories/`, which `start.sh`
> symlinks to the persistent disk — not from where the Dockerfile copies
> them. So of the three, only `SOUL.md` loads from the image. The templates'
> headers say how to make the other two live (one `cp` in the Render shell).
> The shipped Dockerfile leaves this as-is because fixing it changes agent
> behaviour; it is on the changelog as known-and-not-fixed.

**Verify:** `pytest tests/ -q` → **648 passed**, in a couple of seconds,
with no network.

---

## 1. Supabase — schema, and the RLS email you MUST change

### 1.1 Create the project

Create a new Supabase project (any region; Singapore if you want it near
Render's). From Project Settings → API collect three things:

| Value | Looks like | Used by |
|---|---|---|
| Project URL | `https://<ref>.supabase.co` | Render (`SUPABASE_URL`), Apps Script, the PWA |
| Publishable / anon key | `sb_publishable_...` (older projects: a long `anon` JWT) | The PWA only — public by design |
| Secret / service_role key | `sb_secret_...` — **and** the legacy `service_role` JWT (`eyJ…`) under *Legacy API keys* | Render (`SUPABASE_SERVICE_KEY`, either form) and Apps Script (**legacy JWT only**, see 5.4) — server side only |

The secret key bypasses row-level security completely. It must never appear
in `pwa/*.html`, and those files carry a comment saying so.

### 1.2 Change the owner email BEFORE running the SQL

In `supabase/migrations/0001_init.sql`, the function `is_owner()`:

```sql
create or replace function is_owner() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() ->> 'email', '') = 'you@example.com'
$$;
```

Replace that literal with the email address you will sign into the PWA
with. Every RLS policy calls `is_owner()`, so this one line decides whether
the PWA can read anything at all. `supabase/schema.sql` contains the same
function — change it there too if you paste that file (its header reminds
you).

If you forget: the PWA will let you sign in and then render an empty
dashboard with no error, because RLS returns zero rows rather than a
permission failure. Fix by re-running just the `create or replace function`
block with your address — the policies pick it up immediately.

> **If you later widen it** (two addresses, say), land the change as a new
> numbered migration too. A live `is_owner()` that no migration describes
> is drift, and the next person to run the schema on a fresh project
> silently gets different access rules.

### 1.3 Run the SQL

**The short version: open the Supabase SQL editor, paste all of
[`supabase/schema.sql`](../supabase/schema.sql), run it once. Done.**

That file is every migration concatenated in order — one paste instead of
eight, and if any statement fails the whole thing rolls back. It is
generated, never hand-written: `python supabase/build_schema.py` rebuilds
it, and `tests/test_schema_consolidation.py` fails the suite if it ever
drifts from the migrations.

The numbered files under `supabase/migrations/` remain the source of truth,
and you want them if you are upgrading an existing database rather than
building a new one — run only the ones you have not run yet, in order:

1. `0001_init.sql` — the core schema, `next_txn_id()`, RLS enabled on every
   table with a SELECT-only `owner_read` policy.
2. `0002_budget_write_policies.sql` — insert/update/delete policies on
   `budgets`, so the PWA's budget editor can write.
3. `0003_cards_bonus_cap.sql` — adds `cards.bonus_cap`. Its two `update`
   lines carry the original deployment's card ids; harmless on a fresh DB.
4. `0004_transactions_update_policy.sql` — lets the PWA change a
   transaction's category inline.
5. `0005_loans.sql` — the `loans` table (IOUs) plus the narrow status-flip
   policy the PWA's one-tap "repaid" button needs.
6. `0006_sub_overrides.sql` — `sub_overrides`, the subscription-detector
   verdict table (keys are normalized **category** names, not merchants).
7. `0007_cards_base_mpd.sql` — adds `cards.base_mpd`, a card's flat
   "everything else" earn rate. Same caveat as 0003. Skipping the whole
   file is silent-but-wrong: the column reads as 0 and the month-end
   scorecard over-reports "miles left on the table".
8. `0008_category_meta.sql` — `category_meta`: which budget categories are
   **fixed** monthly bills (subscriptions, insurance, utilities). Fixed
   bills are left out of the over-80% warnings and flagged only when they
   come in over their usual amount. The seed block is an *example*
   (Spotify, Insurance, Utilities) — replace it with your own fixed
   categories; anything not listed is `variable`. Skipping the file is
   safe: everything reads as variable, so the evening review lists your
   subscriptions at 100%.

There is deliberately no seed file for `cards` / `card_strategy` — those
rows describe whichever cards you actually carry (1.4).

All of them are re-runnable (`create table if not exists`, policies via the
idempotent `do $$ … duplicate_object` pattern), so running `schema.sql`
over a database that already has some of them is safe. Migrations are
append-only: never edit one that has already run, and never edit
`schema.sql` by hand — a new schema change is a new numbered file plus a
regenerate, in the same commit.

That is **15 tables** when you are done — `transactions`, `budgets`,
`merchant_map`, `txn_id_counters`, `insights`, `journal`, `webhook_log`,
`cards`, `card_strategy`, `card_nudge_log`, `trip_nudge_log`, `travel_mode`,
`loans`, `sub_overrides`, `category_meta`.

Everything degrades gracefully if you stop early: the loans,
`sub_overrides` and `category_meta` reads are guarded, so an absent table
means the feature is quiet rather than broken.

### 1.4 Seed the tables that are hand-maintained

**Budgets.** The PWA's `budget.html` is the intended editor once you are
signed in. To bootstrap before then, insert directly — one row per category
per month, `YYYY-MM`:

```sql
insert into budgets (category, month, limit_amount) values
  ('Groceries',              '2026-09', 400),
  ('Personal - Food & Drinks','2026-09', 300),
  ('Transport',              '2026-09', 120),
  ('Subscriptions',          '2026-09',  45);
```

Category names are yours to choose; the agent only ever logs into
categories that have a budgets row (it refuses unknown names unless you
name them yourself). A `$0` limit is a real budget (any spend is over);
"no budget set" means no row at all.

**Cards.** The card optimiser stays inert (`status: "setup_required"`)
until both `cards` and `card_strategy` have rows, including a `_default`
sentinel in `card_strategy`:

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
Column meanings are documented inline in `0001_init.sql`.

### 1.5 Auth for the PWA

The PWA signs in with Supabase Auth **email OTP** — `signInWithOtp` sends
the mail, and the page accepts either the 6-digit code or the magic link.
In Authentication settings you need: email auth enabled, your static-site
URL (section 4) in the allowed redirect URLs, and — realistically — your
own SMTP configured, because the built-in sender is heavily rate-limited.
The PWA's own error copy points at the same place.

**Order matters for one setting.** The OTP flow *creates* your user on the
first sign-in, so do the first sign-in (section 4) **before** you turn off
"Allow new users to sign up" — disable it first and your own first OTP is
rejected with "Signups not allowed". Once you are in, turn sign-ups off:
the PWA is single-user and there is no reason to let strangers create
accounts (RLS protects the data either way).

**Verify:** in the SQL editor, `select * from card_strategy where category =
'_default';` returns a row (if you seeded cards), and `select count(*) from
category_meta;` succeeds (it proves `0008` ran).

---

## 2. Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a
   username ending in `bot`.
2. Copy the token (`123456:ABC-DEF...`). That is `TELEGRAM_BOT_TOKEN`.
3. Message your new bot once, so it is allowed to reply to you.
4. Get your own numeric user id: message **@userinfobot**, which replies
   with it. That is `TELEGRAM_ALLOWED_USERS`.

That variable does double duty: `deploy/start.sh` also exports it as
`TELEGRAM_HOME_CHANNEL`, where the six cron jobs deliver. So it must be your
numeric id, not a username.

**Verify:** `https://api.telegram.org/bot<TOKEN>/getMe` in a browser returns
your bot's JSON.

---

## 3. Render — the web service

### 3.1 Create it

Blueprint route: Render dashboard → Blueprints → New Blueprint Instance →
connect your fork. It reads `render.yaml` — Docker runtime, Singapore
region, Starter plan, a 1 GB disk named `hermes-data` mounted at `/data` —
and prompts you for the `sync: false` variables.

Manual route: New → Web Service → your fork → Docker runtime → add a disk
with mount path `/data`, size 1 GB. The disk is not optional: it holds
sessions, memories, cron jobs, the LLM usage log, the cron seed marker, and
the service-account file if you use one.

Region and timezone: the image sets `TZ=Asia/Singapore` and every cron
expression is wall-clock in that zone. For your own zone, change that
`ENV TZ` line in the Dockerfile — do **not** convert the cron expressions
to UTC, and do not add timezone-aware datetimes to the Python (all datetime
code is deliberately naive).

### 3.2 Environment variables

Set these in the dashboard. Everything except `PEHD_LLM_USAGE_DIR` is
`sync: false` in `render.yaml`, meaning Render will not take a value from
the file — you type them in by hand.

| Variable | What it is | Where to get it |
|---|---|---|
| `OPENAI_API_KEY` | Model + STT credentials; `cli-config.yaml` points at `api.openai.com/v1` | OpenAI dashboard → API keys |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | Step 1.1 |
| `SUPABASE_SERVICE_KEY` | The **secret / service_role** key | Step 1.1 |
| `WEBHOOK_HMAC_SECRET` | Shared secret between Apps Script and the gateway. **Not optional** — see below | Make one: `openssl rand -hex 32` |
| `TELEGRAM_BOT_TOKEN` | Bot token | Step 2 |
| `TELEGRAM_ALLOWED_USERS` | Your numeric user id; also becomes `TELEGRAM_HOME_CHANNEL` | Step 2 |
| `PEHD_LLM_USAGE_DIR` | Where per-call LLM usage JSONL is written | Literal `/data/llm_usage` (the one var with a value in `render.yaml`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of the service-account JSON | Section 8 — optional |
| `GSPREAD_SPREADSHEET_ID` | The Sheet id | Section 8 — optional |

> **Do not skip `WEBHOOK_HMAC_SECRET`.** `start.sh` only substitutes it
> into `config.yaml` when it is set; otherwise the literal
> `__WEBHOOK_SECRET_PLACEHOLDER__` stays there and becomes your live signing
> key. That string is published in this repo, so an unconfigured instance
> will accept any request signed with a value anyone can read — and every
> accepted webhook is a write to your ledger. The container prints four
> `WARNING:` lines at boot if you get this wrong.

`render.yaml` is authoritative for the list; `.env.example` has the same
nine with comments.

### 3.3 Deploy

Push to `main` (auto-deploy) or hit Manual Deploy. The build fetches
**only the pinned commit** of `NousResearch/hermes-agent` (`ARG
HERMES_AGENT_SHA` in the `Dockerfile`; three attempts, and a check that the
commit is the one asked for), installs it, copies **six** `tools/*.py`
files in, and applies **two** patches — each of which fails the build
loudly if its upstream anchor moved. A build that dies in one of those
`RUN python3 /app/patches/...` steps means upstream drifted; do not loosen
the anchor to get green — see [UPGRADING-HERMES.md](UPGRADING-HERMES.md).

**If the build fails, Render keeps the previous container running.** You
lose nothing; read the log, fix, push again.

The 37 tool names reach the model through `registry.register()` in
`expense_sheets_tool.py` plus the `expense_tracker` entry in each
`platform_toolsets` list in `hermes-config/cli-config.yaml` — one per
platform (`telegram`, `webhook`, `cron`). A platform whose list omits it
sees none of the tools, and says nothing about it.

**Verify — watch the deploy log for these lines from `start.sh`:**

```
Environment written to /root/.hermes/.env
sessions created on persistent disk
Webhook secret injected into config
OpenAI key injected into stt config
Seeding cron jobs (first run on this disk)...
Cron jobs seeded successfully
Starting Hermes gateway (foreground)...
TELEGRAM_BOT_TOKEN is set: yes
```

`TELEGRAM_BOT_TOKEN is set: NO` means the variable did not reach the
container. "Webhook secret injected into config" missing means
`WEBHOOK_HMAC_SECRET` is unset. A wall of `check_fn … returned False`
warnings after that is normal — upstream's built-in tools whose
requirements you have not met; none are in this config's toolsets. What
matters is that there is **no** such line about the expense tools and no
"Unknown toolset".

---

## 4. The PWA

`pwa/` is a static, bundler-free app: two HTML files, a `vendor/`
directory, an icon, a manifest. It is **not** in the container — the
Dockerfile never copies it. It is served as a **separate Render static
site** from the same repo.

Everything in `vendor/` is committed on purpose: `supabase-2.111.0.js` is
pinned (it used to load `@supabase/supabase-js@2` from a CDN, which meant
executing whatever the CDN served that day on a page that holds an
authenticated session), and `cards.css` / `cards.global.js` are the card
art, synced from their own repo.

### 4.1 Point it at your Supabase

Two files, two constants each — `pwa/index.html` and `pwa/budget.html`:

```js
const SUPABASE_URL = "https://<your-ref>.supabase.co";
const SUPABASE_PUBLISHABLE_KEY = "sb_publishable_...";
```

Use the **publishable / anon** key. It is public by design: it ships in
plain text to every visitor, and what protects the data is RLS plus auth —
which is why step 1.2 matters. The secret key must never appear here.

Both pages have a graceful fallback: opened over `file://`, or with a key
still starting `__`, `index.html` renders sample data and prints "setup:
paste the publishable key into pwa/index.html". That makes a useful offline
preview before you have Supabase wired.

### 4.2 Host it

Render → New → **Static Site** → same repo → publish directory `pwa`, no
build command. Then add the resulting URL to Supabase Authentication's
allowed redirect URLs (the OTP flow passes
`emailRedirectTo: location.origin + location.pathname`).

There is no config file for this in the repo — `render.yaml` describes only
the web service — so it is a dashboard-only setup.

### 4.3 What to expect

- `index.html` — the dashboard. Reads `transactions`, `budgets`, `cards`,
  `travel_mode`, `insights`, `loans`, `sub_overrides`. Sections: monthly
  hero and chart, budget meters, per-card cycle/bonus meters, trip pot,
  IOUs, subscriptions, and the receipt-styled transaction tape.
- `budget.html` — the budget grid editor. Needs the `0002` write policies.

Two deliberate divergences worth knowing before you "fix" them: the monthly
hero and chart **count** `backfill` and pending rows (honest cash-out
totals), while every agent-side aggregate excludes them. And YouTrip
*spends* are excluded from monthly totals everywhere, because the top-up
was already the counted outflow.

**Verify:** sign in with the email you put in `is_owner()`. If the dashboard
loads but every number is zero and the tape is empty, `is_owner()` does not
match your login address — go back to 1.2.

---

## 5. Apps Script — Gmail ingest

This is the only surface that merging a PR does not deploy. It ships with
`clasp push` (or paste-and-save in the editor).

### 5.1 Push the code

```bash
npm install -g @google/clasp
clasp login
cd apps-script/
clasp create --type standalone --title "Expense Tracker"
clasp push
```

Or: script.google.com → New project → paste `Code.gs`, and paste
`appsscript.json` over the manifest (View → Show manifest file).

`clasp create` in a directory that already has a manifest can pull the new
project's default `appsscript.json` over the committed one (timezone and
scopes). If `git status` shows it modified after `clasp create`, run
`git checkout apps-script/appsscript.json` before `clasp push`.

`clasp create` in a directory that already has a manifest can pull the new
project's default `appsscript.json` over the committed one (timezone and
scopes). If `git status` shows it modified after `clasp create`, run
`git checkout apps-script/appsscript.json` before `clasp push`.

### 5.2 Edit the two hardcoded constants

In `apps-script/Code.gs`:

- `WEBHOOK_URL` — change the host to your Render service:
  `https://<your-service>.onrender.com/webhooks/expense-ingest`. Plural
  `/webhooks/`; the path is the route name in `cli-config.yaml`.
- `RENDER_SERVICE_ID` (`srv-...`), only used by the optional nightly
  restart. Your service id is in the Render dashboard URL. Replace it, or
  delete `setupRestartTrigger` / `restartRenderService` if you do not want
  the restart.

### 5.3 The two Gmail searches

There are deliberately **two** queries, not one boolean query:

- `GMAIL_QUERY` — bank senders (DBS, PayLah, UOB, HSBC) plus a subject
  filter.
- `SHORTCUT_QUERY` — `from:me subject:"YouTrip Transaction"`, a self-sent
  iPhone-Shortcut source.

Do not merge them. Gmail's parser is loose about `OR` precedence and a
combined query silently ANDed the `from:` clause over everything, so the
self-sent email never matched. `from:me` is also a **forgery guard**: the
Shortcut body is plain text with no signature; without it, anyone who knows
the inbox address could mail a crafted body and have it parsed, signed by
your own script, and written to the ledger as a real transaction.

There are four parsers, each tied to one email layout: `parseDBS`,
`parseUOB`, `parseHSBC`, and `parseYouTrip` (dispatched on the subject
prefix). A different bank means a new query clause and a new parser. If you
write one, mirror the regexes into `tests/test_email_parser.py` (a
deliberate byte-for-byte Python port) in the same commit —
`.claude/skills/new-alert-source` is the checklist (section 9.2).

### 5.4 Script Properties

Project Settings (gear icon) → Script Properties. All but `RENDER_API_KEY` are read at module load (it is read when the restart fires):

| Property | Why | Required? |
|---|---|---|
| `WEBHOOK_HMAC_SECRET` | Signs `X-Webhook-Signature`; must equal Render's value byte-for-byte | Yes |
| `SUPABASE_URL` | Audit rows go to the `webhook_log` table, which the weekly sweep reads | Yes |
| `SUPABASE_SERVICE_KEY` | **The legacy `service_role` JWT** (Supabase → Settings → API Keys → *Legacy API keys*, starts `eyJ`), **not** an `sb_secret_*` key. Supabase blocks secret keys from browser-like user agents, and Apps Script's `UrlFetchApp` presents one that cannot be changed — the wrong key fails silently into the Sheet fallback (a month of it, once) | Yes |
| `SPREADSHEET_ID` | Fallback audit target (a `WebhookLog` tab the script creates) when the Supabase write fails | Recommended |
| `TELEGRAM_BOT_TOKEN` | FX-failure and degraded-audit alerts | Recommended |
| `TELEGRAM_CHAT_ID` | Where those alerts go — your numeric id | Recommended |
| `RENDER_API_KEY` | Nightly restart (Render → Account Settings → API Keys, `rnd_...`) | Optional |

### 5.5 Triggers

In the editor, run these functions once each (Run ▶ with the function
selected; grant the consent prompts for your own script):

- `setupTrigger()` — checks Gmail every 5 minutes. Required.
- `setupRestartTrigger()` — restarts the Render service at ~04:00 daily.
  Optional; it exists because the gateway's memory ratchets toward the
  512 MB Starter cap over days.

Then enable transaction alert emails in your bank's app, or the pipeline
has no input.

**Verify:** run `debugAudit()` from the editor — it prints every property's
status, does a real `webhook_log` insert (deleted immediately) and a real
Telegram send, and names the exact fix when something is wrong (both paths
are silent otherwise). Then run `testIdempotencyKeyParity()` — it checks
the JS key against values pinned in the Python suite. Then, after a real
transaction, check the execution log and look for a new row in
`webhook_log`. Emails are marked read after processing and there is **no
retry**, which is why the audit row is written *before* the webhook fires.

**Pacing.** The script sends at most two webhooks per five-minute tick,
20 seconds apart; the rest stay unread for the next tick. Each POST is its
own concurrent agent session, and two at once is what exceeded a fresh
OpenAI account's per-minute ceiling. The constants are
`MAX_WEBHOOKS_PER_TICK` and `WEBHOOK_SPACING_MS`.

---

## 6. Cron jobs

`cron/setup-cron-jobs.sh` creates six jobs via the `hermes cron` CLI on the
first container start on a given disk, gated by the marker
`/data/cron/.seeded`. Seeding failures are non-fatal.

| Schedule (container `TZ`) | Job |
|---|---|
| `0 21 * * *` | Evening review: over-80% variable categories, over-usual fixed bills, one insight, completed IOU repayments, journal prompt |
| `0 18 * * 5` | Friday weekly summary + cards this month |
| `0 9 1 * *` | 1st-of-month report + card plan + last month's scorecard |
| `0 22 * * 0` | Sunday sweep for missed transactions (`[SILENT]` when nothing missed) |
| `0 3 * * *` | Nightly Sheet export from Supabase (`[SILENT]` on success; skipped quietly with no Sheet) |
| `0 7 1 1 *` | Jan 1 yearly cold-storage archive into an `Archive-<year>` tab |

Three things worth knowing, all from hermes 0.20+: cron tools are
allow-listed per platform, and the `cron:` entry under `platform_toolsets`
in `cli-config.yaml` is the only thing that gives these jobs their tools;
silence is the literal `[SILENT]`, because an empty response is booked as a
soft failure; and cron loads the memory files into every run. Details in
[cron/README.md](../cron/README.md), including the re-seed procedure for
when you change the roster.

**Verify:** in the Render shell, `hermes cron list` shows six jobs.

---

## 7. Verify the whole thing works

Work through these in order; each isolates a different link in the chain.

1. **Bot is alive.** Telegram → "hi". You get a reply. (Model + token +
   allowed-users all good.)
2. **Tools are visible.** "what's my remaining budget?" — it should call a
   tool and answer with categories. If it says it cannot access your
   budget, `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` is missing.
3. **Manual logging works.** `log $4.20 Test Merchant` (with or without the
   leading slash). Expect a confirmation bubble containing a
   `txn_YYYYMMDD_NNN` id — or a "which category?" question first if the
   merchant is unknown — and exactly **one** message. A second, chattier
   message after the bubble, or a "⚠️ Processing completed but no response"
   bubble, means the silence patch is not doing its job.
4. **Follow-ups still get answered.** Say "thanks". You must get a reply.
   (Silence is bounded to the turn that sent a bubble; this proves it.)
5. **It landed.** Supabase → `select * from transactions order by id desc
   limit 5;` — your test row, with `source = 'manual'`.
6. **The PWA sees it.** Reload the dashboard; the transaction appears in
   the tape.
7. **Email ingest works.** Make a small real card transaction, wait for the
   bank alert, and within ~5 minutes expect: a row in `webhook_log`
   (`webhook_status = 'sent'`), a categorisation bubble in Telegram, and a
   `transactions` row with `source = 'email'`.
8. **The sweep works.** "run sweep_missed_transactions for the last 7 days"
   — `missed_count: 0` is the healthy answer.
9. **A cron job can use its tools.** In the Render shell, `hermes cron
   list`, then `hermes cron run <id of the 03:00 export>`. With a Sheet: the
   tabs rebuild and Telegram stays quiet. Without: it returns
   `setup_required` and Telegram stays quiet. Either proves the cron
   toolset is wired; a Telegram message saying it cannot find a tool means
   `platform_toolsets.cron` is missing.
10. **Loans work.** "Adam owes me $20 for lunch" → a preview, then confirm.
    The PWA's IOU card shows a one-tap repaid button.
11. **The card optimiser is either wired or politely quiet.** "which card
    for Cold Storage?" — a recommendation once `cards` + `card_strategy`
    have rows, or one `setup_required` line until then.

Then delete the test transaction ("undo") and the test loan.

---

## 8. Optional: the Google Sheet backup

**Skip this entire section unless you want it.** Nothing here is required —
the ledger is Supabase, and no tool reads the Sheet at runtime. Two tools
*write* to it: `export_sheet_backup` (the 3 AM cron) and
`archive_year_snapshot` (Jan 1). Without the two environment variables both
return `setup_required` and stay quiet.

What it buys you: a human-browsable grid of every transaction, and a second
copy of the data on a different provider's free tier. What it costs: a
Google Cloud project, a service account, and a spreadsheet with exact
headers — realistically the fiddliest part of the whole setup.

### 8.1 Google Cloud service account

1. console.cloud.google.com → create a project.
2. APIs & Services → enable **Google Sheets API** and **Google Drive API**.
3. IAM & Admin → Service Accounts → create one (no project role needed —
   access is granted by sharing the Sheet with it directly).
4. Keys → Add key → **JSON**. Download the file.

`scripts/setup-google-oauth.sh` walks the same steps and automates them if
you have `gcloud` installed. Google Cloud project ids are globally unique,
so set your own first: `GCP_PROJECT_ID=my-kevin-backup bash
scripts/setup-google-oauth.sh` (without it the script generates one).

Open the JSON and note the `client_email` value
(`something@project-id.iam.gserviceaccount.com`).

### 8.2 The Google Sheet

Create a new spreadsheet with two tabs. The nightly export rebuilds exactly
these two.

**Tab `Transactions`** — header row exactly (12 columns):

```
Date | Merchant | Amount | Currency | Category | Source | Payment Method | Notes | txn_id | telegram_message_id | idempotency_key | Time
```

**Tab `Budget`** — header row exactly:

```
Category | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec
```

Two tabs are created for you later and you should not make them:
`WebhookLog` (by the Apps Script, as its fallback audit target) and
`Archive-<year>` (by the Jan 1 cron; write-once).

Then: copy the spreadsheet ID out of the URL
(`https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`), and
**Share** the sheet with the service account's `client_email` as
**Editor**. Set `GOOGLE_SERVICE_ACCOUNT_JSON` (the whole file's contents)
and `GSPREAD_SPREADSHEET_ID` in Render, and `SPREADSHEET_ID` in the Apps
Script.

**Verify:** the share dialog lists the `...iam.gserviceaccount.com` address
as an Editor — a missing share is the single most common Sheets failure and
it surfaces as a 403. Then "export to sheets" in Telegram rebuilds both tabs.

---

## 9. Optional local tooling (never ships)

### 9.1 The statement-recon CLI

`recon/` diffs DBS and UOB credit-card e-statement PDFs against the ledger
once a month — the bank's ground truth against yours. It exists because the
webhook path can miss transactions, and because a supplementary card that
never emails you enters the ledger only through this ritual, as
`source='backfill'`.

```bash
pip install pypdf                      # first run only
python recon/recon.py samples/DBS-estatement.pdf samples/UOB-estatement.pdf
```

`samples/` is gitignored on purpose — real e-statements carry full card
numbers. The parsers are checksum-gated: if a statement's own totals do not
reconcile, the parser refuses rather than reporting a confident wrong
answer. Full ritual in [STATEMENT-RECON.md](STATEMENT-RECON.md).

### 9.2 The repo-side Claude Code skills

`.claude/skills/` holds three workflows for whoever maintains this repo,
invoked as slash commands in Claude Code and readable as plain checklists
without it: `/statement-recon`, `/card-tnc-review`, `/new-alert-source`.
They never reach the container; delete the directory if you do not want it.

---

## Keeping hermes-agent current

The framework releases every week or two. The pin is one line in the
`Dockerfile`; the weekly [anchor-check workflow](../.github/workflows/anchor-check.yml)
tests the newest release against every assumption this repo makes and
opens an issue saying "safe to bump" or "this drifted"; and
[UPGRADING-HERMES.md](UPGRADING-HERMES.md) is the procedure, with every
check runnable on your own machine. No step depends on the original author.

---

## Swapping the pieces

Telegram, OpenAI and Render are the defaults because they are what this
instance runs on, not because anything is welded to them. They are not
equally easy to replace.

### The model — easy, it is a config edit

```yaml
model:
  default: "gpt-5.4-nano"
  provider: "custom"
  base_url: "https://api.openai.com/v1"
```

Point `base_url` at any OpenAI-compatible endpoint and change `default` to
that provider's model id — MiniMax, GLM/Zhipu, DeepSeek, Together,
OpenRouter, or a local vLLM/Ollama server. Do the same for `stt` if the
provider also does transcription (otherwise leave STT pointed at OpenAI, or
drop voice notes). Claude and Gemini need an OpenAI-compatible shim in front.
Categorisation is not a frontier task; a nano-class model is genuinely
enough, so pick on price. The two build-time patches assume the OpenAI
Responses-style leg that a direct `api.openai.com` URL selects; a different
provider goes through the chat-completions leg, where the silence patch's
anchor still holds but the "codex incomplete" saving it sits in front of
does not apply.

### The host — easy enough, it is a Dockerfile

`render.yaml` is Render-specific, but the `Dockerfile` is not. Anything
that runs a container with (a) a persistent volume mounted at `/data`,
(b) environment variables, and (c) a long-lived process rather than
scale-to-zero will work: Fly, Railway, DigitalOcean App Platform, a plain
VPS with `docker compose`. Re-point the Apps Script's `WEBHOOK_URL` at the
new host, and note the container needs ~512 MB with the shipped config.

### The chat platform — this one is a real port

Telegram is the one piece that is genuinely wired in:

- `_send_telegram_bubble` / `_send_telegram_photo` in
  `tools/expense_sheets_tool.py` are direct Bot API calls.
- The **silence contract** depends on the *tool* delivering the
  confirmation and then returning `assistant_reply_required: false`, which
  a build-time patch keys on to exit the agent loop. A new platform needs
  the same send-then-suppress shape or you get duplicate messages.
- Reply-to-correct resolves an edit back to a row through
  `telegram_message_id`. A platform without stable per-message ids needs a
  different correction flow entirely.
- `TELEGRAM_HOME_CHANNEL` is where all six cron jobs deliver.

Everything above that line — tools, skills, ledger, PWA — is
platform-agnostic.

### The bank parsers — expect to write your own

`apps-script/Code.gs` has four regex parsers for specific banks' exact alert
layouts. Yours will differ. `tests/test_email_parser.py` is a byte-for-byte
Python mirror that must change in the same commit, and
`testIdempotencyKeyParity()` proves the dedup key still matches Python
before you trust it with real money. If you would rather not touch regexes,
skip section 5 and log by chat.

### The currency

SGD is the default in the schema (`transactions.currency default 'SGD'`),
the FX conversion in `Code.gs` converts *to* SGD, and the PWA formats with
`en-SG`. Changing the home currency is those three places plus the
`frankfurter` base currency.

---

## Rate limits

A new OpenAI account is on **Tier 1: 200,000 tokens per minute**. One
webhook ingest is ~14k tokens per call over 2–3 calls; two arriving in the
same minute once exceeded the ceiling and both failed with 429. The Apps
Script's pacing (two posts per tick, 20 s apart) keeps a single-user
deployment under it. Tier 2 (10× the ceiling) unlocks automatically after
$50 of cumulative spend — you only need it if you run a second agent on the
same key.

---

## What you will need to change because it is hardcoded

Grouped by consequence. The first group breaks or leaks if you skip it.

### Must change

| Where | What | If you don't |
|---|---|---|
| `supabase/migrations/0001_init.sql` and `supabase/schema.sql` — `is_owner()` | Compares the login email to `you@example.com` | The PWA signs in and shows nothing — RLS returns 0 rows, no error |
| `pwa/index.html`, `pwa/budget.html` — `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` | Ship as `__PLACEHOLDER__` strings | The pages render their sample-data preview and never talk to a database |
| `apps-script/Code.gs` — `WEBHOOK_URL` | `https://YOUR-RENDER-SERVICE.onrender.com/webhooks/expense-ingest` | Your bank emails are POSTed at a host that does not exist |
| `apps-script/Code.gs` — `RENDER_SERVICE_ID` | `srv-REPLACE-WITH-YOUR-SERVICE-ID` | The nightly restart targets nothing |
| `apps-script/Code.gs` — `GMAIL_QUERY` | DBS/PayLah/UOB/HSBC sender addresses | No emails match; nothing ever ingests |
| `apps-script/Code.gs` — the four `parse*` functions | Regexes tied to those banks' exact layouts | Other banks parse to `null` and are silently skipped |
| `hermes-config/USER.md` | Ships only as `USER.md.example` | The build fails on a missing COPY source |
| Render env `WEBHOOK_HMAC_SECRET` | Must be set | The published placeholder becomes your signing key |

### Should change

| Where | What |
|---|---|
| `supabase/migrations/0003_cards_bonus_cap.sql`, `0007_cards_base_mpd.sql` | `update` lines carrying the original deployment's card ids |
| `supabase/migrations/0008_category_meta.sql` | The example seed block — your fixed categories |
| `tools/card_optimiser.py` | Steering nudges and the two-pool bonus tracker special-case the original card ids (`uob-pref`, `dbs-yuu`, …); the pattern tuples are annotated "replace wholesale with your own issuers' lists" |
| `pwa/index.html` | `LIVERY_ID` map (card ids → card art), the same two-pool split on the card tiles, and the sample-data block |
| `recon/statement_parsers.py` | DBS/UOB statement layouts, and the supplementary-card attribution rule |
| `hermes-config/MEMORY.md`, `SOUL.md` | Ship as `.example`; payment-method examples use fictional card last-4s; persona and tone |
| `pwa/manifest.webmanifest`, `pwa/kevin-icon.png` | The app is named "Kevin" |
| `Dockerfile` | `ENV TZ=Asia/Singapore` |
| `render.yaml` | Service name and `region: singapore` |
| `cron/setup-cron-jobs.sh` | Schedules and the prompts' tone |
| `skills/*/SKILL.md` | Worked examples use fictional people and categories. Harmless, but they are prompts the model reads |
| `.github/workflows/ci.yml` | The scrub gate reads an extended regex from the repository secret `SCRUB_PATTERN` and fails on the canonical repo if it is missing; if you later publish a fork, set your own under Settings → Secrets and variables → Actions |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot replies but says it cannot see your budget / never calls a tool | `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` is unset | Set both in the Render dashboard; redeploy |
| Nightly export never runs, no error either | The Sheets pair is unset, so the export returns `setup_required` and the cron stays silent by design | Nothing to fix unless you want the backup — then section 8 |
| Webhook accepts transactions you did not make | `WEBHOOK_HMAC_SECRET` was never set | Set it, redeploy, check the boot log for "Webhook secret injected into config" |
| Webhook returns 401/403; nothing logs from email | HMAC mismatch between the Script Property and Render's env var | Make both sides byte-identical; redeploy |
| Webhook 404s | Wrong path — `/webhook/` instead of `/webhooks/expense-ingest` | Use the plural form |
| Apps Script audit rows never reach Supabase; sweep reports everything as missed; a 6-hourly "audit degraded" alert | `SUPABASE_SERVICE_KEY` in Script Properties is an `sb_secret_*` key | Use the legacy `service_role` JWT (5.4); `debugAudit()` says so outright |
| Bank emails ingest, YouTrip Shortcut emails do not | The two Gmail searches were merged into one | Keep `GMAIL_QUERY` and `SHORTCUT_QUERY` separate |
| Two bank emails arrive together and both fail with 429 | OpenAI Tier 1 tokens-per-minute ceiling | Keep the Apps Script pacing defaults; see [Rate limits](#rate-limits) |
| Sheets calls fail with 403 | Sheet not shared with the service account | Share it with `client_email`, Editor access |
| PWA signs in, dashboard is empty, no error | `is_owner()` still holds the placeholder email | Re-run the `create or replace function is_owner()` block with your address |
| PWA cannot send the login email | Supabase SMTP not configured, or built-in sender rate-limited | Configure SMTP; check Supabase Auth Logs |
| Budget editor's delete "works" but the row stays | `0002` never ran (no DELETE policy) | Run it |
| PWA's IOU repaid button does nothing | `0005` never ran | Run it |
| Cron jobs never fire | Never seeded, or seeded before your edits | `hermes cron list` in the Render shell; if empty, delete `/data/cron/.seeded` and restart |
| Duplicate cron jobs after a re-seed | The marker was deleted without removing the old jobs | `hermes cron remove <id>` the duplicates, then re-seed |
| Cron jobs run but report "unknown tool"; the nightly export silently stops | `platform_toolsets.cron` is missing from `cli-config.yaml` — cron allow-lists tools per platform and warns about nothing | Add a `cron:` entry containing `expense_tracker` |
| Cron fires at the wrong hour | Someone converted expressions to UTC, or `TZ` was changed | Expressions are wall-clock against `ENV TZ`; keep the two consistent |
| Docker build fails inside a `RUN python3 /app/patches/...` step | Upstream `hermes-agent` moved and a patch anchor no longer matches | Do not loosen the anchor — [UPGRADING-HERMES.md](UPGRADING-HERMES.md) section 5 |
| Docker build fails at `COPY hermes-config/USER.md` (or `MEMORY.md` / `SOUL.md`) | The three memory files are gitignored, so they never reached your fork — `git add .` skipped them | `git add -f` the three files and push (section 0) |
| Docker build fails at the fetch step with HTTP 429 | GitHub rate-limited the build host | Redeploy; the fetch already retries three times |
| Every reply arrives twice (bubble + a chatty summary) | The silence contract broke | Check the build log for the v3 patch marker grep |
| A bubble is followed by "⚠️ Processing completed but no response was generated" | The silence patch is emitting an empty reply instead of `NO_REPLY` | Confirm the patch is at v3 and its marker grep passed |
| "thanks" after a logged expense gets no reply at all | The silence scan is not bounded to the current turn | Confirm the patch is at v3 |
| Telegram answers `/log …` with "Unknown command" | The bundle for that command is missing from `hermes-config/skill-bundles/` | Restore it (or type the message without the slash) |
| A tool loads in tests but the model never calls it | That platform's `platform_toolsets` list is missing `expense_tracker` | Add it |
| Five "SQLite … WAL-reset corruption bug" warnings at boot | The base image's SQLite is old; hermes fell back to the safe journal mode | Harmless; upgrade SQLite in the image if the noise bothers you |
| `pyo3_runtime.PanicException` when running tests | `cffi` not installed | `pip install cffi` |

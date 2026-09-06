# AGENTS.md

Operating manual for coding agents working in this repo — Claude Code,
Codex, Cursor, whatever you are using. It is written so that a model less
capable than the one that wrote it can still ship safely here: follow the
checklists literally, treat the named mistakes as real (each one cost
debugging time at least once), and use the escalation rules instead of
guessing. When this file and your instinct disagree, this file wins.

> This is the single source. `CLAUDE.md` is a short pointer at this
> file, because Claude Code looks for that name — there is nothing to keep
> in sync.

## What this repo is

> **"PEHD"** — you will see it in the env var `PEHD_LLM_USAGE_DIR`, the
> `PEHD_PATCH_TARGET` override, and the `PEHD patch …` marker strings the
> Dockerfile greps for. It is the acronym of the private deployment this
> repo is exported from. Those identifiers are load-bearing (the patch
> scripts, the Dockerfile greps and the usage summariser all key on them);
> leave them as they are.

`kevin-agent` is the **deployment shell** for a personal
expense-tracking agent built on the
[hermes-agent](https://github.com/NousResearch/hermes-agent) framework. It runs on
Render (Singapore, $7/mo) as a Docker container. The agent ingests
DBS/UOB/HSBC bank emails — plus self-sent YouTrip iPhone-Shortcut alerts —
via a Gmail Apps Script → HMAC-signed webhook, accepts manual `/log`
messages and voice notes via Telegram, categorises via OpenAI
(`gpt-5.4-nano`), writes to Supabase Postgres, and talks back via Telegram.
A static PWA dashboard (`pwa/`) reads Supabase directly.

**Supabase Postgres is the ledger of record** — every runtime read and
write goes through `tools/supabase_client.py` (stdlib urllib against
PostgREST; no SDK). The Google Sheet survives as a **read-only nightly
export** (the `export_sheet_backup` tool, 3 AM cron): a human-readable grid
plus the free-tier backup copy. Hand edits to the Sheet are overwritten
nightly by design; nothing reads the Sheet at runtime anymore.
`tools/sheets_client.py` remains as the export/archive writer and the home
of the idempotency-key formula.

### The five deployment surfaces (core mental model)

Nothing you edit here is live until it crosses its surface's boundary, and
each surface deploys differently:

1. **This repo → Render container.** Push to `main` auto-builds the
   Dockerfile. The container is NOT a checkout of this repo — it clones
   upstream `hermes-agent` and `COPY`s only specific files in. A file
   without a COPY line is silently absent in prod.
2. **`apps-script/` → Google Apps Script.** Deployed with `clasp push` (or
   paste-and-save in the editor; Script Properties set by hand there). It
   never ships in the container. If your PR touches `Code.gs`, say so in
   the PR body — merging alone deploys nothing.
3. **`supabase/migrations/` → the Supabase project.** Numbered SQL files
   run BY HAND in the Supabase SQL editor. Merging alone changes no
   schema; a PR that adds a migration must say so in its manual steps.
4. **`pwa/` → a separate Render Static Site** (publish directory `pwa/`,
   no build step). Deploys automatically on merge; the container never
   copies it. It holds only the PUBLIC publishable Supabase key — the
   service key must never appear there (RLS + email OTP guard the data).
5. **The Google Sheet** — backup surface only. `Transactions`/`Budget`
   tabs are rebuilt nightly by `export_sheet_backup`; `Archive-<year>`
   tabs are written once a year by `archive_year_snapshot`.

### How a transaction flows

```
Bank email → Gmail → Apps Script (every 5 min, two searches: bank query
+ YouTrip-Shortcut subject query):
  parse (regex; DBS / UOB / HSBC / YouTrip-Shortcut) → FX-convert non-SGD
  via frankfurter.dev (stamps "orig: …" into notes) → audit row to the
  Supabase webhook_log table (durable, BEFORE send; Sheet WebhookLog tab
  is the fallback) → HMAC-sign (X-Webhook-Signature) →
  POST /webhooks/expense-ingest
    → hermes webhook route renders the prompt in cli-config.yaml
    → gpt-5.4-nano + expense-ingest skill (slim webhook subset of
      expense-tracker) picks a category
      (multi-category list → MerchantMap → judgment → ask — the HOME
       category only; trip routing happens deterministically INSIDE
       log_expense, see "Travel mode + trip pots";
       resolve_category snaps proposals onto the canonical budgets list
       and refuses unknowns unless the USER named them — create_category
       is opt-in, the phantom-🍜 fix)
    → log_expense: atomic idempotency dedup (on_conflict=idempotency_key)
      → insert with txn_id from the next_txn_id() RPC → Telegram bubble
      → link message_id → fire card-cap + trip-bucket nudge hooks
    → tool returns assistant_reply_required:false → patched agent loop
      exits silently (no duplicate message)
```

Manual path: user texts "/log $30 IKEA" → same skill flow with
`source="manual"`. Cron path: 6 jobs (see `cron/setup-cron-jobs.sh`) invoke
skills on SGT schedules and deliver to Telegram.

## Repo layout

```
Dockerfile                     # Render build — see "Build & deploy reference"
render.yaml                    # Render Blueprint for the web service (env-var list lives here)
deploy/start.sh                # Container entrypoint; writes .env, seds placeholders, links /data, prunes sessions, runs hermes
deploy/patches/                # TWO build-time patches applied to hermes-agent source (three retired at 0.20.6; see Build & deploy)
apps-script/                   # Gmail Apps Script (bank + Shortcut email → webhook); deployed via clasp, not the container
cron/setup-cron-jobs.sh        # Creates the 6 hermes cron jobs (seeded once per /data disk by start.sh)
docs/                          # SETUP.md (cloner guide), UPGRADING-HERMES.md (move the upstream pin
                               #   yourself), HERMES-0.20-MIGRATION-NOTES.md, the two RFCs, STATEMENT-RECON.md
hermes-config/
  cli-config.yaml              # Hermes gateway config (model, stt, webhook route, platform toolsets)
  USER.md.example              # Tier-2 identity template (you, your household, payment methods) — the real USER.md is gitignored
  MEMORY.md.example            # Tier-2 system facts template (schema, webhook shape, enums)
  SOUL.md.example              # Tier-2 agent persona template (tone, principles)
  skill-bundles/*.yaml         # /log /undo /budget /summary — the gateway rejects unknown slash-commands since 0.20.x; a bundle registers one (ships to /root/.hermes/skill-bundles/)
skills/                        # RUNTIME skills (ship to /root/.hermes/skills/)
  expense-tracker/SKILL.md     # Main skill (v5.10.1): categorisation, travel routing, trips/pots, lending, silence contract
  expense-ingest/SKILL.md      # Webhook-only SUBSET of expense-tracker (v1.0.1) — the only skill the ingest route loads; mirror-locked to it
  budget-manager/SKILL.md      # Cron-driven budget warnings / reallocation (v3.1.0)
  weekly-summary/SKILL.md      # Friday + 1st-of-month summaries (v3.1.0)
  card-optimiser/SKILL.md      # Cap tracking, card recs, nudges, scorecard (v1.5.0)
.claude/skills/                # REPO-side Claude Code skills (never ship): /statement-recon,
                               #   /card-tnc-review, /new-alert-source
tools/
  supabase_client.py           # ALL PostgREST I/O — the ledger of record's only gateway
  sheets_client.py             # Sheet export/archive writer + idempotency formula + legacy gspread helpers
  expense_sheets_tool.py       # ALL tool registration (37 tools) + Telegram send helpers
  card_optimiser.py            # Card logic + post-cap nudge hook (no registration here)
  travel_mode.py               # Trip routing, [trip:]/[bucket:] writers + bucket nudge hook (no registration here)
  loans.py                     # IOU logic: create/repay/offset-sweep (no registration here)
supabase/migrations/           # 0001..0008 numbered SQL — THE schema reference (run BY HAND in the SQL editor)
pwa/                           # Static PWA dashboard (separate Render Static Site; never in the container)
recon/                         # Local statement-recon CLI (pypdf, checksum-gated parsers) — never ships
scripts/                       # setup-google-oauth.sh (optional Sheet backup), summarize_llm_usage.py
samples/                       # GITIGNORED — real e-statement PDFs live here, never committed
tests/                         # 648 tests across 10 files; see "Testing"
LICENSE                        # MIT
CHANGELOG.md                   # Dated, human-readable
.github/workflows/             # ci.yml (pytest + scrub gate) and anchor-check.yml (weekly upstream test)
conftest.py                    # Repo root; sys.path fix + stubs tools.registry (framework-injected at runtime)
```

## The failure catalogue — named mistakes and the rule that prevents each

These are the mistakes a capable-but-unfamiliar model actually makes here.
Check your diff against this list before every commit.

### Build & ship

- **M1 — The Silent COPY Miss.** You add a file, imports look right, tests
  pass, and the file simply doesn't exist on Render because the Dockerfile
  never COPYs it. This really happened with `card_optimiser.py`.
  *Rule: every new file that must exist in prod gets a COPY line in the
  Dockerfile in the same commit. Verify: each `tools/*.py` (except
  `__init__.py`) appears in a `COPY` line.*
- **M2 — The Ghost Tool.** You register a tool in Python and the LLM never
  sees it. Until the 0.20.6 bump this was the Dockerfile `sed` injection
  into upstream `toolsets.py`; that anchor (`"send_message",`) no longer
  exists — upstream deleted the agent-callable `send_message` tool and the
  whole `messaging` toolset in v0.16.0 — and the sed was silently doing
  nothing (a `sed` address that matches nothing still exits 0). The sed is
  gone. The mechanism is now `platform_toolsets`, and the trap moved with
  it: **a platform whose `platform_toolsets` list omits `expense_tracker`
  sees NONE of our tools, silently.** That is exactly how cron broke at
  0.20.x — the scheduler now resolves tools per platform, and with no
  `platform_toolsets.cron` entry every job runs with zero expense tools and
  prints no warning at all.
  *Rule: a tool name must appear in THREE places in the same commit —
  (1) `registry.register(name=…)` in `expense_sheets_tool.py`, (2) a
  SKILL.md that tells the LLM when to call it, (3) tests — AND the toolset
  name `expense_tracker` must be present in EVERY `platform_toolsets.<platform>`
  list that should see it (`telegram`, `webhook`, `cron`). Adding a new
  platform means adding that entry. Update the tool count here (currently
  **37**: 22 core + 6 card + 5 travel + 4 loans).*
- **M3 — The Blind SHA Bump.** Bumping `HERMES_AGENT_SHA` without checking
  that the upstream anchor strings still exist. The build fails loud by
  design — never "fix" that by loosening an anchor.
  *Rule: follow the "hermes-agent SHA bump" checklist below, always.*
- **M4 — The Placeholder Cleanup.** "Tidying" `cli-config.yaml` and removing
  `__WEBHOOK_SECRET_PLACEHOLDER__`. hermes YAML cannot expand env vars;
  `start.sh` sed-replaces that placeholder at container start. Removing it
  breaks all webhooks. *Rule: the placeholder is load-bearing. Any new
  config value that needs an env var gets its own placeholder + a sed line
  in `deploy/start.sh`.*
- **M5 — The UTC "Fix".** Converting cron expressions to UTC or adding
  timezone conversion to Python. The image sets `TZ=Asia/Singapore`; cron
  expressions are SGT wall-clock; ALL Python datetime code is deliberately
  naive (`datetime.now()`, no zoneinfo anywhere). *Rule: never introduce
  UTC conversion or tz-aware datetimes. In Apps Script, never round-trip
  dates through `new Date()` + `toISOString()` — that shifted DBS txn
  dates a day back once; `formatDBSDate` does string arithmetic on purpose.*
- **M6 — The Local Install Trap.** Looking for an install script to run
  in the Render shell. There is none. *Rule: the container's install path
  is the Dockerfile itself; changing what is installed means a rebuild.*

### Code (tools/)

- **M7 — The Decorator Mirage.** Assuming a `@register_tool` decorator or a
  `tools/registry.py` in this repo. Neither exists. Registration is an
  imperative call at module top of `expense_sheets_tool.py`; `tools.registry`
  is injected by hermes-agent at runtime and stubbed by `conftest.py` in
  tests. *Rule: copy the exact four-part pattern in "The tool registration
  pattern" below. All registration lives in `expense_sheets_tool.py` only.*
- **M8 — The Hard-Coded Column.** Writing `ws.update_cell(row, 9, …)` or
  indexing rows positionally. This now applies to the Sheet EXPORT path
  (sheets_client) — backup tabs may carry the legacy 8-column layout, and
  header-name lookup via `_get_column_index(ws, header)` (1-indexed,
  `None` if absent → degrade gracefully, never throw) is what keeps every
  layout working. On the Supabase side the equivalent rule holds: read
  paths return Sheet-shaped dicts keyed by header name; never assume
  positional order, and remember "row numbers" there are `transactions.id`
  PKs. The static `TRANSACTION_COLUMNS` map is a last-resort fallback,
  not a primary path.*
- **M9 — The Duplicate Bubble.** Half-implementing the silence contract.
  Tools that deliver their own Telegram message (`log_expense`,
  `log_expense_pending`, `render_budget_chart`) must return, ON THE SUCCESS
  PATH ONLY, all three of `assistant_reply_required: false`,
  `assistant_reply_policy` (the verbose STOP string), and `note`
  ("DUPLICATE MESSAGE WARNING…"), AND repeat the empty-reply instruction in
  the tool's schema `description`, AND the skill must restate it. A small
  model needs all of them; the build-time patch keys on the literal substring
  `"assistant_reply_required": false` in the tool result. *Rule: if you add
  or refactor a Telegram-sending tool, replicate every layer. Never set the
  flags on the duplicate/early-return path (it sends no bubble).*
- **M10 — The Top-Level Import Loop.** `expense_sheets_tool` imports
  `card_optimiser`/`travel_mode`, and those import `_send_telegram_bubble`
  back from `expense_sheets_tool`. Any of these at module top = circular
  import. *Rule: cross-module reach-backs are imported INSIDE the function
  (the existing lazy imports are deliberate — keep the pattern).*
- **M11 — The Helpful DRY-Up.** "Deduplicating" `_parse_date`, `_as_float`,
  `_already_nudged`, `_ensure_nudge_log` between `card_optimiser` and
  `travel_mode`. Same names, DIFFERENT signatures and dedup keys
  (card: `(cycle_window, card_id, category, threshold)`; travel:
  `(trip_label, bucket, threshold, budget_at_nudge)`). *Rule: this
  duplication is deliberate; don't merge it. Shared logic lives in
  `sheets_client` only when semantics are actually identical
  (`_get_column_index`, `_is_pending`, `get_spreadsheet`).*
- **M12 — The Raising Nudge.** Letting `maybe_send_post_cap_nudge` or
  `maybe_send_trip_bucket_nudge` propagate an exception. They run inside
  `handle_log_expense`; a raise breaks core expense logging. *Rule: nudge
  hooks wrap their ENTIRE body in `try/except → {"sent": False, "reason":
  "exception", …}`, and the caller wraps them again. Keep both layers.*
- **M13 — The Notes Bulldozer.** Treating the `Notes` column as free text.
  It carries FOUR structured encodings: `[bucket:X]` prefix (travel-bucket
  accounting is derived ENTIRELY from parsing this — there is no bucket
  column), `[trip:<label>]` (links a YouTrip top-up to its trip; the
  derived funded pot is the sum of tagged top-ups — the PWA's
  `TRIP_TAG_RE` is the reader twin), `orig: <CUR> <amt> @ <rate>` FX
  traces (the travel-mode activation signal), and — in
  `card_strategy.notes` — the `||PREV:` promo snapshot that lazy reversion
  parses. *Rule: mutate bucket tags only via `_apply_bucket_tag` /
  `set_trip_bucket`; trip tags only via `_apply_trip_tag` /
  `link_topup_to_trip` (the ONLY writer); never reorder or strip Notes
  content; writer and reader regexes — including the PWA's — must change
  together.*
- **M14 — The Backfill Contaminator.** Including `source="backfill"` or
  `UNCATEGORIZED` rows in analytics. Backfill rows are statement imports
  (a supplementary card that never emails you); counting them corrupts subscription-creep,
  card-cycle spend, trip spend, and efficiency review. *Rule: every
  agent-side aggregate filters out `Source == "backfill"` and
  `_is_pending(row)`. `source` is a closed enum: `email | manual |
  backfill` — never invent values. `UNCATEGORIZED` is defined once in
  `sheets_client.PENDING_CATEGORY` (imported by `supabase_client`) plus a
  separate `expense_sheets_tool._PENDING_CATEGORY` — the two must stay
  equal. Known, deliberate divergence: the PWA's monthly hero/chart COUNT
  backfill and pending rows (honest cash-out totals, a deliberate 2026-07-31
  decision) — don't "fix" either side to match the other.*

### Sync contracts (places that must change together)

- **M15 — The Second Manual.** Creating a separate `CLAUDE.md` body "to
  help Claude Code" and then letting the two drift. This repo has ONE
  operating manual (this file); `CLAUDE.md` is a ten-line pointer at it.
  *Rule: edit `AGENTS.md` only. Never paste its contents anywhere else.*
- **M16 — The Split-Brain Parser.** Editing the email regexes in
  `apps-script/Code.gs` without `tests/test_email_parser.py` (a deliberate
  Python port of the same regexes, byte-for-byte), or vice versa. *Rule:
  they change together in the same commit, and the change ships only after
  `clasp push` (note it in the PR body).*
- **M17 — The Idempotency Drift.** Changing
  `sheets_client._compute_idempotency_key` (the single source of truth —
  `supabase_client` imports it, never copies it) or
  `Code.gs::computeIdempotencyKey` independently. The key is
  `sha256("{date}|{MERCHANT.strip().upper()}|{amount:.2f}|{payment_method.strip()}")`
  with an OPTIONAL fifth `|{time}` field appended only when a transaction
  time exists (the time-in-key fix for same-day same-amount purchases —
  the KOPITIAM $7.80 incident, 2026-07-25; time-less inputs stay
  byte-identical to the legacy 4-field form so old keys remain valid).
  First 16 hex chars, byte-identical in Python and JS (the JS `& 0xFF`
  mask is what makes signed bytes match `hexdigest()`). Drift silently
  breaks dedup of real money and makes the sweep report every email as
  missed. *Rule: change both sides together, regenerate the pinned parity
  tables in `TestIdempotencyKeyParity`
  (`tests/test_expense_sheets_tool.py`) and `testIdempotencyKeyParity()`
  (Code.gs), and run the latter manually in the Apps Script editor after
  deploy. This is also an ask-first change (see escalation rules).*
- **M18 — The Tier-2 Dump.** Putting transactions, insights, budget numbers,
  or session history into `USER.md` / `MEMORY.md` / `SOUL.md`. `SOUL.md`
  loads into the system prompt EVERY turn; `MEMORY.md`/`USER.md` are meant
  to (see "Memory files" for why the COPY'd copies currently don't) and must
  be sized as if they do. *Rule: respect each file's "what does NOT belong
  here" list; MEMORY.md past ~200 lines or SOUL.md past ~60 is a bug.*
- **(unnumbered) The subscription-detector three-way parity.** The
  billing-behaviour thresholds (≥2 distinct months; exactly 1 charge per
  month; day-of-month spread ≤4; every charge ≥ $0.50; consecutive step ≤
  max(10%, $0.05); grouped by CATEGORY, never merchant — bank strings
  carry per-charge refs) live in THREE places:
  `supabase_client.detect_subscription_creep`, its `sheets_client` twin,
  and the PWA's `calcSubs`. Change all three together. `sub_overrides`
  keys are normalized CATEGORY names; `exclude` verdicts are honoured by
  both the PWA and the supabase twin (the sheets twin takes them as a
  parameter — it can't reach the table); `include` (declared no-email
  subs, amounts cross-checked against the budget row) is PWA-only today.
- **(unnumbered) The ingest-skill mirror.** `skills/expense-ingest/SKILL.md`
  is a slim SUBSET of `skills/expense-tracker/SKILL.md`, and it is the ONLY
  skill the `expense-ingest` webhook route loads
  (`cli-config.yaml → routes.expense-ingest.skills`). Why: upstream
  `gateway/platforms/webhook.py` injects the WHOLE body of the route's
  first skill into every webhook prompt, and expense-tracker (~10k tokens)
  rode along in every API call of every ingest — 3–5 calls per
  transaction. The token diet only stays honest if the two files agree.
  *Rule: any edit to the categorisation order, the multi-category list,
  the merchant heuristics, the silence contract (bubble_sent → EMPTY
  reply), the webhook step list, or the travel / YouTrip routing rules in
  expense-tracker MUST be mirrored into expense-ingest in the same commit
  (and vice versa), with both versions bumped. Everything else (lending,
  undo/edit, receipts, reports, journal, sweep, budgets, manual /log) is
  deliberately absent from expense-ingest — don't "complete" it.*

## Conventions

### Python (tools/)

- Layers: `supabase_client.py` is the only module that touches PostgREST
  (the ledger of record); `sheets_client.py` is the only module that
  touches gspread (export/archive path + the idempotency formula);
  `expense_sheets_tool.py` is the only module that registers tools;
  `card_optimiser.py` / `travel_mode.py` / `loans.py` are pure logic
  wrapped by handlers. Keep new code in the layer it belongs to. Ledger
  mutations from feature modules route through `supabase_client` functions
  (`edit_transaction`, `find_transaction_by_id`) — never raw writes.
  `supabase_client` deliberately mirrors `sheets_client`'s public surface
  (same signatures, same return shapes, Sheet-shaped row dicts) — it was
  the migration seam; keep the mirror honest when adding functions.
- Naming: public domain functions `snake_case`; private helpers `_prefixed`
  (never registered, never exposed); handlers `handle_<toolname>`; schema
  constants `<TOOLNAME>_SCHEMA`; module constants `UPPER_SNAKE`
  (tab names like `TRANSACTIONS_SHEET` — never hard-code the string).
- Type hints: modern PEP 604 (`int | None`, `list[dict]`) on params and
  returns; the gspread worksheet arg stays untyped (`ws`).
- Docstrings and comments explain **why**, and record operational history
  ("Historical 503s from … caused the agent to crash mid-cron"). When you
  fix a footgun, write the footgun into the docstring. Section banners:
  `# --- Section name ---`.
- **No logging framework, no prints.** Observability = fields on the
  returned dict (`reason`, `error`, `note`). Add a field, not a logger.
- **Handlers never raise.** Expected failures are dicts with a `status`
  field; exceptions are caught locally and converted
  (`{"status": "error", "message": f"...: {exc}"}`).
- Retries: `supabase_client._request()` retries {429, 500, 502, 503, 504}
  and network errors with 0.5/1/2s backoff (3 attempts, 20s timeout),
  raises on persistent failure (handlers catch → `{"status": "error"}`);
  `get_spreadsheet()` retries 429/5xx the same way on the Sheet path;
  `_retry_link` retries the Telegram-link step. Copy these; don't invent
  new retry loops.
- Sheets I/O idioms (the EXPORT path only now):
  `append_row(row, value_input_option="USER_ENTERED")` (always
  USER_ENTERED, never RAW); `update_cell` for single cells;
  `get_all_records()` for dict rows; `col_values(idx)[1:]` skips header;
  row-number arithmetic is `i + 2` (header + 0-index) — respect the
  existing comments. `get_client()` is `@lru_cache`. Supabase reads are
  deliberately uncached per call — staleness mid-conversation is the
  constraint to think about before adding any cache.
- Month/date handling: months are `"YYYY-MM"` strings; use `if not month:`
  (NOT `is None` — the LLM sends empty strings; `"".split("-")[1]` was a
  real crash). Robust parsing via the module's `_parse_date`.
- Telegram sends: reuse `_send_telegram_bubble` / `_send_telegram_photo`
  in `expense_sheets_tool.py` (stdlib urllib, `{"ok": bool}` return shape —
  note: helpers use `ok`, tools use `status`; don't conflate).

### The tool registration pattern (copy exactly)

Every tool is a four-part block in `expense_sheets_tool.py`:

```python
MY_TOOL_SCHEMA = {
    "name": "my_tool",
    "description": "…when to call it; repeat any silence contract here…",
    "parameters": {
        "type": "object",
        "properties": {"amount": {"type": "number", "description": "…"}},
        "required": ["amount"],       # written explicitly even when []
    },
}

def handle_my_tool(args: dict, **kwargs) -> str:     # **kwargs is mandatory
    amount = float(args.get("amount", 0))            # coerce defensively
    result = supabase_client.my_tool(amount=amount)  # or lazy-import module
    return json.dumps(result)                        # ALWAYS a JSON string

registry.register(
    name="my_tool",
    toolset=TOOLSET,                 # always "expense_tracker"
    schema=MY_TOOL_SCHEMA,
    handler=handle_my_tool,
    check_fn=_sheets_configured,     # always this gate
)
```

Card/travel/loans handlers lazy-import their module inside the handler
(`from tools import card_optimiser`) — see M10. Also list the tool in the
module docstring at the top of `expense_sheets_tool.py`. `_sheets_configured`
(the universal `check_fn`, name kept from the sheet era) gates on the
Supabase pair only; `_sheet_export_configured()` separately gates the two
Sheet export tools, so Google Cloud is optional for a cloner.

### Return-shape contracts (the skill layer branches on these)

- Core vocabulary: `{"status": "ok" | "error" | "duplicate" | "preview" |
  "not_found" | "no_match" | "match" | "created" | "exists" | "updated" |
  "unknown_category" | "setup_required"}` plus a human `message` on
  non-ok.
- Category guard (the phantom-🍜 fix): writers pass proposals through
  `resolve_category` — emoji-stripped, case-insensitive snap onto the
  canonical budgets list; unknowns return `status: "unknown_category"`
  with `closest`/`existing_categories` and write NOTHING unless the
  caller passes `create_category=True` (only when the USER named the
  category). Never bypass the guard.
- Card optimiser: every public function starts
  `gate = _check_setup(); if gate: return gate` →
  `{"status": "setup_required", "message": …}` until the `cards` +
  `card_strategy` tables (incl. the `_default` sentinel row) exist. The skill
  surfaces that message in one line and stops — preserve the gate.
- Travel mode does NOT use `status` uniformly: `get_active_travel_mode`
  returns an `active: bool`; `get_trip_budget_status` can return
  `no_travel_mode_tab` / `not_found` / `no_active_trip`. Don't "normalise"
  these — the skill prompts key on them.
- Nudge hooks return `{"sent": bool, "reason": str}` — a different shape
  from tools, on purpose.
- Field names are API: `txn_id`, `idempotency_key`, `telegram_message_id`,
  `bubble_sent`, `assistant_reply_required`, `new_category_created`,
  `txn_ids`, `pending_review`. Renaming one breaks skill prompts silently.
- `status="duplicate"` short-circuits BEFORE the bubble send — keep the
  early return.

### Tests

- Run: `pip install pytest gspread google-auth cffi` once, then
  `pytest tests/ -q`.
  `cffi` is a hidden hard dependency — `pyo3_runtime.PanicException` at
  collection means it's missing.
- Structure: class per unit (`TestLogExpense`, `TestCycleWindow`), plain
  classes + bare `assert` (no unittest.TestCase), methods named
  `test_<behavior>` ("test_duplicate_skips_bubble_and_returns_early").
  Parametrize is rare; write named methods for variants.
- **Import the SUT inside the test body**, not at module top — conftest's
  registry stub and the autouse `mock_env` fixture (defined per test
  MODULE, not in conftest) must be in place first.
- Mocking is `unittest.mock` (`patch`, `patch.object`, `MagicMock`) — the
  suite never uses pytest's `monkeypatch`. There is NO FakeWorksheet class;
  configure MagicMock worksheets with only the gspread methods the code
  path calls. Copy the nearest existing helper (`_mock_ws`, `_make_records`,
  `TestSweepMissedTransactions._make_spreadsheet`) instead of inventing one.
- Patch where the name is LOOKED UP, not where it's defined:
  `patch("tools.sheets_client.get_spreadsheet")` for sheets_client
  internals; `patch.object(card_optimiser, "get_spreadsheet", …)` for
  modules that imported the symbol into their namespace.
- Functions that call `ws.row_values(1)` more than once need an ordered
  `side_effect` list — one entry per consumer, each annotated with a
  comment naming the consumer. Too few entries = `StopIteration`.
- Freezing time: patch the module's `datetime`, set `mock_dt.now.return_value`
  AND `mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)` so plain
  construction still works.
- gspread specifics: build `APIError` via `__new__` + a mock `.response`
  (its real `__init__` needs a live Response); simulate a missing tab with
  `ss.worksheet.side_effect = gspread.WorksheetNotFound("X")`.
- Handlers return JSON strings — wrap every handler call in `json.loads`.
  Handler tests pin two things: exact downstream kwargs
  (`assert_called_once_with`, including `""`→`None` coercions) and JSON
  round-trip.
- The fake registry accepts anything silently — a mis-registered tool will
  NOT fail tests. That's what `platform_toolsets` in `cli-config.yaml` and
  the boot log (no "Unknown toolset", no check_fn warning for our tools) are for.
- The canonical 11-column header for fixtures:
  `Date, Merchant, Amount, Currency, Category, Source, Payment Method,
  Notes, txn_id, telegram_message_id, idempotency_key`. Legacy-layout tests
  (8- and 10-column) are first-class citizens — anything touching
  Transactions columns needs one.
- `tests/fixtures/*` are reference artifacts only (not imported); real test
  inputs are inline `BODY` constants and `_make_*` helpers.

### Skill files (skills/*/SKILL.md)

- Skills are pure markdown read by the LLM at runtime; they are the
  behavior spec. Structure: YAML frontmatter (semver `version`) → "When to
  Use" trigger list → "HARD RULES" (numbered, first) → flows with worked
  examples in fenced blocks → "Important Notes".
- Version bump on every behavioral edit: patch = wording, minor = new
  flow/tool reference, major = changed tool contract or flow semantics.
- Behavioral invariants that must survive ANY skill edit:
  1. `bubble_sent: true` → EMPTY assistant reply (stated at every call site).
  2. `learn_merchant_mapping` fires only AFTER a user correction — never on
     first sight, never for multi-category merchants (supermarkets,
     marketplaces, 7-Eleven default-then-reply instead).
  3. Telegram formatting: short bullet lines; NEVER markdown tables or
     `#` headers (they render as broken raw text).
  4. Preview-then-confirm for every mutation of money state (budget
     changes, undo, receipt logging).
  5. Category names cited exactly as stored; the live category list lives
     in the budgets table (the skill text still says "Budget tab" — the
     nightly export keeps the tab in sync), never in skill files.
  6. Cron-invoked flows stay silent when there's nothing to say
     (`setup_required` sections skipped without warnings — that wording
     lives in card-optimiser's SKILL.md and the cron prompts; count-zero
     silence for sweeps lives in expense-tracker).
- budget-manager and weekly-summary are prompt variants over the
  expense-tracker toolset — no tool files of their own. Travel-mode,
  trip-pot, YouTrip-spend, and lending rules live INSIDE expense-tracker
  (there is no separate travel or loans skill).
- expense-ingest is a SUBSET of expense-tracker (categorisation order,
  multi-category list, heuristics, silence contract, webhook steps,
  travel/YouTrip rules — nothing else), loaded only by the webhook route
  as a token diet. It is mirror-locked to expense-tracker — see "The
  ingest-skill mirror" in Sync contracts; edit both, bump both versions.
- **Slash-commands the user types need a bundle.** Since 0.20.x the
  gateway answers any slash-command it does not recognise with "Unknown
  command" and never forwards it to the model — there is no config switch.
  `/log`, `/undo`, `/budget`, `/summary` work only because
  `hermes-config/skill-bundles/<name>.yaml` exists for each (a bundle
  registers `/<name>`, injects the listed skill bodies plus the user's
  text, then runs a normal turn; bundles are dispatched BEFORE skills).
  If a SKILL.md starts teaching a new `/command`, ship its bundle in the
  same commit and add it to `EXPECTED_COMMANDS` in
  `tests/test_skill_bundles.py`. Found in production 2026-09-04.
- The `.claude/skills/` skills are a DIFFERENT species: repo-side Claude
  Code workflows (/statement-recon, /card-tnc-review, /new-alert-source)
  that never ship to the container and are run by a human from the repo.
- If a skill references a tool, that tool must be registered in
  `expense_sheets_tool.py` and reachable via `platform_toolsets`. If a cron prompt in `setup-cron-jobs.sh` encodes a skill behavior
  (e.g. "skip the cards section on setup_required"), keep prompt and skill
  consistent in the same commit.

### Apps Script (apps-script/)

- Deployed with `clasp push` (or paste-and-save); Script Properties
  (`WEBHOOK_HMAC_SECRET`, `SPREADSHEET_ID`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
  `RENDER_API_KEY`) are set by hand in the editor. `WEBHOOK_URL` is a
  hard-coded const in Code.gs (path `/webhooks/expense-ingest` — plural;
  Code.gs is the source of truth for the URL).
- TWO Gmail searches, deliberately not one boolean query: `GMAIL_QUERY`
  (bank senders + subject filter) and `SHORTCUT_QUERY`
  (`from:me subject:"YouTrip Transaction"` — the self-sent
  iPhone-Shortcut source; dispatch to parseYouTrip keys on the subject
  PREFIX, and `from:me` is a forgery guard: without it anyone who knows
  the inbox address could mail a crafted body and have it parsed,
  HMAC-signed, and logged as a real transaction). Gmail's parser is
  loose about OR precedence; a combined query silently ANDed the from:
  clause over everything and the self-sent email never matched (live
  failure, 2026-08-01). Keep them separate.
- Emails are marked read after processing — there is NO retry. That is why
  `logAudit` writes to the Supabase `webhook_log` table BEFORE the webhook
  fires (the Sheet `WebhookLog` tab is the fallback target, with a
  Telegram warning because the sweep can't see Sheet-only rows), and why
  the sweep exists. Preserve that ordering.
- Pacing: at most `MAX_WEBHOOKS_PER_TICK` (2) webhook posts per 5-min
  tick, `WEBHOOK_SPACING_MS` (20s) apart; the rest stay UNREAD (no audit
  row, no markRead) for the next tick — this is the token-free ingest
  queue (each POST is its own concurrent hermes session, ~28k tokens x
  3-5 calls; two in one tick broke OpenAI's Tier-1 200k TPM ceiling,
  2026-08-15). Only sends count, not scanned emails or fx_failed alerts.
- The nightly ~04:00 Render restart also lives here
  (`restartRenderService` + `RENDER_API_KEY`) — memory hygiene for the
  512MB container; its trigger is created once by hand
  (`setupRestartTrigger`).
- HMAC: `X-Webhook-Signature` = lowercase-hex HMAC-SHA256 of the exact
  `JSON.stringify(payload)` string, bytes masked `& 0xFF`.
- Currency is captured dynamically (`[A-Z]{3}`) — hard-coding SGD silently
  drops foreign transactions (real incident, 2026-04-26). Non-SGD converts
  via frankfurter.dev (`api.frankfurter.app` now 301-redirects there,
  2026-08-17); BND converts at the 1:1 SGD peg in Code.gs itself
  (frankfurter has no BND — a Brunei hotel, 2026-08-29, failed permanently);
  on FX failure: audit row `fx_failed`, no webhook, one-line Telegram
  alert, user logs manually. `debugAudit()` (run manually in the editor)
  live-tests the Supabase audit write and the Telegram alert path — both
  fail SILENTLY when Script Properties are wrong. The Script Property
  `SUPABASE_SERVICE_KEY` must be the LEGACY `service_role` JWT (`eyJ…`),
  never an `sb_secret_*` key — Supabase browser-blocks secret keys and
  UrlFetchApp's Mozilla User-Agent cannot be overridden (the 2026-09-01
  silent-audit incident: a month of 401s into the Sheet fallback).
- The webhook payload shape is documented in `hermes-config/MEMORY.md`
  (plus a `notes` field when FX ran). If you change the payload, update
  MEMORY.md and the route template in `cli-config.yaml` together.
- Every parsing/keying change mirrors into `tests/test_email_parser.py` /
  the parity pins (M16/M17), and the PR body must say
  "requires `clasp push`" plus any new Script Properties.

### Docs, roadmaps, handoffs

- Architecture decisions get an RFC-style doc in `docs/`: Problem (with the
  real dated incident) → Goals/Non-goals → Decisions with rationale →
  Sheet schema tables → Tool surface with JSON examples → Risk table →
  Open decisions → v1 exit criteria as checkboxes. Copy
  `docs/CARD-OPTIMISER-ARCHITECTURE.md`'s shape.
- `docs/HANDOFF-*.md` (none are checked in here; write one when needed) is the session-resumption genre: branch state,
  non-negotiables, what already shipped, implementation checklist,
  verification recipe, PR-body template. Write one whenever work will be
  finished by a different session.
- For what ships today, THIS file + `supabase/migrations/` are
  authoritative. `docs/SETUP.md` is the cloner-facing first-run guide;
  `docs/UPGRADING-HERMES.md` is how the upstream pin moves;
  `docs/STATEMENT-RECON.md` documents the monthly ground-truth ritual.
- Docs are kept honest: when a framing turns out wrong, rewrite it rather
  than paper over.

### Memory files (hermes-config/)

`USER.md` (identity/preferences), `MEMORY.md` (system facts: schema,
webhook shape, enums), `SOUL.md` (persona, ~50 lines) are tier-2 memory.
Keep them small; each has a "what does NOT belong here" rule — respect it
(M18). Transaction data, insights, category lists live in Supabase; session
history lives in FTS5.

**Which of them actually load (verified at v0.21.0, 2026-09-04).** Only
`SOUL.md` does. The prompt builder reads `SOUL.md` from `HERMES_HOME`, which
is exactly where the Dockerfile COPYs it. But `MEMORY.md` and `USER.md` are
read by the memory store from `HERMES_HOME/memories/` — and `start.sh`
symlinks that path to `/data/memories`, so the two files COPY'd to
`/root/.hermes/` have never been in the system prompt at all. What the agent
reads under those names is whatever lives on the persistent disk. This is
pre-existing (it predates the 0.20 bump, and the bump does not change it),
and it is deliberately NOT fixed here — seeding them would change agent
behaviour and belongs in its own PR. Until then: edits to
`hermes-config/MEMORY.md` / `USER.md` are documentation of intent, not a
deploy. Keep writing them correctly; just don't assume they took effect.

### Git & PR style

- Feature branches: `claude/<slug>`, pushed directly; PRs opened
  ready-for-review against `main` on
  your fork (the original lives at `alhazjm/kevin-agent`). Merging to `main` deploys to Render.
- Commits: short imperative subject, blank line, 1–3 sentence body that
  explains the WHY (and for bug fixes, the root cause).
- Opportunistic bug fixes found mid-feature may ride along in the active
  PR, but each gets its own line in the PR body with its root cause.
- PR body states: what changed, why, how it was verified (test count), and
  any manual deploy steps (clasp push, sheet edits, Render env vars, cron
  re-seed).

## Data schema (Supabase)

`supabase/migrations/0001..0008` are THE schema reference — numbered SQL
files, run BY HAND in the Supabase SQL editor, append-only (new files, new
policies via the idempotent `do $$ … duplicate_object` pattern; `create
table if not exists`). Fifteen tables (`supabase/schema.sql` is all of them concatenated,
generated by `supabase/build_schema.py`; regenerate it in the same commit as
any new migration — `tests/test_schema_consolidation.py` fails otherwise):

**`transactions`** (append-only ledger; read paths return Sheet-shaped
dicts with these headers):
```
Date | Merchant | Amount | Currency | Category | Source | Payment Method | Notes | txn_id | telegram_message_id | idempotency_key (+ txn_time)
```

- `txn_id`: `txn_<YYYYMMDD>_<NNN>` — minted by the atomic `next_txn_id()`
  RPC over `txn_id_counters` (legacy rows use `txn_legacy_NNN`).
- `telegram_message_id`: resolves reply-to-message edits.
- `idempotency_key`: 16-hex SHA-256 per M17 (unique; inserts dedupe
  atomically via `on_conflict=idempotency_key`).
- `Source` enum: `email` | `manual` | `backfill` (see M14).
- `Category`: must resolve against a budgets-table row (see the category
  guard); `UNCATEGORIZED` is the reserved pending value.

**`budgets`**: `(category, month "YYYY-MM", limit_amount)` — one row per
category per month. A $0 limit is a REAL budget (any spend = over); "no
budget set" means no row.

**`merchant_map`**: learned merchant → category mappings; case-insensitive
substring match, **longest matching pattern wins**; learning fires only on
user corrections.

The rest: `txn_id_counters` (RPC state), `insights` (derived facts, tier-4
memory), `journal` (reply=journal narratives), `webhook_log` (audit rows
feeding the sweep), `cards` + `card_strategy` (card optimiser inputs,
hand-maintained; card_strategy requires the `_default` sentinel row;
`cards.bonus_cap` since 0003, `cards.base_mpd` since 0007), `card_nudge_log`
/ `trip_nudge_log` (nudge
dedup, auto-written), `travel_mode` (trip definitions), `loans` (IOUs,
0005), `sub_overrides` (subscription verdicts, 0006 — keys are normalized
CATEGORY names), `category_meta` (0008 — `kind` = `fixed` | `variable`;
fixed monthly bills are excluded from over-80% warnings and surface only
when OVER their usual amount; `get_spending_summary` stamps `kind` on
every row and precomputes `_attention.lines`, which the review prompts
print verbatim; absent table → everything reads `variable`).

RLS everywhere: the agent writes with the service key (bypasses RLS,
server-side only); the PWA authenticates via email OTP and `is_owner()`.
PWA write policies are deliberately narrow: budgets (0002), transaction
category updates (0004), loan status flips (0005), sub_overrides (0006).
If you ever widen `is_owner()` by hand (a second login address), land the
same change as a new numbered migration too — a live function no migration
describes is drift, and the next fresh install silently gets different
access rules.

### Travel mode + trip pots — design constraints

- One budgets row per trip (e.g. `Travel - ID 2026-04`); all trip txns
  land in that category so existing budget machinery works unchanged.
- Per-bucket allocations live in `travel_mode.budget_map` as a string
  (`food=450; transport=300; …`). Trips are created conversationally via
  `create_trip` (preview-then-confirm) or by hand.
- `total_budget` is the PLANNED envelope ONLY. The funded pot is DERIVED:
  the sum of `[trip:<label>]`-tagged YouTrip top-ups (never mutate
  total_budget to reflect funding). YouTrip SPENDS are pot-internal —
  excluded from monthly totals everywhere (the top-up was the counted
  outflow; the shared physical card means spend alerts are incomplete by
  construction).
- A txn's bucket is stamped into `Notes` as a `[bucket:X]` prefix —
  deliberately NOT a column (M13).
- Trip routing is DETERMINISTIC and lives in the TOOL
  (`travel_mode.route_for_trip`, called inside `log_expense` /
  `log_expense_pending` before the append — the model never routes).
  When a trip covers the txn date, ONE signal is enough: (A)
  `payment_method` contains "youtrip" (Shortcut taps; pot-internal as
  before), or (B) notes carry `orig:` (bank FX-converted abroad), or (C)
  a manual non-SGD `currency` — converted to SGD in the tool FIRST
  (`_fx_to_sgd`, frankfurter, stamped `orig: MYR 33.00 @ 0.313220
  (frankfurter YYYY-MM-DD)` exactly like Code.gs) and then routed as B.
  Routed = category → the trip's `trip_category`, `[bucket:X]` derived
  from the model's PROPOSED home category via `_bucket_for_category`
  (food/transport/shopping/health/lodging/misc keyword map; an existing
  `[bucket:]` is respected). MerchantMap now decides the BUCKET, never
  the category — the old "a learned mapping beats travel mode" rule is
  dead (it sent Gojek-in-KL to the home Personal - Travel budget → 912%).
  No signal (SGD, no `orig:`, not YouTrip — Shopee for home, PayLah to a
  friend) → NORMAL flow untouched: currency/pot is the guard against
  "everything during the trip is travel", not the date. Never routed:
  YouTrip top-ups (`[trip:]`-tagged via `link_topup_to_trip` instead),
  rows already in the trip category, fixed monthly bills (`category_meta`
  kind `fixed` — Anthropic bills in USD mid-trip carry `orig:` but are
  not trip spend), calls with `route_to_trip: false` (user said "not a
  trip cost"), and any trip whose category doesn't resolve against
  `budgets` (`create_trip` ensures the $0 row; without it the home
  category lands rather than an unknown_category refusal). Pending flow: a signalled spend
  skips the ask-prompt and logs straight into the trip category with
  `[bucket:misc]` + the normal bubble. The applied routing is reported as
  `trip_routed: {trip_label, from_category, to_category, bucket,
  signal}`; FX failure returns `status=error, reason=fx_failed` with
  nothing written and no bubble. The model's only job during a trip:
  propose the HOME category as always and pass currency / notes /
  payment_method / time / idempotency_key through. Manual foreign entries
  ("rm33") mean CASH unless the user names YouTrip (product decision, 2026-08-18):
  no payment method → converted, routed via `orig:`, COUNTED in the month;
  `payment_method="YouTrip Card"` only when the user says so (pot-internal).
- 80%/100% bucket alerts dedupe via `trip_nudge_log` keyed on
  (trip, bucket, threshold, budget_at_nudge) — a mid-trip reallocation
  re-arms the nudge.

### Cycle math (card optimiser)

"Cycle" ≠ calendar month. Each card has `cycle_start_day` (hand-set in the
cards table; confirm off a real statement), clamped to the month's last
day for short months. Always use the `cycle_window` from tool payloads.
UOB Preferred's bonus pools (tap + online) are CALENDAR-month, tracked by
`get_bonus_pool_status` — cycle spend and pool spend are different
windows on the same tile. Cap bands: ok <80%, warning 80–99%, capped
≥100% — thresholds appear in `_cap_status`, both nudge hooks, and
`get_trip_budget_status`; keep them aligned.

## Build & deploy reference

1. **Only COPY'd paths exist in the container** (M1). Currently:
   `tools/{supabase_client,sheets_client,expense_sheets_tool,card_optimiser,travel_mode,loans}.py`
   → `/app/hermes-agent/tools/`; `skills/` → `/root/.hermes/skills/` (after
   pruning upstream's built-in skill catalogs); `hermes-config/cli-config.yaml`
   → `/root/.hermes/config.yaml`; `USER.md`/`MEMORY.md`/`SOUL.md` →
   `/root/.hermes/`; `deploy/start.sh` → `/app/start.sh`;
   `cron/setup-cron-jobs.sh` → `/app/cron/`;
   `hermes-config/skill-bundles/` → `/root/.hermes/skill-bundles/`. `pwa/`,
   `recon/`, `scripts/`, and `.claude/` never ship.
2. **How our tools reach the model** (M2): `platform_toolsets` in
   `cli-config.yaml` — one list per platform, each containing
   `expense_tracker`. There is no sed injection any more (removed at the
   0.20.6 bump; its `toolsets.py` anchor is gone upstream). Registration is
   auto-discovered — `tools/registry.py::discover_builtin_tools()`
   AST-scans `tools/*.py` for a top-level `registry.register()` — but the
   `printf 'import tools.expense_sheets_tool' >> model_tools.py` line is
   KEPT as a fail-loud canary, because discovery swallows an `ImportError`
   as a log warning and would ship zero tools silently.
3. **TWO build-time patches** in `deploy/patches/`, both applied to
   `agent/conversation_loop.py` (upstream v0.15.0 extracted the agent loop
   out of `run_agent.py` into the module-level function
   `run_conversation(agent, …)` — there is no `self` in that scope):
   `suppress_reply_on_silent_tools.py` (**v3** — exits the agent loop when
   the latest tool result carries `"assistant_reply_required": false`,
   scanning only THIS turn's messages, and emits the literal `NO_REPLY`;
   an empty `final_response` is now rewritten into a delivered
   "⚠️ Processing completed but no response was generated" bubble before
   any silence check runs, so v2's `""` would have put one junk bubble in
   Telegram per logged expense) and `log_llm_usage.py` (usage JSONL to
   `/data`; its injection imports `datetime`/`Path` locally because
   `conversation_loop.py` imports neither, and the surrounding
   `except Exception: pass` would turn a `NameError` into a permanently
   empty usage log).
   Three patches were RETIRED at the 0.20.6 bump because upstream now does
   the job: `suppress_retry_status_after_silent_tools.py` (retry statuses
   are buffered and only flushed on terminal failure),
   `suppress_codex_incomplete_after_silent_tools.py` (the sentinel is
   hidden gateway-side, and patch v3 exits before the continuation block
   anyway), and `skip_memory_flush_for_webhook_sessions.py`
   (`_flush_memories_for_session` no longer exists — the expiry watcher
   runs no agent, so the 2026-08-17 ~40%-of-tokens burn is fixed upstream).
   Each surviving patch requires its anchor to match exactly once and exits
   non-zero otherwise → Docker build fails loud; the Dockerfile greps for
   marker strings after. Never weaken these checks. Both honour a
   `PEHD_PATCH_TARGET` env override so they can be sanity-run locally
   against a downloaded upstream `conversation_loop.py`.
4. **Two filesystems**: `/root/.hermes/` is rebuilt every deploy —
   ephemeral. `/data/` is the Render persistent disk: `service-account.json`
   plus `sessions`/`memories`/`cron` symlinked in by `start.sh` (which
   also prunes old session transcripts). Cron jobs seed once per disk via
   the `/data/cron/.seeded` marker — the seeding script only CREATES
   jobs, so re-seeding requires removing the existing jobs first
   (`hermes cron list` / `remove`), then the marker, then a restart.
5. **Env vars** (render.yaml lists them; secrets `sync: false`):
   `OPENAI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (the full JSON blob —
   start.sh writes it to `/data/service-account.json`; a file PATH in
   local dev), `GSPREAD_SPREADSHEET_ID` (still needed — nightly export),
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `WEBHOOK_HMAC_SECRET`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, plus non-secret
   `PEHD_LLM_USAGE_DIR=/data/llm_usage`. start.sh sed-replaces TWO
   config placeholders (M4): `__WEBHOOK_SECRET_PLACEHOLDER__` and
   `__OPENAI_KEY_PLACEHOLDER__` (the STT key).
6. **Memory hygiene trio** (512MB container): Dockerfile sets
   `MALLOC_ARENA_MAX=2`, does NOT install the `[voice]` extra that carries
   `faster-whisper` (its ~150MB model caused the 2026-07-30 OOM; STT is the OpenAI API —
   `gpt-4o-mini-transcribe` — instead), and the Apps Script nightly
   ~04:00 Render restart resets the RSS baseline. `ffmpeg` is installed
   for Edge-TTS voice bubbles (mp3 → OGG/Opus for Telegram send_voice).
7. **`TZ=Asia/Singapore`** makes SGT cron wall-clock work (M5).
8. Gateway runs foreground (`hermes gateway run`) — `gateway start` needs
   systemd, which Docker lacks. Webhook listens on 8644.
9. `cron.wrap_response: false` in cli-config.yaml suppresses the cron
   executor's wrapper text — still honored at v0.21.0 (grep
   `wrap_response` in `cron/scheduler.py`; it is documented in
   `config_defaults.py` now, default True). Re-verify on SHA bumps.
10. **Cron tools are allowlisted per platform** since v0.20.x:
   `platform_toolsets.cron` in cli-config.yaml is the ONLY thing that puts
   the 37 expense tools in front of the six scheduled jobs, and its absence
   fails silently. `hermes cron create` has no toolset flag, so this cannot
   be fixed in the seeding script. Cron jobs with nothing to report must
   reply `[SILENT]` — an empty response suppresses delivery but is booked
   as a soft-fail that feeds the failure-streak nudge.

## Testing

```bash
pip install pytest gspread google-auth cffi   # cffi is required (see Tests)
pytest tests/                                 # full suite (~1s, no network)
pytest tests/test_travel_mode.py              # one file
pytest tests/ -k "idempotency"                # by keyword
```

Current count: **648 passing** across ten files:
`test_expense_sheets_tool` (also covers `sheets_client`),
`test_supabase_client`, `test_card_optimiser`, `test_travel_mode`,
`test_loans`, `test_email_parser` (the Apps Script mirror, M16),
`test_statement_recon`, `test_skill_bundles` (the slash-command bundles:
every listed skill exists, the Dockerfile ships the dir),
`test_sheets_optional` (Google Sheets stays optional) and
`test_schema_consolidation` (`schema.sql` matches the migrations). State the new total in every PR body; CI runs the same suite on every push.

## Checklists

### Adding a new tool
1. Logic in the right layer (`supabase_client.py`, or the feature module).
2. Schema constant + `handle_*` + `registry.register` block in
   `expense_sheets_tool.py`; add to the module docstring list.
3. No Dockerfile change needed for the tool name (the sed is gone) — but
   confirm `expense_tracker` is in every `platform_toolsets` list in
   `cli-config.yaml` that should see it (M2).
4. Reference it from the owning SKILL.md (when to call, example) + bump
   the skill version.
5. Tests: ≥2 handler tests (kwargs pass-through + JSON round-trip) + a
   behavior class for the logic (happy path, each non-ok status, legacy
   sheet layout if it touches the export path).
6. New table/column? → schema-change checklist too.
7. Update the tool count in this file, `README.md` and `docs/SETUP.md`
   (a script in the sync PR checked all three; keep them equal).

### Adding a new config file
1. Put it in an already-COPY'd directory, or add a COPY line.
2. Env-var interpolation needed? Placeholder + sed in `deploy/start.sh`
   (hermes YAML does not expand env vars — M4).

### Changing the data schema
1. New numbered file in `supabase/migrations/` (never edit an existing
   one): `create table if not exists`, policies via the idempotent
   `do $$ … duplicate_object` pattern. NEVER rename, reorder, or delete
   existing columns/tables (ask first — escalation rules).
2. The migration runs BY HAND in the Supabase SQL editor — the PR body
   must name it as a manual step (surface 3).
3. Code degrades gracefully pre-migration (guarded reads, absent table →
   empty/skip, never throw — the loans/sub_overrides fetches are the
   pattern).
4. Update `MEMORY.md`'s schema section; if the export tab layout changes,
   the Sheet-export writer too.
5. Tests for both presence and absence of the new column/table.

### Editing a skill
1. Check the edit against the behavioral invariants list (Skill files
   section). 2. Bump the version per semver rule. 3. Verify every tool
   named is registered and that `expense_tracker` is in the relevant
   `platform_toolsets` list. 4. If a cron prompt encodes the same
   behavior, update `cron/setup-cron-jobs.sh` in the same commit (and note
   that live cron jobs only pick it up after deleting `/data/cron/.seeded`
   or editing via `hermes cron`).

### Changing the Apps Script
1. Mirror regex/key changes into `tests/test_email_parser.py` / parity pins
   (M16/M17); run the suite. 2. Payload changed? → MEMORY.md +
   cli-config route template. 3. PR body: "requires `clasp push`" + any new
   Script Properties. 4. After deploy, run `testIdempotencyKeyParity()` in
   the Apps Script editor if keying changed.

### hermes-agent SHA bump
`docs/UPGRADING-HERMES.md` is the procedure; the weekly
`.github/workflows/anchor-check.yml` runs every item below against the
newest upstream tag and reports into an issue. This list is the contract.
Upstream is pinned by `HERMES_AGENT_SHA` in the Dockerfile (currently
`29112bef` = v0.21.0, tag `v2026.8.31`; the 0.20.6 → 0.21.0 step on
2026-09-04 moved nothing we anchor on). The two patch scripts in
`deploy/patches/` depend on upstream anchor strings — each fails the Docker
build loudly if its anchor shifts. Pin to a TAG, never floating `main`.
Before bumping, verify at the candidate SHA (`raw.githubusercontent.com`
fetches are pre-allowed; download the files once and grep locally):
1. `agent/conversation_loop.py` still contains
   `            if agent.api_mode == "codex_responses" and finish_reason == "incomplete":`
   (12-space indent) exactly once — the `suppress_reply_on_silent_tools.py`
   v3 anchor. Also confirm `current_turn_user_idx`, `messages`,
   `final_response`, `_turn_exit_reason` and `assistant_message` are still
   locals of `run_conversation` bound before that line, and that a `break`
   there still exits the MAIN agent loop (not an inner one). If refactored,
   update `ANCHOR` + `INJECTION` — an ask-first change.
2. `agent/conversation_loop.py` still contains the 20-space
   `agent.session_cost_status = cost_result.status` /
   `agent.session_cost_source = cost_result.source` / blank /
   `# Persist token counts to session DB for /insights.` block exactly once
   — the `log_llm_usage.py` anchor — and still does NOT import `datetime`
   or `Path` at module level (the injection imports them itself; if that
   ever changes, the local imports stay harmless).
3. `gateway/response_filters.py` still lists `NO_REPLY` in
   `LIVE_GATEWAY_SILENT_MARKERS`, and both the strict
   (`is_intentional_silence_response`, telegram lane) and loose
   (`is_autonomous_silence_response`, webhook + cron lanes) matchers still
   accept it. This is what makes patch v3's emitted token silent; if the
   marker set changes, the silence contract breaks on every lane at once.
4. `agent/conversation_loop.py` still routes retry statuses through
   `agent._buffer_status(…)`, not `_emit_status` — that buffering is why
   the retry-status patch was retired.
5. `model_tools.py` still exists (the Dockerfile appends an import to it)
   and `tools/registry.py` still auto-discovers top-level
   `registry.register()` in `tools/*.py`.
6. `cron/scheduler.py` still honors `wrap_response`, and still resolves a
   job's tools via the `cron` platform's `platform_toolsets` entry — if
   that resolution changes again, re-check that all 37 tools still reach
   the six jobs (they fail SILENTLY when they don't).
7. `hermes_cli/config_defaults.py`: re-enumerate default-ON auxiliary
   forks. We explicitly disable `auxiliary.background_review`,
   `auxiliary.title_generation`, `curator`, `lsp` and `model_catalog`; a
   new one added upstream would start costing tokens/RAM on the next bump
   without any config change on our side.
8. A top-level `skills/` directory still exists in the clone (the
   Dockerfile prunes it; the gateway re-seeds from it on every start).
9. `pyproject.toml` still defines the `all`, `messaging` and `edge-tts`
   extras the Dockerfile installs, and `requires-python` still admits 3.11.
10. `gateway/run.py` still dispatches skill BUNDLES before skills and still
   reads them from `HERMES_HOME/skill-bundles/*.yaml`
   (`agent/skill_bundles.py`); and the unknown-slash-command block still
   exists (it is what makes the bundles load-bearing — if upstream ever
   forwards unknown commands again, the bundles become optional, not
   wrong).
Record what you verified in the commit body. If any anchor drifted, STOP
and present findings — never loosen an anchor to make the build pass.

## Quality bar per deliverable (done = all boxes check)

**A new tool** — the three-place rule + `platform_toolsets` (M2) satisfied; handler returns a JSON
string and never raises; status vocabulary from the contracts section (no
new statuses without a skill consumer); silence contract complete if it
sends Telegram (M9); tests as per checklist; suite green with the new count
stated; tool count updated in this file, README.md and docs/SETUP.md.

**A bug fix** — commit body names the root cause (not just the symptom); a
regression test exists that fails on the pre-fix code; no drive-by
refactors in the same diff; if the bug corrupted ledger data, the PR body
says what manual correction (SQL) is needed (or that none is).

**A skill edit** — version bumped; all six behavioral invariants still
present in the text (grep for "EMPTY", "learn_merchant_mapping",
"tables"); every referenced tool is registered and reachable via
`platform_toolsets`; cron prompts
consistent; no schema/tool facts duplicated into the skill that belong in
MEMORY.md or the ledger.

**A schema change** — a new numbered migration, append-only; graceful
pre-migration degradation; `MEMORY.md` updated (and the export writer if tab layouts change); the manual SQL step
named in the PR body; presence+absence tests.

**An Apps Script change** — regex/parity mirrors updated in the same
commit; suite green; PR body carries the deploy steps; no
`new Date()`-based date conversion introduced (M5).

**Any PR** — suite green with count stated; `AGENTS.md` updated where it
describes the changed behaviour; commit style honored; PR body lists every manual
step a human must take (clasp paste, SQL migrations, env vars, cron
re-seed); if nothing manual is needed, it says so explicitly.

## When uncertain — escalation rules

**Resolution ladder** (exhaust in order before asking):
1. This file. 2. `supabase/migrations/` for anything schema.
3. The owning SKILL.md for runtime behavior contracts. 4. The tests — they
are the executable spec (a behavior pinned by a test is a contract, not an
accident). 5. Upstream hermes-agent source at the pinned SHA via
`raw.githubusercontent.com/NousResearch/hermes-agent/<SHA>/<path>` (pre-allowed
domain). 6. Ask the repo owner — for a fork, that is you.

**Proceed without asking** (reversible, checklist-covered): new tools per
the checklist; additive tests; doc syncs; comment/docstring improvements;
skill version bumps; new files with their COPY lines; bug fixes with
regression tests.

**Ask FIRST — even if the request seems to imply it** (present numbered
options, recommendation first, one screenful max):
- Changing the idempotency-key formula, inputs, or truncation (M17) — this
  is dedup of real money.
- Changing the `txn_id` format, the
  `[bucket:X]`/`[trip:<label>]`/`orig:`/`||PREV:` Notes encodings, or the
  bubble text format (the reply-to-edit fallback parses `txn_YYYYMMDD_NNN`
  out of bubble text).
- Renaming/reordering/deleting table columns or tables; any migration
  that rewrites existing transactions rows.
- Bumping `HERMES_AGENT_SHA`; editing `ANCHOR`/`INJECTION` in the patch.
- Adding/removing/re-scheduling cron jobs.
- New external services or dependencies (even free ones), new env vars.
- Deleting or hollowing out files you didn't create (docs included).

**Stop and report — do not work around**:
- A Dockerfile/patch anchor doesn't match: never loosen the anchor or
  bypass the grep; report what upstream changed.
- Tests fail in an area your diff didn't touch: report, don't "fix" the
  test to green.
- The live Supabase schema doesn't match `supabase/migrations/` (a
  hand-run SQL change never made it into a migration file): stop and
  reconcile the two before writing code against either.
- You'd need a secret/credential that isn't in the documented env vars.

**How to ask**: numbered options with your recommendation first and the
trade-off in one line each — never open-ended "what do you want?". If the
session is non-interactive, ship the safe subset and put the question in
the PR body under "Open questions" instead of guessing.

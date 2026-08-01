# CLAUDE.md

Operating manual for coding agents (Claude Code and others) working in this
repo. It is written so that a model less capable than the one that wrote it
can still ship safely here: follow the checklists literally, treat the named
mistakes as real (each one cost debugging time at least once), and use the
escalation rules instead of guessing. When this file and your instinct
disagree, this file wins.

> `AGENTS.md` is a mirror of this file for non-Claude agents (Codex etc.).
> The two files differ ONLY in the title and this intro block. When you edit
> this file, apply the same edits there — see mistake **M15**.

## What this repo is

This repo is the **deployment shell** for a personal
expense-tracking agent built on the
[hermes-agent](https://github.com/alhazjm/hermes-agent) framework. It runs on
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
  via frankfurter.app (stamps "orig: …" into notes) → audit row to the
  Supabase webhook_log table (durable, BEFORE send; Sheet WebhookLog tab
  is the fallback) → HMAC-sign (X-Webhook-Signature) →
  POST /webhooks/expense-ingest
    → hermes webhook route renders the prompt in cli-config.yaml
    → gpt-5.4-nano + expense-tracker skill picks a category
      (multi-category list → travel mode → MerchantMap → judgment → ask;
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
`source="manual"`. Cron path: 7 jobs (see `cron/setup-cron-jobs.sh`) invoke
skills on SGT schedules and deliver to Telegram.

## Repo layout

```
Dockerfile                     # Render build — see "Build & deploy reference"
render.yaml                    # Render Blueprint for the web service (env-var list lives here)
deploy/start.sh                # Container entrypoint; writes .env, seds placeholders, links /data, prunes sessions, runs hermes
deploy/patches/                # FOUR build-time patches applied to hermes-agent source (see Build & deploy)
apps-script/                   # Gmail Apps Script (bank + Shortcut email → webhook); deployed via clasp, not the container
cron/setup-cron-jobs.sh        # Creates the 7 hermes cron jobs (seeded once per /data disk by start.sh)
docs/                          # SETUP.md (cloner guide) + RFC-style notes: STATEMENT-RECON.md,
                               #   CARD-OPTIMISER-ARCHITECTURE.md, SWEEP-ARCHITECTURE.md, HANDOFF-*.md
hermes-config/
  cli-config.yaml              # Hermes gateway config (model, stt, webhook route, platform toolsets)
  USER.md.example              # Tier-2 identity template (you, household, payment methods)
  MEMORY.md.example            # Tier-2 system facts (schema, webhook shape, enums)
  SOUL.md.example              # Tier-2 agent persona (tone, principles)
                               #   Fill these in, drop the .example suffix — the Dockerfile COPYs the real names
  install.sh                   # LOCAL DEV ONLY — not in the container
skills/                        # RUNTIME skills (ship to /root/.hermes/skills/)
  expense-tracker/SKILL.md     # Main skill (v5.7.0): categorisation, travel routing, trips/pots, lending, silence contract
  budget-manager/SKILL.md      # Cron-driven budget warnings / reallocation (v3.0.0)
  weekly-summary/SKILL.md      # Friday + 1st-of-month summaries (v3.1.0)
  card-optimiser/SKILL.md      # Cap tracking, card recs, nudges, scorecard (v1.4.0)
.claude/skills/                # REPO-side Claude Code skills (never ship): /statement-recon,
                               #   /card-tnc-review, /new-alert-source
tools/
  supabase_client.py           # ALL PostgREST I/O — the ledger of record's only gateway
  sheets_client.py             # Sheet export/archive writer + idempotency formula + legacy gspread helpers
  expense_sheets_tool.py       # ALL tool registration (37 tools) + Telegram send helpers
  card_optimiser.py            # Card logic + post-cap nudge hook (no registration here)
  travel_mode.py               # Trip routing, [trip:]/[bucket:] writers + bucket nudge hook (no registration here)
  loans.py                     # IOU logic: create/repay/offset-sweep (no registration here)
supabase/migrations/           # 0001..0007 numbered SQL — THE schema reference (run BY HAND in the SQL editor)
supabase/schema.sql            # GENERATED one-paste consolidation for fresh installs — never hand-edit
supabase/build_schema.py       # Regenerates schema.sql; a test fails if the two drift
pwa/                           # Static PWA dashboard (separate Render Static Site; never in the container)
recon/                         # Local statement-recon CLI (pypdf, checksum-gated parsers) — never ships
scripts/                       # Local/ops helpers (one-time Supabase backfill, LLM-usage summariser)
samples/                       # GITIGNORED — real e-statement PDFs live here, never committed
sheets-template/README.md      # LEGACY sheet-era schema doc — kept for the export tab layout only
tests/                         # 578 tests across 8 files; see "Testing"
conftest.py                    # Repo root; sys.path fix + stubs tools.registry (framework-injected at runtime)
ROADMAP.md                     # Strategic direction + "Things NOT to do" — read before proposing features
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
  sees it, because upstream `toolsets.py` hard-codes the telegram gateway's
  tool list and our Dockerfile `sed` injection is what appends our names to
  it. A tool missing from the sed list loads fine and is never callable.
  *Rule: a tool name must appear in FOUR places in the same commit —
  (1) `registry.register(name=…)` in `expense_sheets_tool.py`, (2) the
  Dockerfile `RUN sed -i` line anchored on `"send_message",`, (3) a
  SKILL.md that tells the LLM when to call it, (4) tests. Update the tool
  count here (currently **37**: 22 core + 6 card + 5 travel + 4 loans).*
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
- **M6 — The Local Install Trap.** Running `hermes-config/install.sh` in the
  Render shell. It's a WSL dev helper and isn't in the container. *Rule:
  the container's install path is the Dockerfile itself.*

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
  (Sam's supplementary card); counting them corrupts subscription-creep,
  card-cycle spend, trip spend, and efficiency review. *Rule: every
  agent-side aggregate filters out `Source == "backfill"` and
  `_is_pending(row)`. `source` is a closed enum: `email | manual |
  backfill` — never invent values. `UNCATEGORIZED` is defined once in
  `sheets_client.PENDING_CATEGORY` (imported by `supabase_client`) plus a
  separate `expense_sheets_tool._PENDING_CATEGORY` — the two must stay
  equal. Known, deliberate divergence: the PWA's monthly hero/chart COUNT
  backfill and pending rows (honest cash-out totals — a deliberate
  2026-07-31 call) — don't "fix" either side to match the other.*

### Sync contracts (places that must change together)

- **M15 — The Mirror Skip.** Editing `CLAUDE.md` without `AGENTS.md` (or
  vice versa). *Rule: apply identical edits to both; only the title and
  intro block may differ. `diff CLAUDE.md AGENTS.md` should touch nothing
  below the intro.*
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
  or session history into `USER.md` / `MEMORY.md` / `SOUL.md`. They load
  into the system prompt EVERY turn. *Rule: respect each file's "what does
  NOT belong here" list; MEMORY.md past ~200 lines or SOUL.md past ~60 is
  a bug.*
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
(the universal `check_fn`, name kept from the sheet era) now gates on BOTH
backends: gspread env vars AND `supabase_client._configured()`.

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
  `{"status": "setup_required", "message": …}` until the `Cards` +
  `CardStrategy` tabs (incl. the `_default` sentinel row) exist. The skill
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

- Run: `.venv-test/Scripts/python.exe -m pytest tests/ -q` (ready venv on
  a prepared venv) or `pip install pytest gspread google-auth cffi` first.
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
  NOT fail tests. That's what the Dockerfile sed check and the preflight
  skill are for.
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
- The `.claude/skills/` skills are a DIFFERENT species: repo-side Claude
  Code workflows (/statement-recon, /card-tnc-review, /new-alert-source)
  that never ship to the container and are run by a human from the repo.
- If a skill references a tool, that tool must exist in the Dockerfile sed
  list. If a cron prompt in `setup-cron-jobs.sh` encodes a skill behavior
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
- The nightly ~04:00 Render restart also lives here
  (`restartRenderService` + `RENDER_API_KEY`) — memory hygiene for the
  512MB container; its trigger is created once by hand
  (`setupRestartTrigger`).
- HMAC: `X-Webhook-Signature` = lowercase-hex HMAC-SHA256 of the exact
  `JSON.stringify(payload)` string, bytes masked `& 0xFF`.
- Currency is captured dynamically (`[A-Z]{3}`) — hard-coding SGD silently
  drops foreign transactions (real incident, 2026-04-26). Non-SGD converts
  via frankfurter.app; on FX failure: audit row `fx_failed`, no webhook,
  one-line Telegram alert, user logs manually.
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
- `docs/HANDOFF-*.md` is the session-resumption genre: branch state,
  non-negotiables, what already shipped, implementation checklist,
  verification recipe, PR-body template. Write one whenever work will be
  finished by a different session.
- Root `ROADMAP.md` = strategy; honor its "Things NOT to do". For what
  ships today, THIS file + `supabase/migrations/` are authoritative
  (`sheets-template/README.md` is the legacy sheet-era schema doc, kept
  for the export tab layout). `docs/SETUP.md` is the cloner-facing
  first-run guide; `docs/STATEMENT-RECON.md` documents the monthly
  ground-truth ritual.
- Docs are kept honest: when a framing turns out wrong, rewrite it rather
  than paper over.

### Memory files (hermes-config/)

`USER.md` (identity/preferences), `MEMORY.md` (system facts: schema,
webhook shape, enums), `SOUL.md` (persona, ~50 lines) are tier-2 memory
loaded every turn. Keep them small; each has a "what does NOT belong here"
rule — respect it (M18). Transaction data, insights, category lists live in
Supabase; session history lives in FTS5.

### Git & PR style

- Feature branches: `claude/<slug>`, pushed directly; PRs opened
  ready-for-review against `main` on your own fork. Merging to `main`
  deploys to Render, so treat `main` as production.
- Commits: short imperative subject, blank line, 1–3 sentence body that
  explains the WHY (and for bug fixes, the root cause). End with the
  session trailer (`https://claude.ai/code/session_*`) when the session
  provides one.
- Opportunistic bug fixes found mid-feature may ride along in the active
  PR, but each gets its own line in the PR body with its root cause.
- PR body states: what changed, why, how it was verified (test count), and
  any manual deploy steps (clasp push, sheet edits, Render env vars, cron
  re-seed).

## Data schema (Supabase)

`supabase/migrations/0001..0007` are THE schema reference — numbered SQL
files, run BY HAND in the Supabase SQL editor, append-only (new files, new
policies via the idempotent `do $$ … duplicate_object` pattern; `create
table if not exists`). Fourteen tables:

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
CATEGORY names).

RLS everywhere: the agent writes with the service key (bypasses RLS,
server-side only); the PWA authenticates via email OTP and `is_owner()`.
PWA write policies are deliberately narrow: budgets (0002), transaction
category updates (0004), loan status flips (0005), sub_overrides (0006).
`is_owner()` in 0001 pins a single address — change it to yours BEFORE
running the migration, or the PWA signs in and renders nothing. If you
ever widen it by hand in the SQL editor, land the same change as a new
migration; a live function that no migration describes is drift.

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
- Activation needs all three: txn notes carry `orig:` (FX-converted),
  `get_active_travel_mode()` active, NO MerchantMap match (learned
  mappings beat travel mode).
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
   `cron/setup-cron-jobs.sh` → `/app/cron/`. `pwa/`, `recon/`, `scripts/`,
   and `.claude/` never ship.
2. **The tool-list sed injection** (M2): `RUN sed -i` anchored on
   `"send_message",` in upstream `toolsets.py` — search for the anchor,
   don't trust line numbers. Tool registration itself is activated by the
   `printf 'import tools.expense_sheets_tool' >> model_tools.py` line.
3. **Four build-time patches** in `deploy/patches/`, applied to upstream
   source: `suppress_reply_on_silent_tools.py` (v2 — EXITS the agent loop
   when the latest tool result carries `"assistant_reply_required":
   false`; v1 zeroed `final_response` and triggered the empty-response
   retry cascade), `suppress_retry_status_after_silent_tools.py`,
   `suppress_codex_incomplete_after_silent_tools.py`, and
   `log_llm_usage.py` (usage JSONL to `/data`). Each requires its anchor
   to match exactly once and exits non-zero otherwise → Docker build
   fails loud; the Dockerfile greps for marker strings after. Never
   weaken these checks.
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
   `MALLOC_ARENA_MAX=2`, explicitly UNINSTALLS `faster-whisper` (its
   ~150MB model caused the 2026-07-30 OOM; STT is the OpenAI API —
   `gpt-4o-mini-transcribe` — instead), and the Apps Script nightly
   ~04:00 Render restart resets the RSS baseline. `ffmpeg` is installed
   for Edge-TTS voice bubbles (mp3 → OGG/Opus for Telegram send_voice).
7. **`TZ=Asia/Singapore`** makes SGT cron wall-clock work (M5).
8. Gateway runs foreground (`hermes gateway run`) — `gateway start` needs
   systemd, which Docker lacks. Webhook listens on 8644.
9. `cron.wrap_response: false` in cli-config.yaml suppresses the cron
   executor's wrapper text — an undocumented-but-supported upstream flag
   (`cron/scheduler.py:313` at the pinned SHA). Re-verify on SHA bumps.

## Testing

```bash
pip install pytest gspread google-auth cffi   # cffi is required (see Tests)
pytest tests/                                 # full suite (~1s, no network)
pytest tests/test_travel_mode.py              # one file
pytest tests/ -k "idempotency"                # by keyword
```

On Windows: `.venv-test/Scripts/python.exe -m pytest tests/ -q`.
Current count: **578 passing** across eight files:
`test_expense_sheets_tool` (also covers `sheets_client`),
`test_supabase_client`, `test_card_optimiser`, `test_travel_mode`,
`test_loans`, `test_email_parser` (the Apps Script mirror, M16),
`test_statement_recon`, and `test_schema_consolidation` (keeps
`supabase/schema.sql` honest against the migrations). State the new total in every PR body.

## Checklists

### Adding a new tool
1. Logic in the right layer (`supabase_client.py`, or the feature module).
2. Schema constant + `handle_*` + `registry.register` block in
   `expense_sheets_tool.py`; add to the module docstring list.
3. Add the name to the Dockerfile sed injection (M2).
4. Reference it from the owning SKILL.md (when to call, example) + bump
   the skill version.
5. Tests: ≥2 handler tests (kwargs pass-through + JSON round-trip) + a
   behavior class for the logic (happy path, each non-ok status, legacy
   sheet layout if it touches the export path).
6. New table/column? → schema-change checklist too.
7. Update the tool count/list in this file AND `AGENTS.md`.

### Adding a new config file
1. Put it in an already-COPY'd directory, or add a COPY line.
2. Env-var interpolation needed? Placeholder + sed in `deploy/start.sh`
   (hermes YAML does not expand env vars — M4).

### Changing the data schema
1. New numbered file in `supabase/migrations/` (never edit an existing
   one): `create table if not exists`, policies via the idempotent
   `do $$ … duplicate_object` pattern. NEVER rename, reorder, or delete
   existing columns/tables (ask first — escalation rules).
1b. Regenerate the one-paste consolidation in the SAME commit:
   `python supabase/build_schema.py`. `tests/test_schema_consolidation.py`
   fails if you forget, and a stale `schema.sql` silently omits your table
   from every fresh install. Never hand-edit `schema.sql`.
2. The migration runs BY HAND in the Supabase SQL editor — the PR body
   must name it as a manual step (surface 3).
3. Code degrades gracefully pre-migration (guarded reads, absent table →
   empty/skip, never throw — the loans/sub_overrides fetches are the
   pattern).
4. Update `MEMORY.md`'s schema section; if the export tab layout changes,
   the Sheet-export writer and `sheets-template/README.md` too.
5. Tests for both presence and absence of the new column/table.

### Editing a skill
1. Check the edit against the behavioral invariants list (Skill files
   section). 2. Bump the version per semver rule. 3. Verify every tool
   named exists in the sed list. 4. If a cron prompt encodes the same
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
Upstream is pinned by `HERMES_AGENT_SHA` in the Dockerfile. The four patch
scripts in `deploy/patches/` depend on upstream anchor strings — each fails
the Docker build loudly if its anchor shifts. Before bumping, verify at the
candidate SHA
(`raw.githubusercontent.com` fetches are pre-allowed):
1. `toolsets.py` still contains `"send_message",` exactly once (the sed
   appends after EVERY match — duplicates would double-inject).
2. `run_agent.py` still contains the line
   `                    final_response = assistant_message.content or ""`
   (20-space indent, no-tool-calls branch) exactly once. If refactored,
   update `ANCHOR` + `INJECTION` in
   `deploy/patches/suppress_reply_on_silent_tools.py` to match the new loop
   shape — and treat that as an ask-first change.
3. `model_tools.py` still exists (the Dockerfile appends an import to it).
4. `cron/scheduler.py` still honors `wrap_response`.
Record what you verified in the commit body. If any anchor drifted, STOP
and present findings — never loosen an anchor to make the build pass.

## Quality bar per deliverable (done = all boxes check)

**A new tool** — the four-place rule (M2) satisfied; handler returns a JSON
string and never raises; status vocabulary from the contracts section (no
new statuses without a skill consumer); silence contract complete if it
sends Telegram (M9); tests as per checklist; suite green with the new count
stated; tool count updated here + AGENTS.md.

**A bug fix** — commit body names the root cause (not just the symptom); a
regression test exists that fails on the pre-fix code; no drive-by
refactors in the same diff; if the bug corrupted ledger data, the PR body
says what manual correction (SQL) is needed (or that none is).

**A skill edit** — version bumped; all six behavioral invariants still
present in the text (grep for "EMPTY", "learn_merchant_mapping",
"tables"); every referenced tool exists in the sed list; cron prompts
consistent; no schema/tool facts duplicated into the skill that belong in
MEMORY.md or the ledger.

**A schema change** — a new numbered migration, append-only; graceful
pre-migration degradation; `MEMORY.md` updated (and the export writer +
`sheets-template/README.md` if tab layouts change); the manual SQL step
named in the PR body; presence+absence tests.

**An Apps Script change** — regex/parity mirrors updated in the same
commit; suite green; PR body carries the deploy steps; no
`new Date()`-based date conversion introduced (M5).

**Any PR** — suite green with count stated; CLAUDE.md/AGENTS.md diff clean
below the intro block; commit style honored; PR body lists every manual
step a human must take (clasp paste, SQL migrations, env vars, cron
re-seed); if nothing manual is needed, it says so explicitly.

## When uncertain — escalation rules

**Resolution ladder** (exhaust in order before asking):
1. This file. 2. `supabase/migrations/` for anything schema
(`sheets-template/README.md` only for the legacy export tab layout).
3. The owning SKILL.md for runtime behavior contracts. 4. The tests — they
are the executable spec (a behavior pinned by a test is a contract, not an
accident). 5. Upstream hermes-agent source at the pinned SHA via
`raw.githubusercontent.com/alhazjm/hermes-agent/<SHA>/<path>` (pre-allowed
domain). 6. Ask the maintainer.

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
- Anything on ROADMAP.md's "Things NOT to do" list.
- Deleting or hollowing out files you didn't create (docs included).

**Stop and report — do not work around**:
- A Dockerfile/patch anchor doesn't match: never loosen the anchor or
  bypass the grep; report what upstream changed.
- Tests fail in an area your diff didn't touch: report, don't "fix" the
  test to green.
- The live Supabase schema doesn't match `supabase/migrations/` (a
  hand-run SQL change never made it into a migration file): reconcile
  with the maintainer before writing code against either.
- You'd need a secret/credential that isn't in the documented env vars.

**How to ask**: numbered options with your recommendation first and the
trade-off in one line each — never open-ended "what do you want?". If the
session is non-interactive, ship the safe subset and put the question in
the PR body under "Open questions" instead of guessing.

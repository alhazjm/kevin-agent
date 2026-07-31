# Handoff — Card Optimiser Build (v1)

**Session context**: This doc hands off the card-optimiser implementation
to a fresh Claude Code session after stream timeouts interrupted the prior
run. Everything needed to finish the PR is below — you should not need to
re-derive any architectural decisions.

## Branch state (as of handoff)

- **Branch**: `claude/card-optimiser-v1` (off `main` post-PR #22 merge).
- **Already committed on this branch**:
  - `9926a5a Fix IndexError on empty-string month + retry transient gspread 5xx`
    — production bug fixes (see "Bug fixes already shipped on this branch"
    below). User said "Bug fixes — do it in the next PR", so they're
    bundled into this PR.
- **Not yet touched**: card-optimiser code, skill, cron extensions, tests,
  docs, CLAUDE.md.

## Your task

Build out the card-optimiser per `docs/CARD-OPTIMISER-ARCHITECTURE.md`.
**Gate every tool behind sheet readiness** — the user hasn't populated the
`Cards` + `CardStrategy` tabs yet, so every tool must return
`{"status": "setup_required", "message": "..."}` until the tabs exist and
have at least one row (plus the `_default` CardStrategy row). The user
explicitly wants the code to ship now and activate later.

When done: open a PR (base `main`, head `claude/card-optimiser-v1`) titled
something like `Card optimiser v1 + bug fixes`. Do NOT push to any other
branch.

## Non-negotiables — read before coding

1. **Read `CLAUDE.md` top to bottom** before touching anything — especially
   the Dockerfile gotchas. Every tool you register MUST be added to the sed
   injection on Dockerfile line ~36 or the LLM never sees it. Currently 19
   tools; you're adding 5 → 24.
2. **Read `docs/CARD-OPTIMISER-ARCHITECTURE.md` in full.** Decisions
   already made — don't relitigate:
   - Category-keyed earn rates (not MCC). MerchantMap stays personal/
     merchant-name-keyed; `CardStrategy` stores earn rate per category.
   - One Hermes profile, second skill. NOT a separate Hermes Profile.
     Shared toolset with expense-tracker. `skills/card-optimiser/SKILL.md`
     is pure markdown — no separate tool file for the skill itself.
   - Post-cap nudge hooks into `handle_log_expense` via
     `card_optimiser.maybe_send_post_cap_nudge()`. That's the only
     cross-skill coupling. Keep it narrow.
3. **Don't touch the reply=journal experiment** in `skills/expense-tracker/
   SKILL.md` § "Journal Replies". It's a running 2-week experiment the user
   reviews at the 2-week mark. Leave it alone.
4. **Don't touch the sweep flow** shipped in PR #22 (Batch 4 Phase 1+2) —
   `WebhookLog` audit + `sweep_missed_transactions`. Independent concern.
5. **Sheets-client discipline** (CLAUDE.md § "Backwards compatibility"):
   use `_get_column_index(ws, header)` for column lookups — never hard-code
   column indices. Pattern is established; follow it.
6. **Tests**: add to `tests/test_card_optimiser.py` (new file). Target
   147 + ~15 new = ~162 passing. The existing `conftest.py` stubs
   `tools.registry` so register-decorated functions work in tests.

## Bug fixes already shipped on this branch (commit `9926a5a`)

Do NOT redo these — they're already committed. But mention them in the PR
body so reviewers know why the branch includes them:

- `get_spending_summary`, `generate_spending_report`, `read_transactions`,
  `write_insight`: changed `if month is None` → `if not month` so an
  empty-string `month=""` from an LLM tool call no longer crashes on
  `"".split("-")[1]`.
- `get_spreadsheet()`: wrapped in exponential-backoff retry (0.5 / 1 / 2
  seconds) for transient gspread statuses (429, 500, 502, 503, 504).
  Fails fast on 4xx so real config errors still surface. Root cause of
  the 2026-04-23 05:01 cron crash.

Write tests for these in the card-optimiser test file (or extend
`tests/test_expense_sheets_tool.py`):
- Test: `get_spending_summary(month="")` does not raise IndexError and
  returns current-month data.
- Test: `get_spreadsheet()` retries up to 3 times on 503, succeeds on
  3rd attempt (mock gspread APIError with a `response.status_code`).
- Test: `get_spreadsheet()` fails fast on 403 (non-transient).

## Sheet structure — what the user will populate

Two new tabs (user will paste these manually; `CardNudgeLog` auto-creates
on first nudge). Document them in `sheets-template/README.md` alongside
the existing tabs.

### Tab: `Cards`

Header row exactly:
```
card_id | display_name | payment_method_pattern | cycle_start_day | min_spend_bonus | notes
```

- `card_id`: Stable lowercase-hyphenated key (e.g. `dbs-altitude`).
  Referenced by `CardStrategy.primary_card_id` and `fallback_card_id`.
- `display_name`: Human-readable name shown in nudges.
- `payment_method_pattern`: Case-insensitive substring matched against
  `Transactions.Payment Method`. **Longest match wins** when multiple
  cards share a bank prefix. Must match exactly what the bank emails
  produce (e.g. `DBS/POSB card ending 1234`).
- `cycle_start_day`: Integer 1–31. Clamped to `min(day, days_in_month)`
  for months like Feb/Apr that have <31 days.
- `min_spend_bonus`: Optional minimum monthly spend to unlock the
  headline rate. `0` or blank = no minimum.
- `notes`: Free text.

### Tab: `CardStrategy`

Header row exactly:
```
category | primary_card_id | primary_cap | primary_earn_rate | fallback_card_id | fallback_earn_rate | promo_active_until | notes
```

- `category`: Must match a `Budget` tab category **exactly**, or the
  sentinel `_default`.
- `primary_card_id`, `fallback_card_id`: keys from `Cards` tab.
- `primary_cap`: Monthly $ cap at headline rate. `0` / blank = unlimited.
- `primary_earn_rate`, `fallback_earn_rate`: miles per dollar (mpd).
- `promo_active_until`: YYYY-MM-DD. Only set via `set_category_primary`
  override; blank for default strategy rows. Reverted by the nightly
  check (or lazy reversion when `plan_month` first runs after expiry).
- `notes`: Free text. Also stores `PREV:<snapshot>` when a promo
  override writes the prior config for later reversion.

**Sentinel row required**: exactly one row with `category = "_default"`.
Without it, unmapped-category transactions bubble a "no strategy" error.

### Tab: `CardNudgeLog` (auto-created, no manual setup)

Header row that the helper writes on first use:
```
timestamp | cycle_window | card_id | category | threshold | triggering_txn_id | fallback_card_id
```

Used to dedupe nudges within the same cycle — one (card_id, category,
threshold) tuple per cycle_window max.

## Implementation checklist

### 1. New module: `tools/card_optimiser.py`

Constants:
```python
CARDS_SHEET = "Cards"
CARD_STRATEGY_SHEET = "CardStrategy"
CARD_NUDGE_LOG_SHEET = "CardNudgeLog"
DEFAULT_STRATEGY_KEY = "_default"
```

Core functions (roughly, adjust signatures as you implement):

- `read_cards() -> list[dict]` — reads `Cards` tab, header-lookup via
  `_get_column_index`. Returns `[]` if tab missing.
- `read_card_strategy() -> list[dict]` — same pattern for
  `CardStrategy`.
- `_check_setup() -> dict | None` — returns `None` if both tabs exist and
  have ≥1 row AND CardStrategy has a `_default` row; otherwise returns
  `{"status": "setup_required", "message": "..."}` listing what's missing.
- `_find_card_by_payment_method(payment_method, cards) -> dict | None` —
  longest `payment_method_pattern` substring match wins (case-insensitive).
  Return None if no match.
- `_find_strategy(category, strategies) -> dict | None` — exact match
  first, else the `_default` row.
- `_cycle_window(cycle_start_day, as_of) -> (start_date, end_date)` —
  handles the 31-in-30-day-month edge by clamping. Tested.
- `_spend_in_cycle(card, category, as_of) -> float` — reads
  `Transactions`, filters by cycle window + payment-method substring +
  exact category match. Excludes `UNCATEGORIZED` and `backfill`.
- `maybe_send_post_cap_nudge(payment_method, category, amount, date)
  -> dict` — the one cross-skill hook. See § 3.

Five tool-facing functions (called by handlers in `expense_sheets_tool.py`):

- `get_card_cap_status(card_id=None, category=None, as_of=None) -> dict`
- `recommend_card_for(category, amount=None, as_of=None) -> dict`
- `plan_month(month=None) -> dict`
- `review_card_efficiency(month=None) -> dict`
- `set_category_primary(category, card_id, until_date=None,
  earn_rate=None, cap=None, fallback_card_id=None,
  fallback_earn_rate=None) -> dict`

Every one of these starts with `gate = _check_setup(); if gate: return gate`.

### 2. Dockerfile COPY line

Add right after the existing two tools copies:
```dockerfile
COPY tools/card_optimiser.py /app/hermes-agent/tools/card_optimiser.py
```

Without this COPY, the module is silently absent on Render.

### 3. Wire `maybe_send_post_cap_nudge` into `handle_log_expense`

In `tools/expense_sheets_tool.py`, inside `handle_log_expense`, after
`result["assistant_reply_policy"] = ...` is set and just before the
budget-category-ensure step. Pattern:

```python
try:
    from tools import card_optimiser
    nudge = card_optimiser.maybe_send_post_cap_nudge(
        payment_method=payment_method,
        category=category,
        amount=amount,
        date=date,
    )
    if nudge.get("sent"):
        result["card_nudge_sent"] = True
        result["card_nudge_detail"] = nudge
except Exception as exc:
    # Card-optimiser must never break log_expense. Swallow and log.
    result["card_nudge_error"] = str(exc)
```

The helper itself should:
1. Short-circuit if setup incomplete — return `{"sent": False, "reason":
   "setup"}`.
2. Resolve `payment_method` → card via longest-match.
3. Look up strategy for `category`.
4. Compute post-transaction MTD spend; check threshold (80% or 100%)
   crossing for the first time this cycle.
5. Check CardNudgeLog for prior (cycle, card_id, category, threshold) —
   skip if already fired.
6. **Silent-when-already-switched**: if prior 80% nudge fired AND the
   last N≥2 transactions in this category already used the fallback
   card, skip the 100% nudge (user is disciplined).
7. If nudge fires: send Telegram bubble via
   `tools.expense_sheets_tool._send_telegram_bubble` (or duplicate the
   helper — keep the dep light). Log to `CardNudgeLog`.

### 4. Register 5 tools in `tools/expense_sheets_tool.py`

Follow the existing register pattern. Each handler is thin:
```python
def handle_get_card_cap_status(args: dict, **kwargs) -> str:
    from tools import card_optimiser
    result = card_optimiser.get_card_cap_status(
        card_id=args.get("card_id"),
        category=args.get("category"),
    )
    return json.dumps(result)
```

Schema descriptions should tell the LLM to fall back gracefully when
`status == "setup_required"` — surface the message and stop.

### 5. Dockerfile sed injection

Extend line ~36. Current line ends with `"sweep_missed_transactions",`.
Append exactly these 5 names:
```
"get_card_cap_status", "recommend_card_for", "plan_month", "review_card_efficiency", "set_category_primary",
```

### 6. New skill: `skills/card-optimiser/SKILL.md`

Version `1.0.0`. Cover:

- **When to use**: user asks which card for what, any "cap/mpd/miles"
  question, the 1st-of-month briefing, the Friday summary extension.
- **Setup-gate behaviour**: when any tool returns
  `status: "setup_required"`, surface the message as ONE short line and
  stop. Do not invent card data.
- **Categorisation contract**: reuse the MerchantMap + Budget categories
  from expense-tracker. Don't redefine categories here.
- **Post-cap nudge contract**: describe that `handle_log_expense` sends
  the nudge automatically — this skill does NOT call the nudge helper
  directly. Just document that it happens.
- **Telegram formatting**: same Telegram-friendly rule as
  expense-tracker. No markdown tables.
- **Manual promo override flow**: user says "for April DBS is doing 10
  mpd on dining until 2026-04-30" → you call `set_category_primary`
  with `until_date="2026-04-30"`.

### 7. Cron extensions (`cron/setup-cron-jobs.sh`)

Extend (don't duplicate) two existing crons:

- **Friday 6 PM weekly summary** prompt: append a "cards on pace"
  section — for each card in CardStrategy, show `spent_in_cycle / cap`
  and flag any in `warning`. Call `get_card_cap_status()` for the data.
- **1st of month 9 AM monthly report** prompt: after the expense report,
  call `plan_month()` and `review_card_efficiency(month=<last_month>)`
  and surface both.

Both crons already pass `--skill expense-tracker`. Add `--skill
card-optimiser` to both.

### 8. `sheets-template/README.md`

Add three new tab sections (Cards, CardStrategy, CardNudgeLog) matching
the schemas above. Follow the existing tab section format (table + exact
header row in a code block).

### 9. `CLAUDE.md`

- Tool count: 19 → 24. Update the list in § Dockerfile gotchas item 2.
- Test count: 147 → (whatever, probably ~162).
- Skill list: add `card-optimiser` at version `1.0.0`.
- Under "When adding a new file — checklist": card_optimiser.py needed a
  new COPY line — that's a good worked example to mention.

### 10. Tests: `tests/test_card_optimiser.py`

Cover at minimum:

- **Setup gate**: `_check_setup()` returns error when Cards missing,
  when CardStrategy missing, when `_default` row absent, None when all
  present.
- **Card resolution**: longest `payment_method_pattern` wins (seed two
  cards with overlapping patterns, verify the longer one matches).
- **Strategy resolution**: exact match wins, falls back to `_default`.
- **Cycle math**: `_cycle_window(15, date(2026, 4, 22))` → (2026-04-15,
  2026-05-14). `_cycle_window(15, date(2026, 4, 10))` → (2026-03-15,
  2026-04-14). `_cycle_window(31, date(2026, 2, 15))` clamps to Feb 28.
- **MTD spend**: filters by cycle window, skips UNCATEGORIZED and
  backfill rows, sums the rest.
- **Cap status**: ok < 80%, warning 80–99%, capped ≥ 100%.
- **Nudge dedup**: CardNudgeLog hit for same (cycle, card, category,
  threshold) → skip.
- **Silent-when-already-switched**: prior 80% nudge + next 2 txns used
  fallback → skip 100% nudge.
- **Handler wrappers**: each of the 5 handlers passes args through
  correctly and JSON-encodes the result.

Plus the bug-fix tests called out in § "Bug fixes already shipped".

## How to verify before opening the PR

```bash
pip install pytest gspread google-auth cffi
pytest tests/
```

Expect all prior tests still passing + your new ones green. Then:

```bash
# Verify nothing broke the Dockerfile sed anchor
grep -c '"send_message",' Dockerfile   # must be > 0
# Verify all 24 tool names in the sed line
grep -o 'get_card_cap_status\|recommend_card_for\|plan_month\|review_card_efficiency\|set_category_primary' Dockerfile | wc -l
# Expect 5.

# Syntax check if node available
cp apps-script/Code.gs /tmp/c.js && node --check /tmp/c.js && rm /tmp/c.js
```

## PR body template

```
## Summary

- Build out the card-optimiser v1 per `docs/CARD-OPTIMISER-ARCHITECTURE.md`.
- Tool-gate every public function behind `Cards` + `CardStrategy` sheet
  setup — user populates manually.
- Also bundles two production bug fixes from the 2026-04-23 05:00 cron
  crash (commit 9926a5a).

## What's new

- `tools/card_optimiser.py` — 5 tools + post-cap nudge helper.
- `skills/card-optimiser/SKILL.md` v1.0.0.
- `Cards` / `CardStrategy` / `CardNudgeLog` tabs documented in
  `sheets-template/README.md`.
- Dockerfile: new COPY for `card_optimiser.py`, sed injection extended
  with 5 new tool names (24 total).
- Cron: 1st-of-month + Friday crons now pass `--skill card-optimiser`
  and include card briefing / scorecard prompts.

## Bug fixes (already on this branch, commit 9926a5a)

- `if not month` guard in get_spending_summary + generate_spending_report
  + read_transactions + write_insight (IndexError on empty-string month).
- Retry wrapper on `get_spreadsheet()` for transient gspread 5xx/429.

## Test plan

- [x] `pytest tests/` → ~162 passing (was 147).
- [x] Each tool returns `status: "setup_required"` when Cards /
  CardStrategy tabs absent or incomplete — verified by unit tests.
- [ ] **Manual after deploy**: user populates the two tabs (see
  `sheets-template/README.md`), then asks the bot "what's my card plan
  for dining?" — expects a clean answer, not a setup error.
- [ ] Manual: trigger a transaction that crosses 80% of a cap, verify
  the nudge bubble fires within a few seconds of the confirmation.

https://claude.ai/code/session_01AECCTYmb5vA62eTghKxGMD
```

## Reference material

- `docs/CARD-OPTIMISER-ARCHITECTURE.md` — architecture, decisions, tool
  specs, risk table, exit criteria for v1.
- `CLAUDE.md` — repo-wide gotchas. **Re-read the Dockerfile section
  before you commit** — the sed injection is the #1 silent-break site.
- `tools/expense_sheets_tool.py` — tool registration pattern to follow
  (see `sweep_missed_transactions` as the most recent example).
- `tools/sheets_client.py` — sheet I/O patterns. `_get_column_index` is
  the canonical column-lookup helper.
- `skills/expense-tracker/SKILL.md` — format & voice for the sibling
  skill. Don't duplicate its content; link conceptually.

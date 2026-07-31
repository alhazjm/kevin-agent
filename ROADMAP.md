# ROADMAP

Long-term direction for this Hermes deployment. Keep this honest — if a
milestone slips or a principle turns out wrong, rewrite rather than paper over.

## Current state (Apr 2026)

- **Skill markdown files**: 3 — `expense-tracker` v4.7.0 (primary),
  `budget-manager` v3.0.0 and `weekly-summary` v3.0.0 (both cron-invoked,
  sharing the same tool surface; no separate tool files)
- **Ingress**: Telegram bot + Gmail Apps Script HMAC webhook (DBS/UOB)
- **Egress**: Telegram (plus four scheduled cron deliveries)
- **Storage**: Google Sheets (append-only ledger, MerchantMap, Budget,
  Insights)
- **LLM**: OpenAI GPT (`gpt-5.4-nano` via OpenAI-compatible custom endpoint;
  `gpt-5-nano` for short/simple smart-routed turns)
- **Memory**: tier-2 files (`USER.md`, `MEMORY.md`, `SOUL.md`) + `/data/`
  persistent disk on Render
- **Cost**: $7/mo Render (Singapore) + Google Sheets free tier
- **Tool count**: 15 registered, all exposed to the Telegram gateway

> Note: the 3 skill files above validate *skill-prompt* multiplicity but
> not the multi-toolset routing envisioned in near-term step #2. The
> Miles/rewards skill is the first skill that will need its own tool
> registrations and per-skill toolset scoping in `cli-config.yaml`.

## Vision

Multi-skill personal assistant with **cross-domain memory**. One agent brain,
multiple life domains, one conversation surface per domain (Slack channels or
equivalent). Narrative layer in Obsidian; structured data in Sheets (today)
or Postgres (if productised). Eventually packageable for 5–50 users without
architectural rewrites.

This is **not** trying to be the "24/7 assistant across every channel" TikTok
framing. That conflates reach with leverage. The leverage is cross-domain
memory; reach is a gateway plugin.

## Principles

1. **Skills are the product.** Storage, LLM provider, and gateway are
   swappable infrastructure.
2. **Obsidian daily notes are the join key** across domains. Structured data
   answers "what"; daily notes answer "why and how it relates".
3. **Tier-2 stays small, tier-3 scales.** Never bloat `MEMORY.md` to store
   history — index it in FTS5 instead.
4. **No premature abstraction.** Ship a skill first, abstract when the
   second skill makes the duplication hurt.
5. **Single-tenant until 3+ non-me users exist daily.** Don't build auth,
   billing, or multi-tenancy on speculation.
6. **Event-driven or user-initiated.** No polling loops. Webhook in, cron
   out, user-initiated in between. This is what keeps API cost flat.

## Near-term (next 1–3 months)

Order matters — each step validates an architectural assumption the next
step depends on.

1. **Start Obsidian daily notes manually.** No agent integration yet. Just
   build the habit and accumulate narrative data that FTS5 will eventually
   index. Structure:
   ```
   Daily/ Merchants/ People/ Cards/ Principles/ Reviews/
   ```
   Template in `hermes-config/` once it stabilises.

2. **Miles/rewards skill.** New skill `card-optimiser`:
   - `Cards` tab in sheet with earn rate per MCC / merchant category / cap
   - Extend `MerchantMap` with `best_card` column
   - Tools: `recommend_card(merchant, amount)`, `log_miles_earned`,
     `miles_balance(card)`, `miles_expiry_watch`
   - Validates: multi-skill routing in `cli-config.yaml`, shared MerchantMap
     across skills, per-skill toolset scoping

3. **Migrate Telegram → Slack.** Telegram supergroups + topics are awkward
   solo. Slack free tier gives channel-per-domain cleanly. Channels:
   `#expenses`, `#cards`, `#journal`, `#agent`. Same container, new gateway.
   Validates: gateway abstraction (if it doesn't exist yet, build it here).

4. **FTS5 indexer.** Cron job on `/data/` that rebuilds
   `/data/fts5.db` from the Obsidian vault + historical sheet rows nightly.
   New tool: `recall(query, scope?)` callable from any skill. Validates:
   cross-skill memory.

## Mid-term (3–9 months)

5. **Fitness/sleep ingestion.** Apple Shortcuts automation POSTs HealthKit
   data to the same HMAC webhook pattern. Skill: `body-log`. Starts
   correlating sleep↓ → spending↑ type insights.

6. **Journal skill.** Voice note → transcription → daily note append.
   Telegram/Slack voice message in, markdown append to today's daily note
   out.

7. **Subscription auditor as a standalone skill.** Extract
   `detect_subscription_creep` + `write_insight` into their own skill with
   monthly cron report. Lower-value than 1-6 but cheap.

8. **Cross-skill `recall` in production.** Once 3+ skills exist, recall
   becomes the main UX: "when did I last …" questions span domains.

## Long-term (9–18 months) — productisation prep

Only if you want to share/sell. Resist until 5 friends are using it daily.

9. **Storage adapter refactor.** Today `expense_sheets_tool.py` calls
   `sheets_client.py` directly. Introduce a `Storage` protocol with methods
   like `append_transaction`, `lookup_merchant`, etc. Implementations:
   `SheetsStorage` (today), `PostgresStorage` (later). No behaviour change
   on this step — purely shape.

10. **Per-tenant namespacing.** Use **Hermes Profiles** — shipped in
    upstream v0.6.0, inherited by our fork. One Profile per tenant: its
    own `HERMES_HOME`, config, bot token, skills, memories, sessions,
    gateway process. Create via `hermes profile create <tenant_id>`,
    switch via `hermes -p <tenant_id>`. Profiles are the right primitive
    *only* for tenant isolation — don't use them for multi-skill within a
    single user (skills in the same profile share tools cleanly, which is
    what card-optimiser + expense-tracker do).

11. **Second storage backend.** Postgres on Render or Supabase. Sheets stays
    as an optional export/view — users like seeing their data in a grid.

12. **Billing + auth.** Last. Stripe + magic-link email, no more. Only once
    users are asking to pay.

## Insurance-today: what to change now for future-sale compatibility

**Nothing structural.** Specifically resist:

- Writing the storage adapter before skill #2 exists
- Adding tenant_id columns before a second tenant exists
- Building a web UI (Slack/Telegram is the UI)
- Replacing gspread with Postgres (sheets works, quotas aren't close)
- Adding auth middleware (no users yet)

The architecture is already close enough. The CLAUDE.md discipline of
`_get_column_index` (header-name lookups instead of hard-coded indices) is
exactly the kind of cheap future-proofing that pays off. Keep applying that
pattern as new sheet columns/tabs get added.

## Things NOT to do

- **Don't chase local LLM (Qwen on Pi).** Cloud inference cost is not your
  bottleneck. The enthusiast-YouTuber variant is cosplay, not engineering.
- **Don't build a web dashboard.** Slack + Sheets + Obsidian is the UI. A
  dashboard is 2 weeks of work for zero unique leverage.
- **Don't poll anything.** If it's not webhook-triggerable or cron-sensible,
  it doesn't belong here.
- **Don't let tier-2 files grow.** `MEMORY.md` creeping past ~200 lines is a
  signal to move things into FTS5.
- **Don't add a tool without updating the Dockerfile sed injection.**
  (See CLAUDE.md §Dockerfile gotchas.)

## Open questions

- **Obsidian sync mechanism** — Obsidian Sync ($4/mo, trivial) vs Syncthing
  (free, fiddly) vs iCloud (free, unreliable on Android). For
  productisation: probably vault-on-/data with a view API.
- **LLM provider** — OpenAI is now the default. Re-evaluate `gpt-5.4-nano`
  vs stronger models if routing quality degrades as tool count grows past 30.
- **Slack free tier** — 90-day message retention. Fine for us since
  everything of value is already indexed elsewhere. Revisit if team use.
- **Daily-note template location** — in `hermes-config/` so the container
  can offer a `new_day` tool that scaffolds today's note, or local-only?

## Milestones that mean "stop and re-evaluate"

- Tool count > 30 → route scoping becomes mandatory, not optional
- A second person using it daily → storage adapter becomes urgent
- Sheet row count > 50k → consider Postgres migration for the transactions
  tab (MerchantMap + Budget can stay in Sheets)
- Monthly Render cost > $30 → you've over-built; revisit the principles

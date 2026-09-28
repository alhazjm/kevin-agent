# Changelog

Human-readable, dated. Each entry is what changed *for someone running their
own copy*, not a commit log. The private deployment this repo is exported
from moves faster than this file; entries land here at each sync.

## 2026-09-27 — CI housekeeping

- **The scrub gate reads its pattern from a secret.** The gate now takes an
  extended regex from the repository secret `SCRUB_PATTERN` instead of a
  pattern written into `ci.yml`. It prints only file names on a match, scans
  `.github/` too, and fails if grep cannot run (an invalid regex no longer
  passes silently). Forks, fork pull requests and Dependabot runs receive no
  secrets, so the gate skips there. If you fork and later publish your copy,
  set your own `SCRUB_PATTERN` under Settings → Secrets and variables →
  Actions. `ci.yml` also now runs with a read-only token.
- **The weekly anchor check downloads through the GitHub API.** The
  anonymous `raw.githubusercontent.com` endpoint rate-limited the shared
  runners (HTTP 429) and failed the job before any check ran. The job now
  uses the authenticated contents API with the workflow token, and it keeps
  one report open, closing the reports for older upstream tags.
  `docs/UPGRADING-HERMES.md` step b uses the same download for the manual
  path. Nothing to do on your side.

## 2026-09-05 — Sync: hermes-agent 0.21.0, the token diet, and the build that stopped being fragile

**Upstream pin: hermes-agent 0.10.0 → 0.21.0** (four and a half months, ~19k
commits). Full account in
[docs/HERMES-0.20-MIGRATION-NOTES.md](docs/HERMES-0.20-MIGRATION-NOTES.md).
What it means for you:

- **Two build-time patches instead of five.** Three retired because upstream
  now does their job (buffered retry status, hidden codex-incomplete
  sentinel, no memory-flush agent on webhook sessions). The survivors moved
  to `agent/conversation_loop.py` and now emit hermes' own `NO_REPLY` silence
  token instead of an empty reply.
- **The `sed` tool injection is gone.** Tools reach the model through
  `platform_toolsets` in `cli-config.yaml` — and cron now *requires* its own
  entry there or every scheduled job silently loses every tool.
- **Slash commands need bundles.** hermes 0.20+ rejects any `/command` it
  does not recognise. `/log`, `/undo`, `/budget`, `/summary` are registered
  by four YAML files in `hermes-config/skill-bundles/`. Plain-text messages
  never needed them and still work.
- **Cron jobs say `[SILENT]`** when there is nothing to report; an empty
  response now counts as a soft failure.
- **Five default-on upstream features switched off** (background review,
  auto-titling, skill curator, LSP, model catalog) — they would have added
  background LLM calls, including one per ingested email.
- **Docker build fetches one commit, with retries**, instead of cloning
  upstream's full multi-gigabyte history — after GitHub rate-limited a build.
- **`[all]` no longer includes Telegram or edge-tts**; the Dockerfile
  installs `[all,messaging,edge-tts]` explicitly.
- A weekly GitHub Action now tests the newest upstream tag against every
  assumption above and opens an issue saying "safe to bump" or "anchor
  drifted in file X". Upgrading is a documented, self-service procedure:
  [docs/UPGRADING-HERMES.md](docs/UPGRADING-HERMES.md).

**Cost.** Measured on the private deployment before/after the August work
(ingest diet, slim webhook skill, flush fix, migration): tokens per webhook
call ~27k → ~14k; the 04:00 memory-flush bucket (~40% of all spend)
eliminated; prompt-cache hit rate 74%; total tokens per day −51%.

**Ingest.** End-to-end idempotency key (computed in Apps Script, carried in
the payload, deduped atomically on insert); near-duplicate advisory in the
confirmation bubble; audit-silence alarm; webhook pacing (at most two posts
per five-minute tick, 20 s apart — a queue that costs no tokens);
`frankfurter.dev` for FX; Brunei dollar converts at its SGD peg; a
`debugAudit()` diagnostic that live-tests the Supabase audit write and the
Telegram alert path. The Apps Script must use the **legacy `service_role`
JWT** for Supabase, not an `sb_secret_*` key — Supabase blocks secret keys
from browser-like user agents, and Apps Script's cannot be changed.

**Ledger.** Migration `0008_category_meta` — mark budget categories as
`fixed` (subscriptions, insurance, utilities) so they are excluded from
over-80% warnings and flagged only when over their usual amount. Sheet
export retries. Deterministic travel routing inside `log_expense` (in-tool
FX, opt-out flag, fixed-bill guard).

**Skills.** `expense-ingest` — a slim webhook-only subset of
`expense-tracker`, the only skill the ingest route loads (the full skill was
riding along in every API call). expense-tracker 5.10.1, card-optimiser
1.5.0, budget-manager 3.1.0, weekly-summary 3.1.0.

**Cron.** Six jobs (the daily-noon budget ping was retired; the 21:00 review
carries a deterministic attention block instead).

**Docs.** SETUP rewritten for the current code with a "what to sign up for"
table and no line-number references; AGENTS.md regenerated from the private
manual; four dev-only helper scripts removed; `budget_categories.json`
(which nothing read) removed; MIT licence added.

**Known, deliberately not fixed:** `MEMORY.md` and `USER.md` are read from
`HERMES_HOME/memories/` — the persistent disk — not from where the
Dockerfile copies them, so the shipped templates have never been in the
system prompt. `SOUL.md` does load. Fixing it changes agent behaviour and is
its own change. Also: the Docker image's SQLite is old enough that hermes
falls back to the slower journal mode (a warning at boot, five times);
harmless at this volume.

## 2026-08-02 — First public release

Exported from the private deployment at roughly its 94th pull request and
hand-scrubbed: fictional card numbers, merchants and people; tier-2 memory
files as `.example` templates; Google Sheets made optional (only the two
export tools need it); `supabase/schema.sql` generated from the migrations
so a fresh install is one paste; SETUP restructured around "the two setups".
37 tools, 4 skills, 587 tests, 7 migrations.

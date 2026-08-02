# docs/ — the reference shelf

Everything reference-shaped lives here. `AGENTS.md` stays at the repo root
because coding agents look for it there; `CLAUDE.md` is a short pointer at
it for the same reason.

| Doc | What it is | Status |
|---|---|---|
| [SETUP.md](SETUP.md) | First-run guide for a cloner: the five deployment surfaces, every credential, what is hardcoded, troubleshooting | **Start here** if you want your own instance |
| [CARD-OPTIMISER-ARCHITECTURE.md](CARD-OPTIMISER-ARCHITECTURE.md) | RFC for the card-optimiser feature (statement cycles, bonus caps, nudges, scorecard) | Live reference |
| [SWEEP-ARCHITECTURE.md](SWEEP-ARCHITECTURE.md) | RFC for the missed-transaction sweep (audit-log diff) | Live reference — the sweep reads the Supabase `webhook_log` table |
| [STATEMENT-RECON.md](STATEMENT-RECON.md) | The monthly e-statement → ledger reconciliation ritual behind `recon/` and `/statement-recon` | Live reference |

Schema reference lives in [`supabase/migrations/`](../supabase/migrations/)
— or [`supabase/schema.sql`](../supabase/schema.sql) if you want all of it
in one file. Column meanings are documented inline in the SQL.

### Deliberately not published

The deployed instance also carries card-strategy research: issuer T&C
extracts with source URLs, a rendered `pwa/strategy.html`, a source-hash
ledger, and a `card_strategy` seed SQL file. Those describe one person's
actual card holdings, so they are not in this repo — and they would be the
wrong answer for you regardless. `/card-tnc-review` in
[`.claude/skills/`](../.claude/skills/) builds the equivalents for whatever
cards you carry.

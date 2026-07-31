# docs/ — the reference shelf

Everything reference-shaped lives here. The three files that stay at the
repo root do so because paths elsewhere depend on them: `CLAUDE.md` /
`AGENTS.md` (the agent operating manuals — tooling reads them from root)
and `ROADMAP.md` (strategy + "Things NOT to do", referenced by both
manuals).

| Doc | What it is | Status |
|---|---|---|
| [SETUP.md](SETUP.md) | First-run guide for a cloner: the five deployment surfaces, every credential, what is hardcoded, troubleshooting | **Start here** if you want your own instance |
| [CARD-OPTIMISER-ARCHITECTURE.md](CARD-OPTIMISER-ARCHITECTURE.md) | RFC for the card-optimiser feature (statement cycles, bonus caps, nudges, scorecard) | Live reference |
| [SWEEP-ARCHITECTURE.md](SWEEP-ARCHITECTURE.md) | RFC for the missed-transaction sweep (audit-log diff) | Live reference — the sweep reads the Supabase `webhook_log` table |
| [STATEMENT-RECON.md](STATEMENT-RECON.md) | The monthly e-statement → ledger reconciliation ritual behind `recon/` and `/statement-recon` | Live reference |
| [HANDOFF-card-optimiser.md](HANDOFF-card-optimiser.md) | Session-resumption doc from the card-optimiser build | Historical — kept as a worked example of the handoff genre |

Schema reference lives in [`supabase/migrations/`](../supabase/migrations/)
(authoritative) and [`sheets-template/README.md`](../sheets-template/README.md)
(the legacy sheet-era column reference, still the best prose description of
each field).

### Deliberately not published

The deployed instance also carries card-strategy research: issuer T&C
extracts with source URLs, a rendered `pwa/strategy.html`, a source-hash
ledger, and a `card_strategy` seed SQL file. Those describe one person's
actual card holdings, so they are not in this repo — and they would be the
wrong answer for you regardless. `/card-tnc-review` in
[`.claude/skills/`](../.claude/skills/) builds the equivalents for whatever
cards you carry.

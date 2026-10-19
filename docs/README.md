# docs/ — the reference shelf

Everything reference-shaped lives here. `AGENTS.md` stays at the repo root
because coding agents look for it there; `CLAUDE.md` is a short pointer at
it for the same reason.

| Doc | What it is | Read it when |
|---|---|---|
| [SETUP.md](SETUP.md) | First-run guide: what to sign up for, every credential, every surface, what is hardcoded, a verification ladder, troubleshooting | **You want your own instance** |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How one purchase becomes a row (the sequence diagram), the pieces, the five skills, the three memory files, the stack | You want the system in your head before you change it |
| [BUILD-STORY.md](BUILD-STORY.md) | The build in five eras, each ended by something breaking, and why this is an agent and not a cron job with an LLM call | You want the reasoning, not just the result |
| [UPGRADING-HERMES.md](UPGRADING-HERMES.md) | How to move the upstream `hermes-agent` pin yourself: find the newest release, test the anchors locally, bump, and what to do when something drifted | A weekly issue from the anchor-check workflow says a new upstream tag exists |
| [HERMES-0.20-MIGRATION-NOTES.md](HERMES-0.20-MIGRATION-NOTES.md) | The worked example of an upgrade: what changed between hermes 0.10 and 0.21, why, and how the deployment works now — including the failures that would have been silent | You want to understand *why* the patches, bundles and config look the way they do |
| [CARD-OPTIMISER-ARCHITECTURE.md](CARD-OPTIMISER-ARCHITECTURE.md) | RFC for the card optimiser (statement cycles, bonus caps, nudges, scorecard) | You are adding a card or changing nudge thresholds |
| [SWEEP-ARCHITECTURE.md](SWEEP-ARCHITECTURE.md) | RFC for the missed-transaction sweep (audit-log diff) | You are changing ingest or the audit path |
| [STATEMENT-RECON.md](STATEMENT-RECON.md) | The monthly e-statement → ledger reconciliation ritual behind `recon/` | You want ground truth against the bank once a month |

Schema reference lives in [`supabase/migrations/`](../supabase/migrations/)
— or [`supabase/schema.sql`](../supabase/schema.sql) if you want all of it
in one file. Column meanings are documented inline in the SQL.

Two of these are dated engineering records rather than live instructions
(`CARD-OPTIMISER-ARCHITECTURE`, `SWEEP-ARCHITECTURE`): where they mention a
Dockerfile `sed` injection, read "`platform_toolsets`" — that mechanism
changed at the hermes 0.20 bump and the RFCs keep their history.

### Deliberately not published

The deployment this repo is exported from also carries card-strategy
research: issuer T&C extracts with source URLs, a rendered strategy page, a
source-hash ledger, and a `card_strategy` seed SQL file. Those describe one
person's actual card holdings, so they are not in this repo — and they would
be the wrong answer for you regardless. `/card-tnc-review` in
[`.claude/skills/`](../.claude/skills/) builds the equivalents for whatever
cards you carry.

# Kevin

[![CI](https://github.com/alhazjm/kevin-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/alhazjm/kevin-agent/actions/workflows/ci.yml)
[![Upstream anchor check](https://github.com/alhazjm/kevin-agent/actions/workflows/anchor-check.yml/badge.svg)](https://github.com/alhazjm/kevin-agent/actions/workflows/anchor-check.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

| "Wet Ink" | everything else in this repo is _toner_. this section is written by me |
|---|---|
| **Why I built this** | _(wet ink pending)_ |
| **Status** | _(wet ink pending)_ |
| **What I verified myself** | _(wet ink pending)_ |
| **What I didn't verify** | _(wet ink pending)_ |
| **Me vs AI** | _(wet ink pending)_ |

Tl;dr (What this does) - Your bank emails you every time you tap your card. Kevin reads that email, picks a category, writes a row into a Postgres ledger you own, and sends you one Telegram message you can reply to ("that's transport, not food") to correct it and teach it. About five minutes from tap to ledger, nothing to type. You can also just talk to it, and a small dashboard shows where the month is going. Built for Singapore: DBS, UOB and HSBC alert emails, SGD, and the miles game.

This is a working deployment's code, exported with the personal data scrubbed. Sample card numbers, merchants and emails are fictional, and the personal memory files ship as `.example` templates.

## What you can say to it

| You, in Telegram | Kevin |
|---|---|
| *"How much have I spent on food?"* | Queries the ledger and answers |
| *"$8 lunch at Toast Box"* | Logs it through the same flow as a bank email |
| *"Which card should I use at Cold Storage?"* | Best card for that merchant, given earn rates and how full each bonus cap is |
| *"Can I afford a $150 jacket?"* | Checks what is left in discretionary and says so |
| *"Reallocate $50 from Dining to Transport"* | Shows the change, waits for a yes, updates the budget |
| *"Alex owes me $20 for lunch"* | Opens an IOU; monthly totals correct themselves when it is repaid |
| *"rm33 lunch"* on a trip | Converts at today's rate and files it under the trip's budget |

It also messages first: a nudge seconds after the purchase that crosses 80% of a card's bonus cap, a one-screen review every evening, a summary on Fridays, a month-end report on the 1st, and a weekly sweep that stays silent unless a bank alert never reached the ledger.

The dashboard is two HTML pages with no build step. Open [`pwa/index.html`](pwa/index.html) from disk and it runs on sample data, before any setup.

## How

```
card tap → bank alert email → Gmail
Apps Script, every 5 min
  regex parse per bank · FX to SGD if foreign · idempotency key
  → audit row into webhook_log FIRST        (emails get marked read; there is no retry)
  → HMAC-signed POST /webhooks/expense-ingest
Kevin
  learned merchant mapping → LLM judgment   (trip routing is decided in the tool, never by the model)
  → insert, on_conflict=idempotency_key     ("duplicate" stops the flow here)
  → the TOOL sends the Telegram bubble, then exits the agent loop: exactly one message
you reply "that's transport, not food" → row edited, mapping learned
```

Three rules hold it together: **audit before send** (every parsed email is logged before the webhook fires, and a weekly sweep diffs that log against the ledger) · **dedup is atomic** (one key, computed byte-identically in Apps Script and Python, enforced by the database) · **fail loud** (the framework is pinned by SHA, and the build aborts if a patch anchor has moved).

## Deploy your own

Everything runs in accounts you own: your Gmail runs the script, your Supabase holds the ledger, your Render box runs the agent, your bot talks to you. There is no service in the middle. Two outbound flows to know about: transaction text goes to the LLM endpoint you configure (which can be a local one), and foreign-currency conversions send amount, currency and date to `frankfurter.dev`, never merchant text.

**With a coding agent.** Clone the repo, open it in Claude Code, Codex, Cursor or whatever you use, and paste:

```text
Read AGENTS.md, starting with "Walking someone through first-time setup", then docs/SETUP.md.
I want to deploy my own Kevin. Walk me through it one step at a time, and check each step
works before we move on.
```

The repo is written to be picked up cold. [`AGENTS.md`](AGENTS.md) tells the agent how to run the walkthrough, what it can do for you in the checkout, and what only you can do in a browser. No coding agent? A chat LLM that can read a GitHub repo works too: give it this repo's URL and the same prompt. It cannot run commands for you, but it can guide you.

**By hand.** [`docs/SETUP.md`](docs/SETUP.md) is the full runbook: every account, every credential, a verification ladder and a troubleshooting table. The short version:

1. **A private copy of this repo.** Not the Fork button: forks of a public repo are public, and section 0 of the runbook has you commit files with your name and card last-4s in them.
2. **Supabase.** Free project; put your email in `is_owner()`, then paste [`supabase/schema.sql`](supabase/schema.sql) into the SQL editor once.
3. **Telegram.** Two messages to @BotFather get you a bot token; your numeric user id gates who the bot listens to.
4. **Render.** Deploy the Dockerfile as a web service with a 1 GB disk and seven environment variables.
5. **The dashboard.** Paste your project URL and publishable key into two files, publish `pwa/` as a static site.
6. **Gmail.** Paste [`Code.gs`](apps-script/Code.gs) into a script on your own Google account, set its Script Properties, run `setupTrigger()`. No Google Cloud project needed. Or skip this step and log by chat.

Optional, later: your cards' earn rates and caps (until then every card tool politely returns `setup_required`), a Google Sheet backup, and a parser for a bank that is not DBS, UOB or HSBC. Budget an afternoon.

## Cost

About USD 7 a month for the always-on Render box. LLM usage is metered on top: categorisation runs on a nano-class model, and the original deployment measured roughly USD 1 to 2 a month at one person's volume (July 2026, before a change that halved token use). Supabase, Telegram, Gmail and the dashboard hosting sit in free tiers.

**For**: one person or one household in Singapore whose banks send transaction alert emails, who wants the ledger in a database they own and does not mind running a small service. **Not for**: anyone who wants an app to install and nothing to maintain, shared or multi-user books (one owner, one chat, by design), or a setup with no third-party cloud at all. Outside Singapore the pattern holds, but you will write your own email parser, and the home currency is SGD in code.

## Go deeper

- [docs/BUILD-STORY.md](docs/BUILD-STORY.md): the build in five eras, each one ended by something breaking, and why this is an agent rather than a cron job with an LLM call.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the sequence diagram, the five skills, the three memory files, the stack.
- [AGENTS.md](AGENTS.md): the operating manual for coding agents, and for humans, changing this repo. A catalogue of the mistakes that cost real debugging time here, and the rule that prevents each.
- [docs/UPGRADING-HERMES.md](docs/UPGRADING-HERMES.md): moving the [hermes-agent](https://github.com/NousResearch/hermes-agent) pin yourself. A weekly workflow tests the newest upstream release against every assumption this repo makes and files an issue saying safe or drifted.
- [docs/](docs/README.md) has the rest; [CHANGELOG.md](CHANGELOG.md) is dated and human-readable.

Tests: `pip install pytest gspread google-auth cffi`, then `pytest tests/ -q`. A couple of seconds, no network.

MIT. [LICENSE](LICENSE).

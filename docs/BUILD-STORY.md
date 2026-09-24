# The build, in five eras

The repo reads best as a story. Each era ended because something broke in a way the next era had to fix.

## 1 · Make it log

Gmail Apps Script regex-parses bank alert emails, HMAC-signs a payload, POSTs to the agent's webhook; the LLM picks a category; a row is appended; Telegram confirms. It worked on day one — and immediately double-replied to every transaction, because the confirmation is sent by the *tool* and the model couldn't reliably be prompted into silence. The eventual fix wasn't a better prompt: it's a build-time patch to the agent loop ([`deploy/patches/suppress_reply_on_silent_tools.py`](../deploy/patches/suppress_reply_on_silent_tools.py)) that exits when a tool result carries `assistant_reply_required: false`. Knowing when to stop talking to the model and start writing code around it became the theme of the whole project.

## 2 · Make it not lose money

An agent that touches a ledger must fail loud, never silent. This era added:

- **Idempotency keys** — 16-hex SHA-256 of `date|merchant|amount|payment_method`, computed byte-identically in Apps Script (JS) and Python, with pinned parity tables on both sides so drift fails tests instead of silently double-logging real money. When two genuinely identical purchases in one day collided (a $7.80 food-court double-charge — the second one was eaten as a "duplicate"), the transaction *time* joined the key — appended only when present, so every previously stored key stayed valid. The key now travels end to end: computed once in the Apps Script, carried in the payload, deduped atomically on insert.
- **Audit-before-send** — every parsed email is written to a durable webhook log *before* the webhook fires; a weekly sweep diffs that log against the ledger and reports anything the LLM or API dropped.
- **Fail-loud deploys** — upstream hermes-agent is pinned by SHA; the build-time patches abort the build if the anchor they key on has moved.

## 3 · Make it know my life

Domain intelligence no off-the-shelf app has:

- **Card optimiser** — Singapore cards run on *two different clocks*: statement cycles cut mid-month (per card), while issuers' bonus caps run on *calendar* months. The card tools track both, recommend the right card per merchant, and nudge at log time — seconds after the purchase that crossed 80%/100% of a cap, when switching cards still matters.
- **Travel mode and trip pots** — a transaction that carries a foreign-currency signal during an active trip is routed to the trip's budget *by the tool*, deterministically, and tagged `[bucket:X]`; per-bucket alerts fire at 80%/100%. A trip's *funded* pot is derived from `[trip:]`-tagged top-ups, never from the planned envelope. Manual foreign entries ("rm33") are converted in the tool and counted; the model never routes.
- **Lending** — IOUs are real ledger rows, so monthly totals self-correct when someone pays you back instead of quietly overstating what you spent.
- **Merchant learning** — corrections (never first sights) become mappings; longest match wins; multi-category merchants like supermarkets stay default-then-reply.
- **Fixed vs variable bills** — subscriptions, insurance and utilities sit at 100% of budget by design. They are excluded from "over 80%" warnings and surface only when they come in *over their usual amount*.

## 4 · Give it a real database and a face

The Google Sheet had been the database from day one — the right call until it wasn't. This era moved the ledger to **Supabase Postgres** (atomic dedup via `ON CONFLICT DO NOTHING`, row-level security, the Sheet demoted to an optional nightly read-only export for humans), and shipped the **dashboard**: two HTML pages, no framework, no build step. Email-OTP auth, RLS-scoped reads, client-side stat calcs, and an in-app budget editor.

Having a real database made derived features cheap. Subscription detection moved from "merchant repeats" to *billing behaviour* — same category, once a month, tight day-of-month spread, near-flat amount — because bank strings carry per-charge reference codes that make merchant matching useless.

The face also turned out to be the attack surface: the self-sent iPhone-Shortcut ingest path was anchored to `from:me` (without it, anyone who knew the inbox address could mail a crafted body and have it signed and logged as a real transaction), a text sink in the insights renderer was escaped, and `supabase-js` was vendored at a pinned version instead of loaded from a floating CDN tag on a page that holds an authenticated session.

## 5 · Make it cheap, and make it current

Two concurrent bank emails once tripped a fresh OpenAI account's tokens-per-minute ceiling. Measuring where the tokens went (a per-call usage log, [`deploy/patches/log_llm_usage.py`](../deploy/patches/log_llm_usage.py)) found that ~40% of all spend was a memory-flush agent replaying every webhook session at 4 AM for nothing, and that every ingest carried the full 10k-token skill plus 5k tokens of tool schemas it never used. The diet: a slim webhook-only skill, a webhook toolset of one, pacing in the Apps Script (two posts per tick, 20 s apart — a queue that costs no tokens), and a fix for the flush. Tokens per webhook call ~27k → ~14k; the 4 AM bucket gone; prompt-cache hit rate 74%; total tokens per day −51%.

Then the framework moved: hermes-agent 0.10 → 0.21 in one bump, ~19,000 upstream commits. Four of five patches stopped applying, the tool-injection trick had been silently doing nothing for months, cron started allow-listing tools per platform (every scheduled job would have lost every tool, with no warning), and "say nothing" changed meaning (an empty reply became a warning bubble). Three patches were deleted because upstream now did their job. The whole thing is written up in [docs/HERMES-0.20-MIGRATION-NOTES.md](HERMES-0.20-MIGRATION-NOTES.md), and the lesson became infrastructure: a weekly [anchor-check workflow](../.github/workflows/anchor-check.yml) tests the newest upstream tag against every assumption this repo makes and opens an issue saying "safe to bump" or "this drifted". Upgrading is now a [documented, self-service procedure](UPGRADING-HERMES.md).

---

## Why an agent and not a cron + LLM call?

The "ChatGPT-summarises-my-CSV-once-a-week" version is the obvious lighter take. It's not what I want:

- **Bank emails are event-driven.** Webhook in seconds, not "we'll see at the next cron tick."
- **No persistent identity.** A cron call starts from zero every run — no learned merchant→category mappings, no "you've already nudged me about Grab this week."
- **No writes, no tools.** Pure summarisation can't log a manual expense, edit yesterday's row, or update the budget.
- **No interactive surface.** You can't ask *"can I afford a $150 jacket?"* against a one-shot summary.
- **Prompt changes are unversioned.** A cron prompt drifts silently; a versioned skill markdown changes deliberately.

Cron is one of *several* invocation paths here (six scheduled jobs), not the whole product.

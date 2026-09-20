# apps-script/ — Gmail → webhook ingest

`Code.gs` is a Google Apps Script that runs on **your own Gmail account**,
every five minutes: it finds unread bank-alert emails, parses them with
per-bank regexes, converts foreign amounts to SGD, writes an audit row,
signs the payload with HMAC-SHA256, and POSTs it to the agent's webhook.
Full setup is [docs/SETUP.md section 5](../docs/SETUP.md#5-apps-script--gmail-ingest);
this page is the shape of the thing.

**It needs no Google Cloud project.** A script on your account reading your
own mail runs under your own consent — the only Google Cloud step in this
repo is the optional Sheet backup, which is unrelated.

## What ships where

| Piece | Deploys how |
|---|---|
| `Code.gs`, `appsscript.json` | `clasp push` from this directory, or paste into the editor at script.google.com. **Merging a PR does not deploy this.** |
| Script Properties (secrets) | Set by hand: Project Settings → Script Properties |
| Triggers | Run `setupTrigger()` once from the editor (and `setupRestartTrigger()` if you want the nightly restart) |

## Two constants you must edit in `Code.gs`

- `WEBHOOK_URL` — `https://<your-service>.onrender.com/webhooks/expense-ingest`.
  Plural `/webhooks/`; the path is the route name in `cli-config.yaml`.
- `RENDER_SERVICE_ID` — your Render service id (`srv-…`), used only by the
  optional nightly restart. Delete `setupRestartTrigger` and
  `restartRenderService` if you do not want it.

## Script Properties

| Property | Why | Required |
|---|---|---|
| `WEBHOOK_HMAC_SECRET` | Signs `X-Webhook-Signature`; must equal Render's env var byte-for-byte | yes |
| `SUPABASE_URL` | Audit rows go to the `webhook_log` table, which the weekly sweep reads | yes |
| `SUPABASE_SERVICE_KEY` | **The legacy `service_role` JWT (starts `eyJ`), not an `sb_secret_*` key.** Supabase refuses secret keys from browser-like user agents, and Apps Script's cannot be changed — a wrong key here fails silently into the Sheet fallback | yes |
| `SPREADSHEET_ID` | Fallback audit target when the Supabase write fails | recommended |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | FX-failure and degraded-audit alerts | recommended |
| `RENDER_API_KEY` | The nightly restart (`rnd_…`) | optional |

Run `debugAudit()` from the editor after setting them: it prints every
property's status, does a real audit insert (deleted immediately) and a real
Telegram send, and names the fix when something is wrong. Both failure paths
are silent otherwise.

## Behaviour worth knowing before you touch it

- **Two Gmail searches, deliberately.** `GMAIL_QUERY` (bank senders + a
  subject filter) and `SHORTCUT_QUERY` (`from:me subject:"YouTrip Transaction"`,
  a self-sent iPhone-Shortcut source). Merging them into one boolean query
  silently broke matching once. `from:me` is also a forgery guard: without
  it anyone who knows the inbox address could mail a crafted body and have
  it signed and logged as a real transaction.
- **Audit before send, no retry.** Emails are marked read after processing.
  The audit row is written *before* the webhook fires so a dropped POST is
  visible to the sweep instead of lost.
- **Pacing.** At most `MAX_WEBHOOKS_PER_TICK` (2) posts per tick,
  `WEBHOOK_SPACING_MS` (20 s) apart; the rest stay unread for the next tick.
  Each POST is its own concurrent agent session, and two at once exceeded a
  fresh OpenAI account's tokens-per-minute ceiling.
- **Currency is captured dynamically** (`[A-Z]{3}`), non-SGD converts via
  `frankfurter.dev` (ECB rates, no key), the Brunei dollar at its 1:1 SGD
  peg. On FX failure: audit row `fx_failed`, no webhook, one Telegram line,
  you log it by chat.
- **The idempotency key** (`sha256("date|MERCHANT|amount|payment_method[|time]")`,
  first 16 hex) is computed here and must match
  `tools/sheets_client._compute_idempotency_key` byte-for-byte — it is what
  makes a retried webhook a no-op instead of a double charge. Run
  `testIdempotencyKeyParity()` from the editor after any change; the pinned
  values come from the Python suite.

## Adding a bank

There are four parsers (`parseDBS`, `parseUOB`, `parseHSBC`,
`parseYouTrip`), each tied to one exact alert layout. Yours will differ.
The parser and its byte-for-byte Python mirror in
`tests/test_email_parser.py` change in the same commit — the test file is
the executable spec. `.claude/skills/new-alert-source` is the step-by-step
checklist if you use Claude Code; the steps are readable as plain prose
otherwise.

If you would rather not touch regexes at all, skip this directory entirely
and log by chat — "$8 lunch at Toast Box" goes through the same
categorisation flow.

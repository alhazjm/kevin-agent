---
name: new-alert-source
description: Onboard a new transaction-alert email source (new bank card, iPhone-Shortcut card, supplementary card) into the Gmail → webhook → Kevin pipeline. Run with /new-alert-source when the first live sample email exists.
---

# New Alert Source

The repeatable build sequence behind every alert source (DBS, UOB, HSBC
PR #55, YouTrip-via-Shortcuts). One hard precondition and eight steps.
The exemplar to copy is the HSBC parser commit (PR #55).

**Precondition — a real, live sample email.** Never build a parser from
a screenshot of a template or a hand-typed example: field formats
(amount decimals, currency symbol vs code, date shape, merchant casing,
trailing punctuation) only exist in the real thing. If the user hasn't
got one yet, stop and tell them exactly what to do (make a real
transaction; for iPhone-Shortcut sources, a manual test-run produces
EMPTY variables — only a genuine Wallet transaction fills Merchant and
Amount, and the automation must be set to Run Immediately). Ask them to
paste the raw body or forward it. Regexes will be pinned to this one
sample — say so in the PR body and ask the user to verify the first
live bubble after deploy.

## Steps

1. **Classify the sample.** Sender address, subject shape, body format
   (HTML label/value table vs plain text vs key: value lines), which
   fields exist (merchant, amount, currency, date, time, card last4),
   and their exact formats. Note anything absent (e.g. no time → the
   idempotency key falls back to the legacy 4-field form).
2. **Parser branch in `apps-script/Code.gs`.** A `parseXxx(body)`
   function with anchored regexes; merchant cleanup (trim trailing
   `[-.\s]+` like HSBC); currency captured dynamically (`[A-Z]{3}` —
   NEVER hard-coded SGD, that dropped foreign txns once, 2026-04-26);
   date via string arithmetic only (M5 — no `new Date()` round-trips);
   a `payment_method` string for the payload (e.g. "HSBC card ending
   1357", "YouTrip Card"). Wire the dispatch on the sender/subject and
   extend `GMAIL_QUERY`. For self-sent sources (Shortcut emails from
   the user's own address) anchor the query and dispatch on the fixed
   SUBJECT prefix, not the sender, so unrelated self-mail never parses.
3. **Mirror the parser in `tests/test_email_parser.py`** (M16): a
   byte-for-byte Python port of the same regexes, plus tests for the
   real sample's shape, format variants, and the date helper. If keying
   changes at all, M17 applies — parity pins both sides (that is
   ask-first territory).
4. **Non-SGD sources**: confirm the FX path applies (frankfurter
   convert, `orig:` note, audit row on failure). Shortcut sources fired
   abroad will arrive in local currency — this is what feeds travel
   mode, don't bypass it.
5. **`cards` table row** (hand-maintained, Supabase Studio): card_id,
   display_name, `payment_method_pattern` matching step 2's string
   exactly, `cycle_start_day` (confirm off a statement; 1 = calendar
   month for wallet-style sources), caps/min-spend only if the card has
   them. Give the user the exact `insert`/`update` SQL in the PR body.
   An EMPTY pattern is safe (optimiser skips it) — fill it only when
   the payment_method string is known from the live sample.
6. **Config docs**: `hermes-config/USER.md` card table row;
   `hermes-config/MEMORY.md` payment-method example if the string shape
   is new. Small — these load every turn (M18).
7. **PWA**: nothing needed for basic tiles (the cards fetch picks up
   the new row); livery only if github.com/alhazjm/cards has a matching
   entry — otherwise the paper fallback renders.
8. **PR body must state the manual steps**: this change deploys via
   **Apps Script, not the container** — merging does nothing for the
   parser. List: paste/`clasp push` Code.gs, any new Script Properties,
   the cards-table SQL, "mark the sample email unread to ingest it",
   and "verify the first live bubble — regexes are pinned to one
   sample". Run the suite; state the count.

## Hard rules

- Steps 2 and 3 land in the SAME commit, always (M16).
- Never touch `computeIdempotencyKey` casually — if the source lacks a
  field the key needs, use the documented fallbacks; formula changes
  are ask-first (M17).
- One parser per source; do not generalise existing parsers to cover a
  new source "while you're in there".
- Best-effort sources (iPhone Shortcuts: Apple-Pay taps only, no
  retry) get that honesty written into the PR body and, if material,
  `docs/STATEMENT-RECON.md` stays the ground-truth pointer.

---
name: card-tnc-review
description: Quarterly re-verification of card T&Cs against official issuer sources — diffs the facts the nudges depend on, skips unchanged sources by hash, sweeps for promos. Run with /card-tnc-review.
---

# Card T&C Review

Keeps the numbers the agent asserts honest against what the issuers
actually publish: bonus caps, min-spends, pool splits, partner/whitelist
merchant lists, exclusions. Those numbers live in
`tools/card_optimiser.py` (nudge thresholds and merchant patterns),
`pwa/index.html` (the card meters), and your Supabase `cards` /
`card_strategy` rows. Runs in Claude Code (WebFetch/WebSearch available);
nothing here touches the Render deployment directly.

**Cadence**: quarterly, or immediately after any nudge or PWA figure looks
wrong. Trigger it by typing `/card-tnc-review`.

## The ledger is the state

`docs/CARD-TNC-LEDGER.md` holds, per source: the URL, a **facts block**
(the extracted numbers/lists we depend on, in canonical form), a SHA-256
hash of that facts block, and the last-verified date. The hash is over
the *extracted facts*, never the raw HTML/PDF bytes — issuer pages churn
markup weekly; the facts are what matter.

That file is **not in this repo** — it describes whoever's cards are
actually in play, so it is created on your first run (see "Baseline mode"
below) and is yours from then on. Same for any rendered strategy page you
choose to keep alongside it.

## Procedure

1. Read `docs/CARD-TNC-LEDGER.md`. For each source, WebFetch the URL and
   re-extract ONLY the watched facts listed in its entry (caps,
   min-spends, pool splits, partner/whitelist lists, exclusions,
   effective dates). Normalise: one fact per line, sorted, amounts as
   plain numbers.
2. **Skip gate**: hash the new facts block. Same hash as stored →
   update `last_verified` only, move on. This is what makes a quiet
   quarter nearly free.
3. **Changed** → produce a side-by-side diff of the facts block and STOP
   to present it before editing anything downstream. Changes to caps,
   min-spends, or pool structures are **ask-first** (they change nudge
   thresholds and PWA meters: `tools/card_optimiser.py`,
   `pwa/index.html`, `supabase` `cards`/`card_strategy` rows). After the
   user confirms, update in the same PR: the ledger entry, any strategy
   doc or page you keep, and any seed SQL — and list the live-table
   `update` statements they must run in the PR body.
4. **Promo sweep** (secondary, report-only): WebSearch each card name +
   "promotion"/"nerf", limited to the rewards-blog sources you trust,
   since the last run date; summarise anything material. Never auto-apply
   promos — they are time-boxed and the ledger tracks structural T&Cs
   only.
5. **Baseline mode**: a source with no stored hash gets its facts block
   captured and recorded without diffing. On a fresh clone that is
   *every* source — create `docs/CARD-TNC-LEDGER.md` from scratch, one
   entry per card you actually hold, and say so in the summary.
6. Output: a table — source · verified/changed/baseline · one-line note —
   plus the diff details for any changes. If nothing changed anywhere,
   say exactly that in one line.

## Hard rules

- Official issuer pages/PDFs are the canonical sources; blogs are news
  leads only — a blog claim that contradicts the issuer page means "check
  the issuer page again", never "update the ledger from the blog".
- Never edit `docs/CARD-TNC-LEDGER.md` facts blocks without a fetched
  source in hand this session.
- A fetch failure is reported per-source ("could not verify"), never
  treated as "unchanged" — stale `last_verified` dates must stay stale.
- If an issuer PDF is image-only or paywalled, note it and ask the user to
  save a copy into `samples/` (gitignored) for manual review.

# Statement reconciliation — the monthly ritual

Repo-side tooling that diffs the DBS and UOB credit-card e-statements
against the Supabase ledger once a month. It runs on your own machine (via
Claude Code or a plain terminal) — it is NOT a hermes tool: nothing here
ships in the Render container, so there is no Dockerfile COPY line, no sed
injection, no registry entry.

## Why

The webhook path can miss transactions (email parser gaps, FX failures,
new banks) and manual logging can drift (typos, forgotten cash-back
reversals). The statement is the bank's ground truth; reconciling against
it monthly bounds how far the ledger can silently drift. Sam's
supplementary UOB card (****-9753) never emails us at all — its rows enter
the ledger only through this ritual, as `source='backfill'`.

## The ritual

1. Download the two PDF e-statements (DBS cuts ~23rd, UOB ~12th) and drop
   them into `samples/` at the repo root. `samples/` is gitignored — real
   statements carry full card numbers and must never be committed.
2. Run, from the repo root:

   ```
   py -V:3.11 recon/recon.py samples/DBS-estatement.pdf samples/UOB-estatement.pdf
   ```

   First run only: `py -V:3.11 -m pip install pypdf` (the sole
   dependency, imported lazily for PDF text extraction).

   Credentials come from the repo-root `.env` (`SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY`) — already present for the PWA/migration
   tooling. The script never prints them.
3. Read the report. Every card section first proves its checksum
   (previous balance − payments + new debits must equal the printed
   total, to the cent); a parser that cannot prove its sums aborts loudly
   instead of producing a plausible-but-wrong diff. Then, per section:
   - **matched** — statement row found in the ledger (amount within
     $0.01, date within ±3 days for post-date lag, merchant substring
     match). No action.
   - **amount_mismatch** — same merchant and date window, different
     amount. Usually a tip adjustment or FX settlement drift; fix the
     ledger row via Kevin (`edit_expense`) if it matters.
   - **statement_only** — on the statement, missing from the ledger. The
     report prints a ready-to-review SQL `INSERT` per row
     (`source='backfill'`, category left as `Miscellaneous` for you to
     correct). Review, adjust the category, then run by hand in Supabase
     Studio — the script never executes writes.
   - **ledger_only** (global list) — in the ledger, not on these
     statements. Cash spends, YouTrip/debit rails, and not-yet-posted
     transactions land here naturally; only investigate rows that
     *should* have been on a card statement.
4. Act on diffs via Kevin (Telegram: edit/delete) or Supabase Studio
   (inserts), then re-run to confirm a clean report if you changed
   anything.

Backfill rows are deliberately INCLUDED in the ledger fetch — recon is
the documented exception to M14 (statement imports are exactly what is
being verified here).

## Files

- `recon/statement_parsers.py` — deterministic DBS/UOB text-layer parsers
  with the checksum gate. The module docstring is the layout contract
  (row shapes, FX blocks, page furniture, the © mojibake, year
  inference). Parsers take extracted TEXT; `pdf_to_text()` is the only
  pypdf touchpoint.
- `recon/recon.py` — the runner: .env → parse → fetch ledger window via
  PostgREST (stdlib urllib) → diff → report + SQL suggestions.
- `tests/test_statement_recon.py` — synthetic-fixture tests (invented
  merchants, same physical layout; real rows never appear in tests).
- `.claude/skills/statement-recon/SKILL.md` — lets a Claude Code session
  run the ritual and help triage the output.

## Known limits

- A statement layout change (new bank template, new section type) fails
  loudly at the checksum or a parse error naming the line — extend the
  parser, don't loosen the gate.
- HSBC is a third bank with no parser yet (statement ~20th); rows on it
  are invisible to this ritual until one is written.
- Duplicate same-day same-amount statement rows match the ledger
  greedily (closest date first); two identical rows against one ledger
  row correctly leave one `statement_only`.

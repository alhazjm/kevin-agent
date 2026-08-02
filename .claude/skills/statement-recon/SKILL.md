---
name: statement-recon
description: Run the monthly DBS/UOB statement reconciliation against the Supabase ledger and help triage the diff. Use when the user says "reconcile my statements", "run recon", or drops e-statement PDFs into samples/.
---

# Statement reconciliation

The deterministic script does ALL the work — your job is to run it
exactly as written, relay its report, and advise on triage. Do not
re-implement parsing or diffing logic in-session, and never write to the
ledger yourself.

## Run

1. Confirm the two PDFs exist in `samples/` at the repo root (ask the user to
   drop them in if not — `samples/` is gitignored on purpose; never
   commit statements or copy their contents into files).
2. From the repo root:

   ```
   py -V:3.11 recon/recon.py samples/DBS-estatement.pdf samples/UOB-estatement.pdf
   ```

   (Adjust filenames to whatever is actually in `samples/`; the script
   sniffs the bank from the content. If pypdf is missing it will say so —
   install with `py -V:3.11 -m pip install pypdf`.)
3. Credentials load from the repo-root `.env` automatically. Never print
   `SUPABASE_URL` or `SUPABASE_SERVICE_KEY`.

## If it fails

- **ChecksumError naming a section** — the parser could not prove that
  section's rows sum to the printed total. This is the gate working, not
  noise. Show the user the exact error, look at the named section in the
  statement text (`recon/statement_parsers.py` docstring documents every
  known row shape), and fix the PARSER in a proper branch/PR. Never
  hand-edit totals or loosen the checksum to make the run pass.
- **Missing env vars / pypdf** — relay the script's message verbatim.

## Triage the report

- **matched** — no action; just report the counts.
- **amount_mismatch** — show both sides. Usually tips/FX settlement
  drift. If the ledger should change, the user does it via Kevin (Telegram
  edit) or Supabase Studio — summarise what to change, don't do it.
- **statement_only** — the report includes a ready-to-review SQL INSERT
  per row (`source='backfill'`, category defaulted to Miscellaneous).
  Help the user pick the right category for each (Budget-tab names, cited
  exactly), then hand him the corrected SQL to run in Supabase Studio.
  The script never executes writes and neither do you.
- **ledger_only** — expected for cash / YouTrip / debit rails and
  not-yet-posted card txns. Only flag rows that should have appeared on
  one of the parsed statements.

Full ritual doc: `docs/STATEMENT-RECON.md`.

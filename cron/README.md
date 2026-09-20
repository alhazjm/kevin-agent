# cron/ — the six scheduled jobs

`setup-cron-jobs.sh` creates the jobs with the `hermes cron` CLI. It runs
**once per persistent disk**, on the first container boot, gated by the
marker `/data/cron/.seeded` (see `deploy/start.sh`). After that the jobs
live on the disk and survive deploys.

| Schedule (container `TZ`) | Job | Silent when |
|---|---|---|
| `0 21 * * *` daily 21:00 | Evening review: over-80% variable categories and over-usual fixed bills, one insight, completed IOU repayments, a journal prompt | never — always one message |
| `0 18 * * 5` Friday 18:00 | Weekly summary + "cards this month" | never |
| `0 9 1 * *` 1st, 09:00 | Last month's report + this month's card plan + last month's card scorecard | never |
| `0 22 * * 0` Sunday 22:00 | Sweep: diffs the webhook audit log against the ledger | nothing missed → `[SILENT]` |
| `0 3 * * *` daily 03:00 | Rebuild the Google Sheet from the ledger (skipped quietly if no Sheet is configured) | success → `[SILENT]` |
| `0 7 1 1 *` 1 Jan, 07:00 | Freeze last year into an `Archive-<year>` Sheet tab | never — one line |

Expressions are wall-clock in the container's `TZ` (`Asia/Singapore` in the
shipped Dockerfile). To run in your own zone change `ENV TZ` in the
Dockerfile — do **not** convert the expressions to UTC, and do not add
timezone-aware datetimes to the Python (all datetime code is deliberately
naive).

## Three things hermes 0.20+ changed here

- **Cron tools are allow-listed per platform.** The `cron:` entry under
  `platform_toolsets` in `hermes-config/cli-config.yaml` is the *only*
  thing that puts the expense tools in front of these jobs. Remove it and
  all six jobs run with zero tools — and nothing warns. `hermes cron create`
  has no toolset flag, so this cannot be set in the script.
- **Silence is a token, not an empty reply.** A job with nothing to report
  must answer exactly `[SILENT]`. An empty response also suppresses
  delivery, but is booked as a soft failure that feeds a "this job has
  failed N runs in a row" nudge.
- **Cron loads the memory files** (`SOUL.md` etc.) into every run now, the
  same as a chat turn.

## Changing the roster

The script only *creates* jobs. Editing it and redeploying changes nothing
on a disk that is already seeded, and deleting the marker alone gives you
duplicates. The procedure, in the Render shell:

```bash
hermes cron list                 # note every id
hermes cron remove <id>          # for each one
rm /data/cron/.seeded
```

then restart the service. `start.sh` sees no marker and seeds the current
script. Subcommands available: `list create edit pause resume run remove
status tick` — there is no `delete`.

To run one job by hand (for example to prove the export works):
`hermes cron run <id>`.

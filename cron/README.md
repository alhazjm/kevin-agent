# Cron Jobs

## Scheduled Jobs

| Schedule | Time | Description |
|---|---|---|
| `0 21 * * *` | Daily 9 PM | Spending review + daily insight + IOU offset sweep + journal prompt |
| `0 18 * * 5` | Friday 6 PM | Weekly summary + cards-on-pace |
| `0 9 1 * *` | 1st of month 9 AM | Monthly report + card plan + last month's efficiency scorecard |
| `0 12 * * *` | Daily noon | Silent budget check (speaks only when a category is over 80%) |
| `0 22 * * 0` | Sunday 10 PM | Sweep for missed transactions (silent when nothing missed) |
| `0 3 * * *` | Daily 3 AM | Sheet export from Supabase (silent; skipped entirely if the Sheet is not configured) |
| `0 7 1 1 *` | Jan 1, 7 AM | Yearly cold-storage archive tab |

Cron expressions are wall-clock against the container's `TZ` (`Asia/Singapore`
in the shipped Dockerfile). Do not convert them to UTC — change `ENV TZ`
instead if you want a different zone.

## Setup

```bash
bash cron/setup-cron-jobs.sh
```

## Management

```bash
hermes cron list              # View all jobs
hermes cron remove <job_id>   # Remove a job
hermes cron status            # Check scheduler status
```

## Notes

`setup-cron-jobs.sh` only CREATES jobs. It runs once per persistent disk,
gated by the marker `/data/cron/.seeded`. To change the roster: edit the
script, redeploy, remove the existing jobs with `hermes cron remove <id>`,
delete the marker, and restart — deleting the marker alone gives you
duplicates.

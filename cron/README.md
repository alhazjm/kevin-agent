# Cron Jobs

## Scheduled Jobs

| Schedule | Time | Description |
|---|---|---|
| `0 21 * * *` | Daily 9 PM | Daily spending summary + budget warnings |
| `0 18 * * 5` | Friday 6 PM | Weekly expense summary (fun tone) |
| `0 9 1 * *` | 1st of month 9 AM | Full monthly report |
| `0 12 * * *` | Daily noon | Silent budget warning check (only alerts if >80%) |

All times are in your system's local timezone (Singapore SGT).

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

## Custom Reminders

You can also create reminders via WhatsApp:
- "Remind me daily to stop spending on Grab rides until Sunday"
- Hermes will create a cron job automatically and delete it on the end date

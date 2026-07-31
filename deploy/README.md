# Render Deployment Guide

## Overview

Deploys the Hermes expense tracker as a Docker web service on Render (Singapore region, Starter $7/mo) with a 1GB persistent disk for sessions, memories, and cron jobs.

Uses Telegram Bot API for the messaging interface — permanent token, no session management needed.

## Setup Steps

### 1. Create your Telegram bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "My Expense Tracker")
4. Choose a username (e.g., `my_expense_bot`)
5. Copy the **bot token** (looks like `123456:ABC-DEF1234...`)
6. Message your new bot once (so it can reply to you)
7. Get your user ID: message **@userinfobot** and it'll reply with your ID

### 2. Create the service on Render

**Option A — Blueprint (recommended):**
1. Go to https://dashboard.render.com/blueprints
2. Click **New Blueprint Instance**
3. Connect GitHub → select **your fork of this repo**
4. Render reads `render.yaml` and sets everything up

**Option B — Manual:**
1. **New** → **Web Service** → connect the private repo
2. Runtime: **Docker**
3. Region: **Singapore**
4. Plan: **Starter** ($7/mo)
5. Add a **Disk**: mount path `/data`, size 1GB

### 3. Set environment variables

In Render dashboard → your service → **Environment**:

| Variable | Value | How to get it |
|---|---|---|
| `OPENAI_API_KEY` | Your API key | OpenAI API dashboard |
| `PEHD_LLM_USAGE_DIR` | `/data/llm_usage` | Optional; where per-call LLM usage JSONL is written |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Entire JSON file contents | Open the downloaded `.json`, copy everything |
| `GSPREAD_SPREADSHEET_ID` | Spreadsheet ID | From Sheet URL: `docs.google.com/spreadsheets/d/`**THIS**`/edit` |
| `WEBHOOK_HMAC_SECRET` | Random string | Make one up or run `openssl rand -hex 32` |
| `TELEGRAM_BOT_TOKEN` | Bot token | From @BotFather (step 1) |
| `TELEGRAM_ALLOWED_USERS` | Your user ID | From @userinfobot (step 1) |

### 4. Deploy

Click **Manual Deploy** → **Deploy latest commit**. Watch the logs for:
```
Starting Hermes gateway...
Telegram bot connected
```

### 5. Update Gmail Apps Script webhook URL

Once deployed, your service URL will be something like:
```
https://YOUR-RENDER-SERVICE.onrender.com
```

Update `WEBHOOK_URL` in your Apps Script (`Code.gs`):
```javascript
const WEBHOOK_URL = "https://YOUR-RENDER-SERVICE.onrender.com/webhooks/expense-ingest";
```

Then run `clasp push` to deploy the update.

### 6. Test it

1. Open Telegram → message your bot: **"Hello"**
2. It should reply (confirms Telegram + LLM working)
3. Send: **"How much have I spent this month?"**
4. It should query your Sheet and respond

## Costs

| Item | Cost |
|---|---|
| Render Starter | $7/month |
| 1GB Disk | Included |
| OpenAI API | Usage-based |
| Google Sheets API | Free (60 req/min) |
| Telegram Bot API | Free forever |

## Updating

Push to the private repo → Render auto-deploys:
```bash
git push render main
```

## Troubleshooting

- **Bot not responding**: Check Render logs for errors. Verify `TELEGRAM_BOT_TOKEN` is correct.
- **Sheets errors**: Verify the service account email has Editor access to your sheet.
- **Webhook not working**: Make sure the Render URL is correct in `Code.gs` and the `WEBHOOK_HMAC_SECRET` matches.

# Gmail Apps Script Deployment

This Apps Script monitors your Gmail for DBS and UOB transaction alerts and forwards parsed data to the Hermes agent via webhook.

## Prerequisites

- Google account with Gmail
- Node.js installed (for clasp CLI)
- DBS/UOB email alerts enabled on your bank accounts

## Setup Steps

### 1. Install clasp (Google Apps Script CLI)

```bash
npm install -g @google/clasp
clasp login
```

### 2. Create a new Apps Script project

```bash
cd apps-script/
clasp create --type standalone --title "Hermes Expense Tracker"
```

### 3. Push the code

```bash
clasp push
```

### 4. Set the webhook secret

In the Apps Script editor (https://script.google.com):

1. Go to **Project Settings** (gear icon)
2. Under **Script Properties**, add:
   - Property: `WEBHOOK_HMAC_SECRET`
   - Value: (same value as in your `~/.hermes/.env`)

### 5. Update the webhook URL

In `Code.gs`, update `WEBHOOK_URL` to your actual endpoint:

- **Local development**: Use ngrok or Cloudflare Tunnel to expose localhost:8644
  ```bash
  ngrok http 8644
  ```
  Then set `WEBHOOK_URL` to the ngrok URL + `/webhook/expense-ingest`

- **Production**: Use your server's public URL

### 6. Create the trigger

In the Apps Script editor, run `setupTrigger()` once. This creates a time-based trigger that checks for new emails every 5 minutes.

### 7. Enable bank email alerts

- **DBS**: Log in to DBS digibank → Alerts & Notifications → Enable transaction alerts for all cards
- **UOB**: Log in to UOB Personal Internet Banking → Alerts Management → Enable spend alerts

## Testing

1. Make a small transaction with your DBS or UOB card
2. Wait for the email alert (usually 1-2 minutes)
3. The trigger runs every 5 minutes — check Apps Script execution logs
4. Verify the transaction appears in your Google Sheet

## Troubleshooting

- **No emails found**: Check the Gmail filter query matches your bank's alert format
- **Parse errors**: Your bank may use a different email format — check logs and adjust regex
- **Webhook fails**: Ensure Hermes gateway is running and the URL is reachable

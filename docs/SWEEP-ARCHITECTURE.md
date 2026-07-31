# Batch 4: Missed Transaction Recovery — Architecture

## Problem

When the LLM API fails (HTTP 529, timeout, rate limit), the webhook payload is
lost. The Gmail Apps Script already marked the email as read, so it won't retry.
The transaction never reaches the Google Sheet. The user has no visibility into
what was missed unless they manually compare their bank app against the sheet.

### Failure timeline

```
Gmail email arrives
  → Apps Script parses it, sends webhook to Render     ← payload exists here
    → Hermes receives webhook, formats LLM prompt
      → LLM API returns 5xx / times out                ← payload lost here
        → Hermes sends retry noise to Telegram
          → User sees error, transaction never logged
```

The key insight: the parsed payload exists briefly inside the Apps Script
`sendWebhook()` function and inside the hermes webhook handler, but neither
persists it. Once the LLM call fails, it's gone.

---

## Solution: Webhook Audit Log + Sweep Tool

Two components that work together:

### Component 1: Webhook Audit Log (Apps Script side)

**What**: Modify `apps-script/Code.gs` to write every parsed transaction to a
new `WebhookLog` tab in the Google Sheet *before* sending the webhook. This
creates a durable record of every bank email that was processed, regardless of
whether the webhook/LLM succeeded.

**New tab: `WebhookLog`**

| Col | Header | Type | Notes |
|---|---|---|---|
| 1 | `timestamp` | ISO 8601 | When the Apps Script processed the email |
| 2 | `bank` | Text | `DBS` or `UOB` |
| 3 | `type` | Text | `paylah` or `card` |
| 4 | `amount` | Number | Parsed amount |
| 5 | `currency` | Text | `SGD` |
| 6 | `merchant` | Text | Raw merchant string |
| 7 | `date` | YYYY-MM-DD | Transaction date |
| 8 | `payment_method` | Text | Card/wallet identifier |
| 9 | `idempotency_key` | Text | Same hash as Transactions col 11 |
| 10 | `webhook_status` | Text | `sent`, `failed`, or `error:{message}` |
| 11 | `matched` | Text | `yes` or empty — filled by sweep tool |

**Apps Script changes** (`Code.gs`):

```javascript
// In checkNewEmails(), after parseDBS/parseUOB succeeds:
if (parsed) {
    parsed.source = "email";
    parsed.raw_from = msg.getFrom();
    parsed.email_date = msg.getDate().toISOString();

    // NEW: Log to WebhookLog before sending
    logToAuditSheet(parsed);

    sendWebhook(parsed);
}

function logToAuditSheet(payload) {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var ws = ss.getSheetByName("WebhookLog");
    if (!ws) {
        ws = ss.insertSheet("WebhookLog");
        ws.appendRow([
            "timestamp", "bank", "type", "amount", "currency",
            "merchant", "date", "payment_method", "idempotency_key",
            "webhook_status", "matched"
        ]);
    }

    var idemKey = computeIdempotencyKey(
        payload.date, payload.merchant, payload.amount, payload.payment_method
    );

    ws.appendRow([
        new Date().toISOString(),
        payload.bank,
        payload.type,
        payload.amount,
        payload.currency,
        payload.merchant,
        payload.date,
        payload.payment_method,
        idemKey,
        "pending",  // updated to "sent" or "error" after sendWebhook
        ""
    ]);
}

function computeIdempotencyKey(date, merchant, amount, paymentMethod) {
    var raw = date + "|" + merchant.trim().toUpperCase() + "|"
            + parseFloat(amount).toFixed(2) + "|" + paymentMethod.trim();
    var hash = Utilities.computeDigest(
        Utilities.DigestAlgorithm.SHA_256, raw
    );
    return hash.slice(0, 8).map(function(b) {
        return ("0" + (b & 0xFF).toString(16)).slice(-2);
    }).join("");
}
```

**Why the Apps Script and not hermes-agent?** We don't control the hermes
webhook handler code — it's cloned from `alhazjm/hermes-agent` at build time.
Patching it via Dockerfile `sed` is fragile. The Apps Script is in this repo
and runs independently.

**Env requirement**: The Apps Script needs `SPREADSHEET_ID` in its script
properties (it already has `WEBHOOK_HMAC_SECRET` there). The service account
that the Apps Script runs under (or the user's Google account, since Apps
Script runs as the user) needs Editor access to the sheet — which it already
has, since the sheet is in the same Google account.

### Component 2: Sweep Tool (Python side)

**What**: A new tool `sweep_missed_transactions` that compares the
`WebhookLog` tab against the `Transactions` tab and surfaces unmatched
entries.

**How it works**:

```
1. Read all rows from WebhookLog
2. Read all idempotency_keys from Transactions
3. For each WebhookLog row where idempotency_key NOT IN Transactions:
     → This transaction was parsed but never logged
4. Return the list of missed transactions
5. User reviews and confirms which ones to log
6. For confirmed entries: call log_expense with source="email"
     (idempotency_key prevents double-insert if it was logged after all)
7. Mark the WebhookLog row's "matched" column as "yes"
```

**Tool schema**:

```python
SWEEP_MISSED_SCHEMA = {
    "name": "sweep_missed_transactions",
    "description": (
        "Compare WebhookLog against Transactions to find bank emails "
        "that were parsed but never logged (due to LLM failures, "
        "timeouts, etc). Returns unmatched entries for user review."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days_back": {
                "type": "integer",
                "description": "How many days back to scan (default 7)",
                "default": 7,
            },
            "auto_log": {
                "type": "boolean",
                "description": (
                    "If true, automatically log missed transactions "
                    "using MerchantMap for categorisation. If false "
                    "(default), return the list for user review."
                ),
                "default": False,
            },
        },
        "required": [],
    },
}
```

**Python implementation** (in `sheets_client.py`):

```python
def sweep_missed_transactions(days_back: int = 7) -> dict:
    """Compare WebhookLog against Transactions to find missed entries."""
    ss = get_spreadsheet()

    # Check if WebhookLog tab exists
    try:
        wl = ss.worksheet("WebhookLog")
    except gspread.WorksheetNotFound:
        return {"status": "error", "message": "WebhookLog tab not found. "
                "Deploy the updated Apps Script first."}

    ws = ss.worksheet(TRANSACTIONS_SHEET)

    # Get all idempotency keys from Transactions
    txn_idem_col = _get_column_index(ws, "idempotency_key")
    txn_keys = set()
    if txn_idem_col:
        txn_keys = set(ws.col_values(txn_idem_col)[1:])  # skip header
        txn_keys.discard("")

    # Get WebhookLog entries within date range
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    log_records = wl.get_all_records()

    missed = []
    for row in log_records:
        if row.get("date", "") < cutoff:
            continue
        key = row.get("idempotency_key", "")
        if key and key not in txn_keys:
            missed.append(row)

    return {
        "status": "ok",
        "total_webhook_logs": len(log_records),
        "missed_count": len(missed),
        "missed": missed,
    }
```

---

## Component 3: Backfill from Bank Statements

Separate from the sweep (which recovers webhook failures), backfill handles
bulk import from bank statement text that was never emailed.

**Use case**: Sam's supplementary card transactions arrive once a month in the
DBS statement PDF — they're not real-time email alerts. The user pastes or
uploads the statement excerpt, and the tool parses and logs them.

### `backfill_transactions` tool

```python
BACKFILL_SCHEMA = {
    "name": "backfill_transactions",
    "description": (
        "Bulk-import transactions from pasted bank statement text. "
        "Parses each line, matches against MerchantMap for categories, "
        "and logs with source='backfill'. Returns a preview first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "statement_text": {
                "type": "string",
                "description": "Raw bank statement text, one transaction per line",
            },
            "bank": {
                "type": "string",
                "enum": ["DBS", "UOB"],
                "description": "Which bank's statement format to parse",
            },
            "payment_method": {
                "type": "string",
                "description": "Payment method for all rows (e.g. 'DBS/POSB card ending 1234')",
            },
            "confirm": {
                "type": "boolean",
                "description": "Set true to log after preview. Default false (preview only).",
                "default": False,
            },
        },
        "required": ["statement_text", "bank"],
    },
}
```

**Flow**:
1. User pastes statement lines (or sends a photo — LLM extracts text)
2. Tool parses each line → returns preview with MerchantMap category guesses
3. User reviews, corrects categories if needed
4. Tool logs all rows with `source="backfill"`
5. Backfill rows are excluded from subscription creep detection (existing rule)

**Statement parsing** is bank-specific:
- DBS statement: `DD MMM  MERCHANT NAME  AMOUNT` format
- UOB statement: `DD/MM  MERCHANT NAME  AMOUNT` format
- Parser extracts date, merchant, amount from each line

---

## Implementation Plan

### Prerequisites
- [ ] Apps Script needs `SPREADSHEET_ID` in script properties (for WebhookLog writes)

### Phase 1: Webhook Audit Log
1. Add `WebhookLog` tab schema to `sheets-template/README.md`
2. Update `apps-script/Code.gs` with `logToAuditSheet()` and `computeIdempotencyKey()`
3. Update `sendWebhook()` to mark status as `sent` or `error` after the call
4. Test: manually trigger Apps Script, verify row appears in WebhookLog

### Phase 2: Sweep Tool
1. Add `sweep_missed_transactions()` to `sheets_client.py`
2. Add tool registration + handler in `expense_sheets_tool.py`
3. Add to Dockerfile sed injection
4. Add to SKILL.md — instructions for when user says "check for missed" or "sweep"
5. Tests: mock WebhookLog + Transactions, verify diff logic
6. Test: trigger a real sweep after deliberately failing a webhook

### Phase 3: Backfill Tool
1. Add `parse_dbs_statement()` and `parse_uob_statement()` to `sheets_client.py`
   (or a new `tools/statement_parser.py`)
2. Add `backfill_transactions` tool with preview-then-confirm
3. Add to Dockerfile sed injection
4. Add to SKILL.md
5. Tests: DBS and UOB statement format parsing

### Phase 4: Telegram Integration
1. Add `/sweep` and `/backfill` as recognized commands in SKILL.md
2. `/sweep` triggers `sweep_missed_transactions(days_back=7)` and presents results
3. `/backfill` prompts user to paste statement text, then runs the tool

---

## Idempotency Key Compatibility

The sweep relies on the idempotency key being identical in both the
`WebhookLog` (computed in Apps Script / JavaScript) and the `Transactions` tab
(computed in Python). Both must use the same formula:

```
SHA-256( "{date}|{MERCHANT.trim().toUpperCase()}|{amount:.2f}|{payment_method.trim()}" )
  → first 16 hex characters
```

**Critical**: The JavaScript `Utilities.computeDigest` returns signed bytes
(-128..127) while Python's `hashlib` returns unsigned hex. The Apps Script
`computeIdempotencyKey` function must produce the same 16-char hex string as
Python's `_compute_idempotency_key`. Both normalize merchant to uppercase and
trim whitespace. Test this with a known input to verify parity before shipping.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Apps Script → Sheet write fails (quota) | Low | Missed log entry — same as today | Apps Script retries sheet writes once |
| Idempotency key mismatch (JS vs Python) | Medium | False positives in sweep | Unit test with identical inputs in both languages |
| WebhookLog grows unbounded | Low | Slow reads after months | Periodic cleanup or archive rows older than 90 days |
| Backfill statement format changes | Low | Parser breaks | Regex-based, easy to update per bank |
| `auto_log=true` miscategorises | Medium | Wrong category logged | Default is preview-only; auto_log requires explicit opt-in |

---

## Sheets API Quota Considerations

The sweep reads two full tabs (WebhookLog + Transactions). At the current
Google Sheets API limit of 60 reads/min, this is 2 calls for the comparison
plus 1 write per missed transaction logged. A typical sweep finding 1-3 missed
transactions stays well under quota. For backfill of 20+ rows, batch the
`append_row` calls or use `sheets.values.append` with a multi-row payload.

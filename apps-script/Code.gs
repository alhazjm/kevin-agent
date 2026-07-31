/**
 * Gmail Apps Script for DBS/UOB/HSBC transaction email parsing.
 * Extracts transaction details and sends webhooks to Hermes agent.
 *
 * DBS sends two types:
 *   - PayLah! (debit): "Amount: SGD25.00" / "To: MERCHANT"
 *   - Card (credit):   "Amount: SGD3.08" / "From: DBS/POSB card ending XXXX" / "To: MERCHANT"
 *     Foreign-currency: "Amount: IDR2334082.00" / "To: MERCHANT" (overseas spend)
 * UOB sends:
 *   - Credit card: "A transaction of SGD 55.00 was made with your UOB Card ending XXXX on DD/MM/YY at MERCHANT"
 *     Foreign-currency: "A transaction of EUR 18.50 was made with your UOB Card..."
 * HSBC sends (from HSBC.Bank.Singapore.Limited@notification.hsbc.com.hk,
 * subject "Transaction Alerts (Credit Card)") — an HTML table of label/value
 * rows, which getPlainBody() flattens; the regexes tolerate the label and
 * value landing on one line or two:
 *   - "Card Number: XXXX-XXXX-XXXX-1357" / "Transaction Date: 29/JUL/2026"
 *     / "Transaction Time: 18:26:09" / "Transaction Amount: SGD8.50"
 *     / "Description: SHENG SIONG SUPERMARKET -"
 * YouTrip sends nothing — an iOS Wallet automation on the user's phone
 * emails a fixed format on every Apple Pay tap of the YouTrip card
 * (self-sent; dispatched on the SUBJECT prefix "YouTrip Transaction",
 * never the sender):
 *   - 'Source: "applepay"' / "Merchant: {Sheng Siong Supermarke}"
 *     / "Amount: {$6.95}" / "ts: {2026-07-31T13:52:52+08:00}"
 *
 * Non-SGD transactions are converted to SGD at ingest via frankfurter.app
 * (ECB rates, no API key). The original currency + amount is stamped in the
 * `notes` field so the conversion is traceable. If the FX lookup fails, a
 * Telegram alert is sent and no Transactions row is written — the user logs
 * it manually after checking the bank app.
 */

const WEBHOOK_URL = "https://YOUR-RENDER-SERVICE.onrender.com/webhooks/expense-ingest";
const WEBHOOK_SECRET = PropertiesService.getScriptProperties().getProperty("WEBHOOK_HMAC_SECRET");
const SPREADSHEET_ID = PropertiesService.getScriptProperties().getProperty("SPREADSHEET_ID");
const TELEGRAM_BOT_TOKEN = PropertiesService.getScriptProperties().getProperty("TELEGRAM_BOT_TOKEN");
const TELEGRAM_CHAT_ID = PropertiesService.getScriptProperties().getProperty("TELEGRAM_CHAT_ID");
// Supabase audit target (migration PR 4). When both are set, audit rows go
// to the webhook_log table; the Sheet's WebhookLog tab is the fallback if
// the POST fails, so audit-before-webhook durability is never weaker than
// the old path.
const SUPABASE_URL = PropertiesService.getScriptProperties().getProperty("SUPABASE_URL");
const SUPABASE_SERVICE_KEY = PropertiesService.getScriptProperties().getProperty("SUPABASE_SERVICE_KEY");

const WEBHOOK_LOG_SHEET = "WebhookLog";
const WEBHOOK_LOG_HEADER = [
  "timestamp", "bank", "type", "amount", "currency",
  "merchant", "date", "payment_method", "idempotency_key",
  "webhook_status", "matched",
];

// Two SEPARATE searches, deliberately not one boolean query: Gmail's
// search parser is loose about OR precedence, and the combined form
// '(from:(banks) subject:(...) OR subject:"YouTrip Transaction")'
// silently ANDed the from: clause over everything — the self-sent
// Shortcut email never matched (live failure, 2026-08-01). Bank alerts
// key on sender+subject; the Shortcut source DISPATCHES on its fixed
// subject prefix, but the search requires from:me — without it, anyone
// who knows this inbox's address could mail a crafted "YouTrip
// Transaction" body and have it parsed, signed, and logged as a real
// transaction (forgery / prompt-injection gate). from:me matches every
// send-as address on this Gmail account; if the iPhone Shortcut ever
// sends from an account Gmail doesn't own, add that sender explicitly.
const GMAIL_QUERY = 'from:(alerts@dbs.com OR ibanking.alert@dbs.com OR paylah.alert@dbs.com OR unialerts@uobgroup.com OR notification.hsbc.com.hk) subject:(transaction OR alert OR PayLah) is:unread newer_than:7d';
const SHORTCUT_QUERY = 'from:me subject:"YouTrip Transaction" is:unread newer_than:7d';


function setupTrigger() {
  ScriptApp.newTrigger("checkNewEmails")
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log("Trigger created: checks every 5 minutes");
}


function checkNewEmails() {
  var threads = GmailApp.search(GMAIL_QUERY, 0, 10)
    .concat(GmailApp.search(SHORTCUT_QUERY, 0, 10));

  for (var i = 0; i < threads.length; i++) {
    var messages = threads[i].getMessages();

    for (var j = 0; j < messages.length; j++) {
      var msg = messages[j];
      if (!msg.isUnread()) continue;

      var from = msg.getFrom().toLowerCase();
      var subject = msg.getSubject() || "";
      var body = msg.getPlainBody();
      var parsed = null;

      // Self-sent Shortcut sources dispatch on the fixed SUBJECT prefix,
      // never the sender — unrelated self-mail must not reach a parser
      // (new-alert-source hard rule).
      if (subject.indexOf("YouTrip Transaction") === 0) {
        parsed = parseYouTrip(body);
      } else if (from.indexOf("dbs.com") !== -1) {
        parsed = parseDBS(body);
      } else if (from.indexOf("uobgroup.com") !== -1) {
        parsed = parseUOB(body);
      } else if (from.indexOf("hsbc.com") !== -1) {
        parsed = parseHSBC(body);
      }

      if (parsed) {
        parsed.source = "email";
        parsed.raw_from = msg.getFrom();
        parsed.email_date = msg.getDate().toISOString();

        // Time-in-key (PR 4): DBS alerts print the transaction time and
        // parseDBS captures it; UOB alerts carry none, so the email's own
        // arrival time stands in. Either way the SAME email re-processed
        // yields the SAME time (dedup preserved), while two real purchases
        // arrive as two emails with different times (collision fixed —
        // the KOPITIAM $7.80 incident, 2026-07-25).
        if (!parsed.time) {
          parsed.time = Utilities.formatDate(
            msg.getDate(), "Asia/Singapore", "HH:mm:ss");
        }

        // FX normalisation. Non-SGD txns get converted to SGD before
        // anything else writes — the audit row, idempotency key, and
        // Transactions row all live in SGD so the budget math works. The
        // original foreign currency + amount is stamped into `notes` for
        // traceability. If the FX lookup fails, we skip the webhook and
        // fire a Telegram nudge so the user can log it manually.
        var fxFailed = false;
        if (parsed.currency !== "SGD") {
          var fx = convertToSGD(parsed.amount, parsed.currency);
          if (fx) {
            var origNote = "orig: " + parsed.currency + " " + parsed.amount.toFixed(2)
                         + " @ " + fx.rate.toFixed(6)
                         + (fx.fxDate ? " (" + fx.source + " " + fx.fxDate + ")"
                                      : " (" + fx.source + ")");
            parsed.notes = parsed.notes ? parsed.notes + "; " + origNote : origNote;
            parsed.amount = Math.round(fx.amount * 100) / 100;
            parsed.currency = "SGD";
          } else {
            fxFailed = true;
          }
        }

        // Audit log FIRST so the transaction is durable even if the webhook /
        // LLM call fails. The sweep tool later diffs WebhookLog vs
        // Transactions to surface anything that never made it into the
        // ledger. idempotency_key is computed here and must produce the
        // same 16-char hex as Python's _compute_idempotency_key — see
        // testIdempotencyKeyParity() for the lock-in values.
        var idemKey = computeIdempotencyKey(
          parsed.date, parsed.merchant, parsed.amount, parsed.payment_method,
          parsed.time
        );
        logAudit(parsed, idemKey);

        if (fxFailed) {
          updateWebhookStatus(idemKey, "fx_failed");
          sendTelegramAlert(
            "⚠️ Couldn't auto-log: " + parsed.bank + " "
            + parsed.currency + " " + parsed.amount.toFixed(2) + " at "
            + parsed.merchant + " (FX lookup failed). Reply with the SGD "
            + "amount when you know it and I'll log it manually."
          );
        } else {
          var status = sendWebhook(parsed);
          updateWebhookStatus(idemKey, status);
        }
      }

      msg.markRead();
    }
  }
}


/**
 * Parse DBS transaction alert emails.
 *
 * PayLah! format:
 *   Date & Time: 10 Apr 13:10 (SGT)
 *   Amount:      SGD25.00
 *   From:        PayLah! Wallet (Mobile ending 9876)
 *   To:          GREENFIELD COMMUNITY FUND
 *
 * Card format:
 *   Date & Time: 14 APR 06:44 (SGT)
 *   Amount: SGD3.08
 *   From: DBS/POSB card ending 1234
 *   To: BUS/MRT
 */
function parseDBS(body) {
  // Currency code is now captured dynamically: any 3-letter code matches
  // (SGD, IDR, USD, EUR, ...). Foreign-currency txns get converted to SGD
  // downstream via convertToSGD(); see checkNewEmails().
  var amountMatch = body.match(/Amount:\s*([A-Z]{3})\s?([\d,]+\.?\d*)/i);
  var merchantMatch = body.match(/To:\s*(.+)/im);
  var dateMatch = body.match(/Date\s*&\s*Time:\s*(\d{1,2}\s+\w+)\s+/i);
  // "Date & Time: 14 APR 06:44 (SGT)" — the time participates in the
  // idempotency key. Zero-pad a single-digit hour for a stable format.
  var timeMatch = body.match(/Date\s*&\s*Time:\s*\d{1,2}\s+\w+\s+(\d{1,2}:\d{2})/i);

  if (!amountMatch || !merchantMatch) return null;

  var fromMatch = body.match(/From:\s*(.+)/im);
  var fromText = fromMatch ? fromMatch[1].trim() : "";
  var isPayLah = fromText.toLowerCase().indexOf("paylah") !== -1;

  var cardMatch = fromText.match(/ending\s+(\d{4})/i);

  var dateStr = "";
  if (dateMatch) {
    var currentYear = String(new Date().getFullYear());
    dateStr = formatDBSDate(dateMatch[1].trim(), currentYear);
  }

  var timeStr = "";
  if (timeMatch) {
    timeStr = timeMatch[1];
    if (timeStr.indexOf(":") === 1) timeStr = "0" + timeStr;
  }

  return {
    bank: "DBS",
    type: isPayLah ? "paylah" : "card",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1].toUpperCase(),
    merchant: merchantMatch[1].trim(),
    date: dateStr || new Date().toISOString().slice(0, 10),
    time: timeStr,
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: fromText,
  };
}


/**
 * Parse UOB transaction alert email.
 * Format: "A transaction of SGD 55.00 was made with your UOB Card ending 5678 on 12/04/26 at UrbanCompany."
 * Sender: unialerts@uobgroup.com
 */
function parseUOB(body) {
  // Currency code captured dynamically — same approach as parseDBS.
  var amountMatch = body.match(/transaction\s+of\s+([A-Z]{3})\s+([\d,]+\.?\d*)\s+was\s+made/i);
  var cardMatch = body.match(/Card\s+ending\s+(\d{4})/i);
  var dateMatch = body.match(/on\s+(\d{2}\/\d{2}\/\d{2,4})/i);
  var merchantMatch = body.match(/at\s+(.+?)(?:\.\s*If|\s*$)/im);

  if (!amountMatch) return null;

  var merchant = merchantMatch ? merchantMatch[1].trim() : "Unknown";
  // Clean trailing punctuation
  merchant = merchant.replace(/[.\s]+$/, "");

  return {
    bank: "UOB",
    type: "card",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1].toUpperCase(),
    merchant: merchant,
    date: dateMatch ? formatDateSlash(dateMatch[1]) : new Date().toISOString().slice(0, 10),
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: "UOB Card" + (cardMatch ? " ending " + cardMatch[1] : ""),
  };
}


/**
 * Parse HSBC credit card transaction alert email.
 *
 * The alert is an HTML table of label/value rows (see the file header for
 * the fields). getPlainBody() may flatten a row onto one line or split the
 * label and value across two, so every regex bridges the gap with \s* —
 * which matches newlines — instead of assuming a layout. First live sample:
 * 2026-07-29, card ending 1357 (HSBC Revolution).
 *
 * The Description cell ends with a trailing " -" (a separator before an
 * empty city/location field); it is stripped so the merchant lands clean.
 * Sender: HSBC.Bank.Singapore.Limited@notification.hsbc.com.hk
 */
function parseHSBC(body) {
  // Currency code captured dynamically — same approach as parseDBS.
  var amountMatch = body.match(/Transaction\s*Amount\s*:?\s*([A-Z]{3})\s*([\d,]+\.?\d*)/i);
  var cardMatch = body.match(/Card\s*Number\s*:?\s*[X*\-]+(\d{4})/i);
  var dateMatch = body.match(/Transaction\s*Date\s*:?\s*(\d{1,2}\/[A-Za-z]{3}\/\d{4})/i);
  // Seconds are optional so a future HH:MM-only alert still parses; the
  // captured time participates in the idempotency key (time-in-key, PR 4).
  var timeMatch = body.match(/Transaction\s*Time\s*:?\s*(\d{1,2}:\d{2}(?::\d{2})?)/i);
  var merchantMatch = body.match(/Description\s*:?\s*(.+)/im);

  if (!amountMatch || !merchantMatch) return null;

  var merchant = merchantMatch[1].trim().replace(/[-.\s]+$/, "");

  var timeStr = "";
  if (timeMatch) {
    timeStr = timeMatch[1];
    if (timeStr.indexOf(":") === 1) timeStr = "0" + timeStr;
  }

  return {
    bank: "HSBC",
    type: "card",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1].toUpperCase(),
    merchant: merchant,
    date: dateMatch ? formatHSBCDate(dateMatch[1]) : new Date().toISOString().slice(0, 10),
    time: timeStr,
    card_last_four: cardMatch ? cardMatch[1] : null,
    payment_method: "HSBC card" + (cardMatch ? " ending " + cardMatch[1] : ""),
  };
}


/**
 * Parse a YouTrip iPhone-Shortcut alert email (self-sent).
 *
 * YouTrip sends no transaction emails, so an iOS Wallet automation fires
 * on every Apple Pay tap of the YouTrip card and emails a fixed format.
 * First live sample (2026-07-31, Sheng Siong):
 *
 *   Source: "applepay"
 *   Merchant: {Sheng Siong Supermarke}
 *   Amount: {$6.95}
 *   ts: {2026-07-31T13:52:52+08:00}
 *
 * Quirks pinned to that sample: Shortcuts renders the template's variable
 * chips wrapped in braces; the Source value carries literal quotes; Apple
 * Pay truncates long merchant names ("…Supermarke") — MerchantMap's
 * substring matching absorbs that. A bare "$" amount is SGD; a 3-letter
 * code before the number (the expected shape for overseas taps,
 * UNVERIFIED until the first one) is captured dynamically and converts
 * via the normal FX path — which stamps `orig:` and feeds travel mode.
 * An empty Merchant chip means a manual Shortcut test-run — skip it.
 *
 * Best-effort source: Apple Pay taps only (the physical card is silent),
 * no retry if the phone is offline at tap time. Statements remain the
 * ground truth (docs/STATEMENT-RECON.md).
 */
function parseYouTrip(body) {
  var merchantMatch = body.match(/Merchant:\s*\{([^}]*)\}/i);
  var amountMatch = body.match(/Amount:\s*\{\s*(?:([A-Z]{3})\s*)?\$?\s*([\d,]+\.?\d*)\s*\}/i);
  var tsMatch = body.match(/ts:\s*\{([^}]*)\}/i);

  if (!amountMatch || !merchantMatch) return null;

  var merchant = merchantMatch[1].trim();
  if (!merchant) return null;

  // "2026-07-31T13:52:52+08:00" → date + time by string slicing (M5:
  // never round-trip through new Date()). Abroad the phone stamps LOCAL
  // wall-clock time — the same convention bank alerts use.
  var dateStr = "";
  var timeStr = "";
  if (tsMatch) {
    var ts = tsMatch[1].trim();
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(ts)) {
      dateStr = ts.slice(0, 10);
      timeStr = ts.slice(11, 19);
    }
  }

  return {
    bank: "YouTrip",
    type: "applepay",
    amount: parseFloat(amountMatch[2].replace(/,/g, "")),
    currency: amountMatch[1] ? amountMatch[1].toUpperCase() : "SGD",
    merchant: merchant,
    date: dateStr || new Date().toISOString().slice(0, 10),
    time: timeStr,
    card_last_four: null,
    // Contains "youtrip" — the PWA's pot-exclusion contract keys on it.
    payment_method: "YouTrip Card",
  };
}


/** Convert "29/JUL/2026" → "2026-07-29" via string arithmetic.
 *
 * Same TZ-safety rule as formatDBSDate below: never round-trip through
 * `new Date()` + `toISOString()` — that shifted DBS txn dates a day back
 * once (M5). Unknown month / malformed input falls back to today, matching
 * the other parsers' date fallbacks.
 */
function formatHSBCDate(dateStr) {
  var parts = String(dateStr).trim().split("/");
  if (parts.length !== 3) return new Date().toISOString().slice(0, 10);
  var day = parts[0].length === 1 ? "0" + parts[0] : parts[0];
  var monthMap = {
    JAN: "01", FEB: "02", MAR: "03", APR: "04", MAY: "05", JUN: "06",
    JUL: "07", AUG: "08", SEP: "09", OCT: "10", NOV: "11", DEC: "12",
  };
  var month = monthMap[parts[1].toUpperCase()];
  if (!month || !/^\d{4}$/.test(parts[2])) {
    return new Date().toISOString().slice(0, 10);
  }
  return parts[2] + "-" + month + "-" + day;
}


/** Convert "23 APR" + "2026" → "2026-04-23" via string arithmetic.
 *
 * Why not `new Date("23 APR 2026").toISOString().slice(0, 10)`? JavaScript's
 * Date parser interprets a bare-date string in the runtime's LOCAL timezone.
 * Apps Script, for a Singapore account, defaults to Asia/Singapore (GMT+8).
 * So `new Date("23 APR 2026")` = 2026-04-23T00:00:00+08:00 = 2026-04-22T16:00:00Z.
 * Then `toISOString().slice(0, 10)` = "2026-04-22" — off by one day.
 *
 * Observed impact (pre-fix): DBS-email-derived txn_ids consistently landed
 * on the prior calendar day. UOB emails were unaffected because
 * formatDateSlash already did pure string arithmetic on DD/MM/YY. This
 * function brings DBS parsing to the same TZ-safe approach.
 */
function formatDBSDate(dayMonth, year) {
  var parts = String(dayMonth).trim().split(/\s+/);
  if (parts.length !== 2) return new Date().toISOString().slice(0, 10);
  var day = parts[0].length === 1 ? "0" + parts[0] : parts[0];
  var monthMap = {
    JAN: "01", FEB: "02", MAR: "03", APR: "04", MAY: "05", JUN: "06",
    JUL: "07", AUG: "08", SEP: "09", OCT: "10", NOV: "11", DEC: "12",
  };
  var month = monthMap[parts[1].toUpperCase()];
  if (!month || !/^\d{4}$/.test(String(year))) {
    return new Date().toISOString().slice(0, 10);
  }
  return year + "-" + month + "-" + day;
}


/** Convert "12/04/26" or "14/04/2026" → "2026-04-14" */
function formatDateSlash(dateStr) {
  var parts = dateStr.split("/");
  if (parts.length !== 3) return new Date().toISOString().slice(0, 10);

  var year = parts[2];
  if (year.length === 2) {
    year = "20" + year;
  }

  return year + "-" + parts[1] + "-" + parts[0];
}


/**
 * Convert a foreign-currency amount to SGD via frankfurter.app.
 *
 * Frankfurter publishes daily ECB reference rates with no API key. The
 * `?amount=N&from=XXX&to=SGD` endpoint does the multiply server-side, so
 * `data.rates.SGD` is the SGD-equivalent of the input amount.
 *
 * Returns: {amount, rate, source, fxDate} on success, or null on any
 * failure (network, non-200, currency unsupported, JSON shape unexpected).
 * Callers MUST handle null — see the FX-failed fallback in checkNewEmails.
 *
 * SGD passes through with rate=1 and source="passthrough", so callers can
 * treat this as an unconditional normaliser.
 */
function convertToSGD(amount, currency) {
  if (currency === "SGD") {
    return { amount: amount, rate: 1.0, source: "passthrough", fxDate: "" };
  }
  var url = "https://api.frankfurter.app/latest"
          + "?amount=" + encodeURIComponent(amount)
          + "&from=" + encodeURIComponent(currency)
          + "&to=SGD";
  try {
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) {
      Logger.log("convertToSGD: HTTP " + response.getResponseCode() + " for " + currency);
      return null;
    }
    var data = JSON.parse(response.getContentText());
    if (!data || !data.rates || typeof data.rates.SGD !== "number") {
      Logger.log("convertToSGD: unexpected response shape for " + currency);
      return null;
    }
    var sgdAmount = data.rates.SGD;
    return {
      amount: sgdAmount,
      // Per-unit rate, useful for the notes stamp.
      rate: amount > 0 ? sgdAmount / amount : 0,
      source: "frankfurter",
      fxDate: data.date || "",
    };
  } catch (e) {
    Logger.log("convertToSGD error: " + e.toString());
    return null;
  }
}


/**
 * Send a one-line Telegram message via the Bot API. Used for the FX-failed
 * fallback nudge — when the agent can't auto-log a foreign-currency txn,
 * we tell the user via Telegram so they can log manually.
 *
 * Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID Script Properties.
 * Returns true on 2xx, false on missing config / non-2xx / exception.
 */
function sendTelegramAlert(text) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    Logger.log("sendTelegramAlert: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping");
    return false;
  }
  var url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage";
  try {
    var response = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text: text }),
      muteHttpExceptions: true,
    });
    return response.getResponseCode() >= 200 && response.getResponseCode() < 300;
  } catch (e) {
    Logger.log("sendTelegramAlert error: " + e.toString());
    return false;
  }
}


/**
 * Send parsed transaction to Hermes webhook with HMAC signature.
 * Returns a status string suitable for the WebhookLog `webhook_status` column:
 *   "sent"            — HTTP 2xx
 *   "failed:<code>"   — HTTP non-2xx
 *   "error:<message>" — network/runtime exception
 */
function sendWebhook(payload) {
  var jsonPayload = JSON.stringify(payload);

  var signature = "";
  if (WEBHOOK_SECRET) {
    var hmac = Utilities.computeHmacSha256Signature(jsonPayload, WEBHOOK_SECRET);
    signature = hmac.map(function(byte) {
      return ("0" + (byte & 0xFF).toString(16)).slice(-2);
    }).join("");
  }

  var options = {
    method: "post",
    contentType: "application/json",
    payload: jsonPayload,
    headers: {
      "X-Webhook-Signature": signature,
    },
    muteHttpExceptions: true,
  };

  try {
    var response = UrlFetchApp.fetch(WEBHOOK_URL, options);
    var code = response.getResponseCode();
    Logger.log("Webhook response: " + code);
    if (code >= 200 && code < 300) {
      return "sent";
    }
    return "failed:" + code;
  } catch (e) {
    Logger.log("Webhook error: " + e.toString());
    return "error:" + e.toString();
  }
}


/**
 * Compute a 16-char hex idempotency key. Must match Python's
 * `tools.sheets_client._compute_idempotency_key` byte-for-byte so the sweep
 * tool can diff WebhookLog against Transactions.
 *
 * Normalisation rules:
 *   - merchant: trim + uppercase
 *   - amount: fixed to 2 decimal places
 *   - payment_method: trim
 *
 * The JS `Utilities.computeDigest` returns signed bytes (-128..127); the
 * `& 0xFF` masks them back to unsigned before hex encoding so the output
 * matches Python's `hashlib.sha256(...).hexdigest()[:16]`.
 */
function computeIdempotencyKey(date, merchant, amount, paymentMethod, time) {
  var raw = date + "|"
          + String(merchant || "").trim().toUpperCase() + "|"
          + parseFloat(amount).toFixed(2) + "|"
          + String(paymentMethod || "").trim();
  // Time-in-key (PR 4): appended ONLY when present, so the raw string for
  // time-less inputs is byte-identical to the legacy 4-field format and
  // every previously stored key stays valid. Mirrors Python's
  // _compute_idempotency_key — change both together (M17).
  if (time) {
    raw += "|" + String(time);
  }
  var hash = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256, raw
  );
  return hash.slice(0, 8).map(function(b) {
    return ("0" + (b & 0xFF).toString(16)).slice(-2);
  }).join("");
}


/**
 * One-off verification function — run manually from the Apps Script editor
 * to confirm the JS idempotency key matches the Python implementation.
 * Expected values are pinned in the Python parity test in
 * tests/test_expense_sheets_tool.py (class TestIdempotencyKeyParity).
 *
 * Drift here = silent duplicates in the sweep tool, so verify before shipping.
 */
function testIdempotencyKeyParity() {
  var cases = [
    // Legacy 4-field (no time) — unchanged pins prove backward compat
    ["2026-04-16", "Starbucks",    5.50,  "DBS card ending 1234",       "", "cb89d3a1d11dd274"],
    ["2026-04-14", "Cold Storage", 45.30, "DBS/POSB card ending 1234",  "", "a2a3de653d223e63"],
    ["2026-04-12", "GRABFOOD",     12.50, "UOB Card ending 5678",       "", "a470ad4b61937830"],
    ["2026-04-10", "Sheng Siong",  23.80, "PayLah! Wallet",             "", "25f16c670462d6c8"],
    // Time-in-key (PR 4) — same day/merchant/amount, different times,
    // different keys (the KOPITIAM $7.80 incident, 2026-07-25)
    ["2026-07-25", "KOPITIAM @ RAFFLES",  7.80,  "UOB Card ending 5678", "16:18",    "5fa19a03294bdad9"],
    ["2026-07-25", "KOPITIAM @ RAFFLES",  7.80,  "UOB Card ending 5678", "19:47",    "ad27b42f9385c92a"],
    ["2026-07-24", "ACME CLOUD SERVICES", 27.40, "DBS/POSB card ending 4321", "21:03:11", "a5e4ac6b14c02f1d"],
  ];
  var allPassed = true;
  for (var i = 0; i < cases.length; i++) {
    var c = cases[i];
    var got = computeIdempotencyKey(c[0], c[1], c[2], c[3], c[4]);
    var expected = c[5];
    var ok = (got === expected);
    Logger.log(
      (ok ? "OK   " : "FAIL ") +
      c[1] + " $" + c[2] + " -> got=" + got + " expected=" + expected
    );
    if (!ok) allPassed = false;
  }
  Logger.log(allPassed ? "All parity cases passed." : "PARITY MISMATCH — do not deploy.");
  return allPassed;
}


/**
 * Audit-before-webhook (migration PR 4): write the parsed transaction to
 * the Supabase webhook_log table with webhook_status "pending". Falls back
 * to the Sheet's WebhookLog tab if Supabase is unconfigured or the POST
 * fails, so audit durability is never weaker than the legacy path. The
 * sweep tool diffs the Supabase table, so the fallback also fires a
 * Telegram alert — a Sheet-only audit row is invisible to the sweep and
 * the user should know the safety net is degraded.
 */
function logAudit(payload, idempotencyKey) {
  if (SUPABASE_URL && SUPABASE_SERVICE_KEY) {
    try {
      var resp = UrlFetchApp.fetch(SUPABASE_URL + "/rest/v1/webhook_log", {
        method: "post",
        contentType: "application/json",
        headers: {
          "apikey": SUPABASE_SERVICE_KEY,
          "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
          "Prefer": "return=minimal",
        },
        payload: JSON.stringify({
          bank: payload.bank || "",
          type: payload.type || "",
          amount: payload.amount,
          currency: payload.currency || "",
          merchant: payload.merchant || "",
          txn_date: payload.date || "",
          payment_method: payload.payment_method || "",
          idempotency_key: idempotencyKey,
          webhook_status: "pending",
          matched: "",
        }),
        muteHttpExceptions: true,
      });
      var code = resp.getResponseCode();
      if (code >= 200 && code < 300) return;
      Logger.log("logAudit: supabase HTTP " + code + " — falling back to sheet");
    } catch (e) {
      Logger.log("logAudit error: " + e.toString() + " — falling back to sheet");
    }
    sendTelegramAlert(
      "⚠️ Audit log fell back to the Sheet for " + (payload.merchant || "?")
      + " — the sweep can't see it there. Check SUPABASE_URL / "
      + "SUPABASE_SERVICE_KEY Script Properties."
    );
  }
  logToAuditSheet(payload, idempotencyKey);
}


/**
 * Legacy Sheet audit writer — now the FALLBACK target for logAudit().
 *
 * Creates the tab with a header row on first use. Tolerates missing
 * SPREADSHEET_ID: logs a warning and returns so email processing still
 * proceeds (we'd rather send the webhook than block on an audit failure).
 */
function logToAuditSheet(payload, idempotencyKey) {
  if (!SPREADSHEET_ID) {
    Logger.log("logToAuditSheet: SPREADSHEET_ID script property not set; skipping audit log");
    return;
  }
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var ws = ss.getSheetByName(WEBHOOK_LOG_SHEET);
    if (!ws) {
      ws = ss.insertSheet(WEBHOOK_LOG_SHEET);
      ws.appendRow(WEBHOOK_LOG_HEADER);
    }
    ws.appendRow([
      new Date().toISOString(),
      payload.bank || "",
      payload.type || "",
      payload.amount,
      payload.currency || "",
      payload.merchant || "",
      payload.date || "",
      payload.payment_method || "",
      idempotencyKey,
      "pending",
      "",
    ]);
  } catch (e) {
    Logger.log("logToAuditSheet error: " + e.toString());
  }
}


/**
 * Update the audit row's webhook_status — Supabase first (PATCH by
 * idempotency_key), Sheet fallback. Silent no-op if the row can't be
 * found — the sweep diffs by key, not status.
 */
function updateWebhookStatus(idempotencyKey, status) {
  if (!idempotencyKey) return;
  if (SUPABASE_URL && SUPABASE_SERVICE_KEY) {
    try {
      var resp = UrlFetchApp.fetch(
        SUPABASE_URL + "/rest/v1/webhook_log?idempotency_key=eq."
          + encodeURIComponent(idempotencyKey),
        {
          method: "patch",
          contentType: "application/json",
          headers: {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
            "Prefer": "return=minimal",
          },
          payload: JSON.stringify({ webhook_status: status }),
          muteHttpExceptions: true,
        }
      );
      if (resp.getResponseCode() >= 200 && resp.getResponseCode() < 300) return;
    } catch (e) {
      Logger.log("updateWebhookStatus supabase error: " + e.toString());
    }
  }
  if (!SPREADSHEET_ID) return;
  try {
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var ws = ss.getSheetByName(WEBHOOK_LOG_SHEET);
    if (!ws) return;
    var idemColIdx = WEBHOOK_LOG_HEADER.indexOf("idempotency_key") + 1;
    var statusColIdx = WEBHOOK_LOG_HEADER.indexOf("webhook_status") + 1;
    var lastRow = ws.getLastRow();
    if (lastRow < 2) return;
    var keys = ws.getRange(2, idemColIdx, lastRow - 1, 1).getValues();
    for (var i = keys.length - 1; i >= 0; i--) {
      if (keys[i][0] === idempotencyKey) {
        ws.getRange(i + 2, statusColIdx).setValue(status);
        return;
      }
    }
  } catch (e) {
    Logger.log("updateWebhookStatus error: " + e.toString());
  }
}


/**
 * Nightly Render restart — memory hygiene for the hermes gateway.
 *
 * The gateway is one long-lived Python process; its RSS ratchets toward the
 * Render 512MB cap over a few days (OOM on 2026-07-22 when the 12:00 SGT
 * cron fired at ~95% baseline). A restart at ~04:00 SGT (quietest hour,
 * aligned with the gateway's daily session reset) resets the baseline.
 * Render restarts the container itself; this just asks its API to do so.
 * A webhook that lands during the ~1-min restart window is covered by the
 * WebhookLog audit row + the Sunday sweep — same guarantee as a deploy.
 *
 * One-time setup:
 *   1. Script Properties: add RENDER_API_KEY (Render dashboard → Account
 *      Settings → API Keys; token starts with "rnd_").
 *   2. Run setupRestartTrigger() once from the editor.
 * The service id below is not a secret (the API key is).
 */

const RENDER_SERVICE_ID = "srv-REPLACE-WITH-YOUR-SERVICE-ID";

function setupRestartTrigger() {
  ScriptApp.newTrigger("restartRenderService")
    .timeBased()
    .everyDays(1)
    .atHour(4)
    .create();
  Logger.log("Trigger created: nightly Render restart ~04:00 " + Session.getScriptTimeZone());
}

function restartRenderService() {
  var apiKey = PropertiesService.getScriptProperties().getProperty("RENDER_API_KEY");
  if (!apiKey) {
    Logger.log("restartRenderService: RENDER_API_KEY script property not set — skipping");
    return;
  }
  try {
    var resp = UrlFetchApp.fetch(
      "https://api.render.com/v1/services/" + RENDER_SERVICE_ID + "/restart",
      {
        method: "post",
        headers: { Authorization: "Bearer " + apiKey },
        muteHttpExceptions: true,
      }
    );
    var code = resp.getResponseCode();
    if (code >= 200 && code < 300) {
      Logger.log("restartRenderService: restart accepted (" + code + ")");
    } else {
      Logger.log(
        "restartRenderService: unexpected response " + code + ": " + resp.getContentText()
      );
      sendTelegramAlert(
        "⚠️ Nightly Render restart failed (HTTP " + code + "). Check RENDER_API_KEY."
      );
    }
  } catch (e) {
    Logger.log("restartRenderService error: " + e.toString());
    sendTelegramAlert("⚠️ Nightly Render restart errored: " + e.toString());
  }
}

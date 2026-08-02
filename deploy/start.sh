#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
mkdir -p "$HERMES_HOME"

# Write service account JSON to a file instead of inlining in .env
SA_PATH="/data/service-account.json"
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
    echo "${GOOGLE_SERVICE_ACCOUNT_JSON}" > "$SA_PATH"
    echo "Service account JSON written to $SA_PATH"
fi

# Export all vars so hermes sees them immediately
export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
export PEHD_LLM_USAGE_DIR="${PEHD_LLM_USAGE_DIR:-/data/llm_usage}"
export GOOGLE_SERVICE_ACCOUNT_JSON="$SA_PATH"
export GSPREAD_SPREADSHEET_ID="${GSPREAD_SPREADSHEET_ID:-}"
export WEBHOOK_HMAC_SECRET="${WEBHOOK_HMAC_SECRET:-}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS:-}"
export TELEGRAM_HOME_CHANNEL="${TELEGRAM_ALLOWED_USERS:-}"
export TELEGRAM_HOME_CHANNEL_NAME="Home"
export SUPABASE_URL="${SUPABASE_URL:-}"
export SUPABASE_SERVICE_KEY="${SUPABASE_SERVICE_KEY:-}"

# Also write to .env for hermes internals
cat > "$HERMES_HOME/.env" <<ENVEOF
OPENAI_API_KEY=${OPENAI_API_KEY}
PEHD_LLM_USAGE_DIR=${PEHD_LLM_USAGE_DIR:-/data/llm_usage}
GOOGLE_SERVICE_ACCOUNT_JSON=${SA_PATH}
GSPREAD_SPREADSHEET_ID=${GSPREAD_SPREADSHEET_ID:-}
WEBHOOK_HMAC_SECRET=${WEBHOOK_HMAC_SECRET:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_ALLOWED_USERS=${TELEGRAM_ALLOWED_USERS:-}
TELEGRAM_HOME_CHANNEL=${TELEGRAM_ALLOWED_USERS:-}
TELEGRAM_HOME_CHANNEL_NAME=Home
SUPABASE_URL=${SUPABASE_URL:-}
SUPABASE_SERVICE_KEY=${SUPABASE_SERVICE_KEY:-}
ENVEOF
echo "Environment written to $HERMES_HOME/.env"

# Link persistent disk paths
for dir in sessions memories cron llm_usage; do
    if [ -d "/data/$dir" ]; then
        ln -sfn "/data/$dir" "$HERMES_HOME/$dir"
        echo "$dir linked to persistent disk"
    else
        mkdir -p "/data/$dir"
        ln -sfn "/data/$dir" "$HERMES_HOME/$dir"
        echo "$dir created on persistent disk"
    fi
done

# Session retention (agreed with Hadi 2026-07-28): ROLLING windows keyed on
# last-activity mtime, applied at every start — the 04:00 nightly restart
# makes this effectively daily. Cron-job transcripts (~2/3 of disk growth,
# near-zero recall value): 30 days. Everything else (chats, webhook
# sessions): 365 days, so "what did we discuss in March" survives a full
# year. Failure mode is deliberately prune-NOTHING (a non-matching pattern
# deletes zero files, never extra); counts are logged so pattern drift is
# visible in Render logs. The FTS index (*.db*) is excluded — its mtime is
# always fresh anyway, this is belt and braces.
if [ -d /data/sessions ]; then
    cron_pruned=$(find /data/sessions -type f \( -name 'cron_*' -o -name 'session_cron*' \) -mtime +30 -print -delete 2>/dev/null | wc -l) || cron_pruned=0
    old_pruned=$(find /data/sessions -type f ! -name '*.db*' -mtime +365 -print -delete 2>/dev/null | wc -l) || old_pruned=0
    echo "Session retention: pruned ${cron_pruned} cron transcript(s) >30d, ${old_pruned} session file(s) >365d"
fi

# Inject webhook HMAC secret into config (can't use env vars in YAML)
if [ -n "${WEBHOOK_HMAC_SECRET:-}" ]; then
    sed -i "s/__WEBHOOK_SECRET_PLACEHOLDER__/${WEBHOOK_HMAC_SECRET}/" "$HERMES_HOME/config.yaml"
    echo "Webhook secret injected into config"
else
    # Fail LOUD: the placeholder is a literal string published in this repo,
    # so an unset secret means the ingest route will accept any request signed
    # with a value anyone can read. Every webhook is a ledger write.
    echo "WARNING: WEBHOOK_HMAC_SECRET empty at boot — the config still holds"
    echo "WARNING: __WEBHOOK_SECRET_PLACEHOLDER__, which is PUBLIC. Anyone who"
    echo "WARNING: knows this URL can sign a valid payload and write fabricated"
    echo "WARNING: transactions to your ledger. Set the variable and redeploy."
fi

# Inject the OpenAI key for voice STT (same M4 placeholder pattern as the
# webhook secret). The transcription resolver checks stt.openai.api_key
# BEFORE process env — the deterministic fix for the 2026-07-30 "no STT
# provider configured" regression after the faster-whisper eviction.
if [ -n "${OPENAI_API_KEY:-}" ]; then
    sed -i "s/__OPENAI_KEY_PLACEHOLDER__/${OPENAI_API_KEY}/" "$HERMES_HOME/config.yaml"
    echo "OpenAI key injected into stt config"
else
    echo "WARNING: OPENAI_API_KEY empty at boot — stt placeholder left in config"
fi

# Seed cron jobs once per persistent disk. The marker lives on /data so it
# survives container restarts but is re-seeded if the disk is recreated.
# Failures are non-fatal: we still start the gateway so the user can inspect
# logs and the next restart retries.
SEED_MARKER="/data/cron/.seeded"
if [ ! -f "$SEED_MARKER" ]; then
    echo "Seeding cron jobs (first run on this disk)..."
    if bash /app/cron/setup-cron-jobs.sh; then
        touch "$SEED_MARKER"
        echo "Cron jobs seeded successfully"
    else
        echo "WARNING: cron seeding failed — gateway will start anyway; next restart will retry"
    fi
else
    echo "Cron jobs already seeded (marker: $SEED_MARKER)"
fi

echo "Starting Hermes gateway (foreground)..."
echo "TELEGRAM_BOT_TOKEN is set: $([ -n "$TELEGRAM_BOT_TOKEN" ] && echo 'yes' || echo 'NO')"
echo "TELEGRAM_ALLOWED_USERS: ${TELEGRAM_ALLOWED_USERS:-not set}"

# Use 'gateway run' (foreground) — 'gateway start' requires systemd which Docker doesn't have
exec hermes gateway run < /dev/null 2>&1

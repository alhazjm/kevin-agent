FROM python:3.11-slim

# ffmpeg: lets hermes convert Edge-TTS mp3 output to OGG/Opus, which the
# Telegram gateway then sends with send_voice — a real voice bubble with a
# waveform, instead of an mp3 file attachment. The conversion is a short
# ffmpeg subprocess per spoken reply (transient RAM; fine post-whisper-
# eviction, PR #63 — don't ship this while local whisper is still in RAM).
RUN apt-get update && apt-get install -y \
    git \
    curl \
    tzdata \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Run the container in Singapore time so crontab expressions ("0 21 * * *")
# resolve to SGT wall-clock instead of UTC. Render containers default to UTC,
# which means "9 PM daily" fires at 5 AM SGT without this line.
ENV TZ=Asia/Singapore

# Cap glibc malloc arenas at 2. The gateway is one long-lived multi-threaded
# process (telegram polling + webhook server + in-process cron agent runs);
# glibc's arena-per-thread default fragments the heap so RSS ratchets to the
# Render 512MB cap over days and never comes back down — the 2026-07-22 OOM
# hit when the 12:00 SGT cron spun up an agent at ~95% baseline. Fewer arenas
# trade a sliver of allocator throughput for a flatter RSS. Pairs with the
# nightly Render-API restart trigger in apps-script/Code.gs
# (restartRenderService).
ENV MALLOC_ARENA_MAX=2

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Pinned hermes-agent commit. Bump this value to pull upstream updates.
# Changing the ARG invalidates the clone layer so Docker re-fetches.
# Before bumping, verify that upstream toolsets.py still contains the string
# '"send_message",' — the sed below depends on it.
ARG HERMES_AGENT_SHA=73d0b083510367adec42746e90c41ace16c0afb2
RUN git clone https://github.com/alhazjm/hermes-agent.git /app/hermes-agent \
    && cd /app/hermes-agent \
    && git checkout ${HERMES_AGENT_SHA}

WORKDIR /app/hermes-agent
RUN uv venv venv --python 3.11 && \
    . venv/bin/activate && \
    uv pip install -e ".[all]" && \
    uv pip install gspread google-auth

# Evict local faster-whisper so voice-note STT falls through to the OpenAI
# whisper-1 API instead of in-container CPU inference. The [all] extra
# installs faster-whisper and hermes' STT auto-detect prefers local — but
# loading the ~150MB "base" model into this 512MB instance is what tipped
# the 2026-07-30 12:01 OOM (clean 04:56 restart + morning webhooks + one
# voice note at 10:34 + the 12:00 cron). Post-removal detect order:
# local whisper CLI (absent) → Groq (no key) → OpenAI (OPENAI_API_KEY is
# set) — ~$0.006/audio-minute, and faster than base-model inference on
# 0.5 CPU anyway. If a future SHA bump drops faster-whisper from [all],
# this uninstall fails loud — re-evaluate then, don't blind-delete it.
RUN . venv/bin/activate && uv pip uninstall faster-whisper

# Pin the OpenAI STT model past the fork's aging whisper-1 default:
# whisper-1 has dropped off OpenAI's models page (deprecation path), and
# gpt-4o-mini-transcribe is cheaper (~$0.003/min) and more accurate. The
# fork reads this env var (transcription_tools.py: STT_OPENAI_MODEL) and
# already handles non-whisper response formats (json vs text). Newer
# "GPT Transcribe" family models are a one-var swap here once their exact
# IDs/pricing are confirmed against the pricing page.
ENV STT_OPENAI_MODEL=gpt-4o-mini-transcribe

COPY tools/sheets_client.py /app/hermes-agent/tools/sheets_client.py
COPY tools/expense_sheets_tool.py /app/hermes-agent/tools/expense_sheets_tool.py
COPY tools/card_optimiser.py /app/hermes-agent/tools/card_optimiser.py
COPY tools/travel_mode.py /app/hermes-agent/tools/travel_mode.py
COPY tools/supabase_client.py /app/hermes-agent/tools/supabase_client.py
COPY tools/loans.py /app/hermes-agent/tools/loans.py

# Register custom tools in hermes-agent source
RUN printf '\nimport tools.expense_sheets_tool\n' >> /app/hermes-agent/model_tools.py

# Inject expense_tracker tools into hermes-telegram toolset so the gateway
# always includes them — no config-level platform_toolsets needed.
RUN sed -i '/"send_message",/a\    # Expense tracker (custom)\n    "log_expense", "log_expense_pending", "update_budget", "get_remaining_budget", "edit_expense", "delete_expense", "link_telegram_message", "get_transaction_by_message_id", "lookup_merchant_category", "learn_merchant_mapping", "detect_subscription_creep", "undo_last_expense", "generate_spending_report", "write_insight", "get_insights", "generate_daily_insight", "render_budget_chart", "append_journal_entry", "get_journal_entries", "sweep_missed_transactions", "export_sheet_backup", "archive_year_snapshot", "get_card_cap_status", "get_bonus_pool_status", "recommend_card_for", "plan_month", "review_card_efficiency", "set_category_primary", "get_active_travel_mode", "get_trip_budget_status", "set_trip_bucket", "create_trip", "link_topup_to_trip", "create_loan", "mark_loan_repaid", "list_open_loans", "sweep_loan_offsets",' /app/hermes-agent/toolsets.py

# Patch hermes-agent run_agent.py to exit the agent loop cleanly when a tool
# result carries `"assistant_reply_required": false`. Our log_expense tool
# side-channels its own Telegram confirmation bubble, and prompt-level silence
# instructions are not a reliable runtime contract — this is the deterministic fix.
# v2 (PR #17): replaces the v1 zero-out, which still triggered the empty-
# response nudge/retry cascade. The patch script exits non-zero if the
# upstream anchor shifts (loud build failure). See CLAUDE.md "hermes-agent
# SHA bump checklist".
COPY deploy/patches/suppress_reply_on_silent_tools.py /app/patches/suppress_reply_on_silent_tools.py
RUN python3 /app/patches/suppress_reply_on_silent_tools.py \
    && grep -q "PEHD patch v2: exit agent loop when tool signalled silence" /app/hermes-agent/run_agent.py

# Patch Hermes retry telemetry and persist per-call LLM usage on /data.
COPY deploy/patches/suppress_retry_status_after_silent_tools.py /app/patches/suppress_retry_status_after_silent_tools.py
COPY deploy/patches/suppress_codex_incomplete_after_silent_tools.py /app/patches/suppress_codex_incomplete_after_silent_tools.py
COPY deploy/patches/log_llm_usage.py /app/patches/log_llm_usage.py
RUN python3 /app/patches/suppress_retry_status_after_silent_tools.py \
    && grep -q "PEHD patch: suppress retry status after silent tool delivery" /app/hermes-agent/run_agent.py \
    && python3 /app/patches/suppress_codex_incomplete_after_silent_tools.py \
    && grep -q "PEHD patch: suppress Codex incomplete after silent tool delivery" /app/hermes-agent/run_agent.py \
    && python3 /app/patches/log_llm_usage.py \
    && grep -q "PEHD patch: persist per-call LLM usage to /data" /app/hermes-agent/run_agent.py

# Prune built-in skill catalogs — this bot only uses the expense-tracker skill
# installed via COPY skills/ below. Keeps /skills output focused and avoids
# leaking irrelevant categories (pixel-art, github, gaming, etc.) into the
# agent's skill discovery surface.
RUN find /app/hermes-agent/skills -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

COPY skills/ /root/.hermes/skills/
COPY hermes-config/cli-config.yaml /root/.hermes/config.yaml
COPY deploy/start.sh /app/start.sh
COPY cron/setup-cron-jobs.sh /app/cron/setup-cron-jobs.sh
COPY hermes-config/USER.md /root/.hermes/USER.md
COPY hermes-config/MEMORY.md /root/.hermes/MEMORY.md
COPY hermes-config/SOUL.md /root/.hermes/SOUL.md
RUN chmod +x /app/start.sh /app/cron/setup-cron-jobs.sh

ENV HERMES_HOME=/root/.hermes
ENV PATH="/app/hermes-agent/venv/bin:$PATH"

EXPOSE 8644

CMD ["/app/start.sh"]

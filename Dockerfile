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
# Before bumping, work the "hermes-agent SHA bump" checklist in CLAUDE.md and
# re-verify BOTH patch anchors in deploy/patches/ at the candidate SHA.
#
# The old '"send_message",' toolsets.py anchor is GONE as of upstream v0.16.0:
# the agent-callable send_message tool and the whole `messaging` toolset were
# removed ("outbound platform messaging is handled outside the agent loop").
# Our 37 expense tools now reach the model ONLY via registry registration plus
# the `expense_tracker` entry in every platform_toolsets list in
# hermes-config/cli-config.yaml. There is no sed injection any more — and note
# that a sed with a non-matching address exits 0, so the old line had been
# failing SILENTLY, not loudly.
#
# Fetched from NousResearch/hermes-agent, the real upstream. If you keep
# your own fork, point the URL at it and keep the SHA pin.
#
# Currently pinned: v0.21.0 (tag v2026.8.31, 2026-08-31). History: 73d0b083
# (v0.10.0, 2026-04-19) -> 5fc308a7 (v0.20.6) on 2026-09-01 -> 29112bef
# (v0.21.0) on 2026-09-04. See docs/HERMES-0.20-MIGRATION-NOTES.md for the
# full delta and what each surviving patch had to change; the 0.21.0 step
# moved nothing we anchor on (both patch contexts byte-identical).
ARG HERMES_AGENT_SHA=29112bef099274229cadff79cdff7bf7b99c4b77
# Fetch ONLY the pinned commit, not the repository's history. A full clone
# pulls every one of upstream's ~24k commits (multi-GB, with binaries under
# apps/ native/ website/) from a Render build IP that is shared with other
# customers, and on 2026-09-04 GitHub answered that with HTTP 429 ("RPC
# failed; expected 'packfile'") and the deploy died at this step. A depth-1
# fetch of the SHA is ~70MB, is what actions/checkout does, and GitHub
# serves it for any commit reachable from a tag or branch. The .git dir is
# kept: hermes only checks that it EXISTS (install-type detection in
# hermes_cli/config.py) and never runs a git command; the package version is
# static in pyproject.toml. Three attempts with backoff so a transient 429
# does not cost a whole build; the final rev-parse makes a moved tag or a
# wrong SHA fail loud instead of shipping the wrong tree.
RUN git init -q /app/hermes-agent \
    && cd /app/hermes-agent \
    && git remote add origin https://github.com/NousResearch/hermes-agent.git \
    && for i in 1 2 3; do \
         git fetch --depth 1 origin "${HERMES_AGENT_SHA}" && break; \
         echo "fetch attempt $i failed; retrying in $((i*20))s"; sleep $((i*20)); \
       done \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${HERMES_AGENT_SHA}"

WORKDIR /app/hermes-agent

# Extras: [all] stopped carrying opt-in backends on 2026-05-12 and is now only
# {cron,pty,mcp,homeassistant,sms,acp,google,web,youtube}. python-telegram-bot
# lives solely in [messaging] and edge-tts solely in [edge-tts]. Both must be
# named explicitly or tools/lazy_deps.py pip-installs python-telegram-bot at
# first use — a runtime network install inside a 512MB container, repeated on
# every cold start including the nightly 04:00 Render restart.
RUN uv venv venv --python 3.11 && \
    . venv/bin/activate && \
    uv pip install -e ".[all,messaging,edge-tts]" && \
    uv pip install gspread google-auth

# faster-whisper is deliberately NOT installed. Loading its ~150MB "base"
# model into this 512MB instance is what tipped the 2026-07-30 12:01 OOM
# (clean 04:56 restart + morning webhooks + one voice note at 10:34 + the
# 12:00 cron), so PR #63 evicted it. Since upstream's 2026-05-12 repackaging
# it lives only in the [voice] extra, which we do not install — so the old
# `uv pip uninstall faster-whisper` line was removed here: uv exits 0 with a
# warning when the package is absent, so it could never have "failed loud"
# the way its comment promised. STT is the OpenAI API, pinned explicitly in
# cli-config.yaml (stt.provider: openai) rather than left to auto-detect.

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

# Registration is automatic at this SHA — tools/registry.py's
# discover_builtin_tools() (called from model_tools.py) AST-scans tools/*.py
# for a top-level registry.register() and imports the matches. This explicit
# import is KEPT as a fail-loud canary: discovery swallows an ImportError as a
# logger.warning, so without it a broken expense_sheets_tool would ship with
# ZERO tools registered and no build signal. Re-importing is a sys.modules
# no-op and re-registering the same name+toolset is an idempotent overwrite.
RUN printf '\nimport tools.expense_sheets_tool\n' >> /app/hermes-agent/model_tools.py

# TWO build-time patches, applied to upstream source in-place. Each script
# exits non-zero unless its anchor matches EXACTLY once, and each RUN then
# greps for the patch's marker string — so an upstream refactor breaks the
# Docker build loudly instead of silently shipping unpatched behaviour on
# Render. Never loosen an anchor to make the build pass (CLAUDE.md M3).
#
# Both now target agent/conversation_loop.py, not run_agent.py: upstream
# v0.15.0 extracted the whole agent loop into the module-level function
# run_conversation(agent, ...), so `self` no longer exists in that scope.
#
# Three sibling patches were RETIRED at the 0.20.6 bump because upstream
# does the job itself — see docs/HERMES-0.20-MIGRATION-NOTES.md:
#   suppress_retry_status_after_silent_tools  → retries are buffered now
#   suppress_codex_incomplete_after_silent_tools → sentinel hidden gateway-side
#                                                 (and patch v3 exits first)
#   skip_memory_flush_for_webhook_sessions    → _flush_memories_for_session
#                                                 no longer exists at all

# 1. Exit the agent loop when a tool result carries
#    `"assistant_reply_required": false`. log_expense side-channels its own
#    Telegram bubble, and prompt-level silence is not a reliable runtime
#    contract. v3 emits the literal NO_REPLY rather than an empty string:
#    upstream now rewrites an empty final_response into a delivered
#    "⚠️ Processing completed but no response was generated" bubble BEFORE any
#    silence check runs, so v2's "" would put one junk bubble in Telegram per
#    logged expense.
COPY deploy/patches/suppress_reply_on_silent_tools.py /app/patches/suppress_reply_on_silent_tools.py
RUN python3 /app/patches/suppress_reply_on_silent_tools.py \
    && grep -q "PEHD patch v3: exit agent loop when tool signalled silence" /app/hermes-agent/agent/conversation_loop.py

# 2. Persist per-call LLM usage as JSONL on /data so cost can be attributed
#    per platform without depending on Hermes internals or the OpenAI
#    dashboard. Record keys are unchanged across the 0.10→0.20 bump so
#    scripts/summarize_llm_usage.py and the before/after cost tables stay
#    comparable.
COPY deploy/patches/log_llm_usage.py /app/patches/log_llm_usage.py
RUN python3 /app/patches/log_llm_usage.py \
    && grep -q "PEHD patch: persist per-call LLM usage to /data" /app/hermes-agent/agent/conversation_loop.py

# Prune built-in skill catalogs — this bot only uses the skills installed via
# COPY skills/ below. Keeps /skills output focused and avoids leaking
# irrelevant categories (pixel-art, github, gaming, etc.) into the agent's
# skill discovery surface, which rides in the prompt.
#
# This is load-bearing at runtime, not just cosmetic: the gateway calls
# sync_skills() on EVERY start, sourcing from /app/hermes-agent/skills, so
# without this prune all 15 upstream skill categories are re-seeded into
# /root/.hermes/skills on every boot. The dir still exists at 0.20.6 (upstream
# also added optional-skills/, which is NOT synced — leave it alone).
RUN find /app/hermes-agent/skills -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

COPY skills/ /root/.hermes/skills/
# Slash-command bundles (/log, /undo, /budget, /summary). Since 0.20.x the
# gateway REJECTS any slash-command it does not recognise ("Unknown command")
# instead of forwarding it to the model as text, with no config switch. A
# bundle YAML registers /<name> natively and injects the listed skill bodies
# plus the user's text. Read from HERMES_HOME/skill-bundles/ — ephemeral, so
# it must be COPY'd every build (M1). Found in production 2026-09-04.
COPY hermes-config/skill-bundles/ /root/.hermes/skill-bundles/
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

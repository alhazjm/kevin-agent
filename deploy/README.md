# deploy/ — the container and its boot script

Two files ship into the image: `start.sh` (the entrypoint) and the two
build-time patches under `patches/`. The full first-run guide is
[docs/SETUP.md](../docs/SETUP.md); this page is the map of what happens at
build and at boot.

## What the build does (`Dockerfile`, repo root)

1. `python:3.11-slim` + `git curl tzdata ffmpeg`, `TZ=Asia/Singapore`,
   `MALLOC_ARENA_MAX=2` (a flatter memory profile for one long-lived
   multi-threaded process in a 512 MB container).
2. Fetches **one commit** of `NousResearch/hermes-agent` — the SHA in
   `ARG HERMES_AGENT_SHA` — with three retries and a final check that the
   commit it got is the one it asked for. Not a full clone: upstream's
   history is multi-gigabyte and GitHub rate-limits shared build hosts.
3. `uv pip install -e ".[all,messaging,edge-tts]"` plus `gspread google-auth`.
   `[all]` no longer includes Telegram or edge-tts, and without them the
   Telegram adapter would pip-install itself at every boot.
4. Copies the six `tools/*.py` files in and appends one import line to
   upstream's `model_tools.py` — a fail-loud canary, since tool discovery
   swallows import errors.
5. Applies the **two patches** in `patches/`. Each matches an exact anchor
   string in `agent/conversation_loop.py`, exits non-zero if the anchor is
   missing or ambiguous, and the Dockerfile then greps for the patch's
   marker. An upstream refactor breaks the build loudly instead of shipping
   unpatched behaviour. Never loosen an anchor to get green — see
   [docs/UPGRADING-HERMES.md](../docs/UPGRADING-HERMES.md).
6. Prunes upstream's bundled skill catalogue (the gateway re-seeds from that
   directory on every start), then copies in `skills/`, `cli-config.yaml`,
   the four slash-command bundles, the three memory files, `start.sh` and
   the cron seeding script.

## What the patches do

| Patch | Why it exists |
|---|---|
| `suppress_reply_on_silent_tools.py` (v3) | `log_expense` sends its own Telegram bubble. Without this, the model then sends a second reply. The patch exits the agent loop when the latest tool result carries `assistant_reply_required: false`, emitting hermes' own `NO_REPLY` silence token (an empty reply is rewritten into a warning bubble by upstream). It scans only the current turn and closes the transcript with the model's own words, for reasons the file documents at length. |
| `log_llm_usage.py` | Appends one JSON line per API call to `/data/llm_usage/usage-YYYY-MM.jsonl` — platform, session, tokens, cache hits, cost — so you can attribute spend without the OpenAI dashboard. `scripts/summarize_llm_usage.py` reads it. |

Both honour `PEHD_PATCH_TARGET=<path>` so you can dry-run them against a
downloaded copy of upstream before bumping the pin.

## What `start.sh` does at boot

1. Writes `GOOGLE_SERVICE_ACCOUNT_JSON` (if set) to `/data/service-account.json`.
2. Exports every env var and writes `/root/.hermes/.env`.
3. Symlinks `sessions/`, `memories/`, `cron/`, `llm_usage/` from the
   persistent disk into `HERMES_HOME`, creating them on first boot.
4. Prunes old session transcripts (cron transcripts after 30 days,
   everything else after 365).
5. Substitutes the two placeholders in `config.yaml` — hermes YAML cannot
   read env vars, so `__WEBHOOK_SECRET_PLACEHOLDER__` and
   `__OPENAI_KEY_PLACEHOLDER__` are replaced with `sed`. **If
   `WEBHOOK_HMAC_SECRET` is unset the script prints four `WARNING:` lines and
   the published placeholder becomes your live signing key.**
6. Seeds the six cron jobs once per disk (marker `/data/cron/.seeded`).
7. `exec hermes gateway run` in the foreground (`gateway start` wants
   systemd, which the container lacks).

## The two filesystems

`/root/.hermes/` is rebuilt on every deploy — ephemeral. `/data/` is the
1 GB persistent disk. Anything that must survive a deploy lives on `/data`
and is symlinked in by `start.sh`; anything copied by the Dockerfile is
replaced on the next build.

## Environment variables

`render.yaml` is authoritative; `.env.example` lists the same nine with
comments. The Google pair is optional (Sheet backup only); everything else
is required, and `WEBHOOK_HMAC_SECRET` is the one you must not skip.

## Hosting elsewhere

`render.yaml` is Render-specific; the `Dockerfile` is not. Any host that
gives you a container with a persistent volume at `/data`, environment
variables, and a process that is never put to sleep will work — the gateway
polls Telegram, serves the webhook on port 8644, and runs cron in-process.
See "Swapping the pieces" in [docs/SETUP.md](../docs/SETUP.md).

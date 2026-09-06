# hermes-agent 0.10 → 0.21 (via 0.20.6) — what changed, why, and how it works now

**The bump**: `HERMES_AGENT_SHA` `73d0b083` (v0.10.0, 2026-04-19) →
`5fc308a70719a83cccdbba4c0e39c23f5a8239d5` (v0.20.6, tag `v2026.8.27`,
2026-08-27). ~18k upstream commits, 15 releases.

**Re-pinned 2026-09-04 to `29112bef099274229cadff79cdff7bf7b99c4b77`
(v0.21.0, tag `v2026.8.31`, 2026-08-31, "The Pantheon Release")** — the owner
spotted it had shipped four days after the SHA above. 911 commits on top of
0.20.6, and the full SHA-bump checklist was re-run against a fresh snapshot:
both patch anchors ×1 with **byte-identical** surrounding context, both
patches apply and compile with the `break` still bound to the main loop,
`gateway/response_filters.py` and `agent/skill_bundles.py` unchanged, retry
statuses still buffered, tool discovery / cron `platform_toolsets`
resolution / `wrap_response` / `[SILENT]`-not-soft-fail all intact, the
V1 HMAC verify block identical, memory and SOUL paths unchanged, and the set
of default-on auxiliary forks identical to 0.20.6 — so the five disables in
`cli-config.yaml` are still complete. Nothing in `deploy/patches/`,
`cli-config.yaml`, or the bundles needed to change; only the pin moved.
Everything below was written against 0.20.6 and remains accurate at
0.21.0. New in 0.21.0 and deliberately left alone: per-job cron
`--continuity` and a durable per-job notepad (both opt-in; the notepad
renders as an empty string until a job writes to it), Bot Mode (desktop
only), and a Telegram inline command picker.

This is the reference for *what is different about Kevin now*. It supersedes
the research in `docs/HANDOFF-hermes-0.20-migration.md` (a private-repo handoff, not exported here), which was written on
2026-08-17 against upstream `main` — five of its conclusions turned out to be
wrong at the SHA we actually pinned, and those are called out below. Every
claim here was re-verified against a local snapshot of upstream at
`5fc308a7`, not against the handoff.

> Ported from the private deployment this repo is exported from. PR
> numbers (#103, #104, #105) refer to that repository's history, not this
> one. The behaviours, anchors and config it describes are what ships here.

---

## 1. The one-paragraph version

Upstream tore the agent loop out of `run_agent.py`, deleted the tool Kevin's
tool-injection hack was anchored to, started allowlisting cron's tools per
platform, changed what "say nothing" means, and switched on three background
LLM features that did not exist before. Four of our five build-time patches
stopped applying. Two were rewritten, three were deleted because upstream now
does their job. The two changes that would have broken production silently —
cron losing every expense tool, and the silence contract emitting a warning
bubble per transaction — are both fixed in this PR.

---

## 2. The five things that would have broken

Ordered by how badly, and by how quietly.

### 2.1 All six cron jobs would have lost all 37 expense tools — silently

**What changed.** At the old pin, the cron scheduler built its agent with
`disabled_toolsets=[...]` and no allowlist, so *every registered tool*
reached every job. At 0.20.x it passes an `enabled_toolsets` allowlist
resolved from the `cron` platform's entry in `platform_toolsets`.

**Why it is dangerous.** We had no `platform_toolsets.cron`. With none, the
default `hermes-cron` composite applies — and `expense_tracker` is not in it.
Every job would have run with zero expense tools: the 21:00 review, the
Friday summary, the 1st-of-month report, the Sunday sweep, the 03:00 Sheet
export, the Jan-1 archive. `get_remaining_budget`, `export_sheet_backup`,
`sweep_missed_transactions` would all be "unknown tool".

It fails **completely silently**. There is no "unknown toolset" warning,
because every name in the default composite is itself valid — the list is
simply the wrong list. The first symptom would have been noticing, days
later, that the nightly Sheet backup had stopped moving.

**How it works now.** `platform_toolsets.cron: [expense_tracker]` in
`cli-config.yaml`. `expense_tracker` alone is enough: `--skill` injection does
not go through the `skills` toolset, and `cronjob`/`clarify` are subtracted by
the scheduler anyway. `hermes cron create` has no toolset flag, so this
**cannot** be fixed in the seeding script — config is the only lever.

> The handoff said "`memory` is always disabled in cron". **That is false at
> this SHA.** Cron now also loads MEMORY.md / USER.md / SOUL.md into every
> scheduled run, which is a new per-run token cost across six jobs. We do not
> list `memory` in the cron toolset, so the memory *write* tools stay out;
> the context files load regardless.

### 2.2 The silence contract would have put a ⚠️ bubble in Telegram per transaction

**What changed.** Upstream added `gateway/response_filters.py` with an
explicit rule: *"A blank response is also not silence; blank output is
handled by the empty-response failure path."* An empty `final_response` is
now rewritten by `_normalize_empty_agent_response` into a **delivered**
message: "⚠️ Processing completed but no response was generated."

**Why it is dangerous.** Our patch v2 achieved silence by setting
`final_response = ""`. Worse, the first of that function's two call sites
runs *inside* `TurnRunner.run_sync`, **before** any silence predicate is
consulted — so there is no way to opt out after the fact. Our ingest route is
`deliver: telegram`, so every bank email would have produced the real
`log_expense` bubble *plus* a junk warning bubble. That is precisely the
duplicate-bubble failure (M9) the patch exists to prevent.

**How it works now.** Patch v3 emits the literal string `NO_REPLY` — the
value of upstream's own `SILENT_REPLY_TOKEN` and a member of
`LIVE_GATEWAY_SILENT_MARKERS`. It is swallowed by the strict matcher
(Telegram), the loose matcher (webhook adapter) and cron alike. Silence is
now something we *say*, not something we leave blank.

### 2.3 A latent bug in the patch that `NO_REPLY` would have made much worse

Found by adversarial review of the migration plan, not by the plan itself.

**The bug.** v1/v2 scanned `messages` backwards for the most recent tool
result carrying the silence flag — **unbounded**. But `messages` is the *full*
conversation: this turn's rows appended onto replayed history, and history
carries `role="tool"` rows.

**What that means.** Turn N: you text `/log $30 IKEA`, `log_expense` runs, its
result (carrying `"assistant_reply_required": false`) stays in history. Turn
N+1: you say "thanks", the model answers with no tool calls, and the scan
walks past this turn's user message straight into turn N's tool result — and
concludes it should stay silent.

Under v2's `""` that misfire was wrong but *visible* (you'd get the ⚠️
warning). Under a real silence marker it is **invisible**: your message gets
no reply at all, and nothing is logged. The token change would have converted
a loud wrong answer into a silently dropped turn.

**How it works now.** The scan is bounded to this turn only, starting at
`current_turn_user_idx` (a live local of `run_conversation`, re-anchored after
mid-turn compression before our anchor is reached). Upstream fixed the exact
same bug shape for `MEDIA:` tags the same way — *"Scope the scan to THIS
turn's tool results only … (Fixes #34608)"*.

Our own tool text had already flagged the hazard at the prompt layer
(`expense_sheets_tool.py`: *"THIS TURN ONLY: in later turns, direct user
questions must always get a text answer"*). The patch just never enforced it.

Two follow-on defects in that bound were caught by review of this PR itself
and are fixed here:

- **The `-1` sentinel.** `reanchor_current_turn_user_idx` returns `-1` when
  compaction leaves no user-originated message. The first version of the
  guard wrote `max(0, idx)`, which quietly turned that sentinel back into a
  scan from index 0 — reinstating the exact unbounded scan. The patch now
  **skips the silence exit entirely** on a negative index. Failing that way
  round is deliberate: a missed silence is a duplicate bubble (visible), a
  false silence is a message dropped without a trace.
- **The marker was being written into history.** See §2.6.

### 2.6 `NO_REPLY` would have been persisted into session history

Also caught by review of this PR, and it is a *new* risk created by the fix
in §2.2 — worth understanding, because it is the kind of thing that only
appears when you change a value from falsy to truthy.

**The mechanism.** `turn_finalizer.finalize_turn` enforces an invariant:
*"delivered final_response ⇒ assistant row in transcript"* (#43849/#44100).
If `final_response` is truthy and the transcript tail is not an assistant
row, it appends `{"role": "assistant", "content": final_response}` and
persists it.

After our early `break` the tail is this turn's `role="tool"` row. So with
`final_response = "NO_REPLY"` the finalizer writes **an assistant row whose
entire content is the literal string `NO_REPLY`** — into the durable Telegram
session transcript, once per silent expense log. v2's `""` was falsy and
never entered that branch, so this was new at v3.

**Why that is bad.** Nothing upstream ever teaches the model that token — it
appears in no prompt, no skill, no system message; the only source of
`NO_REPLY` in any of our sessions would be this patch. Replayed history would
therefore become a growing set of few-shot examples of a string the gateway
swallows without a trace. If gpt-5.4-nano ever imitated it on a real
question, the reply would vanish silently — the §2.3 failure mode, arriving
by a different road.

**How it works now.** The patch closes the turn itself, with the model's own
words: it appends `{"role": "assistant", "content": assistant_message.content
or ""}` before setting `final_response = "NO_REPLY"`, guarded so it never
creates an `assistant → assistant` pair. The transcript records what the
model actually said (usually nothing, or a "Logged." we chose not to
deliver); only the *delivery* value is the marker. Verified by replaying
upstream's finalizer logic against the exact post-break message state:
`NO_REPLY` present in the transcript before the fix, absent after.

### 2.4 Telegram would have pip-installed itself at every cold start

**What changed.** On 2026-05-12 upstream repackaged `[all]` to contain only
what cannot be lazy-installed. It is now just
`{cron, pty, mcp, homeassistant, sms, acp, google, web, youtube}`.
`python-telegram-bot` lives solely in `[messaging]`; `edge-tts` solely in
`[edge-tts]`.

**Why it matters.** With `.[all]` alone, the Telegram adapter would trigger a
*runtime* `pip install` on first use — inside a 512MB container, over the
network, repeated on every cold start including the nightly 04:00 Render
restart.

**How it works now.** `uv pip install -e ".[all,messaging,edge-tts]"`.

### 2.5 The tool-injection sed had already been failing silently

**What changed.** Upstream removed the agent-callable `send_message` tool and
the entire `messaging` toolset in v0.16.0 ("outbound platform messaging is
handled outside the agent loop"). The string `"send_message",` no longer
appears in `toolsets.py`.

**Why it matters.** Our Dockerfile injected all 37 tool names after that
anchor. **A `sed` address that matches nothing exits 0.** So the build would
have succeeded while injecting nothing — no error, no warning, no signal. The
"fail loud" promise in the Dockerfile comment was never true for `sed`, only
for the patch scripts (which grep for their markers afterward).

**How it works now.** The sed is deleted. Our tools were always *actually*
delivered by `platform_toolsets` + registry passthrough; the sed had been
redundant belt-and-braces even before it broke. This is why **M2's rule
changed from four places to three** — see §5.

### 2.7 `/log` answered with "Unknown command" — found in production

The one the verification fan-out missed. Reported by the owner on 2026-09-04,
the first manual entry after deploy: `/log $3 test kopi` →
*"Unknown command `/log`. Type /commands to see what's available, or resend
without the leading slash."*

**What changed.** The gateway now resolves every slash-command against
three registries — built-ins, skill bundles, skills — and if none match it
**replies with that notice and never forwards the text to the model**.
Upstream's stated reason: silently forwarding unknown commands "leads to
silent-failure behavior like the model inventing a delegate_task call".
At the old pin, `/log …` reached the model as plain text and the model
loaded the expense-tracker skill itself. There is no config switch.

**Blast radius.** Every slash-command our skills teach you to type:
`/log`, `/undo`, `/budget`, `/summary`. (`undo` as a plain word still
worked; `/undo` did not.)

**How it works now.** A **skill bundle** per command in
`hermes-config/skill-bundles/*.yaml`, shipped to
`/root/.hermes/skill-bundles/`. A bundle's `name:` registers `/<name>`
natively; on invocation the gateway injects every listed skill's full body,
the bundle's `instruction:`, and the text you typed after the command, then
runs a normal agent turn. Bundles are dispatched before skills, and `log`
collides with nothing (it is only a `kanban` *sub*command upstream).

| Command | Loads | Note |
|---|---|---|
| `/log` | `expense-tracker` | Restates `source="manual"` and invariant #1 (`bubble_sent: true` → EMPTY reply) |
| `/undo` | `expense-tracker` | Restates preview-then-confirm |
| `/budget` | `budget-manager` | Text after the command = category |
| `/summary` | `weekly-summary` | Text after the command = period |

This is what the old behaviour did implicitly, made explicit — `/log` now
costs one expense-tracker body (~10k tokens) per manual entry, which is
what the model was loading via `skill_view` before anyway, minus the extra
tool call.

Guarded by `tests/test_skill_bundles.py`: upstream's loader is deliberately
forgiving (a bundle naming a non-existent skill still loads, with a quiet
"skipped" note to the model), so a typo would ship as a slash command that
injects nothing. The test pins every listed skill to an existing
`skills/<name>/SKILL.md` and checks the Dockerfile ships the directory.

### 2.8 The build itself: GitHub rate-limited the clone

Not an upstream behaviour change — a consequence of upstream's *size*. The
first Render build after #104 died at `git clone` with
`RPC failed; HTTP 429 … expected 'packfile'` (2026-09-04). A full clone
pulls the entire ~24k-commit history — multi-GB, with binaries under
`apps/`, `native/`, `website/` — from a Render egress IP shared with other
customers, and GitHub throttled it. The identical line had worked the day
before; this is load-dependent and would recur.

**How it works now (PR #105).** `git init` + `git fetch --depth 1 origin
<SHA>` + `checkout FETCH_HEAD`: only the pinned tree (~70 MB), which is
exactly what `actions/checkout` does. Three attempts with 20/40/60 s
backoff, and a final `rev-parse` equality check so a moved tag or a typo
fails loud instead of shipping the wrong tree. Safe to shallow because
hermes only checks that `.git` *exists* (install-type detection in
`hermes_cli/config.py`) and never runs a git command; the version is
static in `pyproject.toml`. The exact `RUN` body was dry-run against
GitHub from a scratch directory before shipping.

---

## 3. The patches: five → two

Both survivors now target `agent/conversation_loop.py`. Upstream v0.15.0
extracted the whole agent loop out of `run_agent.py` into a module-level
function `run_conversation(agent, …)`, so there is no `self` in that scope —
every receiver became `agent.`.

### Kept

| Patch | Anchor at 5fc308a7 | What changed |
|---|---|---|
| `suppress_reply_on_silent_tools.py` (**v3**) | `agent/conversation_loop.py`, 12sp `if agent.api_mode == "codex_responses" and finish_reason == "incomplete":` (×1) | New file, new anchor, `self.`→`agent.`, emits `NO_REPLY`, scan bounded to this turn, moved *ahead* of the codex continuation block |
| `log_llm_usage.py` | `agent/conversation_loop.py`, 20sp `agent.session_cost_status = …` block (×1) | New file, `self.`→`agent.`, indent 24→20, **injection now imports its own `datetime`/`Path`** |

**Why patch v3 sits before the codex block** (your call, 2026-09-01). We run
the `codex_responses` leg — provider `custom` against `api.openai.com` is
auto-upgraded to it. A reasoning-only follow-up after a silent tool otherwise
burns up to **3 continuation API calls** before it ever reaches the
no-tool-calls branch ~780 lines later. Exiting early skips them.

The trade-off, stated plainly: `finish_reason == "incomplete"` with no tool
calls means the response was *truncated*, so we abort a turn upstream would
have retried 3×. That is what we want here — `log_expense` has already
written the ledger row and sent the bubble, and everything left is prose we
would suppress anyway. The `not assistant_message.tool_calls` guard keeps
genuine multi-tool turns alive.

**Why `log_llm_usage` needed local imports.** `conversation_loop.py` imports
`json` and `os` at module level but **not** `datetime` or `pathlib.Path`. The
whole record write is wrapped in `except Exception: pass`, so a `NameError`
would have failed *silently*: green build, marker present, `/data/llm_usage`
empty forever. This is the single most dangerous failure mode of that patch,
and it is why there is an acceptance test for it in §7.

### Retired

| Patch | Why it's gone |
|---|---|
| `suppress_retry_status_after_silent_tools.py` | Retry statuses are now **buffered** (`agent._buffer_status`) and flushed only on terminal failure. The only unbuffered path is Z.AI-specific and we're on OpenAI. |
| `suppress_codex_incomplete_after_silent_tools.py` | The "Codex response remained incomplete" sentinel is now returned as a *string* and blanked gateway-side. Patch v3 also exits before that block is reached. |
| `skip_memory_flush_for_webhook_sessions.py` | **`_flush_memories_for_session` no longer exists**, and the session-expiry watcher runs no agent at all. The 2026-08-17 problem — 661 of 700 platform-less requests belonging to webhook sessions, ~40% of all tokens burned at 04:00 for nothing — is fixed upstream. Nothing to re-anchor to. |

Two residual risks worth knowing, neither a blocker:

- **Retry chatter on a terminally failed ingest.** The webhook lane is exempt
  from the gateway's noisy-status filter, so if an ingest exhausts its
  retries, the buffered "⏳ Retrying in 2.5s…" lines now flush through to
  Telegram. Noisier failure bubble, not a duplicate-bubble regression — a
  failing ingest was never silent anyway.
- **Cron is not covered by upstream's codex-sentinel fix.** The cron lane has
  its own result handler and never calls the gateway's hidden-incomplete
  check, so a codex-incomplete on a cron turn produces a visible failure
  message. Not live today: none of the six jobs calls a bubble-sending tool,
  so the branch is unreachable. It becomes live the moment a cron prompt calls
  `render_budget_chart`.

---

## 4. New upstream behaviour we deliberately turned off

All default-**on** upstream, none existed at our old pin.

| Config key | What it does | Why off |
|---|---|---|
| `auxiliary.background_review.enabled` | Post-turn "self-improvement" agent fork, ~30k tokens per fire, 600k input cap | Its job is to write MEMORY.md / create skills, but `/root/.hermes` is rebuilt every deploy so the output is discarded. Our tier-2 memory is hand-curated (M18). **No per-platform switch — global, Telegram included.** |
| `auxiliary.title_generation.enabled` | Auto-names sessions, on the **main** model | Webhook sessions are *not* excluded upstream (only `cron` and `subagent` are) — this would have been one extra LLM call **per ingested bank email** |
| `curator.enabled` | Weekly prune/archive pass over `HERMES_HOME/skills` | That is where our five runtime skills live. It only adopts agent-created skills today, so ours aren't candidates — but it ticks a background thread and can rewrite cron-job skill references after a consolidation. Cheap insurance. |
| `lsp.enabled` | Spawns language servers + a background event loop | Unused; RAM on a 512MB box |
| `model_catalog.enabled` | Periodically fetches a remote model registry | Unused; our model is pinned. A new outbound network dependency we don't need. |

> The handoff said the curator "would prune OUR four skills". **That reason is
> wrong** — curation candidates are seeded from agent-created skills, and ours
> are COPY'd in, so they're invisible to it. The recommendation still stands
> for the other two reasons. Don't record the wrong reason.

Also new and left alone: `models.dev` is now fetched as a model registry;
the gateway runs ~11 supervised background watchers; per-route `rate_limit`
(30/min) and `max_body_bytes` (1MB) defaults apply to the webhook — our Apps
Script pacing (2 posts per 5-minute tick) is far under both.

---

## 5. Rules that changed in CLAUDE.md / AGENTS.md

- **M2 is now a three-place rule, not four.** The Dockerfile sed line is
  gone. A tool needs: `registry.register(…)`, a SKILL.md reference, tests —
  **plus** `expense_tracker` present in every `platform_toolsets.<platform>`
  list that should see it (`telegram`, `webhook`, `cron`). The trap moved
  from "missing from the sed list" to "missing from a platform's toolset
  list", and it is just as silent.
- **The SHA-bump checklist was rewritten** against the new file paths, and
  grew two items: re-check that `NO_REPLY` is still in
  `LIVE_GATEWAY_SILENT_MARKERS` (the whole silence contract hangs on it), and
  re-enumerate default-on auxiliary forks (a new one appears and starts
  costing tokens with no config change on our side).
- **"Five build-time patches" → "TWO"**, with the retirement reasons recorded
  so nobody resurrects them.
- **`cron.wrap_response`** line reference was stale; replaced with a grep
  instruction, and a new item 10 documents the cron toolset allowlist and the
  `[SILENT]` requirement.

Other live surfaces swept for now-wrong instructions, all fixed here:

- `docs/SETUP.md` described the sed and "four patches", and its
  troubleshooting table prescribed a no-op fix for "the model never calls my
  tool". Corrected, plus new rows: the ⚠️-bubble symptom, the cron
  "unknown tool" / silent-export symptom, and the "Unknown command"
  slash-command symptom.
- `hermes-config/MEMORY.md.example` and `USER.md.example` both claimed
  "Loaded into the system prompt every turn", which §6 shows is false for
  those two files. Their headers now say where they are actually read from
  and how to make them live. `SOUL.md.example`'s header was correct and is
  only clarified.

Left alone deliberately: `docs/CARD-OPTIMISER-ARCHITECTURE.md` and
`docs/SWEEP-ARCHITECTURE.md` also mention the sed injection. They are dated
design records of work already shipped, not live instructions, so they keep
their history. If you follow a checklist in one of them, translate "sed
injection" to "`platform_toolsets`".

---

## 6. One thing found but deliberately NOT fixed

**`MEMORY.md` and `USER.md` have never been loaded into the system prompt.**

This was an open question in the handoff. It is now settled: the prompt
builder reads `SOUL.md` from `HERMES_HOME` (exactly where the Dockerfile puts
it, so SOUL.md works), but `MEMORY.md`/`USER.md` are read by the memory store
from `HERMES_HOME/memories/` — and `start.sh` symlinks that to
`/data/memories`. So the two files COPY'd to `/root/.hermes/` sit there
unread. What the agent actually reads under those names is whatever is on the
persistent disk.

This is **pre-existing** — it predates the bump and the bump doesn't change
it. It is not fixed here because seeding those files would change how Kevin
behaves, and that deserves its own PR with its own before/after. What this PR
does is stop the docs from claiming otherwise.

Practical consequence in the meantime: edits to `hermes-config/MEMORY.md` and
`USER.md` are documentation of intent, not a deploy. Keep writing them
correctly; just don't assume they took effect.

---

## 7. Verification

Done before merge:

- [x] Every anchor and config claim re-verified against a local snapshot of
      upstream at `5fc308a7` (10-agent fan-out, including an adversarial pass
      that caught §2.3).
- [x] Both patches applied to a pristine copy of upstream
      `conversation_loop.py` via `PEHD_PATCH_TARGET`: exit 0, markers present,
      idempotent on re-run, and the result **compiles**.
- [x] Injected `break` confirmed by AST to bind to the main agent loop (line
      2018), not the inner scan loop.
- [x] Fail-loud confirmed: with the anchor renamed, the patch exits 3.
- [x] Both patches apply in **either order** (they inject ~2600 lines apart
      and shift each other's line numbers) and the result still compiles.
- [x] §2.6 proven by replaying upstream's `finalize_turn` invariant block
      against the exact post-break message state: `NO_REPLY` lands in the
      transcript before the fix, and does not after.
- [x] All five names the injection reads (`current_turn_user_idx`,
      `messages`, `final_response`, `_turn_exit_reason`, `assistant_message`)
      confirmed bound before the injection point.
- [x] All 11 root keys in `cli-config.yaml` confirmed present in upstream's
      known-root-keys set — no "unknown key" warnings.
- [x] Every disabled aux key path confirmed to exist with default `True`.
- [x] `bash -n cron/setup-cron-jobs.sh` clean; no comment lines inside
      Dockerfile `RUN` continuations.
- [x] `pytest tests/ -q` → **626 passing** at the time (648 in this repo now), unchanged (no `tools/*.py` touched).

Still to do on the running container (the build is the first real gate):

- [ ] `docker build` green — every patch grep must pass.
- [ ] `hermes doctor`; startup log shows no unknown-root-key warning and no
      double registration of `log_expense`.
- [ ] `hermes cron list` shows 6 jobs. Trigger the 03:00 export manually:
      the Sheet export timestamp must move (proves `platform_toolsets.cron`
      works) and Telegram must receive **nothing**.
- [ ] One signed webhook replay → **exactly one** Telegram bubble, no ⚠️
      warning bubble, no second reply. Check the `transactions` row and
      `webhook_log`.
- [ ] Telegram: `/log $3 test kopi` → one bubble, silent after. Then a
      follow-up "thanks" **must get a reply** — that is the §2.3 regression
      test. Then `undo` (preview-then-confirm), then one voice note (STT).
- [ ] `wc -l /data/llm_usage/usage-$(date +%Y-%m).jsonl` **> 0**. Zero rows
      with a green build means the usage patch's local imports were dropped.
- [ ] Render RSS steady under 512MB across a webhook + a voice note + a cron.

---

## 8. Manual steps

1. **Re-seed cron** — the two prompts changed, and live jobs keep their old
   text: `hermes cron remove` ×6, `rm /data/cron/.seeded`, restart.
   Without this, the sweep and export jobs keep saying "produce an EMPTY
   response" and keep booking soft-fails. *(done on the original deployment 2026-09-04)*
2. No SQL migration. No `clasp push`. No new env vars. No Render config
   change.
3. `docs/HANDOFF-hermes-0.20-migration.md` (private repo) is now historical — this file is
   the current reference.

### 8a. Follow-up deploy: the slash-command bundles (§2.7)

Merging the bundles PR redeploys the container; nothing else is needed —
no cron re-seed (prompts unchanged), no env vars, no SQL. After it goes
live, the §7 Telegram check is the acceptance test: `/log $3 test kopi`
must produce one bubble (not "Unknown command"), and "thanks" afterwards
must get a reply. Try `/budget` once too.

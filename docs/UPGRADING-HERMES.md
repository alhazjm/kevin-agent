# Upgrading hermes-agent yourself

This repo is a thin shell around [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
pinned to one exact commit by `ARG HERMES_AGENT_SHA` in the `Dockerfile`.
Upstream ships a release every week or two. Nothing here depends on the
original author to move that pin: this page is the whole procedure, the
weekly [anchor-check workflow](../.github/workflows/anchor-check.yml) does
the tedious part for you, and every check is something you can run on your
own machine in a few minutes.

Read [HERMES-0.20-MIGRATION-NOTES.md](HERMES-0.20-MIGRATION-NOTES.md) once
if you want to see a full-size upgrade worked end to end, including two
changes that would have broken production silently. This page is the
routine version.

## 1. Why upgrades can break this repo at all

Most software uses a framework like an appliance: plug in, press the
buttons it offers. Two things here could not be done through hermes'
buttons, so the build **edits hermes' source** before installing it:

| Patch (`deploy/patches/`) | What it does | Anchored on |
|---|---|---|
| `suppress_reply_on_silent_tools.py` | Exits the agent loop when a tool has already sent the Telegram message, so the model does not send a second one | one exact line in `agent/conversation_loop.py` |
| `log_llm_usage.py` | Writes one JSON line per API call to the persistent disk for cost attribution | a four-line block in `agent/conversation_loop.py` |

Each script looks for its anchor string **exactly once**, inserts code
around it, and exits non-zero otherwise; the Dockerfile then greps for a
marker. So when upstream moves or reindents that code, the build fails
**loudly** — which is the design. What it cannot catch is *behaviour*
drifting while the anchor stays put, which is why the checklist below tests
behaviours too (the silence token, the cron toolset rule, the
slash-command bouncer, the default-on background features).

The config (`hermes-config/cli-config.yaml`) and the four slash-command
bundles rely on upstream behaviours the same way. All of them are listed in
section 3.

## 2. When to upgrade

- **The weekly issue says "Safe to bump."** `anchor-check.yml` runs every
  Monday (and on demand from the Actions tab), downloads the newest tag,
  runs every check in section 3, and creates or updates one issue titled
  `hermes-agent upstream: <tag>`. Green means the mechanical part is done;
  you still read the release notes.
- **Cadence.** Monthly is comfortable; quarterly is the outer limit. The
  0.10 → 0.20 gap was four and a half months and cost a full day, because
  every change arrived at once. Four small bumps would have been four
  twenty-minute jobs.
- **Never pin to a floating branch.** Always a tag's commit SHA, so the
  build is reproducible and the anchor check has something to test against.

## 3. The checklist (what "safe" means)

Run these against the candidate SHA. The workflow automates every one; this
is the same list for doing it by hand or for understanding a red run.

1. `agent/conversation_loop.py` contains the silence-patch anchor
   `            if agent.api_mode == "codex_responses" and finish_reason == "incomplete":`
   (12-space indent) **exactly once**, and the locals the injection reads
   (`current_turn_user_idx`, `messages`, `final_response`,
   `_turn_exit_reason`, `assistant_message`) are still bound before it.
2. The same file contains the usage-patch anchor (the 20-space
   `agent.session_cost_status = cost_result.status` / `…cost_source…` /
   blank / `# Persist token counts to session DB for /insights.` block)
   exactly once, and still does **not** import `datetime` or `Path` at
   module level (the patch imports them itself; if upstream adds them the
   local imports stay harmless).
3. `gateway/response_filters.py` still lists `NO_REPLY` in
   `LIVE_GATEWAY_SILENT_MARKERS`, with both `is_intentional_silence_response`
   and `is_autonomous_silence_response` present. The silence patch emits
   that token; if the marker set changes, every lane double-replies at once.
4. Retry statuses still go through `agent._buffer_status(…)`, not
   `_emit_status`. That buffering is why a third patch was retired; if it
   reverts, "⏳ Retrying…" lines reach Telegram during silent ingests.
5. `model_tools.py` calls `discover_builtin_tools()` and `tools/registry.py`
   defines it — custom tool registration depends on it.
6. `cron/scheduler.py` still resolves a job's tools via
   `_get_platform_tools(cfg or {}, "cron")` (that literal), honours `wrap_response`, and treats
   `[SILENT]` as intentional silence. If cron tool resolution changes again,
   re-check that all 37 tools still reach the six jobs — they fail silently
   when they don't.
7. `agent/skill_bundles.py` reads `HERMES_HOME/skill-bundles/`, and
   `gateway/run.py` still dispatches bundles before its "Unknown command"
   notice. This is what makes `/log`, `/undo`, `/budget`, `/summary` work.
8. `hermes_cli/config_defaults.py`: the set of default-on
   auxiliary/background features is unchanged. The config disables
   `auxiliary.background_review`, `auxiliary.title_generation`, `curator`,
   `lsp` and `model_catalog`; a new one appears upstream and starts costing
   tokens or RAM with no change on your side.
9. `pyproject.toml` still has the `all`, `messaging` and `edge-tts` extras
   the Dockerfile installs, and `requires-python` still admits 3.11.
10. A top-level `skills/` directory still exists upstream (the Dockerfile
    prunes it; the gateway re-seeds from it on every start).

Everything in that list is a `grep`, and the workflow runs all ten (items 9
and 10 as one check each). The two patches are also *applied* to a
downloaded copy and the result compiled — see section 4.

## 4. The procedure

You need `git`, `curl`, Python 3.11 and the `gh` CLI (optional, for the
tag lookup).

**a. Find the newest release and its commit.**

```bash
gh api "repos/NousResearch/hermes-agent/releases?per_page=3" \
  --jq '.[] | "\(.tag_name)  \(.name)  \(.published_at)"'
gh api "repos/NousResearch/hermes-agent/commits/<tag>" --jq '.sha'
```

Or on GitHub: Releases → the tag → the commit hash under it. Read the
release notes. Words to search for: *silence*, *cron*, *toolset*,
*platform*, *slash command*, *bundle*, *auxiliary*, *background*,
*telegram*, *webhook*.

**b. Download the files the checks need.** Use the authenticated GitHub API
through `gh`; the anonymous `raw.githubusercontent.com` endpoint rate-limits
(HTTP 429) often enough to break the loop:

```bash
S=<sha>; R=repos/NousResearch/hermes-agent/contents
mkdir -p up/agent up/gateway up/cron up/hermes_cli up/tools
for f in agent/conversation_loop.py gateway/response_filters.py gateway/run.py \
         agent/skill_bundles.py cron/scheduler.py hermes_cli/config_defaults.py \
         pyproject.toml model_tools.py tools/registry.py; do
  gh api -H "Accept: application/vnd.github.raw" "$R/$f?ref=$S" > "up/$f"
done
```

Without `gh`, swap the download line for
`curl -fsSL "https://raw.githubusercontent.com/NousResearch/hermes-agent/$S/$f" -o "up/$f"`;
it works when you are not being rate-limited.

**c. Dry-run both patches against the downloaded loop.** They honour
`PEHD_PATCH_TARGET`, so nothing touches the repo:

```bash
cp up/agent/conversation_loop.py cl.py
PEHD_PATCH_TARGET=cl.py python deploy/patches/suppress_reply_on_silent_tools.py
PEHD_PATCH_TARGET=cl.py python deploy/patches/log_llm_usage.py
python -c "import py_compile; py_compile.compile('cl.py', doraise=True); print('compiles')"
grep -c "PEHD patch" cl.py        # expect 2
```

`FATAL: … anchor not found` or `… ambiguous` means section 5.

**d. Run the behaviour greps** (items 3–10). The quickest way is to trigger
the workflow with the SHA as its input (Actions → *Upstream anchor check* →
*Run workflow*) and read the summary; the equivalent shell lines are in the
workflow file.

**e. Bump the pin.** In the `Dockerfile`, change `ARG HERMES_AGENT_SHA=…`
to the full 40-character SHA and update the "Currently pinned" comment
above it (tag, date, previous SHA). Nothing else in the file references the
version.

**f. Update the two places that quote the pin:** the "hermes-agent SHA
bump" section of `AGENTS.md`, and `CHANGELOG.md` (one line: version, date,
"no anchor moved" or what did).

**g. Open a PR, let CI run, merge.** Render rebuilds on merge. If the build
fails it keeps the old container running — you lose nothing — and the log
names the step. If it succeeds, do the smoke checks in
[SETUP.md section 7](SETUP.md#7-verify-the-whole-thing-works): a manual
log gets exactly one bubble, a follow-up "thanks" gets a reply, and a cron
job you trigger by hand can call its tools.

**h. Cron prompts changed?** Only if you edited `cron/setup-cron-jobs.sh`.
Live jobs keep their old text: re-seed per [cron/README.md](../cron/README.md).

## 5. When an anchor drifts

The build (or the dry run) fails with `anchor not found`. Do **not** loosen
the anchor, and do not pick the nearest similar line and hope. Instead:

1. **Find the successor.** `grep -n` the downloaded file for a distinctive
   fragment of the old anchor. Upstream refactors usually move code
   intact — the 0.15 refactor moved the whole loop to a new file and
   changed `self.` to `agent.`, but every line was still there.
2. **Confirm it is the same place.** Read 40 lines around it. For the
   silence patch: is this still the point *after* tool results are in
   `messages` and *before* the model's follow-up is finalised, inside the
   main loop? For the usage patch: are `canonical_usage`, `prompt_tokens`,
   `completion_tokens`, `total_tokens`, `api_duration`, `api_call_count`
   and `cost_result` still bound there?
3. **Update `ANCHOR` (and `TARGET` if the file moved), then `INJECTION`** —
   every attribute and local it references must exist at the new site.
   Keep the exact-once check and the marker.
4. **Dry-run again** (4c) and confirm the `break` still exits the *main*
   loop, not an inner one. A quick way: in Python, parse the patched file
   with `ast`, find the injected `break`, and walk up to its enclosing
   `While`. The migration notes show this done.
5. **Ask whether the patch is still needed.** Three of the original five
   were retired because upstream implemented what they did. Check the
   release notes and the surrounding code before porting a patch forward —
   a deleted patch is the best outcome.
6. Record what you verified in the commit message.

If it is the **config** that drifted (a check in section 3 fails but both
patches apply), the fix is usually one key in `cli-config.yaml` — the
migration notes list the key for each behaviour.

## 6. Things that look like breakage and are not

- **`git clone` fails with HTTP 429** during the Docker build. GitHub
  rate-limited the build host. The Dockerfile already fetches only the
  pinned commit with three retries; just redeploy.
- **"SQLite 3.46.1 is vulnerable to the WAL-reset corruption bug… using
  journal_mode=DELETE"** at boot, five times. Hermes checked the base
  image's SQLite and chose the older, safe journal mode. Harmless; fix by
  upgrading SQLite in the image if the log noise bothers you.
- **Dozens of `check_fn … returned False; dependent tools will be
  unavailable`** at boot. Upstream's built-in tools (browser, Discord,
  Spotify…) whose requirements you have not met. None of them are in the
  toolsets this config enables. What matters is what is *absent*: no line
  about the expense tools, and no "Unknown toolset".
- **"skipping project-context discovery … fell back to the Hermes install
  tree"**. Hermes deliberately refusing to load its own contributor
  instructions into your prompt. Protective, not a problem.
- **"Gateway shutting down — your current task will be interrupted"** in
  Telegram after a restart. Only sent to sessions with a run in flight.

## 7. If you want to stop patching altogether

Both patches exist because hermes had no supported hook for the behaviour.
That is changing: 0.20 added a first-class silence token (which the patch
now uses), and 0.21 added a `post_api_request` plugin hook that receives
nearly the same record the usage patch writes. When a release lets you
express either behaviour through config or a plugin, delete the patch, its
`COPY`/`RUN`/`grep` lines in the Dockerfile, and its row in the anchor-check
workflow. Fewer anchors, fewer things to break next time.

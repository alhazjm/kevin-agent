"""Patch hermes-agent's agent loop to exit cleanly when the most-recent tool
result included `"assistant_reply_required": false`.

Why: our log_expense tool side-channels its own Telegram confirmation bubble
via the Bot API. If the LLM then produces any final assistant reply, the
gateway sends it too — a duplicate. Prompt-level silence instructions are
not a reliable runtime contract.

History of this patch:

v1 zeroed `final_response` at the no-tool-calls branch. Necessary
but not sufficient: the nudge/retry cascade ran BEFORE the gateway's
`if response:` gate, so zeroing alone produced ~6 user-visible warnings
plus a fallback apology per expense.

v2 added a `break` out of the loop at the same anchor, still
setting `final_response = ""`.

v3 (2026-09-01, hermes-agent 0.10 -> 0.20.6) — THREE changes, each forced
by a verified upstream change at SHA 5fc308a7 (tag v2026.8.27):

  1. NEW TARGET. The whole conversation loop was extracted out of
     run_agent.py into `agent/conversation_loop.py` as a module-level
     function `run_conversation(agent, ...)` (v0.15.0, "The Velocity
     Release"). There is no `self` in that scope any more — every
     receiver is the `agent` parameter. The v2 anchor matches ZERO times
     in both files, so v2 fails the build loud, exactly as designed.

  2. EMIT A MARKER, NOT "". This is the important one. Upstream added
     `gateway/response_filters.py`, whose contract is explicit:
     "A blank response is also not silence; blank output is handled by
     the empty-response failure path." An empty `final_response` is now
     rewritten to a delivered "⚠️ Processing completed but no response
     was generated" bubble by `_normalize_empty_agent_response`
     (gateway/run.py) — and the FIRST of its two call sites runs inside
     `TurnRunner.run_sync`, BEFORE any silence predicate is consulted.
     Our ingest route is `deliver: telegram`, so v2's `""` would have put
     one junk warning bubble in the user's Telegram chat per logged expense: the exact
     M9 symptom this patch exists to prevent. We therefore emit
     `NO_REPLY` — the value of `response_filters.SILENT_REPLY_TOKEN` and
     a member of `LIVE_GATEWAY_SILENT_MARKERS`, so it is swallowed by the
     strict matcher (telegram lane), the loose matcher (webhook adapter)
     and the cron lane alike.

  3. SCAN ONLY THIS TURN. v1/v2 scanned `messages` backwards without a
     bound, but `messages` is the FULL conversation — this turn's rows
     appended onto replayed history, and history carries role="tool"
     rows. So on any later turn where the model answers with no tool
     calls ("thanks", a question answered from context) the scan would
     walk past this turn's user row and hit the PREVIOUS turn's
     log_expense result, which still carries the silence flag. Under v2's
     `""` that mis-hit was a wrong-but-visible outcome; under a real
     silence marker it would drop the user's turn in total silence. The
     scan is now bounded at `current_turn_user_idx` (a live local of
     run_conversation, re-anchored after mid-turn compression before this
     anchor is reached). Upstream fixed the identical bug shape for
     MEDIA: tags the same way — see gateway/run.py "Scope the scan to
     THIS turn's tool results only ... (Fixes #34608)".

Placement (decided 2026-09-01): the injection goes BEFORE the
`codex_responses` / `finish_reason == "incomplete"` continuation block
rather than at the no-tool-calls branch ~780 lines below it. Our
deployment runs that leg — provider "custom" against api.openai.com is
auto-upgraded to `api_mode = "codex_responses"` — and a reasoning-only
follow-up after a silent tool otherwise burns up to 3 continuation API
calls before it ever reaches the no-tool-calls branch. That saving is why
the old `suppress_codex_incomplete_after_silent_tools.py` patch existed;
exiting here makes it unnecessary and it was deleted in the same commit.

The trade-off, stated plainly: `finish_reason == "incomplete"` with an
empty tool_calls list means the response was TRUNCATED, so we abort a
turn upstream would have retried up to 3x. That is what we want here —
the tool has already written the ledger row and sent the bubble, and
every remaining token is prose we would suppress anyway. The
`not assistant_message.tool_calls` guard is what keeps a turn that still
has pending tool calls alive.

The scan is additionally skipped whenever `current_turn_user_idx` is
negative — that is upstream's "no user-originated message survived
compaction" sentinel, and clamping it to zero would silently reinstate
the unbounded scan. See the comment at the guard.

Applied at Docker build time. The anchor is narrow and unique (verified
count == 1 at the pinned SHA). If upstream refactors it the script exits
non-zero and fails the Docker build — loud failure, never silent
breakage on Render. When that happens, bump HERMES_AGENT_SHA
deliberately, re-inspect agent/conversation_loop.py for the new anchor,
and update this script. See AGENTS.md "hermes-agent SHA bump checklist".

Local sanity run: set PEHD_PATCH_TARGET=/path/to/conversation_loop.py to
patch a downloaded copy instead of the container path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TARGET = Path(
    os.environ.get("PEHD_PATCH_TARGET")
    or "/app/hermes-agent/agent/conversation_loop.py"
)

# Anchor: the head of the codex_responses "incomplete" continuation block.
# 12-space indent, inside the main `while` of run_conversation(). Verified
# exactly once at HERMES_AGENT_SHA=5fc308a7, re-verified at 29112bef / v0.21.0 (0 matches in run_agent.py).
ANCHOR = '            if agent.api_mode == "codex_responses" and finish_reason == "incomplete":'

# Injection: prepended BEFORE the anchor (see the placement note above), so
# the silent turn never enters the continuation loop.
#
# `_turn_exit_reason` is deliberately prefixed "text_response(" — upstream's
# turn-completion explainer (agent/turn_finalizer.py) early-returns for any
# reason starting with that prefix, which keeps it from appending a
# "⚠️ No reply: ..." suffix onto our marker and breaking the exact-match
# silence test.
#
# `agent._mute_post_response = False` mirrors what upstream does one line
# after the no-tool-calls branch we would otherwise have fallen through to;
# we set it ourselves because we exit before reaching it.
INJECTION = '''            # PEHD patch v3: exit agent loop when tool signalled silence
            # (see deploy/patches/suppress_reply_on_silent_tools.py in
            # kevin-agent). Placed BEFORE the codex
            # continuation block so a reasoning-only follow-up after a silent
            # tool does not burn up to 3 extra API calls. The scan is bounded
            # to THIS turn — an unbounded scan would hit a previous turn's
            # log_expense result and silently drop a later user message.
            # A negative index is reanchor_current_turn_user_idx()'s "no
            # user-originated message survived" sentinel. Clamping it to 0
            # would restore the very unbounded scan this bound exists to
            # prevent, so we skip the silence exit instead and let the turn
            # reply normally. Failing this way round is deliberate: a missed
            # silence is a duplicate bubble (visible, diagnosable), a false
            # silence is a user message dropped without a trace.
            if not assistant_message.tool_calls and isinstance(current_turn_user_idx, int) and current_turn_user_idx >= 0:
                _pehd_last_tool = None
                for _pehd_msg in reversed(messages[current_turn_user_idx:]):
                    if isinstance(_pehd_msg, dict) and _pehd_msg.get("role") == "tool":
                        _pehd_last_tool = _pehd_msg
                        break
                if _pehd_last_tool is not None:
                    _pehd_content = _pehd_last_tool.get("content", "") or ""
                    if isinstance(_pehd_content, str) and '"assistant_reply_required": false' in _pehd_content:
                        _turn_exit_reason = "text_response(tool_requested_no_reply)"
                        agent._mute_post_response = False
                        # Close the durable turn ourselves, with the model's
                        # OWN words. If we leave the tail as this turn's
                        # role="tool" row, upstream's #43849/#44100 chokepoint
                        # in agent/turn_finalizer.finalize_turn appends
                        # {"role": "assistant", "content": final_response} and
                        # persists it — writing the literal control token
                        # NO_REPLY into session history on every silent log.
                        # Nothing upstream ever teaches the model that token,
                        # so those rows would replay as few-shot examples of a
                        # string the gateway swallows without a trace: exactly
                        # the silent-drop failure this patch's turn bound
                        # exists to prevent. (v2's "" was falsy and never hit
                        # that branch, so this is new at v3.) An assistant tail
                        # here carries content and no tool_calls, so the
                        # _is_pure_tool_call_tail refill cannot fire either.
                        _pehd_tail = messages[-1] if messages else None
                        if not (isinstance(_pehd_tail, dict) and _pehd_tail.get("role") == "assistant"):
                            append_message(messages, {"role": "assistant", "content": assistant_message.content or ""})
                        final_response = "NO_REPLY"
                        agent._response_was_previewed = True
                        break

'''

# Stable marker grepped by the Dockerfile after patch runs.
MARKER = "PEHD patch v3: exit agent loop when tool signalled silence"


def main() -> int:
    if not TARGET.exists():
        print(f"FATAL: {TARGET} not found", file=sys.stderr)
        return 2

    src = TARGET.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"PEHD patch: already applied to {TARGET} (marker present) — skipping")
        return 0

    if ANCHOR not in src:
        print(
            "FATAL: PEHD patch v3 anchor not found in agent/conversation_loop.py. "
            "Upstream hermes-agent likely refactored the codex_responses "
            "'incomplete' continuation block. "
            "Bump HERMES_AGENT_SHA deliberately, re-inspect "
            "agent/conversation_loop.py for the new anchor, and update "
            "ANCHOR + INJECTION in this script. "
            "See AGENTS.md 'hermes-agent SHA bump checklist'.",
            file=sys.stderr,
        )
        return 3

    if src.count(ANCHOR) != 1:
        print(
            f"FATAL: PEHD patch v3 anchor is ambiguous "
            f"({src.count(ANCHOR)} matches) — refusing to patch. "
            "Tighten ANCHOR in this script.",
            file=sys.stderr,
        )
        return 4

    patched = src.replace(ANCHOR, INJECTION + ANCHOR, 1)
    TARGET.write_text(patched, encoding="utf-8")
    print(f"PEHD patch: applied v3 silence exit to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

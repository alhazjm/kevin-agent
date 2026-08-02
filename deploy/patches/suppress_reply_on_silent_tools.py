"""Patch hermes-agent's run_agent.py to exit the agent loop cleanly when the
most-recent tool result included `"assistant_reply_required": false`.

Why: our log_expense tool side-channels its own Telegram confirmation bubble
via the Bot API. If the LLM then produces any final assistant reply, the
gateway sends it too — a duplicate. Prompt-level silence instructions are
not a reliable runtime contract.

v1 (PR #16) tried zeroing `final_response` at this same anchor. That was
necessary but not sufficient: the nudge/retry cascade at run_agent.py:11514+
runs BEFORE the gateway's `if response:` gate, so zeroing alone produced
~6 user-visible warnings plus a fallback apology per expense. See the PR #17
body for the screenshot post-mortem.

v2 approach: after tool results are appended and the follow-up turn returns
empty, detect the silence flag and `break` out of the retry loop before the
nudge cascade fires. The break is modelled on the existing partial-stream
recovery break ~25 lines below this anchor — it sets `final_response`,
`_turn_exit_reason`, `self._response_was_previewed`, and breaks. Using the
same exit convention means we know this path is supported at this indent.

Applied at Docker build time. The anchor is narrow and unique. If upstream
refactors it the script exits non-zero and fails the Docker build — loud
failure, never silent breakage on Render. When that happens, bump
HERMES_AGENT_SHA deliberately, re-inspect run_agent.py for the new anchor,
and update this script. See CLAUDE.md "hermes-agent SHA bump checklist".
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/hermes-agent/run_agent.py")

# Anchor: the exact line where the agent loop assigns `final_response` in
# the no-tool-calls branch. Reached on the iteration AFTER tool calls have
# executed, so `messages` already contains the tool result by the time we
# inspect it here.
ANCHOR = '                    final_response = assistant_message.content or ""'

# Injection: scans messages backwards for the most-recent tool result. If
# it carries our silence marker, sets final_response to "" and breaks out
# of the enclosing retry loop using the same convention as the existing
# partial-stream-recovery break at run_agent.py:~11461. Exiting here skips
# the nudge-model / thinking-prefill / empty-retry cascade at 11514+, and
# the gateway's `if response:` gate then skips the send.
INJECTION = """
                    # PEHD patch v2: exit agent loop when tool signalled silence
                    # (see deploy/patches/suppress_reply_on_silent_tools.py
                    # in this repo). Replaces the v1 zero-out
                    # which didn't prevent the nudge/retry cascade below.
                    _pehd_last_tool = None
                    for _pehd_msg in reversed(messages):
                        if isinstance(_pehd_msg, dict) and _pehd_msg.get("role") == "tool":
                            _pehd_last_tool = _pehd_msg
                            break
                    if _pehd_last_tool is not None:
                        _pehd_content = _pehd_last_tool.get("content", "") or ""
                        if isinstance(_pehd_content, str) and '"assistant_reply_required": false' in _pehd_content:
                            _turn_exit_reason = "tool_requested_no_reply"
                            final_response = ""
                            self._response_was_previewed = True
                            break"""

# Stable marker grepped by the Dockerfile after patch runs.
MARKER = "PEHD patch v2: exit agent loop when tool signalled silence"


def main() -> int:
    if not TARGET.exists():
        print(f"FATAL: {TARGET} not found", file=sys.stderr)
        return 2

    src = TARGET.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"PEHD patch: already applied to {TARGET} (marker present) \u2014 skipping")
        return 0

    if ANCHOR not in src:
        print(
            "FATAL: PEHD patch anchor not found in run_agent.py. "
            "Upstream hermes-agent likely refactored the final_response "
            "assignment in the no-tool-calls branch. "
            "Bump HERMES_AGENT_SHA deliberately, re-inspect run_agent.py "
            "for the new anchor, and update ANCHOR + INJECTION in this script. "
            "See CLAUDE.md 'hermes-agent SHA bump checklist'.",
            file=sys.stderr,
        )
        return 3

    if src.count(ANCHOR) != 1:
        print(
            f"FATAL: PEHD patch anchor is ambiguous "
            f"({src.count(ANCHOR)} matches) \u2014 refusing to patch. "
            "Tighten ANCHOR in this script.",
            file=sys.stderr,
        )
        return 4

    patched = src.replace(ANCHOR, ANCHOR + INJECTION, 1)
    TARGET.write_text(patched, encoding="utf-8")
    print(f"PEHD patch: applied to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

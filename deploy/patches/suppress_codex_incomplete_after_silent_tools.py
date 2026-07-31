"""Suppress Codex continuation failures after silent tool-delivered replies.

Hermes' direct OpenAI path uses the Responses/Codex runtime for newer OpenAI
models. After a tool such as log_expense sends its own Telegram confirmation
and returns `"assistant_reply_required": false`, the model can still enter the
Codex continuation path while trying to produce a final assistant message. If
that continuation remains incomplete after 3 attempts, Hermes returns an error
string that the Telegram gateway sends to the user.

For PEHD, that is noise when the latest tool already delivered the real user
message. This patch converts only that narrow case into a clean silent turn.
Real Codex incomplete failures without a recent silent tool still surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/hermes-agent/run_agent.py")

ANCHOR = '''                    self._codex_incomplete_retries = 0
                    self._persist_session(messages, conversation_history)
                    return {
                        "final_response": None,
                        "messages": messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Codex response remained incomplete after 3 continuation attempts",
                    }'''

INJECTION = '''                    # PEHD patch: suppress Codex incomplete after silent tool delivery.
                    _pehd_suppress_codex_incomplete = False
                    for _pehd_msg in reversed(messages):
                        if not isinstance(_pehd_msg, dict):
                            continue
                        _pehd_role = _pehd_msg.get("role")
                        if _pehd_role == "tool":
                            _pehd_content = _pehd_msg.get("content", "") or ""
                            if isinstance(_pehd_content, str) and '"assistant_reply_required": false' in _pehd_content:
                                _pehd_suppress_codex_incomplete = True
                            break
                        if _pehd_role == "user":
                            _pehd_user_content = _pehd_msg.get("content", "") or ""
                            if isinstance(_pehd_user_content, str) and _pehd_user_content.strip().startswith("[System:"):
                                continue
                            break
                    if _pehd_suppress_codex_incomplete:
                        self._codex_incomplete_retries = 0
                        _turn_exit_reason = "tool_requested_no_reply_after_codex_incomplete"
                        final_response = ""
                        self._response_was_previewed = True
                        break

                    self._codex_incomplete_retries = 0
                    self._persist_session(messages, conversation_history)
                    return {
                        "final_response": None,
                        "messages": messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Codex response remained incomplete after 3 continuation attempts",
                    }'''

MARKER = "PEHD patch: suppress Codex incomplete after silent tool delivery"


def main() -> int:
    if not TARGET.exists():
        print(f"FATAL: {TARGET} not found", file=sys.stderr)
        return 2

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"PEHD patch: already applied to {TARGET} (Codex incomplete marker present)")
        return 0

    if ANCHOR not in src:
        print(
            "FATAL: Codex incomplete patch anchor not found in run_agent.py. "
            "Upstream continuation handling likely moved; re-inspect the "
            "`Codex response remained incomplete after 3 continuation attempts` "
            "branch and update this patch.",
            file=sys.stderr,
        )
        return 3

    if src.count(ANCHOR) != 1:
        print(
            f"FATAL: Codex incomplete patch anchor ambiguous ({src.count(ANCHOR)} matches)",
            file=sys.stderr,
        )
        return 4

    TARGET.write_text(src.replace(ANCHOR, INJECTION, 1), encoding="utf-8")
    print(f"PEHD patch: applied Codex incomplete suppression to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

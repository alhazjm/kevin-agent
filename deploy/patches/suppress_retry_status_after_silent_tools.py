"""Suppress user-visible retry statuses after silent tool-delivered replies.

Hermes emits status messages like "Retrying in 2.5s..." while retrying model
calls. For PEHD, tools such as log_expense/log_expense_pending already deliver
their own Telegram bubble and return `"assistant_reply_required": false`.
If the follow-up model call hits a transient provider error, retry statuses
are noise: the user already got the real message.

This patch keeps the retry/recovery mechanics intact, but skips the platform
status send when the current trailing tool batch requested silence.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/hermes-agent/run_agent.py")

ANCHOR = '''                    if is_rate_limited:
                        self._emit_status(f"\u23f1\ufe0f Rate limited. Waiting {wait_time:.1f}s (attempt {retry_count + 1}/{max_retries})...")
                    else:
                        self._emit_status(f"\u23f3 Retrying in {wait_time:.1f}s (attempt {retry_count}/{max_retries})...")'''

INJECTION = '''                    # PEHD patch: suppress retry status after silent tool delivery.
                    _pehd_suppress_retry_status = False
                    for _pehd_msg in reversed(messages):
                        if not isinstance(_pehd_msg, dict):
                            continue
                        _pehd_role = _pehd_msg.get("role")
                        if _pehd_role == "tool":
                            _pehd_content = _pehd_msg.get("content", "") or ""
                            if isinstance(_pehd_content, str) and '"assistant_reply_required": false' in _pehd_content:
                                _pehd_suppress_retry_status = True
                            break
                        if _pehd_role in {"user", "assistant"}:
                            break
                    if not _pehd_suppress_retry_status:
                        if is_rate_limited:
                            self._emit_status(f"\u23f1\ufe0f Rate limited. Waiting {wait_time:.1f}s (attempt {retry_count + 1}/{max_retries})...")
                        else:
                            self._emit_status(f"\u23f3 Retrying in {wait_time:.1f}s (attempt {retry_count}/{max_retries})...")'''

MARKER = "PEHD patch: suppress retry status after silent tool delivery"


def main() -> int:
    if not TARGET.exists():
        print(f"FATAL: {TARGET} not found", file=sys.stderr)
        return 2

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"PEHD patch: already applied to {TARGET} (retry-status marker present)")
        return 0

    if ANCHOR not in src:
        print(
            "FATAL: retry-status patch anchor not found in run_agent.py. "
            "Upstream retry status emission likely moved; re-inspect the "
            "`Retrying in` block and update this patch.",
            file=sys.stderr,
        )
        return 3

    if src.count(ANCHOR) != 1:
        print(
            f"FATAL: retry-status patch anchor ambiguous ({src.count(ANCHOR)} matches)",
            file=sys.stderr,
        )
        return 4

    TARGET.write_text(src.replace(ANCHOR, INJECTION, 1), encoding="utf-8")
    print(f"PEHD patch: applied retry-status suppression to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

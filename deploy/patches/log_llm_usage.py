"""Persist per-request LLM usage to /data for cost observation.

Hermes already normalizes provider usage and estimates cost in memory/session
DB. This patch adds a simple JSONL sidecar on the Render persistent disk so the
deployment can answer "what did webhooks vs Telegram vs cron actually cost?"
without depending on Hermes internals or the OpenAI dashboard UI.

Re-anchored 2026-09-01 for hermes-agent 0.20.6 (SHA 5fc308a7). Three things
changed and each one is load-bearing:

  1. The accounting block moved out of run_agent.py into
     `agent/conversation_loop.py` when the loop became the module-level
     function `run_conversation(agent, ...)`. Same shape, same locals,
     indent 24 -> 20, and every `self.` became `agent.`.

  2. THE INJECTION NOW IMPORTS ITS OWN `datetime` AND `Path`.
     `conversation_loop.py` imports `json` and `os` at module level but
     NOT `datetime` or `pathlib.Path` (verified by grep at the pinned
     SHA). Because the whole record write is wrapped in a bare
     `except Exception: pass`, a NameError there would fail SILENTLY:
     the build greps green, the marker is present, and
     /data/llm_usage stays empty forever. That is the single most
     dangerous failure mode of this patch — hence the local imports and
     the acceptance test below.

  3. ONE LEG, NOT TWO. `agent/codex_runtime.py` carries a similar
     assignment, but it is the `codex_app_server` leg. Our deployment
     resolves to `api_mode = "codex_responses"` (provider "custom"
     against api.openai.com is auto-upgraded), which flows through
     conversation_loop.py. A second anchor would double the SHA-bump
     maintenance surface for a leg we never execute.

Record keys are deliberately unchanged across the migration so
scripts/summarize_llm_usage.py and the pre/post-migration cost tables stay
directly comparable.

ACCEPTANCE TEST after deploy (do not skip — see failure mode 2): after one
webhook ingest plus one Telegram turn,
`wc -l /data/llm_usage/usage-$(date +%Y-%m).jsonl` must be > 0. Zero rows
with a green build means the local imports were dropped.

Note on `input_tokens` at this SHA: upstream's codex_responses branch
defines `input_tokens` as UNCACHED input (total minus cache_read minus
cache_write), with `prompt_tokens` as the property summing all three. The
record carries both, so the summariser can tell caching apart from real
input growth.

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

# 20-space indent, `agent.` receiver. Verified exactly once at
# HERMES_AGENT_SHA=5fc308a7 (0 matches in run_agent.py).
ANCHOR = '''                    agent.session_cost_status = cost_result.status
                    agent.session_cost_source = cost_result.source

                    # Persist token counts to session DB for /insights.'''

INJECTION = '''                    agent.session_cost_status = cost_result.status
                    agent.session_cost_source = cost_result.source

                    # PEHD patch: persist per-call LLM usage to /data.
                    # datetime/Path are imported locally on purpose: this
                    # module has json + os at module level but neither of
                    # these, and the except-pass below would swallow the
                    # resulting NameError into an empty usage log.
                    try:
                        from datetime import datetime as _pehd_dt
                        from pathlib import Path as _pehd_Path
                        _pehd_usage_dir = _pehd_Path(os.environ.get("PEHD_LLM_USAGE_DIR", "/data/llm_usage"))
                        _pehd_usage_dir.mkdir(parents=True, exist_ok=True)
                        _pehd_model_key = (agent.model or "").split("/")[-1].lower()
                        _pehd_prices = {
                            "gpt-5.5": (5.00, 0.50, 5.00, 30.00),
                            "gpt-5.4": (2.50, 0.25, 2.50, 15.00),
                            "gpt-5.4-nano": (0.20, 0.02, 0.20, 1.25),
                            "gpt-5-nano": (0.05, 0.005, 0.05, 0.40),
                        }
                        _pehd_cost = (
                            float(cost_result.amount_usd)
                            if cost_result.amount_usd is not None else None
                        )
                        _pehd_cost_status = str(cost_result.status)
                        if _pehd_cost is None and _pehd_model_key in _pehd_prices:
                            _in_rate, _cache_read_rate, _cache_write_rate, _out_rate = _pehd_prices[_pehd_model_key]
                            _pehd_cost = (
                                canonical_usage.input_tokens * _in_rate
                                + canonical_usage.cache_read_tokens * _cache_read_rate
                                + canonical_usage.cache_write_tokens * _cache_write_rate
                                + canonical_usage.output_tokens * _out_rate
                            ) / 1_000_000
                            _pehd_cost_status = "pehd_estimated"
                        _pehd_record = {
                            "timestamp": _pehd_dt.now().isoformat(timespec="seconds"),
                            "session_id": agent.session_id,
                            "platform": agent.platform or "",
                            "gateway_session_key": getattr(agent, "_gateway_session_key", "") or "",
                            "provider": agent.provider or "",
                            "model": agent.model or "",
                            "api_mode": agent.api_mode or "",
                            "base_url": agent.base_url or "",
                            "api_call_count": api_call_count,
                            "duration_seconds": round(float(api_duration), 3),
                            "input_tokens": canonical_usage.input_tokens,
                            "output_tokens": canonical_usage.output_tokens,
                            "cache_read_tokens": canonical_usage.cache_read_tokens,
                            "cache_write_tokens": canonical_usage.cache_write_tokens,
                            "reasoning_tokens": canonical_usage.reasoning_tokens,
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens,
                            "estimated_cost_usd": _pehd_cost,
                            "cost_status": _pehd_cost_status,
                            "cost_source": str(cost_result.source),
                        }
                        _pehd_file = _pehd_usage_dir / f"usage-{_pehd_dt.now().strftime('%Y-%m')}.jsonl"
                        with _pehd_file.open("a", encoding="utf-8") as _pehd_f:
                            _pehd_f.write(json.dumps(_pehd_record, ensure_ascii=False) + "\\n")
                    except Exception:
                        pass

                    # Persist token counts to session DB for /insights.'''

MARKER = "PEHD patch: persist per-call LLM usage to /data"


def main() -> int:
    if not TARGET.exists():
        print(f"FATAL: {TARGET} not found", file=sys.stderr)
        return 2

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"PEHD patch: already applied to {TARGET} (usage marker present)")
        return 0

    if ANCHOR not in src:
        print(
            "FATAL: LLM usage patch anchor not found in "
            "agent/conversation_loop.py. Upstream usage accounting likely "
            "moved; re-inspect the session_cost_status/session_cost_source "
            "block. See CLAUDE.md 'hermes-agent SHA bump checklist'.",
            file=sys.stderr,
        )
        return 3

    if src.count(ANCHOR) != 1:
        print(
            f"FATAL: LLM usage patch anchor ambiguous ({src.count(ANCHOR)} matches)",
            file=sys.stderr,
        )
        return 4

    TARGET.write_text(src.replace(ANCHOR, INJECTION, 1), encoding="utf-8")
    print(f"PEHD patch: applied LLM usage logging to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

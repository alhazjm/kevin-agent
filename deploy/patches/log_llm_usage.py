"""Persist per-request LLM usage to /data for cost observation.

Hermes already normalizes provider usage and estimates cost in memory/session
DB. This patch adds a simple JSONL sidecar on the Render persistent disk so the
deployment can answer "what did webhooks vs Telegram vs cron actually cost?"
without depending on Hermes internals or the OpenAI dashboard UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/app/hermes-agent/run_agent.py")

ANCHOR = '''                        self.session_cost_status = cost_result.status
                        self.session_cost_source = cost_result.source

                        # Persist token counts to session DB for /insights.'''

INJECTION = '''                        self.session_cost_status = cost_result.status
                        self.session_cost_source = cost_result.source

                        # PEHD patch: persist per-call LLM usage to /data.
                        try:
                            _pehd_usage_dir = Path(os.environ.get("PEHD_LLM_USAGE_DIR", "/data/llm_usage"))
                            _pehd_usage_dir.mkdir(parents=True, exist_ok=True)
                            _pehd_model_key = (self.model or "").split("/")[-1].lower()
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
                                "timestamp": datetime.now().isoformat(timespec="seconds"),
                                "session_id": self.session_id,
                                "platform": self.platform or "",
                                "gateway_session_key": getattr(self, "_gateway_session_key", "") or "",
                                "provider": self.provider or "",
                                "model": self.model or "",
                                "api_mode": self.api_mode or "",
                                "base_url": self.base_url or "",
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
                            _pehd_file = _pehd_usage_dir / f"usage-{datetime.now().strftime('%Y-%m')}.jsonl"
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
            "FATAL: LLM usage patch anchor not found in run_agent.py. "
            "Upstream usage accounting likely moved; re-inspect the "
            "session_cost_status/session_cost_source block.",
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

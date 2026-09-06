#!/usr/bin/env python3
"""Summarize Kevin LLM usage JSONL files.

Usage on Render:
  python scripts/summarize_llm_usage.py /data/llm_usage/usage-2026-05.jsonl
  python scripts/summarize_llm_usage.py /data/llm_usage/*.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _money(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def _tokens(value: int) -> str:
    return f"{value:,}"


def _iter_records(patterns: list[str]):
    for pattern in patterns:
        paths = sorted(glob.glob(pattern))
        if not paths and Path(pattern).exists():
            paths = [pattern]
        for path in paths:
            with open(path, "r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(f"Skipping malformed JSON at {path}:{line_no}: {exc}")
                        continue
                    record["_source_file"] = path
                    yield record


def _bucket_key(record: dict, field: str) -> str:
    value = str(record.get(field) or "").strip()
    return value or "(unknown)"


def _add(bucket: dict, record: dict) -> None:
    bucket["calls"] += 1
    bucket["input"] += int(record.get("input_tokens") or 0)
    bucket["cache_read"] += int(record.get("cache_read_tokens") or 0)
    bucket["cache_write"] += int(record.get("cache_write_tokens") or 0)
    bucket["output"] += int(record.get("output_tokens") or 0)
    bucket["reasoning"] += int(record.get("reasoning_tokens") or 0)
    bucket["total"] += int(record.get("total_tokens") or 0)
    cost = record.get("estimated_cost_usd")
    if isinstance(cost, (int, float)):
        bucket["cost"] += float(cost)


def _print_table(title: str, rows: list[tuple[str, dict]], limit: int | None = None) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    if limit:
        rows = rows[:limit]
    for key, data in rows:
        avg = data["cost"] / data["calls"] if data["calls"] else 0.0
        print(
            f"{key}: calls={data['calls']:,} cost={_money(data['cost'])} "
            f"avg={_money(avg)} input={_tokens(data['input'])} "
            f"cache={_tokens(data['cache_read'])} output={_tokens(data['output'])}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Kevin LLM usage JSONL.")
    parser.add_argument("paths", nargs="+", help="Usage JSONL path(s), glob patterns allowed.")
    args = parser.parse_args()

    totals = defaultdict(float)
    by_model: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_platform: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    by_session_prefix: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    days: set[str] = set()
    records = []

    for record in _iter_records(args.paths):
        records.append(record)
        _add(totals, record)
        _add(by_model[_bucket_key(record, "model")], record)
        _add(by_platform[_bucket_key(record, "platform")], record)
        session_id = str(record.get("session_id") or "")
        prefix = "cron" if session_id.startswith("cron_") else _bucket_key(record, "platform")
        _add(by_session_prefix[prefix], record)
        timestamp = str(record.get("timestamp") or "")
        if len(timestamp) >= 10:
            days.add(timestamp[:10])

    if not records:
        print("No usage records found.")
        return 1

    day_count = max(1, len(days))
    daily_cost = totals["cost"] / day_count
    projected_month = daily_cost * 30
    cache_total = totals["input"] + totals["cache_read"] + totals["cache_write"]
    cache_pct = (totals["cache_read"] / cache_total * 100) if cache_total else 0.0

    print("Kevin LLM usage Summary")
    print("======================")
    print(f"Records: {len(records):,}")
    print(f"Days observed: {day_count}")
    print(f"Total cost: {_money(totals['cost'])}")
    print(f"Projected 30-day cost: {_money(projected_month)}")
    print(f"Calls: {int(totals['calls']):,}")
    print(f"Input tokens: {_tokens(int(totals['input']))}")
    print(f"Cached input tokens: {_tokens(int(totals['cache_read']))} ({cache_pct:.1f}% of input)")
    print(f"Output tokens: {_tokens(int(totals['output']))}")
    print(f"Reasoning tokens: {_tokens(int(totals['reasoning']))}")

    model_rows = sorted(by_model.items(), key=lambda item: item[1]["cost"], reverse=True)
    platform_rows = sorted(by_platform.items(), key=lambda item: item[1]["cost"], reverse=True)
    prefix_rows = sorted(by_session_prefix.items(), key=lambda item: item[1]["cost"], reverse=True)

    _print_table("By Model", model_rows)
    _print_table("By Platform", platform_rows)
    _print_table("By Session Type", prefix_rows)

    expensive = sorted(
        records,
        key=lambda r: float(r.get("estimated_cost_usd") or 0),
        reverse=True,
    )
    print("\nTop Expensive Calls")
    print("-------------------")
    for record in expensive[:10]:
        ts = record.get("timestamp") or ""
        model = record.get("model") or ""
        platform = record.get("platform") or ""
        cost = float(record.get("estimated_cost_usd") or 0)
        total = int(record.get("total_tokens") or 0)
        session = str(record.get("session_id") or "")[:32]
        print(f"{ts} {platform} {model} cost={_money(cost)} total={_tokens(total)} session={session}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

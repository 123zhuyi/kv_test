#!/usr/bin/env python3
"""Poll relevant Prometheus metrics during a cancellation experiment."""

from __future__ import annotations

import argparse
import csv
import re
import time
import urllib.request
from pathlib import Path


SAMPLE = re.compile(r"^([^\s{]+)(\{[^}]*\})?\s+([-+0-9.eE]+)$")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000/metrics")
    p.add_argument("--interval-ms", type=float, default=100)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--output", type=Path, default=Path("results/metrics.csv"))
    p.add_argument(
        "--filter",
        default=r"running|waiting|queue|kv_cache|generation_tokens|prompt_tokens|abort|cancel|disconnect",
    )
    args = p.parse_args()
    wanted = re.compile(args.filter, re.I)
    rows: list[dict[str, object]] = []
    start = time.perf_counter()
    next_sample = start
    while time.perf_counter() - start < args.duration:
        wall_ns = time.time_ns()
        elapsed = time.perf_counter() - start
        try:
            text = urllib.request.urlopen(args.url, timeout=2).read().decode("utf-8", "replace")
            for line in text.splitlines():
                if not line or line.startswith("#"):
                    continue
                match = SAMPLE.match(line)
                if match and wanted.search(match.group(1)):
                    rows.append(
                        {
                            "wall_time_ns": wall_ns,
                            "elapsed_s": elapsed,
                            "name": match.group(1),
                            "labels": match.group(2) or "",
                            "value": match.group(3),
                            "scrape_error": "",
                        }
                    )
        except Exception as exc:
            rows.append(
                {
                    "wall_time_ns": wall_ns,
                    "elapsed_s": elapsed,
                    "name": "",
                    "labels": "",
                    "value": "",
                    "scrape_error": f"{type(exc).__name__}: {exc}",
                }
            )
        next_sample += args.interval_ms / 1000
        time.sleep(max(0.0, next_sample - time.perf_counter()))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["wall_time_ns", "elapsed_s", "name", "labels", "value", "scrape_error"]
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} metric samples to {args.output}")


if __name__ == "__main__":
    main()

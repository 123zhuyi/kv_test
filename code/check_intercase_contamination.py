#!/usr/bin/env python3
"""Inter-case orphan contamination check (review point 4).

For each surface cell: map every trace event to its case via the run_id
embedded in the audit id (probe run ids are unique per case), then check
whether a case's KV_RELEASED / scheduler-iteration events spill past the
NEXT case's start (capacity_summary started_wall).

If orphans from case k are still holding KV when case k+1 starts, the
measured harm in case k+1 is partly inherited rather than self-generated.

Output: results/intercase_contamination.csv + console summary.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tarfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

FREEZE = Path(__file__).resolve().parent.parent / "kv_test" / "freeze_2026-09-13"
OUT = Path(__file__).resolve().parent / "results" / "intercase_contamination.csv"

TS_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


def parse_ts(s: str) -> float:
    # trace ts_wall like "2026-09-13T15:24:01.123456+00:00"
    return datetime.fromisoformat(s).timestamp()


def main() -> None:
    rows_out = []
    with tarfile.open(FREEZE / "freeze_surface8b.tar.gz") as tf:
        names = set(tf.getnames())
        cells = sorted({n.split("/")[1] for n in names if n.startswith("surface8b/") and n.count("/") >= 2})
        for cell in cells:
            summary_name = f"surface8b/{cell}/sglang/sglang/capacity_summary.csv"
            trace_name = f"surface8b/{cell}/sglang/runtime_trace.jsonl"
            if summary_name not in names or trace_name not in names:
                continue
            summary = list(csv.DictReader(io.StringIO(tf.extractfile(summary_name).read().decode())))
            main_cases = [r for r in summary if r["case"].startswith("main_")]
            main_cases.sort(key=lambda r: r["started_wall"])
            starts = {r["case"]: parse_ts(r["started_wall"].replace("Z", "+00:00")) for r in main_cases}

            # run_id -> case, from per-case request CSVs
            run2case = {}
            for r in main_cases:
                req_name = f"surface8b/{cell}/sglang/sglang/{r['case']}.csv"
                if req_name not in names:
                    continue
                rf = tf.extractfile(req_name)
                rf.readline()  # header
                row = rf.readline().decode()  # first request
                m = re.search(r"cancel-audit-([0-9a-f]{12})-", row)
                if m:
                    run2case[m.group(1)] = r["case"]

            # trace events: per case, count KV_RELEASED spilling into next case
            spill = defaultdict(int)
            total_rel = defaultdict(int)
            sched_spill = defaultdict(int)
            order = [r["case"] for r in main_cases]
            next_start = {order[i]: starts[order[i + 1]] for i in range(len(order) - 1)}
            f = io.TextIOWrapper(tf.extractfile(trace_name))
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = ev.get("runtime_request_id", "")
                m = re.search(r"cancel-audit-([0-9a-f]{12})-", rid)
                if not m or m.group(1) not in run2case:
                    continue
                case = run2case[m.group(1)]
                if case not in next_start:
                    continue
                ts = parse_ts(ev["ts_wall"])
                if ev["event"] == "KV_RELEASED":
                    total_rel[case] += 1
                    if ts > next_start[case]:
                        spill[case] += 1
                elif ev["event"] == "SCHEDULER_ITERATION" and ts > next_start[case] + 1.0:
                    sched_spill[case] += 1
            m = re.match(r"(broken|fixed)_r(\d+)_rep(\d+)", cell)
            for case in order[:-1]:
                rows_out.append({
                    "cell": cell, "arm": m.group(1), "rate": m.group(2), "rep": m.group(3),
                    "case": case,
                    "kv_released_total": total_rel[case],
                    "kv_released_after_next_start": spill[case],
                    "sched_iters_1s_into_next_case": sched_spill[case],
                })
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"wrote {OUT}")
    print(f"{'cell':22s} {'case':14s} {'KV rel':>7s} {'spill':>6s} {'sched_iters>1s into next':>8s}")
    for r in rows_out:
        print(f"{r['cell']:22s} {r['case']:14s} {r['kv_released_total']:7d} "
              f"{r['kv_released_after_next_start']:6d} {r['sched_iters_1s_into_next_case']:8d}")


if __name__ == "__main__":
    main()

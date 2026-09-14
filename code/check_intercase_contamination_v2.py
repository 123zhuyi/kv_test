#!/usr/bin/env python3
"""Inter-case contamination check v2 (review round 2, point 2).

Round-1 wording overstated the code: scheduler-iteration spill used a
next_start+1.0s threshold. v2 reports KV_RELEASED and SCHEDULER_ITERATION
spill at three boundaries (0 ms / 100 ms / 1 s after next case start) and
adds the c40 -> next-cell boundary (server restart between cells).

Output: results/intercase_contamination_v2.csv + console summary.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tarfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

FREEZE = Path(__file__).resolve().parent.parent / "kv_test" / "freeze_2026-09-13"
OUT = Path(__file__).resolve().parent / "results" / "intercase_contamination_v2.csv"

BOUNDARIES = [0.0, 0.1, 1.0]


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> None:
    rows_out = []
    with tarfile.open(FREEZE / "freeze_surface8b.tar.gz") as tf:
        names = set(tf.getnames())
        cells = sorted({n.split("/")[1] for n in names if n.startswith("surface8b/") and n.count("/") >= 2})
        # chronological execution order from the driver, not alphabetical
        chrono = []
        for arm in ["broken", "fixed"]:
            for tag in ["r4_rep1", "r8_rep1", "r12_rep2", "r12_rep3", "r16_rep1"]:
                c = f"{arm}_{tag}"
                if c in cells:
                    chrono.append(c)
        cell_order = chrono
        cell_last_event = {}
        cell_first_case_start = {}

        for cell in cells:
            summary_name = f"surface8b/{cell}/sglang/sglang/capacity_summary.csv"
            trace_name = f"surface8b/{cell}/sglang/runtime_trace.jsonl"
            if summary_name not in names or trace_name not in names:
                continue
            summary = list(csv.DictReader(io.StringIO(tf.extractfile(summary_name).read().decode())))
            summary.sort(key=lambda r: r["started_wall"])
            main_cases = [r for r in summary if r["case"].startswith("main_")]
            starts = {r["case"]: parse_ts(r["started_wall"]) for r in summary}
            cell_first_case_start[cell] = min(starts.values())

            run2case = {}
            for r in main_cases:
                req_name = f"surface8b/{cell}/sglang/sglang/{r['case']}.csv"
                if req_name not in names:
                    continue
                rf = tf.extractfile(req_name)
                rf.readline()
                row = rf.readline().decode()
                m = re.search(r"cancel-audit-([0-9a-f]{12})-", row)
                if m:
                    run2case[m.group(1)] = r["case"]

            order = [r["case"] for r in sorted(main_cases, key=lambda r: r["started_wall"])]
            next_start = {order[i]: starts[order[i + 1]] for i in range(len(order) - 1)}
            kv_spill = defaultdict(lambda: defaultdict(int))
            si_spill = defaultdict(lambda: defaultdict(int))
            last_ev = 0.0
            f = io.TextIOWrapper(tf.extractfile(trace_name))
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_ts(ev["ts_wall"])
                last_ev = max(last_ev, ts)
                rid = ev.get("runtime_request_id", "")
                m = re.search(r"cancel-audit-([0-9a-f]{12})-", rid)
                if not m or m.group(1) not in run2case:
                    continue
                case = run2case[m.group(1)]
                if case not in next_start:
                    continue
                dt = ts - next_start[case]
                if dt <= 0:
                    continue
                bucket = ">1s" if dt > 1.0 else ("0-100ms+..1s" if dt > 0.1 else "0-100ms")
                if ev["event"] == "KV_RELEASED":
                    kv_spill[case][bucket] += 1
                elif ev["event"] == "SCHEDULER_ITERATION":
                    si_spill[case][bucket] += 1
            cell_last_event[cell] = last_ev

            m = re.match(r"(broken|fixed)_r(\d+)_rep(\d+)", cell)
            for case in order[:-1]:
                rows_out.append({
                    "boundary": f"{cell}:{case}->next_case",
                    "arm": m.group(1), "rate": m.group(2),
                    "kv_spill_0_100ms": kv_spill[case]["0-100ms"],
                    "kv_spill_100ms_1s": kv_spill[case]["0-100ms+..1s"],
                    "kv_spill_gt1s": kv_spill[case][">1s"],
                    "sched_spill_0_100ms": si_spill[case]["0-100ms"],
                    "sched_spill_100ms_1s": si_spill[case]["0-100ms+..1s"],
                    "sched_spill_gt1s": si_spill[case][">1s"],
                })

        # c40 -> next cell boundary (server restart in between)
        for i in range(len(cell_order) - 1):
            a, b = cell_order[i], cell_order[i + 1]
            if a in cell_last_event and b in cell_first_case_start:
                gap = cell_first_case_start[b] - cell_last_event[a]
                rows_out.append({
                    "boundary": f"{a}:c40->cell:{b}",
                    "arm": "cross-cell", "rate": "",
                    "kv_spill_0_100ms": "", "kv_spill_100ms_1s": "", "kv_spill_gt1s": "",
                    "sched_spill_0_100ms": "", "sched_spill_100ms_1s": "", "sched_spill_gt1s": "",
                })
                rows_out[-1]["sched_spill_0_100ms"] = f"gap_s={gap:.1f}"

    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"wrote {OUT}")
    bad = 0
    for r in rows_out:
        if r["arm"] == "cross-cell":
            print(f"[cross-cell] {r['boundary']}  {r['sched_spill_0_100ms']}")
            continue
        tot = sum(int(r[k]) for k in r if k.startswith(("kv_spill", "sched_spill")))
        flag = "" if tot == 0 else "  <-- SPILL"
        if tot:
            bad += 1
        print(f"{r['boundary']:44s} kv {r['kv_spill_0_100ms']}/{r['kv_spill_100ms_1s']}/{r['kv_spill_gt1s']} "
              f"sched {r['sched_spill_0_100ms']}/{r['sched_spill_100ms_1s']}/{r['sched_spill_gt1s']}{flag}")
    print(f"\nboundaries with any spill: {bad}")


if __name__ == "__main__":
    main()

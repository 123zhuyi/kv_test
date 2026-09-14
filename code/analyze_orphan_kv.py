#!/usr/bin/env python3
"""Orphan KV-residency accounting from frozen traces (review round 2, point 3).

For every cancelled request in the surface cells:
  residency = ts_wall(KV_RELEASED) - ts_wall(CLIENT_CANCEL_INIT)
Exact from data. Token footprint is the only estimated part: a cancelled
request holds ~(prompt + 8) tokens at cancel, growing to ~(prompt + 512)
at natural completion; assuming linear growth, mean footprint
~ prompt + 260 tokens (prompt ~= 25 tokens for the fixed workload;
labeled estimate).

Orphan token-seconds and slot-seconds are reported per case, plus the
share of total KV-pool-time (22,720 tokens x case wall) they consume.
Fixed-arm cells provide the prompt-cancellation contrast.

Output: results/orphan_kv_8b.csv + console summary.
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
OUT = Path(__file__).resolve().parent / "results" / "orphan_kv_8b.csv"

KV_POOL_TOKENS = 22720
PROMPT_TOKENS_EST = 25
TOKENS_AT_CANCEL = PROMPT_TOKENS_EST + 8
TOKENS_AT_DONE = PROMPT_TOKENS_EST + 512
MEAN_FOOTPRINT_EST = (TOKENS_AT_CANCEL + TOKENS_AT_DONE) / 2  # ~286


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> None:
    rows_out = []
    with tarfile.open(FREEZE / "freeze_surface8b.tar.gz") as tf:
        names = set(tf.getnames())
        cells = sorted({n.split("/")[1] for n in names
                        if n.startswith("surface8b/") and n.count("/") >= 2})
        for cell in cells:
            m = re.match(r"(broken|fixed)_r(\d+)_rep(\d+)", cell)
            arm, rate, rep = m.group(1), int(m.group(2)), int(m.group(3))
            summary_name = f"surface8b/{cell}/sglang/sglang/capacity_summary.csv"
            trace_name = f"surface8b/{cell}/sglang/runtime_trace.jsonl"
            if summary_name not in names:
                continue
            summary = {r["case"]: r for r in csv.DictReader(
                io.StringIO(tf.extractfile(summary_name).read().decode()))}

            # KV_RELEASED per rid (first occurrence)
            kv_rel = {}
            if trace_name in names:
                for line in io.TextIOWrapper(tf.extractfile(trace_name)):
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev["event"] == "KV_RELEASED":
                        kv_rel.setdefault(ev["runtime_request_id"], parse_ts(ev["ts_wall"]))

            for case, srow in sorted(summary.items()):
                if not case.startswith("main_"):
                    continue
                cj_name = f"surface8b/{cell}/sglang/sglang/{case}.client.jsonl"
                if cj_name not in names:
                    continue
                resid = []
                for line in io.TextIOWrapper(tf.extractfile(cj_name)):
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("event") != "CLIENT_CANCEL_INIT":
                        continue
                    rid = ev.get("request_id") or ev.get("audit_id")
                    if rid in kv_rel:
                        resid.append(kv_rel[rid] - parse_ts(ev["ts_wall"]))
                if not resid:
                    continue
                wall = float(srow["wall_s"])
                slot_s = sum(resid)
                tok_s = slot_s * MEAN_FOOTPRINT_EST
                rows_out.append({
                    "cell": cell, "arm": arm, "rate": rate, "rep": rep, "case": case,
                    "cancel_rate": srow["cancel_rate"],
                    "n_cancelled": len(resid),
                    "residency_mean_s": round(sum(resid) / len(resid), 3),
                    "residency_p50_s": round(sorted(resid)[len(resid) // 2], 3),
                    "residency_max_s": round(max(resid), 3),
                    "orphan_slot_seconds": round(slot_s, 1),
                    "orphan_token_seconds_est": round(tok_s, 0),
                    "kv_pool_time_share_est": round(tok_s / (KV_POOL_TOKENS * wall), 4),
                    "case_wall_s": wall,
                })
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"wrote {OUT}")
    for r in rows_out:
        if float(r["cancel_rate"]) > 0:
            print(f"{r['cell']:22s} {r['case']:14s} n={r['n_cancelled']:3d} "
                  f"resid p50={r['residency_p50_s']:7.3f}s max={r['residency_max_s']:7.3f}s "
                  f"slot-s={r['orphan_slot_seconds']:7.1f} tok-s(est)={r['orphan_token_seconds_est']:9.0f} "
                  f"pool-share(est)={r['kv_pool_time_share_est']:.3f}")


if __name__ == "__main__":
    main()

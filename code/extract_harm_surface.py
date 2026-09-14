#!/usr/bin/env python3
"""Build the tidy harm-surface dataset from the frozen result tarballs.

Cells:
  - freeze_surface8b.tar.gz: surface8b/{arm}_r{rate}_rep{N} (broken/fixed, r=4/8/12/16)
  - results_capacity8b2_remote.tar.gz: broken r=12 rep 1
  - results_fixed8b_remote.tar.gz:    fixed  r=12 rep 1
  - freeze_vllm8b_remote.tar.gz:      vllm   r=12 rep 1 (reference, KV pool 21696)

Per (arm, rate, rep, case) row: survivor TTFT/E2E percentiles, useful
goodput, plus pressure indicators parsed from the server log inside the
case's time window (mean/max token usage, max queue depth, mean running).

SGLang log timestamps are remote-local (UTC+8); capacity_summary
started_wall is UTC. vLLM "GPU KV cache usage" lines lack per-request
queue depth; waiting is always 0 (preemption-based admission).
"""

from __future__ import annotations

import csv
import io
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

FREEZE = Path(__file__).resolve().parent.parent / "kv_test" / "freeze_2026-09-13"
OUT_CSV = Path(__file__).resolve().parent / "results" / "harm_surface_8b.csv"

SGLANG_USAGE = re.compile(r"^\[(?P<ts>[\d-]+ [\d:]+)\].*?token usage: (?P<usage>[\d.]+).*?#queue-req: (?P<queue>\d+)")
SGLANG_RUNNING = re.compile(r"#running-req: (?P<run>\d+)")
VLLM_USAGE = re.compile(r"GPU KV cache usage: (?P<usage>[\d.]+)%")
VLLM_WAITING = re.compile(r"Waiting: (?P<queue>\d+) reqs")
VLLM_TS = re.compile(r"\((?:APIServer|EngineCore) pid=\d+\) \S+ (?P<ts>\d+-\d+ [\d:]+)")

CELL_MAP = {
    "freeze_surface8b.tar.gz": None,  # parse from path surface8b/{arm}_r{rate}_rep{N}
    "results_capacity8b2_remote.tar.gz": ("broken", 12, 1),
    "results_fixed8b_remote.tar.gz": ("fixed", 12, 1),
    "freeze_vllm8b_remote.tar.gz": ("vllm", 12, 1),
}


def parse_log_stats(text: str, runtime: str, t0: datetime, t1: datetime):
    """Mean/max KV token usage, max queue depth, mean running-req within [t0, t1] (UTC)."""
    usages, queues, runs = [], [], []
    for line in text.splitlines():
        if runtime == "vllm":
            mu = VLLM_USAGE.search(line)
            if not mu:
                continue
            mt = VLLM_TS.search(line)
            if not mt:
                continue
            ts = datetime.strptime(mt.group("ts"), "%m-%d %H:%M:%S").replace(year=t0.year)
            # vllm loguru timestamps are remote-local (UTC+8), same as sglang
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
            if not (t0 - timedelta(seconds=15) <= ts <= t1 + timedelta(seconds=15)):
                continue
            usages.append(float(mu.group("usage")) / 100.0)
            mq = VLLM_WAITING.search(line)
            if mq:
                queues.append(int(mq.group("queue")))
        else:
            m = SGLANG_USAGE.search(line)
            if not m:
                continue
            ts_local = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            ts = ts_local.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
            if not (t0 - timedelta(seconds=15) <= ts <= t1 + timedelta(seconds=15)):
                continue
            usages.append(float(m.group("usage")))
            queues.append(int(m.group("queue")))
            mr = SGLANG_RUNNING.search(line)
            if mr:
                runs.append(int(mr.group("run")))
    if not usages:
        return None, None, None, None, 0
    return (sum(usages) / len(usages), max(usages),
            max(queues) if queues else None,
            sum(runs) / len(runs) if runs else None, len(usages))


def iter_cells():
    for tb_name, fixed_map in CELL_MAP.items():
        tb_path = FREEZE / tb_name
        with tarfile.open(tb_path) as tf:
            names = tf.getnames()
            for name in names:
                if not name.endswith("capacity_summary.csv"):
                    continue
                if fixed_map is not None:
                    arm, rate, rep = fixed_map
                    log_name = [n for n in names if n.endswith("/server.log") or n.endswith("server.log")][0]
                else:
                    cell = name.split("/")[1]  # surface8b/{cell}/...
                    m = re.match(r"(broken|fixed)_r(\d+)_rep(\d+)", cell)
                    arm, rate, rep = m.group(1), int(m.group(2)), int(m.group(3))
                    log_name = f"surface8b/{cell}/sglang/server.log"
                summary = tf.extractfile(name).read().decode()
                try:
                    log_text = tf.extractfile(log_name).read().decode(errors="replace")
                except KeyError:
                    log_text = ""
                yield arm, rate, rep, summary, log_text


def main() -> None:
    rows = []
    for arm, rate, rep, summary, log_text in iter_cells():
        runtime = "vllm" if arm == "vllm" else "sglang"
        for r in csv.DictReader(io.StringIO(summary)):
            if not r["case"].startswith("main_"):
                continue
            t0 = datetime.strptime(r["started_wall"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            t1 = t0 + timedelta(seconds=float(r["elapsed_s"]))
            mu_mean, mu_max, q_max, run_mean, n = parse_log_stats(log_text, runtime, t0, t1)
            rows.append({
                "arm": arm, "rate": rate, "rep": rep, "case": r["case"],
                "cancel_rate": float(r["cancel_rate"]),
                "n_survivors": int(r["n_survivors"]),
                "ttft_p50_ms": round(float(r["survivor_ttft_p50_ms"]), 1),
                "ttft_p95_ms": round(float(r["survivor_ttft_p95_ms"]), 1),
                "ttft_p99_ms": round(float(r["survivor_ttft_p99_ms"]), 1),
                "e2e_p95_ms": round(float(r["survivor_e2e_p95_ms"]), 1),
                "useful_tokens_per_s": round(float(r["useful_tokens_per_s"]), 1),
                "wall_s": round(float(r["wall_s"]), 1),
                "kv_usage_mean": round(mu_mean, 3) if mu_mean is not None else "",
                "kv_usage_max": round(mu_max, 3) if mu_max is not None else "",
                "queue_max": q_max if q_max is not None else "",
                "running_mean": round(run_mean, 1) if run_mean is not None else "",
                "n_usage_samples": n,
            })
    rows.sort(key=lambda r: (r["arm"], r["rate"], r["rep"], r["cancel_rate"]))
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT_CSV} rows={len(rows)}")


if __name__ == "__main__":
    main()

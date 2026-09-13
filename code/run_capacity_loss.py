#!/usr/bin/env python3
"""Capacity-loss experiment: does SGLang orphan compute cut useful-token goodput?

Design:
  Phase A (calibration): cancel_rate=0 controls at increasing request rates to
    locate the saturation knee for this server/model/max_tokens combination.
  Phase B (main matrix): at the knee rate, sweep FIN cancellation rate
    (0%, 10%, 20%, 40%), FIN after 8 SSE events.

Primary metrics per case (computed from the probe CSV, no server changes):
  - survivor TTFT p50/p95/p99, E2E p50/p95/p99
  - completed useful requests
  - useful tokens delivered to surviving clients and useful tokens/sec
  - cancelled requests and tokens delivered to already-cancelled clients
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path


MAIN_CANCEL_RATES = [0.0, 0.10, 0.20, 0.40]
# Knee = highest calibration rate whose survivor E2E p95 stays within this
# factor of the lowest-rate control. E2E (not TTFT) is the saturation signal
# here: admission is effectively uncapped (max_running_requests=2048), so
# overload shows up as decode-throughput dilution, not queueing delay.
KNEE_E2E_FACTOR = 1.5

PROMPT = "Write an unbroken numbered list from 1 upward. Do not stop, summarize, or emit an ending sentence."


def percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    rank = (len(sorted_vals) - 1) * q
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


def _f(row: dict[str, str], key: str) -> float | None:
    val = row.get(key, "")
    if val in ("", "None"):
        return None
    return float(val)


def analyze_csv(path: Path) -> dict[str, object]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    survivors, cancelled = [], []
    for row in rows:
        (cancelled if row["cancelled"] == "True" else survivors).append(row)

    ttft, e2e = [], []
    useful_tokens = 0
    completed = 0
    for row in survivors:
        first, sent, ended = _f(row, "first_event_s"), _f(row, "sent_s"), _f(row, "ended_s")
        if first is not None and sent is not None:
            ttft.append(1000 * (first - sent))
        if ended is not None and sent is not None:
            e2e.append(1000 * (ended - sent))
        if row["completed"] == "True":
            completed += 1
            useful_tokens += max(0, int(row["sse_events"]) - 1)  # exclude the [DONE] record

    starts = [_f(r, "sent_s") for r in rows if _f(r, "sent_s") is not None]
    ends = [_f(r, "ended_s") for r in survivors if _f(r, "ended_s") is not None]
    wall_s = (max(ends) - min(starts)) if starts and ends else None

    ttft.sort()
    e2e.sort()
    return {
        "n_requests": len(rows),
        "n_survivors": len(survivors),
        "n_cancelled": len(cancelled),
        "n_completed_survivors": completed,
        "n_errors": sum(1 for r in rows if r.get("error") not in ("", "None")),
        "survivor_ttft_p50_ms": percentile(ttft, 0.50),
        "survivor_ttft_p95_ms": percentile(ttft, 0.95),
        "survivor_ttft_p99_ms": percentile(ttft, 0.99),
        "survivor_e2e_p50_ms": percentile(e2e, 0.50),
        "survivor_e2e_p95_ms": percentile(e2e, 0.95),
        "survivor_e2e_p99_ms": percentile(e2e, 0.99),
        "useful_tokens": useful_tokens,
        "tokens_delivered_to_cancelled": sum(int(r["sse_events"]) for r in cancelled),
        "wall_s": wall_s,
        "useful_tokens_per_s": (useful_tokens / wall_s) if wall_s else None,
    }


def run_probe(root: Path, args: argparse.Namespace, *, name: str, requests: int,
              request_rate: float, cancel_rate: float, seed: int) -> dict[str, object]:
    stem = args.output_dir / args.runtime / name
    cmd = [
        sys.executable,
        str(root / "cancel_probe.py"),
        "--runtime", args.runtime,
        "--url", args.url,
        "--model", args.model,
        "--requests", str(requests),
        "--request-rate", str(request_rate),
        "--cancel-rate", str(cancel_rate),
        "--cancel-mode", "fin",
        "--max-tokens", str(args.max_tokens),
        "--generation-seed", str(args.generation_seed),
        "--seed", str(seed),
        "--prompt", PROMPT,
        "--output", str(stem.with_suffix(".csv")),
    ]
    if cancel_rate > 0:
        cmd += ["--cancel-after-events", str(args.cancel_after_events)]
    started = time.time()
    proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
    elapsed = time.time() - started
    stem.with_suffix(".probe.log").write_text(proc.stdout + "\n--- STDERR ---\n" + proc.stderr, encoding="utf-8")
    analysis = analyze_csv(stem.with_suffix(".csv"))
    item = {
        "case": name,
        "request_rate": request_rate,
        "cancel_rate": cancel_rate,
        "cancel_after_events": args.cancel_after_events if cancel_rate > 0 else None,
        "requests": requests,
        "max_tokens": args.max_tokens,
        "returncode": proc.returncode,
        "elapsed_s": elapsed,
        "started_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "csv": str(stem.with_suffix(".csv")),
        "client_trace": str(stem.with_suffix(".client.jsonl")),
        **analysis,
    }
    print(f"{name}: rc={proc.returncode} elapsed={elapsed:.1f}s", flush=True)
    print(json.dumps(analysis, ensure_ascii=False, indent=2), flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"probe failed; see {stem.with_suffix('.probe.log')}")
    return item


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, choices=["vllm", "sglang", "mock"])
    p.add_argument("--url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--requests", type=int, default=80, help="requests per main-matrix case")
    p.add_argument("--calibration-requests", type=int, default=60)
    p.add_argument("--calibration-rates", default="1,2,4",
                   help="comma-separated control request rates for the saturation sweep")
    p.add_argument("--knee-e2e-factor", type=float, default=KNEE_E2E_FACTOR)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--cancel-after-events", type=int, default=8)
    p.add_argument("--generation-seed", type=int, default=20260913)
    p.add_argument("--output-dir", type=Path, default=Path("results/capacity"))
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    out_dir = args.output_dir / args.runtime
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "runtime": args.runtime,
        "url": args.url,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "generation_seed": args.generation_seed,
        "started_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "calibration": [],
        "main": [],
    }

    calibration_rates = [float(x) for x in args.calibration_rates.split(",") if x.strip()]

    # Phase A: saturation calibration, no cancellation.
    calibration: list[dict[str, object]] = []
    for idx, rate in enumerate(calibration_rates, 1):
        item = run_probe(root, args, name=f"calib_r{rate:g}_c00", requests=args.calibration_requests,
                         request_rate=rate, cancel_rate=0.0, seed=4000 + idx)
        calibration.append(item)
        manifest["calibration"] = calibration
        (out_dir / "capacity_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(3.0)

    baseline_e2e_p95 = calibration[0]["survivor_e2e_p95_ms"]
    knee_rate = calibration_rates[0]
    for item in calibration:
        e2e_p95 = item["survivor_e2e_p95_ms"]
        if (e2e_p95 is not None and baseline_e2e_p95 is not None
                and e2e_p95 <= args.knee_e2e_factor * baseline_e2e_p95 and item["n_errors"] == 0):
            knee_rate = item["request_rate"]
    manifest["baseline_e2e_p95_ms"] = baseline_e2e_p95
    manifest["knee_request_rate"] = knee_rate
    print(f"[capacity] baseline_e2e_p95={baseline_e2e_p95} knee_rate={knee_rate}", flush=True)

    # Phase B: cancellation-rate sweep at the knee load.
    main_rows: list[dict[str, object]] = []
    for idx, cancel_rate in enumerate(MAIN_CANCEL_RATES, 1):
        item = run_probe(root, args, name=f"main_r{knee_rate:g}_c{int(cancel_rate * 100):02d}",
                         requests=args.requests, request_rate=float(knee_rate),
                         cancel_rate=cancel_rate, seed=5000 + idx)
        main_rows.append(item)
        manifest["main"] = main_rows
        (out_dir / "capacity_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(3.0)

    rows = calibration + main_rows
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields and key not in ("csv", "client_trace"):
                fields.append(key)
    with (out_dir / "capacity_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    manifest["finished_wall"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / "capacity_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[capacity] summary={out_dir / 'capacity_summary.csv'}", flush=True)


if __name__ == "__main__":
    main()

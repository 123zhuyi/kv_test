#!/usr/bin/env python3
"""Run the fixed Phase-1 direct-path kill-test matrix against one live runtime."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path


CASES = [
    ("control", "fin", None, False),
    ("fin_sse1", "fin", 1, True),
    ("fin_sse8", "fin", 8, True),
    ("fin_sse32", "fin", 32, True),
    ("rst_sse8", "rst", 8, True),
    ("rst_sse32", "rst", 32, True),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, choices=["vllm", "sglang"])
    p.add_argument("--url", required=True)
    p.add_argument("--model", default="Qwen/Qwen3-0.6B")
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--max-tokens", type=int, choices=[1024, 2048], default=1024)
    p.add_argument("--generation-seed", type=int, default=20260910)
    p.add_argument("--settle-seconds", type=float, default=0.5)
    p.add_argument("--output-dir", type=Path, default=Path("results/phase1"))
    args = p.parse_args()
    if args.trials < 1:
        p.error("--trials must be positive")

    root = Path(__file__).resolve().parent
    out_dir = args.output_dir / args.runtime
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "runtime": args.runtime,
        "url": args.url,
        "model": args.model,
        "trials": args.trials,
        "max_tokens": args.max_tokens,
        "generation_seed": args.generation_seed,
        "started_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": [],
    }
    all_rows: list[dict[str, str]] = []
    for case_name, mode, after, cancel in CASES:
        for trial in range(1, args.trials + 1):
            stem = out_dir / f"{case_name}_trial{trial:02d}"
            cmd = [
                sys.executable,
                str(root / "cancel_probe.py"),
                "--runtime", args.runtime,
                "--url", args.url,
                "--model", args.model,
                "--requests", "1",
                "--request-rate", "1",
                "--cancel-rate", "1" if cancel else "0",
                "--cancel-mode", mode,
                "--max-tokens", str(args.max_tokens),
                "--generation-seed", str(args.generation_seed),
                "--prompt", "Write an unbroken numbered list from 1 upward. Do not stop, summarize, or emit an ending sentence.",
                "--output", str(stem.with_suffix(".csv")),
            ]
            if after is not None:
                cmd += ["--cancel-after-events", str(after)]
            started = time.time()
            proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
            item = {
                "case": case_name,
                "trial": trial,
                "returncode": proc.returncode,
                "elapsed_s": time.time() - started,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "csv": str(stem.with_suffix(".csv")),
                "client_trace": str(stem.with_suffix(".client.jsonl")),
            }
            manifest["cases"].append(item)
            stem.with_suffix(".probe.log").write_text(proc.stdout + "\n--- STDERR ---\n" + proc.stderr, encoding="utf-8")
            if stem.with_suffix(".csv").exists():
                with stem.with_suffix(".csv").open(encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        row.update(case=case_name, trial=str(trial), probe_returncode=str(proc.returncode))
                        all_rows.append(row)
            manifest_path = out_dir / "run_manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{args.runtime} {case_name} trial={trial}: rc={proc.returncode} elapsed={item['elapsed_s']:.2f}s", flush=True)
            if proc.returncode != 0:
                raise SystemExit(f"probe failed; see {stem.with_suffix('.probe.log')}")
            time.sleep(args.settle_seconds)

    if all_rows:
        fields = list(all_rows[0])
        with (out_dir / "probe_trials.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)
    manifest["finished_wall"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

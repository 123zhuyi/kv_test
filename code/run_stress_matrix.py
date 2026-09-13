#!/usr/bin/env python3
"""Run mixed live/cancelled request stress cases against one live runtime."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path


CASES = [
    ("control_r05", 0.5, 0.00, "fin", None),
    ("fin08_r05_c05", 0.5, 0.05, "fin", 8),
    ("fin08_r05_c10", 0.5, 0.10, "fin", 8),
    ("fin08_r05_c20", 0.5, 0.20, "fin", 8),
    ("control_r10", 1.0, 0.00, "fin", None),
    ("fin08_r10_c05", 1.0, 0.05, "fin", 8),
    ("fin08_r10_c10", 1.0, 0.10, "fin", 8),
    ("fin08_r10_c20", 1.0, 0.20, "fin", 8),
]


def read_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, choices=["vllm", "sglang", "mock"])
    p.add_argument("--url", required=True)
    p.add_argument("--model", default="Qwen/Qwen3-0.6B")
    p.add_argument("--requests", type=int, default=40)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--generation-seed", type=int, default=20260913)
    p.add_argument("--output-dir", type=Path, default=Path("results/stress"))
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    out_dir = args.output_dir / args.runtime
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "runtime": args.runtime,
        "url": args.url,
        "model": args.model,
        "requests": args.requests,
        "max_tokens": args.max_tokens,
        "generation_seed": args.generation_seed,
        "started_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": [],
    }
    rows: list[dict[str, object]] = []

    for idx, (name, rate, cancel_rate, mode, after) in enumerate(CASES, 1):
        stem = out_dir / name
        cmd = [
            sys.executable,
            str(root / "cancel_probe.py"),
            "--runtime", args.runtime,
            "--url", args.url,
            "--model", args.model,
            "--requests", str(args.requests),
            "--request-rate", str(rate),
            "--cancel-rate", str(cancel_rate),
            "--cancel-mode", mode,
            "--max-tokens", str(args.max_tokens),
            "--generation-seed", str(args.generation_seed),
            "--seed", str(1000 + idx),
            "--prompt", "Write an unbroken numbered list from 1 upward. Do not stop, summarize, or emit an ending sentence.",
            "--output", str(stem.with_suffix(".csv")),
        ]
        if after is not None:
            cmd += ["--cancel-after-events", str(after)]
        started = time.time()
        proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
        elapsed = time.time() - started
        stem.with_suffix(".probe.log").write_text(proc.stdout + "\n--- STDERR ---\n" + proc.stderr, encoding="utf-8")
        summary = read_summary(stem.with_suffix(".summary.json"))
        item = {
            "case": name,
            "request_rate": rate,
            "cancel_rate": cancel_rate,
            "cancel_mode": mode if cancel_rate else "none",
            "cancel_after_events": after,
            "returncode": proc.returncode,
            "elapsed_s": elapsed,
            "csv": str(stem.with_suffix(".csv")),
            "client_trace": str(stem.with_suffix(".client.jsonl")),
            "summary": summary,
        }
        manifest["cases"].append(item)
        row = {
            "case": name,
            "request_rate": rate,
            "cancel_rate": cancel_rate,
            "cancel_after_events": after,
            "elapsed_s": elapsed,
            **summary,
        }
        rows.append(row)
        print(f"{name}: rc={proc.returncode} elapsed={elapsed:.1f}s summary={summary}", flush=True)
        (out_dir / "stress_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if proc.returncode != 0:
            raise SystemExit(f"probe failed; see {stem.with_suffix('.probe.log')}")
        time.sleep(1.0)

    if rows:
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        with (out_dir / "stress_summary.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    manifest["finished_wall"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / "stress_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

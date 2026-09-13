#!/usr/bin/env python3
"""Run blocker-then-survivor interference probes against one live runtime."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path


CASES = [
    ("baseline", "none", 0),
    ("cancel_blocker_1", "cancel", 1),
    ("cancel_blocker_4", "cancel", 4),
    ("cancel_blocker_8", "cancel", 8),
    ("live_blocker_1", "live", 1),
    ("live_blocker_4", "live", 4),
    ("live_blocker_8", "live", 8),
]


def run_probe(root: Path, args: argparse.Namespace, *, name: str, requests: int, request_rate: float,
              cancel_rate: float, cancel_after_events: int | None, max_tokens: int, seed: int) -> dict[str, object]:
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
        "--max-tokens", str(max_tokens),
        "--generation-seed", str(args.generation_seed),
        "--seed", str(seed),
        "--prompt", "Write an unbroken numbered list from 1 upward. Do not stop, summarize, or emit an ending sentence.",
        "--output", str(stem.with_suffix(".csv")),
    ]
    if cancel_after_events is not None:
        cmd += ["--cancel-after-events", str(cancel_after_events)]
    started = time.time()
    proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
    elapsed = time.time() - started
    stem.with_suffix(".probe.log").write_text(proc.stdout + "\n--- STDERR ---\n" + proc.stderr, encoding="utf-8")
    summary_path = stem.with_suffix(".summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {
        "name": name,
        "returncode": proc.returncode,
        "elapsed_s": elapsed,
        "csv": str(stem.with_suffix(".csv")),
        "client_trace": str(stem.with_suffix(".client.jsonl")),
        "summary": summary,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--runtime", required=True, choices=["vllm", "sglang", "mock"])
    p.add_argument("--url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--survivors", type=int, default=20)
    p.add_argument("--survivor-rate", type=float, default=5.0)
    p.add_argument("--blocker-gap-s", type=float, default=0.2)
    p.add_argument("--blocker-max-tokens", type=int, default=256)
    p.add_argument("--survivor-max-tokens", type=int, default=128)
    p.add_argument("--generation-seed", type=int, default=20260913)
    p.add_argument("--output-dir", type=Path, default=Path("results/interference"))
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    (args.output_dir / args.runtime).mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    manifest: dict[str, object] = {"cases": [], "started_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    for idx, (case, kind, blockers) in enumerate(CASES, 1):
        case_items: list[dict[str, object]] = []
        if kind != "none":
            blocker = run_probe(
                root, args,
                name=f"{case}_blocker",
                requests=blockers,
                request_rate=1000.0,
                cancel_rate=1.0 if kind == "cancel" else 0.0,
                cancel_after_events=8 if kind == "cancel" else None,
                max_tokens=args.blocker_max_tokens,
                seed=2000 + idx,
            )
            case_items.append(blocker)
            print(f"{case} blocker: {blocker['summary']}", flush=True)
            time.sleep(args.blocker_gap_s)
        survivor = run_probe(
            root, args,
            name=f"{case}_survivor",
            requests=args.survivors,
            request_rate=args.survivor_rate,
            cancel_rate=0.0,
            cancel_after_events=None,
            max_tokens=args.survivor_max_tokens,
            seed=3000 + idx,
        )
        case_items.append(survivor)
        manifest["cases"].append({"case": case, "kind": kind, "blockers": blockers, "items": case_items})
        summary = survivor["summary"]
        row = {"case": case, "kind": kind, "blockers": blockers, **summary}
        rows.append(row)
        print(f"{case} survivor: {summary}", flush=True)
        if any(int(item["returncode"]) != 0 for item in case_items):
            raise SystemExit(f"{case} failed; see logs")
        time.sleep(3.0)

    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    out_dir = args.output_dir / args.runtime
    with (out_dir / "interference_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    manifest["finished_wall"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out_dir / "interference_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

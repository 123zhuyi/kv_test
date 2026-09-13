#!/usr/bin/env python3
"""Run an isolated no-GPU smoke matrix against mock_server.py."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "smoke"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for policy in ("honor", "ignore"):
        for mode in ("fin", "rst"):
            stem = f"{policy}_{mode}"
            with (OUT / f"server_{stem}.log").open("w", encoding="utf-8") as log:
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        str(ROOT / "mock_server.py"),
                        "--disconnect-policy",
                        policy,
                        "--slots",
                        "2",
                        "--token-ms",
                        "30",
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    time.sleep(0.5)
                    output = OUT / f"{stem}.csv"
                    subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "cancel_probe.py"),
                            "--requests",
                            "20",
                            "--request-rate",
                            "4",
                            "--max-tokens",
                            "20",
                            "--cancel-rate",
                            "0.4",
                            "--cancel-delay",
                            "0.15",
                            "--cancel-mode",
                            mode,
                            "--seed",
                            "7",
                            "--output",
                            str(output),
                        ],
                        cwd=ROOT,
                        check=True,
                    )
                    metrics = urllib.request.urlopen(
                        "http://127.0.0.1:18000/metrics", timeout=5
                    ).read().decode()
                    (OUT / f"{stem}.metrics").write_text(metrics, encoding="utf-8")
                    summary = json.loads(output.with_suffix(".summary.json").read_text())
                    generated = next(
                        int(line.split()[1])
                        for line in metrics.splitlines()
                        if line.startswith("cancel_audit_tokens_generated_total ")
                    )
                    rows.append(
                        {
                            "policy": policy,
                            "mode": mode,
                            "cancellation_observed": summary["cancellation_observed"],
                            "survivor_mean_ttft_ms": round(summary["survivor_mean_ttft_ms"], 2),
                            "survivor_p95_ttft_ms": round(summary["survivor_p95_ttft_ms"], 2),
                            "survivor_mean_e2e_ms": round(summary["survivor_mean_e2e_ms"], 2),
                            "tokens_generated": generated,
                        }
                    )
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                    time.sleep(0.3)
    with (OUT / "matrix_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()

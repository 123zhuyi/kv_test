#!/usr/bin/env python3
"""Survivor latency distribution + SLO analysis for the harm-surface cells.

Addresses review points: report full CDFs and SLO-violation rates alongside
percentiles (the 100x TTFT p95 gap must be shown together with E2E), using
per-request data from the frozen tarballs.

Per request (survivors only): ttfse = first_event_s - sent_s (first SSE
event, see metric-semantics note), e2e = ended_s - sent_s.

Outputs:
  results/survivor_dist_8b.csv   — per (arm, rate, cancel, rep) stats:
      mean/p50/p95/p99 for ttfse & e2e, SLO violation rates, queue-wait mean
  results/survivor_cdf_r12.png   — CDF panels at r=12 (TTFT + E2E)
"""

from __future__ import annotations

import csv
import io
import tarfile
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

FREEZE = Path(__file__).resolve().parent.parent / "kv_test" / "freeze_2026-09-13"
HERE = Path(__file__).resolve().parent
OUT_CSV = HERE / "results" / "survivor_dist_8b.csv"
OUT_PNG = HERE / "results" / "survivor_cdf_r12.png"

TARBALLS = {
    "freeze_surface8b.tar.gz": None,  # parse cell from path
    "results_capacity8b2_remote.tar.gz": ("broken", 12, 1),
    "results_fixed8b_remote.tar.gz": ("fixed", 12, 1),
}

# candidate SLO thresholds (ms) — arbitrary but stated explicitly
TTFSE_SLOS = [500.0, 1000.0, 2000.0]
E2E_SLOS = [15000.0, 20000.0]


def pct(xs, q):
    return float(np.percentile(xs, q)) if len(xs) else float("nan")


def iter_case_csvs():
    for tb_name, fixed_map in TARBALLS.items():
        with tarfile.open(FREEZE / tb_name) as tf:
            for name in tf.getnames():
                if not name.endswith(".csv") or "main_" not in name:
                    continue
                base = name.rsplit("/", 1)[-1]  # main_r12_c40.csv
                if not base.startswith("main_"):
                    continue
                cancel = float(base.split("_c")[1][:2]) / 100.0
                if fixed_map is not None:
                    arm, rate, rep = fixed_map
                else:
                    import re
                    cell = name.split("/")[1]
                    m = re.match(r"(broken|fixed)_r(\d+)_rep(\d+)", cell)
                    arm, rate, rep = m.group(1), int(m.group(2)), int(m.group(3))
                rows = list(csv.DictReader(io.StringIO(tf.extractfile(name).read().decode())))
                yield arm, rate, rep, cancel, rows


def main() -> None:
    out_rows = []
    cdf_data = defaultdict(list)  # (arm, cancel) -> list of (ttfse, e2e) across reps at r=12
    for arm, rate, rep, cancel, rows in iter_case_csvs():
        ttfse, e2e = [], []
        for r in rows:
            if r["cancelled"] == "True" or r["completed"] != "True":
                continue
            ttfse.append((float(r["first_event_s"]) - float(r["sent_s"])) * 1000.0)
            e2e.append((float(r["ended_s"]) - float(r["sent_s"])) * 1000.0)
        if not ttfse:
            continue
        if rate == 12:
            cdf_data[(arm, cancel)].extend(zip(ttfse, e2e))
        row = {"arm": arm, "rate": rate, "rep": rep, "cancel_rate": cancel,
               "n": len(ttfse),
               "ttfse_mean": round(float(np.mean(ttfse)), 1),
               "ttfse_p50": round(pct(ttfse, 50), 1),
               "ttfse_p95": round(pct(ttfse, 95), 1),
               "ttfse_p99": round(pct(ttfse, 99), 1),
               "e2e_mean": round(float(np.mean(e2e)), 1),
               "e2e_p50": round(pct(e2e, 50), 1),
               "e2e_p95": round(pct(e2e, 95), 1),
               "e2e_p99": round(pct(e2e, 99), 1)}
        for s in TTFSE_SLOS:
            row[f"ttfse_viol_{s:.0f}ms"] = round(float(np.mean([x > s for x in ttfse])), 3)
        for s in E2E_SLOS:
            row[f"e2e_viol_{s:.0f}ms"] = round(float(np.mean([x > s for x in e2e])), 3)
        out_rows.append(row)

    out_rows.sort(key=lambda r: (r["arm"], r["rate"], r["cancel_rate"], r["rep"]))
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"wrote {OUT_CSV} rows={len(out_rows)}")

    # CDF figure at r=12 (pooled over 3 reps)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    colors = {0.0: "#7f8c8d", 0.1: "#f39c12", 0.2: "#8e44ad", 0.4: "#c0392b"}
    for arm, ls in [("broken", "-"), ("fixed", "--")]:
        for cancel in [0.0, 0.1, 0.2, 0.4]:
            pts = cdf_data.get((arm, cancel))
            if not pts:
                continue
            t = sorted(p[0] for p in pts)
            e = sorted(p[1] for p in pts)
            label = f"{arm} c{int(cancel*100)}"
            axes[0].plot(t, np.arange(1, len(t) + 1) / len(t), ls=ls, color=colors[cancel], lw=1.5, label=label)
            axes[1].plot(e, np.arange(1, len(e) + 1) / len(e), ls=ls, color=colors[cancel], lw=1.5, label=label)
    for ax, title in [(axes[0], "TTFSE (time to first SSE event)"),
                      (axes[1], "E2E latency")]:
        if ax is axes[0]:
            ax.set_xscale("log")
            ax.set_xlabel("latency (ms, log scale)")
        else:
            ax.set_xlim(8000, 18500)
            ax.set_xticks(range(8000, 18001, 2000))
            ax.set_xlabel("latency (ms)")
        ax.set_ylabel("CDF")
        ax.set_title(f"r=12 survivor {title}", fontsize=10)
        ax.grid(True, which="both", lw=0.3, alpha=0.5)
        ax.legend(fontsize=7, loc="lower right")
    fig.suptitle("Survivor latency CDFs at KV saturation (pooled 3 reps, n≈100/case) — Qwen3-8B/RTX 5090", fontsize=10)
    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    print(f"wrote {OUT_PNG}")

    # console summary: key cells, TTFT vs E2E contrast
    print("\n== r=12 pooled means over reps ==")
    agg = defaultdict(lambda: defaultdict(list))
    for r in out_rows:
        if r["rate"] == 12:
            for k in ["ttfse_p95", "e2e_p95", "ttfse_mean", "e2e_mean",
                      "ttfse_viol_500ms", "ttfse_viol_1000ms", "e2e_viol_15000ms"]:
                agg[(r["arm"], r["cancel_rate"])][k].append(r[k])
    for (arm, cancel), d in sorted(agg.items()):
        m = {k: round(float(np.mean(v)), 3) for k, v in d.items()}
        print(f"{arm:7s} c{int(cancel*100):02d}  ttfse95={m['ttfse_p95']:8.1f}  e2e95={m['e2e_p95']:8.1f}  "
              f"ttfse_mean={m['ttfse_mean']:7.1f}  viol>500ms={m['ttfse_viol_500ms']:.3f}  "
              f"viol>1s={m['ttfse_viol_1000ms']:.3f}  e2e>15s={m['e2e_viol_15000ms']:.3f}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Plot the harm surface: broken-vs-fixed survivor TTFT p95 gap over
cancel rate x request rate (KV pressure). Paper core figure.

Panel (a): heatmap of the broken/fixed TTFT-p95 ratio (log scale).
Panel (b): 2x2 absolute TTFT p95 curves per request rate, broken vs
fixed, with rep spread at r=12 and the vLLM healthy-baseline reference.

Input: results/harm_surface_8b.csv (from extract_harm_surface.py)
Output: results/harm_surface_8b.png / .pdf
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm

HERE = Path(__file__).resolve().parent
CSV = HERE / "results" / "harm_surface_8b.csv"
OUT_PNG = HERE / "results" / "harm_surface_8b.png"
OUT_PDF = HERE / "results" / "harm_surface_8b.pdf"

RATES = [4, 8, 12, 16]
CANCELS = [0.0, 0.1, 0.2, 0.4]
REGIME = {4: "unsaturated\n(queue 0, KV ≤ 0.65)", 8: "knee\n(queue forms)",
          12: "saturated\n(KV 1.00, queue ~48)", 16: "overloaded\n(queue ~51)"}


def main() -> None:
    df = pd.read_csv(CSV)
    df = df[df.arm.isin(["broken", "fixed", "vllm"])]

    # mean over reps per (arm, rate, cancel)
    g = (df.groupby(["arm", "rate", "cancel_rate"], as_index=False)
           .agg(ttft_p95=("ttft_p95_ms", "mean"),
                ttft_p95_min=("ttft_p95_ms", "min"),
                ttft_p95_max=("ttft_p95_ms", "max"),
                n=("ttft_p95_ms", "size")))

    fig = plt.figure(figsize=(13.2, 5.4))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.55], wspace=0.24)

    # ---------- Panel (a): gap heatmap ----------
    ax = fig.add_subplot(gs[0])
    ratio = np.full((len(RATES), len(CANCELS)), np.nan)
    for i, r in enumerate(RATES):
        for j, c in enumerate(CANCELS):
            b = g[(g.arm == "broken") & (g.rate == r) & (g.cancel_rate == c)]
            f = g[(g.arm == "fixed") & (g.rate == r) & (g.cancel_rate == c)]
            if len(b) and len(f):
                ratio[i, j] = b.ttft_p95.iloc[0] / f.ttft_p95.iloc[0]
    im = ax.imshow(ratio, norm=LogNorm(vmin=0.9, vmax=150), cmap="inferno_r",
                   aspect="auto", origin="lower")
    ax.set_xticks(range(len(CANCELS)), [f"{int(c*100)}%" for c in CANCELS])
    ax.set_yticks(range(len(RATES)), [f"r={r}" for r in RATES])
    ax.set_xlabel("client cancel rate")
    ax.set_ylabel("request rate (KV pressure →)")
    ax.set_title("(a) survivor TTFT p95 gap: broken / fixed", fontsize=11)
    for i in range(len(RATES)):
        for j in range(len(CANCELS)):
            v = ratio[i, j]
            if np.isnan(v):
                continue
            txt = f"{v:.0f}×" if v >= 10 else f"{v:.2f}×"
            color = "black" if v < 8 else "white"  # inferno_r: light cells for small ratios
            ax.text(j, i, txt, ha="center", va="center", fontsize=9, color=color)
    cb = fig.colorbar(im, ax=ax, pad=0.02)
    cb.set_label("TTFT p95 ratio (broken ÷ fixed)", fontsize=9)
    # grid lines
    for x in np.arange(-0.5, len(CANCELS), 1):
        ax.axvline(x, color="gray", lw=0.5, alpha=0.5)
    for y in np.arange(-0.5, len(RATES), 1):
        ax.axhline(y, color="gray", lw=0.5, alpha=0.5)

    # ---------- Panel (b): per-rate curves ----------
    gsb = gs[1].subgridspec(2, 2, hspace=0.52, wspace=0.30)
    for k, r in enumerate(RATES):
        axb = fig.add_subplot(gsb[k])
        for arm, color, marker, label in [("broken", "#c0392b", "o", "SGLang 0.5.19 (broken)"),
                                          ("fixed", "#27ae60", "s", "SGLang + PR#35255 (fixed)")]:
            sub = g[(g.arm == arm) & (g.rate == r)].sort_values("cancel_rate")
            axb.plot(sub.cancel_rate * 100, sub.ttft_p95, marker=marker, color=color,
                     lw=1.8, ms=5, label=label)
            # rep spread where >1 rep
            multi = sub[sub.n > 1]
            if len(multi):
                axb.fill_between(multi.cancel_rate * 100, multi.ttft_p95_min,
                                 multi.ttft_p95_max, color=color, alpha=0.18)
        if r == 12:
            v = df[(df.arm == "vllm") & (df.rate == 12)].sort_values("cancel_rate")
            axb.plot(v.cancel_rate * 100, v.ttft_p95_ms, marker="^", color="#2980b9",
                     lw=1.4, ms=5, ls=":", label="vLLM 0.28 (healthy baseline)")
        axb.set_yscale("log")
        axb.set_ylim(40, 15000)
        axb.set_xticks([0, 10, 20, 40])
        axb.set_title(f"r={r} req/s — {REGIME[r]}", fontsize=9.5)
        axb.grid(True, which="both", lw=0.3, alpha=0.5)
        if k % 2 == 0:
            axb.set_ylabel("survivor TTFT p95 (ms)", fontsize=9)
        if k >= 2:
            axb.set_xlabel("client cancel rate (%)", fontsize=9)
        if k == 0:
            axb.legend(fontsize=8, loc="upper left", framealpha=0.9)
    fig.suptitle("Cancellation harm surface — Qwen3-8B, RTX 5090, KV pool 22,720 tokens, "
                 "FIN after 8 SSE events, max_tokens=512 (3 reps at r=12)", fontsize=10.5, y=0.99)
    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    print(f"wrote {OUT_PNG}\nwrote {OUT_PDF}")


if __name__ == "__main__":
    main()

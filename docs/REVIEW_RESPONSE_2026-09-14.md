# Review Response Round 1 — Credibility Fixes from Frozen Data (2026-09-14)

Responses to the external review, using only frozen artifacts (no new GPU runs).
Each item lists the verdict and the evidence.

## 1. Citation verification (review point 7)

Both citations the review raised are **real and accurate** (verified 2026-09-14):

- **Sethi et al., "Cancellation in Systems: An Empirical Study of Task
  Cancellation Patterns and Failures", OSDI 2022**
  (usenix.org/conference/osdi22/presentation/sethi). Studies 62
  cancel-feature requests + 156 cancel bugs across 13 systems
  (Java/C#/Go); trigger/propagation/execution taxonomy; static
  anti-pattern checkers. → Our related-work must position against this:
  their scope is generic distributed systems + static detection; ours is
  LLM-serving-specific runtime resource accounting (KV block-seconds) and
  survivor-SLO causality under load. No "first quantitative" claims.
- **NVIDIA AIPerf request-cancellation testing** (docs v0.12.0, real):
  `--request-cancellation-rate` + `--request-cancellation-delay` measure
  impact of cancellations on surviving load. → We cannot claim benchmarks
  ignore cancellation. Our delta: internal lifecycle verification
  (abort-propagation evidence), resource-reclaim accounting, and a causal
  broken/fixed A/B, which AIPerf-style black-box testing does not provide.

## 2. Metric semantics (review point 3)

- **TTFT → TTFSE.** The probe records time to the first complete SSE
  `data:` record. For OpenAI chat streaming the first record typically
  carries the role delta (empty content), so the metric is *time to first
  SSE event*, at most one decode flush (~10–30 ms) away from true TTFT.
  Payloads were not logged, so offline recompute is impossible; all
  reporting renamed to TTFSE, and probe v2 (parse `delta.content`) is on
  the GPU todo list. Headline comparisons (60 ms vs 6.8 s) are unaffected
  by a ±1-event offset.
- **useful_tokens = sse_events − 1** overestimates tokens by ~2–3 per
  request (role chunk, [DONE], optional usage chunk; observed 515 events
  for 512-token completions). Fine within one runtime; not a
  cross-runtime unit. Reporting switches to "useful SSE tokens (proxy)".
- **SSE parsing robustness**: the probe buffers raw TCP bytes and waits
  for complete `data:...\n\n` records; HTTP chunk headers contain single
  CRLF so they cannot terminate a record early. Residual risk = the
  literal string "data:" inside generated content inflating counts — the
  fixed workload (numbered list) generates digits/punctuation only, so
  counts are exact for this workload; documented as a workload-dependent
  caveat.

## 3. Inter-case contamination (review point 4) — ruled out

Script: `code/check_intercase_contamination.py`; data:
`data/intercase_contamination.csv`. Trace events are mapped to cases via
the per-case probe run_id embedded in every audit id.

Across all 10 surface cells: **0 KV_RELEASED events and 0 scheduler
decode iterations from case k occur after case k+1 starts** (checked for
c00/c10/c20 → next-case boundaries). Mechanistically: survivors under
queueing always outlive the orphans they coexist with (orphans die
~7.6–10 s after cancel; survivors at saturation take 11–17 s), plus the
3 s inter-case sleep. Contamination cannot explain the harm numbers.

(Side observation: broken cells emit ~120 KV_RELEASED per 100-request
case — retraction/retry under memory pressure releases KV too; worth a
footnote when we quantify orphan KV block-seconds.)

## 4. Distribution-level analysis (review point 6) — the cliff is real

Script: `code/analyze_survivor_dist.py`; data:
`data/survivor_dist_8b.csv`; figure: `figures/survivor_cdf_r12.png`.

r=12, pooled over 3 reps (survivors only):

| arm | cancel | TTFSE p95 | E2E p95 | TTFSE mean | viol >500ms | viol >1s | E2E viol >15s |
|---|---|---:|---:|---:|---:|---:|---:|
| broken | 0% | 5702 | 16280 | 1424 | 0.247 | 0.247 | 0.247 |
| broken | 10% | 6255 | 17025 | 1754 | 0.286 | 0.286 | 0.286 |
| broken | 20% | 5614 | 16257 | 1375 | 0.254 | 0.254 | 0.284 |
| broken | 40% | 6540 | 17274 | 1672 | 0.254 | 0.254 | 0.254 |
| fixed | 0% | 5571 | 15981 | 1357 | 0.240 | 0.240 | 0.240 |
| fixed | 10% | 5749 | 16111 | 1083 | 0.187 | 0.187 | 0.187 |
| fixed | 20% | 4216 | 14122 | 316 | 0.062 | 0.062 | 0.000 |
| fixed | 40% | **60.5** | 11467 | 52 | **0.000** | **0.000** | **0.000** |

Findings:

1. **Not a percentile artifact.** The TTFSE distribution is bimodal:
   fast-admitted survivors (~55 ms) + a queued population (~5–7 s). At
   r=12 c40, **25.4% of broken-arm survivors occupy the queued mode vs
   0.0% in fixed** — the gap is a population shift of a quarter of all
   survivors, not a p95 threshold-crossing fluke. (viol>500ms ==
   viol>1s everywhere: violators are all far above 1 s.)
2. **E2E must be reported beside TTFSE** (review was right): E2E p95
   ratio at c40 is only 17.3/11.5 ≈ 1.5×, but the E2E *SLO-violation*
   story is equally stark (25.4% vs 0% beyond 15 s). Both framings in
   the paper.
3. c00 sanity holds at the distribution level: broken/fixed violation
   rates 24.7% vs 24.0% — identical queueing without cancels.

## 5. Status vs the review's priority list

| item | status |
|---|---|
| P0 metric semantics (TTFSE etc.) | documented + renamed here; probe v2 on GPU todo |
| P0 instrumentation on/off A/B | **needs GPU** (~20 min), P0 confirmed |
| P0 longer arrival window / seed-varied paired reps / drain check | drain check: ruled out above; longer window + seed variation **needs GPU** |
| P1 KV-budget sweep (mediator test) | **needs GPU** — the key causality experiment |
| P1 heterogeneous lengths / cancel times / prefix miss | needs GPU |
| P1 SLO-defined analysis | done above (thresholds explicit, arbitrary but stated) |
| citations | verified real; related-work rewrite owed |
| "transient vs steady-state" wording | accepted: current results = transient queueing amplification under finite bursts |

## 6. Language corrections adopted

Per the review's table, all applied to future text: "fails in 100% of
measured trials under the tested configuration"; "resources become
reclaimable" (not "memory returned"); "broken ≈ flat, fixed improves with
cancel rate"; "c00 provides a matched control"; "cancellation reduces
remaining service demand" (not "free admission control"); "healthy
signature" scoped to the resource-bound regime; disconnect-detection
timing attributed to the background-task hook specifically.

---

# Round 2 (2026-09-14, still frozen-data-only)

## R2.1 Instrumentation perturbation — accepted as P0, now a 3-way design

Accepted. Next GPU session runs broken/fixed under three observation
levels: (a) no audit hooks (CANCEL_AUDIT_TRACE unset; residual cost is
one env-var check per call, <1 µs), (b) light hooks (cancel/abort/
release events only — ~1.5k events/cell), (c) full hooks (current;
~236k scheduler-iteration events/cell, ~150× the light volume). The
queued-population gap (25.4% vs 0%) must reproduce under (a) before the
magnitude numbers are claimed. Prior expectation from mechanism data:
the 30.6 ms vs 10.8 s cancel-to-release contrast is independent of
scheduler-loop logging, so the population shift should survive; the
exact ratio (108×) may move.

## R2.2 Contamination check v2 — wording fixed, strict boundaries clean

The reviewer was right: round-1 code used `ts > next_start + 1.0` for
scheduler iterations while the text claimed "after next start". v2
(`code/check_intercase_contamination_v2.py`,
`data/intercase_contamination_v2.csv`) reports three boundaries
(0 ms / 100 ms / 1 s) for both KV_RELEASED and SCHEDULER_ITERATION:

- **All 30 intra-cell case boundaries: 0 spill at every boundary,
  including the strict 0 ms one** — the round-1 conclusion holds with
  the corrected, stricter test.
- **c40 → next-cell boundary** (server restart between cells): the gap
  between a cell's last trace event and the next cell's first request is
  79–85 s for all 9 chronological cell transitions (including the
  broken→fixed arm switch) — a full server restart with fresh KV pool
  separates cells; no cross-cell orphan inheritance is possible.

## R2.3 Orphan KV-residency accounting — first resource numbers

`code/analyze_orphan_kv.py`, `data/orphan_kv_8b.csv`. Per cancelled
request, residency = KV_RELEASED − CLIENT_CANCEL_INIT (exact from
traces; token footprint is the only estimated term, mean ≈ prompt+260
tokens under linear decode growth, prompt≈25):

| arm | case | residency p50 | orphan slot-s | orphan token-s (est) | share of KV-pool-time (est) |
|---|---|---:|---:|---:|---:|
| broken | r12 c10 | 11.0 s | 98 | 28k | 5.2% |
| broken | r12 c20 | 10.7 s | 181 | 51k | 9.4% |
| broken | r12 c40 | 10.8 s | 393 | **112k** | **20.9%** |
| broken | r16 c40 | 10.5 s | 388 | 110k | 21.4% |
| broken | r4 c40 | 10.4 s | 426 | 121k | 17.5% |
| fixed | any c40 | **0.033 s** | 1.6 | ~430 | **0.1%** |

Two directly quotable findings:

1. **Residency is constant per orphan (~10.8 s ≈ the remaining 504
   tokens' decode time) and independent of load** — only the orphan
   *count* scales with cancel rate. The knob that turns waste into harm
   is therefore whether the pool is saturated, not how long orphans
   live.
2. **The waste fraction alone does not determine harm**: r=4 c40 wastes
   a comparable share (17.5%) with zero survivor impact (pool never
   fills, usage ≤ 0.65), while r=12/r16 c40 waste ~21% with 25% of
   survivors queued. Consistent with the capacity-boundary model; the
   KV-budget sweep (GPU) remains the direct mediator test.

## R2.4 SLO framing — attainment curves, not picked thresholds

The CDF figure *is* the attainment curve P(TTFSE ≤ x) / P(E2E ≤ y);
the 500 ms/1 s/15 s rows were illustrative slices. Text now reads:
"tail-latency exceedance analyzed at multiple thresholds; full
attainment curves in figures/survivor_cdf_r12.png; deployment SLOs to
be pre-registered from workload targets in future runs." The claim
"SLO-defined analysis done" is withdrawn.

## R2.5 Statistical units — corrected

- Round-1's "pooled over 3 reps" table reported *means of per-run
  percentiles*; only the CDF pooled raw requests. Corrected labels
  everywhere. Requests within a run share queue state, so **the run is
  the unit of repetition**.
- Per-run r=12 stats (the honest presentation):

| arm | case | TTFSE p95 per run (ms) | mean ± sd | viol>500ms per run |
|---|---|---|---|---|
| broken | c00 | 6015 / 5542 / 5548 | 5702 ± 272 | 0.24 / 0.24 / 0.26 |
| broken | c40 | 6818 / 6223 / 6579 | 6540 ± 299 | 0.254 / 0.237 / 0.271 |
| fixed | c00 | 5549 / 5616 / 5548 | 5571 ± 39 | 0.24 / 0.24 / 0.24 |
| fixed | c40 | 59.8 / 61.1 / 60.5 | 60.2 ± 0.7 | 0 / 0 / 0 |

- Accepted: the 3 reps reused identical arrival seeds (per-case seeds
  5000+idx), so they measure runtime jitter on one arrival trace, not
  workload generalization. Next GPU round: ≥5 distinct seeds,
  broken/fixed paired on the same seed (nested cancel sets).

## R2.6 GPU minimal package (unchanged scope, now explicit)

1. 3-way instrumentation A/B (none / light / full) — locks the magnitude.
2. KV-budget sweep at fixed workload (mem-fraction 0.4/0.5/0.6/0.7) —
   does the harm threshold move with the pool? The mediator test.
3. ≥5-seed paired reps at r=12 — workload generalization.
   (Plus if budget allows: 0.6B fixed arm; heterogeneous lengths.)

## R2.7 Framing adopted

Title scope: "LLM 推理服务中请求取消传播失效的测量与容量影响分析".
Working thesis statement (from the review, adopted):
cancellation-propagation delay retains already-worthless service demand;
when its occupancy crosses the runtime's capacity boundary, normal
requests form a *predictable* queued population, and the boundary is
explainable from the resource budget and post-cancel remaining work.

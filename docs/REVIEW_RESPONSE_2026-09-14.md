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

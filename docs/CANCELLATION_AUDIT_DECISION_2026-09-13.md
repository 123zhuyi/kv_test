# Cancellation Audit Decision Update

> Updated: 2026-09-13 (late evening), after the KV-bound broken-vs-fixed A/B experiment.

## Decision

**GO (revised from WATCH).** The broken-vs-fixed A/B under KV-capacity-bound load produced the clean causal harm claim that earlier within-SGLang sweeps structurally could not: at 40% cancellation, SGLang 0.5.19's broken cancellation path inflates survivor TTFT p95 from **60 ms to 6.8 s (~114×)** relative to the fixed runtime, under identical workload.

## Bottom Line

- Phenomenon: YES.
- Stable orphan compute (SGLang ≤ 0.5.19): YES — root-caused and OBSERVED end-to-end.
- Survivor SLO harm: **YES — but only visible as an A/B against a prompt-cancellation runtime, and only in the KV-capacity-bound regime** (queue forms, token usage = 1.00). Within-SGLang cancel-rate sweeps are structurally blind to it (identical total server work at any cancel rate).
- Paper direction: **GO** — cross-runtime cancellation audit + root cause + fix validation + quantified capacity cost.

## The Decisive A/B (Qwen3-8B, RTX 5090, KV-bound)

Setup: SGLang, 8B bf16, `--mem-fraction-static 0.60` → max_total_num_tokens=22720 (KV-limited concurrency ≈ 41 requests). r=12/s, 100 requests/case, max_tokens=512, FIN after 8 SSE events, cancel rate sweep. Server saturated: #queue-req peak 44-48, token usage 1.00. Two sides, identical harness and seeds:

- **broken** = SGLang 0.5.19 (latest release, contains the bug)
- **fixed (backport)** = same wheel + PR #35255 backport (10/12 hunks clean, 1 hand-applied, 1 already present) + identical trace hooks
- **fixed (official main)** = upstream main @ `f9fca0580340a0c3ff4d5a3fc78f03a7a60de3ca`, installed with `pip install --no-deps --target` (shadows the venv via PYTHONPATH, reusing its sgl-kernel/torch) + identical trace hooks

| case | side | TTFT p50 | TTFT p95 | TTFT p99 | E2E p95 | wall |
|---|---|---:|---:|---:|---:|---:|
| c00 | broken | 56.1 | 6015 | 6102 | 16663 | 24.8 |
| c00 | fixed (backport) | 53.6 | 5549 | 5656 | 15867 | 24.0 |
| c00 | fixed (official main) | 53.3 | 5256 | 5321 | 15426 | 23.7 |
| c00 | vLLM 0.28.0 (healthy baseline) | 58.0 | 629 | 899 | 9715 | 17.9 |
| c10 | broken | 57.7 | 6406 | 6554 | 17360 | 24.1 |
| c10 | fixed (backport) | 54.6 | 5672 | 5790 | 15920 | 22.8 |
| c10 | fixed (official main) | 54.3 | 5440 | 5577 | 15561 | 22.4 |
| c10 | vLLM 0.28.0 (healthy baseline) | 53.0 | 74.2 | 1188 | 9888 | 17.3 |
| c20 | broken | 57.4 | 6023 | 6078 | 16995 | 25.1 |
| c20 | fixed (backport) | 52.1 | 4255 | 4314 | 14104 | 23.2 |
| c20 | fixed (official main) | 51.6 | 4044 | 4172 | 13717 | 22.8 |
| c20 | vLLM 0.28.0 (healthy baseline) | 52.9 | 74.1 | 82.9 | 8832 | 17.6 |
| c40 | broken | 57.5 | 6818 | 6972 | 17609 | 24.1 |
| c40 | fixed (backport) | 51.8 | **59.8** | 62.9 | 11517 | 17.0 |
| c40 | fixed (official main) | 50.7 | **60.3** | 61.7 | 11105 | 16.9 |
| c40 | vLLM 0.28.0 (healthy baseline) | 52.5 | **64.7** | 66.2 | 8773 | 15.4 |

Fixed-arm lifecycle (70 cancelled requests each): backport post-cancel iterations mean 0.94 (max 2), cancel-to-KV-release p50 = 30.6 ms, p99 = 75.8 ms; official main post-cancel iterations mean 0.99 (max 2), cancel-to-KV-release p50 = 30.6 ms, p99 = 51.2 ms. **Official main (commit f9fca05) matches the backport within run-to-run noise on every metric** — upstream's fix empirically validates our diagnosis. c00 sanity: all three arms match (same total work).

**vLLM healthy-baseline arm (2026-09-13, run 3):** vLLM 0.28.0 under the identical harness and KV-bound regime (KV pool 21,696 tokens vs SGLang's 22,720 — within 5%; KV usage peak 94.5%, Running peak 60, decode throughput peak 3874 tok/s). vLLM shows the healthy signature: TTFT p95 *improves* with cancel rate (629 ms c00 → 64.7 ms c40) because freed KV relieves pressure, and its c40 TTFT p95 (**64.7 ms**) matches the two fixed SGLang arms (59.8/60.3 ms) — 105× better than broken SGLang (6818 ms). Cancelled-request lifecycle: 70/70 complete, **0 post-cancel decode iterations** (abort lands before the next step), cancel-to-KV-release p50 = 9.0 ms, p99 = 13.1 ms. Two cross-runtime caveats: (1) vLLM admits without an admission queue (preemption-based; Waiting always 0), so its absolute TTFT/E2E levels are lower than SGLang's queueing policy at every cancel rate — the comparison is the cancel-rate trend and the c40 gap, not absolute ms; (2) attention/sampler backends differ (flash-attn vs triton on sm120). Both fixed SGLang arms reproduce the healthy-runtime signature; the broken release does not.

Interpretation:

- Orphan KV occupancy scales with cancel rate; in the KV-bound regime each orphan holds a scarce slot for its full remaining generation (~10 s here), directly inflating survivor queue wait. The effect is superlinear in cancel rate: negligible at 10%, ~30% TTFT-p95 inflation at 20%, ~114× at 40%.
- The c00 match confirms the difference is the cancellation path, not run-to-run drift.
- Harm mechanism is **capacity loss** (KV slots + decode step rows), invisible on small models (0.6B run: flat even at batch 100, KV 29%) because decode there is overhead-bound and admission is never reached.

## Harm Surface (2026-09-14, matrix run + local analysis)

Matrix: broken/fixed arms × r ∈ {4, 8, 12, 16} req/s × cancel {0/10/20/40%}, 100 req/case, r=12 has 3 reps/arm (rep 1 = the original A/B above). Driver: `remote_harm_surface.sh` (single-rate calibration pins the main sweep at each rate). Measured pressure per rate (from server logs): r=4 queue 0, KV ≤ 0.65 (unsaturated); r=8 queue forms (≤ 41); r=12 KV 1.00, queue ~48; r=16 queue ~51. Figure: `results/harm_surface_8b.png/.pdf`; tidy data: `results/harm_surface_8b.csv` (52 rows, per-case pressure indicators included).

Survivor TTFT-p95 gap (broken ÷ fixed, mean of reps):

| rate | c00 | c10 | c20 | c40 |
|---|---:|---:|---:|---:|
| r=4 (unsaturated) | 0.98× | 1.01× | 0.95× | 1.05× |
| r=8 (knee) | 0.82× | 1.04× | **41×** | **71×** |
| r=12 (saturated) | 1.02× | 1.09× | 1.33× | **108×** |
| r=16 (overloaded) | 1.03× | 1.07× | 1.18× | **121×** |

Findings:

1. **Threshold structure**: no harm anywhere below KV saturation (r=4 flat at ~60 ms both arms, all cancel rates); harm ignites only once a queue exists, and grows superlinearly with cancel rate (r=12: 1.33× at c20 → 108× at c40).
2. **Load-shedding cliff in the fixed arm**: at r≥8 the fixed runtime's TTFT p95 *collapses* to ~60 ms once the cancel rate sheds enough offered load to drop below saturation (r=8: cliff at c20; r=12/16: cliff at c40). The broken runtime cannot shed — orphans hold KV slots to natural completion — and stays saturated at every cancel rate. Cancellation, when propagated, acts as free admission control; when broken, it acts as nothing.
3. **c00/c10 sanity everywhere**: ratios ≈ 1× (0.82–1.33) — the arms diverge only where cancellation should free capacity.
4. **Reproducibility**: r=12 c40 over 3 reps — broken 6818/6223/6579 ms (CV ≈ 4.5%), fixed 59.8/61.1/60.5 ms; the gap is two orders of magnitude above run-to-run noise.
5. vLLM baseline (r=12, dotted in the figure) tracks the fixed arm's cliff shape (629→65 ms), confirming the cliff is the healthy signature, not a patch artifact.

## Prior rounds (unchanged conclusions)

- 0.6B/5090 capacity-loss sweeps (r up to 32/s, batch 100, KV 29%): TTFT/E2E flat — killed the naive within-runtime harm claim.
- 8B/5090 within-SGLang sweep at KV saturation (queue 48, usage 1.00): still flat across cancel rates — total server work is identical at any cancel rate in a homogeneous workload, so the differential can only appear against a prompt-cancellation baseline. This structural point is itself a useful methodological finding for the paper.
- vLLM 0.28.0 direct path: good baseline (cancel-to-KV 13-22 ms).

## Root Cause of SGLang Orphan Compute (OBSERVED, unchanged)

`sglang/srt/managers/tokenizer_manager.py` in 0.5.19 (latest release):

1. Client FIN → Starlette tears down the SSE stream in <1 ms.
2. `GeneratorExit` inside `generate_request` → `except BaseException` → `_discard_pending_req_states` pops `rid_to_state[rid]` immediately.
3. `StreamingResponse` background abort task starts (+0.2-0.4 ms), sleeps a hardcoded 2 s, then calls `abort_request(rid)`.
4. Guard `rid not in self.rid_to_state → return` silently drops the abort; `AbortReq` never reaches the scheduler.
5. Orphan decodes to `max_tokens`; KV released only at natural completion.

Observed sequence (8/8 cancelled): REQ_STATE_DISCARDED (+0 ms) → SERVER_DISCONNECT_DETECTED (+0.35 ms) → ABORT_DROPPED_NO_STATE (+2000 ms) → KV_RELEASED (+7.6 s @512 tok; ~16 s @1024 tok). Non-streaming path has working `is_disconnected()` aborts; only streaming is broken.

Upstream: bug present in latest release 0.5.19; fixed on main by PR #35255 (merged 2026-09-04, unreleased); alternatives #34894/#35936 closed unmerged. Our backport of #35255 reproduces main's behavior (cancel-to-KV ~30 ms).

## Remaining Work for a Paper

1. ~~**vLLM A/B under the same KV-bound workload**~~ — done (2026-09-13): vLLM 0.28.0 shows the healthy signature (TTFT p95 629→64.7 ms as cancel rate rises; cancel-to-KV p50 9 ms, 0 post-cancel iterations), matching both fixed SGLang arms at c40. The harm is a cancellation-propagation property, not SGLang-fix-specific.
2. ~~Harm surface mapping~~ — done (2026-09-14): cancel rate × request rate (KV pressure) surface produced (`results/harm_surface_8b.png`), with 3 reps at r=12 for variance. Threshold structure + load-shedding cliff documented above. Model-size dimension remains two points (0.6B no-harm, 8B harm) — a third model would strengthen but is not blocking.
3. ~~Verify official main~~ — done (2026-09-13): main @ f9fca05 matches the backport on all metrics; "upstream fix validates our diagnosis" is now empirically supported.
4. Write-up assets already in hand: methodology (FIN/RST probe + 8-event trace hooks + lifecycle merge), root-cause case study with OBSERVED event chains, A/B table above.
5. Optional defensive experiments (needs GPU, not blocking): instrumentation on/off control run; RST-vs-FIN scope check (all current results are FIN-scope; declare this in the paper if RST is not run).

## Artifacts

- `results/capacity8b_remote/` — 8B round 1 (unsaturated, r≤4).
- `results/capacity8b2_remote/` — 8B round 2, broken side @ KV saturation (the broken A/B arm).
- `results/fixed8b_remote/` — fixed side A/B arm + lifecycle analysis.
- `results/vllm8b_remote/` — vLLM 0.28.0 healthy-baseline arm + lifecycle analysis + vllm env freeze.
- `results/capacity_remote_run1/`, `capacity_remote_run2/` — 0.6B sweeps (harm killed there).
- `results/mechanism_remote/` — root-cause verification traces.
- `instrument_sglang_0519.py`, `instrument_sglang_fixed.py` — trace hooks for broken/fixed code; PR backport lives on the remote (`/tmp/prtest/`, live files + `.live_broken_backup` for revert).

## Appendix A: 0.6B Capacity-Loss Detail (superseded by the 8B A/B above)

Two remote runs on SGLang 0.5.19 + Qwen3-0.6B + RTX 5090, FIN after 8 SSE events, max_tokens=512.

Run 1 (r=1/2/4/s calibration, 80 req/case, cancel 0/10/20/40%):

- Never saturated: max batch 42, KV usage ≤ 5%, queue-req = 0, max_running_requests = 2048 (uncapped).
- Orphan confirmed: 57 cancelled, cancel-to-KV-release p50 = 8293 ms (≈ full 512-token generation).
- Survivor TTFT p95: 52.7 (c00) → 54.2 ms (c40). Flat.
- Survivor E2E p99: 8557 (c00) → 8813 ms (c40). ~Flat.

Run 2 (r=4/8/16/32/s calibration with E2E-based knee, 100 req/case at knee r=32/s):

- Saturation indicators: batch up to 100, KV usage 29%, queue-req = 0, decode throughput up to ~5900 tok/s. E2E p95 stayed within 1.12× of the r=4 baseline — the 5090 + 0.6B stack stays overhead-bound even at batch 100.
- Survivor TTFT p50/p95/p99: 43.6/53.9/62.4 (c00) vs 45.9/54.0/55.8 ms (c40). Flat.
- Survivor E2E p99: 9975 (c00) vs 10003 ms (c40). +0.3%.
- useful tokens/s drops with cancel rate (4223 → 2608) but this is mechanical: cancelled requests produce no useful tokens by definition and the wall time is dominated by the fixed arrival span. Within-SGLang comparisons cannot separate this from orphan waste; the flat E2E shows the waste is genuinely free here.

**Why there is no harm on this setup:** admission is effectively uncapped (max_running_requests=2048, KV headroom 3×), and decode steps are launch/overhead-bound, not GPU-bound — an extra ~40 orphan rows in the batch do not measurably inflate step time. The orphan compute is real but free on this hardware/model.

## Appendix B: Root-Cause Detail (same as summary above, with evidence counts)

All previous rounds showed: disconnect detected ~1 ms, no ABORT_EMITTED, no SCHEDULER_ABORT_SEEN, KV released only at natural completion. Code audit + two new trace hooks (`REQ_STATE_DISCARDED`, `ABORT_DROPPED_NO_STATE`) now pin the mechanism in `sglang/srt/managers/tokenizer_manager.py` (0.5.19):

1. Client FIN → uvicorn reports disconnect → Starlette tears down the SSE stream in <1 ms.
2. Generator teardown delivers `GeneratorExit` (a `BaseException`) inside `generate_request`; its `except BaseException` handler runs `_discard_pending_req_states(obj)`, which **pops `rid_to_state[rid]` immediately** — a leak-fix cleanup that also fires on post-dispatch disconnect.
3. The `StreamingResponse` background task (`create_abort_task`) starts at the same moment (+0.2-0.4 ms) and then sleeps a **hardcoded 2 s** before calling `abort_request(rid)`.
4. `abort_request`'s guard `rid not in self.rid_to_state → return` now sees the already-discarded state and **silently drops the abort**; `AbortReq` is never dispatched to the scheduler.
5. The scheduler-side request is orphaned and decodes to `max_tokens`; KV is released only at natural completion.

Observed per-request sequence (8/8 cancelled requests, identical shape):

```text
+    0.00 ms  REQ_STATE_DISCARDED {was_present: True}
+    0.35 ms  SERVER_DISCONNECT_DETECTED {streaming_response_background_started}
+ 2000.12 ms  ABORT_DROPPED_NO_STATE        <- 2 s delayed abort silently swallowed
+ 7606.09 ms  KV_RELEASED {path: direct}    <- natural completion of 512 tokens
```

Trace counts for the 16-request verification run: 8 REQ_STATE_DISCARDED, 16 SERVER_DISCONNECT_DETECTED, 13 ABORT_DROPPED_NO_STATE (3 background tasks died with server shutdown), **0 ABORT_EMITTED, 0 SCHEDULER_ABORT_SEEN**, 8208 SCHEDULER_ITERATION (all 16 requests ran to completion server-side).

Notes:

- Only the **streaming** path is broken. Non-streaming requests have working `is_disconnected()` aborts ("type 1"/"type 3" paths in `_stream_one_response`).
- Even if the guard didn't drop it, the abort would arrive 2 s late (the hardcoded sleep) — the "suspected 2 s path" from earlier notes is this background task.
- vLLM 0.28.0's direct path remains the good baseline (cancel-to-KV 13-22 ms).

## Appendix C: Upstream Status Detail (checked 2026-09-13)

- **0.5.19 is the latest release** (PyPI + GitHub tags) and contains the bug (verified on the installed wheel: has `_discard_pending_req_states`, lacks `_release_req_states_on_failure`).
- Upstream main is **fixed**: PR [#35255](https://github.com/sgl-project/sglang/pull/35255) "Fix: abort handling for dispatched requests after client disconnect", merged 2026-09-04, replaces the discard with `_release_req_states_on_failure` + `state.dispatched` + `state.abort_sent`, aborting immediately at generator teardown instead of dropping state. Two alternative fix PRs were closed unmerged (#34894, #35936 — the latter titled "Prevent zombie requests after client disconnect", same diagnosis as ours).
- v0.5.19 was tagged 2026-09-04 (after the merge) but from a release branch that predates it, so the fix is currently **unreleased**.

Implication: the specific defect is independently known upstream and fixed on main. Our marginal contribution is not "new bug" but the **audit methodology + cross-runtime quantification + observed-evidence root-cause chain**, and potentially **empirical validation of the fix** and **harm characterization** (below).

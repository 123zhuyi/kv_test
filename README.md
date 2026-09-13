# kv_test — LLM Serving Cancellation-Path Audit

Evidence freeze for the cancellation-propagation audit of LLM serving runtimes
(SGLang 0.5.19 vs PR #35255 fix vs vLLM 0.28.0).

**Headline result** (Qwen3-8B, RTX 5090, KV-capacity-bound, 40% client FIN cancellation):
broken SGLang 0.5.19 leaves cancelled requests decoding to `max_tokens`
(observed root cause in the streaming abort path), inflating survivor TTFT p95
to **6818 ms vs 59.8 ms** on the fixed runtime under identical load.

- `MANIFEST.md` — versions, launch flags, seeds, model hashes, reproduction steps.
- `docs/CANCELLATION_AUDIT_DECISION_2026-09-13.md` — full decision write-up: A/B table, root cause, upstream status.
- `code/` — probe harness, experiment matrices, remote runners, trace instrumentation.
- `freeze_2026-09-13/` — raw data tarballs (traces, logs, CSVs) + checksums.
